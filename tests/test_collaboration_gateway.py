"""ASGI behavior tests for the same-origin collaboration gateway."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from uvicorn.protocols.utils import ClientDisconnected

from inqtrix.project.editor_collaboration_ports import CollaborationInstanceLease
from inqtrix.server import collaboration_gateway


class _FakeUpstream:
    """Async WebSocket peer with deterministic inbound and outbound frames."""

    def __init__(
        self,
        *,
        echo: bool = False,
        incoming: tuple[bytes | str | Exception, ...] = (),
    ) -> None:
        self.echo = echo
        self.incoming = incoming
        self.sent: list[bytes] = []
        self.close_calls: list[tuple[int, str]] = []
        self._queue: asyncio.Queue[bytes | str | Exception] | None = None

    async def __aenter__(self) -> "_FakeUpstream":
        """Create loop-affine queue state when the ASGI app connects."""
        self._queue = asyncio.Queue()
        for item in self.incoming:
            self._queue.put_nowait(item)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        """Leave exceptions visible to the gateway."""
        del exc_type, exc, traceback
        return False

    async def send(self, payload: bytes) -> None:
        """Record browser bytes and optionally echo a Node binary reply."""
        self.sent.append(bytes(payload))
        if self.echo:
            assert self._queue is not None
            self._queue.put_nowait(b"node:" + bytes(payload))

    async def close(self, *, code: int, reason: str = "") -> None:
        """Record the close signal sent toward Node."""
        self.close_calls.append((code, reason))

    def __aiter__(self) -> "_FakeUpstream":
        """Return the upstream frame iterator."""
        return self

    async def __anext__(self) -> bytes | str:
        """Yield the next scripted frame or raise its scripted exception.

        A scripted ``StopAsyncIteration`` ends the iteration the way a
        normally closed upstream connection does.
        """
        assert self._queue is not None
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    def unread(self) -> int:
        """Return how many scripted inbound items the relay has not read."""
        assert self._queue is not None
        return self._queue.qsize()


class _FailingContext:
    """Connection context that fails during the private WebSocket handshake."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> Any:
        """Raise the configured upstream failure."""
        raise self.error

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        """Keep the original failure visible."""
        del exc_type, exc, traceback
        return False


