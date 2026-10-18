"""Native local email/password auth (INQTRIX_AUTH_MODE=local).

Covers the first-run owner bootstrap + permanent lock, password login,
the inherited session-cookie + CSRF machinery (local sessions are the
same ``oidc_session`` kind), uniform failure on bad credentials, and the
resolve_auth_mode fail-loud contract. Memory backend, ``secure_cookies``
off so the TestClient (http) keeps the cookies.
"""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.auth.principal import resolve_auth_mode
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import AuthSettings, Settings

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "correct-horse-battery"  # >= 12 chars


def make_client() -> TestClient:
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            oidc_insecure_dev_cookies=True,  # http test client keeps cookies
        )
    )
    provider = build_local_provider(settings)
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    principal_dep = provider.build_principal_dependency()

    @app.get("/v1/protected")
    async def protected_get(principal=Depends(principal_dep)):
        return {"sub": principal.user_id, "kind": principal.kind}

    @app.post("/v1/protected")
    async def protected_post(principal=Depends(principal_dep)):
        return {"sub": principal.user_id}

    return TestClient(app, base_url="http://127.0.0.1:5100")


def _create_owner(client: TestClient):
    return client.post(
        "/api/setup/owner",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "display_name": "Owner"},
    )


def test_resolve_auth_mode_local_requires_secrets():
    server = Settings().server
    with pytest.raises(RuntimeError, match="INQTRIX_SESSION_SECRET"):
        resolve_auth_mode(AuthSettings(mode="local"), server)
    # With both secrets it resolves cleanly.
    assert (
        resolve_auth_mode(
            AuthSettings(mode="local", session_secret="x" * 32, pat_pepper="y" * 32),
            server,
        )
        == "local"
    )


