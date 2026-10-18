"""Regression tests for gateway websocket proxy responsibility."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import settings as gateway_settings
from inqtrix_web_gateway import websocket_proxy as gateway_websocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from uvicorn.protocols.utils import ClientDisconnected

from .support import (
    _FakeWebSocketUpstream,
    _WebSocketConnector,
)


class _OneFrameUpstream:
    """One late upstream frame followed by a clean iterator end."""

    close_code = 1000
    close_reason = ""

    def __init__(self, payload: bytes) -> None:
        self._payload: bytes | None = payload

    def __aiter__(self) -> "_OneFrameUpstream":
        return self

    async def __anext__(self) -> bytes:
        payload = self._payload
        if payload is None:
            raise StopAsyncIteration
        self._payload = None
        return payload


class _BrowserSendFailure:
    """Browser double for a late send after a configurable ASGI state."""

    def __init__(self, state: WebSocketState) -> None:
        self.application_state = state
        self.client_state = state

    async def send_bytes(self, payload: bytes) -> None:
        del payload
        raise RuntimeError("browser response already completed")

    async def accept(self) -> None:
        raise AssertionError("relay must not re-accept the browser")

    async def close(self, *, code: int, reason: str = "") -> None:
        del code, reason

def test_collaboration_relays_binary_websocket_to_fastapi_backend(
    fake_dist: Path,
    websocket_backend: tuple[str, dict[str, object]],
) -> None:
    """``/collaboration`` is a binary relay to the configured FastAPI URL.

    The upstream handshake keeps the browser-facing Host and Origin even
    though the TCP connection goes to the private backend address. This is
    required by FastAPI's same-origin gateway check and proves that the
    gateway doesn't connect browsers directly to the Node service.
    """
    backend_url, captured = websocket_backend
    app = gateway_app.build_app(fake_dist, backend_url)
    payload = b"\x00browser\x80update\xff"

    with TestClient(app) as client:
        with client.websocket_connect(
            "/collaboration?room=doc%2Fone&client=browser%2Bone",
            headers={
                "Host": "desk.example:8443",
                "Origin": "http://desk.example:8443",
                "Cookie": "__Host-inqtrix_session=session-value",
            },
        ) as websocket:
            websocket.send_bytes(payload)
            assert websocket.receive_bytes() == b"\x00backend\xffreply"

    assert captured == {
        "path": "/collaboration?room=doc%2Fone&client=browser%2Bone",
        "host": "desk.example:8443",
        "origin": "http://desk.example:8443",
        "cookie": "__Host-inqtrix_session=session-value",
        "xff": "testclient",
        "proto": "http",
        "forwarded_host": "desk.example:8443",
        "payload": payload,
    }

def test_collaboration_tls_forwarding_uses_explicit_public_origin(
    fake_dist: Path,
    websocket_backend: tuple[str, dict[str, object]],
) -> None:
    """Outer TLS metadata comes only from configured public origin."""
    backend_url, captured = websocket_backend
    app = gateway_app.build_app(
        fake_dist,
        backend_url,
        public_base_url="https://Desk.Example:8443/",
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/collaboration",
            headers={
                "Host": "gateway.internal:8080",
                "Origin": "https://desk.example:8443",
                "Cookie": "__Host-inqtrix_session=session-value",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Host": "attacker.example",
            },
        ) as websocket:
            websocket.send_bytes(b"tls-update")
            assert websocket.receive_bytes() == b"\x00backend\xffreply"

    assert captured["host"] == "desk.example:8443"
    assert captured["origin"] == "https://desk.example:8443"
    assert captured["proto"] == "https"
    assert captured["forwarded_host"] == "desk.example:8443"
    assert captured["payload"] == b"tls-update"

def test_collaboration_unavailable_closes_with_retryable_gateway_code(
    fake_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable FastAPI upstream is distinct from an internal fault."""

    def unavailable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("upstream unavailable")

    monkeypatch.setattr(gateway_websocket, "websocket_connect", unavailable)
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")

    with TestClient(app) as client:
        with client.websocket_connect("/collaboration") as websocket:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_bytes()

    assert exc_info.value.code == 4503
    assert exc_info.value.reason == "collaboration_gateway_unavailable"


class _ScriptedUpstream:
    """Upstream frames followed by a normal end of the iteration."""

    close_code = 1000
    close_reason = ""

    def __init__(self, *payloads: bytes) -> None:
        self._payloads = list(payloads)

    def __aiter__(self) -> "_ScriptedUpstream":
        return self

    async def __anext__(self) -> bytes:
        if not self._payloads:
            raise StopAsyncIteration
        return self._payloads.pop(0)

    def unread(self) -> int:
        """Return how many scripted frames the relay has not read."""
        return len(self._payloads)


