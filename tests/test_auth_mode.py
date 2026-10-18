"""Tests for AUTH_MODE resolution and the principal providers.

Covers the explicit-wins / infer-for-backwards-compat rule, the loud
rejection of contradictory configuration, and the principal identities
of the legacy modes (anonymous / ``__static__``).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from inqtrix.auth.api_key import ApiKeyAuthProvider, build_auth_provider
from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    STATIC_PRINCIPAL,
    NoneAuthProvider,
    resolve_auth_mode,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.settings import AuthSettings, ServerSettings, Settings


class _DummyLLM:
    def complete(self, *args, **kwargs):
        return "ok"

    def is_available(self) -> bool:
        return True


class _DummySearch:
    def search(self, *args, **kwargs):
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


class _FakeRequest:
    """Minimal request stand-in carrying only headers."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


# ------------------------------------------------------------------ #
# resolve_auth_mode
# ------------------------------------------------------------------ #


def test_mode_inferred_none_without_api_key():
    assert resolve_auth_mode(AuthSettings(), ServerSettings()) == "none"


def test_mode_inferred_apikey_with_api_key():
    assert (
        resolve_auth_mode(AuthSettings(), ServerSettings(api_key="secret"))
        == "apikey"
    )


def test_explicit_none_wins_over_configured_key(caplog):
    import logging

    inqtrix_logger = logging.getLogger("inqtrix")
    inqtrix_logger.addHandler(caplog.handler)
    previous_level = inqtrix_logger.level
    inqtrix_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            mode = resolve_auth_mode(
                AuthSettings(mode="none"), ServerSettings(api_key="secret")
            )
    finally:
        inqtrix_logger.removeHandler(caplog.handler)
        inqtrix_logger.setLevel(previous_level)

    assert mode == "none"
    assert any("disables the API key gate" in rec.getMessage() for rec in caplog.records)


def test_explicit_apikey_without_key_raises():
    with pytest.raises(RuntimeError, match="INQTRIX_SERVER_API_KEY"):
        resolve_auth_mode(AuthSettings(mode="apikey"), ServerSettings())


def test_explicit_oidc_without_connection_settings_raises():
    with pytest.raises(RuntimeError, match="INQTRIX_OIDC_ISSUER"):
        resolve_auth_mode(AuthSettings(mode="oidc"), ServerSettings())


def test_explicit_oidc_with_connection_settings_resolves():
    settings = AuthSettings(
        mode="oidc",
        oidc_issuer="http://127.0.0.1:5556/dex",
        oidc_client_id="inqtrix-local",
        oidc_client_secret="dev-secret",
        session_secret="dev-session-secret",
        pat_pepper="dev-pat-pepper",
        pseudonym_pepper="dev-pseudonym-pepper",
    )
    assert resolve_auth_mode(settings, ServerSettings()) == "oidc"


def test_explicit_oidc_without_pat_pepper_is_rejected():
    """The pepper is mandatory from first boot — discovering at
    token-creation time that hashes were minted pepperless would be
    unrecoverable without invalidating every token."""
    settings = AuthSettings(
        mode="oidc",
        oidc_issuer="http://127.0.0.1:5556/dex",
        oidc_client_id="inqtrix-local",
        oidc_client_secret="dev-secret",
        session_secret="dev-session-secret",
    )
    with pytest.raises(RuntimeError, match="INQTRIX_PAT_PEPPER"):
        resolve_auth_mode(settings, ServerSettings())


def test_explicit_oidc_without_pseudonym_pepper_is_rejected():
    """Multi-user SSO is exactly where subject correlation across
    processes (logs <-> audit <-> traces) must not silently degrade to
    per-process pseudonyms, so the pepper is required from first boot."""
    settings = AuthSettings(
        mode="oidc",
        oidc_issuer="http://127.0.0.1:5556/dex",
        oidc_client_id="inqtrix-local",
        oidc_client_secret="dev-secret",
        session_secret="dev-session-secret",
        pat_pepper="dev-pat-pepper",
    )
    with pytest.raises(RuntimeError, match="INQTRIX_PSEUDONYM_PEPPER"):
        resolve_auth_mode(settings, ServerSettings())


# ------------------------------------------------------------------ #
# Providers
# ------------------------------------------------------------------ #


def test_none_provider_resolves_anonymous_principal():
    provider = NoneAuthProvider()
    principal = provider.resolve_principal(_FakeRequest())
    assert principal is ANONYMOUS_PRINCIPAL
    assert principal.user_id is None
    assert principal.kind == "anonymous"


def test_api_key_provider_resolves_static_principal_on_valid_bearer():
    provider = ApiKeyAuthProvider(api_key="secret-token")
    principal = provider.resolve_principal(
        _FakeRequest({"Authorization": "Bearer secret-token"})
    )
    assert principal is STATIC_PRINCIPAL
    assert principal.user_id is None
    assert principal.kind == "static"


def test_api_key_provider_rejects_wrong_bearer_with_401():
    provider = ApiKeyAuthProvider(api_key="secret-token")
    with pytest.raises(HTTPException) as excinfo:
        provider.resolve_principal(_FakeRequest({"Authorization": "Bearer wrong"}))
    assert excinfo.value.status_code == 401
    assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}


def test_api_key_provider_rejects_empty_key_at_construction():
    with pytest.raises(ValueError, match="non-empty api_key"):
        ApiKeyAuthProvider(api_key="  ")


def test_build_auth_provider_bridges_settings_to_provider():
    open_settings = Settings(server=ServerSettings())
    gated_settings = Settings(server=ServerSettings(api_key="secret"))
    assert build_auth_provider(open_settings).mode == "none"
    assert build_auth_provider(gated_settings).mode == "apikey"


# ------------------------------------------------------------------ #
# create_app integration
# ------------------------------------------------------------------ #


def _make_app(settings: Settings):
    providers = ProviderContext(llm=_DummyLLM(), search=_DummySearch())
    return create_app(settings=settings, providers=providers)


def test_health_reports_auth_mode_apikey():
    from fastapi.testclient import TestClient

    app = _make_app(Settings(server=ServerSettings(api_key="secret")))
    with TestClient(app) as client:
        payload = client.get("/health").json()
    assert payload["auth_required"] is True
    assert payload["auth_mode"] == "apikey"


def test_health_reports_auth_mode_none():
    from fastapi.testclient import TestClient

    app = _make_app(Settings())
    with TestClient(app) as client:
        payload = client.get("/health").json()
    assert payload["auth_required"] is False
    assert payload["auth_mode"] == "none"


def test_explicit_none_mode_opens_gated_routes_despite_key():
    from fastapi.testclient import TestClient

    settings = Settings(
        server=ServerSettings(api_key="secret"),
        auth=AuthSettings(mode="none"),
    )
    app = _make_app(settings)
    with TestClient(app) as client:
        response = client.get("/v1/runs")
    assert response.status_code == 200


def test_create_app_raises_at_startup_on_contradictory_mode():
    settings = Settings(auth=AuthSettings(mode="apikey"))
    with pytest.raises(RuntimeError, match="INQTRIX_SERVER_API_KEY"):
        _make_app(settings)


def test_csv_frozenset_trims_lowercases_and_drops_empties():
    from inqtrix.auth.api_key import _csv_frozenset

    assert _csv_frozenset("a, b ,, c") == frozenset({"a", "b", "c"})
    assert _csv_frozenset("") == frozenset()
    assert _csv_frozenset("Corp.Example, Other.Org", lower=True) == frozenset(
        {"corp.example", "other.org"}
    )
