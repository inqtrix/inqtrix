"""Records, statuses and handles of an indexing job, shared by every job store.

The Postgres store, the services and the worker name these types without the
in-memory store behind :mod:`inqtrix.runs.indexing_memory`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.runs.indexing_auto_resume import public_plan

if TYPE_CHECKING:
    from inqtrix.runs.indexing_memory import IndexingJobStore

log = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_INDEXING_STATUSES",
    "ACTIVE_INDEXING_STATUS_VALUES",
    "IndexingExecutionSpec",
    "FencedIndexingJobHandle",
    "IndexingJobConflict",
    "IndexingJobHandle",
    "IndexingJobNotFound",
    "IndexingOperationKind",
    "IndexingJobRecord",
    "IndexingResumeUnavailable",
    "IndexingJobStatus",
    "IndexingQueueFull",
    "TERMINAL_INDEXING_EVENTS",
    "TERMINAL_INDEXING_STATUSES",
    "build_indexing_event",
    "build_indexing_job_summary",
    "new_indexing_job_id",
]

IndexingWork = Callable[["IndexingJobHandle"], None]
IndexingCleanup = Callable[[], Awaitable[Any]]
"""Removes what a job staged; runs on the loop, after the store has let go of its lock."""


class IndexingOperationKind(StrEnum):
    """Long-running knowledge operation executed by the indexing workers."""

    COLLECTION_GENERATION = "collection_generation"
    DOCUMENT_REVISION = "document_revision"


@dataclass(frozen=True)
class IndexingExecutionSpec:
    """Canonical identity needed to reconstruct one indexing operation.

    This is an internal execution contract, not part of the public job
    summary: requester attribution must remain server-side while allowing a
    durable paused job to rebind the same operation after a no-queue process
    restart.
    """

    job_id: str
    collection_id: str
    embedding_model: str
    operation_kind: IndexingOperationKind
    document_id: str | None
    revision_id: str | None
    generation_id: str | None
    created_by_user_id: uuid.UUID | None
    created_by_tenant_id: str | None


class IndexingJobStatus(StrEnum):
    """Lifecycle status shared by all background indexing operations."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    PAUSED_DEPENDENCY = "paused_dependency"
    PAUSED_VALIDATION = "paused_validation"
    SUPERSEDED = "superseded"
    READY_RAW_BY_USER_CHOICE = "ready_raw_by_user_choice"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_INDEXING_STATUSES = frozenset(
    {
        IndexingJobStatus.COMPLETED,
        IndexingJobStatus.FAILED,
        IndexingJobStatus.CANCELLED,
        IndexingJobStatus.SUPERSEDED,
        IndexingJobStatus.READY_RAW_BY_USER_CHOICE,
    }
)

ACTIVE_INDEXING_STATUSES = frozenset(
    {
        IndexingJobStatus.QUEUED,
        IndexingJobStatus.RUNNING,
        IndexingJobStatus.CANCELLING,
        IndexingJobStatus.PAUSED_DEPENDENCY,
        IndexingJobStatus.PAUSED_VALIDATION,
    }
)
"""States that retain indexing work and therefore block aggregate teardown."""

ACTIVE_INDEXING_STATUS_VALUES = tuple(
    status.value for status in ACTIVE_INDEXING_STATUSES
)
"""Database-ready representation of :data:`ACTIVE_INDEXING_STATUSES`."""

TERMINAL_INDEXING_EVENTS = frozenset(
    {
        "inqtrix.index.completed",
        "inqtrix.index.failed",
        "inqtrix.index.cancelled",
        "inqtrix.index.paused_dependency",
        "inqtrix.index.paused_validation",
        "inqtrix.index.superseded",
        "inqtrix.index.ready_raw_by_user_choice",
    }
)


class IndexingQueueFull(RuntimeError):
    """Raised when the indexing-operation queue has no free slot."""


