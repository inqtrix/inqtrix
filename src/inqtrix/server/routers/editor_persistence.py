"""Editor-persistence endpoints (project tier).

Thin per-surface router mirroring ``chat_history.py``: parse, delegate to
:class:`~inqtrix.services.editor_persistence_service.EditorPersistenceService`,
serialize. The list endpoint returns visible owned and accepted-shared document
METADATA only; the single-document GET returns the body (load-on-open).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.auth.permissions import AccessMode, ResourceAccess, SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    list_envelope,
)
from inqtrix.project.editor_ports import (
    DocumentContentModeConflict,
    DocumentMetadataConflict,
    DocumentNotFound,
    DocumentRevisionConflict,
    EditorComment,
    EditorDocument,
    EditorFolder,
    FolderNotFound,
    SuggestionDraftNotFound,
    SuggestionDraftRevisionConflict,
    suggestion_draft_payload,
)
from inqtrix.services.editor_persistence_service import (
    EditorValidationError,
    SuggestionDraftTooLarge,
)
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def _caller_user_id(principal: Principal) -> uuid.UUID | None:
    return principal.user_id if principal.kind in ("oidc_session", "pat") else None


def _access_payload(
    access: ResourceAccess | None,
    *,
    owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Public editor access contract with effective write permission."""
    if access is not None and access.mode is AccessMode.SHARED:
        payload: dict[str, Any] = {
            "mode": "shared",
            "permission": (access.permission or SharePermission.VIEW).value,
        }
        if owner is not None:
            payload["owner"] = owner
        return payload
    return {"mode": "owner", "permission": SharePermission.EDIT.value}


