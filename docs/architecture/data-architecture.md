# Data architecture

## Scope

This page is the map of **where each kind of data lives, why it lives there,
and how it is loaded**. It covers the platform (HTTP serving) deployments, not
the library mode (a single `research()` call holds everything in memory). Read
it when you need to reason about persistence, latency, sharing, or the
local-first vs server-persistent split.

The guiding rule, applied everywhere below:

> **Postgres is the relational source of truth. Qdrant holds only vectors +
> a lean payload. The object store holds original binaries. Valkey is a
> dispatch/live-event channel, never a system of record. The browser keeps
> only device-local UI state.**

For an editor document converted to collaboration mode, the Yjs binary update
journal and verified snapshots inside PostgreSQL are the body source of truth;
its Markdown column is a derived projection. This is the only project-tier
exception to treating the relational text body itself as authoritative.

Latency comes from **keyset pagination + a client cache + load-on-use**, not
from a server read-cache.

## The five stores and why

| Store | Role | Why here |
|---|---|---|
| **Postgres** (one database, many tables) | Relational source of truth: canonical user UUIDs / workspaces / direct shares / quota, runs + run events, user invalidations, files registry, prompt and skill templates, knowledge collections / documents / chunk text, indexing-job rows, and the project tier (chat, editor text or collaboration binaries, file-asset records, knowledge-session records, vector-index records, account preferences). | One transactional, joinable, backup-able store. Sharing, ownership, audit, invalidation, and cross-entity queries commit together. Row-level security (RLS) enforces tenant isolation in the database itself. |
| **Qdrant** | Vectors + a lean payload only (document id, chunk id, collection id, tenant, filter metadata — under ~1 KB). | A vector index is a read index, not a record store. Keeping full text or metadata in the payload is an anti-pattern; the canonical text is relational in Postgres, so a re-embed always has an authoritative source. |
| **Object store** (S3 / SeaweedFS / local volume) | Original binaries (PDF, DOCX, …) via the files registry. | Binaries do not belong in Postgres rows. Postgres keeps a reference (`server_file_id`); the bytes stay external. |
| **Valkey** | Job dispatch + live SSE buffering for runs and indexing jobs. | Durability lives in the Postgres job rows; Valkey only fans work out to the worker and carries live progress. Losing Valkey loses no committed state. |
| **Browser `localStorage`** | Device-local UI state only: theme/contrast/preset/user bubble tone (when offline), editor panel widths, scroll, drafts (`editorUi` / `ui`). | Per-device, sub-millisecond, never a server round-trip. Note: when a real session is active, theme/locale/contrast/user bubble tone are promoted to the account tier (see below) and `localStorage` becomes a fallback. |

```mermaid
flowchart LR
  Browser["Browser (research desk)\ndevice-local UI state"]
  subgraph Backend
    API["FastAPI api"]
    Worker["inqtrix-worker"]
    PG[("Postgres\nsource of truth")]
    QD[("Qdrant\nvectors + lean payload")]
    OS[("Object store\noriginal binaries")]
    VK[("Valkey\ndispatch + live events")]
  end
  Browser -- "REST: keyset list, load-on-use, debounced autosave" --> API
  API -- "rows + RLS" --> PG
  API -- "vector search" --> QD
  API -- "blob ref" --> OS
  API -- "enqueue job / SSE" --> VK
  VK -- "dispatch" --> Worker
  Worker -- "re-embed from canonical text" --> PG
  Worker -- "upsert vectors" --> QD
```

## The storage matrix

Every row: where it lives, how the client loads it, what caches it, and the
latency note. This is the reference; the prose above explains the *why*.

| Data | Store | Loading | Cache | Note |
|---|---|---|---|---|
| Original files (PDF/DOCX) | Object store + Postgres reference | lazy (download/preview) | browser HTTP | never a Postgres BLOB above ~5 MB |
| Extracted / chunk text (RAG source) | **Postgres TEXT** (canonical) | first page eager, rest lazy | client | full text in the Qdrant payload is an anti-pattern |
| Embeddings / vectors | **Qdrant only** | at search time | Qdrant internal | no Postgres duplication |
| Chat threads (list) | Postgres, keyset | eager first page (metadata) | client | thread metadata only on the list |
| Chat messages | Postgres, keyset | eager on thread open; a reply into the thread waits for them | client | composite PK `(thread_id, id)` isolates threads |
| Knowledge session groups | Postgres | eager (metadata) | client | folder records; deleting a group orphans sessions |
| Knowledge sessions | Postgres | metadata eager, items load-on-open | client | list/get include `group_id`; `items_json` excluded from list |
| Editor documents (Markdown mode) | **Postgres TEXT** | **load-on-open** | client | revision-CAS debounced autosave; body excluded from the list |
| Editor documents (collaboration mode) | **Postgres Yjs updates + verified snapshots** | WebSocket join; Markdown projection for read-only/fallback consumers | active Y.Doc | binary state is truth; durable ack follows commit; Node has no persistence volume |
| Private editor AI notes | Postgres (idx `document_id`, creator-scoped API) | lazy (on open) | client | creator-only; never promoted to a team discussion implicitly |
| Shared editor comments | Postgres threads/messages/read cursors + content-free `user_events` | incremental on document open/reconnect | client + browser draft storage | Yjs-relative anchor with quote fallback; author-only message mutation; tombstones and resolution remain auditable |
| Editor suggestions | transient for Markdown mode; Postgres patch metadata + Yjs marks for collaboration mode | assistant/changes inspector | active Y.Doc + client | collaboration decisions are idempotent server mutations; AI drafts stay private until publication |
| File-asset records (+ extracted text) | Postgres (binary in object store) | metadata eager, **body load-on-use** | client | record relational, blob external |
| Vector-index records (file↔collection) | Postgres (full record) | eager (small) | client | members + capped history travel with the record |
| Account preferences | Postgres (per user) | eager on login | React state | theme, locale, contrast, and user bubble tone follow the user, not the workspace |
| Device UI state (panels, scroll, drafts) | Browser `localStorage` | eager (mount) | localStorage | no server round-trip |
| Auth sessions | Postgres | eager (init) | memory | cookie session |
| Job / progress state | Postgres job rows (durable) / in-memory (fallback) | status poll / SSE | — | claimed by id from the dispatch message; `FOR UPDATE` on that row + status CAS |
| Job event stream | Postgres `*_events` + Valkey | SSE live | Valkey buffer | durability in Postgres |
| User invalidations | Postgres `user_events` / bounded memory queue | one per-user SSE channel | client refetch | content-free refetch signal, 24-hour retention |

