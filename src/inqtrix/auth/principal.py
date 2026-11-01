"""Principal model, AuthProvider seam, and the AUTH_MODE resolution rule.

Design decisions:

* A request ALWAYS resolves to a :class:`Principal` — the legacy
  open-server deployment yields :data:`ANONYMOUS_PRINCIPAL`, the
  static-key deployment yields :data:`STATIC_PRINCIPAL`. Downstream
  code never holds ``Principal | None``.
* ``Principal`` carries single-valued identity facts only. Plural,
  membership-style facts (workspace ids, group ids) belong to
  :class:`UserContext`, which the permission layer resolves
  server-side — never from client-supplied headers.
* The active mode is decided once at startup by
  :func:`resolve_auth_mode` with an explicit-wins / infer-for-
  backwards-compat rule, and the decision is logged. Misconfiguration
  raises at startup instead of silently downgrading.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from fastapi import Request

if TYPE_CHECKING:
    from inqtrix.settings import AuthSettings, ServerSettings

log = logging.getLogger(__name__)

AuthMode = Literal["none", "apikey", "oidc", "local", "gateway"]

COOKIE_SESSION_MODES: frozenset[str] = frozenset({"oidc", "local", "gateway"})
"""Modes whose identity rides a server-side cookie session.

Sharing, collaboration, quota and admin all gate on this set. It is
defined once here because a mode dropping out of one copy and not the
others disables those surfaces silently, with no error anywhere.
"""

# ``oidc_session`` is the cookie-session kind for every session-cookie
# provider: OIDC, local email/password and the gateway. The kind names the
# transport, not the identity provider -- local carries a synthetic
# ``"local"`` issuer, keeping every scoped surface working without a new kind.
PrincipalKind = Literal["anonymous", "static", "oidc_session", "pat"]


@dataclass(frozen=True)
class Principal:
    """Resolved identity of one HTTP request.

    Attributes:
        user_id: Canonical local ``users.id`` UUID for a scoped user. It is
            ``None`` for anonymous and static-key requests, which therefore
            cannot participate in user-scoped sharing or persistence.
        kind: How the principal authenticated. Drives audit labelling;
            never used for silent behavioural branches.
        tenant_id: Tenant the principal belongs to. A deployment runs
            one tenant, so this is the constant ``"default"``; the field
            exists on every principal so tenant scoping reaches every
            table and query without a retrofit.
        role: Coarse role used by the permission layer. The anonymous
            and static principals get ``"owner"`` so single-tenant
            deployments keep their unrestricted behaviour bit-for-bit
            under the permission checks.
        scopes: Optional capability scopes (personal access tokens
            carry these). Empty means unscoped/full access for the
            principal's role.
        display_name: Optional human-readable name for UI/audit
            surfaces.
        email: Optional e-mail claim from the IdP; identity anchoring
            always uses ``user_id``, never e-mail or an IdP subject.
        session_id: Server-side session row id for ``oidc_session``
            principals, enabling targeted revocation.
        pat_id: Token row id for ``pat`` principals, enabling targeted
            revocation and last-used bookkeeping.
    """

    user_id: uuid.UUID | None
    kind: PrincipalKind
    tenant_id: str = "default"
    role: str = "owner"
    scopes: frozenset[str] = frozenset()
    display_name: str | None = None
    email: str | None = None
    session_id: str | None = None
    pat_id: str | None = None


@dataclass(frozen=True)
class UserContext:
    """Principal plus server-resolved membership facts.

    The permission layer consumes this richer object. Memberships are
    resolved from the database against the verified principal — the
    client-supplied workspace header is a UI namespace filter, never
    an authorization input.

    Attributes:
        principal: The verified request identity.
        workspace_ids: Workspaces the principal is a member of,
            resolved server-side.
    """

    principal: Principal
    workspace_ids: tuple[str, ...] = ()


ANONYMOUS_PRINCIPAL = Principal(user_id=None, kind="anonymous")
"""Singleton principal for the open-server (no-auth) deployment mode."""

STATIC_PRINCIPAL = Principal(user_id=None, kind="static")
"""Singleton principal for the legacy static-Bearer-key deployment mode."""


class AuthProvider(ABC):
    """Baukasten seam that turns an HTTP request into a principal.

    Mirrors the ``LLMProvider`` / ``SearchProvider`` pattern: concrete
    providers receive every credential or expected secret via their
    constructor (never from ``os.environ``); only the settings bridge
    (:func:`inqtrix.auth.api_key.build_auth_provider`) translates env
    configuration into constructor arguments.
    """

    @property
    @abstractmethod
    def mode(self) -> AuthMode:
        """The auth mode this provider implements (operator-facing)."""

    @abstractmethod
    def resolve_principal(self, request: Request) -> Principal:
        """Resolve the request to a principal or reject it.

        Args:
            request: The incoming FastAPI/Starlette request.

        Returns:
            The verified principal for this request.

        Raises:
            fastapi.HTTPException: With status 401 when credentials
                are missing, malformed, or invalid. The envelope shape
                is provider-specific but must keep the historical
                ``{"error": {"message": ..., "type": "unauthorized"}}``
                contract for the static-key mode.
        """

    def build_principal_dependency(self) -> Callable[..., Principal]:
        """Build the FastAPI dependency yielding the request principal.

        The default wraps :meth:`resolve_principal` synchronously,
        which is correct for providers that inspect the request
        themselves. Providers that adapt caller-supplied FastAPI
        dependencies override this to route through real FastAPI
        dependency injection (so async dependencies and
        parameter-based signatures keep their historical semantics).
        """
        return make_principal_dependency(self)

    async def aclose(self) -> None:
        """Release what the provider holds open; nothing by default.

        The app lifespan calls this on shutdown. A lifespan may start
        again, so whatever this closes must reopen on its next use.
        """


class NoneAuthProvider(AuthProvider):
    """Open-server provider: every request is the anonymous principal.

    Preserves the historical no-``INQTRIX_SERVER_API_KEY`` behaviour:
    no credential check, no 401, every route reachable.
    """

    @property
    def mode(self) -> AuthMode:
        """Always ``"none"``."""
        return "none"

    def resolve_principal(self, request: Request) -> Principal:
        """Return the anonymous principal without inspecting the request."""
        return ANONYMOUS_PRINCIPAL


def make_principal_dependency(
    provider: AuthProvider,
) -> Callable[[Request], Principal]:
    """Build the FastAPI dependency that yields the request principal.

    Args:
        provider: The active auth provider chosen at startup.

    Returns:
        A dependency function suitable for ``Depends(...)`` on every
        gated route. Open routes (``/health``, ``/v1/models``,
        ``/v1/stacks``) deliberately do not take it.
    """

    def get_principal(request: Request) -> Principal:
        principal = provider.resolve_principal(request)
        # Stamp the subject onto the log context (stable pseudonym, never
        # the raw id) so every log line of this request carries it. The
        # request task owns the binding — no reset needed.
        from inqtrix.observability.context import bind_principal_context

        bind_principal_context(principal)
        return principal

    return get_principal


async def resolve_live_principal(
    dependency: Callable[..., object],
    request: Request,
) -> Principal:
    """Re-run the active credential resolver for a long-lived request.

    SSE handlers call this immediately before data frames so session/PAT
    revocation and user disablement take effect without reconnecting. The
    helper accepts both synchronous and asynchronous provider dependencies.

    Args:
        dependency: Principal dependency built by the active auth provider.
        request: Original request carrying the cookie or bearer credential.

    Returns:
        The currently valid principal.

    Raises:
        TypeError: The configured dependency returned an invalid object.
        fastapi.HTTPException: The credential is no longer valid.
    """
    live_resolver = getattr(dependency, "__inqtrix_live_resolver__", dependency)
    return await _resolved_principal(live_resolver, request)


async def resolve_request_principal(
    dependency: Callable[..., object],
    request: Request,
) -> Principal:
    """Run the full credential check for a request outside dependency injection.

    A route that calls the principal dependency itself holds a fresh request,
    so it needs the same check as the injected dependency. A provider whose
    live resolver checks less for streams (the gateway reads no assertion
    from frozen headers) names its full check ``__inqtrix_request_resolver__``;
    otherwise the live resolver, or the dependency itself, is that check.

    Raises:
        TypeError: The configured dependency returned an invalid object.
        fastapi.HTTPException: The credential is not valid for this request.
    """
    resolver = getattr(
        dependency,
        "__inqtrix_request_resolver__",
        getattr(dependency, "__inqtrix_live_resolver__", dependency),
    )
    return await _resolved_principal(resolver, request)


async def _resolved_principal(
    resolver: Callable[..., object],
    request: Request,
) -> Principal:
    resolved = resolver(request)
    if inspect.isawaitable(resolved):
        resolved = await resolved
    if not isinstance(resolved, Principal):
        raise TypeError("principal dependency returned an invalid value")
    return resolved


def resolve_auth_mode(
    auth: "AuthSettings",
    server: "ServerSettings",
    *,
    storage_backend: str | None = None,
) -> AuthMode:
    """Decide the active auth mode with explicit-wins semantics.

    Resolution rule:

    1. An explicit ``INQTRIX_AUTH_MODE`` value wins. ``"oidc"``
       without its required connection settings (issuer, client id,
       client secret, session secret, PAT pepper) is a contradiction
       and rejected loudly; ``"local"`` likewise requires the
       session-cookie secrets; ``"gateway"`` requires the secrets of
       ``"oidc"``, the postgres storage backend and, without SCIM, at
       least one bound on personal access tokens; ``"apikey"`` without a configured
       ``INQTRIX_SERVER_API_KEY`` likewise; ``"none"`` with a
       configured key disables the gate deliberately and logs a
       WARNING so the override is visible.
    2. ``"infer"`` (the default) derives the mode for backwards
       compatibility: a non-empty ``api_key`` means ``"apikey"``, an
       empty one means ``"none"``. The sentinel is a first-class
       value, so settings objects survive ``model_dump`` /
       ``model_validate`` round-trips without changing behaviour.

    SCIM provisioning (``INQTRIX_SCIM_ENABLED``) is valid only in
    ``"gateway"`` mode, whichever way the mode was reached.

    Args:
        auth: The resolved :class:`~inqtrix.settings.AuthSettings`.
        server: The resolved :class:`~inqtrix.settings.ServerSettings`
            (only ``api_key`` is inspected).
        storage_backend: ``INQTRIX_STORAGE_BACKEND`` of the process. The
            gateway mode starts only with ``"postgres"``; ``None`` (not
            supplied) never satisfies it.

    Returns:
        The active mode: ``"none"``, ``"apikey"``, ``"oidc"``, ``"local"``
        or ``"gateway"``.

    Raises:
        RuntimeError: On the contradictory explicit configurations
            described above.
    """
    api_key_set = bool((server.api_key or "").strip())
    explicit = auth.mode
    if explicit != "infer":
        if explicit == "oidc":
            missing = [
                name
                for name, value in (
                    ("INQTRIX_OIDC_ISSUER", auth.oidc_issuer),
                    ("INQTRIX_OIDC_CLIENT_ID", auth.oidc_client_id),
                    (
                        "INQTRIX_OIDC_CLIENT_SECRET",
                        auth.oidc_client_secret,
                    ),
                    ("INQTRIX_SESSION_SECRET", auth.session_secret),
                    # The PAT pepper is mandatory alongside oidc even
                    # before any token exists: requiring it at first
                    # boot beats discovering at token-creation time
                    # that hashes were minted pepperless.
                    ("INQTRIX_PAT_PEPPER", auth.pat_pepper),
                    # The pseudonym pepper is mandatory in oidc mode:
                    # multi-user SSO deployments are exactly where
                    # cross-process subject correlation (logs <-> audit
                    # <-> traces) must not silently degrade to
                    # per-process references.
                    ("INQTRIX_PSEUDONYM_PEPPER", auth.pseudonym_pepper),
                )
                if not value.strip()
            ]
            if missing:
                raise RuntimeError(
                    "INQTRIX_AUTH_MODE=oidc requires configured values for "
                    + ", ".join(missing)
                    + "."
                )
        if explicit == "local":
            # Native email/password reuses the session-cookie + CSRF + PAT
            # machinery, so the same two secrets OIDC needs are mandatory at
            # first boot (fail-loud, never pepperless hashes / unsigned CSRF).
            missing = [
                name
                for name, value in (
                    ("INQTRIX_SESSION_SECRET", auth.session_secret),
                    ("INQTRIX_PAT_PEPPER", auth.pat_pepper),
                )
                if not value.strip()
            ]
            if missing:
                raise RuntimeError(
                    "INQTRIX_AUTH_MODE=local requires configured values for "
                    + ", ".join(missing)
                    + "."
                )
        if explicit == "apikey" and not api_key_set:
            raise RuntimeError(
                "INQTRIX_AUTH_MODE=apikey requires "
                "INQTRIX_SERVER_API_KEY to be set."
            )
        if explicit == "gateway":
            check_gateway_deployment(auth, storage_backend)
        if explicit == "none" and api_key_set:
            log.warning(
                "INQTRIX_AUTH_MODE=none disables the API key gate even though "
                "INQTRIX_SERVER_API_KEY is set."
            )
        mode: AuthMode = explicit
    else:
        mode = "apikey" if api_key_set else "none"
    if auth.scim_enabled and mode != "gateway":
        raise RuntimeError(
            "INQTRIX_SCIM_ENABLED=true requires INQTRIX_AUTH_MODE=gateway; "
            "SCIM provisions the users of a gateway sign-in only."
        )
    if auth.gateway_resolver.strip() and mode != "gateway":
        raise RuntimeError(
            "INQTRIX_GATEWAY_RESOLVER requires INQTRIX_AUTH_MODE=gateway; a "
            "resolver of its own signs in gateway users only."
        )
    return mode


_SCIM_TOKEN_MIN_LENGTH = 32


def check_gateway_deployment(
    auth: "AuthSettings", storage_backend: str | None
) -> None:
    """Start checks every process of a gateway deployment runs.

    The identity source itself (assertion kind, issuer, headers) is checked
    where it is built, in :func:`inqtrix.auth.gateway.build_gateway_provider`;
    a process without an HTTP provider never builds one. Every problem is
    named at once; secret values never appear in the message.
    """
    problems: list[str] = []
    missing = [
        name
        for name, value in (
            ("INQTRIX_SESSION_SECRET", auth.session_secret),
            ("INQTRIX_PAT_PEPPER", auth.pat_pepper),
            ("INQTRIX_PSEUDONYM_PEPPER", auth.pseudonym_pepper),
        )
        if not value.strip()
    ]
    if missing:
        problems.append("configured values for " + ", ".join(missing))
    if storage_backend != "postgres":
        # Tombstones, the inactivity bound and SCIM need durable state.
        problems.append("INQTRIX_STORAGE_BACKEND=postgres")
    if auth.scim_enabled:
        if len(auth.scim_token.strip()) < _SCIM_TOKEN_MIN_LENGTH:
            problems.append(
                f"an INQTRIX_SCIM_TOKEN of at least {_SCIM_TOKEN_MIN_LENGTH} "
                "characters"
            )
        if auth.registration == "invite":
            problems.append(
                "INQTRIX_REGISTRATION=open, because with SCIM enabled SCIM "
                "is the only way an account is created "
                "(INQTRIX_REGISTRATION=invite is set)"
            )
    elif auth.gateway_pat_max_ttl_days == 0 and auth.gateway_pat_inactive_days == 0:
        # Without SCIM nothing tells Inqtrix that a person left; a personal
        # access token would then stay valid forever.
        problems.append(
            "INQTRIX_GATEWAY_PAT_MAX_TTL_DAYS or "
            "INQTRIX_GATEWAY_PAT_INACTIVE_DAYS above 0 while SCIM is off"
        )
    if problems:
        raise RuntimeError(
            "INQTRIX_AUTH_MODE=gateway requires " + "; ".join(problems) + "."
        )
