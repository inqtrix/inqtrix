"""The gateway spreads requests over the replicas behind one backend name."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import backend_replicas, http_proxy
from inqtrix_web_gateway.backend_replicas import (
    BackendReplicaTransport,
    replica_transport,
)

NAME = "backend.test"
URL = f"http://{NAME}:5100/v1/runs"


class _Recording(httpx.AsyncBaseTransport):
    """Answer every request and remember what reached the transport."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.seen: list[httpx.Request] = []
        self.hosts: list[str] = []
        self.closed = False

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.seen.append(request)
        self.hosts.append(request.url.host)
        failure = self.failures.get(request.url.host)
        if failure is not None:
            raise failure
        return httpx.Response(200)

    async def aclose(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Names:
    """A resolver the test can change, count and hold back."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.error: Exception | None = None
        self.calls = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def __call__(self, host: str) -> list[str]:
        assert host == NAME
        self.calls += 1
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return list(self.addresses)


def _transport(
    inner: httpx.AsyncBaseTransport,
    names: _Names,
    clock: _Clock | None = None,
) -> BackendReplicaTransport:
    return BackendReplicaTransport(
        inner, host=NAME, resolve=names, clock=clock or _Clock()
    )


def _request(url: str = URL, **kwargs: Any) -> httpx.Request:
    return httpx.Request("GET", url, **kwargs)


async def _settle() -> None:
    """Let the background lookup finish."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _ready(transport: BackendReplicaTransport) -> None:
    """Send the first request, which starts the lookup, and wait for its answer."""
    await transport.handle_async_request(_request())
    await _settle()


@pytest.mark.asyncio
async def test_requests_take_the_addresses_in_turn() -> None:
    inner = _Recording()
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)
    inner.hosts.clear()

    for _ in range(6):
        await transport.handle_async_request(_request())

    assert Counter(inner.hosts) == {"10.0.0.8": 3, "10.0.0.9": 3}
    assert inner.hosts[0] != inner.hosts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [[], ["10.0.0.8"]])
async def test_without_several_addresses_the_request_stays_as_it_is(
    addresses: list[str],
) -> None:
    inner = _Recording()
    transport = _transport(inner, _Names(addresses))
    await _ready(transport)

    request = _request(headers={"x-probe": "1"})
    extensions = dict(request.extensions)
    await transport.handle_async_request(request)

    assert str(request.url) == URL
    assert request.headers["x-probe"] == "1"
    assert request.extensions == extensions


@pytest.mark.asyncio
async def test_a_request_never_waits_for_the_lookup() -> None:
    inner = _Recording()
    names = _Names(["10.0.0.8", "10.0.0.9"])
    names.gate.clear()
    transport = _transport(inner, names)

    await asyncio.wait_for(transport.handle_async_request(_request()), timeout=1)
    await asyncio.wait_for(transport.handle_async_request(_request()), timeout=1)

    assert inner.hosts == [NAME, NAME]
    await _settle()
    assert names.calls == 1
    names.gate.set()
    await _settle()
    await transport.handle_async_request(_request())
    assert inner.hosts[-1] in {"10.0.0.8", "10.0.0.9"}


@pytest.mark.asyncio
async def test_the_name_stays_in_the_host_header_and_the_certificate_check() -> None:
    inner = _Recording()
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)

    plain = _request()
    await transport.handle_async_request(plain)
    assert plain.url.host in {"10.0.0.8", "10.0.0.9"}
    assert plain.url.port == 5100
    assert plain.url.path == "/v1/runs"
    assert plain.headers["host"] == f"{NAME}:5100"
    assert "sni_hostname" not in plain.extensions

    secure = _request(f"https://{NAME}/v1/runs")
    await transport.handle_async_request(secure)
    assert secure.url.host in {"10.0.0.8", "10.0.0.9"}
    assert secure.headers["host"] == NAME
    assert secure.extensions["sni_hostname"] == NAME


@pytest.mark.asyncio
async def test_a_raw_target_survives_the_new_address() -> None:
    inner = _Recording()
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)

    request = _request(f"http://{NAME}:5100/v1/a%2Fb?q=x%20y")
    await transport.handle_async_request(request)

    assert request.url.raw_path == b"/v1/a%2Fb?q=x%20y"


