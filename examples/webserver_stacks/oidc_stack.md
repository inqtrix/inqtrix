# OIDC stack walkthrough (browser SSO with Dex)

> Files: `deploy/compose/compose.stack.yaml`, `deploy/compose/compose.dev-ports.yaml`, `deploy/compose/dex-config.yaml`, `src/inqtrix/server/routers/auth.py`, `src/inqtrix/auth/oidc.py`
>
> Every `INQTRIX_OIDC_*` / `INQTRIX_AUTH_MODE` variable (defaults, allowed values) is defined in [`docs/configuration/settings-and-env.md`](../../docs/configuration/settings-and-env.md), the single source of truth for env vars; this page is the walkthrough.

## Scope

End-to-end recipe for `INQTRIX_AUTH_MODE=oidc`: start the Dex reference
IdP from the dev compose stack, point any webserver-stack script (or
`python -m inqtrix`) at it, log in through the browser, and verify the
session over curl. Dex is the recommended dev IdP, never an
architecture component — any OIDC-compliant provider works with the
same env vars. The browser never sees a token: the server is a
confidential BFF client and stores only an opaque session cookie.

## 1. Start the IdP

Dex is gated behind the compose profile `oidc`; without that profile Compose
does not start the bundled IdP and leaves the configured authentication mode
unchanged. Start from the named local pair described in the
[local infrastructure guide](../../docs/development/local-infrastructure.md#start--stop). Keep the
issuer/client id in `deploy/.env.stack.local` and add the client credential
only to `deploy/.env.stack.secrets.local`:

Visible configuration:

```dotenv
INQTRIX_AUTH_MODE=oidc
INQTRIX_OIDC_ISSUER=http://dex.localhost:5556/dex
INQTRIX_OIDC_CLIENT_ID=inqtrix-local
```

Credentials (mode `0600`):

```dotenv
INQTRIX_OIDC_CLIENT_SECRET=replace-with-a-local-dev-secret
```

```bash
podman compose \
  -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.dev-ports.yaml \
  --env-file deploy/.env.stack.secrets.local \
  --env-file deploy/.env.stack.local \
  --profile oidc up -d dex
```

(`docker compose` works identically.) The issuer is
`http://dex.localhost:5556/dex` — loopback-published, so the backend
(discovery/token calls) and the browser (authorize redirect) reach it
under the identical string. Health check:

```bash
curl http://dex.localhost:5556/dex/healthz
```

`dex-config.yaml` registers one static client and one demo user:

| What | Value |
|---|---|
| Client id | `inqtrix-local` |
| Client secret | from `INQTRIX_OIDC_CLIENT_SECRET` in the selected companion secrets file; there is no committed credential default |
| Redirect URIs | `http://127.0.0.1:5100/api/auth/callback` and `http://127.0.0.1:5173/api/auth/callback` |
| Demo login | `admin@example.com` / `password` |

## 2. Configure and start the server

Every webserver-stack script builds its auth provider through
`create_app(...)`, so OIDC needs env vars only — no code change:

| Variable | Dev value | Notes |
|---|---|---|
| `INQTRIX_AUTH_MODE` | `oidc` | Explicit mode; unset would infer `apikey`/`none` from `INQTRIX_SERVER_API_KEY`. |
| `INQTRIX_OIDC_ISSUER` | `http://dex.localhost:5556/dex` | Must equal the Dex issuer byte-for-byte; discovery is fetched from `{issuer}/.well-known/openid-configuration`. |
| `INQTRIX_OIDC_CLIENT_ID` | `inqtrix-local` | The static client from `dex-config.yaml`. |
| `INQTRIX_OIDC_CLIENT_SECRET` | the value selected above | Same variable feeds the Dex container and host-side API; keep the value in the private companion file and export it only into the API process. |
| `INQTRIX_OIDC_REDIRECT_URL` | `http://127.0.0.1:5100/api/auth/callback` | Must match a registered redirect URI byte-for-byte (`localhost` is a different string). |
| `INQTRIX_SESSION_SECRET` | any random string | CSRF-token derivation; required in oidc mode. |
| `INQTRIX_PAT_PEPPER` | any random string | HMAC pepper for personal access tokens; required in oidc mode. |
| `INQTRIX_OIDC_INSECURE_DEV_COOKIES` | `true` | Drops the `Secure` flag and `__Host-` prefix so login works over plain `http://127.0.0.1` in every browser. NEVER in production; activation is loudly logged. |

Install the project once with either `uv sync --extra dev` or a standard
environment created with `python -m venv .venv` followed by
`python -m pip install -e ".[dev]"`. The following block uses plain Python;
uv users can replace its final command with
`uv run python examples/webserver_stacks/anthropic_perplexity.py`.

```bash
INQTRIX_AUTH_MODE=oidc \
INQTRIX_OIDC_ISSUER=http://dex.localhost:5556/dex \
INQTRIX_OIDC_CLIENT_ID=inqtrix-local \
INQTRIX_OIDC_CLIENT_SECRET=replace-with-the-same-local-dev-secret \
INQTRIX_OIDC_REDIRECT_URL=http://127.0.0.1:5100/api/auth/callback \
INQTRIX_SESSION_SECRET=dev-session-secret-change-me \
INQTRIX_PAT_PEPPER=dev-pat-pepper-change-me \
INQTRIX_OIDC_INSECURE_DEV_COOKIES=true \
python examples/webserver_stacks/anthropic_perplexity.py
```

Any other stack script accepts the same variables. The env-only server starts
with `uv run python -m inqtrix` or, after the pip installation,
`python -m inqtrix`. The startup log line records `auth_mode=oidc`.

## 3. The browser login flow

Open `http://127.0.0.1:5100/api/auth/login` (the SPA triggers the same
URL from its lock screen). The sequence:

1. `GET /api/auth/login` — server stores a login transaction (PKCE
   S256 verifier, `state`, `nonce`), sets a short-lived flow cookie,
   and 302-redirects to the Dex authorize endpoint.
2. Dex shows the login form — `admin@example.com` / `password`
   (consent screen is skipped in the dev config).
3. `GET /api/auth/callback?code=...&state=...` — the server compares
   the flow cookie against `state` (login-CSRF defense), exchanges the
   code server-side, validates the id_token, creates the server-side
   session, and sets two cookies: the HttpOnly session cookie
   (`inqtrix_session`; `__Host-inqtrix_session` with secure cookies)
   and the JS-readable CSRF cookie (`inqtrix_csrf`).
4. 303-redirect to `/` (or the `next` path passed to `/login`).

## 4. Verify over curl

`GET /api/auth/session` is the SPA bootstrap and the quickest probe.
Anonymous (no cookie):

```bash
curl http://127.0.0.1:5100/api/auth/session
# {"authenticated": false}
```

Authenticated (reuse the browser's session cookie value):

```bash
curl http://127.0.0.1:5100/api/auth/session \
  -H 'Cookie: inqtrix_session=<value from the browser devtools>'
# {"authenticated": true, "sub": "...", "email": "admin@example.com",
#  "display_name": "admin", "csrf_token": "..."}
```

State-changing routes require the CSRF token from that payload in the
`X-CSRF-Token` header (OWASP signed double-submit); without it any
POST under an oidc session returns 403:

```bash
curl -X POST http://127.0.0.1:5100/api/auth/logout \
  -H 'Cookie: inqtrix_session=<value>' \
  -H 'X-CSRF-Token: <csrf_token from /api/auth/session>'
# {"logged_out": true}
```

## Frontend dev server (Vite proxy)

The research-desk dev server (`apps/research-desk`, port 5173) proxies
`/health`, `/v1`, and `/api` to the backend
(`VITE_INQTRIX_API_BASE_URL`, default `http://localhost:5100` — see
`apps/research-desk/vite.config.ts`). The OIDC callback therefore
lands same-origin with the SPA, which is why `dex-config.yaml`
registers the second redirect URI
`http://127.0.0.1:5173/api/auth/callback`. When logging in through the
Vite server, set
`INQTRIX_OIDC_REDIRECT_URL=http://127.0.0.1:5173/api/auth/callback`
and open `http://127.0.0.1:5173` — cookies are host-scoped, so the
port difference does not matter.

## Related docs

- [Local infrastructure](../../docs/development/local-infrastructure.md)
- [Web server mode](../../docs/deployment/webserver-mode.md)
- [`webserver_stacks/README.md`](README.md) — env-var matrix, logging, TLS
