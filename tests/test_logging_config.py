"""Content rule of the log filter: projection, redaction, preserved structure."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from inqtrix.logging_config import InqtrixLogFilter, configure_logging


def _file_logger(tmp_path: Path) -> tuple[logging.Logger, Path]:
    resolved = configure_logging(
        service="api", target="file", format="text", log_dir=str(tmp_path / "logs")
    )
    assert resolved.file_path is not None
    return logging.getLogger("inqtrix"), Path(resolved.file_path)


def _read(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_reconfigure_closes_the_previous_file_handler(tmp_path: Path) -> None:
    _file_logger(tmp_path)
    first = next(
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    )
    first_stream = first.stream

    _file_logger(tmp_path)

    assert first_stream is not None
    assert first_stream.closed is True


def test_mapping_style_logging_is_preserved_and_redacted(tmp_path, capsys):
    logger, log_path = _file_logger(tmp_path)

    # Realistic-length API key so the {16,} guard in sanitize_log_message fires.
    logger.info(
        "%(user)s %(token)s",
        {"user": "alice", "token": "sk-secrettoken1234567890abcdef"},
    )

    captured = capsys.readouterr()
    content = _read(log_path)
    assert "Logging error" not in captured.err
    assert "alice" in content
    assert "sk-secrettoken1234567890abcdef" not in content
    assert "[KEY]" in content


class _CodedLogError(RuntimeError):
    error_code = "provider_timeout"


def test_log_filter_removes_exception_message_without_losing_safe_frames(
    tmp_path,
) -> None:
    logger, log_path = _file_logger(tmp_path)

    try:
        raise RuntimeError("provider failed sk-fakeSecretToken1234567890abcdef")
    except RuntimeError:
        logger.exception("Native run failed")

    content = _read(log_path)
    assert "sk-fakeSecretToken1234567890abcdef" not in content
    assert "provider failed" not in content
    assert "[KEY]" not in content
    assert "Traceback (most recent call last)" in content
    assert "RuntimeError" in content
    assert re.search(r'File ".+test_logging_config\.py", line \d+, in ', content)


def test_log_filter_projects_exception_arguments_to_type_and_code(tmp_path) -> None:
    logger, log_path = _file_logger(tmp_path)
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

    content = _read(log_path)
    assert "_CodedLogError[error_code=provider_timeout]" in content
    assert "source_read failed: ValueError" in content
    assert "PRIVATE_CLAIM" not in content
    assert "private.example" not in content
    assert "secret-value" not in content
    assert "PRIVATE_MAPPING_EXCEPTION_ARGUMENT" not in content


def test_log_exception_projects_nested_validation_cause_without_arguments(
    tmp_path,
) -> None:
    logger, log_path = _file_logger(tmp_path)

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

    content = _read(log_path)
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

    assert InqtrixLogFilter().filter(record) is True

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

    assert InqtrixLogFilter().filter(record) is True

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

    assert InqtrixLogFilter().filter(record) is True

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


def test_host_filter_leaves_foreign_records_unprojected() -> None:
    """Without a service the host keeps its policy for foreign loggers."""

    record = logging.LogRecord(
        "thirdparty.library",
        logging.ERROR,
        __file__,
        1,
        "Foreign failure: %s",
        (RuntimeError("foreign benign detail"),),
        None,
    )

    assert InqtrixLogFilter().filter(record) is True

    assert record.getMessage() == "Foreign failure: foreign benign detail"
    assert "RuntimeError" not in record.getMessage()


def test_service_filter_projects_foreign_records() -> None:
    record = logging.LogRecord(
        "thirdparty.library",
        logging.ERROR,
        __file__,
        1,
        "Foreign failure: %s",
        (RuntimeError("foreign benign detail"),),
        None,
    )

    assert InqtrixLogFilter(service="api").filter(record) is True

    assert record.getMessage() == "Foreign failure: RuntimeError"


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
        "src/inqtrix/runs/indexing_memory.py",
        "src/inqtrix/runs/indexing_records.py",
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
    logger, log_path = _file_logger(tmp_path)

    logger.info("ANSWER fragment: see [6](https://www.zacks.com/article/abc) for details")

    content = _read(log_path)
    assert "https://www.zacks.com/article/abc" in content
    assert "[URL]" not in content


def test_log_filter_strips_credential_inside_url(tmp_path):
    """Credential query parameters inside URLs are still redacted."""
    logger, log_path = _file_logger(tmp_path)

    logger.info(
        "Outbound call: https://api.example.com/v1?api_key=sk-realLookingApiKey1234567890&page=2"
    )

    content = _read(log_path)
    assert "sk-realLookingApiKey1234567890" not in content
    assert "api_key=[REDACTED]" in content
    assert "https://api.example.com/v1" in content
    assert "page=2" in content


def test_log_filter_redacts_editor_guest_route_tokens(tmp_path):
    """Guest bearer path segments never reach console or file logs."""
    logger, log_path = _file_logger(tmp_path)
    token = "sentinel-guest-link-token-that-must-not-leak"

    logger.warning(
        "Guest routes failed: https://desk.example/s/%s and "
        "/v1/editor/share-links/%s:unlock",
        token,
        token,
    )

    content = _read(log_path)
    assert token not in content
    assert "/s/[REDACTED]" in content
    assert "/v1/editor/share-links/[REDACTED]" in content


def test_access_record_keeps_uvicorn_formatter_arguments_and_redacts_token():
    """An organization may format access lines with uvicorn's AccessFormatter."""
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
            f"/v1/editor/share-links/{token}:unlock?draft=1",
            "1.1",
            200,
        ),
        None,
    )

    assert InqtrixLogFilter(service="api").filter(record) is True
    formatted = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s'
    ).format(record)

    assert token not in formatted
    assert "127.0.0.1" not in formatted
    assert "draft=1" not in formatted
    assert "/v1/editor/share-links/[REDACTED]" in formatted
    assert len(record.args) == 5


def test_log_directory_is_created_automatically(tmp_path):
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()

    resolved = configure_logging(service="worker", target="file", log_dir=str(log_dir))

    assert log_dir.exists()
    assert resolved.file_path == str(log_dir / "inqtrix-worker.log")
