"""Webserver-Spiegel zu provider_stacks/azure_foundry_web_search.py.

Mounts the OpenAI-compatible Inqtrix server on top of an
``AzureOpenAILLM`` + ``AzureFoundryWebSearch`` stack (Variante B —
Foundry Web Search Agent through the project-scoped Responses API
with ``agent_reference``). Provider construction is **1:1 identical**
to the library example.

Required environment variables
------------------------------
- ``AZURE_OPENAI_API_KEY``           (LLM)
- ``AZURE_OPENAI_ENDPOINT``
- ``AZURE_AI_PROJECT_ENDPOINT``
- ``AZURE_AI_PROJECT_API_KEY``       (web search, Foundry project key)
- ``WEB_SEARCH_AGENT_NAME``

Optional (parity with provider_stacks):
- ``WEB_SEARCH_AGENT_VERSION``
- ``AZURE_OPENAI_CONTEXT_WINDOW_TOKENS``  (context window of the deployments; default 128000)

Deployments
-----------
The Azure deployment names are set in the provider builder below as three
model tiers (high for the answer, mid for plan / evaluate / direct chat, fast
for classify and claim extraction), as in the library example; replace them
with your deployment names.

Optional logging / server / security: see ``examples/webserver_stacks/README.md``
(``INQTRIX_LOG_*``, ``OBSERVABILITY_PROFILE``).

Run with::

    uv sync
    uv run python examples/webserver_stacks/azure_foundry_web_search.py
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AgentConfig,
    AzureFoundryWebSearch,
    AzureOpenAILLM,
    ReportProfile,
    ResearchAgent,
)
from inqtrix.logging_config import configure_logging, log_status_line
from inqtrix.providers.base import ProviderContext
from inqtrix.server import create_app
from inqtrix.server.security import resolve_tls_paths
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    check_retired_settings,
)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _build_providers() -> ProviderContext:
    """Build AzureOpenAILLM + AzureFoundryWebSearch — identical to the library example."""
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    project_endpoint = _require_env("AZURE_AI_PROJECT_ENDPOINT")
    web_agent_name = _require_env("WEB_SEARCH_AGENT_NAME")
    web_agent_version = _optional_env("WEB_SEARCH_AGENT_VERSION")
    azure_context_window_tokens = int(
        os.environ.get("AZURE_OPENAI_CONTEXT_WINDOW_TOKENS", "128000").strip()
        or "128000"
    )

    # -- LLM Provider ---------------------------------------------------------
    #
    # Authentication: the API key of the Azure OpenAI resource.
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        # Three model tiers (Azure DEPLOYMENT names -- replace with yours).
        # Nodes map: answer -> high, plan/evaluate/direct_chat -> mid,
        # classify/claim_extract -> fast. Per-tier effort maps to
        # reasoning_effort; only the high tier reasons here (a graded effort
        # on a non-reasoning deployment is an Azure HTTP 400). A per-node
        # <node>_model arg overrides the tier. See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="gpt-5.4",
        tier_high_model="gpt-5.4",       tier_high_effort="medium",
        tier_mid_model="gpt-5.4",        tier_mid_effort="none",
        tier_fast_model="gpt-5.4-mini",  tier_fast_effort="none",
        # Optional: concrete models the chat/editor UI may offer for direct
        # selection (besides the high/mid/fast tiers above). These are the Azure
        # DEPLOYMENT names; naming each deployment after the canonical model id
        # lets the built-in model-card catalogue resolve it (display name,
        # context window, cost, reasoning levels) -- here gpt-5.4 / -mini /
        # -nano map to their cards. Omit this argument to keep the plain tier
        # picker; nothing else in the stack depends on it.
        selectable_models=["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"],
        context_window_tokens=azure_context_window_tokens,
    )

    # -- Search Provider ------------------------------------------------------
    # Auth: the Foundry project key, sent in the api-key header.
    project_api_key = _require_env("AZURE_AI_PROJECT_API_KEY")

    search_kwargs: dict[str, Any] = {
        "project_endpoint": project_endpoint,
        "agent_name": web_agent_name,
        "api_key": project_api_key,
    }
    if web_agent_version:
        search_kwargs["agent_version"] = web_agent_version

    search = AzureFoundryWebSearch(**search_kwargs)

    return ProviderContext(llm=llm, search=search)


def _build_settings() -> Settings:
    agent = AgentSettings(
        report_profile=ReportProfile.DEEP,
        max_rounds=4,
        confidence_stop=8,
        first_round_queries=8,
        answer_prompt_citations_max=500,
        max_total_seconds=3600,
        max_question_length=60_000,
        reasoning_timeout=600,
        search_timeout=600,
        claim_extract_timeout=600,
        high_risk_score_threshold=4,
        search_cache_maxsize=256,
        search_cache_ttl=3600,
    )
    return Settings(
        models=ModelSettings(),
        agent=agent,
        server=ServerSettings(),
    )


def build_app() -> FastAPI:
    providers = _build_providers()
    settings = _build_settings()
    return create_app(settings=settings, providers=providers)


def main() -> None:
    load_dotenv()
    resolved = configure_logging(service="api")
    log_status_line(resolved, retired=check_retired_settings())
    app = build_app()
    settings = _build_settings()
    tls = resolve_tls_paths(settings.server)
    uvicorn_kwargs: dict[str, Any] = dict(
        host=os.getenv("INQTRIX_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("INQTRIX_SERVER_PORT", "5100")),
        workers=1,
        timeout_keep_alive=300,
    )
    if tls is not None:
        uvicorn_kwargs["ssl_keyfile"], uvicorn_kwargs["ssl_certfile"] = tls
    uvicorn_kwargs["log_config"] = None
    # Inqtrix reads X-Forwarded-For itself (INQTRIX_TRUSTED_PROXY_HOPS).
    uvicorn_kwargs["proxy_headers"] = False
    uvicorn.run(app, **uvicorn_kwargs)


_ = (AgentConfig, ResearchAgent)


if __name__ == "__main__":
    main()
