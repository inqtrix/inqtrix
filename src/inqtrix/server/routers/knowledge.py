"""Knowledge endpoints: collections, documents, and retrieval search.

Registered only when the knowledge engine is enabled — a disabled
deployment has no knowledge surface at all (404s), keeping the
historical route set untouched.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.permissions import AccessMode, ResourceAccess, SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.knowledge.stores.ports import (
    CollectionMaintenanceActive,
    CollectionNotFound,
    DocumentNotFound,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    KnowledgeCollection,
    KnowledgeDocument,
    RetrievalCandidate,
    SourceDeletionConflict,
)
from inqtrix.knowledge.contextualize import (
    ContextualizationDependencyError,
    ContextualizationValidationError,
)
from inqtrix.knowledge.evidence import (
    KnowledgeEvidenceProjector,
    UnverifiedKnowledgeEvidence,
)
from inqtrix.content.ports import FileNotFound
from inqtrix.knowledge.page_mapping import extract_pdf_page_texts
from inqtrix.knowledge.parsing import DocumentParseError
from inqtrix.knowledge.retrieval_warnings import (
    project_retrieval_exclusion_warnings,
    warning_message,
)
from inqtrix.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    list_envelope,
)
from inqtrix.providers.embeddings import EmbeddingProviderError
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetUploadConflict,
)
from inqtrix.quota.models import QuotaDimension, estimate_tokens
from inqtrix.server.routers import (
    quota_admission,
    quota_record,
)
from inqtrix.server.indexing import IndexingJobConflict, IndexingQueueFull
from inqtrix.runs.deletion_operations import DeletionOperationConflict
from inqtrix.services.knowledge_service import (
    ChunkNotFound,
    KnowledgeValidationError,
    SourceDocumentResolutionConflict,
)
from inqtrix.services.file_preparation import PreparationError, canonical_preparation
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)
from inqtrix.source_authority import SourceScope

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def _collection_payload(
    collection: KnowledgeCollection,
    *,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": collection.id,
        "name": collection.name,
        "embedding_model": collection.embedding_model,
        "embedding_dim": collection.embedding_dim,
        "document_count": collection.document_count,
        "created_at": collection.created_at,
        **({"access": access} if access is not None else {}),
    }


def _document_payload(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "collection_id": document.collection_id,
        "title": document.title,
        "metadata": dict(document.metadata),
        "chunk_count": document.chunk_count,
        "created_at": document.created_at,
    }


_SEARCH_TOP_K_MAX = 50
"""Upper bound for the debug-search ``top_k`` (mirrors the agent_context
retrieval-width validator); requests above it are rejected, not silently
clamped."""

_CHUNK_CONTEXT_MAX = 3
"""Upper bound for the chunk-detail ``context`` query parameter
(neighbour chunks per side). Three chunks each way already exceed what
the evidence view renders; larger windows should read the document text
endpoint instead. Requests above it are rejected, not silently
clamped."""


def _client_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Client metadata without the keys only the server may write.

    ``extraction`` names the parser, its revision and the pages a file
    extraction did not capture. Readers show it as a fact about the source,
    so a caller must not be able to supply it.
    """
    if metadata is None:
        return None
    return {key: value for key, value in metadata.items() if key != "extraction"}


def _collection_maintenance_response() -> JSONResponse:
    """Visible conflict while a collection is exclusively reindexed."""
    return error_response(
        409,
        "The collection is being reindexed. Please try again later.",
        "collection_maintenance",
    )


