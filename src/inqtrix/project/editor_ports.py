"""Contracts of the editor-persistence store.

Mirrors ``chat_ports.py``: the service resolves owner/share visibility and the
store owns persistence. Child mutations additionally receive the resolved
parent identity so the durable store can lock the parent and revalidate live
authority in its transaction. The wire shape remains in the router. Two
implementations sit behind the same port:
:class:`~inqtrix.project.editor_memory.MemoryEditorStore` (the tier without
Postgres, also the offline test backend) and
:class:`~inqtrix.project.editor_postgres.PostgresEditorStore`.

Differences from the chat store:

* A document carries a heavy ``content_markdown`` body. ``list_documents_page``
  returns metadata WITHOUT the body (``content_markdown=""``); ``get_document``
  returns the full document with the body (the load-on-open path).
* Comments are independently mutated (resolve / edit / re-tag), so they are
  a diffed collection with delete propagation — ``upsert_comments`` +
  ``delete_comment`` + ``list_comments_page`` — not append-only like chat
  messages.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from inqtrix.project.scoped_upsert import ResourceScope

from inqtrix.auth.permissions import SharePermission

if TYPE_CHECKING:
    from inqtrix.auth.permissions import ResourceAccess

EDITOR_CONTEXT_COMMENT_PAGE_SIZE = 200
"""Maximum comment page delivered with a model's editor context."""


class DocumentNotFound(KeyError):
    """Raised when a document id is unknown to the store (maps to HTTP 404)."""


class DocumentRevisionConflict(RuntimeError):
    """Raised when a document save loses the revision guard (HTTP 409).

    The store accepts a save only when it carries a STRICTLY newer
    revision than the stored row (monotonic guard, not base+1-exact —
    debounced autosave legitimately jumps several revisions per flush).
    A concurrent writer — human autosave vs. agent patch apply — whose
    counter is stale-or-equal gets this instead of silently overwriting
    the newer content or moving the revision backwards. The carried
    ``current_revision`` lets the client refetch and rebase precisely.

    Attributes:
        current_revision: The revision actually stored right now.
        expected_revision: The base the losing writer assumed
            (its ``revision - 1``).
    """

    def __init__(self, *, current_revision: int, expected_revision: int) -> None:
        super().__init__(
            f"document revision moved to {current_revision}, "
            f"writer assumed base {expected_revision}"
        )
        self.current_revision = current_revision
        self.expected_revision = expected_revision


class DocumentContentModeConflict(RuntimeError):
    """Raised when a legacy body write targets a collaboration document."""

    def __init__(self, document_id: str) -> None:
        super().__init__(f"document {document_id} is no longer markdown-owned")
        self.document_id = document_id


class DocumentMetadataConflict(RuntimeError):
    """Raised when an owner metadata PATCH loses its revision guard."""

    def __init__(self, *, current_revision: int) -> None:
        super().__init__(f"document metadata moved to {current_revision}")
        self.current_revision = current_revision


class FolderNotFound(KeyError):
    """Raised when a folder id is unknown to the store (maps to HTTP 404)."""


class SuggestionDraftNotFound(KeyError):
    """Raised when a private suggestion draft is absent for the caller."""


class SuggestionDraftRevisionConflict(RuntimeError):
    """Raised when a private-draft write loses its revision guard."""

    def __init__(self, *, current_revision: int) -> None:
        super().__init__(f"suggestion draft moved to revision {current_revision}")
        self.current_revision = current_revision


