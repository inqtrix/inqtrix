"""Visibility tests: scoped principals cannot see each other's runs.

Drives the real runs router through ``build_container`` with a
test-only auth provider that maps a request header to distinct OIDC
principals — the exact wiring an OIDC deployment gets, minus the
token verification. The core isolation property lives here:
principal A requesting principal B's run receives the identical
404 a missing run produces, while the legacy unscoped principals keep
today's see-everything behaviour bit-for-bit.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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
from inqtrix.server.runs import RunNotFound, RunStore
from inqtrix.settings import Settings, StorageSettings

from tests.contract._app import (
    StubLLM,
    StubSearch,
    minimal_agent_result,
    parse_sse_frames,
    wait_for_run_status,
)

SUB_HEADER = "X-Test-Sub"
TENANT_HEADER = "X-Test-Tenant"
WORKSPACE_HEADER = "X-Inqtrix-Workspace-Id"


def _user_id(label: str) -> uuid.UUID:
    """Map a test header label to a stable canonical user UUID."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"inqtrix-visibility:{label}")


class HeaderSubAuthProvider(AuthProvider):
    """Test-only provider: the sub header selects an OIDC principal.

    Requests without the sub header resolve to the anonymous principal
    so one client exercises both the scoped and the legacy path. The
    optional tenant header simulates multi-tenant deployments.
    """

    def __init__(self) -> None:
        self.users = _HeaderUsers()

    @property
    def mode(self) -> AuthMode:
        return "none"

    def resolve_principal(self, request: Request) -> Principal:
        sub = request.headers.get(SUB_HEADER, "")
        if not sub:
            return ANONYMOUS_PRINCIPAL
        tenant_id = request.headers.get(TENANT_HEADER, "default")
        return Principal(
            user_id=_user_id(sub),
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )


class _HeaderUsers:
    """Live-user lookup for the scoped principals emitted by the fixture."""

    async def find_by_user_id(self, *, tenant_id, user_id):
        return SimpleNamespace(user_id=user_id, disabled_at=None)

    async def has_user_id(self, *, tenant_id, user_id):
        return True


def make_visibility_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *args, **kwargs: minimal_agent_result(),
    )
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        # Storage pinned to memory: the offline suite must stay
        # hermetic even when the developer's .env configures the
        # Postgres backend for the dev server.
        settings=Settings(
            storage=StorageSettings(backend="memory", database_url="")
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=HeaderSubAuthProvider(),
    )
    app = FastAPI()
    app.include_router(runs_router.build_router(container))
    return TestClient(app)


def submit_run(client: TestClient, *, sub: str | None) -> dict[str, Any]:
    headers = {SUB_HEADER: sub} if sub else {}
    response = client.post(
        "/v1/runs", json={"question": "Testfrage?"}, headers=headers
    )
    assert response.status_code == 202
    return response.json()


def test_scoped_principal_cannot_see_anothers_run(monkeypatch):
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="user-a")

        as_b_get = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-b"}
        )
        as_b_result = client.get(
            f"/v1/runs/{run_id}/result", headers={SUB_HEADER: "user-b"}
        )
        as_b_cancel = client.post(
            f"/v1/runs/{run_id}/cancel", headers={SUB_HEADER: "user-b"}
        )
        missing = client.get(
            "/v1/runs/run_does_not_exist", headers={SUB_HEADER: "user-b"}
        )

    assert as_b_get.status_code == 404
    assert as_b_result.status_code == 404
    assert as_b_cancel.status_code == 404
    # Denial and absence are byte-identical (existence stays hidden).
    assert as_b_get.json() == missing.json()


def test_scoped_principal_sees_its_own_run(monkeypatch):
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        summary = wait_for_run_status_as(client, run_id, "completed", sub="user-a")
        result = client.get(
            f"/v1/runs/{run_id}/result", headers={SUB_HEADER: "user-a"}
        )

    assert summary["run_id"] == run_id
    assert result.status_code == 200
    assert result.json()["answer"].startswith("Antwort")


