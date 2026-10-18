# Local infrastructure

## Scope

How to expose selected services from the canonical stack on loopback with
Podman and Compose: the image policy for third-party containers, starting and
stopping services, applying schema migrations, and running the
database-gated integration tests. The platform layer persists identity
facts (workspaces, memberships, shares, audit log) in PostgreSQL when
`INQTRIX_STORAGE_BACKEND=postgres`; the default remains `memory` — no
external services, the full test suite runs offline. This page does
not cover production deployment or the research agent's own
configuration (see [Related docs](#related-docs)).

## Image policy

Binding rules for every container image in this repository:

1. **Trusted-content tiers only.** Docker Official Images
   (`docker.io/library/*`) first; where no official image exists, the
   image the upstream project publishes itself, in its own Docker Hub
   or GitHub Container Registry namespace — `docker.io/valkey/valkey`
   for the job queue (there is no `library/valkey` and there will not
   be one), `docker.io/qdrant/qdrant`, `docker.io/chrislusf/seaweedfs`,
   `ghcr.io/cloudnative-pg/pgbouncer`, `ghcr.io/dexidp/dex`. Both tiers
   are actively maintained and rebuilt for base-image CVEs. Nothing
   gets built from scratch, nothing from unknown publishers.
2. **Fully qualified names.** Always `docker.io/library/postgres`,
   never bare `postgres` — Podman has no implicit registry and
   short-name resolution differs per host.
3. **Tag + digest pinned.** `postgres:18.4-trixie@sha256:...` — the
   digest (multi-arch manifest list, not a platform digest) is what is
   enforced; the tag documents intent. Official images are rebuilt
   every few weeks for base-image CVE fixes, so a pinned digest goes
   stale: refresh it deliberately when bumping, via
   `skopeo inspect docker://docker.io/library/postgres:18.4-trixie`
   or the registry API.
4. **One service per container.** No multi-process containers, no
   shared pods for unrelated services — compose orchestrates.

## Why a compose file (not a Podman pod / kube YAML)

`podman kube play` ignores readiness/startup probes and has no
equivalent of `depends_on: condition: service_healthy`; compose is the
runtime-agnostic dev format and the Kubernetes manifests arrive
separately with the deployment phase. `podman compose` delegates to an
external provider — install docker-compose v2 for the best
compose-spec coverage (podman-compose has known health-gating bugs):

```bash
brew install docker-compose   # provider only; the runtime stays Podman
podman machine start          # forwards the API socket
```

## Start / stop

Create a local pair once. Keep every host, port, backend, and complete DSN in
the visible config; keep credentials in the single mode-`0600` secrets file.
For named pairs, update `INQTRIX_ENV_FILE` and `INQTRIX_SECRETS_FILE` in the
config so they point to these exact root-relative paths.

```bash
cp deploy/.env.stack.example deploy/.env.stack.local
cp deploy/.env.stack.secrets.example deploy/.env.stack.secrets.local
chmod 0600 deploy/.env.stack.secrets.local
# edit both files and set their INQTRIX_*_FILE pointers
```

Edit or replace existing assignments in the copied templates; do not append a
duplicate key. The deployment preflight deliberately rejects duplicate keys in
both files rather than silently selecting one value.

The development override selects the isolated Compose project name
`inqtrix-dev` and publishes ports only. Images, volumes, commands, and health
checks remain defined once in `compose.stack.yaml`. The optional CLI applies
the same project name automatically with `--dev-ports`; an explicit
`--project-name` still wins:

| Service | Loopback default | Override |
|---|---:|---|
| PostgreSQL | `5432` | `INQTRIX_PG_PORT` |
| FastAPI | `5100` | `INQTRIX_API_PORT` |
| Qdrant HTTP / gRPC | `6333` / `6334` | `INQTRIX_QDRANT_PORT` / `INQTRIX_QDRANT_GRPC_PORT` |
| Valkey | `6379` | `INQTRIX_VALKEY_PORT` |
| SeaweedFS S3 | `8333` | `INQTRIX_S3_PORT` |
| Collaboration sidecar | `1234` | `INQTRIX_COLLABORATION_PORT` |

Every published address remains hard-bound to `127.0.0.1`; an override changes
only the host port.

```bash
podman compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  up -d postgres

podman compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  ps

# Keep data:
podman compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  down
```

`down --volumes` irreversibly removes the project volumes and should be used
only for an intentional reset. The default database user is `inqtrix`, the
default database is `inqtrix`, and the port override binds `5432` to loopback.
The password has no repository default. Postgres data lives in the named volume `pgdata`
(inside the Podman machine VM — fast, no macOS bind-mount permission
issues). Note the postgres:18 mount point `/var/lib/postgresql`; do
not reuse pre-18 volumes mounted at `/var/lib/postgresql/data`.

## Migrate and run against Postgres

```bash
export INQTRIX_DATABASE_URL="postgresql+asyncpg://inqtrix:<local-password>@127.0.0.1:5432/inqtrix"
uv run inqtrix-migrate                       # apply schema (Alembic head)
INQTRIX_STORAGE_BACKEND=postgres uv run python -m inqtrix
```

The same host workflow after a normal pip installation:

```bash
python -m pip install -e .
python -m inqtrix.storage.migrate
INQTRIX_STORAGE_BACKEND=postgres python -m inqtrix
```

The migrations create the restricted `inqtrix_app` role
(NOLOGIN/NOSUPERUSER/NOBYPASSRLS); every application transaction
switches to it via `SET LOCAL ROLE`, so row-level security applies
even though the dev connection user owns the tables. Tenant context is
transaction-local (`set_config('inqtrix.tenant_id', ..., true)`); a
query without it fails loudly in `inqtrix_current_tenant_id()`.

## Database-gated tests

The default suite never needs a database. The Postgres integration
tests run only when a test database is configured:

```bash
export INQTRIX_TEST_DATABASE_URL="postgresql+asyncpg://inqtrix:<local-password>@127.0.0.1:5432/inqtrix_test"
podman exec -it inqtrix-dev-postgres-1 createdb -U inqtrix inqtrix_test
uv run pytest tests/storage/ -v
# or, after `python -m pip install -e ".[dev]"`:
python -m pytest tests/storage/ -v
```

The fixtures migrate the test database to head once per session and
never roll the schema back — they only clear the rows they own, and
parts of the migration chain refuse to downgrade at all; never point
`INQTRIX_TEST_DATABASE_URL` at a database with real data.

## Durable runs and the worker queue (Valkey)

Run records, events, and results become durable with the Postgres
backend alone — no extra service, execution stays in-process; start it as
shown under
[Migrate and run against Postgres](#migrate-and-run-against-postgres).

Moving run EXECUTION to separate worker processes additionally needs
the Valkey job stream (service `valkey` in the compose stack, AOF
persistence enabled, loopback-only, password mandatory):

```bash
export INQTRIX_STORAGE_BACKEND=postgres
export INQTRIX_DATABASE_URL="postgresql+asyncpg://inqtrix:<local-password>@127.0.0.1:5432/inqtrix"
export INQTRIX_QUEUE_BACKEND=valkey
export INQTRIX_VALKEY_URL="redis://:<local-valkey-password>@127.0.0.1:6379/0"   # the INQTRIX_VALKEY_PASSWORD from the selected secrets file

uv run python -m inqtrix          # API replica(s): accept + persist + enqueue
uv run inqtrix-worker             # worker replica(s): claim + execute
```

With a normal pip installation, use `python -m inqtrix` and
`python -m inqtrix.worker` instead.

Semantics worth knowing as an operator:

* The Postgres run row is the source of truth; the stream carries only
  dispatch messages. Delivery is at-least-once — guarded status
  transitions (`queued -> running` compare-and-set, fenced terminal
  writes) make redelivery harmless, and the worker's reconciler
  re-enqueues rows whose dispatch message was lost.
* Cancellation crosses processes via the run row
  (`cancel_requested`), polled by the executing worker and observed at
  graph node boundaries — exactly the in-memory two-phase semantics.
* SIGTERM stops claiming and drains in-flight runs (90s); undrained
  runs are NOT cancelled — heartbeat silence hands them to another
  worker. Per-worker parallelism: `INQTRIX_WORKER_CONCURRENCY`
  (default 100). It sizes the research loop directly; the indexing,
  upload and deletion loops each run at the lower of it and their own
  ceiling, and a ceiling that binds is named in the startup log.

## OIDC login (Dex reference IdP)

The bundled Dex reference IdP for `INQTRIX_AUTH_MODE=oidc` is gated
behind the `oidc` compose profile, so the default stack stays auth-free:

```bash
podman compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  --profile oidc up -d dex
```

Demo login: `admin@example.com` / `password` (see
`deploy/compose/dex-config.yaml`; mint new bcrypt hashes with
`htpasswd -bnBC 10 "" 'pw' | tr -d ':\n'`). The server env block, the
browser login flow, and the curl verification are in the
[OIDC stack walkthrough](../../examples/webserver_stacks/oidc_stack.md).

## Object storage (SeaweedFS)

The compose stack includes SeaweedFS (`docker.io/chrislusf/seaweedfs`,
the official project image, Apache-2.0) as the S3-compatible blob
store for the file feature. One container runs master + volume +
filer + S3 gateway; the named volume `seaweedfs_data` holds blobs AND
the embedded filer metadata — back up the whole volume, never blobs
alone. The S3 gateway receives `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` from the selected stack secret file. Its startup guard
refuses the `s3` profile when either value is absent, so a missing
configuration cannot silently expose an anonymous object store.

```bash
export INQTRIX_OBJECT_STORE_BACKEND=s3
export INQTRIX_S3_ENDPOINT_URL="http://127.0.0.1:8333"
export INQTRIX_S3_ACCESS_KEY=inqtrix-dev-access
export INQTRIX_S3_SECRET_KEY=inqtrix-dev-secret
```

Without any object-store configuration the file feature uses the
`local` backend (a directory below `INQTRIX_OBJECT_STORE_PATH`,
default `data/object-store`) — no container needed. The store is
never exposed to clients in either mode: downloads stream through
`GET /v1/files/{id}/content` after the permission check.

The SeaweedFS-gated tests run with:

```bash
INQTRIX_TEST_S3_ENDPOINT="http://127.0.0.1:8333" uv run pytest tests/test_object_store.py -v
# or:
INQTRIX_TEST_S3_ENDPOINT="http://127.0.0.1:8333" python -m pytest tests/test_object_store.py -v
```

## Related docs

- [Settings and environment variables](../configuration/settings-and-env.md)
  — the definition and default of every `INQTRIX_*` variable used here,
  including the worker heartbeat, claim-idle, and attempt limits.
- [Testing strategy](testing-strategy.md) — where the database-gated
  tests sit in the test pyramid.
- [Docs maintenance](docs-maintenance.md) — conventions this page
  follows.
