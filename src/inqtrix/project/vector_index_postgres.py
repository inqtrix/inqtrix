"""Postgres-backed vector-index-record store (durable project tier).

Records persist relationally with two owned child collections (members +
history), scoped per ``(tenant_id, created_by_user_id, workspace_id)`` with RLS
and the inherited tenant-session lifecycle (:class:`BaseSessionStore`).

The record and its children travel together (the serialized
``VectorIndexRecord`` carries them), so :meth:`upsert_index` rewrites the
whole child set inside ONE transaction: upsert the record, delete its
members + history, re-insert the supplied set. The list path loads the
children for the whole page in two grouped queries (not per-record) to
avoid an N+1.
"""

from __future__ import annotations

import uuid
import time
from collections import defaultdict

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.pagination import encode_cursor
from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
from inqtrix.project.vector_index_ports import (
    VectorIndexHistoryEntry,
    VectorIndexMemberUnavailable,
    VectorIndexMember,
    VectorIndexNotFound,
    VectorIndexRecord,
)
from inqtrix.project.asset_lifecycle import lock_asset_lifecycle
from inqtrix.project.scoped_upsert import ResourceScope, delete_scoped_postgres
from inqtrix.project.scoped_upsert import scoped_postgres_upsert
from inqtrix.storage.vector_index_orm import (
    vector_index_history,
    vector_index_members,
    vector_index_records,
)
from inqtrix.storage.asset_records_orm import asset_records

# Mutable record columns for the on-conflict upsert (everything except the
# PK, created_at, and the ownership anchor — never reassigned).
_MUTABLE = [
    "title", "handle", "model", "dims", "status",
    "server_collection_id", "server_collection_model", "last_error", "updated_at",
]


