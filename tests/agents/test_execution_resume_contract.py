"""Crash-window contracts for mission plan-task execution."""

from __future__ import annotations

import pytest

import json

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from inqtrix.agents.algorithm import (
    _DEPS,
    _child_origin_key,
    _emit_task_ended,
    _fold_pending_children,
    _node_children_wait,
    _node_synthesize,
    _reconcile_persisted_execution,
    _run_web_instant,
)
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    PlanRecord,
    PlanTaskRecord,
    additive_replan_errors,
    carry_forward_terminal_task_results,
)
from inqtrix.agents.plan_models import ReplanDeltaModel
from inqtrix.agents.planner import merge_replan_delta
from inqtrix.agents.web_execution_policy import derive_web_research_policy
from inqtrix.agents.scheduler import TaskOutcome
from inqtrix.core.results import SourcePolicy
from inqtrix.capabilities import build_capability_registry
from inqtrix.search_result import GroundedSearchResult, GroundedSource


def _task(
    task_id: str,
    tool_kind: str,
    *,
    status: str = "pending",
    child_run_id: str | None = None,
    result_summary: str = "",
) -> PlanTaskRecord:
    return PlanTaskRecord(
        task_id=task_id,
        plan_id="plan-a",
        run_id="run-a",
        ordinal=0 if task_id != "s" else 1,
        title=task_id,
        tool_kind=tool_kind,
        queries=("Welche Evidenz liegt vor?",),
        status=status,
        child_run_id=child_run_id,
        result_summary=result_summary,
    )


def test_instant_web_task_books_provider_reported_usage() -> None:
    class UsageSearch:
        def search(self, query: str, **_kwargs: Any) -> GroundedSearchResult:
            return GroundedSearchResult(
                answer=f"Evidence for {query}",
                sources=[GroundedSource(url="https://example.test")],
                prompt_tokens=101,
                completion_tokens=29,
            )

    events: list[tuple[str, dict[str, Any]]] = []
    usage_search = UsageSearch()
    deps = SimpleNamespace(
        capabilities=build_capability_registry(search_provider=usage_search),
        context=SimpleNamespace(
            principal=None,
            providers=SimpleNamespace(search=usage_search),
            run_id="run-usage",
            workspace_id="ws-usage",
        ),
        request=SimpleNamespace(knowledge_filters={}),
        knowledge_collection_ids=None,
        emit=lambda event_type, payload: events.append((event_type, payload)),
        visible_to=None,
    )

    outcome = _run_web_instant(deps, _task("web", "web_instant"))

    assert outcome.usage == {
        "prompt_tokens": 101,
        "completion_tokens": 29,
    }
    assert events[-1][1]["metrics"]["prompt_tokens"] == 101


class _RunStore:
    def __init__(self, children: list[dict[str, Any]]) -> None:
        self.rows = {str(row["run_id"]): row for row in children}
        self.results: dict[str, dict[str, Any]] = {}

    def children(self, parent_run_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows.values()
            if row.get("parent_run_id") == parent_run_id
        ]

    def get(self, run_id: str) -> dict[str, Any]:
        return dict(self.rows[run_id])

    def result(self, run_id: str) -> dict[str, Any]:
        return dict(self.results[run_id])


class _RunService:
    def __init__(self, store: _RunStore) -> None:
        self.run_store = store
        self.submissions: list[dict[str, Any]] = []

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        self.submissions.append(kwargs)
        run_id = f"child-{len(self.submissions)}"
        self.run_store.rows[run_id] = {
            "run_id": run_id,
            "parent_run_id": kwargs["parent_run_id"],
            "origin_key": kwargs["origin_key"],
            "status": "running",
        }
        return dict(self.run_store.rows[run_id])


