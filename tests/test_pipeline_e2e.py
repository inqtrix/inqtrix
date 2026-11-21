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
import re

import pytest

from inqtrix.core.results import WebRecency
from inqtrix.evidence import assemble_evidence_records
from inqtrix.exceptions import AzureOpenAIAPIError
from inqtrix.graph import run
from inqtrix.report_profiles import ReportProfile
from inqtrix.providers.base import ProviderContext
from inqtrix.result import ResearchResult
from inqtrix.search_result import CitationSpan, GroundedSearchResult, GroundedSource
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
    run_event_sink=None,
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
        run_event_sink=run_event_sink,
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

    # Every statement the evidence layer ties to a source in the report's
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


_NOT_HANDED = "not handed"
"""A call that did not get the keyword at all; ``None`` would be a keyword an older provider cannot take."""


class _RecordingLLM(FakeLLM):
    """Keeps the ``verbosity`` every call of the run was handed, by kind of call."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.verbosity_by_call: list[tuple[str, object]] = []
        self.section_calls: list[tuple[object, object]] = []

    def complete(self, prompt, **kwargs):
        self.verbosity_by_call.append(("text", kwargs.get("verbosity", _NOT_HANDED)))
        return super().complete(prompt, **kwargs)

    def complete_with_metadata(self, prompt, **kwargs):
        self.verbosity_by_call.append(("section", kwargs.get("verbosity", _NOT_HANDED)))
        self.section_calls.append((kwargs.get("model"), kwargs.get("verbosity", _NOT_HANDED)))
        return super().complete_with_metadata(prompt, **kwargs)

    def complete_structured(self, prompt, **kwargs):
        self.verbosity_by_call.append(("structured", kwargs.get("verbosity", _NOT_HANDED)))
        return super().complete_structured(prompt, **kwargs)


class _VerbosityLLM(_RecordingLLM):
    """A provider that says it can send the parameter."""

    def supports_verbosity(self, *, model: str | None = None) -> bool:
        return True


def _run_on_a_model_with_a_prose_verbosity(llm: _RecordingLLM) -> dict:
    """One clean run in which every node resolves to a model whose card names a prose verbosity."""
    llm.models.reasoning_model = "gpt-6.1-terra"
    raw, _events = _run_scenario(llm, FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)))
    assert raw["result_state"]["node_model_resolutions"]["classify"]["model"] == "gpt-6.1-terra"
    return raw


def test_only_the_report_sections_ask_for_the_prose_verbosity_of_their_model() -> None:
    """Classify, plan, evaluate and the claim extraction run on the same model and ask for nothing."""
    llm = _VerbosityLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings())

    raw = _run_on_a_model_with_a_prose_verbosity(llm)

    by_kind: dict[str, set[object]] = {}
    for kind, verbosity in llm.verbosity_by_call:
        by_kind.setdefault(kind, set()).add(verbosity)
    assert by_kind == {"text": {_NOT_HANDED}, "structured": {_NOT_HANDED}, "section": {"high"}}
    resolutions = raw["result_state"]["node_model_resolutions"]
    assert resolutions["answer"]["verbosity"] == "high"
    assert resolutions["evaluate"]["verbosity"] == ""


def test_a_provider_that_cannot_send_the_verbosity_is_not_handed_it() -> None:
    llm = _RecordingLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings())

    _run_on_a_model_with_a_prose_verbosity(llm)

    assert {verbosity for _kind, verbosity in llm.verbosity_by_call} == {_NOT_HANDED}
    assert llm.answer_section_calls >= 1


def test_a_model_without_the_rule_writes_its_sections_as_before() -> None:
    llm = _VerbosityLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings())

    _run_scenario(llm, FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)))

    assert {verbosity for _kind, verbosity in llm.verbosity_by_call} == {_NOT_HANDED}
    assert llm.answer_section_calls >= 1


class _FailingOnOneModelLLM(_VerbosityLLM):
    """Refuses every section of one model the way an overloaded service does."""

    def __init__(self, *, failing_model: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.failing_model = failing_model

    def complete_with_metadata(self, prompt, **kwargs):
        if kwargs.get("model") == self.failing_model:
            self.section_calls.append((kwargs.get("model"), kwargs.get("verbosity", _NOT_HANDED)))
            raise AzureOpenAIAPIError(
                model=self.failing_model, status_code=503, error_code="server_error", message="overloaded"
            )
        return super().complete_with_metadata(prompt, **kwargs)


@pytest.mark.parametrize(
    ("routed", "fallback", "routed_level", "fallback_level"),
    [
        ("gpt-6.1-terra", "gpt-5.4", "high", _NOT_HANDED),
        ("gpt-5.4", "gpt-6.1-terra", _NOT_HANDED, "high"),
    ],
)
def test_a_fallback_model_writes_with_the_level_of_its_own_card(
    routed: str, fallback: str, routed_level: object, fallback_level: object
) -> None:
    """The level follows the model that writes; the resolution keeps naming the routed one."""
    llm = _FailingOnOneModelLLM(
        failing_model=routed,
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    llm.models.tier_high_model = routed
    llm.models.tier_mid_model = fallback

    raw, _events = _run_scenario(llm, FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)))

    by_model: dict[object, set[object]] = {}
    for model, verbosity in llm.section_calls:
        by_model.setdefault(model, set()).add(verbosity)
    assert by_model == {routed: {routed_level}, fallback: {fallback_level}}
    resolution = raw["result_state"]["node_model_resolutions"]["answer"]
    assert resolution["model"] == routed
    assert resolution["verbosity"] == ("" if routed_level is _NOT_HANDED else routed_level)


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
# Why the contract reads as it does: cause, ceiling and basis as fields
# --------------------------------------------------------------------------- #

# The reader's words under a German report, spelled out here: a reworded or
# swapped text must be caught at the answer, not read back from the table.
_NOTICE_HEADING = "## Evidenzhinweis"
_NOTICE_BY_CAUSE = {
    "unknown_citation": "Mindestens ein zitierter Link ließ sich keinem Beleg zuordnen.",
    "citation_without_claim": "Zu mindestens einer zitierten Quelle ist keine Aussage erfasst.",
    "algorithm_failure": "Mindestens ein Schritt der Recherche ist fehlgeschlagen.",
}
_PROVIDER_BASIS_LINE = (
    "Grundlage: die Angaben des Suchanbieters zu den zitierten Quellen; "
    "die Seiten selbst wurden nicht gelesen."
)
_FOOTER_RULE = "\n\n---\n*"


def _depth_numbers(multi: int, supported: int) -> str:
    return (
        "Gestützte Aussagen der Recherche mit mehr als einer Quelle: "
        f"{multi} von {supported}."
    )


def _assert_notice_names_only(answer: str, cause: str) -> None:
    """The notice carries the heading and the text of *cause*, no other's."""
    assert _NOTICE_HEADING in answer, answer[-1500:]
    assert _NOTICE_BY_CAUSE[cause] in answer, answer[-1500:]
    others = [text for name, text in _NOTICE_BY_CAUSE.items() if name != cause]
    assert not [text for text in others if text in answer], answer[-1500:]


