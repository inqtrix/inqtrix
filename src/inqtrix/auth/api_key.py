"""Static-Bearer-key auth provider and the settings-to-provider bridge.

This module owns :func:`build_bearer_guard` — the single source of the
constant-time static-key check. Both the provider here and the legacy
``inqtrix.server.security.make_api_key_dependency`` path delegate to
it, so the 401 envelopes and the ``hmac.compare_digest`` discipline
cannot drift between the two (No Redundancy). The dependency direction
is server -> auth; the auth layer never imports the server package.

A request that passes the guard resolves to the ``__static__``
principal — the implicit single-tenant operator identity that keeps
existing deployments behaving bit-for-bit.
"""

from __future__ import annotations

import logging

import hmac
import inspect
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Callable

from fastapi import HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response

from inqtrix.auth.principal import (
    STATIC_PRINCIPAL,
    AuthMode,
    AuthProvider,
    NoneAuthProvider,
    Principal,
    resolve_auth_mode,
)

log = logging.getLogger("inqtrix")

if TYPE_CHECKING:
    from inqtrix.settings import Settings


def build_bearer_guard(expected: str) -> Callable[[Request], None]:
    """Build the constant-time Bearer guard for one expected API key.

    Args:
        expected: The non-empty plaintext API key the caller already
            validated as configured. Passing an empty string is a
            programming error (the gate-disabled decision belongs to
            the caller, not to this guard).

    Returns:
        A FastAPI-compatible dependency function that raises
        :class:`HTTPException` (401, ``WWW-Authenticate: Bearer``) on
        missing, malformed, or mismatching credentials.
    """
    expected_bytes = expected.encode("utf-8")

    def require_api_key(request: Request) -> None:
        header = request.headers.get("Authorization", "").strip()
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": {
                        "message": "Missing or malformed Authorization header",
                        "type": "unauthorized",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        candidate = header[len("Bearer ") :].strip().encode("utf-8")
        # Constant-time compare to deny timing side channels.
        if not hmac.compare_digest(candidate, expected_bytes):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": {
                        "message": "Invalid API key",
                        "type": "unauthorized",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_api_key


class ApiKeyAuthProvider(AuthProvider):
    """Bearer-gate provider resolving to the static operator principal.

    Args:
        api_key: The expected plaintext API key. Constructor-First:
            the value arrives as an argument (from the settings bridge
            or a test), never from the environment. An empty key is a
            programming error — the "gate disabled" decision belongs
            to :func:`build_auth_provider`, not here.

    Raises:
        ValueError: When *api_key* is empty after stripping.
    """

    def __init__(self, *, api_key: str) -> None:
        expected = (api_key or "").strip()
        if not expected:
            raise ValueError(
                "ApiKeyAuthProvider requires a non-empty api_key; use "
                "NoneAuthProvider for the open-server mode."
            )
        self._require_api_key = build_bearer_guard(expected)

    @property
    def mode(self) -> AuthMode:
        """Always ``"apikey"``."""
        return "apikey"

    def resolve_principal(self, request: "Request") -> Principal:
        """Enforce the Bearer gate, then return the static principal.

        Raises:
            fastapi.HTTPException: 401 with the historical
                ``unauthorized`` envelope and ``WWW-Authenticate:
                Bearer`` header on missing/malformed/wrong credentials.
        """
        self._require_api_key(request)
        return STATIC_PRINCIPAL