def _deps(
    control: MemoryAgentControlStore, service: _RunService
) -> SimpleNamespace:
    return SimpleNamespace(
        control=control,
        run_service=service,
        resolver=SimpleNamespace(
            resolve=lambda body: SimpleNamespace(body=body)
        ),
        context=SimpleNamespace(
            run_id="run-a",
            workspace_id="workspace-a",
            principal=None,
        ),
        request=SimpleNamespace(tool_directives=("web_research",)),
        source_policy=SourcePolicy(),
        depth="normal",
        tier="",
        emit=lambda *_args, **_kwargs: None,
        cancelled=lambda: False,
    )


def _save(
    control: MemoryAgentControlStore, tasks: list[PlanTaskRecord]
) -> None:
    asyncio.run(
        control.save_plan(
            run_id="run-a",
            plan=PlanRecord(
                plan_id="plan-a",
                run_id="run-a",
                version=1,
                status="approved",
                created_by="agent",
            ),
            tasks=tasks,
        )
    )


async def _request_task_cancel(
    control: MemoryAgentControlStore, task_id: str
) -> None:
    async def _authorize(control_write: Any) -> Any:
        return control_write(None, lambda _child_run_id: "cancelled")

    await control.request_plan_task_cancel(
        run_id="run-a",
        plan_id="plan-a",
        task_id=task_id,
        authorize=_authorize,
    )


def test_running_child_reattaches_and_terminal_row_rehydrates() -> None:
    """Submit-before-checkpoint and terminal-before-checkpoint do no work twice."""
    control = MemoryAgentControlStore()
    research = _task("r", "web_research", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [research, synthesis])
    child_id = "child-existing"
    run_store = _RunStore(
        [
            {
                "run_id": child_id,
                "parent_run_id": "run-a",
                "origin_key": _child_origin_key(research, 1),
                "status": "running",
            }
        ]
    )
    service = _RunService(run_store)
    deps = _deps(control, service)
    policy = derive_web_research_policy(
        depth="normal", admitted_directive=True
    )

    outcomes, pending = _reconcile_persisted_execution(
        deps,
        {},
        [research, synthesis],
        {},
        {},
        research_policy=policy,
    )

    assert outcomes == {}
    assert pending == {
        "r": {"child_run_id": child_id, "attempt": 1}
    }
    assert service.submissions == []
    _, attached = asyncio.run(control.get_plan("run-a"))
    assert attached[0].child_run_id == child_id

    run_store.rows[child_id]["status"] = "completed"
    run_store.results[child_id] = {
        "answer": "Belegte Antwort",
        "references": [{"url": "https://example.test/source"}],
        "top_claims": [],
    }
    outcomes, pending = _fold_pending_children(
        deps,
        {},
        attached,
        outcomes,
        pending,
        research_policy=policy,
    )
    assert pending == {}
    assert outcomes["r"].status == "completed"

    # Simulate a lost checkpoint after the terminal task transition. The task
    # row and child result reconstruct the outcome without another submission.
    _, terminal_tasks = asyncio.run(control.get_plan("run-a"))
    recovered, pending = _reconcile_persisted_execution(
        deps,
        {},
        terminal_tasks,
        {},
        {},
        research_policy=policy,
    )
    assert pending == {}
    assert recovered["r"].summary == "Belegte Antwort"
    assert recovered["r"].evidence
    assert service.submissions == []


def test_running_child_submit_is_idempotent_across_reconciliation() -> None:
    control = MemoryAgentControlStore()
    research = _task("r", "web_research", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [research, synthesis])
    service = _RunService(_RunStore([]))
    deps = _deps(control, service)
    policy = derive_web_research_policy(
        depth="normal", admitted_directive=True
    )

    _outcomes, pending = _reconcile_persisted_execution(
        deps,
        {},
        [research, synthesis],
        {},
        {},
        research_policy=policy,
    )
    _, attached = asyncio.run(control.get_plan("run-a"))
    _reconcile_persisted_execution(
        deps,
        {},
        attached,
        {},
        pending,
        research_policy=policy,
    )

    assert len(service.submissions) == 1
    assert service.submissions[0]["origin_key"] == _child_origin_key(
        research, 1
    )