class _LeftBrowser:
    """Browser double whose every send fails because the browser has left."""

    def __init__(self, error: Exception, state: WebSocketState) -> None:
        self.error = error
        self.application_state = state
        self.client_state = state
        self.sends = 0
        self.closes = 0

    async def send_bytes(self, payload: bytes) -> None:
        del payload
        self.sends += 1
        raise self.error

    async def accept(self) -> None:
        raise AssertionError("relay must not re-accept the browser")

    async def close(self, *, code: int, reason: str = "") -> None:
        del code, reason
        self.closes += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "state"),
    [
        # Starlette turns uvicorn's ClientDisconnected into this error once
        # the server has read the browser's close.
        (WebSocketDisconnect(1006), WebSocketState.CONNECTED),
        (
            RuntimeError("browser response already completed"),
            WebSocketState.DISCONNECTED,
        ),
    ],
    ids=["disconnect", "after-close"],
)
async def test_backend_frames_after_the_browser_left_are_read_and_dropped_until_upstream_closes(
    error: Exception,
    state: WebSocketState,
) -> None:
    """A browser that left must not end the relay that keeps FastAPI flowing.

    The browser relay still forwards what the browser sent before its close;
    this relay keeps reading the backend's frames, drops them, and ends only
    when the upstream connection ends.
    """
    upstream = _ScriptedUpstream(b"late-frame", b"later-frame", b"last-frame")
    browser = _LeftBrowser(error, state)

    await asyncio.wait_for(
        gateway_websocket._relay_upstream_websocket(
            upstream,  # type: ignore[arg-type]
            browser,  # type: ignore[arg-type]
            64,
        ),
        timeout=5,
    )

    assert upstream.unread() == 0
    assert browser.sends == 1
    assert browser.closes == 0


