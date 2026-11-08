"""The Messages API shape of a tool-calling chat turn, both ways.

The kernel speaks the OpenAI chat shape (system, user, assistant with
``tool_calls``, ``tool`` results). The Claude Messages API wants a top-level
``system``, ``tool_use`` blocks in the assistant turn, and every tool result of a
turn as a ``tool_result`` block in ONE user message. Claude's thinking blocks go
back unchanged and in their order, with the turn that produced them; they travel
in the assistant message's provider state (:data:`PROVIDER_STATE_FIELD`).

Source: Claude API documentation, "Tool use" and "Thinking". A live call has not
checked these shapes.
"""

from __future__ import annotations

import json
from typing import Any

from inqtrix.exceptions import AgentStructuredOutputError
from inqtrix.providers._claude import refuse_cut_tool_calls, refuse_unreadable_tool_inputs, tool_input
from inqtrix.providers.base import PROVIDER_STATE_FIELD, ChatTurn, ToolCallRequest, is_foreign_state

PROVIDER = "anthropic"
"""How this adapter names itself in a provider state."""

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

_FINISH_REASONS = {
    "tool_use": "tool_calls",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
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
    """A tool call's arguments as the object ``tool_use.input`` must be."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _assistant_content(message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The assistant turn's content blocks, and whether it returns thinking blocks.

    The turn goes back exactly as the model wrote it when the stored content still
    names the same tool calls with the same arguments. When the kernel changed the
    calls since (a new id, edited arguments), the thinking blocks go first and the
    text and calls follow as they stand now.
    """
    text = _text(message.get("content"))
    calls = [
        {
            "type": "tool_use",
            "id": str(call.get("id") or ""),
            "name": str((call.get("function") or {}).get("name") or ""),
            "input": _arguments((call.get("function") or {}).get("arguments")),
        }
        for call in message.get("tool_calls") or []
    ]
    text_blocks = [{"type": "text", "text": text}] if text else []
    state = message.get(PROVIDER_STATE_FIELD)
    if not isinstance(state, dict) or is_foreign_state(state, provider=PROVIDER):
        return text_blocks + calls, False
    stored = [block for block in state.get("content") or [] if isinstance(block, dict)]
    stored_calls = [
        {key: block.get(key) for key in ("type", "id", "name", "input")}
        for block in stored
        if block.get("type") == "tool_use"
    ]
    thinking = [block for block in stored if block.get("type") in _THINKING_TYPES]
    if stored_calls == calls:
        return stored, bool(thinking)
    return thinking + text_blocks + calls, bool(thinking)


def to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]], bool]:
    """``(system, messages, returns_thinking)`` for the Messages API.

    Raises:
        ValueError: A system message after the first turn, or a conversation that
            ends with an assistant turn (a prefill, which current models reject).
    """
    system: list[str] = []
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    returns_thinking = False
    leading = True

    def flush_results() -> None:
        if results:
            out.append({"role": "user", "content": list(results)})
            results.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            if not leading:
                raise ValueError("A system message after the first turn has no place in a Messages API request.")
            system.append(_text(message.get("content")))
            continue
        leading = False
        if role == "tool":
            results.append({
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": _text(message.get("content")),
            })
            continue
        flush_results()
        if role == "assistant":
            content, thinking = _assistant_content(message)
            returns_thinking = returns_thinking or thinking
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": "user", "content": _text(message.get("content"))})
    flush_results()
    if out and out[-1]["role"] == "assistant":
        raise ValueError("A Messages API request cannot end with an assistant turn.")
    return ("\n\n".join(part for part in system if part) or None), out, returns_thinking


def to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI function tool as a Messages API tool."""
    function = tool.get("function") or tool
    return {
        "name": str(function.get("name") or ""),
        "description": str(function.get("description") or ""),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


def anthropic_tool_choice(
    tool_choice: str | None,
    *,
    tool_names: list[str],
    forced_allowed: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """``(tool_choice, instruction)`` for the kernel's obligation.

    ``required`` becomes ``{"type": "any"}`` where the model and the request's
    thinking allow forcing a tool. Elsewhere it becomes ``{"type": "auto"}`` plus an
    instruction for the system prompt, and the caller checks that a call came.

    Raises:
        ValueError: An obligation this adapter does not know.
    """
    if tool_choice is None:
        return None, None
    if tool_choice == "none":
        return {"type": "none"}, None
    if tool_choice != "required":
        raise ValueError(f"The Anthropic adapter cannot send tool_choice={tool_choice!r}.")
    if forced_allowed:
        return {"type": "any"}, None
    names = ", ".join(tool_names) or "the available tools"
    return {"type": "auto"}, (
        f"This step must end with a call to one of these tools: {names}. "
        "Give your result through that call, not as text."
    )


def with_instruction(system: str | None, instruction: str) -> str:
    """The system prompt with *instruction* at its end.

    Every turn of a conversation under the same obligation carries the same
    instruction, so the prefix of each thinking block stays as it was. In the last
    user turn it would be gone from the next request, and the API would drop the
    thinking block it produced.
    """
    return f"{system}\n\n{instruction}" if system else instruction


def chat_turn(
    payload: dict[str, Any],
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache: dict[str, int | None],
) -> ChatTurn:
    """The Messages API response as the kernel's turn.

    Raises:
        AgentStructuredOutputError: The model declined the request (``refusal``).
        AgentToolArgumentsError: The output limit stopped the turn while it wrote
            tool calls, or a tool call's input is no JSON object.
    """
    stop_reason = str(payload.get("stop_reason") or "")
    if stop_reason == "refusal":
        details = payload.get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        raise AgentStructuredOutputError(
            model,
            "chat_turn",
            f"The model declined the request (category: {category or 'unknown'}).",
        )
    content = [block for block in payload.get("content") or [] if isinstance(block, dict)]
    texts: list[str] = []
    calls: list[ToolCallRequest] = []
    inputs: list[Any] = []
    thinking = False
    for index, block in enumerate(content):
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            inputs.append(block.get("input"))
            calls.append(ToolCallRequest(
                id=str(block.get("id") or f"toolu_unnamed_{index}"),
                name=str(block.get("name") or ""),
                arguments=tool_input(block.get("input")) or {},
            ))
        elif kind in _THINKING_TYPES:
            thinking = True
    # The output limit first: a call it cut off needs the hint to write less.
    return refuse_unreadable_tool_inputs(refuse_cut_tool_calls(ChatTurn(
        text="".join(texts),
        tool_calls=tuple(calls),
        finish_reason=_FINISH_REASONS.get(stop_reason, stop_reason),
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        raw=payload,
        provider_state=(
            {"provider": PROVIDER, "model": model, "content": json.loads(json.dumps(content))}
            if thinking
            else None
        ),
        cache_read_tokens=cache.get("cache_read_tokens"),
        cache_creation_tokens=cache.get("cache_creation_tokens"),
    )), inputs)
