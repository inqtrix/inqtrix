"""Quota enforcement across the cost-incurring HTTP surfaces.

Three layers:

* unit — the router guard helpers no-op when quotas are unwired, the
  container builds the service only for an enabled oidc deployment, and
  the per-run token budget aborts via :func:`check_cancel_event`.
* HTTP admission — a caller already over a dimension is blocked with the
  429 quota envelope BEFORE any cost (runs, chat, editor, text,
  knowledge ingestion, file upload).
* HTTP accounting — the real spend is booked after the fact (run/chat
  LLM tokens from provider usage; editor/text/embedding estimates;
  stored bytes charged on upload and freed on delete against the owner).

The agent engine is monkeypatched (``run_web_graph``) so runs/chat
exercise the real ``execute_run_request`` / ``ChatService`` recording
path without network IO.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    AuthMode,
    AuthProvider,
    NoneAuthProvider,
    Principal,
    UserContext,
)
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.exceptions import AgentCancelled
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import ProviderContext
from inqtrix.quota.memory import MemoryQuotaStore
from inqtrix.quota.models import (
    QuotaDimension,
    QuotaExceeded,
    QuotaSubject,
    current_period_start,
    estimate_tokens,
    period_end,
)
from inqtrix.services.quota_service import QuotaService
from inqtrix.server.container import build_container
from inqtrix.server.routers import (
    capabilities as capabilities_router,
    chat as chat_router,
    editor as editor_router,
    files as files_router,
    knowledge as knowledge_router,
    quota as quota_router,
    quota_admission,
    quota_record,
    quota_record_for_subject,
    runs as runs_router,
    text as text_router,
)
from inqtrix.settings import (
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)
from inqtrix.state import check_cancel_event, initial_state

from tests.contract._app import StubSearch, minimal_agent_result
from tests.test_knowledge_engine import StubEmbeddings

SUB_HEADER = "X-Test-Sub"
USER_A = "user-a"
USER_B = "user-b"


def _user_id(label: str) -> uuid.UUID:
    """Map test labels to stable canonical user UUIDs."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"inqtrix-test:{label}")


#: Real usage the stub reports per ``complete`` call (prompt + completion).
STUB_PROMPT_TOKENS = 9
STUB_COMPLETION_TOKENS = 4
STUB_CALL_TOKENS = STUB_PROMPT_TOKENS + STUB_COMPLETION_TOKENS


class _Stub429LLM:
    """LLM stub returning JSON valid for text + editor-suggest parsing.

    Reports real usage through the ``state`` accumulator (as the
    production providers do via ``track_tokens``) so the editor/text
    recording assertions exercise the real-usage path, not an estimate.
    """

    def complete(self, prompt=None, *, state=None, **kwargs) -> str:
        if state is not None:
            state["total_prompt_tokens"] = (
                state.get("total_prompt_tokens", 0) + STUB_PROMPT_TOKENS
            )
            state["total_completion_tokens"] = (
                state.get("total_completion_tokens", 0) + STUB_COMPLETION_TOKENS
            )
        return (
            '{"improved_text": "Verbesserter Text.", '
            '"rewritten_text": "Verbesserter Text.", "changes": []}'
        )

    def supports_structured_output(self, *, model=None) -> bool:
        return False

    def is_available(self) -> bool:
        return True


class _InstructStubLLM(_Stub429LLM):
    """Stub returning a valid editor-INSTRUCT response (one append edit)."""

    def complete(self, prompt=None, *, state=None, **kwargs) -> str:
        super().complete(prompt, state=state, **kwargs)  # accumulate usage
        return (
            '{"assistant_message": "Erledigt.", "edits": [{"find": "", '
            '"quote_before": "", "quote_after": "", "position": "append", '
            '"text": "Neuer Absatz.", "note": "append"}], "warnings": []}'
        )