def _claim(text: str, snippet: str, refs: list[str]) -> dict:
    return {
        "claim_text": text,
        "evidence_snippet": snippet,
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "provider_refs": refs,
        "published_date": "2026-05-20",
    }


_THREE_CLAIMS_ONE_SOURCE = [
    _claim(
        "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 einen Rekordumsatz von 81,6 Milliarden US-Dollar.",
        "record revenue for the first quarter ended April 26, 2026, of $81.6 billion",
        ["3"],
    ),
    _claim(
        "Der Umsatz im Rechenzentrumsgeschaeft von NVIDIA erreichte 75,2 Milliarden US-Dollar.",
        "Data Center revenue of $75.2 billion",
        ["3"],
    ),
    _claim(
        "NVIDIA erwartet fuer das zweite Quartal einen Umsatz von 91,0 Milliarden US-Dollar.",
        "revenue is expected to be $91.0 billion",
        ["3"],
    ),
]

_FOUR_CLAIMS_TWO_PRIMARY_SOURCES = [
    _claim(
        "NVIDIA meldete fuer das erste Quartal Geschaeftsjahr 2027 einen Rekordumsatz von 81,6 Milliarden US-Dollar.",
        "record revenue for the first quarter ended April 26, 2026, of $81.6 billion",
        ["1", "3"],
    ),
    _claim(
        "Der Umsatz im Rechenzentrumsgeschaeft von NVIDIA erreichte 75,2 Milliarden US-Dollar.",
        "Data Center revenue of $75.2 billion",
        ["2", "3"],
    ),
    _claim(
        "NVIDIA erwartet fuer das zweite Quartal einen Umsatz von 91,0 Milliarden US-Dollar.",
        "revenue is expected to be $91.0 billion",
        ["1", "3"],
    ),
    _claim(
        "Die Bruttomarge von NVIDIA lag im ersten Quartal bei 74,9 Prozent.",
        "GAAP gross margin of 74.9%",
        ["2", "3"],
    ),
]


