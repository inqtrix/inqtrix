"""What a claim's status may say: a source's own text checked (``verified``)
or only the search provider's attribution (``attributed``).

A tier says who speaks, never what a page says; provider prose is
attribution, not verification. The research loop reads support (verified
and attributed alike), so a provider that returns no source text runs the
same loop as one that does.
"""

import copy

import pytest

from inqtrix.evidence import (
    STANDING_TEXT,
    _render_source_record_block,
    derive_claim_ledger_from_evidence,
    project_claim_verification_to_evidence,
)
from inqtrix.nodes import (
    _evidence_depth_gap,
    _select_crosscheck_targets,
    has_report_bundles,
)
from inqtrix.strategies._claim_values import page_states_claim

CLAIM = "Die Frist für Artikel 50 endet am 2. August 2026."
PAGE = "Nach der Verordnung endet die Frist für Artikel 50 am 2. August 2026 für alle Anbieter."


def _entry(
    url: str,
    *,
    tier: str,
    query: str,
    page: str = "",
    text: str = CLAIM,
    snippet: str = "",
    raw_id: str = "",
) -> dict:
    # A short, distinct excerpt per entry: two provider answers never
    # quote the same words, and an identical excerpt would make the second
    # entry a reprint of the first.
    entry = {
        "raw_claim_id": raw_id or f"raw_{query}_{url}",
        "claim_text": text,
        "evidence_snippet": snippet or f"Auszug {raw_id or query}",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": True,
        "source_urls": [url],
        "source_tiers": {url: tier},
        "query_id": query,
    }
    if page:
        entry["source_texts"] = {url: page}
    return entry


class TestPageText:
    """``page_states_claim``: key values exact, half the content words."""

    def test_a_page_with_the_values_and_the_words_states_the_claim(self):
        assert page_states_claim(CLAIM, PAGE)

    def test_a_page_without_the_date_does_not(self):
        assert not page_states_claim(CLAIM, "Die Frist für Artikel 50 endet später.")

    def test_a_year_does_not_stand_for_a_day_on_the_page(self):
        assert not page_states_claim("Die Regeln gelten ab 2026.", "Stand: 12. März 2026. Die Regeln gelten.")
        assert page_states_claim("Die Regeln gelten ab 2026.", "Die Regeln gelten ab 2026 für alle.")

    def test_a_rounded_figure_needs_its_own_number(self):
        assert not page_states_claim(
            "Rund 40 % der Unternehmen nutzen KI.", "Der Umsatz der Unternehmen stieg um 3 %.",
        )

    def test_the_values_alone_are_not_enough(self):
        assert not page_states_claim(CLAIM, "Termine: 2. August 2026, Artikel 50, Sitzung.")

    def test_a_claim_without_a_key_value_is_never_checked_by_words(self):
        text = "Die Kommission hat Leitlinien veröffentlicht."
        assert not page_states_claim(text, text)

    def test_no_page_text_states_nothing(self):
        assert not page_states_claim(CLAIM, "")


