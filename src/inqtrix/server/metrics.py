"""Optional Prometheus ``/metrics`` endpoint (scaling measure 2.5).

Off by default (``INQTRIX_METRICS_ENABLED``). Only ``prometheus-client``
is required — it ships in the optional ``metrics`` extra; when the flag
is on but the extra is not installed, this module logs a loud WARNING
and leaves ``/metrics`` unmounted rather than crashing startup (a
missing OPTIONAL dependency is an operator misconfiguration to surface,
not a fatal one — the visibility lives in the log).

The HTTP request histograms come from a small in-tree ASGI middleware
rather than ``prometheus-fastapi-instrumentator`` on purpose: that
package pins ``starlette<1.0`` and would force a project-wide starlette
downgrade for an optional feature. Labelling by the matched route
TEMPLATE (``scope["route"].path`` = ``/v1/runs/{run_id}``) keeps the
cardinality bounded without it.

Metric surface (NO run-id, subject, or session labels ever):

* ``inqtrix_run_queue_depth`` / ``inqtrix_run_active`` /
  ``inqtrix_run_capacity`` — gauges read at scrape time from the run
  store's read-only :class:`~inqtrix.runs.ports.RunStoreMetrics`
  snapshot (capacity only for the in-process memory backend).
* ``inqtrix_run_admission_rejected_total{reason}`` — a counter bumped
  at the run/quota admission-rejection sites via
  :func:`record_admission_rejected`.
* ``inqtrix_http_requests_total{method,handler,status}`` and
  ``inqtrix_http_request_duration_seconds{method,handler}`` — per-route
  request count and latency, keyed on the route template.

Each ``create_app`` gets its OWN ``CollectorRegistry``, so repeated app
construction in tests never trips prometheus' duplicate-timeseries guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Request, Response

from inqtrix.auth.api_key import build_bearer_guard

if TYPE_CHECKING:
    from fastapi import FastAPI

    from inqtrix.server.container import AppContainer
    from inqtrix.settings import Settings

log = logging.getLogger("inqtrix")

# The reasons a run/quota admission is rejected BEFORE any cost is paid.
# Pre-initialised to 0 so the series exist for rate() from the first
# scrape. Kept in sync with the call sites of record_admission_rejected;
# "draining" is intentionally absent until the graceful-drain measure
# (2.4) introduces that rejection path.
_ADMISSION_REASONS = (
    "queue_full",
    "per_user_limit",
    "quota",
    "knowledge_store_unavailable",
)

# Latency buckets (seconds) spanning fast JSON reads to long SSE polls.
_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

# A scrape slower than this is worth explaining in the log: below it the split
# between waiting for a thread and collecting is noise, above it something is
# genuinely contended and the operator needs to know which side.
_SLOW_SCRAPE_SECONDS = 1.0

# Set by setup_metrics when metrics are enabled AND the extra is present;
# None otherwise, which makes record_admission_rejected a no-op. Process-
# global because the increment sites (quota_error_response, the run-cap
# handlers) are backend-neutral helpers with no app handle to reach a
# per-app counter.
#
# ASSUMES one metrics-enabled app per process -- the real serving topology
# (a single create_app per uvicorn process). setup_metrics resets this to
# None first, so building a metrics-OFF app cleanly disarms a prior one, and
# the test suite (sequential apps) is unaffected. If a process ever hosts
# TWO metrics-enabled apps at once, the last setup_metrics wins the global
# and admissions rejected by the other app would land in this counter's
# registry -- revisit (hang the counter off app.state, threading request
# through quota_admission's 8 callers) before adopting a multi-app-per-
# process server.
_admission_counter: Any = None


def record_admission_rejected(reason: str) -> None:
    """Count one pre-queue admission rejection (no-op when metrics off).

    Called from the run-cap handlers and the shared quota-rejection
    responder. A no-op unless :func:`setup_metrics` wired a counter, so
    the call sites never branch on whether metrics are enabled. See the
    module note above on the single-app-per-process assumption.
    """
    counter = _admission_counter
    if counter is not None:
        counter.labels(reason=reason).inc()


class _RunStoreCollector:
    """Prometheus custom collector: reads the run store at scrape time.

    Registered per app so it reflects THIS app's run store. A store read
    that raises is downgraded to a WARNING and an empty result — a
    metrics scrape must never take the store or the endpoint down.
    """

    def __init__(self, run_store: Any, gauge_family: Any) -> None:
        self._run_store = run_store
        self._gauge = gauge_family

    def collect(self):  # noqa: ANN201 (prometheus collector protocol)
        try:
            snapshot = self._run_store.metrics_snapshot()
        except Exception:  # noqa: BLE001 — scrape must not crash the store
            log.warning("run-store metrics snapshot failed", exc_info=True)
            return
        yield self._gauge(
            "inqtrix_run_queue_depth",
            "Native runs waiting in the queue (status=queued).",
            value=snapshot.queued,
        )
        yield self._gauge(
            "inqtrix_run_active",
            "Native runs currently executing (status=running).",
            value=snapshot.active,
        )
        if snapshot.capacity is not None:
            yield self._gauge(
                "inqtrix_run_capacity",
                "In-process concurrent run capacity (memory backend only; "
                "the durable backend's capacity is owned by the worker fleet).",
                value=snapshot.capacity,
            )


class _HttpMetricsMiddleware:
    """Record per-route request count and latency, keyed by template.

    Pure ASGI (no ``BaseHTTPMiddleware`` streaming pitfalls). The matched
    route is read from ``scope["route"]`` AFTER the inner app routes the
    request, so the ``handler`` label is the path TEMPLATE
    (``/v1/runs/{run_id}``); unmatched paths collapse to a single
    ``__unmatched__`` bucket — no id cardinality either way.
    """

    def __init__(self, app: Any, *, counter: Any, histogram: Any) -> None:
        self.app = app
        self._counter = counter
        self._histogram = histogram

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "UNKNOWN")
        status = {"code": 500}

        async def send_wrapper(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            handler = getattr(route, "path", None) or "__unmatched__"
            self._counter.labels(
                method=method, handler=handler, status=str(status["code"])
            ).inc()
            self._histogram.labels(method=method, handler=handler).observe(
                elapsed
            )


def setup_metrics(
    app: "FastAPI", *, container: "AppContainer", settings: "Settings"
) -> bool:
    """Mount ``/metrics`` when enabled and the extra is installed.

    Idempotent per app-construction: always resets the process-global
    admission counter first, so an app built with metrics OFF (the test
    default) cleanly disarms any counter a previous app armed.

    Returns:
        ``True`` when the endpoint was mounted, ``False`` otherwise
        (disabled, or the optional extra missing). The boolean is for
        callers/tests; the operator-facing signal is the WARNING log.
    """
    global _admission_counter
    _admission_counter = None
    # Repeated create_app in tests: clear the shared holder so a
    # disabled app never records into a previous app's registry.
    from inqtrix.observability.metrics_defs import set_active_metrics

    set_active_metrics(None)

    server = settings.server
    if not server.metrics_enabled:
        return False

    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Counter,
            Histogram,
            generate_latest,
        )
        from prometheus_client.core import GaugeMetricFamily
    except ImportError:
        log.warning(
            "INQTRIX_METRICS_ENABLED is set, but the optional 'metrics' extra is "
            "missing (prometheus-client). /metrics remains disabled; install the extra "
            "or disable the flag."
        )
        return False

    registry = CollectorRegistry()

    run_store = getattr(container, "run_store", None)
    if run_store is not None:
        registry.register(_RunStoreCollector(run_store, GaugeMetricFamily))

    # Shared call metrics: the provider wrappers, retrieval
    # timers, and indexing counters reach these instruments through the
    # process holder — one definition module, per-process registry.
    from inqtrix.observability.metrics_defs import build_call_metrics

    set_active_metrics(build_call_metrics(registry))

    _admission_counter = Counter(
        "inqtrix_run_admission_rejected_total",
        "Run/quota admissions rejected before entering the queue, by reason.",
        ["reason"],
        registry=registry,
    )
    for reason in _ADMISSION_REASONS:
        _admission_counter.labels(reason=reason)

    http_requests = Counter(
        "inqtrix_http_requests",
        "HTTP requests by method, route template, and status.",
        ["method", "handler", "status"],
        registry=registry,
    )
    http_duration = Histogram(
        "inqtrix_http_request_duration_seconds",
        "HTTP request latency by method and route template.",
        ["method", "handler"],
        buckets=_LATENCY_BUCKETS,
        registry=registry,
    )
    app.add_middleware(
        _HttpMetricsMiddleware, counter=http_requests, histogram=http_duration
    )

    expected = (server.api_key or "").strip()
    guard = build_bearer_guard(expected) if expected else None

    async def metrics_endpoint(request: Request) -> Response:
        if guard is not None:
            guard(request)
        # generate_latest runs the custom collectors synchronously, and
        # the run-store gauges do a real Postgres round-trip. On the
        # event loop a slow or hung database would stall EVERY request,
        # not just the DB-bound ones — a scrape must never be able to do
        # that, so it runs off-loop.
        #
        # A slow scrape has two possible causes that look identical from
        # outside: no thread was free, or the collectors themselves were
        # slow. Timing both sides of the handoff separates them, so the
        # answer is in the log instead of being guessed at.
        submitted = time.monotonic()
        started: list[float] = []

        def _collect() -> bytes:
            started.append(time.monotonic())
            return generate_latest(registry)

        payload = await asyncio.to_thread(_collect)
        finished = time.monotonic()
        entered = started[0] if started else finished
        waited, worked = entered - submitted, finished - entered
        if waited + worked >= _SLOW_SCRAPE_SECONDS:
            log.warning(
                "Prometheus scrape took %.2fs: %.2fs waiting for a thread, %.2fs "
                "collecting metrics (including database reads).",
                waited + worked,
                waited,
                worked,
            )
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    app.add_api_route(
        "/metrics",
        metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
    log.info(
        "Prometheus /metrics enabled%s.",
        " (Bearer-protected)" if guard is not None else " (unprotected)",
    )
    return True
