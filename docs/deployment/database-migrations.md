# Database migrations

## Scope

This page defines the production contract for schema upgrades on bundled and
managed PostgreSQL. Inqtrix uses tenant row-level security (RLS), including
`FORCE ROW LEVEL SECURITY`, so a generic application login is not sufficient
for data-moving migrations. Managed/external production databases deliberately
separate the privileged migration credential from the runtime credential. The
bundled PostgreSQL compatibility path instead derives a direct migration DSN
from the same config/secrets pair and does not require a third file.

Every Inqtrix schema migration requires PostgreSQL 15 or newer, regardless of
the revision involved. The migration preflight checks the server version before
taking locks or changing schema state and fails with an operator-facing
diagnostic on older releases.

Inqtrix uses PostgreSQL relationally only (RLS-protected tables); no PostgreSQL
extension is required — in particular not pgvector. A pgvector-flavoured image such
as pgvector 0.5.1 on Postgres 15 is PostgreSQL 15 and therefore satisfies the
requirement; the extension simply remains unused.

## The three-identity rule

Production uses three database identities. Two of them are logins; the third
never logs in at all.

| Identity | Where it is available | Required properties |
|---|---|---|
| Application role (`inqtrix_app`) | Nowhere — entered with `SET LOCAL ROLE` | `NOLOGIN NOSUPERUSER NOBYPASSRLS`, schema `USAGE` without database/schema `CREATE`, cannot own, inherit or assume an object-owner/BYPASS role, always tenant-scoped. |
| Runtime login (`inqtrix_runtime`) | API and worker only | `LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`, can `SET ROLE` to the application role, holds no schema `CREATE`, owns no RLS table and can assume no owner/BYPASS role. |
| Migration login (`inqtrix_migrate`) | One-shot migration job only | A direct PostgreSQL connection, `USAGE` **and** `CREATE` on the active schema, ownership (direct or inherited) of every managed table, the tenant-policy function and both identity sequences, and `ADMIN OPTION` on the application role. Either the `bypass` or `owner` authority described below. Never expose it through API/worker env, Collaboration, PgBouncer transaction pooling, or the browser. |

The runtime login and the migration login are never members of each other in
either direction. This three-identity split is mandatory for managed/external
production databases.
The official bundled PostgreSQL service retains its documented
`bundled_legacy` compatibility identity, but the one-shot migration still uses
a direct `postgres:5432` DSN and never the optional PgBouncer runtime target.

When set, `INQTRIX_MIGRATION_DATABASE_URL` supplies the migration-only
connection. Otherwise the migrator uses `INQTRIX_DATABASE_URL`; bundled
Compose/Helm installations deliberately provide a direct database value on
that path. A managed production database must always supply the separate
migration Secret.

### Bootstrap SQL for an external database

`INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=restricted` is the managed-production
default. Run the following once, as a database administrator (`postgres` on a
self-managed server, or the provider's admin role such as `azure_pg_admin`,
`rds_superuser` or `cloudsqlsuperuser`). Inqtrix itself never needs that
account again afterwards: no superuser runs `inqtrix-migrate`.

```sql
-- 1. Application role: the SET LOCAL ROLE target. Never a login.
CREATE ROLE inqtrix_app NOLOGIN NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION;

-- 2. Runtime login for API and worker.
CREATE ROLE inqtrix_runtime LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '<runtime-secret>';
GRANT inqtrix_app TO inqtrix_runtime;

-- 3. Migration login: no superuser, no BYPASSRLS, no CREATEROLE.
--    Revision 0001 grants the application role to this login, so it needs
--    ADMIN OPTION on that role.
CREATE ROLE inqtrix_migrate LOGIN NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '<migration-secret>';
GRANT inqtrix_app TO inqtrix_migrate WITH ADMIN OPTION;

-- 4. Database and schema ownership.
CREATE DATABASE inqtrix OWNER inqtrix_migrate;
\connect inqtrix
ALTER SCHEMA public OWNER TO inqtrix_migrate;
REVOKE ALL ON DATABASE inqtrix FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE inqtrix TO inqtrix_runtime, inqtrix_migrate;
GRANT USAGE ON SCHEMA public TO inqtrix_app;
GRANT USAGE, CREATE ON SCHEMA public TO inqtrix_migrate;
```

No table, function or sequence grants are made by hand: the migrations issue the
exact least-privilege contract for `inqtrix_app` themselves.

