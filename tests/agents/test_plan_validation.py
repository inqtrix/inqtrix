"""Deterministic plan-validation rules for user edits and the planner."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.agents.plan_models import ExecutionPlanModel
from inqtrix.agents.plan_validation import validate_plan


def _plan(tasks: list[dict]) -> ExecutionPlanModel:
    return ExecutionPlanModel.model_validate(
        {"summary_markdown": "Testplan", "tasks": tasks}
    )


def _task(task_id: str, **overrides) -> dict:
    base = {
        "id": task_id,
        "title": f"Aufgabe {task_id}",
        "tool_kind": "rag_query",
        # Retrieval kinds require concrete queries since the plan
        # transparency change; tests override to [] to probe the rule.
        "queries": [f"Frage zu {task_id}"],
    }
    base.update(overrides)
    return base


def _valid_tasks() -> list[dict]:
    return [
        _task("t1", tool_kind="web_research"),
        _task("t2", tool_kind="rag_query"),
        _task("s", tool_kind="synthesis", depends_on=["t1", "t2"]),
    ]


def test_valid_plan_has_no_errors() -> None:
    assert validate_plan(_plan(_valid_tasks())) == []


def test_exactly_one_synthesis_required() -> None:
    errors = validate_plan(
        _plan([_task("t1"), _task("t2", tool_kind="web_research")])
    )
    assert any("exactly one synthesis task" in error for error in errors)


def test_synthesis_must_depend_on_every_other_task() -> None:
    tasks = _valid_tasks()
    tasks[2]["depends_on"] = ["t1"]
    errors = validate_plan(_plan(tasks))
    assert any("missing: t2" in error for error in errors)


def test_duplicate_task_ids_reported() -> None:
    tasks = [
        _task("t1"),
        _task("t1", tool_kind="web_research"),
        _task("s", tool_kind="synthesis", depends_on=["t1"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert any("Duplicate task ids: t1" in error for error in errors)


def test_unknown_dependency_and_self_dependency() -> None:
    tasks = [
        _task("t1", depends_on=["ghost"]),
        _task("t2", depends_on=["t2"]),
        _task("s", tool_kind="synthesis", depends_on=["t1", "t2"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert any("unknown task id 'ghost'" in error for error in errors)
    assert any("depends on itself" in error for error in errors)


def test_dependency_cycle_detected() -> None:
    tasks = [
        _task("a", depends_on=["b"]),
        _task("b", depends_on=["a"]),
        _task("s", tool_kind="synthesis", depends_on=["a", "b"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert any("Cyclic dependencies" in error for error in errors)
    assert any("a, b" in error for error in errors)


def test_profile_domain_is_tool_specific() -> None:
    tasks = [
        _task("t1", tool_kind="web_research", params={"profile": "tief"}),
        _task("t2", tool_kind="rag_query", params={"profile": "deep"}),
        _task("t3", tool_kind="file_analysis", params={"profile": "schnell"}),
        _task("s", tool_kind="synthesis", depends_on=["t1", "t2", "t3"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert any("t1: unknown profile 'tief'" in error for error in errors)
    assert any("t2: unknown profile 'deep'" in error for error in errors)
    assert any("t3: file_analysis does not accept params.profile; set it to null." in error for error in errors)


@pytest.mark.parametrize("tool_kind", ["synthesis", "web_instant", "file_analysis"])
def test_non_retrieval_profiles_are_rejected_with_the_required_correction(tool_kind):
    task = _task("unit", tool_kind=tool_kind, params={"profile": "standard"})
    tasks = [task]
    if tool_kind != "synthesis":
        tasks.append(_task("s", tool_kind="synthesis", depends_on=["unit"]))
    plan = _plan(tasks)
    assert validate_plan(plan) == [
        f"Task unit: {tool_kind} does not accept params.profile; set it to null."
    ]
    # Do not make a generated plan valid by silently discarding a non-null knob.
    assert plan.tasks[0].params.profile == "standard"
    plan.tasks[0].params.profile = None
    assert validate_plan(plan) == []


def test_task_budgets_are_server_managed() -> None:
    tasks = [
        _task("t1", budget={"max_tokens": 6000}),
        _task("t2", budget={"max_tokens": 6000}),
        _task("s", tool_kind="synthesis", depends_on=["t1", "t2"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert sum("managed server-side" in error for error in errors) == 2


def test_web_instant_requires_exactly_one_query() -> None:
    tasks = [
        _task(
            "t1",
            tool_kind="web_instant",
            queries=["Frage eins?", "Frage zwei?"],
        ),
        _task("s", tool_kind="synthesis", depends_on=["t1"]),
    ]
    errors = validate_plan(_plan(tasks))
    assert any("web_instant needs exactly one" in error for error in errors)


def test_normal_plan_blocks_research_and_explicit_profile_is_fixed() -> None:
    plan = _plan(_valid_tasks())
    normal_errors = validate_plan(plan, web_research_allowed=False)
    assert any(
        "in this tier" in error and "web_instant" in error
        for error in normal_errors
    )
    explicit_errors = validate_plan(
        plan,
        web_research_allowed=True,
        web_research_profile="compact",
    )
    assert any("profile=compact" in error for error in explicit_errors)


def test_recency_uses_the_capability_vocabulary() -> None:
    plan = _plan([_task("t1", params={"recency": ""})])
    assert plan.tasks[0].params.recency is None
    with pytest.raises(ValidationError):
        _plan([_task("t1", params={"recency": "365d"})])


def test_gap_ids_checked_only_when_universe_given() -> None:
    tasks = [
        _task("t1", gap_ids=["g1", "ghost"]),
        _task("s", tool_kind="synthesis", depends_on=["t1"]),
    ]
    assert validate_plan(_plan(tasks)) == []
    errors = validate_plan(_plan(tasks), known_gap_ids={"g1"})
    assert any("unknown gap id 'ghost'" in error for error in errors)


def test_max_tasks_ceiling() -> None:
    tasks = [_task(f"t{i}") for i in range(8)] + [
        _task("s", tool_kind="synthesis", depends_on=[f"t{i}" for i in range(8)])
    ]
    errors = validate_plan(_plan(tasks), max_tasks=8)
    assert any("Too many tasks (9" in error for error in errors)


def test_retrieval_tasks_require_concrete_queries() -> None:
    """web/rag tasks without literal query strings are invalid; the
    exempt kinds (synthesis by design, file_analysis via its
    objective/title executor fallback) stay valid without queries."""
    tasks = [
        _task("t1", tool_kind="web_research", queries=[]),
        _task("t2", tool_kind="web_instant", queries=["   "]),
        _task("t3", tool_kind="rag_query", queries=[]),
        _task("t4", tool_kind="file_analysis", queries=[]),
        _task(
            "s",
            tool_kind="synthesis",
            queries=[],
            depends_on=["t1", "t2", "t3", "t4"],
        ),
    ]
    errors = validate_plan(_plan(tasks))
    assert any(
        "t1: web_research needs at least" in error for error in errors
    )
    assert any(
        "t2: web_instant needs at least" in error for error in errors
    )
    assert any(
        "t3: rag_query needs at least" in error for error in errors
    )
    assert not any("t4" in error for error in errors)
    assert not any("s:" in error for error in errors)


def test_allowed_collection_ids_gate() -> None:
    """Explicit collection references outside the caller-visible set are
    violations; ``None`` skips the check (no knowledge service), an
    empty set rejects every explicit reference."""
    tasks = [
        _task("t1", params={"collection_ids": ["kc_1", "kc_ghost"]}),
        _task("s", tool_kind="synthesis", depends_on=["t1"]),
    ]
    assert validate_plan(_plan(tasks)) == []
    errors = validate_plan(
        _plan(tasks), allowed_collection_ids={"kc_1"}
    )
    assert errors == [
        "Task t1: collection 'kc_ghost' is not visible or unknown."
    ]
    errors = validate_plan(_plan(tasks), allowed_collection_ids=set())
    assert any("'kc_1'" in error for error in errors)
    assert any("'kc_ghost'" in error for error in errors)


def test_schema_rejects_unknown_fields_and_empty_plans() -> None:
    with pytest.raises(ValidationError):
        ExecutionPlanModel.model_validate({"tasks": []})
    with pytest.raises(ValidationError):
        ExecutionPlanModel.model_validate(
            {"tasks": [_task("t1")], "surprise": True}
        )
    with pytest.raises(ValidationError):
        ExecutionPlanModel.model_validate(
            {"tasks": [_task("t1", params={"typo_knob": 1})]}
        )


def test_planner_schema_omits_legacy_task_budget() -> None:
    schema = ExecutionPlanModel.model_json_schema()
    task_properties = schema["$defs"]["PlanTaskModel"]["properties"]
    assert "budget" not in task_properties


def test_planner_cannot_select_a_task_model_tier() -> None:
    schema = ExecutionPlanModel.model_json_schema()
    assert "model_tier" not in schema["$defs"]["PlanTaskParams"]["properties"]
    for value in ("high", "mid", "fast", None):
        with pytest.raises(ValidationError) as info:
            _plan([_task("t1", params={"model_tier": value})])
        assert info.value.errors()[0]["loc"] == ("tasks", 0, "params", "model_tier")
        assert info.value.errors()[0]["type"] == "extra_forbidden"


# -- dropping a replan's new file analyses ---------------------------------- #


def _new_task(task_id: str, tool_kind: str, *depends_on: str) -> dict:
    """One task of a replan delta; a file analysis carries its questions."""
    return {
        "id": task_id,
        "title": f"Zusatz {task_id}",
        "tool_kind": tool_kind,
        "queries": [f"Zusatzfrage {task_id}?"],
        "depends_on": list(depends_on),
    }


def _analysis_delta(*tasks: dict, skip: tuple[str, ...] = ()) -> dict:
    return {
        "summary_markdown": "Additiver Replan.",
        "new_tasks": list(tasks),
        "skip_task_ids": list(skip),
        "assumptions": [],
    }


def _previous(*tasks):
    from dataclasses import replace

    from tests.agents.test_execution_resume_contract import _task as _record_task

    return [*tasks, replace(_record_task("s", "synthesis", status="completed"), queries=())]


def test_dropping_any_new_tasks_keeps_a_merged_plan_valid() -> None:
    """Removing new tasks from a valid merged plan can never make it
    invalid: for every subset of the new tasks, the plan without them
    passes the validator and the additive check, and no remaining task
    depends on a removed one. Turns red when the removed ids are stripped
    from the synthesis only."""
    from itertools import combinations

    from inqtrix.agents.control_ports import PlanRecord, additive_replan_errors
    from inqtrix.agents.plan_models import ReplanDeltaModel
    from inqtrix.agents.planner import merge_replan_delta, plan_to_records, without_tasks
    from tests.agents.test_execution_resume_contract import _task as _record_task

    previous = _previous(
        _record_task("t1", "rag_query", status="completed"), _record_task("tp", "web_instant"),
    )
    plan, errors = merge_replan_delta(
        ReplanDeltaModel.model_validate(_analysis_delta(
            _new_task("t3", "file_analysis"), _new_task("t4", "rag_query", "t3"),
            _new_task("t5", "file_analysis", "t4"), skip=("tp",),
        )),
        previous_plan=PlanRecord(plan_id="plan-a", run_id="run-a", version=1, status="approved",
                                 created_by="agent"),
        previous_tasks=previous,
    )
    assert plan is not None and errors == [] and validate_plan(plan, max_tasks=8) == []
    subsets = [set(ids) for size in (1, 2, 3) for ids in combinations(("t3", "t4", "t5"), size)]

    def holds(ids: set[str]) -> bool:
        reduced = without_tasks(plan, ids)
        remaining = {task.id for task in reduced.tasks}
        _record, candidate = plan_to_records(reduced, run_id="run-a", created_by="agent")
        return (
            validate_plan(reduced, max_tasks=8) == []
            and additive_replan_errors(previous, candidate) == []
            and all(set(task.depends_on) <= remaining for task in reduced.tasks)
        )

    assert all(holds(ids) for ids in subsets)


def test_only_new_file_analyses_are_offered_for_dropping() -> None:
    """The plan-time check is asked about a replan's NEW file analyses only
    - never a kept task, never another kind - and a dropped one leaves the
    plan with its id gone from the synthesis. Turns red when every new task
    is offered."""
    from tests.agents.test_execution_resume_contract import (
        _ScriptedPlannerLLM,
        _replan,
        _task as _record_task,
    )

    offered: list[str] = []

    def drop(task) -> bool:
        offered.append(task.task_id)
        return True

    llm = _ScriptedPlannerLLM([_analysis_delta(
        _new_task("t3", "file_analysis"), _new_task("t4", "web_instant"),
    )])
    plan, _usage = _replan(
        llm, _previous(_record_task("t1", "file_analysis", status="completed")), drop_new_task=drop,
    )
    assert (offered, [task.id for task in plan.tasks], plan.tasks[-1].depends_on) == (
        ["t3"], ["t1", "t4", "s"], ["t1", "t4"],
    )


def test_a_plan_attempt_with_errors_is_never_checked_for_drops() -> None:
    """A plan a later check discards is never searched: both attempts
    carry a web task the assignment does not need, so no drop is asked.
    Turns red when the drop runs before the checks."""
    from inqtrix.agents.planner import PlanningFailed
    from tests.agents.test_execution_resume_contract import (
        _ScriptedPlannerLLM,
        _replan,
        _task as _record_task,
    )

    offered: list[str] = []

    def drop(task) -> bool:
        offered.append(task.task_id)
        return True

    delta = _analysis_delta(_new_task("t3", "file_analysis"), _new_task("t5", "web_instant"))
    llm = _ScriptedPlannerLLM([delta, dict(delta)])
    raised = False
    try:
        _replan(
            llm, _previous(_record_task("t1", "file_analysis", status="completed")),
            web_needed=False, drop_new_task=drop,
        )
    except PlanningFailed:
        raised = True
    assert (raised, offered) == (True, [])


def test_a_drop_that_breaks_the_plan_fails_without_a_repair_round(monkeypatch) -> None:
    """The model's plan was valid and the server's drop broke it: a repair
    round cannot help, so the planning fails at once, with the check's
    errors, and the previous plan version stays (the caller's
    replan_invalid exit). Turns red when the planner goes on to a repair
    round."""
    from inqtrix.agents import planner
    from inqtrix.agents.planner import PlanningFailed
    from tests.agents.test_execution_resume_contract import (
        _ScriptedPlannerLLM,
        _replan,
        _task as _record_task,
    )

    def leaves_the_synthesis_dependency(plan, ids):
        # Removes the tasks, but leaves their ids in the synthesis.
        return plan.model_copy(update={"tasks": [task for task in plan.tasks if task.id not in ids]})

    monkeypatch.setattr(planner, "without_tasks", leaves_the_synthesis_dependency)
    delta = _analysis_delta(_new_task("t3", "file_analysis"), _new_task("t4", "web_instant"))
    llm = _ScriptedPlannerLLM([delta, dict(delta)])
    with pytest.raises(PlanningFailed) as info:
        _replan(
            llm, _previous(_record_task("t1", "file_analysis", status="completed")),
            drop_new_task=lambda task: task.task_id == "t3",
        )
    assert (
        any("depends_on refers to the unknown task id 't3'" in error for error in info.value.errors),
        len(llm.prompts),
    ) == (True, 1)
