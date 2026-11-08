"""Edges of the Claude turn shapes that must never pass silently.

A tool call whose input is no object, and a provider state another provider
wrote, used to vanish without a word: the input became ``{}``, the state was
dropped. Both converters (Messages API and Converse) and the OpenAI-compatible
adapters share the rules checked here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from inqtrix.exceptions import AgentToolArgumentsError
from inqtrix.providers import _anthropic_messages, _bedrock_messages
from inqtrix.providers import base as provider_base
from inqtrix.providers.base import PROVIDER_STATE_FIELD, without_provider_state

USAGE: dict[str, Any] = {"model": "claude-opus-5-5", "prompt_tokens": 1, "completion_tokens": 1, "cache": {}}


def _anthropic_turn(tool_input: Any) -> Any:
    payload = {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "search", "input": tool_input}],
    }
    return _anthropic_messages.chat_turn(payload, **USAGE)


def _converse_turn(tool_input: Any) -> Any:
    response = {
        "stopReason": "tool_use",
        "output": {"message": {"content": [{"toolUse": {"toolUseId": "toolu_1", "name": "search", "input": tool_input}}]}},
    }
    return _bedrock_messages.chat_turn(response, **USAGE)


@pytest.mark.parametrize("turn_of", [_anthropic_turn, _converse_turn])
@pytest.mark.parametrize("tool_input", [["Frist"], 42, "not json", '["Frist"]'])
def test_a_tool_input_that_is_no_object_goes_back_to_the_model(turn_of, tool_input: Any) -> None:
    with pytest.raises(AgentToolArgumentsError) as refused:
        turn_of(tool_input)

    (error,) = refused.value.validation_errors
    assert "toolu_1" in error
    assert "expected a JSON object" in error
    (raw_call,) = refused.value.raw_tool_calls
    sent = raw_call["function"]["arguments"]
    assert sent == (tool_input if isinstance(tool_input, str) else json.dumps(tool_input))


@pytest.mark.parametrize("turn_of", [_anthropic_turn, _converse_turn])
@pytest.mark.parametrize(
    ("tool_input", "arguments"),
    [({"q": "Frist"}, {"q": "Frist"}), ('{"q": "Frist"}', {"q": "Frist"}), (None, {}), ("", {})],
)
def test_an_object_or_no_input_becomes_the_arguments(turn_of, tool_input: Any, arguments: dict[str, Any]) -> None:
    (call,) = turn_of(tool_input).tool_calls
    assert call.arguments == arguments


@pytest.fixture()
def fresh_drop_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_base, "_FOREIGN_STATE_DROPS", {})


def _assistant_with_state(provider: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "Ich suche.",
        "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
        PROVIDER_STATE_FIELD: {"provider": provider, "model": "claude-opus-5-5", "content": [{"type": "thinking", "thinking": "geheim", "signature": "c2ln"}]},
    }


def _conversation(provider: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Frage"},
        _assistant_with_state(provider),
        {"role": "tool", "tool_call_id": "toolu_1", "content": "Ergebnis"},
    ]


@pytest.mark.parametrize(
    ("convert", "foreign"),
    [(_anthropic_messages.to_anthropic, "bedrock"), (_bedrock_messages.to_converse, "anthropic")],
)
def test_a_foreign_provider_state_is_not_sent_and_said_once_per_pair(
    convert, foreign: str, fresh_drop_count: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="inqtrix.providers.base"):
        for _ in range(3):
            converted = convert(_conversation(foreign))

    assert "geheim" not in json.dumps(converted, default=str)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    (warning,) = warnings
    assert f"provider state of {foreign}" in warning.getMessage()
    counted = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert counted[-1].endswith("(3 so far).")


def test_an_own_provider_state_goes_back_without_a_word(fresh_drop_count: None, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="inqtrix.providers.base"):
        _, messages, returns_thinking = _anthropic_messages.to_anthropic(_conversation("anthropic"))

    assert returns_thinking
    assert messages[1]["content"][0]["type"] == "thinking"
    assert caplog.records == []


def test_an_openai_compatible_backend_drops_any_state_with_a_warning(
    fresh_drop_count: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.base"):
        sent = without_provider_state(_conversation("anthropic"), provider="azure")

    assert all(PROVIDER_STATE_FIELD not in message for message in sent)
    (warning,) = caplog.records
    assert warning.getMessage().startswith("Did not send the provider state of anthropic to azure")


def _anthropic_calls(*inputs: Any, stop_reason: str = "tool_use") -> Any:
    payload = {
        "stop_reason": stop_reason,
        "content": [
            {"type": "tool_use", "id": f"toolu_{index}", "name": "search", "input": tool_input}
            for index, tool_input in enumerate(inputs, start=1)
        ],
    }
    return _anthropic_messages.chat_turn(payload, **USAGE)


def _converse_calls(*inputs: Any, stop_reason: str = "tool_use") -> Any:
    response = {
        "stopReason": stop_reason,
        "output": {"message": {"content": [
            {"toolUse": {"toolUseId": f"toolu_{index}", "name": "search", "input": tool_input}}
            for index, tool_input in enumerate(inputs, start=1)
        ]}},
    }
    return _bedrock_messages.chat_turn(response, **USAGE)


@pytest.mark.parametrize("calls_of", [_anthropic_calls, _converse_calls])
def test_one_unreadable_input_sends_the_whole_turn_back(calls_of) -> None:
    with pytest.raises(AgentToolArgumentsError) as refused:
        calls_of({"q": "Frist"}, ["Frist"])

    assert [call["id"] for call in refused.value.raw_tool_calls] == ["toolu_1", "toolu_2"]
    (error,) = refused.value.validation_errors
    assert "toolu_2" in error


@pytest.mark.parametrize(
    ("calls_of", "limit"), [(_anthropic_calls, "max_tokens"), (_converse_calls, "max_tokens")]
)
def test_a_call_the_output_limit_cut_gets_the_hint_to_write_less(calls_of, limit: str) -> None:
    with pytest.raises(AgentToolArgumentsError) as refused:
        calls_of('{"q": "Fri', stop_reason=limit)

    (error,) = refused.value.validation_errors
    assert "output limit" in error


def test_each_pair_of_providers_is_said_once(fresh_drop_count: None, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.base"):
        for foreign in ("bedrock", "litellm", "bedrock"):
            _anthropic_messages.to_anthropic(_conversation(foreign))

    said = [record.getMessage() for record in caplog.records]
    assert len(said) == 2
    assert [message.split(" to ")[0].rsplit(" ", 1)[1] for message in said] == ["bedrock", "litellm"]


def test_the_litellm_adapter_sends_no_state_and_says_so(
    fresh_drop_count: None, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx

    from inqtrix.providers.litellm import LiteLLM

    sent: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    llm = LiteLLM(api_key="k", base_url="http://gateway.test/v1", default_model="m")
    llm._client_kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(gateway))

    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.base"):
        llm.chat(_conversation("anthropic"))

    (body,) = sent
    assert all(PROVIDER_STATE_FIELD not in message for message in body["messages"])
    assert "geheim" not in json.dumps(body)
    (warning,) = caplog.records
    assert warning.getMessage().startswith("Did not send the provider state of anthropic to litellm")

