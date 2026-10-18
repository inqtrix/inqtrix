"""Behavioral pins for the provider-level run-cancellation probe.

The probe (``provider_cancel_scope``) lets a cancelled run interrupt provider
retry ladders and backoff sleeps instead of serving them in full. These tests
pin three contracts: cancellation aborts before the next attempt, cancellation
interrupts an in-progress backoff sleep promptly, and code paths WITHOUT a
bound probe behave exactly as before (library-mode backwards compatibility).
"""

from __future__ import annotations

import time

import pytest

from inqtrix.exceptions import AgentCancelled
from inqtrix.providers import base as provider_base
from inqtrix.providers.base import (
    _call_openai_chat_completion_with_retries,
    _sleep_before_retry,
    provider_cancel_scope,
)


class _RetryableError(Exception):
    """Stub for an OpenAI-SDK transient failure (retryable 503)."""

    status_code = 503


def _call_with_retries(create, notices):
    return _call_openai_chat_completion_with_retries(
        provider_label="TestProvider",
        model="test-model",
        operation="test-operation",
        deadline=time.monotonic() + 30,
        create=create,
        append_retry_notice=notices.append,
    )


def test_cancel_after_first_failure_skips_backoff():
    """A cancel observed after a failed attempt aborts without the backoff."""
    calls: list[int] = []
    notices: list[dict] = []
    cancelled = {"flag": False}

    def create():
        calls.append(1)
        cancelled["flag"] = True
        raise _RetryableError("boom")

    started = time.monotonic()
    with provider_cancel_scope(lambda: cancelled["flag"]):
        with pytest.raises(AgentCancelled):
            _call_with_retries(create, notices)
    elapsed = time.monotonic() - started

    assert len(calls) == 1
    # The first retry backoff would be >= 0.5s (base 1.0s, jitter floor 0.5);
    # the probe must abort well before that.
    assert elapsed < 0.4


def test_cancel_before_first_attempt_never_calls_provider():
    calls: list[int] = []

    def create():
        calls.append(1)
        return "unreachable"

    with provider_cancel_scope(lambda: True):
        with pytest.raises(AgentCancelled):
            _call_with_retries(create, [])

    assert calls == []


def test_without_probe_retries_proceed_to_success(monkeypatch):
    """No bound probe keeps the historical retry-until-success behaviour."""
    monkeypatch.setattr(
        provider_base, "_retry_delay_seconds", lambda *a, **kw: 0.01
    )
    attempts = {"count": 0}
    notices: list[dict] = []

    def create():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _RetryableError("boom")
        return "ok"

    assert _call_with_retries(create, notices) == "ok"
    assert attempts["count"] == 3
    assert [notice["attempt"] for notice in notices] == [1, 2]


def test_sleep_without_probe_clamps_to_deadline():
    """The sliced sleep keeps the exact deadline-clamp contract."""
    started = time.monotonic()
    _sleep_before_retry(5.0, deadline=time.monotonic() + 0.2)
    elapsed = time.monotonic() - started

    assert 0.1 <= elapsed < 1.0


def test_cancel_interrupts_backoff_sleep_promptly():
    """A cancel arriving mid-backoff wakes the sleep at the next slice."""
    probes = {"count": 0}

    def probe() -> bool:
        probes["count"] += 1
        # First check (sleep entry) passes; every later slice reports cancel.
        return probes["count"] > 1

    started = time.monotonic()
    with provider_cancel_scope(probe):
        with pytest.raises(AgentCancelled):
            _sleep_before_retry(10.0, deadline=time.monotonic() + 30)
    elapsed = time.monotonic() - started

    # One 0.5s slice plus scheduling jitter, far below the 10s delay.
    assert elapsed < 2.0
