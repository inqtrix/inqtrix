# Providers

> Package: `src/inqtrix/providers/`

## Scope

The provider package is the Baukasten surface. Every LLM backend and every search backend is an implementation of one of two ABCs and can be swapped independently. This page lists the built-ins, the contracts they satisfy, and the conventions custom providers must follow.

For the **server/Stack-mode path**, pick a combination purely via `.env` (`INQTRIX_LLM_PROVIDER` × `INQTRIX_SEARCH_PROVIDER`) — copy-paste recipes for every combo are in [Provider recipes](../getting-started/provider-recipes.md). This page is the deeper per-provider reference behind those recipes.

## Provider matrix

| Role | Provider class | Module | Backend |
|------|----------------|--------|---------|
| LLM | `LiteLLM` | `providers/litellm.py` | Any OpenAI-compatible endpoint |
| LLM | `AnthropicLLM` | `providers/anthropic.py` | Anthropic Messages API |
| LLM | `AzureOpenAILLM` | `providers/azure.py` | Azure OpenAI Chat Completions |
| LLM | `BedrockLLM` | `providers/bedrock.py` | Amazon Bedrock Converse API |
| Search | `PerplexitySearch` | `providers/perplexity.py` | Perplexity Agent API (TTL cache) |
| Search | `AzureFoundryWebSearch` | `providers/azure_web_search.py` | Azure Foundry Web Search agent |

All six providers are exported from the top-level `inqtrix` package.

Use the matrix as a menu: import one LLM provider and one search provider, construct them with their own credentials and model names, then pass both into `AgentConfig`:

```python
from inqtrix import AgentConfig, AnthropicLLM, PerplexitySearch, ResearchAgent


llm = AnthropicLLM(
    api_key="sk-ant-...",
    default_model="claude-sonnet-4-6",
    claim_extract_model="claude-haiku-4-5",
)
search = PerplexitySearch(
    api_key="pplx-...",
    model="sonar-pro",
)

agent = ResearchAgent(AgentConfig(llm=llm, search=search))
result = agent.research("What changed in EU AI Act enforcement this quarter?")
print(result.answer)
```

The same wiring works for any provider pair in the matrix; only the imports and constructor arguments change. In real applications, read secrets from environment variables or a secret manager rather than hard-coding them.

## LiteLLM

> File: `src/inqtrix/providers/litellm.py`

### When to use LiteLLM

- You run a LiteLLM proxy that handles routing, retries, and quota for you.
- You want a single provider that can reach both reasoning models and search-capable models through one endpoint.
- You are prototyping and want the shortest path to a working setup.

If your backend is **not** OpenAI-shaped, implement `LLMProvider` directly instead. See [Writing a custom provider](../extending/provider.md).

### LiteLLM constructor

```python
from inqtrix import LiteLLM


llm = LiteLLM(
    api_key="sk-...",
    base_url="http://localhost:4000/v1",
    default_model="gpt-4o",
    classify_model="gpt-4o-mini",
    claim_extract_model="gpt-4o-mini",
    evaluate_model="gpt-4o-mini",
)
```

| Parameter | Purpose |
|-----------|---------|
| `api_key` | Proxy API key; passed straight to the OpenAI SDK. |
| `base_url` | Proxy URL including `/v1`. Constructor default `http://localhost:4000/v1`. In server mode the `LITELLM_BASE_URL` env default is `http://litellm-proxy:4000/v1`, the Compose service name. |
| `default_model` | Fallback model used for `reasoning`, `plan`, `answer`. |
| `classify_model`, `claim_extract_model`, `evaluate_model` | Optional per-role overrides; each falls back to `default_model` when omitted. |
| `plan_model`, `answer_model`, `direct_chat_model` | Further per-role overrides, same fallback rule as the other role models. |
| `tier_high_model`, `tier_mid_model`, `tier_fast_model` | Tier routing. Nodes map to a tier via `inqtrix.model_routing.NODE_TIER_ASSIGNMENT`; an empty tier and a per-role override both fall back to `default_model`. |
| `tier_high_effort`, `tier_mid_effort`, `tier_fast_effort` | Per-tier reasoning effort, resolved per call and forwarded by the adapter. |
| `default_max_tokens` | Default output-token budget per reasoning call; `64000`. Sent in the request field named by `token_budget_parameter` so the adapter can record an honest token-utilisation ratio. |
| `token_budget_parameter` | Which request field carries the output-token budget. `"max_tokens"` is the legacy default and works with most LiteLLM proxies. Switch to `"max_completion_tokens"` when targeting OpenAI o-series reasoning models directly. |

