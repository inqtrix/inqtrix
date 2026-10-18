"""Shared-in run enforcement (store matrix + recipient HTTP path).

The store half pins live direct-share resolution on the in-memory
:class:`~inqtrix.server.runs.RunStore`: the listing union with the
additive ``access`` annotation, the workspace-namespace bypass for
shared-in rows, view-grantees blocked from cancel (deny before
mutate), edit-grantees allowed, and the SSE replay admitted. The HTTP
half drives the real runs + shares routers through ``build_container``
with a header-based oidc test provider — the recipient-side path
``tests/test_share_routes.py`` deliberately defers to here.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
from inqtrix.auth.permissions import AuthorizationService, SharePermission
from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    AuthMode,
    AuthProvider,
    Principal,
    UserContext,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers import runs as runs_router
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.server.runs import RunNotFound, RunStore
from inqtrix.settings import Settings, StorageSettings
from inqtrix.user_events import MemoryUserEventStore

from tests.contract._app import StubLLM, StubSearch, minimal_agent_result

USER_ID_HEADER = "X-Test-User-Id"
# Shared route-test import retained while the consuming prompt/skill fixtures
# migrate their variable names; the header itself is canonical-user-id based.
SUB_HEADER = USER_ID_HEADER
WORKSPACE_HEADER = "X-Inqtrix-Workspace-Id"

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
RECIPIENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
STRANGER = uuid.UUID("33333333-3333-4333-8333-333333333333")


# ---------------------------------------------------------------------------
# Store-level matrix (memory RunStore with a live identity backend)
# ---------------------------------------------------------------------------


def make_store() -> tuple[RunStore, MemoryIdentityStore]:
    identity = MemoryIdentityStore()
    store = RunStore(
        max_concurrent=1,
        max_queue_size=8,
        completed_ttl_seconds=60,
        event_buffer_size=16,
    )
    store.bind_authorization(
        share_lookup=identity.permission_for_sync,
        share_workspace_check=identity.share_workspace_sync,
        resource_access_guard=identity.resource_access_guard_sync,
        restrict_to_workspace_members=False,
    )
    return store, identity


def scoped(user_id: uuid.UUID, *, tenant_id: str = "default") -> UserContext:
    return UserContext(
        principal=Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )
    )


def submit_completed(
    store: RunStore,
    *,
    user_id: uuid.UUID = OWNER,
    workspace_id: str | None = None,
) -> str:
    summary = store.submit(
        question="Testfrage?",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "ok"}),
        created_by_user_id=user_id,
        created_by_tenant_id="default",
        workspace_id=workspace_id,
    )
    run_id = summary["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if (
            store.get(run_id, visible_to=scoped(user_id))["status"]
            == "completed"
        ):
            return run_id
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not complete")


@pytest.mark.asyncio
async def test_memory_run_retention_revokes_share_and_invalidates_atomically():
    """TTL cleanup cannot leave a live share pointing at a removed run."""
    users = MemoryUserDirectory()
    for user_id, subject in ((OWNER, "owner"), (RECIPIENT, "recipient")):
        await users.record_login(
            tenant_id="default",
            issuer="https://issuer.example",
            subject=subject,
            email=f"{subject}@example.com",
            email_verified=True,
            display_name=subject.title(),
            canonical_user_id=user_id,
        )
    identity = MemoryIdentityStore()
    events = MemoryUserEventStore()
    identity.bind_user_event_sink(events.append_nowait)
    authority = MemoryAuthorityCoordinator()
    authority.bind_users(users)
    identity.bind_authority_coordinator(authority)
    store = RunStore(
        max_concurrent=1,
        max_queue_size=1,
        completed_ttl_seconds=0,
        event_buffer_size=8,
    )
    store.bind_authority_coordinator(authority)
    imported = store.import_completed_run(
        source_run_id="external-run",
        question="Imported",
        stack_name="default",
        result={"answer": "done"},
        created_by_user_id=OWNER,
        created_by_tenant_id="default",
    )
    run_id = imported["run_id"]
    identity.add_share(
        recipient_user_id=RECIPIENT,
        resource_type="run",
        resource_id=run_id,
        permission=SharePermission.VIEW,
        granted_by_user_id=OWNER,
    )

    assert store.list(visible_to=scoped(OWNER)) == []
    assert identity.permission_for_sync(
        tenant_id="default",
        resource_type="run",
        resource_id=run_id,
        recipient_user_id=RECIPIENT,
    ) is None
    assert any(
        entry.action == "run.retention_deleted"
        and entry.resource_id == run_id
        for entry in identity.audit_entries
    )
    page = await events.page_after(
        tenant_id="default", target_user_id=RECIPIENT, cursor=0
    )
    assert any(
        event.scope == "runs" and event.resource_id == run_id
        for event in page.events
    )


def test_view_grantee_reads_but_cannot_cancel():
    store, identity = make_store()
    run_id = submit_completed(store)
    identity.add_share(
        recipient_user_id=RECIPIENT,
        resource_type="run",
        resource_id=run_id,
        permission=SharePermission.VIEW,
        granted_by_user_id=OWNER,
    )
    recipient = scoped(RECIPIENT)

    summary = store.get(run_id, visible_to=recipient)
    assert summary["access"] == {"mode": "shared", "permission": "view"}

    result = store.result(run_id, visible_to=recipient)
    assert result["answer"] == "ok"

    subscription = store.subscribe(
        run_id, visible_to=recipient
    )
    try:
        assert any(
            event["type"] == "inqtrix.run.completed"
            for event in subscription.replay
        )
    finally:
        subscription.close()

    with pytest.raises(RunNotFound):
        store.cancel(run_id, visible_to=recipient)


def test_owner_summary_reports_owner_access_mode():
    store, _identity = make_store()
    run_id = submit_completed(store)
    summary = store.get(run_id, visible_to=scoped(OWNER))
    assert summary["access"] == {"mode": "owner"}


def test_stranger_stays_blind_despite_other_grants():
    """A grant map for OTHER runs must not admit this one."""
    store, identity = make_store()
    run_id = submit_completed(store)
    identity.add_share(
        recipient_user_id=STRANGER,
        resource_type="run",
        resource_id="run_other",
        permission=SharePermission.VIEW,
        granted_by_user_id=OWNER,
    )
    with pytest.raises(RunNotFound):
        store.get(run_id, visible_to=scoped(STRANGER))


def test_list_union_bypasses_workspace_filter():
    """Shared-in rows join the listing regardless of the caller's
    workspace namespace — they carry the grantor's workspace id."""
    store, identity = make_store()
    shared_run = submit_completed(store, workspace_id="ws-owner")
    own_run = submit_completed(
        store, user_id=RECIPIENT, workspace_id="ws-recipient"
    )
    identity.add_share(
        recipient_user_id=RECIPIENT,
        resource_type="run",
        resource_id=shared_run,
        permission=SharePermission.VIEW,
        granted_by_user_id=OWNER,
    )

    listed = store.list(
        workspace_id="ws-recipient",
        visible_to=scoped(RECIPIENT),
    )
    by_id = {item["run_id"]: item for item in listed}
    assert set(by_id) == {own_run, shared_run}
    assert by_id[own_run]["access"] == {"mode": "owner"}
    assert by_id[shared_run]["access"] == {
        "mode": "shared",
        "permission": "view",
    }
    # The shared-in row keeps the GRANTOR's workspace id visible.
    assert by_id[shared_run]["workspace_id"] == "ws-owner"


