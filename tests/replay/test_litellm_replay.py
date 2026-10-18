"""VCR-replay tests for the LiteLLM provider.

These tests run offline against hand-crafted cassettes under
``tests/fixtures/cassettes/litellm/``. To re-record one against a real
proxy::

    INQTRIX_RECORD_MODE=once \\
      LITELLM_BASE_URL=http://localhost:4000/v1 \\
      LITELLM_API_KEY=... \\
      uv run pytest tests/replay/test_litellm_replay.py::test_complete_success_replay -v

The cassettes are sanitized via ``tests/fixtures/sanitize.py`` before
being committed; the protective scan in
``tests/replay/test_sanitization.py`` enforces that no committed
cassette ever contains real credentials.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from inqtrix.exceptions import AgentRateLimited, AgentTimeout
from inqtrix.providers.litellm import LiteLLM

pytestmark = pytest.mark.replay

_BASE_URL = "http://localhost:4000/v1"
_DEFAULT_MODEL = "gpt-4o"


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Pin LiteLLM cassettes under ``tests/fixtures/cassettes/litellm/``."""
    return str(
        pathlib.Path(__file__).resolve().parent.parent
        / "fixtures"
        / "cassettes"
        / "litellm"
    )


def _build_provider(*, max_retries: int | None = None) -> LiteLLM:
    """Construct a LiteLLM provider wired against the local-proxy URL.

    When *max_retries* is provided, it is set on the underlying OpenAI
    SDK client AFTER construction. The OpenAI SDK retries 408/409/429/
    5xx by default; for cassette-based tests we want exactly one
    interaction, so the rate-limit and api-error tests pass
    ``max_retries=0`` to disable the SDK's retry loop.
    """
    provider = LiteLLM(
        api_key="test-key",
        base_url=_BASE_URL,
        default_model=_DEFAULT_MODEL,
    )
    if max_retries is not None:
        provider._client = provider._client.with_options(max_retries=max_retries)
    return provider


@pytest.mark.vcr
def test_complete_success_replay() -> None:
    """A normal completion replays content + token counts from the cassette."""
    provider = _build_provider()

    state: dict = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    response = provider.complete_with_metadata("Was ist Inqtrix?", state=state)

    assert "Inqtrix" in response.content
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 22
    assert response.finish_reason == "stop"
    assert response.model == _DEFAULT_MODEL
    assert state["total_prompt_tokens"] == 12
    assert state["total_completion_tokens"] == 22


@pytest.mark.vcr
def test_rate_limit_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXHAUSTED 429 budget escalates to ``AgentRateLimited``.

    A 429 is retried with backoff; the single-429 cassette covers the
    escalation path, so the rate-limit retry budget is set to 0 here to
    exhaust it on the first hit. The retry-then-succeed
    behaviour is covered deterministically in ``tests/test_providers.py``.
    """
    monkeypatch.setattr(
        "inqtrix.providers.base._SDK_RATE_LIMIT_MAX_RETRIES", 0
    )
    provider = _build_provider(max_retries=0)

    with pytest.raises(AgentRateLimited):
        provider.complete("Was loest den Rate-Limit aus?")


@pytest.mark.vcr
def test_api_error_replay() -> None:
    """A non-429 5xx cassette propagates as the SDK's error type."""
    provider = _build_provider(max_retries=0)

    from openai import OpenAIError

    with pytest.raises(OpenAIError):
        provider.complete("Was passiert bei einem 500er?")

def test_deadline_timeout_short_circuits_without_cassette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-expired deadline raises ``AgentTimeout`` before any HTTP call.

    No cassette is required — :func:`_check_deadline` runs before the
    SDK is invoked. This guards the contract that providers honour the
    global agent budget even when the network would have been ready.
    """
    provider = _build_provider()
    expired = time.monotonic() - 1.0

    with pytest.raises(AgentTimeout):
        provider.complete("Wird nicht ausgefuehrt", deadline=expired)
