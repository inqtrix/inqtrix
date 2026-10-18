"""Same-origin binary WebSocket relay for the private collaboration service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer
    from inqtrix.settings import CollaborationSettings

log = logging.getLogger("inqtrix")
_INSTANCE_PROBE_CONTRACT = "inqtrix-collaboration-instance-v1"
_INSTANCE_PROBE_SERVICE = "inqtrix-collaboration"


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the only browser-visible collaboration transport endpoint."""
    if container.editor_collaboration_service is None:
        raise RuntimeError("Collaboration gateway requires a wired service")
    router = APIRouter()
    settings = container.settings.collaboration
    public_base_url = container.settings.server.public_base_url.strip()
    public_origin = _normalize_origin(public_base_url, allow_path=True)
    if public_base_url and public_origin is None:
        log.warning(
            "INQTRIX_PUBLIC_BASE_URL cannot authorize the collaboration "
            "gateway because it is not a valid HTTP(S) URL."
        )

    @router.get("/collaboration/instance")
    async def collaboration_instance() -> JSONResponse:
        """Expose the stable DB-fenced identity of the ready data plane."""
        try:
            instance = await container.editor_collaboration_service.ready_instance()
        except Exception:
            log.warning(
                "Collaboration instance probe could not read authoritative state.",
                exc_info=True,
            )
            instance = None
        ready = instance is not None
        return JSONResponse(
            {
                "contract": _INSTANCE_PROBE_CONTRACT,
                "service": _INSTANCE_PROBE_SERVICE,
                "status": "ready" if ready else "not_ready",
                "instance_id": instance.instance_id if instance else None,
                "epoch": instance.epoch if instance else None,
            },
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    @router.websocket("/collaboration")
    async def collaboration_gateway(websocket: WebSocket) -> None:
        if not _origin_allowed(
            websocket,
            settings,
            public_origin=public_origin,
        ):
            # Name both sides. An origin is an address, not an identity, so
            # logging it does not weaken pseudonymization — and without it the
            # operator cannot tell a misconfigured base URL from a real
            # cross-site attempt.
            log.warning(
                "Collaboration WebSocket rejected an invalid Origin: "
                "presented=%s expected one of=%s",
                websocket.headers.get("origin", "") or "(none)",
                ", ".join(
                    sorted(
                        {
                            *settings.allowed_origins,
                            *([public_origin] if public_origin else []),
                        }
                    )
                ) or "(none configured)",
            )
            await websocket.accept()
            await websocket.close(code=4403, reason="origin_rejected")
            return

        await websocket.accept()
        try:
            async with connect(
                settings.ws_url,
                additional_headers={
                    "Authorization": f"Bearer {settings.secret}",
                },
                max_size=settings.max_frame_bytes,
                max_queue=settings.max_queued_frames,
                open_timeout=5,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
            ) as upstream:
                browser_to_node = asyncio.create_task(
                    _relay_browser_to_node(websocket, upstream, settings)
                )
                node_to_browser = asyncio.create_task(
                    _relay_node_to_browser(upstream, websocket, settings)
                )
                done, pending = await asyncio.wait(
                    {browser_to_node, node_to_browser},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            return
        except ConnectionClosed as exc:
            await _close_browser(websocket, exc.code, exc.reason)
        except Exception:
            log.warning(
                "Collaboration WebSocket upstream is unavailable.",
                exc_info=True,
            )
            await _close_browser(
                websocket, 4503, "collaboration_service_unavailable"
            )

    return router


async def _relay_browser_to_node(
    browser: WebSocket,
    upstream: ClientConnection,
    settings: "CollaborationSettings",
) -> None:
    while True:
        message = await browser.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            code = int(message.get("code") or 1000)
            await upstream.close(code=_safe_close_code(code))
            return
        if message_type != "websocket.receive":
            continue
        payload = message.get("bytes")
        if payload is None:
            await upstream.close(code=4409, reason="binary_frames_required")
            await _close_browser(browser, 4409, "binary_frames_required")
            return
        if len(payload) > settings.max_frame_bytes:
            await upstream.close(code=1009, reason="message_too_big")
            await _close_browser(browser, 1009, "message_too_big")
            return
        await upstream.send(payload)


async def _relay_node_to_browser(
    upstream: ClientConnection,
    browser: WebSocket,
    settings: "CollaborationSettings",
) -> None:
    """Relay Node frames to the browser; drain them once the browser left.

    A browser that closes may have written frames just before its close that
    the server still holds. The browser relay forwards them to Node and only
    then closes upstream, so a failed send here must not end this relay: that
    would cancel the browser relay with those frames unforwarded.
    """
    try:
        async for payload in upstream:
            if isinstance(payload, str):
                log.warning(
                    "Collaboration service emitted an unexpected text frame."
                )
                await _close_browser(browser, 1011, "invalid_upstream_frame")
                return
            if len(payload) > settings.max_frame_bytes:
                await _close_browser(browser, 1009, "message_too_big")
                return
            try:
                await browser.send_bytes(bytes(payload))
            except WebSocketDisconnect:
                await _drain_upstream_after_browser_left(upstream)
                return
            except RuntimeError as exc:
                if _browser_send_after_close(exc):
                    await _drain_upstream_after_browser_left(upstream)
                    return
                raise
    except ConnectionClosed as exc:
        await _close_browser(browser, exc.code, exc.reason)


async def _drain_upstream_after_browser_left(upstream: ClientConnection) -> None:
    """Read and drop Node's frames until the upstream connection ends.

    Reading on keeps Node's outbound flow control from stalling behind an
    unread socket while the browser relay forwards the browser's last frames.
    The browser relay closes upstream after them, which ends this drain.
    """
    dropped = 1
    try:
        with suppress(ConnectionClosed):
            async for _payload in upstream:
                dropped += 1
    finally:
        log.debug(
            "Collaboration gateway dropped %d service frames for a browser "
            "that had left.",
            dropped,
        )


async def _close_browser(
    websocket: WebSocket, code: int | None, reason: str | None
) -> None:
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(
            code=_safe_close_code(code or 1011),
            reason=(reason or "")[:123],
        )


def _safe_close_code(code: int) -> int:
    if code in {1000, 1001, 1002, 1003, 1008, 1009, 1011, 1012}:
        return code
    if 4000 <= code <= 4999:
        return code
    return 1011


def _browser_send_after_close(error: RuntimeError) -> bool:
    """Recognize the ASGI race produced by an ordinary browser disconnect."""
    message = str(error)
    return (
        "websocket.send" in message
        and (
            "websocket.close" in message
            or "response already completed" in message
        )
    )


def _origin_allowed(
    websocket: WebSocket,
    settings: "CollaborationSettings",
    *,
    public_origin: str | None,
) -> bool:
    normalized = _normalize_origin(
        websocket.headers.get("origin", ""),
        allow_path=False,
    )
    if normalized is None:
        return False
    configured_origins = {
        configured
        for value in settings.allowed_origins
        if (configured := _normalize_origin(value, allow_path=False))
        is not None
    }
    if normalized in configured_origins:
        return True

    websocket_scheme = websocket.url.scheme.lower()
    expected_scheme = "https" if websocket_scheme == "wss" else "http"
    direct_origin = _normalize_origin(
        f"{expected_scheme}://{websocket.headers.get('host', '')}",
        allow_path=False,
    )
    if normalized == direct_origin:
        return True

    return (
        public_origin is not None
        and normalized == public_origin
        and _forwarded_origin(websocket) == public_origin
    )


def _forwarded_origin(websocket: WebSocket) -> str | None:
    """Return the nginx-sanitized external origin, rejecting header chains."""
    scheme = websocket.headers.get("x-forwarded-proto", "").strip().lower()
    host = websocket.headers.get("x-forwarded-host", "").strip()
    if scheme not in {"http", "https"} or not host:
        return None
    if "," in scheme or "," in host:
        return None
    return _normalize_origin(f"{scheme}://{host}", allow_path=False)


def _normalize_origin(value: str, *, allow_path: bool) -> str | None:
    """Canonicalize an HTTP(S) origin, including default-port equivalence."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and parsed.path not in {"", "/"})
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = 443 if scheme == "https" else 80
    authority = (
        normalized_host
        if port is None or port == default_port
        else f"{normalized_host}:{port}"
    )
    return f"{scheme}://{authority}"