def test_clean_run_names_its_basis_and_carries_no_ceiling() -> None:
    """A clean web run names the material behind its cited references:
    what the search provider returned, no stored source text. The report
    says so in one quiet line right above the footer, without a notice."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    metrics = ResearchResult.from_raw(raw).metrics

    assert metrics.evidence_contract_status == "clean"
    assert metrics.evidence_contract_causes == []
    assert metrics.confidence_cap is None
    assert metrics.confidence_cap_reasons == []
    assert metrics.evidence_depth_gap_reasons == []
    assert metrics.evidence_basis == "provider_context"
    assert metrics.cited_reference_count >= 1
    assert metrics.provider_only_reference_count == metrics.cited_reference_count

    # One supported statement with two sources (provider ranks 3 and 4).
    answer = raw["answer"]
    assert f"{_PROVIDER_BASIS_LINE} {_depth_numbers(1, 1)}{_FOOTER_RULE}" in answer, answer[-1500:]
    assert _NOTICE_HEADING not in answer, answer[-1500:]


def test_cited_source_without_statement_is_named_as_the_cause() -> None:
    """The mixed case of the scenario above: the cause is the cited record
    that carries no statement, not a link that could not be assigned."""
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
    metrics = ResearchResult.from_raw(raw).metrics

    assert metrics.evidence_contract_status == "needs_review"
    assert metrics.evidence_contract_causes == ["citation_without_claim"]
    assert metrics.confidence_cap == 6
    assert metrics.confidence_cap_reasons == ["citation_without_claim"]
    # The reader learns the same cause, under the warning heading.
    _assert_notice_names_only(raw["answer"], "citation_without_claim")
    assert _PROVIDER_BASIS_LINE in raw["answer"]


def test_a_failed_step_is_named_as_the_cause() -> None:
    """A step that failed without blocking the report (the query plan could
    not be read, the run fell back to the question itself) holds an
    otherwise clean run at needs_review, and the result says which cause."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)

    class UnreadablePlanLLM(FakeLLM):
        def _plan_block(self) -> str:
            return "keine Liste von Suchanfragen"

    llm = UnreadablePlanLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    metrics = ResearchResult.from_raw(raw).metrics

    failures = state.get("algorithm_failures") or []
    assert failures, "precondition: the plan failure was recorded"
    assert not any(failure.get("blocking") for failure in failures), failures
    statuses = {b.get("binding_status") for b in state.get("answer_claim_bindings", [])}
    assert "matched" in statuses and "citation_without_claim" not in statuses, statuses

    assert metrics.evidence_contract_status == "needs_review"
    assert metrics.evidence_contract_causes == ["algorithm_failure"]
    assert metrics.confidence_cap_reasons == ["algorithm_failure"]
    _assert_notice_names_only(raw["answer"], "algorithm_failure")


def test_a_cited_link_without_a_record_is_named_as_the_cause(monkeypatch) -> None:
    """The URL audit's own signal: a cited link that resolves to no record.
    The link allowlist of a run rules it out in normal flow, so the audit's
    result is extended here; the contract still has to name it."""
    import inqtrix.nodes as nodes

    real_audit = nodes.audit_answer_evidence_bindings

    def audit_with_an_unknown_link(answer_body, evidence_ledger):
        return [
            *real_audit(answer_body, evidence_ledger),
            {
                "binding_id": "bind_ev_without_record",
                "citation_url": "https://example.org/ohne-beleg",
                "evidence_id": "",
                "verification": "unknown",
                "binding_status": "unknown_citation",
            },
        ]

    monkeypatch.setattr(nodes, "audit_answer_evidence_bindings", audit_with_an_unknown_link)
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    metrics = ResearchResult.from_raw(raw).metrics

    assert metrics.unbound_answer_citations_count == 1
    assert metrics.evidence_contract_status == "needs_review"
    assert metrics.evidence_contract_causes == ["unknown_citation"]
    assert metrics.confidence_cap_reasons == ["unknown_citation"]
    _assert_notice_names_only(raw["answer"], "unknown_citation")


def test_depth_gap_alone_caps_a_clean_run_at_six() -> None:
    """Three supported statements from one source, nothing cited without a
    record or a statement: the run is clean, and the depth gap alone holds
    the confidence. The evaluator's 8 ends at the ceiling of 6, the ceiling
    names the gap's own reasons, and the report carries the basis line with
    the numbers instead of a warning notice."""
    from inqtrix.agents.kernel.evidence_read import research_evidence_note

    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_THREE_CLAIMS_ONE_SOURCE,
        source_roles=_FIXTURE_SOURCE_ROLES,
        answer_bindings=[
            {"label": "E1", "claim_text": _THREE_CLAIMS_ONE_SOURCE[0]["claim_text"]},
        ],
        evaluate_confidence=8,
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    metrics = ResearchResult.from_raw(raw).metrics

    statuses = {b.get("binding_status") for b in state.get("answer_claim_bindings", [])}
    assert "matched" in statuses, state.get("answer_claim_bindings")
    assert not statuses & {"citation_without_claim"}, statuses
    gap = state["evidence_depth_gap"]
    assert gap["active"] and gap["single_source_count"] == gap["supported_count"] == 3, gap

    assert metrics.evidence_contract_status == "clean"
    assert metrics.evidence_contract_causes == []
    assert metrics.confidence == 6
    assert metrics.confidence_cap == 6
    assert metrics.evidence_depth_gap_reasons == [
        "no_cross_checked_claims", "majority_single_source_claims",
    ]
    assert metrics.confidence_cap_reasons == metrics.evidence_depth_gap_reasons

    answer = raw["answer"]
    assert f"{_PROVIDER_BASIS_LINE} {_depth_numbers(0, 3)}{_FOOTER_RULE}" in answer, answer[-1500:]
    assert _NOTICE_HEADING not in answer, answer[-1500:]
    assert "single-source" not in answer, answer[-600:]
    # A parent run reading this child learns that clean is capped, and why.
    assert research_evidence_note(metrics.model_dump()).startswith(
        "Evidence state of the subtask: clean, confidence 6/10 (ceiling 6: "
        "none of the supported statements counts as cross-checked; most "
        "supported statements cite a single source)."
    )


