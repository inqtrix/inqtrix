"""Deterministic offline end-to-end harness for the research graph.

Every scenario drives the REAL ``inqtrix.graph.run`` pipeline
(classify -> plan -> search -> evaluate -> answer). Fakes sit ONLY at the
LLM / search I/O boundary (:mod:`tests._fake_providers`); every node,
strategy, consolidation and contract computation is the real code.

Assertions read REAL graph outputs — ``ResearchResult.from_raw(raw).metrics``,
the returned ``result_state`` dict, and captured progress messages — never a
value a fake set directly.
"""

from __future__ import annotations

import dataclasses
import queue

import pytest

from inqtrix.core.results import WebRecency
from inqtrix.evidence import assemble_evidence_records
from inqtrix.graph import run
from inqtrix.report_profiles import ReportProfile
from inqtrix.providers.base import ProviderContext
from inqtrix.result import ResearchResult
from inqtrix.search_result import GroundedSearchResult
from inqtrix.runtime_logging import normalize_source_provenance
from inqtrix.settings import AgentSettings
from inqtrix.strategies import StrategyContext, create_default_strategies
from inqtrix.urls import normalize_url

from tests._fake_providers import (
    FakeLLM,
    FakeSearch,
    load_search_result_fixture,
    source_only_result,
    subset_result,
)

_FIXTURE_SLUG = "nvidia_quartalszahlen"
# Real Azure Foundry capture of the SAME question, recorded by a maintainer
# with keys. While it is absent the Azure scenario skips, so the harness
# never asserts against an invented shape.
_AZURE_FIXTURE_SLUG = "nvidia_quartalszahlen_azure"
_QUESTION = "Welche Quartalszahlen hat NVIDIA zuletzt gemeldet?"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _settings() -> AgentSettings:
    """Minimal-loop settings: one round, COMPACT profile, testing mode."""
    return AgentSettings(
        testing_mode=True,
        first_round_queries=2,
        max_rounds=1,
        min_rounds=1,
        report_profile=ReportProfile.COMPACT,
    )


def _drain(progress_queue: "queue.Queue") -> list[tuple[str, str]]:
    """Drain ``(kind, msg)`` tuples emitted during the run."""
    items: list[tuple[str, str]] = []
    while True:
        try:
            items.append(progress_queue.get_nowait())
        except queue.Empty:
            break
    return items


def _progress_messages(events: list[tuple[str, str]]) -> list[str]:
    """Project progress tuples down to their message strings."""
    messages: list[str] = []
    for event in events:
        if isinstance(event, tuple) and len(event) >= 2:
            messages.append(str(event[1]))
        else:
            messages.append(str(event))
    return messages


def _load_fixture_or_skip(slug: str) -> "GroundedSearchResult":
    """Load a captured fixture, or skip the test when it is not yet committed.

    The harness drives only real captured search shapes; it never
    invents one. A provider whose capture a maintainer has not yet produced
    (e.g. Azure Foundry, which needs Azure keys) leaves its fixture absent, so
    the scenario skips with an actionable message instead of failing or being
    silently dropped.
    """
    try:
        return load_search_result_fixture(slug)
    except FileNotFoundError as exc:
        pytest.skip(
            f"missing real capture fixture {slug!r} ({exc}); capture via "
            "scripts/debug_search_dataflow.py, sanitize with "
            "tests/fixtures/sanitize.assert_cassette_clean, and commit it as "
            f"tests/fixtures/search_results/{slug}.json"
        )


def _run_scenario(
    llm: FakeLLM,
    search: FakeSearch,
    settings: AgentSettings | None = None,
    strategies: StrategyContext | None = None,
    web_recency: WebRecency | None = None,
) -> tuple[dict, list[tuple[str, str]]]:
    """Execute the real graph with the given fakes and capture progress."""
    settings = settings or _settings()
    providers = ProviderContext(llm=llm, search=search)
    strategies = strategies or create_default_strategies(
        settings,
        llm=llm,
        claim_extract_model="claim-extract-model",
    )
    progress_queue: "queue.Queue" = queue.Queue()
    raw = run(
        _QUESTION,
        progress_queue=progress_queue,
        providers=providers,
        strategies=strategies,
        settings=settings,
        web_recency=web_recency,
    )
    return raw, _drain(progress_queue)


