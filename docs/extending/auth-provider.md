# Writing a custom auth provider

## Scope

How to plug your own authentication into the server when the built-in modes
(`none` / `apikey` / `local` / `oidc`) do not fit — a bespoke SSO, a
gateway that injects a header, an mTLS front end, etc. Covers the `AuthProvider`
contract, the `Principal` it returns, and the `create_app(auth_provider=...)`
injection seam. For the built-in modes use configuration instead
([Auth modes](../deployment/auth-modes.md)); to swap a storage backend see
[Writing a custom storage backend](storage.md).

## The contract

Every request resolves to a `Principal` through an `AuthProvider`
([`src/inqtrix/auth/principal.py`](../../src/inqtrix/auth/principal.py)). The
ABC is small:

```python
from inqtrix.auth.principal import AuthMode, AuthProvider, Principal
from fastapi import Request


class HeaderAuthProvider(AuthProvider):
    """Trust a reverse-proxy gate for an unscoped deployment."""

    @property
    def mode(self) -> AuthMode:
        return "apikey"

    def resolve_principal(self, request: Request) -> Principal:
        subject = request.headers.get("X-Forwarded-User", "").strip()
        if not subject:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="missing user header")
        return Principal(
            user_id=None,
            kind="static",
            tenant_id="default",
        )
```

`resolve_principal` is the synchronous path. If your check is async (a network
call, a DB lookup), override `build_principal_dependency()` to return an async
FastAPI dependency instead — that is exactly what `OidcAuthProvider` does for
cookie-session lookups.

The `Principal` carries the canonical local `user_id`, `kind`
(`anonymous`/`static`/`oidc_session`/`pat`), `tenant_id`, and optional
`display_name`/`email`/`session_id`. The `kind` names the **transport**, not the
identity provider: scoped surfaces (workspaces, sharing, PAT
ownership) require `kind == "oidc_session"` or `"pat"` plus a non-null
`user_id`. External issuer/subject values stay inside the authentication
adapter and its directory binding.

`anonymous` and `static` principals always use `user_id=None`. They preserve
the unscoped single-user deployment modes and cannot participate in sharing.

## Reuse the session machinery (recommended for cookie logins)

If your provider is a browser login that should get sharing, quotas, the admin
surface, and CSRF "for free", **subclass `OidcAuthProvider`** and reuse its
session/CSRF/PAT/user-mirror machinery verbatim — this is precisely how the
built-in `LocalAuthProvider` is built (it passes
`client=None` and differs only in its login route and a synthetic issuer).
Read those for the pattern:

- [`src/inqtrix/auth/local.py`](../../src/inqtrix/auth/local.py)

For an unscoped stateless/header gate, implementing `AuthProvider` directly (as
above) is enough. A per-user header provider must instead reuse the session
machinery or provide the equivalent canonical-user lookup, active-user check,
CSRF protection, and lifecycle stores; merely returning a UUID is not a safe
substitute.

## Inject it

`create_app(auth_provider=...)` takes a pre-built provider and uses it verbatim,
bypassing the `INQTRIX_AUTH_MODE` env dispatch — you never edit
`build_auth_provider`:

```python
from inqtrix.server import create_app

app = create_app(auth_provider=HeaderAuthProvider())
```

Everything downstream (route mounts, the capability flags, the `apikey`-gate
banner) reads `auth_provider.mode`, so the rest of the server adapts with no
further wiring. Run it with any ASGI server pointed at this `app`, e.g.
`uvicorn yourmodule:app`.

## Checklist

- `mode` returns one of the existing `AuthMode` values. Avoid a new kind unless
  you also extend every scoped surface because that change has a large blast
  radius.
- Failures raise `HTTPException(401/403)` — never return a partial/anonymous
  principal silently (no silent fallbacks).
- For scoped authentication, resolve every external identity to one active
  canonical `users.id` UUID on every request. Never expose or authorize by the
  external subject.
- The provider reads its configuration from constructor arguments, not the
  environment (Constructor-First); an `examples/` script or your own composition
  root translates env to constructor args.
- If you mint sessions, reuse `kind="oidc_session"` so sharing/quota/admin work.

## Related docs

- [Auth modes](../deployment/auth-modes.md) — the built-in modes (prefer config).
- [Writing a custom storage backend](storage.md) — the matching seams for stores.
- [Create and manage users](../deployment/create-and-manage-users.md) — instance roles, disabling, PATs.
