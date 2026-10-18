"""Kernel chat bridge contract.

Secures: the LangChain -> OpenAI message translation (roles, tool-call
re-serialization, tool_call_id threading), the ChatTurn -> AIMessage
mapping including native tool calls, tool binding to OpenAI function
schemas, and the per-generation usage hook.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from inqtrix.agents.kernel.chat_bridge import (
    build_tool_chat_model,
    messages_to_openai,
)
from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    """Provider double recording chat() calls and replaying scripted turns."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def complete(self, prompt, **kwargs):  # type: ignore[override]
        raise AssertionError("kernel bridge must use chat(), not complete()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model=None) -> bool:
        return True

    def chat(self, messages, *, tools=None, model=None, **kwargs):  # type: ignore[override]
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "model": model,
                "kwargs": dict(kwargs),
            }
        )
        return self._turns.pop(0)


def _text_turn(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="gpt-test",
        prompt_tokens=21,
        completion_tokens=9,
        raw=None,
    )


def _tool_turn() -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_web1",
                name="web_instant",
                arguments={"query": "EU AI Act Fristen"},
            ),
        ),
        finish_reason="tool_calls",
        model="gpt-test",
        prompt_tokens=33,
        completion_tokens=5,
        raw=None,
    )


def test_messages_to_openai_maps_all_roles():
    payload = messages_to_openai(
        [
            SystemMessage(content="Du bist der Kernel."),
            HumanMessage(content="Frage."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "web_instant",
                        "args": {"query": "x"},
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="Ergebnis.", tool_call_id="call_1"),
            AIMessage(content="Fertig."),
        ]
    )
    assert payload[0] == {"role": "system", "content": "Du bist der Kernel."}
    assert payload[1] == {"role": "user", "content": "Frage."}
    assistant = payload[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "web_instant",
                "arguments": '{"query": "x"}',
            },
        }
    ]
    assert payload[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Ergebnis.",
    }
    assert payload[4] == {"role": "assistant", "content": "Fertig."}


def test_invoke_maps_tool_calls_and_reports_usage():
    provider = _ScriptedProvider([_tool_turn()])
    usage: list[tuple[int, int]] = []
    chat_model = build_tool_chat_model(
        provider,
        model="gpt-pinned",
        usage_hook=lambda p, c: usage.append((p, c)),
    )

    result = chat_model.invoke([HumanMessage(content="Recherchiere.")])

    assert isinstance(result, AIMessage)
    assert result.tool_calls == [
        {
            "id": "call_web1",
            "name": "web_instant",
            "args": {"query": "EU AI Act Fristen"},
            "type": "tool_call",
        }
    ]
    assert result.response_metadata["finish_reason"] == "tool_calls"
    assert usage == [(33, 5)]
    assert provider.calls[0]["model"] == "gpt-pinned"


def test_bind_tools_converts_to_openai_schema_and_forwards():
    def web_instant(query: str) -> str:
        """Fuehrt eine schnelle Websuche aus."""
        return query

    provider = _ScriptedProvider([_text_turn("Antwort.")])
    bound = build_tool_chat_model(provider).bind_tools([web_instant])

    result = bound.invoke([HumanMessage(content="Hi")])

    assert result.content == "Antwort."
    assert result.tool_calls == []
    tools = provider.calls[0]["tools"]
    assert tools is not None and len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "web_instant"
    assert "query" in tools[0]["function"]["parameters"]["properties"]


def test_unbound_invoke_sends_no_tools():
    provider = _ScriptedProvider([_text_turn("Hallo.")])
    build_tool_chat_model(provider).invoke([HumanMessage(content="Hi")])
    assert provider.calls[0]["tools"] is None


def _web_instant(query: str) -> str:
    """Fuehrt eine schnelle Websuche aus."""
    return query


@pytest.mark.parametrize("bound", ["any", "required"])
def test_an_obligation_to_call_a_tool_reaches_the_provider(bound):
    """A typed role result binds ``tool_choice="any"`` (the framework's
    ToolStrategy). The provider must receive that obligation in its own
    vocabulary; without it a role may end with prose instead of its
    typed result."""
    provider = _ScriptedProvider([_tool_turn()])
    bound_model = build_tool_chat_model(provider).bind_tools(
        [_web_instant], tool_choice=bound
    )

    bound_model.invoke([HumanMessage(content="Prüfe.")])

    assert provider.calls[0]["kwargs"]["tool_choice"] == "required"


def test_a_binding_without_an_obligation_sends_none():
    """The conversational kernel binds no ``tool_choice``: its turns stay
    free to answer in text, so nothing is sent."""
    provider = _ScriptedProvider([_text_turn("Antwort."), _text_turn("Hallo.")])
    model = build_tool_chat_model(provider)

    model.bind_tools([_web_instant], tool_choice=None).invoke(
        [HumanMessage(content="Hi")]
    )
    model.bind_tools([_web_instant]).invoke([HumanMessage(content="Hi")])

    assert [call["kwargs"].get("tool_choice", "absent") for call in provider.calls] == [
        "absent",
        "absent",
    ]


def test_an_unknown_tool_choice_is_refused_before_the_provider_call():
    """A binding the bridge cannot express is refused, never dropped."""
    provider = _ScriptedProvider([_text_turn("Antwort.")])
    bound_model = build_tool_chat_model(provider).bind_tools(
        [_web_instant],
        tool_choice={"type": "function", "function": {"name": "web_instant"}},
    )

    with pytest.raises(ValueError, match="tool_choice"):
        bound_model.invoke([HumanMessage(content="Hi")])
    assert provider.calls == []
