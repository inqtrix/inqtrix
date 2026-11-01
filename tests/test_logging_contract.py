"""Process logging contract: one root configuration, one filter, one format.

Every service process configures the root logger once through
``configure_logging(service=...)``. These tests pin what an operator sees on
stdout, stderr and in the log file, and what never appears there: user
content, foreign records below WARNING, client addresses and query strings.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import traceback
import warnings
from logging.handlers import QueueHandler
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from inqtrix import logging_config
from inqtrix.observability.context import bind_log_context, reset_log_context

MARKER = "MARKER-7f3a"
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "logging" / "organization_format.yaml"


def _json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def _configure(**kwargs: Any) -> Any:
    kwargs.setdefault("service", "api")
    return logging_config.configure_logging(**kwargs)


class ReadTimeout(Exception):
    """An exception type whose name does not end in Error, like httpx's."""


# --------------------------------------------------------------------- #
# Configuration and topology
# --------------------------------------------------------------------- #


def test_default_is_json_info_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    logging.getLogger("inqtrix").info("service ready %d", 1)
    logging.getLogger("inqtrix").debug("hidden detail")

    captured = capsys.readouterr()
    lines = _json_lines(captured.out)
    assert [line["message"] for line in lines] == ["service ready 1"]
    assert lines[0]["service"] == "api"
    assert captured.err == ""


@pytest.mark.parametrize("target", ["console", "file", "both", "none"])
@pytest.mark.parametrize("fmt", ["json", "text"])
@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_target_matrix_writes_only_where_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str, fmt: str, level: str
) -> None:
    _configure(target=target, format=fmt, level=level, log_dir=str(tmp_path))
    logger = logging.getLogger("inqtrix")
    logger.debug("matrix line %s", "debug")
    logger.info("matrix line %s", "info")
    logger.warning("matrix line %s", "warning")

    captured = capsys.readouterr()
    log_file = tmp_path / "inqtrix-api.log"
    file_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    shown = {"DEBUG": {"debug", "info", "warning"}, "INFO": {"info", "warning"}, "WARNING": {"warning"}}

    for name in ("debug", "info", "warning"):
        expected = name in shown[level]
        assert (f"matrix line {name}" in captured.out) is (expected and target in {"console", "both"})
        assert (f"matrix line {name}" in file_text) is (expected and target in {"file", "both"})
    assert "matrix line" not in captured.err
    for text in (captured.out, file_text):
        for line in (line for line in text.splitlines() if "matrix line" in line):
            assert line.startswith("{") is (fmt == "json")


