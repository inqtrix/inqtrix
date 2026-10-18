"""Deterministic plan validation — no LLM involved.

Planner output and a user's edit decision run through the same
:func:`validate_plan`. Errors are collected (not fail-fast) so the planner
gets its full repair list in one round and the edit endpoint can show the
user everything at once. English messages identify invalid fields and the
required correction; the edit endpoint returns them with its 400 response.
"""

from __future__ import annotations

from collections import deque

from inqtrix.agents.control_ports import TASK_TOOL_KINDS
from inqtrix.agents.plan_models import (
    RAG_PROFILES,
    WEB_RESEARCH_PROFILE_ORDER,
    WEB_RESEARCH_PROFILES,
    ExecutionPlanModel,
)
from inqtrix.constants import MAX_PLAN_TASKS_DEFAULT, MAX_TASK_QUERIES

_PROFILE_DOMAINS = {
    "web_research": WEB_RESEARCH_PROFILES,
    "rag_query": RAG_PROFILES,
}

_QUERY_REQUIRED_KINDS = ("web_research", "web_instant", "rag_query")
"""Tool kinds whose plan tasks must carry concrete query strings.

These are the retrieval tasks whose approval hinges on the user seeing
the LITERAL questions before execution (plan transparency). Deliberately
excludes ``synthesis`` (queryless by design) and ``file_analysis``
(its executor falls back to objective/title, which is an acceptable
contract for a document-bound analysis)."""


