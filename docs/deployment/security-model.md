# Security model

> Files: `src/inqtrix/server/security.py`, `src/inqtrix/server/routes.py`, `src/inqtrix/auth/api_key.py`

## Scope

The security boundary of a running deployment, in two halves. The first is what
you switch on: TLS at the uvicorn layer, Bearer API-key authentication, and a
CORS allow-list, all off by default and driven by `ServerSettings`. The second
is what holds whether you switch anything on or not: which identity carries
authority, how resource access is decided and re-decided, when revocation takes
effect, and which processes are allowed to hold the migration and object-store
credentials.

Authentication is mode-driven (`none` | `apikey` | `local` | `oidc`); this page
covers the `apikey` gate. For the full mode matrix, the inference rule, and
browser sessions see [Authentication modes](auth-modes.md).

> Inqtrix is explicitly experimental. The root `README.md` disclaimer applies.
> Secure configuration, hardening, deployment architecture, access control,
> secret handling, and compliance remain the operator's responsibility.

## The three layers

### TLS termination in uvicorn

Set both `INQTRIX_SERVER_TLS_KEYFILE` and `INQTRIX_SERVER_TLS_CERTFILE` to local PEM file paths. Both are required; setting only one raises `RuntimeError` on startup (no silent fallback).

The example stack scripts under `examples/webserver_stacks/` resolve the pair and hand it to `uvicorn.run(..., ssl_keyfile=..., ssl_certfile=...)`; `python -m inqtrix` does not wire TLS into uvicorn, so terminate TLS at a reverse proxy (nginx, Traefik, Azure Application Gateway) or start the server through a stack script — built-in TLS exists for small self-hosted setups where a proxy would be overkill.

Editor guest links require an HTTPS `INQTRIX_PUBLIC_BASE_URL` at startup (guest tokens and passwords travel with every request). For plain-HTTP local development only, `INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP=true` converts the hard startup failure into a loud startup WARNING and drops the `Secure` flag from the guest cookies so browsers accept them over http. The switch is not for production.

### Bearer API key

Set `INQTRIX_SERVER_API_KEY` to a random string. With `INQTRIX_AUTH_MODE` unset, a non-empty key selects the `apikey` mode (the inference rule in [Authentication modes](auth-modes.md)). The server then enforces the Bearer gate on every principal-gated route — that is, on everything except the open endpoints named below. Which routers are mounted depends on the active feature gates; besides `/v1/chat/completions`, `/v1/text/improvements`, `/v1/editor/*`, `/v1/runs*`, and `/v1/test/run` the gate typically also covers `/v1/files*`, `/v1/uploads*`, `/v1/knowledge/*`, `/v1/sources/*`, `/v1/knowledge-sessions*`, `/v1/agent/*`, `/v1/agent-sessions*`, `/v1/assets*`, `/v1/deletion-operations*`, `/v1/prompt-templates*`, `/v1/skills*`, `/v1/vector-indexes*`, and `/v1/account/*`; the cookie session modes add `/v1/users*`, `/v1/shares*`, `/v1/workspaces*`, `/v1/admin/*`, `/v1/quota*`, and `/v1/usage*`:

```http
Authorization: Bearer <key>
```

Comparison uses `hmac.compare_digest` (timing-safe). `/health`, `/readyz`, `/v1/models`, `/v1/capabilities`, and `/api/auth/config` remain open for liveness, readiness, and pre-login discovery; `/health` also carries the project legal/source metadata used by UIs. `/v1/stacks` (multi-stack apps) also stays open because UIs need it before prompting for credentials. `/metrics` is mounted only with `INQTRIX_METRICS_ENABLED=true` and reuses `INQTRIX_SERVER_API_KEY` as its Bearer guard — without a key it is unauthenticated (the startup log says so) and must stay cluster-internal.

### CORS allow-list

Set `INQTRIX_SERVER_CORS_ORIGINS` to a comma-separated list of origins:

```dotenv
INQTRIX_SERVER_CORS_ORIGINS=https://ui.example.com,https://admin.example.com
```

- `*` is accepted but WARNs on startup, because browsers reject `Access-Control-Allow-Origin: *` together with `allow_credentials=True`.
- The built-in policy allows the `Authorization`, `Content-Type`, `Last-Event-ID`, `X-CSRF-Token`, `X-Inqtrix-Expected-User-Id`, and `X-Inqtrix-Workspace-Id` request headers, so Bearer tokens, SSE reconnects, and the browser session headers pass through. It restricts `allow_methods` to `GET`, `POST`, and `OPTIONS` — cross-origin `PATCH` and `DELETE` are not permitted, so a browser UI on a different origin cannot reach the routes that use them.

### Editor collaboration WebSocket

