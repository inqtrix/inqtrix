# Production checklist

## Scope

What to change when you take the product compose from a laptop to a real
deployment: TLS, secrets, cookie security, backups, and the hardening of each
opt-in backend. It assumes you already have the stack running locally
([Stack quickstart](../getting-started/stack-quickstart.md)) and builds on
the component map ([Platform components](../getting-started/platform-components.md)).
It is not a Kubernetes manifest — the same env contract applies whatever the
orchestrator.

## TLS and the public URL

Terminate TLS at your ingress/load balancer (or directly in the bundled Python gateway)
and serve the app over HTTPS. The public origin must match how the browser
reaches the app, because cookies and the OIDC callback derive from it:

```dotenv
INQTRIX_PUBLIC_BASE_URL=https://research.example.com
# Optional only when an additional trusted proxy needs a scheme-only override:
# INQTRIX_EXTERNAL_SCHEME=https
```

`INQTRIX_PUBLIC_BASE_URL` is the primary trust anchor consumed by both API and
web gateway; it supplies the complete scheme and authority. The optional
`INQTRIX_EXTERNAL_SCHEME` is only a scheme-level override for unusual
multi-proxy topologies. When both are set they must agree, otherwise the web
gateway fails at startup. See [Deploy editor collaboration](editor-collaboration.md)
for the full origin-trust chain.

Over HTTPS, leave secure cookies ON — that is the default. **Never** set
`INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` in production; it drops the `Secure`
flag and the `__Host-` prefix and exists only for `http://localhost` dev.

## Secrets

These must be set to real, high-entropy values and, for multi-replica
deployments, **shared identically** across every api/worker replica (a session
minted on one replica must validate on another):

| Secret | Why |
|---|---|
| `INQTRIX_SESSION_SECRET` | HMAC key for the CSRF double-submit token (local/oidc); the session cookie itself is an opaque random id, so rotating this invalidates outstanding CSRF tokens while sessions survive. |
| `INQTRIX_PAT_PEPPER` | Pepper for personal-access-token hashing. |
| `INQTRIX_PG_PASSWORD` | Database auth; the visible `INQTRIX_DATABASE_URL` interpolates this one secret. |

Generate them with a CSPRNG and inject via your secret manager, not the repo:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # one per secret
```

Any `CHANGE_ME_*` placeholder left in place is logged loudly at startup as
insecure (No Silent Fallbacks) — treat that log line as a release blocker.

## Storage durability

Production runs the Postgres backend; normal Compose deployments apply
migrations through the orchestrated one-shot `migrate` service before the API
starts. The equivalent Helm deployment uses its one-shot migration Job. A
direct `uv run inqtrix-migrate` or pip-installed `inqtrix-migrate` invocation
is a break-glass operation only after the documented workload drain and backup,
not a parallel normal startup path. Without Postgres everything is in-memory
and lost on restart, including sessions and the user mirror — so a restart
logs everyone out.

## Backups

Back up two things:

- **Postgres** — runs, identity/credentials, knowledge metadata, prompt
  templates, quotas.
- **The object store** — uploaded file blobs.

See [Runbooks](runbooks.md#backup) for the commands and the restore order.

## Hardening the opt-in backends

| Backend | Hardening |
|---|---|
| Qdrant (`--profile knowledge`) | Self-hosted Qdrant is unauthenticated by default — set `INQTRIX_QDRANT_API_KEY` and keep it off the public network. |
| Valkey + workers (`--profile workers`) | Set a strong `INQTRIX_VALKEY_PASSWORD`; the worker refuses to start without Postgres + Valkey. |
| Object store S3 | Configure a managed/native S3 endpoint without the bundled profile; use real credentials or workload identity, a private bucket, and TLS. |
| OIDC | Configure your external IdP without the bundled profile and register the exact `https://…/api/auth/callback`; Dex is a dev reference, not for production. |

Misconfiguration fails loudly at startup (e.g. `INQTRIX_QUEUE_BACKEND=valkey`
without a URL, or `valkey` without `postgres`) rather than degrading silently.

## Auth in production

Pick a real auth mode — `local` (email/password, owner setup)
or `oidc` (your IdP). The
single-operator `none`/`apikey` modes have no scoped identity and no sharing;
use them only for a locked-down single-user instance.

## Login rate limiting

The credential check is uniform-timing (no email-enumeration oracle) AND the
login endpoints are throttled: after `INQTRIX_LOGIN_RATE_LIMIT_MAX_ATTEMPTS`
failures (default 10) for a `(identifier, source_ip)` within
`INQTRIX_LOGIN_RATE_LIMIT_WINDOW_SECONDS` (default 300) the key is locked for
`INQTRIX_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS` (default 60) and further attempts get
`429`. On by default; tune or disable via `INQTRIX_LOGIN_RATE_LIMIT_ENABLED`.

The counters are **process-local**, so a **multi-replica** deployment should
ALSO rate-limit per-IP at the reverse proxy / WAF (e.g. on `/api/auth/login/*`)
— the in-app limit then complements, not replaces, the edge limit.

The client IP is read from the **right** of `X-Forwarded-For`, at the depth set
by `INQTRIX_TRUSTED_PROXY_HOPS` (default `1`, matching the single selected web
adapter in the `web` container: the packaged Python gateway unless another is
explicitly selected). Both adapters append the real peer. The right-most hop is
therefore not client-spoofable. Set it to the **exact** number of chained
proxies in front of the server (e.g. `2` for an external load balancer in front
of the bundled adapter); a value **higher** than the real chain lets a client
spoof again. Set it to `0` only when the API server is exposed directly with no
reverse proxy — then just the socket peer is trusted. The reference row is
[Settings and env](../configuration/settings-and-env.md#auth-authsettings).

## Pre-flight checklist

- [ ] HTTPS at the ingress; `INQTRIX_PUBLIC_BASE_URL` is the exact public `https://` origin. If `INQTRIX_EXTERNAL_SCHEME` is explicitly set, it is also `https`.
- [ ] `INQTRIX_OIDC_INSECURE_DEV_COOKIES` unset/false.
- [ ] `SESSION_SECRET`, `PAT_PEPPER`, `PG_PASSWORD` real and shared across replicas; no `CHANGE_ME_*` in the startup log.
- [ ] Postgres backend + migrations applied; backups scheduled for Postgres and the blob store.
- [ ] Each enabled profile hardened (Qdrant key, Valkey password, S3 creds, OIDC callback).
- [ ] A real auth mode chosen; login endpoints rate-limited at the proxy.

## Related docs

- [Stack quickstart](../getting-started/stack-quickstart.md) — the local starting point.
- [Platform components](../getting-started/platform-components.md) — which backend each feature needs.
- [Runbooks](runbooks.md) — start/stop/update/restore operations.
- [Auth modes](auth-modes.md) — choosing and wiring auth.
- [Deploy editor collaboration](editor-collaboration.md) — origin trust
  chain and TLS requirements for live editing.