class _Connector:
    """Callable replacement for ``websockets.connect`` with call capture."""

    def __init__(
        self,
        upstream: _FakeUpstream | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.upstream = upstream
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        """Return the scripted connection context and record safe options."""
        self.calls.append((url, dict(kwargs)))
        if self.error is not None:
            return _FailingContext(self.error)
        assert self.upstream is not None
        return self.upstream


class _UpstreamClosed(Exception):
    """Minimal close exception carrying the public WebSocket close contract."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _ProbeService:
    """Script the authoritative service result behind the public HTTP probe."""

    def __init__(
        self,
        instance: CollaborationInstanceLease | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.instance = instance
        self.error = error
        self.calls = 0

    async def ready_instance(self) -> CollaborationInstanceLease | None:
        """Return or fail the scripted stable fencing/readiness check."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.instance


class _FailingBrowser:
    """Browser peer whose every send fails with one scripted error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.sends = 0

    async def send_bytes(self, payload: bytes) -> None:
        del payload
        self.sends += 1
        raise self.error


# The two shapes in which a send toward a browser that has already left fails:
# Starlette turns uvicorn's ClientDisconnected (the browser's close was read)
# into WebSocketDisconnect(1006); a send after the response completed is
# uvicorn's RuntimeError.
_BROWSER_GONE_FAILURES = (
    WebSocketDisconnect(1006),
    RuntimeError(
        "Unexpected ASGI message 'websocket.send', after sending "
        "'websocket.close' or response already completed."
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", _BROWSER_GONE_FAILURES, ids=["disconnect", "after-close"]
)
async def test_node_frames_after_the_browser_left_are_read_and_dropped_until_upstream_closes(
    failure: Exception,
) -> None:
    """A browser that left must not end the relay that keeps Node flowing.

    The browser relay still forwards what the browser sent before its close;
    this relay keeps reading Node's frames, drops them, and ends only when the
    upstream connection ends.
    """
    upstream = _FakeUpstream(
        incoming=(b"late-frame", b"later-frame", StopAsyncIteration())
    )
    browser = _FailingBrowser(failure)
    async with upstream:
        await asyncio.wait_for(
            collaboration_gateway._relay_node_to_browser(
                upstream,
                browser,  # type: ignore[arg-type]
                SimpleNamespace(max_frame_bytes=64),
            ),
            timeout=5,
        )
        assert upstream.unread() == 0
    assert browser.sends == 1


@pytest.mark.asyncio
async def test_node_relay_keeps_unexpected_browser_send_failures_visible() -> None:
    """Only a browser that has left starts the drain; other failures surface."""
    upstream = _FakeUpstream(
        incoming=(b"frame", b"later-frame", StopAsyncIteration())
    )
    browser = _FailingBrowser(RuntimeError("unexpected browser failure"))
    async with upstream:
        with pytest.raises(RuntimeError, match="unexpected browser failure"):
            await asyncio.wait_for(
                collaboration_gateway._relay_node_to_browser(
                    upstream,
                    browser,  # type: ignore[arg-type]
                    SimpleNamespace(max_frame_bytes=64),
                ),
                timeout=5,
            )
        assert upstream.unread() == 2
    assert browser.sends == 1


def _gateway_app(
    monkeypatch: pytest.MonkeyPatch,
    connector: _Connector,
    *,
    max_frame_bytes: int = 16,
    max_queued_frames: int = 32,
    allowed_origins: tuple[str, ...] = (),
    public_base_url: str = "",
    service: _ProbeService | None = None,
) -> FastAPI:
    """Bind the real gateway route to a private upstream test connector."""
    monkeypatch.setattr(collaboration_gateway, "connect", connector)
    settings = SimpleNamespace(
        ws_url="ws://collaboration.internal:1234/collaboration",
        secret="gateway-internal-test-secret",
        max_frame_bytes=max_frame_bytes,
        max_queued_frames=max_queued_frames,
        allowed_origins=allowed_origins,
    )
    container = SimpleNamespace(
        editor_collaboration_service=service or _ProbeService(),
        settings=SimpleNamespace(
            collaboration=settings,
            server=SimpleNamespace(public_base_url=public_base_url),
        ),
    )
    app = FastAPI()
    app.include_router(collaboration_gateway.build_router(container))
    return app


def _gateway_client(
    monkeypatch: pytest.MonkeyPatch,
    connector: _Connector,
    **options: Any,
) -> TestClient:
    """Serve the real gateway route through Starlette's test client."""
    return TestClient(
        _gateway_app(monkeypatch, connector, **options),
        raise_server_exceptions=False,
    )


async def _run_leaving_browser(
    app: FastAPI,
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
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return outbound


@pytest.mark.asyncio
async def test_gateway_relays_every_frame_a_leaving_browser_sent_while_node_sends_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames a closing page wrote reach Node before the upstream close."""
    upstream = _FakeUpstream(incoming=(b"node-acknowledgement",))
    app = _gateway_app(monkeypatch, _Connector(upstream), max_frame_bytes=64)
    frames = [b"update-%02d" % index for index in range(30)]
    forwarded_when_node_sent: list[int] = []

    outbound = await _run_leaving_browser(
        app,
        frames,
        close_code=1001,
        on_browser_send=lambda: forwarded_when_node_sent.append(
            len(upstream.sent)
        ),
    )

    # The race this test names happened: Node's frame met the gone browser
    # before the browser's frames were all forwarded.
    assert outbound == ["websocket.accept", "websocket.send"]
    assert len(forwarded_when_node_sent) == 1
    assert forwarded_when_node_sent[0] < len(frames)
    assert upstream.sent == frames
    assert upstream.close_calls == [(1001, "")]


def _assert_closed(websocket: Any, *, code: int, reason: str) -> None:
    """Assert the next ASGI event is the expected browser close frame."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        websocket.receive_bytes()
    assert exc_info.value.code == code
    assert exc_info.value.reason == reason


def test_instance_probe_returns_only_the_ready_authoritative_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unauthenticated release probe exposes no token or document data."""
    service = _ProbeService(
        CollaborationInstanceLease(
            instance_id="node-production-b",
            epoch=18,
            lease_expires_at=1_000.0,
            updated_at=990.0,
        )
    )
    connector = _Connector(_FakeUpstream())
    client = _gateway_client(monkeypatch, connector, service=service)

    with client:
        response = client.get("/collaboration/instance")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "contract": "inqtrix-collaboration-instance-v1",
        "service": "inqtrix-collaboration",
        "status": "ready",
        "instance_id": "node-production-b",
        "epoch": 18,
    }
    assert service.calls == 1
    assert connector.calls == []


@pytest.mark.parametrize("error", [None, RuntimeError("database unavailable")])
def test_instance_probe_fails_closed_when_authoritative_state_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
) -> None:
    """Missing or failed fencing state is a visible JSON 503, never fixture data."""
    service = _ProbeService(error=error)
    connector = _Connector(_FakeUpstream())
    client = _gateway_client(monkeypatch, connector, service=service)

    with client:
        response = client.get("/collaboration/instance")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "contract": "inqtrix-collaboration-instance-v1",
        "service": "inqtrix-collaboration",
        "status": "not_ready",
        "instance_id": None,
        "epoch": None,
    }
    assert connector.calls == []


@pytest.mark.parametrize(
    "origin",
    [None, "https://cross-origin.example", "http://testserver/forbidden-path"],
)
def test_gateway_rejects_missing_cross_site_and_malformed_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None,
) -> None:
    """Origin failure closes before any private service connection is attempted."""
    connector = _Connector(_FakeUpstream())
    client = _gateway_client(monkeypatch, connector)
    headers = {"Origin": origin} if origin is not None else {}

    with client:
        with client.websocket_connect("/collaboration", headers=headers) as websocket:
            _assert_closed(websocket, code=4403, reason="origin_rejected")

    assert connector.calls == []


@pytest.mark.parametrize(
    ("origin", "allowed_origins"),
    [
        ("http://testserver", ()),
        ("https://desk.example", ("https://desk.example",)),
    ],
)
def test_gateway_relays_binary_frames_for_same_and_allowlisted_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    allowed_origins: tuple[str, ...],
) -> None:
    """Allowed browsers exchange unchanged bytes through the authenticated hop."""
    upstream = _FakeUpstream(echo=True)
    connector = _Connector(upstream)
    client = _gateway_client(
        monkeypatch,
        connector,
        max_frame_bytes=16,
        max_queued_frames=7,
        allowed_origins=allowed_origins,
    )
    payload = b"\x00yjs\xff"

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": origin}
        ) as websocket:
            websocket.send_bytes(payload)
            assert websocket.receive_bytes() == b"node:" + payload

    assert upstream.sent == [payload]
    assert len(connector.calls) == 1
    url, options = connector.calls[0]
    assert url == "ws://collaboration.internal:1234/collaboration"
    assert options["additional_headers"] == {
        "Authorization": "Bearer gateway-internal-test-secret"
    }
    assert options["max_size"] == 16
    assert options["max_queue"] == 7


def test_gateway_accepts_tls_origin_from_sanitized_nginx_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingress TLS remains same-origin across the plaintext nginx/API hops."""
    upstream = _FakeUpstream(echo=True)
    connector = _Connector(upstream)
    client = _gateway_client(
        monkeypatch,
        connector,
        public_base_url="https://desk.example",
    )

    with client:
        with client.websocket_connect(
            "/collaboration",
            headers={
                "Origin": "https://desk.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "desk.example",
            },
        ) as websocket:
            websocket.send_bytes(b"update")
            assert websocket.receive_bytes() == b"node:update"

    assert upstream.sent == [b"update"]


@pytest.mark.parametrize(
    ("public_base_url", "origin", "forwarded_proto", "forwarded_host"),
    [
        ("", "https://desk.example", "https", "desk.example"),
        (
            "https://desk.example",
            "https://attacker.example",
            "https",
            "desk.example",
        ),
        (
            "https://desk.example",
            "https://desk.example",
            "https",
            "attacker.example",
        ),
        (
            "https://desk.example",
            "https://desk.example",
            "http",
            "desk.example",
        ),
        (
            "https://desk.example",
            "https://desk.example",
            "https,http",
            "desk.example",
        ),
    ],
)
def test_gateway_rejects_untrusted_or_mismatched_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    public_base_url: str,
    origin: str,
    forwarded_proto: str,
    forwarded_host: str,
) -> None:
    """Client-supplied forwarding headers cannot select an accepted origin."""
    connector = _Connector(_FakeUpstream())
    client = _gateway_client(
        monkeypatch,
        connector,
        public_base_url=public_base_url,
    )

    with client:
        with client.websocket_connect(
            "/collaboration",
            headers={
                "Origin": origin,
                "X-Forwarded-Proto": forwarded_proto,
                "X-Forwarded-Host": forwarded_host,
            },
        ) as websocket:
            _assert_closed(websocket, code=4403, reason="origin_rejected")

    assert connector.calls == []


def test_gateway_rejects_browser_text_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay is binary-only in both directions."""
    upstream = _FakeUpstream()
    connector = _Connector(upstream)
    client = _gateway_client(monkeypatch, connector)

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": "http://testserver"}
        ) as websocket:
            websocket.send_text("not a binary Yjs frame")
            _assert_closed(websocket, code=4409, reason="binary_frames_required")

    assert upstream.sent == []
    assert upstream.close_calls == [(4409, "binary_frames_required")]


def test_gateway_enforces_browser_to_node_frame_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized browser frame closes both relay endpoints with 1009."""
    upstream = _FakeUpstream()
    connector = _Connector(upstream)
    client = _gateway_client(monkeypatch, connector, max_frame_bytes=4)

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": "http://testserver"}
        ) as websocket:
            websocket.send_bytes(b"12345")
            _assert_closed(websocket, code=1009, reason="message_too_big")

    assert upstream.sent == []
    assert upstream.close_calls == [(1009, "message_too_big")]


