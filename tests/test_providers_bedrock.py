"""Tests for the Bedrock LLM provider adapter."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from inqtrix.exceptions import AgentRateLimited, AgentTimeout, BedrockAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client_error(code: str, message: str = "error", status_code: int = 400, request_id: str = "req-123"):
    """Build a botocore ClientError with realistic response structure."""
    from botocore.exceptions import ClientError

    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status_code,
                "RequestId": request_id,
            },
        },
        operation_name="Converse",
    )


def _converse_response(text: str = "Hallo Welt", input_tokens: int = 12, output_tokens: int = 7):
    """Build a realistic Bedrock Converse response dict."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "stopReason": "end_turn",
    }


def _converse_response_with_thinking(text: str = "ok", thinking_text: str = "let me think..."):
    """Converse response containing both reasoning and text blocks."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": thinking_text, "signature": "sig"}}},
                    {"text": text},
                ],
            }
        },
        "usage": {"inputTokens": 20, "outputTokens": 15},
        "stopReason": "end_turn",
    }


@pytest.fixture()
def mock_boto3():
    """Patch boto3 so BedrockLLM can be imported and instantiated without real AWS credentials."""
    mock_client = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch("inqtrix.providers.bedrock.boto3") as mock_boto3_mod, \
            patch("inqtrix.providers.bedrock.BotoConfig"):
        mock_boto3_mod.Session.return_value = mock_session
        from inqtrix.providers.bedrock import BedrockLLM
        yield BedrockLLM, mock_client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_returns_text(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("Hallo Welt")

    llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")
    result = llm.complete("Frage")

    assert result == "Hallo Welt"
    mock_client.converse.assert_called_once()


def test_complete_with_metadata_returns_tokens(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("Antwort", 10, 5)

    llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")
    state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    resp = llm.complete_with_metadata("Frage", state=state)

    assert resp.content == "Antwort"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.finish_reason == "end_turn"
    assert resp.raw is not None
    assert state["total_prompt_tokens"] == 10
    assert state["total_completion_tokens"] == 5


def test_complete_structured_uses_bedrock_output_config(
    mock_boto3: tuple[type, MagicMock],
) -> None:
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response(
        '{"claims": []}',
        10,
        4,
    )

    llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")
    response = llm.complete_structured(
        "Extract claims",
        schema={
            "type": "object",
            "properties": {"claims": {"type": "array"}},
            "required": ["claims"],
            "additionalProperties": False,
        },
        schema_name="inqtrix_claim_extraction_v1",
        schema_description="Extract claims",
    )

    assert response.parsed == {"claims": []}
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 4
    call_kwargs = mock_client.converse.call_args.kwargs
    text_format = call_kwargs["outputConfig"]["textFormat"]
    assert text_format["type"] == "json_schema"
    json_schema = text_format["structure"]["jsonSchema"]
    assert json_schema["name"] == "inqtrix_claim_extraction_v1"
    assert json.loads(json_schema["schema"])["required"] == ["claims"]


def test_complete_with_system_prompt(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM()
    llm.complete("Frage", system="Du bist ein Forscher.")

    call_kwargs = mock_client.converse.call_args
    params = call_kwargs.kwargs if call_kwargs.kwargs else {}
    assert "system" in params
    assert params["system"] == [{"text": "Du bist ein Forscher."}]


def test_claim_extract_model_defaults_to_default_model(mock_boto3):
    """Claim extraction model metadata falls back to the default model."""
    BedrockLLM, _ = mock_boto3

    llm = BedrockLLM(default_model="nvidia.nemotron-super-3-120b")

    assert llm.models.effective_claim_extract_model == "nvidia.nemotron-super-3-120b"


def test_is_available_returns_true(mock_boto3):
    BedrockLLM, _ = mock_boto3
    llm = BedrockLLM()
    assert llm.is_available() is True


# ---------------------------------------------------------------------------
# Thinking
# ---------------------------------------------------------------------------


def test_thinking_in_params(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response_with_thinking("ok")

    llm = BedrockLLM(thinking={"type": "adaptive"})
    resp = llm.complete_with_metadata("test")

    call_kwargs = mock_client.converse.call_args
    # additionalModelRequestFields should contain thinking
    assert "additionalModelRequestFields" in (call_kwargs.kwargs or {})
    assert call_kwargs.kwargs["additionalModelRequestFields"]["thinking"] == {
        "type": "adaptive"}
    # _extract_text must skip reasoning blocks
    assert resp.content == "ok"


def test_bedrock_reasoning_effort_none_suppresses_thinking_and_effort(mock_boto3):
    """A per-call ``reasoning_effort="none"`` drops thinking AND effort.

    The uniform replacement for the removed ``without_thinking`` context
    manager: helper paths (claim extraction on the fast tier) pass ``"none"``
    so neither field rides in ``additionalModelRequestFields``.
    """
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(thinking={"type": "adaptive"}, effort="medium")

    # Inherit (reasoning_effort=""/None): both fields present.
    llm.complete("inherit")
    additional = mock_client.converse.call_args.kwargs.get(
        "additionalModelRequestFields", {}
    )
    assert additional.get("thinking") == {"type": "adaptive"}
    assert additional.get("output_config") == {"effort": "medium"}

    # reasoning_effort="none": no additionalModelRequestFields at all.
    mock_client.converse.reset_mock()
    mock_client.converse.return_value = _converse_response("ok")
    llm.complete("none", reasoning_effort="none")
    assert "additionalModelRequestFields" not in mock_client.converse.call_args.kwargs


def test_thinking_auto_raises_max_tokens(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    from inqtrix.providers.bedrock import _THINKING_MIN_MAX_TOKENS

    llm = BedrockLLM(
        default_max_tokens=1024,
        thinking={"type": "enabled", "budget_tokens": 8000},
    )
    llm.complete("test")

    call_kwargs = mock_client.converse.call_args
    inference_config = call_kwargs.kwargs.get("inferenceConfig", {})
    # budget_tokens (8000) >= default_max_tokens (1024) → raised to 9024,
    # then clamped up to _THINKING_MIN_MAX_TOKENS (16384) by the floor check.
    assert inference_config["maxTokens"] == _THINKING_MIN_MAX_TOKENS


def test_temperature_and_thinking_raises(mock_boto3):
    BedrockLLM, _ = mock_boto3
    with pytest.raises(ValueError, match="mutually exclusive"):
        BedrockLLM(temperature=0.5, thinking={"type": "adaptive"})


def test_bedrock_effort_in_output_config_with_thinking(mock_boto3):
    """effort travels via additionalModelRequestFields.output_config alongside thinking."""
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(thinking={"type": "adaptive"}, effort="medium")
    llm.complete("test")

    call_kwargs = mock_client.converse.call_args
    additional = call_kwargs.kwargs.get("additionalModelRequestFields", {})
    assert additional.get("thinking") == {"type": "adaptive"}
    assert additional.get("output_config") == {"effort": "medium"}


def test_bedrock_effort_works_without_thinking(mock_boto3):
    """effort can be used standalone (controls overall token spend)."""
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(effort="low")
    llm.complete("test")

    call_kwargs = mock_client.converse.call_args
    additional = call_kwargs.kwargs.get("additionalModelRequestFields", {})
    assert additional == {"output_config": {"effort": "low"}}


def test_bedrock_effort_invalid_value_raises(mock_boto3):
    BedrockLLM, _ = mock_boto3
    with pytest.raises(ValueError, match="effort must be one of"):
        BedrockLLM(effort="ultra-mega")


def test_bedrock_effort_accepts_xhigh_and_max(mock_boto3):
    """xhigh + max are valid effort levels (Opus 4.7 / Mythos)."""
    BedrockLLM, _ = mock_boto3
    BedrockLLM(effort="xhigh")
    BedrockLLM(effort="max")


def test_bedrock_effort_skipped_for_haiku_per_call_model(mock_boto3):
    """Bedrock Haiku per-call model must omit output_config.effort.

    Same blacklist mechanic as in AnthropicLLM. With effort configured on
    the session, calls targeted at Haiku via the per-call ``model`` arg
    must not include output_config — otherwise Bedrock returns 400.
    """
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(
        default_model="eu.anthropic.claude-opus-4-6-v1",
        claim_extract_model="eu.anthropic.claude-haiku-4-5",
        thinking={"type": "adaptive"},
        effort="medium",
    )

    # Reasoning call (Opus, default) → effort sent.
    llm.complete("question")
    params = mock_client.converse.call_args.kwargs
    additional = params.get("additionalModelRequestFields", {})
    assert additional.get("output_config") == {"effort": "medium"}

    # Helper call to Haiku via complete_with_metadata model override → no effort.
    mock_client.converse.reset_mock()
    mock_client.converse.return_value = _converse_response("ok")
    llm.complete_with_metadata("snippet", model="eu.anthropic.claude-haiku-4-5")
    params = mock_client.converse.call_args.kwargs
    additional = params.get("additionalModelRequestFields", {})
    assert "output_config" not in additional


def test_bedrock_effort_kept_for_sonnet_per_call_model(mock_boto3):
    """Bedrock Sonnet helper call DOES receive effort."""
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(
        default_model="eu.anthropic.claude-opus-4-6-v1",
        claim_extract_model="eu.anthropic.claude-sonnet-4-6",
        thinking={"type": "adaptive"},
        effort="medium",
    )
    llm.complete_with_metadata("snippet", model="eu.anthropic.claude-sonnet-4-6")
    params = mock_client.converse.call_args.kwargs
    additional = params.get("additionalModelRequestFields", {})
    assert additional.get("output_config") == {"effort": "medium"}


def test_bedrock_effort_config_warnings_emitted_for_haiku_role(mock_boto3, caplog):
    """Configuring effort + a Haiku role on Bedrock yields a warning."""
    import logging

    BedrockLLM, _ = mock_boto3
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        llm = BedrockLLM(
            default_model="eu.anthropic.claude-opus-4-6-v1",
            claim_extract_model="eu.anthropic.claude-haiku-4-5",
            thinking={"type": "adaptive"},
            effort="medium",
        )
    warnings = llm.consume_effort_config_warnings()
    assert len(warnings) == 1
    assert "claim_extract_model" in warnings[0]
    assert "haiku" in warnings[0].lower()
    assert "is not sent for" in warnings[0]
    assert "Set effort=None" in warnings[0]
    # And the same line reached the log.
    assert any("haiku" in rec.getMessage().lower() for rec in caplog.records)
    # Consume drains the list.
    assert llm.consume_effort_config_warnings() == []


def test_bedrock_effort_no_warning_when_all_models_support_it(mock_boto3, caplog):
    """No warning when every configured role uses an effort-capable Bedrock model."""
    import logging

    BedrockLLM, _ = mock_boto3
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        llm = BedrockLLM(
            default_model="eu.anthropic.claude-opus-4-6-v1",
            claim_extract_model="eu.anthropic.claude-sonnet-4-6",
            thinking={"type": "adaptive"},
            effort="medium",
        )
    assert llm.consume_effort_config_warnings() == []


def test_temperature_in_params(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(temperature=0.3)
    llm.complete("test")

    call_kwargs = mock_client.converse.call_args
    inference_config = call_kwargs.kwargs.get("inferenceConfig", {})
    assert inference_config["temperature"] == 0.3
    assert "additionalModelRequestFields" not in (call_kwargs.kwargs or {})


def test_complete_allows_per_call_output_budget_override(mock_boto3):
    BedrockLLM, mock_client = mock_boto3
    mock_client.converse.return_value = _converse_response("ok")

    llm = BedrockLLM(default_max_tokens=1024)
    llm.complete("test", max_output_tokens=77)

    call_kwargs = mock_client.converse.call_args
    inference_config = call_kwargs.kwargs.get("inferenceConfig", {})
    assert inference_config["maxTokens"] == 77


# ---------------------------------------------------------------------------
# Retry & error handling
# ---------------------------------------------------------------------------


def test_retries_transient_error_then_succeeds(mock_boto3):
    BedrockLLM, mock_client = mock_boto3


    transient = _make_client_error("ServiceUnavailableException", "service busy", 503)
    mock_client.converse.side_effect = [transient, transient, _converse_response("ok")]

    llm = BedrockLLM()
    # Patch sleep to avoid real delays
    with patch("inqtrix.providers.bedrock.time") as mock_time:
        mock_time.monotonic.return_value = time.monotonic() + 1000
        mock_time.sleep = MagicMock()
        result = llm.complete("test")

    assert result == "ok"
    assert mock_client.converse.call_count == 3


def test_throttling_retries_then_raises_rate_limited(mock_boto3):
    BedrockLLM, mock_client = mock_boto3

    throttle = _make_client_error("ThrottlingException", "rate exceeded", 429)
    mock_client.converse.side_effect = [throttle] * 5

    llm = BedrockLLM()
    with patch("inqtrix.providers.bedrock.time") as mock_time:
        mock_time.monotonic.return_value = time.monotonic() + 1000
        mock_time.sleep = MagicMock()
        with pytest.raises(AgentRateLimited):
            llm.complete("test")

    assert mock_client.converse.call_count == 3


def test_non_retryable_error_raises_bedrock_api_error(mock_boto3):
    BedrockLLM, mock_client = mock_boto3

    validation_error = _make_client_error(
        "ValidationException", "invalid input", 400, "req-val-456")
    mock_client.converse.side_effect = validation_error

    llm = BedrockLLM()
    with pytest.raises(BedrockAPIError) as exc_info:
        llm.complete("test")

    error = exc_info.value
    assert error.error_code == "ValidationException"
    assert error.status_code == 400
    assert error.request_id == "req-val-456"
    assert "invalid input" in str(error)


def test_connection_closed_retries_once_then_succeeds(mock_boto3):
    from botocore.exceptions import ConnectionClosedError

    BedrockLLM, mock_client = mock_boto3

    transport_error = ConnectionClosedError(endpoint_url="https://example.test")
    mock_client.converse.side_effect = [transport_error, _converse_response("ok")]

    llm = BedrockLLM()
    observed = []
    with patch("inqtrix.providers.bedrock._sleep_before_retry") as sleep:
        with llm.observe_retries(lambda notice: observed.append(notice)):
            result = llm.complete("test")

    notices = llm.consume_retry_notices()
    assert result == "ok"
    assert mock_client.converse.call_count == 2
    assert sleep.call_count == 1
    assert len(observed) == 1
    assert observed[0]["provider"] == "Bedrock"
    assert observed[0]["progress_emitted"] is True
    assert len(notices) == 1
    assert notices[0]["error_code"] == "ConnectionClosedError"
    assert notices[0]["max_attempts"] == 3


def test_connection_closed_exhausts_limited_transport_attempts(mock_boto3):
    from botocore.exceptions import ConnectionClosedError

    BedrockLLM, mock_client = mock_boto3

    transport_error = ConnectionClosedError(endpoint_url="https://example.test")
    mock_client.converse.side_effect = [transport_error] * 5

    llm = BedrockLLM()
    with patch("inqtrix.providers.bedrock._sleep_before_retry"):
        with pytest.raises(BedrockAPIError) as exc_info:
            llm.complete("test")

    assert mock_client.converse.call_count == 3
    assert exc_info.value.error_code == "ConnectionClosedError"


# ---------------------------------------------------------------------------
# Deadline enforcement
# ---------------------------------------------------------------------------


def test_deadline_raises_agent_timeout(mock_boto3):
    BedrockLLM, mock_client = mock_boto3

    llm = BedrockLLM()
    # Set a deadline that's already in the past
    past_deadline = time.monotonic() - 10

    with pytest.raises(AgentTimeout):
        llm.complete("test", deadline=past_deadline)


def test_transport_read_timeout_is_bounded_by_outer_deadline():
    """A short outer budget must reach botocore, not just Python checks."""
    mock_client = MagicMock()
    mock_client.converse.return_value = _converse_response("ok")
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch("inqtrix.providers.bedrock.boto3") as mock_boto3_mod, patch(
        "inqtrix.providers.bedrock.BotoConfig",
        side_effect=lambda **kwargs: kwargs,
    ):
        mock_boto3_mod.Session.return_value = mock_session
        from inqtrix.providers.bedrock import BedrockLLM

        llm = BedrockLLM(timeout=600)
        outer_deadline = time.monotonic() + 5
        llm.complete("test", timeout=600, deadline=outer_deadline)

    read_timeouts = [
        call.kwargs["config"]["read_timeout"]
        for call in mock_session.client.call_args_list
    ]
    connect_timeouts = [
        call.kwargs["config"]["connect_timeout"]
        for call in mock_session.client.call_args_list
    ]
    assert read_timeouts[0] == 600
    assert 0 < read_timeouts[-1] <= 5
    assert connect_timeouts == read_timeouts


def _make_transport_mocked_provider():
    """Build a BedrockLLM whose session yields a fresh mock client per build."""
    mock_session = MagicMock()
    mock_session.client.side_effect = lambda *args, **kwargs: MagicMock()

    with patch("inqtrix.providers.bedrock.boto3") as mock_boto3_mod, patch(
        "inqtrix.providers.bedrock.BotoConfig",
        side_effect=lambda **kwargs: kwargs,
    ):
        mock_boto3_mod.Session.return_value = mock_session
        from inqtrix.providers.bedrock import BedrockLLM

        return BedrockLLM(timeout=600), mock_session


def test_client_for_deadline_reuses_rung_clients_under_shrinking_budget():
    """A second-by-second shrinking budget maps to rungs, not one client each."""
    from inqtrix.providers.bedrock import _TRANSPORT_TIMEOUT_RUNGS

    llm, mock_session = _make_transport_mocked_provider()

    seen_timeouts = set()
    for remaining in range(120, 1, -1):
        _, read_timeout = llm._client_for_deadline(time.monotonic() + remaining)
        assert read_timeout <= remaining
        assert read_timeout in _TRANSPORT_TIMEOUT_RUNGS
        seen_timeouts.add(read_timeout)

    assert len(seen_timeouts) <= 12
    assert mock_session.client.call_count == 1 + len(seen_timeouts)


def test_client_for_deadline_caps_cached_clients():
    """The client cache stays within the LRU bound across every rung."""
    from inqtrix.providers.bedrock import (
        _MAX_TRANSPORT_CLIENTS,
        _TRANSPORT_TIMEOUT_RUNGS,
    )

    llm, _ = _make_transport_mocked_provider()

    for rung in _TRANSPORT_TIMEOUT_RUNGS:
        llm._client_for_deadline(time.monotonic() + rung + 0.5)
        assert len(llm._clients_by_read_timeout) <= _MAX_TRANSPORT_CLIENTS

    top_rung = _TRANSPORT_TIMEOUT_RUNGS[-1]
    client, read_timeout = llm._client_for_deadline(
        time.monotonic() + top_rung + 5
    )
    cached_client, cached_timeout = llm._client_for_deadline(
        time.monotonic() + top_rung + 5
    )
    assert cached_client is client
    assert cached_timeout == read_timeout == top_rung


# ---------------------------------------------------------------------------
# Model settings
# ---------------------------------------------------------------------------


def test_models_property(mock_boto3):
    BedrockLLM, _ = mock_boto3
    llm = BedrockLLM(
        default_model="eu.anthropic.claude-opus-4-6-v1",
        classify_model="eu.anthropic.claude-sonnet-4-6",
        claim_extract_model="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        evaluate_model="eu.anthropic.claude-sonnet-4-6",
    )
    assert llm.models.reasoning_model == "eu.anthropic.claude-opus-4-6-v1"
    assert llm.models.classify_model == "eu.anthropic.claude-sonnet-4-6"
    assert llm.models.claim_extract_model == "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert llm.models.evaluate_model == "eu.anthropic.claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# BedrockAPIError
# ---------------------------------------------------------------------------


def test_bedrock_api_error_message_format():
    err = BedrockAPIError(
        model="eu.anthropic.claude-opus-4-6-v1",
        error_code="ValidationException",
        status_code=400,
        message="invalid model id",
        request_id="req-abc",
    )
    assert "Bedrock call failed" in str(err)
    assert "eu.anthropic.claude-opus-4-6-v1" in str(err)
    assert "ValidationException" in str(err)
    assert "HTTP 400" in str(err)
    assert "request-id=req-abc" in str(err)
    assert "invalid model id" in str(err)
