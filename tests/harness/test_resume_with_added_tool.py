"""A stored run resumes after the kernel gained a tool.

An added tool is no break of the workflow contract only if a run parked under
the old tool set resumes under the new one without an error and without losing
what it had. This parks a run on an approval with its tool call open, rebuilds
the kernel graph with one more tool over the same checkpointer, as another
worker after a deploy would, and resumes it. Rebuilt without the tool the open
call names, the same resume cannot run that call: that is the break the
workflow pins guard against.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from inqtrix.agents.harness import build_kernel_agent
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest


class _ScriptedProvider(LLMProvider):
    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def complete(self, prompt, **kwargs):  # type: ignore[override]
        raise AssertionError("the kernel path uses chat()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model=None) -> bool:
        return True

    def chat(self, messages, *, tools=None, model=None, **kwargs):  # type: ignore[override]
        self.calls.append({"messages": list(messages), "tools": tools})
        return self._turns.pop(0)


def _turn(text: str = "", *calls: ToolCallRequest) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=calls,
        finish_reason="tool_calls" if calls else "stop",
        model="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        raw=None,
    )


@tool
def echo_tool(text: str) -> str:
    """Return the text."""
    return f"echo: {text}"


@tool
def added_tool(query: str) -> str:
    """A tool added after the run parked."""
    return f"added: {query}"


def _kernel(provider: _ScriptedProvider, tools: list, saver: MemorySaver):
    # The production slots: approval gate, tool budget, compaction.
    return build_kernel_agent(
        build_tool_chat_model(provider),
        tools=tools,
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=saver,
        max_tool_calls=5,
        context_keep_messages=20,
    )


def _park(saver: MemorySaver, config: dict) -> list:
    first = _kernel(
        _ScriptedProvider(
            [_turn("", ToolCallRequest(id="call_open", name="echo_tool", arguments={"text": "weiter"}))]
        ),
        [echo_tool],
        saver,
    )
    updates = list(
        first.stream(
            {"messages": [{"role": "user", "content": "Frage"}]},
            config=config,
            stream_mode="updates",
        )
    )
    assert any("__interrupt__" in update for update in updates)
    return list(first.get_state(config).values["messages"])


def _resume(second, config: dict) -> list:
    updates = list(
        second.stream(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            stream_mode="updates",
        )
    )
    assert not any("__interrupt__" in update for update in updates)
    return list(second.get_state(config).values["messages"])


def test_a_run_parked_on_an_open_call_resumes_with_an_added_tool() -> None:
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "added-tool"}}
    parked = _park(saver, config)
    provider = _ScriptedProvider([_turn("Erledigt.")])

    messages = _resume(_kernel(provider, [echo_tool, added_tool], saver), config)

    assert [message.id for message in messages[: len(parked)]] == [
        message.id for message in parked
    ]
    replies = [message for message in messages if message.type == "tool"]
    assert [(reply.tool_call_id, reply.content) for reply in replies] == [
        ("call_open", "echo: weiter")
    ]
    assert isinstance(messages[-1], AIMessage) and messages[-1].content == "Erledigt."
    offered = {entry["function"]["name"] for entry in provider.calls[0]["tools"]}
    assert {"echo_tool", "added_tool"} <= offered


def test_rebuilt_without_the_tool_its_open_call_names_the_run_cannot_run_that_call() -> None:
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "removed-tool"}}
    _park(saver, config)

    messages = _resume(
        _kernel(_ScriptedProvider([_turn("Erledigt.")]), [added_tool], saver), config
    )

    replies = [message for message in messages if message.type == "tool"]
    assert [reply.tool_call_id for reply in replies] == ["call_open"]
    assert replies[0].status == "error"
    assert "echo: weiter" not in str(replies[0].content)
