"""Regression tests for graph wiring and node orchestration."""

from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
from typing import Any

import copy
import dataclasses
import re
import pytest

from inqtrix.evidence import STANDING_TEXT
from inqtrix.exceptions import AgentRateLimited, AgentTimeout, AnthropicAPIError
from inqtrix.graph import default_graph_config, run, run_test
from inqtrix.nodes import (
    _join_rendered_sections,
    _build_answer_appendix_sections,
    _build_answer_claim_bindings,
    _build_answer_prompt_diagnostics,
    _claim_extraction_text,
    _expand_bare_evidence_label_links,
    _extract_evidence_labels,
    _build_query_slots,
    _evidence_depth_gap,
    _select_crosscheck_targets,
    _target_query_count_for_round,
    answer,
    apply_confidence_guardrails,
    classify,
    contested_conflict_count,
    evaluate,
    search,
)
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.report_profiles import ReportProfile
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state, track_tokens
from inqtrix.strategies import StrategyContext, create_default_strategies


class _SearchStub:
    def search(self, *a, **kw):
        return GroundedSearchResult()

    def is_available(self):
        return True


class _EvalLLMStub:
    def __init__(self, response: str) -> None:
        self._response = response
        self.models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
        )

    def complete(self, *a, **kw):
        return self._response


    def is_available(self):
        return True


class _DirectLLMStub:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.models = SimpleNamespace(reasoning_model="direct-model")

    def complete_with_metadata(self, prompt: str, **kwargs) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            content="direct answer",
            prompt_tokens=2,
            completion_tokens=3,
            model="direct-model",
        )

    def complete(self, *a, **kw) -> str:
        raise AssertionError("Direct chat must call complete_with_metadata")


    def is_available(self) -> bool:
        return True


class _BudgetDirectLLMStub(_DirectLLMStub):
    def complete_with_metadata(
        self, prompt: str, **kwargs: Any
    ) -> LLMResponse:
        response = super().complete_with_metadata(prompt, **kwargs)
        track_tokens(kwargs["state"], response)
        return response


class _DoneStopCriteria:
    def check_contradictions(self, s, eval_text, conf):
        return conf

    def extract_competing_events(self, s, eval_text, conf):
        return conf

    def extract_evidence_scores(self, s, eval_text, conf):
        return conf

    def check_falsification(self, s, conf, prev_conf):
        return False

    def check_stagnation(self, s, conf, prev_conf, n_citations, falsification_just_triggered):
        return conf, False

    def compute_utility(self, s, conf, prev_conf, n_citations):
        s["done"] = True
        return 0.0, True

    def check_plateau(self, s, conf, prev_conf, stagnation_detected):
        return False

    def should_stop(self, state):
        return False, ""


def test_default_graph_config_uses_answer_node_name():
    settings = AgentSettings()
    providers = ProviderContext(llm=_EvalLLMStub(""), search=_SearchStub())
    strategies = create_default_strategies(settings)

    config = default_graph_config(providers, strategies, settings)

    assert "answer" in config.nodes
    assert "synthesize" not in config.nodes
    routes = {source: router({"done": True}) for source, router in config.conditional_edges}
    assert routes == {
        "classify": "answer",
        "plan": "answer",
        "evaluate": "answer",
    }


def test_run_skip_search_uses_direct_llm_without_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inqtrix.graph as graph_module

    def fail_get_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("skip_search must bypass LangGraph")

    monkeypatch.setattr(graph_module, "get_agent", fail_get_agent)
    settings = AgentSettings(skip_search=True)
    llm = _DirectLLMStub()
    providers = ProviderContext(llm=llm, search=_SearchStub())
    strategies = create_default_strategies(settings)

    result = run(
        "Hallo",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )

    assert result["answer"] == "direct answer"
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert result["result_state"]["_current_node"] == "direct_llm"
    assert result["result_state"]["done"] is True


def test_direct_llm_binds_its_purpose_for_the_trace() -> None:
    """The web-free chat answer must not be an anonymous generation.

    The research nodes get their purpose from the graph loop; the direct
    path bypasses that loop, so its one provider call binds ``direct_chat``
    itself (the node id of its tier assignment). Without the binding a chat
    trace shows ``purpose None`` and cannot be filtered by call site.
    """
    from inqtrix.observability.context import current_purpose

    class _PurposeRecordingLLM(_DirectLLMStub):
        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            purpose = current_purpose()
            self.purpose = purpose.purpose if purpose else None
            return super().complete_with_metadata(prompt, **kwargs)

    settings = AgentSettings(skip_search=True)
    llm = _PurposeRecordingLLM()
    providers = ProviderContext(llm=llm, search=_SearchStub())
    strategies = create_default_strategies(settings)

    run("Hallo", providers=providers, strategies=strategies, settings=settings)

    assert llm.purpose == "direct_chat"
    assert current_purpose() is None, "the binding must not leak past the call"


def test_direct_llm_prompt_leads_with_the_answer_language_directive() -> None:
    """The web-free chat answer follows the question language: the
    directive is the first line of the prompt, phrased in that language.
    Before, the direct chat had no language rule at all."""
    from inqtrix.prompt_language import directive_for

    settings = AgentSettings(skip_search=True)
    llm = _DirectLLMStub()
    providers = ProviderContext(llm=llm, search=_SearchStub())
    strategies = create_default_strategies(settings)

    run(
        "How does the energy balance of a storage system work and what matters?",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )
    assert llm.prompts[0].startswith(directive_for("en") + "\n\n")

    run(
        "Wie funktioniert die Energiebilanz eines Speichers und was ist wichtig?",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )
    assert llm.prompts[1].startswith(directive_for("de") + "\n\n")
    assert "nie als ae/oe/ue/ss" in llm.prompts[1]


def test_direct_llm_answer_escapes_currency_dollars() -> None:
    """Unescaped, "$5 und $10" in a chat answer renders as inline math in
    the report panel. The direct path shares the agent guard."""

    class _CurrencyLLM(_DirectLLMStub):
        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            self.prompts.append(prompt)
            return LLMResponse(
                content="Speicher: 5 GB × $0.023 = **$0.12** pro Monat; Formel $E = P \\cdot t$.",
                prompt_tokens=2,
                completion_tokens=3,
                model="direct-model",
            )

    settings = AgentSettings(skip_search=True)
    providers = ProviderContext(llm=_CurrencyLLM(), search=_SearchStub())
    strategies = create_default_strategies(settings)

    result = run("Was kostet das?", providers=providers, strategies=strategies, settings=settings)

    assert "\\$0.023" in result["answer"] and "\\$0.12" in result["answer"]
    assert "$E = P \\cdot t$" in result["answer"]


def test_direct_llm_enforces_token_budget_after_its_only_model_call() -> None:
    settings = AgentSettings(skip_search=True)
    providers = ProviderContext(
        llm=_BudgetDirectLLMStub(), search=_SearchStub()
    )

    result = run(
        "Hallo",
        providers=providers,
        strategies=create_default_strategies(settings),
        settings=settings,
        token_budget=1,
    )

    assert result["answer"] == ""
    assert result["usage"] == {"prompt_tokens": 2, "completion_tokens": 3}
    assert result["result_state"]["cancelled"] is True
    assert result["result_state"]["cancel_reason"] == "token_budget_exceeded"


def test_research_terminal_boundary_preserves_final_usage_on_budget_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inqtrix.graph as graph_module

    class FinalUsageAgent:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            return {
                **state,
                "answer": "finished answer",
                "total_prompt_tokens": 7,
                "total_completion_tokens": 5,
            }

    monkeypatch.setattr(
        graph_module,
        "get_agent",
        lambda *_args, **_kwargs: FinalUsageAgent(),
    )
    settings = AgentSettings()
    providers = ProviderContext(llm=_EvalLLMStub(""), search=_SearchStub())

    result = run(
        "Hallo",
        providers=providers,
        strategies=create_default_strategies(settings),
        settings=settings,
        token_budget=1,
    )

    assert result["answer"] == ""
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 5}
    assert result["result_state"]["cancel_reason"] == "token_budget_exceeded"


