"""Process logging for Inqtrix services.

Every service process (API server, worker, migration job, scripts) calls
:func:`configure_logging` once, before anything else, with its service name.
The function configures the ROOT logger of the process through
:func:`logging.config.dictConfig`:

- one output topology chosen by ``INQTRIX_LOG_TARGET`` (``console``,
  ``file``, ``both`` or ``none``), one format chosen by
  ``INQTRIX_LOG_FORMAT`` (``json`` or ``text``);
- one :class:`InqtrixLogFilter` on every handler. It stamps the correlation
  fields onto the record, projects exceptions to type, code and frames, keeps
  foreign libraries at WARNING and above with their arguments reduced to type
  names, and scrubs known secret shapes;
- the standard hooks: ``logging.captureWarnings`` and the process and thread
  exception hooks. When the process ends at an unhandled exception, its
  CRITICAL line names the messages of the exceptions Inqtrix code raised
  itself, scrubbed; the message of a foreign exception appears only where an
  Inqtrix message quotes it.

An organization with its own log schema points ``INQTRIX_LOG_CONFIG`` at a
``dictConfig`` file (or passes ``config=``); Inqtrix then attaches the filter
to each of its handlers. Embedding applications that own their logging do not
call this function and attach :class:`InqtrixLogFilter` to their handlers.

Logs never carry user content: prompts, answers, document or source text,
free URLs or queries, tokens or raw identities. Text that a foreign library
formats into its own WARNING or ERROR message before logging it is kept with
known secret shapes scrubbed; that is a documented limit.
"""

from __future__ import annotations

import copy
import dis
import importlib.util
import json
import logging
import logging.config
import re
import sys
import threading
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.observability.context import current_log_context
from inqtrix.observability.json_formatter import current_trace_ids
from inqtrix.urls import sanitize_log_message

SERVICES: tuple[str, ...] = ("api", "worker", "migrate", "gateway", "collaboration", "script")

_LEVEL_NAMES: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TEXT_FORMAT = "%(asctime)s.%(msecs)03dZ | %(levelname)-7s | %(service)s | %(name)s | %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_FILTER_NAME = "inqtrix_log_filter"
_ROOT_NULL_HANDLER = "inqtrix_root_null"
_FILTER_TARGETS = frozenset(
    {
        "inqtrix.InqtrixLogFilter",
        "inqtrix.logging_config.InqtrixLogFilter",
    }
)
_GATEWAY_LOGGER = "inqtrix.web_gateway"
_ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
_WEBSOCKET_TEMPLATE_PREFIX = '%s - "WebSocket %s"'
_WARNING_LOCATION_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<category>[A-Za-z_][\w.]*):")

_EXCEPTION_CODE_ATTRIBUTES = ("code", "error_code", "status_code", "sqlstate", "errno")
_SAFE_EXCEPTION_CODE_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,120}$")
_SAFE_TRACEBACK_TYPE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning)"
    r"|BaseException|ExceptionGroup|BaseExceptionGroup|KeyboardInterrupt"
    r"|SystemExit|GeneratorExit|StopIteration|StopAsyncIteration)"
    rf"(?:\[(?:{'|'.join(_EXCEPTION_CODE_ATTRIBUTES)})=[A-Za-z0-9_.:+-]{{1,120}}\])?$"
)
_TRACEBACK_FRAME_RE = re.compile(r'^  File ".+", line \d+, in .+$')
_TRACEBACK_HEADERS = ("Traceback (most recent call last):", "Stack (most recent call last):")
_PACKAGE_DIR = Path(__file__).resolve().parent
_RAISE_OPCODE = dis.opmap["RAISE_VARARGS"]
_SAFE_LOCATION_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ASYNCIO_VALUE_START_RE = re.compile(r"[<'\"\[{]")
_VALIDATOR_MESSAGE_TYPES = frozenset({"value_error", "assertion_error"})
_CORRELATION_FIELDS = ("request_id", "run_id", "user", "tenant", "trace_id", "span_id")
_STAMPED_FIELDS = frozenset(("service", *_CORRELATION_FIELDS))
_TRACEBACK_CHAIN_LINES = frozenset({
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
})


# --------------------------------------------------------------------- #
# Exception projection (shared with tracing through project_exception)
# --------------------------------------------------------------------- #