def test_running_local_task_fails_closed_instead_of_repeating_call() -> None:
    control = MemoryAgentControlStore()
    local = _task("i", "web_instant", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [local, synthesis])
    deps = _deps(control, _RunService(_RunStore([])))

    outcomes, _pending = _reconcile_persisted_execution(
        deps,
        {},
        [local, synthesis],
        {},
        {},
        research_policy=derive_web_research_policy(depth="normal"),
    )

    assert outcomes["i"].failure_code == "local_execution_interrupted"
    _, tasks = asyncio.run(control.get_plan("run-a"))
    assert tasks[0].status == "failed"


def test_terminal_local_task_rehydrates_without_terminal_to_running() -> None:
    control = MemoryAgentControlStore()
    local = _task(
        "i",
        "web_instant",
        status="completed",
        result_summary="Persistiertes Teilergebnis",
    )
    synthesis = _task("s", "synthesis")
    _save(control, [local, synthesis])
    service = _RunService(_RunStore([]))

    outcomes, pending = _reconcile_persisted_execution(
        _deps(control, service),
        {},
        [local, synthesis],
        {},
        {},
        research_policy=derive_web_research_policy(depth="normal"),
    )

    assert pending == {}
    assert outcomes["i"].status == "completed"
    assert outcomes["i"].summary == "Persistiertes Teilergebnis"
    assert service.submissions == []
    _, tasks = asyncio.run(control.get_plan("run-a"))
    assert tasks[0].status == "completed"


def test_terminal_local_task_payload_recovers_evidence_claims_and_usage() -> None:
    """A terminal row survives the row-before-checkpoint crash window."""
    control = MemoryAgentControlStore()
    local = _task("i", "web_instant", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [local, synthesis])
    deps = _deps(control, _RunService(_RunStore([])))
    outcome = TaskOutcome(
        status="completed",
        summary="Persistiertes Ergebnis",
        evidence=[{"url": "https://example.test/evidence"}],
        claims=[{"text": "Belegte Aussage", "status": "supported"}],
        usage={"prompt_tokens": 7, "completion_tokens": 3},
    )

    _emit_task_ended(deps, {"language": "de"}, local, local.task_id, outcome)
    _, terminal_tasks = asyncio.run(control.get_plan("run-a"))
    stored = terminal_tasks[0]
    assert stored.status == "completed"
    assert "status" not in stored.result_payload
    assert "summary" not in stored.result_payload
    assert stored.result_payload["evidence"] == outcome.evidence

    recovered_state: dict[str, Any] = {
        "usage": {"prompt_tokens": 0, "completion_tokens": 0}
    }
    recovered, pending = _reconcile_persisted_execution(
        deps,
        recovered_state,
        terminal_tasks,
        {},
        {},
        research_policy=derive_web_research_policy(depth="normal"),
    )
    assert pending == {}
    assert recovered["i"].evidence == outcome.evidence
    assert recovered["i"].claims == outcome.claims
    assert recovered_state["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
    }

    # A checkpointed outcome already contributed its usage. Reconciliation
    # must not charge the row payload a second time.
    checkpoint_state: dict[str, Any] = {
        "usage": {"prompt_tokens": 7, "completion_tokens": 3}
    }
    checkpointed, _ = _reconcile_persisted_execution(
        deps,
        checkpoint_state,
        terminal_tasks,
        {"i": outcome},
        {},
        research_policy=derive_web_research_policy(depth="normal"),
    )
    assert checkpointed["i"].evidence == outcome.evidence
    assert checkpoint_state["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
    }