def test_default_graph_config_emits_native_node_events(monkeypatch):
    import inqtrix.nodes as nodes_module

    def fake_classify(state, *, providers, strategies, settings):
        state["done"] = True
        return state

    monkeypatch.setattr(nodes_module, "classify", fake_classify)
    settings = AgentSettings(max_rounds=4)
    providers = ProviderContext(llm=_EvalLLMStub(""), search=_SearchStub())
    strategies = create_default_strategies(settings)
    events = []
    state = initial_state(
        "Frage?",
        max_rounds=settings.max_rounds,
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    config = default_graph_config(providers, strategies, settings)
    result = config.nodes["classify"](state)

    assert result["_current_node"] == "classify"
    assert [event_type for event_type, _payload in events] == [
        "inqtrix.node.started",
        "inqtrix.node.finished",
    ]
    assert events[0][1]["snapshot"]["current_node"] == "classify"


def test_explicit_recency_survives_classification_fallback() -> None:
    """A provider failure cannot replace a delegated freshness contract."""

    class _FailingClassifyLLM(_EvalLLMStub):
        def complete(self, *args, **kwargs):
            raise AgentTimeout("classification unavailable")

    settings = AgentSettings(testing_mode=True)
    providers = ProviderContext(
        llm=_FailingClassifyLLM(""),
        search=_SearchStub(),
    )
    state = initial_state(
        "Welche Entwicklungen gab es?",
        web_recency="year",
    )

    classify(
        state,
        providers=providers,
        strategies=create_default_strategies(settings),
        settings=settings,
    )

    assert state["recency"] == "year"
    assert state["iteration_logs"][-1]["_classify_fallback"] is True


def test_run_test_exports_public_sources_and_claims(monkeypatch):
    import inqtrix.graph as graph_module

    class _StubAgent:
        def invoke(self, state):
            return {
                "answer": "Antwort",
                "round": 1,
                "queries": ["q1"],
                "all_citations": ["https://www.bmwk.bund.de/dokumente/x"],
                "consolidated_claims": [
                    {
                        "claim_text": "Ein verifizierter Fakt",
                        "status": "verified",
                        "claim_type": "fact",
                        "needs_primary": True,
                        "status_reason": "primaer belegt",
                        "support_count": 2,
                        "contradict_count": 0,
                        "source_tier_counts": {
                            "primary": 1,
                            "mainstream": 0,
                            "stakeholder": 0,
                            "unknown": 0,
                            "low": 0,
                        },
                        "source_urls": ["https://www.bmwk.bund.de/dokumente/x"],
                    }
                ],
                "iteration_logs": [{"node": "answer"}],
                "final_confidence": 8,
                "source_tier_counts": {
                    "primary": 1,
                    "mainstream": 0,
                    "stakeholder": 0,
                    "unknown": 0,
                    "low": 0,
                },
                "source_quality_score": 1.0,
                "claim_status_counts": {"verified": 1, "contested": 0, "unverified": 0},
                "claim_quality_score": 1.0,
            }

    monkeypatch.setattr(
        graph_module,
        "get_agent",
        lambda providers, strategies, settings: _StubAgent(),
    )

    settings = AgentSettings()
    providers = ProviderContext(llm=_EvalLLMStub(""), search=_SearchStub())
    strategies = create_default_strategies(settings)

    result = run_test(
        "Was ist passiert?",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )

    assert result["top_sources"][0]["tier"] == "primary"
    assert result["top_claims"][0]["needs_primary"] is True
    assert result["top_claims"][0]["support_count"] == 2


def test_evaluate_min_rounds_suppresses_early_stop():
    """min_rounds prevents an early confidence-driven stop.

    The stub stop criteria immediately set ``done=True`` (utility_stop) and
    the eval LLM returns CONFIDENCE: 9 which exceeds confidence_stop=8.
    With min_rounds=2 and the agent currently at round 1, both early-stop
    triggers must be suppressed and the loop must continue.
    """
    settings = AgentSettings(confidence_stop=8, max_rounds=4, min_rounds=2)
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["round"] = 1  # below min_rounds (=2)
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/report"]

    provider_context = ProviderContext(
        llm=_EvalLLMStub(
            "STATUS: SUFFICIENT\n"
            "CONFIDENCE: 9\n"
            "GAPS: Keine\n"
            "CONTRADICTIONS: Nein\n"
            "IRRELEVANT: Keine\n"
            "COMPETING_EVENTS: Keine\n"
            "EVIDENCE_CONSISTENCY: 8\n"
            "EVIDENCE_SUFFICIENCY: 8"
        ),
        search=_SearchStub(),
    )

    evaluate(state, providers=provider_context,
             strategies=strategies, settings=settings)

    assert state["done"] is False, "min_rounds must suppress early stop"
    messages = []
    while not progress_queue.empty():
        messages.append(progress_queue.get()[1])
    assert any("min_rounds=2" in m for m in messages), (
        f"Expected a min_rounds progress hint, got: {messages}"
    )


def test_evaluate_min_rounds_does_not_override_max_rounds():
    """min_rounds must never extend the loop beyond max_rounds.

    Misconfiguration ``min_rounds > max_rounds`` is bounded by the user's
    explicit hard cap.
    """
    settings = AgentSettings(confidence_stop=8, max_rounds=2, min_rounds=5)
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["round"] = 2  # = max_rounds
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/report"]

    provider_context = ProviderContext(
        llm=_EvalLLMStub(
            "STATUS: SUFFICIENT\n"
            "CONFIDENCE: 9\n"
            "GAPS: Keine\n"
            "CONTRADICTIONS: Nein\n"
            "IRRELEVANT: Keine\n"
            "COMPETING_EVENTS: Keine\n"
            "EVIDENCE_CONSISTENCY: 8\n"
            "EVIDENCE_SUFFICIENCY: 8"
        ),
        search=_SearchStub(),
    )

    evaluate(state, providers=provider_context,
             strategies=strategies, settings=settings)

    assert state["done"] is True, (
        "max_rounds must always win over min_rounds; loop must terminate"
    )


def test_evaluate_min_rounds_default_one_preserves_legacy_behaviour():
    """Default min_rounds=1 keeps the stop semantics without a round floor."""
    settings = AgentSettings(confidence_stop=8, max_rounds=4)  # min_rounds default = 1
    assert settings.min_rounds == 1
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["round"] = 1  # >= min_rounds (=1) so stop is allowed
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/report"]

    provider_context = ProviderContext(
        llm=_EvalLLMStub(
            "STATUS: SUFFICIENT\n"
            "CONFIDENCE: 9\n"
            "GAPS: Keine\n"
            "CONTRADICTIONS: Nein\n"
            "IRRELEVANT: Keine\n"
            "COMPETING_EVENTS: Keine\n"
            "EVIDENCE_CONSISTENCY: 8\n"
            "EVIDENCE_SUFFICIENCY: 8"
        ),
        search=_SearchStub(),
    )

    evaluate(state, providers=provider_context,
             strategies=strategies, settings=settings)

    assert state["done"] is True


def test_stop_is_suppressed_when_report_evidence_is_too_thin():
    settings = AgentSettings(confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True)
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["all_citations"] = ["https://example.com/report"]
    # Only one report-eligible record -- below ``min_report_eligible_evidence``.
    state["evidence_ledger"] = [
        {
            "evidence_id": "ev_1",
            "report_eligible": True,
            "claims": [],
            "tier": "mainstream",
            "source_snippet": "One useful source is available.",
            "source_passages": [{"passage_id": "passage_1", "text": "One useful source is available."}],
            "citation_set": [{"url": "https://example.com/report"}],
        }
    ]

    provider_context = ProviderContext(
        llm=_EvalLLMStub(
            "STATUS: SUFFICIENT\n"
            "CONFIDENCE: 9\n"
            "GAPS: Keine\n"
            "CONTRADICTIONS: Nein\n"
            "IRRELEVANT: Keine\n"
            "COMPETING_EVENTS: Keine\n"
            "EVIDENCE_CONSISTENCY: 8\n"
            "EVIDENCE_SUFFICIENCY: 8"
        ),
        search=_SearchStub(),
    )

    evaluate(state, providers=provider_context, strategies=strategies, settings=settings)

    assert state["done"] is False
    stop_cascade = state["iteration_logs"][-1]["stop_cascade"]
    assert stop_cascade["suppressed_by_report_evidence"] is True
    assert stop_cascade["report_eligible_evidence_count"] == 1


def test_evaluate_emits_completion_when_stop_criteria_already_finished():
    settings = AgentSettings(confidence_stop=8, max_rounds=4)
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/report"]

    provider_context = ProviderContext(
        llm=_EvalLLMStub(
            "STATUS: INSUFFICIENT\n"
            "CONFIDENCE: 6\n"
            "GAPS: Keine\n"
            "CONTRADICTIONS: Nein\n"
            "IRRELEVANT: Keine\n"
            "COMPETING_EVENTS: Keine\n"
            "EVIDENCE_CONSISTENCY: 8\n"
            "EVIDENCE_SUFFICIENCY: 8"
        ),
        search=_SearchStub(),
    )

    evaluate(
        state,
        providers=provider_context,
        strategies=strategies,
        settings=settings,
    )

    messages = []
    while not progress_queue.empty():
        messages.append(progress_queue.get()[1])

    assert messages[-1].startswith("Recherche abgeschlossen")


def test_search_emits_fallback_progress_messages():
    class _SearchWithNotice:
        def __init__(self) -> None:
            self._notice = None

        def search(self, *a, **kw):
            self._notice = "search fallback"
            return GroundedSearchResult(
                answer="Gefundener Text",
                sources=[GroundedSource(url="https://example.com/report", rank=1)],
            )

        def consume_nonfatal_notice(self):
            notice = self._notice
            self._notice = None
            return notice

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self._notice = None
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *a, **kw):
            return ""

        def consume_nonfatal_notice(self):
            notice = self._notice
            self._notice = None
            return notice

        def is_available(self):
            return True

    class _ClaimExtractionWithNotice:
        def __init__(self) -> None:
            self._notice = None

        def extract(self, *a, **kw):
            self._notice = "claim fallback"
            return ([], 0, 0)

        def consume_nonfatal_notice(self):
            notice = self._notice
            self._notice = None
            return notice

    settings = AgentSettings(first_round_queries=1, max_rounds=4)
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_ClaimExtractionWithNotice(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["queries"] = ["q1"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_SearchWithNotice()),
        strategies=strategies,
        settings=settings,
    )

    messages = []
    while not progress_queue.empty():
        messages.append(progress_queue.get()[1])

    assert any("Suchanfragen fehlgeschlagen" in msg for msg in messages)
    assert any("Claim-Extraktionen fehlgeschlagen" in msg for msg in messages)
    assert not any("Zusammenfassungen" in msg for msg in messages)


def test_search_emits_claim_binding_issues_only_when_claims_are_unbound():
    class _SearchWithSource:
        def search(self, *a, **kw):
            return GroundedSearchResult(
                answer="Foo berichtet etwas.",
                sources=[
                    GroundedSource(url="https://a.example/r", title="A", snippet="s", rank=1)
                ],
            )

        def is_available(self):
            return True

    class _MetadataClaimExtraction:
        def __init__(self, metadata):
            self._metadata = metadata

        def extract(self, *a, **kw):
            return ([], 0, 0)

        def consume_nonfatal_notice(self):
            return None

        def consume_extraction_metadata(self):
            return dict(self._metadata)

    class _LLM:
        def __init__(self):
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *a, **kw):
            return ""

        def is_available(self):
            return True

    def _run(metadata):
        settings = AgentSettings(first_round_queries=1, max_rounds=4)
        llm = _LLM()
        defaults = create_default_strategies(
            settings, llm=llm, claim_extract_model="claim-extract-model"
        )
        strategies = StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=_MetadataClaimExtraction(metadata),
            claim_consolidation=defaults.claim_consolidation,
            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        )
        progress_queue = Queue()
        state = initial_state(
            "Was ist passiert?", progress_queue=progress_queue, max_total_seconds=30
        )
        state["queries"] = ["q1"]
        search(
            state,
            providers=ProviderContext(llm=llm, search=_SearchWithSource()),
            strategies=strategies,
            settings=settings,
        )
        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get()[1])
        return messages

    fired = _run(
        {
            "claim_extraction_mode": "structured_output",
            "unbound_claim_count": 2,
            "unknown_provider_ref_count": 1,
        }
    )
    assert any("ohne belegte Quelle" in msg for msg in fired)

    clean = _run(
        {
            "claim_extraction_mode": "structured_output",
            "unbound_claim_count": 0,
            "unknown_provider_ref_count": 0,
        }
    )
    assert not any("ohne belegte Quelle" in msg for msg in clean)