class PostgresVectorIndexStore(BaseSessionStore):
    """Durable :class:`~inqtrix.project.vector_index_ports.VectorIndexStore`.

    Inherits the engine + tenant-session lifecycle from
    :class:`~inqtrix.project.base_session_store.BaseSessionStore`.
    """

    async def upsert_index(
        self, *, id, title, handle, model, dims, status, server_collection_id,
        server_collection_model, last_error, members, history, created_at,
        updated_at, created_by_user_id: uuid.UUID | None, workspace_id,
    ) -> VectorIndexRecord:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, title=title, handle=handle, model=model,
            dims=dims, status=status, server_collection_id=server_collection_id,
            server_collection_model=server_collection_model,
            last_error=last_error, created_at=created_at, updated_at=updated_at,
        )
        record_stmt = scoped_postgres_upsert(
            pg_insert(vector_index_records), vector_index_records, values, _MUTABLE
        ).returning(vector_index_records)
        members = tuple(members)
        history = tuple(history)
        async with self._session() as session:
            member_ids = sorted({member.file_id for member in members})
            for asset_id in member_ids:
                await lock_asset_lifecycle(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    asset_id=asset_id,
                )
            if member_ids:
                active_ids = set(
                    (
                        await session.execute(
                            select(asset_records.c.id).where(
                                asset_records.c.tenant_id == _DEFAULT_TENANT,
                                asset_records.c.id.in_(member_ids),
                                asset_records.c.created_by_user_id.is_not_distinct_from(
                                    created_by_user_id
                                ),
                                asset_records.c.workspace_id.is_not_distinct_from(
                                    workspace_id
                                ),
                                asset_records.c.lifecycle_status == "active",
                            )
                        )
                    ).scalars()
                )
                unavailable = [
                    asset_id for asset_id in member_ids if asset_id not in active_ids
                ]
                if unavailable:
                    raise VectorIndexMemberUnavailable(unavailable[0])
            row = (await session.execute(record_stmt)).first()
            if row is None:
                raise VectorIndexNotFound(id)
            await self._replace_children(session, id, members, history)
        return self._record_from_row(row, members, history)

    async def list_indexes_page(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id, limit, after
    ) -> tuple[list[VectorIndexRecord], str | None]:
        query = select(vector_index_records).where(
            vector_index_records.c.tenant_id == _DEFAULT_TENANT
        )
        if created_by_user_id is not None:
            query = query.where(vector_index_records.c.created_by_user_id == created_by_user_id)
        if workspace_id is not None:
            query = query.where(vector_index_records.c.workspace_id == workspace_id)
        if after is not None:
            query = query.where(
                tuple_(vector_index_records.c.created_at, vector_index_records.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            vector_index_records.c.created_at.desc(), vector_index_records.c.id.desc()
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
            page_rows = rows[:limit]
            members_by_index, history_by_index = await self._load_children(
                session, [r.id for r in page_rows]
            )
        records = [
            self._record_from_row(
                r, members_by_index.get(r.id, ()), history_by_index.get(r.id, ())
            )
            for r in page_rows
        ]
        next_cursor = (
            encode_cursor(records[-1].created_at, records[-1].id)
            if len(rows) > limit and records else None
        )
        return records, next_cursor

    async def get_index(self, index_id: str) -> VectorIndexRecord:
        async with self._session() as session:
            row = (await session.execute(select(vector_index_records).where(
                vector_index_records.c.tenant_id == _DEFAULT_TENANT,
                vector_index_records.c.id == index_id,
            ))).first()
            if row is None:
                raise VectorIndexNotFound(index_id)
            members_by_index, history_by_index = await self._load_children(
                session, [index_id]
            )
        return self._record_from_row(
            row, members_by_index.get(index_id, ()), history_by_index.get(index_id, ())
        )

    async def delete_index(
        self, index_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=vector_index_records, resource_id=index_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=VectorIndexNotFound,
            )

    async def set_deletion_state(
        self,
        index_id: str,
        *,
        scope: ResourceScope,
        status: str,
        error: str | None,
    ) -> None:
        async with self._session() as session:
            result = await session.execute(
                update(vector_index_records)
                .where(
                    vector_index_records.c.tenant_id == _DEFAULT_TENANT,
                    vector_index_records.c.id == index_id,
                    vector_index_records.c.created_by_user_id.is_not_distinct_from(
                        scope.created_by_user_id
                    ),
                    vector_index_records.c.workspace_id.is_not_distinct_from(
                        scope.workspace_id
                    ),
                )
                .values(status=status, last_error=error, updated_at=time.time())
                .returning(vector_index_records.c.id)
            )
            if result.scalar_one_or_none() is None:
                raise VectorIndexNotFound(index_id)

    async def count_index(self, index_id: str, *, scope: ResourceScope) -> int:
        async with self._session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(vector_index_records)
                    .where(
                        vector_index_records.c.tenant_id == _DEFAULT_TENANT,
                        vector_index_records.c.id == index_id,
                        vector_index_records.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        vector_index_records.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                    )
                )
                or 0
            )

    async def remove_asset_memberships(
        self, file_id: str, *, scope: ResourceScope
    ) -> int:
        owned_indexes = select(vector_index_records.c.id).where(
            vector_index_records.c.tenant_id == _DEFAULT_TENANT,
            vector_index_records.c.created_by_user_id.is_not_distinct_from(
                scope.created_by_user_id
            ),
            vector_index_records.c.workspace_id.is_not_distinct_from(
                scope.workspace_id
            ),
        )
        stmt = delete(vector_index_members).where(
            vector_index_members.c.tenant_id == _DEFAULT_TENANT,
            vector_index_members.c.file_id == file_id,
            vector_index_members.c.index_id.in_(owned_indexes),
        )
        async with self._session() as session:
            result = await session.execute(stmt)
        return max(0, int(result.rowcount or 0))

    async def count_asset_memberships(
        self, file_id: str, *, scope: ResourceScope
    ) -> int:
        owned_indexes = select(vector_index_records.c.id).where(
            vector_index_records.c.tenant_id == _DEFAULT_TENANT,
            vector_index_records.c.created_by_user_id.is_not_distinct_from(
                scope.created_by_user_id
            ),
            vector_index_records.c.workspace_id.is_not_distinct_from(
                scope.workspace_id
            ),
        )
        query = select(func.count()).select_from(vector_index_members).where(
            vector_index_members.c.tenant_id == _DEFAULT_TENANT,
            vector_index_members.c.file_id == file_id,
            vector_index_members.c.index_id.in_(owned_indexes),
        )
        async with self._session() as session:
            return int((await session.execute(query)).scalar_one())

    # -- children --------------------------------------------------------- #

    @staticmethod
    async def _replace_children(session, index_id, members, history) -> None:
        """Rewrite the member + history rows of one index inside the caller's
        transaction (delete-all then bulk-insert the supplied newest-first set)."""
        await session.execute(
            delete(vector_index_members).where(vector_index_members.c.index_id == index_id)
        )
        await session.execute(
            delete(vector_index_history).where(vector_index_history.c.index_id == index_id)
        )
        if members:
            await session.execute(vector_index_members.insert(), [
                dict(index_id=index_id, file_id=m.file_id, seq=seq, state=m.state,
                     server_document_id=m.server_document_id, tenant_id=_DEFAULT_TENANT)
                for seq, m in enumerate(members)
            ])
        if history:
            await session.execute(vector_index_history.insert(), [
                dict(index_id=index_id, seq=seq, tenant_id=_DEFAULT_TENANT,
                     result=h.result, documents=h.documents, duration_ms=h.duration_ms,
                     error=h.error, started_at=h.started_at, finished_at=h.finished_at)
                for seq, h in enumerate(history)
            ])

    @staticmethod
    async def _load_children(session, index_ids):
        """Members + history for a set of indexes, grouped by index id (two
        queries total, not per-index — avoids the N+1 on the list path)."""
        members_by_index: dict[str, list[VectorIndexMember]] = defaultdict(list)
        history_by_index: dict[str, list[VectorIndexHistoryEntry]] = defaultdict(list)
        if not index_ids:
            return members_by_index, history_by_index
        member_rows = (await session.execute(
            select(vector_index_members)
            .where(vector_index_members.c.index_id.in_(index_ids))
            .order_by(vector_index_members.c.index_id, vector_index_members.c.seq)
        )).all()
        for row in member_rows:
            members_by_index[row.index_id].append(
                VectorIndexMember(
                    file_id=row.file_id,
                    state=row.state,
                    server_document_id=row.server_document_id,
                )
            )
        history_rows = (await session.execute(
            select(vector_index_history)
            .where(vector_index_history.c.index_id.in_(index_ids))
            .order_by(vector_index_history.c.index_id, vector_index_history.c.seq)
        )).all()
        for row in history_rows:
            history_by_index[row.index_id].append(VectorIndexHistoryEntry(
                result=row.result, documents=row.documents, duration_ms=row.duration_ms,
                error=row.error, started_at=row.started_at, finished_at=row.finished_at,
            ))
        return members_by_index, history_by_index

    # -- row mapping ------------------------------------------------------ #

    @staticmethod
    def _record_from_row(row, members, history) -> VectorIndexRecord:
        return VectorIndexRecord(
            id=row.id, title=row.title, handle=row.handle, model=row.model,
            dims=row.dims, status=row.status,
            server_collection_id=row.server_collection_id,
            server_collection_model=row.server_collection_model,
            last_error=row.last_error,
            members=tuple(members), history=tuple(history),
            created_at=row.created_at, updated_at=row.updated_at,
            tenant_id=row.tenant_id, created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
        )
