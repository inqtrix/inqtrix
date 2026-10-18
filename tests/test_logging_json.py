"""JSON log format (INQTRIX_LOG_FORMAT=json) and its text-mode guarantees.

The JSON formatter renders one machine-readable object per line with the
bound correlation fields; the redaction filter runs unchanged in front of
it. Without the flag the historical text output stays byte-identical.
"""

from __future__ import annotations

import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.logging_config import (
    _FILE_FORMAT,
    _RedactSecretsFilter,
    build_uvicorn_log_config,
    configure_logging,
    describe_logging_state,
    read_logging_env,
)
from inqtrix.observability.context import bind_log_context, reset_log_context
from inqtrix.observability.json_formatter import InqtrixJsonFormatter
from inqtrix.server.request_context import RequestContextMiddleware


def _json_capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """An isolated inqtrix-child logger writing JSON lines to a buffer."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(InqtrixJsonFormatter())
    handler.addFilter(_RedactSecretsFilter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, buffer


def test_json_line_carries_required_fields_and_context():
    logger, buffer = _json_capture_logger("inqtrix.test_json_fields")
    tokens = bind_log_context(
        request_id="req-42", run_id="run_abc", user="usr_0123456789abcdef"
    )
    try:
        logger.info("hello %s", "world")
    finally:
        reset_log_context(tokens)
    line = json.loads(buffer.getvalue().strip())
    assert line["message"] == "hello world"
    assert line["level"] == "INFO"
    assert line["logger"] == "inqtrix.test_json_fields"
    assert line["event"] == "log"
    assert line["request_id"] == "req-42"
    assert line["run_id"] == "run_abc"
    assert line["user"] == "usr_0123456789abcdef"
    assert line["ts"].endswith("+00:00")
    assert "thread" in line


def test_json_line_redacts_url_credentials():
    logger, buffer = _json_capture_logger("inqtrix.test_json_redaction")
    logger.info("calling https://api.example.com/v1?api_key=SECRET123")
    line = json.loads(buffer.getvalue().strip())
    assert "SECRET123" not in line["message"]


def test_json_line_renders_exception_field():
    logger, buffer = _json_capture_logger("inqtrix.test_json_exc")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.warning("failed", exc_info=True)
    line = json.loads(buffer.getvalue().strip())
    assert line["level"] == "WARNING"
    assert "ValueError" in line["exc"]


def test_structured_payload_merges_without_clobbering_reserved_keys():
    logger, buffer = _json_capture_logger("inqtrix.test_json_payload")
    logger.info(
        "EVENT run_end",
        extra={
            "inqtrix_event": "run_end",
            "inqtrix_payload": {"status": "completed", "message": "shadow"},
        },
    )
    line = json.loads(buffer.getvalue().strip())
    assert line["event"] == "run_end"
    assert line["status"] == "completed"
    assert line["message"] == "EVENT run_end"
    assert line["payload_message"] == "shadow"


def test_text_mode_stays_byte_identical(tmp_path):
    """Without the flag the file handler keeps the historical format."""
    log_file = configure_logging(enabled=True, log_dir=str(tmp_path))
    try:
        logger = logging.getLogger("inqtrix")
        handler = next(
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        )
        assert not isinstance(handler.formatter, InqtrixJsonFormatter)
        assert handler.formatter._style._fmt == _FILE_FORMAT
        assert describe_logging_state()["format"] == "text"
        assert log_file is not None
    finally:
        configure_logging(enabled=False)


def test_json_mode_switches_handlers_and_state(tmp_path):
    configure_logging(enabled=True, log_dir=str(tmp_path), json_format=True)
    try:
        logger = logging.getLogger("inqtrix")
        handler = next(
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        )
        assert isinstance(handler.formatter, InqtrixJsonFormatter)
        assert describe_logging_state()["format"] == "json"
        logger.info("json check")
        handler.flush()
        content = next(tmp_path.glob("inqtrix_*.log")).read_text().strip()
        assert json.loads(content)["message"] == "json check"
    finally:
        configure_logging(enabled=False)


def test_uvicorn_config_switches_formatters_with_the_flag():
    text_config = build_uvicorn_log_config(None)
    assert (
        text_config["formatters"]["default"]["()"]
        == "uvicorn.logging.DefaultFormatter"
    )
    json_config = build_uvicorn_log_config(None, json_format=True)
    for name in ("default", "access", "file"):
        assert (
            json_config["formatters"][name]["()"]
            == "inqtrix.observability.json_formatter.InqtrixJsonFormatter"
        )


def test_read_logging_env_defaults_and_json_flag(monkeypatch):
    for name in (
        "INQTRIX_LOG_ENABLED",
        "INQTRIX_LOG_LEVEL",
        "INQTRIX_LOG_CONSOLE",
        "INQTRIX_LOG_INCLUDE_WEB",
        "INQTRIX_LOG_WEB_LEVEL",
        "INQTRIX_LOG_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)
    env = read_logging_env()
    assert env.enabled is False
    assert env.level == "INFO"
    assert env.console is False
    assert env.include_web is True
    assert env.web_level == "INFO"
    assert env.json_format is False

    monkeypatch.setenv("INQTRIX_LOG_FORMAT", "JSON")
    assert read_logging_env().json_format is True
    monkeypatch.setenv("INQTRIX_LOG_FORMAT", "text")
    assert read_logging_env().json_format is False


def _middleware_app() -> TestClient:
    app = FastAPI()

    @app.get("/probe")
    async def probe():
        from inqtrix.observability.context import current_log_context

        return {"request_id": current_log_context().get("request_id", "")}

    app.add_middleware(RequestContextMiddleware)
    return TestClient(app)


def test_request_id_is_generated_bound_and_echoed():
    client = _middleware_app()
    response = client.get("/probe")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert response.json()["request_id"] == request_id


def test_valid_incoming_request_id_is_honored():
    client = _middleware_app()
    response = client.get("/probe", headers={"X-Request-ID": "abc.DEF_1-2"})
    assert response.headers["X-Request-ID"] == "abc.DEF_1-2"
    assert response.json()["request_id"] == "abc.DEF_1-2"


def test_invalid_incoming_request_id_is_replaced():
    client = _middleware_app()
    response = client.get(
        "/probe", headers={"X-Request-ID": "inject\nX-Evil: 1"}
    )
    replaced = response.headers["X-Request-ID"]
    assert replaced != "inject\nX-Evil: 1"
    assert len(replaced) == 32


def test_redaction_filter_lets_a_record_with_exc_info_false_through():
    # ``exc_info=False`` is a legal logging call and reaches the record as
    # ``False``; the filter unpacked it and the call raised TypeError instead
    # of logging.
    for name in ("inqtrix.test_exc_info_false", "third_party.test_exc_info_false"):
        logger, buffer = _json_capture_logger(name)
        logger.warning("plain warning %s", "kept", exc_info=False)
        line = json.loads(buffer.getvalue().strip())
        assert line["message"] == "plain warning kept"
        assert "exc" not in line
