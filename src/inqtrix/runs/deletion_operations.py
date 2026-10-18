"""Durable-operation contract for destructive aggregate lifecycle work.

Deleting a library file, vector index, knowledge resource, or durable desk
session is an aggregate operation, not a row delete. All owned search,
knowledge, blob, run, quota, and metadata state must converge on the same end state.
This module owns the backend-neutral state machine and its in-memory
implementation. The Postgres/Valkey implementation uses the same records and
summary shape so clients never need to infer whether deletion is durable.

The operation deliberately has no cancel transition.  Once a source has been
detached from search, undoing only part of the saga would be a second,
ill-defined write operation.  A failed operation retains its manifest and is
explicitly retryable instead.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable

from inqtrix.pagination import keyset_page

log = logging.getLogger("inqtrix")


class DeletionOperationNotFound(KeyError):
    """Raised when an operation is absent or invisible to the caller."""


class DeletionOperationConflict(RuntimeError):
    """Raised when the target already has a non-terminal deletion."""


class DeletionAttemptSuperseded(RuntimeError):
    """Raised when a worker no longer owns the destructive attempt fence."""


def refuse_on_event_loop(action: str) -> None:
    """Refuse a deletion-store write on a thread that runs an event loop.

    Registering or reopening an operation can wait for row and tenant locks
    that other requests hold. Their transactions run on the same event loop
    and can only commit while it keeps running, so a write that blocked the
    loop would wait for itself. A caller on a loop hands the write to a
    worker thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"a deletion {action} must not run on an event loop thread; "
        "hand it to a worker thread"
    )


class DeletionOperationStatus(StrEnum):
    """Lifecycle states exposed by the deletion API."""

    QUEUED = "queued"
    RUNNING = "running"
    DELETE_FAILED = "delete_failed"
    DELETED = "deleted"


class DeletionStage(StrEnum):
    """Ordered, truthful checkpoints of the aggregate cleanup."""

    QUEUED = "queued"
    VECTOR_INDEX_DETACHED = "vector_index_detached"
    INDEXING_CANCELLED = "indexing_cancelled"
    SEARCH_DETACHED = "search_detached"
    VECTORS_REMOVED = "vectors_removed"
    KNOWLEDGE_REMOVED = "knowledge_removed"
    SESSION_DATA_REMOVED = "session_data_removed"
    BLOBS_REMOVED = "blobs_removed"
    METADATA_REMOVED = "metadata_removed"
    AGGREGATES_REMOVED = "aggregates_removed"
    ROWS_REMOVED = "rows_removed"
    RESIDUALS_VERIFIED = "residuals_verified"
    DELETE_FAILED = "delete_failed"
    DELETED = "deleted"


class DeletionTargetKind(StrEnum):
    """Destructive aggregate selected by the user."""

    ASSET = "asset"
    BULK = "bulk"
    GROUP = "group"
    SECTION = "section"
    VECTOR_INDEX = "vector_index"
    KNOWLEDGE_COLLECTION = "knowledge_collection"
    KNOWLEDGE_DOCUMENT = "knowledge_document"
    AGENT_SESSION = "agent_session"
    KNOWLEDGE_SESSION = "knowledge_session"
    #: An account, erased by an administrator. The operation belongs to the
    #: administrator; ``target_id`` is the account's user id.
    USER = "user"
    #: A source whose asset, section or document is already gone, settled
    #: by an account erasure. ``target_id`` is the source id itself.
    SOURCE = "source"


def target_source_id(target_kind: DeletionTargetKind, target_id: str) -> str | None:
    """The source a deletion's target owns beside its manifest, if any.

    A section is a source of its own; an orphaned source is nothing else.
    Registration fences it and the terminal write keeps its tombstone,
    exactly like the sources of the manifest.
    """
    if target_kind == DeletionTargetKind.SECTION:
        return f"section:{target_id}"
    if target_kind == DeletionTargetKind.SOURCE:
        return target_id
    return None


KNOWLEDGE_DELETION_TARGET_KINDS = frozenset(
    {
        DeletionTargetKind.KNOWLEDGE_COLLECTION,
        DeletionTargetKind.KNOWLEDGE_DOCUMENT,
    }
)

SESSION_DELETION_TARGET_KINDS = frozenset(
    {
        DeletionTargetKind.AGENT_SESSION,
        DeletionTargetKind.KNOWLEDGE_SESSION,
    }
)


