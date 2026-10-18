# Writing a custom provider

## Scope

How to integrate a backend that is not covered by the built-in providers. The walkthrough covers both `SearchProvider` and `LLMProvider`, shows concrete `main.py` wiring, and flags the conventions the custom adapter must follow.

## Checklist before you start

- Look at [Providers overview](../configuration/providers.md) first — a dedicated adapter is only needed when model params and `extra_body` cannot express what you want.
- Confirm that no in-tree provider covers your target. Custom Azure flavours should probably subclass an existing adapter rather than starting from scratch.
- Plan for the Constructor-First convention: your adapter must not read environment variables; pass every secret and endpoint as a constructor argument.

## `SearchProvider`

```python
from inqtrix import ResearchAgent, AgentConfig, SearchProvider
from inqtrix.search_result import GroundedSearchResult, GroundedSource


class CustomWebSearch(SearchProvider):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> GroundedSearchResult:
        return GroundedSearchResult(
            answer="Summarised answer text",
            sources=[
                GroundedSource(url="https://source1.com", title="Source 1", rank=1),
                GroundedSource(url="https://source2.com", title="Source 2", rank=2),
            ],
            related_questions=[],
            prompt_tokens=0,
            completion_tokens=0,
        )

    def is_available(self) -> bool:
        return bool(self._api_key)


agent = ResearchAgent(AgentConfig(search=CustomWebSearch(api_key="...")))
```

### Hints are optional, return shape is not

Inqtrix passes the hints as keyword arguments but does not require every backend to honour them. `query` is the only required input; `recency_filter`, `language_filter`, `domain_filter`, `search_mode`, `return_related` are best-effort. The return type above is **mandatory** — the search node fails fast when a provider returns a legacy dict instead of `GroundedSearchResult`.

If your backend rejects unsupported parameters, declare the accepted subset. The search node will filter every other hint before calling your provider:

```python
class CustomWebSearch(SearchProvider):
    supported_search_parameters = frozenset({
        "search_context_size",
        "recency_filter",
        "language_filter",
        "return_related",
    })
```

Use `search_capabilities` when the set is dynamic:

```python
from inqtrix.providers.base import SearchProviderCapabilities


class MySearch(SearchProvider):
    @property
    def search_capabilities(self) -> SearchProviderCapabilities:
        return SearchProviderCapabilities(
            supported_parameters=frozenset({"domain_filter"})
        )
```

### Add a `search_model` property

`SearchProvider.search_model` has a loud default (`"<ClassName>(unknown)"`) so `/health` and `/v1/stacks` stay useful even for external subclasses. Override it to name the backend (see the built-in providers in [Providers overview](../configuration/providers.md)):

```python
@property
def search_model(self) -> str:
    return "custom-web-search-v1"
```

### Provider provenance via `GroundedSource`

Each `GroundedSource` row drives the per-source forensic events (`source_record`, `provider_citation_record`, `claim_record`). Fill `url`, `rank`, `title`, `snippet`, `date`, `last_updated`, and `origin` when the backend exposes them; omit fields the backend does not know.

```python
def search(self, query, **hints):
    raw = self._call_backend(query, **hints)
    sources = [
        GroundedSource(
            url=item["url"],
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            rank=index + 1,
            origin="search_result",
        )
        for index, item in enumerate(raw)
    ]
    return GroundedSearchResult(answer=self._compose_answer(raw), sources=sources)
```

See the [Forensic cookbook](../observability/forensic-cookbook.md) for the full event catalog and `jq`/Python recipes that consume normalized source records.

## `LLMProvider`

```python
from inqtrix import ResearchAgent, AgentConfig, LLMProvider


class MyLLM(LLMProvider):
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = 600.0,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        return "LLM response"

    def is_available(self) -> bool:
        return True


agent = ResearchAgent(AgentConfig(llm=MyLLM()))
```

### Exposing a `.models` property

`ResearchAgent` expects the provider to tell the strategies layer which model each role resolves to. If your adapter exposes the `ModelSettings`-shaped `.models` property, the strategies consume it directly. If not, `ResearchAgent` wraps the provider with default `ModelSettings` from the environment (legacy safety net); however, that path can misreport model names on non-LiteLLM stacks. Provide `.models` explicitly.

Every LLM implementation exposes `.models` — a `ModelSettings`-shaped object that captures the model names chosen at construction time. Code that needs to know which model a given role resolves to reads it **constructor-first**:

```python
provider.models.effective_classify_model    # falls back to reasoning
provider.models.effective_claim_extract_model   # falls back to reasoning
provider.models.effective_evaluate_model    # falls back to reasoning
provider.models.reasoning_model
```

