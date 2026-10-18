"""Webserver-Spiegel zu provider_stacks/litellm_perplexity.py.

Mounts the OpenAI-compatible Inqtrix server on top of an explicit
LiteLLM + PerplexitySearch provider stack. Provider construction is
**1:1 identical** to ``examples/provider_stacks/litellm_perplexity.py``;
only the run block differs (it injects the providers into
``create_app(...)`` and starts ``uvicorn`` instead of calling
``agent.research(...)``).

Architecture
------------
The LiteLLM proxy serves the language model; web search runs against the
native Perplexity Agent API directly (the Perplexity SDK cannot be routed
through LiteLLM), so ``PerplexitySearch`` uses its own endpoint and a
dedicated ``PERPLEXITY_API_KEY`` independent of the LiteLLM gateway.

Required environment variables
------------------------------
- ``LITELLM_API_KEY``                 — LiteLLM proxy auth (language model)
- ``PERPLEXITY_API_KEY``              — native Perplexity Agent API (search)
- ``LITELLM_BASE_URL``                — defaults to ``http://localhost:4000/v1``

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
- ``INQTRIX_SERVER_TLS_KEYFILE`` + ``INQTRIX_SERVER_TLS_CERTFILE``  — HTTPS via uvicorn
- ``INQTRIX_SERVER_API_KEY``          — Bearer-API-key gate on /v1/chat/completions
- ``INQTRIX_SERVER_CORS_ORIGINS``     — comma-separated CORS whitelist

Run with::

    uv sync
    uv run python examples/webserver_stacks/litellm_perplexity.py

Then call::

    curl http://localhost:5100/health
    curl -X POST http://localhost:5100/v1/chat/completions \
        -H 'Content-Type: application/json' \
        -d '{"messages":[{"role":"user","content":"Hallo"}],
             "agent_overrides":{"report_profile":"deep"}}'
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AgentConfig,
    LiteLLM,
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

# Logging mirrors the provider_stacks pattern — controlled via env so
# the example stays quiet unless requested.
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
    """Build LiteLLM + PerplexitySearch — identical to the library example.

    Diff against ``provider_stacks/litellm_perplexity.py`` is intentionally
    confined to the surrounding run-block; this function is byte-for-byte
    the same Baukasten construction so behaviour stays comparable.
    """
    api_key = _require_env("LITELLM_API_KEY")
    base_url = os.environ.get(
        "LITELLM_BASE_URL", "http://localhost:4000/v1"
    ).strip()

    llm = LiteLLM(
        api_key=api_key,
        base_url=base_url,
        # classify_model="claude-sonnet-4.6",
        # evaluate_model="claude-sonnet-4.6",
        # Three model tiers (LiteLLM model aliases -- replace with yours).
        # Nodes map: answer -> high, plan/evaluate/direct_chat -> mid,
        # classify/claim_extract -> fast. A per-node <node>_model arg
        # overrides the tier.
        #
        # NOTE: LiteLLM currently IGNORES per-tier effort (tier_*_effort is
        # accepted but not mapped yet); tier MODEL routing still applies.
        # Use Anthropic/Bedrock/Azure for reasoning control.
        # See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="your-high-model",
        tier_high_model="your-high-model",
        tier_mid_model="your-mid-model",
        tier_fast_model="your-fast-model",
    )
    search = PerplexitySearch(
        api_key=_require_env("PERPLEXITY_API_KEY"),
        base_url="https://api.perplexity.ai",
    )
    return ProviderContext(llm=llm, search=search)


def _build_settings() -> Settings:
    """Return the full Settings stack used to drive the FastAPI server.

    All security / lifecycle defaults flow through ``ServerSettings``
    (and therefore through the ``INQTRIX_SERVER_*`` env-vars). The
    behavioural envelope mirrors the AgentConfig values used in the
    sibling library example, so a side-by-side run produces the same
    research behaviour for the same question.
    """
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
        models=ModelSettings(),  # not used in Baukasten injection mode
        agent=agent,
        server=ServerSettings(),  # picks up INQTRIX_SERVER_* env vars
    )


def build_app() -> FastAPI:
    """Test-friendly entry point: build the wired FastAPI app.

    Used by the test suite (which calls this without ever invoking
    ``uvicorn.run``). The same function is what ``main()`` consumes
    so the production and test paths stay symmetric.
    """
    providers = _build_providers()
    settings = _build_settings()
    return create_app(settings=settings, providers=providers)


def main() -> None:
    """Build the app and start uvicorn (with optional TLS)."""
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


# Optional: hint that an Inqtrix-`AgentConfig` / `ResearchAgent` could
# also be instantiated for in-process use; the webserver itself does
# not rely on these symbols. Kept as imports for symmetry with the
# library example so the diff against provider_stacks is minimal.
_ = (AgentConfig, ResearchAgent)


if __name__ == "__main__":
    main()
