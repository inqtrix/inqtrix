"""Structured JSON log formatter (``INQTRIX_LOG_FORMAT=json``, the default).

One line per record, machine-readable (Loki, SIEM, jq). The formatter reads
record attributes only: :class:`~inqtrix.logging_config.InqtrixLogFilter`
stamps the correlation fields onto the record in the thread that logged it
and projects message, arguments and exceptions before any formatter runs, so
a record keeps its correlation wherever it is formatted.

Field contract: ``ts`` (RFC 3339 UTC with milliseconds and ``Z``), ``level``,
``service``, ``logger``, ``module``, ``function``, ``line``, ``message``,
``event`` (only when a call passed ``extra={"event": ...}``), ``request_id``,
``run_id``, ``trace_id``, ``span_id``, ``user`` and ``tenant`` (pseudonyms;
each only when bound), ``thread`` and ``exc`` (projected, when present).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

try:  # Resolve the optional dependency ONCE at import time — the filter
    # calls current_trace_ids per log record.
    from opentelemetry import trace as _otel_trace
except Exception:  # noqa: BLE001 — extra not installed
    _otel_trace = None

_CORRELATION_FIELDS = ("request_id", "run_id", "trace_id", "span_id", "user", "tenant")


def current_trace_ids() -> tuple[str, str] | None:
    """Return (trace_id, span_id) of the active span context, if any.

    Valid ids are enough — the span need not be recording (``local``
    mode drop-samples every span on purpose). Without the
    ``observability`` extra the module-level import above resolved to
    ``None`` and log lines simply omit the ids.
    """
    if _otel_trace is None:
        return None
    span = _otel_trace.get_current_span()
    span_context = span.get_span_context()
    if not span_context.is_valid:
        return None
    return (
        format(span_context.trace_id, "032x"),
        format(span_context.span_id, "016x"),
    )


def _timestamp(created: float) -> str:
    rendered = datetime.fromtimestamp(created, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )
    return rendered.replace("+00:00", "Z")


class InqtrixJsonFormatter(logging.Formatter):
    """Render one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
        }
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
        for name in _CORRELATION_FIELDS:
            value = getattr(record, name, None)
            if value:
                payload[name] = value
        payload["thread"] = record.threadName
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            # Only reached when no InqtrixLogFilter projected the record: show
            # the exception type, never its message or source lines.
            exc_type = record.exc_info[0]
            payload["exc"] = getattr(exc_type, "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=False, default=str)
