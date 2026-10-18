"""Background knowledge-indexing endpoints and event streams.

Registered alongside the knowledge surface (only when the knowledge
engine is enabled). Collection-generation submission resolves and edit-checks
the target collection, gates embedding-token quota, and queues work on the
:class:`~inqtrix.server.indexing.IndexingJobStore`. Job authorization follows
the parent collection: viewers may inspect its jobs and editors may cancel
them, independent of which authorized user originally submitted the job.
"""

from __future__ import annotations

import asyncio
from functools import partial
from queue import Empty
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import (
    Principal,
    UserContext,
    resolve_live_principal,
)
from inqtrix.knowledge.stores.ports import CollectionNotFound, KnowledgeError
from inqtrix.quota.models import QuotaDimension
from inqtrix.server.indexing import (
    TERMINAL_INDEXING_EVENTS,
    IndexingJobConflict,
    IndexingJobNotFound,
    IndexingQueueFull,
    IndexingResumeUnavailable,
    format_sse_event,
)
from inqtrix.server.routers import quota_admission
from inqtrix.server.stream_authorization import (
    GenerationGatedFrameAuthorization,
)
from inqtrix.services.indexing_service import ReindexUnsupported
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the indexing-operation routes against the container.

    Raises:
        RuntimeError: When called without a wired indexing service —
            registration is a composition decision, not a runtime
            fallback (mirrors the knowledge router).
    """
    indexing_service = container.indexing_service
    lanes = container.execution_lanes
    knowledge_service = container.knowledge_service
    if indexing_service is None or knowledge_service is None:
        raise RuntimeError(
            "build_router(indexing) requires a wired indexing service; "
            "register the router only when knowledge is enabled."
        )
    job_store = indexing_service.job_store
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    quota_service = container.quota_service

    async def _authorized_job(
        job_id: str,
        *,
        visible_to: UserContext | None,
        require_edit: bool = False,
    ) -> dict[str, Any]:
        """Resolve one job through its parent collection authority."""
        try:
            summary = await asyncio.to_thread(job_store.get, job_id)
            collection = await knowledge_service.knowledge.store.get_collection(
                str(summary["collection_id"])
            )
            await knowledge_service.collection_access(
                collection,
                visible_to,
                minimum=(
                    SharePermission.EDIT
                    if require_edit
                    else SharePermission.VIEW
                ),
            )
        except (CollectionNotFound, IndexingJobNotFound, KeyError) as exc:
            raise IndexingJobNotFound(job_id) from exc
        return summary

    @router.post("/v1/knowledge/collections/{collection_id}/reindex")
    async def start_reindex(
        collection_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Queue a background re-embed of one collection's documents."""
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        index_id = body.get("index_id")
        index_id = str(index_id) if index_id is not None else None
        try:
            workspace_id = workspace_id_from_request(req, body)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)

        # Embedding-token admission (block-next): a caller already over
        # their monthly embedding budget is denied before the job starts.
        # The job's real per-document spend is booked as it embeds.
        denied = await quota_admission(
            quota_service, principal, QuotaDimension.EMBEDDING_TOKENS
        )
        if denied is not None:
            return denied

        try:
            collection = await knowledge_service.knowledge.store.get_collection(
                collection_id
            )
            await knowledge_service.collection_access(
                collection,
                visible_to,
                minimum=SharePermission.EDIT,
            )
            summary = await asyncio.to_thread(
                indexing_service.submit,
                collection=collection,
                index_id=index_id,
                workspace_id=workspace_id,
                principal=principal,
            )
        except CollectionNotFound:
            return error_response(404, "Collection not found", "not_found")
        except ReindexUnsupported as exc:
            return error_response(501, str(exc), "not_implemented")
        except IndexingJobConflict:
            return error_response(
                409,
                "Indexing is already running for this collection.",
                "reindex_in_progress",
            )
        except IndexingQueueFull:
            return error_response(
                429,
                "Too many queued indexing jobs. Please wait.",
                "rate_limit_error",
            )
        return JSONResponse(status_code=202, content=summary)

    @router.get("/v1/knowledge/indexing-jobs")
    async def list_jobs(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List visible indexing operations, newest first.

        Optional ``?collection_id=`` narrows to one collection's history
        (the inline "last N" view); the resume path lists all active
        jobs for the workspace.
        """
        try:
            workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        collection_id = req.query_params.get("collection_id") or None
        operation_kind = (
            req.query_params.get("operation_kind") or "collection_generation"
        )
        if operation_kind not in {
            "collection_generation",
            "document_revision",
        }:
            return error_response(
                400, "Unknown index operation", "invalid_request_error"
            )
        visible_collections = await knowledge_service.list_collections(
            visible_to=visible_to,
        )
        visible_collection_ids = {
            collection.id for collection in visible_collections
        }
        jobs = await asyncio.to_thread(
            job_store.list,
            collection_id=collection_id,
        )
        return {
            "object": "list",
            "data": [
                job
                for job in jobs
                if job.get("collection_id") in visible_collection_ids
                and job.get("operation_kind") == operation_kind
            ],
        }

    @router.get("/v1/knowledge/indexing-jobs/{job_id}")
    async def get_job(
        job_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Return the current public summary for one indexing operation."""
        try:
            workspace_id_from_request(req)
            return await _authorized_job(
                job_id,
                visible_to=visible_to,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except IndexingJobNotFound:
            return error_response(404, "Indexing job not found", "not_found")

    @router.post("/v1/knowledge/indexing-jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Request cancellation for a queued or running indexing operation."""
        try:
            workspace_id_from_request(req)
            await _authorized_job(
                job_id,
                visible_to=visible_to,
                require_edit=True,
            )
            summary = await asyncio.to_thread(
                job_store.cancel,
                job_id,
                actor_user_id=principal.user_id,
            )
            if (
                summary.get("status") == "cancelled"
                and summary.get("operation_kind") == "collection_generation"
                and summary.get("generation_id")
            ):
                await knowledge_service.discard_generation(
                    collection_id=str(summary["collection_id"]),
                    generation_id=str(summary["generation_id"]),
                    actor_user_id=principal.user_id,
                )
            return summary
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except IndexingJobNotFound:
            return error_response(404, "Indexing job not found", "not_found")

    @router.post("/v1/knowledge/indexing-jobs/{job_id}/resume")
    async def resume_job(
        job_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Resume a dependency/validation-paused indexing job."""
        try:
            workspace_id_from_request(req)
            await _authorized_job(
                job_id,
                visible_to=visible_to,
                require_edit=True,
            )
            return await asyncio.to_thread(
                indexing_service.resume,
                job_id,
                principal=principal,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except IndexingJobNotFound:
            return error_response(404, "Indexing job not found", "not_found")
        except IndexingResumeUnavailable as exc:
            return error_response(409, str(exc), "resume_unavailable")
        except IndexingJobConflict as exc:
            return error_response(409, str(exc), "reindex_in_progress")

    @router.post("/v1/knowledge/indexing-jobs/{job_id}/resume-raw")
    async def resume_job_without_context(
        job_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Explicitly rebuild a paused shadow generation without context."""
        try:
            workspace_id_from_request(req)
            summary = await _authorized_job(
                job_id,
                visible_to=visible_to,
                require_edit=True,
            )
            if summary.get("status") not in {
                "paused_dependency",
                "paused_validation",
            }:
                return error_response(
                    409,
                    "Only paused indexing can be rebuilt without contextualization.",
                    "raw_rebuild_unavailable",
                )
            return await asyncio.to_thread(
                indexing_service.resume,
                job_id,
                principal=principal,
                raw_by_user_choice=True,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except IndexingJobNotFound:
            return error_response(404, "Indexing job not found", "not_found")
        except IndexingResumeUnavailable as exc:
            return error_response(409, str(exc), "resume_unavailable")
        except (IndexingJobConflict, KnowledgeError) as exc:
            return error_response(409, str(exc), "raw_rebuild_unavailable")

    @router.get("/v1/knowledge/indexing-jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Resume after ``Last-Event-ID`` and stream live job events."""
        raw_cursor = (req.headers.get("last-event-id") or "0").strip()
        try:
            after_sequence = int(raw_cursor)
        except ValueError:
            return error_response(
                400,
                "Invalid event cursor",
                "invalid_request_error",
            )
        if after_sequence < 0:
            return error_response(
                400,
                "Invalid event cursor",
                "invalid_request_error",
            )
        try:
            workspace_id_from_request(req)
            authorized_summary = await _authorized_job(
                job_id,
                visible_to=visible_to,
            )
            terminal_already_seen = (
                str(authorized_summary.get("status"))
                in {
                    "completed",
                    "failed",
                    "cancelled",
                    "paused_dependency",
                    "paused_validation",
                    "superseded",
                    "ready_raw_by_user_choice",
                    "expired",
                }
                and after_sequence
                >= int(authorized_summary.get("last_event_sequence") or 0)
            )
            subscription = await asyncio.to_thread(
                job_store.subscribe,
                job_id,
                after_sequence=after_sequence,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except IndexingJobNotFound:
            return error_response(404, "Indexing job not found", "not_found")

        async def _event_generator():
            async def _full_frame_check() -> bool:
                try:
                    current = await resolve_live_principal(principal_dep, req)
                    if (
                        current.user_id != principal.user_id
                        or current.kind != principal.kind
                        or current.tenant_id != principal.tenant_id
                        or current.session_id != principal.session_id
                        or current.pat_id != principal.pat_id
                        or current.scopes != principal.scopes
                    ):
                        return False
                    live_visible_to = (
                        await container.permission_service.resolve_user_context(
                            current
                        )
                    )
                    await _authorized_job(
                        job_id,
                        visible_to=live_visible_to,
                    )
                except (HTTPException, IndexingJobNotFound):
                    return False
                return True

            # One shared gate with the run-stream twin (the two copies had
            # already drifted: this stream lacked the keepalive below).
            _frame_gate = GenerationGatedFrameAuthorization(
                full_check=_full_frame_check,
                read_generation=(
                    (
                        lambda: container.permission_service
                        .authorization_generation(
                            tenant_id=principal.tenant_id,
                            user_id=principal.user_id,
                        )
                    )
                    if principal.user_id is not None
                    else None
                ),
            )

            async def _authorized_frame() -> bool:
                return await _frame_gate.allowed()

            try:
                if terminal_already_seen:
                    return
                terminal_replayed = False
                for event in subscription.replay:
                    if not await _authorized_frame():
                        return
                    yield format_sse_event(event)
                    terminal_replayed = event.get("type") in TERMINAL_INDEXING_EVENTS
                if terminal_replayed:
                    return
                loop = asyncio.get_running_loop()
                next_heartbeat = loop.time() + 5.0
                while True:
                    if await req.is_disconnected():
                        return
                    try:
                        # Reader lane, not the shared default pool: a
                        # busy AI call must never delay event delivery.
                        event = await loop.run_in_executor(
                            lanes.streams,
                            partial(subscription.queue.get, True, 0.5),
                        )
                    except Empty:
                        # Same keepalive contract as the run stream: a
                        # quiet indexing stream still rechecks
                        # authorization and feeds proxy read timeouts, or
                        # revocation latency is unbounded while idle.
                        if loop.time() >= next_heartbeat:
                            if not await _authorized_frame():
                                return
                            yield ": keepalive\n\n"
                            next_heartbeat = loop.time() + 5.0
                        continue
                    if not await _authorized_frame():
                        return
                    yield format_sse_event(event)
                    next_heartbeat = loop.time() + 5.0
                    if event.get("type") in TERMINAL_INDEXING_EVENTS:
                        return
            finally:
                subscription.close()

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
