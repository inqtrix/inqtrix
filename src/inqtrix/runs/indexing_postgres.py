"""Durable knowledge-indexing records and events in Postgres.

Same public surface as the in-memory
:class:`~inqtrix.server.indexing.IndexingJobStore` — the indexing routers
and :class:`~inqtrix.services.indexing_service.IndexingService` cannot
tell the backends apart. The durable twin of
:class:`~inqtrix.runs.postgres_store.PostgresRunStore`, built from the
same parts and sharing its worker stack; the differences are the
indexing domain itself: per-document progress, operation-kind and revision
identity, one active collection-generation publication slot per collection,
a per-collection history cap, and no separate share layer.

Two execution modes (mirroring the run store):

* ``queue is None`` (``INQTRIX_STORAGE_BACKEND=postgres`` alone):
  records and events are durable; execution stays in this process with
  the same daemon-thread dispatch as the in-memory store.
* ``queue`` set (``INQTRIX_QUEUE_BACKEND=valkey``): accepted jobs are
  persisted and dispatched to the indexing stream; ``inqtrix-worker``
  claims and executes them from canonical Postgres source revisions. The job
  row is the source of truth.

The storage layer is async (asyncpg) while this surface is sync (the
router and the job handle call it from worker threads); the store owns
one background event loop and funnels every database operation through
it. Sequence numbers are allocated via
``UPDATE indexing_jobs SET event_seq = event_seq + 1 RETURNING event_seq``
— gap-free across processes, which keeps the SSE stream byte-compatible
with the in-memory store.

The schema-agnostic plumbing — the ``_call``/``close``/``_session``
async-loop bridge, the background-loop construction, the no-queue
in-process dispatch loop, and the DB-polling SSE subscription — lives
once in :class:`~inqtrix.runs.durable_store.DurableJobStoreBase` and
:class:`~inqtrix.runs.durable_store.PollingJobSubscription`, shared with
the run store. Only the schema-bearing bodies stay here: the SQL, the
visibility predicate, and this store's history-cap / active-collection /
progress semantics.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from inqtrix.storage.migration_contract import (
    assert_schema_head,
)
from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import SharePermission
from inqtrix.contextualization_circuit import (
    CircuitAdmission,
    ContextualizationCircuitPermit,
    ContextualizationCircuitState,
    ContextualizationCircuitView,
    circuit_admission,
)
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.indexing_auto_resume import (
    AutoResumePolicy,
    DueResume,
    is_due,
    plan_pause,
    public_plan,
)
from inqtrix.knowledge.stores.ports import CollectionNotFound
from inqtrix.runs.durable_store import (
    DEFAULT_TENANT,
    DurableJobStoreBase,
    PollingJobSubscription,
    _LocalJob,
)
from inqtrix.server.indexing import (
    ACTIVE_INDEXING_STATUS_VALUES,
    FencedIndexingJobHandle,
    TERMINAL_INDEXING_EVENTS,
    TERMINAL_INDEXING_STATUSES,
    IndexingExecutionSpec,
    IndexingJobConflict,
    IndexingJobHandle,
    IndexingJobNotFound,
    IndexingJobRecord,
    IndexingJobStatus,
    IndexingOperationKind,
    IndexingQueueFull,
    IndexingResumeUnavailable,
    IndexingWork,
    _contextualization_documents,
    _log_auto_resume_plan,
    _log_automatic_resume,
    _log_landed_pause,
    _progress_mark,
    _with_auto_resume,
    _with_document_progress,
    _with_contextualization_batch,
    _without_document_progress,
    _without_contextualization_document,
    build_indexing_event,
    build_indexing_job_summary,
    new_indexing_job_id,
)
from inqtrix.storage.indexing_orm import (
    contextualization_provider_circuits,
    indexing_job_events,
    indexing_jobs,
)
from inqtrix.storage.knowledge_orm import knowledge_collections
from inqtrix.storage.resource_access import (
    LockedResourceAccess,
    append_resource_effects,
    lock_resource_access,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from inqtrix.auth.principal import UserContext
    from inqtrix.runs.indexing_queue import ValkeyIndexingQueue

log = logging.getLogger("inqtrix")

_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_INDEXING_STATUSES)
_ACTIVE_VALUES = ACTIVE_INDEXING_STATUS_VALUES

_RESTART_ORPHAN_VALUES = (
    IndexingJobStatus.QUEUED.value,
    IndexingJobStatus.RUNNING.value,
    IndexingJobStatus.CANCELLING.value,
)
"""In-process work states whose callable is lost with an API restart.

