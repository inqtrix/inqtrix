"""Bind browser identity generations to authenticated API requests."""

from __future__ import annotations

from typing import Callable, cast

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.principal import Principal, resolve_live_principal
from inqtrix.services.request_parsing import error_response

EXPECTED_USER_ID_HEADER = "x-inqtrix-expected-user-id"


class PrincipalChangedError(Exception):
    """Signal that browser state belongs to a different live principal."""


def _require_expected_principal(
    request: Request,
    principal: Principal,
) -> Principal:
    """Reject a request when the SPA's identity generation is stale.

    The header is an additive consistency guard, not an authentication
    credential. Requests from API clients that omit it retain the existing
    contract; the cookie-driven SPA sends it after session bootstrap so a
    login change in another tab cannot apply stale local state to the new
    user's account.
    """
    expected_user_id = request.headers.get(EXPECTED_USER_ID_HEADER)
    if expected_user_id is None:
        return principal
    if principal.user_id is None or str(principal.user_id) != expected_user_id:
        raise PrincipalChangedError
    return principal


def bind_principal_generation(
    dependency: Callable[..., Principal],
) -> Callable[..., Principal]:
    """Wrap one principal dependency with the optional browser generation.

    FastAPI resolves the original dependency only once per request. Long-lived
    streams receive a matching live resolver so revocation and identity-switch
    checks keep using the provider's canonical credential path.
    """

    injected_principal = Depends(dependency)

    async def expected_principal(
        request: Request,
        principal: Principal = cast(Principal, injected_principal),
    ) -> Principal:
        if principal is injected_principal:
            principal = await resolve_live_principal(dependency, request)
        return _require_expected_principal(request, principal)

    async def resolve_expected_live(request: Request) -> Principal:
        principal = await resolve_live_principal(dependency, request)
        return _require_expected_principal(request, principal)

    setattr(
        expected_principal,
        "__inqtrix_live_resolver__",
        resolve_expected_live,
    )
    return expected_principal


def install_principal_generation_error_handler(app: FastAPI) -> None:
    """Install the stable response used for stale browser generations."""

    @app.exception_handler(PrincipalChangedError)
    async def _principal_changed_handler(
        _request: Request,
        _exc: PrincipalChangedError,
    ) -> JSONResponse:
        return error_response(
            409,
            "The signed-in session has changed. The application will reload.",
            "principal_changed",
        )
