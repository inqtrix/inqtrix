# Runbooks — operate the Stack-mode deployment

> Files: `deploy/compose/compose.stack.yaml`

## Scope

Day-to-day lifecycle commands for the Stack mode compose stack: start, stop, restart, update, backup, restore, reset. Set up the stack first with [Stack quickstart](../getting-started/stack-quickstart.md).

Raw Compose is the canonical interface. The following temporary shell function
only keeps the examples readable; it contains the full contract and stores no
state:

```bash
inqtrix_compose() {
  docker compose \
    -f deploy/compose/compose.stack.yaml \
    --env-file deploy/.env.stack.secrets \
    --env-file deploy/.env.stack \
    "$@"
}
```

Replace `docker` with `podman` when required. For a named setup, replace both
env paths together and make their `INQTRIX_ENV_FILE` /
`INQTRIX_SECRETS_FILE` pointers match. The optional `inqtrix-deploy` CLI emits
this same Compose argv; it does not maintain another config or infer a cloud
provider.

Volumes are project-prefixed because the compose project is named `inqtrix`. The default stack creates `inqtrix_pgdata`, `inqtrix_objectstore` and `inqtrix_trace_spool`; the optional profiles add `inqtrix_qdrant_storage` (`knowledge`), `inqtrix_valkey_data` (`workers`), `inqtrix_seaweedfs_data` (`s3`), `inqtrix_clickhouse_data` and `inqtrix_langfuse_valkey_data` (`observability`), and `inqtrix_dex_data` (`oidc`).

The `psql` and `pg_dump` examples below spell out the shipped defaults `-U inqtrix -d inqtrix`; a stack whose `deploy/.env.stack` sets a different `INQTRIX_PG_USER` or `INQTRIX_PG_DB` must substitute those values in every one of them, because the compose env file is not loaded into the shell that types the command.

## Start

```bash
# Default stack: postgres + migrate + api + web
inqtrix_compose up -d --build

# With optional profiles (mix as needed)
inqtrix_compose --profile knowledge --profile workers up -d --build

# Live editor collaboration (requires matching env settings and cookie auth)
inqtrix_compose --profile collaboration up -d --build

# A named setup: pass the entire pair
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up -d --build
```

Optional CLI, with the environment selected explicitly:

```bash
# uv
uv run inqtrix-deploy \
  --engine docker \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up --build --detach

# Standard Python/pip
python -m pip install -e .
python -m inqtrix.deploy \
  --engine docker \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up --build --detach
```

PgBouncer remains opt-in. It starts only when the operator supplies
`--profile pgbouncer` and changes the visible runtime DSN to the
`pgbouncer:6432` form. Neither raw Compose nor the CLI activates it
automatically.

## Status & logs

```bash
inqtrix_compose ps                       # wait for healthy
inqtrix_compose logs -f api              # startup/config errors
inqtrix_compose logs migrate             # one-shot migration output
inqtrix_compose logs -f collaboration    # fencing/readiness/persistence
```

The CLI equivalents address the same project and pair:

```bash
inqtrix-deploy status
inqtrix-deploy logs api --follow --tail 200
```

## Stop / restart

```bash
inqtrix_compose down
inqtrix_compose restart api web
inqtrix_compose restart api collaboration web
```

A restart keeps each container's environment. After a change in
`deploy/.env.stack` or the secrets file, use `inqtrix_compose up -d`, which
re-creates the services whose configuration changed.

Equivalent optional CLI:

```bash
inqtrix-deploy down
inqtrix-deploy restart api web
```

## Update

Pull the new code, then choose the update path that matches the configured
migration authority. Every installed-schema revision change requires the same
maintenance boundary, including bundled/`auto` and dedicated `bypass`: build
the new images, record active profiles, stop ingress plus API, worker,
Collaboration and all poolers, run the one-shot migration with
`INQTRIX_MIGRATION_SERVICES_QUIESCED=true`, verify head, then recreate only the
recorded workloads. The exact raw-Compose sequence is documented in
[Database migrations](database-migrations.md).

