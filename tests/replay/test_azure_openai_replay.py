"""VCR-replay tests for the Azure OpenAI LLM provider.

Coverage:

* Four distinct cassettes (success-api-key, success-sp-auth, rate-limit,
  api-error) under ``tests/fixtures/cassettes/azure_openai/``.
* Two of the six tests share the success cassette via explicit
  ``@pytest.mark.vcr("test_complete_success_api_key_replay.yaml")``
  to cover the remaining two Azure auth modes (custom token-provider
  callable and pre-built ``credential``) without duplicating wire data.

Auth-mode mocking notes: the ``ClientSecretCredential`` and
``get_bearer_token_provider`` symbols are imported lazily inside the
helper :func:`build_azure_openai_token_provider`, so they must be
patched on the ``azure.identity`` module itself (not on the inqtrix
import path).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

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


def _build_with_api_key(*, max_retries: int | None = None) -> AzureOpenAILLM:
    provider = AzureOpenAILLM(
        azure_endpoint=_ENDPOINT,
        api_key="test-key",
        default_model="my-gpt4o-deployment",
    )
    if max_retries is not None:
        provider._client = provider._client.with_options(max_retries=max_retries)
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
def test_complete_success_sp_auth_replay() -> None:
    """Service-Principal auth (tenant_id/client_id/client_secret) succeeds.

    ``ClientSecretCredential`` and ``get_bearer_token_provider`` are
    patched on ``azure.identity`` because :func:`build_azure_openai_token_provider`
    imports them lazily inside the function body.
    """
    fake_token_provider = MagicMock(return_value="fake-bearer-token")

    with patch("azure.identity.ClientSecretCredential") as mock_cred_cls, \
            patch("azure.identity.get_bearer_token_provider", return_value=fake_token_provider):
        mock_cred_cls.return_value = MagicMock()
        provider = AzureOpenAILLM(
            azure_endpoint=_ENDPOINT,
            tenant_id="fake-tenant",
            client_id="fake-client-id",
            client_secret="fake-client-secret",
            default_model="sp-gpt4o-deployment",
        )

    response = provider.complete("Hello via SP")
    assert "Service-Principal-Auth" in response


@pytest.mark.vcr("test_complete_success_api_key_replay.yaml")
def test_complete_success_token_provider_replay() -> None:
    """Custom ``azure_ad_token_provider`` callable wires through unchanged."""
    fake_token_provider = MagicMock(return_value="static-bearer")

    provider = AzureOpenAILLM(
        azure_endpoint=_ENDPOINT,
        azure_ad_token_provider=fake_token_provider,
        default_model="my-gpt4o-deployment",
    )

    response = provider.complete("Hello via token-provider")
    assert "Azure OpenAI" in response


@pytest.mark.vcr("test_complete_success_api_key_replay.yaml")
def test_complete_success_credential_replay() -> None:
    """Pre-built ``credential`` object also produces a valid completion.

    Same wire shape as api-key auth, so we reuse the success cassette.
    """
    fake_credential = MagicMock()
    fake_token_provider = MagicMock(return_value="cred-bearer")

    with patch("azure.identity.get_bearer_token_provider", return_value=fake_token_provider):
        provider = AzureOpenAILLM(
            azure_endpoint=_ENDPOINT,
            credential=fake_credential,
            default_model="my-gpt4o-deployment",
        )

    response = provider.complete("Hello via credential")
    assert "Azure OpenAI" in response


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
    provider = _build_with_api_key(max_retries=0)

    with pytest.raises(AgentRateLimited):
        provider.complete("Triggert Rate-Limit")


@pytest.mark.vcr
def test_api_error_replay() -> None:
    """A non-429 surfaces as ``AzureOpenAIAPIError`` with structured details."""
    provider = _build_with_api_key(max_retries=0)

    with pytest.raises(AzureOpenAIAPIError) as excinfo:
        provider.complete("Triggert deployment-not-found")
    err = excinfo.value
    assert err.status_code == 404
    assert err.error_code == "DeploymentNotFound"
    assert err.request_id == "req-azure-replay-404-1"
