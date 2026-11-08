"""Bedrock tool calling through a real Converse client and botocore's own checks.

No live call checked these shapes (operator decision: Bedrock is proven offline
only). Every request goes through a real ``bedrock-runtime`` client, so botocore
validates it against the Converse service model before the ``Stubber`` answers;
the ``Stubber`` validates each scripted answer against the same model.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from typing import Any

import pytest
from botocore.stub import Stubber

from inqtrix.exceptions import (
    AgentRequiredToolCallMissing,
    AgentStructuredOutputError,
    AgentToolArgumentsError,
)
from inqtrix.providers import _claude
from inqtrix.providers.base import PROVIDER_STATE_FIELD
from inqtrix.providers.bedrock import BedrockLLM

OPUS_4_6 = "eu.anthropic.claude-opus-4-6-v1:0"
OPUS_5_5 = "eu.anthropic.claude-opus-5-5-v1:0"
TOOLS = [
    {"type": "function", "function": {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }},
    {"type": "function", "function": {
        "name": "submit_result",
        "description": "Return the result.",
        "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}},
    }},
]
REASONING = {"reasoningContent": {"reasoningText": {"text": "Erst die Quelle.", "signature": "EqQBCkYIBRgCKkBsig=="}}}
REDACTED = {"reasoningContent": {"redactedContent": b"\x01\x02verdeckt"}}


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_claude, "_warned", set())


def _response(*content: dict[str, Any], stop_reason: str = "tool_use") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": list(content)}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
        "metrics": {"latencyMs": 400},
    }


def _tool_use(call_id: str = "tooluse_01", name: str = "lookup", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": call_id, "name": name, "input": arguments or {"q": "Haftung"}}}


def _text(text: str) -> dict[str, Any]:
    return {"text": text}


class _Wire:
    """Records each validated request; the Stubber answers with the next scripted response."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, llm: BedrockLLM, responses: list[dict[str, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        # One client for every deadline rung, so no call leaves the stub.
        monkeypatch.setattr(llm, "_build_client", lambda read_timeout: llm._client)
        llm._clients_by_read_timeout.clear()
        llm._client.meta.events.register(
            "before-parameter-build.bedrock-runtime.Converse",
            lambda params, **_: self.requests.append(copy.deepcopy(params)),
        )
        self.stubber = Stubber(llm._client)
        for response in responses:
            self.stubber.add_response("converse", response)
        self.stubber.activate()


def _llm(model: str = OPUS_4_6, **kwargs: Any) -> BedrockLLM:
    return BedrockLLM(region_name="eu-central-1", default_model=model, **kwargs)


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are the research kernel."},
        {"role": "user", "content": "Was gilt für die Haftung?"},
    ]


