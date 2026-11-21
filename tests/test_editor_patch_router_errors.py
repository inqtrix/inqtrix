"""Failure answers of the patch decision routes.

``:apply`` and ``:reject`` answer the failures they name themselves first,
unchanged. A collaboration failure they do not name answers through the
shared collaboration mapping, never with an unhandled 500. A bare
``ValueError`` is no such failure: there it is an inner error, such as a
collaboration-service reply that failed its shape check, and stays a 500
without its inner text.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.editor_collaboration_ports import CollaborationConflict
from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
    PatchRevisionConflict,
)
from inqtrix.project.editor_ports import DocumentContentModeConflict, DocumentNotFound
from inqtrix.server.routers import editor_patches
from inqtrix.server.routers._collaboration_errors import collaboration_error_view
from inqtrix.services.collaboration_client import (
    CollaborationNodeConflict,
    CollaborationServiceUnavailable,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
)
from inqtrix.services.editor_patch_service import EditorPatchValidationError
from inqtrix.services.editor_persistence_service import (
    CollaborationProjectionUnavailable,
)

_USER_ID = uuid.UUID("99999999-9999-4999-8999-999999999999")
_PRINCIPAL = Principal(
    user_id=_USER_ID,
    kind="oidc_session",
    tenant_id="default",
    role="member",
    session_id="session-router",
)
_INNER_TEXT = "suggestion_ids must be an array"


class _FailingPatches:
    """Patch service double whose decisions raise one prepared failure."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def apply(self, patch_id: str, **_values: Any) -> EditorPatchRecord:
        raise self.failure

    async def reject(self, patch_id: str, **_values: Any) -> EditorPatchRecord:
        raise self.failure


def _client(failure: Exception) -> TestClient:
    container = SimpleNamespace(
        editor_patch_service=_FailingPatches(failure),
        principal_dependency=lambda: _PRINCIPAL,
        user_context_dependency=lambda: UserContext(principal=_PRINCIPAL),
    )
    app = FastAPI()
    app.include_router(editor_patches.build_router(container))  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


_DECISIONS = {
    "apply": {
        "expected_sequence": 19,
        "decision_id": "33333333-3333-4333-8333-333333333333",
    },
    "reject": {"note": "Not suitable."},
}


def _decide(client: TestClient, route: str) -> Any:
    return client.post(
        f"/v1/editor/patches/pch_router:{route}", json=_DECISIONS[route]
    )


@pytest.mark.parametrize("route", sorted(_DECISIONS))
@pytest.mark.parametrize(
    ("failure", "status", "error"),
    [
        pytest.param(
            DocumentNotFound("ed_router"),
            404,
            {"message": "Document not found", "type": "not_found"},
            id="document_not_found",
        ),
        pytest.param(
            CollaborationAuthenticationRequired("cookie session required"),
            403,
            {
                "message": "Live collaboration requires an active browser session.",
                "type": "forbidden",
                "reason": "cookie_session_required",
            },
            id="cookie_session_required",
        ),
        pytest.param(
            CollaborationDocumentTooLarge("document too large"),
            413,
            {
                "message": "The document is too large for live collaboration.",
                "type": "invalid_request_error",
                "reason": "document_too_large",
            },
            id="document_too_large",
        ),
        pytest.param(
            CollaborationProjectionUnavailable("ed_router"),
            503,
            {
                "message": "The current collaboration state could not be persisted.",
                "type": "service_unavailable",
                "reason": "collaboration_projection_unavailable",
            },
            id="projection_unavailable",
        ),
        pytest.param(
            DocumentContentModeConflict("ed_router"),
            409,
            {
                "message": "Document content is already being synchronized live.",
                "type": "conflict",
                "reason": "content_mode_changed",
            },
            id="content_mode_changed",
        ),
    ],
)
def test_a_collaboration_failure_the_route_does_not_name_answers_through_the_shared_mapping(
    route: str, failure: Exception, status: int, error: dict[str, Any]
) -> None:
    response = _decide(_client(failure), route)
    assert response.status_code == status, response.text
    assert response.json() == {"error": error}


@pytest.mark.parametrize("route", sorted(_DECISIONS))
@pytest.mark.parametrize(
    ("failure", "status", "error"),
    [
        pytest.param(
            CollaborationConflict("sequence_conflict", current_sequence=21),
            409,
            {
                "message": "The collaboration state has changed in the meantime.",
                "type": "conflict",
                "reason": "sequence_conflict",
            },
            id="store_conflict",
        ),
        pytest.param(
            CollaborationNodeConflict("suggestion_conflict"),
            409,
            {
                "message": "The collaboration state has changed in the meantime.",
                "type": "conflict",
                "reason": "suggestion_conflict",
            },
            id="node_conflict",
        ),
        pytest.param(
            CollaborationServiceUnavailable("collaboration service is not ready"),
            503,
            {
                "message": "Live collaboration is currently unavailable.",
                "type": "service_unavailable",
                "reason": "collaboration_unavailable",
            },
            id="service_unavailable",
        ),
        pytest.param(
            PatchNotFound("pch_router"),
            404,
            {"message": "Patch not found", "type": "not_found"},
            id="patch_not_found",
        ),
        pytest.param(
            EditorPatchValidationError("expected_sequence and decision_id are required"),
            400,
            {
                "message": "expected_sequence and decision_id are required",
                "type": "invalid_request_error",
            },
            id="validation",
        ),
    ],
)
def test_the_answers_the_route_names_itself_stay_as_they_are(
    route: str, failure: Exception, status: int, error: dict[str, Any]
) -> None:
    response = _decide(_client(failure), route)
    assert response.status_code == status, response.text
    assert response.json() == {"error": error}


def test_the_apply_conflicts_the_route_names_itself_stay_as_they_are() -> None:
    revision = _decide(
        _client(PatchRevisionConflict(current_revision=4, revision_before=3)),
        "apply",
    )
    assert revision.status_code == 409
    assert revision.json() == {
        "error": {
            "message": "The document has changed in the meantime.",
            "type": "conflict",
            "current_revision": 4,
            "revision_before": 3,
        }
    }
    decided = SimpleNamespace(patch_id="pch_router", status="accepted")
    for route in sorted(_DECISIONS):
        response = _decide(_client(PatchAlreadyDecided(decided)), route)  # type: ignore[arg-type]
        assert response.status_code == 409
        assert response.json() == {
            "error": {
                "message": "The patch already has a different decision.",
                "type": "conflict",
                "status": "accepted",
            }
        }


@pytest.mark.parametrize("route", sorted(_DECISIONS))
def test_a_bare_value_error_stays_an_inner_error(route: str) -> None:
    response = _decide(_client(ValueError(_INNER_TEXT)), route)
    assert response.status_code == 500
    assert _INNER_TEXT not in response.text


def test_the_shared_mapping_names_both_new_failures_for_every_route() -> None:
    projection = collaboration_error_view(CollaborationProjectionUnavailable("ed_router"))
    assert projection is not None
    assert (projection.status, projection.error_type, projection.extra) == (
        503,
        "service_unavailable",
        {"reason": "collaboration_projection_unavailable"},
    )
    mode = collaboration_error_view(DocumentContentModeConflict("ed_router"))
    assert mode is not None
    assert (mode.status, mode.message, mode.error_type, mode.extra) == (
        409,
        "Document content is already being synchronized live.",
        "conflict",
        {"reason": "content_mode_changed"},
    )
    # The collaboration and guest routes keep their request-value answer.
    rejected = collaboration_error_view(ValueError("limit must be an integer"))
    assert rejected is not None
    assert (rejected.status, rejected.message) == (400, "limit must be an integer")