class TestStatus:
    def test_a_primary_source_without_its_text_is_attributed(self, consolidator):
        claim = consolidator.consolidate([
            _entry("https://eur-lex.europa.eu/a", tier="primary", query="q1"),
        ])[0]

        assert claim["status"] == "attributed"
        assert claim["verification_basis"] == "primary_source"
        assert claim["checked_urls"] == []
        assert claim["status_reason"] == "primary evidence present; attributed by the search provider"

    def test_the_page_text_of_the_source_verifies(self, consolidator):
        claim = consolidator.consolidate([
            _entry("https://eur-lex.europa.eu/a", tier="primary", query="q1", page=PAGE),
        ])[0]

        assert claim["status"] == "verified"
        assert claim["verification_basis"] == "primary_source"
        assert claim["checked_urls"] == ["https://eur-lex.europa.eu/a"]
        assert claim["status_reason"] == "primary evidence present; checked against source text"

    def test_a_page_text_without_the_value_leaves_it_attributed(self, consolidator):
        claim = consolidator.consolidate([
            _entry(
                "https://eur-lex.europa.eu/a", tier="primary", query="q1",
                page="Die Frist für Artikel 50 endet am 2. August 2027.",
            ),
        ])[0]

        assert claim["status"] == "attributed"

    def test_one_answer_citing_two_pages_is_one_witness(self, consolidator):
        """One provider sentence bound to two mainstream sites corroborates
        nothing; two answers on two sites do."""
        one_answer = consolidator.consolidate([
            _entry("https://www.a-news.de/x", tier="mainstream", query="q1"),
            _entry("https://www.b-news.de/y", tier="mainstream", query="q1"),
        ])[0]
        two_answers = consolidator.consolidate([
            _entry("https://www.a-news.de/x", tier="mainstream", query="q1"),
            _entry("https://www.b-news.de/y", tier="mainstream", query="q2"),
        ])[0]

        assert one_answer["verification_basis"] == "quality_source"
        assert one_answer["independent_support_count"] == 1
        assert two_answers["verification_basis"] == "cross_checked"
        assert two_answers["independent_support_count"] == 2

    def test_a_second_answer_on_a_low_source_corroborates_nothing(self, consolidator):
        claim = consolidator.consolidate([
            _entry("https://www.a-news.de/x", tier="unknown", query="q1"),
            _entry("https://www.b-news.de/y", tier="unknown", query="q1"),
            _entry("https://www.reddit.com/r/x", tier="low", query="q2"),
        ])[0]

        assert claim["verification_basis"] != "cross_checked"

    def test_a_wire_text_on_two_sites_checks_once(self, consolidator):
        """Two checked pages with the same text are one page: alone they do
        not lift the checked subset to a cross-check, and two unknown-tier
        pages give no other structure."""
        wire = PAGE + " Die Agentur meldet weitere Einzelheiten zur Umsetzung."
        claim = consolidator.consolidate([
            _entry("https://www.a-news.de/x", tier="unknown", query="q1", page=wire),
            _entry("https://www.b-news.de/y", tier="unknown", query="q2", page=wire + " Mehr dazu."),
        ])[0]

        assert claim["verification_basis"] == "cross_checked"
        assert claim["checked_urls"] == ["https://www.a-news.de/x"]
        assert claim["status"] == "attributed"


def _mixed_ledger(*, with_pages: bool) -> list[dict]:
    """G4: a primary source with no text, a mainstream and an unknown
    source whose pages state the claim; plus a single-source quality claim,
    a contested pair and enough claims to exceed a small selection."""
    page = PAGE if with_pages else ""
    ledger = [
        _entry("https://eur-lex.europa.eu/a", tier="primary", query="q1", raw_id="r1"),
        _entry("https://www.a-news.de/x", tier="mainstream", query="q1", page=page, raw_id="r2"),
        _entry("https://www.b-blog.de/y", tier="unknown", query="q2", page=page, raw_id="r3"),
        _entry(
            "https://www.c-news.de/z", tier="mainstream", query="q3", raw_id="r4",
            text="Die Behörde prüft 120 Anträge pro Woche.",
            page="Die Behörde prüft 120 Anträge pro Woche." if with_pages else "",
        ),
        _entry(
            "https://www.d-news.de/z", tier="mainstream", query="q4", raw_id="r5",
            text="Die Strafe beträgt 35 Mio. Euro.",
        ),
        _entry(
            "https://www.e-news.de/z", tier="mainstream", query="q5", raw_id="r6",
            text="Die Strafe beträgt 15 Mio. Euro.",
        ),
    ]
    for index in range(4):
        ledger.append(_entry(
            f"https://www.f{index}-news.de/z", tier="mainstream", query=f"q{6 + index}",
            raw_id=f"r{7 + index}", text=f"Studie {index} belegt einen Nutzen von KI.",
            page=f"Studie {index} belegt einen Nutzen von KI in Firmen." if with_pages else "",
        ))
    return ledger