def test_scoped_principal_cannot_delete_anothers_run(monkeypatch):
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="user-a")

        as_b_delete = client.delete(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-b"}
        )
        missing = client.delete(
            "/v1/runs/run_does_not_exist", headers={SUB_HEADER: "user-b"}
        )
        # Owner-only is stronger than cancel (which a shared-in editor may
        # call): a non-owner's delete is the indistinct 404, and the run
        # survives for its owner.
        assert as_b_delete.status_code == 404
        assert as_b_delete.json() == missing.json()
        still_there = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-a"}
        )
        assert still_there.status_code == 200

        # The owner can delete it.
        owner_delete = client.delete(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-a"}
        )
        assert owner_delete.status_code == 204


def test_run_listing_is_filtered_per_scoped_principal(monkeypatch):
    client = make_visibility_client(monkeypatch)
    with client:
        run_a = submit_run(client, sub="user-a")["run_id"]
        run_b = submit_run(client, sub="user-b")["run_id"]
        wait_for_run_status_as(client, run_a, "completed", sub="user-a")
        wait_for_run_status_as(client, run_b, "completed", sub="user-b")

        listed_a = client.get("/v1/runs", headers={SUB_HEADER: "user-a"}).json()
        listed_anonymous = client.get("/v1/runs").json()

    ids_a = [item["run_id"] for item in listed_a["data"]]
    assert ids_a == [run_a]
    # Unscoped deployments see ownerless legacy rows only, never scoped data.
    assert listed_anonymous["data"] == []


def test_events_stream_is_visibility_gated(monkeypatch):
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="user-a")

        denied = client.get(
            f"/v1/runs/{run_id}/events", headers={SUB_HEADER: "user-b"}
        )

    assert denied.status_code == 404


def test_anonymous_runs_stay_invisible_to_scoped_principals(monkeypatch):
    """Pre-scoping records must not leak into a scoped principal's view."""
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub=None)["run_id"]
        wait_for_run_status(client, run_id, "completed")

        as_scoped = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-a"}
        )
        as_anonymous = client.get(f"/v1/runs/{run_id}")

    assert as_scoped.status_code == 404
    assert as_anonymous.status_code == 200


def wait_for_run_status_as(
    client: TestClient, run_id: str, status: str, *, sub: str
) -> dict[str, Any]:
    """Scoped variant of the contract helper (adds the sub header)."""
    import time

    deadline = time.time() + 2.0
    summary: dict[str, Any] = {}
    while time.time() < deadline:
        summary = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: sub}
        ).json()
        if summary.get("status") == status:
            return summary
        time.sleep(0.01)
    raise AssertionError(
        f"run {run_id} did not reach status {status!r}; last: {summary}"
    )


def test_visibility_denial_is_logged_for_operators(monkeypatch, caplog):
    """The client gets the indistinct 404; the operator sees the denial."""
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="user-a")

        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            denied = client.get(
                f"/v1/runs/{run_id}", headers={SUB_HEADER: "user-b"}
            )

    assert denied.status_code == 404
    authz_messages = [
        message for message in caplog.messages if "authz denied" in message
    ]
    assert any("actor_ref=usr_" in message for message in authz_messages)
    assert all(
        str(_user_id("user-b")) not in message for message in authz_messages
    )
    assert all(run_id not in message for message in authz_messages)


def test_sub_collision_across_tenants_is_not_visible(monkeypatch):
    """OIDC subs are only unique per issuer — same sub in another
    tenant must not see the run."""
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="alice")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="alice")

        same_tenant = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: "alice"}
        )
        other_tenant = client.get(
            f"/v1/runs/{run_id}",
            headers={SUB_HEADER: "alice", TENANT_HEADER: "tenant-x"},
        )

    assert same_tenant.status_code == 200
    assert other_tenant.status_code == 404


