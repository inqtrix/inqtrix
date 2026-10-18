"""Postgres-backed editor-persistence store (durable project tier).

Documents, folders, and comments persist relationally, scoped per
``(tenant_id, created_by_user_id, workspace_id)`` with RLS + ``tenant_session``
+ ON CONFLICT autosave — identical discipline to ``chat_postgres.py``.

Editor specifics:

* ``list_documents_page`` SELECTs metadata columns only (NOT the heavy
  ``content_markdown`` body); ``get_document`` SELECTs the full row with the
  body (load-on-open).
* Comments are a diffed collection (upsert + delete + list), not append-only.

The engine is its own NullPool engine (loop-agnostic), awaited from the
HTTP loop — the same rule the chat/knowledge stores document.
"""

from __future__ import annotations

import uuid

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from sqlalchemy import (
    Integer,
    Text,
    and_,
    any_,
    bindparam,
    delete,
    null,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from inqtrix.auth.permissions import AccessMode, ResourceAccess
from inqtrix.pagination import encode_cursor
from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
from inqtrix.project.editor_ports import (
    comment_write_permission,
    DocumentContentModeConflict,
    DocumentMetadataConflict,
    DocumentNotFound,
    DocumentRevisionConflict,
    EditorComment,
    EditorDocument,
    EditorDocumentFacts,
    EditorFolder,
    EditorSuggestionDraft,
    FolderNotFound,
    SuggestionDraftNotFound,
    SuggestionDraftRevisionConflict,
    suggestion_draft_from_payload,
    suggestion_draft_payload,
)
from inqtrix.project.scoped_upsert import (
    ResourceScope,
    delete_scoped_postgres,
    require_scoped_parent,
    scoped_postgres_upsert,
)
from inqtrix.storage.editor_orm import (
    editor_comments,
    editor_documents,
    editor_folders,
)
from inqtrix.storage.resource_access import (
    VISIBLE_SHARE_PERMISSION,
    append_resource_effects,
    listed_resource_access,
    lock_resource_access,
    revoke_resource_shares,
    visible_resource_select,
)

log = logging.getLogger("inqtrix")

# Document metadata columns (everything EXCEPT the heavy content_markdown
# body) for the list path — the body is transferred only on get_document.
_DOC_META_COLUMNS = (
    editor_documents.c.id,
    editor_documents.c.tenant_id,
    editor_documents.c.created_by_user_id,
    editor_documents.c.workspace_id,
    editor_documents.c.title,
    editor_documents.c.folder_id,
    editor_documents.c.source,
    editor_documents.c.source_run_id,
    editor_documents.c.revision,
    editor_documents.c.content_mode,
    editor_documents.c.metadata_revision,
    editor_documents.c.collaboration_generation,
    editor_documents.c.collaboration_schema_version,
    editor_documents.c.collaboration_schema_hash,
    editor_documents.c.persisted_sequence,
    editor_documents.c.projection_sequence,
    editor_documents.c.projection_updated_at,
    editor_documents.c.collaboration_comment_revision,
    editor_documents.c.deleted_at,
    editor_documents.c.diff_anchor_markdown,
    editor_documents.c.diff_anchor_updated_at,
    editor_documents.c.created_at,
    editor_documents.c.updated_at,
)

# The body-free facts that decide access and address a collaboration room.
# Neither Markdown column (content_markdown, diff_anchor_markdown) is read.
_DOC_FACT_COLUMNS = (
    editor_documents.c.id,
    editor_documents.c.tenant_id,
    editor_documents.c.created_by_user_id,
    editor_documents.c.title,
    editor_documents.c.content_mode,
    editor_documents.c.collaboration_generation,
    editor_documents.c.deleted_at,
)


class PostgresEditorStore(BaseSessionStore):
    """Durable :class:`~inqtrix.project.editor_ports.EditorStore` over Postgres.

    Inherits the dedicated engine + tenant-scoped session lifecycle from
    :class:`~inqtrix.project.base_session_store.BaseSessionStore`.
    """

    def __init__(
        self,
        *,
        engine,
        app_role: str,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
    ) -> None:
        super().__init__(engine=engine, app_role=app_role)
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled

    @property
    def atomic_delete_resource_effects(self) -> bool:
        """Whether deletions include audit and invalidations atomically."""
        return True

    # -- documents -------------------------------------------------------- #

    async def upsert_document(
        self,
        *,
        id: str,
        title: str,
        content_markdown: str,
        folder_id: str | None,
        source: str,
        source_run_id: str | None,
        revision: int,
        diff_anchor_markdown: str | None,
        diff_anchor_updated_at: float | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> EditorDocument:
        values = dict(
            id=id,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            title=title,
            content_markdown=content_markdown,
            folder_id=folder_id,
            source=source,
            source_run_id=source_run_id,
            revision=revision,
            diff_anchor_markdown=diff_anchor_markdown,
            diff_anchor_updated_at=diff_anchor_updated_at,
            created_at=created_at,
            updated_at=updated_at,
        )
        insert_stmt = pg_insert(editor_documents)
        stmt = scoped_postgres_upsert(
            insert_stmt,
            editor_documents,
            values,
            [
                "title",
                "content_markdown",
                "folder_id",
                "source",
                "source_run_id",
                "revision",
                "diff_anchor_markdown",
                "diff_anchor_updated_at",
                "updated_at",
            ],
            # Revision CAS: the update fires only when the stored
            # revision is EXACTLY the writer's base — i.e. the incoming
            # revision is base+1. The client now tracks `revision` as the
            # last-synced SERVER revision (its base) and sends base+1, so
            # a writer whose base is stale (it never saw a concurrent
            # agent patch or peer edit) fails the CAS and gets a 409 to
            # rebase, instead of silently clobbering with a higher counter.
            # This is the same "stored == expected" contract the agent
            # patch path already enforces. The brand-new-id INSERT branch
            # is unaffected (no conflict, no WHERE).
            extra_condition=(
                (editor_documents.c.revision == insert_stmt.excluded.revision - 1)
                & (editor_documents.c.content_mode == "markdown")
                & editor_documents.c.deleted_at.is_(None)
            ),
        ).returning(editor_documents)
        async with self._session() as session:
            if folder_id is not None:
                await require_scoped_parent(
                    session,
                    table=editor_folders,
                    parent_id=folder_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=FolderNotFound,
                )
            row = (await session.execute(stmt)).first()
            if row is None:
                # Conflict fired but the WHERE suppressed the update:
                # the row exists at a different revision than the
                # writer's base. Read it for the client's rebase.
                current = (
                    await session.execute(
                        select(
                            editor_documents.c.revision,
                            editor_documents.c.content_mode,
                            editor_documents.c.deleted_at,
                        ).where(
                            editor_documents.c.id == id,
                            editor_documents.c.tenant_id == _DEFAULT_TENANT,
                            editor_documents.c.created_by_user_id.is_not_distinct_from(
                                created_by_user_id
                            ),
                            editor_documents.c.workspace_id.is_not_distinct_from(
                                workspace_id
                            ),
                        )
                    )
                ).one_or_none()
                if current is None or current.deleted_at is not None:
                    raise DocumentNotFound(id)
                if current.content_mode == "collaboration":
                    raise DocumentContentModeConflict(id)
                log.warning(
                    "Editor document %s: revision CAS failed (stored=%s, writer "
                    "base=%d); write discarded, and the client must rebase.",
                    id,
                    current.revision,
                    revision - 1,
                )
                raise DocumentRevisionConflict(
                    current_revision=int(current.revision),
                    expected_revision=revision - 1,
                )
        return self._document_from_row(row)

    async def list_documents_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorDocument], str | None]:
        query = select(*_DOC_META_COLUMNS).where(
            editor_documents.c.tenant_id == _DEFAULT_TENANT,
            editor_documents.c.deleted_at.is_(None),
        )
        if created_by_user_id is not None:
            query = query.where(editor_documents.c.created_by_user_id == created_by_user_id)
        if workspace_id is not None:
            query = query.where(editor_documents.c.workspace_id == workspace_id)
        if after is not None:
            query = query.where(
                tuple_(editor_documents.c.created_at, editor_documents.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            editor_documents.c.created_at.desc(),
            editor_documents.c.id.desc(),
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        documents = [self._document_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(documents[-1].created_at, documents[-1].id)
            if len(rows) > limit and documents
            else None
        )
        return documents, next_cursor

    async def list_visible_documents_page(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        workspace_id: str | None,
        scope: Literal["owned", "shared", "all"],
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[tuple[EditorDocument, ResourceAccess]], str | None]:
        """List metadata through the existing live direct-share boundary."""
        if scope not in {"owned", "shared", "all"}:
            raise ValueError("scope must be owned, shared, or all")
        if actor_user_id is None:
            if scope == "shared":
                return [], None
            documents, cursor = await self.list_documents_page(
                created_by_user_id=None,
                workspace_id=workspace_id,
                limit=limit,
                after=after,
            )
            return [
                (document, ResourceAccess(AccessMode.UNSCOPED))
                for document in documents
            ], cursor

        visible = visible_resource_select(
            resource_table=editor_documents,
            id_column=editor_documents.c.id,
            owner_column=editor_documents.c.created_by_user_id,
            resource_type="editor_document",
            tenant_id=_DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        )
        permission_column = visible.selected_columns[VISIBLE_SHARE_PERMISSION]
        query = visible.with_only_columns(
            *_DOC_META_COLUMNS,
            permission_column,
            maintain_column_froms=True,
        ).where(editor_documents.c.deleted_at.is_(None))
        owned_filter = editor_documents.c.created_by_user_id == actor_user_id
        if workspace_id is not None:
            owned_filter = and_(
                owned_filter,
                editor_documents.c.workspace_id == workspace_id,
            )
        shared_filter = and_(
            editor_documents.c.created_by_user_id != actor_user_id,
            permission_column.is_not(None),
            editor_documents.c.content_mode == "collaboration",
        )
        if scope == "owned":
            query = query.where(owned_filter)
        elif scope == "shared":
            query = query.where(shared_filter)
        else:
            query = query.where(or_(owned_filter, shared_filter))
        if after is not None:
            query = query.where(
                tuple_(editor_documents.c.created_at, editor_documents.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            editor_documents.c.created_at.desc(),
            editor_documents.c.id.desc(),
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        items: list[tuple[EditorDocument, ResourceAccess]] = []
        for row in rows[:limit]:
            access = listed_resource_access(
                owner_user_id=row.created_by_user_id,
                actor_user_id=actor_user_id,
                share_permission=getattr(row, VISIBLE_SHARE_PERMISSION),
            )
            document = self._document_from_row(row)
            if access.mode is AccessMode.SHARED:
                document = replace(document, folder_id=None)
            items.append((document, access))
        next_cursor = (
            encode_cursor(items[-1][0].created_at, items[-1][0].id)
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    async def get_document(self, document_id: str) -> EditorDocument:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(editor_documents).where(
                        editor_documents.c.tenant_id == _DEFAULT_TENANT,
                        editor_documents.c.id == document_id,
                        editor_documents.c.deleted_at.is_(None),
                    )
                )
            ).first()
        if row is None:
            raise DocumentNotFound(document_id)
        return self._document_from_row(row)

    async def get_document_facts(
        self, document_ids: Sequence[str]
    ) -> dict[str, EditorDocumentFacts]:
        wanted = sorted(set(document_ids))
        if not wanted:
            return {}
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(*_DOC_FACT_COLUMNS).where(
                        editor_documents.c.tenant_id == _DEFAULT_TENANT,
                        editor_documents.c.id
                        == any_(bindparam("document_ids", wanted, type_=ARRAY(Text))),
                        editor_documents.c.deleted_at.is_(None),
                    )
                )
            ).all()
        return {
            row.id: EditorDocumentFacts(
                id=row.id,
                tenant_id=row.tenant_id,
                created_by_user_id=row.created_by_user_id,
                title=row.title,
                content_mode=row.content_mode,
                collaboration_generation=int(row.collaboration_generation),
                deleted_at=row.deleted_at,
            )
            for row in rows
        }

    async def patch_document_metadata(
        self,
        *,
        document_id: str,
        expected_metadata_revision: int,
        title: str | None,
        folder_id: str | None,
        set_folder_id: bool,
        diff_anchor_markdown: str | None,
        set_diff_anchor_markdown: bool,
        diff_anchor_updated_at: float | None,
        set_diff_anchor_updated_at: bool,
        updated_at: float,
        scope: ResourceScope,
    ) -> EditorDocument:
        values: dict[str, object] = {
            "metadata_revision": expected_metadata_revision + 1,
            "updated_at": updated_at,
        }
        if title is not None:
            values["title"] = title
        if set_folder_id:
            values["folder_id"] = folder_id
        if set_diff_anchor_markdown:
            values["diff_anchor_markdown"] = diff_anchor_markdown
        if set_diff_anchor_updated_at:
            values["diff_anchor_updated_at"] = diff_anchor_updated_at
        async with self._session() as session:
            if set_folder_id and folder_id is not None:
                await require_scoped_parent(
                    session,
                    table=editor_folders,
                    parent_id=folder_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    not_found=FolderNotFound,
                )
            row = (
                await session.execute(
                    update(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == _DEFAULT_TENANT,
                        editor_documents.c.id == document_id,
                        editor_documents.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        editor_documents.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                        editor_documents.c.metadata_revision
                        == expected_metadata_revision,
                        editor_documents.c.deleted_at.is_(None),
                    )
                    .values(**values)
                    .returning(editor_documents)
                )
            ).one_or_none()
            if row is None:
                current = (
                    await session.execute(
                        select(
                            editor_documents.c.metadata_revision,
                            editor_documents.c.deleted_at,
                        ).where(
                            editor_documents.c.tenant_id == _DEFAULT_TENANT,
                            editor_documents.c.id == document_id,
                            editor_documents.c.created_by_user_id.is_not_distinct_from(
                                scope.created_by_user_id
                            ),
                            editor_documents.c.workspace_id.is_not_distinct_from(
                                scope.workspace_id
                            ),
                        )
                    )
                ).one_or_none()
                if current is None or current.deleted_at is not None:
                    raise DocumentNotFound(document_id)
                raise DocumentMetadataConflict(
                    current_revision=int(current.metadata_revision)
                )
        return self._document_from_row(row)

    async def delete_document(
        self, document_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            recipients = await revoke_resource_shares(
                session,
                tenant_id=_DEFAULT_TENANT,
                resource_type="editor_document",
                resource_id=document_id,
                revoked_by_user_id=scope.created_by_user_id,
            )
            await delete_scoped_postgres(
                session,
                table=editor_documents,
                resource_id=document_id,
                tenant_id=_DEFAULT_TENANT,
                scope=scope,
                not_found=DocumentNotFound,
                extra_condition=and_(
                    editor_documents.c.content_mode == "markdown",
                    editor_documents.c.deleted_at.is_(None),
                ),
            )
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=scope.created_by_user_id,
                owner_user_id=scope.created_by_user_id,
                action="editor_document.deleted",
                resource_type="editor_document",
                resource_id=document_id,
                scope="editor_documents",
                additional_targets=recipients,
            )

    # -- folders ---------------------------------------------------------- #

    async def upsert_folder(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> EditorFolder:
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
            pg_insert(editor_folders),
            editor_folders,
            values,
            ["title", "updated_at"],
        ).returning(editor_folders)
        async with self._session() as session:
            row = (await session.execute(stmt)).first()
            if row is None:
                raise FolderNotFound(id)
        return self._folder_from_row(row)

    async def list_folders(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[EditorFolder]:
        query = select(editor_folders).where(
            editor_folders.c.tenant_id == _DEFAULT_TENANT
        )
        if created_by_user_id is not None:
            query = query.where(editor_folders.c.created_by_user_id == created_by_user_id)
        if workspace_id is not None:
            query = query.where(editor_folders.c.workspace_id == workspace_id)
        query = query.order_by(
            editor_folders.c.created_at.desc(),
            editor_folders.c.id.desc(),
        )
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [self._folder_from_row(row) for row in rows]

    async def delete_folder(
        self, folder_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=editor_folders, resource_id=folder_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=FolderNotFound,
            )

    # -- comments --------------------------------------------------------- #

    async def upsert_comments(
        self,
        comments: list[EditorComment],
        *,
        expected_document_id: str,
        expected_document_owner_user_id: uuid.UUID | None,
        expected_document_workspace_id: str | None,
        expected_document_content_mode: str,
        actor_user_id: uuid.UUID | None,
    ) -> list[EditorComment]:
        if not comments:
            return []
        if any(
            comment.document_id != expected_document_id
            or comment.created_by_user_id != actor_user_id
            for comment in comments
        ):
            raise DocumentNotFound(expected_document_id)
        values = [
            {
                "id": comment.id,
                "document_id": comment.document_id,
                "tenant_id": _DEFAULT_TENANT,
                "created_by_user_id": comment.created_by_user_id,
                "comment_markdown": comment.comment_markdown,
                "anchor": dict(comment.anchor),
                "kind": comment.kind,
                "status": comment.status,
                "evidence_preset": comment.evidence_preset,
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
            }
            for comment in comments
        ]
        stmt = pg_insert(editor_comments).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[editor_comments.c.document_id, editor_comments.c.id],
            set_={
                "comment_markdown": stmt.excluded.comment_markdown,
                "anchor": stmt.excluded.anchor,
                "kind": stmt.excluded.kind,
                "status": stmt.excluded.status,
                "evidence_preset": stmt.excluded.evidence_preset,
                "updated_at": stmt.excluded.updated_at,
            },
            where=editor_comments.c.created_by_user_id.is_not_distinct_from(
                stmt.excluded.created_by_user_id
            ),
        ).returning(editor_comments)
        async with self._session() as session:
            await self._lock_comment_parent_authority(
                session,
                document_id=expected_document_id,
                expected_owner_user_id=expected_document_owner_user_id,
                expected_workspace_id=expected_document_workspace_id,
                expected_content_mode=expected_document_content_mode,
                actor_user_id=actor_user_id,
            )
            stored_rows = (await session.execute(stmt)).all()
            if len(stored_rows) != len(comments):
                raise DocumentNotFound(expected_document_id)
        return [self._comment_from_row(row) for row in stored_rows]

    async def list_comments_page(
        self,
        document_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorComment], str | None]:
        query = select(editor_comments).where(
            editor_comments.c.tenant_id == _DEFAULT_TENANT,
            editor_comments.c.document_id == document_id,
        )
        if created_by_user_id is not None:
            query = query.where(
                editor_comments.c.created_by_user_id == created_by_user_id
            )
        if after is not None:
            query = query.where(
                tuple_(editor_comments.c.created_at, editor_comments.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            editor_comments.c.created_at.desc(),
            editor_comments.c.id.desc(),
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        items = [self._comment_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    async def get_comment(
        self,
        document_id: str,
        comment_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
    ) -> EditorComment:
        predicates = [
            editor_comments.c.tenant_id == _DEFAULT_TENANT,
            editor_comments.c.document_id == document_id,
            editor_comments.c.id == comment_id,
        ]
        if created_by_user_id is not None:
            predicates.append(
                editor_comments.c.created_by_user_id == created_by_user_id
            )
        async with self._session() as session:
            row = (
                await session.execute(
                    select(editor_comments).where(*predicates)
                )
            ).one_or_none()
        if row is None:
            raise SuggestionDraftNotFound(comment_id)
        return self._comment_from_row(row)

    async def delete_comment(
        self,
        *,
        document_id: str,
        comment_id: str,
        created_by_user_id: uuid.UUID | None = None,
        expected_document_owner_user_id: uuid.UUID | None,
        expected_document_workspace_id: str | None,
        expected_document_content_mode: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        predicates = [
            editor_comments.c.tenant_id == _DEFAULT_TENANT,
            editor_comments.c.document_id == document_id,
            editor_comments.c.id == comment_id,
        ]
        if created_by_user_id is not None:
            predicates.append(
                editor_comments.c.created_by_user_id == created_by_user_id
            )
        async with self._session() as session:
            await self._lock_comment_parent_authority(
                session,
                document_id=document_id,
                expected_owner_user_id=expected_document_owner_user_id,
                expected_workspace_id=expected_document_workspace_id,
                expected_content_mode=expected_document_content_mode,
                actor_user_id=actor_user_id,
            )
            await session.execute(
                delete(editor_comments).where(*predicates)
            )

    async def save_comment_suggestion_draft(
        self,
        *,
        document_id: str,
        comment_id: str,
        draft: EditorSuggestionDraft,
        expected_revision: int,
        expected_document_owner_user_id: uuid.UUID | None,
        expected_document_workspace_id: str | None,
        expected_document_content_mode: str,
        actor_user_id: uuid.UUID | None,
    ) -> EditorSuggestionDraft:
        revision_value = editor_comments.c.suggestion_draft[
            "revision"
        ].astext.cast(Integer)
        revision_guard = (
            editor_comments.c.suggestion_draft.is_(None)
            if expected_revision == 0
            else revision_value == expected_revision
        )
        async with self._session() as session:
            await self._lock_comment_parent_authority(
                session,
                document_id=document_id,
                expected_owner_user_id=expected_document_owner_user_id,
                expected_workspace_id=expected_document_workspace_id,
                expected_content_mode=expected_document_content_mode,
                actor_user_id=actor_user_id,
            )
            stored = (
                await session.execute(
                    update(editor_comments)
                    .where(
                        editor_comments.c.tenant_id == _DEFAULT_TENANT,
                        editor_comments.c.document_id == document_id,
                        editor_comments.c.id == comment_id,
                        editor_comments.c.created_by_user_id.is_not_distinct_from(
                            actor_user_id
                        ),
                        revision_guard,
                    )
                    .values(suggestion_draft=suggestion_draft_payload(draft))
                    .returning(editor_comments.c.suggestion_draft)
                )
            ).scalar_one_or_none()
            if stored is None:
                await self._raise_suggestion_draft_cas_miss(
                    session,
                    document_id=document_id,
                    comment_id=comment_id,
                    actor_user_id=actor_user_id,
                )
                raise RuntimeError("unreachable suggestion draft CAS state")
        decoded = suggestion_draft_from_payload(stored)
        if decoded is None:
            raise RuntimeError("stored suggestion draft unexpectedly absent")
        return decoded

    async def delete_comment_suggestion_draft(
        self,
        *,
        document_id: str,
        comment_id: str,
        expected_revision: int,
        patch_id: str,
        expected_document_owner_user_id: uuid.UUID | None,
        expected_document_workspace_id: str | None,
        expected_document_content_mode: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        revision_value = editor_comments.c.suggestion_draft[
            "revision"
        ].astext.cast(Integer)
        patch_value = editor_comments.c.suggestion_draft["patch_id"].astext
        async with self._session() as session:
            await self._lock_comment_parent_authority(
                session,
                document_id=document_id,
                expected_owner_user_id=expected_document_owner_user_id,
                expected_workspace_id=expected_document_workspace_id,
                expected_content_mode=expected_document_content_mode,
                actor_user_id=actor_user_id,
            )
            cleared = (
                await session.execute(
                    update(editor_comments)
                    .where(
                        editor_comments.c.tenant_id == _DEFAULT_TENANT,
                        editor_comments.c.document_id == document_id,
                        editor_comments.c.id == comment_id,
                        editor_comments.c.created_by_user_id.is_not_distinct_from(
                            actor_user_id
                        ),
                        revision_value == expected_revision,
                        patch_value == patch_id,
                    )
                    # SQL NULL, not the JSON ``null`` a bound ``None``
                    # becomes in a JSONB column: the object-only CHECK
                    # rejects the latter, and the revision-0 guard of the
                    # next draft tests for SQL NULL.
                    .values(suggestion_draft=null())
                    .returning(editor_comments.c.id)
                )
            ).scalar_one_or_none()
            if cleared is None:
                await self._raise_suggestion_draft_cas_miss(
                    session,
                    document_id=document_id,
                    comment_id=comment_id,
                    actor_user_id=actor_user_id,
                )
                raise RuntimeError("unreachable suggestion draft CAS state")

    @staticmethod
    async def _raise_suggestion_draft_cas_miss(
        session: AsyncSession,
        *,
        document_id: str,
        comment_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        row = (
            (
                await session.execute(
                    select(
                        editor_comments.c.id,
                        editor_comments.c.suggestion_draft,
                    ).where(
                    editor_comments.c.tenant_id == _DEFAULT_TENANT,
                    editor_comments.c.document_id == document_id,
                    editor_comments.c.id == comment_id,
                    editor_comments.c.created_by_user_id.is_not_distinct_from(
                        actor_user_id
                    ),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SuggestionDraftNotFound(comment_id)
        current = suggestion_draft_from_payload(row["suggestion_draft"])
        raise SuggestionDraftRevisionConflict(
            current_revision=current.revision if current is not None else 0
        )

    async def _lock_comment_parent_authority(
        self,
        session: AsyncSession,
        *,
        document_id: str,
        expected_owner_user_id: uuid.UUID | None,
        expected_workspace_id: str | None,
        expected_content_mode: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Lock the live document and revalidate comment-write authority."""
        access = await lock_resource_access(
            session,
            tenant_id=_DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            resource_type="editor_document",
            resource_table=editor_documents,
            id_column=editor_documents.c.id,
            resource_id=document_id,
            owner_column=editor_documents.c.created_by_user_id,
            minimum=comment_write_permission(expected_content_mode),
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        )
        if access is None or access.owner_user_id != expected_owner_user_id:
            raise DocumentNotFound(document_id)
        parent = (
            await session.execute(
                select(
                    editor_documents.c.workspace_id,
                    editor_documents.c.content_mode,
                ).where(
                    editor_documents.c.tenant_id == _DEFAULT_TENANT,
                    editor_documents.c.id == document_id,
                )
            )
        ).one_or_none()
        if (
            parent is None
            or parent.workspace_id != expected_workspace_id
            or parent.content_mode != expected_content_mode
        ):
            raise DocumentNotFound(document_id)

    # -- row mapping ------------------------------------------------------ #

    @staticmethod
    def _document_from_row(row) -> EditorDocument:
        # content_markdown is absent on metadata-only (list) rows -> "".
        return EditorDocument(
            id=row.id,
            title=row.title,
            content_markdown=getattr(row, "content_markdown", "") or "",
            folder_id=row.folder_id,
            source=row.source,
            source_run_id=row.source_run_id,
            revision=row.revision,
            diff_anchor_markdown=row.diff_anchor_markdown,
            diff_anchor_updated_at=row.diff_anchor_updated_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
            content_mode=row.content_mode,
            metadata_revision=int(row.metadata_revision),
            collaboration_generation=int(row.collaboration_generation),
            collaboration_schema_version=row.collaboration_schema_version,
            collaboration_schema_hash=row.collaboration_schema_hash,
            persisted_sequence=int(row.persisted_sequence),
            projection_sequence=int(row.projection_sequence),
            projection_updated_at=row.projection_updated_at,
            collaboration_comment_revision=int(
                row.collaboration_comment_revision
            ),
            deleted_at=row.deleted_at,
        )

    @staticmethod
    def _folder_from_row(row) -> EditorFolder:
        return EditorFolder(
            id=row.id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
        )

    @staticmethod
    def _comment_from_row(row) -> EditorComment:
        return EditorComment(
            id=row.id,
            document_id=row.document_id,
            comment_markdown=row.comment_markdown,
            anchor=dict(row.anchor or {}),
            kind=row.kind,
            status=row.status,
            evidence_preset=row.evidence_preset,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            suggestion_draft=suggestion_draft_from_payload(
                row.suggestion_draft
            ),
        )
