"""Tests for the central model-tier router (src/inqtrix/model_routing.py).

Resolver logic is tested against lightweight ``SimpleNamespace`` stubs so the
cases are deterministic and independent of any ``.env`` on the machine. A
separate integration block constructs a real ``ModelSettings`` to confirm the
new fields exist and that the ``effective_*`` properties delegate to the
resolver.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.model_routing import (
    NODE_TIER_ASSIGNMENT,
    TIER_NAMES,
    describe_chat_model_options,
    describe_node_resolutions,
    describe_resolution,
    describe_unresolved_resolution,
    normalize_tier,
    resolve_effort,
    resolve_model,
    resolve_tier,
    validate_model_tier,
)
from inqtrix.settings import AgentSettings, ModelSettings

# Derived from the source of truth so a newly-assigned node (e.g. the
# agent/knowledge call sites cannot silently fall out of the resolver's
# coverage: the `set(descs) == set(ALL_NODES)` assertions then verify
# describe_node_resolutions returns EXACTLY the assigned node set.
ALL_NODES = tuple(NODE_TIER_ASSIGNMENT)


def _models(**kwargs: str) -> SimpleNamespace:
    """Build a stub models object; ``reasoning_model`` defaults to ``"R"``.

    The resolver reads ``reasoning_model`` directly and everything else via
    ``getattr(..., default="")``, so a namespace with only the attributes a
    test cares about is sufficient and fully deterministic.
    """
    kwargs.setdefault("reasoning_model", "R")
    return SimpleNamespace(**kwargs)


# --------------------------------------------------------------------------- #
# normalize_tier / resolve_tier
# --------------------------------------------------------------------------- #


def test_tier_names_are_high_mid_fast() -> None:
    assert TIER_NAMES == ("high", "mid", "fast")


@pytest.mark.parametrize(
    "node, expected_tier",
    [
        ("answer", "high"),
        ("plan", "mid"),
        ("evaluate", "mid"),
        ("direct_chat", "mid"),
        ("session_title", "fast"),
        ("classify", "fast"),
        ("claim_extract", "fast"),
        ("agent_deep_review", "high"),
    ],
)
def test_node_tier_assignment_defaults(node: str, expected_tier: str) -> None:
    assert NODE_TIER_ASSIGNMENT[node] == expected_tier
    assert resolve_tier(node) == expected_tier


@pytest.mark.parametrize("raw", ["HIGH", " mid ", "Fast"])
def test_normalize_tier_accepts_case_and_whitespace(raw: str) -> None:
    assert normalize_tier(raw) in TIER_NAMES


@pytest.mark.parametrize("raw", ["", "  ", "bogus", "strong", None])
def test_normalize_tier_rejects_unknown(raw: str | None) -> None:
    assert normalize_tier(raw) is None


@pytest.mark.parametrize("raw, expected", [("", ""), (" HIGH ", "high"), ("fast", "fast")])
def test_validate_model_tier_accepts_empty_and_known(raw: str, expected: str) -> None:
    assert validate_model_tier(raw) == expected


@pytest.mark.parametrize("raw", ["hgih", "bogus", "strong"])
def test_validate_model_tier_rejects_unknown(raw: str) -> None:
    # Strict config-boundary check: unlike normalize_tier, an unknown non-empty
    # value raises instead of silently falling back to the default assignment.
    with pytest.raises(ValueError, match="model_tier must be one of"):
        validate_model_tier(raw)


def test_resolve_tier_requested_overrides_reasoning_defaults() -> None:
    assert resolve_tier("plan", "high") == "high"
    assert resolve_tier("answer", "fast") == "fast"


@pytest.mark.parametrize("node", [
    "classify", "claim_extract", "knowledge_gate", "knowledge_contextualize",
    "knowledge_decompose", "knowledge_rerank", "agent_intake", "agent_sufficiency",
    "agent_skill_point_check",
    "session_title",
])
def test_fast_nodes_keep_their_model_and_effort_under_run_tier(node: str) -> None:
    models = _models(
        tier_fast_model="F", tier_mid_model="M", tier_high_model="H",
        tier_fast_effort="none", tier_mid_effort="medium", tier_high_effort="high",
    )
    for requested in ("high", "mid", "fast"):
        desc = describe_resolution(node, models, requested_tier=requested)
        assert desc["requested_tier"] == requested
        assert desc["tier"] == "fast"
        assert desc["model"] == "F"
        assert desc["model_source"] == "tier:fast"
        assert desc["effort"] == "none"
        assert desc["effort_source"] == "tier:fast"


def test_resolve_tier_invalid_requested_falls_back_to_default() -> None:
    assert resolve_tier("answer", "bogus") == "high"
    assert resolve_tier("answer", "") == "high"
    assert resolve_tier("answer", None) == "high"


# --------------------------------------------------------------------------- #
# resolve_model
# --------------------------------------------------------------------------- #


def test_layer1_all_nodes_use_reasoning_model_when_nothing_else_set() -> None:
    models = _models(reasoning_model="only-model")
    for node in ALL_NODES:
        assert resolve_model(node, models) == "only-model"


def test_layer2_tiers_map_to_nodes() -> None:
    models = _models(tier_high_model="H", tier_mid_model="M", tier_fast_model="F")
    assert resolve_model("answer", models) == "H"
    assert resolve_model("plan", models) == "M"
    assert resolve_model("evaluate", models) == "M"
    assert resolve_model("direct_chat", models) == "M"
    assert resolve_model("classify", models) == "F"
    assert resolve_model("claim_extract", models) == "F"


def test_layer3_per_node_override_beats_tier() -> None:
    models = _models(tier_high_model="H", answer_model="A")
    assert resolve_model("answer", models) == "A"


def test_empty_tier_falls_through_to_reasoning_model() -> None:
    models = _models(reasoning_model="R", tier_high_model="")
    assert resolve_model("answer", models) == "R"


def test_requested_tier_replaces_reasoning_default_tier() -> None:
    models = _models(tier_high_model="H", tier_fast_model="F")
    assert resolve_model("plan", models, requested_tier="high") == "H"


def test_per_node_override_beats_requested_tier() -> None:
    models = _models(tier_high_model="H", classify_model="C")
    assert resolve_model("classify", models, requested_tier="high") == "C"


def test_invalid_requested_tier_keeps_default_tier() -> None:
    models = _models(tier_fast_model="F", tier_high_model="H")
    assert resolve_model("classify", models, requested_tier="nope") == "F"


# --------------------------------------------------------------------------- #
# resolve_effort
# --------------------------------------------------------------------------- #


def test_effort_unset_returns_empty_string() -> None:
    models = _models()
    for node in ALL_NODES:
        assert resolve_effort(node, models) == ""


def test_effort_from_tier() -> None:
    models = _models(tier_high_effort="high")
    assert resolve_effort("answer", models) == "high"
    # fast tier effort unset -> empty (inherit provider default).
    assert resolve_effort("classify", models) == ""


def test_effort_requested_tier_selects_tier_effort() -> None:
    models = _models(tier_fast_effort="none", tier_high_effort="high")
    assert resolve_effort("plan", models, requested_tier="high") == "high"


def test_effort_none_sentinel_is_distinct_from_unset() -> None:
    # Explicit "none" forces reasoning off; it must not collapse to "".
    models = _models(tier_high_effort="none")
    assert resolve_effort("answer", models) == "none"


# --------------------------------------------------------------------------- #
# describe_resolution (single source; the wrappers delegate to it)
# --------------------------------------------------------------------------- #


def test_describe_resolution_model_source_reasoning_default() -> None:
    desc = describe_resolution("answer", _models(reasoning_model="R"))
    assert desc["model"] == "R"
    assert desc["model_source"] == "reasoning_model_default"
    assert desc["tier"] == "high"
    assert desc["effort"] == ""
    assert desc["effort_source"] == "provider_default"
    assert desc["requested_tier"] == ""


def test_describe_resolution_model_source_tier() -> None:
    desc = describe_resolution("classify", _models(tier_fast_model="F"))
    assert desc["model"] == "F"
    assert desc["model_source"] == "tier:fast"


def test_describe_resolution_model_source_per_node_override() -> None:
    desc = describe_resolution("answer", _models(tier_high_model="H", answer_model="A"))
    assert desc["model"] == "A"
    assert desc["model_source"] == "per_node_override"


def test_describe_resolution_effort_source_tier() -> None:
    desc = describe_resolution("answer", _models(tier_high_effort="medium"))
    assert desc["effort"] == "medium"
    assert desc["effort_source"] == "tier:high"


def test_describe_resolution_records_requested_tier() -> None:
    desc = describe_resolution(
        "plan", _models(tier_high_model="H"), requested_tier="high"
    )
    assert desc["requested_tier"] == "high"
    assert desc["tier"] == "high"
    assert desc["model"] == "H"
    assert desc["model_source"] == "tier:high"


def test_wrappers_delegate_to_describe_resolution() -> None:
    models = _models(tier_high_model="H", tier_high_effort="high", answer_model="A")
    for node in ALL_NODES:
        desc = describe_resolution(node, models)
        assert resolve_model(node, models) == desc["model"]
        assert resolve_effort(node, models) == desc["effort"]


def test_describe_node_resolutions_respects_requested_tier() -> None:
    models = _models(tier_high_model="H", tier_mid_model="M", tier_fast_model="F")
    descs = describe_node_resolutions(models, requested_tier="fast")
    assert set(descs) == set(ALL_NODES)
    assert descs["answer"]["model"] == "F"
    assert descs["answer"]["tier"] == "fast"
    assert descs["evaluate"]["model"] == "F"


def test_describe_node_resolutions_preserves_per_node_override() -> None:
    models = _models(tier_fast_model="F", answer_model="PINNED")
    descs = describe_node_resolutions(models, requested_tier="fast")
    assert descs["answer"]["model"] == "PINNED"
    assert descs["answer"]["model_source"] == "per_node_override"


def test_describe_unresolved_resolution_shape_is_stable() -> None:
    desc = describe_unresolved_resolution("answer", requested_tier="fast")
    assert desc == {
        "node": "answer",
        "model": "",
        "tier": "fast",
        "effort": "",
        "model_source": "provider_models_missing",
        "effort_source": "provider_default_unseen",
        "requested_tier": "fast",
    }


def test_describe_node_resolutions_missing_models_is_loud_shape() -> None:
    descs = describe_node_resolutions(None, requested_tier="high")
    assert set(descs) == set(ALL_NODES)
    assert descs["claim_extract"]["model"] == ""
    assert descs["claim_extract"]["model_source"] == "provider_models_missing"
    assert descs["claim_extract"]["effort_source"] == "provider_default_unseen"


def test_describe_chat_model_options_resolves_direct_chat_for_all_tiers() -> None:
    models = _models(
        tier_high_model="H",
        tier_mid_model="M",
        tier_fast_model="F",
        tier_high_effort="medium",
        tier_mid_effort="none",
    )

    options = describe_chat_model_options(models)

    assert [option["tier"] for option in options] == ["high", "mid", "fast"]
    assert [option["requested_tier"] for option in options] == ["high", "mid", "fast"]
    assert [option["node"] for option in options] == ["direct_chat"] * 3
    assert [option["model"] for option in options] == ["H", "M", "F"]
    assert [option["effort"] for option in options] == ["medium", "none", ""]


def test_describe_chat_model_options_missing_models_is_loud_shape() -> None:
    options = describe_chat_model_options(None)

    assert [option["tier"] for option in options] == ["high", "mid", "fast"]
    assert all(option["node"] == "direct_chat" for option in options)
    assert all(option["model"] == "" for option in options)
    assert all(option["model_source"] == "provider_models_missing" for option in options)
    assert all(option["effort_source"] == "provider_default_unseen" for option in options)


# --------------------------------------------------------------------------- #
# ModelSettings integration
# --------------------------------------------------------------------------- #


def test_modelsettings_exposes_all_new_fields() -> None:
    ms = ModelSettings()
    for field in (
        "tier_high_model",
        "tier_mid_model",
        "tier_fast_model",
        "tier_high_effort",
        "tier_mid_effort",
        "tier_fast_effort",
        "plan_model",
        "answer_model",
        "direct_chat_model",
    ):
        assert hasattr(ms, field), field


def test_effective_properties_delegate_to_resolver() -> None:
    ms = ModelSettings(reasoning_model="R", classify_model="C", evaluate_model="E")
    assert ms.effective_classify_model == resolve_model("classify", ms) == "C"
    assert ms.effective_evaluate_model == resolve_model("evaluate", ms) == "E"
    assert ms.effective_claim_extract_model == resolve_model("claim_extract", ms)


def test_agent_settings_drops_escalation_and_adds_model_tier() -> None:
    fields = set(AgentSettings.model_fields)
    assert "high_risk_classify_escalate" not in fields
    assert "high_risk_evaluate_escalate" not in fields
    assert "model_tier" in fields
    assert "high_risk_score_threshold" in fields  # risk scoring itself stays


def test_tier_vocabulary_has_one_definition() -> None:
    """Every field that carries a tier is typed with ``ModelTier``; a second
    literal list would drift from the tiers the resolver actually knows."""
    from typing import get_args

    from inqtrix.model_routing import ModelTier
    from inqtrix.services.overrides import AgentOverridesRequest

    assert TIER_NAMES == get_args(ModelTier)
    annotation = AgentOverridesRequest.model_fields["model_tier"].annotation
    literal = next(arg for arg in get_args(annotation) if arg is not type(None))
    assert get_args(literal) == TIER_NAMES