class CallableGateAuthProvider(AuthProvider):
    """Adapter for a caller-injected gate dependency (legacy seam).

    Callers of ``register_routes(..., api_key_dependency=...)`` rely on
    FastAPI dependency injection resolving the injected callable, as
    ``Depends(...)`` does: ``async def`` gates are awaited and
    parameter-based signatures (``Header``, sub-``Depends``) are filled
    in. This adapter preserves exactly that contract by
    overriding :meth:`build_principal_dependency` to route the gate
    through real FastAPI DI — a synchronous direct call would silently
    skip async gates (an auth gate degrading to open, the worst kind
    of silent fallback).

    Args:
        gate: The injected dependency callable. Anything it raises
            propagates unchanged.
    """

    def __init__(self, *, gate: Callable[..., object]) -> None:
        self._gate = gate

    @property
    def mode(self) -> AuthMode:
        """Reported as ``"apikey"`` — the legacy gate semantics."""
        return "apikey"

    def resolve_principal(self, request: "Request") -> Principal:
        """Directly run a plain synchronous ``(Request) -> None`` gate.

        Only valid for the simple callable shape; the served HTTP path
        uses :meth:`build_principal_dependency` instead, which supports
        every FastAPI dependency shape. Calling this with an ``async``
        gate is a programming error and rejected loudly rather than
        silently skipping the check.

        Raises:
            TypeError: When the gate is a coroutine function (it would
                never be awaited here).
            fastapi.HTTPException: Whatever the gate raises.
        """
        import inspect

        if inspect.iscoroutinefunction(self._gate):
            raise TypeError(
                "CallableGateAuthProvider.resolve_principal cannot run an "
                "async gate synchronously; use build_principal_dependency()."
            )
        self._gate(request)
        return STATIC_PRINCIPAL

    def build_principal_dependency(self) -> Callable[..., Principal]:
        """Route the legacy gate through real FastAPI dependency injection."""
        from fastapi import Depends

        gate = self._gate

        async def resolve_live(request: Request) -> Principal:
            """Re-evaluate the complete FastAPI gate graph for an SSE frame."""
            dependant = get_dependant(path=request.url.path, call=gate)
            missing = object()
            previous_request_stack = request.scope.get(
                "fastapi_inner_astack", missing
            )
            previous_function_stack = request.scope.get(
                "fastapi_function_astack", missing
            )
            async with (
                AsyncExitStack() as request_stack,
                AsyncExitStack() as function_stack,
            ):
                request.scope["fastapi_inner_astack"] = request_stack
                request.scope["fastapi_function_astack"] = function_stack
                try:
                    solved = await solve_dependencies(
                        request=request,
                        dependant=dependant,
                        body=None,
                        background_tasks=None,
                        response=Response(),
                        dependency_overrides_provider=request.app,
                        dependency_cache={},
                        async_exit_stack=request_stack,
                        embed_body_fields=False,
                    )
                    if solved.errors:
                        raise RequestValidationError(solved.errors)
                    is_async_gate = inspect.iscoroutinefunction(
                        gate
                    ) or inspect.iscoroutinefunction(
                        getattr(gate, "__call__", None)
                    )
                    result = (
                        gate(**solved.values)
                        if is_async_gate
                        else await run_in_threadpool(gate, **solved.values)
                    )
                    if inspect.isawaitable(result):
                        await result
                finally:
                    for key, previous in (
                        ("fastapi_inner_astack", previous_request_stack),
                        ("fastapi_function_astack", previous_function_stack),
                    ):
                        if previous is missing:
                            request.scope.pop(key, None)
                        else:
                            request.scope[key] = previous
            return STATIC_PRINCIPAL

        def get_principal(
            request: "Request",
            _gate_result: object = Depends(gate),
        ) -> Principal:
            return STATIC_PRINCIPAL

        setattr(get_principal, "__inqtrix_live_resolver__", resolve_live)
        return get_principal


def _csv_frozenset(value: str, *, lower: bool = False) -> frozenset[str]:
    """Parse a comma-separated env value into a trimmed frozenset.

    The single place comma-list auth settings (allowed/admin groups,
    admin roles, allowed domains) become sets, so the trimming and
    empty-dropping cannot drift. *lower* lower-cases each entry for
    case-insensitive matches (email domains).
    """
    items = (item.strip() for item in value.split(","))
    return frozenset(item.lower() if lower else item for item in items if item)


def build_auth_provider(settings: "Settings") -> AuthProvider:
    """Settings bridge: translate env configuration into a provider.

    This is the only env-coupled construction surface for auth; every
    provider itself is configured through its constructor. The mode decision delegates to
    :func:`~inqtrix.auth.principal.resolve_auth_mode`, which raises on
    contradictory explicit configuration.

    Args:
        settings: The resolved root :class:`~inqtrix.settings.Settings`.

    Returns:
        :class:`NoneAuthProvider` for the open-server mode,
        :class:`ApiKeyAuthProvider` for the static-key mode, or
        :class:`~inqtrix.auth.oidc.OidcAuthProvider` for the BFF.
    """
    mode = resolve_auth_mode(settings.auth, settings.server)
    if mode == "apikey":
        return ApiKeyAuthProvider(api_key=settings.server.api_key)
    if mode == "oidc":
        return build_oidc_provider(settings)
    if mode == "local":
        return build_local_provider(settings)
    return NoneAuthProvider()


