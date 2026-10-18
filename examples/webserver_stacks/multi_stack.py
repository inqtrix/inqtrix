"""Multi-stack webserver example: hosts every supported provider stack at once.

Mounts the OpenAI-compatible Inqtrix server with a registry of named
``StackBundle`` entries — one per provider combination from
``examples/provider_stacks/``. Frontends (the React research desk, custom
web UIs, etc.) discover the available stacks via ``GET /v1/stacks`` and pick one per
request via the new ``"stack"`` top-level body field on
``POST /v1/chat/completions``.

Each stack is opt-in: a stack is only registered when the env vars its
provider construction needs are present. The script never fails the
whole server because one provider family is missing — it just skips
that bundle and logs a notice.

Required environment variables
------------------------------
At least one of the following stacks must have its env vars present,
otherwise startup raises ``RuntimeError``:

* ``litellm_perplexity``        → ``LITELLM_API_KEY``
* ``anthropic_perplexity``      → ``ANTHROPIC_API_KEY`` + ``PERPLEXITY_API_KEY``
* ``bedrock_perplexity``        → ``PERPLEXITY_API_KEY`` (AWS via profile)
* ``azure_openai_perplexity``   → ``AZURE_OPENAI_API_KEY`` +
                                   ``AZURE_OPENAI_ENDPOINT`` +
                                   ``AZURE_OPENAI_DEPLOYMENT_NAME`` +
                                   ``PERPLEXITY_API_KEY``
* ``azure_foundry_web_search``  → ``AZURE_OPENAI_API_KEY`` +
                                   ``AZURE_OPENAI_ENDPOINT`` +
                                   ``AZURE_OPENAI_DEPLOYMENT_NAME`` +
                                   ``AZURE_AI_PROJECT_ENDPOINT`` +
                                   ``WEB_SEARCH_AGENT_NAME``

Optional logging / server / security: same as the single-stack examples
(``INQTRIX_LOG_*``, ``INQTRIX_SERVER_*``). Forensic lineage: ``OBSERVABILITY_PROFILE=forensic``
with ``INQTRIX_LOG_LEVEL=DEBUG`` — see ``examples/webserver_stacks/README.md`` and
``docs/observability/forensic-cookbook.md``.

Optional default-stack override: ``INQTRIX_DEFAULT_STACK`` picks which
stack is used when a request omits the ``"stack"`` field. Defaults to
the first registered stack in the iteration order above.

Run with::

    uv sync
    uv run python examples/webserver_stacks/multi_stack.py

Then call::

    curl http://localhost:5100/v1/stacks
    curl -X POST http://localhost:5100/v1/chat/completions \
        -H 'Content-Type: application/json' \
        -d '{"messages":[{"role":"user","content":"Hallo"}],
             "stack":"anthropic_perplexity"}'
"""

from __future__ import annotations

import logging
import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AnthropicLLM,
    AzureFoundryWebSearch,
    AzureOpenAILLM,
    BedrockLLM,
    LiteLLM,
    PerplexitySearch,
    ReportProfile,
)
from inqtrix.logging_config import build_uvicorn_log_config, configure_logging
from inqtrix.providers.base import ProviderContext
from inqtrix.server.security import resolve_tls_paths
from inqtrix.server.stacks import StackBundle, create_multi_stack_app
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

log = logging.getLogger("inqtrix")


# ------------------------------------------------------------------ #
# Per-stack opt-in builders. Each returns ``(name, StackBundle)`` or
# ``None`` when its env vars are not all present.
# ------------------------------------------------------------------ #


def _has(*names: str) -> bool:
    """Return True iff every given env var is set and non-empty."""
    return all(bool(os.environ.get(name, "").strip()) for name in names)


def _build_litellm_perplexity() -> tuple[str, StackBundle] | None:
    if not _has("LITELLM_API_KEY", "PERPLEXITY_API_KEY"):
        return None
    api_key = os.environ["LITELLM_API_KEY"].strip()
    base_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1").strip()
    return "litellm_perplexity", StackBundle(
        providers=ProviderContext(
            llm=LiteLLM(
                api_key=api_key,
                base_url=base_url,
                # classify_model="claude-sonnet-4.6",
                # evaluate_model="claude-sonnet-4.6",
                # Three model tiers (LiteLLM model aliases). Nodes map:
                # answer -> high, plan/evaluate/direct_chat -> mid,
                # classify/claim_extract -> fast. A per-node <node>_model arg
                # overrides the tier.
                #
                # NOTE: LiteLLM currently IGNORES per-tier effort (tier_*_effort
                # is accepted but not mapped yet); tier MODEL routing still
                # applies.  Use Anthropic/Bedrock/Azure for reasoning control.
                # See docs/architecture/websearch/nodes.md
                # Fallback + reasoning_model identity on /health & /v1/stacks
                # (= high tier); the tiers below are the active routing.
                default_model="claude-opus-4.6-agent",
                tier_high_model="claude-opus-4.6-agent",
                tier_mid_model="claude-sonnet-4.6",
                tier_fast_model="claude-haiku-4.5",
            ),
            search=PerplexitySearch(
                api_key=os.environ["PERPLEXITY_API_KEY"].strip(),
                base_url="https://api.perplexity.ai",
            ),
        ),
        description="LiteLLM proxy (Claude Opus 4.6) + Perplexity Sonar Pro",
    )


