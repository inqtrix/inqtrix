"""The deterministic replan gate + the autonomy auto-approve policy.

Two pure decision functions — no LLM, no IO — that gate the replan loop and
the HITL interrupt. Table-driven so a change to any branch (or the balanced
<= 2 read-only threshold, which has security-relevant consequences for
auto-approving agent-initiated work) goes red.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.agents.replan import autonomy_auto_approves, evaluate_replan, has_unresolved_outcome
from inqtrix.agents.scheduler import TaskOutcome


def _outcomes(*statuses: str) -> dict[str, TaskOutcome]:
    return {f"t{i}": TaskOutcome(status=status) for i, status in enumerate(statuses)}


@pytest.mark.parametrize(
    "outcomes, blocking, coverage, used, cap, expected",
    [
        # Rounds exhausted wins over everything, even a blocking gap.
        (_outcomes("failed"), True, "uncovered", 2, 2, False),
        # Advisory gap signals cannot reopen a clean successful execution.
        (_outcomes("completed"), True, "covered", 0, 3, False),
        (_outcomes("completed"), False, "uncovered", 0, 3, False),
        # A blocking gap plus a real insufficient task forces a replan.
        (_outcomes("insufficient_evidence"), True, "covered", 0, 3, True),
        # An unresolved task plus uncovered sufficiency triggers a replan.
        (_outcomes("insufficient_evidence"), False, "uncovered", 0, 3, True),
        # A failure with only partial coverage triggers a replan.
        (_outcomes("failed", "completed"), False, "partial", 0, 3, True),
        # A failure but full coverage proceeds-and-marks (no replan).
        (_outcomes("failed"), False, "covered", 0, 3, False),
        # Partial coverage but no failure proceeds.
        (_outcomes("completed"), False, "partial", 0, 3, False),
        # Clean run, rounds remain: no replan.
        (_outcomes("completed"), False, "covered", 0, 3, False),
        (_outcomes("completed", "skipped"), True, "uncovered", 0, 3, False),
        ({}, True, "uncovered", 0, 3, False),
    ],
)
def test_evaluate_replan_truth_table(
    outcomes, blocking, coverage, used, cap, expected
):
    assert (
        evaluate_replan(
            outcomes=outcomes,
            blocking_gap_uncovered=blocking,
            sufficiency_coverage=coverage,
            replan_rounds_used=used,
            max_replan_rounds=cap,
        )
        is expected
    )


@pytest.mark.parametrize("statuses, expected", [
    ((), False), (("completed",), False), (("completed", "skipped"), False),
    (("completed", "failed"), True), (("insufficient_evidence",), True),
])
def test_unresolved_outcome_uses_the_replan_failure_contract(statuses, expected):
    assert has_unresolved_outcome(_outcomes(*statuses)) is expected


def _task(tool_kind: str):
    # autonomy_auto_approves reads only tool_kind off each task.
    return SimpleNamespace(tool_kind=tool_kind)


@pytest.mark.parametrize(
    "autonomy, tasks, expected",
    [
        # Autonomous approves always — even 3 non-read-only deltas.
        (
            "autonomous",
            [_task("editor_patch"), _task("web_research"), _task("web_research")],
            True,
        ),
        # Strict never approves — even a single internal read-only delta.
        ("strict", [_task("rag_query")], False),
        # Balanced: no new tasks is <= 2 with a vacuous all() -> approve.
        ("balanced", [], True),
        # Balanced: two INTERNAL searches -> approve.
        ("balanced", [_task("rag_query"), _task("rag_query")], True),
        # A file analysis reads whole documents (one model call per
        # window): a replan that adds one shows its plan.
        ("balanced", [_task("file_analysis")], False),
        # A replan that adds WEB tasks always re-gates in Standard: the
        # approved plan is the web consent, so new queries must be seen
        # before they run.
        ("balanced", [_task("web_research")], False),
        ("balanced", [_task("web_instant"), _task("rag_query")], False),
        # Balanced: three internal tasks exceeds the <= 2 threshold.
        (
            "balanced",
            [_task("rag_query"), _task("rag_query"), _task("rag_query")],
            False,
        ),
        # Balanced: any non-read-only task blocks auto-approve.
        ("balanced", [_task("rag_query"), _task("editor_patch")], False),
    ],
)
def test_autonomy_auto_approves_policy(autonomy, tasks, expected):
    assert (
        autonomy_auto_approves(autonomy=autonomy, new_tasks=tasks) is expected
    )
