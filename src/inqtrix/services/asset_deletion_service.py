"""Aggregate deletion orchestration for file-library assets.

The asset row is the user's handle to several independently persisted
resources.  This service is the only place allowed to translate "delete this
file" into the ordered, idempotent cleanup of search evidence, vector-index
membership, knowledge revisions, blob/quota state, and project metadata.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.ports import FileNotFound
from inqtrix.execution_failures import RunExecutionFailure
from inqtrix.knowledge.source_cleanup import (
    SourceCleanupPlan,
    empty_source_cleanup_plan,
)
from inqtrix.knowledge.stores.ports import CollectionNotFound, DocumentNotFound
from inqtrix.project.asset_records_ports import (
    AssetNotFound,
    AssetRecord,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.vector_index_ports import VectorIndexNotFound
from inqtrix.quota.models import QuotaDimension, QuotaSubject, file_stock_key
from inqtrix.runs.deletion_operations import (
    DeletionAttemptSuperseded,
    DeletionJobHandle,
    DeletionManifestItem,
    DeletionOperationConflict,
    DeletionStage,
    DeletionTargetKind,
    KnowledgeDeletionContext,
    SessionDeletionContext,
    VectorIndexDeletionContext,
)
from inqtrix.source_authority import SourceDeletionPermit, SourceScope
from inqtrix.sync_bridge import run_coro_sync
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.runs.deletion_operations import DeletionOperationStore
    from inqtrix.services.asset_records_service import AssetRecordsService
    from inqtrix.services.file_service import FileService
    from inqtrix.services.knowledge_service import KnowledgeService
    from inqtrix.services.agent_sessions_service import AgentSessionsService
    from inqtrix.services.knowledge_sessions_service import KnowledgeSessionsService
    from inqtrix.services.quota_service import QuotaService
    from inqtrix.services.upload_operation_service import UploadOperationService
    from inqtrix.services.vector_index_service import VectorIndexService

log = logging.getLogger("inqtrix")

MAX_BULK_DELETE_ASSETS = 200


class KnowledgeDeletionDependencyUnavailable(RunExecutionFailure):
    """Knowledge cleanup is required but its lifecycle service is unavailable."""

    def __init__(self) -> None:
        super().__init__(
            "dependency_unavailable",
            ("The file is retained to preserve data: the knowledge deletion component is"
            " unavailable, so remaining search or vector data could not be ruled out or"
            " removed. Enable the knowledge component and retry deletion."),
        )


def asset_source_id(asset_id: str) -> str:
    """Stable source identity shared by upload, indexing, and deletion."""

    return f"asset:{asset_id}"


class AssetDeletionService:
    """Start, inspect, retry, and execute aggregate file deletions.

    Every async start and the async retry hand their registration to a
    worker thread: registering can wait for locks other requests on the
    same event loop hold, and the deletion store refuses a write on a
    thread that runs a loop (``refuse_on_event_loop``).
    """

    def __init__(
        self,
        *,
        assets: "AssetRecordsService",
        operation_store: "DeletionOperationStore",
        files: "FileService | None",
        knowledge: "KnowledgeService | None",
        vector_indexes: "VectorIndexService | None",
        indexing_jobs: Any | None = None,
        quota: "QuotaService | None" = None,
        uploads: "UploadOperationService | None" = None,
        audit: Any | None = None,
    ) -> None:
        self._assets = assets
        self._operation_store = operation_store
        self._files = files
        self._knowledge = knowledge
        self._vector_indexes = vector_indexes
        self._indexing_jobs = indexing_jobs
        self._quota = quota
        self._uploads = uploads
        self._audit = audit
        self._agent_sessions: AgentSessionsService | None = None
        self._knowledge_sessions: KnowledgeSessionsService | None = None
        self._agent_checkpointer: Any | None = None
        self._user_erasure: Any | None = None

    @property
    def operation_store(self) -> "DeletionOperationStore":
        return self._operation_store

    def bind_quota_service(self, quota: "QuotaService | None") -> None:
        """Attach the worker-owned loop-agnostic quota recorder."""

        self._quota = quota

    def bind_session_deletion(
        self,
        *,
        agent_sessions: "AgentSessionsService",
        knowledge_sessions: "KnowledgeSessionsService",
        agent_checkpointer: Any | None = None,
    ) -> None:
        """Attach the two durable session aggregates to the shared ledger."""

        self._agent_sessions = agent_sessions
        self._knowledge_sessions = knowledge_sessions
        self._agent_checkpointer = agent_checkpointer

    def bind_user_erasure(self, erasure: Any) -> None:
        """Attach the account erasure, which runs as one deletion operation."""

        self._user_erasure = erasure

    def with_operation_store(self, store: Any) -> "AssetDeletionService":
        """This service over another operation store.

        An account erasure registers its aggregate deletions through a
        store that does not dispatch them, because it runs them itself.
        """

        clone = copy.copy(self)
        clone._operation_store = store
        return clone

    async def start_user_erasure(
        self,
        user_id: uuid.UUID,
        *,
        principal: Principal,
        operation_id: str,
    ) -> dict[str, Any]:
        """Register and dispatch the erasure of one account.

        The operation belongs to the administrator (``principal``); its
        target is the account. ``operation_id`` is the id the account was
        already marked with when it was cut off.
        """

        if self._user_erasure is None:
            raise RuntimeError("account erasure is not wired")

        def _work(handle: DeletionJobHandle) -> None:
            self.execute(
                handle,
                manifest=(),
                target_kind=DeletionTargetKind.USER,
                target_id=str(user_id),
                principal=principal,
                visible_to=None,
                workspace_id=None,
            )

        return await asyncio.to_thread(
            self._operation_store.submit,
            target_kind=DeletionTargetKind.USER,
            target_id=str(user_id),
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=None,
            work=_work,
            total_items=2,
            operation_id=operation_id,
        )

    def assert_upload_allowed(
        self,
        asset_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
        section_id: str | None = None,
    ) -> None:
        """Reject resurrection of an id retained by a deletion receipt."""

        operation = self._operation_store.find_for_asset(
            asset_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if operation is not None:
            raise DeletionOperationConflict(operation["operation_id"])
        if section_id is not None:
            self.assert_target_allowed(
                DeletionTargetKind.SECTION,
                section_id,
                principal=principal,
                workspace_id=workspace_id,
            )

    def assert_target_allowed(
        self,
        target_kind: DeletionTargetKind,
        target_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
    ) -> None:
        """Reject recreation/mutation of a retained destructive target."""

        operation = self._operation_store.find_for_target(
            target_kind,
            target_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if operation is not None:
            raise DeletionOperationConflict(operation["operation_id"])

    async def start_asset(
        self,
        asset_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
        workspace_id: str | None,
    ) -> dict[str, Any]:
        try:
            asset = await self._assets.get_asset(asset_id, visible_to=visible_to)
        except AssetNotFound:
            # A caller-supplied id is not deletion authority.  In particular,
            # minting a source fence for an absent or invisible asset would
            # let that caller target another scope which happens to use the
            # same stable source id.  Keep absence and invisibility
            # non-disclosing and do not create a receipt or tombstone.
            raise AssetNotFound(asset_id) from None
        return await asyncio.to_thread(
            self._start,
            assets=(asset,),
            target_kind=DeletionTargetKind.ASSET,
            target_id=asset_id,
            principal=principal,
            visible_to=visible_to,
            workspace_id=workspace_id,
        )

    async def start_bulk(
        self,
        asset_ids: Iterable[str],
        *,
        principal: Principal,
        visible_to: "UserContext | None",
        workspace_id: str | None,
    ) -> dict[str, Any]:
        unique_ids = tuple(dict.fromkeys(str(item) for item in asset_ids if item))
        if not unique_ids:
            raise ValueError("asset_ids must contain at least one id")
        if len(unique_ids) > MAX_BULK_DELETE_ASSETS:
            raise ValueError(
                f"asset_ids may contain at most {MAX_BULK_DELETE_ASSETS} ids"
            )
        records = tuple(
            await self._assets.assets_by_ids(
                unique_ids,
                visible_to=visible_to,
                workspace_id=workspace_id,
            )
        )
        if len(records) != len(unique_ids):
            # Bulk deletion is atomic with respect to authorization: silently
            # dropping unknown/foreign ids would reveal which subset exists
            # and would let the caller believe a broader destructive request
            # was accepted than the server actually authorized.
            found_ids = {record.id for record in records}
            missing_id = next(
                asset_id
                for asset_id in unique_ids
                if asset_id not in found_ids
            )
            raise AssetNotFound(missing_id)
        return await asyncio.to_thread(
            self._start,
            assets=records,
            target_kind=DeletionTargetKind.BULK,
            target_id=(
                "bulk:"
                + hashlib.sha256("\0".join(sorted(unique_ids)).encode()).hexdigest()[
                    :32
                ]
            ),
            principal=principal,
            visible_to=visible_to,
            workspace_id=workspace_id,
        )

    async def start_section(
        self,
        section_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
        workspace_id: str | None,
    ) -> dict[str, Any]:
        records = tuple(
            await self._assets.assets_for_section(section_id, visible_to=visible_to)
        )
        return await asyncio.to_thread(
            self._start,
            assets=records,
            target_kind=DeletionTargetKind.SECTION,
            target_id=section_id,
            principal=principal,
            visible_to=visible_to,
            workspace_id=workspace_id,
        )

    async def start_group(
        self,
        group_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Delete only group metadata; member assets remain active and ungrouped."""

        retained = self._operation_store.find_for_target(
            DeletionTargetKind.GROUP,
            group_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if retained is not None:
            return retained
        group = await self._assets.get_group(
            group_id,
            visible_to=visible_to,
            request_workspace_id=workspace_id,
        )
        scope = ResourceScope.from_record(group)

        def _set_registered(_operation_id: str) -> None:
            run_coro_sync(
                self._assets.store.tombstone_group_id(group_id, scope=scope)
            )

        def _work(handle: DeletionJobHandle) -> None:
            record = self._operation_store.get_record(
                handle.operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=principal.user_id,
                workspace_id=group.workspace_id,
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=record.workspace_id,
            )

        def _delete_memory_group_at_terminal_commit() -> None:
            run_coro_sync(self._assets.store.delete_group(group_id, scope=scope))

        return await asyncio.to_thread(
            self._operation_store.submit,
            target_kind=DeletionTargetKind.GROUP,
            target_id=group.id,
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=group.workspace_id,
            work=_work,
            before_dispatch=_set_registered,
            terminal_action=_delete_memory_group_at_terminal_commit,
            total_items=1,
        )

    async def start_vector_index(
        self,
        index_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
        workspace_id: str | None,
        server_collection_id_hint: str | None = None,
    ) -> dict[str, Any]:
        """Start one durable index + backing-collection deletion.

        ``server_collection_id_hint`` recovers the last server-confirmed
        collection identity held by the UI when a prior terminal index-record
        autosave could not reach the server. The hint is accepted only when
        the durable index has no binding and normal collection authorization
        succeeds; a persisted binding always wins.
        """

        retained = self._operation_store.find_for_target(
            DeletionTargetKind.VECTOR_INDEX,
            index_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if retained is not None:
            return retained
        if self._vector_indexes is None:
            raise VectorIndexNotFound(index_id)
        index = await self._vector_indexes.require_owned_index(
            index_id,
            visible_to=visible_to,
            request_workspace_id=workspace_id,
        )
        hinted_collection_id = (server_collection_id_hint or "").strip() or None
        server_collection_id = index.server_collection_id or hinted_collection_id
        embedding_model = index.server_collection_model or index.model
        if server_collection_id and self._knowledge is not None:
            try:
                collection = await self._knowledge.knowledge.store.get_collection(
                    server_collection_id
                )
            except CollectionNotFound:
                # A previous interrupted cleanup may already have removed the
                # collection. The owned vector-index row plus its persisted
                # or client-retained identifier is sufficient to resume the
                # idempotent tail.
                pass
            else:
                access = await self._knowledge.collection_access(collection, visible_to)
                if access.mode.value == "shared":
                    raise CollectionNotFound(server_collection_id)
                embedding_model = collection.embedding_model
        context = VectorIndexDeletionContext(
            index_id=index.id,
            server_collection_id=server_collection_id,
            embedding_model=embedding_model,
        )
        scope = ResourceScope.from_record(index)

        def _set_registered(_operation_id: str) -> None:
            run_coro_sync(
                self._vector_indexes.set_deletion_state(index.id, scope=scope)
            )

        def _work(handle: DeletionJobHandle) -> None:
            record = self._operation_store.get_record(
                handle.operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=principal.user_id,
                workspace_id=workspace_id,
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=record.workspace_id,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
                session_context=record.session_context,
            )

        return await asyncio.to_thread(
            self._operation_store.submit,
            target_kind=DeletionTargetKind.VECTOR_INDEX,
            target_id=index.id,
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=index.workspace_id,
            work=_work,
            before_dispatch=_set_registered,
            vector_index_context=context,
            total_items=4,
        )

    async def start_knowledge_collection(
        self,
        collection_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
    ) -> dict[str, Any]:
        """Start one owner-only collection deletion through the shared ledger."""

        retained = self._operation_store.find_for_target(
            DeletionTargetKind.KNOWLEDGE_COLLECTION,
            collection_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=None,
        )
        if retained is not None:
            return retained
        if self._knowledge is None:
            raise CollectionNotFound(collection_id)
        collection = await self._knowledge.knowledge.store.get_collection(collection_id)
        access = await self._knowledge.collection_access(collection, visible_to)
        if access.mode.value == "shared":
            raise CollectionNotFound(collection_id)
        context = KnowledgeDeletionContext(
            target_kind=DeletionTargetKind.KNOWLEDGE_COLLECTION,
            collection_id=collection.id,
            embedding_model=collection.embedding_model,
        )
        return await asyncio.to_thread(
            self._start_knowledge,
            context=context,
            principal=principal,
            visible_to=visible_to,
        )

    async def start_knowledge_document(
        self,
        document_id: str,
        *,
        principal: Principal,
        visible_to: "UserContext | None",
    ) -> dict[str, Any]:
        """Start one edit-authorized document deletion through the shared ledger."""

        retained = self._operation_store.find_for_target(
            DeletionTargetKind.KNOWLEDGE_DOCUMENT,
            document_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=None,
        )
        if retained is not None:
            return retained
        if self._knowledge is None:
            raise DocumentNotFound(document_id)
        document, collection = await self._knowledge.prepare_document_deletion(
            document_id,
            visible_to=visible_to,
        )
        context = KnowledgeDeletionContext(
            target_kind=DeletionTargetKind.KNOWLEDGE_DOCUMENT,
            collection_id=collection.id,
            document_id=document.id,
            embedding_model=collection.embedding_model,
        )
        return await asyncio.to_thread(
            self._start_knowledge,
            context=context,
            principal=principal,
            visible_to=visible_to,
        )

    async def start_agent_session(
        self,
        session_id: str,
        *,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Start one owner-scoped Agent Desk aggregate deletion."""

        if self._agent_sessions is None:
            raise RuntimeError("agent-session deletion is not wired")
        retained = self._operation_store.find_for_target(
            DeletionTargetKind.AGENT_SESSION,
            session_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if retained is not None:
            return retained
        session = await self._agent_sessions.prepare_deletion(
            session_id,
            visible_to=visible_to,
            request_workspace_id=workspace_id,
        )
        return await asyncio.to_thread(
            self._start_session,
            context=SessionDeletionContext(
                target_kind=DeletionTargetKind.AGENT_SESSION,
                session_id=session.id,
            ),
            session=session,
            principal=principal,
            visible_to=visible_to,
        )

    async def start_knowledge_session(
        self,
        session_id: str,
        *,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Start one owner-scoped Knowledge Desk session deletion."""

        if self._knowledge_sessions is None:
            raise RuntimeError("knowledge-session deletion is not wired")
        retained = self._operation_store.find_for_target(
            DeletionTargetKind.KNOWLEDGE_SESSION,
            session_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if retained is not None:
            return retained
        session = await self._knowledge_sessions.prepare_deletion(
            session_id,
            visible_to=visible_to,
            request_workspace_id=workspace_id,
        )
        return await asyncio.to_thread(
            self._start_session,
            context=SessionDeletionContext(
                target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
                session_id=session.id,
            ),
            session=session,
            principal=principal,
            visible_to=visible_to,
        )

    async def start_orphaned_source(
        self,
        source_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Settle one of the principal's sources that outlived its aggregate.

        Its asset, section or document is already gone, so the lifecycle row
        is the only map to what the Knowledge store may still hold for it.
        An account erasure starts this for the account's own sources.
        """
        visible_to = UserContext(
            principal=principal,
            workspace_ids=(workspace_id,) if workspace_id else (),
        )

        def _work(handle: DeletionJobHandle) -> None:
            self.execute(
                handle,
                manifest=(),
                target_kind=DeletionTargetKind.SOURCE,
                target_id=source_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            )

        return await asyncio.to_thread(
            self._operation_store.submit,
            target_kind=DeletionTargetKind.SOURCE,
            target_id=source_id,
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
            work=_work,
            total_items=2,
        )

    def _start_session(
        self,
        *,
        context: SessionDeletionContext,
        session: Any,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        scope = ResourceScope.from_record(session)
        session_service = (
            self._agent_sessions
            if context.target_kind == DeletionTargetKind.AGENT_SESSION
            else self._knowledge_sessions
        )
        if session_service is None:
            raise RuntimeError("session deletion service is not wired")

        def _set_registered(operation_id: str) -> None:
            run_coro_sync(
                session_service.mark_deletion_state(
                    session,
                    lifecycle_status="deleting",
                    operation_id=operation_id,
                    stage=DeletionStage.QUEUED.value,
                    error=None,
                )
            )

        def _work(handle: DeletionJobHandle) -> None:
            record = self._operation_store.get_record(
                handle.operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=principal.user_id,
                workspace_id=scope.workspace_id,
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=record.workspace_id,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
                session_context=record.session_context,
            )

        return self._operation_store.submit(
            target_kind=context.target_kind,
            target_id=context.session_id,
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=scope.workspace_id,
            work=_work,
            before_dispatch=_set_registered,
            session_context=context,
            total_items=2,
        )

    def _start_knowledge(
        self,
        *,
        context: KnowledgeDeletionContext,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        """Register one immutable knowledge target and its immediate fence."""

        target_id = context.document_id or context.collection_id

        def _set_registered(_operation_id: str) -> None:
            if context.document_id and self._knowledge is not None:
                run_coro_sync(
                    self._knowledge.mark_document_deleting_for_aggregate(
                        context.document_id
                    )
                )

        def _work(handle: DeletionJobHandle) -> None:
            record = self._operation_store.get_record(
                handle.operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=principal.user_id,
                workspace_id=None,
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=None,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
            )

        return self._operation_store.submit(
            target_kind=context.target_kind,
            target_id=target_id,
            manifest=(),
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=None,
            work=_work,
            before_dispatch=_set_registered,
            knowledge_context=context,
            total_items=3,
        )

    def get(
        self,
        operation_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        return self._operation_store.get(
            operation_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )

    def list_operations(
        self,
        *,
        principal: Principal,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self._operation_store.list_operations(
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
            limit=limit,
            after=after,
        )

    async def retry(
        self,
        operation_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Reopen a failed deletion; the reopening runs on a worker thread."""
        return await asyncio.to_thread(
            self._retry,
            operation_id,
            principal=principal,
            workspace_id=workspace_id,
        )

    def _retry(
        self,
        operation_id: str,
        *,
        principal: Principal,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        record = self._operation_store.get_record(
            operation_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        persisted_workspace_id = record.workspace_id

        def _work(handle: DeletionJobHandle) -> None:
            visible_to = UserContext(
                principal=principal,
                workspace_ids=(
                    (persisted_workspace_id,) if persisted_workspace_id else ()
                ),
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=persisted_workspace_id,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
                session_context=record.session_context,
            )

        def _restore_document_tombstone() -> None:
            context = record.knowledge_context
            if (
                record.completed_items == 0
                and context is not None
                and context.target_kind == DeletionTargetKind.KNOWLEDGE_DOCUMENT
                and context.document_id is not None
                and self._knowledge is not None
            ):
                run_coro_sync(
                    self._knowledge.mark_document_deleting_for_aggregate(
                        context.document_id
                    )
                )

        return self._operation_store.retry(
            operation_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=persisted_workspace_id,
            work=_work,
            before_dispatch=_restore_document_tombstone,
        )

    def _start(
        self,
        *,
        assets: tuple[AssetRecord, ...],
        target_kind: DeletionTargetKind,
        target_id: str,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        for asset in assets:
            if asset.workspace_id != workspace_id and workspace_id is not None:
                raise AssetNotFound(asset.id)
            if asset.lifecycle_status in {"deleting", "delete_failed"}:
                if len(assets) == 1 and asset.deletion_operation_id:
                    return self.get(
                        asset.deletion_operation_id,
                        principal=principal,
                        workspace_id=workspace_id,
                    )
                raise DeletionOperationConflict(asset.id)

        manifest = tuple(
            DeletionManifestItem(
                asset_id=asset.id,
                source_id=asset_source_id(asset.id),
                server_file_id=asset.server_file_id,
                size_bytes=asset.size_bytes,
                upload_operation_id=asset.upload_operation_id,
                file_owner_user_id=asset.created_by_user_id,
            )
            for asset in assets
        )
        return self._start_manifest(
            manifest=manifest,
            target_kind=target_kind,
            target_id=target_id,
            principal=principal,
            visible_to=visible_to,
            workspace_id=workspace_id,
        )

    def _start_manifest(
        self,
        *,
        manifest: tuple[DeletionManifestItem, ...],
        target_kind: DeletionTargetKind,
        target_id: str,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        scope = ResourceScope(
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        effective_manifest = manifest

        def _refresh_manifest() -> tuple[DeletionManifestItem, ...]:
            """Re-read mutable blob facts after the source fence has landed."""

            nonlocal effective_manifest
            refreshed: list[DeletionManifestItem] = []
            for item in manifest:
                try:
                    asset = run_coro_sync(
                        self._assets.get_asset(
                            item.asset_id,
                            visible_to=visible_to,
                        )
                    )
                except AssetNotFound:
                    refreshed.append(
                        DeletionManifestItem(
                            asset_id=item.asset_id,
                            source_id=item.source_id,
                            server_file_id=item.server_file_id,
                            size_bytes=item.size_bytes,
                            source_cleanup_plan=item.source_cleanup_plan,
                            upload_operation_id=item.upload_operation_id,
                            file_owner_user_id=item.file_owner_user_id,
                        )
                    )
                    continue
                upload_operation_id = (
                    asset.upload_operation_id or item.upload_operation_id
                )
                pending_file = (
                    self._uploads.prepared_file_for_deletion(
                        upload_operation_id,
                        tenant_id=principal.tenant_id,
                        asset_id=item.asset_id,
                    )
                    if self._uploads is not None and upload_operation_id is not None
                    else None
                )
                refreshed.append(
                    DeletionManifestItem(
                        asset_id=item.asset_id,
                        source_id=item.source_id,
                        server_file_id=(
                            asset.server_file_id
                            or (
                                pending_file.id
                                if pending_file is not None
                                else item.server_file_id
                            )
                        ),
                        size_bytes=(
                            asset.size_bytes
                            if asset.server_file_id is not None
                            else (
                                pending_file.size_bytes
                                if pending_file is not None
                                else asset.size_bytes
                            )
                        ),
                        source_cleanup_plan=item.source_cleanup_plan,
                        upload_operation_id=upload_operation_id,
                        file_owner_user_id=(
                            asset.created_by_user_id or item.file_owner_user_id
                        ),
                    )
                )
            effective_manifest = tuple(refreshed)
            return effective_manifest

        def _set_registered(operation_id: str) -> None:
            if target_kind == DeletionTargetKind.SECTION:
                run_coro_sync(
                    self._assets.store.tombstone_section_id(
                        target_id,
                        scope=scope,
                    )
                )
            for item in effective_manifest:
                try:
                    run_coro_sync(
                        self._assets.store.set_asset_deletion_state(
                            item.asset_id,
                            scope=scope,
                            lifecycle_status="deleting",
                            deletion_operation_id=operation_id,
                            deletion_stage=DeletionStage.QUEUED.value,
                            deletion_error=None,
                        )
                    )
                except AssetNotFound:
                    run_coro_sync(
                        self._assets.store.tombstone_asset_id(
                            item.asset_id,
                            scope=scope,
                        )
                    )
                permit = self._operation_store.source_deletion_permit(
                    operation_id,
                    scope=SourceScope(
                        tenant_id=principal.tenant_id,
                        source_id=item.source_id,
                        owner_user_id=principal.user_id,
                        workspace_id=workspace_id,
                    ),
                )
                self._knowledge_mark_deleting(
                    item.source_id,
                    visible_to,
                    principal=principal,
                    deletion_permit=permit,
                )

        def _work(handle: DeletionJobHandle) -> None:
            record = self._operation_store.get_record(
                handle.operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=principal.user_id,
                workspace_id=workspace_id,
            )
            self.execute(
                handle,
                manifest=record.manifest,
                target_kind=record.target_kind,
                target_id=record.target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=record.workspace_id,
                vector_index_context=record.vector_index_context,
                knowledge_context=record.knowledge_context,
            )

        return self._operation_store.submit(
            target_kind=target_kind,
            target_id=target_id,
            manifest=manifest,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
            work=_work,
            before_dispatch=_set_registered,
            refresh_manifest=_refresh_manifest,
        )

    def execute(
        self,
        handle: DeletionJobHandle,
        *,
        manifest: tuple[DeletionManifestItem, ...],
        target_kind: DeletionTargetKind,
        target_id: str,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
        vector_index_context: VectorIndexDeletionContext | None = None,
        knowledge_context: KnowledgeDeletionContext | None = None,
        session_context: SessionDeletionContext | None = None,
    ) -> None:
        """Run one idempotent attempt from its persisted manifest."""

        if target_kind == DeletionTargetKind.USER:
            if self._user_erasure is None:
                raise RuntimeError("account erasure is not wired")
            self._user_erasure.execute(
                handle,
                tenant_id=principal.tenant_id,
                user_id=uuid.UUID(target_id),
                actor_user_id=principal.user_id,
            )
            return

        if target_kind == DeletionTargetKind.SOURCE:
            self._execute_source(
                handle,
                source_id=target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            )
            return

        if target_kind == DeletionTargetKind.GROUP:
            self._execute_group(
                handle,
                group_id=target_id,
                principal=principal,
                workspace_id=workspace_id,
            )
            return

        if target_kind == DeletionTargetKind.VECTOR_INDEX:
            if vector_index_context is None:
                raise RuntimeError(
                    "vector-index deletion lost its immutable recovery context"
                )
            self._execute_vector_index(
                handle,
                context=vector_index_context,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            )
            return

        if target_kind in {
            DeletionTargetKind.AGENT_SESSION,
            DeletionTargetKind.KNOWLEDGE_SESSION,
        }:
            if session_context is None or session_context.target_kind != target_kind:
                raise RuntimeError(
                    "session deletion lost its immutable recovery context"
                )
            self._execute_session(
                handle,
                context=session_context,
                principal=principal,
                workspace_id=workspace_id,
            )
            return

        if target_kind in {
            DeletionTargetKind.KNOWLEDGE_COLLECTION,
            DeletionTargetKind.KNOWLEDGE_DOCUMENT,
        }:
            if (
                knowledge_context is None
                or knowledge_context.target_kind != target_kind
            ):
                raise RuntimeError(
                    "knowledge deletion lost its immutable recovery context"
                )
            self._execute_knowledge(
                handle,
                context=knowledge_context,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            )
            return

        scope = ResourceScope(
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        total = len(manifest)
        source_state: dict[
            str, tuple[SourceDeletionPermit, SourceCleanupPlan | None]
        ] = {}
        try:
            for item in manifest:
                handle.assert_current()
                source_scope = SourceScope(
                    tenant_id=principal.tenant_id,
                    source_id=item.source_id,
                    owner_user_id=principal.user_id,
                    workspace_id=workspace_id,
                )
                permit = handle.source_deletion_permit(source_scope)
                if permit is None:
                    raise DeletionAttemptSuperseded(handle.operation_id)
                cleanup_plan = (
                    SourceCleanupPlan.from_dict(item.source_cleanup_plan)
                    if item.source_cleanup_plan is not None
                    else None
                )
                if (
                    cleanup_plan is None
                    and self._knowledge is None
                    and self._can_prove_empty_knowledge_cleanup(
                        item.asset_id,
                        scope=scope,
                    )
                ):
                    cleanup_plan = empty_source_cleanup_plan(permit)
                if cleanup_plan is not None:
                    cleanup_plan.assert_permit(permit)
                self._require_knowledge_cleanup_dependency(cleanup_plan)
                self._knowledge_mark_deleting(
                    item.source_id,
                    visible_to,
                    principal=principal,
                    deletion_permit=permit,
                )
                if cleanup_plan is None:
                    cleanup_plan = self._knowledge_prepare_cleanup(
                        item.source_id,
                        deletion_permit=permit,
                    )
                if item.source_cleanup_plan is None:
                    handle.checkpoint_source_cleanup(
                        item.asset_id, cleanup_plan.as_dict()
                    )
                source_state[item.source_id] = (permit, cleanup_plan)
            self._progress_assets(
                handle,
                manifest,
                scope,
                DeletionStage.SEARCH_DETACHED,
                completed=0,
                total=total,
            )

            if self._vector_indexes is not None:
                for item in manifest:
                    handle.assert_current()
                    run_coro_sync(
                        self._vector_indexes.remove_asset_memberships(
                            item.asset_id, scope=scope
                        )
                    )
            self._progress_assets(
                handle,
                manifest,
                scope,
                DeletionStage.VECTORS_REMOVED,
                completed=0,
                total=total,
            )

            for item in manifest:
                handle.assert_current()
                permit, cleanup_plan = source_state[item.source_id]
                self._knowledge_delete_source(
                    item.source_id,
                    visible_to,
                    principal=principal,
                    deletion_permit=permit,
                    cleanup_plan=cleanup_plan,
                )
            self._progress_assets(
                handle,
                manifest,
                scope,
                DeletionStage.KNOWLEDGE_REMOVED,
                completed=0,
                total=total,
            )

            file_groups: dict[str, list[DeletionManifestItem]] = {}
            unbound_count = 0
            for item in manifest:
                if item.server_file_id:
                    file_groups.setdefault(item.server_file_id, []).append(item)
                else:
                    unbound_count += 1

            # Transitional integrity guard for legacy rows. A physical file
            # may not be removed while any asset outside this operation still
            # references it. Validate every group before deleting the first
            # blob so a bad historical binding cannot produce partial damage.
            manifest_ids = {item.asset_id for item in manifest}
            for server_file_id in file_groups:
                references = run_coro_sync(
                    self._assets.list_assets_by_server_file_id(server_file_id)
                )
                unexpected = [
                    asset.id for asset in references if asset.id not in manifest_ids
                ]
                if unexpected:
                    raise RuntimeError(
                        ("The original file is still bound to another asset; automatic "
                        "deletion was halted to preserve data")
                    )

            completed = unbound_count
            if completed:
                self._progress_assets(
                    handle,
                    manifest,
                    scope,
                    DeletionStage.BLOBS_REMOVED,
                    completed=completed,
                    total=total,
                )
            for server_file_id, items in file_groups.items():
                handle.assert_current()
                if self._files is None:
                    raise RuntimeError(
                        "File storage is unavailable for the required blob deletion"
                    )
                for item in items:
                    handle.assert_current()
                    try:
                        run_coro_sync(
                            self._assets.store.detach_server_file_for_deletion(
                                item.asset_id,
                                scope=scope,
                                operation_id=handle.operation_id,
                                expected_server_file_id=server_file_id,
                            )
                        )
                    except AssetNotFound:
                        # A prior attempt may have removed metadata after all
                        # external residuals were already clear. The immutable
                        # manifest remains sufficient to finish idempotently.
                        pass
                remaining_references = run_coro_sync(
                    self._assets.list_assets_by_server_file_id(server_file_id)
                )
                if remaining_references:
                    raise RuntimeError(
                        ("The original file remains referenced after releasing the asset"
                        " binding")
                    )
                handle.assert_current()
                try:
                    deleted = run_coro_sync(
                        self._files.delete(server_file_id, principal=principal)
                    )
                except FileNotFound:
                    deleted = run_coro_sync(
                        self._files.discard_file_lifecycle(
                            server_file_id,
                            tenant_id=principal.tenant_id,
                        )
                    )
                if self._quota is not None:
                    owner_user_id = (
                        deleted.owner_user_id
                        if deleted is not None
                        else next(
                            (
                                item.file_owner_user_id
                                for item in items
                                if item.file_owner_user_id is not None
                            ),
                            principal.user_id,
                        )
                    )
                    subject = (
                        QuotaSubject(
                            tenant_id=(
                                deleted.tenant_id
                                if deleted is not None
                                else principal.tenant_id
                            ),
                            user_id=owner_user_id,
                        )
                        if owner_user_id is not None
                        else None
                    )
                    if subject is not None:
                        stock = self._quota.tombstone_stock_blocking(
                            subject,
                            QuotaDimension.STORED_BYTES,
                            stock_key=file_stock_key(server_file_id),
                        )
                        if stock.amount != 0 or not stock.tombstoned:
                            raise RuntimeError(
                                ("File storage quota has not yet been tombstoned and "
                                "cannot be finalized")
                            )
                completed += len(items)
                self._progress_assets(
                    handle,
                    manifest,
                    scope,
                    DeletionStage.BLOBS_REMOVED,
                    completed=completed,
                    total=total,
                )

            self._verify_external_residuals(
                manifest,
                principal=principal,
                visible_to=visible_to,
                scope=scope,
                source_state=source_state,
            )

            for item in manifest:
                handle.assert_current()
                try:
                    run_coro_sync(
                        self._assets.store.delete_asset(item.asset_id, scope=scope)
                    )
                except AssetNotFound:
                    pass
            if target_kind == DeletionTargetKind.SECTION:
                handle.assert_current()
                try:
                    run_coro_sync(
                        self._assets.store.delete_section(target_id, scope=scope)
                    )
                except Exception as exc:
                    # A missing section is already the desired terminal state;
                    # other store failures must remain visible and retryable.
                    if exc.__class__.__name__ != "SectionNotFound":
                        raise
            handle.progress(
                DeletionStage.METADATA_REMOVED,
                completed_items=total,
                total_items=total,
            )

            self._verify_metadata_absent(manifest)
            handle.progress(
                DeletionStage.RESIDUALS_VERIFIED,
                completed_items=total,
                total_items=total,
            )
            handle.complete()
        except DeletionAttemptSuperseded:
            raise
        except Exception as exc:
            message = sanitize_error(exc)
            if not handle.manages_asset_lifecycle:
                self._mark_failed_assets(
                    manifest,
                    scope=scope,
                    operation_id=handle.operation_id,
                    message=message,
                )
            log.warning(
                "Asset deletion operation %s failed (error_type=%s)",
                handle.operation_id,
                type(exc).__name__,
            )
            raise

    def _execute_source(
        self,
        handle: DeletionJobHandle,
        *,
        source_id: str,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> None:
        """Clean up an orphaned source by its exact plan and verify it.

        Registration fenced the source; the terminal write keeps its
        tombstone. Without the Knowledge service nothing proves the source
        is empty, so the attempt fails instead of guessing.
        """
        permit = handle.source_deletion_permit(
            SourceScope(
                tenant_id=principal.tenant_id,
                source_id=source_id,
                owner_user_id=principal.user_id,
                workspace_id=workspace_id,
            )
        )
        if permit is None:
            raise DeletionAttemptSuperseded(handle.operation_id)
        self._knowledge_mark_deleting(
            source_id, visible_to, principal=principal, deletion_permit=permit
        )
        cleanup_plan = self._knowledge_prepare_cleanup(
            source_id, deletion_permit=permit
        )
        self._knowledge_delete_source(
            source_id,
            visible_to,
            principal=principal,
            deletion_permit=permit,
            cleanup_plan=cleanup_plan,
        )
        handle.progress(
            DeletionStage.KNOWLEDGE_REMOVED, completed_items=1, total_items=2
        )
        residuals = self._knowledge_residual_count(
            source_id, visible_to, deletion_permit=permit, cleanup_plan=cleanup_plan
        )
        if residuals:
            raise RuntimeError(
                "Deletion verification found remaining resources: "
                f"{source_id}:knowledge_records={residuals}"
            )
        handle.progress(
            DeletionStage.RESIDUALS_VERIFIED, completed_items=2, total_items=2
        )
        handle.complete()

    def _execute_group(
        self,
        handle: DeletionJobHandle,
        *,
        group_id: str,
        principal: Principal,
        workspace_id: str | None,
    ) -> None:
        """Commit group removal through the operation store's terminal boundary."""

        del group_id, principal, workspace_id
        handle.assert_current()
        handle.complete()

    def _execute_session(
        self,
        handle: DeletionJobHandle,
        *,
        context: SessionDeletionContext,
        principal: Principal,
        workspace_id: str | None,
    ) -> None:
        """Delete session dependants while retaining the UI tombstone.

        The durable operation store removes the session registry row in the
        same transaction that marks this operation complete.  Keeping that
        final step out of the worker prevents a crash from making an
        unfinished deletion disappear from the user's history.
        """

        scope = ResourceScope(
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        handle.assert_current()
        if context.target_kind == DeletionTargetKind.AGENT_SESSION:
            if self._agent_sessions is None:
                raise RuntimeError("agent-session deletion service is unavailable")
            self._agent_sessions.delete_run_aggregate(
                context.session_id,
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                workspace_id=workspace_id,
                run_ids=context.run_ids,
                checkpointer=self._agent_checkpointer,
            )
            handle.progress(
                DeletionStage.SESSION_DATA_REMOVED,
                completed_items=1,
                total_items=2,
            )
            handle.assert_current()
            residuals = run_coro_sync(
                self._agent_sessions.deletion_residuals(
                    context.session_id,
                    tenant_id=principal.tenant_id,
                    owner_user_id=principal.user_id,
                    workspace_id=workspace_id,
                    run_ids=context.run_ids,
                    scope=scope,
                )
            )
            if any(int(value) for value in residuals.values()):
                raise RuntimeError("agent session still has dependent data")
            total_items = 2
        else:
            if self._knowledge_sessions is None:
                raise RuntimeError(
                    "knowledge-session deletion service is unavailable"
                )
            self._knowledge_sessions.delete_run_aggregate(
                context.session_id,
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                workspace_id=workspace_id,
                run_ids=context.run_ids,
            )
            handle.progress(
                DeletionStage.SESSION_DATA_REMOVED,
                completed_items=1,
                total_items=2,
            )
            handle.assert_current()
            residuals = run_coro_sync(
                self._knowledge_sessions.deletion_residuals(
                    context.session_id,
                    tenant_id=principal.tenant_id,
                    owner_user_id=principal.user_id,
                    workspace_id=workspace_id,
                    run_ids=context.run_ids,
                    scope=scope,
                )
            )
            if any(int(value) for value in residuals.values()):
                raise RuntimeError("knowledge session still has dependent data")
            total_items = 2

        handle.progress(
            DeletionStage.RESIDUALS_VERIFIED,
            completed_items=total_items,
            total_items=total_items,
        )
        handle.complete()

    def _execute_vector_index(
        self,
        handle: DeletionJobHandle,
        *,
        context: VectorIndexDeletionContext,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> None:
        """Converge a vector-index aggregate from its durable identifiers."""

        if self._vector_indexes is None:
            raise RuntimeError("vector-index deletion service is unavailable")
        scope = ResourceScope(
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        record = self._operation_store.get_record(
            handle.operation_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        backing_collection_exists = bool(context.server_collection_id)
        if context.server_collection_id and self._knowledge is not None:
            try:
                run_coro_sync(
                    self._knowledge.knowledge.store.get_collection(
                        context.server_collection_id
                    )
                )
            except CollectionNotFound:
                backing_collection_exists = False
        try:
            handle.assert_current()
            try:
                run_coro_sync(
                    self._vector_indexes.set_deletion_state(
                        context.index_id,
                        scope=scope,
                    )
                )
            except VectorIndexNotFound:
                pass
            handle.progress(
                DeletionStage.VECTOR_INDEX_DETACHED,
                completed_items=1,
                total_items=4,
            )

            handle.assert_current()
            if (
                record.completed_items < 2
                and context.server_collection_id
                and backing_collection_exists
                and self._indexing_jobs is not None
            ):
                self._indexing_jobs.fence_collection_for_deletion(
                    context.server_collection_id,
                    actor_user_id=principal.user_id,
                )
                if self._indexing_jobs.has_active_job(context.server_collection_id):
                    raise RuntimeError(
                        "indexing job remained active after deletion fence"
                    )
            handle.progress(
                DeletionStage.INDEXING_CANCELLED,
                completed_items=2,
                total_items=4,
            )

            handle.assert_current()
            if context.server_collection_id and self._knowledge is not None:
                run_coro_sync(
                    self._knowledge.delete_collection_for_aggregate(
                        context.server_collection_id,
                        visible_to=visible_to,
                    )
                )
                residuals = run_coro_sync(
                    self._knowledge.collection_residuals(
                        context.server_collection_id,
                        embedding_model=context.embedding_model,
                    )
                )
                if any(int(value) for value in residuals.values()):
                    raise RuntimeError(
                        "backing knowledge collection still has residual data"
                    )
            handle.progress(
                DeletionStage.KNOWLEDGE_REMOVED,
                completed_items=3,
                total_items=4,
            )

            handle.assert_current()
            run_coro_sync(
                self._vector_indexes.delete_index_idempotent(
                    context.index_id,
                    scope=scope,
                )
            )
            handle.progress(
                DeletionStage.METADATA_REMOVED,
                completed_items=4,
                total_items=4,
            )
            if run_coro_sync(
                self._vector_indexes.count_index(context.index_id, scope=scope)
            ):
                raise RuntimeError("vector-index record remained after deletion")
            if (
                context.server_collection_id
                and self._indexing_jobs is not None
                and self._indexing_jobs.has_active_job(context.server_collection_id)
            ):
                raise RuntimeError("indexing job reappeared after collection deletion")
            handle.progress(
                DeletionStage.RESIDUALS_VERIFIED,
                completed_items=4,
                total_items=4,
            )
            handle.complete()
        except DeletionAttemptSuperseded:
            raise
        except Exception as exc:
            message = sanitize_error(exc)
            try:
                run_coro_sync(
                    self._vector_indexes.set_deletion_state(
                        context.index_id,
                        scope=scope,
                        failed_error=message,
                    )
                )
            except VectorIndexNotFound:
                pass
            log.warning(
                "Vector index deletion operation %s failed (error_type=%s)",
                handle.operation_id,
                type(exc).__name__,
            )
            raise

    def _execute_knowledge(
        self,
        handle: DeletionJobHandle,
        *,
        context: KnowledgeDeletionContext,
        principal: Principal,
        visible_to: UserContext | None,
        workspace_id: str | None,
    ) -> None:
        """Converge a collection/document deletion from persisted authority."""

        del workspace_id  # Knowledge collections are intentionally cross-workspace.
        if self._knowledge is None:
            raise RuntimeError("knowledge deletion service is unavailable")
        record = self._operation_store.get_record(
            handle.operation_id,
            tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id,
            workspace_id=None,
        )
        try:
            handle.assert_current()
            if record.completed_items == 0:
                try:
                    run_coro_sync(
                        self._knowledge.authorize_knowledge_deletion(
                            context,
                            visible_to=visible_to,
                        )
                    )
                except Exception:
                    # Submission hid the document atomically. If current ACLs
                    # no longer authorize the first destructive step, restore
                    # that preflight-only tombstone before publishing failure.
                    if context.document_id:
                        run_coro_sync(
                            self._knowledge.restore_document_after_deletion_preflight(
                                context.document_id
                            )
                        )
                    raise
            handle.progress(
                DeletionStage.SEARCH_DETACHED,
                completed_items=max(1, record.completed_items),
                total_items=3,
            )

            handle.assert_current()
            if record.completed_items < 2 and self._indexing_jobs is not None:
                if context.target_kind == DeletionTargetKind.KNOWLEDGE_COLLECTION:
                    self._indexing_jobs.fence_collection_for_deletion(
                        context.collection_id,
                        actor_user_id=principal.user_id,
                    )
                    indexing_active = self._indexing_jobs.has_active_job(
                        context.collection_id
                    )
                else:
                    if context.document_id is None:
                        raise RuntimeError(
                            "knowledge-document deletion has no document id"
                        )
                    self._indexing_jobs.fence_document_for_deletion(
                        context.collection_id,
                        context.document_id,
                        actor_user_id=principal.user_id,
                    )
                    indexing_active = self._indexing_jobs.has_active_document_job(
                        context.document_id
                    )
                if indexing_active:
                    raise RuntimeError(
                        "indexing job remained active after deletion fence"
                    )
            handle.progress(
                DeletionStage.INDEXING_CANCELLED,
                completed_items=2,
                total_items=3,
            )

            handle.assert_current()
            if context.target_kind == DeletionTargetKind.KNOWLEDGE_COLLECTION:
                run_coro_sync(
                    self._knowledge.delete_collection_for_aggregate(
                        context.collection_id,
                        visible_to=visible_to,
                    )
                )
                residuals = run_coro_sync(
                    self._knowledge.collection_residuals(
                        context.collection_id,
                        embedding_model=context.embedding_model,
                    )
                )
            else:
                if context.document_id is None:
                    raise RuntimeError("knowledge-document deletion has no document id")
                run_coro_sync(
                    self._knowledge.delete_document_for_aggregate(
                        context.document_id,
                        visible_to=visible_to,
                    )
                )
                residuals = run_coro_sync(
                    self._knowledge.document_residuals(
                        context.document_id,
                        embedding_model=context.embedding_model,
                    )
                )
            if any(int(value) for value in residuals.values()):
                raise RuntimeError(
                    "knowledge deletion still has canonical or vector residuals"
                )
            handle.progress(
                DeletionStage.KNOWLEDGE_REMOVED,
                completed_items=3,
                total_items=3,
            )
            if self._indexing_jobs is not None:
                if context.target_kind == DeletionTargetKind.KNOWLEDGE_COLLECTION:
                    indexing_active = self._indexing_jobs.has_active_job(
                        context.collection_id
                    )
                else:
                    assert context.document_id is not None
                    indexing_active = self._indexing_jobs.has_active_document_job(
                        context.document_id
                    )
                if indexing_active:
                    raise RuntimeError(
                        "indexing job reappeared after knowledge deletion"
                    )
            handle.progress(
                DeletionStage.RESIDUALS_VERIFIED,
                completed_items=3,
                total_items=3,
            )
            handle.complete()
        except DeletionAttemptSuperseded:
            raise
        except Exception as exc:
            log.warning(
                "Knowledge deletion operation %s failed (error_type=%s)",
                handle.operation_id,
                type(exc).__name__,
            )
            raise

    def _progress_assets(
        self,
        handle: DeletionJobHandle,
        manifest: tuple[DeletionManifestItem, ...],
        scope: ResourceScope,
        stage: DeletionStage,
        *,
        completed: int,
        total: int,
    ) -> None:
        handle.progress(stage, completed_items=completed, total_items=total)
        if handle.manages_asset_lifecycle:
            return
        for item in manifest:
            try:
                run_coro_sync(
                    self._assets.store.set_asset_deletion_state(
                        item.asset_id,
                        scope=scope,
                        lifecycle_status="deleting",
                        deletion_operation_id=handle.operation_id,
                        deletion_stage=stage.value,
                        deletion_error=None,
                    )
                )
            except AssetNotFound:
                # The metadata-removal stage deliberately makes this update a
                # no-op; the operation manifest remains the audit anchor.
                pass

    def _mark_failed_assets(
        self,
        manifest: tuple[DeletionManifestItem, ...],
        *,
        scope: ResourceScope,
        operation_id: str,
        message: str,
    ) -> None:
        for item in manifest:
            try:
                run_coro_sync(
                    self._assets.store.set_asset_deletion_state(
                        item.asset_id,
                        scope=scope,
                        lifecycle_status="delete_failed",
                        deletion_operation_id=operation_id,
                        deletion_stage=DeletionStage.DELETE_FAILED.value,
                        deletion_error=message,
                    )
                )
            except AssetNotFound:
                pass

    def _knowledge_mark_deleting(
        self,
        source_id: str,
        visible_to: UserContext | None,
        *,
        principal: Principal,
        deletion_permit: SourceDeletionPermit,
    ) -> None:
        if self._knowledge is None:
            return
        run_coro_sync(
            self._knowledge.mark_source_deleting(
                source_id,
                visible_to=visible_to,
                principal=principal,
                deletion_permit=deletion_permit,
            )
        )

    def _knowledge_prepare_cleanup(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        if self._knowledge is None:
            raise KnowledgeDeletionDependencyUnavailable()
        return run_coro_sync(
            self._knowledge.prepare_source_cleanup(
                source_id,
                deletion_permit=deletion_permit,
            )
        )

    def _knowledge_delete_source(
        self,
        source_id: str,
        visible_to: UserContext | None,
        *,
        principal: Principal,
        deletion_permit: SourceDeletionPermit,
        cleanup_plan: SourceCleanupPlan | None,
    ) -> None:
        if self._knowledge is None:
            self._require_knowledge_cleanup_dependency(cleanup_plan)
            return
        run_coro_sync(
            self._knowledge.delete_source(
                source_id,
                visible_to=visible_to,
                principal=principal,
                deletion_permit=deletion_permit,
                cleanup_plan=cleanup_plan,
            )
        )

    def _knowledge_residual_count(
        self,
        source_id: str,
        visible_to: UserContext | None,
        *,
        deletion_permit: SourceDeletionPermit,
        cleanup_plan: SourceCleanupPlan | None,
    ) -> int:
        if self._knowledge is None:
            self._require_knowledge_cleanup_dependency(cleanup_plan)
            return 0
        return int(
            run_coro_sync(
                self._knowledge.count_source_residuals(
                    source_id,
                    principal=(visible_to.principal if visible_to else None),
                    workspace_id=(
                        visible_to.workspace_ids[0]
                        if visible_to and visible_to.workspace_ids
                        else None
                    ),
                    deletion_permit=deletion_permit,
                    cleanup_plan=cleanup_plan,
                )
            )
        )

    def _require_knowledge_cleanup_dependency(
        self,
        cleanup_plan: SourceCleanupPlan | None,
    ) -> None:
        """Fail closed unless an immutable empty cleanup plan proves absence.

        A source fence prevents new index writes after operation registration.
        Consequently, an empty plan produced under that fence is durable proof
        that this source had no Knowledge documents or physical point targets.
        A missing plan is uncertainty, not a zero count; a non-empty plan still
        requires the configured Knowledge lifecycle service to remove and
        verify its exact targets.
        """

        if self._knowledge is not None:
            return
        if cleanup_plan is not None and not cleanup_plan.targets:
            return
        raise KnowledgeDeletionDependencyUnavailable()

    def _can_prove_empty_knowledge_cleanup(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
    ) -> bool:
        """Return a volatile-tier proof while the source fence is already held.

        The durable Postgres operation store derives a stronger proof from the
        canonical Knowledge and vector-link rows in its registration
        transaction.  In the process-local tier, an unavailable Knowledge
        service means no Knowledge store exists in this composition.  The
        shared vector-index membership registry is therefore the remaining
        server-owned linkage witness.  A missing registry cannot prove
        absence and intentionally returns ``False``.
        """

        if self._knowledge is not None or self._vector_indexes is None:
            return False
        return not bool(
            run_coro_sync(
                self._vector_indexes.count_asset_memberships(
                    asset_id,
                    scope=scope,
                )
            )
        )

    def _verify_external_residuals(
        self,
        manifest: tuple[DeletionManifestItem, ...],
        *,
        principal: Principal,
        visible_to: UserContext | None,
        scope: ResourceScope,
        source_state: dict[str, tuple[SourceDeletionPermit, SourceCleanupPlan | None]],
    ) -> None:
        residuals: list[str] = []
        for item in manifest:
            if self._vector_indexes is not None:
                count = run_coro_sync(
                    self._vector_indexes.count_asset_memberships(
                        item.asset_id, scope=scope
                    )
                )
                if count:
                    residuals.append(f"{item.asset_id}:vector_members={count}")
            permit, cleanup_plan = source_state[item.source_id]
            knowledge_count = self._knowledge_residual_count(
                item.source_id,
                visible_to,
                deletion_permit=permit,
                cleanup_plan=cleanup_plan,
            )
            if knowledge_count:
                residuals.append(f"{item.asset_id}:knowledge_records={knowledge_count}")
            if item.server_file_id and self._files is not None:
                registry_exists, object_exists = run_coro_sync(
                    self._files.file_lifecycle_residuals(
                        item.server_file_id,
                        tenant_id=principal.tenant_id,
                    )
                )
                if registry_exists:
                    residuals.append(f"{item.asset_id}:file_registry=1")
                if object_exists:
                    residuals.append(f"{item.asset_id}:blob_object=1")
                if self._quota is not None:
                    stock = self._quota.stock_state_blocking(
                        tenant_id=principal.tenant_id,
                        stock_key=file_stock_key(item.server_file_id),
                    )
                    if stock is None or stock.amount != 0 or not stock.tombstoned:
                        residuals.append(f"{item.asset_id}:quota_stock_not_deleted=1")
            if (
                item.upload_operation_id is not None
                and self._uploads is not None
                and not self._uploads.deletion_can_finalize(
                    item.upload_operation_id,
                    tenant_id=principal.tenant_id,
                    asset_id=item.asset_id,
                )
            ):
                residuals.append(f"{item.asset_id}:upload_cleanup_in_progress=1")
        if residuals:
            raise RuntimeError(
                "Deletion verification found remaining resources: " + ", ".join(residuals)
            )

    def _verify_metadata_absent(
        self, manifest: tuple[DeletionManifestItem, ...]
    ) -> None:
        residuals: list[str] = []
        for item in manifest:
            try:
                run_coro_sync(self._assets.store.get_asset(item.asset_id))
            except AssetNotFound:
                continue
            residuals.append(item.asset_id)
        if residuals:
            raise RuntimeError(
                "Deletion verification found remaining asset metadata: "
                + ", ".join(residuals)
            )
