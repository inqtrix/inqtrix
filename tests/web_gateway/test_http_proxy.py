"""Regression tests for gateway http proxy responsibility."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from inqtrix.settings import StorageSettings
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import http_proxy
from inqtrix_web_gateway import settings as gateway_settings

from .support import (
    _AbortingStream,
    _json_response,
)

def test_v1_proxies_to_backend(fake_dist: Path) -> None:
    """``GET /v1/runs`` reaches the upstream and returns its body."""
    received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["method"] = request.method
        received["auth"] = request.headers.get("authorization")
        return _json_response(200, {"data": ["proxied"]})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/runs",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"data": ["proxied"]}
    assert received["url"] == "http://backend.invalid/v1/runs"
    assert received["method"] == "GET"
    assert received["auth"] == "Bearer test-token"

def test_health_proxies_to_backend(fake_dist: Path) -> None:
    """``GET /health`` reaches the upstream ``/health`` endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://backend.invalid/health"
        return _json_response(200, {"status": "ok"})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_readyz_preserves_backend_unready_contract(fake_dist: Path) -> None:
    """``/readyz`` must never become a successful SPA fallback."""
    payload = {
        "status": "not_ready",
        "checks": {
            "database": "error",
            "queue": "ok",
            "vector_store": "ok",
            "object_store": "ok",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://backend.invalid/readyz"
        return _json_response(503, payload)

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == payload

def test_collaboration_instance_probe_proxies_to_backend(fake_dist: Path) -> None:
    """The dist gateway cannot serve SPA HTML at the release probe path."""
    payload = {
        "contract": "inqtrix-collaboration-instance-v1",
        "service": "inqtrix-collaboration",
        "status": "ready",
        "instance_id": "node-dist",
        "epoch": 9,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://backend.invalid/collaboration/instance"
        )
        assert request.method == "GET"
        return _json_response(200, payload)

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        response = client.get("/collaboration/instance")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == payload

def test_api_proxies_to_backend(fake_dist: Path) -> None:
    """``GET /api/auth/session`` reaches the upstream ``/api/*`` path.

    Without this the same-origin production login (auth BFF, local-auth
    setup wizard, admin routes) is unreachable behind the gateway.
    """
    received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["method"] = request.method
        return _json_response(200, {"authenticated": False})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        response = client.get("/api/auth/session")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert received["url"] == "http://backend.invalid/api/auth/session"
    assert received["method"] == "GET"

def test_auth_proxy_preserves_session_and_csrf_cookies(fake_dist: Path) -> None:
    """The same-origin gateway must not collapse the double-cookie contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://backend.invalid/api/setup/owner"
        body = b'{"authenticated":true}'
        return httpx.Response(
            201,
            stream=httpx.ByteStream(body),
            headers=[
                ("content-type", "application/json"),
                ("content-length", str(len(body))),
                (
                    "set-cookie",
                    "inqtrix_session=synthetic-session; HttpOnly; Path=/; SameSite=lax",
                ),
                (
                    "set-cookie",
                    "inqtrix_csrf=synthetic-csrf; Path=/; SameSite=lax",
                ),
            ],
        )

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        response = client.post("/api/setup/owner", json={"synthetic": True})

    assert response.status_code == 201
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert cookies[0].startswith("inqtrix_session=")
    assert cookies[1].startswith("inqtrix_csrf=")
    assert "HttpOnly" in cookies[0]
    assert "HttpOnly" not in cookies[1]


@pytest.mark.asyncio
async def test_backend_cookies_never_cross_independent_clients(
    fake_dist: Path,
) -> None:
    """A pooled backend client must not become a shared cookie authority."""
    observations: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie")
        if request.url.path == "/api/setup/owner":
            body = b'{"authenticated":true}'
            return httpx.Response(
                201,
                stream=httpx.ByteStream(body),
                headers=[
                    ("content-type", "application/json"),
                    ("content-length", str(len(body))),
                    (
                        "set-cookie",
                        "inqtrix_session=client-a; HttpOnly; Path=/",
                    ),
                    ("set-cookie", "inqtrix_csrf=client-a; Path=/"),
                ],
            )
        if request.url.path == "/api/auth/tokens":
            observations.append(cookie is None)
            return _json_response(401 if cookie is None else 200, {})
        observations.append(cookie == "independent=client-c")
        return httpx.Response(
            204 if cookie == "independent=client-c" else 401,
            stream=httpx.ByteStream(b""),
        )

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway_transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
        ) as client_a:
            seeded = await client_a.post("/api/setup/owner", json={"synthetic": True})
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
        ) as client_b:
            anonymous = await client_b.get("/api/auth/tokens")
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
            headers={"Cookie": "independent=client-c"},
        ) as client_c:
            independent = await client_c.get("/v1/independent")

    assert seeded.status_code == 201
    assert len(seeded.headers.get_list("set-cookie")) == 2
    assert anonymous.status_code == 401
    assert independent.status_code == 204
    assert observations == [True, True]


@pytest.mark.asyncio
async def test_backend_cookie_isolation_holds_under_overlapping_clients(
    fake_dist: Path,
) -> None:
    """Concurrent requests keep inbound cookie ownership per client."""

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie")
        if request.url.path == "/api/setup/owner":
            return httpx.Response(
                204,
                headers={"set-cookie": "inqtrix_session=seed; Path=/"},
                stream=httpx.ByteStream(b""),
            )
        if "/anonymous/" in request.url.path:
            return httpx.Response(
                401 if cookie is None else 200,
                stream=httpx.ByteStream(b""),
            )
        client_number = request.url.path.rsplit("/", 1)[-1]
        expected = f"independent={client_number}"
        return httpx.Response(
            204 if cookie == expected else 401,
            stream=httpx.ByteStream(b""),
        )

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway_transport = httpx.ASGITransport(app=app)

    async def anonymous_request(index: int) -> int:
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
        ) as client:
            response = await client.get(f"/v1/anonymous/{index}")
            return response.status_code

    async def independent_request(index: int) -> int:
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
            headers={"Cookie": f"independent={index}"},
        ) as client:
            response = await client.get(f"/v1/independent/{index}")
            return response.status_code

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway.invalid",
        ) as seed_client:
            assert (
                await seed_client.post("/api/setup/owner", json={"synthetic": True})
            ).status_code == 204
        anonymous_statuses, independent_statuses = await asyncio.gather(
            asyncio.gather(*(anonymous_request(i) for i in range(12))),
            asyncio.gather(*(independent_request(i) for i in range(12))),
        )

    assert set(anonymous_statuses) == {401}
    assert set(independent_statuses) == {204}


def test_backend_unreachable_maps_to_502(fake_dist: Path) -> None:
    """A connect failure answers 502 Bad Gateway like nginx, not a 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        response = client.get("/v1/runs")

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]
    assert "backend.invalid" not in response.text


def test_backend_failure_log_redacts_route_and_exception_details(
    fake_dist: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport diagnostics contain neither guest tokens nor free-form data."""
    route_token = "SYNTHETIC-SHARE-TOKEN"
    exception_secret = "SYNTHETIC-TRANSPORT-CREDENTIAL"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(exception_secret)

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level("WARNING", logger="inqtrix.web_gateway"):
        with TestClient(app) as client:
            response = client.get(
                f"/v1/editor/share-links/{route_token}:unlock"
            )

    assert response.status_code == 502
    assert route_token not in caplog.text
    assert exception_secret not in caplog.text
    assert "/v1/editor/share-links/[REDACTED]" in caplog.text
    assert "ConnectError" in caplog.text


def test_proxy_preserves_raw_target(fake_dist: Path) -> None:
    """Encoded path segments and repeated query keys survive verbatim.

    Rebuilding the URL from the decoded route parameter would turn
    ``a%2Fb`` into ``a/b`` and collapse ``tag=1&tag=2`` to the last
    value; nginx forwards the raw request target and so must the
    gateway.
    """
    received: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        return _json_response(200, {})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        client.get("/v1/items/a%2Fb?tag=1&tag=2")

    assert received["url"] == "http://backend.invalid/v1/items/a%2Fb?tag=1&tag=2"

def test_proxy_streams_request_body(fake_dist: Path) -> None:
    """Upload bodies reach the backend through the streaming path."""
    received: dict[str, bytes] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = await request.aread()
        return _json_response(200, {})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        client.post("/v1/files", content=b"chunk-one chunk-two")

    assert received["body"] == b"chunk-one chunk-two"

def test_midstream_abort_ends_quietly_and_closes_upstream(
    fake_dist: Path,
) -> None:
    """A backend dying mid-stream truncates the body and frees the upstream.

    Without the relay guard the ReadError escapes as an unhandled ASGI
    exception and the upstream response is never closed (pooled
    connection leak on every dropped SSE stream).
    """
    stream = _AbortingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with TestClient(app) as client:
        response = client.get("/v1/runs/xyz/events")

    assert response.status_code == 200
    assert response.text == "data: one\n\n"
    assert stream.closed

def test_backend_timeout_keeps_read_unbounded_for_configurable_server_waits() -> None:
    """The read timeout must stay unbounded; write and pool are finite.

    Server-side waits are operator-configurable without a cap
    (``request_timeout_seconds`` defaults to 3630 seconds) and chat SSE
    emits no keepalive frames, so any finite read value silently kills
    legitimate long requests. This test is the tripwire against a
    well-meaning "nginx parity" change reintroducing that bug.
    """
    timeout = http_proxy._backend_timeout()
    assert timeout.read is None
    assert timeout.connect == 60.0
    assert timeout.write == 3600.0
    assert timeout.pool == 10.0

def test_backend_limits_are_explicit() -> None:
    """Both pool fields are pinned; omitted Limits fields mean unbounded."""
    limits = http_proxy._backend_limits(37)
    assert limits.max_connections == 37
    assert limits.max_keepalive_connections == 20

@pytest.mark.parametrize("max_connections", [True, 0, -1])
def test_max_upstream_connections_rejects_invalid_configuration(
    fake_dist: Path,
    max_connections: object,
) -> None:
    """Gateway construction fails loudly for a non-positive pool ceiling."""
    with pytest.raises(ValueError, match="INQTRIX_MAX_UPSTREAM_CONNECTIONS"):
        gateway_app.build_app(
            fake_dist,
            "http://backend.invalid",
            max_upstream_connections=max_connections,
        )

def test_pool_exhaustion_answers_503_not_backend_unreachable(
    fake_dist: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PoolTimeout is gateway saturation, not a dead backend.

    The generic TransportError arm would log "unreachable" and answer 502,
    sending the operator to debug a healthy backend; the dedicated arm must
    answer 503 and name the tuning knob.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool wait budget exceeded")

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    with caplog.at_level("WARNING", logger="inqtrix.web_gateway"):
        with TestClient(app) as client:
            response = client.get("/v1/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "Upstream connection pool exhausted"}
    assert "INQTRIX_MAX_UPSTREAM_CONNECTIONS" in caplog.text
    assert "unreachable" not in caplog.text

def test_oversized_content_length_is_rejected_before_upstream(
    fake_dist: Path,
) -> None:
    """A declared oversized body gets 413 without contacting the backend."""
    contacted: dict[str, bool] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        contacted["backend"] = True
        return _json_response(200, {})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=transport,
        max_request_bytes=1000,
    )

    with TestClient(app) as client:
        response = client.post("/v1/files", content=b"x" * 1001)

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert contacted == {}

def test_body_at_exact_limit_is_forwarded(fake_dist: Path) -> None:
    """The cap is strictly greater-than: an exact-limit body passes."""
    received: dict[str, bytes] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = await request.aread()
        return _json_response(200, {})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=transport,
        max_request_bytes=1000,
    )

    with TestClient(app) as client:
        response = client.post("/v1/files", content=b"x" * 1000)

    assert response.status_code == 200
    assert received["body"] == b"x" * 1000


@pytest.mark.asyncio
async def test_chunked_body_is_counted_and_aborted_above_limit(
    fake_dist: Path,
) -> None:
    """Missing Content-Length cannot bypass the streaming gateway ceiling."""
    upstream_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request.url.path)
        return _json_response(200, {})

    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(handler),
        max_request_bytes=1000,
    )

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 600
        yield b"y" * 500

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.invalid",
            ) as client:
                response = await client.post("/v1/files", content=chunks())
                follow_up = await client.get("/health")

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert follow_up.status_code == 200
    assert upstream_calls == ["/health"]


@pytest.mark.asyncio
async def test_bounded_stream_never_yields_the_over_limit_chunk() -> None:
    """The real transport can receive at most the configured byte ceiling."""

    class ChunkedRequest:
        async def stream(self) -> AsyncIterator[bytes]:
            yield b"x" * 600
            yield b"y" * 500

    forwarded: list[bytes] = []
    body_done = asyncio.Event()
    with pytest.raises(http_proxy._RequestBodyTooLarge):
        async for chunk in http_proxy._bounded_request_stream(  # type: ignore[arg-type]
            ChunkedRequest(),
            1000,
            body_done,
        ):
            forwarded.append(chunk)

    assert forwarded == [b"x" * 600]
    assert not body_done.is_set()


def test_request_body_limit_leaves_api_413_authoritative() -> None:
    """The packaged cap sits strictly above the API's own precheck.

    ``files.py`` prechecks at ``max_file_bytes + 64 KiB`` and its 413
    carries the user-facing message; the gateway cap must never win that
    race (the documented intent behind the nginx template's ``110m``). A
    naive "mirror the API precheck" refactor would invert it.
    """
    api_precheck = (
        int(StorageSettings.model_fields["max_file_bytes"].default) + 64 * 1024
    )
    assert gateway_settings._DEFAULT_MAX_REQUEST_BYTES > api_precheck
    assert gateway_settings._DEFAULT_MAX_REQUEST_BYTES == 115_343_360

def test_build_app_wires_pool_policy_into_client(
    fake_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_app`` must feed the policy builders into the httpx client.

    Request-level tests cannot see the pool policy because an injected
    MockTransport bypasses the pool, so this pins the constructor wiring:
    inlining the builders away (or dropping ``limits=``) would bring back
    silent hangs and read timeouts while every request-level test stays
    green.
    """
    captured: dict[str, Any] = {}
    real_client = http_proxy.httpx.AsyncClient

    class RecordingClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(http_proxy.httpx, "AsyncClient", RecordingClient)
    gateway_app.build_app(
        fake_dist, "http://backend.invalid", max_upstream_connections=44
    )

    assert captured["timeout"].read is None
    assert captured["timeout"].write == 3600.0
    assert captured["timeout"].pool == 10.0
    assert captured["limits"].max_connections == 44
    assert captured["limits"].max_keepalive_connections == 20


def _disconnect_scope(method: str, extra_headers: list | None = None) -> dict:
    headers = [(b"host", b"testserver")]
    headers.extend(extra_headers or [])
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


class _HangingTransport(httpx.AsyncBaseTransport):
    """Upstream that never answers until cancelled (long non-stream call)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.completed = asyncio.Event()

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        await request.aread()
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed.set()
        return _json_response(200, {"never": "delivered"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "body"),
    [("POST", b"{}"), ("GET", None)],
    ids=["with-body", "without-body"],
)
async def test_client_disconnect_while_awaiting_upstream_aborts_upstream(
    fake_dist: Path, method: str, body: bytes | None
) -> None:
    """A browser abort during the response wait must cancel the upstream
    request instead of letting the backend compute to completion."""
    transport = _HangingTransport()
    app = gateway_app.build_app(
        fake_dist, "http://backend.invalid", transport=transport
    )

    extra = (
        [(b"content-type", b"application/json"),
         (b"content-length", str(len(body)).encode())]
        if body is not None
        else []
    )
    scope = _disconnect_scope(method, extra)

    body_messages = []
    if body is not None:
        body_messages.append(
            {"type": "http.request", "body": body, "more_body": False}
        )
    sent: list[dict] = []

    async def receive() -> dict:
        if body_messages:
            return body_messages.pop(0)
        await transport.started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=5)

    assert transport.cancelled.is_set()
    assert not transport.completed.is_set()
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 499


@pytest.mark.asyncio
async def test_aborted_upstream_connection_is_never_reused(
    fake_dist: Path,
) -> None:
    """A cancelled upstream exchange must close its connection: reusing a
    half-run HTTP/1.1 socket from the keep-alive pool would desync every
    following request on it."""
    connections: list[asyncio.StreamWriter] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connections.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
            if len(connections) == 1:
                # First request: never answer; wait for the abort (EOF).
                await reader.read()
                return
            body = b'{"ok": true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: application/json\r\n"
                b"content-length: " + str(len(body)).encode() + b"\r\n"
                b"\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    app = gateway_app.build_app(
        fake_dist, f"http://{host}:{port}", transport=None
    )

    sent_first: list[dict] = []

    async def receive_abort() -> dict:
        # Give the proxy time to open the upstream connection, then leave.
        while not connections:
            await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send_first(message: dict) -> None:
        sent_first.append(message)

    await asyncio.wait_for(
        app(_disconnect_scope("GET"), receive_abort, send_first), timeout=5
    )

    sent_second: list[dict] = []
    second_done = asyncio.Event()

    async def receive_idle() -> dict:
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    async def send_second(message: dict) -> None:
        sent_second.append(message)
        if message["type"] == "http.response.body" and not message.get(
            "more_body"
        ):
            second_done.set()

    task = asyncio.ensure_future(
        app(_disconnect_scope("GET"), receive_idle, send_second)
    )
    await asyncio.wait_for(second_done.wait(), timeout=5)
    await asyncio.wait_for(task, timeout=5)
    server.close()
    await server.wait_closed()

    aborted = next(
        m for m in sent_first if m["type"] == "http.response.start"
    )
    assert aborted["status"] == 499
    start = next(
        m for m in sent_second if m["type"] == "http.response.start"
    )
    assert start["status"] == 200
    assert len(connections) == 2