class IndexingJobConflict(RuntimeError):
    """Raised when an indexing operation conflicts with an active fence.

    Generation builds are serialized per collection: a second concurrent run
    would compete for the same publication contract, so the router maps this
    to HTTP 409. Document revisions use an independent uniqueness fence per
    immutable revision. Any caller already authorized for the parent
    collection receives the existing job rather than starting duplicate
    provider work.
    """


class IndexingJobNotFound(KeyError):
    """Raised when a requested indexing job id is not present in memory."""


class IndexingResumeUnavailable(IndexingJobConflict):
    """Paused work lacks valid canonical identity for safe reconstruction.

    The pause and its checkpoint remain untouched. Callers surface this as a
    typed 409 rather than moving the job to a queue that cannot execute it.
    """


def new_indexing_job_id() -> str:
    """Return an opaque, log-safe identifier for one indexing operation."""
    return f"ix_{uuid.uuid4().hex}"


@dataclass
class IndexingJobRecord:
    """Mutable server-side state for one indexing operation."""

    job_id: str
    collection_id: str
    collection_name: str
    embedding_model: str
    created_at: float
    operation_kind: IndexingOperationKind = IndexingOperationKind.COLLECTION_GENERATION
    document_id: str | None = None
    revision_id: str | None = None
    work: IndexingWork | None = field(repr=False, default=None)
    cleanup: IndexingCleanup | None = field(repr=False, default=None)
    index_id: str | None = None
    """Client-side vector-index id echoed back so the browser can map a
    job (keyed by ``job_id``) onto the right index on resume — events
    never carry the client's id otherwise."""
    workspace_id: str | None = None
    created_by_user_id: uuid.UUID | None = None
    """Canonical user UUID that started the job.

    The value is server-resolved authorization state. ``None`` is reserved
    for unscoped principals.
    """
    created_by_tenant_id: str | None = None
    """Tenant paired with ``created_by_user_id`` for job visibility."""
    status: IndexingJobStatus = IndexingJobStatus.QUEUED
    started_at: float | None = None
    finished_at: float | None = None
    finished_monotonic: float | None = None
    total_documents: int = 0
    completed_documents: int = 0
    current_document_title: str = ""
    phase: str = "queued"
    current_batch: int = 0
    total_batches: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict)
    generation_id: str | None = None
    fence_token: str | None = None
    error: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    event_seq: int = 0
    events: deque[dict[str, Any]] = field(default_factory=deque, repr=False)
    subscribers: list[Queue] = field(default_factory=list, repr=False)

    @property
    def percent(self) -> int:
        """Whole-percent progress, derived from completed/total documents.

        Terminal completion reads 100 even when an operation had no document
        units; otherwise the floor is 0 so a fresh job never flashes a stale
        value.
        """
        if self.total_documents > 0:
            return round(self.completed_documents / self.total_documents * 100)
        return 100 if self.status == IndexingJobStatus.COMPLETED else 0


def _job_snapshot(record: IndexingJobRecord) -> dict[str, Any]:
    """The progress snapshot carried by events and the public summary.

    One shape so the SSE ``data.snapshot`` and the summary cannot drift;
    ``progress_estimate`` is the 0..1 form mirrored from the run
    snapshot so the frontend's existing progress reader works unchanged.
    """
    return {
        "completed_documents": record.completed_documents,
        "total_documents": record.total_documents,
        "progress_estimate": round(record.percent / 100, 4),
        "current_document_title": record.current_document_title,
        "phase": record.phase,
        "current_batch": record.current_batch,
        "total_batches": record.total_batches,
    }


def _error_event_snapshot(record: IndexingJobRecord) -> dict[str, Any]:
    """Return progress coordinates without source titles for error events."""

    snapshot = _job_snapshot(record)
    snapshot["current_document_title"] = ""
    return snapshot