def test_none_target_never_falls_back_to_last_resort(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(target="none")
    logging.getLogger("acme.client").error("upstream failed")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_foreign_logger_created_before_configuration_reaches_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    early = logging.getLogger("acme.early")
    _configure()
    early.info("chatty %s", MARKER)
    early.warning("retrying %s", MARKER)

    lines = _json_lines(capsys.readouterr().out)
    assert [line["message"] for line in lines] == ["retrying <str>"]
    assert lines[0]["logger"] == "acme.early"


def test_file_target_rotates_under_a_fixed_name(tmp_path: Path) -> None:
    _configure(target="file", log_dir=str(tmp_path), max_bytes=600, backup_count=2)
    for index in range(60):
        logging.getLogger("inqtrix").info("rotation line %03d", index)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["inqtrix-api.log", "inqtrix-api.log.1", "inqtrix-api.log.2"]


def test_configure_returns_the_resolved_configuration(tmp_path: Path) -> None:
    resolved = _configure(
        service="worker", target="file", level="DEBUG", format="text", log_dir=str(tmp_path)
    )

    assert resolved.service == "worker"
    assert resolved.target == "file"
    assert resolved.level == "DEBUG"
    assert resolved.format == "text"
    assert resolved.file_path == str(tmp_path / "inqtrix-worker.log")


def test_web_level_defaults_to_info_and_refuses_debug() -> None:
    _configure()
    assert logging.getLogger("uvicorn").level == logging.INFO
    assert logging.getLogger("fastapi").level == logging.INFO

    with pytest.raises(ValueError):
        _configure(web_level="DEBUG")


def test_uvicorn_records_below_info_are_always_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(level="DEBUG")
    logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.error").debug("frame %s", MARKER)

    assert MARKER not in capsys.readouterr().out


def test_both_target_projects_exactly_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from inqtrix.auth.log_redaction import stable_pseudonym

    _configure(target="both", log_dir=str(tmp_path))
    tokens = bind_log_context(tenant="tenant-a")
    try:
        try:
            raise ReadTimeout(MARKER)
        except ReadTimeout:
            logging.getLogger("inqtrix").warning(
                "call failed: %s", RuntimeError(MARKER), exc_info=True
            )
    finally:
        reset_log_context(tokens)

    stdout_lines = _json_lines(capsys.readouterr().out)
    file_lines = _json_lines((tmp_path / "inqtrix-api.log").read_text(encoding="utf-8"))
    assert stdout_lines == file_lines
    (line,) = stdout_lines
    assert line["message"] == "call failed: RuntimeError"
    assert "ReadTimeout" in line["exc"]
    assert line["tenant"] == stable_pseudonym("ten", "tenant-a")
    assert MARKER not in json.dumps(line)


def test_the_filter_leaves_only_the_correlation_fields_on_a_record() -> None:
    # Exporters such as the OpenTelemetry handler send every attribute beyond
    # the standard ones, so anything else here reaches the collector.
    record = logging.LogRecord("inqtrix.example", logging.INFO, __file__, 1, "ready", (), None)
    standard = set(vars(record))

    assert logging_config.InqtrixLogFilter(service="api").filter(record) is True

    assert set(vars(record)) - standard - {"message", "asctime"} == {
        "service",
        "request_id",
        "run_id",
        "user",
        "tenant",
        "trace_id",
        "span_id",
    }


def test_a_record_a_queue_handler_copied_is_not_stamped_again() -> None:
    # QueueHandler hands its listener a copy, in a thread without the context
    # of the call, and a configuration of its own puts a filter on both sides.
    from inqtrix.auth.log_redaction import stable_pseudonym

    queue: Queue[logging.LogRecord] = Queue()
    queue_handler = QueueHandler(queue)
    queue_handler.addFilter(logging_config.InqtrixLogFilter(service="api"))
    logger = logging.getLogger("inqtrix.test_queue_copy")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(queue_handler)
    tokens = bind_log_context(tenant="tenant-a", request_id="request-1")
    try:
        logger.info("call failed: %s", RuntimeError(MARKER))
    finally:
        reset_log_context(tokens)
        logger.removeHandler(queue_handler)
        logger.propagate = True

    copied = queue.get_nowait()
    assert logging_config.InqtrixLogFilter(service="api").filter(copied) is True

    assert copied.tenant == stable_pseudonym("ten", "tenant-a")
    assert copied.request_id == "request-1"
    assert copied.getMessage() == "call failed: RuntimeError"


def test_a_dropped_record_stays_dropped_on_every_handler(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(target="both", log_dir=str(tmp_path))
    foreign = logging.getLogger("acme.storage")
    foreign.setLevel(logging.INFO)
    try:
        foreign.info("detail %s", MARKER)
    finally:
        foreign.setLevel(logging.NOTSET)

    assert capsys.readouterr().out == ""
    assert (tmp_path / "inqtrix-api.log").read_text(encoding="utf-8") == ""


def test_correlation_names_in_extra_do_not_replace_the_projection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inqtrix.auth.log_redaction import stable_pseudonym

    _configure()
    logging.getLogger("inqtrix").warning(
        "call failed: %s",
        RuntimeError(MARKER),
        extra={"service": "spoofed", "tenant": "tenant-a", "request_id": "request-1"},
    )

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["message"] == "call failed: RuntimeError"
    assert line["service"] == "api"
    assert line["tenant"] == stable_pseudonym("ten", "tenant-a")
    assert line["request_id"] == "request-1"
    assert MARKER not in json.dumps(line)


def test_values_resolve_code_then_environment_then_dotenv_then_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "INQTRIX_LOG_LEVEL=ERROR\nINQTRIX_LOG_FORMAT=text\nINQTRIX_LOG_WEB_LEVEL=ERROR\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(logging_config.LoggingSettings.model_config, "env_file", str(dotenv))
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("INQTRIX_LOG_FORMAT", "json")

    resolved = _configure(level="WARNING", target="none")

    assert resolved.level == "WARNING"
    assert resolved.format == "json"
    assert resolved.web_level == "ERROR"
    assert resolved.target == "none"


def test_status_line_reports_service_target_level_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    resolved = _configure(service="worker", target="both", log_dir=str(tmp_path))
    logging_config.log_status_line(resolved)

    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    message = lines[0]["message"]
    for part in ("service=worker", "target=both", "level=INFO", "format=json"):
        assert part in message
    assert str(tmp_path / "inqtrix-worker.log") in message


def test_status_line_names_a_configuration_passed_in_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolved = _configure(config=_org_config())
    logging_config.log_status_line(resolved)

    out = capsys.readouterr().out
    assert "format=custom" in out
    assert "config=code " in out


# --------------------------------------------------------------------- #
# Field contract
# --------------------------------------------------------------------- #


def test_json_fields_follow_the_contract(capsys: pytest.CaptureFixture[str]) -> None:
    from inqtrix.auth.log_redaction import stable_pseudonym

    _configure()
    tokens = bind_log_context(
        request_id="req-1", run_id="run_1", user="usr_abc", tenant="tenant-a"
    )
    try:
        logging.getLogger("inqtrix").info("hello %s", 3, extra={"event": "evt.x"})
    finally:
        reset_log_context(tokens)

    (line,) = _json_lines(capsys.readouterr().out)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", line["ts"])
    assert line["level"] == "INFO"
    assert line["service"] == "api"
    assert line["logger"] == "inqtrix"
    assert line["module"] == "test_logging_contract"
    assert line["function"] == "test_json_fields_follow_the_contract"
    assert isinstance(line["line"], int)
    assert line["message"] == "hello 3"
    assert line["event"] == "evt.x"
    assert line["request_id"] == "req-1"
    assert line["run_id"] == "run_1"
    assert line["user"] == "usr_abc"
    assert line["tenant"] == stable_pseudonym("ten", "tenant-a")
    assert line["tenant"] != "tenant-a"
    assert line["thread"]
    assert "workspace" not in line


def test_json_omits_unset_event_and_unbound_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("inqtrix").info("plain")

    (line,) = _json_lines(capsys.readouterr().out)
    for key in ("event", "request_id", "run_id", "user", "tenant", "workspace"):
        assert key not in line


def test_workspace_is_no_longer_a_log_field() -> None:
    with pytest.raises(KeyError):
        bind_log_context(workspace="ws_1")


def test_record_keeps_its_request_id_when_formatted_in_another_thread() -> None:
    from inqtrix.observability.json_formatter import InqtrixJsonFormatter

    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep()
    handler.addFilter(logging_config.InqtrixLogFilter(service="api"))
    logger = logging.getLogger("inqtrix")
    logger.addHandler(handler)
    tokens = bind_log_context(request_id="req-thread")
    try:
        logger.warning("queued line")
    finally:
        reset_log_context(tokens)
        logger.removeHandler(handler)

    rendered: list[str] = []
    worker = threading.Thread(
        target=lambda: rendered.append(InqtrixJsonFormatter().format(records[0]))
    )
    worker.start()
    worker.join()
    assert json.loads(rendered[0])["request_id"] == "req-thread"


def test_formatter_ignores_the_removed_structured_payload_path() -> None:
    from inqtrix.observability.json_formatter import InqtrixJsonFormatter

    record = logging.LogRecord("inqtrix", logging.INFO, __file__, 1, "plain", (), None)
    record.inqtrix_event = "legacy.event"
    record.inqtrix_payload = {"legacy_key": MARKER}
    line = json.loads(InqtrixJsonFormatter().format(record))

    assert "legacy_key" not in line
    assert "payload_legacy_key" not in line
    assert line.get("event") != "legacy.event"


# --------------------------------------------------------------------- #
# Content rule and foreign loggers
# --------------------------------------------------------------------- #


def test_foreign_warning_arguments_become_type_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("acme.client").warning(
        "call %s failed after %d tries (%s)", MARKER, 3, True
    )

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["message"] == "call <str> failed after 3 tries (True)"


def test_foreign_mapping_argument_is_projected_per_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("acme.client").warning(
        "%(name)s=%(n)d", {"name": MARKER, "n": 5}
    )

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["message"] == "<str>=5"


def test_foreign_error_whose_message_is_an_exception_shows_type_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("langchain_core.tracers.langchain").error(
        ValueError(f"{MARKER} prompt text")
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["message"] == "ValueError"


def test_foreign_exception_info_keeps_type_and_frames_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    try:
        raise RuntimeError(f"{MARKER} provider body")
    except RuntimeError:
        logging.getLogger("acme.client").error("request failed", exc_info=True)

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert "RuntimeError" in line["exc"]
    assert "test_logging_contract.py" in line["exc"]


def test_openai_debug_body_never_reaches_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    _configure(level="DEBUG")
    logging.getLogger("openai").setLevel(logging.DEBUG)
    logging.getLogger("openai._base_client").debug(
        "Request options: %s", {"json_data": {"messages": MARKER}}
    )

    # Not even a projected line: foreign records below WARNING never appear.
    out = capsys.readouterr().out
    assert MARKER not in out
    assert _json_lines(out) == []


def test_openai_debug_set_in_a_custom_configuration_never_appears(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(config=_org_config(openai={"level": "DEBUG"}))
    logging.getLogger("openai._base_client").debug(
        "Request options: %s", {"json_data": {"messages": MARKER}}
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    assert not any(line.startswith("ORG DEBUG openai") for line in out.splitlines())


def test_preformatted_foreign_text_is_kept_with_secrets_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Library-formatted text stays; known secret shapes are scrubbed."""

    _configure()
    token = "abcdefghijklmnop1234567890"
    task_name = f"node-{MARKER}"
    logging.getLogger("langgraph.pregel").warning(
        f"Task {task_name} wrote to unknown channel Bearer {token}"
    )
    error = RuntimeError(f"{MARKER} Authorization: Bearer {token}")
    logging.getLogger("langchain_core.language_models.llms").warning(
        f"Error in on_retry: {error}"
    )

    out = capsys.readouterr().out
    lines = _json_lines(out)
    assert len(lines) == 2
    assert f"node-{MARKER}" in lines[0]["message"]
    assert MARKER in lines[1]["message"]
    assert token not in out


def _raise_with_a_marker_in_the_source() -> None:
    raise RuntimeError("SOURCE-LINE-5c1d " + MARKER)


@pytest.mark.parametrize("logger_name", ["uvicorn.error", "acme.server"])
def test_traceback_in_the_message_text_keeps_types_and_frames_only(
    capsys: pytest.CaptureFixture[str], logger_name: str
) -> None:
    """Like the startup failure text Starlette hands to uvicorn's logger."""

    _configure()
    try:
        _raise_with_a_marker_in_the_source()
    except RuntimeError:
        text = "Application startup failed.\n" + traceback.format_exc()
    logging.getLogger(logger_name).error(text)

    out = capsys.readouterr().out
    assert MARKER not in out
    assert "SOURCE-LINE-5c1d" not in out
    (line,) = _json_lines(out)
    assert line["message"].startswith(
        "Application startup failed.\nTraceback (most recent call last):"
    )
    assert "_raise_with_a_marker_in_the_source" in line["message"]
    assert line["message"].splitlines()[-1] == "RuntimeError"


def test_a_failing_context_lookup_never_raises_into_the_caller(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure()

    def _broken() -> dict[str, Any]:
        raise RuntimeError(MARKER)

    monkeypatch.setattr(logging_config, "current_log_context", _broken)
    logging.getLogger("inqtrix").warning("still logged %d", 1)

    out = capsys.readouterr().out
    (line,) = _json_lines(out)
    assert line["message"] == "still logged 1"
    assert line["service"] == "api"
    assert MARKER not in out


def test_malformed_log_calls_never_raise_into_the_caller(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("acme.bad").warning("%d items", "not-a-number")
    logging.getLogger("inqtrix").warning("%d items", "not-a-number")

    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 2
    assert "acme.bad" in lines[0]["message"]
    assert "inqtrix" in lines[1]["message"]
    assert all("not-a-number" not in line["message"] for line in lines)


def test_inqtrix_exception_argument_is_projected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("inqtrix").warning("x %s", RuntimeError(MARKER))

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["message"] == "x RuntimeError"


def test_access_line_drops_client_address_and_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.9:5555",
        "GET",
        f"/v1/items?token={MARKER}&q=search",
        "1.1",
        200,
    )

    out = capsys.readouterr().out
    assert "203.0.113.9" not in out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["message"] == '- - "GET /v1/items HTTP/1.1" 200'


def test_websocket_line_drops_client_address_and_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s"', "203.0.113.9:5555", f"/ws/doc?token={MARKER}"
    )

    out = capsys.readouterr().out
    assert "203.0.113.9" not in out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["message"] == '- - "WebSocket /ws/doc"'


def test_asyncio_keeps_only_the_first_message_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    try:
        raise ValueError(f"{MARKER} task failure")
    except ValueError:
        logging.getLogger("asyncio").error(
            f"Task exception was never retrieved\nfuture: <Task coro=<job() {MARKER}>>",
            exc_info=True,
        )

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["message"] == "Task exception was never retrieved"
    assert "ValueError" in line["exc"]


def test_asyncio_keeps_its_own_words_without_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # asyncio writes the repr of a callback and its arguments into the
    # first line; only its own words before the first value remain.
    _configure()
    logging.getLogger("asyncio").error(
        f"Exception in callback cb('{MARKER}')\nhandle: <Handle cb('{MARKER}')>"
    )
    logging.getLogger("asyncio").warning(
        f"Executing <Handle cb('{MARKER}')> took 0.250 seconds"
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    assert [line["message"] for line in _json_lines(out)] == [
        "Exception in callback cb",
        "Executing",
    ]


def test_python_warnings_keep_category_and_location_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    warnings.warn(f"{MARKER} deprecated call", UserWarning, stacklevel=1)

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["logger"] == "py.warnings"
    assert "UserWarning" in line["message"]
    assert "test_logging_contract.py:" in line["message"]


def test_host_filter_without_service_keeps_host_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host that attaches the filter itself keeps its own policy."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    handler.addFilter(logging_config.InqtrixLogFilter())
    host = logging.getLogger("hostapp.views")
    host.addHandler(handler)
    host.setLevel(logging.INFO)
    product = logging.getLogger("inqtrix")
    product.addHandler(handler)
    product.setLevel(logging.INFO)
    try:
        host.info("user %s signed in", "alice")
        product.warning("failed: %s", RuntimeError(MARKER))
    finally:
        host.removeHandler(handler)
        product.removeHandler(handler)

    out = capsys.readouterr().out
    assert "hostapp.views user alice signed in" in out
    assert "inqtrix failed: RuntimeError" in out
    assert MARKER not in out


@pytest.mark.parametrize("target", ["console", "none"])
def test_otlp_export_failures_appear_once_and_projected(
    capsys: pytest.CaptureFixture[str], target: str
) -> None:
    from inqtrix.observability.otel import build_tracer_provider
    from inqtrix.settings import ObservabilitySettings, Settings

    _configure(target=target)
    provider = build_tracer_provider(
        Settings(observability=ObservabilitySettings(tracing="otlp")), service_role="api"
    )
    try:
        logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").warning(
            "Failed to export batch code: %s, reason: %s", 401, f"{MARKER} body"
        )
    finally:
        if provider is not None:
            provider.shutdown()

    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    exported = [
        line["message"] for line in _json_lines(captured.out) if "Failed to export" in line["message"]
    ]
    if target == "none":
        assert exported == []
        assert "Failed to export" not in captured.err
    else:
        assert exported == ["Failed to export batch code: 401, reason: <str>"]


def test_project_exception_output_is_unchanged() -> None:
    class _CodedError(RuntimeError):
        code = "E42"

    def _fail() -> None:
        raise _CodedError(f"{MARKER} message")

    try:
        _fail()
    except _CodedError as exc:
        descriptor, rendered = logging_config.project_exception(exc)

    assert descriptor == "_CodedError[code=E42]"
    lines = rendered.splitlines()
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[-1] == "_CodedError[code=E42]"
    assert all(line.startswith('  File "') for line in lines[1:-1])
    assert MARKER not in rendered


def test_filter_is_exported_from_the_package() -> None:
    import inqtrix

    assert inqtrix.InqtrixLogFilter is logging_config.InqtrixLogFilter
    assert "InqtrixLogFilter" in inqtrix.__all__


# --------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------- #


def _exc_info(message: str) -> tuple[type[BaseException], BaseException, Any]:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return RuntimeError, exc, exc.__traceback__


def test_unhandled_exception_is_logged_critical_and_projected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    sys.excepthook(*_exc_info(f"{MARKER} startup failure"))

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["level"] == "CRITICAL"
    assert "RuntimeError" in line["exc"]


def test_unhandled_thread_exception_is_logged_as_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    exc_type, exc, tb = _exc_info(f"{MARKER} thread failure")
    threading.excepthook(
        SimpleNamespace(exc_type=exc_type, exc_value=exc, exc_traceback=tb, thread=None)
    )

    out = capsys.readouterr().out
    assert MARKER not in out
    (line,) = _json_lines(out)
    assert line["level"] == "ERROR"
    assert "RuntimeError" in line["exc"]


def test_none_target_still_shows_a_projected_crash_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(target="none")
    sys.excepthook(*_exc_info(f"{MARKER} startup failure"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert MARKER not in captured.err
    assert "RuntimeError" in captured.err
    assert "test_logging_contract.py" in captured.err


# --------------------------------------------------------------------- #
# Organisation format
# --------------------------------------------------------------------- #


def test_example_file_and_config_dict_give_the_same_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_CONFIG", str(EXAMPLE_CONFIG))
    resolved = _configure()
    logging.getLogger("inqtrix").info("custom line")
    from_file = capsys.readouterr().out
    monkeypatch.delenv("INQTRIX_LOG_CONFIG")

    _configure(config=yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    logging.getLogger("inqtrix").info("custom line")
    from_dict = capsys.readouterr().out

    assert resolved.format == "custom"
    assert "custom line" in from_file
    assert not from_file.startswith("{")
    strip = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d[.,]\d{3}Z?")
    assert strip.sub("<ts>", from_file) == strip.sub("<ts>", from_dict)


def _org_config(**loggers: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "formatters": {"org": {"format": "ORG %(levelname)s %(name)s %(message)s"}},
        "handlers": {
            "out": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "org"}
        },
        "root": {"level": "WARNING", "handlers": ["out"]},
        "loggers": {"inqtrix": {"level": "INFO"}, **loggers},
    }


def test_custom_handlers_get_the_filter(capsys: pytest.CaptureFixture[str]) -> None:
    _configure(config=_org_config())
    logging.getLogger("inqtrix").error("call failed: %s", RuntimeError(MARKER))

    out = capsys.readouterr().out
    assert "ORG ERROR inqtrix call failed: RuntimeError" in out
    assert MARKER not in out


def test_a_filter_declared_in_the_configuration_runs_with_the_service(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _org_config(acme={"level": "INFO"})
    config["filters"] = {"mine": {"()": "inqtrix.InqtrixLogFilter"}}
    config["handlers"]["out"]["filters"] = ["mine"]
    config["formatters"]["org"]["format"] = "ORG %(service)s %(levelname)s %(name)s %(message)s"
    _configure(service="worker", config=config)
    logging.getLogger("acme").info("chatty %s", MARKER)
    logging.getLogger("acme").warning("retry %s", MARKER)

    out = capsys.readouterr().out
    assert MARKER not in out
    assert "ORG worker WARNING acme retry <str>" in out
    assert "ORG worker INFO acme" not in out


@pytest.mark.parametrize("custom", [False, True])
def test_a_root_handler_a_library_attached_earlier_does_not_survive(
    capsys: pytest.CaptureFixture[str], custom: bool
) -> None:
    """Like openai with OPENAI_LOG=debug, which calls logging.basicConfig."""

    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    if custom:
        _configure(
            config={
                "version": 1,
                "handlers": {
                    "out": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"}
                },
                "loggers": {"inqtrix": {"level": "INFO", "handlers": ["out"]}},
            }
        )
    else:
        _configure()
    logging.getLogger("openai").setLevel(logging.DEBUG)
    logging.getLogger("openai._base_client").debug(
        "Request options: %s", {"json_data": MARKER}
    )
    logging.getLogger("acme").warning("failed: %s", RuntimeError(MARKER))

    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    assert captured.err == ""


def test_a_custom_root_without_handlers_never_falls_back_to_last_resort(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(
        config={
            "version": 1,
            "root": {"level": "WARNING"},
            "handlers": {"out": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"}},
            "loggers": {"inqtrix": {"level": "INFO", "handlers": ["out"]}},
        }
    )
    logging.getLogger("acme").error("upstream failed: %s", RuntimeError(MARKER))

    captured = capsys.readouterr()
    assert captured.err == ""
    assert MARKER not in captured.out


def test_custom_configuration_conflict_names_the_variable_and_the_keyword(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError) as error:
        _configure(config=_org_config(), log_dir=str(tmp_path))

    assert "INQTRIX_LOG_DIR" in str(error.value)
    assert "log_dir" in str(error.value)


def test_an_empty_logging_variable_does_not_conflict_with_a_custom_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INQTRIX_LOG_WEB_LEVEL", "")

    resolved = _configure(config=_org_config())

    assert resolved.format == "custom"


@pytest.mark.parametrize(
    "variable",
    [
        "INQTRIX_LOG_TARGET",
        "INQTRIX_LOG_LEVEL",
        "INQTRIX_LOG_FORMAT",
        "INQTRIX_LOG_MAX_BYTES",
        "INQTRIX_LOG_BACKUP_COUNT",
    ],
)
def test_an_empty_logging_variable_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # Compose passes ``${INQTRIX_LOG_LEVEL:-}`` and env files carry
    # ``INQTRIX_LOG_LEVEL=`` lines: an empty value keeps the default.
    monkeypatch.setenv(variable, "")

    resolved = _configure(service="migrate")

    assert (resolved.target, resolved.level, resolved.format) == ("console", "INFO", "json")


def test_empty_logging_variables_do_not_conflict_with_a_custom_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("INQTRIX_LOG_LEVEL", "INQTRIX_LOG_FORMAT"):
        monkeypatch.setenv(variable, "")

    resolved = _configure(service="migrate", config=_org_config())

    assert resolved.format == "custom"


def test_custom_configuration_is_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INQTRIX_LOG_LEVEL", "DEBUG")
    with pytest.raises(ValueError, match="INQTRIX_LOG_LEVEL"):
        _configure(config=_org_config())
    monkeypatch.delenv("INQTRIX_LOG_LEVEL")
    with pytest.raises(ValueError, match="level"):
        _configure(config=_org_config(), level="DEBUG")


def test_custom_configuration_cannot_open_uvicorn_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(config=_org_config(uvicorn={"level": "DEBUG"}))
    logging.getLogger("uvicorn.error").debug("frame %s", MARKER)

    assert MARKER not in capsys.readouterr().out


def test_foreign_logger_below_warning_in_custom_config_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(config=_org_config(httpx={"level": "INFO"}))
    logging.getLogger("httpx").info("GET %s", MARKER)
    logging.getLogger("httpx").warning("retry %s", MARKER)

    out = capsys.readouterr().out
    assert MARKER not in out
    assert not any(line.startswith("ORG INFO httpx") for line in out.splitlines())
    notices = [line for line in out.splitlines() if "httpx" in line and "WARNING" in line]
    assert any("ORG WARNING httpx retry <str>" == line for line in notices)
    assert sum(1 for line in out.splitlines() if line.startswith("ORG WARNING inqtrix") and "httpx" in line) == 1


def test_custom_text_formatter_can_print_the_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _org_config()
    config["formatters"]["org"] = {"format": "%(request_id)s|%(service)s|%(message)s"}
    _configure(config=config)
    tokens = bind_log_context(request_id="rq-9")
    try:
        logging.getLogger("inqtrix").info("hello")
    finally:
        reset_log_context(tokens)

    assert "rq-9|api|hello" in capsys.readouterr().out


def test_unknown_inqtrix_logger_names_are_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(
        config=_org_config(
            **{"inqtrix.agnets": {"level": "DEBUG"}, "inqtrix.providers": {"level": "DEBUG"}}
        )
    )

    out = capsys.readouterr().out
    assert sum(1 for line in out.splitlines() if "inqtrix.agnets" in line) == 1
    assert "inqtrix.providers" not in out


def test_a_level_set_for_one_area_reaches_only_its_modules(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inqtrix.agents import planner
    from inqtrix.providers import bedrock

    _configure(config=_org_config(**{"inqtrix.providers": {"level": "DEBUG"}}))
    bedrock.log.debug("adapter detail %d", 1)
    planner.log.debug("planner detail %d", 2)

    out = capsys.readouterr().out
    assert "ORG DEBUG inqtrix.providers.bedrock adapter detail 1" in out
    assert "planner detail" not in out


def test_custom_configuration_keeps_existing_loggers_enabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    early = logging.getLogger("acme.early.lib")
    _configure(config=_org_config())
    early.warning("still alive")

    assert "ORG WARNING acme.early.lib still alive" in capsys.readouterr().out


# --------------------------------------------------------------------- #
# Migration job
# --------------------------------------------------------------------- #


def test_migration_job_shows_alembic_progress_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(service="migrate")
    logging.getLogger("alembic.runtime.migration").info(
        "Running upgrade %s -> %s, %s", "0095_alpha", "0096_beta", "add things"
    )
    logging.getLogger("httpx").info("HTTP Request: GET %s", MARKER)

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = _json_lines(captured.err)
    assert [line["message"] for line in lines] == [
        "Running upgrade 0095_alpha -> 0096_beta, add things"
    ]
    assert lines[0]["service"] == "migrate"


# --------------------------------------------------------------------- #
# Factories and entry points
# --------------------------------------------------------------------- #


class _LLM:
    def complete(self, *args: Any, **kwargs: Any) -> str:
        return "ok"

    def is_available(self) -> bool:
        return True


class _Search:
    def search(self, *args: Any, **kwargs: Any) -> Any:
        from inqtrix.search_result import GroundedSearchResult

        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


def _create_app(**auth: Any) -> Any:
    from inqtrix.providers.base import ProviderContext
    from inqtrix.server import create_app
    from inqtrix.settings import AuthSettings, Settings

    settings = Settings(auth=AuthSettings(**auth)) if auth else Settings()
    return create_app(settings=settings, providers=ProviderContext(llm=_LLM(), search=_Search()))


def _create_multi_stack_app(**auth: Any) -> Any:
    from inqtrix.providers.base import ProviderContext
    from inqtrix.server.stacks import StackBundle, create_multi_stack_app
    from inqtrix.settings import AuthSettings, Settings

    settings = Settings(auth=AuthSettings(**auth)) if auth else Settings()
    stacks = {"main": StackBundle(providers=ProviderContext(llm=_LLM(), search=_Search()))}
    return create_multi_stack_app(settings=settings, stacks=stacks, default_stack="main")


@pytest.mark.parametrize("factory", [_create_app, _create_multi_stack_app])
def test_app_factories_leave_the_process_logging_alone(factory: Any) -> None:
    root = logging.getLogger()
    product = logging.getLogger("inqtrix")
    before = (list(root.handlers), root.level, list(product.handlers), product.level, product.propagate)

    factory()

    assert (list(root.handlers), root.level, list(product.handlers), product.level, product.propagate) == before


@pytest.mark.parametrize("factory", [_create_app, _create_multi_stack_app])
def test_app_factories_name_a_placeholder_secret_at_startup(
    factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        factory(session_secret="CHANGE_ME_SESSION_SECRET", pat_pepper="not-a-placeholder")

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("INSECURE:")
    ]
    assert len(warnings) == 1
    assert "1 secret(s)" in warnings[0]
    assert "(INQTRIX_SESSION_SECRET)" in warnings[0]
    assert "CHANGE_ME_SESSION_SECRET" not in caplog.text


def test_create_app_startup_warning_reaches_the_configured_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from inqtrix.auth import log_redaction

    monkeypatch.setattr(log_redaction, "_fallback_warned", False)
    monkeypatch.setattr(log_redaction, "_stable_key", None)
    _configure()
    _create_app(pseudonym_pepper="")

    lines = _json_lines(capsys.readouterr().out)
    assert any("INQTRIX_PSEUDONYM_PEPPER is unset" in line["message"] for line in lines)


@pytest.mark.parametrize("factory", [_create_app, _create_multi_stack_app])
def test_database_gate_rejection_carries_a_request_id(factory: Any) -> None:
    app = factory()
    app.state.database_contract_ready = False
    with TestClient(app) as client:
        client.app.state.database_contract_ready = False
        response = client.get("/v1/models")

    assert response.status_code == 503
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers.get("X-Request-ID", ""))


def test_multi_stack_app_trusts_the_configured_proxy_hops() -> None:
    from inqtrix.auth.request_origin import current_request_origin

    app = _create_multi_stack_app(trusted_proxy_hops=2)

    @app.get("/__origin")
    def _origin() -> dict[str, str]:
        return current_request_origin()

    with TestClient(app) as client:
        response = client.get(
            "/__origin", headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.2"}
        )

    assert response.json()["ip"] == "198.51.100.7"


def test_multi_stack_app_installs_the_pseudonym_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.auth import log_redaction

    monkeypatch.setattr(log_redaction, "_stable_key", None)
    monkeypatch.setattr(log_redaction, "_fallback_warned", True)
    log_redaction.configure_stable_pseudonyms("shared-pepper-for-test")
    expected = log_redaction.stable_pseudonym("usr", "user-1")
    log_redaction.configure_stable_pseudonyms("")

    _create_multi_stack_app(pseudonym_pepper="shared-pepper-for-test")

    assert log_redaction.stable_pseudonyms_active()
    assert log_redaction.stable_pseudonym("usr", "user-1") == expected


def test_api_main_configures_logging_before_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inqtrix.__main__ as server_main

    calls: list[str] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        server_main,
        "configure_logging",
        lambda **kwargs: calls.append(f"configure:{kwargs.get('service')}")
        or SimpleNamespace(service="api", target="console", level="INFO", format="json", file_path=None),
    )
    monkeypatch.setattr(server_main, "log_status_line", lambda *a, **k: calls.append("status"))
    monkeypatch.setattr(server_main, "check_retired_settings", lambda **k: calls.append("retired") or [])

    def _settings() -> Any:
        calls.append("settings")
        return SimpleNamespace(collaboration=SimpleNamespace(max_frame_bytes=1, max_queued_frames=2))

    monkeypatch.setattr(server_main, "Settings", _settings)
    monkeypatch.setattr(server_main, "create_app", lambda settings: calls.append("create_app") or "app")
    monkeypatch.setattr(
        server_main.uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, options=kwargs),
    )

    server_main.main()

    assert calls == ["configure:api", "retired", "status", "settings", "create_app"]
    assert captured["app"] == "app"
    assert captured["options"]["log_config"] is None
    assert "log_level" not in captured["options"]


def test_importing_the_api_entry_point_configures_nothing() -> None:
    import importlib

    import inqtrix.__main__ as server_main

    root = logging.getLogger()
    before = list(root.handlers)
    importlib.reload(server_main)

    assert list(root.handlers) == before
    assert not hasattr(server_main, "app")


def test_worker_main_configures_logging_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import inqtrix.worker.__main__ as worker_main

    calls: list[str] = []
    monkeypatch.setattr(
        worker_main,
        "configure_logging",
        lambda **kwargs: calls.append(f"configure:{kwargs.get('service')}")
        or SimpleNamespace(service="worker", target="console", level="INFO", format="json", file_path=None),
    )
    monkeypatch.setattr(worker_main, "log_status_line", lambda *a, **k: calls.append("status"))
    monkeypatch.setattr(worker_main, "check_retired_settings", lambda **k: calls.append("retired") or [])

    class _Stop(Exception):
        pass

    def _settings() -> Any:
        calls.append("settings")
        raise _Stop

    monkeypatch.setattr(worker_main, "Settings", _settings)
    with pytest.raises(_Stop):
        worker_main.main()

    assert calls == ["configure:worker", "retired", "status", "settings"]


def test_migration_main_configures_logging_instead_of_basic_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.storage import migrate

    calls: list[str] = []
    monkeypatch.setattr(
        migrate,
        "configure_logging",
        lambda **kwargs: calls.append(f"configure:{kwargs.get('service')}")
        or SimpleNamespace(service="migrate", target="console", level="INFO", format="json", file_path=None),
    )
    monkeypatch.setattr(migrate, "check_retired_settings", lambda **k: calls.append("retired") or [])
    monkeypatch.setattr(migrate, "log_status_line", lambda *a, **k: calls.append("status"))
    monkeypatch.setattr(
        logging, "basicConfig", lambda *a, **k: calls.append("basicConfig")
    )
    monkeypatch.setattr(sys, "argv", ["inqtrix-migrate", "--database-url", ""])
    monkeypatch.setenv("INQTRIX_DATABASE_URL", "")
    monkeypatch.setenv("INQTRIX_MIGRATION_DATABASE_URL", "")

    with pytest.raises(SystemExit):
        migrate.main()

    assert calls[:3] == ["configure:migrate", "retired", "status"]
    assert "basicConfig" not in calls


_WEBSERVER_EXAMPLES = (
    "anthropic_perplexity",
    "azure_foundry_web_search",
    "azure_knowledge_quickstart",
    "azure_openai_perplexity",
    "bedrock_perplexity",
    "litellm_perplexity",
    "multi_stack",
)


@pytest.mark.parametrize("name", _WEBSERVER_EXAMPLES)
def test_webserver_example_main_configures_logging_like_the_api(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import importlib

    module = importlib.import_module(f"examples.webserver_stacks.{name}")
    calls: list[str] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **k: calls.append("dotenv"))
    monkeypatch.setattr(
        module,
        "configure_logging",
        lambda **kwargs: calls.append(f"configure:{kwargs.get('service')}") or SimpleNamespace(),
    )
    monkeypatch.setattr(module, "check_retired_settings", lambda **k: calls.append("retired") or [])
    monkeypatch.setattr(module, "log_status_line", lambda *a, **k: calls.append("status"))
    monkeypatch.setattr(module, "build_app", lambda: calls.append("build_app") or "app")
    monkeypatch.setattr(module, "_build_settings", lambda: SimpleNamespace(server=None))
    monkeypatch.setattr(module, "resolve_tls_paths", lambda server: None)
    monkeypatch.setattr(
        module.uvicorn, "run", lambda app, **kwargs: captured.update(app=app, options=kwargs)
    )

    module.main()

    assert calls == ["dotenv", "configure:api", "retired", "status", "build_app"]
    assert captured["app"] == "app"
    assert captured["options"]["log_config"] is None


# --------------------------------------------------------------------- #
# Real process starts
# --------------------------------------------------------------------- #


def _process_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }
    env.update(extra)
    return env


def test_child_processes_of_these_tests_write_no_bytecode(tmp_path: Path) -> None:
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.dont_write_bytecode)"],
        env=_process_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert completed.stdout.strip() == "True"


def test_child_processes_of_these_tests_load_no_dotenv_file(tmp_path: Path) -> None:
    from tests._hermetic import dotenv_probe_after_magika

    (tmp_path / ".env").write_text("ZZ_DOTENV_PROBE=loaded\n", encoding="utf-8")
    child = _process_env(tmp_path)
    without_switch = {name: value for name, value in child.items() if name != "PYTHON_DOTENV_DISABLED"}

    assert dotenv_probe_after_magika(tmp_path, without_switch) == "loaded"
    assert dotenv_probe_after_magika(tmp_path, child) == ""


def test_worker_process_reports_status_and_a_projected_crash(tmp_path: Path) -> None:
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-m", "inqtrix.worker"],
        cwd=tmp_path,
        env=_process_env(tmp_path, INQTRIX_STORAGE_BACKEND="memory"),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode != 0
    lines = _json_lines(completed.stdout)
    status = [line for line in lines if line["message"].startswith("Logging:")]
    assert len(status) == 1
    assert "service=worker" in status[0]["message"]
    crashes = [line for line in lines if line["level"] == "CRITICAL"]
    assert len(crashes) == 1
    assert "RuntimeError" in crashes[0]["exc"]
    # The operator learns what to fix: the worker's own diagnosis survives.
    assert "requires INQTRIX_STORAGE_BACKEND=postgres" in crashes[0]["message"]
    assert all(line["service"] == "worker" for line in lines)
    assert completed.stderr == ""


def test_migration_job_never_shows_the_password_of_its_database_url(tmp_path: Path) -> None:
    import subprocess

    # Port 9 refuses the connection: the job stops, and the password in its
    # URL appears in none of its output.
    completed = subprocess.run(
        [sys.executable, "-c", "from inqtrix.storage.migrate import main; main()"],
        cwd=tmp_path,
        env=_process_env(
            tmp_path,
            INQTRIX_DATABASE_URL=f"postgresql+asyncpg://inqtrix:pw{MARKER}@127.0.0.1:9/inqtrix",
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 1
    assert MARKER not in completed.stdout + completed.stderr
    crashes = [line for line in _json_lines(completed.stderr) if line["level"] == "CRITICAL"]
    assert [line["message"] for line in crashes] == [
        "Unhandled exception; the process stops: ConnectionRefusedError[errno=111]"
    ]


def test_api_process_logs_status_and_uvicorn_lines_as_json(tmp_path: Path) -> None:
    import socket
    import subprocess
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "inqtrix"],
        cwd=tmp_path,
        env=_process_env(
            tmp_path,
            INQTRIX_SERVER_HOST="127.0.0.1",
            INQTRIX_SERVER_PORT=str(port),
            INQTRIX_LOG_TARGET="both",
            INQTRIX_LOG_DIR=str(tmp_path / "logs"),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    collected: list[str] = []
    reader = threading.Thread(
        target=lambda: collected.extend(iter(process.stdout.readline, "")), daemon=True
    )
    reader.start()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any("Uvicorn running on" in line for line in collected):
                break
            if process.poll() is not None:
                break
            time.sleep(0.2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        reader.join(timeout=5)

    lines = _json_lines("".join(collected))
    messages = [line["message"] for line in lines]
    assert any(message.startswith("Logging:") and "service=api" in message for message in messages)
    assert any("Uvicorn running on" in message for message in messages)
    assert all(line["service"] == "api" for line in lines)
    assert (tmp_path / "logs" / "inqtrix-api.log").exists()