def test_search_builds_evidence_without_semantic_grouping_events():
    class _SearchWithCitation:
        def search(self, *a, **kw):
            return GroundedSearchResult(
                answer="Reuters berichtet, dass Energiepreise steigen.",
                sources=[
                    GroundedSource(
                        url="https://www.reuters.com/business/energy-report",
                        title="Energy report",
                        snippet="Reuters berichtet, dass Energiepreise steigen.",
                        date="2026-05-14",
                        rank=1,
                    )
                ],
            )

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *a, **kw):
            return ""

        def complete_with_metadata(self, *a, **kw):
            raise AssertionError("search() must not run semantic grouping")

        def is_available(self):
            return True

    class _ClaimExtractionWithClaim:
        def extract(self, *a, **kw):
            return (
                [
                    {
                        "claim_text": "Reuters berichtet, dass Energiepreise steigen.",
                        "claim_type": "fact",
                        "needs_primary": False,
                        "source_urls": ["https://www.reuters.com/business/energy-report"],
                        "citation_set": [
                            {
                                "url": "https://www.reuters.com/business/energy-report",
                                "role": "source",
                            }
                        ],
                    }
                ],
                0,
                0,
            )

    settings = AgentSettings(
        first_round_queries=1,
        max_rounds=4,
        observability_profile="forensic",
        testing_mode=True,
    )
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["queries"] = ["q1"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_SearchWithCitation()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=_ClaimExtractionWithClaim(),
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    search_log = state["iteration_logs"][-1]
    assert state["evidence_ledger"]
    assert "semantic_evidence_grouping" not in state
    assert "semantic_evidence_grouping" not in search_log
    assert "_semantic_evidence_grouping_fallback" not in search_log
    assert not any(
        log.get("event") == "semantic_evidence_grouping"
        for log in state.get("iteration_logs", [])
    )


def test_search_ingests_source_only_result_without_answer():
    """sources without a synthesized answer must still enter the ledger.

    GroundedSearchResult.sources is independent of .answer, so a provider may
    return citable sources with no prose. Those become source-context evidence
    records (no claims) and their URLs reach all_citations -- an answer-only
    gate would drop them.
    """
    class _SourceOnlySearch:
        def search(self, *a, **kw):
            return GroundedSearchResult(
                answer="",
                sources=[
                    GroundedSource(
                        url="https://investor.example/q1-report",
                        title="Quartalsbericht",
                        snippet="",
                        rank=1,
                    )
                ],
            )

        def is_available(self):
            return True

    class _UnusedLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *a, **kw):
            return ""

        def is_available(self):
            return True

    settings = AgentSettings(
        first_round_queries=1,
        max_rounds=4,
        observability_profile="summary",
        testing_mode=True,
    )
    llm = _UnusedLLM()
    defaults = create_default_strategies(
        settings, llm=llm, claim_extract_model="claim-extract-model"
    )
    state = initial_state("Welche Quartalszahlen?", max_total_seconds=30)
    state["queries"] = ["q1"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_SourceOnlySearch()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert state["all_citations"], "source URL must reach all_citations"
    ledger = state["evidence_ledger"]
    assert ledger, "source-only result must produce a source-context record"
    record = ledger[0]
    assert record["report_eligible"] is True
    assert record["canonical_url"] in state["all_citations"]
    assert record["claims"] == []


def test_run_emits_progress_before_rate_limit_abort(monkeypatch):
    import inqtrix.graph as graph_module

    class _StubAgent:
        def invoke(self, state):
            raise AgentRateLimited("demo-model", RuntimeError("429"))

    monkeypatch.setattr(
        graph_module,
        "get_agent",
        lambda providers, strategies, settings: _StubAgent(),
    )

    progress_queue = Queue()
    settings = AgentSettings()
    providers = ProviderContext(llm=_EvalLLMStub(""), search=_SearchStub())
    strategies = create_default_strategies(settings)

    result = run(
        "Was ist passiert?",
        progress_queue=progress_queue,
        providers=providers,
        strategies=strategies,
        settings=settings,
    )

    messages = []
    while not progress_queue.empty():
        messages.append(progress_queue.get()[1])

    assert any(msg.startswith("Recherche abgebrochen:") for msg in messages)
    assert result["result_state"]["_terminal_failure"] == {
        "type": "rate_limited",
        "message": "Rate limit reached for model 'demo-model': 429",
    }


def test_answer_catches_anthropic_api_error_and_falls_back():
    """AnthropicAPIError in answer node must trigger context fallback, not crash.

    Also asserts that the fallback is **never silent**:
    - The visible warning header is present at the top of the answer.
    - The iteration log carries ``_answer_fallback=True`` plus a
      structured ``_answer_fallback_kind`` and ``_answer_fallback_reason``.
    - A progress message was emitted on the queue so terminal/UI consumers
      see the degradation in real time.
    """

    class _FailingLLM:
        def __init__(self):
            self.models = SimpleNamespace(
                reasoning_model="claude-sonnet-4-6",
                effective_evaluate_model="claude-sonnet-4-6",
            )

        def complete(self, *a, **kw):
            raise AnthropicAPIError(
                model="claude-sonnet-4-6",
                status_code=529,
                error_type="overloaded_error",
                message="Overloaded",
            )


        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    progress_queue = Queue()
    state = initial_state("Was ist passiert?",
                          progress_queue=progress_queue, max_total_seconds=30)
    state["context"] = ["Recherche-Kontext: Wichtige Informationen"]
    state["all_citations"] = ["https://example.com/report"]
    state["consolidated_claims"] = []

    answer(
        state,
        providers=ProviderContext(llm=_FailingLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert "Recherche-Kontext" in state["answer"]
    assert "[!WARNING] Antwort-Synthese-Fallback aktiv" in state["answer"]
    assert "_answer_fallback=true" in state["answer"]

    answer_log = next(
        entry for entry in reversed(state["iteration_logs"])
        if entry.get("node") == "answer"
    )
    assert answer_log["_answer_fallback"] is True
    assert answer_log["_answer_fallback_kind"] == "no_fallback_model"
    assert "AnthropicAPIError" in answer_log["_answer_fallback_reason"]

    progress_messages: list[str] = []
    while not progress_queue.empty():
        item = progress_queue.get_nowait()
        if isinstance(item, tuple) and len(item) == 2:
            progress_messages.append(str(item[1]))
        elif isinstance(item, str):
            progress_messages.append(item)
        elif isinstance(item, dict):
            progress_messages.append(str(item.get("text", "") or item))
    assert any(
        "kein Fallback-Modell konfiguriert" in m
        or "no fallback model configured" in m
        for m in progress_messages
    ), f"Progress queue must surface the answer fallback: {progress_messages}"


def test_answer_node_escapes_currency_dollars_like_the_agent_answers():
    """Unescaped, a research answer with "$5" in prose renders as inline
    math in the report panel; the composed answer passes the same currency
    guard as the agent answers."""

    class _CurrencyLLM:
        def __init__(self):
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete(self, *a, **kw):
            return "Speicher: 5 GB × $0.023 = **$0.12** pro Monat; Formel $E = P t$."

        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    state = initial_state("Was kostet das?", max_total_seconds=30)
    state["context"] = ["Recherche-Kontext: Preise"]
    state["all_citations"] = ["https://example.com/report"]
    state["consolidated_claims"] = []

    answer(
        state,
        providers=ProviderContext(llm=_CurrencyLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,
            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert "\\$0.023" in state["answer"] and "\\$0.12" in state["answer"]
    assert "$E = P t$" in state["answer"]


def test_answer_timeout_emits_visible_warning_and_marker():
    """AgentTimeout in answer node must be a non-silent fallback.

    A timeout path that wrote a short German fallback string into
    ``state["answer"]`` without setting ``_answer_fallback``, without
    ``emit_progress``, and without a structured iteration-log reason
    would be silent. This regression guards against that path.
    """

    class _TimingOutLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete(self, *a, **kw):
            raise AgentTimeout("LLM call timed out during answer composition")


        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    progress_queue = Queue()
    state = initial_state(
        "Welche aktuellen Entwicklungen gibt es?",
        progress_queue=progress_queue,
        max_total_seconds=30,
    )
    state["context"] = ["Recherche-Kontext: Findings 1, 2, 3"]
    state["all_citations"] = ["https://example.com/report"]
    state["consolidated_claims"] = []

    answer(
        state,
        providers=ProviderContext(llm=_TimingOutLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert "[!WARNING] Antwort-Synthese-Fallback aktiv" in state["answer"]
    assert "Recherche-Kontext" in state["answer"]
    assert "_answer_fallback=true" in state["answer"]

    answer_log = next(
        entry for entry in reversed(state["iteration_logs"])
        if entry.get("node") == "answer"
    )
    assert answer_log["_answer_fallback"] is True
    assert answer_log["_answer_fallback_kind"] == "timeout"
    assert "Timeout" in answer_log["_answer_fallback_reason"]

    progress_messages: list[str] = []
    while not progress_queue.empty():
        item = progress_queue.get_nowait()
        if isinstance(item, tuple) and len(item) == 2:
            progress_messages.append(str(item[1]))
    assert any(
        "Antwort-Synthese-Timeout" in m or "Answer synthesis timeout" in m
        for m in progress_messages
    ), f"Timeout fallback must surface on progress queue: {progress_messages}"


def _ledger_record(
    evidence_id: str,
    url: str,
    *,
    title: str = "Source report",
    claims: list[dict] | None = None,
) -> dict:
    """Build a minimal report-eligible EvidenceRecord for answer-node tests."""
    return {
        "evidence_id": evidence_id,
        "record_type": "source",
        "report_eligible": True,
        "query_id": f"qry_{evidence_id}",
        "query": "test query",
        "canonical_url": url,
        "source_title": title,
        "source_date": "2026-05-10",
        "tier": "mainstream",
        "source_snippet": f"{title} snippet with substance.",
        "source_passages": [
            {"passage_id": f"passage_{evidence_id}", "origin": "source_snippet",
             "text": f"{title} passage with concrete detail."}
        ],
        "citation_set": [{"label": "E1.1", "url": url, "role": "source", "title": title}],
        "claims": claims or [],
    }


def test_answer_records_algorithm_failure_when_evidence_overview_renders_no_records():
    class _LLMShouldNotRun:
        models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
        )

        def complete_with_metadata(self, *args, **kwargs):
            raise AssertionError("blocking evidence overview failure should skip synthesis")

        def complete(self, *args, **kwargs):
            raise AssertionError("blocking evidence overview failure should skip fallback completion")

        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="forensic",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["final_confidence"] = 8
    record = _ledger_record("ev_too_large", "https://example.com/too-large")
    state["evidence_ledger"] = [record]
    state["query_synthesis"] = {
        record["query_id"]: {
            "query": "test query",
            "round": 0,
            # Larger than the profile's whole evidence budget, so the
            # single group cannot render at any count.
            "provider_answer": "A"
            * (settings.report_tuning.prompt_evidence_total_char_budget + 10000),
            "citation_urls_by_rank": {"1": record["canonical_url"]},
        }
    }

    answer(
        state,
        providers=ProviderContext(llm=_LLMShouldNotRun(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    failures = state.get("algorithm_failures", [])
    assert failures
    assert failures[-1]["reason"] == "no_rendered_evidence_records"
    assert failures[-1]["blocking"] is True
    assert state["rendered_evidence_record_count"] == 0
    assert state["answer_finish_reason"] == "algorithm_failure"


def test_answer_composes_sections_with_full_body_citation_pool():
    class _SectionedLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )
            self._sections = [
                "Direkte Antwort mit Quelle [1](https://source1.example/report).",
                "- Punkt eins [2](https://source2.example/report)\n- Punkt zwei [3](https://source3.example/report)",
                "### Kosten\n\nDetailanalyse mit Zahl [4](https://source4.example/report).\n\n### Wirkung\n\nWeitere Einordnung [5](https://source5.example/report).",
                "Ausblick mit Einordnung [6](https://source6.example/report).",
            ]

        def complete_with_metadata(self, *a, **kw):
            finish_reason = "length" if not self.calls else "stop"
            self.calls.append(
                {
                    "prompt": a[0],
                    "system": kw.get("system", ""),
                    "max_output_tokens": kw.get("max_output_tokens"),
                    "timeout": kw.get("timeout"),
                }
            )
            return LLMResponse(
                content=self._sections[len(self.calls) - 1],
                prompt_tokens=50,
                completion_tokens=120,
                model="reasoning-model",
                finish_reason=finish_reason,
                raw={"choices": [{"finish_reason": finish_reason}]},
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise metadata completions")


        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        reasoning_timeout=321,
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    llm = _SectionedLLM()
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 7
    urls = [f"https://source{i}.example/report" for i in range(1, 13)]
    state["evidence_ledger"] = [
        _ledger_record(f"ev_{i}", url, title=f"Source {i}")
        for i, url in enumerate(urls, start=1)
    ]
    state["all_citations"] = list(urls)

    answer(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert len(llm.calls) == 4
    assert all(call["timeout"] == 321 for call in llm.calls)
    assert "## Kurzfazit" in state["answer"]
    assert "## Kernaussagen" in state["answer"]
    assert "## Detailanalyse" in state["answer"]
    assert "## Einordnung / Ausblick" in state["answer"]
    assert "## Referenzen" in state["answer"]
    # Headings carry the structure: the only rule left is the one before
    # the closing stats line.
    assert state["answer"].count("\n---") == 1
    assert "\n---\n*" in state["answer"]
    assert state["answer_incomplete"] is False
    assert state["total_prompt_tokens"] == 200
    assert state["total_completion_tokens"] == 480
    assert state["iteration_logs"][-1]["allowed_citation_count"] == 12
    assert state["iteration_logs"][-1]["section_logs"][0]["limit_hit"] is True
    assert state["iteration_logs"][-1]["section_logs"][0]["accepted_with_limit"] is True
    rendered_record_count = state["iteration_logs"][-1][
        "rendered_evidence_record_count"
    ]
    rendered_record_label = (
        "Beleg" if rendered_record_count == 1 else "Belege"
    )
    assert (
        f"{rendered_record_count} {rendered_record_label} für den Bericht bereitgestellt"
        in state["answer"]
    )
    assert "Evidence-Prompt" not in state["answer"]
    omitted_record_count = state["iteration_logs"][-1][
        "omitted_evidence_record_count"
    ]
    if omitted_record_count:
        omitted_record_label = (
            "Evidenzbeleg" if omitted_record_count == 1 else "Evidenzbelege"
        )
        assert (
            f"{omitted_record_count} {omitted_record_label} wegen Budget ausgelassen"
            in state["answer"]
        )
        assert "Quellen wegen Budget ausgelassen" not in state["answer"]


def test_section_progress_counts_in_write_order_and_types_the_position():
    """The reader sees sections counted as they are written.

    The regression: a ``write_last`` section (Kurzfazit) is written after the
    body but was announced with its DISPLAY index, so the progress counted
    "2/2" before "1/2". The count is the write position; the snapshot carries
    it as typed fields so a parent never parses the message.
    """
    class _SectionedLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete_with_metadata(self, *a, **kw):
            self.calls += 1
            return LLMResponse(
                content=f"Abschnitt {self.calls} [1](https://source1.example/report).",
                prompt_tokens=10,
                completion_tokens=20,
                model="reasoning-model",
                finish_reason="stop",
                raw={"choices": [{"finish_reason": "stop"}]},
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise metadata completions")

        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 7
    urls = [f"https://source{i}.example/report" for i in range(1, 5)]
    state["evidence_ledger"] = [
        _ledger_record(f"ev_{i}", url, title=f"Source {i}")
        for i, url in enumerate(urls, start=1)
    ]
    state["all_citations"] = list(urls)
    events: list[tuple[str, dict]] = []
    state["_run_event_sink"] = lambda kind, payload: events.append((kind, payload))

    answer(
        state,
        providers=ProviderContext(llm=_SectionedLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,
            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    # The announcement of each section; other progress written while a
    # section is on (its snapshot names that section too) is not counted.
    section_events = [
        payload
        for kind, payload in events
        if kind == "inqtrix.progress.message"
        and str(payload.get("message") or "").startswith("Schreibe Report-Abschnitt")
    ]
    total = len(settings.report_tuning.answer_sections)
    assert [event["snapshot"]["writing_section"] for event in section_events] == list(
        range(1, total + 1)
    )
    assert {event["snapshot"]["sections_total"] for event in section_events} == {total}
    # The message counts the same way: k-th written of n, never the display slot.
    counts = [
        re.search(r"(\d+)/(\d+)", event["message"]).groups() for event in section_events
    ]
    assert counts == [(str(k), str(total)) for k in range(1, total + 1)]
    # The write_last section is written — and announced — last.
    write_last = [spec for spec in settings.report_tuning.answer_sections if spec.write_last]
    assert write_last, "COMPACT must keep a write_last section for this regression"
    assert write_last[0].heading("de") in section_events[-1]["message"]
    # After the writer the snapshot names no section any more.
    from inqtrix.state import build_run_snapshot

    assert "writing_section" not in build_run_snapshot(state)
    assert "sections_total" not in build_run_snapshot(state)


def test_answer_preserves_source_context_and_references_without_bundles():
    class _SourceContextLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete_with_metadata(self, *a, **kw):
            self.calls.append({"system": kw.get("system", "")})
            return LLMResponse(
                content="Quellenbasierte Synthese [1](https://source.example/report).",
                prompt_tokens=10,
                completion_tokens=20,
                model="reasoning-model",
                finish_reason="stop",
                raw={"choices": [{"finish_reason": "stop"}]},
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise metadata completions")


        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    llm = _SourceContextLLM()
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 8
    state["evidence_ledger"] = [
        {
            "evidence_id": "ev_1",
            "record_type": "source",
            "claims": [],
            "report_eligible": True,
            "source_title": "Source report",
            "source_date": "2026-05-10",
            "tier": "mainstream",
            "query_id": "qry_src",
            "query": "source query",
            "canonical_url": "https://source.example/report",
            "source_snippet": "The source snippet remains available when claims are empty.",
            "source_passages": [
                {
                    "passage_id": "passage_1",
                    "origin": "source_snippet",
                    "text": "The source snippet remains available when claims are empty.",
                }
            ],
            "citation_set": [
                {
                    "label": "E1.1",
                    "url": "https://source.example/report",
                    "role": "source",
                    "title": "Source report",
                }
            ],
        }
    ]
    state["query_synthesis"] = {
        "qry_src": {
            "query": "source query",
            "round": 1,
            "provider_answer": "Summary from Evidence-Ledger view.",
            "related_questions": [],
        }
    }
    state["all_citations"] = ["https://source.example/report"]
    state["consolidated_claims"] = []

    answer(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    assert llm.calls
    assert "Summary from Evidence-Ledger view" in llm.calls[0]["system"]
    assert f"Standing: {STANDING_TEXT['source-context']}" in llm.calls[0]["system"]
    assert "EVIDENCE OVERVIEW" in llm.calls[0]["system"]
    assert "[1](https://source.example/report)" in state["answer"]
    assert "## Referenzen" in state["answer"]
    assert "Evidence-Contract: source_context_only" in state["answer"]
    # source_context_only (no verified/contested claim backs the answer) is the
    # weakest non-failure evidence state and is capped at 4 via
    # _CONTRACT_CONFIDENCE_CAP -- below needs_review (6), above algorithm_failed (3).
    assert state["final_confidence"] == 4
    assert state["iteration_logs"][-1]["allowed_citation_count"] == 1
    assert state["iteration_logs"][-1]["removed_non_allowed_links"] == 0
    assert state["iteration_logs"][-1]["reference_link_count"] == 1
    assert state["iteration_logs"][-1]["evidence_contract_status"] == "source_context_only"
    assert state["iteration_logs"][-1]["rendered_evidence_record_count"] == 1
    assert state["allowed_citations"] == ["https://source.example/report"]
    assert state["report_references"] == [
        {
            "label": "1",
            "url": "https://source.example/report",
            "tier": "mainstream",
            "cited": True,
            # The ledger knew the page title -> the record carries it.
            "title": "Source report",
        }
    ]
    assert state["visible_evidence_labels"] == ["E1"]
    assert state["evidence_label_urls"] == {"E1": "https://source.example/report"}
    assert state["rendered_evidence_ids"] == ["ev_1"]


def test_answer_prompt_diagnostics_counts_claimless_sources():
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["all_citations"] = ["https://example.com/a"]
    state["evidence_ledger"] = [
        {"evidence_id": "ev_1", "report_eligible": True, "claims": []},
        {"evidence_id": "ev_2", "report_eligible": True, "claims": [{"claim_text": "Claim"}]},
    ]
    state["consolidated_claims"] = [{"claim_id": "claim_1", "status": "verified"}]
    state_data = {
        "evidence_overview": "RESEARCH RESULT R1\n[E1] Source",
        "allowed_citations": ["https://example.com/a"],
        "rendered_evidence_record_count": 2,
        "omitted_evidence_record_count": 0,
        "visible_evidence_label_count": 1,
    }

    diagnostics = _build_answer_prompt_diagnostics(state_data, state)

    assert diagnostics["evidence_record_count"] == 2
    assert diagnostics["report_eligible_evidence_count"] == 2
    assert diagnostics["claimless_evidence_count"] == 1
    assert diagnostics["rendered_evidence_record_count"] == 2
    assert diagnostics["evidence_overview_chars"] == len("RESEARCH RESULT R1\n[E1] Source")
    assert diagnostics["visible_evidence_label_count"] == 1
    assert diagnostics["allowed_citation_count"] == 1
    assert diagnostics["consolidated_claim_count"] == 1


def test_claim_extraction_text_keeps_provider_answer_before_complete_snippet():
    provider_snippet = (
        "This provider snippet must remain complete in the projection. " * 24
    ) + "UNIQUE_SNIPPET_END"
    citation_record = {
        "rank": 2,
        "canonical_url": "https://example.com/source",
        "title": "Example Source",
        "source_date": "2026-05-24",
        "snippet": provider_snippet,
    }
    text = _claim_extraction_text(
        "Provider answer with inline refs [2].",
        [citation_record],
    )

    assert (
        "[2] https://example.com/source | title: Example Source | date: 2026-05-24"
        in text
    )
    assert "PROVIDER_SNIPPET[2]:" in text
    assert provider_snippet in text
    assert "UNIQUE_SNIPPET_END" in text
    assert "Coherent, web-grounded provider answer:" in text
    assert "Provider answer with inline refs [2]." in text
    assert text.index("Provider answer with inline refs [2].") < text.index(
        "Source metadata returned by the web search provider:"
    )
    assert "claim_prompt_provider_snippet_omitted_chars" not in citation_record


def test_bare_evidence_labels_expand_to_markdown_links():
    label_urls = {
        "E1": "https://example.com/source",
        "E2": "https://example.com/context",
    }

    expanded, count = _expand_bare_evidence_label_links(
        "Aussage [E1], Kontext [E2], bereits verlinkt [E1](https://example.com/source).",
        label_urls,
    )

    assert count == 2
    assert "[E1](https://example.com/source)" in expanded
    assert "[E2](https://example.com/context)" in expanded
    assert expanded.count("[E1](https://example.com/source)") == 2
    assert _extract_evidence_labels(expanded) == {"E1", "E2"}


def test_adjacent_evidence_labels_expand_with_spacing():
    label_urls = {
        "E1": "https://example.com/source-a",
        "E23": "https://example.com/source-b",
    }

    expanded, count = _expand_bare_evidence_label_links(
        "Aussage [E1][E23].",
        label_urls,
    )

    assert count == 2
    assert "[E1](https://example.com/source-a) [E23](https://example.com/source-b)" in expanded


def test_unmapped_bare_evidence_labels_are_visible():
    expanded, count = _expand_bare_evidence_label_links("Aussage [E99].", {})

    assert count == 0
    assert "[E99: nicht zugeordnet]" in expanded


def test_answer_allows_only_canonical_prompt_evidence_citations_in_body():
    class _BroadCitationLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete_with_metadata(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                content = (
                    "Report-Beleg [1](https://report.example/story), "
                    "verifizierter Claim [2](https://verified.example/story), "
                    "unverified Kontext [3](https://unverified.example/story) und "
                    "Rohquelle [4](https://raw.example/story)."
                )
            else:
                content = "Weitere Einordnung."
            return LLMResponse(
                content=content,
                prompt_tokens=10,
                completion_tokens=20,
                model="reasoning-model",
                finish_reason="stop",
                raw={"choices": [{"finish_reason": "stop"}]},
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise metadata completions")


        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT,
        answer_prompt_citations_max=10,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 8
    # Only report.example and verified.example are in the EvidenceLedger, so
    # only their URLs are in the citation allowlist. unverified.example and
    # raw.example are NOT in the ledger and must be stripped from the body.
    state["evidence_ledger"] = [
        _ledger_record("ev_report", "https://report.example/story", title="Report"),
        _ledger_record("ev_verified", "https://verified.example/story", title="Verified"),
    ]
    state["all_citations"] = [
        "https://report.example/story",
        "https://verified.example/story",
        "https://unverified.example/story",
        "https://raw.example/story",
    ]
    state["consolidated_claims"] = []

    answer(
        state,
        providers=ProviderContext(llm=_BroadCitationLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    answer_log = state["iteration_logs"][-1]
    assert answer_log["allowed_citation_count"] == 2
    assert answer_log["removed_non_allowed_links"] == 2
    assert answer_log["allowed_link_count"] == 2


def test_answer_omits_truncated_section_and_marks_incomplete_without_recovery():
    """Truncated sections are repaired and included; loop continues through all sections."""

    class _TruncatedLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_evaluate_model="evaluate-model",
            )

        def complete_with_metadata(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="Kurzer Befund mit Primaerquelle [1](https://source1.example/report).",
                    prompt_tokens=80,
                    completion_tokens=150,
                    model="reasoning-model",
                    finish_reason="stop",
                    raw={"choices": [{"finish_reason": "stop"}]},
                )
            if self.calls == 2:
                # Truncated section with unbalanced bold — will be repaired
                return LLMResponse(
                    content="Der Kontext zeigt, dass die **Kass",
                    prompt_tokens=80,
                    completion_tokens=200,
                    model="reasoning-model",
                    finish_reason="length",
                    raw={"choices": [{"finish_reason": "length"}]},
                )
            # Remaining sections complete normally
            return LLMResponse(
                content=f"Abschnitt {self.calls} mit Inhalt.",
                prompt_tokens=80,
                completion_tokens=100,
                model="reasoning-model",
                finish_reason="stop",
                raw={"choices": [{"finish_reason": "stop"}]},
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() must not fall back to a plain recovery completion")


        def is_available(self):
            return True

    settings = AgentSettings(report_profile=ReportProfile.DEEP, testing_mode=True)
    defaults = create_default_strategies(settings)
    llm = _TruncatedLLM()
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 7
    urls = [f"https://source{i}.example/report" for i in range(1, 26)]
    state["evidence_ledger"] = [
        _ledger_record(f"ev_{i}", url, title=f"Source {i}")
        for i, url in enumerate(urls, start=1)
    ]
    state["all_citations"] = list(urls)

    answer(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    # All 6 DEEP sections were attempted (loop never aborted)
    assert llm.calls == 6
    # First section included normally
    assert "## Executive Summary" in state["answer"]
    # Truncated section was repaired and included (not dropped)
    assert "## Hintergrund / Kontext" in state["answer"]
    assert "die **Kass**" in state["answer"]  # repaired: closing ** added
    # No Synthese-Status stub
    assert "## Synthese-Status" not in state["answer"]
    # Remaining sections were generated
    assert "## Analyse" in state["answer"]
    assert "## Perspektiven / Positionen" in state["answer"]
    assert "## Risiken / Unsicherheiten" in state["answer"]
    assert "## Fazit / Ausblick" in state["answer"]
    # Every rendered online evidence source stays on record (cited first,
    # then the examined-but-uncited ones), but only the cited sources are
    # listed as references; the rest is named as examined. No separate
    # additional-links section is needed for already-listed sources.
    assert "## Referenzen" in state["answer"]
    assert "## Weitere geprüfte Quellen" in state["answer"]
    assert "## Weiterfuehrende Links" not in state["answer"]
    assert state["iteration_logs"][-1]["allowed_citation_count"] == 25
    assert len(state["report_references"]) == 25
    assert [reference["url"] for reference in state["report_references"]] == urls
    # Only source1 is cited by the sections; the other 24 were examined.
    assert [reference["cited"] for reference in state["report_references"]] == (
        [True] + [False] * 24
    )
    # Section 2 shows limit_hit in logs
    section_log_2 = state["iteration_logs"][-1]["section_logs"][1]
    assert section_log_2["limit_hit"] is True
    assert section_log_2["finish_reason"] == "length"
    # Log WARNING was emitted for the truncated section (visible in caplog)
    assert state["iteration_logs"][-1]["reference_link_count"] == 25
    assert state["iteration_logs"][-1]["additional_link_count"] == 0


def test_reference_extraction_runs_when_answer_complete():
    """Markdown-linked references must be extracted even when the answer
    has no ``incomplete_reasons`` entries."""
    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    answer_text = (
        "Ein Verweis auf [1](https://example.com/report/a) und "
        "ein zweiter auf [2](https://example.com/report/b)."
    )
    allowed_citations = [
        "https://example.com/report/a",
        "https://example.com/report/b",
        "https://example.com/extra/c",
    ]

    appendix = _build_answer_appendix_sections(
        answer_text,
        allowed_citations=allowed_citations,
        label_urls={
            "E1": "https://example.com/report/a",
            "E2": "https://example.com/report/b",
            "E3": "https://example.com/extra/c",
        },
        strategies=strategies,
        incomplete_reasons=[],
        finish_reason="stop",
        language="de",
    )
    sections = appendix.sections

    # No Hinweis-zur-Vollständigkeit block because incomplete_reasons is empty
    assert all("Hinweis zur Vollständigkeit" not in section for section in sections)
    # But the referenced URLs must still be extracted
    assert len(appendix.references) == 3
    joined = "\n".join(sections)
    assert "https://example.com/report/a" in joined
    assert "https://example.com/report/b" in joined
    assert "https://example.com/extra/c" in joined
    assert "## Weiterfuehrende Links" not in joined
    assert [reference["url"] for reference in appendix.references] == allowed_citations
    # The reference list names only what the body cites; the source the
    # writer saw but did not cite is listed as examined, never as a
    # reference (two blind readers took the padded list for an overstated
    # evidence base).
    references_section = next(s for s in sections if s.startswith("## Referenzen"))
    examined_section = next(s for s in sections if s.startswith("## Weitere geprüfte Quellen"))
    assert "https://example.com/extra/c" not in references_section
    assert "https://example.com/extra/c" in examined_section
    assert "https://example.com/report/a" not in examined_section
    assert "Im Text nicht zitiert" in examined_section
    assert [reference["cited"] for reference in appendix.references] == [True, True, False]


def test_an_answer_that_cites_nothing_has_no_references_only_examined_sources():
    """An incomplete answer without citations must not promote the whole
    allowlist to references: the reference list says so, and every visible
    source is listed as examined with ``cited: False``."""
    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    appendix = _build_answer_appendix_sections(
        "Der Text bricht ab.",
        allowed_citations=["https://example.com/report/a", "https://example.com/report/b"],
        label_urls={"E1": "https://example.com/report/a", "E2": "https://example.com/report/b"},
        strategies=strategies,
        incomplete_reasons=["Provider-Stop"],
        finish_reason="length",
        language="de",
    )
    sections = appendix.sections
    references_section = next(s for s in sections if s.startswith("## Referenzen"))
    assert "Keine zitatgebundenen Markdown-Links" in references_section
    assert "https://example.com/report/a" not in references_section
    examined_section = next(s for s in sections if s.startswith("## Weitere geprüfte Quellen"))
    assert "https://example.com/report/a" in examined_section
    assert "https://example.com/report/b" in examined_section
    assert [reference["cited"] for reference in appendix.references] == [False, False]


def test_reference_appendix_uses_lowest_label_for_duplicate_urls():
    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )

    appendix = _build_answer_appendix_sections(
        "",
        allowed_citations=["https://example.com/shared"],
        label_urls={
            "E7": "https://example.com/shared",
            "E2": "https://example.com/shared",
        },
        strategies=strategies,
        incomplete_reasons=[],
        finish_reason="stop",
        language="de",
    )

    joined = "\n".join(appendix.sections)
    assert len(appendix.references) == 1
    assert appendix.references[0]["label"] == "E2"
    assert "- [E2](https://example.com/shared)" in joined
    assert "- [E7](https://example.com/shared)" not in joined


class TestBuildAnswerClaimBindings:
    """Coverage for the body-only, plausibility-based answer/claim binder."""

    def _claim(
        self,
        claim_id: str,
        text: str,
        url: str,
        *,
        signature: str | None = None,
        status: str = "verified",
    ) -> dict:
        return {
            "claim_id": claim_id,
            "claim_text": text,
            "signature": signature or text.lower(),
            "status": status,
            "source_urls": [url],
        }

    def _citation_record(self, citation_id: str, url: str) -> dict:
        return {
            "citation_id": citation_id,
            "canonical_url": url,
            "url": url,
            "rank": 1,
            "origin": "search_result",
            "provider": "Stub",
        }

    def test_appendix_links_do_not_produce_bindings(self):
        url = "https://example.com/study"
        body = (
            "Der Energiepreis stieg um 12 Prozent im Mai 2025 laut "
            f"[Studie]({url}). Die Erhebung berichtet ueber Energiepreise."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_1",
                    "Der Energiepreis stieg um 12 Prozent im Mai 2025.",
                    url,
                    signature="energiepreis stieg 12 prozent",
                ),
            ],
            allowed_citations=[url],
            provider_citation_records=[self._citation_record("cit_1", url)],
        )
        assert any(b["binding_status"] == "matched" for b in bindings)

        appendix_url = "https://example.com/appendix"
        bindings_no_appendix = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_2",
                    "Anhang behandelt Methodik der Erhebung.",
                    appendix_url,
                    signature="anhang methodik erhebung",
                ),
            ],
            allowed_citations=[url, appendix_url],
            provider_citation_records=[
                self._citation_record("cit_1", url),
                self._citation_record("cit_2", appendix_url),
            ],
        )
        assert all(
            b["citation_url"] != appendix_url for b in bindings_no_appendix
        ), "appendix-only URLs must not appear when body does not link to them"

    def test_single_claim_url_produces_matched_binding_with_citation_id(self):
        url = "https://example.com/report"
        body = (
            "Die Studie zeigt einen Anstieg der Energiepreise um zwoelf Prozent "
            f"im Mai 2025 [Quelle]({url})."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_a",
                    "Die Studie dokumentiert einen Anstieg der Energiepreise um zwoelf Prozent.",
                    url,
                    signature="anstieg energiepreise zwoelf prozent",
                ),
            ],
            allowed_citations=[url],
            provider_citation_records=[self._citation_record("cit_42", url)],
        )

        matched = [b for b in bindings if b["binding_status"] == "matched"]
        assert len(matched) == 1
        assert matched[0]["claim_id"] == "clm_a"
        assert matched[0]["citation_id"] == "cit_42"
        assert matched[0]["source_id"]

    def test_multi_claim_url_only_plausible_match_is_matched(self):
        url = "https://example.com/multi"
        body = (
            "Die Energiepreise stiegen laut Studie deutlich um zwoelf Prozent "
            f"im Mai 2025 [1]({url})."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_match",
                    "Energiepreise stiegen um zwoelf Prozent im Mai 2025.",
                    url,
                    signature="energiepreise stiegen zwoelf prozent mai",
                ),
                self._claim(
                    "clm_unrelated",
                    "Wassertemperatur der Nordsee erreichte Rekordniveau.",
                    url,
                    signature="wassertemperatur nordsee rekordniveau",
                ),
            ],
            allowed_citations=[url],
            provider_citation_records=[self._citation_record("cit_99", url)],
        )

        matched = [b for b in bindings if b["binding_status"] == "matched"]
        source_only = [b for b in bindings if b["binding_status"] == "source_only_binding"]

        assert [b["claim_id"] for b in matched] == ["clm_match"]
        assert source_only == [], (
            "When at least one related claim matches, no source_only_binding "
            "should be emitted for that segment+url"
        )

    def test_url_with_no_plausible_claim_yields_source_only_binding(self):
        url = "https://example.com/lonely"
        body = (
            "Die Inflation lag bei 2,3 Prozent im April 2025 laut "
            f"Bundesbank [Quelle]({url})."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_off_topic",
                    "Wassertemperatur der Nordsee erreichte Rekordniveau.",
                    url,
                    signature="wassertemperatur nordsee rekordniveau",
                ),
            ],
            allowed_citations=[url],
            provider_citation_records=[self._citation_record("cit_77", url)],
        )

        statuses = [b["binding_status"] for b in bindings]
        assert "matched" not in statuses
        assert statuses.count("source_only_binding") == 1
        source_only = next(
            b for b in bindings if b["binding_status"] == "source_only_binding"
        )
        assert source_only["citation_id"] == "cit_77"
        assert source_only["claim_id"] == ""

    def test_citation_without_claim_status_for_url_not_in_ledger(self):
        url = "https://example.com/orphan"
        body = (
            "Eine Vorbemerkung zum Berichtsumfang ohne Claim-Bezug "
            f"[ref]({url})."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[],
            allowed_citations=[url],
            provider_citation_records=[self._citation_record("cit_orph", url)],
        )

        assert len(bindings) == 1
        assert bindings[0]["binding_status"] == "citation_without_claim"
        assert bindings[0]["citation_id"] == "cit_orph"
        assert bindings[0]["claim_id"] == ""

    def test_legacy_call_without_citation_id_uses_empty_string(self):
        url = "https://example.com/legacy"
        body = (
            f"Energiepreise stiegen um zwoelf Prozent im Mai 2025 [a]({url})."
        )
        bindings = _build_answer_claim_bindings(
            body,
            consolidated_claims=[
                self._claim(
                    "clm_legacy",
                    "Energiepreise stiegen um zwoelf Prozent im Mai 2025.",
                    url,
                    signature="energiepreise stiegen zwoelf prozent mai",
                ),
            ],
            allowed_citations=[url],
            provider_citation_records=[],
        )

        assert len(bindings) == 1
        assert bindings[0]["binding_status"] == "matched"
        assert bindings[0]["citation_id"] == ""


class TestApplyConfidenceGuardrails:

    def _args(self, **overrides):
        base = {
            "has_citations": True,
            "has_evidence_records": True,
            "has_claims": True,
            "has_report_bundles": True,
            "primary_n": 2,
            "mainstream_n": 1,
            "low_n": 0,
            "uncovered_aspects": [],
            "contested_conflicts": 0,
            "needs_primary": False,
            "existing_gap": "",
        }
        base.update(overrides)
        return base

    def test_no_citations_clamps_and_sets_gap(self):
        result = apply_confidence_guardrails(
            9, **self._args(has_citations=False))
        assert result.confidence == 6
        assert result.gap_suggestion == "Keine belastbaren Quellen gefunden."
        assert any("no_citations" in r for r in result.reasons)

    def test_low_quality_majority_caps_at_seven(self):
        result = apply_confidence_guardrails(
            9, **self._args(primary_n=0, mainstream_n=1, low_n=3))
        assert result.confidence == 7
        assert any("low_quality_majority" in r for r in result.reasons)

    def test_needs_primary_caps_at_eight_whatever_the_citation_pool_holds(self):
        """The flag is the claims' own state (flagged, none verified); a
        primary source elsewhere in the citations verifies none of them."""
        for primary_n in (0, 2):
            result = apply_confidence_guardrails(
                9, **self._args(primary_n=primary_n, mainstream_n=2, needs_primary=True))
            assert result.confidence == 8
            # No language given resolves to English, never silently German.
            assert result.gap_suggestion == (
                "Key figures and rules are not backed by a primary source."
            )
        german = apply_confidence_guardrails(
            9,
            **self._args(primary_n=0, mainstream_n=2, needs_primary=True),
            language="de",
        )
        assert german.gap_suggestion == (
            "Zentrale Zahlen/Regelungen nicht mit Primärquelle belegt."
        )

    def test_uncovered_aspects_caps_at_eight(self):
        result = apply_confidence_guardrails(
            10, **self._args(uncovered_aspects=["Rechtslage", "Kosten"]))
        assert result.confidence == 8
        assert "Rechtslage" in (result.gap_suggestion or "")

    def test_two_conflicts_cap_at_seven(self):
        result = apply_confidence_guardrails(
            9, **self._args(contested_conflicts=2))
        assert result.confidence == 7
        assert "umstritten" in (result.gap_suggestion or "")

    def test_one_value_conflict_is_one_conflict_not_two(self):
        """A value conflict contests both of its sides; the guardrail counts
        it once, as it counts a negated twin once."""
        one_value_conflict = [
            {"signature": "artikel 50 anwendbar", "status": "contested"},
            {"signature": "artikel 50 anwendbar", "status": "contested"},
            {"signature": "frist endet", "status": "verified"},
        ]
        assert contested_conflict_count(one_value_conflict) == 1
        result = apply_confidence_guardrails(
            9, **self._args(contested_conflicts=contested_conflict_count(one_value_conflict)))
        assert result.confidence == 9
        two_conflicts = [
            *one_value_conflict,
            {"signature": "mehrheit vorschlag", "status": "contested"},
        ]
        assert contested_conflict_count(two_conflicts) == 2

    def test_citations_without_structured_claims_caps_at_five(self):
        result = apply_confidence_guardrails(
            8,
            **self._args(
                has_evidence_records=True,
                has_claims=False,
                has_report_bundles=False,
            ),
        )

        assert result.confidence == 5
        assert "evidence pipeline" in (result.gap_suggestion or "")
        assert any("no_structured_evidence" in r for r in result.reasons)

    def test_existing_gap_is_preserved(self):
        """When state already has a gap, no suggestion should overwrite it."""
        result = apply_confidence_guardrails(
            10, **self._args(
                has_citations=False,
                uncovered_aspects=["Kosten"],
                existing_gap="Vorher belegter Gap-Text",
            ))
        assert result.gap_suggestion is None

    def test_guardrails_stable_under_multiple_clamps(self):
        """Running the function twice on its own output must be a no-op."""
        first = apply_confidence_guardrails(
            10, **self._args(
                primary_n=0,
                mainstream_n=0,
                low_n=5,
                contested_conflicts=3,
                uncovered_aspects=["A"],
                needs_primary=True,
            ))
        second = apply_confidence_guardrails(
            first.confidence, **self._args(
                primary_n=0,
                mainstream_n=0,
                low_n=5,
                contested_conflicts=3,
                uncovered_aspects=["A"],
                needs_primary=True,
                existing_gap=first.gap_suggestion or "",
            ))
        assert second.confidence == first.confidence
        # Second invocation must not emit a new gap suggestion because the
        # first invocation already chose one (or there wasn't one to emit).
        assert second.gap_suggestion is None


class _RecordingLLMStub:
    """LLM stub that captures every prompt passed to ``complete``."""

    def __init__(self, response: str = "[\"q1\", \"q2\"]") -> None:
        self._response = response
        self.recorded_prompts: list[str] = []
        self.models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
            effective_claim_extract_model="claim-extract-model",
            effective_classify_model="classify-model",
            effective_plan_model="plan-model",
        )

    def complete(self, prompt, *a, **kw):
        self.recorded_prompts.append(prompt)
        return self._response


    def is_available(self):
        return True


def _stub_strategies(settings: AgentSettings) -> StrategyContext:
    """Build a strategy bundle with a no-op stop_criteria for evaluate tests."""
    defaults = create_default_strategies(settings)
    return StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=_DoneStopCriteria(),
    )


def _eval_response(
    *,
    confidence: int,
    contradictions: str = "Nein",
    competing_events: str = "Keine",
) -> str:
    return (
        "STATUS: SUFFICIENT\n"
        f"CONFIDENCE: {confidence}\n"
        "GAPS: Keine\n"
        f"CONTRADICTIONS: {contradictions}\n"
        "IRRELEVANT: Keine\n"
        f"COMPETING_EVENTS: {competing_events}\n"
        "EVIDENCE_CONSISTENCY: 8\n"
        "EVIDENCE_SUFFICIENCY: 8"
    )


# --------------------------------------------------------------------------- #
# Confidence stability via previous-round context
# --------------------------------------------------------------------------- #


def test_evaluate_prompt_omits_previous_round_context_in_round_zero():
    settings = AgentSettings(confidence_stop=8, max_rounds=4, min_rounds=1)
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 0
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]

    llm = _RecordingLLMStub(_eval_response(confidence=4))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    assert llm.recorded_prompts, "Evaluate must call complete()"
    assert "PREVIOUS ROUND CONTEXT" not in llm.recorded_prompts[0], (
        "Round 0 must not include the previous-round block."
    )


def test_evaluate_prompt_includes_previous_round_context_from_round_one():
    settings = AgentSettings(confidence_stop=8, max_rounds=4, min_rounds=1)
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["context"] = ["Kontextblock 1", "Kontextblock 2"]
    state["all_citations"] = ["https://example.com/a", "https://example.com/b"]
    state["final_confidence"] = 5
    state["gaps"] = "Zahlen fehlen"
    state["prev_citation_count"] = 1

    llm = _RecordingLLMStub(_eval_response(confidence=4))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    prompt = llm.recorded_prompts[0]
    assert "PREVIOUS ROUND CONTEXT" in prompt
    assert "PREVIOUS ROUND (round 0): CONFIDENCE=5" in prompt
    assert "GAPS=\"Zahlen fehlen\"" in prompt
    assert "1 new sources" in prompt


def test_evaluate_prompt_includes_evidence_depth_gap():
    settings = AgentSettings(confidence_stop=8, max_rounds=4, min_rounds=1)
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["all_citations"] = ["https://example.com/a"]
    # Several supported claims (verified and attributed alike), all
    # single-source quality-source -> the evidence-depth gap fires (no
    # cross-checked claims).
    state["consolidated_claims"] = [
        {
            "claim_id": f"claim_{idx}",
            "claim_text": f"Model X reached {idx} percent on Benchmark Y.",
            "status": "verified" if idx == 0 else "attributed",
            "verification_basis": "quality_source",
            "citation_set": [{"url": f"https://example.com/{idx}"}],
        }
        for idx in range(3)
    ]
    state["claim_status_counts"] = {"verified": 1, "attributed": 2, "contested": 0, "unverified": 0}

    llm = _RecordingLLMStub(_eval_response(confidence=7))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    prompt = llm.recorded_prompts[0]
    assert "EVIDENCE DEPTH GAP" in prompt
    # The block names the counts it read (a renamed key must not print 0).
    assert "- Supported claims: 3, independently confirmed claims: 0, single-source: 3." in prompt
    # The claim ledger names both supported statuses and what they mean.
    assert "- verified=1, attributed=2, contested=0, unverified=0." in prompt
    assert "Attributed claims suffice for an answer that attributes them" in prompt
    assert state["evidence_depth_gap"]["active"] is True
    # The depth gap is model-facing prompt input, so it reads English.
    assert state["gaps"].startswith("Report evidence")


def test_evaluate_logs_unjustified_drop_marker_when_no_new_contradictions():
    settings = AgentSettings(
        confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True,
    )
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]
    state["final_confidence"] = 7
    state["competing_events"] = ""
    state["prev_competing_events"] = ""

    llm = _RecordingLLMStub(_eval_response(
        confidence=4,
        contradictions="Nein",
        competing_events="Keine",
    ))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    eval_logs = [
        entry for entry in state["iteration_logs"] if entry.get("node") == "evaluate"
    ]
    assert eval_logs, "Evaluate must produce an iteration log entry"
    assert eval_logs[-1]["confidence_unjustified_drop"] is True


def test_evaluate_clamps_an_out_of_range_confidence_to_ten():
    """An evaluator answering outside 0-10 must not reach the metrics raw.

    Nothing downstream enforces the range: every guardrail is a conditional
    min() cap and ResearchMetrics.confidence carries no field constraint. An
    unclamped "CONFIDENCE: 42" satisfies conf >= confidence_stop on the spot,
    so the run ends immediately and the report carries a fabricated number.
    """
    settings = AgentSettings(
        confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True,
    )
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]

    llm = _RecordingLLMStub(_eval_response(confidence=42))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    assert state["final_confidence"] == 10
    eval_logs = [e for e in state["iteration_logs"] if e.get("node") == "evaluate"]
    assert eval_logs, "Evaluate must produce an iteration log entry"
    # The clamp is recorded, not silent: a run that hit it must be
    # distinguishable from one the evaluator genuinely scored 10.
    assert eval_logs[-1]["confidence_clamped"] is True
    assert eval_logs[-1]["confidence"] == 10


def test_evaluate_leaves_an_in_range_confidence_untouched():
    """The clamp must not fire on ordinary values, or the marker is noise."""
    settings = AgentSettings(
        confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True,
    )
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]

    llm = _RecordingLLMStub(_eval_response(confidence=7))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    eval_logs = [e for e in state["iteration_logs"] if e.get("node") == "evaluate"]
    assert eval_logs[-1]["confidence_clamped"] is False


def test_evaluate_does_not_flag_drop_when_competing_events_changed():
    settings = AgentSettings(
        confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True,
    )
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]
    state["final_confidence"] = 7
    state["competing_events"] = "Event A vs Event B"
    state["prev_competing_events"] = "Event A"

    llm = _RecordingLLMStub(_eval_response(
        confidence=4,
        contradictions="Nein",
        competing_events="Event A vs Event B",
    ))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    eval_logs = [
        entry for entry in state["iteration_logs"] if entry.get("node") == "evaluate"
    ]
    assert eval_logs[-1]["confidence_unjustified_drop"] is False


def test_evaluate_does_not_flag_drop_when_contradictions_present():
    settings = AgentSettings(
        confidence_stop=8, max_rounds=4, min_rounds=1, testing_mode=True,
    )
    strategies = _stub_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["context"] = ["Kontextblock"]
    state["all_citations"] = ["https://example.com/a"]
    state["final_confidence"] = 7
    state["competing_events"] = ""
    state["prev_competing_events"] = ""

    llm = _RecordingLLMStub(_eval_response(
        confidence=4,
        contradictions="Ja, Quelle X widerspricht Quelle Y bei den Zahlen",
        competing_events="Keine",
    ))
    providers = ProviderContext(llm=llm, search=_SearchStub())

    evaluate(state, providers=providers, strategies=strategies, settings=settings)

    eval_logs = [
        entry for entry in state["iteration_logs"] if entry.get("node") == "evaluate"
    ]
    assert eval_logs[-1]["confidence_unjustified_drop"] is False


# --------------------------------------------------------------------------- #
# Plan-prompt erzeugt Queries als Fragen
# --------------------------------------------------------------------------- #


def _plan_state_round_zero() -> dict:
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 0
    return state


def test_target_query_count_later_rounds_match_plan_and_search_width():
    compact = AgentSettings(first_round_queries=6)
    deep = AgentSettings(first_round_queries=10, report_profile=ReportProfile.DEEP)

    assert _target_query_count_for_round(0, compact) == 6
    assert _target_query_count_for_round(1, compact) == 6
    assert _target_query_count_for_round(1, deep) == 8


def test_build_query_slots_returns_one_slot_per_later_round_query():
    settings = AgentSettings(first_round_queries=6)
    state = initial_state("Welche KI-News sind wichtig?", max_total_seconds=30)
    state["round"] = 1
    state["gaps"] = "Arbeitsmarktdaten fehlen"
    evidence_depth_gap = {"active": True, "reason": "majority_single_source_bundles"}

    slots = _build_query_slots(
        state,
        target_count=_target_query_count_for_round(state["round"], settings),
        crosscheck_targets=[
            {
                "claim_id": "claim_1",
                "claim_text": "AI layoffs rose by 18 percent in May.",
                "source_domains": ["fortune.com"],
                "verification_basis": "quality_source",
            }
        ],
        evidence_depth_gap=evidence_depth_gap,
    )

    assert len(slots) == 6
    assert slots[0]["slot_type"] == "gap"
    assert any(slot["slot_type"] == "crosscheck" for slot in slots)
    assert any(slot["slot_type"] == "primary_source" for slot in slots)


def test_plan_logs_query_slots_for_later_round():
    from inqtrix.nodes import plan

    settings = AgentSettings(first_round_queries=6, testing_mode=True)
    defaults = create_default_strategies(settings)
    llm = _RecordingLLMStub(
        "["
        "\"Welche Primaerquelle bestaetigt Claim A?\","
        "\"Welche unabhaengige Quelle prueft Claim A?\","
        "\"Welche Daten verifizieren Claim A?\","
        "\"Welche Gegenargumente gibt es zu Claim A?\","
        "\"Welche Marktfolgen hat Claim A?\","
        "\"Welche Regulierung betrifft Claim A?\""
        "]"
    )
    providers = ProviderContext(llm=llm, search=_SearchStub())

    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["gaps"] = "Primaerquelle fehlt"
    state["consolidated_claims"] = [
        {
            "claim_id": "claim_1",
            "claim_text": "Model X reached 87 percent on Benchmark Y.",
            "status": "verified",
            "verification_basis": "quality_source",
            "needs_primary": True,
            "support_count": 1,
            "independent_support_count": 1,
            "source_urls": ["https://analysis-alpha.example/report"],
        }
    ]
    state["report_evidence_bundles"] = [
        {
            "bundle_id": "bundle_1",
            "claim_id": "claim_1",
            "claim_text": "Model X reached 87 percent on Benchmark Y.",
            "verification_status": "verified",
            "verification_basis": "quality_source",
            "citation_set": ["https://analysis-alpha.example/report"],
        }
    ]

    plan(state, providers=providers, strategies=defaults, settings=settings)

    prompt = llm.recorded_prompts[0]
    plan_log = state["iteration_logs"][-1]
    assert "RESEARCH SLOTS" in prompt
    assert plan_log["query_slot_count"] == 6
    assert len(plan_log["query_slots"]) == 6
    assert plan_log["crosscheck_target_count"] == 1
    assert "crosscheck" in plan_log["query_slot_types"]


def test_search_later_round_executes_six_queries():
    class _CountingSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query, *a, **kw):
            self.queries.append(query)
            return GroundedSearchResult()

        def is_available(self):
            return True

    settings = AgentSettings(first_round_queries=6, testing_mode=True)
    llm = _RecordingLLMStub()
    defaults = create_default_strategies(settings)
    search_provider = _CountingSearch()
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = [f"q{i}" for i in range(7)]

    search(
        state,
        providers=ProviderContext(llm=llm, search=search_provider),
        strategies=defaults,
        settings=settings,
    )

    assert len(search_provider.queries) == 6
    assert state["search_offset"] == 6
    assert state["iteration_logs"][-1]["queries_executed"] == 6
    assert state["iteration_logs"][-1]["target_query_count"] == 6


