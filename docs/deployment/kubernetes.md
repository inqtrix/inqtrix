# Kubernetes

> Files: `deploy/helm/inqtrix/`, `deploy/docker/Dockerfile.api`, `deploy/docker/Dockerfile.web`, `src/inqtrix_web_gateway/`

## Scope

How to deploy Inqtrix on Kubernetes with the bundled Helm
chart at `deploy/helm/inqtrix/`. This page covers the concepts (what Helm does,
what runs where), building and publishing the images, three step-by-step install
scenarios (turnkey demo, Azure-backed demo, production with an external
database), secret and auth configuration, scaling and sizing, upgrades, and
troubleshooting.

It does **not** cover the single-host Docker Compose stack (see
[Stack quickstart](../getting-started/stack-quickstart.md)) or the conceptual
component overview (see [Platform components](../getting-started/platform-components.md)).
Provider selection and the full environment-variable reference live in
[Provider recipes](../getting-started/provider-recipes.md) and
[Settings and env](../configuration/settings-and-env.md).

## Concepts: how a Helm deployment works

If you know Docker Compose, Helm is the Kubernetes equivalent of `docker compose up`:

```text
Compose                              Kubernetes
  docker compose up          ~=        helm install inqtrix ./deploy/helm/inqtrix
  (reads compose.yaml)                 (reads the Helm chart)
  starts the containers                creates the Kubernetes objects (Pods, ...)
```

A **Helm chart** is a parameterised package of Kubernetes manifests. `helm install`
reads it, fills the placeholders from `values.yaml` (and your `--set` overrides),
and creates everything automatically. There is one command that brings the whole
deployment up; that is the normal path, not the exception.

The end-to-end flow:

```text
  [1] source + Dockerfiles
        |  docker build (the images run non-root)
        v
  [2] two images: inqtrix-api, inqtrix-web   --push-->  a container registry
        v
  [3] helm install inqtrix ./deploy/helm/inqtrix      <- the "one command"
        |
        v
  +------------------------- Kubernetes cluster ---------------------------+
  |  migrate Job (runs ONCE first: inqtrix-migrate) ---+                   |
  |                                                    v                   |
  |  api Deployment   web Deployment   worker Deployment   (our pods)      |
  |        |               |                 |                             |
  |        +---- ConfigMap (settings) + Secret (passwords/keys) ----+      |
  |                                                                        |
  |  optionally bundled:  [Postgres] [Qdrant] [Valkey] [MinIO/S3]          |
  |  OR point at external/managed services instead                         |
  |                                                                        |
  |  Ingress  -> reachable over HTTPS                                      |
  +------------------------------------------------------------------------+
```

Two groups of components, important to keep apart:

| Group | Components | Who provides them |
|---|---|---|
| **Inqtrix itself** | `api`, `web`, `worker`, the one-shot `migrate` Job | Always the chart (your images). The worker is never "external"; it is on by default, and `worker.enabled=false` selects the [single-pod variant](#the-single-pod-variant). |
| **Backing infrastructure** | PostgreSQL, Qdrant (vector store), Valkey (queue), S3 object store (MinIO) | Either bundled by the chart (demo) **or** external/managed (production). Your choice per service. |

The chart bundles Postgres, Qdrant, Valkey and an S3 object store (MinIO) as
optional turnkey services, but not every Docker Compose profile. In particular,
Dex/OIDC is external to the chart: use your own identity provider (or deploy one
with your platform's preferred operator/chart) and point Inqtrix at it through
`config` and `secret`.

Configuration is split exactly as Kubernetes expects: non-secret settings go into
a **ConfigMap** (chart value `config`), secrets (DB URL, session/PAT secrets, API
keys) go into a **Secret** (chart value `secret`). Both are injected into the pods
as the same environment variables you would set in a `.env` file, including
provider-specific names such as `AZURE_OPENAI_ENDPOINT`.

## Prerequisites

- A running cluster and a configured `kubectl`.
- [Helm](https://helm.sh/) 3.8 or newer (`helm version`).
- A container registry the cluster can pull from (Docker Hub, GHCR, Quay, ...).
  For a local kind/minikube cluster you can load images directly instead of
  pushing.
- A default StorageClass (dynamic provisioning) when you bundle Postgres,
  Qdrant, Valkey or MinIO: each keeps its data on a PersistentVolumeClaim.
- The chart ships no NetworkPolicies. In a namespace with a default-deny
  policy, allow DNS, traffic between the release's own pods, egress from the
  API and worker pods to the database, Valkey, the object store and your model
  and search providers, egress from the migration Job to the database, and
  egress from the schema-maintenance Job to the Kubernetes API server, which it
  calls to stop the workloads before every schema upgrade.

Decide before you start (each maps to a value below):

- **Topology**: the default runs the API, the web gateway and the durable
  worker. It needs PostgreSQL, a Valkey-compatible queue and a shared S3 object
  store, and lets you add API and worker replicas. The single-pod variant
  (`worker.enabled=false`) needs only PostgreSQL, but runs exactly one API
  replica that executes every job itself. See [Scaling](#scaling).
- **Database, queue and object store**: bundle demo services
  (`postgres.enabled=true`, `valkey.enabled=true`, `s3.enabled=true`, each with
  its credential) or point at external/managed ones (`INQTRIX_DATABASE_URL`,
  `INQTRIX_VALKEY_URL`, `INQTRIX_OBJECT_STORE_BACKEND=s3` with the S3 settings).
- **Auth mode**: `none`, `apikey`, `local`, or `oidc`? (See [Auth modes](auth-modes.md).)
- **Knowledge engine**: needed? (Qdrant via `qdrant.enabled=true` or `INQTRIX_QDRANT_URL`.)
- **Live editor collaboration**: needed? It requires Postgres, cookie auth, and one private collaboration pod.

## Step 1: build and publish the images, prepare the chart

The chart always deploys API and web images and can deploy the optional
collaboration image. There is no CI publishing them yet, so build and push them
yourself. Run these from the repository root and replace `<REGISTRY>`
(your registry prefix, e.g. `ghcr.io/your-org`) and `<TAG>` (e.g. `0.2.0`):

```bash
# Build (build context is the repo root)
docker build -f deploy/docker/Dockerfile.api -t <REGISTRY>/inqtrix-api:<TAG> .
docker build -f deploy/docker/Dockerfile.web -t <REGISTRY>/inqtrix-web:<TAG> .
docker build -f deploy/docker/Dockerfile.collaboration -t <REGISTRY>/inqtrix-collaboration:<TAG> .

# Push (the cluster pulls from here)
docker push <REGISTRY>/inqtrix-api:<TAG>
docker push <REGISTRY>/inqtrix-web:<TAG>
docker push <REGISTRY>/inqtrix-collaboration:<TAG> # only when enabled
```

For a multi-architecture image (amd64 + arm64) use buildx:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
    -f deploy/docker/Dockerfile.api -t <REGISTRY>/inqtrix-api:<TAG> --push .
```

If your registry is private, create a pull secret and reference it via
`imagePullSecrets` (see the values reference below). The namespace must exist
first (`kubectl create namespace inqtrix`):

```bash
kubectl create namespace inqtrix   # skip if it already exists
kubectl -n inqtrix create secret docker-registry regcred \
    --docker-server=<REGISTRY> --docker-username=<USER> --docker-password=<TOKEN>
```

Then pass it to the chart on `helm install` with `--set imagePullSecrets[0].name=regcred`.

Point the chart at the images you pushed using both readable tags and immutable
release digests, either on the command line or in a private values file. The
tag defaults to the chart `appVersion`; production rendering fails when a
deployed first-party digest is absent. `image.allowUnpinned=true` is only for
local images that cannot yet have a registry digest.

There is currently no source-controlled CI or release publishing workflow.
Build, scan, attest, and publish every architecture deliberately as an
operator-owned release step. The historical GitHub Actions files are audit
snapshots only and must not be reactivated; see
[Release process](../development/release-process.md).

The chart has no external Helm dependencies, so no `helm dependency update` is
needed. Validate it any time with:

```bash
# A bare `helm template` hits the fail-fast guards (default postgres backend
# without a database, then the worker's queue and shared object store) and
# exits 1; that is the guard, not a broken chart. A green `helm lint` is not
# evidence either (see docs/development/testing-strategy.md).
# The render-check values bundle every prerequisite of the default topology
# with throwaway credentials; never pass them to `helm install`:
helm lint deploy/helm/inqtrix -f deploy/helm/inqtrix/values-dev.yaml
helm template inqtrix deploy/helm/inqtrix -f deploy/helm/inqtrix/values-dev.yaml
# The single-pod variant with the memory backend and migrations off renders a
# coherent stateless manifest for inspection:
helm template inqtrix deploy/helm/inqtrix \
    --set image.allowUnpinned=true --set worker.enabled=false \
    --set config.INQTRIX_STORAGE_BACKEND=memory --set migrations.enabled=false
```

## Compose profiles to Helm values

Docker Compose can read a whole `.env` file through `INQTRIX_ENV_FILE`. Helm does
not read `.env` files directly. Instead, put every non-secret environment
variable under `config` (or `extraConfig`) and every secret under `secret.data`
or an externally managed Kubernetes Secret.

| Compose / `.env` setting | Helm equivalent |
|---|---|
| `INQTRIX_ENV_FILE=deploy/.env.stack.azure`, `INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets.azure` | Compose-only paired loaders. For Helm, copy non-secrets into a private values file or `config.*`, and secrets into `secret.data.*` or `secret.existingSecret`. |
| `INQTRIX_WEB_PORT=8080` | `service.webPort` controls the Service port; expose it with port-forward or Ingress. |
| `INQTRIX_PG_PASSWORD` | For bundled demo Postgres set the required `postgres.auth.password`; for external Postgres put the full `INQTRIX_DATABASE_URL` in the Secret. |
| `INQTRIX_MIGRATION_DATABASE_URL`, `INQTRIX_MIGRATION_RLS_MODE` | Put the direct privileged URL in a dedicated Secret selected by `migrations.databaseSecret.name/key`; its name must differ from the runtime application Secret. `secret.data.INQTRIX_MIGRATION_DATABASE_URL` is rejected. Select authority with `migrations.rlsMode`. The URL is injected only into the one-shot hook Job and explicitly blanked in API/worker. |
| `INQTRIX_QDRANT_URL`, `INQTRIX_VECTOR_BACKEND`, `INQTRIX_KNOWLEDGE_ENABLED` | Auto-wired when `qdrant.enabled=true`; the bundled service also requires `qdrant.apiKey`. For external Qdrant set the topology in `config` and put `INQTRIX_QDRANT_API_KEY` in the Secret when needed. |
| `INQTRIX_QUEUE_BACKEND=valkey`, `INQTRIX_VALKEY_URL`, `INQTRIX_VALKEY_PASSWORD` | The chart derives `INQTRIX_QUEUE_BACKEND=valkey` whenever `worker.enabled=true` (the default). A bundled broker (`valkey.enabled=true`) requires `valkey.password` and auto-wires the URL. For an external broker put `INQTRIX_VALKEY_URL` in `secret.data`, or in `secret.existingSecret` together with `config.INQTRIX_QUEUE_BACKEND=valkey` as confirmation, because the chart cannot read that Secret. |
| `--profile collaboration`, `INQTRIX_COLLABORATION_ENABLED=true` | `collaboration.enabled=true`; set `image.collaboration.*` and provide `collaboration.secret.existingSecret` (or the main Secret) with `collaboration.secret.key`. The chart derives the private HTTP/WS URLs. |
| `--profile s3` (SeaweedFS), `INQTRIX_OBJECT_STORE_BACKEND=s3`, `INQTRIX_S3_*` | Bundle MinIO with `s3.enabled=true`, use static credentials in the app Secret, or set `AUTH_MODE=default` plus API/worker ServiceAccount overrides for managed workload identity. See [Object storage](object-storage.md). |
| Dex/OIDC profile | Helm has no bundled Dex service. Use `config.INQTRIX_AUTH_MODE=oidc` and point at an external IdP/directory. |

## Step 2, scenario A: turnkey demo on vanilla Kubernetes

The fastest way to see Inqtrix running. The chart starts Postgres, Qdrant,
Valkey and an S3 object store (MinIO) in-cluster and auto-wires their
connections, so the default topology (API, web gateway and worker) runs
without any database setup. **Demo/dev only** (single-pod backing services,
not highly available).

> Resource names: the commands here use the release name `inqtrix`, so the
> resources are named `inqtrix-api`, `inqtrix-web`, `inqtrix-postgres`, etc. If
> you install under a different release name `<rel>`, the chart names them
> `<rel>-inqtrix-*`; adjust the `svc/`, `deploy/` and `job/` names in every
> command below accordingly.

```bash
helm install inqtrix deploy/helm/inqtrix \
    --namespace inqtrix --create-namespace \
    --set image.registry=<REGISTRY> \
    --set image.api.tag=<TAG> --set image.web.tag=<TAG> \
    --set image.api.digest=sha256:<API-DIGEST> \
    --set image.web.digest=sha256:<WEB-DIGEST> \
    --set postgres.enabled=true \
    --set-string postgres.auth.password=$(openssl rand -hex 24) \
    --set qdrant.enabled=true \
    --set-string qdrant.apiKey=$(openssl rand -hex 32) \
    --set valkey.enabled=true \
    --set-string valkey.password=$(openssl rand -hex 24) \
    --set s3.enabled=true \
    --set-string s3.secretKey=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_SESSION_SECRET=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PAT_PEPPER=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PSEUDONYM_PEPPER=$(openssl rand -hex 32)
```

What happens: the bundled services start, then a post-install migration job
waits for Postgres and creates the schema; the api, web and worker pods may
restart a few times until it finishes, then become ready.
`INQTRIX_DATABASE_URL`, `INQTRIX_QDRANT_URL`, `INQTRIX_VALKEY_URL`, the S3
settings and the knowledge, vector and queue backend flags are auto-wired from
the bundled services. There are no fallback credentials: the render fails
until every bundled service has its credential. The Postgres and Valkey
passwords must use URL-unreserved characters `[A-Za-z0-9._~-]`, because they
are embedded in the connection URLs, and the MinIO secret key needs at least
eight characters. The four services keep their data on PersistentVolumeClaims
(8, 10, 5 and 20 GiB by default, 43 GiB in total), so the cluster needs a
default StorageClass. Prefer a private values file instead of command line
values outside this disposable demo.

For the bundled-Postgres demo, run the install command as shown (no `--wait`).
The migration is a Helm post-install hook, so Helm waits for that hook; then use
Kubernetes readiness to wait for the pods:

```bash
kubectl -n inqtrix wait --for=condition=ready pod \
    -l app.kubernetes.io/instance=inqtrix --timeout=5m
kubectl -n inqtrix get pods
```

No Ingress is enabled by default, so reach it with a port-forward:

```bash
kubectl -n inqtrix port-forward svc/inqtrix-web 8080:8080
# then open http://127.0.0.1:8080/
```

(The default auth mode is `infer` -> `none`, i.e. anonymous, which is fine for a
local trial. Pick a real auth mode for anything shared - see scenario C.)
This scenario is a platform smoke test. Use scenario B for a usable Azure-backed
chat/search/knowledge demo.

### Smaller trial: the single-pod variant

Without Valkey and MinIO, turn the worker off. The API then runs every job
itself, exactly one API replica is allowed, and the local object store keeps
files on the pod (an `emptyDir`, or a PVC with `persistence.enabled=true`):

```bash
helm install inqtrix deploy/helm/inqtrix \
    --namespace inqtrix --create-namespace \
    --set image.registry=<REGISTRY> \
    --set image.api.tag=<TAG> --set image.web.tag=<TAG> \
    --set image.api.digest=sha256:<API-DIGEST> \
    --set image.web.digest=sha256:<WEB-DIGEST> \
    --set worker.enabled=false \
    --set postgres.enabled=true \
    --set-string postgres.auth.password=$(openssl rand -hex 24) \
    --set-string secret.data.INQTRIX_SESSION_SECRET=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PAT_PEPPER=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PSEUDONYM_PEPPER=$(openssl rand -hex 32)
```

What this variant gives up is listed under
[The single-pod variant](#the-single-pod-variant).

## Step 2, scenario B: Azure-backed demo on vanilla Kubernetes

This is closer to a real application stack than the minimal demo: Azure OpenAI
for chat, Azure AI Foundry for web search, Azure embeddings, Qdrant for
knowledge, Valkey + worker for durable runs, and a bundled S3 object store
(MinIO) for shared files. Postgres, Qdrant, Valkey and the MinIO object store are
all bundled for a self-contained demo; use scenario C when those services are
managed externally.

Start from the checked-in example overlay and change the placeholder endpoints,
project path and bucket to your own values:

```bash
deploy/helm/inqtrix/values-azure.example.yaml
```

Install with secrets supplied on the command line (or through a private values
file / external secret manager):

```bash
helm install inqtrix deploy/helm/inqtrix \
    --namespace inqtrix --create-namespace \
    -f deploy/helm/inqtrix/values-azure.example.yaml \
    --set image.registry=<REGISTRY> \
    --set image.api.tag=<TAG> --set image.web.tag=<TAG> \
    --set image.api.digest=sha256:<API-DIGEST> \
    --set image.web.digest=sha256:<WEB-DIGEST> \
    --set-string postgres.auth.password=$(openssl rand -hex 24) \
    --set-string qdrant.apiKey=$(openssl rand -hex 32) \
    --set-string valkey.password=$(openssl rand -hex 24) \
    --set-string s3.secretKey=$(openssl rand -hex 32) \
    --set config.INQTRIX_OIDC_INSECURE_DEV_COOKIES=true \
    --set-string secret.data.INQTRIX_SESSION_SECRET=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PAT_PEPPER=$(openssl rand -hex 32) \
    --set-string secret.data.INQTRIX_PSEUDONYM_PEPPER=$(openssl rand -hex 32) \
    --set-string secret.data.AZURE_OPENAI_API_KEY=<AZURE-OPENAI-KEY> \
    --set-string secret.data.AZURE_AI_PROJECT_API_KEY=<AZURE-FOUNDRY-KEY> \
    --set-string secret.data.INQTRIX_EMBEDDING_AZURE_API_KEY=<AZURE-EMBEDDING-KEY>
```

The bundled services auto-wire their connection settings, but never invent
credentials. The command therefore supplies Postgres, Qdrant, Valkey, and MinIO
credentials explicitly; the MinIO secret must contain at least eight
characters. Put these values in a mode-restricted private values file for any
repeatable environment rather than retaining them in shell history or process
arguments.

The `INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` override is only for the local
HTTP port-forward shown below. Remove it when you expose the app over HTTPS.

Reach the web UI:

```bash
kubectl -n inqtrix wait --for=condition=ready pod \
    -l app.kubernetes.io/instance=inqtrix --timeout=5m
kubectl -n inqtrix port-forward svc/inqtrix-web 8080:8080
```

For a lighter trial (no object store, queue or worker), disable them and use the
local object store instead:

```bash
--set s3.enabled=false \
--set worker.enabled=false \
--set valkey.enabled=false \
--set config.INQTRIX_OBJECT_STORE_BACKEND=local
```

That is the single-pod variant; its limits are listed under
[The single-pod variant](#the-single-pod-variant).

### What the Azure overlay sets

| Area | Values |
|---|---|
| Chat models | `INQTRIX_LLM_PROVIDER=azure`, `AZURE_OPENAI_ENDPOINT`, `REASONING_MODEL`, `TIER_*_MODEL`, `INQTRIX_SELECTABLE_CHAT_MODELS` |
| Web search | `INQTRIX_SEARCH_PROVIDER=azure_foundry`, `AZURE_AI_PROJECT_ENDPOINT`, `WEB_SEARCH_AGENT_NAME`, `WEB_SEARCH_AGENT_VERSION` |
| Embeddings | `INQTRIX_EMBEDDING_PROVIDER=azure`, `INQTRIX_EMBEDDING_AZURE_ENDPOINT`, `INQTRIX_EMBEDDING_MODEL=text-embedding-3-large` |
| Knowledge | `qdrant.enabled=true` auto-wires `INQTRIX_QDRANT_URL`, `INQTRIX_VECTOR_BACKEND=qdrant`, `INQTRIX_KNOWLEDGE_ENABLED=true` |
| Durable work | `valkey.enabled=true` and `worker.enabled=true` auto-wire `INQTRIX_QUEUE_BACKEND=valkey` and `INQTRIX_VALKEY_URL` |
| Object store | `s3.enabled=true` bundles MinIO and auto-wires `INQTRIX_OBJECT_STORE_BACKEND=s3`, `INQTRIX_S3_ENDPOINT_URL`, `INQTRIX_S3_BUCKET` and the S3 access/secret keys |
| Auth & access | `INQTRIX_AUTH_MODE=local`, `INQTRIX_LOCAL_REGISTRATION=closed` (first user becomes the instance owner via first-run setup), `INQTRIX_QUOTA_ENABLED=true` (per-user quotas; every limit `0` = unlimited until an admin sets one) |

## Step 2, scenario C: production with an external database (vanilla Kubernetes)

Production keeps state in a managed/external PostgreSQL, dispatches runs through
an external Valkey-compatible broker, keeps files in an external S3 bucket, and
exposes the app over an Ingress with TLS. Provide secrets through a Kubernetes
Secret you own.

0. **Prepare the database.** Before any Kubernetes object exists, a database
   administrator creates the three roles once with `psql` and hands you two
   credentials: an unprivileged runtime login and a separate migration login.
   The bundled demo database (scenarios A and B) does this implicitly; an
   external database does not, and the Secret in step 1 has nothing to carry
   until it is done. Follow
   [Bootstrap SQL for an external database](database-migrations.md#bootstrap-sql-for-an-external-database).

   Decide the RLS mode in the same step, because it depends on what your
   provider grants. Managed services frequently withhold `BYPASSRLS` (it
   requires superuser), which makes `owner` the applicable mode. Check on the
   intended migration login:

   ```sql
   SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;
   ```

   Both `false` means `migrations.rlsMode=owner`. A dedicated `NOSUPERUSER`
   role with `rolbypassrls = true` allows `migrations.rlsMode=bypass`, which
   avoids the owner-mode maintenance requirements described in
   [Database migrations](database-migrations.md).

1. Create the namespace and the Secret (replace every `<...>`; generate the two
   random secrets with `openssl rand -hex 32`):

   ```bash
   kubectl create namespace inqtrix   # skip if it already exists
   kubectl -n inqtrix create secret generic inqtrix-secrets \
       --from-literal=INQTRIX_DATABASE_URL='postgresql+asyncpg://<USER>:<PASS>@<DB-HOST>:5432/<DB-NAME>' \
       --from-literal=INQTRIX_VALKEY_URL='redis://:<VALKEY-PASS>@<VALKEY-HOST>:6379/0' \
       --from-literal=INQTRIX_S3_ACCESS_KEY='<S3-ACCESS-KEY>' \
       --from-literal=INQTRIX_S3_SECRET_KEY='<S3-SECRET-KEY>' \
       --from-literal=INQTRIX_SESSION_SECRET='<openssl rand -hex 32>' \
       --from-literal=INQTRIX_PAT_PEPPER='<openssl rand -hex 32>' \
       --from-literal=INQTRIX_PSEUDONYM_PEPPER='<openssl rand -hex 32>' \
       --from-literal=AZURE_OPENAI_API_KEY='<AZURE-OPENAI-KEY>'
   kubectl -n inqtrix create secret generic inqtrix-migration-database \
       --from-literal=INQTRIX_MIGRATION_DATABASE_URL='postgresql+asyncpg://<MIGRATION-USER>:<PASS>@<DB-HOST>:5432/<DB-NAME>'
   ```

   `<USER>` is the runtime login and `<MIGRATION-USER>` the migration login
   from step 0 — same host, same port, same database, different roles. The
   scheme must be `postgresql+asyncpg`, the password must be percent-encoded,
   and TLS is selected with `?ssl=require` (asyncpg does not accept libpq's
   `sslmode`).

   `INQTRIX_VALKEY_URL` points at your managed Valkey or Redis (see
   [Queue broker](#queue-broker)); percent-encode its password too. The two S3
   keys are for static credentials; with a workload identity
   (`INQTRIX_S3_AUTH_MODE=default`) leave them out, as described in
   [Object storage](object-storage.md).

   The runtime Secret holds only application/provider secrets. The second
   Secret is available only to the migration Job and must use a direct database
   connection with the authority described in [Database migrations](database-migrations.md).

2. Install, pointing at the Secret and your Ingress host:

   ```bash
   helm install inqtrix deploy/helm/inqtrix \
       --namespace inqtrix --create-namespace \
       --set image.registry=<REGISTRY> \
       --set image.api.tag=<TAG> --set image.web.tag=<TAG> \
       --set image.api.digest=sha256:<API-DIGEST> \
       --set image.web.digest=sha256:<WEB-DIGEST> \
       --set secret.existingSecret=inqtrix-secrets \
       --set migrations.databaseSecret.name=inqtrix-migration-database \
       --set migrations.rlsMode=owner \
       --set config.INQTRIX_QUEUE_BACKEND=valkey \
       --set config.INQTRIX_OBJECT_STORE_BACKEND=s3 \
       --set config.INQTRIX_S3_AUTH_MODE=static \
       --set config.INQTRIX_S3_ENDPOINT_URL=https://<S3-ENDPOINT> \
       --set config.INQTRIX_S3_BUCKET=<BUCKET> \
       --set config.INQTRIX_S3_BUCKET_PROVISIONING=existing \
       --set config.INQTRIX_AUTH_MODE=local \
       --set config.INQTRIX_LLM_PROVIDER=azure \
       --set config.AZURE_OPENAI_ENDPOINT=https://<AZURE-OPENAI-RESOURCE>.openai.azure.com/ \
       --set-string config.REASONING_MODEL=gpt-5.4 \
       --set-string config.TIER_HIGH_MODEL=gpt-5.4 \
       --set-string config.TIER_MID_MODEL=gpt-5.4 \
       --set-string config.TIER_FAST_MODEL=gpt-5.4-mini \
       --set config.INQTRIX_PUBLIC_BASE_URL=https://<YOUR-DOMAIN> \
       --set ingress.enabled=true \
       --set ingress.className=<YOUR-INGRESS-CLASS> \
       --set ingress.host=<YOUR-DOMAIN> \
       --set ingress.tls.enabled=true \
       --set ingress.tls.secretName=<YOUR-TLS-SECRET>
   ```

   For an enabled Ingress, the chart derives `INQTRIX_PUBLIC_BASE_URL` from
   `ingress.host` and `ingress.tls.enabled`, and configures the web gateway to overwrite
   forwarded proto with that trusted boundary. The explicit value above is
   therefore optional when it is identical, and keeps precedence when the
   externally published origin intentionally differs from the Ingress host.

   `config.INQTRIX_QUEUE_BACKEND=valkey` confirms that `INQTRIX_VALKEY_URL` is
   in your Secret. The chart cannot read a Secret it does not manage, so
   without this confirmation it refuses to render instead of starting the API
   and the worker without a queue URL. Native AWS S3 leaves
   `INQTRIX_S3_ENDPOINT_URL` empty; the full S3 contract is in
   [Object storage](object-storage.md).

3. The worker (on by default) resumes interrupted runs after a restart and
   lets you add API replicas; see [Scaling](#scaling). Without a broker or an
   S3 bucket, install the [single-pod variant](#the-single-pod-variant)
   (`--set worker.enabled=false`, and leave out the queue and S3 values)
   instead: the platform still self-heals to visible failed states
   (`execution_lost`, `queued_timeout`) instead of hanging, but interrupted
   work fails rather than resuming.

## Configuration in detail

### Secrets

These are the secret keys Inqtrix reads (full reference:
[Settings and env](../configuration/settings-and-env.md)). Put them in your
`existingSecret` (production) or under `secret.data` via `--set-string` (dev).

Important: when `secret.existingSecret` is set, the chart references only that
Secret. It cannot merge chart-derived secret values into your Secret. If you use
an existing Secret together with bundled services, that Secret must contain
both the app connection values and the narrowly scoped backing-service values:
`INQTRIX_DATABASE_URL` + `INQTRIX_BUNDLED_POSTGRES_PASSWORD`,
`INQTRIX_VALKEY_URL` + `INQTRIX_BUNDLED_VALKEY_PASSWORD`,
`INQTRIX_QDRANT_API_KEY`, and/or `INQTRIX_S3_ACCESS_KEY` +
`INQTRIX_S3_SECRET_KEY`. For bundled demo installs, prefer the chart-managed
Secret unless you intentionally own every required key.

When the chart manages the Secret, changing a bundled Qdrant, Valkey, or MinIO
credential changes the corresponding Pod-template checksum and rolls that
workload together with API/worker. With `secret.existingSecret`, Kubernetes
does not restart env-backed Pods after an in-place Secret update: rotate the
external value and explicitly restart every affected workload. Bundled
PostgreSQL is different in both cases—restarting it does not change an
initialized role password, so use a real database-role rotation procedure.

| Secret key | When needed | What it is |
|---|---|---|
| `INQTRIX_DATABASE_URL` | storage backend `postgres` | `postgresql+asyncpg://user:pass@host:5432/db`. Auto-wired when `postgres.enabled=true`. |
| `INQTRIX_BUNDLED_POSTGRES_PASSWORD` | bundled Postgres with `secret.existingSecret` | Raw password used by the Postgres container; it must match the password represented in `INQTRIX_DATABASE_URL`. |
| `INQTRIX_VALKEY_URL` | the worker (on by default) with an external broker | `redis://:password@host:6379/0`, password percent-encoded. Auto-wired when `valkey.enabled=true`. In `secret.existingSecret`, confirm it with `config.INQTRIX_QUEUE_BACKEND=valkey`; the chart cannot read that Secret. |
| `INQTRIX_MIGRATION_DATABASE_URL` | managed PostgreSQL migration job | Direct migration-only URL stored in `migrations.databaseSecret`; never put it in the app Secret. |
| `INQTRIX_SESSION_SECRET` | auth `local`/`oidc` | >=32 chars; CSRF-token HMAC key. `openssl rand -hex 32`. |
| `INQTRIX_PAT_PEPPER` | auth `local`/`oidc` | >=32 chars; pepper for personal access tokens. Rotating it invalidates all tokens. |
| `INQTRIX_PSEUDONYM_PEPPER` | required for `oidc`; recommended for `local` | Instance-wide pepper behind the stable `usr_<hex16>` subject pseudonyms in logs and audit correlation. Share ONE value across API and worker pods (the app Secret reaches both). Empty degrades to per-process references (startup WARNING); rotation unlinks previously written pseudonyms. |
| `INQTRIX_OIDC_CLIENT_SECRET` | auth `oidc` | Confidential OIDC client secret from your IdP. |
| `INQTRIX_S3_ACCESS_KEY`, `INQTRIX_S3_SECRET_KEY`, optional `INQTRIX_S3_SESSION_TOKEN` | object store `s3`, static auth | S3 credentials. Auto-wired from `s3.accessKey`/`s3.secretKey` only for bundled MinIO. Omit all three with `INQTRIX_S3_AUTH_MODE=default`. |
| `INQTRIX_BUNDLED_VALKEY_PASSWORD` | bundled Valkey with `secret.existingSecret` | Raw password used by the Valkey container; it must match the password represented in `INQTRIX_VALKEY_URL`. |
| `INQTRIX_QDRANT_API_KEY` | bundled Qdrant or external Qdrant with auth | Required for bundled Qdrant and shared by the Qdrant pod and application. |
| Provider, search, embedding and reranker keys | the providers and features you selected | Named as in every other deployment mode (`LITELLM_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `PERPLEXITY_API_KEY`, `AZURE_AI_PROJECT_API_KEY`, `INQTRIX_EMBEDDING_API_KEY`, `INQTRIX_RERANKER_API_KEY`, AWS credentials for `bedrock`). Defaults, fallback chains and the Azure service-principal alternative: [Settings and env](../configuration/settings-and-env.md), [Provider recipes](../getting-started/provider-recipes.md). |
| `INQTRIX_SERVER_API_KEY` | auth `apikey` | Static Bearer key gating the server. |

Set only the keys your selected providers/features use. The exhaustive, always-current
list of every secret field is [Settings and env](../configuration/settings-and-env.md).
Provider endpoints, deployment names, model names, bucket names and auth
issuer/client ids are usually non-secret and belong under `config`, for example
`AZURE_OPENAI_ENDPOINT`, `AZURE_AI_PROJECT_ENDPOINT`, `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `INQTRIX_S3_ENDPOINT_URL` and `INQTRIX_S3_BUCKET`.

Generate the random secrets once:

```bash
openssl rand -hex 32   # use the output for INQTRIX_SESSION_SECRET and (a second run) INQTRIX_PAT_PEPPER
```

For production secret management beyond a hand-created Secret, use the External
Secrets Operator, Sealed Secrets, or SOPS, and point `secret.existingSecret` at
the resulting Secret.

### Auth modes

Set the mode in `config.INQTRIX_AUTH_MODE` and provide the mode's secrets. Full
behaviour is in [Auth modes](auth-modes.md); the chart-specific essentials:

`local` and `oidc` need the **postgres** storage backend (to persist
accounts/sessions durably). The chart default `config.INQTRIX_STORAGE_BACKEND`
is already `postgres`, so it just works — do **not** override it to `memory` for
these modes (with `memory`, logins are lost on restart and not shared across
replicas).

- **none / apikey** — no database needed for auth. `apikey` requires
  `INQTRIX_SERVER_API_KEY` in the Secret.
- **local** — email/password accounts (requires the postgres storage backend).
  Needs `INQTRIX_SESSION_SECRET` + `INQTRIX_PAT_PEPPER`.
  If you test local auth through an HTTP port-forward, temporarily set
  `config.INQTRIX_OIDC_INSECURE_DEV_COOKIES=true`; remove it behind HTTPS.

- **oidc** — SSO via your IdP.

  ```bash
  --set config.INQTRIX_AUTH_MODE=oidc \
  --set config.INQTRIX_OIDC_ISSUER=https://<IDP>/realms/<REALM> \
  --set config.INQTRIX_OIDC_CLIENT_ID=<CLIENT-ID> \
  --set config.INQTRIX_PUBLIC_BASE_URL=https://<YOUR-DOMAIN>
  # Secrets and boot preconditions: see Auth modes (auth-modes.md).
  ```

### Edge timeouts and body size

The cluster edge in front of the `web` pod — the Ingress controller — has its
own request timeouts and body-size limits, and their defaults break real Inqtrix
workloads even though `helm install` is green and probes pass:

- Several endpoints legitimately send no bytes while they work: editor AI
  calls block up to 630 seconds, non-streaming chat completions up to 3630
  seconds (`request_timeout_seconds` = `INQTRIX_MAX_TOTAL_SECONDS` + 30s
  margin), and the chat completion SSE stream carries no keepalive frames.
- File uploads carry up to `INQTRIX_MAX_FILE_BYTES` (default 100 MiB) plus
  the multipart envelope; the web gateway defaults to 110 MiB.

On **vanilla Kubernetes** annotations are controller-specific, so the chart
cannot set them for you. For ingress-nginx the defaults cap request bodies at
`1m` — every upload above 1 MiB fails at the ingress with a bare 413 before
Inqtrix is ever reached — and cut idle responses after 60 seconds. Start from
the commented example at `ingress.annotations` in `values.yaml`:

```yaml
ingress:
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "110m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3630"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3630"
```

Raise `proxy-body-size` together with `INQTRIX_MAX_FILE_BYTES`. The chart
passes the byte value to the web gateway, which derives 10 MiB headroom so
the API's labelled 413 stays authoritative. Raise the timeouts together with
`INQTRIX_MAX_TOTAL_SECONDS`.

Two related settings behind the chart's edge:

- `INQTRIX_TRUSTED_PROXY_HOPS=2` is derived automatically when the chart's
  Ingress is enabled: the edge plus the Python web gateway put two entries
  into `X-Forwarded-For`, and with the app default of 1 the login rate
  limiter would key every client on the edge pod address (collapsing all
  users into one lockout bucket). This assumes an edge that writes
  `X-Forwarded-For` — ingress-nginx does. An
  explicit `config.INQTRIX_TRUSTED_PROXY_HOPS` wins — for example `3` with
  an additional load balancer in front of the edge, or `1` for an exotic
  edge that does not touch the header.
- `web.externalScheme` — set to `https` when a TLS terminator the chart does
  not own (own LoadBalancer, Gateway API, service mesh) fronts the web
  Service; otherwise the web gateway forwards `X-Forwarded-Proto: http` and
  the collaboration WebSocket origin check rejects sessions even with a
  correct `INQTRIX_PUBLIC_BASE_URL`.

### Values reference (most-used knobs)

The commented [`values.yaml`](../../deploy/helm/inqtrix/values.yaml) is the full
reference. The knobs you will reach for most:

| Value | Default | Effect |
|---|---|---|
| `image.registry`, `image.*.tag`, `image.*.digest`, `image.allowUnpinned` | `ghcr.io/inqtrix`, appVersion, `""`, `false` | Production requires each deployed first-party image digest. Tag-only rendering is an explicit local-development opt-in. |
| `imagePullSecrets` | `[]` | Pull-secret names for a private registry, e.g. `--set imagePullSecrets[0].name=regcred`. |
| `config.<ENV_VAR>` / `extraConfig.<ENV_VAR>` | see file | Non-secret env (ConfigMap). Provider, auth mode, endpoints, object store, tuning. |
| `secret.existingSecret` | `""` | Name of a Secret you created (production). |
| `secret.data.<ENV_VAR>` | `{}` | Chart-managed secret values (dev only). |
| `migrations.databaseSecret.name/key` | `""`, `INQTRIX_MIGRATION_DATABASE_URL` | Dedicated direct migration DSN, injected only into the hook Job. |
| `migrations.rlsMode`, `migrations.maintenanceConfirmed` | `auto`, `false` | Migration authority and explicit installed-schema maintenance assertion. |
| `serviceAccount.api`, `serviceAccount.worker` | shared account | Optional independent names/annotations/token policy for workload identity. Web, Collaboration and migrations retain tokenless identities. |
| `s3.caBundle.existingConfigMap/key/mountPath` | empty | Optional private CA mounted only into API/worker and exposed as `INQTRIX_S3_CA_BUNDLE`. |
| `ingress.enabled`, `ingress.host`, `ingress.tls.*` | `false` | Vanilla-k8s Ingress. |
| `worker.enabled` | `true` | Durable worker Deployment. Needs Postgres, a Valkey queue and a shared S3 object store; the render names whatever is missing. `false` selects the [single-pod variant](#the-single-pod-variant). |
| `worker.replicaCount` | `1` | Worker pods. Each executes up to `INQTRIX_WORKER_CONCURRENCY` (default 100) jobs at once. See [Scaling](#scaling). |
| `collaboration.enabled` | `false` | Private one-replica `Recreate` Deployment and ClusterIP Service. Requires Postgres, cookie auth, and an independent secret; it has no HPA/PDB/PVC. |
| `collaboration.resources` | `100m` / `512Mi` request, `1` / `1Gi` limit | Node caps its heap at about half the memory limit, so keep the limit at twice the request. For long documents (about 50 pages or more) typed in concurrently use `1Gi` / `2Gi`. Every change restarts the single replica and briefly disconnects all editors. See [Collaboration service](#collaboration-service). |
| `api.replicaCount`, `api.autoscaling.*` | `1`, off | Scale the API. More than one replica requires the worker. See [Scaling](#scaling). |
| `persistence.enabled`, `persistence.size` | `false`, `5Gi` | PVC for the local object store of the single-pod variant. Mounted only while the effective backend is `local`; disabling it deletes the PVC. |
| `postgres.enabled`, `qdrant.enabled`, `valkey.enabled`, `s3.enabled` | `false` | Bundle a demo backing service in-cluster (`s3.enabled` = MinIO object store). Each enabled service requires an explicit credential; no `change-me` fallback is rendered. |
| `valkey.engine` | `valkey` | Engine of the bundled broker (`valkey` or `redis`); selects the server/CLI binaries. A foreign engine must bring its own pinned `valkey.image`. See [Queue broker](#queue-broker). |
| `postgres.enabled` (side effect) | `false` | Also **derives** `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY`: `true` renders `bundled_legacy` (the image's superuser login), `false` renders `restricted` (your own unprivileged login). With an external database, `postgres.auth.*` is ignored entirely. See [Bundled versus external databases](database-migrations.md#bundled-versus-external-databases). |
| `api.resources`, `worker.resources` | `250m` / `768Mi` request, `2` / `2Gi` limit | Memory is sized for the shipped run cap; lower `MAX_CONCURRENT` / `RUN_MAX_CONCURRENT` before lowering it. The CPU request is a starting point: with `collaboration.enabled`, request `1` core per API replica. See [Sizing rules](#sizing-rules) and [`values.yaml`](../../deploy/helm/inqtrix/values.yaml). |
| `web.maxUpstreamConnections` | *(unset)* | Per-worker ceiling for the gateway's pooled backend connections (`INQTRIX_MAX_UPSTREAM_CONNECTIONS`). Unset keeps the gateway's built-in default. A typed value because the web pod has **no** `envFrom`: `config:`/`extraConfig:` never reach it. Values below `1` fail the render. Every open SSE stream holds one upstream connection for its whole duration; exhaustion is a `503` naming the variable. |
| `pgbouncer.enabled` | `false` | Transaction-mode PgBouncer in front of the **bundled** Postgres (it requires `postgres.enabled`; the render fails otherwise). The derived `INQTRIX_DATABASE_URL` then points at the pooler with `?prepared_statement_cache_size=0`, while the migrate hook keeps the direct Postgres URL, and `migrations.maintenanceConfirmed=true` quiesces the pooler along with API, worker and Collaboration. Reach for it instead of raising `postgres.maxConnections` when many replicas each carry their own pool. An explicit `secret.data.INQTRIX_DATABASE_URL` always wins, so an external database runs its own pooler. See [Database migrations](database-migrations.md). |
| `postgres.maxConnections` | `300` | Bundled demo database only, rendered as `-c max_connections=...`. Every connection costs memory, and `postgres.resources` is sized for this value — raise them together. A value below `1` fails the render rather than the container start. Ignored with an external database — that ceiling belongs to whoever runs it. How the 300 was derived: [`values.yaml`](../../deploy/helm/inqtrix/values.yaml). |
| `postgres.auth.username`, `postgres.auth.database` | `inqtrix` | Bundled demo database only. The username becomes the image's `POSTGRES_USER`, which PostgreSQL creates as a superuser owning every object; the chart builds `INQTRIX_DATABASE_URL` from it. |

### Chart-derived environment variables

The chart computes the following ConfigMap keys itself. A deployment that
**bypasses the chart** (raw manifests, its own templates) must set them
directly — this is the complete list, with the condition each one appears
under and whether an explicit `config:`/`extraConfig:` value may override
it. Merge priority is `config:` > `extraConfig:` > derived, so an explicit
value wins wherever overriding is allowed at all.

| Key | Derived when | Derived value | Override allowed? |
| --- | --- | --- | --- |
| `INQTRIX_OBJECT_STORE_BACKEND` | always | `local`, or `s3` when `s3.enabled` | yes when `s3.enabled=false` — e.g. to point at an external S3. With `s3.enabled=true` the render fails: the key is part of the bundled-MinIO refusal list, so disable the bundled MinIO first when migrating to an external S3. |
| `INQTRIX_REPLICA_COUNT` | always | `api.replicaCount` (or `autoscaling.maxReplicas`) + `worker.replicaCount` when the worker is enabled | **no, the render fails.** The chart's own topology checks already keep the local object store to one pod, and a value that is too high would pass the chart and then crash-loop every api pod at startup via the app-side guard. |
| `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY` | always | `bundled_legacy` with the bundled Postgres, else `restricted` | yes, but doing so waives a guard — see [Database migrations](database-migrations.md) |
| `INQTRIX_PUBLIC_BASE_URL` | ingress with a host | scheme + host of the chart-owned edge | yes — explicitly supported for edges the chart does not own |
| `INQTRIX_TRUSTED_PROXY_HOPS` | chart-owned ingress | `2` | yes |
| `INQTRIX_KNOWLEDGE_ENABLED`, `INQTRIX_VECTOR_BACKEND`, `INQTRIX_QDRANT_URL` | `qdrant.enabled` | `true` / `qdrant` / in-cluster service URL | yes |
| `INQTRIX_QUEUE_BACKEND` | `worker.enabled` | `valkey` | an explicit `valkey` is the confirmation that a URL in `secret.existingSecret` needs; `memory` with the worker, or `valkey` without it, fails the render |
| `INQTRIX_S3_AUTH_MODE`, `INQTRIX_S3_ENDPOINT_URL`, `INQTRIX_S3_BUCKET`, `INQTRIX_S3_ADDRESSING_STYLE`, `INQTRIX_S3_BUCKET_PROVISIONING` | `s3.enabled` | bundled-MinIO topology | **no — the render fails**; the bundled topology is derived from `s3.*` |
| `INQTRIX_S3_CA_BUNDLE` | `s3.caBundle.*` set | the configured mount path | n/a (driven by its own values keys) |
| `INQTRIX_METRICS_ENABLED`, `INQTRIX_WORKER_METRICS_PORT` | `metrics.enabled` | `true` / `metrics.workerPort` | yes |
| `INQTRIX_COLLABORATION_ENABLED`, `INQTRIX_COLLABORATION_HTTP_URL`, `INQTRIX_COLLABORATION_WS_URL` | `collaboration.enabled` | `true` / in-cluster service URLs | yes |

Note the pod-scope asymmetry: api and worker mount the **same** ConfigMap,
so every key lands on both pods, while some govern only one process — see
[Who reads a variable](../configuration/settings-and-env.md#who-reads-a-variable--and-how-to-set-it-yourself).
The web pod mounts **none** of it (no `envFrom`); its knobs are typed
values (`web.maxUpstreamConnections`) or `web.extraEnv`.

## Queue broker

Durable run dispatch (`worker.enabled=true`, the default, from which the chart
derives `INQTRIX_QUEUE_BACKEND=valkey`) needs a broker that speaks the Redis
protocol. Inqtrix uses Streams, `EVAL` and key expiry only, so **any RESP
server from Redis 6.2 onwards satisfies the contract** (`XAUTOCLAIM` is the
newest command used). Two options:

- **Bundled** (`--set valkey.enabled=true`): a single-replica Valkey StatefulSet
  that auto-wires `INQTRIX_VALKEY_URL`. Demo/dev only — one replica, no
  failover. `valkey.password` is required (no `change-me` fallback) and must use
  only URL-unreserved characters `[A-Za-z0-9._~-]`, because it is embedded
  verbatim into the derived URL.
- **External** (managed or self-operated): leave `valkey.enabled=false` and put
  your own `INQTRIX_VALKEY_URL` in `secret.data`, or in `secret.existingSecret`
  together with `config.INQTRIX_QUEUE_BACKEND=valkey`. The chart cannot read a
  Secret it does not manage, so it asks for that confirmation instead of
  starting pods that might lack the URL. This is also the route for a password
  outside the character set above (you own the URL-encoding).

For production prefer external. Managed Valkey and managed Redis both work
(AWS ElastiCache, Azure Cache, Google Memorystore), as does any self-operated
server meeting the version floor.

The broker also backs the guest-link rate limit
(`INQTRIX_EDITOR_GUEST_LINKS_ENABLED`). The optional Langfuse trace backend runs
its **own** separate broker and is deliberately not coupled to this one.

### Choosing an engine

The bundled default is Valkey, the BSD-licensed Linux Foundation fork of Redis.
Inqtrix is indifferent to the choice — every engine below serves the same command
set. Where it matters is licensing:

| Image | License | Notes |
|---|---|---|
| `valkey/valkey:9.x` | BSD-3-Clause | Chart default, digest-pinned and updated here. |
| `redis:8-alpine` | AGPLv3 **or** RSALv2 **or** SSPLv1 | Current Redis line; elect AGPLv3 for an OSI-approved option. |
| `redis:7.2-alpine` | BSD-3-Clause | Feature-frozen since March 2024, still receiving security patches (7.2.15, July 2026). The end of that window is set by Redis Ltd., not by you. |
| `redis:7-alpine` | RSALv2 / SSPLv1 | **Avoid** — the tag resolves to 7.4.x, the only line with no open-source option. |

### Running the bundled service on Redis

Set the engine **and** an image of that engine, always together; the chart does
not cross-check them, and a Redis image left on the default engine fails only at
container start (`executable file "valkey-server" not found in $PATH`):

```bash
helm upgrade --install inqtrix deploy/helm/inqtrix -n inqtrix \
    --set migrations.maintenanceConfirmed=true \
    --set valkey.enabled=true \
    --set valkey.engine=redis \
    --set valkey.image=docker.io/library/redis:8-alpine@sha256:<DIGEST> \
    --set valkey.password=<URL-SAFE-PASSWORD> \
    --set worker.enabled=true
```

`valkey.engine` accepts `valkey` (default) and `redis`; it selects
`valkey-server`/`valkey-cli` or `redis-server`/`redis-cli`. Every server flag the
chart passes (`--requirepass`, `--appendonly`, `--appendfsync`, `--dir`) and the
`REDISCLI_AUTH` variable used by the probes are identical on both. Any other
engine value fails the render.

**Only `valkey/valkey` is digest-pinned and updated by this project.** Selecting
another engine makes the image yours to pin and to patch.

> **Switching engines discards the queue.** Valkey and Redis write incompatible
> RDB/AOF files, so a swap needs a fresh volume. Drain first — let running runs
> reach a terminal state, then delete the StatefulSet's PVC before the upgrade.
> Nothing durable is lost, because the stream carries only dispatch messages and
> the Postgres run row stays the source of truth, but in-flight messages are, and
> their runs would only come back through the worker reconciler.

On Docker Compose the same choice is made with `INQTRIX_BROKER_ENGINE` and
`INQTRIX_BROKER_IMAGE` in `deploy/.env.stack`; see
[`deploy/.env.stack.example`](../../deploy/.env.stack.example).

## Object store

The chart offers three object-store options:

- **Local**: files on the pod's own storage (`emptyDir`, or a PVC when
  `persistence.enabled=true`). Only the [single-pod variant](#the-single-pod-variant)
  can use it: the chart refuses the local backend together with the worker,
  and refuses more than one API pod without the worker. The PVC is mounted
  only while the effective backend is `local`.
- **Bundled S3 (MinIO)**: `--set s3.enabled=true` starts an in-cluster MinIO and
  auto-wires the object-store backend, endpoint, bucket and credentials; the
  bucket is created on first upload. Demo/dev (single-pod, not HA).
  `s3.secretKey` is required even for a local trial (at least eight characters);
  there is no default credential.
- **External S3** (AWS S3, MinIO, R2, SeaweedFS, ...):

  Set `INQTRIX_S3_AUTH_MODE=static` with keys for an S3-compatible
  provider, or `default` with API/worker ServiceAccount annotations for AWS/
  ROSA workload identity. Native AWS leaves `INQTRIX_S3_ENDPOINT_URL` empty;
  managed buckets should set `INQTRIX_S3_BUCKET_PROVISIONING=existing`.

The default topology (with the worker) needs shared S3, bundled or external. For
production, prefer external/managed S3; put static keys in
`secret.existingSecret`, or grant only API/worker a workload identity. The
complete IAM, CA and SSE-KMS contract is [Object storage](object-storage.md).

## Optional: Langfuse trace backend (observability.tracing)

Traces of the agentic runs (`INQTRIX_TRACING`, see
[Settings and environment variables](../configuration/settings-and-env.md#observability-observabilitysettings))
export via OTLP/HTTP. The chart wires the API **and** worker pods
identically — one trace spans submit and execution.

**Path 1 — existing Langfuse server (the enterprise minimum).** No
in-cluster deployment at all; connect by configuration only:

```bash
kubectl -n inqtrix create secret generic langfuse-otlp \
    --from-literal=OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' 'pk-lf-…' 'sk-lf-…' | base64),x-langfuse-ingestion-version=4"

helm upgrade inqtrix deploy/helm/inqtrix --reuse-values \
    --set migrations.maintenanceConfirmed=true \
    --set observability.tracing.enabled=true \
    --set observability.tracing.otlpEndpoint=https://langfuse.example.com/api/public/otel \
    --set observability.tracing.headersSecret.name=langfuse-otlp
```

Depth, log format and retention are separate knobs in the same block:

```bash
helm upgrade inqtrix deploy/helm/inqtrix --reuse-values \
    --set migrations.maintenanceConfirmed=true \
    --set observability.tracing.profile=forensic \
    --set observability.logFormat=json \
    --set observability.retention.auditDays=365 \
    --set observability.retention.usageDays=730
```

- `tracing.profile` (`OBSERVABILITY_PROFILE`) decides HOW DEEP; `tracing.mode`
  decides WHERE TO. `forensic` without a recording sink — mode `off`/`local`,
  or `sampleRate` below `1.0` — records only part of what was asked for and
  warns at startup.
- `logFormat: json` makes stdout the machine-readable sink carrying the full
  configured level plus the correlation envelope.
- Both retention jobs run in the WORKER process. Without `worker.enabled`
  these values describe an intent, not an enforced policy; prune externally
  through the same `audit_prune` / `llm_usage_prune` functions instead.

**Path 2 — co-deploy Langfuse in-cluster.** Install the OFFICIAL chart as a
separate release (never vendored into this chart) and point path 1's values
at its service:

```bash
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm install langfuse langfuse/langfuse -n langfuse --create-namespace \
    -f my-langfuse-values.yaml
# otlpEndpoint: http://langfuse-web.langfuse.svc:3000/api/public/otel
```

In `my-langfuse-values.yaml`, reuse existing infrastructure instead of the
bundled subcharts where you have it; ClickHouse stays deployed — it is the
net-new component (plan ~2 CPU / 8 GiB and a >=100 GB volume). Put every
credential in `existingSecret`-style values, never inline.

**Retention.** The built-in per-project retention of self-hosted Langfuse
is an Enterprise feature; without a license key plan the trace cleanup
job on the Inqtrix side (batch `DELETE /api/public/traces` by age) before
volume accumulates.

## Scaling

The default topology scales horizontally wherever the components allow it:
API, worker and web gateway take replicas, the collaboration service is one
process that you size vertically, and the backing services are scaled by
whoever runs them. This section says what each component carries, how
connections reach the API pods, which value relieves which bottleneck, and
how to size from measurements.

Figures carry their origin. **Measured** means read on a local reference
stack; **derived** means computed from measured differences, assuming a
constant cost per unit; **offline** means replayed outside the service with
the same libraries and a controlled garbage collector; **extrapolated** means
calculated beyond what was measured. The reference setup is described in
[Where the numbers come from](#where-the-numbers-come-from). Measure your own
installation before you rely on any of them.

### What each component carries

| Component | Replicas | Carries |
|---|---|---|
| API | `api.replicaCount` (default 1) or `api.autoscaling`; more than one requires the worker | One process with one event loop per pod. Every browser request that the web gateway forwards; every server-sent event (SSE) stream: one per open tab for user events, one per open run view, and the chat answer stream while it is written; chat answers themselves, which are computed in the API process; editor AI calls; uploads; the relay of every collaboration WebSocket (one upstream connection to the collaboration service per browser socket); and the collaboration service's internal persistence calls, about one per persisted update, which reach the API through its Service. |
| Worker | `worker.replicaCount` (default 1) | Research and agent runs with their child runs, knowledge indexing, upload recovery, bulk deletions, the retention jobs for audit, usage and traces, and the sweep of orphaned objects. Each pod executes up to `INQTRIX_WORKER_CONCURRENCY` (default 100) jobs at once. |
| Collaboration service | exactly 1, `Recreate` | The live state of every open document, the validation of every update, and snapshots. One Node process with one JavaScript thread. |
| Web gateway | `web.replicaCount` (default 1) or `web.autoscaling` | The static interface and the proxy for HTTP, SSE and WebSocket traffic. One process per pod by default, each with its own pool of upstream connections to the API (`web.maxUpstreamConnections`, default 512). |
| PostgreSQL, Valkey, object store | bundled: 1 each | All durable state; dispatch messages only (the run row in PostgreSQL stays the source of truth); files. |

### How connections reach the API pods

- The API Service is a plain ClusterIP Service without session affinity.
  Kubernetes balances **new TCP connections** across the ready API pods; a pod
  whose `/readyz` fails receives no new connections. The web Service in front
  of the gateway pods works the same way one step earlier.
- The web gateway keeps up to 20 idle keep-alive connections to the API per
  gateway pod and closes those that stay idle for 5 seconds. Short requests
  therefore spread over the API pods as connections are renewed. Under
  continuous traffic a connection can stay on one pod for longer, so expect
  the spread to even out over time rather than per request (not measured with
  several API replicas).
- An SSE stream or a WebSocket is one long-lived connection and stays on its
  pod until it closes. After you add replicas, only new connections reach the
  new pods; existing streams move when the browser reconnects. Scaling down
  and rollouts close the streams of the pods that go away, and the browsers
  reconnect.
- The collaboration service's internal calls also go through the API Service
  over keep-alive connections, so they spread per connection, not per call
  (not measured).

### Which value relieves what

| Symptom | Value | Notes |
|---|---|---|
| API pods close to one core each, slow responses | `api.replicaCount` or `api.autoscaling` | Requires the worker. One process uses at most about one core, so add replicas instead of raising the CPU limit. An HPA target is a percentage of `api.resources.requests.cpu`. |
| Many open tabs or streams; a `503` naming `INQTRIX_MAX_UPSTREAM_CONNECTIONS` | `web.maxUpstreamConnections`, `web.replicaCount`, `api.replicaCount` | Every open SSE stream holds one gateway connection for its whole duration. |
| Runs wait in `queued` | `worker.replicaCount`, `INQTRIX_WORKER_CONCURRENCY`, `worker.resources` | Size the worker CPU from the number of runs that write reports at the same time. |
| Collaboration memory, or long pauses while typing in long documents | `collaboration.resources` | See [Collaboration service](#collaboration-service). There is no horizontal knob. |
| Collaboration CPU | none horizontal | The cost per update grows with the document size. |
| Web gateway CPU | `web.replicaCount`, `web.autoscaling` | |
| PostgreSQL connections | `pgbouncer.enabled` (bundled database), `postgres.maxConnections`, or an external pooler | Every API and worker process logs its worst-case connection budget at startup. |

Failed-login counters live in each API process, so every API replica adds a
full attempt budget per account and client address. Throttle the login
endpoints per address at the edge as well (see
[Production checklist](production-checklist.md#login-rate-limiting)).
Password-protected guest links throttle through Valkey and share one budget
across replicas.

### Sizing rules

#### API

- At most about one core per pod: one process with one event loop serves
  every request, and its other threads stayed below 0.02 cores in a profile
  (measured).
- Idle, about 0.05 cores (measured).
- Each open browser tab, about 3.5 to 4 ms of CPU per second (measured in
  total for 25 tabs, per tab derived).
- Each persisted collaboration update, about 6 ms of API CPU at light load and
  about 13 ms under sustained typing, for documents of two to three pages
  (derived).
- A sign-in wave of 22 people within 28 seconds ran the API at 0.88 cores and
  opened about 25 new PostgreSQL sessions per second (measured).
- Chat answers are computed in the API process; their cost depends on the
  provider and the answer and was not isolated.
- Starting point: the default request of 250m without collaboration, and
  `api.resources.requests.cpu: 1` per replica with `collaboration.enabled`, so
  that the scheduler reserves what one process can use and the HPA reacts to
  real load. Watch the observed usage and adjust.

#### Worker

- A compact research run used about 0.05 cores on average and about 0.3 cores
  while writing its report; two runs writing reports at once used 0.53 to 0.67
  cores (measured in total, per run derived).
- Memory follows the run cap; the rationale is in
  [`values.yaml`](../../deploy/helm/inqtrix/values.yaml).

#### Collaboration service

- Memory: about 126 to 135 MiB when idle, and 210 to 315 MiB after work,
  because the heap keeps its grown size (measured).
- Node caps its heap at about half the container memory limit: 512Mi allows
  about 259 MiB of heap, 1Gi about 524 MiB, 2Gi about 1048 MiB (offline).
  Neither the chart nor Compose sets `NODE_OPTIONS`.
- An open document costs little: about 0.075 MiB of heap per page (offline);
  twenty open 85-page documents added 73 MiB (measured).
- Typing in long documents dominates, because every update clones and
  validates the whole document. Four people typing in 85-page documents drove
  the service to about 1020 MiB while they typed, and it fell back to about
  300 MiB within about 8 seconds after they stopped; in 20-page documents they
  added about 23 MiB, in 2-page documents about 1.4 MiB (measured).
- A tight limit slows typing rather than crashing the service: replaying the
  85-page case reached 14.2 updates per second without a limit, 4.4 under 1Gi
  and 1.3 under 512Mi (offline).
- Default `requests.memory: 512Mi`, `limits.memory: 1Gi`. For teams that type
  concurrently in documents of about 50 pages or more, use `1Gi` / `2Gi`: its
  heap of about 1048 MiB holds the heap peak of the measured 85-page case
  (786 MiB, offline).
- Rule of thumb (extrapolated from three document sizes; the typing term
  rests on one data point): B ≈ 300 MiB + 0.1 MiB per page of every loaded
  document + 2 MiB per page of every document of about 50 pages or more in
  which someone is typing. Request at least B, limit at least 2 × B. The rule
  is conservative: for the measured case (twenty 85-page documents, four
  people typing) it gives about 1150 MiB against 1023 MiB measured and so
  asks for slightly more than the `1Gi` / `2Gi` variant, whose heap already
  holds that case's heap peak. Use the rule for cases beyond the measured one.
- A loaded document is the active document of every open tab plus up to three
  recently viewed ones, which stay loaded for three minutes.
- CPU per persisted update: about 7 to 15 ms for 2 to 3 pages, about 28 ms for
  20 pages and about 170 ms for 85 pages (derived). With 85-page documents the
  service processed about 6.9 updates per second at 1.17 cores (measured).
- The default CPU request of 100m reserves less than sustained typing uses: at
  12 to 15 ms per update, 20 updates per second already take 0.25 to 0.3
  cores (extrapolated). Raise `collaboration.resources.requests.cpu` to the
  observed usage.
- Every change to `collaboration.resources` restarts the single replica
  (`Recreate`) and briefly disconnects all editors.

#### Web gateway

- About 0.24 ms of CPU per forwarded WebSocket frame (derived). A reader of
  an active document receives about 25 frames per second (measured).

#### PostgreSQL

- 56 to 67 of 300 connections were in use with one API, one worker and the
  collaboration service under mixed load (measured, single reading). Each
  additional API or worker replica brings its own pool.
- Several request paths open about one new connection per user request
  (measured), so the connection rate grows with open tabs and clicks rather
  than with typing.
- Each persisted collaboration update writes about 2 to 2.4 kB of WAL and
  grows the tables by about 1.2 kB (measured). Update payloads that a
  snapshot covers may be pruned after 24 hours
  (`INQTRIX_COLLABORATION_UPDATE_PAYLOAD_RETENTION_SECONDS`), while their
  attribution metadata stays 90 days
  (`INQTRIX_COLLABORATION_ACTIVITY_RETENTION_SECONDS`); the size of that
  metadata was not measured.

#### Valkey

- About 2 operations per second for the workers' idle polling (measured).
  Dispatch and guest-link rate limiting grow with use (not measured).

#### Cost per user

"Typing continuously" means a synthetic typist at about 425 keystrokes per
minute without pauses, roughly four times a person. The connection counts and
request rates in this table are measured.

| User | Long-lived connections | HTTP requests per minute | Server cost |
|---|---|---|---|
| Editor, typing continuously | 1 WebSocket + 1 SSE | about 4 | 3.5 to 4 persisted updates per second under load (measured), each about 13 ms of API CPU and 12 to 15 ms of collaboration CPU (derived) |
| Editor tab open, not typing | 1 WebSocket + 1 SSE | about 4 | about 3.5 ms of API CPU per second (derived); about 25 incoming frames per second while others type in the document (measured) |
| Navigating the interface | 1 SSE | about 7.4, 16.6 with static files | |
| Chat, one turn about every 30 seconds | 1 SSE + the answer stream | about 11, about 5 per turn | answer computed in the API |
| Compact research run | 2 SSE | 2 to 3 | worker about 0.3 cores while it writes the report |

#### Worked example (extrapolated)

100 users, 50 of them active at once, with an assumed mix: 15 editor users in
6 documents who type a quarter of the time, 10 readers in those documents, 8
chat users with one turn every two minutes, 6 research users and 6 agent users
with 2 runs each in flight at a time, and 5 people navigating; another 25 tabs
stay open without activity.

- API, with the worker: about 0.65 to 0.7 cores on average on one replica,
  peaks about 0.9 (report phases, many people typing, sign-in waves). Two to
  three replicas keep each near 0.25 to 0.35 cores and leave room for peaks:
  `api.replicaCount: 2` or `api.autoscaling` with `minReplicas: 2`, and
  `api.resources.requests.cpu: 1`, since collaboration is on.
- Worker: one replica; raise its CPU request toward about 0.3 cores per run
  that writes its report at the same time.
- Collaboration service: size the memory with the rule above from your
  document lengths, not from the number of users.
- PostgreSQL: about 70 to 80 connections, plus about 17 to 30 per further API
  replica; about 6 to 7 new connections per second on average; WAL about
  130 kB per second during working hours.

This guide gives no throughput ceiling for the editor path. Size from the
per-unit costs above and confirm with a load test on your installation.

#### Where the numbers come from

One host with 14 cores, containers in a VM with 7 virtual CPUs, browsers and
servers on the same host, localhost without TLS or network shaping, one
tenant, one API replica and one worker. The API and collaboration CPU per
update under continuous typing was measured with documents of two to three
pages; the collaboration service's memory, and its CPU per update for 20- and
85-page documents, with up to 85 pages and four people typing at a human pace.
Each figure comes from a single run. Because browsers and servers shared the
host, latencies from this setup are upper bounds.

### The single-pod variant

`worker.enabled=false` runs every job in the API process and needs only
PostgreSQL (or the memory backend for a stateless trial). The chart then
allows exactly one API replica, which may use the local object store. The API
pod keeps itself honest: an active run whose executing thread is gone (crash,
storage outage mid-write) is failed as `execution_lost` within about a minute
of the next read, a run that never finds an executor fails as `queued_timeout` after
24 hours, and a last-resort failsafe fails anything still non-terminal after 7
days. No run stays "running" forever and no run silently disappears. The
limits: a restart mid-run surfaces as a visible failure rather than resuming
(crash resume needs the worker), the variant supports exactly one API replica,
and the retention jobs (traces, audit, usage ledger) do not run because they
live in the worker process; the admin System page flags this.

### Editor collaboration

Editor collaboration is an exception to the horizontal-scaling advice.
Version 1 always renders exactly one collaboration replica with `Recreate` and
database fencing. Do not add an HPA, PDB, or second replica; Redis/Valkey is not
a persistence or multi-writer substitute for this service.

## Upgrades and migrations

Schema migrations run automatically as a Helm hook Job (`inqtrix-migrate`): a
`pre-upgrade` hook on every upgrade (and on install, `pre-install` for an external
database or `post-install` when Postgres is bundled). Helm blocks until it
succeeds, so a failed migration fails the release. A normal upgrade within
the same chart version is:

```bash
helm upgrade inqtrix deploy/helm/inqtrix -n inqtrix --reuse-values \
    --set migrations.maintenanceConfirmed=true \
    --set image.api.tag=<NEW-TAG> --set image.web.tag=<NEW-TAG> \
    --set image.api.digest=sha256:<NEW-API-DIGEST> \
    --set image.web.digest=sha256:<NEW-WEB-DIGEST>
```

`migrations.maintenanceConfirmed=true` is required on every upgrade of an
installed schema; without it the render fails. The maintenance hook it
authorizes stops the API, the worker and the collaboration service before the
migration, so every upgrade is a short outage. The hook waits up to
`migrations.maintenanceTimeoutSeconds` (default 300) for those pods to
terminate, and a draining worker may use its full termination grace period of
120 seconds. Keep the timeout above that grace period; chart versions before
0.2.0 gave the worker the Kubernetes default of 30 seconds, so check a lower
custom timeout before you upgrade.

For collaboration-enabled releases, update
`image.collaboration.tag=<NEW-TAG>` in the same maintenance window so the API,
browser schema, Node schema, and migration stay compatible.

Back up the database (and the object store) before upgrades — see [Runbooks](runbooks.md).
For managed PostgreSQL, give only the Job a direct migration Secret and select
`bypass` or `owner`. Every installed-schema upgrade requires
`migrations.maintenanceConfirmed=true`, which authorizes the chart's bounded
quiesce hook for API, worker, Collaboration and chart-owned PgBouncer. An
external pooler must be stopped and drained by its operator. Custom charts must
preserve the stop -> one-shot job -> readiness -> start ordering. Manual
`inqtrix-migrate` is break-glass only. See [Database migrations](database-migrations.md).

### Chart defaults on upgrade

`--reuse-values` reuses the values of the last release, including the defaults
of the chart version that installed it, so a changed chart default does not
reach a release upgraded that way. To adopt the current defaults, export your
values and pass them as a file instead:

```bash
helm get values inqtrix -n inqtrix -o yaml > inqtrix-values.yaml
chmod 600 inqtrix-values.yaml   # the file carries every --set value, secrets included
helm upgrade inqtrix deploy/helm/inqtrix -n inqtrix -f inqtrix-values.yaml \
    --set migrations.maintenanceConfirmed=true
rm inqtrix-values.yaml
```

Helm 3.14 and later also offer `--reset-then-reuse-values`. Compare
`helm get values inqtrix -n inqtrix --all` before and after the upgrade.

Chart versions before 0.3.0 defaulted `image.registry` to `ghcr.io/bzandi`,
the project's previous namespace; the current default is `ghcr.io/inqtrix`.
A release upgraded with `--reuse-values` keeps the old registry until you set
`image.registry`.

### Upgrading a release that runs without the worker

Chart versions before 0.2.0 defaulted to `worker.enabled=false`. When the
current defaults reach such a release and it lacks one of the worker's
prerequisites, the render fails with the message that names what is missing;
Helm renders before it runs hooks or applies anything, so the cluster stays
unchanged. A release that already has PostgreSQL, a Valkey URL and a shared
object store renders instead and starts the worker with that upgrade, so
prepare it like the second way below. Two ways forward:

1. **Keep the single-pod variant.** Add `worker.enabled: false` to your values.
   The manifests stay as before apart from the chart labels (which roll the
   pods), the collaboration resources described below, and, where S3 runs
   next to `persistence.enabled`, an unmounted PVC. A release that runs more
   than one API replica (or an HPA) without the worker is refused; reduce it to
   one replica, or take the second way, which removes the reason for the
   limit.
2. **Adopt the worker.**
   - Provide a Valkey queue and a shared object store, bundled (`valkey.enabled`
     and `s3.enabled` with their credentials) or external (see scenario C).
     With `secret.existingSecret`, add `INQTRIX_VALKEY_URL` to that Secret
     before the upgrade and set `config.INQTRIX_QUEUE_BACKEND=valkey`.
   - Switch in a quiet period without active runs. The maintenance hook stops
     the API pod, which asks its runs to stop; a run that does not stop before
     the pod is killed stays `running` until the 7-day failsafe fails it,
     because with the queue only the worker recovers runs. Queued runs are
     picked up by the worker.
   - Move files that the local object store holds on a PVC
     (`persistence.enabled=true`; an `emptyDir` already loses them on every
     pod restart). The object keys are the relative paths below
     `INQTRIX_OBJECT_STORE_PATH` (`/var/lib/inqtrix/objects`), so copy that
     tree 1:1 to the bucket root, skip `*.part` files (unfinished writes), and
     apply the server-side encryption the app uses
     (`INQTRIX_S3_SERVER_SIDE_ENCRYPTION`, `INQTRIX_S3_KMS_KEY_ID`). Order:
     copy, switch the backend (the PVC stays, but only the local backend mounts
     it), compare the object counts, then set `persistence.enabled=false`,
     which deletes the PVC. Copy during the quiet period: once the backend is
     switched, reaching the unmounted volume needs a helper pod.

### Collaboration resources on upgrade

Chart versions before 0.2.0 defaulted the collaboration service to
`128Mi` / `512Mi`. A release that adopts the current defaults gets
`512Mi` / `1Gi`; one upgraded with `--reuse-values` keeps the old values until
you set `collaboration.resources` explicitly. Either change restarts the
single replica (`Recreate`) and briefly disconnects all editors, so plan it
for a maintenance window. Sizing: [Collaboration service](#collaboration-service).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pods `CrashLoopBackOff`, logs show a DB/connection error | `INQTRIX_DATABASE_URL` missing or wrong, or storage backend is `postgres` with no DB | Provide the Secret (scenario C) or bundle Postgres (scenario A/B); verify the URL. |
| Pod `CrashLoopBackOff` with `Read-only file system: 'logs'` | `INQTRIX_LOG_ENABLED=true` under the read-only root filesystem | Logs already go to container stdout (captured by the cluster). Leave file logging off; the chart mounts an ephemeral `/app/logs` so the flag does not crash, but stdout is the durable sink. |
| `helm install` hangs on the migrate hook | The database is unreachable from the cluster | Check `kubectl -n inqtrix logs job/inqtrix-migrate`; fix connectivity/credentials. |
| Migration fails with SQLSTATE `28000` / `inqtrix.tenant_id is not set` | A normal managed-PostgreSQL role touched a forced-RLS table, or the custom chart reused the runtime credential | Configure the migration-only Secret and explicit `bypass`/`owner` mode; do not set a tenant env variable. See [Database migrations](database-migrations.md). |
| `/readyz` says database unavailable and product routes return `database_not_ready` | Confirmed contract break (old schema, `SET ROLE` failure, privileged role) or the boot-time check never passed. Check the readyz body's `database_contract` field: `violation` is a confirmed break; `unavailable` is mere unreachability, which keeps product routes serving until it persists past the bounded keep-open window | Inspect the failed migration Job and role grants. Do not bypass readiness or start workers until the contract passes. |
| `runtime session login must be LOGIN NOSUPERUSER NOBYPASSRLS …` | `INQTRIX_DATABASE_URL` uses the external database's admin account | Create the unprivileged runtime login from [step 0](#step-2-scenario-c-production-with-an-external-database-vanilla-kubernetes). Do **not** set `INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=bundled_legacy` to silence it — that runs the app as a database superuser. |
| Migration Job: `migration role requires USAGE and CREATE on the active schema` | The Job is using the runtime login (migration Secret missing, misnamed, or a custom chart reused the runtime Secret) | Check `migrations.databaseSecret.name`; it must differ from the runtime Secret and carry the migration login. |
| Migration Job: `fresh installation requires ADMIN OPTION on the pre-created inqtrix_app role` | The application role exists but was not granted to the migration login with `ADMIN OPTION` | `GRANT inqtrix_app TO <migration-login> WITH ADMIN OPTION;` as an administrator. |
| Migration Job: `migration role does not own or inherit ownership of tenant tables` | Objects still belong to a previous owner (moved database, or schema created by another account) | `REASSIGN OWNED BY <previous-owner> TO <migration-login>;` as an administrator. |
| Migration Job: `found N other database client session(s)` | Workloads or an external pooler still hold connections | Scale API, worker and Collaboration to zero, drain operator-managed poolers, then rerun. The chart automates this with `migrations.maintenanceConfirmed=true`. |
| Migration Job: `installed schema uses FORCE ROW LEVEL SECURITY but the migration role is neither SUPERUSER nor BYPASSRLS` | `migrations.rlsMode=auto` against a managed database | Set `migrations.rlsMode=owner` (plus `maintenanceConfirmed=true`), or supply a dedicated `BYPASSRLS` login and use `bypass`. |
| Turnkey install with bundled Postgres hangs when `--wait` is added | Helm can wait for normal resources before the post-install migration hook has run | Install scenario A without `--wait`, then run `kubectl wait` as shown above. |
| Bundled services are enabled but a Secret key is missing | `secret.existingSecret` was set, so the chart cannot render derived values into the selected Secret | Add the app connection value and matching backing-service credential listed above (`INQTRIX_BUNDLED_POSTGRES_PASSWORD`, `INQTRIX_BUNDLED_VALKEY_PASSWORD`, Qdrant key, or S3 key pair), or use the chart-managed Secret for the demo. |
| S3 uploads fail against `http://seaweedfs:8333` | That hostname is the Compose stack's SeaweedFS; the chart's bundled store is MinIO under a different Service name | Bundle the store with `s3.enabled=true` (auto-wires `INQTRIX_S3_ENDPOINT_URL`), or set `INQTRIX_S3_ENDPOINT_URL` to your external endpoint. |
| `ImagePullBackOff` | Registry not reachable or private without a pull secret | Push to a reachable registry; add `imagePullSecrets`. |
| Render fails: `worker.enabled=true (the chart default) ... needs: ...` | The default worker lacks the Postgres run store, a Valkey queue or a shared object store | Provide what the message names (scenario A bundles everything, scenario C lists the external values), or set `worker.enabled=false` for the [single-pod variant](#the-single-pod-variant). |
| Render fails: `... contradicts worker.enabled=true` | `config.INQTRIX_QUEUE_BACKEND=memory` together with the worker | Remove the key; the chart derives `valkey`. |
| Render fails: `... renders none, so queued runs would never start` | `INQTRIX_QUEUE_BACKEND=valkey` without the worker | Enable the worker, or remove the key. |
| Render fails: `... supports exactly one API replica` | More than one API replica, or an HPA, without the worker | Enable the worker, or run one API replica. |
| API and worker `CrashLoopBackOff`; logs name `INQTRIX_VALKEY_URL` | `config.INQTRIX_QUEUE_BACKEND=valkey` confirms a URL that `secret.existingSecret` does not contain | Add `INQTRIX_VALKEY_URL` to the Secret, then restart the API and worker Deployments. |
| Collaboration pod is ready but `service_available=false` | API/Node secrets differ, private API is unreachable, or the instance fencing lease was not acquired | Compare Secret references, inspect API and collaboration logs, and verify one collaboration replica only. |

Inspect a deployment:

```bash
kubectl -n inqtrix get pods
kubectl -n inqtrix logs deploy/inqtrix-api
kubectl -n inqtrix logs job/inqtrix-migrate
helm test inqtrix -n inqtrix          # /readyz; also requires S3 when configured
```

## Related docs

- [Deploy editor collaboration](editor-collaboration.md) - secret, readiness, backup, and kill-switch details.
- [Auth modes](auth-modes.md) - full `none`/`apikey`/`local`/`oidc` reference.
- [Settings and env](../configuration/settings-and-env.md) - every environment variable.
- [Runbooks](runbooks.md) - backup, restore, and operational procedures.