# Two NVIDIA claims, each cross-checking across two distinct primary/mainstream
# domains via the fixture's real source ranks:
#   rank 3 = investor.nvidia.com (primary),  rank 4 = kiplinger.com (mainstream)
#   rank 9 = stocktitan.net,                 rank 7 = businessinsider.com (mainstream)
# The provider_refs are the source RANKS as strings; the real extractor resolves
# them to source URLs via the citation records derived from the fixture.
# The extractor's judgement of the fixture's sources (who speaks), by
# provider rank: the company's newsroom and investor pages, two news
# outlets, two aggregators, a video platform. Scenarios that pass these
# roles rank their evidence records by them; a scenario without them runs
# with every source unjudged (unknown) and the round's 'nicht beurteilt'
# status line — the documented contract for a non-judging extractor.
_FIXTURE_SOURCE_ROLES = [
    {"ref": "1", "role": "company_primary", "reason": "Newsroom des Unternehmens"},
    {"ref": "2", "role": "company_primary", "reason": "Newsroom des Unternehmens"},
    {"ref": "3", "role": "company_primary", "reason": "Investor Relations"},
    {"ref": "4", "role": "reporting", "reason": "Finanzredaktion"},
    {"ref": "5", "role": "aggregator_unknown", "reason": "Kursportal"},
    {"ref": "6", "role": "reporting", "reason": "Finanzredaktion"},
    {"ref": "7", "role": "reporting", "reason": "Wirtschaftsredaktion"},
    {"ref": "8", "role": "low", "reason": "Videoplattform"},
    {"ref": "9", "role": "aggregator_unknown", "reason": "Nachrichtenaggregator"},
]

_CLEAN_CLAIM_PAYLOAD = [
    {
        "claim_text": "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 einen Rekordumsatz von 81,6 Milliarden US-Dollar.",
        "evidence_snippet": "record revenue for the first quarter ended April 26, 2026, of $81.6 billion",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "provider_refs": ["3", "4"],
        "published_date": "2026-05-20",
    },
]


def _clean_answer_bindings() -> list[dict[str, str]]:
    """Answer-binding hints that restate the clean claim's text verbatim."""
    return [
        {
            "label": "E1",
            "claim_text": (
                "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 "
                "einen Rekordumsatz von 81,6 Milliarden US-Dollar."
            ),
        },
    ]


# --------------------------------------------------------------------------- #
# Scenario: clean
# --------------------------------------------------------------------------- #


def test_explicit_year_recency_overrides_classification_and_reaches_search() -> None:
    """Delegated freshness remains authoritative through the real graph."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)

    class RecordingSearch(FakeSearch):
        def __init__(self) -> None:
            super().__init__(fixture)
            self.kwargs: list[dict[str, object]] = []

        def search(self, query: str, **kwargs: object) -> GroundedSearchResult:
            self.kwargs.append(dict(kwargs))
            return super().search(query, **kwargs)

    search = RecordingSearch()
    raw, _events = _run_scenario(
        FakeLLM(classify_recency="NONE"),
        search,
        web_recency="year",
    )

    assert raw["result_state"]["recency"] == "year"
    assert search.kwargs
    assert all(call["recency_filter"] == "year" for call in search.kwargs)


def test_clean_contract_from_verified_bound_claim() -> None:
    """A cross-checked claim cited in the answer yields ``clean``."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    # The graph really ran end to end: classify, plan, evaluate via complete();
    # the answer node composed sections via complete_with_metadata; claim
    # extraction went through complete_structured.
    assert "classify" in llm.complete_calls
    assert "plan" in llm.complete_calls
    assert "evaluate" in llm.complete_calls
    assert llm.structured_calls >= 1
    assert llm.answer_section_calls >= 1

    # Real consolidation supports the claim (the issuer's own page is among
    # its sources), and the real contract computation in the answer node
    # settled on "clean". The provider's snippets are English page text that
    # carries neither the German claim's words nor its fiscal year, so no
    # source text checked the claim: it is the provider's attribution.
    assert any(
        c.get("status") == "attributed" for c in state.get("consolidated_claims", [])
    ), state.get("consolidated_claims")
    assert result.metrics.evidence_contract_status == "clean"
    assert result.metrics.answer_bound_claims_count >= 1

    # I3: every statement the evidence layer ties to a source in the report's
    # references is readable at that reference, with its status.
    by_url = {reference.url: reference for reference in result.references}
    checked_any = False
    for record in state.get("evidence_ledger", []):
        reference = by_url.get(normalize_url(str(record.get("canonical_url", ""))))
        if reference is None:
            continue
        statements = {statement.text: statement for statement in reference.statements}
        for claim in record.get("claims", []) or []:
            text = " ".join(str(claim.get("claim_text", "")).split())
            assert text in statements, (reference.url, text)
            assert statements[text].status == claim.get("verification_status")
            checked_any = True
    assert checked_any, "precondition: a referenced source carries a claim"