Normal Compose orchestration remains valid for a fresh install or a restart
whose database is already at the packaged head:

```bash
git pull
inqtrix_compose up -d --build

# Preserve active profiles in the same orchestrated update
inqtrix_compose --profile collaboration up -d --build
```

Managed PostgreSQL `owner` mode may use the guarded CLI operation; do not run
`compose up` first:

```bash
git pull
inqtrix-deploy \
  --external-db \
  --migration-env-file deploy/.env.migrate.secrets.azure \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  owner-upgrade --confirm-project inqtrix
```

A failure before the migration attempt restores old containers; an interrupted
or failed attempt leaves all database clients stopped because its commit state
is not safe to infer from a CLI exit code. Verify the job and database revision
before recovery.

Do not run `inqtrix-migrate` while any workload or pooler session remains.
Managed PostgreSQL uses a separate migration env file and explicit RLS mode.
See [Database migrations](database-migrations.md). A direct command is allowed
only inside the documented maintenance window after a verified backup and full
database-client drain.

### Workflow contracts in an update

Every run is bound to the workflow contract its mode carried when it was
accepted. A release that raises a contract ends, at the first worker of the
new version, the open runs that already ran under the old one: they become
`cancelled` with the reason `workflow_contract_changed`, children included, and
a person sees why instead of approving a run that can no longer continue. Read
what that will hit before the update. The list answers only a browser session
of an instance admin (an access token gets `404`), so open it signed in, or
send that session's cookie:

```text
https://<host>/v1/admin/system/run-bindings?mode=agent_kernel&below=2
```

`contracts` names what the running version carries, `counts` the open runs per
mode, status and contract, and `runs` those bound below the version you name
(at most 1,000; `runs_total` and `truncated` say when more exist). A running run
that was already asked to stop counts under the status `cancel_requested`.

Rolling such a release back is the other direction: a worker of the old version
cannot run a run bound to the newer contract, and the first such run it claims
stops its run claims (`claims no further runs` in its log); its indexing,
deletions and uploads go on. Before starting the old workers, list the runs
above the old version:

```text
https://<host>/v1/admin/system/run-bindings?above=1
```

Then cancel them with the API of the old version running. In the web app,
Settings, Admin, System counts the open runs per mode, status and contract and
offers, for each mode, to cancel the runs this version cannot run. The same from
the signed-in session, with its CSRF token in `X-CSRF-Token`:

```text
POST https://<host>/v1/admin/system/run-bindings/cancel
{"mode": "agent_kernel"}
```

It cancels the open runs of that mode bound above the contract the API carries,
or every open run of a mode the API does not register; a run bound below ends on
its own. A queued or parked run ends at once, a running one at its next step or
when a worker takes it over, and active children go with their parent (reason
`parent_cancelled`). The answer counts both (`cancelled`, `cancel_requested`).
A run already asked to stop, by an earlier cancel or with its parent, is neither
asked nor recorded again; the System page counts it apart and does not offer it.
Each cancelled run is recorded in the audit log as `run.cancelled_by_admin`, with
the admin as actor, the run id, the mode and both contract versions; children
cancelled with it have no row of their own. The owner is not part of the row:
once the run is deleted, the audit log no longer says whose run it was. A
cancelled run, running or not, ends with the reason `cancelled_by_admin`. The
owner of an agent run sees it in the Agent Desk and can start the question
again; a research or knowledge run shows only as cancelled in the desk. Neither
the view nor the answer shows a question, a title or an owner.

A run that has not started yet and is bound to an older contract is not ended:
it starts under the contract the worker carries. A run bound to a newer contract
stops the run claims of the worker that claims it, whether it ran before or not.

