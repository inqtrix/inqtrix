"""Webserver-Spiegel zu provider_stacks/azure_foundry_web_search.py.

Mounts the OpenAI-compatible Inqtrix server on top of an
``AzureOpenAILLM`` + ``AzureFoundryWebSearch`` stack (Variante B —
Foundry Web Search Agent through the project-scoped Responses API
with ``agent_reference``). Provider construction is **1:1 identical**
to the library example.

Required environment variables
------------------------------
- ``AZURE_OPENAI_API_KEY``           (LLM — Option A default)
- ``AZURE_OPENAI_ENDPOINT``
- ``AZURE_OPENAI_DEPLOYMENT_NAME``
- ``AZURE_AI_PROJECT_ENDPOINT``
- ``WEB_SEARCH_AGENT_NAME``

Optional (parity with provider_stacks):
- ``WEB_SEARCH_AGENT_VERSION``
- ``AZURE_AI_PROJECT_API_KEY``
- ``AZURE_OPENAI_CLAIM_EXTRACT_DEPLOYMENT_NAME``
- ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``

Foundry token caveat
--------------------
The Foundry web-search provider mints a bearer token at constructor
time (~60-75 min lifetime, auto-refresh via ``azure.identity``). For
long-running servers keep a single per-process instance and rely on
the credential's automatic refresh; chronic 401s after long uptime are
the documented signal to restart the process.

Optional logging / server / security: see ``examples/webserver_stacks/README.md``
(``INQTRIX_LOG_*``, ``OBSERVABILITY_PROFILE``; ``forensic`` lineage needs
``INQTRIX_LOG_LEVEL=DEBUG``).

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
from inqtrix.logging_config import build_uvicorn_log_config, configure_logging
from inqtrix.providers.base import ProviderContext
from inqtrix.server import create_app
from inqtrix.server.security import resolve_tls_paths
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
)


load_dotenv()

_INQTRIX_LOG_PATH = configure_logging(
    enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
    level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
    console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
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

    # -- LLM Provider ---------------------------------------------------------
    #
    # Option A: API Key (default)
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
    )

    # -- Option B: Service Principal — direct constructor args ----------------
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     tenant_id=os.environ["AZURE_TENANT_ID"],
    #     client_id=os.environ["AZURE_CLIENT_ID"],
    #     client_secret=os.environ["AZURE_CLIENT_SECRET"],
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option C: Service Principal — manual token provider ------------------
    # from azure.identity import ClientSecretCredential, get_bearer_token_provider
    # credential = ClientSecretCredential(
    #     tenant_id=os.environ["AZURE_TENANT_ID"],
    #     client_id=os.environ["AZURE_CLIENT_ID"],
    #     client_secret=os.environ["AZURE_CLIENT_SECRET"],
    # )
    # token_provider = get_bearer_token_provider(
    #     credential, "https://cognitiveservices.azure.com/.default",
    # )
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option D: Existing token provider ------------------------------------
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=existing_token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option E: DefaultAzureCredential -------------------------------------
    # from azure.identity import DefaultAzureCredential
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     credential=DefaultAzureCredential(),
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Search Provider ------------------------------------------------------
    # Auth order via azure-ai-projects: project API key (api-key header),
    # Service Principal, then DefaultAzureCredential.
    project_api_key = _optional_env("AZURE_AI_PROJECT_API_KEY")
    tenant_id = _optional_env("AZURE_TENANT_ID")
    client_id = _optional_env("AZURE_CLIENT_ID")
    client_secret = _optional_env("AZURE_CLIENT_SECRET")

    search_kwargs: dict[str, Any] = {
        "project_endpoint": project_endpoint,
        "agent_name": web_agent_name,
    }
    if web_agent_version:
        search_kwargs["agent_version"] = web_agent_version
    if project_api_key:
        search_kwargs["api_key"] = project_api_key
    elif tenant_id and client_id and client_secret:
        search_kwargs["tenant_id"] = tenant_id
        search_kwargs["client_id"] = client_id
        search_kwargs["client_secret"] = client_secret

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
    if os.getenv("INQTRIX_LOG_INCLUDE_WEB", "true").lower() != "false":
        uvicorn_kwargs["log_config"] = build_uvicorn_log_config(
            _INQTRIX_LOG_PATH,
            web_level=os.getenv("INQTRIX_LOG_WEB_LEVEL", "INFO"),
        )
    uvicorn.run(app, **uvicorn_kwargs)


_ = (AgentConfig, ResearchAgent)


if __name__ == "__main__":
    main()
