"""What a delegating parent learns while its mission child works.

The mission reports progress under ``agent.*``. A projection allowlist
that knows only the research vocabulary would hand a delegating kernel
run nothing but lifecycle events — queued, started, resumed, waiting,
completed — and nothing about the work itself, so the parent surface
could not say what was running.
"""

import json

from inqtrix.runs.shared import (
    OUTCOME_EVENT,
    build_child_progress_payload,
    should_project_child_event,
)


def _project(event_type: str, payload: dict) -> dict:
    return build_child_progress_payload(
        child_run_id="run_child",
        parent_task_id="call_abc",
        run_status="running",
        event_type=event_type,
        payload=payload,
        snapshot={},
        attempt=1,
    )


def test_mission_progress_events_reach_the_parent() -> None:
    """The regression: these three carry a mission's visible progress."""
    for event_type in (
        "inqtrix.agent.phase.changed",
        "inqtrix.agent.task.started",
        "inqtrix.agent.task.finished",
        "inqtrix.agent.task.failed",
    ):
        assert should_project_child_event(event_type), event_type


def test_a_task_start_names_the_unit_of_work() -> None:
    projected = _project(
        "inqtrix.agent.task.started",
        {"task_id": "t2", "ordinal": 1, "tool_kind": "web_research", "attempt": 1},
    )
    assert projected["ordinal"] == 1
    assert projected["tool_kind"] == "web_research"
    # The projection's task_id is the PARENT's, never the child's own.
    assert projected["task_id"] == "call_abc"


def test_a_finished_task_carries_its_outcome_but_not_its_report() -> None:
    """The child's executive summary must not cross the run boundary."""
    projected = _project(
        "inqtrix.agent.task.finished",
        {
            "task_id": "t1",
            "ordinal": 0,
            "tool_kind": "web_research",
            "status": "completed",
            "child_run_id": "run_grandchild",
            "result_summary": "## Executive Summary " + "x" * 4000,
            "metrics": {"reference_count": 53, "claim_count": 30},
        },
    )
    assert projected["status"] == "completed"
    assert projected["ordinal"] == 0
    assert projected["metrics"]["reference_count"] == 53
    assert "result_summary" not in projected
    # A GRANDCHILD id must never overwrite the child this row is about.
    assert projected["child_run_id"] == "run_child"


def test_a_phase_change_carries_the_childs_phase() -> None:
    projected = _project(
        "inqtrix.agent.phase.changed",
        {
            "phase": "execution",
            "previous_phase": "planning",
            "snapshot": {"current_node": "execution", "phase": "execution"},
        },
    )
    assert projected["snapshot"]["phase"] == "execution"
    assert projected["current_node"] == "execution"


def test_token_deltas_still_never_cross() -> None:
    """The 91%-of-all-events class stays out: this is a status feed, not
    a mirror of the child's stream."""
    assert not should_project_child_event("inqtrix.output_text.delta")


def test_a_failed_task_settles_on_the_parent_too() -> None:
    """A failed task crosses to the parent line like a finished one;
    otherwise it stays 'open' there for the rest of the mission."""
    projected = _project(
        "inqtrix.agent.task.failed",
        {
            "task_id": "t6",
            "ordinal": 5,
            "tool_kind": "web_research",
            "title": "Pruefe strittige Wochenmeldungen",
            "status": "failed",
            "error": "capability_unavailable",
            "result_summary": "NEVER",
        },
    )
    assert projected["event_type"] == "inqtrix.agent.task.failed"
    assert projected["ordinal"] == 5
    assert projected["status"] == "failed"
    assert projected["title"] == "Pruefe strittige Wochenmeldungen"
    assert "result_summary" not in projected


def test_a_task_title_crosses_text_capped() -> None:
    """The plan task's title names the unit of work on the parent line;
    the sanitizer's `title` cap (180) bounds it like every other title."""
    projected = _project(
        "inqtrix.agent.task.started",
        {"task_id": "t1", "ordinal": 0, "tool_kind": "web_research", "title": "T" * 300},
    )
    assert projected["title"].startswith("T" * 100)
    assert len(projected["title"]) < 200
    assert projected["title"].endswith("...")


def _projects(event_type: str, payload: dict) -> bool:
    return should_project_child_event(event_type, payload)


