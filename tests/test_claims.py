""" Tests for claim consolidation, quality metrics, and relevance filtering."""

import pytest

from inqtrix.strategies._source_tiering import DefaultSourceTiering


class _PinnedTiering(DefaultSourceTiering):
    """A strategy with pinned judgements per host (what the run's source
    registry supplies in production); the default floor judges no site."""

    def __init__(self, by_host: dict[str, str]) -> None:
        self._by_host = by_host

    def tier_for_url(self, url: str) -> str:
        for host, tier in self._by_host.items():
            if host in url:
                return tier
        return super().tier_for_url(url)


class TestClaimLedger:

    def test_consolidation_and_metrics(self, consolidator):
        ledger = [
            {
                "signature": "no_plan",
                "claim_text": "Die Bundesregierung plant keine Privatisierung von Zahnleistungen.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": False,
                "source_urls": [
                    "https://www.bundesgesundheitsministerium.de/presse/pm",
                    "https://www.aerzteblatt.de/news/x",
                ],
            },
            {
                "signature": "no_plan",
                "claim_text": "Es gibt keine Regierungsplaene zur Privatisierung.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": False,
                "source_urls": ["https://www.bundesregierung.de/breg-de/aktuelles/x"],
            },
            {
                "signature": "savings_18",
                "claim_text": "Eine Auslagerung spare 18 Milliarden Euro pro Jahr.",
                "claim_type": "actor_claim",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://www.kzbv.de/presse/x"],
            },
            {
                "signature": "majority",
                "claim_text": "Der Vorschlag hat eine parlamentarische Mehrheit.",
                "claim_type": "forecast",
                "polarity": "affirmed",
                "needs_primary": False,
                "source_urls": ["https://www.handelsblatt.com/politik/x"],
            },
            {
                "signature": "majority",
                "claim_text": "Der Vorschlag hat keine parlamentarische Mehrheit.",
                "claim_type": "forecast",
                "polarity": "negated",
                "needs_primary": False,
                "source_urls": ["https://www.tagesspiegel.de/politik/y"],
            },
        ]

        consolidated = consolidator.consolidate(ledger)
        by_sig = {c["signature"]: c for c in consolidated}

        # No source text was checked: the government sources' standing is
        # the provider's attribution, with a primary-source structure.
        assert by_sig["no_plan"]["status"] == "attributed"
        assert by_sig["no_plan"]["verification_basis"] == "primary_source"
        assert by_sig["savings_18"]["status"] == "unverified"
        assert by_sig["majority"]["status"] == "contested"

        counts, score, np_total, np_supported = consolidator.quality_metrics(consolidated)
        assert counts == {"verified": 0, "attributed": 1, "contested": 1, "unverified": 1}
        assert score == pytest.approx(0.467, abs=1e-3)
        assert np_total == 0
        assert np_supported == 0

    def test_claim_prompt_view_contains_status(self, consolidator):
        consolidated = [
            {
                "signature": "s1",
                "claim_text": "Testclaim",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": ["https://example.com/a"],
            }
        ]
        view = consolidator.claims_prompt_view(consolidated, max_items=1)
        assert "status=verified" in view
        assert "Testclaim" in view

    def test_unknown_independent_non_low_domains_cross_check_claim(self, consolidator):
        ledger = [
            {
                "signature": "same_claim",
                "claim_text": "Model X reached 87 percent on Benchmark Y.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://analysis-alpha.example/report"],
                "query_id": "q1",
            },
            {
                "signature": "same_claim",
                "claim_text": "Model X scored 87 percent on Benchmark Y.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://analysis-beta.example/report"],
                "query_id": "q2",
            },
        ]

        by_sig = {c["signature"]: c for c in consolidator.consolidate(ledger)}

        # Two provider answers on two domains corroborate; without source
        # text the corroboration is the provider's attribution.
        assert by_sig["same_claim"]["status"] == "attributed"
        assert by_sig["same_claim"]["verification_basis"] == "cross_checked"
        assert by_sig["same_claim"]["supporting_domain_count"] == 2

    def test_single_unknown_source_stays_unverified(self, consolidator):
        ledger = [
            {
                "signature": "single_claim",
                "claim_text": "Model X reached 87 percent on Benchmark Y.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://analysis-alpha.example/report"],
            }
        ]

        by_sig = {c["signature"]: c for c in consolidator.consolidate(ledger)}

        assert by_sig["single_claim"]["status"] == "unverified"
        assert by_sig["single_claim"]["verification_basis"] == "missing_primary_source"

    def test_low_quality_domains_do_not_cross_check_claim(self, consolidator):
        """Two forum posts (role ``low`` pinned in the ledger) are not two
        independent witnesses: the claim stays unverified."""
        ledger = [
            {
                "signature": "low_claim",
                "claim_text": "Model X reached 87 percent on Benchmark Y.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://www.reddit.com/r/example/comments/a"],
                "source_tiers": {"https://www.reddit.com/r/example/comments/a": "low"},
            },
            {
                "signature": "low_claim",
                "claim_text": "Model X scored 87 percent on Benchmark Y.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://medium.com/example/model-x-benchmark"],
                "source_tiers": {"https://medium.com/example/model-x-benchmark": "low"},
            },
        ]

        by_sig = {c["signature"]: c for c in consolidator.consolidate(ledger)}

        assert by_sig["low_claim"]["status"] == "unverified"
        assert by_sig["low_claim"]["verification_basis"] == "missing_primary_source"

    def test_select_answer_citations_uses_full_claim_source_list(self, consolidator):
        """The full source_urls list of a claim reaches the selection path as
        long as the URLs sit on distinct domains (no Phase 2 cap conflict)."""
        citations = [
            f"https://example-{letter}.com/report/{idx}"
            for idx, letter in enumerate("abcdef")
        ]
        consolidated = [
            {
                "signature": "s1",
                "claim_text": "Testclaim",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": citations,
            }
        ]

        selected = consolidator.select_answer_citations(
            consolidated,
            citations,
            max_items=6,
        )

        assert set(selected) == set(citations)
        assert len(selected) == 6

    def test_select_answer_citations_tier_bias(self, consolidator, tiering):
        """verified+mainstream ranks above unverified+primary; primary still
        wins among verified-tier ties."""
        # mainstream (ard.de) beats a unverified primary claim on bundestag.de
        primary_url = "https://www.bundestag.de/dokumente/primaer"
        mainstream_url = "https://www.tagesschau.de/inland/mainstream"
        primary_url_verified = "https://www.bundesregierung.de/breg-de/primary-v"

        consolidated = [
            {
                "signature": "unv_primary",
                "claim_text": "Unverifizierter primaerer Claim",
                "claim_type": "fact",
                "needs_primary": True,
                "support_count": 1,
                "contradict_count": 0,
                "status": "unverified",
                "source_urls": [primary_url],
            },
            {
                "signature": "ver_mainstream",
                "claim_text": "Verifizierter Mainstream Claim",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": [mainstream_url],
            },
            {
                "signature": "ver_primary",
                "claim_text": "Verifizierter primaerer Claim",
                "claim_type": "fact",
                "needs_primary": True,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": [primary_url_verified],
            },
        ]

        selected = consolidator.select_answer_citations(
            consolidated,
            [primary_url, mainstream_url, primary_url_verified],
            max_items=3,
            source_tiering=_PinnedTiering({
                "bundestag.de": "primary",
                "bundesregierung.de": "primary",
                "tagesschau.de": "mainstream",
            }),
        )

        # verified+primary comes first, verified+mainstream second, then the
        # unverified fallback pass picks up the primary unverified URL.
        assert selected[0] == primary_url_verified
        assert selected[1] == mainstream_url
        assert primary_url in selected

    def test_select_answer_citations_domain_diversity(self, consolidator, tiering):
        """Phase 1 picks one URL per domain before adding a second URL from a
        domain already represented. Phase 2 fills remaining slots with
        additional URLs from seen domains in claim-score order."""
        domain_a_urls = [
            "https://www.bundesregierung.de/breg-de/a1",
            "https://www.bundesregierung.de/breg-de/a2",
        ]
        domain_a_urls_2 = ["https://www.bundesregierung.de/breg-de/a3"]
        domain_b_url = "https://www.tagesschau.de/inland/b1"
        domain_c_url = "https://www.bundestag.de/dokumente/c1"

        consolidated = [
            {
                "signature": "s1",
                "claim_text": "Claim A1 (verified, primary)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": list(domain_a_urls),
            },
            {
                "signature": "s2",
                "claim_text": "Claim A2 (verified, primary, same domain)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": list(domain_a_urls_2),
            },
            {
                "signature": "s3",
                "claim_text": "Claim B (verified, mainstream)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": [domain_b_url],
            },
            {
                "signature": "s4",
                "claim_text": "Claim C (unverified, primary)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 1,
                "contradict_count": 0,
                "status": "unverified",
                "source_urls": [domain_c_url],
            },
        ]

        all_urls = (
            domain_a_urls + domain_a_urls_2 + [domain_b_url, domain_c_url]
        )

        selected = consolidator.select_answer_citations(
            consolidated,
            all_urls,
            max_items=10,
            source_tiering=tiering,
        )

        # Phase 1 — one URL per domain (3 unique domains available).
        assert selected[0] == domain_a_urls[0]
        assert selected[1] == domain_b_url
        assert selected[2] == domain_c_url

        # Phase 2 — up to _PHASE2_MAX_PER_DOMAIN additional URLs per seen
        # domain in claim-score order: claim s1 adds a2, claim s2 adds a3;
        # domain A now at the 3-URL cap.
        assert selected[3] == domain_a_urls[1]
        assert selected[4] == domain_a_urls_2[0]
        assert len(selected) == 5

    def test_select_answer_citations_phase2_domain_cap(
        self, consolidator, tiering
    ):
        """Phase 2 honours the per-domain cap (_PHASE2_MAX_PER_DOMAIN=3).
        A domain that monopolises the claim pool cannot fill more than three
        slots even when max_items still has room."""
        domain_a_urls = [
            f"https://www.bundesregierung.de/breg-de/a{i}" for i in range(1, 7)
        ]
        domain_b_url = "https://www.tagesschau.de/inland/b1"

        consolidated = [
            {
                "signature": f"s{idx}",
                "claim_text": f"Claim A{idx} (verified, primary)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": [url],
            }
            for idx, url in enumerate(domain_a_urls, start=1)
        ] + [
            {
                "signature": "sB",
                "claim_text": "Claim B (verified, mainstream)",
                "claim_type": "fact",
                "needs_primary": False,
                "support_count": 2,
                "contradict_count": 0,
                "status": "verified",
                "source_urls": [domain_b_url],
            }
        ]

        selected = consolidator.select_answer_citations(
            consolidated,
            domain_a_urls + [domain_b_url],
            max_items=10,
            source_tiering=tiering,
        )

        # Phase 1 places one URL per domain (A, B) — 2 slots.
        # Phase 2 tops up from domain A until _PHASE2_MAX_PER_DOMAIN = 3.
        # Remaining A URLs must be skipped even though max_items=10 has room.
        from inqtrix.strategies._claim_consolidation import (
            _PHASE2_MAX_PER_DOMAIN,
        )

        a_count = sum(1 for u in selected if "bundesregierung.de" in u)
        assert a_count == _PHASE2_MAX_PER_DOMAIN
        assert domain_b_url in selected
        assert len(selected) == _PHASE2_MAX_PER_DOMAIN + 1

        # Claim-C's URL must not be duplicated; list has no repeats.
        assert len(selected) == len(set(selected))

    def test_needs_primary_counts_only_facts(self, consolidator):
        ledger = [
            {
                "signature": "np_fact",
                "claim_text": "Die GKV-Ausgaben betragen 18 Milliarden Euro.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://www.bundestag.de/dokumente/x"],
                "source_tiers": {"https://www.bundestag.de/dokumente/x": "primary"},
            },
            {
                "signature": "np_actor",
                "claim_text": "Ein Verband fordert Einsparungen von 18 Milliarden Euro.",
                "claim_type": "actor_claim",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://www.kzbv.de/presse/x"],
            },
        ]
        consolidated = consolidator.consolidate(ledger)
        counts, score, np_total, np_supported = consolidator.quality_metrics(consolidated)
        assert counts["attributed"] == 1
        assert score > 0.0
        assert np_total == 1
        assert np_supported == 1

    def test_needs_primary_with_mainstream_source_remains_report_worthy(self, consolidator):
        """A single mainstream-tier source still supports a claim.

        Reuters / Bloomberg / Tagesschau-tier domains carry editorial weight,
        so one of them gives the structure ``quality_source``; without the
        source's own text the claim is ``attributed``. The depth-gap
        diagnostic (handled elsewhere) is responsible for flagging that the
        claim still rests on one source -- the structure itself stays lax on
        purpose so legitimate quality sources are not buried as
        ``weak_evidence``.
        """
        ledger = [
            {
                "signature": "ai_capex",
                "claim_text": "Hyperscaler investieren 700 Milliarden Euro in KI-Infrastruktur.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": ["https://www.tagesschau.de/wirtschaft/ki-investitionen"],
                "source_tiers": {"https://www.tagesschau.de/wirtschaft/ki-investitionen": "mainstream"},
            },
        ]

        consolidated = consolidator.consolidate(ledger)

        assert consolidated[0]["status"] == "attributed"
        assert consolidated[0]["verification_basis"] == "quality_source"
        counts, score, np_total, np_supported = consolidator.quality_metrics(consolidated)
        assert counts == {"verified": 0, "attributed": 1, "contested": 0, "unverified": 0}
        assert score == pytest.approx(0.7, abs=1e-3)
        assert np_total == 1
        assert np_supported == 1

    def test_negated_only_evidence_does_not_verify_claim(self, consolidator):
        ledger = [
            {
                "signature": "privatisierung",
                "claim_text": "Die Bundesregierung plant keine Privatisierung von Zahnleistungen.",
                "claim_type": "fact",
                "polarity": "negated",
                "needs_primary": False,
                "source_urls": ["https://www.bundesregierung.de/breg-de/aktuelles/x"],
            },
        ]

        consolidated = consolidator.consolidate(ledger)

        assert len(consolidated) == 1
        assert consolidated[0]["support_count"] == 0
        assert consolidated[0]["contradict_count"] == 1
        assert consolidated[0]["status"] == "unverified"


