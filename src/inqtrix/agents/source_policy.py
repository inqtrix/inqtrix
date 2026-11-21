"""The source policy of agent runs.

The Agent Desk exposes source availability independently from the model's
choice to use a tool.  Enforcement therefore belongs at the tool dispatch
choke points, not only in prompts.  This module is the single policy
definition consumed by the cognitive kernel and child-run submission.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from inqtrix.core.results import SourcePolicy
from inqtrix.exceptions import AgentPolicyDenied

log = logging.getLogger(__name__)

WEB_TOOL_NAMES = frozenset(
    {
        "web_instant",
        "run_web_research",
    }
)
"""Kernel tool names that contact the public web."""

BATCH_TOOL_NAME = "delegate_batch"
"""The batch of subtasks. It reaches the web through its research
assignments and project knowledge through its knowledge assignments, so it
belongs to neither list: the tool is offered while either source is
available, and what one assignment may reach its own admission decides.
Which sources a batch counts as used it says in the artifact of its tool
message (``SOURCE_USES`` in ``agents.kernel.tools``); the counters read
them from there."""

KNOWLEDGE_TOOL_NAMES = frozenset(
    {
        "search_project_knowledge",
        "read_project_document",
        "cite_document_passages",
        "analyze_document",
    }
)
"""Kernel tool names that access project knowledge."""


def batch_assignment_mode(assignment: object) -> str:
    """The mode one assignment of a batch names, as the batch tool reads it.

    One reading for the tool, for the gate that decides whether to ask
    first and for the approval card: a gate that read the mode differently
    from the tool would let an assignment past the question it owes.
    """
    if not isinstance(assignment, Mapping):
        return ""
    return str(assignment.get("mode") or "").strip()


def batch_reaches_knowledge(args: object) -> bool:
    """Whether the arguments of a batch call carry a knowledge assignment."""
    assignments = args.get("assignments") if isinstance(args, Mapping) else None
    return isinstance(assignments, list) and any(
        batch_assignment_mode(assignment) == "knowledge" for assignment in assignments
    )

UNCOUNTED_SOURCE_TOOLS = frozenset({"cite_document_passages"})
"""Source tools that reach no new source: a citation labels text the run
already read, so it spends no source budget (the live counter skips its
capability the same way)."""


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
    if tool_name == BATCH_TOOL_NAME:
        _deny("source_policy.web=disabled and source_policy.knowledge=disabled", tool_name)
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
    if tool_name == BATCH_TOOL_NAME:
        return "available" in (policy.web, policy.knowledge)
    if tool_name in WEB_TOOL_NAMES and policy.web != "available":
        return False
    if tool_name in KNOWLEDGE_TOOL_NAMES and policy.knowledge != "available":
        return False
    return True


def require_web_research_allowed(*, policy: SourcePolicy) -> None:
    """Fail loudly when the source policy disables the web for a research run.

    The research algorithm checks here before its graph runs, so a research
    run whose source policy disables the web fails before any search.
    """
    if policy.web != "available":
        _deny("source_policy.web=disabled", "web_research")


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
    knowledge_searches: int | None = None,
    limits: dict[str, object] | None = None,
    tool_grants: "Iterable[str] | None" = None,
    knowledge_scope: str = "inherited",
    knowledge_collection_count: int = 0,
    source_coverage: "Iterable[Mapping[str, Any]] | None" = None,
    work_profile: str = "",
    document_analyses: "Iterable[Mapping[str, Any]] | None" = None,
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
        # counter above counts uses of a source and takes a delegated
        # research as one web use; a research child reports its own
        # searches (`executed_queries`).
        "web_searches": max(0, int(web_searches or 0)),
        # The searches of the knowledge base the run executed itself. The
        # counter above takes a read window as a use of the source like a
        # search, so it is no count of them. ``None`` when the caller passes
        # none (the stored payload of an older mission run reads so): not
        # recorded is never a zero.
        "knowledge_searches": (
            None if knowledge_searches is None else max(0, int(knowledge_searches))
        ),
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
        # What the run's document analyses read of each document, read on
        # the server and not handed to the model (reading facts only): an
        # explicit empty list for engines without the analysis tool.
        "document_analyses": [dict(entry) for entry in (document_analyses or [])],
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
    persisted request — ONE projection
    (``**knowledge_boundary_payload(request.knowledge_filters)``)."""
    return {
        "knowledge_scope": knowledge_scope_of(knowledge_filters),
        "knowledge_collection_count": len(
            knowledge_filters.get("collection_ids") or []
        ),
    }