def test_cancel_requested_local_task_discards_late_provider_result() -> None:
    control = MemoryAgentControlStore()
    local = _task("i", "web_instant", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [local, synthesis])
    deps = _deps(control, _RunService(_RunStore([])))
    asyncio.run(_request_task_cancel(control, "i"))

    _emit_task_ended(
        deps,
        {"language": "de"},
        local,
        local.task_id,
        TaskOutcome(
            status="completed",
            summary="Must be discarded",
            answer_markdown="# Must be discarded",
            evidence=[{"url": "https://example.test/late"}],
            usage={"prompt_tokens": 17, "completion_tokens": 5},
        ),
    )

    _, tasks = asyncio.run(control.get_plan("run-a"))
    stored = tasks[0]
    assert stored.status == "cancelled"
    assert stored.result_payload == {
        "usage": {"prompt_tokens": 17, "completion_tokens": 5},
        "failure_code": "task_cancelled",
    }
    assert "Must be discarded" not in stored.result_summary


def test_cancel_requested_child_folds_as_cancelled_not_failed() -> None:
    control = MemoryAgentControlStore()
    research = replace(
        _task("r", "web_research", status="running"),
        child_run_id="child-cancel",
    )
    synthesis = _task("s", "synthesis")
    _save(control, [research, synthesis])
    asyncio.run(_request_task_cancel(control, "r"))
    run_store = _RunStore(
        [
            {
                "run_id": "child-cancel",
                "parent_run_id": "run-a",
                "status": "cancelled",
                "parent_task_attempt": 1,
            }
        ]
    )
    deps = _deps(control, _RunService(run_store))
    events: list[tuple[str, dict[str, Any]]] = []
    deps.emit = lambda event_type, payload: events.append(
        (event_type, payload)
    )
    _, current = asyncio.run(control.get_plan("run-a"))

    outcomes, pending = _fold_pending_children(
        deps,
        {},
        current,
        {},
        {"r": {"child_run_id": "child-cancel", "attempt": 1}},
        research_policy=derive_web_research_policy(
            depth="normal", admitted_directive=True
        ),
    )

    assert pending == {}
    assert outcomes["r"].status == "cancelled"
    assert outcomes["r"].child_run_id == "child-cancel"
    assert events[-1][1]["child_run_id"] == "child-cancel"
    _, terminal = asyncio.run(control.get_plan("run-a"))
    assert terminal[0].status == "cancelled"
    assert terminal[0].child_run_id == "child-cancel"


def test_completed_child_provider_report_is_not_discarded_without_references() -> None:
    control = MemoryAgentControlStore()
    child_id = "child-empty"
    research = _task(
        "r", "web_research", status="running", child_run_id=child_id
    )
    _save(control, [research, _task("s", "synthesis")])
    run_store = _RunStore(
        [
            {
                "run_id": child_id,
                "parent_run_id": "run-a",
                "origin_key": _child_origin_key(research, 1),
                "status": "completed",
            }
        ]
    )
    run_store.results[child_id] = {
        "answer": "Keine belastbare Quelle gefunden.",
        "references": [],
        "top_claims": [],
    }
    deps = _deps(control, _RunService(run_store))
    policy = derive_web_research_policy(
        depth="normal", admitted_directive=True
    )

    outcomes, pending = _reconcile_persisted_execution(
        deps,
        {},
        [research],
        {},
        {},
        research_policy=policy,
    )
    outcomes, pending = _fold_pending_children(
        deps,
        {},
        [research],
        outcomes,
        pending,
        research_policy=policy,
    )

    assert pending == {}
    assert outcomes["r"].status == "completed"
    assert outcomes["r"].answer_markdown == (
        "Keine belastbare Quelle gefunden."
    )
    _, tasks = asyncio.run(control.get_plan("run-a"))
    assert tasks[0].status == "completed"


