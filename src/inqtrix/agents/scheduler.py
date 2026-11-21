"""What a child run delivers to its parent, and the agent runtime's worker pool.

:func:`project_child_run_outcome` projects a finished child run onto one
:class:`TaskOutcome`, the contract a kernel parent reads a delegated child
through. :func:`run_in_pool` runs independent items concurrently and folds
their results in item order (the windows of one document analysis).
"""

from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from inqtrix.agents.control_ports import TASK_RESULT_SUMMARY_MAX_WORDS
from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.exceptions import RunNotFound

log = logging.getLogger(__name__)


@dataclass
class TaskOutcome:
    """A finished child run as its parent reads it: the kernel reads it
    when it condenses a child's report.

    Attributes:
        status: ``completed`` | ``failed`` | ``insufficient_evidence``.
        summary: Compact result (<= 300 words), the text of a condensed
            child report.
        evidence: EvidenceRef dicts (``label``/``kind``/``url`` or
            ``document_id``+``chunk_index``/``excerpt``).
        failure_reason: Visible reason for failed tasks.
        failure_code: Stable machine-readable failure category.
    """

    status: str
    summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""
    failure_code: str = ""


def run_in_pool(
    items: list[Any],
    run_one: "Callable[[int, Any], Any]",
    max_parallel: int,
    *,
    skipped: "Callable[[int, Any], Any]",
    on_result: "Callable[[Any], None] | None" = None,
    thread_name_prefix: str,
) -> list[Any]:
    """Run ``run_one`` over independent ``items`` and fold in ITEM order.

    THE concurrency shape of the agent runtime (the windows of one document
    analysis): a sequential fast path (no pool for one item or
    ``max_parallel`` 1), a ``contextvars`` copy without which the run context
    would not reach the worker thread, and stop-on-exception — the
    sequential loop stops at the first exception, so its siblings never run;
    a pool submits them all, and a capability that is simply down would fail
    every one of them, N paid failures where the loop had one. An item that
    has not STARTED when a sibling raises gets ``skipped(index, item)``
    instead.

    ``on_result`` runs on the CALLING thread, in item order, as each
    result becomes available — the place for everything that mutates
    shared state (usage, flushes, narration) while later items are still
    in flight.
    """
    if len(items) <= 1 or max_parallel <= 1:
        results = []
        for index, item in enumerate(items, start=1):
            result = run_one(index, item)
            if on_result is not None:
                on_result(result)
            results.append(result)
        return results
    stop = threading.Event()

    def _guarded(index: int, item: Any) -> Any:
        if stop.is_set():
            return skipped(index, item)
        try:
            return run_one(index, item)
        except BaseException:
            stop.set()
            raise

    with ThreadPoolExecutor(
        max_workers=min(max_parallel, len(items)),
        thread_name_prefix=thread_name_prefix,
    ) as pool:
        futures = []
        for index, item in enumerate(items, start=1):
            worker_context = contextvars.copy_context()
            futures.append(
                pool.submit(worker_context.run, _guarded, index, item)
            )
        results = []
        for future in futures:
            result = future.result()
            if on_result is not None:
                on_result(result)
            results.append(result)
        return results


TASK_SUMMARY_CUT_MARK = "[…]"
"""The last word of a summary cut at the word bound
(:data:`~inqtrix.agents.control_ports.TASK_RESULT_SUMMARY_MAX_WORDS`); the
full text stays in the child's stored result."""


def task_result_summary(value: str) -> str:
    """Normalize one child result to the summary word bound.

    A longer summary keeps its first words and ends in
    :data:`TASK_SUMMARY_CUT_MARK`, so the bound still holds and the cut is
    visible. Idempotent at the bound: a cut summary passes unchanged, and one
    a later step lengthened is cut again to one mark.
    """
    words = normalize_agent_markdown(str(value)).split()
    if len(words) > TASK_RESULT_SUMMARY_MAX_WORDS:
        words = [*words[: TASK_RESULT_SUMMARY_MAX_WORDS - 1], TASK_SUMMARY_CUT_MARK]
    return " ".join(words)


def project_child_run_outcome(
    run_store: Any,
    child_id: str,
    *,
    visible_to: Any = None,
) -> TaskOutcome | None:
    """Project one child run row/result into a :class:`TaskOutcome`.

    ``None`` means the child is still active. Missing retained data and every
    terminal failure become explicit outcomes.
    """
    try:
        child = (
            run_store.get(child_id, visible_to=visible_to)
            if visible_to is not None
            else run_store.get(child_id)
        )
    except RunNotFound:
        log.warning(
            "Child run %s is no longer available because retention expired or it was "
            "deleted; marking the task as failed.",
            child_id,
        )
        return TaskOutcome(
            status="failed",
            failure_reason="child_row_missing",
            failure_code="child_row_missing",
        )
    if child["status"] not in ("completed", "failed", "cancelled"):
        return None
    if child["status"] != "completed":
        error = child.get("error") or {}
        return TaskOutcome(
            status="failed",
            failure_reason=str(error.get("message", child["status"])),
            failure_code=(
                "cancelled"
                if child["status"] == "cancelled"
                else str(error.get("type") or "child_failed")
            ),
        )
    result = (
        run_store.result(child_id, visible_to=visible_to)
        if visible_to is not None
        else run_store.result(child_id)
    )
    # Only what the child's report cites is grounded evidence for the
    # parent; examined-but-uncited sources stay in the child's report.
    references = [
        dict(ref)
        for ref in result.get("references", [])
        if isinstance(ref, dict) and ref.get("cited", True)
    ]
    answer = str(result.get("answer", ""))
    raw_ledger = result.get("web_search_ledger")
    web_search_ledger = (
        dict(raw_ledger) if isinstance(raw_ledger, dict) else {}
    )
    if web_search_ledger:
        from inqtrix.evidence import attach_web_search_lineage

        references = attach_web_search_lineage(
            references, web_search_ledger
        )
    return TaskOutcome(
        status="completed" if answer.strip() else "insufficient_evidence",
        summary=task_result_summary(answer),
        evidence=references,
    )


