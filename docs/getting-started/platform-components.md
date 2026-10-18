# Platform components — do you need this?

> Files: `deploy/compose/compose.stack.yaml`, `deploy/compose/compose.dev-ports.yaml`, `src/inqtrix/storage/migrate.py`, `src/inqtrix/worker/__main__.py`, `src/inqtrix/settings.py`

## Scope

Inqtrix runs with **zero infrastructure** by default (in-memory storage, in-memory queue, knowledge engine off). Each platform component - Postgres, an object store, Qdrant, Valkey + workers, a collaboration sidecar, or an identity provider - is opt-in and unlocks specific features. This page answers *which components you actually need*, maps every user-facing feature to its requirements, and shows the manual (Framework-Mode) setup. For the one-command Stack-mode path see [Stack quickstart](stack-quickstart.md).

## Do you need this?

| Your situation | What to run |
|---|---|
| Trying it out / single user, no persistence | Nothing extra — zero-infra default ([First research run](first-research-run.md)) |
| Single-user setup, data survives restart | Stack mode default: **Postgres** (+ API + web) |
| Cited answers over your own documents | + **Qdrant** (`--profile knowledge`) for persistent/hybrid retrieval |
| File uploads | + an **object store** (local volume by default; bundled or managed **S3**) |
| Many concurrent runs / runs that survive an API restart | + **Valkey** and worker processes (`--profile workers`) |
| High database connection fan-in | optionally + **PgBouncer** (`--profile pgbouncer` and an explicit pooler runtime DSN) |
| Multiple people editing one editor document | + **Postgres**, cookie auth, and the private **collaboration** service (`--profile collaboration`) |
| Enterprise SSO | + an external **OIDC IdP** (configuration only), or bundled Dex for local validation (`--profile oidc`) |

## Feature → requirements matrix

Which feature needs which component and which auth mode. The capability endpoint (`/v1/capabilities`) reports each flag's actual state so the UI degrades visibly; for S3 and Qdrant it also accounts for whether the configured backing service is reachable.

| Feature | Auth mode | Storage | Object store | Qdrant | Valkey/worker | Capability flag |
|---|---|---|---|---|---|---|
| Research runs (core) | any | memory ok | – | – | – | (always on) |
| Durable runs (survive restart) | any | **postgres** | – | – | optional | (implicit) |
| Scaled / queued runs | any | **postgres** | – | – | **valkey + worker** | (implicit) |
| Knowledge / RAG | any | memory ok | – | qdrant (memory ok) | – | `features.knowledge` |
| Hybrid retrieval + reranking | – | – | – | **qdrant** + reranker | – | `features.hybrid_retrieval` / `reranker` |
| File uploads / attachments | any | postgres | **local or s3** | – | – | `features.files` |
| Prompt templates (durable) | any | **postgres** | – | – | – | `features.prompt_templates` |
| Multi-user / invitations | oidc / local | **postgres** | – | – | – | (implicit) |
| Sharing (runs/collections/templates) | oidc / local | **postgres** | – | – | – | `features.sharing` |
| Live editor collaboration | oidc / local | **postgres** | – | – | not used | `features.collaboration` plus root `collaboration.service_available` |

Note: multi-user, invitations, and sharing require a cookie-session auth mode — `local` (email/password) or `oidc` — plus the Postgres backend. The single-operator `none`/`apikey` modes have no scoped identity and therefore no sharing; for single-user-with-sharing run `local` with one owner.

For administrators, `GET /v1/admin/system/runtime` exposes the same component
choices as a sanitized runtime manifest for the System settings page. It reports
categories only (for example Postgres vs memory, local volume vs S3, Qdrant vs
memory, Valkey worker dispatch vs in-process execution), plus read-only
availability booleans for the optional backing services. It never returns URLs,
paths, bucket names, or credentials. The worker row describes configured
dispatch mode and queue reachability, not live worker replica count.

## Component glossary

Each component states what it enables, the env switch, and what you lose without it.