def test_gateway_enforces_node_to_browser_frame_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized Node frame is never forwarded into the browser."""
    upstream = _FakeUpstream(incoming=(b"12345",))
    connector = _Connector(upstream)
    client = _gateway_client(monkeypatch, connector, max_frame_bytes=4)

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": "http://testserver"}
        ) as websocket:
            _assert_closed(websocket, code=1009, reason="message_too_big")

    assert upstream.sent == []


def test_gateway_maps_private_upstream_failure_to_service_unavailable_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Node handshake becomes the stable browser-side 4503 close."""
    connector = _Connector(error=OSError("private service refused connection"))
    client = _gateway_client(monkeypatch, connector)

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": "http://testserver"}
        ) as websocket:
            _assert_closed(
                websocket,
                code=4503,
                reason="collaboration_service_unavailable",
            )

    assert len(connector.calls) == 1


def test_gateway_propagates_upstream_application_close_code_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node policy closes retain their safe application code and reason."""
    monkeypatch.setattr(collaboration_gateway, "ConnectionClosed", _UpstreamClosed)
    upstream = _FakeUpstream(
        incoming=(_UpstreamClosed(4403, "access_revoked"),)
    )
    connector = _Connector(upstream)
    client = _gateway_client(monkeypatch, connector)

    with client:
        with client.websocket_connect(
            "/collaboration", headers={"Origin": "http://testserver"}
        ) as websocket:
            _assert_closed(websocket, code=4403, reason="access_revoked")

    assert len(connector.calls) == 1
