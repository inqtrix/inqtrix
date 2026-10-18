"""In-memory editor-persistence store (the tier without Postgres).

The fallback when ``INQTRIX_STORAGE_BACKEND`` is not ``postgres`` and the
offline test backend for the port contract. Mirrors the visibility and
keyset semantics of :class:`~inqtrix.project.editor_postgres.PostgresEditorStore`
byte-for-byte (filter before slice; reuse :func:`~inqtrix.pagination.keyset_page`),
INCLUDING that ``list_documents_page`` returns documents with an empty body
(the body is loaded only via :meth:`get_document`). Process-local and not
durable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from inqtrix.auth.permissions import AccessMode, ResourceAccess
from inqtrix.pagination import keyset_page
from inqtrix.project.editor_ports import (
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
)
from inqtrix.project.scoped_upsert import ResourceScope, require_memory_scope


class MemoryEditorStore:
    """Process-local :class:`~inqtrix.project.editor_ports.EditorStore`."""

    def __init__(self) -> None:
        self._documents: dict[str, EditorDocument] = {}
        self._folders: dict[str, EditorFolder] = {}
        # Keyed by the COMPOSITE (document_id, id), mirroring the Postgres PK.
        self._comments: dict[tuple[str, str], EditorComment] = {}

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
        if folder_id is not None:
            require_memory_scope(
                self._folders.get(folder_id),
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=folder_id,
                not_found=FolderNotFound,
            )
        existing = self._documents.get(id)
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=DocumentNotFound,
            )
            if existing.revision != revision - 1:
                # Revision CAS, wire-identical to the Postgres store:
                # the stored revision must be EXACTLY the writer's base
                # (incoming is base+1). A stale base — a writer that never
                # saw a concurrent agent patch or peer edit — conflicts and
                # rebases instead of clobbering with a higher counter.
                raise DocumentRevisionConflict(
                    current_revision=existing.revision,
                    expected_revision=revision - 1,
                )
            document = replace(
                existing,
                title=title,
                content_markdown=content_markdown,
                folder_id=folder_id,
                source=source,
                source_run_id=source_run_id,
                revision=revision,
                diff_anchor_markdown=diff_anchor_markdown,
                diff_anchor_updated_at=diff_anchor_updated_at,
                updated_at=updated_at,
            )
        else:
            document = EditorDocument(
                id=id,
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
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        self._documents[id] = document
        return document

    async def list_documents_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorDocument], str | None]:
        items = list(self._documents.values())
        if created_by_user_id is not None:
            items = [d for d in items if d.created_by_user_id == created_by_user_id]
        if workspace_id is not None:
            items = [d for d in items if d.workspace_id == workspace_id]
        items.sort(key=lambda d: (d.created_at, d.id), reverse=True)
        page, cursor = keyset_page(
            items,
            limit=limit,
            after=after,
            created_at_of=lambda d: d.created_at,
            id_of=lambda d: d.id,
        )
        # Metadata only — the body loads via get_document.
        return [replace(d, content_markdown="") for d in page], cursor

    async def list_visible_documents_page(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        workspace_id: str | None,
        scope: Literal["owned", "shared", "all"],
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[tuple[EditorDocument, ResourceAccess]], str | None]:
        """Memory deployments have no shareable collaboration documents."""
        if scope == "shared":
            return [], None
        documents, cursor = await self.list_documents_page(
            created_by_user_id=actor_user_id,
            workspace_id=workspace_id,
            limit=limit,
            after=after,
        )
        mode = (
            AccessMode.OWNER
            if actor_user_id is not None
            else AccessMode.UNSCOPED
        )
        return [
            (document, ResourceAccess(mode)) for document in documents
        ], cursor

    async def get_document(self, document_id: str) -> EditorDocument:
        try:
            return self._documents[document_id]
        except KeyError as exc:
            raise DocumentNotFound(document_id) from exc

    async def get_document_facts(
        self, document_ids: Sequence[str]
    ) -> dict[str, EditorDocumentFacts]:
        return {
            document.id: EditorDocumentFacts(
                id=document.id,
                tenant_id=document.tenant_id,
                created_by_user_id=document.created_by_user_id,
                title=document.title,
                content_mode=document.content_mode,
                collaboration_generation=document.collaboration_generation,
                deleted_at=document.deleted_at,
            )
            for document_id in document_ids
            if (document := self._documents.get(document_id)) is not None
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
        existing = require_memory_scope(
            self._documents.get(document_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=document_id,
            not_found=DocumentNotFound,
        )
        if set_folder_id and folder_id is not None:
            require_memory_scope(
                self._folders.get(folder_id),
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                resource_id=folder_id,
                not_found=FolderNotFound,
            )
        if existing.metadata_revision != expected_metadata_revision:
            raise DocumentMetadataConflict(
                current_revision=existing.metadata_revision
            )
        changes = {
            "metadata_revision": expected_metadata_revision + 1,
            "updated_at": updated_at,
        }
        if title is not None:
            changes["title"] = title
        if set_folder_id:
            changes["folder_id"] = folder_id
        if set_diff_anchor_markdown:
            changes["diff_anchor_markdown"] = diff_anchor_markdown
        if set_diff_anchor_updated_at:
            changes["diff_anchor_updated_at"] = diff_anchor_updated_at
        stored = replace(existing, **changes)
        self._documents[document_id] = stored
        return stored

    async def delete_document(
        self, document_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._documents.get(document_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=document_id,
            not_found=DocumentNotFound,
        )
        self._documents.pop(document_id, None)
        self._comments = {
            key: comment
            for key, comment in self._comments.items()
            if comment.document_id != document_id
        }

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
        existing = self._folders.get(id)
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=FolderNotFound,
            )
            folder = replace(existing, title=title, updated_at=updated_at)
        else:
            folder = EditorFolder(
                id=id,
                title=title,
                created_at=created_at,
                updated_at=updated_at,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        self._folders[id] = folder
        return folder

    async def list_folders(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[EditorFolder]:
        items = list(self._folders.values())
        if created_by_user_id is not None:
            items = [f for f in items if f.created_by_user_id == created_by_user_id]
        if workspace_id is not None:
            items = [f for f in items if f.workspace_id == workspace_id]
        items.sort(key=lambda f: (f.created_at, f.id), reverse=True)
        return items

    async def delete_folder(
        self, folder_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._folders.get(folder_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=folder_id,
            not_found=FolderNotFound,
        )
        self._folders.pop(folder_id, None)
        for did, document in list(self._documents.items()):
            if document.folder_id == folder_id:
                self._documents[did] = replace(document, folder_id=None)

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
        document = require_memory_scope(
            self._documents.get(expected_document_id),
            created_by_user_id=expected_document_owner_user_id,
            workspace_id=expected_document_workspace_id,
            resource_id=expected_document_id,
            not_found=DocumentNotFound,
        )
        if document.content_mode != expected_document_content_mode:
            raise DocumentNotFound(expected_document_id)
        for comment in comments:
            if (
                comment.document_id != expected_document_id
                or comment.created_by_user_id != actor_user_id
            ):
                raise DocumentNotFound(expected_document_id)
            existing = self._comments.get((comment.document_id, comment.id))
            if (
                existing is not None
                and existing.created_by_user_id != comment.created_by_user_id
            ):
                raise DocumentNotFound(comment.document_id)
        stored: list[EditorComment] = []
        for comment in comments:
            key = (comment.document_id, comment.id)
            existing = self._comments.get(key)
            if existing is not None:
                merged = replace(
                    existing,
                    comment_markdown=comment.comment_markdown,
                    anchor=dict(comment.anchor),
                    kind=comment.kind,
                    status=comment.status,
                    evidence_preset=comment.evidence_preset,
                    updated_at=comment.updated_at,
                )
            else:
                merged = replace(comment, anchor=dict(comment.anchor))
            self._comments[key] = merged
            stored.append(merged)
        return stored

    async def list_comments_page(
        self,
        document_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorComment], str | None]:
        items = [
            comment
            for comment in self._comments.values()
            if comment.document_id == document_id
            and (
                created_by_user_id is None
                or comment.created_by_user_id == created_by_user_id
            )
        ]
        items.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return keyset_page(
            items,
            limit=limit,
            after=after,
            created_at_of=lambda c: c.created_at,
            id_of=lambda c: c.id,
        )

    async def get_comment(
        self,
        document_id: str,
        comment_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
    ) -> EditorComment:
        comment = self._comments.get((document_id, comment_id))
        if comment is None or (
            created_by_user_id is not None
            and comment.created_by_user_id != created_by_user_id
        ):
            raise SuggestionDraftNotFound(comment_id)
        return comment

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
        document = require_memory_scope(
            self._documents.get(document_id),
            created_by_user_id=expected_document_owner_user_id,
            workspace_id=expected_document_workspace_id,
            resource_id=document_id,
            not_found=DocumentNotFound,
        )
        if document.content_mode != expected_document_content_mode:
            raise DocumentNotFound(document_id)
        if (
            expected_document_content_mode == "collaboration"
            and created_by_user_id != actor_user_id
        ):
            raise DocumentNotFound(document_id)
        key = (document_id, comment_id)
        comment = self._comments.get(key)
        if comment is None:
            return
        if (
            created_by_user_id is not None
            and comment.created_by_user_id != created_by_user_id
        ):
            return
        self._comments.pop(key, None)

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
        document = require_memory_scope(
            self._documents.get(document_id),
            created_by_user_id=expected_document_owner_user_id,
            workspace_id=expected_document_workspace_id,
            resource_id=document_id,
            not_found=DocumentNotFound,
        )
        if (
            document.content_mode != expected_document_content_mode
            or expected_document_content_mode != "collaboration"
        ):
            raise DocumentNotFound(document_id)
        comment = self._comments.get((document_id, comment_id))
        if comment is None or comment.created_by_user_id != actor_user_id:
            raise SuggestionDraftNotFound(comment_id)
        current_revision = (
            comment.suggestion_draft.revision
            if comment.suggestion_draft is not None
            else 0
        )
        if current_revision != expected_revision:
            raise SuggestionDraftRevisionConflict(
                current_revision=current_revision
            )
        if draft.revision != expected_revision + 1:
            raise SuggestionDraftRevisionConflict(
                current_revision=current_revision
            )
        self._comments[(document_id, comment_id)] = replace(
            comment,
            suggestion_draft=draft,
        )
        return draft

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
        document = require_memory_scope(
            self._documents.get(document_id),
            created_by_user_id=expected_document_owner_user_id,
            workspace_id=expected_document_workspace_id,
            resource_id=document_id,
            not_found=DocumentNotFound,
        )
        if (
            document.content_mode != expected_document_content_mode
            or expected_document_content_mode != "collaboration"
        ):
            raise DocumentNotFound(document_id)
        comment = self._comments.get((document_id, comment_id))
        if comment is None or comment.created_by_user_id != actor_user_id:
            raise SuggestionDraftNotFound(comment_id)
        draft = comment.suggestion_draft
        current_revision = draft.revision if draft is not None else 0
        if (
            draft is None
            or draft.patch_id != patch_id
            or current_revision != expected_revision
        ):
            raise SuggestionDraftRevisionConflict(
                current_revision=current_revision
            )
        self._comments[(document_id, comment_id)] = replace(
            comment,
            suggestion_draft=None,
        )

    async def aclose(self) -> None:
        return None