class TestLoopReadsSupportNotChecking:
    """I5: the same witnesses with or without page text give the research
    loop the same structure, order, selection, counts and guardrails; only
    the status says what was checked."""

    def _loop_view(self, consolidator, ledger):
        consolidated = consolidator.consolidate(copy.deepcopy(ledger))
        state = {"consolidated_claims_full": consolidated, "consolidated_claims": consolidated}
        _, score, np_total, np_supported = consolidator.quality_metrics(consolidated)
        return {
            "order": [claim["claim_id"] for claim in consolidated],
            "basis": {claim["claim_id"]: claim["verification_basis"] for claim in consolidated},
            "selected": [
                claim["claim_id"]
                for claim in consolidator.materialize(consolidated, max_total=4, max_unverified=1)
            ],
            "score": score,
            "needs_primary": (np_total, np_supported),
            "depth": _evidence_depth_gap(state),
            "targets": [target["claim_id"] for target in _select_crosscheck_targets(consolidated)],
            "report_bundles": has_report_bundles(consolidated),
            "supported": sum(
                1 for claim in consolidated if claim["status"] in {"verified", "attributed"}
            ),
        }, {claim["claim_id"]: claim["status"] for claim in consolidated}

    def test_page_text_changes_the_status_and_nothing_the_loop_reads(self, consolidator):
        with_pages, statuses_with = self._loop_view(consolidator, _mixed_ledger(with_pages=True))
        without, statuses_without = self._loop_view(consolidator, _mixed_ledger(with_pages=False))

        assert with_pages == without
        changed = {
            claim_id for claim_id, status in statuses_with.items()
            if status != statuses_without[claim_id]
        }
        assert changed  # the pages verified something...
        assert {statuses_with[claim_id] for claim_id in changed} == {"verified"}
        assert {statuses_without[claim_id] for claim_id in changed} == {"attributed"}
        # ...and the mixed claim keeps its primary structure either way (G4).
        mixed = next(
            claim_id for claim_id, basis in without["basis"].items()
            if basis == "primary_source"
        )
        assert statuses_with[mixed] == "verified"
        assert statuses_without[mixed] == "attributed"

    def test_an_attributed_ledger_is_a_report_basis(self, consolidator):
        """G1: a provider without source text attributes every claim; the
        confidence guardrail must not read that as "no report evidence"."""
        consolidated = consolidator.consolidate([
            entry for entry in _mixed_ledger(with_pages=False)
            if "Strafe" not in entry["claim_text"]
        ])

        assert {claim["status"] for claim in consolidated} == {"attributed"}
        assert has_report_bundles(consolidated)
        assert has_report_bundles([{"status": "contested"}])
        assert not has_report_bundles([{"status": "unverified"}])


def _record(claims: list[dict], *, url: str = "https://www.a-news.de/x", snippet: str = "") -> dict:
    return {
        "evidence_id": "ev_1",
        "query_id": "q1",
        "canonical_url": url,
        "tier": "mainstream",
        "tier_reason": "",
        "source_title": "A News",
        "source_snippet": snippet,
        "source_passages": [],
        "claims": claims,
        "report_eligible": True,
    }


class TestEvidenceBlock:
    """G8/RC5: the writer sees per statement whether this source's text was
    checked, and provider text is never labelled as evidence."""

    def _block(self, record: dict) -> str:
        return _render_source_record_block([record], "E1", max_chars=4000, seen_texts=set())

    def test_statements_are_grouped_by_what_this_source_checked(self, consolidator):
        checked = _entry("https://www.a-news.de/x", tier="mainstream", query="q1", page=PAGE, raw_id="c1")
        attributed = _entry(
            "https://www.a-news.de/x", tier="mainstream", query="q1", raw_id="c2",
            text="Die Behörde prüft 120 Anträge pro Woche.",
        )
        record = _record(
            [
                {"raw_claim_id": "c1", "claim_text": checked["claim_text"], "evidence_snippet": "Auszug eins"},
                {"raw_claim_id": "c2", "claim_text": attributed["claim_text"], "evidence_snippet": "Auszug zwei"},
            ],
            snippet=PAGE,
        )
        project_claim_verification_to_evidence(
            [record], consolidator.consolidate([checked, attributed]),
        )

        block = self._block(record)

        assert f"Standing: {STANDING_TEXT['single-source verified']}" in block
        assert f"Statements checked against this source's text:\n  - {CLAIM}" in block
        assert (
            "Statements the search provider attributes to this source:\n"
            "  - Die Behörde prüft 120 Anträge pro Woche."
        ) in block
        assert f"Text the search provider returned from this source:\n  - {PAGE}" in block
        assert "Search-provider text these statements were taken from:" in block
        assert "Evidence excerpts" not in block
        assert "Statements from this source" not in block

    def test_a_source_whose_text_was_not_checked_is_provider_attributed(self, consolidator):
        entry = _entry("https://www.a-news.de/x", tier="mainstream", query="q1", raw_id="c1")
        record = _record([{"raw_claim_id": "c1", "claim_text": CLAIM, "evidence_snippet": CLAIM}])
        project_claim_verification_to_evidence([record], consolidator.consolidate([entry]))

        block = self._block(record)

        assert f"Standing: {STANDING_TEXT['provider-attributed']}" in block
        assert "Statements checked against" not in block


