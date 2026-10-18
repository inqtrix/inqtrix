"""Central model-tier routing for every LLM call site in the algorithm.

One resolver, consumed identically by every call site (classify, plan,
evaluate, answer, claim_extract, direct_chat), so that model and reasoning
selection is uniform across the whole path -- no per-node special cases.

Three configuration layers, simple to fine-grained:

1. **One model for everything**: only ``reasoning_model`` is set; every node
   uses it (this is the historical default, unchanged).
2. **Three tiers**: ``tier_{high,mid,fast}_model`` (and optional
   ``tier_{high,mid,fast}_effort``). Each node is mapped to a tier by
   :data:`NODE_TIER_ASSIGNMENT`.
3. **Per-node model override**: ``<node>_model`` beats the tier for that one
   node. Reasoning effort is configured per tier, not per node.

A per-run ``requested_tier`` (e.g. from the chat endpoint's ``model_tier``
override) replaces mid/high default assignments. Nodes assigned to fast keep
that tier and its effort; an explicit per-node model override still wins.

The resolver returns the model id (a non-empty string) and a reasoning-effort
token. For effort, an empty string means "inherit the provider's constructor
default"; an explicit ``"none"`` means "force reasoning off for this call".
That sentinel difference is what keeps the change backward compatible: an
unconfigured node behaves exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from inqtrix.settings import ModelSettings


ModelTier = Literal["high", "mid", "fast"]
"""THE tier vocabulary. Request override fields that carry a tier are typed
with this alias; :data:`TIER_NAMES` is derived
from it, never re-spelled."""

TIER_NAMES: tuple[str, ...] = get_args(ModelTier)
"""The three tier identifiers, ordered strongest to fastest."""

NODE_TIER_ASSIGNMENT: dict[str, str] = {
    "classify": "fast",
    "claim_extract": "fast",
    "evaluate": "mid",
    "plan": "mid",
    "answer": "high",
    "direct_chat": "mid",
    "session_title": "fast",
    "knowledge_answer": "high",
    "knowledge_gate": "fast",
    "knowledge_contextualize": "fast",
    "knowledge_decompose": "fast",
    "knowledge_rerank": "fast",
    # Workspace-agent phases use inexpensive models for assembly-line
    # steps and stronger models only where the leverage is material.
    "agent_intake": "fast",
    "agent_discovery_analyst": "mid",
    "agent_plan": "high",
    "agent_contradiction": "mid",
    "agent_sufficiency": "fast",
    "agent_synthesis": "high",
    # The user-facing chat deliverable stays on the high tier; the LIGHT
    # variant is the deterministic auto-downgrade the
    # algorithm picks DETERMINISTICALLY when no web evidence is in play
    # (purely internal conversational answers) — auditable per run via
    # the model_resolution events.
    "agent_answer": "high",
    "agent_answer_light": "mid",
    "agent_file_analysis": "mid",
    "agent_patch": "mid",
    # The cognitive kernel has ONE node id for the tool loop: the kernel
    # is the brain and intelligence lives here. Tool/child calls
    # keep their own node tiers; the kernel never inherits downward.
    "agent_kernel": "high",
    # The review of both agent engines judges the assignment and the
    # substance of the evidence. It follows the selected model and effort;
    # the kernel's automatic Deep effort increase remains local to the
    # reasoning loop and revision.
    "agent_deep_review": "high",
    # Skill clarification-point checks use one inexpensive extraction
    # call per attached skill at intake.
    "agent_skill_point_check": "fast",
}
"""Maps each LLM call site to its default tier.

