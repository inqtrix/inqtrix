"""Kernel walking skeleton: ask_user park/resume end-to-end.

Drives ``mode=agent_kernel`` through the FULL platform path (router ->
RunService -> worker -> park -> clarification answer -> resume) with a
scripted tool-calling provider — the same trajectory a real user takes,
no seam skipped. Plus the pure interrupt-translation contract.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.agents.kernel.interrupts import (
    CLARIFICATION_INTERRUPT,
    TOOL_APPROVAL_INTERRUPT,
    ask_user_clarification_id,
    translate_kernel_interrupt,
)
from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


class ScriptedToolLLM(LLMProvider):
    """Sequential ChatTurn script with full call recording."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self.models = ModelSettings(
            reasoning_model="base-model",
            tier_high_model="high-model",
            tier_mid_model="mid-model",
            tier_fast_model="fast-model",
        )
        self._turns = list(turns)
        self.chat_calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        raise AssertionError("kernel must use chat(), not complete()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(
        self, messages: Any, *, tools: Any = None, model: Any = None, **kwargs: Any
    ) -> ChatTurn:
        self.chat_calls.append(
            {"messages": list(messages), "tools": tools, "model": model}
        )
        if not self._turns:
            raise AssertionError("scripted provider ran out of turns")
        return self._turns.pop(0)


