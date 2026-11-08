"""The Bedrock Converse shape of a tool-calling chat turn, both ways.

Converse carries the same turn as the Messages API in other words: a list of
``system`` text blocks, ``toolUse`` blocks in the assistant turn, every tool result
of a turn as a ``toolResult`` block in ONE user message, and Claude's thinking as
``reasoningContent`` (``reasoningText`` with a signature, or ``redactedContent``
bytes). Thinking blocks go back unchanged and in their order with the turn that
produced them; in the provider state their bytes are base64 text, because a
checkpoint holds JSON-plain values only.

Source: the botocore 1.42.85 service model of ``bedrock-runtime`` (Converse), the
Bedrock user guide on Claude's preserved thinking and the Claude API documentation
on thinking with tools. A live call has not checked these shapes.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from inqtrix.exceptions import AgentStructuredOutputError
from inqtrix.providers._claude import refuse_cut_tool_calls, refuse_unreadable_tool_inputs, tool_input
from inqtrix.providers.base import PROVIDER_STATE_FIELD, ChatTurn, ToolCallRequest, is_foreign_state

PROVIDER = "bedrock"
"""How this adapter names itself in a provider state."""

_FINISH_REASONS = {
    "tool_use": "tool_calls",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
}
_UNUSABLE = {
    "guardrail_intervened": "a guardrail stopped the answer",
    "content_filtered": "a content filter stopped the answer",
    "malformed_model_output": "the answer could not be read",
    "malformed_tool_use": "a tool call could not be read",
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return "" if content is None else str(content)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _plain(block: dict[str, Any]) -> dict[str, Any]:
    """A Converse content block with its redacted reasoning bytes as base64 text."""
    reasoning = block.get("reasoningContent")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("redactedContent"), (bytes, bytearray)):
        encoded = base64.b64encode(bytes(reasoning["redactedContent"])).decode("ascii")
        return {**block, "reasoningContent": {**reasoning, "redactedContent": encoded}}
    return json.loads(json.dumps(block))


def _wire(block: dict[str, Any]) -> dict[str, Any]:
    """A stored content block as Converse takes it: redacted reasoning back to bytes."""
    reasoning = block.get("reasoningContent")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("redactedContent"), str):
        return {
            **block,
            "reasoningContent": {
                **reasoning,
                "redactedContent": base64.b64decode(reasoning["redactedContent"]),
            },
        }
    return block


def _assistant_content(message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The assistant turn's content blocks, and whether it returns reasoning blocks."""
    text = _text(message.get("content"))
    calls = [
        {"toolUse": {
            "toolUseId": str(call.get("id") or ""),
            "name": str((call.get("function") or {}).get("name") or ""),
            "input": _arguments((call.get("function") or {}).get("arguments")),
        }}
        for call in message.get("tool_calls") or []
    ]
    text_blocks = [{"text": text}] if text else []
    state = message.get(PROVIDER_STATE_FIELD)
    if not isinstance(state, dict) or is_foreign_state(state, provider=PROVIDER):
        return text_blocks + calls, False
    stored = [block for block in state.get("content") or [] if isinstance(block, dict)]
    stored_calls = [
        {"toolUse": {key: block["toolUse"].get(key) for key in ("toolUseId", "name", "input")}}
        for block in stored
        if isinstance(block.get("toolUse"), dict)
    ]
    reasoning = [_wire(block) for block in stored if "reasoningContent" in block]
    if stored_calls == calls:
        return [_wire(block) for block in stored], bool(reasoning)
    return reasoning + text_blocks + calls, bool(reasoning)