### Example stack

Library mode:

```python
from inqtrix import AgentConfig, LiteLLM, PerplexitySearch, ResearchAgent

agent = ResearchAgent(AgentConfig(
    llm=LiteLLM(api_key="sk-...", base_url="http://localhost:4000/v1",
                default_model="gpt-4o"),
    search=PerplexitySearch(api_key="pplx-...",
                            base_url="https://api.perplexity.ai"),
    max_rounds=4,
    confidence_stop=8,
))
```

Server mode: `examples/webserver_stacks/litellm_perplexity.py` wires the same Baukasten pair behind the FastAPI server; it reads `LITELLM_BASE_URL` from the environment and routes through the three model tiers instead of a single `default_model`. See [Web server mode](../deployment/webserver-mode.md).

### Response shape expected from the endpoint

The adapter reads the text from any of the standard OpenAI-compatible shapes:

- Non-stream: `choices[0].message.content`.
- SSE chunk stream encoded as text: `choices[*].delta.content`.
- Optional token metadata: `usage.prompt_tokens` and `usage.completion_tokens`.

Both plain JSON responses and SSE payloads encoded as a single string are accepted, so proxies that collapse an SSE stream into one blob still work.

### Limitations

- Hidden OpenAI SDK retries are disabled. `LiteLLM` retries transient 408/409/5xx and SDK timeout/connection errors itself so attempts are logged and surfaced in live progress. HTTP 429 still escalates to `AgentRateLimited`.
- Any other 4xx is never retried. One whose structured error code is `content_filter` (the Azure/OpenAI content filter refused the request) is classified as a content refusal of that request (`provider_failure_contract.CONTENT_REFUSAL_CODES`): the whole-document analysis then leaves only the refused window unanalysed and names it, instead of stopping the reading, and knowledge contextualization indexes the refused batch without retrieval context instead of pausing the revision.
- Anthropic's `thinking` block is not part of this adapter. Reasoning effort is: `tier_high_effort` / `tier_mid_effort` / `tier_fast_effort` are resolved per call and forwarded as the OpenAI-compatible `reasoning_effort` request field. A gateway that cannot honour a graded value rejects the request visibly. For an explicit thinking budget use `AnthropicLLM` or `BedrockLLM`.
- Token-usage metadata is only as good as the upstream proxy: a proxy that strips `usage` from the response will report zeros.

## Anthropic

> File: `src/inqtrix/providers/anthropic.py`

### When to use Anthropic

- You already have an Anthropic API key and want the shortest path to Claude.
- You want to enable extended thinking (`thinking={"type": "adaptive"}`) on reasoning-heavy roles.
- You want the adapter's own visible retry loop (at most 3 total attempts within one operation deadline) rather than hidden SDK retry semantics.

### Anthropic constructor

```python
from inqtrix import AnthropicLLM


llm = AnthropicLLM(
    api_key="sk-ant-...",
    default_model="claude-sonnet-4-6",
    classify_model="claude-haiku-4-5",   # optional, cheaper role
    claim_extract_model="claude-haiku-4-5",  # usually the biggest cost lever
    evaluate_model="claude-sonnet-4-6",  # optional per-role override
    # thinking={"type": "adaptive"},     # optional reasoning budget
)
```