def _assistant_from(turn: Any) -> dict[str, Any]:
    """The assistant message the kernel bridge builds from a turn."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": turn.text or None,
        "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
            for call in turn.tool_calls
        ],
    }
    if turn.provider_state:
        message[PROVIDER_STATE_FIELD] = turn.provider_state
    return message


def _after_lookup(first: Any) -> list[dict[str, Any]]:
    return [*_conversation(), _assistant_from(first), {"role": "tool", "tool_call_id": "tooluse_01", "content": "Ergebnis"}]


def test_the_conversation_takes_the_converse_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(_tool_use(), _tool_use("tooluse_02", arguments={"q": "Vertrag"}))])

    turn = llm.chat(_conversation(), tools=TOOLS)

    request = wire.requests[0]
    assert request["system"] == [{"text": "You are the research kernel."}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "Was gilt für die Haftung?"}]}]
    assert request["toolConfig"] == {"tools": [
        {"toolSpec": {
            "name": "lookup",
            "inputSchema": {"json": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
            "description": "Look something up.",
        }},
        {"toolSpec": {
            "name": "submit_result",
            "inputSchema": {"json": {"type": "object", "properties": {"answer": {"type": "string"}}}},
            "description": "Return the result.",
        }},
    ]}
    assert [call.id for call in turn.tool_calls] == ["tooluse_01", "tooluse_02"]
    assert turn.tool_calls[1].arguments == {"q": "Vertrag"}
    assert turn.finish_reason == "tool_calls"
    assert (turn.prompt_tokens, turn.completion_tokens) == (120, 30)
    assert turn.provider_state is None


def test_a_tool_without_description_goes_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(_text("ok"), stop_reason="end_turn")])
    bare = {"type": "function", "function": {"name": "ping", "parameters": {"type": "object", "properties": {}}}}

    llm.chat(_conversation(), tools=[bare])

    assert wire.requests[0]["toolConfig"]["tools"] == [
        {"toolSpec": {"name": "ping", "inputSchema": {"json": {"type": "object", "properties": {}}}}}
    ]


def test_all_tool_results_of_a_turn_go_back_in_one_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [
        _response(_tool_use(), _tool_use("tooluse_02")),
        _response(_text("Antwort."), stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS)

    final = llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "tooluse_01", "content": "Ergebnis eins"},
        {"role": "tool", "tool_call_id": "tooluse_02", "content": "Ergebnis zwei"},
    ], tools=TOOLS)

    sent = wire.requests[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": [_tool_use(), _tool_use("tooluse_02")]}
    assert sent[2] == {"role": "user", "content": [
        {"toolResult": {"toolUseId": "tooluse_01", "content": [{"text": "Ergebnis eins"}]}},
        {"toolResult": {"toolUseId": "tooluse_02", "content": [{"text": "Ergebnis zwei"}]}},
    ]}
    assert final.text == "Antwort."
    assert final.finish_reason == "stop"


def test_the_kernel_s_consecutive_user_messages_go_as_one_converse_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Converse wants user and assistant turns in turn: a skill text after the question and a
    hint after the tool results join the user message before them."""
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [
        _response(_tool_use()),
        _response(_text("Antwort."), stop_reason="end_turn"),
    ])
    first = llm.chat([*_conversation(), {"role": "user", "content": "Skill: Haftung"}], tools=TOOLS)

    llm.chat([
        *_conversation(),
        {"role": "user", "content": "Skill: Haftung"},
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "tooluse_01", "content": "Ergebnis"},
        {"role": "user", "content": "Hinweis: Quelle prüfen."},
    ], tools=TOOLS)

    for request in wire.requests:
        roles = [message["role"] for message in request["messages"]]
        assert all(left != right for left, right in zip(roles, roles[1:])), roles
    sent = wire.requests[1]["messages"]
    assert sent[0]["content"][-1] == {"text": "Skill: Haftung"}
    assert sent[2] == {"role": "user", "content": [
        {"toolResult": {"toolUseId": "tooluse_01", "content": [{"text": "Ergebnis"}]}},
        {"text": "Hinweis: Quelle prüfen."},
    ]}


@pytest.mark.parametrize("reasoning_block", [REASONING, REDACTED], ids=["reasoning_text", "redacted_content"])
def test_a_reasoning_block_goes_back_unchanged_with_its_tool_turn(
    monkeypatch: pytest.MonkeyPatch, reasoning_block: dict[str, Any]
) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [
        _response(reasoning_block, _tool_use()),
        _response(_text("Fertig."), stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS, reasoning_effort="high")
    # A checkpoint holds the state, so it must be plain JSON.
    assert json.loads(json.dumps(first.provider_state)) == first.provider_state

    llm.chat(_after_lookup(first), tools=TOOLS, reasoning_effort="high")

    assert wire.requests[1]["messages"][1]["content"] == [reasoning_block, _tool_use()]


def test_redacted_reasoning_is_base64_text_in_the_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    _Wire(monkeypatch, llm, [_response(REDACTED, _tool_use())])

    turn = llm.chat(_conversation(), tools=TOOLS)

    stored = turn.provider_state["content"][0]["reasoningContent"]["redactedContent"]
    assert base64.b64decode(stored) == b"\x01\x02verdeckt"


def test_a_turn_the_kernel_changed_keeps_its_reasoning_first(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(REASONING, _tool_use()), _response(_text("ok"), stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS)
    changed = _assistant_from(first)
    changed["tool_calls"][0]["id"] = "tooluse_01_1"

    llm.chat([*_conversation(), changed, {"role": "tool", "tool_call_id": "tooluse_01_1", "content": "x"}], tools=TOOLS)

    assert wire.requests[1]["messages"][1]["content"] == [REASONING, _tool_use("tooluse_01_1")]


def test_an_interleaved_turn_goes_back_in_the_order_the_model_wrote_it(monkeypatch: pytest.MonkeyPatch) -> None:
    progress = {"reasoningContent": {"reasoningText": {"text": "Dann die Frist.", "signature": "c2lnLTI="}}}
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [
        _response(REASONING, _tool_use(), progress, _tool_use("tooluse_02", arguments={"q": "Frist"})),
        _response(_text("ok"), stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS)

    llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "tooluse_01", "content": "a"},
        {"role": "tool", "tool_call_id": "tooluse_02", "content": "b"},
    ], tools=TOOLS)

    assert wire.requests[1]["messages"][1]["content"] == [
        REASONING, _tool_use(), progress, _tool_use("tooluse_02", arguments={"q": "Frist"}),
    ]