## Two deployment realities

The same frontend runs in two modes, and the **capability flag** decides which:

- **Plain (`.env`, no Docker)** — no Postgres, no Qdrant, no object store. The
  research desk is **local-first**: the project lives in a local markdown
  directory / zip the user picks, and nothing syncs to a server. Indexing and
  RAG do not exist here.
- **Full stack (`docker compose`)** — Postgres + (optionally Qdrant + Valkey +
  `inqtrix-worker` + object store + one collaboration service). Here everything above can become
  **server-persistent**.

The switch is **capability-gated**. The server advertises
`features.project_persistence` (true only when the durable backend is on:
`INQTRIX_STORAGE_BACKEND=postgres`). How sync turns on then depends on the auth tier:

- **Authenticated cookie session (`local`/`oidc`) — automatic, server-first.**
  The desk derives `serverSyncEnabled` from the live session, so the project
  auto-hydrates from the server on every boot/reload and autosaves with **no button**.
  Data is scoped to a per-user namespace (below), so it survives reload + re-login and
  follows the user across devices. The server-first boot starts from an empty state and
  hydrates, so it does **not** re-push on boot. There is no import button in this tier.
- **`apikey` / local-first — manual opt-in.** The desk shows an explicit "move this
  project to the server" import (the button appears iff
  `canPersistProject && !serverSyncEnabled`); once a user opts in (`serverSyncEnabled`),
  the per-entity sync hooks hydrate the just-pushed data and take over autosave.

Without the capability, the desk stays local-first. No data is migrated behind
the user's back.

**Detached project import.** Before a manual server import, or when an
authenticated user loads a project exported from another per-user namespace,
the desk allocates a new globally unique id graph. Chat/rule/message, editor,
asset/index, knowledge-session and agent-session ids are remapped together and
all local references follow atomically in the reducer. Source server identities
(prompt-template, collaboration, file, knowledge collection/document, share,
indexing-job and agent-run ids) are cleared rather than promoted into ownership
of the clone. Imported report run ids remain local source references until the
run-import endpoint allocates canonical server ids and rewrites their project
references.

The current whole-project push still writes resource families sequentially;
it is not advertised as a cross-family server transaction. The detached graph
becomes the active, dirty local project before the first write and its mapping
is stable across retries in that project epoch. A mid-flight outage can leave
an incomplete same-owner copy until retry, but cannot mutate another user's
row; the scoped conflict guards remain the database backstop.

**Per-user namespace.** `workspace_id` is a free-form UI namespace, never an auth
input (authorization is the canonical `users.id` UUID). A fresh browser mints a random `ws_…`; on
the first authenticated boot the server **adopts** that id as the user's canonical
`users.default_workspace_id` (idempotent, atomic set-only-when-NULL) and returns it as
`project_namespace` from `/api/auth/session`. Every device then resolves the same
namespace from the session, so an authenticated user's project follows them with **zero
migration** — the originating browser's id IS the adopted namespace, so the data
reappears at once. The desk computes `effectiveWorkspaceId` (the namespace when
authenticated + capable, else the browser-local id) and scopes every project-data and
run call to it from a single source of truth. The flip to the namespace is in lockstep
with the desk becoming usable, so the sync lifecycle keys on the namespace from its
first hydrate and never re-keys off the browser id. The server-side
`workspaces`/`workspace_members` tables are a separate sharing/collab concept and are
not touched.

## The project-persistence tier

Each project entity follows the same faithful pattern: an ORM table → a linear
Alembic migration (with RLS + GRANT + CHECK boilerplate) → a port → a memory
implementation (offline/test) + a Postgres implementation → a service (scoping
+ validation) → a thin per-surface router → capability-gated frontend hooks.

