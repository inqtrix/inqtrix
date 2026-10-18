"""VCR-replay tests for the direct Anthropic provider (urllib transport).

These tests cover the two non-trivial code paths in
:class:`inqtrix.providers.anthropic.AnthropicLLM`: the structured
content/usage extraction from a Messages-API response, and the
exponential-backoff retry loop on transient 5xx/529 errors.
"""

from __future__ import annotations

import pathlib

import pytest

from inqtrix.exceptions import AgentRateLimited, AnthropicAPIError
from inqtrix.providers.anthropic import AnthropicLLM

pytestmark = pytest.mark.replay


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Pin Anthropic cassettes under ``tests/fixtures/cassettes/anthropic/``."""
    return str(
        pathlib.Path(__file__).resolve().parent.parent
        / "fixtures"
        / "cassettes"
        / "anthropic"
    )


def _build_provider() -> AnthropicLLM:
    """Construct an AnthropicLLM provider with hard-coded test defaults."""
    return AnthropicLLM(
        api_key="test-key",
        default_model="claude-sonnet-4-6",
        claim_extract_model="claude-haiku-4-5",
    )


@pytest.mark.vcr
def test_complete_success_replay() -> None:
    """A normal Messages-API response yields content + token counts."""
    provider = _build_provider()

    state: dict = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    response = provider.complete_with_metadata("Beschreibe Inqtrix kurz.", state=state)

    assert "Inqtrix" in response.content
    assert response.prompt_tokens == 11
    assert response.completion_tokens == 17
    assert response.finish_reason == "end_turn"
    assert state["total_prompt_tokens"] == 11
    assert state["total_completion_tokens"] == 17


@pytest.mark.vcr
def test_529_then_success_replay() -> None:
    """A transient 529 is retried; the second interaction succeeds."""
    provider = _build_provider()

    response = provider.complete_with_metadata("Triggert Overload-Retry.")

    assert "Antwort nach erfolgreichem Retry" in response.content
    assert response.prompt_tokens == 11
    assert response.completion_tokens == 7


@pytest.mark.vcr
def test_rate_limit_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXHAUSTED 429 budget escalates to ``AgentRateLimited``.

    A 429 is retried with backoff; the single-429 cassette covers the
    escalation path, so the rate-limit retry budget is set to 0 here to
    exhaust it on the first hit. anthropic.py imports the constant by
    value, so patch it on the anthropic module.
    """
    monkeypatch.setattr(
        "inqtrix.providers.anthropic._SDK_RATE_LIMIT_MAX_RETRIES", 0
    )
    provider = _build_provider()

    with pytest.raises(AgentRateLimited):
        provider.complete("Triggert Rate-Limit.")


@pytest.mark.vcr
def test_api_error_replay() -> None:
    """A non-retryable 400 surfaces as ``AnthropicAPIError`` with details."""
    provider = _build_provider()

    with pytest.raises(AnthropicAPIError) as excinfo:
        provider.complete("Triggert invalid-request-error.")
    err = excinfo.value
    assert err.status_code == 400
    assert err.error_type == "invalid_request_error"
    assert err.request_id == "req-replay-invalid-1"