@dataclass(frozen=True)
class EditorFolder:
    """One grouping of a user's editor documents (a tree folder).

    Attributes mirror :class:`~inqtrix.project.chat_ports.ChatThreadGroup`:
    a client-supplied prefixed id (``edf_``), unix-seconds timestamps, and
    the ``(tenant_id, created_by_user_id, workspace_id)`` scope.
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class EditorDocument:
    """One editor document. ``content_markdown`` is the heavy body — empty
    on records returned by ``list_documents_page`` (metadata only) and
    populated by ``get_document`` (load-on-open).

    Attributes:
        id: Client-supplied id (``ed_``), the primary key.
        title: Document title.
        content_markdown: The document body (heavy; lazy).
        folder_id: Owning folder id, or ``None`` when at the tree root.
        source: Origin (``blank``/``imported-research-report``/``pasted``).
        source_run_id: The research run a document was imported from.
        revision: Client mutation counter (round-tripped verbatim).
        diff_anchor_markdown: Snapshot body for the in-editor diff, or None.
        diff_anchor_updated_at: Unix timestamp of the diff snapshot, or None.
        created_at: Unix timestamp of creation (the stable list-sort key).
        updated_at: Unix timestamp of the last change (display + autosave key).
    """

    id: str
    title: str
    content_markdown: str = field(repr=False, default="")
    folder_id: str | None = None
    source: str = "blank"
    source_run_id: str | None = None
    revision: int = 1
    diff_anchor_markdown: str | None = None
    diff_anchor_updated_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None
    content_mode: str = "markdown"
    metadata_revision: int = 1
    collaboration_generation: int = 0
    collaboration_schema_version: int | None = None
    collaboration_schema_hash: str | None = None
    persisted_sequence: int = 0
    projection_sequence: int = 0
    projection_updated_at: float | None = None
    collaboration_comment_revision: int = 0
    deleted_at: float | None = None


@dataclass(frozen=True)
class EditorDocumentFacts:
    """Identity, ownership and mode of one editor document, without any
    Markdown body: enough to decide access and to address its collaboration
    room. Read for every activity poll and shared-resource listing, where
    loading the body would be pure transfer cost."""

    id: str
    tenant_id: str
    created_by_user_id: uuid.UUID | None
    title: str
    content_mode: str
    collaboration_generation: int
    deleted_at: float | None


@dataclass(frozen=True)
class EditorSuggestionDraftRevision:
    """One prior private-draft value retained for user-visible undo context."""

    proposed_text: str
    source: str
    created_at: float
    instruction: str | None = None
    change_summary: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditorSuggestionDraft:
    """Creator-private AI proposal attached to one private editor comment.

    The UUID patch and command identifiers are allocated once when the draft
    is created. They make publication replayable without creating a second
    shared Yjs patch after a lost acknowledgement. The draft itself remains
    private and is cleared in the same PostgreSQL transaction that persists
    the shared suggestion.
    """

    suggestion_id: str
    group_id: str
    patch_id: str
    publication_command_id: str
    proposed_text: str = field(repr=False)
    anchor_version: int = 1
    #: Wo die Aenderung greift. Der Kommentarweg ersetzt immer den markierten
    #: Bereich und brauchte die Angabe nie; ein Assistentenlauf fuegt in drei
    #: von vier Faellen ein. Fehlt sie, baut der Uebernahmepfad den Vorschlag
    #: als Ersetzung neu auf und der Nutzer verliert still Text.
    edit_position: str | None = None
    #: Der Suchtext der Ankerstelle. Beim Kommentarweg traegt ihn der
    #: Kommentar des Nutzers; ein Assistentenlauf hat keinen, also gehoert er
    #: in den Entwurf -- sonst ist die Stelle nach einem Neuladen verloren.
    anchor_text: str | None = None
    revision: int = 1
    change_summary: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None
    revision_history: tuple[EditorSuggestionDraftRevision, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0


def suggestion_draft_payload(draft: EditorSuggestionDraft) -> dict[str, Any]:
    """Return the canonical JSON-compatible private-draft representation."""
    return {
        "anchor_text": draft.anchor_text,
        "anchor_version": draft.anchor_version,
        "change_summary": list(draft.change_summary),
        "created_at": draft.created_at,
        "edit_position": draft.edit_position,
        "evidence": dict(draft.evidence) if draft.evidence is not None else None,
        "group_id": draft.group_id,
        "patch_id": draft.patch_id,
        "proposed_text": draft.proposed_text,
        "publication_command_id": draft.publication_command_id,
        "revision": draft.revision,
        "revision_history": [
            {
                "change_summary": list(item.change_summary),
                "created_at": item.created_at,
                "instruction": item.instruction,
                "proposed_text": item.proposed_text,
                "source": item.source,
                "warnings": list(item.warnings),
            }
            for item in draft.revision_history
        ],
        "suggestion_id": draft.suggestion_id,
        "updated_at": draft.updated_at,
        "warnings": list(draft.warnings),
    }


def suggestion_draft_from_payload(value: Any) -> EditorSuggestionDraft | None:
    """Decode a store-owned draft; ``None`` remains the absent state."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("stored suggestion draft must be an object")
    history = value.get("revision_history", [])
    if not isinstance(history, list):
        raise ValueError("stored suggestion draft history must be a list")
    return EditorSuggestionDraft(
        suggestion_id=str(value["suggestion_id"]),
        group_id=str(value["group_id"]),
        patch_id=str(value["patch_id"]),
        publication_command_id=str(value["publication_command_id"]),
        proposed_text=str(value["proposed_text"]),
        anchor_version=int(value["anchor_version"]),
        # Bestandsentwuerfe kennen die beiden Felder nicht. Sie fehlen dort
        # legitim und bedeuten "Ersetzung ohne eigenen Suchtext" -- genau das
        # Verhalten, das der Kommentarweg immer schon hatte.
        edit_position=(
            str(value["edit_position"])
            if value.get("edit_position") is not None
            else None
        ),
        anchor_text=(
            str(value["anchor_text"])
            if value.get("anchor_text") is not None
            else None
        ),
        revision=int(value["revision"]),
        change_summary=tuple(str(item) for item in value.get("change_summary", [])),
        warnings=tuple(str(item) for item in value.get("warnings", [])),
        evidence=(
            dict(value["evidence"])
            if isinstance(value.get("evidence"), dict)
            else None
        ),
        revision_history=tuple(
            EditorSuggestionDraftRevision(
                proposed_text=str(item["proposed_text"]),
                source=str(item["source"]),
                created_at=float(item["created_at"]),
                instruction=(
                    str(item["instruction"])
                    if item.get("instruction") is not None
                    else None
                ),
                change_summary=tuple(
                    str(entry) for entry in item.get("change_summary", [])
                ),
                warnings=tuple(str(entry) for entry in item.get("warnings", [])),
            )
            for item in history
            if isinstance(item, dict)
        ),
        created_at=float(value["created_at"]),
        updated_at=float(value["updated_at"]),
    )