def test_two_primary_sources_per_statement_are_not_called_single_source() -> None:
    """Four statements with two primary sources each: no statement rests on
    a single source, yet none counts as confirmed by independent witnesses
    because a primary source wins first. The run is clean; the ceiling says
    why and does not claim a single-source majority, and the basis line
    counts every statement with more than one source."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_FOUR_CLAIMS_TWO_PRIMARY_SOURCES,
        source_roles=_FIXTURE_SOURCE_ROLES,
        answer_bindings=[
            {"label": "E1", "claim_text": _FOUR_CLAIMS_TWO_PRIMARY_SOURCES[0]["claim_text"]},
        ],
        evaluate_confidence=8,
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    metrics = ResearchResult.from_raw(raw).metrics

    gap = state["evidence_depth_gap"]
    assert gap["supported_count"] == 4 and gap["single_source_count"] == 0, gap
    assert {
        claim.get("verification_basis") for claim in state["consolidated_claims"]
    } == {"primary_source"}, state["consolidated_claims"]

    assert metrics.evidence_contract_status == "clean"
    assert metrics.evidence_contract_causes == []
    assert metrics.confidence == 6
    assert metrics.confidence_cap == 6
    assert metrics.evidence_depth_gap_reasons == ["no_cross_checked_claims"]
    assert metrics.confidence_cap_reasons == ["no_cross_checked_claims"]
    assert metrics.single_source_ratio == 0.0
    assert _depth_numbers(4, 4) in raw["answer"], raw["answer"][-1500:]


def test_the_research_basis_matches_the_projection_a_review_reads() -> None:
    """The basis a research result reports equals what the evidence
    projection of a review yields for the same cited references and the
    same retained searches, by the same material rule."""
    from inqtrix.agents.evidence_context import project_evidence_context
    from inqtrix.evidence import cited_material_basis

    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_bindings=_clean_answer_bindings(),
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    result = ResearchResult.from_raw(raw)

    cited = [
        reference.model_dump(mode="json")
        for reference in result.references
        if reference.cited
    ]
    assert cited, "precondition: the answer cites a reference"
    projected = project_evidence_context(cited, result.web_search_ledger or {})
    kinds = [source["material_kind"] for source in projected["sources"]]

    assert result.metrics.evidence_basis == cited_material_basis(kinds) == "provider_context"
    assert result.metrics.provider_only_reference_count == kinds.count("provider_context")


class _AnswerProviderDownLLM(FakeLLM):
    """Every report section fails the way an unavailable service does."""

    def complete_with_metadata(self, prompt, **kwargs):
        raise AzureOpenAIAPIError(
            model="reasoning-model", status_code=503, error_code="server_error", message="unavailable"
        )


def _binding_cause_scenario() -> tuple[FakeLLM, AgentSettings]:
    llm = FakeLLM(
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        source_roles=_FIXTURE_SOURCE_ROLES,
        answer_bindings=[
            {"label": "E1", "claim_text": _CLEAN_CLAIM_PAYLOAD[0]["claim_text"]},
            {"label": "E3", "claim_text": "Ergaenzender Hintergrund ohne belegte Kennzahl."},
        ],
    )
    return llm, _settings()


def _source_context_scenario() -> tuple[FakeLLM, AgentSettings]:
    return FakeLLM(claim_payload=[]), _settings()


def _blocked_report_scenario() -> tuple[FakeLLM, AgentSettings]:
    # The forensic profile blocks the report instead of writing a fallback.
    settings = AgentSettings(
        testing_mode=True,
        observability_profile="forensic",
        first_round_queries=2,
        max_rounds=1,
        min_rounds=1,
        report_profile=ReportProfile.COMPACT,
    )
    return _AnswerProviderDownLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD), settings


@pytest.mark.parametrize(
    ("key", "scenario"),
    [
        ("evidence_contract_needs_review", _binding_cause_scenario),
        ("evidence_contract_source_context_only", _source_context_scenario),
        ("algorithm_failure_report_blocked", _blocked_report_scenario),
    ],
)
def test_an_evidence_step_stays_a_warning_when_its_text_changes(monkeypatch, key, scenario) -> None:
    """Each evidence step names its severity itself. Reworded so that no
    word of it names a problem, the step still reaches the run events as a
    warning."""
    from inqtrix.i18n import MESSAGES

    reworded = "Hinweis zur Belegbindung des Berichts"
    monkeypatch.setitem(MESSAGES, key, {"de": reworded, "en": reworded})
    events: list[tuple[str, dict]] = []
    llm, settings = scenario()

    _run_scenario(
        llm,
        FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)),
        settings,
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    steps = [
        payload
        for event_type, payload in events
        if event_type == "inqtrix.progress.message" and payload.get("message") == reworded
    ]
    assert [step["severity"] for step in steps] == ["warning"], steps


class _SmallWindowLLM(FakeLLM):
    """A model whose context window cannot hold a report section's prompt."""

    context_window_tokens = 1_000


class _OutputLimitLLM(FakeLLM):
    """Every report section stops at the model's output limit."""

    def complete_with_metadata(self, prompt, **kwargs):
        response = super().complete_with_metadata(prompt, **kwargs)
        return dataclasses.replace(response, finish_reason="length")


