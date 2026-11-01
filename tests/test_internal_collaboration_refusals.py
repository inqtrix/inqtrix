"""What the private collaboration API says when a request is refused.

The sidecar logs the reason of a refusal it can read. So a reason is always a
fixed code of the router, never the text of an exception raised further down:
a one-word text passes the sidecar's reason rule and would reach its log.
"""

from __future__ import annotations

import binascii

import pytest

from tests.test_editor_collaboration import (
    DOCUMENT_ID,
    INTERNAL_HEADERS,
    _internal_client,
    _InternalService,
    _update_payload,
)

INPUT_WORD = "inputword"


@pytest.mark.parametrize(
    "error",
    [
        ValueError(INPUT_WORD),
        ValueError(f"invalid literal for int() with base 10: '{INPUT_WORD}'"),
        ValueError(""),
        TypeError(INPUT_WORD),
        binascii.Error(INPUT_WORD),
    ],
    ids=["value_word", "value_sentence", "value_empty", "type_word", "base64_word"],
)
def test_a_foreign_exception_is_refused_with_a_fixed_code(error: Exception) -> None:
    service = _InternalService()
    service.error = error
    client = _internal_client(service)

    with client:
        response = client.post(
            "/internal/collaboration/leases/introspect",
            headers=INTERNAL_HEADERS,
            json={
                "lease_token": "opaque-lease",
                "room": f"inqtrix-editor-v1:{DOCUMENT_ID}:g4",
                "instance_id": "node-a",
                "epoch": 7,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": "Invalid collaboration request.",
        "type": "invalid_request_error",
        "reason": "invalid_request",
    }
    assert INPUT_WORD not in response.text


def test_the_router_keeps_naming_its_own_refusals() -> None:
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["update_base64"] = "not!base64"

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "invalid_update_base64"