def _public_checkpoint(
    checkpoint: dict[str, Any], *, show_auto_resume: bool = True
) -> dict[str, Any]:
    """Expose progress coordinates without leaking generated source context."""
    public: dict[str, Any] = {
        "completed_document_ids": list(checkpoint.get("completed_document_ids", []))
    }
    contextualization = _contextualization_documents(checkpoint)
    if contextualization:
        document_ids = sorted(contextualization)
        public["contextualization"] = {
            "document_id": document_ids[0] if len(document_ids) == 1 else None,
            "active_documents": len(document_ids),
            "completed_batches": _completed_context_batches(checkpoint),
            "total_batches": sum(
                max(0, int(entry.get("total_batches", 0)))
                for entry in contextualization.values()
            ),
        }
    document_progress = _document_progress_entries(checkpoint)
    if document_progress:
        public["document_progress"] = document_progress
    auto_resume = public_plan(checkpoint.get("auto_resume")) if show_auto_resume else None
    if auto_resume is not None:
        public["auto_resume"] = auto_resume
    return public


def _completed_context_batches(checkpoint: dict[str, Any]) -> int:
    """Context batches checkpointed for the documents still in progress."""
    return sum(
        len(entry.get("batches", [])) if isinstance(entry.get("batches"), list) else 0
        for entry in _contextualization_documents(checkpoint).values()
    )


def _progress_mark(completed_documents: int, checkpoint: dict[str, Any]) -> list[int]:
    """The work a job has finished, compared from one pause to the next."""
    return [int(completed_documents), _completed_context_batches(checkpoint)]


def _with_auto_resume(
    checkpoint: dict[str, Any], plan: dict[str, Any] | None
) -> dict[str, Any]:
    """The checkpoint carrying ``plan`` as its resume plan (none when ``None``)."""
    updated = {key: value for key, value in checkpoint.items() if key != "auto_resume"}
    if plan is not None:
        updated["auto_resume"] = plan
    return updated


def _log_auto_resume_plan(job_id: str, plan: dict[str, Any] | None) -> None:
    """One WARNING and one count per planned or ended streak; nothing without a plan."""
    if plan is None:
        return
    _count_auto_resume("stopped" if plan["stopped"] is not None else "planned")
    if plan["stopped"] is not None:
        log.warning(
            "Automatic resume of indexing job %s stopped after %d attempts (reason=%s)",
            job_id,
            int(plan["attempts"]),
            plan["stopped"],
        )
        return
    log.warning(
        "Indexing job %s resumes automatically at %s (attempt %d/%d)",
        job_id,
        time.strftime("%H:%M:%S", time.gmtime(float(plan["next_at"]))) + "Z",
        int(plan["attempts"]) + 1,
        int(plan["max"]),
    )


def _log_automatic_resume(job_id: str, attempt: int, max_attempts: int) -> None:
    """One WARNING and one count per automatic resume."""
    _count_auto_resume("resumed")
    log.warning(
        "Indexing job %s resumed automatically (attempt %d/%d)",
        job_id,
        attempt,
        max_attempts,
    )


