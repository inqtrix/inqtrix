"""Global IntegrityError -> typed 4xx backstop (3.1).

The app-wide handler maps the two client-attributable SQLSTATEs
(unique-violation, foreign-key-violation) to 409/400 and RE-RAISES every
other IntegrityError so genuine faults still surface as a 500 with a
traceback (No Silent Fallbacks). Tested at two levels: the mapping
helper directly, and end-to-end through a FastAPI route that raises.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from inqtrix.server.app import db_integrity_response


class _FakeOrig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _integrity(sqlstate: str) -> IntegrityError:
    return IntegrityError("stmt", {}, _FakeOrig(sqlstate))


def _request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/v1/x", "headers": []}
    )


def test_unique_violation_maps_to_409_conflict() -> None:
    response = db_integrity_response(_request(), _integrity("23505"))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["error"]["type"] == "conflict"


def test_foreign_key_violation_maps_to_400() -> None:
    response = db_integrity_response(_request(), _integrity("23503"))
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"]["type"] == "invalid_request_error"


def test_other_sqlstate_is_reraised_not_masked() -> None:
    # A check-constraint (23514) or a null-violation (23502) is a real
    # fault and must NOT be swallowed into a polite 409.
    for sqlstate in ("23514", "23502", None):
        exc = _integrity(sqlstate) if sqlstate else IntegrityError("s", {}, None)
        with pytest.raises(IntegrityError):
            db_integrity_response(_request(), exc)


def test_end_to_end_route_raising_unique_violation_returns_409() -> None:
    app = FastAPI()

    @app.exception_handler(IntegrityError)
    async def _handler(request: Request, exc: IntegrityError):
        return db_integrity_response(request, exc)

    @app.post("/boom")
    async def boom():
        raise _integrity("23505")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/boom")
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "conflict"