def test_view_cancel_denies_before_mutating_running_run():
    """A view grantee's cancel must not set the cancel event."""
    store, identity = make_store()
    started = threading.Event()
    release = threading.Event()

    def blocking_work(handle):
        started.set()
        release.wait(timeout=5)
        handle.complete({"answer": "done"})

    run_id = store.submit(
        question="Testfrage?",
        stack_name="default",
        work=blocking_work,
        created_by_user_id=OWNER,
        created_by_tenant_id="default",
    )["run_id"]
    assert started.wait(timeout=5)
    try:
        identity.add_share(
            recipient_user_id=RECIPIENT,
            resource_type="run",
            resource_id=run_id,
            permission=SharePermission.VIEW,
            granted_by_user_id=OWNER,
        )
        with pytest.raises(RunNotFound):
            store.cancel(run_id, visible_to=scoped(RECIPIENT))
        record = store._records[run_id]
        assert not record.cancel_event.is_set()

        active = asyncio.run(
            identity.inbox_for_recipient(
                tenant_id="default", recipient_user_id=RECIPIENT
            )
        )[0]
        updated = asyncio.run(
            identity.update_share_permission(
                tenant_id="default",
                share_id=active.id,
                permission=SharePermission.EDIT,
                expected_revision=active.revision,
                actor_user_id=OWNER,
            )
        )
        assert updated is not None
        edit_summary = store.cancel(run_id, visible_to=scoped(RECIPIENT))
        assert record.cancel_event.is_set()
        assert edit_summary["status"] == "running"
    finally:
        release.set()


