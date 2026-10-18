"""Pins: the answer contract and the coverage checklist come from the
classify call, the evaluator judges coverage, competing events are an
event-mode mechanism, gap slots are the sub-questions verbatim, and a
claim's date is the bound citation's provider date."""

from __future__ import annotations

import json
import logging
from queue import Queue
from types import SimpleNamespace

import pytest

from inqtrix.exceptions import AgentTimeout
from inqtrix.nodes import (
    _build_query_slots,
    _fallback_query_for_slot,
    classify,
    evaluate,
    search,
)
from inqtrix.prompts import build_answer_system_prompt, build_evaluate_format_suffix
from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state
from inqtrix.strategies import StrategyContext, create_default_strategies


class _ScriptedLLM:
    def __init__(self, response: str = "", *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.prompts: list[str] = []
        self.models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
            effective_claim_extract_model="claim-extract-model",
        )

    def complete(self, prompt, *a, **kw):
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        return self._response

    def consume_nonfatal_notice(self):
        return None

    def is_available(self):
        return True


class _NoSearch:
    def search(self, *a, **kw):
        raise AssertionError("no search expected")

    def is_available(self):
        return True


def _strategies(settings: AgentSettings, llm=None, claim_extraction=None) -> StrategyContext:
    defaults = create_default_strategies(settings, llm=llm)
    return StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=claim_extraction or defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )


def _classify_reply(contract: str, subs: list[str], *, recency: str = "MONTH", qtype: str = "GENERAL") -> str:
    return (
        "DECISION: SEARCH\nLANGUAGE: de\nSEARCH_LANGUAGE: de\n"
        f"RECENCY: {recency}\nTYPE: {qtype}\n"
        f"ANSWER_CONTRACT: {contract}\n"
        f"SUB_QUESTIONS: {json.dumps(subs, ensure_ascii=False)}"
    )


def _drain(queue: Queue) -> list[str]:
    out: list[str] = []
    while not queue.empty():
        out.append(queue.get()[1])
    return out


COMPARISON_QUESTION = (
    "Vergleiche die Foerderbedingungen fuer Waermepumpen in Deutschland und "
    "Oesterreich 2026 und belege jede Aussage mit Quellen"
)
COMPARISON_SUBS = [
    "Wie hoch ist die Waermepumpen-Foerderung in Deutschland 2026?",
    "Wie hoch ist die Waermepumpen-Foerderung in Oesterreich 2026?",
    "Worin unterscheiden sich Voraussetzungen und Fristen der beiden Laender 2026?",
]


