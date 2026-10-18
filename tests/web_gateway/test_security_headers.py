"""The baseline headers every edge response must carry."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix_web_gateway.security_headers import SecurityHeadersMiddleware


def _client(external_scheme: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/")
    def root() -> dict:
        return {"ok": True}

    @app.get("/guest")
    def guest():
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"referrer-policy": "no-referrer"})

    app.add_middleware(SecurityHeadersMiddleware, external_scheme=external_scheme)
    return TestClient(app)


def test_authenticated_shell_is_not_framable():
    """An authenticated tool that can be framed can be clickjacked."""
    answer = _client("https").get("/")

    assert answer.headers["x-frame-options"] == "DENY"
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_hsts_only_when_the_deployment_is_https():
    """Announcing HSTS from a plain-HTTP edge promises what it cannot keep."""
    over_tls = _client("https").get("/")
    plain = _client("http").get("/")
    unset = _client(None).get("/")

    assert over_tls.headers["strict-transport-security"].startswith("max-age=")
    assert "strict-transport-security" not in plain.headers
    assert "strict-transport-security" not in unset.headers


def test_hsts_does_not_bind_sibling_hosts():
    """includeSubDomains would speak for hosts this application does not own."""
    value = _client("https").get("/").headers["strict-transport-security"]

    assert "includeSubDomains" not in value
    assert "preload" not in value


def test_a_stricter_route_policy_survives():
    """The guest surface sets no-referrer; the baseline must not weaken it."""
    answer = _client("https").get("/guest")

    assert answer.headers["referrer-policy"] == "no-referrer"
    assert answer.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("scheme", ["https", "http", None])
def test_every_response_carries_the_frame_policy(scheme: str | None):
    assert _client(scheme).get("/").headers["x-frame-options"] == "DENY"
