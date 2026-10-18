"""Binary collaboration WebSocket adapter."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.asyncio.client import (
    ClientConnection,
    connect as websocket_connect,
)
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from .headers import _proxy_websocket_headers
from .settings import CollaborationProxySettings, _PublicOrigin

log = logging.getLogger("inqtrix.web_gateway")


class _Destination(TypedDict, total=False):
    host: str
    port: int
    server_hostname: str


def _websocket_upstream(
    websocket: WebSocket,
    backend_url: str,
    public_origin: _PublicOrigin | None,
) -> tuple[str, str, int, str | None]:
    """Separate the public handshake authority from the private destination."""
    backend = urlsplit(backend_url)
    if backend.scheme not in {"http", "https"} or backend.hostname is None:
        raise ValueError("backend_url must be an absolute HTTP(S) origin")
    scheme = "wss" if backend.scheme == "https" else "ws"
    authority = (
        public_origin.authority
        if public_origin is not None
        else (websocket.headers.get("host") or backend.netloc)
    )
    query = bytes(websocket.scope.get("query_string", b"")).decode("ascii")
    target = urlunsplit((scheme, authority, "/collaboration", query, ""))
    port = backend.port or (443 if backend.scheme == "https" else 80)
    server_hostname = backend.hostname if backend.scheme == "https" else None
    return target, backend.hostname, port, server_hostname


def _safe_websocket_close_code(code: int) -> int:
    """Return a close code that Starlette may send to a browser."""
    if code in {
        1000,
        1001,
        1002,
        1003,
        1007,
        1008,
        1009,
        1010,
        1011,
        1012,
        1013,
        1014,
    }:
        return code
    if 4000 <= code <= 4999:
        return code
    return 1011


def _truncate_websocket_reason(reason: str) -> str:
    """Fit a close reason into the RFC 6455 123-byte UTF-8 payload."""
    encoded = reason.encode("utf-8")
    if len(encoded) <= 123:
        return reason
    return encoded[:123].decode("utf-8", errors="ignore")


async def _close_websocket(
    websocket: WebSocket, code: int, reason: str = ""
) -> None:
    """Close a browser WebSocket on any ASGI connection state."""
    with suppress(RuntimeError, WebSocketDisconnect):
        if websocket.application_state is WebSocketState.CONNECTING:
            await websocket.accept()
        if websocket.application_state is WebSocketState.CONNECTED:
            await websocket.close(
                code=_safe_websocket_close_code(code),
                reason=_truncate_websocket_reason(reason),
            )


async def _relay_browser_websocket(
    browser: WebSocket,
    upstream: ClientConnection,
    max_frame_bytes: int,
) -> None:
    """Relay binary browser messages to FastAPI until either side closes."""
    while True:
        message = await browser.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            await upstream.close(
                code=_safe_websocket_close_code(
                    int(message.get("code") or 1000)
                )
            )
            return
        if message_type != "websocket.receive":
            continue
        payload = message.get("bytes")
        if payload is None:
            await upstream.close(code=4409, reason="binary_frames_required")
            await _close_websocket(browser, 4409, "binary_frames_required")
            return
        if len(payload) > max_frame_bytes:
            await upstream.close(code=1009, reason="message_too_big")
            await _close_websocket(browser, 1009, "message_too_big")
            return
        await upstream.send(payload)


async def _relay_upstream_websocket(
    upstream: ClientConnection,
    browser: WebSocket,
    max_frame_bytes: int,
) -> None:
    """Relay binary FastAPI messages to the browser and propagate closure.

    A browser that closes may have written frames just before its close that
    the server still holds. The browser relay forwards them to FastAPI and
    only then closes upstream, so a failed send here must not end this relay:
    that would cancel the browser relay with those frames unforwarded.
    """
    try:
        async for payload in upstream:
            if isinstance(payload, str):
                log.warning(
                    "Backend emitted an unexpected text WebSocket message."
                )
                await _close_websocket(browser, 1011, "invalid_upstream_frame")
                return
            if len(payload) > max_frame_bytes:
                await upstream.close(code=1009, reason="message_too_big")
                await _close_websocket(browser, 1009, "message_too_big")
                return
            try:
                await browser.send_bytes(bytes(payload))
            except WebSocketDisconnect:
                await _drain_upstream_after_browser_left(upstream)
                return
            except RuntimeError:
                if _websocket_is_disconnected(browser):
                    await _drain_upstream_after_browser_left(upstream)
                    return
                raise
    except ConnectionClosed as exc:
        await _close_websocket(browser, exc.code, exc.reason)
        return
    await _close_websocket(
        browser,
        upstream.close_code or 1000,
        upstream.close_reason or "",
    )


async def _drain_upstream_after_browser_left(upstream: ClientConnection) -> None:
    """Read and drop FastAPI's frames until the upstream connection ends.

    Reading on keeps the backend's outbound flow control from stalling behind
    an unread socket while the browser relay forwards the browser's last
    frames. The browser relay closes upstream after them, which ends this
    drain.
    """
    dropped = 1
    try:
        with suppress(ConnectionClosed):
            async for _payload in upstream:
                dropped += 1
    finally:
        log.debug(
            "Collaboration gateway dropped %d backend frames for a browser "
            "that had left.",
            dropped,
        )


def _websocket_is_disconnected(websocket: WebSocket) -> bool:
    """Return whether either ASGI side has already completed its close."""
    return (
        websocket.application_state is WebSocketState.DISCONNECTED
        or websocket.client_state is WebSocketState.DISCONNECTED
    )


def register_websocket_route(
    app: FastAPI,
    *,
    backend_url: str,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
    settings: CollaborationProxySettings,
) -> None:
    """Attach the collaboration route to an application."""

    @app.websocket("/collaboration")
    async def proxy_collaboration(websocket: WebSocket) -> None:
        try:
            target, tcp_host, tcp_port, server_hostname = _websocket_upstream(
                websocket, backend_url, public_origin
            )
            destination = _Destination(host=tcp_host, port=tcp_port)
            if server_hostname is not None:
                destination["server_hostname"] = server_hostname
            async with websocket_connect(
                target,
                origin=cast(Origin | None, websocket.headers.get("origin")),
                additional_headers=_proxy_websocket_headers(
                    websocket, public_origin, external_scheme
                ),
                user_agent_header=websocket.headers.get("user-agent"),
                compression=None,
                proxy=None,
                open_timeout=60,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
                max_size=settings.max_frame_bytes,
                max_queue=settings.max_queued_frames,
                **destination,
            ) as upstream:
                await websocket.accept()
                browser_to_backend = asyncio.create_task(
                    _relay_browser_websocket(
                        websocket,
                        upstream,
                        settings.max_frame_bytes,
                    )
                )
                backend_to_browser = asyncio.create_task(
                    _relay_upstream_websocket(
                        upstream,
                        websocket,
                        settings.max_frame_bytes,
                    )
                )
                done, pending = await asyncio.wait(
                    {browser_to_backend, backend_to_browser},
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
            await _close_websocket(websocket, exc.code, exc.reason)
        except Exception as exc:
            log.warning(
                "FastAPI collaboration WebSocket upstream is unavailable "
                "(%s).",
                type(exc).__name__,
            )
            await _close_websocket(
                websocket, 4503, "collaboration_gateway_unavailable"
            )
