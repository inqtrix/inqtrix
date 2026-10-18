"""Streaming HTTP reverse-proxy adapter."""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .headers import _proxy_request_headers, _relay_response_headers
from .logging import _redact_guest_route_tokens
from .settings import _PublicOrigin

log = logging.getLogger("inqtrix.web_gateway")

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_MAX_KEEPALIVE_CONNECTIONS = 20
_POOL_TIMEOUT_SECONDS = 10.0
_WRITE_TIMEOUT_SECONDS = 3600.0


class _RequestBodyTooLarge(Exception):
    """Stop an in-flight chunked upload after the configured byte ceiling."""


class _RejectAllCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Keep the pooled backend transport stateless for client authority."""

    def set_ok(
        self,
        cookie: http.cookiejar.Cookie,
        request: object,
    ) -> bool:
        """Never retain an upstream ``Set-Cookie`` response."""
        return False

    def return_ok(
        self,
        cookie: http.cookiejar.Cookie,
        request: object,
    ) -> bool:
        """Never synthesize a Cookie header from transport state."""
        return False


def _stateless_cookie_jar() -> http.cookiejar.CookieJar:
    """Build a reject-all jar while inbound Cookie headers pass explicitly."""
    return http.cookiejar.CookieJar(policy=_RejectAllCookiePolicy())


async def _bounded_request_stream(
    request: Request,
    max_request_bytes: int,
    body_done: asyncio.Event,
) -> AsyncIterator[bytes]:
    """Relay chunks while enforcing a byte count independent of headers.

    ``body_done`` is set only after the client body is fully relayed: the
    disconnect watcher must not call ``receive()`` before then, or it
    would steal body chunks from this stream.
    """
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_request_bytes:
            raise _RequestBodyTooLarge
        if chunk:
            yield chunk
    body_done.set()


async def _client_disconnected(
    request: Request, body_done: asyncio.Event
) -> None:
    """Park until the client connection is gone.

    Waits for the request body to be fully relayed first; after
    exhaustion the next ``receive()`` only ever yields
    ``http.disconnect`` (the same server mechanic the API's own
    disconnect watcher relies on).
    """
    await body_done.wait()
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _send_watching_disconnect(
    client: httpx.AsyncClient,
    upstream: httpx.Request,
    request: Request,
    body_done: asyncio.Event,
) -> httpx.Response | None:
    """Await the upstream response, or ``None`` if the client left first.

    While the proxy waits for upstream response headers nothing else
    observes the client socket, so a browser abort would leave the
    backend computing for the full request duration. Racing the send
    against a disconnect watcher closes that gap. A cancelled send tears
    down its upstream connection inside httpcore — a half-run HTTP/1.1
    exchange can never return to the keep-alive pool.
    """
    send_task = asyncio.ensure_future(client.send(upstream, stream=True))
    watch_task = asyncio.ensure_future(
        _client_disconnected(request, body_done)
    )
    try:
        done, _pending = await asyncio.wait(
            {send_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if send_task in done:
            return send_task.result()
        send_task.cancel()
        with suppress(asyncio.CancelledError, httpx.HTTPError):
            await send_task
        return None
    finally:
        watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await watch_task


def _backend_limits(max_connections: int) -> httpx.Limits:
    """Build an explicit, bounded per-process backend pool."""
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
    )


def _backend_timeout() -> httpx.Timeout:
    """Keep SSE reads unbounded while bounding connect, write, and pool wait."""
    return httpx.Timeout(
        connect=60.0,
        read=None,
        write=_WRITE_TIMEOUT_SECONDS,
        pool=_POOL_TIMEOUT_SECONDS,
    )


def build_backend_client(
    backend_url: str,
    *,
    max_connections: int,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    """Construct the one shared, cookie-stateless app-lifespan client."""
    return httpx.AsyncClient(
        base_url=backend_url,
        cookies=_stateless_cookie_jar(),
        timeout=_backend_timeout(),
        limits=_backend_limits(max_connections),
        transport=transport,
    )


async def _relay_upstream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Relay a streaming response and release its pool slot on every exit."""
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.TransportError as exc:
        log.warning(
            "Upstream stream aborted mid-response (%s).",
            type(exc).__name__,
        )
    finally:
        await response.aclose()


def register_http_routes(
    app: FastAPI,
    *,
    client: httpx.AsyncClient,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
    max_request_bytes: int,
    max_upstream_connections: int,
) -> None:
    """Attach all same-origin HTTP routes using one proxy implementation."""

    async def proxy(request: Request) -> Response:
        declared_length = request.headers.get("content-length", "")
        if (
            declared_length.isdecimal()
            and int(declared_length) > max_request_bytes
        ):
            return JSONResponse(
                {"detail": "Request body too large"},
                status_code=413,
            )

        raw_path = request.scope.get("raw_path") or request.url.path.encode(
            "utf-8"
        )
        target = bytes(raw_path).split(b"?", 1)[0]
        if request.url.query:
            target += b"?" + request.url.query.encode("ascii")
        has_body = (
            "content-length" in request.headers
            or "transfer-encoding" in request.headers
        )
        body_done = asyncio.Event()
        if not has_body:
            body_done.set()
        upstream = client.build_request(
            method=request.method,
            url=httpx.URL(raw_path=target),
            headers=_proxy_request_headers(
                request, public_origin, external_scheme
            ),
            content=(
                _bounded_request_stream(
                    request, max_request_bytes, body_done
                )
                if has_body
                else None
            ),
        )
        try:
            response = await _send_watching_disconnect(
                client, upstream, request, body_done
            )
        except _RequestBodyTooLarge:
            return JSONResponse(
                {"detail": "Request body too large"},
                status_code=413,
            )
        except httpx.PoolTimeout:
            log.warning(
                "Upstream connection pool exhausted for %s %s (limit %d per "
                "worker; raise INQTRIX_MAX_UPSTREAM_CONNECTIONS for "
                "sustained concurrent load).",
                request.method,
                _redact_guest_route_tokens(request.url.path),
                max_upstream_connections,
            )
            return JSONResponse(
                {"detail": "Upstream connection pool exhausted"},
                status_code=503,
            )
        except httpx.TransportError as exc:
            log.warning(
                "Configured backend is unreachable for %s %s (%s).",
                request.method,
                _redact_guest_route_tokens(request.url.path),
                type(exc).__name__,
            )
            return JSONResponse(
                {"detail": "Backend unreachable"},
                status_code=502,
            )
        if response is None:
            log.warning(
                "Client disconnected while awaiting the upstream response "
                "for %s %s — upstream request aborted.",
                request.method,
                _redact_guest_route_tokens(request.url.path),
            )
            return Response(status_code=499)
        relay = StreamingResponse(
            _relay_upstream(response),
            status_code=response.status_code,
        )
        relay.raw_headers = _relay_response_headers(response.headers)
        return relay

    @app.api_route("/health", methods=_PROXY_METHODS)
    async def proxy_health(request: Request) -> Response:
        return await proxy(request)

    @app.api_route("/readyz", methods=_PROXY_METHODS)
    async def proxy_readiness(request: Request) -> Response:
        return await proxy(request)

    @app.get("/collaboration/instance")
    async def proxy_collaboration_instance(request: Request) -> Response:
        return await proxy(request)

    @app.api_route("/v1/{full_path:path}", methods=_PROXY_METHODS)
    async def proxy_v1(request: Request) -> Response:
        return await proxy(request)

    @app.api_route("/api/{full_path:path}", methods=_PROXY_METHODS)
    async def proxy_api(request: Request) -> Response:
        return await proxy(request)
