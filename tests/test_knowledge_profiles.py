"""Unit tests for the retrieval-profile domain model.

Pins the three contracts the profile feature stands on: the STANDARD
spec encodes exactly the pre-profile pipeline (byte-stability anchor),
the operator ceiling clamps every profile visibly (``degraded_stages``
is never empty when a stage was removed), and the auto heuristics
route the documented German question shapes.
"""

from __future__ import annotations

import pytest

from inqtrix.knowledge.profiles import (
    PROFILE_SPECS,
    RERANK_DEPTH_MAX,
    KnowledgeProfile,
    KnowledgeStageCeiling,
    build_profile_manifest,
    choose_auto_profile,
    parse_knowledge_profile,
    resolve_run_plan,
)


def make_ceiling(**overrides) -> KnowledgeStageCeiling:
    defaults = dict(
        gate_available=True,
        grounding_available=True,
        reranker_available=True,
        gate_max_rounds=3,
        rerank_candidate_depth=40,
    )
    defaults.update(overrides)
    return KnowledgeStageCeiling(**defaults)


class TestSpecInvariants:
    def test_standard_encodes_the_pre_profile_pipeline(self):
        """STANDARD == today's behaviour: rerank on, ONE gate rewrite,
        no bridge, no decomposition, compact answer."""
        spec = PROFILE_SPECS[KnowledgeProfile.STANDARD]
        assert spec.rerank is True
        assert spec.rerank_depth_factor == 1.0
        assert spec.final_k_factor == 1.0
        assert spec.gate is True
        assert spec.gate_rewrite_rounds == 1
        assert spec.vocabulary_bridge is False
        assert spec.decompose is False
        assert spec.report is False

    def test_schnell_drops_exactly_gate_and_rerank(self):
        spec = PROFILE_SPECS[KnowledgeProfile.SCHNELL]
        assert spec.rerank is False
        assert spec.gate is False
        assert spec.gate_rewrite_rounds == 0
        assert spec.decompose is False

    def test_tief_requests_everything_and_tracks_the_env_cap(self):
        spec = PROFILE_SPECS[KnowledgeProfile.TIEF]
        assert spec.decompose is True
        assert spec.report is True
        assert spec.vocabulary_bridge is True
        assert spec.gate_rewrite_rounds is None

    def test_only_tief_widens_the_final_evidence_count(self):
        """Deep raises final_k_factor so its fan-out surfaces more evidence;
        the other profiles keep the shared cap (factor 1.0)."""
        assert PROFILE_SPECS[KnowledgeProfile.TIEF].final_k_factor == 2.0
        for profile in (
            KnowledgeProfile.SCHNELL,
            KnowledgeProfile.STANDARD,
            KnowledgeProfile.GRUENDLICH,
        ):
            assert PROFILE_SPECS[profile].final_k_factor == 1.0

    def test_auto_has_no_spec_entry(self):
        """AUTO is a meta profile; resolution must replace it before
        spec lookup."""
        assert KnowledgeProfile.AUTO not in PROFILE_SPECS


