"""Per-request override surface for ``/v1/chat/completions``.

This module hosts the whitelist that lets HTTP callers (browser UI
controls, OpenAI-SDK clients with extra-body, integration tests)
override a small, deliberate set of behavioural fields per request.
The override travels in the JSON body under the optional
``"agent_overrides"`` key; OpenAI-SDK clients that do not know about
the field simply omit it, so the schema stays drop-in compatible.

How to add a new override field
===============================

The merge logic is generic: every new whitelist field flows through
:func:`apply_overrides` and on into ``agent_run`` without touching
``routes.py`` or the LangGraph code. Adding a new field is a
five-step recipe:

1. **Precondition** — the field must already exist on
   :class:`~inqtrix.settings.AgentSettings`. Practically every
   behavioural field there is fair game (``min_rounds``,
   ``high_risk_*``, ``search_cache_*``,
   ``claim_extract_timeout``, ``reasoning_timeout``, ``search_timeout``,
   ``answer_prompt_citations_max`` and friends).

   Forbidden without a fresh ADR: provider selection, model names,
   ``testing_mode``, anything on
   :class:`~inqtrix.settings.ServerSettings` (``max_concurrent``,
   ``api_key``, ``cors_origins``, ``tls_*``) and any
   strategy slot. These are deployment / operator concerns, not
   request concerns.

2. **AgentOverridesRequest** — add a single field with sane range
   validation::

       new_field: int | None = Field(default=None, ge=..., le=...)

3. ``apply_overrides`` — leave it alone. The implementation calls
   ``base.model_copy(update=overrides.model_dump(exclude_none=True))``
   followed by ``with_report_profile_defaults(...)`` and is generic.

4. ``routes.chat_completions`` — leave it alone. The route hands the
   whole Pydantic model through.

5. ``tests/test_server_overrides.py`` — add at least one test for the
   new field (range validation + an end-to-end test against a mocked
   ``agent_run``).

This versioned module is the canonical recipe so every maintainer can follow
the path without depending on local editor or agent memory.

Profile-switch semantics for scenarios A/B/C
====================================================

When a caller flips ``report_profile`` per request, the profile preset
fans out across other settings via
:meth:`AgentSettings.with_report_profile_defaults`. To keep both the
operator's explicit defaults *and* the caller's explicit overrides
authoritative, :func:`apply_overrides` builds the ``explicit_fields``
union from two sources:

* ``base.model_fields_set`` — fields the operator set explicitly
  (server-side ``AgentConfig`` / env).
* The keys present in the request override.

The profile defaults only fill in fields that **neither** side
specified, which preserves operator intent and request intent at
the same time.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inqtrix.report_profiles import ReportProfile
from inqtrix.model_routing import ModelTier
from inqtrix.settings import AgentSettings

log = logging.getLogger("inqtrix")


class AgentOverridesRequest(BaseModel):
    """Whitelist of behavioural fields a caller may override per request.

    Mirrors a deliberate subset of :class:`~inqtrix.settings.AgentSettings`.
    Unknown keys are rejected with a Pydantic ``ValidationError`` so the
    server can return a clean ``400 invalid_request_error`` instead of
    silently ignoring typos.

    Args:
        max_rounds: Hard upper bound for the research loop. ``1``-``4``.
            See :attr:`AgentConfig.max_rounds` for tuning guidance.
        min_rounds: Lower bound for the research loop. ``1``-``10`` and
            must not exceed ``max_rounds`` when both are present in the
            same request.
        confidence_stop: Minimum evaluator confidence (1-10) at which
            the stop cascade may emit ``done``.
        report_profile: ``"schnell"``, ``"compact"`` or ``"deep"``.
            Triggers the profile preset cascade for non-explicit fields,
            see module docstring for the merge semantics.
        max_total_seconds: Wall-clock deadline for the entire run, in
            seconds. ``30``-``3600``.
        first_round_queries: Number of broad queries generated in
            Round 0 by the plan node. ``1``-``8``.
    """

    model_config = ConfigDict(extra="forbid")

    max_rounds: int | None = Field(default=None, ge=1, le=4)
    """Hard upper bound for the research loop."""

    min_rounds: int | None = Field(default=None, ge=1, le=10)
    """Lower bound for the research loop."""

    confidence_stop: int | None = Field(default=None, ge=1, le=10)
    """Minimum evaluator confidence (1-10) before the stop cascade may emit done."""

    report_profile: ReportProfile | None = None
    """Report style preset (``schnell``, ``compact`` or ``deep``)."""

    depth: Literal["normal", "deep"] | None = None
    """Agent-mode thoroughness: ``deep`` = high kernel effort,
    raised iteration ceiling, DEEP child research profile and one
    verification pass. Distinct from ``report_profile``. Legacy knob —
    new clients send ``agent_tier`` instead; sending BOTH is a 400."""

    agent_tier: Literal["schnell", "gruendlich", "tief"] | None = None
    """Agent-Desk speed/depth tier (Stufen). Bridges into ``depth``
    (``tief`` -> ``deep``) so depth consumers keep working; the budget/
    gate semantics come from :data:`inqtrix.agents.tier_policy
    .TIER_POLICIES`. Mutually exclusive with ``depth`` — no silent
    precedence guessing."""

    max_total_seconds: int | None = Field(default=None, ge=30, le=3600)
    """Wall-clock deadline for the entire run, in seconds."""

    first_round_queries: int | None = Field(default=None, ge=1, le=8)
    """Number of broad queries generated in Round 0."""

    skip_search: bool | None = Field(default=None)
    """When ``True`` the request skips plan/search/evaluate and hits the LLM
    directly. See :attr:`AgentSettings.skip_search` for semantics."""

    model_tier: ModelTier | None = Field(default=None)
    """Per-run model tier for this request (``"high"``, ``"mid"``, or
    ``"fast"``). Routed to :attr:`AgentSettings.model_tier`; it replaces the
    mid/high default assignments; fast nodes keep their tier and effort.
    Primarily lets a caller pick
    the model class for a direct-chat answer (with ``skip_search=true``)."""

    model: str | None = Field(default=None, min_length=1, max_length=200)
    """Explicit model id for a direct-chat answer (the UI model picker choosing a
    concrete model instead of a tier). Routed to :attr:`AgentSettings.model`; it
    bypasses tier routing for the direct-chat call only. Pair with ``effort``."""

    effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = Field(default=None)
    """Reasoning effort for a direct-chat answer, paired with ``model``. Routed to
    :attr:`AgentSettings.effort`; accepted levels are model-dependent."""


def parse_overrides_payload(payload: Any) -> AgentOverridesRequest | None:
    """Validate the raw ``agent_overrides`` body slice into the model.

    Args:
        payload: The raw value of ``body["agent_overrides"]``. ``None``
            (the field was absent) returns ``None`` without raising.

    Returns:
        The validated :class:`AgentOverridesRequest`, or ``None`` when
        no overrides were supplied.

    Raises:
        HTTPException: 400 ``invalid_request_error`` when the payload is
            not a dict, contains unknown keys, or fails range validation.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "agent_overrides must be an object",
                    "type": "invalid_request_error",
                }
            },
        )
    try:
        overrides = AgentOverridesRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f'Invalid agent_overrides: {exc.errors()}',
                    "type": "invalid_request_error",
                }
            },
        ) from exc
    if overrides.agent_tier is not None and overrides.depth is not None:
        # Loud instead of a precedence rule: the two knobs answer the
        # same question ("how deep"), and a client mixing them
        # CONTRADICTORILY is a bug. The consistent pair stays valid —
        # the worker replays the resolved (tier, bridged depth) body.
        bridged = "deep" if overrides.agent_tier == "tief" else "normal"
        if overrides.depth != bridged:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": (
                            ("agent_overrides: depth conflicts with agent_tier; set only"
                            " one of them")
                        ),
                        "type": "invalid_request_error",
                    }
                },
            )
    return overrides