def _build_session_backends(settings: "Settings"):
    """Build the session/flow/user/PAT/invitation stores per storage backend.

    Shared by every cookie-session provider (oidc/local) so the
    memory-vs-Postgres wiring lives in ONE place (No Redundancy). Returns
    the five stores plus the Postgres session factory (or ``None`` for the
    memory backend) so a caller can build extra stores — e.g. the local
    credential store — on the very same engine.
    """
    from inqtrix.auth.directory import MemoryUserDirectory
    from inqtrix.auth.pat import MemoryPatStore
    from inqtrix.auth.sessions import MemoryFlowStore, MemorySessionStore

    if settings.storage.backend == "postgres":
        if not settings.storage.database_url.strip():
            raise RuntimeError(
                "INQTRIX_STORAGE_BACKEND=postgres requires "
                "INQTRIX_DATABASE_URL to be set."
            )
        from inqtrix.storage.auth_postgres import (
            PostgresFlowStore,
            PostgresSessionStore,
            PostgresUserDirectory,
        )
        from inqtrix.storage.db import build_engine, build_session_factory
        from inqtrix.storage.invitations_postgres import (
            PostgresInvitationRepository,
        )
        from inqtrix.storage.pat_postgres import PostgresPatStore
        from inqtrix.storage.user_lifecycle_postgres import (
            PostgresUserLifecycleTransaction,
        )

        session_factory = build_session_factory(
            build_engine(
                settings.storage.database_url,
                **settings.storage.pool_kwargs(),
            )
        )
        app_role = settings.storage.app_role
        return (
            PostgresSessionStore(
                session_factory=session_factory, app_role=app_role
            ),
            PostgresFlowStore(
                session_factory=session_factory, app_role=app_role
            ),
            PostgresUserDirectory(
                session_factory=session_factory, app_role=app_role
            ),
            PostgresPatStore(
                session_factory=session_factory, app_role=app_role
            ),
            PostgresInvitationRepository(
                session_factory=session_factory, app_role=app_role
            ),
            session_factory,
            PostgresUserLifecycleTransaction(
                session_factory=session_factory, app_role=app_role
            ),
        )
    log.warning(
        "PAT store uses memory storage: access tokens do not survive a restart "
        "(set INQTRIX_STORAGE_BACKEND=postgres for persistence)."
    )
    return (
        MemorySessionStore(),
        MemoryFlowStore(),
        MemoryUserDirectory(),
        MemoryPatStore(),
        None,
        None,
        None,
    )


def _build_login_rate_limiter(settings: "Settings"):
    """A process-local login throttle for the password modes, or ``None``.

    Secure default (enabled); the settings bridge translates the env knobs to
    constructor args (Constructor-First). ``None`` when explicitly disabled.
    """
    auth = settings.auth
    if not auth.login_rate_limit_enabled:
        return None
    from inqtrix.auth.ratelimit import MemoryLoginRateLimiter

    return MemoryLoginRateLimiter(
        max_attempts=auth.login_rate_limit_max_attempts,
        window_seconds=auth.login_rate_limit_window_seconds,
        lockout_seconds=auth.login_rate_limit_lockout_seconds,
    )


def build_local_provider(settings: "Settings") -> AuthProvider:
    """Construct the native local email/password provider (env bridge).

    Reuses :func:`_build_session_backends` for the session machinery and
    adds the credential store on the same backend. The credential store is
    the source of truth for the disabled flag; the OIDC ``RegistrationGate``
    is wired with ``registration="open"`` (it is reused only for future
    invitation admission, never to invite-gate a password login).
    """
    from inqtrix.auth.credentials import (
        LocalAuthenticator,
        MemoryCredentialStore,
    )
    from inqtrix.auth.invitations import RegistrationGate
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.auth.local import LocalAuthProvider
    from inqtrix.auth.pat import PatService, PatVerifier

    auth = settings.auth
    (
        sessions,
        flows,
        users,
        pat_store,
        invitation_repo,
        session_factory,
        lifecycle_transaction,
    ) = (
        _build_session_backends(settings)
    )
    if session_factory is not None:
        from inqtrix.storage.credentials_postgres import (
            PostgresCredentialStore,
        )

        credentials = PostgresCredentialStore(
            session_factory=session_factory,
            app_role=settings.storage.app_role,
        )
    else:
        credentials = MemoryCredentialStore()
        log.warning(
            "Local credential store uses memory storage: accounts do not "
            "survive a restart (set INQTRIX_STORAGE_BACKEND=postgres for "
            "persistence)."
        )
        from inqtrix.auth.lifecycle import MemoryUserLifecycleTransaction

        lifecycle_transaction = MemoryUserLifecycleTransaction(
            users=users,
            sessions=sessions,
            invitations=invitation_repo,
            credentials=credentials,
            pat_store=pat_store,
        )
    authenticator = LocalAuthenticator(store=credentials)
    pat_verifier = PatVerifier(store=pat_store, pepper=auth.pat_pepper, user_lookup=users)
    pat_service = PatService(
        store=pat_store,
        pepper=auth.pat_pepper,
        max_per_user=auth.pat_max_per_user,
        default_ttl_days=auth.pat_default_ttl_days,
    )
    registration_gate = RegistrationGate(
        invitations=invitation_repo,
        users=users,
        registration="open",
    )
    lifecycle = UserLifecycleService(
        users=users,
        sessions=sessions,
        invitations=invitation_repo,
        credentials=credentials,
        pat_service=pat_service,
        transaction=lifecycle_transaction,
    )
    return LocalAuthProvider(
        authenticator=authenticator,
        credentials=credentials,
        registration=auth.local_registration,
        sessions=sessions,
        flows=flows,
        users=users,
        session_secret=auth.session_secret,
        session_max_age_seconds=auth.session_max_age_seconds,
        secure_cookies=not auth.oidc_insecure_dev_cookies,
        pats=pat_verifier,
        pat_service=pat_service,
        registration_gate=registration_gate,
        invitations=invitation_repo,
        login_rate_limiter=_build_login_rate_limiter(settings),
        trusted_proxy_hops=auth.trusted_proxy_hops,
        lifecycle=lifecycle,
    )


