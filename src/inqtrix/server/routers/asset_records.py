"""File-asset-record endpoints (project tier).

Thin per-surface router mirroring chat_history / editor_persistence: parse,
delegate to
:class:`~inqtrix.services.asset_records_service.AssetRecordsService`,
serialize. Records are private per owner within a workspace (no sharing
surface). The asset LIST returns metadata only; the single-asset GET
returns the extracted text (load-on-open).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    list_envelope,
)
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetGroup,
    AssetNotFound,
    AssetRecord,
    AssetSection,
    AssetUploadConflict,
    GroupNotFound,
    SectionNotFound,
)
from inqtrix.services.asset_records_service import AssetValidationError
from inqtrix.runs.deletion_operations import (
    DeletionOperationConflict,
    DeletionOperationNotFound,
    DeletionTargetKind,
)
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def _caller_user_id(principal: Principal) -> uuid.UUID | None:
    return principal.user_id if principal.kind in ("oidc_session", "pat") else None


def _section_payload(s: AssetSection) -> dict[str, Any]:
    return {"id": s.id, "kind": s.kind, "title": s.title,
            "semantic_role": s.semantic_role,
            "created_at": s.created_at, "updated_at": s.updated_at}


def _group_payload(g: AssetGroup) -> dict[str, Any]:
    return {"id": g.id, "section_id": g.section_id, "title": g.title,
            "created_at": g.created_at, "updated_at": g.updated_at}


def asset_meta_payload(a: AssetRecord) -> dict[str, Any]:
    """Asset metadata as its public wire shape (no body).

    Shared contract: the asset list/PUT responses here and the upload
    response's ``asset`` object (``POST /v1/files`` with binding) must
    describe an asset identically, so clients can treat both as the
    same record.
    """
    return {
        "id": a.id, "section_id": a.section_id, "group_id": a.group_id,
        "title": a.title, "label": a.label, "file_name": a.file_name,
        "mime_type": a.mime_type, "origin": a.origin, "page_count": a.page_count,
        "parse_status": a.parse_status, "parse_warning": a.parse_warning,
        "text_truncated": a.text_truncated, "size_bytes": a.size_bytes,
        "server_file_id": a.server_file_id, "parser_id": a.parser_id,
        "prepared_parser_id": a.prepared_parser_id,
        "prepared_parser_revision": a.prepared_parser_revision,
        "prepared_content_hash": a.prepared_content_hash,
        "prepared_at": a.prepared_at,
        "lifecycle_status": a.lifecycle_status,
        "deletion_operation_id": a.deletion_operation_id,
        "deletion_stage": a.deletion_stage,
        "deletion_error": a.deletion_error,
        "upload_status": a.upload_status,
        "upload_error": a.upload_error,
        "upload_operation_id": a.upload_operation_id,
        "created_at": a.created_at, "updated_at": a.updated_at,
    }


def _asset_detail_payload(a: AssetRecord) -> dict[str, Any]:
    return {
        **asset_meta_payload(a),
        "extracted_text": a.extracted_text,
        "prepared_text": a.prepared_text,
    }


def _require_numbers(body: dict[str, Any]) -> bool:
    return isinstance(body.get("created_at"), (int, float)) and isinstance(
        body.get("updated_at"), (int, float)
    )


def build_router(container: "AppContainer") -> APIRouter:
    service = container.asset_records_service
    if service is None:
        raise RuntimeError("build_router(asset_records) requires a wired service.")
    deletion_service = getattr(container, "asset_deletion_service", None)
    if deletion_service is None:
        raise RuntimeError(
            "build_router(asset_records) requires aggregate deletion wiring."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency

    # -- sections --------------------------------------------------------- #

    @router.get("/v1/assets/sections")
    async def list_sections(req: Request, principal: Principal = Depends(principal_dep)):
        sections = await service.list_sections(
            caller_user_id=_caller_user_id(principal), workspace_id=workspace_id_from_request(req)
        )
        return {"object": "list", "data": [_section_payload(s) for s in sections]}

    @router.put("/v1/assets/default-sections")
    async def ensure_default_sections(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Return the canonical prepared sections for this exact scope."""

        sections = await service.ensure_default_sections(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
        )
        return {"object": "list", "data": [_section_payload(s) for s in sections]}

    @router.put("/v1/assets/sections/{section_id}")
    async def save_section(
        section_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        try:
            workspace_id = workspace_id_from_request(req)
            deletion_service.assert_target_allowed(
                DeletionTargetKind.SECTION,
                section_id,
                principal=principal,
                workspace_id=workspace_id,
            )
            section = await service.save_section(
                id=section_id, kind=str(body.get("kind", "custom")),
                title=str(body.get("title", "")), created_at=float(body["created_at"]),
                updated_at=float(body["updated_at"]), caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id, visible_to=visible_to,
            )
        except AssetValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except SectionNotFound:
            return error_response(404, "Section not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "Section is being deleted and cannot be restored",
                "deletion_in_progress",
            )
        return _section_payload(section)

    @router.delete("/v1/assets/sections/{section_id}", status_code=202)
    async def delete_section(
        section_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            summary = await deletion_service.start_section(
                section_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id_from_request(req),
            )
        except SectionNotFound:
            return error_response(404, "Section not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "At least one file is already being deleted",
                "deletion_in_progress",
            )
        return JSONResponse(status_code=202, content=summary)

    # -- groups ----------------------------------------------------------- #

    @router.get("/v1/assets/groups")
    async def list_groups(req: Request, principal: Principal = Depends(principal_dep)):
        groups = await service.list_groups(
            caller_user_id=_caller_user_id(principal), workspace_id=workspace_id_from_request(req)
        )
        return {"object": "list", "data": [_group_payload(g) for g in groups]}

    @router.put("/v1/assets/groups/{group_id}")
    async def save_group(
        group_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        section_id = body.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            return error_response(400, "section_id is required", "invalid_request_error")
        try:
            workspace_id = workspace_id_from_request(req)
            deletion_service.assert_target_allowed(
                DeletionTargetKind.GROUP,
                group_id,
                principal=principal,
                workspace_id=workspace_id,
            )
            group = await service.save_group(
                id=group_id, section_id=section_id, title=str(body.get("title", "")),
                created_at=float(body["created_at"]), updated_at=float(body["updated_at"]),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id, visible_to=visible_to,
            )
        except GroupNotFound:
            return error_response(404, "Group not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "Group is being deleted and cannot be restored",
                "deletion_in_progress",
            )
        return _group_payload(group)

    @router.delete("/v1/assets/groups/{group_id}", status_code=202)
    async def delete_group(
        group_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            summary = await deletion_service.start_group(
                group_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id_from_request(req),
            )
        except GroupNotFound:
            return error_response(404, "Group not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "Group is already being deleted",
                "deletion_in_progress",
            )
        return JSONResponse(status_code=202, content=summary)

    # -- assets ----------------------------------------------------------- #

    @router.get("/v1/assets")
    async def list_assets(req: Request, principal: Principal = Depends(principal_dep)):
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        assets, next_cursor = await service.list_assets(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req), limit=limit, after=after,
        )
        return list_envelope([asset_meta_payload(a) for a in assets], next_cursor)

    @router.post("/v1/assets/deletion-operations", status_code=202)
    async def start_bulk_deletion(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        asset_ids = body.get("asset_ids") if isinstance(body, dict) else None
        if not isinstance(asset_ids, list) or not all(
            isinstance(item, str) and item for item in asset_ids
        ):
            return error_response(
                400,
                "asset_ids must be a non-empty list of IDs",
                "invalid_request_error",
            )
        try:
            summary = await deletion_service.start_bulk(
                asset_ids,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id_from_request(req),
            )
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except AssetNotFound:
            return error_response(404, "Asset not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "At least one file is already being deleted",
                "deletion_in_progress",
            )
        return JSONResponse(status_code=202, content=summary)

    @router.get("/v1/deletion-operations")
    @router.get("/v1/assets/deletion-operations")
    async def list_deletion_operations(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        operations, next_cursor = deletion_service.list_operations(
            principal=principal,
            workspace_id=workspace_id_from_request(req),
            limit=clamp_limit(req.query_params.get("limit")),
            after=after,
        )
        return list_envelope(operations, next_cursor)

    @router.get("/v1/deletion-operations/{operation_id}")
    @router.get("/v1/assets/deletion-operations/{operation_id}")
    async def get_deletion_operation(
        operation_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            return deletion_service.get(
                operation_id,
                principal=principal,
                workspace_id=workspace_id_from_request(req),
            )
        except DeletionOperationNotFound:
            return error_response(
                404, "Deletion operation not found", "not_found"
            )

    @router.post("/v1/deletion-operations/{operation_id}/retry")
    @router.post("/v1/assets/deletion-operations/{operation_id}/retry")
    async def retry_deletion_operation(
        operation_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            return JSONResponse(
                status_code=202,
                content=await deletion_service.retry(
                    operation_id,
                    principal=principal,
                    workspace_id=workspace_id_from_request(req),
                ),
            )
        except DeletionOperationNotFound:
            return error_response(
                404, "Deletion operation not found", "not_found"
            )
        except DeletionOperationConflict:
            return error_response(
                409,
                "Deletion operation cannot be retried in this state",
                "deletion_not_retryable",
            )

    @router.get("/v1/assets/{asset_id}")
    async def get_asset(asset_id: str, visible_to: UserContext | None = Depends(user_context_dep)):
        try:
            asset = await service.get_asset(asset_id, visible_to=visible_to)
        except AssetNotFound:
            return error_response(404, "Asset not found", "not_found")
        return _asset_detail_payload(asset)

    @router.post("/v1/assets/{asset_id}/upload-reservation", status_code=201)
    async def reserve_asset_upload(
        asset_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Reserve the stable asset identity before original bytes move.

        The subsequent multipart upload carries the same binding and only
        finalises this record.  Repeating an identical reservation is safe;
        changing its target or reusing a deleted id is a visible conflict.
        """

        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        section_id = body.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            return error_response(
                400, "section_id is required", "invalid_request_error"
            )
        group_id = body.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            return error_response(
                400, "group_id must be a string or null", "invalid_request_error"
            )
        page_count = body.get("page_count")
        if page_count is not None and not isinstance(page_count, int):
            return error_response(
                400, "page_count must be an integer or null", "invalid_request_error"
            )
        size_bytes = body.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            return error_response(
                400, "size_bytes must be a non-negative integer", "invalid_request_error"
            )
        workspace_id = workspace_id_from_request(req)
        try:
            deletion_service.assert_upload_allowed(
                asset_id,
                principal=principal,
                workspace_id=workspace_id,
                section_id=section_id,
            )
            asset = await service.reserve_upload(
                id=asset_id,
                section_id=section_id,
                group_id=group_id,
                title=str(body.get("title", "")),
                label=str(body.get("label", "")),
                file_name=str(body.get("file_name", "")),
                mime_type=str(body.get("mime_type", "application/octet-stream")),
                origin=str(body.get("origin", "library")),
                page_count=page_count,
                parse_status=str(body.get("parse_status", "parsed")),
                parse_warning=(
                    str(body["parse_warning"])
                    if body.get("parse_warning") is not None
                    else None
                ),
                text_truncated=bool(body.get("text_truncated", False)),
                size_bytes=size_bytes,
                parser_id=(
                    str(body["parser_id"])
                    if body.get("parser_id") is not None
                    else None
                ),
                created_at=float(body["created_at"]),
                updated_at=float(body["updated_at"]),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
        except (AssetDeletionInProgress, DeletionOperationConflict):
            return error_response(
                409,
                "File is being deleted and cannot be uploaded again",
                "asset_deletion_in_progress",
            )
        except AssetUploadConflict as exc:
            return error_response(409, str(exc), "upload_binding_conflict")
        except AssetValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except SectionNotFound:
            return error_response(404, "Section not found", "not_found")
        except GroupNotFound:
            return error_response(404, "Group not found", "not_found")
        except AssetNotFound:
            return error_response(404, "Asset not found", "not_found")
        return JSONResponse(status_code=201, content=asset_meta_payload(asset))

    @router.put("/v1/assets/{asset_id}")
    async def save_asset(
        asset_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        section_id = body.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            return error_response(400, "section_id is required", "invalid_request_error")
        group_id = body.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            return error_response(400, "group_id must be a string or null", "invalid_request_error")
        page_count = body.get("page_count")
        if page_count is not None and not isinstance(page_count, int):
            return error_response(400, "page_count must be an integer or null", "invalid_request_error")
        try:
            workspace_id = workspace_id_from_request(req)
            deletion_service.assert_upload_allowed(
                asset_id,
                principal=principal,
                workspace_id=workspace_id,
                section_id=section_id,
            )
            asset = await service.save_asset(
                id=asset_id, section_id=section_id, group_id=group_id,
                title=str(body.get("title", "")), label=str(body.get("label", "")),
                file_name=str(body.get("file_name", "")),
                mime_type=str(body.get("mime_type", "")),
                origin=str(body.get("origin", "library")), page_count=page_count,
                parse_status=str(body.get("parse_status", "parsed")),
                parse_warning=(str(body["parse_warning"]) if body.get("parse_warning") is not None else None),
                text_truncated=bool(body.get("text_truncated", False)),
                size_bytes=int(body.get("size_bytes", 0)),
                server_file_id=(str(body["server_file_id"]) if body.get("server_file_id") is not None else None),
                parser_id=(str(body["parser_id"]) if body.get("parser_id") is not None else None),
                extracted_text=str(body.get("extracted_text", "")),
                created_at=float(body["created_at"]), updated_at=float(body["updated_at"]),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id, visible_to=visible_to,
            )
        except AssetUploadConflict as exc:
            return error_response(409, str(exc), "upload_binding_conflict")
        except AssetValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except AssetDeletionInProgress:
            return error_response(
                409,
                "File is already being deleted and cannot be changed",
                "asset_deletion_in_progress",
            )
        except DeletionOperationConflict:
            return error_response(
                409,
                "File was deleted and cannot be restored",
                "asset_deletion_in_progress",
            )
        except AssetNotFound:
            return error_response(404, "Asset not found", "not_found")
        return _asset_detail_payload(asset)

    @router.delete("/v1/assets/{asset_id}", status_code=202)
    async def delete_asset(
        asset_id: str, req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            summary = await deletion_service.start_asset(
                asset_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id_from_request(req),
            )
        except AssetNotFound:
            return error_response(404, "Asset not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409, "Asset is already being deleted", "deletion_in_progress"
            )
        return JSONResponse(status_code=202, content=summary)

    return router


async def _json_object(req: Request) -> Any:
    try:
        return await req.json()
    except Exception:
        return None
