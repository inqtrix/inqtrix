"""Contract tests for the native ``/v1/runs*`` lifecycle surface.

Locks the run summary key set, the error envelopes (404 wording the
React client matches on, 409 before completion), the result payload
injection (``run_id``/``status`` + export payload + usage), and the
client-supplied workspace namespace semantics that the authorization
rebuild must keep byte-compatible for the legacy mode.
"""

from __future__ import annotations

import threading

import inqtrix.research.web_research as web_research_module

from tests.contract._app import (
    make_contract_client,
    minimal_agent_result,
    wait_for_run_status,
)

RUN_SUMMARY_KEYS = {
    "access",
    "run_id",
    "status",
    "queue_position",
    "question",
    "stack",
    "workspace_id",
    "mode",
    "agent_overrides",
    "created_at",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "snapshot",
    "error",
    "events_url",
    "result_url",
}

NOT_FOUND_ENVELOPE = {
    "error": {"message": "Run not found", "type": "not_found"}
}


def test_create_run_returns_202_with_full_summary(monkeypatch):
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **kw: minimal_agent_result()
    )

    with make_contract_client() as client:
        response = client.post(
            "/v1/runs",
            json={"question": "Was ist neu?", "mode": "research"},
        )

        assert response.status_code == 202
        summary = response.json()
        assert set(summary.keys()) == RUN_SUMMARY_KEYS
        assert summary["status"] in {"queued", "running"}
        assert summary["stack"] == "default"
        assert summary["mode"] == "research"
        assert summary["workspace_id"] is None
        assert summary["access"] == {"mode": "unscoped"}
        assert summary["events_url"] == f"/v1/runs/{summary['run_id']}/events"
        assert summary["result_url"] == f"/v1/runs/{summary['run_id']}/result"

        wait_for_run_status(client, summary["run_id"], "completed")


def test_response_form_is_rejected_outside_agent_modes() -> None:
    with make_contract_client() as client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Was ist neu?",
                "mode": "research",
                "response_form": "chat",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "response_form" in response.json()["error"]["message"]


def test_session_id_is_rejected_outside_saved_session_modes() -> None:
    with make_contract_client() as client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Was ist neu?",
                "mode": "research",
                "session_id": "as_agent_only",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "session_id" in response.json()["error"]["message"]


def test_list_runs_wraps_data_in_list_object(monkeypatch):
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **kw: minimal_agent_result()
    )

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        wait_for_run_status(client, created["run_id"], "completed")

        listing = client.get("/v1/runs")

        assert listing.status_code == 200
        payload = listing.json()
        assert payload["object"] == "list"
        assert [run["run_id"] for run in payload["data"]] == [created["run_id"]]
        assert set(payload["data"][0].keys()) == RUN_SUMMARY_KEYS


def test_delete_removes_a_terminal_run_durably(monkeypatch):
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **kw: minimal_agent_result()
    )

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "completed")

        deleted = client.delete(f"/v1/runs/{run_id}")
        assert deleted.status_code == 204

        # Durable removal is the whole point: get is the 404 envelope the
        # client matches, and the list no longer carries it (so a reload
        # cannot bring the deleted run back).
        gone = client.get(f"/v1/runs/{run_id}")
        assert gone.status_code == 404
        assert gone.json() == NOT_FOUND_ENVELOPE
        listing = client.get("/v1/runs").json()
        assert all(item["run_id"] != run_id for item in listing["data"])

        # Not idempotent — a repeat delete is a clean 404, not a 500.
        again = client.delete(f"/v1/runs/{run_id}")
        assert again.status_code == 404
        assert again.json() == NOT_FOUND_ENVELOPE


