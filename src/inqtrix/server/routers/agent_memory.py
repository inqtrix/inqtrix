"""Agent memory endpoints.

Every route resolves the verified request principal through the container
dependency. Owner fields in query/body are rejected; personal memory scope
is always derived server-side from the principal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from inqtrix.agents.memory_ports import (
    AgentMemoryCandidate,
    AgentFeedbackRecord,
    AgentMemoryNotFound,
    AgentMemoryRecord,
    AgentMemoryUnavailable,
    AgentMemoryValidationError,
)
from inqtrix.auth.principal import Principal
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_OWNER_FIELDS = frozenset(
    {"user_id", "tenant_id", "user_id", "owner", "owner_user_id", "namespace"}
)


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the memory routes to the container service."""
    service = container.agent_memory_service
    if service is None:
        raise RuntimeError("agent_memory router requires agent_memory_service")
    router = APIRouter()
    principal_dep = container.principal_dependency

    @router.get("/v1/agent/memory")
    async def list_memory(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        owner_error = _reject_query_owner_fields(req)
        if owner_error is not None:
            return owner_error
        try:
            memories = await service.list_memories(
                principal=principal,
                scope=req.query_params.get("scope"),
                query=req.query_params.get("q", ""),
                limit=_int_query(req, "limit", 100),
            )
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {
            "object": "list",
            "status": service.status(principal),
            "data": [_memory_payload(row) for row in memories],
        }

    @router.get("/v1/agent/memory/feedback")
    async def list_memory_feedback(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        owner_error = _reject_query_owner_fields(req)
        if owner_error is not None:
            return owner_error
        try:
            rows = await service.list_feedback(
                principal=principal,
                run_id=req.query_params.get("run_id"),
                limit=_int_query(req, "limit", 100),
            )
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        return {
            "object": "list",
            "data": [_feedback_payload(row) for row in rows],
        }

    @router.patch("/v1/agent/memory/{memory_id}")
    async def update_memory(
        memory_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        owner_error = _reject_body_owner_fields(body)
        if owner_error is not None:
            return owner_error
        try:
            row = await service.update_memory(
                principal=principal,
                memory_id=memory_id,
                content=str(body.get("content") or body.get("text") or ""),
                scope=str(body.get("scope") or "user"),
                category=str(body.get("category") or "project_fact"),
            )
        except AgentMemoryNotFound:
            return error_response(404, "Memory not found", "not_found")
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _memory_payload(row)

    @router.delete("/v1/agent/memory/{memory_id}")
    async def delete_memory(
        memory_id: str,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            await service.delete_memory(principal=principal, memory_id=memory_id)
        except AgentMemoryNotFound:
            return error_response(404, "Memory not found", "not_found")
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        return {"deleted": True}

    @router.post("/v1/agent/memory:clear")
    async def clear_memory(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(req, allow_empty=True)
        if body is None:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        owner_error = _reject_body_owner_fields(body)
        if owner_error is not None:
            return owner_error
        try:
            deleted = await service.clear_memories(
                principal=principal, scope=body.get("scope")
            )
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {"deleted": deleted}

    @router.get("/v1/agent/memory/candidates")
    async def list_candidates(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        owner_error = _reject_query_owner_fields(req)
        if owner_error is not None:
            return owner_error
        try:
            rows = await service.list_candidates(
                principal=principal, status=req.query_params.get("status")
            )
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {
            "object": "list",
            "status": service.status(principal),
            "data": [_candidate_payload(row) for row in rows],
        }

    @router.post("/v1/agent/memory/candidates/{candidate_id}:accept")
    async def accept_candidate(
        candidate_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(req, allow_empty=True)
        if body is None:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        owner_error = _reject_body_owner_fields(body)
        if owner_error is not None:
            return owner_error
        try:
            row = await service.accept_candidate(
                principal=principal,
                candidate_id=candidate_id,
                content=body.get("content") if "content" in body else None,
            )
        except AgentMemoryNotFound:
            return error_response(404, "Memory not found", "not_found")
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _candidate_payload(row)

    @router.post("/v1/agent/memory/candidates/{candidate_id}:reject")
    async def reject_candidate(
        candidate_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(req, allow_empty=True)
        if body is None:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        owner_error = _reject_body_owner_fields(body)
        if owner_error is not None:
            return owner_error
        try:
            row = await service.reject_candidate(
                principal=principal, candidate_id=candidate_id
            )
        except AgentMemoryNotFound:
            return error_response(404, "Memory not found", "not_found")
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _candidate_payload(row)

    @router.post("/v1/agent/runs/{run_id}/feedback")
    async def submit_memory_feedback(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        owner_error = _reject_body_owner_fields(body)
        if owner_error is not None:
            return owner_error
        try:
            row = await service.feedback(
                principal=principal,
                run_id=run_id,
                memory_id=str(body.get("memory_id") or ""),
                feedback=str(body.get("feedback") or ""),
                reason=str(body.get("reason") or f"run:{run_id}"),
            )
        except AgentMemoryNotFound:
            return error_response(404, "Memory not found", "not_found")
        except AgentMemoryUnavailable:
            return error_response(404, "Memory unavailable", "not_found")
        except AgentMemoryValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _feedback_payload(row)

    return router


async def _json_object(
    req: Request, *, allow_empty: bool = False
) -> dict[str, Any] | None:
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        return {} if allow_empty else None
    return body if isinstance(body, dict) else None


def _reject_query_owner_fields(req: Request):
    if any(field in req.query_params for field in _OWNER_FIELDS):
        return error_response(
            400,
            "Owner fields are derived from the authenticated principal.",
            "invalid_request_error",
        )
    return None


def _reject_body_owner_fields(body: dict[str, Any]):
    if any(field in body for field in _OWNER_FIELDS):
        return error_response(
            400,
            "Owner fields are derived from the authenticated principal.",
            "invalid_request_error",
        )
    return None


def _int_query(req: Request, name: str, default: int) -> int:
    raw = req.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _memory_payload(row: AgentMemoryRecord) -> dict[str, Any]:
    return {
        "id": row.memory_id,
        "scope": row.scope,
        "category": row.category,
        "content": row.content,
        "confidence": row.confidence,
        "source_run_id": row.source_run_id,
        "metadata": row.metadata,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _candidate_payload(row: AgentMemoryCandidate) -> dict[str, Any]:
    return {
        "id": row.candidate_id,
        "scope": row.scope,
        "category": row.category,
        "content": row.content,
        "reason": row.reason,
        "confidence": row.confidence,
        "source_run_id": row.source_run_id,
        "status": row.status,
        "memory_id": row.memory_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _feedback_payload(row: AgentFeedbackRecord) -> dict[str, Any]:
    return {
        "id": row.feedback_id,
        "run_id": row.run_id,
        "memory_id": row.memory_id,
        "feedback": row.feedback,
        "reason": row.reason,
        "created_at": row.created_at,
    }
