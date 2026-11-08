"""Rules the Claude adapters share: what a request may carry, and when a turn is usable.

The Anthropic and Bedrock adapters serve the same Claude models. What a model
accepts comes from its card (:class:`~inqtrix.model_cards.ClaudeRules`). A model
without a card keeps the behaviour the adapters had before the cards knew it:
``reasoning_effort="none"`` leaves ``thinking`` out, and only the model name
decides whether ``output_config.effort`` is sent.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from typing import Any

from inqtrix.exceptions import AgentToolArgumentsError
from inqtrix.model_cards import ClaudeRules, resolve_model_card
from inqtrix.providers.base import (
    _THINKING_MIN_MAX_TOKENS,
    ChatTurn,
    normalize_reasoning_effort,
    validate_reasoning_effort,
)

log = logging.getLogger(__name__)

CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
"""Effort levels the Messages API accepts in ``output_config.effort``: no ``minimal``,
but ``max``. ``none`` (off) and ``""`` (inherit) are handled separately."""

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
"""Beta that lets a request say what to do with a thinking block whose prefix changed.

The Claude API takes it as the ``anthropic-beta`` header, Bedrock as ``anthropic_beta``
in ``additionalModelRequestFields``."""

_EFFORT_UNSUPPORTED_FRAGMENTS: tuple[str, ...] = ("haiku",)
"""For a model without a card only: Haiku-class models before 5.5 reject effort."""

_warned: set[tuple[str, str, str]] = set()
_warned_lock = threading.Lock()


def claude_rules(model: str) -> ClaudeRules | None:
    """The request rules of *model*, or ``None`` when no card describes it."""
    card = resolve_model_card(model) if model else None
    return card.claude if card is not None else None


def _model_key(model: str) -> str:
    """One name per model, whatever region or version prefix the id carries."""
    card = resolve_model_card(model) if model else None
    return card.id if card is not None else model


def model_supports_effort(model: str) -> bool:
    """Whether *model* accepts ``output_config.effort``."""
    rules = claude_rules(model)
    if rules is not None:
        return rules.effort
    if not model:
        return True
    lowered = model.lower()
    return not any(fragment in lowered for fragment in _EFFORT_UNSUPPORTED_FRAGMENTS)


def models_rejecting_sampling(models: Iterable[str]) -> list[str]:
    """The configured models that reject ``temperature``, ``top_p`` and ``top_k``."""
    rejecting: list[str] = []
    for model in models:
        rules = claude_rules(model)
        if rules is not None and not rules.sampling and model not in rejecting:
            rejecting.append(model)
    return rejecting


def _first_warning(label: str, model: str, kind: str) -> bool:
    """Whether this warning about this model is the first one of the process."""
    key = (label, _model_key(model), kind)
    with _warned_lock:
        if key in _warned:
            return False
        _warned.add(key)
    return True


def thinking_and_effort(
    reasoning_effort: str | None,
    *,
    model: str,
    inherit_thinking: dict[str, Any] | None,
    inherit_effort: str | None,
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the ``thinking`` config and ``output_config.effort`` of one call.

    ``None`` or ``""`` inherits the adapter's configured thinking and effort.
    ``"none"`` turns thinking off where the model allows it, explicitly where the
    model thinks unless told otherwise; where it cannot be switched off, the call
    runs adaptive thinking at effort ``low`` with one WARNING per model. A graded
    level turns on adaptive thinking with that effort; a model that only takes a
    fixed thinking budget runs without thinking, again with one WARNING.

    Args:
        reasoning_effort: The per-call effort override.
        model: The effective model id of this call.
        inherit_thinking: The adapter's configured thinking config.
        inherit_effort: The adapter's configured effort.
        label: The adapter's name for log lines.

    Returns:
        ``(thinking, effort)``; either may be ``None`` (not sent).
    """
    token = normalize_reasoning_effort(reasoning_effort)
    rules = claude_rules(model)
    if token == "":
        effort = inherit_effort if inherit_effort is not None and model_supports_effort(model) else None
        return inherit_thinking, effort
    if token == "none":
        if rules is None:
            return None, None
        if rules.thinking_off:
            return ({"type": "disabled"} if rules.thinks_by_default else None), None
        if _first_warning(label, model, "thinking_on"):
            log.warning(
                "CONFIG: %s: thinking cannot be switched off on model=%s; "
                "reasoning_effort='none' runs adaptive thinking at effort 'low'.",
                label,
                model,
            )
        return {"type": "adaptive"}, ("low" if rules.effort else None)
    effort, warnings = validate_reasoning_effort(
        token,
        supported_levels=CLAUDE_EFFORT_LEVELS,
        label=f"{label}({model})",
    )
    for warning in warnings:
        log.warning("CONFIG: %s", warning)
    if effort in ("", "none"):
        return None, None
    if rules is not None and rules.thinking == "manual":
        if _first_warning(label, model, "manual_thinking"):
            log.warning(
                "CONFIG: %s: model=%s takes only a fixed thinking budget; "
                "reasoning_effort=%r runs without thinking.",
                label,
                model,
                token,
            )
        return None, None
    if model_supports_effort(model):
        return {"type": "adaptive"}, effort
    log.warning(
        "CONFIG: %s: reasoning_effort=%r requested on model=%s, which does not "
        "accept output_config.effort (Haiku class); using adaptive thinking "
        "without an effort cap.",
        label,
        token,
        model,
    )
    return {"type": "adaptive"}, None