def test_import_allocates_server_id_and_is_idempotent_while_row_exists() -> None:
    payload = {
        "source_run_id": "local-report-1",
        "question": "Importierte Frage",
        "status": "completed",
        "result": {"answer": "Importierte Antwort"},
    }
    with make_contract_client() as client:
        first = client.post("/v1/runs/import", json=payload)
        second = client.post("/v1/runs/import", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["run_id"] != payload["source_run_id"]
    assert second.json()["run_id"] == first.json()["run_id"]


def test_import_after_delete_never_reuses_the_stale_public_run_id() -> None:
    payload = {
        "source_run_id": "local-report-reimport",
        "question": "Importierte Frage",
        "status": "completed",
        "result": {"answer": "Importierte Antwort"},
    }
    with make_contract_client() as client:
        first = client.post("/v1/runs/import", json=payload)
        first_run_id = first.json()["run_id"]
        assert client.delete(f"/v1/runs/{first_run_id}").status_code == 204

        reimported = client.post("/v1/runs/import", json=payload)

    assert reimported.status_code == 200
    assert reimported.json()["run_id"] != first_run_id


def test_import_rejects_legacy_client_controlled_run_id() -> None:
    with make_contract_client() as client:
        response = client.post(
            "/v1/runs/import",
            json={
                "run_id": "client-controlled",
                "question": "Importierte Frage",
                "result": {"answer": "Importierte Antwort"},
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "source_run_id" in response.json()["error"]["message"]


def test_delete_of_an_active_run_returns_409(monkeypatch):
    release = threading.Event()

    def blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "running")

        active = client.delete(f"/v1/runs/{run_id}")
        assert active.status_code == 409
        assert active.json() == {
            "error": {
                "message": "Run is still active; cancel it first.",
                "type": "run_active",
            }
        }

        release.set()
        wait_for_run_status(client, run_id, "completed")


def test_result_before_completion_returns_409_with_status(monkeypatch):
    release = threading.Event()

    def blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "running")

        early = client.get(f"/v1/runs/{run_id}/result")
        assert early.status_code == 409
        assert early.json() == {
            "error": {
                "message": "Run has not completed yet",
                "type": "run_not_completed",
                "status": "running",
            }
        }

        release.set()
        wait_for_run_status(client, run_id, "completed")


def test_completed_result_payload_injects_run_id_status_and_usage(monkeypatch):
    monkeypatch.setattr(
        web_research_module,
        "run_web_graph",
        lambda *a, **kw: minimal_agent_result(prompt_tokens=11, completion_tokens=7),
    )

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "completed")

        result = client.get(f"/v1/runs/{run_id}/result")

    assert result.status_code == 200
    payload = result.json()
    assert payload["run_id"] == run_id
    assert payload["status"] == "completed"
    assert payload["answer"] == "Antwort mit Quelle [1]."
    assert payload["references"] == [
        {
            "label": "E1", "url": "https://example.com/source", "tier": "unknown",
            "tier_reason": "", "role_reason": "",
            "cited": True,
            "title": None, "document_id": None, "chunk_index": None,
            "excerpt": None, "source_text": None, "quotes": [],
            "grounded_support": None, "page_number": None,
            "provider_snippet": None,
            "reference_id": None,
            "source_id": None,
            "query_id": None,
            "query_ids": [],
            "citation_id": None,
            "citation_ids": [],
            "source_run_id": None,
            "source_run_ids": [],
            "source_span": None,
            "revision_id": None,
            "generation_id": None,
            "provenance_status": None,
            "statements": [],
        }
    ]
    assert payload["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        # This engine keeps no cumulative count, so the number is one
        # segment's - and the payload says so rather than leaving the
        # reader of an exported file to guess which it is.
        "scope": "segment",
    }
    assert "metrics" in payload


def test_unknown_run_id_envelopes_are_uniform():
    with make_contract_client() as client:
        get = client.get("/v1/runs/run_does_not_exist")
        result = client.get("/v1/runs/run_does_not_exist/result")
        cancel = client.post("/v1/runs/run_does_not_exist/cancel")
        events = client.get("/v1/runs/run_does_not_exist/events")

    assert get.status_code == 404
    assert get.json() == NOT_FOUND_ENVELOPE
    assert result.status_code == 404
    assert result.json() == NOT_FOUND_ENVELOPE
    assert cancel.status_code == 404
    assert cancel.json() == NOT_FOUND_ENVELOPE
    assert events.status_code == 404
    assert events.json() == NOT_FOUND_ENVELOPE