class OidcProvider(AuthProvider):
    """Test-only oidc-mode provider: the sub header IS the identity.

    A lightweight double for the cost-enforcement tests, where the metered
    subject is the header, not a real session. The instance-admin quota
    surface is covered separately in ``test_quota_admin_routes.py`` over the
    real cookie-session provider.
    """

    @property
    def mode(self) -> AuthMode:
        return "oidc"

    @property
    def users(self):
        class _Users:
            async def find_by_user_id(self, *, tenant_id, user_id):
                return SimpleNamespace(disabled_at=None)

            async def has_user_id(self, *, tenant_id, user_id):
                return True

        return _Users()

    def resolve_principal(self, request: Request) -> Principal:
        sub = request.headers.get(SUB_HEADER, "")
        if not sub:
            return ANONYMOUS_PRINCIPAL
        return Principal(
            user_id=_user_id(sub),
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )

    def build_principal_dependency(self):
        async def _dependency(request: Request) -> Principal:
            return self.resolve_principal(request)

        return _dependency


def make_quota_client(
    tmp_path: Path,
    quota: QuotaSettings,
    *,
    llm=None,
) -> tuple[TestClient, object]:
    """Build an oidc app with quotas on and every cost router mounted.

    Wires a memory identity store as permissions + workspace_admin so the
    scoped routes resolve memberships and record audit facts. The quota
    admin surface is covered separately in ``test_quota_admin_routes.py``.
    """
    identity = MemoryIdentityStore()
    container = build_container(
        providers=ProviderContext(
            llm=llm or _Stub429LLM(), search=StubSearch()
        ),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(
                backend="memory",
                database_url="",
                object_store_backend="local",
                object_store_path=str(tmp_path / "blobs"),
                max_file_bytes=10_000_000,
            ),
            quota=quota,
        ),
        semaphore_factory=lambda: asyncio.Semaphore(4),
        auth_provider=OidcProvider(),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=4,
        ),
    )
    assert container.quota_service is not None
    app = FastAPI()
    app.include_router(runs_router.build_router(container))
    app.include_router(chat_router.build_router(container))
    app.include_router(editor_router.build_router(container))
    app.include_router(text_router.build_router(container))
    app.include_router(knowledge_router.build_router(container))
    app.include_router(files_router.build_router(container))
    app.include_router(quota_router.build_router(container))
    app.include_router(capabilities_router.build_router(container))
    return TestClient(app), container


def used(container, sub: str, dimension: QuotaDimension) -> int:
    """Read one subject's booked usage for *dimension* (memory store)."""
    rows = asyncio.run(
        container.quota_service.usage_for(
            QuotaSubject("default", _user_id(sub))
        )
    )
    return next(r.used for r in rows if r.dimension == dimension)


# --------------------------------------------------------------------------- #
# Unit: guard helpers, container gating, per-run token budget
# --------------------------------------------------------------------------- #


def test_guards_noop_without_service():
    """Every guard tolerates an unwired (``None``) service silently."""

    async def _run():
        assert (
            await quota_admission(None, ANONYMOUS_PRINCIPAL, QuotaDimension.RUNS)
            is None
        )
        # No raise / no effect:
        await quota_record(None, ANONYMOUS_PRINCIPAL, QuotaDimension.RUNS, 5)
        await quota_record_for_subject(
            None,
            QuotaSubject("t", _user_id("s")),
            QuotaDimension.STORED_BYTES,
            5,
        )

    asyncio.run(_run())


