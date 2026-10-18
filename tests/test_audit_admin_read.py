"""Read layer: GET /v1/admin/audit, /export, run events drawer."""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuditEntry
from inqtrix.auth.principal_generation import (
    bind_principal_generation,
    install_principal_generation_error_handler,
)
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.audit_admin import (
    build_router as build_audit_admin_router,
)
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import AuthSettings, Settings

OWNER = ("owner@example.com", "correct-horse-battery")


class _FakeRunStore:
    def events_snapshot(self, run_id, *, after=0):
        if run_id != "run_1":
            return []
        return [
            {
                "type": "phase",
                "run_id": run_id,
                "sequence": 1,
                "created_at": 1.0,
                "data": {"status": "running"},
            },
            {
                "type": "inqtrix.run.completed",
                "run_id": run_id,
                "sequence": 2,
                "created_at": 2.0,
                "data": {"status": "completed"},
            },
        ][after:]


def make_client():
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            pseudonym_pepper="pep" * 12,
            oidc_insecure_dev_cookies=True,
        )
    )
    provider = build_local_provider(settings)
    sink = MemoryIdentityStore()
    app = FastAPI()
    principal_dep = bind_principal_generation(
        provider.build_principal_dependency()
    )
    install_principal_generation_error_handler(app)
    app.include_router(build_auth_router(provider, principal_dep, audit=sink))
    app.include_router(build_admin_router(provider, principal_dep))
    container = SimpleNamespace(
        auth_provider=provider,
        principal_dependency=principal_dep,
        permission_service=SimpleNamespace(audit_sink=sink),
        run_store=_FakeRunStore(),
    )
    app.include_router(build_audit_admin_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    client.post(
        "/api/setup/owner",
        json={
            "email": OWNER[0],
            "password": OWNER[1],
            "display_name": "Owner",
        },
    )
    return client, sink


def _seed(sink: MemoryIdentityStore, count: int = 5) -> None:
    import asyncio

    async def _fill():
        for index in range(count):
            await sink.record(
                AuditEntry(
                    tenant_id="default",
                    actor_user_id=uuid.uuid4(),
                    action=(
                        "run.completed" if index % 2 == 0 else "auth.logout"
                    ),
                    resource_type="run" if index % 2 == 0 else "session",
                    resource_id=f"res-{index}",
                    outcome="success" if index != 3 else "failure",
                    correlation={"run_id": f"run-{index}"},
                )
            )

    asyncio.run(_fill())


def test_audit_list_filters_and_paginates():
    client, sink = make_client()
    _seed(sink, count=5)
    response = client.get("/v1/admin/audit?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    # Newest first: the seed wrote res-0..res-4 in order.
    assert body["data"][0]["resource_id"] == "res-4"
    assert body["next_cursor"] is not None

    second = client.get(
        f"/v1/admin/audit?limit=2&cursor={body['next_cursor']}"
    )
    assert second.status_code == 200
    assert second.json()["data"][0]["id"] < body["data"][-1]["id"]

    filtered = client.get("/v1/admin/audit?action=run.")
    actions = {row["action"] for row in filtered.json()["data"]}
    assert actions == {"run.completed"}

    failures = client.get("/v1/admin/audit?outcome=failure")
    assert [r["resource_id"] for r in failures.json()["data"]] == ["res-3"]

    bad = client.get("/v1/admin/audit?cursor=abc")
    assert bad.status_code == 400


def test_audit_export_streams_and_audits_itself():
    client, sink = make_client()
    _seed(sink, count=3)
    response = client.get("/v1/admin/audit/export?format=ndjson")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/x-ndjson"
    )
    assert "attachment" in response.headers["content-disposition"]
    lines = [
        json.loads(line)
        for line in response.text.strip().splitlines()
        if line
    ]
    # The export itself was audited BEFORE streaming and therefore
    # appears as the newest row of its own output.
    assert lines[0]["action"] == "export.audit"
    assert len(lines) == 4

    csv_response = client.get("/v1/admin/audit/export?format=csv")
    assert csv_response.status_code == 200
    header = csv_response.text.splitlines()[0]
    assert header.startswith("id,occurred_at,action,outcome")

    assert (
        client.get("/v1/admin/audit/export?format=xml").status_code == 400
    )


def test_logout_session_reference_is_safe_in_list_and_exports():
    client, _sink = make_client()
    raw_session_id = client.cookies.get("inqtrix_session")
    assert raw_session_id is not None and len(raw_session_id) == 43
    csrf = client.get("/api/auth/session").json()["csrf_token"]

    logout = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    )
    assert logout.status_code == 200
    assert client.get("/api/auth/session").json() == {"authenticated": False}

    login = client.post(
        "/api/auth/login/local",
        json={"email": OWNER[0], "password": OWNER[1]},
    )
    assert login.status_code == 200

    listed = client.get("/v1/admin/audit?action=auth.logout")
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1
    safe_reference = rows[0]["resource_id"]
    assert re.fullmatch(r"ses_[0-9a-f]{16}", safe_reference)

    ndjson = client.get(
        "/v1/admin/audit/export?format=ndjson&action=auth.logout"
    )
    csv = client.get(
        "/v1/admin/audit/export?format=csv&action=auth.logout"
    )
    assert ndjson.status_code == 200
    assert csv.status_code == 200
    for rendered in (json.dumps(listed.json()), ndjson.text, csv.text):
        assert safe_reference in rendered
        assert raw_session_id not in rendered
        assert raw_session_id[:12] not in rendered
        assert raw_session_id[-12:] not in rendered


def test_run_events_endpoint_serves_drawer_steps():
    client, _ = make_client()
    response = client.get("/v1/admin/runs/run_1/events")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [e["type"] for e in data] == [
        "phase",
        "inqtrix.run.completed",
    ]
    after = client.get("/v1/admin/runs/run_1/events?after=1")
    assert [e["sequence"] for e in after.json()["data"]] == [2]
    empty = client.get("/v1/admin/runs/unknown/events")
    assert empty.json()["data"] == []


def test_read_layer_is_admin_gated():
    client, sink = make_client()
    _seed(sink, count=1)
    client.cookies.clear()
    for path in (
        "/v1/admin/audit",
        "/v1/admin/audit/export",
        "/v1/admin/runs/run_1/events",
    ):
        assert client.get(path).status_code in (401, 404)


def test_read_layer_denies_authenticated_non_admin_indistinguishably():
    client, sink = make_client()
    _seed(sink, count=1)
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
    for path in (
        "/v1/admin/audit",
        "/v1/admin/audit/export",
        "/v1/admin/runs/run_1/events",
    ):
        # Not-403 convention: denial is an indistinguishable 404.
        assert client.get(path).status_code == 404


def test_csv_cells_neutralize_spreadsheet_formulas():
    from inqtrix.server.routers.audit_admin import _csv_cell

    assert _csv_cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert _csv_cell("+SUM(A1)") == "'+SUM(A1)"
    assert _csv_cell("-2+3") == "'-2+3"
    assert _csv_cell("@x") == "'@x"
    assert _csv_cell("harmless") == "harmless"
    assert _csv_cell(None) == ""
    assert _csv_cell(42) == 42
    assert _csv_cell({"a": 1}) == '{"a": 1}'
