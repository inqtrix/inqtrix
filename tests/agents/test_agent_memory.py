"""Enterprise memory service and HTTP-surface tests."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.agents.memory_candidates_memory import (
    MemoryAgentFeedbackStore,
    MemoryAgentMemoryCandidateStore,
)
from inqtrix.agents.memory_ports import (
    AgentMemoryNotFound,
    AgentMemoryRecord,
    AgentMemoryUnavailable,
)
from inqtrix.agents.algorithm import _load_memory_briefing
from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    STATIC_PRINCIPAL,
    Principal,
)
from inqtrix.server.routers.agent_memory import build_router
from inqtrix.services.agent_memory_service import AgentMemoryService

OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OWNER = Principal(
    user_id=OWNER_ID,
    kind="oidc_session",
    tenant_id="default",
    role="member",
)
OTHER = Principal(
    user_id=OTHER_ID,
    kind="oidc_session",
    tenant_id="default",
    role="member",
)


class FakeMemoryProvider:
    """Namespace-scoped memory provider used by the service tests."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, AgentMemoryRecord]] = {}
        self.feedback_calls: list[dict[str, str]] = []
        self.recall_queries: list[dict[str, str]] = []
        self.fail_recall = False

    async def list_memories(
        self, *, namespace: str, scope: str | None, limit: int
    ) -> list[AgentMemoryRecord]:
        rows = list(self.rows.get(namespace, {}).values())
        if scope is not None:
            rows = [row for row in rows if row.scope == scope]
        return rows[:limit]

    async def recall(
        self, *, namespace: str, query: str, limit: int
    ) -> list[AgentMemoryRecord]:
        if self.fail_recall:
            raise AgentMemoryUnavailable("provider down")
        self.recall_queries.append({"namespace": namespace, "query": query})
        return list(self.rows.get(namespace, {}).values())[:limit]

    async def retain(
        self,
        *,
        namespace: str,
        content: str,
        scope: str,
        category: str,
        confidence: float,
        source_run_id: str,
    ) -> AgentMemoryRecord:
        memory_id = f"mem_{sum(len(rows) for rows in self.rows.values()) + 1}"
        row = AgentMemoryRecord(
            memory_id=memory_id,
            scope=scope,
            category=category,
            content=content,
            confidence=confidence,
            source_run_id=source_run_id,
        )
        self.rows.setdefault(namespace, {})[memory_id] = row
        return row

    async def update(
        self,
        *,
        namespace: str,
        memory_id: str,
        content: str,
        scope: str,
        category: str,
    ) -> AgentMemoryRecord:
        if memory_id not in self.rows.get(namespace, {}):
            raise AgentMemoryNotFound(memory_id)
        row = AgentMemoryRecord(
            memory_id=memory_id,
            scope=scope,
            category=category,
            content=content,
        )
        self.rows[namespace][memory_id] = row
        return row

    async def delete(self, *, namespace: str, memory_id: str) -> None:
        if memory_id not in self.rows.get(namespace, {}):
            raise AgentMemoryNotFound(memory_id)
        del self.rows[namespace][memory_id]

    async def clear(self, *, namespace: str, scope: str | None) -> int:
        rows = self.rows.setdefault(namespace, {})
        ids = [
            memory_id
            for memory_id, row in rows.items()
            if scope is None or row.scope == scope
        ]
        for memory_id in ids:
            del rows[memory_id]
        return len(ids)

    async def feedback(
        self,
        *,
        namespace: str,
        memory_id: str,
        feedback: str,
        reason: str,
    ) -> None:
        if memory_id not in self.rows.get(namespace, {}):
            raise AgentMemoryNotFound(memory_id)
        self.feedback_calls.append(
            {
                "namespace": namespace,
                "memory_id": memory_id,
                "feedback": feedback,
                "reason": reason,
            }
        )


def _service(provider: FakeMemoryProvider | None = None) -> AgentMemoryService:
    return AgentMemoryService(
        candidate_store=MemoryAgentMemoryCandidateStore(),
        feedback_store=MemoryAgentFeedbackStore(),
        provider=provider or FakeMemoryProvider(),
        provider_name="mem0",
        mode="candidate_only",
    )


