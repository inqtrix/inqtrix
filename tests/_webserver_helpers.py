"""Shared test infrastructure for ``examples/webserver_stacks/*.py`` scripts.

Each webserver-stack example exposes a ``build_app() -> FastAPI``
function that constructs providers, settings and a FastAPI instance
via ``create_app(...)``. The tests in ``test_webserver_examples.py``
exercise every stack uniformly: import-only, ``build_app()`` returns
a FastAPI, and minimal ``/health`` / ``/v1/models`` /
``/v1/chat/completions`` round-trips work without touching the
network.

This helper hosts:

* :class:`_StubLLM` / :class:`_StubSearch` — copies of the patterns
  in ``tests/conftest.py``, kept here so the helper does not
  cross-import test fixtures.
* :func:`patch_provider_constructors` — monkeypatches the public
  provider classes (``LiteLLM``, ``PerplexitySearch``,
  ``AnthropicLLM``, ``BedrockLLM``, ``AzureOpenAILLM``, the four
  Azure search variants) so example scripts that read env-vars and
  build providers do not actually hit any SDK.
* :func:`set_minimum_env` — sets only the env-vars that each example
  needs to pass its ``_require_env`` calls. Stub providers receive
  the env values verbatim but never use them.
* :func:`make_test_client` — orchestrates the patches, builds the
  app via ``module.build_app()`` and returns a configured
  :class:`TestClient`.

The helper avoids autouse fixtures so callers stay explicit about
when stubs are installed.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.search_result import GroundedSearchResult


# ------------------------------------------------------------------ #
# Stub providers
# ------------------------------------------------------------------ #


class _StubLLM:
    """Network-free LLMProvider stub for webserver-example tests."""

    def __init__(self, *args, **kwargs) -> None:
        # Discard whatever Baukasten args the example passed; the stub
        # never touches them.
        self._init_args = args
        self._init_kwargs = kwargs

    def complete(self, *args, **kwargs):  # pragma: no cover — only via TestClient
        return ""


    def is_available(self) -> bool:
        return True


class _StubSearch:
    """Network-free SearchProvider stub for webserver-example tests."""

    def __init__(self, *args, **kwargs) -> None:
        self._init_args = args
        self._init_kwargs = kwargs

    def search(self, *args, **kwargs):  # pragma: no cover
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True

    @classmethod
    def create_agent(cls, **kwargs) -> "_StubSearch":  # pragma: no cover
        return cls(**kwargs)


# ------------------------------------------------------------------ #
# Patching helpers
# ------------------------------------------------------------------ #


_LLM_TARGETS = (
    "inqtrix.LiteLLM",
    "inqtrix.AnthropicLLM",
    "inqtrix.AzureOpenAILLM",
    "inqtrix.BedrockLLM",
)

_SEARCH_TARGETS = (
    "inqtrix.PerplexitySearch",
    "inqtrix.AzureFoundryWebSearch",
)


def patch_provider_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every public provider class with the matching stub.

    Patches both ``inqtrix.<Name>`` and the underlying submodule path
    so example scripts that import via either route get the stub.
    """
    import inqtrix

    for dotted in _LLM_TARGETS:
        _, attr = dotted.rsplit(".", 1)
        monkeypatch.setattr(inqtrix, attr, _StubLLM, raising=True)
    for dotted in _SEARCH_TARGETS:
        _, attr = dotted.rsplit(".", 1)
        monkeypatch.setattr(inqtrix, attr, _StubSearch, raising=True)


# Per-stack minimum env-var sets that satisfy each example's
# ``_require_env`` calls. Values are placeholders; stubs ignore them.
_MIN_ENV_BY_STACK: dict[str, dict[str, str]] = {
    "litellm_perplexity": {
        "LITELLM_API_KEY": "test-key",
        "LITELLM_BASE_URL": "http://localhost:4000/v1",
    },
    "anthropic_perplexity": {
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "PERPLEXITY_API_KEY": "test-perplexity-key",
    },
    "bedrock_perplexity": {
        "PERPLEXITY_API_KEY": "test-perplexity-key",
        "AWS_REGION": "eu-central-1",
    },
    "azure_openai_perplexity": {
        "AZURE_OPENAI_API_KEY": "test-aoai-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
        "PERPLEXITY_API_KEY": "test-perplexity-key",
    },
    "azure_knowledge_quickstart": {
        "AZURE_OPENAI_API_KEY": "test-aoai-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
        "AZURE_AI_PROJECT_ENDPOINT": (
            "https://example.services.ai.azure.com/api/projects/test"
        ),
        "AZURE_AI_PROJECT_API_KEY": "test-project-key",
        "PERPLEXITY_API_KEY": "test-perplexity-key",
    },
    "azure_foundry_web_search": {
        "AZURE_OPENAI_API_KEY": "test-aoai-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
        "AZURE_AI_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "WEB_SEARCH_AGENT_NAME": "test-web-search-agent",
    },
    # The multi-stack example needs every env var that any single-stack
    # builder consumes — we union the per-stack sets so all 5 stacks
    # successfully register inside its build_stacks() call.
    "multi_stack": {
        "LITELLM_API_KEY": "test-key",
        "LITELLM_BASE_URL": "http://localhost:4000/v1",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "PERPLEXITY_API_KEY": "test-perplexity-key",
        "AWS_REGION": "eu-central-1",
        "AZURE_OPENAI_API_KEY": "test-aoai-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
        "AZURE_AI_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "WEB_SEARCH_AGENT_NAME": "test-web-search-agent",
    },
}


def set_minimum_env(monkeypatch: pytest.MonkeyPatch, stack_name: str) -> None:
    """Set only the env-vars required by ``stack_name``'s ``_require_env``."""
    for key, value in _MIN_ENV_BY_STACK[stack_name].items():
        monkeypatch.setenv(key, value)


def import_stack_module(stack_name: str) -> Any:
    """Import an ``examples.webserver_stacks.<stack_name>`` module fresh."""
    module_path = f"examples.webserver_stacks.{stack_name}"
    return importlib.import_module(module_path)


def make_test_client(stack_name: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a fully wired TestClient for the given webserver stack.

    Args:
        stack_name: Module slug under ``examples.webserver_stacks``.
        monkeypatch: pytest fixture used to install env + provider stubs.

    Returns:
        A :class:`TestClient` bound to the example's ``build_app()``.
    """
    set_minimum_env(monkeypatch, stack_name)
    patch_provider_constructors(monkeypatch)
    module = import_stack_module(stack_name)
    app = module.build_app()
    assert isinstance(app, FastAPI)
    return TestClient(app)
