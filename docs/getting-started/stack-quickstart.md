# Stack quickstart — 5 minutes to Research Desk

> Files: `deploy/compose/compose.stack.yaml`, `deploy/.env.stack.example`, `deploy/docker/Dockerfile.api`, `deploy/docker/Dockerfile.web`, `deploy/docker/Dockerfile.collaboration`

## Scope

The fastest path to a running Research Desk with one command: `docker compose up`. This is **Stack mode** — one compose file, one URL, a built-in web UI, Postgres for durable data. No Python toolchain, no host-side build. For embedding Inqtrix as a library or wiring providers in Python, see [Library mode](../deployment/library-mode.md) instead.

## Prerequisites

- Docker with Compose v2.24 or newer (or Podman delegating to an equally
  recent docker-compose). The stack file uses the `env_file` long form and
  `depends_on` with `required: false`; older versions fail with a schema
  error that does not name the real cause.
- One LLM API key and one search API key (e.g. an OpenAI-compatible gateway + Perplexity). See [Provider recipes](provider-recipes.md) for every supported combination.

## 1. Configure

```bash
cp deploy/.env.stack.example deploy/.env.stack
cp deploy/.env.stack.secrets.example deploy/.env.stack.secrets
chmod 0600 deploy/.env.stack.secrets
# edit the visible configuration and the credential file
```

Keep topology and provider selection in `deploy/.env.stack`. Its complete DSN
remains directly editable and references the password instead of repeating it:

```dotenv
INQTRIX_ENV_FILE=deploy/.env.stack
INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets
INQTRIX_DATABASE_URL=postgresql+asyncpg://inqtrix:${INQTRIX_PG_PASSWORD}@postgres:5432/inqtrix
INQTRIX_LLM_PROVIDER=litellm
LITELLM_BASE_URL=http://litellm-proxy:4000/v1
INQTRIX_SEARCH_PROVIDER=perplexity
REASONING_MODEL=gpt-4o
SEARCH_MODEL=perplexity-sonar-pro-agent
```

Put only credentials in `deploy/.env.stack.secrets`:

```dotenv
INQTRIX_PG_PASSWORD=replace-with-url-safe-random-value
INQTRIX_SESSION_SECRET=replace-with-independent-random-value
INQTRIX_PAT_PEPPER=replace-with-another-independent-random-value
LITELLM_API_KEY=sk-...
PERPLEXITY_API_KEY=pplx-...
```

To run Azure, Anthropic, or Bedrock instead, change the same pair using
[Provider recipes](provider-recipes.md). No script derives a URL or creates a
third runtime file.

The committed stack example also exposes the shared long-operation policy used
by API and worker containers. Keep these values aligned unless you intentionally
tune the deployment:

```dotenv
REASONING_TIMEOUT=600
EDITOR_ASSISTANT_TIMEOUT=600
SEARCH_TIMEOUT=600
CLAIM_EXTRACT_TIMEOUT=600
MAX_TOTAL_SECONDS=3600
INQTRIX_AGENT_MAX_PARALLEL_CHILDREN=6
INQTRIX_AZURE_FOUNDRY_MAX_CONCURRENCY=6
INQTRIX_QUOTA_MAX_TOKENS_PER_RUN=900000
```

The first four values budget one logical operation including every retry;
retries do not receive another full timeout. The optional 900,000-token run
quota stops at the next safe graph boundary and never shortens a stored answer.

That minimum gets you research + chat. The config example has one canonical
assignment location per key; corresponding credentials stay in the one secrets
file. Edit an existing selector instead of appending it, uncomment optional
fields only once, and run the matching profile in step 2:

