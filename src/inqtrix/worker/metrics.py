"""Worker-process Prometheus exposition.

Each worker exposes its OWN registry on ``INQTRIX_WORKER_METRICS_PORT``
(no pushgateway — Prometheus scrapes every process). Definitions come
from :mod:`inqtrix.observability.metrics_defs`, so the series are
identical to the API server's; the shared holder makes the provider
wrappers, loops, and retrieval timers feed them without plumbing.

Contract mirrors ``server/metrics.py``: off by default (port 0 or
``INQTRIX_METRICS_ENABLED=false``), a missing ``metrics`` extra logs one
WARNING and stays off — never a startup crash. ``start_http_server``
runs daemon threads (prometheus_client ≥0.20), matching the worker's
os._exit shutdown paths.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("inqtrix")

# One warning per process for the half-configured case below; a claim
# loop restarting the holder must not turn the hint into log spam.
_off_while_enabled_warned = False


def start_worker_metrics(settings: Any) -> bool:
    """Start the worker /metrics endpoint; returns True when serving."""
    global _off_while_enabled_warned
    port = int(settings.queue.worker_metrics_port or 0)
    if port <= 0:
        # Metrics off everywhere is the documented default and stays
        # silent. Metrics ON while the worker port is 0 is a different
        # thing: the operator asked for observability, and the process
        # that runs every job would silently contribute nothing.
        if settings.server.metrics_enabled and not _off_while_enabled_warned:
            _off_while_enabled_warned = True
            log.warning(
                "INQTRIX_METRICS_ENABLED=true, but INQTRIX_WORKER_METRICS_PORT=0; this "
                "worker exports no metrics. Run, LLM, search and retrieval counts from "
                "jobs are missing from monitoring even when API series look complete. "
                "Set a port > 0 (e.g. 9091) and scrape each worker process, or "
                "deliberately enable INQTRIX_METRICS_ENABLED=true only for the API."
            )
        return False
    if not settings.server.metrics_enabled:
        log.warning(
            "INQTRIX_WORKER_METRICS_PORT=%d is set, but INQTRIX_METRICS_ENABLED=false; "
            "worker /metrics remains disabled.",
            port,
        )
        return False
    try:
        from prometheus_client import CollectorRegistry, start_http_server
    except ImportError:
        log.warning(
            "INQTRIX_WORKER_METRICS_PORT is set, but the optional 'metrics' extra is "
            "missing (prometheus-client). Worker /metrics remains disabled."
        )
        return False

    from inqtrix.observability.metrics_defs import (
        build_call_metrics,
        set_active_metrics,
    )

    registry = CollectorRegistry()
    try:
        start_http_server(port, registry=registry)
    except OSError as exc:
        # An observability add-on must never take the worker down; the
        # holder stays unset so no series pretend to be scrapeable.
        log.error(
            "Worker /metrics could not bind port %d (%s); metrics remain disabled, but "
            "the worker will start.",
            port,
            exc,
        )
        return False
    set_active_metrics(build_call_metrics(registry))
    log.info(
        "Worker metrics enabled: http://0.0.0.0:%d/metrics (dedicated registry for this"
        " process).",
        port,
    )
    return True