def test_classify_takes_the_contract_and_the_sub_questions_from_the_model():
    """A request for sources is a work instruction, not a claim check: the
    model says COMPARISON and the three content sub-questions become the
    coverage checklist. Before, a keyword regex ("beleg…") made every such
    child a claim check with three rubric aspects."""
    settings = AgentSettings()
    llm = _ScriptedLLM(_classify_reply("COMPARISON", COMPARISON_SUBS))
    state = initial_state(COMPARISON_QUESTION, max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    assert state["answer_contract"] == "comparison"
    assert state["answer_contract_source"] == "llm"
    assert state["required_aspects"] == COMPARISON_SUBS
    assert state["uncovered_aspects"] == COMPARISON_SUBS
    assert state["aspects_source"] == "llm"
    assert state["event_mode"] is False
    joined = " ".join(state["required_aspects"]).lower()
    for rubric in ("behauptung", "gegenbelege", "cross-check"):
        assert rubric not in joined
    # The prompt asks for the contract BEFORE the sub-questions (DOTALL parser).
    prompt = llm.prompts[0]
    assert prompt.index("ANSWER_CONTRACT:") < prompt.index("SUB_QUESTIONS:")
    assert "a request for evidence, sources or citations is a work instruction" in prompt


def test_classify_claim_check_only_on_explicit_checking_intent():
    settings = AgentSettings()
    # A model that writes the value in mixed case still counts (the key stays fixed).
    llm = _ScriptedLLM(_classify_reply("Claim_Check", ["Investiert Meta 2026 weniger in KI als 2025?"]))
    state = initial_state("Stimmt es, dass Meta 2026 weniger in KI investiert?", max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    assert state["answer_contract"] == "claim_check"
    assert state["answer_contract_source"] == "llm"


def test_classify_falls_back_visibly_on_an_unknown_contract():
    """An unknown value never leaks into the state: general + fallback_invalid,
    a WARNING and a status line."""
    settings = AgentSettings()
    llm = _ScriptedLLM(_classify_reply("ESSAY", ["Teilfrage"]))
    queue: Queue = Queue()
    state = initial_state("Frage?", progress_queue=queue, max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    assert state["answer_contract"] == "general"
    assert state["answer_contract_source"] == "fallback_invalid"
    assert "essay" not in json.dumps(state, default=str)
    assert any("Antwortform nicht erkannt" in m for m in _drain(queue))


def test_classify_provider_error_falls_back_to_general_and_the_question():
    settings = AgentSettings()
    llm = _ScriptedLLM(error=AgentTimeout("classify"))
    state = initial_state("Welche Foerderung gibt es 2026?", max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    assert state["answer_contract"] == "general"
    assert state["answer_contract_source"] == "fallback_error"
    assert state["required_aspects"] == ["Welche Foerderung gibt es 2026?"]
    assert state["aspects_source"] == "fallback"
    assert state["event_mode"] is False


def test_event_mode_is_the_classifiers_decision_not_the_recency_window():
    """Event mode follows the classifier (news briefing or TYPE news). The
    recency window is no proxy: a delegated comparison child inherits
    ``week`` from a news parent and must not be judged for competing
    events."""
    settings = AgentSettings()

    def _classified(reply: str, question: str) -> dict:
        llm = _ScriptedLLM(reply)
        state = initial_state(question, max_total_seconds=30)
        classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
        return state

    briefing = _classified(
        _classify_reply("NEWS_BRIEFING", ["Was passierte diese Woche?"], recency="WEEK", qtype="NEWS"),
        "Welche KI-Neuigkeiten gab es diese Woche?",
    )
    assert briefing["answer_contract"] == "news_briefing" and briefing["event_mode"] is True
    news_type = _classified(
        _classify_reply("GENERAL", ["Was ist passiert?"], recency="MONTH", qtype="NEWS"),
        "Was ist bei der Bahn passiert?",
    )
    assert news_type["event_mode"] is True
    comparison_in_a_week_window = _classified(
        _classify_reply("COMPARISON", COMPARISON_SUBS, recency="WEEK"), COMPARISON_QUESTION
    )
    assert comparison_in_a_week_window["event_mode"] is False
    market = _classified(
        _classify_reply("GENERAL", ["Wie entwickelt sich der Markt bis 2030?"], recency="MONTH"),
        "Wie entwickelt sich der Markt fuer Batteriespeicher bis 2030?",
    )
    assert market["event_mode"] is False


def test_missing_sub_questions_fall_back_to_the_question_visibly():
    """No SUB_QUESTIONS line: the question itself is the only sub-question,
    said as a WARNING and a status line — never a silent checklist of one."""
    settings = AgentSettings()
    llm = _ScriptedLLM(
        "DECISION: SEARCH\nLANGUAGE: de\nSEARCH_LANGUAGE: de\nRECENCY: MONTH\nTYPE: GENERAL\n"
        "ANSWER_CONTRACT: GENERAL\n"
    )
    queue: Queue = Queue()
    state = initial_state("Welche Foerderung gibt es 2026?", progress_queue=queue, max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    assert state["required_aspects"] == ["Welche Foerderung gibt es 2026?"]
    assert state["aspects_source"] == "fallback"
    assert any("Teilfragen nicht erkannt" in m for m in _drain(queue))


def test_gap_slots_are_the_open_sub_questions_verbatim_and_carry_no_rubric_slots():
    state = initial_state("Welche Foerderung gilt 2030?", max_total_seconds=30)
    state["answer_contract"] = "general"
    state["required_aspects"] = list(COMPARISON_SUBS[:2])
    state["uncovered_aspects"] = list(COMPARISON_SUBS[:2])
    slots = _build_query_slots(state, target_count=6, crosscheck_targets=[], evidence_depth_gap={})
    gap_slots = [slot for slot in slots if slot["slot_type"] == "gap"]
    assert [slot["aspect"] for slot in gap_slots] == COMPARISON_SUBS[:2]
    assert all(slot["instruction"].endswith(slot["aspect"]) for slot in gap_slots)
    assert not any("aspect" in slot for slot in slots if slot["slot_type"] == "storm_perspective")
    # "2030" in the question is no reason for a data-verification slot.
    assert not any(slot["slot_type"] == "data_verification" for slot in slots)
    # The slot fallback query is the sub-question itself, in both languages.
    assert _fallback_query_for_slot(gap_slots[0], question="q", search_language="de") == COMPARISON_SUBS[0]
    assert _fallback_query_for_slot(gap_slots[0], question="q", search_language="en") == COMPARISON_SUBS[0]
    # The contract, not a digit, decides on data verification.
    state["answer_contract"] = "data_extraction"
    data_slots = _build_query_slots(state, target_count=6, crosscheck_targets=[], evidence_depth_gap={})
    assert any(slot["slot_type"] == "data_verification" for slot in data_slots)


_EVAL_TAIL = "\nCONTRADICTIONS: Nein\nEVIDENCE_CONSISTENCY: 8\nEVIDENCE_SUFFICIENCY: 8"


@pytest.mark.parametrize(
    ("slot", "german", "english"),
    [
        (
            {"slot_type": "crosscheck", "target_claim": "user_fuer_claim"},
            "Welche unabhängigen Quellen bestätigen oder widerlegen die Aussage, dass user_fuer_claim?",
            "Which independent sources confirm or refute the claim that user_fuer_claim?",
        ),
        (
            {"slot_type": "primary_source"},
            "Was ist die Primärquelle für user_fuer_question?",
            "What is the original primary source for user_fuer_question?",
        ),
        (
            {"slot_type": "gap", "gap": "user_fuer_gap"},
            "Welche Quellen beantworten die offene Evidenzlücke user_fuer_gap direkt?",
            "Which sources directly answer the open evidence gap user_fuer_gap?",
        ),
        (
            {"slot_type": "counterevidence"},
            "Welche glaubwürdige Evidenz widerspricht den wichtigsten Aussagen zu user_fuer_question?",
            "What credible evidence challenges the main claims about user_fuer_question?",
        ),
        (
            {"slot_type": "data_verification"},
            "Welche Primärdaten verifizieren die zentralen Zahlen zu user_fuer_question?",
            "Which primary data sources verify the key numbers about user_fuer_question?",
        ),
    ],
    ids=["crosscheck", "primary-source", "gap", "counterevidence", "data-verification"],
)
def test_fallback_search_copy_uses_umlauts_without_rewriting_user_content(slot, german, english):
    for language, expected in [("de", german), ("en", english)]:
        assert _fallback_query_for_slot(
            slot, question="user_fuer_question", search_language=language
        ) == expected


def _evaluate_with(
    reply: str,
    *,
    event_mode: bool = False,
    aspects: list[str] | None = None,
    confidence_stop: int = 7,
    strategy_confidence_stop: int | None = None,
    error: Exception | None = None,
):
    settings = AgentSettings(confidence_stop=confidence_stop, max_rounds=3)
    strategy_settings = (
        AgentSettings(confidence_stop=strategy_confidence_stop, max_rounds=3)
        if strategy_confidence_stop is not None
        else settings
    )
    llm = _ScriptedLLM(reply, error=error)
    queue: Queue = Queue()
    state = initial_state("Frage?", progress_queue=queue, max_total_seconds=30)
    state["round"] = 1
    state["context"] = ["Kontext"]
    state["all_citations"] = ["https://example.com/a"]
    state["event_mode"] = event_mode
    subs = list(aspects if aspects is not None else COMPARISON_SUBS)
    state["required_aspects"] = subs
    state["uncovered_aspects"] = list(subs)
    evaluate(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(strategy_settings), settings=settings)
    return state, llm, _drain(queue)


def test_evaluator_judges_coverage_via_covered_aspects():
    state, llm, _ = _evaluate_with("STATUS: INSUFFICIENT\nCONFIDENCE: 6\nGAPS: Oesterreich fehlt\nCOVERED_ASPECTS: [1, 3]" + _EVAL_TAIL)
    assert state["uncovered_aspects"] == [COMPARISON_SUBS[1]]
    assert state["aspect_coverage"] == 0.667
    assert state["coverage_source"] == "llm"
    assert state["evaluate_status"] == "insufficient"
    prompt = llm.prompts[-1]
    assert "SUB-QUESTION COVERAGE:" in prompt and f"2. {COMPARISON_SUBS[1]}" in prompt
    assert "COVERED_ASPECTS:" in prompt
    # Out-of-range numbers are ignored; every valid index counts.
    full, _, _ = _evaluate_with("STATUS: SUFFICIENT\nCONFIDENCE: 8\nGAPS: Keine\nCOVERED_ASPECTS: [1, 2, 3, 9]" + _EVAL_TAIL)
    assert full["uncovered_aspects"] == [] and full["aspect_coverage"] == 1.0
    assert full["evaluate_status"] == "sufficient"


def test_missing_covered_aspects_line_keeps_the_open_list_and_says_so():
    state, _, messages = _evaluate_with("STATUS: SUFFICIENT\nCONFIDENCE: 8\nGAPS: Keine" + _EVAL_TAIL)
    assert state["uncovered_aspects"] == COMPARISON_SUBS
    assert state["coverage_source"] == "missing"
    assert any("COVERED_ASPECTS" in m for m in messages), messages


def test_covered_aspects_accepts_numeric_strings_and_flags_lists_without_numbers():
    """Models write ["1", "3"] as readily as [1, 3]; both count. A list
    that carries no usable number is unparsable, said out loud — before it
    silently read as "nothing covered"."""
    state, _, _ = _evaluate_with('STATUS: INSUFFICIENT\nCONFIDENCE: 6\nGAPS: x\nCOVERED_ASPECTS: ["1", "3"]' + _EVAL_TAIL)
    assert state["uncovered_aspects"] == [COMPARISON_SUBS[1]]
    assert state["coverage_source"] == "llm"
    odd, _, messages = _evaluate_with('STATUS: INSUFFICIENT\nCONFIDENCE: 6\nGAPS: x\nCOVERED_ASPECTS: ["Deutschland", "Fristen"]' + _EVAL_TAIL)
    assert odd["uncovered_aspects"] == COMPARISON_SUBS
    assert odd["coverage_source"] == "unparsable"
    assert any("COVERED_ASPECTS" in m for m in messages), messages


def test_temporal_consistency_is_asked_for_every_question_and_event_hints_only_in_event_mode():
    """'vor kurzem' is a property of the question's wording, not of event
    mode; the multiple-events and competing-explanations hints are."""
    reply = "STATUS: SUFFICIENT\nCONFIDENCE: 8\nGAPS: Keine\nCOVERED_ASPECTS: [1, 2, 3]" + _EVAL_TAIL
    _, calm_llm, _ = _evaluate_with(reply, event_mode=False)
    calm = calm_llm.prompts[-1]
    assert "TEMPORAL CONSISTENCY:" in calm
    assert "SEVERAL MATCHING EVENTS" not in calm and "COMPETING EXPLANATIONS" not in calm
    _, live_llm, _ = _evaluate_with(reply, event_mode=True)
    live = live_llm.prompts[-1]
    assert "TEMPORAL CONSISTENCY:" in live and "SEVERAL MATCHING EVENTS" in live


def test_evaluate_provider_error_marks_status_and_coverage_as_fallback():
    state, _, messages = _evaluate_with("", error=AgentTimeout("evaluate"))
    assert state["evaluate_status"] == "fallback"
    assert state["coverage_source"] == "fallback"
    assert state["uncovered_aspects"] == COMPARISON_SUBS
    assert any("AgentTimeout" in m for m in messages), messages


def test_the_evaluate_node_hands_the_runs_stop_threshold_to_the_stop_strategy():
    """The stop strategy is built once per process with the base settings;
    the run's profile-adjusted ``confidence_stop`` reaches its caps through
    the state, so a stricter run is not capped at the process default."""
    reply = "STATUS: SUFFICIENT\nCONFIDENCE: 9\nGAPS: Keine\nCOMPETING_EVENTS: Reform A vs Reform B\nCOVERED_ASPECTS: [1, 2, 3]" + _EVAL_TAIL
    state, _, _ = _evaluate_with(reply, event_mode=True, confidence_stop=9, strategy_confidence_stop=6)
    assert state["_confidence_stop"] == 9
    assert state["final_confidence"] == 8, "capped one below the RUN's threshold, not below the process default 6"


def test_competing_events_are_asked_and_applied_only_in_event_mode():
    reply = "STATUS: SUFFICIENT\nCONFIDENCE: 9\nGAPS: Keine\nCOMPETING_EVENTS: Reform A vs Reform B\nCOVERED_ASPECTS: [1, 2, 3]" + _EVAL_TAIL
    calm, llm, _ = _evaluate_with(reply, event_mode=False, confidence_stop=7)
    assert "COMPETING_EVENTS:" not in llm.prompts[-1]
    assert "COMPETING EXPLANATIONS" not in llm.prompts[-1]
    assert calm["competing_events"] == ""
    assert calm["final_confidence"] >= 7, "no competing-events cap outside event mode"
    live, live_llm, _ = _evaluate_with(reply, event_mode=True, confidence_stop=7)
    assert "COMPETING_EVENTS:" in live_llm.prompts[-1]
    assert live["competing_events"] == "Reform A vs Reform B"
    assert live["final_confidence"] < 7, "the cap applies in event mode"


def test_evaluate_format_suffix_is_built_per_mode():
    calm = build_evaluate_format_suffix(event_mode=False, aspect_count=0)
    assert "COMPETING_EVENTS" not in calm and "COVERED_ASPECTS" not in calm
    live = build_evaluate_format_suffix(event_mode=True, aspect_count=2)
    assert "COMPETING_EVENTS:" in live and "COVERED_ASPECTS:" in live


def test_answer_prompt_carries_the_contract_block():
    base = {
        "question": "Vergleich?",
        "language": "de",
        "answer_contract": "comparison",
        "required_aspects": COMPARISON_SUBS,
        "uncovered_aspects": [],
        "source_tier_counts": {"primary": 1, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0},
        "claim_status_counts": {"verified": 1, "contested": 0, "unverified": 0},
    }
    comparison = build_answer_system_prompt(dict(base))
    assert "ANSWER FORM: comparison." in comparison
    assert "SUB-QUESTION COVERAGE:" in comparison
    general = build_answer_system_prompt({**base, "answer_contract": "general"})
    assert "ANSWER FORM:" not in general


def test_answer_prompt_says_when_coverage_was_not_judged():
    """Only an evaluator judgement may be reported as "noch offen"; a run
    whose evaluation failed or never ran says so instead of presenting the
    initial open list as a finding."""
    base = {
        "question": "Vergleich?",
        "language": "de",
        "answer_contract": "comparison",
        "required_aspects": COMPARISON_SUBS,
        "uncovered_aspects": [COMPARISON_SUBS[2]],
        "source_tier_counts": {"primary": 1, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0},
        "claim_status_counts": {"verified": 1, "contested": 0, "unverified": 0},
    }
    judged = build_answer_system_prompt({**base, "coverage_source": "llm"})
    assert "Still open according to the evaluation" in judged and COMPARISON_SUBS[2] in judged
    unjudged = build_answer_system_prompt({**base, "coverage_source": "fallback"})
    assert "Coverage not judged" in unjudged
    assert "Still open according to the evaluation" not in unjudged


def test_search_dates_a_claim_from_the_bound_citation_not_from_the_model():
    """The extractor returns no date; the search node takes the
    provider's citation date (else unknown) and does not scan the evidence
    for coverage — that judgement is the evaluator's."""

    class _DatedSearch:
        def search(self, *a, **kw):
            return GroundedSearchResult(
                answer="Foo berichtet: passiert ist eine Reform.",
                sources=[GroundedSource(url="https://a.example/r", title="A", snippet="s", rank=1, date="2026-03-01")],
            )

        def is_available(self):
            return True

    class _ClaimExtraction:
        def extract(self, *a, **kw):
            return (
                [
                    {
                        "claim_text": "Passiert ist eine Reform der Foerderung im Jahr 2026.",
                        "claim_type": "fact",
                        "polarity": "affirmed",
                        "needs_primary": False,
                        "source_urls": ["https://a.example/r"],
                        "provider_refs": ["1"],
                        "binding_status": "bound",
                        "evidence_snippet": "Reform der Foerderung.",
                        "published_date": "2019-01-01",  # a model date must never win
                    }
                ],
                0,
                0,
            )

        def consume_nonfatal_notice(self):
            return None

    settings = AgentSettings(first_round_queries=1, max_rounds=4)
    llm = _ScriptedLLM("")
    strategies = _strategies(settings, llm=llm, claim_extraction=_ClaimExtraction())
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    state["queries"] = ["q1"]
    state["required_aspects"] = ["Was ist passiert?"]
    state["uncovered_aspects"] = ["Was ist passiert?"]
    search(state, providers=ProviderContext(llm=llm, search=_DatedSearch()), strategies=strategies, settings=settings)
    claims = [claim for record in state["evidence_ledger"] for claim in record.get("claims", [])]
    assert claims, "the bound claim must reach the ledger"
    assert {claim["published_date"] for claim in claims} == {"2026-03-01"}
    # The search node does not estimate coverage.
    assert state["uncovered_aspects"] == ["Was ist passiert?"]
    assert state["aspect_coverage"] == 0.0


def _classified_state(reply: str, question: str) -> dict:
    settings = AgentSettings()
    llm = _ScriptedLLM(reply)
    state = initial_state(question, max_total_seconds=30)
    classify(state, providers=ProviderContext(llm=llm, search=_NoSearch()), strategies=_strategies(settings), settings=settings)
    return state


def test_classify_resolves_the_answer_language_without_a_silent_default(caplog):
    """Classifier code first, then the text heuristic; without a signal the
    run records ``unresolved`` and warns instead of assuming German."""
    from inqtrix.i18n import detect_ui_language

    reply = _classify_reply("GENERAL", ["Sous-question"])
    french = _classified_state(reply.replace("LANGUAGE: de", "LANGUAGE: fr"), "Quelle est la situation?")
    assert (french["language"], french["language_source"]) == ("fr", "classified")

    no_language_line = reply.replace("LANGUAGE: de\n", "")
    detected = _classified_state(
        no_language_line, "Wie hoch ist die Waermepumpen-Foerderung in Deutschland 2026?"
    )
    assert (detected["language"], detected["language_source"]) == ("de", "detected")

    with caplog.at_level(logging.WARNING):
        unresolved = _classified_state(no_language_line, "DSGVO Pflichten")
    assert unresolved["language_source"] == "unresolved"
    # The progress-text seed survives; only the answer directive turns neutral.
    assert unresolved["language"] == detect_ui_language("DSGVO Pflichten")
    assert any("Answer language unresolved" in r.getMessage() for r in caplog.records)


def test_search_language_follows_the_classifier_then_the_answer_language_then_english(caplog):
    reply = _classify_reply("GENERAL", ["Teilfrage"])
    explicit = _classified_state(reply.replace("SEARCH_LANGUAGE: de", "SEARCH_LANGUAGE: en"), "Wie funktioniert ein Transformer-Modell?")
    assert explicit["search_language"] == "en"

    without_search_line = reply.replace("SEARCH_LANGUAGE: de\n", "")
    inherited = _classified_state(without_search_line, "Wie funktioniert ein Transformer-Modell?")
    assert inherited["search_language"] == "de"

    neither = without_search_line.replace("LANGUAGE: de\n", "")
    with caplog.at_level(logging.WARNING):
        fallback = _classified_state(neither, "DSGVO Pflichten")
    assert fallback["search_language"] == "en"
    assert any("Search language unresolved" in r.getMessage() for r in caplog.records)
