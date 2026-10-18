"""Azure OpenAI v1 + Perplexity Sonar example (no LiteLLM proxy).

Architecture
------------
This example combines two independent components directly:

1. **Sprachmodell / Reasoning** -- via ``AzureOpenAILLM``
2. **Websuche / Grounding** -- via ``PerplexitySearch``

Recommended validation order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Before using this combined example, validate the Azure reasoning path first:

- ``examples/provider_stacks/azure_smoke_tests/test_llm.py``

Related combined examples:

- ``examples/provider_stacks/azure_foundry_web_search.py`` uses
    ``AzureOpenAILLM`` + ``AzureFoundryWebSearch``.

Why no dedicated Perplexity smoke test here?

- ``PerplexitySearch`` is already a thin direct API wrapper with a single key.
- The Azure side is usually the more failure-prone setup, so
    ``azure_smoke_tests/test_llm.py`` gives the highest-value isolated smoke test.
- This file is therefore the main full-stack example for the
    ``AzureOpenAILLM`` + ``PerplexitySearch`` combination.

This combined script itself uses:
``AzureOpenAILLM`` + ``PerplexitySearch``.

This example calls two independent APIs directly:

1. **Azure OpenAI v1 Chat Completions API** — via ``AzureOpenAILLM``,
     using the official ``openai`` SDK's generic ``OpenAI`` client
     against the Azure ``/openai/v1/`` endpoint.

2. **Perplexity Sonar API** — via ``PerplexitySearch``, using the
     OpenAI Python SDK pointed at ``https://api.perplexity.ai``.

Authentication for Azure OpenAI supports three common modes:

* **API key** (default below) — the simplest path.  Get a key from
    the Azure Portal under your resource's "Keys and Endpoint".
* **Service Principal (Entra ID)** — for CI/CD, automation, and
    environments without interactive login.  Requires the
    ``azure-identity`` package (included in core dependencies).
* **DefaultAzureCredential** — for local development with ``az login``,
    Managed Identity, or VS Code Azure sign-in.

Use this example when:
- you have an Azure OpenAI resource with a model deployment
- you have a direct Perplexity API key
- you do NOT want to run a LiteLLM proxy
- you want to use GPT models hosted on Azure (e.g. EU region)

Required environment variables (in .env or process env):
- PERPLEXITY_API_KEY
- AZURE_OPENAI_API_KEY          (for API-key auth)
- AZURE_OPENAI_ENDPOINT         (e.g. https://mein-openai.openai.azure.com/)
- AZURE_OPENAI_DEPLOYMENT_NAME  (the deployment name, NOT the model name)

Optional:
- AZURE_OPENAI_CLAIM_EXTRACT_DEPLOYMENT_NAME  (cheaper deployment for claim extraction)
- HTTPS_PROXY                             (for corporate proxy environments)

For Service Principal auth (instead of API key):
- AZURE_TENANT_ID
- AZURE_CLIENT_ID
- AZURE_CLIENT_SECRET

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

        uv run python examples/provider_stacks/azure_openai_perplexity.py
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from inqtrix import AgentConfig, AzureOpenAILLM, PerplexitySearch, ResearchAgent

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
    perplexity_key = _require_env("PERPLEXITY_API_KEY")
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")

    # ── LLM Provider ────────────────────────────────────────────────
    #
    # AzureOpenAILLM calls the Azure OpenAI v1 Chat Completions API via
    # the official openai SDK's generic OpenAI client.
    #
    # Internally, the provider converts the resource endpoint
    #
    #   https://<resource>.openai.azure.com/
    #
    # into the v1 base URL
    #
    #   https://<resource>.openai.azure.com/openai/v1/
    #
    # so you do NOT need to manage date-based api-version strings.
    #
    # ⚠️  IMPORTANT: The "model" parameter is the DEPLOYMENT NAME you
    # created in the Azure Portal under "Model Deployments", NOT the
    # underlying model name (e.g. "gpt-4o").  If your deployment is
    # called "my-gpt4o-deployment", use that string here.
    #
    # Authentication — choose ONE of the three options below:
    #
    # ── Option A: API Key (default, uncommented) ────────────────────
    #
    # The simplest path.  Get your key from the Azure Portal:
    # Resource → "Keys and Endpoint" → KEY1 or KEY2.
    #
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        # classify_model="my-gpt4o-mini-deployment",
        # evaluate_model="my-gpt4o-mini-deployment",
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
        # token_budget_parameter="max_tokens",  # only if a deployment requires the older field
        # temperature=0.7,                                  # mutually exclusive with *_reasoning_effort
        # default_reasoning_effort="low",                   # classify, plan, evaluate, answer
        #   Allowed values: "none", "minimal", "low", "medium", "high", "xhigh".
        #   Per-value caveats (gpt-5.1 needs explicit value, "minimal" only on
        #   original gpt-5, "xhigh" only on gpt-5.1-codex-max, etc.) are enforced
        #   by Azure with HTTP 400. See docs/configuration/providers.md.
    )

    # ── Option B: Service Principal — direct constructor args ──────
    #
    # Recommended for CI/CD, automation, or environments without
    # interactive login. The provider builds the ``ClientSecretCredential``
    # and token provider internally, so you do not need to import
    # ``azure.identity`` yourself. Requires: uv sync
    #
    # Uncomment the block below and comment out Option A above.
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     tenant_id=os.environ["AZURE_TENANT_ID"],
    #     client_id=os.environ["AZURE_CLIENT_ID"],
    #     client_secret=os.environ["AZURE_CLIENT_SECRET"],
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # ── Option C: Service Principal — manual token provider ────────
    #
    # Same effect as Option B, but you build the token provider
    # yourself. Use this when you need custom credential settings
    # (token caching, regional authority, additional scopes, etc.).
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
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # ── Option D: Existing token provider ──────────────────────────
    #
    # If your application already centralizes Azure auth, you can pass
    # a prebuilt bearer-token provider directly.
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     azure_ad_token_provider=existing_token_provider,
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # ── Option E: DefaultAzureCredential (local dev / Managed Identity) ─
    #
    # Uses your ``az login`` session, VS Code credentials, Managed
    # Identity, etc.  Simplest for local development. Pass the
    # credential directly — the provider builds the token provider for
    # you.
    #
    # from azure.identity import DefaultAzureCredential
    #
    # llm = AzureOpenAILLM(
    #     azure_endpoint=azure_endpoint,
    #     credential=DefaultAzureCredential(),
    #     # tier_high_model / tier_mid_model / tier_fast_model as in Option A
    # )

    # ── Proxy (optional) ────────────────────────────────────────────
    #
    # For corporate environments that route HTTPS traffic through a
    # proxy.  Uncomment and add proxy_url to the AzureOpenAILLM
    # constructor above:
    #
    #   proxy_url=os.environ.get("HTTPS_PROXY"),
    #
    # The provider will create an httpx client with that proxy.
    # Alternatively, set the HTTPS_PROXY environment variable — the
    # openai SDK's httpx transport picks it up automatically.

    # ── Search Provider ─────────────────────────────────────────────
    #
    # PerplexitySearch is the direct web-search component used by THIS file.
    #
    # Compared with the Azure search backends, Perplexity supports more of the
    # standard SearchProvider parameters natively inside the API request:
    #
    # - recency_filter      → native API field
    # - language_filter     → native API field
    # - domain_filter       → native API field
    # - search_mode         → native API field (e.g. academic)
    # - return_related      → native API field
    # - search_context_size → native API field
    #
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
    # Model names are Perplexity-native here (sonar-pro, sonar),
    # not LiteLLM aliases (perplexity-sonar-pro-agent).
    #
    # Option A: higher-quality default model
    search = PerplexitySearch(
        api_key=perplexity_key,
        base_url="https://api.perplexity.ai",
    )

    # Option B: cheaper / faster model
    # search = PerplexitySearch(
    #     api_key=perplexity_key,
    #     base_url="https://api.perplexity.ai",
    #     model="sonar",
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
        confidence_stop=8,
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