def test_cancel_after_child_terminal_preserves_completed_task() -> None:
    """Cancel settlement folds a terminal child before closing open rows."""
    control = MemoryAgentControlStore()
    child_id = "child-finished"
    research = _task(
        "r", "web_research", status="running", child_run_id=child_id
    )
    _save(control, [research, _task("s", "synthesis")])
    run_store = _RunStore(
        [
            {
                "run_id": child_id,
                "parent_run_id": "run-a",
                "origin_key": _child_origin_key(research, 1),
                "status": "completed",
            }
        ]
    )
    run_store.results[child_id] = {
        "answer": "Belegte Antwort",
        "references": [{"url": "https://example.test/source"}],
        "top_claims": [],
    }
    deps = _deps(control, _RunService(run_store))
    deps.cancelled = lambda: True
    token = _DEPS.set(deps)
    try:
        state = _node_children_wait(
            {
                "pending_children": {
                    "r": {"child_run_id": child_id, "attempt": 1}
                },
                "outcomes": {},
            }
        )
    finally:
        _DEPS.reset(token)

    assert state["cancelled"] is True
    assert state["outcomes"]["r"]["status"] == "completed"
    _, tasks = asyncio.run(control.get_plan("run-a"))
    assert tasks[0].status == "completed"
    assert tasks[1].status == "skipped"


def test_running_synthesis_fails_closed_on_resume() -> None:
    control = MemoryAgentControlStore()
    synthesis = _task("s", "synthesis", status="running")
    _save(control, [synthesis])
    deps = _deps(control, _RunService(_RunStore([])))
    token = _DEPS.set(deps)
    try:
        state = _node_synthesize({})
    finally:
        _DEPS.reset(token)

    assert state["failure"] == "synthesis_execution_interrupted"
    _, tasks = asyncio.run(control.get_plan("run-a"))
    assert tasks[0].status == "failed"


def test_additive_replan_carries_terminal_results_and_rejects_id_reuse() -> None:
    previous = _task(
        "i",
        "web_instant",
        status="completed",
        result_summary="Belegt",
    )
    previous = replace(
        previous,
        result_payload={
            "evidence": [{"url": "https://example.test/source"}]
        },
    )
    unchanged = replace(
        previous,
        plan_id="plan-b",
        title="Praeziserer sichtbarer Titel",
        status="pending",
        result_summary="",
        result_payload={},
    )

    carried = carry_forward_terminal_task_results([previous], [unchanged])

    assert carried[0].status == "completed"
    assert carried[0].result_summary == "Belegt"
    assert carried[0].result_payload == previous.result_payload

    changed = replace(unchanged, queries=("Eine geaenderte Frage",))
    errors = additive_replan_errors([previous], [changed])
    assert any("new ids" in error and "i" in error for error in errors)


def test_replan_delta_preserves_terminal_truth_and_rebuilds_synthesis() -> None:
    previous_plan = PlanRecord(
        plan_id="plan-a",
        run_id="run-a",
        version=1,
        status="approved",
        created_by="agent",
        summary_markdown="Initial plan",
        assumptions=("Initial assumption",),
        success_criteria=("Criterion",),
    )
    completed = replace(
        _task("t1", "web_instant", status="completed"),
        result_summary="Complete",
        result_payload={"answer_markdown": "Full result"},
    )
    synthesis = replace(
        _task("s", "synthesis"),
        queries=(),
        depends_on=("t1",),
    )
    delta = ReplanDeltaModel.model_validate(
        {
            "summary_markdown": "Close remaining evidence gap",
            "new_tasks": [
                {
                    "id": "t2",
                    "title": "Additional evidence",
                    "tool_kind": "web_instant",
                    "queries": ["Which evidence closes the remaining gap?"],
                }
            ],
            "assumptions": ["New assumption"],
        }
    )

    merged, errors = merge_replan_delta(
        delta,
        previous_plan=previous_plan,
        previous_tasks=[completed, synthesis],
    )

    assert errors == []
    assert merged is not None
    assert [task.id for task in merged.tasks] == ["t1", "t2", "s"]
    assert merged.tasks[-1].depends_on == ["t1", "t2"]
    assert merged.tasks[0].queries == ["Welche Evidenz liegt vor?"]
    assert merged.assumptions == ["Initial assumption", "New assumption"]


