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
- ``AWS_REGION`` (optional — defaults to ``eu-central-1``)

Optional logging:
- ``INQTRIX_LOG_ENABLED`` / ``INQTRIX_LOG_LEVEL`` / ``INQTRIX_LOG_CONSOLE``
- ``INQTRIX_LOG_INCLUDE_WEB`` (default ``true``) — mirror uvicorn / FastAPI
  logs into the same file when file logging is enabled
- ``INQTRIX_LOG_WEB_LEVEL`` (default ``INFO``) — separate verbosity knob
  for the uvicorn / FastAPI loggers
- ``OBSERVABILITY_PROFILE`` (default ``summary``) — set ``forensic`` for structured
  lineage ``EVENT`` JSON in the same log file; requires ``INQTRIX_LOG_LEVEL=DEBUG``.
  See ``docs/observability/logging.md`` and ``docs/observability/forensic-cookbook.md``.

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