def test_container_builds_quota_only_for_enabled_oidc():
    base = dict(
        providers=ProviderContext(llm=_Stub429LLM(), search=StubSearch()),
        strategies=None,
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    # none-mode + enabled: never metered (no service constructed).
    none_enabled = build_container(
        settings=Settings(
            storage=StorageSettings(backend="memory", database_url=""),
            quota=QuotaSettings(enabled=True),
        ),
        auth_provider=NoneAuthProvider(),
        **base,
    )
    assert none_enabled.quota_service is None
    # oidc + disabled: off.
    oidc_off = build_container(
        settings=Settings(
            storage=StorageSettings(backend="memory", database_url=""),
            quota=QuotaSettings(enabled=False),
        ),
        auth_provider=OidcProvider(),
        **base,
    )
    assert oidc_off.quota_service is None
    # oidc + enabled: wired.
    oidc_on = build_container(
        settings=Settings(
            storage=StorageSettings(backend="memory", database_url=""),
            quota=QuotaSettings(enabled=True),
        ),
        auth_provider=OidcProvider(),
        **base,
    )
    assert oidc_on.quota_service is not None


def test_initial_state_seeds_token_budget():
    seeded = initial_state("Frage", token_budget=500)
    assert seeded["_token_budget"] == 500
    off = initial_state("Frage")
    assert "_token_budget" not in off


def test_check_cancel_event_token_budget():
    state = initial_state("Frage", token_budget=100)
    # Under budget: no-op.
    state["total_prompt_tokens"] = 40
    state["total_completion_tokens"] = 40
    check_cancel_event(state)
    # At/over budget: graceful abort.
    state["total_completion_tokens"] = 60
    with pytest.raises(AgentCancelled):
        check_cancel_event(state)


def test_check_cancel_event_no_budget_never_caps():
    state = initial_state("Frage")  # token_budget=0 -> key absent
    state["total_prompt_tokens"] = 10_000_000
    state["total_completion_tokens"] = 10_000_000
    check_cancel_event(state)  # must not raise


# --------------------------------------------------------------------------- #
# HTTP: runs (RUNS + LLM_TOKENS) and the per-run budget plumbing
# --------------------------------------------------------------------------- #


def _complete_run(client: TestClient, sub: str = USER_A):
    import time

    resp = client.post(
        "/v1/runs", json={"question": "Testfrage?"}, headers={SUB_HEADER: sub}
    )
    if resp.status_code != 202:
        return resp
    run_id = resp.json()["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        summary = client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: sub}
        ).json()
        if summary.get("status") == "completed":
            return resp
        time.sleep(0.01)
    raise AssertionError("run did not complete")


def test_runs_admission_and_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: minimal_agent_result(),
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, runs_default=1)
    )
    with client:
        first = _complete_run(client)
        assert first.status_code == 202
        # One run booked; the run's LLM tokens (11+7) booked at completion.
        assert used(container, USER_A, QuotaDimension.RUNS) == 1
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18

        # Second run crosses the 1-run allowance -> 429 quota envelope.
        blocked = client.post(
            "/v1/runs",
            json={"question": "Noch eine?"},
            headers={SUB_HEADER: USER_A},
        )
        assert blocked.status_code == 429
        error = blocked.json()["error"]
        assert error["type"] == "quota_exceeded"
        assert error["dimension"] == "runs"
        # The actionable envelope fields the UI renders, not just the type.
        assert error["limit"] == 1
        assert error["used"] == 1
        assert error["reset_at"] > 0  # flow dimension: next month boundary
        # A different user is unaffected.
        assert used(container, USER_B, QuotaDimension.RUNS) == 0


def test_runs_passes_token_budget_to_graph(tmp_path, monkeypatch):
    captured: list[dict] = []

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return minimal_agent_result()

    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph", _capture
    )
    client, _ = make_quota_client(
        tmp_path,
        QuotaSettings(enabled=True, max_tokens_per_run=500),
    )
    with client:
        assert _complete_run(client).status_code == 202
    assert captured and captured[0].get("token_budget") == 500


def test_runs_no_budget_omits_token_budget_kwarg(tmp_path, monkeypatch):
    captured: list[dict] = []

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return minimal_agent_result()

    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph", _capture
    )
    client, _ = make_quota_client(tmp_path, QuotaSettings(enabled=True))
    with client:
        assert _complete_run(client).status_code == 202
    assert captured and "token_budget" not in captured[0]


# --------------------------------------------------------------------------- #
# HTTP: chat (LLM_TOKENS, non-streaming)
# --------------------------------------------------------------------------- #


def test_chat_admission_and_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: minimal_agent_result(),
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=10)
    )
    with client:
        first = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo?"}]},
            headers={SUB_HEADER: USER_A},
        )
        assert first.status_code == 200
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18

        # 18 already booked > 10 allowance -> next request blocked.
        blocked = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Wieder?"}]},
            headers={SUB_HEADER: USER_A},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["dimension"] == "llm_tokens"