No worker can run such a run, and the same holds for a run whose mode the workers
do not register (a service the mode needs is not configured on them). Every
worker that claims it logs `claims no further runs until that run is cancelled,
ends or parks` and takes no new run while the run still waits for a worker; its
other jobs go on, and the runs it took with it in the same batch start. The run
is not failed on its last delivery either, because only a worker that may run it
decides about it. The worker checks the run every few seconds and claims again,
with the line `claims runs again`, once the run no longer waits: it is cancelled
(the admin cancel above covers a newer binding and a mode the API does not
register; the next claim ends a running one), it ended under a worker that
carries the mode and the contract, or it parks there. No restart is needed. A
run that is still queued fails after two hours in the queue (`queued_timeout`).
Restore the configuration or the version, or cancel the runs.

### Upgrading the bundled Qdrant

A Qdrant **minor** version bump is not a plain image swap. Two upstream rules
govern it, and neither is enforced by the stack:

1. **One minor version at a time.** Storage compatibility is guaranteed for a
   single minor step only (1.18.x to 1.19.x). Crossing more than one requires
   landing on each intermediate minor first.
2. **The storage migration is not reversible.** The first 1.19 start rewrites
   the storage directory in place. Reverting the pinned tag afterwards does
   **not** undo it — the older server cannot read the migrated directory.

So a rollback plan means a copy of the volume, taken before the new image
starts, not a second image reference:

```bash
# Before the upgrade, with the stack stopped
docker run --rm -v inqtrix_qdrant_storage:/data -v "$PWD:/out" \
  docker.io/library/busybox tar czf /out/qdrant-storage-pre-upgrade.tgz -C /data .
```

Upstream also recommends upgrading the client SDK before the server; the
packaged `qdrant-client` floor tracks the bundled server minor, so a normal
image rebuild already satisfies that ordering.

**The fallback when no volume copy exists.** Qdrant here is a *derived* index:
Postgres holds the canonical documents, revisions, chunk text and exact source
spans (see [Data architecture](../architecture/data-architecture.md)). A
collection whose vectors are lost can therefore be rebuilt rather than
recovered:

```bash
curl -X POST "$BASE/v1/knowledge/collections/kc_.../reindex" \
    -H "Authorization: Bearer $TOKEN"
```

That costs a full re-embedding of every chunk, billed by the embedding
provider and taking as long as the original ingest, so it is a recovery route
rather than a routine one. Note the ordering: wiping the Qdrant volume while
Postgres still points at an active generation leaves the canonical chunks
without vectors to rank them. The vector search returns nothing, so retrieval
over the affected collections yields no evidence at all — no hits and no
retrieval warning — until a reindex publishes a new generation. The
`chunks_pending_reconciliation` warning covers the opposite skew: vector
points that rank but have no canonical row left in Postgres.

## Rotate the bundled PostgreSQL password

Changing `INQTRIX_PG_PASSWORD` in a file does not alter an initialized
PostgreSQL role. Generate a URL-safe value, stage it in the selected mode-`0600`
secret file, then let the guarded operation quiesce clients, apply `ALTER ROLE`
over stdin, and recreate the affected services:

```bash
inqtrix-deploy \
  --engine docker \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  db rotate-password --confirm-project inqtrix
```

The value is read only from the selected secret file. It is not accepted as a
CLI argument and is never printed. The visible DSN must continue to reference
`${INQTRIX_PG_PASSWORD}`. This operation deliberately rejects external
databases; use the provider's rotation procedure there.

The raw administrative counterpart is intentionally interactive so the new
password is not exposed in process arguments. First record exactly which
database clients are running; keep this shell and its captured positional
arguments until recovery is complete:

```bash
set -- $(
  inqtrix_compose ps --services --filter status=running |
    awk '/^(api|worker|collaboration|pgbouncer)$/'
)
printf 'Database clients to restore: %s\n' "$*"
if [ "$#" -gt 0 ]; then
  inqtrix_compose stop "$@"
fi
inqtrix_compose exec postgres psql -U inqtrix -d inqtrix
```