| Parameter | Purpose |
|-----------|---------|
| `api_key` | Anthropic API key. Read from `.env` in the example scripts. |
| `default_model` | Fallback for every role: used when neither a per-role override nor a tier model applies. |
| `classify_model` / `evaluate_model` / `claim_extract_model` | Optional per-role overrides. |
| `plan_model`, `answer_model`, `direct_chat_model` | Further per-role overrides, same fallback rule as the other role models. |
| `tier_high_model`, `tier_mid_model`, `tier_fast_model` | Tier routing. Nodes map to a tier via `inqtrix.model_routing.NODE_TIER_ASSIGNMENT`; an empty tier and a per-role override both fall back to `default_model`. |
| `tier_high_effort`, `tier_mid_effort`, `tier_fast_effort` | Per-tier reasoning effort, resolved per call and forwarded by the adapter. |
| `effort` | Constructor-level reasoning effort (`"low"`, `"medium"`, `"high"`, `"xhigh"`, `"max"`), inherited by every call that passes no per-call `reasoning_effort`. |
| `thinking` | Constructor default, forwarded on every call that passes no per-call `reasoning_effort` (claim extraction included). A per-call `reasoning_effort` of `"none"` turns reasoning off; any graded level (`"low"` … `"max"`, e.g. from `tier_*_effort`) replaces the constructor default with adaptive thinking. If the model does not support thinking, Anthropic rejects the request with a 400. |
| `default_max_tokens` | Default `max_tokens` budget per call; `64000`. Enables honest token-utilisation logging. |

`AnthropicLLM` takes no constructor timeout. The timeout is per call (`complete(..., timeout=600)`, default `REASONING_TIMEOUT`) and every retry is additionally clamped to the run deadline.

### Extended thinking

`thinking` is forwarded verbatim to the Messages API. Supported shapes include:

- `{"type": "adaptive"}` — the API picks a thinking budget.
- `{"type": "enabled", "budget_tokens": 5000}` — explicit budget.

#### Haiku quirk

Claude Haiku 4.5 rejects `output_config.effort` with HTTP 400 ("Extra inputs are not permitted") — the very layout Inqtrix uses for every model, because a top-level `effort` field also returns 400. Inqtrix therefore drops `effort` for Haiku roles via `_EFFORT_UNSUPPORTED_FRAGMENTS` (`("haiku",)`) and keeps adaptive thinking without an effort cap. The suppression is collected once at construction time, logged as a warning per affected role, and drained once by the classify node through `consume_effort_config_warnings` — loud on purpose, so the fallback is never silent. The run still succeeds.

### Retry behaviour

- HTTP 500/502/503/504, 529 (overloaded), transient transport failures, and HTTP 429 all share one counter of at most 3 total attempts (`MAX_PROVIDER_ATTEMPTS`) with exponential backoff, jitter, and the `retry-after` header where the response supplies one.
- After the last attempt an HTTP 429 is translated to `AgentRateLimited`; it is never raised on the first 429.
- Other errors: raise `AnthropicAPIError` with status code and body.

Every retry consults the run deadline so retries do not push the run past `MAX_TOTAL_SECONDS`.

### Anthropic example stacks

