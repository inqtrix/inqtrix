"""Delete an account's aggregates through the surfaces that own them.

The account erasure (:mod:`inqtrix.services.user_erasure_executor`) owns
the order of the whole teardown; this module owns how ONE aggregate goes.

Desk sessions, library sections with their assets and files, knowledge
documents and collections, and vector indexes are deleted by the durable
deletion machine: one ordinary operation each, registered in the account's
name and run by the erasure's own attempt. They never enter the queue, so
no other worker races the erasure for them and the erasure never waits for
a free worker slot -- with the default of two deletion slots, an erasure
waiting for queued operations could block itself. A failed or interrupted
operation is reopened and run again on the next attempt.

Runs that no session claimed go through the run store, which removes
their checkpoints together with them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.exceptions import RunNotFound
from inqtrix.runs.deletion_execution import execute_claimed_deletion
from inqtrix.runs.deletion_operations import (
    DeletionOperationConflict,
    DeletionOperationStatus,
)
from inqtrix.services.user_erasure_executor import AggregateRef
from inqtrix.services.user_erasure_inventory import _tables
from inqtrix.storage.db import tenant_session
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:  # pragma: no cover - typing only
    from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
    from inqtrix.services.asset_deletion_service import AssetDeletionService

AGENT_SESSION = "agent_session"
KNOWLEDGE_SESSION = "knowledge_session"
RUN = "run"
SECTION = "section"
KNOWLEDGE_DOCUMENT = "knowledge_document"
KNOWLEDGE_COLLECTION = "knowledge_collection"
VECTOR_INDEX = "vector_index"
SOURCE = "source"


class _RegisterWithoutDispatch:
    """The deletion store, registering and reopening without dispatch."""

    def __init__(self, store: "PostgresDeletionOperationStore") -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.submit(dispatch=False, **kwargs)

    def retry(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._store.retry(operation_id, dispatch=False, **kwargs)


class DeletionMachineAggregates:
    """The erasure's port onto the deletion machine and the run store."""

    def __init__(
        self,
        *,
        service: "AssetDeletionService",
        store: "PostgresDeletionOperationStore",
        run_store: Any,
        session_factory: Any,
        app_role: str,
    ) -> None:
        self._store = _RegisterWithoutDispatch(store)
        self._service = service
        self._run_store = run_store
        self._session_factory = session_factory
        self._app_role = app_role

    # -- what is left ------------------------------------------------------ #

    def pending(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[AggregateRef, ...]:
        """Every aggregate the account still owns, in deletion order.

        Sessions go first because they own runs and artifacts; then the
        runs no session claimed; then the library, section by section --
        a section is its root, and every asset lives in a section of its
        own account and workspace; then documents the account put into
        OTHER people's collections before its own collections; then
        indexes. Last the sources that outlived their asset, section or
        document: every aggregate above settles its own sources itself.
        """
        return run_coro_sync(self._pending(tenant_id, user_id))

    async def _pending(
        self, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[AggregateRef, ...]:
        tables = _tables()
        refs: list[AggregateRef] = []
        async with tenant_session(
            self._session_factory, tenant_id=tenant_id, app_role=self._app_role
        ) as db:
            for name, kind in (
                ("agent_sessions", AGENT_SESSION),
                ("knowledge_sessions", KNOWLEDGE_SESSION),
            ):
                table = tables[name]
                rows = await db.execute(
                    sa.select(table.c.id, table.c.workspace_id)
                    .where(table.c.created_by_user_id == user_id)
                    .order_by(table.c.id)
                )
                refs.extend(
                    AggregateRef(kind, (row.id,), row.workspace_id) for row in rows
                )

            runs = tables["runs"]
            # A run inside one of the account's own sessions goes with that
            # session, which is deleted first; listed again, it would only
            # be found gone.
            in_own_session = sa.or_(
                *(
                    sa.exists().where(
                        tables[name].c.id == runs.c.session_id,
                        tables[name].c.created_by_user_id == user_id,
                    )
                    for name in ("agent_sessions", "knowledge_sessions")
                )
            )
            rows = await db.execute(
                sa.select(runs.c.run_id, runs.c.workspace_id)
                .where(
                    runs.c.created_by_user_id == user_id,
                    runs.c.parent_run_id.is_(None),
                    ~in_own_session,
                )
                .order_by(runs.c.run_id)
            )
            refs.extend(
                AggregateRef(RUN, (row.run_id,), row.workspace_id) for row in rows
            )

            sections = tables["asset_sections"]
            rows = await db.execute(
                sa.select(sections.c.id, sections.c.workspace_id)
                .where(sections.c.created_by_user_id == user_id)
                .order_by(sections.c.workspace_id, sections.c.id)
            )
            refs.extend(
                AggregateRef(SECTION, (row.id,), row.workspace_id) for row in rows
            )

            documents = tables["knowledge_documents"]
            collections = tables["knowledge_collections"]
            rows = await db.execute(
                sa.select(documents.c.id)
                .select_from(
                    documents.join(
                        collections,
                        collections.c.id == documents.c.collection_id,
                    )
                )
                .where(
                    documents.c.source_owner_user_id == user_id,
                    collections.c.created_by_user_id.is_distinct_from(user_id),
                )
                .order_by(documents.c.id)
            )
            refs.extend(AggregateRef(KNOWLEDGE_DOCUMENT, (row.id,)) for row in rows)
            rows = await db.execute(
                sa.select(collections.c.id)
                .where(collections.c.created_by_user_id == user_id)
                .order_by(collections.c.id)
            )
            refs.extend(
                AggregateRef(KNOWLEDGE_COLLECTION, (row.id,)) for row in rows
            )

            indexes = tables["vector_index_records"]
            rows = await db.execute(
                sa.select(indexes.c.id, indexes.c.workspace_id)
                .where(indexes.c.created_by_user_id == user_id)
                .order_by(indexes.c.id)
            )
            refs.extend(
                AggregateRef(VECTOR_INDEX, (row.id,), row.workspace_id)
                for row in rows
            )

            # A live source nothing backs any more: its lifecycle row is the
            # only map to what the Knowledge store may still hold for it.
            lifecycles = tables["source_lifecycles"]
            records = tables["asset_records"]
            documents = tables["knowledge_documents"]
            source_id = lifecycles.c.source_id
            backed = sa.or_(
                sa.and_(
                    source_id.startswith("asset:"),
                    sa.exists().where(records.c.id == sa.func.substr(source_id, 7)),
                ),
                sa.and_(
                    source_id.startswith("section:"),
                    sa.exists().where(sections.c.id == sa.func.substr(source_id, 9)),
                ),
                sa.exists().where(documents.c.source_id == source_id),
            )
            rows = await db.execute(
                sa.select(source_id, lifecycles.c.workspace_id)
                .where(
                    lifecycles.c.owner_user_id == user_id,
                    lifecycles.c.state != "deleted",
                    ~backed,
                )
                .order_by(source_id)
            )
            refs.extend(
                AggregateRef(SOURCE, (row.source_id,), row.workspace_id)
                for row in rows
            )
        return tuple(refs)

    # -- one aggregate ------------------------------------------------------ #

    def delete(
        self, ref: AggregateRef, *, tenant_id: str, user_id: uuid.UUID
    ) -> None:
        """Delete one aggregate completely, or raise why it could not."""
        principal = Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )
        visible_to = UserContext(
            principal=principal,
            workspace_ids=(ref.workspace_id,) if ref.workspace_id else (),
        )
        if ref.kind == RUN:
            self._delete_run(ref, visible_to=visible_to, user_id=user_id)
            return
        # A fresh view per call: bindings the worker adds to the service
        # after start-up (its quota recorder) must reach this one too.
        service = self._service.with_operation_store(self._store)
        target_id = ref.target_ids[0]
        if ref.kind == AGENT_SESSION:
            start = service.start_agent_session(
                target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=ref.workspace_id,
            )
        elif ref.kind == KNOWLEDGE_SESSION:
            start = service.start_knowledge_session(
                target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=ref.workspace_id,
            )
        elif ref.kind == SECTION:
            start = self._start_section(
                service, ref, principal=principal, visible_to=visible_to
            )
        elif ref.kind == KNOWLEDGE_DOCUMENT:
            start = service.start_knowledge_document(
                target_id, principal=principal, visible_to=visible_to
            )
        elif ref.kind == KNOWLEDGE_COLLECTION:
            start = service.start_knowledge_collection(
                target_id, principal=principal, visible_to=visible_to
            )
        elif ref.kind == VECTOR_INDEX:
            start = service.start_vector_index(
                target_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=ref.workspace_id,
            )
        elif ref.kind == SOURCE:
            start = service.start_orphaned_source(
                target_id, principal=principal, workspace_id=ref.workspace_id
            )
        else:
            raise ValueError(f"unknown aggregate kind: {ref.kind}")
        summary = run_coro_sync(start)
        self._run_to_the_end(
            service,
            summary,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=ref.workspace_id,
            kind=ref.kind,
        )

    async def _start_section(
        self,
        service: "AssetDeletionService",
        ref: AggregateRef,
        *,
        principal: Principal,
        visible_to: UserContext,
    ) -> dict[str, Any]:
        """One section deletion, or the one that already holds its files.

        Files already in deletion refuse a new operation on their section,
        and the refusal names one of them; the operation holding that file
        is the one to finish. Usually it is this section's own, left by an
        earlier attempt. If it is a deletion the account started itself,
        the section follows on the erasure's next attempt.
        """
        try:
            return await service.start_section(
                ref.target_ids[0],
                principal=principal,
                visible_to=visible_to,
                workspace_id=ref.workspace_id,
            )
        except DeletionOperationConflict as conflict:
            existing = (
                self._store.find_for_asset(
                    str(conflict.args[0]),
                    tenant_id=principal.tenant_id,
                    created_by_user_id=principal.user_id,
                    workspace_id=ref.workspace_id,
                )
                if conflict.args
                else None
            )
            if existing is not None:
                return existing
            raise

    def _run_to_the_end(
        self,
        service: "AssetDeletionService",
        summary: dict[str, Any],
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        workspace_id: str | None,
        kind: str,
    ) -> None:
        """Run a registered aggregate deletion inline until it is deleted."""
        operation_id = str(summary["operation_id"])
        status = summary.get("status")
        if status == DeletionOperationStatus.DELETED.value:
            return
        if status == DeletionOperationStatus.DELETE_FAILED.value:
            self._store.retry(
                operation_id,
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                workspace_id=workspace_id,
            )
        # A takeover is safe here: the only other attempt that can hold this
        # operation is one an interrupted erasure attempt left behind, and
        # the fence stops it at its next step.
        claimed = self._store.claim_for_execution(
            operation_id, tenant_id, allow_takeover=True
        )
        if claimed is not None:
            execute_claimed_deletion(self._store, service, claimed)
            workspace_id = claimed.workspace_id
        record = self._store.get_record(
            operation_id,
            tenant_id=tenant_id,
            created_by_user_id=user_id,
            workspace_id=workspace_id,
        )
        if record.status != DeletionOperationStatus.DELETED:
            reason = (record.error or {}).get("message") or record.status.value
            raise RuntimeError(f"the {kind} deletion did not finish: {reason}")

    def _delete_run(
        self,
        ref: AggregateRef,
        *,
        visible_to: UserContext,
        user_id: uuid.UUID,
    ) -> None:
        # Function-local import: the services layer must not pull in
        # server modules at import time (layering regression test).
        from inqtrix.server.runs import RunActive

        run_id = ref.target_ids[0]
        try:
            self._run_store.delete(
                run_id,
                workspace_id=ref.workspace_id,
                requester_user_id=user_id,
            )
        except RunNotFound:
            return
        except RunActive:
            # Deletion is terminal-only. Ask the run to stop; the next
            # attempt of the erasure deletes it once it has.
            self._run_store.cancel(
                run_id, workspace_id=ref.workspace_id, visible_to=visible_to
            )
            raise RuntimeError(
                "a run of the account is still stopping; retry the erasure "
                "once it has ended"
            ) from None
