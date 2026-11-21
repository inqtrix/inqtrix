"""Runs of the retired mission engine stay readable.

A mission run's event history, its result and its decided approvals are
stored data. The read routes serve them as they were stored, also when no
code writes those forms any more: each test writes the stored form through
the store's own write path, or puts it into the store as stored data where
no write path accepts it, and reads it back through the route a client uses.
The event types are spelled out here; they are values of stored rows.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inqtrix.agents.control_ports import ApprovalRecord
from tests.test_runs_sharing import OWNER, make_sharing_client, scoped, user_headers

MISSION_HISTORY: list[tuple[str, dict[str, Any]]] = [
    ("inqtrix.agent.plan.proposed", {"plan_id": "plan_v1", "version": 1, "task_count": 3}),
    ("inqtrix.agent.task.started", {
        "task_id": "t1", "ordinal": 0, "title": "Marktlage prüfen",
        "tool_kind": "web_research", "attempt": 1,
    }),
    ("inqtrix.agent.task.finished", {
        "task_id": "t1", "status": "completed",
        "result_summary": "Zwei Quellen bestätigen die Lage.",
    }),
    ("inqtrix.agent.task.failed", {
        "task_id": "t2", "failure_code": "task_failed",
        "failure_reason": "Die Sammlung war nicht erreichbar.",
    }),
    ("inqtrix.agent.task.cancel_requested", {"task_id": "t3"}),
    ("inqtrix.agent.plan.revised", {"plan_id": "plan_v2", "version": 2, "reason": "critic_research"}),
    ("inqtrix.agent.patch.proposed", {"patch_id": "pat_1", "document_id": "doc_1"}),
    ("inqtrix.agent.artifact.edit_conflict", {
        "artifact_id": "art_memo", "expected_revision": 2, "current_revision": 3,
    }),
]
"""One event of each type only a mission run wrote, in an order a mission
wrote them."""

STORED_MISSION_RESULT: dict[str, Any] = {
    "answer": "Die Frist beträgt 30 Tage [W1].",
    "metrics": {"confidence": 7, "evidence_contract_status": "clean"},
    "top_sources": [],
    "references": [{
        "label": "W1", "url": "https://example.org/frist", "title": "Fristen",
        "excerpt": "Die Frist beträgt 30 Tage.",
    }],
    "top_claims": [],
    "execution": {
        "effective_mode": "workspace_agent",
        "response_form": "canvas",
        "depth": "deep",
        "model": "test-model",
        "reasoning_effort": "medium",
        "source_policy": {"web": "available", "knowledge": "disabled"},
        "knowledge_scope": None,
        "knowledge_collection_count": 0,
        "consent_reason": "permission_policy",
        "tool_use_counts": {"web": 1, "knowledge": 0},
        "web_searches": None,
        "knowledge_searches": None,
        "limits": {},
        "tool_grants": [],
        "source_coverage": [],
        "document_analyses": [],
        "work_profile": "",
    },
}
"""A mission run's result in the form the run stored it."""


