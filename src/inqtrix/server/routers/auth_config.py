"""Public auth-config discovery: ``GET /api/auth/config``.

Unauthenticated and ALWAYS mounted (like ``/health``) so the SPA can pick
the correct login surface — an SSO button vs an email/password form vs an
API-key field — and learn the session contract BEFORE authenticating. The
BFF auth router (``/api/auth/login``, ``/callback`` ...) is mounted only in
the cookie-session modes, so this discovery route lives in its own
always-on router rather than there.

Distinct from ``/api/auth/session`` (post-login identity) and from the
IdP's own ``/.well-known/openid-configuration`` (machine discovery at the
issuer): this is the app-level, pre-login login-UI hint. Every field is
derived from an existing seam, so there is no new source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from inqtrix.auth.oidc import CSRF_HEADER
from inqtrix.auth.principal import COOKIE_SESSION_MODES

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer



def _login_methods(
    mode: str, provider_name: str | None
) -> list[dict[str, str]]:
    """The ordered login methods the SPA should render for *mode*.

    A list (not a single value) so a future mixed local+SSO deployment can
    render several buttons in a server-controlled order without a frontend
    change. ``identifier`` tells the credential form how to label its
    first field.
    """
    if mode == "apikey":
        return [{"kind": "apikey", "label": "API key"}]
    if mode == "oidc":
        return [{"kind": "sso", "label": provider_name or "SSO"}]
    if mode == "local":
        return [
            {
                "kind": "password",
                "label": "Email & password",
                "identifier": "email",
            }
        ]
    return []


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the auth-config discovery route against the container."""
    router = APIRouter()
    provider = container.auth_provider

    @router.get("/api/auth/config")
    async def auth_config() -> JSONResponse:
        """Pre-login auth capabilities; no-store (never cache login state)."""
        mode = provider.mode
        cookie = mode in COOKIE_SESSION_MODES
        provider_name = getattr(provider, "provider_name", "") or None

        # self_service (in-app signup form) and needs_owner (first-run owner
        # setup) are local-only; both are derived from the SAME credential
        # store the /api/setup/status route uses, so they cannot drift.
        self_service = False
        needs_owner = False
        if mode == "local":
            self_service = (
                getattr(provider, "registration", "closed") == "open"
            )
            credentials = getattr(provider, "credentials", None)
            if credentials is not None:
                needs_owner = (
                    await credentials.count(tenant_id="default")
                ) == 0

        payload = {
            "auth_mode": mode,
            "auth_required": mode != "none",
            "login_methods": _login_methods(mode, provider_name),
            "provider_name": provider_name,
            "registration": {
                "self_service": self_service,
                "needs_owner": needs_owner,
            },
            "pat_available": getattr(provider, "pat_service", None)
            is not None,
            "supports_logout": cookie,
            "csrf_required": cookie,
            "csrf_header": CSRF_HEADER,
        }
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    return router
