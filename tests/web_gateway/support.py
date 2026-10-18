"""Shared fixtures and deterministic protocol doubles for gateway tests."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import cli as gateway_cli
from inqtrix_web_gateway import settings as gateway_settings
from websockets.sync.server import ServerConnection, serve

def _json_response(status_code: int, payload: object) -> httpx.Response:
    """Build a streaming Mock response.

    ``httpx.Response(json=...)`` reads the stream eagerly, which leaves
    the response in a ``is_stream_consumed=True`` state and makes
    ``aiter_raw()`` raise ``StreamConsumed`` when the proxy iterates it.
    Using an explicit ``stream=`` parameter keeps the body unread until
    the production code iterates it through Starlette.
    """
    body = json.dumps(payload).encode("utf-8")
    return httpx.Response(
        status_code,
        stream=httpx.ByteStream(body),
        headers={
            "content-type": "application/json",
            "content-length": str(len(body)),
        },
    )

class _FakeWebSocketUpstream:
    """Deterministic ``websockets`` peer for gateway relay contracts."""

    def __init__(self, *, incoming: tuple[bytes | str, ...] = ()) -> None:
        self.incoming = incoming
        self.sent: list[bytes] = []
        self.close_calls: list[tuple[int, str]] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._queue: asyncio.Queue[bytes | str] | None = None

    async def __aenter__(self) -> "_FakeWebSocketUpstream":
        self._queue = asyncio.Queue()
        for payload in self.incoming:
            self._queue.put_nowait(payload)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del exc_type, exc, traceback
        return False

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    async def close(self, *, code: int, reason: str = "") -> None:
        self.close_calls.append((code, reason))
        self.close_code = code
        self.close_reason = reason

    def __aiter__(self) -> "_FakeWebSocketUpstream":
        return self

    async def __anext__(self) -> bytes | str:
        assert self._queue is not None
        return await self._queue.get()

class _WebSocketConnector:
    """Capture gateway upstream options and return a scripted peer."""

    def __init__(self, upstream: _FakeWebSocketUpstream) -> None:
        self.upstream = upstream
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, target: str, **kwargs: Any) -> _FakeWebSocketUpstream:
        self.calls.append((target, dict(kwargs)))
        return self.upstream

@pytest.fixture
def fake_dist(tmp_path: Path) -> Path:
    """Create a minimal dist/ layout with index.html and one asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>fake-react-app</body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "main.js").write_text(
        "console.log('fake bundle');", encoding="utf-8"
    )
    return dist

_GATEWAY_ENV_VARS = (
    "RESEARCH_DESK_HOST",
    "RESEARCH_DESK_PORT",
    "RESEARCH_DESK_WORKERS",
    "RESEARCH_DESK_SSL_CERTFILE",
    "RESEARCH_DESK_SSL_KEYFILE",
    "RESEARCH_DESK_SSL_KEYFILE_PASSWORD",
    "INQTRIX_BACKEND_URL",
    "INQTRIX_API_UPSTREAM",
    "INQTRIX_PUBLIC_BASE_URL",
    "INQTRIX_EXTERNAL_SCHEME",
    "INQTRIX_DIST_DIR",
    "INQTRIX_MAX_UPSTREAM_CONNECTIONS",
    "INQTRIX_PROXY_MAX_BODY_BYTES",
    "INQTRIX_MAX_FILE_BYTES",
    "WEB_CONCURRENCY",
    "INQTRIX_COLLABORATION_ENABLED",
    "INQTRIX_COLLABORATION_MAX_FRAME_BYTES",
    "INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES",
)

@pytest.fixture
def clean_gateway_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove all gateway environment knobs before the test sets its own."""
    for name in _GATEWAY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch

def _run_stubbed_main(
    monkeypatch: pytest.MonkeyPatch,
    fake_dist: Path,
    argv: Sequence[str] = (),
) -> dict[str, Any]:
    """Run ``main()`` with its three seams stubbed and record what they saw.

    Unlike the explicit-signature stub in
    ``test_main_propagates_non_default_collaboration_transport_limits``
    (which exists to break loudly when the ``build_app`` call-site signature
    changes), this helper accepts any kwargs: its callers assert on uvicorn
    options, call order, and app identity, not on the build contract.
    """
    captured: dict[str, Any] = {"order": []}
    app = object()
    captured["stub_app"] = app

    def fake_build_app(*args: Any, **kwargs: Any) -> object:
        captured["order"].append("build_app")
        captured["build_app_args"] = args
        captured["build_app_kwargs"] = kwargs
        return app

    def fake_run(received_app: object, **kwargs: Any) -> None:
        captured["order"].append("uvicorn.run")
        captured["uvicorn_app"] = received_app
        captured["uvicorn_options"] = kwargs

    monkeypatch.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    monkeypatch.setattr(gateway_app, "build_app", fake_build_app)
    monkeypatch.setattr(gateway_cli.uvicorn, "run", fake_run)
    gateway_cli.main(argv)
    return captured

@pytest.fixture
def websocket_backend() -> Iterator[tuple[str, dict[str, object]]]:
    """Run a real local WebSocket endpoint for binary relay verification."""
    captured: dict[str, object] = {}
    reply = b"\x00backend\xffreply"

    def handler(connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        captured["path"] = request.path
        captured["host"] = request.headers["Host"]
        captured["origin"] = request.headers["Origin"]
        captured["cookie"] = request.headers["Cookie"]
        captured["xff"] = request.headers["X-Forwarded-For"]
        captured["proto"] = request.headers["X-Forwarded-Proto"]
        captured["forwarded_host"] = request.headers["X-Forwarded-Host"]
        captured["payload"] = connection.recv(timeout=5)
        connection.send(reply)

    server = serve(handler, "127.0.0.1", 0, compression=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.socket.getsockname()[:2]
        yield f"http://{host}:{port}", captured
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()

class _AbortingStream(httpx.AsyncByteStream):
    """Upstream body that dies after the first chunk (backend restart)."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: one\n\n"
        raise httpx.ReadError("upstream reset")

    async def aclose(self) -> None:
        self.closed = True