def test_setup_status_then_owner_then_lock():
    client = make_client()
    assert client.get("/api/setup/status").json() == {"needs_owner": True}

    created = _create_owner(client)
    assert created.status_code == 201
    assert created.json()["authenticated"] is True
    cookies = created.headers.get_list("set-cookie")
    assert len(cookies) == 2
    session_cookie = next(
        header for header in cookies if header.startswith("inqtrix_session=")
    )
    csrf_cookie = next(
        header for header in cookies if header.startswith("inqtrix_csrf=")
    )
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Path=/" in session_cookie
    assert "Path=/" in csrf_cookie
    assert "SameSite=lax" in session_cookie
    assert "SameSite=lax" in csrf_cookie
    # The owner is logged in immediately (session cookie set).
    assert client.get("/api/setup/status").json() == {"needs_owner": False}

    # Idempotent permanent lock: a second owner is refused.
    second = client.post(
        "/api/setup/owner",
        json={"email": "intruder@example.com", "password": "another-strong-pw"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["type"] == "setup_locked"


def test_owner_setup_logs_in_and_protected_route_resolves():
    client = make_client()
    _create_owner(client)
    info = client.get("/api/auth/session").json()
    assert info["authenticated"] is True
    assert info["user"]["email"] == OWNER_EMAIL
    assert info["csrf_token"]

    protected = client.get("/v1/protected")
    assert protected.status_code == 200
    # Local sessions use the same cookie-session kind as OIDC.
    assert protected.json()["kind"] == "oidc_session"


def test_session_bootstrap_refreshes_the_csrf_cookie():
    """A live opaque session can repair a token invalidated by secret rotation."""
    client = make_client()
    _create_owner(client)

    # Simulate the browser retaining a stale readable token while the opaque
    # session cookie remains valid.  The safe bootstrap must mint and set the
    # authoritative token again, not merely return it in JSON.
    client.cookies.set(
        "inqtrix_csrf", "stale-token", domain="127.0.0.1", path="/"
    )
    response = client.get("/api/auth/session")

    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert token and token != "stale-token"
    assert response.cookies.get("inqtrix_csrf") == token
    assert client.cookies.get(
        "inqtrix_csrf", domain="127.0.0.1", path="/"
    ) == token

    protected = client.post(
        "/v1/protected", headers={"X-CSRF-Token": token}
    )
    assert protected.status_code == 200


def test_login_local_after_logout():
    client = make_client()
    _create_owner(client)
    token = client.get("/api/auth/session").json()["csrf_token"]
    logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": token})
    assert logout.status_code == 200
    assert client.get("/api/auth/session").json() == {"authenticated": False}

    # Log back in with the owner credentials.
    login = client.post(
        "/api/auth/login/local",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["authenticated"] is True
    assert client.get("/v1/protected").status_code == 200


def test_login_local_accepts_identifier_field():
    client = make_client()
    _create_owner(client)
    client.post("/api/auth/logout", headers={
        "X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]
    })
    # The shared form posts "identifier"; it must work like "email".
    login = client.post(
        "/api/auth/login/local",
        json={"identifier": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert login.status_code == 200


def test_login_wrong_password_is_uniform_401():
    client = make_client()
    _create_owner(client)
    client.post("/api/auth/logout", headers={
        "X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]
    })
    bad = client.post(
        "/api/auth/login/local",
        json={"email": OWNER_EMAIL, "password": "wrong-but-long-enough"},
    )
    assert bad.status_code == 401
    unknown = client.post(
        "/api/auth/login/local",
        json={"email": "nobody@example.com", "password": "whatever-long-pw"},
    )
    assert unknown.status_code == 401
    # Same message for both — no account-existence oracle.
    assert bad.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_owner_setup_rejects_short_password_and_bad_email():
    client = make_client()
    short = client.post(
        "/api/setup/owner", json={"email": OWNER_EMAIL, "password": "short"}
    )
    assert short.status_code == 400
    bad_email = client.post(
        "/api/setup/owner",
        json={"email": "not-an-email", "password": OWNER_PASSWORD},
    )
    assert bad_email.status_code == 400
    # Nothing was created.
    assert client.get("/api/setup/status").json() == {"needs_owner": True}


def test_unsafe_method_requires_csrf():
    client = make_client()
    _create_owner(client)
    # POST without the CSRF header is rejected even with a valid session.
    without = client.post("/v1/protected")
    assert without.status_code == 403
    token = client.get("/api/auth/session").json()["csrf_token"]
    with_header = client.post("/v1/protected", headers={"X-CSRF-Token": token})
    assert with_header.status_code == 200


def test_csrf_warning_uses_only_a_pseudonymous_session_reference(caplog):
    client = make_client()
    setup = _create_owner(client)
    session_id = setup.cookies.get("inqtrix_session")
    assert session_id is not None and len(session_id) >= 32

    inqtrix_logger = logging.getLogger("inqtrix")
    inqtrix_logger.addHandler(caplog.handler)
    previous_level = inqtrix_logger.level
    previous_propagate = inqtrix_logger.propagate
    inqtrix_logger.setLevel(logging.WARNING)
    inqtrix_logger.propagate = False
    try:
        rejected = client.post(
            "/v1/protected",
            headers={"X-CSRF-Token": "synthetic-invalid-csrf-token"},
        )
    finally:
        inqtrix_logger.removeHandler(caplog.handler)
        inqtrix_logger.setLevel(previous_level)
        inqtrix_logger.propagate = previous_propagate

    assert rejected.status_code == 403
    assert rejected.json()["detail"]["error"]["type"] == "csrf_error"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "CSRF check failed" in record.getMessage()
    ]
    assert len(warnings) == 1
    warning = warnings[0]
    assert re.search(r"session_ref=ses_[0-9a-f]{16}(?:\s|$)", warning)
    assert "method=POST" in warning
    assert session_id not in warning
    assert session_id[:12] not in warning
    midpoint = len(session_id) // 2
    assert session_id[midpoint - 6 : midpoint + 6] not in warning
    assert session_id[-12:] not in warning


def test_change_password_flow():
    client = make_client()
    _create_owner(client)
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    changed = client.post(
        "/api/auth/password",
        json={"current_password": OWNER_PASSWORD, "new_password": "new-correct-horse-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200 and changed.json()["changed"] is True
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    # Old password no longer works; the new one does.
    assert (
        client.post(
            "/api/auth/login/local",
            json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login/local",
            json={"email": OWNER_EMAIL, "password": "new-correct-horse-1"},
        ).status_code
        == 200
    )


def test_change_password_requires_current_and_session():
    client = make_client()
    _create_owner(client)
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    # Wrong current password -> 401, the stored password is unchanged.
    wrong = client.post(
        "/api/auth/password",
        json={"current_password": "not-the-current", "new_password": "new-correct-horse-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert wrong.status_code == 401
    # Too-short new password -> 400.
    short = client.post(
        "/api/auth/password",
        json={"current_password": OWNER_PASSWORD, "new_password": "short"},
        headers={"X-CSRF-Token": csrf},
    )
    assert short.status_code == 400
    # No session at all -> rejected (no anonymous password change).
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    anon = client.post(
        "/api/auth/password",
        json={"current_password": OWNER_PASSWORD, "new_password": "new-correct-horse-1"},
    )
    assert anon.status_code in (401, 403)
