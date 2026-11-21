"""The kernel's ports for the document analysis.

:class:`~inqtrix.agents.document_analysis.AnalysisPorts` over the running
kernel segment: the run's capability registry and identity, its provider,
its cancellation and its window budget. Progress goes out as the existing
document-analysis activity, one row per document, thinned to the first and
every fifth processed window and the row's settlement. The module goes away
when the analysis becomes an operation with ports of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from inqtrix.agents.document_analysis import (
    ANALYSIS_CAPABILITIES,
    ANALYSIS_NODE,
    READ_CAPABILITY,
    AnalysisPorts,
)
from inqtrix.agents.kernel.deps import run_coro
from inqtrix.harness.contracts.events import ACTIVITY_EVENT
from inqtrix.prompt_language import resolve_answer_language

if TYPE_CHECKING:
    from inqtrix.agents.kernel.deps import KernelDeps

log = logging.getLogger(__name__)

PROGRESS_EVERY = 5
"""A processed-window row goes out for the first window and every fifth."""


def analysis_unavailable(deps: "KernelDeps") -> str | None:
    """Why this run cannot analyse documents, as the visible tool text, or ``None``.

    Checked before anything is read: the engine handles a failure of a
    capability it calls, not a registry that is missing or lacks one.
    """
    registry = deps.capability_registry
    if registry is None:
        return (
            "Tool unavailable: project documents cannot be read on this server. "
            "Answer without this tool and name the gap."
        )
    if not ANALYSIS_CAPABILITIES <= set(registry.ids()):
        return (
            "Tool unavailable: the document analysis is not set up on this server. "
            "Answer without this tool and name the gap."
        )
    return None


def scope_document_count(deps: "KernelDeps") -> int | None:
    """Documents in the collections this run reaches, or ``None`` when unknown.

    Tells an absence its population: the documents one call analysed are
    not the run's collections. Read from the same listing the approval card
    names the collections from.
    """
    registry = deps.capability_registry
    if registry is None:
        return None
    try:
        output = run_coro(
            registry.invoke("knowledge.collections.list", {}, deps.capability_context)
        )
    except Exception as exc:  # noqa: BLE001 - a reader fact, never a gate
        log.warning(
            "Document count of the run's collections unavailable (error_type=%s); "
            "the analysis names no population.",
            type(exc).__name__,
        )
        return None
    return sum(
        int(getattr(collection, "document_count", 0) or 0)
        for collection in getattr(output, "collections", [])
    )


def _should_emit(payload: dict[str, Any]) -> bool:
    if payload.get("status") != "started":
        return True
    current = int(payload.get("current") or 0)
    return current == 1 or current % PROGRESS_EVERY == 0


def kernel_analysis_ports(
    deps: "KernelDeps",
    *,
    call_id: str,
    opened: dict[str, str],
) -> AnalysisPorts:
    """The ports of one analysis in the kernel segment *deps* runs.

    *call_id* keys the activity rows of this call, so two calls reading the
    same document keep two rows. *opened* receives every document whose
    text a read returned, with its collection: what the call binds as
    material.
    """
    from inqtrix.model_routing import describe_resolution, describe_unresolved_resolution
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import operation_span

    registry = deps.capability_registry
    provider_models = getattr(deps.llm, "models", None)
    resolution = (
        describe_unresolved_resolution(ANALYSIS_NODE, "")
        if provider_models is None
        else describe_resolution(
            ANALYSIS_NODE, provider_models, "", requested_model="", requested_effort="",
        )
    )
    deps.emit("inqtrix.node.model_resolution", resolution)

    def invoke(capability_id: str, payload: dict[str, Any]) -> Any:
        # The capability span of the tool observation, as for every
        # capability a kernel tool calls; reading here is no use of a
        # source of its own (the call counts once, as the tool).
        with operation_span(
            capability_id,
            {semconv.INQTRIX_TOOL_CAPABILITY: capability_id},
            record_exceptions=False,
        ) as span:
            try:
                output = run_coro(registry.invoke(capability_id, payload, deps.capability_context))
            except Exception as exc:
                # A typed capability failure names its code on the span; the
                # engine decides what the failure means for the reading.
                code = getattr(exc, "code", None)
                if span is not None and isinstance(code, str):
                    span.set_attribute(semconv.INQTRIX_TOOL_FAILURE_CODE, code)
                raise
        if capability_id == READ_CAPABILITY:
            opened.setdefault(str(output.id), str(getattr(output, "collection_id", "") or ""))
        return output

    def emit(payload: dict[str, Any]) -> None:
        if not _should_emit(payload):
            return
        deps.emit(
            ACTIVITY_EVENT,
            {
                "scope": "run",
                "phase": "execution",
                **payload,
                "activity_id": f"{call_id}:{payload.get('activity_id', '')}",
            },
        )

    def cancelled() -> bool:
        return deps.cancel_token is not None and deps.cancel_token.is_set()

    return AnalysisPorts(
        invoke=invoke,
        llm=deps.llm,
        model=resolution.get("model") or None,
        reasoning_effort=resolution.get("effort") or None,
        timeout=deps.timeout,
        language=resolve_answer_language(deps.question).code or "",
        cancelled=cancelled,
        emit=emit,
        claim_windows=deps.claim_document_windows,
        max_windows=int(deps.platform.document_analysis_max_windows),
        max_parallel=int(deps.platform.max_parallel_queries_per_task),
    )
