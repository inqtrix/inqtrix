"""File endpoints (``/v1/files*``): upload, list, metadata, download, delete.

Thin layer over the :class:`~inqtrix.services.file_service.FileService`.
Downloads stream through the API after the permission check — the
object store itself is never exposed to clients.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.ports import FileNotFound, FileRecord
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetUploadConflict,
    GroupNotFound,
    SectionNotFound,
)
from inqtrix.quota.models import QuotaDimension, QuotaSubject
from inqtrix.server.routers import (
    quota_admission,
    quota_record_for_subject,
)
from inqtrix.server.routers.asset_records import asset_meta_payload
from inqtrix.runs.deletion_operations import DeletionOperationConflict
from inqtrix.runs.upload_operations import (
    UploadBinding,
    UploadOperationConflict,
    UploadOperationNotFound,
)
from inqtrix.services.asset_records_service import AssetValidationError
from inqtrix.services.file_preparation import PreparationError, read_file_text
from inqtrix.services.file_service import (
    FileParserUnavailable,
    FileTextExtractionError,
    FileTooLarge,
)
from inqtrix.services.upload_operation_service import (
    UploadBytesRequired,
    UploadExecutionDeferred,
)
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)
from inqtrix.storage.object_store import ObjectStoreError

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_UPLOAD_CHUNK_BYTES = 1024 * 1024
# Binding metadata rides in the multipart envelope next to the file part;
# the cap keeps it far inside the request-size margin above the file limit.
_BINDING_MAX_CHARS = 1024
_OBJECT_STORE_WARNING_INTERVAL_SECONDS = 60.0
_object_store_warning_lock = threading.Lock()
_object_store_last_warning: dict[str, float] = {}
log = logging.getLogger("inqtrix")

_DISPOSITION_SAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]")


def _object_store_unavailable(operation: str, exc: Exception) -> JSONResponse:
    """Return the stable public 503 and rate-limit sanitized diagnostics."""
    now = time.monotonic()
    with _object_store_warning_lock:
        last = _object_store_last_warning.get(operation, 0.0)
        should_log = now - last >= _OBJECT_STORE_WARNING_INTERVAL_SECONDS
        if should_log:
            _object_store_last_warning[operation] = now
    if should_log:
        log.warning(
            "Object-store %s failed (error_type=%s)",
            operation,
            type(exc).__name__,
        )
    return error_response(
        503,
        "Object store temporarily unavailable",
        "object_store_unavailable",
    )


def _content_disposition(file_name: str) -> str:
    """Build a header-injection-safe attachment disposition.

    The original filename is display data from the client; everything
    outside a conservative ASCII subset is replaced so the header can
    never carry CR/LF, quotes, or non-ASCII bytes.
    """
    safe = _DISPOSITION_SAFE.sub("_", file_name).strip() or "download"
    return f'attachment; filename="{safe}"'


def _caller_user_id(principal: Principal) -> uuid.UUID | None:
    return principal.user_id if principal.kind in ("oidc_session", "pat") else None


# Postgres TEXT rejects NUL bytes; every other control character is display
# data and stays the client's problem.
_BINDING_MAX_PAGE_COUNT = 2**31 - 1


def _binding_text_error(**fields: str | None) -> str | None:
    """Visible reason the binding's text fields are unacceptable, or None."""
    for name, value in fields.items():
        if value is None:
            continue
        if len(value) > _BINDING_MAX_CHARS:
            return f"Binding-Feld zu lang: {name}"
        if "\x00" in value:
            return f'Binding field contains invalid characters: {name}'
    return None


def _binding_number_error(
    page_count: int | None,
    created_at: float | None,
    updated_at: float | None,
) -> str | None:
    """Visible reason the binding's numeric fields are unacceptable, or None.

    NaN/Infinity would persist, then poison every JSON render of the record
    (json.dumps refuses non-finite floats), bricking the asset listing; an
    out-of-int32 page_count would blow up only at the DB insert, past the
    point where the rejection can still be clean.
    """
    if page_count is not None and not 0 <= page_count <= _BINDING_MAX_PAGE_COUNT:
        return "page_count is outside the valid range"
    for name, value in (("created_at", created_at), ("updated_at", updated_at)):
        if value is not None and not math.isfinite(value):
            return f'Binding field must be a finite number: {name}'
    return None


