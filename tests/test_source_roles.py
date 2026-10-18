"""Pins: a source's role is judged once by the extraction call and pinned in
the run's source registry (structural floor > first model judgement >
unjudged), the claim ledger carries the pinned tiers, the source quality
is computed over the registry, and the public result uses the configured
strategy for citations the registry does not cover."""

from __future__ import annotations

import logging
from queue import Queue
from types import SimpleNamespace

from inqtrix.evidence import derive_claim_ledger_from_evidence
from inqtrix.nodes import search
from inqtrix.providers.base import ProviderContext
from inqtrix.result import ResearchResult
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state
from inqtrix.strategies import StrategyContext, create_default_strategies

KFW = "https://www.kfw.de/inlandsfoerderung/waermepumpe"
GVAT = "https://foerderung.example.gv.at/waermepumpe"
PRESS = "https://www.example-zeitung.de/wirtschaft/waermepumpe"
FORUM = "https://forum.example.net/thread/waermepumpe"


class _LLM:
    def __init__(self) -> None:
        self.models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_evaluate_model="evaluate-model",
            effective_claim_extract_model="claim-extract-model",
        )

    def complete(self, prompt, *a, **kw):
        return ""

    def consume_nonfatal_notice(self):
        return None

    def is_available(self):
        return True


class _Search:
    """Two queries; the second sees the KfW page and the newspaper again."""

    def __init__(self) -> None:
        self.calls = 0

    def search(self, *a, **kw):
        self.calls += 1
        if self.calls == 1:
            sources = [KFW, GVAT, PRESS]
        else:
            sources = [KFW, FORUM, PRESS]
        return GroundedSearchResult(
            answer="Die KfW foerdert Waermepumpen. " + " ".join(f"Quelle {u}." for u in sources),
            sources=[
                GroundedSource(url=u, title=f"Seite {i}", snippet="s", rank=i + 1)
                for i, u in enumerate(sources)
            ],
        )

    def is_available(self):
        return True


class _RolesExtraction:
    """Judges the sources of each call. Call 1 leaves the KfW page
    UNJUDGED (the case the counters exist for) and judges the rest; call 2
    judges the KfW page official_primary and contradicts the newspaper
    (a conflict), the forum stays unjudged."""

    def __init__(self) -> None:
        self.calls = 0
        self.fixed_roles_seen: list[dict[str, str]] = []
        self._roles: list[dict[str, str]] = []
        self._metadata: dict[str, int] = {}

    def extract(self, text, citations, question, *, fixed_roles=None, **kw):
        self.calls += 1
        self.fixed_roles_seen.append(dict(fixed_roles or {}))
        if self.calls == 1:
            self._roles = [
                {"url": GVAT, "role": "reporting", "reason": "sieht aus wie Presse"},
                {"url": PRESS, "role": "reporting", "reason": "Redaktion einer Tageszeitung"},
            ]
            self._metadata = {"source_role_count": 2, "source_role_missing_count": 1, "source_role_invalid_count": 0}
            claims = [
                {
                    "claim_text": "Die Foerderung betraegt 30 Prozent der Kosten.",
                    "claim_type": "fact",
                    "polarity": "affirmed",
                    "needs_primary": True,
                    "source_urls": [KFW],
                    "provider_refs": ["1"],
                    "binding_status": "bound",
                    "evidence_snippet": "30 Prozent der Kosten.",
                }
            ]
        else:
            self._roles = [
                {"url": KFW, "role": "official_primary", "reason": "Foerderbank des Bundes"},
                {"url": PRESS, "role": "commentary_stakeholder", "reason": "zweite Meinung"},
            ]
            self._metadata = {"source_role_count": 2, "source_role_missing_count": 1, "source_role_invalid_count": 0}
            claims = []
        return claims, 0, 0

    def consume_source_roles(self):
        roles, self._roles = self._roles, []
        return roles

    def consume_extraction_metadata(self):
        metadata, self._metadata = self._metadata, {}
        return metadata

    def consume_nonfatal_notice(self):
        return None


def _strategies(settings: AgentSettings, extraction, tiering=None) -> StrategyContext:
    defaults = create_default_strategies(settings)
    return StrategyContext(
        source_tiering=tiering or defaults.source_tiering,
        claim_extraction=extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )


def _run_search(caplog, *, tiering=None, settings: AgentSettings | None = None) -> tuple[dict, list[str], _RolesExtraction]:
    settings = settings or AgentSettings(first_round_queries=2, max_rounds=4)
    queue: Queue = Queue()
    state = initial_state("Wie hoch ist die Waermepumpen-Foerderung 2026?", progress_queue=queue, max_total_seconds=30)
    state["queries"] = ["q1", "q2"]
    state["required_aspects"] = ["Foerderhoehe"]
    state["uncovered_aspects"] = ["Foerderhoehe"]
    extraction = _RolesExtraction()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        search(
            state,
            providers=ProviderContext(llm=_LLM(), search=_Search()),
            strategies=_strategies(settings, extraction, tiering),
            settings=settings,
        )
    messages: list[str] = []
    while not queue.empty():
        messages.append(queue.get()[1])
    return state, messages, extraction


