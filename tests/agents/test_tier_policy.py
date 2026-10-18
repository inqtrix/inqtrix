"""Contracts of the Stufen ladder (tier_policy + its consumers).

What these tests pin: the policy table drives web budgets and plan
validation deterministically (published == enforced), the legacy
``tier=None`` path stays byte-identical, and the override seam bridges
tier -> depth without ever touching model selection.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from inqtrix.agents.plan_models import (
    ExecutionPlanModel,
    WEB_RESEARCH_PROFILES,
)
from inqtrix.agents.plan_validation import validate_plan
from inqtrix.agents.tier_policy import (
    AGENT_TIERS,
    TIER_POLICIES,
    VERIFY_LEVELS,
    effective_clarification_rounds,
    resolve_tier_policy,
    resolve_verify_level,
    review_required,
    tier_capabilities_payload,
)
from inqtrix.agents.web_execution_policy import derive_web_research_policy
from inqtrix.services.overrides import (
    apply_overrides,
    parse_overrides_payload,
)
from inqtrix.settings import AgentSettings


def _plan(profile: str | None) -> ExecutionPlanModel:
    return ExecutionPlanModel.model_validate(
        {
            "summary_markdown": "Plan",
            "tasks": [
                {
                    "id": "t1",
                    "title": "Recherche",
                    "tool_kind": "web_research",
                    "queries": ["Frage eins"],
                    **(
                        {"params": {"profile": profile}}
                        if profile is not None
                        else {}
                    ),
                },
                {
                    "id": "s",
                    "title": "Synthese",
                    "tool_kind": "synthesis",
                    "depends_on": ["t1"],
                },
            ],
        }
    )


def test_web_policy_per_tier_matches_the_table():
    schnell = derive_web_research_policy(depth="normal", tier="schnell")
    assert schnell.allowed is False
    assert schnell.profile is None

    gruendlich = derive_web_research_policy(depth="normal", tier="gruendlich")
    assert gruendlich.allowed is True
    assert gruendlich.profile == "schnell"
    assert gruendlich.max_profile == "compact"

    tief = derive_web_research_policy(depth="deep", tier="tief")
    assert tief.allowed is True
    assert tief.profile == "compact"
    assert tief.max_profile == "deep"


def test_web_policy_without_tier_keeps_depth_profiles():
    assert derive_web_research_policy(depth="deep") == (
        derive_web_research_policy(depth="deep", tier=None)
    )
    legacy = derive_web_research_policy(depth="normal", admitted_directive=True)
    assert legacy.allowed is True
    assert legacy.profile == "compact"
    assert legacy.max_profile is None
    normal = derive_web_research_policy(depth="normal")
    assert normal.allowed is True
    assert normal.profile == "compact"


def test_validate_plan_enforces_the_tier_ceiling():
    over = validate_plan(
        _plan("deep"),
        web_research_allowed=True,
        web_research_profile_ceiling="compact",
    )
    assert any("exceeds the search depth" in e for e in over)
    within = validate_plan(
        _plan("schnell"),
        web_research_allowed=True,
        web_research_profile_ceiling="compact",
    )
    assert within == []
    # Missing profile is fine — the server default applies at execution.
    defaulted = validate_plan(
        _plan(None),
        web_research_allowed=True,
        web_research_profile_ceiling="compact",
    )
    assert defaulted == []


def test_validate_plan_rejects_research_when_the_tier_forbids_it():
    errors = validate_plan(_plan("schnell"), web_research_allowed=False)
    assert any("web_instant" in e for e in errors)


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
        assert entry["plan_gate"] == policy.plan_gate
        assert entry["response_form"] == policy.response_form
        assert entry["web_child_ceiling"] == policy.web_child_ceiling
        assert entry["web_instant_budget"] == policy.web_instant_budget
        assert entry["knowledge_search_budget"] == policy.knowledge_search_budget
        # No human-readable display text in the manifest: the server
        # cannot know the reader's language, and it published a German
        # latency window that the English composer printed verbatim.
        # Tier wording belongs to the surface's own translations.
        assert "latency_hint" not in entry
        if entry["web_child_profile"] is not None:
            assert entry["web_child_profile"] in WEB_RESEARCH_PROFILES


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


def test_schnell_instant_budget_is_validator_enforced():
    """The published 'exactly one web_instant' budget is never
    prompt-only: a second instant task is a repairable violation."""
    from inqtrix.agents.plan_models import ExecutionPlanModel
    from inqtrix.agents.plan_validation import validate_plan
    from inqtrix.agents.web_execution_policy import derive_web_research_policy

    policy = derive_web_research_policy(depth="normal", tier="schnell")
    assert policy.max_instant_tasks == 1
    plan = ExecutionPlanModel.model_validate({
        "summary_markdown": "s",
        "assumptions": [],
        "success_criteria": [],
        "tasks": [
            {
                "id": "t1",
                "title": "Frage eins beantworten",
                "tool_kind": "web_instant",
                "objective": "",
                "queries": ["Frage eins"],
                "gap_ids": [],
                "depends_on": [],
                "budget": {},
                "params": {},
                "expected_output": "",
            },
            {
                "id": "t2",
                "title": "Frage zwei beantworten",
                "tool_kind": "web_instant",
                "objective": "",
                "queries": ["Frage zwei"],
                "gap_ids": [],
                "depends_on": [],
                "budget": {},
                "params": {},
                "expected_output": "",
            },
            {
                "id": "s",
                "title": "Memo",
                "tool_kind": "synthesis",
                "objective": "",
                "queries": [],
                "gap_ids": [],
                "depends_on": ["t1", "t2"],
                "budget": {},
                "params": {},
                "expected_output": "",
            },
        ],
    })
    errors = validate_plan(
        plan,
        web_research_allowed=False,
        max_web_instant_tasks=policy.max_instant_tasks,
    )
    assert any("at most 1 web_instant" in error for error in errors)
    assert any("t1, t2" in error for error in errors)


def test_mission_column_makes_the_middle_rung_deterministic():
    """The ladder is one search, one research child, one mission: only
    ``tief`` may delegate a mission, and the kernel tools a Stufe removes
    are DERIVED from the table — never a second list. ``write_canvas`` is
    never removed: a document on request is possible in every Stufe."""
    from inqtrix.agents.tier_policy import blocked_kernel_tools

    assert [TIER_POLICIES[t].mission for t in AGENT_TIERS] == [
        False,
        False,
        True,
    ]
    assert [e["mission"] for e in tier_capabilities_payload()] == [
        False,
        False,
        True,
    ]
    assert blocked_kernel_tools(TIER_POLICIES["schnell"]) == frozenset(
        {"ask_user", "run_web_research", "run_deep_mission", "delegate_batch"}
    )
    assert blocked_kernel_tools(TIER_POLICIES["gruendlich"]) == frozenset(
        {"run_deep_mission"}
    )
    assert blocked_kernel_tools(TIER_POLICIES["tief"]) == frozenset()
    assert all(
        "write_canvas" not in blocked_kernel_tools(TIER_POLICIES[t])
        for t in AGENT_TIERS
    )


def test_the_review_promise_of_a_stufe_comes_from_the_published_table():
    """One predicate answers "does this run verify?", for both engines.

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