`inqtrix_runtime` deliberately receives no schema `USAGE` of its own and is never
a member of `inqtrix_migrate`. `NOINHERIT` plus `SET ROLE inqtrix_app` is the
isolation boundary; membership in the owning role makes readiness fail with
*"must not assume an RLS table-owner role"*.

PostgreSQL 16 split role membership into separate `INHERIT` and `SET` options.
`GRANT` enables `SET` by default, so the statements above are correct as
written, but automation that grants `WITH SET FALSE` leaves a runtime login that
cannot enter the application role. The readiness probe evaluates the same
distinction (`SET` on PostgreSQL 16+, `MEMBER` on PostgreSQL 15), so state the
option explicitly when a platform's role automation is not under your control.

When moving an **existing** database onto this split, its objects still belong
to the previous owner. Reassign them as an administrator before the first
migration, otherwise the preflight rejects the run:

```sql
REASSIGN OWNED BY <previous-owner> TO inqtrix_migrate;
```

This covers the tables, `inqtrix_current_tenant_id()`, `audit_log_id_seq` and
`user_events_id_seq` — all of which the preflight checks individually.

### Naming: what is fixed and what is yours

The database and both login names are yours. The application role name is not:
`inqtrix_app` is written into the migrations and into the runtime contract.

| Name | Where you declare it | Free to choose |
|---|---|---|
| Database | inside both connection URLs | yes |
| Runtime login | inside `INQTRIX_DATABASE_URL` | yes |
| Migration login | inside `INQTRIX_MIGRATION_DATABASE_URL` | yes |
| Application role | fixed as `inqtrix_app` | no |

Revision 0001 creates `inqtrix_app` by that name and every later revision
grants the table, function and sequence contract to that name.
`inqtrix-migrate` refuses to run against an installed schema whose
`inqtrix_app` role is missing, and readiness fails unless the canonical grants
still sit on that role.

`INQTRIX_DATABASE_APP_ROLE` selects which role a transaction enters with
`SET LOCAL ROLE`; it does not rename the role the migrations grant to. Point it
at a different role only if that role reaches the same privileges through
membership in `inqtrix_app`, and keep `inqtrix_app` itself in place. Empty
disables the switch, which then requires the login itself to satisfy the
contract.

