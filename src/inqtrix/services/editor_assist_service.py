"""Editor assistant orchestration cores (suggest + instruct).

The single-LLM-call cores extracted verbatim from
:mod:`inqtrix.server.routers.editor` so that BOTH consumers share one
implementation: the HTTP router (which keeps request parsing, quota,
semaphore, and the exception -> error-envelope mapping) and the
workspace-agent runtime (which calls the SYNC entry points from its
worker threads).

Both entry points are synchronous by design: the router wraps them in
its existing ``run_in_executor`` + ``asyncio.wait_for`` (so the HTTP
timeout semantics stay unchanged), while agent worker threads call them
directly. Provider-level timeouts are enforced per call via the
``timeout_seconds`` argument.

Layering: this module must never import ``inqtrix.server`` at import
time (the services <- server direction is pinned by a regression test).
The prompt/parse helpers still live in the server package, so they are
imported function-locally, exactly like the ``RunActive`` precedent in
:mod:`inqtrix.services.agent_control_service`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Sequence

from inqtrix.i18n import t
from inqtrix.model_routing import resolve_effort, resolve_model
from inqtrix.providers.base import provider_cancel_scope

if TYPE_CHECKING:
    from inqtrix.server.editor_instructions import (
        EditorInstructRequestData,
        EditorInstructResult,
    )
    from inqtrix.server.editor_suggestions import (
        EditorSuggestRequestData,
        EditorSuggestResult,
    )
from inqtrix.observability.context import with_feature
from inqtrix.observability import semconv
from inqtrix.observability.otel import operation_root_span

log = logging.getLogger("inqtrix")

EDITOR_INSTRUCT_CONTEXT_FLOOR_TOKENS = 128_000
"""Assumed context floor for document-level instructions. Providers that
publish a smaller window are still budgeted against this modern floor so
a conservative metadata value does not reject normal-sized documents."""

EDITOR_INSTRUCT_RESERVED_PROMPT_TOKENS = 6_000
"""Tokens reserved for the system rules, the instruction, and the model
output when computing the instruct document budget."""


class EditorDocumentTooLarge(ValueError):
    """The document exceeds the instruct context budget (HTTP 400).

    Raised BEFORE any prompt is built or any provider call happens, so
    the caller (router or agent) can reject the request without paying
    for a model call. Carries the user-facing German message.
    """


def resolve_editor_model(resolved: Any, llm: Any) -> tuple[str | None, str | None, list[str]]:
    """Resolve model/effort for one editor call, mirroring the chat picker.

    Returns:
        ``(model, effort, warnings)`` where a UI-picked concrete model
        wins, tier metadata resolves next, and a custom provider
        without metadata falls back to its own default with a visible
        warning, never silently.
    """
    provider_models = getattr(llm, "models", None)
    requested_tier = resolved.agent_settings.model_tier or None
    requested_model = (resolved.agent_settings.model or "").strip()
    requested_effort = (resolved.agent_settings.effort or "").strip()
    warnings: list[str] = []
    if requested_model:
        # The UI picked a concrete model -> use it directly (surfaced as
        # explicit_request); works even without published model metadata.
        return requested_model, requested_effort or None, warnings
    if provider_models is not None:
        model = resolve_model("direct_chat", provider_models, requested_tier) or None
        effort = resolve_effort("direct_chat", provider_models, requested_tier) or None
        return model, effort, warnings
    warnings.append(
        "Model chosen by provider default (no tier metadata available)."
    )
    return None, None, warnings


def _usage_accumulator() -> dict[str, int]:
    """A fresh ``state`` dict for one ``llm.complete`` token tally.

    ``inqtrix.state.track_tokens`` accumulates with ``+=``, so the two
    counters must pre-exist. Passed as ``state=`` so the raw-completion
    path captures the provider's REAL usage (the structured path reads
    it off the response object directly).
    """
    return {"total_prompt_tokens": 0, "total_completion_tokens": 0}


def _accumulated_tokens(usage_state: dict[str, int]) -> int:
    """Total tokens a :func:`_usage_accumulator` captured (prompt + completion)."""
    return int(usage_state.get("total_prompt_tokens", 0) or 0) + int(
        usage_state.get("total_completion_tokens", 0) or 0
    )


def _structured_tokens(structured: object) -> int:
    """Real prompt+completion tokens off a structured-output response.

    Defensive ``getattr`` so a provider/test double without usage fields
    contributes 0 rather than raising.
    """
    return int(getattr(structured, "prompt_tokens", 0) or 0) + int(
        getattr(structured, "completion_tokens", 0) or 0
    )


@operation_root_span("editor:suggest", **{semconv.INQTRIX_MODE: "editor_suggest"})
@with_feature("editor")
def run_editor_suggest(
    request: "EditorSuggestRequestData",
    *,
    llm: Any,
    model: str | None,
    reasoning_effort: str | None,
    structured_supported: bool,
    timeout_seconds: float,
    base_warnings: Sequence[str] = (),
    cancel_probe: "Callable[[], bool] | None" = None,
) -> tuple["EditorSuggestResult", int]:
    """Run one paragraph-rewrite suggestion end to end (SYNC core).

    Covers the whole moved orchestration: context/background clamping,
    reference-document clamping, prompt build, the structured-or-raw
    provider call, parsing, the punctuation fit at the passage's right
    edge, deterministic validation, and exactly ONE retry when the first
    result violates the edit contract.

    Args:
        request: Validated suggest payload (the router parses it; the
            agent builds it directly).
        llm: The resolved LLM provider (its ``complete`` /
            ``complete_structured`` are called synchronously here).
        model: Concrete model id, or ``None`` for the provider default.
        reasoning_effort: Effort hint, or ``None``.
        structured_supported: Whether to use the native structured-output
            path (the caller evaluates ``llm.supports_structured_output``
            once, so both the first call and the retry use the same path).
        timeout_seconds: Per-provider-call timeout (the editor budget,
            ``editor_assistant_timeout``).
        base_warnings: Warnings collected before the call (model
            resolution); they lead the response warning list, exactly as
            the pre-extraction route emitted them.

    Returns:
        ``(result, tokens_consumed)`` — tokens are the provider's REAL
        usage (both calls summed when the retry fires).

    Raises:
        EditorSuggestError: The model response could not be parsed or is
            unsafe to return (the router maps this to 502).
        Exception: Provider failures propagate for the caller to map.
    """
    from inqtrix.server.editor_suggestions import (
        EDITOR_SUGGEST_SCHEMA,
        EDITOR_SUGGEST_SCHEMA_NAME,
        build_editor_suggest_prompt,
        clamp_background,
        fit_rewrite_to_following_text,
        parse_editor_suggest_response,
        result_from_parsed,
        validate_editor_suggest_result,
        warnings_for_validation_issues,
    )
    from inqtrix.server.reference_documents import (
        clamp_reference_documents,
        render_reference_documents,
    )

    warnings = list(base_warnings)
    context_window = getattr(llm, "context_window_tokens", None)
    budget_tokens = max(2000, (context_window or 16000) - 6000)
    background, truncated = clamp_background(
        request.background,
        request.block_text,
        max_chars=budget_tokens * 3,
    )
    if truncated:
        # Read-only report context is windowed around the edited paragraph.
        # For a single-paragraph rewrite the excerpt is sufficient, so this
        # is informational, not a failure — phrased neutrally so it does not
        # read as an error, and logged for visibility (No Silent Fallbacks).
        log.warning(
            "Editor suggest: report context windowed to the edited passage."
        )
        warnings.append(
            t({"language": request.locale}, "editor_excerpt_only_note")
        )
    request = replace(request, background=background)

    reference_budget = max(0, budget_tokens * 3 - len(background))
    reference_docs, reference_truncated = clamp_reference_documents(
        request.reference_documents,
        max_chars=reference_budget,
    )
    warnings.extend(request.reference_warnings)
    if reference_truncated:
        warnings.append("Reference documents truncated (context budget).")
        log.warning(
            "Editor suggestion reference documents truncated to fit the context budget."
        )
    reference_block = render_reference_documents(reference_docs)

    def complete_editor_prompt(prompt: str) -> tuple["EditorSuggestResult", int]:
        """Run one suggest call; return ``(result, tokens_consumed)``.

        Tokens come from the provider's REAL usage — the structured
        response carries it directly; the raw path reads it back from
        the ``state`` accumulator (:func:`inqtrix.state.track_tokens`).
        This includes any reasoning tokens the char estimate would miss.
        The cancel probe binds on THIS executor thread; providers
        consult it at retry-attempt boundaries and backoff sleeps, so a
        client abort stops the ladder after the in-flight attempt. The
        result is fitted to the text after the passage BEFORE validation:
        a rewrite that only repeated the mark that follows the passage is
        no change at all.
        """
        with provider_cancel_scope(cancel_probe):
            result, tokens = _unscoped_complete(prompt)
        return fit_rewrite_to_following_text(request, result), tokens

    def _unscoped_complete(
        prompt: str,
    ) -> tuple["EditorSuggestResult", int]:
        if structured_supported:
            structured = llm.complete_structured(
                prompt,
                schema=EDITOR_SUGGEST_SCHEMA,
                schema_name=EDITOR_SUGGEST_SCHEMA_NAME,
                model=model,
                reasoning_effort=reasoning_effort,
                max_output_tokens=4000,
                timeout=timeout_seconds,
            )
            return (
                result_from_parsed(structured.parsed, warnings=warnings),
                _structured_tokens(structured),
            )
        usage_state = _usage_accumulator()
        raw_response = llm.complete(
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            max_output_tokens=4000,
            timeout=timeout_seconds,
            state=usage_state,
        )
        return (
            parse_editor_suggest_response(raw_response, warnings=warnings),
            _accumulated_tokens(usage_state),
        )

    prompt = build_editor_suggest_prompt(request, reference_block=reference_block)
    result, consumed = complete_editor_prompt(prompt)
    validation_issues = validate_editor_suggest_result(request, result)
    if validation_issues:
        log.warning(
            "Editor suggestion violates the editing contract: %s",
            ", ".join(issue.code for issue in validation_issues),
        )
        retry_prompt = build_editor_suggest_prompt(
            request,
            previous_result=result.rewritten_text,
            validation_issues=validation_issues,
            reference_block=reference_block,
        )
        retry_result, retry_tokens = complete_editor_prompt(retry_prompt)
        # The retry is a second full LLM call — its tokens add to the
        # booked spend (the estimate billed it as one).
        result = retry_result
        consumed += retry_tokens
        validation_issues = validate_editor_suggest_result(request, result)
        if validation_issues:
            result = replace(
                result,
                warnings=[
                    *result.warnings,
                    *warnings_for_validation_issues(
                        validation_issues,
                        locale=request.locale,
                    ),
                ],
            )
    return result, consumed


@operation_root_span("editor:instruct", **{semconv.INQTRIX_MODE: "editor_instruct"})
@with_feature("editor")
def run_editor_instruct(
    request: "EditorInstructRequestData",
    *,
    llm: Any,
    model: str | None,
    reasoning_effort: str | None,
    structured_supported: bool,
    timeout_seconds: float,
    base_warnings: Sequence[str] = (),
    cancel_probe: "Callable[[], bool] | None" = None,
) -> tuple["EditorInstructResult", int]:
    """Run one document-level instruction end to end (SYNC core).

    Covers the moved orchestration: the document context-budget guard,
    reference-document clamping, prompt build, the structured-or-raw
    provider call, parsing, and the deterministic result validation.
    Unlike suggest there is no retry — the instruct contract keeps
    unverifiable anchors as visible warnings instead.

    Args mirror :func:`run_editor_suggest`.

    Returns:
        ``(result, tokens_consumed)`` with the provider's real usage.

    Raises:
        EditorDocumentTooLarge: The document exceeds the context budget
            (checked BEFORE any provider call; the router maps it to the
            historical 400).
        EditorInstructError: The model response could not be parsed or
            contains only empty edits (mapped to 502 by the router).
        Exception: Provider failures propagate for the caller to map.
    """
    from inqtrix.server.editor_instructions import (
        EDITOR_INSTRUCT_SCHEMA,
        EDITOR_INSTRUCT_SCHEMA_NAME,
        build_editor_instruct_prompt,
        parse_editor_instruct_response,
        result_from_parsed as result_from_instruction_parsed,
        validate_editor_instruct_result,
    )
    from inqtrix.server.reference_documents import (
        clamp_reference_documents,
        render_reference_documents,
    )

    context_window = getattr(llm, "context_window_tokens", None)
    if not isinstance(context_window, int) or context_window <= 0:
        context_window = None
    effective_context_window = max(
        context_window or 0,
        EDITOR_INSTRUCT_CONTEXT_FLOOR_TOKENS,
    )
    budget_chars = max(
        2000,
        effective_context_window - EDITOR_INSTRUCT_RESERVED_PROMPT_TOKENS,
    ) * 3
    if len(request.document_markdown) > budget_chars:
        raise EditorDocumentTooLarge(
            "Document is too large for a full revision."
        )

    warnings = list(base_warnings)
    reference_budget = max(0, budget_chars - len(request.document_markdown))
    reference_docs, reference_truncated = clamp_reference_documents(
        request.reference_documents,
        max_chars=reference_budget,
    )
    warnings.extend(request.reference_warnings)
    if reference_truncated:
        warnings.append("Reference documents truncated (context budget).")
        log.warning(
            "Editor instruction reference documents truncated to fit the context budget."
        )
    reference_block = render_reference_documents(reference_docs)

    def complete_instruction_prompt(prompt: str) -> tuple["EditorInstructResult", int]:
        """Run one instruct call; return ``(result, tokens_consumed)``.

        Real provider usage, as in the suggest path — structured
        response carries it; the raw path reads it back from the
        ``state`` accumulator. The cancel probe binds on THIS executor
        thread (see the suggest twin).
        """
        with provider_cancel_scope(cancel_probe):
            return _unscoped_complete(prompt)

    def _unscoped_complete(
        prompt: str,
    ) -> tuple["EditorInstructResult", int]:
        if structured_supported:
            structured = llm.complete_structured(
                prompt,
                schema=EDITOR_INSTRUCT_SCHEMA,
                schema_name=EDITOR_INSTRUCT_SCHEMA_NAME,
                model=model,
                reasoning_effort=reasoning_effort,
                max_output_tokens=8000,
                timeout=timeout_seconds,
            )
            return (
                result_from_instruction_parsed(structured.parsed),
                _structured_tokens(structured),
            )
        usage_state = _usage_accumulator()
        raw_response = llm.complete(
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            max_output_tokens=8000,
            timeout=timeout_seconds,
            state=usage_state,
        )
        return (
            parse_editor_instruct_response(raw_response),
            _accumulated_tokens(usage_state),
        )

    prompt = build_editor_instruct_prompt(request, reference_block=reference_block)
    result, consumed = complete_instruction_prompt(prompt)
    if warnings:
        result = replace(result, warnings=[*warnings, *result.warnings])
    result = validate_editor_instruct_result(request, result)
    return result, consumed
