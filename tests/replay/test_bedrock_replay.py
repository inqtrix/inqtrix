"""Stubber-based replay tests for the Bedrock provider.

Bedrock-runtime is not covered by VCR (vcrpy's boto3 integration is
fragile for non-S3 services and Moto does not support
``bedrock-runtime`` at all — see ``docs/development/testing-strategy.md``
for the rationale). Instead, we use ``botocore.stub.Stubber``, the
official AWS-recommended mechanism for unit-testing boto3 clients.

Each test:

1. Sets fake AWS env vars so :class:`BedrockLLM`'s constructor can
   instantiate a real boto3 client without hitting the credential
   chain.
2. Wraps the constructed client in a ``Stubber``.
3. Queues canned responses (success / throttling sequences / errors)
   via ``add_response`` / ``add_client_error``.
4. Activates the stubber and exercises the provider end-to-end.

The fake-credentials approach is deliberately scoped to the
``stubbed_provider`` fixture so the global ``isolate_provider_env``
auto-fixture from ``tests/replay/conftest.py`` keeps its protective
effect on every other test in this module.
"""

from __future__ import annotations

import pytest
from botocore.stub import Stubber

from inqtrix.exceptions import AgentRateLimited, BedrockAPIError
from inqtrix.providers.bedrock import _MAX_BEDROCK_ATTEMPTS, BedrockLLM

from tests.fixtures.bedrock_responses import (
    load_bedrock_response,
    make_service_unavailable_error,
    make_throttling_error,
    make_validation_error,
)

pytestmark = pytest.mark.replay


@pytest.fixture()
def stubbed_provider(monkeypatch: pytest.MonkeyPatch):
    """Yield ``(provider, stubber)`` with a real boto3 client + Stubber.

    Sets fake AWS env vars before construction so the provider's
    boto3 client can be created without hitting the AWS credential
    chain. The ``isolate_provider_env`` auto-fixture in
    ``tests/replay/conftest.py`` deletes ``AWS_*`` vars first;
    re-adding them here is intentional and scoped to this fixture.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")

    provider = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")
    stubber = Stubber(provider._client)
    # The provider caches one transport client per remaining-deadline
    # bucket (``_client_for_deadline``), so the very first call misses the
    # constructor's default bucket and would build a fresh UNSTUBBED
    # client (= a real HTTP call). Route every bucket to the one stubbed
    # client; the deadline/bucket math itself stays fully exercised.
    monkeypatch.setattr(
        provider, "_build_client", lambda read_timeout: provider._client
    )
    stubber.activate()
    try:
        yield provider, stubber
    finally:
        stubber.deactivate()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_complete_success_stub(stubbed_provider) -> None:
    """A successful Converse call yields content + token counts."""
    provider, stubber = stubbed_provider
    stubber.add_response("converse", load_bedrock_response("success"))

    state: dict = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    response = provider.complete_with_metadata("Test", state=state)

    assert "Bedrock-Converse-Replay" in response.content
    assert response.prompt_tokens == 14
    assert response.completion_tokens == 18
    assert response.finish_reason == "end_turn"
    assert state["total_prompt_tokens"] == 14
    assert state["total_completion_tokens"] == 18

    stubber.assert_no_pending_responses()


def test_thinking_response_stub(stubbed_provider) -> None:
    """A thinking-enabled response surfaces visible text only.

    The Bedrock helper extracts text-only blocks for the ``content``
    payload; the reasoning block (``reasoningContent``) is surfaced
    elsewhere (logging / state) and is intentionally not part of the
    visible answer.
    """
    provider, stubber = stubbed_provider
    stubber.add_response("converse", load_bedrock_response("success_thinking"))

    response = provider.complete_with_metadata("Test mit Thinking")

    assert "Mit Thinking-Block" in response.content
    assert "Erst denken" not in response.content
    assert response.prompt_tokens == 22
    assert response.completion_tokens == 31

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Retry / failure paths
# ---------------------------------------------------------------------------


def test_throttling_then_success_stub(stubbed_provider) -> None:
    """A throttling burst recovers on the final retry attempt."""
    provider, stubber = stubbed_provider

    # Burst pattern: (_MAX_BEDROCK_ATTEMPTS - 1) throttling + 1 success.
    for _ in range(_MAX_BEDROCK_ATTEMPTS - 1):
        stubber.add_client_error(
            "converse",
            service_error_code="ThrottlingException",
            service_message="Rate exceeded",
            http_status_code=429,
            response_meta={"RequestId": "req-replay-throttling-burst"},
        )
    stubber.add_response("converse", load_bedrock_response("success"))

    response = provider.complete_with_metadata("Test throttle-then-success")

    assert "Bedrock-Converse-Replay" in response.content
    stubber.assert_no_pending_responses()


def test_throttling_terminal_stub(stubbed_provider) -> None:
    """All retry attempts throttle → ``AgentRateLimited`` (graph-fatal)."""
    provider, stubber = stubbed_provider

    for _ in range(_MAX_BEDROCK_ATTEMPTS):
        stubber.add_client_error(
            "converse",
            service_error_code="ThrottlingException",
            service_message="Rate exceeded",
            http_status_code=429,
            response_meta={"RequestId": "req-replay-throttle-terminal"},
        )

    with pytest.raises(AgentRateLimited):
        provider.complete("Test terminal throttle")
    stubber.assert_no_pending_responses()


def test_validation_error_stub(stubbed_provider) -> None:
    """A 400 ValidationException is non-retryable and surfaces immediately."""
    provider, stubber = stubbed_provider
    stubber.add_client_error(
        "converse",
        service_error_code="ValidationException",
        service_message="Invalid model id format",
        http_status_code=400,
        response_meta={"RequestId": "req-replay-validation-1"},
    )

    with pytest.raises(BedrockAPIError) as excinfo:
        provider.complete("Triggert ValidationException")

    err = excinfo.value
    assert err.error_code == "ValidationException"
    assert err.status_code == 400
    assert err.request_id == "req-replay-validation-1"
    stubber.assert_no_pending_responses()


def test_service_unavailable_then_success_stub(stubbed_provider) -> None:
    """A 503 is retried like a throttling error and recovers on the next call."""
    provider, stubber = stubbed_provider
    stubber.add_client_error(
        "converse",
        service_error_code="ServiceUnavailableException",
        service_message="Service unavailable",
        http_status_code=503,
        response_meta={"RequestId": "req-replay-svc-1"},
    )
    stubber.add_response("converse", load_bedrock_response("success"))

    response = provider.complete_with_metadata("Test 503-then-success")

    assert "Bedrock-Converse-Replay" in response.content
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Helper-builder smoke tests (catches typos in tests/fixtures/bedrock_responses.py)
# ---------------------------------------------------------------------------


def test_helper_builders_produce_expected_error_codes() -> None:
    assert make_throttling_error().response["Error"]["Code"] == "ThrottlingException"
    assert make_validation_error().response["Error"]["Code"] == "ValidationException"
    assert (
        make_service_unavailable_error().response["Error"]["Code"]
        == "ServiceUnavailableException"
    )
