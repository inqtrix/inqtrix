"""Agent-kernel eval: harness contract (offline) + gated live trials.

OFFLINE (default suite): the harness runs scripted German scenarios
through the REAL serving path and the code graders judge them — this
pins the HARNESS CONTRACT (collection, grading, pass^k math), it does
NOT claim model quality. Deterministic by construction.

LIVE (opt-in): ``INQTRIX_EVAL_AGENT_LIVE=1`` plus real provider env
(``uv run --env-file .env``) runs the same scenarios with multiple
trials against the configured providers; the artifact under
``tests/eval/artifacts/`` is the reviewable outcome. Stays out of the
offline default suite by design (cost).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.eval.agent_kernel_harness import (
    KernelScenario,
    grade_trial,
    pass_hat_k,
    run_scenario_trials,
    write_kernel_eval_artifact,
)


class ScriptedToolLLM(LLMProvider):
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.models = ModelSettings(
            reasoning_model="base-model",
            tier_high_model="high-model",
            tier_mid_model="mid-model",
            tier_fast_model="fast-model",
        )
        self._turns = list(turns)

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        raise AssertionError("kernel must use chat()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
        if not self._turns:
            raise AssertionError("scripted provider ran out of turns")
        return self._turns.pop(0)


class OneSourceSearch:
    def is_available(self) -> bool:
        return True

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        return GroundedSearchResult(
            answer=f"Web-Antwort zu {query}",
            sources=[
                GroundedSource(
                    url="https://example.com/a",
                    title="Quelle A",
                    snippet="Auszug.",
                )
            ],
            prompt_tokens=17,
            completion_tokens=9,
        )


def _tool_turn(call_id: str, name: str, arguments: dict) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(id=call_id, name=name, arguments=arguments),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _text_turn(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _client(llm: LLMProvider, search: Any) -> TestClient:
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
    register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=search),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    return TestClient(app)


# -- versioned German scenarios (shared by offline contract and live) ------ #

SCENARIO_CONVERSATION = KernelScenario(
    scenario_id="gespraech_direkt",
    question="Erklaere kurz den Unterschied zwischen Skonto und Rabatt.",
    autonomy="autonomous",
    forbidden_tools=frozenset({"web_instant", "run_web_research"}),
)

SCENARIO_WEB_CITED = KernelScenario(
    scenario_id="web_belegt",
    question="Was aendert sich 2026 bei der E-Rechnung? Belege deine Aussagen.",
    autonomy="autonomous",
    expect_citations=True,
    expected_tools=frozenset({"web_instant"}),
)

SCENARIO_GATED_WEB = KernelScenario(
    scenario_id="standard_gate",
    question="Suche im Web nach dem aktuellen Stand der NIS2-Umsetzung.",
    autonomy="balanced",
    expect_citations=True,
    expected_tools=frozenset({"web_instant"}),
    expect_gate=True,
    expected_gate_tool="web_instant",
)

SCENARIO_WRITE_GATE = KernelScenario(
    scenario_id="write_gate",
    question=(
        "Aktualisiere mein Editor-Dokument doc-1: ersetze 'alt' durch 'neu'."
    ),
    # autonomous, yet propose_editor_patch is ALWAYS gated (a write to the
    # user's document is never autonomous) — this pins that the blanket
    # auto-approver cannot clear a write path undetected: the gate must
    # fire AND name propose_editor_patch.
    autonomy="autonomous",
    expect_gate=True,
    expected_gate_tool="propose_editor_patch",
)

OFFLINE_SCRIPTS: dict[str, Any] = {
    "gespraech_direkt": lambda: _client(
        ScriptedToolLLM(
            [_text_turn("Skonto ist ein Zahlungszielnachlass; Rabatt ein Preisnachlass.")]
        ),
        OneSourceSearch(),
    ),
    "web_belegt": lambda: _client(
        ScriptedToolLLM(
            [
                _tool_turn(
                    "call_ev1", "web_instant", {"query": "E-Rechnung 2026"}
                ),
                _text_turn("Ab 2026 gilt die Empfangspflicht [W1]."),
            ]
        ),
        OneSourceSearch(),
    ),
    "standard_gate": lambda: _client(
        ScriptedToolLLM(
            [
                _tool_turn(
                    "call_ev2", "web_instant", {"query": "NIS2 Stand"}
                ),
                _text_turn("Der Stand ist X [W1]."),
            ]
        ),
        OneSourceSearch(),
    ),
    "write_gate": lambda: _client(
        ScriptedToolLLM(
            [
                _tool_turn(
                    "call_wp1",
                    "propose_editor_patch",
                    {
                        "document_id": "doc-1",
                        "edits": [
                            {
                                "position": "replace",
                                "find": "alt",
                                "text": "neu",
                            }
                        ],
                        "summary": "alt -> neu",
                    },
                ),
                _text_turn(
                    "Ich habe den Patch vorgeschlagen; du pruefst ihn im "
                    "Editor."
                ),
            ]
        ),
        OneSourceSearch(),
    ),
}


def test_pass_hat_k_math() -> None:
    """The reliability estimator, pinned against hand-computed values."""
    assert pass_hat_k(3, 3, 3) == 1.0
    assert pass_hat_k(0, 3, 3) == 0.0
    # 2 successes of 3, k=2: C(2,2)/C(3,2) = 1/3.
    assert pass_hat_k(2, 3, 2) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        pass_hat_k(1, 1, 2)


@pytest.mark.parametrize(
    "scenario",
    [
        SCENARIO_CONVERSATION,
        SCENARIO_WEB_CITED,
        SCENARIO_GATED_WEB,
        SCENARIO_WRITE_GATE,
    ],
    ids=lambda s: s.scenario_id,
)
def test_harness_contract_offline(scenario: KernelScenario) -> None:
    """Scripted trials through the REAL serving path pass every grader.

    Would fail if: the answer artifact stops materializing, citations
    stop resolving (or exceed the cited-only contract), tool routing or
    the balanced gate change, or the harness stops collecting any of
    the graded surfaces — the harness contract, end to end.
    """
    records, summary = run_scenario_trials(
        OFFLINE_SCRIPTS[scenario.scenario_id],
        scenario,
        trials=2,
        k=2,
    )
    assert summary["failures"] == [], [r.failures for r in records]
    assert summary["pass_hat_k"] == 1.0
    assert summary["total_tokens"] > 0


def test_grader_catches_broken_contracts() -> None:
    """Negative control: a defective trial FAILS the graders.

    A grader that cannot fail is decoration — this pins that unresolved
    citations, a missing answer artifact, and a policy breach each
    produce their named failure.
    """
    from tests.eval.agent_kernel_harness import KernelTrialRecord

    record = KernelTrialRecord(
        scenario_id="defekt",
        trial=0,
        status="completed",
        answer="Behauptung [W9].",
        cited_labels=["W9"],
        reference_labels=["W1"],
        artifact_kinds=["evidence_bundle"],
        tools_started=["run_web_research"],
        approvals=[{"payload": {"actions": [{"tool": "web_instant"}]}}],
        gated_tools=["web_instant"],
        premature_tools=["web_instant"],
        narrations=[],
        prompt_tokens=1,
        completion_tokens=1,
        latency_s=0.1,
    )
    scenario = KernelScenario(
        scenario_id="defekt",
        question="x",
        forbidden_tools=frozenset({"run_web_research"}),
        expect_gate=True,
        expected_gate_tool="propose_editor_patch",
    )
    grade_trial(record, scenario)
    assert "unresolved_citations=['W9']" in record.failures
    assert "missing_answer_artifact" in record.failures
    assert "forbidden_tools_used=['run_web_research']" in record.failures
    # The gate was parked but for the WRONG tool, and a gated tool ran
    # before consent — both must surface (count-only grading would miss
    # them: an approval exists).
    assert any(
        failure.startswith("gate_tool_mismatch")
        for failure in record.failures
    )
    assert "tool_ran_before_consent=['web_instant']" in record.failures


LIVE = os.environ.get("INQTRIX_EVAL_AGENT_LIVE") == "1"


@pytest.mark.skipif(
    not LIVE,
    reason=(
        "live agent eval is opt-in: INQTRIX_EVAL_AGENT_LIVE=1 with real "
        "provider env (uv run --env-file .env) — multiple real LLM runs."
    ),
)
def test_agent_kernel_live_eval() -> None:
    """k=3 real trials per scenario against the configured providers."""
    from inqtrix.providers import create_providers
    from inqtrix.settings import Settings as LiveSettings

    settings = LiveSettings()
    providers = create_providers(settings)

    def factory() -> TestClient:
        return _client(providers.llm, providers.search)

    per_scenario: dict[str, dict[str, Any]] = {}
    for scenario in (
        SCENARIO_CONVERSATION,
        SCENARIO_WEB_CITED,
        SCENARIO_GATED_WEB,
        SCENARIO_WRITE_GATE,
    ):
        records, summary = run_scenario_trials(
            factory, scenario, trials=3, k=3
        )
        per_scenario[scenario.scenario_id] = {
            **summary,
            "answers_preview": [r.answer[:200] for r in records],
        }
    path = write_kernel_eval_artifact("agent_kernel_live", per_scenario)
    # Reliability floors as pass^k (ALL k trials pass — the module's own
    # metric, not raw successes). Deterministic contracts (gate identity,
    # consent ordering, answer artifact) must be fully reliable; the
    # citation-bearing web answer, which depends on live model output, is
    # held one notch lower (>= 1/3 = 2-of-3) and tightened via baseline.
    assert per_scenario["gespraech_direkt"]["pass_hat_k"] >= 1.0, path
    assert per_scenario["standard_gate"]["pass_hat_k"] >= 1.0, path
    assert per_scenario["write_gate"]["pass_hat_k"] >= 1.0, path
    assert per_scenario["web_belegt"]["pass_hat_k"] >= 1 / 3, path
