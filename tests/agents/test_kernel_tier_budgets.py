"""Tier source budgets at the kernel tool chokepoint (published == enforced)."""

from __future__ import annotations

import pytest

from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.exceptions import AgentPolicyDenied
from inqtrix.settings import AgentPlatformSettings


def _deps(tier: str, **counts: int) -> KernelDeps:
    deps = KernelDeps(
        run_id="run_tier_budget",
        control=None,  # type: ignore[arg-type]
        platform=AgentPlatformSettings(),
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    deps.tier = tier
    deps.tool_use_counts.update(counts)
    return deps


def test_schnell_denies_the_second_knowledge_search_with_the_published_budget():
    """A live schnell follow-up spent seven knowledge searches and parked at
    the step ceiling: the ceiling bounds turns, not the searches the tier
    promises. The tier table's knowledge budget is enforced here, like the
    web budget, and the model is told to answer from what it has."""
    _deps("schnell").require_tool_allowed("search_project_knowledge")
    with pytest.raises(AgentPolicyDenied) as denied:
        _deps("schnell", knowledge=1).require_tool_allowed("search_project_knowledge")
    assert "at most 1 project-knowledge search" in str(denied.value)
    assert "answer from the results you already have" in str(denied.value)


def test_web_budget_reads_the_same_table():
    _deps("schnell").require_tool_allowed("web_instant")
    with pytest.raises(AgentPolicyDenied):
        _deps("schnell", web=1).require_tool_allowed("web_instant")


def test_uncapped_tiers_and_tier_less_runs_keep_searching():
    for tier in ("gruendlich", "tief", ""):
        _deps(tier, knowledge=5, web=5).require_tool_allowed("search_project_knowledge")
        _deps(tier, knowledge=5, web=5).require_tool_allowed("web_instant")