def _contextualization_documents(
    checkpoint: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return every unfinished document's private batch checkpoint.

    The legacy shape retained exactly one ``contextualization`` object. New
    collection generations process several documents concurrently, so their
    independently resumable batches live under
    ``contextualization_by_document``. Reading both shapes keeps already
    paused jobs resumable across the additive wire change.
    """

    raw = checkpoint.get("contextualization_by_document")
    documents = {
        str(document_id): dict(entry)
        for document_id, entry in raw.items()
        if isinstance(raw, dict)
        and isinstance(document_id, str)
        and isinstance(entry, dict)
    } if isinstance(raw, dict) else {}
    legacy = checkpoint.get("contextualization")
    if isinstance(legacy, dict):
        document_id = legacy.get("document_id")
        if isinstance(document_id, str) and document_id not in documents:
            documents[document_id] = dict(legacy)
    return documents


def _with_contextualization_batch(
    checkpoint: dict[str, Any],
    document_id: str,
    batch_checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Return a checkpoint containing one idempotently replaced batch."""

    documents = _contextualization_documents(checkpoint)
    current = documents.get(document_id, {})
    existing = current.get("batches", [])
    batch_number = int(batch_checkpoint.get("batch_number", 0))
    retained = [
        dict(item)
        for item in existing
        if isinstance(item, dict)
        and int(item.get("batch_number", 0)) != batch_number
    ]
    retained.append(dict(batch_checkpoint))
    retained.sort(key=lambda item: int(item.get("batch_number", 0)))
    documents[document_id] = {
        "document_id": document_id,
        "total_batches": batch_checkpoint.get("total_batches", 0),
        "batches": retained,
    }
    updated = {
        **checkpoint,
        "contextualization_by_document": documents,
    }
    updated.pop("contextualization", None)
    return updated


def _without_contextualization_document(
    checkpoint: dict[str, Any],
    document_id: str,
) -> dict[str, Any]:
    """Remove only one completed document's resumable batch payload."""

    documents = _contextualization_documents(checkpoint)
    documents.pop(document_id, None)
    updated = dict(checkpoint)
    updated.pop("contextualization", None)
    if documents:
        updated["contextualization_by_document"] = documents
    else:
        updated.pop("contextualization_by_document", None)
    return updated


def _document_progress_entries(
    checkpoint: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return safe, resumable phase coordinates keyed by document id."""

    raw = checkpoint.get("document_progress")
    if not isinstance(raw, dict):
        return {}
    return {
        str(document_id): {
            "phase": str(entry.get("phase", "preparing")),
            "current_batch": max(0, int(entry.get("current_batch", 0))),
            "total_batches": max(0, int(entry.get("total_batches", 0))),
        }
        for document_id, entry in raw.items()
        if isinstance(document_id, str) and isinstance(entry, dict)
    }


def _with_document_progress(
    checkpoint: dict[str, Any],
    document_id: str,
    phase: str,
    *,
    current_batch: int = 0,
    total_batches: int = 0,
) -> dict[str, Any]:
    progress = _document_progress_entries(checkpoint)
    progress[document_id] = {
        "phase": str(phase),
        "current_batch": max(0, int(current_batch)),
        "total_batches": max(0, int(total_batches)),
    }
    return {**checkpoint, "document_progress": progress}


def _without_document_progress(
    checkpoint: dict[str, Any],
    document_id: str,
) -> dict[str, Any]:
    progress = _document_progress_entries(checkpoint)
    progress.pop(document_id, None)
    updated = dict(checkpoint)
    if progress:
        updated["document_progress"] = progress
    else:
        updated.pop("document_progress", None)
    return updated


def build_indexing_event(
    *,
    job_id: str,
    sequence: int,
    event_type: str,
    created_at: float,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """The one SSE event envelope shape for indexing operations.

    Both the in-memory store and the durable Postgres store build their
    frames through this helper so the wire stays byte-identical across
    backends (the frontend ``indexingTypes.ts`` contract).
    """
    return {
        "type": event_type,
        "job_id": job_id,
        "sequence": sequence,
        "created_at": created_at,
        "data": dict(payload),
    }


def build_indexing_job_summary(
    record: IndexingJobRecord,
    *,
    queue_position: int | None = None,
    show_auto_resume: bool = True,
) -> dict[str, Any]:
    """Public indexing-operation summary for HTTP responses.

    ``queue_position`` is the 1-based slot a still-queued job occupies in
    the FIFO wait line (``None`` once running/terminal), mirroring the run
    summary so the UI can show a waiting state instead of a stalled 0 %.
    ``show_auto_resume`` is false where no sweep runs: a stored plan would
    promise an attempt nobody makes.
    """
    elapsed = None
    if record.started_at is not None:
        end = record.finished_at or time.time()
        elapsed = round(max(0.0, end - record.started_at), 2)
    return {
        "job_id": record.job_id,
        "collection_id": record.collection_id,
        "collection_name": record.collection_name,
        "embedding_model": record.embedding_model,
        "operation_kind": record.operation_kind.value,
        "document_id": record.document_id,
        "revision_id": record.revision_id,
        "index_id": record.index_id,
        "status": record.status.value,
        "queue_position": queue_position,
        "workspace_id": record.workspace_id,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "elapsed_seconds": elapsed,
        "total_documents": record.total_documents,
        "completed_documents": record.completed_documents,
        "percent": record.percent,
        "snapshot": _job_snapshot(record),
        "error": dict(record.error) if record.error else None,
        "phase": record.phase,
        "current_batch": record.current_batch,
        "total_batches": record.total_batches,
        "checkpoint": _public_checkpoint(
            record.checkpoint, show_auto_resume=show_auto_resume
        ),
        "generation_id": record.generation_id,
        "fence_token": record.fence_token,
        "last_event_sequence": record.event_seq,
        "events_url": f"/v1/knowledge/indexing-jobs/{record.job_id}/events",
    }


@dataclass(frozen=True)
class IndexingJobSubscription:
    """Live event subscription with buffered replay."""

    job_id: str
    queue: Queue
    replay: list[dict[str, Any]]
    store: "IndexingJobStore"

    def close(self) -> None:
        """Detach the subscriber queue from the store."""
        self.store.unsubscribe(self.job_id, self.queue)


def _log_landed_pause(
    job_id: str, status: IndexingJobStatus, error_type: str
) -> None:
    """One WARNING and one count per pause that landed, whichever path paused."""
    log.warning(
        "Indexing job %s paused (status=%s, error_type=%s)",
        job_id,
        status.value,
        error_type,
    )
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.count_indexing_pause(status=status.value, error_type=error_type)


def _count_auto_resume(outcome: str) -> None:
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.count_auto_resume(outcome=outcome)


class IndexingJobHandle:
    """Worker-side handle for advancing one job without exposing the store."""

    def __init__(
        self,
        store: "IndexingJobStore",
        job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        self._store = store
        self.job_id = job_id
        self.cancel_event = cancel_event
        self._parked = False

    @property
    def cancelled(self) -> bool:
        """Whether cancellation was requested for this job."""
        return self.cancel_event.is_set()

    @property
    def parked(self) -> bool:
        """Whether execution stopped in a resumable visible pause."""
        return self._parked

    @property
    def fence_job_id(self) -> str | None:
        """Durable publication fence; in-memory workers do not need one."""
        return None

    @property
    def fence_attempt(self) -> int | None:
        return None

    def begin(self, total_documents: int) -> None:
        """Record the total document count once it is known."""
        self._store.set_total(self.job_id, total_documents)

    @property
    def completed_document_ids(self) -> frozenset[str]:
        """Document checkpoints that survive a dependency pause/resume."""
        return self._store.completed_document_ids(self.job_id)

    @property
    def raw_by_user_choice(self) -> bool:
        """Whether the paused job was explicitly reset to a raw build."""
        return self._store.raw_by_user_choice(self.job_id)

    def phase(
        self,
        name: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        """Expose a truthful phase and known batch counters."""
        self._store.set_phase(
            self.job_id,
            name,
            current_batch=current_batch,
            total_batches=total_batches,
        )

    def checkpoint_document(
        self,
        document_id: str,
        *,
        embedding_receipt: dict[str, Any] | None = None,
    ) -> None:
        """Persist one fully staged document in the resumable manifest."""
        self._store.checkpoint_document(
            self.job_id,
            document_id,
            embedding_receipt=embedding_receipt,
        )

    def embedding_receipt(self, document_id: str) -> dict[str, Any] | None:
        """Read the exact quota facts persisted with a document checkpoint."""

        return self._store.embedding_receipt(self.job_id, document_id)

    def context_batch_checkpoints(self, document_id: str) -> list[dict[str, Any]]:
        """Read the private batch outputs retained for a resumable document."""
        return self._store.context_batch_checkpoints(self.job_id, document_id)

    def checkpoint_context_batch(
        self, document_id: str, checkpoint: dict[str, Any]
    ) -> None:
        """Durably retain one validated batch before the next provider call."""
        self._store.checkpoint_context_batch(self.job_id, document_id, checkpoint)

    def progress(
        self, *, completed_documents: int, current_document_title: str = ""
    ) -> None:
        """Emit one progress step for completed document work."""
        self._store.progress(
            self.job_id,
            completed_documents=completed_documents,
            current_document_title=current_document_title,
        )

    def document_started(self, document_id: str) -> None:
        """Emit the stable id of one document entering active processing."""
        self._store.document_started(self.job_id, document_id)

    def document_progress(
        self,
        document_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        """Emit phase progress bound to one exact document id."""
        self._store.document_progress(
            self.job_id,
            document_id,
            phase,
            current_batch=current_batch,
            total_batches=total_batches,
        )

    def complete(self) -> None:
        """Mark the job completed."""
        self._store.complete(self.job_id)

    def complete_raw_by_user_choice(self) -> None:
        """Publish a raw generation under its distinct audited terminal state."""
        self._store.complete_raw_by_user_choice(self.job_id)

    def supersede(self, reason: str = "newer_revision_requested") -> None:
        """Finish without publication because a newer source intent won."""
        self._store.supersede(self.job_id, reason=reason)

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        """Mark the job failed with a sanitized error payload."""
        self._store.fail(self.job_id, message, error_type=error_type)

    def cancel(self, reason: str = "cancelled") -> None:
        """Mark the job cancelled after the worker observed the request."""
        self._store.mark_cancelled(self.job_id, reason=reason)

    def pause_dependency(
        self, message: str, *, error_type: str = "dependency_timeout"
    ) -> None:
        """Pause provider work with a machine-readable dependency reason."""
        self._parked = bool(self._store.pause(
            self.job_id,
            IndexingJobStatus.PAUSED_DEPENDENCY,
            message,
            error_type=error_type,
        ))
        if not self._parked and self.cancelled:
            self.cancel("client_requested_cancel")

    def pause_validation(self, message: str) -> None:
        self._parked = bool(self._store.pause(
            self.job_id,
            IndexingJobStatus.PAUSED_VALIDATION,
            message,
            error_type="validation_error",
        ))
        if not self._parked and self.cancelled:
            self.cancel("client_requested_cancel")

    def document_completed(
        self, document_id: str, *, context_omitted_chunks: int = 0
    ) -> None:
        """Emit a per-document 'embedded' event for one finished document."""
        self._store.document_completed(
            self.job_id,
            document_id,
            context_omitted_chunks=context_omitted_chunks,
        )

    def document_publication_guard(
        self,
        *,
        document_id: str,
        revision_id: str,
    ) -> Any:
        """Return the process-local guard for the final revision mutation.

        Durable Postgres knowledge stores validate ``fence_job_id`` and
        ``fence_attempt`` in their own publication transaction. The in-memory
        job store instead owns the cancellation boundary. The knowledge store
        enters this guard only after acquiring its canonical source/store
        locks, preserving lock order while keeping cancel and activation
        mutually exclusive.
        """

        factory = getattr(self._store, "document_publication_guard", None)
        if callable(factory):
            return factory(
                self.job_id,
                document_id=document_id,
                revision_id=revision_id,
            )
        return nullcontext()


class FencedIndexingJobHandle(IndexingJobHandle):
    """Durable handle whose writes carry the claimed worker attempt.

    Queue workers and Postgres' in-process dispatcher use this same handle, so
    progress, publication, pause, and terminal writes share one
    ``(claimed_by, attempt)`` contract in either dispatch mode.
    """

    def __init__(
        self,
        store: Any,
        job_id: str,
        cancel_event: threading.Event,
        attempt: int,
    ) -> None:
        super().__init__(store, job_id, cancel_event)
        self._fence_attempt = attempt
        self.terminal_landed = False

    @property
    def fence_job_id(self) -> str:
        return self.job_id

    @property
    def fence_attempt(self) -> int:
        return self._fence_attempt

    def durable_cancel_requested(self) -> bool:
        """Check the exact durable attempt after a publication-fence loss."""

        checker = getattr(self._store, "attempt_cancel_requested", None)
        return bool(
            callable(checker)
            and checker(self.job_id, fence_attempt=self._fence_attempt)
        )

    def begin(self, total_documents: int) -> None:
        self._store.set_total(
            self.job_id, total_documents, fence_attempt=self._fence_attempt
        )

    def progress(
        self, *, completed_documents: int, current_document_title: str = ""
    ) -> None:
        self._store.progress(
            self.job_id,
            completed_documents=completed_documents,
            current_document_title=current_document_title,
            fence_attempt=self._fence_attempt,
        )

    def document_started(self, document_id: str) -> None:
        self._store.document_started(
            self.job_id, document_id, fence_attempt=self._fence_attempt
        )

    def document_progress(
        self,
        document_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        self._store.document_progress(
            self.job_id,
            document_id,
            phase,
            current_batch=current_batch,
            total_batches=total_batches,
            fence_attempt=self._fence_attempt,
        )

    def document_completed(
        self, document_id: str, *, context_omitted_chunks: int = 0
    ) -> None:
        self._store.document_completed(
            self.job_id,
            document_id,
            fence_attempt=self._fence_attempt,
            context_omitted_chunks=context_omitted_chunks,
        )

    def phase(
        self,
        name: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        self._store.set_phase(
            self.job_id,
            name,
            current_batch=current_batch,
            total_batches=total_batches,
            fence_attempt=self._fence_attempt,
        )

    def checkpoint_document(
        self,
        document_id: str,
        *,
        embedding_receipt: dict[str, Any] | None = None,
    ) -> None:
        self._store.checkpoint_document(
            self.job_id,
            document_id,
            embedding_receipt=embedding_receipt,
            fence_attempt=self._fence_attempt,
        )

    def checkpoint_context_batch(
        self, document_id: str, checkpoint: dict[str, Any]
    ) -> None:
        self._store.checkpoint_context_batch(
            self.job_id,
            document_id,
            checkpoint,
            fence_attempt=self._fence_attempt,
        )

    def pause_dependency(
        self, message: str, *, error_type: str = "dependency_timeout"
    ) -> None:
        self._parked = self._store.pause(
            self.job_id,
            IndexingJobStatus.PAUSED_DEPENDENCY,
            message,
            error_type=error_type,
            fence_attempt=self._fence_attempt,
        )
        # A durable pause owns and acknowledges the current dispatch message
        # even though the job remains resumable rather than terminal.
        self.terminal_landed = self._parked
        if not self._parked and (
            self.cancelled or self.durable_cancel_requested()
        ):
            self.cancel("client_requested_cancel")

    def pause_validation(self, message: str) -> None:
        self._parked = self._store.pause(
            self.job_id,
            IndexingJobStatus.PAUSED_VALIDATION,
            message,
            error_type="validation_error",
            fence_attempt=self._fence_attempt,
        )
        self.terminal_landed = self._parked
        if not self._parked and (
            self.cancelled or self.durable_cancel_requested()
        ):
            self.cancel("client_requested_cancel")

    def complete(self) -> None:
        self.terminal_landed = self._store.complete(
            self.job_id, fence_attempt=self._fence_attempt
        )

    def complete_raw_by_user_choice(self) -> None:
        self.terminal_landed = self._store.complete_raw_by_user_choice(
            self.job_id, fence_attempt=self._fence_attempt
        )

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        self.terminal_landed = self._store.fail(
            self.job_id,
            message,
            error_type=error_type,
            fence_attempt=self._fence_attempt,
        )

    def supersede(self, reason: str = "newer_revision_requested") -> None:
        self.terminal_landed = self._store.supersede(
            self.job_id,
            reason=reason,
            fence_attempt=self._fence_attempt,
        )

    def cancel(self, reason: str = "cancelled") -> None:
        self.terminal_landed = self._store.mark_cancelled(
            self.job_id,
            reason=reason,
            fence_attempt=self._fence_attempt,
        )
