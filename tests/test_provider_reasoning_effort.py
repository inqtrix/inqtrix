"""Per-call ``reasoning_effort`` mapping for the LLM providers.

The wire mapping lives in small per-call helpers
(``AnthropicLLM._call_thinking_and_effort``,
``BedrockLLM._call_thinking_and_effort``,
``AzureOpenAILLM._apply_call_reasoning_effort``). Those are tested directly
(deterministic, no network), plus Anthropic gets wire-payload assertions through
a faked ``_request_json`` to prove the resolved values actually land in the
outgoing request body. LiteLLM forwards normalized values across completion,
structured-output, and tool-calling chat requests.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.base import (
    ConfiguredLLMProvider,
    LLMProvider,
    LLMResponse,
    StructuredLLMResponse,
)
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.providers.litellm import LiteLLM
from inqtrix.settings import ModelSettings

_OPUS = "claude-opus-4-7"
_HAIKU = "claude-haiku-4-5"


# --------------------------------------------------------------------------- #
# ABC contract: every provider accepts the new parameter
# --------------------------------------------------------------------------- #


def test_all_providers_accept_reasoning_effort_parameter() -> None:
    for cls in (AnthropicLLM, BedrockLLM, AzureOpenAILLM, LiteLLM):
        for method in ("complete", "complete_with_metadata"):
            params = inspect.signature(getattr(cls, method)).parameters
            assert "reasoning_effort" in params, f"{cls.__name__}.{method}"


def _litellm_response(content: str) -> MagicMock:
    """Build the minimal SDK response shared by LiteLLM wire tests."""
    message = MagicMock(content=content, tool_calls=None)
    response = MagicMock(
        choices=[MagicMock(message=message, finish_reason="stop")],
        usage=MagicMock(prompt_tokens=1, completion_tokens=1),
    )
    response.model_dump.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return response


@pytest.mark.parametrize(
    ("effort", "expected"),
    [(" HIGH ", "high"), (None, None), ("none", None)],
)
def test_litellm_completion_reasoning_effort_wire_payload(
    monkeypatch, effort: str | None, expected: str | None
) -> None:
    provider = LiteLLM(api_key="k")
    captured: dict[str, object] = {}

    def fake_request(**kwargs):
        captured.update(kwargs["create_kwargs"])
        return _litellm_response("ok")

    monkeypatch.setattr(provider, "_create_chat_completion_with_retry", fake_request)
    provider.complete_with_metadata("q", reasoning_effort=effort)

    assert captured.get("reasoning_effort") == expected


def test_litellm_structured_reasoning_effort_wire_payload(monkeypatch) -> None:
    provider = LiteLLM(api_key="k")
    captured: dict[str, object] = {}

    def fake_request(**kwargs):
        captured.update(kwargs["create_kwargs"])
        return _litellm_response("{}")

    monkeypatch.setattr(provider, "_create_chat_completion_with_retry", fake_request)
    provider.complete_structured(
        "q",
        schema={"type": "object", "properties": {}},
        schema_name="result",
        reasoning_effort="high",
    )

    assert captured["reasoning_effort"] == "high"
    assert captured["response_format"]["type"] == "json_schema"


def test_litellm_chat_reasoning_effort_wire_payload(monkeypatch) -> None:
    provider = LiteLLM(api_key="k")
    captured: dict[str, object] = {}

    def fake_request(**kwargs):
        captured.update(kwargs["create_kwargs"])
        return _litellm_response("ok")

    monkeypatch.setattr(provider, "_create_chat_completion_with_retry", fake_request)
    provider.chat(
        [{"role": "user", "content": "q"}],
        reasoning_effort="high",
    )

    assert captured["reasoning_effort"] == "high"


# --------------------------------------------------------------------------- #
# Anthropic helper
# --------------------------------------------------------------------------- #


def test_anthropic_inherit_uses_constructor_config() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="medium")
    assert llm._call_thinking_and_effort("", use_model=_OPUS) == ({"type": "adaptive"}, "medium")
    assert llm._call_thinking_and_effort(None, use_model=_OPUS) == ({"type": "adaptive"}, "medium")


def test_anthropic_inherit_without_constructor_config_is_off() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)
    assert llm._call_thinking_and_effort("", use_model=_OPUS) == (None, None)


def test_anthropic_none_forces_off_overriding_constructor() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="high")
    assert llm._call_thinking_and_effort("none", use_model=_OPUS) == (None, None)


def test_anthropic_graded_turns_on_adaptive_plus_effort() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)  # no constructor reasoning
    assert llm._call_thinking_and_effort("high", use_model=_OPUS) == ({"type": "adaptive"}, "high")


def test_anthropic_graded_on_haiku_keeps_adaptive_drops_effort() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)
    assert llm._call_thinking_and_effort("high", use_model=_HAIKU) == ({"type": "adaptive"}, None)


def test_anthropic_unknown_level_downgrades_to_off() -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)
    assert llm._call_thinking_and_effort("bogus", use_model=_OPUS) == (None, None)


def test_anthropic_none_effort_forces_off_despite_constructor_defaults() -> None:
    # "none" forces reasoning off for this call regardless of the constructor
    # thinking/effort -- the per-call replacement for the removed suppression.
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="high")
    assert llm._call_thinking_and_effort("none", use_model=_OPUS) == (None, None)
    # Inherit ("") still carries the constructor defaults.
    assert llm._call_thinking_and_effort("", use_model=_OPUS) == ({"type": "adaptive"}, "high")


# --------------------------------------------------------------------------- #
# Anthropic wire payload (faked _request_json)
# --------------------------------------------------------------------------- #


def _fake_text_response():
    return {"content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def _capture_payload(monkeypatch, llm, *, structured: bool = False):
    captured: dict[str, object] = {}

    def fake_request_json(*, payload, timeout, deadline=None):
        captured["payload"] = payload
        if structured:
            return {"content": [{"type": "text", "text": "{}"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        return _fake_text_response()

    monkeypatch.setattr(llm, "_request_json", fake_request_json)
    return captured


def test_anthropic_payload_graded_effort(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)
    captured = _capture_payload(monkeypatch, llm)
    llm.complete_with_metadata("q", reasoning_effort="high")
    assert captured["payload"]["thinking"] == {"type": "adaptive"}
    assert captured["payload"]["output_config"] == {"effort": "high"}


def test_anthropic_payload_none_omits_thinking_and_effort(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="high")
    captured = _capture_payload(monkeypatch, llm)
    llm.complete_with_metadata("q", reasoning_effort="none")
    assert "thinking" not in captured["payload"]
    assert "output_config" not in captured["payload"]


def test_anthropic_payload_unset_inherits_constructor(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="medium")
    captured = _capture_payload(monkeypatch, llm)
    llm.complete_with_metadata("q")  # no reasoning_effort -> inherit
    assert captured["payload"]["thinking"] == {"type": "adaptive"}
    assert captured["payload"]["output_config"] == {"effort": "medium"}


def test_anthropic_payload_haiku_graded_keeps_adaptive_no_effort(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_HAIKU)
    captured = _capture_payload(monkeypatch, llm)
    llm.complete_with_metadata("q", reasoning_effort="high")
    assert captured["payload"]["thinking"] == {"type": "adaptive"}
    assert "output_config" not in captured["payload"]


def test_anthropic_structured_payload_carries_effort(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS)
    captured = _capture_payload(monkeypatch, llm, structured=True)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    llm.complete_structured("q", schema=schema, schema_name="s", reasoning_effort="high")
    assert captured["payload"]["thinking"] == {"type": "adaptive"}
    assert captured["payload"]["output_config"]["effort"] == "high"
    assert captured["payload"]["output_config"]["format"]["type"] == "json_schema"


def test_anthropic_structured_payload_none_omits_effort(monkeypatch) -> None:
    llm = AnthropicLLM(api_key="k", default_model=_OPUS,
                       thinking={"type": "adaptive"}, effort="high")
    captured = _capture_payload(monkeypatch, llm, structured=True)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    llm.complete_structured("q", schema=schema, schema_name="s", reasoning_effort="none")
    assert "thinking" not in captured["payload"]
    assert "effort" not in captured["payload"]["output_config"]


# --------------------------------------------------------------------------- #
# Bedrock helper (mirrors Anthropic)
# --------------------------------------------------------------------------- #

_BEDROCK_OPUS = "eu.anthropic.claude-opus-4-7"
_BEDROCK_HAIKU = "eu.anthropic.claude-haiku-4-5"


def test_bedrock_inherit_without_config_is_off() -> None:
    llm = BedrockLLM(default_model=_BEDROCK_OPUS)
    assert llm._call_thinking_and_effort("", use_model=_BEDROCK_OPUS) == (None, None)


def test_bedrock_graded_turns_on_adaptive_plus_effort() -> None:
    llm = BedrockLLM(default_model=_BEDROCK_OPUS)
    assert llm._call_thinking_and_effort("high", use_model=_BEDROCK_OPUS) == ({"type": "adaptive"}, "high")


def test_bedrock_graded_on_haiku_drops_effort() -> None:
    llm = BedrockLLM(default_model=_BEDROCK_OPUS)
    assert llm._call_thinking_and_effort("high", use_model=_BEDROCK_HAIKU) == ({"type": "adaptive"}, None)


def test_bedrock_none_forces_off() -> None:
    llm = BedrockLLM(default_model=_BEDROCK_OPUS, thinking={"type": "adaptive"}, effort="high")
    assert llm._call_thinking_and_effort("none", use_model=_BEDROCK_OPUS) == (None, None)


# --------------------------------------------------------------------------- #
# Azure helper (mutates request kwargs == the wire payload)
# --------------------------------------------------------------------------- #


def _azure() -> AzureOpenAILLM:
    return AzureOpenAILLM(azure_endpoint="https://t.openai.azure.com/", api_key="k",
                          default_model="gpt-5")


def test_azure_inherit_leaves_kwargs_untouched() -> None:
    az = _azure()
    base = {"model": "gpt-5", "temperature": 0.3}
    assert az._apply_call_reasoning_effort(dict(base), "", use_model="gpt-5") == base


def test_azure_graded_sets_effort_and_drops_temperature() -> None:
    az = _azure()
    out = az._apply_call_reasoning_effort(
        {"model": "gpt-5", "temperature": 0.3}, "high", use_model="gpt-5"
    )
    assert out["reasoning_effort"] == "high"
    assert "temperature" not in out


def test_azure_none_omits_reasoning_effort() -> None:
    az = _azure()
    out = az._apply_call_reasoning_effort(
        {"model": "gpt-5", "reasoning_effort": "medium"}, "none", use_model="gpt-5"
    )
    assert "reasoning_effort" not in out


def test_azure_unknown_level_omits_reasoning_effort() -> None:
    az = _azure()
    out = az._apply_call_reasoning_effort(
        {"model": "gpt-5", "reasoning_effort": "medium"}, "bogus", use_model="gpt-5"
    )
    assert "reasoning_effort" not in out


# --------------------------------------------------------------------------- #
# ConfiguredLLMProvider forwards effort to a wrapped custom provider
# --------------------------------------------------------------------------- #


class _RecordingProvider(LLMProvider):
    """Minimal duck-typed provider that records the kwargs it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def complete(self, prompt: str, **kwargs):
        self.calls.append(("complete", kwargs))
        return "x"

    def complete_with_metadata(self, prompt: str, **kwargs):
        self.calls.append(("complete_with_metadata", kwargs))
        return LLMResponse(content="x")

    def complete_structured(self, prompt: str, **kwargs):
        self.calls.append(("complete_structured", kwargs))
        return StructuredLLMResponse(parsed={}, content="{}", schema_name="s")

    def is_available(self) -> bool:
        return True


def _wrapped() -> tuple[_RecordingProvider, ConfiguredLLMProvider]:
    inner = _RecordingProvider()
    return inner, ConfiguredLLMProvider(inner, ModelSettings(reasoning_model="R"))


def test_configured_wrapper_forwards_effort_when_set() -> None:
    inner, wrapper = _wrapped()
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    wrapper.complete("q", reasoning_effort="high")
    wrapper.complete_with_metadata("q", reasoning_effort="high")
    wrapper.complete_structured("q", schema=schema, schema_name="s", reasoning_effort="high")
    assert [name for name, _ in inner.calls] == [
        "complete", "complete_with_metadata", "complete_structured",
    ]
    for _, kwargs in inner.calls:
        assert kwargs.get("reasoning_effort") == "high"


def test_configured_wrapper_omits_effort_when_empty() -> None:
    inner, wrapper = _wrapped()
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    wrapper.complete("q", reasoning_effort="")
    wrapper.complete_with_metadata("q")
    wrapper.complete_structured("q", schema=schema, schema_name="s", reasoning_effort="")
    for _, kwargs in inner.calls:
        assert "reasoning_effort" not in kwargs
