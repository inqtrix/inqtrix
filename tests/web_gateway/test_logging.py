"""The web gateway logs in the Inqtrix format without the ``inqtrix`` package.

One console pipeline on stdout: JSON or text, a threshold from the
environment, exceptions as type, code and frames, foreign loggers from WARNING
on with their arguments as type names, uvicorn never below INFO and its
WebSocket line without client and query, guest-link tokens and secret shapes
scrubbed, and a crash that names only what the gateway itself raised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

from inqtrix_web_gateway import logging as gateway_logging
from inqtrix_web_gateway import settings as gateway_settings

MARKER = "zzmarker7"


def _lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


# --------------------------------------------------------------------- #
# Format, target and threshold
# --------------------------------------------------------------------- #


def test_the_gateway_writes_json_lines_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()
    logging.getLogger("inqtrix.web_gateway").info("Gateway %s ready.", "probe")

    captured = capsys.readouterr()
    (line,) = _lines(captured.out)
    assert line["message"] == "Gateway probe ready."
    assert (line["level"], line["service"], line["logger"]) == ("INFO", "gateway", "inqtrix.web_gateway")
    assert {"ts", "module", "function", "line", "thread"} <= line.keys()
    assert line["ts"].endswith("Z")
    assert captured.err == ""


def test_threshold_and_format_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "warning")
    monkeypatch.setenv("INQTRIX_LOG_FORMAT", "TEXT")
    gateway_logging.configure_logging()
    log = logging.getLogger("inqtrix.web_gateway")
    log.info("hidden line")
    log.warning("shown line")

    out = capsys.readouterr().out
    assert "hidden line" not in out
    assert "| WARNING | gateway | inqtrix.web_gateway | shown line" in out


@pytest.mark.parametrize("variable", ["INQTRIX_LOG_LEVEL", "INQTRIX_LOG_FORMAT"])
def test_an_empty_value_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], variable: str
) -> None:
    monkeypatch.setenv(variable, "")
    gateway_logging.configure_logging()
    log = logging.getLogger("inqtrix.web_gateway")
    log.debug("hidden probe")
    log.info("shown probe")

    # The defaults: threshold INFO, format JSON.
    assert [line["message"] for line in _lines(capsys.readouterr().out)] == ["shown probe"]


@pytest.mark.parametrize(
    ("variable", "allowed"),
    [
        ("INQTRIX_LOG_LEVEL", "DEBUG, INFO, WARNING, ERROR, CRITICAL"),
        ("INQTRIX_LOG_FORMAT", "json, text"),
    ],
)
def test_an_invalid_value_names_the_variable_not_the_value(
    monkeypatch: pytest.MonkeyPatch, variable: str, allowed: str
) -> None:
    monkeypatch.setenv(variable, MARKER)
    with pytest.raises(ValueError) as error:
        gateway_logging.configure_logging()

    assert MARKER not in str(error.value)
    assert variable in str(error.value)
    assert allowed in str(error.value)


def test_the_configuration_works_in_a_fresh_process_without_the_core_package() -> None:
    # uvicorn applies the mapping again in every worker process it spawns.
    config = gateway_logging.gateway_log_config()
    script = (
        "import json, logging, logging.config, sys\n"
        "logging.config.dictConfig(json.loads(sys.argv[1]))\n"
        "logging.getLogger('inqtrix.web_gateway').info('worker ready')\n"
        "print(json.dumps({'core_loaded': 'inqtrix' in sys.modules}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(config)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )

    log_line, modules = _lines(result.stdout)
    assert (log_line["message"], log_line["service"]) == ("worker ready", "gateway")
    assert modules == {"core_loaded": False}


def test_two_uvicorn_workers_log_in_the_gateway_format(fake_dist: Path, tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "RESEARCH_DESK_WORKERS": "2",
    }
    process = subprocess.Popen(
        [
            sys.executable, "-m", "inqtrix_web_gateway", "--dist-dir", str(fake_dist),
            "--backend-url", "http://127.0.0.1:9", "--host", "127.0.0.1", "--port", str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )
    output: list[str] = []
    reader = threading.Thread(target=lambda: output.extend(process.stdout or ()), daemon=True)
    reader.start()

    def started_workers() -> set[str]:
        return {
            match.group(1)
            for line in list(output)
            if (match := re.search(r'"message": "Started server process \[(\d+)\]"', line))
        }

    try:
        deadline = time.monotonic() + 60
        while len(started_workers()) < 2 and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        workers = started_workers()
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        reader.join(timeout=10)

    # uvicorn applies the gateway's mapping again in each worker it spawns.
    assert len(workers) == 2, "".join(output)[-2000:]
    lines = [line for line in output if line.strip()]
    assert all(line.startswith("{") and json.loads(line)["service"] == "gateway" for line in lines)


def test_a_logger_made_before_the_configuration_still_reaches_the_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    early = logging.getLogger("httpx")
    gateway_logging.configure_logging()
    early.warning("connection pool exhausted")

    assert [line["message"] for line in _lines(capsys.readouterr().out)] == [
        "connection pool exhausted"
    ]


# --------------------------------------------------------------------- #
# Content rule
# --------------------------------------------------------------------- #


def test_foreign_debug_lines_never_show(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    gateway_logging.configure_logging()
    # A library may lower its own level; its lines below WARNING stay out.
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpx").debug("HTTP Request: GET %s", f"http://api/x?q={MARKER}")
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").debug("send_request_headers.started request=%s", MARKER)
    logging.getLogger("inqtrix.web_gateway").debug("own debug line")

    out = capsys.readouterr().out
    assert MARKER not in out
    assert [line["message"] for line in _lines(out)] == ["own debug line"]


def test_a_foreign_warning_shows_its_arguments_as_types(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway_logging.configure_logging()
    logging.getLogger("websockets.client").warning("closing %s after %d tries", MARKER, 3)

    (line,) = _lines(capsys.readouterr().out)
    assert line["message"] == "closing <str> after 3 tries"


def test_exceptions_show_type_and_frames_only(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()
    try:
        raise RuntimeError(f"upstream said {MARKER} for /s/{MARKER}")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _lines(out)
    assert line["message"] == "Exception in ASGI application"
    assert line["exc"].startswith("Traceback (most recent call last):")
    assert line["exc"].splitlines()[-1] == "RuntimeError"


def test_a_traceback_inside_the_message_keeps_types_and_frames(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway_logging.configure_logging()
    logging.getLogger("uvicorn.error").error(
        "Application startup failed.\n"
        "Traceback (most recent call last):\n"
        '  File "/app/start.py", line 3, in start\n'
        f"    token = '{MARKER}'\n"
        f"ValueError: {MARKER}"
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _lines(out)
    assert line["message"].startswith("Application startup failed.\nTraceback")
    assert line["message"].endswith("\nValueError")


def test_uvicorn_never_shows_below_info_and_hides_websocket_client_and_query(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    gateway_logging.configure_logging()
    error = logging.getLogger("uvicorn.error")
    error.setLevel(logging.DEBUG)
    error.debug("< TEXT '%s' [%d bytes]", MARKER, 9)
    error.info('%s - "WebSocket %s" [accepted]', "10.1.2.3:5555", f"/collaboration?doc={MARKER}")

    out = capsys.readouterr().out
    assert MARKER not in out
    assert "10.1.2.3" not in out
    assert [line["message"] for line in _lines(out)] == ['- - "WebSocket /collaboration" [accepted]']


def test_guest_tokens_and_secret_shapes_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()
    logging.getLogger("inqtrix.web_gateway").warning(
        "Backend %s is unreachable for %s (Authorization: Bearer %s).",
        f"redis://:pw{MARKER}@cache:6379/0",
        f"/s/{MARKER}",
        f"tok{MARKER}",
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _lines(out)
    assert "/s/[REDACTED]" in line["message"]
    assert "Bearer [REDACTED]" in line["message"]


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_a_line_break_in_the_path_cannot_start_a_line_of_its_own(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], log_format: str
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_FORMAT", log_format)
    gateway_logging.configure_logging()
    forged = "2026-10-24T00:00:00.000Z | CRITICAL | gateway | forged"

    async def not_found(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    middleware = gateway_logging.AccessLogMiddleware(not_found)
    asyncio.run(middleware({"type": "http", "method": "GET", "path": f"/assets/x\n{forged}"}, receive, send))

    out = capsys.readouterr().out
    # The whole path stays in the one access line, its line break encoded.
    assert len(out.splitlines()) == 1
    assert "GET /assets/x%0A" in out


def test_asyncio_keeps_its_own_words(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()
    logging.getLogger("asyncio").error(
        f"Exception in callback cb('{MARKER}')\nhandle: <Handle cb('{MARKER}')>"
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    assert [line["message"] for line in _lines(out)] == ["Exception in callback cb"]


def test_python_warnings_keep_category_and_location(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()
    warnings.warn(f"{MARKER} deprecated call", UserWarning, stacklevel=1)

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _lines(out)
    assert line["logger"] == "py.warnings"
    assert line["message"].startswith("UserWarning at ")


# --------------------------------------------------------------------- #
# Crashes
# --------------------------------------------------------------------- #


def test_a_crash_names_what_the_gateway_raised_and_hides_foreign_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RESEARCH_DESK_WORKERS", MARKER)
    gateway_logging.configure_logging()
    try:
        gateway_settings._resolve_worker_count()
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = [line for line in _lines(out) if line["level"] == "CRITICAL"]
    assert line["message"] == (
        "Unhandled exception; the process stops: ValueError: RESEARCH_DESK_WORKERS must be "
        "an integer; caused by ValueError"
    )


def test_a_thread_crash_names_only_the_thread(capsys: pytest.CaptureFixture[str]) -> None:
    gateway_logging.configure_logging()

    def _work() -> None:
        raise RuntimeError(f"relay detail {MARKER}")

    worker = threading.Thread(target=_work, name="relay-probe")
    worker.start()
    worker.join()

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = [line for line in _lines(out) if line["level"] == "ERROR"]
    assert line["message"] == "Unhandled exception in thread relay-probe."
