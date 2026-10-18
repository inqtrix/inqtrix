"""Capacity details never replace stronger protocol failure evidence."""

from __future__ import annotations

from email.utils import formatdate

import pytest
import httpx
from openai import APIStatusError

from inqtrix.execution_failures import classify_execution_failure
from inqtrix.exceptions import ModelCapacityReason
from inqtrix.exceptions import AzureOpenAIAPIError
from inqtrix.providers import base


class StatusError(RuntimeError):
    def __init__(self, status, *, code="", param="", message="Rejected."):
        self.status_code = status
        self.body = {"error": {"code": code, "param": param, "message": message}}
        super().__init__(message)


@pytest.mark.parametrize("error,expected", [
    (StatusError(400, code="context_length_exceeded", param="max_tokens"), ModelCapacityReason.CONTEXT_WINDOW),
    (StatusError(400, param="max_completion_tokens"), ModelCapacityReason.OUTPUT_LIMIT),
    (StatusError(400, message="This model's maximum context length is 8192 tokens."), ModelCapacityReason.CONTEXT_WINDOW),
    (StatusError(400, message="max_tokens exceeds the supported output limit"), ModelCapacityReason.OUTPUT_LIMIT),
    (StatusError(400, message="too many tokens"), ModelCapacityReason.UNKNOWN),
    (RuntimeError("maximum context length"), ModelCapacityReason.UNKNOWN),
    (RuntimeError("Unknown implementation failure"), None),
    (StatusError(401, code="context_length_exceeded"), None),
    (StatusError(403, param="max_tokens"), None),
    (StatusError(429, message="too many tokens"), None),
    (StatusError(503, message="maximum context length"), None),
])
def test_capacity_reason_requires_specific_evidence(error, expected):
    assert base.model_capacity_reason(error) is expected


def test_native_failure_typing_preserves_explicit_request_denial():
    for status, expected in [(403, "provider_access_denied"), (400, "provider_error"), (503, "upstream_5xx")]:
        raw = APIStatusError("Rejected.", response=httpx.Response(
            status, request=httpx.Request("POST", "https://provider.test")
        ), body={})
        assert classify_execution_failure(raw) == expected
        wrapped = AzureOpenAIAPIError(model="fixture", status_code=status, message="Rejected.", original=TimeoutError())
        assert classify_execution_failure(wrapped) == expected
    assert classify_execution_failure(StatusError(400)) == "server_error"


def test_retry_after_accepts_http_date_and_rejects_infinite_delays(monkeypatch):
    monkeypatch.setattr(base.time, "time", lambda: 2_000_000_000.0)
    monkeypatch.setattr(base.random, "uniform", lambda *_: 0.25)
    assert base._retry_delay_seconds(0, formatdate(2_000_000_030, usegmt=True)) == 30.25
    assert base._retry_delay_seconds(0, "3") == 3.25
    assert base._retry_delay_seconds(0, "Infinity") == 0.25


def test_retry_log_and_activity_share_the_operation_attempt_count(monkeypatch, caplog):
    monkeypatch.setattr(base, "_sleep_before_retry", lambda *_: None)
    responses = iter([StatusError(503), "completed"])
    notices = []

    def create():
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    with base.provider_attempt_scope(base.ProviderAttemptBudget(used=1)):
        assert base._call_openai_chat_completion_with_retries(
            provider_label="Fixture", model="fixture-model", operation="chat",
            deadline=None, create=create, append_retry_notice=notices.append,
        ) == "completed"
    assert notices[0]["attempt"] == 2
    assert notices[0]["max_attempts"] == 3
    retry_logs = [record.getMessage() for record in caplog.records
                  if "Retrying in" in record.getMessage()]
    assert len(retry_logs) == 1
    assert "attempt=2/3" in retry_logs[0]
