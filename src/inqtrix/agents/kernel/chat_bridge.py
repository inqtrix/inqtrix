"""Tool-calling bridge from ``LLMProvider.chat`` to LangChain.

The kernel loop (LangChain's agent loop) drives a ``BaseChatModel`` that
must EMIT ``AIMessage.tool_calls``. This bridge translates LangChain
messages to the OpenAI chat shape, calls the provider's native
:meth:`~inqtrix.providers.base.LLMProvider.chat`, and maps the returned
:class:`~inqtrix.providers.base.ChatTurn` back — including tool calls,
whose provider ids pass through verbatim (they are frozen into the
checkpointed message, which is what makes the kernel's deterministic
interrupt ids stable across resume). The binding's ``tool_choice`` reaches
the provider too (see :func:`provider_tool_choice`): a typed role result
depends on it.

Import guard: LangChain lives behind the optional ``agent`` extra, so
it is imported inside :func:`build_tool_chat_model` — building a bridge
model without the extra raises the loud install hint.
"""

from __future__ import annotations

import json
import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRequiredToolCallMissing,
    AgentStructuredOutputError,
    AgentToolArgumentsError,
    ModelCapacityReason,
)
from inqtrix.providers.base import PROVIDER_STATE_FIELD, counted_input_tokens, provider_attempt_timeout
if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

_IMPORT_HINT = (
    "Der Agent-Kernel braucht das 'agent'-Extra (uv sync --extra agent)."
)

KERNEL_MODEL_IDENTIFIER = "kernel"
"""Model name in error reports when neither the call nor the provider names one."""

UsageHook = Callable[[int, int], None]
"""Receives ``(prompt_tokens, completion_tokens)`` after every
generation — the kernel's per-segment usage accumulator."""

REJECTED_TOOL_ARGUMENTS_FLAG = "inqtrix_rejected_tool_arguments"
"""Server-authored reply metadata: this is model feedback, not a tool execution."""

PROVIDER_STATE_KWARG = "inqtrix_provider_state"
"""Where an assistant message keeps the provider's state for the next turn
(``ChatTurn.provider_state``): in ``additional_kwargs``, so ``content`` stays text
for every reader, and out of every transcript and archive (``public_message_dump``)."""

KERNEL_USAGE_STATE = "kernel_usage"
_MODEL_USAGE: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "inqtrix_model_usage", default=None
)


