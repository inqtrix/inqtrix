"""Admin pseudonym resolution (POST /v1/admin/audit/resolve-pseudonym).

Re-identification recomputes the deterministic HMAC over the tenant's
user directory (no lookup table), is instance-admin gated, refuses to
answer without the instance pepper, and writes EVERY attempt — hit or
miss — into the audit sink before responding.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth import log_redaction
from inqtrix.auth.api_key import build_local_provider
from inqtrix.auth.log_redaction import (
    configure_stable_pseudonyms,
    stable_pseudonym,
)
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
PEPPER = "resolve-test-pepper"


class _RecordingAuditSink:
    def __init__(self) -> None:
        self.entries = []

    async def record(self, entry) -> None:
        self.entries.append(entry)


@pytest.fixture(autouse=True)
def _reset_pseudonym_state():
    saved_key = log_redaction._stable_key
    saved_warned = log_redaction._fallback_warned
    log_redaction._stable_key = None
    log_redaction._fallback_warned = False
    yield
    log_redaction._stable_key = saved_key
    log_redaction._fallback_warned = saved_warned


def make_client() -> tuple[TestClient, _RecordingAuditSink]:
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            pseudonym_pepper=PEPPER,
            oidc_insecure_dev_cookies=True,
        )
    )
    provider = build_local_provider(settings)
    audit = _RecordingAuditSink()
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
    )
    app.include_router(build_audit_admin_router(container))
    return TestClient(app, base_url="http://127.0.0.1:5100"), audit


def _owner_client():
    configure_stable_pseudonyms(PEPPER)
    client, audit = make_client()
    client.post(
        "/api/setup/owner",
        json={"email": OWNER[0], "password": OWNER[1], "display_name": "Owner"},
    )
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    return client, audit, csrf


def test_resolve_finds_user_and_audits_the_lookup():
    client, audit, csrf = _owner_client()
    owner_id = client.get("/v1/admin/users").json()["users"][0]["id"]
    pseudonym = stable_pseudonym("usr", owner_id)

    response = client.post(
        "/v1/admin/audit/resolve-pseudonym",
        json={"pseudonym": pseudonym},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["user"]["id"] == owner_id
    assert body["user"]["email"] == OWNER[0]
    entry = audit.entries[-1]
    assert entry.action == "audit.pseudonym_resolved"
    assert entry.resource_id == owner_id
    assert entry.detail["found"] == "true"
    assert entry.detail["pseudonym"] == pseudonym


def test_resolve_miss_still_audits():
    client, audit, csrf = _owner_client()
    response = client.post(
        "/v1/admin/audit/resolve-pseudonym",
        json={"pseudonym": "usr_" + "0" * 16},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {"found": False}
    entry = audit.entries[-1]
    assert entry.action == "audit.pseudonym_resolved"
    assert entry.detail["found"] == "false"


def test_resolve_rejects_malformed_pseudonym():
    client, audit, csrf = _owner_client()
    response = client.post(
        "/v1/admin/audit/resolve-pseudonym",
        json={"pseudonym": "owner@example.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert audit.entries == []


def test_resolve_requires_configured_pepper():
    """Without the instance pepper a recomputation would only match
    pseudonyms this very process wrote — refuse instead of half-answering."""
    client, audit, csrf = _owner_client()
    configure_stable_pseudonyms(None)
    response = client.post(
        "/v1/admin/audit/resolve-pseudonym",
        json={"pseudonym": "usr_" + "0" * 16},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert "INQTRIX_PSEUDONYM_PEPPER" in response.json()["error"]["message"]
    assert audit.entries == []


def test_resolve_is_admin_gated():
    configure_stable_pseudonyms(PEPPER)
    client, audit = make_client()
    response = client.post(
        "/v1/admin/audit/resolve-pseudonym",
        json={"pseudonym": "usr_" + "0" * 16},
    )
    assert response.status_code == 401
    assert audit.entries == []