Paused jobs are deliberately excluded: their durable checkpoint and explicit
resume/cancel decision remain valid across process and dependency outages.
Queue-backed deployments recover the execution states through stream reclaim
and claim fencing instead of enabling the in-process orphan sweep.
"""

_ACTIVE_GENERATION_CONSTRAINT = "uq_indexing_jobs_active_collection"
_ACTIVE_REVISION_CONSTRAINT = "uq_indexing_jobs_active_revision"


@dataclass(frozen=True)
class ClaimedIndexingJob:
    """Result of a successful worker claim on a queued indexing operation."""

    job_id: str
    tenant_id: str
    attempt: int
    collection_id: str
    embedding_model: str
    operation_kind: IndexingOperationKind = IndexingOperationKind.COLLECTION_GENERATION
    document_id: str | None = None
    revision_id: str | None = None
    checkpoint: dict[str, Any] | None = None
    generation_id: str = ""
    # Persisted attribution of the submitter, so the worker can meter the
    # provider work against the canonical user UUID without a live principal.
    created_by_user_id: uuid.UUID | None = None
    created_by_tenant_id: str | None = None
    cancel_requested: bool = False


@dataclass(frozen=True)
class _MaintenanceAction:
    """One retention or recovery mutation applied under lifecycle locks."""

    action: str
    error: dict[str, str] | None = None


class PostgresIndexingJobStore(DurableJobStoreBase):
    """Durable indexing registry with the in-memory store's public surface.

    Args:
        engine: Async engine OWNED by this store. asyncpg pools are
            event-loop-affine: every connection must live on the store's
            background loop, so the engine is deliberately NOT shared
            with the identity/file/knowledge backends.
        app_role: Restricted database role assumed per transaction
            (``SET LOCAL ROLE``) so forced RLS applies.
        queue: Indexing dispatch queue; ``None`` keeps execution in this
            process (durable records, unchanged threading).
        max_concurrent: In-process execution slots (no-queue mode) and
            part of the queue-saturation formula.
        max_queue_size: Waiting-job bound; exceeding it with all slots
            busy rejects submissions (HTTP 429 upstream).
        completed_ttl_seconds: Retention for terminal jobs; lazy cleanup
            deletes older rows (events cascade).
        history_limit: Maximum terminal jobs retained per collection
            (the inline "last N" history); older terminal rows for a
            collection are deleted beyond this count.
        worker_id: Identity stamped into ``claimed_by`` for jobs this
            process executes or fences.
        recover_orphans: Whether this instance may blanket-fail
            queued/running/cancelling rows left by a previous process.
            Durable paused rows are never restart orphans. ``None``
            infers from ``queue`` (no-queue single API process sweeps,
            queue mode never); the queue-backed WORKER passes an
            explicit ``False`` — its ``queue=None`` is claim-mode
            wiring, stream reclaim owns crash recovery there.

    Tenancy: job rows live in the single deployment tenant (``default``)
    at the RLS layer — per-user visibility is the
    ``(created_by_user_id, created_by_tenant_id)`` predicate, exactly like
    the run store and the in-memory indexing store.
    """

    _loop_thread_name = "inqtrix-index-db"
    _dispatch_thread_prefix = "inqtrix-reindex"
    _job_kind = "Durable indexing job"

    def _enter_execution_telemetry(
        self, stack: Any, entity_id: str, claimed: Any
    ) -> None:
        """Root span + log context for NO-QUEUE indexing executions.

        Parity with :class:`~inqtrix.worker.indexing_loop.IndexingWorkerLoop`
        (which opens the same span): without it, postgres-without-worker
        deployments emit the embedding/contextualization spans as ORPHAN
        root traces and their log lines carry no job correlation at all.
        """
        from inqtrix.observability import semconv
        from inqtrix.observability.context import (
            bind_log_context,
            reset_log_context,
        )
        from inqtrix.observability.otel import operation_span

        tenant_id = str(
            getattr(claimed, "created_by_tenant_id", "") or DEFAULT_TENANT
        )
        stack.enter_context(
            operation_span(
                "inqtrix.indexing",
                {
                    semconv.INQTRIX_RUN_ID: entity_id,
                    semconv.INQTRIX_TENANT: tenant_id,
                    semconv.INQTRIX_ATTEMPT: int(
                        getattr(claimed, "attempt", 1) or 1
                    ),
                    semconv.LANGFUSE_TRACE_NAME: "indexing",
                },
            )
        )
        tokens = bind_log_context(run_id=entity_id, tenant=tenant_id)
        stack.callback(reset_log_context, tokens)

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        queue: "ValkeyIndexingQueue | None" = None,
        max_concurrent: int,
        max_queue_size: int,
        completed_ttl_seconds: int,
        history_limit: int,
        worker_id: str,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
        recover_orphans: bool | None = None,
        auto_resume: AutoResumePolicy | None = None,
        clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        # The engine/session/loop/dispatch plumbing lives in
        # DurableJobStoreBase; this store adds only its sizing and
        # retention state.
        super().__init__(
            engine=engine,
            app_role=app_role,
            worker_id=worker_id,
            queue=queue,
            recover_orphans=recover_orphans,
        )
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._completed_ttl_seconds = completed_ttl_seconds
        self._history_limit = history_limit
        self._auto_resume = auto_resume or AutoResumePolicy(max_attempts=0)
        self._clock = clock
        self._random = random_source
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled
        self._cleanup_callbacks: dict[str, Any] = {}
        # The restart sweep runs eagerly so orphans of the previous
        # process are terminal before the first client read — including
        # the active-collection checks the deletion services consult. A
        # failure keeps the one-shot flag set; the lazy first-cleanup
        # fallback remains, so startup gains no new hard dependency.
        if self._sweep_orphans:
            try:
                self._call(self._startup_cleanup_db())
            except Exception as boot_exc:  # noqa: BLE001 — lazy cleanup remains
                log.warning(
                    "Startup cleanup failed; it will be retried on the next database "
                    "access (error_type=%s).",
                    type(boot_exc).__name__,
                )

    # -- public surface (IndexingJobStore parity) ------------------------- #

    def submit(
        self,
        *,
        collection_id: str,
        collection_name: str,
        embedding_model: str,
        work: IndexingWork,
        cleanup: Any | None = None,
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
        """Persist one queued indexing operation, then dispatch it.

        Raises:
            IndexingJobConflict: The collection already has an active
                collection-generation operation, or the revision id is bound
                to another collection. Authorized retries for the same
                immutable revision return the existing summary.
            IndexingQueueFull: The waiting queue is full and every slot
                is busy (queue-mode counts are cluster-wide via the
                database, exactly like the run store).
        """
        # Best-effort fence before admission (bounded by grace +
        # throttle): a lost execution should stop blocking the
        # collection or holding a slot within that window.
        self._expire_lost_executions()
        summary = self._call(
            self._submit_db(
                collection_id=collection_id,
                collection_name=collection_name,
                embedding_model=embedding_model,
                index_id=index_id,
                workspace_id=workspace_id,
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
                generation_id=generation_id,
                operation_kind=IndexingOperationKind(operation_kind).value,
                document_id=document_id,
                revision_id=revision_id,
                checkpoint=dict(checkpoint or {}),
            )
        )
        job_id = summary["job_id"]
        if self._queue is not None:
            try:
                self._queue.enqueue(job_id=job_id, tenant_id=DEFAULT_TENANT)
            except Exception as exc:  # noqa: BLE001 — row is committed; visible
                log.warning(
                    "Dispatch for indexing job %s could not be sent; the "
                    "reconciler will retry it (error_type=%s).",
                    job_id,
                    type(exc).__name__,
                )
        else:
            with self._lock:
                # A lost-response retry can return the already-persisted active
                # job. Attach local work only when this process has not done so
                # yet; overwriting a running/parked closure or enqueueing the
                # same local job twice would defeat submission idempotency.
                if job_id not in self._local:
                    self._local[job_id] = _LocalJob(work=work)
                    if cleanup is not None:
                        self._cleanup_callbacks[job_id] = cleanup
                    self._pending.append(job_id)
                    self._dispatch_locked()
        return summary

    def get(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return a public summary for *job_id*."""
        self._expire_lost_executions()
        return self._call(self._summary_db(job_id, workspace_id, visible_to))

    def execution_spec(self, job_id: str) -> IndexingExecutionSpec:
        """Return private canonical identity for resume reconstruction."""

        return self._call(self._execution_spec_db(job_id))

    def list(
        self,
        *,
        collection_id: str | None = None,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Return summaries for visible jobs, newest first."""
        self._expire_lost_executions()
        return self._call(self._list_db(collection_id, workspace_id, visible_to))

    def has_active_job(self, collection_id: str) -> bool:
        """Whether *collection_id* has a queued/running/cancelling job."""
        # Best-effort fence first (bounded by grace + throttle): the
        # deletion services consult this check, and a lost execution
        # should stop vetoing collection maintenance within that window.
        # The boolean itself must keep answering even when the
        # maintenance transaction fails, so fence errors degrade loudly
        # instead of failing the read.
        try:
            self._expire_lost_executions()
        except Exception as fence_exc:  # noqa: BLE001 — read must answer
            log.warning(
                "Lost-execution detection before the activity check failed; the check "
                "continues without it (error_type=%s).",
                type(fence_exc).__name__,
            )
        return self._call(self._has_active_job_db(collection_id))

    def has_active_document_job(self, document_id: str) -> bool:
        """Whether a document-revision job can still publish this document."""

        try:
            self._expire_lost_executions()
        except Exception as fence_exc:  # noqa: BLE001 — read must answer
            log.warning(
                "Lost-execution detection before the activity check failed; the check "
                "continues without it (error_type=%s).",
                type(fence_exc).__name__,
            )
        return self._call(self._has_active_document_job_db(document_id))

    def cancel(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Request cancellation for a queued or running indexing operation."""
        summary = self._call(
            self._cancel_db(job_id, workspace_id, visible_to, actor_user_id)
        )
        with self._lock:
            local = self._local.get(job_id)
            if local is not None:
                local.cancel_event.set()
                if summary["status"] == IndexingJobStatus.CANCELLED.value:
                    try:
                        self._pending.remove(job_id)
                    except ValueError:
                        pass
                    local.work = None
                    cleanup = self._cleanup_callbacks.pop(job_id, None)
                    if cleanup is not None:
                        cleanup()
        return summary

    def fence_collection_for_deletion(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> int:
        """Terminally fence every active attempt for an owned collection."""

        job_ids = self._call(
            self._fence_collection_for_deletion_db(
                collection_id, actor_user_id=actor_user_id
            )
        )
        self._forget_fenced_local_jobs(job_ids)
        return len(job_ids)

    def fence_document_for_deletion(
        self,
        collection_id: str,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> int:
        """Fence only revision jobs for one durable deletion target."""

        job_ids = self._call(
            self._fence_document_for_deletion_db(
                collection_id,
                document_id,
                actor_user_id=actor_user_id,
            )
        )
        self._forget_fenced_local_jobs(job_ids)
        return len(job_ids)

    def _forget_fenced_local_jobs(self, job_ids: tuple[str, ...]) -> None:
        with self._lock:
            for job_id in job_ids:
                local = self._local.get(job_id)
                if local is not None:
                    local.cancel_event.set()
                    local.work = None
                try:
                    self._pending.remove(job_id)
                except ValueError:
                    pass
                cleanup = self._cleanup_callbacks.pop(job_id, None)
                if cleanup is not None:
                    cleanup()

    def resume(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
        work: IndexingWork | None = None,
        cleanup: Any | None = None,
        automatic: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Requeue a paused durable job without losing its checkpoint.

        ``automatic`` is the sweep's resume (a due dependency pause only, the
        attempt counted); a person's resume clears the plan.
        """
        rebound = False
        with self._lock:
            local = self._local.get(job_id)
            if (
                self._queue is None
                and (local is None or local.work is None)
                and work is not None
            ):
                local = _LocalJob(work=work)
                self._local[job_id] = local
                if cleanup is not None:
                    self._cleanup_callbacks[job_id] = cleanup
                rebound = True
            execution_available = bool(
                self._queue is not None
                or (local is not None and local.work is not None)
            )
        try:
            summary = self._call(
                self._resume_db(
                    job_id,
                    workspace_id,
                    visible_to,
                    actor_user_id,
                    raw_by_user_choice=False,
                    execution_available=execution_available,
                    automatic=automatic,
                    now=now,
                )
            )
        except Exception:
            if rebound:
                self._discard_rebound_work(job_id)
            raise
        if summary["status"] != IndexingJobStatus.QUEUED.value:
            if rebound:
                self._discard_rebound_work(job_id)
            return summary
        if self._queue is not None:
            self._queue.enqueue(job_id=job_id, tenant_id=DEFAULT_TENANT)
            return summary
        with self._lock:
            local = self._local.get(job_id)
            if local is None or local.work is None:
                raise RuntimeError(
                    "resume precondition changed after durable transition"
                )
            local.cancel_event.clear()
            if local.park_in_flight:
                local.resume_requested = True
            else:
                local.parked = False
                self._pending.append(job_id)
                self._dispatch_locked()
        return summary

    @property
    def auto_resume_policy(self) -> AutoResumePolicy:
        """How this store's dependency pauses resume on their own."""
        return self._auto_resume

    def due_auto_resumes(self, *, now: float) -> list[DueResume]:
        """Dependency pauses whose automatic resume is due at ``now``."""
        return self._call(self._due_auto_resumes_db(now))

    async def _due_auto_resumes_db(self, now: float) -> list[DueResume]:
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(
                            indexing_jobs.c.job_id,
                            indexing_jobs.c.error,
                            # Only the plan: a checkpoint also carries every
                            # embedding receipt of the job.
                            indexing_jobs.c.checkpoint["auto_resume"].label(
                                "auto_resume"
                            ),
                        ).where(
                            indexing_jobs.c.status
                            == IndexingJobStatus.PAUSED_DEPENDENCY.value
                        )
                    )
                )
                .mappings()
                .all()
            )
        due: list[DueResume] = []
        for row in rows:
            plan = row["auto_resume"]
            if is_due(plan, now):
                due.append(
                    DueResume(
                        job_id=str(row["job_id"]),
                        error_type=str((row["error"] or {}).get("type") or ""),
                        next_at=float(plan["next_at"]),
                    )
                )
        return due

    def stop_auto_resume(self, job_id: str, reason: str) -> None:
        """End a job's automatic resumes visibly; it stays paused for a person."""
        plan = self._call(self._stop_auto_resume_db(job_id, reason))
        _log_auto_resume_plan(job_id, plan)

    async def _stop_auto_resume_db(
        self, job_id: str, reason: str
    ) -> dict[str, Any] | None:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(
                session, job_id=job_id
            )
            access = None
            if locked is not None:
                row, access = locked
            else:
                # The creator lost access: the stop is still recorded, there
                # is just nobody left to invalidate a view for.
                row = (
                    (
                        await session.execute(
                            select(indexing_jobs)
                            .where(indexing_jobs.c.job_id == job_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
            if (
                row is None
                or row["status"] != IndexingJobStatus.PAUSED_DEPENDENCY.value
            ):
                return None
            checkpoint = dict(row["checkpoint"] or {})
            plan = dict(checkpoint.get("auto_resume") or {})
            plan.update(next_at=None, stopped=reason)
            await session.execute(
                update(indexing_jobs)
                .where(indexing_jobs.c.job_id == job_id)
                .values(checkpoint=_with_auto_resume(checkpoint, plan))
            )
            await self._append_events_db(
                session,
                job_id,
                row["tenant_id"],
                "inqtrix.index.auto_resume_stopped",
                {"reason": reason, "attempts": int(plan.get("attempts", 0))},
            )
            if access is not None:
                await self._append_collection_effects(
                    session,
                    row=row,
                    access=access,
                    action="indexing.auto_resume_stopped",
                )
            return plan

    def resume_raw_by_user_choice(
        self,
        job_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        actor_user_id: uuid.UUID | None = None,
        work: IndexingWork | None = None,
        cleanup: Any | None = None,
    ) -> dict[str, Any]:
        """Reset a paused durable job and requeue its explicit raw build."""
        rebound = False
        with self._lock:
            local = self._local.get(job_id)
            if (
                self._queue is None
                and (local is None or local.work is None)
                and work is not None
            ):
                local = _LocalJob(work=work)
                self._local[job_id] = local
                if cleanup is not None:
                    self._cleanup_callbacks[job_id] = cleanup
                rebound = True
            execution_available = bool(
                self._queue is not None
                or (local is not None and local.work is not None)
            )
        try:
            summary = self._call(
                self._resume_db(
                    job_id,
                    workspace_id,
                    visible_to,
                    actor_user_id,
                    raw_by_user_choice=True,
                    execution_available=execution_available,
                )
            )
        except Exception:
            if rebound:
                self._discard_rebound_work(job_id)
            raise
        if summary["status"] != IndexingJobStatus.QUEUED.value:
            if rebound:
                self._discard_rebound_work(job_id)
            return summary
        if self._queue is not None:
            self._queue.enqueue(job_id=job_id, tenant_id=DEFAULT_TENANT)
            return summary
        with self._lock:
            local = self._local.get(job_id)
            if local is None or local.work is None:
                raise RuntimeError(
                    "resume precondition changed after durable transition"
                )
            local.cancel_event.clear()
            if local.park_in_flight:
                local.resume_requested = True
            else:
                local.parked = False
                self._pending.append(job_id)
            self._dispatch_locked()
        return summary

    def _discard_rebound_work(self, job_id: str) -> None:
        """Undo a local rebind when the durable paused-to-queued CAS fails."""

        with self._lock:
            self._local.pop(job_id, None)
            self._cleanup_callbacks.pop(job_id, None)

    def raw_by_user_choice(self, job_id: str) -> bool:
        checkpoint = self._call(self._checkpoint_db(job_id))
        return checkpoint.get("raw_by_user_choice") is True

    def subscribe(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> PollingJobSubscription:
        """Subscribe after a durable cursor, then tail new events."""
        self._expire_lost_executions()
        tenant_id, replay = self._call(
            self._replay_db(
                job_id,
                workspace_id,
                visible_to,
                after_sequence=max(0, int(after_sequence)),
            )
        )
        return PollingJobSubscription(
            self,
            job_id,
            tenant_id,
            replay,
            after_sequence=after_sequence,
            terminal_events=TERMINAL_INDEXING_EVENTS,
            thread_label="inqtrix-index-events",
        )

    def unsubscribe(self, job_id: str, queue: Queue) -> None:
        """Parity no-op: polling subscriptions detach via ``close()``."""

    def set_total(
        self, job_id: str, total_documents: int, *, fence_attempt: int | None = None
    ) -> None:
        """Record the job's total document count and emit a progress step."""
        self._call(
            self._progress_db(
                job_id,
                total_documents=max(0, int(total_documents)),
                fence_attempt=fence_attempt,
            )
        )

    def progress(
        self,
        job_id: str,
        *,
        completed_documents: int,
        current_document_title: str = "",
        fence_attempt: int | None = None,
    ) -> None:
        """Update completed-document progress and emit a progress event."""
        self._call(
            self._progress_db(
                job_id,
                completed_documents=max(0, int(completed_documents)),
                current_document_title=current_document_title,
                fence_attempt=fence_attempt,
            )
        )

    def completed_document_ids(self, job_id: str) -> frozenset[str]:
        row = self._call(self._checkpoint_db(job_id))
        return frozenset(str(value) for value in row.get("completed_document_ids", []))

    def embedding_receipt(self, job_id: str, document_id: str) -> dict[str, Any] | None:
        checkpoint = self._call(self._checkpoint_db(job_id))
        receipts = checkpoint.get("embedding_receipts")
        if not isinstance(receipts, dict):
            return None
        receipt = receipts.get(document_id)
        return dict(receipt) if isinstance(receipt, dict) else None

    def context_batch_checkpoints(
        self, job_id: str, document_id: str
    ) -> list[dict[str, Any]]:
        checkpoint = self._call(self._checkpoint_db(job_id))
        current = _contextualization_documents(checkpoint).get(document_id)
        if current is None:
            return []
        batches = current.get("batches", [])
        return [dict(item) for item in batches if isinstance(item, dict)]

    def checkpoint_context_batch(
        self,
        job_id: str,
        document_id: str,
        checkpoint: dict[str, Any],
        *,
        fence_attempt: int | None = None,
    ) -> None:
        self._call(
            self._checkpoint_context_batch_db(
                job_id,
                document_id,
                checkpoint,
                fence_attempt=fence_attempt,
            )
        )

    def set_phase(
        self,
        job_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
        fence_attempt: int | None = None,
    ) -> None:
        self._call(
            self._progress_db(
                job_id,
                phase=str(phase),
                current_batch=max(0, int(current_batch)),
                total_batches=max(0, int(total_batches)),
                fence_attempt=fence_attempt,
            )
        )

    def checkpoint_document(
        self,
        job_id: str,
        document_id: str,
        *,
        embedding_receipt: dict[str, Any] | None = None,
        fence_attempt: int | None = None,
    ) -> None:
        self._call(
            self._checkpoint_document_db(
                job_id,
                document_id,
                embedding_receipt=embedding_receipt,
                fence_attempt=fence_attempt,
            )
        )

    def pause(
        self,
        job_id: str,
        status: IndexingJobStatus,
        message: str,
        *,
        error_type: str,
        fence_attempt: int | None = None,
    ) -> bool:
        landed = self._call(
            self._pause_db(
                job_id,
                status,
                message,
                error_type=error_type,
                fence_attempt=fence_attempt,
            )
        )
        if landed:
            with self._lock:
                local = self._local.get(job_id)
                if local is not None:
                    local.parked = True
                    local.park_in_flight = True
        return landed

    def complete(self, job_id: str, *, fence_attempt: int | None = None) -> bool:
        """Mark the job completed.

        Returns:
            ``True`` when the terminal transition landed; ``False`` when
            absorbed (already terminal or fenced out) — the worker must
            NOT ack the dispatch message in that case.
        """
        landed = self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.COMPLETED,
                fence_attempt=fence_attempt,
                clear_title=True,
                event_type="inqtrix.index.completed",
                extra={"status": "completed"},
            )
        )
        if landed:
            with self._lock:
                self._cleanup_callbacks.pop(job_id, None)
        return landed

    def complete_raw_by_user_choice(
        self,
        job_id: str,
        *,
        fence_attempt: int | None = None,
    ) -> bool:
        landed = self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.READY_RAW_BY_USER_CHOICE,
                fence_attempt=fence_attempt,
                clear_title=True,
                event_type="inqtrix.index.ready_raw_by_user_choice",
                extra={"status": "ready_raw_by_user_choice"},
            )
        )
        if landed:
            with self._lock:
                self._cleanup_callbacks.pop(job_id, None)
        return landed

    def fail(
        self,
        job_id: str,
        message: str,
        *,
        error_type: str = "server_error",
        fence_attempt: int | None = None,
    ) -> bool:
        """Mark the job failed with a sanitized error payload."""
        error = {"message": sanitize_error(message), "type": error_type}
        landed = self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.FAILED,
                fence_attempt=fence_attempt,
                error=error,
                event_type="inqtrix.index.failed",
                extra={"status": "failed", "error": error},
            )
        )
        if landed:
            self._run_local_cleanup(job_id)
        return landed

    def supersede(
        self,
        job_id: str,
        *,
        reason: str = "newer_revision_requested",
        fence_attempt: int | None = None,
    ) -> bool:
        """Terminalize a stale document revision under the worker fence."""
        return self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.SUPERSEDED,
                fence_attempt=fence_attempt,
                clear_title=True,
                event_type="inqtrix.index.superseded",
                extra={"status": "superseded", "reason": reason},
            )
        )

    def mark_cancelled(
        self, job_id: str, *, reason: str, fence_attempt: int | None = None
    ) -> bool:
        """Mark a running job cancelled after its worker observed it."""
        landed = self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.CANCELLED,
                fence_attempt=fence_attempt,
                event_type="inqtrix.index.cancelled",
                extra={"status": "cancelled", "reason": reason},
            )
        )
        if landed:
            self._run_local_cleanup(job_id)
        return landed

    def _run_local_cleanup(self, job_id: str) -> None:
        with self._lock:
            cleanup = self._cleanup_callbacks.pop(job_id, None)
        if cleanup is None:
            return
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001 - terminal state remains authoritative
            log.error(
                "Indexing cleanup for %s failed (error_type=%s)",
                job_id,
                type(exc).__name__,
            )

    def document_started(
        self, job_id: str, document_id: str, *, fence_attempt: int | None = None
    ) -> None:
        """Emit the stable id of one document entering active processing."""
        self._call(
            self._document_event_db(
                job_id,
                document_id,
                event_type="inqtrix.index.document_started",
                fence_attempt=fence_attempt,
            )
        )

    def document_progress(
        self,
        job_id: str,
        document_id: str,
        phase: str,
        *,
        current_batch: int = 0,
        total_batches: int = 0,
        fence_attempt: int | None = None,
    ) -> None:
        """Persist and emit phase progress for one exact document."""
        self._call(
            self._document_progress_db(
                job_id,
                document_id,
                phase,
                current_batch=current_batch,
                total_batches=total_batches,
                fence_attempt=fence_attempt,
            )
        )

    def document_completed(
        self,
        job_id: str,
        document_id: str,
        *,
        fence_attempt: int | None = None,
        context_omitted_chunks: int = 0,
    ) -> None:
        """Emit a per-document completion event (one document finished embedding)."""
        self._call(
            self._document_event_db(
                job_id,
                document_id,
                event_type="inqtrix.index.document_completed",
                fence_attempt=fence_attempt,
                outcome="embedded",
                context_omitted_chunks=context_omitted_chunks,
            )
        )

    # -- worker surface --------------------------------------------------- #

    def claim_for_execution(
        self, job_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedIndexingJob | None:
        """Atomically claim one indexing operation for execution."""
        return self._call(
            self._claim_db(job_id, tenant_id, allow_takeover=allow_takeover)
        )

    def cancel_requested_jobs(self, job_ids: dict[str, str]) -> set[str]:
        """Subset of ``job_ids`` (id -> tenant) with a pending cancel."""
        return self._call(self._cancel_requested_db(job_ids))

    def attempt_cancel_requested(
        self,
        job_id: str,
        *,
        fence_attempt: int,
    ) -> bool:
        """Whether this exact claimed attempt has a durable cancel request."""

        return self._call(
            self._attempt_cancel_requested_db(
                job_id,
                fence_attempt=fence_attempt,
            )
        )

    def stale_queued_jobs(self, *, older_than_seconds: float) -> list[tuple[str, str]]:
        """Queued ``(job_id, tenant_id)`` pairs older than the threshold."""
        return self._call(self._stale_queued_db(older_than_seconds))

    @property
    def contextualization_circuit(self) -> "PostgresIndexingJobStore":
        """Durable circuit authority shared by all worker replicas."""

        return self

    def acquire_contextualization_circuit(
        self,
        *,
        provider_key: str,
        model: str,
        cooldown_seconds: float,
        probe_lease_seconds: float,
    ) -> ContextualizationCircuitPermit | None:
        """Atomically grant a normal call or the sole half-open probe."""

        return self._call(
            self._acquire_contextualization_circuit_db(
                provider_key=provider_key,
                model=model,
                cooldown_seconds=cooldown_seconds,
                probe_lease_seconds=probe_lease_seconds,
            )
        )

    def record_contextualization_circuit_success(
        self,
        permit: ContextualizationCircuitPermit,
    ) -> None:
        """Close a half-open circuit only for its current probe token."""

        self._call(self._record_contextualization_circuit_success_db(permit))

    def record_contextualization_circuit_failure(
        self,
        permit: ContextualizationCircuitPermit,
        *,
        error_type: str,
    ) -> None:
        """Open the shared circuit after one transient provider failure."""

        self._call(
            self._record_contextualization_circuit_failure_db(
                permit,
                error_type=error_type,
            )
        )

    def contextualization_circuit_view(
        self,
        *,
        provider_key: str,
        model: str,
    ) -> ContextualizationCircuitView | None:
        """Read one shared circuit without changing it."""

        return self._call(
            self._contextualization_circuit_view_db(
                provider_key=provider_key, model=model
            )
        )

    async def _contextualization_circuit_view_db(
        self,
        *,
        provider_key: str,
        model: str,
    ) -> ContextualizationCircuitView | None:
        async with self._session(DEFAULT_TENANT) as session:
            row = (
                (
                    await session.execute(
                        select(contextualization_provider_circuits).where(
                            contextualization_provider_circuits.c.tenant_id
                            == DEFAULT_TENANT,
                            contextualization_provider_circuits.c.provider_key
                            == str(provider_key).strip(),
                            contextualization_provider_circuits.c.model
                            == str(model).strip(),
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return ContextualizationCircuitView(
            state=ContextualizationCircuitState(row["state"]),
            cooldown_until=float(row["cooldown_until"] or 0),
            probe_lease_until=(
                None
                if row["probe_lease_until"] is None
                else float(row["probe_lease_until"])
            ),
        )

    # -- in-process execution hooks (no-queue mode) ---------------------- #

    def _make_handle(self, job_id: str, cancel_event) -> IndexingJobHandle:
        return IndexingJobHandle(self, job_id, cancel_event)

    def _make_claimed_handle(
        self,
        job_id: str,
        cancel_event,
        claimed: ClaimedIndexingJob,
    ) -> FencedIndexingJobHandle:
        """Carry the in-process claim attempt through every publication write."""

        return FencedIndexingJobHandle(
            self,
            job_id,
            cancel_event,
            claimed.attempt,
        )

    def _auto_complete(self, job_id: str) -> None:
        # Usually a no-op (the work body already completed the job);
        # warn_on_noop=False keeps the genuine fenced-out warning on the
        # public complete()/fail() path meaningful.
        self._call(
            self._terminal_db(
                job_id,
                IndexingJobStatus.COMPLETED,
                fence_attempt=None,
                clear_title=True,
                event_type="inqtrix.index.completed",
                extra={"status": "completed"},
                warn_on_noop=False,
            )
        )

    # -- subscription poll bridge ----------------------------------------- #

    def _events_after(
        self, job_id: str, tenant_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        # Fence on the poll path: an already attached stream terminalizes
        # a lost job on its next poll and receives the terminal event.
        self._expire_lost_executions()
        return self._call(self._events_after_db(job_id, tenant_id, after_sequence))

    # -- lost-execution fence (no-queue mode) ----------------------------- #

    async def _lost_execution_candidates_db(
        self, grace_seconds: float
    ) -> list[str]:
        async with self._session(DEFAULT_TENANT) as session:
            return list(
                (
                    await session.execute(
                        select(indexing_jobs.c.job_id).where(
                            indexing_jobs.c.status.in_(_RESTART_ORPHAN_VALUES),
                            func.coalesce(
                                indexing_jobs.c.started_at,
                                indexing_jobs.c.created_at,
                            )
                            < time.time() - grace_seconds,
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def _expire_lost_executions_db(self, entity_ids: list[str]) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(
                session, execution_lost_ids=frozenset(entity_ids)
            )
            return True

    async def _startup_cleanup_db(self) -> None:
        """Run the indexing store's lazy cleanup once in its own transaction.

        Covers the restart sweep plus this store's collection-locked
        retention and history eviction.
        """
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)

    # -- async database operations ---------------------------------------- #

    async def _has_active_job_db(self, collection_id: str) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            active = await session.scalar(
                select(indexing_jobs.c.job_id)
                .where(
                    indexing_jobs.c.collection_id == collection_id,
                    indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                )
                .limit(1)
            )
            return active is not None

    async def _has_active_document_job_db(self, document_id: str) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            active = await session.scalar(
                select(indexing_jobs.c.job_id)
                .where(
                    indexing_jobs.c.document_id == document_id,
                    indexing_jobs.c.operation_kind
                    == IndexingOperationKind.DOCUMENT_REVISION.value,
                    indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                )
                .limit(1)
            )
            return active is not None

    async def _fence_collection_for_deletion_db(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> tuple[str, ...]:
        async with self._session(DEFAULT_TENANT) as session:
            access = await lock_resource_access(
                session,
                tenant_id=DEFAULT_TENANT,
                actor_user_id=actor_user_id,
                resource_type="knowledge_collection",
                resource_table=knowledge_collections,
                id_column=knowledge_collections.c.id,
                resource_id=collection_id,
                owner_column=knowledge_collections.c.created_by_user_id,
                minimum=SharePermission.EDIT,
                restrict_to_workspace_members=(self._restrict_to_workspace_members),
                sharing_enabled=self._sharing_enabled,
            )
            if access is None:
                raise CollectionNotFound(collection_id)
            rows = (
                (
                    await session.execute(
                        select(indexing_jobs.c.job_id)
                        .where(
                            indexing_jobs.c.collection_id == collection_id,
                            indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            job_ids = tuple(str(job_id) for job_id in rows)
            if not job_ids:
                return ()
            now = time.time()
            await session.execute(
                update(indexing_jobs)
                .where(indexing_jobs.c.job_id.in_(job_ids))
                .values(
                    status=IndexingJobStatus.CANCELLED.value,
                    cancel_requested=True,
                    claimed_by=None,
                    attempt=indexing_jobs.c.attempt + 1,
                    fence_token=uuid.uuid4().hex,
                    finished_at=now,
                )
            )
            for job_id in job_ids:
                await self._append_events_db(
                    session,
                    job_id,
                    DEFAULT_TENANT,
                    "inqtrix.index.cancelled",
                    {
                        "status": "cancelled",
                        "reason": "collection_deletion",
                    },
                )
            return job_ids

    async def _fence_document_for_deletion_db(
        self,
        collection_id: str,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> tuple[str, ...]:
        """Fence one target after the deletion ledger fixed edit authority."""

        del actor_user_id
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(indexing_jobs.c.job_id)
                        .where(
                            indexing_jobs.c.collection_id == collection_id,
                            indexing_jobs.c.document_id == document_id,
                            indexing_jobs.c.operation_kind
                            == IndexingOperationKind.DOCUMENT_REVISION.value,
                            indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            job_ids = tuple(str(job_id) for job_id in rows)
            if not job_ids:
                return ()
            now = time.time()
            await session.execute(
                update(indexing_jobs)
                .where(indexing_jobs.c.job_id.in_(job_ids))
                .values(
                    status=IndexingJobStatus.CANCELLED.value,
                    cancel_requested=True,
                    claimed_by=None,
                    attempt=indexing_jobs.c.attempt + 1,
                    fence_token=uuid.uuid4().hex,
                    finished_at=now,
                )
            )
            for job_id in job_ids:
                await self._append_events_db(
                    session,
                    job_id,
                    DEFAULT_TENANT,
                    "inqtrix.index.cancelled",
                    {
                        "status": "cancelled",
                        "reason": "document_deletion",
                    },
                )
            return job_ids

    async def _lock_collection_access_for_job(
        self,
        session: "AsyncSession",
        *,
        job_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[Any, LockedResourceAccess] | None:
        """Lock parent collection access, then the immutable job pointer."""
        pointer = (
            await session.execute(
                select(
                    indexing_jobs.c.collection_id,
                    indexing_jobs.c.created_by_user_id,
                ).where(indexing_jobs.c.job_id == job_id)
            )
        ).first()
        if pointer is None:
            return None
        effective_actor = (
            actor_user_id if actor_user_id is not None else pointer.created_by_user_id
        )
        access = await lock_resource_access(
            session,
            tenant_id=DEFAULT_TENANT,
            actor_user_id=effective_actor,
            resource_type="knowledge_collection",
            resource_table=knowledge_collections,
            id_column=knowledge_collections.c.id,
            resource_id=pointer.collection_id,
            owner_column=knowledge_collections.c.created_by_user_id,
            minimum=SharePermission.EDIT,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        )
        if access is None:
            return None
        row = (
            (
                await session.execute(
                    select(indexing_jobs)
                    .where(indexing_jobs.c.job_id == job_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["collection_id"] != pointer.collection_id:
            return None
        return row, access

    async def _append_collection_effects(
        self,
        session: "AsyncSession",
        *,
        row: Any,
        access: LockedResourceAccess,
        action: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Invalidate the parent collection views in the same transaction.

        The audit row doubles as the Dienststart-Index entry for this
        job state, so outcome/correlation fill the 0072 read-model
        columns (failed/cancelled terminals must not read as success).
        """
        await append_resource_effects(
            session,
            tenant_id=row["tenant_id"],
            actor_user_id=(
                actor_user_id
                if actor_user_id is not None
                else row["created_by_user_id"]
            ),
            owner_user_id=access.owner_user_id,
            action=action,
            resource_type="knowledge_collection",
            resource_id=row["collection_id"],
            scope="indexing",
            outcome=(
                "failure"
                if action.endswith((".failed", ".cancelled"))
                else "success"
            ),
            correlation={"run_id": str(row["job_id"])},
        )

    async def _submit_db(self, **fields: Any) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            collection_id = fields["collection_id"]
            access = await lock_resource_access(
                session,
                tenant_id=DEFAULT_TENANT,
                actor_user_id=fields["created_by_user_id"],
                resource_type="knowledge_collection",
                resource_table=knowledge_collections,
                id_column=knowledge_collections.c.id,
                resource_id=collection_id,
                owner_column=knowledge_collections.c.created_by_user_id,
                minimum=SharePermission.EDIT,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
            )
            if access is None:
                raise CollectionNotFound(collection_id)
            if (
                fields.get("operation_kind")
                == IndexingOperationKind.COLLECTION_GENERATION.value
            ):
                active = await session.scalar(
                    select(func.count())
                    .select_from(indexing_jobs)
                    .where(
                        indexing_jobs.c.collection_id == collection_id,
                        indexing_jobs.c.operation_kind
                        == IndexingOperationKind.COLLECTION_GENERATION.value,
                        indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                    )
                )
                if active:
                    raise IndexingJobConflict(collection_id)
            elif (
                fields.get("operation_kind")
                == IndexingOperationKind.DOCUMENT_REVISION.value
            ):
                existing = await self._active_revision_row(
                    session, fields.get("revision_id")
                )
                if existing is not None:
                    return await self._existing_revision_summary(
                        session,
                        existing,
                        fields=fields,
                    )
            queued = await session.scalar(
                select(func.count())
                .select_from(indexing_jobs)
                .where(indexing_jobs.c.status == IndexingJobStatus.QUEUED.value)
            )
            running = await session.scalar(
                select(func.count())
                .select_from(indexing_jobs)
                .where(
                    indexing_jobs.c.status.in_(
                        (
                            IndexingJobStatus.RUNNING.value,
                            IndexingJobStatus.CANCELLING.value,
                        )
                    )
                )
            )
            if (queued or 0) >= self._max_queue_size and (
                running or 0
            ) >= self._max_concurrent:
                raise IndexingQueueFull("indexing queue is full")

            created_at = time.time()
            try:
                # Keep the outer transaction usable if a concurrent submit
                # reaches the partial unique index first. The savepoint rolls
                # back only the failed insert so we can safely return the
                # canonical existing job.
                async with session.begin_nested():
                    job_id = await self._insert_with_unique_id(
                        session, created_at=created_at, **fields
                    )
            except IntegrityError as exc:
                constraint = str(exc.orig)
                if _ACTIVE_GENERATION_CONSTRAINT in constraint:
                    raise IndexingJobConflict(fields["collection_id"]) from exc
                if _ACTIVE_REVISION_CONSTRAINT in constraint:
                    existing = await self._active_revision_row(
                        session, fields.get("revision_id")
                    )
                    if existing is None:
                        raise
                    return await self._existing_revision_summary(
                        session,
                        existing,
                        fields=fields,
                        cause=exc,
                    )
                raise
            position = await self._queue_position_db(session, created_at)
            await self._append_events_db(
                session,
                job_id,
                DEFAULT_TENANT,
                "inqtrix.index.queued",
                {"status": "queued", "queue_position": position},
            )
            row = await self._row_db(session, job_id)
            await self._append_collection_effects(
                session,
                row=row,
                access=access,
                action="indexing.submitted",
            )
            return await self._row_summary(session, row)

    async def _active_revision_row(
        self,
        session: "AsyncSession",
        revision_id: str | None,
    ) -> Any | None:
        """Find the globally unique active job for one immutable revision."""

        if not revision_id:
            return None
        return (
            (
                await session.execute(
                    select(indexing_jobs)
                    .where(
                        indexing_jobs.c.operation_kind
                        == IndexingOperationKind.DOCUMENT_REVISION.value,
                        indexing_jobs.c.revision_id == revision_id,
                        indexing_jobs.c.status.in_(_ACTIVE_VALUES),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )

    async def _existing_revision_summary(
        self,
        session: "AsyncSession",
        row: Any,
        *,
        fields: dict[str, Any],
        cause: BaseException | None = None,
    ) -> dict[str, Any]:
        """Return the canonical job only within its authorized collection."""

        if row["collection_id"] != fields["collection_id"]:
            conflict = IndexingJobConflict(fields["collection_id"])
            if cause is not None:
                raise conflict from cause
            raise conflict
        return await self._row_summary(session, row)

    async def _insert_with_unique_id(
        self, session: "AsyncSession", *, created_at: float, **fields: Any
    ) -> str:
        for _ in range(8):
            job_id = new_indexing_job_id()
            result = await session.execute(
                pg_insert(indexing_jobs)
                .values(
                    job_id=job_id,
                    tenant_id=DEFAULT_TENANT,
                    status=IndexingJobStatus.QUEUED.value,
                    collection_id=fields["collection_id"],
                    operation_kind=fields.get(
                        "operation_kind",
                        IndexingOperationKind.COLLECTION_GENERATION.value,
                    ),
                    document_id=fields.get("document_id"),
                    revision_id=fields.get("revision_id"),
                    collection_name=fields["collection_name"],
                    embedding_model=fields["embedding_model"],
                    index_id=fields["index_id"],
                    workspace_id=fields["workspace_id"],
                    created_by_user_id=fields["created_by_user_id"],
                    created_by_tenant_id=fields["created_by_tenant_id"],
                    generation_id=(
                        fields.get("generation_id")
                        or (
                            f"gen_{uuid.uuid4().hex[:20]}"
                            if fields.get("operation_kind")
                            == IndexingOperationKind.COLLECTION_GENERATION.value
                            else None
                        )
                    ),
                    checkpoint=dict(fields.get("checkpoint") or {}),
                    fence_token=uuid.uuid4().hex,
                    created_at=created_at,
                )
                .on_conflict_do_nothing(index_elements=["job_id"])
                .returning(indexing_jobs.c.job_id)
            )
            if result.scalar_one_or_none() is not None:
                return job_id
            log.warning("Indexing job id collision detected; retrying.")
        raise RuntimeError("could not allocate a unique indexing job id")

    async def _queue_position_db(
        self, session: "AsyncSession", created_at: float
    ) -> int:
        earlier = await session.scalar(
            select(func.count())
            .select_from(indexing_jobs)
            .where(
                indexing_jobs.c.status == IndexingJobStatus.QUEUED.value,
                indexing_jobs.c.created_at < created_at,
            )
        )
        return int(earlier or 0) + 1

    async def _row_summary(self, session: "AsyncSession", row) -> dict[str, Any]:
        """Public summary for one row, with the FIFO position attached for a
        still-queued job (``None`` once running/terminal)."""
        position = (
            await self._queue_position_db(session, row["created_at"])
            if row["status"] == IndexingJobStatus.QUEUED.value
            else None
        )
        # A plan made before automatic resume was switched off survives a
        # restart only here: it is not shown once no sweep runs.
        return build_indexing_job_summary(
            _record_from_row(row),
            queue_position=position,
            show_auto_resume=self._auto_resume.enabled,
        )

    async def _row_db(self, session: "AsyncSession", job_id: str):
        row = (
            (
                await session.execute(
                    select(indexing_jobs).where(indexing_jobs.c.job_id == job_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise IndexingJobNotFound(job_id)
        return row

    async def _visible_row_db(
        self,
        session: "AsyncSession",
        job_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ):
        row = await self._row_db(session, job_id)
        if _visible_row(row, visible_to):
            if not _workspace_matches_row(row, workspace_id):
                raise IndexingJobNotFound(job_id)
            return row
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

    async def _summary_db(
        self,
        job_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row = await self._visible_row_db(session, job_id, workspace_id, visible_to)
            return await self._row_summary(session, row)

    async def _execution_spec_db(self, job_id: str) -> IndexingExecutionSpec:
        """Read server-only execution identity from the canonical job row."""

        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row = await self._row_db(session, job_id)
            return IndexingExecutionSpec(
                job_id=row["job_id"],
                collection_id=row["collection_id"],
                embedding_model=row["embedding_model"],
                operation_kind=IndexingOperationKind(row["operation_kind"]),
                document_id=row["document_id"],
                revision_id=row["revision_id"],
                generation_id=row["generation_id"],
                created_by_user_id=row["created_by_user_id"],
                created_by_tenant_id=row["created_by_tenant_id"],
            )

    async def _list_db(
        self,
        collection_id: str | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> list[dict[str, Any]]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            query = select(indexing_jobs).order_by(indexing_jobs.c.created_at.desc())
            if collection_id is not None:
                query = query.where(indexing_jobs.c.collection_id == collection_id)
            if visible_to is not None:
                query = query.where(
                    indexing_jobs.c.created_by_user_id == visible_to.principal.user_id,
                    indexing_jobs.c.created_by_tenant_id
                    == visible_to.principal.tenant_id,
                )
                if workspace_id is not None:
                    query = query.where(indexing_jobs.c.workspace_id == workspace_id)
            elif workspace_id is not None:
                query = query.where(indexing_jobs.c.workspace_id == workspace_id)
            rows = (await session.execute(query)).mappings().all()
            return [await self._row_summary(session, row) for row in rows]

    async def _cancel_db(
        self,
        job_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        actor_user_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            locked = await self._lock_collection_access_for_job(
                session,
                job_id=job_id,
                actor_user_id=actor_user_id,
            )
            if locked is None:
                raise IndexingJobNotFound(job_id)
            row, access = locked
            if not _workspace_matches_row(row, workspace_id):
                raise IndexingJobNotFound(job_id)
            if (
                visible_to is not None
                and actor_user_id is None
                and not _visible_row(row, visible_to)
            ):
                raise IndexingJobNotFound(job_id)
            status = row["status"]
            cancellable_now = (
                IndexingJobStatus.QUEUED.value,
                IndexingJobStatus.PAUSED_DEPENDENCY.value,
                IndexingJobStatus.PAUSED_VALIDATION.value,
            )
            if status in cancellable_now:
                cancelled = (
                    await session.execute(
                        update(indexing_jobs)
                        .where(
                            indexing_jobs.c.job_id == job_id,
                            indexing_jobs.c.status.in_(cancellable_now),
                        )
                        .values(
                            status=IndexingJobStatus.CANCELLED.value,
                            cancel_requested=True,
                            finished_at=time.time(),
                        )
                        .returning(indexing_jobs.c.job_id)
                    )
                ).scalar_one_or_none()
                if cancelled is not None:
                    await self._append_events_db(
                        session,
                        job_id,
                        row["tenant_id"],
                        "inqtrix.index.cancelled",
                        {
                            "status": "cancelled",
                            "reason": (
                                "cancelled_before_start"
                                if status == IndexingJobStatus.QUEUED.value
                                else "cancelled_while_paused"
                            ),
                            "snapshot": _snapshot_from_row(
                                await self._row_db(session, job_id)
                            ),
                        },
                    )
                    await self._append_collection_effects(
                        session,
                        row=await self._row_db(session, job_id),
                        access=access,
                        action="indexing.cancelled",
                        actor_user_id=actor_user_id,
                    )
                else:
                    # Lost the CAS to a concurrent claim: the job runs
                    # now — degrade to the two-phase cancel request.
                    status = IndexingJobStatus.RUNNING.value
            if status == IndexingJobStatus.RUNNING.value:
                requested = (
                    await session.execute(
                        update(indexing_jobs)
                        .where(
                            indexing_jobs.c.job_id == job_id,
                            indexing_jobs.c.status == IndexingJobStatus.RUNNING.value,
                        )
                        .values(
                            status=IndexingJobStatus.CANCELLING.value,
                            cancel_requested=True,
                        )
                        .returning(indexing_jobs.c.job_id)
                    )
                ).scalar_one_or_none()
                if requested is not None:
                    await self._append_events_db(
                        session,
                        job_id,
                        row["tenant_id"],
                        "inqtrix.index.cancel_requested",
                        {
                            "status": "cancelling",
                            "reason": "client_requested_cancel",
                        },
                    )
                    await self._append_collection_effects(
                        session,
                        row=await self._row_db(session, job_id),
                        access=access,
                        action="indexing.cancel_requested",
                        actor_user_id=actor_user_id,
                    )
            fresh = await self._row_db(session, job_id)
            return await self._row_summary(session, fresh)

    async def _resume_db(
        self,
        job_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        actor_user_id: uuid.UUID | None,
        *,
        raw_by_user_choice: bool,
        execution_available: bool,
        automatic: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(
                session,
                job_id=job_id,
                actor_user_id=actor_user_id,
            )
            if locked is None:
                raise IndexingJobNotFound(job_id)
            row, access = locked
            if not _workspace_matches_row(row, workspace_id):
                raise IndexingJobNotFound(job_id)
            paused = (
                IndexingJobStatus.PAUSED_DEPENDENCY.value,
                IndexingJobStatus.PAUSED_VALIDATION.value,
            )
            if row["status"] not in paused:
                return await self._row_summary(session, row)
            # Only a dependency pause carries a plan.
            plan = dict(row.get("checkpoint") or {}).get("auto_resume")
            if automatic and not is_due(plan, self._clock() if now is None else now):
                return await self._row_summary(session, row)
            if not execution_available:
                raise IndexingResumeUnavailable(
                    ("The paused indexing job lost its local execution context after a "
                    "process restart. The checkpoint is unchanged; start the service "
                    "with the durable worker profile or cancel the operation visibly.")
                )
            checkpoint = dict(row.get("checkpoint") or {})
            resumed: dict[str, Any] = {"status": "queued"}
            if automatic:
                attempt = int(plan["attempts"]) + 1
                checkpoint = _with_auto_resume(
                    checkpoint,
                    {**plan, "attempts": attempt, "next_at": None,
                     "resumed_automatically": True},
                )
                resumed.update(automatic=True, attempt=attempt, max=int(plan["max"]))
            else:
                checkpoint = _with_auto_resume(checkpoint, None)
            if raw_by_user_choice:
                checkpoint = {"raw_by_user_choice": True}
            await session.execute(
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.in_(paused),
                )
                .values(
                    status=IndexingJobStatus.QUEUED.value,
                    phase="queued",
                    error=None,
                    cancel_requested=False,
                    claimed_by=None,
                    fence_token=uuid.uuid4().hex,
                    checkpoint=checkpoint,
                    completed_documents=(
                        0 if raw_by_user_choice else row["completed_documents"]
                    ),
                    current_batch=(0 if raw_by_user_choice else row["current_batch"]),
                    total_batches=(0 if raw_by_user_choice else row["total_batches"]),
                )
            )
            fresh = await self._row_db(session, job_id)
            if raw_by_user_choice:
                await self._append_events_db(
                    session,
                    job_id,
                    fresh["tenant_id"],
                    "inqtrix.index.raw_rebuild_requested",
                    {
                        "status": row["status"],
                        "generation_id": row["generation_id"],
                        "snapshot": _snapshot_from_row(fresh),
                    },
                )
                await self._append_collection_effects(
                    session,
                    row=fresh,
                    access=access,
                    action="indexing.raw_rebuild_requested",
                    actor_user_id=actor_user_id,
                )
            resumed["snapshot"] = _snapshot_from_row(fresh)
            await self._append_events_db(
                session,
                job_id,
                fresh["tenant_id"],
                "inqtrix.index.resumed",
                resumed,
            )
            await self._append_collection_effects(
                session,
                row=fresh,
                access=access,
                action="indexing.resumed",
                actor_user_id=actor_user_id,
            )
            summary = await self._row_summary(session, fresh)
        if automatic:
            _log_automatic_resume(job_id, resumed["attempt"], resumed["max"])
        return summary

    async def _replay_db(
        self,
        job_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        *,
        after_sequence: int = 0,
    ) -> tuple[str, list[dict[str, Any]]]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row = await self._visible_row_db(session, job_id, workspace_id, visible_to)
            events = await self._fetch_events(
                session,
                job_id,
                max(0, int(after_sequence)),
            )
            return row["tenant_id"], events

    async def _events_after_db(
        self, job_id: str, tenant_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        async with self._session(tenant_id) as session:
            return await self._fetch_events(session, job_id, after_sequence)

    async def _fetch_events(
        self, session: "AsyncSession", job_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        rows = (
            (
                await session.execute(
                    select(indexing_job_events)
                    .where(
                        indexing_job_events.c.job_id == job_id,
                        indexing_job_events.c.sequence > after_sequence,
                    )
                    .order_by(indexing_job_events.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [
            build_indexing_event(
                job_id=job_id,
                sequence=row["sequence"],
                event_type=row["type"],
                created_at=row["created_at"],
                payload=row["data"],
            )
            for row in rows
        ]

    async def _append_events_db(
        self,
        session: "AsyncSession",
        job_id: str,
        tenant_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        sequence = (
            await session.execute(
                update(indexing_jobs)
                .where(indexing_jobs.c.job_id == job_id)
                .values(event_seq=indexing_jobs.c.event_seq + 1)
                .returning(indexing_jobs.c.event_seq)
            )
        ).scalar_one()
        await session.execute(
            insert(indexing_job_events).values(
                job_id=job_id,
                tenant_id=tenant_id,
                sequence=sequence,
                type=event_type,
                created_at=time.time(),
                data=dict(payload),
            )
        )

    async def _fence_ok(
        self,
        session: "AsyncSession",
        job_id: str,
        fence_attempt: int | None,
    ) -> bool:
        """Atomic ownership re-check before a fenced non-terminal write.

        Mirrors the run store: the no-op ``event_seq + 0`` update returns
        a row only while this worker attempt still owns the job, so a
        superseded attempt's progress is dropped with a visible log
        instead of interleaving with the new owner's stream.
        """
        if fence_attempt is None:
            return True
        owned = (
            await session.execute(
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.claimed_by == self._worker_id,
                    indexing_jobs.c.attempt == fence_attempt,
                )
                .values(event_seq=indexing_jobs.c.event_seq + 0)
                .returning(indexing_jobs.c.event_seq)
            )
        ).scalar_one_or_none()
        if owned is None:
            log.warning(
                "Progress for reindex job %s discarded; the job now belongs to another "
                "worker attempt.",
                job_id,
            )
            return False
        return True

    async def _progress_db(
        self,
        job_id: str,
        *,
        completed_documents: int | None = None,
        current_document_title: str | None = None,
        total_documents: int | None = None,
        phase: str | None = None,
        current_batch: int | None = None,
        total_batches: int | None = None,
        fence_attempt: int | None = None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(session, job_id=job_id)
            if locked is None:
                raise AuthorizationRevoked(
                    "indexing requester lost collection edit access"
                )
            _job, _access = locked
            if not await self._fence_ok(session, job_id, fence_attempt):
                return
            values: dict[str, Any] = {}
            if total_documents is not None:
                values["total_documents"] = total_documents
            if completed_documents is not None:
                values["completed_documents"] = completed_documents
            if current_document_title is not None:
                values["current_document_title"] = current_document_title
            if phase is not None:
                values["phase"] = phase
            if current_batch is not None:
                values["current_batch"] = current_batch
            if total_batches is not None:
                values["total_batches"] = total_batches
            updated = (
                await session.execute(
                    update(indexing_jobs)
                    .where(
                        indexing_jobs.c.job_id == job_id,
                        indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                    )
                    .values(**values)
                    .returning(indexing_jobs.c.job_id)
                )
            ).scalar_one_or_none()
            if updated is None:
                return
            row = await self._row_db(session, job_id)
            await self._append_events_db(
                session,
                job_id,
                row["tenant_id"],
                "inqtrix.index.progress",
                {"snapshot": _snapshot_from_row(row)},
            )

    async def _checkpoint_db(self, job_id: str) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            row = await self._row_db(session, job_id)
            return dict(row["checkpoint"] or {})

    async def _checkpoint_document_db(
        self,
        job_id: str,
        document_id: str,
        *,
        embedding_receipt: dict[str, Any] | None,
        fence_attempt: int | None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            if not await self._fence_ok(session, job_id, fence_attempt):
                return
            row = (
                await session.execute(
                    select(indexing_jobs.c.checkpoint)
                    .where(indexing_jobs.c.job_id == job_id)
                    .with_for_update()
                )
            ).one()
            checkpoint = dict(row.checkpoint or {})
            completed = list(
                dict.fromkeys(
                    [*checkpoint.get("completed_document_ids", []), document_id]
                )
            )
            checkpoint["completed_document_ids"] = completed
            if embedding_receipt is not None:
                receipts = checkpoint.get("embedding_receipts")
                receipt_map = dict(receipts) if isinstance(receipts, dict) else {}
                receipt_map[document_id] = dict(embedding_receipt)
                checkpoint["embedding_receipts"] = receipt_map
            checkpoint = _without_contextualization_document(
                checkpoint,
                document_id,
            )
            checkpoint = _without_document_progress(
                checkpoint,
                document_id,
            )
            await session.execute(
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(checkpoint=checkpoint)
            )

    async def _checkpoint_context_batch_db(
        self,
        job_id: str,
        document_id: str,
        batch_checkpoint: dict[str, Any],
        *,
        fence_attempt: int | None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            if not await self._fence_ok(session, job_id, fence_attempt):
                return
            row = (
                await session.execute(
                    select(indexing_jobs.c.checkpoint)
                    .where(indexing_jobs.c.job_id == job_id)
                    .with_for_update()
                )
            ).one()
            checkpoint = dict(row.checkpoint or {})
            checkpoint = _with_contextualization_batch(
                checkpoint,
                document_id,
                batch_checkpoint,
            )
            await session.execute(
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(checkpoint=checkpoint)
            )

    async def _pause_db(
        self,
        job_id: str,
        status: IndexingJobStatus,
        message: str,
        *,
        error_type: str,
        fence_attempt: int | None,
    ) -> bool:
        if status not in {
            IndexingJobStatus.PAUSED_DEPENDENCY,
            IndexingJobStatus.PAUSED_VALIDATION,
        }:
            raise ValueError(f"{status} is not a resumable pause status")
        async with self._session(DEFAULT_TENANT) as session:
            if not await self._fence_ok(session, job_id, fence_attempt):
                return False
            error = {
                "message": sanitize_error(message),
                "type": error_type,
            }
            tenant_id = (
                await session.execute(
                    update(indexing_jobs)
                    .where(
                        indexing_jobs.c.job_id == job_id,
                        indexing_jobs.c.status
                        == IndexingJobStatus.RUNNING.value,
                        indexing_jobs.c.cancel_requested.is_(False),
                    )
                    .values(status=status.value, error=error)
                    .returning(indexing_jobs.c.tenant_id)
                )
            ).scalar_one_or_none()
            if tenant_id is None:
                return False
            row = await self._row_db(session, job_id)
            checkpoint = dict(row["checkpoint"] or {})
            plan = (
                plan_pause(
                    checkpoint.get("auto_resume"),
                    error_type=error_type,
                    progress=_progress_mark(row["completed_documents"], checkpoint),
                    now=self._clock(),
                    policy=self._auto_resume,
                    random=self._random,
                )
                if status == IndexingJobStatus.PAUSED_DEPENDENCY
                else None
            )
            updated = _with_auto_resume(checkpoint, plan)
            if updated != checkpoint:
                await session.execute(
                    update(indexing_jobs)
                    .where(indexing_jobs.c.job_id == job_id)
                    .values(checkpoint=updated)
                )
            data: dict[str, Any] = {
                "status": status.value,
                "error": error,
                "snapshot": _error_event_snapshot_from_row(row),
            }
            if plan is not None:
                # The pause ends the event stream: the plan travels with it.
                data["auto_resume"] = public_plan(plan)
            await self._append_events_db(
                session, job_id, tenant_id, f"inqtrix.index.{status.value}", data
            )
        _log_landed_pause(job_id, status, error_type)
        _log_auto_resume_plan(job_id, plan)
        return True

    async def _document_event_db(
        self,
        job_id: str,
        document_id: str,
        *,
        event_type: str,
        fence_attempt: int | None = None,
        outcome: str | None = None,
        context_omitted_chunks: int = 0,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(session, job_id=job_id)
            if locked is None:
                raise AuthorizationRevoked(
                    "indexing requester lost collection edit access"
                )
            job, _access = locked
            if not await self._fence_ok(session, job_id, fence_attempt):
                return
            values: dict[str, Any] = {}
            if event_type == "inqtrix.index.document_started":
                values["checkpoint"] = _with_document_progress(
                    dict(job["checkpoint"] or {}),
                    document_id,
                    "preparing",
                )
            # Guard against a late event after the job went terminal,
            # mirroring _progress_db's status gate.
            statement = (
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(**values)
                .returning(indexing_jobs.c.tenant_id)
                if values
                else select(indexing_jobs.c.tenant_id).where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                )
            )
            tenant_id = (
                await session.execute(statement)
            ).scalar_one_or_none()
            if tenant_id is None:
                return
            payload: dict[str, Any] = {"document_id": document_id}
            if outcome is not None:
                payload["outcome"] = outcome
            if context_omitted_chunks:
                payload["context_omitted_chunks"] = context_omitted_chunks
            await self._append_events_db(
                session,
                job_id,
                tenant_id,
                event_type,
                payload,
            )

    async def _document_progress_db(
        self,
        job_id: str,
        document_id: str,
        phase: str,
        *,
        current_batch: int,
        total_batches: int,
        fence_attempt: int | None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(
                session,
                job_id=job_id,
            )
            if locked is None:
                raise AuthorizationRevoked(
                    "indexing requester lost collection edit access"
                )
            job, _access = locked
            if not await self._fence_ok(session, job_id, fence_attempt):
                return
            checkpoint = _with_document_progress(
                dict(job["checkpoint"] or {}),
                document_id,
                phase,
                current_batch=current_batch,
                total_batches=total_batches,
            )
            values = {
                "phase": str(phase),
                "current_batch": max(0, int(current_batch)),
                "total_batches": max(0, int(total_batches)),
                "checkpoint": checkpoint,
            }
            tenant_id = (
                await session.execute(
                    update(indexing_jobs)
                    .where(
                        indexing_jobs.c.job_id == job_id,
                        indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                    )
                    .values(**values)
                    .returning(indexing_jobs.c.tenant_id)
                )
            ).scalar_one_or_none()
            if tenant_id is None:
                return
            row = await self._row_db(session, job_id)
            await self._append_events_db(
                session,
                job_id,
                tenant_id,
                "inqtrix.index.document_progress",
                {
                    "document_id": document_id,
                    **values,
                    "snapshot": _snapshot_from_row(row),
                },
            )

    async def _terminal_db(
        self,
        job_id: str,
        status: IndexingJobStatus,
        *,
        fence_attempt: int | None,
        event_type: str,
        extra: dict[str, Any],
        error: dict[str, Any] | None = None,
        clear_title: bool = False,
        warn_on_noop: bool = True,
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            locked = await self._lock_collection_access_for_job(session, job_id=job_id)
            if locked is None:
                return await self._terminalize_revoked_job_db(
                    session,
                    job_id=job_id,
                    fence_attempt=fence_attempt,
                )
            _locked_row, access = locked
            values: dict[str, Any] = {
                "status": (
                    case(
                        (
                            (
                                indexing_jobs.c.cancel_requested.is_(True)
                                | (
                                    indexing_jobs.c.status
                                    == IndexingJobStatus.CANCELLING.value
                                )
                            ),
                            IndexingJobStatus.CANCELLED.value,
                        ),
                        else_=IndexingJobStatus.COMPLETED.value,
                    )
                    if status == IndexingJobStatus.COMPLETED
                    else status.value
                ),
                "finished_at": time.time(),
            }
            if error is not None:
                values["error"] = error
            if clear_title:
                values["current_document_title"] = ""
            query = (
                update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(**values)
                .returning(indexing_jobs.c.tenant_id)
            )
            if status == IndexingJobStatus.COMPLETED:
                query = query.where(
                    indexing_jobs.c.status.notin_(
                        (
                            IndexingJobStatus.PAUSED_DEPENDENCY.value,
                            IndexingJobStatus.PAUSED_VALIDATION.value,
                        )
                    )
                )
            if fence_attempt is not None:
                query = query.where(
                    indexing_jobs.c.claimed_by == self._worker_id,
                    indexing_jobs.c.attempt == fence_attempt,
                )
            tenant_id = (await session.execute(query)).scalar_one_or_none()
            if tenant_id is None:
                # Already terminal, missing, or fenced out (a reclaimed
                # zombie) — absorbing states stay absorbing. The discarded
                # write is operator-visible, EXCEPT the auto-complete
                # safety net whose no-op is the expected happy path.
                if warn_on_noop:
                    log.warning(
                        "Terminal write for reindex job %s discarded because it is "
                        "already terminal or was claimed by another worker.",
                        job_id,
                    )
                return False
            row = await self._row_db(session, job_id)
            if (
                status == IndexingJobStatus.COMPLETED
                and row["status"] == IndexingJobStatus.CANCELLED.value
            ):
                event_type = "inqtrix.index.cancelled"
                extra = {
                    "status": "cancelled",
                    "reason": "client_requested_cancel",
                }
            snapshot = (
                _error_event_snapshot_from_row(row)
                if event_type == "inqtrix.index.failed"
                else _snapshot_from_row(row)
            )
            await self._append_events_db(
                session,
                job_id,
                tenant_id,
                event_type,
                {**extra, "snapshot": snapshot},
            )
            await self._append_collection_effects(
                session,
                row=row,
                access=access,
                action=f"indexing.{row['status']}",
            )
            return True

    async def _terminalize_revoked_job_db(
        self,
        session: "AsyncSession",
        *,
        job_id: str,
        fence_attempt: int | None,
    ) -> bool:
        """Fail a job whose requester lost collection edit authority."""
        error = {
            "message": "Collection authorization was revoked during indexing.",
            "type": "authorization_revoked",
        }
        query = (
            update(indexing_jobs)
            .where(
                indexing_jobs.c.job_id == job_id,
                indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
            )
            .values(
                status=IndexingJobStatus.FAILED.value,
                finished_at=time.time(),
                current_document_title="",
                error=error,
            )
            .returning(
                indexing_jobs.c.tenant_id,
                indexing_jobs.c.collection_id,
                indexing_jobs.c.created_by_user_id,
            )
        )
        if fence_attempt is not None:
            query = query.where(
                indexing_jobs.c.claimed_by == self._worker_id,
                indexing_jobs.c.attempt == fence_attempt,
            )
        landed = (await session.execute(query)).first()
        if landed is None:
            return False
        row = await self._row_db(session, job_id)
        await self._append_events_db(
            session,
            job_id,
            landed.tenant_id,
            "inqtrix.index.failed",
            {
                "status": "failed",
                "error": error,
                "snapshot": _snapshot_from_row(row),
            },
        )
        owner_user_id = (
            await session.execute(
                select(knowledge_collections.c.created_by_user_id).where(
                    knowledge_collections.c.tenant_id == landed.tenant_id,
                    knowledge_collections.c.id == landed.collection_id,
                )
            )
        ).scalar_one_or_none()
        requester_targets = (
            (landed.created_by_user_id,)
            if landed.created_by_user_id is not None
            else ()
        )
        await append_resource_effects(
            session,
            tenant_id=landed.tenant_id,
            actor_user_id=landed.created_by_user_id,
            owner_user_id=owner_user_id,
            action="indexing.authorization_revoked",
            resource_type="knowledge_collection",
            resource_id=landed.collection_id,
            scope="indexing",
            additional_targets=requester_targets,
            # The job terminalizes as FAILED here — the index row must
            # never read as success, and the drawer needs the job id.
            outcome="failure",
            correlation={"run_id": str(job_id)},
        )
        return True

    async def _claim_db(
        self, job_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedIndexingJob | None:
        async with self._session(tenant_id) as session:
            # Cutover fence, FIRST statement of the claim transaction: a
            # worker whose image predates a completed migration must not
            # take a durable claim -- nor run this transaction's other
            # writes -- against a schema its code no longer matches. Rides
            # in the same transaction, so a mismatch rolls everything back
            # and the queue entry stays unacked for an upgraded worker.
            await assert_schema_head(session)
            locked = await self._lock_collection_access_for_job(session, job_id=job_id)
            if locked is None:
                await self._terminalize_revoked_job_db(
                    session,
                    job_id=job_id,
                    fence_attempt=None,
                )
                return None
            allowed = [IndexingJobStatus.QUEUED.value]
            if allow_takeover:
                allowed.extend(
                    (
                        IndexingJobStatus.RUNNING.value,
                        IndexingJobStatus.CANCELLING.value,
                    )
                )
            row = (
                await session.execute(
                    update(indexing_jobs)
                    .where(
                        indexing_jobs.c.job_id == job_id,
                        indexing_jobs.c.status.in_(allowed),
                    )
                    .values(
                        status=case(
                            (
                                indexing_jobs.c.cancel_requested.is_(True),
                                IndexingJobStatus.CANCELLING.value,
                            ),
                            else_=IndexingJobStatus.RUNNING.value,
                        ),
                        claimed_by=self._worker_id,
                        attempt=indexing_jobs.c.attempt + 1,
                        started_at=func.coalesce(
                            indexing_jobs.c.started_at, time.time()
                        ),
                        phase="starting",
                    )
                    .returning(
                        indexing_jobs.c.attempt,
                        indexing_jobs.c.collection_id,
                        indexing_jobs.c.embedding_model,
                        indexing_jobs.c.created_by_user_id,
                        indexing_jobs.c.created_by_tenant_id,
                        indexing_jobs.c.cancel_requested,
                        indexing_jobs.c.generation_id,
                        indexing_jobs.c.operation_kind,
                        indexing_jobs.c.document_id,
                        indexing_jobs.c.revision_id,
                        indexing_jobs.c.checkpoint,
                    )
                )
            ).first()
            if row is None:
                return None
            started_row = await self._row_db(session, job_id)
            await self._append_events_db(
                session,
                job_id,
                tenant_id,
                "inqtrix.index.started",
                {
                    "status": started_row["status"],
                    "snapshot": _snapshot_from_row(started_row),
                },
            )
            return ClaimedIndexingJob(
                job_id=job_id,
                tenant_id=tenant_id,
                attempt=int(row[0]),
                collection_id=row[1],
                embedding_model=row[2],
                created_by_user_id=row[3],
                created_by_tenant_id=row[4],
                cancel_requested=bool(row[5]),
                generation_id=str(row[6] or ""),
                operation_kind=IndexingOperationKind(row[7]),
                document_id=row[8],
                revision_id=row[9],
                checkpoint=dict(row[10] or {}),
            )

    async def _cancel_requested_db(self, job_ids: dict[str, str]) -> set[str]:
        if not job_ids:
            return set()
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(indexing_jobs.c.job_id).where(
                            indexing_jobs.c.job_id.in_(list(job_ids)),
                            indexing_jobs.c.cancel_requested.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return set(rows)

    async def _attempt_cancel_requested_db(
        self,
        job_id: str,
        *,
        fence_attempt: int,
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            pending = await session.scalar(
                select(indexing_jobs.c.cancel_requested).where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.attempt == fence_attempt,
                )
            )
            return bool(pending)

    async def _stale_queued_db(
        self, older_than_seconds: float
    ) -> list[tuple[str, str]]:
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                await session.execute(
                    select(indexing_jobs.c.job_id, indexing_jobs.c.tenant_id).where(
                        indexing_jobs.c.status == IndexingJobStatus.QUEUED.value,
                        indexing_jobs.c.created_at < time.time() - older_than_seconds,
                    )
                )
            ).all()
            return [(row[0], row[1]) for row in rows]

    async def _acquire_contextualization_circuit_db(
        self,
        *,
        provider_key: str,
        model: str,
        cooldown_seconds: float,
        probe_lease_seconds: float,
    ) -> ContextualizationCircuitPermit | None:
        """Serialize open/half-open admission under the provider/model row."""

        provider = str(provider_key).strip()
        resolved_model = str(model).strip()
        cooldown = float(cooldown_seconds)
        lease = float(probe_lease_seconds)
        if not provider or not resolved_model:
            raise ValueError("provider_key and model must not be empty")
        if cooldown <= 0 or lease <= 0:
            raise ValueError("circuit cooldown and probe lease must be positive")
        now = time.time()
        async with self._session(DEFAULT_TENANT) as session:
            row = (
                (
                    await session.execute(
                        select(contextualization_provider_circuits)
                        .where(
                            contextualization_provider_circuits.c.tenant_id
                            == DEFAULT_TENANT,
                            contextualization_provider_circuits.c.provider_key
                            == provider,
                            contextualization_provider_circuits.c.model
                            == resolved_model,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            permit = ContextualizationCircuitPermit(
                provider_key=provider,
                model=resolved_model,
                cooldown_seconds=cooldown,
                probe_lease_seconds=lease,
            )
            admission = circuit_admission(
                None if row is None else row["state"],
                cooldown_until=None if row is None else row["cooldown_until"],
                probe_lease_until=None if row is None else row["probe_lease_until"],
                now=now,
            )
            if admission == CircuitAdmission.ALL:
                return permit
            if admission == CircuitAdmission.NONE:
                return None

            # The cooldown elapsed, or the prior probe worker crashed and its
            # lease expired. SELECT FOR UPDATE grants one replacement token
            # across every API/worker replica.
            probe_token = uuid.uuid4().hex
            updated = (
                await session.execute(
                    update(contextualization_provider_circuits)
                    .where(
                        contextualization_provider_circuits.c.tenant_id
                        == DEFAULT_TENANT,
                        contextualization_provider_circuits.c.provider_key
                        == provider,
                        contextualization_provider_circuits.c.model
                        == resolved_model,
                    )
                    .values(
                        state=ContextualizationCircuitState.HALF_OPEN.value,
                        probe_token=probe_token,
                        probe_lease_until=now + lease,
                        updated_at=now,
                    )
                    .returning(
                        contextualization_provider_circuits.c.provider_key
                    )
                )
            ).scalar_one_or_none()
            if updated is None:
                raise RuntimeError("contextualization circuit row disappeared")
            log.info(
                "Contextualization circuit entered half-open",
                extra={
                    "event": "knowledge.contextualization.circuit.half_open",
                    "provider": provider,
                    "model": resolved_model,
                    "probe_lease_seconds": lease,
                },
            )
            return ContextualizationCircuitPermit(
                provider_key=provider,
                model=resolved_model,
                cooldown_seconds=cooldown,
                probe_lease_seconds=lease,
                probe_token=probe_token,
            )

    async def _record_contextualization_circuit_success_db(
        self,
        permit: ContextualizationCircuitPermit,
    ) -> None:
        if permit.probe_token is None:
            return
        now = time.time()
        async with self._session(DEFAULT_TENANT) as session:
            closed = (
                await session.execute(
                    update(contextualization_provider_circuits)
                    .where(
                        contextualization_provider_circuits.c.tenant_id
                        == DEFAULT_TENANT,
                        contextualization_provider_circuits.c.provider_key
                        == permit.provider_key,
                        contextualization_provider_circuits.c.model
                        == permit.model,
                        contextualization_provider_circuits.c.state
                        == ContextualizationCircuitState.HALF_OPEN.value,
                        contextualization_provider_circuits.c.probe_token
                        == permit.probe_token,
                    )
                    .values(
                        state=ContextualizationCircuitState.CLOSED.value,
                        consecutive_failures=0,
                        cooldown_until=0.0,
                        probe_token=None,
                        probe_lease_until=None,
                        last_error_type=None,
                        updated_at=now,
                    )
                    .returning(
                        contextualization_provider_circuits.c.provider_key
                    )
                )
            ).scalar_one_or_none()
            if closed is not None:
                log.info(
                    "Contextualization circuit closed after successful probe",
                    extra={
                        "event": "knowledge.contextualization.circuit.closed",
                        "provider": permit.provider_key,
                        "model": permit.model,
                    },
                )

    async def _record_contextualization_circuit_failure_db(
        self,
        permit: ContextualizationCircuitPermit,
        *,
        error_type: str,
    ) -> None:
        now = time.time()
        async with self._session(DEFAULT_TENANT) as session:
            if permit.probe_token is not None:
                failures = (
                    await session.execute(
                        update(contextualization_provider_circuits)
                        .where(
                            contextualization_provider_circuits.c.tenant_id
                            == DEFAULT_TENANT,
                            contextualization_provider_circuits.c.provider_key
                            == permit.provider_key,
                            contextualization_provider_circuits.c.model
                            == permit.model,
                            contextualization_provider_circuits.c.state
                            == ContextualizationCircuitState.HALF_OPEN.value,
                            contextualization_provider_circuits.c.probe_token
                            == permit.probe_token,
                        )
                        .values(
                            state=ContextualizationCircuitState.OPEN.value,
                            consecutive_failures=(
                                contextualization_provider_circuits.c
                                .consecutive_failures
                                + 1
                            ),
                            cooldown_until=now + permit.cooldown_seconds,
                            probe_token=None,
                            probe_lease_until=None,
                            last_error_type=str(error_type),
                            updated_at=now,
                        )
                        .returning(
                            contextualization_provider_circuits.c
                            .consecutive_failures
                        )
                    )
                ).scalar_one_or_none()
                # A stale token belongs to a crashed/reclaimed attempt and
                # cannot reopen state now owned by another half-open probe.
                if failures is None:
                    return
                failure_count = int(failures)
            else:
                insert_stmt = pg_insert(
                    contextualization_provider_circuits
                ).values(
                    tenant_id=DEFAULT_TENANT,
                    provider_key=permit.provider_key,
                    model=permit.model,
                    state=ContextualizationCircuitState.OPEN.value,
                    consecutive_failures=1,
                    cooldown_until=now + permit.cooldown_seconds,
                    probe_token=None,
                    probe_lease_until=None,
                    last_error_type=str(error_type),
                    updated_at=now,
                )
                failure_count = int(
                    (
                        await session.execute(
                            insert_stmt.on_conflict_do_update(
                                index_elements=[
                                    contextualization_provider_circuits.c.tenant_id,
                                    contextualization_provider_circuits.c.provider_key,
                                    contextualization_provider_circuits.c.model,
                                ],
                                set_={
                                    "state": (
                                        ContextualizationCircuitState.OPEN.value
                                    ),
                                    "consecutive_failures": (
                                        contextualization_provider_circuits.c
                                        .consecutive_failures
                                        + 1
                                    ),
                                    "cooldown_until": (
                                        now + permit.cooldown_seconds
                                    ),
                                    "probe_token": None,
                                    "probe_lease_until": None,
                                    "last_error_type": str(error_type),
                                    "updated_at": now,
                                },
                            ).returning(
                                contextualization_provider_circuits.c
                                .consecutive_failures
                            )
                        )
                    ).scalar_one()
                )
            log.warning(
                "Contextualization circuit opened after transient failure",
                extra={
                    "event": "knowledge.contextualization.circuit.opened",
                    "provider": permit.provider_key,
                    "model": permit.model,
                    "cooldown_seconds": permit.cooldown_seconds,
                    "failure_count": failure_count,
                    "error_type": str(error_type),
                },
            )

    async def _cleanup_db(
        self,
        session: "AsyncSession",
        *,
        execution_lost_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Apply recovery and retention through one locked lifecycle path.

        Collection rows are locked in canonical order before any job row.
        This is the same collection-to-job order used by normal lifecycle
        writes, so cleanup cannot deadlock with a claim, progress write, or
        terminal transition. In-process execution orphans are failed visibly;
        paused rows are neither retention candidates nor restart orphans.
        Terminal history is invalidated and audited before deletion.
        ``execution_lost_ids`` carries the lost-execution fence's
        pre-filtered candidates through the same locked path; the status
        re-check under lock absorbs races.
        """
        now = time.time()
        recover_orphans = self._sweep_orphans
        recovery_rows = []
        if recover_orphans:
            recovery_rows = (
                await session.execute(
                    select(
                        indexing_jobs.c.job_id,
                        indexing_jobs.c.collection_id,
                    ).where(indexing_jobs.c.status.in_(_RESTART_ORPHAN_VALUES))
                )
            ).all()
        recovery_ids = {row.job_id for row in recovery_rows}

        lost_rows = []
        if execution_lost_ids:
            lost_rows = (
                await session.execute(
                    select(
                        indexing_jobs.c.job_id,
                        indexing_jobs.c.collection_id,
                    ).where(
                        indexing_jobs.c.job_id.in_(sorted(execution_lost_ids)),
                        indexing_jobs.c.status.in_(_RESTART_ORPHAN_VALUES),
                    )
                )
            ).all()
        lost_ids = frozenset(row.job_id for row in lost_rows)

        candidate_rows = await self._retention_candidate_rows_db(
            session,
            now=now,
        )
        history_rows = await self._history_overflow_rows_db(session)
        candidate_pairs = {
            (row.job_id, row.collection_id)
            for row in (*recovery_rows, *lost_rows, *candidate_rows, *history_rows)
        }
        if not candidate_pairs:
            if recover_orphans:
                self._sweep_orphans = False
            return

        collection_ids = tuple(
            sorted({collection_id for _job_id, collection_id in candidate_pairs})
        )
        locked_collections = (
            await session.execute(
                select(
                    knowledge_collections.c.id,
                    knowledge_collections.c.created_by_user_id,
                )
                .where(
                    knowledge_collections.c.tenant_id == DEFAULT_TENANT,
                    knowledge_collections.c.id.in_(collection_ids),
                )
                .order_by(knowledge_collections.c.id)
                .with_for_update()
            )
        ).all()
        owner_by_collection = {
            row.id: row.created_by_user_id for row in locked_collections
        }

        # Re-evaluate ordinary retention after the collection locks land.
        # Recovery deliberately keeps its initial id snapshot: jobs accepted
        # concurrently by this new process are not previous-process orphans.
        candidate_rows = await self._retention_candidate_rows_db(
            session,
            now=now,
            collection_ids=collection_ids,
        )
        history_rows = await self._history_overflow_rows_db(
            session,
            collection_ids=collection_ids,
        )
        history_ids = {row.job_id for row in history_rows}
        job_ids = tuple(
            sorted(
                recovery_ids
                | lost_ids
                | {row.job_id for row in candidate_rows}
                | history_ids
            )
        )
        locked_jobs = (
            (
                await session.execute(
                    select(indexing_jobs)
                    .where(indexing_jobs.c.job_id.in_(job_ids))
                    .order_by(indexing_jobs.c.job_id)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )

        terminal_cutoff = now - self._completed_ttl_seconds
        for row in locked_jobs:
            action = self._maintenance_action_for_row(
                row,
                recovery_ids=recovery_ids,
                history_ids=history_ids,
                terminal_cutoff=terminal_cutoff,
                execution_lost_ids=lost_ids,
            )
            if action is None:
                continue
            await self._apply_maintenance_action_db(
                session,
                row=row,
                owner_user_id=owner_by_collection.get(row["collection_id"]),
                action=action,
                now=now,
            )
        if recover_orphans:
            # Retry on the next transaction if any recovery mutation above
            # raises and rolls this transaction back.
            self._sweep_orphans = False

    async def _retention_candidate_rows_db(
        self,
        session: "AsyncSession",
        *,
        now: float,
        collection_ids: tuple[str, ...] | None = None,
    ) -> list[Any]:
        """Return terminal jobs whose configured history TTL expired.

        Non-terminal indexing work has no age deadline. Queued delivery is
        reconciled by the worker, running/cancelling delivery is recovered by
        claim fencing, and dependency/validation pauses wait for an explicit
        resume, raw-build choice, or cancellation.
        """
        terminal_expired = (
            indexing_jobs.c.status.in_(_TERMINAL_VALUES)
            & indexing_jobs.c.finished_at.isnot(None)
            & (indexing_jobs.c.finished_at < now - self._completed_ttl_seconds)
        )
        statement = select(
            indexing_jobs.c.job_id,
            indexing_jobs.c.collection_id,
        ).where(terminal_expired)
        if collection_ids is not None:
            statement = statement.where(
                indexing_jobs.c.collection_id.in_(collection_ids)
            )
        return (await session.execute(statement)).all()

    async def _history_overflow_rows_db(
        self,
        session: "AsyncSession",
        *,
        collection_ids: tuple[str, ...] | None = None,
    ) -> list[Any]:
        """Return terminal jobs beyond the per-collection history cap."""
        ranked_statement = select(
            indexing_jobs.c.job_id,
            indexing_jobs.c.collection_id,
            func.row_number()
            .over(
                partition_by=indexing_jobs.c.collection_id,
                order_by=(
                    func.coalesce(
                        indexing_jobs.c.finished_at,
                        indexing_jobs.c.created_at,
                    ).desc(),
                    indexing_jobs.c.job_id.desc(),
                ),
            )
            .label("rn"),
        ).where(indexing_jobs.c.status.in_(_TERMINAL_VALUES))
        if collection_ids is not None:
            ranked_statement = ranked_statement.where(
                indexing_jobs.c.collection_id.in_(collection_ids)
            )
        ranked = ranked_statement.subquery()
        return (
            await session.execute(
                select(ranked.c.job_id, ranked.c.collection_id).where(
                    ranked.c.rn > self._history_limit
                )
            )
        ).all()

    @staticmethod
    def _maintenance_action_for_row(
        row: Any,
        *,
        recovery_ids: set[str],
        history_ids: set[str],
        terminal_cutoff: float,
        execution_lost_ids: frozenset[str] = frozenset(),
    ) -> _MaintenanceAction | None:
        """Choose one mutation after all lifecycle locks have landed."""
        status = row["status"]
        job_id = row["job_id"]
        if job_id in recovery_ids and status in _RESTART_ORPHAN_VALUES:
            return _MaintenanceAction(
                action="indexing.server_restarted",
                error={
                    "message": (
                        "A server restart interrupted the indexing."
                    ),
                    "type": "server_restarted",
                },
            )
        if job_id in execution_lost_ids and status in _RESTART_ORPHAN_VALUES:
            # FAILED, not CANCELLED, even for ``cancelling`` rows: the
            # orderly cancellation cleanup never ran, so partial work may
            # be applied — reporting an orderly cancel would be a lie.
            # ``cancel_requested`` stays on the row as the intent record.
            return _MaintenanceAction(
                action="indexing.execution_lost",
                error={
                    "message": (
                        ("Execution of this indexing job was lost; no process is running"
                        " it.")
                    ),
                    "type": "execution_lost",
                },
            )
        if (
            status in _TERMINAL_VALUES
            and row["finished_at"] is not None
            and row["finished_at"] < terminal_cutoff
        ):
            return _MaintenanceAction(action="indexing.retention_deleted")
        if status in _TERMINAL_VALUES and job_id in history_ids:
            return _MaintenanceAction(action="indexing.history_evicted")
        return None

    async def _apply_maintenance_action_db(
        self,
        session: "AsyncSession",
        *,
        row: Any,
        owner_user_id: uuid.UUID | None,
        action: _MaintenanceAction,
        now: float,
    ) -> None:
        """Apply one audited terminalization or deletion atomically."""
        if action.error is not None:
            values: dict[str, Any] = {
                "status": IndexingJobStatus.FAILED.value,
                "finished_at": now,
                "current_document_title": "",
                "error": action.error,
            }
            landed = (
                await session.execute(
                    update(indexing_jobs)
                    .where(
                        indexing_jobs.c.job_id == row["job_id"],
                        indexing_jobs.c.status.notin_(_TERMINAL_VALUES),
                    )
                    .values(**values)
                    .returning(indexing_jobs.c.job_id)
                )
            ).scalar_one_or_none()
            if landed is None:
                return
            row = await self._row_db(session, row["job_id"])
            await self._append_events_db(
                session,
                row["job_id"],
                row["tenant_id"],
                "inqtrix.index.failed",
                {
                    "status": "failed",
                    "error": action.error,
                    "snapshot": _snapshot_from_row(row),
                },
            )
        requester_targets = (
            (row["created_by_user_id"],)
            if row["created_by_user_id"] is not None
            else ()
        )
        await append_resource_effects(
            session,
            tenant_id=row["tenant_id"],
            actor_user_id=None,
            owner_user_id=owner_user_id,
            action=action.action,
            resource_type="knowledge_collection",
            resource_id=row["collection_id"],
            scope="indexing",
            additional_targets=requester_targets,
            # server_restarted terminalizes the job as FAILED — mirror
            # that in the read model instead of the success default.
            outcome="failure" if action.error is not None else "success",
            correlation={"run_id": str(row["job_id"])},
        )
        if action.error is None:
            await session.execute(
                delete(indexing_jobs).where(indexing_jobs.c.job_id == row["job_id"])
            )
        log.warning(
            "Reindex lifecycle maintenance applied %s to job %s.",
            action.action,
            row["job_id"],
        )


def _record_from_row(row: Any) -> IndexingJobRecord:
    """Hydrate an :class:`IndexingJobRecord` from one ``indexing_jobs`` row.

    Only the persisted fields matter to ``build_indexing_job_summary``
    (the in-proc ``cancel_event``/``events``/``subscribers`` defaults are
    unused for summary building); reusing the dataclass keeps the wire
    shape byte-identical to the in-memory store.
    """
    return IndexingJobRecord(
        job_id=row["job_id"],
        collection_id=row["collection_id"],
        collection_name=row["collection_name"],
        embedding_model=row["embedding_model"],
        created_at=row["created_at"],
        operation_kind=IndexingOperationKind(
            row.get(
                "operation_kind",
                IndexingOperationKind.COLLECTION_GENERATION.value,
            )
        ),
        document_id=row.get("document_id"),
        revision_id=row.get("revision_id"),
        index_id=row["index_id"],
        workspace_id=row["workspace_id"],
        created_by_user_id=row["created_by_user_id"],
        created_by_tenant_id=row["created_by_tenant_id"],
        status=IndexingJobStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        total_documents=row["total_documents"],
        completed_documents=row["completed_documents"],
        current_document_title=row["current_document_title"],
        phase=row.get("phase", "queued"),
        current_batch=int(row.get("current_batch", 0)),
        total_batches=int(row.get("total_batches", 0)),
        checkpoint=dict(row.get("checkpoint") or {}),
        generation_id=row.get("generation_id"),
        fence_token=row.get("fence_token"),
        error=dict(row["error"]) if row["error"] else None,
        event_seq=row["event_seq"],
    )


def _snapshot_from_row(row: Any) -> dict[str, Any]:
    """The progress snapshot for one row (same shape as the in-memory store)."""
    return build_indexing_job_summary(_record_from_row(row))["snapshot"]


def _error_event_snapshot_from_row(row: Any) -> dict[str, Any]:
    """Return progress coordinates without source titles for error events."""

    snapshot = _snapshot_from_row(row)
    snapshot["current_document_title"] = ""
    return snapshot


def _workspace_matches_row(row: Any, workspace_id: str | None) -> bool:
    return workspace_id is None or row["workspace_id"] == workspace_id


def _visible_row(row: Any, visible_to: "UserContext | None") -> bool:
    """SQL-row twin of the in-memory visibility predicate."""
    if visible_to is None:
        return True
    return (
        row["created_by_user_id"] is not None
        and row["created_by_user_id"] == visible_to.principal.user_id
        and row["created_by_tenant_id"] == visible_to.principal.tenant_id
    )
