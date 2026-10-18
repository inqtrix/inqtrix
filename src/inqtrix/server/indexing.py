"""In-memory registry and queue for durable knowledge-indexing operations.

The sibling of :mod:`inqtrix.server.runs` covers long-running collection
generation builds and immutable document-revision deltas. A browser may close
and later resume observing either operation through the same bounded queue,
replayable event stream, canonical ``(tenant_id, user_id)`` visibility, and
per-collection history. Collection generations own one publication slot per
collection; document revisions use independent revision fences and may run
alongside that generation.

A reindex builds an isolated physical generation while the active generation
stays readable.  Document identities remain stable, source changes are folded
into the staged manifest, and one validated compare-and-swap publishes the
complete generation.  Cancellation or dependency pause leaves the active
generation unchanged; the unpublished generation remains resumable or can be
discarded exactly.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import SharePermission
from inqtrix.contextualization_circuit import (
    ContextualizationCircuitBreaker,
    MemoryContextualizationCircuitBreaker,
)
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.indexing_auto_resume import (
    AutoResumePolicy,
    DueResume,
    is_due,
    plan_pause,
    public_plan,
)
from inqtrix.server.runs import format_sse_event
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
    from inqtrix.auth.principal import UserContext
    from inqtrix.settings import KnowledgeSettings

log = logging.getLogger("inqtrix")

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
    "IndexingJobStore",
    "IndexingQueueFull",
    "TERMINAL_INDEXING_EVENTS",
    "TERMINAL_INDEXING_STATUSES",
    "build_indexing_event",
    "build_indexing_job_summary",
    "format_sse_event",
    "new_indexing_job_id",
]

IndexingWork = Callable[["IndexingJobHandle"], None]


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
    cleanup: Callable[[], None] | None = field(repr=False, default=None)
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


class IndexingJobStore:
    """Thread-safe in-memory queue and registry for indexing operations.

    Args:
        max_concurrent: Maximum number of actively executing indexing jobs.
            Additional accepted jobs wait in the FIFO queue.
        max_queue_size: Maximum number of waiting jobs. Active jobs do
            not count against this number.
        completed_ttl_seconds: How long terminal records remain
            queryable after completion. Queued/running jobs are never
            TTL-evicted.
        event_buffer_size: Number of recent events retained per job for
            late SSE subscribers.
        history_limit: Maximum number of terminal records retained per
            collection (the inline "last N" history); older terminal
            records for a collection are evicted beyond this count.
    """

    def __init__(
        self,
        *,
        max_concurrent: int,
        max_queue_size: int,
        completed_ttl_seconds: int,
        event_buffer_size: int,
        history_limit: int,
        auto_resume: AutoResumePolicy | None = None,
        clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self._max_concurrent = _require_minimum(
            "max_concurrent", max_concurrent, minimum=1
        )
        self._auto_resume = auto_resume or AutoResumePolicy(max_attempts=0)
        self._clock = clock
        self._random = random_source
        self._max_queue_size = _require_minimum(
            "max_queue_size", max_queue_size, minimum=0
        )
        self._completed_ttl_seconds = _require_minimum(
            "completed_ttl_seconds", completed_ttl_seconds, minimum=0
        )
        self._event_buffer_size = _require_minimum(
            "event_buffer_size", event_buffer_size, minimum=1
        )
        self._history_limit = _require_minimum(
            "history_limit", history_limit, minimum=0
        )
        self._records: dict[str, IndexingJobRecord] = {}
        self._pending: deque[str] = deque()
        self._running_count = 0
        self._lock = threading.RLock()
        self._authority: MemoryAuthorityCoordinator | None = None
        self._contextualization_circuit = (
            MemoryContextualizationCircuitBreaker()
        )

    @property
    def contextualization_circuit(self) -> ContextualizationCircuitBreaker:
        """Process-shared circuit authority for the memory deployment tier."""

        return self._contextualization_circuit

    def bind_authority_coordinator(
        self, coordinator: "MemoryAuthorityCoordinator"
    ) -> None:
        """Use the process-wide authority lock for job and collection writes."""
        self._authority = coordinator
        self._lock = coordinator.lock

    def _job_authority_context_locked(
        self,
        record: IndexingJobRecord,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> Any:
        actor = actor_user_id or record.created_by_user_id
        if self._authority is None or actor is None:
            return nullcontext()
        if not record.created_by_tenant_id:
            raise AuthorizationRevoked("indexing actor has no tenant authority")
        return self._authority.registered_resource_access_guard(
            tenant_id=record.created_by_tenant_id,
            actor_user_id=actor,
            resource_type="knowledge_collection",
            resource_id=record.collection_id,
            minimum=SharePermission.EDIT,
        )

    @contextmanager
    def _submission_authority_context(
        self,
        *,
        tenant_id: str | None,
        actor_user_id: uuid.UUID | None,
        collection_id: str,
    ) -> Iterator[None]:
        if self._authority is None or actor_user_id is None:
            yield
            return
        if not tenant_id:
            raise AuthorizationRevoked("indexing actor has no tenant authority")
        with self._authority.registered_resource_access_guard(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            resource_type="knowledge_collection",
            resource_id=collection_id,
            minimum=SharePermission.EDIT,
        ):
            yield

    def _append_job_effect_locked(
        self,
        record: IndexingJobRecord,
        *,
        action: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        if self._authority is None or not record.created_by_tenant_id:
            return
        self._authority.append_registered_resource_effects(
            tenant_id=record.created_by_tenant_id,
            actor_user_id=actor_user_id or record.created_by_user_id,
            action=action,
            resource_type="knowledge_collection",
            resource_id=record.collection_id,
            scope="knowledge",
        )

    @classmethod
    def from_settings(cls, settings: "KnowledgeSettings") -> "IndexingJobStore":
        """Build an indexing-operation store from knowledge settings."""
        return cls(
            max_concurrent=settings.reindex_max_concurrent,
            max_queue_size=settings.reindex_queue_max_size,
            completed_ttl_seconds=settings.reindex_completed_ttl_seconds,
            event_buffer_size=settings.reindex_event_buffer_size,
            history_limit=settings.reindex_history_limit,
            auto_resume=AutoResumePolicy(
                max_attempts=settings.reindex_auto_resume_attempts
            ),
        )

    @property
    def auto_resume_policy(self) -> AutoResumePolicy:
        """How this store's dependency pauses resume on their own."""
        return self._auto_resume

    def submit(
        self,
        *,
        collection_id: str,
        collection_name: str,
        embedding_model: str,
        work: IndexingWork,
        cleanup: Callable[[], None] | None = None,
        generation_id: str | None = None,
        operation_kind: IndexingOperationKind | str = (
            IndexingOperationKind.COLLECTION_GENERATION
        ),
        document_id: str | None = None,
        revision_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        index_id: str | None = None,
        workspace_id: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        created_by_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued indexing operation and dispatch it when possible.

        Raises:
            IndexingJobConflict: The collection already has an active
                collection-generation operation. Document-revision operations
                are independent deltas and may run alongside one generation.
            IndexingQueueFull: The waiting queue is already full.
        """
        with (
            self._lock,
            self._submission_authority_context(
                tenant_id=created_by_tenant_id,
                actor_user_id=created_by_user_id,
                collection_id=collection_id,
            ),
        ):
            self._cleanup_locked()
            operation_kind = IndexingOperationKind(operation_kind)
            if (
                operation_kind == IndexingOperationKind.COLLECTION_GENERATION
                and self._active_generation_for_collection_locked(collection_id)
                is not None
            ):
                raise IndexingJobConflict(collection_id)
            if operation_kind == IndexingOperationKind.DOCUMENT_REVISION:
                existing = self._active_revision_locked(revision_id)
                if existing is not None:
                    if existing.collection_id == collection_id:
                        return self._summary(existing)
                    raise IndexingJobConflict(collection_id)
            if (
                len(self._pending) >= self._max_queue_size
                and self._running_count >= self._max_concurrent
            ):
                raise IndexingQueueFull("indexing queue is full")
            job_id = self._new_unique_job_id_locked()
            record = IndexingJobRecord(
                job_id=job_id,
                collection_id=collection_id,
                collection_name=collection_name,
                embedding_model=embedding_model,
                created_at=time.time(),
                operation_kind=operation_kind,
                document_id=document_id,
                revision_id=revision_id,
                work=work,
                cleanup=cleanup,
                index_id=index_id,
                workspace_id=workspace_id,
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
                generation_id=generation_id,
                checkpoint=dict(checkpoint or {}),
                fence_token=uuid.uuid4().hex,
            )
            record.events = deque(maxlen=self._event_buffer_size)
            self._records[job_id] = record
            self._pending.append(job_id)
            self._emit_locked(
                record,
                "inqtrix.index.queued",
                {
                    "status": "queued",
                    "queue_position": self._queue_position_locked(job_id),
                },
            )
            self._append_job_effect_locked(record, action="indexing.submitted")
            self._dispatch_locked()
            return self._summary(record)

    def get(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return a public summary for *job_id*."""
        with self._lock:
            self._cleanup_locked()
            record = self._record_locked(
                job_id, workspace_id=workspace_id, visible_to=visible_to
            )
            return self._summary(record)

    def execution_spec(self, job_id: str) -> IndexingExecutionSpec:
        """Return the private canonical identity for resume reconstruction."""

        with self._lock:
            self._cleanup_locked()
            record = self._record_locked(job_id)
            return IndexingExecutionSpec(
                job_id=record.job_id,
                collection_id=record.collection_id,
                embedding_model=record.embedding_model,
                operation_kind=record.operation_kind,
                document_id=record.document_id,
                revision_id=record.revision_id,
                generation_id=record.generation_id,
                created_by_user_id=record.created_by_user_id,
                created_by_tenant_id=record.created_by_tenant_id,
            )

    def list(
        self,
        *,
        collection_id: str | None = None,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Return summaries for visible jobs, newest first.

        Optionally narrowed to one *collection_id* (the per-collection
        history/resume view). Visibility matches the run store:
        ``None`` *visible_to* sees every job (unscoped principals), a
        scoped principal sees only its own.
        """
        with self._lock:
            self._cleanup_locked()
            summaries = []
            for record in sorted(
                self._records.values(),
                key=lambda item: item.created_at,
                reverse=True,
            ):
                if collection_id is not None and record.collection_id != collection_id:
                    continue
                if _workspace_matches(
                    record, workspace_id
                ) and self._visible_to_matches_locked(record, visible_to):
                    summaries.append(self._summary(record))
            return summaries

    def has_active_job(self, collection_id: str) -> bool:
        """Whether any indexing operation still owns collection resources.

        Aggregate teardown intentionally waits for both generation builds and
        document deltas; submission serialization uses the narrower generation
        helper below.
        """
        with self._lock:
            self._cleanup_locked()
            return self._active_job_for_collection_locked(collection_id) is not None

    def has_active_document_job(self, document_id: str) -> bool:
        """Whether one document-revision operation can still publish writes."""

        with self._lock:
            self._cleanup_locked()
            return any(
                record.document_id == document_id
                and record.operation_kind == IndexingOperationKind.DOCUMENT_REVISION
                and record.status in ACTIVE_INDEXING_STATUSES
                for record in self._records.values()
            )

    def run_collection_mutation(
        self, collection_id: str, mutation: Callable[[], Any]
    ) -> Any:
        """Run aggregate teardown mutation iff no indexing job is active.

        The existing job-store lock is the memory backend's collection-row
        boundary. The callback must contain only the final store mutation;
        parsing and embedding happen before it.

        Raises:
            CollectionMaintenanceActive: An active job owns the collection.
        """
        from inqtrix.knowledge.stores.ports import CollectionMaintenanceActive

        with self._lock:
            self._cleanup_locked()
            if self._active_job_for_collection_locked(collection_id) is not None:
                raise CollectionMaintenanceActive(collection_id)
            return mutation()

    @contextmanager
    def document_publication_guard(
        self,
        job_id: str,
        *,
        document_id: str,
        revision_id: str,
    ) -> Iterator[None]:
        """Publish iff this exact non-cancelled revision job still owns intent.

        The knowledge store enters this context after its own canonical locks.
        This lock is also the cancellation lock: a cancel that lands first
        makes publication fail, while an activation holding the guard cannot
        be reclassified as cancelled halfway through its mutation.
        """

        from inqtrix.knowledge.stores.ports import IndexGenerationSuperseded

        with self._lock:
            self._cleanup_locked()
            record = self._records.get(job_id)
            if (
                record is None
                or record.status != IndexingJobStatus.RUNNING
                or record.cancel_event.is_set()
                or record.operation_kind
                != IndexingOperationKind.DOCUMENT_REVISION
                or record.document_id != document_id
                or record.revision_id != revision_id
            ):
                raise IndexGenerationSuperseded(
                    f"indexing job {job_id} no longer owns document publication"
                )
            with self._job_authority_context_locked(record):
                yield

    def cancel(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Request cancellation for a queued or running indexing operation."""
        with self._lock:
            self._cleanup_locked()
            record = self._record_locked(
                job_id, workspace_id=workspace_id, visible_to=visible_to
            )
            try:
                with self._job_authority_context_locked(
                    record, actor_user_id=actor_user_id
                ):
                    canonical = self._records[job_id]
                    if canonical.status == IndexingJobStatus.QUEUED:
                        self._remove_pending_locked(job_id)
                        canonical.cancel_event.set()
                        self._mark_terminal_locked(
                            canonical, IndexingJobStatus.CANCELLED
                        )
                        self._emit_locked(
                            canonical,
                            "inqtrix.index.cancelled",
                            {
                                "status": "cancelled",
                                "reason": "cancelled_before_start",
                                "snapshot": _job_snapshot(canonical),
                            },
                        )
                        canonical.work = None
                        if canonical.cleanup is not None:
                            canonical.cleanup()
                            canonical.cleanup = None
                        self._append_job_effect_locked(
                            canonical,
                            action="indexing.cancelled",
                            actor_user_id=actor_user_id,
                        )
                        return self._summary(canonical)
                    if canonical.status in {
                        IndexingJobStatus.PAUSED_DEPENDENCY,
                        IndexingJobStatus.PAUSED_VALIDATION,
                    }:
                        canonical.cancel_event.set()
                        self._mark_terminal_locked(
                            canonical, IndexingJobStatus.CANCELLED
                        )
                        self._emit_locked(
                            canonical,
                            "inqtrix.index.cancelled",
                            {
                                "status": "cancelled",
                                "reason": "cancelled_while_paused",
                                "snapshot": _job_snapshot(canonical),
                            },
                        )
                        if canonical.cleanup is not None:
                            canonical.cleanup()
                            canonical.cleanup = None
                        canonical.work = None
                        self._append_job_effect_locked(
                            canonical,
                            action="indexing.cancelled",
                            actor_user_id=actor_user_id,
                        )
                        return self._summary(canonical)
                    if canonical.status == IndexingJobStatus.RUNNING:
                        canonical.cancel_event.set()
                        canonical.status = IndexingJobStatus.CANCELLING
                        self._emit_locked(
                            canonical,
                            "inqtrix.index.cancel_requested",
                            {
                                "status": "cancelling",
                                "reason": "client_requested_cancel",
                            },
                        )
                        self._append_job_effect_locked(
                            canonical,
                            action="indexing.cancel_requested",
                            actor_user_id=actor_user_id,
                        )
                    return self._summary(canonical)
            except AuthorizationRevoked as exc:
                raise IndexingJobNotFound(job_id) from exc

    def fence_collection_for_deletion(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> int:
        """Terminally fence every active job before aggregate collection deletion.

        This internal transition is stronger than an interactive cancel
        request: collection deletion cannot wait behind a worker that still
        owns an active-job reservation.  The cancellation event plus terminal
        status makes every existing handle stale before the collection is
        removed.
        """

        with self._lock:
            self._cleanup_locked()
            records = [
                record
                for record in self._records.values()
                if record.collection_id == collection_id
                and record.status in ACTIVE_INDEXING_STATUSES
            ]
            for record in records:
                with self._job_authority_context_locked(
                    record, actor_user_id=actor_user_id
                ):
                    self._remove_pending_locked(record.job_id)
                    record.cancel_event.set()
                    self._mark_terminal_locked(record, IndexingJobStatus.CANCELLED)
                    self._emit_locked(
                        record,
                        "inqtrix.index.cancelled",
                        {
                            "status": "cancelled",
                            "reason": "collection_deletion",
                            "snapshot": _job_snapshot(record),
                        },
                    )
                    if record.cleanup is not None:
                        record.cleanup()
                        record.cleanup = None
                    record.work = None
                    self._append_job_effect_locked(
                        record,
                        action="indexing.cancelled",
                        actor_user_id=actor_user_id,
                    )
            return len(records)

    def fence_document_for_deletion(
        self,
        collection_id: str,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> int:
        """Fence only revision jobs that could republish one deleted document.

        Collection-generation jobs remain active: their snapshot/delta loop
        observes the tombstone and removes the document from the staged
        manifest. Authority was fixed by the durable deletion operation before
        this internal transition, so a later share revocation cannot strand a
        partially detached document.
        """

        with self._lock:
            self._cleanup_locked()
            records = [
                record
                for record in self._records.values()
                if record.collection_id == collection_id
                and record.document_id == document_id
                and record.operation_kind == IndexingOperationKind.DOCUMENT_REVISION
                and record.status in ACTIVE_INDEXING_STATUSES
            ]
            for record in records:
                self._remove_pending_locked(record.job_id)
                record.cancel_event.set()
                self._mark_terminal_locked(record, IndexingJobStatus.CANCELLED)
                self._emit_locked(
                    record,
                    "inqtrix.index.cancelled",
                    {
                        "status": "cancelled",
                        "reason": "document_deletion",
                        "snapshot": _job_snapshot(record),
                    },
                )
                if record.cleanup is not None:
                    record.cleanup()
                    record.cleanup = None
                record.work = None
                self._append_job_effect_locked(
                    record,
                    action="indexing.cancelled",
                    actor_user_id=actor_user_id,
                )
            return len(records)

    def subscribe(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> IndexingJobSubscription:
        """Subscribe after a durable cursor, then tail new events."""
        with self._lock:
            self._cleanup_locked()
            record = self._record_locked(
                job_id, workspace_id=workspace_id, visible_to=visible_to
            )
            queue: Queue = Queue()
            record.subscribers.append(queue)
            return IndexingJobSubscription(
                job_id=job_id,
                queue=queue,
                replay=[
                    event
                    for event in record.events
                    if int(event.get("sequence", 0)) > max(0, after_sequence)
                ],
                store=self,
            )

    def unsubscribe(self, job_id: str, queue: Queue) -> None:
        """Remove a queue from the subscriber list if still present."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            try:
                record.subscribers.remove(queue)
            except ValueError:
                return

    def set_total(self, job_id: str, total_documents: int) -> None:
        """Record the job's total document count and emit a progress step."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            if record.status in {
                IndexingJobStatus.PAUSED_DEPENDENCY,
                IndexingJobStatus.PAUSED_VALIDATION,
            }:
                return
            with self._job_authority_context_locked(record):
                record.total_documents = max(0, int(total_documents))
                self._emit_locked(
                    record,
                    "inqtrix.index.progress",
                    {"snapshot": _job_snapshot(record)},
                )

    def completed_document_ids(self, job_id: str) -> frozenset[str]:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise IndexingJobNotFound(job_id)
            values = record.checkpoint.get("completed_document_ids", [])
            return frozenset(str(value) for value in values)

    def embedding_receipt(self, job_id: str, document_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise IndexingJobNotFound(job_id)
            receipts = record.checkpoint.get("embedding_receipts")
            if not isinstance(receipts, dict):
                return None
            receipt = receipts.get(document_id)
            return dict(receipt) if isinstance(receipt, dict) else None

    def context_batch_checkpoints(
        self, job_id: str, document_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise IndexingJobNotFound(job_id)
            current = _contextualization_documents(record.checkpoint).get(
                document_id
            )
            if current is None:
                return []
            batches = current.get("batches", [])
            return [dict(item) for item in batches if isinstance(item, dict)]

    def checkpoint_context_batch(
        self,
        job_id: str,
        document_id: str,
        checkpoint: dict[str, Any],
    ) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            record.checkpoint = _with_contextualization_batch(
                record.checkpoint,
                document_id,
                checkpoint,
            )

    def set_phase(
        self,
        job_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            record.phase = str(phase)
            record.current_batch = max(0, int(current_batch))
            record.total_batches = max(0, int(total_batches))
            self._emit_locked(
                record,
                "inqtrix.index.progress",
                {"snapshot": _job_snapshot(record)},
            )

    def checkpoint_document(
        self,
        job_id: str,
        document_id: str,
        *,
        embedding_receipt: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            completed = list(
                dict.fromkeys(
                    [
                        *record.checkpoint.get("completed_document_ids", []),
                        document_id,
                    ]
                )
            )
            record.checkpoint = {
                **record.checkpoint,
                "completed_document_ids": completed,
            }
            if embedding_receipt is not None:
                receipts = record.checkpoint.get("embedding_receipts")
                receipt_map = dict(receipts) if isinstance(receipts, dict) else {}
                receipt_map[document_id] = dict(embedding_receipt)
                record.checkpoint["embedding_receipts"] = receipt_map
            record.checkpoint = _without_contextualization_document(
                record.checkpoint,
                document_id,
            )
            record.checkpoint = _without_document_progress(
                record.checkpoint,
                document_id,
            )

    def pause(
        self,
        job_id: str,
        status: IndexingJobStatus,
        message: str,
        *,
        error_type: str,
    ) -> bool:
        if status not in {
            IndexingJobStatus.PAUSED_DEPENDENCY,
            IndexingJobStatus.PAUSED_VALIDATION,
        }:
            raise ValueError(f"{status} is not a resumable pause status")
        with self._lock:
            record = self._records.get(job_id)
            if (
                record is None
                or record.status != IndexingJobStatus.RUNNING
                or record.cancel_event.is_set()
            ):
                return False
            record.status = status
            record.error = {
                "message": sanitize_error(message),
                "type": error_type,
            }
            plan = (
                plan_pause(
                    record.checkpoint.get("auto_resume"),
                    error_type=error_type,
                    progress=_progress_mark(
                        record.completed_documents, record.checkpoint
                    ),
                    now=self._clock(),
                    policy=self._auto_resume,
                    random=self._random,
                )
                if status == IndexingJobStatus.PAUSED_DEPENDENCY
                else None
            )
            record.checkpoint = _with_auto_resume(record.checkpoint, plan)
            data: dict[str, Any] = {
                "status": status.value,
                "error": record.error,
                "snapshot": _error_event_snapshot(record),
            }
            if plan is not None:
                # The pause ends the event stream: the plan travels with it.
                data["auto_resume"] = public_plan(plan)
            self._emit_locked(record, f"inqtrix.index.{status.value}", data)
            _log_landed_pause(job_id, status, error_type)
            _log_auto_resume_plan(job_id, plan)
            return True

    def resume(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
        work: IndexingWork | None = None,
        cleanup: Callable[[], None] | None = None,
        automatic: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Requeue a dependency/validation-paused job from its checkpoint.

        ``automatic`` is the sweep's resume: it applies only to a dependency
        pause whose plan is due and counts the attempt. A person's resume
        clears the plan, so a later pause starts a fresh count.
        """
        with self._lock:
            self._cleanup_locked()
            record = self._record_locked(
                job_id, workspace_id=workspace_id, visible_to=visible_to
            )
            if record.status not in {
                IndexingJobStatus.PAUSED_DEPENDENCY,
                IndexingJobStatus.PAUSED_VALIDATION,
            }:
                return self._summary(record)
            # Only a dependency pause carries a plan.
            plan = record.checkpoint.get("auto_resume")
            if automatic and not is_due(plan, self._clock() if now is None else now):
                return self._summary(record)
            if record.operation_kind == IndexingOperationKind.COLLECTION_GENERATION:
                other = self._active_generation_for_collection_locked(
                    record.collection_id
                )
                if other is not None and other.job_id != record.job_id:
                    raise IndexingJobConflict(record.collection_id)
            if record.work is None:
                if work is None:
                    raise IndexingResumeUnavailable(
                        f"job {job_id} has no retained work to resume"
                    )
                record.work = work
                record.cleanup = cleanup
            record.cancel_event.clear()
            record.status = IndexingJobStatus.QUEUED
            record.error = None
            record.phase = "queued"
            record.fence_token = uuid.uuid4().hex
            data: dict[str, Any] = {"status": "queued"}
            if automatic:
                attempt = int(plan["attempts"]) + 1
                record.checkpoint = _with_auto_resume(
                    record.checkpoint,
                    {**plan, "attempts": attempt, "next_at": None,
                     "resumed_automatically": True},
                )
                data.update(automatic=True, attempt=attempt, max=int(plan["max"]))
                _log_automatic_resume(job_id, attempt, int(plan["max"]))
            else:
                record.checkpoint = _with_auto_resume(record.checkpoint, None)
            self._pending.append(job_id)
            data["queue_position"] = self._queue_position_locked(job_id)
            data["snapshot"] = _job_snapshot(record)
            self._emit_locked(record, "inqtrix.index.resumed", data)
            self._append_job_effect_locked(
                record,
                action="indexing.resumed",
                actor_user_id=actor_user_id,
            )
            self._dispatch_locked()
            return self._summary(record)

    def due_auto_resumes(self, *, now: float) -> list[DueResume]:
        """Dependency pauses whose automatic resume is due at ``now``."""
        with self._lock:
            return [
                DueResume(
                    job_id=record.job_id,
                    error_type=str((record.error or {}).get("type") or ""),
                    next_at=float(record.checkpoint["auto_resume"]["next_at"]),
                )
                for record in self._records.values()
                if record.status == IndexingJobStatus.PAUSED_DEPENDENCY
                and is_due(record.checkpoint.get("auto_resume"), now)
            ]

    def stop_auto_resume(self, job_id: str, reason: str) -> None:
        """End a job's automatic resumes visibly; it stays paused for a person."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status != IndexingJobStatus.PAUSED_DEPENDENCY:
                return
            plan = dict(record.checkpoint.get("auto_resume") or {})
            plan.update(next_at=None, stopped=reason)
            record.checkpoint = _with_auto_resume(record.checkpoint, plan)
            attempts = int(plan.get("attempts", 0))
            self._emit_locked(
                record,
                "inqtrix.index.auto_resume_stopped",
                {"reason": reason, "attempts": attempts},
            )
            self._append_job_effect_locked(
                record, action="indexing.auto_resume_stopped"
            )
            _log_auto_resume_plan(job_id, plan)

    def resume_raw_by_user_choice(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
        work: IndexingWork | None = None,
        cleanup: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Reset checkpoints and requeue a paused job in explicit raw mode."""
        with self._lock:
            record = self._record_locked(
                job_id, workspace_id=workspace_id, visible_to=visible_to
            )
            if record.status not in {
                IndexingJobStatus.PAUSED_DEPENDENCY,
                IndexingJobStatus.PAUSED_VALIDATION,
            }:
                return self._summary(record)
            record.checkpoint = {"raw_by_user_choice": True}
            record.completed_documents = 0
            record.current_batch = 0
            record.total_batches = 0
            self._emit_locked(
                record,
                "inqtrix.index.raw_rebuild_requested",
                {
                    "status": record.status.value,
                    "generation_id": record.generation_id,
                    "snapshot": _job_snapshot(record),
                },
            )
            self._append_job_effect_locked(
                record,
                action="indexing.raw_rebuild_requested",
                actor_user_id=actor_user_id,
            )
        return self.resume(
            job_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
            actor_user_id=actor_user_id,
            work=work,
            cleanup=cleanup,
        )

    def raw_by_user_choice(self, job_id: str) -> bool:
        with self._lock:
            record = self._records.get(job_id)
            return bool(
                record is not None
                and record.checkpoint.get("raw_by_user_choice") is True
            )

    def progress(
        self,
        job_id: str,
        *,
        completed_documents: int,
        current_document_title: str = "",
    ) -> None:
        """Update completed-document progress and emit a progress event."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            with self._job_authority_context_locked(record):
                record.completed_documents = max(0, int(completed_documents))
                record.current_document_title = current_document_title
                self._emit_locked(
                    record,
                    "inqtrix.index.progress",
                    {"snapshot": _job_snapshot(record)},
                )

    def complete(self, job_id: str) -> None:
        """Mark the job completed."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            if record.status in {
                IndexingJobStatus.PAUSED_DEPENDENCY,
                IndexingJobStatus.PAUSED_VALIDATION,
            }:
                return
            with self._job_authority_context_locked(record):
                if record.status == IndexingJobStatus.CANCELLING:
                    self._mark_terminal_locked(record, IndexingJobStatus.CANCELLED)
                    self._emit_locked(
                        record,
                        "inqtrix.index.cancelled",
                        {
                            "status": "cancelled",
                            "reason": "client_requested_cancel",
                            "snapshot": _job_snapshot(record),
                        },
                    )
                    self._append_job_effect_locked(record, action="indexing.cancelled")
                    return
                record.current_document_title = ""
                record.phase = "ready"
                self._mark_terminal_locked(record, IndexingJobStatus.COMPLETED)
                self._emit_locked(
                    record,
                    "inqtrix.index.completed",
                    {"status": "completed", "snapshot": _job_snapshot(record)},
                )
                self._append_job_effect_locked(record, action="indexing.completed")
                record.cleanup = None

    def complete_raw_by_user_choice(self, job_id: str) -> None:
        """Terminalize a successfully published explicit raw generation."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            if record.status in {
                IndexingJobStatus.PAUSED_DEPENDENCY,
                IndexingJobStatus.PAUSED_VALIDATION,
            }:
                return
            with self._job_authority_context_locked(record):
                record.current_document_title = ""
                record.phase = "ready_raw"
                self._mark_terminal_locked(
                    record, IndexingJobStatus.READY_RAW_BY_USER_CHOICE
                )
                self._emit_locked(
                    record,
                    "inqtrix.index.ready_raw_by_user_choice",
                    {
                        "status": "ready_raw_by_user_choice",
                        "generation_id": record.generation_id,
                        "snapshot": _job_snapshot(record),
                    },
                )
                self._append_job_effect_locked(
                    record, action="indexing.ready_raw_by_user_choice"
                )
                record.cleanup = None

    def fail(
        self,
        job_id: str,
        message: str,
        *,
        error_type: str = "server_error",
    ) -> None:
        """Mark the job failed with a sanitized error payload."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            record.error = {"message": sanitize_error(message), "type": error_type}
            if record.cleanup is not None:
                try:
                    record.cleanup()
                except Exception as exc:  # noqa: BLE001 - failure remains terminal/visible
                    log.error(
                        "Indexing job %s cleanup failed after job failure "
                        "(error_type=%s)",
                        job_id,
                        type(exc).__name__,
                    )
                finally:
                    record.cleanup = None
            self._mark_terminal_locked(record, IndexingJobStatus.FAILED)
            self._emit_locked(
                record,
                "inqtrix.index.failed",
                {
                    "status": "failed",
                    "error": record.error,
                    "snapshot": _error_event_snapshot(record),
                },
            )
            self._append_job_effect_locked(record, action="indexing.failed")

    def supersede(
        self, job_id: str, *, reason: str = "newer_revision_requested"
    ) -> None:
        """Mark a stale revision job terminal without treating it as failure."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            record.phase = "superseded"
            record.current_document_title = ""
            self._mark_terminal_locked(record, IndexingJobStatus.SUPERSEDED)
            self._emit_locked(
                record,
                "inqtrix.index.superseded",
                {
                    "status": "superseded",
                    "reason": reason,
                    "snapshot": _job_snapshot(record),
                },
            )
            self._append_job_effect_locked(record, action="indexing.superseded")
            record.cleanup = None

    def mark_cancelled(self, job_id: str, *, reason: str) -> None:
        """Mark a running job cancelled after its worker exits."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            if record.cleanup is not None:
                try:
                    record.cleanup()
                except Exception as exc:  # noqa: BLE001 - cancellation stays visible
                    log.error(
                        "Indexing job %s cleanup failed during cancellation "
                        "(error_type=%s)",
                        job_id,
                        type(exc).__name__,
                    )
                finally:
                    record.cleanup = None
            self._mark_terminal_locked(record, IndexingJobStatus.CANCELLED)
            self._emit_locked(
                record,
                "inqtrix.index.cancelled",
                {
                    "status": "cancelled",
                    "reason": reason,
                    "snapshot": _job_snapshot(record),
                },
            )
            self._append_job_effect_locked(record, action="indexing.cancelled")

    def document_started(self, job_id: str, document_id: str) -> None:
        """Emit the stable id of one document entering active processing."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            with self._job_authority_context_locked(record):
                record.checkpoint = _with_document_progress(
                    record.checkpoint,
                    document_id,
                    "preparing",
                )
                self._emit_locked(
                    record,
                    "inqtrix.index.document_started",
                    {"document_id": document_id},
                )

    def document_progress(
        self,
        job_id: str,
        document_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
    ) -> None:
        """Persist and emit phase progress for one exact document."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            with self._job_authority_context_locked(record):
                record.phase = str(phase)
                record.current_batch = max(0, int(current_batch))
                record.total_batches = max(0, int(total_batches))
                record.checkpoint = _with_document_progress(
                    record.checkpoint,
                    document_id,
                    record.phase,
                    current_batch=record.current_batch,
                    total_batches=record.total_batches,
                )
                self._emit_locked(
                    record,
                    "inqtrix.index.document_progress",
                    {
                        "document_id": document_id,
                        "phase": record.phase,
                        "current_batch": record.current_batch,
                        "total_batches": record.total_batches,
                        "snapshot": _job_snapshot(record),
                    },
                )

    def document_completed(
        self, job_id: str, document_id: str, *, context_omitted_chunks: int = 0
    ) -> None:
        """Emit a per-document completion event (one document finished embedding).

        A standalone, non-terminal event carrying the backend document id so the
        frontend can flip that one file's row to "Indexiert" the moment it lands,
        rather than all files flipping together on completion. It is NOT part of
        the progress snapshot — the document-count bar is unchanged. Chunks the
        content filter left without context are counted in it.
        """
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            with self._job_authority_context_locked(record):
                self._emit_locked(
                    record,
                    "inqtrix.index.document_completed",
                    {
                        "document_id": document_id,
                        "outcome": "embedded",
                        **(
                            {"context_omitted_chunks": context_omitted_chunks}
                            if context_omitted_chunks
                            else {}
                        ),
                    },
                )

    # -- internals -------------------------------------------------------- #

    def _run_worker(
        self, job_id: str, work: IndexingWork, cancel_event: threading.Event
    ) -> None:
        handle = IndexingJobHandle(self, job_id, cancel_event)
        try:
            work(handle)
            self.complete(job_id)
        except Exception as exc:
            from inqtrix.indexing_failures import IndexingDependencyError
            from inqtrix.knowledge.contextualize import (
                ContextualizationDependencyError,
                ContextualizationValidationError,
            )
            from inqtrix.knowledge.stores.ports import GenerationValidationError

            if isinstance(exc, ContextualizationDependencyError):
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            elif isinstance(exc, IndexingDependencyError):
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            elif isinstance(
                exc, (ContextualizationValidationError, GenerationValidationError)
            ):
                handle.pause_validation(str(exc))
            else:
                log.error(
                    "Indexing job %s failed (error_type=%s)",
                    job_id,
                    type(exc).__name__,
                )
                from inqtrix.execution_failures import classify_execution_failure

                self.fail(
                    job_id,
                    sanitize_error(exc),
                    error_type=classify_execution_failure(exc),
                )
        finally:
            with self._lock:
                record = self._records.get(job_id)
                if record is not None and record.status not in {
                    IndexingJobStatus.PAUSED_DEPENDENCY,
                    IndexingJobStatus.PAUSED_VALIDATION,
                }:
                    record.work = None
                self._running_count = max(0, self._running_count - 1)
                self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        while self._running_count < self._max_concurrent and self._pending:
            job_id = self._pending.popleft()
            record = self._records.get(job_id)
            if (
                record is None
                or record.status != IndexingJobStatus.QUEUED
                or record.work is None
            ):
                continue
            record.status = IndexingJobStatus.RUNNING
            if record.started_at is None:
                record.started_at = time.time()
            record.phase = "starting"
            self._running_count += 1
            self._emit_locked(
                record,
                "inqtrix.index.started",
                {"status": "running", "snapshot": _job_snapshot(record)},
            )
            thread = threading.Thread(
                target=self._run_worker,
                args=(job_id, record.work, record.cancel_event),
                name=f"inqtrix-reindex-{job_id}",
                daemon=True,
            )
            thread.start()

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        ttl_expired = {
            job_id
            for job_id, record in self._records.items()
            if record.status in TERMINAL_INDEXING_STATUSES
            and record.finished_monotonic is not None
            and (now - record.finished_monotonic) > self._completed_ttl_seconds
            and not record.subscribers
        }
        beyond_history = self._beyond_history_locked()
        for job_id in ttl_expired | beyond_history:
            del self._records[job_id]

    def _beyond_history_locked(self) -> set[str]:
        """Terminal records past the per-collection history cap, oldest first.

        Records with live subscribers are kept so an in-flight SSE
        replay is never yanked mid-stream.
        """
        per_collection: dict[str, list[IndexingJobRecord]] = {}
        for record in self._records.values():
            if record.status not in TERMINAL_INDEXING_STATUSES:
                continue
            per_collection.setdefault(record.collection_id, []).append(record)
        doomed: set[str] = set()
        for records in per_collection.values():
            records.sort(
                key=lambda item: item.finished_at or item.created_at,
                reverse=True,
            )
            for record in records[self._history_limit :]:
                if not record.subscribers:
                    doomed.add(record.job_id)
        return doomed

    def _active_job_for_collection_locked(
        self, collection_id: str
    ) -> IndexingJobRecord | None:
        for record in self._records.values():
            if (
                record.collection_id == collection_id
                and record.status in ACTIVE_INDEXING_STATUSES
            ):
                return record
        return None

    def _active_generation_for_collection_locked(
        self, collection_id: str
    ) -> IndexingJobRecord | None:
        """Return the active collection-generation slot, excluding deltas."""

        for record in self._records.values():
            if (
                record.collection_id == collection_id
                and record.operation_kind == IndexingOperationKind.COLLECTION_GENERATION
                and record.status in ACTIVE_INDEXING_STATUSES
            ):
                return record
        return None

    def _active_revision_locked(
        self, revision_id: str | None
    ) -> IndexingJobRecord | None:
        """Return the one active job for an immutable revision, if any."""

        if revision_id is None:
            return None
        for record in self._records.values():
            if (
                record.operation_kind == IndexingOperationKind.DOCUMENT_REVISION
                and record.revision_id == revision_id
                and record.status in ACTIVE_INDEXING_STATUSES
            ):
                return record
        return None

    def _record_locked(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> IndexingJobRecord:
        record = self._records.get(job_id)
        if record is None:
            raise IndexingJobNotFound(job_id)
        if self._visible_to_matches_locked(record, visible_to):
            if not _workspace_matches(record, workspace_id):
                raise IndexingJobNotFound(job_id)
            return record
        # The client sees the indistinct 404; the denial stays
        # operator-visible.
        principal = visible_to.principal if visible_to is not None else None
        log_authorization_denial(
            log,
            action="read",
            principal_kind=principal.kind if principal is not None else None,
            actor_user_id=principal.user_id if principal is not None else None,
            tenant_id=principal.tenant_id if principal is not None else None,
            resource_type="reindex_job",
            resource_id=job_id,
        )
        raise IndexingJobNotFound(job_id)

    def _visible_to_matches_locked(
        self,
        record: IndexingJobRecord,
        visible_to: "UserContext | None",
    ) -> bool:
        if _visible_to_matches(record, visible_to):
            return True
        if self._authority is None or visible_to is None:
            return False
        principal = visible_to.principal
        if principal.user_id is None:
            return False
        try:
            with self._authority.registered_resource_access_guard(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                resource_type="knowledge_collection",
                resource_id=record.collection_id,
                minimum=SharePermission.VIEW,
            ):
                return True
        except AuthorizationRevoked:
            return False

    def _new_unique_job_id_locked(self) -> str:
        for _ in range(8):
            job_id = new_indexing_job_id()
            if job_id not in self._records:
                return job_id
            log.warning("Indexing job id collision detected; retrying allocation.")
        raise RuntimeError("could not allocate a unique indexing job id")

    def _mark_terminal_locked(
        self, record: IndexingJobRecord, status: IndexingJobStatus
    ) -> None:
        record.status = status
        record.finished_at = time.time()
        record.finished_monotonic = time.monotonic()

    def _queue_position_locked(self, job_id: str) -> int | None:
        try:
            return list(self._pending).index(job_id) + 1
        except ValueError:
            return None

    def _summary(self, record: IndexingJobRecord) -> dict[str, Any]:
        """Build the public summary, attaching the FIFO position for a
        still-queued job (``None`` once running/terminal)."""
        position = (
            self._queue_position_locked(record.job_id)
            if record.status == IndexingJobStatus.QUEUED
            else None
        )
        return build_indexing_job_summary(record, queue_position=position)

    def _remove_pending_locked(self, job_id: str) -> None:
        try:
            self._pending.remove(job_id)
        except ValueError:
            return

    def _emit_locked(
        self,
        record: IndexingJobRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        record.event_seq += 1
        event = build_indexing_event(
            job_id=record.job_id,
            sequence=record.event_seq,
            event_type=event_type,
            created_at=time.time(),
            payload=payload,
        )
        record.events.append(event)
        for subscriber in list(record.subscribers):
            subscriber.put(event)


def _require_minimum(name: str, value: int, *, minimum: int) -> int:
    """Coerce an integer setting and reject invalid values loudly."""
    coerced = int(value)
    if coerced < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {coerced}")
    return coerced


def _workspace_matches(record: IndexingJobRecord, workspace_id: str | None) -> bool:
    """Whether *record* belongs to the optional workspace namespace."""
    return workspace_id is None or record.workspace_id == workspace_id


def _visible_to_matches(
    record: IndexingJobRecord, visible_to: "UserContext | None"
) -> bool:
    """Authorization visibility predicate for one indexing job record.

    Mirrors the run store: ``None`` means no scoping (anonymous/static
    principals see every job). A scoped principal sees only jobs created by
    its canonical UUID in the same tenant. Ownerless records stay invisible
    to scoped principals rather than leaking across users.
    """
    if visible_to is None:
        return True
    return (
        record.created_by_user_id is not None
        and record.created_by_user_id == visible_to.principal.user_id
        and record.created_by_tenant_id == visible_to.principal.tenant_id
    )