@dataclass(frozen=True)
class EditorComment:
    """One anchored comment on a document.

    Attributes:
        id: Client-supplied id (``edc_``); PK together with ``document_id``.
        document_id: Owning document.
        comment_markdown: The comment body text.
        anchor: The verbatim positional anchor (block id, char range,
            surrounding quotes) — stored as-is, never reinterpreted.
        kind: ``collect`` for every note a user writes; ``assistant_edit``
            for the machine carrier of a private draft. ``inline_edit`` and
            ``evidence_review`` are still accepted and still stored, but the
            client stopped writing them — rows that carry them are read as
            ordinary notes.
        status: ``open``/``resolved``/``stale``.
        evidence_preset: Optional evidence preset, or ``None``.
        created_at: Unix timestamp (stable; the keyset key within a doc).
        updated_at: Unix timestamp of the last mutation (autosave diff key).
    """

    id: str
    document_id: str
    comment_markdown: str
    anchor: dict[str, Any] = field(default_factory=dict)
    kind: str = "collect"
    status: str = "open"
    evidence_preset: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    suggestion_draft: EditorSuggestionDraft | None = None


def comment_write_permission(content_mode: str) -> SharePermission:
    """Return the live permission required to mutate document comments.

    Collaboration comments are a suggestion surface; legacy Markdown
    comments remain an edit surface. Keeping this mapping in the persistence
    contract lets the service preflight and the transactional PostgreSQL
    authority check use one policy definition.
    """
    if content_mode == "collaboration":
        return SharePermission.SUGGEST
    if content_mode == "markdown":
        return SharePermission.EDIT
    raise ValueError(f"unsupported editor content mode: {content_mode!r}")


@runtime_checkable
class EditorStore(Protocol):
    """Persistence port for editor documents, folders, and comments.

    Scoping note (same as the chat store): ``list_documents_page`` /
    ``list_folders`` take the resolved ``created_by_user_id`` + ``workspace_id``
    and filter in the query so the DB ``LIMIT`` is never under-filled.
    ``get_document`` returns the row unscoped — the service applies the
    owner/share access check on top.
    """

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
        """Create or idempotently update a document by id (autosave upsert).

        An existing id keeps its ``created_at`` and ownership columns; only
        the mutable fields (title, body, folder, source metadata, revision,
        diff anchor, updated_at) are overwritten."""
        ...

    async def list_documents_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorDocument], str | None]:
        """One keyset page of the caller's documents (newest first),
        METADATA ONLY (``content_markdown=""`` — the body loads on open)."""
        ...

    async def list_visible_documents_page(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        workspace_id: str | None,
        scope: Literal["owned", "shared", "all"],
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[tuple[EditorDocument, "ResourceAccess"]], str | None]:
        """List owned and/or accepted-shared documents with live access."""
        ...

    async def get_document(self, document_id: str) -> EditorDocument:
        """One document WITH its body (load-on-open), or
        :class:`DocumentNotFound`."""
        ...

    async def get_document_facts(
        self, document_ids: Sequence[str]
    ) -> dict[str, EditorDocumentFacts]:
        """Body-free facts of each listed document ``get_document`` would
        return, keyed by id, in one read; other ids are absent. Performs no
        access check: only the service and share layers call it, and they
        authorize before anything leaves them."""
        ...

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
        """CAS-update owner-managed metadata without touching the body."""
        ...

    async def delete_document(
        self, document_id: str, *, scope: ResourceScope
    ) -> None:
        """Delete a document and its comments (cascade)."""
        ...

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
        """Create or idempotently update a folder by id."""
        ...

    async def list_folders(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[EditorFolder]:
        """All of the caller's folders, newest first (few — no keyset page)."""
        ...

    async def delete_folder(
        self, folder_id: str, *, scope: ResourceScope
    ) -> None:
        """Delete a folder; its documents orphan to ungrouped (SET NULL)."""
        ...

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
        """Idempotently upsert comments by (document_id, id). An existing
        comment overwrites its mutable fields (body/anchor/kind/status/
        preset/updated_at), never ``created_at``. The expected document
        identity and current actor authority are revalidated in the same
        transaction as the child write."""
        ...

    async def list_comments_page(
        self,
        document_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[EditorComment], str | None]:
        """One keyset page of a document's comments (newest first)."""
        ...

    async def get_comment(
        self,
        document_id: str,
        comment_id: str,
        *,
        created_by_user_id: uuid.UUID | None = None,
    ) -> EditorComment:
        """Return one comment in its creator scope or raise not-found."""
        ...

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
        """Delete one comment after transactional parent authorization."""
        ...

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
        """CAS-create or replace one creator-private suggestion draft."""
        ...

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
        """CAS-delete one creator-private suggestion draft."""
        ...

    async def aclose(self) -> None:
        """Release backing resources at application shutdown."""
        ...