def _by_domain(state: dict) -> dict[str, dict]:
    return {record["domain"]: record for record in state["source_records"].values()}


def test_roles_are_pinned_once_floor_over_model_over_unjudged(caplog):
    state, _, _ = _run_search(caplog)
    records = _by_domain(state)
    kfw = records["kfw.de"]
    assert (kfw["tier"], kfw["tier_reason"], kfw["role_reason"]) == ("primary", "model:official_primary", "Foerderbank des Bundes")
    gvat = records["foerderung.example.gv.at"]
    assert (gvat["tier"], gvat["tier_reason"]) == ("primary", "structural:official_primary"), "the floor wins over 'reporting'"
    assert records["example-zeitung.de"]["tier"] == "mainstream", "the first judgement wins over 'commentary_stakeholder'"
    assert records["example-zeitung.de"]["tier_reason"] == "model:reporting"
    assert records["forum.example.net"]["tier"] == "unknown" and records["forum.example.net"]["tier_reason"] == "unjudged"
    assert state["source_role_counts"] == {"strategy": 1, "model": 2, "unjudged": 1, "conflicts": 1}
    assert "Conflicting source role" in caplog.text, "the second judgement of the newspaper is a visible conflict"


def test_a_source_judged_in_a_later_query_updates_the_earlier_record(caplog):
    """The KfW page reached the ledger through q1 unjudged and was judged
    by q2's call: the registry, q1's evidence record, the derived ledger,
    the claim status and the report reference must all say primary — a
    reference list showing 'unknown' for a page the top sources call
    'primary' was the visible symptom."""
    state, messages, _ = _run_search(caplog)
    kfw_records = [r for r in state["evidence_ledger"] if r["canonical_url"] == KFW]
    assert kfw_records and all(r["tier"] == "primary" and r["tier_reason"] == "model:official_primary" for r in kfw_records)
    assert all(r["role_reason"] == "Foerderbank des Bundes" for r in kfw_records)
    ledger = derive_claim_ledger_from_evidence(state["evidence_ledger"])
    entry = next(e for e in ledger if KFW in e.get("source_urls", []))
    assert entry["source_tiers"] == {KFW: "primary"}
    claim = next(c for c in state["consolidated_claims"] if "30 Prozent" in c["claim_text"])
    # The role makes the structure primary; the provider returned no page
    # text, so the claim is the provider's attribution, not verified.
    assert claim["status"] == "attributed" and claim["verification_basis"] == "primary_source"
    assert claim["primary_basis"] == "role"
    assert claim["status_reason"] == "primary evidence present; attributed by the search provider"
    # Source quality over the registry: KfW + gv.at primary, the newspaper mainstream, the forum unknown.
    assert state["source_tier_counts"] == {"primary": 2, "mainstream": 1, "stakeholder": 0, "unknown": 1, "low": 0}
    # What stayed unjudged after the whole round is said once, from the registry (not per-call remainders).
    role_lines = [m for m in messages if "Quellen-Rollen" in m or "Source roles" in m]
    assert len(role_lines) == 1 and ("1 nicht beurteilt" in role_lines[0] or "1 not judged" in role_lines[0]), role_lines


def test_a_configured_strategys_placement_wins_over_the_model(caplog):
    """A custom tiering strategy (an organisation's own list) places the
    newspaper low with its own reason; the extractor's 'reporting' must
    not overwrite it, and the placement counts as the strategy's."""
    from inqtrix.strategies import DefaultSourceTiering

    class _OrgList(DefaultSourceTiering):
        def tier_for_url(self, url: str) -> str:
            return "low" if "example-zeitung" in url else super().tier_for_url(url)

        def explain_url(self, url: str) -> dict[str, str]:
            if "example-zeitung" in url:
                return {"domain": "example-zeitung.de", "tier": "low", "tier_reason": "org_blocklist"}
            return super().explain_url(url)

    state, _, extraction = _run_search(caplog, tiering=_OrgList())
    press = _by_domain(state)["example-zeitung.de"]
    assert (press["tier"], press["tier_reason"]) == ("low", "org_blocklist")
    assert state["source_role_counts"]["strategy"] == 2
    assert all(card[PRESS] == "strategy:low" for card in extraction.fixed_roles_seen)