def test_answer_diagnostics_reflect_routed_model_and_effort() -> None:
    """Answer diagnostics name the routed answer-tier model + effort.

    ``answer_prompt_inputs.model``, ``answer_section.model`` and the
    ``node_model_resolution`` event must reflect the model and effort actually
    routed to the answer node, not the provider's ``reasoning_model`` default
    or an empty string.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    # Route the answer node (high tier) to a model distinct from reasoning_model,
    # with a graded effort so thinking_likely_active must be True.
    llm.models.tier_high_model = "answer-tier-model"
    llm.models.tier_high_effort = "high"

    settings = AgentSettings(
        testing_mode=True,
        observability_profile="forensic",
        first_round_queries=2,
        max_rounds=1,
        min_rounds=1,
        report_profile=ReportProfile.COMPACT,
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture), settings)
    logs = raw["result_state"].get("iteration_logs", [])

    prompt_inputs = next(e for e in logs if e.get("event") == "answer_prompt_inputs")
    assert prompt_inputs["model"] == "answer-tier-model"

    section_events = [e for e in logs if e.get("event") == "answer_section"]
    assert section_events
    assert all(e["model"] == "answer-tier-model" for e in section_events)
    assert all(e["thinking_likely_active"] is True for e in section_events)

    answer_resolution = next(
        e
        for e in logs
        if e.get("event") == "node_model_resolution" and e.get("node") == "answer"
    )
    assert answer_resolution["model"] == "answer-tier-model"
    assert answer_resolution["effort"] == "high"


def test_search_node_tolerates_old_signature_claim_strategy() -> None:
    """A custom claim strategy on the pre-routing signature must not break.

    The search node forwards `model`/`reasoning_effort` only to extractors whose
    signature accepts them (Baukasten backward-compat). A strategy written
    against the older `extract(...)` signature must run through the real graph
    without `TypeError`.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings())

    class _OldSignatureExtractor:
        """Mirrors the ClaimExtractionStrategy.extract signature before routing."""

        def __init__(self) -> None:
            self.calls = 0

        def extract(
            self, text, citations, question, *, deadline=None, provider_refs=None,
            text_char_limit=7000, citation_cap=8, max_claims=8, source_url_limit=4,
        ):
            self.calls += 1
            return [], 0, 0

        def consume_nonfatal_notice(self):
            return None

        def consume_extraction_metadata(self):
            return {}

    settings = _settings()
    base = create_default_strategies(settings, llm=llm, claim_extract_model="x")
    old = _OldSignatureExtractor()
    strategies = dataclasses.replace(base, claim_extraction=old)

    # Must not raise TypeError on the model= / reasoning_effort= kwargs.
    raw, _events = _run_scenario(llm, FakeSearch(fixture), settings, strategies)

    assert old.calls >= 1  # the old-signature extractor really ran
    assert raw["result_state"].get("answer")  # the graph completed end to end


# --------------------------------------------------------------------------- #
# Scenario: mixed (verified claim cited as source-only, not restated)
# --------------------------------------------------------------------------- #