def _document_meta_payload(
    document: EditorDocument,
    access: ResourceAccess | None = None,
    *,
    owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Document WITHOUT the body — the list shape."""
    return {
        "id": document.id,
        "title": document.title,
        "folder_id": document.folder_id,
        "source": document.source,
        "source_run_id": document.source_run_id,
        "revision": document.revision,
        "diff_anchor_markdown": document.diff_anchor_markdown,
        "diff_anchor_updated_at": document.diff_anchor_updated_at,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "content_mode": document.content_mode,
        "metadata_revision": document.metadata_revision,
        "access": _access_payload(access, owner=owner),
        "collaboration": (
            {
                "generation": document.collaboration_generation,
                "schema_version": document.collaboration_schema_version,
                "persisted_sequence": document.persisted_sequence,
                "projection_sequence": document.projection_sequence,
                "projection_updated_at": document.projection_updated_at,
                "comment_revision": document.collaboration_comment_revision,
            }
            if document.content_mode == "collaboration"
            else None
        ),
    }


def _document_detail_payload(
    document: EditorDocument,
    access: ResourceAccess | None = None,
    *,
    owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Document WITH the body — the get-one / upsert-response shape."""
    return {
        **_document_meta_payload(document, access, owner=owner),
        "content_markdown": document.content_markdown,
    }


def _folder_payload(folder: EditorFolder) -> dict[str, Any]:
    return {
        "id": folder.id,
        "title": folder.title,
        "created_at": folder.created_at,
        "updated_at": folder.updated_at,
    }


def _comment_payload(comment: EditorComment) -> dict[str, Any]:
    return {
        "id": comment.id,
        "document_id": comment.document_id,
        "comment_markdown": comment.comment_markdown,
        "anchor": dict(comment.anchor),
        "kind": comment.kind,
        "status": comment.status,
        "evidence_preset": comment.evidence_preset,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
        "created_by_user_id": (
            str(comment.created_by_user_id)
            if comment.created_by_user_id is not None
            else None
        ),
        "suggestion_draft": (
            suggestion_draft_payload(comment.suggestion_draft)
            if comment.suggestion_draft is not None
            else None
        ),
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the editor-persistence routes against the container."""
    service = container.editor_persistence_service
    if service is None:
        raise RuntimeError(
            "build_router(editor_persistence) requires a wired editor service."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    users = getattr(container.auth_provider, "users", None)

    async def _owner_profiles(
        documents: list[tuple[EditorDocument, ResourceAccess]],
        *,
        tenant_id: str,
    ) -> dict[uuid.UUID, Any]:
        owner_ids = tuple(
            {
                document.created_by_user_id
                for document, access in documents
                if access.mode is AccessMode.SHARED
                and document.created_by_user_id is not None
            }
        )
        if users is None or not owner_ids:
            return {}
        return await users.profiles_for_user_ids(
            tenant_id=tenant_id,
            user_ids=owner_ids,
        )

    def _owner_payload(
        document: EditorDocument,
        access: ResourceAccess,
        profiles: dict[uuid.UUID, Any],
    ) -> dict[str, str] | None:
        if (
            access.mode is not AccessMode.SHARED
            or document.created_by_user_id is None
        ):
            return None
        owner_id = document.created_by_user_id
        profile = profiles.get(owner_id)
        if profile is None:
            log.warning(
                "editor shared-document owner profile missing",
                extra={
                    "document_ref": pseudonymous_log_reference(
                        "res", document.id
                    ),
                    "owner_ref": pseudonymous_log_reference("usr", owner_id),
                },
            )
            return {"id": str(owner_id), "name": "Unknown user"}
        return {
            "id": str(owner_id),
            "name": profile.display_name or profile.email or "Unknown user",
        }

    # -- documents -------------------------------------------------------- #

    @router.get("/v1/editor/documents")
    async def list_documents(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """One keyset page of the caller's documents (metadata only)."""
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        scope = req.query_params.get("scope", "owned")
        try:
            documents, next_cursor = await service.list_visible_documents(
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                scope=scope,
                limit=limit,
                after=after,
            )
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        profiles = await _owner_profiles(
            documents,
            tenant_id=principal.tenant_id,
        )
        return list_envelope(
            [
                _document_meta_payload(
                    document,
                    access,
                    owner=_owner_payload(document, access, profiles),
                )
                for document, access in documents
            ],
            next_cursor,
        )

    @router.get("/v1/editor/documents/{document_id}")
    async def get_document(
        document_id: str,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """One document WITH its body (load-on-open)."""
        try:
            document, access = await service.get_document_with_access(
                document_id, visible_to=visible_to
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        profiles = await _owner_profiles(
            [(document, access)],
            tenant_id=visible_to.principal.tenant_id
            if visible_to is not None
            else "default",
        )
        return _document_detail_payload(
            document,
            access,
            owner=_owner_payload(document, access, profiles),
        )

    @router.put("/v1/editor/documents/{document_id}")
    async def save_document(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create or idempotently update a document (autosave upsert)."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        created_at = body.get("created_at")
        updated_at = body.get("updated_at")
        if not isinstance(created_at, (int, float)) or not isinstance(
            updated_at, (int, float)
        ):
            return error_response(
                400, "created_at and updated_at must be numbers",
                "invalid_request_error",
            )
        folder_id = body.get("folder_id")
        if folder_id is not None and not isinstance(folder_id, str):
            return error_response(
                400, "folder_id must be a string or null",
                "invalid_request_error",
            )
        diff_anchor_updated_at = body.get("diff_anchor_updated_at")
        if diff_anchor_updated_at is not None and not isinstance(
            diff_anchor_updated_at, (int, float)
        ):
            return error_response(
                400, "diff_anchor_updated_at must be a number or null",
                "invalid_request_error",
            )
        revision = body.get("revision", 1)
        if not isinstance(revision, int):
            return error_response(
                400, "revision must be an integer", "invalid_request_error"
            )
        try:
            document = await service.save_document(
                id=document_id,
                title=str(body.get("title", "")),
                content_markdown=str(body.get("content_markdown", "")),
                folder_id=folder_id,
                source=str(body.get("source", "blank")),
                source_run_id=(
                    str(body["source_run_id"])
                    if body.get("source_run_id") is not None
                    else None
                ),
                revision=revision,
                diff_anchor_markdown=(
                    str(body["diff_anchor_markdown"])
                    if body.get("diff_anchor_markdown") is not None
                    else None
                ),
                diff_anchor_updated_at=(
                    float(diff_anchor_updated_at)
                    if diff_anchor_updated_at is not None
                    else None
                ),
                created_at=float(created_at),
                updated_at=float(updated_at),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                visible_to=visible_to,
            )
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        except DocumentContentModeConflict:
            return error_response(
                409,
                "Document content is already being synchronized live.",
                "conflict",
                reason="content_mode_changed",
            )
        except DocumentRevisionConflict as exc:
            # A concurrent writer (agent patch apply vs. this autosave)
            # moved the document past the caller's base. 409 with the
            # live revision — the client refetches and rebases (same
            # envelope as the artifact/patch CAS conflicts).
            return error_response(
                409,
                "The document has changed in the meantime; reload it and save again.",
                "conflict",
                current_revision=exc.current_revision,
            )
        return _document_detail_payload(document)

    @router.patch("/v1/editor/documents/{document_id}")
    async def patch_document_metadata(
        document_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Owner-only metadata CAS that never mutates the document body."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        expected = body.get("expected_metadata_revision")
        if not isinstance(expected, int):
            return error_response(
                400,
                "expected_metadata_revision must be an integer",
                "invalid_request_error",
            )
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            return error_response(
                400, "title must be a string", "invalid_request_error"
            )
        set_folder_id = "folder_id" in body
        folder_id = body.get("folder_id")
        if set_folder_id and folder_id is not None and not isinstance(folder_id, str):
            return error_response(
                400,
                "folder_id must be a string or null",
                "invalid_request_error",
            )
        set_diff_anchor_markdown = "diff_anchor_markdown" in body
        diff_anchor_markdown = body.get("diff_anchor_markdown")
        if (
            set_diff_anchor_markdown
            and diff_anchor_markdown is not None
            and not isinstance(diff_anchor_markdown, str)
        ):
            return error_response(
                400,
                "diff_anchor_markdown must be a string or null",
                "invalid_request_error",
            )
        set_diff_anchor_updated_at = "diff_anchor_updated_at" in body
        diff_anchor_updated_at = body.get("diff_anchor_updated_at")
        if set_diff_anchor_updated_at and (
            diff_anchor_updated_at is not None
            and (
                isinstance(diff_anchor_updated_at, bool)
                or not isinstance(diff_anchor_updated_at, (int, float))
            )
        ):
            return error_response(
                400,
                "diff_anchor_updated_at must be a number or null",
                "invalid_request_error",
            )
        if (
            title is None
            and not set_folder_id
            and not set_diff_anchor_markdown
            and not set_diff_anchor_updated_at
        ):
            return error_response(
                400,
                "At least one metadata field is required",
                "invalid_request_error",
            )
        try:
            document = await service.patch_document_metadata(
                document_id,
                expected_metadata_revision=expected,
                title=title,
                folder_id=folder_id,
                set_folder_id=set_folder_id,
                diff_anchor_markdown=diff_anchor_markdown,
                set_diff_anchor_markdown=set_diff_anchor_markdown,
                diff_anchor_updated_at=(
                    float(diff_anchor_updated_at)
                    if diff_anchor_updated_at is not None
                    else None
                ),
                set_diff_anchor_updated_at=set_diff_anchor_updated_at,
                visible_to=visible_to,
            )
        except (DocumentNotFound, FolderNotFound):
            return error_response(404, "Document not found", "not_found")
        except DocumentMetadataConflict as exc:
            return error_response(
                409,
                "Document metadata has changed in the meantime.",
                "conflict",
                reason="metadata_conflict",
                current_metadata_revision=exc.current_revision,
            )
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _document_detail_payload(document)

    @router.delete("/v1/editor/documents/{document_id}", status_code=204)
    async def delete_document(
        document_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete a document and its comments (owner-only)."""
        try:
            await service.delete_document(
                document_id,
                visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")

    # -- comments --------------------------------------------------------- #

    @router.get("/v1/editor/documents/{document_id}/comments")
    async def list_comments(
        document_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """One keyset page of a document's comments (newest first)."""
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        try:
            comments, next_cursor = await service.list_comments(
                document_id, limit=limit, after=after, visible_to=visible_to
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        return list_envelope(
            [_comment_payload(comment) for comment in comments], next_cursor
        )

    @router.post("/v1/editor/documents/{document_id}/comments", status_code=201)
    async def save_comments(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Upsert comments into a document the caller may edit."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        raw_comments = body.get("comments")
        if not isinstance(raw_comments, list):
            return error_response(
                400, "Field 'comments' must be a list",
                "invalid_request_error",
            )
        try:
            stored = await service.save_comments(
                document_id,
                comments=raw_comments,
                visible_to=visible_to,
                caller_user_id=_caller_user_id(principal),
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {
            "object": "list",
            "data": [_comment_payload(comment) for comment in stored],
        }

    @router.delete(
        "/v1/editor/documents/{document_id}/comments/{comment_id}",
        status_code=204,
    )
    async def delete_comment(
        document_id: str,
        comment_id: str,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete one comment from a document the caller may edit."""
        try:
            await service.delete_comment(
                document_id, comment_id, visible_to=visible_to
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")

    @router.put(
        "/v1/editor/documents/{document_id}/comments/{comment_id}/suggestion-draft"
    )
    async def save_comment_suggestion_draft(
        document_id: str,
        comment_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create or revise one creator-private unpublished AI proposal."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        draft = body.get("draft")
        if not isinstance(draft, dict):
            return error_response(
                400,
                "Field 'draft' must be an object",
                "invalid_request_error",
            )
        try:
            stored = await service.save_comment_suggestion_draft(
                document_id,
                comment_id,
                expected_revision=body.get("expected_revision"),
                payload=draft,
                visible_to=visible_to,
            )
        except (DocumentNotFound, SuggestionDraftNotFound):
            return error_response(404, "Draft not found", "not_found")
        except SuggestionDraftRevisionConflict as exc:
            return error_response(
                409,
                "The private draft has changed in the meantime.",
                "conflict",
                current_revision=exc.current_revision,
            )
        except SuggestionDraftTooLarge as exc:
            return error_response(413, str(exc), "request_too_large")
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {"suggestion_draft": suggestion_draft_payload(stored)}

    @router.delete(
        "/v1/editor/documents/{document_id}/comments/{comment_id}/suggestion-draft",
        status_code=204,
    )
    async def delete_comment_suggestion_draft(
        document_id: str,
        comment_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Discard one private draft with an exact revision/patch guard."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            await service.delete_comment_suggestion_draft(
                document_id,
                comment_id,
                expected_revision=body.get("expected_revision"),
                patch_id=body.get("patch_id"),
                visible_to=visible_to,
            )
        except (DocumentNotFound, SuggestionDraftNotFound):
            return error_response(404, "Draft not found", "not_found")
        except SuggestionDraftRevisionConflict as exc:
            return error_response(
                409,
                "The private draft has changed in the meantime.",
                "conflict",
                current_revision=exc.current_revision,
            )
        except EditorValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")

    # -- folders ---------------------------------------------------------- #

    @router.get("/v1/editor/folders")
    async def list_folders(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """All of the caller's editor folders (newest first)."""
        folders = await service.list_folders(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
        )
        return {
            "object": "list",
            "data": [_folder_payload(folder) for folder in folders],
        }

    @router.put("/v1/editor/folders/{folder_id}")
    async def save_folder(
        folder_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create or idempotently update an editor folder."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        created_at = body.get("created_at")
        updated_at = body.get("updated_at")
        if not isinstance(created_at, (int, float)) or not isinstance(
            updated_at, (int, float)
        ):
            return error_response(
                400, "created_at and updated_at must be numbers",
                "invalid_request_error",
            )
        try:
            folder = await service.save_folder(
                id=folder_id,
                title=str(body.get("title", "")),
                created_at=float(created_at),
                updated_at=float(updated_at),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                visible_to=visible_to,
            )
        except FolderNotFound:
            return error_response(404, "Folder not found", "not_found")
        return _folder_payload(folder)

    @router.delete("/v1/editor/folders/{folder_id}", status_code=204)
    async def delete_folder(
        folder_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete a folder; its documents orphan to ungrouped."""
        try:
            await service.delete_folder(
                folder_id,
                visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except FolderNotFound:
            return error_response(404, "Folder not found", "not_found")

    return router


async def _json_object(req: Request) -> Any:
    """Parse the request body as JSON, or ``None`` on a malformed body."""
    try:
        return await req.json()
    except Exception:
        return None