def test_plan_prompt_contains_question_form_instruction():
    from inqtrix.nodes import plan

    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    llm = _RecordingLLMStub("[\"Welche neuen Studien sind erschienen?\"]")
    providers = ProviderContext(llm=llm, search=_SearchStub())

    state = _plan_state_round_zero()
    plan(state, providers=providers, strategies=defaults, settings=settings)

    assert llm.recorded_prompts, "plan() must call complete()"
    prompt = llm.recorded_prompts[0]
    assert "COMPLETE QUESTION" in prompt
    assert "Which breakthroughs in large language models" in prompt
    assert "keyword chain, too vague" in prompt


def test_plan_prompt_falsification_block_remains_unchanged():
    from inqtrix.nodes import plan

    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    llm = _RecordingLLMStub("[\"Was wurde widerlegt?\"]")
    providers = ProviderContext(llm=llm, search=_SearchStub())

    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 2
    state["falsification_triggered"] = True

    plan(state, providers=providers, strategies=defaults, settings=settings)

    prompt = llm.recorded_prompts[0]
    assert "FALSIFICATION MODE ACTIVE" in prompt
    assert "debunked" in prompt
    assert "hoax" in prompt
    assert "COMPLETE QUESTION" in prompt


def test_plan_prompt_includes_crosscheck_targets_after_search_round():
    from inqtrix.nodes import plan

    settings = AgentSettings(testing_mode=True)
    defaults = create_default_strategies(settings)
    llm = _RecordingLLMStub("[\"Welche Primaerquelle bestaetigt Benchmark Y?\"]")
    providers = ProviderContext(llm=llm, search=_SearchStub())

    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["consolidated_claims"] = [
        {
            "claim_id": "claim_1",
            "claim_text": "Model X reached 87 percent on Benchmark Y.",
            "status": "unverified",
            "verification_basis": "missing_primary_source",
            "needs_primary": True,
            "support_count": 1,
            "independent_support_count": 1,
            "source_urls": ["https://analysis-alpha.example/report"],
        }
    ]

    plan(state, providers=providers, strategies=defaults, settings=settings)

    prompt = llm.recorded_prompts[0]
    assert "CROSS-CHECK TARGETS" in prompt
    assert "Model X reached 87 percent on Benchmark Y" in prompt
    assert "analysis-alpha.example" in prompt
    plan_log = state["iteration_logs"][-1]
    assert "Cross-Check" in plan_log["planning_basis"]["active_strategies"]
    assert plan_log["planning_basis"]["crosscheck_target_count"] == 1


