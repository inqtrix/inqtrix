"""Shared instance-admin request guard (single authorization axis).

The instance-admin check — cookie session -> users-mirror ->
``instance_role == "admin"`` — is the platform-administration axis. It
gates user management (:mod:`inqtrix.server.routers.admin`), quota
administration (:mod:`inqtrix.server.routers.quota`), and admin workspace
management. Defining it once here keeps every administrative surface
byte-identical: SESSION-ONLY (a personal access token can never administer,
so a leaked token cannot promote itself), a disabled admin loses the
surface even within the brief session-purge race window, and denial hides
behind a 404 (the permission layer's not-403 convention).

It is deliberately distinct from :class:`inqtrix.auth.permissions.WorkspaceRole`:
workspace ownership is the *collaboration* axis (sharing, invitations within
one workspace) and never confers tenant-wide administrative power.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from fastapi import Request
from starlette.responses import JSONResponse

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser
    from inqtrix.auth.oidc import OidcAuthProvider
    from inqtrix.auth.principal import Principal
    from inqtrix.auth.sessions import AuthSession

log = logging.getLogger("inqtrix")

TENANT = "default"

AdminResolution = tuple[
    tuple["Principal", "AuthSession", "MirroredUser"], None
] | tuple[None, JSONResponse]


async def require_instance_admin(
    provider: "OidcAuthProvider",
    request: Request,
    principal_dependency: Callable[..., "Principal"] | None = None,
) -> AdminResolution:
    """Resolve a session principal and require instance admin.

    Args:
        provider: The active cookie-session auth provider (oidc/local;
            local subclasses the oidc provider). It must expose
            ``users``, ``sessions``, and ``build_principal_dependency()``.
        request: The incoming request to authenticate.
        principal_dependency: Bound application dependency. Production routers
            pass the principal-generation guard so stale browser tabs cannot
            cross the instance-admin boundary. The provider fallback preserves
            the direct helper contract for non-HTTP callers.

    Returns:
        ``((principal, session, mirror), None)`` for an authenticated
        instance admin, or ``(None, error_response)`` otherwise.
        Non-session principals (personal access tokens, anonymous),
        non-admins, and disabled admins all receive an indistinguishable
        404; an expired session yields 401.
    """
    users = getattr(provider, "users", None)
    sessions = getattr(provider, "sessions", None)
    principal_dep = (
        principal_dependency or provider.build_principal_dependency()
    )
    principal = await principal_dep(request)
    if principal.kind != "oidc_session" or not principal.session_id:
        return None, error_response(404, "Not found", "not_found")
    session = await sessions.get(principal.session_id) if sessions is not None else None
    if session is None:
        return None, error_response(401, "Session expired", "unauthorized")
    mirror = (
        await users.find_by_user_id(
            tenant_id=TENANT, user_id=session.user_id
        )
        if users is not None
        else None
    )
    if (
        mirror is None
        or mirror.instance_role != "admin"
        or mirror.disabled_at is not None
    ):
        # An authenticated session that is not an active instance admin is an
        # authorization denial: it is logged as one (log_authorization_denial);
        # the body stays 404 (not 403, no membership oracle). A disabled admin
        # loses the surface too: the disable cascade
        # purges sessions, but this closes the race where a request loaded its
        # session a hair before the purge landed. The pre-auth branches above
        # (no session / expired) are deliberately not logged: they are
        # high-volume authentication outcomes, not authorization denials.
        log_authorization_denial(
            log,
            action="instance_admin",
            principal_kind=principal.kind,
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            resource_type="admin_surface",
            resource_id=TENANT,
        )
        return None, error_response(404, "Not found", "not_found")
    return (principal, session, mirror), None