The intro example under [Provider matrix](#provider-matrix) wires exactly this pair, with `model="sonar-pro"` and the `agent.research(...)` call on top; runnable in `examples/provider_stacks/anthropic_perplexity.py`. Server: `examples/webserver_stacks/anthropic_perplexity.py`.

## Azure OpenAI (LLM)

> File: `src/inqtrix/providers/azure.py`

### When to use Azure OpenAI

- Your organisation runs GPT-class models through an Azure OpenAI resource.
- You need enterprise auth (Service Principal, Managed Identity, custom token provider) rather than a static API key.
- You must stay within an Azure region for data-residency reasons.

For Azure **search** backends see [Azure Foundry Web Search](#azure-foundry-web-search).

### Four authentication modes

All four are mutually exclusive. Supplying more than one raises `ValueError` — and so does supplying none.

#### 1. API key

```python
AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    api_key="...",
    default_model="my-gpt4o-deployment",
)
```

Simplest path. Good for single-developer setups and quick experiments.

#### 2. Service Principal

```python
AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    tenant_id="...",
    client_id="...",
    client_secret="...",
    default_model="my-gpt4o-deployment",
)
```

Canonical for CI/CD and servers that cannot use Managed Identity. Internally builds a `ClientSecretCredential` and wraps it in a token provider that caches with ~60–75 minute token lifetime.

#### 3. Pre-built credential (Managed Identity, DefaultAzureCredential, ...)

```python
from azure.identity import DefaultAzureCredential

AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    credential=DefaultAzureCredential(),
    default_model="my-gpt4o-deployment",
)
```

Production-recommended when Inqtrix runs inside Azure (AKS, App Service, VMs with Managed Identity). Same token-lifetime caveat applies; see [Token lifetime](#token-lifetime).

#### 4. Pre-built token provider

```python
AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    azure_ad_token_provider=my_custom_token_provider,
    default_model="my-gpt4o-deployment",
)
```

Use when you already issue bearer tokens through a custom code path. The callable must return a non-empty string; Inqtrix calls it on every request, so implement your own cache.

### Azure OpenAI constructor

```python
AzureOpenAILLM(
    *,
    azure_endpoint: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    azure_ad_token_provider: Callable[[], str] | None = None,
    credential: Any | None = None,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_scope: str = "https://cognitiveservices.azure.com/.default",
    default_model: str = "gpt-4o",
    classify_model: str = "",
    claim_extract_model: str = "",
    evaluate_model: str = "",
    plan_model: str = "",
    answer_model: str = "",
    direct_chat_model: str = "",
    tier_high_model: str = "",
    tier_mid_model: str = "",
    tier_fast_model: str = "",
    tier_high_effort: str = "",
    tier_mid_effort: str = "",
    tier_fast_effort: str = "",
    default_max_tokens: int = 64000,
    context_window_tokens: int | None = None,
    temperature: float | None = None,
    token_budget_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens",
    proxy_url: str | None = None,
    timeout: float = 600,
    default_headers: Mapping[str, str] | None = None,
    request_params: Mapping[str, Any] | None = None,
    default_reasoning_effort: str | None = None,
    selectable_models: list[str] | None = None,
)
```

#### Key parameters

- `azure_endpoint` / `base_url` — exactly one of the two is required; passing neither or both raises `ValueError`. `azure_endpoint` is the resource URL in the form `https://<resource>.openai.azure.com/`; `base_url` is the full v1 base URL `https://<resource>.openai.azure.com/openai/v1/`. Either form is normalised to the `/openai/v1/` shape. An Azure AI Project endpoint (`.../api`) is rejected (different API surface).
- `default_model` — **deployment name**, not the underlying model name. Must match a deployment in the target resource. The placeholder default `"gpt-4o"` almost always needs to be overridden.
- `plan_model`, `answer_model`, `direct_chat_model` — further per-role overrides, same fallback rule as the other role models.
- `tier_high_model`, `tier_mid_model`, `tier_fast_model` — tier routing. Nodes map to a tier via `inqtrix.model_routing.NODE_TIER_ASSIGNMENT`; an empty tier and a per-role override both fall back to `default_model`.
- `tier_high_effort`, `tier_mid_effort`, `tier_fast_effort` — per-tier reasoning effort, resolved per call and forwarded by the adapter.
- `token_budget_parameter` — newer deployments expect `"max_completion_tokens"` (default). Switch to `"max_tokens"` only when a specific legacy deployment still requires it — using the wrong one yields a `400 unsupported parameter` from the API.
- `default_reasoning_effort` — see [Reasoning effort](#reasoning-effort) below.

### Reasoning effort

Azure GPT-5 series and o-series deployments accept a `reasoning_effort`
parameter that trades latency against reasoning depth.
`default_reasoning_effort` is merged into every request this client sends, so
it also applies to claim extraction; `claim_extract_model` only changes the
deployment, not the effort. Point it at a cheaper deployment when cost
matters, but note that a graded effort on a non-reasoning deployment is
rejected by Azure with HTTP 400 — the provider emits a construction-time
warning for known non-reasoning deployment names in `claim_extract_model`. A
per-call or per-tier effort overrides the constructor value, and `"none"`
removes `reasoning_effort` from the request entirely:

```python
AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    api_key="...",
    default_model="gpt-5",
    claim_extract_model="gpt-5-nano",
    default_reasoning_effort="low",        # every call on this client, claim extraction included
)
```

#### Allowed values

`"none"`, `"minimal"`, `"low"`, `"medium"`, `"high"`, `"xhigh"`. The
provider validates the value set; per-value-per-model compatibility is
handled by Azure (it returns HTTP 400 with a clear message when a
deployment rejects a value).

#### Per-model caveats

- `"none"` disables reasoning entirely. Only on `gpt-5.1+` deployments — other models return 400.
- `"minimal"` is "few reasoning tokens", **not** none. Only on the **original** GPT-5 (not `gpt-5.1+`, not `gpt-5-codex`).
- `"xhigh"` only on `gpt-5.1-codex-max`.
- `gpt-5-pro` accepts only `"high"`.
- `gpt-5.1+` defaults to `"none"`. After upgrading from `gpt-5` to `gpt-5.1`, set `default_reasoning_effort="medium"` (or higher) explicitly if you want reasoning to occur.
- `o-series` (`o1`, `o3`, `o4-mini`) accepts only `low`/`medium`/`high`.
- `o1-mini` does not support `reasoning_effort` at all.

The provider warns at construction time when the deployment name contains a known non-reasoning fragment (`gpt-4o`, `gpt-3.5`, `embedding`, `whisper`, `tts`, `dall-e`, `o1-mini`, ...). Unfamiliar names pass through silently — Azure is the authoritative source.

#### Mutex with `temperature`

Reasoning models reject `temperature`. Setting `temperature` together with `default_reasoning_effort` raises `ValueError` in the constructor — analog to Anthropic's `temperature`/`thinking` mutex. The per-tier `tier_high_effort` / `tier_mid_effort` / `tier_fast_effort` arguments are **not** covered by that check: combined with `temperature` they construct without complaint and fail later as an HTTP 400 from Azure.

### Errors

- `AzureOpenAIAPIError(status_code=404)` — deployment name does not exist in the resource.
- `AzureOpenAIAPIError(status_code=400)` — `token_budget_parameter` mismatch, prompt/model constraint violation, deployment does not support the requested `reasoning_effort` value (e.g. `"xhigh"` on a non-codex-max deployment), or `temperature` set on a reasoning deployment.
- `ValueError` (constructor) — invalid `default_reasoning_effort` value, or `temperature` together with `default_reasoning_effort`.
- `AgentRateLimited` — HTTP 429 or token-per-minute quota exceeded.

SDK retries are disabled for LLM calls. The provider retries transient 408/409/5xx and SDK timeout/connection errors itself so attempts are visible in logs and live progress.

## Amazon Bedrock

> File: `src/inqtrix/providers/bedrock.py`

### When to use Amazon Bedrock

- You already have Bedrock access and prefer IAM / AWS SSO over a dedicated Anthropic key.
- You need EU cross-region inference (for example `eu.anthropic.claude-sonnet-4-6`).
- You want `AgentRateLimited` on `ThrottlingException` instead of silent retries.

### Amazon Bedrock constructor

```python
from inqtrix import BedrockLLM


llm = BedrockLLM(
    default_model="eu.anthropic.claude-sonnet-4-6",
    claim_extract_model="eu.anthropic.claude-haiku-4-5",  # usually the biggest cost lever
    profile_name="my-profile",   # optional AWS profile name
    region_name="eu-central-1",  # optional; this is also the hard default and overrides the profile/AWS_DEFAULT_REGION
)
```

| Parameter | Purpose |
|-----------|---------|
| `default_model` | Bedrock model id (including EU cross-region prefix where applicable). Used for classify fallback, plan, answer, evaluate fallback. |
| `classify_model`, `claim_extract_model`, `evaluate_model` | Optional per-role overrides. |
| `plan_model`, `answer_model`, `direct_chat_model` | Further per-role overrides, same fallback rule as the other role models. |
| `tier_high_model`, `tier_mid_model`, `tier_fast_model` | Tier routing. Nodes map to a tier via `inqtrix.model_routing.NODE_TIER_ASSIGNMENT`; an empty tier and a per-role override both fall back to `default_model`. |
| `tier_high_effort`, `tier_mid_effort`, `tier_fast_effort` | Per-tier reasoning effort, resolved per call and forwarded by the adapter. |
| `effort` | Constructor-level reasoning effort (`"low"`, `"medium"`, `"high"`, `"xhigh"`, `"max"`), inherited by every call that passes no per-call `reasoning_effort`. |
| `profile_name` | AWS profile name; passed to `boto3.Session(profile_name=...)`. Use IAM role or SSO credentials. |
| `region_name` | AWS region for Bedrock. |
| `default_max_tokens` | Default Converse `maxTokens` budget per call; `64000`. Enables honest token-utilisation logging. |

### Authentication

Authentication is handled entirely by `boto3`'s credential chain:

1. Explicit `profile_name` argument (passed to `boto3.Session`). A pre-built `boto3.Session` cannot be injected; the provider always constructs its own.
2. Environment variables (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, ...).
3. IAM role attached to the EC2/ECS/EKS instance.
4. SSO cached credentials.

The provider itself reads no AWS environment variables; it always defers to `boto3`.

### Retry and rate-limit behaviour

- `ThrottlingException`: up to 3 total attempts (`_MAX_BEDROCK_ATTEMPTS`) inside one logical-operation deadline, with exponential backoff + jitter. After the last attempt, the error is translated to `AgentRateLimited` so upstream handlers treat it the same as an Anthropic 429.
- Other Bedrock `ClientError` responses: raised as `BedrockAPIError` with the mapped HTTP-ish status code.
- Retries respect the run deadline so retries cannot push past `MAX_TOTAL_SECONDS`.

### Amazon Bedrock example stacks

Library: the intro example under [Provider matrix](#provider-matrix) with the constructor above in place of `AnthropicLLM`; runnable in `examples/provider_stacks/bedrock_perplexity.py`. Server: `examples/webserver_stacks/bedrock_perplexity.py`.

## Perplexity

> File: `src/inqtrix/providers/perplexity.py`

### When to use Perplexity

- You want high-quality citation-rich search with minimal configuration.
- You want Perplexity's native Agent API instead of an OpenAI-compatible proxy.
- You want Inqtrix's search cache (TTL-based LRU) without building your own.

### Perplexity constructor

```python
from inqtrix import PerplexitySearch


search = PerplexitySearch(
    api_key="pplx-...",
    preset="fast-search",                        # default; provides cited web-search answers
    model=None,                                  # optional explicit Agent model override
    cache_maxsize=256,
    cache_ttl=3600,
)
```

| Parameter | Purpose |
|-----------|---------|
| `api_key` | Perplexity API key. Server-mode auto-creation reads `PERPLEXITY_API_KEY`. |
| `preset` | Agent preset. Default `fast-search` is preferred because it returns cited web-search output out of the box. |
| `model` | Optional explicit Agent model. When set, it overrides `preset`. |
| `instructions` | Optional Agent instructions appended to the request. |
| `base_url` | Optional Perplexity-compatible endpoint override. Leave empty for the SDK default. |
| `cache_maxsize` / `cache_ttl` | TTL-based LRU cache parameters. Key = SHA-256 of query + supported hints. |

### Perplexity `search_model` property

Returns the explicit model when configured, otherwise the bare preset label — with the default configuration simply `fast-search`. Unlike the Foundry property there is no prefix.

### Request shape sent to the API

```python
client.responses.create(
    input=query,
    tools=[{"type": "web_search"}],
    stream=False,
    preset="fast-search",  # or model=...
    instructions=...,
    timeout=...,
)
```

Recency, language, and domain hints are folded into the user input when the
Agent API has no native parameter for that hint.

The shared hint builder used by Perplexity and Azure Foundry writes its
instructions in English. It retains the requested time window and names the
first `language_filter` entry as the reply language and source preference.
Domain inclusions and exclusions remain `site:` and `-site:` query operators.
These are best-effort provider instructions, not verified properties of every
returned source.

### Response shape expected from the API

The adapter reads:

- **Answer text** — from `output_text` or message content.
- **Sources** — `output[].type == "search_results"` with `results[]` fields
  `id`, `url`, `title`, `snippet`, `date`, and `last_updated`.
- **Tokens** — `usage.input_tokens` / `usage.output_tokens`.

Minimal response example:

```json
{
  "output_text": "NVIDIA reported ... [2]",
  "output": [
    {
      "type": "search_results",
      "results": [
        {"id": 2, "url": "https://example.com/report", "title": "...", "snippet": "..."}
      ]
    }
  ],
  "usage": {"input_tokens": 300, "output_tokens": 120}
}
```

### Errors and retries

- Hidden Perplexity SDK retries are disabled (`max_retries=0`). The adapter runs Inqtrix' own visible retry loop — at most 3 total attempts inside one logical-operation deadline — so every attempt is logged and surfaced in live progress.
- HTTP 429 / quota: raise `AgentRateLimited`.
- Other API errors degrade to an empty `GroundedSearchResult` with a visible
  non-fatal notice so the search node can continue and report partial failure.

## Azure Foundry Web Search

> File: `src/inqtrix/providers/azure_web_search.py`

### When to use Azure Foundry Web Search

- Your organisation mandates Foundry-managed web search instead of a direct search API.
- You want all agent calls to flow through Foundry for audit and cost attribution.
- You already have a Foundry project with the Web Search tool configured.

### Azure Foundry Web Search constructor

```python
from inqtrix import AzureFoundryWebSearch


search = AzureFoundryWebSearch(
    project_endpoint="https://my-project.services.ai.azure.com/api/projects/my-project",
    tenant_id="...",
    client_id="...",
    client_secret="...",
    agent_name="web-search-agent",
    # agent_version="2",                     # optional version label
)
```

| Parameter | Purpose |
|-----------|---------|
| `project_endpoint` | Foundry project endpoint (`.services.ai.azure.com/api/projects/...`). |
| `agent_name` | Pre-created agent name (required). Env alias in server mode: `WEB_SEARCH_AGENT_NAME`. The adapter references it per call via `agent_reference`. |
| `agent_version` | Optional version label, default `""`. Env alias in server mode: `WEB_SEARCH_AGENT_VERSION`. |
| `api_key` / `credential` / `tenant_id` + `client_id` + `client_secret` | One of three auth modes, in this precedence order: static project key (`api-key` header), pre-built Azure token credential, Service Principal trio. Unlike `AzureOpenAILLM` there is no `azure_ad_token_provider` (see [Azure OpenAI](#azure-openai-llm)). |

### Azure Foundry Web Search `search_model` property

Returns `f"foundry-web:{name}@{version_or_latest}"` so operators see the agent identity and version in `/health` and `/v1/stacks`.

### Token lifetime

Cached bearer tokens are valid for ~60–75 minutes. Long-running servers occasionally see transient 401s when a near-expired token is handed out for a long request.

### Response shape

The adapter collects Responses-API tool output and normalises it into a `GroundedSearchResult` (a dataclass, not a dict):

- `answer` — assembled from the agent's final message content.
- `sources` — one `GroundedSource` per cited URL, de-duplicated, with `url`, `title`, `rank`, `origin` (`url_citation`, `markdown_link`, or `answer_url_fallback`), and the annotation offsets. URL-only: Foundry returns no snippets.
- `prompt_tokens` / `completion_tokens` — from the Responses `usage` block (`input_tokens` / `output_tokens`).
- `related_questions` — exists on the dataclass but is never populated by this adapter.

### Errors and degradation

- `AzureFoundryWebSearchAPIError` exists and is exported, but this adapter never raises it. HTTP 429 becomes `AgentRateLimited`; every other `APIStatusError` is turned into a non-fatal notice with the mapped status code (`provider_timeout` on 408, `upstream_5xx` on 5xx, otherwise `provider_error`) and an empty result.
- Any other exception is caught by a final `except Exception` block, recorded as a non-fatal notice, and the adapter returns a fresh empty `GroundedSearchResult()`. The node keeps running with no citations; the iteration log records the fallback. `AgentCancelled` and `AgentTimeout` are re-raised instead — a cancelled or timed-out run must never degrade into a fake empty result.

## Related docs

- [Agent config](agent-config.md)
- [Settings and env](settings-and-env.md)
- [Architecture overview](../architecture/overview.md)