ACTIVE_DELETION_STATUSES = frozenset(
    {DeletionOperationStatus.QUEUED, DeletionOperationStatus.RUNNING}
)
TERMINAL_DELETION_STATUSES = frozenset(
    {DeletionOperationStatus.DELETE_FAILED, DeletionOperationStatus.DELETED}
)


@dataclass(frozen=True)
class DeletionManifestItem:
    """Stable identifiers retained until every cleanup step is verified."""

    asset_id: str
    source_id: str
    server_file_id: str | None
    size_bytes: int
    source_cleanup_plan: dict[str, Any] | None = None
    upload_operation_id: str | None = None
    file_owner_user_id: uuid.UUID | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "server_file_id": self.server_file_id,
            "size_bytes": self.size_bytes,
            "source_cleanup_plan": self.source_cleanup_plan,
            "upload_operation_id": self.upload_operation_id,
            "file_owner_user_id": (
                str(self.file_owner_user_id)
                if self.file_owner_user_id is not None
                else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DeletionManifestItem":
        return cls(
            asset_id=str(payload["asset_id"]),
            source_id=str(payload["source_id"]),
            server_file_id=(
                str(payload["server_file_id"])
                if payload.get("server_file_id") is not None
                else None
            ),
            size_bytes=max(0, int(payload.get("size_bytes", 0))),
            source_cleanup_plan=(
                dict(payload["source_cleanup_plan"])
                if isinstance(payload.get("source_cleanup_plan"), dict)
                else None
            ),
            upload_operation_id=(
                str(payload["upload_operation_id"])
                if payload.get("upload_operation_id") is not None
                else None
            ),
            file_owner_user_id=(
                uuid.UUID(str(payload["file_owner_user_id"]))
                if payload.get("file_owner_user_id") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class VectorIndexDeletionContext:
    """Immutable recovery pointer for one vector-index aggregate deletion.

    The vector-index row is intentionally removed only near the end of the
    operation.  A worker retry must nevertheless remain possible after either
    the backing collection or that row has already disappeared, so all
    identifiers needed by later attempts live on the durable operation.
    """

    index_id: str
    server_collection_id: str | None
    embedding_model: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": DeletionTargetKind.VECTOR_INDEX.value,
            "index_id": self.index_id,
            "server_collection_id": self.server_collection_id,
            "embedding_model": self.embedding_model,
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> "VectorIndexDeletionContext | None":
        if not payload or payload.get("kind") != DeletionTargetKind.VECTOR_INDEX.value:
            return None
        index_id = payload.get("index_id")
        if not isinstance(index_id, str) or not index_id:
            raise ValueError("vector-index deletion context has no index_id")
        raw_collection_id = payload.get("server_collection_id")
        raw_model = payload.get("embedding_model")
        return cls(
            index_id=index_id,
            server_collection_id=(
                str(raw_collection_id) if raw_collection_id is not None else None
            ),
            embedding_model=str(raw_model) if raw_model is not None else None,
        )


@dataclass(frozen=True)
class KnowledgeDeletionContext:
    """Immutable collection/document identifiers for durable knowledge cleanup."""

    target_kind: DeletionTargetKind
    collection_id: str
    embedding_model: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if self.target_kind not in {
            DeletionTargetKind.KNOWLEDGE_COLLECTION,
            DeletionTargetKind.KNOWLEDGE_DOCUMENT,
        }:
            raise ValueError("invalid knowledge deletion target kind")
        if (
            self.target_kind == DeletionTargetKind.KNOWLEDGE_DOCUMENT
            and not self.document_id
        ):
            raise ValueError("knowledge-document context requires document_id")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.target_kind.value,
            "collection_id": self.collection_id,
            "document_id": self.document_id,
            "embedding_model": self.embedding_model,
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> "KnowledgeDeletionContext | None":
        if not payload or payload.get("kind") not in {
            DeletionTargetKind.KNOWLEDGE_COLLECTION.value,
            DeletionTargetKind.KNOWLEDGE_DOCUMENT.value,
        }:
            return None
        return cls(
            target_kind=DeletionTargetKind(str(payload["kind"])),
            collection_id=str(payload["collection_id"]),
            document_id=(
                str(payload["document_id"])
                if payload.get("document_id") is not None
                else None
            ),
            embedding_model=str(payload["embedding_model"]),
        )


@dataclass(frozen=True)
class SessionDeletionContext:
    """Immutable session identity and run lineage retained across retries."""

    target_kind: DeletionTargetKind
    session_id: str
    run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.target_kind not in SESSION_DELETION_TARGET_KINDS:
            raise ValueError("invalid session deletion target kind")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.target_kind.value,
            "session_id": self.session_id,
            "run_ids": list(self.run_ids),
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> "SessionDeletionContext | None":
        if not payload or payload.get("kind") not in {
            kind.value for kind in SESSION_DELETION_TARGET_KINDS
        }:
            return None
        raw_run_ids = payload.get("run_ids", [])
        if not isinstance(raw_run_ids, list) or not all(
            isinstance(item, str) and item for item in raw_run_ids
        ):
            raise ValueError("session deletion context has invalid run_ids")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session deletion context has no session_id")
        return cls(
            target_kind=DeletionTargetKind(str(payload["kind"])),
            session_id=session_id,
            run_ids=tuple(dict.fromkeys(raw_run_ids)),
        )


@dataclass
class DeletionOperationRecord:
    """Canonical state for one idempotent deletion saga."""

    operation_id: str
    target_kind: DeletionTargetKind
    target_id: str
    manifest: tuple[DeletionManifestItem, ...]
    tenant_id: str
    created_by_user_id: uuid.UUID | None
    workspace_id: str | None
    vector_index_context: VectorIndexDeletionContext | None
    knowledge_context: KnowledgeDeletionContext | None
    session_context: SessionDeletionContext | None
    created_at: float
    updated_at: float
    status: DeletionOperationStatus = DeletionOperationStatus.QUEUED
    stage: DeletionStage = DeletionStage.QUEUED
    completed_items: int = 0
    total_items: int = 0
    attempt: int = 0
    claimed_by: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: dict[str, str] | None = None
    work: "DeletionWork | None" = field(default=None, repr=False)
    terminal_action: "DeletionTerminalAction | None" = field(
        default=None, repr=False
    )


DeletionWork = Callable[["DeletionJobHandle"], None]
DeletionManifestRefresh = Callable[[], tuple[DeletionManifestItem, ...]]
DeletionTerminalAction = Callable[[], None]


def new_deletion_operation_id() -> str:
    """Return an opaque public identifier for one deletion operation."""

    return f"del_{uuid.uuid4().hex}"


def build_deletion_summary(record: DeletionOperationRecord) -> dict[str, Any]:
    """Project an operation without leaking blob or internal document ids."""

    return {
        "operation_id": record.operation_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "asset_ids": [item.asset_id for item in record.manifest],
        "status": record.status.value,
        "stage": record.stage.value,
        "completed_items": record.completed_items,
        "total_items": record.total_items,
        "attempt": record.attempt,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error": dict(record.error) if record.error else None,
        "retryable": record.status == DeletionOperationStatus.DELETE_FAILED,
    }


class DeletionJobHandle:
    """Progress/terminal writer passed to one deletion attempt."""

    def __init__(self, store: Any, operation_id: str) -> None:
        self._store = store
        self.operation_id = operation_id
        self.terminal_landed = False
        self.manages_asset_lifecycle = False

    def assert_current(self) -> None:
        """Stop before another external mutation when this attempt lost ownership."""

        checker = getattr(self._store, "is_current", None)
        if checker is not None and not checker(self.operation_id):
            raise DeletionAttemptSuperseded(self.operation_id)

    def checkpoint_source_cleanup(self, asset_id: str, plan: dict[str, Any]) -> None:
        landed = self._store.checkpoint_source_cleanup(
            self.operation_id,
            asset_id=asset_id,
            plan=plan,
        )
        if landed is False:
            raise DeletionAttemptSuperseded(self.operation_id)

    def source_deletion_permit(self, scope: Any) -> Any:
        return self._store.source_deletion_permit(
            self.operation_id,
            scope=scope,
        )

    def progress(
        self,
        stage: DeletionStage,
        *,
        completed_items: int,
        total_items: int,
    ) -> None:
        landed = self._store.progress(
            self.operation_id,
            stage=stage,
            completed_items=completed_items,
            total_items=total_items,
        )
        if landed is False:
            raise DeletionAttemptSuperseded(self.operation_id)

    def complete(self) -> None:
        self.terminal_landed = bool(self._store.complete(self.operation_id))
        if not self.terminal_landed:
            raise DeletionAttemptSuperseded(self.operation_id)

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        self.terminal_landed = bool(
            self._store.fail(
                self.operation_id,
                message,
                error_type=error_type,
            )
        )


class DeletionOperationStore:
    """Thread-safe in-memory operation store and background dispatcher.

    This is the zero-infrastructure tier.  It intentionally mirrors the
    durable store's public surface, including idempotent active-target
    submission and retrying the same operation id.
    """

    def __init__(self, *, worker_id: str = "in-process-deletion") -> None:
        self._worker_id = worker_id
        self._records: dict[str, DeletionOperationRecord] = {}
        self._lock = threading.RLock()
        from inqtrix.source_authority import MemorySourceLifecycleAuthority

        self._source_authority = MemorySourceLifecycleAuthority()
        self._authority: Any | None = None

    def bind_source_lifecycle_authority(self, authority: Any) -> None:
        self._source_authority = authority

    def bind_authority_coordinator(self, coordinator: Any) -> None:
        """Memory twin of the Postgres in-tx audit rows."""
        self._authority = coordinator

    def _append_audit_locked(
        self,
        record: "DeletionOperationRecord",
        *,
        action: str,
        outcome: str,
        detail: dict[str, str] | None = None,
    ) -> None:
        append_row = getattr(self._authority, "append_audit_row", None)
        if append_row is None:
            return
        workspace_uuid = None
        if record.workspace_id:
            try:
                workspace_uuid = uuid.UUID(str(record.workspace_id))
            except ValueError:
                workspace_uuid = None
        try:
            append_row(
                tenant_id=record.tenant_id,
                actor_user_id=record.created_by_user_id,
                action=action,
                resource_type=str(record.target_kind.value),
                resource_id=record.target_id,
                detail=detail or {},
                outcome=outcome,
                correlation={"run_id": record.operation_id},
                workspace_id=workspace_uuid,
            )
        except Exception:  # noqa: BLE001 — index row must not kill terminals
            log.warning(
                "Deletion index row for %s could not be written.",
                record.operation_id,
                exc_info=True,
            )

    @property
    def worker_id(self) -> str:
        return self._worker_id

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
        before_dispatch: Callable[[str], None] | None = None,
        refresh_manifest: DeletionManifestRefresh | None = None,
        terminal_action: DeletionTerminalAction | None = None,
        vector_index_context: VectorIndexDeletionContext | None = None,
        knowledge_context: KnowledgeDeletionContext | None = None,
        session_context: SessionDeletionContext | None = None,
        total_items: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Register one deletion and start it.

        ``operation_id`` lets a caller that already recorded the id
        elsewhere register under that id, as an account erasure does.
        """
        refuse_on_event_loop("registration")
        now = time.time()
        with self._lock:
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
                    DeletionTargetKind.VECTOR_INDEX,
                    DeletionTargetKind.KNOWLEDGE_COLLECTION,
                }
                if target_kind != DeletionTargetKind.KNOWLEDGE_DOCUMENT:
                    blocking_kinds.add(DeletionTargetKind.KNOWLEDGE_DOCUMENT)
                for existing in self._records.values():
                    existing_collection_id = (
                        existing.vector_index_context.server_collection_id
                        if existing.vector_index_context is not None
                        else (
                            existing.knowledge_context.collection_id
                            if existing.knowledge_context is not None
                            else None
                        )
                    )
                    retained = existing.status in ACTIVE_DELETION_STATUSES or (
                        existing.status == DeletionOperationStatus.DELETE_FAILED
                        and existing.completed_items > 0
                    )
                    if not (
                        existing.tenant_id == tenant_id
                        and existing.target_kind in blocking_kinds
                        and existing_collection_id == destructive_collection_id
                        and retained
                    ):
                        continue
                    if (
                        existing.target_kind == target_kind
                        and existing.target_id == target_id
                        and existing.created_by_user_id == created_by_user_id
                    ):
                        return build_deletion_summary(existing)
                    raise DeletionOperationConflict(destructive_collection_id)
            for existing in self._records.values():
                same_knowledge_target = target_kind in {
                    DeletionTargetKind.KNOWLEDGE_COLLECTION,
                    DeletionTargetKind.KNOWLEDGE_DOCUMENT,
                }
                same_session_target = target_kind in SESSION_DELETION_TARGET_KINDS
                retained = (
                    existing.status in ACTIVE_DELETION_STATUSES
                    or (
                        same_knowledge_target
                        and existing.status == DeletionOperationStatus.DELETE_FAILED
                        and existing.completed_items > 0
                    )
                    or (
                        same_session_target
                        and existing.status == DeletionOperationStatus.DELETE_FAILED
                    )
                )
                if not (
                    existing.tenant_id == tenant_id
                    and existing.target_kind == target_kind
                    and existing.target_id == target_id
                    and retained
                ):
                    continue
                if same_knowledge_target:
                    if existing.created_by_user_id == created_by_user_id:
                        return build_deletion_summary(existing)
                    raise DeletionOperationConflict(target_id)
                if (
                    existing.created_by_user_id == created_by_user_id
                    and existing.workspace_id == workspace_id
                ):
                    return build_deletion_summary(existing)
            requested_assets = {item.asset_id for item in manifest}
            if requested_assets:
                for existing in self._records.values():
                    if (
                        existing.tenant_id == tenant_id
                        and existing.created_by_user_id == created_by_user_id
                        and existing.workspace_id == workspace_id
                        and existing.status in ACTIVE_DELETION_STATUSES
                        and requested_assets.intersection(
                            item.asset_id for item in existing.manifest
                        )
                    ):
                        raise DeletionOperationConflict(existing.operation_id)
            operation_id = operation_id or new_deletion_operation_id()
            from inqtrix.source_authority import SourceLifecycleConflict, SourceScope

            source_scopes = [
                SourceScope(
                    tenant_id=tenant_id,
                    source_id=item.source_id,
                    owner_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
                for item in manifest
            ]
            target_source = target_source_id(target_kind, target_id)
            if target_source is not None:
                source_scopes.append(
                    SourceScope(
                        tenant_id=tenant_id,
                        source_id=target_source,
                        owner_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                    )
                )
            try:
                self._source_authority.begin_delete_many(
                    tuple(source_scopes), operation_id=operation_id
                )
            except SourceLifecycleConflict as exc:
                raise DeletionOperationConflict(target_id) from exc
            record = DeletionOperationRecord(
                operation_id=operation_id,
                target_kind=target_kind,
                target_id=target_id,
                manifest=manifest,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                vector_index_context=vector_index_context,
                knowledge_context=knowledge_context,
                session_context=session_context,
                created_at=now,
                updated_at=now,
                total_items=(
                    max(0, int(total_items))
                    if total_items is not None
                    else len(manifest)
                ),
                work=work,
                terminal_action=terminal_action,
            )
            self._records[operation_id] = record
            self._append_audit_locked(
                record,
                action="asset.delete_requested",
                outcome="success",
                detail={"manifest_items": str(len(manifest))},
            )
            if refresh_manifest is not None:
                try:
                    refreshed_manifest = refresh_manifest()
                    original_sources = tuple(
                        (item.asset_id, item.source_id) for item in manifest
                    )
                    refreshed_sources = tuple(
                        (item.asset_id, item.source_id) for item in refreshed_manifest
                    )
                    if refreshed_sources != original_sources:
                        raise RuntimeError(
                            "deletion manifest refresh changed fenced source identity"
                        )
                    record.manifest = refreshed_manifest
                    record.total_items = len(refreshed_manifest)
                    record.updated_at = time.time()
                except Exception as exc:
                    # The receipt must survive a failed post-fence refresh so
                    # the source cannot become an invisible permanent
                    # tombstone. A retry can safely continue from the stable
                    # original identity manifest.
                    self.fail(
                        operation_id,
                        str(exc),
                        error_type="server_error",
                    )
                    return build_deletion_summary(record)
        if before_dispatch is not None:
            try:
                before_dispatch(operation_id)
            except Exception as exc:
                # The source fence is already active. Keep the operation and
                # expose a retryable failure instead of orphaning a permanent
                # deleting source behind an HTTP exception.
                self.fail(
                    operation_id,
                    str(exc),
                    error_type="server_error",
                )
                return build_deletion_summary(record)
        self._dispatch(record)
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
        """Return the authorized internal record for execution recovery."""

        with self._lock:
            return self._visible(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )

    def list_operations(
        self,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List retained operations, including empty aggregate targets."""

        with self._lock:
            visible = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.tenant_id == tenant_id
                    and record.created_by_user_id == created_by_user_id
                    and (
                        workspace_id is None
                        or record.target_kind in KNOWLEDGE_DELETION_TARGET_KINDS
                        or record.workspace_id == workspace_id
                    )
                ),
                key=lambda record: (record.created_at, record.operation_id),
                reverse=True,
            )
            page, next_cursor = keyset_page(
                visible,
                limit=limit,
                after=after,
                created_at_of=lambda record: record.created_at,
                id_of=lambda record: record.operation_id,
            )
            return [build_deletion_summary(record) for record in page], next_cursor

    def retry(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        work: DeletionWork | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        refuse_on_event_loop("retry")
        with self._lock:
            record = self._visible(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
            if record.status != DeletionOperationStatus.DELETE_FAILED:
                raise DeletionOperationConflict(operation_id)
            if work is not None:
                record.work = work
            if record.work is None:
                raise DeletionOperationConflict(operation_id)
            record.status = DeletionOperationStatus.QUEUED
            record.stage = DeletionStage.QUEUED
            record.error = None
            record.finished_at = None
            record.updated_at = time.time()
        if before_dispatch is not None:
            try:
                before_dispatch()
            except Exception as exc:
                self.fail(
                    operation_id,
                    str(exc),
                    error_type="server_error",
                )
                return build_deletion_summary(record)
        self._dispatch(record)
        return build_deletion_summary(record)

    def find_for_asset(
        self,
        asset_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any] | None:
        """Return the newest retained tombstone for an asset, if visible."""

        with self._lock:
            matches = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.created_by_user_id == created_by_user_id
                and (workspace_id is None or record.workspace_id == workspace_id)
                and any(item.asset_id == asset_id for item in record.manifest)
            ]
            if not matches:
                return None
            return build_deletion_summary(
                max(matches, key=lambda record: record.created_at)
            )

    def find_for_target(
        self,
        target_kind: DeletionTargetKind,
        target_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any] | None:
        """Return the newest retained receipt for an exact scoped target."""

        with self._lock:
            matches = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.created_by_user_id == created_by_user_id
                and (
                    record.target_kind in KNOWLEDGE_DELETION_TARGET_KINDS
                    or record.workspace_id == workspace_id
                )
                and record.target_kind == target_kind
                and record.target_id == target_id
            ]
            if not matches:
                return None
            return build_deletion_summary(
                max(matches, key=lambda record: record.created_at)
            )

    def has_collection_deletion(self, collection_id: str) -> bool:
        """Whether a retained vector-index operation fences this collection."""

        with self._lock:
            return any(
                (
                    record.target_kind == DeletionTargetKind.VECTOR_INDEX
                    and record.status != DeletionOperationStatus.DELETED
                    and record.vector_index_context is not None
                    and record.vector_index_context.server_collection_id
                    == collection_id
                )
                or (
                    record.target_kind == DeletionTargetKind.KNOWLEDGE_COLLECTION
                    and record.knowledge_context is not None
                    and record.knowledge_context.collection_id == collection_id
                    and (
                        record.status in ACTIVE_DELETION_STATUSES
                        or (
                            record.status == DeletionOperationStatus.DELETE_FAILED
                            and record.completed_items > 0
                        )
                    )
                )
                for record in self._records.values()
            )

    def has_document_deletion(self, document_id: str) -> bool:
        """Whether a knowledge-document operation currently hides one document."""

        with self._lock:
            return any(
                record.target_kind == DeletionTargetKind.KNOWLEDGE_DOCUMENT
                and record.knowledge_context is not None
                and record.knowledge_context.document_id == document_id
                and (
                    record.status in ACTIVE_DELETION_STATUSES
                    or (
                        record.status == DeletionOperationStatus.DELETE_FAILED
                        and record.completed_items > 0
                    )
                )
                for record in self._records.values()
            )

    def progress(
        self,
        operation_id: str,
        *,
        stage: DeletionStage,
        completed_items: int,
        total_items: int,
    ) -> bool:
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.status != DeletionOperationStatus.RUNNING:
                return False
            record.stage = stage
            record.completed_items = max(
                record.completed_items,
                0,
                completed_items,
            )
            record.total_items = max(record.completed_items, total_items)
            record.updated_at = time.time()
            return True

    def is_current(self, operation_id: str) -> bool:
        """Return whether the in-process attempt may continue mutating state."""

        with self._lock:
            record = self._records.get(operation_id)
            return bool(
                record is not None and record.status == DeletionOperationStatus.RUNNING
            )

    def checkpoint_source_cleanup(
        self,
        operation_id: str,
        *,
        asset_id: str,
        plan: dict[str, Any],
    ) -> bool:
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.status != DeletionOperationStatus.RUNNING:
                return False
            matched = False
            manifest: list[DeletionManifestItem] = []
            for item in record.manifest:
                if item.asset_id == asset_id:
                    item = replace(
                        item,
                        source_cleanup_plan=dict(plan),
                    )
                    matched = True
                manifest.append(item)
            if not matched:
                return False
            record.manifest = tuple(manifest)
            record.updated_at = time.time()
            return True

    def source_deletion_permit(self, operation_id: str, *, scope: Any) -> Any:
        return self._source_authority.get_deletion_permit(
            scope, operation_id=operation_id
        )

    def complete(self, operation_id: str) -> bool:
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.status != DeletionOperationStatus.RUNNING:
                return False
            if record.terminal_action is not None:
                record.terminal_action()
            from inqtrix.source_authority import SourceScope

            source_scopes = [
                SourceScope(
                    tenant_id=record.tenant_id,
                    source_id=item.source_id,
                    owner_user_id=record.created_by_user_id,
                    workspace_id=record.workspace_id,
                )
                for item in record.manifest
            ]
            target_source = target_source_id(record.target_kind, record.target_id)
            if target_source is not None:
                source_scopes.append(
                    SourceScope(
                        tenant_id=record.tenant_id,
                        source_id=target_source,
                        owner_user_id=record.created_by_user_id,
                        workspace_id=record.workspace_id,
                    )
                )
            permits = tuple(
                self._source_authority.get_deletion_permit(
                    scope, operation_id=operation_id
                )
                for scope in source_scopes
            )
            self._source_authority.complete_delete_many(permits)
            now = time.time()
            record.status = DeletionOperationStatus.DELETED
            record.stage = DeletionStage.DELETED
            record.completed_items = record.total_items
            record.updated_at = now
            record.finished_at = now
            record.error = None
            self._append_audit_locked(
                record,
                action="asset.delete_completed",
                outcome="success",
                detail={"stage": record.stage.value},
            )
            return True

    def fail(
        self,
        operation_id: str,
        message: str,
        *,
        error_type: str = "server_error",
    ) -> bool:
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.status not in ACTIVE_DELETION_STATUSES:
                return False
            now = time.time()
            record.status = DeletionOperationStatus.DELETE_FAILED
            record.stage = DeletionStage.DELETE_FAILED
            record.updated_at = now
            record.finished_at = now
            record.error = {"message": message, "type": error_type}
            self._append_audit_locked(
                record,
                action="asset.delete_failed",
                outcome="failure",
                detail={
                    "stage": record.stage.value,
                    "error_type": error_type,
                },
            )
            return True

    def close(self) -> None:
        return None

    def _visible(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> DeletionOperationRecord:
        record = self._records.get(operation_id)
        if (
            record is None
            or record.tenant_id != tenant_id
            or record.created_by_user_id != created_by_user_id
            or (
                workspace_id is not None
                and record.target_kind not in KNOWLEDGE_DELETION_TARGET_KINDS
                and record.workspace_id != workspace_id
            )
        ):
            raise DeletionOperationNotFound(operation_id)
        return record

    def _dispatch(self, record: DeletionOperationRecord) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(record.operation_id,),
            name=f"inqtrix-delete-{record.operation_id}",
            daemon=True,
        )
        thread.start()

    def _run(self, operation_id: str) -> None:
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.status != DeletionOperationStatus.QUEUED:
                return
            now = time.time()
            record.status = DeletionOperationStatus.RUNNING
            record.started_at = record.started_at or now
            record.updated_at = now
            record.attempt += 1
            work = record.work
        handle = DeletionJobHandle(self, operation_id)
        try:
            if work is None:
                raise RuntimeError("deletion operation has no executable work")
            work(handle)
            if not handle.terminal_landed:
                handle.complete()
        except Exception as exc:  # noqa: BLE001 - persist retryable failure
            handle.fail(
                str(exc),
                error_type=str(
                    getattr(exc, "error_type", None) or "server_error"
                ),
            )
