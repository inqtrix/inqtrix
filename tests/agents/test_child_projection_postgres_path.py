"""The Postgres store's child projection reads the EVENT payload.

A gate that received the child run's request body (parent ids) in
place of the event payload would keep the payload-gated family — a role
child's settled tool calls, by their position — from crossing to the
parent on Postgres while the memory store (and every test) projected
them. Unit
pin without a database: a fake session answers the parent lookup, the
append is captured.
"""

from __future__ import annotations

import asyncio
from typing import Any

from inqtrix.runs.postgres_store import PostgresRunStore


class _Result:
    def __init__(self, row: tuple[str, str] | None) -> None:
        self._row = row

    def first(self) -> tuple[str, str] | None:
        return self._row


class _Session:
    async def execute(self, _statement: Any) -> _Result:
        return _Result(("waiting_for_children", "tenant-1"))


def _project(
    event_type: str, payload: dict[str, Any], *, work_profile: str = ""
) -> list[Any]:
    store = PostgresRunStore.__new__(PostgresRunStore)
    appended: list[Any] = []

    async def _capture(_session: Any, run_id: str, tenant_id: str, row: Any) -> None:
        appended.append((run_id, tenant_id, row))

    store._append_events_db = _capture  # type: ignore[method-assign]
    asyncio.run(
        store._project_child_progress_db(
            _Session(),
            child_run_id="run_child",
            kind="agent_child",
            parent_run_id="run_parent",
            request_payload={"body": {
                "parent_task_id": "call_1", "parent_task_attempt": 1,
                "work_profile": work_profile,
            }},
            run_status="running",
            event_type=event_type,
            payload=payload,
            snapshot={},
        )
    )
    return appended


def test_a_role_childs_first_tool_call_reaches_the_parent_on_postgres() -> None:
    appended = _project(
        "inqtrix.agent.tool.finished",
        {"tool": "read_evidence", "call_ordinal": 1, "status": "completed"},
        work_profile="review",
    )
    assert len(appended) == 1
    run_id, tenant_id, row = appended[0]
    assert (run_id, tenant_id) == ("run_parent", "tenant-1")
    # The appended row is the expanded child.progress event; its shape
    # belongs to the store — the pin is that the tool call is in it.
    text = str(row)
    assert "read_evidence" in text
    assert "child.progress" in text


def test_a_tool_call_without_a_role_stays_behind_on_postgres() -> None:
    assert _project(
        "inqtrix.agent.tool.finished",
        {"tool": "read_evidence", "call_ordinal": 1, "status": "completed"},
    ) == []


def test_a_run_scoped_activity_still_stays_behind_on_postgres() -> None:
    assert _project(
        "inqtrix.agent.activity",
        {"scope": "run", "operation": "agent.model.turn", "status": "started"},
    ) == []