def test_select_crosscheck_targets_prefers_unverified_numeric_claims():
    targets = _select_crosscheck_targets(
        [
            {
                "claim_id": "claim_verified",
                "claim_text": "A well supported claim exists.",
                "status": "verified",
                "verification_basis": "cross_checked",
                "support_count": 2,
                "independent_support_count": 2,
            },
            {
                "claim_id": "claim_unverified",
                "claim_text": "Model X reached 87 percent on Benchmark Y.",
                "status": "unverified",
                "verification_basis": "missing_primary_source",
                "needs_primary": True,
                "support_count": 1,
                "independent_support_count": 1,
                "source_urls": ["https://analysis-alpha.example/report"],
            },
        ]
    )

    assert [target["claim_id"] for target in targets] == ["claim_unverified"]
    assert targets[0]["source_domains"] == ["analysis-alpha.example"]


def test_select_crosscheck_targets_prefers_single_source_verified_claims():
    claims = [
        {
            "claim_id": "claim_single",
            "claim_text": "A Fortune article says AI layoffs rose by 18 percent.",
            "status": "verified",
            "verification_basis": "quality_source",
            "support_count": 1,
            "independent_support_count": 1,
            "source_urls": ["https://fortune.com/ai-layoffs"],
            "citation_set": [{"url": "https://fortune.com/ai-layoffs"}],
        },
        {
            "claim_id": "claim_cross_checked",
            "claim_text": "A well corroborated claim with two independent sources.",
            "status": "verified",
            "verification_basis": "cross_checked",
            "support_count": 2,
            "independent_support_count": 2,
            "source_urls": ["https://a.example/x", "https://b.example/y"],
            "citation_set": [
                {"url": "https://a.example/x"},
                {"url": "https://b.example/y"},
            ],
        },
    ]
    targets = _select_crosscheck_targets(claims, max_targets=1)

    assert [target["claim_id"] for target in targets] == ["claim_single"]
    assert targets[0]["source_domains"] == ["fortune.com"]


