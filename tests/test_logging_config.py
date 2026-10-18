"""Tests for centralized logging configuration."""

from __future__ import annotations

import ast
import io
import logging
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from inqtrix.logging_config import (
    _RedactSecretsFilter,
    _WEB_LOGGER_NAMES,
    build_uvicorn_log_config,
    configure_logging,
    describe_logging_state,
    format_logging_banner,
    is_configured,
    print_logging_banner,
)


def test_console_handler_works_without_file_logging(capsys):
    log_path = configure_logging(enabled=False, level="WARNING", console=True)

    assert log_path is None

    logger = logging.getLogger("inqtrix")
    logger.warning("visible warning")

    captured = capsys.readouterr()
    assert "visible warning" in captured.err
    assert all(not isinstance(handler, logging.NullHandler) for handler in logger.handlers)


def test_reconfigure_closes_previous_file_handler(tmp_path):
    log_dir = tmp_path / "logs"

    configure_logging(enabled=True, log_dir=str(log_dir))
    logger = logging.getLogger("inqtrix")
    first_handler = next(
        handler for handler in logger.handlers if isinstance(handler, logging.FileHandler)
    )
    first_stream = first_handler.stream

    configure_logging(enabled=True, log_dir=str(log_dir))

    assert first_stream is not None
    assert first_stream.closed is True


def test_mapping_style_logging_is_preserved_and_redacted(tmp_path, capsys):
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")

    # Realistic-length API key so the {16,} guard in sanitize_log_message fires.
    logger.info(
        "%(user)s %(token)s",
        {"user": "alice", "token": "sk-secrettoken1234567890abcdef"},
    )
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    captured = capsys.readouterr()
    content = Path(log_path).read_text(encoding="utf-8")

    assert "Logging error" not in captured.err
    assert "alice" in content
    assert "sk-secrettoken1234567890abcdef" not in content
    assert "[KEY]" in content


class _CodedLogError(RuntimeError):
    error_code = "provider_timeout"


def test_log_filter_removes_exception_message_without_losing_safe_frames(
    tmp_path,
) -> None:
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")

    try:
        raise RuntimeError("provider failed sk-fakeSecretToken1234567890abcdef")
    except RuntimeError:
        logger.exception("Native run failed")
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")
    assert "sk-fakeSecretToken1234567890abcdef" not in content
    assert "provider failed" not in content
    assert "[KEY]" not in content
    assert "Traceback (most recent call last)" in content
    assert "RuntimeError" in content
    assert re.search(r'File ".+test_logging_config\.py", line \d+, in ', content)


def test_log_filter_projects_exception_arguments_to_type_and_code(tmp_path) -> None:
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")
    secret = "PRIVATE_CLAIM_AT_https://private.example/path?token=secret-value"

    logger.error(
        "Provider operation failed: %s",
        _CodedLogError(secret),
    )
    logger.error(
        "%(operation)s failed: %(failure)s",
        {
            "operation": "source_read",
            "failure": ValueError("PRIVATE_MAPPING_EXCEPTION_ARGUMENT"),
        },
    )
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")
    assert "_CodedLogError[error_code=provider_timeout]" in content
    assert "source_read failed: ValueError" in content
    assert "PRIVATE_CLAIM" not in content
    assert "private.example" not in content
    assert "secret-value" not in content
    assert "PRIVATE_MAPPING_EXCEPTION_ARGUMENT" not in content


def test_log_exception_projects_nested_validation_cause_without_arguments(
    tmp_path,
) -> None:
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")

    class _Payload(BaseModel):
        count: int

    def _raise_nested_validation_error() -> None:
        try:
            _Payload.model_validate(
                {
                    "count": (
                        "PRIVATE_VALIDATION_CLAIM "
                        "https://private.example/evidence?api_key=secret-value"
                    )
                }
            )
        except ValidationError as cause:
            raise _CodedLogError(
                "PRIVATE_OUTER_EXCEPTION_ARGUMENT"
            ) from cause

    try:
        _raise_nested_validation_error()
    except _CodedLogError:
        logger.exception("Source validation failed")
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")
    assert "ValidationError" in content
    assert "_CodedLogError[error_code=provider_timeout]" in content
    assert "direct cause" in content
    assert "in _raise_nested_validation_error" in content
    assert "PRIVATE_VALIDATION_CLAIM" not in content
    assert "PRIVATE_OUTER_EXCEPTION_ARGUMENT" not in content
    assert "private.example" not in content
    assert "secret-value" not in content
    assert "Input should be a valid integer" not in content