The optional editor transport has a separate, fail-closed boundary. Browsers
connect only to same-origin `/collaboration`; the Vite development proxy or
production web adapter (the Python gateway, unless another is explicitly
selected) forwards it to FastAPI, which validates `Origin` and relays binary
frames to private Node. `INQTRIX_COLLABORATION_ALLOWED_ORIGINS` adds explicit
origins when TLS termination or a trusted external frontend makes derived
same-origin insufficient. CORS configuration does not replace this WebSocket
Origin check. Node must have no public host port or database credentials.
See [Deploy editor collaboration](editor-collaboration.md#security-and-limits)
for the collaboration secret, the default limits, and close codes.

## What is not covered

All of these are explicit non-goals for this family of settings. They are tracked as follow-up tasks:

- Rate limiting on the business routes, and per-API-key throttling. Failed `local` logins are already throttled per `(identifier, client IP)` and are on by default (`INQTRIX_LOGIN_RATE_LIMIT_*`), and password-protected editor guest links carry their own limiter; those login counters are process-local, so a multi-replica deployment still throttles per IP at the reverse proxy or WAF.
- Multi-key rotation lists.
- Selfsigned cert helpers (`mkcert`, `openssl req -x509 ...`) — there is no built-in generator.

If any of these is a hard requirement, a reverse proxy or an API gateway in front of Inqtrix is the recommended route.

Covered elsewhere: OIDC browser sessions are a first-class auth mode ([Authentication modes](auth-modes.md)); durable run persistence across restarts is provided by `INQTRIX_STORAGE_BACKEND=postgres` together with the `valkey` queue backend ([Settings and env](../configuration/settings-and-env.md)).

Request tracing and correlation IDs are covered: every request carries a validated or freshly minted request id, bound into the log context and echoed as the `X-Request-ID` response header, and the same middleware opens an OpenTelemetry server span that continues an incoming `traceparent`.

## Operator-visible behaviour

The lifespan log records the active layers on startup in a single line:

```text
Inqtrix server starting | llm=... ready=True | search=... ready=True | report_profile=compact | max_concurrent=100 | run_max_concurrent=100 | run_queue_max_size=100 | run_completed_ttl_seconds=300 | api_key_gate=on | auth_mode=apikey | cors=on
```

`api_key_gate` reflects whether the resolved auth mode is `apikey`; `auth_mode` names the resolved mode (`none` | `apikey` | `local` | `oidc`). Any deviation from the expected line is a deployment mistake; fail the smoke test fast.

## Identity, authorization, and revocation boundaries

Multi-user deployments use the local `users.id` UUID as the only authority
identifier. OIDC issuer and subject values are authentication-adapter
provenance, not resource ids. Session and PAT verification resolves the user
and checks that the account is still active on every request. Disabling a user
revokes their sessions and PATs; enabling the user later does not reactivate
those credentials.

Resource access is owner-or-direct-share. Accepted shares for runs, knowledge
collections, prompt templates, and skill templates remain `view`/`edit`.
Collaboration editor documents additionally permit `suggest`; this does not
expand the permission vocabulary of other resource types. Workspace roles do
not imply resource rights, and files are owner-bound. Services re-check the
resource owner, active user, current share, permission, and optional
common-workspace restriction at the access boundary.
Long-running work repeats those checks at safepoints. The run and indexing
SSE streams gate every data frame and quiet keepalive on a commit-ordered
per-user authorization generation: every permission-relevant mutation
(share change, workspace membership, user disable, password reset, session
deletion, PAT revocation) advances it inside its own transaction, and a
moved value re-runs the full authorization chain before the next frame.
Session or PAT EXPIRY writes no mutation, so a bounded time ceiling (well
below a minute) re-runs the full chain on time alone — that ceiling is the
revocation bound for expiry. The per-user event stream re-checks before
each data frame as before. None of this claims that bytes already sent to
a browser or an external provider can be recalled.

Erasing an account (`DELETE /v1/admin/users/{id}`, instance admins with a
browser session only) cuts the account off in the transaction that starts
the erasure: its sessions end, its tokens are revoked, its password is
refused, and every later sign-in, enable or role change of the account is
refused while the erasure runs. An account being erased never counts as an
administrator, neither for the last-admin invariant nor for administrative
notifications. The deletion worker then removes the account's data as its
owner, through the same deletion operations and access checks as the owner's
own delete; at that point nobody else holds a session or token for the
account. The audit log keeps its entries under the actor's pseudonym, and
every source the erasure deleted keeps its content-free tombstone, so no late
request can bring it back. See
[Erasing an account](create-and-manage-users.md#erasing-an-account).

Deleting a source (an uploaded file, a knowledge document or a collection)
fences it as soon as the deletion operation is registered: from then on no
run can open or search it, a later turn that tries to reopen a run transcript
or context archive that read from it finds that source listed as unavailable,
and a run that had taken such a transcript into its own context stops at its
next model call. Deletion does not rewrite what runs already took from the
source. A run that is still executing keeps the text it has read until it
ends, and the conversation keeps it afterwards: kernel transcripts, context
archives, published answers, memos, deliverables and the question-and-answer
history that follow-up turns receive. With the Postgres backend, runs of a
saved Agent Desk session are exempt from `RUN_DURABLE_RETENTION_SECONDS` and
keep this text until the session is deleted, which removes it together with
the session's checkpoints; other runs, Knowledge Desk runs included, are
removed by that retention or by explicit deletion. An incognito Knowledge
Desk ask is removed once `RUN_INCOGNITO_RETENTION_SECONDS` (default 15
minutes) have passed since it ended, within about a minute: the API process
sweeps the run store every minute, on top of the cleanup every access runs.
A saved Knowledge Desk
session keeps its answers, with the excerpts and quotes they cite, in the
session itself until that session is deleted; run retention does not touch
it. Span content captured for
tracing (`INQTRIX_TRACE_CONTENT=on`, or `auto` with
`OBSERVABILITY_PROFILE=forensic`) is not touched by either deletion; an
incognito chat or Knowledge Desk ask (`"incognito": true`) captures none in
the first place. With
`INQTRIX_TRACING=otlp` and a Langfuse destination, the worker deletes traces
older than `INQTRIX_TRACE_RETENTION_DAYS` (`0` disables this); the file spool
is bounded by its size cap instead, and any other OTLP destination keeps
traces under its own retention.

Postgres mutations that combine authorization, a resource write, audit, and
cache invalidation perform them in one transaction. Security invariants use
targeted row locks: a tenant security row serializes first-admin and
last-admin decisions; a workspace row serializes last-owner changes; resource
and direct-share rows follow one resource-then-share lock order, and an
update, a lease issue or rotation, deleting a document and revoking a guest
link lock the editor document's row before its collaboration leases; a
guest's lease issue takes that row before it checks the guest's access, so
a completed revocation, rotation or deletion leaves no guest lease live,
and a guest's comment mutation checks the guest's access only after it
locks that row, so no guest comment lands after a completed revocation or
rotation. A lease introspection that finds the lease's access gone revokes
the lease, and the revocation commits with the refusal.
Revoking a guest link or rotating its password or token, opening a guest
session and refreshing one lock the rows each of them needs in one order
(the editor document, the link, the guest identity), and a refresh locks
the identity only in the mode of its own update, which a guest lease's
foreign-key check does not wait for. Operators
should run the PostgreSQL-backed race tests against a disposable database as
part of release qualification; an offline suite that skips those tests does
not validate these concurrency guarantees.

## Database migration and object-store identities

Production PostgreSQL uses a restricted runtime path and a separate direct
migration identity. The migration Secret is available only to the one-shot job;
API/worker must never receive it, and Alembic must not traverse PgBouncer
transaction pooling. `bypass` mode requires a dedicated `BYPASSRLS` login;
`owner` mode requires table ownership, an exclusive maintenance boundary and
an explicit operator assertion. Runtime readiness rejects a stale Alembic head,
a failed `SET ROLE`, or an effective role that is superuser, `BYPASSRLS`, or an
RLS-table owner. The effective role also needs the exact least-privilege grant
set, and the live probe requires enabled/active RLS and a writable transaction.
Missing dependencies, read-replica DSNs or excess grants keep API and workers
not ready; the grant contract and the bootstrap SQL are on the migrations page.
See [Database migrations](database-migrations.md).

Managed S3 credentials follow the same least-privilege split. Static keys and
STS tokens are injected only into API/worker; workload identity uses only their
component ServiceAccounts. Web, Collaboration, migration and smoke-test pods
remain tokenless and receive neither the S3 Secret nor CA bundle. TLS
verification cannot be disabled, and uploads set no object ACL. See [Object
storage](object-storage.md).

The migration DSN follows a stricter split: Helm rejects reuse of the runtime
application Secret for `migrations.databaseSecret`, and API/worker explicitly
override `INQTRIX_MIGRATION_DATABASE_URL` to an empty value as defense in depth.
For `serviceAccount.create=false`, provide an existing unannotated internal
ServiceAccount name; component API/worker accounts remain the only place for
cloud-identity annotations.

## Testing hardening locally

1. Generate a throwaway cert (example):

   ```bash
   mkcert -install
   mkcert -key-file key.pem -cert-file cert.pem localhost
   ```

2. Set all three layers in `.env`:

   ```dotenv
   INQTRIX_SERVER_TLS_KEYFILE=./key.pem
   INQTRIX_SERVER_TLS_CERTFILE=./cert.pem
   INQTRIX_SERVER_API_KEY=dev-secret-xxxxx
   INQTRIX_SERVER_CORS_ORIGINS=https://localhost:3000
   ```

3. Start the server and smoke-test with `curl -k`:

   ```bash
   curl -kN https://localhost:5100/health
   curl -kN -H "Authorization: Bearer dev-secret-xxxxx" \
        https://localhost:5100/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"research-agent","messages":[{"role":"user","content":"hi"}],"stream":true}'
   ```

The first call should return 200 without auth; the second should stream.

## Related docs

- [Authentication modes](auth-modes.md)
- [Deploy editor collaboration](editor-collaboration.md)
- [Settings and env](../configuration/settings-and-env.md)
- [Runbooks](runbooks.md)