def test_chat_terminal_failure_records_consumed_tokens(tmp_path, monkeypatch):
    rejected = minimal_agent_result(answer="rejected model completion")
    rejected["result_state"]["_terminal_failure"] = {
        "type": "knowledge_grounding_quote_unverified",
        "message": "Die Wissensantwort konnte nicht sicher belegt werden.",
    }
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: rejected,
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )

    with client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo?"}]},
            headers={SUB_HEADER: USER_A},
        )

    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "message": "Die Wissensantwort konnte nicht sicher belegt werden.",
            "type": "knowledge_grounding_quote_unverified",
        }
    }
    assert "rejected model completion" not in response.text
    assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18


# --------------------------------------------------------------------------- #
# HTTP: editor + text (LLM_TOKENS estimate)
# --------------------------------------------------------------------------- #


def test_text_records_real_usage(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )
    with client:
        resp = client.post(
            "/v1/text/improvements",
            json={"context": "chat_input", "text": "Bitte verbessere dies."},
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 200
        # The provider's real usage (via the state accumulator), not a
        # char estimate.
        assert (
            used(container, USER_A, QuotaDimension.LLM_TOKENS)
            == STUB_CALL_TOKENS
        )


def test_editor_suggest_records_real_usage(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )
    with client:
        resp = client.post(
            "/v1/editor/suggest",
            json={
                "block_text": "Ein kurzer Satz.",
                "instruction": "Mach es praeziser.",
            },
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 200, resp.text
        assert (
            used(container, USER_A, QuotaDimension.LLM_TOKENS)
            == STUB_CALL_TOKENS
        )


def test_editor_admission_blocks_over_quota(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=50)
    )
    with client:
        # Pre-load the subject over the LLM-token allowance.
        asyncio.run(
            container.quota_service.record(
                Principal(
                    user_id=_user_id(USER_A),
                    kind="oidc_session",
                    tenant_id="default",
                    role="member",
                ),
                QuotaDimension.LLM_TOKENS,
                100,
            )
        )
        resp = client.post(
            "/v1/editor/suggest",
            json={
                "block_text": "Ein kurzer Satz.",
                "instruction": "Mach es praeziser.",
            },
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["dimension"] == "llm_tokens"


# --------------------------------------------------------------------------- #
# HTTP: knowledge ingestion (EMBEDDING_TOKENS)
# --------------------------------------------------------------------------- #


def _create_collection(client: TestClient, sub: str = USER_A) -> str:
    created = client.post(
        "/v1/knowledge/collections",
        json={"name": "Sammlung"},
        headers={SUB_HEADER: sub},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_knowledge_ingestion_records_and_blocks(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, embedding_tokens_default=1)
    )
    text = "Dies ist ein Dokument mit etwas Inhalt zum Einbetten."
    with client:
        collection_id = _create_collection(client)
        ingested = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"title": "Doc", "text": text},
            headers={SUB_HEADER: USER_A},
        )
        assert ingested.status_code == 201, ingested.text
        booked = used(container, USER_A, QuotaDimension.EMBEDDING_TOKENS)
        assert booked == estimate_tokens(text)

        # Now over the 1-token allowance -> next ingestion blocked.
        blocked = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"title": "Doc2", "text": "Noch ein Dokument."},
            headers={SUB_HEADER: USER_A},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["dimension"] == "embedding_tokens"


# --------------------------------------------------------------------------- #
# HTTP: files (STORED_BYTES — charged on upload, freed on delete)
# --------------------------------------------------------------------------- #


def _upload(client: TestClient, content: bytes, sub: str = USER_A):
    return client.post(
        "/v1/files",
        files={"file": ("doc.bin", content, "application/octet-stream")},
        headers={SUB_HEADER: sub},
    )


def test_files_charge_on_upload_and_free_on_delete(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, stored_bytes_default=1000)
    )
    payload = b"x" * 200
    with client:
        up = _upload(client, payload)
        assert up.status_code == 201
        file_id = up.json()["id"]
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 200

        # Deletion frees the owner's stock back to zero.
        deleted = client.delete(
            f"/v1/files/{file_id}", headers={SUB_HEADER: USER_A}
        )
        assert deleted.status_code == 204
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 0


