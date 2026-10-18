"""Provider failure classification uses typed protocol evidence only."""

from __future__ import annotations

from inqtrix.exceptions import AgentProviderTimeout, AgentRateLimited
from inqtrix.provider_failure_contract import (
    ProviderFailureKind,
    classify_provider_failure,
)


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("message text is not classification input")


def test_inqtrix_semantic_wrappers_are_classified_directly() -> None:
    timeout = classify_provider_failure(AgentProviderTimeout("deadline"))
    rate_limit = classify_provider_failure(
        AgentRateLimited("deployment", RuntimeError("opaque SDK error"))
    )

    assert timeout.kind == ProviderFailureKind.TIMEOUT
    assert rate_limit.kind == ProviderFailureKind.RATE_LIMITED


def test_explicit_http_status_across_chain_outranks_transport_type() -> None:
    wrapper = RuntimeError("wrapper")
    wrapper.source = TimeoutError("incidental timeout")  # type: ignore[attr-defined]
    wrapper.__cause__ = _StatusError(400)

    result = classify_provider_failure(wrapper)

    assert result.kind == ProviderFailureKind.TERMINAL
    assert result.status_code == 400


def test_bedrock_response_metadata_status_is_structured_evidence() -> None:
    error = RuntimeError("bedrock wrapper")
    error.response = {  # type: ignore[attr-defined]
        "ResponseMetadata": {"HTTPStatusCode": 503}
    }

    result = classify_provider_failure(error)

    assert result.kind == ProviderFailureKind.UNAVAILABLE
    assert result.status_code == 503


def test_unknown_programming_error_is_not_assumed_to_be_a_provider_outage() -> None:
    result = classify_provider_failure(RuntimeError("implementation defect"))

    assert result.kind == ProviderFailureKind.UNKNOWN
    assert result.transient is False


def _sdk_error(status: int, body: dict | None):
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.invalid/chat")
    response = httpx.Response(status, request=request)
    error_class = {400: openai.BadRequestError, 404: openai.NotFoundError, 429: openai.RateLimitError,
                   503: openai.InternalServerError}[status]
    return error_class("provider says no", response=response, body=body)


def test_a_content_filter_rejection_is_a_terminal_content_refusal() -> None:
    """A refusal is deterministic and bound to the request's content: the
    Inqtrix wrapper and the SDK error both say so by their structured code."""
    from inqtrix.exceptions import AzureOpenAIAPIError

    wrapped = AzureOpenAIAPIError(model="deployment", status_code=400, error_code="content_filter")
    sdk = _sdk_error(400, {"code": "content_filter", "message": "filtered"})
    for exc in (wrapped, sdk, AzureOpenAIAPIError(model="d", status_code=400, error_code="x", original=sdk)):
        classification = classify_provider_failure(exc)
        assert classification.kind is ProviderFailureKind.TERMINAL
        assert classification.content_refused is True
        assert classification.transient is False


def test_a_refusal_needs_a_terminal_rejection_and_a_structured_code() -> None:
    """Never from message text, never for an outage, a rate limit or another code."""
    from inqtrix.exceptions import AgentStructuredOutputError, AzureOpenAIAPIError

    not_refusals = [
        _sdk_error(503, {"code": "content_filter"}),
        _sdk_error(429, {"code": "content_filter"}),
        _sdk_error(404, {"code": "DeploymentNotFound"}),
        _sdk_error(400, {"message": "content_filter triggered"}),
        AzureOpenAIAPIError(model="d", status_code=400, message="content_filter"),
        AgentStructuredOutputError("model", "Schema", "content_filter"),
        RuntimeError("content_filter"),
    ]
    for exc in not_refusals:
        assert classify_provider_failure(exc).content_refused is False, exc


def test_a_content_filter_400_is_never_retried_by_the_provider_layer() -> None:
    """A refused window must cost one call, not the SDK retry budget: the
    same request is refused again every time."""
    from inqtrix.providers.base import _is_retryable_sdk_error, _is_sdk_rate_limit

    refused = _sdk_error(400, {"code": "content_filter", "message": "filtered"})
    assert _is_retryable_sdk_error(refused) is False and _is_sdk_rate_limit(refused) is False