def test_mixed_source_only_citation_does_not_inflate_bound_claims() -> None:
    """A verified claim cited but not restated must not count as a bound claim.

    The single extracted claim cross-checks (provider_refs 3,4) and consolidates
    to ``verified``; its primary source (investor.nvidia.com) therefore carries a
    ``primary-source`` evidence record. The answer cites that source WITHOUT
    restating the claim, so the two binding layers disagree on purpose:

    * the URL-coarse audit (``audit_answer_evidence_bindings``) sees the cited
      URL resolve to a verified record -> ``matched``;
    * the claim-level binding (``_build_answer_claim_bindings``) finds no segment
      that plausibly carries the claim -> ``source_only_binding`` (no match).

    The contract follows the claim-level signal and is ``source_context_only``.
    The public ``answer_bound_claims_count`` must agree with the contract and
    report 0. A count that summed both binding lists would let the URL-coarse
    ``matched`` leak in, so a source_context_only report would claim a bound
    claim -- the same overstatement the single canonical contract prevents.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    # Same verified claim as the clean scenario, but NO answer bindings: the
    # answer cites a source label without restating the claim text.
    llm = FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=[])
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    # Real consolidation supports the claim (attributed: see the clean case).
    assert any(
        c.get("status") == "attributed" for c in state.get("consolidated_claims", [])
    ), state.get("consolidated_claims")

    claim_bindings = state.get("answer_claim_bindings", [])
    evidence_bindings = state.get("answer_evidence_bindings", [])
    # This is genuinely the MIXED shape: URL-coarse matched present, but no
    # claim-level matched (the cited sentence does not restate the claim).
    assert any(
        b.get("binding_status") == "matched" for b in evidence_bindings
    ), evidence_bindings
    assert not any(
        b.get("binding_status") == "matched" for b in claim_bindings
    ), claim_bindings

    # Contract follows the claim-level signal: no bound claim.
    assert result.metrics.evidence_contract_status == "source_context_only"
    # The public metric must agree with the contract, not with the URL audit.
    assert result.metrics.answer_bound_claims_count == 0


# --------------------------------------------------------------------------- #
# Scenario: mixed-but-unbacked (matched + citation_without_claim)
# --------------------------------------------------------------------------- #


def test_matched_plus_uncited_source_downgrades_to_needs_review() -> None:
    """A matched claim mixed with a claimless citation is needs_review, not clean.

    The answer restates the verified claim at its primary source (E1 ->
    investor.nvidia.com, ``matched``) but ALSO cites a second source
    (E3 -> nvidianews.nvidia.com, the company's newsroom: primary by role but
    without a claim) that backs no consolidated claim. The URL
    audit sees no ``unknown_citation`` -- that second source is in the ledger,
    merely claimless -- so a single match alone would score ``clean``. The
    claim-level ``citation_without_claim`` signal (the twin of
    ``unknown_citation``) forces ``needs_review``.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        source_roles=_FIXTURE_SOURCE_ROLES,
        answer_bindings=[
            {"label": "E1", "claim_text": _CLEAN_CLAIM_PAYLOAD[0]["claim_text"]},
            {"label": "E3", "claim_text": "Ergaenzender Hintergrund ohne belegte Kennzahl."},
        ],
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    claim_bindings = state.get("answer_claim_bindings", [])
    evidence_bindings = state.get("answer_evidence_bindings", [])
    statuses = {b.get("binding_status") for b in claim_bindings}
    # Genuinely mixed: at least one matched AND at least one citation_without_claim.
    assert "matched" in statuses, claim_bindings
    assert "citation_without_claim" in statuses, claim_bindings
    # The downgrade is driven by citation_without_claim, NOT by an
    # unknown_citation (the second source is in the ledger, just claimless).
    assert not any(
        b.get("binding_status") == "unknown_citation" for b in evidence_bindings
    ), evidence_bindings

    assert result.metrics.evidence_contract_status == "needs_review"
    # _CONTRACT_CONFIDENCE_CAP["needs_review"] == 6.
    assert state["final_confidence"] <= 6


# --------------------------------------------------------------------------- #
# Scenario: matched + tolerated source-only binding (stays clean)
# --------------------------------------------------------------------------- #