class TestResolveRunPlan:
    def test_no_profile_resolves_to_standard_unchanged(self):
        plan = resolve_run_plan(
            None, question="Was regelt Artikel 5?", ceiling=make_ceiling()
        )
        assert plan.profile is KnowledgeProfile.STANDARD
        assert plan.requested_profile is None
        assert plan.auto_selected is False
        assert plan.rerank is True
        assert plan.rerank_candidate_depth == 40
        assert plan.gate_enabled is True
        assert plan.gate_rewrite_rounds == 1
        assert plan.grounding_enabled is True
        assert plan.vocabulary_bridge is False
        assert plan.decompose is False
        assert plan.report is False
        assert plan.degraded_stages == ()

    def test_final_k_factor_propagates_to_the_plan(self):
        ceiling = make_ceiling()
        assert (
            resolve_run_plan(KnowledgeProfile.TIEF, question="", ceiling=ceiling).final_k_factor
            == 2.0
        )
        assert (
            resolve_run_plan(KnowledgeProfile.STANDARD, question="", ceiling=ceiling).final_k_factor
            == 1.0
        )

    def test_depth_factors_scale_from_the_configured_depth(self):
        ceiling = make_ceiling(rerank_candidate_depth=40)
        gruendlich = resolve_run_plan(
            KnowledgeProfile.GRUENDLICH, question="", ceiling=ceiling
        )
        tief = resolve_run_plan(
            KnowledgeProfile.TIEF, question="", ceiling=ceiling
        )
        assert gruendlich.rerank_candidate_depth == 60
        assert tief.rerank_candidate_depth == 80

    def test_depth_clamps_to_the_settings_upper_bound(self):
        ceiling = make_ceiling(rerank_candidate_depth=150)
        plan = resolve_run_plan(
            KnowledgeProfile.TIEF, question="", ceiling=ceiling
        )
        assert plan.rerank_candidate_depth == RERANK_DEPTH_MAX

    def test_gate_off_ceiling_degrades_every_gated_profile(self):
        ceiling = make_ceiling(gate_available=False)
        for profile in (
            KnowledgeProfile.STANDARD,
            KnowledgeProfile.GRUENDLICH,
            KnowledgeProfile.TIEF,
        ):
            plan = resolve_run_plan(profile, question="", ceiling=ceiling)
            assert plan.gate_enabled is False
            assert plan.gate_rewrite_rounds == 0
            assert "gate" in plan.degraded_stages
            # The bridge lives inside the gate rewrite — no gate, no
            # bridge, without a second degradation entry.
            assert plan.vocabulary_bridge is False

    def test_schnell_does_not_report_gate_degradation(self):
        """A stage the profile never wanted is not 'degraded'."""
        ceiling = make_ceiling(gate_available=False, reranker_available=False)
        plan = resolve_run_plan(
            KnowledgeProfile.SCHNELL, question="", ceiling=ceiling
        )
        assert plan.degraded_stages == ()

    def test_missing_reranker_degrades_visibly(self):
        ceiling = make_ceiling(reranker_available=False)
        plan = resolve_run_plan(
            KnowledgeProfile.STANDARD, question="", ceiling=ceiling
        )
        assert plan.rerank is False
        assert "rerank" in plan.degraded_stages

    def test_round_cap_below_profile_request_is_visible(self):
        ceiling = make_ceiling(gate_max_rounds=1)
        plan = resolve_run_plan(
            KnowledgeProfile.GRUENDLICH, question="", ceiling=ceiling
        )
        assert plan.gate_rewrite_rounds == 1
        assert "gate_rounds" in plan.degraded_stages

    def test_tief_rounds_track_a_raised_cap_without_degradation(self):
        ceiling = make_ceiling(gate_max_rounds=5)
        plan = resolve_run_plan(
            KnowledgeProfile.TIEF, question="", ceiling=ceiling
        )
        assert plan.gate_rewrite_rounds == 5
        assert "gate_rounds" not in plan.degraded_stages

    def test_grounding_off_is_listed_on_every_profile(self):
        ceiling = make_ceiling(grounding_available=False)
        plan = resolve_run_plan(
            KnowledgeProfile.SCHNELL, question="", ceiling=ceiling
        )
        assert plan.grounding_enabled is False
        assert "grounding" in plan.degraded_stages

    def test_auto_resolves_and_records_reason(self):
        plan = resolve_run_plan(
            KnowledgeProfile.AUTO,
            question="Was ist TLPT?",
            ceiling=make_ceiling(),
        )
        assert plan.auto_selected is True
        assert plan.profile is KnowledgeProfile.SCHNELL
        assert plan.auto_reason == "short_simple"
        assert plan.requested_profile is KnowledgeProfile.AUTO


class TestParse:
    def test_accepts_all_names_case_insensitively(self):
        assert (
            parse_knowledge_profile("Gruendlich")
            is KnowledgeProfile.GRUENDLICH
        )
        assert parse_knowledge_profile(" auto ") is KnowledgeProfile.AUTO

    @pytest.mark.parametrize("raw", ["", "deep", 3, None, "gründlich"])
    def test_rejects_unknown_values_naming_the_valid_set(self, raw):
        with pytest.raises(ValueError, match="schnell.*standard.*tief"):
            parse_knowledge_profile(raw)


