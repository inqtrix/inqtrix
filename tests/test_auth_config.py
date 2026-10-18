"""Tests for the public auth-config discovery endpoint.

The endpoint reads only ``container.auth_provider``, so a SimpleNamespace
container is enough to drive it per mode without the full app container.
"""

from __future__ import annotations

import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.server.routers.auth_config import _login_methods, build_router


def _client(provider: object) -> TestClient:
    container = types.SimpleNamespace(auth_provider=provider)
    app = FastAPI()
    app.include_router(build_router(container))
    return TestClient(app)


def test_login_methods_per_mode():
    assert _login_methods("none", None) == []
    assert _login_methods("apikey", None) == [
        {"kind": "apikey", "label": "API key"}
    ]
    assert _login_methods("oidc", "Okta") == [{"kind": "sso", "label": "Okta"}]
    assert _login_methods("oidc", None) == [{"kind": "sso", "label": "SSO"}]
    assert _login_methods("local", None)[0]["identifier"] == "email"


def test_config_none_mode_is_open_and_no_store():
    from inqtrix.auth.principal import NoneAuthProvider

    response = _client(NoneAuthProvider()).get("/api/auth/config")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["auth_mode"] == "none"
    assert body["auth_required"] is False
    assert body["login_methods"] == []
    assert body["pat_available"] is False
    assert body["csrf_required"] is False
    assert body["provider_name"] is None


def test_config_apikey_mode():
    from inqtrix.auth.api_key import ApiKeyAuthProvider

    body = _client(ApiKeyAuthProvider(api_key="k")).get(
        "/api/auth/config"
    ).json()
    assert body["auth_mode"] == "apikey"
    assert body["auth_required"] is True
    assert body["login_methods"][0]["kind"] == "apikey"
    assert body["csrf_required"] is False


def test_config_oidc_mode_surfaces_provider_name():
    from inqtrix.auth.oidc import OidcAuthProvider
    from inqtrix.auth.sessions import MemoryFlowStore, MemorySessionStore

    provider = OidcAuthProvider(
        client=None,
        sessions=MemorySessionStore(),
        flows=MemoryFlowStore(),
        session_secret="s",
        session_max_age_seconds=3600,
        provider_name="Okta",
        secure_cookies=False,
    )
    body = _client(provider).get("/api/auth/config").json()
    assert body["auth_mode"] == "oidc"
    assert body["provider_name"] == "Okta"
    assert body["login_methods"] == [{"kind": "sso", "label": "Okta"}]
    assert body["supports_logout"] is True
    assert body["csrf_required"] is True
    assert body["csrf_header"] == "X-CSRF-Token"


def test_config_local_mode_reports_owner_setup_and_self_service():
    from inqtrix.auth.credentials import (
        LocalAuthenticator,
        MemoryCredentialStore,
    )
    from inqtrix.auth.local import LocalAuthProvider
    from inqtrix.auth.sessions import MemoryFlowStore, MemorySessionStore

    credentials = MemoryCredentialStore()
    provider = LocalAuthProvider(
        authenticator=LocalAuthenticator(store=credentials),
        credentials=credentials,
        registration="open",
        sessions=MemorySessionStore(),
        flows=MemoryFlowStore(),
        session_secret="s",
        session_max_age_seconds=3600,
        secure_cookies=False,
    )
    body = _client(provider).get("/api/auth/config").json()
    assert body["auth_mode"] == "local"
    assert body["login_methods"][0]["identifier"] == "email"
    assert body["registration"]["self_service"] is True
    # Empty credential store -> first-run owner setup is still required.
    assert body["registration"]["needs_owner"] is True
