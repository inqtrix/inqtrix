"""Recoverable original-file upload orchestration over existing services."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.ports import FileRecord
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetUploadConflict,
)
from inqtrix.quota.models import QuotaDimension, QuotaSubject, file_stock_key
from inqtrix.runs.upload_operations import (
    UploadAttempt,
    UploadAttemptSuperseded,
    UploadBinding,
    UploadOperationConflict,
    UploadOperationNotFound,
    UploadOperationRecord,
    UploadOperationStatus,
    UploadStage,
    build_upload_summary,
)
from inqtrix.services.file_service import (
    FileRegistryConflict,
    FileService,
    FileTextExtractionError,
    SpooledUpload,
)
from inqtrix.storage.migration_contract import SchemaHeadMismatch
from inqtrix.sync_bridge import run_coro_sync
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.services.asset_records_service import AssetRecordsService
    from inqtrix.services.quota_service import QuotaService

log = logging.getLogger("inqtrix")


class UploadExecutionDeferred(RuntimeError):
    """The operation remains visible and queued for a bounded retry."""

    def __init__(self, operation: dict) -> None:
        super().__init__("upload operation was queued for retry")
        self.operation = operation


class UploadBytesRequired(RuntimeError):
    """Recovery reached a prepared operation whose request spool is gone."""


def _stage_before(left: UploadStage, right: UploadStage) -> bool:
    order = list(UploadStage)
    return order.index(left) < order.index(right)


class UploadOperationService:
    """Drive and reconcile one monotonic upload state machine.

    Object bytes and file metadata still belong exclusively to
    :class:`FileService`; this service only orders those existing capabilities
    against the asset aggregate and retry-safe quota receipt.
    """

    def __init__(
        self,
        *,
        operations,
        files: FileService,
        assets: "AssetRecordsService",
        quota: "QuotaService | None",
        max_attempts: int,
        heartbeat_seconds: float = 15.0,
        fault_hook: Callable[[str, UploadOperationRecord], None] | None = None,
        audit=None,
    ) -> None:
        self.operations = operations
        self._files = files
        self._assets = assets
        self._quota = quota
        self._max_attempts = max(1, max_attempts)
        self._heartbeat_seconds = max(0.01, heartbeat_seconds)
        self._fault_hook = fault_hook
        self._audit = audit

    def bind_quota_service(self, quota: "QuotaService | None") -> None:
        """Bind worker-side quota accounting after identity-free composition."""

        self._quota = quota

    async def _audit_uploaded(self, record: UploadOperationRecord) -> None:
        """file.uploaded index row — ONE chokepoint for sync AND worker-
        deferred uploads (both funnel through the READY commit above).
        Fail-safe with WARNING via AuditService; metadata only."""
        if self._audit is None:
            return
        from inqtrix.services.audit_service import AuditService

        workspace_uuid = None
        workspace_id = getattr(record.file, "workspace_id", None)
        if workspace_id:
            try:
                import uuid as _uuid

                workspace_uuid = _uuid.UUID(str(workspace_id))
            except ValueError:
                workspace_uuid = None
        await AuditService(self._audit).record_event(
            tenant_id=record.tenant_id,
            actor_user_id=record.file.owner_user_id,
            action="file.uploaded",
            resource_type="file",
            resource_id=str(record.file.id),
            detail={
                "size_bytes": str(int(record.file.size_bytes or 0)),
                "mime": str(getattr(record.file, "mime", "") or ""),
            },
            correlation={"run_id": record.operation_id},
            workspace_id=workspace_uuid,
        )

    def prepared_file_for_deletion(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        asset_id: str,
    ) -> FileRecord | None:
        """Return immutable file facts retained by an asset's upload ledger."""

        try:
            record = self.operations.get_record(operation_id, tenant_id=tenant_id)
        except UploadOperationNotFound:
            return None
        if record.asset_id != asset_id:
            raise UploadOperationConflict(
                "asset deletion referenced an unrelated upload operation"
            )
        return record.file

    def deletion_can_finalize(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        asset_id: str,
    ) -> bool:
        """Whether no current upload attempt can still publish file state."""

        try:
            record = self.operations.get_record(operation_id, tenant_id=tenant_id)
        except UploadOperationNotFound:
            return True
        if record.asset_id != asset_id:
            raise UploadOperationConflict(
                "asset deletion referenced an unrelated upload operation"
            )
        return record.status not in {
            UploadOperationStatus.RUNNING,
            UploadOperationStatus.QUEUED,
        }

    async def start_from_spool(
        self,
        *,
        asset_id: str,
        spooled: SpooledUpload,
        file_name: str,
        content_type: str,
        binding: UploadBinding,
        visible_to: UserContext | None,
    ) -> UploadAttempt:
        """Persist immutable operation facts before the first object put."""

        asset = await self._assets.get_asset(asset_id, visible_to=visible_to)
        if asset.server_file_id is not None:
            existing = await self._files.prepared_file_record(
                asset.server_file_id, tenant_id=asset.tenant_id
            )
            if existing is None:
                raise UploadOperationConflict(
                    "asset binding refers to a missing file registry row"
                )
            if (
                existing.sha256 != spooled.sha256
                or existing.size_bytes != spooled.size_bytes
                or existing.file_name != file_name
                or existing.content_type != (content_type or "application/octet-stream")
            ):
                raise UploadOperationConflict(
                    "asset id already belongs to a different original file"
                )
            proposed = existing
        else:
            proposed = self._files.prepare_file_record(
                spooled=spooled,
                file_name=file_name,
                content_type=content_type,
                tenant_id=asset.tenant_id,
                owner_user_id=asset.created_by_user_id,
                workspace_id=asset.workspace_id,
            )
        return self.operations.start_or_resume(
            asset=asset,
            proposed_file=proposed,
            binding=binding,
        )

    async def execute(
        self,
        attempt: UploadAttempt,
        *,
        visible_to: UserContext | None,
        spooled: SpooledUpload | None = None,
    ) -> tuple[FileRecord, object, dict]:
        """Converge all checkpoints, validating actual state at each boundary."""

        record = attempt.record
        if attempt.already_ready:
            asset = await self._assets.get_asset(record.asset_id, visible_to=visible_to)
            return record.file, asset, build_upload_summary(record)
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_attempt(
                record,
                attempt.attempt,
                heartbeat_stop,
            )
        )
        try:
            await self._execute_stages(
                record,
                fence_attempt=attempt.attempt,
                visible_to=visible_to,
                spooled=spooled,
            )
        except UploadAttemptSuperseded:
            raise
        except UploadExecutionDeferred:
            raise
        except UploadBytesRequired as exc:
            self._land_failure(
                record,
                attempt.attempt,
                str(exc),
                error_type="upload_bytes_required",
                awaiting_bytes=True,
            )
            raise
        except (
            AssetDeletionInProgress,
            AssetNotFound,
            AssetUploadConflict,
        ) as exc:
            try:
                await self._discard_if_unbound(record)
            except Exception as cleanup_exc:
                self._defer_dependency_failure(
                    record,
                    attempt.attempt,
                    cleanup_exc,
                    error_type="upload_cleanup_error",
                )
                raise  # pragma: no cover - helper always raises
            self._land_failure(
                record,
                attempt.attempt,
                sanitize_error(exc),
                error_type="upload_source_unavailable",
            )
            raise
        except (FileRegistryConflict, UploadOperationConflict) as exc:
            self._land_failure(
                record,
                attempt.attempt,
                sanitize_error(exc),
                error_type="upload_integrity_error",
            )
            raise
        except Exception as exc:
            self._defer_dependency_failure(
                record,
                attempt.attempt,
                exc,
                error_type="dependency_error",
            )
            raise  # pragma: no cover - helper always raises
        finally:
            heartbeat_stop.set()
            await heartbeat

        final = self.operations.get_record(
            record.operation_id, tenant_id=record.tenant_id
        )
        asset = await self._assets.get_asset(record.asset_id, visible_to=visible_to)
        return final.file, asset, build_upload_summary(final)

    async def _execute_stages(
        self,
        record: UploadOperationRecord,
        *,
        fence_attempt: int,
        visible_to: UserContext | None,
        spooled: SpooledUpload | None,
    ) -> None:
        self._assert_current(record, fence_attempt)
        # An operation that was ready before only prepares its stored
        # original again (queue_missing_preparations); that is no new upload.
        prepared_again = record.finished_at is not None
        asset = await self._assets.get_asset(record.asset_id, visible_to=visible_to)
        if asset.lifecycle_status != "active":
            raise AssetDeletionInProgress(record.asset_id)
        if asset.upload_operation_id != record.operation_id:
            raise AssetUploadConflict(record.asset_id)

        object_exists = await self._files.prepared_object_exists(record.file)
        if not object_exists:
            if spooled is None:
                raise UploadBytesRequired(
                    ("The temporary upload bytes are no longer available after the "
                    "interruption. Upload the same file again.")
                )
            await self._files.store_prepared_object(record.file, spooled)
            self._fault("after_object_store", record)
        self._checkpoint(record, fence_attempt, UploadStage.OBJECT_STORED)

        registered = await self._files.prepared_file_record(
            record.file.id, tenant_id=record.tenant_id
        )
        if registered is None:
            await self._files.register_prepared_file(record.file)
            self._fault("after_file_registry", record)
        elif registered != record.file:
            raise FileRegistryConflict(record.file.id)
        self._checkpoint(record, fence_attempt, UploadStage.FILE_REGISTERED)

        asset = await self._assets.get_asset(record.asset_id, visible_to=visible_to)
        if asset.lifecycle_status != "active":
            raise AssetDeletionInProgress(record.asset_id)
        if asset.server_file_id is None:
            binding = record.binding
            await self._assets.bind_uploaded_file(
                id=record.asset_id,
                section_id=binding.section_id,
                group_id=binding.group_id,
                title=binding.title,
                label=binding.label,
                file_name=record.file.file_name,
                mime_type=record.file.content_type,
                origin=binding.origin,
                page_count=binding.page_count,
                parse_status=binding.parse_status,
                parse_warning=binding.parse_warning,
                text_truncated=binding.text_truncated,
                size_bytes=record.file.size_bytes,
                server_file_id=record.file.id,
                parser_id=binding.parser_id,
                created_at=binding.created_at,
                updated_at=record.file.created_at,
                caller_user_id=record.created_by_user_id,
                workspace_id=record.workspace_id,
                visible_to=visible_to,
                upload_operation_id=record.operation_id,
            )
            self._fault("after_asset_bind", record)
        elif asset.server_file_id != record.file.id:
            raise AssetUploadConflict(record.asset_id)
        self._checkpoint(record, fence_attempt, UploadStage.ASSET_BOUND)

        if self._files.text_extraction_available:
            if _stage_before(record.stage, UploadStage.PARSING):
                self._checkpoint(record, fence_attempt, UploadStage.PARSING)
            if record.stage == UploadStage.PARSING:
                # The request owns the temporary upload spool.  Once bytes and
                # binding are durable it hands the potentially long parser to
                # the existing worker lifecycle and returns 202 immediately.
                if spooled is not None:
                    self._queue_parser_continuation(record, fence_attempt)
                asset = await self._assets.get_asset(
                    record.asset_id, visible_to=visible_to
                )
                if asset.prepared_text:
                    prepared_hash = hashlib.sha256(
                        asset.prepared_text.encode("utf-8")
                    ).hexdigest()
                    if (
                        asset.prepared_file_sha256 != record.file.sha256
                        or asset.prepared_content_hash != prepared_hash
                        or not asset.prepared_parser_id
                        or asset.prepared_at is None
                    ):
                        raise UploadOperationConflict(
                            "canonical prepared text does not match its file identity"
                        )
                else:
                    principal = (
                        visible_to.principal
                        if visible_to is not None
                        else Principal(
                            user_id=record.created_by_user_id,
                            kind=(
                                "oidc_session"
                                if record.created_by_user_id is not None
                                else "anonymous"
                            ),
                            tenant_id=record.tenant_id,
                            role=(
                                "member"
                                if record.created_by_user_id is not None
                                else "owner"
                            ),
                        )
                    )
                    try:
                        extracted = await self._files.extract_text(
                            record.file.id, principal=principal
                        )
                    except FileTextExtractionError as exc:
                        # Unconvertible content is deterministic, not a
                        # dependency outage. The original file remains usable,
                        # while the asset exposes that no canonical index
                        # source exists.
                        await self._assets.publish_parse_failure(
                            record.asset_id,
                            visible_to=visible_to,
                            server_file_id=record.file.id,
                            upload_operation_id=record.operation_id,
                            message=sanitize_error(exc),
                        )
                    else:
                        clean_text = extracted.text.strip()
                        await self._assets.publish_prepared_text(
                            record.asset_id,
                            visible_to=visible_to,
                            server_file_id=record.file.id,
                            upload_operation_id=record.operation_id,
                            text=clean_text,
                            parser_id=extracted.parser_id,
                            parser_revision=extracted.parser_revision,
                            content_hash=hashlib.sha256(
                                clean_text.encode("utf-8")
                            ).hexdigest(),
                            file_sha256=record.file.sha256,
                            page_texts=list(extracted.page_texts),
                            prepared_at=time.time(),
                        )
                self._fault("after_parse_result", record)
                self._checkpoint(record, fence_attempt, UploadStage.PARSE_FINISHED)

        if self._quota is not None and record.file.owner_user_id is not None:
            self._assert_current(record, fence_attempt)
            stock = await self._quota.set_stock_for_subject(
                QuotaSubject(
                    tenant_id=record.tenant_id,
                    user_id=record.file.owner_user_id,
                ),
                QuotaDimension.STORED_BYTES,
                stock_key=file_stock_key(record.file.id),
                amount=record.file.size_bytes,
            )
            if stock.tombstoned:
                raise AssetDeletionInProgress(record.asset_id)
        self._fault("after_quota_receipt", record)
        self._checkpoint(record, fence_attempt, UploadStage.QUOTA_BOOKED)

        landed = self.operations.complete(
            record.operation_id,
            tenant_id=record.tenant_id,
            fence_attempt=fence_attempt,
        )
        if not landed:
            raise UploadAttemptSuperseded(record.operation_id)
        if not prepared_again:
            await self._audit_uploaded(record)
        self._fault("after_ready_commit", record)

    def _checkpoint(
        self,
        record: UploadOperationRecord,
        fence_attempt: int,
        stage: UploadStage,
    ) -> None:
        landed = self.operations.checkpoint(
            record.operation_id,
            tenant_id=record.tenant_id,
            stage=stage,
            fence_attempt=fence_attempt,
        )
        if not landed:
            raise UploadAttemptSuperseded(record.operation_id)
        if not _stage_before(stage, record.stage):
            record.stage = stage

    def _queue_parser_continuation(
        self, record: UploadOperationRecord, fence_attempt: int
    ) -> None:
        landed = self.operations.queue_continuation(
            record.operation_id,
            tenant_id=record.tenant_id,
            fence_attempt=fence_attempt,
        )
        if not landed:
            raise UploadAttemptSuperseded(record.operation_id)
        raise UploadExecutionDeferred(
            self.operations.get(
                record.operation_id,
                tenant_id=record.tenant_id,
                created_by_user_id=record.created_by_user_id,
                workspace_id=record.workspace_id,
            )
        )

    def _assert_current(
        self, record: UploadOperationRecord, fence_attempt: int
    ) -> None:
        if not self.operations.is_attempt_current(
            record.operation_id,
            tenant_id=record.tenant_id,
            fence_attempt=fence_attempt,
        ):
            raise UploadAttemptSuperseded(record.operation_id)

    def _land_failure(
        self,
        record: UploadOperationRecord,
        fence_attempt: int,
        message: str,
        *,
        error_type: str,
        awaiting_bytes: bool = False,
    ) -> None:
        landed = self.operations.fail(
            record.operation_id,
            message,
            tenant_id=record.tenant_id,
            fence_attempt=fence_attempt,
            error_type=error_type,
            awaiting_bytes=awaiting_bytes,
        )
        if not landed:
            raise UploadAttemptSuperseded(record.operation_id)

    async def _discard_if_unbound(self, record: UploadOperationRecord) -> None:
        """Remove exact staged bytes when deletion won before asset binding.

        If the file is already linked to an asset, aggregate deletion owns its
        blob, registry row and quota release.  Otherwise this upload operation
        is the only possible owner and compensates its deterministic object and
        registry facts itself.
        """

        linked = await self._assets.find_asset_by_server_file_id(record.file.id)
        if linked is None:
            await self._files.discard_prepared_file(record.file)

    def _defer_dependency_failure(
        self,
        record: UploadOperationRecord,
        fence_attempt: int,
        exc: Exception,
        *,
        error_type: str,
    ) -> None:
        message = sanitize_error(exc)
        if fence_attempt < self._max_attempts:
            landed = self.operations.queue_retry(
                record.operation_id,
                message,
                tenant_id=record.tenant_id,
                fence_attempt=fence_attempt,
                error_type=error_type,
            )
            if not landed:
                raise UploadAttemptSuperseded(record.operation_id) from exc
            raise UploadExecutionDeferred(
                self.operations.get(
                    record.operation_id,
                    tenant_id=record.tenant_id,
                    created_by_user_id=record.created_by_user_id,
                    workspace_id=record.workspace_id,
                )
            ) from exc
        self._land_failure(
            record,
            fence_attempt,
            message,
            error_type="retry_budget_exhausted",
        )
        raise exc

    def _fault(self, boundary: str, record: UploadOperationRecord) -> None:
        if self._fault_hook is not None:
            self._fault_hook(boundary, record)

    async def _heartbeat_attempt(
        self,
        record: UploadOperationRecord,
        fence_attempt: int,
        stop: asyncio.Event,
    ) -> None:
        """Keep legitimate long writes distinct from a crashed process.

        Reconciliation uses heartbeat loss, never elapsed upload duration, to
        decide that an attempt may be reclaimed.  The lease therefore cannot
        turn a slow object store into a hidden timeout or quality fallback.
        """

        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                return
            except TimeoutError:
                try:
                    landed = await asyncio.to_thread(
                        self.operations.heartbeat,
                        record.operation_id,
                        tenant_id=record.tenant_id,
                        fence_attempt=fence_attempt,
                    )
                except Exception as exc:
                    log.warning(
                        "Upload heartbeat for %s could not be renewed; the current "
                        "attempt remains unchanged (error_type=%s).",
                        record.operation_id,
                        type(exc).__name__,
                    )
                    continue
                if not landed:
                    return

    def queue_missing_preparations(self, *, limit: int = 20) -> list[tuple[str, str]]:
        """Send ready uploads whose preparation was skipped back to parsing.

        An upload completed while no parser was configured is ready without
        a canonical text, and nothing on the read paths prepares an
        operation-owned file later. Once this process has a parser, such an
        operation returns to its parsing stage -- the continuation a
        deferred parse takes -- and its executor prepares the stored
        original. A recorded parse failure is never queued again.
        """
        if not self._files.text_extraction_available:
            return []
        return self.operations.queue_missing_preparations(limit=limit)

    def execute_claimed(self, claimed) -> dict:
        """Synchronous worker bridge over the same service implementation."""

        record = claimed.record
        principal = Principal(
            user_id=record.created_by_user_id,
            kind=("oidc_session" if record.created_by_user_id else "anonymous"),
            tenant_id=record.tenant_id,
            role=("member" if record.created_by_user_id else "owner"),
        )
        visible_to = UserContext(
            principal=principal,
            workspace_ids=((record.workspace_id,) if record.workspace_id else ()),
        )
        _, _, summary = run_coro_sync(
            self.execute(
                UploadAttempt(record=record, attempt=claimed.attempt),
                visible_to=visible_to,
                spooled=None,
            )
        )
        return summary


