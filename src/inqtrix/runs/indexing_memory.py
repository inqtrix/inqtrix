"""In-memory registry and queue for durable knowledge-indexing operations.

The sibling of :mod:`inqtrix.runs.memory_store` covers long-running collection
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
from functools import partial
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable

from inqtrix import aio
from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import SharePermission
from inqtrix.contextualization_circuit import (
    ContextualizationCircuitBreaker,
    MemoryContextualizationCircuitBreaker,
)
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.runs.shared import bridge_to_loop, inline_on_loop
from inqtrix.runs.indexing_auto_resume import (
    AutoResumePolicy,
    DueResume,
    is_due,
    plan_pause,
    public_plan,
)
from inqtrix.runs.indexing_records import (
    IndexingCleanup,
    ACTIVE_INDEXING_STATUSES,
    IndexingExecutionSpec,
    IndexingJobConflict,
    IndexingJobHandle,
    IndexingJobNotFound,
    IndexingJobRecord,
    IndexingJobStatus,
    IndexingJobSubscription,
    IndexingOperationKind,
    IndexingQueueFull,
    IndexingResumeUnavailable,
    IndexingWork,
    TERMINAL_INDEXING_STATUSES,
    _contextualization_documents,
    _error_event_snapshot,
    _job_snapshot,
    _log_auto_resume_plan,
    _log_automatic_resume,
    _log_landed_pause,
    _progress_mark,
    _with_auto_resume,
    _with_contextualization_batch,
    _with_document_progress,
    _without_contextualization_document,
    _without_document_progress,
    build_indexing_event,
    build_indexing_job_summary,
    new_indexing_job_id,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
    from inqtrix.auth.principal import UserContext
    from inqtrix.settings import KnowledgeSettings

log = logging.getLogger(__name__)

__all__ = ["IndexingJobStore"]


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
        cleanup: IndexingCleanup | None = None,
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

    async def aclose(self) -> None:
        """Ask every running body to stop: a lane's thread keeps the process alive until it ends."""
        with self._lock:
            for record in self._records.values():
                if record.status == IndexingJobStatus.RUNNING:
                    record.cancel_event.set()

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

    async def acancel(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Request cancellation for a queued or running indexing operation."""
        summary, cleanup = self._cancel_now(
            job_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
            actor_user_id=actor_user_id,
        )
        if cleanup is not None:
            await cleanup()
        return summary

    cancel = bridge_to_loop(acancel)

    def _cancel_now(
        self,
        job_id: str,
        *,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        actor_user_id: uuid.UUID | None,
    ) -> tuple[dict[str, Any], IndexingCleanup | None]:
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
                        cleanup = canonical.cleanup
                        canonical.cleanup = None
                        self._append_job_effect_locked(
                            canonical,
                            action="indexing.cancelled",
                            actor_user_id=actor_user_id,
                        )
                        return self._summary(canonical), cleanup
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
                        cleanup = canonical.cleanup
                        canonical.cleanup = None
                        canonical.work = None
                        self._append_job_effect_locked(
                            canonical,
                            action="indexing.cancelled",
                            actor_user_id=actor_user_id,
                        )
                        return self._summary(canonical), cleanup
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
                    return self._summary(canonical), None
            except AuthorizationRevoked as exc:
                raise IndexingJobNotFound(job_id) from exc

    async def afence_collection_for_deletion(
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

        cleanups: list[IndexingCleanup] = []
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
                        cleanups.append(record.cleanup)
                        record.cleanup = None
                    record.work = None
                    self._append_job_effect_locked(
                        record,
                        action="indexing.cancelled",
                        actor_user_id=actor_user_id,
                    )
        for cleanup in cleanups:
            await cleanup()
        return len(records)

    fence_collection_for_deletion = bridge_to_loop(afence_collection_for_deletion)

    async def afence_document_for_deletion(
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

        cleanups: list[IndexingCleanup] = []
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
                    cleanups.append(record.cleanup)
                    record.cleanup = None
                record.work = None
                self._append_job_effect_locked(
                    record,
                    action="indexing.cancelled",
                    actor_user_id=actor_user_id,
                )
        for cleanup in cleanups:
            await cleanup()
        return len(records)

    fence_document_for_deletion = bridge_to_loop(afence_document_for_deletion)

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
        cleanup: IndexingCleanup | None = None,
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
        cleanup: IndexingCleanup | None = None,
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

    async def afail(
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
            cleanup = record.cleanup
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
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:  # noqa: BLE001 - failure remains terminal/visible
                log.error(
                    "Indexing job %s cleanup failed after job failure "
                    "(error_type=%s)",
                    job_id,
                    type(exc).__name__,
                )

    fail = bridge_to_loop(afail)

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

    async def amark_cancelled(self, job_id: str, *, reason: str) -> None:
        """Mark a running job cancelled after its worker exits."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in TERMINAL_INDEXING_STATUSES:
                return
            cleanup = record.cleanup
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
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:  # noqa: BLE001 - cancellation stays visible
                log.error(
                    "Indexing job %s cleanup failed during cancellation "
                    "(error_type=%s)",
                    job_id,
                    type(exc).__name__,
                )

    mark_cancelled = bridge_to_loop(amark_cancelled)

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

    # What waits for a job's cleanup is async above; a sync name reaches it from a thread.
    _call = staticmethod(aio.call_from_thread)

    def _attempt_was_replaced(self, job_id: str, attempt: str | None) -> bool:
        record = self._records.get(job_id)
        return record is not None and record.fence_token != attempt

    def _run_worker(
        self,
        job_id: str,
        work: IndexingWork,
        cancel_event: threading.Event,
        attempt: str | None,
    ) -> None:
        """Run one attempt; a resume after its pause hands the job to the next one."""
        handle = IndexingJobHandle(self, job_id, cancel_event)
        try:
            work(handle)
            with self._lock:
                if not self._attempt_was_replaced(job_id, attempt):
                    self.complete(job_id)
        except Exception as exc:
            with self._lock:
                if self._attempt_was_replaced(job_id, attempt):
                    log.warning(
                        "Indexing job %s: a paused attempt failed after its resume "
                        "(error_type=%s); the next attempt decides the job.",
                        job_id,
                        type(exc).__name__,
                    )
                    return
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
                if record is not None and record.status in TERMINAL_INDEXING_STATUSES:
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
            aio.start_legacy_segment(
                partial(
                    self._run_worker,
                    job_id,
                    record.work,
                    record.cancel_event,
                    record.fence_token,
                )
            )

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

    asubmit = inline_on_loop(submit)
    aget = inline_on_loop(get)
    aexecution_spec = inline_on_loop(execution_spec)
    alist = inline_on_loop(list)
    ahas_active_job = inline_on_loop(has_active_job)
    ahas_active_document_job = inline_on_loop(has_active_document_job)
    asubscribe = inline_on_loop(subscribe)
    aset_total = inline_on_loop(set_total)
    acompleted_document_ids = inline_on_loop(completed_document_ids)
    aembedding_receipt = inline_on_loop(embedding_receipt)
    acontext_batch_checkpoints = inline_on_loop(context_batch_checkpoints)
    acheckpoint_context_batch = inline_on_loop(checkpoint_context_batch)
    aset_phase = inline_on_loop(set_phase)
    acheckpoint_document = inline_on_loop(checkpoint_document)
    apause = inline_on_loop(pause)
    aresume = inline_on_loop(resume)
    adue_auto_resumes = inline_on_loop(due_auto_resumes)
    astop_auto_resume = inline_on_loop(stop_auto_resume)
    aresume_raw_by_user_choice = inline_on_loop(resume_raw_by_user_choice)
    araw_by_user_choice = inline_on_loop(raw_by_user_choice)
    aprogress = inline_on_loop(progress)
    acomplete = inline_on_loop(complete)
    acomplete_raw_by_user_choice = inline_on_loop(complete_raw_by_user_choice)
    asupersede = inline_on_loop(supersede)
    adocument_started = inline_on_loop(document_started)
    adocument_progress = inline_on_loop(document_progress)
    adocument_completed = inline_on_loop(document_completed)


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
