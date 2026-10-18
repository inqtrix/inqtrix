"""Postgres-backed knowledge-session store (durable Wissensmodus tier).

Sessions persist relationally, scoped per ``(tenant_id, created_by_user_id,
workspace_id)`` with the inherited tenant-session lifecycle
(:class:`BaseSessionStore`). ``list_sessions`` SELECTs metadata columns only
(NOT the heavy ``items_json``); ``get_session`` SELECTs the full row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
from inqtrix.project.deletion_fence import reject_retained_deletion_target
from inqtrix.project.knowledge_sessions_ports import (
    KnowledgeSession,
    KnowledgeSessionGroup,
    KnowledgeSessionGroupNotFound,
    KnowledgeSessionNotFound,
)
from inqtrix.project.scoped_upsert import (
    ResourceScope,
    delete_scoped_postgres,
    require_scoped_parent,
    scoped_postgres_upsert,
)
from inqtrix.storage.knowledge_sessions_orm import (
    knowledge_session_groups,
    knowledge_sessions,
)

# Metadata columns (everything EXCEPT the heavy items_json) for the list path.
_META_COLUMNS = (
    knowledge_sessions.c.id,
    knowledge_sessions.c.tenant_id,
    knowledge_sessions.c.created_by_user_id,
    knowledge_sessions.c.workspace_id,
    knowledge_sessions.c.title,
    knowledge_sessions.c.group_id,
    knowledge_sessions.c.lifecycle_status,
    knowledge_sessions.c.deletion_operation_id,
    knowledge_sessions.c.deletion_stage,
    knowledge_sessions.c.deletion_error,
    knowledge_sessions.c.created_at,
    knowledge_sessions.c.updated_at,
)


class PostgresKnowledgeSessionStore(BaseSessionStore):
    """Durable
    :class:`~inqtrix.project.knowledge_sessions_ports.KnowledgeSessionStore`."""

    async def claim_session(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> KnowledgeSession:
        values = dict(
            id=id,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            title=title,
            group_id=None,
            items_json="[]",
            created_at=created_at,
            updated_at=created_at,
        )
        async with self._session() as session:
            await reject_retained_deletion_target(
                session,
                target_kind="knowledge_session",
                target_id=id,
                tenant_id=_DEFAULT_TENANT,
                not_found=KnowledgeSessionNotFound,
            )
            row = (
                await session.execute(
                    pg_insert(knowledge_sessions)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[knowledge_sessions.c.id])
                    .returning(knowledge_sessions)
                )
            ).first()
            if row is None:
                row = (
                    await session.execute(
                        select(knowledge_sessions).where(
                            knowledge_sessions.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_sessions.c.id == id,
                            knowledge_sessions.c.lifecycle_status == "active",
                        )
                    )
                ).first()
                if row is None:
                    raise KnowledgeSessionNotFound(id)
        return _from_row(row)

    async def upsert_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> KnowledgeSession:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, title=title, group_id=group_id,
            items_json=items_json,
            created_at=created_at, updated_at=updated_at,
        )
        mutable = ["title", "group_id", "items_json", "updated_at"]
        stmt = scoped_postgres_upsert(
            pg_insert(knowledge_sessions),
            knowledge_sessions,
            values,
            mutable,
            extra_condition=knowledge_sessions.c.lifecycle_status == "active",
        ).returning(knowledge_sessions)
        async with self._session() as session:
            await reject_retained_deletion_target(
                session,
                target_kind="knowledge_session",
                target_id=id,
                tenant_id=_DEFAULT_TENANT,
                not_found=KnowledgeSessionNotFound,
            )
            if group_id is not None:
                await require_scoped_parent(
                    session,
                    table=knowledge_session_groups,
                    parent_id=group_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=KnowledgeSessionGroupNotFound,
                )
            row = (await session.execute(stmt)).first()
            if row is None:
                raise KnowledgeSessionNotFound(id)
        return _from_row(row)

    async def list_sessions(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSession]:
        query = _scoped_query(
            select(*_META_COLUMNS), knowledge_sessions, created_by_user_id, workspace_id
        )
        query = query.order_by(
            knowledge_sessions.c.updated_at.desc(), knowledge_sessions.c.id.desc()
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [_from_row(row) for row in rows]

    async def get_session(self, session_id: str) -> KnowledgeSession:
        query = select(knowledge_sessions).where(
            knowledge_sessions.c.tenant_id == _DEFAULT_TENANT,
            knowledge_sessions.c.id == session_id,
        )
        async with self._session() as session:
            row = (await session.execute(query)).first()
        if row is None:
            raise KnowledgeSessionNotFound(session_id)
        return _from_row(row)

    async def delete_session(
        self, session_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=knowledge_sessions, resource_id=session_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=KnowledgeSessionNotFound,
            )

    async def set_session_deletion_state(
        self,
        session_id: str,
        *,
        scope: ResourceScope,
        lifecycle_status: str,
        deletion_operation_id: str,
        deletion_stage: str,
        deletion_error: str | None,
    ) -> None:
        async with self._session() as session:
            changed = await session.scalar(
                update(knowledge_sessions)
                .where(
                    knowledge_sessions.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_sessions.c.id == session_id,
                    knowledge_sessions.c.created_by_user_id.is_not_distinct_from(
                        scope.created_by_user_id
                    ),
                    knowledge_sessions.c.workspace_id.is_not_distinct_from(
                        scope.workspace_id
                    ),
                )
                .values(
                    lifecycle_status=lifecycle_status,
                    deletion_operation_id=deletion_operation_id,
                    deletion_stage=deletion_stage,
                    deletion_error=deletion_error,
                )
                .returning(knowledge_sessions.c.id)
            )
            if changed is None:
                raise KnowledgeSessionNotFound(session_id)

    async def count_session_residuals(
        self, session_id: str, *, scope: ResourceScope
    ) -> int:
        async with self._session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_sessions)
                    .where(
                        knowledge_sessions.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_sessions.c.id == session_id,
                        knowledge_sessions.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        knowledge_sessions.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                    )
                )
                or 0
            )

    async def upsert_group(
        self, *, id: str, title: str, created_at: float, updated_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> KnowledgeSessionGroup:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, title=title, created_at=created_at,
            updated_at=updated_at,
        )
        stmt = scoped_postgres_upsert(
            pg_insert(knowledge_session_groups), knowledge_session_groups,
            values, ["title", "updated_at"],
        ).returning(knowledge_session_groups)
        async with self._session() as session:
            row = (await session.execute(stmt)).first()
            if row is None:
                raise KnowledgeSessionGroupNotFound(id)
        return _group_from_row(row)

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSessionGroup]:
        query = _scoped_query(
            select(knowledge_session_groups), knowledge_session_groups,
            created_by_user_id, workspace_id,
        )
        query = query.order_by(
            knowledge_session_groups.c.created_at.desc(),
            knowledge_session_groups.c.id.desc(),
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [_group_from_row(row) for row in rows]

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=knowledge_session_groups, resource_id=group_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=KnowledgeSessionGroupNotFound,
            )


def _from_row(row) -> KnowledgeSession:
    return KnowledgeSession(
        id=row.id,
        title=row.title,
        group_id=row.group_id,
        items_json=getattr(row, "items_json", "[]") or "[]",
        created_at=row.created_at,
        updated_at=row.updated_at,
        tenant_id=row.tenant_id,
        created_by_user_id=row.created_by_user_id,
        workspace_id=row.workspace_id,
        lifecycle_status=getattr(row, "lifecycle_status", "active") or "active",
        deletion_operation_id=getattr(row, "deletion_operation_id", None),
        deletion_stage=getattr(row, "deletion_stage", None),
        deletion_error=getattr(row, "deletion_error", None),
    )


def _group_from_row(row) -> KnowledgeSessionGroup:
    return KnowledgeSessionGroup(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        tenant_id=row.tenant_id,
        created_by_user_id=row.created_by_user_id,
        workspace_id=row.workspace_id,
    )


def _scoped_query(
    query, table, created_by_user_id: uuid.UUID | None, workspace_id
):
    query = query.where(table.c.tenant_id == _DEFAULT_TENANT)
    if created_by_user_id is not None:
        query = query.where(table.c.created_by_user_id == created_by_user_id)
    if workspace_id is not None:
        query = query.where(table.c.workspace_id == workspace_id)
    return query