def _safe_exception_code(exc: BaseException) -> tuple[str, str] | None:
    """Return one machine-readable exception code without stringifying it."""

    for attribute in _EXCEPTION_CODE_ATTRIBUTES:
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
    the projection :class:`InqtrixLogFilter` applies to every record. For
    telemetry that records an exception outside the logging pipeline, such as
    a span's exception event, so a trace never carries more of an exception
    than a log line does.
    """
    return (
        _exception_descriptor(exc),
        _format_exception_without_arguments((type(exc), exc, exc.__traceback__)),
    )


def _raised_by_inqtrix(exc: BaseException) -> bool:
    """Whether Inqtrix code raised *exc* with an explicit ``raise``.

    The innermost frame decides. Its file must lie in the installed package;
    ``__name__`` would read ``__main__`` under ``python -m inqtrix``. Its last
    instruction must be a ``raise``: a C function that Inqtrix code calls
    (``int()``, a dict lookup, ``open()``) puts the value into its message and
    does not count.
    """

    tb = exc.__traceback__
    if tb is None:
        return False
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    try:
        path = Path(code.co_filename).resolve()
    except (OSError, ValueError):
        return False
    if _PACKAGE_DIR not in path.parents:
        return False
    raw = code.co_code
    return 0 <= tb.tb_lasti < len(raw) and raw[tb.tb_lasti] == _RAISE_OPCODE


def _validation_location(error: Mapping[str, Any]) -> str:
    return ".".join(
        str(part)
        if isinstance(part, int) or _SAFE_LOCATION_PART_RE.fullmatch(str(part))
        else "<key>"
        for part in error.get("loc", ())
    ) or "-"


def validation_error_pairs(exc: ValidationError) -> list[str]:
    """Each error of *exc* as ``type@location``, never the input or the message.

    Location parts that are not identifiers (dictionary keys taken from the
    validated data) appear as ``<key>``.
    """

    return [
        f"{error.get('type', 'unknown')}@{_validation_location(error)}"
        for error in exc.errors(include_input=False, include_url=False)
    ]


def _validation_summary(exc: ValidationError) -> str:
    """Each error as ``type@location: schema message``, never the input.

    A validator's own message (``value_error``, ``assertion_error``) is kept
    only when Inqtrix code raised it; a foreign validator may quote the value.
    """

    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        kind = str(error.get("type", "unknown"))
        message = str(error.get("msg", ""))
        if kind in _VALIDATOR_MESSAGE_TYPES:
            original = (error.get("ctx") or {}).get("error")
            if not (isinstance(original, BaseException) and _raised_by_inqtrix(original)):
                message = ""
        entry = f"{kind}@{_validation_location(error)}"
        parts.append(f"{entry}: {sanitize_log_message(message)}" if message else entry)
    return "; ".join(parts)


def _exit_entry(exc: BaseException) -> str:
    descriptor = _exception_descriptor(exc)
    if isinstance(exc, ValidationError):
        details = _validation_summary(exc)
    elif _raised_by_inqtrix(exc):
        details = sanitize_log_message(str(exc))
    else:
        details = ""
    return f"{descriptor}: {details}" if details else descriptor


def _exit_summary(exc: BaseException) -> str:
    """The chain of an exception that ends the process, outermost first.

    Messages appear only where Inqtrix code raised the exception itself, so an
    operator reads the diagnosis Inqtrix wrote (a missing setting, a violated
    database contract). The text of a driver or a provider appears only where
    such a message quotes it, scrubbed like every shown message.
    """

    try:
        chain = _exception_chain(exc, exc.__traceback__)
        return "; caused by ".join(_exit_entry(current) for current, _tb, _sep in reversed(chain))
    except Exception:  # noqa: BLE001 - the hook must always write its line
        return _exception_descriptor(exc)


def _asyncio_words(line: str) -> str:
    """The words asyncio wrote itself, before the first value in *line*.

    asyncio puts reprs of callbacks and their arguments into its messages
    (``Exception in callback cb('…')``, ``Executing <Handle …> took 0.2
    seconds``); a value starts at a quote, an angle bracket or a bracket.
    """
    return _ASYNCIO_VALUE_START_RE.split(line, maxsplit=1)[0].rstrip(" (:")


def _project_preformatted_traceback(value: str) -> str:
    """Strip messages/source lines from a traceback formatted by another handler."""

    safe_lines: list[str] = []
    for line in str(value).splitlines():
        stripped = line.strip()
        if stripped in _TRACEBACK_HEADERS:
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


def _project_traceback_in_text(text: str) -> str:
    """Project a traceback that a library formatted into the message text.

    Starlette, for example, hands ``traceback.format_exc()`` of a failed
    application startup to uvicorn, which logs it as the message. The text
    before the traceback stays; the traceback keeps types and frames only.
    """

    starts = [index for index in (text.find(header) for header in _TRACEBACK_HEADERS) if index >= 0]
    if not starts:
        return text
    start = min(starts)
    return text[:start] + _project_preformatted_traceback(text[start:])


# --------------------------------------------------------------------- #
# Logger families
# --------------------------------------------------------------------- #


def _in_family(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def _is_inqtrix_record(record: logging.LogRecord) -> bool:
    """Return whether *record* belongs to the application logger tree."""

    return _in_family(record.name, "inqtrix")


def _is_web_record(name: str) -> bool:
    return _in_family(name, "uvicorn") or _in_family(name, "fastapi")


def _project_foreign_value(value: Any) -> Any:
    """Numbers and booleans stay; any other foreign argument becomes its type."""

    if isinstance(value, BaseException):
        return _exception_descriptor(value)
    if isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _project_foreign_args(args: Any) -> Any:
    if isinstance(args, Mapping):
        return {key: _project_foreign_value(item) for key, item in args.items()}
    if isinstance(args, tuple):
        return tuple(_project_foreign_value(item) for item in args)
    return args


def _strip_query(target: Any) -> str:
    return sanitize_log_message(str(target).split("?", 1)[0])


# --------------------------------------------------------------------- #
# The filter
# --------------------------------------------------------------------- #


class InqtrixLogFilter(logging.Filter):
    """Stamp correlation fields and enforce the log content rule.

    Attached to every handler. The first handler that sees a record decides
    whether it is shown; a shown record is projected and gets ``service``,
    ``request_id``, ``run_id``, ``user``, ``tenant`` (as a pseudonym),
    ``trace_id`` and ``span_id``. Those stamps are also the mark that the
    filter is done: a later handler, or the far side of a queue handler that
    copied the record, passes a stamped record on, so a record is projected
    exactly once. A dropped record stays unstamped and is dropped again. No
    other attribute marks this, because exporters send every attribute beyond
    the standard ones. Formatters therefore read record attributes only and
    may run in any thread.

    With a ``service`` (as :func:`configure_logging` builds it) the filter
    owns the whole process: foreign loggers below WARNING are dropped and
    their arguments become type names. Without a ``service`` (a host attaches
    the filter to its own handlers) it treats only ``inqtrix``, ``uvicorn``
    and ``fastapi`` records and leaves every other record as the host logged
    it.
    """

    def __init__(self, *, service: str | None = None) -> None:
        super().__init__()
        self._service = service

    def filter(self, record: logging.LogRecord) -> bool:
        if record.__dict__.keys() >= _STAMPED_FIELDS:
            return True
        try:
            decision = self._decide(record)
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            record.msg = f"{record.name}: log record could not be projected"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            decision = True
        if decision:
            try:
                self._stamp(record)
            except Exception:  # noqa: BLE001 - a failing lookup costs the correlation, not the record
                self._stamp_unbound(record)
        return decision

    def _stamp(self, record: logging.LogRecord) -> None:
        context = current_log_context()
        record.service = self._service
        for name in ("request_id", "run_id", "user"):
            if record.__dict__.get(name) is None:
                setattr(record, name, context.get(name) or None)
        tenant = record.__dict__.get("tenant") or context.get("tenant")
        record.tenant = stable_pseudonym("ten", tenant) if tenant else None
        trace_ids = current_trace_ids()
        record.trace_id, record.span_id = trace_ids if trace_ids else (None, None)

    def _stamp_unbound(self, record: logging.LogRecord) -> None:
        # Every field is cleared: a tenant passed through ``extra`` must never
        # reach a formatter before it was pseudonymized.
        record.service = self._service
        for name in _CORRELATION_FIELDS:
            setattr(record, name, None)

    def _decide(self, record: logging.LogRecord) -> bool:
        name = record.name
        if _in_family(name, "uvicorn") and record.levelno < logging.INFO:
            # On DEBUG the WebSocket library writes every frame through
            # ``uvicorn.error``: document content. Never shown.
            return False
        if _in_family(name, "uvicorn.access"):
            self._project_access(record)
            return True
        if _is_inqtrix_record(record) or _is_web_record(name) or self._owns(name):
            if name == "uvicorn.error":
                self._project_websocket(record)
            self._project_own(record)
            return True
        if self._service is None:
            # A host owns its other loggers (no level rule, no argument
            # projection); known secret shapes are still scrubbed.
            self._scrub_for_host(record)
            return True
        if name == "py.warnings":
            self._project_warning(record)
            return True
        if record.levelno < logging.WARNING:
            return False
        self._project_foreign(record)
        if _in_family(name, "asyncio"):
            first_line = record.getMessage().splitlines()[0] if record.msg else ""
            record.msg = _asyncio_words(first_line)
        return True

    def _owns(self, name: str) -> bool:
        return self._service == "migrate" and _in_family(name, "alembic")

    @staticmethod
    def _render(record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken template must not raise
            message = f"{record.name}: log message could not be formatted"
        record.msg = sanitize_log_message(message)
        record.args = ()

    @staticmethod
    def _project_tracebacks(record: logging.LogRecord) -> None:
        # Truthiness like logging.Formatter: ``exc_info=False`` is a legal
        # call and reaches the record as ``False``.
        if record.exc_info:
            record.exc_text = _format_exception_without_arguments(record.exc_info)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _project_preformatted_traceback(record.exc_text)
        if record.stack_info:
            record.stack_info = _project_preformatted_traceback(record.stack_info)

    def _project_own(self, record: logging.LogRecord) -> None:
        # Exceptions are operational signals, never log content. Project
        # exception objects before getMessage() can call ``str(exc)`` and
        # render provider bodies, URLs, validation input or credentials.
        record.msg = _project_exception_values(record.msg)
        record.args = _project_exception_values(record.args)
        self._render(record)
        record.msg = _project_traceback_in_text(record.msg)
        self._project_tracebacks(record)

    def _project_foreign(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, BaseException):
            record.msg = _exception_descriptor(record.msg)
        elif not isinstance(record.msg, str):
            record.msg = f"<{type(record.msg).__name__}>"
        record.args = _project_foreign_args(record.args)
        self._render(record)
        record.msg = _project_traceback_in_text(record.msg)
        self._project_tracebacks(record)

    def _project_access(self, record: logging.LogRecord) -> None:
        # Keep uvicorn's five positional arguments: an organization's own
        # configuration may format access lines with uvicorn's
        # AccessFormatter, which reads them after the filters ran.
        if (
            record.msg == _ACCESS_TEMPLATE
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            client, method, target, http_version, status = record.args
            record.args = ("-", method, _strip_query(target), http_version, status)
        else:
            self._project_own(record)

    def _scrub_for_host(self, record: logging.LogRecord) -> None:
        # Scrub the fully rendered message: scrubbing template and arguments
        # separately would redact a ``%s`` placeholder itself and make
        # logging raise.
        self._render(record)
        if record.exc_info:
            record.exc_text = sanitize_log_message(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = sanitize_log_message(record.exc_text)

    @staticmethod
    def _project_websocket(record: logging.LogRecord) -> None:
        if (
            isinstance(record.msg, str)
            and record.msg.startswith(_WEBSOCKET_TEMPLATE_PREFIX)
            and isinstance(record.args, tuple)
            and len(record.args) >= 2
        ):
            record.args = ("-", _strip_query(record.args[1]), *record.args[2:])

    def _project_warning(self, record: logging.LogRecord) -> None:
        # ``logging.captureWarnings`` renders "file:line: Category: message"
        # plus the source line. Keep the category and the location only.
        text = record.msg if isinstance(record.msg, str) else ""
        match = _WARNING_LOCATION_RE.match(text)
        if match is None:
            record.msg = "Python warning without a parsable location"
        else:
            record.msg = (
                f"{match.group('category')} at {match.group('file')}:{match.group('line')}"
            )
        record.args = ()
        self._project_tracebacks(record)


# --------------------------------------------------------------------- #
# Settings and the resolved configuration
# --------------------------------------------------------------------- #


class LoggingSettings(BaseSettings):
    """``INQTRIX_LOG_*`` values from the process environment and ``.env``.

    An empty value counts as unset: Compose passes ``${INQTRIX_LOG_LEVEL:-}``
    and env files carry ``INQTRIX_LOG_LEVEL=`` lines.
    """

    model_config = SettingsConfigDict(
        env_prefix="INQTRIX_LOG_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    target: Literal["console", "file", "both", "none"] = "console"
    level: str = "INFO"
    format: Literal["json", "text"] = "json"
    dir: str = "logs"
    max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    backup_count: int = Field(default=5, ge=0)
    web_level: Literal["", "INFO", "WARNING", "ERROR"] = ""
    config: str = ""

    @field_validator("target", "format", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("level", "web_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        if value not in _LEVEL_NAMES:
            raise ValueError(f"INQTRIX_LOG_LEVEL must be one of {', '.join(_LEVEL_NAMES)}")
        return value


@dataclass(frozen=True)
class ResolvedLogging:
    """What :func:`configure_logging` applied; feeds the status line."""

    service: str
    target: str
    level: str
    format: str
    file_path: str | None
    web_level: str
    config_source: str | None = None


class _TextFormatter(logging.Formatter):
    """One readable line per record in UTC; reads record attributes only."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(
            _TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT, defaults={"service": "-"}
        )


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #


_KWARG_FIELDS = {
    "target": "target",
    "level": "level",
    "format": "format",
    "log_dir": "dir",
    "max_bytes": "max_bytes",
    "backup_count": "backup_count",
    "web_level": "web_level",
}
_FIELD_KEYWORDS = {field: keyword for keyword, field in _KWARG_FIELDS.items()}


def configure_logging(
    *,
    service: str,
    target: str | None = None,
    level: str | None = None,
    format: str | None = None,  # noqa: A002 - mirrors INQTRIX_LOG_FORMAT
    log_dir: str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    web_level: str | None = None,
    config: Mapping[str, Any] | str | Path | None = None,
) -> ResolvedLogging:
    """Configure the root logger of this process.

    Values passed here win over the process environment, which wins over
    ``.env``, which wins over the defaults. ``config`` (or
    ``INQTRIX_LOG_CONFIG``) replaces the built-in topology with an
    organization's own ``dictConfig``; it excludes every other logging value.

    Args:
        service: ``api``, ``worker``, ``migrate``, ``gateway``,
            ``collaboration`` or ``script``; stamped on every record.
        target: ``console``, ``file``, ``both`` or ``none``.
        level: Threshold for the ``inqtrix`` loggers.
        format: ``json`` or ``text``.
        log_dir: Directory of the rotating ``inqtrix-<service>.log``.
        max_bytes: Rotation threshold per file.
        backup_count: Number of rotated files kept.
        web_level: ``INFO``, ``WARNING`` or ``ERROR`` for ``uvicorn`` and
            ``fastapi``; empty follows ``level`` but never below INFO.
        config: A ``dictConfig`` mapping or the path of a JSON or YAML file.

    Returns:
        The resolved configuration.

    Raises:
        ValueError: On an unknown service or value, or when a custom
            configuration is combined with other logging values.
    """
    if service not in SERVICES:
        raise ValueError(f"service must be one of {', '.join(SERVICES)}")
    passed = {
        "target": target,
        "level": level,
        "format": format,
        "log_dir": log_dir,
        "max_bytes": max_bytes,
        "backup_count": backup_count,
        "web_level": web_level,
    }
    explicit = {
        field: passed[keyword]
        for keyword, field in _KWARG_FIELDS.items()
        if passed[keyword] is not None
    }
    if config is not None and not isinstance(config, Mapping):
        explicit["config"] = str(config)
    try:
        settings = LoggingSettings(**explicit)
    except ValidationError as exc:
        # Before the hooks: Python's own hook prints this, so no input value
        # and no context (``str(exc)`` of the original quotes the input).
        raise ValueError(f"Invalid logging setting: {_validation_summary(exc)}") from None
    custom = config if isinstance(config, Mapping) else None
    if custom is not None or settings.config:
        others = sorted(
            field
            for field in settings.model_fields_set - {"config"}
            if getattr(settings, field) != ""
        )
        if others:
            named = ", ".join(
                f"INQTRIX_LOG_{field.upper()} ({_FIELD_KEYWORDS[field]}=)" for field in others
            )
            raise ValueError(
                "INQTRIX_LOG_CONFIG (config=) replaces the built-in logging setup and "
                f"excludes other logging values; also set: {named}"
            )
        source = "code" if custom is not None else Path(settings.config).name
        loaded = copy.deepcopy(dict(custom)) if custom is not None else _load_config_file(settings.config)
        resolved = _apply_custom(service, loaded, source)
    else:
        resolved = _apply_builtin(service, settings)
    _install_hooks(to_stderr_only=resolved.target == "none")
    return resolved