def _file_payload(record: FileRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "file_name": record.file_name,
        "content_type": record.content_type,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "workspace_id": record.workspace_id,
        "created_at": record.created_at,
    }


async def _upload_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
        yield chunk


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the file routes against the container.

    Raises:
        RuntimeError: When called without a wired file service —
            registration is a composition decision, not a runtime
            fallback.
    """
    service = container.file_service
    if service is None:
        raise RuntimeError(
            "build_router(files) requires a wired file service."
        )
    asset_service = container.asset_records_service
    deletion_service = getattr(container, "asset_deletion_service", None)
    upload_service = getattr(container, "upload_operation_service", None)
    if asset_service is not None and deletion_service is None:
        raise RuntimeError(
            "build_router(files) requires aggregate deletion wiring when "
            "upload bindings are available."
        )
    if asset_service is not None and upload_service is None:
        raise RuntimeError(
            "build_router(files) requires durable upload-operation wiring "
            "when upload bindings are available."
        )
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    quota_service = container.quota_service

    def _owner_quota_subject(record: FileRecord) -> QuotaSubject | None:
        """The metered owner of a file's stored bytes, or ``None``.

        Stock occupancy belongs to the uploader (``owner_user_id``); legacy
        unscoped uploads carry no subject and are not metered.
        """
        if not record.owner_user_id:
            return None
        return QuotaSubject(
            tenant_id=record.tenant_id,
            user_id=record.owner_user_id,
        )

    router = APIRouter()

    max_file_bytes = container.settings.storage.max_file_bytes
    # Generous allowance for multipart boundaries/headers around the
    # actual file part.
    max_request_bytes = max_file_bytes + 64 * 1024

    async def _discard_unbound_upload(
        record: FileRecord, principal: Principal
    ) -> None:
        """Best-effort rollback after a rejected upload binding.

        The blob and file row are already persisted at this point, but a
        binding rejection means the client will treat the whole upload as
        failed — without the rollback the bytes would linger as an
        invisible, never-booked orphan. Rollback failures are logged and
        swallowed; the rejection response must reach the client either
        way, and the orphan class is the same one a crash between the two
        writes can leave.
        """
        try:
            await service.delete(record.id, principal=principal)
        except Exception as exc:  # noqa: BLE001 - never mask the rejection
            log.warning(
                "Rollback of unbound upload %s failed (error_type=%s).",
                record.id,
                type(exc).__name__,
            )

    @router.post("/v1/files", status_code=201)
    async def upload_file(
        req: Request,
        file: UploadFile,
        asset_id: str | None = Form(None),
        section_id: str | None = Form(None),
        group_id: str | None = Form(None),
        title: str | None = Form(None),
        label: str | None = Form(None),
        origin: str = Form("library"),
        parse_status: str = Form("parsed"),
        parse_warning: str | None = Form(None),
        page_count: int | None = Form(None),
        text_truncated: bool = Form(False),
        parser_id: str | None = Form(None),
        created_at: float | None = Form(None),
        updated_at: float | None = Form(None),
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Accept one multipart file upload and register it.

        With ``asset_id`` + ``section_id`` the endpoint first idempotently
        reserves the stable library asset, then stores the original bytes and
        finalises that reservation.  New clients create the same reservation
        through the lightweight reservation endpoint before sending the
        multipart body; doing it here as well keeps older clients on the same
        service contract.  A repeated request for an already-finalised,
        identical asset returns the existing file and never creates a second
        blob.  Without binding fields the endpoint stores the raw file exactly
        as before.

        The Content-Length precheck below rejects oversized requests
        before the framework parses (and disk-spools) the body; the
        service's running-size check remains the authoritative limit
        for chunked or lying clients. Deployments exposed directly to
        untrusted networks should additionally cap request bodies at
        the reverse proxy.
        """
        declared = req.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_request_bytes:
            return error_response(
                413,
                f'File too large (limit {max_file_bytes} bytes)',
                "invalid_request_error",
            )
        try:
            workspace_id = workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        bind_requested = asset_id is not None or section_id is not None
        if bind_requested:
            if not asset_id or not section_id:
                return error_response(
                    400,
                    "asset_id and section_id are both required for an upload binding",
                    "invalid_request_error",
                )
            binding_error = _binding_text_error(
                asset_id=asset_id, section_id=section_id, group_id=group_id,
                title=title, label=label, origin=origin,
                parse_status=parse_status, parse_warning=parse_warning,
                parser_id=parser_id,
            ) or _binding_number_error(page_count, created_at, updated_at)
            if binding_error is not None:
                return error_response(400, binding_error, "invalid_request_error")
            if asset_service is None:
                return error_response(
                    501,
                    "Upload binding is unavailable on this server",
                    "not_implemented",
                )
        reservation = None

        async def _mark_reservation_failed(message: str) -> None:
            if reservation is None or asset_id is None or asset_service is None:
                return
            try:
                await asset_service.mark_upload_failed(
                    asset_id,
                    visible_to=visible_to,
                    message=message,
                )
            except (AssetDeletionInProgress, AssetNotFound):
                # A concurrent aggregate deletion owns the visible lifecycle;
                # never overwrite it with an upload status.
                return
            except Exception as exc:  # noqa: BLE001 - preserve primary error
                log.warning(
                    "Could not persist upload failure status for %s (error_type=%s).",
                    asset_id,
                    type(exc).__name__,
                )

        # Stored-bytes admission (block-next) precedes spooling and a new
        # reservation for every new physical upload.  A replay/resume whose
        # durable operation or exact server-file binding already exists must
        # remain repairable even if the account is now full: it creates no
        # second blob, and refusing it would strand already occupied bytes
        # outside the quota ledger.  Multipart Content-Length includes envelope
        # overhead and is never used as the amount; max_file_bytes bounds the
        # one admitted upload.
        existing_upload_anchor = None
        if bind_requested:
            assert asset_id is not None and asset_service is not None
            try:
                existing_upload_anchor = await asset_service.get_asset(
                    asset_id, visible_to=visible_to
                )
            except AssetNotFound:
                pass
        needs_storage_admission = (
            not bind_requested
            or existing_upload_anchor is None
            or (
                existing_upload_anchor.server_file_id is None
                and existing_upload_anchor.upload_operation_id is None
            )
        )
        if needs_storage_admission:
            denied = await quota_admission(
                quota_service, principal, QuotaDimension.STORED_BYTES
            )
            if denied is not None:
                return denied

        if bind_requested:
            assert asset_id is not None and section_id is not None
            now = time.time()
            try:
                deletion_service.assert_upload_allowed(
                    asset_id,
                    principal=principal,
                    workspace_id=workspace_id,
                    section_id=section_id,
                )
                reservation = await asset_service.reserve_upload(
                    id=asset_id,
                    section_id=section_id,
                    group_id=group_id,
                    title=title if title is not None else (file.filename or "upload"),
                    label=label if label is not None else (file.filename or "upload"),
                    file_name=file.filename or "upload",
                    mime_type=file.content_type or "application/octet-stream",
                    origin=origin,
                    page_count=page_count,
                    parse_status=parse_status,
                    parse_warning=parse_warning,
                    text_truncated=text_truncated,
                    size_bytes=max(0, int(file.size or 0)),
                    parser_id=parser_id,
                    created_at=created_at if created_at is not None else now,
                    updated_at=updated_at if updated_at is not None else now,
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

        if bind_requested:
            assert upload_service is not None
            spooled = None
            try:
                spooled = await service.spool_upload(_upload_chunks(file))
                binding = UploadBinding(
                    section_id=section_id,
                    group_id=group_id,
                    title=title if title is not None else (file.filename or "upload"),
                    label=label if label is not None else (file.filename or "upload"),
                    origin=origin,
                    page_count=page_count,
                    parse_status=parse_status,
                    parse_warning=parse_warning,
                    text_truncated=text_truncated,
                    parser_id=parser_id,
                    created_at=(created_at if created_at is not None else time.time()),
                )
                attempt = await upload_service.start_from_spool(
                    asset_id=asset_id,
                    spooled=spooled,
                    file_name=file.filename or "upload",
                    content_type=file.content_type or "application/octet-stream",
                    binding=binding,
                    visible_to=visible_to,
                )
                record, bound_asset, operation = await upload_service.execute(
                    attempt,
                    visible_to=visible_to,
                    spooled=spooled,
                )
            except FileTooLarge as exc:
                await _mark_reservation_failed(
                    "The upload was cancelled because the file is too large."
                )
                return error_response(
                    413,
                    f'File too large (limit {exc.limit_bytes} bytes)',
                    "invalid_request_error",
                )
            except UploadExecutionDeferred as exc:
                current_asset = await asset_service.get_asset(
                    asset_id, visible_to=visible_to
                )
                return JSONResponse(
                    status_code=202,
                    content={
                        "object": "upload_operation",
                        "asset": asset_meta_payload(current_asset),
                        "upload_operation": exc.operation,
                    },
                )
            except UploadBytesRequired:
                return error_response(
                    409,
                    "The same file must be uploaded again to resume",
                    "upload_bytes_required",
                )
            except (AssetUploadConflict, UploadOperationConflict) as exc:
                return error_response(409, str(exc), "upload_binding_conflict")
            except AssetValidationError as exc:
                return error_response(400, str(exc), "invalid_request_error")
            except AssetDeletionInProgress:
                return error_response(
                    409,
                    "File was deleted during upload",
                    "asset_deletion_in_progress",
                )
            except SectionNotFound:
                return error_response(404, "Section not found", "not_found")
            except GroupNotFound:
                return error_response(404, "Group not found", "not_found")
            except AssetNotFound:
                return error_response(404, "Asset not found", "not_found")
            finally:
                if spooled is not None:
                    spooled.path.unlink(missing_ok=True)
            payload = _file_payload(record)
            payload["asset"] = asset_meta_payload(bound_asset)
            payload["upload_operation"] = operation
            return JSONResponse(
                status_code=200 if attempt.already_ready else 201,
                content=payload,
            )

        # Raw, deliberately unbound file uploads keep the compatibility path.
        try:
            record = await service.upload(
                chunks=_upload_chunks(file),
                file_name=file.filename or "upload",
                content_type=file.content_type or "application/octet-stream",
                principal=principal,
                workspace_id=workspace_id,
            )
        except FileTooLarge as exc:
            return error_response(
                413,
                f'File too large (limit {exc.limit_bytes} bytes)',
                "invalid_request_error",
            )
        except ObjectStoreError as exc:
            return _object_store_unavailable("upload", exc)
        await quota_record_for_subject(
            quota_service,
            _owner_quota_subject(record),
            QuotaDimension.STORED_BYTES,
            record.size_bytes,
        )
        payload = _file_payload(record)
        return JSONResponse(status_code=201, content=payload)

    @router.get("/v1/uploads")
    async def list_upload_operations(
        req: Request,
        limit: int = 100,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            workspace_id = workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        bounded = min(200, max(1, limit))
        return {
            "object": "list",
            "data": upload_service.operations.list_operations(
                tenant_id=principal.tenant_id,
                created_by_user_id=_caller_user_id(principal),
                workspace_id=workspace_id,
                limit=bounded,
            ),
        }

    @router.get("/v1/uploads/{operation_id}")
    async def get_upload_operation(
        operation_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            workspace_id = workspace_id_from_request(req)
            return upload_service.operations.get(
                operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=_caller_user_id(principal),
                workspace_id=workspace_id,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except UploadOperationNotFound:
            return error_response(404, "Upload operation not found", "not_found")

    @router.post("/v1/uploads/{operation_id}/retry", status_code=202)
    async def retry_upload_operation(
        operation_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            workspace_id = workspace_id_from_request(req)
            return upload_service.operations.retry(
                operation_id,
                tenant_id=principal.tenant_id,
                created_by_user_id=_caller_user_id(principal),
                workspace_id=workspace_id,
            )
        except UploadOperationNotFound:
            return error_response(404, "Upload operation not found", "not_found")
        except UploadOperationConflict as exc:
            error_type = (
                "upload_bytes_required"
                if "bytes are required" in str(exc)
                else "upload_operation_conflict"
            )
            return error_response(409, str(exc), error_type)

    @router.get("/v1/files")
    async def list_files(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List files visible to the principal, newest first."""
        try:
            workspace_id = workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        records = await service.list(
            principal=principal,
            user_context_is_scoped=visible_to is not None,
            workspace_id=workspace_id,
        )
        return {
            "object": "list",
            "data": [_file_payload(record) for record in records],
        }

    @router.get("/v1/files/{file_id}")
    async def get_file(
        file_id: str,
        principal: Principal = Depends(principal_dep),
    ):
        """Return one file's metadata after the access check."""
        try:
            record = await service.get(file_id, principal=principal)
        except FileNotFound:
            return error_response(404, "File not found", "not_found")
        return _file_payload(record)

    @router.get("/v1/files/{file_id}/content")
    async def download_file(
        file_id: str,
        principal: Principal = Depends(principal_dep),
    ):
        """Stream the file bytes after the access check."""
        try:
            record, chunks = await service.open_stream(
                file_id, principal=principal
            )
        except FileNotFound:
            return error_response(404, "File not found", "not_found")
        except ObjectStoreError as exc:
            return _object_store_unavailable("download", exc)
        return StreamingResponse(
            chunks,
            media_type=record.content_type,
            headers={
                "Content-Length": str(record.size_bytes),
                "Content-Disposition": _content_disposition(record.file_name),
                # The content type is client-declared at upload; never
                # let browsers second-guess it into something active.
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/v1/files/{file_id}/text")
    async def file_text(
        file_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Server-side extracted text of one file.

        A library file answers with the canonical preparation its upload
        produced (verified on this read, never parsed again); a raw upload
        outside the library is parsed here. ``extraction`` names the parser
        revision and the pages that produced no text. Returns ``501`` when no
        parser is configured, ``422`` when the file cannot be converted and
        ``409`` while a library file's preparation is pending, failed or
        inconsistent -- a visible error, never a silent empty body.
        """
        try:
            source = await read_file_text(
                file_id, files=service, assets=asset_service,
                principal=principal, visible_to=visible_to,
            )
        except FileParserUnavailable as exc:
            return error_response(501, str(exc), "not_implemented")
        except FileNotFound:
            return error_response(404, "File not found", "not_found")
        except ObjectStoreError as exc:
            return _object_store_unavailable("text extraction", exc)
        except FileTextExtractionError as exc:
            return error_response(422, str(exc), "unprocessable_entity")
        except PreparationError as exc:
            return error_response(exc.status, str(exc), exc.code)
        return {
            "file_id": source.file.id,
            "parser_id": source.facts["parser_id"],
            "text": source.text,
            "representation": source.representation,
            "extraction": source.facts,
        }

    @router.delete("/v1/files/{file_id}", status_code=204)
    async def delete_file(
        file_id: str,
        principal: Principal = Depends(principal_dep),
    ):
        """Delete an unbound file after the manage-access check.

        A file referenced by an asset belongs to the asset aggregate and may
        only be removed through ``DELETE /v1/assets/{asset_id}``.  Allowing
        this lower-level route to remove it would leave the asset, knowledge
        evidence, and quota receipt inconsistent.
        """
        try:
            await service.get(file_id, principal=principal)
            bound_asset = (
                await asset_service.find_asset_by_server_file_id(file_id)
                if asset_service is not None
                else None
            )
            if bound_asset is not None:
                return error_response(
                    409,
                    "Bound original files must be deleted through the asset",
                    "asset_aggregate_required",
                )
            record = await service.delete(file_id, principal=principal)
        except FileNotFound:
            return error_response(404, "File not found", "not_found")
        except ObjectStoreError as exc:
            return _object_store_unavailable("delete", exc)
        await quota_record_for_subject(
            quota_service,
            _owner_quota_subject(record),
            QuotaDimension.STORED_BYTES,
            -record.size_bytes,
        )
        return Response(status_code=204)

    return router
