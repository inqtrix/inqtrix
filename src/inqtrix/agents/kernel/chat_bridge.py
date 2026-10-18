"""Tool-calling bridge from ``LLMProvider.chat`` to LangChain.

The kernel loop (deepagents) drives a LangChain ``BaseChatModel`` that
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
from inqtrix.exceptions import AgentModelCapacityError, AgentStructuredOutputError, AgentToolArgumentsError, ModelCapacityReason
from inqtrix.providers.base import provider_attempt_timeout
if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

_IMPORT_HINT = (
    "Der Agent-Kernel braucht das 'agent'-Extra (uv sync --extra agent)."
)

KERNEL_MODEL_PROVIDER = "inqtrix"
"""``ls_provider`` the bridge model reports to deepagents."""

KERNEL_MODEL_IDENTIFIER = "kernel"
"""Model identifier the bridge model reports to deepagents.

Together with :data:`KERNEL_MODEL_PROVIDER` this forms the exact
``provider:model`` key the harness registers its kernel profile under
(``inqtrix:kernel``). deepagents otherwise derives the profile key from
the CLASS NAME of a pre-built model — renaming the bridge class would
silently drop the profile (its documented "my profile isn't applying"
failure mode), so the identity is declared explicitly instead."""

UsageHook = Callable[[int, int], None]
"""Receives ``(prompt_tokens, completion_tokens)`` after every
generation — the kernel's per-segment usage accumulator."""

REJECTED_TOOL_ARGUMENTS_FLAG = "inqtrix_rejected_tool_arguments"
"""Server-authored reply metadata: this is model feedback, not a tool execution."""

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


def _record_model_usage(turn):
    totals = _MODEL_USAGE.get()
    if totals is not None:
        totals["prompt_tokens"] += turn.prompt_tokens
        totals["completion_tokens"] += turn.completion_tokens


_FORCED_TOOL_CHOICES = frozenset({"any", "required"})
"""The framework's and the OpenAI-compatible names of ONE obligation: the
turn must call a tool."""


def provider_tool_choice(bound: Any) -> str | None:
    """The provider's ``tool_choice`` for the framework's binding.

    A typed role result (``ToolStrategy``) binds ``"any"`` so the role
    cannot end with prose instead of its result call; the OpenAI-compatible
    wire names the same obligation ``"required"``. No binding sends none,
    so the conversational kernel's turns stay free to answer in text. Any
    other value is refused before the call, never dropped: a dropped
    obligation is exactly how a role could end with its result as text.

    Raises:
        ValueError: The binding is neither absent nor the obligation to
            call a tool.
    """
    if bound is None:
        return None
    if isinstance(bound, str) and bound in _FORCED_TOOL_CHOICES:
        return "required"
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

        @property
        def model_name(self) -> str:
            # deepagents reads this attribute as the model identifier
            # for harness-profile resolution (see KERNEL_MODEL_IDENTIFIER).
            return KERNEL_MODEL_IDENTIFIER

        def _get_ls_params(self, stop: Any = None, **kwargs: Any) -> Any:
            params = super()._get_ls_params(stop=stop, **kwargs)
            params["ls_provider"] = KERNEL_MODEL_PROVIDER
            return params

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
            except AgentToolArgumentsError as exc:
                _record_model_usage(exc.turn)
                if usage_hook is not None:
                    usage_hook(exc.turn.prompt_tokens, exc.turn.completion_tokens)
                raise
            except ContextOverflowError as exc:
                raise AgentModelCapacityError(
                    model or KERNEL_MODEL_IDENTIFIER,
                    "llm_chat",
                    "Provider rejected the input context size.",
                    reason=ModelCapacityReason.CONTEXT_WINDOW,
                    original=exc,
                ) from exc
            _record_model_usage(turn)
            if usage_hook is not None:
                usage_hook(turn.prompt_tokens, turn.completion_tokens)
            if not turn.text.strip() and not turn.tool_calls:
                raise AgentStructuredOutputError(
                    turn.model or model or KERNEL_MODEL_IDENTIFIER,
                    "chat_turn",
                    "Provider returned neither text nor tool calls.",
                )
            message = AIMessage(
                content=turn.text,
                tool_calls=[
                    {
                        "id": call.id,
                        "name": call.name,
                        "args": dict(call.arguments),
                        "type": "tool_call",
                    }
                    for call in turn.tool_calls
                ],
                response_metadata={
                    "finish_reason": turn.finish_reason,
                    "model_name": turn.model,
                },
                # Checkpointed with the message: later segments recover
                # the run-cumulative spend for the token-budget check
                # without a second bookkeeping channel.
                usage_metadata={
                    "input_tokens": turn.prompt_tokens,
                    "output_tokens": turn.completion_tokens,
                    "total_tokens": turn.prompt_tokens
                    + turn.completion_tokens,
                },
            )
            return ChatResult(
                generations=[ChatGeneration(message=message)]
            )

    return _InqtrixToolChatModel()
