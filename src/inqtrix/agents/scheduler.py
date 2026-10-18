"""Wave scheduling and task execution.

Waves are the topological levels over ``depends_on``; inside a wave up
to ``max_parallel_children`` tasks run concurrently. Failure policy is
DETERMINISTIC: one retry with the same operator-owned resource bounds on
an allowlisted transient failure, then failed → proceed-and-mark (the
memo lists the hole).

That policy runs on TWO execution substrates: this module executes
in-process wave tasks and retries them inline (:func:`execute_wave`); the
parked child-run half lives in the algorithm's children-wait node. Both
share the ONE decision — :func:`should_retry` — so the two loops cannot
drift apart. Tool execution is injected (the
``TaskExecutor`` protocol) so the trajectory catalog asserts call ORDER
without any network.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, Callable

from inqtrix.agents.control_ports import (
    TASK_RESULT_SUMMARY_MAX_WORDS,
    PlanTaskRecord,
)
from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.i18n import t
from inqtrix.exceptions import (
    AgentCancelled,
    AgentTokenBudgetExceeded,
    RunNotFound,
)
from inqtrix.execution_failures import (
    RETRYABLE_AGENT_TASK_ORCHESTRATION_CODES,
)

log = logging.getLogger("inqtrix")


class CheckpointSchemaError(RuntimeError):
    """A checkpointed value cannot be reconciled with the current schema.

    Durable checkpoints (the Postgres saver) outlive deploys, so a value
    written by an older version is read by newer code.
    :meth:`TaskOutcome.from_state` absorbs the common drift — a field added,
    removed, or renamed since the checkpoint was written — but cannot invent
    a REQUIRED field that has no default. That single unreconcilable case
    raises this, loud and diagnosable ("restart as a new run"), instead of
    an opaque ``TypeError`` deep in a node.
    """

TaskExecutor = Callable[[PlanTaskRecord, int], "TaskOutcome"]
"""Executes ONE task attempt: ``executor(task, attempt) -> TaskOutcome``.
The algorithm supplies it with the tool wiring (children, capabilities,
in-process knowledge runs) bound in."""

TaskOutcomeObserver = Callable[[PlanTaskRecord, "TaskOutcome"], None]
"""Consumes a terminal local-task outcome on the scheduler thread."""

TaskRetryAdmission = Callable[[PlanTaskRecord], bool]
"""Revalidates the authoritative task row before a local retry starts."""


@dataclass
class TaskOutcome:
    """Result of one task execution (the TaskResult projection).

    Attributes:
        status: ``completed`` | ``failed`` | ``insufficient_evidence`` |
            ``skipped``.
        summary: Compact result the supervisor keeps (<= 300 words) —
            the ONLY task text that enters later prompts.
        answer_markdown: Complete user-facing task output. This is persisted
            for the lazy detail surface and never substituted for ``summary``
            in supervisor prompts.
        evidence: EvidenceRef dicts (``label``/``kind``/``url`` or
            ``document_id``+``chunk_index``/``excerpt``).
        web_search_ledger: Provider-returned search answers and citation
            lineage produced by this task. It is checkpointed separately from
            the compact summary and never fetches the linked pages.
        claims: Compact claim projections harvested from children.
        child_run_id: The spawned research child, when any.
        failure_reason: Visible reason for failed tasks.
        failure_code: Stable machine-readable failure category.
        usage: LLM tokens the task consumed in-process.
        transient: Whether a failure belongs to the explicit retry
            allowlist (drives the ONE retry).
        evidence_contract_status: The research child's evidence contract
            as its ``ResearchMetrics`` report it (``clean``,
            ``needs_review``, ``source_context_only``, ...); ``unknown``
            for in-process tools and children that ran no audit.
        confidence: The child's capped final confidence (0-10; 0 when
            unknown).
        single_source_ratio: Share of the child's supported claims
            (verified or attributed) resting on one source while its
            evidence-depth gap was active.
        omitted_evidence_count: Report-eligible sources the child's report
            did not evaluate because they did not fit its evidence budget
            (``0`` when everything was read). Non-zero makes the task's
            :attr:`coverage` ``partial``: the parent must carry the gap as
            an open point instead of reading the report as complete.
        omitted_evidence_query_count: Searches of the child that lost at
            least one source to that budget.
        document_analysis: The server-built report of a whole-document
            analysis (``agents.document_analysis``): what was read of each
            document, extraction facts, verified findings. Its counts,
            ranges and reasons are server facts; the quotes, claims and
            summary inside it are model text over verified passages,
            rendered for the reader and never into another prompt.
        search_notices: The limits an in-process knowledge search reported
            (``knowledge.retrieval_warnings.SearchNotice.as_dict`` values):
            text-free, folded by code over the task's searches. Non-empty
            makes the task's evidence an open point; a duplicate collapse is
            informative and never lands here.
    """

    status: str
    summary: str = ""
    answer_markdown: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    web_search_ledger: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    child_run_id: str | None = None
    failure_reason: str = ""
    failure_code: str = ""
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    transient: bool = False
    evidence_contract_status: str = "unknown"
    confidence: int = 0
    single_source_ratio: float = 0.0
    omitted_evidence_count: int = 0
    omitted_evidence_query_count: int = 0
    document_analysis: dict[str, Any] = field(default_factory=dict)
    search_notices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def coverage(self) -> str:
        """``complete`` when the child evaluated every source it had,
        ``partial`` when its evidence budget left sources unevaluated."""
        return "partial" if self.omitted_evidence_count > 0 else "complete"

    def to_state(self) -> dict[str, Any]:
        """The checkpoint-serializable form (paired with :meth:`from_state`).

        Explicit round-trip contract so the outcomes map that is threaded
        through the durable checkpoint survives a schema change — see
        :meth:`from_state`.
        """
        return asdict(self)

    @classmethod
    def from_state(cls, value: dict[str, Any]) -> "TaskOutcome":
        """Rehydrate from a checkpoint dict, tolerating schema drift.

        Unknown keys (a field removed or renamed since the checkpoint was
        written) are dropped; keys this version added but the checkpoint
        lacks fall back to their field default. So resuming an in-flight run
        across a deploy that changed ``TaskOutcome`` does not die on
        ``TaskOutcome(**value)`` with an opaque ``TypeError`` — the common
        drift is absorbed silently. The one case that cannot be reconciled —
        a REQUIRED field (no default) missing from the checkpoint — raises
        :class:`CheckpointSchemaError` so the failure is diagnosable and the
        run can be restarted, mirroring the lost-checkpoint contract.
        """
        known = {f.name: f for f in fields(cls)}
        accepted = {k: v for k, v in value.items() if k in known}
        missing_required = [
            name
            for name, f in known.items()
            if name not in accepted
            and f.default is MISSING
            and f.default_factory is MISSING
        ]
        if missing_required:
            raise CheckpointSchemaError(
                "TaskOutcome checkpoint is missing required field(s) "
                f"{missing_required}; the checkpoint predates this version "
                "and cannot be reconciled — restart as a new run."
            )
        return cls(**accepted)


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

    THE concurrency shape of the agent runtime below the wave scheduler
    (task sub-queries, memo sections, document windows): a sequential fast path (no pool
    for one item or ``max_parallel`` 1), a ``contextvars`` copy without
    which the run context would not reach the worker thread, and
    stop-on-exception — the sequential loop stopped at the first
    exception, so its siblings never ran; a pool submits them all, and a
    capability that is simply down would fail every one of them, N paid
    failures where the loop had one. An item that has not STARTED when a
    sibling raises gets ``skipped(index, item)`` instead, mirroring
    ``scheduler.execute_wave``'s ``wave_stop``.

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
"""The last word of a task summary cut at the control-row word bound; the
full text stays in the task result: its ``answer_markdown``, or for a
failed task without an answer its ``failure_reason``
(:func:`task_result_payload`)."""


def task_result_summary(value: str) -> str:
    """Normalize one task result to the public control-row word bound.

    A longer summary keeps its first words and ends in
    :data:`TASK_SUMMARY_CUT_MARK`, so the bound still holds and the cut is
    visible. Idempotent at the bound: a cut summary passes unchanged, and one
    a later step lengthened is cut again to one mark.
    """
    words = normalize_agent_markdown(str(value)).split()
    if len(words) > TASK_RESULT_SUMMARY_MAX_WORDS:
        words = [*words[: TASK_RESULT_SUMMARY_MAX_WORDS - 1], TASK_SUMMARY_CUT_MARK]
    return " ".join(words)


def task_result_payload(
    outcome: TaskOutcome, *, persisted_summary: str
) -> dict[str, Any]:
    """Return only checkpoint-recovery fields absent from row columns."""
    payload: dict[str, Any] = {}
    if outcome.answer_markdown:
        payload["answer_markdown"] = outcome.answer_markdown
    if outcome.evidence:
        payload["evidence"] = list(outcome.evidence)
    if outcome.web_search_ledger:
        payload["web_search_ledger"] = dict(outcome.web_search_ledger)
    if outcome.claims:
        payload["claims"] = list(outcome.claims)
    usage = {
        "prompt_tokens": int(outcome.usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(
            outcome.usage.get("completion_tokens", 0) or 0
        ),
    }
    if any(usage.values()):
        payload["usage"] = usage
    if outcome.failure_code:
        payload["failure_code"] = outcome.failure_code
    if outcome.failure_reason and outcome.failure_reason != persisted_summary:
        payload["failure_reason"] = outcome.failure_reason
    if outcome.evidence_contract_status != "unknown":
        payload["evidence_contract_status"] = outcome.evidence_contract_status
        payload["confidence"] = outcome.confidence
        if outcome.single_source_ratio:
            payload["single_source_ratio"] = outcome.single_source_ratio
    if outcome.omitted_evidence_count > 0:
        payload["omitted_evidence_count"] = outcome.omitted_evidence_count
        payload["omitted_evidence_query_count"] = (
            outcome.omitted_evidence_query_count
        )
    if outcome.document_analysis:
        payload["document_analysis"] = dict(outcome.document_analysis)
    if outcome.search_notices:
        payload["search_notices"] = list(outcome.search_notices)
    return payload


def project_child_run_outcome(
    run_store: Any,
    child_id: str,
    attempt: int,
    *,
    visible_to: Any = None,
) -> TaskOutcome | None:
    """Project one child run row/result into the shared task outcome.

    ``None`` means the child is still active. Missing retained data and every
    terminal failure become explicit outcomes so callers in the graph and in
    the synchronous cancel route use exactly one projection contract.
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
            child_run_id=child_id,
            failure_reason="child_row_missing",
            failure_code="child_row_missing",
            transient=False,
        )
    effective_attempt = max(
        1, int(child.get("parent_task_attempt", attempt) or attempt)
    )
    if child["status"] not in ("completed", "failed", "cancelled"):
        return None
    if child["status"] != "completed":
        error = child.get("error") or {}
        failure_code = (
            "cancelled"
            if child["status"] == "cancelled"
            else str(error.get("type") or "child_failed")
        )
        return TaskOutcome(
            status="failed",
            child_run_id=child_id,
            failure_reason=str(error.get("message", child["status"])),
            failure_code=failure_code,
            transient=(
                effective_attempt == 1
                and failure_code in RETRYABLE_AGENT_TASK_ORCHESTRATION_CODES
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
    claims = [
        {"text": claim.get("text", ""), "status": claim.get("status", "")}
        for claim in result.get("top_claims", [])
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
    raw_metrics = result.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    return TaskOutcome(
        status="completed" if answer.strip() else "insufficient_evidence",
        summary=task_result_summary(answer),
        answer_markdown=normalize_agent_markdown(answer),
        evidence=references,
        web_search_ledger=web_search_ledger,
        claims=claims,
        child_run_id=child_id,
        # The child's evidence audit travels with its outcome: a capped
        # confidence must not vanish behind a confident-sounding summary.
        evidence_contract_status=str(
            metrics.get("evidence_contract_status") or "unknown"
        ),
        confidence=int(metrics.get("confidence", 0) or 0),
        single_source_ratio=float(metrics.get("single_source_ratio", 0.0) or 0.0),
        # The child's evidence-budget cut travels too: a report that did
        # not evaluate every source is partial coverage, not "nothing found".
        omitted_evidence_count=int(
            metrics.get("omitted_evidence_record_count", 0) or 0
        ),
        omitted_evidence_query_count=int(
            metrics.get("omitted_evidence_query_count", 0) or 0
        ),
    )


def topological_waves(tasks: list[PlanTaskRecord]) -> list[list[PlanTaskRecord]]:
    """Kahn levels over ``depends_on`` (synthesis excluded by caller).

    Unknown dependency targets count as satisfied (the plan validator
    already reported them); a residual cycle would have failed
    validation, so leftovers are a programming error and raise.
    """
    pending = {task.task_id: task for task in tasks}
    resolved: set[str] = set()
    waves: list[list[PlanTaskRecord]] = []
    while pending:
        ready = [
            task
            for task in pending.values()
            if all(
                dep in resolved or dep not in pending
                for dep in task.depends_on
            )
        ]
        if not ready:
            raise RuntimeError(
                "cyclic task dependencies survived validation: "
                + ", ".join(sorted(pending))
            )
        ready.sort(key=lambda task: task.ordinal)
        waves.append(ready)
        for task in ready:
            resolved.add(task.task_id)
            del pending[task.task_id]
    return waves


def should_retry(
    outcome: TaskOutcome, *, attempt: int, cancelled: bool
) -> bool:
    """Whether a failed attempt earns the ONE deterministic retry.

    True only for the FIRST attempt of a TRANSIENT failure while no cancel
    is pending — the single retry the failure policy grants, identically on
    either execution substrate (in-process :func:`execute_wave` or a parked
    child run). Substrate-specific preconditions (e.g. the task still being
    in the plan on a resume) stay at the call site; this is the shared
    core so the two retry loops cannot diverge. Operator resource bounds
    remain unchanged for the retry.
    """
    return (
        outcome.status == "failed"
        and outcome.transient
        and attempt == 1
        and not cancelled
    )


def execute_wave(
    wave: list[PlanTaskRecord],
    *,
    executor: TaskExecutor,
    max_parallel: int,
    cancelled: Callable[[], bool],
    language: str,
    on_outcome: TaskOutcomeObserver | None = None,
    retry_allowed: TaskRetryAdmission | None = None,
) -> dict[str, TaskOutcome]:
    """Run one wave with the deterministic retry policy.

    Returns outcomes per task id. A cancel observed before a task starts
    marks it ``skipped``; a started task that raises :class:`AgentCancelled`
    is ``failed``. Completed siblings remain in the returned map so the
    caller persists honest partial results before ending the parent run.
    """
    outcomes: dict[str, TaskOutcome] = {}
    wave_stop = threading.Event()
    # Resolved once per wave: these summaries are PERSISTED with the plan
    # row, so they follow the run's answer language like the rest of its
    # text (the scheduler itself holds no prose).
    _words = {"language": language or ""}

    def _run_attempt(
        task: PlanTaskRecord, attempt: int
    ) -> tuple[TaskOutcome, bool]:
        """Run one admitted attempt and report whether execution began."""
        if cancelled() or wave_stop.is_set():
            return (
                TaskOutcome(
                    status="skipped",
                    summary=t(_words, "agent_task_summary_skipped_cancelled"),
                ),
                False,
            )
        spent_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            return executor(task, attempt), True
        except AgentTokenBudgetExceeded as exc:
            wave_stop.set()
            return (
                TaskOutcome(
                    status="failed",
                    summary=t(
                        _words, "agent_task_summary_token_budget"
                    ),
                    failure_reason="token_budget_exceeded",
                    failure_code="token_budget_exceeded",
                    usage=dict(getattr(exc, "usage", {}) or spent_usage),
                ),
                True,
            )
        except AgentCancelled:
            wave_stop.set()
            return (
                TaskOutcome(
                    status="failed",
                    summary=t(
                        _words, "agent_task_summary_cancelled_midway"
                    ),
                    failure_reason="client_requested_cancel",
                    failure_code="client_requested_cancel",
                    usage=spent_usage,
                ),
                True,
            )

    def _retry_admitted(task: PlanTaskRecord, outcome: TaskOutcome) -> bool:
        if not should_retry(outcome, attempt=1, cancelled=cancelled()):
            return False
        return retry_allowed is None or retry_allowed(task)

    def _combined_usage(
        first: TaskOutcome,
        second: TaskOutcome,
    ) -> TaskOutcome:
        second.usage = {
            "prompt_tokens": first.usage.get("prompt_tokens", 0)
            + second.usage.get("prompt_tokens", 0),
            "completion_tokens": first.usage.get("completion_tokens", 0)
            + second.usage.get("completion_tokens", 0),
        }
        return second

    def _log_retry(task: PlanTaskRecord, outcome: TaskOutcome) -> None:
        log.warning(
            "Task %s failed transiently (%s); retrying once with unchanged operator "
            "limits.",
            task.task_id,
            outcome.failure_reason,
        )

    def _record(
        task: PlanTaskRecord, task_id: str, outcome: TaskOutcome
    ) -> None:
        outcomes[task_id] = outcome
        if on_outcome is not None:
            on_outcome(task, outcome)

    if len(wave) == 1 or max_parallel <= 1:
        for task in wave:
            first, _first_started = _run_attempt(task, 1)
            outcome = first
            if _retry_admitted(task, first):
                _log_retry(task, first)
                second, second_started = _run_attempt(task, 2)
                if second_started:
                    outcome = _combined_usage(first, second)
            _record(task, task.task_id, outcome)
        return outcomes
    with ThreadPoolExecutor(
        max_workers=min(max_parallel, len(wave)),
        thread_name_prefix="inqtrix-agent-task",
    ) as pool:
        futures: dict[
            Future[tuple[TaskOutcome, bool]],
            tuple[PlanTaskRecord, int, TaskOutcome | None],
        ] = {}

        def _submit(
            task: PlanTaskRecord,
            attempt: int,
            first: TaskOutcome | None = None,
        ) -> None:
            context = contextvars.copy_context()
            future = pool.submit(context.run, _run_attempt, task, attempt)
            futures[future] = (task, attempt, first)

        for task in wave:
            _submit(task, 1)
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                task, attempt, first = futures.pop(future)
                outcome, attempt_started = future.result()
                if attempt == 1 and _retry_admitted(task, outcome):
                    _log_retry(task, outcome)
                    _submit(task, 2, outcome)
                    continue
                if first is not None and attempt_started:
                    outcome = _combined_usage(first, outcome)
                elif first is not None:
                    # Retry admission happened, but cancellation or a sibling
                    # failure closed the wave before attempt two entered the
                    # executor. Preserve the already executed first attempt;
                    # calling it "skipped" would corrupt the durable history.
                    outcome = first
                # Retry admission and outcome observation deliberately run on
                # the scheduler thread. Loop-affine control stores are never
                # touched by provider workers, and each submitted attempt gets
                # its own copied ContextVar context for telemetry.
                _record(task, task.task_id, outcome)
    return outcomes
