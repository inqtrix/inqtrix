"""Durable lifecycle contract for bound original-file uploads.

The multipart request owns only the temporary spool.  Everything after
spooling is represented by one immutable operation manifest and monotonic
checkpoints, so a process restart can inspect the real object/registry/asset
state and continue without guessing or silently reporting success.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from inqtrix.content.ports import FileRecord
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetUploadConflict,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:
    from inqtrix.project.asset_records_ports import AssetRecord, AssetStore


class UploadOperationNotFound(KeyError):
    """The operation is absent or outside the caller's canonical scope."""


class UploadOperationConflict(RuntimeError):
    """The asset or operation cannot accept the requested retry."""


class UploadAttemptSuperseded(RuntimeError):
    """A worker/API attempt lost its monotonic attempt fence."""


class UploadOperationStatus(StrEnum):
    RUNNING = "running"
    QUEUED = "queued"
    AWAITING_BYTES = "awaiting_bytes"
    UPLOAD_FAILED = "upload_failed"
    READY = "ready"


class UploadStage(StrEnum):
    PREPARED = "prepared"
    OBJECT_STORED = "object_stored"
    FILE_REGISTERED = "file_registered"
    ASSET_BOUND = "asset_bound"
    PARSING = "parsing"
    PARSE_FINISHED = "parse_finished"
    QUOTA_BOOKED = "quota_booked"
    READY = "ready"


@dataclass(frozen=True)
class UploadBinding:
    """Validated library placement and display facts frozen for one upload."""

    section_id: str
    group_id: str | None
    title: str
    label: str
    origin: str
    page_count: int | None
    parse_status: str
    parse_warning: str | None
    text_truncated: bool
    parser_id: str | None
    created_at: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "group_id": self.group_id,
            "title": self.title,
            "label": self.label,
            "origin": self.origin,
            "page_count": self.page_count,
            "parse_status": self.parse_status,
            "parse_warning": self.parse_warning,
            "text_truncated": self.text_truncated,
            "parser_id": self.parser_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "UploadBinding":
        return cls(
            section_id=str(payload["section_id"]),
            group_id=(str(payload["group_id"]) if payload.get("group_id") else None),
            title=str(payload.get("title", "")),
            label=str(payload.get("label", "")),
            origin=str(payload.get("origin", "library")),
            page_count=(
                int(payload["page_count"])
                if payload.get("page_count") is not None
                else None
            ),
            parse_status=str(payload.get("parse_status", "parsed")),
            parse_warning=(
                str(payload["parse_warning"])
                if payload.get("parse_warning") is not None
                else None
            ),
            text_truncated=bool(payload.get("text_truncated", False)),
            parser_id=(
                str(payload["parser_id"])
                if payload.get("parser_id") is not None
                else None
            ),
            created_at=float(payload.get("created_at", 0.0)),
        )


@dataclass
class UploadOperationRecord:
    operation_id: str
    asset_id: str
    file: FileRecord
    binding: UploadBinding
    tenant_id: str
    created_by_user_id: uuid.UUID | None
    workspace_id: str | None
    created_at: float
    updated_at: float
    status: UploadOperationStatus = UploadOperationStatus.RUNNING
    stage: UploadStage = UploadStage.PREPARED
    attempt: int = 1
    claimed_by: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: dict[str, str] | None = None
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class UploadAttempt:
    record: UploadOperationRecord
    attempt: int
    already_ready: bool = False


def new_upload_operation_id() -> str:
    return f"upl_{uuid.uuid4().hex}"


def file_record_to_payload(record: FileRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "owner_user_id": (
            str(record.owner_user_id) if record.owner_user_id is not None else None
        ),
        "workspace_id": record.workspace_id,
        "file_name": record.file_name,
        "content_type": record.content_type,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "object_key": record.object_key,
        "created_at": record.created_at,
    }