**Scoping and resource identity.** Every project row is scoped
`(tenant_id, created_by_user_id, workspace_id)` — "this user's data in the
current workspace", the one-project-per-`(user, workspace)` model.
`created_by_user_id` is the principal's canonical local UUID for real sessions
(`oidc_session` / `pat`) and `NULL` for ownerless rows in anonymous/static
deployments. External issuer and subject values remain only in the
authentication binding and never authorize a project row. RLS enforces tenant
isolation in the database. Services apply the principal scope, while the store
also guards the conflict update itself: an `ON CONFLICT (id)` update proceeds
only when tenant, owner, and workspace match with null-safe equality. A foreign
id collision changes no row and is returned through the existing indistinct
not-found path.

Client-addressed project resource ids are globally unique, opaque values. The
browser uses one prefixed random-id generator; semantic behavior is determined
by fields such as `kind`, never by a fixed id. Legacy fixed bootstrap ids for
file sections and the default knowledge session are retained only when the
authoritative server list shows that they already belong to the current scope.
Otherwise the browser rekeys the record and every local reference atomically
before its first write. The same graph-wide rule is applied to detached project
imports; it is not limited to the two historical bootstrap ids.

**Autosave.** A shared `syncCollection` diff loop pushes entities whose
fingerprint (usually `updatedAt`) changed and deletes those the local project
no longer has, against an in-memory "what the server holds" map. On hydration
that map is seeded to the **server's** `updatedAt` so a local-newer entity is
pushed up rather than stranded. Server upserts are idempotent and never
reassign `created_at` or ownership. A child write locks and validates its parent
in the same database transaction, so relationships such as
asset-to-section/group and session-to-group cannot cross a tenant, owner, or
workspace boundary between a service precheck and the write.

These behaviours are worth calling out because they are where data-loss bugs
hide:

- **Re-arm on project identity, not just on the sync toggle.** The project-scoped
  hooks (chat, editor, file assets, knowledge sessions, vector indexes) key their reset+hydrate
  lifecycle on a project identity — `${workspaceId}:${projectEpoch}`, where
  `projectEpoch` is an ephemeral counter bumped on every wholesale project-state
  replace — via the shared `useProjectSyncLifecycle`. Switching to a *different*
  project that is also server-synced keeps the sync gate true but changes the
  identity, so each project re-hydrates from its **own** server state instead of
  inheriting the previous project's "what the server holds" map and deleting that
  project's rows on the next autosave. (Account preferences are account-scoped
  and deliberately re-arm on login instead — see below.) As defense-in-depth, the
  owner-only DELETE endpoints also refuse a request whose workspace namespace
  differs from the resource's (symmetric with how list filters `workspace_id`);
  edit-level deletes such as comments are exempt so shared collaborators still
  work.
- **Load-on-use for heavy bodies.** An editor document's `content_markdown` and
  a file asset's `extractedText` are excluded from the list (metadata-first)
  and fetched on demand. The invariant: a body is *authoritative locally* once
  loaded or once present at hydrate, and a metadata-only edit on an
  unloaded server entity must fetch the server body **before** a full-record
  PUT, or the empty local body would erase it. Editor documents load on open
  (the selected document); file assets have no per-asset "open" event, so their
  body loads **on use** — prefetched when a chip is attached to chat and awaited
  before send / before an index build / before an editor AI run, so a freshly
  hydrated body is never sent empty.
- **Knowledge sessions mirror Chat folders but keep item bodies lazy.**
  `knowledge_session_groups` are server records, and
  `knowledge_sessions.group_id` is a nullable membership FK. The frontend
  hydrates groups before sessions, saves groups before sessions, and includes
  `group_id` in the session fingerprint so moving a session into a folder is
  an autosaved metadata change. The heavy `items_json` body loads only through
  single-session GET. Deleting a group sets member sessions back to ungrouped
  (`ON DELETE SET NULL` in Postgres, explicit orphaning in the memory store).
  The untouched bootstrap placeholder remains local; a renamed or user-created
  empty session is persisted as real user state.
- **Server-owned index operations.** A vector-index record carries the user's
  logical membership and capped visible history, while every actual collection
  generation or document-revision build is a separately persisted indexing
  job. The client never serializes a guessed `indexing` status into the record:
  it projects the authoritative job summary and SSE stream, reconnects by job
  id, and removes that projection only after a terminal event. With Postgres
  and Valkey the job, checkpoints, cancel request, and worker fencing survive
  API/browser restarts. A stalled dependency is a visible paused state, not a
  terminal record repaired locally after reload.