def test_matched_plus_source_only_binding_stays_clean() -> None:
    """A matched claim plus a tolerated source-only citation stays clean.

    The answer restates the verified claim at its primary source (E1 ->
    investor.nvidia.com, ``matched``) and also cites the claim's OTHER
    cross-check source (E2 -> kiplinger.com, a newsroom by role) WITHOUT
    restating it. That second
    citation is a ``source_only_binding`` -- the cited URL does carry a
    consolidated claim, this sentence just does not assert it. Such background
    citations are deliberately tolerated, so the contract stays ``clean``: the
    severity reorder must not treat source_only_binding as unsubstantiated.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        source_roles=_FIXTURE_SOURCE_ROLES,
        answer_bindings=[
            {"label": "E1", "claim_text": _CLEAN_CLAIM_PAYLOAD[0]["claim_text"]},
            {"label": "E2", "claim_text": "Marktkontext zur Quartalsmeldung."},
        ],
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    claim_bindings = state.get("answer_claim_bindings", [])
    statuses = {b.get("binding_status") for b in claim_bindings}
    # Genuinely the tolerated mix: matched + source_only_binding, nothing unbacked.
    assert "matched" in statuses, claim_bindings
    assert "source_only_binding" in statuses, claim_bindings
    assert "citation_without_claim" not in statuses, claim_bindings

    assert result.metrics.evidence_contract_status == "clean"


# --------------------------------------------------------------------------- #
# Scenario: source_context_only
# --------------------------------------------------------------------------- #


def test_provider_grounded_search_is_preserved_without_page_fetch() -> None:
    """The provider answer and citations form the durable web-search ledger."""

    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    raw, _events = _run_scenario(
        FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD),
        FakeSearch(fixture),
    )

    result = ResearchResult.from_raw(raw)
    assert result.web_search_ledger is not None
    searches = result.web_search_ledger["searches"]
    assert searches
    assert any(search["provider_answer"] for search in searches.values())
    assert any(search["citations"] for search in searches.values())


def test_source_context_only_when_no_claims_extracted() -> None:
    """No extracted claims -> ``source_context_only`` and capped confidence."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    # claim_payload defaults to [] -> valid-empty extraction (no ALGO-FAIL).
    llm = FakeLLM(claim_payload=[])
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    assert state.get("consolidated_claims") == []
    assert result.metrics.evidence_contract_status == "source_context_only"
    # _CONTRACT_CONFIDENCE_CAP["source_context_only"] == 4.
    assert state["final_confidence"] <= 4

    # With no consolidated claims there can be no claim-level "matched" binding,
    # so the claim-grounded metric reports 0 -- consistent with the contract.
    assert result.metrics.answer_bound_claims_count == 0


# --------------------------------------------------------------------------- #
# Scenario: source-only (answer stripped, sources kept)
# --------------------------------------------------------------------------- #