class RetryNotifyingToolLLM(ScriptedToolLLM):
    """Kernel provider that exposes one retry through the shared seam."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        super().__init__(turns)
        self._retry_callback: Any = None
        self._retry_emitted = False

    @contextmanager
    def observe_retries(self, callback: Any) -> Any:
        previous = self._retry_callback
        self._retry_callback = callback
        try:
            yield self
        finally:
            self._retry_callback = previous

    def chat(
        self, messages: Any, *, tools: Any = None, model: Any = None, **kwargs: Any
    ) -> ChatTurn:
        if not self._retry_emitted and self._retry_callback is not None:
            self._retry_emitted = True
            self._retry_callback({
                "attempt": 1,
                "max_attempts": 3,
                "delay_seconds": 0.5,
                "error_code": "rate_limited",
                "operation": "chat",
            })
        return super().chat(
            messages,
            tools=tools,
            model=model,
            **kwargs,
        )


def _ask_turn() -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_ask0001",
                name="ask_user",
                arguments={
                    "question": "Welches Format soll die Antwort haben?",
                    "options": ["Kompakt", "Ausfuehrlich", "Stichpunkte"],
                    "default_assumption": "Kompakt",
                },
            ),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=40,
        completion_tokens=12,
        raw=None,
    )


def _answer_turn() -> ChatTurn:
    return ChatTurn(
        text="Hier ist die kompakte Antwort auf deinen Auftrag.",
        tool_calls=(),
        finish_reason="stop",
        model="high-model",
        prompt_tokens=60,
        completion_tokens=25,
        raw=None,
    )


def _two_read_calls_turn() -> ChatTurn:
    """A side-effect-free batch that cannot partially dispatch."""
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_read0001",
                name="read_canvas",
                arguments={"artifact_id": "missing-a"},
            ),
            ToolCallRequest(
                id="call_read0002",
                name="read_canvas",
                arguments={"artifact_id": "missing-b"},
            ),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=40,
        completion_tokens=12,
        raw=None,
    )


def make_kernel_client(
    llm: ScriptedToolLLM,
    *,
    max_tokens_per_run: int = 0,
    max_tool_calls: int = 30,
    max_iterations: int = 73,
) -> TestClient:
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=max_tool_calls,
        INQTRIX_AGENT_KERNEL_MAX_ITERATIONS=max_iterations,
    )
    if max_tokens_per_run:
        settings.quota.max_tokens_per_run = max_tokens_per_run
    return _client_for(settings, llm)


def _client_for(settings: Settings, llm: ScriptedToolLLM) -> TestClient:
    app = FastAPI()
    router = create_router()
    register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.llm = llm  # type: ignore[attr-defined]
    return client


def wait_status(
    client: TestClient, run_id: str, statuses: set[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never reached {statuses}")


def run_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    with client.stream("GET", f"/v1/runs/{run_id}/events") as stream:
        body = stream.read().decode("utf-8")
    return [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_default_mode_flip_requires_registered_kernel():
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(DEPTH="deep"),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
        INQTRIX_AGENT_DEFAULT_MODE="agent_kernel",
    )
    with _client_for(settings, ScriptedToolLLM([])) as client:
        agent = client.get("/v1/capabilities").json()["agent"]
        assert agent["default_mode"] == "agent_kernel"
        assert agent["default_depth"] == "deep"

    # Kernel configured as default but NOT registered (rollout switch
    # off): capabilities fall back so the desk never submits a 400 mode.
    settings2 = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings2.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=False,
        INQTRIX_AGENT_DEFAULT_MODE="agent_kernel",
    )
    with _client_for(settings2, ScriptedToolLLM([])) as client:
        agent = client.get("/v1/capabilities").json()["agent"]
        assert agent["default_mode"] == "workspace_agent"
        assert agent["default_depth"] == "normal"


def test_kernel_model_retries_use_agent_activity_channel():
    llm = RetryNotifyingToolLLM([_answer_turn()])
    client = make_kernel_client(llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={"question": "Fasse den EU AI Act zusammen.", "mode": "agent_kernel"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        retries = [
            event["data"]
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.activity"
            and event["data"].get("retry")
        ]

    assert len(retries) == 1
    assert retries[0]["scope"] == "run"
    assert retries[0]["phase"] == "execution"
    assert retries[0]["purpose"] == "Agent-Antwort wird fortgesetzt"
    assert retries[0]["retry"]["max_attempts"] == 3


def test_kernel_completion_materializes_answer_artifact():
    """A completed kernel run persists the ``answer`` artifact the desk renders.

    An answer returned only in ``AgentResult`` and never as a
    ``kind="answer"`` artifact would leave the Agent Desk without answer
    text. This pins the materialization + the
    ``artifact.created`` event the frontend refetches on.
    """
    llm = ScriptedToolLLM([_answer_turn()])
    client = make_kernel_client(llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Fasse den EU AI Act zusammen.",
                "mode": "agent_kernel",
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"completed"})

        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        answers = [a for a in artifacts if a["kind"] == "answer"]
        assert len(answers) == 1
        assert answers[0]["title"] == "Antwort"
        assert answers[0]["status"] == "ready"
        # Deterministic id -> idempotent across a redelivered terminal segment.
        assert answers[0]["artifact_id"] == f"art_{run_id[-12:]}_answer"

        body = client.get(
            f"/v1/runs/{run_id}/artifacts/{answers[0]['artifact_id']}"
        ).json()
        assert body["content_markdown"] == (
            "Hier ist die kompakte Antwort auf deinen Auftrag."
        )

        created = [
            event
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.artifact.created"
            and event["data"].get("kind") == "answer"
        ]
        assert len(created) == 1


def test_ask_user_park_resume_trajectory():
    llm = ScriptedToolLLM([_ask_turn(), _answer_turn()])
    client = make_kernel_client(llm)
    with client:
        capabilities = client.get("/v1/capabilities").json()
        assert capabilities["features"]["agent_kernel"] is True
        # The kernel is the Agent Desk front door once its independent
        # registration gate passes.
        assert capabilities["agent"]["default_mode"] == "agent_kernel"

        response = client.post(
            "/v1/runs",
            json={"question": "Fasse den EU AI Act zusammen.", "mode": "agent_kernel"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        wait_status(client, run_id, {"waiting_for_input"})
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        # Deterministic id derived from run + frozen tool_call_id.
        assert row["clarification_id"] == ask_user_clarification_id(
            run_id, "call_ask0001"
        )
        assert row["question"] == "Welches Format soll die Antwort haben?"
        assert [o["label"] for o in row["questions"][0]["options"]] == [
            "Kompakt",
            "Ausfuehrlich",
            "Stichpunkte",
        ]
        assert row["default_assumption"] == "Kompakt"

        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}",
            json={"option_id": "q1_o2"},
        )
        assert answered.status_code == 200, answered.text

        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == (
            "Hier ist die kompakte Antwort auf deinen Auftrag."
        )

        # The resumed segment saw the user's answer as the tool result.
        assert len(llm.chat_calls) == 2
        resumed_messages = llm.chat_calls[1]["messages"]
        tool_replies = [
            m for m in resumed_messages if m.get("role") == "tool"
        ]
        assert len(tool_replies) == 1
        assert "User's answer: Ausfuehrlich" in tool_replies[0]["content"]
        # The ask_user tool schema went to the provider on both calls.
        tool_names = {
            t["function"]["name"] for t in (llm.chat_calls[0]["tools"] or [])
        }
        assert "ask_user" in tool_names


def test_tool_limit_extension_resumes_checkpoint_without_repeating_model_call():
    """An explicit extension continues the rejected batch exactly once."""
    llm = ScriptedToolLLM([_two_read_calls_turn(), _answer_turn()])
    client = make_kernel_client(llm, max_tool_calls=1)
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]

        waiting = wait_status(client, run_id, {"waiting_for_input"})
        assert waiting["status"] == "waiting_for_input"
        assert len(llm.chat_calls) == 1
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        assert len(rows) == 1
        assert [option["id"] for option in rows[0]["options"]] == [
            "extend",
            "partial",
            "cancel",
        ]

        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{rows[0]['clarification_id']}",
            json={"option_id": "extend"},
        )
        assert answered.status_code == 200, answered.text
        wait_status(client, run_id, {"completed"})

        # The first provider turn is checkpointed. Resume dispatches the
        # whole batch that was held back, then asks only for the final answer.
        assert len(llm.chat_calls) == 2
        result = client.get(f"/v1/runs/{run_id}/result").json()
        limits = result["execution"]["limits"]
        assert limits["tool_calls"] == {
            "used": 2,
            "limit": 2,
            "ceiling": 60,
            "recoverable": True,
            "extendable": True,
            "reason": "",
        }
        stored = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        assert stored[0]["status"] == "answered"
        assert stored[0]["option_id"] == "extend"


def test_tool_limit_partial_requires_explicit_choice_and_calls_no_model():
    """A reached limit remains parked until the user accepts a partial."""
    llm = ScriptedToolLLM([_two_read_calls_turn()])
    client = make_kernel_client(llm, max_tool_calls=1)
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]
        wait_status(client, run_id, {"waiting_for_input"})
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]

        # Merely reaching the boundary never fabricates a partial answer.
        assert client.get(f"/v1/runs/{run_id}").json()["status"] == "waiting_for_input"
        assert len(llm.chat_calls) == 1
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{rows[0]['clarification_id']}",
            json={"option_id": "partial"},
        )
        assert answered.status_code == 200, answered.text
        wait_status(client, run_id, {"completed"})

        result = client.get(f"/v1/runs/{run_id}/result").json()
        # "Auftrag." carries no resolvable language signal, so the receipt
        # is written in the neutral fallback rather than silently in German.
        assert "Partial result" in result["answer"]
        assert "No complete synthesis was simulated" in result["answer"]
        decisions = [
            event
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.limit.decided"
        ]
        assert [event["data"]["choice"] for event in decisions] == ["partial"]
        assert len(llm.chat_calls) == 1


def test_step_limit_extensions_use_cumulative_checkpoint_coordinate():
    """Repeated resumes widen one cumulative allowance, not fresh segments."""
    llm = ScriptedToolLLM([_answer_turn()])
    client = make_kernel_client(llm, max_iterations=3)
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]

        answered_limits = 0
        for _ in range(4):
            summary = wait_status(
                client, run_id, {"waiting_for_input", "completed", "failed"}
            )
            if summary["status"] == "completed":
                break
            assert summary["status"] == "waiting_for_input", summary
            rows = client.get(
                f"/v1/runs/{run_id}/clarifications"
            ).json()["data"]
            pending = [row for row in rows if row["status"] == "pending"]
            assert len(pending) == 1
            # "Auftrag." carries no resolvable language signal, so the card
            # is written in the neutral fallback (English), never silently
            # in German. The DE/EN pair itself is pinned as a unit in
            # tests/agents/test_limit_contract.py.
            assert "execution steps" in pending[0]["question"]
            response = client.post(
                f"/v1/runs/{run_id}/clarifications/"
                f"{pending[0]['clarification_id']}",
                json={"option_id": "extend"},
            )
            assert response.status_code == 200, response.text
            answered_limits += 1
        result_summary = wait_status(client, run_id, {"completed", "failed"})
        assert result_summary["status"] == "completed", result_summary
        assert answered_limits >= 1
        assert len(llm.chat_calls) == 1
        step_limit = client.get(f"/v1/runs/{run_id}/result").json()[
            "execution"
        ]["limits"]["steps"]
        assert step_limit["used"] <= step_limit["limit"]
        assert step_limit["limit"] <= step_limit["ceiling"]


def test_two_ask_user_rounds_use_distinct_rows():
    """Regression: prefix-sharing tool_call_ids must not collide.

    Round 2 reusing round 1's ANSWERED row would skip the create, park
    the run as waiting_for_input with nothing pending, and strand it.
    """
    second_ask = ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_ask0002",
                name="ask_user",
                arguments={
                    "question": "Welche Sprache?",
                    "options": ["Deutsch", "Englisch"],
                    "default_assumption": "Deutsch",
                },
            ),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=20,
        completion_tokens=8,
        raw=None,
    )
    llm = ScriptedToolLLM([_ask_turn(), second_ask, _answer_turn()])
    client = make_kernel_client(llm)
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]

        wait_status(client, run_id, {"waiting_for_input"})
        first_rows = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{first_rows[0]['clarification_id']}",
            json={"answer": "Kompakt."},
        )

        wait_status(client, run_id, {"waiting_for_input"})
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        assert len(rows) == 2
        pending = [r for r in rows if r["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["question"] == "Welche Sprache?"
        client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{pending[0]['clarification_id']}",
            json={"answer": "Deutsch."},
        )

        wait_status(client, run_id, {"completed"})


def test_token_budget_stops_the_kernel_across_segments():
    """The per-run cap counts checkpointed spend from earlier segments.

    Segment 1 books 52 tokens (over the 30-token cap); the resumed
    segment's first model call must abort BEFORE reaching the provider and
    terminate with the distinct token-budget failure type.
    """
    llm = ScriptedToolLLM([_ask_turn(), _answer_turn()])
    client = make_kernel_client(llm, max_tokens_per_run=30)
    with client:
        token_limit = client.get("/v1/capabilities").json()["agent"]["limits"][
            "tokens"
        ]
        assert token_limit == {
            "enabled": True,
            "limit": 30,
            "ceiling": 30,
            "recoverable": False,
            "extendable": False,
            "reason": "operator_ceiling_exactly_once_required",
        }
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]
        wait_status(client, run_id, {"waiting_for_input"})
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{rows[0]['clarification_id']}",
            json={"answer": "Egal."},
        )
        summary = wait_status(client, run_id, {"failed", "completed"})
        assert summary["status"] == "failed"
        assert summary["error"]["type"] == "token_budget_exceeded"
        limit_events = [
            event
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.limit.reached"
        ]
        assert limit_events[-1]["data"] == {
            "kind": "tokens",
            "used": 52,
            "limit": 30,
            "ceiling": 30,
            "extendable": False,
            "recoverable": False,
            "reason": "operator_ceiling_exactly_once_required",
            "state": "failed_at_model_boundary",
        }
        # The second scripted turn was never consumed: the abort fired
        # at the model boundary, not after another paid call.
        assert len(llm.chat_calls) == 1


def test_the_shipping_default_registers_the_kernel():
    """A deployment that configures nothing runs the cognitive kernel.

    The switch is the single value that decides which engine a fresh
    installation gets, and nothing else pins it: without this test the
    default could flip either way unnoticed. Read from the DECLARATION,
    not from a constructed object — a settings instance takes the
    ambient environment, so it would pin whatever the machine exports.
    """
    assert (
        AgentPlatformSettings.model_fields["kernel_enabled"].default is True
    )
    llm = ScriptedToolLLM([])
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    with _client_for(settings, llm) as client:
        payload = client.get("/v1/capabilities").json()
        assert payload["features"]["agent_kernel"] is True
        assert payload["agent"]["default_mode"] == "agent_kernel"
        assert payload["feature_status"]["agent_kernel"] == {
            "configured": True,
            "available": True,
            "state": "enabled",
            "reason_code": None,
        }


def test_a_missed_gate_names_itself_instead_of_reading_as_switched_off():
    """With the kernel on by default, "unavailable" means something broke.

    An absent kernel is never the operator's own choice: the reason the
    registration path already logs has to reach
    the surface, or the fallback to the phase machine is silent.
    """
    class NoToolsLLM(ScriptedToolLLM):
        def supports_tool_calls(self, *, model: str | None = None) -> bool:
            return False

    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    with _client_for(settings, NoToolsLLM([])) as client:
        payload = client.get("/v1/capabilities").json()
        assert payload["features"]["agent_kernel"] is False
        assert payload["agent"]["default_mode"] == "workspace_agent"
        status = payload["feature_status"]["agent_kernel"]
        assert status["state"] == "degraded"
        assert status["reason_code"] == "tool_calls_required"


def test_a_switched_off_kernel_is_an_unknown_mode():
    """Switching the kernel off removes the mode, visibly and completely.

    The switch is the operator's way back to the phase machine, so it must
    take mode=agent_kernel out of the published surface AND out of the
    admitted request vocabulary — a mode that is advertised but refused,
    or refused but advertised, is worse than either state.
    """
    llm = ScriptedToolLLM([])
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=False,
    )
    register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/runs", json={"question": "x", "mode": "agent_kernel"}
        )
        assert response.status_code == 400
        payload = client.get("/v1/capabilities").json()
        assert payload["features"]["agent_kernel"] is False
        # Switched off by the operator, not held back by a gate.
        status = payload["feature_status"]["agent_kernel"]
        assert status["state"] == "disabled"
        assert status["reason_code"] == "operator_disabled"


def test_kernel_gate_warns_without_tool_calls(caplog):
    class NoToolsLLM(ScriptedToolLLM):
        def supports_tool_calls(self, *, model: str | None = None) -> bool:
            return False

    llm = NoToolsLLM([])
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    with caplog.at_level("WARNING", logger="inqtrix"):
        register_routes(
            router,
            providers=SimpleNamespace(llm=llm, search=None),
            strategies=SimpleNamespace(),
            settings=settings,
            semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
        )
    app.include_router(router)
    assert any(
        "Agent kernel" in record.message for record in caplog.records
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/runs", json={"question": "x", "mode": "agent_kernel"}
        )
        assert response.status_code == 400


def test_kernel_gate_reads_an_unimplemented_capability_as_missing(caplog):
    """A provider without the tool-call surface misses the gate, not the build.

    The gate decides whether one mode registers; it must never decide
    whether the server starts. An adapter is free to implement the
    prompt surface alone, so the gate reads the capability the way the
    provider decorators read it and lands an undeclared capability in
    the WARNING below — an AttributeError here would take every route
    of the deployment down with the mode.
    """

    class PromptOnlyLLM:
        """Duck-typed provider without any tool-calling declaration."""

        def complete(self, prompt: str, **kwargs: Any) -> str:
            return "ok"

        def is_available(self) -> bool:
            return True

    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    with caplog.at_level("WARNING", logger="inqtrix"):
        register_routes(
            router,
            providers=SimpleNamespace(llm=PromptOnlyLLM(), search=None),
            strategies=SimpleNamespace(),
            settings=settings,
            semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
        )
    app.include_router(router)
    assert any(
        "tool_calls=missing" in record.getMessage()
        for record in caplog.records
    )
    with TestClient(app) as client:
        assert (
            client.get("/v1/capabilities").json()["features"]["agent_kernel"]
            is False
        )


def test_ask_user_discards_empty_question_without_parking():
    """A whitespace question must not park the run on an empty prompt."""
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import (
        KernelDeps,
        run_coro,
        set_kernel_deps,
    )
    from inqtrix.agents.kernel.tools import build_kernel_tools

    store = MemoryAgentControlStore()
    set_kernel_deps(
        KernelDeps(
            run_id="run_guard",
            control=store,
            platform=None,  # type: ignore[arg-type]
            llm=None,  # type: ignore[arg-type]
            model=None,
            reasoning_effort=None,
            timeout=1.0,
        )
    )
    try:
        ask_user = build_kernel_tools()[0]
        message = ask_user.invoke(
            {
                "name": "ask_user",
                "type": "tool_call",
                "id": "call_guard1",
                "args": {
                    "question": "   ",
                    "options": [],
                    "default_assumption": "Kompakt",
                },
            }
        )
    finally:
        set_kernel_deps(None)
    result = str(message.content)
    assert "discarded" in result
    assert "Kompakt" in result
    assert run_coro(store.list_clarifications("run_guard")) == []


def test_translate_kernel_interrupt_discriminates_origins():
    origin, payload = translate_kernel_interrupt(
        {"kind": "clarification", "id": "clr_abc"}
    )
    assert (origin, payload) == (CLARIFICATION_INTERRUPT, {"id": "clr_abc"})

    origin, payload = translate_kernel_interrupt(
        {
            "action_requests": [{"name": "web_instant", "args": {}}],
            "review_configs": [],
        }
    )
    assert origin == TOOL_APPROVAL_INTERRUPT
    assert payload["action_requests"][0]["name"] == "web_instant"

    with pytest.raises(ValueError):
        translate_kernel_interrupt({"kind": "unbekannt"})
    with pytest.raises(ValueError):
        translate_kernel_interrupt("garbage")
    # An actionless HITL payload has nothing to approve — routing it
    # would park a run no decision can wake.
    with pytest.raises(ValueError):
        translate_kernel_interrupt(
            {"action_requests": [], "review_configs": []}
        )


def test_ask_user_clarification_id_is_deterministic_and_collision_safe():
    first = ask_user_clarification_id("run_1234567890ab", "call_abcdef12")
    assert first == ask_user_clarification_id(
        "run_1234567890ab", "call_abcdef12"
    )
    # Provider ids share long constant prefixes (call_..., toolu_01...);
    # the id must still differ per call — a collision reuses round 1's
    # answered row and strands round 2 parked with nothing pending.
    prefix_shared = [
        ask_user_clarification_id("run_1234567890ab", tool_call_id)
        for tool_call_id in (
            "toolu_01Aaaaaaaaaaaaaaaaaaaaaa",
            "toolu_01Bbbbbbbbbbbbbbbbbbbbbb",
            "call_abc111111111111111111",
            "call_abc222222222222222222",
        )
    ]
    assert len(set(prefix_shared)) == len(prefix_shared)


def test_result_usage_and_metrics_sum_all_segments():
    """A parked run reports the whole run, not its last segment.

    Segment 1 (ask_user) books 40/12, the resumed segment 60/25: the
    published usage, the completion metrics and the token counter of
    the execution limits must all say 100/37 — earlier the result and
    the run.completed metrics carried only the resumed segment's 60/25
    while the limits said 137.
    """
    llm = ScriptedToolLLM([_ask_turn(), _answer_turn()])
    client = make_kernel_client(llm, max_tokens_per_run=1000)
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
        ).json()["run_id"]
        wait_status(client, run_id, {"waiting_for_input"})
        rows = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        client.post(
            f"/v1/runs/{run_id}/clarifications/{rows[0]['clarification_id']}",
            json={"answer": "Egal."},
        )
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 37,
            "total_tokens": 137,
            # Summed over every segment, so the count is the run's - and
            # the field that says which travels with it.
            "scope": "run",
        }
        assert result["metrics"]["prompt_tokens"] == 100
        assert result["metrics"]["completion_tokens"] == 37
        assert result["execution"]["limits"]["tokens"]["used"] == 137
        completed = [
            event for event in run_events(client, run_id)
            if event["type"] == "inqtrix.run.completed"
        ][-1]
        assert completed["data"]["metrics"]["prompt_tokens"] == 100
        assert completed["data"]["metrics"]["completion_tokens"] == 37


def test_partial_answer_writes_reference_titles_without_markdown_escapes():
    """A bracket in a source title stays a bracket-shaped word, never an
    escape: the web renderer treats a backslash-escaped bracket pair as a
    LaTeX display delimiter, so an escaped title was typeset as a formula."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import _partial_limit_answer

    answer = _partial_limit_answer(
        SimpleNamespace(kind="tool_calls"),  # type: ignore[arg-type]
        [{"label": "W1", "title": "Lagebericht [2024]  der  Agentur"}],
        language="de",
    )
    assert "- [W1] Lagebericht (2024) der Agentur" in answer
    assert "\\[" not in answer and "\\]" not in answer
    assert answer.startswith("## Teilstand")

    # The receipt follows the run language; an unresolved one reads English.
    english = _partial_limit_answer(
        SimpleNamespace(kind="tool_calls"),  # type: ignore[arg-type]
        [{"label": "W1", "title": "Situation report"}],
        language="en",
    )
    assert english.startswith("## Partial result")
    assert "Teilstand" not in english


