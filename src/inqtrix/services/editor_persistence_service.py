"""Editor-persistence service (project tier).

The editor counterpart of
:class:`~inqtrix.services.chat_history_service.ChatHistoryService`: payload
validation, the owner-only access rule (
:func:`~inqtrix.auth.permissions.require_owned_access`), and the
"which documents belong to this caller" resolution before the store's
keyset query. Persistence only — it never calls a model.

Ownership model (identical to chat): documents/folders carry
``created_by_user_id``; ``None`` (anonymous/static principals) stays visible
to all. Collaboration documents can additionally be shared through the one
platform share model; comments inherit the parent document's access. Every
denial is the indistinct :class:`DocumentNotFound`/
:class:`FolderNotFound`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from inqtrix.auth.permissions import (
    ResourceAccess,
    ResourceNotFound,
    SharePermission,
    require_owned_access,
)
from inqtrix.auth.shares import ShareFacts
from inqtrix.services.collaboration_client import CollaborationProjection
from inqtrix.services.workspace_guard import deny_cross_workspace
from inqtrix.project.editor_ports import (
    EDITOR_CONTEXT_COMMENT_PAGE_SIZE,
    comment_write_permission,
    DocumentNotFound,
    EditorComment,
    EditorDocument,
    EditorDocumentFacts,
    EditorFolder,
    EditorSuggestionDraft,
    EditorSuggestionDraftRevision,
    EditorStore,
    FolderNotFound,
    SuggestionDraftNotFound,
    SuggestionDraftRevisionConflict,
    suggestion_draft_payload,
)
from inqtrix.project.scoped_upsert import ResourceScope

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.auth.principal import UserContext
    from inqtrix.project.editor_collaboration_ports import EditorCollaborationStore
    from inqtrix.user_events import ResourceInvalidator

_VALID_SOURCES = frozenset(
    {"blank", "imported-research-report", "pasted", "agent-artifact"}
)
# "assistant_edit" ist der Maschinentraeger eines Assistentenlaufs: eine
# creator-private Kommentarzeile, die es nur gibt, damit ein privater Entwurf
# einen Anker hat. Sie ist bewusst KEINE Notiz des Nutzers -- die Notizliste
# und ihre Zaehler zeigen sie nicht, und sie ist nicht erneut ausfuehrbar.
_VALID_COMMENT_KINDS = frozenset(
    {"collect", "inline_edit", "evidence_review", "assistant_edit"}
)
_VALID_COMMENT_STATUSES = frozenset({"open", "resolved", "stale"})
_VALID_EVIDENCE_PRESETS = frozenset({"add_sources", "fact_check", "verify_citations"})
_VALID_DRAFT_REVISION_SOURCES = frozenset({"llm_refine", "manual_edit"})
_VALID_EDIT_POSITIONS = frozenset({"replace", "before", "after", "append"})
_PRIVATE_DRAFT_ANCHOR_VERSION = 1
_MAX_PRIVATE_DRAFT_ANCHOR_TEXT = 4_096
_MAX_PRIVATE_DRAFT_BYTES = 1_048_576
_MAX_PRIVATE_DRAFT_HISTORY = 50
_MAX_PRIVATE_DRAFT_LIST_ITEMS = 50
_MAX_PRIVATE_DRAFT_METADATA_TEXT = 4_096
_MAX_PRIVATE_DRAFT_INSTRUCTION = 8_192
_MAX_PRIVATE_DRAFT_ID = 255

log = logging.getLogger("inqtrix")


class CollaborationProjectionUnavailable(RuntimeError):
    """Raised instead of silently serving stale Markdown to AI consumers."""


class EditorValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400).

    The source / comment kind / status / evidence-preset domains are
    rejected here, before the database CHECK constraint, so a bad value is
    a clean 400 instead of an opaque 500 (No Silent Fallbacks)."""


class SuggestionDraftTooLarge(EditorValidationError):
    """Raised when a private AI draft exceeds its explicit byte budget."""