def test_evidence_depth_gap_flags_majority_single_source_claims():
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["consolidated_claims"] = [
        {
            "claim_id": f"claim_{idx}",
            "claim_text": f"AI benchmark {idx} improved by {idx} percent.",
            "status": "verified",
            "verification_basis": "quality_source",
            "citation_set": [{"url": f"https://example.com/{idx}"}],
        }
        for idx in range(4)
    ]

    gap = _evidence_depth_gap(state)

    assert gap["active"] is True
    assert gap["cross_checked_count"] == 0
    assert gap["single_source_count"] == 4
    assert gap["supported_count"] == 4
    assert "majority_single_source_claims" in gap["reason"]


def test_plan_prompt_question_form_instruction_present_across_rounds():
    from inqtrix.nodes import plan

    settings = AgentSettings()
    defaults = create_default_strategies(settings)

    state_round_zero = _plan_state_round_zero()
    llm_zero = _RecordingLLMStub("[\"Wie funktioniert X?\"]")
    plan(
        state_round_zero,
        providers=ProviderContext(llm=llm_zero, search=_SearchStub()),
        strategies=defaults,
        settings=settings,
    )

    state_round_two = initial_state("Was ist passiert?", max_total_seconds=30)
    state_round_two["round"] = 2
    state_round_two["context"] = ["Kontextblock"]
    state_round_two["all_citations"] = ["https://example.com/a"]
    llm_two = _RecordingLLMStub("[\"Welche neuen Erkenntnisse?\"]")
    plan(
        state_round_two,
        providers=ProviderContext(llm=llm_two, search=_SearchStub()),
        strategies=defaults,
        settings=settings,
    )

    assert "COMPLETE QUESTION" in llm_zero.recorded_prompts[0]
    assert "COMPLETE QUESTION" in llm_two.recorded_prompts[0]


