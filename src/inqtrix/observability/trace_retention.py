"""Time-based trace retention against Langfuse (worker job).

Native retention is EE-only in self-hosted Langfuse, so Inqtrix ships
its own cleanup: list traces older than ``INQTRIX_TRACE_RETENTION_DAYS``
via ``GET /api/public/traces?toTimestamp=…`` (cursor pages) and delete
them in batches via ``DELETE /api/public/traces`` (body ``traceIds``).
Langfuse processes deletions asynchronously (~15 min) — the job reports
what it REQUESTED; a later pass simply finds fewer rows.

Auth and base URL come from the same OTLP variables the exporter uses
(see :mod:`inqtrix.observability.trace_readers`) — no second credential
set. The job only runs in ``otlp`` mode; the file spool has its own
size cap and ``off``/``local`` record nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger("inqtrix")

_PAGE_LIMIT = 100
_DELETE_BATCH = 100
# Safety valve per pass: a first activation on a big backlog trickles
# instead of hammering ClickHouse; the entry point drains saturated
# passes back-to-back (bounded), so throughput still converges.
_MAX_DELETES_PER_RUN = 5_000
# A misbehaving server could advertise endless pages of id-less rows;
# never spend more than this many list calls in one pass.
_MAX_LIST_PAGES = 200
# Back-to-back passes per job run while saturated (see run_trace_retention).
_MAX_DRAIN_PASSES = 10


@dataclass(frozen=True)
class RetentionReport:
    """What one retention pass saw and requested."""

    cutoff_iso: str
    scanned: int
    delete_requested: int
    failed_batches: int
    requested_ids: tuple[str, ...] = ()


def prune_old_traces(
    *,
    base_url: str,
    authorization: str,
    retention_days: int,
    get: Callable[..., Any],
    delete: Callable[..., Any],
    now: datetime | None = None,
    exclude: frozenset[str] = frozenset(),
) -> RetentionReport:
    """One retention pass; ``get``/``delete`` are httpx-shaped callables.

    Never raises for HTTP-level failures — a telemetry cleanup must not
    take the worker down; failures surface as WARNING lines and in the
    report. ``exclude`` carries ids whose deletion an earlier pass in
    the same run already requested: Langfuse deletes ASYNCHRONOUSLY, so
    a re-list still returns them and without the exclusion a drain loop
    would re-request the same ids instead of reaching fresh backlog.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=retention_days)
    cutoff_iso = (
        cutoff.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    headers = {"Authorization": authorization}
    trace_ids: list[str] = []
    page = 1
    while (
        len(trace_ids) < _MAX_DELETES_PER_RUN and page <= _MAX_LIST_PAGES
    ):
        response = get(
            f"{base_url}/api/public/traces",
            params={
                "toTimestamp": cutoff_iso,
                "page": page,
                "limit": _PAGE_LIMIT,
            },
            headers=headers,
        )
        status = getattr(response, "status_code", 0)
        if not 200 <= status < 300:
            log.warning(
                "Trace retention: Langfuse list returned HTTP %s; pass aborted.",
                status,
            )
            return RetentionReport(cutoff_iso, len(trace_ids), 0, 1)
        body = response.json()
        rows = body.get("data") or []
        trace_ids.extend(
            str(row.get("id"))
            for row in rows
            if row.get("id") and str(row.get("id")) not in exclude
        )
        meta = body.get("meta") or {}
        total_pages = int(meta.get("totalPages") or 1)
        if page >= total_pages or not rows:
            break
        page += 1
    trace_ids = trace_ids[:_MAX_DELETES_PER_RUN]
    if not trace_ids:
        return RetentionReport(cutoff_iso, 0, 0, 0)
    requested_ids = tuple(trace_ids)

    requested = 0
    failed_batches = 0
    for start in range(0, len(trace_ids), _DELETE_BATCH):
        batch = trace_ids[start : start + _DELETE_BATCH]
        response = delete(
            f"{base_url}/api/public/traces",
            json={"traceIds": batch},
            headers=headers,
        )
        status = getattr(response, "status_code", 0)
        if 200 <= status < 300:
            requested += len(batch)
        else:
            failed_batches += 1
            log.warning(
                "Trace retention: batch deletion returned HTTP %s (%d traces remain "
                "until the next pass).",
                status,
                len(batch),
            )
    if requested:
        log.info(
            "Trace retention: requested deletion of %d traces older than %s "
            "(asynchronous in Langfuse).",
            requested,
            cutoff_iso,
            extra={
                "event": "observability.trace_retention.completed",
                "delete_requested": requested,
                "cutoff": cutoff_iso,
            },
        )
    return RetentionReport(
        cutoff_iso,
        len(trace_ids),
        requested,
        failed_batches,
        requested_ids=requested_ids,
    )


def run_trace_retention(settings: Any) -> RetentionReport | None:
    """Settings-driven entry point for the worker maintenance hook.

    Returns ``None`` when the job does not apply (mode is not otlp,
    retention disabled, or the endpoint is not a Langfuse target).
    """
    observability = settings.observability
    if observability.tracing != "otlp":
        return None
    if int(observability.trace_retention_days or 0) <= 0:
        return None
    import os

    from inqtrix.observability.trace_readers import (
        langfuse_auth_header,
        langfuse_base_url,
    )

    base = langfuse_base_url(
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )
    authorization = langfuse_auth_header()
    if not base or not authorization:
        log.warning(
            "Trace retention skipped: OTEL_EXPORTER_OTLP_* does not configure a "
            "Langfuse destination."
        )
        return None
    import httpx

    with httpx.Client(timeout=60.0) as client:

        def _delete(url: str, *, json: Any, headers: Any) -> Any:
            # httpx.Client.delete() refuses a body by design; the
            # Langfuse batch endpoint requires DELETE with JSON.
            return client.request("DELETE", url, json=json, headers=headers)

        # Drain loop: one saturated pass (cap hit) means a backlog —
        # run follow-up passes immediately instead of waiting 6h, so
        # high-volume deployments converge. Langfuse deletes ASYNC, so a
        # re-list still returns already-requested ids; the accumulated
        # exclusion set makes every pass reach FRESH backlog instead of
        # re-requesting the same ids. Bounded; multiple worker replicas
        # overlapping here are safe (a re-delete is absorbed as a
        # warned non-2xx).
        already_requested: set[str] = set()
        report: RetentionReport | None = None
        for _ in range(_MAX_DRAIN_PASSES):
            report = prune_old_traces(
                base_url=base,
                authorization=authorization,
                retention_days=int(observability.trace_retention_days),
                get=client.get,
                delete=_delete,
                exclude=frozenset(already_requested),
            )
            already_requested.update(report.requested_ids)
            if report.delete_requested < _MAX_DELETES_PER_RUN:
                return report
        log.warning(
            "Trace retention saturated: a deletion backlog remains in Langfuse after %d"
            " passes; check inflow (sample rate) or shorten the retention interval.",
            _MAX_DRAIN_PASSES,
        )
        return report
