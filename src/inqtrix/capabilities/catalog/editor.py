"""Editor capabilities: document context (read) and patch lifecycle (write).

The read capability bundles a document with its comments; the
write pair (``editor.patch.propose`` / ``editor.patch.apply``) are THIN
wrappers over
:class:`~inqtrix.services.editor_patch_service.EditorPatchService` —
proposals created through the capability always carry ``source='agent'``
and the run attribution from the injected context.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from inqtrix.agents.context_sources import MaterialReadInput, MaterialReadOutput
from inqtrix.capabilities.catalog.context import read_material_window
from inqtrix.pagination import decode_cursor

from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    CapabilityError,
    Effect,
)
from inqtrix.project.editor_patch_ports import (
    PatchAlreadyDecided,
    PatchNotFound,
    PatchRevisionConflict,
)
from inqtrix.project.editor_ports import EDITOR_CONTEXT_COMMENT_PAGE_SIZE, DocumentNotFound
from inqtrix.project.editor_search import EDITOR_SEARCH_MAX_MATCHES, EDITOR_SEARCH_QUERY_MAX, find_editor_anchors
from inqtrix.services.editor_patch_service import EditorPatchValidationError
from inqtrix.services.editor_persistence_service import (
    CollaborationProjectionUnavailable,
)

if TYPE_CHECKING:
    from inqtrix.services.editor_patch_service import EditorPatchService
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )

class DocumentContextInput(MaterialReadInput):
    document_id: str = Field(..., min_length=1)
    include_comments: bool = True
    comments_cursor: str = Field(default="", max_length=4096)
    comment_limit: StrictInt = Field(default=EDITOR_CONTEXT_COMMENT_PAGE_SIZE, ge=1, le=EDITOR_CONTEXT_COMMENT_PAGE_SIZE)


class EditorCommentView(BaseModel):
    id: str
    comment_markdown: str
    kind: str
    status: str


class DocumentContextOutput(MaterialReadOutput):
    id: str
    title: str
    content_markdown: str
    revision: int
    comments: list[EditorCommentView]
    comments_included: bool
    next_comments: dict | None


class EditorDocumentSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1)
    query: str = Field(min_length=2, max_length=EDITOR_SEARCH_QUERY_MAX)
    offset: StrictInt = Field(default=0, ge=0)
    version: str = ""
    content_hash: str = ""
    max_matches: StrictInt = Field(default=EDITOR_SEARCH_MAX_MATCHES, ge=1, le=EDITOR_SEARCH_MAX_MATCHES)


class EditorDocumentSearchOutput(BaseModel):
    representation: str = "editor_anchor_matches"
    id: str
    title: str
    revision: int
    version: str
    content_hash: str
    matches: list[dict]
    total: int
    offset: int
    end: int
    total_chars: int
    next_search: dict | None


def _editor_version_prefix(document) -> str:
    return f"{document.revision}:{document.metadata_revision}:{document.collaboration_generation}:{document.projection_sequence}"


def build_editor_capabilities(
    service: "EditorPersistenceService",
) -> list[CapabilityDefinition]:
    """Build the editor capabilities bound to *service*."""

    async def _read_context(
        payload: DocumentContextInput, context: CapabilityContext
    ) -> DocumentContextOutput:
        if context.target_document_id and context.target_document_id != payload.document_id:
            raise CapabilityError("editor.wrong_target", "The assignment targets a different editor document.")
        if payload.comments_cursor and (not payload.version or not payload.content_hash):
            raise CapabilityError("context.invalid_read", "A comment continuation requires the document version and content fingerprint.")
        try:
            after = decode_cursor(payload.comments_cursor) if payload.comments_cursor else None
        except ValueError as exc:
            raise CapabilityError("context.invalid_read", "Invalid comment cursor.") from exc
        try:
            document, comments, cursor = await service.get_document_context(
                payload.document_id,
                visible_to=context.visible_to,
                comment_limit=payload.comment_limit,
                comment_after=after,
                include_comments=payload.include_comments,
            )
        except DocumentNotFound as exc:
            raise CapabilityError(
                "editor.document_not_found",
                "Document not found.",
                http_status=404,
            ) from exc
        except CollaborationProjectionUnavailable as exc:
            raise CapabilityError(
                "editor.collaboration_projection_unavailable",
                "The current collaboration state could not be persisted.",
                http_status=503,
            ) from exc
        text, window = read_material_window(
            document.content_markdown,
            _editor_version_prefix(document),
            payload,
        )
        if window.next_read is not None:
            window.next_read["include_comments"] = False
            window.next_read["comments_cursor"] = ""
        result = DocumentContextOutput(
            id=document.id,
            title=document.title,
            content_markdown=text,
            revision=document.revision,
            comments=[
                EditorCommentView(
                    id=comment.id,
                    comment_markdown=comment.comment_markdown,
                    kind=comment.kind,
                    status=comment.status,
                )
                for comment in comments
            ],
            window=window,
            representation="editor_markdown",
            comments_included=payload.include_comments,
            next_comments={**payload.model_dump(), "version": window.version,
                           "content_hash": window.content_hash, "comments_cursor": cursor}
            if cursor else None,
        )
        return result

    async def _search(payload: EditorDocumentSearchInput, context: CapabilityContext) -> EditorDocumentSearchOutput:
        if context.target_document_id and context.target_document_id != payload.document_id:
            raise CapabilityError("editor.wrong_target", "The assignment targets a different editor document.")
        if len(payload.query.strip()) < 2:
            raise CapabilityError("editor.search_query_invalid", "The search text needs at least two characters.")
        try:
            document = await service.get_document_for_ai(payload.document_id, visible_to=context.visible_to)
        except DocumentNotFound as exc:
            raise CapabilityError("editor.document_not_found", "Document not found.", http_status=404) from exc
        except CollaborationProjectionUnavailable as exc:
            raise CapabilityError("editor.collaboration_projection_unavailable", "The current collaboration state could not be persisted.", http_status=503) from exc
        _, window = read_material_window(document.content_markdown, _editor_version_prefix(document), MaterialReadInput(
            offset=payload.offset, version=payload.version, content_hash=payload.content_hash, max_chars=1,
        ))
        matches, total = find_editor_anchors(document.content_markdown, payload.query, offset=payload.offset, limit=payload.max_matches)
        result = EditorDocumentSearchOutput(
            id=document.id, title=document.title, revision=document.revision,
            version=window.version, content_hash=window.content_hash,
            matches=matches, total=total, offset=payload.offset,
            end=len(document.content_markdown), total_chars=len(document.content_markdown),
            next_search={**payload.model_dump(), "offset": matches[-1]["offset"] + 1,
                         "version": window.version, "content_hash": window.content_hash}
            if total > len(matches) else None,
            representation="editor_anchor_matches",
        )
        return result

    return [
        CapabilityDefinition(
            id="editor.document.read",
            summary="Read one editor document with its comments as context.",
            input_model=DocumentContextInput,
            output_model=DocumentContextOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_read_context,
        ),
        CapabilityDefinition(
            id="editor.document.search", summary="Find exact, versioned anchors across an editor document.",
            input_model=EditorDocumentSearchInput, output_model=EditorDocumentSearchOutput,
            effect=Effect.READ, idempotent=True, handler=_search,
        ),
    ]


# -- patch lifecycle (write pair) ------------------------------------------- #


class PatchEditInput(BaseModel):
    """One anchored edit in the ``editor_instructions`` shape."""

    model_config = ConfigDict(extra="forbid")

    find: str = ""
    quote_before: str = ""
    quote_after: str = ""
    position: Literal["replace", "before", "after", "append"]
    text: str = ""
    note: str = ""


class PatchProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1)
    edits: list[PatchEditInput] = Field(..., min_length=1)
    summary: str = ""
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Document revision the caller read before proposing; "
            "a mismatch refuses the proposal with "
            "editor.patch_revision_conflict. None skips the pin."
        ),
    )


class PatchProposeOutput(BaseModel):
    patch_id: str
    document_id: str
    status: str
    edit_count: int
    revision_before: int


class PatchApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch_id: str = Field(..., min_length=1)
    expected_revision: int


class PatchApplyOutput(BaseModel):
    document_id: str
    revision: int
    applied_edit_ids: list[str]


def _capability_user_id(context: CapabilityContext) -> uuid.UUID | None:
    """Return the canonical caller ID used by scoped persistence surfaces."""
    principal = context.principal
    if principal is not None and principal.kind in ("oidc_session", "pat"):
        return principal.user_id
    return None


def build_editor_patch_capabilities(
    service: "EditorPatchService",
) -> list[CapabilityDefinition]:
    """Build the patch write capabilities bound to *service*.

    Thin wrappers only: the visibility rule, the revision snapshot, and
    the CAS apply live in the service. Registered exclusively when the
    patch service is wired (conditional-registration pattern).
    """

    async def _propose(
        payload: PatchProposeInput, context: CapabilityContext
    ) -> PatchProposeOutput:
        try:
            patch = await service.propose(
                document_id=payload.document_id,
                run_id=context.run_id,
                source="agent",
                edits=[edit.model_dump() for edit in payload.edits],
                summary=payload.summary,
                warnings=[],
                created_by_user_id=_capability_user_id(context),
                visible_to=context.visible_to,
                principal=context.principal,
                expected_revision=payload.expected_revision,
            )
        except DocumentNotFound as exc:
            raise CapabilityError(
                "editor.document_not_found",
                "Document not found.",
                http_status=404,
            ) from exc
        except EditorPatchValidationError as exc:
            raise CapabilityError(
                "editor.patch_invalid",
                str(exc),
                http_status=400,
            ) from exc
        except PatchRevisionConflict as exc:
            # Same conflict vocabulary as apply: a proposal pinned to a
            # stale read must fail loudly, never anchor against text the
            # proposer has not seen.
            raise CapabilityError(
                "editor.patch_revision_conflict",
                f'The document changed in the meantime (current revision {exc.current_revision}). Read it again before proposing changes.',
                http_status=409,
            ) from exc
        return PatchProposeOutput(
            patch_id=patch.patch_id,
            document_id=patch.document_id,
            status=patch.status,
            edit_count=len(patch.edits),
            revision_before=patch.revision_before,
        )

    async def _apply(
        payload: PatchApplyInput, context: CapabilityContext
    ) -> PatchApplyOutput:
        try:
            patch = await service.apply(
                payload.patch_id,
                expected_revision=payload.expected_revision,
                visible_to=context.visible_to,
                principal=context.principal,
            )
        except PatchNotFound as exc:
            raise CapabilityError(
                "editor.patch_not_found",
                "Patch not found.",
                http_status=404,
            ) from exc
        except PatchRevisionConflict as exc:
            raise CapabilityError(
                "editor.patch_revision_conflict",
                f'The document changed in the meantime (current revision {exc.current_revision}).',
                http_status=409,
            ) from exc
        except PatchAlreadyDecided as exc:
            raise CapabilityError(
                "editor.patch_already_decided",
                "The patch already has a different decision.",
                http_status=409,
            ) from exc
        return PatchApplyOutput(
            document_id=patch.document_id,
            revision=patch.applied_revision or 0,
            applied_edit_ids=list(patch.applied_edit_ids or ()),
        )

    return [
        CapabilityDefinition(
            id="editor.patch.propose",
            summary="Propose anchored edits against one editor document.",
            input_model=PatchProposeInput,
            output_model=PatchProposeOutput,
            effect=Effect.WRITE,
            idempotent=False,
            handler=_propose,
        ),
        CapabilityDefinition(
            id="editor.patch.apply",
            summary="Apply a pending editor patch at an expected document revision.",
            input_model=PatchApplyInput,
            output_model=PatchApplyOutput,
            effect=Effect.WRITE,
            idempotent=True,
            handler=_apply,
        ),
    ]