def test_evaluate_sees_every_source_in_the_compact_view(monkeypatch):
    """The evaluator judges breadth: with the profile's evaluator budget and
    the compact record view, every report-eligible source is in its prompt —
    even when the full source blocks would not fit. Before, a 24k full-block
    view showed the evaluator a fraction of the ledger and it named GAPS for
    sources the run already had. The budget is the profile's own field, so a
    profile with a small evaluator budget cuts visibly instead of silently."""
    class _RecordingEvalLLM(_EvalLLMStub):
        def __init__(self, response: str) -> None:
            super().__init__(response)
            self.prompts: list[str] = []

        def complete(self, prompt, *a, **kw):
            self.prompts.append(prompt)
            return self._response

    settings = AgentSettings(
        report_profile=ReportProfile.SCHNELL, confidence_stop=8, max_rounds=1
    )
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    ledger: list[dict] = []
    query_synthesis: dict[str, dict] = {}
    for q in range(6):
        query_id = f"qry{q}"
        for i in range(30):
            # Distinct per source (the renderer de-duplicates repeated text
            # within a group): ~1.4k chars each in the full view.
            passage = f"Belegtext {q}-{i}, konkrete Zahl und Einordnung. " * 30
            record = _ledger_record(
                f"ev_{q}_{i}",
                f"https://example.com/{query_id}-{i}",
                title=f"Quelle {q}-{i}",
                claims=[
                    {
                        "claim_text": f"Aussage {q}-{i}: eine belegte Zahl aus dieser Quelle.",
                        "evidence_snippet": passage,
                        "status": "verified",
                    }
                ],
            )
            record["query_id"] = query_id
            record["query"] = f"Suchanfrage {q}"
            record["source_passages"] = [
                {"passage_id": f"p_{q}_{i}", "origin": "source_snippet", "text": passage}
            ]
            ledger.append(record)
        query_synthesis[query_id] = {
            "query": f"Suchanfrage {q}",
            "round": 0,
            "provider_answer": "Provider-Synthese.",
            "related_questions": [],
        }
    assert len(ledger) == 180
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["evidence_ledger"] = ledger
    state["query_synthesis"] = query_synthesis
    state["context"] = ["Kontextblock"]
    state["all_citations"] = [r["canonical_url"] for r in ledger]
    llm = _RecordingEvalLLM(
        "STATUS: SUFFICIENT\nCONFIDENCE: 8\nGAPS: Keine\nCONTRADICTIONS: Nein\n"
        "IRRELEVANT: Keine\nCOMPETING_EVENTS: Keine\nEVIDENCE_CONSISTENCY: 8\n"
        "EVIDENCE_SUFFICIENCY: 8"
    )
    evaluate(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=strategies,
        settings=settings,
    )
    assert llm.prompts, "the evaluator must have been called"
    prompt = llm.prompts[-1]
    missing = [f"[E{n}]" for n in range(1, 181) if f"[E{n}]" not in prompt]
    assert missing == [], f"evaluator did not see {len(missing)} sources: {missing[:5]}"
    # Breadth, not depth: the compact view carries claims, never the passages.
    assert "Evidence excerpts:" not in prompt
    assert "Aussage 5-29" in prompt
    # The evaluator's budget is the profile's own field (not the writer's
    # budget, not a constant): a tight evaluator budget cuts visibly.
    tight = dataclasses.replace(
        settings.report_tuning, evaluate_evidence_total_char_budget=6000
    )
    monkeypatch.setattr(AgentSettings, "report_tuning", property(lambda self: tight))
    tight_state = initial_state("Was ist passiert?", max_total_seconds=30)
    tight_state["round"] = 1
    tight_state["evidence_ledger"] = copy.deepcopy(ledger)
    tight_state["query_synthesis"] = query_synthesis
    tight_state["context"] = ["Kontextblock"]
    tight_state["all_citations"] = [r["canonical_url"] for r in ledger]
    tight_llm = _RecordingEvalLLM(llm._response)
    evaluate(
        tight_state,
        providers=ProviderContext(llm=tight_llm, search=_SearchStub()),
        strategies=strategies,
        settings=settings,
    )
    tight_prompt = tight_llm.prompts[-1]
    assert "NOTE:" in tight_prompt and "did not fit into the evidence budget" in tight_prompt
    assert "[E180]" not in tight_prompt



def test_appendix_names_the_sources_the_budget_left_unevaluated():
    """A budget cut is a completeness fact of the report: the appendix names
    how many report-eligible sources of which searches were not evaluated,
    so a reader never takes the cut for 'nothing found'."""
    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    appendix = _build_answer_appendix_sections(
        "Antwort mit [1](https://example.com/report/a).",
        allowed_citations=["https://example.com/report/a"],
        label_urls={"E1": "https://example.com/report/a"},
        strategies=strategies,
        incomplete_reasons=[],
        finish_reason="stop",
        omitted_by_query=[
            {"query_id": "q1", "query": "Foerderung Oesterreich 2026", "omitted": 12, "rendered": 0},
            {"query_id": "q2", "query": "Fristen 2026", "omitted": 9, "rendered": 4},
            {"query_id": "q3", "query": "voll gerendert", "omitted": 0, "rendered": 5},
        ],
        language="de",
    )
    note = next(s for s in appendix.sections if "Hinweis zur Vollständigkeit" in s)
    assert "21 belegfähige Quellen aus 2 Suchen" in note
    assert "„Foerderung Oesterreich 2026“" in note and "„Fristen 2026“" in note
    assert "voll gerendert" not in note
    assert "Status: unvollständig" not in note  # the report itself finished
    # No cut, no note.
    quiet = _build_answer_appendix_sections(
        "Antwort mit [1](https://example.com/report/a).",
        allowed_citations=["https://example.com/report/a"],
        label_urls={"E1": "https://example.com/report/a"},
        strategies=strategies,
        incomplete_reasons=[],
        finish_reason="stop",
        omitted_by_query=[{"query_id": "q3", "query": "voll", "omitted": 0, "rendered": 5}],
        language="de",
    )
    assert all("Hinweis zur Vollständigkeit" not in s for s in quiet.sections)


