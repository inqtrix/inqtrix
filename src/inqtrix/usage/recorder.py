"""Buffered, fail-safe usage-ledger writer.

``record_provider_call`` is the ONE feed point, called from the same
provider wrappers that feed spans and metrics. It reads the ambient
usage subject (raw booking identity), feature, and run id, assembles a
:class:`UsageRow`, and hands it to the process recorder. Everything is
fail-safe: a ledger problem must never touch the provider call it
books — but never fail-SILENT either (WARNING on every degradation
class, once per streak/process).

The recorder buffers rows in memory and a daemon thread flushes them in
batches through the store's own throwaway event loop (NullPool engines
only — the quota ``record_blocking`` precedent). ``close()`` flushes
the remainder; the worker's ``os._exit`` paths may still drop the last
seconds of rows, which is acceptable for consumption history and
mirrors the span-batch contract.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from inqtrix.sync_bridge import run_coro_sync
from inqtrix.usage.models import UsageRow

log = logging.getLogger("inqtrix")

_FLUSH_INTERVAL_SECONDS = 5.0
_MAX_BUFFERED_ROWS = 10_000
_MAX_FLUSH_RETRIES = 20
"""Retries before a permanently unwritable batch is dropped loudly.

At the 5s default cadence this is ~100s of retrying — long enough to
ride out a database restart, short enough that a poison row cannot hold
the buffer hostage for the rest of the process lifetime."""


class UsageRecorder:
    """Bounded buffer + background flusher for one ledger store."""

    def __init__(
        self,
        store: Any,
        *,
        flush_interval_seconds: float = _FLUSH_INTERVAL_SECONDS,
        max_buffered_rows: int = _MAX_BUFFERED_ROWS,
    ) -> None:
        self._store = store
        self._interval = float(flush_interval_seconds)
        self._max_rows = int(max_buffered_rows)
        self._buffer: deque[UsageRow] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._overflow_warned = False
        self._failure_streak = False
        self._consecutive_failures = 0
        self._thread = threading.Thread(
            target=self._run,
            name="inqtrix-usage-ledger",
            daemon=True,
        )
        self._thread.start()

    def record(self, row: UsageRow) -> None:
        """Enqueue one row; overflow drops the NEW row with a WARNING."""
        with self._lock:
            if len(self._buffer) >= self._max_rows:
                if not self._overflow_warned:
                    self._overflow_warned = True
                    log.warning(
                        "Usage ledger buffer full (%d rows); new usage rows are dropped"
                        " while the buffer is full.",
                        self._max_rows,
                    )
                return
            self._buffer.append(row)

    def _drain(self) -> list[UsageRow]:
        with self._lock:
            if not self._buffer:
                return []
            batch = list(self._buffer)
            self._buffer.clear()
            return batch

    def _requeue_front(self, batch: list[UsageRow]) -> None:
        dropped = 0
        with self._lock:
            for row in reversed(batch):
                if len(self._buffer) >= self._max_rows:
                    dropped += 1
                    continue
                self._buffer.appendleft(row)
        if dropped and not self._overflow_warned:
            self._overflow_warned = True
            log.warning(
                "Usage ledger: %d rows from a failed flush no longer fit in the buffer "
                "and were lost.",
                dropped,
            )

    def _flush_once(self) -> None:
        batch = self._drain()
        if not batch:
            return
        # One transaction per tenant: a failure must requeue ONLY the
        # groups that did not commit, otherwise the committed ones are
        # inserted twice (the ledger has no idempotency key).
        groups: dict[str, list[UsageRow]] = {}
        for row in batch:
            groups.setdefault(row.tenant_id, []).append(row)
        failed: list[UsageRow] = []
        failure: BaseException | None = None
        for rows in groups.values():
            try:
                run_coro_sync(self._store.insert_rows(rows))
            except Exception as exc:  # noqa: BLE001 — never crash the host
                failure = exc
                failed.extend(rows)
        if failed:
            self._consecutive_failures += 1
            if self._consecutive_failures > _MAX_FLUSH_RETRIES:
                # Poison isolation: a permanently rejected batch (e.g. a
                # value outside a CHECK constraint) would otherwise be
                # retried forever and wedge the buffer for every later
                # row. Drop it LOUDLY and keep the ledger writing.
                log.error(
                    "Usage ledger: %d rows could not be written after %d attempts "
                    "(error_type=%s); discarding them so subsequent usage recording can"
                    " continue.",
                    len(failed),
                    self._consecutive_failures,
                    type(failure).__name__ if failure else "unknown",
                )
                self._consecutive_failures = 0
                self._failure_streak = False
                return
        else:
            self._consecutive_failures = 0
        if failure is not None:
            if not self._failure_streak:
                self._failure_streak = True
                # No exc_info: SQLAlchemy statement errors embed the bound
                # parameters, and those carry raw user ids/tenants that
                # must never reach the log stream.
                log.warning(
                    "Usage ledger flush failed (error_type=%s, %d rows buffered); "
                    "retrying in %.0fs.",
                    type(failure).__name__,
                    len(failed),
                    self._interval,
                )
            self._requeue_front(failed)
            return
        if self._failure_streak:
            self._failure_streak = False
            log.info("Usage ledger flush recovered.")
        with self._lock:
            self._overflow_warned = False

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._flush_once()

    def close(self) -> None:
        """Stop the flusher and write the remaining buffer."""
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._flush_once()

    @property
    def store(self) -> Any:
        """The backing ledger store (retention thread, shutdown aclose)."""
        return self._store


# ---- process holder (set_active_metrics pattern) -------------------- #

_active: UsageRecorder | None = None


def set_active_usage_recorder(recorder: UsageRecorder | None) -> None:
    global _active
    _active = recorder


def active_usage_recorder() -> UsageRecorder | None:
    return _active


def install_usage_recorder(settings: Any) -> UsageRecorder:
    """Build and publish the process recorder (idempotent per app).

    Postgres deployments get the durable ledger; the memory backend gets
    the RAM twin (same behaviour, process lifetime — the quota-store
    parity). A previously installed recorder is closed first so repeated
    ``create_app`` in tests never leaks flusher threads.
    """
    previous = _active
    if previous is not None:
        previous.close()
    if getattr(settings.storage, "backend", "") == "postgres":
        from inqtrix.storage.usage_postgres import PostgresUsageStore

        store: Any = PostgresUsageStore(
            database_url=settings.storage.database_url,
            app_role=settings.storage.app_role,
        )
    else:
        from inqtrix.usage.memory import MemoryUsageStore

        store = MemoryUsageStore()
    recorder = UsageRecorder(store)
    set_active_usage_recorder(recorder)
    return recorder


_record_failed_warned = False


def record_provider_call(
    *,
    operation: str,
    model: str,
    outcome: str,
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Assemble and enqueue one ledger row from ambient context.

    No recorder (ledger off) or no bound subject (unmetered principals:
    anonymous/static — the same set quota never meters) is a normal
    no-op. Any unexpected failure warns once per process and never
    reaches the provider call.
    """
    global _record_failed_warned
    recorder = _active
    if recorder is None:
        return
    try:
        from inqtrix.observability.context import (
            current_feature,
            current_log_context,
            current_usage_subject,
        )

        subject = current_usage_subject()
        if subject is None:
            return
        run_id = str(current_log_context().get("run_id") or "") or None
        recorder.record(
            UsageRow(
                tenant_id=subject.tenant_id,
                user_id=subject.user_id,
                workspace_id=subject.workspace_id,
                run_id=run_id,
                feature=current_feature(),
                operation=operation,
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                request_count=1,
                duration_ms=int(max(0.0, duration_seconds) * 1000),
                outcome=outcome,
                created_at=time.time(),
            )
        )
    except Exception:  # noqa: BLE001 — never touch the metered call
        if not _record_failed_warned:
            _record_failed_warned = True
            log.warning(
                "Usage ledger row could not be recorded; usage history is incomplete.",
                exc_info=True,
            )