class _ClassifyTimeoutLLM(FakeLLM):
    """The classification call times out; the research goes on without it."""

    def complete(self, prompt, **kwargs):
        if "CLASSIFICATION" in prompt:
            from inqtrix.exceptions import AgentTimeout

            raise AgentTimeout("classification timed out")
        return super().complete(prompt, **kwargs)


class _UnhandledErrorLLM(FakeLLM):
    """Every model call fails with an error no node handles."""

    def complete(self, prompt, **kwargs):
        raise RuntimeError("model unavailable")

    def complete_with_metadata(self, prompt, **kwargs):
        raise RuntimeError("model unavailable")


class _RateLimitedChatLLM(FakeLLM):
    """The chat-only call hits the model service's request limit."""

    def complete_with_metadata(self, prompt, **kwargs):
        from inqtrix.exceptions import AgentRateLimited

        raise AgentRateLimited("reasoning-model", RuntimeError("429 Too Many Requests"))


class _RateLimitedLLM(FakeLLM):
    """Every model call of the research run hits the service's request limit."""

    def complete(self, prompt, **kwargs):
        from inqtrix.exceptions import AgentRateLimited

        raise AgentRateLimited("reasoning-model", RuntimeError("429 Too Many Requests"))

    def complete_with_metadata(self, prompt, **kwargs):
        from inqtrix.exceptions import AgentRateLimited

        raise AgentRateLimited("reasoning-model", RuntimeError("429 Too Many Requests"))


def _chat_only_settings() -> AgentSettings:
    return AgentSettings(testing_mode=True, skip_search=True)


