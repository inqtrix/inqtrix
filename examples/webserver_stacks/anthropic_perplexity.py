"""Webserver-Spiegel zu provider_stacks/anthropic_perplexity.py.

Mounts the OpenAI-compatible Inqtrix server on top of an explicit
AnthropicLLM + PerplexitySearch provider stack. Provider construction
is **1:1 identical** to the library example; only the run block
injects the providers into ``create_app(...)`` and starts uvicorn.

Architecture
------------
Direct Anthropic + Perplexity, no LiteLLM proxy:

* ``AnthropicLLM`` calls ``https://api.anthropic.com/v1/messages``.
* ``PerplexitySearch`` calls ``https://api.perplexity.ai`` (OpenAI-
  SDK-compatible).

Required environment variables
------------------------------
- ``ANTHROPIC_API_KEY``
- ``PERPLEXITY_API_KEY``

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
    uv run python examples/webserver_stacks/anthropic_perplexity.py
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AgentConfig,
    AnthropicLLM,
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
    """Build AnthropicLLM + PerplexitySearch — identical to the library example."""
    anthropic_key = _require_env("ANTHROPIC_API_KEY")
    perplexity_key = _require_env("PERPLEXITY_API_KEY")

    llm = AnthropicLLM(
        api_key=anthropic_key,
        default_max_tokens=64000,
        context_window_tokens=200_000,
        thinking={"type": "adaptive"},
        # "medium" + adaptive thinking is the documented sweet spot for
        # research workloads (see provider_stacks sibling for the rationale).
        effort="medium",
        # Three model tiers. Nodes map: answer -> high, plan/evaluate/
        # direct_chat -> mid, classify/claim_extract -> fast. Per-tier
        # effort turns reasoning on deliberately (only the high tier here);
        # tiers otherwise differ by model alone. A per-node <node>_model arg
        # overrides the tier. See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="claude-opus-4-7",
        tier_high_model="claude-opus-4-7",   tier_high_effort="medium",
        tier_mid_model="claude-sonnet-4-6",  tier_mid_effort="none",
        tier_fast_model="claude-haiku-4-5",  tier_fast_effort="none",
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
    """Test-friendly entry point: build the wired FastAPI app."""
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