def test_scoped_visibility_combines_with_workspace_namespace(monkeypatch):
    """Visibility (authz) and the workspace UI namespace must intersect."""
    client = make_visibility_client(monkeypatch)
    with client:
        in_workspace = client.post(
            "/v1/runs",
            json={"question": "Testfrage?", "workspace_id": "ws-ui-0001"},
            headers={SUB_HEADER: "user-a"},
        ).json()["run_id"]
        no_workspace = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, in_workspace, "completed", sub="user-a")
        wait_for_run_status_as(client, no_workspace, "completed", sub="user-a")

        own_filtered = client.get(
            "/v1/runs",
            headers={SUB_HEADER: "user-a", WORKSPACE_HEADER: "ws-ui-0001"},
        ).json()
        foreign_with_correct_namespace = client.get(
            f"/v1/runs/{in_workspace}",
            headers={SUB_HEADER: "user-b", WORKSPACE_HEADER: "ws-ui-0001"},
        )
        missing = client.get(
            "/v1/runs/run_does_not_exist", headers={SUB_HEADER: "user-b"}
        )

    assert [item["run_id"] for item in own_filtered["data"]] == [in_workspace]
    # Knowing the right UI namespace must not weaken the hiding rule.
    assert foreign_with_correct_namespace.status_code == 404
    assert foreign_with_correct_namespace.json() == missing.json()


def test_owner_still_streams_events_under_scoped_auth(monkeypatch):
    """The positive half of the SSE gate: the creator gets the replay."""
    client = make_visibility_client(monkeypatch)
    with client:
        run_id = submit_run(client, sub="user-a")["run_id"]
        wait_for_run_status_as(client, run_id, "completed", sub="user-a")

        response = client.get(
            f"/v1/runs/{run_id}/events", headers={SUB_HEADER: "user-a"}
        )

    assert response.status_code == 200
    frames = parse_sse_frames(response.text)
    assert any('"inqtrix.run.completed"' in data for _name, data in frames)


def scoped_context(sub: str, *, tenant_id: str = "default") -> UserContext:
    return UserContext(
        principal=Principal(
            user_id=_user_id(sub),
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )
    )


def test_cancel_of_running_run_denies_before_mutating():
    """Deny-before-mutate: a foreign principal's cancel must not set the
    cancel event of an in-flight run (pins the check ordering)."""
    store = RunStore(
        max_concurrent=1,
        max_queue_size=4,
        completed_ttl_seconds=60,
        event_buffer_size=16,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_work(handle):
        started.set()
        release.wait(timeout=5)
        handle.complete({"answer": "done"})

    summary = store.submit(
        question="Testfrage?",
        stack_name="default",
        work=blocking_work,
        created_by_user_id=_user_id("user-a"),
        created_by_tenant_id="default",
    )
    run_id = summary["run_id"]
    assert started.wait(timeout=5)
    try:
        with pytest.raises(RunNotFound):
            store.cancel(run_id, visible_to=scoped_context("user-b"))
        record = store._records[run_id]
        assert not record.cancel_event.is_set()

        owner_summary = store.cancel(
            run_id, visible_to=scoped_context("user-a")
        )
        assert record.cancel_event.is_set()
        assert owner_summary["status"] == "running"
    finally:
        release.set()


def test_run_list_route_paginates_with_cursor(monkeypatch):
    """2.2: /v1/runs pages via ?limit/?cursor and exposes next_cursor."""
    client = make_visibility_client(monkeypatch)
    with client:
        ids = []
        for _ in range(5):
            ids.append(submit_run(client, sub="user-a")["run_id"])
        for rid in ids:
            wait_for_run_status_as(client, rid, "completed", sub="user-a")

        seen = []
        cursor = None
        for _ in range(10):
            url = "/v1/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(url, headers={SUB_HEADER: "user-a"}).json()
            assert body["object"] == "list"
            assert "next_cursor" in body
            seen.extend(item["run_id"] for item in body["data"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert set(seen) == set(ids)
        assert len(seen) == len(set(seen))
        assert seen == list(reversed(ids))  # newest first

        # A malformed cursor is a 400, never a silent first-page.
        bad = client.get(
            "/v1/runs?cursor=not-a-real-cursor",
            headers={SUB_HEADER: "user-a"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["type"] == "invalid_cursor"