@pytest.mark.parametrize(
    ("key", "make_llm", "make_settings", "raises"),
    [
        # FakeLLM declares no context window, so the answer node warns once.
        (
            "context_window_unknown_warning",
            lambda: FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings()),
            _settings,
            None,
        ),
        ("context_window_too_small", lambda: _SmallWindowLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD), _settings, None),
        (
            "compose_section_token_limit",
            lambda: _OutputLimitLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings()),
            _settings,
            None,
        ),
        (
            "compose_section_truncation",
            lambda: FakeLLM(
                claim_payload=_CLEAN_CLAIM_PAYLOAD,
                answer_override="Ein vollständiger Satz steht hier.\n\nDer Abschnitt bricht ab",
            ),
            _settings,
            None,
        ),
        (
            "classify_failed",
            lambda: _ClassifyTimeoutLLM(
                claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings()
            ),
            _settings,
            None,
        ),
        ("run_failed", lambda: _UnhandledErrorLLM(), _settings, RuntimeError),
        ("chat_mode_failed", lambda: _UnhandledErrorLLM(), _chat_only_settings, RuntimeError),
        ("run_aborted", lambda: _RateLimitedChatLLM(), _chat_only_settings, None),
        # The research path ends at the same request limit as the chat path.
        ("run_rate_limited", lambda: _RateLimitedLLM(), _settings, None),
        # Every section comes back empty, so the writing stops after two.
        ("compose_aborted", lambda: FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_override=""), _settings, None),
    ],
)
def test_a_problem_step_stays_a_warning_when_its_text_changes(
    monkeypatch, key, make_llm, make_settings, raises
) -> None:
    """A step that reports a problem names its severity itself. Reworded so
    that no word of it names a problem, the step still reaches the run
    events as a warning, every time it is sent."""
    from inqtrix.i18n import MESSAGES

    reworded = "Hinweis zum Ablauf der Recherche"
    monkeypatch.setitem(MESSAGES, key, {"de": reworded, "en": reworded})
    events: list[tuple[str, dict]] = []

    def sink(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    search = FakeSearch(load_search_result_fixture(_FIXTURE_SLUG))
    if raises is None:
        _run_scenario(make_llm(), search, make_settings(), run_event_sink=sink)
    else:
        with pytest.raises(raises):
            _run_scenario(make_llm(), search, make_settings(), run_event_sink=sink)

    severities = [
        payload["severity"]
        for event_type, payload in events
        if event_type == "inqtrix.progress.message" and payload.get("message") == reworded
    ]
    assert severities and set(severities) == {"warning"}, severities


def test_an_extraction_that_finds_no_statement_is_an_info_step(monkeypatch) -> None:
    """A page that states nothing the question needs is the ordinary yield of
    a search, so the step informs; the server log keeps its WARNING for the
    operator. Reworded, the step still goes out as info."""
    from inqtrix.i18n import MESSAGES

    reworded = "Hinweis zur Aussagenerfassung"
    monkeypatch.setitem(MESSAGES, "claim_extract_empty", {"de": reworded, "en": reworded})
    events: list[tuple[str, dict]] = []

    def sink(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    search = FakeSearch(load_search_result_fixture(_FIXTURE_SLUG))
    _run_scenario(
        FakeLLM(claim_payload=None, answer_bindings=_clean_answer_bindings()),
        search,
        _settings(),
        run_event_sink=sink,
    )

    severities = [
        payload["severity"]
        for event_type, payload in events
        if event_type == "inqtrix.progress.message" and payload.get("message") == reworded
    ]
    assert severities and set(severities) == {"info"}, severities


# --------------------------------------------------------------------------- #
# Reader texts of a report: no internal codes, in the language of the answer
# --------------------------------------------------------------------------- #

# The internal codes and labels no report text may carry, in any language.
_INTERNAL_REPORT_CODES = (
    "ALGO-FAIL",
    "Evidence-Contract",
    "single-source",
    "mehrfach belegt",
    "Hard-Fact",
    "hard-fact",
    "Evidence-Pfad",
    "Records",
    "Claim-Extraction",
    "evidence ledger",
)
# Every piece of the stats footer, spelled out per language; a piece the
# server adds or rewords outside these forms is caught at the footer.
_FOOTER_PIECE = {
    "de": re.compile(
        r"\d+ (?:Quellen? recherchiert|Belege? für den Bericht bereitgestellt"
        r"|Quellen? im Bericht verlinkt|verifizierte Aussagen?(?: gegengeprüft)?"
        r"|vom Anbieter zugeschriebene Aussagen?|Evidenzbelege? wegen Budget ausgelassen"
        r"|fehlgeschlagener Schritt|fehlgeschlagene Schritte|Suchen?|Runden?)"
        r"|\d+s|\d+:\d{2} min|Konfidenz \d+/10"
    ),
    "en": re.compile(
        r"\d+ (?:sources? researched|evidence records? provided for the report"
        r"|sources? linked in the report|verified statements?(?: cross-checked)?"
        r"|statements? attributed by the search provider|evidence records? omitted for budget"
        r"|failed steps?|search(?:es)?|rounds?)"
        r"|\d+s|\d+:\d{2} min|Confidence \d+/10"
    ),
}
# German marks a fixed text would carry into an English report: real
# umlauts, and words with no English twin.
_GERMAN_MARK = re.compile(
    r"[äöüßÄÖÜ]|\b(?:der|das|und|nicht|kein|keine|wurde|ist|sind|eine|mit|ohne|zur|zum"
    r"|Quelle|Quellen|Runde|Runden|Suchen|Konfidenz|Grundlage|Hinweis|Bericht|Diagnose"
    r"|Fehlende|Einordnung|Ausblick|Unbalancierte|Offener|leer)\b"
)


class _UnreadablePlanLLM(FakeLLM):
    """The query plan cannot be read, so the run searches with the question."""

    def _plan_block(self) -> str:
        return "keine Liste von Suchanfragen"


def _footer_pieces(answer: str) -> list[str]:
    footer = answer.rsplit(_FOOTER_RULE, 1)[1]
    assert footer.endswith("*"), answer[-400:]
    return footer[:-1].split(" · ")


def _english_llm(cls: type[FakeLLM] = FakeLLM, **kwargs) -> FakeLLM:
    return cls(classify_language="en", classify_search_language="en", **kwargs)


@pytest.mark.parametrize(
    ("make_llm", "make_settings", "failed_steps"),
    [
        (lambda: FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings()), _settings, 0),
        (
            lambda: _UnreadablePlanLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_bindings=_clean_answer_bindings()),
            _settings,
            1,
        ),
        (lambda: _blocked_report_scenario()[0], lambda: _blocked_report_scenario()[1], 1),
    ],
    ids=["clean", "failed_step", "blocked"],
)
def test_a_german_report_carries_no_internal_code(make_llm, make_settings, failed_steps) -> None:
    """A German report, the blocked one included, names no internal code:
    its footer is reader phrases only, a failed step is counted in words,
    and the confidence reads 'Konfidenz'."""
    raw, _events = _run_scenario(make_llm(), FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)), make_settings())
    answer = raw["answer"]
    state = raw["result_state"]

    assert len(state.get("algorithm_failures") or []) == failed_steps, state.get("algorithm_failures")
    assert not [code for code in _INTERNAL_REPORT_CODES if code in answer], answer[-2500:]
    pieces = _footer_pieces(answer)
    assert [piece for piece in pieces if not _FOOTER_PIECE["de"].fullmatch(piece)] == [], pieces
    assert any(piece.startswith("Konfidenz ") for piece in pieces), pieces
    if failed_steps:
        assert "1 fehlgeschlagener Schritt" in pieces, pieces


def test_the_blocked_german_report_keeps_its_diagnosis_for_operators() -> None:
    """The blocked report says in German what happened, keeps the run id,
    and lists the technical failure verbatim under a section for operators,
    where the settings to look at stand as code."""
    llm, settings = _blocked_report_scenario()
    raw, _events = _run_scenario(llm, FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)), settings)
    answer = raw["answer"]
    state = raw["result_state"]

    reader_part, operator_part = answer.split("## Für den Betrieb: blockierende Fehler", 1)
    assert reader_part.startswith("## Kein Bericht erstellt\n\nEin Schritt, den der Bericht braucht, ist fehlgeschlagen."), answer[:600]
    assert f"- Laufkennung: `{state['_run_id']}`" in reader_part, reader_part
    assert "`OBSERVABILITY_PROFILE=forensic`" not in reader_part, reader_part
    failure = state["algorithm_failures"][0]
    assert f"\n  {failure['message']}\n" in operator_part, operator_part
    assert "### Nächster Schritt" in operator_part, operator_part
    assert "`OBSERVABILITY_PROFILE=forensic`" in operator_part, operator_part


