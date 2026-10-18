"""Postgres-backed agent-session store (durable Agent-Desk tier).

Sessions persist relationally, scoped per ``(tenant_id, created_by_user_id,
workspace_id)`` with the inherited tenant-session lifecycle
(:class:`BaseSessionStore`). ``list_sessions`` SELECTs metadata columns only
(NOT the heavy ``items_json``); ``get_session`` SELECTs the full row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
from inqtrix.project.deletion_fence import reject_retained_deletion_target
from inqtrix.project.agent_sessions_ports import (
    AgentSession,
    AgentSessionGroup,
    AgentSessionGroupNotFound,
    AgentSessionNotFound,
)
from inqtrix.project.session_titles import TitleSource, check_legacy_title_conflict, generated_title_update, legacy_title_condition, title_update_columns
from inqtrix.project.scoped_upsert import (
    ResourceScope,
    delete_scoped_postgres,
    require_scoped_parent,
    scoped_postgres_upsert,
)
from inqtrix.storage.agent_sessions_orm import (
    agent_session_groups,
    agent_sessions,
)

# Metadata columns (everything EXCEPT the heavy items_json) for the list path.
_META_COLUMNS = (
    agent_sessions.c.id,
    agent_sessions.c.tenant_id,
    agent_sessions.c.created_by_user_id,
    agent_sessions.c.workspace_id,
    agent_sessions.c.title,
    agent_sessions.c.title_revision,
    agent_sessions.c.title_source,
    agent_sessions.c.group_id,
    agent_sessions.c.lifecycle_status,
    agent_sessions.c.deletion_operation_id,
    agent_sessions.c.deletion_stage,
    agent_sessions.c.deletion_error,
    agent_sessions.c.created_at,
    agent_sessions.c.updated_at,
)


class PostgresAgentSessionStore(BaseSessionStore):
    """Durable
    :class:`~inqtrix.project.agent_sessions_ports.AgentSessionStore`."""

    async def claim_session(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        values = dict(
            id=id,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            title=title,
            title_source=title_source,
            group_id=None,
            items_json="[]",
            created_at=created_at,
            updated_at=created_at,
        )
        async with self._session() as session:
            await reject_retained_deletion_target(
                session,
                target_kind="agent_session",
                target_id=id,
                tenant_id=_DEFAULT_TENANT,
                not_found=AgentSessionNotFound,
            )
            row = (
                await session.execute(
                    pg_insert(agent_sessions)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[agent_sessions.c.id])
                    .returning(agent_sessions)
                )
            ).first()
            if row is None:
                row = (
                    await session.execute(
                        select(agent_sessions).where(
                            agent_sessions.c.tenant_id == _DEFAULT_TENANT,
                            agent_sessions.c.id == id,
                            agent_sessions.c.lifecycle_status == "active",
                        )
                    )
                ).first()
                if row is None:
                    raise AgentSessionNotFound(id)
        return _from_row(row)

    async def upsert_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, title=title, group_id=group_id,
            title_revision=0, title_source=title_source,
            items_json=items_json,
            created_at=created_at, updated_at=updated_at,
        )
        mutable = ["title", "title_revision", "title_source", "group_id", "items_json", "updated_at"]
        stmt = scoped_postgres_upsert(
            pg_insert(agent_sessions),
            agent_sessions,
            values,
            mutable,
            extra_condition=and_(agent_sessions.c.lifecycle_status == "active",
                legacy_title_condition(agent_sessions, pg_insert(agent_sessions).excluded.title, title_revision)),
            column_updates=title_update_columns(
                agent_sessions, title=pg_insert(agent_sessions).excluded.title,
                revision=title_revision, source=title_source,
            ),
        ).returning(agent_sessions)
        async with self._session() as session:
            await reject_retained_deletion_target(
                session,
                target_kind="agent_session",
                target_id=id,
                tenant_id=_DEFAULT_TENANT,
                not_found=AgentSessionNotFound,
            )
            if group_id is not None:
                await require_scoped_parent(
                    session,
                    table=agent_session_groups,
                    parent_id=group_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=AgentSessionGroupNotFound,
                )
            row = (await session.execute(stmt)).first()
            if row is None and title_revision is None:
                await check_legacy_title_conflict(session, agent_sessions, id, ResourceScope(created_by_user_id, workspace_id), title, tenant_id=_DEFAULT_TENANT)
        if row is None:
            raise AgentSessionNotFound(id)
        return _from_row(row)

    async def apply_generated_title(
        self, session_id: str, *, title: str, revision: int, scope: ResourceScope
    ) -> bool:
        async with self._session() as session:
            await require_scoped_parent(session, table=agent_sessions, parent_id=session_id,
                tenant_id=_DEFAULT_TENANT, created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id, not_found=AgentSessionNotFound,
                extra_condition=agent_sessions.c.lifecycle_status == "active")
            result = await session.execute(generated_title_update(
                agent_sessions, resource_id=session_id, tenant_id=_DEFAULT_TENANT,
                scope=scope, title=title, revision=revision,
            ))
            return result.scalar_one_or_none() is not None

    async def list_sessions(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSession]:
        query = _scoped_query(
            select(*_META_COLUMNS, agent_sessions.c.items_json),
            agent_sessions, created_by_user_id, workspace_id,
        )
        query = query.order_by(
            agent_sessions.c.updated_at.desc(), agent_sessions.c.id.desc()
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [_from_row(row) for row in rows]

    async def get_session(self, session_id: str) -> AgentSession:
        query = select(agent_sessions).where(
            agent_sessions.c.tenant_id == _DEFAULT_TENANT,
            agent_sessions.c.id == session_id,
        )
        async with self._session() as session:
            row = (await session.execute(query)).first()
        if row is None:
            raise AgentSessionNotFound(session_id)
        return _from_row(row)

    async def delete_session(
        self, session_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=agent_sessions, resource_id=session_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=AgentSessionNotFound,
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
                update(agent_sessions)
                .where(
                    agent_sessions.c.tenant_id == _DEFAULT_TENANT,
                    agent_sessions.c.id == session_id,
                    agent_sessions.c.created_by_user_id.is_not_distinct_from(
                        scope.created_by_user_id
                    ),
                    agent_sessions.c.workspace_id.is_not_distinct_from(
                        scope.workspace_id
                    ),
                )
                .values(
                    lifecycle_status=lifecycle_status,
                    deletion_operation_id=deletion_operation_id,
                    deletion_stage=deletion_stage,
                    deletion_error=deletion_error,
                )
                .returning(agent_sessions.c.id)
            )
            if changed is None:
                raise AgentSessionNotFound(session_id)

    async def count_session_residuals(
        self, session_id: str, *, scope: ResourceScope
    ) -> int:
        async with self._session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(agent_sessions)
                    .where(
                        agent_sessions.c.tenant_id == _DEFAULT_TENANT,
                        agent_sessions.c.id == session_id,
                        agent_sessions.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        agent_sessions.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                    )
                )
                or 0
            )

    async def upsert_group(
        self, *, id: str, title: str, created_at: float, updated_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> AgentSessionGroup:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, title=title, created_at=created_at,
            updated_at=updated_at,
        )
        stmt = scoped_postgres_upsert(
            pg_insert(agent_session_groups),
            agent_session_groups,
            values,
            ["title", "updated_at"],
        ).returning(agent_session_groups)
        async with self._session() as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            raise AgentSessionGroupNotFound(id)
        return _group_from_row(row)

    async def claim_group(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> AgentSessionGroup:
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id, workspace_id=workspace_id,
            title=title, created_at=created_at, updated_at=created_at,
        )
        async with self._session() as session:
            row = (
                await session.execute(
                    pg_insert(agent_session_groups)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[agent_session_groups.c.id]
                    )
                    .returning(agent_session_groups)
                )
            ).first()
            if row is None:
                row = (
                    await session.execute(
                        select(agent_session_groups).where(
                            agent_session_groups.c.tenant_id
                            == _DEFAULT_TENANT,
                            agent_session_groups.c.id == id,
                        )
                    )
                ).one()
        return _group_from_row(row)

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSessionGroup]:
        query = _scoped_query(
            select(agent_session_groups), agent_session_groups,
            created_by_user_id, workspace_id,
        )
        query = query.order_by(
            agent_session_groups.c.created_at.desc(),
            agent_session_groups.c.id.desc(),
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [_group_from_row(row) for row in rows]

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=agent_session_groups, resource_id=group_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=AgentSessionGroupNotFound,
            )


def _from_row(row) -> AgentSession:
    return AgentSession(
        id=row.id,
        title=row.title,
        title_revision=row.title_revision,
        title_source=row.title_source,
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


def _group_from_row(row) -> AgentSessionGroup:
    return AgentSessionGroup(
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