At the `psql` prompt run `\password inqtrix`, enter the exact staged value
twice, then `\q`. Recreate PostgreSQL and exactly the clients captured above:

```bash
inqtrix_compose up -d --wait --no-deps --force-recreate postgres
if [ "$#" -gt 0 ]; then
  inqtrix_compose up -d --wait --no-deps --force-recreate "$@"
fi
```

This deliberately does not infer profiles or start an optional worker,
Collaboration service, or PgBouncer that was inactive. Verify health and the
selected runtime DSN before reopening traffic.

## Local development (changing the code)

The Stack-mode images serve a **built** bundle — editing source does **not** hot-reload. After a code change, rebuild the affected image:

```bash
inqtrix_compose up -d --build web    # after a frontend change
inqtrix_compose up -d --build api    # after a backend change
```

For active development with **hot reload**, do not use the Stack-mode images. Run the frontend dev server (and optionally the backend) on the host:

```bash
# 1. Backing services + API, with the loopback ports the Vite proxy needs.
#    compose.dev-ports.yaml carries `name: inqtrix-dev`, so this is a SEPARATE
#    Compose project from the stack above, not an overlay on it.
docker compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  up -d postgres migrate api

# 2. Frontend with hot reload (Vite, http://127.0.0.1:5173, proxies /api /v1 /health and the /collaboration websocket to the API):
npm run ui:dev
```

Edit files under `apps/research-desk/` and the browser updates instantly.

See the manual/host setup in
[Platform components](../getting-started/platform-components.md#manual--host-platform-framework-mode)
and the UI paths in
[First research run](../getting-started/first-research-run.md).

## Backup

Capture **both** the Postgres database and the object-store volume - uploaded blobs live outside Postgres. PostgreSQL already contains every collaboration update, snapshot, projection, lease, and decision row; the Node service has no separate volume to back up.

```bash
# Postgres dump
inqtrix_compose exec -T postgres pg_dump -U inqtrix inqtrix > backup-$(date +%F).sql

# Object-store volume (local backend)
docker run --rm -v inqtrix_objectstore:/data -v "$PWD:/out" \
  docker.io/library/busybox tar czf /out/objectstore-$(date +%F).tgz -C /data .
```

If you run `--profile knowledge`, also snapshot the Qdrant volume (`inqtrix_qdrant_storage`) the same way; re-ingestion can rebuild it, so it is optional.

## Restore

For a collaboration-enabled deployment, stop the API and Node writer before
restore. Restore a matching application/schema version, then start FastAPI
before exactly one collaboration instance. Node reconstructs each room from
the verified snapshot plus update tail; never reconstruct it from Markdown
alone. See [Deploy editor collaboration](editor-collaboration.md#backup-and-restore).

```bash
# Postgres (into a fresh, migrated database)
cat backup-YYYY-MM-DD.sql | inqtrix_compose exec -T postgres psql -U inqtrix inqtrix

# Object-store volume
docker run --rm -v inqtrix_objectstore:/data -v "$PWD:/in" \
  docker.io/library/busybox sh -c "cd /data && tar xzf /in/objectstore-YYYY-MM-DD.tgz"
```

## Reset (destroy all data)

```bash
inqtrix_compose down --volumes        # removes containers AND named volumes
```

The CLI requires its explicit destructive confirmation for the same operation.
Re-running `up -d --build` then re-migrates a clean database.

## Collaboration kill switch

Set `INQTRIX_COLLABORATION_ENABLED=false`, restart FastAPI, and then stop the
collaboration service. Legacy Markdown editing continues. Existing
collaboration documents retain their binary PostgreSQL state and expose only
the last saved Markdown projection read-only; the switch does not convert or
delete them.

## Related docs

- [Stack quickstart](../getting-started/stack-quickstart.md)
- [Platform components](../getting-started/platform-components.md)
- [Security model](security-model.md)
- [Deploy editor collaboration](editor-collaboration.md)