class TestClaimRelevance:

    def test_focus_stems_detect_core_topics(self, consolidator):
        q = "Sollen zahnärztliche Leistungen zukünftig privatisiert werden? In welche Richtung gehen die Diskussionen?"
        stems = consolidator.focus_stems_from_question(q)
        assert any(s.startswith("zahn") for s in stems)
        assert any(s.startswith("priv") for s in stems)

    def test_claim_matches_focus_stems(self, consolidator):
        q = "Sollen zahnärztliche Leistungen zukünftig privatisiert werden?"
        stems = consolidator.focus_stems_from_question(q)

        assert consolidator.claim_matches_focus_stems(
            "Die Bundesregierung plant keine Privatisierung von Zahnleistungen.",
            stems,
        )
        assert not consolidator.claim_matches_focus_stems(
            "Nina Warken ist Mitglied der CDU.",
            stems,
        )


class TestCorroborationCountsPublishers:
    """Independence counts registrable domains, a syndicated text is one
    witness, and a fact claim naming its own issuer is primary only when
    the run could not place the source."""

    def _entry(self, sig, text, url, snippet, *, tiers=None, query="q1"):
        entry = {
            "signature": sig,
            "claim_text": text,
            "claim_type": "fact",
            "polarity": "affirmed",
            "needs_primary": True,
            "source_urls": [url],
            "evidence_snippet": snippet,
            "query_id": query,
        }
        if tiers:
            entry["source_tiers"] = tiers
        return entry

    def test_two_subdomains_of_one_registration_are_one_domain(self, consolidator):
        ledger = [
            self._entry("sub", "Der Zuschuss betraegt 30 Prozent der Kosten.", "https://news.example-bank.de/a", "Zuschuss dreissig Prozent laut Programmseite Anfang des Jahres."),
            self._entry("sub", "Der Zuschuss liegt bei 30 Prozent der Kosten.", "https://www.example-bank.de/b", "Antragsfrist endet im Dezember, Zuschuss dreissig Prozent."),
        ]
        claim = consolidator.consolidate(ledger)[0]
        assert claim["supporting_domain_count"] == 1
        assert claim["status"] == "unverified"
        assert claim["verification_basis"] == "missing_primary_source"

    def test_a_syndicated_text_on_two_domains_is_one_witness(self, consolidator):
        wire = "Die Agentur meldet: Der Zuschuss betraegt dreissig Prozent der foerderfaehigen Kosten fuer Waermepumpen ab Januar."
        ledger = [
            self._entry("syn", "Der Zuschuss betraegt 30 Prozent.", "https://www.first-paper.example/a", wire),
            self._entry("syn", "Der Zuschuss betraegt 30 Prozent.", "https://www.second-paper.example/b", wire + " Weitere Details folgen.", query="q2"),
        ]
        claim = consolidator.consolidate(ledger)[0]
        assert claim["syndicated_count"] == 1
        assert claim["support_count"] == 1
        assert claim["status"] == "unverified"
        # A genuinely different second text from another provider answer
        # still cross-checks (attributed: no source text was checked).
        ledger.append(self._entry("syn", "Der Zuschuss betraegt 30 Prozent.", "https://www.third-paper.example/c", "Eigene Recherche der Redaktion bestaetigt die Hoehe des Zuschusses bei den Behoerden.", query="q3"))
        claim = consolidator.consolidate(ledger)[0]
        assert claim["status"] == "attributed" and claim["verification_basis"] == "cross_checked"

    def test_a_fact_claim_naming_its_own_issuer_is_primary(self, consolidator):
        from inqtrix.strategies._claim_consolidation import issuer_label, issuer_match

        ledger = [self._entry("kfw", "Die KfW foerdert Waermepumpen mit 30 Prozent der Kosten.", "https://www.kfw.de/x", "Foerderung dreissig Prozent.")]
        claim = consolidator.consolidate(ledger)[0]
        assert claim["status"] == "attributed"
        assert claim["verification_basis"] == "primary_source"
        assert claim["primary_basis"] == "issuer:kfw"
        assert claim["status_reason"] == (
            "issuer identifies itself (kfw); attributed by the search provider"
        )
        # A source the extractor placed (a newsroom, a forum) is never "the
        # issuer speaking" because the text names it; a negated entry is
        # no witness at all.
        press = self._entry("sp", "Laut Spiegel liegt die Inflation bei 3,2 Prozent.", "https://www.spiegel.de/x", "s", tiers={"https://www.spiegel.de/x": "mainstream"})
        assert consolidator.consolidate([press])[0]["primary_basis"] == ""
        forum = self._entry("rd", "Reddit-Nutzer melden 30 Prozent Foerderung.", "https://www.reddit.com/r/x", "s", tiers={"https://www.reddit.com/r/x": "low"})
        assert consolidator.consolidate([forum])[0]["status"] == "unverified"
        negated = {**self._entry("neg", "Die KfW foerdert Waermepumpen mit 30 Prozent der Kosten.", "https://www.kfw.de/x", "s"), "polarity": "negated"}
        assert consolidator.consolidate([negated])[0]["primary_basis"] == ""
        # No issuer in the text -> no primary basis; a format label never matches.
        assert consolidator.consolidate([self._entry("x", "Die Foerderung betraegt 30 Prozent.", "https://www.kfw.de/x", "s")])[0]["primary_basis"] == ""
        assert issuer_label("https://www.news.example/x") == ""
        assert issuer_match("Die News melden 30 Prozent.", ["https://www.news.example/x"]) == ""
        assert issuer_match("Die Umweltförderung nennt 30 Prozent.", ["https://www.umweltfoerderung.at/x"]) == "umweltfoerderung"

    @pytest.mark.parametrize(("reason", "expected_basis"), [
        ("unjudged", "issuer:atlas"),
        ("", "issuer:atlas"),
        ("model:aggregator_unknown", ""),
        ("strategy_default", ""),
    ])
    def test_issuer_fallback_distinguishes_unjudged_from_placed_unknown(self, consolidator, reason, expected_basis):
        """Unknown is also a deliberate placement, not always missing judgement."""
        from inqtrix.evidence import derive_claim_ledger_from_evidence

        url = "https://atlas.example/report"
        records = [{
            "evidence_id": "evidence_1", "source_id": "source_1", "query_id": "query_1",
            "canonical_url": url, "tier": "unknown", "tier_reason": reason,
            "claims": [{
                "raw_claim_id": "claim_1", "signature": "grant",
                "claim_text": "Atlas nennt einen Zuschuss von 30 Prozent.",
                "evidence_snippet": "Der Zuschuss betraegt 30 Prozent.",
                "claim_type": "fact", "polarity": "affirmed", "needs_primary": True,
            }],
        }]
        claim = consolidator.consolidate(derive_claim_ledger_from_evidence(records))[0]
        assert claim["primary_basis"] == expected_basis
        assert claim["status"] == ("attributed" if expected_basis else "unverified")