@pytest.mark.parametrize(
    ("sources", "language", "line"),
    [
        (31, "de", "- 1 weitere Quelle bleibt im Belegprotokoll erhalten."),
        (32, "de", "- 2 weitere Quellen bleiben im Belegprotokoll erhalten."),
        (31, "en", "- 1 further source remains in the evidence log."),
        (33, "en", "- 3 further sources remain in the evidence log."),
    ],
)
def test_partial_answer_counts_the_sources_it_does_not_list(sources, language, line):
    """The receipt lists 30 sources; the rest is one counted line, in the
    singular for one ("Weitere 1 Quellen" read wrong before)."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import _partial_limit_answer

    answer = _partial_limit_answer(
        SimpleNamespace(kind="tool_calls"),  # type: ignore[arg-type]
        [{"label": f"W{i}", "title": f"Quelle {i}"} for i in range(1, sources + 1)],
        language=language,
    )
    assert line in answer.splitlines()



def test_the_kernel_carries_the_users_own_request_text_into_every_segment(monkeypatch):
    """The writer guard reads the user's words off the durable request.

    ``session_history`` and ``memory_briefing`` are filled only where a
    segment composes a user message, so a resumed segment holds neither. If
    the request's own fields were not copied onto the deps, the guard would
    treat an identifier the user wrote as one the model introduced and send
    a correct answer into the repair round.
    """
    from inqtrix.agents.kernel import algorithm as kernel_algorithm

    seen: list[str] = []
    real = kernel_algorithm._validate_kernel_answer_citations

    def spy(deps: Any, answer: str, ledger: Any) -> str:
        seen.append(deps.request_user_text)
        return real(deps, answer, ledger)

    monkeypatch.setattr(
        kernel_algorithm, "_validate_kernel_answer_citations", spy
    )
    client = make_kernel_client(ScriptedToolLLM([_answer_turn()]))
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Fasse den EU AI Act zusammen.",
                "mode": "agent_kernel",
                "report_guidance": "Beziehe dich auf doc-42.",
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"completed"})
    assert seen, "the final answer never reached the citation guard"
    assert "doc-42" in seen[0], seen


def test_the_durable_request_hands_over_the_users_half_of_its_transcript():
    """An API caller may send the conversation on the request itself.

    That transcript holds both voices. Copying it whole onto the deps would
    let an id the model wrote in an earlier turn exempt every later copy of
    itself - the guard would certify what it exists to catch - so the
    request is projected exactly like the session's own history.
    """
    from inqtrix.agents.kernel.algorithm import _request_user_text
    from inqtrix.core.results import RunRequest

    text = _request_user_text(RunRequest(
        question="Fasse zusammen.",
        history="Nutzer: bitte kd_user\nAssistent: laut kd_model gilt X.",
        report_requirement="Beziehe dich auf kd_guide.",
    ))
    assert "kd_user" in text and "kd_guide" in text
    assert "kd_model" not in text
