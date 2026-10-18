"""Bedrock fixture loader and ClientError builders for replay tests.

Centralises the response-shape data so the replay tests stay focused
on assertions instead of inline boto3 dict construction. Fixtures live
as JSON files under ``tests/fixtures/bedrock/`` so they are discovered
by the protective sanitization scan automatically.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from botocore.exceptions import ClientError

_FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "bedrock"


def load_bedrock_response(scenario: str) -> dict[str, Any]:
    """Load ``tests/fixtures/bedrock/<scenario>.json`` as a Converse response dict.

    Args:
        scenario: File stem under ``tests/fixtures/bedrock/`` without
            the ``.json`` suffix (for example ``"success"`` or
            ``"success_thinking"``). The loader raises
            :class:`FileNotFoundError` with the resolved path when the
            file is missing, so a typo in the test surfaces immediately
            instead of as a silent KeyError downstream.

    Returns:
        The parsed JSON payload, ready to be passed to
        ``Stubber.add_response("converse", ...)``.
    """
    path = _FIXTURE_ROOT / f"{scenario}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Bedrock fixture {scenario!r} not found at {path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def make_throttling_error(
    *,
    message: str = "Rate exceeded",
    request_id: str = "req-replay-throttling-1",
) -> ClientError:
    """Return a realistic ``ThrottlingException`` ClientError.

    Mirrors the production retry-loop trigger: the provider catches
    ``ThrottlingException`` and retries with jittered backoff up to
    ``MAX_PROVIDER_ATTEMPTS=3`` times.
    """
    return ClientError(
        error_response={
            "Error": {"Code": "ThrottlingException", "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": 429,
                "RequestId": request_id,
            },
        },
        operation_name="Converse",
    )


def make_validation_error(
    *,
    message: str = "Invalid request: model id format unrecognised",
    request_id: str = "req-replay-validation-1",
) -> ClientError:
    """Return a non-retryable ``ValidationException`` ClientError."""
    return ClientError(
        error_response={
            "Error": {"Code": "ValidationException", "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": 400,
                "RequestId": request_id,
            },
        },
        operation_name="Converse",
    )


def make_service_unavailable_error(
    *,
    message: str = "Service unavailable",
    request_id: str = "req-replay-svcunavail-1",
) -> ClientError:
    """Return a retryable ``ServiceUnavailableException`` ClientError."""
    return ClientError(
        error_response={
            "Error": {
                "Code": "ServiceUnavailableException",
                "Message": message,
            },
            "ResponseMetadata": {
                "HTTPStatusCode": 503,
                "RequestId": request_id,
            },
        },
        operation_name="Converse",
    )