@pytest.mark.asyncio
async def test_a_request_for_another_host_is_not_redirected() -> None:
    inner = _Recording()
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)

    request = _request("http://other.test/v1/runs")
    await transport.handle_async_request(request)

    assert request.url.host == "other.test"


@pytest.mark.asyncio
async def test_the_name_is_looked_up_once_per_window() -> None:
    inner = _Recording()
    names = _Names(["10.0.0.8", "10.0.0.9"])
    clock = _Clock()
    transport = _transport(inner, names, clock)
    await _ready(transport)

    for _ in range(5):
        await transport.handle_async_request(_request())
    clock.now += 9.9
    await transport.handle_async_request(_request())
    await _settle()
    assert names.calls == 1

    names.addresses = ["10.0.0.8", "10.0.0.9", "10.0.0.10"]
    clock.now += 0.2
    await transport.handle_async_request(_request())
    await _settle()
    assert names.calls == 2

    inner.hosts.clear()
    for _ in range(3):
        await transport.handle_async_request(_request())
    assert Counter(inner.hosts) == {
        "10.0.0.8": 1,
        "10.0.0.9": 1,
        "10.0.0.10": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OSError("no such name"), UnicodeError("bad label")]
)
async def test_a_failed_lookup_keeps_the_last_answer_and_says_so_once(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    inner = _Recording()
    names = _Names(["10.0.0.8", "10.0.0.9"])
    clock = _Clock()
    transport = _transport(inner, names, clock)
    await _ready(transport)

    names.error = error
    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        for _ in range(3):
            clock.now += 11
            await transport.handle_async_request(_request())
            await _settle()
        inner.hosts.clear()
        await transport.handle_async_request(_request())
        names.error = None
        clock.now += 11
        await transport.handle_async_request(_request())
        await _settle()

    assert inner.hosts[0] in {"10.0.0.8", "10.0.0.9"}
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert type(error).__name__ in warnings[0].getMessage()
    assert "resolves again" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("timed out")],
)
async def test_an_unreachable_address_is_skipped_and_the_request_goes_on(
    caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    inner = _Recording({"10.0.0.8": failure})
    clock = _Clock()
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]), clock)
    await _ready(transport)
    inner.hosts.clear()

    with caplog.at_level(logging.WARNING, logger="inqtrix.web_gateway"):
        for _ in range(4):
            response = await transport.handle_async_request(_request())
            assert response.status_code == 200

    assert inner.hosts.count("10.0.0.8") == 1
    assert inner.hosts.count("10.0.0.9") == 4
    warnings = [
        record for record in caplog.records if "10.0.0.8" in record.getMessage()
    ]
    assert len(warnings) == 1

    inner.hosts.clear()
    clock.now += 10.1
    for _ in range(2):
        await transport.handle_async_request(_request())
    assert "10.0.0.8" in inner.hosts


@pytest.mark.asyncio
async def test_with_no_address_left_the_connection_error_reaches_the_caller() -> None:
    failure = httpx.ConnectError("refused")
    inner = _Recording({"10.0.0.8": failure, "10.0.0.9": failure})
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)
    inner.hosts.clear()

    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(_request())
    assert sorted(inner.hosts) == ["10.0.0.8", "10.0.0.9"]

    # All of them skipped is no reason to stop trying.
    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError("gone"),
        httpx.WriteError("gone"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("broken"),
    ],
)
async def test_a_failure_after_connecting_is_never_sent_again(
    failure: Exception,
) -> None:
    inner = _Recording({"10.0.0.8": failure, "10.0.0.9": failure})
    transport = _transport(inner, _Names(["10.0.0.8", "10.0.0.9"]))
    await _ready(transport)
    inner.hosts.clear()

    with pytest.raises(type(failure)):
        await transport.handle_async_request(
            httpx.Request("POST", URL, content=b"once")
        )

    assert len(inner.hosts) == 1


@pytest.mark.asyncio
async def test_closing_stops_the_lookup_and_closes_the_transport() -> None:
    inner = _Recording()
    names = _Names(["10.0.0.8", "10.0.0.9"])
    names.gate.clear()
    transport = _transport(inner, names)
    await transport.handle_async_request(_request())
    await _settle()

    closing = asyncio.ensure_future(transport.aclose())
    await _settle()
    done = closing.done()
    names.gate.set()
    await closing

    assert done
    assert inner.closed
    assert names.calls == 1