def _web_level(settings: LoggingSettings) -> str:
    if settings.web_level:
        return settings.web_level
    if logging.getLevelName(settings.level) < logging.INFO:
        return "INFO"
    return settings.level


def _apply_builtin(service: str, settings: LoggingSettings) -> ResolvedLogging:
    stream = "ext://sys.stderr" if service == "migrate" else "ext://sys.stdout"
    formatter = settings.format
    file_path: str | None = None
    handlers: dict[str, dict[str, Any]] = {}
    if settings.target in {"console", "both"}:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "stream": stream,
            "formatter": formatter,
            "filters": [_FILTER_NAME],
        }
    if settings.target in {"file", "both"}:
        directory = Path(settings.dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_path = str(directory / f"inqtrix-{service}.log")
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": file_path,
            "maxBytes": settings.max_bytes,
            "backupCount": settings.backup_count,
            "encoding": "utf-8",
            "formatter": formatter,
            "filters": [_FILTER_NAME],
        }
    if settings.target == "none":
        # A handler at the root keeps logging.lastResort from writing
        # unfiltered records to stderr.
        handlers["none"] = {"class": "logging.NullHandler"}
    web_level = _web_level(settings)
    loggers: dict[str, dict[str, Any]] = {
        "inqtrix": {"level": settings.level, "propagate": True},
        "uvicorn": {"level": web_level, "propagate": True},
        "uvicorn.error": {"level": "NOTSET", "propagate": True},
        "uvicorn.access": {"level": "NOTSET", "propagate": True},
        "fastapi": {"level": web_level, "propagate": True},
    }
    if service == "migrate":
        loggers["alembic"] = {"level": "INFO", "propagate": True}
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {_FILTER_NAME: {"()": InqtrixLogFilter, "service": service}},
            "formatters": {
                "json": {"()": "inqtrix.observability.json_formatter.InqtrixJsonFormatter"},
                "text": {"()": _TextFormatter},
            },
            "handlers": handlers,
            "root": {"level": "WARNING", "handlers": list(handlers)},
            "loggers": loggers,
        }
    )
    return ResolvedLogging(
        service=service,
        target=settings.target,
        level=settings.level,
        format=settings.format,
        file_path=file_path,
        web_level=web_level,
    )


