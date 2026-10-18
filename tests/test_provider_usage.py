"""Provider-reported token totals and optional cache/reasoning details."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openai.types.chat import ChatCompletion

from inqtrix.providers.base import _normalize_completion_response, openai_usage_details


@pytest.mark.parametrize("kind", ["text", "structured", "chat"])
def test_response_duration_keeps_its_positional_slot(kind):
    from inqtrix.providers.base import LLMResponse, StructuredLLMResponse, ChatTurn

    response = {
        "text": LLMResponse("text", 1, 2, "model", "stop", {}, 33, 12.5),
        "structured": StructuredLLMResponse(
            {}, "text", 1, 2, "model", "stop", {}, 33, "schema", 12.5
        ),
        "chat": ChatTurn("text", (), "stop", "model", 1, 2, {}, 12.5),
    }[kind]
    assert response.duration_ms == 12.5
    assert (response.cache_read_tokens, response.cache_creation_tokens, response.reasoning_tokens) == (None, None, None)


def completion_payload():
    return {
        "id": "completion-usage",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "provider_extension": {"nested": ["retained", 42]},
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 7,
            "total_tokens": 27,
            "prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _assert_traced_native_text(llm, state, *, input_tokens, reasoning_tokens):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.observability.provider_tracing import instrument_llm

    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    wrapped = instrument_llm(llm, provider_name="test", policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=32768), tracer_provider=tracer)
    assert wrapped.complete("test", state=state) == "{}"
    tracer.shutdown()
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    assert json.loads(attrs["inqtrix.response.raw"])["usage"]
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (input_tokens, 7)
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 5
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert attrs.get("gen_ai.usage.reasoning.output_tokens") == reasoning_tokens
    assert (state["total_prompt_tokens"], state["total_completion_tokens"]) == (input_tokens, 7)


@pytest.mark.parametrize("encoding", ["sdk", "dict", "json", "sse"])
def test_completion_details_survive_response_encodings(encoding):
    payload = completion_payload()
    response = {
        "sdk": ChatCompletion.model_validate(payload),
        "dict": payload,
        "json": json.dumps(payload),
        # A later content-only chunk must not erase the final usage block.
        "sse": "data: "
        + json.dumps(payload)
        + '\n\ndata: {"choices": []}\n\ndata: [DONE]\n',
    }[encoding]
    result = _normalize_completion_response(response)
    assert (result.prompt_tokens, result.completion_tokens) == (20, 7)
    assert (
        result.cache_read_tokens,
        result.cache_creation_tokens,
        result.reasoning_tokens,
    ) == (5, 2, 3)
    assert result.raw["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
    if encoding == "sse":
        assert result.raw["response_text"] == response
    else:
        assert result.raw["provider_extension"] == {"nested": ["retained", 42]}


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, None),
        ({}, None),
        ({"prompt_tokens_details": {"cached_tokens": 0}}, 0),
        ({"prompt_tokens_details": {"cached_tokens": 5}}, 5),
    ],
)
def test_usage_absence_is_distinct_from_measured_zero(usage, expected):
    assert openai_usage_details(usage)["cache_read_tokens"] == expected


@pytest.mark.parametrize("value", [-1, True, "7", 1.5])
def test_invalid_usage_detail_is_warned_without_inventing_a_count(value, caplog):
    assert (
        openai_usage_details(
            {"completion_tokens_details": {"reasoning_tokens": value}}
        )["reasoning_tokens"]
        is None
    )
    assert "not a nonnegative integer" in caplog.text


@pytest.mark.parametrize("provider_name", ["azure", "litellm"])
@pytest.mark.parametrize("operation", ["metadata", "structured", "chat", "text"])
def test_openai_provider_paths_preserve_token_subsets(
    provider_name, operation, monkeypatch
):
    client = MagicMock()
    client.chat.completions.create.return_value = ChatCompletion.model_validate(
        completion_payload()
    )
    if provider_name == "azure":
        from inqtrix.providers.azure import AzureOpenAILLM

        monkeypatch.setattr("inqtrix.providers.azure.OpenAI", lambda **_: client)
        llm = AzureOpenAILLM(
            azure_endpoint="https://test.openai.azure.com",
            api_key="test-key",
            default_model="gpt-4o",
        )
    else:
        from inqtrix.providers.litellm import LiteLLM

        llm = LiteLLM(api_key="test-key", default_model="gpt-4o")
        llm._client = client
    state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    if operation == "text":
        _assert_traced_native_text(llm, state, input_tokens=20, reasoning_tokens=3)
        assert client.chat.completions.create.call_count == 1
        return
    if operation == "chat":
        result = llm.chat([{"role": "user", "content": "test"}])
    elif operation == "structured":
        result = llm.complete_structured(
            "test",
            schema={"type": "object", "properties": {}},
            schema_name="test",
            state=state,
        )
    else:
        result = llm.complete_with_metadata("test", state=state)
    assert (result.prompt_tokens, result.completion_tokens) == (20, 7)
    assert (
        result.cache_read_tokens,
        result.cache_creation_tokens,
        result.reasoning_tokens,
    ) == (5, 2, 3)
    assert result.raw["provider_extension"] == {"nested": ["retained", 42]}
    if operation != "chat":
        assert (state["total_prompt_tokens"], state["total_completion_tokens"]) == (
            20,
            7,
        )


@pytest.mark.parametrize("provider_name", ["anthropic", "bedrock"])
@pytest.mark.parametrize("operation", ["metadata", "structured", "text"])
def test_native_input_total_includes_both_cache_buckets(
    provider_name, operation, monkeypatch
):
    if provider_name == "anthropic":
        from inqtrix.providers.anthropic import AnthropicLLM

        llm = AnthropicLLM(api_key="test-key", default_model="claude-sonnet-4-6")
        raw = {
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 2,
            },
        }
        monkeypatch.setattr(llm, "_request_json", lambda **_: raw)
    else:
        from inqtrix.providers.bedrock import BedrockLLM

        monkeypatch.setattr("inqtrix.providers.bedrock.boto3", MagicMock())
        llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")
        raw = {
            "output": {"message": {"content": [{"text": "{}"}]}},
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 11,
                "outputTokens": 7,
                "cacheReadInputTokens": 5,
                "cacheWriteInputTokens": 2,
            },
        }
        monkeypatch.setattr(llm, "_converse_with_retry", lambda **_: raw)
    state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    if operation == "text":
        _assert_traced_native_text(llm, state, input_tokens=18, reasoning_tokens=None)
        return
    if operation == "structured":
        result = llm.complete_structured(
            "test",
            schema={"type": "object", "properties": {}},
            schema_name="test",
            state=state,
        )
    else:
        result = llm.complete_with_metadata("test", state=state)
    assert (result.prompt_tokens, result.completion_tokens) == (18, 7)
    assert (
        result.cache_read_tokens,
        result.cache_creation_tokens,
        result.reasoning_tokens,
    ) == (5, 2, None)
    assert (state["total_prompt_tokens"], state["total_completion_tokens"]) == (18, 7)


def test_raw_serialization_failure_does_not_break_the_provider_result(caplog):
    from types import SimpleNamespace
    from inqtrix.providers.base import _normalize_completion_response

    def broken_dump():
        raise ValueError("serialization failed")

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="answer"), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        model_dump=broken_dump,
    )
    result = _normalize_completion_response(response)
    assert result.content == "answer"
    assert (result.prompt_tokens, result.completion_tokens) == (3, 1)
    assert not result.raw
    assert "raw response could not be serialized" in caplog.text
