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

## Extra headers for an API gateway

Some installations reach a provider through an API gateway that expects headers of its own, for example a subscription key or a routing header. Every LLM and search adapter except `BedrockLLM` takes `default_headers`, and so do `LiteLLMEmbeddings`, `AzureOpenAIEmbeddings` and `CohereRerank`: a mapping of header names to text values that goes with every request of that provider:

```python
llm = LiteLLM(
    api_key="...",
    base_url="https://gateway.example.com/v1",
    default_headers={"Ocp-Apim-Subscription-Key": "..."},
)
```

In server mode one setting per kind of call carries them, so headers meant for a gateway never reach a provider outside it:

| Setting | Goes to |
|---------|---------|
| `INQTRIX_LLM_EXTRA_HEADERS` | the provider selected by `INQTRIX_LLM_PROVIDER` |
| `INQTRIX_SEARCH_EXTRA_HEADERS` | the provider selected by `INQTRIX_SEARCH_PROVIDER` |
| `INQTRIX_EMBEDDING_EXTRA_HEADERS` | the embedding provider (`INQTRIX_EMBEDDING_PROVIDER`), also when it reuses the LiteLLM gateway |
| `INQTRIX_RERANKER_EXTRA_HEADERS` | the `cohere` reranker; the `llm` reranker calls the LLM provider and ignores it with a WARNING, and so does a start without a reranker (`none`) |

Each holds a JSON object of header names to text values. In a dotenv file single quotes keep the JSON literal:

```dotenv
INQTRIX_LLM_EXTRA_HEADERS='{"Ocp-Apim-Subscription-Key": "...", "X-Tenant": "research"}'
```

In a Kubernetes Secret the value is the JSON itself, without the quotes. The values may be secrets, so these settings belong in the secrets file. At startup the log names the headers and never their values. The start stops with an error that names the setting, again without a value, when the text is not a JSON object, a value is not text of visible ASCII characters, spaces or tabs, a value starts or ends with a space or tab, a name is not a valid HTTP header name, a name appears twice (also when two names differ only in case), or a name is one the provider sets itself: `Authorization`, `Proxy-Authorization`, `api-key` and `x-api-key` come from the provider's key setting, and `Host`, `Connection`, `Content-Length`, `Content-Type` and `Transfer-Encoding` from the HTTP client. With `INQTRIX_LLM_PROVIDER=bedrock` a set `INQTRIX_LLM_EXTRA_HEADERS` stops the start too, because boto3 signs the Bedrock calls and sends no extra headers. With the knowledge engine off, a set `INQTRIX_EMBEDDING_EXTRA_HEADERS` or `INQTRIX_RERANKER_EXTRA_HEADERS` is ignored with a WARNING. The settings of the four kinds of call are secrets for `inqtrix-deploy`: in `deploy/.env.stack` it refuses them like any other secret. The headers are fixed for the process; nothing varies them per user or per request.

## Reasoning that never arrives

A provider or gateway may accept a reasoning setting and still not reason, for every LLM provider alike. When a model whose card lists the requested effort answers three times in a row at `"medium"` or higher with zero reasoning tokens, Inqtrix logs one WARNING per provider, model and effort and marks those generations WARNING in Langfuse. An answer that reports no reasoning figure at all is not judged. Claude reports no reasoning token count, so its answers are not judged.

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
| `default_headers` | Optional headers sent with every request, for example an API gateway's subscription header; server mode: `INQTRIX_LLM_EXTRA_HEADERS`. See [Extra headers](#extra-headers-for-an-api-gateway). |

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