def test_another_providers_state_never_reaches_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A state goes back only to the adapter that wrote it, whatever its blocks look like."""
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(_text("ok"), stop_reason="end_turn")])
    foreign = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "tooluse_01", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\": \"Haftung\"}"}}],
        PROVIDER_STATE_FIELD: {"provider": "anthropic", "model": "claude-opus-4-6", "content": [REASONING, _tool_use()]},
    }

    llm.chat([*_conversation(), foreign, {"role": "tool", "tool_call_id": "tooluse_01", "content": "x"}], tools=TOOLS)

    assert wire.requests[0]["messages"][1]["content"] == [_tool_use()]


def test_returned_reasoning_on_a_prefix_bound_model_asks_to_drop_a_changed_block(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm(OPUS_5_5)
    wire = _Wire(monkeypatch, llm, [_response(REASONING, _tool_use()), _response(_text("ok"), stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS)

    llm.chat(_after_lookup(first), tools=TOOLS)

    assert "additionalModelRequestFields" not in wire.requests[0]
    assert wire.requests[1]["additionalModelRequestFields"] == {
        "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        "anthropic_beta": ["thinking-binding-controls-2026-08-01"],
    }


def test_returned_reasoning_on_a_model_without_prefix_binding_sends_no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(REASONING, _tool_use()), _response(_text("ok"), stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS, reasoning_effort="high")

    llm.chat(_after_lookup(first), tools=TOOLS, reasoning_effort="high")

    assert wire.requests[1]["additionalModelRequestFields"] == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }


def test_tool_choice_none_becomes_auto_with_one_warning_per_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [
        _response(_text("ok"), stop_reason="end_turn"),
        _response(_text("ok"), stop_reason="end_turn"),
    ])

    with caplog.at_level(logging.WARNING):
        llm.chat(_conversation(), tools=TOOLS, tool_choice="none")
        llm.chat(_conversation(), tools=TOOLS, tool_choice="none")

    assert [request["toolConfig"]["toolChoice"] for request in wire.requests] == [{"auto": {}}, {"auto": {}}]
    assert caplog.text.count("Converse has no tool_choice 'none'") == 1


def test_a_required_tool_is_forced_where_the_model_allows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [_response(_tool_use(name="submit_result", arguments={"answer": "a"}))])

    llm.chat(_conversation(), tools=TOOLS, tool_choice="required", reasoning_effort="high")

    assert wire.requests[0]["toolConfig"]["toolChoice"] == {"any": {}}
    assert wire.requests[0]["system"] == [{"text": "You are the research kernel."}]


def test_a_model_that_rejects_forcing_gets_auto_and_an_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm(OPUS_5_5)
    wire = _Wire(monkeypatch, llm, [_response(_tool_use(name="submit_result", arguments={"answer": "a"}))])

    turn = llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    request = wire.requests[0]
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    first, instruction = request["system"]
    assert first == {"text": "You are the research kernel."}
    assert "lookup, submit_result" in instruction["text"]
    assert request["messages"] == [{"role": "user", "content": [{"text": "Was gilt für die Haftung?"}]}]
    assert turn.tool_calls[0].name == "submit_result"


def test_a_conversation_under_an_obligation_reaches_bedrock_append_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefix of every returned reasoning block stays as it was in the next request."""
    llm = _llm(OPUS_5_5)
    wire = _Wire(monkeypatch, llm, [
        _response(REASONING, _tool_use()),
        _response(REASONING, _tool_use(name="submit_result", arguments={"answer": "a"})),
    ])
    first = llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    llm.chat(_after_lookup(first), tools=TOOLS, tool_choice="required")

    earlier, later = wire.requests
    assert later["system"] == earlier["system"]
    assert later["toolConfig"] == earlier["toolConfig"]
    assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


def test_manual_thinking_never_forces_a_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm(thinking={"type": "enabled", "budget_tokens": 4000})
    wire = _Wire(monkeypatch, llm, [_response(_tool_use(name="submit_result"))])

    llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    assert wire.requests[0]["toolConfig"]["toolChoice"] == {"auto": {}}


