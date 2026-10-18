"""Kernel sufficiency gate: adaptive advisory evidence judgement.

The middleware tests pin threshold cheapness, nudge shape, the run-wide
cap, the same-state latch used for park/resume idempotency, and
loud-but-nonfatal degradation. The platform test drives a real kernel
run through three searches and the resulting nudge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import HumanMessage

from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.kernel.middleware import (
    POST_MISSION_NUDGE_FLAG,
    SUFFICIENCY_NUDGE_FLAG,
    KernelSufficiencyMiddleware,
)
from inqtrix.providers.base import LLMProvider
from inqtrix.settings import AgentPlatformSettings

JUDGED_EVENT = "inqtrix.agent.sufficiency.judged"


class StructuredJudgeLLM(LLMProvider):
    """Serves ONLY the structured judge call; everything else asserts."""

    def __init__(
        self,
        parsed: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.structured_calls: list[dict[str, Any]] = []
        self._parsed = parsed or {"coverage": "covered", "missing": []}
        self._error = error

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        raise AssertionError("judge must use complete_structured")

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        raise AssertionError("middleware tests never chat")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        self.structured_calls.append(
            {"prompt": prompt, "schema_name": schema_name}
        )
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            parsed=dict(self._parsed), prompt_tokens=7, completion_tokens=3
        )


def make_deps(
    llm: LLMProvider,
    *,
    web_uses: int = 3,
    depth: str = "normal",
    tier: str = "",
    gate: bool = True,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> KernelDeps:
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_SUFFICIENCY_GATE=gate,
    )
    deps = KernelDeps(
        run_id="run_suff_test",
        control=None,  # the middleware path never touches the store
        platform=platform,
        llm=llm,
        model=None,
        reasoning_effort=None,
        timeout=5.0,
    )
    deps.tool_use_counts = {"web": web_uses, "knowledge": 0}
    deps.depth = depth
    deps.tier = tier
    deps.question = "Wie ist die aktuelle Lage?"
    deps.evidence_refs = {
        "ref_1": {
            "label": "W1",
            "url": "https://example.com/a",
            "title": "Quelle A",
            "excerpt": "Aussage A",
        }
    }
    if events is not None:
        deps.event_sink = lambda kind, payload: events.append((kind, payload))
    return deps


@pytest.fixture(autouse=True)
def _clear_deps():
    yield
    set_kernel_deps(None)


def _state(messages: list[Any] | None = None) -> dict[str, Any]:
    return {"messages": list(messages or [])}


def _nudge_message(state_uses: int) -> HumanMessage:
    return HumanMessage(
        content="Interim evidence state: ...",
        additional_kwargs={SUFFICIENCY_NUDGE_FLAG: state_uses},
    )


def test_gate_nudges_when_covered():
    events: list[tuple[str, dict[str, Any]]] = []
    llm = StructuredJudgeLLM({"coverage": "covered", "missing": []})
    deps = make_deps(llm, events=events)
    set_kernel_deps(deps)

    update = KernelSufficiencyMiddleware().before_model(_state(), None)

    assert update is not None
    (message,) = update["messages"]
    assert message.additional_kwargs[SUFFICIENCY_NUDGE_FLAG] == 3
    assert "sufficient" in message.content
    assert "formulate the answer now" in message.content
    assert len(llm.structured_calls) == 1
    judged = [payload for kind, payload in events if kind == JUDGED_EVENT]
    assert judged == [
        {
            "marker": judged[0]["marker"],
            "nudge": True,
            "tool_uses": 3,
            "coverage": "covered",
            "missing": [],
            "open_continuations": 0,
            "open_sources": [],
        }
    ]
    # The judge's resolution is visible on the activity surface...
    assert any(
        kind == "inqtrix.node.model_resolution" for kind, _ in events
    )
    # ...and its tokens are booked into the segment accumulator.
    assert deps.usage == {"prompt_tokens": 7, "completion_tokens": 3}


def test_gate_names_gaps_when_partial():
    llm = StructuredJudgeLLM(
        {"coverage": "partial", "missing": ["Preisstand 2026"]}
    )
    set_kernel_deps(make_deps(llm))

    update = KernelSufficiencyMiddleware().before_model(_state(), None)

    assert update is not None
    (message,) = update["messages"]
    assert "Preisstand 2026" in message.content
    assert "ONLY for these gaps" in message.content


def test_gate_is_free_below_threshold():
    """Simple runs never pay a judge call — the cheapness guarantee."""
    llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(llm, web_uses=2))

    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    assert llm.structured_calls == []

    # Deep raises the threshold: 3 uses stay below the deep minimum.
    deep_llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(deep_llm, web_uses=3, depth="deep"))
    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    assert deep_llm.structured_calls == []


def test_gate_latches_on_state_and_caps_judgements():
    llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(llm, web_uses=3))

    # Same evidence state already judged (a park/resume replay or a
    # turn without new source evidence) -> no second call.
    latched = _state([_nudge_message(3)])
    assert (
        KernelSufficiencyMiddleware().before_model(latched, None) is None
    )
    assert llm.structured_calls == []

    # New evidence beyond the latched state -> judged again...
    set_kernel_deps(make_deps(llm, web_uses=4))
    update = KernelSufficiencyMiddleware().before_model(latched, None)
    assert update is not None
    assert len(llm.structured_calls) == 1

    # ...but never beyond the run-wide cap, whatever the state says.
    capped_llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(capped_llm, web_uses=9))
    capped = _state([_nudge_message(3), _nudge_message(5)])
    assert (
        KernelSufficiencyMiddleware().before_model(capped, None) is None
    )
    assert capped_llm.structured_calls == []


def test_gate_skips_schnell_and_disabled():
    schnell_llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(schnell_llm, tier="schnell"))
    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    assert schnell_llm.structured_calls == []

    off_llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(off_llm, gate=False))
    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    assert off_llm.structured_calls == []


def test_unvalidated_judgement_emits_event_without_nudge(monkeypatch):
    """No-silent-fallback: a reply that never validates stays visible in
    the event stream, and the loop continues unadvised."""
    from inqtrix.agents.kernel import cognition
    from inqtrix.agents.patterns._structured import StructuredOutcome

    events: list[tuple[str, dict[str, Any]]] = []
    llm = StructuredJudgeLLM()
    set_kernel_deps(make_deps(llm, events=events))
    monkeypatch.setattr(
        cognition,
        "judge_kernel_sufficiency",
        lambda deps: StructuredOutcome(
            value=None,
            usage={"prompt_tokens": 2, "completion_tokens": 1},
            marker="prompt_json_invalid",
        ),
    )

    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    judged = [payload for kind, payload in events if kind == JUDGED_EVENT]
    assert judged == [
        {"marker": "prompt_json_invalid", "nudge": False, "tool_uses": 3}
    ]


def test_provider_crash_degrades_loudly_but_nonfatally():
    """The judge only ADVISES — a hard provider failure must not fail
    the run (mirrors the deep-review degradation contract)."""
    events: list[tuple[str, dict[str, Any]]] = []
    llm = StructuredJudgeLLM(error=RuntimeError("Provider explodierte"))
    set_kernel_deps(make_deps(llm, events=events))

    assert KernelSufficiencyMiddleware().before_model(_state(), None) is None
    judged = [payload for kind, payload in events if kind == JUDGED_EVENT]
    assert judged == [{"marker": "error", "nudge": False, "tool_uses": 3}]


def test_checkpoint_rehydration_counts_only_successful_source_calls():
    """Park/resume must not inflate the source-tool counts with FAILED
    calls: the live counter increments only after a successful invoke,
    and failures persist as prefix-marked texts or error-status
    ToolMessages. A drifting count would arm the sufficiency judge over
    thin evidence and burn the schnell web budget on resume."""
    from langchain_core.messages import ToolMessage

    from inqtrix.agents.kernel.algorithm import (
        _checkpointed_tool_use_counts,
    )

    messages = [
        ToolMessage(
            content="Antwort mit Quellenliste [W1]",
            name="web_instant",
            tool_call_id="c1",
        ),
        ToolMessage(
            content=(
                "Tool blocked: The 'schnell' tier allows at most 1 web "
                "search(es) per run — answer from the results you already "
                "have. Mention this restriction in the answer."
            ),
            name="web_instant",
            tool_call_id="c2",
        ),
        ToolMessage(
            content="Tool error (rate_limited): limit reached.",
            name="search_project_knowledge",
            tool_call_id="c3",
        ),
        ToolMessage(
            content="Provider explodierte",
            name="web_instant",
            tool_call_id="c4",
            status="error",
        ),
        ToolMessage(
            content="Treffer aus dem Bestand [K1]",
            name="search_project_knowledge",
            tool_call_id="c5",
        ),
    ]
    snapshot = SimpleNamespace(values={"messages": messages})
    assert _checkpointed_tool_use_counts(snapshot) == {
        "web": 1,
        "knowledge": 1,
    }


def test_kernel_run_judges_after_three_searches():
    """Platform path: after the third successful search the next model
    turn sees the advisory nudge, exactly one judge call is spent, and
    the verdict is on the event stream."""
    from inqtrix.providers.base import ChatTurn, ToolCallRequest
    from tests.agents.test_kernel_prompt_events import (
        ScriptedToolLLM,
        _text_turn,
        make_client,
        wait_status,
    )

    class JudgingToolLLM(ScriptedToolLLM):
        def __init__(self, turns: list[ChatTurn]) -> None:
            super().__init__(turns)
            self.structured_calls: list[str] = []

        def supports_structured_output(
            self, *, model: str | None = None
        ) -> bool:
            return True

        def complete_structured(
            self, prompt: str, **kwargs: Any
        ) -> Any:
            self.structured_calls.append(prompt)
            return SimpleNamespace(
                parsed={"coverage": "covered", "missing": []},
                prompt_tokens=7,
                completion_tokens=3,
            )

    def _web_turn(index: int) -> ChatTurn:
        return ChatTurn(
            text="",
            tool_calls=(
                ToolCallRequest(
                    id=f"call_suff{index}",
                    name="web_instant",
                    arguments={"query": f"Lage Aspekt {index}"},
                ),
            ),
            finish_reason="tool_calls",
            model="high-model",
            prompt_tokens=10,
            completion_tokens=5,
            raw=None,
        )

    llm = JudgingToolLLM(
        [
            _web_turn(1),
            _web_turn(2),
            _web_turn(3),
            _text_turn("Antwort auf Basis der Belege."),
        ]
    )
    client = make_client(llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Wie ist die aktuelle Lage?",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
            },
        ).json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary

        # Exactly ONE judge call: turns 1-3 stay below the threshold.
        assert len(llm.structured_calls) == 1
        assert "Wie ist die aktuelle Lage?" in llm.structured_calls[0]

        # The answer turn SAW the nudge as the newest human message.
        final_messages = llm.chat_calls[3]["messages"]
        nudges = [
            m
            for m in final_messages
            if m.get("role") == "user"
            and "Interim evidence state" in str(m.get("content", ""))
        ]
        assert len(nudges) == 1

        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        judged = [
            event["data"]
            for event in events
            if event["type"] == JUDGED_EVENT
        ]
        assert len(judged) == 1
        assert judged[0]["nudge"] is True
        assert judged[0]["coverage"] == "covered"
        assert judged[0]["tool_uses"] == 3


def test_post_mission_advisory_is_delivered_exactly_once():
    """A returned mission child arms ONE advisory: the memo is the
    deliverable and no second research child verifies it. The flag
    clears on delivery, so the following turn gets nothing."""
    deps = make_deps(StructuredJudgeLLM([]), gate=False, tier="tief")
    deps.post_mission_nudge_pending = True
    set_kernel_deps(deps)
    middleware = KernelSufficiencyMiddleware()

    update = middleware.before_model(_state(), None)

    assert update is not None
    (message,) = update["messages"]
    assert isinstance(message, HumanMessage)
    assert message.additional_kwargs[POST_MISSION_NUDGE_FLAG] is True
    assert "clarify doubtful statements with read_evidence" in message.content
    assert "The final deep check itself starts no new research" in message.content
    assert "short conclusion" in message.content
    assert deps.post_mission_nudge_pending is False
    assert middleware.before_model(_state(), None) is None


def test_post_mission_advisory_precedes_the_sufficiency_gate():
    """With the gate armed AND a mission just returned, the mission
    advisory wins the turn; the gate judges the next one."""
    deps = make_deps(StructuredJudgeLLM([]), gate=True, web_uses=3, tier="tief")
    deps.post_mission_nudge_pending = True
    set_kernel_deps(deps)
    update = KernelSufficiencyMiddleware().before_model(_state(), None)
    assert update is not None
    (message,) = update["messages"]
    assert POST_MISSION_NUDGE_FLAG in message.additional_kwargs
    assert SUFFICIENCY_NUDGE_FLAG not in message.additional_kwargs
