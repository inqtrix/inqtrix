"""Logging for the web gateway in the Inqtrix log format.

The web image ships this package without ``inqtrix``, so the gateway carries a
small copy of the API's log contract (``inqtrix.logging_config``): one console
handler on stdout, JSON or text, a threshold from ``INQTRIX_LOG_LEVEL``.
Exceptions appear as type, stable code and frames; foreign loggers from WARNING
on with their arguments as type names; uvicorn never below INFO and its
WebSocket line without client and query; asyncio with its own words only;
Python warnings with category and location; guest-link tokens and known secret
shapes scrubbed. A crash names the messages the gateway itself raised. A
parity test sends the same records through both copies.

uvicorn applies the mapping in every worker process; the crash hooks and the
capture of warnings exist only in the process that calls
``configure_logging``.
"""

from __future__ import annotations

import dis
import json
import logging as stdlib_logging
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from logging.config import dictConfig
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_FORMATS = ("json", "text")
_SERVICE = "gateway"
_TEXT_FORMAT = "%(asctime)s.%(msecs)03dZ | %(levelname)-7s | %(service)s | %(name)s | %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_GUEST_ROUTE_TOKEN_RE = re.compile(
    r"(?i)(/s/|/v1/editor/share-links/)[^/?#\s\"'<>]+"
)
_PERCENT_ENCODED_BYTE_RE = re.compile(r"%([0-9a-f]{2})", re.IGNORECASE)

_WEBSOCKET_TEMPLATE_PREFIX = '%s - "WebSocket %s"'
_WARNING_LOCATION_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<category>[A-Za-z_][\w.]*):")
_ASYNCIO_VALUE_START_RE = re.compile(r"[<'\"\[{]")

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
_TRACEBACK_CHAIN_LINES = frozenset({
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
})
_PACKAGE_DIR = Path(__file__).resolve().parent
_RAISE_OPCODE = dis.opmap["RAISE_VARARGS"]

# Secret shapes, as the API scrubs them (``inqtrix.urls``).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_URL_PASSWORD_RE = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s\"'<>@:/]*:[^\s\"'<>]*@"
)
_URL_USERINFO_RE = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^@/\s\"'<>]*@")
_API_KEY_RE = re.compile(r"(sk-|pplx-)[a-zA-Z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*")
_AWS_KEY_RE = re.compile(r"AKIA[A-Z0-9]{16}")
_AWS_SECRET_RE = re.compile(r"(?i)(aws_secret_access_key|aws_session_token)[=:]\s*\S+")
_KEYWORD_PASSWORD_RE = re.compile(
    r"(?i)\b(password|passwd|pwd)\s*=\s*('[^']*'|\"[^\"]*\"|[^\s'\"]+)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:_SECRET|_PASSWORD|_PASSWD|_TOKEN|_PEPPER|_KEY))"
    r"(\s*[=:]\s*)('[^']*'|\"[^\"]*\"|[^\s'\"]+)"
)
_CREDENTIAL_KEY_EXACT = frozenset({
    "apikey", "xapikey", "key", "accesskey", "accesskeyid", "awsaccesskeyid",
    "subscriptionkey", "clientkey", "privatekey", "token", "accesstoken",
    "authtoken", "bearertoken", "idtoken", "refreshtoken", "sessiontoken",
    "secret", "clientsecret", "apisecret", "signature", "sig", "xamzsignature",
    "xgoogsignature", "password", "passwd", "pwd", "credential", "xamzcredential",
})
_CREDENTIAL_KEY_MARKERS = (
    "apikey", "accesskey", "privatekey", "subscriptionkey", "accesstoken",
    "authtoken", "bearertoken", "idtoken", "refreshtoken", "sessiontoken",
    "secret", "signature", "password", "passwd", "credential",
)


# --------------------------------------------------------------------- #
# Guest-link tokens and secret shapes
# --------------------------------------------------------------------- #


def _normalize_route_match_characters(value: str) -> str:
    """Decode only URI characters needed to classify a sensitive route.

    httpx retains percent-encoded reserved separators such as ``%2F`` in its
    request log. Decoding the full string would allow encoded controls to
    inject log lines, so this deliberately normalizes only ASCII unreserved
    characters plus slash.
    """

    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        if character.isascii() and (
            character.isalnum() or character in "-._~/"
        ):
            return character
        return match.group(0)

    return _PERCENT_ENCODED_BYTE_RE.sub(replace, value)


