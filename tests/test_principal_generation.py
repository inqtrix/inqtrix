"""Browser principal generations reject stale cross-tab state."""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.principal import Principal
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
