"""One structured-LLM-call helper shared by the agent patterns.

Workspace-agent phases produce Pydantic-validated objects from a single LLM call.
This is the ONE place that does it: native ``complete_structured`` when
the provider supports it, otherwise a prompt-JSON call parsed with a
LOUD fallback marker (the same visible-degradation shape as
``knowledge/gate.py``) — never a silent empty object.

The JSON schema is the canonical ``model_json_schema()`` of the target
Pydantic model — provider-agnostic. Each provider owns adapting it to its
own structured-output contract inside ``complete_structured`` (e.g. the
OpenAI/Azure strict form via
:func:`inqtrix.providers._schema.strictify_json_schema`), so pattern
models may carry defaults freely; this layer encodes no provider quirks.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.observability.context import bind_purpose
from inqtrix.providers.base import observe_provider_retries

log = logging.getLogger("inqtrix")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

STRUCTURED_MARKER_NATIVE = "_structured_native"
STRUCTURED_MARKER_PARSED = "_structured_prompt_parsed"
STRUCTURED_MARKER_FALLBACK = "_structured_fallback"

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_StructuredRetrySink = Callable[[str, dict[str, Any]], None]
_STRUCTURED_RETRY_SINK: contextvars.ContextVar[_StructuredRetrySink | None] = (
    contextvars.ContextVar("inqtrix_structured_retry_sink", default=None)
)


@contextmanager
def observe_structured_retries(
    sink: _StructuredRetrySink | None,
) -> Iterator[None]:
    """Bind one Agent retry sink across all structured phase calls.

    The phase machine owns the event sink while this helper owns the only
    structured-provider call boundary. Keeping the binding in a ContextVar
    preserves the shared compiled graph without adding callback parameters to
    every phase function.
    """
    token = _STRUCTURED_RETRY_SINK.set(sink)
    try:
        yield
    finally:
        _STRUCTURED_RETRY_SINK.reset(token)


@contextmanager
def observe_bound_model_retries(llm: Any, node: str) -> Iterator[None]:
    """Bind the current Agent retry sink at one concrete provider boundary."""
    retry_sink = _STRUCTURED_RETRY_SINK.get()
    retry_callback = (
        (lambda notice: retry_sink(node, notice))
        if retry_sink is not None
        else None
    )
    with observe_provider_retries(llm, retry_callback):
        yield


@dataclass(frozen=True)
class StructuredOutcome:
    """A validated structured result plus token usage and provenance.

    Attributes:
        value: The validated Pydantic model, or ``None`` when neither
            native structured output nor prompt-JSON parsing produced a
            schema-valid object (the loud fallback — the caller decides
            the degraded behaviour, it is never hidden).
        usage: ``{"prompt_tokens", "completion_tokens"}`` from the call.
        marker: One of the ``STRUCTURED_MARKER_*`` constants, recording
            HOW the value was obtained (native / prompt-parsed / failed).
    """

    value: BaseModel | None
    usage: dict[str, int]
    marker: str


def structured_call(
    llm: Any,
    *,
    prompt: str,
    model_cls: type[_ModelT],
    node: str,
    system: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float = REASONING_TIMEOUT,
    deadline: float | None = None,
    state: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> StructuredOutcome:
    """Run one LLM call and validate the reply into *model_cls*.

    Uses the provider's native structured output when available; else a
    plain completion whose JSON body is extracted and validated. A reply
    that never validates yields ``value=None`` with the fallback marker
    (logged), so the caller degrades visibly instead of trusting an
    empty object.

    Args:
        llm: The :class:`~inqtrix.providers.base.LLMProvider`.
        prompt: English instructions with the answer-language directive.
        model_cls: The Pydantic model the reply must validate into.
        node: The routing call-site name (for logs/diagnostics).
        system: Optional system instruction.
        model: Resolved model id (``None`` inherits the provider default).
        reasoning_effort: Resolved effort token (``""``/``None`` = default).
        timeout: Per-call timeout budget in seconds.
        deadline: Optional absolute monotonic deadline.
        state: Optional provider accounting accumulator, also retained when
            native JSON parsing raises after a response consumed tokens.
        max_output_tokens: Optional small output budget for bounded tasks.

    Returns:
        A :class:`StructuredOutcome`.
    """
    schema = model_cls.model_json_schema()
    schema_name = model_cls.__name__
    options: dict[str, Any] = {}
    if state is not None:
        options["state"] = state
    if max_output_tokens is not None:
        options["max_output_tokens"] = max_output_tokens
    if llm.supports_structured_output(model=model):
        # The node name is the call's purpose on the provider span: this
        # helper is the one boundary every structured phase call crosses,
        # so binding here attributes all of them without call-site edits.
        with observe_bound_model_retries(llm, node), bind_purpose(node):
            response = llm.complete_structured(
                prompt,
                schema=schema,
                schema_name=schema_name,
                system=system,
                model=model,
                timeout=timeout,
                deadline=deadline,
                reasoning_effort=reasoning_effort,
                **options,
            )
        usage = _usage(response)
        try:
            return StructuredOutcome(
                value=model_cls.model_validate(response.parsed),
                usage=usage,
                marker=STRUCTURED_MARKER_NATIVE,
            )
        except ValidationError:
            log.warning(
                "Structured response (%s) violates schema %s; visibly discarding it "
                "(marker %s).",
                node,
                schema_name,
                STRUCTURED_MARKER_FALLBACK,
            )
            return StructuredOutcome(None, usage, STRUCTURED_MARKER_FALLBACK)

    with observe_bound_model_retries(llm, node), bind_purpose(node):
        response = llm.complete_with_metadata(
            prompt,
            system=system,
            model=model,
            timeout=timeout,
            deadline=deadline,
            reasoning_effort=reasoning_effort,
            **options,
        )
    usage = _usage(response)
    content = getattr(response, "content", "") or ""
    match = _JSON_BLOCK.search(content)
    if match is not None:
        try:
            parsed = json.loads(match.group(0))
            return StructuredOutcome(
                value=model_cls.model_validate(parsed),
                usage=usage,
                marker=STRUCTURED_MARKER_PARSED,
            )
        except (ValueError, TypeError, ValidationError):
            pass
    log.warning(
        "Response (%s) could not be parsed as %s; visible fallback (marker %s).",
        node,
        schema_name,
        STRUCTURED_MARKER_FALLBACK,
    )
    return StructuredOutcome(None, usage, STRUCTURED_MARKER_FALLBACK)


def _usage(response: Any) -> dict[str, int]:
    return {
        "prompt_tokens": getattr(response, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response, "completion_tokens", 0) or 0,
    }