def file_record_from_payload(payload: dict[str, Any]) -> FileRecord:
    return FileRecord(
        id=str(payload["id"]),
        tenant_id=str(payload["tenant_id"]),
        owner_user_id=(
            uuid.UUID(str(payload["owner_user_id"]))
            if payload.get("owner_user_id") is not None
            else None
        ),
        workspace_id=(
            str(payload["workspace_id"])
            if payload.get("workspace_id") is not None
            else None
        ),
        file_name=str(payload["file_name"]),
        content_type=str(payload["content_type"]),
        size_bytes=int(payload["size_bytes"]),
        sha256=str(payload["sha256"]),
        object_key=str(payload["object_key"]),
        created_at=float(payload["created_at"]),
    )


def build_upload_summary(record: UploadOperationRecord) -> dict[str, Any]:
    """Project only user-safe lifecycle facts."""

    return {
        "operation_id": record.operation_id,
        "asset_id": record.asset_id,
        "file_id": record.file.id,
        "status": record.status.value,
        "stage": record.stage.value,
        "attempt": record.attempt,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error": dict(record.error) if record.error else None,
        "retryable": record.status
        in {
            UploadOperationStatus.AWAITING_BYTES,
            UploadOperationStatus.UPLOAD_FAILED,
        },
        "requires_bytes": record.status == UploadOperationStatus.AWAITING_BYTES,
    }


def append_memory_event(
    record: UploadOperationRecord, event_type: str, data: dict[str, Any]
) -> None:
    record.events.append(
        {
            "sequence": len(record.events) + 1,
            "type": event_type,
            "created_at": time.time(),
            "data": dict(data),
        }
    )


def _same_uploaded_file(left: FileRecord, right: FileRecord) -> bool:
    """Compare user-observable immutable file facts, not generated ids/time."""

    return (
        left.tenant_id == right.tenant_id
        and left.owner_user_id == right.owner_user_id
        and left.workspace_id == right.workspace_id
        and left.file_name == right.file_name
        and left.content_type == right.content_type
        and left.size_bytes == right.size_bytes
        and left.sha256 == right.sha256
    )


def _same_upload_binding(left: UploadBinding, right: UploadBinding) -> bool:
    """Compare the immutable placement contract, excluding UI timestamps.

    ``created_at`` belongs to the already-reserved asset and is preserved by
    the asset store.  A browser retry may reconstruct its multipart request
    after a reload and therefore must not have to reproduce a client timestamp
    byte-for-byte.  Placement, labels and parsing facts remain fenced.
    """

    return (
        left.section_id == right.section_id
        and left.group_id == right.group_id
        and left.title == right.title
        and left.label == right.label
        and left.origin == right.origin
        and left.page_count == right.page_count
        and left.parse_status == right.parse_status
        and left.parse_warning == right.parse_warning
        and left.text_truncated == right.text_truncated
        and left.parser_id == right.parser_id
    )