- **Account preferences follow the user.** Theme / locale / contrast / user
  bubble tone are an
  account tier (one row per `(tenant, user_id)`), not project data, and are
  deliberately excluded from the project import. On a real per-user session the
  saved row is applied over the device's preferences on login ("account wins");
  a project file's embedded preferences are ignored while a session drives the
  account tier, so loading a file never overwrites the account. Anonymous /
  apikey sessions do not sync preferences (there is no canonical per-user
  identity in those modes, so one caller could otherwise overwrite another
  visitor's preferences).

**Suggestion persistence depends on document mode.** Legacy Markdown AI
suggestions remain transient and only acceptance/rejection mutates the body.
Collaboration suggestions are part of the Yjs document and carry durable patch,
author, sequence, and decision metadata because other users must review the
same pending change after reconnect or restart. Private AI-note work remains
visible only to its creator until the AI result is published as a shared Yjs
suggestion. Shared discussion threads are separate durable relational data and
never enter an AI request unless a user explicitly attaches one.

## Tenant isolation

Every Postgres table that holds tenant data is protected by two layers:

1. **Row-level security (RLS), fail-closed.** Each request resolves to a
   `Principal` carrying a `tenant_id`. The store opens its work inside a
   `tenant_session` wrapper that runs under a restricted role
   (`inqtrix_app`, `NOBYPASSRLS`) and sets a transaction-local GUC; the RLS
   policy on every table is `tenant_id = (SELECT inqtrix_current_tenant_id())`,
   `ENABLE`d **and** `FORCE`d. With no tenant set the resolver returns a value
   that matches nothing, so a missing tenant denies rather than leaks.
2. **Explicit query predicates.** The stores also filter `tenant_id` (and, for
   project rows, `created_by_user_id` / `workspace_id`) in the SQL itself, so
   per-user scoping is enforced in the service/store layer on top of the
   database-level tenant boundary.

The shared lifecycle for the project tier lives in
`src/inqtrix/project/base_session_store.py` (the dedicated NullPool engine +
`tenant_session`); the policy/GRANT boilerplate is applied uniformly in each
Alembic migration.

`tenant_session` also owns transaction finalization. Request cancellation is
allowed to stop application work, but rollback or commit and session close run
inside a cancellation shield before the cancellation propagates. This keeps
the tenant GUC transaction-local and returns the connection to its pool even
when a client navigates away or closes a page during a request.

Cancelled asyncpg work invalidates rather than recycles its driver connection.
The engine adapter lets that invalidation finish updating the pool record after
the driver has force-closed the socket; the original request cancellation still
propagates. A closed handle must never remain eligible for a later checkout or
trigger pool-wide invalidation through the next pre-ping.

## Engines and event-loop domains

An asyncpg connection belongs to the event loop that created it. A **pooled**
engine therefore belongs to exactly one loop: the pool caches connections, and a
cached connection handed to a second loop fails on checkout
(`Future attached to a different loop`), corrupts asyncpg's protocol state, and
poisons the pool for every later borrower — including unrelated callers who
share it.

Inqtrix has two kinds of consumer, and they need different engines:

* **The HTTP request path** runs on uvicorn's one persistent loop. Pooling is
  correct and valuable here.
* **Run threads** (`inqtrix-run-*`, `inqtrix-reindex-*`) execute algorithms
  synchronously and reach async stores through `run_coro_sync` or a bare
  `asyncio.run` — a fresh, immediately closed loop *per call*. Only a
  `NullPool` engine, which caches nothing, is safe there.

That is why a single API process holds more than one engine, and why merging
them would be a regression rather than a cleanup:

| Engine | Pool | Loop domain |
|---|---|---|
| Platform bundle (identity, files, skills, quota reads of the request path) | pooled | HTTP loop |
| Auth bundle (sessions, users, PATs) | pooled | HTTP loop |
| Run store, indexing store, deletion and upload operation stores | pooled | each pinned to its OWN dedicated loop (`DurableJobStoreBase`) |
| **Run-thread lane** (`build_run_thread_persistence`) | **NullPool** | any per-call loop |
| Knowledge, control, editor stores; quota recording; usage ledger; account erasure | NullPool | any per-call loop |
| Worker claim-contract probe | NullPool, one engine per probe | throwaway loop (`run_coro_sync`) |
| Agent checkpointer (`PostgresSaver`) | psycopg pool, `min_size=0` | worker threads, never the HTTP loop |

Two ways to make a pooled engine safe are in use: pin it to one long-lived loop
and marshal every call onto it (the job stores), or give the off-loop consumer
its own NullPool engine (everything else). The run-thread lane is the second:
the API keeps its pooled bundle for requests and hands run threads a NullPool
twin. On the worker the whole bundle is already NullPool, so the lane *is* that
bundle — no third engine.

The quota store uses both lanes. Admission, the composer meter (every 30
seconds per visible tab) and the admin overview run only on the request loop,
so they read through the platform bundle's pooled engine, usage and limits in
one session; run threads record spend through the store's own NullPool engine.
The worker re-proves its claim contract once a minute while the database is
healthy and every 5 seconds after an outage, each time on a fresh NullPool
engine, so an idle worker no longer opens a connection every few seconds.

Enforcement is deliberate, because this rule was documented in four places and
broken anyway (v0.2.0.4 wired the pooled auth directory into the run thread's
actor probe, and nothing caught it):

* `tenant_session` warns once per engine when a pooled engine is reached from a
  second loop. It is the one chokepoint every tenant-scoped statement passes,
  so it sees callers that never touch `sync_bridge`.
* `tests/test_enterprise_seams.py` asserts the pool class on the engines reached
  through the real container — offline, with a dummy URL, since
  `create_async_engine` is lazy.

NullPool costs a connect per operation. That is affordable only because the
actor's authority is checked at safepoints (see `agent-platform.md`), not per
emitted event; a per-event check on a NullPool engine would be a connection
storm. The run store does re-check its execution path inside every event's
write transaction, but on its own pooled engine and loop, so an event costs
round trips, not a connection. A finished answer is published in
byte-bounded deltas of at most 2 KB, not word by word, so even a long answer
is a few dozen events rather than thousands.

## Durable jobs and concurrency

Runs and indexing jobs are durable Postgres rows. The dispatch message names
the id, so a worker never scans a queue: it claims that one addressed row
under a blocking `FOR UPDATE` lock and a status CAS, with Valkey used only to
dispatch work to the `inqtrix-worker`. Live SSE reads the durable event rows:
each open stream polls Postgres for new events every 0.3 seconds. This is the right design while worker
concurrency stays modest (order ~100 workers). Beyond that, move the *dispatch*
onto Valkey / Redis Streams (Postgres still owning the durable rows) before
reaching for a dedicated broker. The reindex tier is low-concurrency, so this
is a documented operating regime, not a current divergence.

### Collection maintenance during generation builds

One collection-generation build is serialized per collection. It never
rewrites the active generation in place: the worker snapshots the active
revision manifest, writes chunks and derived vectors under a new
`generation_id`, validates counts, dimensions, source spans, and manifest
identity, then changes the active-generation pointer in one transaction.
Search reads that pointer once and admits only matching active revisions and
generation points; stale Qdrant candidates are over-fetched and discarded
until asynchronous cleanup completes.

The old generation therefore remains readable throughout the build and remains
available for the configured rollback window after publication. Expiry is
handled by the indexing worker's existing reconciliation cadence: the ledger
leaves `rollback_available` before any external vector delete, retries
`deleting`/`cleanup_failed` rows idempotently, verifies zero vector residuals,
and only then removes chunk rows and marks the generation `deleted`. Normal
document revisions may continue while a generation is staged. Before the
pointer switch, the worker compares the staged manifest with the current active
revision set, releases its short publication lock when a delta is needed,
builds that delta, and validates again. A stale document worker publishes only
through its own desired-revision compare-and-swap and ends `superseded` when a
newer intent won.

Collection deletion is the exception: its durable deletion operation fences
active indexing jobs and tombstones the collection out of reads before
physical cleanup. The worker checks the requester's current account and
required access before and after external vector writes and ends as
`authorization_revoked` if authority disappears. Current `view` can inspect
job summaries/events; current `edit` can resume, explicitly choose a raw build,
or cancel. A backend without transactional collection metadata returns 501
rather than presenting process-local state as a security boundary.

## List loading and pagination contract

Lists are split by how they grow:

- **Keyset-paginated** (opaque `(created_at, id)` cursor): `/v1/runs`, the
  per-document knowledge list (`/v1/knowledge/collections/{id}/documents`),
  and every project-tier list endpoint (chat threads/messages, editor
  documents/comments, file assets, vector indexes). These can grow per user,
  so the API exposes `cursor`/`limit`.
- **Intentionally NOT paginated** (carved out, by design):
  - **`/v1/knowledge/collections`**, **`/v1/prompt-templates`**, and
    **`/v1/skills`** are list-all today, an intentional scale ceiling rather
    than an accidental omission. Prompt templates are expected to remain a
    small per-user library of chat rules and saved prompts. The listing is
    also the lazy seeding chokepoint for the stock prompts (Lektor,
    Executive Briefing, Summarizer, Translator): a scoped user's FIRST listing
    installs them once — the per-user marker row
    (`prompt_template_seed_markers`) is claimed atomically with the inserts,
    so a deleted default stays deleted forever and a library that already
    holds templates is never injected into. Unscoped deployments never seed;
    on the memory backend the marker is as volatile as the store itself.
    Each query joins
    owned rows with accepted direct user shares and returns an `access`
    annotation. There is no separate `/v1/shares/shared-with-me` resource union
    and no group expansion. The lifecycle-only `/v1/shares/inbox` and
    `/v1/shares/mine` lists are also intentionally small. These surfaces should
    gain keyset pagination only when observed cardinality warrants the added
    union-cursor contract; silently truncating them is not acceptable.

### Frontend loading policy (and the reference-map constraint)

The frontend loads a list incrementally **only when its entities are not
resolved by id from elsewhere**:

- **Chat threads** have no incoming cross-references (only their own order
  arrays + the selected id), so they load **truly on-demand**: page one first,
  then cursor-based load-more / infinite scroll. The autosave stays safe because
  its delete-detection only diffs *loaded* entities against the synced map — and
  you can only delete what is displayed, so an un-loaded entity is never mistaken
  for a local deletion. A pass diffs against the map as it stood when the pass
  took its snapshot, for threads and for each thread's messages, so what loads
  while the pass waits on a request is not read as deleted; it stops once its
  project or workspace is no longer open. A reply into a thread whose messages
  have not arrived waits for them, so the model gets the conversation and the
  autosave sees the whole thread.
- **Agent runs** list as summaries (status, question, session). Their control
  rows, answer bodies, the session artifact index and the replay of a finished
  run's event history load only for the open session and for child missions
  someone inspects; another session loads when the user opens it. The result
  payload of an agent run is not fetched, because its answer is the answer
  artifact. A result request that is still on its way is shared instead of
  asked again, and so is an artifact body; the body of a pinned artifact
  revision never changes and is fetched once per page.
- **Knowledge answers**: the result of a finished knowledge ask loads once its
  question is in view (its session opened or prefetched), not for every
  finished ask at start; an ask whose question was never loaded costs no
  request.
- **Editor documents** already load their heavy data lazily — the list returns
  metadata only (`content_markdown=""`) and the body loads on open. The metadata
  tree itself is **not** lazily loaded on expand (deferred — see the limitation
  below); instead the editor offers a **client-side title search** that filters
  the already-loaded metadata across all folders (instant, no round-trip, works
  in the local-first tier too).
- **File assets** and **vector indexes** must keep their **metadata resident**:
  `fileAssets` is resolved by id from vector-index members, chat attachments,
  chat-rule context, and group expansion, so a partially-loaded asset map would
  silently break those references (a broken index member, an empty attachment).
  Their metadata is therefore the project's **reference map** — loaded fully but
  non-blocking (no blocking upfront walk; the heavy `extractedText` / vector
  payload stays lazy as always). Vector indexes are few per user and depend on
  assets being resolvable, so they are not paginated either.

Making assets themselves truly on-demand would require turning every
`fileAssets[id]` lookup into a load-aware async resolution (a reference-aware
project-store / resolver) — a deliberate, separate effort, **not** folded into
the metadata-first scale-proofing. Server knowledge collections are always
loaded from `/v1/knowledge/collections`, which is the authoritative
owned-plus-shared resource view. Browser vector-index records remain a
separate local project feature for setup/progress and never synthesize server
collection ownership or sharing state. Knowledge, prompt, skill, and
share-lifecycle lists stay list-all by design and are replaced as complete
server slices by the client, including when the response is empty.

**Known limitation — lazy hydrate vs. the local-full project (re-push).** This
applies to the **manual local→server import path** (apikey / local-first), not to
the authenticated server-first boot. An authenticated cookie session now opens
**server-first** — it starts from an empty state and hydrates from the server
(exactly the "server-first project open" foundation noted below), so there is no
local-full project to re-push. The limitation below is therefore scoped to the
import path and to opening a local markdown into an already-synced project.

On that path, a server-synced project is opened from the local markdown, which
loads **every** entity (chat threads, editor documents with bodies). The autosave
seeds its "what the server holds" (synced) baseline only for entities the lazy
hydrate actually **fetched** (page one), so the first reconciling flush
re-pushes every un-fetched-but-local entity. This is **not** data loss (the
no-spurious-delete invariant holds) and is idempotent + one-time per session.
For chat it is cheap (a hydrated thread carries no loaded messages, so only
thread metadata re-pushes) and is accepted as-is. It is the reason the editor
metadata tree is **not** lazily loaded: doing so would re-push document **bodies**
on every open. Removing the re-push entirely (and enabling a genuinely lazy
editor tree) needs one of two foundations, deferred until planned: a **durable
per-entity last-synced baseline** persisted in the project so hydrate can seed
the full synced map without fetching, or a **server-first project open** where a
synced project hydrates lazily from the server and the local markdown becomes an
export, not the live source.

## Sharing

`resource_shares` is one direct user-to-resource table for **research runs**,
**indexed knowledge collections**, **prompt templates**, and **skill
templates**. Each active row names exactly one `recipient_user_id`, one
`view`/`edit` permission, an integer revision, and consent/revocation
timestamps. The resource table remains the sole source of ownership;
`granted_by_user_id` is audit information, not a second owner.

There are no application sharing groups, derived workspace-role grants,
`comment`/`manage` permissions, or file shares. Files and original upload
binaries remain owner-bound. A shared collection exposes its metadata and
extracted/indexed text, not the source binary. Content an editor ingests into a
foreign collection becomes collection content and remains after that editor
leaves.

List queries join owned rows with accepted direct shares and return an
`access` object (`unscoped`, `owner`, or `shared` plus `view|edit`). A request
never carries a precomputed grant map as authority. Every read checks current
ownership/user/share state; every mutation locks the resource first and the
applicable share second, then checks active user, acceptance, permission,
optional common-workspace restriction, and revision before changing data. The
write, audit row, and user invalidations commit in the same transaction.

### Consent gate

A share is **not active on grant**: `accepted_at=NULL` is pending and grants
nothing; a timestamp means accepted. `POST /v1/shares` first validates the
entire invitee batch (duplicates, active users, workspace boundary, supported
permission, existing active shares) and then writes all rows atomically. An
existing active share is a 409, not an upsert. `PATCH /v1/shares/{id}` changes
permission through `expected_revision` and preserves acceptance for that same
row. Revoking and sharing again creates a new share id with fresh pending
consent; acceptance is never carried across the new grant.

The recipient surface lives behind `/v1/shares`: `GET /v1/shares/inbox` returns
the caller's incoming shares split into `pending` (the consent queue) and
`accepted` (shared with me); `POST /v1/shares/{id}/accept` consents; `DELETE
/v1/shares/{id}` is one verb for two callers — the owner revokes, or the
recipient drops their own share (declines a pending one, leaves an accepted
one). `GET /v1/shares/mine` summarizes what the caller has shared out. These
are lifecycle views only; regular resource lists are the authoritative
owned-plus-accepted-shared views. `/v1/shares/mine` reads the owner and title
of every listed resource with one read per resource type, keyed by the shared
ids and reading no resource body, so its cost does not grow by a database
round trip per shared resource. Ownership still comes from the resource, never
from the share row.

When `restrict_to_workspace_members` is enabled, owner and recipient must have
at least one common workspace at grant, accept, and every live access. Removing
a member or deleting a workspace revokes shares in either direction only when
the last common workspace disappears. Startup reconciles existing shares
before readiness. Disabling the restriction stops future workspace checks but
never resurrects a revoked share.

## User invalidations and authoritative frontend state

`user_events` is a content-free cache-invalidation stream, not an event-sourced
resource model or JSON patch channel. Mutations append rows for affected owners,
recipients, and other active share recipients in the same transaction as the
domain write. A row contains only target user, scope, optional resource type/id,
and time; 24-hour retention is maintained by bounded lazy cleanup.

Each browser tab opens one `GET /v1/user/events` SSE channel. `ready` carries
the canonical `user_id` and cursor; `invalidate` names only what server view to
refetch; `reset` means the cursor fell outside retention. `Last-Event-ID`
replays retained rows, but every connect/reconnect still performs a broad
authoritative refetch. The stream checks the live credential immediately before
each data frame. The browser re-reads only the lists an `invalidate` scope can
change: a run starting or finishing (`runs`) re-reads the run list, not the
skills, templates, collections, editor or share lists. A reconnect, a `reset`,
a scope the client does not know, window focus and coming online re-read every
list.

Within one API process, all waiting streams of a tenant share one poll of
`user_events` per half second. A waiting stream registers its user and cursor;
the poll reads the newest event of each waiting user with one index probe per
user and wakes the streams whose user has an event past their cursor. A wake
carries no data: the woken stream reads its own page. The poll stops as soon as
the last stream of the tenant stops waiting. A failed poll is logged once as a
warning (and its recovery as information) and retried on the next interval;
waiting streams keep their five-second check meanwhile. The poll does not use
`LISTEN`/`NOTIFY`, which a transaction-mode connection pooler does not carry.

Durable deletion operations notify their owner through this same stream when
queued, retried, completed, or failed. The notification commits with the
operation event and carries only the receipt id, never file content or the
deletion manifest. Open file-library views then read the retained deletion
receipts and reconcile affected rows under their existing scope and operation
guards. Discovered operations use their existing status poller for intermediate
progress, so progress updates do not trigger broad resource refreshes.

Status polls the server keeps failing are bounded. The upload status poll and
the deletion receipt polls wait twice as long after each failed read, up to 30
seconds, and stop after ten failed reads in a row. A stopped poll starts again
with a fresh count once the browser is back online or the tab is visible again,
or when the view starts it anew (a new upload, changed rows or sessions, another
project). The error the view already shows then says that the poll stopped.

The frontend treats responses as replacements, not additive patches: missing
shared runs are removed and their streams closed; prompt, skill, and server
collection slices are cleared by a successful empty list; share inbox/mine and
the resource list refresh together. A permission downgrade to `view` preserves
readable content but disables save. A remote revision that meets a dirty local
prompt/skill buffer becomes an explicit conflict, never a silent overwrite.
Login/logout performs a document reload. After bootstrap, every protected SPA
request carries the rendered user's opaque id as an expected-principal
generation. The server compares it with the freshly authenticated principal in
the same dependency resolution and returns `409 principal_changed` before a
stale tab can read or mutate the new account; the SPA then reloads. Quiet user
SSE streams also re-check the principal before keepalives, so an identity switch
cannot remain hidden indefinitely. Session refreshes use generation
and abort guards so a late response for user A cannot modify user B's state.
Authoritative list responses replace the current user's slice, including when
the response is empty; they are never merged into a later login generation.

Before an invalidation leads to any list request, and whenever the window
regains focus or the browser comes back online, the tab probes the canonical
session (`GET /api/auth/session`), because another tab may have changed the
cookie. Only a confirmed identity change reloads the page: an anonymous
session, another user, a `401` or `403` answer, or `409 principal_changed`. An
answer that proves nothing about the identity keeps the rendered page, its open
editors and their unconfirmed local work: a network failure, a timeout, a rate
limit, a server error or any other HTTP failure, a response that is not JSON
(such as a proxy's error page), or JSON without a recognizable identity. The
tab then withholds the refetch, reports why, and probes again after 1, 2, 4, 8,
16, then 30 seconds until the identity is confirmed or changed. A `ready` frame
of the stream that names the same user confirms the identity by itself and ends
a pending retry; one that names another user reloads.

## Hot path on app start

Target ~100–150 ms to interactive: in parallel, fetch identity + account
preferences + the **first page** of the primary list (paginated metadata).
Everything else is lazy / load-on-open. There is no monolithic state rebuild
and no server read-cache — keyset pagination + the client cache + load-on-use
are sufficient. A Redis/Valkey read-cache is a deliberate carve-out, justified
only by repeated identical hot queries (>5 req/s) or >50 ms queries.

## Thresholds

- Binary **>5 MB** → object store (never a Postgres BLOB); **>100 MB**
  mandatory external.
- Extracted text stays in Postgres `TEXT` (TOAST); only **>~100 MB per
  document** would move it to the object store with a Postgres reference.

## Where this lives in code

- Postgres ORM + migrations: `src/inqtrix/storage/*_orm.py`,
  `src/inqtrix/storage/migrations/versions/`.
- Knowledge (Postgres-canonical + Qdrant `VectorIndex` seam):
  `src/inqtrix/knowledge/`, `src/inqtrix/services/knowledge_service.py`.
- Durable jobs + worker: `src/inqtrix/runs/`, `src/inqtrix/worker/`,
  `src/inqtrix/storage/indexing_orm.py`.
- Canonical identity, direct-share mutation guards, and invalidations:
  `src/inqtrix/auth/principal.py`, `src/inqtrix/auth/shares.py`,
  `src/inqtrix/storage/resource_access.py`, `src/inqtrix/user_events.py`, and
  `src/inqtrix/storage/user_events_postgres.py`.
- Project tier: `src/inqtrix/project/` (ports + memory/postgres stores +
  `base_session_store.py`), `src/inqtrix/services/*` (chat/editor/asset/
  vector-index/account-preferences services), `src/inqtrix/server/routers/*`.
- Keyset pagination: `src/inqtrix/pagination.py`.
- Frontend sync: `apps/research-desk/src/features/*/`+`syncCollection.ts`,
  the per-entity `use*HistoryApi` / `useAccountPreferences` hooks, and the
  project-level `useProjectServerImport`.
- Per-user namespace: `users.default_workspace_id`
  (`src/inqtrix/storage/identity_orm.py` + migration `0019`),
  `UserDirectory.resolve_default_workspace` (`src/inqtrix/auth/directory.py`,
  `src/inqtrix/storage/auth_postgres.py`), surfaced as `project_namespace` by
  `session_payload` (`src/inqtrix/auth/oidc.py`); the desk resolves
  `effectiveWorkspaceId` in `ResearchDesk.tsx` from `useAuthSession` +
  `useServerDiscovery`.
- Durable run retention: `run_durable_retention_seconds`
  (`src/inqtrix/settings.py`) wired into `PostgresRunStore` by `build_run_store`
  (`src/inqtrix/server/container.py`); run scoping lives in
  `apps/research-desk/src/features/researchRuns/useResearchRunApi.ts`.

## Platform HTTP contract (v0.2)

### Identity and access annotations

`GET /api/auth/session` exposes the current local user as
`user.id` (`UUID` string). `GET /v1/users/search` returns that same `id` plus
display name and email. Admin, workspace-member, quota, sharing, session, PAT,
audit, and actor contracts all refer to this UUID; external OIDC issuer
and subject values never cross into resource APIs.

Regular lists for runs, knowledge collections, prompt templates, and skills
combine owned and accepted-shared resources. Each record carries one
authoritative access annotation:

```json
{"access":{"mode":"shared","permission":"edit"}}
```

`mode` is `unscoped`, `owner`, or `shared`. `permission` is present only for a
shared record and is `view` or `edit`. The annotation explains the response;
it is not a reusable authorization token. The server re-evaluates access on
every subsequent read or mutation. Missing or unauthorized resources use the
same 404 response to avoid existence disclosure.

### Direct shares

`resource_shares` is a direct user-to-resource lifecycle for `run`,
`knowledge_collection`, `prompt_template`, and `skill_template`. Pending
shares grant no access. `POST /v1/shares` validates the complete invitee batch
before writing; duplicate or malformed recipients return 400 with zero writes,
and an already-active direct share returns 409. A recipient accepts with
`POST /v1/shares/{share_id}/accept`; the owner revokes, or the recipient
declines/leaves, with `DELETE /v1/shares/{share_id}`. A later re-share creates
a new id and requires new consent.

`editor_document` is shareable too, but only once collaboration is enabled on
it. A plain Markdown document has no owner to resolve for sharing, so
`POST /v1/shares` answers `404 Resource not found` — the same answer an
unknown id gets, because the sharing surface deliberately never distinguishes
"exists but you may not" from "does not exist". Call
`POST /v1/editor/documents/{id}/collaboration:enable` first; the browser does
exactly this before it opens the share dialog.

Only the resource owner manages shares. An accepted editor may perform the
resource-specific edit operations but may not delete or re-share the resource.
`GET /v1/shares/inbox` and `GET /v1/shares/mine` are lifecycle views; there are
no `/shared-with-me` or `/outgoing` resource-list endpoints. See
[Authentication modes](../deployment/auth-modes.md#canonical-identity-and-direct-sharing)
for request examples and the endpoint matrix.

### Revisions and imports

Share permission updates, prompt updates, and skill updates use mandatory
integer optimistic concurrency. The client sends `expected_revision`; the
server performs a compare-and-swap, increments `revision` on success, and
returns HTTP 409 with `current_revision` when another editor won. There is no
force-overwrite or timestamp fallback.

`POST /v1/runs/import` requires `source_run_id`, but that value is provenance
and an owner-scoped idempotency key only. The server always generates the
public `run_id`. While an imported run exists, re-importing the same
`source_run_id` for the same owner is idempotent; after retention removes it,
a later import receives a new server id. A historical share therefore cannot
attach to a newly imported report through client-controlled id reuse.

### Collection and file boundary

Sharing a knowledge collection grants access to its metadata and extracted,
indexed text according to `view`/`edit`. It does not share the uploader's
original binary. Files remain owner-bound and there is no generic file-share
contract. A shared editor may ingest extracted content into the collection;
that content becomes part of the collection even if the editor later leaves,
while the source binary remains owned by its uploader.

## Related docs

- [Editor collaboration architecture](editor-collaboration.md) — Yjs truth, durable update flow, suggestions, and failure semantics.
- [Authentication modes](../deployment/auth-modes.md) — canonical users,
  sessions, admin routes, and the direct-share lifecycle.
- [Knowledge engine](knowledge-engine.md) — collection authorization,
  reindex maintenance, and the Postgres/Qdrant split.
- [React UI](../deployment/react-ui.md) — authoritative client replacement and
  invalidation handling.
