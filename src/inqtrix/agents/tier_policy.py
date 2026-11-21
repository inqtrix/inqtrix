"""THE agent speed/depth tier table (Stufen-System).

One server-owned policy record per user-facing tier — every consumer
(``derive_web_research_policy``, the kernel's budgets and blocked tools, its
review gate through ``resolve_verify_level``/``review_required``,
``/v1/capabilities`` publishing) reads THIS table, so the published contract
is the enforced one (the ``permission_modes`` pattern), with three named
gaps: ``clarification_rounds`` is published from the table, and the kernel
enforces only its zero (no ``ask_user`` at all); it does not count the rounds
of a tier that allows some. A tier's ``response_form`` reaches the kernel only
as a sentence of its first message, and the form a run delivered is read from
its result. ``escalating`` verification runs the same review pass as
``standard``. Budgets are stated in prompts AND enforced at the kernel's tool
chokepoint — never prompt-only.

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
    """Cap on human clarification rounds, as published. ``0`` = never ask:
    the kernel has no ``ask_user`` and every open question degrades to a
    visibly named assumption. A larger number is not counted by the kernel
    (see the module docstring)."""
    web_research: bool
    """Whether a run may start multi-step ``web_research`` children at all.
    ``False`` = instant web only."""
    document_analysis: bool
    """Whether the kernel may have whole project documents analysed on the
    server (``analyze_document``: a model call per window). ``False`` leaves
    reading documents window by window."""
    web_child_profile: Literal["schnell", "compact", "deep"] | None
    """Report profile of ``web_research`` children."""
    web_instant_budget: int | None
    """Cap on instant web searches per kernel run; ``None`` = uncapped.
    Enforced at the kernel's tool chokepoint (never prompt-only)."""
    knowledge_search_budget: int | None
    """Cap on ``search_project_knowledge`` calls per kernel run; ``None``
    = uncapped. Enforced at the kernel's tool chokepoint like the web
    budget — a step ceiling alone bounds turns, not the searches a
    tier promises, and the extra turns of a wider ceiling would be spent
    on repeated searches instead of the answer."""
    rag_default_profile: Literal["schnell", "standard", "gruendlich", "tief"]
    """Knowledge profile of the tier: what a knowledge assignment of
    ``delegate_batch`` hands its child as ``knowledge_filters.profile``."""
    verify: VerifyLevel
    """Report-quality level, read by the kernel through
    :func:`review_required`: ``labels`` = citation-label validation only
    and no review pass at all; ``standard`` = the review pass;
    ``escalating`` = the same pass. The kernel gates on the level but does
    not add measured quote findings of its own, so its ``escalating``
    equals its ``standard``."""
    response_form: Literal["auto", "chat", "canvas"]
    """The tier's deliverable default, published under ``agent.tiers``. For a
    run whose user chose no form, ``canvas`` makes the kernel's first message
    ask for a canvas document; the ``schnell`` section of that message asks
    for the chat answer in its own words; ``auto`` adds no form rule. The
    kernel is prompt-driven: the form a run delivered is read from its
    result."""


TIER_POLICIES: dict[AgentTier, TierPolicy] = {
    "schnell": TierPolicy(
        tier="schnell",
        clarification_rounds=0,
        web_research=False,
        document_analysis=False,
        web_child_profile=None,
        web_instant_budget=1,
        knowledge_search_budget=1,
        rag_default_profile="schnell",
        verify="labels",
        response_form="chat",
    ),
    "gruendlich": TierPolicy(
        tier="gruendlich",
        clarification_rounds=1,
        web_research=True,
        document_analysis=True,
        web_child_profile="schnell",
        web_instant_budget=None,
        knowledge_search_budget=None,
        rag_default_profile="standard",
        verify="standard",
        response_form="auto",
    ),
    "tief": TierPolicy(
        tier="tief",
        clarification_rounds=2,
        web_research=True,
        document_analysis=True,
        web_child_profile="compact",
        web_instant_budget=None,
        knowledge_search_budget=None,
        rag_default_profile="gruendlich",
        verify="escalating",
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
    ``run_web_research`` and no ``delegate_batch``: such a tier starts no
    child at all, a knowledge assignment included; no document analysis →
    no ``analyze_document``. ``write_canvas`` is never removed — a document
    on request is possible in every Stufe.
    """
    blocked: set[str] = set()
    if policy.clarification_rounds == 0:
        blocked.add("ask_user")
    if not policy.web_research:
        blocked.add("run_web_research")
        blocked.add("delegate_batch")
    if not policy.document_analysis:
        blocked.add("analyze_document")
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
        The level the kernel gates its review pass on.
    """
    if not tier:
        return legacy
    return resolve_tier_policy(tier).verify


def review_required(level: str) -> bool:
    """Whether a run at this report-quality level owes a review pass.

    The one place that spells out what ``labels`` means, so the published
    ``verify`` level stays the enforced one.
    """
    return level != "labels"


def tier_capabilities_payload() -> list[dict[str, object]]:
    """Wire projection for ``/v1/capabilities`` — generated from
    :data:`TIER_POLICIES`, so the published values are the table's."""
    return [
        {
            "id": policy.tier,
            "clarification_rounds": policy.clarification_rounds,
            "web_research": policy.web_research,
            "document_analysis": policy.document_analysis,
            "web_child_profile": policy.web_child_profile,
            "web_instant_budget": policy.web_instant_budget,
            "knowledge_search_budget": policy.knowledge_search_budget,
            "rag_default_profile": policy.rag_default_profile,
            "verify": policy.verify,
            "response_form": policy.response_form,
        }
        for policy in (TIER_POLICIES[tier] for tier in AGENT_TIERS)
    ]