def _candidate_payload(candidate: RetrievalCandidate, *, rank: int) -> dict[str, Any]:
    """One search hit.

    The citation key stays ``(document_id, chunk_index)`` across reindex.
    Only the projected original ``excerpt`` is exposed; synthetic retrieval
    context and the store's internal embedding text never cross this boundary.
    """
    evidence = KnowledgeEvidenceProjector.project(
        candidate, reference_id=f"K{rank}"
    ).as_dict()
    return {
        **evidence,
        "document_title": evidence["title"],
        "rank": rank,
        "score": round(candidate.score, 6),
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the knowledge routes against the container.

    Raises:
        RuntimeError: When called without a wired knowledge service —
            registration is a composition decision, not a runtime
            fallback.
    """
    service = container.knowledge_service
    if service is None:
        raise RuntimeError(
            "build_router(knowledge) requires a wired knowledge service; "
            "register the router only when knowledge is enabled."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    file_service = container.file_service
    asset_service = container.asset_records_service
    quota_service = container.quota_service
    indexing_service = container.indexing_service
    deletion_service = container.asset_deletion_service
    if deletion_service is None:
        raise RuntimeError("knowledge routes require aggregate deletion service")

    async def _wait_for_document_job(
        summary: dict[str, Any],
        *,
        visible_to: UserContext | None,
    ):
        """Compatibility wait over the durable job, never over provider work.

        The asynchronous revision endpoint is the primary contract.  This
        legacy adapter deliberately has no document deadline: a slow provider
        must not become a hidden fallback.  Polling backs off while the job is
        unchanged and resets when durable progress advances.
        """
        if indexing_service is None:
            raise RuntimeError("indexing service is not configured")
        job_store = indexing_service.job_store
        current = summary
        delay_seconds = 0.05
        last_progress = None
        while current.get("status") in {
            "queued",
            "running",
            "cancelling",
        }:
            progress = (
                current.get("status"),
                current.get("phase"),
                current.get("current_batch"),
                current.get("completed_documents"),
            )
            if progress != last_progress:
                delay_seconds = 0.05
                last_progress = progress
            await asyncio.sleep(delay_seconds)
            current = await asyncio.to_thread(
                job_store.get, str(summary["job_id"])
            )
            delay_seconds = min(delay_seconds * 1.7, 1.0)
        status = current.get("status")
        if status in {"completed", "ready_raw_by_user_choice"}:
            return await service.get_document(
                str(current["document_id"]), visible_to=visible_to
            )
        error = current.get("error") or {}
        message = str(error.get("message") or "Indexing failed")
        error_type = str(error.get("type") or "indexing_failed")
        status_code = 502
        if status == "paused_dependency":
            status_code = 503
            if error_type == "dependency_timeout":
                error_type = "contextualization_dependency_error"
        elif status == "paused_validation":
            status_code = 422
            error_type = "contextualization_validation_error"
        elif status == "superseded":
            status_code = 409
            error_type = "document_revision_superseded"
            message = "A newer document revision has already been requested."
        elif status == "cancelled":
            status_code = 409
            error_type = "document_indexing_cancelled"
            message = "Document indexing was cancelled."
        elif status == "expired":
            status_code = 410
            error_type = "document_indexing_expired"
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "message": message,
                    "type": error_type,
                    "job_id": current.get("job_id") or summary.get("job_id"),
                    "job_status": status,
                    "document_id": current.get("document_id"),
                    "revision_id": current.get("revision_id"),
                    "events_url": current.get("events_url")
                    or summary.get("events_url"),
                }
            },
        )

    @router.post("/v1/knowledge/collections", status_code=201)
    async def create_collection(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Create a collection with an immutable embedding model."""
        try:
            body = await req.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        try:
            collection = await service.create_collection(
                name=str(body.get("name", "")),
                embedding_model=(
                    str(body["embedding_model"])
                    if body.get("embedding_model") is not None
                    else None
                ),
                # Scoped principals own what they create; the
                # anonymous/static principals keep minting legacy
                # (visible-to-all) collections.
                created_by_user_id=(
                    principal.user_id
                    if principal.kind in ("oidc_session", "pat")
                    else None
                ),
            )
        except KnowledgeValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except EmbeddingProviderError as exc:
            log.warning(
                "Collection creation failed at the embedding backend (error_type=%s)",
                type(exc).__name__,
            )
            return error_response(502, str(exc), "server_error")
        access = ResourceAccess(
            AccessMode.OWNER
            if principal.kind in ("oidc_session", "pat")
            else AccessMode.UNSCOPED
        )
        return _collection_payload(collection, access=access.as_dict())

    @router.get("/v1/knowledge/collections")
    async def list_collections(
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """The caller's visible collections, newest first."""
        payloads = []
        for collection, access in await service.list_collections_with_access(
            visible_to=visible_to
        ):
            payloads.append(
                _collection_payload(collection, access=access.as_dict())
            )
        return {"object": "list", "data": payloads}

    @router.delete("/v1/knowledge/collections/{collection_id}", status_code=202)
    async def delete_collection(
        collection_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Start durable owner-only deletion of a collection and its vectors."""
        try:
            return await deletion_service.start_knowledge_collection(
                collection_id,
                principal=principal,
                visible_to=visible_to,
            )
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "A deletion operation is already running for this collection.",
                "deletion_conflict",
            )

    @router.post(
        "/v1/knowledge/collections/{collection_id}/document-revisions",
        status_code=202,
    )
    async def start_document_revision(
        collection_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Reserve source text and queue chunk/context/embed/publication work."""
        if indexing_service is None:
            return error_response(
                501,
                "Asynchronous document indexing is unavailable.",
                "not_implemented",
            )
        try:
            body = await req.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return error_response(
                400, "Field 'metadata' must be an object", "invalid_request_error"
            )
        metadata = _client_metadata(metadata)
        asset_id = str(body.get("asset_id", "") or "")
        if asset_id and body.get("text"):
            return error_response(
                400,
                "Provide either 'text' OR 'asset_id', not both.",
                "invalid_request_error",
            )
        if body.get("file_id"):
            return error_response(
                409,
                ("Asynchronous file indexing addresses the prepared asset through "
                "'asset_id', not the raw file through 'file_id'."),
                "file_preparation_required",
            )
        denied = await quota_admission(
            quota_service, principal, QuotaDimension.EMBEDDING_TOKENS
        )
        if denied is not None:
            return denied
        try:
            workspace_id = workspace_id_from_request(req, body)
            prepared_title = str(body.get("title", ""))
            prepared_text = str(body.get("text", ""))
            prepared_metadata = dict(metadata or {})
            page_texts = None
            prepared_source_scope = None
            collection = await service.knowledge.store.get_collection(
                collection_id
            )
            await service.collection_access(
                collection, visible_to, minimum=SharePermission.EDIT
            )
            if asset_id:
                if asset_service is None or file_service is None:
                    return error_response(
                        501,
                        "Server-side file preparation is unavailable.",
                        "not_implemented",
                    )
                asset = await asset_service.get_asset(
                    asset_id, visible_to=visible_to
                )
                if asset.workspace_id != workspace_id:
                    raise AssetNotFound(asset_id)
                try:
                    preparation = await canonical_preparation(
                        asset, assets=asset_service, files=file_service,
                        principal=principal, visible_to=visible_to,
                    )
                except PreparationError as exc:
                    return error_response(exc.status, str(exc), exc.code)
                asset, file_record = preparation.asset, preparation.file
                prepared_title = prepared_title or asset.title
                prepared_text = asset.prepared_text
                page_texts = list(asset.prepared_page_texts) or None
                prepared_metadata.update(
                    {
                        "fileId": asset.id,
                        "file_id": file_record.id,
                        "file_name": file_record.file_name,
                        "source_id": f"asset:{asset.id}",
                        "source_file_sha256": file_record.sha256,
                        "source_parser_id": asset.prepared_parser_id,
                        "extraction": preparation.facts(),
                    }
                )
                prepared_source_scope = SourceScope(
                    tenant_id=asset.tenant_id,
                    source_id=f"asset:{asset.id}",
                    owner_user_id=asset.created_by_user_id,
                    workspace_id=asset.workspace_id,
                )
            summary = await indexing_service.submit_document_revision(
                collection=collection,
                title=prepared_title,
                text=prepared_text,
                metadata=prepared_metadata,
                page_texts=page_texts,
                workspace_id=workspace_id,
                principal=principal,
                visible_to=visible_to,
                source_scope=prepared_source_scope,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except KnowledgeValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except (AssetNotFound, FileNotFound):
            return error_response(404, "File not found", "not_found")
        except AssetDeletionInProgress:
            return error_response(
                409,
                "The source file is being deleted and cannot be indexed.",
                "source_deleted",
            )
        except AssetUploadConflict:
            return error_response(
                409,
                "The source file changed during preparation. Please try again.",
                "source_preparation_conflict",
            )
        except SourceDeletionConflict:
            return error_response(
                409,
                "The source file was deleted and cannot be restored.",
                "source_deleted",
            )
        except IndexingQueueFull:
            return error_response(
                429,
                "Too many queued indexing jobs. Please wait.",
                "rate_limit_error",
            )
        except IndexingJobConflict:
            return error_response(
                409,
                ("This document revision is already being processed by another "
                "authorized session."),
                "document_revision_in_progress",
            )
        return JSONResponse(status_code=202, content=summary)

    @router.post(
        "/v1/knowledge/collections/{collection_id}/documents", status_code=201
    )
    async def add_document(
        collection_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Ingest one document (chunk + embed) synchronously."""
        try:
            body = await req.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return error_response(
                400, "Field 'metadata' must be an object", "invalid_request_error"
            )
        metadata = _client_metadata(metadata)
        file_id = str(body.get("file_id", "") or "")
        if file_id and body.get("text"):
            return error_response(
                400,
                "Provide either 'text' OR 'file_id', not both",
                "invalid_request_error",
            )
        # Embedding-token admission (block-next): a caller already over
        # their monthly embedding budget is denied before any parse or
        # embed call. A single ingestion cannot run away — per-document
        # size is bounded by the document/file limits — so the exact
        # embedded-text tokens are recorded after success.
        denied = await quota_admission(
            quota_service, principal, QuotaDimension.EMBEDDING_TOKENS
        )
        if denied is not None:
            return denied
        metered_by_job = False
        try:
            if file_id:
                prepared_title, prepared_text, prepared_metadata, page_texts = (
                    await _prepare_file_revision(
                        file_id=file_id,
                        title=str(body.get("title", "")),
                        metadata=metadata,
                        principal=principal,
                    )
                )
            else:
                # Text path: the caller reuses already-extracted text (e.g. the
                # server's MarkItDown parse) and does NOT re-parse. When the
                # document still references its original server file
                # (metadata.file_id), run a lightweight page pass over that file
                # (pdfminer only — no MarkItDown re-parse, no LLM) so per-chunk
                # page numbers are still captured for the citation page-jump.
                page_texts = await _page_texts_for_metadata(metadata, principal)
                prepared_title = str(body.get("title", ""))
                prepared_text = str(body.get("text", ""))
                prepared_metadata = metadata
            if indexing_service is not None:
                collection = await service.knowledge.store.get_collection(
                    collection_id
                )
                await service.collection_access(
                    collection, visible_to, minimum=SharePermission.EDIT
                )
                summary = await indexing_service.submit_document_revision(
                    collection=collection,
                    title=prepared_title,
                    text=prepared_text,
                    metadata=prepared_metadata,
                    page_texts=page_texts,
                    workspace_id=workspace_id_from_request(req, body),
                    principal=principal,
                    visible_to=visible_to,
                )
                metered_by_job = True
                document = await _wait_for_document_job(
                    summary, visible_to=visible_to
                )
            else:
                document = await service.add_document(
                    collection_id=collection_id,
                    title=prepared_title,
                    text=prepared_text,
                    metadata=prepared_metadata,
                    visible_to=visible_to,
                    page_texts=page_texts,
                )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except FileNotFound:
            return error_response(404, "File not found", "not_found")
        except DocumentParseError as exc:
            return error_response(422, str(exc), "invalid_request_error")
        except KnowledgeValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except CollectionMaintenanceActive:
            return _collection_maintenance_response()
        except IndexingJobConflict:
            return error_response(
                409,
                ("This document revision is already being processed by another "
                "authorized session."),
                "document_revision_in_progress",
            )
        except DocumentRevisionSuperseded:
            return error_response(
                409,
                "A newer document revision has already been requested.",
                "document_revision_superseded",
            )
        except SourceDeletionConflict:
            return error_response(
                409,
                "The source file was deleted and cannot be restored.",
                "source_deleted",
            )
        except ContextualizationDependencyError as exc:
            return error_response(
                503,
                str(exc),
                exc.error_type,
            )
        except ContextualizationValidationError as exc:
            return error_response(
                422,
                str(exc),
                "contextualization_validation_error",
            )
        except EmbeddingDimensionMismatch as exc:
            log.warning(
                "Embedding dimension conflict during ingestion (error_type=%s)",
                type(exc).__name__,
            )
            return error_response(409, str(exc), "embedding_dimension_mismatch")
        except EmbeddingProviderError as exc:
            log.warning(
                "Ingestion failed at the embedding backend (error_type=%s)",
                type(exc).__name__,
            )
            return error_response(502, str(exc), "server_error")
        # Book the exact embedded-text size (the chunks partition this
        # text; the ~4-char heuristic mirrors the embedding provider's
        # token accounting closely enough for a usage meter).
        if not metered_by_job:
            await quota_record(
                quota_service,
                principal,
                QuotaDimension.EMBEDDING_TOKENS,
                estimate_tokens(document.text),
            )
        return _document_payload(document)

    async def _prepare_file_revision(
        *,
        file_id: str,
        title: str,
        metadata: dict[str, Any] | None,
        principal: Principal,
    ):
        """Fetch and parse a registered file for the legacy sync adapter.

        The file read goes through the FileService so the same
        owner/share rules gate preparation as gate downloads. The additive
        asynchronous revision endpoint accepts already-prepared text only;
        this compatibility path may wait for parsing before it reserves and
        queues the exact same revision build.
        """
        if file_service is None:
            raise KnowledgeValidationError(
                "File ingestion is unavailable (files feature disabled)"
            )
        record, chunks_iter = await file_service.open_stream(
            file_id, principal=principal
        )
        content = await asyncio.to_thread(lambda: b"".join(chunks_iter))
        document_metadata = dict(metadata or {})
        document_metadata.setdefault("file_id", record.id)
        document_metadata.setdefault("file_name", record.file_name)
        return await service.prepare_document_file(
            file_name=record.file_name,
            content=content,
            metadata=document_metadata,
            title=title,
        )

    async def _page_texts_for_metadata(
        metadata: dict[str, Any] | None, principal: Principal
    ) -> list[str] | None:
        """Best-effort per-page text of a text-ingested document's ORIGINAL file
        for chunk page mapping.

        Reads ``metadata.file_id`` through the FileService (same access rules as
        a download), then extracts per-page text with pdfminer only — it does NOT
        re-parse the document (the caller already supplied the text). Returns
        ``None`` when there is no file id, the files feature is off, or the file
        is gone — the document still ingests, just without page numbers.
        """
        source_file_id = str((metadata or {}).get("file_id", "") or "")
        if not source_file_id or file_service is None:
            return None
        try:
            _record, chunks_iter = await file_service.open_stream(
                source_file_id, principal=principal
            )
            content = await asyncio.to_thread(lambda: b"".join(chunks_iter))
        except FileNotFound:
            return None
        return await asyncio.to_thread(extract_pdf_page_texts, content)

    @router.get("/v1/knowledge/collections/{collection_id}/documents")
    async def list_documents(
        collection_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List a collection's documents, newest first (keyset-paginated).

        ``?limit=`` (clamped) and ``?cursor=`` page the list; the response
        carries ``next_cursor`` (null on the last page). Both params are
        optional — without them the first page at the default size returns,
        keeping older callers working."""
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        try:
            documents, next_cursor = await service.list_documents_page(
                collection_id,
                limit=limit,
                after=after,
                visible_to=visible_to,
            )
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        return list_envelope(
            [_document_payload(document) for document in documents],
            next_cursor,
        )

    @router.get(
        "/v1/knowledge/collections/{collection_id}/documents/by-source"
    )
    async def resolve_document_by_source(
        collection_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Resolve one legacy index member without exposing document text."""

        del principal
        source_id = (req.query_params.get("source_id") or "").strip()
        if not source_id:
            return error_response(
                400,
                "Parameter 'source_id' is required",
                "invalid_request_error",
            )
        try:
            document = await service.resolve_document_by_source(
                collection_id,
                source_id,
                visible_to=visible_to,
            )
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except DocumentNotFound:
            return error_response(
                404,
                "No active document found for this source",
                "knowledge_source_unresolved",
            )
        except SourceDocumentResolutionConflict as exc:
            return error_response(
                409,
                str(exc),
                "knowledge_source_ambiguous",
            )
        return _document_payload(document)

    @router.delete("/v1/knowledge/documents/{document_id}", status_code=202)
    async def delete_document(
        document_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Start durable deletion of one document and its vectors."""
        try:
            return await deletion_service.start_knowledge_document(
                document_id,
                principal=principal,
                visible_to=visible_to,
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409,
                "A deletion operation is already running for this document.",
                "deletion_conflict",
            )

    @router.get("/v1/knowledge/documents/{document_id}/text")
    async def document_text(
        document_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Full extracted text of one document (the reader's source).

        Serves the document viewer: the extracted view renders exactly
        what was ingested (what retrieval sees), and snippet/quote
        highlighting works by text search within this payload.
        ``metadata.file_id`` (set by file ingestion) links to the
        original binary under ``/v1/files/{file_id}/content``.
        """
        try:
            document = await service.get_document(
                document_id,
                visible_to=visible_to,
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        payload = _document_payload(document)
        payload["text"] = document.text
        return payload

    @router.get("/v1/knowledge/documents/{document_id}/chunks/{chunk_index}")
    async def document_chunk(
        document_id: str,
        chunk_index: int,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """One chunk plus optional neighbour context (the evidence view).

        ``?context=N`` (0..3, default 0) additionally returns up to N
        chunks before and after the target so a cited quote can be read
        in its surroundings. Scoping mirrors the document text route:
        an unknown and an invisible document stay byte-identical 404s.
        """
        raw_context = req.query_params.get("context")
        context = 0
        if raw_context is not None:
            try:
                context = int(raw_context)
            except ValueError:
                context = -1
            if not 0 <= context <= _CHUNK_CONTEXT_MAX:
                return error_response(
                    400,
                    (
                        f"Parameter 'context' must be between 0 and {_CHUNK_CONTEXT_MAX}"
                    ),
                    "invalid_request_error",
                )
        try:
            chunk, neighbors = await service.get_chunk(
                document_id,
                chunk_index,
                context=context,
                visible_to=visible_to,
            )
        except DocumentNotFound:
            return error_response(404, "Document not found", "not_found")
        except ChunkNotFound:
            return error_response(404, "Chunk not found", "not_found")
        try:
            projected = KnowledgeEvidenceProjector.project_chunk(
                chunk,
                reference_id=f"K{chunk.chunk_index + 1}",
                title="",
            ).as_dict()
            projected_neighbors = [
                KnowledgeEvidenceProjector.project_chunk(
                    neighbor,
                    reference_id=f"K{neighbor.chunk_index + 1}",
                    title="",
                ).as_dict()
                for neighbor in neighbors
            ]
        except UnverifiedKnowledgeEvidence:
            return error_response(
                409,
                (
                    ("This document passage has no verified source span yet and must be "
                    "reindexed.")
                ),
                "knowledge_reindex_required",
            )
        return {
            "chunk_id": projected["chunk_id"],
            "document_id": projected["document_id"],
            "chunk_index": projected["chunk_index"],
            "excerpt": projected["excerpt"],
            "page_number": projected["page_number"],
            "source_span": projected["source_span"],
            "revision_id": projected["revision_id"],
            "generation_id": projected["generation_id"],
            "provenance_status": projected["provenance_status"],
            "neighbors": [
                {
                    "chunk_index": neighbor["chunk_index"],
                    "excerpt": neighbor["excerpt"],
                    "source_span": neighbor["source_span"],
                    "revision_id": neighbor["revision_id"],
                    "generation_id": neighbor["generation_id"],
                    "provenance_status": neighbor["provenance_status"],
                }
                for neighbor in projected_neighbors
            ],
        }

    @router.post("/v1/knowledge/search")
    async def search(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Synchronous retrieval search for debugging and evaluation."""
        try:
            body = await req.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        raw_collection_ids = body.get("collection_ids")
        if raw_collection_ids is not None and not isinstance(raw_collection_ids, list):
            return error_response(
                400,
                "Field 'collection_ids' must be a list",
                "invalid_request_error",
            )
        collection_ids = (
            [str(item) for item in raw_collection_ids]
            if raw_collection_ids is not None
            else None
        )
        raw_top_k = body.get("top_k")
        if raw_top_k is not None and (
            not isinstance(raw_top_k, int)
            or not 1 <= raw_top_k <= _SEARCH_TOP_K_MAX
        ):
            return error_response(
                400,
                f"Field 'top_k' must be between 1 and {_SEARCH_TOP_K_MAX}",
                "invalid_request_error",
            )
        try:
            outcome = await service.search_reported(
                query=str(body.get("query", "")),
                collection_ids=collection_ids,
                top_k=raw_top_k,
                visible_to=visible_to,
            )
        except KnowledgeValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except EmbeddingDimensionMismatch as exc:
            return error_response(409, str(exc), "embedding_dimension_mismatch")
        except EmbeddingProviderError as exc:
            log.warning(
                "Knowledge search failed at the embedding backend (error_type=%s)",
                type(exc).__name__,
            )
            return error_response(502, str(exc), "server_error")
        warnings: list[dict[str, Any]] = []
        if outcome.filtered_collection_ids:
            warnings.append(
                {
                    "code": "collections_filtered",
                    "message": warning_message("collections_filtered"),
                    "filtered_ids": outcome.filtered_collection_ids,
                }
            )
        for warning in project_retrieval_exclusion_warnings(
            outcome.retrieval_exclusions
        ):
            warnings.append(
                {
                    "code": warning.code,
                    "message": warning.message,
                    "count": warning.count,
                }
            )
        for degradation in outcome.retrieval_degradations:
            warnings.append(
                {
                    "code": degradation.reason,
                    "message": warning_message(
                        degradation.reason,
                        final_evidence_complete=degradation.final_evidence_complete,
                    ),
                    **degradation.as_dict(),
                }
            )
        return {
            "object": "list",
            "data": [
                _candidate_payload(candidate, rank=index)
                for index, candidate in enumerate(outcome.candidates, start=1)
            ],
            "warnings": warnings,
        }

    return router
