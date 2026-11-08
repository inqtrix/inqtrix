"""Smoke tests for every ``examples/webserver_stacks/*.py`` script.

Per stack we verify five things:

1. The module imports cleanly without env vars (env reads happen in
   ``build_app()``/``main()``, never at import time).
2. ``module.build_app()`` returns a FastAPI instance when minimum env
   vars are set and provider constructors are stubbed out.
3. ``GET /health`` returns 200 with the expected JSON envelope.
4. ``GET /v1/models`` returns the static OpenAI-compatible payload.
5. ``POST /v1/chat/completions`` (non-streaming, ``agent_run`` mocked)
   returns 200 with the OpenAI chat-completion shape.

Tests are parametrised so adding a new stack only needs an entry in
``_STACKS`` plus the matching env-var slot in
``tests/_webserver_helpers.py``.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi import FastAPI

import inqtrix.research.web_research as web_research_module
from tests._webserver_helpers import (
    _StubLLM,
    _StubSearch,
    import_stack_module,
    make_test_client,
    patch_provider_constructors,
    set_minimum_env,
)


_STACKS = [
    "litellm_perplexity",
    "anthropic_perplexity",
    "bedrock_perplexity",
    "azure_openai_perplexity",
    "azure_foundry_web_search",    "azure_knowledge_quickstart",
]


@pytest.fixture
def fake_agent_run(monkeypatch: pytest.MonkeyPatch):
    """Replace ``agent_run`` with a deterministic stub for chat completions."""

    def _fake_run(
        question,
        *,
        history,
        providers,
        strategies,
        settings,
        cancel_event,
    ):
        # The blocking chat path wires the route's disconnect event into
        # the graph; it arrives as a real, unset Event on a healthy call.
        assert cancel_event is not None
        assert not cancel_event.is_set()
        return {
            "answer": "stubbed answer",
            "result_state": {},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(web_research_module, "run_web_graph", _fake_run)
    return _fake_run


# ------------------------------------------------------------------ #
# Parametrised tests — run for every stack in _STACKS
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("stack", _STACKS)
def test_stack_imports(stack: str):
    """Module import must succeed before any env-var is set."""
    module = import_stack_module(stack)
    assert hasattr(module, "build_app")
    assert hasattr(module, "main")


def test_a_stack_imported_before_the_patch_still_builds_with_the_stubs(
    monkeypatch: pytest.MonkeyPatch,
):
    """The examples bind the provider classes when they are imported.

    An earlier import without the patch (the import test runs first) left the
    real classes bound, and every later test built real providers.
    """
    importlib.import_module("examples.webserver_stacks.azure_foundry_web_search")
    patch_provider_constructors(monkeypatch)

    module = import_stack_module("azure_foundry_web_search")

    assert module.AzureFoundryWebSearch is _StubSearch
    assert module.AzureOpenAILLM is _StubLLM


@pytest.mark.parametrize("stack", _STACKS)
def test_stack_build_app_returns_fastapi(
    stack: str, monkeypatch: pytest.MonkeyPatch
):
    set_minimum_env(monkeypatch, stack)
    patch_provider_constructors(monkeypatch)
    module = import_stack_module(stack)
    app = module.build_app()
    assert isinstance(app, FastAPI)


@pytest.mark.parametrize("stack", _STACKS)
def test_stack_health_endpoint(
    stack: str, monkeypatch: pytest.MonkeyPatch
):
    with make_test_client(stack, monkeypatch) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert "llm" in payload
    assert "search" in payload
    assert payload["llm"]["status"] == "ready"
    assert payload["search"]["status"] == "ready"


@pytest.mark.parametrize("stack", _STACKS)
def test_stack_models_endpoint(
    stack: str, monkeypatch: pytest.MonkeyPatch
):
    with make_test_client(stack, monkeypatch) as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == "research-agent"


@pytest.mark.parametrize("stack", _STACKS)
def test_stack_chat_completions_blocking(
    stack: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_run: Any,
):
    with make_test_client(stack, monkeypatch) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": False,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "stubbed answer"


# ------------------------------------------------------------------ #
# Multi-stack example — discovery + per-stack routing
# ------------------------------------------------------------------ #


def test_multi_stack_imports():
    module = import_stack_module("multi_stack")
    assert hasattr(module, "build_app")
    assert hasattr(module, "main")


def test_multi_stack_build_app_returns_fastapi(monkeypatch: pytest.MonkeyPatch):
    set_minimum_env(monkeypatch, "multi_stack")
    patch_provider_constructors(monkeypatch)
    module = import_stack_module("multi_stack")
    app = module.build_app()
    assert isinstance(app, FastAPI)


def test_multi_stack_discovery_lists_all_five_stacks(monkeypatch: pytest.MonkeyPatch):
    with make_test_client("multi_stack", monkeypatch) as client:
        response = client.get("/v1/stacks")
    assert response.status_code == 200
    payload = response.json()
    names = {s["name"] for s in payload["stacks"]}
    expected = {
        "litellm_perplexity",
        "anthropic_perplexity",
        "bedrock_perplexity",
        "azure_openai_perplexity",
        "azure_foundry_web_search",
    }
    assert names == expected, f"Unexpected stacks: {names ^ expected}"


def test_multi_stack_chat_completions_routes_to_named_stack(
    monkeypatch: pytest.MonkeyPatch, fake_agent_run: Any
):
    with make_test_client("multi_stack", monkeypatch) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": False,
                "stack": "anthropic_perplexity",
            },
        )
    assert response.status_code == 200


def test_multi_stack_chat_completions_unknown_stack_returns_400(
    monkeypatch: pytest.MonkeyPatch, fake_agent_run: Any
):
    with make_test_client("multi_stack", monkeypatch) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": False,
                "stack": "does_not_exist",
            },
        )
    assert response.status_code == 400
    assert "available_stacks" in response.json()["error"]
