"""The provider's state of a turn goes back to that provider, and nowhere else.

Claude's thinking blocks must return unchanged with the turn that produced them.
They ride on the assistant message in ``additional_kwargs``; every transcript,
archive, summary prompt and span leaves them out, and an adapter that cannot use
them drops them before sending.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from inqtrix.agents.kernel.chat_bridge import (
    PROVIDER_STATE_KWARG,
    THREAD_CALL_IDS,
    build_tool_chat_model,
    messages_to_openai,
    provider_tool_choice,
)
from inqtrix.agents.kernel.compaction import InqtrixCompactionMiddleware, summary_request
from inqtrix.agents.kernel.model_recovery import _rejected_messages
from inqtrix.agents.kernel.transcript import public_message_dump
from inqtrix.exceptions import AgentToolArgumentsError
from inqtrix.providers.base import (
    PROVIDER_STATE_FIELD,
    ChatTurn,
    LLMProvider,
    ToolCallRequest,
    redact_thinking,
)
from tests._sdk_doubles import serve_sdk_calls

SECRET = "THINKING-SENTINEL-4471"
STATE = {
    "provider": "anthropic",
    "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": SECRET, "signature": "sig-1"},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
    ],
}


class _Provider(LLMProvider):
    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
        raise AssertionError("the bridge uses chat()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatTurn:  # type: ignore[override]
        self.calls.append({"messages": messages, **kwargs})
        return self._turns.pop(0)


def _tool_turn(call_id: str = "toolu_1", state: dict[str, Any] | None = STATE) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(ToolCallRequest(id=call_id, name="lookup", arguments={"q": "x"}),),
        finish_reason="tool_calls",
        model="claude-opus-5-5",
        provider_state=state,
    )


def test_the_provider_state_rides_on_its_message_and_back_to_the_provider() -> None:
    provider = _Provider([_tool_turn()])
    model = build_tool_chat_model(provider)

    message = model.invoke([HumanMessage(content="question")])

    assert message.additional_kwargs[PROVIDER_STATE_KWARG] == STATE
    (assistant,) = messages_to_openai([message])
    assert assistant[PROVIDER_STATE_FIELD] == STATE


def test_tool_choice_none_reaches_the_provider() -> None:
    assert provider_tool_choice("none") == "none"
    provider = _Provider([ChatTurn(text="Answer.", finish_reason="stop", model="m")])

    build_tool_chat_model(provider).invoke([HumanMessage(content="q")], tool_choice="none")

    assert provider.calls[0]["tool_choice"] == "none"


def test_a_tool_call_id_the_conversation_already_carries_gets_a_new_one() -> None:
    history = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "call_0", "name": "lookup", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="done", tool_call_id="call_0", name="lookup"),
    ]
    provider = _Provider([_tool_turn(call_id="call_0", state=None)])

    message = build_tool_chat_model(provider).invoke(history)

    assert [call["id"] for call in message.tool_calls] == ["call_0_1"]


def test_an_id_the_thread_carries_outside_the_view_gets_a_new_one() -> None:
    """After a compaction the view lacks the evicted turns; the thread's ids come along.

    Answers, canvas targets and child runs key on the tool-call id: a provider
    that repeats ``call_0`` after a compaction would otherwise have a new
    question answered with the old answer.
    """
    view = [HumanMessage(content="Summary of the conversation so far: the user answered.")]
    provider = _Provider([_tool_turn(call_id="call_0", state=None)])

    message = build_tool_chat_model(provider).invoke(view, **{THREAD_CALL_IDS: frozenset({"call_0"})})

    assert [call["id"] for call in message.tool_calls] == ["call_0_1"]


def test_a_model_turn_hands_the_bridge_the_ids_of_the_whole_thread() -> None:
    from types import SimpleNamespace

    from inqtrix.agents.kernel.middleware import KernelModelTurnMiddleware

    class _Request:
        def __init__(self, state: dict[str, Any], model_settings: dict[str, Any]) -> None:
            self.state, self.model_settings = state, model_settings

        def override(self, **changes: Any) -> "_Request":
            clone = _Request(self.state, self.model_settings)
            for name, value in changes.items():
                setattr(clone, name, value)
            return clone

    thread = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "call_0", "name": "ask_user", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="Nur 2025.", tool_call_id="call_0", name="ask_user"),
    ]
    seen: list[Any] = []

    def handler(request: _Request) -> Any:
        seen.append(request.model_settings.get(THREAD_CALL_IDS))
        return SimpleNamespace(result=[AIMessage(content="ok")])

    KernelModelTurnMiddleware().wrap_model_call(_Request({"messages": thread}, {}), handler)

    assert seen == [frozenset({"call_0"})]


def test_a_rejected_batch_takes_ids_new_to_the_thread() -> None:
    turn = _tool_turn(call_id="call_0", state=None)
    error = AgentToolArgumentsError(
        "claude-opus-5-5",
        turn=turn,
        raw_tool_calls=({"id": "call_0", "type": "function", "function": {"name": "lookup", "arguments": "{"}},),
        validation_errors=("not JSON",),
    )

    rejected, result = _rejected_messages(error, used={"call_0"})

    assert [call["id"] for call in rejected.invalid_tool_calls] == ["call_0_1"]
    assert result.tool_call_id == "call_0_1"


def test_a_transcript_record_keeps_everything_but_the_provider_state() -> None:
    message = AIMessage(content="text", additional_kwargs={PROVIDER_STATE_KWARG: STATE, "other": 1})

    dumped = public_message_dump(message)

    assert SECRET not in json.dumps(dumped)
    assert dumped["additional_kwargs"] == {"other": 1}
    assert message.additional_kwargs[PROVIDER_STATE_KWARG] == STATE


def test_no_kernel_module_dumps_a_message_but_through_the_one_serializer() -> None:
    """Each sink of whole messages (transcript, archives) goes through public_message_dump."""
    root = Path(__file__).resolve().parents[2] / "src" / "inqtrix" / "agents" / "kernel"
    offenders: list[str] = []

    class _Finder(ast.NodeVisitor):
        def __init__(self, name: str) -> None:
            self.name = name
            self.function = ""

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            outer, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = outer

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_dump"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "message"
                and self.function != "public_message_dump"
            ):
                offenders.append(f"{self.name}:{node.lineno}")
            self.generic_visit(node)

    for path in sorted(root.glob("*.py")):
        _Finder(path.name).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert offenders == []


def test_the_compaction_archive_keeps_the_provider_state_out() -> None:
    written: list[str] = []
    deps = MagicMock(context_sources_available=True)
    deps.append_private_context_archive.side_effect = (
        lambda title, text, messages, representation: written.append(text) or "src_1"
    )
    message = AIMessage(content="text", additional_kwargs={PROVIDER_STATE_KWARG: STATE})

    InqtrixCompactionMiddleware._archive(deps, (message,))

    assert written and SECRET not in written[0]


def test_the_summary_prompt_never_carries_the_provider_state() -> None:
    message = AIMessage(content="visible text", additional_kwargs={PROVIDER_STATE_KWARG: STATE})

    prompt = summary_request("", (HumanMessage(content="q"), message))

    assert "visible text" in prompt
    assert SECRET not in prompt


def test_a_rejected_tool_turn_keeps_its_provider_state() -> None:
    turn = _tool_turn()
    error = AgentToolArgumentsError(
        "claude-opus-5-5",
        turn=turn,
        raw_tool_calls=({"id": "toolu_1", "type": "function", "function": {"name": "lookup", "arguments": "{"}},),
        validation_errors=("not JSON",),
    )

    rejected = _rejected_messages(error, used=set())[0]

    assert rejected.additional_kwargs[PROVIDER_STATE_KWARG] == STATE


def test_an_openai_compatible_adapter_never_sends_the_provider_state() -> None:
    from inqtrix.providers.azure import AzureOpenAILLM

    llm = AzureOpenAILLM(azure_endpoint="https://test.openai.azure.com/", api_key="k")
    sent: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        sent.update(kwargs)
        raise RuntimeError("stop after capture")

    serve_sdk_calls(llm, MagicMock())
    llm._client.chat.completions.create.side_effect = create
    try:
        llm.chat([{"role": "assistant", "content": "", PROVIDER_STATE_FIELD: STATE}, {"role": "user", "content": "q"}])
    except Exception:
        pass

    assert sent and all(PROVIDER_STATE_FIELD not in message for message in sent["messages"])


def test_a_span_shows_that_the_model_thought_never_what() -> None:
    anthropic_raw = {"content": [
        {"type": "thinking", "thinking": SECRET, "signature": "sig"},
        {"type": "redacted_thinking", "data": SECRET},
        {"type": "text", "text": "visible"},
    ]}
    bedrock_raw = {"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": SECRET, "signature": "sig"}}},
        {"text": "visible"},
    ]}}}
    messages = [{"role": "assistant", "content": "visible", PROVIDER_STATE_FIELD: STATE}]

    shown = json.dumps([redact_thinking(anthropic_raw), redact_thinking(bedrock_raw), redact_thinking(messages)])

    assert SECRET not in shown
    assert "visible" in shown
    assert '"redacted": true' in shown


def test_a_traced_chat_span_keeps_thinking_and_provider_state_out() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.providers.tracing import TracingLLMProvider

    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    raw = {"content": [{"type": "thinking", "thinking": SECRET, "signature": "sig"}, {"type": "text", "text": "ok"}]}
    provider = _Provider([ChatTurn(text="ok", finish_reason="stop", model="m", raw=raw, provider_state=STATE)])
    traced = TracingLLMProvider(
        provider,
        provider_name="anthropic",
        policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
        tracer_provider=tracer,
    )

    traced.chat([{"role": "assistant", "content": "", PROVIDER_STATE_FIELD: STATE}, {"role": "user", "content": "q"}])

    (span,) = exporter.get_finished_spans()
    assert SECRET not in json.dumps(dict(span.attributes), default=str)
    assert "gen_ai.input.messages" in span.attributes