def apply_overrides(
    base: AgentSettings,
    overrides: AgentOverridesRequest | None,
) -> AgentSettings:
    """Merge per-request overrides into a base ``AgentSettings`` instance.

    Returns ``base`` unchanged when no overrides are present so that the
    common no-override path stays allocation-free.

    The merge is generic and treats every whitelist field uniformly.
    For the ``report_profile`` cascade see the module docstring.

    Args:
        base: The ``AgentSettings`` resolved from server-side
            ``AgentConfig`` / env for this server process.
        overrides: Validated request-side overrides. ``None`` returns
            ``base`` verbatim.

    Returns:
        A new :class:`AgentSettings` with the overrides applied and the
        ``report_profile``-driven defaults expanded for every field
        neither the operator nor the caller set explicitly.

    Side Effects:
        Emits an ``INFO`` log line for each applied override field so
        the audit trail tracks which slider moved which value.
    """
    update = (
        {} if overrides is None else overrides.model_dump(exclude_none=True)
    )
    # Tier -> depth bridge: a selected tier drives the SAME depth switch
    # every existing consumer (kernel ceilings, verify pass, child
    # profiles) already reads — additive, no consumer changes. The
    # mutual-exclusion check in parse_overrides_payload guarantees no
    # caller-supplied depth is overwritten here.
    if "agent_tier" in update and "depth" not in update:
        update["depth"] = "deep" if update["agent_tier"] == "tief" else "normal"
    if "agent_tier" not in update and base.agent_tier:
        # The bridge must also hold for an ENV-level AGENT_TIER: without
        # it the resolved pair is contradictory and the durable replay
        # body 400s at worker claim time. An explicit request depth that
        # contradicts the env tier wins (request beats env everywhere in
        # this module) — the run then keeps legacy depth semantics.
        bridged = "deep" if base.agent_tier == "tief" else "normal"
        if "depth" in update:
            if update["depth"] != bridged:
                log.info(
                    "Per-request depth=%r overrides env AGENT_TIER=%r; "
                    "the run uses legacy depth semantics.",
                    update["depth"],
                    base.agent_tier,
                )
                update["agent_tier"] = ""
        elif base.depth != bridged:
            log.info(
                "Env AGENT_TIER=%r bridges depth to %r (env DEPTH=%r "
                "is superseded by the tier).",
                base.agent_tier,
                bridged,
                base.depth,
            )
            update["depth"] = bridged
    if not update:
        return base
    for field_name, value in update.items():
        log.info("Per-request override: %s=%r", field_name, value)
    # Combine the two "explicit" sources so profile presets neither
    # overwrite operator-set fields nor caller-set fields. See module
    # docstring (scenarios A / B / C) for the rationale.
    explicit_fields = set(base.model_fields_set) | set(update.keys())
    patched = base.model_copy(update=update)
    return patched.with_report_profile_defaults(explicit_fields=explicit_fields)
