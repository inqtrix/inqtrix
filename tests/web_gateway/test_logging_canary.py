"""Canary: a marker in a request to the web gateway never reaches its log.

A real uvicorn server runs the gateway on DEBUG with its own log
configuration and, as in production, without uvicorn's access log. With an
unreachable backend every proxied request ends in the gateway's own warning;
the marker sits in the query, a header, the body, a guest-link path and the
query of a WebSocket request. With a real WebSocket backend the marker also
travels in a cookie and a relayed binary frame, which the WebSocket library
writes on DEBUG in hex.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import logging as gateway_logging
from tests._uvicorn_helpers import serving, websocket_attempt

MARKER = "zzcanary7"


def test_requests_with_markers_leave_no_marker_in_the_gateway_log(
    fake_dist: Path, clean_gateway_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_gateway_env.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    gateway_logging.configure_logging()
    app = gateway_app.build_app(fake_dist, "http://127.0.0.1:9")

    with serving(app, gateway_logging.gateway_log_config(), access_log=False) as port:
        base = f"http://127.0.0.1:{port}"
        httpx.get(f"{base}/v1/things?q={MARKER}", headers={"X-Probe": MARKER}, timeout=5)
        httpx.post(f"{base}/v1/runs", json={"question": MARKER}, timeout=5)
        httpx.get(f"{base}/s/{MARKER}", timeout=5)
        websocket_attempt(port, f"/collaboration?doc={MARKER}")
        time.sleep(0.2)

    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    messages = [json.loads(line)["message"] for line in captured.out.splitlines() if line.startswith("{")]
    assert "Configured backend is unreachable for GET /v1/things (ConnectError)." in messages
    assert any(message.startswith("GET /s/[REDACTED] ") for message in messages)
    assert any(message.startswith('- - "WebSocket /collaboration"') for message in messages)


def test_websocket_frames_with_markers_leave_no_marker_in_the_gateway_log(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    websocket_backend: tuple[str, dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from websockets.sync.client import connect

    backend_url, received = websocket_backend
    clean_gateway_env.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    gateway_logging.configure_logging()
    app = gateway_app.build_app(fake_dist, backend_url)
    payload = b"\x00" + MARKER.encode()

    with serving(app, gateway_logging.gateway_log_config(), access_log=False) as port:
        with connect(
            f"ws://127.0.0.1:{port}/collaboration?doc={MARKER}",
            origin=f"http://127.0.0.1:{port}",
            additional_headers={"Cookie": f"__Host-inqtrix_session={MARKER}"},
            open_timeout=5,
        ) as websocket:
            websocket.send(payload)
            assert websocket.recv(timeout=5) == b"\x00backend\xffreply"
        time.sleep(0.2)

    assert received["payload"] == payload
    captured = capsys.readouterr()
    written = captured.out + captured.err
    assert MARKER not in written
    assert MARKER.encode().hex(" ") not in written
    messages = [json.loads(line)["message"] for line in captured.out.splitlines() if line.startswith("{")]
    assert any(message.startswith('- - "WebSocket /collaboration"') for message in messages)
