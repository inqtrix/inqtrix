"""Native local email/password auth provider (``INQTRIX_AUTH_MODE=local``).

Subclasses :class:`~inqtrix.auth.oidc.OidcAuthProvider` to reuse the BFF's
session/CSRF/PAT/user-mirror machinery verbatim — only the login transport
differs (a server-side email/password check instead of an IdP round-trip).
The per-request principal path (Bearer -> PAT, cookie -> session, CSRF) is
inherited unchanged; local sessions are minted by the login route in
:mod:`inqtrix.server.routers.auth` exactly like the OIDC callback mints
them, under the synthetic issuer ``"local"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from inqtrix.auth.oidc import OidcAuthProvider

if TYPE_CHECKING:
    from inqtrix.auth.credentials import CredentialStore, LocalAuthenticator
    from inqtrix.auth.invitations import InvitationRepository, RegistrationGate
    from inqtrix.auth.pat import PatService, PatVerifier
    from inqtrix.auth.principal import AuthMode
    from inqtrix.auth.ratelimit import LoginRateLimiter
    from inqtrix.auth.sessions import FlowStore, SessionStore
    from inqtrix.auth.directory import UserDirectory
    from inqtrix.auth.lifecycle import UserLifecycleService


class LocalAuthProvider(OidcAuthProvider):
    """Email/password provider on the shared session-cookie machinery.

    Args:
        authenticator: Verifies email/password against the credential
            store (uniform, fail-closed, dummy-hash timing).
        credentials: The credential store (owner setup, admin actions).
        registration: ``"closed"`` (owner + admin-created/invited only)
            or ``"open"`` (public self-signup route mounted).

    The remaining arguments are the same session-machinery collaborators
    :class:`OidcAuthProvider` takes; ``client`` is omitted (local mode
    never talks to an IdP) and ``flows`` is supplied only to satisfy the
    base constructor (the local login path uses no OAuth flow store).
    """

    def __init__(
        self,
        *,
        authenticator: "LocalAuthenticator",
        credentials: "CredentialStore",
        registration: Literal["closed", "open"] = "closed",
        sessions: "SessionStore",
        flows: "FlowStore",
        users: "UserDirectory | None" = None,
        session_secret: str,
        session_max_age_seconds: int,
        secure_cookies: bool = True,
        pats: "PatVerifier | None" = None,
        pat_service: "PatService | None" = None,
        registration_gate: "RegistrationGate | None" = None,
        invitations: "InvitationRepository | None" = None,
        login_rate_limiter: "LoginRateLimiter | None" = None,
        trusted_proxy_hops: int = 1,
        lifecycle: "UserLifecycleService | None" = None,
    ) -> None:
        super().__init__(
            client=None,
            sessions=sessions,
            flows=flows,
            users=users,
            session_secret=session_secret,
            session_max_age_seconds=session_max_age_seconds,
            secure_cookies=secure_cookies,
            pats=pats,
            pat_service=pat_service,
            registration_gate=registration_gate,
            invitations=invitations,
            lifecycle=lifecycle,
        )
        self.authenticator = authenticator
        self.credentials = credentials
        self.registration = registration
        self.login_rate_limiter = login_rate_limiter
        self.trusted_proxy_hops = trusted_proxy_hops

    @property
    def mode(self) -> "AuthMode":
        """``"local"``."""
        return "local"