# ---------------------------------------------------------------------------
# Recipient-side HTTP path (runs + shares routers, real ShareService)
# ---------------------------------------------------------------------------


class OidcHeaderProvider(AuthProvider):
    """Test-only OIDC-mode provider over canonical user UUID headers.

    Reports ``mode == "oidc"`` and exposes a users mirror so
    ``build_container`` wires the real :class:`ShareService` with the
    run owner-resolver — the exact production composition minus token
    verification.
    """

    def __init__(self, users: MemoryUserDirectory) -> None:
        self._users = users
        self.revoke_live = False

    @property
    def mode(self) -> AuthMode:
        return "oidc"

    @property
    def users(self) -> MemoryUserDirectory:
        return self._users

    def resolve_principal(self, request: Request) -> Principal:
        raw_user_id = request.headers.get(USER_ID_HEADER, "")
        if not raw_user_id:
            return ANONYMOUS_PRINCIPAL
        try:
            user_id = uuid.UUID(raw_user_id)
        except ValueError:
            return ANONYMOUS_PRINCIPAL
        return Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )

    def build_principal_dependency(self):
        # The shares/users routers await the dependency — the real
        # oidc provider's is async, so this test double must be too.
        async def _dependency(request: Request) -> Principal:
            return self.resolve_principal(request)

        async def _live_dependency(request: Request) -> Principal:
            if self.revoke_live:
                from fastapi import HTTPException

                raise HTTPException(status_code=401, detail="revoked")
            return self.resolve_principal(request)

        setattr(_dependency, "__inqtrix_live_resolver__", _live_dependency)
        return _dependency


def make_sharing_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *args, **kwargs: minimal_agent_result(),
    )
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror_all() -> None:
        for user_id, subject, name in (
            (OWNER, "user-owner", "Olga Owner"),
            (RECIPIENT, "user-recipient", "Rita Recipient"),
            (STRANGER, "user-stranger", "Stefan Stranger"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=name,
                canonical_user_id=user_id,
            )

    asyncio.run(mirror_all())
    auth_provider = OidcHeaderProvider(users)
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            storage=StorageSettings(backend="memory", database_url="")
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=auth_provider,
        permissions=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        workspace_admin=identity,
    )
    assert container.share_service is not None
    app = FastAPI()
    app.include_router(runs_router.build_router(container))
    app.include_router(build_shares_router(container))
    from inqtrix.server.routers import agent_runs as agent_runs_router
    from inqtrix.server.routers import agent_sessions as agent_sessions_router

    app.include_router(agent_runs_router.build_router(container))
    app.include_router(agent_sessions_router.build_router(container))
    client = TestClient(app)
    # Store handles for tests that need agent trees / control fixtures
    # (the HTTP API here only creates standard runs; agent runs, approvals
    # and artifacts are seeded as store-level primitives).
    client.run_store = container.run_store  # type: ignore[attr-defined]
    client.agent_control = container.agent_control_service  # type: ignore[attr-defined]
    client.auth_provider = auth_provider  # type: ignore[attr-defined]
    return client


