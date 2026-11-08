"""Request rules of the OpenAI models, read by the OpenAI-compatible adapters.

The card says which API carries a model (:class:`~inqtrix.model_cards.OpenAIRules`)
and which reasoning efforts the model knows. The API decides which of them can be
sent: Chat Completions takes no ``max`` (Azure answers "Supported values are:
'none', 'low', 'medium', 'high', and 'xhigh'"), the Responses API takes every
effort of the card. :func:`effort_to_send` turns a requested effort into the value
a request carries, the same way for every OpenAI-compatible adapter. A model
without OpenAI rules (no card, or another vendor behind an OpenAI-compatible
gateway) keeps the adapter's or gateway's own handling.
"""

from __future__ import annotations

import logging
import threading

from inqtrix.model_cards import OpenAIRules, resolve_model_card
from inqtrix.providers.base import validate_reasoning_effort

log = logging.getLogger(__name__)

CHAT_COMPLETIONS_EFFORTS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh"}
)
"""The efforts the Chat Completions API accepts at all; ``max`` needs the Responses API."""

_warned: set[tuple[str, str, str]] = set()
_warned_lock = threading.Lock()


def openai_rules(model: str) -> OpenAIRules | None:
    """The request rules of *model*, or ``None`` when no OpenAI card describes it."""
    card = resolve_model_card(model) if model else None
    return card.openai if card is not None else None


def reasoning_levels(model: str) -> list[str] | None:
    """The efforts the API of *model* can send, in the card's order.

    ``None`` when *model* has no OpenAI rules: the adapter then adds no limit of
    its own to what the card or the gateway says.
    """
    card = resolve_model_card(model) if model else None
    if card is None or card.openai is None:
        return None
    if card.openai.api == "responses":
        return list(card.reasoning_levels)
    return [level for level in card.reasoning_levels if level in CHAT_COMPLETIONS_EFFORTS]


def effort_to_send(model: str, effort: str, *, label: str) -> str | None:
    """The ``reasoning_effort`` a request of *model* carries for the normalized *effort*; ``None`` for no field.

    For a model with OpenAI rules (:func:`reasoning_levels` is not ``None``):

    - ``none`` goes out explicitly where the card knows it: such a model thinks at
      its own default when the field is missing (GPT-6 and GPT-5.6 at ``medium``).
    - A level the API cannot send becomes the strongest allowed level below it.
    - "Off", or a step down to off, on a model that reasons but cannot be switched
      off runs at its weakest level, because a missing field would run it at its
      default; a model without any reasoning level gets no field.

    Every change against *effort* is logged once per model as a ``CONFIG`` warning.
    """
    levels = reasoning_levels(model) or []
    graded = [level for level in levels if level != "none"]
    warnings: list[str] = []
    if effort != "none":
        effort, warnings = validate_reasoning_effort(effort, supported_levels=tuple(graded), label=f"{label}({model})")
        if effort != "none":
            for warning in warnings:
                warn_once(label, model, warning)
            return effort
    if "none" in levels or not graded:
        for warning in warnings:
            warn_once(label, model, warning)
        return "none" if "none" in levels else None
    weakest = graded[0]
    warn_once(
        label,
        model,
        f"{label}({model}): reasoning cannot be switched off on this model; a call asking for less than "
        f"{weakest!r} runs at {weakest!r}, its weakest level.",
    )
    return weakest


def warn_once(label: str, model: str, message: str) -> None:
    """Log *message* as a WARNING the first time this adapter sees it for *model*."""
    key = (label, model, message)
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning("CONFIG: %s", message)
