"""Spread backend requests over the replicas behind one backend name."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx

log = logging.getLogger("inqtrix.web_gateway")

_REFRESH_SECONDS = 10.0
_SKIP_SECONDS = 10.0


async def _resolve_ipv4(host: str) -> list[str]:
    """Return the distinct IPv4 addresses of a name in a stable order."""
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return sorted(
        {str(answer[4][0]) for answer in answers if answer[0] == socket.AF_INET}
    )


class BackendReplicaTransport(httpx.AsyncBaseTransport):
    """Send each request to the next address the backend name resolves to.

    Left alone, a new connection takes the first address of an answer whose
    order hardly changes, so further replicas stay idle. With fewer than two
    addresses a request passes untouched, which is the normal case: one
    Compose replica, or a Kubernetes Service with its one address.

    The name is looked up in the background at most once per refresh window,
    so a request never waits for it and a lookup never dies with an aborted
    request. An address that refuses the connection is skipped for a while and
    the same request goes on to the next one, as a connection to the name does
    too: nothing has been sent at that point, so even a POST cannot run twice.
    Any later failure stays the caller's.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        host: str,
        resolve: Callable[[str], Awaitable[list[str]]] = _resolve_ipv4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._host = host
        self._resolve = resolve
        self._clock = clock
        self._addresses: list[str] = []
        self._skipped_until: dict[str, float] = {}
        self._turn = 0
        self._next_refresh = 0.0
        self._refresh_task: asyncio.Future[None] | None = None
        self._resolve_failing = False

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        """Forward one request to the next address, or as it is."""
        self._refresh_when_due()
        if (
            len(self._addresses) < 2
            or request.url.raw_host.decode("ascii") != self._host
        ):
            return await self._inner.handle_async_request(request)
        if request.url.scheme == "https":
            # The certificate is checked against the name, never the address.
            request.extensions = {
                **request.extensions,
                "sni_hostname": self._host,
            }
        for address in self._in_turn():
            request.url = request.url.copy_with(host=address)
            try:
                return await self._inner.handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                self._skip(address, exc)
                unreachable = exc
        raise unreachable

    async def aclose(self) -> None:
        """Stop a running lookup and close the wrapped transport."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task
        await self._inner.aclose()

    def _in_turn(self) -> list[str]:
        now = self._clock()
        usable = [
            address
            for address in self._addresses
            if self._skipped_until.get(address, 0.0) <= now
        ] or self._addresses
        start = self._turn % len(usable)
        self._turn += 1
        return usable[start:] + usable[:start]

    def _skip(self, address: str, exc: Exception) -> None:
        self._skipped_until[address] = self._clock() + _SKIP_SECONDS
        log.warning(
            "Backend address %s is unreachable (%s); skipping it for %.0f s.",
            address,
            type(exc).__name__,
            _SKIP_SECONDS,
        )

    def _refresh_when_due(self) -> None:
        now = self._clock()
        if now < self._next_refresh:
            return
        self._next_refresh = now + _REFRESH_SECONDS
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.ensure_future(self._refresh())

    async def _refresh(self) -> None:
        try:
            addresses = await self._resolve(self._host)
        except Exception as exc:  # noqa: BLE001 - a bad label is no OSError
            if not self._resolve_failing:
                self._resolve_failing = True
                log.warning(
                    "Backend name did not resolve (%s); keeping the last answer.",
                    type(exc).__name__,
                )
            return
        if self._resolve_failing:
            self._resolve_failing = False
            log.info("Backend name resolves again.")
        if addresses != self._addresses:
            log.info("Backend name resolves to %d address(es).", len(addresses))
        self._addresses = addresses


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def replica_transport(
    backend_url: str, limits: httpx.Limits
) -> httpx.AsyncBaseTransport | None:
    """Build the spreading transport, or ``None`` where the client's own is right.

    An address has no replicas behind it. A proxy from the environment decides
    by the name (``NO_PROXY=api``), which an address in the URL would no longer
    match, so the request stays on the name.
    """
    url = httpx.URL(backend_url)
    if _is_ip_address(url.host):
        return None
    proxies = urllib.request.getproxies()
    if proxies.get(url.scheme) or proxies.get("all"):
        log.info(
            "A proxy is configured in the environment: backend requests "
            "keep going to the name and are not spread over its addresses."
        )
        return None
    return BackendReplicaTransport(
        httpx.AsyncHTTPTransport(limits=limits),
        host=url.raw_host.decode("ascii"),
    )
