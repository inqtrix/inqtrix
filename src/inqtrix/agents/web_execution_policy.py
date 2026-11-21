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


def derive_web_research_policy(
    *,
    depth: AgentDepth,
    tier: str | None = None,
) -> WebResearchPolicy:
    """Derive one agent web-research contract from admitted inputs.

    Args:
        depth: Centrally normalized agent depth.
        tier: Selected Agent-Desk tier. Empty/``None`` reproduces the
            legacy depth-driven behavior byte-identically; a tier reads
            its child profile from :data:`~inqtrix.agents.tier_policy
            .TIER_POLICIES`.

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
            # The tier is the user's own speed choice.
            return WebResearchPolicy(allowed=False, profile=None)
        return WebResearchPolicy(allowed=True, profile=policy.web_child_profile)
    if depth == "deep":
        return WebResearchPolicy(allowed=True, profile="deep")
    # The kernel orchestrates the existing Research Desk rather than
    # implementing its own search loop. In balanced/strict autonomy the
    # child dispatch still goes through the normal approval policy;
    # autonomous mode is itself the user's persisted consent.
    return WebResearchPolicy(allowed=True, profile="compact")