@pytest.mark.parametrize(
    ("make_llm", "make_settings"),
    [
        (
            lambda: _english_llm(
                claim_payload=_CLEAN_CLAIM_PAYLOAD,
                answer_override="A complete sentence stands here.\n\nThe section breaks off",
            ),
            _settings,
        ),
        (
            lambda: _english_llm(_AnswerProviderDownLLM, claim_payload=_CLEAN_CLAIM_PAYLOAD),
            lambda: _blocked_report_scenario()[1],
        ),
    ],
    ids=["cut_off", "blocked"],
)
def test_an_english_report_carries_no_fixed_german_text(make_llm, make_settings) -> None:
    """Every text the server adds to an English report reads in English:
    the completeness note and its reasons, the evidence notice, the source
    labels, the blocked report and the footer. The model's text here is
    English too, so the whole report is checked."""
    raw, _events = _run_scenario(make_llm(), FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)), make_settings())
    answer = raw["answer"]

    assert raw["result_state"]["language"] == "en"
    assert _GERMAN_MARK.findall(answer) == [], answer[-2500:]
    assert not [code for code in _INTERNAL_REPORT_CODES if code in answer], answer[-2500:]
    pieces = _footer_pieces(answer)
    assert [piece for piece in pieces if not _FOOTER_PIECE["en"].fullmatch(piece)] == [], pieces


def test_an_unlabelled_source_is_numbered_in_the_language_of_the_report() -> None:
    """A source the evidence overview gave no label is listed under the
    source label of the run's language, in the text and in the record; a
    record handed over without a label gets the same word."""
    from inqtrix.nodes import _build_answer_appendix_sections, _format_report_reference_records

    strategies = create_default_strategies(_settings())
    for language, word in (("en", "Source"), ("de", "Quelle")):
        appendix = _build_answer_appendix_sections(
            "",
            allowed_citations=["https://example.com/report/a"],
            label_urls={},
            strategies=strategies,
            incomplete_reasons=[],
            finish_reason="stop",
            language=language,
        )
        assert f"- [{word} 1](https://example.com/report/a)" in "\n".join(appendix.sections), language
        assert [record["label"] for record in appendix.references] == [f"{word} 1"], language
        records = _format_report_reference_records(
            [("", "https://example.com/report/b")], strategies, language=language
        )
        assert [record["label"] for record in records] == [word], language


@pytest.mark.parametrize(
    ("language", "step"),
    [
        ("de", "Warnung: Abschnitt 'Kernaussagen' wirkt abgeschnitten: endet mit einem kurzen Bruchstück ohne Satzende"),
        ("en", "Warning: section 'Key points' looks cut off: ends in a short fragment without a sentence ending"),
    ],
)
def test_a_cut_off_section_names_its_signal_in_the_language_of_the_run(language, step) -> None:
    """The step of a section that looks cut off names why in the run's
    language; the log keeps the English words."""
    llm = FakeLLM(
        classify_language=language,
        classify_search_language=language,
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_override=_CUT_OFF_SECTION,
    )
    raw, events = _run_scenario(llm, FakeSearch(load_search_result_fixture(_FIXTURE_SLUG)))

    assert step in _progress_messages(events), _progress_messages(events)
    section_logs = raw["result_state"]["iteration_logs"][-1]["section_logs"]
    assert section_logs[0]["incomplete_reasons"] == [
        "ends in a short fragment without a sentence ending"
    ]


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


def test_a_research_reference_carries_its_own_cell_of_a_cited_table_row() -> None:
    """Two sources in one row of the provider's table, each annotated at
    its link: the references of the result say what the row says for their
    own source, behind the label of the row."""
    vdi = "https://www.vdi.example/faktencheck"
    energy = "https://www.energiewechsel.example/waermepumpe"
    answer = (
        "| Kriterium | Luft-Wasser | Sole-Wasser |\n"
        "|---|---|---|\n"
        f"| **Effizienz** | Schwankt mit der Außentemperatur. ([vdi.example]({vdi})) "
        f"| Meist effizienter, weil das Erdreich konstant bleibt. ([energiewechsel.example]({energy})) |"
    )

    def span(url: str) -> CitationSpan:
        start = answer.index("([", answer.index(url) - 40)
        return CitationSpan(start, answer.index("))", start) + 2)

    search = FakeSearch(GroundedSearchResult(
        answer=answer,
        sources=[
            GroundedSource(url=url, rank=rank, origin="url_citation", spans=(span(url),))
            for rank, url in enumerate((vdi, energy), start=1)
        ],
    ))
    raw, _events = _run_scenario(FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD), search)
    result = ResearchResult.from_raw(raw)

    supports = {reference.url: reference.grounded_support for reference in result.references}
    assert supports == {
        vdi: "**Effizienz** | Schwankt mit der Außentemperatur.",
        energy: "**Effizienz** | Meist effizienter, weil das Erdreich konstant bleibt.",
    }


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
    # The status is its own reason; no cause of needs_review is read into it.
    assert result.metrics.evidence_contract_causes == []
    assert result.metrics.confidence_cap == 4
    assert result.metrics.confidence_cap_reasons == ["source_context_only"]
    # The reader gets the source-context notice under the warning heading;
    # the basis line names no numbers while no statement is supported.
    answer = raw["answer"]
    assert _NOTICE_HEADING in answer, answer[-1500:]
    # The notice says what the status counts: no cited sentence restates a
    # recorded statement, whatever that statement's status.
    assert (
        "Kein Satz der Antwort, der eine Quelle zitiert, gibt eine zu dieser "
        "Quelle erfasste Aussage wieder. Ob ein Satz von seiner Quelle gestützt "
        "wird, ist daher nicht festgestellt."
    ) in answer, answer[-1500:]
    assert "verifizierte oder strittige" not in answer, answer[-1500:]
    assert f"{_PROVIDER_BASIS_LINE}{_FOOTER_RULE}" in answer, answer[-1500:]

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
    # Nothing is cited, so the run names no basis and carries no ceiling,
    # and the report carries no basis line.
    assert result.metrics.evidence_basis == "none"
    assert result.metrics.cited_reference_count == 0
    assert result.metrics.confidence_cap is None
    assert "Grundlage:" not in raw["answer"], raw["answer"][-800:]

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
    # The system-generated appendix, basis line and footer follow the answer
    # language too.
    assert "## References" in raw["answer"] and "Referenzen" not in raw["answer"]
    assert " round" in raw["answer"] and "Runde" not in raw["answer"]
    assert (
        "Basis: what the search provider returned for the cited sources; the "
        "pages themselves were not read. Supported statements of the research "
        "with more than one source: 1 of 1."
    ) in raw["answer"], raw["answer"][-1200:]
    assert "Grundlage:" not in raw["answer"]


