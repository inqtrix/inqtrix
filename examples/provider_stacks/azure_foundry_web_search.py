"""Azure OpenAI v1 + Azure Foundry Web Search example (Foundry Agent).

==============================================================================
WHAT THIS FILE COVERS
==============================================================================
This script uses the Azure Foundry Web Search Agent path (provider class
``AzureFoundryWebSearch``, smoke-tested in ``test_foundry_web_search.py``).  A
pre-created Foundry agent equipped with the Web Search tool is invoked
via the project-scoped Responses API and ``agent_reference``.

==============================================================================
WHICH SMOKE TESTS MUST PASS BEFORE RUNNING THIS FILE
==============================================================================
Run, in order, and only proceed if each prints a green panel:

1. ``examples/provider_stacks/azure_smoke_tests/test_llm.py``
   -- proves the Azure OpenAI reasoning path (this file uses it for
   classify / plan / claim extraction / evaluate / answer).
2. ``examples/provider_stacks/azure_smoke_tests/test_foundry_web_search.py``
   -- proves the Azure Foundry Web Search Agent path (this file uses
   it for every web-search call).

Architecture
------------
This example combines two independent Azure components directly:

1. **Sprachmodell / Reasoning** -- via ``AzureOpenAILLM``
2. **Websuche / Grounding** -- via ``AzureFoundryWebSearch``

This combined script itself uses:
``AzureOpenAILLM`` + ``AzureFoundryWebSearch``.

This example calls two independent Azure services directly:

1. **Azure OpenAI v1 Chat Completions API** -- via ``AzureOpenAILLM``,
   using the official ``openai`` SDK's generic ``OpenAI`` client
   against the Azure ``/openai/v1/`` endpoint.  Used for all reasoning
   tasks: question classification, query planning, evidence evaluation,
   parallel claim extraction, and final answer synthesis.

2. **Azure AI Foundry Web Search Agent** -- via
   ``AzureFoundryWebSearch``, using the OpenAI ``responses`` API
   against the project-scoped Foundry ``/openai/v1/`` endpoint.
   A pre-created Foundry agent with the Web Search tool performs web
   searches.  Used ONLY for web search -- not for reasoning.

Why two separate providers?
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Even though both components ultimately use OpenAI-compatible clients,
the Web Search agent should not be reused for reasoning because:

- The agent's instructions and attached tool are specialized for web search.
- The classify/plan/evaluate/answer nodes need structured reasoning
  (e.g. JSON query lists, DECISION keywords) that agent-side web search
  would corrupt.
- Search behaviour is mostly defined by the agent and its tool config,
  not by the research graph's reasoning prompts.

Prerequisites (one-time Azure Portal setup)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. **AI Foundry project:** Azure Portal > "Azure AI Foundry" > create
   a Hub (if needed) > create a Project within the Hub > deploy a
   model (e.g. gpt-4o or gpt-5-mini) under "Models + Endpoints".

2. **Create a Web Search agent:** In the Foundry project, create an
   agent with the Web Search tool enabled.  Note the **Agent Name** and,
   if relevant, the **Agent Version**.

3. **Authentication:** Note the project **API key** from Project
   Settings > Keys & Endpoint.  Web search authenticates with it.

Required environment variables (in .env or process env):
- AZURE_OPENAI_API_KEY          (for LLM -- API-key auth)
- AZURE_OPENAI_ENDPOINT         (e.g. https://mein-openai.openai.azure.com/)
- AZURE_AI_PROJECT_ENDPOINT     (e.g. https://mein-projekt.services.ai.azure.com/api/projects/mein-projekt)
- AZURE_AI_PROJECT_API_KEY      (Foundry project key for web search)
- WEB_SEARCH_AGENT_NAME         (agent name from Foundry)

Optional:
- WEB_SEARCH_AGENT_VERSION      (specific agent version; if empty, latest/default)
- AZURE_OPENAI_CONTEXT_WINDOW_TOKENS  (context window of the deployments; default 128000)

Deployments and question
~~~~~~~~~~~~~~~~~~~~~~~~
The Azure deployment names are set in ``main()`` as three model tiers
(high for the answer, mid for plan / evaluate / direct chat, fast for
classify and claim extraction); replace them with your deployment names.
The demo question is the ``QUESTION`` constant below.

Terminal rendering
------------------
The agent returns Markdown, which looks good in a chat UI but is hard
to read as raw text in a terminal.  This example uses ``rich`` to
render Markdown with colours, formatted headers, bullet lists, and
clickable links (in terminals that support OSC 8 hyperlinks such as
iTerm2, Windows Terminal, or GNOME Terminal).

``rich`` is a core dependency and always available after::

    uv sync

Run with::

    uv sync
    uv run python examples/provider_stacks/azure_foundry_web_search.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from inqtrix import (
    AgentConfig,
    AzureFoundryWebSearch,
    AzureOpenAILLM,
    ResearchAgent,
)
from inqtrix.constants import DEFAULT_LLM_MAX_OUTPUT_TOKENS


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
# Logging with automatic secret redaction. Off by default so the
# terminal stays clean — enable via environment variables:
#
#   INQTRIX_LOG_TARGET=file    — console, file (logs/inqtrix-script.log) or both
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_FORMAT=text    — json (default) or text
#   See docs/observability/logging.md.
from inqtrix.logging_config import configure_logging

# Examples keep the terminal for their own output: logs appear only when
# INQTRIX_LOG_TARGET is set (console, file or both).
_logging = configure_logging(
    service="script", target=os.environ.get("INQTRIX_LOG_TARGET") or "none"
)
if _logging.file_path:
    print(f"Logging to {_logging.file_path}")

QUESTION = "Was ist der aktuelle Stand der GKV-Reform?"


def _print_result(result) -> None:
    """Pretty-print a ResearchResult."""
    metrics_line = (
        f"Confidence: {result.metrics.confidence}/10  |  "
        f"Sources: {result.metrics.total_citations}  |  "
        f"Rounds: {result.metrics.rounds}"
    )
    console = Console()
    console.print(Markdown(result.answer))
    console.print()
    console.print(Panel(metrics_line, title="Metrics", expand=False))


# ── Output mode ──────────────────────────────────────────────────────
# True  → streaming (live progress messages + word-by-word answer)
# False → blocking  (waits for the full result, then prints at once)
USE_STREAMING = True

# Only relevant when USE_STREAMING is True:
# True  → show intermediate progress messages before the answer
#          e.g. "Analysiere Frage…", "Plane Suchanfragen (Runde 1/4)…"
# False → stream only the final answer text, no status updates
INCLUDE_PROGRESS = True


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    project_endpoint = _require_env("AZURE_AI_PROJECT_ENDPOINT")
    agent_name = _require_env("WEB_SEARCH_AGENT_NAME")
    agent_version = os.getenv("WEB_SEARCH_AGENT_VERSION", "")

    azure_context_window_tokens = int(
        os.environ.get("AZURE_OPENAI_CONTEXT_WINDOW_TOKENS", "128000").strip()
        or "128000"
    )

    # -- LLM Provider ---------------------------------------------------------
    #
    # AzureOpenAILLM is the same component that is smoke-tested in:
    #   examples/provider_stacks/azure_smoke_tests/test_llm.py
    # Test that file first if the combined setup fails.
    #
    # Authentication: the API key of the Azure OpenAI resource.
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        default_max_tokens=DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        context_window_tokens=azure_context_window_tokens,
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

    # -- Search Provider -------------------------------------------------------
    #
    # AzureFoundryWebSearch is the same component that is smoke-tested in:
    #   examples/provider_stacks/azure_smoke_tests/test_foundry_web_search.py
    # Test that file first if the combined setup fails.
    #
    # This provider uses the Foundry Responses API with agent_reference.
    # The research algorithm passes the standard SearchProvider parameters,
    # but this backend only supports some of them directly:
    #
    # - domain_filter       → best-effort via site:/-site: operators
    # - recency_filter      → best-effort prompt hint
    # - language_filter     → best-effort prompt hint
    # - search_context_size → ignored at runtime
    # - search_mode         → ignored
    # - return_related      → ignored
    #
    # Option A: Project API key from the environment (recommended)
    # The Foundry project key is sent in the api-key header.
    #
    web_api_key = _require_env("AZURE_AI_PROJECT_API_KEY")

    search = AzureFoundryWebSearch(
        project_endpoint=project_endpoint,
        agent_name=agent_name,
        agent_version=agent_version,
        api_key=web_api_key,
    )

    # -- Option B: Hardcoded API key -------------------------------------------
    #
    # search = AzureFoundryWebSearch(
    #     project_endpoint=project_endpoint,
    #     agent_name=agent_name,
    #     agent_version=agent_version,
    #     api_key="your-api-key-from-azure-portal",
    # )

    # ── AgentConfig — all available options ──────────────────────────
    #
    # Only llm + search are required for explicit setup.  Every other
    # field has a sensible default.  Uncomment and change only what you
    # need.  If llm or search are omitted (None), they are auto-created
    # from environment variables / .env.
    agent = ResearchAgent(AgentConfig(
        # -- Providers (None = auto-create from env) --
        llm=llm,
        search=search,

        # -- Behaviour --
        max_rounds=4,                       # max research-loop iterations before forced stop
        # confidence threshold (1-10) — stop early when reached
        confidence_stop=9,
        first_round_queries=8,              # DEEP first-round search width
        answer_prompt_citations_max=500,    # DEEP citation context budget
        # hard wall-clock deadline for the entire run (seconds).
        # Active-run deadline shared by all four research rounds.
        max_total_seconds=3600,
        max_question_length=60_000,         # reject questions longer than this (characters)

        # -- Timeouts (per individual LLM/search call, in seconds) --
        reasoning_timeout=600,              # logical reasoning-operation budget
        search_timeout=600,                 # logical web-search budget, retries included
        claim_extract_timeout=600,          # logical claim-extraction budget

        # -- Risk scoring ──────────────────────────────────────────────
        #
        # The risk score (0-10) is computed automatically by the
        # RiskScoringStrategy (domain-neutral: recency, numerics,
        # normative language, length).  A score >= the threshold sets the
        # high_risk flag.
        #
        # high_risk is an observability signal only (forensic events,
        # /health, follow-up preservation); it does NOT change model
        # selection.  To run demanding questions on a stronger model, use
        # the model tiers (tier_high_model / tier_high_effort) -- see
        # docs/architecture/websearch/nodes.md.
        high_risk_score_threshold=4,        # risk score ≥ this triggers high_risk = True

        # -- Search cache --
        search_cache_maxsize=256,           # max cached search results (LRU eviction)
        search_cache_ttl=3600,              # cache time-to-live in seconds

        # -- Strategies (None = use built-in defaults) ────────────────
        # Each strategy is a pluggable ABC.  Pass your own implementation
        # to override the default algorithm for that concern.
        # source_tiering=None,              # URL → quality-tier mapping
        # claim_extraction=None,            # extract structured claims from search results
        # claim_consolidation=None,         # deduplicate and verify claims across rounds
        # risk_scoring=None,                # score question risk (0-10)
        # stop_criteria=None,               # multi-signal heuristic: keep researching or stop?
    ))

    # ── Run ──────────────────────────────────────────────────────────
    if USE_STREAMING:
        # Collect chunks: progress lines go to stdout immediately,
        # answer text is buffered for a final rich render pass.
        answer_buf: list[str] = []
        in_answer = False
        for chunk in agent.stream(QUESTION, include_progress=INCLUDE_PROGRESS):
            if not in_answer and chunk == "---\n":
                in_answer = True
                continue
            if in_answer:
                answer_buf.append(chunk)
            else:
                # Progress lines — always printed raw
                print(chunk, end="", flush=True)

        full_answer = "".join(answer_buf)
        if full_answer:
            console = Console()
            print()  # newline after progress block
            console.print(Markdown(full_answer))
    else:
        result = agent.research(QUESTION)
        _print_result(result)


if __name__ == "__main__":
    main()