def _valued(
    text: str,
    url: str,
    *,
    tier: str = "mainstream",
    query: str = "q1",
    polarity: str = "affirmed",
) -> dict:
    """One raw ledger entry as the search node builds it: no precomputed
    signature, the URL's pinned tier, one provider answer."""
    return {
        "claim_text": text,
        "claim_type": "fact",
        "polarity": polarity,
        "needs_primary": False,
        "source_urls": [url],
        "source_tiers": {url: tier},
        "evidence_snippet": text,
        "query_id": query,
    }


class TestValueIdentity:
    """A value is part of what a claim says (dates, percentages, amounts)."""

    def test_two_dates_for_one_statement_contest_each_other(self, consolidator):
        consolidated = consolidator.consolidate([
            _valued(
                "Artikel 50 ist ab dem 2. August 2025 anwendbar.",
                "https://eur-lex.europa.eu/legal-content/a", tier="primary",
            ),
            _valued(
                "Artikel 50 ist ab dem 2. August 2026 anwendbar.",
                "https://www.heise.de/news/b", query="q2",
            ),
        ])

        assert [claim["status"] for claim in consolidated] == ["contested", "contested"]
        first, second = consolidated
        assert first["signature"] == second["signature"] == "artikel 50 anwendbar"
        assert first["conflicting_claim_ids"] == [second["claim_id"]]
        assert second["conflicting_claim_ids"] == [first["claim_id"]]
        assert first["contradict_count"] == second["contradict_count"] == 1
        assert first["status_reason"] == "conflicting values"
        assert {tuple(claim["values"]) for claim in consolidated} == {
            ("date:2025-08-02",), ("date:2026-08-02",),
        }

    def test_one_date_written_two_ways_is_one_claim_with_two_supports(self, consolidator):
        consolidated = consolidator.consolidate([
            _valued("Die Frist endet am 02.08.2026.", "https://www.a-news.de/x"),
            _valued("Die Frist endet am 2. August 2026.", "https://www.b-news.de/y", query="q2"),
        ])

        assert len(consolidated) == 1
        assert consolidated[0]["support_count"] == 2
        assert consolidated[0]["status"] == "attributed"
        assert consolidated[0]["verification_basis"] == "cross_checked"

    def test_two_amounts_are_two_claims_not_mutual_support(self, consolidator):
        consolidated = consolidator.consolidate([
            _valued("Die Strafe beträgt 35 Mio. Euro.", "https://www.a-news.de/x"),
            _valued("Die Strafe beträgt 15 Mio. Euro.", "https://www.b-news.de/y", query="q2"),
        ])

        assert len(consolidated) == 2
        assert {claim["status"] for claim in consolidated} == {"contested"}
        assert {claim["support_count"] for claim in consolidated} == {1}

    def test_bounds_split_but_do_not_contest(self, consolidator):
        """Two penalty tiers ("bis zu 35 Mio." and "bis zu 15 Mio.") are no
        conflict, and no longer support each other either."""
        consolidated = consolidator.consolidate([
            _valued("Die Strafe beträgt bis zu 35 Mio. Euro.", "https://www.a-news.de/x"),
            _valued(
                "Die Strafe beträgt bis zu 15 Mio. Euro.", "https://www.b-news.de/y", query="q2",
            ),
        ])

        assert len(consolidated) == 2
        assert {claim["status"] for claim in consolidated} == {"attributed"}
        assert {claim["support_count"] for claim in consolidated} == {1}

    def test_values_in_different_time_scopes_are_separate_statements(self, consolidator):
        consolidated = consolidator.consolidate([
            _valued("Der Mindestlohn steigt 2026 auf 13,90 Euro.", "https://www.a-news.de/x"),
            _valued(
                "Der Mindestlohn steigt 2027 auf 14,60 Euro.", "https://www.b-news.de/y",
                query="q2",
            ),
        ])

        assert len(consolidated) == 2
        assert {claim["status"] for claim in consolidated} == {"attributed"}

    def test_numbers_and_roman_numerals_keep_statements_apart(self, consolidator):
        signature = consolidator.claim_signature
        assert signature("Die Vorschriften von Kapitel II gelten ab dem 2. Februar 2025.") != (
            signature("Die Vorschriften von Kapitel V gelten ab dem 2. August 2025.")
        )
        assert signature("Artikel 5 gilt ab dem 2. Februar 2025.") != signature(
            "Artikel 50 gilt ab dem 2. Februar 2025."
        )
        assert signature("Die Frist beträgt 3 Monate.") != signature("Die Frist beträgt 6 Monate.")
        assert signature("Der Beitrag steigt um 5 Prozent") == "beitrag steigt"

    def test_a_reprint_with_another_value_is_still_the_other_side(self, consolidator):
        """Syndication runs inside a value subgroup: the near-identical text
        with another date is a second statement, not a reprint."""
        consolidated = consolidator.consolidate([
            _valued(
                "Laut EU-Kommission gelten die Transparenzpflichten nach Artikel 50 "
                "der KI-Verordnung ab dem 2. August 2026.",
                "https://www.a-news.de/x",
            ),
            _valued(
                "Laut EU-Kommission gelten die Transparenzpflichten nach Artikel 50 "
                "der KI-Verordnung ab dem 2. August 2025.",
                "https://www.b-news.de/y", query="q2",
            ),
        ])

        assert {claim["status"] for claim in consolidated} == {"contested"}
        assert {claim["syndicated_count"] for claim in consolidated} == {0}

    def test_contested_claims_come_first_and_survive_the_budget(self, consolidator):
        ledger = [
            _valued(f"Studie {name} belegt den Nutzen von KI.", f"https://www.{name}-news.de/x")
            for name in ("alpha", "beta", "gamma")
        ]
        ledger += [
            _valued("Die Strafe beträgt 35 Mio. Euro.", "https://www.a-news.de/x"),
            _valued("Die Strafe beträgt 15 Mio. Euro.", "https://www.b-news.de/y", query="q2"),
        ]

        consolidated = consolidator.consolidate(ledger)

        assert [claim["status"] for claim in consolidated[:2]] == ["contested", "contested"]
        for max_unverified in (0, 1):
            kept = consolidator.materialize(
                consolidated, max_total=2, max_unverified=max_unverified,
            )
            assert [claim["status"] for claim in kept] == ["contested", "contested"]
