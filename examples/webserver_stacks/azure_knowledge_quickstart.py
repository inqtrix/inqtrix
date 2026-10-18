"""Knowledge-engine quickstart on the Azure OpenAI + Perplexity stack.

Same provider construction as ``azure_openai_perplexity.py`` (Option A,
API key — see that sibling for the Service Principal / Managed Identity
options B-E), plus the knowledge engine switched on: in-memory vector
store (no external services, contents lost on restart) and Azure
deployment-based embeddings. The server then registers the
``/v1/knowledge/*`` routes and the ``mode=knowledge`` run algorithm.

Required environment variables
------------------------------
- ``PERPLEXITY_API_KEY``
- ``AZURE_OPENAI_API_KEY``
- ``AZURE_OPENAI_ENDPOINT``
- ``INQTRIX_EMBEDDING_AZURE_ENDPOINT``  (falls back to ``AZURE_AI_PROJECT_ENDPOINT``;
  startup raises ``RuntimeError`` when both are empty)

Optional embedding configuration (read by ``KnowledgeSettings`` from env):
- ``INQTRIX_EMBEDDING_AZURE_API_KEY``   (falls back to ``AZURE_AI_PROJECT_API_KEY``,
  then ``AZURE_OPENAI_API_KEY``)
- ``INQTRIX_EMBEDDING_MODEL``           (Azure DEPLOYMENT name of the embeddings
  model; default ``text-embedding-3-small``)
- ``INQTRIX_EMBEDDING_AZURE_API_VERSION`` (default ``2024-10-21``)

Optional logging / server / security: see ``examples/webserver_stacks/README.md``.

Run with::

    uv sync
    uv run python examples/webserver_stacks/azure_knowledge_quickstart.py

End-to-end curl sequence
------------------------
1. Create a collection (embedding model is stored immutably at creation)::

    curl -X POST http://localhost:5100/v1/knowledge/collections \
        -H 'Content-Type: application/json' \
        -d '{"name": "Compliance-Korpus"}'

   Returns 201 with ``{"id": "...", "name": "...", "embedding_model": "...",
   "embedding_dim": ..., "document_count": 0, "created_at": ...}``. Keep the
   ``id`` for the following calls.

2. Ingest one text document (chunked and embedded synchronously)::

    curl -X POST http://localhost:5100/v1/knowledge/collections/<collection_id>/documents \
        -H 'Content-Type: application/json' \
        -d '{"title": "Notfallmanagement",
             "text": "Institutionen muessen ein Notfallmanagement etablieren...",
             "metadata": {"source": "demo"}}'

   Returns 201 with the document payload. Alternative for uploaded
   binaries: pass ``{"file_id": "..."}`` instead of ``text`` (the file
   must exist under ``/v1/files``; ``text`` and ``file_id`` are mutually
   exclusive).

3. Optional retrieval check without an LLM call::

    curl -X POST http://localhost:5100/v1/knowledge/search \
        -H 'Content-Type: application/json' \
        -d '{"query": "Was verlangt das Notfallmanagement?",
             "collection_ids": ["<collection_id>"], "top_k": 5}'

4. Start a knowledge run (the retrieval profile is per request; valid
   values ``schnell`` | ``standard`` | ``gruendlich`` | ``tief`` | ``auto`` —
   see docs/configuration/knowledge-profiles.md)::

    curl -X POST http://localhost:5100/v1/runs \
        -H 'Content-Type: application/json' \
        -d '{"mode": "knowledge",
             "question": "Welche Anforderungen stellt das Notfallmanagement?",
             "knowledge_filters": {"collection_ids": ["<collection_id>"],
                                   "profile": "gruendlich",
                                   "top_k": 8}}'

   Returns 202 with the run summary (``run_id``, ``status: "queued"``,
   ``queue_position``, ``mode``, ...).

5. Stream the structured run events as SSE (``-N`` disables buffering)::

    curl -N http://localhost:5100/v1/runs/<run_id>/events

   Terminal event types: ``inqtrix.run.completed`` / ``.failed`` /
   ``.cancelled``.

6. Fetch the final report payload::

    curl http://localhost:5100/v1/runs/<run_id>/result

   Returns 409 ``run_not_completed`` while the run is still executing;
   completed records stay readable for ``RUN_COMPLETED_TTL_SECONDS``
   (default 300).
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from inqtrix import (
    AzureOpenAILLM,
    PerplexitySearch,
    ReportProfile,
)
from inqtrix.logging_config import build_uvicorn_log_config, configure_logging
from inqtrix.providers.base import ProviderContext
from inqtrix.server import create_app
from inqtrix.server.security import resolve_tls_paths
from inqtrix.settings import (
    AgentSettings,
    KnowledgeSettings,
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
    """Build AzureOpenAILLM + PerplexitySearch — identical to azure_openai_perplexity.py."""
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")
    perplexity_key = _require_env("PERPLEXITY_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        # Three model tiers (Azure DEPLOYMENT names -- replace with yours).
        # Nodes map: answer -> high, plan/evaluate/direct_chat -> mid,
        # classify/claim_extract -> fast. The knowledge gate, rerank, and
        # decompose calls run on the fast tier. Auth options B-E (Service
        # Principal, Managed Identity) are documented in the sibling
        # azure_openai_perplexity.py — the construction here is identical.
        default_model="gpt-5.4",
        tier_high_model="gpt-5.4",       tier_high_effort="medium",
        tier_mid_model="gpt-5.4",        tier_mid_effort="none",
        tier_fast_model="gpt-5.4-mini",  tier_fast_effort="none",
    )
    search = PerplexitySearch(
        api_key=perplexity_key,
        base_url="https://api.perplexity.ai",
    )
    return ProviderContext(llm=llm, search=search)


def _build_settings() -> Settings:
    """Settings with the knowledge engine enabled on the memory store.

    ``KnowledgeSettings`` is the env-coupled Settings bridge: the three
    explicit fields pin the quickstart shape (engine on, in-process
    vector store, Azure deployment-based embedding auth); every other
    knowledge field — embedding endpoint, key, model, api-version —
    still resolves from the ``INQTRIX_EMBEDDING_*`` env vars listed in
    the module docstring. A missing embedding endpoint fails loudly at
    startup instead of degrading.
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
    knowledge = KnowledgeSettings(
        enabled=True,
        vector_backend="memory",
        embedding_provider="azure",
    )
    return Settings(
        models=ModelSettings(),
        agent=agent,
        server=ServerSettings(),
        knowledge=knowledge,
    )


def build_app() -> FastAPI:
    """Test-friendly entry point: build the wired FastAPI app."""
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


if __name__ == "__main__":
    main()