async def _run_leaving_browser(
    app: Any,
    frames: list[bytes],
    *,
    close_code: int,
    on_browser_send: Any,
) -> list[str]:
    """Drive the route with a browser that sends frames, then closes.

    The server already holds every frame and the close when the route starts
    reading, as uvicorn does for a page that unloads. A send toward the browser
    fails the way uvicorn fails it once it has read that close.
    """
    inbound: list[dict[str, Any]] = [
        {"type": "websocket.connect"},
        *({"type": "websocket.receive", "bytes": frame} for frame in frames),
        {"type": "websocket.disconnect", "code": close_code},
    ]
    outbound: list[str] = []

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0)
        return inbound.pop(0)

    async def send(message: dict[str, Any]) -> None:
        outbound.append(message["type"])
        if message["type"] == "websocket.send":
            on_browser_send()
            raise ClientDisconnected()

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "http_version": "1.1",
        "path": "/collaboration",
        "raw_path": b"/collaboration",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"desk.example:8443"),
            (b"origin", b"http://desk.example:8443"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("desk.example", 8443),
        "subprotocols": [],
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return outbound


@pytest.mark.asyncio
async def test_collaboration_relays_every_frame_a_leaving_browser_sent_while_the_backend_sends_to_it(
    fake_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames a closing page wrote reach FastAPI before the upstream close."""
    upstream = _FakeWebSocketUpstream(incoming=(b"backend-acknowledgement",))
    monkeypatch.setattr(
        gateway_websocket, "websocket_connect", _WebSocketConnector(upstream)
    )
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    frames = [b"update-%02d" % index for index in range(30)]
    forwarded_when_backend_sent: list[int] = []

    outbound = await _run_leaving_browser(
        app,
        frames,
        close_code=1001,
        on_browser_send=lambda: forwarded_when_backend_sent.append(
            len(upstream.sent)
        ),
    )

    # The race this test names happened: the backend's frame met the gone
    # browser before the browser's frames were all forwarded.
    assert outbound == ["websocket.accept", "websocket.send"]
    assert len(forwarded_when_backend_sent) == 1
    assert forwarded_when_backend_sent[0] < len(frames)
    assert upstream.sent == frames
    assert upstream.close_calls == [(1001, "")]


@pytest.mark.asyncio
async def test_upstream_relay_keeps_connected_browser_send_failures_visible() -> None:
    """A send failure while both ASGI states are live must still propagate."""
    with pytest.raises(RuntimeError, match="already completed"):
        await gateway_websocket._relay_upstream_websocket(
            _OneFrameUpstream(b"unexpected-failure"),  # type: ignore[arg-type]
            _BrowserSendFailure(WebSocketState.CONNECTED),  # type: ignore[arg-type]
            64,
        )


@pytest.mark.parametrize("code", [1007, 1010, 1013, 1014])
def test_standard_websocket_close_codes_retain_semantics(code: int) -> None:
    """Valid standard close codes are never collapsed to an internal error."""
    assert gateway_websocket._safe_websocket_close_code(code) == code


def test_websocket_close_reason_is_bounded_by_utf8_bytes() -> None:
    """Multibyte reasons fit the 123-byte close-frame allowance."""
    reason = "ä" * 100
    truncated = gateway_websocket._truncate_websocket_reason(reason)
    assert len(truncated.encode("utf-8")) <= 123
    assert truncated == "ä" * 61


def test_collaboration_rejects_oversized_browser_frame_before_upstream_send(
    fake_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway closes 1009 before forwarding an oversized browser frame."""
    limit = 65_536
    settings = gateway_settings.CollaborationProxySettings(
        max_frame_bytes=limit,
        max_queued_frames=5,
    )
    upstream = _FakeWebSocketUpstream()
    connector = _WebSocketConnector(upstream)
    monkeypatch.setattr(gateway_websocket, "websocket_connect", connector)
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        collaboration_settings=settings,
    )

    with TestClient(app) as client:
        with client.websocket_connect("/collaboration") as websocket:
            websocket.send_bytes(b"x" * (limit + 1))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_bytes()

    assert exc_info.value.code == 1009
    assert exc_info.value.reason == "message_too_big"
    assert upstream.sent == []
    assert (1009, "message_too_big") in upstream.close_calls
    assert connector.calls[0][1]["max_size"] == limit
    assert connector.calls[0][1]["max_queue"] == 5

def test_collaboration_rejects_oversized_upstream_frame_before_browser_send(
    fake_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reverse relay closes both peers with 1009 for oversized output."""
    limit = 65_536
    upstream = _FakeWebSocketUpstream(incoming=(b"x" * (limit + 1),))
    connector = _WebSocketConnector(upstream)
    monkeypatch.setattr(gateway_websocket, "websocket_connect", connector)
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        collaboration_settings=gateway_settings.CollaborationProxySettings(
            max_frame_bytes=limit
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect("/collaboration") as websocket:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_bytes()

    assert exc_info.value.code == 1009
    assert exc_info.value.reason == "message_too_big"
    assert upstream.sent == []
    assert (1009, "message_too_big") in upstream.close_calls

@pytest.mark.parametrize(
    "frame_limit",
    [True, 65_535, 16 * 1_048_576 + 1],
)
def test_collaboration_frame_limit_rejects_invalid_configuration(
    frame_limit: object,
) -> None:
    """Gateway construction fails loudly for values outside shared bounds."""
    with pytest.raises(
        ValueError,
        match="INQTRIX_COLLABORATION_MAX_FRAME_BYTES",
    ):
        gateway_settings.CollaborationProxySettings(
            max_frame_bytes=frame_limit,  # type: ignore[arg-type]
        )

@pytest.mark.parametrize("queue_limit", [0, 257])
def test_collaboration_queue_limit_rejects_invalid_configuration(
    queue_limit: int,
) -> None:
    """The canonical gateway settings bound the parser's frame queue."""
    with pytest.raises(ValueError):
        gateway_settings.CollaborationProxySettings(
            max_queued_frames=queue_limit,
        )

def test_collaboration_external_scheme_does_not_retarget_websocket_transport(
    fake_dist: Path,
    websocket_backend: tuple[str, dict[str, object]],
) -> None:
    """``https`` override pins the forwarded proto but never the transport.

    The fixture backend speaks plaintext ``ws``; a handshake that reached it
    proves the override did not flip :func:`_websocket_upstream` to ``wss``
    (which would attempt TLS against the cleartext socket and kill every
    collaboration session). The browser Host must survive unchanged because
    the override is scheme-only.
    """
    backend_url, captured = websocket_backend
    app = gateway_app.build_app(
        fake_dist,
        backend_url,
        external_scheme="https",
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/collaboration",
            headers={
                "Host": "desk.example:8443",
                "Origin": "https://desk.example:8443",
                "Cookie": "__Host-inqtrix_session=session-value",
            },
        ) as websocket:
            websocket.send_bytes(b"scheme-update")
            assert websocket.receive_bytes() == b"\x00backend\xffreply"

    assert captured["proto"] == "https"
    assert captured["host"] == "desk.example:8443"
    assert captured["forwarded_host"] == "desk.example:8443"
    assert captured["payload"] == b"scheme-update"