class TestAutoHeuristics:
    def test_short_lookup_routes_schnell(self):
        profile, reason = choose_auto_profile(
            "Wie hoch ist das maximale Zwangsgeld?"
        )
        assert profile is KnowledgeProfile.SCHNELL
        assert reason == "short_simple"

    def test_d20_style_paraphrase_is_not_schnell(self):
        """The hard-paraphrase class needs the gate — never `schnell`."""
        profile, _ = choose_auto_profile(
            "Wie oft muessen Banken Uebungen durchfuehren lassen, bei "
            "denen beauftragte Hacker einen echten Angriff simulieren?"
        )
        assert profile in (
            KnowledgeProfile.STANDARD,
            KnowledgeProfile.GRUENDLICH,
        )

    def test_b30_style_enumeration_routes_gruendlich(self):
        profile, reason = choose_auto_profile(
            "Welche Pflichten gelten jeweils fuer Backups, "
            "Verschluesselung und Aufbewahrung?"
        )
        assert profile is KnowledgeProfile.GRUENDLICH
        assert reason == "strong_enumeration_marker"

    def test_multiple_question_marks_route_gruendlich(self):
        profile, reason = choose_auto_profile(
            "Wer meldet den Vorfall? Und in welcher Frist?"
        )
        assert profile is KnowledgeProfile.GRUENDLICH
        assert reason == "multiple_questions"

    def test_plain_compound_und_does_not_escalate(self):
        """Ordinary German compounds with a single 'und' must not push
        every question into the expensive profile."""
        profile, _ = choose_auto_profile(
            "Welche Anforderungen stellt das Sicherheits- und "
            "Risikomanagement an die Leitungsebene des Instituts?"
        )
        assert profile is KnowledgeProfile.STANDARD

    def test_never_picks_tief(self):
        monster = (
            "Vergleiche jeweils die Pflichten und Fristen sowie die "
            "Meldewege und Zustaendigkeiten? Und die Ausnahmen? " * 5
        )
        profile, _ = choose_auto_profile(monster)
        assert profile is not KnowledgeProfile.TIEF


class TestManifest:
    def test_lists_all_profiles_plus_delegating_auto(self):
        entries = build_profile_manifest(make_ceiling())
        ids = [entry["id"] for entry in entries]
        assert ids == ["schnell", "standard", "gruendlich", "tief", "auto"]
        auto = entries[-1]
        assert auto["delegates_to"] == ["schnell", "standard", "gruendlich"]
        assert "tief" not in auto["delegates_to"]

    def test_degradation_is_shown_per_profile_not_hidden(self):
        entries = build_profile_manifest(
            make_ceiling(reranker_available=False)
        )
        by_id = {entry["id"]: entry for entry in entries}
        assert by_id["standard"]["degraded"] == ["rerank"]
        assert by_id["standard"]["stages"]["rerank"] is False
        assert by_id["schnell"]["degraded"] == []

    def test_stage_shape_is_complete(self):
        entries = build_profile_manifest(make_ceiling())
        stages = {
            key
            for entry in entries
            if "stages" in entry
            for key in entry["stages"]
        }
        assert stages == {
            "rerank",
            "gate_rounds",
            "grounding",
            "vocabulary_bridge",
            "decompose",
            "report",
        }

    def test_final_k_factor_is_published_per_profile(self):
        # The client recomputes the effective final_k from this factor, so the
        # manifest must carry it (only deep widens beyond the request top_k).
        by_id = {
            entry["id"]: entry
            for entry in build_profile_manifest(make_ceiling())
            if "stages" in entry
        }
        assert by_id["tief"]["final_k_factor"] == 2.0
        assert by_id["standard"]["final_k_factor"] == 1.0
        assert by_id["schnell"]["final_k_factor"] == 1.0