def validate_plan(
    plan: ExecutionPlanModel,
    *,
    max_tasks: int = MAX_PLAN_TASKS_DEFAULT,
    known_gap_ids: set[str] | None = None,
    allowed_collection_ids: set[str] | None = None,
    web_research_allowed: bool = True,
    web_research_profile: str | None = None,
    web_research_profile_ceiling: str | None = None,
    max_web_instant_tasks: int | None = None,
) -> list[str]:
    """Collect every structural violation of *plan*.

    Args:
        plan: The parsed plan (Pydantic already enforced field shapes).
        max_tasks: Task-count ceiling for this run.
        known_gap_ids: The discovery gap universe; ``None`` skips the
            gap-reference check (a user edit has no gap context —
            the planner always passes it).
        allowed_collection_ids: Caller-visible collection ids; explicit
            ``params.collection_ids`` entries outside this set are
            violations. ``None`` skips the check (no knowledge service
            wired — the runtime visibility gate still guards retrieval); an
            EMPTY set means the caller sees no collections, so every
            explicit reference is invalid. Callers canonicalize name
            references first (:func:`inqtrix.agents.plan_collections.
            resolve_plan_collections`), so this check only ever sees
            ids the resolver could not map.
        web_research_allowed: Whether this request explicitly permits a
            multi-step research child. Normal Agent Desk plans set this to
            ``False`` and use ``web_instant`` instead.
        web_research_profile: Required server-selected child profile when
            research children are permitted and no ceiling applies — the
            legacy exact pin (``compact`` or ``deep``).
        web_research_profile_ceiling: Highest child profile a task may
            request (tier semantics). When set it REPLACES the exact
            pin: a task may pick any profile up to the ceiling
            (``schnell < compact < deep``), a missing profile falls back
            to the server default at execution.
        max_web_instant_tasks: Tier cap on ``web_instant`` tasks per
            plan (never prompt-only); ``None`` skips the check.

    Returns:
        All violations as user-facing messages; empty means valid.
    """
    errors: list[str] = []
    tasks = plan.tasks
    if len(tasks) > max_tasks:
        errors.append(
            f"Too many tasks ({len(tasks)}, allowed are {max_tasks})."
        )

    ids = [task.id for task in tasks]
    id_set = set(ids)
    if len(id_set) != len(ids):
        duplicates = sorted({tid for tid in ids if ids.count(tid) > 1})
        errors.append(f"Duplicate task ids: {', '.join(duplicates)}.")

    if max_web_instant_tasks is not None:
        instant_ids = [t.id for t in tasks if t.tool_kind == "web_instant"]
        if len(instant_ids) > max_web_instant_tasks:
            errors.append(
                "This tier allows at most "
                f"{max_web_instant_tasks} web_instant task(s); "
                f"found: {len(instant_ids)} "
                f"({', '.join(instant_ids)})."
            )

    synthesis_ids = [t.id for t in tasks if t.tool_kind == "synthesis"]
    if len(synthesis_ids) != 1:
        errors.append(
            "The plan needs exactly one synthesis task "
            f"(found: {len(synthesis_ids)})."
        )

    for task in tasks:
        if task.tool_kind not in TASK_TOOL_KINDS:
            errors.append(
                f"Task {task.id}: unknown tool "
                f"{task.tool_kind!r} (allowed: {', '.join(TASK_TOOL_KINDS)})."
            )
        if len(task.queries) > MAX_TASK_QUERIES:
            errors.append(
                f"Task {task.id}: too many questions "
                f"({len(task.queries)}, max. {MAX_TASK_QUERIES})."
            )
        if task.tool_kind == "web_instant" and len(task.queries) != 1:
            errors.append(
                f"Task {task.id}: web_instant needs exactly one "
                "self-contained question in queries."
            )
        if task.tool_kind in _QUERY_REQUIRED_KINDS and not any(
            query.strip() for query in task.queries
        ):
            errors.append(
                f"Task {task.id}: {task.tool_kind} needs at least "
                "one concrete question in queries."
            )
        if allowed_collection_ids is not None:
            for collection_id in task.params.collection_ids or []:
                if collection_id not in allowed_collection_ids:
                    errors.append(
                        f"Task {task.id}: collection {collection_id!r} is "
                        "not visible or unknown."
                    )
        for dep in task.depends_on:
            if dep not in id_set:
                errors.append(
                    f"Task {task.id}: depends_on refers to the unknown "
                    f"task id {dep!r}."
                )
            elif dep == task.id:
                errors.append(f"Task {task.id}: depends on itself.")
        profile = task.params.profile
        domain = _PROFILE_DOMAINS.get(task.tool_kind)
        if profile is not None:
            if domain is None:
                errors.append(
                    f"Task {task.id}: {task.tool_kind} does not accept "
                    "params.profile; set it to null."
                )
            elif profile not in domain:
                errors.append(
                    f"Task {task.id}: unknown profile {profile!r} "
                    f"(allowed: {', '.join(domain)})."
                )
        if task.tool_kind == "web_research":
            if not web_research_allowed:
                errors.append(
                    f"Task {task.id}: web_research is not allowed in this "
                    "tier or without an explicit research directive; use "
                    "web_instant."
                )
            elif web_research_profile_ceiling is not None:
                if profile is not None and (
                    WEB_RESEARCH_PROFILE_ORDER.get(profile, 99)
                    > WEB_RESEARCH_PROFILE_ORDER.get(
                        web_research_profile_ceiling, -1
                    )
                ):
                    errors.append(
                        f"Task {task.id}: profile={profile} exceeds the "
                        "search depth allowed in this tier (max. "
                        f"{web_research_profile_ceiling})."
                    )
            elif web_research_profile and profile != web_research_profile:
                errors.append(
                    f"Task {task.id}: web_research must use "
                    f"profile={web_research_profile} in this run."
                )
        if task.budget.model_dump(exclude_none=True):
            errors.append(
                f"Task {task.id}: budget is managed server-side and must "
                "not be set in new or edited plans."
            )
        if known_gap_ids is not None and task.tool_kind != "synthesis":
            for gap_id in task.gap_ids:
                if gap_id not in known_gap_ids:
                    errors.append(
                        f"Task {task.id}: unknown gap id {gap_id!r}."
                    )

    if len(synthesis_ids) == 1 and len(id_set) == len(ids):
        synthesis = next(t for t in tasks if t.id == synthesis_ids[0])
        missing = [
            t.id
            for t in tasks
            if t.id != synthesis.id and t.id not in set(synthesis.depends_on)
        ]
        if missing:
            errors.append(
                "The synthesis task must depend on every other task "
                f"(missing: {', '.join(missing)})."
            )

    cycle_members = _cycle_members(tasks)
    if cycle_members:
        errors.append(
            "Cyclic dependencies between tasks: "
            f"{', '.join(sorted(cycle_members))}."
        )

    return errors


def _cycle_members(tasks) -> set[str]:
    """Task ids stuck in a dependency cycle (Kahn's algorithm remainder).

    Unknown ``depends_on`` targets are ignored here — they are reported
    separately, and treating them as satisfied keeps the cycle check from
    double-reporting the same mistake.
    """
    id_set = {task.id for task in tasks}
    indegree = {task.id: 0 for task in tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep in id_set and dep != task.id:
                indegree[task.id] += 1
                dependents[dep].append(task.id)
    ready = deque(tid for tid, degree in indegree.items() if degree == 0)
    resolved = 0
    while ready:
        current = ready.popleft()
        resolved += 1
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if resolved == len(indegree):
        return set()
    return {tid for tid, degree in indegree.items() if degree > 0}
