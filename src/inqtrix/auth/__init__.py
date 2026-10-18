"""Identity layer: principal resolution behind a Baukasten provider seam.

Every HTTP request resolves to exactly one :class:`~inqtrix.auth.principal.Principal`
— including the legacy no-auth deployment (the anonymous principal) and
the static-API-key deployment (the ``__static__`` principal). Routers
and services therefore never branch on "is auth enabled"; they consume
one uniform principal object, with no deployment-specific branches.

Modules:

* :mod:`inqtrix.auth.principal` — ``Principal`` / ``UserContext``
  dataclasses, the ``AuthProvider`` ABC, the anonymous provider, and
  the ``AUTH_MODE`` resolution rule.
* :mod:`inqtrix.auth.api_key` — the static-Bearer-key provider that
  wraps the historical ``hmac.compare_digest`` gate byte-for-byte,
  plus the settings-to-provider bridge ``build_auth_provider``.
* :mod:`inqtrix.auth.permissions` — the authorization chokepoint:
  ordered roles/permissions, repository ports, and the
  ``AuthorizationService`` keeping workspace membership separate from
  owner-or-direct-share resource access (denials hidden as not-found,
  audited loudly).
* :mod:`inqtrix.auth.identity_memory` — the no-infrastructure
  identity backend implementing every permission-layer port.

OIDC (browser SSO via an external IdP) and personal access tokens
arrive as additional providers behind the same ABC.
"""

from inqtrix.auth.api_key import ApiKeyAuthProvider, build_auth_provider
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import (
    AuditEntry,
    AuthorizationService,
    ResourceNotFound,
    SharePermission,
    WorkspaceNotFound,
    WorkspaceRole,
)
from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    STATIC_PRINCIPAL,
    AuthProvider,
    NoneAuthProvider,
    Principal,
    UserContext,
    make_principal_dependency,
    resolve_auth_mode,
)

__all__ = [
    "ANONYMOUS_PRINCIPAL",
    "STATIC_PRINCIPAL",
    "ApiKeyAuthProvider",
    "AuditEntry",
    "AuthProvider",
    "MemoryIdentityStore",
    "NoneAuthProvider",
    "AuthorizationService",
    "Principal",
    "ResourceNotFound",
    "SharePermission",
    "UserContext",
    "WorkspaceNotFound",
    "WorkspaceRole",
    "build_auth_provider",
    "make_principal_dependency",
    "resolve_auth_mode",
]
