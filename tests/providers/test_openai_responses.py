"""Responses API for the models whose card names it, behind the Chat Completions seam of both OpenAI adapters.

GPT-6 and GPT-5.6 deployments refuse function tools while they reason over Chat
Completions ("use /v1/responses or set reasoning_effort to 'none'") and take them
with reasoning over the Responses API. Their answers come as ordered output items:
reasoning, messages marked with a ``phase`` (``commentary`` before tool calls,
``final_answer`` for the answer), and function calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import openai
import pytest
from openai.types.chat import ChatCompletion
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from inqtrix.exceptions import AgentRateLimited, AgentToolArgumentsError
from inqtrix.providers import base as provider_base
from inqtrix.providers._openai_responses import (
    OUTPUT_ITEMS_FIELD,
    api_name,
    chat_completion_from_response,
    responses_input,
    responses_request,
    sent_form,
    uses_responses,
)
from inqtrix.providers.azure import AzureOpenAIAPIError, AzureOpenAILLM
from inqtrix.providers.base import PROVIDER_STATE_FIELD, ChatTurn, LLMProvider, redact_thinking
from inqtrix.providers.litellm import LiteLLM

_URL = "https://t.openai.azure.com/openai/v1/responses"
_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Return the current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
_REASONING = {"id": "rs_1", "summary": [], "type": "reasoning", "encrypted_content": "enc-1"}
_SECRET = "REASONING-SENTINEL-5531"


def _usage(prompt: int = 12, completion: int = 7, reasoning: int = 4, cached: int = 2) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=prompt,
        input_tokens_details=InputTokensDetails(cached_tokens=cached),
        output_tokens=completion,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        total_tokens=prompt + completion,
    )


def _message(text: str, *, phase: str | None = None, item_id: str = "msg_1") -> ResponseOutputMessage:
    message = ResponseOutputMessage(id=item_id, role="assistant", status="completed", type="message",
                                    content=[ResponseOutputText(annotations=[], text=text, type="output_text")])
    if phase is not None:
        message = message.model_copy(update={"phase": phase})
    return message


def _call(
    *, arguments: str = '{"city":"Berlin"}', call_id: str = "call_1", item_id: str = "fc_1", name: str = "get_weather"
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(arguments=arguments, call_id=call_id, name=name,
                                    type="function_call", id=item_id, status="completed")


def _response(*output, status: str = "completed", reason: str | None = None, usage=None) -> Response:
    return Response.model_construct(
        id="resp_1", created_at=1.0, model="gpt-6.1-terra-2026-10-26", object="response", status=status,
        usage=_usage() if usage is None else usage, incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=list(output),
    )


def _tool_call_response(*, arguments: str = '{"city":"Berlin"}', name: str = "get_weather") -> Response:
    return _response(ResponseReasoningItem(**_REASONING), _call(arguments=arguments, name=name))


def _text_response(text: str, *, status: str = "completed", reason: str | None = None) -> Response:
    return _response(_message(text), status=status, reason=reason)


def _dumped(item) -> dict:
    return item.model_dump(mode="json", exclude_none=True)


class _FakeClient:
    """Records which API each attempt used; answers from the queued responses or raises queued errors."""

    def __init__(self, *, responses=(), completions=()) -> None:
        self.responses_calls: list[dict] = []
        self.chat_calls: list[dict] = []
        responses = list(responses)
        completions = list(completions)

        async def create_response(**kwargs):
            self.responses_calls.append(kwargs)
            answer = responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def create_completion(**kwargs):
            self.chat_calls.append(kwargs)
            return completions.pop(0)

        self.responses = SimpleNamespace(create=create_response)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create_completion))


def _azure(client: _FakeClient) -> AzureOpenAILLM:
    llm = AzureOpenAILLM(azure_endpoint="https://t.openai.azure.com/", api_key="k", default_model="gpt-6.1-terra")
    llm._async_client = lambda: client  # type: ignore[method-assign]
    return llm


def _assistant(text: str, calls: list[dict], state: dict | None) -> dict:
    message: dict = {"role": "assistant", "content": text, "tool_calls": calls}
    if state is not None:
        message[PROVIDER_STATE_FIELD] = state
    return message


def _kernel_call(call_id: str = "call_1", arguments: dict | None = None) -> dict:
    """A tool call as the kernel writes it back: arguments re-serialized from the parsed object."""
    return {"id": call_id, "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps(arguments or {"city": "Berlin"}, ensure_ascii=False)}}


def test_the_card_names_the_api() -> None:
    assert uses_responses("gpt-6.1-terra")
    assert uses_responses("gpt-6.1-terra-2026-10-26")
    assert uses_responses("openai/gpt-6-luna")
    assert not uses_responses("gpt-5.4")
    assert not uses_responses("gpt-4o")
    assert not uses_responses("claude-opus-4-8")
    assert (api_name("gpt-6-luna"), api_name("gpt-5.4")) == ("responses", "chat_completions")


def test_the_request_asks_what_the_chat_request_asks() -> None:
    request = responses_request(
        {
            "model": "gpt-6.1-terra",
            "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Weather?"}],
            "tools": [_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            "reasoning_effort": "medium",
            "max_completion_tokens": 512,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}, "strict": True}},
            "verbosity": "low",
            "temperature": 0.2,
            "timeout": 30.0,
            "stream": False,
        },
        provider="azure",
    )
    assert request == {
        "model": "gpt-6.1-terra",
        "input": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Weather?"}],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "tools": [{"type": "function", "name": "get_weather", "strict": False,
                   "description": "Return the current weather for a city.",
                   "parameters": _TOOL["function"]["parameters"]}],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 512,
        "text": {"format": {"type": "json_schema", "name": "r", "schema": {"type": "object"}, "strict": True}, "verbosity": "low"},
        "temperature": 0.2,
        "timeout": 30.0,
    }


def test_a_chat_only_parameter_fails_before_the_request_naming_key_and_model() -> None:
    with pytest.raises(ValueError, match=r"'gpt-6\.1-terra'.*seed, stop"):
        responses_request({"model": "gpt-6.1-terra", "messages": [], "stop": ["x"], "seed": 7}, provider="azure")


def test_the_size_estimate_counts_what_goes_out() -> None:
    """Reasoning of another model stays out of the Responses request, so it stays out of the estimate too."""
    other_model = {"role": "assistant", "content": "Luna said so.",
                   PROVIDER_STATE_FIELD: {"provider": "azure", "model": "gpt-6-luna", "content": [dict(_REASONING, encrypted_content="x" * 4000)]}}
    chat = {"model": "gpt-5.4", "messages": [other_model]}
    assert sent_form(chat, provider="azure") is chat
    sent = sent_form({"model": "gpt-6.1-terra", "messages": [other_model]}, provider="azure")
    assert sent["input"] == [{"role": "assistant", "content": "Luna said so."}]
    assert "x" * 100 not in json.dumps(sent)


def test_a_state_with_only_reasoning_goes_back_reasoning_first() -> None:
    """A state that holds only reasoning, or one of another provider or model, falls back to the rebuilt order."""
    assistant = _assistant("", [_kernel_call()], {"provider": "azure", "model": "gpt-6.1-terra", "content": [_REASONING]})
    foreign = {"role": "assistant", "content": "Earlier answer.",
               PROVIDER_STATE_FIELD: {"provider": "anthropic", "model": "claude-opus-5-5", "content": [{"type": "thinking"}]}}
    other_model = {"role": "assistant", "content": "Luna said so.",
                   PROVIDER_STATE_FIELD: {"provider": "azure", "model": "gpt-6-luna", "content": [dict(_REASONING, id="rs_9")]}}
    items = responses_input(
        [
            {"role": "user", "content": [{"type": "text", "text": "Weather?"}, {"type": "image_url", "image_url": {"url": "inqtrix-image:1"}}]},
            assistant,
            {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
            foreign,
            other_model,
        ],
        provider="azure",
        model="gpt-6.1-terra",
    )
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}, {"type": "input_image", "image_url": "inqtrix-image:1"}]},
        _REASONING,
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": '{"city": "Berlin"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "Sunny"},
        {"role": "assistant", "content": "Earlier answer."},
        {"role": "assistant", "content": "Luna said so."},
    ]


def test_the_answer_goes_back_unchanged_while_its_text_and_calls_still_stand() -> None:
    """Order, item ids and the phase of each message go back as the model wrote them."""
    output = [ResponseReasoningItem(**_REASONING), _message("I will check the weather.", phase="commentary"), _call()]
    completion = chat_completion_from_response(_response(*output), url=_URL)
    state = {"provider": "azure", "model": "gpt-6.1-terra", "content": getattr(completion, OUTPUT_ITEMS_FIELD)}
    items = responses_input([_assistant("I will check the weather.", [_kernel_call()], state)], provider="azure", model="gpt-6.1-terra")
    assert items == [_dumped(item) for item in output]
    assert items[1]["phase"] == "commentary"
    assert items[2]["id"] == "fc_1"


def test_a_changed_call_goes_back_reasoning_first_with_the_phase_and_without_ids() -> None:
    output = [ResponseReasoningItem(**_REASONING), _message("I will check the weather.", phase="commentary"), _call()]
    state = {"provider": "azure", "model": "gpt-6.1-terra", "content": [_dumped(item) for item in output]}
    renamed = _assistant("I will check the weather.", [_kernel_call("call_1_2")], state)
    assert responses_input([renamed], provider="azure", model="gpt-6.1-terra") == [
        _REASONING,
        {"role": "assistant", "content": "I will check the weather.", "phase": "commentary"},
        {"type": "function_call", "call_id": "call_1_2", "name": "get_weather", "arguments": '{"city": "Berlin"}'},
    ]
    answered = _assistant("It is sunny.", [], state)
    assert responses_input([answered], provider="azure", model="gpt-6.1-terra")[-1] == {
        "role": "assistant", "content": "It is sunny.", "phase": "final_answer",
    }


def test_no_phase_is_invented_for_a_model_that_never_marked_its_messages() -> None:
    output = [ResponseReasoningItem(**_REASONING), _message("I will check."), _call()]
    state = {"provider": "azure", "model": "gpt-6.1-terra", "content": [_dumped(item) for item in output]}
    items = responses_input([_assistant("I will check.", [_kernel_call("call_9")], state)], provider="azure", model="gpt-6.1-terra")
    assert {"role": "assistant", "content": "I will check."} in items
    assert all("phase" not in item for item in items)


@pytest.mark.parametrize(
    "message",
    [{"role": "function", "content": "x"}, {"role": "user", "content": [{"type": "input_audio", "data": "x"}]}],
)
def test_a_part_or_role_without_a_responses_form_fails_loudly(message: dict) -> None:
    with pytest.raises(ValueError, match="no input form"):
        responses_input([message], provider="azure", model="gpt-6.1-terra")


def test_the_answer_reads_like_a_chat_completion() -> None:
    completion = chat_completion_from_response(_tool_call_response(), url=_URL)
    assert isinstance(completion, ChatCompletion)
    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls[0].id == "call_1"
    assert choice.message.tool_calls[0].function.arguments == '{"city":"Berlin"}'
    assert completion.model == "gpt-6.1-terra-2026-10-26"
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (12, 7)
    assert completion.usage.completion_tokens_details.reasoning_tokens == 4
    assert completion.usage.prompt_tokens_details.cached_tokens == 2
    assert getattr(completion, OUTPUT_ITEMS_FIELD) == [_REASONING, _dumped(_call())]


def test_several_messages_join_with_a_blank_line() -> None:
    completion = chat_completion_from_response(
        _response(_message("Checked.", phase="commentary"), _message("It is sunny.", phase="final_answer", item_id="msg_2")), url=_URL)
    assert completion.choices[0].message.content == "Checked.\n\nIt is sunny."


@pytest.mark.parametrize(
    ("status", "reason", "finish"),
    [("completed", None, "stop"), ("incomplete", "max_output_tokens", "length"), ("incomplete", "content_filter", "content_filter")],
)
def test_the_finish_reason_says_why_the_answer_ended(status: str, reason: str | None, finish: str) -> None:
    completion = chat_completion_from_response(_text_response("Sunny.", status=status, reason=reason), url=_URL)
    assert completion.choices[0].finish_reason == finish
    assert completion.choices[0].message.content == "Sunny."


def test_a_call_cut_off_by_the_output_limit_reports_length_like_chat_completions() -> None:
    cut = _response(_call(arguments='{"city": "Ber'), status="incomplete", reason="max_output_tokens")
    assert chat_completion_from_response(cut, url=_URL).choices[0].finish_reason == "length"


def test_a_count_the_response_does_not_report_stays_absent() -> None:
    """Absence is not a measured zero: no usage block, or one without details, leaves the counts empty."""
    assert chat_completion_from_response(_response(_message("Hi"), usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5)), url=_URL).usage.completion_tokens_details is None
    bare = Response.model_construct(id="r", created_at=0.0, model="m", object="response", status="completed",
                                    output=[_message("Hi")], usage=None)
    completion = chat_completion_from_response(bare, url=_URL)
    assert completion.usage is None
    turn = provider_base.chat_turn_from_openai_response(completion, model="m")
    assert (turn.reasoning_tokens, turn.cache_read_tokens) == (None, None)


@pytest.mark.parametrize(
    ("code", "error", "status"),
    [("server_error", openai.InternalServerError, 500), ("rate_limit_exceeded", openai.RateLimitError, 429),
     ("invalid_prompt", openai.BadRequestError, 400)],
)
def test_a_failed_response_raises_the_status_error_of_its_code(code: str, error: type, status: int) -> None:
    failed = Response.model_construct(id="r", created_at=0.0, model="m", object="response", status="failed",
                                      output=[], error=SimpleNamespace(code=code, message="boom"))
    with pytest.raises(error, match=code) as caught:
        chat_completion_from_response(failed, url=_URL)
    assert caught.value.status_code == status
    assert provider_base._is_retryable_sdk_error(caught.value) is (code == "server_error")


def _failed(code: str) -> Response:
    return Response.model_construct(id="r", created_at=0.0, model="m", object="response", status="failed",
                                    output=[], error=SimpleNamespace(code=code, message="boom"))


def test_a_failed_server_answer_is_retried_like_an_http_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_base, "_retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    client = _FakeClient(responses=[_failed("server_error"), _text_response("Sunny.")])
    turn = asyncio.run(_azure(client).achat([{"role": "user", "content": "Weather?"}], model="gpt-6.1-terra"))
    assert len(client.responses_calls) == 2
    assert turn.text == "Sunny."


def test_a_failed_answer_maps_like_its_http_failure_and_the_log_names_the_api(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_base, "_retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    caplog.set_level(logging.ERROR, logger="inqtrix.providers.azure")
    limited = _FakeClient(responses=[_failed("rate_limit_exceeded") for _ in range(3)])
    with pytest.raises(AgentRateLimited):
        asyncio.run(_azure(limited).achat([{"role": "user", "content": "q"}], model="gpt-6.1-terra"))
    assert len(limited.responses_calls) == 3
    with pytest.raises(AzureOpenAIAPIError) as caught:
        asyncio.run(_azure(_FakeClient(responses=[_failed("invalid_prompt")])).achat([{"role": "user", "content": "q"}], model="gpt-6.1-terra"))
    assert caught.value.status_code == 400
    assert "api=responses" in caplog.text


def test_a_responses_model_goes_through_the_responses_api_and_keeps_its_output() -> None:
    client = _FakeClient(responses=[_tool_call_response(), _text_response("Sunny in Berlin.")])
    llm = _azure(client)
    messages = [{"role": "user", "content": "Weather in Berlin?"}]
    turn = asyncio.run(llm.achat(messages, tools=[_TOOL], model="gpt-6.1-terra", reasoning_effort="medium"))
    assert client.chat_calls == []
    first = client.responses_calls[0]
    assert (first["store"], first["reasoning"]) == (False, {"effort": "medium"})
    assert turn.tool_calls[0].name == "get_weather"
    assert turn.provider_state == {"provider": "azure", "model": "gpt-6.1-terra", "content": [_REASONING, _dumped(_call())]}

    follow_up = [
        *messages,
        _assistant("", [_kernel_call()], turn.provider_state),
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
    ]
    final = asyncio.run(llm.achat(follow_up, tools=[_TOOL], model="gpt-6.1-terra", reasoning_effort="medium"))
    assert client.responses_calls[1]["input"][1:3] == [_REASONING, _dumped(_call())]
    assert final.text == "Sunny in Berlin."


def test_a_rejected_tool_batch_keeps_the_reasoning_that_led_to_it() -> None:
    client = _FakeClient(responses=[_tool_call_response(arguments='{"city": "Ber')])
    with pytest.raises(AgentToolArgumentsError) as caught:
        asyncio.run(_azure(client).achat([{"role": "user", "content": "Weather?"}], tools=[_TOOL], model="gpt-6.1-terra"))
    state = caught.value.turn.provider_state
    assert state is not None and state["content"][0] == _REASONING


def test_a_chat_completions_model_stays_on_chat_completions() -> None:
    completion = ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "gpt-5.4-2026-03-05",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    client = _FakeClient(completions=[completion])
    turn = asyncio.run(_azure(client).achat([{"role": "user", "content": "Hi"}], tools=[_TOOL], model="gpt-5.4"))
    assert client.responses_calls == []
    assert len(client.chat_calls) == 1
    assert turn.provider_state is None


def test_structured_output_on_a_responses_model_uses_the_text_format() -> None:
    client = _FakeClient(responses=[_text_response('{"answer": "yes"}')])
    llm = _azure(client)
    result = asyncio.run(llm.acomplete_structured(
        "Is it sunny?", schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
        schema_name="verdict", model="gpt-6.1-terra",
    ))
    assert client.responses_calls[0]["text"]["format"]["type"] == "json_schema"
    assert result.parsed == {"answer": "yes"}


def test_litellm_routes_by_the_same_card() -> None:
    client = _FakeClient(responses=[_tool_call_response()])
    llm = LiteLLM(api_key="k")
    llm._async_client = lambda: client  # type: ignore[method-assign]
    turn = asyncio.run(llm.achat([{"role": "user", "content": "Weather?"}], tools=[_TOOL], model="openai/gpt-6.1-terra"))
    assert client.chat_calls == []
    assert turn.provider_state["provider"] == "litellm"


class _Provider(LLMProvider):
    def __init__(self, turn: ChatTurn) -> None:
        self._turn = turn

    def complete(self, prompt, **kwargs):  # pragma: no cover - not used here
        raise NotImplementedError

    def summarize_parallel(self, text, **kwargs):  # pragma: no cover - not used here
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> ChatTurn:
        return self._turn


def test_a_traced_responses_turn_shows_that_the_model_thought_never_what() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.providers.tracing import TracingLLMProvider

    reasoning = ResponseReasoningItem(id="rs_1", type="reasoning", encrypted_content=_SECRET,
                                      summary=[{"type": "summary_text", "text": _SECRET}])
    completion = chat_completion_from_response(_response(reasoning, _message("It is sunny.", phase="final_answer")), url=_URL)
    turn = provider_base.chat_turn_from_openai_response(completion, model="gpt-6.1-terra")
    assert _SECRET in json.dumps(turn.raw)
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    traced = TracingLLMProvider(
        _Provider(turn), provider_name="azure",
        policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=65_536), tracer_provider=tracer,
    )

    traced.chat([{"role": "user", "content": "q"}], model="gpt-6.1-terra")

    (span,) = exporter.get_finished_spans()
    shown = json.dumps(dict(span.attributes), default=str)
    assert _SECRET not in shown
    assert "It is sunny." in shown
    assert redact_thinking({"type": "reasoning", "encrypted_content": _SECRET})["redacted"] is True


def test_the_adapter_reports_the_size_of_the_request_it_sends() -> None:
    """Reasoning of another model stays out of the request, so the attempt does not count it."""
    from inqtrix.providers.attempts import bind_attempt_observer, estimate_request_tokens

    class _Seen:
        def __init__(self) -> None:
            self.infos = []

        def before_attempt(self, info):
            self.infos.append(info)

        def after_attempt(self, ticket, outcome) -> None:
            return None

    other_model = {"role": "assistant", "content": "Luna said so.",
                   PROVIDER_STATE_FIELD: {"provider": "azure", "model": "gpt-6-luna", "content": [dict(_REASONING, encrypted_content="x" * 8000)]}}
    messages = [{"role": "user", "content": "Weather?"}, other_model, {"role": "user", "content": "And now?"}]
    seen = _Seen()
    with bind_attempt_observer(seen):
        asyncio.run(_azure(_FakeClient(responses=[_text_response("Sunny.")])).achat(messages, model="gpt-6.1-terra"))
    (info,) = seen.infos
    assert info.estimated_input_tokens < estimate_request_tokens({"messages": messages}) - 1_000


def test_include_and_reasoning_are_merged_and_store_stays_off_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.providers import _openai_rules

    _openai_rules._warned.clear()
    kwargs = {"model": "gpt-6.1-terra", "messages": [], "store": True, "include": ["message.output_text.logprobs"],
              "reasoning": {"summary": "auto"}, "reasoning_effort": "high"}
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers._openai_rules"):
        request = responses_request(kwargs, provider="azure")
        responses_request(kwargs, provider="azure")
    assert request["store"] is False
    assert request["include"] == ["message.output_text.logprobs", "reasoning.encrypted_content"]
    assert request["reasoning"] == {"summary": "auto", "effort": "high"}
    assert len([r for r in caplog.records if "store=true" in r.getMessage()]) == 1
    _openai_rules._warned.clear()


def test_a_failed_answer_names_its_response_id() -> None:
    with pytest.raises(openai.BadRequestError, match="resp_77"):
        chat_completion_from_response(
            Response.model_construct(id="resp_77", created_at=0.0, model="m", object="response", status="failed",
                                     output=[], error=SimpleNamespace(code="invalid_prompt", message="boom")),
            url=_URL,
        )