def test_uvicorn_error_projects_exception_format_argument() -> None:
    secret = "PRIVATE_PROVIDER_BODY_https://private.example?token=secret-value"
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "ASGI operation failed: %s",
        (_CodedLogError(secret),),
        None,
    )

    assert _RedactSecretsFilter().filter(record) is True

    assert record.getMessage() == (
        "ASGI operation failed: "
        "_CodedLogError[error_code=provider_timeout]"
    )
    assert record.args == ()
    assert "PRIVATE_PROVIDER_BODY" not in record.getMessage()
    assert "private.example" not in record.getMessage()
    assert "secret-value" not in record.getMessage()


def test_fastapi_projects_nested_exception_chain_without_arguments() -> None:
    try:
        try:
            raise ValueError(
                "PRIVATE_DOCUMENT_TEXT https://private.example/source"
            )
        except ValueError as cause:
            raise _CodedLogError("PRIVATE_PROVIDER_RESPONSE") from cause
    except _CodedLogError as failure:
        record = logging.LogRecord(
            "fastapi",
            logging.ERROR,
            __file__,
            1,
            "Request failed",
            (),
            (type(failure), failure, failure.__traceback__),
        )

    assert _RedactSecretsFilter().filter(record) is True

    assert record.exc_info is None
    assert record.exc_text is not None
    assert "ValueError" in record.exc_text
    assert "_CodedLogError[error_code=provider_timeout]" in record.exc_text
    assert "direct cause" in record.exc_text
    assert "PRIVATE_DOCUMENT_TEXT" not in record.exc_text
    assert "PRIVATE_PROVIDER_RESPONSE" not in record.exc_text
    assert "private.example" not in record.exc_text


def test_web_error_projects_preformatted_exception_and_stack_text() -> None:
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "Exception in ASGI application",
        (),
        None,
    )
    record.exc_text = "\n".join(
        (
            "Traceback (most recent call last):",
            '  File "/app/api.py", line 42, in dispatch',
            "    provider_body = 'PRIVATE_PROVIDER_BODY'",
            "PRIVATE_DOCUMENT_IDENTIFIER",
            "ValueError: PRIVATE_DOCUMENT_TEXT",
            "",
            "The above exception was the direct cause of the following exception:",
            "",
            '  File "/app/server.py", line 7, in app',
            "RuntimeError: PRIVATE_OUTER_MESSAGE",
        )
    )
    record.stack_info = "\n".join(
        (
            "Stack (most recent call last):",
            '  File "/app/server.py", line 8, in app',
            "    document = 'PRIVATE_STACK_DOCUMENT'",
            "PRIVATE_STACK_IDENTIFIER",
        )
    )

    assert _RedactSecretsFilter().filter(record) is True

    assert record.exc_text == "\n".join(
        (
            "Traceback (most recent call last):",
            '  File "/app/api.py", line 42, in dispatch',
            "ValueError",
            "The above exception was the direct cause of the following exception:",
            '  File "/app/server.py", line 7, in app',
            "RuntimeError",
        )
    )
    assert record.stack_info == "\n".join(
        (
            "Stack (most recent call last):",
            '  File "/app/server.py", line 8, in app',
        )
    )
    assert "PRIVATE" not in record.exc_text
    assert "PRIVATE" not in record.stack_info


def test_log_filter_does_not_apply_exception_projection_to_foreign_record() -> None:
    record = logging.LogRecord(
        "thirdparty.library",
        logging.ERROR,
        __file__,
        1,
        "Foreign failure: %s",
        (RuntimeError("foreign benign detail"),),
        None,
    )

    assert _RedactSecretsFilter().filter(record) is True

    assert record.getMessage() == "Foreign failure: foreign benign detail"
    assert "RuntimeError" not in record.getMessage()


