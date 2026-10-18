"""Regression tests for auth, serialization and layering guards.

Each test pins one behaviour: the async-gate auth bypass, the AUTH_MODE
serialization round-trip, the UnknownAlgorithm 500 escape, the streaming
capability guard, principal threading into the chat path, and the
services-never-import-server layering rule.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.auth.principal import STATIC_PRINCIPAL, resolve_live_principal
from inqtrix.core.algorithms import AlgorithmRegistry
from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.container import build_container
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import AuthSettings, ServerSettings, Settings

from tests.contract._app import StubLLM, StubSearch, minimal_agent_result


def _register_app(**register_kwargs) -> TestClient:
    app = FastAPI()
    router = create_router()
    register_routes(
        router,
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=GroundedSearchResult,  # unused placeholder shape
        settings=Settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        **register_kwargs,
    )
    app.include_router(router)
    return TestClient(app)


# ------------------------------------------------------------------ #
# Guard: an async legacy gate is never silently skipped (auth bypass)
# ------------------------------------------------------------------ #


def test_async_legacy_gate_still_rejects_with_401():
    """An ``async def`` gate injected via the legacy seam must run."""

    async def async_gate() -> None:
        raise HTTPException(status_code=401, detail="async gate says no")

    client = _register_app(api_key_dependency=async_gate)
    response = client.get("/v1/runs")
    assert response.status_code == 401


def test_parameterized_legacy_gate_resolves_via_fastapi_di():
    """A gate with a FastAPI Header signature keeps its DI semantics."""

    def header_gate(authorization: str = Header(...)) -> None:
        if authorization != "Bearer legacy-token":
            raise HTTPException(status_code=401, detail="wrong header")

    client = _register_app(api_key_dependency=header_gate)

    rejected = client.get("/v1/runs")
    allowed = client.get(
        "/v1/runs", headers={"Authorization": "Bearer legacy-token"}
    )

    # FastAPI maps a missing required Header to 422; the load-bearing
    # assertions are: the gate RUNS (no silent bypass) and valid
    # credentials pass.
    assert rejected.status_code in (401, 422)
    assert allowed.status_code == 200


def test_sync_callable_gate_direct_resolution_keeps_working():
    from inqtrix.auth.api_key import CallableGateAuthProvider

    calls: list[str] = []

    def gate(request) -> None:
        calls.append("ran")

    provider = CallableGateAuthProvider(gate=gate)
    principal = provider.resolve_principal(object())
    assert principal is STATIC_PRINCIPAL
    assert calls == ["ran"]


def test_async_gate_direct_resolution_fails_loudly():
    from inqtrix.auth.api_key import CallableGateAuthProvider

    async def async_gate(request) -> None:
        raise AssertionError("never awaited here")

    provider = CallableGateAuthProvider(gate=async_gate)
    with pytest.raises(TypeError, match="build_principal_dependency"):
        provider.resolve_principal(object())


@pytest.mark.asyncio
async def test_callable_gate_live_resolution_replays_parameterized_di():
    """An SSE frame re-runs Header injection instead of calling DI directly."""
    from starlette.requests import Request

    from inqtrix.auth.api_key import CallableGateAuthProvider

    calls: list[str] = []

    async def gate(authorization: str = Header(...)) -> None:
        calls.append(authorization)
        if authorization != "Bearer current":
            raise HTTPException(status_code=401, detail="revoked")

    app = FastAPI()
    dependency = CallableGateAuthProvider(
        gate=gate
    ).build_principal_dependency()

    def request_for(value: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/events",
                "raw_path": b"/events",
                "query_string": b"",
                "headers": [(b"authorization", value.encode())],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "root_path": "",
                "app": app,
            }
        )

    assert await resolve_live_principal(
        dependency, request_for("Bearer current")
    ) is STATIC_PRINCIPAL
    with pytest.raises(HTTPException) as rejected:
        await resolve_live_principal(dependency, request_for("Bearer old"))
    assert rejected.value.status_code == 401
    assert calls == ["Bearer current", "Bearer old"]


# ------------------------------------------------------------------ #
# Guard: the AUTH_MODE sentinel survives serialization round-trips
# ------------------------------------------------------------------ #


def test_settings_round_trip_preserves_inferred_open_mode():
    from inqtrix.auth.principal import resolve_auth_mode

    original = Settings()
    assert resolve_auth_mode(original.auth, original.server) == "none"

    rebuilt = Settings.model_validate(original.model_dump())
    assert resolve_auth_mode(rebuilt.auth, rebuilt.server) == "none"


def test_settings_round_trip_preserves_inferred_apikey_mode():
    from inqtrix.auth.principal import resolve_auth_mode

    original = Settings(server=ServerSettings(api_key="secret"))
    rebuilt = Settings.model_validate(original.model_dump())
    assert resolve_auth_mode(rebuilt.auth, rebuilt.server) == "apikey"


def test_raw_mode_field_reports_infer_not_a_fake_mode():
    assert Settings().auth.mode == "infer"
    assert AuthSettings(mode="none").mode == "none"


# ------------------------------------------------------------------ #
# Guard: a custom registry without builtins answers 400, never bare-500
# ------------------------------------------------------------------ #


def _client_with_registry(registry: AlgorithmRegistry) -> TestClient:
    app = FastAPI()
    router = create_router()
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        registry=registry,
    )
    from inqtrix.server.routers import chat, runs

    router.include_router(chat.build_router(container))
    router.include_router(runs.build_router(container))
    app.include_router(router)
    return TestClient(app)


def test_modeless_request_on_custom_registry_returns_400_envelope():
    class _CustomAlgorithm:
        id = "custom_only"
        display_name = "Custom"

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            raise NotImplementedError

    registry = AlgorithmRegistry()
    registry.register(_CustomAlgorithm())
    client = _client_with_registry(registry)

    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "mode must be 'custom_only'",
            "type": "invalid_request_error",
        }
    }


# ------------------------------------------------------------------ #
# Guard: streaming rejects an algorithm that is not a chat peer
# ------------------------------------------------------------------ #


def test_streaming_rejects_algorithm_without_chat_completions_capability():
    """Streaming dispatches through the registry, so the gate is
    ``supports_chat_completions``. An algorithm that is not a chat peer (e.g.
    workspace_agent, which needs run_id + park) is rejected with a loud 400
    rather than dispatched into a context it will fail in."""
    class _NonChatAlgorithm:
        id = "research"
        display_name = "Custom non-chat algorithm"

        def capabilities(self) -> dict:
            return {"supports_chat_completions": False}

        def run(self, request, *, runtime, context):
            raise NotImplementedError

    registry = AlgorithmRegistry()
    registry.register(_NonChatAlgorithm())
    client = _client_with_registry(registry)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": True,
        },
    )

    assert response.status_code == 400
    assert "stream=true" in response.json()["error"]["message"]


# ------------------------------------------------------------------ #
# Guard: chat executions carry the resolved principal
# ------------------------------------------------------------------ #


def test_chat_execution_context_carries_static_principal(monkeypatch):
    from tests.contract._app import make_contract_client

    seen: dict[str, object] = {}
    original_execute = web_research_module._execute_graph

    def spying_execute(request, context):
        seen["principal"] = context.principal
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "_execute_graph", spying_execute)

    with make_contract_client(
        server_settings=ServerSettings(api_key="secret-token"),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    assert seen["principal"] is STATIC_PRINCIPAL
    assert original_execute is not spying_execute


# ------------------------------------------------------------------ #
# Guard: services do not import the server package (layering)
# ------------------------------------------------------------------ #


def test_services_package_does_not_import_server_at_runtime():
    """Import every services module fresh and assert no server modules load.

    The worker process (and the future knowledge service) must be able
    to import the service layer without dragging in the HTTP server
    package. ``inqtrix.server.overrides`` stays importable as a
    deprecation shim, but the dependency direction is
    services <- server, never services -> server.
    """
    import importlib
    import pkgutil

    import inqtrix.services as services_pkg

    # Snapshot and RESTORE the original module objects afterwards —
    # leaving freshly re-imported inqtrix modules in sys.modules would
    # break module identity (isinstance checks, monkeypatch targets)
    # for every later test in the session (Test-Order-Hygiene,
    # Gotcha #1 class of failures).
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("inqtrix")
    }
    for name in saved:
        del sys.modules[name]
    try:
        importlib.import_module("inqtrix.services")
        for module_info in pkgutil.iter_modules(services_pkg.__path__):
            importlib.import_module(f"inqtrix.services.{module_info.name}")
        loaded_server_modules = [
            name for name in sys.modules if name.startswith("inqtrix.server")
        ]
        assert loaded_server_modules == [], (
            "services imports pulled in server modules: "
            f"{loaded_server_modules}"
        )
    finally:
        for name in [n for n in sys.modules if n.startswith("inqtrix")]:
            del sys.modules[name]
        sys.modules.update(saved)
