""" Tests for source tiering and quality scoring strategies."""

import pytest


class TestSourceTiering:
    """The default strategy is a structural floor: registry-class suffixes
    prove a role, everything else is unjudged until the extractor judges it.
    The domains are invented on purpose — a site list would pass on names,
    the floor must pass on the suffix family alone."""

    @pytest.mark.parametrize(("url", "role"), [
        ("https://foerderung.example.gv.at/waermepumpe", "official_primary"),
        ("https://www.sec.gov/Archives/edgar/data/1", "official_primary"),
        ("https://ecb.europa.eu/press/pr/date/2026", "official_primary"),
        ("https://www.bmwk.bund.de/Redaktion/DE/foerderung", "official_primary"),
        ("https://www.judiciary.uk/judgments/x", "official_primary"),
        ("https://www.meti.go.jp/policy/x", "official_primary"),
        ("https://hai.stanford.edu/ai-index", "academic_primary"),
        ("https://www.cs.ox.ac.uk/news/x", "academic_primary"),
        ("https://research.example.ac.at/paper", "academic_primary"),
    ])
    def test_structural_suffixes_are_primary_with_a_named_reason(self, tiering, url, role):
        assert tiering.tier_for_url(url) == "primary"
        explanation = tiering.explain_url(url)
        assert explanation["tier"] == "primary"
        assert explanation["tier_reason"] == f"structural:{role}"

    @pytest.mark.parametrize("url", [
        "https://www.kfw.de/inlandsfoerderung/x",
        "https://www.bundestag.de/dokumente",
        "https://www.spiegel.de/politik/x",
        "https://www.reddit.com/r/example/comments/a",
        "https://random-hobbyblog.example/article",
        "https://www.gov.example/not-a-registry",
    ])
    def test_everything_else_is_unjudged_not_a_guess(self, tiering, url):
        """No site list: a newsroom, a forum and a federal bank are all
        ``unknown``/``unjudged`` until the extractor judges them — the floor
        never invents mainstream, stakeholder or low."""
        assert tiering.tier_for_url(url) == "unknown"
        assert tiering.explain_url(url) == {"domain": tiering.explain_url(url)["domain"], "tier": "unknown", "tier_reason": "unjudged"}

    def test_empty_or_malformed_url_is_unknown_without_raising(self, tiering):
        assert tiering.tier_for_url("") == "unknown"
        assert tiering.explain_url("")["tier_reason"] == "no_domain"
        assert tiering.tier_for_url("not a url") == "unknown"

    def test_the_floor_stays_a_floor(self):
        """Admission rule of the structural floor, pinned with literals (a
        bound imported from the module under test would grow with the
        list): seven registry-class labels, four reserved TLDs, at most
        sixteen explicit state registries, two roles, never a site."""
        from inqtrix.domains import (
            STRUCTURAL_LABEL_ROLES,
            STRUCTURAL_REGISTRY_ROLES,
            STRUCTURAL_TLD_ROLES,
        )
        assert len(STRUCTURAL_LABEL_ROLES) <= 7
        assert len(STRUCTURAL_TLD_ROLES) <= 4
        assert 0 < len(STRUCTURAL_REGISTRY_ROLES) <= 16
        for table in (STRUCTURAL_LABEL_ROLES, STRUCTURAL_TLD_ROLES, STRUCTURAL_REGISTRY_ROLES):
            assert set(table.values()) <= {"official_primary", "academic_primary"}
        assert all(suffix.count(".") == 1 for suffix in STRUCTURAL_REGISTRY_ROLES), "a registry, never a site"
        assert "go" not in STRUCTURAL_LABEL_ROLES, "go.it is the province of Gorizia"

    @pytest.mark.parametrize(("url", "role"), [
        ("https://www.info.gov.hk/x", "official_primary"),
        ("https://www.argentina.gob.ar/x", "official_primary"),
        ("https://www.tel-aviv.ac.il/x", "academic_primary"),
        ("https://www.uw.edu.pl/x", "academic_primary"),
        ("https://www.meti.go.jp/x", "official_primary"),
    ])
    def test_the_floor_is_a_rule_over_the_public_suffix_list(self, tiering, url, role):
        """Registry-class suffixes of every country come from the bundled
        public suffix list, not from a country list — and an ambiguous
        label family stays out (``go.it`` is a province)."""
        assert tiering.explain_url(url)["tier_reason"] == f"structural:{role}"
        assert tiering.explain_url("https://www.comune.go.it/x")["tier_reason"] == "unjudged"

    def test_no_allow_lists_survive(self):
        import inqtrix.domains as domains
        for name in ("PRIMARY_SOURCE_DOMAINS", "MAINSTREAM_SOURCE_DOMAINS", "STAKEHOLDER_SOURCE_DOMAINS", "LOW_QUALITY_DOMAINS", "PRIMARY_REGULATOR_DOMAINS"):
            assert not hasattr(domains, name), name

    def test_source_quality_score_weighted(self, tiering):
        counts, score = tiering.quality_from_urls([
            "https://www.sec.gov/x",
            "https://hai.stanford.edu/y",
            "https://www.kfw.de/z",
        ])
        assert counts["primary"] == 2 and counts["unknown"] == 1
        assert score == round((1.0 + 1.0 + 0.35) / 3, 3)
        assert tiering.quality_from_urls([]) == ({"primary": 0, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0}, 0.0)

    def test_quality_from_pinned_tiers_matches_the_url_arithmetic(self):
        from inqtrix.strategies._source_tiering import quality_from_tiers
        counts, score = quality_from_tiers(["primary", "mainstream", "low", "weird"])
        assert counts == {"primary": 1, "mainstream": 1, "stakeholder": 0, "unknown": 1, "low": 1}
        assert score == round((1.0 + 0.8 + 0.1 + 0.35) / 4, 3)

    @pytest.mark.parametrize(("url", "expected"), [
        ("https://foerderung.example.gv.at/x", "example.gv.at"),
        ("https://www.kfw.de/x", "kfw.de"),
        ("https://news.kfw.de/x", "kfw.de"),
        ("https://ecb.europa.eu/x", "europa.eu"),
        ("https://www.gov.uk/x", "gov.uk"),
        ("https://alice.github.io/x", "alice.github.io"),
        ("https://foo.blogspot.com/x", "foo.blogspot.com"),
        ("https://10.0.0.1/x", "10.0.0.1"),
        ("", ""),
    ])
    def test_registrable_domain_uses_the_bundled_public_suffix_list(self, url, expected):
        from inqtrix.urls import registrable_domain
        assert registrable_domain(url) == expected


class TestRiskScoring:

    def test_risk_score_accumulates_topic_neutral_signals(self, risk_scorer):
        # No German-policy bonus: the score reflects only the
        # topic-neutral signals -- recency ("aktuell"), numeric ("Mrd Euro"),
        # and normative phrasing ("Sollen") -- which sum to 3.
        q = "Sollen Leistungen privatisiert werden, welche Kosten in Mrd Euro und welche Gesetzeslage gilt aktuell?"
        assert risk_scorer.score(q) == 3