def test_a_text_answer_where_a_call_was_required_raises_for_the_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm(OPUS_5_5)
    _Wire(monkeypatch, llm, [_response(_text("Die Antwort lautet a."), stop_reason="end_turn")])

    with pytest.raises(AgentRequiredToolCallMissing) as raised:
        llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    assert raised.value.turn.text == "Die Antwort lautet a."
    assert raised.value.tool_names == ("lookup", "submit_result")


@pytest.mark.parametrize(
    "stop_reason", ["guardrail_intervened", "content_filtered", "malformed_model_output", "malformed_tool_use"]
)
def test_an_answer_bedrock_could_not_use_is_a_visible_error(monkeypatch: pytest.MonkeyPatch, stop_reason: str) -> None:
    llm = _llm()
    _Wire(monkeypatch, llm, [_response(_text("Teil"), stop_reason=stop_reason)])

    with pytest.raises(AgentStructuredOutputError, match=stop_reason):
        llm.chat(_conversation(), tools=TOOLS)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_a_tool_call_cut_by_the_output_limit_never_runs(monkeypatch: pytest.MonkeyPatch, stop_reason: str) -> None:
    llm = _llm()
    _Wire(monkeypatch, llm, [_response(_tool_use(arguments={"q": "Haft"}), stop_reason=stop_reason)])

    with pytest.raises(AgentToolArgumentsError) as raised:
        llm.chat(_conversation(), tools=TOOLS)

    (call,) = raised.value.raw_tool_calls
    assert call["id"] == "tooluse_01"
    assert json.loads(call["function"]["arguments"]) == {"q": "Haft"}
    assert "output limit" in raised.value.validation_errors[0]


def test_a_text_answer_cut_by_the_output_limit_stays_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    _Wire(monkeypatch, llm, [_response(_text("Halbe Antw"), stop_reason="max_tokens")])

    turn = llm.chat(_conversation(), tools=TOOLS)

    assert (turn.text, turn.finish_reason) == ("Halbe Antw", "length")


def test_a_conversation_that_ends_with_the_assistant_is_refused_before_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm()
    wire = _Wire(monkeypatch, llm, [])

    with pytest.raises(ValueError, match="cannot end with an assistant turn"):
        llm.chat([*_conversation(), {"role": "assistant", "content": "Vorweg"}], tools=TOOLS)

    assert wire.requests == []


def test_switching_thinking_off_keeps_the_configured_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _llm("anthropic.claude-opus-5", default_max_tokens=4096)
    wire = _Wire(monkeypatch, llm, [
        _response(_text("ok"), stop_reason="end_turn"),
        _response(_text("ok"), stop_reason="end_turn"),
    ])

    llm.chat(_conversation(), tools=TOOLS, reasoning_effort="none")
    llm.complete("Frage", reasoning_effort="none")

    for request in wire.requests:
        assert request["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
        assert request["inferenceConfig"]["maxTokens"] == 4096


def test_the_adapter_offers_tool_calls_for_claude_models_only() -> None:
    llm = _llm()

    assert llm.supports_tool_calls() is True
    assert llm.supports_tool_calls(model="anthropic.claude-haiku-4-5") is True
    assert llm.supports_tool_calls(model="amazon.nova-pro-v1:0") is False


def test_the_kernel_shows_the_missed_obligation_again_and_takes_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage

    from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
    from inqtrix.agents.kernel.model_recovery import KernelModelRecoveryMiddleware

    llm = _llm(OPUS_5_5)
    wire = _Wire(monkeypatch, llm, [
        _response(_text("Die Antwort lautet a."), stop_reason="end_turn"),
        _response(_tool_use(name="submit_result", arguments={"answer": "a"})),
    ])
    booked: list[tuple[int, int]] = []
    model = build_tool_chat_model(llm, usage_hook=lambda i, o: booked.append((i, o))).bind_tools(
        [TOOLS[1]["function"]], tool_choice="any"
    )
    request = ModelRequest(model=model, messages=[HumanMessage(content="Gib das Ergebnis zurück.")], state={})

    response = KernelModelRecoveryMiddleware().wrap_model_call(
        request, lambda current: ModelResponse(result=[model.invoke(current.messages)])
    )

    second = wire.requests[1]["messages"]
    assert second[1] == {"role": "assistant", "content": [{"text": "Die Antwort lautet a."}]}
    assert "must end with a call to one of these tools: submit_result" in json.dumps(second[2])
    assert response.result[-1].tool_calls[0]["name"] == "submit_result"
    assert booked == [(120, 30), (120, 30)]
