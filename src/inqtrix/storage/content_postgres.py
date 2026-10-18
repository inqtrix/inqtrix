"""Postgres-backed file registry (same port as the memory default).

Every operation runs inside
:func:`~inqtrix.storage.db.tenant_session` — restricted role,
transaction-local tenant GUC — with explicit tenant predicates as
layer 1 and row-level security as layer 2, identical to the identity
repositories.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager

from sqlalchemy import Row, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from inqtrix.content.ports import FileNotFound, FileRecord
from inqtrix.storage.content_orm import files
from inqtrix.storage.db import tenant_session


def _record_from_row(row: Row) -> FileRecord:
    return FileRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        workspace_id=row.workspace_id,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        object_key=row.object_key,
        created_at=row.created_at,
    )


class PostgresFileRegistry:
    """File metadata over the ``files`` table.

    Args:
        session_factory: Factory from
            :func:`inqtrix.storage.db.build_session_factory`.
        app_role: Restricted Postgres role for the tenant sessions
            (see ``StorageSettings.app_role``).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        app_role: str,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role

    def _session(
        self, tenant_id: str
    ) -> AbstractAsyncContextManager[AsyncSession]:
        """One tenant transaction with this registry's app role bound."""
        return tenant_session(
            self._session_factory, tenant_id=tenant_id, app_role=self._app_role
        )

    async def create(self, record: FileRecord) -> None:
        """Insert one metadata row."""
        async with self._session(record.tenant_id) as session:
            await session.execute(
                insert(files).values(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    owner_user_id=record.owner_user_id,
                    workspace_id=record.workspace_id,
                    file_name=record.file_name,
                    content_type=record.content_type,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    object_key=record.object_key,
                    created_at=record.created_at,
                )
            )

    async def get(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Fetch one record; absence and foreign tenants raise alike."""
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(files).where(
                        files.c.tenant_id == tenant_id,
                        files.c.id == file_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise FileNotFound(file_id)
        return _record_from_row(row)

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[FileRecord]:
        """Newest-first listing with tenant/owner/namespace facets."""
        statement = select(files).where(files.c.tenant_id == tenant_id)
        if owner_user_id is not None:
            statement = statement.where(files.c.owner_user_id == owner_user_id)
        if workspace_id is not None:
            statement = statement.where(files.c.workspace_id == workspace_id)
        statement = statement.order_by(files.c.created_at.desc())
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).all()
        return [_record_from_row(row) for row in rows]

    async def delete(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Delete and return the record or raise :class:`FileNotFound`."""
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    delete(files)
                    .where(
                        files.c.tenant_id == tenant_id,
                        files.c.id == file_id,
                    )
                    .returning(*files.c)
                )
            ).one_or_none()
        if row is None:
            raise FileNotFound(file_id)
        return _record_from_row(row)
