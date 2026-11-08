"""Anthropic tool calling against responses built from the Messages API documentation.

No live call checked these shapes (operator decision: Anthropic is proven offline
only); the responses follow the documented ``tool_use``, ``thinking`` and
``redacted_thinking`` blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from inqtrix.exceptions import (
    AgentRequiredToolCallMissing,
    AgentStructuredOutputError,
    AgentToolArgumentsError,
)
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.base import PROVIDER_STATE_FIELD
from tests._sdk_doubles import async_double

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
THINKING = {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBRgCKkBsig=="}
REDACTED = {"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT"}


def _response(*content: dict[str, Any], stop_reason: str = "tool_use", **extra: Any) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": list(content),
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 120, "output_tokens": 30},
        **extra,
    }


def _tool_use(call_id: str = "toolu_01", name: str = "lookup", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments or {"q": "Haftung"}}


class _Wire:
    """Records each request and answers with the next scripted response."""

    def __init__(self, llm: AnthropicLLM, responses: list[dict[str, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.betas: list[str | None] = []
        self._responses = list(responses)
        llm._arequest_json = async_double(self._request)  # type: ignore[method-assign]

    def _request(self, *, payload: dict[str, Any], timeout: float, deadline: float | None = None, beta: str | None = None) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(payload)))
        self.betas.append(beta)
        return self._responses.pop(0)


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


def test_the_conversation_takes_the_messages_api_shape() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [_response(_tool_use(), _tool_use("toolu_02", arguments={"q": "Vertrag"}))])

    turn = llm.chat(_conversation(), tools=TOOLS)

    request = wire.requests[0]
    assert request["system"] == "You are the research kernel."
    assert request["messages"] == [{"role": "user", "content": "Was gilt für die Haftung?"}]
    assert request["tools"][0] == {
        "name": "lookup",
        "description": "Look something up.",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }
    assert "tool_choice" not in request
    assert [call.id for call in turn.tool_calls] == ["toolu_01", "toolu_02"]
    assert turn.tool_calls[1].arguments == {"q": "Vertrag"}
    assert turn.finish_reason == "tool_calls"
    assert turn.provider_state is None


def test_all_tool_results_of_a_turn_go_back_in_one_user_message() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [
        _response(_tool_use(), _tool_use("toolu_02")),
        _response({"type": "text", "text": "Antwort."}, stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS)

    final = llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "toolu_01", "content": "Ergebnis eins"},
        {"role": "tool", "tool_call_id": "toolu_02", "content": "Ergebnis zwei"},
    ], tools=TOOLS)

    sent = wire.requests[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {"q": "Haftung"}},
        {"type": "tool_use", "id": "toolu_02", "name": "lookup", "input": {"q": "Haftung"}},
    ]}
    assert sent[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "Ergebnis eins"},
        {"type": "tool_result", "tool_use_id": "toolu_02", "content": "Ergebnis zwei"},
    ]}
    assert final.text == "Antwort."
    assert final.finish_reason == "stop"


@pytest.mark.parametrize("thinking_block", [THINKING, REDACTED], ids=["thinking", "redacted_thinking"])
def test_a_thinking_block_goes_back_unchanged_with_its_tool_turn(thinking_block: dict[str, Any]) -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [
        _response(thinking_block, _tool_use()),
        _response({"type": "text", "text": "Fertig."}, stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS, reasoning_effort="high")
    assert first.provider_state["content"][0] == thinking_block

    llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "toolu_01", "content": "Ergebnis"},
    ], tools=TOOLS, reasoning_effort="high")

    assistant = wire.requests[1]["messages"][1]
    assert json.dumps(assistant["content"]) == json.dumps([thinking_block, _tool_use()])


def test_a_turn_the_kernel_changed_keeps_its_thinking_first() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [_response(THINKING, _tool_use()), _response({"type": "text", "text": "ok"}, stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS)
    changed = _assistant_from(first)
    changed["tool_calls"][0]["id"] = "toolu_01_1"

    llm.chat([*_conversation(), changed, {"role": "tool", "tool_call_id": "toolu_01_1", "content": "x"}], tools=TOOLS)

    assert wire.requests[1]["messages"][1]["content"] == [
        THINKING,
        {"type": "tool_use", "id": "toolu_01_1", "name": "lookup", "input": {"q": "Haftung"}},
    ]


def test_returned_thinking_on_a_prefix_bound_model_asks_to_drop_a_changed_block() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = _Wire(llm, [_response(THINKING, _tool_use()), _response({"type": "text", "text": "ok"}, stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS)

    llm.chat([*_conversation(), _assistant_from(first), {"role": "tool", "tool_call_id": "toolu_01", "content": "x"}], tools=TOOLS)

    assert wire.betas == [None, "thinking-binding-controls-2026-08-01"]
    assert wire.requests[1]["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }


def test_returned_thinking_on_a_model_without_prefix_binding_sends_no_binding() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [_response(THINKING, _tool_use()), _response({"type": "text", "text": "ok"}, stop_reason="end_turn")])
    first = llm.chat(_conversation(), tools=TOOLS, reasoning_effort="high")

    llm.chat([*_conversation(), _assistant_from(first), {"role": "tool", "tool_call_id": "toolu_01", "content": "x"}], tools=TOOLS, reasoning_effort="high")

    assert wire.betas == [None, None]
    assert wire.requests[1]["thinking"] == {"type": "adaptive"}


def test_tool_choice_none_becomes_the_none_choice() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [_response({"type": "text", "text": "ok"}, stop_reason="end_turn")])

    llm.chat(_conversation(), tools=TOOLS, tool_choice="none")

    assert wire.requests[0]["tool_choice"] == {"type": "none"}


def test_a_required_tool_is_forced_where_the_model_allows_it() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [_response(_tool_use(name="submit_result", arguments={"answer": "a"}))])

    llm.chat(_conversation(), tools=TOOLS, tool_choice="required", reasoning_effort="high")

    assert wire.requests[0]["tool_choice"] == {"type": "any"}


def test_a_model_that_rejects_forcing_gets_auto_and_an_instruction() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = _Wire(llm, [_response(_tool_use(name="submit_result", arguments={"answer": "a"}))])

    turn = llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    request = wire.requests[0]
    assert request["tool_choice"] == {"type": "auto"}
    assert request["system"].startswith("You are the research kernel.\n\n")
    assert "lookup, submit_result" in request["system"]
    assert request["messages"] == [{"role": "user", "content": "Was gilt für die Haftung?"}]
    assert turn.tool_calls[0].name == "submit_result"


def test_a_conversation_under_an_obligation_reaches_the_api_append_only() -> None:
    """The prefix of every returned thinking block stays as it was in the next request."""
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = _Wire(llm, [
        _response(THINKING, _tool_use()),
        _response(THINKING, _tool_use(name="submit_result", arguments={"answer": "a"})),
    ])
    first = llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "toolu_01", "content": "Ergebnis"},
    ], tools=TOOLS, tool_choice="required")

    earlier, later = wire.requests
    assert later["system"] == earlier["system"]
    assert later["tools"] == earlier["tools"]
    assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


def test_a_tool_call_cut_by_the_output_limit_never_runs() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    _Wire(llm, [_response(_tool_use(arguments={"q": "Haft"}), stop_reason="max_tokens")])

    with pytest.raises(AgentToolArgumentsError) as raised:
        llm.chat(_conversation(), tools=TOOLS)

    (call,) = raised.value.raw_tool_calls
    assert call["id"] == "toolu_01"
    assert json.loads(call["function"]["arguments"]) == {"q": "Haft"}
    assert "output limit" in raised.value.validation_errors[0]


def test_manual_thinking_never_forces_a_tool() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6", thinking={"type": "enabled", "budget_tokens": 4000})
    wire = _Wire(llm, [_response(_tool_use(name="submit_result"))])

    llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    assert wire.requests[0]["tool_choice"] == {"type": "auto"}


def test_a_text_answer_where_a_call_was_required_raises_for_the_correction() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    _Wire(llm, [_response({"type": "text", "text": "Die Antwort lautet a."}, stop_reason="end_turn")])

    with pytest.raises(AgentRequiredToolCallMissing) as raised:
        llm.chat(_conversation(), tools=TOOLS, tool_choice="required")

    assert raised.value.turn.text == "Die Antwort lautet a."
    assert raised.value.tool_names == ("lookup", "submit_result")


def test_a_declined_request_is_a_visible_error() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    _Wire(llm, [_response(stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"})])

    with pytest.raises(AgentStructuredOutputError, match="cyber"):
        llm.chat(_conversation(), tools=TOOLS)


def test_a_conversation_that_ends_with_the_assistant_is_refused_before_the_call() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-4-6")
    wire = _Wire(llm, [])

    with pytest.raises(ValueError, match="cannot end with an assistant turn"):
        llm.chat([*_conversation(), {"role": "assistant", "content": "Vorweg"}], tools=TOOLS)

    assert wire.requests == []


def test_the_sampling_and_thinking_rules_of_the_model_hold_in_chat() -> None:
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = _Wire(llm, [_response({"type": "text", "text": "ok"}, stop_reason="end_turn")])

    llm.chat(_conversation(), tools=TOOLS, reasoning_effort="none")

    request = wire.requests[0]
    assert "temperature" not in request
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}


def test_the_adapter_offers_tool_calls() -> None:
    assert AnthropicLLM(api_key="k").supports_tool_calls() is True


def test_the_kernel_shows_the_missed_obligation_again_and_takes_the_call() -> None:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage

    from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
    from inqtrix.agents.kernel.model_recovery import KernelModelRecoveryMiddleware

    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = _Wire(llm, [
        _response({"type": "text", "text": "Die Antwort lautet a."}, stop_reason="end_turn"),
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
    assert second[1] == {"role": "assistant", "content": [{"type": "text", "text": "Die Antwort lautet a."}]}
    assert "must end with a call to one of these tools: submit_result" in json.dumps(second[2])
    assert response.result[-1].tool_calls[0]["name"] == "submit_result"
    assert booked == [(120, 30), (120, 30)]


def test_an_interleaved_turn_goes_back_in_the_order_the_model_wrote_it() -> None:
    progress = {"type": "thinking", "thinking": "", "signature": "c2lnLTI="}
    llm = AnthropicLLM(api_key="k", default_model="claude-sonnet-5-5")
    wire = _Wire(llm, [
        _response(THINKING, _tool_use(), progress, _tool_use("toolu_02", arguments={"q": "Frist"})),
        _response({"type": "text", "text": "ok"}, stop_reason="end_turn"),
    ])
    first = llm.chat(_conversation(), tools=TOOLS)

    llm.chat([
        *_conversation(),
        _assistant_from(first),
        {"role": "tool", "tool_call_id": "toolu_01", "content": "a"},
        {"role": "tool", "tool_call_id": "toolu_02", "content": "b"},
    ], tools=TOOLS)

    assert wire.requests[1]["messages"][1]["content"] == [
        THINKING, _tool_use(), progress, _tool_use("toolu_02", arguments={"q": "Frist"}),
    ]


def test_a_traced_turn_that_missed_its_call_still_records_what_the_model_said() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.providers.tracing import TracingLLMProvider

    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    llm = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    _Wire(llm, [_response({"type": "text", "text": "Nur Text."}, stop_reason="end_turn")])
    traced = TracingLLMProvider(
        llm,
        provider_name="anthropic",
        policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
        tracer_provider=tracer,
    )

    with pytest.raises(AgentRequiredToolCallMissing):
        traced.chat(_conversation(), tools=TOOLS, tool_choice="required")

    (span,) = exporter.get_finished_spans()
    assert "Nur Text." in span.attributes["gen_ai.output.messages"]



_DONE = {"type": "text", "text": "ok"}


def _bound_turns(*answers: dict[str, Any], model: str = "claude-opus-5-5") -> list[str | None]:
    """A first turn with thinking, then one turn per answer that sends that thinking back."""
    llm = AnthropicLLM(api_key="k", default_model=model)
    wire = _Wire(llm, [_response(THINKING, _tool_use()), *answers])
    first = asyncio.run(llm.achat(_conversation(), tools=TOOLS))
    follow_up = [*_conversation(), _assistant_from(first), {"role": "tool", "tool_call_id": "toolu_01", "content": "x"}]
    for _ in answers:
        asyncio.run(llm.achat(follow_up, tools=TOOLS))
    return wire.betas


@pytest.fixture()
def fresh_beta_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from inqtrix.providers import anthropic as anthropic_module

    monkeypatch.setattr(anthropic_module, "_BETA_UNSEEN_MODELS", set())


def test_an_answer_without_input_transformations_says_once_the_beta_did_not_arrive(
    fresh_beta_memory: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.anthropic"):
        betas = _bound_turns(_response(_DONE, stop_reason="end_turn"), _response(_DONE, stop_reason="end_turn"))

    assert betas[1:] == ["thinking-binding-controls-2026-08-01"] * 2
    unseen = [record for record in caplog.records if "input_transformations" in record.getMessage()]
    assert len(unseen) == 1
    assert unseen[0].levelno == logging.WARNING
    assert "claude-opus-5-5" in unseen[0].getMessage()


def test_an_empty_input_transformations_proves_the_beta_arrived(
    fresh_beta_memory: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.anthropic"):
        _bound_turns(_response(_DONE, stop_reason="end_turn", input_transformations=[]))

    assert [record.getMessage() for record in caplog.records] == []


def test_dropped_thinking_blocks_are_counted_per_reason(
    fresh_beta_memory: None, caplog: pytest.LogCaptureFixture
) -> None:
    transformations = [{"type": "thinking_dropped", "reason": "prefix_mismatch"}] * 2 + [{"type": "other", "reason": "x"}]

    with caplog.at_level(logging.INFO, logger="inqtrix.providers.anthropic"):
        _bound_turns(_response(_DONE, stop_reason="end_turn", input_transformations=transformations))

    counted = [record.getMessage() for record in caplog.records]
    assert len(counted) == 1
    assert counted[0].endswith("claude-opus-5-5: prefix_mismatch=2")


def test_without_the_beta_nothing_is_checked(fresh_beta_memory: None, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.anthropic"):
        betas = _bound_turns(_response(_DONE, stop_reason="end_turn"), model="claude-opus-4-6")

    assert betas == [None, None]
    assert [record.getMessage() for record in caplog.records] == []