def _application_log_call_sources(relative_path: str) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    source = (root / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(
            node.func, ast.Attribute
        ):
            continue
        if node.func.attr not in {
            "debug",
            "info",
            "warning",
            "error",
            "critical",
            "exception",
        }:
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in {"log", "logger"}:
            continue
        calls.append(ast.get_source_segment(source, node) or "")
    return calls


def test_high_risk_operational_logs_do_not_render_content_or_exception_text() -> None:
    paths = (
        "src/inqtrix/graph.py",
        "src/inqtrix/providers/azure.py",
        "src/inqtrix/providers/anthropic.py",
        "src/inqtrix/providers/bedrock.py",
        "src/inqtrix/providers/litellm.py",
        "src/inqtrix/providers/base.py",
        "src/inqtrix/providers/embeddings.py",
        "src/inqtrix/providers/rerankers.py",
        "src/inqtrix/agents/scheduler.py",
        "src/inqtrix/agents/discovery.py",
        "src/inqtrix/agents/memory_mem0.py",
        "src/inqtrix/agents/patch_phase.py",
        "src/inqtrix/agents/harness.py",
        "src/inqtrix/knowledge/algorithm.py",
        "src/inqtrix/knowledge/contextualize.py",
        "src/inqtrix/knowledge/page_mapping.py",
        "src/inqtrix/knowledge/parsing.py",
        "src/inqtrix/server/routers/knowledge.py",
        "src/inqtrix/server/routers/editor.py",
        "src/inqtrix/server/routers/text.py",
        "src/inqtrix/server/routers/test.py",
        "src/inqtrix/server/routers/files.py",
        "src/inqtrix/server/routers/auth.py",
        "src/inqtrix/server/streaming.py",
        "src/inqtrix/server/text_improvements.py",
        "src/inqtrix/server/app.py",
        "src/inqtrix/services/chat_service.py",
        "src/inqtrix/services/agent_memory_service.py",
        "src/inqtrix/services/file_service.py",
        "src/inqtrix/services/health_service.py",
        "src/inqtrix/services/quota_service.py",
        "src/inqtrix/services/system_runtime.py",
        "src/inqtrix/storage/object_store.py",
        "src/inqtrix/strategies/_claim_extraction.py",
        "src/inqtrix/worker/__main__.py",
    )
    forbidden_tokens = (
        "sanitize_error(",
        "sanitize_log_message(",
        "exc_message",
        "object_key",
        "file_name",
        "request.url",
        "decision.reason",
        "._bucket",
    )
    direct_exception_arg = re.compile(
        r",\s*(?:exc|e|api_error|cleanup_exc|initial_error)\s*[,)]"
    )

    violations: list[str] = []
    for path in paths:
        for call in _application_log_call_sources(path):
            if any(token in call for token in forbidden_tokens):
                violations.append(f"{path}: {call}")
            elif direct_exception_arg.search(call):
                violations.append(f"{path}: {call}")
    assert violations == []


def test_durable_lifecycle_logs_use_typed_fields_without_tracebacks() -> None:
    paths = (
        "src/inqtrix/knowledge/stores/postgres_store.py",
        "src/inqtrix/runs/indexing_postgres.py",
        "src/inqtrix/runs/deletion_postgres.py",
        "src/inqtrix/runs/upload_postgres.py",
        "src/inqtrix/server/indexing.py",
        "src/inqtrix/services/indexing_service.py",
        "src/inqtrix/services/asset_deletion_service.py",
        "src/inqtrix/services/upload_operation_service.py",
        "src/inqtrix/worker/indexing_loop.py",
        "src/inqtrix/worker/deletion_loop.py",
        "src/inqtrix/worker/upload_loop.py",
    )

    violations: list[str] = []
    for path in paths:
        for call in _application_log_call_sources(path):
            if ".exception(" in call or re.search(
                r"\bexc_info\s*=\s*True\b", call
            ):
                violations.append(f"{path}: {call}")
    assert violations == []


def test_log_filter_preserves_benign_urls(tmp_path):
    """The log filter must not erase harmless URLs (regression for [URL] redaction)."""
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")

    logger.info("ANSWER fragment: see [6](https://www.zacks.com/article/abc) for details")
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")

    assert "https://www.zacks.com/article/abc" in content
    assert "[URL]" not in content


def test_log_filter_strips_credential_inside_url(tmp_path):
    """Credential query parameters inside URLs are still redacted."""
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")

    logger.info(
        "Outbound call: https://api.example.com/v1?api_key=sk-realLookingApiKey1234567890&page=2"
    )
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")

    assert "sk-realLookingApiKey1234567890" not in content
    assert "api_key=[REDACTED]" in content
    assert "https://api.example.com/v1" in content
    assert "page=2" in content


def test_log_filter_redacts_editor_guest_route_tokens(tmp_path):
    """Guest bearer path segments never reach console or file logs."""
    log_path = configure_logging(enabled=True, log_dir=str(tmp_path / "logs"))
    logger = logging.getLogger("inqtrix")
    token = "sentinel-guest-link-token-that-must-not-leak"

    logger.warning(
        "Guest routes failed: https://desk.example/s/%s and "
        "/v1/editor/share-links/%s:unlock",
        token,
        token,
    )
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    content = Path(log_path).read_text(encoding="utf-8")
    assert token not in content
    assert "/s/[REDACTED]" in content
    assert "/v1/editor/share-links/[REDACTED]" in content


def test_uvicorn_console_access_filter_preserves_formatter_and_redacts_token():
    """The default stdout access handler must not bypass guest-token redaction."""
    from uvicorn.logging import AccessFormatter

    token = "sentinel-guest-link-token-that-must-not-leak"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:12345",
            "POST",
            f"/v1/editor/share-links/{token}:unlock",
            "1.1",
            200,
        ),
        None,
    )

    assert _RedactSecretsFilter().filter(record) is True
    formatted = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s'
    ).format(record)

    assert token not in formatted
    assert "/v1/editor/share-links/[REDACTED]" in formatted
    assert len(record.args) == 5