def user_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Return the test provider's canonical identity header."""
    return {USER_ID_HEADER: str(user_id)}


def create_completed_run(
    client: TestClient, *, user_id: uuid.UUID = OWNER
) -> str:
    response = client.post(
        "/v1/runs",
        json={"question": "Testfrage?"},
        headers=user_headers(user_id),
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        summary = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(user_id)
        ).json()
        if summary.get("status") == "completed":
            return run_id
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not complete")


def grant_via_http(
    client: TestClient, run_id: str, *, permission: str = "view"
) -> dict[str, Any]:
    response = client.post(
        "/v1/shares",
        json={
            "resource_type": "run",
            "resource_id": run_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": permission}
            ],
        },
        headers=user_headers(OWNER),
    )
    assert response.status_code == 201
    return response.json()["data"][0]


def accept_via_http(client: TestClient, share_id: str) -> None:
    response = client.post(
        f"/v1/shares/{share_id}/accept", headers=user_headers(RECIPIENT)
    )
    assert response.status_code == 200


def update_share_via_http(
    client: TestClient,
    share_id: str,
    *,
    permission: str,
    expected_revision: int,
) -> dict[str, Any]:
    """Update one share through the owner-only revision contract."""
    response = client.patch(
        f"/v1/shares/{share_id}",
        json={
            "permission": permission,
            "expected_revision": expected_revision,
        },
        headers=user_headers(OWNER),
    )
    assert response.status_code == 200
    return response.json()["data"]


def test_http_recipient_sees_shared_run_until_revoked(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)

        before_grant = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
        )
        assert before_grant.status_code == 404

        share = grant_via_http(client, run_id)

        # Consent gate: a pending share grants nothing — still hidden.
        pending = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
        )
        assert pending.status_code == 404

        accept_via_http(client, share["id"])

        summary = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
        )
        assert summary.status_code == 200
        assert summary.json()["access"] == {
            "mode": "shared",
            "permission": "view",
        }

        result = client.get(
            f"/v1/runs/{run_id}/result", headers=user_headers(RECIPIENT)
        )
        assert result.status_code == 200
        assert "answer" in result.json()

        events = client.get(
            f"/v1/runs/{run_id}/events", headers=user_headers(RECIPIENT)
        )
        assert events.status_code == 200
        assert '"inqtrix.run.completed"' in events.text

        revoked = client.delete(
            f"/v1/shares/{share['id']}", headers=user_headers(OWNER)
        )
        assert revoked.status_code == 204
        after_revoke = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
        )
        assert after_revoke.status_code == 404


def test_run_stream_stops_before_replay_when_credential_is_revoked(monkeypatch):
    """The SSE frame boundary re-resolves credentials after route admission."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        client.auth_provider.revoke_live = True  # type: ignore[attr-defined]

        events = client.get(
            f"/v1/runs/{run_id}/events", headers=user_headers(OWNER)
        )

    assert events.status_code == 200
    assert events.text == ""


def test_json_polling_is_a_one_shot_read_not_a_stream_viewer(monkeypatch):
    """``?format=json`` must subscribe with ``stream=False``, SSE with True.

    Without the flag every ~3s poll of the 4a fallback registers a full
    subscription: a throwaway poller thread per poll and one viewer-
    histogram join per poll, biasing the 5b evidence gate. Killing
    mutant: dropping ``stream=not wants_json`` from the router call.
    """
    from inqtrix.server.runs import RunStore

    seen: list[bool] = []
    original = RunStore.subscribe

    def recording(
        self, run_id, *, workspace_id=None, visible_to=None, stream=True
    ):
        seen.append(stream)
        return original(
            self,
            run_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
            stream=stream,
        )

    monkeypatch.setattr(RunStore, "subscribe", recording)
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        seen.clear()
        polled = client.get(
            f"/v1/runs/{run_id}/events?format=json",
            headers=user_headers(OWNER),
        )
        assert polled.status_code == 200
        streamed = client.get(
            f"/v1/runs/{run_id}/events", headers=user_headers(OWNER)
        )
        assert streamed.status_code == 200
    assert seen == [False, True], (
        "the JSON polling fallback must be a one-shot replay read; "
        "only the SSE path is a stream viewer"
    )


def test_run_polling_rechecks_credential_before_returning_replay(monkeypatch):
    """The JSON fallback has the same final authorization boundary as SSE."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        client.auth_provider.revoke_live = True  # type: ignore[attr-defined]

        events = client.get(
            f"/v1/runs/{run_id}/events?format=json",
            headers=user_headers(OWNER),
        )

    assert events.status_code == 404
    assert events.json() == {
        "error": {"message": "Run not found", "type": "not_found"}
    }


