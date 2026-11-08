"""Provider reasoning survives an approval: interrupt, checkpoint, resume.

The kernel parks a run at a tool that needs approval; another worker resumes it
with a freshly built agent over the same checkpointer. The thinking block of the
turn that asked for the tool must reach the next model call byte for byte, and
the checkpointer, with the server's strict type list, must load nothing as
raw data. The answers follow the Claude API documentation and the Bedrock user
guide; no live call checked them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langchain")

from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from inqtrix.agents.checkpointing import blocked_checkpoint_types, checkpoint_serializer
from inqtrix.agents.harness import build_kernel_agent
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.providers.anthropic import AnthropicLLM
from tests.providers import test_native_tool_calls_anthropic as anthropic_wire
from tests.providers import test_native_tool_calls_bedrock as bedrock_wire
from tests.providers import test_openai_responses as responses_wire

QUESTION = {"messages": [{"role": "user", "content": "Was gilt für die Haftung?"}]}
APPROVE = Command(resume={"decisions": [{"type": "approve"}]})


@tool
def lookup(q: str) -> str:
    """Look something up."""
    return f"Fund zu {q}"


def _agent(llm: Any, saver: InMemorySaver) -> Any:
    return build_kernel_agent(
        build_tool_chat_model(llm),
        tools=[lookup],
        system_prompt="You are the research kernel.",
        interrupt_on={"lookup": True},
        checkpointer=saver,
        max_tool_calls=2,
    )


def _park_and_resume(first_llm: Any, second_llm: Any) -> list[tuple[str, str]]:
    """Park at the approval with *first_llm*, resume in a new agent with *second_llm*."""
    saver = InMemorySaver(serde=checkpoint_serializer())
    config = {"configurable": {"thread_id": "roundtrip"}}
    with blocked_checkpoint_types() as blocked:
        updates = list(_agent(first_llm, saver).stream(QUESTION, config=config, stream_mode="updates"))
        assert any("__interrupt__" in update for update in updates)
        list(_agent(second_llm, saver).stream(APPROVE, config=config, stream_mode="updates"))
    return blocked


@pytest.mark.parametrize(
    "block",
    [anthropic_wire.THINKING | {"thinking": "Erst die Quelle prüfen."}, anthropic_wire.REDACTED],
    ids=["thinking", "redacted_thinking"],
)
def test_a_claude_thinking_block_reaches_the_next_call_unchanged_across_an_approval(block: dict[str, Any]) -> None:
    first = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    answer = anthropic_wire._response(block, anthropic_wire._tool_use(name="lookup"))
    anthropic_wire._Wire(first, [answer])
    second = AnthropicLLM(api_key="k", default_model="claude-opus-5-5")
    wire = anthropic_wire._Wire(second, [anthropic_wire._response({"type": "text", "text": "Erledigt."}, stop_reason="end_turn")])

    assert _park_and_resume(first, second) == []

    (request,) = wire.requests
    (assistant,) = [message for message in request["messages"] if message["role"] == "assistant"]
    assert json.dumps(assistant["content"]) == json.dumps(answer["content"])
    (result,) = request["messages"][-1]["content"]
    assert (result["type"], result["tool_use_id"]) == ("tool_result", "toolu_01")
    assert "Fund zu Haftung" in result["content"]


@pytest.mark.parametrize("block", [bedrock_wire.REASONING, bedrock_wire.REDACTED], ids=["reasoning", "redacted"])
def test_a_bedrock_reasoning_block_reaches_the_next_call_unchanged_across_an_approval(
    block: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = bedrock_wire._llm()
    answer = bedrock_wire._response(block, bedrock_wire._tool_use())
    bedrock_wire._Wire(monkeypatch, first, [answer])
    second = bedrock_wire._llm()
    wire = bedrock_wire._Wire(monkeypatch, second, [bedrock_wire._response(bedrock_wire._text("Erledigt."), stop_reason="end_turn")])

    assert _park_and_resume(first, second) == []

    (request,) = wire.requests
    (assistant,) = [message for message in request["messages"] if message["role"] == "assistant"]
    # Redacted reasoning goes back as the bytes it came as.
    assert assistant["content"] == answer["output"]["message"]["content"]
    (result,) = request["messages"][-1]["content"]
    assert result["toolResult"]["toolUseId"] == "tooluse_01"
    assert "Fund zu Haftung" in result["toolResult"]["content"][0]["text"]


def test_the_encrypted_reasoning_of_a_responses_model_reaches_the_next_call_across_an_approval() -> None:
    """GPT-6 on Azure: the reasoning item goes back in front of the call it led to, after the resume."""
    first_client = responses_wire._FakeClient(
        responses=[responses_wire._tool_call_response(name="lookup", arguments='{"q": "Haftung"}')]
    )
    second_client = responses_wire._FakeClient(responses=[responses_wire._text_response("Erledigt.")])

    assert _park_and_resume(responses_wire._azure(first_client), responses_wire._azure(second_client)) == []

    (request,) = second_client.responses_calls
    items = request["input"]
    kinds = [item.get("type") or item.get("role") for item in items]
    assert items[kinds.index("reasoning")] == responses_wire._REASONING
    assert kinds.index("reasoning") < kinds.index("function_call")
    assert items[kinds.index("function_call")]["name"] == "lookup"
    (output,) = [item for item in items if item.get("type") == "function_call_output"]
    assert output["call_id"] == "call_1"
    assert "Fund zu Haftung" in output["output"]