- Calls go through the async OpenAI client on the process loop, one client per adapter and event loop, so connections are reused; the sync methods wait for that call, the async twins (`acomplete`, `achat`, ...) await it.
- Hidden OpenAI SDK retries are disabled. `LiteLLM` retries rate limits (429), transient 408/409/500/502/503/504/529 and SDK timeout/connection errors itself, three attempts in total within the call's deadline, waiting as long as a `Retry-After` asks when that wait ends before the deadline, so attempts are logged and surfaced in live progress. A rate limit that outlasts the attempts escalates to `AgentRateLimited`.
- Any other 4xx is never retried. One whose structured error code is `content_filter` (the Azure/OpenAI content filter refused the request) is classified as a content refusal of that request (`provider_failure_contract.CONTENT_REFUSAL_CODES`): the whole-document analysis then leaves only the refused window unanalysed and names it, instead of stopping the reading, and knowledge contextualization indexes the refused batch without retrieval context instead of pausing the revision.
- Anthropic's `thinking` block is not part of this adapter. Reasoning effort is: `tier_high_effort` / `tier_mid_effort` / `tier_fast_effort` are resolved per call and forwarded as the OpenAI-compatible `reasoning_effort` request field. A gateway that cannot honour a graded value rejects the request visibly; one that accepts it and does not reason is reported as described in [Reasoning that never arrives](#reasoning-that-never-arrives). For an explicit thinking budget use `AnthropicLLM` or `BedrockLLM`.
- Models with OpenAI rules on their card follow the same rules as on Azure: `none` goes out explicitly where the card lists it, a level the model's API cannot send runs at the strongest allowed level below it, and a model whose card says `api="responses"` is called through the gateway's Responses endpoint (`/responses`), see [Models on the Responses API](#models-on-the-responses-api). The gateway must offer that endpoint for those models.
- Token-usage metadata is only as good as the upstream proxy: a proxy that strips `usage` from the response will report zeros.
- An alias with a provider prefix (`azure/gpt-6.1-terra`, `anthropic/claude-opus-4-8`) finds the card of its model. For a model on the Responses API the adapter then calls the gateway's `/responses` endpoint, which the gateway must route. A Claude model behind the gateway shows its card's levels in the picker, `xhigh` and `max` included; the adapter passes the level on as `reasoning_effort`, and whether the gateway maps it for Anthropic is up to the gateway.

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
| `default_headers` | Optional headers sent with every request; they win over the adapter's own headers of the same name except `x-api-key` and `Content-Type`, and an `anthropic-beta` joins the beta a request sends itself (comma-separated) instead of replacing it. Server mode: `INQTRIX_LLM_EXTRA_HEADERS`. See [Extra headers](#extra-headers-for-an-api-gateway). |

`AnthropicLLM` takes no constructor timeout. The timeout is per call (`complete(..., timeout=600)`, default `REASONING_TIMEOUT`) and every retry is additionally clamped to the run deadline.

### Extended thinking

`thinking` is forwarded verbatim to the Messages API. Supported shapes include:

- `{"type": "adaptive"}` — the API picks a thinking budget.
- `{"type": "enabled", "budget_tokens": 5000}` — explicit budget.

#### What each Claude model accepts

`AnthropicLLM` and `BedrockLLM` read the same per-model rules from the model card of the model a call uses (`inqtrix.model_cards`, field `claude`). The rules follow the Claude API documentation:

| Model | `reasoning_effort="none"` | Graded effort | `tool_choice` forcing a tool | `temperature`, `top_p`, `top_k` |
|---|---|---|---|---|
| Claude Opus 5.5, Sonnet 5.5, Fable 5.1, Fable 5 | thinking cannot be switched off: adaptive thinking at effort `low`, one WARNING per model | adaptive thinking with that effort | rejected on Opus 5.5, Sonnet 5.5 and Fable 5.1 | rejected |
| Claude Opus 5, Sonnet 5, Haiku 5.5 | `thinking: {"type": "disabled"}`, because these models think unless told otherwise | adaptive thinking with that effort | accepted | rejected |
| Claude Opus 4.8, Opus 4.7 | `thinking` left out | adaptive thinking with that effort | accepted | rejected |
| Claude Opus 4.6, Sonnet 4.6 | `thinking` left out | adaptive thinking with that effort | accepted | accepted |
| Claude Haiku 4.5 | `thinking` left out | no thinking and no effort, one WARNING per model: the model takes only a fixed `budget_tokens` budget, and `output_config.effort` returns a 400 | accepted | accepted |

A `temperature` that a configured model rejects stops the provider at start with a message naming the models, instead of failing every call with a 400. A model without a card keeps the earlier rules: `"none"` leaves `thinking` out, and a model name containing `haiku` gets adaptive thinking without an effort cap. Effort warnings for configured roles are collected at construction time and drained once by the classify node through `consume_effort_config_warnings`, so they reach both the log and the progress feed.

### Tool calling and the agent kernel

`AnthropicLLM` and `BedrockLLM` serve the agent kernel natively. `AnthropicLLM.supports_tool_calls()` is `True`. `BedrockLLM` answers `True` for Claude models (a model id that contains `claude`) and `False` for other model families and for application inference profile ARNs, which do not name their model; there the kernel reports itself unavailable instead of failing mid-run.

A kernel turn becomes one request. On the Messages API a leading system message becomes `system`, tool calls become `tool_use` blocks, and every tool result of a turn goes back as a `tool_result` block in one user message. On Bedrock Converse the same turn uses `system` text blocks, `toolUse` blocks, `toolResult` blocks in one user message and `toolConfig` for the tools; a tool without a description goes without one, because Converse takes no empty description. Claude's thinking blocks (`reasoningContent` on Converse) travel with the assistant turn that produced them (`ChatTurn.provider_state`) and go back unchanged and in their original order, and only to the adapter that produced them (a state another provider wrote is not sent: one WARNING per pair of providers and process, every further drop counted at DEBUG; the OpenAI-compatible adapters, which write no state, drop any state the same way); transcripts, archives, summaries and spans never hold them, and spans show them only as type and length. Redacted reasoning bytes from Converse are kept as base64 text, so a checkpoint holds plain JSON.

- `tool_choice="required"` becomes `{"type": "any"}` (Converse: `{"any": {}}`) where the model allows a forced tool with the request's thinking. Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 reject forcing, and manual thinking (`{"type": "enabled", "budget_tokens": N}`) rejects it on every model; there the request sends `auto` plus an instruction at the end of the system prompt to call one of the tools, and a text answer raises `AgentRequiredToolCallMissing`, which the kernel corrects once more within its attempt budget. The instruction sits in the system prompt rather than in the last user turn: every turn under the same obligation carries it there, so the conversation reaches the API append-only and no thinking block loses its prefix.
- `tool_choice="none"` becomes `{"type": "none"}` on the Messages API. Converse has no `none`: such a turn gets `auto` with one WARNING per model, and the kernel's admission still refuses a tool call the step does not allow.
- On models that bind thinking blocks to an unchanged prefix (Claude 5.1 and later), a request that returns thinking blocks sends the `thinking-binding-controls-2026-08-01` beta and `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`, because the kernel's own compaction changes earlier turns. The Messages API takes the beta as the `anthropic-beta` header; Converse takes both through `additionalModelRequestFields` (`anthropic_beta`), as the Bedrock user guide on preserved thinking describes. With the beta, every Messages API answer carries `input_transformations`, empty when nothing was dropped. When the field is missing, a proxy between Inqtrix and the API probably stripped the header: Inqtrix logs one WARNING per model, because dropped thinking blocks then stay invisible. Dropped blocks are logged as counts per reason. Converse does not document where it would return the field, so Bedrock answers are not checked.
- A turn that stops at its output limit (`max_tokens`, `model_context_window_exceeded`) while it writes tool calls runs none of them, because their arguments may be cut off: the kernel shows the batch back to the model as rejected arguments within its attempt budget. A text answer cut at the limit stays a turn with `finish_reason="length"`.
- A declined request (`stop_reason: "refusal"`) is a visible error naming the category. On Converse, a guardrail (`guardrail_intervened`), a content filter (`content_filtered`) and an answer or tool call Bedrock could not read (`malformed_model_output`, `malformed_tool_use`) are visible errors naming the stop reason. A conversation that would end with an assistant turn (a prefill) is refused before the call.
- A tool call whose input is no JSON object runs no call of the turn: the kernel shows the batch back to the model as rejected arguments, as for invalid JSON on the OpenAI-compatible adapters.
- Tool schemas go out as they are, without strict schema enforcement; the kernel validates the arguments of every call and corrects invalid ones within its attempt budget.

These shapes follow the Claude API documentation, the Bedrock user guide and the botocore service model of Converse; the Bedrock tests send every request and answer through botocore's own validation. Neither adapter has been checked against the live API yet.

### Retry behaviour

- HTTP 408/409/500/502/503/504, 529 (overloaded), transient transport failures, and HTTP 429 all share one counter of at most 3 total attempts (`MAX_PROVIDER_ATTEMPTS`) with exponential backoff and jitter; any of them honours a `retry-after` header, and a wait that would end at or past the call's deadline is not taken: the call ends with that failure. This is the retry loop every built-in adapter shares, and each request is one attempt of it.
- After the last attempt an HTTP 429 is translated to `AgentRateLimited`; it is never raised on the first 429.
- Other errors: raise `AnthropicAPIError` with status code, error type, message and request-id.

Every retry consults the run deadline so retries do not push the run past `MAX_TOTAL_SECONDS`.

### Transport

Calls go through one `httpx.AsyncClient` per adapter and event loop on the process loop; the sync methods (`complete`, `chat`, ...) wait for that call, the async twins (`acomplete`, `achat`, ...) await it. TLS trusts the certificates `httpx` uses, the `certifi` bundle or the file named by `SSL_CERT_FILE`, not the operating system store that the earlier `urllib` transport used; a private certificate authority therefore goes into `SSL_CERT_FILE`.

### Anthropic example stacks

The intro example under [Provider matrix](#provider-matrix) wires exactly this pair, with `model="sonar-pro"` and the `agent.research(...)` call on top; runnable in `examples/provider_stacks/anthropic_perplexity.py`. Server: `examples/webserver_stacks/anthropic_perplexity.py`.

## Azure OpenAI (LLM)

> File: `src/inqtrix/providers/azure.py`

### When to use Azure OpenAI

- Your organisation runs GPT-class models through an Azure OpenAI resource.
- You must stay within an Azure region for data-residency reasons.

For Azure **search** backends see [Azure Foundry Web Search](#azure-foundry-web-search).

### Authentication

Azure OpenAI is reached with the resource's API key. `api_key` is required; an empty or blank key raises `ValueError`.

```python
AzureOpenAILLM(
    azure_endpoint="https://my-resource.openai.azure.com/",
    api_key="...",
    default_model="my-gpt4o-deployment",
)
```

In server mode the key comes from `AZURE_OPENAI_API_KEY`. Entra ID sign-in (service principal, managed identity, token provider) is not supported: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` have no effect, and a start with one of them set logs a WARNING that names the variable.

### Azure OpenAI constructor

```python
AzureOpenAILLM(
    *,
    azure_endpoint: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
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
- `default_model` — **deployment name**, not the underlying model name. Must match a deployment in the target resource. The placeholder default `"gpt-4o"` almost always needs to be overridden. Name each deployment after its model id (`gpt-6.1-terra`, `gpt-5.4`): the model card is found by that name, and with it the context window, the prices and the request rules, including the Responses API for GPT-6 and GPT-5.6. A configured model without a card gets one `CONFIG` warning at start and runs over Chat Completions.
- `plan_model`, `answer_model`, `direct_chat_model` — further per-role overrides, same fallback rule as the other role models.
- `tier_high_model`, `tier_mid_model`, `tier_fast_model` — tier routing. Nodes map to a tier via `inqtrix.model_routing.NODE_TIER_ASSIGNMENT`; an empty tier and a per-role override both fall back to `default_model`.
- `tier_high_effort`, `tier_mid_effort`, `tier_fast_effort` — per-tier reasoning effort, resolved per call and forwarded by the adapter.
- `token_budget_parameter` — newer deployments expect `"max_completion_tokens"` (default). Switch to `"max_tokens"` only when a specific legacy deployment still requires it — using the wrong one yields a `400 unsupported parameter` from the API.
- `default_reasoning_effort` — see [Reasoning effort](#reasoning-effort) below.
- `default_headers` — optional headers sent with every request, for example an API gateway's subscription header; server mode: `INQTRIX_LLM_EXTRA_HEADERS`. See [Extra headers](#extra-headers-for-an-api-gateway).

### Reasoning effort

Azure GPT-5 series and o-series deployments accept a `reasoning_effort`
parameter that trades latency against reasoning depth.
`default_reasoning_effort` is merged into every request this client sends, so
it also applies to claim extraction; `claim_extract_model` only changes the
deployment, not the effort. Point it at a cheaper deployment when cost
matters, but note that a graded effort on a non-reasoning deployment is
rejected by Azure with HTTP 400 — the provider emits a construction-time
warning for known non-reasoning deployment names in `claim_extract_model`. A
per-call or per-tier effort overrides the constructor value. `"none"` sends
`reasoning_effort: "none"` when the model card lists `none` (GPT-6 and GPT-5.6
think at `medium` when the field is missing). A model whose card has reasoning
levels but no `none` (GPT-6 Astra, GPT-6.1 Sol, the Pro models, GPT-5 mini and
nano) cannot be switched off: `"none"` runs it at its weakest level, with one
`CONFIG` warning, because a missing field would run it at its default. Every
other deployment gets no `reasoning_effort` for `"none"`:

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
provider validates the value set. The constructor's `default_reasoning_effort`
goes out as configured; the card rules below apply to per-call and per-tier
efforts. For a deployment with an OpenAI model card
it also knows which levels the model takes over Chat Completions: the card's
levels without `max`, which Chat Completions refuses ("Supported values are:
'none', 'low', 'medium', 'high', and 'xhigh'"). A level outside that set, from
a per-run override or a skill, runs at the strongest allowed level below it
(`max` at `xhigh`), with one `CONFIG` warning per model, and the model picker
does not offer it; a level below the weakest one runs at `none` where the card
knows it and at the weakest level where it does not. The trace of the call
shows both: `inqtrix.request.reasoning_effort` for the level asked for and
`inqtrix.request.reasoning_effort.sent` for the level the request carried. For
deployments without a card, per-value-per-model compatibility is handled by
Azure (it returns HTTP 400 with a clear message when a deployment rejects a
value).

#### Per-model caveats

- `"none"` disables reasoning entirely where the card lists it (`gpt-5.1+`, GPT-5.6 and most GPT-6 models); a model with a card that cannot switch reasoning off runs at its weakest level instead, and a deployment without a card gets no field.
- `"minimal"` is "few reasoning tokens", **not** none. Only on the **original** GPT-5 (not `gpt-5.1+`, not `gpt-5-codex`).
- `"xhigh"` only on `gpt-5.1-codex-max`.
- `gpt-5-pro` accepts only `"high"`.
- `gpt-5.1+` defaults to `"none"`. After upgrading from `gpt-5` to `gpt-5.1`, set `default_reasoning_effort="medium"` (or higher) explicitly if you want reasoning to occur.
- `o-series` (`o1`, `o3`, `o4-mini`) accepts only `low`/`medium`/`high`.
- `o1-mini` does not support `reasoning_effort` at all.

The provider warns at construction time when the deployment name contains a known non-reasoning fragment (`gpt-4o`, `gpt-3.5`, `embedding`, `whisper`, `tts`, `dall-e`, `o1-mini`, ...). Unfamiliar names pass through silently — Azure is the authoritative source.

A provider or gateway may accept the setting and still not reason; Inqtrix says so, see [Reasoning that never arrives](#reasoning-that-never-arrives).

#### Mutex with `temperature`

Reasoning models reject `temperature` while they reason. Setting `temperature` together with `default_reasoning_effort` raises `ValueError` in the constructor — analog to Anthropic's `temperature`/`thinking` mutex. For a per-call or per-tier effort (`tier_high_effort` / `tier_mid_effort` / `tier_fast_effort`) the adapter drops `temperature` from every request that carries a graded level, including the weakest level that stands in for "off" on a model that cannot switch reasoning off. `none` sent explicitly turns reasoning off, and `temperature` goes out. An empty effort sends no `reasoning_effort` and keeps `temperature`: a model that reasons by default (GPT-6, GPT-5.6) can then reject the request with HTTP 400, so set an effort for such models or leave `temperature` unset.

### Models on the Responses API

Chat Completions is the adapter's path for every model. Some newer models refuse
function tools while they reason over it: Azure answers "Function tools with
reasoning_effort are not supported … in /v1/chat/completions. To use function
tools, use /v1/responses or set reasoning_effort to 'none'" for GPT-6 and GPT-5.6,
and the Pro models offer no Chat Completions at all. Their model cards say
`openai=OpenAIRules(api="responses")`, and the adapter sends every call of such a
model through the Responses API instead: the agent's tool calls keep their
reasoning, and `max` is available. The rest of Inqtrix sees the same answers as
from Chat Completions.

Chat Completions stays a full path for the whole agent: every model whose card
does not say otherwise, such as `gpt-5.4` and `gpt-5.4-mini`, runs tool calls,
structured answers and reasoning efforts there as before. Only two things are
Responses-only: `max`, and reasoning handed from one turn to the next; over Chat
Completions a model reasons afresh in each turn.

- Requests go out with `store=false`: Azure keeps no response.
- The answer's output items travel with the assistant turn as its provider
  state, in the order the model wrote them: its reasoning (encrypted), its
  messages with their `phase` (`commentary` for text before tool calls,
  `final_answer` for the answer) and its tool calls. The next turn of the same
  model gets them back unchanged while the turn still has the text and calls of
  that answer; when the agent changed them (a renamed call, edited text), the
  reasoning goes first and the text and calls follow as they stand now, with the
  `phase` the model would have set. The reasoning never reaches a log line, an
  event or a transcript, a span shows only its type and length, and another
  model or provider never receives it.
- Which earlier reasoning the model reads again is the model's choice
  (`reasoning.context`): GPT-6 and GPT-5.6 read the reasoning of every earlier
  turn they get back, also across the notes the agent adds as user messages
  during a run (skills, hints, corrections). That keeps their line of thought
  and costs input tokens; Inqtrix leaves the model's default as it is.
- Reasoning goes back only to the backend that wrote it. A LiteLLM alias that
  spreads one Responses model over several Azure resources may hand a turn's
  reasoning to another resource, which can refuse it; keep such an alias on one
  resource.
- Parameters both APIs know pass through; `verbosity` becomes `text.verbosity`.
  An `include` or `reasoning` in `request_params` (for example
  `{"summary": "auto"}`) is merged with what this path needs, the encrypted
  reasoning and the call's effort; `store` is always `false` here, and
  `store=true` gets one `CONFIG` warning per model. A Chat Completions-only
  parameter in `request_params` (`seed`, `stop`, `n`, `logit_bias`, …) fails
  with a `ValueError` naming it and the model before any request goes out.
- A Responses answer that reports `failed` raises the same error as the
  matching HTTP failure (rate limit, server error, bad request), so it is
  retried and mapped the same way. Failure log lines name the API
  (`api=responses` or `api=chat_completions`).
- An Azure deployment must be named after the model id (as for the model
  picker) for its card, and with it the Responses path, to apply. A deployment
  without a card stays on Chat Completions.

### Errors

- `AzureOpenAIAPIError(status_code=404)` — deployment name does not exist in the resource.
- `AzureOpenAIAPIError(status_code=400)` — `token_budget_parameter` mismatch, prompt/model constraint violation, deployment does not support the requested `reasoning_effort` value (e.g. `"xhigh"` on a non-codex-max deployment), or `temperature` set on a reasoning deployment.
- `ValueError` (constructor) — invalid `default_reasoning_effort` value, or `temperature` together with `default_reasoning_effort`.
- `AgentRateLimited` — HTTP 429 or token-per-minute quota exceeded.

SDK retries are disabled for LLM calls. The provider retries rate limits (429), transient 408/409/500/502/503/504/529 and SDK timeout/connection errors itself, three attempts in total within the call's deadline, waiting as long as a `Retry-After` asks when that wait ends before the deadline, so attempts are visible in logs and live progress.

Calls go through the async OpenAI client on the process loop, one client per adapter and event loop, so connections are reused; the sync methods (`complete`, `chat`, ...) wait for that call, the async twins (`acomplete`, `achat`, ...) await it.

## Amazon Bedrock

> File: `src/inqtrix/providers/bedrock.py`

### When to use Amazon Bedrock

- You already have Bedrock access and prefer IAM / AWS SSO over a dedicated Anthropic key.
- You need EU cross-region inference (for example `eu.anthropic.claude-sonnet-4-6`).
- You want `AgentRateLimited` on `ThrottlingException` instead of silent retries.

The adapter maps Claude's request rules: reasoning, tool use and structured
output. Converse also serves other model families, but on them the agent kernel
reports itself unavailable, and the catalogue ships no cards for them (see
[Model cards](model-cards.md)). Models that Bedrock serves only on its
OpenAI-compatible endpoint are not reachable through this adapter.

### Amazon Bedrock constructor

```python
from inqtrix import BedrockLLM


llm = BedrockLLM(
    default_model="eu.anthropic.claude-sonnet-4-6",
    claim_extract_model="eu.anthropic.claude-haiku-4-5",  # usually the biggest cost lever
    profile_name="my-profile",   # optional AWS profile name; or api_key="..." for a Bedrock API key
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
| `api_key` | Optional Bedrock API key; every call of this provider carries it as a bearer token. Mutually exclusive with `profile_name`. |
| `profile_name` | AWS profile name; passed to `boto3.Session(profile_name=...)`. Use IAM role or SSO credentials. |
| `region_name` | AWS region for Bedrock. |
| `default_max_tokens` | Default Converse `maxTokens` budget per call; `64000`. Enables honest token-utilisation logging. |

### Authentication

Bedrock calls authenticate with a Bedrock API key or with AWS credentials.

**Bedrock API key.** With `api_key=`, every call of this provider carries the key as a bearer token. The provider's own botocore session holds the key, so the process environment stays untouched and other AWS clients in the process keep their credentials. The key replaces the AWS credentials: `api_key` and `profile_name` are mutually exclusive. The server reads the key from `AWS_BEARER_TOKEN_BEDROCK` like every setting, so the dotenv file works as well as the environment; when `AWS_PROFILE` is set too, it is not used, and a WARNING says so. Long-term keys (valid from one day up to unlimited) suit a service; short-term keys expire after at most 12 hours. Without `api_key`, `boto3` itself signs with `AWS_BEARER_TOKEN_BEDROCK` when that variable is set in the process environment; `boto3` reads it from nowhere else. A blank key counts as none, as an argument and in the variable. `boto3` would send a blank variable as an empty bearer token, which Bedrock refuses, so the provider signs with the AWS credential chain instead and logs a WARNING that names the variable. Inqtrix requires `boto3` 1.42.85 or later, the release its API-key tests run against.

**AWS credentials.** Without a key, `boto3`'s credential chain decides:

1. Explicit `profile_name` argument (passed to `boto3.Session`). A pre-built `boto3.Session` cannot be injected; the provider always constructs its own.
2. Environment variables (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, ...).
3. IAM role attached to the EC2/ECS/EKS instance.
4. SSO cached credentials.

At construction the provider logs one line naming how its calls authenticate (the configured API key, the key from `AWS_BEARER_TOKEN_BEDROCK`, or the credential chain with profile and region), never the key itself. Apart from that line and the blank-key check it reads no AWS environment variables; it defers to `boto3`.

### Retry and rate-limit behaviour

- `ThrottlingException`, `ModelTimeoutException`, `InternalServerException`, `ServiceUnavailableException`, `ModelNotReadyException` and transport failures (connection closed, endpoint unreachable, read or connect timeout): up to 3 total attempts (`MAX_PROVIDER_ATTEMPTS`) inside one logical-operation deadline, with exponential backoff + jitter. This is the retry loop every built-in adapter shares, and each Converse call is one attempt of it. A `ThrottlingException` after the last attempt is translated to `AgentRateLimited` so upstream handlers treat it the same as an Anthropic 429.
- Other Bedrock `ClientError` responses: raised as `BedrockAPIError` with the mapped HTTP-ish status code.
- Retries respect the run deadline so retries cannot push past `MAX_TOTAL_SECONDS`.

### Transport

boto3 is synchronous: each Converse call runs on the provider lane of the process loop, and the async twins (`acomplete`, `achat`, ...) await it while the sync methods wait for them. The client's `read_timeout` bounds the thread; the attempt's deadline ends the wait even while that thread still runs until its timeout.

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
| `default_headers` | Optional headers sent with every request; server mode: `INQTRIX_SEARCH_EXTRA_HEADERS`. See [Extra headers](#extra-headers-for-an-api-gateway). |

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

- Searches go through the async Perplexity client on the process loop, one client per adapter and event loop; `search()` waits for that call, `asearch()` awaits it.
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
    api_key="...",
    agent_name="web-search-agent",
    # agent_version="2",                     # optional version label
)
```

| Parameter | Purpose |
|-----------|---------|
| `project_endpoint` | Foundry project endpoint (`.services.ai.azure.com/api/projects/...`). |
| `agent_name` | Pre-created agent name (required). Env alias in server mode: `WEB_SEARCH_AGENT_NAME`. The adapter references it per call via `agent_reference`. |
| `agent_version` | Optional version label, default `""`. Env alias in server mode: `WEB_SEARCH_AGENT_VERSION`. |
| `api_key` | Foundry project key (required), sent in the `api-key` header. Env alias in server mode: `AZURE_AI_PROJECT_API_KEY`. As with [Azure OpenAI](#azure-openai-llm), Entra ID sign-in is not supported. |
| `default_headers` | Optional headers sent with every request; the project key's `api-key` header always wins. Server mode: `INQTRIX_SEARCH_EXTRA_HEADERS`. See [Extra headers](#extra-headers-for-an-api-gateway). |

### Azure Foundry Web Search `search_model` property

Returns `f"foundry-web:{name}@{version_or_latest}"` so operators see the agent identity and version in `/health` and `/v1/stacks`.

### Transport and concurrency

Searches go through the async OpenAI client on the process loop, one client per adapter and event loop, against the project's `/openai/v1` endpoint; `search()` waits for that call, `asearch()` awaits it. `max_concurrency` (default 6) caps the searches one adapter runs at once; a search that waits for a slot holds no thread, and one that waits past its deadline ends as a provider timeout.

The grounded search is a model call that Inqtrix does not instruct: the Foundry agent reads web pages and writes its answer under its own server-side instruction, and only the user query goes along with `agent_reference`. The rule that fenced content is data (`UNTRUSTED_CONTENT_RULE`, see [Cognitive kernel](../architecture/agent-platform.md#cognitive-kernel-modeagent_kernel)) applies only further down, when the answer is fenced as source text, not inside the agent.

### Response shape

The adapter collects Responses-API tool output and normalises it into a `GroundedSearchResult` (a dataclass, not a dict):

- `answer` — assembled from the agent's final message content.
- `sources` — one `GroundedSource` per cited URL, de-duplicated, with `url`, `title`, `rank`, `origin` (`url_citation`, `markdown_link`, or `answer_url_fallback`), and the annotation offsets. URL-only: Foundry returns no snippets.
- `prompt_tokens` / `completion_tokens` — from the Responses `usage` block (`input_tokens` / `output_tokens`).
- `related_questions` — exists on the dataclass but is never populated by this adapter.

### Errors and degradation

- `AzureFoundryWebSearchAPIError` exists and is exported, but this adapter never raises it. HTTP 429 becomes `AgentRateLimited`; every other `APIStatusError` is turned into a non-fatal notice with the mapped status code (`provider_timeout` on 408, `upstream_5xx` on 5xx, otherwise `provider_error`) and an empty result.
- An answer the agent did not finish (`status: incomplete`, for example `incomplete_details.reason: content_filter`) is no grounded answer: the adapter logs a WARNING with the reason and returns an empty result with a notice (`content_filter`, otherwise `incomplete_answer`) and the call's usage, because the call is billed. The kernel's web tool then reports the restriction to the model; a research run keeps the query with its notice.
- Any other exception is caught by a final `except Exception` block, recorded as a non-fatal notice, and the adapter returns a fresh empty `GroundedSearchResult()`. The node keeps running with no citations; the iteration log records the fallback. `AgentCancelled` and `AgentTimeout` are re-raised instead — a cancelled or timed-out run must never degrade into a fake empty result.

## Related docs

- [Agent config](agent-config.md)
- [Settings and env](settings-and-env.md)
- [Architecture overview](../architecture/overview.md)