def test_cancel_running_run_keeps_running_until_worker_observes(monkeypatch):
    release = threading.Event()

    def blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        cancel_event = kwargs.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            return {
                "answer": "",
                "result_state": {"cancelled": True},
            }
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "running")

        cancelled = client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        # Cancellation of a RUNNING run is a request, not an immediate
        # terminal transition: the summary stays "running" until the
        # worker observes the cancel event at the next node boundary.
        assert cancelled.json()["status"] == "running"

        release.set()
        wait_for_run_status(client, run_id, "cancelled")


def test_invalid_workspace_header_envelope():
    with make_contract_client() as client:
        response = client.get(
            "/v1/runs",
            headers={"X-Inqtrix-Workspace-Id": "../bad"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Invalid workspace_id.",
            "type": "invalid_request_error",
        }
    }


def test_workspace_namespace_scopes_get_and_list(monkeypatch):
    """Legacy semantics: the header is a client-side namespace filter.

    A request WITHOUT the header sees every run; a request with a
    different namespace gets 404. The authorization rebuild keeps this
    exact behaviour for the no-auth/static-key legacy mode.
    """
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **kw: minimal_agent_result()
    )

    with make_contract_client() as client:
        created = client.post(
            "/v1/runs",
            headers={"X-Inqtrix-Workspace-Id": "ws_browser_a"},
            json={"question": "Frage"},
        ).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "completed")

        same_namespace = client.get(
            f"/v1/runs/{run_id}",
            headers={"X-Inqtrix-Workspace-Id": "ws_browser_a"},
        )
        other_namespace = client.get(
            f"/v1/runs/{run_id}",
            headers={"X-Inqtrix-Workspace-Id": "ws_browser_b"},
        )
        unscoped = client.get(f"/v1/runs/{run_id}")

    assert same_namespace.status_code == 200
    assert other_namespace.status_code == 404
    assert other_namespace.json() == NOT_FOUND_ENVELOPE
    assert unscoped.status_code == 200


def test_child_events_and_polling_enforce_inherited_workspace() -> None:
    """Child direct URLs retain the parent's workspace security boundary."""
    headers_a = {"X-Inqtrix-Workspace-Id": "workspace-a"}
    headers_b = {"X-Inqtrix-Workspace-Id": "workspace-b"}
    parent_started = threading.Event()
    parent_release = threading.Event()

    def run_parent(handle) -> None:
        parent_started.set()
        assert parent_release.wait(timeout=5)
        handle.complete({"answer": "parent", "metrics": {}})

    with make_contract_client() as client:
        store = client.app.state.container.run_store
        parent = store.submit(
            question="Agent",
            stack_name="default",
            workspace_id="workspace-a",
            kind="agent",
            work=run_parent,
        )
        assert parent_started.wait(timeout=2)
        try:
            child = store.submit(
                question="Child",
                stack_name="default",
                workspace_id="workspace-a",
                kind="agent_child",
                parent_run_id=parent["run_id"],
                root_run_id=parent["run_id"],
                request_payload={"body": {"parent_task_id": "task-a"}},
                work=lambda handle: handle.complete(
                    {"answer": "child", "metrics": {}}
                ),
            )
        finally:
            parent_release.set()
        wait_for_run_status(client, child["run_id"], "completed")

        polling = client.get(
            f"/v1/runs/{child['run_id']}/events?format=json",
            headers=headers_a,
        )
        polling_denied = client.get(
            f"/v1/runs/{child['run_id']}/events?format=json",
            headers=headers_b,
        )
        with client.stream(
            "GET",
            f"/v1/runs/{child['run_id']}/events",
            headers=headers_a,
        ) as stream:
            assert stream.status_code == 200
            assert "inqtrix.run.completed" in stream.read().decode("utf-8")
        stream_denied = client.get(
            f"/v1/runs/{child['run_id']}/events",
            headers=headers_b,
        )

    assert polling.status_code == 200
    assert polling.json()["terminal"] is True
    assert polling_denied.status_code == 404
    assert polling_denied.json() == NOT_FOUND_ENVELOPE
    assert stream_denied.status_code == 404
    assert stream_denied.json() == NOT_FOUND_ENVELOPE
