"""Chat-history persistence endpoints (project tier).

Thin per-surface router: parse the request, delegate to
:class:`~inqtrix.services.chat_history_service.ChatHistoryService`,
serialize. Distinct from ``chat.py`` (the OpenAI-compatible completions
endpoint) — these routes persist and read the conversation record, they
never call a model.

Registered whenever the chat-history service is wired (always — the
memory tier is the default). The frontend only switches its project to
this tier when ``/v1/capabilities`` reports ``project_persistence`` (a
durable, Postgres-backed store); the volatile tier stays unused.

Scope: a thread is private to its owner within a workspace (the decided
one-project-per-(user, workspace) model). No sharing surface, so
the routes resolve only the principal and the optional user context —
a non-owner scoped caller gets the indistinct 404, never another user's
conversations.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    list_envelope,
)
from inqtrix.project.chat_ports import (
    ChatMessage,
    ChatThread,
    ChatThreadGroup,
    ThreadGroupNotFound,
    ThreadNotFound,
)
from inqtrix.services.chat_history_service import ChatValidationError
from inqtrix.project.session_titles import SessionTitleConflict, parse_title_intent
from inqtrix.services.request_parsing import (
    error_response,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def _caller_user_id(principal: Principal) -> uuid.UUID | None:
    """Owner anchor: scoped principals own what they create; the
    anonymous/static principals stay unscoped (the established rule)."""
    return principal.user_id if principal.kind in ("oidc_session", "pat") else None


def _thread_payload(thread: ChatThread) -> dict[str, Any]:
    return {
        "id": thread.id,
        "title": thread.title,
        "title_revision": thread.title_revision,
        "title_source": thread.title_source,
        "preview": thread.preview,
        "source": thread.source,
        "group_id": thread.group_id,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "model_selection": thread.model_selection,
    }


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "role": message.role,
        "content_markdown": message.content_markdown,
        "metadata": dict(message.metadata),
        "created_at": message.created_at,
    }


def _group_payload(group: ChatThreadGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "title": group.title,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the chat-history routes against the container.

    Raises:
        RuntimeError: When called without a wired chat-history service —
            registration is a composition decision, not a runtime
            fallback.
    """
    service = container.chat_history_service
    if service is None:
        raise RuntimeError(
            "build_router(chat_history) requires a wired chat-history "
            "service."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency

    # -- threads ---------------------------------------------------------- #

    @router.get("/v1/chat/threads")
    async def list_threads(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """One keyset page of the caller's threads (newest first)."""
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        threads, next_cursor = await service.list_threads(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
            limit=limit,
            after=after,
        )
        return list_envelope(
            [_thread_payload(thread) for thread in threads], next_cursor
        )

    @router.put("/v1/chat/threads/{thread_id}")
    async def save_thread(
        thread_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create or idempotently update a thread (autosave upsert)."""
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
                400,
                "created_at and updated_at must be numbers",
                "invalid_request_error",
            )
        group_id = body.get("group_id")
        if group_id is not None and not isinstance(group_id, str):
            return error_response(
                400, "group_id must be a string or null",
                "invalid_request_error",
            )
        # Absent and null both mean "nothing picked" — str(None) would store
        # the literal 'None' (the account-preferences lesson).
        model_selection = body.get("model_selection")
        if model_selection is None:
            model_selection = ""
        if not isinstance(model_selection, str):
            return error_response(
                400, "model_selection must be a string",
                "invalid_request_error",
            )
        try:
            title_revision, title_source = parse_title_intent(body)
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        try:
            thread = await service.save_thread(
                id=thread_id,
                title=str(body.get("title", "")),
                preview=str(body.get("preview", "")),
                source=str(body.get("source", "api")),
                group_id=group_id,
                created_at=float(created_at),
                updated_at=float(updated_at),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                visible_to=visible_to,
                model_selection=model_selection,
                title_revision=title_revision, title_source=title_source,
            )
        except ChatValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except SessionTitleConflict as exc:
            return error_response(409, str(exc), "session_title_conflict")
        except ThreadNotFound:
            return error_response(404, "Thread not found", "not_found")
        payload = _thread_payload(thread)
        payload["title_preserved"] = thread.title != str(body.get("title", "")) or (title_revision is not None and thread.title_source != title_source)
        return payload

    @router.delete("/v1/chat/threads/{thread_id}", status_code=204)
    async def delete_thread(
        thread_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete a thread and its messages (owner-only)."""
        try:
            await service.delete_thread(
                thread_id,
                visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except ThreadNotFound:
            return error_response(404, "Thread not found", "not_found")

    # -- messages --------------------------------------------------------- #

    @router.get("/v1/chat/threads/{thread_id}/messages")
    async def list_messages(
        thread_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """One keyset page of a thread's messages (newest first)."""
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        try:
            messages, next_cursor = await service.list_messages(
                thread_id, limit=limit, after=after, visible_to=visible_to
            )
        except ThreadNotFound:
            return error_response(404, "Thread not found", "not_found")
        return list_envelope(
            [_message_payload(message) for message in messages], next_cursor
        )

    @router.post("/v1/chat/threads/{thread_id}/messages", status_code=201)
    async def append_messages(
        thread_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Append/upsert messages into a thread the caller may edit."""
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list):
            return error_response(
                400, "Field 'messages' must be a list",
                "invalid_request_error",
            )
        try:
            stored = await service.append_messages(
                thread_id, messages=raw_messages, visible_to=visible_to
            )
        except ThreadNotFound:
            return error_response(404, "Thread not found", "not_found")
        except ChatValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {
            "object": "list",
            "data": [_message_payload(message) for message in stored],
        }

    @router.delete(
        "/v1/chat/threads/{thread_id}/messages/{message_id}", status_code=204
    )
    async def delete_message(
        thread_id: str,
        message_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete one message from a thread the caller may edit."""
        try:
            await service.delete_message(
                thread_id,
                message_id,
                visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except ThreadNotFound:
            return error_response(404, "Thread not found", "not_found")

    # -- groups ----------------------------------------------------------- #

    @router.get("/v1/chat/thread-groups")
    async def list_groups(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """All of the caller's thread groups (newest first)."""
        groups = await service.list_groups(
            caller_user_id=_caller_user_id(principal),
            workspace_id=workspace_id_from_request(req),
        )
        return {
            "object": "list",
            "data": [_group_payload(group) for group in groups],
        }

    @router.put("/v1/chat/thread-groups/{group_id}")
    async def save_group(
        group_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create or idempotently update a thread group."""
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
                400,
                "created_at and updated_at must be numbers",
                "invalid_request_error",
            )
        try:
            group = await service.save_group(
                id=group_id,
                title=str(body.get("title", "")),
                created_at=float(created_at),
                updated_at=float(updated_at),
                caller_user_id=_caller_user_id(principal),
                workspace_id=workspace_id_from_request(req),
                visible_to=visible_to,
            )
        except ThreadGroupNotFound:
            return error_response(404, "Group not found", "not_found")
        return _group_payload(group)

    @router.delete("/v1/chat/thread-groups/{group_id}", status_code=204)
    async def delete_group(
        group_id: str,
        req: Request,
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete a group; its threads orphan to ungrouped."""
        try:
            await service.delete_group(
                group_id,
                visible_to=visible_to,
                request_workspace_id=workspace_id_from_request(req),
            )
        except ThreadGroupNotFound:
            return error_response(404, "Group not found", "not_found")

    return router


async def _json_object(req: Request) -> Any:
    """Parse the request body as JSON, or ``None`` on a malformed body."""
    try:
        return await req.json()
    except Exception:
        return None