def test_log_directory_is_created_automatically(tmp_path):
    log_dir = tmp_path / "nested" / "logs"

    assert not log_dir.exists()

    log_path = configure_logging(enabled=True, log_dir=str(log_dir))

    assert log_dir.exists()
    assert log_path == log_dir / log_path.name


def test_force_false_preserves_existing_handlers(tmp_path):
    """Bug B regression: when a script already configured the inqtrix
    logger (with a FileHandler), a follow-up ``configure_logging(force=False)``
    from ``create_app`` must NOT close and replace those handlers.

    Without this guard, every webserver-stack example silently lost its
    INFO-level file logging because ``create_app`` called
    ``configure_logging`` a second time and tore the script's setup down.
    """
    log_dir = tmp_path / "logs"

    first_path = configure_logging(
        enabled=True, level="INFO", log_dir=str(log_dir), console=True
    )
    logger = logging.getLogger("inqtrix")
    file_handlers_before = [
        h for h in logger.handlers if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers_before) == 1
    assert is_configured() is True

    second_path = configure_logging(
        enabled=False, level="WARNING", console=True, force=False
    )

    assert second_path is None
    file_handlers_after = [
        h for h in logger.handlers if isinstance(h, logging.FileHandler)
    ]
    assert file_handlers_after == file_handlers_before, (
        "force=False must not remove the existing FileHandler"
    )
    assert file_handlers_after[0].stream.closed is False
    assert first_path is not None and first_path.exists()


def test_force_true_default_still_replaces_handlers(tmp_path):
    """Backwards compatibility: the default ``force=True`` keeps the
    historical reset-and-replace semantics so existing test fixtures and
    explicit script-driven re-configures still work.
    """
    log_dir = tmp_path / "logs"

    configure_logging(enabled=True, level="INFO", log_dir=str(log_dir))
    logger = logging.getLogger("inqtrix")
    first_handler = next(
        h for h in logger.handlers if isinstance(h, logging.FileHandler)
    )
    first_stream = first_handler.stream

    configure_logging(enabled=True, level="DEBUG", log_dir=str(log_dir))

    assert first_stream.closed is True


def test_build_uvicorn_log_config_with_log_file_mirrors_into_file(tmp_path):
    """When a log_file is provided, the dict-config must wire a
    FileHandler into uvicorn / uvicorn.access / fastapi so request
    access lines land in the inqtrix file once uvicorn applies the
    config.
    """
    import logging.config

    log_file = tmp_path / "logs" / "inqtrix_test.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch()

    config = build_uvicorn_log_config(log_file, web_level="INFO")
    logging.config.dictConfig(config)

    logging.getLogger("uvicorn.access").info(
        '127.0.0.1:12345 - "GET /health HTTP/1.1" 200'
    )
    logging.getLogger("fastapi").info("fastapi pre-flight check ok")
    for name in _WEB_LOGGER_NAMES:
        for handler in logging.getLogger(name).handlers:
            if hasattr(handler, "flush"):
                handler.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "GET /health" in content
    assert "fastapi pre-flight check ok" in content


