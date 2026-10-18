"""Server-owned depth and tier policy for shared multi-step web research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from inqtrix.agents.tier_policy import resolve_tier_policy

AgentDepth = Literal["normal", "deep"]
ResearchProfile = Literal["schnell", "compact", "deep"]


@dataclass(frozen=True)
class WebResearchPolicy:
    """Effective permission and fixed child profile for web research."""

    allowed: bool
    profile: ResearchProfile | None
    max_profile: ResearchProfile | None = None
    """Highest child profile a user may select per task at the plan
    gate; ``None`` pins tasks to exactly ``profile`` (legacy shape)."""
    max_instant_tasks: int | None = None
    """Tier cap on ``web_instant`` tasks per plan (``None`` = uncapped);
    forwarded verbatim to the plan validator so the published budget is
    the enforced one."""


def derive_web_research_policy(
    *,
    depth: AgentDepth,
    admitted_directive: bool = False,
    edited_plan: bool = False,
    tier: str | None = None,
) -> WebResearchPolicy:
    """Derive one agent web-research contract from admitted inputs.

    Args:
        depth: Centrally normalized agent depth.
        admitted_directive: Whether the request admission layer accepted the
            ``web_research`` tool directive. The flag remains compatible with
            callers that use an explicit route, but normal adaptive runs no
            longer require it.
        edited_plan: Whether the user explicitly selected research by editing
            the plan.
        tier: Selected Agent-Desk tier. Empty/``None`` reproduces the
            legacy depth-driven behavior byte-identically; a tier reads
            its budget from :data:`~inqtrix.agents.tier_policy
            .TIER_POLICIES` (default child profile + per-task ceiling).

    Returns:
        Permission plus the server-selected child profile. Normal adaptive
        runs use the compact Research-Desk profile. A speed tier may disable
        research and constrain the run to instant web.

    Raises:
        ValueError: If a caller bypasses the central depth normalization.
    """
    if depth not in {"normal", "deep"}:
        raise ValueError(f"agent depth is not normalized: {depth!r}")
    if tier:
        policy = resolve_tier_policy(tier)
        if not policy.web_research:
            # A tier that forbids research also subsumes an explicit
            # directive/edit consent: the tier is the user's own speed
            # choice, and the approved plan makes the reduction visible.
            return WebResearchPolicy(
                allowed=False,
                profile=None,
                max_instant_tasks=policy.web_instant_budget,
            )
        # Explicit consent (directive/edit) never LOWERS a tier budget;
        # the tier already grants research, so consent is subsumed.
        return WebResearchPolicy(
            allowed=True,
            profile=policy.web_child_profile,
            max_profile=policy.web_child_ceiling,
            max_instant_tasks=policy.web_instant_budget,
        )
    if depth == "deep":
        return WebResearchPolicy(allowed=True, profile="deep")
    # The Kernel and Missions-Maschine both orchestrate the existing Research
    # Desk rather than implementing their own search loop. In balanced/strict
    # autonomy the child dispatch still goes through the normal approval
    # policy; autonomous mode is itself the user's persisted consent.
    return WebResearchPolicy(allowed=True, profile="compact")
