"""User-scoped, content-free cache invalidation stream."""

from __future__ import annotations

import json
import random
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from inqtrix.auth.principal import Principal, resolve_live_principal
from inqtrix.auth.principal_generation import PrincipalChangedError
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer
    from inqtrix.user_events import UserInvalidation

_KEEPALIVE_SECONDS = 5.0
"""How often a quiet stream proves itself alive to the browser and to proxies."""

_CHECK_WINDOW_SECONDS = (45.0, 60.0)
"""When a quiet stream checks its credential and reads its page once. The upper
bound is how long a signed-out or expired session can keep a stream open; only
comments flow in that time. A window, so that tabs reconnecting together after a
restart do not stay in step."""


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
            """Re-resolve credentials before each data frame and each quiet check."""
            try:
                current = await resolve_live_principal(principal_dep, request)
            except (HTTPException, PrincipalChangedError):
                return False
            return (
                current.user_id == expected_user_id
                and current.tenant_id == tenant_id
            )

        def _next_check() -> float:
            return time.monotonic() + random.uniform(*_CHECK_WINDOW_SECONDS)

        async def _events():
            nonlocal cursor
            if not await _still_authorized():
                return
            check_at = _next_check()
            yield _sse_frame(
                "ready",
                {"user_id": str(expected_user_id), "cursor": str(cursor)},
            )
            read_page = True
            while True:
                if await request.is_disconnected():
                    return
                if read_page:
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
                    for event in page.events:
                        if not await _still_authorized():
                            return
                        yield _invalidation_frame(event)
                        cursor = event.id
                read_page = await store.wait_for_change(
                    tenant_id=tenant_id,
                    target_user_id=expected_user_id,
                    cursor=cursor,
                    timeout=min(_KEEPALIVE_SECONDS, check_at - time.monotonic()),
                )
                if read_page:
                    continue
                if time.monotonic() >= check_at:
                    if not await _still_authorized():
                        return
                    check_at = _next_check()
                    # Only a read finds a cursor that no longer fits: the wake-up
                    # compares ids and stays silent when they became smaller.
                    read_page = True
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
