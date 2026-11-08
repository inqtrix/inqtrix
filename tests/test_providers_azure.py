"""Tests for the Azure OpenAI LLM provider adapter."""

from __future__ import annotations

import asyncio

import time
from unittest.mock import MagicMock, patch

import pytest

from inqtrix.providers.attempts import observe_retries
from inqtrix.exceptions import AgentRateLimited, AgentTimeout, AzureOpenAIAPIError
from tests._sdk_doubles import async_sdk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_completion_response(
    content: str = "Hallo Welt",
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
):
    """Build a mock ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.delta = None
    choice.finish_reason = "stop"

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model_dump.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }
    return response


@pytest.fixture()
def mock_azure_client():
    """Serve the adapter's SDK calls from a sync mock; no credentials, no network."""
    mock_client = MagicMock()

    with patch("inqtrix.providers.azure.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = async_sdk(mock_client.chat.completions.create)
        from inqtrix.providers.azure import AzureOpenAILLM
        yield AzureOpenAILLM, mock_client


def _built_client_kwargs(llm) -> dict:
    """The keyword arguments the adapter builds its SDK client with, at first use."""

    async def first_use() -> None:
        llm._async_client()

    with patch("inqtrix.providers.azure.AsyncOpenAI") as mock_cls:
        asyncio.run(first_use())
    return mock_cls.call_args.kwargs


def test_client_uses_v1_base_url_from_endpoint(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    call_kwargs = _built_client_kwargs(
        AzureOpenAILLM(
            azure_endpoint="https://test.openai.azure.com/",
            api_key="test-key",
        )
    )

    assert call_kwargs["base_url"] == "https://test.openai.azure.com/openai/v1/"
    assert call_kwargs["api_key"] == "test-key"
    assert call_kwargs["max_retries"] == 0


def test_client_accepts_explicit_base_url(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    call_kwargs = _built_client_kwargs(
        AzureOpenAILLM(
            base_url="https://test.openai.azure.com/openai/v1/",
            api_key="test-key",
        )
    )

    assert call_kwargs["base_url"] == "https://test.openai.azure.com/openai/v1/"


def test_ai_project_endpoint_rejected(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    with pytest.raises(ValueError, match="AI Project endpoint"):
        AzureOpenAILLM(
            azure_endpoint="https://my-project.services.ai.azure.com/api",
            api_key="test-key",
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_returns_text(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("Hallo Welt")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        default_model="gpt-4o",
    )
    result = llm.complete("Frage")

    assert result == "Hallo Welt"
    mock_client.chat.completions.create.assert_called_once()


def test_complete_with_metadata_returns_tokens(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response(
        "Antwort", 10, 5)

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    resp = llm.complete_with_metadata("Frage", state=state)

    assert resp.content == "Antwort"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.finish_reason == "stop"
    assert resp.raw is not None
    assert state["total_prompt_tokens"] == 10
    assert state["total_completion_tokens"] == 5


def test_complete_structured_sets_response_format(
    mock_azure_client: tuple[type, MagicMock],
) -> None:
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response(
        '{"claims": []}',
        10,
        4,
    )

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    response = llm.complete_structured(
        "Extract claims",
        schema={
            "type": "object",
            "properties": {"claims": {"type": "array"}},
            "required": ["claims"],
            "additionalProperties": False,
        },
        schema_name="inqtrix_claim_extraction_v1",
    )

    assert response.parsed == {"claims": []}
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 4
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    response_format = call_kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "inqtrix_claim_extraction_v1"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == ["claims"]


def test_complete_structured_hardens_defaulted_schema_to_strict(
    mock_azure_client: tuple[type, MagicMock],
) -> None:
    # A model with a defaulted/optional property (as Pydantic emits for the
    # shared ExecutionPlanModel) must be normalised to the strict contract by
    # the provider: every property in `required`, no `default` — otherwise
    # Azure `strict:True` returns HTTP 400 (the agent PLAN-phase failure).
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response(
        '{"a": 1, "b": 2}', 3, 3
    )
    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    llm.complete_structured(
        "x",
        schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer", "default": 0},
            },
            "required": ["a"],
            "additionalProperties": False,
        },
        schema_name="s",
    )
    sent = mock_client.chat.completions.create.call_args.kwargs["response_format"][
        "json_schema"
    ]["schema"]
    assert set(sent["required"]) == {"a", "b"}
    assert "default" not in sent["properties"]["b"]


def test_complete_with_system_prompt(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    llm.complete("Frage", system="Du bist ein Forscher.")

    call_kwargs = mock_client.chat.completions.create.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
    assert messages[0] == {"role": "system", "content": "Du bist ein Forscher."}
    assert messages[1] == {"role": "user", "content": "Frage"}


def test_is_available_returns_true(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    assert llm.is_available() is True


# ---------------------------------------------------------------------------
# Model settings
# ---------------------------------------------------------------------------


def test_models_property(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        default_model="gpt-4o",
        classify_model="gpt-4o-mini",
        claim_extract_model="gpt-4o-mini",
        evaluate_model="gpt-4o-mini",
    )
    assert llm.models.reasoning_model == "gpt-4o"
    assert llm.models.classify_model == "gpt-4o-mini"
    assert llm.models.claim_extract_model == "gpt-4o-mini"
    assert llm.models.evaluate_model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------


def test_temperature_in_params(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        temperature=0.3,
    )
    llm.complete("test")

    call_kwargs = mock_client.chat.completions.create.call_args
    assert call_kwargs.kwargs.get("temperature") == 0.3


def test_completion_uses_max_completion_tokens_by_default(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        default_max_tokens=123,
    )
    llm.complete("test")

    call_kwargs = mock_client.chat.completions.create.call_args
    assert call_kwargs.kwargs.get("max_completion_tokens") == 123
    assert "max_tokens" not in call_kwargs.kwargs


def test_completion_can_use_max_tokens_when_configured(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        default_max_tokens=321,
        token_budget_parameter="max_tokens",
    )
    llm.complete("test")

    call_kwargs = mock_client.chat.completions.create.call_args
    assert call_kwargs.kwargs.get("max_tokens") == 321
    assert "max_completion_tokens" not in call_kwargs.kwargs


def test_completion_allows_per_call_output_budget_override(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        default_max_tokens=321,
    )
    llm.complete("test", max_output_tokens=77)

    call_kwargs = mock_client.chat.completions.create.call_args
    assert call_kwargs.kwargs.get("max_completion_tokens") == 77


def test_no_temperature_when_not_set(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client
    mock_client.chat.completions.create.return_value = _chat_completion_response("ok")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    llm.complete("test")

    call_kwargs = mock_client.chat.completions.create.call_args
    assert "temperature" not in call_kwargs.kwargs


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_rate_limit_raises_agent_rate_limited(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    from openai import RateLimitError
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    error = RateLimitError(
        message="rate limit",
        response=mock_response,
        body=None,
    )
    mock_client.chat.completions.create.side_effect = error

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )

    with pytest.raises(AgentRateLimited):
        llm.complete("test")


def test_api_status_error_429_raises_agent_rate_limited(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    from openai import APIStatusError

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    error = APIStatusError(
        message="too many requests",
        response=mock_response,
        body=None,
    )
    mock_client.chat.completions.create.side_effect = error

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )

    with pytest.raises(AgentRateLimited):
        llm.complete("test")


def test_api_status_error_non_429_raises_azure_api_error(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    from openai import APIStatusError

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {"apim-request-id": "req-404"}
    error = APIStatusError(
        message="deployment not found",
        response=mock_response,
        body={"error": {"code": "DeploymentNotFound", "message": "deployment not found"}},
    )
    mock_client.chat.completions.create.side_effect = error

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )

    with pytest.raises(AzureOpenAIAPIError) as exc_info:
        llm.complete("test")

    err = exc_info.value
    assert err.status_code == 404
    assert err.model == "gpt-4o"
    assert err.error_code == "DeploymentNotFound"
    assert err.request_id == "req-404"
    assert "Azure OpenAI call failed" in str(err)


def test_transient_api_status_retries_with_visible_notice(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    from openai import APIStatusError

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.headers = {"apim-request-id": "req-503"}
    error = APIStatusError(
        message="service unavailable",
        response=mock_response,
        body={"error": {"code": "ServiceUnavailable", "message": "busy"}},
    )
    mock_client.chat.completions.create.side_effect = [
        error,
        _chat_completion_response("ok"),
    ]

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    observed = []

    with patch("inqtrix.providers.attempts._sleep_before_retry") as sleep:
        with observe_retries("model", lambda notice: observed.append(notice)):
            response = llm.complete_with_metadata("test")

    assert response.content == "ok"
    assert mock_client.chat.completions.create.call_count == 2
    assert sleep.call_count == 1
    assert len(observed) == 1
    assert observed[0]["provider"] == "AzureOpenAI"
    assert observed[0]["error_code"] == "ServiceUnavailable"
    assert observed[0]["status_code"] == 503
    assert observed[0]["request_id"] == "req-503"
    assert observed[0]["progress_emitted"] is True


def test_openai_error_raises_azure_api_error(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    from openai import OpenAIError

    mock_client.chat.completions.create.side_effect = OpenAIError("connection failed")

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )

    with pytest.raises(AzureOpenAIAPIError) as exc_info:
        llm.complete("test")

    assert "connection failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Deadline enforcement
# ---------------------------------------------------------------------------


def test_deadline_raises_agent_timeout(mock_azure_client):
    AzureOpenAILLM, mock_client = mock_azure_client

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
    )
    past_deadline = time.monotonic() - 10

    with pytest.raises(AgentTimeout):
        llm.complete("test", deadline=past_deadline)


# ---------------------------------------------------------------------------
# Authentication validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_a_missing_or_blank_key_is_refused(mock_azure_client, api_key):
    """Azure is reached with the resource's key only; without one nothing is built."""
    AzureOpenAILLM, _ = mock_azure_client

    with pytest.raises(ValueError, match="non-empty api_key"):
        AzureOpenAILLM(
            azure_endpoint="https://test.openai.azure.com/",
            api_key=api_key,
        )


def test_endpoint_and_base_url_together_raises(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    with pytest.raises(ValueError, match="exactly one"):
        AzureOpenAILLM(
            azure_endpoint="https://test.openai.azure.com/",
            base_url="https://test.openai.azure.com/openai/v1/",
            api_key="test-key",
        )


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


def test_proxy_creates_http_client(mock_azure_client):
    AzureOpenAILLM, _ = mock_azure_client

    llm = AzureOpenAILLM(
        azure_endpoint="https://test.openai.azure.com/",
        api_key="test-key",
        proxy_url="http://proxy.corp.local:8080",
    )
    with patch("inqtrix.providers.azure.DefaultAsyncHttpxClient") as mock_httpx_cls:
        call_kwargs = _built_client_kwargs(llm)

    assert call_kwargs["http_client"] is mock_httpx_cls.return_value
    assert mock_httpx_cls.call_args.kwargs["proxy"] == "http://proxy.corp.local:8080"


# ---------------------------------------------------------------------------
# AzureOpenAIAPIError
# ---------------------------------------------------------------------------


def test_azure_openai_api_error_message_format():
    err = AzureOpenAIAPIError(
        model="gpt-4o",
        status_code=404,
        error_code="DeploymentNotFound",
        message="deployment not found",
        request_id="req-abc",
    )
    assert "Azure OpenAI call failed" in str(err)
    assert "gpt-4o" in str(err)
    assert "HTTP 404" in str(err)
    assert "DeploymentNotFound" in str(err)
    assert "request-id=req-abc" in str(err)
    assert "deployment not found" in str(err)


def test_azure_openai_api_error_minimal():
    err = AzureOpenAIAPIError(
        model="gpt-4o-mini",
        message="something went wrong",
    )
    assert "gpt-4o-mini" in str(err)
    assert "something went wrong" in str(err)


# ---------------------------------------------------------------------------
# Reasoning effort (constructor args, validation, mutex, warnings)
# ---------------------------------------------------------------------------


class TestAzureReasoningEffort:
    """Cover constructor-level Azure reasoning-effort validation."""

    def _build_llm(self, mock_azure_client, **kwargs):
        AzureOpenAILLM, mock_client = mock_azure_client
        defaults = dict(
            azure_endpoint="https://test.openai.azure.com/",
            api_key="test-key",
            default_model="gpt-5",
            claim_extract_model="gpt-5-nano",
        )
        defaults.update(kwargs)
        return AzureOpenAILLM(**defaults), mock_client

    def test_default_effort_injected_into_request_params(self, mock_azure_client):
        llm, mock_client = self._build_llm(
            mock_azure_client, default_reasoning_effort="low"
        )
        mock_client.chat.completions.create.return_value = _chat_completion_response()
        llm.complete("Was ist 2+2?")
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["reasoning_effort"] == "low"

    @pytest.mark.parametrize(
        "value", ["none", "minimal", "low", "medium", "high", "xhigh"]
    )
    def test_all_documented_values_accepted(self, mock_azure_client, value):
        llm, _ = self._build_llm(
            mock_azure_client, default_reasoning_effort=value
        )
        assert llm._request_params["reasoning_effort"] == value

    @pytest.mark.parametrize("value", ["foo", "xxhigh", "Medium", "LOW", ""])
    def test_invalid_value_raises(self, mock_azure_client, value):
        AzureOpenAILLM, _ = mock_azure_client
        with pytest.raises(ValueError, match="default_reasoning_effort must be"):
            AzureOpenAILLM(
                azure_endpoint="https://test.openai.azure.com/",
                api_key="test-key",
                default_model="gpt-5",
                default_reasoning_effort=value,
            )

    def test_explicit_arg_wins_over_request_params_dict(self, mock_azure_client):
        llm, _ = self._build_llm(
            mock_azure_client,
            request_params={"reasoning_effort": "high"},
            default_reasoning_effort="low",
        )
        assert llm._request_params["reasoning_effort"] == "low"
        warnings = llm.consume_effort_config_warnings()
        assert any("is overridden by constructor argument" in w for w in warnings)
        assert any("'high'" in w and "'low'" in w for w in warnings)

    def test_request_params_dict_kept_when_constructor_arg_absent(
        self, mock_azure_client
    ):
        llm, _ = self._build_llm(
            mock_azure_client,
            request_params={"reasoning_effort": "high"},
        )
        assert llm._request_params["reasoning_effort"] == "high"
        assert llm.consume_effort_config_warnings() == []

    def test_known_nonreasoning_deployment_warns_softly(self, mock_azure_client):
        llm, _ = self._build_llm(
            mock_azure_client,
            default_model="my-gpt4o-prod",
            claim_extract_model="my-gpt4o-prod",
            default_reasoning_effort="low",
        )
        warnings = llm.consume_effort_config_warnings()
        assert any(
            "non-reasoning model" in w and "my-gpt4o-prod" in w
            for w in warnings
        )

    def test_unknown_deployment_does_not_warn(self, mock_azure_client):
        llm, _ = self._build_llm(
            mock_azure_client,
            default_model="my-prod-2026",
            claim_extract_model="my-prod-2026",
            default_reasoning_effort="low",
        )
        assert llm.consume_effort_config_warnings() == []

    def test_consume_warnings_drains_buffer(self, mock_azure_client):
        llm, _ = self._build_llm(
            mock_azure_client,
            default_model="my-gpt4o-prod",
            claim_extract_model="my-gpt4o-prod",
            default_reasoning_effort="low",
        )
        first = llm.consume_effort_config_warnings()
        assert first
        second = llm.consume_effort_config_warnings()
        assert second == []

    def test_none_means_no_injection(self, mock_azure_client):
        llm, mock_client = self._build_llm(mock_azure_client)
        assert "reasoning_effort" not in llm._request_params
        mock_client.chat.completions.create.return_value = _chat_completion_response()
        llm.complete("Hallo")
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "reasoning_effort" not in call_kwargs

    def test_temperature_and_default_effort_mutex_raises(self, mock_azure_client):
        AzureOpenAILLM, _ = mock_azure_client
        with pytest.raises(ValueError, match="mutually exclusive"):
            AzureOpenAILLM(
                azure_endpoint="https://test.openai.azure.com/",
                api_key="test-key",
                default_model="gpt-5",
                temperature=0.7,
                default_reasoning_effort="low",
            )

    def test_temperature_alone_still_works(self, mock_azure_client):
        llm, _ = self._build_llm(mock_azure_client, temperature=0.5)
        assert llm._temperature == 0.5
        assert "reasoning_effort" not in llm._request_params

    def test_effort_none_string_is_passed_through(self, mock_azure_client):
        llm, mock_client = self._build_llm(
            mock_azure_client, default_reasoning_effort="none"
        )
        mock_client.chat.completions.create.return_value = _chat_completion_response()
        llm.complete("Hallo")
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["reasoning_effort"] == "none"
        assert isinstance(call_kwargs["reasoning_effort"], str)

    def test_consume_method_is_duck_typed_for_nodes(self, mock_azure_client):
        llm, _ = self._build_llm(mock_azure_client)
        assert callable(getattr(llm, "consume_effort_config_warnings", None))