def test_files_admission_blocks_when_full(tmp_path):
    """Block-next: an upload that crosses the cap still lands, the NEXT
    is denied. Admission cannot use the multipart Content-Length as the
    file size, so it is a pure already-over guard; ``max_file_bytes``
    bounds a single upload's overshoot."""
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, stored_bytes_default=100)
    )
    with client:
        # First upload (80) fits and lands.
        assert _upload(client, b"y" * 80).status_code == 201
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 80
        # Second (80) is admitted (still under 100), pushing the stock
        # to 160 — the one allowed overshoot.
        assert _upload(client, b"z" * 80).status_code == 201
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 160
        # Now over the cap -> the next upload is denied before spooling.
        blocked = _upload(client, b"w" * 10)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["dimension"] == "stored_bytes"
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 160


def test_bound_upload_replay_repairs_at_full_quota_without_second_charge(
    tmp_path,
):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, stored_bytes_default=10)
    )
    user_id = _user_id(USER_A)
    principal = Principal(
        user_id=user_id,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    visible_to = UserContext(principal=principal)
    asyncio.run(
        container.asset_records_service.save_section(
            id="quota-section",
            kind="custom",
            title="Quota",
            created_at=1.0,
            updated_at=1.0,
            caller_user_id=user_id,
            workspace_id=None,
            visible_to=visible_to,
        )
    )
    data = {
        "asset_id": "quota-asset",
        "section_id": "quota-section",
        "title": "Quota file",
        "label": "quota",
        "origin": "library",
        "created_at": "1.0",
        "updated_at": "1.0",
    }
    payload = b"q" * 20
    with client:
        first = client.post(
            "/v1/files",
            files={"file": ("quota.bin", payload, "application/octet-stream")},
            data=data,
            headers={SUB_HEADER: USER_A},
        )
        assert first.status_code == 202, first.text
        first_body = first.json()
        operation_id = first_body["upload_operation"]["operation_id"]
        file_id = first_body["asset"]["server_file_id"]
        assert first_body["upload_operation"]["status"] == "queued"
        assert first_body["upload_operation"]["stage"] == "parsing"
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 0

        claimed = (
            container.upload_operation_service.operations.claim_for_execution(
                operation_id, "default", allow_takeover=False
            )
        )
        assert claimed is not None
        container.upload_operation_service.execute_claimed(claimed)

        terminal = client.get(
            f"/v1/uploads/{operation_id}", headers={SUB_HEADER: USER_A}
        )
        assert terminal.status_code == 200, terminal.text
        assert terminal.json()["status"] == "ready"
        assert terminal.json()["stage"] == "ready"
        assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 20

        replay = client.post(
            "/v1/files",
            files={"file": ("quota.bin", payload, "application/octet-stream")},
            data={**data, "created_at": "9.0", "updated_at": "9.0"},
            headers={SUB_HEADER: USER_A},
        )
        fresh = client.post(
            "/v1/files",
            files={"file": ("fresh.bin", b"x", "application/octet-stream")},
            data={
                **data,
                "asset_id": "quota-asset-fresh",
                "title": "Fresh",
            },
            headers={SUB_HEADER: USER_A},
        )

    assert replay.status_code == 200
    assert replay.json()["id"] == file_id
    assert replay.json()["upload_operation"]["operation_id"] == operation_id
    assert replay.json()["upload_operation"]["status"] == "ready"
    assert used(container, USER_A, QuotaDimension.STORED_BYTES) == 20
    assert fresh.status_code == 429


# --------------------------------------------------------------------------- #
# HTTP: editor instruct (own admission + recording wiring)
# --------------------------------------------------------------------------- #


def _preload_over(container, sub: str, dimension: QuotaDimension, amount: int):
    asyncio.run(
        container.quota_service.record(
            Principal(
                user_id=_user_id(sub),
                kind="oidc_session",
                tenant_id="default",
                role="member",
            ),
            dimension,
            amount,
        )
    )


def test_editor_instruct_admission_blocks_over_quota(tmp_path):
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=50)
    )
    with client:
        _preload_over(container, USER_A, QuotaDimension.LLM_TOKENS, 100)
        resp = client.post(
            "/v1/editor/instruct",
            json={
                "instruction": "Fuege einen Absatz hinzu.",
                "document_markdown": "",
            },
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["dimension"] == "llm_tokens"


def test_editor_instruct_records_real_usage(tmp_path):
    client, container = make_quota_client(
        tmp_path,
        QuotaSettings(enabled=True, llm_tokens_default=100_000),
        llm=_InstructStubLLM(),
    )
    with client:
        resp = client.post(
            "/v1/editor/instruct",
            json={
                "instruction": "Fuege einen Absatz hinzu.",
                "document_markdown": "",
            },
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 200, resp.text
        assert (
            used(container, USER_A, QuotaDimension.LLM_TOKENS)
            == STUB_CALL_TOKENS
        )


# --------------------------------------------------------------------------- #
# HTTP: streamed chat recording (the separate stream_response site)
# --------------------------------------------------------------------------- #


def _drain(response) -> None:
    for _ in response.iter_lines():
        pass


def test_chat_stream_records_tokens(tmp_path, monkeypatch):
    # Streaming now dispatches through the registry and reaches the graph at the
    # SAME seam as the non-stream/run path (inqtrix.research.web_research.run_web_graph).
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: minimal_agent_result(),
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )
    with client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "stream": True,
                "messages": [{"role": "user", "content": "Hallo?"}],
            },
            headers={SUB_HEADER: USER_A},
        ) as response:
            assert response.status_code == 200
            _drain(response)
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18