def test_a_source_judged_in_a_later_round_updates_retained_claims(caplog):
    """A later role reaches retained claims before they are consolidated."""
    settings = AgentSettings(first_round_queries=1, max_rounds=4)
    state = initial_state("Wie hoch ist die Foerderung?", max_total_seconds=30)
    extraction = _RolesExtraction()
    providers = ProviderContext(llm=_LLM(), search=_Search())
    strategies = _strategies(settings, extraction)
    state["queries"] = ["Foerderhoehe"]
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        search(state, providers=providers, strategies=strategies, settings=settings)
    original = next(c for c in state["consolidated_claims"] if "30 Prozent" in c["claim_text"])
    assert original["status"] == "unverified"
    original_members = original["member_claim_ids"]
    assert _by_domain(state)["kfw.de"]["tier_reason"] == "unjudged"

    assert state["round"] == 1
    state["queries"].append("Herausgeber der Foerderung")
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        search(state, providers=providers, strategies=strategies, settings=settings)

    assert extraction.calls == 2
    retained = next(c for c in state["consolidated_claims"] if "30 Prozent" in c["claim_text"])
    assert retained["member_claim_ids"] == original_members, "the later call adds no replacement claim"
    assert _by_domain(state)["kfw.de"]["tier_reason"] == "model:official_primary"
    records = [r for r in state["evidence_ledger"] if r["canonical_url"] == KFW]
    assert {q["round"] for q in state["query_records"]} == {0, 1}
    assert {r["query_id"] for r in records} == {q["query_id"] for q in state["query_records"]}
    assert all(r["tier"] == "primary" for r in records)
    assert retained["status"] == "attributed"
    assert retained["verification_basis"] == "primary_source"
    assert retained["primary_basis"] == "role"


def test_already_placed_sources_are_fixed_on_the_card(caplog):
    """The extractor is not asked to judge a source the run already placed:
    the gv.at page (structural floor) is fixed on the first card; the KfW
    page is still unjudged when the second card is built."""
    _, _, extraction = _run_search(caplog)
    assert extraction.fixed_roles_seen[0] == {GVAT: "official_primary"}
    assert extraction.fixed_roles_seen[1] == {}


def test_the_forensic_source_record_carries_the_pinned_role(caplog):
    settings = AgentSettings(first_round_queries=2, max_rounds=4, observability_profile="forensic", testing_mode=True)
    state, _, _ = _run_search(caplog, settings=settings)
    events = [e for e in state["iteration_logs"] if e.get("event") == "source_record" and e.get("canonical_url") == KFW]
    assert events and events[0]["tier"] == "primary" and events[0]["role_reason"] == "Foerderbank des Bundes"


class _AllPrimary:
    def tier_for_url(self, url: str) -> str:
        return "primary"

    def quality_from_urls(self, urls):
        return {"primary": len(urls), "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0}, 1.0


def test_the_public_result_uses_the_configured_strategy_for_uncovered_citations():
    """A legacy state without source records: the configured strategy
    classifies, not a freshly built default (which knows no sites)."""
    raw = {"answer": "Antwort", "usage": {}, "result_state": {"all_citations": ["https://www.kfw.de/x"]}}
    configured = ResearchResult.from_raw(raw, source_tiering=_AllPrimary())
    assert configured.top_sources[0].tier == "primary"
    default = ResearchResult.from_raw(raw)
    assert default.top_sources[0].tier == "unknown"


def _evaluate_with_claim_signal(*, needs_primary_total: int, needs_primary_supported: int) -> int:
    from inqtrix.nodes import evaluate

    class _Eval(_LLM):
        def complete(self, prompt, *a, **kw):
            return (
                "STATUS: SUFFICIENT\nCONFIDENCE: 9\nGAPS: Keine\nCOVERED_ASPECTS: [1]\n"
                "CONTRADICTIONS: Nein\nEVIDENCE_CONSISTENCY: 9\nEVIDENCE_SUFFICIENCY: 9"
            )

    settings = AgentSettings(confidence_stop=7, max_rounds=3)
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    # A cost question: the retired word list would have fired on "Kosten" alone.
    state = initial_state("Was kostet eine Waermepumpe 2026 in Euro?", max_total_seconds=30)
    state["round"] = 1
    state["context"] = ["Kontext"]
    state["all_citations"] = ["https://www.example-zeitung.de/a"]
    state["required_aspects"] = ["Kosten"]
    state["uncovered_aspects"] = ["Kosten"]
    state["source_tier_counts"] = {"primary": 0, "mainstream": 1, "stakeholder": 0, "unknown": 0, "low": 0}
    state["claim_needs_primary_total"] = needs_primary_total
    state["claim_needs_primary_supported"] = needs_primary_supported
    evaluate(state, providers=ProviderContext(llm=_Eval(), search=_Search()), strategies=strategies, settings=settings)
    return int(state["final_confidence"])


def test_the_needs_primary_guardrail_follows_the_claims_not_the_question_words():
    """Only claims the extractor flagged as primary-needing with none
    supported (verified or attributed) cap the confidence; a cost question
    with no such claim is not capped."""
    assert _evaluate_with_claim_signal(needs_primary_total=2, needs_primary_supported=0) == 8
    assert _evaluate_with_claim_signal(needs_primary_total=2, needs_primary_supported=1) == 9
    assert _evaluate_with_claim_signal(needs_primary_total=0, needs_primary_supported=0) == 9
