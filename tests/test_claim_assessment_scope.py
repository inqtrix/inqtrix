"""A display selection must not change already computed claim assessments."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from inqtrix import runtime_logging
import inqtrix.nodes as nodes_module
from inqtrix.graph import _build_test_metrics
from inqtrix.nodes import _evidence_depth_gap, answer, evaluate, plan, search
from inqtrix.providers.base import LLMProvider, ProviderContext
from inqtrix.report_profiles import ReportProfile
from inqtrix.result import ResearchResult
from inqtrix.scoring import build_score_snapshot
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings, ModelSettings
from inqtrix.state import build_run_snapshot, initial_state
from inqtrix.strategies import StrategyContext, create_default_strategies
from inqtrix.strategies._claim_consolidation import DefaultClaimConsolidator


ALPHA = "Die Alpha-Anlage erzeugt 10 Kilowatt Leistung."
BETA = "Der Beta-Bestand umfasst 20 Baeume."
GAMMA = "Der Gamma-Anteil betraegt 30 Prozent."
URLS = ["https://agency.gov.uk/alpha", "https://agency.gov.uk/beta", "https://discussion.example/gamma"]


class _LLM(LLMProvider):
    models = ModelSettings(reasoning_model="test-model")
    default_max_tokens = 4096

    def complete(self, prompt, **kwargs):
        # Keep citations in their sentences to isolate the claim-pool boundary.
        return f"{ALPHA.rstrip('.')} [E1].\n\n{BETA.rstrip('.')} [E2]."

    def is_available(self):
        return True


class _Search:
    def __init__(self):
        self.calls = 0

    def search(self, *args, **kwargs):
        self.calls += 1
        urls = URLS if self.calls == 1 else [*URLS[:2], "https://agency.gov.uk/gamma"]
        # Each page returns its own text (a provider snippet), so the claims
        # are checked against source text and the assessment says verified.
        return GroundedSearchResult(
            answer="\n".join((ALPHA, BETA, GAMMA)),
            sources=[
                GroundedSource(url=url, rank=i, title=f"Report {i}", snippet=text)
                for i, (url, text) in enumerate(zip(urls, (ALPHA, BETA, GAMMA)), 1)
            ],
        )


class _Extraction:
    def extract(self, text, citations, question, **kwargs):
        return [
            {"claim_text": claim, "claim_type": "fact", "polarity": "affirmed", "needs_primary": True,
             "source_urls": [url], "provider_refs": [str(i)], "binding_status": "bound",
             "evidence_snippet": claim}
            for i, (claim, url) in enumerate(zip((ALPHA, BETA, GAMMA), citations, strict=True), 1)
        ], 0, 0


def _run(cap=1, *, copy_selection=False):
    tuning = dataclasses.replace(
        AgentSettings(report_profile=ReportProfile.SCHNELL).report_tuning,
        materialize_max_total=cap, materialize_max_unverified=1,
    )

    class _Settings(AgentSettings):
        @property
        def report_tuning(self):
            return tuning

    class _CopiedSelection(DefaultClaimConsolidator):
        def materialize(self, *args, **kwargs):
            return copy.deepcopy(super().materialize(*args, **kwargs))

    settings = _Settings(
        report_profile=ReportProfile.SCHNELL, first_round_queries=1,
        max_rounds=2, testing_mode=True, observability_profile="forensic",
    )
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering, claim_extraction=_Extraction(),
        claim_consolidation=_CopiedSelection(defaults.source_tiering) if copy_selection else defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring, stop_criteria=defaults.stop_criteria,
    )
    providers = ProviderContext(llm=_LLM(), search=_Search())
    state = initial_state(
        "Welche Leistung erzeugt die Alpha-Anlage, wie gross ist der Beta-Bestand an Baeumen "
        "und welchen Prozentanteil hat Gamma?", max_total_seconds=60,
    )
    state["queries"] = ["Messwerte"]
    search(state, providers=providers, strategies=strategies, settings=settings)
    return state, settings, providers, strategies


def _statuses(state):
    return {claim["claim_text"]: claim.get("verification_status", "unverified")
            for record in state["evidence_ledger"] for claim in record.get("claims", [])}


def test_display_limits_do_not_change_verification_or_quality():
    narrow, *_ = _run(1)
    broad, *_ = _run(3)
    assert len(narrow["consolidated_claims"]) == 1
    assert len(broad["consolidated_claims"]) == 3
    assert narrow["consolidated_claims"][0]["claim_text"] == ALPHA
    for state in (narrow, broad):
        assert len(state["consolidated_claims_full"]) == 3
        assert _statuses(state) == {ALPHA: "verified", BETA: "verified", GAMMA: "unverified"}
        assert state["claim_status_counts"] == {"verified": 2, "attributed": 0, "contested": 0, "unverified": 1}
        assert (state["claim_needs_primary_total"], state["claim_needs_primary_supported"]) == (3, 2)
    assert narrow["claim_quality_score"] == broad["claim_quality_score"]


def test_a_later_assessment_updates_retained_claims_outside_the_display_selection():
    state, settings, providers, strategies = _run(1)
    original_record = next(record for record in state["evidence_ledger"] if record["canonical_url"] == URLS[2])
    assert original_record["claims"][0].get("verification_status", "unverified") == "unverified"
    state["queries"].append("Weitere Messwerte")
    search(state, providers=providers, strategies=strategies, settings=settings)
    assert providers.search.calls == 2
    assert len(state["consolidated_claims"]) == 1
    retained = next(record for record in state["evidence_ledger"] if record["evidence_id"] == original_record["evidence_id"])
    assert retained["claims"][0]["verification_status"] == "verified"


def test_snapshots_and_run_summaries_count_all_assessed_claims(monkeypatch):
    state, *_ = _run(1)
    snapshot = build_score_snapshot(state, phase="search")
    assert snapshot["evidence"]["verified_claim_count"] == 2
    assert snapshot["evidence"]["unverified_claim_count"] == 1
    assert snapshot["claims"]["consolidated_claim_count"] == 3
    assert snapshot["claims"].get("materialized_claim_count") == 1
    assert build_run_snapshot(state)["consolidated_claim_count"] == 3
    assert _build_test_metrics(state, 0)["consolidated_claims_count_final"] == 3
    events = []
    monkeypatch.setattr(runtime_logging, "emit_runtime_event", lambda name, payload: events.append(payload))
    runtime_logging.log_run_end(run_id="run-test", run_mode="run", status="success", elapsed_s=0, state=state)
    assert events[-1]["consolidated_claims_count"] == 3
    selection = next(row for row in state["iteration_logs"] if row.get("event") == "evidence_selection")
    assert selection["consolidated_claim_count"] == 3
    assert selection.get("materialized_claim_count") == 1
    assert sum(row.get("event") == "claim_merge" for row in state["iteration_logs"]) == 3


def test_answer_binding_and_usage_cover_claims_outside_copied_display_lists():
    state, settings, providers, strategies = _run(1, copy_selection=True)
    answer(state, providers=providers, strategies=strategies, settings=settings)
    assert len(state["consolidated_claims"]) == 1
    assert state["consolidated_claims"][0]["used_in_answer"] is True
    used = {claim["claim_text"] for claim in state["consolidated_claims_full"] if claim.get("used_in_answer")}
    assert used == {ALPHA, BETA}
    result = ResearchResult.from_raw({"answer": state["answer"], "result_state": state}, source_tiering=strategies.source_tiering)
    assert len(result.top_claims) == 1
    assert result.metrics.verified_claims_used_count == 2


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_an_explicit_empty_assessment_is_not_replaced_by_the_display_list(explicit_empty):
    state = initial_state("Pruefe die vorhandenen Aussagen.")
    state["consolidated_claims"] = [{"status": "verified"}]
    if explicit_empty:
        state["consolidated_claims_full"] = []
    snapshot = build_score_snapshot(state, phase="answer")
    assert snapshot["evidence"]["verified_claim_count"] == (0 if explicit_empty else 1)


def test_evidence_depth_uses_all_assessed_claims():
    full = [{"status": "attributed", "verification_basis": "primary_source", "source_urls": [URLS[0]]} for _ in range(5)]
    state = {"consolidated_claims_full": full, "consolidated_claims": full[:1]}
    gap = _evidence_depth_gap(state)
    assert gap["supported_count"] == 5
    assert gap["single_source_count"] == 5
    assert gap["active"] is True


def test_a_contested_assessment_is_preserved_outside_the_display_selection(monkeypatch):
    state, settings, providers, strategies = _run(1)
    consolidate = strategies.claim_consolidation.consolidate

    def disputed(ledger):
        claims = consolidate(ledger)
        for claim in claims:
            if claim["claim_text"] == GAMMA:
                claim["status"] = "contested"
                claim["verification_basis"] = "contested"
        return claims

    # The strategy owns the assessment; the orchestration must carry it as is.
    monkeypatch.setattr(strategies.claim_consolidation, "consolidate", disputed)
    state["queries"].append("Weitere Messwerte")
    search(state, providers=providers, strategies=strategies, settings=settings)
    assert _statuses(state)[GAMMA] == "contested"
    assert state["claim_status_counts"] == {"verified": 2, "attributed": 0, "contested": 1, "unverified": 0}
    assert len(state["consolidated_claims"]) == 1


def test_crosscheck_planning_considers_claims_outside_the_display_selection(monkeypatch):
    state, settings, providers, strategies = _run(1)
    prompts = []

    def capture(prompt, **kwargs):
        prompts.append(prompt)
        return '{"queries": ["Gamma-Anteil Primaerquelle"]}'

    monkeypatch.setattr(providers.llm, "complete", capture)
    plan(state, providers=providers, strategies=strategies, settings=settings)
    assert any(GAMMA in prompt for prompt in prompts)


@pytest.mark.parametrize("cap", [0, 1])
def test_evaluator_keeps_its_claim_list_bounded_while_reading_full_quality(monkeypatch, cap):
    state, settings, providers, strategies = _run(cap)
    displayed = []
    guard_inputs = []
    original = strategies.claim_consolidation.claims_prompt_view
    original_guard = nodes_module.apply_confidence_guardrails

    def capture(claims, **kwargs):
        displayed.extend(claim["claim_text"] for claim in claims)
        return original(claims, **kwargs)

    monkeypatch.setattr(strategies.claim_consolidation, "claims_prompt_view", capture)
    def capture_guard(*args, **kwargs):
        guard_inputs.append(kwargs)
        return original_guard(*args, **kwargs)
    monkeypatch.setattr(nodes_module, "apply_confidence_guardrails", capture_guard)
    monkeypatch.setattr(providers.llm, "complete", lambda *args, **kwargs:
                        "STATUS: INSUFFICIENT\nCONFIDENCE: 5\nGAPS: Offene Belege\n"
                        "CONTRADICTIONS: Nein\nCOVERED_ASPECTS: []\nEVIDENCE_CONSISTENCY: 5\nEVIDENCE_SUFFICIENCY: 5")
    evaluate(state, providers=providers, strategies=strategies, settings=settings)
    assert displayed == ([ALPHA] if cap else [])
    assert guard_inputs[-1]["has_claims"] is True
    assert state["claim_status_counts"] == {"verified": 2, "attributed": 0, "contested": 0, "unverified": 1}


def test_an_empty_display_list_does_not_erase_answer_grounding():
    state, settings, providers, strategies = _run(0)
    answer(state, providers=providers, strategies=strategies, settings=settings)
    assert state["consolidated_claims"] == []
    assert state["evidence_contract_status"] == "clean"
    assert {claim["claim_text"] for claim in state["consolidated_claims_full"] if claim.get("used_in_answer")} == {ALPHA, BETA}
