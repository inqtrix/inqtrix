"""What a delegating kernel parent learns while its child works.

A research child reports its lifecycle and its nodes, a kernel role child its
phase and its settled tool calls. Everything else stays in the child's own
stream: the parent's feed is a status feed, not a mirror of the child.
"""

from inqtrix.runs.shared import build_child_progress_payload, should_project_child_event


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


def test_the_lifecycle_nodes_and_a_role_childs_phase_reach_the_parent() -> None:
    for event_type in (
        "inqtrix.run.started",
        "inqtrix.node.started",
        "inqtrix.node.finished",
        "inqtrix.agent.phase.changed",
    ):
        assert should_project_child_event(event_type), event_type


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


def test_a_childs_activity_narration_and_outcome_stay_in_its_own_stream() -> None:
    """No child sends a search row, a narration line or an outcome record
    its parent shows: the kernel tells its children's results on its own
    stream, and a role run sends no outcome record."""
    search = {
        "scope": "task",
        "operation": "web.search.instant",
        "status": "started",
        "query": "EU AI Act Fristen",
    }
    assert not should_project_child_event("inqtrix.agent.activity", search)
    narration = {"narration_id": "n1", "kind": "task", "text": "Fertig."}
    assert not should_project_child_event("inqtrix.agent.narration", narration)
    assert not should_project_child_event("inqtrix.agent.outcome", {"final": "completed"})


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