def _collaboration_shareable(
    document: EditorDocument | EditorDocumentFacts, tenant_id: str
) -> bool:
    """Whether the document can be shared: a live collaboration document of
    the tenant. The share layer reads owner and title only through this."""
    return (
        document.tenant_id == tenant_id
        and document.deleted_at is None
        and document.content_mode == "collaboration"
    )


class EditorPersistenceService:
    """Application service over an :class:`EditorStore`."""

    def __init__(
        self,
        *,
        store: EditorStore,
        durable: bool = False,
        authorization: "AuthorizationService | None" = None,
        collaboration_store: "EditorCollaborationStore | None" = None,
        invalidator: "ResourceInvalidator | None" = None,
    ) -> None:
        self._store = store
        self._durable = durable
        self._authorization = authorization
        self._collaboration_store = collaboration_store
        self._invalidator = invalidator
        self._collaboration_projector: (
            Callable[..., Awaitable[CollaborationProjection]] | None
        ) = None

    @property
    def store(self) -> EditorStore:
        """The wired editor store (shutdown disposes its engine)."""
        return self._store

    @property
    def durable(self) -> bool:
        """Whether the backing store survives a restart."""
        return self._durable

    def bind_collaboration_projector(
        self, projector: Callable[..., Awaitable[CollaborationProjection]]
    ) -> None:
        """Bind the one Node-backed projection barrier during composition."""
        if self._collaboration_projector is not None:
            raise RuntimeError("Collaboration projector is already bound")
        self._collaboration_projector = projector

    # -- documents -------------------------------------------------------- #

    async def save_document(
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
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> EditorDocument:
        """Create or update a document (idempotent autosave).

        A new id is owned by the caller; an existing id must belong to the
        caller and keeps its original owner/workspace.
        """
        if source not in _VALID_SOURCES:
            raise EditorValidationError(f"unknown document source: {source!r}")
        try:
            existing = await self._store.get_document(id)
        except DocumentNotFound:
            existing = None
        if existing is not None:
            await self._resolve_document_access(
                existing,
                visible_to=visible_to,
                minimum=SharePermission.EDIT,
            )
            owner_user_id = existing.created_by_user_id
            owner_workspace = existing.workspace_id
        else:
            owner_user_id = caller_user_id
            owner_workspace = workspace_id
        return await self._store.upsert_document(
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
            created_by_user_id=owner_user_id,
            workspace_id=owner_workspace,
        )

    async def list_documents(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorDocument], str | None]:
        """One keyset page of the caller's documents (metadata only)."""
        return await self._store.list_documents_page(
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
            limit=limit,
            after=after,
        )

    async def list_visible_documents(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        scope: str,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[tuple[EditorDocument, ResourceAccess]], str | None]:
        """List owned and accepted-shared metadata with live access facts."""
        if scope not in {"owned", "shared", "all"}:
            raise EditorValidationError("scope must be owned, shared, or all")
        return await self._store.list_visible_documents_page(
            actor_user_id=caller_user_id,
            workspace_id=workspace_id,
            scope=scope,
            limit=limit,
            after=after,
        )

    async def get_document(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission = SharePermission.VIEW,
    ) -> EditorDocument:
        """One document (with body) the caller may see, or not-found."""
        document = await self._store.get_document(document_id)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=minimum,
        )
        return document

    async def get_document_facts(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission = SharePermission.VIEW,
    ) -> EditorDocumentFacts:
        """Identity, ownership and mode of one document the caller may see,
        without its body, or not-found; the access rule is ``get_document``'s."""
        facts = (await self._store.get_document_facts((document_id,))).get(
            document_id
        )
        if facts is None:
            raise DocumentNotFound(document_id)
        await self._resolve_document_access(
            facts,
            visible_to=visible_to,
            minimum=minimum,
        )
        return facts

    async def get_document_with_access(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission = SharePermission.VIEW,
    ) -> tuple[EditorDocument, ResourceAccess]:
        """Return one document together with its current owner/share access."""
        document = await self._store.get_document(document_id)
        access = await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=minimum,
        )
        return document, access

    async def get_document_for_ai(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission = SharePermission.VIEW,
    ) -> EditorDocument:
        """Return a projection-current document or fail visibly for AI use."""
        document = await self.get_document(
            document_id,
            visible_to=visible_to,
            minimum=minimum,
        )
        if document.content_mode != "collaboration":
            return document
        if self._collaboration_projector is None or visible_to is None:
            log.warning(
                "Collaboration projection barrier is unavailable for an AI read."
            )
            raise CollaborationProjectionUnavailable(document_id)
        try:
            projection = await self._collaboration_projector(
                document_id=document_id,
                principal=visible_to.principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            log.warning(
                "Collaboration projection barrier failed for an AI read."
            )
            raise CollaborationProjectionUnavailable(document_id) from exc
        if (
            projection.authoritative_sequence != projection.sequence
            or projection.generation != document.collaboration_generation
            or projection.schema_hash != document.collaboration_schema_hash
        ):
            log.warning(
                "Collaboration projection barrier returned a non-current result "
                "for an AI read."
            )
            raise CollaborationProjectionUnavailable(document_id)
        return replace(
            document,
            content_markdown=projection.markdown,
            persisted_sequence=projection.sequence,
            projection_sequence=projection.sequence,
            projection_updated_at=time.time(),
        )

    async def share_owner_user_id(
        self, tenant_id: str, document_id: str
    ) -> uuid.UUID | None:
        """Return the owner only when the document is collaboration-shareable."""
        try:
            document = await self._store.get_document(document_id)
        except DocumentNotFound:
            return None
        if not _collaboration_shareable(document, tenant_id):
            return None
        return document.created_by_user_id

    async def share_title(self, tenant_id: str, document_id: str) -> str | None:
        """Return the title only when the document is collaboration-shareable."""
        try:
            document = await self._store.get_document(document_id)
        except DocumentNotFound:
            return None
        if not _collaboration_shareable(document, tenant_id):
            return None
        return document.title

    async def share_facts(
        self, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        """Owner and title of each listed collaboration-shareable document,
        in one body-free read; other ids are absent."""
        facts = await self._store.get_document_facts(document_ids)
        return {
            document_id: ShareFacts(
                owner_user_id=document.created_by_user_id, title=document.title
            )
            for document_id, document in facts.items()
            if _collaboration_shareable(document, tenant_id)
        }

    async def delete_document(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None = None,
    ) -> None:
        """Delete a document (owner-only) with its comments (cascade)."""
        document = await self._store.get_document(document_id)
        require_owned_access(
            owner_user_id=document.created_by_user_id,
            resource_tenant_id=document.tenant_id,
            resource_id=document.id,
            visible_to=visible_to,
            not_found=DocumentNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=document.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: DocumentNotFound(document_id),
        )
        if document.content_mode == "collaboration":
            if (
                self._collaboration_store is None
                or document.created_by_user_id is None
            ):
                raise RuntimeError(
                    "Collaboration document deletion requires the durable "
                    "collaboration store and a canonical owner."
                )
            await self._collaboration_store.tombstone_document(
                tenant_id=document.tenant_id,
                document_id=document.id,
                owner_user_id=document.created_by_user_id,
                now=time.time(),
            )
            return
        await self._store.delete_document(
            document_id, scope=ResourceScope.from_record(document)
        )
        if self._invalidator is not None and not getattr(
            self._store, "atomic_delete_resource_effects", False
        ):
            await self._invalidator.revoke_deleted(
                tenant_id=document.tenant_id,
                owner_user_id=document.created_by_user_id,
                resource_type="editor_document",
                resource_id=document.id,
                scope="editor_documents",
                actor_user_id=(
                    visible_to.principal.user_id
                    if visible_to is not None
                    else None
                ),
            )

    async def patch_document_metadata(
        self,
        document_id: str,
        *,
        expected_metadata_revision: int,
        title: str | None,
        folder_id: str | None,
        set_folder_id: bool,
        diff_anchor_markdown: str | None,
        set_diff_anchor_markdown: bool,
        diff_anchor_updated_at: float | None,
        set_diff_anchor_updated_at: bool,
        visible_to: "UserContext | None",
    ) -> EditorDocument:
        """Owner-only metadata CAS that never writes the document body."""
        if expected_metadata_revision < 1:
            raise EditorValidationError(
                "expected_metadata_revision must be positive"
            )
        if diff_anchor_updated_at is not None and diff_anchor_updated_at < 0:
            raise EditorValidationError(
                "diff_anchor_updated_at must be non-negative"
            )
        document = await self._store.get_document(document_id)
        require_owned_access(
            owner_user_id=document.created_by_user_id,
            resource_tenant_id=document.tenant_id,
            resource_id=document.id,
            visible_to=visible_to,
            not_found=DocumentNotFound,
        )
        if set_folder_id and folder_id is not None:
            folders = await self._store.list_folders(
                created_by_user_id=document.created_by_user_id,
                workspace_id=document.workspace_id,
            )
            if not any(folder.id == folder_id for folder in folders):
                raise FolderNotFound(folder_id)
        return await self._store.patch_document_metadata(
            document_id=document_id,
            expected_metadata_revision=expected_metadata_revision,
            title=title,
            folder_id=folder_id,
            set_folder_id=set_folder_id,
            diff_anchor_markdown=diff_anchor_markdown,
            set_diff_anchor_markdown=set_diff_anchor_markdown,
            diff_anchor_updated_at=diff_anchor_updated_at,
            set_diff_anchor_updated_at=set_diff_anchor_updated_at,
            updated_at=time.time(),
            scope=ResourceScope.from_record(document),
        )

    # -- folders ---------------------------------------------------------- #

    async def save_folder(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> EditorFolder:
        """Create or update a folder (idempotent)."""
        existing = None
        for folder in await self._store.list_folders(
            created_by_user_id=None, workspace_id=None
        ):
            if folder.id == id:
                existing = folder
                break
        if existing is not None:
            require_owned_access(
                owner_user_id=existing.created_by_user_id,
                resource_tenant_id=existing.tenant_id,
                resource_id=existing.id,
                visible_to=visible_to,
                not_found=FolderNotFound,
            )
            owner_user_id = existing.created_by_user_id
            owner_workspace = existing.workspace_id
        else:
            owner_user_id = caller_user_id
            owner_workspace = workspace_id
        return await self._store.upsert_folder(
            id=id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            created_by_user_id=owner_user_id,
            workspace_id=owner_workspace,
        )

    async def list_folders(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[EditorFolder]:
        """All of the caller's folders (newest first)."""
        return await self._store.list_folders(
            created_by_user_id=caller_user_id, workspace_id=workspace_id
        )

    async def delete_folder(
        self,
        folder_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None = None,
    ) -> None:
        """Delete a folder (its documents orphan to ungrouped)."""
        existing = None
        for folder in await self._store.list_folders(
            created_by_user_id=None, workspace_id=None
        ):
            if folder.id == folder_id:
                existing = folder
                break
        if existing is None:
            raise FolderNotFound(folder_id)
        require_owned_access(
            owner_user_id=existing.created_by_user_id,
            resource_tenant_id=existing.tenant_id,
            resource_id=existing.id,
            visible_to=visible_to,
            not_found=FolderNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: FolderNotFound(folder_id),
        )
        await self._store.delete_folder(
            folder_id, scope=ResourceScope.from_record(existing)
        )

    # -- comments --------------------------------------------------------- #

    async def save_comments(
        self,
        document_id: str,
        *,
        comments: list[dict[str, Any]],
        visible_to: "UserContext | None",
        caller_user_id: uuid.UUID | None = None,
    ) -> list[EditorComment]:
        """Upsert comments into a document the caller may edit."""
        document = await self._store.get_document(document_id)
        minimum = comment_write_permission(document.content_mode)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=minimum,
        )
        actor_user_id = (
            visible_to.principal.user_id if visible_to is not None else None
        )
        if caller_user_id not in (None, actor_user_id):
            raise DocumentNotFound(document_id)
        parsed = [
            self._parse_comment(
                document_id,
                raw,
                created_by_user_id=actor_user_id,
            )
            for raw in comments
        ]
        return await self._store.upsert_comments(
            parsed,
            expected_document_id=document.id,
            expected_document_owner_user_id=document.created_by_user_id,
            expected_document_workspace_id=document.workspace_id,
            expected_document_content_mode=document.content_mode,
            actor_user_id=actor_user_id,
        )

    async def list_comments(
        self,
        document_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
        visible_to: "UserContext | None",
    ) -> tuple[list[EditorComment], str | None]:
        """One keyset page of a readable document's comments."""
        document = await self.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        comment_owner = (
            visible_to.principal.user_id
            if document.content_mode == "collaboration" and visible_to is not None
            else None
        )
        return await self._store.list_comments_page(
            document_id,
            created_by_user_id=comment_owner,
            limit=limit,
            after=after,
        )

    async def assert_comments_visible(
        self,
        document_id: str,
        comment_ids: list[str],
        *,
        visible_to: "UserContext | None",
    ) -> EditorDocument:
        """The document is readable and every named comment still exists in the caller's scope.

        The material re-check of an archived transcript: a deleted
        document, a revoked share or a deleted transferred comment must not
        stay readable through the old raw text. Comment scope mirrors
        :meth:`list_comments` (collaboration documents: own comments only).
        """
        document = await self.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        comment_owner = (
            visible_to.principal.user_id
            if document.content_mode == "collaboration" and visible_to is not None
            else None
        )
        for comment_id in comment_ids:
            await self._store.get_comment(
                document_id,
                comment_id,
                created_by_user_id=comment_owner,
            )
        return document

    async def get_document_context(
        self,
        document_id: str,
        *,
        comment_limit: int = EDITOR_CONTEXT_COMMENT_PAGE_SIZE,
        comment_after: tuple[float, str] | None = None,
        include_comments: bool = True,
        visible_to: "UserContext | None",
    ) -> tuple[EditorDocument, list[EditorComment], str | None]:
        """One document plus its comments in a single access-checked call.

        The read bundle an agent needs to understand a document before
        proposing changes: it saves the two-round-trip dance of
        ``get_document_for_ai`` + ``list_comments`` and applies the same
        access check once. Collaboration documents cross the exact Node-backed
        projection barrier before any body or private comment context is
        returned; a stale projection is never substituted. Comments retain
        the existing keyset-page contract and its explicit continuation;
        a page is not an immutable snapshot of the entire comment collection.
        """
        document = await self.get_document_for_ai(
            document_id,
            visible_to=visible_to,
        )
        if not include_comments:
            return document, [], None
        comment_owner = (
            visible_to.principal.user_id
            if document.content_mode == "collaboration" and visible_to is not None
            else None
        )
        comments, cursor = await self._store.list_comments_page(
            document_id,
            created_by_user_id=comment_owner,
            limit=comment_limit,
            after=comment_after,
        )
        return document, comments, cursor

    async def delete_comment(
        self,
        document_id: str,
        comment_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> None:
        """Delete one comment from a document the caller may edit."""
        document = await self._store.get_document(document_id)
        minimum = comment_write_permission(document.content_mode)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=minimum,
        )
        await self._store.delete_comment(
            document_id=document_id,
            comment_id=comment_id,
            created_by_user_id=(
                visible_to.principal.user_id
                if document.content_mode == "collaboration"
                and visible_to is not None
                else None
            ),
            expected_document_owner_user_id=document.created_by_user_id,
            expected_document_workspace_id=document.workspace_id,
            expected_document_content_mode=document.content_mode,
            actor_user_id=(
                visible_to.principal.user_id if visible_to is not None else None
            ),
        )

    async def save_comment_suggestion_draft(
        self,
        document_id: str,
        comment_id: str,
        *,
        expected_revision: int,
        payload: dict[str, Any],
        visible_to: "UserContext | None",
    ) -> EditorSuggestionDraft:
        """Create or revise one creator-private collaboration AI draft."""
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise EditorValidationError(
                "expected_revision must be a non-negative integer"
            )
        document = await self._store.get_document(document_id)
        if document.content_mode != "collaboration":
            raise DocumentNotFound(document_id)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=SharePermission.SUGGEST,
        )
        actor_user_id = (
            visible_to.principal.user_id if visible_to is not None else None
        )
        if actor_user_id is None:
            raise DocumentNotFound(document_id)
        try:
            comment = await self._store.get_comment(
                document_id,
                comment_id,
                created_by_user_id=actor_user_id,
            )
        except SuggestionDraftNotFound:
            raise SuggestionDraftNotFound(comment_id) from None
        current = comment.suggestion_draft
        current_revision = current.revision if current is not None else 0
        if current_revision != expected_revision:
            raise SuggestionDraftRevisionConflict(
                current_revision=current_revision
            )
        now = time.time()
        draft = (
            self._parse_new_suggestion_draft(payload, now=now)
            if current is None
            else self._parse_revised_suggestion_draft(
                current,
                payload,
                now=now,
            )
        )
        self._validate_suggestion_draft_size(draft)
        return await self._store.save_comment_suggestion_draft(
            document_id=document_id,
            comment_id=comment_id,
            draft=draft,
            expected_revision=expected_revision,
            expected_document_owner_user_id=document.created_by_user_id,
            expected_document_workspace_id=document.workspace_id,
            expected_document_content_mode=document.content_mode,
            actor_user_id=actor_user_id,
        )

    async def delete_comment_suggestion_draft(
        self,
        document_id: str,
        comment_id: str,
        *,
        expected_revision: int,
        patch_id: str,
        visible_to: "UserContext | None",
    ) -> None:
        """Discard one private draft without retaining provider content."""
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise EditorValidationError(
                "expected_revision must be a positive integer"
            )
        self._uuid_string(patch_id, field="patch_id")
        document = await self._store.get_document(document_id)
        if document.content_mode != "collaboration":
            raise DocumentNotFound(document_id)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=SharePermission.SUGGEST,
        )
        actor_user_id = (
            visible_to.principal.user_id if visible_to is not None else None
        )
        if actor_user_id is None:
            raise DocumentNotFound(document_id)
        await self._store.delete_comment_suggestion_draft(
            document_id=document_id,
            comment_id=comment_id,
            expected_revision=expected_revision,
            patch_id=patch_id,
            expected_document_owner_user_id=document.created_by_user_id,
            expected_document_workspace_id=document.workspace_id,
            expected_document_content_mode=document.content_mode,
            actor_user_id=actor_user_id,
        )

    # -- helpers ---------------------------------------------------------- #

    async def _require_document_edit(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> None:
        document = await self._store.get_document(document_id)
        await self._resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=SharePermission.EDIT,
        )

    async def _resolve_document_access(
        self,
        document: EditorDocument | EditorDocumentFacts,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission,
    ) -> ResourceAccess:
        if self._authorization is None or visible_to is None:
            return require_owned_access(
                owner_user_id=document.created_by_user_id,
                resource_tenant_id=document.tenant_id,
                resource_id=document.id,
                visible_to=visible_to,
                not_found=DocumentNotFound,
            )
        try:
            return await self._authorization.require(
                visible_to.principal,
                minimum,
                owner_user_id=document.created_by_user_id,
                resource_tenant_id=document.tenant_id,
                resource_type="editor_document",
                resource_id=document.id,
            )
        except ResourceNotFound:
            raise DocumentNotFound(document.id) from None

    @staticmethod
    def _parse_comment(
        document_id: str,
        raw: dict[str, Any],
        *,
        created_by_user_id: uuid.UUID | None,
    ) -> EditorComment:
        comment_id = raw.get("id")
        if not isinstance(comment_id, str) or not comment_id:
            raise EditorValidationError("comment id is required")
        kind = raw.get("kind")
        if kind not in _VALID_COMMENT_KINDS:
            raise EditorValidationError(f"unknown comment kind: {kind!r}")
        status = raw.get("status")
        if status not in _VALID_COMMENT_STATUSES:
            raise EditorValidationError(f"unknown comment status: {status!r}")
        evidence_preset = raw.get("evidence_preset")
        if evidence_preset is not None and evidence_preset not in _VALID_EVIDENCE_PRESETS:
            raise EditorValidationError(
                f"unknown evidence preset: {evidence_preset!r}"
            )
        anchor = raw.get("anchor", {})
        if not isinstance(anchor, dict):
            raise EditorValidationError("comment anchor must be an object")
        content = raw.get("comment_markdown", "")
        if not isinstance(content, str):
            raise EditorValidationError("comment_markdown must be a string")
        created_at = raw.get("created_at")
        updated_at = raw.get("updated_at")
        if not isinstance(created_at, (int, float)) or not isinstance(
            updated_at, (int, float)
        ):
            raise EditorValidationError(
                "comment created_at and updated_at must be numbers"
            )
        return EditorComment(
            id=comment_id,
            document_id=document_id,
            comment_markdown=content,
            anchor=dict(anchor),
            kind=kind,
            status=status,
            evidence_preset=evidence_preset,
            created_at=float(created_at),
            updated_at=float(updated_at),
            created_by_user_id=created_by_user_id,
        )

    @classmethod
    def _parse_new_suggestion_draft(
        cls,
        payload: dict[str, Any],
        *,
        now: float,
    ) -> EditorSuggestionDraft:
        allowed = {
            "anchor_text",
            "anchor_version",
            "change_summary",
            "edit_position",
            "evidence",
            "group_id",
            "patch_id",
            "proposed_text",
            "publication_command_id",
            "suggestion_id",
            "warnings",
        }
        cls._reject_unknown_fields(payload, allowed)
        anchor_version = payload.get("anchor_version")
        if anchor_version != _PRIVATE_DRAFT_ANCHOR_VERSION:
            raise EditorValidationError("unsupported suggestion anchor_version")
        # Nur bei der Erstanlage: Ankerstelle und Einfuegeart gehoeren zur
        # Identitaet des Vorschlags. Eine Revision darf den Vorschlag
        # umformulieren, aber nicht anderswohin wandern lassen -- deshalb
        # nimmt _parse_revised_suggestion_draft die Felder nicht entgegen.
        edit_position = payload.get("edit_position")
        if edit_position is not None and edit_position not in _VALID_EDIT_POSITIONS:
            raise EditorValidationError(
                f"unknown suggestion edit_position: {edit_position!r}"
            )
        anchor_text = payload.get("anchor_text")
        if anchor_text is not None:
            anchor_text = cls._bounded_string(
                anchor_text,
                field="anchor_text",
                maximum=_MAX_PRIVATE_DRAFT_ANCHOR_TEXT,
            )
        return EditorSuggestionDraft(
            suggestion_id=cls._bounded_string(
                payload.get("suggestion_id"),
                field="suggestion_id",
                maximum=_MAX_PRIVATE_DRAFT_ID,
            ),
            group_id=cls._bounded_string(
                payload.get("group_id"),
                field="group_id",
                maximum=_MAX_PRIVATE_DRAFT_ID,
            ),
            patch_id=cls._uuid_string(
                payload.get("patch_id"), field="patch_id"
            ),
            publication_command_id=cls._uuid_string(
                payload.get("publication_command_id"),
                field="publication_command_id",
            ),
            proposed_text=cls._draft_proposed_text(payload.get("proposed_text")),
            anchor_version=_PRIVATE_DRAFT_ANCHOR_VERSION,
            edit_position=(
                str(edit_position) if edit_position is not None else None
            ),
            anchor_text=anchor_text,
            revision=1,
            change_summary=cls._metadata_strings(
                payload.get("change_summary", []), field="change_summary"
            ),
            warnings=cls._metadata_strings(
                payload.get("warnings", []), field="warnings"
            ),
            evidence=cls._evidence(payload.get("evidence")),
            revision_history=(),
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def _parse_revised_suggestion_draft(
        cls,
        current: EditorSuggestionDraft,
        payload: dict[str, Any],
        *,
        now: float,
    ) -> EditorSuggestionDraft:
        allowed = {
            "change_summary",
            "evidence",
            "instruction",
            "proposed_text",
            "revision_source",
            "warnings",
        }
        cls._reject_unknown_fields(payload, allowed)
        source = payload.get("revision_source")
        if source not in _VALID_DRAFT_REVISION_SOURCES:
            raise EditorValidationError("unknown suggestion revision_source")
        if len(current.revision_history) >= _MAX_PRIVATE_DRAFT_HISTORY:
            raise EditorValidationError("suggestion revision history is full")
        instruction_value = payload.get("instruction")
        instruction = (
            cls._bounded_string(
                instruction_value,
                field="instruction",
                maximum=_MAX_PRIVATE_DRAFT_INSTRUCTION,
            )
            if instruction_value is not None
            else None
        )
        history_entry = EditorSuggestionDraftRevision(
            proposed_text=current.proposed_text,
            source=source,
            created_at=now,
            instruction=instruction,
            change_summary=current.change_summary,
            warnings=current.warnings,
        )
        return replace(
            current,
            proposed_text=cls._draft_proposed_text(payload.get("proposed_text")),
            revision=current.revision + 1,
            change_summary=cls._metadata_strings(
                payload.get("change_summary", []), field="change_summary"
            ),
            warnings=cls._metadata_strings(
                payload.get("warnings", []), field="warnings"
            ),
            evidence=(
                cls._evidence(payload.get("evidence"))
                if "evidence" in payload
                else current.evidence
            ),
            revision_history=(*current.revision_history, history_entry),
            updated_at=now,
        )

    @staticmethod
    def _reject_unknown_fields(
        payload: dict[str, Any], allowed: set[str]
    ) -> None:
        if not isinstance(payload, dict):
            raise EditorValidationError("suggestion draft must be an object")
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise EditorValidationError(
                f"unknown suggestion draft field: {unknown[0]}"
            )

    @staticmethod
    def _bounded_string(value: Any, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
            raise EditorValidationError(f"invalid suggestion draft {field}")
        return value

    @staticmethod
    def _draft_proposed_text(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise EditorValidationError("invalid suggestion draft proposed_text")
        if len(value.encode("utf-8")) > _MAX_PRIVATE_DRAFT_BYTES:
            raise SuggestionDraftTooLarge(
                "suggestion draft exceeds the size limit"
            )
        return value

    @classmethod
    def _uuid_string(cls, value: Any, *, field: str) -> str:
        text = cls._bounded_string(
            value,
            field=field,
            maximum=36,
        )
        try:
            parsed = uuid.UUID(text)
        except ValueError as exc:
            raise EditorValidationError(
                f"invalid suggestion draft {field}"
            ) from exc
        if str(parsed) != text:
            raise EditorValidationError(
                f"invalid suggestion draft {field}"
            )
        return text

    @classmethod
    def _metadata_strings(cls, value: Any, *, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) > _MAX_PRIVATE_DRAFT_LIST_ITEMS:
            raise EditorValidationError(f"invalid suggestion draft {field}")
        return tuple(
            cls._bounded_string(
                item,
                field=field,
                maximum=_MAX_PRIVATE_DRAFT_METADATA_TEXT,
            )
            for item in value
        )

    @classmethod
    def _evidence(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"mode", "sources"}:
            raise EditorValidationError("invalid suggestion draft evidence")
        mode = value.get("mode")
        if mode not in _VALID_EVIDENCE_PRESETS:
            raise EditorValidationError("invalid suggestion draft evidence mode")
        sources = value.get("sources")
        if not isinstance(sources, list) or len(sources) > _MAX_PRIVATE_DRAFT_LIST_ITEMS:
            raise EditorValidationError("invalid suggestion draft evidence sources")
        normalized: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"title", "url"}:
                raise EditorValidationError("invalid suggestion draft evidence source")
            title = cls._bounded_string(
                source.get("title"),
                field="evidence title",
                maximum=_MAX_PRIVATE_DRAFT_METADATA_TEXT,
            )
            url = cls._bounded_string(
                source.get("url"),
                field="evidence url",
                maximum=_MAX_PRIVATE_DRAFT_METADATA_TEXT,
            )
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise EditorValidationError("invalid suggestion draft evidence url")
            normalized.append({"title": title, "url": url})
        return {"mode": mode, "sources": normalized}

    @staticmethod
    def _validate_suggestion_draft_size(draft: EditorSuggestionDraft) -> None:
        encoded = json.dumps(
            suggestion_draft_payload(draft),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_PRIVATE_DRAFT_BYTES:
            raise SuggestionDraftTooLarge(
                "suggestion draft exceeds the size limit"
            )
