"""Browser principal generations reject stale cross-tab state."""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.principal import (
    Principal,
    resolve_live_principal,
    resolve_request_principal,
)
from inqtrix.auth.principal_generation import (
    bind_principal_generation,
    install_principal_generation_error_handler,
)


def test_expected_user_header_rejects_a_changed_cookie_principal() -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    current = {"principal": Principal(user_id=user_a, kind="oidc_session")}
    resolutions = 0

    async def resolve_principal(_request: Request) -> Principal:
        nonlocal resolutions
        resolutions += 1
        return current["principal"]

    dependency = bind_principal_generation(resolve_principal)
    app = FastAPI()
    install_principal_generation_error_handler(app)

    @app.get("/v1/private")
    async def private(
        principal: Principal = Depends(dependency),
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id)}

    with TestClient(app) as client:
        assert client.get("/v1/private").status_code == 200
        matching = client.get(
            "/v1/private",
            headers={"X-Inqtrix-Expected-User-Id": str(user_a)},
        )
        assert matching.status_code == 200

        current["principal"] = Principal(
            user_id=user_b,
            kind="oidc_session",
        )
        stale = client.get(
            "/v1/private",
            headers={"X-Inqtrix-Expected-User-Id": str(user_a)},
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "principal_changed"
    assert resolutions == 3


def test_expected_user_header_does_not_create_identity_for_open_mode() -> None:
    async def resolve_principal(_request: Request) -> Principal:
        return Principal(user_id=None, kind="anonymous")

    dependency = bind_principal_generation(resolve_principal)
    app = FastAPI()
    install_principal_generation_error_handler(app)

    @app.get("/v1/private")
    async def private(
        _principal: Principal = Depends(dependency),
    ) -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get(
            "/v1/private",
            headers={"X-Inqtrix-Expected-User-Id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "principal_changed"


def test_bound_generation_preserves_direct_router_resolution() -> None:
    user_id = uuid.uuid4()
    resolutions = 0

    async def resolve_principal(_request: Request) -> Principal:
        nonlocal resolutions
        resolutions += 1
        return Principal(user_id=user_id, kind="oidc_session")

    dependency = bind_principal_generation(resolve_principal)
    app = FastAPI()

    @app.get("/v1/direct")
    async def direct(request: Request) -> dict[str, str]:
        principal = await dependency(request)
        return {"user_id": str(principal.user_id)}

    with TestClient(app) as client:
        response = client.get(
            "/v1/direct",
            headers={"X-Inqtrix-Expected-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user_id)}
    assert resolutions == 1


def _direct_and_stream(dependency) -> dict[str, str]:
    """Resolve once as a route that calls the guard and once as a stream."""
    bound = bind_principal_generation(dependency)
    app = FastAPI()

    @app.get("/v1/direct")
    async def direct(request: Request) -> dict[str, str]:
        called = await bound(request)
        streamed = await resolve_live_principal(bound, request)
        return {"direct": str(called.user_id), "stream": str(streamed.user_id)}

    with TestClient(app) as client:
        return client.get("/v1/direct").json()


def test_a_direct_call_checks_fully_and_a_stream_keeps_its_live_check() -> None:
    """A route that calls the guard holds a fresh request and gets the full
    check; only the re-check of a stream with frozen headers may check less."""
    full = uuid.uuid4()
    relaxed = uuid.uuid4()

    async def resolve_principal(_request: Request) -> Principal:
        return Principal(user_id=full, kind="oidc_session")

    async def resolve_live(_request: Request) -> Principal:
        return Principal(user_id=relaxed, kind="oidc_session")

    setattr(resolve_principal, "__inqtrix_live_resolver__", resolve_live)
    setattr(resolve_principal, "__inqtrix_request_resolver__", resolve_principal)

    assert _direct_and_stream(resolve_principal) == {
        "direct": str(full),
        "stream": str(relaxed),
    }


def test_without_a_request_resolver_a_direct_call_uses_the_live_resolver() -> None:
    """The API key dependency needs FastAPI to solve its gate; its live
    resolver runs the gate itself, so a direct call must take that one."""
    checked = uuid.uuid4()

    async def resolve_principal(_request: Request) -> Principal:
        return Principal(user_id=uuid.uuid4(), kind="static")

    async def resolve_live(_request: Request) -> Principal:
        return Principal(user_id=checked, kind="static")

    setattr(resolve_principal, "__inqtrix_live_resolver__", resolve_live)

    assert _direct_and_stream(resolve_principal) == {
        "direct": str(checked),
        "stream": str(checked),
    }


def test_the_bound_guard_names_its_full_check_for_callers_outside_injection() -> None:
    """Whoever resolves the bound guard with resolve_request_principal gets
    the provider's full check and the generation, not the stream's check."""
    full = uuid.uuid4()
    relaxed = uuid.uuid4()

    async def resolve_principal(_request: Request) -> Principal:
        return Principal(user_id=full, kind="oidc_session")

    async def resolve_live(_request: Request) -> Principal:
        return Principal(user_id=relaxed, kind="oidc_session")

    setattr(resolve_principal, "__inqtrix_live_resolver__", resolve_live)
    setattr(resolve_principal, "__inqtrix_request_resolver__", resolve_principal)
    bound = bind_principal_generation(resolve_principal)
    app = FastAPI()
    install_principal_generation_error_handler(app)

    @app.get("/v1/outside")
    async def outside(request: Request) -> dict[str, str]:
        principal = await resolve_request_principal(bound, request)
        return {"user_id": str(principal.user_id)}

    with TestClient(app) as client:
        current = client.get("/v1/outside", headers={"X-Inqtrix-Expected-User-Id": str(full)})
        stale = client.get("/v1/outside", headers={"X-Inqtrix-Expected-User-Id": str(relaxed)})

    assert current.json() == {"user_id": str(full)}
    assert stale.status_code == 409
