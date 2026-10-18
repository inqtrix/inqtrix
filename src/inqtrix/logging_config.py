"""Centralized logging configuration for inqtrix.

Provides ``configure_logging()`` for the ``inqtrix`` logger (with
automatic secret redaction) plus ``build_uvicorn_log_config()`` for the
uvicorn/FastAPI loggers. The two helpers are intentionally separate
because uvicorn overwrites its own loggers via ``logging.config.dictConfig``
on every ``uvicorn.run(...)`` call — any handler that was attached to
``uvicorn.*`` before that call gets dropped. The recipe in the
webserver-stack examples is therefore:

1. ``configure_logging(...)`` — sets up the inqtrix logger and returns
   the file path of the active logfile.
2. ``uvicorn.run(app, log_config=build_uvicorn_log_config(log_path), ...)``
   — uvicorn applies the returned dict-config which mirrors its own
   defaults (stderr/stdout) AND adds a FileHandler pointing at the
   inqtrix logfile so request access lines, startup/shutdown notices
   and FastAPI errors land in the same file as the inqtrix records.

Environment variables (convention, not enforced here):

- ``INQTRIX_LOG_ENABLED`` — set to ``true`` to activate file logging.
- ``INQTRIX_LOG_LEVEL``   — ``DEBUG``, ``INFO``, ``WARNING`` (default: ``INFO``).
- ``INQTRIX_LOG_CONSOLE``  — set to ``true`` to additionally print
  WARNING+ messages to stderr. Useful for server mode.
- ``INQTRIX_LOG_INCLUDE_WEB`` — when file logging is enabled, also
  pass a uvicorn ``log_config`` that mirrors web-server logs into the
  same file (default ``true``; set to ``false`` when uvicorn streams
  to a structured-logging sink and the duplication would be noise).
- ``INQTRIX_LOG_WEB_LEVEL`` — log level applied to the uvicorn loggers
  in the generated ``log_config`` (default ``INFO`` so access lines
  ``GET /health 200 OK`` make it into the file).
- ``OBSERVABILITY_PROFILE`` — agent setting that controls protected run-audit
  detail (``summary`` by default, ``forensic`` for source/citation/claim/answer
  lineage); ordinary logs receive a content-minimized operational projection.
- ``INQTRIX_LOG_FORMAT`` — ``text`` (default, the historical pipe
  format, byte-identical without the flag) or ``json`` (one
  machine-readable object per line carrying the correlation fields from
  :mod:`inqtrix.observability.context`; the recommended container
  setting). The redaction filter runs unchanged in both formats.

:func:`read_logging_env` reads all of these in one place so the four
bootstrap sites (server entrypoint, worker entrypoint, ``create_app``,
multi-stack assembly) cannot drift apart.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from inqtrix.observability.json_formatter import InqtrixJsonFormatter
from inqtrix.urls import sanitize_log_message

_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(threadName)s | %(name)s | %(message)s"
_CONSOLE_FORMAT = "%(levelname)s | %(message)s"


@dataclass(frozen=True)
class LoggingEnv:
    """The process-level logging environment, resolved with defaults."""

    enabled: bool
    level: str
    console: bool
    include_web: bool
    web_level: str
    json_format: bool


def read_logging_env() -> LoggingEnv:
    """Read every ``INQTRIX_LOG_*`` process variable with its default.

    Logging must be configurable BEFORE ``Settings()`` validation can
    fail, so these stay plain environment reads on purpose; ``Settings``
    never mirrors them (one source of truth per variable).
    """
    return LoggingEnv(
        enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
        level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
        console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
        include_web=(
            os.getenv("INQTRIX_LOG_INCLUDE_WEB", "true").lower() != "false"
        ),
        web_level=os.getenv("INQTRIX_LOG_WEB_LEVEL", "INFO"),
        json_format=(
            os.getenv("INQTRIX_LOG_FORMAT", "text").strip().lower() == "json"
        ),
    )

_SAFE_EXCEPTION_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,120}$")
_SAFE_TRACEBACK_TYPE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning)"
    r"|BaseException|ExceptionGroup|BaseExceptionGroup|KeyboardInterrupt"
    r"|SystemExit|GeneratorExit|StopIteration|StopAsyncIteration)"
    r"(?:\[(?:code|error_code|status_code|sqlstate)=[A-Za-z0-9_.:+-]{1,120}\])?$"
)
_TRACEBACK_FRAME_RE = re.compile(r'^  File ".+", line \d+, in .+$')
_TRACEBACK_CHAIN_LINES = frozenset({
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
})

# Web-server loggers that ``build_uvicorn_log_config`` reconfigures so
# uvicorn / FastAPI log records mirror into the inqtrix file. Order
# matters only for documentation clarity; the dict-config wires each
# logger independently.
_WEB_LOGGER_NAMES: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
)


def _is_inqtrix_record(record: logging.LogRecord) -> bool:
    """Return whether *record* belongs to the application logger tree."""

    return record.name == "inqtrix" or record.name.startswith("inqtrix.")


def _is_web_error_record(record: logging.LogRecord) -> bool:
    """Return whether a web-server record can carry application failures.

    Uvicorn's access logger has a separate formatter contract and contains
    only the bounded request-line fields handled below.  Every other uvicorn
    logger, plus FastAPI's logger tree, can receive an application exception
    and therefore uses the same content-minimized projection as ``inqtrix``.
    """

    name = record.name
    if name == "uvicorn.access" or name.startswith("uvicorn.access."):
        return False
    return (
        name == "uvicorn"
        or name.startswith("uvicorn.")
        or name == "fastapi"
        or name.startswith("fastapi.")
    )


def _safe_exception_code(exc: BaseException) -> tuple[str, str] | None:
    """Return one machine-readable exception code without stringifying it."""

    for attribute in ("code", "error_code", "status_code", "sqlstate"):
        try:
            raw = getattr(exc, attribute, None)
        except Exception:  # noqa: BLE001 - defensive projection only
            continue
        if isinstance(raw, Enum):
            raw = raw.value
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            continue
        value = str(raw)
        if _SAFE_EXCEPTION_CODE_RE.fullmatch(value):
            return attribute, value
    return None


def _exception_descriptor(exc: BaseException) -> str:
    """Describe an exception solely by type and an optional stable code."""

    label = type(exc).__name__
    code = _safe_exception_code(exc)
    if code is None:
        return label
    key, value = code
    return f"{label}[{key}={value}]"


def _project_exception_values(value: Any) -> Any:
    """Replace exceptions nested in logging arguments without rendering args."""

    if isinstance(value, BaseException):
        return _exception_descriptor(value)
    if isinstance(value, Mapping):
        return {
            key: _project_exception_values(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_project_exception_values(item) for item in value)
    if isinstance(value, list):
        return [_project_exception_values(item) for item in value]
    return value


def _exception_chain(
    exc: BaseException,
    tb: Any,
    *,
    seen: set[int] | None = None,
) -> list[tuple[BaseException, Any, str | None]]:
    """Return cause/context chain from root to outer exception without args."""

    visited = seen if seen is not None else set()
    identity = id(exc)
    if identity in visited:
        return []
    visited.add(identity)

    prefix: list[tuple[BaseException, Any, str | None]] = []
    cause = exc.__cause__
    context = exc.__context__ if not exc.__suppress_context__ else None
    if cause is not None:
        prefix.extend(_exception_chain(cause, cause.__traceback__, seen=visited))
        separator = "The above exception was the direct cause of the following exception:"
    elif context is not None:
        prefix.extend(_exception_chain(context, context.__traceback__, seen=visited))
        separator = "During handling of the above exception, another exception occurred:"
    else:
        separator = None
    prefix.append((exc, tb, separator if prefix else None))
    return prefix


def _format_exception_without_arguments(exc_info: tuple[Any, Any, Any]) -> str:
    """Render safe frames and exception types, never exception messages/args."""

    exc_type, exc, tb = exc_info
    if not isinstance(exc, BaseException):
        name = getattr(exc_type, "__name__", "Exception")
        return str(name)

    lines: list[str] = []
    for current, current_tb, separator in _exception_chain(exc, tb):
        if separator is not None:
            lines.extend(("", separator, ""))
        if current_tb is not None:
            lines.append("Traceback (most recent call last):")
            for frame in traceback.extract_tb(current_tb):
                lines.append(
                    f'  File "{frame.filename}", line {frame.lineno}, '
                    f"in {frame.name}"
                )
        lines.append(_exception_descriptor(current))
    return "\n".join(lines)


def project_exception(exc: BaseException) -> tuple[str, str]:
    """An exception as an application log line shows it: no prose, no source.

    Returns the descriptor (type and an optional stable code) and the
    traceback reduced to frame locations and the descriptors of its chain:
    the projection :class:`_RedactSecretsFilter` applies to application and
    web-error records. For telemetry that records an exception outside the
    logging pipeline, such as a span's exception event, so a trace never
    carries more of an exception than a log line does.
    """
    return (
        _exception_descriptor(exc),
        _format_exception_without_arguments((type(exc), exc, exc.__traceback__)),
    )


def _project_preformatted_traceback(value: str) -> str:
    """Strip messages/source lines from a traceback formatted by another handler."""

    safe_lines: list[str] = []
    for line in str(value).splitlines():
        stripped = line.strip()
        if stripped in {"Traceback (most recent call last):", "Stack (most recent call last):"}:
            safe_lines.append(stripped)
        elif stripped in _TRACEBACK_CHAIN_LINES:
            safe_lines.append(stripped)
        elif _TRACEBACK_FRAME_RE.fullmatch(line):
            # Normal traceback frame; source-code lines are intentionally not
            # retained because literals may contain prompts or credentials.
            safe_lines.append(line)
        else:
            # A prior formatter may already have rendered ``Type: message``.
            # Recover only the type/code prefix.  The prose after the first
            # colon can contain provider responses, validation input or
            # document text and is therefore never retained.
            descriptor = stripped.partition(":")[0]
            if _SAFE_TRACEBACK_TYPE_RE.fullmatch(descriptor):
                safe_lines.append(descriptor)
    return "\n".join(safe_lines) or "Exception"


class _RedactSecretsFilter(logging.Filter):
    """Project application/web exceptions and scrub secrets before handlers.

    Uses :func:`inqtrix.urls.sanitize_log_message` rather than
    ``sanitize_error`` so harmless URLs in answer text or citation maps are
    preserved (only credential values inside URLs are redacted). Exception
    arguments and tracebacks from application and web-error loggers retain
    only their type, stable machine-readable code, and frame locations;
    exception prose is never treated as operational log data.  Uvicorn access
    records retain their canonical formatter arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's AccessFormatter requires its canonical five positional
        # arguments after handler filters have run. Preserve that structure
        # and scrub the request target in place; eagerly formatting and
        # clearing ``args`` (the generic path below) would make the formatter
        # fail while still leaving the console access stream unsanitized.
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 5
        ):
            sanitized_args = list(record.args)
            sanitized_args[2] = sanitize_log_message(str(sanitized_args[2]))
            record.args = tuple(sanitized_args)
            return True
        if _is_inqtrix_record(record) or _is_web_error_record(record):
            # Application/web exceptions are operational signals, never log
            # content. Project exception objects before getMessage() can call
            # ``str(exc)`` and render provider bodies, URLs, validation input,
            # claims or credentials. Non-exception arguments retain their
            # existing formatting contract.
            record.msg = _project_exception_values(record.msg)
            record.args = _project_exception_values(record.args)
            record.msg = sanitize_log_message(record.getMessage())
            record.args = ()
            # Truthiness like logging.Formatter: ``exc_info=False`` is a legal
            # call and reaches the record as ``False``.
            if record.exc_info:
                record.exc_text = _format_exception_without_arguments(
                    record.exc_info
                )
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = _project_preformatted_traceback(
                    record.exc_text
                )
            if record.stack_info:
                record.stack_info = _project_preformatted_traceback(
                    record.stack_info
                )
            return True
        # Sanitize the fully rendered message. Scrubbing the format template
        # and arguments independently is unsafe for bearer path segments:
        # ``"/s/%s", token`` would redact the ``%s`` placeholder itself,
        # leave the argument behind, and make logging raise TypeError. Eager
        # formatting happens at handler time (where logging would format
        # anyway), preserves mapping-style records, and catches credentials
        # that span a template/argument boundary.
        record.msg = sanitize_log_message(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Formatters render exc_info only after filters have run, so a raw
            # provider exception would otherwise bypass msg/args redaction.
            # Preserve every frame, but replace the formatter input with one
            # already-scrubbed traceback shared by all handlers.
            record.exc_text = sanitize_log_message(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = sanitize_log_message(record.exc_text)
        return True


def _close_handlers(logger: logging.Logger) -> None:
    """Remove and close all handlers currently attached to *logger*."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def is_configured() -> bool:
    """Return True iff the ``inqtrix`` logger already has real handlers.

    A ``NullHandler``-only logger is considered *not configured* — it is
    the silent default that example scripts and the server's last-resort
    bootstrap should be allowed to overwrite. Any non-null handler
    (FileHandler, StreamHandler, ...) counts as a real prior
    configuration that callers like :func:`inqtrix.server.app.create_app`
    must respect rather than tear down.

    Returns:
        True when at least one non-``NullHandler`` is attached to the
        ``inqtrix`` logger; False otherwise.
    """
    logger = logging.getLogger("inqtrix")
    return any(
        not isinstance(handler, logging.NullHandler)
        for handler in logger.handlers
    )


def configure_logging(
    *,
    enabled: bool = False,
    level: str = "INFO",
    log_dir: str = "logs",
    console: bool = False,
    force: bool = True,
    json_format: bool = False,
) -> Path | None:
    """Configure the ``inqtrix`` logger.

    Only touches ``logging.getLogger("inqtrix")`` — never the root
    logger — so third-party libraries like ``botocore``, ``httpx``,
    or ``openai`` do not flood the output. The uvicorn/FastAPI
    loggers are configured separately via
    :func:`build_uvicorn_log_config` because uvicorn applies its own
    ``dictConfig`` on every ``uvicorn.run(...)`` call and would drop
    any handler attached here.

    Parameters
    ----------
    enabled:
        When *False* (default), logging is silenced (``NullHandler``).
        When *True*, a file handler is created in *log_dir*.
    level:
        Log level name (``DEBUG``, ``INFO``, ``WARNING``, …).
    log_dir:
        Directory for log files.  Created automatically.
    console:
        If *True*, an additional ``StreamHandler`` at ``WARNING`` level
        is attached so critical issues still appear on stderr even when
        file logging is disabled.
    force:
        When *True* (default, backwards-compatible), the existing
        handlers are closed and replaced — useful for tests and for
        an explicit re-configure from a script. When *False*, the
        function becomes a no-op if a real handler is already present
        (see :func:`is_configured`); this is the mode the server's
        last-resort bootstrap uses so a webserver-stack example that
        already wired its own file logger is not torn down by
        :func:`inqtrix.server.app.create_app`. The check looks at
        non-``NullHandler`` handlers only, so the silent default
        installed by ``configure_logging(enabled=False)`` is still
        replaceable.
    json_format:
        When *True*, every handler renders structured JSON lines via
        :class:`~inqtrix.observability.json_formatter.InqtrixJsonFormatter`
        instead of the plaintext formats. The redaction filter is
        attached identically in both modes; the default *False* keeps
        the historical output byte-identical.

    Returns
    -------
    Path | None
        Path to the log file when file logging is active, else *None*.
        When ``force=False`` and the logger was already configured the
        return value is also *None* (no new file handler is created).
    """
    logger = logging.getLogger("inqtrix")
    if not force and is_configured():
        return None
    _close_handlers(logger)
    logger.propagate = False

    resolved_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(resolved_level)

    if console:
        console_handler = logging.StreamHandler()
        # Text mode keeps the historical "mirror WARNING+ to stderr"
        # behaviour (byte-compatible). JSON mode makes stdout the
        # CANONICAL machine-readable sink for container runtimes, so it
        # must carry the configured level — otherwise the documented
        # correlation workflow (`logs | jq 'select(.user == …)'`) can
        # never see an INFO line, and every correlation field the
        # program adds is invisible wherever no file sink is enabled.
        console_handler.setLevel(
            resolved_level if json_format else logging.WARNING
        )
        console_handler.setFormatter(
            InqtrixJsonFormatter()
            if json_format
            else logging.Formatter(_CONSOLE_FORMAT)
        )
        console_handler.addFilter(_RedactSecretsFilter())
        logger.addHandler(console_handler)

    if not enabled:
        if console:
            return None
        logger.addHandler(logging.NullHandler())
        return None

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"inqtrix_{datetime.now():%Y%m%d_%H%M%S}.log"

    redact_filter = _RedactSecretsFilter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(resolved_level)
    file_handler.setFormatter(
        InqtrixJsonFormatter()
        if json_format
        else logging.Formatter(_FILE_FORMAT)
    )
    file_handler.addFilter(redact_filter)
    logger.addHandler(file_handler)

    return log_file


def build_uvicorn_log_config(
    log_file: Path | str | None,
    *,
    web_level: str = "INFO",
    json_format: bool = False,
) -> dict[str, object]:
    """Build a uvicorn-compatible ``log_config`` dict that mirrors web logs into *log_file*.

    Designed to be passed to ``uvicorn.run(app, log_config=...)`` so
    uvicorn's own ``dictConfig`` install does not strip the inqtrix
    file handler off the ``uvicorn``/``uvicorn.error``/``uvicorn.access``/
    ``fastapi`` loggers. The returned dict mirrors uvicorn's default
    ``LOGGING_CONFIG`` (stderr stream for ``default``, stdout for
    ``access``) and additionally wires each logger to a ``FileHandler``
    pointing at *log_file*.

    Args:
        log_file: Path to the inqtrix logfile (typically the return
            value of :func:`configure_logging`). When ``None``, the
            returned config is identical to uvicorn's defaults — useful
            for the ``include_web=False`` env path.
        web_level: Log level name applied to every web-server logger.
            Default ``INFO`` so request access lines reach the file.
        json_format: When ``True``, every uvicorn/FastAPI handler
            renders the same structured JSON lines as the ``inqtrix``
            logger. This MUST follow the ``configure_logging`` choice —
            uvicorn re-applies its ``dictConfig`` on every ``run()``, so
            leaving it plaintext would mix formats on stdout.

    Returns:
        A dict-config compatible with ``logging.config.dictConfig`` and
        with uvicorn's ``log_config`` parameter.
    """
    web_level_resolved = getattr(logging, web_level.upper(), logging.INFO)
    web_level_name = logging.getLevelName(web_level_resolved)

    handlers: dict[str, dict[str, object]] = {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "filters": ["redact_secrets"],
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "filters": ["redact_secrets"],
            "stream": "ext://sys.stdout",
        },
    }

    default_handlers = ["default"]
    access_handlers = ["access"]

    if log_file is not None:
        handlers["inqtrix_file"] = {
            "formatter": "file",
            "class": "logging.FileHandler",
            "filename": str(log_file),
            "encoding": "utf-8",
            "filters": ["redact_secrets"],
        }
        default_handlers.append("inqtrix_file")
        access_handlers.append("inqtrix_file")

    config: dict[str, object] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "redact_secrets": {
                "()": "inqtrix.logging_config._RedactSecretsFilter",
            },
        },
        "formatters": (
            {
                # One JSON shape across app and web-server records; the
                # access line is rendered into the message field.
                "default": {
                    "()": "inqtrix.observability.json_formatter.InqtrixJsonFormatter",
                },
                "access": {
                    "()": "inqtrix.observability.json_formatter.InqtrixJsonFormatter",
                },
                "file": {
                    "()": "inqtrix.observability.json_formatter.InqtrixJsonFormatter",
                },
            }
            if json_format
            else {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(levelprefix)s %(message)s",
                    "use_colors": None,
                },
                "access": {
                    "()": "uvicorn.logging.AccessFormatter",
                    "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                },
                "file": {
                    "format": _FILE_FORMAT,
                },
            }
        ),
        "handlers": handlers,
        "loggers": {
            "uvicorn": {
                "handlers": default_handlers,
                "level": web_level_name,
                "propagate": False,
            },
            "uvicorn.error": {
                "level": web_level_name,
            },
            "uvicorn.access": {
                "handlers": access_handlers,
                "level": web_level_name,
                "propagate": False,
            },
            "fastapi": {
                "handlers": default_handlers,
                "level": web_level_name,
                "propagate": False,
            },
        },
    }
    return config


def describe_logging_state() -> dict[str, Any]:
    """Introspect the ``inqtrix`` and uvicorn loggers and return a status dict.

    Used by :func:`format_logging_banner` (and by tests) to render a
    terminal-friendly summary of the active logging configuration at
    server startup. The return value is intentionally plain Python
    (no Pydantic) so it stays cheap to call and easy to serialise.

    Returns:
        A dict with the following keys:

        - ``file_enabled`` (bool): whether a real :class:`logging.FileHandler`
          is attached to the ``inqtrix`` logger.
        - ``file_path`` (str | None): absolute path of the first file
          handler, or ``None`` when file logging is off.
        - ``level`` (str): effective level name of the ``inqtrix`` logger
          (``DEBUG``, ``INFO``, ``WARNING``, ...).
        - ``console_enabled`` (bool): whether a :class:`logging.StreamHandler`
          (i.e. stderr mirror) is attached to the ``inqtrix`` logger.
        - ``silent`` (bool): ``True`` when only a ``NullHandler`` is
          attached — useful to tell operators that nothing will be
          emitted anywhere.
        - ``web_mirrored`` (bool): ``True`` when uvicorn / FastAPI
          loggers also write into the inqtrix logfile (i.e. a
          :class:`logging.FileHandler` is wired into ``uvicorn`` or
          ``uvicorn.access``). ``False`` otherwise.
        - ``web_level`` (str | None): uvicorn logger level when web
          loggers have any non-default handler; ``None`` when
          untouched.
        - ``format`` (str): ``"json"`` when any real handler renders
          structured JSON lines, ``"text"`` otherwise.
    """
    logger = logging.getLogger("inqtrix")

    file_path: str | None = None
    file_enabled = False
    console_enabled = False
    silent_only = True
    json_active = False

    for handler in logger.handlers:
        if isinstance(handler.formatter, InqtrixJsonFormatter):
            json_active = True
        if isinstance(handler, logging.FileHandler):
            file_enabled = True
            silent_only = False
            if file_path is None:
                try:
                    file_path = str(Path(handler.baseFilename).resolve())
                except Exception:  # noqa: BLE001
                    file_path = handler.baseFilename
        elif isinstance(handler, logging.NullHandler):
            continue
        elif isinstance(handler, logging.StreamHandler):
            console_enabled = True
            silent_only = False
        else:
            silent_only = False

    web_mirrored = False
    web_level: str | None = None
    for name in _WEB_LOGGER_NAMES:
        web_logger = logging.getLogger(name)
        if web_logger.handlers:
            if web_level is None:
                web_level = logging.getLevelName(web_logger.level or logging.INFO)
            for handler in web_logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    web_mirrored = True
                    break
        if web_mirrored:
            break

    return {
        "file_enabled": file_enabled,
        "file_path": file_path,
        "level": logging.getLevelName(logger.level or logging.WARNING),
        "console_enabled": console_enabled,
        "silent": silent_only and not file_enabled and not console_enabled,
        "web_mirrored": web_mirrored,
        "web_level": web_level,
        "format": "json" if json_active else "text",
    }


def format_logging_banner(state: dict[str, Any] | None = None) -> str:
    """Render a human-readable banner describing the current logging state.

    Produces a compact multi-line box suitable for ``print()`` to
    stderr at server startup. The banner is intentionally free of ANSI
    colour codes so it stays readable in log collectors (Docker, k8s,
    journalctl) that do not interpret terminal escapes.

    Args:
        state: Pre-computed state dict (see :func:`describe_logging_state`).
            When ``None`` (default), the function calls
            ``describe_logging_state()`` itself. The argument exists
            mainly for tests.

    Returns:
        A newline-terminated multi-line string. The caller decides
        where to print it.
    """
    if state is None:
        state = describe_logging_state()

    lines: list[str] = []
    lines.append("-" * 64)
    lines.append("  Inqtrix Server - Logging Status")
    lines.append("-" * 64)

    if state["file_enabled"]:
        lines.append(f"  File logging:    ENABLED (level={state['level']})")
        lines.append(f"  Log file:        {state['file_path']}")
    else:
        lines.append("  File logging:    DISABLED")

    lines.append(f"  Log format:      {state.get('format', 'text')}")

    if state["console_enabled"]:
        lines.append("  Console output:  ENABLED (stderr, WARNING+)")
    else:
        lines.append("  Console output:  disabled")

    if state["web_mirrored"]:
        web_level = state.get("web_level") or "INFO"
        lines.append(
            f"  Web-server logs: mirrored into log file (level={web_level})"
        )
    else:
        lines.append("  Web-server logs: terminal only (uvicorn defaults)")

    if state["silent"]:
        lines.append("")
        lines.append("  Note: Logger is silent. Enable file logging with:")
        lines.append("    INQTRIX_LOG_ENABLED=true INQTRIX_LOG_LEVEL=INFO")
    elif not state["file_enabled"]:
        lines.append("")
        lines.append("  Tip: Activate persistent logs with:")
        lines.append("    INQTRIX_LOG_ENABLED=true INQTRIX_LOG_LEVEL=INFO")

    lines.append("-" * 64)
    return "\n".join(lines) + "\n"


def print_logging_banner(stream: TextIO | None = None) -> dict[str, Any]:
    """Print the logging banner to *stream* (default ``sys.stderr``).

    Convenience wrapper around :func:`describe_logging_state` +
    :func:`format_logging_banner` + ``print``. Returns the state dict
    so callers can react to it (e.g. additionally log it through the
    inqtrix logger).

    Args:
        stream: Output stream. Defaults to ``sys.stderr`` so the banner
            stays visible when stdout is piped to another process.

    Returns:
        The state dict produced by :func:`describe_logging_state`.
    """
    state = describe_logging_state()
    target = stream if stream is not None else sys.stderr
    try:
        target.write(format_logging_banner(state))
        target.flush()
    except Exception:  # noqa: BLE001 — never let a broken stream crash startup
        pass
    return state