def build_oidc_provider(settings: "Settings") -> AuthProvider:
    """Construct the OIDC BFF provider from settings (env bridge).

    Session/flow/user stores follow the storage backend: memory for
    the zero-infrastructure default (single process), Postgres when
    ``INQTRIX_STORAGE_BACKEND=postgres`` so logins survive restarts
    and replica switches. The Postgres stores get their own session
    factory, used exclusively on the HTTP loop.
    """
    from inqtrix.auth.invitations import RegistrationGate
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.auth.oidc import OidcAuthProvider, OidcClient
    from inqtrix.auth.pat import PatService, PatVerifier

    auth = settings.auth
    if (
        auth.registration == "invite"
        and settings.storage.backend != "postgres"
    ):
        raise RuntimeError(
            "INQTRIX_REGISTRATION=invite requires "
            "INQTRIX_STORAGE_BACKEND=postgres; in-memory invitations "
            "would disappear on restart and lock everyone out."
        )
    redirect_url = auth.oidc_redirect_url.strip()
    if not redirect_url and settings.server.public_base_url.strip():
        base = settings.server.public_base_url.rstrip("/")
        redirect_url = f"{base}/api/auth/callback"
    if not redirect_url:
        raise RuntimeError(
            "INQTRIX_AUTH_MODE=oidc requires INQTRIX_OIDC_REDIRECT_URL "
            "or INQTRIX_PUBLIC_BASE_URL to be set."
        )
    client = OidcClient(
        issuer=auth.oidc_issuer,
        client_id=auth.oidc_client_id,
        client_secret=auth.oidc_client_secret,
        redirect_url=redirect_url,
        scopes=auth.oidc_scopes,
        discovery_url=auth.oidc_discovery_url,
        ca_cert=auth.oidc_ca_cert,
    )
    (
        sessions,
        flows,
        users,
        pat_store,
        invitation_repo,
        _session_factory,
        lifecycle_transaction,
    ) = (
        _build_session_backends(settings)
    )
    registration_gate = RegistrationGate(
        invitations=invitation_repo,
        users=users,
        registration=auth.registration,
    )
    pat_verifier = PatVerifier(store=pat_store, pepper=auth.pat_pepper, user_lookup=users)
    pat_service = PatService(
        store=pat_store,
        pepper=auth.pat_pepper,
        max_per_user=auth.pat_max_per_user,
        default_ttl_days=auth.pat_default_ttl_days,
    )
    if lifecycle_transaction is None:
        from inqtrix.auth.lifecycle import MemoryUserLifecycleTransaction

        lifecycle_transaction = MemoryUserLifecycleTransaction(
            users=users,
            sessions=sessions,
            invitations=invitation_repo,
            pat_store=pat_store,
        )
    lifecycle = UserLifecycleService(
        users=users,
        sessions=sessions,
        invitations=invitation_repo,
        pat_service=pat_service,
        transaction=lifecycle_transaction,
    )
    return OidcAuthProvider(
        client=client,
        sessions=sessions,
        flows=flows,
        users=users,
        session_secret=auth.session_secret,
        session_max_age_seconds=auth.session_max_age_seconds,
        username_claim=auth.oidc_username_claim,
        email_claim=auth.oidc_email_claim,
        groups_claim=auth.oidc_groups_claim,
        allowed_groups=_csv_frozenset(auth.oidc_allowed_groups),
        roles_claim=auth.oidc_roles_claim,
        admin_roles=_csv_frozenset(auth.oidc_admin_roles),
        admin_groups=_csv_frozenset(auth.oidc_admin_groups),
        claim_separators=auth.oidc_claim_separators,
        strip_group_path_prefix=auth.oidc_groups_strip_path_prefix,
        allowed_domains=_csv_frozenset(auth.oidc_allowed_domains, lower=True),
        provider_name=auth.oidc_provider_name,
        skip_email_verified=auth.oidc_skip_email_verified,
        userinfo_fallback=auth.oidc_userinfo_fallback,
        secure_cookies=not auth.oidc_insecure_dev_cookies,
        pats=pat_verifier,
        pat_service=pat_service,
        registration_gate=registration_gate,
        invitations=invitation_repo,
        lifecycle=lifecycle,
    )
