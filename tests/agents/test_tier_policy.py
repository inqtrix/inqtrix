"""Contracts of the Stufen ladder (tier_policy + its consumers).

What these tests pin: the policy table drives the web budgets and the
kernel's blocked tools deterministically (published == enforced), the legacy
``tier=None`` path stays byte-identical, and the override seam bridges
tier -> depth without ever touching model selection.
"""

from __future__ import annotations

from typing import get_args

import pytest
from fastapi import HTTPException

from inqtrix.agents.tier_policy import (
    AGENT_TIERS,
    TIER_POLICIES,
    VERIFY_LEVELS,
    resolve_tier_policy,
    resolve_verify_level,
    review_required,
    tier_capabilities_payload,
)
from inqtrix.agents.web_execution_policy import ResearchProfile, derive_web_research_policy
from inqtrix.services.overrides import (
    apply_overrides,
    parse_overrides_payload,
)
from inqtrix.settings import AgentSettings


def test_web_policy_per_tier_matches_the_table():
    schnell = derive_web_research_policy(depth="normal", tier="schnell")
    assert schnell.allowed is False
    assert schnell.profile is None

    gruendlich = derive_web_research_policy(depth="normal", tier="gruendlich")
    assert gruendlich.allowed is True
    assert gruendlich.profile == "schnell"

    tief = derive_web_research_policy(depth="deep", tier="tief")
    assert tief.allowed is True
    assert tief.profile == "compact"


def test_web_policy_without_tier_keeps_depth_profiles():
    assert derive_web_research_policy(depth="deep") == (
        derive_web_research_policy(depth="deep", tier=None)
    )
    normal = derive_web_research_policy(depth="normal")
    assert normal.allowed is True
    assert normal.profile == "compact"


def test_overrides_bridge_tier_into_depth_and_reject_contradictions():
    base = AgentSettings()
    tief = apply_overrides(
        base, parse_overrides_payload({"agent_tier": "tief"})
    )
    assert tief.agent_tier == "tief"
    assert tief.depth == "deep"
    schnell = apply_overrides(
        base, parse_overrides_payload({"agent_tier": "schnell"})
    )
    assert schnell.depth == "normal"
    # The CONSISTENT pair replays fine (worker resume body).
    replay = parse_overrides_payload(
        {"agent_tier": "tief", "depth": "deep"}
    )
    assert replay is not None and replay.agent_tier == "tief"
    with pytest.raises(HTTPException) as denied:
        parse_overrides_payload({"agent_tier": "tief", "depth": "normal"})
    assert denied.value.status_code == 400
    with pytest.raises(HTTPException):
        parse_overrides_payload({"agent_tier": "extrem"})


def test_capabilities_payload_mirrors_the_policy_table():
    payload = tier_capabilities_payload()
    assert [entry["id"] for entry in payload] == list(AGENT_TIERS)
    for entry in payload:
        policy = TIER_POLICIES[entry["id"]]  # type: ignore[index]
        # The rounds are the table's: the kernel enforces only a zero.
        assert entry["clarification_rounds"] == policy.clarification_rounds
        assert entry["response_form"] == policy.response_form
        assert entry["web_instant_budget"] == policy.web_instant_budget
        assert entry["knowledge_search_budget"] == policy.knowledge_search_budget
        # No human-readable display text in the manifest: the server
        # cannot know the reader's language, and it published a German
        # latency window that the English composer printed verbatim.
        # Tier wording belongs to the surface's own translations.
        assert "latency_hint" not in entry
        if entry["web_child_profile"] is not None:
            assert entry["web_child_profile"] in get_args(ResearchProfile)


def test_schnell_caps_both_source_searches_at_one():
    """The seconds-scale tier promises ONE web search; a wider step ceiling
    (read allowance) would otherwise be spent on repeated knowledge
    searches, so the knowledge search is capped the same way and both caps
    are published."""
    schnell = TIER_POLICIES["schnell"]
    assert (schnell.web_instant_budget, schnell.knowledge_search_budget) == (1, 1)
    for tier in ("gruendlich", "tief"):
        assert TIER_POLICIES[tier].knowledge_search_budget is None


def test_unknown_tier_fails_loudly():
    with pytest.raises(ValueError):
        resolve_tier_policy("extrem")


def test_the_kernel_tools_a_stufe_removes_are_derived_from_the_table():
    """The ladder is one search, then research children: ``schnell`` starts
    no child at all, a batch included, and the kernel tools a Stufe removes
    are DERIVED from the table — never a second list. ``write_canvas`` is
    never removed: a document on request is possible in every Stufe."""
    from inqtrix.agents.tier_policy import blocked_kernel_tools

    assert all("mission" not in entry for entry in tier_capabilities_payload())
    assert blocked_kernel_tools(TIER_POLICIES["schnell"]) == frozenset(
        {"ask_user", "run_web_research", "delegate_batch", "analyze_document"}
    )
    assert blocked_kernel_tools(TIER_POLICIES["gruendlich"]) == frozenset()
    assert blocked_kernel_tools(TIER_POLICIES["tief"]) == frozenset()
    assert all(
        "write_canvas" not in blocked_kernel_tools(TIER_POLICIES[t])
        for t in AGENT_TIERS
    )


def test_the_review_promise_of_a_stufe_comes_from_the_published_table():
    """One predicate answers "does this run verify?".

    Derived from the table rather than from a second list: a Stufe whose
    published ``verify`` level says it reviews must actually review, or the
    contract in ``/v1/capabilities`` is a claim instead of a rule.
    """
    for tier in AGENT_TIERS:
        level = TIER_POLICIES[tier].verify
        assert level in VERIFY_LEVELS
        assert review_required(level) is (level != "labels")
        assert resolve_verify_level(tier, legacy="labels") == level
    # The default Stufe of the composer is one that verifies — that is the
    # promise the surface makes to every user who changes nothing.
    assert review_required(TIER_POLICIES["gruendlich"].verify) is True
    assert review_required(TIER_POLICIES["schnell"].verify) is False


def test_a_run_without_a_stufe_keeps_the_caller_s_legacy_level():
    """No Stufe, no table: the caller's pre-Stufen semantics stay exact.

    Same rule as ``derive_web_research_policy`` for the neighbouring field.
    A tiers-aware client always sends a Stufe, so this path carries only
    requests that predate the ladder.
    """
    assert resolve_verify_level("", legacy="standard") == "standard"
    assert resolve_verify_level(None, legacy="labels") == "labels"
    # A Stufe always wins over the legacy argument.
    assert resolve_verify_level("schnell", legacy="escalating") == "labels"