def test_answer_records_the_budget_cut_in_state_and_names_it_in_the_report():
    """When the profile budget cannot hold every source, the answer node
    stores the cut in the run state (ResearchMetrics and a parent run read
    it) and the report's appendix names the unevaluated sources per search."""

    class _ShortSectionLLM:
        models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
        )

        def complete_with_metadata(self, *a, **kw):
            return LLMResponse(
                content="Kurzer Abschnitt mit Beleg [E1].",
                prompt_tokens=10,
                completion_tokens=5,
                model="reasoning-model",
                finish_reason="stop",
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise metadata completions")

        def is_available(self):
            return True

    settings = AgentSettings(
        report_profile=ReportProfile.SCHNELL,
        observability_profile="summary",
        testing_mode=True,
    )
    defaults = create_default_strategies(settings)
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["round"] = 1
    state["queries"] = ["q0", "q1", "q2"]
    state["final_confidence"] = 7
    ledger: list[dict] = []
    for q in range(3):
        for i in range(25):
            passage = f"Belegtext {q}-{i}, konkrete Zahl und Einordnung. " * 30
            record = _ledger_record(
                f"ev_{q}_{i}",
                f"https://example.com/qry{q}-{i}",
                title=f"Quelle {q}-{i}",
                claims=[{"claim_text": f"Aussage {q}-{i}.", "evidence_snippet": passage, "status": "verified"}],
            )
            record["query_id"] = f"qry{q}"
            record["query"] = f"Suchanfrage {q}"
            record["source_passages"] = [
                {"passage_id": f"p_{q}_{i}", "origin": "source_snippet", "text": passage}
            ]
            ledger.append(record)
    state["evidence_ledger"] = ledger
    state["all_citations"] = [r["canonical_url"] for r in ledger]

    answer(
        state,
        providers=ProviderContext(llm=_ShortSectionLLM(), search=_SearchStub()),
        strategies=StrategyContext(
            source_tiering=defaults.source_tiering,
            claim_extraction=defaults.claim_extraction,
            claim_consolidation=defaults.claim_consolidation,
            risk_scoring=defaults.risk_scoring,
            stop_criteria=defaults.stop_criteria,
        ),
        settings=settings,
    )

    omitted = state["omitted_evidence_record_count"]
    assert omitted > 0, "75 sources x ~1.4k chars must overflow the 64k budget"
    assert state["rendered_evidence_record_count"] + omitted == 75
    by_query = state["omitted_evidence_by_query"]
    assert sum(entry["omitted"] for entry in by_query) == omitted
    # Every search kept sources: the cut thinned evenly instead of dropping a search.
    assert all(entry["rendered"] > 0 for entry in by_query)
    assert "## Hinweis zur Vollständigkeit" in state["answer"]
    assert f"Nicht ausgewertete Quellen: {omitted} belegfähige Quellen aus {len(by_query)} Suchen" in state["answer"]
    assert "„Suchanfrage 0“" in state["answer"]


def _capture_evaluated_answer_prompts(profile, *, translated_headings=False, obey_disclosure_heading=False):
    tuning = AgentSettings(report_profile=profile).report_tuning
    if translated_headings:
        tuning = dataclasses.replace(
            tuning,
            answer_sections=tuple(
                dataclasses.replace(section, key=f"Del {index}")
                for index, section in enumerate(tuning.answer_sections)
            ),
        )

    class _Settings(AgentSettings):
        @property
        def report_tuning(self):
            return tuning

    class _CapturingLLM(_DirectLLMStub):
        def __init__(self):
            super().__init__()
            self.systems = []

        def complete_with_metadata(self, prompt, **kwargs):
            self.systems.append(kwargs["system"])
            response = super().complete_with_metadata(prompt, **kwargs)
            content = "Aussage mit Beleg [E1]."
            if obey_disclosure_heading:
                # Model a writer following the structural instruction. The
                # final-answer expectation below is independent of this prompt.
                directive = re.search(r"Add a section '([^']+)'", kwargs["system"])
                if directive:
                    content += f"\n\n{directive[1]}\n- Die Anwendbarkeit bleibt offen. [E1]"
            return dataclasses.replace(response, content=content)

    settings = _Settings(report_profile=profile, testing_mode=True)
    state = initial_state("Welche Aussagen sind belegt?", max_total_seconds=30)
    state["round"] = 1
    state["final_confidence"] = 6
    state["required_aspects"] = ["Ergebnis", "Geltungsbereich"]
    state["uncovered_aspects"] = ["Geltungsbereich"]
    state["coverage_source"] = "llm"
    state["evaluate_status"] = "insufficient"
    state["gaps"] = "The current applicability has not been established."
    state["claim_status_counts"] = {"verified": 0, "unverified": 1}
    state["all_citations"] = ["https://source.example/report"]
    state["evidence_ledger"] = [_ledger_record("ev_1", state["all_citations"][0])]
    llm = _CapturingLLM()
    answer(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=create_default_strategies(settings),
        settings=settings,
    )
    assert len(llm.systems) == len(tuning.answer_sections)
    return llm.systems, state


@pytest.mark.parametrize("profile", list(ReportProfile))
@pytest.mark.parametrize("translated_headings", [False, True])
def test_every_section_plan_has_one_reachable_uncertainty_owner(profile, translated_headings):
    systems, _ = _capture_evaluated_answer_prompts(profile, translated_headings=translated_headings)
    directive = "Add a section '### Unsicherheiten / Offene Punkte'"
    # These are call positions: summaries are written after the body.
    expected_call = {ReportProfile.SCHNELL: 0, ReportProfile.COMPACT: 2, ReportProfile.DEEP: 3}[profile]
    assert [index for index, system in enumerate(systems) if directive in system] == [expected_call]


def test_answer_passes_the_evaluators_explanation_to_every_writer_call():
    systems, _ = _capture_evaluated_answer_prompts(ReportProfile.SCHNELL)
    assert all("The current applicability has not been established." in system for system in systems)
    assert all('"status": "insufficient"' in system for system in systems)


@pytest.mark.parametrize("profile", list(ReportProfile))
def test_requested_disclosure_survives_section_composition(profile):
    _, state = _capture_evaluated_answer_prompts(profile, obey_disclosure_heading=True)
    assert "### Unsicherheiten / Offene Punkte\n- Die Anwendbarkeit bleibt offen." in state["answer"]
    assert state["answer"].count("Die Anwendbarkeit bleibt offen.") == 1


@pytest.mark.parametrize("language, search_language, expected", [
    ("de", "de", "IMPORTANT: write the search queries in German."),
    ("en", "en", "IMPORTANT: write the search queries in English."),
    ("de", "en", "IMPORTANT: write the search queries in ENGLISH; English sources serve this topic best."),
    ("de", "fr", "IMPORTANT: write the search queries in French."),
])
def test_plan_prompt_always_states_the_query_language(language, search_language, expected):
    """The planner prompt is English, so without the line every question
    would get English queries; the line names the search language even when
    it equals the answer language."""
    from inqtrix.nodes import plan

    settings = AgentSettings()
    defaults = create_default_strategies(settings)
    llm = _RecordingLLMStub('["q1"]')
    providers = ProviderContext(llm=llm, search=_SearchStub())
    state = _plan_state_round_zero()
    state["language"] = language
    state["search_language"] = search_language
    plan(state, providers=providers, strategies=defaults, settings=settings)
    assert expected in llm.recorded_prompts[0]


def test_direct_llm_system_prompt_is_english_and_carries_the_format_block() -> None:
    """The web-free chat shares the one output-format block with every
    other answer path and scaffolds the conversation in English; the
    answer language is carried by the directive alone."""
    from inqtrix.prompt_language import OUTPUT_FORMAT_BLOCK

    class _SystemRecordingLLM(_DirectLLMStub):
        def __init__(self) -> None:
            super().__init__()
            self.systems: list[str] = []

        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            self.systems.append(str(kwargs.get("system") or ""))
            return super().complete_with_metadata(prompt, **kwargs)

    settings = AgentSettings(skip_search=True)
    llm = _SystemRecordingLLM()
    providers = ProviderContext(llm=llm, search=_SearchStub())
    strategies = create_default_strategies(settings)

    run(
        "Wie funktioniert die Energiebilanz eines Speichers und was ist wichtig?",
        "User: Hallo\nAssistant: Hallo!",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )
    from inqtrix.prompt_language import WRITING_STYLE_BLOCK

    system = llm.systems[0]
    assert system.startswith("You are a helpful conversational assistant without web research.")
    assert OUTPUT_FORMAT_BLOCK in system
    assert WRITING_STYLE_BLOCK in system
    assert system.index(OUTPUT_FORMAT_BLOCK) < system.index(WRITING_STYLE_BLOCK)
    assert system.rstrip().endswith("Keep answers compact; prefer inline math.")
    assert "Du bist" not in system
    prompt = llm.prompts[0]
    assert "\n\nConversation so far:\nUser: Hallo\nAssistant: Hallo!\n\nCurrent question:\n" in prompt
    assert "Gespraechsverlauf" not in prompt


def test_unresolved_language_reaches_the_answer_prompt_as_the_neutral_directive():
    """When neither the classifier nor the text heuristic resolved a
    language, the writer gets the language-neutral directive instead of
    a guessed one; the headings follow the progress-language seed."""
    from inqtrix.prompt_language import QUESTION_LANGUAGE_DIRECTIVE

    class _CapturingLLM(_DirectLLMStub):
        def __init__(self):
            super().__init__()
            self.systems = []

        def complete_with_metadata(self, prompt, **kwargs):
            self.systems.append(kwargs["system"])
            response = super().complete_with_metadata(prompt, **kwargs)
            return dataclasses.replace(response, content="Statement with evidence [E1].")

    settings = AgentSettings(report_profile=ReportProfile.SCHNELL, testing_mode=True)
    state = initial_state("DSGVO Pflichten", max_total_seconds=30)
    state["language"] = "en"
    state["language_source"] = "unresolved"
    state["all_citations"] = ["https://source.example/report"]
    state["evidence_ledger"] = [_ledger_record("ev_1", state["all_citations"][0])]
    llm = _CapturingLLM()
    answer(
        state,
        providers=ProviderContext(llm=llm, search=_SearchStub()),
        strategies=create_default_strategies(settings),
        settings=settings,
    )
    assert llm.systems
    assert all(system.startswith(QUESTION_LANGUAGE_DIRECTIVE) for system in llm.systems)
    assert "ALWAYS respond in English" not in llm.systems[0]
    assert "## Summary" in state["answer"] and "## Key points" in state["answer"]


def test_rendered_sections_are_joined_without_horizontal_rules():
    """Headings carry the structure of a composed report; a rule between
    every section was the default separator the operator rejected."""
    joined = _join_rendered_sections(["## A\n\nfirst", "  ", "## B\n\nsecond\n"])
    assert joined == "## A\n\nfirst\n\n## B\n\nsecond"
    assert "---" not in joined


def test_answer_appendix_texts_follow_the_run_language():
    """References, examined sources and the completeness note are answer
    text: an English run gets English headings and notes, a German run
    German ones with real umlauts."""
    settings = AgentSettings()
    strategies = create_default_strategies(settings)
    for language, heading, examined, status in (
        ("en", "## References", "## Further examined sources", "- Status: incomplete"),
        ("de", "## Referenzen", "## Weitere geprüfte Quellen", "- Status: unvollständig"),
    ):
        appendix = _build_answer_appendix_sections(
            "Answer with [1](https://example.com/report/a).",
            allowed_citations=["https://example.com/report/a", "https://example.com/report/b"],
            label_urls={"E1": "https://example.com/report/a", "E2": "https://example.com/report/b"},
            strategies=strategies,
            incomplete_reasons=["section ended mid-sentence"],
            finish_reason="length",
            language=language,
        )
        joined = "\n".join(appendix.sections)
        assert heading in joined and examined in joined and status in joined, language
        assert "ae" not in "".join(w for w in joined.split() if w.lower().startswith(("belegf", "unvollst", "ausgef")))