def test_build_uvicorn_log_config_without_file_keeps_defaults(tmp_path):
    """``log_file=None`` must produce a dict-config that preserves
    uvicorn's stderr/stdout defaults but does not write to any file
    (no inqtrix_file handler key).
    """
    config = build_uvicorn_log_config(None)
    assert "inqtrix_file" not in config["handlers"]
    # The two default uvicorn handlers must still be present.
    assert {"default", "access"} <= set(config["handlers"].keys())
    assert config["handlers"]["default"]["filters"] == ["redact_secrets"]
    assert config["handlers"]["access"]["filters"] == ["redact_secrets"]


def test_build_uvicorn_log_config_respects_web_level(tmp_path):
    """The dict-config must apply ``web_level`` to every uvicorn
    logger so an operator can crank uvicorn down to WARNING without
    losing the inqtrix file logger.
    """
    log_file = tmp_path / "logs" / "inqtrix_test.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch()

    config = build_uvicorn_log_config(log_file, web_level="WARNING")

    for logger_name in _WEB_LOGGER_NAMES:
        assert config["loggers"][logger_name]["level"] == "WARNING", (
            f"web_level should propagate to {logger_name}"
        )


def test_describe_logging_state_reports_silent_logger():
    """Before configuration the logger is silent and the banner must say so."""
    state = describe_logging_state()

    assert state["file_enabled"] is False
    assert state["file_path"] is None
    assert state["console_enabled"] is False
    assert state["web_mirrored"] is False


def test_describe_logging_state_reports_file_path(tmp_path):
    """With file logging enabled the state must expose the absolute log path."""
    log_dir = tmp_path / "logs"
    log_path = configure_logging(enabled=True, level="INFO", log_dir=str(log_dir))
    assert log_path is not None

    state = describe_logging_state()

    assert state["file_enabled"] is True
    assert state["file_path"] is not None
    assert Path(state["file_path"]) == log_path.resolve()
    assert state["level"] == "INFO"
    assert state["silent"] is False


def test_describe_logging_state_reports_console_only(tmp_path):
    """Console-only configuration must be reported as such (no file path)."""
    configure_logging(enabled=False, level="WARNING", console=True)

    state = describe_logging_state()

    assert state["file_enabled"] is False
    assert state["file_path"] is None
    assert state["console_enabled"] is True
    assert state["silent"] is False


def test_format_logging_banner_mentions_log_file(tmp_path):
    """The banner must contain the log path when file logging is active."""
    log_path = configure_logging(
        enabled=True, level="DEBUG", log_dir=str(tmp_path / "logs")
    )
    banner = format_logging_banner()

    assert "ENABLED" in banner
    assert "DEBUG" in banner
    assert str(log_path) in banner


def test_format_logging_banner_disabled_has_tip():
    """When logging is off, the banner must tell operators how to turn it on."""
    banner = format_logging_banner()

    assert "DISABLED" in banner
    assert "INQTRIX_LOG_ENABLED=true" in banner


def test_print_logging_banner_writes_to_stream(tmp_path):
    """print_logging_banner writes to the provided stream and returns state."""
    configure_logging(enabled=True, level="INFO", log_dir=str(tmp_path / "logs"))
    buffer = io.StringIO()

    state = print_logging_banner(buffer)

    written = buffer.getvalue()
    assert "Inqtrix Server - Logging Status" in written
    assert state["file_enabled"] is True
    assert state["file_path"] is not None
    assert state["file_path"] in written


def test_is_configured_ignores_null_handler():
    """``is_configured`` must treat a NullHandler-only logger as silent
    so the server's last-resort bootstrap can install real handlers.
    """
    logger = logging.getLogger("inqtrix")
    logger.addHandler(logging.NullHandler())

    assert is_configured() is False

    configure_logging(enabled=False, console=True)

    assert is_configured() is True