def test_replan_delta_rejects_completed_id_reuse_and_terminal_skip() -> None:
    previous_plan = PlanRecord(
        plan_id="plan-a",
        run_id="run-a",
        version=1,
        status="approved",
        created_by="agent",
    )
    completed = _task("t1", "web_instant", status="completed")
    synthesis = replace(_task("s", "synthesis"), queries=())
    delta = ReplanDeltaModel.model_validate(
        {
            "new_tasks": [
                {
                    "id": "t1",
                    "title": "Changed work",
                    "tool_kind": "web_instant",
                    "queries": ["A changed question?"],
                }
            ],
            "skip_task_ids": ["t1"],
        }
    )

    merged, errors = merge_replan_delta(
        delta,
        previous_plan=previous_plan,
        previous_tasks=[completed, synthesis],
    )

    assert merged is None
    assert any("already present" in error for error in errors)
    assert any("have not started yet" in error for error in errors)


def test_replan_delta_accepts_explicit_noop_and_preserves_prior_truth() -> None:
    previous_plan = PlanRecord(
        plan_id="plan-noop",
        run_id="run-noop",
        version=1,
        status="approved",
        created_by="agent",
        summary_markdown="Prior plan",
    )
    completed = replace(
        _task("t1", "web_instant", status="insufficient_evidence"),
        result_summary="Gap remains visible.",
    )
    synthesis = replace(_task("s", "synthesis"), queries=())
    delta = ReplanDeltaModel.model_validate(
        {
            "summary_markdown": "No additional work is justified.",
            "new_tasks": [],
            "skip_task_ids": [],
        }
    )

    merged, errors = merge_replan_delta(
        delta,
        previous_plan=previous_plan,
        previous_tasks=[completed, synthesis],
    )

    assert errors == []
    assert merged is not None
    assert [task.id for task in merged.tasks] == ["t1", "s"]
    assert merged.tasks[-1].depends_on == ["t1"]