def to_converse(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """``(system, messages, returns_reasoning)`` for Converse.

    Raises:
        ValueError: A system message after the first turn, or a conversation that
            ends with an assistant turn.
    """
    system: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    returns_reasoning = False
    leading = True

    def add_user(content: list[dict[str, Any]]) -> None:
        # Converse wants user and assistant turns in turn: a user message
        # right after another one joins it.
        if out and out[-1]["role"] == "user":
            out[-1]["content"].extend(content)
        else:
            out.append({"role": "user", "content": list(content)})

    def flush_results() -> None:
        if results:
            add_user(results)
            results.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            if not leading:
                raise ValueError("A system message after the first turn has no place in a Converse request.")
            text = _text(message.get("content"))
            if text:
                system.append({"text": text})
            continue
        leading = False
        if role == "tool":
            results.append({"toolResult": {
                "toolUseId": str(message.get("tool_call_id") or ""),
                "content": [{"text": _text(message.get("content"))}],
            }})
            continue
        flush_results()
        if role == "assistant":
            content, reasoning = _assistant_content(message)
            returns_reasoning = returns_reasoning or reasoning
            out.append({"role": "assistant", "content": content})
        else:
            add_user([{"text": _text(message.get("content"))}])
    flush_results()
    if out and out[-1]["role"] == "assistant":
        raise ValueError("A Converse request cannot end with an assistant turn.")
    return system, out, returns_reasoning


def to_converse_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI function tool as a Converse tool specification.

    Converse takes no empty description, so a tool without one goes without.
    """
    function = tool.get("function") or tool
    spec: dict[str, Any] = {
        "name": str(function.get("name") or ""),
        "inputSchema": {"json": function.get("parameters") or {"type": "object", "properties": {}}},
    }
    if function.get("description"):
        spec["description"] = str(function["description"])
    return {"toolSpec": spec}


def converse_tool_choice(
    tool_choice: str | None,
    *,
    tool_names: list[str],
    forced_allowed: bool,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """``(toolChoice, instruction, none_as_auto)`` for the kernel's obligation.

    Converse has no ``none``: it becomes ``auto``, and the caller warns once per
    model. ``required`` becomes ``any`` where the model and the request's thinking
    allow forcing a tool, elsewhere ``auto`` plus an instruction for the system
    prompt.

    Raises:
        ValueError: An obligation this adapter does not know.
    """
    if tool_choice is None:
        return None, None, False
    if tool_choice == "none":
        return {"auto": {}}, None, True
    if tool_choice != "required":
        raise ValueError(f"The Bedrock adapter cannot send tool_choice={tool_choice!r}.")
    if forced_allowed:
        return {"any": {}}, None, False
    names = ", ".join(tool_names) or "the available tools"
    return {"auto": {}}, (
        f"This step must end with a call to one of these tools: {names}. "
        "Give your result through that call, not as text."
    ), False


def with_instruction(system: list[dict[str, Any]], instruction: str) -> list[dict[str, Any]]:
    """The system blocks with *instruction* as the last one.

    Every turn under the same obligation carries it, so the prefix of each
    thinking block stays as it was (see the Anthropic twin of this function).
    """
    return [*system, {"text": instruction}]


def chat_turn(
    response: dict[str, Any],
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache: dict[str, int | None],
) -> ChatTurn:
    """The Converse response as the kernel's turn.

    Raises:
        AgentStructuredOutputError: A guardrail or a content filter stopped the
            answer, or Bedrock could not read the answer or one of its tool calls.
        AgentToolArgumentsError: The output limit stopped the turn while it wrote
            tool calls, or a tool call's input is no JSON object.
    """
    stop_reason = str(response.get("stopReason") or "")
    if stop_reason in _UNUSABLE:
        raise AgentStructuredOutputError(
            model,
            "chat_turn",
            f"Bedrock ended the turn without a usable answer: {_UNUSABLE[stop_reason]} ({stop_reason}).",
        )
    output = response.get("output") or {}
    message = output.get("message") or {} if isinstance(output, dict) else {}
    content = [block for block in message.get("content") or [] if isinstance(block, dict)]
    texts: list[str] = []
    calls: list[ToolCallRequest] = []
    inputs: list[Any] = []
    reasoning = False
    for index, block in enumerate(content):
        if isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif isinstance(block.get("toolUse"), dict):
            use = block["toolUse"]
            inputs.append(use.get("input"))
            calls.append(ToolCallRequest(
                id=str(use.get("toolUseId") or f"tooluse_unnamed_{index}"),
                name=str(use.get("name") or ""),
                arguments=tool_input(use.get("input")) or {},
            ))
        elif "reasoningContent" in block:
            reasoning = True
    # The output limit first: a call it cut off needs the hint to write less.
    return refuse_unreadable_tool_inputs(refuse_cut_tool_calls(ChatTurn(
        text="".join(texts),
        tool_calls=tuple(calls),
        finish_reason=_FINISH_REASONS.get(stop_reason, stop_reason),
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw=response,
        provider_state=(
            {"provider": PROVIDER, "model": model, "content": [_plain(block) for block in content]}
            if reasoning
            else None
        ),
        cache_read_tokens=cache.get("cache_read_tokens"),
        cache_creation_tokens=cache.get("cache_creation_tokens"),
    )), inputs)