class MemoryUploadOperationStore:
    """Process-local semantic twin of the durable Postgres operation store."""

    def __init__(
        self,
        *,
        assets: "AssetStore",
        worker_id: str = "in-process-upload",
        reconcile_delay_seconds: float = 120.0,
    ) -> None:
        import threading

        self._assets = assets
        self._worker_id = worker_id
        self._reconcile_delay_seconds = reconcile_delay_seconds
        self._records: dict[str, UploadOperationRecord] = {}
        self._by_asset: dict[tuple[str, uuid.UUID | None, str | None, str], str] = {}
        self._lock = threading.RLock()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start_or_resume(
        self,
        *,
        asset: "AssetRecord",
        proposed_file: FileRecord,
        binding: UploadBinding,
    ) -> UploadAttempt:
        key = (
            asset.tenant_id,
            asset.created_by_user_id,
            asset.workspace_id,
            asset.id,
        )
        now = time.time()
        with self._lock:
            operation_id = self._by_asset.get(key)
            existing = self._records.get(operation_id) if operation_id else None
            if existing is not None:
                if not _same_uploaded_file(existing.file, proposed_file):
                    raise UploadOperationConflict(
                        "asset id already belongs to a different upload"
                    )
                if not _same_upload_binding(existing.binding, binding):
                    raise UploadOperationConflict(
                        "upload binding changed after the operation was prepared"
                    )
                if existing.status == UploadOperationStatus.READY:
                    return UploadAttempt(
                        record=existing,
                        attempt=existing.attempt,
                        already_ready=True,
                    )
                run_coro_sync(
                    self._assets.set_asset_upload_state(
                        asset.id,
                        scope=ResourceScope.from_record(asset),
                        upload_status="uploading",
                        upload_error=None,
                        upload_operation_id=existing.operation_id,
                        expected_upload_operation_id=existing.operation_id,
                    )
                )
                existing.status = UploadOperationStatus.RUNNING
                existing.attempt += 1
                existing.claimed_by = self._worker_id
                existing.updated_at = now
                existing.started_at = existing.started_at or now
                existing.finished_at = None
                existing.error = None
                record = existing
                append_memory_event(
                    record,
                    "inqtrix.upload.retried",
                    {"attempt": record.attempt, "stage": record.stage.value},
                )
            else:
                operation_id = new_upload_operation_id()
                record = UploadOperationRecord(
                    operation_id=operation_id,
                    asset_id=asset.id,
                    file=proposed_file,
                    binding=binding,
                    tenant_id=asset.tenant_id,
                    created_by_user_id=asset.created_by_user_id,
                    workspace_id=asset.workspace_id,
                    created_at=now,
                    updated_at=now,
                    claimed_by=self._worker_id,
                    started_at=now,
                )
                # Publish the operation id on the source aggregate before the
                # operation becomes discoverable in this store.  A deletion
                # fence therefore leaves neither a detached operation nor a
                # writable reservation behind.
                run_coro_sync(
                    self._assets.set_asset_upload_state(
                        asset.id,
                        scope=ResourceScope.from_record(asset),
                        upload_status="uploading",
                        upload_error=None,
                        upload_operation_id=record.operation_id,
                    )
                )
                append_memory_event(
                    record,
                    "inqtrix.upload.prepared",
                    {"attempt": 1, "stage": UploadStage.PREPARED.value},
                )
                self._records[operation_id] = record
                self._by_asset[key] = operation_id
            return UploadAttempt(record=record, attempt=record.attempt)

    def get_record(
        self, operation_id: str, *, tenant_id: str = "default"
    ) -> UploadOperationRecord:
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise UploadOperationNotFound(operation_id)
            return record

    def get(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        record = self.get_record(operation_id, tenant_id=tenant_id)
        if (
            record.tenant_id != tenant_id
            or record.created_by_user_id != created_by_user_id
            or (workspace_id is not None and record.workspace_id != workspace_id)
        ):
            raise UploadOperationNotFound(operation_id)
        return build_upload_summary(record)

    def list_operations(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.created_by_user_id == created_by_user_id
                and record.workspace_id == workspace_id
            ]
            rows.sort(
                key=lambda item: (item.created_at, item.operation_id), reverse=True
            )
            return [build_upload_summary(item) for item in rows[:limit]]

    def is_attempt_current(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            return bool(
                record is not None
                and record.status == UploadOperationStatus.RUNNING
                and record.attempt == fence_attempt
            )

    def heartbeat(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        """Renew a live-attempt lease without changing lifecycle progress."""

        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
            ):
                return False
            record.updated_at = time.time()
            return True

    def checkpoint(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        stage: UploadStage,
        fence_attempt: int,
    ) -> bool:
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
            ):
                return False
            if list(UploadStage).index(stage) < list(UploadStage).index(record.stage):
                return True
            record.stage = stage
            record.updated_at = time.time()
            if stage in {
                UploadStage.PARSING,
                UploadStage.PARSE_FINISHED,
                UploadStage.QUOTA_BOOKED,
            }:
                try:
                    asset = run_coro_sync(self._assets.get_asset(record.asset_id))
                    run_coro_sync(
                        self._assets.set_asset_upload_state(
                            record.asset_id,
                            scope=ResourceScope.from_record(asset),
                            upload_status=(
                                "parsing"
                                if stage == UploadStage.PARSING
                                else "finalizing"
                            ),
                            upload_error=None,
                            upload_operation_id=record.operation_id,
                            expected_upload_operation_id=record.operation_id,
                        )
                    )
                except (AssetDeletionInProgress, AssetNotFound, AssetUploadConflict):
                    return False
            append_memory_event(
                record,
                "inqtrix.upload.progress",
                {"status": "running", "stage": stage.value},
            )
            return True

    def queue_continuation(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        """Hand a prepared upload from the HTTP attempt to a worker."""

        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
                or record.stage != UploadStage.PARSING
            ):
                return False
            record.status = UploadOperationStatus.QUEUED
            record.claimed_by = None
            record.updated_at = time.time()
            append_memory_event(
                record,
                "inqtrix.upload.continuation_queued",
                {"status": "queued", "stage": record.stage.value},
            )
            return True

    def queue_missing_preparations(self, *, limit: int) -> list[tuple[str, str]]:
        """Return ready operations whose asset never got a preparation to parsing.

        ``finished_at`` stays set: the executor reads it as "prepared again".
        """

        queued: list[tuple[str, str]] = []
        with self._lock:
            for record in self._records.values():
                if len(queued) >= limit:
                    break
                if record.status != UploadOperationStatus.READY:
                    continue
                try:
                    asset = run_coro_sync(self._assets.get_asset(record.asset_id))
                except AssetNotFound:
                    continue
                if (
                    asset.upload_operation_id != record.operation_id
                    or asset.lifecycle_status != "active"
                    or asset.upload_status != "ready"
                    or asset.prepared_parser_id is not None
                    or asset.parse_status == "error"
                ):
                    continue
                run_coro_sync(
                    self._assets.set_asset_upload_state(
                        record.asset_id,
                        scope=ResourceScope.from_record(asset),
                        upload_status="parsing",
                        upload_error=None,
                        upload_operation_id=record.operation_id,
                        expected_upload_operation_id=record.operation_id,
                    )
                )
                record.status = UploadOperationStatus.QUEUED
                record.stage = UploadStage.PARSING
                record.claimed_by = None
                record.error = None
                record.updated_at = time.time()
                append_memory_event(
                    record,
                    "inqtrix.upload.preparation_queued",
                    {"status": "queued", "stage": record.stage.value},
                )
                queued.append((record.operation_id, record.tenant_id))
        return queued

    def complete(
        self,
        operation_id: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
    ) -> bool:
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
                or record.stage != UploadStage.QUOTA_BOOKED
            ):
                return False
            asset = run_coro_sync(self._assets.get_asset(record.asset_id))
            run_coro_sync(
                self._assets.set_asset_upload_state(
                    record.asset_id,
                    scope=ResourceScope.from_record(asset),
                    upload_status="ready",
                    upload_error=None,
                    upload_operation_id=record.operation_id,
                    expected_upload_operation_id=record.operation_id,
                )
            )
            now = time.time()
            record.status = UploadOperationStatus.READY
            record.stage = UploadStage.READY
            record.claimed_by = None
            record.updated_at = now
            record.finished_at = now
            record.error = None
            append_memory_event(record, "inqtrix.upload.ready", {"status": "ready"})
            return True

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
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
            ):
                return False
            status = (
                UploadOperationStatus.AWAITING_BYTES
                if awaiting_bytes
                else UploadOperationStatus.UPLOAD_FAILED
            )
            now = time.time()
            record.status = status
            record.claimed_by = None
            record.updated_at = now
            record.finished_at = now
            record.error = {"message": message, "type": error_type}
            try:
                asset = run_coro_sync(self._assets.get_asset(record.asset_id))
                run_coro_sync(
                    self._assets.set_asset_upload_state(
                        record.asset_id,
                        scope=ResourceScope.from_record(asset),
                        upload_status="failed",
                        upload_error=message,
                        upload_operation_id=record.operation_id,
                        expected_upload_operation_id=record.operation_id,
                    )
                )
            except (AssetUploadConflict, AssetDeletionInProgress, AssetNotFound):
                # Deletion owns the aggregate projection now.  The operation
                # failure remains durable and inspectable independently.
                pass
            append_memory_event(
                record,
                (
                    "inqtrix.upload.awaiting_bytes"
                    if awaiting_bytes
                    else "inqtrix.upload.failed"
                ),
                {
                    "status": status.value,
                    "stage": record.stage.value,
                    "error": record.error,
                },
            )
            return True

    def queue_retry(
        self,
        operation_id: str,
        message: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
        error_type: str = "dependency_error",
    ) -> bool:
        del tenant_id
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.status != UploadOperationStatus.RUNNING
                or record.attempt != fence_attempt
            ):
                return False
            record.status = UploadOperationStatus.QUEUED
            record.claimed_by = None
            record.updated_at = time.time()
            record.error = {"message": message, "type": error_type}
            try:
                asset = run_coro_sync(self._assets.get_asset(record.asset_id))
                run_coro_sync(
                    self._assets.set_asset_upload_state(
                        record.asset_id,
                        scope=ResourceScope.from_record(asset),
                        upload_status="retrying",
                        upload_error=message,
                        upload_operation_id=record.operation_id,
                        expected_upload_operation_id=record.operation_id,
                    )
                )
            except (AssetDeletionInProgress, AssetNotFound, AssetUploadConflict):
                # The upload ledger remains the recovery source even after the
                # aggregate has moved under deletion authority.
                pass
            append_memory_event(
                record,
                "inqtrix.upload.retry_scheduled",
                {
                    "status": "queued",
                    "stage": record.stage.value,
                    "error": record.error,
                },
            )
            return True

    def claim_for_execution(
        self, operation_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> UploadAttempt | None:
        del allow_takeover
        with self._lock:
            record = self._records.get(operation_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.status != UploadOperationStatus.QUEUED
            ):
                return None
            record.status = UploadOperationStatus.RUNNING
            record.attempt += 1
            record.claimed_by = self._worker_id
            record.updated_at = time.time()
            return UploadAttempt(record=record, attempt=record.attempt)

    def stale_dispatches(self, *, older_than_seconds: float) -> list[tuple[str, str]]:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            ready: list[tuple[str, str]] = []
            for record in self._records.values():
                if record.status == UploadOperationStatus.QUEUED or (
                    record.status == UploadOperationStatus.RUNNING
                    and record.updated_at <= cutoff
                ):
                    record.status = UploadOperationStatus.QUEUED
                    record.claimed_by = None
                    ready.append((record.operation_id, record.tenant_id))
            return ready

    def retry(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        record = self.get_record(operation_id, tenant_id=tenant_id)
        if (
            record.tenant_id != tenant_id
            or record.created_by_user_id != created_by_user_id
            or record.workspace_id != workspace_id
        ):
            raise UploadOperationNotFound(operation_id)
        if record.status == UploadOperationStatus.AWAITING_BYTES:
            raise UploadOperationConflict("upload bytes are required")
        if record.status != UploadOperationStatus.UPLOAD_FAILED:
            raise UploadOperationConflict(operation_id)
        record.status = UploadOperationStatus.QUEUED
        record.finished_at = None
        record.error = None
        record.updated_at = time.time()
        return build_upload_summary(record)

    def close(self) -> None:
        return None
