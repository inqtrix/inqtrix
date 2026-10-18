# Web server mode

> Files: `src/inqtrix/server/app.py`, `src/inqtrix/server/container.py`, `src/inqtrix/server/routes.py`, `src/inqtrix/server/routers/`, `src/inqtrix/services/`, `src/inqtrix/server/stacks.py`

## Scope

How Inqtrix exposes the agent platform as an HTTP service (v0.2.0): the layering, the endpoint surface per feature, native runs with their SSE event stream, the capability manifest, and the storage/queue backends. The operational reference for the example stacks stays [`examples/webserver_stacks/README.md`](../../examples/webserver_stacks/README.md).

## Layering

```text
server/routers/*          thin, one module per surface (chat, runs, knowledge, files, ...)
  -> services/*           orchestration: AgentContextResolver, ChatService, RunService,
                          HealthService, KnowledgeService, FileService
    -> core/              AlgorithmRegistry, AgentAlgorithm, RunRequest/AgentResult, RunContext
      -> engines          research/web_research.py (research, direct_llm) and
                          knowledge/algorithm.py (knowledge)
```

`server/container.py:build_container` is the composition root — the single place where providers, strategies, settings, the algorithm registry, the auth provider, and the application services are assembled into one `AppContainer`. Services never read the environment (Constructor-First); the container hands them everything. `server/routes.py` is a thin registration facade kept for the legacy `register_routes(...)` signature; it builds the container and includes the split routers. `research/web_research.py` is the only module invoking `graph.run` for serving — tests monkeypatch `inqtrix.research.web_research.run_web_graph`.

`create_app(*, settings=None, providers=None, strategies=None, auth_provider=None, object_store_impl=None)` is the single-stack factory (used by `python -m inqtrix`); `create_multi_stack_app(*, settings, stacks, default_stack)` mounts several `StackBundle`s in one process and adds `GET /v1/stacks` plus the per-request `body["stack"]` selector. Both call `build_auth_provider(settings)` and wire an ASGI lifespan that logs providers, security layers, and queue/concurrency configuration on startup (no silent fallbacks).

## Endpoints