class _ScriptedPlannerLLM:
    """Structured replies in order; every call books 7/5 tokens."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self, prompt: str, *, schema_name: str, **kwargs: Any
    ) -> Any:
        self.prompts.append(prompt)
        parsed = self.replies.pop(0)
        return SimpleNamespace(
            parsed=parsed,
            content=json.dumps(parsed),
            prompt_tokens=7,
            completion_tokens=5,
        )


def _delta(*ids: str) -> dict[str, Any]:
    return {
        "summary_markdown": "Additiver Replan.",
        "new_tasks": [
            {
                "id": task_id,
                "title": f"Zusatz {task_id}",
                "tool_kind": "web_instant",
                "queries": [f"Zusatzfrage {task_id}?"],
            }
            for task_id in ids
        ],
        "skip_task_ids": [],
        "assumptions": [],
    }


def _replan(llm: Any, previous_tasks: list[PlanTaskRecord], **kwargs: Any) -> Any:
    from inqtrix.agents.planner import run_planner

    previous_plan = PlanRecord(
        plan_id="plan-a",
        run_id="run-a",
        version=1,
        status="approved",
        created_by="agent",
    )
    return run_planner(
        llm,
        question="Frage?",
        discovery=None,
        criteria=(),
        covered_gap_ids=frozenset(),
        max_tasks=8,
        web_allowed=True,
        knowledge_allowed=True,
        model=None,
        reasoning_effort=None,
        timeout=1.0,
        previous_plan=previous_plan,
        previous_tasks=previous_tasks,
        **kwargs,
    )


def test_replan_prompt_carries_the_headroom_and_repair_names_the_ceiling() -> None:
    """Six kept tasks + synthesis leave room for ONE new task at a ceiling
    of eight: the first prompt says so, an oversized delta is repaired
    with the validator's own count, and the repaired plan holds exactly
    eight tasks."""
    kept = [_task(f"t{i}", "web_instant", status="completed") for i in range(1, 7)]
    synthesis = replace(_task("s", "synthesis", status="completed"), queries=())
    llm = _ScriptedPlannerLLM([_delta("n1", "n2", "n3"), _delta("n1")])

    plan, usage = _replan(llm, [*kept, synthesis])

    assert len(plan.tasks) == 8
    assert len(llm.prompts) == 2
    assert "room for at most 1 new tasks" in llm.prompts[0]
    assert "Too many tasks (10, allowed are 8)" in llm.prompts[1]
    assert usage == {"prompt_tokens": 14, "completion_tokens": 10}


def test_failed_tasks_occupy_replan_slots() -> None:
    """Failed tasks are immutable in an additive replan, so they count
    against the ceiling: five completed + two failed + synthesis leaves
    no room, the prompt says so, and an empty delta is accepted."""
    kept = [_task(f"t{i}", "web_instant", status="completed") for i in range(1, 6)]
    failed = [_task(f"f{i}", "web_instant", status="failed") for i in range(1, 3)]
    synthesis = replace(_task("s", "synthesis", status="completed"), queries=())
    llm = _ScriptedPlannerLLM([_delta()])

    plan, _usage = _replan(llm, [*kept, *failed, synthesis])

    assert len(llm.prompts) == 1
    assert "NO room for new tasks" in llm.prompts[0]
    assert len(plan.tasks) == 8


def test_planning_failed_carries_the_usage_of_both_attempts() -> None:
    """A failed planner attempt is still spend: the exception carries the
    usage of every attempt so the caller can book it."""
    from inqtrix.agents.planner import PlanningFailed, run_planner

    invalid = {
        "summary_markdown": "x",
        "tasks": [{"id": "t1", "title": "Nur Suche", "tool_kind": "rag_query"}],
    }
    llm = _ScriptedPlannerLLM([dict(invalid), dict(invalid)])
    with pytest.raises(PlanningFailed) as info:
        run_planner(
            llm,
            question="Frage?",
            discovery=None,
            criteria=(),
            covered_gap_ids=frozenset(),
            max_tasks=8,
            web_allowed=True,
            knowledge_allowed=True,
            model=None,
            reasoning_effort=None,
            timeout=1.0,
        )
    assert len(llm.prompts) == 2
    assert info.value.usage == {"prompt_tokens": 14, "completion_tokens": 10}


def _child_fold_with_metrics(metrics: dict[str, Any] | None):
    control = MemoryAgentControlStore()
    research = _task("r", "web_research", status="running")
    synthesis = _task("s", "synthesis")
    _save(control, [research, synthesis])
    child_id = "child-existing"
    run_store = _RunStore(
        [
            {
                "run_id": child_id,
                "parent_run_id": "run-a",
                "origin_key": _child_origin_key(research, 1),
                "status": "running",
            }
        ]
    )
    service = _RunService(run_store)
    deps = _deps(control, service)
    events: list[tuple[str, dict[str, Any]]] = []
    deps.emit = lambda event_type, payload: events.append((event_type, dict(payload)))
    policy = derive_web_research_policy(depth="normal", admitted_directive=True)
    outcomes, pending = _reconcile_persisted_execution(
        deps, {}, [research, synthesis], {}, {}, research_policy=policy
    )
    assert pending == {"r": {"child_run_id": child_id, "attempt": 1}}
    run_store.rows[child_id]["status"] = "completed"
    result: dict[str, Any] = {
        "answer": "Belegte Antwort",
        "references": [{"url": "https://example.test/source"}],
        "top_claims": [],
    }
    if metrics is not None:
        result["metrics"] = metrics
    run_store.results[child_id] = result
    _, attached = asyncio.run(control.get_plan("run-a"))
    outcomes, pending = _fold_pending_children(
        deps, {}, attached, outcomes, pending, research_policy=policy
    )
    assert pending == {}
    return control, deps, events, outcomes, policy


def test_child_fold_carries_the_evidence_contract_and_emits_a_review_activity() -> None:
    """A child that capped its own confidence (93 % single-source) must not
    become a confident-sounding summary: the outcome carries the contract,
    task.finished publishes it, one activity names it, the row persists it
    and a lost checkpoint recovers it."""
    control, deps, events, outcomes, policy = _child_fold_with_metrics(
        {"evidence_contract_status": "needs_review", "confidence": 6, "single_source_ratio": 0.93}
    )
    outcome = outcomes["r"]
    assert outcome.evidence_contract_status == "needs_review"
    assert outcome.confidence == 6
    assert outcome.single_source_ratio == 0.93
    finished = [p for t, p in events if t == "inqtrix.agent.task.finished"]
    assert finished and finished[-1]["metrics"]["evidence_contract_status"] == "needs_review"
    assert finished[-1]["metrics"]["confidence"] == 6
    reviews = [
        p for t, p in events
        if t == "inqtrix.agent.activity" and p.get("operation") == "task.evidence_needs_review"
    ]
    assert len(reviews) == 1
    assert reviews[0]["task_id"] == "r"
    assert reviews[0]["activity_id"] == "evidence-review:r"
    # A note about the evidence, not a fallback path: the task keeps its
    # own status group on the desk.
    assert reviews[0]["evidence_limit"] is True
    assert "fallback" not in reviews[0]
    detail = reviews[0]["detail"]
    # Reader wording on every surface: no task ids, no status codes. The
    # stand-in deps carry no question, so the answer language is unresolved
    # and the note falls back to the English reader wording.
    assert detail.startswith("Subtask “")
    assert "supported by a single source only" in detail
    assert "93 % of the evidence from a single source each" in detail
    assert "confidence 6/10" in detail
    assert "needs_review" not in detail and "Task r" not in detail
    _, terminal_tasks = asyncio.run(control.get_plan("run-a"))
    row = next(task for task in terminal_tasks if task.task_id == "r")
    assert row.result_payload["evidence_contract_status"] == "needs_review"
    assert row.result_payload["confidence"] == 6
    recovered, _pending = _reconcile_persisted_execution(
        deps, {}, terminal_tasks, {}, {}, research_policy=policy
    )
    assert recovered["r"].evidence_contract_status == "needs_review"
    assert recovered["r"].confidence == 6


def test_a_partially_covered_child_emits_an_evidence_note_not_a_fallback() -> None:
    """A child whose evidence budget left sources unevaluated completes;
    its note names the cut and is marked as an evidence limit, never as
    a fallback path the task took."""
    _control, _deps, events, outcomes, _policy = _child_fold_with_metrics(
        {"omitted_evidence_record_count": 3, "omitted_evidence_query_count": 1}
    )
    assert outcomes["r"].status == "completed"
    notes = [
        p for t, p in events
        if t == "inqtrix.agent.activity" and p.get("operation") == "task.evidence_partial"
    ]
    assert len(notes) == 1
    assert notes[0]["status"] == "completed"
    assert notes[0]["evidence_limit"] is True
    assert "fallback" not in notes[0]
    finished = [p for t, p in events if t == "inqtrix.agent.task.finished"]
    assert finished[-1]["status"] == "completed"


def test_clean_child_emits_no_review_activity() -> None:
    _control, _deps, events, outcomes, _policy = _child_fold_with_metrics(
        {"evidence_contract_status": "clean", "confidence": 8}
    )
    assert outcomes["r"].evidence_contract_status == "clean"
    assert not [
        p for t, p in events
        if t == "inqtrix.agent.activity" and p.get("operation") == "task.evidence_needs_review"
    ]
    _control2, _deps2, events2, outcomes2, _policy2 = _child_fold_with_metrics(None)
    assert outcomes2["r"].evidence_contract_status == "unknown"
    finished = [p for t, p in events2 if t == "inqtrix.agent.task.finished"]
    assert set(finished[-1]["metrics"]) == {"reference_count", "claim_count"}