def test_http_listing_union_uses_live_accepted_shares(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        shared_run = create_completed_run(client)
        own_run = create_completed_run(client, user_id=RECIPIENT)
        accept_via_http(client, grant_via_http(client, shared_run)["id"])

        listed = client.get(
            "/v1/runs", headers=user_headers(RECIPIENT)
        ).json()["data"]
        by_id = {item["run_id"]: item for item in listed}
        assert set(by_id) == {own_run, shared_run}
        assert by_id[own_run]["access"] == {"mode": "owner"}
        assert by_id[shared_run]["access"]["permission"] == "view"

        # The stranger's listing stays untouched by the grant.
        stranger_listed = client.get(
            "/v1/runs", headers=user_headers(STRANGER)
        ).json()["data"]
        assert stranger_listed == []


def test_http_inbox_accept_and_leave(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        share = grant_via_http(client, run_id)

        # The pending invitation shows in the recipient inbox, title-enriched
        # and carrying the grantor's display name.
        inbox = client.get(
            "/v1/shares/inbox", headers=user_headers(RECIPIENT)
        ).json()["data"]
        assert inbox["accepted"] == []
        assert len(inbox["pending"]) == 1
        invite = inbox["pending"][0]
        assert invite["id"] == share["id"]
        assert invite["resource_type"] == "run"
        assert invite["resource_id"] == run_id
        assert invite["resource_title"] == "Testfrage?"
        assert invite["permission"] == "view"
        assert invite["granted_by_display_name"] == "Olga Owner"
        assert invite["accepted_at"] is None

        # A stranger cannot accept; the share stays pending.
        assert (
            client.post(
                f"/v1/shares/{share['id']}/accept",
                headers=user_headers(STRANGER),
            ).status_code
            == 404
        )

        accept_via_http(client, share["id"])

        # After consent it moves to the accepted section.
        accepted_inbox = client.get(
            "/v1/shares/inbox", headers=user_headers(RECIPIENT)
        ).json()["data"]
        assert accepted_inbox["pending"] == []
        assert len(accepted_inbox["accepted"]) == 1
        assert accepted_inbox["accepted"][0]["accepted_at"] is not None

        # The recipient leaves: their own DELETE drops the share and access.
        left = client.delete(
            f"/v1/shares/{share['id']}", headers=user_headers(RECIPIENT)
        )
        assert left.status_code == 204
        assert client.get(
            "/v1/shares/inbox", headers=user_headers(RECIPIENT)
        ).json()["data"] == {"pending": [], "accepted": []}
        assert (
            client.get(
                f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
            ).status_code
            == 404
        )


def test_http_recipient_declines_pending(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        share = grant_via_http(client, run_id)
        # Decline a pending invitation: the recipient's DELETE before accepting.
        declined = client.delete(
            f"/v1/shares/{share['id']}", headers=user_headers(RECIPIENT)
        )
        assert declined.status_code == 204
        assert (
            client.get(
                "/v1/shares/inbox", headers=user_headers(RECIPIENT)
            ).json()["data"]["pending"]
            == []
        )
        # The owner's outgoing view no longer lists the resource.
        assert (
            client.get(
                "/v1/shares/mine", headers=user_headers(OWNER)
            ).json()["data"]
            == []
        )


def test_http_stranger_cannot_delete_share(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        share = grant_via_http(client, run_id)
        # Neither owner nor recipient: the DELETE dual-path denies (404), and
        # the invitation survives for the real recipient.
        denied = client.delete(
            f"/v1/shares/{share['id']}", headers=user_headers(STRANGER)
        )
        assert denied.status_code == 404
        inbox = client.get(
            "/v1/shares/inbox", headers=user_headers(RECIPIENT)
        ).json()["data"]
        assert len(inbox["pending"]) == 1


def test_http_mine_counts_pending_then_accepted(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        share = grant_via_http(client, run_id)

        before = client.get(
            "/v1/shares/mine", headers=user_headers(OWNER)
        ).json()["data"]
        assert len(before) == 1
        assert before[0]["resource_id"] == run_id
        assert before[0]["resource_title"] == "Testfrage?"
        assert before[0]["share_count"] == 1
        assert before[0]["pending_count"] == 1

        accept_via_http(client, share["id"])
        after = client.get(
            "/v1/shares/mine", headers=user_headers(OWNER)
        ).json()["data"]
        assert after[0]["share_count"] == 1
        assert after[0]["pending_count"] == 0


def test_http_cancel_needs_edit_grant(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = create_completed_run(client)
        view_share = grant_via_http(client, run_id, permission="view")
        accept_via_http(client, view_share["id"])

        # Accepted view grant: a view grantee still cannot cancel.
        denied = client.post(
            f"/v1/runs/{run_id}/cancel", headers=user_headers(RECIPIENT)
        )
        assert denied.status_code == 404

        # The owner upgrades the same accepted share through revision CAS;
        # recipient consent remains live without a second accept.
        updated = update_share_via_http(
            client,
            view_share["id"],
            permission="edit",
            expected_revision=view_share["revision"],
        )
        assert updated["revision"] == view_share["revision"] + 1
        allowed = client.post(
            f"/v1/runs/{run_id}/cancel", headers=user_headers(RECIPIENT)
        )
        assert allowed.status_code == 200


def test_http_children_follow_parent_view_share(monkeypatch):
    """A view share on the PARENT grants the children listing."""
    client = make_sharing_client(monkeypatch)
    with client:
        store = client.run_store  # type: ignore[attr-defined]
        release_parent = threading.Event()
        parent = store.submit(
            question="Agentenauftrag",
            stack_name="default",
            work=lambda handle: (
                release_parent.wait(timeout=2.0),
                handle.complete({"answer": "fertig"}),
            ),
            kind="agent",
            created_by_user_id=OWNER,
            created_by_tenant_id="default",
        )
        release_parent.set()
        child = store.submit(
            question="Teilaufgabe",
            stack_name="default",
            work=lambda handle: handle.complete({"answer": "teil"}),
            kind="agent_child",
            parent_run_id=parent["run_id"],
            root_run_id=parent["run_id"],
            created_by_user_id=OWNER,
            created_by_tenant_id="default",
        )
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if (
                store.get(
                    parent["run_id"], visible_to=scoped(OWNER)
                )["status"]
                == "completed"
                and store.get(
                    child["run_id"], visible_to=scoped(OWNER)
                )["status"]
                == "completed"
            ):
                break
            time.sleep(0.01)

        # Stranger and not-yet-invited recipient: indistinct 404.
        for user_id in (RECIPIENT, STRANGER):
            denied = client.get(
                f"/v1/runs/{parent['run_id']}/children",
                headers=user_headers(user_id),
            )
            assert denied.status_code == 404

        accept_via_http(
            client, grant_via_http(client, parent["run_id"])["id"]
        )

        listing = client.get(
            f"/v1/runs/{parent['run_id']}/children",
            headers=user_headers(RECIPIENT),
        )
        assert listing.status_code == 200
        payload = listing.json()
        assert payload["object"] == "list"
        assert [row["run_id"] for row in payload["data"]] == [child["run_id"]]
        assert payload["data"][0]["parent_run_id"] == parent["run_id"]

        # The child's DIRECT url stays owner-scoped (no grant on it).
        direct = client.get(
            f"/v1/runs/{child['run_id']}", headers=user_headers(RECIPIENT)
        )
        assert direct.status_code == 404
