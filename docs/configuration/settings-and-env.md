# Settings and environment variables

> Files: `src/inqtrix/settings.py`, `src/inqtrix/server/container.py`, `.env.example`

## Scope

This page is the single source of truth for environment variables: it lists **every** variable the code reads. `Settings` is a Pydantic `BaseSettings` container with fourteen groups — Models, Providers, Agent, Server, Auth, Storage, Queue, Knowledge, Quota, Sharing, Editor guest links, Collaboration, Agent platform, Observability — each loaded from process environment variables and optionally a local `.env` file. The groups are the only env-coupled surface: the providers and stores they configure receive every value via constructor arguments (Constructor-First). Further categories sit outside `Settings` and are documented on this page: [process-level variables](#process-level-variables-outside-settings) read by the server/worker bootstrap (including the Helm maintenance job's `INQTRIX_K8S_*` group), [Compose-only variables](#compose-only-variables) read by the container stack rather than the app, and [development and test-only variables](#development-and-test-only-variables) read by scripts, the eval harness, and the test suite. Which PROCESS reads a variable — and how to set it in a deployment that does not use the shipped templates — is mapped in [Who reads a variable](#who-reads-a-variable--and-how-to-set-it-yourself).

Deep-dive pages (provider recipes, auth modes, logging, knowledge profiles) cover usage and walkthroughs; when they name a variable they link back here for the authoritative definition. The committed `.env.example` and `deploy/.env.stack.example` templates are practical starting points, not the reference — this page is.

## Configuration sources

1. Real process environment variables (`export VAR=...`, CI/CD secrets, Docker `-e`, Kubernetes `env:`).
2. A local `.env` file for development only.
3. Built-in defaults for non-sensitive values.

When the same variable exists in both process env and `.env`, the process environment wins. Do not commit `.env` and do not rely on checked-in config files for production credentials.

### Who reads a variable — and how to set it yourself

A variable that never reaches its reader looks like a setting and is not
one. Inqtrix has **six** reader surfaces, and setting a value requires a
different mechanism per surface. This matters most when a deployment writes
its own manifests or its own `values.yaml` instead of using the shipped
templates:

| Reader | What reads it | How to set it in your own deployment |
| --- | --- | --- |
| **api + worker** (the `Settings` groups on this page) | Both processes load the identical `Settings` object; whether a knob does anything is a property of which code runs in that process, not of the variable name. | Helm: a `config:`/`extraConfig:` entry — the ConfigMap is mounted into api **and** worker via `envFrom`. Raw manifests: set the env var on both pods. |
| **web gateway** (`src/inqtrix_web_gateway/`) | Raw `os.getenv`, no pydantic. `INQTRIX_MAX_UPSTREAM_CONNECTIONS`, `RESEARCH_DESK_*`, `INQTRIX_BACKEND_URL`, … | Helm: the web pod has **no `envFrom`** — `config:` never reaches it. Use the typed value where one exists (`web.maxUpstreamConnections`) or `web.extraEnv`. Raw manifests: env var on the web pod. |
| **collaboration server** (`apps/collaboration-server/`) | Its own config parser; several `INQTRIX_COLLABORATION_*` names have no `Settings` counterpart. | Helm: via `config:` — the chart re-emits the relevant keys onto the collaboration container explicitly. Raw manifests: env vars on that pod. |
| **process bootstrap** (outside `Settings`) | `INQTRIX_LOG_*`, `INQTRIX_SERVER_HOST/PORT`, `OTEL_*`, and the Helm-job-only `INQTRIX_K8S_*` group ([below](#process-level-variables-outside-settings)). | Same as api/worker; the `INQTRIX_K8S_*` group is set only on the chart's maintenance job. |
| **Compose interpolation** | `${VAR}` in `deploy/compose/*.yaml` — configures containers, never the app. `INQTRIX_BUNDLED_POSTGRES_MAX_CONNECTIONS`, `INQTRIX_WEB_PORT`, `INQTRIX_PG_*`, … | Shell/env-file at `podman compose` invocation time. **These names do nothing on Kubernetes** — see the per-variable notes. |
| **Helm values without an env-var form** | `postgres.maxConnections` renders a container **argument**; `api.replicaCount` renders `spec.replicas`. | Only via `values.yaml`. Putting the Compose-era name into `config:` creates a ConfigMap entry nothing reads. |

Two hazards deserve names:

- **Chart-derived variables.** The chart computes a set of ConfigMap keys itself,
  so a deployment that bypasses the chart must set them. They are not
  homogeneous: `INQTRIX_PUBLIC_BASE_URL` may be overridden explicitly,
  while `INQTRIX_REPLICA_COUNT` may not — the chart refuses to render when
  it appears in `config:`/`extraConfig:`, because a too-low value is
  caught at render time but a too-HIGH value would pass the chart and then
  crash-loop every api pod at startup via the app's local-store guard. The
  complete list, with the condition and the override rule per key, is in
  [Chart-derived environment variables](../deployment/kubernetes.md#chart-derived-environment-variables).
- **Process asymmetry.** Because api and worker share one ConfigMap, every
  shared key is always set on both pods — and some govern only one:
  `INQTRIX_WORKER_CONCURRENCY` and `INQTRIX_UPLOAD_MAX_CONCURRENT` act in
  the worker; `INQTRIX_STREAM_READER_WORKERS` acts in the api;
  `MAX_CONCURRENT`/`RUN_MAX_CONCURRENT` act in both but differently
  (admission in the api; in the worker they bound agent child-run
  submission through the same store). The per-variable tables note this
  where it bites.

The **effective** values of the concurrency limits are visible at runtime
in Settings → Admin → System (`/v1/admin/system/runtime`, instance-admin
only) — published there and not on the unauthenticated `/v1/capabilities`,
because capacity ceilings have no browser consumer.

## Models (`ModelSettings`)

Routes each agent role (classify, plan, evaluate, answer, claim extraction, direct chat) to a model name; `SEARCH_MODEL` names the search provider's model and is not part of the tier router. Resolution order per node: an explicit per-request model (`MODEL_OVERRIDE`, the UI model picker) -> `<node>_model` -> `tier_<tier>_model` -> `reasoning_model`. Default node-to-tier mapping: answer -> high; plan/evaluate/direct_chat -> mid; classify/claim_extract -> fast. Full reference, including the per-provider reasoning-effort mapping, in [LLM calls](../architecture/websearch/nodes.md).

| Variable | Default | Effect |
|----------|---------|--------|
| `REASONING_MODEL` | `claude-opus-4.6-agent` | Primary reasoning model and fallback for every unset role. Format is provider-dependent: OpenAI/LiteLLM model id, Anthropic model name, or Azure deployment name. |
| `SEARCH_MODEL` | *(empty)* | Model called by `PerplexitySearch` or a LiteLLM-routed search adapter. Ignored by non-LLM search providers. |
| `TIER_HIGH_MODEL` / `TIER_MID_MODEL` / `TIER_FAST_MODEL` | *(reasoning)* | Models for the high / mid / fast tiers. Empty falls back to `REASONING_MODEL`. |
| `INQTRIX_SESSION_TITLES_ENABLED` | `true` | Optional short titles in Chat and Agent Desk. Requires `TIER_FAST_MODEL`; otherwise the provisional title remains. Does not select or download another model. |
| `TIER_HIGH_EFFORT` / `TIER_MID_EFFORT` / `TIER_FAST_EFFORT` | *(empty)* | Per-tier reasoning effort: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. Empty inherits the provider constructor default. For Anthropic any non-empty/non-`none` value turns on adaptive thinking. |
| `CLASSIFY_MODEL`, `CLAIM_EXTRACT_MODEL`, `EVALUATE_MODEL`, `PLAN_MODEL`, `ANSWER_MODEL`, `DIRECT_CHAT_MODEL` | *(tier/reasoning)* | Per-node overrides; an explicit per-node model always wins over the tier. `CLAIM_EXTRACT_MODEL` is the highest-volume role (one call per search hit) and the largest cost lever. |

> **Note.** The `REASONING_MODEL` default is a LiteLLM alias, not a real Anthropic model id. Runtime code reads model names constructor-first via `provider.models.effective_*_model`; new endpoints and strategies must follow that rule rather than reading `settings.models.*` directly.

**Interactions.** The per-request routing overrides `MODEL_TIER`, `MODEL_OVERRIDE`, and `EFFORT_OVERRIDE` live in the Agent block (below) but act on this block's tier router. The Knowledge block adds two fast-tier call sites (`knowledge_rerank`, `knowledge_decompose`) that resolve through the same tiers.

**When this block is unset.** Answer and research roles run on `REASONING_MODEL`. Functional, but the high-volume claim-extraction role then pays frontier-model cost; setting `TIER_FAST_MODEL` is the standard optimisation. Optional `session_title` calls are skipped without a dedicated Fast model. Their capability block publishes `enabled`, `available`, `reason`, `model`, and output limits.

## Providers (`ProviderSettings`)

Selects WHICH provider builds each axis of the auto-created server stack, plus the per-provider construction knobs that have no home in the Models block. Two independent axes (any LLM pairs with any search backend); unset reproduces the byte-identical LiteLLM + Perplexity default. Per-combination `.env` recipes live in [Provider recipes](../getting-started/provider-recipes.md) — this block is the variable reference.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_LLM_PROVIDER` | `litellm` | `litellm`, `anthropic`, `azure`, or `bedrock`. An unknown value fails at `Settings()` construction; a missing required credential for the chosen provider fails loudly at startup — never a silent fallback to litellm. |
| `INQTRIX_SEARCH_PROVIDER` | `perplexity` | `perplexity` or `azure_foundry`. Independent of the LLM axis. |
| `INQTRIX_SELECTABLE_CHAT_MODELS` | *(empty)* | Comma-separated chat models offered in the UI model picker (feeds `/health.models_catalog`). Empty offers only the resolved default model. |
| `INQTRIX_TEMPERATURE` | *(unset)* | Sampling temperature for the `anthropic`/`azure`/`bedrock` providers. `litellm` ignores it and logs a visible warning when set. |
| `INQTRIX_TOKEN_BUDGET_PARAMETER` | *(empty)* | Output-budget request field for `litellm`/`azure`: `max_tokens` or `max_completion_tokens` (the latter for OpenAI o-series). Anthropic/Bedrock ignore it (logged when set). |
| `INQTRIX_SEARCH_PRESET` | *(empty → `fast-search`)* | Perplexity agent preset: `fast-search`, `pro-search`, or `deep-research`. Ignored by `azure_foundry` (logged when set). Web search has no high/mid/fast tiers — this preset plus `SEARCH_MODEL` are the search knobs. |
| `INQTRIX_SEARCH_INSTRUCTIONS` | *(empty)* | Optional system instructions for the Perplexity search agent; ignored by `azure_foundry`. |
| `INQTRIX_AZURE_FOUNDRY_MAX_CONCURRENCY` | `6` | Per-process gate shared by Agent Desk and Research Desk calls using the same Azure Foundry search-provider instance. This is an Inqtrix deployment limit, not an Azure service-wide maximum. |

Further tuning (the chosen provider's credentials): `anthropic` reads `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`; `azure` reads `AZURE_OPENAI_ENDPOINT` plus `AZURE_OPENAI_API_KEY` or the `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` service-principal trio; `bedrock` reads `AWS_REGION` (default `eu-central-1`) / `AWS_PROFILE`; `azure_foundry` search reads `AZURE_AI_PROJECT_ENDPOINT` plus `WEB_SEARCH_AGENT_NAME` (and the optional `WEB_SEARCH_AGENT_VERSION` pin), authenticating with `AZURE_AI_PROJECT_API_KEY` or the same `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` trio. Per-tier reasoning effort and tier models stay in the Models block (`TIER_*_EFFORT`, `TIER_*_MODEL`); the selector passes them to whichever provider is chosen. Context window and output budget are NOT env vars — the model cards (`src/inqtrix/model_cards.py`) are the authoritative source. Inqtrix passes provider credentials explicitly (constructor-first) and does not rely on the underlying SDKs' own credential env vars (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `PPLX_API_KEY`); use `LITELLM_API_KEY`/`LITELLM_BASE_URL` and `PERPLEXITY_API_KEY` instead. The one exception is Bedrock, which uses boto3's standard AWS chain (`AWS_ACCESS_KEY_ID` etc.) when `AWS_PROFILE` is not set.

**Interactions.** Unset selectors reproduce the legacy `LiteLLM` + `PerplexitySearch` bodies exactly; there is deliberately no inference from which keys are present (that would be a silent fallback). A behavioural knob set for a provider that ignores it (e.g. `INQTRIX_TEMPERATURE` with `litellm`) is logged, never silently dropped.

**When this block is unset.** The server builds `LiteLLM` for reasoning and `PerplexitySearch` for search from the Server block's gateway/keys, identical to pre-selector behaviour.

## Agent (`AgentSettings`)

Tunes a single research run: loop bounds, stop thresholds, timeouts, input limits, and the search cache. `REPORT_PROFILE` doubles as a preset trigger — assigning `compact` or `deep` applies a bundle of overrides for any field not explicitly set, so explicit env values always win over the profile.

| Variable | Default | Effect |
|----------|---------|--------|
| `REPORT_PROFILE` | `compact` | `schnell`, `compact` or `deep`. `schnell` presets `max_rounds=1`, `min_rounds=1`, `confidence_stop=6`, `first_round_queries=6` for the lowest-latency answer; `compact` presets `max_rounds=2`, `min_rounds=1`, `confidence_stop=7`, `first_round_queries=6`; `deep` presets `max_rounds=3`, `min_rounds=2`, `confidence_stop=7`, `first_round_queries=6`, and larger evidence/output context. Every profile uses 600-second AI-operation budgets and a 3600-second active-run budget unless explicitly overridden. See [Report profiles](report-profiles.md). |
| `DEPTH` | `normal` | Agent-mode thoroughness: `normal` or `deep`. `deep` is deterministic budget + verification — high kernel reasoning effort (unless the request or a skill pin sets one), the raised kernel iteration ceiling, DEEP child research profiles, and exactly ONE rubric-checked verification/revision round before the final answer. Per-request override: `agent_overrides.depth`. Distinct from `REPORT_PROFILE` (research answer preset). |
| `AGENT_TIER` | *(empty)* | Agent Desk speed/depth Stufe: `schnell` (one instant web search, no clarification, no plan gate outside `strict`, chat answer), `gruendlich` (bounded research children, max. one clarification round) or `tief` (full budgets, canvas report, escalating quote verification). Empty keeps the legacy `DEPTH` semantics. Per-request override: `agent_overrides.agent_tier`; sending it together with a contradictory `depth` is a 400. All tier budgets come from the ONE `TIER_POLICIES` table and are validator-enforced, then published under `agent.tiers`. |
| `MAX_ROUNDS` | `2` | Hard upper bound for the research loop (`4` under DEEP). |
| `MIN_ROUNDS` | `1` | Suppresses early stops until this round. There is no clamp anywhere: the suppression additionally requires `round < MAX_ROUNDS`, so `MAX_ROUNDS` always wins and a `MIN_ROUNDS` set above it never extends the loop. |
| `CONFIDENCE_STOP` | `7` | Minimum evaluator confidence (1-10) at which the stop cascade may emit `done` (`8` under DEEP). |
| `MAX_TOTAL_SECONDS` | `3600` | Outer active research-run budget, checked at node boundaries and used to clamp each provider-operation deadline. |
| `REASONING_TIMEOUT` | `600` | Logical reasoning-operation budget (classify/plan/evaluate/answer), including at most three attempts and their backoff. |
| `EDITOR_ASSISTANT_TIMEOUT` | `600` | Logical editor suggest/instruct budget, independent from research reasoning. |
| `SEARCH_TIMEOUT` | `600` | Logical search-provider budget, including at most three attempts and their backoff. |
| `CLAIM_EXTRACT_TIMEOUT` | `600` | Logical claim-extraction budget (and `/v1/text` improvement call), including retries. |
| `SKIP_SEARCH` | `false` | Short-circuits the entire research graph — classify, plan, search, evaluate and the answer node never run — and answers directly from the LLM provider with the question plus conversation history. No citations, `round` stays `0`. |
| `TESTING_MODE` | `false` | Expose `/v1/test/run` (used by `inqtrix-parity run`). Never enable in production: no rate limiting, returns full iteration logs. |
| `OBSERVABILITY_PROFILE` | `summary` | `summary`, `debug` (reserved), or `forensic` (full source/citation/claim/stop/answer lineage in the protected run audit; ordinary logs receive only IDs, states and metrics). See [Logging](../observability/logging.md). |

Further tuning: `FIRST_ROUND_QUERIES` (6), `REQUIRED_CONTEXT_WINDOW_TOKENS` (128000), `MAX_QUESTION_LENGTH` (60000 — generous because the chat composer inlines attached file content), `HIGH_RISK_SCORE_THRESHOLD` (4 — observability signal only), `SEARCH_CACHE_MAXSIZE` (256; `0` disables), `SEARCH_CACHE_TTL` (3600), `MODEL_TIER` / `MODEL_OVERRIDE` / `EFFORT_OVERRIDE` (per-request model routing, normally sent by API clients rather than set globally). `ANSWER_PROMPT_CITATIONS_MAX` (60) is still accepted and profile-overridden but no runtime code reads it, so setting it changes nothing. The per-call/run timeouts and how the HTTP and client waits derive from them are detailed under "Timeout dependency chain" below.

The shipped full-stack starter pins these timeout and concurrency values
explicitly; the block it sets, and why, is in [Stack quickstart](../getting-started/stack-quickstart.md#1-configure).

**Interactions.** `REPORT_PROFILE` rewrites the other loop fields unless they were set explicitly. `MODEL_TIER` replaces the default node-to-tier mapping for the research nodes, the knowledge gate/decompose/contextualize/answer calls, the editor assist call, the agent kernel and its final review, and the five override-scoped Mission nodes (`agent_plan`, `agent_synthesis`, `agent_deep_review`, `agent_answer`, `agent_answer_light`); the LLM reranker, the ingestion contextualizer and the eight unscoped Mission nodes stay on their tier-map assignment. `MODEL_OVERRIDE`/`EFFORT_OVERRIDE` bypass tier routing on every path that honours a per-request model: the direct-chat call, the knowledge answer, the editor suggest/instruct call, the agent kernel and final review, and the five override-scoped Mission nodes; the remaining research nodes keep tier routing, and `EFFORT_OVERRIDE` additionally beats the tier effort even where the model stays tier-routed. Mission children inherit the selected settings, including skill pins, before the kernel's automatic Deep effort increase; research children retain their own model routing.

**Timeout dependency chain.** Three layers bound any single operation, and the same outer-wait shape deliberately hangs off different base budgets depending on the endpoint — making that explicit is the point of this section. Every per-call timeout is additionally clamped to the time left in `MAX_TOTAL_SECONDS` (`_bounded_timeout`), so no single call can outlive the run.

```text
Base logical-operation / run budgets (env-configurable)
  MAX_TOTAL_SECONDS ........ whole-run wall clock
  REASONING_TIMEOUT ........ one reasoning operation, all attempts included
  EDITOR_ASSISTANT_TIMEOUT . one editor suggest/instruct operation
                             (defaults to the REASONING_TIMEOUT default)
  SEARCH_TIMEOUT ........... one search-provider operation, all attempts included
  CLAIM_EXTRACT_TIMEOUT .... one claim-extraction operation; the /v1/text call

Derived HTTP waits  (inner budget + REQUEST_WAIT_MARGIN_SECONDS, 30s; each is
                     also capped at the chat wait, so none outlives the run)
  chat + SSE streaming     <- MAX_TOTAL_SECONDS
  editor suggest/instruct  <- EDITOR_ASSISTANT_TIMEOUT  (NOT CLAIM_EXTRACT_TIMEOUT:
                                                        editor work is a full
                                                        generation, not a tight hit)
  /v1/text improvement     <- CLAIM_EXTRACT_TIMEOUT

Client abort timeouts (browser)
  editor AbortController   = server editor wait + client margin
  chat-chain step abort    = server chat  wait  + client margin
  Both are DISCOVERED from GET /v1/capabilities ("timeouts" block), never
  hardcoded, so raising a server budget lengthens the client abort in step.
```

`REQUEST_WAIT_MARGIN_SECONDS` (30s, an internal constant in `constants.py`, not
an env var) is the grace each HTTP wait adds over its inner per-call budget so
the inner call raises its specific provider error before the outer
`asyncio.wait_for` fires a generic 504.

**When this block is unset.** There is no off switch — unset means the COMPACT defaults. The one degradation toggle is `SKIP_SEARCH=true`, which turns every run into a direct LLM answer without web research or citations; UI clients normally send it per request instead of setting it globally.

## Server (`ServerSettings`)

Configures the FastAPI surface started by `python -m inqtrix`: the upstream LLM gateway for auto-created providers, request concurrency, native-run retention, and transport hardening. Library-mode users (instantiating `ResearchAgent` directly) can ignore this group entirely.

| Variable | Default | Effect |
|----------|---------|--------|
| `LITELLM_BASE_URL` | `http://litellm-proxy:4000/v1` | OpenAI-compatible gateway for the auto-created `LiteLLM` provider. Must include `/v1`. |
| `LITELLM_API_KEY` | `sk-placeholder` | Bearer key for the gateway. The obvious placeholder makes misconfiguration fail loudly on the first upstream call. |
| `PERPLEXITY_API_KEY` | *(empty)* | Key for the auto-created `PerplexitySearch` provider (its own endpoint, not the gateway). |
| `MAX_CONCURRENT` | `100` | Concurrent `/v1/chat/completions` requests. Native runs reuse this unless `RUN_MAX_CONCURRENT` is set. Each active run holds one thread (the research graph is synchronous), and this value sizes the dedicated AI thread lane — with headroom, because a disconnected stream frees its admission slot while its thread runs on. The real ceiling is the provider rate limit + host CPU/RAM. |
| `INQTRIX_STREAM_READER_WORKERS` | `128` | Threads reserved for event-stream readers, kept apart from the AI thread lane so a busy chat cannot stall event delivery. Drawn on by every open run stream, every open indexing stream, and every streamed chat completion. Sized above the run cap so a hundred runs with a viewer each still get their own reader. Past this many readers they are served in rotation — events are delayed, never dropped. A waiting thread costs a few kilobytes and does not hold the interpreter lock. |
| `RUN_MAX_CONCURRENT` | `100` | Active-job cap for native `/v1/runs`, set explicitly so runs and chat have independent ceilings. Clearing the environment variable yields this default, NOT the historical fallback to `MAX_CONCURRENT`, and an EMPTY value (`RUN_MAX_CONCURRENT=`) fails startup validation rather than meaning "unset". The `None` fallback survives only for callers that build settings programmatically. |
| `RUN_MAX_CONCURRENT_PER_USER` | *(unset)* | Optional per-user fairness bound under the global run cap: a single subject's QUEUED+RUNNING native runs. Parked (waiting) agent runs are excluded; agent child runs count against the parent's user (size it above one agent tree). Unset = admission stays purely global (single-tenant unchanged); the monthly quota still bounds each user's spend. A rejected submit answers HTTP 429 with `reason=per_user_limit`. |
| `RUN_QUEUE_MAX_SIZE` | `100` | Waiting native runs; a full queue returns HTTP 429 on `POST /v1/runs` (with `reason=queue_full`). |
| `INQTRIX_SERVER_API_KEY` | *(empty)* | Static Bearer gate on chat, text-improvement, test-run, and native run routes (`hmac.compare_digest`). `/health` and `/v1/models` stay public. Also drives auth-mode inference (see Auth). |
| `INQTRIX_METRICS_ENABLED` | `false` | Mount the Prometheus `/metrics` endpoint (run-queue gauges, admission rejections by reason, per-route request latency, plus the shared call metrics: LLM request/duration/token series, search, retrieval steps, run durations, queue wait, worker jobs, indexed documents; bounded cardinality, no run-id/subject/session labels). Needs the image built with the `metrics` extra (the stack image bakes it in); the flag alone otherwise logs a WARNING and stays off. Bearer-gated with the API key when one is set, else keep it cluster-internal. Worker processes get their own exporter via `INQTRIX_WORKER_METRICS_PORT` (Queue section). See [Metrics](../observability/metrics.md). |
| `INQTRIX_SERVER_CORS_ORIGINS` | *(empty)* | Comma-list of allowed origins; installs `CORSMiddleware` with credentials. `*` is accepted but WARNs (browsers reject wildcard with credentials). |
| `INQTRIX_ENABLE_OPENAPI` | `false` | Serve `/openapi.json`, `/docs`, `/redoc`. Documentation routes only; never changes API behaviour. |
| `INQTRIX_PUBLIC_BASE_URL` | *(empty)* | Externally reachable base URL. It anchors collaboration same-origin checks behind TLS proxies, OIDC callback derivation, and clickable `/v1/sources/...` citations. Empty keeps internal `inqtrix://` citations and forwarded headers cannot authorize a collaboration origin. Helm derives it from an enabled Ingress; explicit config wins. |
| `INQTRIX_MAX_TOTAL_INPUT_TOKENS` | `500000` | Approximate-token DoS cap on `question` + `messages[]` (estimated `len(text) // 4`). |

Further tuning: `RUN_COMPLETED_TTL_SECONDS` (300 — how long finished native runs stay queryable in memory), `RUN_DURABLE_RETENTION_SECONDS` (7776000 — 90-day retention for terminal native run records in the DURABLE Postgres store, so completed reports survive reloads, re-logins and other devices; distinct from `RUN_COMPLETED_TTL_SECONDS`, which bounds only the in-memory replay window, and ignored by the in-memory store. `0` evicts on the next cleanup), `RUN_INCOGNITO_RETENTION_SECONDS` (900 — how long a finished incognito Knowledge Desk ask stays in either run store before the run, its events and its result are deleted at the next cleanup, which the API process runs at least once a minute; ahead of both retentions above, at least 60 so the answer stays fetchable for the browser that asked), `RUN_EVENT_BUFFER_SIZE` (200 — replay buffer for late SSE subscribers), `INQTRIX_DELETION_MAX_CONCURRENT` (2 — aggregate deletion workers admitted in one process; the distributed worker ceiling still applies), `INQTRIX_UPLOAD_MAX_CONCURRENT` (6 — uploads executed at once by a WORKER process; unlike `INQTRIX_DELETION_MAX_CONCURRENT` beside it, this one has no effect in the API process, where uploads are reconciled one at a time on a path that is not configurable. A research run spends its thread waiting on a provider, but an upload extracts text from the file itself and spends real CPU, so it is capped independently of `INQTRIX_WORKER_CONCURRENCY`, which remains an additional upper bound), `INQTRIX_DELETION_DISPATCH_TIMEOUT_SECONDS` (240 — after this long unclaimed in `queued`, a deletion operation fails with `dispatch_timeout` so it stops being a dead end and becomes retryable; keep it above the worker reconciler's re-dispatch window of 120 s plus its 60 s tick, and lower it to about 60 s only without the Valkey queue, where no reconciler exists), `INQTRIX_DELETION_RECEIPT_RETENTION_SECONDS` (2592000 — 30-day retention for terminal technical deletion receipts without source content), `MAX_MESSAGES_HISTORY` (20), `INQTRIX_MAX_MESSAGE_COUNT` (200 — HTTP 413 above), `PERPLEXITY_BASE_URL`, `INQTRIX_SERVER_TLS_KEYFILE` / `INQTRIX_SERVER_TLS_CERTFILE` (both or neither; partial setup raises `RuntimeError`). See [Security model](../deployment/security-model.md).

**Interactions.** `INQTRIX_SERVER_API_KEY` is the inference input for `INQTRIX_AUTH_MODE=infer`. `INQTRIX_PUBLIC_BASE_URL` feeds knowledge citation links, the derived OIDC callback URL, and the trusted external collaboration origin. `LITELLM_BASE_URL`/`LITELLM_API_KEY` are reused as the default embedding endpoint by the Knowledge block.

**When this block is OFF.** In pure library mode the whole group is ignored. With everything at defaults the server runs open (no auth gate, no CORS headers, no TLS, no OpenAPI schema) against a placeholder gateway that fails on the first LLM call.

## Auth (`AuthSettings`)

Selects how every HTTP request resolves to a `Principal`. Four modes exist: `none` (anonymous principal), `apikey` (static Bearer gate, byte-identical to the legacy behaviour), `oidc` (browser session via authorization code + PKCE through a backend-for-frontend; Dex is the reference IdP in the dev compose stack), and `local` (native email/password accounts with a first-run owner setup). The built-in default is `infer`, which resolves to `apikey` or `none` and never to `local`; the shipped Compose stack sets `INQTRIX_AUTH_MODE=local` explicitly in `deploy/.env.stack.example`. The two cookie-session modes (`oidc`/`local`) share the same session/CSRF/PAT machinery; `local` rides a synthetic issuer and so reuses it verbatim. Inqtrix speaks only generic OIDC for `oidc` — discovery plus claim mapping, no hardwired provider. Per-mode walkthroughs: [Auth modes](../deployment/auth-modes.md), [Create and manage users](../deployment/create-and-manage-users.md).

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_AUTH_MODE` | `infer` | `none`, `apikey`, `oidc`, `local`, or `infer`. `infer` derives the mode for backwards compatibility: non-empty `INQTRIX_SERVER_API_KEY` means `apikey`, empty means `none`. An explicit value wins over inference. |
| `INQTRIX_OIDC_ISSUER` | *(empty)* | Issuer URL; discovery comes from `{issuer}/.well-known/openid-configuration` and must echo this exact value. Required for `oidc`. |
| `INQTRIX_OIDC_CLIENT_ID` / `INQTRIX_OIDC_CLIENT_SECRET` | *(empty)* | Confidential client registered at the IdP; tokens never reach the browser. Required for `oidc`. |
| `INQTRIX_SESSION_SECRET` | *(empty)* | Server-side secret for CSRF-token derivation (signed double-submit). Required for `oidc` and `local`. |
| `INQTRIX_PAT_PEPPER` | *(empty)* | HMAC pepper for personal access tokens. Required for `oidc` and `local`; rotation invalidates every issued token. |
| `INQTRIX_PSEUDONYM_PEPPER` | *(empty)* | Instance-wide secret behind the pseudonymous `usr_<hex16>` subject references in logs and audit correlation. Set the SAME value for the API server and every worker: one person then carries ONE stable reference across processes and restarts, and admins can resolve a reference via `POST /v1/admin/audit/resolve-pseudonym` (each resolution is itself audit-logged). Empty keeps the historical per-process references and logs a startup WARNING. Required for `oidc`; rotation deliberately unlinks all previously written pseudonyms. |
| `INQTRIX_REGISTRATION` | `open` | First-login admission: `invite` requires a matching open invitation (and the postgres storage backend); `open` keeps the historical behaviour. |
| `INQTRIX_PAT_MAX_PER_USER` | `10` | Active-token cap per user (sprawl guardrail). |
| `INQTRIX_PAT_DEFAULT_TTL_DAYS` | `0` | Default token lifetime when no explicit expiry is given; `0` = non-expiring. |
| `INQTRIX_OIDC_REDIRECT_URL` | *(derived)* | Callback URL registered at the IdP (byte-for-byte match). Empty derives `{INQTRIX_PUBLIC_BASE_URL}/api/auth/callback`. |
| `INQTRIX_OIDC_ALLOWED_GROUPS` | *(empty)* | Comma-separated group allowlist; non-matching logins get a visible, audit-logged 403 at the callback. Empty admits every authenticated user. |
| `INQTRIX_SESSION_MAX_AGE_SECONDS` | `28800` | Absolute session lifetime (8 h); expiry resolves to 401 and the SPA re-runs the login redirect. |
| `INQTRIX_LOCAL_REGISTRATION` | `closed` | `local` only: `closed` (owner + admin-created accounts) or `open`. `open` mounts no route; it only sets `registration.self_service: true` in `GET /api/auth/config` as an advisory hint. |
| `INQTRIX_OIDC_ADMIN_ROLES` / `INQTRIX_OIDC_ADMIN_GROUPS` | *(empty)* | Comma-separated role/group claim values that grant instance-admin on `oidc` login (grant-only — a non-match never demotes). |
| `INQTRIX_LOGIN_RATE_LIMIT_ENABLED` | `true` | Login brute-force throttle for `local`, keyed per identifier + client IP (sliding window + lockout). Tune with `INQTRIX_LOGIN_RATE_LIMIT_MAX_ATTEMPTS` (10), `INQTRIX_LOGIN_RATE_LIMIT_WINDOW_SECONDS` (300), `INQTRIX_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS` (60). |
| `INQTRIX_TRUSTED_PROXY_HOPS` | `1` | Trusted reverse-proxy count for resolving the throttle client IP from the **right** of `X-Forwarded-For`. `1` fits the bundled Python web gateway and is not client-spoofable. The Helm chart derives `2` automatically when its own Ingress fronts the web pod (edge + gateway = two hops). Set the exact chain length for multiple proxies (too high re-opens spoofing); set `0` for a directly-exposed API server (socket peer only). |

Further tuning (IdP exchangeability): `INQTRIX_OIDC_SCOPES` (`openid profile email`; add `groups` for Okta/Dex), `INQTRIX_OIDC_USERNAME_CLAIM` (`preferred_username`, dot paths descend into nested claims), `INQTRIX_OIDC_EMAIL_CLAIM` (`email`), `INQTRIX_OIDC_GROUPS_CLAIM` (`groups`), `INQTRIX_OIDC_ROLES_CLAIM` (`roles`; the claim used for admin elevation, dot paths supported), `INQTRIX_OIDC_ALLOWED_DOMAINS` (*(empty)*; comma-separated email-domain allowlist orthogonal to the group allowlist — a login without a listed email domain gets a visible 403, and a login without any email is rejected fail-closed), `INQTRIX_OIDC_CLAIM_SEPARATORS` (`" ,"`; characters a string-valued group/role claim is split on, a JSON array is used as-is), `INQTRIX_OIDC_GROUPS_STRIP_PATH_PREFIX` (`false`; strip a single leading `/` from Keycloak full-path groups), `INQTRIX_OIDC_PROVIDER_NAME` (*(empty)*; SSO login-button label surfaced by the auth-config endpoint), `INQTRIX_OIDC_SKIP_EMAIL_VERIFIED` (`false`; required for Entra ID), `INQTRIX_OIDC_DISCOVERY_URL`, `INQTRIX_OIDC_USERINFO_FALLBACK` (`true`), `INQTRIX_OIDC_CA_CERT`, `INQTRIX_OIDC_INSECURE_DEV_COOKIES` (`false`; loopback-HTTP dev only, WARNs at startup).

**Interactions.** Mode inference reads `INQTRIX_SERVER_API_KEY` from the Server block. Misconfiguration fails loudly at startup: `apikey` without a configured key and `oidc` without issuer + client id + client secret + session secret + PAT pepper + pseudonym pepper are rejected; `local` without `INQTRIX_SESSION_SECRET` + `INQTRIX_PAT_PEPPER` is rejected; `none` with a configured key disables the gate deliberately and logs a WARNING. The cookie-session modes register the `/api/auth/*` routes — `oidc` adds `login|callback`, `local` adds `POST /api/auth/login/local` and the `/api/setup/*` owner gate — sharing the `session|logout` and PAT routes; the dev compose stack starts Dex only under the `oidc` profile. Both want the postgres storage backend for durable accounts/sessions (memory works but logins evaporate on restart).

**When this block is OFF.** Mode `none`: every request resolves to the anonymous principal and all routes are open — acceptable only on trusted networks. The mode is reported by the auth provider, not by re-reading the raw `INQTRIX_AUTH_MODE` value.

## Storage (`StorageSettings`)

Selects persistence for the platform layer (identity, file registry, run records) and for uploaded binary blobs (ObjectStore). The two selectors are independent: relational state goes to `memory` or `postgres`, blobs go to `local` disk or any `s3`-compatible endpoint.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_STORAGE_BACKEND` | `memory` | `memory` (no external services, pytest default) or `postgres`. There is deliberately no SQLite option — the schema relies on PostgreSQL 15+ row-level security and constraint features. |
| `INQTRIX_DATABASE_URL` | *(empty)* | Runtime async SQLAlchemy URL, e.g. `postgresql+asyncpg://inqtrix:...@127.0.0.1:5432/inqtrix`. Required for `postgres`; empty + `postgres` fails loudly at startup. API/worker use this restricted path, never the migration-only credential. |
| `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY` | `restricted` | `restricted` requires the session login itself to be `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`, have no database/schema `CREATE`, not own or inherit any RLS-owner/BYPASS role, and only `SET ROLE` to the strict app role. `bundled_legacy` is limited to the chart/Compose bundled owner login and still requires the effective app role to be strict. Never use `bundled_legacy` to waive a managed-database role error. |
| `INQTRIX_MIGRATION_DATABASE_URL` | *(empty)* | Optional direct PostgreSQL URL read only by `inqtrix-migrate`. Empty falls back to `INQTRIX_DATABASE_URL` for bundled/backwards-compatible installations. Production should inject a dedicated migration credential only into the one-shot job. |
| `INQTRIX_MIGRATION_RLS_MODE` | `auto` | `auto`, `owner`, or `bypass`. `auto` accepts an ordinary schema owner on a fresh, empty schema and, on an installed schema, only a superuser or `BYPASSRLS` login; `bypass` requires a dedicated `NOSUPERUSER BYPASSRLS` login; `owner` uses transaction-bound `NO FORCE ROW LEVEL SECURITY` and table ownership/DDL rights. It never sets a wildcard tenant or disables RLS. |
| `INQTRIX_MIGRATION_SERVICES_QUIESCED` | `false` | Explicit assertion that API, worker, Collaboration and poolers are stopped and database sessions drained. Required for every actual revision change on an installed schema, independent of RLS mode; a fresh install or no-op at the installed target does not require it. |
| `INQTRIX_DATABASE_APP_ROLE` | `inqtrix_app` | Restricted role switched to via `SET LOCAL ROLE` per transaction. A custom role must expose the same table-level DML/function/sequence contract; empty disables the switch and therefore requires the login itself to satisfy it. The canonical `inqtrix_app` grants remain migration-managed. |
| `INQTRIX_DATABASE_POOL_SIZE` | `5` | Persistent connections per POOLED engine. The engines are deliberately not enumerated here — every process counts the ones it actually builds and logs the total at startup. NullPool stores are unaffected. The worst-case budget of one process is pooled_engines x (pool_size + max_overflow) plus the in-flight NullPool connections; size the sum over all replicas below the server's `max_connections`, or front it with a transaction pooler. |
| `INQTRIX_DATABASE_POOL_MAX_OVERFLOW` | `10` | Burst connections a pooled engine may open beyond `INQTRIX_DATABASE_POOL_SIZE`, closed again when idle. Part of the same budget arithmetic. |
| `INQTRIX_DATABASE_POOL_TIMEOUT_SECONDS` | `30` | Seconds a request waits for a free pooled connection before failing loudly. It bounds the queueing latency an undersized pool would otherwise convert into silent waiting. |
| `INQTRIX_DATABASE_COMMAND_TIMEOUT_SECONDS` | `120` | Per-STATEMENT ceiling on pooled engines (asyncpg `command_timeout`; `0` disables it). Without it a statement on a silently dead connection hangs its worker thread indefinitely. A statement legitimately longer than the ceiling needs a raised value, never a disabled one. |
| `INQTRIX_OBJECT_STORE_BACKEND` | `local` | `local` writes content-addressed blobs below `INQTRIX_OBJECT_STORE_PATH`; `s3` targets any S3-compatible endpoint (SeaweedFS in the dev compose stack) and requires the `INQTRIX_S3_*` fields. File metadata always lives in the file registry, never in the blob store. |
| `INQTRIX_S3_AUTH_MODE` | `static` | `static` requires explicit access/secret keys and accepts an optional STS token. `default` passes no Inqtrix credentials to boto3, enabling workload/container/instance identity; any Inqtrix static credential in that mode is rejected. |
| `INQTRIX_S3_ENDPOINT_URL` | *(empty)* | Optional S3-compatible endpoint, e.g. `http://127.0.0.1:8333` for SeaweedFS. Empty delegates native AWS endpoint resolution to boto3. |
| `INQTRIX_S3_BUCKET` | `inqtrix-files` | Non-empty bucket holding keys below `tenants/<tenant>/files/<uuid>`. Provisioning is controlled separately. |
| `INQTRIX_OBJECT_ORPHAN_GRACE_HOURS` | `24` | Daily object-store consistency sweep in the worker: blobs under `tenants/<tenant>/files/` that have no `files` row and are older than this many hours are deleted (a crash between blob write and row commit leaves such an orphan). Every deletion is logged; a tenant whose listing has objects but no matching registry row at all is skipped with a WARNING (protection against a misresolved tenant context). `0` disables the sweep. The bucket (or local object path) must belong to exactly ONE Inqtrix database — with a shared bucket, one instance's sweep would delete the other instance's live blobs. |
| `INQTRIX_S3_ACCESS_KEY` | *(empty)* | Required with `AUTH_MODE=static`; forbidden with `default`. |
| `INQTRIX_S3_SECRET_KEY` | *(empty)* | Required with `AUTH_MODE=static`; forbidden with `default`. |
| `INQTRIX_S3_SESSION_TOKEN` | *(empty)* | Optional temporary STS token with static credentials; forbidden with `default`. |
| `INQTRIX_S3_REGION` | `us-east-1` | Non-empty SDK region. Set the real bucket region for native AWS. |
| `INQTRIX_S3_ADDRESSING_STYLE` | `path` | `path`, `auto`, or `virtual`. `path` preserves SeaweedFS/MinIO compatibility; `auto` is recommended for native AWS. |
| `INQTRIX_S3_BUCKET_PROVISIONING` | `create_if_missing` | `create_if_missing` preserves bundled behavior and may call `CreateBucket` after a 404. `existing` never creates infrastructure and is recommended for managed storage. |
| `INQTRIX_S3_CA_BUNDLE` | *(empty)* | Optional readable PEM CA-file path for private endpoints. There is no TLS-disable setting. |
| `INQTRIX_S3_SERVER_SIDE_ENCRYPTION` | `none` | `none`, `AES256`, or `aws:kms`; adds only upload encryption arguments and never an ACL. |
| `INQTRIX_S3_KMS_KEY_ID` | *(empty)* | Optional key id/ARN, accepted only with `SERVER_SIDE_ENCRYPTION=aws:kms`. |
| `INQTRIX_MAX_FILE_BYTES` | `104857600` | Per-upload cap (100 MiB), enforced while spooling; HTTP 413 above. |
| `INQTRIX_DOCUMENT_PARSER` | `markitdown` | Server-side preparation of uploads: converts PDF/DOCX/PPTX/XLSX/HTML to Markdown in pure Python, the one text that chat attachments and knowledge ingestion read. Independent of `INQTRIX_KNOWLEDGE_ENABLED`. `none` disables server-side preparation; uploads keep only their original. Switching it on later prepares those uploads from their stored originals on the next reconcile pass, without a new upload; a file the parser cannot read stays marked as failed. |

Further tuning: `INQTRIX_OBJECT_STORE_PATH` (`data/object-store`) and `INQTRIX_REPLICA_COUNT` (`1`; the local backend is rejected above one declared replica).

**Interactions.** Runtime database readiness checks far more than the connection: the packaged Alembic head, the expected tenant-table inventory with enabled and forced RLS, canonical policies and exact least-privilege grants, and a session login plus effective role audited for forbidden DDL, grant and mutation capabilities — the full contract and its failure diagnosis are on the migrations page linked below. PUBLIC table/column ACLs, canonical app-role column ACLs, table `TRUNCATE`, `REFERENCES`, `TRIGGER`, `MAINTAIN`, grant options and non-append-only rights on the append-only tables (`audit_log`, `llm_usage`) are rejected. A mismatch returns `not_ready`, gates business routes and prevents workers from claiming jobs. `INQTRIX_QUEUE_BACKEND=valkey` requires `INQTRIX_STORAGE_BACKEND=postgres` (the run row is the source of truth for distributed execution). Compose-only `INQTRIX_MIGRATION_ENV_FILE` selects an optional separate migration env file; API/worker never load it. See [Database migrations](../deployment/database-migrations.md) and [Object storage](../deployment/object-storage.md).

**When this block is OFF.** With `memory`, identity facts, the file registry, and native run records live in process memory and vanish on restart; finished runs only outlive completion by `RUN_COMPLETED_TTL_SECONDS`. Uploaded blobs still hit the local disk path, but their registry entries do not survive a restart.

## Queue (`QueueSettings`)

Moves native-run execution out of the API process. Two orthogonal upgrades keep the zero-infrastructure default intact: `storage=postgres` makes run records durable (execution stays in-process), and `queue=valkey` additionally dispatches execution to `inqtrix-worker` processes via a Valkey Stream with at-least-once delivery.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_QUEUE_BACKEND` | `memory` | `memory` executes runs in-process; `valkey` dispatches to workers and requires `INQTRIX_STORAGE_BACKEND=postgres` plus a non-empty `INQTRIX_VALKEY_URL`. |
| `INQTRIX_VALKEY_URL` | *(empty)* | Connection URL (`redis://` scheme, e.g. `redis://127.0.0.1:6379/0`). Required for `valkey`; ignored otherwise. |
| `INQTRIX_WORKER_CONCURRENCY` | `100` | Process-wide execution ceiling for one worker replica. It sizes the research loop directly and caps every other loop: indexing also by `INQTRIX_REINDEX_MAX_CONCURRENT`, upload by `INQTRIX_UPLOAD_MAX_CONCURRENT`, deletion by `INQTRIX_DELETION_MAX_CONCURRENT` — each runs at the LOWER of the two, and a ceiling that binds is named in the worker's startup log. That split is why the default is high: a research run blocks its thread waiting on a provider, while an upload extracts text and spends real CPU. |
| `INQTRIX_WORKER_METRICS_PORT` | `0` | Worker-process Prometheus port. `0` (default) keeps the worker without an exporter; a port >0 serves that worker's own registry on `http://0.0.0.0:<port>/metrics` (one target per worker process, no pushgateway, **no bearer gate** — keep it cluster-internal). Requires `INQTRIX_METRICS_ENABLED=true` and the `metrics` extra — otherwise one WARNING and the exporter stays off. |
| `INQTRIX_WORKER_MAX_ATTEMPTS` | `3` | Delivery attempts before a run is dead-lettered and marked failed. Redelivery of finished runs is a no-op. |

Agent Desk instant tasks execute inside their parent run's eight-wide local
wave, so they do not consume eight worker slots. Delegated `web_research` tasks
are separate child runs: to execute eight such children physically at once,
provision at least eight worker slots across replicas. The shipped
`INQTRIX_WORKER_CONCURRENCY` of `100` already covers a wave; a deployment that
lowered it below the Agent width serialises the wave rather than starving it.
The planner/Agent width never silently rewrites the worker setting.

Further tuning: `INQTRIX_WORKER_HEARTBEAT_SECONDS` (15.0 — workers re-claim their in-flight stream entries so long runs are not stolen), `INQTRIX_WORKER_CLAIM_IDLE_SECONDS` (90.0 — idle threshold for reclaiming entries from crashed workers; sized to heartbeat loss, not run duration).

**Interactions.** `valkey` without `postgres` storage or without a Valkey URL fails loudly at startup, on both sides: `build_container` rejects it in the API server, and `inqtrix-worker` refuses to start without `INQTRIX_STORAGE_BACKEND=postgres` plus `INQTRIX_QUEUE_BACKEND=valkey`. The stream carries only dispatch messages; run state lives in the Postgres run row. The dev compose stack provides the Valkey service.

**When this block is OFF.** With `memory`, runs execute inside the API server, capped by `RUN_MAX_CONCURRENT` / `MAX_CONCURRENT`; there are no worker processes, no retries across crashes, and a server restart aborts in-flight runs.

## Knowledge (`KnowledgeSettings`)

The internal document-retrieval engine: collections, document ingestion, hybrid retrieval, and the `mode=knowledge` answer pipeline (gate, grounding, retrieval profiles). Disabled by default so the historical deployment shape stays untouched.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_KNOWLEDGE_ENABLED` | `false` | Master switch. Off: no knowledge/sources routes, no embedding provider, `mode=knowledge` is not a registered algorithm. |
| `INQTRIX_VECTOR_BACKEND` | `memory` | `memory` (in-process, lost on restart) or `qdrant` (persistent, hybrid dense + BM25 retrieval; requires the `knowledge-qdrant` extra and `INQTRIX_QDRANT_URL`). |
| `INQTRIX_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant REST endpoint; default matches host-side development. The canonical Compose `knowledge` profile always requires `INQTRIX_QDRANT_API_KEY`, including on loopback, and refuses to start bundled Qdrant without it. A separately operated pure-loopback Qdrant may deliberately remain unauthenticated, but that is outside the bundled profile. |
| `INQTRIX_KNOWLEDGE_SPARSE` | `bm25_german` | Lexical branch for the qdrant backend: BM25 sparse vectors computed inside the Qdrant server (core inference, server >= 1.15.3; nothing downloads at runtime) and fused with the dense branch via RRF. `off` runs dense-only. Ignored by the memory backend. |
| `INQTRIX_RERANKER_PROVIDER` | `none` | Rerank stage after retrieval. `none` skips it (a visible capability flag, never a silent downgrade); `cohere` calls a Cohere-rerank-schema endpoint (native or Azure AI Foundry serverless) and requires `INQTRIX_RERANKER_BASE_URL` / `INQTRIX_RERANKER_API_KEY` / `INQTRIX_RERANKER_MODEL`; `llm` ranks listwise through the deployment's own LLM. |
| `INQTRIX_KNOWLEDGE_GATE` | `on` | Sufficiency gate for `mode=knowledge`: a fast-tier LLM call judges the evidence and may trigger another retrieval pass; insufficient evidence yields an honest no-evidence answer. |
| `INQTRIX_KNOWLEDGE_GROUNDING` | `on` | Fail-closed quote-then-answer grounding: labelled verbatim quotes are required and verified deterministically against original evidence. One bounded header-format repair is allowed; malformed grounding or any unverifiable quote terminally rejects the completion instead of publishing unchecked model text. |
| `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS` | `3` | Hard operator cap (1-5) on gate rewrite-and-retrieve rounds for every retrieval profile; `tief` requests up to this many, `standard` always uses one. |
| `INQTRIX_KNOWLEDGE_CONTEXTUALIZE` | `off` | Contextual retrieval: dynamic fast-tier batches of at most 25 contiguous chunks, bounded by the resolved model's complete prompt/output budget. Validated batches checkpoint; dependency/validation failures pause without publication, and a raw build requires an explicit user choice. Existing active generations are unchanged until a revision/rebuild publishes. |
| `INQTRIX_CONTEXTUALIZATION_CIRCUIT_COOLDOWN_SECONDS` | `60` | Cooldown for the tenant/provider/model contextualization circuit after a transient provider failure. Calls are rejected into a visible dependency pause during the window; this is not an indexing deadline and never publishes a fallback. |
| `INQTRIX_CONTEXTUALIZATION_CIRCUIT_PROBE_LEASE_SECONDS` | `900` | Minimum lease for the single half-open recovery probe shared by all durable workers. Runtime raises the effective lease above the provider call/retry budget; an expired lease can be reclaimed after a worker crash. |
| `INQTRIX_EMBEDDING_MODEL` | `text-embedding-3-small` | Default embedding model for new collections. Each collection stores its model immutably at creation. |

Further tuning: `INQTRIX_RERANK_CANDIDATE_DEPTH` (40 — pool retrieved before rerank reduces to top_k), `INQTRIX_KNOWLEDGE_TOP_K` (8; per-request override via `knowledge_filters.top_k`), `INQTRIX_EMBEDDING_PROVIDER` (`openai_compatible` or `azure`; `azure` reads `INQTRIX_EMBEDDING_AZURE_ENDPOINT` / `INQTRIX_EMBEDDING_AZURE_API_KEY` / `INQTRIX_EMBEDDING_AZURE_API_VERSION` (`2024-10-21`) with fallbacks to the established `AZURE_AI_PROJECT_ENDPOINT` / `AZURE_AI_PROJECT_API_KEY` / `AZURE_OPENAI_API_KEY` variables), `INQTRIX_EMBEDDING_BASE_URL` / `INQTRIX_EMBEDDING_API_KEY` (empty reuses `LITELLM_BASE_URL` / `LITELLM_API_KEY`), `INQTRIX_SELECTABLE_EMBEDDING_MODELS` (empty hides the collection-creation picker), `INQTRIX_KNOWLEDGE_CHUNK_MAX_CHARS` (2000), `INQTRIX_KNOWLEDGE_MAX_DOCUMENT_CHARS` (2000000).

Background indexing tuning: `INQTRIX_REINDEX_MAX_CONCURRENT` (6 — how many DIFFERENT collection-generation or document-revision jobs are admitted concurrently; a collection generation is serialized per collection while normal document revisions use their own CAS identity; in worker mode the indexing loop runs at the LOWER of this and `INQTRIX_WORKER_CONCURRENCY`, so this value bounds execution as well as admission), `INQTRIX_REINDEX_QUEUE_MAX_SIZE` (50 — waiting indexing jobs; full → 429), `INQTRIX_REINDEX_COMPLETED_TTL_SECONDS` (3600 — retention of terminal records only, in memory and PostgreSQL), `INQTRIX_REINDEX_HISTORY_LIMIT` (10 — terminal records kept per collection), `INQTRIX_REINDEX_AUTO_RESUME_ATTEMPTS` (5 — automatic resumes of one `paused_dependency` job in a row without progress, after waits that grow from 60 seconds to 15 minutes; `0` leaves every pause to a person; see [automatic resume](../architecture/knowledge-engine.md)), `INQTRIX_REINDEX_EVENT_BUFFER_SIZE` (200 — recent events retained for late SSE subscribers in the memory tier), and `INQTRIX_GENERATION_ROLLBACK_RETENTION_SECONDS` (604800 — rollback window for the prior validated generation). No setting or built-in age limit expires queued, running, cancelling, `paused_dependency`, or `paused_validation` work. Queue-backed delivery uses reconcile/reclaim plus attempt fencing after worker loss; a no-queue restart reconstructs paused work from the canonical operation identity before changing it to queued. If that identity is incomplete, the API returns the typed `resume_unavailable` conflict and leaves the checkpoint paused. Rebuilds validate document/revision identity, exact source spans and chunk/point counts, and embedding dimension before switching the Postgres pointer. Expired generations leave `rollback_available` before vector deletion starts; the existing worker reconciliation cadence resumes `deleting`/`cleanup_failed` work idempotently, so an interrupted cleanup is visible and never masquerades as a usable rollback generation.

**Interactions.** The embedding endpoint defaults to the Server block's LiteLLM gateway, so a standard proxy deployment needs no extra embedding configuration. The sparse branch (`INQTRIX_KNOWLEDGE_SPARSE=bm25_german`) requires a Qdrant server >= 1.15.3 (server-side BM25 inference): an EXTERNAL Qdrant older than that fails fast at startup with an actionable error naming both remedies (upgrade the server or set `INQTRIX_KNOWLEDGE_SPARSE=off`); when the server is unreachable at startup, the requirement is logged as a warning and re-checked on the next start — the bundled compose/Helm Qdrant (1.19.0) always satisfies it. `INQTRIX_RERANKER_PROVIDER=llm` runs through the deployment's own LLM provider (fast tier) — no rerank API contract needed, but roughly an order of magnitude costlier and slower than a cross-encoder, hard-capped at 20 candidates per query with a visible log line. `INQTRIX_PUBLIC_BASE_URL` (Server) turns knowledge citations into clickable `/v1/sources/...` links. The dev compose stack provides Qdrant. Combining an `http://` `INQTRIX_QDRANT_URL` with `INQTRIX_QDRANT_API_KEY` makes qdrant-client emit its insecure-connection `UserWarning` at startup — by design, because the key crosses the wire unencrypted. In-cluster plain HTTP constrained by a NetworkPolicy is an accepted posture (the warning is informational); for TLS set `INQTRIX_QDRANT_URL=https://…`. There is no Qdrant-specific CA-bundle setting, so a private-CA certificate must be trusted by the container's system trust store.

**When this block is OFF.** No knowledge or sources routes are registered, no embedding provider is constructed, and requests naming `mode=knowledge` get the standard mode-validation 400. The `/v1/capabilities` manifest reports the absence, so the research-desk UI hides the knowledge workspace instead of rendering dead controls.

**The two-level rule.** Knowledge env switches are the OPERATOR CEILING; the per-request retrieval profile (`schnell` | `standard` | `gruendlich` | `tief` | `auto`) selects within it. `INQTRIX_KNOWLEDGE_GATE=off`, `INQTRIX_KNOWLEDGE_GROUNDING=off`, `INQTRIX_RERANKER_PROVIDER=none`, and `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS` clamp every profile, and every clamp is visible (`degraded_stages` in events, result state, and `/v1/capabilities`). Full matrix and stage notes in [Retrieval profiles](knowledge-profiles.md).

**Availability states.** The knowledge store is opt-in and every state is
visible: (1) `INQTRIX_KNOWLEDGE_ENABLED=false` — no knowledge routes, no
collections in the Agent Desk `@` menu, no `/wissen`; `/v1/capabilities`
reports `features.knowledge=false`, `agent.source_controls[knowledge]`
with `available=false, reason_code=operator_disabled` and
`feature_status.knowledge` with `state: "disabled"` and the same reason;
the Agent Desk runs fully on web and model knowledge. Uploads are still
prepared by the server-side parser (`INQTRIX_DOCUMENT_PARSER`, Storage
block). The database view keeps existing vector indexes readable but offers
no way to create, fill or build one, and names the reason in the rail and
on the index instead of simulating a build. (2) Enabled, but the vector
store does not answer the availability probe — `features.knowledge=false`,
`reason_code=store_unreachable` (`feature_status.knowledge` reports
`state: "degraded"`); a run that asks for project knowledge
(chips, `scope: "all"`, `mode=knowledge`) is refused with HTTP 503
`knowledge_store_unavailable` before it costs anything, a running run whose
search hits the outage fails that tool call with a typed error and never
falls back to another source, and the composer greys the knowledge glyph
out with the reason. A page loaded during the outage shows the database
view read-only with that reason; a page opened before it keeps its server
connection and shows a build the outage paused as paused. (3) Enabled and
reachable — everything on. The probe
is the store's read-only availability check behind a short timeout, run
fresh (not cached) by every `/v1/capabilities` read and by the admission of
every explicit knowledge request (see
[webserver-mode.md](../deployment/webserver-mode.md)).

## Quota (`QuotaSettings`)

Per-user usage quotas for multi-user deployments — the operator-ceiling layer of a two-level rule: the admin UI sets the middle layer (a tenant default and per-user overrides) within these bounds. Each flow dimension has a `*_default` (the out-of-box per-user allowance) and a `*_max` (the hard ceiling no admin-set value may exceed). A value of `0` means UNLIMITED, so the all-zero default leaves every deployment byte-identical until an operator sets real numbers. Quotas apply ONLY when enabled and the active auth mode is one of the cookie-session modes (`oidc`, `local`); the anonymous/static principals are never metered.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_QUOTA_ENABLED` | `false` | Master switch. Even when on, quotas bind only in `oidc` or `local` mode — the single-operator `none`/`apikey` modes are never metered. Advertised in `/v1/capabilities` as `features.quota`. |
| `INQTRIX_QUOTA_RUNS_PER_MONTH` | `0` | Default native research runs a user may start per calendar month. `0` = unlimited. Checked exactly at submission. |
| `INQTRIX_QUOTA_RUNS_PER_MONTH_MAX` | `0` | Hard ceiling for the monthly-run allowance; no admin default or override may exceed it. `0` = no ceiling. |
| `INQTRIX_QUOTA_LLM_TOKENS_PER_MONTH` | `0` | Default LLM tokens (prompt + completion, summed across runs, chat, editor) per user per calendar month. `0` = unlimited. Recorded post-hoc: the current call finishes and the NEXT submission is blocked once the budget is reached. |
| `INQTRIX_QUOTA_LLM_TOKENS_PER_MONTH_MAX` | `0` | Hard ceiling for the monthly LLM-token allowance. `0` = no ceiling. |
| `INQTRIX_QUOTA_EMBEDDING_TOKENS_PER_MONTH` | `0` | Default embedding input tokens per user per calendar month (document ingestion). `0` = unlimited. Same block-next model as the other flow dimensions. |
| `INQTRIX_QUOTA_EMBEDDING_TOKENS_PER_MONTH_MAX` | `0` | Hard ceiling for the monthly embedding-token allowance. `0` = no ceiling. |
| `INQTRIX_QUOTA_STORED_BYTES` | `0` | Default object-store occupancy a user may hold, in bytes. A STOCK quota (rises on upload, falls on delete, never resets). `0` = unlimited. |
| `INQTRIX_QUOTA_STORED_BYTES_MAX` | `0` | Hard ceiling for per-user object-store occupancy in bytes. `0` = no ceiling. |
| `INQTRIX_QUOTA_MAX_TOKENS_PER_RUN` | `0` | Optional HARD per-run token budget. `0` = off. When set, a single run that crosses the budget mid-flight is cancelled gracefully at the next graph-node boundary (partial result returned, never a mid-call kill). Independent of the monthly token quota. Size it against the report profile's evidence budgets: every section call of the answer node carries the rendered evidence overview (`prompt_evidence_total_char_budget`, roughly a quarter as many tokens as characters) and every evaluation the compact view (`evaluate_evidence_total_char_budget`), so a DEEP run with six sections and three rounds needs several hundred thousand input tokens before its own searches count. |

**Interactions.** Enforcement reads the ACTIVE auth mode (so it follows `INQTRIX_AUTH_MODE=infer`), not the raw value. The admin UI sets the tenant default and per-user overrides within each `*_max`. Embedding-token runaway on a single ingestion is already bounded by `INQTRIX_KNOWLEDGE_MAX_DOCUMENT_CHARS` and `INQTRIX_MAX_FILE_BYTES`. See [Create and manage users](../deployment/create-and-manage-users.md) and [Authentication modes](../deployment/auth-modes.md).

**When this block is OFF.** With `INQTRIX_QUOTA_ENABLED=false` (default) or any non-cookie-session auth mode (`none`/`apikey`), nothing is metered and behaviour is byte-identical to deployments before quotas existed.

## Sharing (`SharingSettings`)

Resource-sharing policy for the cookie-session multi-user modes (`oidc`/`local`). Sharing is otherwise tenant-wide: any authenticated user may be a share target and the share typeahead searches the whole tenant.

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_SHARING_ENABLED` | `true` | Master switch for direct person-to-person sharing. `false` removes share controls and suspends every stored grant at the live authorization boundary without deleting or revoking it; a later `true` restores non-revoked grants. Effective sharing additionally requires PostgreSQL and a cookie-session auth mode in a durable multi-user deployment. |
| `INQTRIX_SHARING_RESTRICT_TO_WORKSPACE_MEMBERS` | `false` | When `true`, grant, accept, typeahead, and every live resource access require owner and recipient to share at least one workspace. Losing the last common workspace revokes pending and accepted shares in both directions; startup reconciles existing shares before readiness. Turning the setting off stops future workspace checks but never restores a revoked share. Default `false` keeps sharing tenant-wide. Only meaningful in cookie-session modes; `none`/`apikey` never mount sharing. |

**When this block is OFF.** `INQTRIX_SHARING_ENABLED=false` mounts no share
commands and denies stored grants without mutating their lifecycle state.
`INQTRIX_SHARING_RESTRICT_TO_WORKSPACE_MEMBERS=false` only means tenant-wide
target discovery while the master switch is on.

## Editor collaboration (`CollaborationSettings`)

Configures the optional PostgreSQL-backed live editor. The browser always uses
the same-origin `/collaboration` path; the HTTP and WebSocket URLs below are
private FastAPI-to-Node addresses. The complete topology and operating
procedure are in [Deploy editor collaboration](../deployment/editor-collaboration.md).

| Variable | Default | Effect |
|---|---|---|
| `INQTRIX_COLLABORATION_ENABLED` | `false` | Registers the public collaboration API, WebSocket gateway, PostgreSQL store, and private Node client. `true` fails startup when any required storage, auth, URL, or secret prerequisite is missing. |
| `INQTRIX_COLLABORATION_HTTP_URL` | *(empty)* | Private Node HTTP origin, for example `http://collaboration:1234`. Required when enabled and never returned to browsers. |
| `INQTRIX_COLLABORATION_WS_URL` | *(empty)* | Private Node WebSocket URL ending in `/collaboration`, for example `ws://collaboration:1234/collaboration`. Required when enabled. |
| `INQTRIX_COLLABORATION_SECRET` | *(empty)* | Bearer/HMAC secret shared only by FastAPI and Node. Required when enabled, at least 32 characters, and must differ from `INQTRIX_SESSION_SECRET`. |
| `INQTRIX_COLLABORATION_TENANT_ID` | `default` | Canonical tenant for the single-deployment Node service. FastAPI rejects every private state, update, command, snapshot, and maintenance request naming a different tenant. Configure the same value on API and Node. |
| `INQTRIX_COLLABORATION_ALLOWED_ORIGINS` | *(empty)* | Comma-separated additional absolute `http(s)` origins accepted by the WebSocket gateway. Same-origin remains implicit; entries may not contain paths, credentials, queries, or fragments. |
| `INQTRIX_COLLABORATION_LEASE_TTL_SECONDS` | `60` | Lifetime of a document-scoped browser lease. Range 15-300 seconds. |
| `INQTRIX_COLLABORATION_TOKEN_REFRESH_SECONDS` | `30` | Browser lease-refresh interval returned by the session API and honored by the collaboration controller. It must be lower than the lease TTL. |
| `INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS` | `15` | Lifetime of the single-writer Node fencing lease. Python validates 6-120 seconds, Node 5-300 — the narrower Python bound is what you get, because the API process refuses to start outside it. |
| `INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS` | `5` | Fencing-lease renewal interval; must be lower than the instance lease. Range 1-60 seconds. |
| `INQTRIX_COLLABORATION_PROVIDER_FLUSH_MS` | `50` | Browser update-batching interval returned by the session API and honored by the collaboration controller: the delay before a local update is sent while no earlier update waits for its acknowledgement. Range 10-1000 ms. |
| `INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS` | `5` | Idle interval after which Node stores a verified snapshot. Range 1-300 seconds. |
| `INQTRIX_COLLABORATION_SNAPSHOT_UPDATE_COUNT` | `256` | Durable tail update count that triggers a snapshot. |
| `INQTRIX_COLLABORATION_SNAPSHOT_TAIL_BYTES` | `1048576` | Durable update-tail byte size that triggers a snapshot (1 MiB by default). |
| `INQTRIX_COLLABORATION_MAX_FRAME_BYTES` | `2097152` | Maximum public/private binary WebSocket frame (2 MiB). Uvicorn and the bundled gateway enforce it before ASGI materializes a frame; relay checks remain as defense in depth and close oversize messages with code `1009`. The session API returns it to the browser, which keeps a merged update within an eighth of this value or of `INQTRIX_COLLABORATION_MAX_QUEUED_BYTES`, whichever is smaller, and within 256 KiB. |
| `INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES` | `32` | Maximum inbound frame queue per physical WebSocket. Uvicorn, the bundled gateway, Python upstream clients, and Node share this validated 1-256 frame bound. On an open document connection with edit or suggest access, Node counts only document frames against it and holds up to four times as many further frames in arrival order; more frames, or more bytes than `INQTRIX_COLLABORATION_MAX_QUEUED_BYTES`, close the socket with code `4429` (`ingress_overflow`). A document connection that is not open yet and a view connection get no such allowance and close with `4429` (`rate_limited`). Node also bounds the frames it has reserved toward one socket but not yet written by the same value. The session API returns it to the browser, which keeps the updates it sent and has not seen acknowledged at least four below it when a hidden or closing page sends its waiting updates at once; collaboration needs a value of at least 5, because a reconnect sends up to four control frames before its first resent update. |
| `INQTRIX_COLLABORATION_MAX_QUEUED_BYTES` | `8388608` | Maximum aggregate inbound bytes Node reserves across one physical socket before Hocuspocus processing (8 MiB by default), including frames held back for an open connection and the latest waiting awareness state. Exceeding the budget closes with code `4429` before another frame copy is enqueued. Node also bounds the bytes it has reserved toward one socket but not yet written by the same value. The API validates the same 65536-67108864 byte range and the session API returns the value to the browser, which keeps its unacknowledged updates, together with what a hidden or closing page sends at once, 16 KiB below the smaller of this value and `INQTRIX_COLLABORATION_MAX_FRAME_BYTES`. |
| `INQTRIX_COLLABORATION_MAX_DOCUMENT_BYTES` | `10485760` | Maximum Markdown/Yjs document accepted for conversion or projection (10 MiB); larger requests fail with HTTP 413. |
| `INQTRIX_COLLABORATION_MAX_SESSIONS_PER_USER_DOCUMENT` | `5` | Concurrent lease/session cap for one user and document. |
| `INQTRIX_COLLABORATION_SESSION_RATE_PER_MINUTE` | `30` | Session issuance/rotation limit per user per minute. |
| `INQTRIX_COLLABORATION_UPDATE_RATE_COUNT` | `120` | Update-frame allowance per connection in the configured rate window. The session API returns it and its window to the browser, which spaces its updates to at most four fifths of the allowance (at least one per window) and leaves eight of it unused when a hidden or closing page sends its waiting updates at once. |
| `INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS` | `10` | Window for `INQTRIX_COLLABORATION_UPDATE_RATE_COUNT`. |
| `INQTRIX_COLLABORATION_AWARENESS_RATE_PER_SECOND` | `20` | Awareness-frame allowance per connection per second. |
| `INQTRIX_COLLABORATION_UPDATE_PAYLOAD_RETENTION_SECONDS` | `86400` | Minimum age before snapshot-covered binary update payloads may be pruned (24 hours). Attribution metadata is retained separately. |
| `INQTRIX_COLLABORATION_ACTIVITY_RETENTION_SECONDS` | `7776000` | Retention for update attribution and decision metadata (90 days). |
| `INQTRIX_COLLABORATION_TOMBSTONE_RETENTION_SECONDS` | `7776000` | Delay before physical cleanup of a tombstoned collaboration document (90 days). Version 1 has no restore UI. |
| `INQTRIX_COLLABORATION_PROTOCOL_VERSION` | `1` | Expected stateless/binary protocol version. A client mismatch becomes read-only and reports an update-required state. |
| `INQTRIX_COLLABORATION_SCHEMA_VERSION` | `2` | Expected shared editor schema version. Browser, Node, and stored document must match. Version `2` adds reversible structure proposals. |

The Node process reads a small additional process-level surface. Give it only
these values and the shared collaboration limits above; do not pass the API's
database, session, provider, or object-store credentials into Node.

| Variable | Default | Effect |
|---|---|---|
| `INQTRIX_API_INTERNAL_URL` | *(required)* | Private FastAPI origin used for lease introspection, durable persistence, policy events, and compaction. |
| `INQTRIX_COLLABORATION_BIND_ADDRESS` | `0.0.0.0` | Node bind address inside its private service network. This is not a browser URL. |
| `INQTRIX_COLLABORATION_PORT` | `1234` | Node HTTP and WebSocket listen port. Bundled deployments expose it only through a private service. |
| `INQTRIX_COLLABORATION_HTTP_TIMEOUT_MS` | `5000` | Timeout for one Node-to-FastAPI internal HTTP request. Range 100-30000 ms. |
| `INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES` | `4194304` | Maximum WebSocket `bufferedAmount` accepted before another inbound frame is processed (4 MiB by default). Exceeding it closes with code `4429`. An outbound frame that would push a socket's buffered data beyond it closes that slow receiver with `4429` (`rate_limited`) instead of queuing more for it. |
| `INQTRIX_COLLABORATION_POLICY_POLL_MS` | `2000` | Interval for polling existing `user_events` revocations. Range 250-60000 ms. |
| `INQTRIX_COLLABORATION_POLICY_REVALIDATION_TIMEOUT_MS` | `7500` | Maximum time an affected connection may remain pending during policy revalidation before Node closes it. Range 1000-60000 ms. |
| `INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS` | `60` | Interval for fenced global compaction and retention maintenance. Range 5-86400 seconds; it runs independently of document snapshot activity. |
| `INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS` | `1000` | Initial delay for autonomous retry of a failed eligible snapshot. Range 100-60000 ms. |
| `INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS` | `30000` | Cap for snapshot retry exponential backoff. Range 100-300000 ms and must be at least the base delay. |
| `INQTRIX_COLLABORATION_TENANT_ID` | `default` | Canonical tenant sent on every private persistence call and checked against lease introspection. Version 1 runs one tenant per deployment, matching the current `Principal` model. |

**Interactions.** Every collaboration session returns
`INQTRIX_COLLABORATION_MAX_FRAME_BYTES`,
`INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES`,
`INQTRIX_COLLABORATION_MAX_QUEUED_BYTES`,
`INQTRIX_COLLABORATION_UPDATE_RATE_COUNT`, and
`INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS` to the browser, which paces
and bounds what it sends by them. The API and Node therefore must read the same
values: the bundled Compose file passes each of them to both services with the
same default, and the Helm chart feeds both from `config`/`extraConfig`. A
browser that receives a value outside the range the service validates stops
with an incompatibility notice instead of guessing; a value the session does
not return falls back to the service default, with one warning in the browser
console. Collaboration requires `INQTRIX_STORAGE_BACKEND=postgres` and a
cookie-session `INQTRIX_AUTH_MODE` (`local` or `oidc`). Anonymous, static-key,
and PAT principals cannot obtain a document lease. The feature uses the
existing PostgreSQL database and `user_events`; it does not require SQLite,
Redis/Valkey, a second database, or a Node data volume. Version 1 is fixed to
one Node replica.

**When this block is OFF.** The public collaboration routes and WebSocket
gateway are absent. Legacy Markdown documents behave unchanged. Existing
collaboration documents retain their binary state in PostgreSQL and are
limited to their last stored Markdown projection in read-only form; no legacy
body autosave is substituted.

## Editor guest links (`EditorGuestLinkSettings`)

Account-less editor access is a separate, opt-in enterprise module. It never
turns on merely because direct sharing or live collaboration is available.

| Variable | Default | Effect |
|---|---|---|
| `INQTRIX_EDITOR_GUEST_LINKS_ENABLED` | `false` | Enables password-protected, expiring `/s/{token}` links. Startup fails loudly unless direct sharing and collaboration are enabled, storage is PostgreSQL, auth is cookie based, `INQTRIX_VALKEY_URL` is configured, and `INQTRIX_PUBLIC_BASE_URL` uses HTTPS (or the explicit insecure-HTTP opt-in below is set). Turning it off invalidates guest leases at their next command/lease check and preserves link rows for audit/reactivation. |
| `INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP` | `false` | Development escape hatch: allows guest links behind a plain-HTTP `INQTRIX_PUBLIC_BASE_URL` and drops the `Secure` flag from the guest cookies. Guest tokens, passwords, and sessions then cross the wire unencrypted — never use in production; activation logs a WARNING at startup. An absolute http(s) base URL stays required (the guest Origin check derives from it). |
| `INQTRIX_EDITOR_GUEST_LINK_STATS_ENABLED` | `true` | Records bounded successful-open, distinct-session, and last-access values. Disabling it does not disable the password throttle or security audit. |
| `INQTRIX_EDITOR_GUEST_LINK_TOKEN_SECRET` | *(empty)* | Independent 32+ character HMAC secret for token digests. It must differ from both session and collaboration secrets. Plain link tokens are never persisted. |
| `INQTRIX_EDITOR_GUEST_LINK_DEFAULT_TTL_SECONDS` | `604800` | Default lifetime (7 days), constrained to one hour through the configured maximum. |
| `INQTRIX_EDITOR_GUEST_LINK_MAX_TTL_SECONDS` | `2592000` | Operator ceiling for link lifetime, at most 30 days. |

Each created link uses a 256-bit token and an independently generated password.
The password is Argon2id-hashed, the token is stored as an HMAC digest, and
either value is shown in clear text only on creation or on its own rotation.
Both halves rotate independently: rotating the password keeps the URL and
invalidates the old password, while rotating the token issues a new URL and
stops the old one from resolving without touching the password — so a leaked
link does not require creating a second link and leaving the first one
alive. Replacing either half also ends every guest session opened with the
previous pair, including an unlock that checked the previous pair but had not
opened its session yet: the session opens only if the link still carries the
pair the password was checked against. Password attempts are throttled through
Valkey so every API replica observes the same lockout. Guest cookies are
`Secure`, `HttpOnly`, `SameSite=Lax`; mutation routes additionally enforce
CSRF and exact-origin checks. Guest pages use `no-store` and `no-referrer`,
and both web/API logging redact token-bearing paths before they reach console
or file handlers.
A guest has no account preference, so a guest page opens in the language last
chosen on that device, otherwise in the first supported language (German or
English) the browser prefers, otherwise in English. Signed-in pages keep
following the account preference.

**When this block is OFF.** Owner controls and public routes are absent.
Unrevoked link records remain stored, but active guest leases fail live
revalidation and no retry loop is started. Signed-in collaboration continues
unchanged.

## Agent platform (`AgentPlatformSettings`)

Workspace-agent limits (`mode=workspace_agent`), enforced server-side:

| Variable | Default | Meaning |
|---|---|---|
| `INQTRIX_AGENT_ENABLED` | `true` | Master switch; even when on, the mode registers only with a durable checkpointer (Postgres) or the volatile escape below. |
| `INQTRIX_AGENT_KERNEL_ENABLED` | `true` | Switch for `mode=agent_kernel`. Additionally gated on the checkpointer rule, deepagents availability, and native tool calling on the default LLM; a failed gate logs a WARNING and publishes its reason under `feature_status.agent_kernel`. Set to `false` to run the phase machine instead. |
| `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS` | `121` | Initial cumulative checkpointed graph-step allowance for a normal kernel run. The answer turn costs 9 super-steps and every tool turn 8 (measured, contract-pinned), so 121 buys 14 sequential tool turns plus the answer — enough for a research child, a follow-up search, the todo bookkeeping and a document. Reaching it parks for an explicit decision. |
| `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_DEEP` | `121` | Initial Deep step allowance: the same 14 sequential tool turns plus the answer; Deep buys more tool calls and the verification pass, not more turns. |
| `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_EXTENSION_CEILING` | `241` | Operator ceiling for an explicitly extended normal step allowance. It cannot reduce the configured base. |
| `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_EXTENSION_CEILING_DEEP` | `241` | Operator ceiling for an explicitly extended Deep step allowance. |
| `INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS` | `30` | Initial run-wide normal tool-call allowance. Counts are derived from checkpointed AI messages across park/resume; an overflowing batch executes no tools and parks for a decision. |
| `INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_DEEP` | `60` | Initial Deep tool-call allowance with the same checkpoint-derived, whole-batch rejection contract. |
| `INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_EXTENSION_CEILING` | `60` | Operator ceiling for an explicitly extended normal tool-call allowance. |
| `INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_EXTENSION_CEILING_DEEP` | `120` | Operator ceiling for an explicitly extended Deep tool-call allowance. |
| `INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE` | `6` | Subject-independent part of an internal role run's tool budget (index read and orientation). How the budget splits between this part and the rate below is an estimate, so the two are tuned together. |
| `INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE` | `1.5` | Tool calls a role may spend per thing it works through (one output, one evidence reference, one released source). The rate is a starting estimate, not a measured distribution. The role's budget is `base + rate x sources`, never below the normal allowance and never above the allowance of the Stufe that commissioned it. |
| `INQTRIX_AGENT_KERNEL_SUFFICIENCY_GATE` | `true` | Advisory evidence-sufficiency judge: after enough successful source-tool calls, one cheap fast-tier verdict nudges the model to answer now or search only the named gaps. Advisory — tool budget and recursion stay the hard stops; `schnell` runs never judge. |
| `INQTRIX_AGENT_KERNEL_SUFFICIENCY_MIN_TOOL_CALLS` | `3` | Successful web/knowledge calls before the first judgement of a normal run; simpler runs never pay a judge call. |
| `INQTRIX_AGENT_KERNEL_SUFFICIENCY_MIN_TOOL_CALLS_DEEP` | `5` | The deep-run threshold — Deep buys more evidence work before the first verdict. |
| `INQTRIX_AGENT_KERNEL_SUFFICIENCY_MAX_JUDGEMENTS` | `2` | Run-wide judgement cap, counted via flagged nudge messages in the checkpointed transcript (idempotent across park/resume). |
| `INQTRIX_AGENT_KERNEL_CONTEXT_TRIGGER_TOKENS` | `0` | Explicit kernel compaction threshold in tokens; `0` derives it from the resolved model's card (`context window x trigger fraction`, floor 128k; unknown models use the floor). Compaction is visible (`inqtrix.agent.context.compacted` + narration) and archives the evicted history in the run's `context_archive` artifact. |
| `INQTRIX_AGENT_KERNEL_CONTEXT_TRIGGER_FRACTION` | `0.75` | Fraction of the model context window that arms compaction when no explicit token pin is set. |
| `INQTRIX_AGENT_KERNEL_CONTEXT_KEEP_MESSAGES` | `12` | Newest transcript messages kept verbatim per compaction; everything older is summarized (rarely-and-largely beats often-and-small for prompt-cache stability). |
| `INQTRIX_AGENT_KERNEL_CONTEXT_TOOL_RESULT_OFFLOAD_CHARS` | `8000` | Bulk tool results above this many characters are archived in full (run `context_archive`, readable via `read_canvas`) and replaced in-context by a digest plus ALL citable reference lines; `0` disables the offload. |
| `INQTRIX_AGENT_DEFAULT_MODE` | `agent_kernel` | Which algorithm the Agent Desk submits by default (`workspace_agent` or `agent_kernel`, validated at startup); capabilities publish the EFFECTIVE value and fall back to `workspace_agent` while the independent kernel registration gate fails, naming that gate under `feature_status.agent_kernel`. |
| `INQTRIX_AGENT_DEFAULT_AUTONOMY` | `balanced` | Permission mode when a request names none: `strict` / `balanced` / `autonomous`. |
| `INQTRIX_AGENT_MAX_PARALLEL_CHILDREN` | `8` | Width of one independent Agent Desk task wave and maximum child-research submissions per wave. Defaults to the validated plan ceiling `INQTRIX_AGENT_MAX_PLAN_TASKS` (`8`), so a plan the validator accepts also runs in ONE wave; below it a wave serialises. Provider-specific gates remain the final concurrency ceiling. |
| `INQTRIX_AGENT_MAX_PARALLEL_QUERIES_PER_TASK` | `3` | Concurrent sub-queries within ONE retrieval task, and concurrent window calls within ONE document analysis (`1` restores the sequential loop). Worst case is `INQTRIX_AGENT_MAX_PARALLEL_CHILDREN` x this value concurrent model calls from a single run, which must fit inside `MAX_CONCURRENT` with room for other traffic — startup warns otherwise. |
| `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_DOCUMENTS` | `3` | Documents ONE `file_analysis` task reads completely, in search order from what its questions found; further found documents are named on the task as not analysed. Published as `agent.limits.mission.documents_per_analysis`. |
| `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS` | `40` | Windows (about 19,000 new characters each, one model call each) ONE `file_analysis` task analyses at most — 40 is about 760,000 characters, above the largest measured document. Bounds a task's cost; the range past it is named as not analysed. Published as `agent.limits.mission.document_windows_per_analysis` and stated to the planner. |
| `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS_PER_RUN` | `80` | Windows ALL `file_analysis` tasks of one mission analyse together, across waves, replans and parks. What the budget leaves unread is named on the task. Published as `agent.limits.mission.document_windows_per_run`. |
| `INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS` | `8` | Concurrent memo sections within ONE mission synthesis (`1` restores the sequential loop; 8 matches the plan-task ceiling, so a typical outline is written in one wave — a measured nine-section memo took three waves of four). Sections are independent by construction and are assembled, flushed and narrated in outline order, so the memo grows in reading order; the synthesis runs after the task waves and never adds to the wave worst case above. |
| `INQTRIX_AGENT_CHECKPOINTER_POOL_SIZE` | `4` | Server connections the agent checkpointer's own psycopg pool may hold, process-wide, shared by every concurrent agent run. One source, three displays: the pool, the startup connection-budget line, and the admin runtime endpoint all read this value. Read by api and worker (`config:`/`extraConfig:` on Kubernetes). Raise only when a measurement shows pool-wait under agent target load — this pool adds to the engine budget, it never replaces it. |
| `INQTRIX_AGENT_DISCOVERY_MAX_TOOL_CALLS` | `15` | Hard probe budget of the read-only discovery phase. |
| `INQTRIX_AGENT_MAX_PLAN_TASKS` | `8` | Task ceiling the plan validator enforces (agent plans AND user edits). The planner is told the ceiling in the first plan and the remaining headroom in every replan prompt. |
| `INQTRIX_AGENT_MAX_REPLAN_ROUNDS` | `2` | Additive replan rounds before the run proceeds as-is. A delegated mission (kernel child) opens at most ONE critic research round regardless — the parent's deep-verify pass is its second check. |
| `INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS` | `720` | Active (non-waiting) seconds a mission may spend before the critic opens no further research round. Reached, the run continues visibly (`inqtrix.agent.limit.reached`, kind `mission_time_budget`, activity "Zeitbudget erreicht") and the memo closes with the critic's remaining findings under "Offene Punkte". Gate waits do not count. `0` disables the budget. |
| `INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET` | `160` | Reference count above which the synthesis PROMPT digest is capped to the top-ranked references (deterministic source-tier/corroboration/excerpt rank, no LLM call). The citation ledger is never truncated; `0` disables the cap. A cap that fires logs a WARNING and raises the `inqtrix.truncation` span event with `limit_name=synthesis_evidence_budget`. |
| `INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET` | *(unset)* | Operator override for how many references a memo section lists as a one-line register beside the ones its outline entry preselected. Unset, the run's Stufe decides: `schnell` 20, `gruendlich` 60, `tief` 180, and 60 for a run that names no Stufe (the `section_register` column of the Stufe table, published under `agent.tiers`). A number replaces every Stufe's width and is published in its place. The preselected references are rendered in full and are never cut; the register bounds what a section may additionally cite; a cut logs a WARNING and raises the `inqtrix.truncation` span event with `limit_name=section_register`. `0` switches the register off, which brings back the state in which an outline that preselected nothing left the section believing the run had no evidence — the section still reports itself as `section_without_preselection` then. Deliberately separate from `INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET`: a register line costs a fraction of a full-text entry. |
| `INQTRIX_AGENT_MAX_CLARIFICATION_ROUNDS` | `2` | User-question rounds before the agent proceeds on its best assumption, named in the transcript and the plan's assumptions. A Stufe caps it tighter, never wider (`schnell` 0, `gruendlich` 1, `tief` 2); the effective number is published under `agent.tiers` and in the run snapshot's `limits.clarification_rounds`. |
| `INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW` | `true` | Permit one instant web search during discovery. Only effective in `autonomous` mode — Standard keeps all web contact behind the plan gate — and only when the assignment needs the web and the question is the user's own (a delegated mission sends none). |
| `INQTRIX_AGENT_ADVANCED_AUTONOMY` | `false` | Show the legacy three-way autonomy control (incl. `strict`) in the UI instead of the two-mode Standard/Auto toggle. Wire vocabulary is unchanged either way. |
| `INQTRIX_AGENT_ALLOW_VOLATILE` | `false` | Dev escape: register without Postgres using an in-memory checkpointer (interrupted runs die with the process; logged loudly, `workspace_agent_durable: false`). |
| `INQTRIX_AGENT_SKILLS_MAX_ATTACHED` | `3` | How many skills one run may attach (`skill_ids`); the runs router rejects excess with a loud 400. Published as `agent.skills.max_attached`. |
| `INQTRIX_AGENT_SKILLS_DISCLOSURE_BUDGET_CHARS` | `4000` | Character budget of the model-facing skill disclosure block; overflow is a visible "… and N more" line, never a silent cut. Published as `agent.skills.disclosure_budget_chars`. |
| `INQTRIX_AGENT_MEMORY_PROVIDER` | `none` | Long-term workspace-agent memory provider: `none` or `mem0`. `none` keeps deployments memory-free by default. |
| `INQTRIX_AGENT_MEMORY_MODE` | `candidate_only` | Learning mode: `off`, `candidate_only`, or `auto_safe`. `auto_safe` currently degrades visibly to `effective_mode=candidate_only`; no automatic retain happens yet. |
| `INQTRIX_MEM0_BASE_URL` | empty | Base URL for a self-hosted Mem0 API when `INQTRIX_AGENT_MEMORY_PROVIDER=mem0`. |
| `INQTRIX_MEM0_API_KEY` | empty | Optional server-side Mem0 API key. This value is never accepted from clients. |

Requires the `agent` dependency extra; deployments should also set
`LANGGRAPH_STRICT_MSGPACK=true`. See
[Agent platform](../architecture/agent-platform.md).

Long-term memory requires a real per-user principal (`oidc_session` or
PAT). It stays disabled for anonymous and legacy static-key principals to
avoid shared anonymous memory. Memory endpoints derive `(tenant_id, user_id)`
from auth and reject owner fields in the request body or query string.

## Observability (`ObservabilitySettings`)

Trace-sink selection. `INQTRIX_TRACING` decides WHERE spans go; the agent
group's `OBSERVABILITY_PROFILE` keeps deciding HOW DEEP capture goes. Off by
default; every non-`off` value requires the `observability` dependency extra
(a missing extra logs one WARNING and tracing stays off — never a crash).

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_TRACING` | `off` | Trace sink. `off` = no tracer. `local` = spans exist only so JSON log lines gain `trace_id`/`span_id`; nothing is persisted, nothing leaves the process. `file` = spans additionally spool as standard OTLP-JSON lines (one `ExportTraceServiceRequest` per line — directly replayable into Langfuse or any OTLP/HTTP-JSON endpoint later). `otlp` = live export via OTLP over HTTP/protobuf. |
| `INQTRIX_TRACE_SAMPLE_RATE` | `1.0` | Head-sampling ratio (`ParentBased` + `TraceIdRatioBased`, decides per whole trace). The Langfuse SDK variable `LANGFUSE_SAMPLE_RATE` does NOT apply to pure OTLP export — this knob is authoritative. |
| `INQTRIX_TRACE_SPOOL_DIR` | `logs/traces` | Spool directory for `file` mode (created automatically). |
| `INQTRIX_TRACE_SPOOL_MAX_MB` | `2048` | Total size cap for the spool directory; oldest spool files rotate out first (size-based backstop — time-based retention is the live backend's job). |
| `INQTRIX_TRACE_CONTENT` | `auto` | Whether spans carry CONTENT (prompts/messages, redacted raw responses, search answers, kernel tool inputs and results) in addition to metadata. `auto` follows `OBSERVABILITY_PROFILE` (content only in `forensic`); `on`/`off` override explicitly. An incognito chat (`"incognito": true`) records no content whatever this says. Content always passes the existing sanitizers and the per-attribute byte cap — Langfuse OSS applies no server-side truncation or masking, so this side is the only guard. |
| `INQTRIX_TRACE_SCORES` | `true` | Export existing Research, Mission Critic and Deep-review signals as Langfuse scores after successful result publication. Uses the existing OTLP target and credentials, asynchronously through the tracing lifecycle. Active only with a Langfuse OTLP sink; no additional model calls. Missing measurements stay absent, and scores remain algorithm judgements rather than independent semantic verification. |
| `INQTRIX_TRACE_MAX_ATTR_BYTES` | `1048576` | Per-attribute byte **backstop** for span content — sized so it does not fire in normal operation (a cap that trims real prompts would defeat forensic capture). The OTel SDK sets no attribute-length limit and Langfuse only warns past 16 MB per span; the real constraints are proxy body size and storage. Every capped value raises an `inqtrix.truncation` span event, so the rare cap is visible — seeing one routinely means the value is too low, not that truncation is normal. |
| `INQTRIX_TRACE_UI_URL` | *(empty)* | Browser base URL of the Langfuse UI for the admin "open trace" deep link (joined with the `htmlPath` the trace API returns — no projectId configuration needed). Usually differs from the in-cluster OTLP endpoint, e.g. `http://localhost:3300` for the bundled compose profile. Empty hides the link. |
| `INQTRIX_TRACE_RETENTION_DAYS` | `30` | Time-based trace retention in Langfuse, enforced by Inqtrix's own worker cleanup job (native Langfuse retention is EE-only self-hosted): traces older than this are batch-deleted via the Langfuse API every 6 hours. `0` disables the job. Only active with `INQTRIX_TRACING=otlp`; the file spool uses its size cap instead. |
| `INQTRIX_AUDIT_SERVICE_STARTS` | `true` | Enriches the service-start index the admin panel drills into. Concretely: it gates the `chat.completed` rows entirely, and the metadata block (mode, duration, token sums) attached to run terminal rows. It does NOT suppress the run and indexing terminal rows themselves — those belong to the mandatory resource-effect trail and, like audit as a whole, have no off switch. Content is never recorded either way. |
| `INQTRIX_AUDIT_RETENTION_DAYS` | `365` | Time-based retention for `audit_log`, enforced daily by the SECURITY DEFINER function `audit_prune` (the app role itself holds INSERT/SELECT only). 365 days matches ISO 27001 / BSI OPS.1.1.5 guidance; `0` keeps rows forever. **The prune job runs in the `inqtrix-worker` process only** — see the retention note below. |
| `INQTRIX_USAGE_RETENTION_DAYS` | `730` | Time-based retention for the `llm_usage` consumption ledger (per-call token rows written by the provider wrappers), enforced daily by the worker via the SECURITY DEFINER function `llm_usage_prune` (the app role holds INSERT/SELECT only). 730 days = 24 months of product data; `0` keeps rows forever. Costs are never stored — `GET /v1/usage` and `GET /v1/admin/usage` derive them at read time from the model and embedding cards, and always report the unpriced remainder (self-hosted embeddings and per-call web search have no token list price) alongside the total. |

### Compose-only variables for the bundled Langfuse profile

These are read by `deploy/compose/compose.stack.yaml` (profile
`observability`) and by Langfuse itself — never by Inqtrix code. They only
matter when you run the BUNDLED trace backend; connecting an EXISTING Langfuse
needs nothing from this table (only `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS` and `INQTRIX_TRACE_UI_URL` above).

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_LANGFUSE_PORT` | `3300` | Host port for the Langfuse UI (bound to `127.0.0.1`). Also anchors `NEXTAUTH_URL`. |
| `LANGFUSE_NEXTAUTH_URL` | `http://localhost:${INQTRIX_LANGFUSE_PORT}` | URL the browser actually uses for the Langfuse UI; set it when the UI is reached under a different address. |
| `LANGFUSE_DATABASE_URL` | *(empty)* | REQUIRED when the bundled PostgreSQL is not used: Langfuse's own database URL. The deploy preflight fails without it in external-database setups. |
| `LANGFUSE_PG_DB` | `langfuse` | Database the provisioning job creates for Langfuse inside the bundled PostgreSQL. Ignored when `LANGFUSE_DATABASE_URL` is set. |
| `LANGFUSE_INIT_ORG_ID` / `LANGFUSE_INIT_ORG_NAME` | `inqtrix` / `Inqtrix` | Headless org bootstrap (idempotent). |
| `LANGFUSE_INIT_PROJECT_ID` / `LANGFUSE_INIT_PROJECT_NAME` | `inqtrix` / `Inqtrix` | Headless project bootstrap. |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY` | *(empty)* | Pre-generated project keys. The SAME pair must appear base64-encoded in `OTEL_EXPORTER_OTLP_HEADERS` — that is what wires Inqtrix to the bundled instance. |
| `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_NAME` / `LANGFUSE_INIT_USER_PASSWORD` | `admin@example.com` / `Admin` / *(empty)* | First UI login. The password has no default and belongs in the secrets file. |
| `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY` | *(empty)* | Langfuse's own mandatory secrets (session signing, hashing, at-rest encryption), passed into the containers as `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY`. Set the `LANGFUSE_*` names: the Langfuse services declare no `env_file`, so the container-side spellings never reach them. Secrets file only; `LANGFUSE_ENCRYPTION_KEY` must be exactly 64 hex characters. |
| `LANGFUSE_S3_BUCKET` | `langfuse` | Raw-event bucket, passed into the containers as `LANGFUSE_S3_EVENT_UPLOAD_BUCKET` and created by an init job. It reuses the stack's S3/SeaweedFS: endpoint, region and credentials come from `INQTRIX_S3_ENDPOINT_URL`, `INQTRIX_S3_REGION`, `INQTRIX_S3_ACCESS_KEY` and `INQTRIX_S3_SECRET_KEY` — there is no separate `LANGFUSE_S3_*` credential pair. |
| `LANGFUSE_CLICKHOUSE_PASSWORD` | *(empty)* | Password for Langfuse's analytical store; required for the `observability` profile. `CLICKHOUSE_URL` (`http://clickhouse:8123`) and `CLICKHOUSE_USER` (`langfuse`) are hard-wired in the Compose file and not operator-settable. ClickHouse ships as a dedicated container; expect roughly +6 CPU / +16 GiB and a ≥100 GB volume. |
| `LANGFUSE_VALKEY_PASSWORD` | *(empty)* | Password for Langfuse's own Valkey queue. REQUIRED for the `observability` profile — the container refuses to start without it. Secrets file only. |

The commented blocks in [`deploy/.env.stack.example`](../../deploy/.env.stack.example)
(the non-secret names) and
[`deploy/.env.stack.secrets.example`](../../deploy/.env.stack.secrets.example)
(the secrets) carry these variables with copy-paste defaults.

**Where retention actually runs.** All four prune jobs (traces, `audit_log`,
`llm_usage`, and the object-orphan sweep — see
`INQTRIX_OBJECT_ORPHAN_GRACE_HOURS` in the storage table) are daemon threads inside the
`inqtrix-worker` process. A deployment WITHOUT a worker
(`INQTRIX_QUEUE_BACKEND=memory`, the default) keeps audit and ledger rows
indefinitely regardless of these settings — the retention values then describe
an intent, not an enforced policy. Run a worker, or prune externally on a
schedule via the same SECURITY DEFINER functions. The admin status panel
reports this as `retention_enforced`.

**Ledger vs. quota — known, deliberate divergences.** Quota stays the
enforcement authority and books one aggregate number per request/run; the
ledger books one row per provider call. They agree for the LLM call paths
(both read the same provider-reported token counts), but three cases differ
by design:

- **Embeddings**: neither side gets provider-reported usage — the
  `EmbeddingProvider` contract returns vectors only. Both therefore book
  the SAME estimate (`ceil(len(text)/4)`) over the same texts, so quota
  and ledger agree here by construction; what differs from the LLM paths
  is the source of the number, not its value. Ledger rows for
  `operation=embeddings` carry that estimate in `input_tokens` and no
  output tokens, and cost derives from the embedding card's single input
  price.
- **Abandoned streams**: a streamed chat that times out books nothing to
  quota (a documented, deliberate gap) while the ledger still records the
  provider calls that ran. The ledger is the more complete record here.
- **Indexing**: contextualization LLM calls appear in the ledger under
  `feature=indexing`; quota books only the embedding dimension for indexing,
  never `llm_tokens`. Ledger totals for indexing therefore exceed quota.

Every LLM call (all engines — research nodes, agent kernel, knowledge,
chat, editor), every web-search call, and every embedding batch is
instrumented at ONE wrap point around the provider interfaces: spans
follow the GenAI conventions (`gen_ai.operation.name` chat /
text_completion / embeddings — Langfuse renders them as generations),
errors and timeouts land on the span, and the response DTOs now carry a
measured `duration_ms`. A grounded web search is exported as a generation
too (`langfuse.observation.type=generation`, the search engine as
`gen_ai.request.model`, the provider's token usage in `gen_ai.usage.*`):
the search agent's tokens are billed and quota-booked like any other call,
and only `gen_ai.usage.*` reaches the Langfuse totals. Every call span also
names its call site (`inqtrix.purpose`, mirrored to the filterable
`metadata.purpose`), its run (`inqtrix.run_id`) and, for mission tasks, its
task (`inqtrix.task_id`, `inqtrix.task_kind`). Langfuse skips usage on the
`invoke_agent` root spans, so the run's token count is exported as
trace metadata (`langfuse.trace.metadata.usage_prompt_tokens` /
`usage_completion_tokens`) next to `mode`, `depth`, `tier`, `profile`,
`stack` and `usage_scope`; the trace total shown by Langfuse is the sum of
the generations. `usage_scope` says what the pair counts: `run` for an
engine that keeps a cumulative count, `segment` for one that does not — and
a parked run has several segments, so there the metadata is a part of the
spend. It is not the quota's number either: the quota books the segment
delta after every segment, which is what keeps it from charging an earlier
segment twice.
Search cost needs a Langfuse model definition for the engine id (Project
Settings → Models); without one the row shows usage but no cost, consistent
with the ledger, which prices web-search rows as unpriced. With tracing
`off` the wrappers degrade to pure duration measurement.

The OTLP endpoint and headers come from the STANDARD OpenTelemetry
variables — `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`,
or their signal-specific forms `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_TRACES_HEADERS`, which take PRECEDENCE. They are never
mirrored into an `INQTRIX_*` name, and Inqtrix does not only hand them to
the exporter: the trace-export reader, the trace-retention job, and the
deploy preflight resolve the traces-specific spelling first and fall back to
the generic one. Set the same pair everywhere, or the export and the
retention job follow a different target than the one you configured.
Langfuse example (self-hosted or an existing enterprise server — the
connection is pure configuration):

```bash
INQTRIX_TRACING=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=https://<langfuse-host>/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk-lf-…:sk-lf-…)>,x-langfuse-ingestion-version=4"
```

The `x-langfuse-ingestion-version=4` header is required for real-time
ingestion (without it Langfuse may delay display by up to 10 minutes);
Langfuse accepts OTLP over HTTP only (no gRPC). Keep
`OTEL_EXPORTER_OTLP_HEADERS`, and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` if you
use it, in the SECRETS env file — the value embeds the Langfuse secret key.
The deploy CLI's `config --redact` output hides the value of every OTLP
headers variable, the generic one and each per-signal form.

Instance admins can pull the full trace of a run via
`GET /v1/admin/runs/{run_id}/trace/export` — served from Langfuse in
`otlp` mode or from the file spool in `file` mode (that document keeps
the replayable OTLP-JSON line shape); `off`/`local` answer with a clear
409. Every export writes an `export.trace` audit entry before the
response. The system status panel (admin settings) shows the configured
mode, whether a tracer is effectively installed, content capture, and
the retention policy.

The replay CLI (`python -m inqtrix.observability.replay`, imports
`INQTRIX_TRACING=file` spools into Langfuse later) reads one extra
variable: `LANGFUSE_REPLAY_AUTH` (`pk:sk`, becomes Basic auth). The CLI
deliberately accepts no auth flag — credentials come from the
environment only, because argv is visible in `ps` output and shell
history; source the value from your secrets file
(`set -a; . <secrets file>; set +a`) rather than typing it inline, so it
stays out of shell history too. Without it the CLI falls back to
`OTEL_EXPORTER_OTLP_HEADERS`.

**Interactions.** The API server and the worker read the same variables —
set them for BOTH containers. The submitter's trace context travels to the
worker inside the run row (`request_payload.telemetry`, W3C `traceparent`),
so one trace spans HTTP request → submit → worker execution; old rows and
mixed versions degrade to fresh root spans. Spans are flushed on server
lifespan shutdown and worker drain (`BatchSpanProcessor` loss window).

**When this block is OFF.** No OpenTelemetry import happens at all; JSON
log lines simply omit `trace_id`/`span_id`.

## Process-level variables (outside `Settings`)

A few variables are read by the process bootstrap rather than the Pydantic groups. The server/worker bind and logging knobs:

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_SERVER_HOST` | `0.0.0.0` | Bind host for uvicorn in `python -m inqtrix`. |
| `INQTRIX_SERVER_PORT` | `5100` | Bind port for uvicorn in `python -m inqtrix`. |
| `INQTRIX_LOG_ENABLED` | `false` | Master switch for persistent file logging (`true` activates it); otherwise the library only attaches a `NullHandler`. Read by both the server and `inqtrix-worker`. |
| `INQTRIX_LOG_LEVEL` | `INFO` | Logging level for the `inqtrix` logger: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Structured runtime events are emitted at `DEBUG`. |
| `INQTRIX_LOG_CONSOLE` | `false` | `true` mirrors WARNING+ records onto stderr in addition to any file sink. |
| `INQTRIX_LOG_INCLUDE_WEB` | `true` | When file logging is on, also route uvicorn/FastAPI logs into the same file. Set `false` to opt out. |
| `INQTRIX_LOG_WEB_LEVEL` | `INFO` | Level for the uvicorn/FastAPI loggers (used by `build_uvicorn_log_config`). |
| `INQTRIX_LOG_FORMAT` | `text` | `text` keeps the historical pipe format byte-identical; `json` renders one machine-readable object per line (Loki/SIEM/jq) carrying `ts`, `level`, `logger`, `event`, `message`, `request_id`, `run_id`, the pseudonymous `user`, `workspace`, `tenant`, and — once tracing is active — `trace_id`/`span_id`. Applies to the `inqtrix` logger AND the mirrored uvicorn/FastAPI loggers; the secret-redaction filter runs unchanged in both formats. Recommended in containers. |

Full logging behaviour, file paths, and forensic-event interaction with `OBSERVABILITY_PROFILE`: [Logging](../observability/logging.md).

The Helm chart's schema-maintenance job (`job-owner-maintenance.yaml`) reads
its own group, set only on that one-shot pod and never on api/worker
(`src/inqtrix/deployment/kubernetes_maintenance.py`):

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_K8S_NAMESPACE` | *(required)* | Namespace whose workloads the maintenance job quiesces. |
| `INQTRIX_K8S_RELEASE` | *(required)* | Helm release name used to select the workloads. |
| `INQTRIX_K8S_WORKLOAD_PREFIX` | *(required)* | Name prefix of the Deployments the job scales to zero: api, worker, collaboration, and pgbouncer. |
| `INQTRIX_K8S_QUIESCE_TIMEOUT_SECONDS` | `300` | How long the job waits for pods to drain before failing. |
| `INQTRIX_K8S_MAINTENANCE_ROLE_BINDING` | `<prefix>-schema-maintenance` | RoleBinding whose presence authorizes the scale operation. |

## Compose-only variables

These names are read by `deploy/compose/*.yaml` interpolation — they
configure **containers**, never the Inqtrix application,, although several are
production controls of the Compose stack. **None of them does anything on
Kubernetes**; the per-entry notes name the Helm equivalent where one
exists. Set them in the invoking shell or the paired env files at
`podman compose` time.

Production-relevant:

- `INQTRIX_WEB_BIND_ADDRESS` (`127.0.0.1`; set `0.0.0.0` only for an
  explicit trusted-LAN test) and `INQTRIX_WEB_PORT` (`8080`) publish the
  single selected `web` adapter — the Python gateway, or another target with the
  explicit override; internal API and data-service ports remain private.
- `INQTRIX_BUNDLED_POSTGRES_MAX_CONNECTIONS` (`300`) is the server ceiling
  on concurrent connections for the **bundled** PostgreSQL, rendered as
  `-c max_connections=...`. The image default of 100 does not cover the
  shipped run cap: run threads open a connection per database operation,
  and an API plus a worker each add their own pooled budget, both reported
  at startup. The 300 carries headroom for that sum; it is an engineering
  estimate, not a figure measured at the run cap. Every connection costs
  memory, so raise the `postgres` service's memory limit alongside it. An
  external database ignores this entirely — its ceiling belongs to whoever
  runs it. **Helm equivalent: `postgres.maxConnections`** (a values key
  rendering a container argument, not an env var — putting this name into
  `config:` creates a ConfigMap entry nothing reads).
- `INQTRIX_MIGRATION_ENV_FILE` selects an optional separate migration env
  file; API and worker never load it.
- `INQTRIX_ENV_FILE` (`deploy/.env.stack`) and `INQTRIX_SECRETS_FILE`
  (`deploy/.env.stack.secrets`) point at the two env files every service
  loads; the paths are repository-root-relative.
- `INQTRIX_PG_USER` (`inqtrix`), `INQTRIX_PG_DB` (`inqtrix`) and
  `INQTRIX_PG_PASSWORD` (no default, REQUIRED) provision the bundled
  PostgreSQL and compose the `INQTRIX_DATABASE_URL` the app receives. An
  external database ignores them.
- `INQTRIX_VALKEY_PASSWORD` (no default) is REQUIRED by the `workers`
  profile — the broker container refuses to start without it.
- `INQTRIX_BROKER_ENGINE` (`valkey`; `redis` selects the Redis binaries and
  CLI) and `INQTRIX_BROKER_IMAGE` (the pinned Valkey image plus digest)
  choose the queue backend container. Both speak the same protocol; the app
  is unaffected.
- `INQTRIX_WEB_TLS_CERTFILE` and `INQTRIX_WEB_TLS_KEYFILE` (no defaults,
  both REQUIRED) are the absolute host paths `compose.web-tls.yaml` mounts
  for direct TLS termination in the web container.
- `INQTRIX_S3_CA_BUNDLE_HOST` (`/dev/null`) is the HOST path mounted into
  api and worker so `INQTRIX_S3_CA_BUNDLE` can name a private CA inside the
  container.
- `INQTRIX_COLLABORATION_VERIFICATION_FAULTS` (`0`) injects
  snapshot-verification faults into the collaboration container; keep it at
  `0` outside fault-injection tests.

Development conveniences:

- `INQTRIX_WEB_NGINX_IMAGE` (`inqtrix-web-nginx:local`) changes only the
  image name/tag used by `compose.web-nginx.yaml`; the override still
  selects nginx.
- `compose.loadtest-providers.yaml` accepts `INQTRIX_MOCK_EMBEDDING_DIM`
  (`3072`), `INQTRIX_MOCK_SEARCH_RESULTS` (`8`), `INQTRIX_MOCK_SEED`
  (`1`), `INQTRIX_MOCK_LATENCY_SCALE` (`1.0`) and `INQTRIX_MOCK_SELECT`
  (*(empty)*) for the calibrated provider mock. The mock's
  `INQTRIX_MOCK_PORT` (`9300`), `INQTRIX_MOCK_PROFILE` and
  `INQTRIX_MOCK_CORPUS` are written into the container as fixed values by
  that file and ignore the invoking shell. They never touch a real
  deployment.
- `compose.dev-ports.yaml` accepts `INQTRIX_PG_PORT` (`5432`),
  `INQTRIX_API_PORT` (`5100`), `INQTRIX_QDRANT_PORT` (`6333`),
  `INQTRIX_QDRANT_GRPC_PORT` (`6334`), `INQTRIX_VALKEY_PORT` (`6379`),
  `INQTRIX_S3_PORT` (`8333`), and `INQTRIX_COLLABORATION_PORT` (`1234`). They change host ports only; every bind
  remains on `127.0.0.1`.

The bundled Langfuse profile has its own Compose-only set, documented next
to the observability group:
[Compose-only variables for the bundled Langfuse profile](#compose-only-variables-for-the-bundled-langfuse-profile).

## Development and test-only variables

These variables are read by helper scripts, the evaluation harness, and the test suite — never by the server or worker at runtime. They are listed here for completeness; none belongs in a production deployment. Background: [Running tests](../development/running-tests.md), [Local infrastructure](../development/local-infrastructure.md), [Testing strategy](../development/testing-strategy.md).

Replay tests (`tests/replay/`):

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_RECORD_MODE` | *(unset → replay-only)* | vcrpy record mode for the cassette replay tests: `once`, `new_episodes`, `all`, or `none`. Re-records cassettes against a real backend when set (see the `uv run` recipe in [Running tests](../development/running-tests.md)). |

Optional external services for integration tests (each empty value skips its suite):

| Variable | Default | Effect |
|----------|---------|--------|
| `INQTRIX_TEST_DATABASE_URL` | *(empty)* | Postgres URL enabling the `tests/storage/*_postgres.py` suite (pytest marker `postgres`). |
| `INQTRIX_TEST_QDRANT_URL` | *(empty)* | Qdrant endpoint enabling the qdrant-store tests (pytest marker `qdrant`). |
| `INQTRIX_TEST_REQUIRE_INTEGRATION` | *(empty)* | `1` turns a missing URL above into a session error instead of a skip, for the mandatory pre-release run in [Running tests](../development/running-tests.md). |
| `INQTRIX_TEST_QDRANT_API_KEY` | *(empty)* | API key for the test Qdrant instance. |
| `INQTRIX_TEST_S3_ENDPOINT` | *(empty)* | S3-compatible endpoint enabling the object-store S3 tests. |
| `INQTRIX_TEST_S3_ACCESS_KEY` | `inqtrix-dev-access` | Access key for the test S3 endpoint. |
| `INQTRIX_TEST_S3_SECRET_KEY` | `inqtrix-dev-secret` | Secret key for the test S3 endpoint. |
| `INQTRIX_TEST_S3_MANAGED_BUCKET` | *(empty)* | Existing bucket enabling the opt-in managed-S3 smoke test through the SDK default credential chain. The test never creates or deletes the bucket. |
| `INQTRIX_TEST_S3_MANAGED_ENDPOINT_URL` | *(empty)* | Optional S3-compatible endpoint for the managed-S3 smoke; empty uses native AWS endpoint resolution. |
| `INQTRIX_TEST_S3_MANAGED_REGION` | `us-east-1` | Region for the opt-in managed-S3 smoke. |

Evaluation harness (`tests/eval/`): `INQTRIX_EVAL_GOLDEN_SET` (`base`), `INQTRIX_EVAL_KNOWLEDGE_PROFILE` (`standard`), `INQTRIX_EVAL_VECTOR_BACKEND` (`memory`), `INQTRIX_EVAL_QDRANT_URL` (`http://127.0.0.1:6333`), `INQTRIX_EVAL_QDRANT_API_KEY` (*(empty)*), `INQTRIX_EVAL_SPARSE` (`bm25_german`), `INQTRIX_EVAL_RERANKER` (`none`), `INQTRIX_EVAL_CONTEXTUALIZE` (`off`), `INQTRIX_EVAL_EMBEDDING_PROVIDER` (`openai_compatible`), `INQTRIX_EVAL_EMBEDDING_MODEL` (`text-embedding-3-small` in `test_retrieval_eval.py`, `text-embedding-3-large` in `test_answer_eval.py` — the two harnesses do NOT share a default), `INQTRIX_EVAL_EMBEDDING_BASE_URL` (*(empty)*), `INQTRIX_EVAL_EMBEDDING_API_KEY` (*(empty)*), `INQTRIX_EVAL_AZURE_ENDPOINT` (*(empty)*), `INQTRIX_EVAL_AZURE_API_KEY` (*(empty)*), `INQTRIX_EVAL_AGENT_LIVE` (*(unset)*; `1` opts into the live agent-kernel eval, which additionally needs real provider credentials). These mirror the corresponding runtime knobs but isolate an eval run from any deployment configuration.

Research Desk web gateway (`python -m inqtrix_web_gateway`, the canonical
runtime of the packaged Python `web` image and the standalone Python-host option):
`INQTRIX_DIST_DIR`, `INQTRIX_BACKEND_URL`, `INQTRIX_WEB_ADAPTER` (`python`
inside the Python image; another image validates its own sentinel),
`RESEARCH_DESK_HOST`/`_PORT`/`_WORKERS`,
`RESEARCH_DESK_SSL_CERTFILE`/`_KEYFILE`/`_KEYFILE_PASSWORD`,
`INQTRIX_PUBLIC_BASE_URL`, `INQTRIX_EXTERNAL_SCHEME`,
`INQTRIX_MAX_UPSTREAM_CONNECTIONS`, `INQTRIX_PROXY_MAX_BODY_BYTES`, and the
collaboration frame limits. `WEB_CONCURRENCY` is deliberately not a second
worker control: when it conflicts with `RESEARCH_DESK_WORKERS`, the gateway
ignores it and emits a warning. The full table with defaults lives in
[React UI: gateway environment variables](../deployment/react-ui.md#gateway-environment-variables).

Optional deployment CLI (`python -m inqtrix.deploy` / `inqtrix-deploy`):
`INQTRIX_DEPLOY_ENGINE` selects the default Compose engine only when
`--engine` is omitted (`auto`, `docker`, or `podman`). Config, secrets,
profiles, Compose overrides, and service targets still come from explicit
files and arguments; the variable does not introduce provider detection or a
second deployment configuration.

Search debug script (`scripts/debug_search_dataflow.py`): `INQTRIX_PERPLEXITY_INSTRUCTIONS` (*(unset)*), `INQTRIX_PERPLEXITY_MODEL` (*(unset)*), `INQTRIX_PERPLEXITY_PRESET` (`fast-search`). Script-local overrides for ad-hoc Perplexity dataflow debugging; the runtime equivalents are `INQTRIX_SEARCH_INSTRUCTIONS` / `SEARCH_MODEL` / `INQTRIX_SEARCH_PRESET` in the Providers block.

Live prompt-flow debug script (`scripts/live_prompt_flow_debug.py`): `INQTRIX_LOG_DIR` (default `logs`; the `configure_logging(log_dir=...)` target, wired from env only by this script — the server/worker always use `logs`). The script also defaults `OBSERVABILITY_PROFILE` to `forensic` for its own run.

## Related docs

- [Agent config](agent-config.md)
- [Report profiles](report-profiles.md)
- [Retrieval profiles](knowledge-profiles.md)
- [Security model](../deployment/security-model.md)
- [Logging](../observability/logging.md)