def _build_anthropic_perplexity() -> tuple[str, StackBundle] | None:
    if not _has("ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"):
        return None
    return "anthropic_perplexity", StackBundle(
        providers=ProviderContext(
            llm=AnthropicLLM(
                api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
                thinking={"type": "adaptive"},
                effort="medium",
                # Three model tiers. Nodes map: answer -> high, plan/evaluate/
                # direct_chat -> mid, classify/claim_extract -> fast. Per-tier
                # effort turns reasoning on deliberately (only the high tier
                # here); tiers otherwise differ by model alone. A per-node
                # <node>_model arg overrides the tier.
                # See docs/architecture/websearch/nodes.md
                # Fallback + reasoning_model identity on /health & /v1/stacks
                # (= high tier); the tiers below are the active routing.
                default_model="claude-opus-4-7",
                tier_high_model="claude-opus-4-7",   tier_high_effort="medium",
                tier_mid_model="claude-sonnet-4-6",  tier_mid_effort="none",
                tier_fast_model="claude-haiku-4-5",  tier_fast_effort="none",
            ),
            search=PerplexitySearch(
                api_key=os.environ["PERPLEXITY_API_KEY"].strip(),
                base_url="https://api.perplexity.ai",
            ),
        ),
        description="Direct Anthropic Opus 4.6 (adaptive thinking) + Perplexity Sonar Pro",
    )


def _build_bedrock_perplexity() -> tuple[str, StackBundle] | None:
    if not _has("PERPLEXITY_API_KEY"):
        return None
    aws_profile = os.environ.get("AWS_PROFILE", "").strip() or None
    aws_region = os.environ.get("AWS_REGION", "").strip() or "eu-central-1"
    return "bedrock_perplexity", StackBundle(
        providers=ProviderContext(
            llm=BedrockLLM(
                profile_name=aws_profile,
                region_name=aws_region,
                thinking={"type": "adaptive"},
                effort="medium",
                # Three model tiers (Bedrock model IDs, eu-* to match the
                # default region_name). Nodes map: answer -> high, plan/
                # evaluate/direct_chat -> mid, classify/claim_extract -> fast.
                # Per-tier effort turns reasoning on deliberately (only the
                # high tier here, Claude models). A per-node <node>_model arg
                # overrides the tier. See docs/architecture/websearch/nodes.md
                # Fallback + reasoning_model identity on /health & /v1/stacks
                # (= high tier); the tiers below are the active routing.
                default_model="eu.anthropic.claude-opus-4-7-v1",
                tier_high_model="eu.anthropic.claude-opus-4-7-v1",   tier_high_effort="medium",
                tier_mid_model="eu.anthropic.claude-sonnet-4-6",     tier_mid_effort="none",
                tier_fast_model="eu.anthropic.claude-haiku-4-5",     tier_fast_effort="none",
            ),
            search=PerplexitySearch(
                api_key=os.environ["PERPLEXITY_API_KEY"].strip(),
                base_url="https://api.perplexity.ai",
            ),
        ),
        description="AWS Bedrock Opus 4.6 (eu region) + Perplexity Sonar Pro",
    )


def _build_azure_openai_perplexity() -> tuple[str, StackBundle] | None:
    required = (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "PERPLEXITY_API_KEY",
    )
    if not _has(*required):
        return None
    return "azure_openai_perplexity", StackBundle(
        providers=ProviderContext(
            llm=AzureOpenAILLM(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"].strip(),
                api_key=os.environ["AZURE_OPENAI_API_KEY"].strip(),
                # Three model tiers (Azure DEPLOYMENT names -- replace with
                # yours). Nodes map: answer -> high, plan/evaluate/direct_chat
                # -> mid, classify/claim_extract -> fast. Per-tier effort maps
                # to reasoning_effort; only the high tier reasons here (a
                # graded effort on a non-reasoning deployment is an Azure HTTP
                # 400). A per-node <node>_model arg overrides the tier.
                # See docs/architecture/websearch/nodes.md
                # Fallback + reasoning_model identity on /health & /v1/stacks
                # (= high tier); the tiers below are the active routing.
                default_model="gpt-5.4",
                tier_high_model="gpt-5.4",       tier_high_effort="medium",
                tier_mid_model="gpt-5.4",        tier_mid_effort="none",
                tier_fast_model="gpt-5.4-mini",  tier_fast_effort="none",
            ),
            search=PerplexitySearch(
                api_key=os.environ["PERPLEXITY_API_KEY"].strip(),
                base_url="https://api.perplexity.ai",
            ),
        ),
        description="Azure OpenAI deployment + Perplexity Sonar Pro",
    )