def _load_config_file(path: str) -> dict[str, Any]:
    source = Path(path)
    suffix = source.suffix.lower()
    text = source.read_text(encoding="utf-8")
    if suffix == ".json":
        loaded = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        import yaml

        loaded = yaml.safe_load(text)
    else:
        raise ValueError("INQTRIX_LOG_CONFIG must name a .json, .yaml or .yml file")
    if not isinstance(loaded, dict):
        raise ValueError("INQTRIX_LOG_CONFIG must contain a dictConfig mapping")
    return loaded


def _references_inqtrix_filter(spec: Any) -> bool:
    if not isinstance(spec, Mapping):
        return False
    factory = spec.get("()")
    return factory is InqtrixLogFilter or factory in _FILTER_TARGETS


def _apply_custom(service: str, config: dict[str, Any], source: str) -> ResolvedLogging:
    config.setdefault("disable_existing_loggers", False)
    filters = config.setdefault("filters", {})
    present = {name for name, spec in filters.items() if _references_inqtrix_filter(spec)}
    for name in present:
        # The process owns its foreign loggers: a filter the configuration
        # declares itself runs with the service too, never in host mode.
        filters[name] = {**filters[name], "service": service}
    if not present:
        filters[_FILTER_NAME] = {"()": InqtrixLogFilter, "service": service}
        present = {_FILTER_NAME}
    handlers = config.setdefault("handlers", {})
    root = config.get("root") or {}
    config["root"] = root
    if not root.get("handlers"):
        # Naming the root removes handlers a library attached there earlier
        # (``logging.basicConfig``), and a root handler keeps records away
        # from Python's unfiltered last-resort handler.
        handlers[_ROOT_NULL_HANDLER] = {"class": "logging.NullHandler"}
        root["handlers"] = [_ROOT_NULL_HANDLER]
    for handler in handlers.values():
        attached = handler.setdefault("filters", [])
        if not present.intersection(attached):
            attached.append(next(iter(present)))
    configured = dict(config.get("loggers", {}))
    logging.config.dictConfig(config)

    notices = logging.getLogger("inqtrix")
    for name, spec in configured.items():
        level = spec.get("level") if isinstance(spec, Mapping) else None
        if level is None or _is_inqtrix_name(name) or _is_web_record(name):
            continue
        if service == "migrate" and _in_family(name, "alembic"):
            continue
        numeric = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
        if isinstance(numeric, int) and numeric < logging.WARNING:
            notices.warning(
                "Logger %s is configured below WARNING; foreign libraries are only shown "
                "from WARNING upwards.",
                name,
            )
    for name in _unknown_inqtrix_names(configured):
        notices.warning(
            "Logger %s matches no Inqtrix module; check the name in the logging configuration.",
            name,
        )
    product = logging.getLogger("inqtrix")
    return ResolvedLogging(
        service=service,
        target="custom",
        level=logging.getLevelName(product.getEffectiveLevel()),
        format="custom",
        file_path=None,
        web_level=logging.getLevelName(logging.getLogger("uvicorn").getEffectiveLevel()),
        config_source=source,
    )


