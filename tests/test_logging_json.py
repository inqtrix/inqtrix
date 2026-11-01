"""JSON log format (the default) and the settings that select it.

The JSON formatter renders one machine-readable object per line from record
attributes only; the log filter stamps the correlation fields and projects
message and exceptions in front of it.
"""

from __future__ import annotations

import io
import json
import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from inqtrix.logging_config import InqtrixLogFilter, LoggingSettings, configure_logging
from inqtrix.observability.context import bind_log_context, reset_log_context
from inqtrix.observability.json_formatter import InqtrixJsonFormatter
from inqtrix.server.request_context import RequestContextMiddleware


def _json_capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """An isolated logger writing JSON lines to a buffer through the filter."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(InqtrixJsonFormatter())
    handler.addFilter(InqtrixLogFilter(service="api"))
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
    assert line["service"] == "api"
    assert line["logger"] == "inqtrix.test_json_fields"
    assert "event" not in line
    assert line["request_id"] == "req-42"
    assert line["run_id"] == "run_abc"
    assert line["user"] == "usr_0123456789abcdef"
    assert line["ts"].endswith("Z")
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
    assert "boom" not in line["exc"]


def test_flat_event_extra_becomes_the_event_field():
    logger, buffer = _json_capture_logger("inqtrix.test_json_event")
    logger.info("Run %s ended", "run_1", extra={"event": "run_end"})
    line = json.loads(buffer.getvalue().strip())
    assert line["event"] == "run_end"
    assert line["message"] == "Run run_1 ended"


def test_text_format_names_level_service_and_logger(tmp_path):
    resolved = configure_logging(
        service="worker", target="file", format="text", log_dir=str(tmp_path)
    )
    logging.getLogger("inqtrix").warning("text check %d", 7)
    for handler in logging.getLogger().handlers:
        handler.flush()

    line = (tmp_path / "inqtrix-worker.log").read_text(encoding="utf-8").strip()
    assert resolved.format == "text"
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \| WARNING \| worker \| inqtrix \| text check 7",
        line,
    )


def test_logging_settings_defaults_and_environment(monkeypatch):
    for name in (
        "INQTRIX_LOG_TARGET",
        "INQTRIX_LOG_LEVEL",
        "INQTRIX_LOG_FORMAT",
        "INQTRIX_LOG_DIR",
        "INQTRIX_LOG_MAX_BYTES",
        "INQTRIX_LOG_BACKUP_COUNT",
        "INQTRIX_LOG_WEB_LEVEL",
        "INQTRIX_LOG_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = LoggingSettings()
    assert settings.target == "console"
    assert settings.level == "INFO"
    assert settings.format == "json"
    assert settings.dir == "logs"
    assert settings.max_bytes == 20 * 1024 * 1024
    assert settings.backup_count == 5
    assert settings.web_level == ""
    assert settings.config == ""

    monkeypatch.setenv("INQTRIX_LOG_FORMAT", "TEXT")
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "debug")
    settings = LoggingSettings()
    assert settings.format == "text"
    assert settings.level == "DEBUG"

    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError):
        LoggingSettings()


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


def test_log_filter_lets_a_record_with_exc_info_false_through():
    # ``exc_info=False`` is a legal logging call and reaches the record as
    # ``False``; the filter unpacked it and the call raised TypeError instead
    # of logging.
    expected = {
        "inqtrix.test_exc_info_false": "plain warning kept",
        "third_party.test_exc_info_false": "plain warning <str>",
    }
    for name, message in expected.items():
        logger, buffer = _json_capture_logger(name)
        logger.warning("plain warning %s", "kept", exc_info=False)
        line = json.loads(buffer.getvalue().strip())
        assert line["message"] == message
        assert "exc" not in line
