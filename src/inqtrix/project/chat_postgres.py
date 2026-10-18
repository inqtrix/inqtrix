"""Postgres-backed chat-history store (the durable project tier).

Threads, their grouping, and messages persist relationally, scoped per
``(tenant_id, created_by_user_id, workspace_id)`` and joinable for the
owner/share visibility rule. Every operation runs inside
:func:`~inqtrix.storage.db.tenant_session` (restricted role +
transaction-local tenant GUC), with an explicit tenant predicate as
layer 1 and row-level security as layer 2 — identical to the runs and
knowledge repositories.

Autosave is an idempotent ``INSERT ... ON CONFLICT (id) DO UPDATE``:
re-saving a thread or message overwrites only its mutable fields and
never reassigns ``created_at`` or the ownership columns, so a later
write cannot silently re-home or re-order existing data.

The engine is its own NullPool engine (loop-agnostic): the store is
awaited from the HTTP loop, the same constraint the knowledge store
documents.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.pagination import encode_cursor
from inqtrix.project.chat_ports import (
    ChatMessage,
    ChatThread,
    ChatThreadGroup,
    ThreadGroupNotFound,
    ThreadNotFound,
)
from inqtrix.project.session_titles import TitleSource, check_legacy_title_conflict, generated_title_update, legacy_title_condition, title_update_columns
from inqtrix.project.scoped_upsert import (
    ResourceScope,
    delete_scoped_postgres,
    require_scoped_parent,
    scoped_postgres_upsert,
)
from inqtrix.storage.chat_orm import (
    chat_messages,
    chat_thread_groups,
    chat_threads,
)
from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)


class PostgresChatStore(BaseSessionStore):
    """Durable :class:`~inqtrix.project.chat_ports.ChatStore` over Postgres.

    Inherits the dedicated engine + tenant-scoped session lifecycle from
    :class:`~inqtrix.project.base_session_store.BaseSessionStore`.
    """

    # -- threads ---------------------------------------------------------- #

    async def upsert_thread(
        self,
        *,
        id: str,
        title: str,
        preview: str,
        source: str,
        group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        model_selection: str = "",
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> ChatThread:
        values = dict(
            id=id,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            title=title,
            preview=preview,
            source=source,
            group_id=group_id,
            model_selection=model_selection,
            title_revision=0,
            title_source=title_source,
            created_at=created_at,
            updated_at=updated_at,
        )
        stmt = scoped_postgres_upsert(
            pg_insert(chat_threads),
            chat_threads,
            values,
            # A column missing here is written by the first INSERT and then
            # never again — the failure only shows on the SECOND save.
            ["title", "title_revision", "title_source", "preview", "source", "group_id", "model_selection", "updated_at"],
            column_updates=title_update_columns(
                chat_threads, title=pg_insert(chat_threads).excluded.title,
                revision=title_revision, source=title_source,
            ),
            extra_condition=legacy_title_condition(chat_threads, pg_insert(chat_threads).excluded.title, title_revision),
        ).returning(chat_threads)
        async with self._session() as session:
            if group_id is not None:
                await require_scoped_parent(
                    session,
                    table=chat_thread_groups,
                    parent_id=group_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=ThreadGroupNotFound,
                )
            row = (await session.execute(stmt)).first()
            if row is None:
                if title_revision is None:
                    await check_legacy_title_conflict(session, chat_threads, id, ResourceScope(created_by_user_id, workspace_id), title, tenant_id=_DEFAULT_TENANT)
                raise ThreadNotFound(id)
        return self._thread_from_row(row)

    async def apply_generated_title(
        self, thread_id: str, *, title: str, revision: int, scope: ResourceScope
    ) -> bool:
        async with self._session() as session:
            await require_scoped_parent(session, table=chat_threads, parent_id=thread_id,
                tenant_id=_DEFAULT_TENANT, created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id, not_found=ThreadNotFound)
            result = await session.execute(generated_title_update(
                chat_threads, resource_id=thread_id, tenant_id=_DEFAULT_TENANT,
                scope=scope, title=title, revision=revision,
            ))
            return result.scalar_one_or_none() is not None

    async def list_threads_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ChatThread], str | None]:
        query = select(chat_threads).where(
            chat_threads.c.tenant_id == _DEFAULT_TENANT
        )
        if created_by_user_id is not None:
            query = query.where(chat_threads.c.created_by_user_id == created_by_user_id)
        if workspace_id is not None:
            query = query.where(chat_threads.c.workspace_id == workspace_id)
        if after is not None:
            query = query.where(
                tuple_(chat_threads.c.created_at, chat_threads.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            chat_threads.c.created_at.desc(),
            chat_threads.c.id.desc(),
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        threads = [self._thread_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(threads[-1].created_at, threads[-1].id)
            if len(rows) > limit and threads
            else None
        )
        return threads, next_cursor

    async def get_thread(self, thread_id: str) -> ChatThread:
        async with self._session() as session:
            row = await self._thread_row(session, thread_id)
        return self._thread_from_row(row)

    async def delete_thread(
        self, thread_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=chat_threads, resource_id=thread_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=ThreadNotFound,
            )

    # -- messages --------------------------------------------------------- #

    async def append_messages(
        self,
        messages: list[ChatMessage],
        *,
        expected_created_by_user_id: uuid.UUID | None,
        expected_workspace_id: str | None,
    ) -> list[ChatMessage]:
        if not messages:
            return []
        values = [
            {
                "id": message.id,
                "thread_id": message.thread_id,
                "tenant_id": _DEFAULT_TENANT,
                "role": message.role,
                "content_markdown": message.content_markdown,
                "metadata": dict(message.metadata),
                "created_at": message.created_at,
            }
            for message in messages
        ]
        # Conflict on the COMPOSITE (thread_id, id): an upsert can only
        # match a row already in the message's own thread, so a re-used id
        # from another thread inserts a fresh row here instead of
        # overwriting the foreign owner's message (the isolation rule the
        # composite PK encodes).
        stmt = pg_insert(chat_messages).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[chat_messages.c.thread_id, chat_messages.c.id],
            set_={
                "role": stmt.excluded.role,
                "content_markdown": stmt.excluded.content_markdown,
                "metadata": stmt.excluded.metadata,
            },
        )
        async with self._session() as session:
            for thread_id in sorted({message.thread_id for message in messages}):
                await require_scoped_parent(
                    session,
                    table=chat_threads,
                    parent_id=thread_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=expected_created_by_user_id,
                    workspace_id=expected_workspace_id,
                    not_found=ThreadNotFound,
                )
            await session.execute(stmt)
        return messages

    async def delete_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        expected_created_by_user_id: uuid.UUID | None,
        expected_workspace_id: str | None,
    ) -> None:
        async with self._session() as session:
            await require_scoped_parent(
                session,
                table=chat_threads,
                parent_id=thread_id,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=expected_created_by_user_id,
                workspace_id=expected_workspace_id,
                not_found=ThreadNotFound,
            )
            await session.execute(
                delete(chat_messages).where(
                    chat_messages.c.tenant_id == _DEFAULT_TENANT,
                    chat_messages.c.thread_id == thread_id,
                    chat_messages.c.id == message_id,
                )
            )

    async def list_messages_page(
        self,
        thread_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ChatMessage], str | None]:
        query = select(chat_messages).where(
            chat_messages.c.tenant_id == _DEFAULT_TENANT,
            chat_messages.c.thread_id == thread_id,
        )
        if after is not None:
            query = query.where(
                tuple_(chat_messages.c.created_at, chat_messages.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            chat_messages.c.created_at.desc(),
            chat_messages.c.id.desc(),
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        items = [self._message_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    # -- groups ----------------------------------------------------------- #

    async def upsert_group(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> ChatThreadGroup:
        values = dict(
            id=id,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
        )
        stmt = scoped_postgres_upsert(
            pg_insert(chat_thread_groups),
            chat_thread_groups,
            values,
            ["title", "updated_at"],
        ).returning(chat_thread_groups)
        async with self._session() as session:
            row = (await session.execute(stmt)).first()
            if row is None:
                raise ThreadGroupNotFound(id)
        return self._group_from_row(row)

    async def list_groups(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[ChatThreadGroup]:
        query = select(chat_thread_groups).where(
            chat_thread_groups.c.tenant_id == _DEFAULT_TENANT
        )
        if created_by_user_id is not None:
            query = query.where(
                chat_thread_groups.c.created_by_user_id == created_by_user_id
            )
        if workspace_id is not None:
            query = query.where(
                chat_thread_groups.c.workspace_id == workspace_id
            )
        query = query.order_by(
            chat_thread_groups.c.created_at.desc(),
            chat_thread_groups.c.id.desc(),
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [self._group_from_row(row) for row in rows]

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=chat_thread_groups, resource_id=group_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=ThreadGroupNotFound,
            )

    # -- row mapping ------------------------------------------------------ #

    async def _thread_row(self, session, thread_id: str):
        row = (
            await session.execute(
                select(chat_threads).where(
                    chat_threads.c.tenant_id == _DEFAULT_TENANT,
                    chat_threads.c.id == thread_id,
                )
            )
        ).first()
        if row is None:
            raise ThreadNotFound(thread_id)
        return row

    @staticmethod
    def _thread_from_row(row) -> ChatThread:
        return ChatThread(
            id=row.id,
            title=row.title,
            title_revision=row.title_revision,
            title_source=row.title_source,
            preview=row.preview,
            source=row.source,
            group_id=row.group_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
            model_selection=row.model_selection,
        )

    @staticmethod
    def _group_from_row(row) -> ChatThreadGroup:
        return ChatThreadGroup(
            id=row.id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
        )

    @staticmethod
    def _message_from_row(row) -> ChatMessage:
        return ChatMessage(
            id=row.id,
            thread_id=row.thread_id,
            role=row.role,
            content_markdown=row.content_markdown,
            metadata=dict(row.metadata or {}),
            created_at=row.created_at,
        )