def output_budget(max_tokens: int, thinking: dict[str, Any] | None) -> int:
    """The output budget of a call that sends *thinking*.

    Claude counts thinking tokens inside the output budget, so with thinking on the
    budget rises: an explicit ``budget_tokens`` is honoured, and the
    ``_THINKING_MIN_MAX_TOKENS`` floor keeps neither the hidden reasoning nor the
    visible answer from being cut. Thinking switched off raises nothing.
    """
    if thinking is None or thinking.get("type") == "disabled":
        return max_tokens
    budget = thinking.get("budget_tokens")
    if isinstance(budget, int) and budget >= max_tokens:
        max_tokens = budget + 1024
    if max_tokens < _THINKING_MIN_MAX_TOKENS:
        log.debug(
            "Output budget auto-raised from %d to %d (thinking enabled)",
            max_tokens,
            _THINKING_MIN_MAX_TOKENS,
        )
        max_tokens = _THINKING_MIN_MAX_TOKENS
    return max_tokens


def tool_forcing_allowed(model: str, thinking: dict[str, Any] | None) -> bool:
    """Whether a request may force a tool call.

    Never with manual thinking (``type: enabled``), and never on a model whose card
    forbids it; a model without a card keeps the API's general rule.
    """
    rules = claude_rules(model)
    return (rules is None or rules.forced_tool_choice) and (thinking or {}).get("type") != "enabled"


def with_block_binding(
    thinking: dict[str, Any] | None,
    *,
    model: str,
    returns_thinking: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """``(thinking, beta)`` for a request that sends earlier thinking blocks back.

    A model that checks the prefix of a returned thinking block rejects a block
    whose prefix changed, and the kernel's own compaction changes it. Such a
    request asks the API to drop that block instead; ``beta`` says that
    :data:`THINKING_BINDING_BETA` must go with it. The binding goes only with
    adaptive thinking, the one mode every such model takes it with.
    """
    rules = claude_rules(model)
    if not returns_thinking or rules is None or not rules.prefix_binding:
        return thinking, False
    effective = thinking or ({"type": "adaptive"} if rules.thinks_by_default else None)
    if effective is None or effective.get("type") != "adaptive":
        return thinking, False
    return {**effective, "block_binding": {"prefix_mismatch_behavior": "drop_block"}}, True


def tool_input(raw: Any) -> dict[str, Any] | None:
    """A tool call's input from the answer as its arguments; ``None`` when it is no object.

    Both APIs send an object. A missing input is a call without arguments, and JSON
    text of an object is that object.
    """
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def refuse_unreadable_tool_inputs(turn: ChatTurn, inputs: Iterable[Any]) -> ChatTurn:
    """*turn*, unless the input of one of its tool calls is no JSON object.

    Such a call has no arguments to run. As with invalid JSON on the OpenAI path, no
    call of the turn runs: the kernel shows the batch back to the model for a
    bounded correction.

    Raises:
        AgentToolArgumentsError: A tool call's input is no JSON object.
    """
    pairs = list(zip(turn.tool_calls, inputs))
    errors = tuple(
        f"The input of tool call {call.id} is {type(raw).__name__}; expected a JSON object."
        for call, raw in pairs
        if tool_input(raw) is None
    )
    if not errors:
        return turn
    raise AgentToolArgumentsError(
        turn.model,
        turn=turn,
        raw_tool_calls=tuple(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str),
                },
            }
            for call, raw in pairs
        ),
        validation_errors=errors,
    )


def refuse_cut_tool_calls(turn: ChatTurn) -> ChatTurn:
    """*turn*, unless the output limit stopped it while it wrote tool calls.

    A call written up to the limit may carry cut-off arguments, so no call of such
    a turn runs: the kernel shows the batch back to the model for a bounded
    correction.

    Raises:
        AgentToolArgumentsError: The turn stopped at its output limit with tool calls.
    """
    if turn.finish_reason != "length" or not turn.tool_calls:
        return turn
    raise AgentToolArgumentsError(
        turn.model,
        turn=turn,
        raw_tool_calls=tuple(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ),
        validation_errors=(
            "The answer reached its output limit while writing these tool calls, "
            "so their arguments may be cut off. Write shorter arguments or split "
            "the work into several calls.",
        ),
    )
