"""Explicit LiteLLM + PerplexitySearch example.

Architecture
------------
The LiteLLM proxy is the gateway for the language models (e.g. Claude).
Web search runs against the native Perplexity Agent API directly — the
Perplexity SDK cannot be routed through LiteLLM — so ``PerplexitySearch``
uses its own endpoint (``https://api.perplexity.ai``) and a dedicated
``PERPLEXITY_API_KEY``, independent of the LiteLLM gateway.

Use this example when:
- your reasoning models are reachable through a LiteLLM- or OpenAI-compatible endpoint
- you want to see how providers are configured explicitly (Baukasten pattern)

Required environment variables (in .env or process env):
- LITELLM_API_KEY
- PERPLEXITY_API_KEY
- optionally LITELLM_BASE_URL (defaults to http://localhost:4000/v1)

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

    uv run python examples/provider_stacks/litellm_perplexity.py
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from inqtrix import AgentConfig, LiteLLM, PerplexitySearch, ResearchAgent, ReportProfile

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
# File-based logging with automatic secret redaction.  Controlled via
# environment variables so the example stays quiet unless requested:
#
#   INQTRIX_LOG_ENABLED=true   — write logs to logs/inqtrix_*.log
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_CONSOLE=true   — also print WARNING+ to stderr
#   OBSERVABILITY_PROFILE=forensic — lineage EVENT JSON in the same log file; requires
#     INQTRIX_LOG_LEVEL=DEBUG. docs/observability/logging.md,
#     docs/observability/forensic-cookbook.md
from inqtrix.logging_config import configure_logging

LOG_ENABLED = os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true"
LOG_LEVEL = os.getenv("INQTRIX_LOG_LEVEL", "INFO")
LOG_TO_CONSOLE = os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true"

_log_path = configure_logging(
    enabled=LOG_ENABLED,
    level=LOG_LEVEL,
    console=LOG_TO_CONSOLE,
)
if _log_path:
    print(f"Logging to {_log_path}")

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
    api_key = _require_env("LITELLM_API_KEY")
    base_url = os.environ.get(
        "LITELLM_BASE_URL", "http://localhost:4000/v1"
    ).strip()

    # ── LLM Provider ────────────────────────────────────────────────
    #
    # default_model: the primary model used for reasoning, query
    #   planning, and final answer synthesis.  This is where the
    #   strongest model usually belongs.
    #
    # classify_model / claim_extract_model / evaluate_model: optional
    #   per-role overrides.  If left empty (""), each falls back to
    #   default_model.
    #
    #   classify_model: good place for a smaller model if the question
    #   decomposition is straightforward and you mainly want to save
    #   cost on the first routing step.
    #
    #   claim_extract_model: usually the best place to save money because
    #   it runs in parallel on many search results.  If claim
    #   extraction gets too shallow or noisy, move this role up.
    #
    #   evaluate_model: useful for a slightly cheaper model than
    #   default_model, but keep it strong enough for evidence weighing
    #   and stop decisions.
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

    # ── Search Provider ─────────────────────────────────────────────
    search = PerplexitySearch(
        api_key=_require_env("PERPLEXITY_API_KEY"),
        base_url="https://api.perplexity.ai",
    )

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
        confidence_stop=8,
        first_round_queries=8,              # DEEP first-round search width
        answer_prompt_citations_max=500,    # DEEP citation context budget
        # hard wall-clock deadline for the entire run (seconds).
        # Active-run deadline shared by all four DEEP rounds.
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
        # profile for generating research reports. You can set this to DEEP or COMPACT based on how detailed you want the final report to be.  DEEP includes more claims, citations, and a longer answer, while COMPACT is more concise and cost-effective.
        report_profile=ReportProfile.DEEP,
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