def test_anonymous_and_static_principals_cannot_use_long_term_memory() -> None:
    service = _service()

    with pytest.raises(AgentMemoryUnavailable):
        asyncio.run(service.list_memories(principal=ANONYMOUS_PRINCIPAL))
    with pytest.raises(AgentMemoryUnavailable):
        asyncio.run(service.list_memories(principal=STATIC_PRINCIPAL))


def test_agent_intake_gates_recall_on_status_and_emits_degradation() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    events: list[tuple[str, dict[str, Any]]] = []

    deps = SimpleNamespace(
        memory=service,
        agent_memory_opt_in=True,
        context=SimpleNamespace(principal=STATIC_PRINCIPAL),
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )
    state: dict[str, Any] = {"question": "Use my project memory."}

    _load_memory_briefing(deps, state)  # type: ignore[arg-type]

    assert state["memory_status"] == "disabled"
    assert provider.recall_queries == []
    assert events == []

    provider.fail_recall = True
    deps.context.principal = OWNER
    state = {"question": "Use my project memory."}

    _load_memory_briefing(deps, state)  # type: ignore[arg-type]

    assert state["memory_status"] == "unavailable"
    assert events[-1][0] == "inqtrix.agent.activity"
    assert events[-1][1]["kind"] == "memory_unavailable"


def test_agent_intake_injects_used_memory_briefing() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Prefer concise vendor comparison tables.",
            scope="user",
            category="preference",
            confidence=0.9,
            source_run_id="run_seed",
        )
    )
    events: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(
        memory=service,
        agent_memory_opt_in=True,
        context=SimpleNamespace(principal=OWNER),
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )
    state: dict[str, Any] = {"question": "How should I present vendors?"}

    _load_memory_briefing(deps, state)  # type: ignore[arg-type]

    assert state["memory_status"] == "used"
    assert (
        "Prefer concise vendor comparison tables." in state["memory_briefing"]
    )
    assert events[-1][0] == "inqtrix.agent.activity"
    assert events[-1][1]["kind"] == "memory"
    assert events[-1][1]["status"] == "used"
    assert (
        provider.recall_queries[-1]["namespace"]
        == service._namespace_for(OWNER)  # type: ignore[attr-defined]
    )


def test_agent_memory_read_and_write_require_user_opt_in() -> None:
    # An eligible, infra-available principal who did NOT opt in gets no recall
    # and no candidate staging — the privacy default is OFF (both gates).
    from inqtrix.agents.algorithm import _stage_memory_candidates

    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Prefer concise vendor comparison tables.",
            scope="user",
            category="preference",
            confidence=0.9,
            source_run_id="run_seed",
        )
    )
    deps = SimpleNamespace(
        memory=service,
        agent_memory_opt_in=False,
        context=SimpleNamespace(principal=OWNER, run_id="run_optout"),
        emit=lambda event_type, payload: None,
        resolved=lambda node: (None, None),
        llm=None,
        timeout=5.0,
    )

    read_state: dict[str, Any] = {"question": "How should I present vendors?"}
    _load_memory_briefing(deps, read_state)  # type: ignore[arg-type]
    assert read_state["memory_status"] == "disabled"
    assert provider.recall_queries == []

    write_state: dict[str, Any] = {
        "question": "Plan the migration.",
        "memo_markdown": "# Memo\n\nErgebnis [W1].",
        "usage": {},
    }
    _stage_memory_candidates(deps, write_state)  # type: ignore[arg-type]
    assert "memory_candidates" not in write_state
    assert (
        asyncio.run(service.list_candidates(principal=OWNER, status="pending"))
        == []
    )


def test_stage_candidates_is_idempotent_per_source_run() -> None:
    """A redelivered terminal segment must not DUPLICATE a run's memory
    candidates — both engines stage from the terminal write, which can
    re-run after a crash. Second staging returns the SAME candidates."""
    service = _service()
    candidates = [
        {
            "scope": "user",
            "category": "project_fact",
            "content": "Bevorzugt Primaerquellen.",
            "reason": "wiederholte Bitte",
            "confidence": 0.9,
        }
    ]
    first = asyncio.run(
        service.stage_candidates(
            principal=OWNER, candidates=candidates, source_run_id="run_dup"
        )
    )
    assert len(first) == 1
    second = asyncio.run(
        service.stage_candidates(
            principal=OWNER, candidates=candidates, source_run_id="run_dup"
        )
    )
    assert [c.candidate_id for c in second] == [
        c.candidate_id for c in first
    ]
    pending = asyncio.run(
        service.list_candidates(principal=OWNER, status="pending")
    )
    assert len(pending) == 1