def _stored_run(
    client: TestClient,
    *,
    mode: str,
    events: Sequence[tuple[str, dict[str, Any]]] = (),
    result: dict[str, Any] | None = None,
) -> str:
    """A completed agent run of OWNER that wrote *events* and stored *result*."""

    def work(handle: Any) -> None:
        for event_type, payload in events:
            handle.emit(event_type, dict(payload))
        handle.complete(dict(result or {"answer": "fertig"}))

    store = client.run_store
    run_id = store.submit(
        mode=mode,
        workflow_contract=1,
        question="Welche Fristen gelten?",
        stack_name="default",
        work=work,
        kind="agent",
        created_by_user_id=OWNER,
        created_by_tenant_id="default",
    )["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if store.get(run_id, visible_to=scoped(OWNER))["status"] == "completed":
            return run_id
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not complete")


def _read_events(client: TestClient, run_id: str) -> list[list[dict[str, Any]]]:
    """The run's history as the polling read and as the stream serve it."""
    polled = client.get(f"/v1/runs/{run_id}/events?format=json", headers=user_headers(OWNER))
    streamed = client.get(f"/v1/runs/{run_id}/events", headers=user_headers(OWNER))
    assert (polled.status_code, streamed.status_code) == (200, 200)
    frames = [
        json.loads(line[len("data: "):])
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    return [polled.json()["data"], frames]


def _written(
    events: list[dict[str, Any]], history: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, dict[str, Any]]]:
    """The events of *events* whose type *history* wrote, in their order."""
    types = {event_type for event_type, _payload in history}
    return [(event["type"], event["data"]) for event in events if event["type"] in types]


def test_a_stored_mission_history_reads_back_whole_and_in_order(monkeypatch):
    """Every type only a mission wrote comes back, with its payload and in
    the order it was written, from the polling read and from the stream.
    The same holds for the progress row a kernel parent stored for a
    mission child of its tree."""
    client = make_sharing_client(monkeypatch)
    with client:
        mission = _stored_run(client, mode="workspace_agent", events=MISSION_HISTORY)
        child_progress = [("inqtrix.agent.child.progress", {
            "task_id": "call_mission", "child_run_id": mission, "run_status": "running",
            "event_type": "inqtrix.agent.task.finished", "updated_at": 1767225600.0,
            "ordinal": 0, "tool_kind": "web_research", "status": "completed",
            "title": "Marktlage prüfen", "child_task_id": "t1",
        })]
        parent = _stored_run(client, mode="agent_kernel", events=child_progress)

        mission_reads = _read_events(client, mission)
        parent_reads = _read_events(client, parent)

    assert [_written(events, MISSION_HISTORY) for events in mission_reads] == [MISSION_HISTORY] * 2
    assert [_written(events, child_progress) for events in parent_reads] == [child_progress] * 2


def test_a_stored_mission_result_reads_back_as_stored(monkeypatch):
    """The result of a run whose mode no longer starts runs is served as it
    was stored: nothing on the read path validates or narrows its mode."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _stored_run(client, mode="workspace_agent", result=STORED_MISSION_RESULT)
        response = client.get(f"/v1/runs/{run_id}/result", headers=user_headers(OWNER))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["run_id"], body["status"]) == (run_id, "completed")
    assert {key: body.get(key) for key in STORED_MISSION_RESULT} == STORED_MISSION_RESULT


def test_a_stored_mission_memo_lists_reads_and_exports_as_written(monkeypatch):
    """The memo a mission run wrote stays its result to take away: it is
    listed with the run, reads back whole, and exports into a new editor
    document that names its source and the run it came from."""
    memo = "# Fristen\n\nDie Frist beträgt 30 Tage [W1].\n"
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _stored_run(client, mode="workspace_agent", result=STORED_MISSION_RESULT)
        asyncio.run(client.agent_control.store.upsert_artifact(
            run_id=run_id, kind="memo", session_id="sess-1", title="Memo", status="ready",
            content_markdown=memo, payload={}, refs=[{"label": "W1", "url": "https://example.org/frist"}],
            updated_by="agent", artifact_id="art_memo",
        ))
        listing = client.get(f"/v1/runs/{run_id}/artifacts", headers=user_headers(OWNER))
        detail = client.get(f"/v1/runs/{run_id}/artifacts/art_memo", headers=user_headers(OWNER))
        exported = client.post(
            f"/v1/runs/{run_id}/artifacts/art_memo/export", json={}, headers=user_headers(OWNER)
        )

    assert (listing.status_code, detail.status_code, exported.status_code) == (200, 200, 201)
    assert [item["artifact_id"] for item in listing.json()["data"]] == ["art_memo"]
    assert detail.json()["content_markdown"] == memo
    assert (exported.json()["source"], exported.json()["source_run_id"]) == ("agent-artifact", run_id)


def test_a_stored_plan_decision_reads_back_without_its_composed_rules(monkeypatch):
    """A decided plan approval of a mission run is listed with its decision.

    The row is put into the store as stored data: the read must serve every
    stored kind, whichever kinds the write path still accepts. Its decision
    payload reads as the parts the user chose; the composed rule text it
    stored for the mission's resume is never handed out.
    """
    parts = {
        "free_text": "Bitte knapp.",
        "rules": [{"template_id": "pt_knapp", "label": "knapp", "revision": 1}],
    }
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _stored_run(client, mode="workspace_agent")
        store = client.agent_control.store
        stored = ApprovalRecord(
            approval_id="apr_plan_v1",
            run_id=run_id,
            kind="plan",
            status="approved",
            subject_type="plan",
            subject_id="plan_v1",
            payload={"plan_id": "plan_v1", "plan_version": 1},
            decision="approve",
            decision_payload={
                "report_guidance": "[Rule: knapp]\nSchreibe knapp und belege jede Zahl.",
                "report_requirement": parts,
            },
            note="passt",
            decided_by_user_id=OWNER,
            created_at=1767225600.0,
            decided_at=1767225660.0,
        )
        with store._lock:
            store._approvals[stored.approval_id] = stored
        listing = client.get(f"/v1/runs/{run_id}/approvals", headers=user_headers(OWNER))

    assert listing.status_code == 200, listing.text
    (row,) = listing.json()["data"]
    assert (
        row["approval_id"], row["kind"], row["status"], row["subject_id"],
        row["decision"], row["note"], row["decision_payload"],
    ) == ("apr_plan_v1", "plan", "approved", "plan_v1", "approve", "passt", {"report_requirement": parts})


@pytest.mark.parametrize(
    ("kind", "status", "decision"),
    [
        ("plan", "pending", "approve"),
        ("replan", "pending", "edit"),
        ("discovery", "pending", "reject"),
        ("patch", "pending", "approve"),
        # The decision it stored, sent again: refused before a replay answers.
        ("plan", "rejected", "reject"),
    ],
)
def test_a_decision_on_a_stored_mission_approval_is_refused_as_retired(
    monkeypatch, kind, status, decision
):
    """Only the kernel's tool gate is decided. A stored approval of another
    kind answers 400 with the reason ``workflow_retired`` and stays listed as
    it was stored."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _stored_run(client, mode="workspace_agent")
        store = client.agent_control.store
        stored = ApprovalRecord(
            approval_id="apr_mission",
            run_id=run_id,
            kind=kind,
            status=status,
            decision="" if status == "pending" else decision,
            created_at=1767225600.0,
            decided_at=None if status == "pending" else 1767225660.0,
        )
        with store._lock:
            store._approvals[stored.approval_id] = stored
        response = client.post(
            f"/v1/runs/{run_id}/approvals/apr_mission",
            json={"decision": decision},
            headers=user_headers(OWNER),
        )
        listing = client.get(f"/v1/runs/{run_id}/approvals", headers=user_headers(OWNER))

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert (error["type"], error["reason"]) == ("invalid_request_error", "workflow_retired")
    assert error["message"] == (
        f"An approval of kind {kind} belongs to the retired mission workflow and "
        "can no longer be decided."
    )
    (row,) = listing.json()["data"]
    assert (row["approval_id"], row["kind"], row["status"], row["decision"]) == (
        "apr_mission", kind, status, stored.decision,
    )
