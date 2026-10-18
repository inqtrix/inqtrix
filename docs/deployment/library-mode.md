# Library mode

> Files: `src/inqtrix/agent.py`, `src/inqtrix/__init__.py`

## Scope

How to embed Inqtrix in a Python script or application as a library. Two common entry paths are documented: env-based auto-creation and explicit Baukasten providers. For the HTTP server see [Web server mode](webserver-mode.md).

## Prerequisites

- Python ≥ 3.11.
- Editable install with dev extras.
- At least one set of provider credentials in `.env` or the process environment.

See [Installation](../getting-started/installation.md) for the full bootstrap.

## Option A: `.env` or process environment

`ResearchAgent()` auto-loads a local `.env` from the current working directory on every run: the settings layer reads it for the tuning fields (`REPORT_PROFILE`, `MAX_ROUNDS`, the timeouts) even when you pass your own `llm` and `search` providers; only the provider auto-creation from `.env` is conditional and happens when `AgentConfig.llm` or `AgentConfig.search` is left unset.

Use this path when you want to select the whole stack from the environment: `INQTRIX_LLM_PROVIDER` (`litellm` | `anthropic` | `azure` | `bedrock`) and `INQTRIX_SEARCH_PROVIDER` (`perplexity` | `azure_foundry`) are two independent axes, so any LLM pairs with any search provider; the `.env` below shows the default LiteLLM plus Perplexity stack, and [Provider recipes](../getting-started/provider-recipes.md) has a copy-paste block for every combination.

```dotenv
# .env
LITELLM_BASE_URL=http://localhost:4000/v1
LITELLM_API_KEY=sk-...
REASONING_MODEL=gpt-4o
SEARCH_MODEL=perplexity-sonar-pro-agent
CLASSIFY_MODEL=gpt-4o-mini
CLAIM_EXTRACT_MODEL=gpt-4o-mini
EVALUATE_MODEL=gpt-4o-mini
REPORT_PROFILE=deep
```

```python
from inqtrix import ResearchAgent

agent = ResearchAgent()
result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")

print(result.answer)
print(f"Confidence: {result.metrics.confidence}/10")
print(f"Sources: {result.metrics.total_citations}")
print(f"Rounds: {result.metrics.rounds}")
```

If `CLASSIFY_MODEL`, `CLAIM_EXTRACT_MODEL`, or `EVALUATE_MODEL` are omitted, the node first falls back to its tier model (`TIER_FAST_MODEL` for classify and claim extraction, `TIER_MID_MODEL` for evaluate) and only then to `REASONING_MODEL`. Set `REPORT_PROFILE=deep` for a longer review-style answer.

## Option B: explicit providers (Baukasten pattern)

Use this path when you want to define providers and models directly in Python. No `.env` file is required by the providers themselves (Constructor-First); however, your script may still read environment variables from `.env` via `load_dotenv()`.

All built-in providers are top-level imports from `inqtrix`. Pick one LLM provider and one search provider, instantiate each with its own constructor arguments, then pass both into `AgentConfig`:

- LLM providers: `LiteLLM`, `AnthropicLLM`, `AzureOpenAILLM`, `BedrockLLM`.
- Search providers: `PerplexitySearch`, `AzureFoundryWebSearch`.

```python
import os
from dotenv import load_dotenv

from inqtrix import AgentConfig, LiteLLM, PerplexitySearch, ReportProfile, ResearchAgent


load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


llm = LiteLLM(
    api_key=_require_env("LITELLM_API_KEY"),
    base_url=os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1"),
    default_model="gpt-4o",
    classify_model="gpt-4o-mini",
    claim_extract_model="gpt-4o-mini",
    evaluate_model="gpt-4o-mini",
)

search = PerplexitySearch(
    api_key=_require_env("PERPLEXITY_API_KEY"),
)

agent = ResearchAgent(AgentConfig(
    llm=llm,
    search=search,
    report_profile=ReportProfile.DEEP,
    max_rounds=4,
    confidence_stop=8,
    max_total_seconds=3600,
))

result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")
print(result.answer)
```

To swap the stack, keep the `ResearchAgent` wiring and exchange only the imports and constructors.

This is the complete Baukasten swap: model names live on provider constructors (`LiteLLM`, `PerplexitySearch`, `AnthropicLLM`, ...), not on `AgentConfig`. See [Providers overview](../configuration/providers.md) and [Agent config](../configuration/agent-config.md).

### Model tiers

Instead of one model for every node, an LLM provider can route nodes to three tiers: `answer` to high, `plan`/`evaluate`/`direct_chat` to mid, and `classify`/`claim_extract` to fast. Per-tier effort turns reasoning on deliberately, so tiers differ by model alone until you set it.

```python
llm = AnthropicLLM(
    api_key=_require_env("ANTHROPIC_API_KEY"),
    tier_high_model="claude-opus-4-7",   tier_high_effort="medium",
    tier_mid_model="claude-sonnet-4-6",  tier_mid_effort="none",
    tier_fast_model="claude-haiku-4-5",  tier_fast_effort="none",
)
```

A per-node `<node>_model` argument still overrides its tier; an unset tier falls back to `default_model`. For the full call-site-to-tier mapping, the effort token semantics, and per-provider effort behaviour see [LLM calls, model tiers, and reasoning effort](../architecture/websearch/nodes.md).

Rule of thumb:

- `ResearchAgent()` auto-loads `.env` on every run; only the provider auto-creation from it is conditional on `AgentConfig.llm` / `AgentConfig.search` being unset.
- If your own script reads `os.environ[...]`, your script must either call `load_dotenv()` or rely on already-exported environment variables.

## Streaming

`research(...)` waits until the full loop is finished and returns a structured `ResearchResult`. `stream(...)` is the incremental variant for scripts and CLIs.

```python
from inqtrix import ResearchAgent

agent = ResearchAgent()
for chunk in agent.stream("Meine Frage"):
    print(chunk, end="", flush=True)
```

By default it emits live status messages first, then the final answer chunks. Pass `include_progress=False` to omit the intermediate updates:

```python
for chunk in agent.stream("Meine Frage", include_progress=False):
    print(chunk, end="", flush=True)
```

Progress messages look like `Analysiere Frage...`, `Plane Suchanfragen (Runde 1/4)...`, `Durchsuche 8 Suchanfragen (Runde 1/4)...`, `Bewerte Informationsqualitaet (nach Runde 1/4)...`, `Formuliere Antwort (nach 3 Runden)...` — the number in the search message counts the search queries executed in that round, not the sources found. These are the German variants: the progress strings are bilingual and follow the language detected in the question, so an English question yields `Analyzing question...`, `Planning search queries (round 1/4)...`, `Searching 8 queries (round 1/4)...`, `Evaluating information quality (after round 1/4)...` and `Formulating answer (after 3 rounds)...` instead.

Runnable versions of both paths live in [`examples/quickstart/`](../../examples/quickstart): `basic_env.py` for the blocking call and `streaming.py` for the streaming loop.

## Related docs

- [Providers overview](../configuration/providers.md)
- [Writing a custom provider](../extending/provider.md)
- [Agent config](../configuration/agent-config.md)
- [Web server mode](webserver-mode.md)
- [React UI](react-ui.md)