The `resolve_claim_extract_model(llm, fallback)` helper in `inqtrix.strategies` is the canonical way to read the claim-extraction model; it emits a warning marker when neither constructor path nor reasoning fallback yields a value and a non-empty `fallback` was passed, so callers that omit `fallback` — `ResearchAgent` among them — get an empty model name back without a log line. Use that helper when you write custom strategy wiring so provider-specific model names do not leak back to global `Settings` defaults.

### Optional structured output

`LLMProvider.complete_structured()` is optional. The default implementation
reports `supports_structured_output() == False`, so existing custom providers
continue to work through the legacy prompt-based JSON path. Implement it only
when the backend can enforce a caller-supplied JSON Schema natively.

The claim extractor uses this capability for the high-volume claim-extraction
helper call. When your provider returns `True`, it must either return a
`StructuredLLMResponse` with a parsed top-level dictionary or raise a provider
error / `AgentStructuredOutputError`. The search node will surface failures as
`ALGO-FAIL claim_extraction`; do not swallow malformed structured responses.

```python
from typing import Any
from inqtrix import LLMProvider, StructuredLLMResponse


class MyLLM(LLMProvider):
    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return model in {"my-json-schema-model"}

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = 600.0,
        state: dict | None = None,
        deadline: float | None = None,
    ) -> StructuredLLMResponse:
        parsed = {"claims": []}  # Replace with the backend's parsed response.
        return StructuredLLMResponse(parsed=parsed, model=model or "", schema_name=schema_name)
```

### Direct Anthropic example (no LiteLLM)

```python
import os
from dotenv import load_dotenv
from inqtrix import AgentConfig, AnthropicLLM, ResearchAgent


load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


agent = ResearchAgent(AgentConfig(
    llm=AnthropicLLM(
        api_key=_require_env("ANTHROPIC_API_KEY"),
        default_model="claude-sonnet-4-6",
        classify_model="claude-haiku-4-5",
        claim_extract_model="claude-haiku-4-5",
        evaluate_model="claude-sonnet-4-6",
        # thinking={"type": "adaptive"},
    ),
))
```

## Mixing custom and built-in providers

You can set only one side and leave the other `None`:

- Only custom search: `search=MySearch(...)` with `llm=None`. `ResearchAgent` will auto-create the LLM from environment variables.
- Only custom LLM: `llm=AnthropicLLM(...)` with `search=None`. Same auto-creation for the search side.

The distinction matters when it comes to `.env` loading:

- `ResearchAgent()` env mode: `.env` is auto-loaded internally by the settings layer.
- Custom-provider scripts that call `os.environ[...]` themselves must call `load_dotenv()` or rely on exported shell variables.

## Full custom `main.py`

```python
import os
from dotenv import load_dotenv
from my_project.providers import MySearch
from inqtrix import AgentConfig, AnthropicLLM, ResearchAgent


load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


agent = ResearchAgent(AgentConfig(
    llm=AnthropicLLM(
        api_key=_require_env("ANTHROPIC_API_KEY"),
        default_model="claude-sonnet-4-6",
        classify_model="claude-haiku-4-5",
        claim_extract_model="claude-haiku-4-5",
    ),
    search=MySearch(api_key=_require_env("MY_SEARCH_API_KEY")),
    max_rounds=4,
    confidence_stop=8,
))

result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")

print(result.answer)
print(f"Confidence: {result.metrics.confidence}/10")
print(f"Sources: {result.metrics.total_citations}")
print(f"Rounds: {result.metrics.rounds}")
```

Run it with:

```bash
# uv
uv sync --extra dev
uv run python main.py

# or standard Python/pip
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python main.py
```

For streaming output, replace the last block with a `for chunk in agent.stream(...)` loop.

## `ProviderContext`

Frozen dataclass bundling the active providers:

```python
@dataclass(frozen=True)
class ProviderContext:
    llm: LLMProvider
    search: SearchProvider
```

Every node receives `providers` via dependency injection; tests replace providers with stubs against this interface only.

## Constructor-first convention

All built-in providers honour the Constructor-First principle: they do not read environment variables directly. The example scripts under `examples/provider_stacks/`, `examples/webserver_stacks/`, and `examples/custom_providers/` are where `.env` translates to constructor arguments. The documentation surface of the provider ABCs and their implementations is locked in by the structural tests in `tests/test_docstring_completeness.py`, which assert that public symbols, public methods and Pydantic fields carry non-empty docstrings and `Field(description=...)` values.

If you integrate a new provider, follow the same pattern:

- Read no env var inside the provider module.
- Accept every secret, endpoint, deployment, and model id as a named constructor argument.
- Use `sanitize_log_message(...)` (from `inqtrix.urls`) before logging anything that may contain credentials.

## Related docs

- [Providers overview](../configuration/providers.md)
- [Agent config](../configuration/agent-config.md)
- [Library mode](../deployment/library-mode.md)