def test_only_a_tasks_searches_cross_as_activity() -> None:
    """Payload-gated: a provider retry (`scope="run"`) or a model-turn
    bracket must not flood the parent; a task's search must reach it."""
    assert _projects(
        "inqtrix.agent.activity",
        {"scope": "task", "operation": "web.search.instant", "status": "started"},
    )
    assert _projects(
        "inqtrix.agent.activity",
        {"scope": "task", "operation": "knowledge.search", "status": "completed"},
    )
    assert not _projects(
        "inqtrix.agent.activity", {"scope": "run", "operation": "agent.model.turn"}
    )
    assert not _projects(
        "inqtrix.agent.activity",
        {"scope": "task", "operation": "task.evidence_needs_review"},
    )
    # No payload -> fail closed for the gated families, unchanged otherwise.
    assert not should_project_child_event("inqtrix.agent.activity")
    assert not should_project_child_event("inqtrix.agent.narration")
    assert should_project_child_event("inqtrix.agent.phase.changed")


def test_a_search_crosses_with_its_query_but_not_its_purpose() -> None:
    projected = _project(
        "inqtrix.agent.activity",
        {
            "kind": "searching",
            "scope": "task",
            "phase": "execution",
            "operation": "web.search.instant",
            "detail": "Welche Modellankuendigungen gab es?",
            "status": "started",
            "current": 2,
            "total": 4,
            "task_id": "t3",
            "query": "Welche Modellankuendigungen gab es?",
            "purpose": "NEVER on the parent",
        },
    )
    assert projected["operation"] == "web.search.instant"
    assert projected["status"] == "started"
    assert projected["query"] == "Welche Modellankuendigungen gab es?"
    assert (projected["current"], projected["total"]) == (2, 4)
    # The child's task id crosses under its own name; `task_id` stays
    # the PARENT's tool-call id.
    assert projected["child_task_id"] == "t3"
    assert projected["task_id"] == "call_abc"
    assert "purpose" not in projected and "detail" not in projected


def test_a_long_query_is_text_capped_on_the_parent() -> None:
    """The sanitizer caps by key NAME (`query` 240): the projection must
    carry the query under that key, never as `detail`."""
    projected = _project(
        "inqtrix.agent.activity",
        {"scope": "task", "operation": "web.search.instant", "status": "started", "query": "q" * 300},
    )
    assert len(projected["query"]) < 260
    assert projected["query"].endswith("...")


def test_derived_narration_lines_cross_bounded_never_bulk_prose() -> None:
    """The mission's one-line intermediate results cross: synthesis
    (outline/section), task (title + result head), plan (summary head),
    discovery (counts). Each is bounded by the narration cap; any other
    kind stays in the child's stream."""
    for kind in ("synthesis", "task", "plan", "discovery"):
        assert _projects("inqtrix.agent.narration", {"kind": kind, "text": "x"}), kind
    for kind in ("intent", "memo", "evidence_bundle", "limit", "patch"):
        assert not _projects("inqtrix.agent.narration", {"kind": kind, "text": "x"}), kind
    projected = _project(
        "inqtrix.agent.narration",
        {"narration_id": "n-task-t3", "kind": "task", "text": "Modellprosa " * 400},
    )
    assert projected["kind"] == "task"
    assert projected["narration_id"] == "n-task-t3"
    assert len(projected["text"]) <= 410
    assert projected["text"].endswith("...")


