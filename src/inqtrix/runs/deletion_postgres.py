"""Postgres source of truth for aggregate deletion operations."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from inqtrix.storage.migration_contract import (
    assert_schema_head,
)
from inqtrix.auth.permissions import SharePermission
from inqtrix.execution_failures import classify_execution_failure
from inqtrix.knowledge.source_cleanup import empty_source_cleanup_plan
from inqtrix.knowledge.stores.ports import CollectionNotFound, DocumentNotFound
from inqtrix.pagination import encode_cursor
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.project.asset_lifecycle import (
    lock_asset_lifecycle,
    lock_group_lifecycle,
    lock_section_lifecycle,
)
from inqtrix.project.asset_records_ports import AssetNotFound, GroupNotFound
from inqtrix.project.knowledge_sessions_ports import KnowledgeSessionNotFound
from inqtrix.project.vector_index_ports import VectorIndexNotFound
from inqtrix.runs.durable_store import (
    DEFAULT_TENANT,
    _EXECUTION_LOST_GRACE_SECONDS,
    DurableJobStoreBase,
    _LocalJob,
)
from inqtrix.runs.deletion_operations import (
    KNOWLEDGE_DELETION_TARGET_KINDS,
    SESSION_DELETION_TARGET_KINDS,
    DeletionJobHandle,
    DeletionManifestItem,
    DeletionOperationConflict,
    DeletionOperationNotFound,
    DeletionOperationRecord,
    DeletionOperationStatus,
    DeletionStage,
    DeletionTargetKind,
    DeletionTerminalAction,
    DeletionWork,
    KnowledgeDeletionContext,
    SessionDeletionContext,
    VectorIndexDeletionContext,
    build_deletion_summary,
    new_deletion_operation_id,
    refuse_on_event_loop,
    target_source_id,
)
from inqtrix.source_authority import (
    PostgresSourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.asset_records_orm import asset_groups, asset_records
from inqtrix.storage.deletions_orm import (
    deletion_operation_assets,
    deletion_operation_events,
    deletion_operations,
)
from inqtrix.storage.knowledge_orm import knowledge_collections, knowledge_documents
from inqtrix.storage.knowledge_sessions_orm import knowledge_sessions
from inqtrix.storage.resource_access import (
    append_audit_row,
    lock_resource_access,
)
from inqtrix.storage.user_events_postgres import append_user_invalidation


def _workspace_uuid(value: object) -> "uuid.UUID | None":
    """Workspace column is Text on operations, UUID on audit_log."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