def _build_azure_foundry_web_search() -> tuple[str, StackBundle] | None:
    required = (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_AI_PROJECT_ENDPOINT",
        "WEB_SEARCH_AGENT_NAME",
    )
    if not _has(*required):
        return None
    project_endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"].strip()
    project_api_key = os.environ.get("AZURE_AI_PROJECT_API_KEY", "").strip()
    web_agent_version = os.environ.get("WEB_SEARCH_AGENT_VERSION", "").strip()

    # Azure Foundry web search auth: project API key (api-key header) when set,
    # otherwise Entra ID (DefaultAzureCredential).
    search_kwargs: dict[str, Any] = {
        "project_endpoint": project_endpoint,
        "agent_name": os.environ["WEB_SEARCH_AGENT_NAME"].strip(),
    }
    if web_agent_version:
        search_kwargs["agent_version"] = web_agent_version
    if project_api_key:
        search_kwargs["api_key"] = project_api_key

    return "azure_foundry_web_search", StackBundle(
        providers=ProviderContext(
            llm=AzureOpenAILLM(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"].strip(),
                api_key=os.environ["AZURE_OPENAI_API_KEY"].strip(),
                # Three model tiers (Azure DEPLOYMENT names -- replace with
                # yours). Nodes map: answer -> high, plan/evaluate/direct_chat
                # -> mid, classify/claim_extract -> fast. Per-tier effort maps
                # to reasoning_effort; only the high tier reasons here (a
                # graded effort on a non-reasoning deployment is an Azure HTTP
                # 400). A per-node <node>_model arg overrides the tier.
                # See docs/architecture/websearch/nodes.md
                # Fallback + reasoning_model identity on /health & /v1/stacks
                # (= high tier); the tiers below are the active routing.
                default_model="gpt-5.4",
                tier_high_model="gpt-5.4",       tier_high_effort="medium",
                tier_mid_model="gpt-5.4",        tier_mid_effort="none",
                tier_fast_model="gpt-5.4-mini",  tier_fast_effort="none",
            ),
            search=AzureFoundryWebSearch(**search_kwargs),
        ),
        description="Azure OpenAI deployment + Azure Foundry Web Search agent",
    )


# Iteration order is also the search order for the implicit default
# stack: the first stack that successfully builds wins.
_STACK_BUILDERS = (
    _build_litellm_perplexity,
    _build_anthropic_perplexity,
    _build_bedrock_perplexity,
    _build_azure_openai_perplexity,
    _build_azure_foundry_web_search,
)


def _build_stacks() -> tuple[dict[str, StackBundle], str]:
    """Build the registry of available stacks from env vars.

    Returns ``(stacks, default_stack)``. Raises ``RuntimeError`` when no
    stack could be built (zero env-var coverage).
    """
    stacks: dict[str, StackBundle] = {}
    for builder in _STACK_BUILDERS:
        result = builder()
        if result is None:
            log.info("Multi-stack: skipped %s (env vars missing)", builder.__name__)
            continue
        name, bundle = result
        stacks[name] = bundle
        log.info(
            "Multi-stack: registered %s (%s)", name, bundle.description or "no description"
        )

    if not stacks:
        raise RuntimeError(
            "Multi-stack server cannot start: no stack has its required "
            "environment variables. See the module docstring for the "
            "per-stack env-var matrix."
        )

    requested_default = os.environ.get("INQTRIX_DEFAULT_STACK", "").strip()
    if requested_default and requested_default not in stacks:
        raise RuntimeError(
            f"INQTRIX_DEFAULT_STACK={requested_default!r} is not in the "
            f"registered stack list ({sorted(stacks.keys())})."
        )
    default_stack = requested_default or next(iter(stacks))
    return stacks, default_stack


def _build_settings() -> Settings:
    """Build the global Settings shared across stacks.

    Per-stack overrides go on the StackBundle.agent_settings field; the
    Settings here only carry the server-side limits, security config
    and the global fallback agent envelope.
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
        models=ModelSettings(),
        agent=agent,
        server=ServerSettings(),
    )


def build_app() -> FastAPI:
    """Test-friendly entry point: build the wired FastAPI app."""
    stacks, default_stack = _build_stacks()
    settings = _build_settings()
    return create_multi_stack_app(
        settings=settings, stacks=stacks, default_stack=default_stack
    )


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


if __name__ == "__main__":
    main()