def _is_inqtrix_name(name: str) -> bool:
    return _in_family(name, "inqtrix")


def _unknown_inqtrix_names(names: Mapping[str, Any]) -> list[str]:
    unknown: list[str] = []
    for name in names:
        if name == "inqtrix" or not name.startswith("inqtrix."):
            continue
        if _in_family(name, _GATEWAY_LOGGER):
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            unknown.append(name)
    return unknown


# --------------------------------------------------------------------- #
# Hooks and status line
# --------------------------------------------------------------------- #


def _install_hooks(*, to_stderr_only: bool) -> None:
    """Route warnings and unhandled exceptions through the configured logging.

    The process hook adds :func:`_exit_summary` to its CRITICAL line, so the
    operator reads the diagnosis Inqtrix raised; ``exc`` stays projected. A
    thread crash stays projected: job threads handle user content. With
    target ``none`` nothing is logged, but a crash must stay visible: the hook
    then writes the summary and the projected frames straight to stderr.
    """
    logging.captureWarnings(True)
    product = logging.getLogger("inqtrix")

    def _process_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        summary = (
            _exit_summary(exc)
            if isinstance(exc, BaseException)
            else getattr(exc_type, "__name__", "Exception")
        )
        if to_stderr_only:
            sys.stderr.write(
                f"Unhandled exception; the process stops: {summary}\n"
                + _format_exception_without_arguments((exc_type, exc, tb))
                + "\n"
            )
            return
        product.critical(
            "Unhandled exception; the process stops: %s", summary, exc_info=(exc_type, exc, tb)
        )

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
        if to_stderr_only:
            sys.stderr.write(_format_exception_without_arguments(exc_info) + "\n")
            return
        thread_name = getattr(args.thread, "name", None) or "unknown"
        product.error("Unhandled exception in thread %s.", thread_name, exc_info=exc_info)

    sys.excepthook = _process_hook
    threading.excepthook = _thread_hook


def log_status_line(resolved: ResolvedLogging, *, retired: tuple[Any, ...] | list[Any] = ()) -> None:
    """Write the one INFO line that tells operators how this process logs."""

    names = ",".join(entry.name for entry in retired) or "-"
    logging.getLogger("inqtrix").info(
        "Logging: service=%s target=%s level=%s format=%s file=%s web_level=%s config=%s "
        "retired_settings=%s",
        resolved.service,
        resolved.target,
        resolved.level,
        resolved.format,
        resolved.file_path or "-",
        resolved.web_level,
        resolved.config_source or "-",
        names,
    )