def test_the_ledger_carries_each_page_text_to_its_url():
    records = [
        {
            "evidence_id": "ev_1", "query_id": "q1", "canonical_url": "https://www.a-news.de/x",
            "source_id": "s1", "citation_id": "c1", "tier": "mainstream", "source_snippet": PAGE,
            "claims": [{"raw_claim_id": "r1", "claim_text": CLAIM, "citation_set": [
                {"url": "https://www.a-news.de/x", "source_id": "s1", "citation_id": "c1"},
            ]}],
        },
        {
            "evidence_id": "ev_2", "query_id": "q1", "canonical_url": "https://www.b-news.de/y",
            "source_id": "s2", "citation_id": "c2", "tier": "mainstream", "source_snippet": "",
            "claims": [{"raw_claim_id": "r1", "claim_text": CLAIM, "citation_set": [
                {"url": "https://www.b-news.de/y", "source_id": "s2", "citation_id": "c2"},
            ]}],
        },
    ]

    (entry,) = derive_claim_ledger_from_evidence(records)

    assert entry["source_texts"] == {"https://www.a-news.de/x": PAGE}


@pytest.mark.parametrize("status", ["verified", "attributed"])
def test_both_supported_statuses_count_for_needs_primary(consolidator, status):
    claims = [{"status": status, "needs_primary": True, "verification_basis": "primary_source"}]

    counts, score, np_total, np_supported = consolidator.quality_metrics(claims)

    assert counts[status] == 1
    assert (np_total, np_supported) == (1, 1)
    assert score == pytest.approx(0.9)


class TestReferenceStatements:
    """F4/I3: whatever the evidence layer ties to a source is readable at
    that source's reference, from every search that cited it."""

    URL = "https://eur-lex.europa.eu/a"
    DECEMBER = "Teilregelungen von Artikel 5 gelten ab dem 2. Dezember 2026."

    def _records(self) -> list[dict]:
        return [
            {"canonical_url": self.URL, "tier": "primary", "claims": [
                {"claim_text": self.DECEMBER, "verification_status": "attributed", "checked_here": False},
            ]},
            {"canonical_url": self.URL, "tier": "primary", "claims": [
                {"claim_text": self.DECEMBER, "verification_status": "attributed", "checked_here": False},
                {"claim_text": CLAIM, "verification_status": "verified", "checked_here": True},
            ]},
            {"canonical_url": "https://www.b-news.de/y", "tier": "mainstream", "claims": []},
        ]

    def test_every_search_s_statements_reach_the_reference_once(self):
        from types import SimpleNamespace

        from inqtrix.nodes import _format_report_reference_records, _reference_facts_from_evidence_ledger
        from inqtrix.strategies._source_tiering import DefaultSourceTiering

        facts = _reference_facts_from_evidence_ledger(self._records())
        expected = [
            {"text": self.DECEMBER, "status": "attributed", "checked": False},
            {"text": CLAIM, "status": "verified", "checked": True},
        ]
        assert facts.statements_by_url == {self.URL: expected}

        records = _format_report_reference_records(
            [("E1", self.URL), ("E2", "https://www.b-news.de/y")],
            SimpleNamespace(source_tiering=DefaultSourceTiering()),
            tier_by_url=facts.tier_by_url,
            statements_by_url=facts.statements_by_url,
        )
        assert records[0]["statements"] == expected
        assert "statements" not in records[1]

    def test_the_api_reference_carries_the_statements(self):
        from inqtrix.result import _report_references_from_state
        from inqtrix.strategies._source_tiering import DefaultSourceTiering

        (reference,) = _report_references_from_state(
            [{"label": "E1", "url": self.URL, "statements": [
                {"text": self.DECEMBER, "status": "attributed", "checked": False},
                {"text": "", "status": "verified"},
            ]}],
            tier_by_url={},
            tiering=DefaultSourceTiering(),
        )
        assert [(item.text, item.status, item.checked) for item in reference.statements] == [
            (self.DECEMBER, "attributed", False),
        ]

    def test_agent_evidence_prompts_leave_the_statements_out(self):
        from inqtrix.agents.evidence_context import project_evidence_context

        context = project_evidence_context(
            [{"label": "W1", "url": self.URL, "source_run_id": "run_a", "query_id": "q1",
              "statements": [{"text": self.DECEMBER, "status": "attributed", "checked": False}]}],
            {"searches": {}},
        )
        assert "statements" not in context["sources"][0]["reference"]
        assert context["sources"][0]["reference"]["label"] == "W1"
