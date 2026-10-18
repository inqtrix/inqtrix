"""Single header policy shared by the HTTP and WebSocket adapters."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from fastapi import Request, WebSocket

from .settings import _PublicOrigin

_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})

_WEBSOCKET_FORWARD_HEADERS = (
    "authorization",
    "cookie",
    "x-csrf-token",
    "x-inqtrix-guest-csrf",
    "x-inqtrix-workspace-id",
)


def _forwarded_scheme(
    connection_scheme: str,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
) -> str:
    """Resolve trusted forwarding metadata without trusting client headers."""
    if public_origin is not None:
        return public_origin.scheme
    if external_scheme is not None:
        return external_scheme
    return connection_scheme


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Remove transport-hop fields from an upstream request."""
    connection_value = ",".join(
        value
        for name, value in headers.items()
        if name.lower() == "connection"
    )
    connection_tokens = {
        token.strip().lower()
        for token in connection_value.split(",")
        if token.strip()
    }
    blocked = _HOP_BY_HOP | connection_tokens
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def _relay_response_headers(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    """Keep duplicate response fields such as multiple Set-Cookie headers."""
    connection_tokens = {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
        if token.strip()
    }
    blocked = _HOP_BY_HOP | connection_tokens
    return [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in headers.multi_items()
        if name.lower() not in blocked
    ]


def _proxy_request_headers(
    request: Request,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
) -> dict[str, str]:
    """Build the trusted HTTP forwarding boundary."""
    headers = _filter_headers(request.headers)
    headers.pop("x-forwarded-proto", None)
    headers.pop("x-forwarded-host", None)
    host = (
        public_origin.authority
        if public_origin is not None
        else request.headers.get("host")
    )
    if host:
        headers["host"] = host
        headers["x-forwarded-host"] = host
    client_host = request.client.host if request.client else None
    if client_host:
        prior = request.headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = (
            f"{prior}, {client_host}" if prior else client_host
        )
        headers["x-real-ip"] = client_host
    headers["x-forwarded-proto"] = _forwarded_scheme(
        request.url.scheme, public_origin, external_scheme
    )
    return headers


def _proxy_websocket_headers(
    websocket: WebSocket,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
) -> dict[str, str]:
    """Build application and forwarding headers for a backend handshake."""
    headers = {
        name: value
        for name in _WEBSOCKET_FORWARD_HEADERS
        if (value := websocket.headers.get(name)) is not None
    }
    client_host = websocket.client.host if websocket.client else None
    if client_host:
        prior = websocket.headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = (
            f"{prior}, {client_host}" if prior else client_host
        )
        headers["x-real-ip"] = client_host
    authority = (
        public_origin.authority
        if public_origin is not None
        else websocket.headers.get("host")
    )
    if not authority:
        raise ValueError("WebSocket Host is required for trusted forwarding")
    headers["x-forwarded-host"] = authority
    headers["x-forwarded-proto"] = _forwarded_scheme(
        "https" if websocket.url.scheme == "wss" else "http",
        public_origin,
        external_scheme,
    )
    return headers
