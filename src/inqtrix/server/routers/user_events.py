"""User-scoped, content-free cache invalidation stream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from inqtrix.auth.principal import Principal, resolve_live_principal
from inqtrix.auth.principal_generation import PrincipalChangedError
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer
    from inqtrix.user_events import UserInvalidation


def _sse_frame(
    event: str,
    payload: dict[str, Any],
    *,
    event_id: int | None = None,
) -> str:
    """Serialize one named SSE frame without exposing domain content."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return "\n".join(lines) + "\n\n"


def _invalidation_frame(event: "UserInvalidation") -> str:
    payload: dict[str, Any] = {"scope": event.scope}
    if event.resource_type is not None:
        payload["resource_type"] = event.resource_type
    if event.resource_id is not None:
        payload["resource_id"] = event.resource_id
    return _sse_frame("invalidate", payload, event_id=event.id)


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the single per-user invalidation stream."""
    store = container.user_event_store
    if store is None:
        raise RuntimeError("user-events router requires a wired event store")
    router = APIRouter()
    principal_dep = container.principal_dependency

    @router.get("/v1/user/events")
    async def user_event_stream(
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Replay retained invalidations and tail new ones as SSE.

        A request without ``Last-Event-ID`` starts at the current cursor: the
        frontend performs a broad authoritative refetch on every connection,
        so replaying an arbitrary 24-hour history would add load without
        improving correctness. A reconnect carrying a cursor replays newer
        retained invalidations; an expired/foreign cursor receives ``reset``.
        """
        if principal.user_id is None:
            return error_response(404, "Not found", "not_found")
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor is None or not raw_cursor.strip():
            cursor = await store.current_cursor(tenant_id=principal.tenant_id)
        else:
            try:
                cursor = int(raw_cursor)
            except ValueError:
                return error_response(
                    400,
                    "Last-Event-ID must be a non-negative integer",
                    "invalid_request_error",
                )
            if cursor < 0:
                return error_response(
                    400,
                    "Last-Event-ID must be a non-negative integer",
                    "invalid_request_error",
                )

        expected_user_id = principal.user_id
        tenant_id = principal.tenant_id

        async def _still_authorized() -> bool:
            """Re-resolve credentials immediately before each data frame."""
            try:
                current = await resolve_live_principal(principal_dep, request)
            except (HTTPException, PrincipalChangedError):
                return False
            return (
                current.user_id == expected_user_id
                and current.tenant_id == tenant_id
            )

        async def _events():
            nonlocal cursor
            if not await _still_authorized():
                return
            yield _sse_frame(
                "ready",
                {"user_id": str(expected_user_id), "cursor": str(cursor)},
            )
            while True:
                if await request.is_disconnected():
                    return
                page = await store.page_after(
                    tenant_id=tenant_id,
                    target_user_id=expected_user_id,
                    cursor=cursor,
                )
                if page.reset_required:
                    if not await _still_authorized():
                        return
                    yield _sse_frame("reset", {})
                    cursor = page.current_cursor
                    continue
                for event in page.events:
                    if not await _still_authorized():
                        return
                    yield _invalidation_frame(event)
                    cursor = event.id
                await store.wait_for_change(
                    tenant_id=tenant_id,
                    target_user_id=expected_user_id,
                    cursor=cursor,
                    timeout=5.0,
                )
                if not page.events:
                    if not await _still_authorized():
                        return
                    yield ": keepalive\n\n"

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