### Meta and discovery (always registered, unauthenticated)

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness, active providers/models, `auth_required` + `auth_mode`, `legal` block, `ai_disclosure` block (see [AI transparency](ai-transparency.md)). |
| `/readyz` | GET | Readiness for load balancers: 503 while the database or queue is unreachable (bounded 2s probes), 200 `degraded` when only the vector store is down (knowledge fails per-request, everything else serves). Memory backends answer `ready`/`skipped`. The body's `database_contract` field separates a confirmed contract break (`violation` — product routes gate immediately) from mere unreachability (`unavailable` — product routes keep serving until unreachability persists past a bounded window; only the confirmed break closes the product gate at once). The Helm chart keys its `readinessProbe` on this; liveness stays on `/health`. |
| `/v1/models` | GET | OpenAI-style model discovery (`research-agent`). |
| `/v1/capabilities` | GET | Feature manifest — see [the feature-gating contract](#v1capabilities-the-feature-gating-contract). |
| `/v1/stacks` | GET | Multi-stack deployments only; per-stack provider/model discovery (5-second cache). |
| `/api/auth/config` | GET | Pre-login discovery for the SPA: active auth mode, the login methods to render (SSO button, email/password form, API-key field), the SSO provider label, whether the first-run owner is still missing, and the CSRF/logout contract. Always mounted, answered with `Cache-Control: no-store`. |

### Chat and editor (gated)

| Route | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible endpoint, streaming and non-streaming, optional `mode` field; a `mode: knowledge` request also accepts `ui_language` (`de`/`en`), as on `/v1/runs`. |
| `/v1/text/improvements` | POST | LLM-backed improvement suggestions for browser text fields. |
| `/v1/editor/suggest` | POST | Rewrite for a selected/commented Markdown block (optional `attachments`, and `context_before`/`context_after`: up to 400 characters each of the text directly around the passage, so the rewrite fits its sentence; a longer value is refused). |
| `/v1/editor/instruct` | POST | Document-level editor instruction returning content-anchored edit proposals. |

`/v1/editor/suggest` retries the model once only when the result is the
unchanged passage although an instruction was given; whether a free-text
instruction asks for something shorter or a number of sentences is the
model's reading, not a server check. When the text after the passage starts
directly with the punctuation mark the rewrite ends with (the same mark twice,
or a full stop before an ellipsis), the rewrite's mark is dropped before that
check and logged; any other pair, such as a question mark before a full stop,
stays as written and is logged. The seam is judged against
the document as it was when the request was made, and a mark inside closing
Markdown (`**...**`) is not seen.

### Native runs (gated)

The runs routes are documented in [Runs API](../extending/runs-api.md), which lists the submit/list/summary/events/result/cancel/delete surface together with the child, plan, task, approval, clarification, and artifact routes.

### Agent memory (gated; personal, when the agent platform is mounted)

| Route | Method | Purpose |
|---|---|---|
| `/v1/agent/memory` | GET | List personal accepted memories; optional `?q=&scope=&limit=` searches the same personal namespace via recall. |
| `/v1/agent/memory/{memory_id}` | PATCH / DELETE | Edit or delete one memory. Foreign ids are indistinguishable 404s. |
| `/v1/agent/memory:clear` | POST | Clear all personal memories, optionally one `scope`. |
| `/v1/agent/memory/candidates` | GET | List personal memory candidates staged by completed agent runs. |
| `/v1/agent/memory/candidates/{id}:accept` | POST | Accept a candidate, optionally with edited `content`. |
| `/v1/agent/memory/candidates/{id}:reject` | POST | Reject a candidate. |
| `/v1/agent/memory/feedback` | GET | Read personal run-feedback history; optional `?run_id=&limit=`. |
| `/v1/agent/runs/{run_id}/feedback` | POST | Store personal run feedback: `feedback=positive\|negative\|neutral`, optional `reason`, optional owner-checked `memory_id`. |

The memory routes never accept client owner fields (`sub`, `tenant_id`,
`user_id`, `owner`, `namespace`, ...). Ownership is derived from the
authenticated principal; anonymous/static-key principals cannot use
long-term memory.

### Knowledge (gated; registered only when `INQTRIX_KNOWLEDGE_ENABLED=true`)

| Route | Method | Purpose |
|---|---|---|
| `/v1/knowledge/collections` | POST / GET | Create (immutable embedding model) / list collections. |
| `/v1/knowledge/collections/{id}` | DELETE | Start the durable collection-and-vector deletion; HTTP 202 returns a `DeletionOperation`. |
| `/v1/knowledge/collections/{id}/document-revisions` | POST | Reserve an immutable text/asset revision and return its server-owned indexing job (HTTP 202). |
| `/v1/knowledge/collections/{id}/documents` | POST / GET | Compatibility ingestion (`text` OR raw `file_id`) over the same revision job / list documents. |
| `/v1/knowledge/documents/{id}` | DELETE | Start the durable document-and-vector deletion; HTTP 202 returns a `DeletionOperation`. |
| `/v1/knowledge/documents/{id}/text` | GET | Full extracted text (the document viewer's source). |
| `/v1/knowledge/documents/{id}/chunks/{chunk_index}` | GET | One chunk plus up to `?context=0..3` neighbour chunks per side (the evidence view). |
| `/v1/knowledge/search` | POST | Synchronous retrieval search (debug/evaluation surface). |
| `/v1/sources/{document_id}` | GET | Citable source view — the target of HTTP knowledge citations. |
| `/v1/knowledge/indexing-jobs*` | GET / POST | Read, cancel, resume, explicitly resume without contextualization, and stream collection-generation or document-revision jobs. |

See [Knowledge engine](../architecture/knowledge-engine.md) for the data model, ingestion chain, and answer path.

### Files (gated; ObjectStore-backed)

| Route | Method | Purpose |
|---|---|---|
| `/v1/files` | POST / GET | Streamed upload (spooled, hash/size-accounted, `INQTRIX_MAX_FILE_BYTES` cap → 413) / list. A normal bound upload also returns its durable upload operation and server-owned parse state. |
| `/v1/files/{file_id}` | GET / DELETE | Metadata / start deletion through the linked asset lifecycle. |
| `/v1/files/{file_id}/content` | GET | Streamed download (clients never see store URLs or credentials). |
| `/v1/uploads` / `/v1/uploads/{operation_id}` | GET | List/read durable bound-upload and parse checkpoints. |
| `/v1/uploads/{operation_id}/retry` | POST | Retry the same recoverable upload operation; missing bytes are requested explicitly. |

### Project persistence (gated; durable when `features.project_persistence=true`)

| Route | Method | Purpose |
|---|---|---|
| `/v1/knowledge-session-groups` | GET | List Knowledge Desk folders for the caller/workspace. |
| `/v1/knowledge-session-groups/{group_id}` | PUT / DELETE | Upsert or delete one Knowledge Desk folder. Delete orphans member sessions to `group_id=null`. |
| `/v1/knowledge-sessions` | GET | List Knowledge Desk session metadata. Rows include `group_id`; `items_json` is excluded. |
| `/v1/knowledge-sessions/{session_id}` | GET / PUT / DELETE | Load, upsert, or delete one Knowledge Desk session. PUT accepts `title`, `items_json`, `created_at`, `updated_at`, and `group_id` (`string` or `null`). |
| `/v1/agent-session-groups*` | GET / PUT / DELETE | List and manage private Agent Desk session folders. |
| `/v1/agent-sessions` | GET | List Agent Desk sessions with their small `items_json` settings body (source policy, knowledge switch, model pick), so the client compares its sessions against the real rows. |
| `/v1/agent-sessions/{session_id}` | GET / PUT / DELETE | Load, upsert, or delete one Agent Desk session. The UI-owned `items_json` includes the session source policy; PUT also carries `title`, `created_at`, `updated_at`, and optional `group_id`. |
| `/v1/assets/{asset_id}` | GET / PUT / DELETE | Read/upsert a file-library asset or start its aggregate deletion. The DELETE response is HTTP 202 and is not terminal. |
| `/v1/assets/deletion-operations` | POST / GET | Start a stable bulk manifest or list retained deletion receipts for recovery after reload. |
| `/v1/deletion-operations/{operation_id}` | GET | Read authoritative progress for asset, group, section, vector-index, knowledge-document, or knowledge-collection deletion, or for an account erasure (read by the administrator who started it, without a workspace). |
| `/v1/deletion-operations/{operation_id}/retry` | POST | Resume the same failed cleanup manifest (HTTP 202). |
| `/v1/vector-indexes/{index_id}` | DELETE | Fence the index and its backing collection, then start the same durable deletion lifecycle. |
| `/v1/editor/documents/{document_id}` | PUT | Autosave upsert of one editor document. `revision` is the client's last-synced SERVER revision plus one, and the write applies only when the stored revision is EXACTLY that base (compare-and-swap). Any other stored value answers 409 `conflict` with `current_revision` — not only a stale-or-equal counter but also a forward jump past the base, because a writer that never saw a concurrent agent patch or peer edit must rebase rather than clobber with a higher counter. |
| `/v1/editor/documents/{document_id}/patches` | GET | Patch metadata of one editor document (no edit bodies), newest first by creation time, compared in one unit although a proposal is stamped in Unix seconds and a patch from a shared-document suggestion in epoch milliseconds; `?status=pending\|accepted\|rejected` filters. |
| `/v1/editor/patches/{patch_id}` | GET | One patch with its anchored edits, warnings, and the document's CURRENT revision. |
| `/v1/editor/patches/{patch_id}:apply` | POST | Apply a pending patch server-side. Body `{"expected_revision": int}` (CAS against the document); stale revision answers 409 with `current_revision` + `revision_before`; replaying the same apply answers 200 with the stored outcome. |
| `/v1/editor/patches/{patch_id}:reject` | POST | Reject a pending patch. Body `{"note"?: string}`; already-applied answers 409, a reject replay 200. |

These routes are wired with the project-persistence tier's private
owner/workspace scoping. The volatile memory implementation is available for
offline/tests, but the frontend treats the tier as durable only when the
capability flag is true.

### Instance administration (session-admin gated)

| Route | Method | Purpose |
|---|---|---|
| `/v1/admin/system/runtime` | GET | Sanitized runtime categories for the System settings page: storage, run queue/execution, file store, knowledge backends, API documentation state, and read-only reachability booleans for optional backing services. |

### Auth (registered in the cookie-session modes, `INQTRIX_AUTH_MODE=oidc` and `local`)

[Auth modes](auth-modes.md) lists these routes per mode (login/callback, the `local` first-run and password routes, session, logout) alongside the mode each one belongs to.

### Test

`POST /v1/test/run` exists only when `TESTING_MODE=true` (used by `inqtrix-parity`). Never enable in production.

Registration is a composition decision, not a runtime fallback: `routes.py` includes the files router only when a `FileService` is wired (the default wiring always builds one), the knowledge and sources routers only when the knowledge engine is enabled, and the auth router only in the cookie-session modes (`oidc` and `local`). A disabled feature has no routes (404), keeping the historical surface untouched.

## Authentication and TLS

Every request resolves to a `Principal` through the active `AuthProvider`; `/health`, `/v1/models`, `/v1/capabilities`, and `/v1/stacks` stay open for probes and pre-credential discovery.

`INQTRIX_AUTH_MODE` is `infer` (default), `none`, `apikey`, `oidc`, or `local`.

See [Auth modes](auth-modes.md) for the full mode reference and the OIDC variable set, and [Security model](security-model.md) for TLS, CORS, and the startup banner. TLS: setting both `INQTRIX_SERVER_TLS_KEYFILE` and `INQTRIX_SERVER_TLS_CERTFILE` makes the example stack scripts bind HTTPS (partial configuration raises at startup); `python -m inqtrix` does not wire TLS into uvicorn — terminate TLS at a reverse proxy or run a stack script.

## Modes and per-request overrides

`/v1/chat/completions` and `/v1/runs` accept a top-level `mode` resolved against the `AlgorithmRegistry`:

- `research` — the classify/plan/search/evaluate/answer web-research graph.
- `direct_llm` — straight to the active LLM provider (same path as the legacy `agent_overrides.skip_search=true`, which remains supported; conflicts return 400).
- `knowledge` — retrieval over knowledge collections, only registered when the engine is enabled. Scope and profile travel in `knowledge_filters` (see [Retrieval profiles](../configuration/knowledge-profiles.md)). Streaming (`stream=true`) is supported: streamed chat dispatches through the `AlgorithmRegistry` like every other mode, so the cited answer streams as content chunks. The granular progress (gate rounds, grounding) is emitted only on the native `/v1/runs` SSE surface — the chat-completions stream carries the answer, not the progress narration.
- `workspace_agent` — the staged Mission engine with plan, approval,
  execution, synthesis, and optional canvas/editor delivery. It is registered
  only when the agent-platform durability gate passes.
- `agent_kernel` — the automatic conversational front door. It may answer
  directly, use instant tools, or delegate a Mission. Registration additionally
  requires the kernel rollout gate and a tool-calling-capable provider.

`agent_overrides` is a strict whitelist (`extra="forbid"`, unknown keys → 400): `max_rounds`, `min_rounds`, `confidence_stop`, `report_profile`, `depth`, `agent_tier`, `max_total_seconds`, `first_round_queries`, `skip_search`, `model_tier`, `model`, `effort`. `depth` (`normal`/`deep`) is the legacy thoroughness knob and `agent_tier` (`schnell`/`gruendlich`/`tief`) its successor; a tier bridges into `depth`, and a `depth` that contradicts the tier returns 400. `model_tier` selects among operator-configured tiers; `model`/`effort` pick a concrete model for direct chat and the Agent Desk's scoped thinking nodes. In the kernel this includes the conversation turn, both quick-web LLM calls and final review; in a Mission it includes planning, synthesis, critique and answer nodes while classifiers stay on the operator tier map. Mission children inherit the selected settings before the kernel's automatic Deep effort increase. Ordinary research-graph node names remain operator configuration. See [LLM calls, model tiers, and reasoning effort](../architecture/websearch/nodes.md) for tier wiring and `/health`'s `chat_model_options` discovery.

```json
{
  "model": "research-agent",
  "mode": "direct_llm",
  "messages": [{"role": "user", "content": "..."}],
  "agent_overrides": {"model_tier": "fast"}
}
```

Agent-native runs may add a `source_policy` (`web` and `knowledge`, each
`available` or `disabled`; omission means both available). The
`tool_directives` hint surface remains accepted. See [Source
policy](../architecture/agent-platform.md#source-policy) for the request
example, how the Agent Desk derives the policy, and the enforcement
precedence.

Mission plan tasks use one server-validated web contract (`web_instant` or a
delegated `web_research` child) — see [Agent
platform](../architecture/agent-platform.md) for when each is accepted.
Task `params.recency` accepts only `day`, `week`, `month`, `year`, or omission.
Task model selection belongs to the server: new plans reject `params.model_tier`,
and editing a persisted plan removes that parameter before validating its task
fields. Existing task records cannot forward it into child research overrides.
The caller's `agent_overrides.model_tier` remains available for mid/high steps;
fast steps retain their assigned tier and effort.
Task resource budgets are deployment-owned: non-empty `budget` objects on new
or edited plans fail with `task_budget_server_managed` instead of silently
changing execution limits.

Completed Agent Desk results expose one evidence shape through
`result.references`. Each entry retains its stable `K#`/`W#` label, canonical
URL or knowledge identity, exact `excerpt`/`source_text` when available, and
optional `grounded_support`. The latter is bounded provider-answer context for
a cited web URL, not a quote from the linked page; clients must label it
accordingly. `report_references` remains the canonical internal state key, with
the older Agent `references` key accepted only as an export compatibility
alias.

## Native runs and the SSE event stream (the UI contract)

`POST /v1/runs` accepts a run into a FIFO queue and returns immediately; `GET /v1/runs/{run_id}/events` replays the buffered events (up to `RUN_EVENT_BUFFER_SIZE`, default 200) and then streams live named SSE events. This stream is the contract the React Research Desk renders from:

- Lifecycle: `inqtrix.run.queued`, `inqtrix.run.started`, `inqtrix.run.resumed`, `inqtrix.run.snapshot`, `inqtrix.run.cancel_requested`, `inqtrix.run.cancelled`, `inqtrix.run.failed`, `inqtrix.run.completed`.
- Progress and answer publication: `inqtrix.node.started`, `inqtrix.progress.message`, `inqtrix.answer.started`, `inqtrix.output_text.delta`, `inqtrix.answer.ready`, `inqtrix.answer.interrupted`.
- Knowledge steps (`mode=knowledge`): `inqtrix.knowledge.profile.resolved`, `decomposition.completed`, `retrieval.completed`, `evidence.truncated`, `gate.evaluated`, `grounding.checked`.
- Agent execution facts: every state-bearing agent snapshot exposes the
  canonical `execution` block; kernel tool events report actual usage, and
  quick-web emits one `web_instant` started/finished pair. See [Run
  events](../extending/runs-api.md) for the block and consent semantics.

See [Runs API](../extending/runs-api.md) for payload schemas. With the in-memory store, terminal runs remain fetchable for `RUN_COMPLETED_TTL_SECONDS` (default 300); the Postgres backend makes records, events, and results durable.

Native UI clients should send a stable, non-secret workspace namespace via the `X-Inqtrix-Workspace-Id` header or a top-level `workspace_id` on `POST /v1/runs`. When present, the server stores it on the run record, filters `GET /v1/runs`, and requires the same namespace on the per-run routes. It is a browser/project routing namespace, not an auth boundary; omitting it preserves the unscoped behaviour for scripts.

`/v1/chat/completions` keeps the OpenAI-compatible SSE format (`stream=true`: progress chunks, `---` separator, answer chunks, `data: [DONE]`; `"include_progress": false` for answer-only). `"incognito": true` keeps the turn's content out of the traces whatever `INQTRIX_TRACE_CONTENT` says: no prompt, answer or tool payload reaches a span, while model, token usage, duration and codes are still recorded; only `true` or `false` is accepted. `POST /v1/runs` accepts the same flag for a `mode=knowledge` ask without `session_id`; such a run is also deleted `RUN_INCOGNITO_RETENTION_SECONDS` after it ended. The React Research Desk consumes the native run API instead — see [React UI](react-ui.md) for `VITE_INQTRIX_API_BASE_URL` and deployment topologies.

## `/v1/capabilities`: the feature-gating contract

Clients discover features here instead of hardcoding them; the manifest is unauthenticated (the UI needs it before any credential prompt) and exposes only feature identity plus sanitized availability-derived booleans. Shape:

| Block | Content |
|---|---|
| `algorithms` | Manifest of registered algorithm ids (`research`, `direct_llm`, plus `knowledge`, `workspace_agent`, and `agent_kernel` when their gates pass) with their capability dicts. |
| `features` | Booleans: `knowledge`, `files`, `openapi`, `embedding_provider`, `document_parser`; plus, when knowledge is on: `hybrid_retrieval`, `reranker`, `contextual_retrieval`. Infrastructure-backed flags are false when the configured object store/vector store is not reachable; `document_parser` reports the server-side file parser and depends neither on knowledge nor on its vector store. |
| `files` | `max_file_bytes` (when files are enabled). |
| `knowledge` | `default_embedding_model`, the annotated `embedding_catalog`, `default_top_k`, `evidence_k_max` (the final-evidence ceiling that bounds a `final_k` override), `default_profile`, `reranker_provider`, and `profiles[]` — the effective stage plan per retrieval profile including each profile's `final_k_factor` and its `degraded` stages, derived from the SAME ceiling instance the algorithm runs against. |
| `agent` | Agent modes, permission/depth presets, tools, and the source surface. `source_controls[]` publishes `web`/`knowledge` entries with `default`, effective `available` and a `reason_code` (`operator_disabled`, `store_unreachable`) when unavailable. Clients must not render a server-disabled source as switchable. |

A capability flag never downgrades silently: a profile stage the operator ceiling forbids appears in `profiles[].degraded`, and unreachable S3/Qdrant backends clear the affected feature flags so the UI renders what would actually run.

## Storage, queue, and the worker

| Concern | Variable | Backends |
|---|---|---|
| Records (runs, identity, file metadata) | `INQTRIX_STORAGE_BACKEND` | `memory` (default, in-process) / `postgres` (`INQTRIX_DATABASE_URL`, migrated via `inqtrix-migrate`, RLS via `INQTRIX_DATABASE_APP_ROLE`). |
| Binary blobs (uploaded files) | `INQTRIX_OBJECT_STORE_BACKEND` | `local` (content-addressed under `INQTRIX_OBJECT_STORE_PATH`) / `s3` (any S3-compatible store; SeaweedFS in the dev stack). |
| Run execution | `INQTRIX_QUEUE_BACKEND` | `memory` (in-process, default) / `valkey` (Valkey Stream consumed by `inqtrix-worker` processes; requires `INQTRIX_STORAGE_BACKEND=postgres` — the run row is the source of truth, the stream only the dispatch channel). |
| Vectors (knowledge) | `INQTRIX_VECTOR_BACKEND` | `memory` / `qdrant` — see [Knowledge engine](../architecture/knowledge-engine.md). |

Contradictory combinations (`valkey` without postgres, `s3` without credentials, `postgres` without a URL) fail loudly at startup. The worker (`uv run inqtrix-worker`, or `python -m inqtrix.worker` after a normal pip install) executes runs with at-least-once delivery: `INQTRIX_WORKER_CONCURRENCY` bounds parallel runs per process, heartbeats keep long runs from being reclaimed, and `INQTRIX_WORKER_MAX_ATTEMPTS` dead-letters crash loops. One Valkey queue instance is injected into both the Postgres run store and worker loop, so child submission, child-terminal parent wake, resume, and ordinary dispatch share the same path. Claiming a durable run row and dispatching its stream message remain separate operations: a successor message is held unacknowledged until the older delivery is acknowledged, which prevents a fast resume from being mistaken for a duplicate and dropped. Host-side development selects services from the canonical stack and adds only `deploy/compose/compose.dev-ports.yaml` for the `inqtrix-dev` project name and loopback ports; `dex` remains behind the `oidc` profile — see [Local infrastructure](../development/local-infrastructure.md).

When the knowledge engine is enabled, the same `inqtrix-worker` process also
consumes the dedicated indexing stream (`inqtrix:index:jobs`, dead-letter
`inqtrix:index:dead`) for both collection-generation builds and individual
document revisions. The durable Postgres job owns checkpoints, pause/cancel
state, and publication fencing; Valkey is dispatch only. A worker started with
knowledge disabled has no indexing consumer and logs that state explicitly.
Index admission/history uses `INQTRIX_REINDEX_*`; delivery, reclaim, and
dead-letter mechanics reuse `INQTRIX_WORKER_*`.

Two further streams carry recovery work that must not be represented as a
finished browser mutation: aggregate deletion uses
`inqtrix:deletion:jobs`/`inqtrix:deletion:dead`, and bound upload/finalization
uses `inqtrix:upload:jobs`/`inqtrix:upload:dead`. Their Postgres rows remain the
source of truth, and the same worker process starts the corresponding
consumers. Operators should monitor these dead-letter streams alongside
`inqtrix:index:dead` and `inqtrix:runs:dead`.

The admin System page reads `GET /v1/admin/system/runtime` for the deployment shape
behind those switches: session-only, instance-admin gated (personal access tokens
cannot call it), sanitized backend categories plus read-only reachability booleans,
never URLs, paths, bucket names, or credentials — see [Platform components](../getting-started/platform-components.md).
`runs.execution=worker_dispatch` describes the run path only; it is not a live
worker-count heartbeat. In queue mode the block also
carries `runs.queue_consumers` (stream consumers seen alive within the last
minute) and `runs.queue_depth` (messages in the dispatch stream, in-flight
ones included), both probed with a bounded timeout. They are `null` when the
broker cannot be asked — and also while no worker has EVER attached, because
the first worker creates the consumer group. A worker fleet that attached
once and then died shows `0` consumers on the System page.

## Concurrency and cancel

`MAX_CONCURRENT` (default 100) caps active `/v1/chat/completions` requests; a saturated semaphore returns 429 (no queueing on the OpenAI-compatible path). Native runs have their own explicit ceiling, `RUN_MAX_CONCURRENT` (default 100), for active workers and queue up to `RUN_QUEUE_MAX_SIZE` jobs (default 100) before `POST /v1/runs` returns 429. Background reindex jobs have their own pair, `INQTRIX_REINDEX_MAX_CONCURRENT` (default 6) and `INQTRIX_REINDEX_QUEUE_MAX_SIZE` (default 50). The caps are per surface, not a global provider limiter — worst case is their sum.

Because those semaphores are shared capacity, request-scoped endpoints stop
working for callers that are gone: non-streaming `/v1/chat/completions` and
both editor assist routes watch the client connection and cancel the
server-side work cooperatively when the caller disconnects mid-request
(provider calls stop at attempt boundaries and during retry backoff waits,
which poll the cancel signal about every half second; a single in-flight
HTTP attempt still runs to its transport timeout). A request abandoned this way is
answered with HTTP 499 and the typed error `client_closed_request` — never
delivered to the caller. The API's own uvicorn access log stays silent
(uvicorn skips access logging for clients that are already gone); the web
gateway's access log DOES record the 499 line for the aborted request. The greppable signals are
the WARNING lines instead — "…nach Client-Disconnect abgebrochen…" on the
API and "Client disconnected while awaiting the upstream response" on the
web gateway. 499 is only used when the disconnect actually happened; a
cancellation raised without one stays an honest 502. The bundled web
gateway propagates client aborts itself: when the browser closes the
connection while the gateway is still waiting for the upstream response,
it cancels the upstream request (the aborted connection is closed, never
reused from the keep-alive pool). Third-party reverse proxies in front of
the API must do the same for server-side cancellation to trigger.

The research graph and document re-embedding are **synchronous work on a bounded thread pool**: each active job holds one OS thread for its full duration, and you scale *out* (more worker processes) rather than *up* (thousands of threads in one process). See [Settings and environment](../configuration/settings-and-env.md) for `MAX_CONCURRENT`, the AI thread lane it sizes, and the real ceiling (provider rate limit, host CPU/RAM).

Streaming disconnects and `POST /v1/runs/{run_id}/cancel` set the same cancel event; execution stops at the next safe boundary (the current logical provider operation may use up to its configured 600-second budget). Explicit cancellation locks and transitions the complete descendant tree, then reconciles every affected Agent plan task from the terminal run rows. A read of an older partially reconciled plan repeats that idempotent settlement, covering a process failure after the run transaction committed. Queued and waiting rows cancel immediately. Agent Desk additionally exposes `POST /v1/runs/{run_id}/tasks/{task_id}/cancel`: pending work ends immediately, research children reuse run cancellation, and an in-flight synchronous instant request records `cancel_requested`, performs no retry or result commit, and ends naturally. Hard network interruption of that synchronous provider request is out of scope.

## Deployment sizing: with or without Valkey

Valkey and the worker are optional execution infrastructure, not an alternate
product implementation. They dispatch the same run, indexing, deletion, and
upload-recovery bodies whose durable records live in Postgres. Knowledge
retrieval, ordinary chat, and editor reads remain normal request paths. Three
tiers are chosen by the storage and queue switches:

| Mode | `INQTRIX_STORAGE_BACKEND` | `INQTRIX_QUEUE_BACKEND` | Execution | Durable across restart? | Multiple API replicas? |
|---|---|---|---|---|---|
| In-memory | `memory` | `memory` | in the API process | no (lost on restart) | no |
| **Postgres, no broker** | `postgres` | `memory` | in the API process | records and paused checkpoints yes; queued/running/cancelling closures fail visibly on restart; paused indexing work is reconstructed on explicit resume | **no — single API process** |
| Postgres + Valkey | `postgres` | `valkey` | `inqtrix-worker` processes | yes (redelivery) | yes |

**Sizing guidance.** For a small-to-mid deployment (say up to ~100 users with bursty, occasional runs/reindexes), **`postgres` + `memory` on a single API process is the right, simpler choice**: durable records, no broker, no worker. Tune `MAX_CONCURRENT` / `INQTRIX_REINDEX_MAX_CONCURRENT` to your provider's rate limit and the host's CPU/RAM. Two constraints define this mode:

- **Bounded concurrency, then a queue.** Beyond the active caps, jobs wait (FIFO) and then return 429. Sustained simultaneous *long* runs are what saturates it, not raw user count.
- **Single API process.** In-process durable mode assumes ONE API process, and its recovery is continuous, not restart-only: on restart it marks lost queued/running/cancelling closures `failed` (`server_restarted` — visible, not silent), and during operation a read-triggered check does the same for active rows this process no longer executes (`execution_lost` — a crashed execution converges within roughly a minute instead of spinning forever). Durable `paused_dependency`/`paused_validation` rows and checkpoints remain paused; explicit resume reconstructs the operation from its canonical document/revision or generation identity before queueing it. Because ownership is decided by this one process's in-memory registry, two replicas sharing one Postgres would continuously terminalize each other's in-flight closures — not just at boot. Running more than one replica, or needing actively executing jobs to survive a restart, is the cue to switch on Valkey + the worker (`--profile workers`).

**When to add Valkey + worker** is therefore about *operational shape*, not a fixed headcount: (a) more than one API replica (HA / load balancing), (b) isolating long jobs from the request-serving process, or (c) in-flight jobs that must survive a restart. With the worker tier, per-worker parallelism is `INQTRIX_WORKER_CONCURRENCY` (default 100) and you scale by running more worker replicas; the API-side `MAX_CONCURRENT` / `REINDEX_MAX_CONCURRENT` then govern admission. Note that the worker-wide value only sizes the research loop outright: indexing, upload and deletion each run at the lower of it and their own ceiling, because those spend real CPU while a research run waits on its provider.

The server is stateless for research data: each request runs a fresh `AgentState`; prior messages are formatted into the `history` string, but citations, evidence, and confidence are not carried over.

## Quick check

```bash
curl -N http://localhost:5100/v1/chat/completions \
    -H "Authorization: Bearer dev-secret-xxxxx" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "research-agent",
        "messages": [{"role": "user", "content": "Was ist der Stand der GKV-Reform?"}],
        "stream": true
    }'
```

A missing or wrong Bearer header returns 401 with `WWW-Authenticate: Bearer`. `INQTRIX_ENABLE_OPENAPI=true` additionally serves `/openapi.json`, `/docs`, and `/redoc` (off by default, preserving the historical no-schema surface).

## Related docs

- [Auth modes](auth-modes.md) — `none` | `apikey` | `oidc` in full, including the OIDC BFF variables.
- [Knowledge engine](../architecture/knowledge-engine.md) — collections, ingestion, the gated answer path.
- [Settings and environment](../configuration/settings-and-env.md) — every server/storage/queue variable with defaults.
- [Retrieval profiles](../configuration/knowledge-profiles.md) — `knowledge_filters.profile` and the stage matrix.
- [Runs API](../extending/runs-api.md) — SSE payload schemas for native runs.
- [Examples README](../../examples/webserver_stacks/README.md) — per-stack env tables and run commands.
- [React UI](react-ui.md) — the bundled frontend.
- [Security model](security-model.md) — TLS, Bearer, CORS, startup banner.
- [AI transparency](ai-transparency.md) — the `ai_disclosure` block and how generated output is marked.
- [Local infrastructure](../development/local-infrastructure.md) — the dev compose stack.
