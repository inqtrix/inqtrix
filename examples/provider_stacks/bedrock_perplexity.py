"""Direct Amazon Bedrock + Perplexity example (no LiteLLM proxy).

Architecture
------------
This example calls two independent APIs directly:

1. **Amazon Bedrock Converse API** — via ``BedrockLLM``, using a native
   ``boto3`` adapter.  Claude models are called through the Bedrock
   Runtime endpoint in your configured AWS region.

2. **Perplexity Sonar API** — via ``PerplexitySearch``, using the
   OpenAI Python SDK pointed at ``https://api.perplexity.ai``.

Authentication for Bedrock uses **AWS named profiles** configured in
``~/.aws/config`` and ``~/.aws/credentials``.  The profile name and
region are read from environment variables (or ``.env``).

Use this example when:
- you have an AWS account with Bedrock model access enabled
- you have a direct Perplexity API key
- you do NOT want to run a LiteLLM proxy
- you want to use Claude models hosted on Bedrock (e.g. EU region)

Required environment variables (in .env or process env):
- PERPLEXITY_API_KEY
- AWS_PROFILE          (optional — defaults to the default profile)
- AWS_REGION           (optional — defaults to eu-central-1)

Prerequisites:
- ``boto3`` must be installed: ``uv sync``
- The AWS profile must have ``bedrock:InvokeModel`` permission
- The requested models must be enabled in the target region

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

    uv run python examples/provider_stacks/bedrock_perplexity.py
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from inqtrix import AgentConfig, BedrockLLM, PerplexitySearch, ResearchAgent, ReportProfile

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
# File-based logging with automatic secret redaction.  Disabled by
# default — enable via environment variables to keep terminal clean:
#
#   INQTRIX_LOG_ENABLED=true   — write logs to logs/inqtrix_*.log
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_CONSOLE=true   — also print WARNING+ to stderr
#   OBSERVABILITY_PROFILE=forensic — lineage EVENT JSON in the same log file; requires
#     INQTRIX_LOG_LEVEL=DEBUG. docs/observability/logging.md,
#     docs/observability/forensic-cookbook.md
from inqtrix.logging_config import configure_logging

_log_path = configure_logging(
    enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
    level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
    console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
)
if _log_path:
    print(f"Logging to {_log_path}")

QUESTION = "Was waren die wichtigsten KI-Entwicklungen der letzten 7 Tage und welche Auswirkung hatte das auf die Wirtschaft?"


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
    perplexity_key = _require_env("PERPLEXITY_API_KEY")
    aws_profile = os.environ.get("AWS_PROFILE", "").strip() or None
    aws_region = os.environ.get("AWS_REGION", "").strip() or "eu-west-1"

    # ── LLM Provider ────────────────────────────────────────────────
    #
    # BedrockLLM calls the Amazon Bedrock Converse API via boto3.
    # Authentication is handled through AWS named profiles.
    #
    # default_model: Bedrock model ID for reasoning calls (classify,
    #   plan, evaluate, answer).  Uses the EU cross-region inference
    #   profile for Opus.
    #
    # claim_extract_model: cheaper model for per-source claim extraction.
    #   and claim extraction (called in parallel threads).
    #
    # Bedrock model IDs (EU region):
    #   eu.anthropic.claude-opus-4-6-v1
    #   eu.anthropic.claude-sonnet-4-6
    #   eu.anthropic.claude-sonnet-4-5-20250929-v1:0
    #
    # Extended thinking (optional):
    #   thinking={"type": "adaptive"}
    #
    # NOTE: temperature and thinking are MUTUALLY EXCLUSIVE.
    #
    # Optional tuning (defaults shown):
    #   default_max_tokens=65536    — output budget for reasoning calls
    #
    # When thinking is enabled, max_tokens remains at least 16384 so
    # older custom low-budget overrides still leave room for both
    # thinking and the visible answer.
    #
    # Transient Bedrock failures (throttling, 5xx) are retried
    # automatically with bounded backoff before the provider gives up.
    #
    # effort (optional, "low" | "medium" | "high" | "xhigh" | "max"):
    #   Forwarded to Bedrock via additionalModelRequestFields.output_config.
    #   Controls overall token spend (text + tool calls + thinking).
    #   Bedrock has noticeably higher per-call latency than direct Anthropic;
    #   "medium" combined with adaptive thinking typically cuts wall-clock by
    #   ~30-40 % at the same answer confidence — particularly impactful on
    #   the Bedrock path. The Anthropic API default is "high" (≈ omitted).
    # Models:
    # eu.anthropic.claude-opus-4-6-v1
    # eu.anthropic.claude-sonnet-4-6

    # # Option 1: ===============================
    # region_name="eu-west-1"
    # default_model="google.gemma-3-27b-it",
    # claim_extract_model="google.gemma-3-27b-it",
    # ==============================================
    # Option 2: ===============================
    # region_name="eu-west-2"
    # default_model="nvidia.nemotron-super-3-120b",
    # ==============================================
    # Option 3: ===============================
    # region_name="eu-west-2"
    # default_model="zai.glm-5",
    # claim_extract_model="zai.glm-5",
    # ==============================================
    # Option 4: ===============================
    # region_name="us-west-2"
    # default_model="openai.gpt-oss-120b-1:0",
    # claim_extract_model="openai.gpt-oss-120b-1:0",
    # effort="medium" # or 'high', thinking beeds to be turned off. Thinking is only für Claude models.
    # ==============================================
    llm = BedrockLLM(
        profile_name=aws_profile,
        region_name=aws_region,
        # thinking={"type": "adaptive"},
        # Recommended default for Bedrock research workloads.
        # Drop or change to "high" for tasks needing maximum reasoning depth.
        # effort="medium",
        # Three model tiers (Bedrock model IDs, eu-* to match region_name
        # above). Nodes map: answer -> high, plan/evaluate/direct_chat -> mid,
        # classify/claim_extract -> fast. Per-tier effort turns reasoning on
        # deliberately (only the high tier here, Claude models); tiers
        # otherwise differ by model alone. A per-node <node>_model arg
        # overrides the tier. See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="eu.anthropic.claude-opus-4-7-v1",
        tier_high_model="eu.anthropic.claude-opus-4-7-v1",   tier_high_effort="medium",
        tier_mid_model="eu.anthropic.claude-sonnet-4-6",     tier_mid_effort="none",
        tier_fast_model="eu.anthropic.claude-haiku-4-5",     tier_fast_effort="none",
    )

    # ── Search Provider ─────────────────────────────────────────────
    #
    # PerplexitySearch pointed directly at the Perplexity Sonar API.
    # This provider is designed specifically for the Sonar API — other
    # Perplexity products (Deep Research, Agent API) use different
    # parameters and endpoints.
    #
    # base_url is "https://api.perplexity.ai" (no /v1 suffix — the
    # OpenAI SDK appends /chat/completions internally).
    #
    # The provider auto-detects direct mode from the URL and formats
    # search parameters (recency, language, domain filters, etc.) as
    # flat top-level extra_body keys — which is what the Perplexity
    # API expects.  Through a LiteLLM proxy these would be nested
    # inside web_search_options instead.
    #
    search = PerplexitySearch(
        api_key=perplexity_key,
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
        # max research-loop iterations before forced stop
        max_rounds=4,
        # confidence threshold (1-10) — stop early when reached
        confidence_stop=8,
        first_round_queries=8,
        answer_prompt_citations_max=500,
        # hard wall-clock deadline for the entire run (seconds).
        # DEEP synthesis and all search rounds share this active-run budget.
        max_total_seconds=3600,
        max_question_length=60_000,         # reject questions longer than this (characters)

        # -- Timeouts (per individual LLM/search call, in seconds) --
        reasoning_timeout=600,              # logical reasoning budget, retries included
        search_timeout=600,                 # logical search budget, retries included
        claim_extract_timeout=600,          # logical extraction budget, retries included

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