# --------------------------------------------------------------------------- #
# Scenario: a cut-off report names what it lacks, in its own language
# --------------------------------------------------------------------------- #

_CUT_OFF_SECTION = "A complete sentence stands here.\n\nThe section breaks off"


class _EmptyClosingSectionLLM(FakeLLM):
    """The closing section of a German compact report comes back empty, every
    other section ends in a fragment."""

    def complete_with_metadata(self, prompt, **kwargs):
        response = super().complete_with_metadata(prompt, **kwargs)
        if "Einordnung / Ausblick" in prompt:
            return dataclasses.replace(response, content="")
        return response


def test_a_cut_off_english_report_misses_no_closing_section_it_carries() -> None:
    """An English report checks its closing sections under its own
    headings: every section of the plan is there, so the cut-off report
    names none of them missing, and its completeness note reads in
    English."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = FakeLLM(
        classify_language="en",
        classify_search_language="en",
        claim_payload=_CLEAN_CLAIM_PAYLOAD,
        answer_override=_CUT_OFF_SECTION,
    )
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    answer = raw["answer"]

    assert "## Context and outlook" in answer, answer[:1500]
    assert state["answer_incomplete"] is True
    assert state["answer_incomplete_reasons"] == [
        "ends in a short fragment without a sentence ending"
    ]
    note = answer[answer.index("## Note on completeness"):]
    assert "- Diagnosis: ends in a short fragment without a sentence ending\n" in note, note
    assert "closing section" not in note and "Abschluss" not in note, note


def test_a_cut_off_quick_report_is_not_held_to_closing_sections_it_never_plans() -> None:
    """The quick profile plans a summary and key points only, so a cut-off
    quick report names no missing closing section."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    settings = AgentSettings(
        testing_mode=True,
        first_round_queries=2,
        max_rounds=1,
        min_rounds=1,
        report_profile=ReportProfile.SCHNELL,
    )
    llm = FakeLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_override=_CUT_OFF_SECTION)
    raw, _events = _run_scenario(llm, FakeSearch(fixture), settings)
    state = raw["result_state"]

    assert state["answer_incomplete"] is True
    assert state["answer_incomplete_reasons"] == [
        "ends in a short fragment without a sentence ending"
    ]
    note = raw["answer"][raw["answer"].index("## Hinweis zur Vollständigkeit"):]
    assert "- Diagnose: endet mit einem kurzen Bruchstück ohne Satzende\n" in note, note
    assert "Abschluss" not in note, note


def test_a_cut_off_report_names_the_closing_section_it_lacks() -> None:
    """The positive control: a compact report whose closing section came
    back empty names that section, under its heading in the run's
    language."""
    fixture = load_search_result_fixture(_FIXTURE_SLUG)
    llm = _EmptyClosingSectionLLM(claim_payload=_CLEAN_CLAIM_PAYLOAD, answer_override=_CUT_OFF_SECTION)
    raw, _events = _run_scenario(llm, FakeSearch(fixture))
    state = raw["result_state"]
    answer = raw["answer"]

    body = answer[: answer.index("## Hinweis zur Vollständigkeit")]
    assert "## Einordnung / Ausblick" not in body and "## Detailanalyse" in body, body
    assert state["answer_incomplete_reasons"] == [
        "ends in a short fragment without a sentence ending",
        "missing closing sections: Einordnung / Ausblick",
    ]
    note = answer[answer.index("## Hinweis zur Vollständigkeit"):]
    assert (
        "- Diagnose: endet mit einem kurzen Bruchstück ohne Satzende; "
        "fehlende Abschlussabschnitte: Einordnung / Ausblick\n"
    ) in note, note
