# Build a UI on Inqtrix

## Scope

How to drive Inqtrix from your own client — a custom frontend, a CLI, a
notebook, another service. Covers authenticating, the native run lifecycle
(`/v1/runs*`) with its event stream, the OpenAI-compatible chat endpoint, and
the capability manifest. The bundled Research Desk
([`apps/research-desk/`](../../apps/research-desk)) is itself just a client of
this API — its [`src/api/inqtrixClient.ts`](../../apps/research-desk/src/api/inqtrixClient.ts)
is a complete worked reference.

## Authenticate

Two ways to call the API, depending on the auth mode:

- **Personal access token** (any cookie-session mode) — mint one in Settings →
  Account → Access tokens, then send it as a Bearer header. Best for
  programmatic clients:

  ```bash
  curl -s http://localhost:8080/v1/runs -H "Authorization: Bearer ipat_..."
  ```

  A `Bearer` header routes exclusively to token verification — it never falls
  back to a cookie, so a wrong token fails cleanly.

- **Cookie session** (browser clients) — log in via `/api/auth/login/local`
  or the OIDC redirect; the server sets an HttpOnly session cookie. Send cookies
  with `credentials: 'include'` and, on every unsafe method (POST/PATCH/DELETE),
  echo the CSRF token in the `X-CSRF-Token` header (OWASP signed double-submit).
  Read the token from the `csrf_token` field of `GET /api/auth/session` — that
  is name-independent and is exactly what the bundled SPA uses. (The raw cookie
  is `__Host-inqtrix_csrf` over HTTPS and `inqtrix_csrf` only on
  `http://localhost` dev, so prefer the session payload.)

In `none` mode no auth is required; in `apikey` mode send the one shared Bearer
token.

## Discover what is available

```bash
curl -s http://localhost:8080/health          # provider/model identity, auth_mode
curl -s http://localhost:8080/v1/capabilities # feature flags (knowledge, files, sharing, …)
```

Gate your UI on `capabilities.features.*` so it degrades visibly when a backend
is off — never assume a feature is present.

## Native run lifecycle (`/v1/runs*`)

A research run is asynchronous: create it, stream its events, fetch the final
result.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/runs` | Create a run; returns a summary with the `run_id`. |
| `GET` | `/v1/runs` | List the caller's runs. |
| `GET` | `/v1/runs/{run_id}` | Run status/summary. |
| `GET` | `/v1/runs/{run_id}/events` | Server-Sent Events stream of progress. |
| `GET` | `/v1/runs/{run_id}/result` | The final `ResearchResult` (markdown + citations). |
| `POST` | `/v1/runs/{run_id}/cancel` | Cancel an in-flight run. |

```bash
# Create (responds 202 with the run summary; the id key is run_id)
RUN=$(curl -s -X POST http://localhost:8080/v1/runs \
  -H "Authorization: Bearer ipat_..." -H 'Content-Type: application/json' \
  -d '{"question":"What changed in EU AI Act enforcement in 2026?"}' | jq -r .run_id)

# Stream progress (SSE; keep the connection open)
curl -s -N http://localhost:8080/v1/runs/$RUN/events -H "Authorization: Bearer ipat_..."

# Fetch the answer when done
curl -s http://localhost:8080/v1/runs/$RUN/result -H "Authorization: Bearer ipat_..."
```

The events stream is `text/event-stream`; each `data:` frame is a JSON event
(planning, search, evaluation, answer, …). Buffering is disabled server-side so
events arrive promptly — if you proxy the API, keep `proxy_buffering off` for
`/v1/`.

## OpenAI-compatible chat

For a synchronous, drop-in path, `POST /v1/chat/completions` speaks the
OpenAI Chat Completions shape (including `stream: true`), so existing OpenAI
client libraries work by pointing their base URL at Inqtrix. Use this for chat;
use `/v1/runs` for long-running, multi-round research with progress.

## Workspace scoping

In multi-user modes, scope a request to a workspace with the
`X-Inqtrix-Workspace-Id` header (omit it for the caller's default scope). Shared
resources appear in the list endpoints with their access role.

## Related docs

- [Create and manage users](../deployment/create-and-manage-users.md) — minting and revoking PATs.
- [Public API](../architecture/library-api.md) — the typed `ResearchResult` payload that `GET /v1/runs/{run_id}/result` returns.
- [Web server mode](../deployment/webserver-mode.md#endpoints) — the endpoint surface per feature.
- [Runs API](runs-api.md) — the SSE event types you can render.
- [Auth modes](../deployment/auth-modes.md) — how each mode authenticates a request.
