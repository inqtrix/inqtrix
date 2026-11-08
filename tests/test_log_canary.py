"""Canary: a marker in every input of the API never reaches a log channel.

The marker goes into the question, a header, the URL, an uploaded file (name
and content), every answer of the fake model, a provider exception and the
request path of an access line and a WebSocket line. The log runs on DEBUG to
stdout and to the file; neither, nor stderr, may carry the marker, and the
diagnostic lines a run writes must be there.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from inqtrix import logging_config
from inqtrix.providers.base import ChatTurn, ToolCallRequest
from inqtrix.server.request_context import RequestContextMiddleware
from tests._uvicorn_helpers import serving, websocket_attempt

pytest.importorskip("langchain")

from tests.agents.test_kernel_walking_skeleton import (  # noqa: E402
    ScriptedToolLLM,
    make_kernel_client,
    wait_status,
)

MARKER = "zzcanary7"


def _turn(*, text: str = "", calls: tuple[ToolCallRequest, ...] = ()) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=calls,
        finish_reason="tool_calls" if calls else "stop",
        model="high-model",
        prompt_tokens=40,
        completion_tokens=12,
        raw=None,
    )


class _FailingToolLLM(ScriptedToolLLM):
    def chat(self, messages: Any, **kwargs: Any) -> ChatTurn:
        raise RuntimeError(f"provider said {MARKER} with Bearer tok{MARKER}")


def _configure(tmp_path: Path) -> Path:
    logging_config.configure_logging(
        service="api", target="both", level="DEBUG", log_dir=str(tmp_path)
    )
    return tmp_path / "inqtrix-api.log"


def _channels(capsys: pytest.CaptureFixture[str], log_file: Path) -> tuple[str, str]:
    captured = capsys.readouterr()
    written = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    return captured.out + captured.err, written


def test_a_kernel_run_on_debug_leaves_no_marker_in_any_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = _configure(tmp_path)
    llm = ScriptedToolLLM(
        [
            _turn(calls=(
                ToolCallRequest(
                    id="call_todo0001",
                    name="write_todos",
                    arguments={"todos": [{"content": f"Plan {MARKER}", "status": "in_progress"}]},
                ),
                ToolCallRequest(
                    id="call_read0001",
                    name="read_canvas",
                    arguments={"artifact_id": f"art-{MARKER}"},
                ),
            )),
            _turn(calls=(
                ToolCallRequest(
                    id="call_read0002",
                    name="read_canvas",
                    arguments={"artifact_id": [MARKER], f"extra_{MARKER}": MARKER},
                ),
                ToolCallRequest(id="call_none0001", name=f"tool_{MARKER}", arguments={}),
            )),
            _turn(text=f"Antwort mit {MARKER} im Text."),
        ]
    )
    client = make_kernel_client(llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={"question": f"Was steht in {MARKER}?", "mode": "agent_kernel"},
            headers={"X-Request-ID": f"not a valid id {MARKER}"},
        ).json()["run_id"]
        wait_status(client, run_id, {"completed"})
        client.get(f"/v1/runs/{run_id}?probe={MARKER}")

    assert len(llm.chat_calls) == 3, "the run did not reach every scripted call"
    console, written = _channels(capsys, log_file)
    assert MARKER not in console
    assert MARKER not in written
    assert f"Run {run_id} segment started (mode=agent_kernel)." in written
    assert f"Run {run_id} segment ended (status=completed, mode=agent_kernel" in written


def test_a_provider_failure_leaves_no_marker_in_any_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = _configure(tmp_path)
    client = make_kernel_client(_FailingToolLLM([]))
    with client:
        run_id = client.post(
            "/v1/runs", json={"question": f"Frage {MARKER}", "mode": "agent_kernel"}
        ).json()["run_id"]
        wait_status(client, run_id, {"failed"})

    console, written = _channels(capsys, log_file)
    assert MARKER not in console
    assert MARKER not in written
    assert f"Run {run_id} segment ended (status=failed, failure_code=" in written


def test_an_uploaded_file_leaves_no_marker_in_any_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.test_files import make_files_client

    log_file = _configure(tmp_path)
    client, _identity = make_files_client(tmp_path)
    with client:
        stored = client.post(
            "/v1/files",
            files={"file": (f"vertrag-{MARKER}.txt", f"Inhalt {MARKER}".encode(), "text/plain")},
        )
        assert stored.status_code == 201, stored.status_code
        client.get(f"/v1/files/{stored.json()['id']}/text?probe={MARKER}")
        unreadable = client.post(
            "/v1/files",
            files={"file": (f"kaputt-{MARKER}.pdf", f"%PDF {MARKER}".encode(), "application/pdf")},
        )
        client.get(f"/v1/files/{unreadable.json()['id']}/text")

    console, written = _channels(capsys, log_file)
    assert MARKER not in console
    assert MARKER not in written
    assert "PDF page extraction failed (error_type=PDFSyntaxError); chunk page numbers unavailable" in written


def test_access_and_websocket_lines_of_the_api_carry_no_client_and_no_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = _configure(tmp_path)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/v1/probe")
    def probe() -> dict[str, str]:
        return {"ok": "yes"}

    with serving(app, None) as port:
        response = httpx.get(
            f"http://127.0.0.1:{port}/v1/probe?token={MARKER}",
            headers={"X-Request-ID": f"not a valid id {MARKER}"},
            timeout=5,
        )
        assert response.status_code == 200
        websocket_attempt(port, f"/collaboration?doc={MARKER}")
        time.sleep(0.2)

    console, written = _channels(capsys, log_file)
    assert MARKER not in console
    assert MARKER not in written
    records = [json.loads(line) for line in written.splitlines() if line.startswith("{")]
    access = [record for record in records if record["message"] == '- - "GET /v1/probe HTTP/1.1" 200']
    assert len(access) == 1
    assert access[0]["request_id"] == response.headers["X-Request-ID"]
    assert any(record["message"].startswith('- - "WebSocket /collaboration"') for record in records)