def test_chat_stream_books_abandoned_run(tmp_path, monkeypatch):
    """A cancelled streamed run still books what it consumed (the spend
    is recorded before the cancel short-circuit)."""
    cancelled = minimal_agent_result()
    cancelled["result_state"]["cancelled"] = True

    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: cancelled,
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )
    with client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "stream": True,
                "messages": [{"role": "user", "content": "Hallo?"}],
            },
            headers={SUB_HEADER: USER_A},
        ) as response:
            assert response.status_code == 200
            _drain(response)
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18


# --------------------------------------------------------------------------- #
# HTTP: a failed chat request must NOT be metered
# --------------------------------------------------------------------------- #


def test_chat_error_books_nothing(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph", _boom
    )
    client, container = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100_000)
    )
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo?"}]},
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 502
        # The error envelope carries no usage -> nothing booked.
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 0


# --------------------------------------------------------------------------- #
# Per-run token cap: a cancelled run still books its partial spend
# --------------------------------------------------------------------------- #


def test_run_cancelled_still_books_spend(tmp_path, monkeypatch):
    """When a run ends cancelled (incl. the per-run budget abort), its
    consumed tokens are recorded before the early return — that spend is
    what blocks the NEXT submission."""
    cancelled = minimal_agent_result()
    cancelled["result_state"]["cancelled"] = True
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: cancelled,
    )
    client, container = make_quota_client(
        tmp_path,
        QuotaSettings(
            enabled=True, runs_default=0, max_tokens_per_run=5
        ),
    )
    import time

    with client:
        resp = client.post(
            "/v1/runs",
            json={"question": "Testfrage?"},
            headers={SUB_HEADER: USER_A},
        )
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        deadline = time.time() + 2.0
        status = ""
        while time.time() < deadline:
            status = client.get(
                f"/v1/runs/{run_id}", headers={SUB_HEADER: USER_A}
            ).json()["status"]
            if status in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.01)
        assert status == "cancelled"
        # Partial spend booked despite the cancel early-return.
        assert used(container, USER_A, QuotaDimension.LLM_TOKENS) == 18


# --------------------------------------------------------------------------- #
# Service-level: month rollover re-admits a blocked subject
# --------------------------------------------------------------------------- #