| Want | Change once in `deploy/.env.stack` | Run with |
|---|---|---|
| Cited answers over your documents (RAG) | `INQTRIX_KNOWLEDGE_ENABLED`, `INQTRIX_VECTOR_BACKEND=qdrant`, `INQTRIX_QDRANT_URL`; required `INQTRIX_QDRANT_API_KEY` in the paired secrets file | `--profile knowledge` |
| Durable / scaled runs | `INQTRIX_QUEUE_BACKEND=valkey`, `INQTRIX_VALKEY_*` | `--profile workers` |
| Optional transaction pooling for the bundled PostgreSQL service | Point the visible `INQTRIX_DATABASE_URL` at `pgbouncer:6432` with `?prepared_statement_cache_size=0`; migrations remain on direct PostgreSQL | `--profile pgbouncer` |
| Bundled S3 object store (instead of the local volume) | replace the canonical object-store backend value with `s3`, then enable the one SeaweedFS static/path/create-if-missing field block | `--profile s3` |
| Managed/native S3 | the default-chain or external S3-compatible block; do not start bundled SeaweedFS | no S3 profile |
| Enterprise SSO with your IdP | the OIDC block + `INQTRIX_AUTH_MODE=oidc` | no profile |
| Local OIDC validation with bundled Dex | the exact Dex block from [Auth modes](../deployment/auth-modes.md) | `--profile oidc` |
| Live shared editor documents | `INQTRIX_COLLABORATION_ENABLED=true` + an independent 32+ character `INQTRIX_COLLABORATION_SECRET` | `--profile collaboration` |
| Trace waterfalls in a self-hosted Langfuse (prompts, provider responses, step timings) | `INQTRIX_TRACING=otlp` + the `LANGFUSE_*` block and `OTEL_EXPORTER_OTLP_*` headers from [Settings and env](../configuration/settings-and-env.md); requires an S3 object store | `--profile observability` |

Which feature needs which component (and which is on by default) is the matrix in [Platform components](platform-components.md); the full variable list is [Settings and env](../configuration/settings-and-env.md).

## 2. Start

Always pass both files in this order: secrets first, visible configuration
second. Compose does not auto-load either filename.

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  up -d --build
```

This builds the API and web images, starts Postgres, runs the schema migration
once through the automatic `migrate` dependency, then starts the API and Python
web container. A normal install/update never requires a separate manual
`inqtrix-migrate` command. Managed PostgreSQL can place its dedicated direct DSN
in the optional migration-only env file; see [Database migrations](../deployment/database-migrations.md).
First build pulls images and installs dependencies, so it takes a few minutes;
subsequent starts are fast.

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets --env-file deploy/.env.stack ps
```

## 3. Open

Open <http://localhost:8080>. The browser talks to a single origin; the Python
web gateway proxies `/api`, `/v1`, `/health`, and the optional
`/collaboration` WebSocket to the API container. Type a question into the
composer—the research run streams live.

The web port binds to `127.0.0.1` by default. For a temporary multi-user test
on a trusted local network, expose only the web ingress and open it through the
host's LAN address:

```bash
INQTRIX_WEB_BIND_ADDRESS=0.0.0.0 \
podman compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  --profile knowledge --profile s3 --profile workers \
  --profile collaboration \
  up -d --build

# On another device: http://<HOST-LAN-IP>:8080
```

You can instead put `INQTRIX_WEB_BIND_ADDRESS=0.0.0.0` in the selected stack
env file so the original command stays unchanged. Keep `api`, Postgres,
Qdrant, Valkey, and the object store private; Compose publishes only the
Python web service.
Use `local` or `oidc` auth for distinct users. Plain HTTP requires
`INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` for the cookie-session modes and is
appropriate only on a trusted test LAN. For any broader or persistent rollout,
keep the loopback bind and terminate TLS at a reverse proxy as described in
[Deploy to production](../deployment/production-checklist.md).

## 4. Verify

```bash
curl http://localhost:8080/health          # status, llm/search provider + models
curl http://localhost:8080/v1/capabilities # which features are on (knowledge, files, sharing, ...)
```

A missing or wrong credential surfaces as a loud startup error in `docker compose logs api`, naming the variable.

## Users and login

The Stack-mode compose ships with **native accounts** as the default (`INQTRIX_AUTH_MODE=local`). On first visit it walks you through creating the instance owner; from then on the in-app admin area manages users, invitations and access tokens, and people sign in with email and password.

> [!IMPORTANT]
> **Login over plain `http://localhost` can fail silently on a Mac.** The session cookie is hardened with `Secure` and the `__Host-` prefix, which Safari (and Chrome, for the `__Host-` prefix) refuse to store over non-HTTPS. The login request returns success, but the cookie is dropped, so you land back on the login screen with no error message. For local HTTP development, set `INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` in `deploy/.env.stack` and restart the `api` container; it drops the `Secure`/`__Host-` hardening (and logs a startup warning, never use it in production). For any real deployment, terminate TLS in front so the secure cookie works as intended.

