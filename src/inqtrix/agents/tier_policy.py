"""THE agent speed/depth tier table (Stufen-System).

One server-owned policy record per user-facing tier — every consumer
(intake routing, clarify cap, planner prompt + validation,
``derive_web_research_policy``, kernel budgets, the review gate of BOTH
engines through ``resolve_verify_level``/``review_required``, the reach
of a memo section through ``resolve_section_register``,
``/v1/capabilities`` publishing) reads THIS table, so the published
contract always equals the enforced one (the ``permission_modes``
pattern). Budgets are stated in prompts AND enforced by validators —
never prompt-only.

Deliberately NOT part of a tier: model/effort selection (the composer
model picker and operator routing own that — asserted by test) and the
autonomy/permission dimension (``strict`` keeps its gates in every
tier; permission always beats speed).

Vocabulary: the tier names reuse the Knowledge-Desk depth family
(``schnell``/``gruendlich``/``tief``). ``standard`` is deliberately
skipped — the composer already shows an autonomy preset labeled
"Standard", and two same-named controls in one composer would collide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AgentTier = Literal["schnell", "gruendlich", "tief"]

AGENT_TIERS: tuple[AgentTier, ...] = ("schnell", "gruendlich", "tief")
"""Ordered ladder, fastest first."""

VerifyLevel = Literal["labels", "standard", "escalating"]

VERIFY_LEVELS: tuple[VerifyLevel, ...] = ("labels", "standard", "escalating")
"""Report-quality ladder, weakest first (see :attr:`TierPolicy.verify`)."""

DEFAULT_AGENT_TIER: AgentTier = "gruendlich"
"""The composer's default SELECTION. A request that omits ``agent_tier``
entirely runs LEGACY depth semantics (no tier budgets) — deliberate
backwards compatibility; tiers-aware clients always send a tier."""


@dataclass(frozen=True)
class TierPolicy:
    """Operational meaning of one tier (frozen — policy, not state)."""

    tier: AgentTier
    clarification_rounds: int
    """Cap on human clarification rounds; the effective cap is
    :func:`effective_clarification_rounds`. ``0`` = never ask, every
    open question degrades to a visibly named assumption."""
    discovery: bool
    """Whether the mission machine runs the discovery phase at all.
    ``False`` routes intake straight to planning (seconds matter more
    than probe context)."""
    plan_gate: Literal["per_autonomy", "skip_unless_strict"]
    """``per_autonomy`` keeps the plan gate of the autonomy mode;
    ``skip_unless_strict`` suppresses the plan interrupt except under
    ``strict`` autonomy (permission beats speed)."""
    web_research: bool
    """Whether the plan may contain multi-step ``web_research`` children
    at all. ``False`` = instant web only."""
    mission: bool
    """Whether the kernel may delegate a whole mission
    (``run_deep_mission``, ``delegate_batch`` mode ``deep_mission``).
    ``False`` keeps the tier in the middle of the ladder: research
    children only, no plan/waves/critic underneath one chat turn."""
    web_child_profile: Literal["schnell", "compact", "deep"] | None
    """Default report profile of ``web_research`` children."""
    web_child_ceiling: Literal["schnell", "compact", "deep"] | None
    """Highest child profile a user may select per task at the gate."""
    web_instant_budget: int | None
    """Cap on ``web_instant`` tasks per plan; ``None`` = uncapped. The
    plan validator enforces it (never prompt-only)."""
    knowledge_search_budget: int | None
    """Cap on ``search_project_knowledge`` calls per kernel run; ``None``
    = uncapped. Enforced at the kernel's tool chokepoint like the web
    budget — a step ceiling alone bounds turns, not the searches a
    tier promises, and the extra turns of a wider ceiling would be spent
    on repeated searches instead of the answer."""
    rag_default_profile: Literal["schnell", "standard", "gruendlich", "tief"]
    """Default retrieval profile for ``rag_query`` tasks."""
    verify: VerifyLevel
    """Report-quality level, read by BOTH agent engines through
    :func:`review_required`: ``labels`` = citation-label validation only
    and no review pass at all; ``standard`` = the shared review pass;
    ``escalating`` = the same pass, and unverified web-quoted claims
    become findings in their own right. The last part is the mission
    machine's today — the kernel gates on the level but does not yet add
    the measured quote findings, so its ``escalating`` equals its
    ``standard``."""
    section_register: int
    """How many references a memo section may reach for beside the ones its
    outline entry preselected — the one-line register a section may cite.
    A tier that goes deeper reaches further, so the width is a quality lever
    of the tier rather than one operator constant for every depth. Read by
    the mission engine, which is the only one that writes sections; it
    carries its tier into a mission a ``tief`` kernel run delegates."""
    response_form: Literal["auto", "chat", "canvas"]
    """Server-side deliverable default; ``chat``/``canvas`` override the
    intake routing, ``auto`` keeps it."""


TIER_POLICIES: dict[AgentTier, TierPolicy] = {
    "schnell": TierPolicy(
        tier="schnell",
        clarification_rounds=0,
        discovery=False,
        plan_gate="skip_unless_strict",
        web_research=False,
        mission=False,
        web_child_profile=None,
        web_child_ceiling=None,
        web_instant_budget=1,
        knowledge_search_budget=1,
        rag_default_profile="schnell",
        verify="labels",
        section_register=20,
        response_form="chat",
    ),
    "gruendlich": TierPolicy(
        tier="gruendlich",
        clarification_rounds=1,
        discovery=True,
        plan_gate="per_autonomy",
        web_research=True,
        mission=False,
        web_child_profile="schnell",
        web_child_ceiling="compact",
        web_instant_budget=None,
        knowledge_search_budget=None,
        rag_default_profile="standard",
        verify="standard",
        section_register=60,
        response_form="auto",
    ),
    "tief": TierPolicy(
        tier="tief",
        clarification_rounds=2,
        discovery=True,
        plan_gate="per_autonomy",
        web_research=True,
        mission=True,
        web_child_profile="compact",
        web_child_ceiling="deep",
        web_instant_budget=None,
        knowledge_search_budget=None,
        rag_default_profile="gruendlich",
        verify="escalating",
        section_register=180,
        response_form="canvas",
    ),
}


def resolve_tier_policy(tier: str | None) -> TierPolicy:
    """The policy of ``tier``; empty/None resolves to the default tier.

    Raises:
        ValueError: For a token outside the published ladder — admission
            validates first, so reaching this is a caller bug that must
            fail loudly instead of silently running the default budgets.
    """
    if not tier:
        return TIER_POLICIES[DEFAULT_AGENT_TIER]
    if tier not in TIER_POLICIES:
        raise ValueError(f"unknown agent tier: {tier!r}")
    return TIER_POLICIES[tier]  # type: ignore[index]


def blocked_kernel_tools(policy: TierPolicy) -> frozenset[str]:
    """Kernel tools ``policy`` removes at the dispatch chokepoint.

    Derived from the table, never listed a second time: no clarification
    rounds → no ``ask_user``; no research children → no
    ``run_web_research``; no mission → no ``run_deep_mission`` (a batch
    assignment in mode ``deep_mission`` runs through the same check);
    ``delegate_batch`` only goes when neither child kind may run.
    ``write_canvas`` is never removed — a document on request is
    possible in every Stufe.
    """
    blocked: set[str] = set()
    if policy.clarification_rounds == 0:
        blocked.add("ask_user")
    if not policy.web_research:
        blocked.add("run_web_research")
    if not policy.mission:
        blocked.add("run_deep_mission")
    if not policy.web_research and not policy.mission:
        blocked.add("delegate_batch")
    return frozenset(blocked)


def resolve_verify_level(
    tier: str | None, *, legacy: VerifyLevel
) -> VerifyLevel:
    """THE report-quality level of one run, tier first, legacy second.

    Same shape as :func:`~inqtrix.agents.web_execution_policy.
    derive_web_research_policy` for the neighbouring field: a run that
    names a tier reads the published table, and a run that names none
    keeps the caller's legacy semantics byte-identically (see the
    module docstring on :data:`DEFAULT_AGENT_TIER` — tiers-aware
    clients always send one).

    Args:
        tier: The run's tier, empty or ``None`` for a legacy request.
        legacy: The level a request without a tier runs at, derived by
            the caller from whatever it used before tiers existed.

    Returns:
        The level both engines gate their review pass on.
    """
    if not tier:
        return legacy
    return resolve_tier_policy(tier).verify


def review_required(level: str) -> bool:
    """Whether a run at this report-quality level owes a review pass.

    The one place that spells out what ``labels`` means, so the gate of
    the mission machine and the gate of the kernel cannot drift apart —
    and so the published ``verify`` level stays the enforced one.
    """
    return level != "labels"


LEGACY_SECTION_REGISTER = 60
"""Register width of a run that names no tier: the width the register had
before it followed the tier, kept exactly for requests that predate the
ladder."""


def resolve_section_register(tier: str | None) -> int:
    """THE register width of one run's memo sections, tier first.

    Same shape as :func:`resolve_verify_level`: a run that names a tier
    reads the published table, a run that names none keeps the width it
    had before the ladder existed. An operator override is applied by the
    caller on top of this value, and published by
    :func:`tier_capabilities_payload` in the same breath.
    """
    if not tier:
        return LEGACY_SECTION_REGISTER
    return resolve_tier_policy(tier).section_register


def effective_clarification_rounds(
    platform_cap: int | None, policy: TierPolicy | None
) -> int:
    """THE clarification-round cap one run enforces and publishes.

    The tier caps rounds tighter, never wider (permission-style min):
    ``schnell`` asks nothing, ``gruendlich`` at most once. The one place
    the two caps meet, so the clarify node, the run snapshot and
    ``/v1/capabilities`` cannot state different numbers.

    Args:
        platform_cap: The operator cap (``max_clarification_rounds``);
            ``None`` where only the table is published.
        policy: The run's tier; ``None`` for a run that names no tier.

    Raises:
        ValueError: When neither cap is given — a cap cannot be invented.
    """
    caps = [
        cap
        for cap in (
            platform_cap,
            policy.clarification_rounds if policy is not None else None,
        )
        if cap is not None
    ]
    if not caps:
        raise ValueError("a clarification cap needs a platform cap or a tier")
    return min(caps)


def tier_capabilities_payload(
    *,
    max_clarification_rounds: int | None = None,
    section_register_override: int | None = None,
) -> list[dict[str, object]]:
    """Wire projection for ``/v1/capabilities`` — generated from
    :data:`TIER_POLICIES` so published == enforced.

    ``max_clarification_rounds`` is the operator cap the clarify node
    also applies (:func:`effective_clarification_rounds`): passing it
    keeps the published round budget equal to the enforced one under a
    tighter env configuration. ``section_register_override`` does the
    same for the register width: an operator number replaces every
    tier's value where the sections are written, so it replaces it here
    too.
    """
    return [
        {
            "id": policy.tier,
            "clarification_rounds": effective_clarification_rounds(
                max_clarification_rounds, policy
            ),
            "plan_gate": policy.plan_gate,
            "web_research": policy.web_research,
            "mission": policy.mission,
            "web_child_profile": policy.web_child_profile,
            "web_child_ceiling": policy.web_child_ceiling,
            "web_instant_budget": policy.web_instant_budget,
            "knowledge_search_budget": policy.knowledge_search_budget,
            "rag_default_profile": policy.rag_default_profile,
            "verify": policy.verify,
            "section_register": (
                section_register_override
                if section_register_override is not None
                else policy.section_register
            ),
            "response_form": policy.response_form,
        }
        for policy in (TIER_POLICIES[tier] for tier in AGENT_TIERS)
    ]
