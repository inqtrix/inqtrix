"""Structured JSON log formatter (``INQTRIX_LOG_FORMAT=json``).

One line per record, machine-readable (Loki/SIEM/jq), with the
correlation fields from :mod:`inqtrix.observability.context` and — once
tracing is active — the current OTel trace/span ids, so a log line and
its trace join on ``trace_id`` without any lookup.

Redaction layering: the ``_RedactSecretsFilter`` in ``logging_config``
runs BEFORE any formatter and scrubs ``message``/``exc``. JSON is built
AFTER the filter, so a URL regex cannot break JSON delimiters here.
As defense in depth the formatter additionally runs
``sanitize_log_message`` over every context/extra string value —
structured fields never pass through the record's message path, so they
need their own scrub.

Field contract: ``ts`` (RFC3339 UTC), ``level``,
``logger``, ``event``, ``message``, ``trace_id``/``span_id`` (whenever a
span context is active — in ``local`` mode spans are drop-sampled and
non-recording but still carry valid ids exactly for this correlation),
``request_id``, ``run_id``, ``user``, ``workspace``, ``tenant`` (when
bound), ``thread``, ``exc`` (when present), plus the sanitized payload
of structured runtime events (``inqtrix_payload`` from
``emit_runtime_event``) merged as real JSON fields.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from inqtrix.observability.context import current_log_context
from inqtrix.urls import sanitize_log_message

try:  # Resolve the optional dependency ONCE at import time — this runs
    # per log line, and a failing per-call import costs ~40 µs each in a
    # tracing-off deployment (matches otel.py/propagation.py).
    from opentelemetry import trace as _otel_trace
except Exception:  # noqa: BLE001 — extra not installed
    _otel_trace = None

# Keys the formatter itself owns; a structured payload must not clobber
# them (its colliding keys are namespaced under "payload_<key>").
_RESERVED_KEYS = frozenset(
    {
        "ts",
        "level",
        "logger",
        "event",
        "message",
        "trace_id",
        "span_id",
        "request_id",
        "run_id",
        "user",
        "workspace",
        "tenant",
        "thread",
        "exc",
    }
)


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


def _sanitize_value(value: Any) -> Any:
    """Defense-in-depth scrub for structured (non-message) values."""
    if isinstance(value, str):
        return sanitize_log_message(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


class InqtrixJsonFormatter(logging.Formatter):
    """Render one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "inqtrix_event", "log"),
            # The redaction filter has already scrubbed msg/args (and
            # eagerly rendered the message), so getMessage() is safe.
            "message": record.getMessage(),
        }
        trace_ids = current_trace_ids()
        if trace_ids is not None:
            payload["trace_id"], payload["span_id"] = trace_ids
        for name, value in current_log_context().items():
            payload[name] = sanitize_log_message(value)
        payload["thread"] = record.threadName

        structured = getattr(record, "inqtrix_payload", None)
        if isinstance(structured, dict):
            for key, value in structured.items():
                target = str(key)
                if target in _RESERVED_KEYS:
                    target = f"payload_{target}"
                payload[target] = _sanitize_value(value)

        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            # The redaction filter normally converts exc_info to a
            # scrubbed exc_text; records emitted around it (foreign
            # handlers) still get their traceback — scrubbed here.
            payload["exc"] = sanitize_log_message(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False, default=str)
