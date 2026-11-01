"""Single header policy shared by the HTTP and WebSocket adapters."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
from fastapi import Request, WebSocket

from .settings import IdentityEdgePolicy, _PublicOrigin

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


def _filter_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove transport-hop fields from an upstream request.

    A list, not a mapping: a header sent twice reaches the API server twice,
    which rejects a repeated identity header instead of reading one value.
    """
    items = list(headers)
    connection_value = ",".join(
        value for name, value in items if name.lower() == "connection"
    )
    connection_tokens = {
        token.strip().lower()
        for token in connection_value.split(",")
        if token.strip()
    }
    blocked = _HOP_BY_HOP | connection_tokens
    return [(name, value) for name, value in items if name.lower() not in blocked]


def _forwarded_for(
    incoming: str | None,
    peer: str | None,
    policy: IdentityEdgePolicy,
    *,
    gateway_mode: bool,
) -> str | None:
    """The client chain the API server reads.

    In the gateway mode the API server reads the client with one trusted hop:
    a peer in the trusted list (the API gateway) already wrote it on the
    right, so its chain passes unchanged; any other peer is the client
    itself, and a chain it sends is discarded. Other modes keep appending the
    peer, which installations with more trusted hops depend on.
    """
    if not gateway_mode:
        if peer is None:
            return incoming
        return f"{incoming}, {peer}" if incoming else peer
    if peer is None:
        return None
    if policy.trusts(peer):
        return incoming or peer
    return peer


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
    *,
    policy: IdentityEdgePolicy,
    gateway_mode: bool,
) -> list[tuple[str, str]]:
    """Build the trusted HTTP forwarding boundary.

    Identity, proof and assertion headers of a peer outside the trusted list
    never reach the API server; the socket peer decides, never a forwarded
    chain.
    """
    peer = request.client.host if request.client else None
    incoming = ", ".join(request.headers.getlist("x-forwarded-for")) or None
    replaced = {"x-forwarded-proto", "x-forwarded-host", "x-forwarded-for"}
    if peer:
        replaced.add("x-real-ip")
    if policy.strips(peer):
        replaced |= policy.header_names
    headers = [
        (name, value)
        for name, value in _filter_headers(request.headers.items())
        if name.lower() not in replaced
    ]
    host = (
        public_origin.authority
        if public_origin is not None
        else request.headers.get("host")
    )
    if host:
        headers.append(("host", host))
        headers.append(("x-forwarded-host", host))
    forwarded_for = _forwarded_for(
        incoming, peer, policy, gateway_mode=gateway_mode
    )
    if forwarded_for:
        headers.append(("x-forwarded-for", forwarded_for))
    if peer:
        headers.append(("x-real-ip", peer))
    headers.append(
        (
            "x-forwarded-proto",
            _forwarded_scheme(request.url.scheme, public_origin, external_scheme),
        )
    )
    return headers


def _proxy_websocket_headers(
    websocket: WebSocket,
    public_origin: _PublicOrigin | None,
    external_scheme: str | None,
    *,
    policy: IdentityEdgePolicy,
    gateway_mode: bool,
) -> dict[str, str]:
    """Build application and forwarding headers for a backend handshake.

    Only the allowlisted headers pass; identity headers never reach the
    handshake, which is admitted by its collaboration lease alone.
    """
    headers = {
        name: value
        for name in _WEBSOCKET_FORWARD_HEADERS
        if (value := websocket.headers.get(name)) is not None
    }
    client_host = websocket.client.host if websocket.client else None
    if client_host:
        incoming = ", ".join(websocket.headers.getlist("x-forwarded-for")) or None
        forwarded_for = _forwarded_for(
            incoming, client_host, policy, gateway_mode=gateway_mode
        )
        if forwarded_for:
            headers["x-forwarded-for"] = forwarded_for
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