def test_a_missions_decisions_cross_with_label_and_bounded_detail() -> None:
    """Why the mission keeps researching or stops: the decision kinds
    cross as run-scoped activity with kind, label and a detail cut visibly
    at the decision cap; other run-scoped kinds still do not. How the
    review ended crosses with the outcome record, never as a decision."""
    for kind in (
        "critic_unavailable",
        "critic_revise",
        "critic_research",
        "critic_research_exhausted",
        "replan_invalid",
    ):
        assert _projects("inqtrix.agent.activity", {"kind": kind, "scope": "run"}), kind
    for kind in (
        "memory", "searching", "working", "research_prospect",
        "replan_analysis_rejected", "replan_analysis_unchecked", "document_version_unchecked",
        "replan_context_incomplete",
    ):
        assert not _projects("inqtrix.agent.activity", {"kind": kind, "scope": "run"}), kind
    # A task's version check is no search, and a plan-time search is a
    # run-scoped row of the planning phase: neither crosses.
    assert not _projects(
        "inqtrix.agent.activity",
        {"kind": "document_version_unchecked", "scope": "task", "task_id": "t1"},
    )
    assert not _projects(
        "inqtrix.agent.activity",
        {"kind": "searching", "scope": "run", "phase": "planning", "operation": "knowledge.search"},
    )
    projected = _project(
        "inqtrix.agent.activity",
        {
            "kind": "critic_research",
            "label": "Critic fordert Recherche",
            "detail": "Das Memo braucht weitere Evidenz zu: " + "Gegenposition " * 40,
            "scope": "run",
        },
    )
    assert projected["kind"] == "critic_research"
    assert projected["label"] == "Critic fordert Recherche"
    assert len(projected["detail"]) <= 240
    assert projected["detail"].endswith("...")
    assert "operation" not in projected


def test_a_task_event_carries_the_childs_task_id() -> None:
    """The child's plan-task id crosses under its own name on task events
    (as it does on searches), so the parent can join a task's searches
    and its result line to the task row."""
    projected = _project(
        "inqtrix.agent.task.started",
        {"task_id": "t2", "ordinal": 1, "tool_kind": "web_research", "title": "Regulierung"},
    )
    assert projected["child_task_id"] == "t2"
    assert projected["task_id"] == "call_abc"
    projected = _project(
        "inqtrix.agent.narration",
        {"narration_id": "n-section-2", "kind": "synthesis", "text": "Abschnitt 'Markt' geschrieben.", "phase": "synthesis", "final": True},
    )
    assert projected["narration_id"] == "n-section-2"
    assert projected["kind"] == "synthesis"
    assert projected["text"] == "Abschnitt 'Markt' geschrieben."
    assert "final" not in projected


def test_a_whole_mission_projects_dozens_of_rows_never_thousands() -> None:
    """Order of magnitude pin: 8 tasks, 4 instant searches, 7 sections,
    2 failures — plus the noise that must NOT cross."""
    stream: list[tuple[str, dict]] = []
    stream += [("inqtrix.agent.phase.changed", {"phase": p}) for p in ("intake", "planning", "execution", "execution", "evidence", "synthesis", "critic", "critic", "done")]
    stream += [("inqtrix.agent.task.started", {"task_id": f"t{i}", "ordinal": i, "tool_kind": "web_research", "title": f"Aufgabe {i}"}) for i in range(8)]
    stream += [("inqtrix.agent.task.finished", {"task_id": f"t{i}", "ordinal": i, "status": "completed"}) for i in range(6)]
    stream += [("inqtrix.agent.task.failed", {"task_id": f"t{i}", "ordinal": i, "status": "failed"}) for i in (6, 7)]
    for i in range(4):
        stream += [("inqtrix.agent.activity", {"scope": "task", "operation": "web.search.instant", "status": s, "query": f"q{i}", "task_id": f"t{i}"}) for s in ("started", "completed")]
    stream += [("inqtrix.agent.narration", {"narration_id": "n-synthesis", "kind": "synthesis", "text": "Memo mit 7 Abschnitten."})]
    stream += [("inqtrix.agent.narration", {"narration_id": f"n-section-{i}", "kind": "synthesis", "text": f"Abschnitt {i} geschrieben."}) for i in range(7)]
    # Noise that stays in the child's own stream.
    stream += [("inqtrix.agent.activity", {"scope": "run", "operation": "agent.model.turn", "status": "started"})] * 40
    stream += [("inqtrix.output_text.delta", {"delta": "x"})] * 500
    stream += [("inqtrix.agent.child.progress", {"task_id": "grandchild"})] * 30
    # One result line per finished task and up to three decisions cross.
    stream += [("inqtrix.agent.narration", {"narration_id": f"n-task-t{i}", "kind": "task", "text": f"Aufgabe {i}: Ergebnis."}) for i in range(6)]
    stream += [("inqtrix.agent.activity", {"kind": "critic_research", "scope": "run", "label": "Critic fordert Recherche"})]
    stream += [("inqtrix.agent.narration", {"kind": "memo", "text": "Modellprosa " * 50})] * 8
    projected = [e for e, p in stream if should_project_child_event(e, p)]
    assert len(projected) == 9 + 8 + 6 + 2 + 8 + 8 + 6 + 1
    assert len(projected) < 80