Hard-coded on purpose: per-node model overrides cover edge cases without adding
a configuration surface. Rationale per node lives in
``docs/architecture/websearch/nodes.md``. Keys must match the ``<node>_model``
field-name prefixes on :class:`~inqtrix.settings.ModelSettings`.
"""

_DEFAULT_TIER = "mid"
"""Fallback tier for an unknown node name (defensive; all real nodes are mapped)."""


def validate_model_tier(value: str) -> str:
    """Validate and canonicalise a configured ``model_tier`` at the config boundary.

    Unlike :func:`normalize_tier` (which silently discards an unknown value so a
    per-run resolution falls back to the default assignment), this is the strict
    config-time check: a non-empty value that is not a known tier raises, so a
    typo like ``MODEL_TIER=hgih`` fails loudly at construction instead of
    silently behaving as if no tier was selected.

    Args:
        value: The configured tier string. Empty (the default) is valid and
            means "use the per-node default assignment".

    Returns:
        The lower-cased, trimmed tier name, or ``""`` when unset.

    Raises:
        ValueError: When *value* is non-empty and not one of :data:`TIER_NAMES`.
    """
    canonical = (value or "").strip().lower()
    if canonical and canonical not in TIER_NAMES:
        raise ValueError(
            f"model_tier must be one of {TIER_NAMES} or empty; got {value!r}"
        )
    return canonical


def normalize_tier(requested_tier: str | None) -> str | None:
    """Return a valid lower-cased tier name, or ``None`` when not usable.

    Args:
        requested_tier: A caller-supplied tier name (e.g. from the
            ``model_tier`` request override). Empty, ``None``, or unknown
            values yield ``None`` so the default assignment is used instead.

    Returns:
        One of :data:`TIER_NAMES`, or ``None`` when *requested_tier* is empty
        or not a recognised tier.
    """
    if not requested_tier:
        return None
    tier = str(requested_tier).strip().lower()
    return tier if tier in TIER_NAMES else None


def resolve_tier(node: str, requested_tier: str | None = None) -> str:
    """Return the effective tier for *node*.

    Fast nodes keep their assigned tier. A valid *requested_tier* replaces
    mid/high defaults; otherwise the node's assignment applies.
    """
    default = NODE_TIER_ASSIGNMENT.get(node, _DEFAULT_TIER)
    if default == "fast":
        return default
    normalized = normalize_tier(requested_tier)
    if normalized is not None:
        return normalized
    return default


def describe_resolution(
    node: str,
    models: "ModelSettings",
    requested_tier: str | None = None,
    *,
    requested_model: str = "",
    requested_effort: str = "",
) -> dict[str, str]:
    """Resolve a call site's model and effort, and name where each came from.

    The single resolution function in this module: :func:`resolve_model` and
    :func:`resolve_effort` are thin wrappers over it, and every visibility
    surface reads its output -- the ``node_model_resolution`` run-event (React
    live view), the forensic log, and the ``/health`` / ``/v1/stacks`` discovery
    blocks. One place decides a node's effective model, and that decision is
    reported with provenance, so a default never grips silently and no
    surface invents a model the resolution did not choose.

    Model resolution order (first non-empty wins) and the resulting
    ``model_source``:

    1. Per-node override ``<node>_model`` -> ``"per_node_override"``.
    2. Tier model ``tier_<tier>_model`` -> ``"tier:<tier>"``.
    3. ``reasoning_model`` (the layer-1 default) -> ``"reasoning_model_default"``.

    Effort is configured per tier: a non-empty ``tier_<tier>_effort`` gives
    ``effort_source="tier:<tier>"``; otherwise ``effort`` is ``""`` (inherit the
    provider constructor default) with ``effort_source="provider_default"``.

    An explicit ``requested_model`` (the chat/editor model picker selecting a
    concrete model rather than a tier) short-circuits the tier lookup entirely,
    with ``model_source="explicit_request"``; a non-empty ``requested_effort``
    overrides the effort the same way. This is how a directly-selected model
    reaches the wire while staying visible in the forensic log.

    Args:
        node: Call-site name (e.g. ``"answer"``); see
            :data:`NODE_TIER_ASSIGNMENT`.
        models: The provider's :class:`~inqtrix.settings.ModelSettings`. Not
            read when ``requested_model`` is set (the explicit path returns
            first), so callers may pass ``None`` in that case.
        requested_tier: Optional per-run tier selection.
        requested_model: Optional explicit model id from the UI picker. When
            non-empty it wins over both tier and per-node resolution.
        requested_effort: Optional explicit reasoning effort (UI picker,
            skill pin, or Deep mode). A non-empty value wins on every
            path with ``effort_source="explicit_request"``.

    Returns:
        A mapping with string values for ``node``, ``model``, ``tier``,
        ``effort``, ``model_source``, ``effort_source`` and ``requested_tier``.
        ``model="" `` only when ``reasoning_model`` itself is empty; callers
        that treat that as "no model resolved" surface it as a loud warning.
    """
    explicit_model = (requested_model or "").strip()
    if explicit_model:
        explicit_effort = (requested_effort or "").strip()
        return {
            "node": node,
            "model": explicit_model,
            "tier": resolve_tier(node, requested_tier),
            "effort": explicit_effort,
            "model_source": "explicit_request",
            "effort_source": (
                "explicit_request" if explicit_effort else "provider_default"
            ),
            "requested_tier": requested_tier or "",
        }
    tier = resolve_tier(node, requested_tier)
    per_node = (getattr(models, f"{node}_model", "") or "").strip()
    tier_model = (getattr(models, f"tier_{tier}_model", "") or "").strip()
    if per_node:
        model, model_source = per_node, "per_node_override"
    elif tier_model:
        model, model_source = tier_model, f"tier:{tier}"
    else:
        model = (getattr(models, "reasoning_model", "") or "").strip()
        model_source = "reasoning_model_default"
    explicit_effort = (requested_effort or "").strip()
    tier_effort = (getattr(models, f"tier_{tier}_effort", "") or "").strip()
    if explicit_effort:
        # An explicit effort beats the tier effort on EVERY path, not
        # only next to an explicit model — skill effort pins and the Deep
        # mode request an effort while keeping tier routing.
        effort, effort_source = explicit_effort, "explicit_request"
    elif tier_effort:
        effort, effort_source = tier_effort, f"tier:{tier}"
    else:
        effort, effort_source = "", "provider_default"
    return {
        "node": node,
        "model": model,
        "tier": tier,
        "effort": effort,
        "model_source": model_source,
        "effort_source": effort_source,
        "requested_tier": requested_tier or "",
    }


def describe_unresolved_resolution(
    node: str,
    requested_tier: str | None = None,
    *,
    reason: str = "provider_models_missing",
) -> dict[str, str]:
    """Describe a node whose model cannot be resolved from provider metadata.

    This is the loud, structured counterpart to :func:`describe_resolution` for
    custom providers that do not expose ``.models``. It deliberately keeps the
    same field shape so discovery payloads, run-events, and iteration-log
    markers can render unresolved model routing without inventing a parallel
    schema or falling back to global settings that the provider may never use.

    Args:
        node: Call-site name (e.g. ``"answer"``).
        requested_tier: Optional per-run tier selection.
        reason: Stable source marker explaining why the model is unknown.

    Returns:
        A resolution descriptor with an empty ``model`` and provenance markers
        that make the provider-default fallback visible.
    """
    return {
        "node": node,
        "model": "",
        "tier": resolve_tier(node, requested_tier),
        "effort": "",
        "model_source": reason,
        "effort_source": "provider_default_unseen",
        "requested_tier": requested_tier or "",
    }


def describe_node_resolutions(
    models: "ModelSettings | None",
    requested_tier: str | None = None,
) -> dict[str, dict[str, str]]:
    """Describe model routing for every algorithm LLM call site.

    The helper does not make model decisions itself; it is only the shared
    collection point for visibility surfaces such as ``/health`` and
    ``/v1/stacks``. When provider metadata is missing it returns unresolved
    descriptors for every node instead of leaking global settings defaults.
    """
    if models is None:
        return {
            node: describe_unresolved_resolution(node, requested_tier)
            for node in NODE_TIER_ASSIGNMENT
        }
    return {
        node: describe_resolution(node, models, requested_tier)
        for node in NODE_TIER_ASSIGNMENT
    }


def describe_chat_model_options(
    models: "ModelSettings | None",
) -> list[dict[str, str]]:
    """Describe the direct-chat resolution for every selectable tier.

    The React chat composer needs the actual operator-configured model names
    before a request is sent. This helper deliberately reuses
    :func:`describe_resolution` for ``direct_chat`` instead of inventing a
    parallel selector contract: the UI sees the same model, tier, effort and
    provenance that a request with ``agent_overrides.model_tier`` would use.

    Args:
        models: The provider's model metadata, or ``None`` when a custom
            provider exposes no public model settings.

    Returns:
        One descriptor per tier in :data:`TIER_NAMES`. Missing provider
        metadata is represented with the same unresolved shape used by
        :func:`describe_node_resolutions`, so consumers can render an honest
        "unknown/provider default" state without falling back to global
        settings that might not apply.
    """
    return [
        (
            describe_unresolved_resolution("direct_chat", requested_tier=tier)
            if models is None
            else describe_resolution("direct_chat", models, requested_tier=tier)
        )
        for tier in TIER_NAMES
    ]


def resolve_model(
    node: str,
    models: "ModelSettings",
    requested_tier: str | None = None,
    *,
    requested_model: str = "",
    requested_effort: str = "",
) -> str:
    """Resolve the model id for a call site.

    Thin wrapper over :func:`describe_resolution`; see there for the resolution
    order (incl. the explicit ``requested_model`` short-circuit). Returns a
    model identifier string (never empty unless ``reasoning_model`` itself is
    empty, which has a non-empty default).
    """
    return describe_resolution(
        node,
        models,
        requested_tier,
        requested_model=requested_model,
        requested_effort=requested_effort,
    )["model"]


def resolve_effort(
    node: str,
    models: "ModelSettings",
    requested_tier: str | None = None,
    *,
    requested_model: str = "",
    requested_effort: str = "",
) -> str:
    """Resolve the reasoning-effort token for a call site.

    Thin wrapper over :func:`describe_resolution`. Returns an effort token
    (``""``, ``"none"``, ``"minimal"``, ``"low"``, ``"medium"``, ``"high"``,
    or ``"xhigh"``). ``""`` means "inherit provider default"; ``"none"`` means
    "force reasoning off". A non-empty ``requested_effort`` (UI picker,
    skill pin, or Deep mode) overrides the tier choice on every path.
    """
    return describe_resolution(
        node,
        models,
        requested_tier,
        requested_model=requested_model,
        requested_effort=requested_effort,
    )["effort"]
