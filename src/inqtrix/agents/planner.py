"""Structured execution planning with one bounded repair attempt.

The planner produces THE plan shape (:mod:`inqtrix.agents.plan_models`)
and is validated by THE validator (:mod:`inqtrix.agents.plan_validation`)
— the same pair the user-edit endpoint runs, so agent plans and user
edits can never drift apart. One repair retry with the full error list;
a second invalid plan fails the run loudly (``plan_invalid``).
"""

from __future__ import annotations

import logging

import uuid
from typing import TYPE_CHECKING, Callable

from inqtrix.i18n import t
from inqtrix.agents.control_ports import (
    PlanRecord,
    PlanTaskRecord,
    additive_replan_errors,
    carry_forward_terminal_task_results,
)
from inqtrix.agents.patterns._structured import structured_call
from inqtrix.agents.phase_models import DiscoveryResult
from inqtrix.agents.plan_collections import (
    CollectionCatalogEntry,
    resolve_plan_collections,
)
from inqtrix.agents.plan_models import (
    ExecutionPlanModel,
    PlanTaskModel,
    ReplanDeltaModel,
)
from inqtrix.agents.plan_validation import validate_plan
from inqtrix.agents.web_execution_policy import derive_web_research_policy
from inqtrix.agents.prompts import (
    agent_planner_system_prompt,
    build_agent_planner_prompt,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inqtrix.agents.prompts import DocumentAnalysisLimits

    from inqtrix.providers.base import LLMProvider

log = logging.getLogger(__name__)

COVERED_GAP_NOTE = "covered by a finished task"
"""How the discovery state marks a gap a finished task covered."""


class PlanningFailed(RuntimeError):
    """Planner produced no valid plan after the repair round (loud).

    Attributes:
        errors: The final validation error list in English.
        usage: Token usage of the failed planner call(s) — the caller
            books it, a failed attempt is still spend.
    """

    def __init__(self, errors: list[str], *, usage: dict[str, int]) -> None:
        super().__init__("; ".join(errors) or "plan_invalid")
        self.errors = errors
        self.usage = dict(usage)


def replan_headroom(
    previous_tasks: "Sequence[PlanTaskRecord]", *, max_tasks: int
) -> int:
    """Task slots a replan of *previous_tasks* may still fill.

    The same count validate_plan will apply: every kept source task (failed
    ones are immutable, so they occupy a slot) plus the one synthesis task
    the merge appends. The planner prompt and the mission's research
    prospect read this one number.
    """
    occupied = sum(1 for task in previous_tasks if task.tool_kind != "synthesis")
    headroom = max_tasks - 1 - occupied
    if headroom < 0:
        log.warning(
            "Replan headroom is negative (%d): %d source tasks with a limit of %d.",
            headroom,
            occupied,
            max_tasks,
        )
        return 0
    return headroom


def run_planner(
    llm: "LLMProvider",
    *,
    question: str,
    discovery: DiscoveryResult | None,
    criteria: "Sequence[str]",
    covered_gap_ids: frozenset[str],
    max_tasks: int,
    web_allowed: bool,
    web_needed: bool = True,
    sources_answered: bool = False,
    knowledge_allowed: bool,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    replan_context: str = "",
    memory_briefing: str = "",
    collection_catalog: "Sequence[CollectionCatalogEntry] | None" = None,
    skills_block: str = "",
    allowed_task_kinds: "set[str] | None" = None,
    depth: str = "normal",
    explicit_web_research: bool = False,
    previous_plan: "PlanRecord | None" = None,
    previous_tasks: "Sequence[PlanTaskRecord] | None" = None,
    history: str = "",
    tier: str | None = None,
    language: str | None = None,
    document_analysis_limits: "DocumentAnalysisLimits | None" = None,
    run_assumptions: "Sequence[dict[str, str]]" = (),
    drop_new_task: Callable[[PlanTaskRecord], bool] | None = None,
) -> tuple[ExecutionPlanModel, dict[str, int]]:
    """Plan, validate, repair once; raises :class:`PlanningFailed`.

    ``criteria`` are the run's success criteria, numbered as the review
    numbers them; ``covered_gap_ids`` the discovery gaps finished tasks
    covered, marked as such in the discovery state.

    ``drop_new_task`` is asked, on a clean replan attempt only, about each
    NEW ``file_analysis`` task (as the record the plan would save) and
    returns True to drop it. The plan without the dropped tasks is checked
    again; when that check fails, :class:`PlanningFailed` is raised at once:
    the model's plan was valid and the server's drop broke it, so a repair
    round could not help.

    ``run_assumptions`` are the questions the run answered with an
    assumption instead of asking, shown to the planner as a fenced block
    of model text (the plan node carries them into the first version's
    assumptions itself).

    ``web_allowed`` is the source policy; ``web_needed`` is the
    assignment's own need (:func:`~inqtrix.agents.intake.
    assignment_needs_web`, decided by the caller once per round). A new
    web task needs both; one the assignment does not need is a repair
    error, while a web task carried over from an earlier version (the
    user's own, or planned before) stays, since a replan cannot remove a
    task that already ran. ``sources_answered``: the user answered a
    question about the sources; the prompt says that this answer decides
    whether web tasks are planned.

    ``collection_catalog`` is the caller-visible knowledge catalog: it is
    shown to the planner (name AND canonical id), name references in the
    produced plan are canonicalized to ids, and unresolvable references
    join the repair errors — a saved plan can then never fail retrieval
    with a raw unknown-collection error. ``None`` (no knowledge service)
    skips catalog handling; the runtime visibility gate still guards retrieval.

    ``previous_plan`` activates the delta-only replan contract. The model can
    then emit only new work (and optional pending-task skips); the server
    reconstructs the complete plan with immutable terminal results and one
    synthesis task before running the normal validator. Returns ``(plan,
    usage)`` with the accumulated token usage of the one or two calls.
    """
    research_policy = derive_web_research_policy(
        depth=depth,
        admitted_directive=explicit_web_research,
        tier=tier,
    )
    known_gap_ids = (
        {gap.gap_id for gap in discovery.gaps} if discovery else None
    )
    digest = summarize_discovery(discovery, covered=covered_gap_ids)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    errors: list[str] = []
    is_replan = previous_plan is not None
    if is_replan and previous_tasks is None:
        raise ValueError("previous_tasks are required for a replan")
    headroom: int | None = None
    if is_replan:
        headroom = replan_headroom(previous_tasks or (), max_tasks=max_tasks)

    def attempt_errors(plan: ExecutionPlanModel) -> list[str]:
        """Every check one attempt's plan must pass before it is returned."""
        # Canonicalize BEFORE validation so the allowed-ids check only
        # ever sees references the resolver could not map (one combined
        # repair list, no double-reporting).
        errors = (
            resolve_plan_collections(plan, collection_catalog)
            if collection_catalog is not None
            else []
        )
        errors += validate_plan(
            plan,
            max_tasks=max_tasks,
            known_gap_ids=known_gap_ids,
            allowed_collection_ids=(
                {entry.collection_id for entry in collection_catalog}
                if collection_catalog is not None
                else None
            ),
            web_research_allowed=research_policy.allowed,
            # A tier publishes a CEILING (per-task choice up to it); the
            # legacy path keeps the exact server pin.
            web_research_profile=(
                None
                if research_policy.max_profile is not None
                else research_policy.profile
            ),
            web_research_profile_ceiling=research_policy.max_profile,
            max_web_instant_tasks=research_policy.max_instant_tasks,
        )
        if not (web_allowed and web_needed):
            carried = (
                set()
                if not web_allowed
                else {task.task_id for task in previous_tasks or ()}
            )
            web_tasks = [
                task.id
                for task in plan.tasks
                if task.tool_kind in ("web_research", "web_instant")
                and task.id not in carried
            ]
            if web_tasks:
                errors.append(
                    (
                        "Web research is not allowed; remove the tasks "
                        if not web_allowed
                        else "The assignment does not need the web; name "
                        "what the documents leave open as an open point "
                        "and remove the tasks "
                    )
                    + f"{', '.join(web_tasks)}."
                )
        if allowed_task_kinds is not None:
            # Activated skill allowlists restrict
            # the tool families; a task outside the union is a repair
            # error, so a forbidden task never even gets persisted.
            blocked = [
                f"{task.id} ({task.tool_kind})"
                for task in plan.tasks
                if task.tool_kind not in allowed_task_kinds
            ]
            if blocked:
                allowed = ", ".join(sorted(allowed_task_kinds))
                errors.append(
                    "The activated skills allow only these tools: "
                    f"{allowed}. Remove or replace: "
                    f"{', '.join(blocked)}."
                )
        if previous_tasks:
            _candidate_record, candidate_tasks = plan_to_records(
                plan,
                run_id="replan-validation",
                created_by="agent",
            )
            errors += additive_replan_errors(
                list(previous_tasks), candidate_tasks
            )
        return errors

    for attempt in range(2):
        outcome = structured_call(
            llm,
            prompt=build_agent_planner_prompt(
                question,
                digest,
                criteria,
                max_tasks=max_tasks,
                web_allowed=web_allowed,
                web_needed=web_needed,
                sources_answered=sources_answered,
                knowledge_allowed=knowledge_allowed,
                replan_context=replan_context,
                memory_briefing=memory_briefing,
                repair_errors=errors or None,
                collection_catalog=collection_catalog,
                skills_block=skills_block,
                research_allowed=research_policy.allowed,
                research_profile=research_policy.profile,
                research_profile_ceiling=research_policy.max_profile,
                max_web_instant_tasks=research_policy.max_instant_tasks,
                replan_mode=is_replan,
                replan_headroom=headroom,
                history=history,
                language=language,
                document_analysis_limits=document_analysis_limits,
                run_assumptions=run_assumptions,
                covered_gaps=len(covered_gap_ids & known_gap_ids) if known_gap_ids else 0,
            ),
            model_cls=ReplanDeltaModel if is_replan else ExecutionPlanModel,
            node="agent_plan",
            system=agent_planner_system_prompt(),
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
        )
        usage["prompt_tokens"] += outcome.usage.get("prompt_tokens", 0)
        usage["completion_tokens"] += outcome.usage.get(
            "completion_tokens", 0
        )
        value = outcome.value
        if value is None:
            errors = ["The response was not valid plan JSON."]
            continue
        if is_replan:
            assert isinstance(value, ReplanDeltaModel)
            plan, errors = merge_replan_delta(
                value,
                language=language,
                previous_plan=previous_plan,
                previous_tasks=list(previous_tasks or ()),
            )
            if plan is None:
                continue
        else:
            assert isinstance(value, ExecutionPlanModel)
            plan = value
            errors = []
        errors += attempt_errors(plan)
        if not errors and is_replan and drop_new_task is not None:
            assert isinstance(value, ReplanDeltaModel)
            new_ids = [
                task.id for task in value.new_tasks if task.tool_kind == "file_analysis"
            ]
            records = {
                task.task_id: task
                for task in plan_to_records(
                    plan, run_id="replan-validation", created_by="agent"
                )[1]
            }
            dropped = [task_id for task_id in new_ids if drop_new_task(records[task_id])]
            if dropped:
                plan = without_tasks(plan, set(dropped))
                errors = attempt_errors(plan)
                if errors:
                    log.warning(
                        "Replan: the plan without the dropped analyses %s failed "
                        "validation (%s); the previous plan version stays.",
                        ", ".join(dropped),
                        "; ".join(errors),
                    )
                    raise PlanningFailed(errors, usage=usage)
        if not errors:
            return plan, usage
    raise PlanningFailed(errors, usage=usage)


def without_tasks(plan: ExecutionPlanModel, ids: set[str]) -> ExecutionPlanModel:
    """*plan* without the tasks *ids*, their ids stripped from every
    remaining ``depends_on`` (the synthesis's included); pure."""
    return plan.model_copy(
        update={
            "tasks": [
                task.model_copy(
                    update={
                        "depends_on": [
                            task_id for task_id in task.depends_on if task_id not in ids
                        ]
                    }
                )
                for task in plan.tasks
                if task.id not in ids
            ]
        }
    )


def merge_replan_delta(
    delta: ReplanDeltaModel,
    *,
    previous_plan: PlanRecord | None,
    previous_tasks: list[PlanTaskRecord],
    language: str | None = None,
) -> tuple[ExecutionPlanModel | None, list[str]]:
    """Merge a model-authored delta with server-owned prior task truth."""
    if previous_plan is None:
        return None, ["A replan needs an existing plan."]
    prior_by_id = {task.task_id: task for task in previous_tasks}
    synthesis = next(
        (task for task in previous_tasks if task.tool_kind == "synthesis"),
        None,
    )
    errors: list[str] = []
    skip_ids = set(delta.skip_task_ids)
    unknown_skips = sorted(skip_ids - set(prior_by_id))
    if unknown_skips:
        errors.append(
            "skip_task_ids enthaelt unbekannte Tasks: "
            + ", ".join(unknown_skips)
            + "."
        )
    invalid_skips = sorted(
        task_id
        for task_id in skip_ids & set(prior_by_id)
        if prior_by_id[task_id].tool_kind == "synthesis"
        or prior_by_id[task_id].status != "pending"
    )
    if invalid_skips:
        errors.append(
            "Only source tasks that have not started yet may be skipped: "
            + ", ".join(invalid_skips) + "."
        )
    new_ids = [task.id for task in delta.new_tasks]
    collisions = sorted(set(new_ids) & set(prior_by_id))
    if collisions:
        errors.append(
            "New replan tasks need new ids; already present: "
            + ", ".join(collisions)
            + "."
        )
    synthesis_new = sorted(
        task.id for task in delta.new_tasks if task.tool_kind == "synthesis"
    )
    if synthesis_new:
        errors.append(
            "The replan delta must not contain a synthesis task: "
            + ", ".join(synthesis_new)
            + "."
        )
    running = sorted(
        task.task_id
        for task in previous_tasks
        if task.tool_kind != "synthesis" and task.status == "running"
    )
    if running:
        errors.append(
            "A replan must not be created while tasks are running: "
            + ", ".join(running)
            + "."
        )
    if errors:
        return None, errors

    source_tasks = [
        _task_record_to_model(task)
        for task in previous_tasks
        if task.tool_kind != "synthesis" and task.task_id not in skip_ids
    ]
    source_tasks.extend(delta.new_tasks)
    source_ids = [task.id for task in source_tasks]
    synthesis_id = synthesis.task_id if synthesis is not None else "s"
    if synthesis_id in set(new_ids):
        return None, [
            f"The new task id {synthesis_id!r} is reserved for the "
            "server-side synthesis."
        ]
    synthesis_task = PlanTaskModel(
        id=synthesis_id,
        title=(
            synthesis.title
            if synthesis is not None
            else t({"language": language or ""}, "task_title_synthesis")
        ),
        tool_kind="synthesis",
        objective=synthesis.objective if synthesis is not None else "",
        depends_on=source_ids,
        expected_output=(
            synthesis.expected_output if synthesis is not None else ""
        ),
    )
    assumptions = list(previous_plan.assumptions)
    for assumption in delta.assumptions:
        if assumption not in assumptions:
            assumptions.append(assumption)
    return (
        ExecutionPlanModel(
            summary_markdown=(
                delta.summary_markdown or previous_plan.summary_markdown
            ),
            tasks=[*source_tasks, synthesis_task],
            assumptions=assumptions,
            success_criteria=list(previous_plan.success_criteria),
        ),
        [],
    )


def _task_record_to_model(task: PlanTaskRecord) -> PlanTaskModel:
    """Project one server-owned task definition back into plan validation."""
    return PlanTaskModel.model_validate(
        {
            "id": task.task_id,
            "title": task.title,
            "tool_kind": task.tool_kind,
            "objective": task.objective,
            "queries": list(task.queries),
            "gap_ids": list(task.gap_ids),
            "depends_on": list(task.depends_on),
            "params": dict(task.params),
            "expected_output": task.expected_output,
        }
    )


def summarize_discovery(
    discovery: DiscoveryResult | None, *, covered: frozenset[str]
) -> str:
    """Compact digest of facts + gaps for the planner prompt.

    Model text condensed from documents: the prompt shows it only inside
    the ``discovery-state`` fence. A fact's source is the title the
    discovery results showed, never an identifier. A gap in *covered* is
    marked :data:`COVERED_GAP_NOTE`, without its blocking flag and its
    recommended capability: a finished task covered it.
    """
    if discovery is None:
        return "(no discovery performed)"
    lines = [
        f"Known: {finding.fact} [{finding.source}]"
        + ("" if finding.fresh else " (possibly outdated)")
        for finding in discovery.known_facts
    ]
    lines += [
        f"Gap {gap.gap_id} ({gap.kind}, {COVERED_GAP_NOTE}): {gap.description}"
        if gap.gap_id in covered
        else f"Gap {gap.gap_id} ({gap.kind}"
        + (", blocking" if gap.blocking else "")
        + f"): {gap.description} -> {gap.recommended_capability}"
        for gap in discovery.gaps
    ]
    return "\n".join(lines) or "(no findings)"


def plan_to_records(
    plan: ExecutionPlanModel,
    *,
    run_id: str,
    created_by: str,
    reason: str = "",
    previous_tasks: "Sequence[PlanTaskRecord] | None" = None,
) -> tuple[PlanRecord, list[PlanTaskRecord]]:
    """Convert the wire plan into control-store records (version assigned
    by the store as latest+1)."""
    plan_id = f"plan_{uuid.uuid4().hex}"
    record = PlanRecord(
        plan_id=plan_id,
        run_id=run_id,
        version=0,
        status="proposed",
        created_by=created_by,
        summary_markdown=plan.summary_markdown,
        assumptions=tuple(plan.assumptions),
        success_criteria=tuple(plan.success_criteria),
        reason=reason,
    )
    tasks = [
        PlanTaskRecord(
            task_id=task.id,
            plan_id=plan_id,
            run_id=run_id,
            ordinal=index,
            title=task.title,
            tool_kind=task.tool_kind,
            objective=task.objective,
            queries=tuple(task.queries),
            gap_ids=tuple(task.gap_ids),
            depends_on=tuple(task.depends_on),
            budget={},
            params=task.params.model_dump(exclude_none=True),
            expected_output=task.expected_output,
        )
        for index, task in enumerate(plan.tasks)
    ]
    if previous_tasks:
        tasks = carry_forward_terminal_task_results(
            list(previous_tasks), tasks
        )
    return record, tasks