def test_source_only_search_still_ingests_sources_as_evidence() -> None:
    """An empty answer with real sources still produces report-eligible evidence."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    search = FakeSearch(source_only_result(fixture))
    # No answer text -> claim extraction is never invoked -> no claims.
    llm = FakeLLM(claim_payload=[])
    raw, events = _run_scenario(llm, search)

    state = raw["result_state"]
    ledger = state.get("evidence_ledger", [])

    report_eligible = [r for r in ledger if r.get("report_eligible")]
    assert report_eligible, "source-only search should yield report-eligible records"

    # The captured fixture URLs reach the real citation pool (all_citations).
    assert state.get("all_citations"), "source URLs must reach all_citations"
    citations = set(state["all_citations"])
    assert any(normalize_url(u) in citations for u in fixture.citation_urls)

    # A source-only result carries real sources and is ingested as evidence, so
    # it is NOT an empty search: the misleading "no results" notice must not
    # fire. The empty-search gate now also checks res.sources, mirroring the
    # ingestion gate. The German text is "{empty} von {total} Suchanfragen
    # lieferten keine Ergebnisse".
    messages = _progress_messages(events)
    misleading_empty = [
        m
        for m in messages
        if "lieferten keine Ergebnisse" in m or "returned no results" in m
    ]
    assert not misleading_empty, (
        "source-only results carry sources and must not trigger the "
        f"empty-search notice; got: {misleading_empty}"
    )


# --------------------------------------------------------------------------- #
# Scenario: truly empty search
# --------------------------------------------------------------------------- #


def test_empty_search_produces_no_evidence() -> None:
    """A fully empty result yields no citations and no evidence records.

    Also confirms the empty-search gate is not over-corrected: a result with
    neither answer NOR sources is genuinely empty, so the "no results" notice
    SHOULD still fire here (it must only be suppressed when sources are present).
    """
    from inqtrix.search_result import GroundedSearchResult

    llm = FakeLLM(claim_payload=[])
    raw, events = _run_scenario(llm, FakeSearch(GroundedSearchResult()))
    state = raw["result_state"]
    assert state.get("all_citations", []) == []
    assert state.get("evidence_ledger", []) == []

    # No citable evidence base at all -> the contract is the legitimate,
    # uncapped ``unknown`` (audit not applicable). This is the boundary partner
    # of test_uncited_body_over_real_evidence_is_capped: ``unknown`` requires
    # NO citable evidence, not merely an uncited body.
    result = ResearchResult.from_raw(raw)
    assert result.metrics.evidence_contract_status == "unknown"

    # Genuinely empty -> the empty-search notice still fires (no over-correction).
    messages = _progress_messages(events)
    assert any(
        "lieferten keine Ergebnisse" in m or "returned no results" in m
        for m in messages
    ), messages


# --------------------------------------------------------------------------- #
# Fixture roundtrip sanity (proves the captured shape is pipeline-valid)
# --------------------------------------------------------------------------- #


def test_fixture_roundtrip_through_real_provenance_and_evidence() -> None:
    """The fixture flows through the real provenance + evidence assembly."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    assert fixture.sources, "fixture must carry sources"
    assert all(s.rank for s in fixture.sources), "ranks drive provider-ref binding"

    source_records, citation_records = normalize_source_provenance(
        fixture,
        query_id="q-roundtrip",
        provider="FakeSearch",
    )
    assert source_records, "real provenance must yield source records"
    assert citation_records, "real provenance must yield citation records"
    # Ranks survive into citation records (what _claim_provider_refs reads).
    assert {str(r["rank"]) for r in citation_records} >= {"1", "3", "4"}

    evidence_records = assemble_evidence_records(
        query_id="q-roundtrip",
        query=_QUESTION,
        provider="FakeSearch",
        source_records=source_records,
        citation_records=citation_records,
        claim_entries=[],
    )
    assert evidence_records, "evidence assembly must yield records"
    assert all(r.get("report_eligible") for r in evidence_records), (
        "every fixture source carries a citable URL -> report_eligible"
    )
    assert {r["canonical_url"] for r in evidence_records}, "records carry URLs"


# --------------------------------------------------------------------------- #
# Scenario: multi-query Evidence-Ledger combination (cross-query cross-check)
# --------------------------------------------------------------------------- #


