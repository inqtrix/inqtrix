"""W3C trace-context propagation across the run job queue.

The API injects the current trace context into the durable
``request_payload`` under one top-level key; the worker extracts it and
parents its execution span there — ONE trace from HTTP request through
submit to the worker's LLM calls. The queue message itself stays
untouched (``(run_id, tenant_id)``): the context travels in the run row,
so replays and takeovers see it too.

Every function degrades to a no-op without the ``observability`` extra
or without an active span — old rows, disabled tracing, and mixed
worker versions all keep working (a missing field simply starts a fresh
root span on the worker).
"""

from __future__ import annotations

from typing import Any, Mapping

try:  # Resolved once at import time (see otel.py rationale).
    from opentelemetry import propagate as _otel_propagate
except Exception:  # noqa: BLE001 — extra not installed
    _otel_propagate = None

# Top-level request_payload key. Chosen to be collision-free with the
# existing payload fields (question/history/messages/body) and ignored
# by workers that predate it.
TELEMETRY_FIELD = "telemetry"


def inject_traceparent(payload: dict[str, Any]) -> None:
    """Write the current W3C trace context into *payload* (in place).

    No-op when OpenTelemetry is not installed or no span context is
    active — the payload then simply carries no ``telemetry`` field.
    """
    if _otel_propagate is None:
        return
    carrier: dict[str, str] = {}
    _otel_propagate.inject(carrier)
    if carrier:
        payload[TELEMETRY_FIELD] = carrier


def extract_incoming_context(headers: Mapping[str, str]) -> Any | None:
    """Return the trace context carried by INCOMING request headers.

    Lets an upstream caller's trace continue through Inqtrix instead of
    starting a disconnected one. ``None`` (no header, malformed, or the
    dependency missing) means "start a root span" — never an error.
    """
    if _otel_propagate is None:
        return None
    try:
        return _otel_propagate.extract(dict(headers))
    except Exception:  # noqa: BLE001 — a hostile header must never 500
        return None


def extract_context(payload: Mapping[str, Any] | None) -> Any | None:
    """Return the propagated context from a run payload, or ``None``.

    ``None`` (missing field, invalid carrier, missing dependency) means
    "start a root span" — never an error.
    """
    if _otel_propagate is None:
        return None
    carrier = (payload or {}).get(TELEMETRY_FIELD)
    if not isinstance(carrier, Mapping) or not carrier:
        return None
    try:
        return _otel_propagate.extract(dict(carrier))
    except Exception:  # noqa: BLE001 — a malformed carrier must never
        # crash the caller. The W3C propagators raise TypeError on
        # non-string carrier values (e.g. a version-drifted or
        # hand-edited run row); telemetry then starts a fresh root span
        # instead of wedging the run. Import lazily so a missing logging
        # setup cannot turn this guard into a second failure.
        import logging

        logging.getLogger("inqtrix").warning(
            "Invalid trace context in run payload; starting a root span.",
            exc_info=True,
        )
        return None