def test_opt_in_enabled_reads_account_preference_default_off() -> None:
    from inqtrix.project.account_preferences_memory import (
        MemoryAccountPreferencesStore,
    )

    store = MemoryAccountPreferencesStore()
    service = _service()
    service._account_preferences = store  # type: ignore[attr-defined]

    # No preferences row yet -> OFF (privacy default); anonymous -> OFF.
    assert asyncio.run(service.opt_in_enabled(OWNER)) is False
    assert asyncio.run(service.opt_in_enabled(ANONYMOUS_PRINCIPAL)) is False

    asyncio.run(
        store.upsert_preferences(
            user_id=OWNER_ID,
            contrast_mode="standard",
            locale="en",
            theme="system",
            theme_preset="standard",
            user_bubble_tone="gray",
            updated_at=1.0,
            enable_agent_memory=True,
        )
    )
    assert asyncio.run(service.opt_in_enabled(OWNER)) is True


def test_shared_run_recipient_cannot_read_owner_memory() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="OWNER private project context.",
            scope="user",
            category="project_fact",
            confidence=0.9,
            source_run_id="run_owner",
        )
    )
    # A recipient of OWNER's SHARED run executes under THEIR OWN principal;
    # the briefing must resolve to the recipient's namespace, never OWNER's.
    deps = SimpleNamespace(
        memory=service,
        agent_memory_opt_in=True,
        context=SimpleNamespace(principal=OTHER),
        emit=lambda event_type, payload: None,
    )
    state: dict[str, Any] = {"question": "Summarize the shared run."}

    _load_memory_briefing(deps, state)  # type: ignore[arg-type]

    assert "OWNER private project context." not in state.get(
        "memory_briefing", ""
    )
    assert state["memory_status"] == "empty"
    assert (
        provider.recall_queries[-1]["namespace"]
        == service._namespace_for(OTHER)  # type: ignore[attr-defined]
    )
    assert service._namespace_for(OTHER) != service._namespace_for(  # type: ignore[attr-defined]
        OWNER
    )


def test_finalize_stages_candidates_without_auto_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.agents import memory_reflection
    from inqtrix.agents.algorithm import _stage_memory_candidates
    from inqtrix.agents.phase_models import (
        MemoryCandidateModel,
        MemoryReflection,
    )

    provider = FakeMemoryProvider()
    service = _service(provider)
    reflection = MemoryReflection(
        candidates=[
            MemoryCandidateModel(
                scope="user",
                category="preference",
                content="Prefer concise implementation plans.",
                reason="The user corrected verbosity in this run.",
                confidence=0.8,
            )
        ]
    )
    monkeypatch.setattr(
        memory_reflection,
        "run_memory_reflection",
        lambda *args, **kwargs: SimpleNamespace(value=reflection, usage={}),
    )
    events: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(
        memory=service,
        agent_memory_opt_in=True,
        context=SimpleNamespace(principal=OWNER, run_id="run_finalize"),
        emit=lambda event_type, payload: events.append((event_type, payload)),
        resolved=lambda node: (None, None),
        llm=None,
        timeout=5.0,
    )
    state: dict[str, Any] = {
        "question": "Plan the migration.",
        "memo_markdown": "# Memo\n\nErgebnis [W1].",
        "usage": {},
    }

    _stage_memory_candidates(deps, state)  # type: ignore[arg-type]

    assert state["memory_status"] == "candidate_created"
    assert [item["content"] for item in state["memory_candidates"]] == [
        "Prefer concise implementation plans."
    ]
    # Candidate is PENDING for review — never auto-retained to the provider.
    assert provider.rows == {}
    pending = asyncio.run(
        service.list_candidates(principal=OWNER, status="pending")
    )
    assert len(pending) == 1
    assert events[-1][0] == "inqtrix.agent.activity"
    assert events[-1][1]["kind"] == "memory_candidate"


