"""Agent-session endpoints (Agent-Desk saved sessions).

Thin per-surface router mirroring chat_history / asset_records: parse, delegate
to
:class:`~inqtrix.services.agent_sessions_service.AgentSessionsService`,
serialize. Sessions are private per owner within a workspace. The LIST and
the single-session GET both carry the small ``items_json`` settings body.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.agent_sessions_ports import (
    AgentSession,
    AgentSessionGroup,
    AgentSessionGroupNotFound,
    AgentSessionNotFound,
)
from inqtrix.runs.deletion_operations import DeletionOperationConflict
from inqtrix.project.session_titles import SessionTitleConflict, parse_title_intent
from inqtrix.server.routers.agent_runs import artifact_meta_payload
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def _caller_user_id(principal: Principal) -> uuid.UUID | None:
    return principal.user_id if principal.kind in ("oidc_session", "pat") else None


def _meta_payload(s: AgentSession) -> dict[str, Any]:
    return {
        "id": s.id, "title": s.title,
        "title_revision": s.title_revision, "title_source": s.title_source,
        "group_id": s.group_id,
        "created_at": s.created_at, "updated_at": s.updated_at,
        "lifecycle_status": s.lifecycle_status,
        "deletion_operation_id": s.deletion_operation_id,
        "deletion_stage": s.deletion_stage,
        "deletion_error": s.deletion_error,
    }


def _full_payload(s: AgentSession) -> dict[str, Any]:
    return {**_meta_payload(s), "items_json": s.items_json}


def _group_payload(group: AgentSessionGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "title": group.title,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _require_numbers(body: dict[str, Any]) -> bool:
    return isinstance(body.get("created_at"), (int, float)) and isinstance(
        body.get("updated_at"), (int, float)
    )


async def _json_object(req: Request) -> Any:
    try:
        return await req.json()
    except Exception:
        return None


def build_router(container: "AppContainer") -> APIRouter:
    service = container.agent_sessions_service
    deletion_service = container.asset_deletion_service
    if service is None:
        raise RuntimeError(
            "build_router(agent_sessions) requires a wired service."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency

    @router.get("/v1/agent-sessions")
    async def list_sessions(req: Request, principal: Principal = Depends(principal_dep)):
        sessions = await service.list_sessions(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
        )
        return {"object": "list", "data": [_full_payload(s) for s in sessions]}

    @router.get("/v1/agent-session-groups")
    async def list_groups(req: Request, principal: Principal = Depends(principal_dep)):
        groups = await service.list_groups(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
        )
        return {"object": "list", "data": [_group_payload(group) for group in groups]}

    @router.put("/v1/agent-session-groups/{group_id}")
    async def save_group(
        group_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        try:
            group = await service.save_group(
                id=group_id, title=str(body.get("title", "")),
                created_at=float(body["created_at"]),
                updated_at=float(body["updated_at"]),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                visible_to=visible_to,
            )
        except AgentSessionGroupNotFound:
            return error_response(404, "Folder not found", "not_found")
        return _group_payload(group)

    @router.delete("/v1/agent-session-groups/{group_id}", status_code=204)
    async def delete_group(
        group_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            await service.delete_group(
                group_id, visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except AgentSessionGroupNotFound:
            return error_response(404, "Folder not found", "not_found")

    control_service = container.agent_control_service

    @router.get("/v1/agent-sessions/{session_id}/artifacts")
    async def list_session_artifacts(
        session_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Session-scoped artifact metadata, anchor-independent.

        A session artifact's ``run_id`` re-anchors to the newest updating
        run, so run-scoped listings silently lose it from older runs —
        this listing keys on the SESSION and therefore never does. Rows
        are meta-only (bodies stay on the run detail route) and carry the
        CURRENT ``run_id`` anchor for the client's follow-up reads.
        Owner-private like every session route (indistinct 404).
        """
        if control_service is None:
            return error_response(404, "Session not found", "not_found")
        try:
            await service.get_session(session_id, visible_to=visible_to)
        except AgentSessionNotFound:
            return error_response(404, "Session not found", "not_found")
        artifacts = await control_service.list_session_artifacts(session_id)
        return {
            "object": "list",
            "data": [artifact_meta_payload(row) for row in artifacts],
        }

    @router.get("/v1/agent-sessions/{session_id}")
    async def get_session(
        session_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            session = await service.get_session(session_id, visible_to=visible_to)
        except AgentSessionNotFound:
            return error_response(404, "Session not found", "not_found")
        return _full_payload(session)

    @router.put("/v1/agent-sessions/{session_id}")
    async def save_session(
        session_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if not isinstance(body, dict) or not _require_numbers(body):
            return error_response(400, "Invalid body", "invalid_request_error")
        items_json = body.get("items_json", "[]")
        if not isinstance(items_json, str):
            return error_response(400, "items_json must be a string", "invalid_request_error")
        group_id = body.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            return error_response(400, "group_id must be a string or null", "invalid_request_error")
        try:
            title_revision, title_source = parse_title_intent(body)
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        try:
            session = await service.save_session(
                id=session_id, title=str(body.get("title", "")), items_json=items_json,
                group_id=group_id,
                created_at=float(body["created_at"]), updated_at=float(body["updated_at"]),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req), visible_to=visible_to,
                title_revision=title_revision, title_source=title_source,
            )
        except SessionTitleConflict as exc:
            return error_response(409, str(exc), "session_title_conflict")
        except AgentSessionGroupNotFound:
            return error_response(404, "Folder not found", "not_found")
        except AgentSessionNotFound:
            return error_response(404, "Session not found", "not_found")
        payload = _full_payload(session)
        payload["title_preserved"] = session.title != str(body.get("title", "")) or (title_revision is not None and session.title_source != title_source)
        return payload

    @router.delete("/v1/agent-sessions/{session_id}")
    async def delete_session(
        session_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            if service.durable:
                summary = await deletion_service.start_agent_session(
                    session_id,
                    principal=principal,
                    visible_to=visible_to,
                    workspace_id=workspace_id_from_request(req),
                )
                return JSONResponse(status_code=202, content=summary)
            await service.delete_session(
                session_id, visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except AgentSessionNotFound:
            return error_response(404, "Session not found", "not_found")
        except DeletionOperationConflict:
            return error_response(
                409, "Session is already being deleted", "deletion_in_progress"
            )
        return Response(status_code=204)

    return router
