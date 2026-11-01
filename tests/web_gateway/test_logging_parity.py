"""The gateway's copy of the log contract writes what the API's filter writes.

The web image ships the gateway without the ``inqtrix`` package, so the
gateway carries its own small copy of the rules. The same records go through
the API's configuration (service ``gateway``) and the gateway's; every field
but the timestamp must match.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, Callable

import pytest

from inqtrix import logging_config, urls
from inqtrix_web_gateway import logging as gateway_logging

MARKER = "zzmarker7"

# The field contract the collaboration server's tests read as well.
_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "logging" / "field_contract.json").read_text(
        encoding="utf-8"
    )
)

# One value per secret shape the scrubbing knows, each carrying the marker.
_SECRET_SHAPES = (
    f"https://api.example/v1/items?api_key={MARKER}&page=2",
    f"ftp://{MARKER}@files.example/report",
    f"sk-{MARKER}abcdefghijklmnop",
    f"pplx-{MARKER}abcdefghijklmnop",
    f"AKIA{MARKER.upper()}ABCDEFG",
    f"aws_secret_access_key={MARKER}",
    f"host=db password={MARKER} user=app",
    f"INQTRIX_SESSION_SECRET={MARKER}",
)


def _raise(message: str) -> None:
    raise RuntimeError(message)


def _emit_cases() -> None:
    gateway = logging.getLogger("inqtrix.web_gateway")
    gateway.info("Gateway %s ready on port %d.", "probe", 8080)
    gateway.debug("own debug line %s", MARKER)
    gateway.warning("Backend %s is unreachable for %s.", f"postgresql://app:pw{MARKER}@db/x", f"/s/{MARKER}")
    gateway.warning("Upstream answered %s (Bearer %s).", "502", f"tok{MARKER}")
    for value in _SECRET_SHAPES:
        gateway.warning("Configured value refused: %s", value)
    logging.getLogger("httpx").warning("closing %s after %d tries", MARKER, 3)
    logging.getLogger("httpx").info("HTTP Request: GET %s", f"http://api/x?q={MARKER}")
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "10.1.2.3:5555", f"/collaboration?doc={MARKER}"
    )
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    logging.getLogger("asyncio").error(f"Exception in callback cb('{MARKER}')\nhandle: <Handle>")
    try:
        _raise(f"upstream said {MARKER}")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    try:
        _raise(f"relay failed {MARKER}")
    except RuntimeError as exc:
        gateway.warning("Relay stopped (%s).", exc)
    logging.getLogger("uvicorn.error").error(
        "Application startup failed.\n"
        "Traceback (most recent call last):\n"
        '  File "/app/start.py", line 3, in start\n'
        f"ValueError: {MARKER}"
    )


def _run(configure: Callable[[], Any], capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    configure()
    capsys.readouterr()
    _emit_cases()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    for line in lines:
        line.pop("ts")
    logging.captureWarnings(False)
    return lines


def _forget_the_api_configuration(hooks: tuple[Any, Any, Any]) -> None:
    # The gateway must produce its lines with its own handler, not with one
    # the API configuration left on the root logger.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for name in ("inqtrix", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    sys.excepthook, threading.excepthook, warnings.showwarning = hooks


def test_the_gateway_logs_exactly_like_the_api(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = (sys.excepthook, threading.excepthook, warnings.showwarning)
    api = _run(lambda: logging_config.configure_logging(service="gateway"), capsys)
    _forget_the_api_configuration(hooks)
    gateway = _run(gateway_logging.configure_logging, capsys)

    assert all(MARKER not in json.dumps(line) for line in api)
    assert all(MARKER.upper() not in json.dumps(line) for line in api)
    assert len(api) == 10 + len(_SECRET_SHAPES)
    assert gateway == api


def test_the_gateway_knows_the_secret_shapes_of_the_api() -> None:
    assert gateway_logging._CREDENTIAL_KEY_EXACT == urls._CREDENTIAL_KEY_EXACT
    assert gateway_logging._CREDENTIAL_KEY_MARKERS == urls._CREDENTIAL_KEY_MARKERS
    for name in ("_URL_IN_TEXT_RE", "_URL_PASSWORD_RE", "_KEYWORD_PASSWORD_RE", "_SECRET_ASSIGNMENT_RE"):
        ours, theirs = getattr(gateway_logging, name), getattr(urls, name)
        assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags), name


@pytest.mark.parametrize(
    "configure",
    [lambda: logging_config.configure_logging(service="gateway"), gateway_logging.configure_logging],
    ids=["api", "gateway"],
)
def test_each_python_service_writes_the_shared_field_contract(
    configure: Callable[[], Any], capsys: pytest.CaptureFixture[str]
) -> None:
    configure()
    capsys.readouterr()
    logging.getLogger("inqtrix.web_gateway").warning("Probe line.")
    line = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert set(_CONTRACT["common_fields"]) <= set(line)
    assert line["level"] in _CONTRACT["levels"]
    assert re.fullmatch(_CONTRACT["ts_pattern"], line["ts"])