from inqtrix.storage.runs_orm import runs
from inqtrix.storage.uploads_orm import upload_operations
from inqtrix.storage.vector_index_orm import (
    vector_index_members,
    vector_index_records,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from inqtrix.runs.deletion_queue import ValkeyDeletionQueue

log = logging.getLogger("inqtrix")

_KNOWLEDGE_DELETION_TARGET_VALUES = tuple(
    target_kind.value for target_kind in KNOWLEDGE_DELETION_TARGET_KINDS
)
_SESSION_DELETION_TARGET_VALUES = tuple(
    target_kind.value for target_kind in SESSION_DELETION_TARGET_KINDS
)
_INVALIDATING_DELETION_EVENTS = frozenset(
    {
        "inqtrix.deletion.queued",
        "inqtrix.deletion.retried",
        "inqtrix.deletion.deleted",
        "inqtrix.deletion.failed",
    }
)
# Read paths drive the expiry sweep, so throttle it: the receipt poll asks
# several times per second and the answer cannot change that fast.
_EXPIRY_CHECK_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class ClaimedDeletionOperation:
    operation_id: str
    tenant_id: str
    attempt: int
    target_kind: DeletionTargetKind
    target_id: str
    manifest: tuple[DeletionManifestItem, ...]
    vector_index_context: VectorIndexDeletionContext | None
    knowledge_context: KnowledgeDeletionContext | None
    session_context: SessionDeletionContext | None
    created_by_user_id: uuid.UUID | None
    workspace_id: str | None


class PostgresDeletionOperationStore(DurableJobStoreBase):
    """Fenced durable store with atomically registered asset tombstones."""

    _loop_thread_name = "inqtrix-deletion-db"
    _dispatch_thread_prefix = "inqtrix-delete"
    _job_kind = "Durable deletion operation"

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        queue: "ValkeyDeletionQueue | None",
        max_concurrent: int,
        completed_ttl_seconds: int,
        worker_id: str,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
        recover_orphans: bool | None = None,
        dispatch_timeout_seconds: float = 240.0,
    ) -> None:
        super().__init__(
            engine=engine,
            app_role=app_role,
            worker_id=worker_id,
            queue=queue,
            recover_orphans=recover_orphans,
        )
        self._max_concurrent = max_concurrent
        self._completed_ttl_seconds = completed_ttl_seconds
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled
        self._source_authority = PostgresSourceLifecycleAuthority()
        self._dispatch_timeout_seconds = float(dispatch_timeout_seconds)
        self._last_expiry_check: float | None = None

    def submit(
        self,
        *,
        target_kind: DeletionTargetKind,
        target_id: str,
        manifest: tuple[DeletionManifestItem, ...],
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        work: DeletionWork,
        before_dispatch=None,
        refresh_manifest=None,
        terminal_action: DeletionTerminalAction | None = None,
        vector_index_context: VectorIndexDeletionContext | None = None,
        knowledge_context: KnowledgeDeletionContext | None = None,
        session_context: SessionDeletionContext | None = None,
        total_items: int | None = None,
        dispatch: bool = True,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Register one deletion durably and dispatch it.

        ``dispatch=False`` registers without enqueueing: the caller claims
        and runs the operation itself, as an account erasure does with the
        aggregate deletions it owns. ``operation_id`` registers under an id
        the caller already recorded elsewhere.
        """
        refuse_on_event_loop("registration")
        # Postgres refreshes the manifest while holding the same lifecycle
        # locks that fence upload finalisation, then atomically registers the
        # operation and tombstones. The memory-tier callback is therefore not
        # needed here.
        del before_dispatch, refresh_manifest, terminal_action
        # Best-effort expiry first (bounded by grace + throttle): a lost
        # previous operation on the same target should stop answering
        # fresh DELETEs with a conflict within that window.
        self.expire_stalled_operations()
        try:
            record, created = self._call(
                self._submit_db(
                    target_kind=target_kind,
                    target_id=target_id,
                    manifest=manifest,
                    tenant_id=tenant_id,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    vector_index_context=vector_index_context,
                    knowledge_context=knowledge_context,
                    session_context=session_context,
                    total_items=total_items,
                    operation_id=operation_id,
                )
            )
        except SourceLifecycleConflict as exc:
            raise DeletionOperationConflict(target_id) from exc
        if not created or not dispatch:
            return build_deletion_summary(record)
        if self._queue is not None:
            try:
                self._queue.enqueue(
                    operation_id=record.operation_id, tenant_id=tenant_id
                )
            except Exception as exc:
                log.warning(
                    "Dispatch for deletion operation %s failed; the reconciler will "
                    "retry (error_type=%s).",
                    record.operation_id,
                    type(exc).__name__,
                )
        else:
            with self._lock:
                self._local[record.operation_id] = _LocalJob(work=work)
                self._pending.append(record.operation_id)
                self._dispatch_locked()
        return build_deletion_summary(record)

    def get(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        return build_deletion_summary(
            self.get_record(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )

    def get_record(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord:
        self.expire_stalled_operations()
        return self._call(
            self._get_record_db(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )

    def expire_stalled_operations(self) -> None:
        """Make terminal what no process is going to execute.

        Three shapes of one failure — a receipt that stays non-terminal
        while nobody owns it — resolved through the ONE public
        :meth:`fail` path, so tombstones, the ``inqtrix.deletion.failed``
        event, and the retry precondition come out identical to an
        ordinary failure:

        * restart orphans, whose in-process work closure died with the
          previous process. Swept once per process, exactly like the run
          store, and only where ``resolve_orphan_sweep`` allows it (never
          in queue mode, where the workers own those rows);
        * operations never claimed within ``dispatch_timeout_seconds`` —
          the shape a queue without a consuming worker produces, where no
          restart ever comes to clean up; and
        * RUNNING receipts whose executing thread died without a terminal
          write (no-queue mode only — queue mode recovers those through
          claim fencing).

        Without this a stuck receipt is unreachable: ``retry`` requires
        ``delete_failed`` and a second DELETE answers 409, so the operation
        can only be freed by editing the database.

        Operations this process owns are never candidates: everything it
        executes or is about to dispatch sits in ``_local`` until its
        worker thread unwinds. Losing a race is harmless anyway — ``fail``
        CASes on ``queued``/``running``, and a worker that wakes up late is
        fenced out by the attempt counter.
        """

        now = time.monotonic()
        with self._lock:
            if self._closing or self._closed:
                return
            last = self._last_expiry_check
            if last is not None and now - last < _EXPIRY_CHECK_INTERVAL_SECONDS:
                return
            self._last_expiry_check = now
            sweep = self._sweep_orphans
        if sweep:
            self._fail_stalled(
                self._call(self._restart_orphan_operations_db()),
                message="A server restart interrupted the deletion.",
                error_type="server_restarted",
            )
            # Cleared only after the sweep landed, so an exception above
            # leaves the next call to retry it.
            with self._lock:
                self._sweep_orphans = False
        self._fail_stalled(
            [
                operation_id
                for operation_id, _tenant_id in self.stale_queued_operations(
                    older_than_seconds=self._dispatch_timeout_seconds
                )
            ],
            message=(
                "No process claimed the deletion; it was cancelled."
            ),
            error_type="dispatch_timeout",
        )
        if self._recovers_orphans:
            # No-queue mode only: a RUNNING receipt whose executing
            # thread died without a terminal write has no other exit.
            # Queue mode leaves running rows to their workers — claim
            # fencing recovers those — so this selector must never fire
            # there, unlike the mode-agnostic dispatch timeout above.
            self._fail_stalled(
                self._call(
                    self._stale_running_db(_EXECUTION_LOST_GRACE_SECONDS)
                ),
                message=(
                    "Execution of this deletion was lost; no process is running it."
                ),
                error_type="execution_lost",
            )

    def _fail_stalled(
        self,
        operation_ids: list[str],
        *,
        message: str,
        error_type: str,
    ) -> None:
        """Fail candidates this process does not own, one row at a time."""

        if not operation_ids:
            return
        with self._lock:
            owned = set(self._local)
        for operation_id in operation_ids:
            if operation_id in owned:
                continue
            log.warning(
                "Deletion operation %s will be terminated as %s because it was "
                "non-terminal and no process claimed it.",
                operation_id,
                error_type,
            )
            self.fail(operation_id, message, error_type=error_type)

    async def _restart_orphan_operations_db(self) -> list[str]:
        """Non-terminal operations left behind by a previous process."""

        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(deletion_operations.c.operation_id).where(
                            deletion_operations.c.status.in_(
                                (
                                    DeletionOperationStatus.QUEUED.value,
                                    DeletionOperationStatus.RUNNING.value,
                                )
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    def has_collection_deletion(self, collection_id: str) -> bool:
        """Return whether a retained aggregate tombstone fences a collection."""

        return bool(self._call(self._has_collection_deletion_db(collection_id)))

    async def _has_collection_deletion_db(self, collection_id: str) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            operation_id = await session.scalar(
                select(deletion_operations.c.operation_id)
                .where(
                    deletion_operations.c.tenant_id == DEFAULT_TENANT,
                    or_(
                        and_(
                            deletion_operations.c.target_kind
                            == DeletionTargetKind.VECTOR_INDEX.value,
                            deletion_operations.c.status
                            != DeletionOperationStatus.DELETED.value,
                            deletion_operations.c.context.op("->>")(
                                "server_collection_id"
                            )
                            == collection_id,
                        ),
                        and_(
                            deletion_operations.c.target_kind
                            == DeletionTargetKind.KNOWLEDGE_COLLECTION.value,
                            deletion_operations.c.context.op("->>")("collection_id")
                            == collection_id,
                            or_(
                                deletion_operations.c.status.in_(
                                    (
                                        DeletionOperationStatus.QUEUED.value,
                                        DeletionOperationStatus.RUNNING.value,
                                    )
                                ),
                                and_(
                                    deletion_operations.c.status
                                    == DeletionOperationStatus.DELETE_FAILED.value,
                                    deletion_operations.c.completed_items > 0,
                                ),
                            ),
                        ),
                    ),
                )
                .limit(1)
            )
            return operation_id is not None

    def has_document_deletion(self, document_id: str) -> bool:
        """Return whether a retained aggregate tombstone hides a document."""

        return bool(self._call(self._has_document_deletion_db(document_id)))

    async def _has_document_deletion_db(self, document_id: str) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            operation_id = await session.scalar(
                select(deletion_operations.c.operation_id)
                .where(
                    deletion_operations.c.tenant_id == DEFAULT_TENANT,
                    deletion_operations.c.target_kind
                    == DeletionTargetKind.KNOWLEDGE_DOCUMENT.value,
                    deletion_operations.c.context.op("->>")("document_id")
                    == document_id,
                    or_(
                        deletion_operations.c.status.in_(
                            (
                                DeletionOperationStatus.QUEUED.value,
                                DeletionOperationStatus.RUNNING.value,
                            )
                        ),
                        and_(
                            deletion_operations.c.status
                            == DeletionOperationStatus.DELETE_FAILED.value,
                            deletion_operations.c.completed_items > 0,
                        ),
                    ),
                )
                .limit(1)
            )
            return operation_id is not None

    def list_operations(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self.expire_stalled_operations()
        records, next_cursor = self._call(
            self._list_operations_db(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                limit=limit,
                after=after,
            )
        )
        return [build_deletion_summary(record) for record in records], next_cursor

    def retry(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        work: DeletionWork | None = None,
        before_dispatch=None,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """Reopen a failed operation and dispatch it again.

        ``dispatch=False`` reopens without enqueueing, for a caller that
        claims and runs the operation itself.
        """
        refuse_on_event_loop("retry")
        # Knowledge-document tombstones are restored inside the same database
        # transaction as the operation retry below. Volatile stores use this
        # callback to provide the equivalent pre-dispatch boundary.
        del before_dispatch
        # Best-effort expiry first (bounded by grace + throttle): a retry
        # against a lost RUNNING receipt should find the row already in
        # ``delete_failed`` within that window.
        self.expire_stalled_operations()
        record = self._call(
            self._retry_db(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )
        if not dispatch:
            return build_deletion_summary(record)
        if self._queue is not None:
            self._queue.enqueue(operation_id=operation_id, tenant_id=tenant_id)
        else:
            if work is None:
                raise DeletionOperationConflict(operation_id)
            with self._lock:
                self._local[operation_id] = _LocalJob(work=work)
                self._pending.append(operation_id)
                self._dispatch_locked()
        return build_deletion_summary(record)

    def find_for_asset(
        self,
        asset_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any] | None:
        record = self._call(
            self._find_for_asset_db(
                asset_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )
        return build_deletion_summary(record) if record is not None else None

    def find_for_target(
        self,
        target_kind: DeletionTargetKind,
        target_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any] | None:
        record = self._call(
            self._find_for_target_db(
                target_kind,
                target_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )
        return build_deletion_summary(record) if record is not None else None

    def progress(
        self,
        operation_id: str,
        *,
        stage: DeletionStage,
        completed_items: int,
        total_items: int,
        fence_attempt: int | None = None,
    ) -> bool:
        return self._call(
            self._progress_db(
                operation_id,
                stage=stage,
                completed_items=max(0, completed_items),
                total_items=max(0, total_items),
                fence_attempt=fence_attempt,
            )
        )

    def is_attempt_current(self, operation_id: str, *, fence_attempt: int) -> bool:
        return self._call(
            self._is_attempt_current_db(operation_id, fence_attempt=fence_attempt)
        )

    def checkpoint_source_cleanup(
        self,
        operation_id: str,
        *,
        asset_id: str,
        plan: dict[str, Any],
        fence_attempt: int | None = None,
    ) -> bool:
        return self._call(
            self._checkpoint_source_cleanup_db(
                operation_id,
                asset_id=asset_id,
                plan=plan,
                fence_attempt=fence_attempt,
            )
        )

    def source_deletion_permit(
        self,
        operation_id: str,
        *,
        scope: SourceScope,
        fence_attempt: int | None = None,
    ):
        return self._call(
            self._source_deletion_permit_db(
                operation_id,
                scope=scope,
                fence_attempt=fence_attempt,
            )
        )

    def complete(self, operation_id: str, *, fence_attempt: int | None = None) -> bool:
        return self._call(
            self._terminal_db(
                operation_id,
                status=DeletionOperationStatus.DELETED,
                stage=DeletionStage.DELETED,
                error=None,
                fence_attempt=fence_attempt,
            )
        )

    def fail(
        self,
        operation_id: str,
        message: str,
        *,
        error_type: str = "server_error",
        fence_attempt: int | None = None,
    ) -> bool:
        return self._call(
            self._terminal_db(
                operation_id,
                status=DeletionOperationStatus.DELETE_FAILED,
                stage=DeletionStage.DELETE_FAILED,
                error={
                    "message": sanitize_error(message),
                    "type": error_type,
                },
                fence_attempt=fence_attempt,
            )
        )

    def claim_for_execution(
        self, operation_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedDeletionOperation | None:
        return self._call(
            self._claim_db(operation_id, tenant_id, allow_takeover=allow_takeover)
        )

    def stale_queued_operations(
        self, *, older_than_seconds: float
    ) -> list[tuple[str, str]]:
        return self._call(self._stale_queued_db(older_than_seconds))

    def cancel_requested_operations(self, watched: dict[str, str]) -> set[str]:
        del watched
        return set()

    def _make_handle(self, operation_id: str, cancel_event) -> DeletionJobHandle:
        del cancel_event
        return DeletionJobHandle(self, operation_id)

    def _terminate_work_exception(
        self,
        handle: DeletionJobHandle,
        operation_id: str,
        exc: BaseException,
    ) -> None:
        """Apply the same stable failure type as the external deletion worker."""

        del operation_id
        handle.fail(
            sanitize_error(exc),
            error_type=classify_execution_failure(exc),
        )

    def _auto_complete(self, operation_id: str) -> None:
        self.complete(operation_id)

    async def _submit_db(
        self,
        *,
        target_kind: DeletionTargetKind,
        target_id: str,
        manifest: tuple[DeletionManifestItem, ...],
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        vector_index_context: VectorIndexDeletionContext | None,
        knowledge_context: KnowledgeDeletionContext | None,
        session_context: SessionDeletionContext | None,
        total_items: int | None,
        operation_id: str | None = None,
    ) -> tuple[DeletionOperationRecord, bool]:
        now = time.time()
        operation_id = operation_id or new_deletion_operation_id()
        try:
            async with self._session(tenant_id) as session:
                locked_knowledge_document_id: str | None = None
                if target_kind == DeletionTargetKind.VECTOR_INDEX:
                    if (
                        vector_index_context is None
                        or vector_index_context.index_id != target_id
                    ):
                        raise ValueError(
                            "vector-index deletion requires its immutable context"
                        )
                    locked_index = (
                        await session.execute(
                            select(vector_index_records)
                            .where(
                                vector_index_records.c.tenant_id == tenant_id,
                                vector_index_records.c.id == target_id,
                                vector_index_records.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                vector_index_records.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                            )
                            .with_for_update()
                        )
                    ).first()
                    if locked_index is None:
                        raise VectorIndexNotFound(target_id)
                    if vector_index_context.server_collection_id:
                        collection_exists = await session.scalar(
                            select(knowledge_collections.c.id).where(
                                knowledge_collections.c.tenant_id == tenant_id,
                                knowledge_collections.c.id
                                == vector_index_context.server_collection_id,
                            )
                        )
                        if collection_exists is not None:
                            access = await lock_resource_access(
                                session,
                                tenant_id=tenant_id,
                                actor_user_id=created_by_user_id,
                                resource_type="knowledge_collection",
                                resource_table=knowledge_collections,
                                id_column=knowledge_collections.c.id,
                                resource_id=(vector_index_context.server_collection_id),
                                owner_column=(
                                    knowledge_collections.c.created_by_user_id
                                ),
                                minimum=SharePermission.VIEW,
                                restrict_to_workspace_members=(
                                    self._restrict_to_workspace_members
                                ),
                                sharing_enabled=self._sharing_enabled,
                                owner_only=True,
                            )
                            if access is None:
                                raise CollectionNotFound(
                                    vector_index_context.server_collection_id
                                )
                elif target_kind == DeletionTargetKind.KNOWLEDGE_COLLECTION:
                    if (
                        knowledge_context is None
                        or knowledge_context.target_kind != target_kind
                        or knowledge_context.collection_id != target_id
                        or knowledge_context.document_id is not None
                    ):
                        raise ValueError(
                            "knowledge-collection deletion requires its immutable context"
                        )
                    access = await lock_resource_access(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=created_by_user_id,
                        resource_type="knowledge_collection",
                        resource_table=knowledge_collections,
                        id_column=knowledge_collections.c.id,
                        resource_id=target_id,
                        owner_column=knowledge_collections.c.created_by_user_id,
                        minimum=SharePermission.VIEW,
                        restrict_to_workspace_members=(
                            self._restrict_to_workspace_members
                        ),
                        sharing_enabled=self._sharing_enabled,
                        owner_only=True,
                    )
                    if access is None:
                        raise CollectionNotFound(target_id)
                    stored_model = await session.scalar(
                        select(knowledge_collections.c.embedding_model).where(
                            knowledge_collections.c.tenant_id == tenant_id,
                            knowledge_collections.c.id == target_id,
                        )
                    )
                    if stored_model != knowledge_context.embedding_model:
                        raise DeletionOperationConflict(target_id)
                elif target_kind == DeletionTargetKind.KNOWLEDGE_DOCUMENT:
                    if (
                        knowledge_context is None
                        or knowledge_context.target_kind != target_kind
                        or knowledge_context.document_id != target_id
                    ):
                        raise ValueError(
                            "knowledge-document deletion requires its immutable context"
                        )
                    preliminary_document = (
                        await session.execute(
                            select(
                                knowledge_documents.c.collection_id,
                                knowledge_documents.c.lifecycle_status,
                            ).where(
                                knowledge_documents.c.tenant_id == tenant_id,
                                knowledge_documents.c.id == target_id,
                            )
                        )
                    ).first()
                    if (
                        preliminary_document is None
                        or preliminary_document.collection_id
                        != knowledge_context.collection_id
                    ):
                        raise DocumentNotFound(target_id)
                    access = await lock_resource_access(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=created_by_user_id,
                        resource_type="knowledge_collection",
                        resource_table=knowledge_collections,
                        id_column=knowledge_collections.c.id,
                        resource_id=knowledge_context.collection_id,
                        owner_column=knowledge_collections.c.created_by_user_id,
                        minimum=SharePermission.EDIT,
                        restrict_to_workspace_members=(
                            self._restrict_to_workspace_members
                        ),
                        sharing_enabled=self._sharing_enabled,
                    )
                    if access is None:
                        raise DocumentNotFound(target_id)
                    locked_document = (
                        await session.execute(
                            select(
                                knowledge_documents.c.id,
                                knowledge_documents.c.collection_id,
                            )
                            .where(
                                knowledge_documents.c.tenant_id == tenant_id,
                                knowledge_documents.c.id == target_id,
                            )
                            .with_for_update()
                        )
                    ).first()
                    if (
                        locked_document is None
                        or locked_document.collection_id
                        != knowledge_context.collection_id
                    ):
                        raise DocumentNotFound(target_id)
                    stored_model = await session.scalar(
                        select(knowledge_collections.c.embedding_model).where(
                            knowledge_collections.c.tenant_id == tenant_id,
                            knowledge_collections.c.id
                            == knowledge_context.collection_id,
                        )
                    )
                    if stored_model != knowledge_context.embedding_model:
                        raise DeletionOperationConflict(target_id)
                    locked_knowledge_document_id = target_id
                elif target_kind in SESSION_DELETION_TARGET_KINDS:
                    if (
                        session_context is None
                        or session_context.target_kind != target_kind
                        or session_context.session_id != target_id
                    ):
                        raise ValueError(
                            "session deletion requires its immutable context"
                        )
                    session_table = (
                        agent_sessions
                        if target_kind == DeletionTargetKind.AGENT_SESSION
                        else knowledge_sessions
                    )
                    locked_session = (
                        await session.execute(
                            select(session_table.c.id)
                            .where(
                                session_table.c.tenant_id == tenant_id,
                                session_table.c.id == target_id,
                                session_table.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                session_table.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                            )
                            .with_for_update()
                        )
                    ).first()
                    if locked_session is None:
                        if target_kind == DeletionTargetKind.AGENT_SESSION:
                            raise AgentSessionNotFound(target_id)
                        raise KnowledgeSessionNotFound(target_id)
                    if target_kind == DeletionTargetKind.AGENT_SESSION:
                        session_roots = select(runs.c.run_id).where(
                            runs.c.tenant_id == tenant_id,
                            runs.c.session_id == target_id,
                        )
                        run_ids = tuple(
                            (
                                await session.execute(
                                    select(runs.c.run_id)
                                    .where(
                                        runs.c.tenant_id == tenant_id,
                                        or_(
                                            runs.c.session_id == target_id,
                                            runs.c.root_run_id.in_(session_roots),
                                        ),
                                    )
                                    .order_by(runs.c.created_at, runs.c.run_id)
                                )
                            ).scalars()
                        )
                    else:
                        linked_runs = (
                            (
                                await session.execute(
                                    select(
                                        runs.c.run_id,
                                        runs.c.created_by_tenant_id,
                                        runs.c.created_by_user_id,
                                        runs.c.workspace_id,
                                        runs.c.mode,
                                        runs.c.kind,
                                    )
                                    .where(
                                        runs.c.tenant_id == tenant_id,
                                        runs.c.session_id == target_id,
                                    )
                                    .order_by(runs.c.created_at, runs.c.run_id)
                                )
                            )
                            .mappings()
                            .all()
                        )
                        if any(
                            row["created_by_tenant_id"] not in (None, tenant_id)
                            or row["created_by_user_id"] != created_by_user_id
                            or row["workspace_id"] != workspace_id
                            or row["mode"] != "knowledge"
                            or row["kind"] != "standard"
                            for row in linked_runs
                        ):
                            raise DeletionOperationConflict(target_id)
                        run_ids = tuple(
                            str(row["run_id"]) for row in linked_runs
                        )
                    session_context = SessionDeletionContext(
                        target_kind=target_kind,
                        session_id=target_id,
                        run_ids=run_ids,
                    )
                destructive_collection_id = (
                    vector_index_context.server_collection_id
                    if vector_index_context is not None
                    else (
                        knowledge_context.collection_id
                        if knowledge_context is not None
                        else None
                    )
                )
                if destructive_collection_id and target_kind in {
                    DeletionTargetKind.VECTOR_INDEX,
                    DeletionTargetKind.KNOWLEDGE_COLLECTION,
                    DeletionTargetKind.KNOWLEDGE_DOCUMENT,
                }:
                    blocking_kinds = {
                        DeletionTargetKind.VECTOR_INDEX.value,
                        DeletionTargetKind.KNOWLEDGE_COLLECTION.value,
                    }
                    if target_kind != DeletionTargetKind.KNOWLEDGE_DOCUMENT:
                        blocking_kinds.add(DeletionTargetKind.KNOWLEDGE_DOCUMENT.value)
                    blocking = (
                        (
                            await session.execute(
                                select(deletion_operations).where(
                                    deletion_operations.c.tenant_id == tenant_id,
                                    deletion_operations.c.target_kind.in_(
                                        blocking_kinds
                                    ),
                                    or_(
                                        deletion_operations.c.context.op("->>")(
                                            "server_collection_id"
                                        )
                                        == destructive_collection_id,
                                        deletion_operations.c.context.op("->>")(
                                            "collection_id"
                                        )
                                        == destructive_collection_id,
                                    ),
                                    or_(
                                        deletion_operations.c.status.in_(
                                            (
                                                DeletionOperationStatus.QUEUED.value,
                                                DeletionOperationStatus.RUNNING.value,
                                            )
                                        ),
                                        and_(
                                            deletion_operations.c.status
                                            == DeletionOperationStatus.DELETE_FAILED.value,
                                            deletion_operations.c.completed_items > 0,
                                        ),
                                    ),
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if blocking is not None:
                        if (
                            blocking["target_kind"] == target_kind.value
                            and blocking["target_id"] == target_id
                            and blocking["created_by_user_id"] == created_by_user_id
                        ):
                            return _record_from_mapping(blocking), False
                        raise DeletionOperationConflict(destructive_collection_id)
                if target_kind == DeletionTargetKind.SECTION:
                    await lock_section_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        section_id=target_id,
                    )
                    candidate_ids = list(
                        (
                            await session.execute(
                                select(asset_records.c.id).where(
                                    asset_records.c.tenant_id == tenant_id,
                                    asset_records.c.section_id == target_id,
                                    asset_records.c.created_by_user_id.is_not_distinct_from(
                                        created_by_user_id
                                    ),
                                    asset_records.c.workspace_id.is_not_distinct_from(
                                        workspace_id
                                    ),
                                )
                            )
                        ).scalars()
                    )
                elif target_kind == DeletionTargetKind.GROUP:
                    await lock_group_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        group_id=target_id,
                    )
                    group_exists = (
                        await session.execute(
                            select(asset_groups.c.id)
                            .where(
                                asset_groups.c.tenant_id == tenant_id,
                                asset_groups.c.id == target_id,
                                asset_groups.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                asset_groups.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if group_exists is None:
                        raise GroupNotFound(target_id)
                    candidate_ids = []
                else:
                    candidate_ids = [item.asset_id for item in manifest]
                for asset_id in sorted(set(candidate_ids)):
                    await lock_asset_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        asset_id=asset_id,
                    )
                if target_kind == DeletionTargetKind.SECTION:
                    # No child can enter this section while its lock is held.
                    # Re-read after acquiring every child lock so a concurrent
                    # move/finalise that won first is reflected in the durable
                    # manifest rather than being lost to the later FK cascade.
                    current_rows = (
                        await session.execute(
                            select(
                                asset_records.c.id,
                                asset_records.c.server_file_id,
                                asset_records.c.size_bytes,
                                asset_records.c.upload_operation_id,
                                asset_records.c.created_by_user_id,
                            ).where(
                                asset_records.c.tenant_id == tenant_id,
                                asset_records.c.section_id == target_id,
                                asset_records.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                asset_records.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                            )
                        )
                    ).all()
                    pending_ids = [
                        row.upload_operation_id
                        for row in current_rows
                        if row.upload_operation_id is not None
                    ]
                    pending_rows = (
                        (
                            await session.execute(
                                select(
                                    upload_operations.c.operation_id,
                                    upload_operations.c.asset_id,
                                    upload_operations.c.file_id,
                                    upload_operations.c.file_manifest,
                                ).where(
                                    upload_operations.c.tenant_id == tenant_id,
                                    upload_operations.c.operation_id.in_(pending_ids),
                                )
                            )
                        )
                        .mappings()
                        .all()
                        if pending_ids
                        else []
                    )
                    pending_by_id = {row["operation_id"]: row for row in pending_rows}
                    manifest = tuple(
                        DeletionManifestItem(
                            asset_id=row.id,
                            source_id=f"asset:{row.id}",
                            server_file_id=(
                                row.server_file_id
                                or (
                                    pending_by_id[row.upload_operation_id]["file_id"]
                                    if row.upload_operation_id in pending_by_id
                                    else None
                                )
                            ),
                            size_bytes=(
                                int(row.size_bytes)
                                if row.server_file_id is not None
                                else (
                                    int(
                                        pending_by_id[row.upload_operation_id][
                                            "file_manifest"
                                        ].get("size_bytes", row.size_bytes)
                                    )
                                    if row.upload_operation_id in pending_by_id
                                    else int(row.size_bytes)
                                )
                            ),
                            upload_operation_id=row.upload_operation_id,
                            file_owner_user_id=row.created_by_user_id,
                        )
                        for row in current_rows
                    )
                elif manifest:
                    # The service-level manifest is an authorization snapshot,
                    # not the deletion truth: an upload may have finalised
                    # between that read and this transaction winning the
                    # lifecycle lock.  Refresh blob/size facts while holding
                    # every asset lock so the worker can never omit a newly
                    # attached original file.
                    ids = [item.asset_id for item in manifest]
                    current_rows = (
                        await session.execute(
                            select(
                                asset_records.c.id,
                                asset_records.c.server_file_id,
                                asset_records.c.size_bytes,
                                asset_records.c.upload_operation_id,
                                asset_records.c.created_by_user_id,
                            ).where(
                                asset_records.c.tenant_id == tenant_id,
                                asset_records.c.id.in_(ids),
                                asset_records.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                asset_records.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                            )
                        )
                    ).all()
                    current_by_id = {row.id: row for row in current_rows}
                    pending_ids = [
                        row.upload_operation_id
                        for row in current_rows
                        if row.upload_operation_id is not None
                    ]
                    pending_rows = (
                        (
                            await session.execute(
                                select(
                                    upload_operations.c.operation_id,
                                    upload_operations.c.asset_id,
                                    upload_operations.c.file_id,
                                    upload_operations.c.file_manifest,
                                ).where(
                                    upload_operations.c.tenant_id == tenant_id,
                                    upload_operations.c.operation_id.in_(pending_ids),
                                )
                            )
                        )
                        .mappings()
                        .all()
                        if pending_ids
                        else []
                    )
                    pending_by_id = {row["operation_id"]: row for row in pending_rows}
                    manifest = tuple(
                        DeletionManifestItem(
                            asset_id=item.asset_id,
                            source_id=item.source_id,
                            server_file_id=(
                                current_by_id[item.asset_id].server_file_id
                                if item.asset_id in current_by_id
                                and current_by_id[item.asset_id].server_file_id
                                is not None
                                else (
                                    pending_by_id[
                                        current_by_id[item.asset_id].upload_operation_id
                                    ]["file_id"]
                                    if item.asset_id in current_by_id
                                    and current_by_id[item.asset_id].upload_operation_id
                                    in pending_by_id
                                    else item.server_file_id
                                )
                            ),
                            size_bytes=(
                                int(current_by_id[item.asset_id].size_bytes)
                                if item.asset_id in current_by_id
                                and current_by_id[item.asset_id].server_file_id
                                is not None
                                else (
                                    int(
                                        pending_by_id[
                                            current_by_id[
                                                item.asset_id
                                            ].upload_operation_id
                                        ]["file_manifest"].get(
                                            "size_bytes",
                                            current_by_id[item.asset_id].size_bytes,
                                        )
                                    )
                                    if item.asset_id in current_by_id
                                    and current_by_id[item.asset_id].upload_operation_id
                                    in pending_by_id
                                    else item.size_bytes
                                )
                            ),
                            upload_operation_id=(
                                current_by_id[item.asset_id].upload_operation_id
                                if item.asset_id in current_by_id
                                else item.upload_operation_id
                            ),
                            file_owner_user_id=(
                                current_by_id[item.asset_id].created_by_user_id
                                if item.asset_id in current_by_id
                                else item.file_owner_user_id
                            ),
                        )
                        for item in manifest
                    )
                await self._cleanup_db(session, now=now)
                requested_ids = {item.asset_id for item in manifest}
                if requested_ids:
                    overlapping = (
                        (
                            await session.execute(
                                select(deletion_operations)
                                .join(
                                    deletion_operation_assets,
                                    deletion_operation_assets.c.operation_id
                                    == deletion_operations.c.operation_id,
                                )
                                .where(
                                    deletion_operations.c.tenant_id == tenant_id,
                                    deletion_operation_assets.c.asset_id.in_(
                                        requested_ids
                                    ),
                                    deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                        created_by_user_id
                                    ),
                                    deletion_operations.c.workspace_id.is_not_distinct_from(
                                        workspace_id
                                    ),
                                    deletion_operations.c.status.in_(
                                        (
                                            DeletionOperationStatus.QUEUED.value,
                                            DeletionOperationStatus.RUNNING.value,
                                        )
                                    ),
                                )
                                .order_by(deletion_operations.c.created_at.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if overlapping is not None:
                        if (
                            overlapping["target_kind"] == target_kind.value
                            and overlapping["target_id"] == target_id
                        ):
                            return _record_from_mapping(overlapping), False
                        raise DeletionOperationConflict(overlapping["operation_id"])
                retained_statuses = [
                    DeletionOperationStatus.QUEUED.value,
                    DeletionOperationStatus.RUNNING.value,
                ]
                existing_conditions = [
                    deletion_operations.c.tenant_id == tenant_id,
                    deletion_operations.c.target_kind == target_kind.value,
                    deletion_operations.c.target_id == target_id,
                    deletion_operations.c.status.in_(retained_statuses),
                ]
                if target_kind in {
                    DeletionTargetKind.KNOWLEDGE_COLLECTION,
                    DeletionTargetKind.KNOWLEDGE_DOCUMENT,
                }:
                    existing_conditions[-1] = or_(
                        deletion_operations.c.status.in_(retained_statuses),
                        and_(
                            deletion_operations.c.status
                            == DeletionOperationStatus.DELETE_FAILED.value,
                            deletion_operations.c.completed_items > 0,
                        ),
                    )
                elif target_kind == DeletionTargetKind.USER:
                    # One erasure per account, whoever started it: a second
                    # administrator's request meets the running or failed
                    # operation instead of starting a parallel one.
                    existing_conditions[-1] = or_(
                        deletion_operations.c.status.in_(retained_statuses),
                        deletion_operations.c.status
                        == DeletionOperationStatus.DELETE_FAILED.value,
                    )
                elif target_kind in SESSION_DELETION_TARGET_KINDS:
                    existing_conditions[-1] = or_(
                        deletion_operations.c.status.in_(retained_statuses),
                        deletion_operations.c.status
                        == DeletionOperationStatus.DELETE_FAILED.value,
                    )
                    existing_conditions.extend(
                        (
                            deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            deletion_operations.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                    )
                else:
                    existing_conditions.extend(
                        (
                            deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            deletion_operations.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                    )
                existing = (
                    (
                        await session.execute(
                            select(deletion_operations).where(
                                *existing_conditions,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    if existing["created_by_user_id"] == created_by_user_id:
                        return _record_from_mapping(existing), False
                    raise DeletionOperationConflict(target_id)
                source_permits = {}
                for item in sorted(
                    manifest,
                    key=lambda candidate: candidate.source_id,
                ):
                    source_permits[item.source_id] = (
                        await self._source_authority.begin_delete_in_session(
                            session,
                            SourceScope(
                                tenant_id=tenant_id,
                                source_id=item.source_id,
                                owner_user_id=created_by_user_id,
                                workspace_id=workspace_id,
                            ),
                            operation_id=operation_id,
                        )
                    )
                manifest = await self._checkpoint_proven_empty_source_cleanup(
                    session,
                    manifest=manifest,
                    tenant_id=tenant_id,
                    source_permits=source_permits,
                )
                target_source = target_source_id(target_kind, target_id)
                if target_source is not None:
                    await self._source_authority.begin_delete_in_session(
                        session,
                        SourceScope(
                            tenant_id=tenant_id,
                            source_id=target_source,
                            owner_user_id=created_by_user_id,
                            workspace_id=workspace_id,
                        ),
                        operation_id=operation_id,
                    )
                await session.execute(
                    insert(deletion_operations).values(
                        operation_id=operation_id,
                        tenant_id=tenant_id,
                        target_kind=target_kind.value,
                        target_id=target_id,
                        manifest=[item.to_payload() for item in manifest],
                        context=(
                            vector_index_context.to_payload()
                            if vector_index_context is not None
                            else (
                                knowledge_context.to_payload()
                                if knowledge_context is not None
                                else (
                                    session_context.to_payload()
                                    if session_context is not None
                                    else {}
                                )
                            )
                        ),
                        status=DeletionOperationStatus.QUEUED.value,
                        stage=DeletionStage.QUEUED.value,
                        completed_items=0,
                        total_items=(
                            max(0, int(total_items))
                            if total_items is not None
                            else len(manifest)
                        ),
                        workspace_id=workspace_id,
                        created_by_user_id=created_by_user_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                if target_kind == DeletionTargetKind.VECTOR_INDEX:
                    await session.execute(
                        update(vector_index_records)
                        .where(
                            vector_index_records.c.tenant_id == tenant_id,
                            vector_index_records.c.id == target_id,
                            vector_index_records.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            vector_index_records.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                        .values(
                            status="deleting",
                            last_error=None,
                            updated_at=now,
                        )
                    )
                elif locked_knowledge_document_id is not None:
                    result = await session.execute(
                        update(knowledge_documents)
                        .where(
                            knowledge_documents.c.tenant_id == tenant_id,
                            knowledge_documents.c.id == locked_knowledge_document_id,
                            knowledge_documents.c.collection_id
                            == knowledge_context.collection_id,
                            knowledge_documents.c.lifecycle_status == "active",
                        )
                        .values(lifecycle_status="deleting")
                    )
                    if result.rowcount != 1:
                        raise DocumentNotFound(locked_knowledge_document_id)
                elif target_kind in SESSION_DELETION_TARGET_KINDS:
                    session_table = (
                        agent_sessions
                        if target_kind == DeletionTargetKind.AGENT_SESSION
                        else knowledge_sessions
                    )
                    result = await session.execute(
                        update(session_table)
                        .where(
                            session_table.c.tenant_id == tenant_id,
                            session_table.c.id == target_id,
                            session_table.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            session_table.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                            session_table.c.lifecycle_status == "active",
                        )
                        .values(
                            lifecycle_status="deleting",
                            deletion_operation_id=operation_id,
                            deletion_stage=DeletionStage.QUEUED.value,
                            deletion_error=None,
                        )
                    )
                    if result.rowcount != 1:
                        if target_kind == DeletionTargetKind.AGENT_SESSION:
                            raise AgentSessionNotFound(target_id)
                        raise KnowledgeSessionNotFound(target_id)
                if manifest:
                    await session.execute(
                        insert(deletion_operation_assets),
                        [
                            {
                                "operation_id": operation_id,
                                "asset_id": item.asset_id,
                                "tenant_id": tenant_id,
                                "created_by_user_id": created_by_user_id,
                                "workspace_id": workspace_id,
                            }
                            for item in manifest
                        ],
                    )
                if manifest:
                    ids = [item.asset_id for item in manifest]
                    updated = await session.execute(
                        update(asset_records)
                        .where(
                            asset_records.c.tenant_id == tenant_id,
                            asset_records.c.id.in_(ids),
                            asset_records.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            asset_records.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                            asset_records.c.lifecycle_status == "active",
                        )
                        .values(
                            lifecycle_status="deleting",
                            deletion_operation_id=operation_id,
                            deletion_stage=DeletionStage.QUEUED.value,
                            deletion_error=None,
                            updated_at=now,
                        )
                        .returning(asset_records.c.id)
                    )
                    updated_count = len(updated.scalars().all())
                    if (
                        updated_count != len(ids)
                        and target_kind != DeletionTargetKind.ASSET
                    ):
                        raise AssetNotFound(target_id)
                await self._append_event_db(
                    session,
                    operation_id,
                    tenant_id,
                    "inqtrix.deletion.queued",
                    {"status": "queued", "stage": "queued"},
                    now=now,
                )
                # Dienststart-Index: the delete REQUEST lands atomically
                # with the operation row (dedup/replay paths above write
                # no second entry). Manifest contents stay out — only
                # counts; the manifest itself remains the deep audit
                # anchor on the operation record.
                await append_audit_row(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=created_by_user_id,
                    action="asset.delete_requested",
                    resource_type=str(target_kind.value),
                    resource_id=target_id,
                    detail={"manifest_items": len(manifest)},
                    correlation={"run_id": operation_id},
                    workspace_id=_workspace_uuid(workspace_id),
                )
                row = (
                    (
                        await session.execute(
                            select(deletion_operations).where(
                                deletion_operations.c.operation_id == operation_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return _record_from_mapping(row), True
        except IntegrityError:
            # A concurrent submit won the partial unique target index.
            async with self._session(tenant_id) as session:
                row = (
                    (
                        await session.execute(
                            select(deletion_operations).where(
                                deletion_operations.c.tenant_id == tenant_id,
                                deletion_operations.c.target_kind == target_kind.value,
                                deletion_operations.c.target_id == target_id,
                                deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                deletion_operations.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                                deletion_operations.c.status.in_(("queued", "running")),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise
                return _record_from_mapping(row), False

    async def _checkpoint_proven_empty_source_cleanup(
        self,
        session: "AsyncSession",
        *,
        manifest: tuple[DeletionManifestItem, ...],
        tenant_id: str,
        source_permits: dict[str, Any],
    ) -> tuple[DeletionManifestItem, ...]:
        """Persist an empty cleanup proof only under the canonical source fence.

        Asset deletion must remain usable when the optional Knowledge
        capability is disabled, but an unavailable cleanup service can never
        be interpreted as zero residuals.  The durable operation transaction
        already owns both the asset lifecycle locks and the source deletion
        permits, so it is the one place that can prove absence without a
        second search architecture: no canonical Knowledge document matches
        the source contract and no server-backed vector-index membership can
        still create one.
        """

        candidates = tuple(
            item for item in manifest if item.source_cleanup_plan is None
        )
        if not candidates:
            return manifest

        scope_predicates = []
        operation_scopes = set()
        for item in candidates:
            permit = source_permits.get(item.source_id)
            if permit is None:
                continue
            scope = permit.scope
            operation_scopes.add(
                (scope.owner_user_id, scope.workspace_id)
            )
            legacy_id = item.source_id.removeprefix("asset:")
            scope_predicates.append(
                and_(
                    or_(
                        knowledge_documents.c.source_id == item.source_id,
                        knowledge_documents.c.metadata["fileId"].as_string()
                        == legacy_id,
                        knowledge_documents.c.metadata["file_id"].as_string()
                        == legacy_id,
                    ),
                    knowledge_documents.c.source_owner_user_id.is_not_distinct_from(
                        scope.owner_user_id
                    ),
                    knowledge_documents.c.source_workspace_id.is_not_distinct_from(
                        scope.workspace_id
                    ),
                )
            )
        if len(operation_scopes) != 1 or not scope_predicates:
            # A durable asset operation is one owner/workspace aggregate.
            # Anything else is an invalid authority checkpoint, never proof
            # that cleanup is empty.
            return manifest
        operation_owner_user_id, operation_workspace_id = next(
            iter(operation_scopes)
        )
        document_rows = (
            await session.execute(
                select(
                    knowledge_documents.c.source_id,
                    knowledge_documents.c.metadata["fileId"]
                    .as_string()
                    .label("file_id_hint"),
                    knowledge_documents.c.metadata["file_id"]
                    .as_string()
                    .label("legacy_file_id_hint"),
                ).where(
                    knowledge_documents.c.tenant_id == tenant_id,
                    or_(*scope_predicates),
                )
            )
        ).all()
        server_linked_asset_ids = set(
            (
                await session.execute(
                    select(vector_index_members.c.file_id)
                    .select_from(
                        vector_index_members.join(
                            vector_index_records,
                            and_(
                                vector_index_members.c.index_id
                                == vector_index_records.c.id,
                                vector_index_members.c.tenant_id
                                == vector_index_records.c.tenant_id,
                            ),
                        )
                    )
                    .where(
                        vector_index_members.c.tenant_id == tenant_id,
                        vector_index_records.c.created_by_user_id.is_not_distinct_from(
                            operation_owner_user_id
                        ),
                        vector_index_records.c.workspace_id.is_not_distinct_from(
                            operation_workspace_id
                        ),
                        vector_index_members.c.file_id.in_(
                            [item.asset_id for item in candidates]
                        ),
                        or_(
                            vector_index_records.c.server_collection_id.is_not(
                                None
                            ),
                            vector_index_members.c.server_document_id.is_not(None),
                        ),
                    )
                )
            ).scalars()
        )

        linked_source_ids: set[str] = set()
        for item in candidates:
            legacy_id = item.source_id.removeprefix("asset:")
            if any(
                row.source_id == item.source_id
                or row.file_id_hint == legacy_id
                or row.legacy_file_id_hint == legacy_id
                for row in document_rows
            ):
                linked_source_ids.add(item.source_id)
            if item.asset_id in server_linked_asset_ids:
                linked_source_ids.add(item.source_id)

        checkpointed: list[DeletionManifestItem] = []
        for item in manifest:
            permit = source_permits.get(item.source_id)
            if (
                item.source_cleanup_plan is None
                and item.source_id not in linked_source_ids
                and permit is not None
            ):
                item = replace(
                    item,
                    source_cleanup_plan=empty_source_cleanup_plan(permit).as_dict(),
                )
            checkpointed.append(item)
        return tuple(checkpointed)

    async def _find_for_asset_db(
        self,
        asset_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord | None:
        async with self._session(tenant_id) as session:
            conditions = [
                deletion_operation_assets.c.tenant_id == tenant_id,
                deletion_operation_assets.c.asset_id == asset_id,
                deletion_operation_assets.c.created_by_user_id.is_not_distinct_from(
                    created_by_user_id
                ),
            ]
            if workspace_id is not None:
                conditions.append(
                    deletion_operation_assets.c.workspace_id == workspace_id
                )
            row = (
                (
                    await session.execute(
                        select(deletion_operations)
                        .join(
                            deletion_operation_assets,
                            deletion_operation_assets.c.operation_id
                            == deletion_operations.c.operation_id,
                        )
                        .where(*conditions)
                        .order_by(deletion_operations.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _record_from_mapping(row) if row is not None else None

    async def _list_operations_db(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[DeletionOperationRecord], str | None]:
        async with self._session(tenant_id) as session:
            await self._cleanup_db(session, now=time.time())
            conditions = [
                deletion_operations.c.tenant_id == tenant_id,
                deletion_operations.c.created_by_user_id.is_not_distinct_from(
                    created_by_user_id
                ),
            ]
            if workspace_id is not None:
                conditions.append(
                    or_(
                        deletion_operations.c.workspace_id == workspace_id,
                        deletion_operations.c.target_kind.in_(
                            _KNOWLEDGE_DELETION_TARGET_VALUES
                        ),
                    )
                )
            if after is not None:
                created_at, operation_id = after
                conditions.append(
                    or_(
                        deletion_operations.c.created_at < created_at,
                        and_(
                            deletion_operations.c.created_at == created_at,
                            deletion_operations.c.operation_id < operation_id,
                        ),
                    )
                )
            rows = (
                (
                    await session.execute(
                        select(deletion_operations)
                        .where(*conditions)
                        .order_by(
                            deletion_operations.c.created_at.desc(),
                            deletion_operations.c.operation_id.desc(),
                        )
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        page = rows[:limit]
        next_cursor = (
            encode_cursor(
                float(page[-1]["created_at"]),
                str(page[-1]["operation_id"]),
            )
            if len(rows) > limit and page
            else None
        )
        return [_record_from_mapping(row) for row in page], next_cursor

    async def _find_for_target_db(
        self,
        target_kind: DeletionTargetKind,
        target_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord | None:
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        select(deletion_operations)
                        .where(
                            deletion_operations.c.tenant_id == tenant_id,
                            deletion_operations.c.target_kind == target_kind.value,
                            deletion_operations.c.target_id == target_id,
                            deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            or_(
                                deletion_operations.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                                deletion_operations.c.target_kind.in_(
                                    _KNOWLEDGE_DELETION_TARGET_VALUES
                                ),
                            ),
                        )
                        .order_by(deletion_operations.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _record_from_mapping(row) if row is not None else None

    async def _get_record_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord:
        async with self._session(tenant_id) as session:
            await self._cleanup_db(session, now=time.time())
            row = (
                (
                    await session.execute(
                        select(deletion_operations).where(
                            deletion_operations.c.operation_id == operation_id,
                            deletion_operations.c.tenant_id == tenant_id,
                            deletion_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            *(
                                (
                                    or_(
                                        deletion_operations.c.workspace_id
                                        == workspace_id,
                                        deletion_operations.c.target_kind.in_(
                                            _KNOWLEDGE_DELETION_TARGET_VALUES
                                        ),
                                    ),
                                )
                                if workspace_id is not None
                                else ()
                            ),
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DeletionOperationNotFound(operation_id)
        return _record_from_mapping(row)

    async def _retry_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord:
        now = time.time()
        async with self._session(tenant_id) as session:
            conditions = [
                deletion_operations.c.operation_id == operation_id,
                deletion_operations.c.tenant_id == tenant_id,
                deletion_operations.c.created_by_user_id.is_not_distinct_from(
                    created_by_user_id
                ),
                deletion_operations.c.status
                == DeletionOperationStatus.DELETE_FAILED.value,
            ]
            if workspace_id is not None:
                conditions.append(
                    or_(
                        deletion_operations.c.workspace_id == workspace_id,
                        deletion_operations.c.target_kind.in_(
                            _KNOWLEDGE_DELETION_TARGET_VALUES
                        ),
                    )
                )
            row = (
                (
                    await session.execute(
                        update(deletion_operations)
                        .where(*conditions)
                        .values(
                            status=DeletionOperationStatus.QUEUED.value,
                            stage=DeletionStage.QUEUED.value,
                            error=None,
                            finished_at=None,
                            claimed_by=None,
                            updated_at=now,
                        )
                        .returning(deletion_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                visibility_conditions = [
                    deletion_operations.c.operation_id == operation_id,
                    deletion_operations.c.tenant_id == tenant_id,
                    deletion_operations.c.created_by_user_id.is_not_distinct_from(
                        created_by_user_id
                    ),
                ]
                if workspace_id is not None:
                    visibility_conditions.append(
                        or_(
                            deletion_operations.c.workspace_id == workspace_id,
                            deletion_operations.c.target_kind.in_(
                                _KNOWLEDGE_DELETION_TARGET_VALUES
                            ),
                        )
                    )
                exists = (
                    await session.execute(
                        select(deletion_operations.c.operation_id).where(
                            *visibility_conditions
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise DeletionOperationNotFound(operation_id)
                raise DeletionOperationConflict(operation_id)
            if (
                row["target_kind"] == DeletionTargetKind.KNOWLEDGE_DOCUMENT.value
                and int(row["completed_items"] or 0) == 0
            ):
                document_id = (row["context"] or {}).get("document_id")
                if not isinstance(document_id, str) or not document_id:
                    raise DeletionOperationConflict(operation_id)
                result = await session.execute(
                    update(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == tenant_id,
                        knowledge_documents.c.id == document_id,
                        knowledge_documents.c.lifecycle_status.in_(
                            ("active", "deleting")
                        ),
                    )
                    .values(lifecycle_status="deleting")
                )
                if result.rowcount != 1:
                    raise DeletionOperationConflict(operation_id)
            if row["target_kind"] in _SESSION_DELETION_TARGET_VALUES:
                session_table = (
                    agent_sessions
                    if row["target_kind"] == DeletionTargetKind.AGENT_SESSION.value
                    else knowledge_sessions
                )
                changed = await session.execute(
                    update(session_table)
                    .where(
                        session_table.c.tenant_id == tenant_id,
                        session_table.c.id == row["target_id"],
                        session_table.c.created_by_user_id.is_not_distinct_from(
                            created_by_user_id
                        ),
                        session_table.c.workspace_id.is_not_distinct_from(
                            row["workspace_id"]
                        ),
                        session_table.c.deletion_operation_id == operation_id,
                    )
                    .values(
                        lifecycle_status="deleting",
                        deletion_stage=DeletionStage.QUEUED.value,
                        deletion_error=None,
                    )
                )
                if changed.rowcount != 1:
                    raise DeletionOperationConflict(operation_id)
            manifest = tuple(
                DeletionManifestItem.from_payload(item)
                for item in (row["manifest"] or [])
            )
            ids = [item.asset_id for item in manifest]
            if ids:
                await session.execute(
                    update(asset_records)
                    .where(
                        asset_records.c.tenant_id == tenant_id,
                        asset_records.c.id.in_(ids),
                        asset_records.c.created_by_user_id.is_not_distinct_from(
                            created_by_user_id
                        ),
                        asset_records.c.workspace_id.is_not_distinct_from(workspace_id),
                    )
                    .values(
                        lifecycle_status="deleting",
                        deletion_operation_id=operation_id,
                        deletion_stage=DeletionStage.QUEUED.value,
                        deletion_error=None,
                    )
                )
            await self._append_event_db(
                session,
                operation_id,
                tenant_id,
                "inqtrix.deletion.retried",
                {"status": "queued", "stage": "queued"},
                now=now,
            )
            return _record_from_mapping(row)

    async def _claim_db(
        self, operation_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedDeletionOperation | None:
        async with self._session(tenant_id) as session:
            # Cutover fence, FIRST statement of the claim transaction: a
            # worker whose image predates a completed migration must not
            # take a durable claim -- nor run this transaction's other
            # writes -- against a schema its code no longer matches. Rides
            # in the same transaction, so a mismatch rolls everything back
            # and the queue entry stays unacked for an upgraded worker.
            await assert_schema_head(session)
            allowed = [DeletionOperationStatus.QUEUED.value]
            if allow_takeover:
                allowed.append(DeletionOperationStatus.RUNNING.value)
            now = time.time()
            row = (
                (
                    await session.execute(
                        update(deletion_operations)
                        .where(
                            deletion_operations.c.operation_id == operation_id,
                            deletion_operations.c.tenant_id == tenant_id,
                            deletion_operations.c.status.in_(allowed),
                        )
                        .values(
                            status=DeletionOperationStatus.RUNNING.value,
                            claimed_by=self._worker_id,
                            attempt=deletion_operations.c.attempt + 1,
                            started_at=func.coalesce(
                                deletion_operations.c.started_at, now
                            ),
                            updated_at=now,
                        )
                        .returning(deletion_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            await self._append_event_db(
                session,
                operation_id,
                tenant_id,
                "inqtrix.deletion.started",
                {"status": "running", "attempt": int(row["attempt"])},
                now=now,
            )
            record = _record_from_mapping(row)
            return ClaimedDeletionOperation(
                operation_id=operation_id,
                tenant_id=tenant_id,
                attempt=record.attempt,
                target_kind=record.target_kind,
                target_id=record.target_id,
                manifest=record.manifest,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
                session_context=record.session_context,
                created_by_user_id=record.created_by_user_id,
                workspace_id=record.workspace_id,
            )

    async def _progress_db(
        self,
        operation_id: str,
        *,
        stage: DeletionStage,
        completed_items: int,
        total_items: int,
        fence_attempt: int | None,
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            conditions = [
                deletion_operations.c.operation_id == operation_id,
                deletion_operations.c.status == DeletionOperationStatus.RUNNING.value,
            ]
            if fence_attempt is not None:
                conditions.extend(
                    (
                        deletion_operations.c.claimed_by == self._worker_id,
                        deletion_operations.c.attempt == fence_attempt,
                    )
                )
            now = time.time()
            row = (
                await session.execute(
                    update(deletion_operations)
                    .where(*conditions)
                    .values(
                        stage=stage.value,
                        completed_items=func.greatest(
                            deletion_operations.c.completed_items,
                            completed_items,
                        ),
                        total_items=func.greatest(
                            deletion_operations.c.total_items,
                            total_items,
                            completed_items,
                        ),
                        updated_at=now,
                    )
                    .returning(
                        deletion_operations.c.tenant_id,
                        deletion_operations.c.manifest,
                        deletion_operations.c.target_kind,
                        deletion_operations.c.target_id,
                    )
                )
            ).first()
            if row is None:
                return False
            ids = [
                DeletionManifestItem.from_payload(item).asset_id
                for item in (row.manifest or [])
            ]
            if ids:
                await session.execute(
                    update(asset_records)
                    .where(
                        asset_records.c.tenant_id == row.tenant_id,
                        asset_records.c.id.in_(ids),
                        asset_records.c.deletion_operation_id == operation_id,
                    )
                    .values(
                        lifecycle_status="deleting",
                        deletion_stage=stage.value,
                        deletion_error=None,
                    )
                )
            if row.target_kind in _SESSION_DELETION_TARGET_VALUES:
                session_table = (
                    agent_sessions
                    if row.target_kind == DeletionTargetKind.AGENT_SESSION.value
                    else knowledge_sessions
                )
                await session.execute(
                    update(session_table)
                    .where(
                        session_table.c.tenant_id == row.tenant_id,
                        session_table.c.id == row.target_id,
                        session_table.c.deletion_operation_id == operation_id,
                    )
                    .values(
                        lifecycle_status="deleting",
                        deletion_stage=stage.value,
                        deletion_error=None,
                    )
                )
            await self._append_event_db(
                session,
                operation_id,
                row.tenant_id,
                "inqtrix.deletion.progress",
                {
                    "status": "running",
                    "stage": stage.value,
                    "completed_items": completed_items,
                    "total_items": max(total_items, completed_items),
                },
                now=now,
            )
            return True

    async def _is_attempt_current_db(
        self, operation_id: str, *, fence_attempt: int
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            current = (
                await session.execute(
                    select(deletion_operations.c.operation_id).where(
                        deletion_operations.c.operation_id == operation_id,
                        deletion_operations.c.status
                        == DeletionOperationStatus.RUNNING.value,
                        deletion_operations.c.claimed_by == self._worker_id,
                        deletion_operations.c.attempt == fence_attempt,
                    )
                )
            ).scalar_one_or_none()
            return current is not None

    async def _checkpoint_source_cleanup_db(
        self,
        operation_id: str,
        *,
        asset_id: str,
        plan: dict[str, Any],
        fence_attempt: int | None,
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            conditions = [
                deletion_operations.c.operation_id == operation_id,
                deletion_operations.c.status == DeletionOperationStatus.RUNNING.value,
            ]
            if fence_attempt is not None:
                conditions.extend(
                    (
                        deletion_operations.c.claimed_by == self._worker_id,
                        deletion_operations.c.attempt == fence_attempt,
                    )
                )
            row = (
                await session.execute(
                    select(deletion_operations.c.manifest)
                    .where(*conditions)
                    .with_for_update()
                )
            ).first()
            if row is None:
                return False
            matched = False
            manifest: list[dict[str, Any]] = []
            for payload in row.manifest or []:
                item = DeletionManifestItem.from_payload(payload)
                if item.asset_id == asset_id:
                    item = replace(
                        item,
                        source_cleanup_plan=dict(plan),
                    )
                    matched = True
                manifest.append(item.to_payload())
            if not matched:
                return False
            updated = await session.execute(
                update(deletion_operations)
                .where(*conditions)
                .values(manifest=manifest, updated_at=time.time())
            )
            return bool(updated.rowcount)

    async def _source_deletion_permit_db(
        self,
        operation_id: str,
        *,
        scope: SourceScope,
        fence_attempt: int | None,
    ):
        async with self._session(scope.tenant_id) as session:
            conditions = [
                deletion_operations.c.operation_id == operation_id,
                deletion_operations.c.tenant_id == scope.tenant_id,
                deletion_operations.c.status == DeletionOperationStatus.RUNNING.value,
            ]
            if fence_attempt is not None:
                conditions.extend(
                    (
                        deletion_operations.c.claimed_by == self._worker_id,
                        deletion_operations.c.attempt == fence_attempt,
                    )
                )
            current = (
                await session.execute(
                    select(deletion_operations.c.operation_id).where(*conditions)
                )
            ).scalar_one_or_none()
            if current is None:
                return None
            return await self._source_authority.get_deletion_permit_in_session(
                session,
                scope,
                operation_id=operation_id,
            )

    async def _terminal_db(
        self,
        operation_id: str,
        *,
        status: DeletionOperationStatus,
        stage: DeletionStage,
        error: dict[str, str] | None,
        fence_attempt: int | None,
    ) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            conditions = [
                deletion_operations.c.operation_id == operation_id,
                deletion_operations.c.status.in_(
                    (
                        DeletionOperationStatus.QUEUED.value,
                        DeletionOperationStatus.RUNNING.value,
                    )
                ),
            ]
            if fence_attempt is not None:
                conditions.extend(
                    (
                        deletion_operations.c.claimed_by == self._worker_id,
                        deletion_operations.c.attempt == fence_attempt,
                    )
                )
            if status == DeletionOperationStatus.DELETED:
                target = (
                    await session.execute(
                        select(
                            deletion_operations.c.tenant_id,
                            deletion_operations.c.created_by_user_id,
                            deletion_operations.c.workspace_id,
                            deletion_operations.c.target_kind,
                            deletion_operations.c.target_id,
                        ).where(*conditions)
                    )
                ).first()
                if target is None:
                    return False
                if target.target_kind == DeletionTargetKind.GROUP.value:
                    # Group removal and receipt completion share one transaction.
                    # Take the same lifecycle lock as create/move/finalise before
                    # locking the receipt row, preserving the global lock order.
                    await lock_group_lifecycle(
                        session,
                        tenant_id=target.tenant_id,
                        created_by_user_id=target.created_by_user_id,
                        workspace_id=target.workspace_id,
                        group_id=target.target_id,
                    )
            now = time.time()
            values: dict[str, Any] = {
                "status": status.value,
                "stage": stage.value,
                "error": error,
                "finished_at": now,
                "updated_at": now,
                "claimed_by": None,
            }
            if status == DeletionOperationStatus.DELETED:
                values["completed_items"] = deletion_operations.c.total_items
            row = (
                await session.execute(
                    update(deletion_operations)
                    .where(*conditions)
                    .values(**values)
                    .returning(
                        deletion_operations.c.tenant_id,
                        deletion_operations.c.manifest,
                        deletion_operations.c.created_by_user_id,
                        deletion_operations.c.workspace_id,
                        deletion_operations.c.target_kind,
                        deletion_operations.c.target_id,
                    )
                )
            ).first()
            if row is None:
                return False
            # Dienststart-Index terminal row, atomic with the terminal
            # write (fenced — zombies never reach this line). The final
            # DeletionStage is the "did PG/Qdrant/S3 all clean up"
            # answer the trail exists for.
            await append_audit_row(
                session,
                tenant_id=row.tenant_id,
                actor_user_id=row.created_by_user_id,
                action=(
                    "asset.delete_completed"
                    if status == DeletionOperationStatus.DELETED
                    else "asset.delete_failed"
                ),
                resource_type=str(row.target_kind),
                resource_id=str(row.target_id),
                detail={
                    "stage": stage.value,
                    **(
                        {"error_type": str(error.get("type") or "")}
                        if error
                        else {}
                    ),
                },
                outcome=(
                    "success"
                    if status == DeletionOperationStatus.DELETED
                    else "failure"
                ),
                correlation={"run_id": operation_id},
                workspace_id=_workspace_uuid(row.workspace_id),
            )
            if status == DeletionOperationStatus.DELETED:
                scopes = [
                    SourceScope(
                        tenant_id=row.tenant_id,
                        source_id=DeletionManifestItem.from_payload(item).source_id,
                        owner_user_id=row.created_by_user_id,
                        workspace_id=row.workspace_id,
                    )
                    for item in (row.manifest or [])
                ]
                target_source = target_source_id(
                    DeletionTargetKind(row.target_kind), row.target_id
                )
                if target_source is not None:
                    scopes.append(
                        SourceScope(
                            tenant_id=row.tenant_id,
                            source_id=target_source,
                            owner_user_id=row.created_by_user_id,
                            workspace_id=row.workspace_id,
                        )
                    )
                for scope in scopes:
                    permit = (
                        await self._source_authority.get_deletion_permit_in_session(
                            session,
                            scope,
                            operation_id=operation_id,
                        )
                    )
                    await self._source_authority.complete_delete_in_session(
                        session, permit
                    )
                if row.target_kind in _SESSION_DELETION_TARGET_VALUES:
                    session_table = (
                        agent_sessions
                        if row.target_kind
                        == DeletionTargetKind.AGENT_SESSION.value
                        else knowledge_sessions
                    )
                    deleted_session = await session.execute(
                        delete(session_table).where(
                            session_table.c.tenant_id == row.tenant_id,
                            session_table.c.id == row.target_id,
                            session_table.c.created_by_user_id.is_not_distinct_from(
                                row.created_by_user_id
                            ),
                            session_table.c.workspace_id.is_not_distinct_from(
                                row.workspace_id
                            ),
                            session_table.c.deletion_operation_id == operation_id,
                        )
                    )
                    if deleted_session.rowcount != 1:
                        raise RuntimeError(
                            "session tombstone disappeared before deletion completed"
                        )
                if row.target_kind == DeletionTargetKind.GROUP.value:
                    deleted_group = await session.execute(
                        delete(asset_groups).where(
                            asset_groups.c.tenant_id == row.tenant_id,
                            asset_groups.c.id == row.target_id,
                            asset_groups.c.created_by_user_id.is_not_distinct_from(
                                row.created_by_user_id
                            ),
                            asset_groups.c.workspace_id.is_not_distinct_from(
                                row.workspace_id
                            ),
                        )
                    )
                    if deleted_group.rowcount != 1:
                        raise RuntimeError(
                            "group tombstone disappeared before deletion completed"
                        )
                    grouped_children = await session.scalar(
                        select(func.count())
                        .select_from(asset_records)
                        .where(
                            asset_records.c.tenant_id == row.tenant_id,
                            asset_records.c.group_id == row.target_id,
                            asset_records.c.created_by_user_id.is_not_distinct_from(
                                row.created_by_user_id
                            ),
                            asset_records.c.workspace_id.is_not_distinct_from(
                                row.workspace_id
                            ),
                        )
                    )
                    if int(grouped_children or 0) != 0:
                        raise RuntimeError(
                            "group children remained attached after deletion"
                        )
            if status == DeletionOperationStatus.DELETE_FAILED:
                ids = [
                    DeletionManifestItem.from_payload(item).asset_id
                    for item in (row.manifest or [])
                ]
                if ids:
                    await session.execute(
                        update(asset_records)
                        .where(
                            asset_records.c.tenant_id == row.tenant_id,
                            asset_records.c.id.in_(ids),
                            asset_records.c.deletion_operation_id == operation_id,
                        )
                        .values(
                            lifecycle_status="delete_failed",
                            deletion_operation_id=operation_id,
                            deletion_stage=stage.value,
                            deletion_error=(error or {}).get("message"),
                        )
                    )
                if row.target_kind in _SESSION_DELETION_TARGET_VALUES:
                    session_table = (
                        agent_sessions
                        if row.target_kind == DeletionTargetKind.AGENT_SESSION.value
                        else knowledge_sessions
                    )
                    await session.execute(
                        update(session_table)
                        .where(
                            session_table.c.tenant_id == row.tenant_id,
                            session_table.c.id == row.target_id,
                            session_table.c.deletion_operation_id == operation_id,
                        )
                        .values(
                            lifecycle_status="delete_failed",
                            deletion_stage=stage.value,
                            deletion_error=(error or {}).get("message"),
                        )
                    )
            await self._append_event_db(
                session,
                operation_id,
                row.tenant_id,
                (
                    "inqtrix.deletion.deleted"
                    if status == DeletionOperationStatus.DELETED
                    else "inqtrix.deletion.failed"
                ),
                {"status": status.value, "stage": stage.value, "error": error},
                now=now,
            )
            return True

    async def _stale_running_db(self, grace_seconds: float) -> list[str]:
        """Running operations without recent progress (no-queue orphans).

        ``updated_at`` moves on every progress write, so a live but quiet
        operation is additionally protected by the ``_local`` exclusion
        in ``_fail_stalled`` — the age here only shields the claim
        window.
        """

        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(deletion_operations.c.operation_id).where(
                            # Belt and braces alongside RLS: this sweep
                            # must never reach beyond its own tenant even
                            # on a bypassing connection role.
                            deletion_operations.c.tenant_id == DEFAULT_TENANT,
                            deletion_operations.c.status
                            == DeletionOperationStatus.RUNNING.value,
                            deletion_operations.c.updated_at
                            < time.time() - grace_seconds,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    async def _stale_queued_db(
        self, older_than_seconds: float
    ) -> list[tuple[str, str]]:
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                await session.execute(
                    select(
                        deletion_operations.c.operation_id,
                        deletion_operations.c.tenant_id,
                    ).where(
                        deletion_operations.c.status
                        == DeletionOperationStatus.QUEUED.value,
                        deletion_operations.c.updated_at
                        < time.time() - older_than_seconds,
                    )
                )
            ).all()
            return [(row.operation_id, row.tenant_id) for row in rows]

    async def _append_event_db(
        self,
        session: "AsyncSession",
        operation_id: str,
        tenant_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        now: float,
    ) -> None:
        operation = (
            await session.execute(
                update(deletion_operations)
                .where(deletion_operations.c.operation_id == operation_id)
                .values(event_seq=deletion_operations.c.event_seq + 1)
                .returning(
                    deletion_operations.c.event_seq,
                    deletion_operations.c.created_by_user_id,
                )
            )
        ).one()
        await session.execute(
            insert(deletion_operation_events).values(
                operation_id=operation_id,
                sequence=operation.event_seq,
                tenant_id=tenant_id,
                type=event_type,
                created_at=now,
                data=data,
            )
        )
        # Other tabs discover this receipt through the existing user stream.
        # Once discovered, the receipt poller owns intermediate progress; only
        # lifecycle changes need a broad refresh. Commit the signal with the
        # receipt so a rollback cannot announce a change that never happened.
        if (
            event_type in _INVALIDATING_DELETION_EVENTS
            and operation.created_by_user_id is not None
        ):
            await append_user_invalidation(
                session,
                tenant_id=tenant_id,
                target_user_id=operation.created_by_user_id,
                scope="deletion_operations",
                resource_type="deletion_operation",
                resource_id=operation_id,
            )

    async def _cleanup_db(self, session: "AsyncSession", *, now: float) -> None:
        cutoff = now - self._completed_ttl_seconds
        await session.execute(
            delete(deletion_operations).where(
                deletion_operations.c.status == DeletionOperationStatus.DELETED.value,
                deletion_operations.c.finished_at.is_not(None),
                deletion_operations.c.finished_at < cutoff,
            )
        )


def _record_from_mapping(row: Any) -> DeletionOperationRecord:
    return DeletionOperationRecord(
        operation_id=row["operation_id"],
        target_kind=DeletionTargetKind(row["target_kind"]),
        target_id=row["target_id"],
        manifest=tuple(
            DeletionManifestItem.from_payload(item) for item in (row["manifest"] or [])
        ),
        tenant_id=row["tenant_id"],
        created_by_user_id=row["created_by_user_id"],
        workspace_id=row["workspace_id"],
        vector_index_context=VectorIndexDeletionContext.from_payload(
            dict(row["context"]) if row.get("context") else None
        ),
        knowledge_context=KnowledgeDeletionContext.from_payload(
            dict(row["context"]) if row.get("context") else None
        ),
        session_context=SessionDeletionContext.from_payload(
            dict(row["context"]) if row.get("context") else None
        ),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        status=DeletionOperationStatus(row["status"]),
        stage=DeletionStage(row["stage"]),
        completed_items=int(row["completed_items"]),
        total_items=int(row["total_items"]),
        attempt=int(row["attempt"]),
        claimed_by=row["claimed_by"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=dict(row["error"]) if row["error"] else None,
    )