class UploadReconciler:
    """Lifespan-owned no-queue recovery for durable upload operations.

    Construction is deliberately inert.  The ASGI lifespan starts the loop
    only after application startup has succeeded and closes it before stores
    are disposed.  ``start`` and ``close`` are idempotent, and the same app
    object may enter a fresh lifespan after a clean shutdown in tests or an
    embedded server.
    """

    def __init__(
        self,
        *,
        service: UploadOperationService,
        interval_seconds: float = 5.0,
        stale_after_seconds: float = 30.0,
    ) -> None:
        self._service = service
        self._interval = max(1.0, interval_seconds)
        self._stale_after = max(1.0, stale_after_seconds)
        self._lifecycle_lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Whether the current lifespan owns a live reconciliation loop."""
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start one loop, or leave the already-running loop untouched."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(stop,),
                name="inqtrix-upload-reconciler",
                daemon=True,
            )
            self._stop = stop
            self._thread = thread
            thread.start()

    def close(self) -> None:
        """Stop the current loop and make a later clean restart possible."""
        with self._lifecycle_lock:
            stop = self._stop
            thread = self._thread
        if stop is None or thread is None:
            return
        stop.set()
        if thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5)
        with self._lifecycle_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._stop = None

    def _queued_preparations(self) -> list[tuple[str, str]]:
        """Preparations skipped for want of a parser, queued for this pass;
        failing that never blocks the stale dispatches."""
        try:
            return list(self._service.queue_missing_preparations())
        except Exception as exc:
            log.warning(
                "Missing upload preparations could not be queued (error_type=%s).",
                type(exc).__name__,
            )
            return []

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval):
            try:
                due = dict.fromkeys(
                    [
                        *self._queued_preparations(),
                        *self._service.operations.stale_dispatches(
                            older_than_seconds=self._stale_after
                        ),
                    ]
                )
                for operation_id, tenant_id in due:
                    claimed = self._service.operations.claim_for_execution(
                        operation_id, tenant_id, allow_takeover=False
                    )
                    if claimed is None:
                        continue
                    try:
                        self._service.execute_claimed(claimed)
                    except UploadBytesRequired:
                        continue
                    except UploadExecutionDeferred:
                        continue
                    except Exception as exc:
                        log.warning(
                            "Upload reconciler could not finalize %s (error_type=%s).",
                            operation_id,
                            type(exc).__name__,
                        )
            except SchemaHeadMismatch as exc:
                # The claim transaction's schema fence fired: the database
                # moved past the head this process was built for. That can
                # never heal for THIS process (its expected head is a code
                # constant), so retrying every pass would only downgrade a
                # fatal state to warning spam. Stop loudly; the upgraded
                # process brings a matching reconciler.
                log.error(
                    "Upload reconciler: schema head changed under this process; the "
                    "reconciler is stopping so an updated process can take over. %s",
                    exc,
                )
                return
            except Exception as exc:
                log.warning(
                    "Upload reconciler pass failed (error_type=%s).",
                    type(exc).__name__,
                )