- **Postgres** (`INQTRIX_STORAGE_BACKEND=postgres`, `INQTRIX_DATABASE_URL=...`) — PostgreSQL 15+ stores durable run rows, identity, knowledge metadata and durable prompt templates; used relationally only (vector search lives in Qdrant or in-memory, no pgvector or other extension needed). Without it: everything is in-memory and lost on restart. The Stack-mode compose defaults to Postgres. Its automatic one-shot migration dependency creates the restricted `inqtrix_app` role; managed services use a separate direct migration credential and explicit RLS authority. API readiness and workers verify Alembic head plus the restricted role before serving/claiming. See [Database migrations](../deployment/database-migrations.md).
- **Object store** (`INQTRIX_OBJECT_STORE_BACKEND=local|s3`) — storage for uploaded file blobs. `local` (default) writes to a volume; `s3` can be bundled SeaweedFS/MinIO, static-key compatible storage, or AWS-native workload identity through boto3's default chain. If S3 is unreachable, file uploads are not advertised (`features.files=false`), file requests return a stable 503, and `/readyz` is degraded without removing unrelated API traffic. See [Object storage](../deployment/object-storage.md).
- **Qdrant** (`--profile knowledge`, `INQTRIX_VECTOR_BACKEND=qdrant`, `INQTRIX_QDRANT_URL=...`) — persistent vector/document store with hybrid dense + BM25 retrieval; the BM25 sparse vectors are computed inside the Qdrant server (core inference, server >= 1.15.3 — an older external server fails fast at startup). The knowledge engine otherwise uses an in-memory store (lost on restart, dense-only). If Qdrant is configured but unreachable, knowledge and hybrid retrieval are not advertised. Self-hosted Qdrant is unauthenticated by default — set `INQTRIX_QDRANT_API_KEY`.
- **Valkey + worker** (`--profile workers`, `INQTRIX_QUEUE_BACKEND=valkey`, `INQTRIX_VALKEY_URL=...`) — dispatches native runs to separate worker processes for horizontal scaling and restart survival. The API/run store and worker use the same queue path for initial runs, child completion wakes, and resumed parents; the reconciler is a safety net, not normal dispatch. The default `memory` queue runs in-process. The worker refuses to start without Postgres + Valkey and validates the schema/role contract before claiming its first message. Only the Redis protocol is required (Redis 6.2 is the floor), so the broker may be bundled or external and may run Valkey or Redis — see [Queue broker](../deployment/kubernetes.md#queue-broker).
- **PgBouncer** (`--profile pgbouncer` plus a visible runtime DSN targeting
  `pgbouncer:6432`) — optional transaction pooling for deployments with high
  database connection fan-in. It is never enabled automatically and is not a
  prerequisite for Postgres, workers, collaboration, or normal multi-user
  operation. Migrations continue to connect directly to PostgreSQL.
- **Editor collaboration service** (`--profile collaboration`, `INQTRIX_COLLABORATION_ENABLED=true`) - a private, single-replica Node/Hocuspocus coordinator for Yjs updates, suggestions, carets, and durable acknowledgements. FastAPI remains the policy and persistence authority; the service has no database credentials, host port, or data volume. See [Deploy editor collaboration](../deployment/editor-collaboration.md).
- **OIDC IdP** (`INQTRIX_AUTH_MODE=oidc`) — browser SSO. Point the normal
  stack directly at an external IdP without a profile. `--profile oidc`
  starts the bundled Dex development reference only. See
  [Auth modes](../deployment/auth-modes.md).

## The "full experience" profile

To turn on the application features backed by bundled services, run the
`knowledge` + `workers` profiles and set the matching environment in
`deploy/.env.stack`. Add `s3` only when using the bundled SeaweedFS service;
managed/native S3 needs configuration but no S3 profile. PgBouncer is a
separate, optional capacity component rather than a feature prerequisite:

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  --profile knowledge --profile workers up -d --build
```

Visible configuration (`deploy/.env.stack`):

```dotenv
INQTRIX_STORAGE_BACKEND=postgres        # durable runs, templates, identity
INQTRIX_OBJECT_STORE_BACKEND=local      # or managed/native s3 without a profile
INQTRIX_KNOWLEDGE_ENABLED=true          # knowledge engine
INQTRIX_VECTOR_BACKEND=qdrant           # persistent/hybrid retrieval
INQTRIX_QDRANT_URL=http://qdrant:6333
INQTRIX_QUEUE_BACKEND=valkey            # scaled/durable run execution
INQTRIX_VALKEY_URL=redis://:${INQTRIX_VALKEY_PASSWORD}@valkey:6379/0
```

Credentials (`deploy/.env.stack.secrets`):

```dotenv
INQTRIX_QDRANT_API_KEY=...
INQTRIX_VALKEY_PASSWORD=...
```

Add the `collaboration` profile separately when the full experience includes
shared editor documents. It requires the feature flag, an independent secret,
and cookie auth; it does not reuse the workers' Valkey service. Misconfiguration
fails loudly at startup (for example `INQTRIX_QUEUE_BACKEND=valkey` without a
URL, or collaboration without Postgres and cookie auth).

## Manual / host platform (Framework Mode)

If you run the API on the host instead of in the Stack-mode container (library
mode, custom providers, integration tests), select only infrastructure services
from the canonical stack and add the development project-name/loopback-port
override:

This local Framework-mode example has no orchestrator, so the explicit
`inqtrix-migrate` call is intentional. Production Compose and Helm deployments
run the packaged command automatically in their one-shot migration service/job;
operators should not execute it manually during a normal rollout.

```bash
# Infrastructure only (no api/web container):
docker compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  --profile knowledge --profile workers \
  up -d postgres qdrant valkey

# Install optional backends with uv:
uv sync --extra knowledge-qdrant --extra queue-valkey          # backends that need extras

# Postgres + migrations, then the API on the host:
export INQTRIX_STORAGE_BACKEND=postgres
export INQTRIX_DATABASE_URL="postgresql+asyncpg://inqtrix:<local-password>@127.0.0.1:5432/inqtrix"
uv run inqtrix-migrate
uv run python -m inqtrix

# Workers (separate shells, same .env) when INQTRIX_QUEUE_BACKEND=valkey:
uv run inqtrix-worker
```

The same host processes after a normal Python installation:

```bash
python -m pip install -e ".[knowledge-qdrant,queue-valkey]"
python -m inqtrix.storage.migrate
python -m inqtrix
python -m inqtrix.worker
```

There are no committed development credentials. The named local secret file
is ignored and must remain mode `0600`. Image policy, Podman setup, and volume
details: [Local infrastructure](../development/local-infrastructure.md).

## Verify

```bash
curl http://localhost:5100/health          # or :8080 through the Stack-mode web container
curl http://localhost:5100/v1/capabilities # the feature manifest
```

## Related docs

- [Stack quickstart](stack-quickstart.md)
- [Runbooks](../deployment/runbooks.md)
- [Deployment modes](../deployment/deployment-modes.md)
- [Settings and env](../configuration/settings-and-env.md)
- [Local infrastructure](../development/local-infrastructure.md)