def test_month_rollover_readmits_blocked_subject():
    """A subject blocked in month N is admitted again in month N+1, and
    the 429 reset_at points at the active window's end."""
    clock = {"now": 1_749_816_000.0}  # mid-June 2025, UTC
    service = QuotaService(
        store=MemoryQuotaStore(),
        settings=QuotaSettings(enabled=True, runs_default=1),
        clock=lambda: clock["now"],
    )
    principal = Principal(
        user_id=_user_id(USER_A),
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )

    async def scenario():
        await service.record(principal, QuotaDimension.RUNS, 1)
        # Over the 1-run allowance this month -> blocked, reset_at = the
        # end of the current window.
        with pytest.raises(QuotaExceeded) as exc_info:
            await service.check(principal, QuotaDimension.RUNS)
        assert exc_info.value.reset_at == period_end(
            current_period_start(clock["now"])
        )
        # Next month: a fresh window reads 0 -> admitted again.
        clock["now"] += 32 * 24 * 3600
        await service.check(principal, QuotaDimension.RUNS)

    asyncio.run(scenario())


class _NamedLane:
    """Serve every store call from one shared memory store, naming the lane."""

    def __init__(self, name: str, backing: MemoryQuotaStore, calls: list) -> None:
        self._name = name
        self._backing = backing
        self._calls = calls

    def __getattr__(self, attribute: str):
        target = getattr(self._backing, attribute)

        async def call(*args, **kwargs):
            self._calls.append((self._name, attribute))
            return await target(*args, **kwargs)

        return call


def test_http_reads_use_the_request_lane_and_run_threads_keep_their_own():
    """Admission and the meter read once, on the request-path store.

    The meter asks every 30 seconds per visible tab and every admission
    asks before a run, so these reads belong on the pooled engine of the
    request loop, as one read each. Recording from a run thread happens on
    a throwaway event loop and must stay on the loop-agnostic store.
    """
    backing = MemoryQuotaStore()
    calls: list[tuple[str, str]] = []
    service = QuotaService(
        store=_NamedLane("run", backing, calls),
        request_store=_NamedLane("request", backing, calls),
        settings=QuotaSettings(enabled=True, runs_default=5),
        clock=lambda: 1_749_816_000.0,
    )
    principal = Principal(
        user_id=_user_id(USER_A),
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    subject = service.subject_for(principal)

    service.record_blocking(subject, QuotaDimension.RUNS, 1)
    assert calls == [("run", "add_usage")]

    calls.clear()

    async def http_reads():
        await service.check(principal, QuotaDimension.RUNS)
        return await service.usage_for(subject)

    rows = asyncio.run(http_reads())
    assert calls == [
        ("request", "read_usage_with_limits"),
        ("request", "read_usage_with_limits"),
    ]
    runs = next(row for row in rows if row.dimension is QuotaDimension.RUNS)
    assert (runs.used, runs.limit) == (1, 5)


# --------------------------------------------------------------------------- #
# self meter endpoint + owner admin surface
# --------------------------------------------------------------------------- #


def test_self_usage_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *a, **k: minimal_agent_result(),
    )
    client, _ = make_quota_client(
        tmp_path, QuotaSettings(enabled=True, llm_tokens_default=100)
    )
    with client:
        # Consume some tokens via a chat call, then read the meter.
        client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo?"}]},
            headers={SUB_HEADER: USER_A},
        )
        resp = client.get("/v1/quota/usage", headers={SUB_HEADER: USER_A})
        assert resp.status_code == 200
        rows = {r["dimension"]: r for r in resp.json()["data"]}
        assert rows["llm_tokens"]["used"] == 18
        assert rows["llm_tokens"]["limit"] == 100
        assert rows["llm_tokens"]["remaining"] == 82
        assert rows["llm_tokens"]["reset_at"] > 0


def test_capabilities_reports_quota(tmp_path):
    client, _ = make_quota_client(tmp_path, QuotaSettings(enabled=True))
    with client:
        features = client.get("/v1/capabilities").json()["features"]
        assert features["quota"] is True


def test_self_usage_empty_for_unscoped(tmp_path):
    client, _ = make_quota_client(tmp_path, QuotaSettings(enabled=True))
    with client:
        # No SUB_HEADER -> anonymous principal -> no metered subject.
        resp = client.get("/v1/quota/usage")
        assert resp.status_code == 200
        assert resp.json() == {"object": "list", "data": []}
