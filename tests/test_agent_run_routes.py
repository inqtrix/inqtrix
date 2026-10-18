"""HTTP tests for the agent control routes (/v1/runs/{id}/plan|approvals|
clarifications|artifacts) and the agent-sessions clone.

Uses the oidc-header sharing harness from ``tests/test_runs_sharing.py``:
ownership and share semantics need real principals, which the contract app
(auth mode ``none``) cannot provide.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable

import pytest

from inqtrix.agents.control_ports import (
    ApprovalRecord,
    ClarificationRecord,
    PlanRecord,
    PlanTaskRecord,
)
from inqtrix.agents.scheduler import TaskOutcome, task_result_payload
from inqtrix.services.agent_sessions_service import AgentSessionsService
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    STRANGER,
    SUB_HEADER,
    accept_via_http,
    grant_via_http,
    make_sharing_client,
    scoped,
)

OWNER_USER_ID = OWNER
RECIPIENT_USER_ID = RECIPIENT
STRANGER_USER_ID = STRANGER
OWNER = str(OWNER_USER_ID)
RECIPIENT = str(RECIPIENT_USER_ID)
STRANGER = str(STRANGER_USER_ID)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


def _parked_agent_run(client, *, sub: str = OWNER) -> str:
    store = client.run_store
    calls = {"count": 0}

    def segmented(handle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        handle.complete({"answer": "fertig"})

    summary = store.submit(
        question="Agentenauftrag",
        stack_name="default",
        work=segmented,
        kind="agent",
        created_by_user_id=uuid.UUID(sub),
        created_by_tenant_id="default",
    )
    run_id = summary["run_id"]
    _wait_until(
        lambda: store.get(
            run_id, visible_to=scoped(uuid.UUID(sub))
        )["status"] == "waiting_for_approval"
    )
    return run_id


def _create_approval(client, run_id: str) -> str:
    approval = asyncio.run(
        client.agent_control.store.create_approval(
            ApprovalRecord(
                approval_id=f"apr_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                kind="plan",
            )
        )
    )
    return approval.approval_id


def _plan_body() -> dict[str, Any]:
    return {
        "summary_markdown": "Ueberarbeiteter Plan",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Suche",
                "tool_kind": "rag_query",
                "queries": ["Welche internen Berichte sind relevant?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }


def test_plan_route_serves_versions_and_404s(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)

        empty = client.get(
            f"/v1/runs/{run_id}/plan", headers={SUB_HEADER: OWNER}
        )
        assert empty.status_code == 404
        assert empty.json()["error"]["message"] == "No plan available yet"

        approval_id = _create_approval(client, run_id)
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "edit", "plan": _plan_body()},
            headers={SUB_HEADER: OWNER},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "edited"
        assert decided.json()["run"]["status"] in {
            "queued",
            "running",
            "completed",
        }

        plan = client.get(
            f"/v1/runs/{run_id}/plan", headers={SUB_HEADER: OWNER}
        ).json()
        assert plan["version"] == 1
        assert plan["created_by"] == "user"
        assert [task["task_id"] for task in plan["tasks"]] == ["t1", "s"]
        assert len(plan["versions"]) == 1

        bad_version = client.get(
            f"/v1/runs/{run_id}/plan?version=abc",
            headers={SUB_HEADER: OWNER},
        )
        assert bad_version.status_code == 400
        missing_version = client.get(
            f"/v1/runs/{run_id}/plan?version=9",
            headers={SUB_HEADER: OWNER},
        )
        assert missing_version.status_code == 404

        stranger = client.get(
            f"/v1/runs/{run_id}/plan", headers={SUB_HEADER: STRANGER}
        )
        assert stranger.status_code == 404


def test_task_result_route_loads_complete_markdown_lazily(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        store = client.agent_control.store
        plan = PlanRecord(
            plan_id="plan-full-result",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        )
        task = PlanTaskRecord(
            task_id="task-full-result",
            plan_id=plan.plan_id,
            run_id=run_id,
            ordinal=0,
            title="Complete result",
            tool_kind="web_instant",
        )
        asyncio.run(store.save_plan(run_id=run_id, plan=plan, tasks=[task]))
        asyncio.run(
            store.transition_plan_task(
                run_id=run_id,
                plan_id=plan.plan_id,
                task_id=task.task_id,
                status="running",
            )
        )
        complete_markdown = "# Full result\n\n" + "evidence " * 500
        outcome = TaskOutcome(
            status="completed",
            summary="Compact overview.",
            answer_markdown=complete_markdown,
            evidence=[
                {
                    "label": "W1",
                    "url": "https://example.test/report",
                    "title": "Report",
                    "grounded_support": "Supported statement.",
                }
            ],
        )
        asyncio.run(
            store.transition_plan_task(
                run_id=run_id,
                plan_id=plan.plan_id,
                task_id=task.task_id,
                status="completed",
                result_summary=outcome.summary,
                result_payload=task_result_payload(
                    outcome, persisted_summary=outcome.summary
                ),
            )
        )

        overview = client.get(
            f"/v1/runs/{run_id}/plan", headers={SUB_HEADER: OWNER}
        ).json()
        assert "answer_markdown" not in overview["tasks"][0]

        response = client.get(
            f"/v1/runs/{run_id}/tasks/{task.task_id}/result",
            headers={SUB_HEADER: OWNER},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["answer_markdown"] == complete_markdown
        assert payload["result_summary"] == "Compact overview."
        assert payload["references"][0]["grounded_support"] == (
            "Supported statement."
        )
        assert payload["legacy_summary_only"] is False

        stranger = client.get(
            f"/v1/runs/{run_id}/tasks/{task.task_id}/result",
            headers={SUB_HEADER: STRANGER},
        )
        assert stranger.status_code == 404


def test_task_cancel_route_preserves_run_and_is_idempotent(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        store = client.agent_control.store
        plan = PlanRecord(
            plan_id="plan-task-cancel-route",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        )
        tasks = [
            PlanTaskRecord(
                task_id="pending",
                plan_id=plan.plan_id,
                run_id=run_id,
                ordinal=0,
                title="Pending search",
                tool_kind="web_instant",
            ),
            PlanTaskRecord(
                task_id="s",
                plan_id=plan.plan_id,
                run_id=run_id,
                ordinal=1,
                title="Synthesis",
                tool_kind="synthesis",
                depends_on=("pending",),
            ),
        ]
        asyncio.run(store.save_plan(run_id=run_id, plan=plan, tasks=tasks))

        cancelled = client.post(
            f"/v1/runs/{run_id}/tasks/pending/cancel",
            headers={SUB_HEADER: OWNER},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        replay = client.post(
            f"/v1/runs/{run_id}/tasks/pending/cancel",
            headers={SUB_HEADER: OWNER},
        )
        assert replay.status_code == 200
        assert replay.json() == cancelled.json()
        assert client.get(
            f"/v1/runs/{run_id}", headers={SUB_HEADER: OWNER}
        ).json()["status"] == "waiting_for_approval"

        synthesis = client.post(
            f"/v1/runs/{run_id}/tasks/s/cancel",
            headers={SUB_HEADER: OWNER},
        )
        assert synthesis.status_code == 409
        assert synthesis.json()["error"]["type"] == "task_cancel_conflict"

        stranger = client.post(
            f"/v1/runs/{run_id}/tasks/pending/cancel",
            headers={SUB_HEADER: STRANGER},
        )
        assert stranger.status_code == 404


def test_approval_decide_gates_on_edit_share(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        approval_id = _create_approval(client, run_id)
        accept_via_http(
            client, grant_via_http(client, run_id, permission="view")["id"]
        )

        listing = client.get(
            f"/v1/runs/{run_id}/approvals", headers={SUB_HEADER: RECIPIENT}
        )
        assert listing.status_code == 200
        assert listing.json()["data"][0]["approval_id"] == approval_id

        # A view share reads but must not decide: indistinct 404.
        denied = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            headers={SUB_HEADER: RECIPIENT},
        )
        assert denied.status_code == 404
        still_pending = client.get(
            f"/v1/runs/{run_id}/approvals", headers={SUB_HEADER: OWNER}
        ).json()["data"][0]
        assert still_pending["status"] == "pending"

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "note": "ok"},
            headers={SUB_HEADER: OWNER},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"

        replay = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            headers={SUB_HEADER: OWNER},
        )
        assert replay.status_code == 200
        conflict = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "reject"},
            headers={SUB_HEADER: OWNER},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["type"] == "conflict"

        missing = client.post(
            f"/v1/runs/{run_id}/approvals/apr_unbekannt",
            json={"decision": "approve"},
            headers={SUB_HEADER: OWNER},
        )
        assert missing.status_code == 404


def test_approval_decide_allowed_with_edit_share(monkeypatch):
    """An accepted EDIT share lets the recipient MUTATE the run (decide an
    approval) — the share-permits-edit ALLOW branch of ``_resolve_run``, the
    mirror of the view->404 gate above. This pins that a sufficient share
    grant actually flows through to permit a mutation (not just that view is
    denied). The higher ``manage`` tier resolves through the SAME router branch
    (``access_permits_edit``); its edit-or-higher ordering is pinned at the unit
    level in ``tests/test_runs_shared.py`` because v1 does not mint manage
    shares over HTTP.
    """
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        approval_id = _create_approval(client, run_id)
        accept_via_http(
            client, grant_via_http(client, run_id, permission="edit")["id"]
        )

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "note": "ok"},
            headers={SUB_HEADER: RECIPIENT},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"


def test_clarification_route_answers_and_validates(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        clarification = asyncio.run(
            client.agent_control.store.create_clarification(
                ClarificationRecord(
                    clarification_id=f"clr_{uuid.uuid4().hex[:8]}",
                    run_id=run_id,
                    question="Welcher Zeitraum?",
                    options=({"id": "q1", "label": "Q1"},),
                    default_assumption="Q1",
                )
            )
        )
        cl_id = clarification.clarification_id

        listing = client.get(
            f"/v1/runs/{run_id}/clarifications", headers={SUB_HEADER: OWNER}
        )
        assert listing.status_code == 200
        assert listing.json()["data"][0]["question"] == "Welcher Zeitraum?"

        both = client.post(
            f"/v1/runs/{run_id}/clarifications/{cl_id}",
            json={"answer": "x", "option_id": "q1"},
            headers={SUB_HEADER: OWNER},
        )
        assert both.status_code == 400

        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{cl_id}",
            json={"option_id": "q1"},
            headers={SUB_HEADER: OWNER},
        )
        assert answered.status_code == 200
        assert answered.json()["status"] == "answered"
        assert answered.json()["run"]["status"] in {
            "queued",
            "running",
            "completed",
        }

        different = client.post(
            f"/v1/runs/{run_id}/clarifications/{cl_id}",
            json={"answer": "etwas anderes"},
            headers={SUB_HEADER: OWNER},
        )
        assert different.status_code == 409


def test_artifact_routes_serve_conflict_matrix_and_export(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        store = client.agent_control.store
        asyncio.run(store.upsert_artifact(
            run_id=run_id,
            kind="memo",
            session_id="sess-1",
            title="Memo",
            status="ready",
            content_markdown="# V1",
            payload={},
            refs=[{"label": "K1", "document_id": "doc-1"}],
            updated_by="agent",
            artifact_id="art_memo",
        ))

        listing = client.get(
            f"/v1/runs/{run_id}/artifacts", headers={SUB_HEADER: OWNER}
        ).json()
        assert listing["object"] == "list"
        assert listing["data"][0]["artifact_id"] == "art_memo"
        assert "content_markdown" not in listing["data"][0]

        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            headers={SUB_HEADER: OWNER},
        ).json()
        assert detail["content_markdown"] == "# V1"
        assert detail["revisions"][0]["revision"] == 1
        # The snapshot carries the write status the picker filters on.
        assert detail["revisions"][0]["status"] == "ready"

        stale = client.put(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            json={"content_markdown": "# stale", "expected_revision": 9},
            headers={SUB_HEADER: OWNER},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["current_revision"] == 1

        edited = client.put(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            json={"content_markdown": "# V2", "expected_revision": 1},
            headers={SUB_HEADER: OWNER},
        )
        assert edited.status_code == 200
        assert edited.json() == {
            "id": "art_memo",
            "revision": 2,
            "updated_by": "user",
        }

        asyncio.run(store.upsert_artifact(
            run_id=run_id,
            kind="memo",
            session_id="sess-1",
            title="Memo",
            status="writing",
            content_markdown="# V3",
            payload={},
            refs=[],
            updated_by="agent",
        ))
        locked = client.put(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            json={"content_markdown": "# x", "expected_revision": 3},
            headers={SUB_HEADER: OWNER},
        )
        assert locked.status_code == 409
        assert locked.json()["error"]["locked_by"] == "agent"

        # A view share reads artifacts but cannot edit them.
        accept_via_http(
            client, grant_via_http(client, run_id, permission="view")["id"]
        )
        shared_read = client.get(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            headers={SUB_HEADER: RECIPIENT},
        )
        assert shared_read.status_code == 200
        shared_edit = client.put(
            f"/v1/runs/{run_id}/artifacts/art_memo",
            json={"content_markdown": "# x", "expected_revision": 3},
            headers={SUB_HEADER: RECIPIENT},
        )
        assert shared_edit.status_code == 404

        # Export creates a fresh editor document per call (copy-out).
        first = client.post(
            f"/v1/runs/{run_id}/artifacts/art_memo/export",
            json={"target": "editor_document", "title": "Mein Memo"},
            headers={SUB_HEADER: OWNER},
        )
        assert first.status_code == 201
        payload = first.json()
        assert payload["title"] == "Mein Memo"
        # Agent exports carry their own source (distinct from a native
        # research-report import) plus the run back-reference (API #12).
        assert payload["source"] == "agent-artifact"
        assert payload["source_run_id"] == run_id
        second = client.post(
            f"/v1/runs/{run_id}/artifacts/art_memo/export",
            json={},
            headers={SUB_HEADER: OWNER},
        )
        assert second.status_code == 201
        assert second.json()["id"] != payload["id"]

        bad_target = client.post(
            f"/v1/runs/{run_id}/artifacts/art_memo/export",
            json={"target": "usb_stick"},
            headers={SUB_HEADER: OWNER},
        )
        assert bad_target.status_code == 400


def test_session_artifacts_listing_is_anchor_independent(monkeypatch):
    """GET /v1/agent-sessions/{id}/artifacts survives the run re-anchor.

    A session artifact's run_id moves to the newest updating run, so the
    OLD run's listing silently loses it — the session listing
    keys on the session and keeps the artifact with its CURRENT anchor.
    Owner-private like every session route."""
    client = make_sharing_client(monkeypatch)
    with client:
        # The session record itself (owner-private resolution target).
        assert client.put(
            "/v1/agent-sessions/sess-anchor",
            json={
                "title": "Anker-Session",
                "items_json": "[]",
                "group_id": None,
                "created_at": 1000.0,
                "updated_at": 1000.0,
            },
            headers={SUB_HEADER: OWNER},
        ).status_code == 200

        run_one = _parked_agent_run(client)
        run_two = _parked_agent_run(client)
        store = client.agent_control.store
        asyncio.run(store.upsert_artifact(
            run_id=run_one,
            kind="memo",
            session_id="sess-anchor",
            title="Sitzungsmemo",
            status="ready",
            content_markdown="# V1",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_anchor_memo",
        ))
        # Turn 2 updates the SAME session memo — the row re-anchors.
        asyncio.run(store.upsert_artifact(
            run_id=run_two,
            kind="memo",
            session_id="sess-anchor",
            title="Sitzungsmemo",
            status="ready",
            content_markdown="# V2",
            payload={},
            refs=[],
            updated_by="agent",
        ))

        # The OLD run's listing has lost the memo (the motivating gap) …
        old_listing = client.get(
            f"/v1/runs/{run_one}/artifacts", headers={SUB_HEADER: OWNER}
        ).json()
        assert old_listing["data"] == []

        # … the session listing keeps it, meta-only, with the NEW anchor.
        listing = client.get(
            "/v1/agent-sessions/sess-anchor/artifacts",
            headers={SUB_HEADER: OWNER},
        ).json()
        assert listing["object"] == "list"
        assert [row["artifact_id"] for row in listing["data"]] == [
            "art_anchor_memo",
        ]
        row = listing["data"][0]
        assert row["run_id"] == run_two
        assert row["revision"] == 2
        assert "content_markdown" not in row

        # Owner-private: another user gets the indistinct 404.
        assert client.get(
            "/v1/agent-sessions/sess-anchor/artifacts",
            headers={SUB_HEADER: RECIPIENT},
        ).status_code == 404
        assert client.get(
            "/v1/agent-sessions/unbekannt/artifacts",
            headers={SUB_HEADER: OWNER},
        ).status_code == 404


def test_agent_sessions_crud_is_owner_private(monkeypatch):
    client = make_sharing_client(monkeypatch)
    with client:
        saved = client.put(
            "/v1/agent-sessions/as_1",
            json={
                "title": "Marktanalyse",
                "items_json": "[{\"q\": \"...\"}]",
                "group_id": None,
                "created_at": 1000.0,
                "updated_at": 1000.0,
            },
            headers={SUB_HEADER: OWNER},
        )
        assert saved.status_code == 200

        listing = client.get(
            "/v1/agent-sessions", headers={SUB_HEADER: OWNER}
        ).json()
        assert [row["id"] for row in listing["data"]] == ["as_1"]
        # List rows carry the small settings body, so the client compares its
        # sessions against the real row instead of default settings.
        assert listing["data"][0]["items_json"] == "[{\"q\": \"...\"}]"

        detail = client.get(
            "/v1/agent-sessions/as_1", headers={SUB_HEADER: OWNER}
        ).json()
        assert detail["items_json"] == "[{\"q\": \"...\"}]"

        # Sessions are private per user — even other users see nothing.
        foreign = client.get(
            "/v1/agent-sessions/as_1", headers={SUB_HEADER: RECIPIENT}
        )
        assert foreign.status_code == 404
        assert (
            client.get(
                "/v1/agent-sessions", headers={SUB_HEADER: RECIPIENT}
            ).json()["data"]
            == []
        )

        deleted = client.delete(
            "/v1/agent-sessions/as_1", headers={SUB_HEADER: OWNER}
        )
        assert deleted.status_code == 204
        assert (
            client.get(
                "/v1/agent-sessions/as_1", headers={SUB_HEADER: OWNER}
            ).status_code
            == 404
        )

        # A durable store deletes through the operation ledger instead: the
        # route accepts the request and hands back the operation the caller
        # polls until it is terminal (the executor itself is covered by
        # tests/test_session_deletion_contract.py).
        assert (
            client.put(
                "/v1/agent-sessions/as_2",
                json={
                    "title": "Marktanalyse",
                    "items_json": "[]",
                    "group_id": None,
                    "created_at": 1000.0,
                    "updated_at": 1000.0,
                },
                headers={SUB_HEADER: OWNER},
            ).status_code
            == 200
        )
        monkeypatch.setattr(
            AgentSessionsService, "durable", property(lambda _self: True)
        )
        accepted = client.delete(
            "/v1/agent-sessions/as_2", headers={SUB_HEADER: OWNER}
        )
        assert accepted.status_code == 202
        summary = accepted.json()
        assert summary["operation_id"].startswith("del_")
        assert summary["target_kind"] == "agent_session"
        assert summary["target_id"] == "as_2"