def test_how_far_a_section_may_reach_is_published_as_it_is_enforced():
    """The register width comes from the table and reaches the manifest.

    One source for both, so the width the composer can read is the width
    the sections are written with - and a deeper Stufe reaches further.
    """
    from inqtrix.agents.tier_policy import resolve_section_register

    payload = {entry["id"]: entry for entry in tier_capabilities_payload()}
    widths = []
    for tier in AGENT_TIERS:
        width = TIER_POLICIES[tier].section_register
        assert resolve_section_register(tier) == width
        assert payload[tier]["section_register"] == width
        widths.append(width)
    assert widths == sorted(widths) and len(set(widths)) == len(widths)


def test_a_run_without_a_stufe_keeps_the_register_it_had_before_the_ladder():
    """A request that names no Stufe sees the width the register shipped with.

    Stated as the number itself, not as the constant that holds it: a test
    that compares a value with its own definition cannot notice the value
    moving.
    """
    from inqtrix.agents.tier_policy import resolve_section_register

    assert resolve_section_register("") == 60
    assert resolve_section_register(None) == 60


def test_an_operator_register_width_is_published_as_the_one_it_enforces():
    """An override replaces every Stufe's width - in the manifest too.

    Otherwise the manifest would publish the table while the sections are
    written with the operator's number.
    """
    for override in (0, 7):
        payload = tier_capabilities_payload(section_register_override=override)
        assert {entry["section_register"] for entry in payload} == {override}


def test_the_manifest_publishes_the_register_width_the_operator_set():
    """The router hands the operator's number to the manifest.

    The payload function can publish an override only if the router passes
    it; without that hand-over the manifest would show the table while the
    sections are written with the operator's width.
    """
    from fastapi.testclient import TestClient

    from inqtrix.providers.base import ProviderContext
    from inqtrix.search_result import GroundedSearchResult
    from inqtrix.server.app import create_app
    from inqtrix.settings import (
        AgentPlatformSettings,
        AgentSettings,
        ModelSettings,
        ServerSettings,
        Settings,
    )

    class _LLM:
        def is_available(self) -> bool:
            return True

    class _Search:
        def search(self, *args: object, **kwargs: object) -> GroundedSearchResult:
            return GroundedSearchResult()

    def published(platform: AgentPlatformSettings) -> set[object]:
        settings = Settings(
            models=ModelSettings(), agent=AgentSettings(), server=ServerSettings()
        )
        settings.agent_platform = platform
        app = create_app(
            settings=settings,
            providers=ProviderContext(llm=_LLM(), search=_Search()),
        )
        tiers = TestClient(app).get("/v1/capabilities").json()["agent"]["tiers"]
        return {entry["section_register"] for entry in tiers}

    table = {TIER_POLICIES[tier].section_register for tier in AGENT_TIERS}
    assert published(AgentPlatformSettings(INQTRIX_AGENT_ALLOW_VOLATILE=True)) == table
    assert published(
        AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_VOLATILE=True,
            INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET=11,
        )
    ) == {11}


def test_the_clarification_cap_is_the_tighter_of_operator_and_tier():
    """One min for the node, the snapshot and the manifest: the tier caps
    tighter, never wider, and a run without a tier keeps the operator cap."""
    gruendlich = TIER_POLICIES["gruendlich"]
    assert effective_clarification_rounds(2, gruendlich) == 1
    assert effective_clarification_rounds(0, gruendlich) == 0
    assert effective_clarification_rounds(2, TIER_POLICIES["tief"]) == 2
    assert effective_clarification_rounds(1, TIER_POLICIES["tief"]) == 1
    assert effective_clarification_rounds(3, None) == 3
    assert effective_clarification_rounds(None, TIER_POLICIES["schnell"]) == 0
    payload = {
        entry["id"]: entry["clarification_rounds"]
        for entry in tier_capabilities_payload(max_clarification_rounds=1)
    }
    assert payload == {"schnell": 0, "gruendlich": 1, "tief": 1}
    with pytest.raises(ValueError):
        effective_clarification_rounds(None, None)
