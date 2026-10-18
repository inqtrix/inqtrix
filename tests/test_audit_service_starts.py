"""Dienststart-Index terminal rows + AuditService envelope."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextlib import contextmanager

from inqtrix.server.runs import RunStatus, RunStore
from inqtrix.services.audit_service import AuditService, build_audit_entry


class _RecordingAuthority:
    """Captures append_audit_row calls (memory-authority double)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_audit_row(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def append_registered_resource_effects(self, **kwargs) -> None:
        pass

    @contextmanager
    def creation_guard(self, **kwargs):
        yield


def _store(**kwargs) -> tuple[RunStore, _RecordingAuthority]:
    store = RunStore(
        max_concurrent=1,
        max_queue_size=4,
        completed_ttl_seconds=30,
        event_buffer_size=50,
        **kwargs,
    )
    authority = _RecordingAuthority()
    store._authority = authority  # test seam: coordinator double
    return store, authority


def _wait_terminal(store: RunStore, run_id: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if store._records[run_id].status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            return
        time.sleep(0.05)
    raise AssertionError("run never reached a terminal state")


def test_completed_run_writes_index_row_with_metadata():
    store, authority = _store()

    def work(handle):
        handle.emit("inqtrix.run.trace", {"trace_id": "c" * 32})
        handle.emit_answer("done")

    summary = store.submit(
        question="index", stack_name="default", work=work
    )
    _wait_terminal(store, summary["run_id"])
    rows = [r for r in authority.rows if r["action"] == "run.completed"]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "success"
    assert row["resource_type"] == "run"
    assert row["resource_id"] == summary["run_id"]
    assert row["correlation"]["run_id"] == summary["run_id"]
    assert row["correlation"]["trace_id"] == "c" * 32
    assert "mode" in row["detail"]
    # Content never leaks into the index row.
    assert "index" not in str(row["detail"])


def test_failed_run_writes_failure_index_row():
    store, authority = _store()

    def work(handle):
        raise RuntimeError("boom")

    summary = store.submit(question="x", stack_name="default", work=work)
    _wait_terminal(store, summary["run_id"])
    rows = [r for r in authority.rows if r["action"] == "run.failed"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["detail"].get("error_type")


def test_toggle_disables_index_rows():
    store, authority = _store(audit_service_starts=False)
    summary = store.submit(
        question="quiet",
        stack_name="default",
        work=lambda handle: handle.emit_answer("ok"),
    )
    _wait_terminal(store, summary["run_id"])
    assert [
        r
        for r in authority.rows
        if r["action"].startswith("run.") and r["action"] != "run.created"
    ] == []


def test_build_audit_entry_fills_envelope(monkeypatch):
    from inqtrix.observability import context as obs_context

    tokens = obs_context.bind_log_context(
        request_id="req-1", run_id="run-9"
    )
    try:
        actor = uuid.uuid4()
        entry = build_audit_entry(
            tenant_id="default",
            action="file.uploaded",
            resource_type="file",
            resource_id="file-1",
            actor_user_id=actor,
            detail={"size": "42"},
        )
    finally:
        obs_context.reset_log_context(tokens)
    assert entry.correlation["request_id"] == "req-1"
    assert entry.correlation["run_id"] == "run-9"
    assert entry.actor_pseudonym is not None
    assert entry.actor_pseudonym.startswith("usr_")
    assert entry.outcome == "success"


def _auth_client(max_attempts: int = 3):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from inqtrix.auth.api_key import build_local_provider
    from inqtrix.auth.principal_generation import (
        bind_principal_generation,
        install_principal_generation_error_handler,
    )
    from inqtrix.server.request_context import RequestContextMiddleware
    from inqtrix.server.routers.auth import build_auth_router
    from inqtrix.settings import AuthSettings, Settings

    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            pseudonym_pepper="pep" * 12,
            oidc_insecure_dev_cookies=True,
            login_rate_limit_max_attempts=max_attempts,
        )
    )
    provider = build_local_provider(settings)

    class _RecordingSink:
        def __init__(self) -> None:
            self.entries = []

        async def record(self, entry) -> None:
            self.entries.append(entry)

    sink = _RecordingSink()
    app = FastAPI()
    principal_dep = bind_principal_generation(
        provider.build_principal_dependency()
    )
    install_principal_generation_error_handler(app)
    app.add_middleware(RequestContextMiddleware, trusted_proxy_hops=0)
    app.include_router(
        build_auth_router(provider, principal_dep, audit=sink)
    )
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    client.post(
        "/api/setup/owner",
        json={
            "email": "owner@example.com",
            "password": "correct-horse-battery",
            "display_name": "Owner",
        },
    )
    return client, sink


def test_login_failure_and_lockout_write_audit_rows():
    client, sink = _auth_client(max_attempts=2)
    for _ in range(2):
        response = client.post(
            "/api/auth/login/local",
            json={"email": "owner@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
    failures = [e for e in sink.entries if e.action == "auth.login_failed"]
    lockouts = [e for e in sink.entries if e.action == "auth.lockout"]
    assert len(failures) == 2
    assert len(lockouts) == 1
    entry = failures[0]
    assert entry.outcome == "failure"
    assert entry.resource_id == "owner@example.com"
    assert entry.origin.get("auth_method") == "local"
    # Middleware bound the socket peer + user agent for the origin.
    assert entry.origin.get("ip")
    assert entry.correlation.get("request_id")
    assert lockouts[0].outcome == "denied"
    # No password anywhere in the trail.
    assert "wrong" not in str(sink.entries)


def test_logout_writes_audit_row():
    client, sink = _auth_client()
    client.post(
        "/api/auth/login/local",
        json={
            "email": "owner@example.com",
            "password": "correct-horse-battery",
        },
    )
    raw_session_id = client.cookies.get("inqtrix_session")
    assert raw_session_id is not None and len(raw_session_id) == 43
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    logouts = [e for e in sink.entries if e.action == "auth.logout"]
    assert len(logouts) == 1
    assert logouts[0].actor_user_id is not None
    assert logouts[0].actor_pseudonym.startswith("usr_")
    resource_id = logouts[0].resource_id
    assert re.fullmatch(r"ses_[0-9a-f]{16}", resource_id)
    assert raw_session_id not in resource_id
    assert raw_session_id[:12] not in resource_id
    assert raw_session_id[-12:] not in resource_id


def test_audit_service_record_is_fail_safe_but_loud(caplog):
    class _BrokenSink:
        async def record(self, entry):
            raise RuntimeError("sink down")

    service = AuditService(_BrokenSink())
    entry = build_audit_entry(
        tenant_id="default",
        action="file.uploaded",
        resource_type="file",
        resource_id="f1",
    )
    with caplog.at_level("WARNING", logger="inqtrix"):
        asyncio.run(service.record(entry))
    assert any(
        "Audit entry" in message for message in caplog.messages
    )
