"""Admin trace export (GET /v1/admin/runs/{run_id}/trace/export).

The endpoint resolves the run's trace id from the durable
``inqtrix.run.trace`` event, exports the trace from the mode-selected
source (Langfuse or file spool), audits the export BEFORE answering,
and answers with clear errors when no sink records trace details.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.auth.principal_generation import (
    bind_principal_generation,
    install_principal_generation_error_handler,
)
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.admin_trace import (
    build_router as build_admin_trace_router,
)
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import (
    AuthSettings,
    ObservabilitySettings,
    Settings,
)

OWNER = ("owner@example.com", "correct-horse-battery")
TRACE_HEX = "0af7651916cd43dd8448eb211c80319c"


class _RecordingAuditSink:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.entries = []
        self._rows = rows or []

    async def record(self, entry) -> None:
        self.entries.append(entry)

    async def list_audit_entries(self, **kwargs):
        """Read side of the sink, filtered like both real twins."""
        self.list_calls = getattr(self, "list_calls", [])
        self.list_calls.append(kwargs)
        rows = [
            row
            for row in self._rows
            if (
                not kwargs.get("resource_type")
                or row["resource_type"] == kwargs["resource_type"]
            )
            and (
                not kwargs.get("resource_id")
                or row["resource_id"] == kwargs["resource_id"]
            )
        ]
        return rows, None


class _FakeRunStore:
    def __init__(self, trace_ids: dict[str, str | None]) -> None:
        self._trace_ids = trace_ids

    def trace_id(self, run_id: str) -> str | None:
        return self._trace_ids.get(run_id)


def make_client(*, tracing: str, spool_dir: str, trace_ids, ui_url="", audit_rows=None):
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            pseudonym_pepper="pepper" * 6,
            oidc_insecure_dev_cookies=True,
        ),
        observability=ObservabilitySettings(
            tracing=tracing,
            trace_spool_dir=spool_dir,
            trace_ui_url=ui_url,
        ),
    )
    provider = build_local_provider(settings)
    audit = _RecordingAuditSink(audit_rows)
    app = FastAPI()
    principal_dep = bind_principal_generation(
        provider.build_principal_dependency()
    )
    install_principal_generation_error_handler(app)
    app.include_router(build_auth_router(provider, principal_dep))
    app.include_router(build_admin_router(provider, principal_dep))
    container = SimpleNamespace(
        auth_provider=provider,
        principal_dependency=principal_dep,
        permission_service=SimpleNamespace(audit_sink=audit),
        run_store=_FakeRunStore(trace_ids),
        settings=settings,
    )
    app.include_router(build_admin_trace_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    client.post(
        "/api/setup/owner",
        json={
            "email": OWNER[0],
            "password": OWNER[1],
            "display_name": "Owner",
        },
    )
    return client, audit


def _spool_with_trace(tmp_path):
    trace_b64 = base64.b64encode(bytes.fromhex(TRACE_HEX)).decode("ascii")
    line = json.dumps(
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {"traceId": trace_b64, "name": "inqtrix.run"}
                            ]
                        }
                    ]
                }
            ]
        }
    )
    (tmp_path / "trace-spool-1-aa-000001.otlp.jsonl").write_text(line + "\n")
    return tmp_path


def test_export_from_spool_audits_and_returns_replayable_document(tmp_path):
    _spool_with_trace(tmp_path)
    client, audit = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": TRACE_HEX},
    )
    response = client.get("/v1/admin/runs/run_1/trace/export")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run_1"
    assert body["trace_id"] == TRACE_HEX
    assert body["source"] == "spool"
    assert body["payload"]["replayable"] is True
    entry = audit.entries[-1]
    assert entry.action == "export.trace"
    assert entry.resource_id == "run_1"
    assert entry.detail["trace_id"] == TRACE_HEX
    assert entry.detail["source"] == "spool"


def test_export_404_when_run_has_no_trace(tmp_path):
    client, audit = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": None},
    )
    response = client.get("/v1/admin/runs/run_1/trace/export")
    assert response.status_code == 404
    assert "trace ID" in response.json()["error"]["message"]
    assert audit.entries == []


def test_export_409_when_no_sink_is_active(tmp_path):
    client, audit = make_client(
        tracing="off",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": TRACE_HEX},
    )
    response = client.get("/v1/admin/runs/run_1/trace/export")
    assert response.status_code == 409
    assert "No trace sink" in response.json()["error"]["message"]
    assert audit.entries == []


def test_export_langfuse_includes_ui_deep_link(tmp_path, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://langfuse:3000/api/public/otel",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc"
    )

    def fake_get(url, params=None, headers=None):
        assert headers["Authorization"] == "Basic abc"
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": TRACE_HEX,
                "htmlPath": f"/project/p1/traces/{TRACE_HEX}",
                "observations": [],
            },
        )

    import inqtrix.observability.trace_readers as readers

    monkeypatch.setattr(
        readers.LangfuseReader,
        "_get",
        lambda self, url, params=None: fake_get(
            url, params, {"Authorization": self.authorization}
        ),
    )
    client, audit = make_client(
        tracing="otlp",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": TRACE_HEX},
        ui_url="http://localhost:3300/",
    )
    response = client.get("/v1/admin/runs/run_1/trace/export")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "langfuse"
    assert (
        body["ui_url"]
        == f"http://localhost:3300/project/p1/traces/{TRACE_HEX}"
    )
    assert audit.entries[-1].detail["source"] == "langfuse"


def test_export_is_admin_gated(tmp_path):
    client, _ = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": TRACE_HEX},
    )
    client.cookies.clear()
    response = client.get("/v1/admin/runs/run_1/trace/export")
    assert response.status_code in (401, 404)


def test_export_denies_authenticated_non_admin_indistinguishably(tmp_path):
    _spool_with_trace(tmp_path)
    client, audit = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_1": TRACE_HEX},
    )
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    created = client.post(
        "/v1/admin/users",
        json={
            "email": "member@example.com",
            "password": "another-horse-battery",
            "display_name": "Member",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code in (200, 201)
    client.cookies.clear()
    login = client.post(
        "/api/auth/login/local",
        json={
            "email": "member@example.com",
            "password": "another-horse-battery",
        },
    )
    assert login.status_code == 200
    response = client.get("/v1/admin/runs/run_1/trace/export")
    # Not-403 convention: denial is an indistinguishable 404 and no
    # export audit entry is written.
    assert response.status_code == 404
    assert all(entry.action != "export.trace" for entry in audit.entries)


def test_deleted_run_recovers_its_trace_id_from_the_surviving_audit_row(tmp_path):
    """run_events cascades with the run; the WORM audit row does not.

    After a deletion the forensic question matters most, and that is exactly
    when the only source the route consulted has already been cascaded away.
    """
    _spool_with_trace(tmp_path)
    client, audit = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_gone": None},
        audit_rows=[
            {
                "action": "run.completed",
                "resource_type": "run",
                "resource_id": "run_gone",
                "correlation": {"trace_id": TRACE_HEX},
            }
        ],
    )

    response = client.get(
        "/v1/admin/runs/run_gone/trace/export", auth=OWNER
    )

    assert response.status_code == 200
    assert response.json()["trace_id"] == TRACE_HEX
    # The lookup must be scoped to this run, not a scan of the whole log.
    assert audit.list_calls[0]["resource_id"] == "run_gone"
    assert audit.list_calls[0]["resource_type"] == "run"


def test_unknown_trace_stays_a_404_when_no_audit_row_carries_one(tmp_path):
    _spool_with_trace(tmp_path)
    client, _audit = make_client(
        tracing="file",
        spool_dir=str(tmp_path),
        trace_ids={"run_gone": None},
        audit_rows=[
            {
                "action": "run.completed",
                "resource_type": "run",
                "resource_id": "other_run",
                "correlation": {"trace_id": TRACE_HEX},
            }
        ],
    )

    response = client.get(
        "/v1/admin/runs/run_gone/trace/export", auth=OWNER
    )

    assert response.status_code == 404
