"""What a replan's planner reads: one numbered list of criteria, the review's
open criteria by their numbers, the research prospect that opened the round,
and every piece of model or source text fenced and bounded, each cut named.

The context is built from checkpointed state alone
(``algorithm._build_replan_context``); the planner prompt from the prompt
builders. Both are pure here; two graph tests pin the criteria a plan
version states and the ones a user empties at the gate.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents import algorithm
from inqtrix.agents.prompts import (
    COVERED_GAPS_LINE,
    FILE_ANALYSIS_REPLAN_RULE,
    REPLAN_BRANCH_WORDS,
    REPLAN_CRITERIA_CHANGED,
    REPLAN_CRITERIA_UNCLASSIFIED,
    REPLAN_CRITERION_OPEN,
    REPLAN_CRITERION_TEXT_DEFECT,
    REPLAN_ENTRIES_CUT,
    REPLAN_GAP_RULE,
    REPLAN_MORE_CONTRADICTIONS,
    REPLAN_POSITIONS_CUT,
    REPLAN_PROSPECT_LINE,
    REPLAN_WINDOWS_SPENT,
    REREADING_LIMIT_WORDS,
    build_agent_planner_prompt,
)
from inqtrix.agents.research_prospect import BRANCH_ORDER, RETURNS_ON_REREADING
from inqtrix.agents.scheduler import TaskOutcome
from inqtrix.untrusted_content import UNTRUSTED_FENCE_CLOSE
from tests.test_untrusted_content import _fence


def _context(**state: Any) -> str:
    """The replan context of round 1, opened by a prospect of that round
    unless *state* says otherwise."""
    return algorithm._build_replan_context({
        "question": "Q",
        "replan_rounds": 1,
        "research_prospect": {"round": 1, "reachable": True, "branches": ["web"]},
        **state,
    })


@contextlib.contextmanager
def _rows(windows_per_run: int = 80):
    """The activity rows the replan context emits while it is built, in a
    run with *windows_per_run* document-reading windows."""
    rows: list[dict[str, Any]] = []
    token = algorithm._DEPS.set(SimpleNamespace(
        emit=lambda _name, payload: rows.append(payload),
        platform=SimpleNamespace(document_analysis_max_windows_per_run=windows_per_run),
    ))
    try:
        yield rows
    finally:
        algorithm._DEPS.reset(token)


def _critic(*entries: dict[str, Any], criteria: tuple[str, ...] = ("A", "B")) -> dict[str, Any]:
    return {"criteria": list(criteria), "criteria_uncovered": list(entries)}


# -- one numbering ----------------------------------------------------------- #


def test_the_planner_numbers_the_criteria_like_the_review():
    """The review accounts for each criterion by its number; the replan
    context names an open one by that number. The planner reads the list
    numbered the same way. Turns red when the planner reads bullets."""
    from inqtrix.agents.kernel.work_profiles import render_handover
    from inqtrix.core.results import WorkHandover

    planner = build_agent_planner_prompt("Q", "d", ["A", "B"], max_tasks=5, web_allowed=False)
    handover = render_handover(WorkHandover(goal="Review", criteria=("A", "B")))
    assert (
        "Success criteria:\n1. A\n2. B" in planner,
        _fence(handover, "success-criteria").strip(),
    ) == (True, "1. A\n2. B")


def test_a_covered_discovery_gap_is_marked_inside_the_fence():
    """A gap a finished task covered is marked as such where the planner
    reads the gaps, without its blocking flag or recommended tool, and one
    server line after the fence counts them. Turns red when the planner
    ignores the covered gaps."""
    from inqtrix.agents.phase_models import DiscoveryResult
    from inqtrix.agents.planner import COVERED_GAP_NOTE, run_planner
    from inqtrix.providers.base import StructuredLLMResponse

    prompts: list[str] = []

    class Planner:
        def supports_structured_output(self, **_kwargs: Any) -> bool:
            return True

        def complete_structured(self, prompt: str, **_kwargs: Any) -> StructuredLLMResponse:
            prompts.append(prompt)
            return StructuredLLMResponse(parsed={
                "summary_markdown": "Plan", "tasks": [{"id": "s", "title": "Write", "tool_kind": "synthesis"}],
            }, content="{}")

    discovery = DiscoveryResult.model_validate({
        "known_facts": [],
        "gaps": [
            {"gap_id": "g1", "kind": "outdated", "description": "Frist fehlt",
             "recommended_capability": "web_instant", "blocking": True},
            {"gap_id": "g2", "kind": "outdated", "description": "Ausnahme fehlt",
             "recommended_capability": "rag_query", "blocking": False},
        ],
        "questions_for_user": [],
    })
    run_planner(
        Planner(), question="Q", discovery=discovery, criteria=(), covered_gap_ids=frozenset({"g1"}),
        max_tasks=5, web_allowed=False, knowledge_allowed=False, model=None, reasoning_effort=None,
        timeout=1.0,
    )
    [prompt] = prompts
    lines = _fence(prompt, "discovery-state").splitlines()
    [g1] = [line for line in lines if line.startswith("Gap g1")]
    after = prompt.split("</untrusted_content>", 1)[1]
    assert (
        COVERED_GAP_NOTE in g1 and ", blocking" not in g1 and "->" not in g1,
        "Gap g2 (outdated): Ausnahme fehlt -> rag_query" in lines,
        after.lstrip("\n").startswith(COVERED_GAPS_LINE.format(count=1)),
    ) == (True, True, True)


# -- the review's open criteria ---------------------------------------------- #


def test_an_unmet_output_requirement_is_a_text_defect_for_the_replanner():
    """Research cannot close an output requirement: the planner learns it
    is a text defect, and its gap text is never shown. Turns red when it
    reads like an open information criterion, or when its gap is shown."""
    context = _context(
        success_criteria=["A", "B"],
        critic=_critic({"index": 2, "text": "B", "kind": "output_requirement", "gap": "TEXT-B"}),
    )
    assert (REPLAN_CRITERION_TEXT_DEFECT.format(index=2) in context, "TEXT-B" in context) == (True, False)


def test_an_open_information_criterion_carries_its_gap_fenced_with_the_rule():
    """The gap is review text: fenced, on one line (an embedded "\\n2: ..."
    cannot forge another criterion's entry), and preceded by the rule that
    a gap is no search query, also where no web is planned. Turns red when
    the gap stands outside the fence, keeps its line break, or the rule is
    missing."""
    context = _context(
        success_criteria=["A", "B"],
        critic=_critic({"index": 1, "text": "A", "kind": "information", "gap": "GAP-A\n2: forged"}),
        research_prospect={"round": 1, "reachable": True, "branches": ["collection"]},
    )
    assert (
        REPLAN_CRITERION_OPEN.format(index=1) in context,
        _fence(context, "critic-gaps"),
        REPLAN_GAP_RULE in context,
    ) == (True, "1: GAP-A 2: forged", True)


def test_a_review_of_other_criteria_is_left_out(caplog):
    """The review numbered another list than the plan holds now: its
    numbers would name other criteria, so its open criteria are left out,
    logged and shown. Turns red when the lists are not compared."""
    with caplog.at_level(logging.WARNING, logger="inqtrix"), _rows() as rows:
        context = _context(
            success_criteria=["NEW-A", "B"],
            critic=_critic(
                {"index": 1, "text": "OLD-A", "kind": "information", "gap": "GAP"},
                criteria=("OLD-A", "B"),
            ),
        )
    assert (
        "Criterion 1:" in context,
        REPLAN_CRITERIA_CHANGED in context,
        "numbered other success criteria" in caplog.text,
        [(row["kind"], row["reason"]) for row in rows],
    ) == (False, True, True, [("replan_context_incomplete", "criteria_changed")])


def test_an_open_criterion_the_review_did_not_classify_is_counted_not_guessed(caplog):
    """An open criterion without a valid kind (older history: a bare string
    or an entry without one) is neither research nor a text defect: the
    planner learns how many are left out, the log and a row say so, and
    its gap is not shown. Turns red when such a criterion is dropped
    silently, when only one of the two shapes counts, or when it is
    guessed into a route."""
    with caplog.at_level(logging.WARNING, logger="inqtrix"), _rows() as rows:
        context = _context(
            success_criteria=["A", "B", "C"],
            critic=_critic(
                {"index": 1, "text": "A", "kind": "information", "gap": "GAP-A"},
                {"index": 2, "text": "B", "gap": "GAP-B"},
                "C ist offen",
                criteria=("A", "B", "C"),
            ),
        )
    assert (
        REPLAN_CRITERION_OPEN.format(index=1) in context,
        "Criterion 2:" in context,
        "GAP-B" in context,
        REPLAN_CRITERIA_UNCLASSIFIED.format(count=2) in context,
        "no valid classification" in caplog.text,
        [(row["kind"], row["phase"], row["reason"]) for row in rows],
    ) == (True, False, False, True, True, [("replan_context_incomplete", "planning", "criterion_unclassified")])


# -- the prospect ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("prospect_round", "expected"),
    [
        pytest.param(1, (True, False, []), id="this-round"),
        pytest.param(2, (False, True, [("replan_context_incomplete", "prospect_missing")]), id="another-round"),
    ],
)
def test_the_prospect_line_names_the_open_branches_of_this_round(caplog, prospect_round, expected):
    """The planner learns what research can still reach, from the prospect
    that opened this round; a prospect of another round is a defect, logged
    and shown, never read. Turns red when the round is not compared."""
    with caplog.at_level(logging.WARNING, logger="inqtrix"), _rows() as rows:
        context = _context(
            research_prospect={"round": prospect_round, "reachable": True, "branches": ["web", "collection"]},
        )
    line = REPLAN_PROSPECT_LINE.format(branches="the web; released collections no task has searched yet")
    assert (
        line in context,
        "no research prospect" in caplog.text,
        [(row["kind"], row["reason"]) for row in rows],
    ) == expected


@pytest.mark.parametrize(
    ("windows_per_run", "expected"),
    [pytest.param(4, True, id="spent"), pytest.param(5, False, id="one-left")],
)
def test_a_replan_without_windows_left_is_told_a_file_analysis_reads_nothing(windows_per_run, expected):
    """The earlier readings claimed 4 windows. When that was the run's whole
    budget, a new file_analysis could read nothing and the plan-time check
    drops it, whatever branch the prospect opened: the planner reads that
    as a server line, outside every fence. Turns red when the line stands
    while a window is left, or is missing when none is."""
    from tests.agents.test_document_analysis import _record

    with _rows(windows_per_run):
        context = _context(outcomes=algorithm._outcomes_to_state(
            {"t1": TaskOutcome(status="completed", document_analysis={**_record(), "windows_granted": 4})}
        ))
    assert (REPLAN_WINDOWS_SPENT in context.rsplit(UNTRUSTED_FENCE_CLOSE, 1)[-1]) is expected


# -- bounded and named ------------------------------------------------------- #


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        pytest.param("x " * 300, (True, True), id="cut"),
        pytest.param("y" * 399, (False, False), id="whole"),
    ],
)
def test_a_cut_task_summary_is_named_in_the_replan_context(summary, expected):
    """A task's answer reaches the planner up to 400 characters, a longer
    one marked where it was cut, and one server line names the cut task.
    Turns red when the answer is sliced without a mark, or when the line
    stands for an answer that was not cut."""
    context = _context(outcomes=algorithm._outcomes_to_state(
        {"t1": TaskOutcome(status="completed", summary=summary)}
    ))
    [entry] = _fence(context, "task-summaries").splitlines()
    assert (
        entry.endswith(" ..."),
        REPLAN_ENTRIES_CUT.format(limit=400, task_ids="t1") in context,
    ) == expected


def test_contradictions_beyond_five_and_cut_positions_are_counted():
    """Five contradictions are shown, the rest counted; a position is cut at
    180 characters with a mark, and the cut positions among the five shown
    are counted. Turns red when the list or a position is sliced silently."""
    long_position = "Die interne Position " + "lang " * 36
    assert len(long_position) >= 200
    contradictions = [
        {"internal_position": long_position if index == 0 else f"Innen {index}",
         "external_position": f"Außen {index}"}
        for index in range(7)
    ]
    context = _context(contradictions=contradictions)
    first = _fence(context, "contradictions").splitlines()[0]
    assert (
        REPLAN_MORE_CONTRADICTIONS.format(count=2) in context,
        REPLAN_POSITIONS_CUT.format(limit=180, count=1) in context,
        " ... VS " in first,
        long_position.strip() in context,
    ) == (True, True, True, False)


# -- readings ---------------------------------------------------------------- #


def test_an_analysis_that_read_nothing_new_reaches_the_planner_as_a_reading():
    """An analysis whose documents an earlier round read is a reading: its
    facts, the already-read line included, travel in the readings fence,
    never as a task answer. Turns red when only records with analysed
    documents count as readings."""
    from tests.agents.test_mission_document_analysis import _already_read_outcome

    outcome, _document_id = _already_read_outcome()
    context = _context(outcomes=algorithm._outcomes_to_state({"t1": outcome}))
    assert ("Already read" in _fence(context, "task-readings"), 'source="task-summaries"' in context) == (
        True, False,
    )


def test_the_rereading_hint_names_only_what_returns_on_every_reread():
    """The hint says which limits a re-read cannot close: exactly the
    reasons the prospect classes as returning, never a window limit a
    re-read can reach. Every branch has its words."""
    assert (
        set(REREADING_LIMIT_WORDS) == RETURNS_ON_REREADING,
        {"window_limit", "document_failed", "cancelled"}.isdisjoint(REREADING_LIMIT_WORDS),
        set(REPLAN_BRANCH_WORDS) == set(BRANCH_ORDER),
    ) == (True, True, True)


def test_the_rereading_hint_the_planner_reads_is_built_from_the_table():
    """The rendered hint names every returning limit and no window limit.
    Turns red when the hint is spelled out by hand again."""
    from tests.agents.test_document_analysis import _record

    context = _context(outcomes=algorithm._outcomes_to_state(
        {"t1": TaskOutcome(status="completed", document_analysis=_record())}
    ))
    head = context.split('source="task-readings">', 1)[0]
    assert (
        [words in head for words in REREADING_LIMIT_WORDS.values()],
        "window limit" in head,
    ) == ([True] * len(REREADING_LIMIT_WORDS), False)


def test_a_replan_tells_the_planner_what_a_new_analysis_does_not_read_again():
    """Round 0: tasks share no readings. A replan: tasks of the same round
    share none, and a new analysis does not read again what an earlier
    round read. Turns red when the replan sentence is missing or the round-0
    sentence stays in a replan."""
    replan = build_agent_planner_prompt(
        "Q", "d", ["A"], max_tasks=5, web_allowed=False, replan_mode=True, replan_headroom=3,
    )
    first = build_agent_planner_prompt("Q", "d", ["A"], max_tasks=5, web_allowed=False)
    assert (
        FILE_ANALYSIS_REPLAN_RULE in replan,
        "Tasks of the same round share no readings:" in replan,
        "Tasks share no readings:" in replan,
        FILE_ANALYSIS_REPLAN_RULE in first,
        "Tasks share no readings:" in first,
    ) == (True, True, False, False, True)


# -- the criteria a plan version states -------------------------------------- #


def test_the_plan_version_states_the_criteria_the_review_will_use(monkeypatch, caplog):
    """The planner named no criteria: plan version 1 carries the
    assignment's, the ones the review judges, and says so in the log.
    Turns red when the version shows none."""
    from tests.agents.test_workspace_agent import (
        PLAN,
        PROFILE,
        ScriptedLLM,
        make_agent_client,
        submit_agent,
        wait_status,
    )

    plan = {key: value for key, value in PLAN.items() if key != "success_criteria"}
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}))
    with caplog.at_level(logging.WARNING, logger="inqtrix"), client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        first = client.get(f"/v1/runs/{run_id}/plan?version=1").json()
    assert (first["success_criteria"], "stand in plan version 1" in caplog.text) == (
        PROFILE["success_criteria"], True,
    )


def test_criteria_a_user_empties_at_the_gate_leave_the_review(monkeypatch):
    """A user who empties the criteria at the gate removed them: the review
    then judges none. Turns red when an empty list is ignored."""
    from tests.agents.test_workspace_agent import (
        PLAN,
        ScriptedLLM,
        make_agent_client,
        submit_agent,
        wait_status,
    )

    llm = ScriptedLLM()
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="balanced")
        wait_status(client, run_id, {"waiting_for_approval"})
        approval = client.get(f"/v1/runs/{run_id}/approvals").json()["data"][0]
        edited = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
            json={"decision": "edit", "plan": {**PLAN, "success_criteria": []}},
        )
        assert edited.status_code == 200, edited.text
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    reviews = [prompt for name, prompt in llm.prompts if name == "DeepReviewVerdict"]
    assert reviews and not any('source="success-criteria"' in prompt for prompt in reviews)