The `inqtrix` role name seen in bundled deployments is the `POSTGRES_USER` of
the official PostgreSQL image (`postgres.auth.username` in the Helm chart), not
a requirement. See
[Bundled versus external databases](#bundled-versus-external-databases) for why
that identity behaves differently.

### Connection URL format

Both URLs use the same anatomy. The scheme is not interchangeable: the migrator
rejects anything but a direct `postgresql+asyncpg` URL.

```text
postgresql+asyncpg://<ROLE>:<PASSWORD>@<HOST>:<PORT>/<DATABASE>
```

```dotenv
# Runtime — API and worker
INQTRIX_DATABASE_URL=postgresql+asyncpg://inqtrix_runtime:s3cr3t@db.example.com:5432/inqtrix

# Migration — the one-shot job only
INQTRIX_MIGRATION_DATABASE_URL=postgresql+asyncpg://inqtrix_migrate:0th3r@db.example.com:5432/inqtrix
```

Four rules that cost the most time when missed:

- **Percent-encode the password.** It sits in the userinfo part of a URL, so
  `@`, `/`, `:`, `?`, `#` and `%` must be escaped (`p@ss` becomes `p%40ss`).
- **TLS is `ssl`, not `sslmode`.** Query parameters are passed straight through
  to asyncpg, which knows `ssl` and not libpq's `sslmode`. Use
  `?ssl=require` (or `verify-full`); `?sslmode=require` fails with
  `TypeError: connect() got an unexpected keyword argument 'sslmode'`.
- **The migration URL must be direct.** Never point it at PgBouncer or any
  other transaction pooler — migrations hold session-level locks across
  statements.
- **The two URLs differ only in the role and its password.** Same host, same
  port, same database.

The migration identity owns every Inqtrix schema object that migrations alter
(`alembic_version`, application tables, tenant-policy functions and identity
sequences), or has `BYPASSRLS` plus equivalent object authority. Use a dedicated
database before revoking `public` schema creation if other applications currently
share it. The migrations issue the exact grant contract for `inqtrix_app`; it is
checked under [Readiness and incident diagnosis](#readiness-and-incident-diagnosis).

PostgreSQL documents that superusers and roles with `BYPASSRLS` always bypass
RLS, while a table owner is subject to policies when `FORCE ROW LEVEL SECURITY`
is active. It also documents that `row_security=off` raises an error rather
than bypassing a policy. Inqtrix therefore never treats `row_security=off` or a
special tenant value as migration authority. See [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
and [`ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`](https://www.postgresql.org/docs/current/sql-altertable.html).

### Bundled versus external databases

The bundled demo database looks like it breaks every rule above, and
understanding why prevents the most common external-database mistake.

The official PostgreSQL image creates its `POSTGRES_USER` as a **superuser** that
owns every object in the database. The Helm chart sets that user from
`postgres.auth.username` (default `inqtrix`) and builds `INQTRIX_DATABASE_URL`
from it, so the bundled runtime login is privileged by construction. To keep
that working, the chart derives the login policy from whether the database is
bundled at all:

| `postgres.enabled` | derived `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY` | what the runtime login must be |
|---|---|---|
| `true` (bundled demo) | `bundled_legacy` | the image's `LOGIN` superuser owning every RLS table |
| `false` (external) | `restricted` | an unprivileged `LOGIN` that can only `SET ROLE` to the application role |

Two consequences for anyone writing their own values file or chart:

- With an external database, `postgres.auth.*` is **ignored**. The chart reads
  it only when `postgres.enabled=true`; changing the username there does not
  rename anything in your managed database.
- If readiness reports *"PostgreSQL runtime session login must be LOGIN
  NOSUPERUSER NOBYPASSRLS …"*, the fix is a restricted runtime login — **not**
  setting `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=bundled_legacy`. That override
  does take effect (explicit config wins over the derived value) and it accepts
  a session login with full database authority. Tenant isolation still holds
  while every statement runs inside the application-role switch, but the
  privilege is one `RESET ROLE` away instead of absent from the credential.
  `bundled_legacy` exists only for the identity the bundled image creates; the
  runtime probe verifies that exact shape and never treats it as a general
  compatibility switch.

## Which setup needs which command

Not every deployment needs the bootstrap above. Find your row first.

| Setup | Create roles by hand? | Where migrations run |
|---|---|---|
| Bundled Postgres (Compose `postgres` service, Helm `postgres.enabled=true`) | No. The image's superuser already owns everything and `bundled_legacy` accepts it. | Automatically: the `migrate` Compose service, or the chart's migration hook Job. |
| External database, supplied Helm chart | **Yes**, once — the bootstrap SQL above. | The chart's migration hook Job, pointed at `migrations.databaseSecret`. |
| External database, your own chart or manifests | **Yes**, once — the bootstrap SQL above. | A one-shot Job you own, from the Inqtrix **API** image. See [Custom charts](#custom-charts). |
| Local development (`INQTRIX_STORAGE_BACKEND=memory`) | No. | Not applicable — there is no schema. |

### Where each command runs

The bootstrap and the migration are different tools in different places. This
trips people up because both are "database work".

| Task | Tool | Runs where |
|---|---|---|
| Create roles, grant ownership | `psql` | Inside the bundled Postgres pod/container, or from a bastion against the managed endpoint. With an external database there is no Inqtrix-managed database pod. |
| Apply the schema | `inqtrix-migrate` | Only from the Inqtrix **API image**, as a separate one-shot Job or Pod. |

`inqtrix-migrate` is a console script from the Inqtrix Python package. It does
**not** exist inside the PostgreSQL container, and running it there fails with
`command not found`.

It also cannot be run inside a live API pod: the chart deliberately blanks
`INQTRIX_MIGRATION_DATABASE_URL` in API and worker containers, **and** the runtime
login holds no schema `CREATE` (*"migration role requires USAGE and CREATE on the
active schema"*), **and** an installed-schema migration refuses to start while any
other client session is connected — including that pod's own connection pool.
Since a schema change requires the API to be scaled to zero anyway, there is no
API pod left to enter. A separate one-shot Job is the only shape that works.

For a break-glass run, every value can be passed as a flag instead of an
environment variable:

```bash
inqtrix-migrate --migration-database-url postgresql+asyncpg://inqtrix_migrate:0th3r@db.example.com:5432/inqtrix --rls-mode owner --confirm-services-quiesced
```

Drop `--confirm-services-quiesced` on a **fresh, empty** database: an install
into an empty schema needs no maintenance window. Every later revision change
requires it.

## RLS modes

Set `INQTRIX_MIGRATION_RLS_MODE` to one of:

| Mode | Intended deployment | Contract |
|---|---|---|
| `auto` | Fresh installations and existing bundled/dev installations | On a fresh, empty schema it also accepts an ordinary schema owner that is neither superuser nor `BYPASSRLS`, and stays silent. On an installed schema it accepts only a superuser or `BYPASSRLS` login and emits a visible compatibility warning. It never silently enters owner maintenance. |
| `bypass` | Preferred when the managed service permits it | Requires a dedicated `NOSUPERUSER BYPASSRLS` migration login. Runtime workloads still switch to the restricted `inqtrix_app` role. |
| `owner` | Managed services that do not grant `BYPASSRLS` | Requires ownership and DDL/grant authority over every managed Inqtrix table, function and sequence. |

RLS mode selects migration authority; it never makes a live schema change
safe. Every actual revision change on an installed schema requires API,
worker, Collaboration and every connection pooler to be stopped, all database
client sessions to be drained, and
`INQTRIX_MIGRATION_SERVICES_QUIESCED=true`. A no-op invocation at the already
installed target does not require a maintenance window.

If the provider grants neither `BYPASSRLS` nor ownership/DDL authority over all
managed schema objects, schema-changing upgrades are not technically possible.
The migration preflight rejects that state before changing the schema.

## What the migration command guarantees

`inqtrix-migrate` is the internal command run by the orchestrator's one-shot
job. Its preflight verifies a direct PostgreSQL connection, current/session
roles, superuser/BYPASS properties, ownership of the revision table, tenant
tables, policy function and identity sequences, required DDL/grant rights and
the selected RLS mode.

Every installed-schema transition performs one atomic PostgreSQL transaction:

1. Verify the explicit quiescence assertion and require the migration
   connection to be the database's only client backend.
2. Set a transaction-local ten-second `lock_timeout`, then acquire the global
   Inqtrix transaction-level advisory lock and `ACCESS EXCLUSIVE` locks on
   `alembic_version` and every tenant table present at the source revision.
   The timeout bounds only lock acquisition; it is not a data-processing or
   migration deadline. A timeout rolls back the transaction and reports that
   no schema transition was published.
3. In owner mode only, change those owner-controlled tables to
   `NO FORCE ROW LEVEL SECURITY`.
   After each Alembic revision, newly created or recreated tenant tables are
   locked and changed the same way before the next revision runs. RLS remains
   enabled; `DISABLE ROW LEVEL SECURITY` is never used.
4. Run all Alembic revisions on that same injected connection and transaction.
5. Verify the target revision, managed-object owners, enabled/forced RLS state,
   tenant policies and exact runtime ACLs before commit.

Any failure rolls back schema changes, data backfills, Alembic revision and RLS
state together. `SET LOCAL row_security=off` is retained only as a fail-closed
detector for an unexpected tenant table. PostgreSQL holds explicit locks to the
end of the transaction. See
[explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html)
and [transaction advisory locks](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS).

## Normal orchestration

### Compose

Compose starts `migrate` automatically and starts API/worker only after that
one-shot service exits successfully. With bundled PostgreSQL, the job receives
a transparent direct `postgres:5432` DSN assembled from the same
config/secrets pair—even when API and worker use optional PgBouncer. There is
no third file in bundled mode.

For a managed/external database:

1. Copy `deploy/.env.migrate.secrets.example` to a named, uncommitted file such
   as `deploy/.env.migrate.secrets.azure` and set mode `0600`.
2. Put only the direct privileged `INQTRIX_MIGRATION_DATABASE_URL` in it.
3. Set the root-relative pointer and non-secret RLS mode in the selected stack
   config:

   ```dotenv
   INQTRIX_MIGRATION_ENV_FILE=deploy/.env.migrate.secrets.azure
   INQTRIX_MIGRATION_RLS_MODE=bypass
   ```

4. Choose exactly one update path:
   - For every RLS mode, use the raw-Compose maintenance sequence below:
     build first, stop ingress and every database client, run the one-shot
     migration with the quiescence assertion, verify head, then recreate the
     recorded workloads.
   - For external `owner`, the optional CLI can run
     `inqtrix-deploy ... owner-upgrade` instead of a
     preceding `compose up`. The command records the active workloads, builds
     the new migration and workload images while the old release is still
     serving, stops the active web ingress and database clients, runs the
     one-shot migration,
     and recreates the previously active API, worker, Collaboration and web
     services from the new images. A failure before the migration attempt
     restarts stopped old containers. Once the attempt begins, any non-zero or
     interrupted result leaves database clients stopped: a disconnected CLI
     cannot prove whether PostgreSQL committed, rolled back, or still has a
     live migration session. Verify the migration job and database revision
     before deliberately resuming or rolling back.

The following command is suitable for a fresh external bypass installation or
an already-at-head restart. It is not an installed-schema upgrade procedure:

```bash
inqtrix-deploy \
  --external-db \
  --migration-env-file deploy/.env.migrate.secrets.azure \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up --detach --build
```

The exact raw Compose counterpart consumes the same pair and the config-side
migration pointer. On an installed schema it is used only after the manual
maintenance migration has reached head:

```bash
docker compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.external-db.yaml \
  --env-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up -d --build
```

For owner mode, the guarded command additionally requires explicit project
confirmation:

```bash
inqtrix-deploy \
  --external-db \
  --migration-env-file deploy/.env.migrate.secrets.azure \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  owner-upgrade --confirm-project inqtrix
```

The canonical raw-Compose upgrade sequence is: record
`compose ps --services --filter status=running`; build the new `migrate` plus
those workload images; stop `web`, `api`, and every running worker,
Collaboration, and pooler; run exactly one
`compose run --rm --no-deps -e INQTRIX_MIGRATION_SERVICES_QUIESCED=true
migrate`; verify Alembic head; then force-recreate only the workloads recorded
at the start. The migrator additionally refuses to start while another
database client session remains. External/operator-managed poolers cannot be
scaled by Inqtrix and must be drained by the operator. If the migration command
is interrupted or its commit outcome is
unknown, keep all clients stopped. The CLI automates these safety conditions;
the raw sequence is intentionally not hidden in another shell script.

Do not start new workload containers before this sequence completes. Fresh
installs and already-at-head restarts retain the normal Compose dependency on
the one-shot `migrate` service.

To run the Compose stack against an external PostgreSQL, add the
`deploy/compose/compose.external-db.yaml` override (it deactivates the
bundled database container) and follow the "External PostgreSQL" block in
`deploy/.env.stack.example`: external runtime DSN with a dedicated
unprivileged login, `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=restricted`, a
migration env file, and an explicit `INQTRIX_MIGRATION_RLS_MODE`. Swapping
the bundled service's image in place (Postgres 18 to a Postgres 15 based
image) works only with a FRESH data volume: an existing Postgres 18 data
directory is not downgrade-compatible and the images mount different data
paths — attaching an external database is the supported route.

### Helm

Configure:

```yaml
migrations:
  databaseSecret:
    name: inqtrix-migration-database
    key: INQTRIX_MIGRATION_DATABASE_URL
  rlsMode: bypass
  maintenanceConfirmed: false
```

The Secret is injected only into the hook Job. Helm blocks the release when the
job fails. On every upgrade, `maintenanceConfirmed: true` authorizes a
chart-owned pre-upgrade hook to remove the API HPA, scale API, worker,
Collaboration and the chart-owned PgBouncer to zero, and wait until their pods
have terminated.
The hook revokes its own temporary RoleBinding before exit. The migration hook
runs only after that bounded quiesce job succeeds; after migration, the normal
release apply writes a positive API `minReplicas` once to reactivate an HPA from
Kubernetes' zero-replica maintenance state.

`migrations.databaseSecret.name` must not be the runtime application Secret.
The chart rejects that collision. It also rejects
`secret.data.INQTRIX_MIGRATION_DATABASE_URL`: the chart-managed runtime Secret
is not an alternative location for a privileged migration credential. The
workload templates defensively blank
`INQTRIX_MIGRATION_DATABASE_URL` in API/worker even when an externally managed
runtime Secret contains the key by mistake.

For every RLS mode, the production sequence is:

1. Back up PostgreSQL and verify the restore procedure.
2. Set `maintenanceConfirmed: true` to authorize the narrow maintenance
   ServiceAccount and quiesce hook.
3. Run the Helm upgrade; the hook scales database clients to zero and drains
   their pools before the migration job starts.
4. Require the hook postcondition and Alembic head check to pass.
5. Start the new workloads and wait for `/readyz` before admitting traffic.

If the migration hook fails, the chart deliberately leaves database clients
quiesced instead of starting an image whose schema contract is unknown. Correct
the migration authority or roll back, rerun the release, and only then restore
traffic. The successful Helm apply recreates the desired replicas and HPA.

An external/operator-managed pooler is outside the chart's RBAC scope. Drain it
before starting the Helm upgrade; the migration job verifies that no residual
database client session remains.

### Custom charts

A bring-your-own-manifests deployment must preserve the same dependency graph
as the supplied chart: stop workloads for every installed-schema change, run
exactly one direct migration Job (`inqtrix-migrate`, `backoffLimit` low enough
to fail loudly), wait for it to succeed, then start the new API and workers.
A post-start hook or a new image serving against an old schema breaks the
boundary. `deploy/helm/inqtrix/templates/job-migrate.yaml` is the reference
implementation, including the environment the Job needs
(`INQTRIX_MIGRATION_RLS_MODE`, `INQTRIX_MIGRATION_SERVICES_QUIESCED`,
`INQTRIX_MIGRATION_DATABASE_URL`).

Against an external database the deployment also owns the one-time role
bootstrap: run
[Bootstrap SQL for an external database](#bootstrap-sql-for-an-external-database)
before the first Job. PostgreSQL 15 or newer, no extensions required.

## Data-moving integrity contracts

Legacy Knowledge metadata is never trusted to mint an asset identity. The
migration resolves `source_id`, `fileId` and `file_id` only against canonical
same-tenant asset/file rows. Every supplied hint must resolve to one asset and
the collection must contain only one document claiming it; dangling,
contradictory or duplicate claims remain stored but are quarantined and cannot
become active evidence.

Every reconciled Knowledge document also receives a server-owned
tenant/owner/workspace source binding; a row whose scope cannot be proved stays
quarantined. See "Why source scope is typed" in [the Knowledge engine](../architecture/knowledge-engine.md).

Ledger relationships include `tenant_id` in their database foreign keys, not only
in row-level-security predicates, so children cannot reference a known parent
identifier from another tenant. Stored-byte migration validates the aggregate
quota counter and each file stock row against its canonical bound file before the
transaction commits.

Lifecycle fencing, deletion receipts, immutable Knowledge history and release
integrity reconciliation cannot be represented truthfully by older schemas.
Their migrations reject schema downgrade instead of deleting or relabelling
state. Production rollback follows the established contract: stop new
binaries, restore the verified pre-upgrade database backup, then start the
matching old binaries.

## Readiness and incident diagnosis

Runtime readiness validates more than `SELECT 1`: the database must be at the
packaged Alembic head, every expected tenant table must have enabled and forced
RLS with the canonical fail-closed policy and exact least-privilege grants, and
a transaction must be able to switch to the restricted application role with a
tenant GUC. `alembic_version` must resolve in that active schema with explicit
`SELECT` only, the policy function must be explicitly executable only by the
application role, and both runtime identity sequences must have exactly
`USAGE`. The probe audits rights held directly and through every role the
effective role or restricted session login can actually assume (`MEMBER` on
PostgreSQL 15, the separate `SET` option on PostgreSQL 16+). Owned, inherited or
assumable ownership-role access, database/schema `CREATE`, missing dependencies,
PUBLIC grants and
excessive table/column rights such as `TRUNCATE`, `REFERENCES`, grant options or
non-append-only mutation of the append-only tables (`audit_log`, `llm_usage`)
fail readiness. Named reporting roles that
the runtime cannot use remain the database operator's boundary. The probe also
requires `row_security=on`, active RLS, a writable transaction, a successful
policy-function call and a harmless protected-table query. The catalog checks
use the active schema rather than assuming `public`. A
mismatch is `not_ready`; workers do not take new jobs. This turns an incomplete
rollout into one deployment error instead of unrelated 500 responses from runs,
preferences, skills and files.

The characteristic managed-PostgreSQL failure is SQLSTATE `28000`, often shown
as `InvalidAuthorizationSpecificationError` with:

```text
inqtrix.tenant_id is not set; refusing the tenant-scoped query (use tenant_session)
```

That means a data migration touched a forced-RLS table without valid migration
authority. It is not fixed by setting a tenant environment variable. Check the
migration job's selected RLS mode and role properties, then rerun the normal
orchestrated rollout after correcting the Secret or maintenance boundary.

Manual `inqtrix-migrate` execution is a break-glass action only. Stop all
workloads, use the same migration-only environment as the job, capture the
preflight output and take a backup first.

Alembic `--sql`/offline rendering is intentionally unsupported. The migration
chain performs data-dependent reconciliation and postcondition checks that
cannot be represented truthfully without the target database. Use only the
managed online runner so locks, data changes, revision stamping and validation
share one transaction.

## Related docs

- [Kubernetes](kubernetes.md) — Helm values, Secrets and
  custom-chart parity.
- [Security model](security-model.md) — Runtime role and
  tenant-isolation requirements.
- [Runbooks](runbooks.md) — Deployment and incident-response procedures.
- [Settings and environment](../configuration/settings-and-env.md) — Complete
  variable reference.