Three other modes cover different environments — all in [Auth modes](../deployment/auth-modes.md):

- **`oidc`** — enterprise SSO via your own IdP; configure the
  `INQTRIX_OIDC_*` block and `INQTRIX_PUBLIC_BASE_URL` without a Compose
  profile. Use `--profile oidc` only for the bundled Dex development
  reference.
- **`apikey`** / **`none`** — a shared Bearer token, or an open single-operator server with no login.

For remote exposure, terminate TLS in front (see [Security model](../deployment/security-model.md)).

## Turn on more

The default stack is Postgres + API + web. Add bundled capability components
with Compose profiles plus matching configuration. In `deploy/.env.stack`,
replace canonical selector values and uncomment each optional field at most
once; never append a second assignment for an existing key. Managed/external
backends use configuration only and do not start the corresponding bundled
profile:

```bash
# Knowledge/RAG (Qdrant) and/or durable run workers (Valkey):
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets --env-file deploy/.env.stack \
  --profile knowledge --profile workers up -d

# Live editor collaboration (private single Node replica):
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets --env-file deploy/.env.stack \
  --profile collaboration up -d --build
```

Which feature needs which component is the decision tree in [Platform components](platform-components.md). Day-to-day start/stop/update/backup commands are in [Runbooks](../deployment/runbooks.md).

To attach an external/managed PostgreSQL instead of the bundled container
(any PostgreSQL 15+ works, including pgvector-enabled images — the extension
stays unused), add the `deploy/compose/compose.external-db.yaml` override and
follow the "External PostgreSQL" block in `deploy/.env.stack.example`:

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.external-db.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  up -d --build
```

The migration contract (dedicated migration DSN, RLS mode) is described in
[Database migrations](../deployment/database-migrations.md).

Profiles and their bundled-service configuration must move together: the
profile starts the backing container, while the env block tells the API to use
that exact in-stack endpoint. The optional `inqtrix-deploy` preflight rejects
either half of a mismatched bundled topology before Compose runs. Raw Compose
remains transparent and does not add that guard; an unreachable configured
backend then appears as degraded in `/v1/capabilities` and the admin System
page.

Collaboration additionally requires Postgres, cookie auth, private service
URLs, and an independent secret. The deployment CLI rejects
`INQTRIX_COLLABORATION_ENABLED=true` without `--profile collaboration` and
also rejects the profile without the flag. See
[Deploy editor collaboration](../deployment/editor-collaboration.md).

## Different setups, the same explicit contract

Keep one visible config and one runtime-secret file per setup:

```text
deploy/.env.stack.azure
deploy/.env.stack.secrets.azure
deploy/.env.stack.aws
deploy/.env.stack.secrets.aws
deploy/.env.stack.anthropic
deploy/.env.stack.secrets.anthropic
```

Each named config declares its own root-relative pointers:

```dotenv
INQTRIX_ENV_FILE=deploy/.env.stack.azure
INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets.azure
```

Edit or replace existing assignments in the copied templates; never append a
second assignment for the same key. This applies to provider, authentication,
secret, and file-pointer keys alike. The deployment preflight rejects duplicate
keys instead of guessing which value should win.

Then pass that exact pair explicitly:

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up -d
```

The optional CLI produces the same Compose argv without maintaining another
configuration:

```bash
inqtrix-deploy \
  --secrets-file deploy/.env.stack.secrets.azure \
  --env-file deploy/.env.stack.azure \
  up --detach
```

## Next steps

- [Provider recipes](provider-recipes.md) — every LLM × search `.env` combination.
- [Platform components](platform-components.md) — do you need Postgres / Qdrant / Valkey / S3? The decision tree + feature matrix.
- [Runbooks](../deployment/runbooks.md) — start, stop, update, backup, restore, reset.
- [Deploy editor collaboration](../deployment/editor-collaboration.md) - optional service, security, and recovery.

## Related docs

- [Overview](overview.md)
- [First research run](first-research-run.md)
- [Deployment modes](../deployment/deployment-modes.md)
- [Deploy editor collaboration](../deployment/editor-collaboration.md)
