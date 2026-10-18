"""The collaboration client survives callers that run each request on its own loop.

Kernel tool threads execute every capability call through the sync bridge,
which runs the coroutine on a private ``asyncio.run`` loop, and several tool
calls of one model turn run concurrently. A keep-alive connection pooled on
one of those loops must never be reused from another loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from inqtrix.services.collaboration_client import CollaborationNodeClient

_MARKDOWN = "# Arbeitsfassung\n\nText."
_PROJECTION = {
    "generation": 1,
    "sequence": 0,
    "projection_markdown": _MARKDOWN,
    "projection_hash": hashlib.sha256(_MARKDOWN.encode("utf-8")).hexdigest(),
    "schema_hash": "0" * 64,
    "schema_version": 1,
}


class _NodeHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keeps the connection alive, which is exactly what lets a
    # pooled socket outlive the loop that created it.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        self._reply(200 if self.path == "/health/ready" else 404, {})

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/project"):
            self._reply(200, _PROJECTION)
        else:
            self._reply(404, {})

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # silence the test server
        return


@pytest.fixture
def node_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# A regression does not fail cleanly: a pooled socket that belongs to a
# closed loop can block the next request forever, so every call is bounded.
_CALL_TIMEOUT_SECONDS = 10.0


async def _project(client: CollaborationNodeClient):
    return await asyncio.wait_for(
        client.project(document_id="ed_doc", generation=1, minimum_sequence=0),
        timeout=_CALL_TIMEOUT_SECONDS,
    )


async def _available(client: CollaborationNodeClient) -> bool:
    return await asyncio.wait_for(client.available(), timeout=_CALL_TIMEOUT_SECONDS)


def test_requests_from_successive_private_loops_do_not_reuse_a_dead_connection(node_url) -> None:
    """Sequential ``asyncio.run`` calls: the second loop must not inherit the first loop's socket."""
    client = CollaborationNodeClient(base_url=node_url, secret="test-secret")
    first = asyncio.run(_project(client))
    second = asyncio.run(_project(client))
    ready = asyncio.run(_available(client))
    assert first.markdown == second.markdown == _MARKDOWN
    assert ready is True
    asyncio.run(client.aclose())


def test_concurrent_private_loops_share_one_client_instance(node_url) -> None:
    """Parallel tool calls of one model turn each run their own loop at the same time."""
    client = CollaborationNodeClient(base_url=node_url, secret="test-secret")

    def call(_: int) -> int:
        return asyncio.run(_project(client)).sequence

    with ThreadPoolExecutor(max_workers=6) as executor:
        for _round in range(3):
            assert list(executor.map(call, range(6))) == [0] * 6
    asyncio.run(client.aclose())


def test_the_owning_loop_keeps_the_pooled_client(node_url) -> None:
    """Inside one long-lived loop the pooled client is reused, not replaced per request."""
    client = CollaborationNodeClient(base_url=node_url, secret="test-secret")

    async def scenario() -> None:
        async with client._client_for_current_loop() as first:
            pass
        async with client._client_for_current_loop() as second:
            pass
        assert first is second is client._client
        await client.aclose()

    asyncio.run(scenario())
