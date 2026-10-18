"""Webserver-Spiegel zu provider_stacks/azure_openai_perplexity.py.

Mounts the OpenAI-compatible Inqtrix server on top of an
``AzureOpenAILLM`` + ``PerplexitySearch`` stack. Provider construction
is **1:1 identical** to the library example, including all four Azure
auth modes available as Options A–E (only A is active; B–E are ready
to uncomment for Service Principal / Managed Identity setups).

Required environment variables
------------------------------
- ``PERPLEXITY_API_KEY``
- ``AZURE_OPENAI_API_KEY``           (Option A — default)
- ``AZURE_OPENAI_ENDPOINT``
- ``AZURE_OPENAI_DEPLOYMENT_NAME``

Optional (parity with provider_stacks):
- ``AZURE_OPENAI_CLAIM_EXTRACT_DEPLOYMENT_NAME``
- ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` (Option B/C)

Optional logging / server / security: see ``examples/webserver_stacks/README.md``
(``INQTRIX_LOG_*``, ``OBSERVABILITY_PROFILE``; ``forensic`` lineage needs
``INQTRIX_LOG_LEVEL=DEBUG``).

Run with::

    uv sync
    uv run python examples/webserver_stacks/azure_openai_perplexity.py
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AgentConfig,
    AzureOpenAILLM,
    PerplexitySearch,
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


def _build_providers() -> ProviderContext:
    """Build AzureOpenAILLM + PerplexitySearch — identical to the library example."""
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    perplexity_key = _require_env("PERPLEXITY_API_KEY")

    # -- LLM Provider ---------------------------------------------------------
    #
    # Option A: API Key (default)
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        # temperature=0.7,                                  # mutually exclusive with *_reasoning_effort
        # default_reasoning_effort="low",                   # classify, plan, evaluate, answer
        #   Allowed values: "none", "minimal", "low", "medium", "high", "xhigh".
        #   Per-value caveats (gpt-5.1 needs explicit value, "minimal" only on
        #   original gpt-5, "xhigh" only on gpt-5.1-codex-max, etc.) are enforced
        #   by Azure with HTTP 400. See docs/configuration/providers.md.
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
    )

    # -- Option B: Service Principal — direct constructor args ----------------
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     tenant_id=os.environ["AZURE_TENANT_ID"],
    #     client_id=os.environ["AZURE_CLIENT_ID"],
    #     client_secret=os.environ["AZURE_CLIENT_SECRET"],
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option C: Service Principal — manual token provider ------------------
    #
    # from azure.identity import ClientSecretCredential, get_bearer_token_provider
    #
    # credential = ClientSecretCredential(
    #     tenant_id=os.environ["AZURE_TENANT_ID"],
    #     client_id=os.environ["AZURE_CLIENT_ID"],
    #     client_secret=os.environ["AZURE_CLIENT_SECRET"],
    # )
    # token_provider = get_bearer_token_provider(
    #     credential,
    #     "https://cognitiveservices.azure.com/.default",
    # )
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option D: Existing token provider ------------------------------------
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=existing_token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # -- Option E: DefaultAzureCredential (local dev / Managed Identity) ------
    #
    # from azure.identity import DefaultAzureCredential
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     credential=DefaultAzureCredential(),
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    search = PerplexitySearch(
        api_key=perplexity_key,
        base_url="https://api.perplexity.ai",
    )
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
