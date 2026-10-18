"""Postgres source of truth for durable bound-file upload operations."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.storage.migration_contract import (
    assert_schema_head,
)
from inqtrix.project.asset_lifecycle import lock_asset_lifecycle
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetRecord,
)
from inqtrix.runs.durable_store import DurableJobStoreBase
from inqtrix.runs.upload_operations import (
    UploadAttempt,
    UploadBinding,
    UploadOperationConflict,
    UploadOperationNotFound,
    UploadOperationRecord,
    UploadOperationStatus,
    UploadStage,
    _same_upload_binding,
    _same_uploaded_file,
    build_upload_summary,
    file_record_from_payload,
    file_record_to_payload,
    new_upload_operation_id,
)
from inqtrix.source_authority import (
    PostgresSourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)
from inqtrix.storage.asset_records_orm import asset_records
from inqtrix.storage.uploads_orm import (
    upload_operation_events,
    upload_operation_outbox,
    upload_operations,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from inqtrix.runs.upload_queue import ValkeyUploadQueue

log = logging.getLogger("inqtrix")
_SOURCE_AUTHORITY = PostgresSourceLifecycleAuthority()


@dataclass(frozen=True)
class ClaimedUploadOperation:
    operation_id: str
    tenant_id: str
    attempt: int
    record: UploadOperationRecord


def _record_from_mapping(row: Any) -> UploadOperationRecord:
    return UploadOperationRecord(
        operation_id=str(row["operation_id"]),
        asset_id=str(row["asset_id"]),
        file=file_record_from_payload(dict(row["file_manifest"] or {})),
        binding=UploadBinding.from_payload(dict(row["binding"] or {})),
        tenant_id=str(row["tenant_id"]),
        created_by_user_id=row["created_by_user_id"],
        workspace_id=row["workspace_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        status=UploadOperationStatus(str(row["status"])),
        stage=UploadStage(str(row["stage"])),
        attempt=int(row["attempt"]),
        claimed_by=row["claimed_by"],
        started_at=(float(row["started_at"]) if row["started_at"] else None),
        finished_at=(float(row["finished_at"]) if row["finished_at"] else None),
        error=(dict(row["error"]) if row["error"] else None),
    )


class PostgresUploadOperationStore(DurableJobStoreBase):
    """Fenced operation ledger with an atomic asset lifecycle projection."""

    _loop_thread_name = "inqtrix-upload-db"
    _dispatch_thread_prefix = "inqtrix-upload"
    _job_kind = "Durable upload operation"

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        queue: "ValkeyUploadQueue | None",
        worker_id: str,
        reconcile_delay_seconds: float = 120.0,
        recover_orphans: bool | None = False,
    ) -> None:
        super().__init__(
            engine=engine,
            app_role=app_role,
            worker_id=worker_id,
            queue=queue,
            recover_orphans=recover_orphans,
        )
        self._reconcile_delay_seconds = max(1.0, reconcile_delay_seconds)

    def start_or_resume(
        self,
        *,
        asset: AssetRecord,
        proposed_file,
        binding: UploadBinding,
    ) -> UploadAttempt:
        return self._call(
            self._start_or_resume_db(
                asset=asset, proposed_file=proposed_file, binding=binding
            )
        )

    def get_record(
        self, operation_id: str, *, tenant_id: str = "default"
    ) -> UploadOperationRecord:
        return self._call(
            self._get_record_by_tenant_db(operation_id, tenant_id=tenant_id)
        )

    def get(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        return build_upload_summary(
            self._call(
                self._get_record_db(
                    operation_id,
                    tenant_id=tenant_id,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
            )
        )

    def list_operations(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self._call(
            self._list_db(
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        )
        return [build_upload_summary(row) for row in rows]

    def checkpoint(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        stage: UploadStage,
        fence_attempt: int,
    ) -> bool:
        return self._call(
            self._checkpoint_db(
                operation_id,
                tenant_id=tenant_id,
                stage=stage,
                fence_attempt=fence_attempt,
            )
        )

    def complete(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        return self._call(
            self._complete_db(
                operation_id,
                tenant_id=tenant_id,
                fence_attempt=fence_attempt,
            )
        )

    def fail(
        self,
        operation_id: str,
        message: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
        error_type: str = "server_error",
        awaiting_bytes: bool = False,
    ) -> bool:
        return self._call(
            self._fail_db(
                operation_id,
                tenant_id=tenant_id,
                message=sanitize_error(message),
                error_type=error_type,
                fence_attempt=fence_attempt,
                awaiting_bytes=awaiting_bytes,
            )
        )

    def queue_retry(
        self,
        operation_id: str,
        message: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
        error_type: str = "dependency_error",
    ) -> bool:
        landed = self._call(
            self._queue_retry_db(
                operation_id,
                tenant_id=tenant_id,
                message=sanitize_error(message),
                error_type=error_type,
                fence_attempt=fence_attempt,
            )
        )
        if landed and self._queue is not None:
            try:
                record = self.get_record(operation_id, tenant_id=tenant_id)
                self._queue.enqueue(
                    operation_id=operation_id, tenant_id=record.tenant_id
                )
            except Exception as exc:
                log.warning(
                    "Upload dispatch %s failed; the outbox reconciler will retry "
                    "(error_type=%s).",
                    operation_id,
                    type(exc).__name__,
                )
        return landed

    def queue_continuation(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        """Persist the HTTP-to-worker handoff for canonical parsing."""

        landed = self._call(
            self._queue_continuation_db(
                operation_id,
                tenant_id=tenant_id,
                fence_attempt=fence_attempt,
            )
        )
        if landed and self._queue is not None:
            try:
                self._queue.enqueue(operation_id=operation_id, tenant_id=tenant_id)
            except Exception as exc:
                log.warning(
                    "Upload continuation %s could not be dispatched; the outbox "
                    "reconciler will retry (error_type=%s).",
                    operation_id,
                    type(exc).__name__,
                )
        return landed

    def queue_missing_preparations(self, *, limit: int) -> list[tuple[str, str]]:
        """Return ready operations whose asset never got a preparation to parsing.

        The caller dispatches every returned operation; each outbox row
        comes due only after the reconcile delay, the safety net for a lost
        dispatch. ``finished_at`` stays set: the executor reads it as
        "prepared again".
        """

        return self._call(self._queue_missing_preparations_db(limit))

    def retry(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        record = self._call(
            self._retry_db(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        )
        if self._queue is not None:
            try:
                self._queue.enqueue(operation_id=operation_id, tenant_id=tenant_id)
            except Exception as exc:
                log.warning(
                    "Upload retry %s could not be dispatched; the outbox remains due "
                    "(error_type=%s).",
                    operation_id,
                    type(exc).__name__,
                )
        return build_upload_summary(record)

    def is_attempt_current(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        return self._call(
            self._is_current_db(
                operation_id,
                tenant_id=tenant_id,
                fence_attempt=fence_attempt,
            )
        )

    def heartbeat(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        return self._call(
            self._heartbeat_db(
                operation_id,
                tenant_id=tenant_id,
                fence_attempt=fence_attempt,
            )
        )

    def claim_for_execution(
        self, operation_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedUploadOperation | None:
        return self._call(
            self._claim_db(operation_id, tenant_id, allow_takeover=allow_takeover)
        )

    def stale_dispatches(self, *, older_than_seconds: float) -> list[tuple[str, str]]:
        return self._call(self._stale_dispatches_db(older_than_seconds))

    def cancel_requested_operations(self, watched: dict[str, str]) -> set[str]:
        del watched
        return set()

    def _make_handle(self, entity_id: str, cancel_event):  # pragma: no cover
        del entity_id, cancel_event
        raise RuntimeError("upload work is composed by UploadOperationService")

    def _auto_complete(self, entity_id: str) -> None:  # pragma: no cover
        del entity_id

    async def _start_or_resume_db(
        self, *, asset: AssetRecord, proposed_file, binding: UploadBinding
    ) -> UploadAttempt:
        if (
            proposed_file.tenant_id != asset.tenant_id
            or proposed_file.owner_user_id != asset.created_by_user_id
            or proposed_file.workspace_id != asset.workspace_id
        ):
            raise UploadOperationConflict("file scope is not asset-derived")
        now = time.time()
        async with self._session(asset.tenant_id) as session:
            await lock_asset_lifecycle(
                session,
                tenant_id=asset.tenant_id,
                created_by_user_id=asset.created_by_user_id,
                workspace_id=asset.workspace_id,
                asset_id=asset.id,
            )
            try:
                await _SOURCE_AUTHORITY.active_write(
                    session,
                    SourceScope(
                        tenant_id=asset.tenant_id,
                        source_id=f"asset:{asset.id}",
                        owner_user_id=asset.created_by_user_id,
                        workspace_id=asset.workspace_id,
                    ),
                    create_if_missing=False,
                )
            except SourceLifecycleConflict as exc:
                raise AssetDeletionInProgress(asset.id) from exc
            # Only the lifecycle decides here; the row lock does not need the
            # asset's text bodies.
            lifecycle_status = (
                await session.execute(
                    select(asset_records.c.lifecycle_status)
                    .where(
                        asset_records.c.tenant_id == asset.tenant_id,
                        asset_records.c.id == asset.id,
                        asset_records.c.created_by_user_id.is_not_distinct_from(
                            asset.created_by_user_id
                        ),
                        asset_records.c.workspace_id.is_not_distinct_from(
                            asset.workspace_id
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if lifecycle_status is None:
                raise AssetNotFound(asset.id)
            if lifecycle_status != "active":
                raise AssetDeletionInProgress(asset.id)
            row = (
                (
                    await session.execute(
                        select(upload_operations)
                        .where(
                            upload_operations.c.tenant_id == asset.tenant_id,
                            upload_operations.c.asset_id == asset.id,
                            upload_operations.c.created_by_user_id.is_not_distinct_from(
                                asset.created_by_user_id
                            ),
                            upload_operations.c.workspace_id.is_not_distinct_from(
                                asset.workspace_id
                            ),
                        )
                        .order_by(upload_operations.c.created_at.desc())
                        .limit(1)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            already_ready = False
            if row is not None:
                record = _record_from_mapping(row)
                if not _same_uploaded_file(record.file, proposed_file):
                    raise UploadOperationConflict(
                        "asset id already belongs to a different upload"
                    )
                if not _same_upload_binding(record.binding, binding):
                    raise UploadOperationConflict(
                        "upload binding changed after the operation was prepared"
                    )
                if record.status == UploadOperationStatus.READY:
                    return UploadAttempt(
                        record=record,
                        attempt=record.attempt,
                        already_ready=True,
                    )
                row = (
                    (
                        await session.execute(
                            update(upload_operations)
                            .where(
                                upload_operations.c.operation_id == record.operation_id,
                                upload_operations.c.tenant_id == asset.tenant_id,
                            )
                            .values(
                                status=UploadOperationStatus.RUNNING.value,
                                attempt=upload_operations.c.attempt + 1,
                                claimed_by=self._worker_id,
                                error=None,
                                finished_at=None,
                                started_at=func.coalesce(
                                    upload_operations.c.started_at, now
                                ),
                                updated_at=now,
                            )
                            .returning(upload_operations)
                        )
                    )
                    .mappings()
                    .one()
                )
                record = _record_from_mapping(row)
                event_type = "inqtrix.upload.retried"
            else:
                operation_id = new_upload_operation_id()
                row = (
                    (
                        await session.execute(
                            insert(upload_operations)
                            .values(
                                operation_id=operation_id,
                                tenant_id=asset.tenant_id,
                                asset_id=asset.id,
                                file_id=proposed_file.id,
                                file_manifest=file_record_to_payload(proposed_file),
                                binding=binding.to_payload(),
                                status=UploadOperationStatus.RUNNING.value,
                                stage=UploadStage.PREPARED.value,
                                workspace_id=asset.workspace_id,
                                created_by_user_id=asset.created_by_user_id,
                                claimed_by=self._worker_id,
                                attempt=1,
                                created_at=now,
                                updated_at=now,
                                started_at=now,
                            )
                            .returning(upload_operations)
                        )
                    )
                    .mappings()
                    .one()
                )
                record = _record_from_mapping(row)
                event_type = "inqtrix.upload.prepared"
            await self._upsert_outbox(
                session,
                record.operation_id,
                record.tenant_id,
                available_at=now + self._reconcile_delay_seconds,
            )
            updated = await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == asset.tenant_id,
                    asset_records.c.id == asset.id,
                    asset_records.c.lifecycle_status == "active",
                    asset_records.c.deletion_operation_id.is_(None),
                )
                .values(
                    upload_status="uploading",
                    upload_error=None,
                    upload_operation_id=record.operation_id,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                raise AssetDeletionInProgress(asset.id)
            await self._append_event(
                session,
                record,
                event_type,
                {"attempt": record.attempt, "stage": record.stage.value},
                now=now,
            )
            return UploadAttempt(
                record=record,
                attempt=record.attempt,
                already_ready=already_ready,
            )

    async def _checkpoint_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        stage: UploadStage,
        fence_attempt: int,
    ) -> bool:
        now = time.time()
        async with self._session(tenant_id) as session:
            current = (
                (
                    await session.execute(
                        select(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is None:
                return False
            record = _record_from_mapping(current)
            order = list(UploadStage)
            if order.index(stage) < order.index(record.stage):
                return True
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .values(stage=stage.value, updated_at=now)
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            if stage == UploadStage.PARSING:
                status = "parsing"
            elif stage in {
                UploadStage.PARSE_FINISHED,
                UploadStage.QUOTA_BOOKED,
            }:
                status = "finalizing"
            else:
                status = "uploading"
            await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == row["tenant_id"],
                    asset_records.c.id == row["asset_id"],
                    asset_records.c.upload_operation_id == operation_id,
                    asset_records.c.lifecycle_status == "active",
                )
                .values(upload_status=status, upload_error=None, updated_at=now)
            )
            await self._upsert_outbox(
                session,
                operation_id,
                str(row["tenant_id"]),
                available_at=now + self._reconcile_delay_seconds,
            )
            await self._append_event(
                session,
                _record_from_mapping(row),
                "inqtrix.upload.progress",
                {"status": "running", "stage": stage.value},
                now=now,
            )
            return True

    async def _complete_db(
        self, operation_id: str, *, tenant_id: str, fence_attempt: int
    ) -> bool:
        now = time.time()
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.stage == UploadStage.QUOTA_BOOKED.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .values(
                            status=UploadOperationStatus.READY.value,
                            stage=UploadStage.READY.value,
                            claimed_by=None,
                            error=None,
                            updated_at=now,
                            finished_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            asset_result = await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == row["tenant_id"],
                    asset_records.c.id == row["asset_id"],
                    asset_records.c.upload_operation_id == operation_id,
                    asset_records.c.lifecycle_status == "active",
                    asset_records.c.server_file_id == row["file_id"],
                )
                .values(
                    upload_status="ready",
                    upload_error=None,
                    updated_at=now,
                )
            )
            if asset_result.rowcount != 1:
                raise UploadOperationConflict("bound asset disappeared before ready")
            await session.execute(
                upload_operation_outbox.delete().where(
                    upload_operation_outbox.c.operation_id == operation_id
                )
            )
            await self._append_event(
                session,
                _record_from_mapping(row),
                "inqtrix.upload.ready",
                {"status": "ready", "stage": "ready"},
                now=now,
            )
            return True

    async def _fail_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        message: str,
        error_type: str,
        fence_attempt: int,
        awaiting_bytes: bool,
    ) -> bool:
        now = time.time()
        status = (
            UploadOperationStatus.AWAITING_BYTES
            if awaiting_bytes
            else UploadOperationStatus.UPLOAD_FAILED
        )
        error = {"message": message, "type": error_type}
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .values(
                            status=status.value,
                            claimed_by=None,
                            error=error,
                            updated_at=now,
                            finished_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == row["tenant_id"],
                    asset_records.c.id == row["asset_id"],
                    asset_records.c.upload_operation_id == operation_id,
                    asset_records.c.lifecycle_status == "active",
                )
                .values(upload_status="failed", upload_error=message, updated_at=now)
            )
            await session.execute(
                upload_operation_outbox.delete().where(
                    upload_operation_outbox.c.operation_id == operation_id
                )
            )
            await self._append_event(
                session,
                _record_from_mapping(row),
                (
                    "inqtrix.upload.awaiting_bytes"
                    if awaiting_bytes
                    else "inqtrix.upload.failed"
                ),
                {"status": status.value, "stage": row["stage"], "error": error},
                now=now,
            )
            return True

    async def _queue_retry_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        message: str,
        error_type: str,
        fence_attempt: int,
    ) -> bool:
        now = time.time()
        error = {"message": message, "type": error_type}
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .values(
                            status=UploadOperationStatus.QUEUED.value,
                            claimed_by=None,
                            error=error,
                            updated_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == row["tenant_id"],
                    asset_records.c.id == row["asset_id"],
                    asset_records.c.upload_operation_id == operation_id,
                    asset_records.c.lifecycle_status == "active",
                )
                .values(upload_status="retrying", upload_error=message, updated_at=now)
            )
            await self._upsert_outbox(
                session, operation_id, str(row["tenant_id"]), available_at=now
            )
            await self._append_event(
                session,
                _record_from_mapping(row),
                "inqtrix.upload.retry_scheduled",
                {"status": "queued", "stage": row["stage"], "error": error},
                now=now,
            )
            return True

    async def _queue_continuation_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        fence_attempt: int,
    ) -> bool:
        now = time.time()
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.tenant_id == tenant_id,
                            upload_operations.c.status
                            == UploadOperationStatus.RUNNING.value,
                            upload_operations.c.stage == UploadStage.PARSING.value,
                            upload_operations.c.claimed_by == self._worker_id,
                            upload_operations.c.attempt == fence_attempt,
                        )
                        .values(
                            status=UploadOperationStatus.QUEUED.value,
                            claimed_by=None,
                            error=None,
                            updated_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            await session.execute(
                update(asset_records)
                .where(
                    asset_records.c.tenant_id == row["tenant_id"],
                    asset_records.c.id == row["asset_id"],
                    asset_records.c.upload_operation_id == operation_id,
                    asset_records.c.lifecycle_status == "active",
                )
                .values(upload_status="parsing", upload_error=None, updated_at=now)
            )
            await self._upsert_outbox(
                session,
                operation_id,
                str(row["tenant_id"]),
                available_at=now,
            )
            await self._append_event(
                session,
                _record_from_mapping(row),
                "inqtrix.upload.continuation_queued",
                {"status": "queued", "stage": UploadStage.PARSING.value},
                now=now,
            )
            return True

    async def _queue_missing_preparations_db(
        self, limit: int
    ) -> list[tuple[str, str]]:
        now = time.time()
        async with self._session("default") as session:
            candidates = (
                await session.execute(
                    select(upload_operations.c.operation_id)
                    .join(
                        asset_records,
                        and_(
                            asset_records.c.tenant_id
                            == upload_operations.c.tenant_id,
                            asset_records.c.id == upload_operations.c.asset_id,
                            asset_records.c.upload_operation_id
                            == upload_operations.c.operation_id,
                        ),
                    )
                    .where(
                        upload_operations.c.status
                        == UploadOperationStatus.READY.value,
                        asset_records.c.lifecycle_status == "active",
                        asset_records.c.upload_status == "ready",
                        asset_records.c.prepared_parser_id.is_(None),
                        asset_records.c.parse_status != "error",
                    )
                    .with_for_update(of=upload_operations, skip_locked=True)
                    .limit(limit)
                )
            ).scalars().all()
            queued: list[tuple[str, str]] = []
            for operation_id in candidates:
                # Locked above for this transaction: still ready.
                row = (
                    (
                        await session.execute(
                            update(upload_operations)
                            .where(upload_operations.c.operation_id == operation_id)
                            .values(
                                status=UploadOperationStatus.QUEUED.value,
                                stage=UploadStage.PARSING.value,
                                claimed_by=None,
                                error=None,
                                updated_at=now,
                            )
                            .returning(upload_operations)
                        )
                    )
                    .mappings()
                    .one()
                )
                await session.execute(
                    update(asset_records)
                    .where(
                        asset_records.c.tenant_id == row["tenant_id"],
                        asset_records.c.id == row["asset_id"],
                        asset_records.c.upload_operation_id == operation_id,
                        asset_records.c.lifecycle_status == "active",
                    )
                    .values(upload_status="parsing", upload_error=None, updated_at=now)
                )
                await self._upsert_outbox(
                    session,
                    operation_id,
                    str(row["tenant_id"]),
                    available_at=now + self._reconcile_delay_seconds,
                )
                await self._append_event(
                    session,
                    _record_from_mapping(row),
                    "inqtrix.upload.preparation_queued",
                    {"status": "queued", "stage": UploadStage.PARSING.value},
                    now=now,
                )
                queued.append((str(operation_id), str(row["tenant_id"])))
            return queued

    async def _retry_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> UploadOperationRecord:
        now = time.time()
        async with self._session(tenant_id) as session:
            current = (
                (
                    await session.execute(
                        select(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.tenant_id == tenant_id,
                            upload_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            upload_operations.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is None:
                raise UploadOperationNotFound(operation_id)
            record = _record_from_mapping(current)
            if record.status == UploadOperationStatus.AWAITING_BYTES:
                raise UploadOperationConflict("upload bytes are required")
            if record.status != UploadOperationStatus.UPLOAD_FAILED:
                raise UploadOperationConflict(operation_id)
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(upload_operations.c.operation_id == operation_id)
                        .values(
                            status=UploadOperationStatus.QUEUED.value,
                            error=None,
                            finished_at=None,
                            updated_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .one()
            )
            await self._upsert_outbox(
                session, operation_id, tenant_id, available_at=now
            )
            return _record_from_mapping(row)

    async def _claim_db(
        self, operation_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedUploadOperation | None:
        allowed = [UploadOperationStatus.QUEUED.value]
        if allow_takeover:
            allowed.append(UploadOperationStatus.RUNNING.value)
        now = time.time()
        async with self._session(tenant_id) as session:
            # Cutover fence, FIRST statement of the claim transaction: a
            # worker whose image predates a completed migration must not
            # take a durable claim -- nor run this transaction's other
            # writes -- against a schema its code no longer matches. Rides
            # in the same transaction, so a mismatch rolls everything back
            # and the queue entry stays unacked for an upgraded worker.
            await assert_schema_head(session)
            row = (
                (
                    await session.execute(
                        update(upload_operations)
                        .where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.tenant_id == tenant_id,
                            upload_operations.c.status.in_(allowed),
                        )
                        .values(
                            status=UploadOperationStatus.RUNNING.value,
                            claimed_by=self._worker_id,
                            attempt=upload_operations.c.attempt + 1,
                            started_at=func.coalesce(
                                upload_operations.c.started_at, now
                            ),
                            updated_at=now,
                        )
                        .returning(upload_operations)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            record = _record_from_mapping(row)
            await self._append_event(
                session,
                record,
                "inqtrix.upload.started",
                {"attempt": record.attempt, "stage": record.stage.value},
                now=now,
            )
            return ClaimedUploadOperation(
                operation_id=operation_id,
                tenant_id=tenant_id,
                attempt=record.attempt,
                record=record,
            )

    async def _is_current_db(
        self, operation_id: str, *, tenant_id: str, fence_attempt: int
    ) -> bool:
        async with self._session(tenant_id) as session:
            current = (
                await session.execute(
                    select(upload_operations.c.operation_id).where(
                        upload_operations.c.operation_id == operation_id,
                        upload_operations.c.status
                        == UploadOperationStatus.RUNNING.value,
                        upload_operations.c.claimed_by == self._worker_id,
                        upload_operations.c.attempt == fence_attempt,
                    )
                )
            ).scalar_one_or_none()
            return current is not None

    async def _heartbeat_db(
        self, operation_id: str, *, tenant_id: str, fence_attempt: int
    ) -> bool:
        async with self._session(tenant_id) as session:
            result = await session.execute(
                update(upload_operations)
                .where(
                    upload_operations.c.operation_id == operation_id,
                    upload_operations.c.tenant_id == tenant_id,
                    upload_operations.c.status == UploadOperationStatus.RUNNING.value,
                    upload_operations.c.claimed_by == self._worker_id,
                    upload_operations.c.attempt == fence_attempt,
                )
                .values(updated_at=time.time())
            )
            return result.rowcount == 1

    async def _stale_dispatches_db(
        self, older_than_seconds: float
    ) -> list[tuple[str, str]]:
        now = time.time()
        stale_cutoff = now - max(0.0, older_than_seconds)
        async with self._session("default") as session:
            rows = (
                await session.execute(
                    select(
                        upload_operations.c.operation_id,
                        upload_operations.c.tenant_id,
                    )
                    .join(
                        upload_operation_outbox,
                        upload_operation_outbox.c.operation_id
                        == upload_operations.c.operation_id,
                    )
                    .where(
                        upload_operation_outbox.c.available_at <= now,
                        or_(
                            upload_operations.c.status
                            == UploadOperationStatus.QUEUED.value,
                            and_(
                                upload_operations.c.status
                                == UploadOperationStatus.RUNNING.value,
                                upload_operations.c.updated_at <= stale_cutoff,
                            ),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
            if not rows:
                return []
            ids = [row.operation_id for row in rows]
            await session.execute(
                update(upload_operations)
                .where(upload_operations.c.operation_id.in_(ids))
                .values(
                    status=UploadOperationStatus.QUEUED.value,
                    claimed_by=None,
                    updated_at=now,
                )
            )
            await session.execute(
                update(upload_operation_outbox)
                .where(upload_operation_outbox.c.operation_id.in_(ids))
                .values(
                    dispatch_count=upload_operation_outbox.c.dispatch_count + 1,
                    last_dispatched_at=now,
                    available_at=now + self._reconcile_delay_seconds,
                )
            )
            return [(str(row.operation_id), str(row.tenant_id)) for row in rows]

    async def _get_record_by_tenant_db(
        self, operation_id: str, *, tenant_id: str
    ) -> UploadOperationRecord:
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        select(upload_operations).where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.tenant_id == tenant_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise UploadOperationNotFound(operation_id)
        return _record_from_mapping(row)

    async def _get_record_db(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> UploadOperationRecord:
        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        select(upload_operations).where(
                            upload_operations.c.operation_id == operation_id,
                            upload_operations.c.tenant_id == tenant_id,
                            upload_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            *(
                                (upload_operations.c.workspace_id == workspace_id,)
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
            raise UploadOperationNotFound(operation_id)
        return _record_from_mapping(row)

    async def _list_db(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
    ) -> list[UploadOperationRecord]:
        async with self._session(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(upload_operations)
                        .where(
                            upload_operations.c.tenant_id == tenant_id,
                            upload_operations.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            upload_operations.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                        .order_by(
                            upload_operations.c.created_at.desc(),
                            upload_operations.c.operation_id.desc(),
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_record_from_mapping(row) for row in rows]

    async def _upsert_outbox(
        self,
        session: "AsyncSession",
        operation_id: str,
        tenant_id: str,
        *,
        available_at: float,
    ) -> None:
        statement = pg_insert(upload_operation_outbox).values(
            operation_id=operation_id,
            tenant_id=tenant_id,
            available_at=available_at,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[upload_operation_outbox.c.operation_id],
                set_={"available_at": available_at},
            )
        )

    async def _append_event(
        self,
        session: "AsyncSession",
        record: UploadOperationRecord,
        event_type: str,
        data: dict[str, Any],
        *,
        now: float,
    ) -> None:
        sequence = (
            await session.execute(
                update(upload_operations)
                .where(upload_operations.c.operation_id == record.operation_id)
                .values(event_seq=upload_operations.c.event_seq + 1)
                .returning(upload_operations.c.event_seq)
            )
        ).scalar_one()
        await session.execute(
            insert(upload_operation_events).values(
                operation_id=record.operation_id,
                sequence=sequence,
                tenant_id=record.tenant_id,
                type=event_type,
                created_at=now,
                data=data,
            )
        )