@pytest.mark.asyncio
async def test_only_ipv4_counts_each_address_once_in_a_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(family: int, address: tuple[Any, ...]) -> tuple[Any, ...]:
        return (family, socket.SOCK_STREAM, 6, "", address)

    async def getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            answer(socket.AF_INET6, ("fd00::1", 0, 0, 0)),
            answer(socket.AF_INET, ("10.0.0.9", 0)),
            answer(socket.AF_INET, ("10.0.0.8", 0)),
            answer(socket.AF_INET, ("10.0.0.9", 0)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)

    assert await backend_replicas._resolve_ipv4(NAME) == ["10.0.0.8", "10.0.0.9"]


@pytest.mark.asyncio
async def test_localhost_counts_as_one_address() -> None:
    """Spreading over ``127.0.0.1`` and ``::1`` would fail every other request."""
    assert await backend_replicas._resolve_ipv4("localhost") == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_a_real_connection_carries_the_name_and_falls_over() -> None:
    """Connection choice happens below the mock, so this one uses a socket."""
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def names(_host: str) -> list[str]:
        # Nothing listens on the second address of the loopback range.
        return ["127.0.0.1", "127.0.0.2"]

    transport = BackendReplicaTransport(
        httpx.AsyncHTTPTransport(), host=NAME, resolve=names
    )
    client = httpx.AsyncClient(transport=transport, trust_env=False)
    transport._addresses = await names(NAME)
    transport._next_refresh = float("inf")
    try:
        for _ in range(3):
            response = await client.get(f"http://{NAME}:{port}/v1/runs")
            assert response.status_code == 200
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()

    assert len(requests) == 3
    assert all(f"host: {NAME}:{port}".encode() in r.lower() for r in requests)


def _clear_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http", "https", "all", "no"):
        monkeypatch.delenv(f"{name}_proxy", raising=False)
        monkeypatch.delenv(f"{name.upper()}_PROXY", raising=False)


def test_a_name_gets_the_spreading_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxies(monkeypatch)
    limits = httpx.Limits(max_connections=7)
    built: dict[str, Any] = {}
    real = httpx.AsyncHTTPTransport

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            built.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(backend_replicas.httpx, "AsyncHTTPTransport", Recording)

    transport = replica_transport("http://api:5100", limits)

    assert isinstance(transport, BackendReplicaTransport)
    assert built["limits"] is limits


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:5100", "http://[::1]:5100", "https://10.0.0.8"]
)
def test_an_address_has_no_replicas_behind_it(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    _clear_proxies(monkeypatch)
    assert replica_transport(url, httpx.Limits()) is None


@pytest.mark.parametrize(
    ("variable", "url", "spreads"),
    [
        ("HTTP_PROXY", "http://api:5100", False),
        ("ALL_PROXY", "http://api:5100", False),
        ("HTTPS_PROXY", "https://api", False),
        # httpx sends a plain backend past a proxy that only covers https.
        ("HTTPS_PROXY", "http://api:5100", True),
        ("NO_PROXY", "http://api:5100", True),
    ],
)
def test_a_proxy_from_the_environment_keeps_the_request_on_the_name(
    monkeypatch: pytest.MonkeyPatch, variable: str, url: str, spreads: bool
) -> None:
    _clear_proxies(monkeypatch)
    monkeypatch.setenv(variable, "http://proxy.test:3128")

    transport = replica_transport(url, httpx.Limits())

    assert isinstance(transport, BackendReplicaTransport) is spreads


def test_the_client_of_the_app_uses_the_spreading_transport(
    fake_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_proxies(monkeypatch)
    captured: dict[str, Any] = {}
    real_client = http_proxy.httpx.AsyncClient

    class RecordingClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(http_proxy.httpx, "AsyncClient", RecordingClient)

    gateway_app.build_app(fake_dist, "http://api:5100")

    assert isinstance(captured["transport"], BackendReplicaTransport)


def test_an_injected_transport_is_used_as_it_is(
    fake_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real_client = http_proxy.httpx.AsyncClient

    class RecordingClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(http_proxy.httpx, "AsyncClient", RecordingClient)
    injected = _Recording()

    gateway_app.build_app(fake_dist, "http://api:5100", transport=injected)

    assert captured["transport"] is injected