def test_multi_query_results_combine_and_cross_check_in_the_ledger() -> None:
    """Distinct per-query results combine into one cross-checked claim.

    The plan emits two queries; FakeSearch serves a DIFFERENT single-source
    reply for each (query A -> investor.nvidia.com, query B -> kiplinger.com --
    both real fixture sources, re-ranked to 1). The same claim
    (``provider_refs == ["1"]``) is extracted from both, so it resolves to a
    different domain per query. Consolidation merges the two by signature:
    support across two distinct domains -> ``verified`` cross-check.

    Crucially, neither query alone could yield this -- each carried exactly ONE
    source, which would be single-source, not cross-checked. So a passing
    assertion proves the Evidence Ledger genuinely COMBINES multiple queries
    (cross-query consolidation + cross-checking), not merely replays one. This
    closes the single-query blind spot in the earlier scenarios, where every
    query saw the same fixture and collapsed on dedup.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    claim_text = _CLEAN_CLAIM_PAYLOAD[0]["claim_text"]
    per_query_answer = f"{claim_text}[1]"
    # Two disjoint single-source replies built from the real fixture sources.
    result_a = subset_result(fixture, [3], answer=per_query_answer)  # investor.nvidia.com
    result_b = subset_result(fixture, [4], answer=per_query_answer)  # kiplinger.com
    # Same claim, citing each query's rank-1 source; each query's page words
    # the evidence its own way (a reprint of one text would be one witness).
    claim_payload = [{**_CLEAN_CLAIM_PAYLOAD[0], "provider_refs": ["1"]}]
    second_wording = {
        **claim_payload[0],
        "evidence_snippet": "Nvidia reported first-quarter revenue of $81.6 billion, a record, the company said in its release.",
    }
    llm = FakeLLM(claim_payload=claim_payload, claim_payloads_by_call=[claim_payload, [second_wording]])
    raw, _events = _run_scenario(llm, FakeSearch(results=[result_a, result_b]))
    state = raw["result_state"]

    url_a = normalize_url(result_a.sources[0].url)
    url_b = normalize_url(result_b.sources[0].url)
    assert url_a and url_b and url_a != url_b, (url_a, url_b)

    # One claim, supported across the TWO queries' domains.
    supported = [
        c for c in state.get("consolidated_claims", [])
        if c.get("status") in {"verified", "attributed"}
    ]
    assert supported, state.get("consolidated_claims")
    cross_query = next(
        (
            c
            for c in supported
            if {url_a, url_b} <= {normalize_url(u) for u in c.get("source_urls", [])}
        ),
        None,
    )
    assert cross_query is not None, (
        "expected a claim supported across BOTH queries' sources; got "
        f"{[c.get('source_urls') for c in supported]}"
    )
    assert int(cross_query.get("support_count", 0)) >= 2, cross_query

    # The citation pool / ledger combined both queries' distinct sources.
    citations = {normalize_url(u) for u in state.get("all_citations", [])}
    assert url_a in citations and url_b in citations, citations
    ledger_urls = {
        normalize_url(r.get("canonical_url", ""))
        for r in state.get("evidence_ledger", [])
        if r.get("report_eligible")
    }
    assert url_a in ledger_urls and url_b in ledger_urls, ledger_urls


# --------------------------------------------------------------------------- #
# Scenario: Azure Foundry shape (real capture required; skips until present)
# --------------------------------------------------------------------------- #


def test_azure_foundry_shape_drives_the_graph() -> None:
    """The graph handles Azure Foundry's URL-only, snippet-less search shape.

    Azure Foundry's normalized ``GroundedSearchResult`` differs structurally
    from Perplexity's: URL-only sources (``origin="url_citation"``, NO
    per-source snippet) and ``([domain](url))`` inline answer citations instead
    of numeric ``[id]``. The other scenarios all run on the Perplexity fixture,
    which risks over-fitting the harness to one provider's rich-snippet shape.
    This drives the REAL graph on a REAL Azure capture to prove the pipeline
    (provenance, evidence assembly, contract) is provider-shape agnostic.

    Skips until a sanitized real Azure capture is committed (the harness uses
    only real captured shapes, never invented ones). To enable::

        uv run python scripts/debug_search_dataflow.py --provider azure \\
            --query "Welche Quartalszahlen hat NVIDIA zuletzt gemeldet?"

    then gate the JSON through ``tests/fixtures/sanitize.assert_cassette_clean``
    and commit ``stage_1_grounded_search_result`` as
    ``tests/fixtures/search_results/nvidia_quartalszahlen_azure.json``.
    """
    fixture = _load_fixture_or_skip(_AZURE_FIXTURE_SLUG)

    # Guard that the committed capture really is the Azure shape: URL-only
    # sources with no per-source body. If a future capture violates this, the
    # message points at the mismatch rather than failing cryptically downstream.
    assert fixture.sources, "Azure capture must carry cited sources"
    assert all(not (s.snippet or "").strip() for s in fixture.sources), (
        "Azure Foundry exposes no per-source snippet; the captured fixture "
        "should reflect that snippet-less shape"
    )

    # No claim payload: the snippet-less shape flows through as source-context
    # evidence -- the path most exposed to a Perplexity-snippet assumption.
    llm = FakeLLM(claim_payload=[])
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]

    # URL-only, snippet-less sources still reach the ledger as report-eligible
    # evidence and the citation pool -- the core provider-agnostic guarantee.
    report_eligible = [
        r for r in state.get("evidence_ledger", []) if r.get("report_eligible")
    ]
    assert report_eligible, "Azure URL-only sources must still yield evidence records"
    citations = {normalize_url(u) for u in state.get("all_citations", [])}
    assert any(normalize_url(u) in citations for u in fixture.citation_urls), citations

    # The contract was computed on the different shape (graph did not crash).
    result = ResearchResult.from_raw(raw)
    assert result.metrics.evidence_contract_status in {
        "clean",
        "needs_review",
        "source_context_only",
        "algorithm_failed",
        "unknown",
    }


def test_azure_foundry_shape_reaches_clean_contract() -> None:
    """The full claim -> clean-contract pipeline is provider-shape agnostic.

    Beyond ingestion: a claim binds to two of Azure's URL-only, snippet-less
    sources (investor.nvidia.com rank 1, the issuer's own page, and
    businessinsider rank 2); Azure returns no page text, so the claim is the
    provider's attribution with a primary-source structure. The answer
    restates it citing that source, and the contract reaches ``clean``. This
    proves claim extraction,
    ``provider_refs`` resolution against Azure citation records, answer binding
    and the contract all work on the Azure shape -- not just source ingestion --
    so the harness is not over-fit to Perplexity's ``[id]``/rich-snippet shape.
    """
    fixture = _load_fixture_or_skip(_AZURE_FIXTURE_SLUG)
    claim_text = (
        "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 einen Rekordumsatz."
    )
    claim_payload = [
        {
            "claim_text": claim_text,
            "evidence_snippet": "Q1 FY2027 record revenue",
            "claim_type": "fact",
            "polarity": "affirmed",
            "needs_primary": False,
            "provider_refs": ["1", "2"],
            "published_date": "2026-05-20",
        }
    ]
    llm = FakeLLM(
        claim_payload=claim_payload,
        answer_bindings=[{"label": "E1", "claim_text": claim_text}],
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    # Supported, never verified: Azure returns no source text to check.
    assert any(
        c.get("status") == "attributed" and c.get("verification_basis") == "primary_source"
        for c in state.get("consolidated_claims", [])
    ), state.get("consolidated_claims")
    assert not any(
        c.get("status") == "verified" for c in state.get("consolidated_claims", [])
    ), state.get("consolidated_claims")
    assert result.metrics.evidence_contract_status == "clean"
    assert result.metrics.answer_bound_claims_count >= 1


# --------------------------------------------------------------------------- #
# Scenario: uncited body over real evidence is capped (not uncapped "unknown")
# --------------------------------------------------------------------------- #


def test_uncited_body_over_real_evidence_is_capped() -> None:
    """An answer that ignores gathered evidence does not escape uncapped.

    The run gathers real evidence (a verified claim, report-eligible records)
    but the LLM writes factual body prose with NO citations. The body-citation
    audit is therefore empty -- yet citable evidence WAS available. Such a body
    must not score the uncapped ``unknown`` contract (which would let the
    reference appendix make it look sourced at full confidence); it falls to
    ``source_context_only`` (cap 4) like any other claimless answer. ``unknown``
    is reserved for runs with no citable evidence base at all (direct-LLM /
    empty search), which the truly-empty scenario covers.
    """
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    uncited_body = (
        "## Antwort\n\n"
        "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 einen "
        "Rekordumsatz von 81,6 Milliarden US-Dollar, deutlich ueber dem Vorjahr.\n"
    )
    # Claim present (so evidence/claims exist), but the body cites nothing.
    llm = FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_override=uncited_body)
    raw, _events = _run_scenario(llm, FakeSearch(fixture))

    result = ResearchResult.from_raw(raw)
    state = raw["result_state"]

    # Evidence really was available to ground in ...
    assert state.get("all_citations"), "run must have gathered citable evidence"
    assert any(
        r.get("report_eligible") for r in state.get("evidence_ledger", [])
    ), "run must have report-eligible evidence records"
    # ... but the body bound none of it.
    assert state.get("answer_evidence_bindings") == [], state.get(
        "answer_evidence_bindings"
    )

    # So the contract is the capped source_context_only, NOT uncapped unknown.
    assert result.metrics.evidence_contract_status == "source_context_only"
    assert state["final_confidence"] <= 4


def test_english_classified_run_composes_english_headings() -> None:
    """Headings are part of the answer: the composer resolves the section
    keys through the answer language the classifier returned, so an
    English run never carries German section titles (and vice versa)."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        classify_language="en",
        classify_search_language="en",
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    assert (state["language"], state["language_source"]) == ("en", "classified")
    assert "## Summary" in raw["answer"] and "## Key points" in raw["answer"]
    assert "## Kurzfazit" not in raw["answer"] and "## Kernaussagen" not in raw["answer"]
    # The system-generated appendix and footer follow the answer language too.
    assert "## References" in raw["answer"] and "Referenzen" not in raw["answer"]
    assert " round" in raw["answer"] and "Runde" not in raw["answer"]