def test_memory_ids_are_scoped_to_principal_namespace() -> None:
    service = _service()
    candidate = asyncio.run(
        service.create_candidate(
            principal=OWNER,
            scope="user",
            category="preference",
            content="Prefer concise implementation plans.",
            reason="The user explicitly corrected verbosity.",
            confidence=0.9,
            source_run_id="run_1",
        )
    )
    accepted = asyncio.run(
        service.accept_candidate(
            principal=OWNER, candidate_id=candidate.candidate_id
        )
    )

    with pytest.raises(AgentMemoryNotFound):
        asyncio.run(
            service.update_memory(
                principal=OTHER,
                memory_id=accepted.memory_id,
                content="tamper",
                scope="user",
                category="preference",
            )
        )
    assert asyncio.run(service.list_memories(principal=OTHER)) == []


def test_memory_search_uses_recall_without_new_route_or_store() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Prefer vendor comparison tables.",
            scope="user",
            category="preference",
            confidence=0.8,
            source_run_id="run_1",
        )
    )

    rows = asyncio.run(
        service.list_memories(principal=OWNER, query="vendor table", scope="user")
    )

    assert [row.content for row in rows] == ["Prefer vendor comparison tables."]
    assert provider.recall_queries[-1]["query"] == "vendor table"


def test_auto_safe_status_degrades_to_candidate_only() -> None:
    service = AgentMemoryService(
        candidate_store=MemoryAgentMemoryCandidateStore(),
        feedback_store=MemoryAgentFeedbackStore(),
        provider=FakeMemoryProvider(),
        provider_name="mem0",
        mode="auto_safe",
    )

    status = service.status(OWNER)

    assert status["mode"] == "auto_safe"
    assert status["effective_mode"] == "candidate_only"
    assert status["degraded_reason"] == "auto_safe_not_implemented"


def test_candidate_inbox_is_user_scoped() -> None:
    service = _service()
    candidate = asyncio.run(
        service.create_candidate(
            principal=OWNER,
            scope="project",
            category="strategy",
            content="For project X, review migrations before API routes.",
            reason="Repeated issue in the run.",
            confidence=0.8,
            source_run_id="run_2",
        )
    )

    assert asyncio.run(service.list_candidates(principal=OTHER)) == []
    owner_rows = asyncio.run(service.list_candidates(principal=OWNER))
    assert [row.candidate_id for row in owner_rows] == [candidate.candidate_id]


def test_feedback_history_is_user_scoped_and_provider_feedback_is_optional() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    memory = asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Prefer concise plans.",
            scope="user",
            category="preference",
            confidence=0.9,
            source_run_id="run_1",
        )
    )

    row = asyncio.run(
        service.feedback(
            principal=OWNER,
            run_id="run_1",
            memory_id=memory.memory_id,
            feedback="positive",
            reason="helpful",
        )
    )
    asyncio.run(
        service.feedback(
            principal=OTHER,
            run_id="run_other",
            memory_id="",
            feedback="neutral",
            reason="",
        )
    )

    assert row.feedback == "positive"
    assert provider.feedback_calls == [
        {
            "namespace": service._namespace_for(OWNER),  # type: ignore[attr-defined]
            "memory_id": memory.memory_id,
            "feedback": "positive",
            "reason": "helpful",
        }
    ]
    assert [
        item.run_id
        for item in asyncio.run(service.list_feedback(principal=OWNER))
    ] == ["run_1"]


def test_feedback_with_foreign_memory_id_is_indistinguishable_404() -> None:
    service = _service()

    with pytest.raises(AgentMemoryNotFound):
        asyncio.run(
            service.feedback(
                principal=OTHER,
                run_id="run_1",
                memory_id="mem_foreign",
                feedback="negative",
                reason="wrong memory",
            )
        )