def test_an_outcome_record_crosses_to_the_parent_without_its_lines_or_codes() -> None:
    """How a mission ended crosses as numbers and its summary: its reading
    lines, codes, its counted and marked statements, its details and any text
    riding along stay in the child's own stream."""
    payload = {
        "outcome_id": "run_child:outcome", "final": "completed", "review": "completed",
        "round": 1, "revision": "none", "accounting": "complete",
        "codes": ["web_not_needed"], "limits": [],
        "open": {"by_code": {"web_not_needed": 2}, "by_kind": {"information": 2}},
        "lines": [{"kind": "information", "text": "CANARY-LINE", "reasons": []}],
        "claims": {"art_memo": {"total": 1, "verified": 0, "source_text": 0, "attributed": 0, "insufficient": 1}},
        "marks": [{"output_id": "art_memo", "quote": "CANARY-QUOTE", "state": "insufficient",
                   "reason": "finding", "note": "CANARY-NOTE"}],
        "details": "CANARY-DETAILS",
        "label": "CANARY-LABEL", "detail": "CANARY-DETAIL",
    }
    projected = _project(OUTCOME_EVENT, payload)
    assert should_project_child_event(OUTCOME_EVENT, payload) is True
    assert "CANARY" not in json.dumps(projected)
    assert not {"codes", "lines", "claims", "marks", "details"} & set(projected)
    assert {key: projected[key] for key in ("final", "review", "round", "revision", "accounting")} == {
        "final": "completed", "review": "completed", "round": 1, "revision": "none",
        "accounting": "complete",
    }
    assert projected["open"] == {"by_code": {"web_not_needed": 2}, "by_kind": {"information": 2}}


def test_the_parent_learns_the_summary_as_a_bounded_label() -> None:
    """The record's summary is the one sentence that crosses, cut visibly at
    the decision bound; beside it only closed states and numbers."""
    summary = " · ".join(["Geprüft", *("2 Punkte nicht geprüft" for _ in range(20))])
    assert len(summary) > 300, "precondition: a summary longer than the bound"
    payload = {
        "outcome_id": "run_child:outcome", "final": "completed", "review": "completed",
        "round": 1, "revision": "unverified", "accounting": "complete", "applied": 2,
        "reach": {"reread": 3, "cited": 3}, "codes": ["extraction_limit"], "limits": [],
        "open": {"by_code": {}, "by_kind": {"not_checked": 2}},
        "lines": [{"kind": "limit", "text": "CANARY-LINE", "reasons": ["extraction_limit"]}],
        "summary": summary,
    }
    projected = _project(OUTCOME_EVENT, payload)
    identity = {"task_id", "child_run_id", "run_status", "event_type"}
    textual = sorted(
        key for key, value in projected.items()
        if isinstance(value, str) and key not in identity
    )
    # The cut at 240 characters, at the last word boundary that leaves
    # room for its mark: the whole line, mark included, is 240 long.
    bounded = "Geprüft" + " · 2 Punkte nicht geprüft" * 9 + " · 2 ..."
    assert len(bounded) == 240
    assert (
        textual, projected["summary"], projected["applied"],
        "lines" in projected, "codes" in projected,
    ) == (["accounting", "final", "review", "revision", "summary"], bounded, 2, False, False)


def test_a_research_child_hands_its_section_position_to_the_parent() -> None:
    """The writer's position crosses typed: the parent never parses the
    progress message ("Schreibe Report-Abschnitt 2/4 …") for it."""
    projected = _project(
        "inqtrix.progress.message",
        {
            "message": "Schreibe Report-Abschnitt 2/4: Kernaussagen",
            "snapshot": {
                "current_node": "answer",
                "total_queries": 6,
                "writing_section": 2,
                "sections_total": 4,
            },
        },
    )
    assert projected["snapshot"]["writing_section"] == 2
    assert projected["snapshot"]["sections_total"] == 4
    assert projected["snapshot"]["total_queries"] == 6
