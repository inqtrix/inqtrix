"""A real uvicorn server on a free local port, for tests that read its log."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn


@contextmanager
def serving(app: Any, log_config: Any, *, access_log: bool = True) -> Iterator[int]:
    """*app* behind a real uvicorn server; yields the port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_config=log_config, access_log=access_log, lifespan="off", ws="websockets"
        )
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def websocket_attempt(port: int, path: str) -> None:
    """Open and close a WebSocket to *path*; a refused handshake is fine."""
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    try:
        with connect(f"ws://127.0.0.1:{port}{path}", open_timeout=5):
            pass
    except (InvalidStatus, OSError):
        pass
