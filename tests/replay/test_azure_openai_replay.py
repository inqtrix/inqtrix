"""VCR-replay tests for the Azure OpenAI LLM provider.

Coverage: three cassettes (success with the resource's API key,
rate-limit, api-error) under ``tests/fixtures/cassettes/azure_openai/``.
"""

from __future__ import annotations

import pathlib

import pytest

from inqtrix.exceptions import AgentRateLimited, AzureOpenAIAPIError
from inqtrix.providers.azure import AzureOpenAILLM

pytestmark = pytest.mark.replay


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Pin Azure OpenAI cassettes under ``tests/fixtures/cassettes/azure_openai/``."""
    return str(
        pathlib.Path(__file__).resolve().parent.parent
        / "fixtures"
        / "cassettes"
        / "azure_openai"
    )


_ENDPOINT = "https://test-resource.openai.azure.com/"


def _build_with_api_key() -> AzureOpenAILLM:
    provider = AzureOpenAILLM(
        azure_endpoint=_ENDPOINT,
        api_key="test-key",
        default_model="my-gpt4o-deployment",
    )
    return provider


@pytest.mark.vcr
def test_complete_success_api_key_replay() -> None:
    """API-key auth + happy-path completion."""
    provider = _build_with_api_key()

    state: dict = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    response = provider.complete_with_metadata("Test", state=state)

    assert "Azure OpenAI" in response.content
    assert response.prompt_tokens == 9
    assert response.completion_tokens == 16
    assert state["total_prompt_tokens"] == 9


@pytest.mark.vcr
def test_rate_limit_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXHAUSTED 429 budget raises ``AgentRateLimited``.

    A 429 is retried with backoff; the single-429 cassette covers the
    escalation path, so the rate-limit retry budget is set to 0 here to
    exhaust it on the first hit.
    """
    monkeypatch.setattr(
        "inqtrix.providers.base._SDK_RATE_LIMIT_MAX_RETRIES", 0
    )
    provider = _build_with_api_key()

    with pytest.raises(AgentRateLimited):
        provider.complete("Triggert Rate-Limit")


@pytest.mark.vcr
def test_api_error_replay() -> None:
    """A non-429 surfaces as ``AzureOpenAIAPIError`` with structured details."""
    provider = _build_with_api_key()

    with pytest.raises(AzureOpenAIAPIError) as excinfo:
        provider.complete("Triggert deployment-not-found")
    err = excinfo.value
    assert err.status_code == 404
    assert err.error_code == "DeploymentNotFound"
    assert err.request_id == "req-azure-replay-404-1"