def _redact_guest_route_tokens(value: str) -> str:
    """Redact account-less bearer tokens while retaining route diagnostics."""
    value = _normalize_route_match_characters(value)
    return _GUEST_ROUTE_TOKEN_RE.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        value,
    )


def _is_credential_query_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    if not compact:
        return False
    return compact in _CREDENTIAL_KEY_EXACT or any(
        marker in compact for marker in _CREDENTIAL_KEY_MARKERS
    )


def _redact_credential_url(url: str) -> str:
    """*url* without userinfo and with the values of credential parameters replaced."""
    try:
        split = urlsplit(str(url).strip())
        host = split.hostname or ""
        if not host:
            return "[REDACTED_URL]"
        host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if split.port is not None:
            host_text += f":{split.port}"

        def redact_parameters(value: str) -> str:
            parts = re.split(r"([&;])", value)
            for index in range(0, len(parts), 2):
                raw_key, separator, _raw_value = parts[index].partition("=")
                if separator and _is_credential_query_key(unquote_plus(raw_key)):
                    parts[index] = f"{raw_key}=[REDACTED]"
            return "".join(parts)

        query = redact_parameters(split.query)
        fragment = redact_parameters(split.fragment)
        return urlunsplit((split.scheme, host_text, split.path, query, fragment))
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def _scrub(text: str) -> str:
    """Guest-link tokens and known secret shapes out of rendered text."""
    text = _redact_guest_route_tokens(text)
    text = _URL_IN_TEXT_RE.sub(lambda match: _redact_credential_url(match.group(0)), text)
    text = _URL_PASSWORD_RE.sub(r"\1[REDACTED]@", text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _API_KEY_RE.sub("[KEY]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _AWS_KEY_RE.sub("[AWS_KEY]", text)
    text = _AWS_SECRET_RE.sub(r"\1=[REDACTED]", text)
    text = _KEYWORD_PASSWORD_RE.sub(r"\1=[REDACTED]", text)
    return _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", text)


# --------------------------------------------------------------------- #
# Exceptions as type, code and frames
# --------------------------------------------------------------------- #


def _safe_exception_code(exc: BaseException) -> tuple[str, str] | None:
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
    code = _safe_exception_code(exc)
    if code is None:
        return type(exc).__name__
    key, value = code
    return f"{type(exc).__name__}[{key}={value}]"


def _project_exception_values(value: Any) -> Any:
    if isinstance(value, BaseException):
        return _exception_descriptor(value)
    if isinstance(value, Mapping):
        return {key: _project_exception_values(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_project_exception_values(item) for item in value)
    if isinstance(value, list):
        return [_project_exception_values(item) for item in value]
    return value


def _exception_chain(
    exc: BaseException, tb: Any, *, seen: set[int] | None = None
) -> list[tuple[BaseException, Any, str | None]]:
    """The cause/context chain from the root to *exc*, with separators."""
    visited = seen if seen is not None else set()
    if id(exc) in visited:
        return []
    visited.add(id(exc))
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
    exc_type, exc, tb = exc_info
    if not isinstance(exc, BaseException):
        return str(getattr(exc_type, "__name__", "Exception"))
    lines: list[str] = []
    for current, current_tb, separator in _exception_chain(exc, tb):
        if separator is not None:
            lines.extend(("", separator, ""))
        if current_tb is not None:
            lines.append("Traceback (most recent call last):")
            for frame in traceback.extract_tb(current_tb):
                lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
        lines.append(_exception_descriptor(current))
    return "\n".join(lines)


def _project_preformatted_traceback(value: str) -> str:
    """A traceback formatted elsewhere, reduced to headers, frames and types."""
    safe_lines: list[str] = []
    for line in str(value).splitlines():
        stripped = line.strip()
        if stripped in _TRACEBACK_HEADERS or stripped in _TRACEBACK_CHAIN_LINES:
            safe_lines.append(stripped)
        elif _TRACEBACK_FRAME_RE.fullmatch(line):
            safe_lines.append(line)
        else:
            descriptor = stripped.partition(":")[0]
            if _SAFE_TRACEBACK_TYPE_RE.fullmatch(descriptor):
                safe_lines.append(descriptor)
    return "\n".join(safe_lines) or "Exception"


def _project_traceback_in_text(text: str) -> str:
    starts = [index for index in (text.find(header) for header in _TRACEBACK_HEADERS) if index >= 0]
    if not starts:
        return text
    start = min(starts)
    return text[:start] + _project_preformatted_traceback(text[start:])


def _project_foreign_value(value: Any) -> Any:
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


def _asyncio_words(line: str) -> str:
    """The words asyncio wrote itself, before the first value in *line*."""
    return _ASYNCIO_VALUE_START_RE.split(line, maxsplit=1)[0].rstrip(" (:")


def _in_family(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def _strip_query(target: Any) -> str:
    return _scrub(str(target).split("?", 1)[0])


# --------------------------------------------------------------------- #
# Filter and formatters
# --------------------------------------------------------------------- #


class GatewayLogFilter(stdlib_logging.Filter):
    """Apply the log content rule to every record of the gateway process."""

    def filter(self, record: stdlib_logging.LogRecord) -> bool:
        decided = record.__dict__.get("_gateway_decision")
        if decided is not None:
            return bool(decided)
        record.service = _SERVICE
        try:
            decision = self._decide(record)
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            record.msg = f"{record.name}: log record could not be projected"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            decision = True
        record._gateway_decision = decision
        return decision

    def _decide(self, record: stdlib_logging.LogRecord) -> bool:
        name = record.name
        if _in_family(name, "uvicorn") and record.levelno < stdlib_logging.INFO:
            # On DEBUG the WebSocket library writes every frame through
            # ``uvicorn.error``: document content. Never shown.
            return False
        if _in_family(name, "inqtrix") or _in_family(name, "uvicorn") or _in_family(name, "fastapi"):
            if name == "uvicorn.error":
                self._project_websocket(record)
            self._project_own(record)
            return True
        if name == "py.warnings":
            self._project_warning(record)
            return True
        if record.levelno < stdlib_logging.WARNING:
            return False
        self._project_foreign(record)
        if _in_family(name, "asyncio"):
            first_line = record.getMessage().splitlines()[0] if record.msg else ""
            record.msg = _asyncio_words(first_line)
        return True

    @staticmethod
    def _render(record: stdlib_logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken template must not raise
            message = f"{record.name}: log message could not be formatted"
        record.msg = _scrub(message)
        record.args = ()

    @staticmethod
    def _project_tracebacks(record: stdlib_logging.LogRecord) -> None:
        if record.exc_info:
            record.exc_text = _format_exception_without_arguments(record.exc_info)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _project_preformatted_traceback(record.exc_text)
        if record.stack_info:
            record.stack_info = _project_preformatted_traceback(record.stack_info)

    def _project_own(self, record: stdlib_logging.LogRecord) -> None:
        record.msg = _project_exception_values(record.msg)
        record.args = _project_exception_values(record.args)
        self._render(record)
        record.msg = _project_traceback_in_text(record.msg)
        self._project_tracebacks(record)

    def _project_foreign(self, record: stdlib_logging.LogRecord) -> None:
        if isinstance(record.msg, BaseException):
            record.msg = _exception_descriptor(record.msg)
        elif not isinstance(record.msg, str):
            record.msg = f"<{type(record.msg).__name__}>"
        record.args = _project_foreign_args(record.args)
        self._render(record)
        record.msg = _project_traceback_in_text(record.msg)
        self._project_tracebacks(record)

    @staticmethod
    def _project_websocket(record: stdlib_logging.LogRecord) -> None:
        if (
            isinstance(record.msg, str)
            and record.msg.startswith(_WEBSOCKET_TEMPLATE_PREFIX)
            and isinstance(record.args, tuple)
            and len(record.args) >= 2
        ):
            record.args = ("-", _strip_query(record.args[1]), *record.args[2:])

    def _project_warning(self, record: stdlib_logging.LogRecord) -> None:
        text = record.msg if isinstance(record.msg, str) else ""
        match = _WARNING_LOCATION_RE.match(text)
        if match is None:
            record.msg = "Python warning without a parsable location"
        else:
            record.msg = f"{match.group('category')} at {match.group('file')}:{match.group('line')}"
        record.args = ()
        self._project_tracebacks(record)


def _timestamp(created: float) -> str:
    rendered = datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="milliseconds")
    return rendered.replace("+00:00", "Z")


class GatewayJsonFormatter(stdlib_logging.Formatter):
    """One JSON object per record, with the API's field names."""

    def format(self, record: stdlib_logging.LogRecord) -> str:
        payload: dict[str, Any] = {"ts": _timestamp(record.created), "level": record.levelname}
        service = getattr(record, "service", None)
        if service:
            payload["service"] = service
        payload.update(
            {
                "logger": record.name,
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
                "message": record.getMessage(),
            }
        )
        event = record.__dict__.get("event")
        if event:
            payload["event"] = str(event)
        payload["thread"] = record.threadName
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = getattr(record.exc_info[0], "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=False, default=str)


class GatewayTextFormatter(stdlib_logging.Formatter):
    """One readable line per record in UTC."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT, defaults={"service": "-"})


# --------------------------------------------------------------------- #
# Configuration and hooks
# --------------------------------------------------------------------- #


def _setting(name: str, allowed: tuple[str, ...], default: str, *, upper: bool) -> str:
    """*name* from the environment; empty is the default, any other value must be allowed."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    value = raw.upper() if upper else raw.lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}.")
    return value


def gateway_log_config() -> dict[str, Any]:
    """The ``dictConfig`` mapping of the gateway process.

    uvicorn receives it as ``log_config`` and applies it again in every
    worker process it spawns, so it holds import paths of this package only.
    """
    level = _setting("INQTRIX_LOG_LEVEL", _LEVELS, "INFO", upper=True)
    log_format = _setting("INQTRIX_LOG_FORMAT", _FORMATS, "json", upper=False)
    web_level = level if stdlib_logging.getLevelName(level) >= stdlib_logging.INFO else "INFO"
    formatter = "GatewayJsonFormatter" if log_format == "json" else "GatewayTextFormatter"
    web_logger = {"level": web_level, "handlers": [], "propagate": True}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"content": {"()": f"{__name__}.GatewayLogFilter"}},
        "formatters": {"gateway": {"()": f"{__name__}.{formatter}"}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["content"],
                "formatter": "gateway",
            }
        },
        "loggers": {
            "inqtrix": {"level": level},
            "uvicorn": dict(web_logger),
            "uvicorn.error": dict(web_logger),
            "fastapi": dict(web_logger),
        },
        "root": {"level": "WARNING", "handlers": ["console"]},
    }


def _raised_by_gateway(exc: BaseException) -> bool:
    """Whether gateway code raised *exc* with an explicit ``raise``.

    The innermost frame decides: its file lies in this package and its last
    instruction is a ``raise``. A C function the gateway calls (``int()``)
    puts the value into its message and does not count.
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


def _exit_entry(exc: BaseException) -> str:
    descriptor = _exception_descriptor(exc)
    if _raised_by_gateway(exc):
        return f"{descriptor}: {_scrub(str(exc))}"
    return descriptor


def _exit_summary(exc: BaseException) -> str:
    """The chain of an exception that ends the process, outermost first."""
    try:
        chain = _exception_chain(exc, exc.__traceback__)
        return "; caused by ".join(_exit_entry(current) for current, _tb, _sep in reversed(chain))
    except Exception:  # noqa: BLE001 - the hook must always write its line
        return _exception_descriptor(exc)


def _install_hooks() -> None:
    stdlib_logging.captureWarnings(True)
    gateway = stdlib_logging.getLogger("inqtrix.web_gateway")

    def _process_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        summary = (
            _exit_summary(exc)
            if isinstance(exc, BaseException)
            else getattr(exc_type, "__name__", "Exception")
        )
        gateway.critical(
            "Unhandled exception; the process stops: %s", summary, exc_info=(exc_type, exc, tb)
        )

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        thread_name = getattr(args.thread, "name", None) or "unknown"
        gateway.error(
            "Unhandled exception in thread %s.",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _process_hook
    threading.excepthook = _thread_hook


def configure_logging() -> None:
    """Configure the gateway process: the mapping above, warnings and crash hooks."""
    dictConfig(gateway_log_config())
    _install_hooks()


class AccessLogMiddleware:
    """Emit sanitized HTTP access lines with method, status, and duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # The path percent-encoded, as uvicorn writes its own access
            # line: a line break in it cannot end the line.
            stdlib_logging.getLogger("inqtrix.web_gateway.access").info(
                "%s %s %d %.1fms",
                scope.get("method") or "-",
                _redact_guest_route_tokens(quote(str(scope.get("path") or "/"))),
                status_code,
                (time.perf_counter() - started) * 1000,
            )
