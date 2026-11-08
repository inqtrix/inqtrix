"""Webserver-Spiegel zu provider_stacks/bedrock_perplexity.py.

Direct Amazon Bedrock + PerplexitySearch behind the OpenAI-compatible
Inqtrix server. Provider construction is **1:1 identical** to the
library example; only the run block injects providers and starts
uvicorn.

Architecture
------------
* ``BedrockLLM`` calls the Bedrock Converse API via boto3.
  Authentication is handled through AWS named profiles configured in
  ``~/.aws/config`` and ``~/.aws/credentials``.
* ``PerplexitySearch`` calls ``https://api.perplexity.ai`` directly.

Required environment variables
------------------------------
- ``PERPLEXITY_API_KEY``
- ``AWS_PROFILE`` (optional — defaults to the default profile)
- ``AWS_BEARER_TOKEN_BEDROCK`` (optional; a Bedrock API key, boto3 signs with it instead of the profile)
- ``AWS_REGION`` (optional — defaults to ``eu-central-1``)

Optional logging:
- ``INQTRIX_LOG_TARGET`` (``console``, ``file``, ``both`` or ``none``; default
  ``console``), ``INQTRIX_LOG_LEVEL`` (default ``INFO``) and
  ``INQTRIX_LOG_FORMAT`` (``json`` or ``text``; default ``json``)
- ``INQTRIX_LOG_WEB_LEVEL`` (default: the log level, never below ``INFO``) —
  separate threshold for the uvicorn / FastAPI loggers
- ``OBSERVABILITY_PROFILE`` (default ``summary``) — ``forensic`` keeps source,
  citation, claim and answer lineage in the protected run audit; logs never
  carry it. See ``docs/observability/logging.md``.

Optional server bind:
- ``INQTRIX_SERVER_HOST`` (default ``0.0.0.0``)
- ``INQTRIX_SERVER_PORT`` (default ``5100``)

Optional security (off-by-default):
- ``INQTRIX_SERVER_TLS_KEYFILE`` + ``INQTRIX_SERVER_TLS_CERTFILE``
- ``INQTRIX_SERVER_API_KEY``
- ``INQTRIX_SERVER_CORS_ORIGINS``

Run with::

    uv sync
    uv run python examples/webserver_stacks/bedrock_perplexity.py
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AgentConfig,
    BedrockLLM,
    PerplexitySearch,
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


def _build_providers() -> ProviderContext:
    """Build BedrockLLM + PerplexitySearch — identical to the library example."""
    perplexity_key = _require_env("PERPLEXITY_API_KEY")
    aws_profile = os.environ.get("AWS_PROFILE", "").strip() or None
    aws_region = os.environ.get("AWS_REGION", "").strip() or "us-west-2"

    llm = BedrockLLM(
        profile_name=aws_profile,
        region_name=aws_region,
        # thinking={"type": "adaptive"},
        # "medium" + adaptive thinking — see provider_stacks sibling for the
        # measurement-backed rationale (~30-40 % wall-clock saving).
        # effort="medium",
        # Three model tiers (Bedrock model IDs, us-* to match region_name
        # above). Nodes map: answer -> high, plan/evaluate/direct_chat -> mid,
        # classify/claim_extract -> fast. Per-tier effort enables adaptive
        # thinking on the high tier (Claude models); tiers otherwise differ
        # by model alone. A per-node <node>_model arg overrides the tier.
        # See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="us.anthropic.claude-opus-4-7-v1:0",
        tier_high_model="us.anthropic.claude-opus-4-7-v1:0",   tier_high_effort="medium",
        tier_mid_model="us.anthropic.claude-sonnet-4-6-v1:0",  tier_mid_effort="none",
        tier_fast_model="us.anthropic.claude-haiku-4-5-v1:0",  tier_fast_effort="none",
    )
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