@contextmanager
def collect_model_usage():
    """Collect every generated turn inside one model node, including summaries."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    token = _MODEL_USAGE.set(totals)
    try:
        yield totals
    finally:
        _MODEL_USAGE.reset(token)


def _book_turn(turn, usage_hook: UsageHook | None) -> None:
    """Count one generated turn where a run counts its spend."""
    input_tokens = counted_input_tokens(turn)
    totals = _MODEL_USAGE.get()
    if totals is not None:
        totals["prompt_tokens"] += input_tokens
        totals["completion_tokens"] += turn.completion_tokens
    if usage_hook is not None:
        usage_hook(input_tokens, turn.completion_tokens)


def turn_usage_metadata(turn: Any) -> dict[str, Any]:
    """The usage a checkpointed message keeps: the whole input, as a framework reads it.

    Context compaction needs the full size of what was sent. What a run counts
    against its limits comes from :func:`counted_usage_of_message`.
    """
    usage: dict[str, Any] = {
        "input_tokens": turn.prompt_tokens,
        "output_tokens": turn.completion_tokens,
        "total_tokens": turn.prompt_tokens + turn.completion_tokens,
    }
    if turn.cache_read_tokens:
        usage["input_token_details"] = {"cache_read": turn.cache_read_tokens}
    return usage


def counted_usage_of_message(message: Any) -> tuple[int, int]:
    """Input and output a checkpointed message counts against the limits of its run."""
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    input_tokens = int(meta.get("input_tokens", 0) or 0)
    cache_read_tokens = int(details.get("cache_read", 0) or 0)
    return max(0, input_tokens - cache_read_tokens), int(meta.get("output_tokens", 0) or 0)


_FORCED_TOOL_CHOICES = frozenset({"any", "required"})
"""The framework's and the OpenAI-compatible names of ONE obligation: the
turn must call a tool."""


def provider_tool_choice(bound: Any) -> str | None:
    """The provider's ``tool_choice`` for the framework's binding.

    A typed role result (``ToolStrategy``) binds ``"any"`` so the role
    cannot end with prose instead of its result call; the OpenAI-compatible
    wire names the same obligation ``"required"``. ``"none"`` asks for an
    answer without a tool call. No binding sends none, so the
    conversational kernel's turns stay free to answer in text. Any other
    value is refused before the call, never dropped: a dropped obligation
    is exactly how a role could end with its result as text.

    Raises:
        ValueError: The binding is neither absent, nor the obligation to
            call a tool, nor ``"none"``.
    """
    if bound is None:
        return None
    if isinstance(bound, str) and bound in _FORCED_TOOL_CHOICES:
        return "required"
    if bound == "none":
        return "none"
    raise ValueError(f"The kernel bridge cannot send tool_choice={bound!r}.")


def messages_to_openai(messages: list[Any]) -> list[dict[str, Any]]:
    """Translate LangChain messages to the OpenAI chat-message shape.

    Pure and import-free (works on duck-typed messages) so the mapping
    rules are unit-testable without LangChain: system/human map to their
    roles, an AI message carries its tool calls re-serialized to the
    OpenAI function shape, and a tool message carries its
    ``tool_call_id``. Unknown message types map to ``user`` — visible in
    the transcript rather than silently dropped.
    """
    payload: list[dict[str, Any]] = []
    for message in messages:
        role = getattr(message, "type", "human")
        text = _message_text(message)
        if role == "system":
            payload.append({"role": "system", "content": text})
        elif role == "ai":
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": text or None,
            }
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": str(call.get("id", "")),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name", "")),
                            "arguments": json.dumps(
                                call.get("args", {}) or {},
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for call in tool_calls
                ]
            invalid_calls = getattr(message, "invalid_tool_calls", None) or []
            if invalid_calls:
                entry.setdefault("tool_calls", []).extend(
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": str(call.get("args") or ""),
                        },
                    }
                    for call in invalid_calls
                )
            state = (getattr(message, "additional_kwargs", None) or {}).get(PROVIDER_STATE_KWARG)
            if state:
                entry[PROVIDER_STATE_FIELD] = state
            payload.append(entry)
        elif role == "tool":
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        getattr(message, "tool_call_id", "") or ""
                    ),
                    "content": text,
                }
            )
        else:
            payload.append({"role": "user", "content": text})
    return payload


# The key under which a kernel model turn hands the bridge the tool-call ids of
# the whole thread (``request.model_settings``); the model's view may hold fewer.
THREAD_CALL_IDS = "inqtrix_thread_call_ids"


def used_call_ids(messages: list[Any]) -> set[str]:
    """The tool-call ids the given messages carry."""
    used: set[str] = set()
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            used.add(str(call.get("id") or ""))
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            used.add(str(tool_call_id))
    used.discard("")
    return used


def unique_call_ids(ids: list[str], *, used: set[str]) -> list[str]:
    """The ids of one turn's tool calls, each one new to the run's thread.

    Derived ids (questions to the user, canvas writes, interrupts) key on the
    tool-call id, so a provider that repeats an id (``call_0`` in two turns)
    would make the second call answer as the first. A repeated id gets the
    first free numeric suffix; a new id stays as the provider wrote it.
    *used* must hold the ids of the whole thread, also those a compaction
    moved out of the model's view.
    """
    taken = set(used)
    result: list[str] = []
    for call_id in ids:
        candidate = call_id
        suffix = 1
        while candidate in taken:
            candidate = f"{call_id}_{suffix}"
            suffix += 1
        taken.add(candidate)
        result.append(candidate)
    return result


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def build_tool_chat_model(
    llm: "LLMProvider",
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float = REASONING_TIMEOUT,
    usage_hook: UsageHook | None = None,
) -> Any:
    """Wrap *llm* as a tool-calling LangChain ``BaseChatModel``.

    Args:
        llm: The Inqtrix provider; must pass ``supports_tool_calls()``
            (the kernel's registration gate checks that — this bridge
            surfaces the provider's loud ``NotImplementedError``
            otherwise, never an empty answer).
        model: Optional provider model override (tier resolution result).
        reasoning_effort: Optional effort override, forwarded verbatim.
        timeout: Per-call timeout in seconds.
        usage_hook: Per-generation token callback (segment accounting).

    Raises:
        RuntimeError: LangChain is not installed (missing ``agent``
            extra).
    """
    try:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.exceptions import ContextOverflowError
        from langchain_core.messages import AIMessage, BaseMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.utils.function_calling import (
            convert_to_openai_tool,
        )
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(_IMPORT_HINT) from exc

    class _InqtrixToolChatModel(BaseChatModel):
        """Tool-calling LangChain adapter over one ``LLMProvider``."""

        @property
        def _llm_type(self) -> str:
            return "inqtrix-kernel-tool-bridge"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            formatted = [convert_to_openai_tool(tool) for tool in tools]
            return self.bind(tools=formatted, **kwargs)

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            tool_choice = provider_tool_choice(kwargs.get("tool_choice"))
            try:
                turn = llm.chat(
                    messages_to_openai(list(messages)),
                    tools=kwargs.get("tools") or None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    timeout=provider_attempt_timeout(timeout),
                    **({"tool_choice": tool_choice} if tool_choice else {}),
                )
            except (AgentToolArgumentsError, AgentRequiredToolCallMissing) as exc:
                _book_turn(exc.turn, usage_hook)
                raise
            except ContextOverflowError as exc:
                raise AgentModelCapacityError(
                    model or KERNEL_MODEL_IDENTIFIER,
                    "llm_chat",
                    "Provider rejected the input context size.",
                    reason=ModelCapacityReason.CONTEXT_WINDOW,
                    original=exc,
                ) from exc
            _book_turn(turn, usage_hook)
            if not turn.text.strip() and not turn.tool_calls:
                raise AgentStructuredOutputError(
                    turn.model or model or KERNEL_MODEL_IDENTIFIER,
                    "chat_turn",
                    "Provider returned neither text nor tool calls.",
                )
            call_ids = unique_call_ids(
                [call.id for call in turn.tool_calls],
                used=used_call_ids(messages) | frozenset(kwargs.get(THREAD_CALL_IDS) or ()),
            )
            message = AIMessage(
                content=turn.text,
                tool_calls=[
                    {
                        "id": call_id,
                        "name": call.name,
                        "args": dict(call.arguments),
                        "type": "tool_call",
                    }
                    for call_id, call in zip(call_ids, turn.tool_calls)
                ],
                response_metadata={
                    "finish_reason": turn.finish_reason,
                    "model_name": turn.model,
                },
                # Checkpointed with the message: later segments recover
                # the run-cumulative spend for the token-budget check
                # without a second bookkeeping channel.
                usage_metadata=turn_usage_metadata(turn),
                additional_kwargs=(
                    {PROVIDER_STATE_KWARG: turn.provider_state} if turn.provider_state else {}
                ),
            )
            return ChatResult(
                generations=[ChatGeneration(message=message)]
            )

    return _InqtrixToolChatModel()
