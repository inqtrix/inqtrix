"""HTTP tests for the agent control routes (/v1/runs/{id}/approvals|
clarifications|artifacts) and the agent-sessions clone.

Uses the oidc-header sharing harness from ``tests/test_runs_sharing.py``:
ownership and share semantics need real principals, which the contract app
(auth mode ``none``) cannot provide.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Callable

import pytest

from inqtrix.agents.control_ports import (
    ApprovalRecord,
    ClarificationRecord,
)
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
        mode="research",
        workflow_contract=1,
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


def _create_approval(client, run_id: str, *, kind: str = "tool") -> str:
    """A pending approval of *kind*; by default the kernel's tool gate with
    the one action it asks about."""
    actions = [
        {
            "tool": "web_instant",
            "args": {"query": "EU AI Act Fristen"},
            "summary": "Websuche",
        }
    ]
    approval = asyncio.run(
        client.agent_control.store.create_approval(
            ApprovalRecord(
                approval_id=f"apr_{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                kind=kind,
                payload={"actions": actions} if kind == "tool" else {},
            )
        )
    )
    return approval.approval_id


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
        # The route denies before it reads the body: a malformed decision
        # from a view share is the same 404, never the 400 that would tell
        # the viewer a decision was possible.
        malformed = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={},
            headers={SUB_HEADER: RECIPIENT},
        )
        assert malformed.status_code == 404
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan", {"summary_markdown": "Plan", "tasks": []}),
        ("edited_tasks", []),
        ("report_guidance", "Bitte knapp."),
        ("report_guidance", None),
        ("report_rule_ids", ["pt_knapp"]),
    ],
)
def test_approval_decide_refuses_a_field_of_the_retired_plan_gate_by_name(
    monkeypatch, field, value
):
    """A field only the mission's plan gate read is refused, never ignored:
    the client learns that it has no effect, and nothing is decided."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        approval_id = _create_approval(client, run_id)

        refused = client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", field: value},
            headers={SUB_HEADER: OWNER},
        )
        still_pending = client.get(
            f"/v1/runs/{run_id}/approvals", headers={SUB_HEADER: OWNER}
        ).json()["data"][0]

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"] == (
        f"{field} is not part of an approval decision; send decision, note, "
        "actions or approval_scope."
    )
    assert still_pending["status"] == "pending"


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


def test_artifact_export_body_may_be_left_out_but_must_be_an_object(monkeypatch):
    """Every export field is optional: no body exports, a broken one is refused."""
    client = make_sharing_client(monkeypatch)
    with client:
        run_id = _parked_agent_run(client)
        asyncio.run(client.agent_control.store.upsert_artifact(
            run_id=run_id,
            kind="memo",
            session_id="sess-1",
            title="Memo",
            status="ready",
            content_markdown="# V1",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_memo",
        ))
        path = f"/v1/runs/{run_id}/artifacts/art_memo/export"
        json_headers = {SUB_HEADER: OWNER, "Content-Type": "application/json"}

        without_body = client.post(path, headers={SUB_HEADER: OWNER})
        not_an_object = client.post(path, content=b"[]", headers=json_headers)
        malformed = client.post(path, content=b"{not json", headers=json_headers)

    assert without_body.status_code == 201, without_body.text
    assert without_body.json()["source_run_id"] == run_id
    for refused in (not_an_object, malformed):
        assert refused.status_code == 400, refused.text
        assert refused.json() == {
            "error": {
                "message": "Invalid JSON body",
                "type": "invalid_request_error",
            }
        }


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
