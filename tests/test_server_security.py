"""Tests for opt-in TLS, bearer API-key, and CORS security helpers."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.server.security import (
    make_api_key_dependency,
    make_cors_middleware_kwargs,
    resolve_tls_paths,
)
from inqtrix.settings import AgentSettings, ModelSettings, ServerSettings, Settings


# ------------------------------------------------------------------ #
# Stubs (network-free providers)
# ------------------------------------------------------------------ #


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


def _make_settings(**server_kwargs) -> Settings:
    return Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(**server_kwargs),
    )


# ------------------------------------------------------------------ #
# resolve_tls_paths
# ------------------------------------------------------------------ #


def test_resolve_tls_paths_returns_pair_when_both_set():
    settings = _make_settings(tls_keyfile="/etc/ssl/key.pem", tls_certfile="/etc/ssl/cert.pem")
    assert resolve_tls_paths(settings.server) == ("/etc/ssl/key.pem", "/etc/ssl/cert.pem")


def test_resolve_tls_paths_returns_none_when_unset():
    settings = _make_settings()
    assert resolve_tls_paths(settings.server) is None


def test_resolve_tls_paths_raises_on_partial_keyfile_only():
    settings = _make_settings(tls_keyfile="/etc/ssl/key.pem")
    with pytest.raises(RuntimeError, match="must be set together"):
        resolve_tls_paths(settings.server)


def test_resolve_tls_paths_raises_on_partial_certfile_only():
    settings = _make_settings(tls_certfile="/etc/ssl/cert.pem")
    with pytest.raises(RuntimeError, match="must be set together"):
        resolve_tls_paths(settings.server)


# ------------------------------------------------------------------ #
# make_api_key_dependency — unit
# ------------------------------------------------------------------ #


def test_make_api_key_dependency_returns_none_when_unset():
    settings = _make_settings()
    assert make_api_key_dependency(settings.server) is None


def test_make_api_key_dependency_returns_callable_when_set():
    settings = _make_settings(api_key="secret-token-123")
    dep = make_api_key_dependency(settings.server)
    assert callable(dep)


# ------------------------------------------------------------------ #
# Bearer dependency — integration through TestClient + protected route
# ------------------------------------------------------------------ #


def _make_app_with_api_key(api_key: str = "secret-token-123") -> TestClient:
    settings = _make_settings(api_key=api_key)
    providers = ProviderContext(llm=_DummyLLM(), search=_DummySearch())
    app = create_app(settings=settings, providers=providers)
    return TestClient(app)


def test_api_key_dependency_accepts_correct_bearer(monkeypatch):
    import inqtrix.research.web_research as web_research_module

    def fake_run(*args, **kwargs):
        return {
            "answer": "ok",
            "result_state": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)
    with _make_app_with_api_key() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
            headers={"Authorization": "Bearer secret-token-123"},
        )
    assert response.status_code == 200


def test_api_key_dependency_rejects_missing_header_with_401():
    with _make_app_with_api_key() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}]},
        )
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_api_key_dependency_rejects_wrong_key_with_401():
    with _make_app_with_api_key() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}]},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert response.status_code == 401


def test_api_key_dependency_protects_native_runs_routes():
    with _make_app_with_api_key() as client:
        missing = client.get("/v1/runs")
        allowed = client.get(
            "/v1/runs",
            headers={"Authorization": "Bearer secret-token-123"},
        )

    assert missing.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["object"] == "list"


def test_api_key_dependency_rejects_malformed_authorization():
    with _make_app_with_api_key() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}]},
            headers={"Authorization": "Basic deadbeef"},
        )
    assert response.status_code == 401


def test_api_key_dependency_uses_constant_time_compare():
    """Verify hmac.compare_digest is the comparison primitive (timing safety)."""
    with patch("inqtrix.auth.api_key.hmac.compare_digest") as mock_cmp:
        mock_cmp.return_value = True
        with _make_app_with_api_key() as client:
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Hallo"}]},
                headers={"Authorization": "Bearer any-value"},
            )
    assert mock_cmp.called


def test_health_endpoint_remains_unauthenticated_when_api_key_set():
    """Liveness probe path must keep working without credentials."""
    with _make_app_with_api_key() as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_models_endpoint_remains_unauthenticated_when_api_key_set():
    """Model discovery path must keep working without credentials."""
    with _make_app_with_api_key() as client:
        response = client.get("/v1/models")
    assert response.status_code == 200


# ------------------------------------------------------------------ #
# CORS
# ------------------------------------------------------------------ #


def test_make_cors_kwargs_returns_none_when_unset():
    settings = _make_settings()
    assert make_cors_middleware_kwargs(settings.server) is None


def test_make_cors_kwargs_parses_comma_separated_origins():
    settings = _make_settings(
        cors_origins="https://app1.example, https://app2.example"
    )
    kwargs = make_cors_middleware_kwargs(settings.server)
    assert kwargs is not None
    assert kwargs["allow_origins"] == [
        "https://app1.example",
        "https://app2.example",
    ]
    assert "POST" in kwargs["allow_methods"]
    assert "Authorization" in kwargs["allow_headers"]
    assert "X-Inqtrix-Expected-User-Id" in kwargs["allow_headers"]
    assert "X-Inqtrix-Workspace-Id" in kwargs["allow_headers"]
    assert kwargs["allow_credentials"] is True


def test_make_cors_kwargs_warns_on_wildcard(caplog):
    settings = _make_settings(cors_origins="*")
    inqtrix_logger = logging.getLogger("inqtrix")
    inqtrix_logger.addHandler(caplog.handler)
    previous_level = inqtrix_logger.level
    inqtrix_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            kwargs = make_cors_middleware_kwargs(settings.server)
    finally:
        inqtrix_logger.removeHandler(caplog.handler)
        inqtrix_logger.setLevel(previous_level)

    assert kwargs is not None
    assert any("CORS wildcard" in rec.getMessage() for rec in caplog.records)


def test_create_app_installs_cors_when_origins_set():
    settings = _make_settings(cors_origins="https://app1.example")
    providers = ProviderContext(llm=_DummyLLM(), search=_DummySearch())
    app = create_app(settings=settings, providers=providers)

    with TestClient(app) as client:
        # Preflight request — Origin + Access-Control-Request-Method headers.
        response = client.options(
            "/v1/models",
            headers={
                "Origin": "https://app1.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "https://app1.example"