def test_router_rejects_owner_fields_and_static_memory_access() -> None:
    provider = FakeMemoryProvider()
    client = _memory_client(_service(provider))

    assert client.get("/v1/agent/memory", headers={"x-kind": "static"}).status_code == 404
    injected = client.get(
        f"/v1/agent/memory?user_id={OTHER_ID}",
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert injected.status_code == 400
    assert "Owner fields" in injected.json()["error"]["message"]

    candidate = asyncio.run(
        client.service.create_candidate(  # type: ignore[attr-defined]
            principal=OWNER,
            scope="user",
            category="preference",
            content="Use tables for vendor comparisons.",
            reason="User approved this presentation style.",
            confidence=0.75,
            source_run_id="run_3",
        )
    )
    accepted = client.post(
        f"/v1/agent/memory/candidates/{candidate.candidate_id}:accept",
        json={},
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert accepted.status_code == 200
    memory_id = accepted.json()["memory_id"]

    tamper = client.patch(
        f"/v1/agent/memory/{memory_id}",
        json={"content": "tamper", "user_id": str(OTHER_ID)},
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert tamper.status_code == 400

    other_update = client.patch(
        f"/v1/agent/memory/{memory_id}",
        json={"content": "tamper", "scope": "user", "category": "preference"},
        headers={"x-user-id": str(OTHER_ID)},
    )
    assert other_update.status_code == 404


def test_router_feedback_history_and_memory_search_are_scoped() -> None:
    provider = FakeMemoryProvider()
    client = _memory_client(_service(provider))
    memory = asyncio.run(
        provider.retain(
            namespace=client.service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Use decision tables.",
            scope="user",
            category="strategy",
            confidence=0.8,
            source_run_id="run_4",
        )
    )

    searched = client.get(
        "/v1/agent/memory?q=decision&scope=user&limit=5",
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert searched.status_code == 200
    assert searched.json()["data"][0]["id"] == memory.memory_id
    assert provider.recall_queries[-1]["query"] == "decision"

    injected = client.post(
        "/v1/agent/runs/run_4/feedback",
        json={"feedback": "positive", "tenant_id": "other"},
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert injected.status_code == 400

    posted = client.post(
        "/v1/agent/runs/run_4/feedback",
        json={
            "feedback": "positive",
            "reason": "useful",
            "memory_id": memory.memory_id,
        },
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert posted.status_code == 200
    assert posted.json()["run_id"] == "run_4"

    other = client.get(
        "/v1/agent/memory/feedback",
        headers={"x-user-id": str(OTHER_ID)},
    )
    assert other.status_code == 200
    assert other.json()["data"] == []

    owner = client.get(
        "/v1/agent/memory/feedback?run_id=run_4",
        headers={"x-user-id": str(OWNER_ID)},
    )
    assert owner.status_code == 200
    assert [row["feedback"] for row in owner.json()["data"]] == ["positive"]

    foreign = client.post(
        "/v1/agent/runs/run_4/feedback",
        json={"feedback": "negative", "memory_id": memory.memory_id},
        headers={"x-user-id": str(OTHER_ID)},
    )
    assert foreign.status_code == 404


def _memory_client(service: AgentMemoryService) -> TestClient:
    async def principal_dependency(request: Request) -> Principal:
        kind = request.headers.get("x-kind", "oidc")
        if kind == "anonymous":
            return ANONYMOUS_PRINCIPAL
        if kind == "static":
            return STATIC_PRINCIPAL
        user_id = request.headers.get("x-user-id", str(OWNER_ID))
        return Principal(
            user_id=uuid.UUID(user_id),
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )

    app = FastAPI()
    app.include_router(
        build_router(
            SimpleNamespace(
                agent_memory_service=service,
                principal_dependency=principal_dependency,
            )
        )
    )
    client = TestClient(app)
    client.service = service  # type: ignore[attr-defined]
    return client


# -- kernel wiring: same service, same gates -------------------------------- #


def _kernel_algo_and_deps(
    service: AgentMemoryService, *, opt_in: bool
) -> tuple[Any, Any, list[tuple[str, dict[str, Any]]]]:
    """Minimal kernel algorithm + deps pair for the memory seams."""
    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm
    from inqtrix.agents.kernel.deps import KernelDeps

    algo = KernelAgentAlgorithm(
        control_store=None,  # type: ignore[arg-type]
        checkpointer=None,  # type: ignore[arg-type]
        platform=SimpleNamespace(),  # type: ignore[arg-type]
        agent_memory_service=service,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    deps = KernelDeps(
        run_id="run_kernel_memory",
        control=None,  # type: ignore[arg-type]
        platform=SimpleNamespace(),  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=5.0,
        event_sink=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )
    deps.memory = service
    deps.memory_opt_in = opt_in
    deps.principal = OWNER
    deps.question = "Wie praesentiere ich Anbieter?"
    return algo, deps, events


def test_kernel_recall_injects_the_memory_briefing() -> None:
    """The kernel recalls the SAME memory the mission engine reads."""
    from inqtrix.agents.prompts import build_kernel_user_message

    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Bevorzugt kompakte Vergleichstabellen.",
            scope="user",
            category="preference",
            confidence=0.9,
            source_run_id="run_seed",
        )
    )
    algo, deps, _events = _kernel_algo_and_deps(service, opt_in=True)
    request = SimpleNamespace(
        question="Wie praesentiere ich Anbieter?", history="", session_id=""
    )

    algo._recall_memory_briefing(request, deps)

    assert deps.memory_status == "used"
    assert "Bevorzugt kompakte Vergleichstabellen." in deps.memory_briefing

    message = build_kernel_user_message(
        "Frage", memory_briefing=deps.memory_briefing
    )
    assert "Non-citable long-term memory" in message
    assert "Bevorzugt kompakte Vergleichstabellen." in message
    assert "NEVER cite it as evidence" in message
    # Without a briefing the memory section must not render at all.
    assert "long-term memory" not in build_kernel_user_message("Frage")


def test_kernel_recall_respects_opt_out_and_narrates_unavailable() -> None:
    provider = FakeMemoryProvider()
    service = _service(provider)
    request = SimpleNamespace(question="Frage", history="", session_id="")

    algo, deps, _events = _kernel_algo_and_deps(service, opt_in=False)
    algo._recall_memory_briefing(request, deps)
    assert deps.memory_status == "disabled"
    assert provider.recall_queries == []

    provider.fail_recall = True
    algo, deps, events = _kernel_algo_and_deps(service, opt_in=True)
    algo._recall_memory_briefing(request, deps)
    assert deps.memory_status == "unavailable"
    narrations = [
        payload
        for event_type, payload in events
        if event_type == "inqtrix.agent.narration"
    ]
    assert narrations and narrations[-1]["narration_id"] == (
        "kernel_memory_recall"
    )


def test_kernel_stages_candidates_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.agents import memory_reflection
    from inqtrix.agents.phase_models import (
        MemoryCandidateModel,
        MemoryReflection,
    )

    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.algorithm import _stage_kernel_memory

    provider = FakeMemoryProvider()
    service = _service(provider)
    reflection = MemoryReflection(
        candidates=[
            MemoryCandidateModel(
                scope="user",
                category="preference",
                content="Bevorzugt deutsche Antworten.",
                reason="Der Nutzer fragte auf Deutsch.",
                confidence=0.8,
            )
        ]
    )
    monkeypatch.setattr(
        memory_reflection,
        "run_memory_reflection",
        lambda *args, **kwargs: SimpleNamespace(value=reflection, usage={}),
    )
    _algo, deps, _events = _kernel_algo_and_deps(service, opt_in=True)

    _stage_kernel_memory(deps, "Finale Antwort mit Substanz.")

    pending = asyncio.run(
        service.list_candidates(principal=OWNER, status="pending")
    )
    assert [row.content for row in pending] == [
        "Bevorzugt deutsche Antworten."
    ]
    # Candidate-only: nothing is auto-retained to the provider.
    assert provider.rows == {}

    # Opt-out stages nothing (same privacy gate as recall).
    _algo, deps_off, _ = _kernel_algo_and_deps(service, opt_in=False)
    _stage_kernel_memory(deps_off, "Weitere Antwort.")
    still_pending = asyncio.run(
        service.list_candidates(principal=OWNER, status="pending")
    )
    assert len(still_pending) == 1


def test_kernel_recall_latches_once_per_segment() -> None:
    """Deep verify rebuilds the user message — recall must not run twice."""
    provider = FakeMemoryProvider()
    service = _service(provider)
    asyncio.run(
        provider.retain(
            namespace=service._namespace_for(OWNER),  # type: ignore[attr-defined]
            content="Bevorzugt kompakte Vergleichstabellen.",
            scope="user",
            category="preference",
            confidence=0.9,
            source_run_id="run_seed",
        )
    )
    algo, deps, _events = _kernel_algo_and_deps(service, opt_in=True)
    request = SimpleNamespace(
        question="Wie praesentiere ich Anbieter?", history="", session_id=""
    )
    algo._recall_memory_briefing(request, deps)
    algo._recall_memory_briefing(request, deps)
    assert len(provider.recall_queries) == 1
    assert deps.memory_status == "used"
