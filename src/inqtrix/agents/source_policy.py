"""Shared source policy for both agent brains.

The Agent Desk exposes source availability independently from the model's
choice to use a tool.  Enforcement therefore belongs at the tool/task
dispatch choke points, not only in prompts.  This module is the single policy
definition consumed by the cognitive kernel, the workspace phase machine,
and child-run submission.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from inqtrix.core.results import SourcePolicy
from inqtrix.exceptions import AgentPolicyDenied

log = logging.getLogger("inqtrix")

WEB_TOOL_NAMES = frozenset(
    {
        "web_instant",
        "run_web_research",
        "delegate_batch",
    }
)
"""Kernel tool names that contact the public web.

``delegate_batch`` sits here for tool-use-counter parity: rehydration
counts one web use per ToolMessage NAME, so the live batch records
exactly ONE web use per batch (never per child). Side effect, accepted
and deliberate: ``source_policy.web=disabled`` removes the whole batch
tool — research assignments would be denied per-assignment anyway, and
a deep_mission-only batch degrades to the still-available single
``run_deep_mission`` tool."""

KNOWLEDGE_TOOL_NAMES = frozenset(
    {"search_project_knowledge", "read_project_document", "cite_document_passages"}
)
"""Kernel tool names that access project knowledge."""

UNCOUNTED_SOURCE_TOOLS = frozenset({"cite_document_passages"})
"""Source tools that reach no new source: a citation labels text the run
already read, so it spends no source budget (the live counter skips its
capability the same way)."""

WEB_TASK_KINDS = frozenset({"web_instant", "web_research"})
"""Workspace-agent task kinds that contact the public web."""

KNOWLEDGE_TASK_KINDS = frozenset({"rag_query", "file_analysis"})
"""Workspace-agent task kinds that access project knowledge."""

_ALL_TASK_KINDS = WEB_TASK_KINDS | KNOWLEDGE_TASK_KINDS | {"synthesis"}
def coerce_source_policy(value: object = None) -> SourcePolicy:
    """Normalize a policy or a deps-like owner, defaulting legacy omission.

    The both-available default is the documented backward-compatible wire
    behavior.  This helper also keeps focused task-executor test seams that
    predate the new deps attribute representative of an omitted policy.
    """
    candidate = getattr(value, "source_policy", value)
    if isinstance(candidate, SourcePolicy):
        return candidate
    if isinstance(candidate, dict):
        return SourcePolicy.model_validate(candidate)
    return SourcePolicy()


def effective_source_policy(policy: SourcePolicy) -> SourcePolicy:
    """The run's source policy as submitted (a defensive copy).

    Web is the session's switch, knowledge follows the attached collections;
    deployment and skill policy remain separate, higher-precedence gates.
    """
    return policy.model_copy()


def require_kernel_tool_allowed(
    tool_name: str,
    *,
    policy: SourcePolicy,
) -> None:
    """Fail loudly when a kernel tool violates the enforced run policy."""
    if kernel_tool_allowed(tool_name, policy=policy):
        return
    if tool_name in WEB_TOOL_NAMES:
        _deny("source_policy.web=disabled", tool_name)
    if tool_name in KNOWLEDGE_TOOL_NAMES:
        _deny("source_policy.knowledge=disabled", tool_name)
    _deny("effective_tool_policy", tool_name)


def kernel_tool_allowed(
    tool_name: str,
    *,
    policy: SourcePolicy,
) -> bool:
    """Whether a kernel tool belongs to the effective run tool surface."""
    if tool_name in WEB_TOOL_NAMES and policy.web != "available":
        return False
    if tool_name in KNOWLEDGE_TOOL_NAMES and policy.knowledge != "available":
        return False
    return True


def require_task_allowed(task_kind: str, *, policy: SourcePolicy) -> None:
    """Fail loudly when a workspace-agent task violates source policy."""
    if task_kind in WEB_TASK_KINDS and policy.web != "available":
        _deny("source_policy.web=disabled", task_kind)
    if task_kind in KNOWLEDGE_TASK_KINDS and policy.knowledge != "available":
        _deny("source_policy.knowledge=disabled", task_kind)


def allowed_task_kinds_for_policy(
    skill_kinds: set[str] | None,
    *,
    policy: SourcePolicy,
) -> set[str] | None:
    """Intersect the skill task allowlist with the effective source policy.

    ``None`` is retained only when neither source nor skills restrict the
    historical surface.  This keeps existing prompts byte-identical for old
    clients while ensuring a disabled source becomes a planner repair error,
    before any task is persisted.
    """
    if skill_kinds is None and (
        policy.web == "available" and policy.knowledge == "available"
    ):
        return None
    allowed = set(skill_kinds) if skill_kinds is not None else set(_ALL_TASK_KINDS)
    if policy.web != "available":
        allowed -= WEB_TASK_KINDS
    if policy.knowledge != "available":
        allowed -= KNOWLEDGE_TASK_KINDS
    allowed.add("synthesis")
    return allowed


def execution_payload(
    *,
    effective_mode: str,
    response_form: str,
    depth: str,
    model: str | None,
    reasoning_effort: str | None,
    source_policy: SourcePolicy,
    consent_reason: str,
    tool_use_counts: dict[str, int] | None = None,
    web_searches: int = 0,
    limits: dict[str, object] | None = None,
    tool_grants: "Iterable[str] | None" = None,
    knowledge_scope: str = "inherited",
    knowledge_collection_count: int = 0,
    source_coverage: "Iterable[Mapping[str, Any]] | None" = None,
    work_profile: str = "",
) -> dict[str, object]:
    """Canonical run/snapshot execution projection for Agent Desk.

    Every agent algorithm emits this exact key set, including zero counts and
    empty optional strings, so the transparency UI never has to infer whether
    a missing field means "off", "unknown", or an older code path.
    """
    counts = tool_use_counts or {}
    return {
        "effective_mode": effective_mode,
        "response_form": response_form,
        "depth": depth,
        "model": model or "",
        "reasoning_effort": reasoning_effort or "",
        "source_policy": {
            "web": source_policy.web,
            "knowledge": source_policy.knowledge,
        },
        # WHOSE knowledge boundary the run used: "all" (the session switch:
        # every visible collection), "pinned" (chips) or "inherited" (no
        # explicit choice; the visible boundary with the gate's consent).
        "knowledge_scope": knowledge_scope,
        "knowledge_collection_count": int(knowledge_collection_count),
        "consent_reason": consent_reason,
        "tool_use_counts": {
            "web": max(0, int(counts.get("web", 0) or 0)),
            "knowledge": max(0, int(counts.get("knowledge", 0) or 0)),
        },
        # The web searches the run's own provider calls executed. The
        # budget counter above takes a delegated research as one web use;
        # a research child reports its own searches (`executed_queries`).
        "web_searches": max(0, int(web_searches or 0)),
        # Server-authored limit facts only. The model never writes this
        # block; absent limits remain an explicit empty object for older or
        # non-agent callers instead of being guessed by the UI.
        "limits": dict(limits or {}),
        # Run-wide tool grants — explicit empty list for engines
        # without grants, never inferred by the UI.
        "tool_grants": sorted(tool_grants or ()),
        # What the run actually read of each source version:
        # an explicit empty list for algorithms without windowed readers.
        "source_coverage": [dict(entry) for entry in (source_coverage or [])],
        # Internal role of the run; empty for every ordinary run.
        "work_profile": work_profile,
    }


def _deny(reason: str, tool_or_task: str) -> None:
    log.warning(
        "Agent tool %s blocked by the effective source policy (%s).",
        tool_or_task,
        reason,
    )
    raise AgentPolicyDenied(
        f'Tool {tool_or_task} is denied by the source policy ({reason}).'
    )


def knowledge_scope_of(knowledge_filters: Mapping[str, Any]) -> str:
    """The scope label for the execution snapshot, from the persisted request."""
    from inqtrix.execution_authority import (
        KNOWLEDGE_SCOPE_ALL,
        KNOWLEDGE_SCOPE_INHERITED,
        KNOWLEDGE_SCOPE_PINNED,
    )

    if knowledge_filters.get("scope") == KNOWLEDGE_SCOPE_ALL:
        return KNOWLEDGE_SCOPE_ALL
    return (
        KNOWLEDGE_SCOPE_PINNED
        if knowledge_filters.get("explicit")
        else KNOWLEDGE_SCOPE_INHERITED
    )


def knowledge_boundary_payload(
    knowledge_filters: Mapping[str, Any],
) -> dict[str, object]:
    """The two boundary fields of :func:`execution_payload`, read from the
    persisted request — ONE projection for the kernel and the mission
    machine (``**knowledge_boundary_payload(request.knowledge_filters)``)."""
    return {
        "knowledge_scope": knowledge_scope_of(knowledge_filters),
        "knowledge_collection_count": len(
            knowledge_filters.get("collection_ids") or []
        ),
    }
