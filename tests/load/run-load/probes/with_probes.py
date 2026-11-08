"""Starts the API or the worker with measurement probes, for the load stack only.

The load stack has to answer questions the application has no instrument for:
how long the event loop stalls, how long a request waits for a pooled connection
and how full each pool gets, and how long a checkpoint write queues behind the
saver's lock. These probes answer them from the outside. The application is not
changed and does not know about them; `compose.run-load.yaml` starts the process
through this file instead of its console script.

    python with_probes.py api
    python with_probes.py worker

The values are served as JSON on 127.0.0.1:9400/probes inside the container, and
the load engine reads them with `podman exec`. Counts and durations only.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

PORT = 9400
_RESERVOIR = 20_000
_LOOP_INTERVAL = 0.1
_LAG_LIMITS_MS = tuple(round(1.25**step, 1) for step in range(53))


class Durations:
    """Recent durations in milliseconds, summarized without unbounded memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: collections.deque[float] = collections.deque(maxlen=_RESERVOIR)
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0

    def add(self, milliseconds: float) -> None:
        with self._lock:
            self._recent.append(milliseconds)
            self.count += 1
            self.total_ms += milliseconds
            self.max_ms = max(self.max_ms, milliseconds)

    def summary(self) -> dict[str, float]:
        with self._lock:
            ordered = sorted(self._recent)
        if not ordered:
            return {"n": 0}
        rank = lambda fraction: ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]
        return {"n": self.count, "total_ms": round(self.total_ms, 1), "p50_ms": round(rank(0.5), 2),
                "p95_ms": round(rank(0.95), 2), "p99_ms": round(rank(0.99), 2), "max_ms": round(self.max_ms, 2)}


class LagHistogram:
    """Event-loop lag in fixed buckets, so that two readings can be subtracted.

    A stalled loop reports one late sample while the wake-ups it swallowed report
    nothing. Each sample therefore also counts those wake-ups, the usual correction
    for coordinated omission: a percentile here is a share of the time, not a share
    of the wake-ups that happened to get through.
    """

    def __init__(self, interval_ms: float = _LOOP_INTERVAL * 1000) -> None:
        self._interval_ms = interval_ms
        self.counts = [0] * (len(_LAG_LIMITS_MS) + 1)
        self.max_ms = 0.0

    def add(self, milliseconds: float) -> None:
        self.max_ms = max(self.max_ms, milliseconds)
        remaining = milliseconds
        while True:
            self.counts[bisect.bisect_left(_LAG_LIMITS_MS, remaining)] += 1
            remaining -= self._interval_ms
            if remaining <= 0:
                return

    def summary(self) -> dict[str, Any]:
        return {"limits_ms": list(_LAG_LIMITS_MS), "counts": list(self.counts), "n": sum(self.counts), "max_ms": round(self.max_ms, 2)}


def _writers_bucket(writers: int) -> str:
    if writers <= 2:
        return str(writers)
    return "3-4" if writers <= 4 else "5+"


class TimedLock:
    """A lock that records how long an acquire waited and how many queued at once."""

    def __init__(self, wait: Durations, inner: Any = None, by_writers: dict[str, Durations] | None = None) -> None:
        self._inner = inner or threading.Lock()
        self._wait = wait
        self._by_writers = by_writers if by_writers is not None else collections.defaultdict(Durations)
        self._guard = threading.Lock()
        self._waiting = 0
        self.max_waiting = 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        with self._guard:
            self._waiting += 1
            writers = self._waiting
            self.max_waiting = max(self.max_waiting, writers)
        started = time.perf_counter()
        try:
            return self._inner.acquire(*args, **kwargs)
        finally:
            waited = (time.perf_counter() - started) * 1000
            self._wait.add(waited)
            self._by_writers[_writers_bucket(writers)].add(waited)
            with self._guard:
                self._waiting -= 1

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "TimedLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class PoolProbe:
    """Wait and occupancy of one SQLAlchemy queue pool.

    The wait includes opening a connection when the pool has to: that is what the
    caller waits for. A checkout that fails counts its wait and as a timeout, so the
    worst case stays in the numbers.
    """

    def __init__(self, pool: Any) -> None:
        self.name = type(pool).__name__
        self.size = getattr(pool, "_pool", None).maxsize if hasattr(getattr(pool, "_pool", None), "maxsize") else None
        self.max_overflow = getattr(pool, "_max_overflow", None)
        self.wait = Durations()
        self.in_use = 0
        self.peak = 0
        self.timeouts = 0
        self.abandoned = 0
        self._guard = threading.Lock()

    def failed(self, milliseconds: float) -> None:
        self.wait.add(milliseconds)
        with self._guard:
            self.timeouts += 1

    def gave_up(self, milliseconds: float) -> None:
        """A caller cancelled while it waited: no timeout, but it did not get one."""
        self.wait.add(milliseconds)
        with self._guard:
            self.abandoned += 1

    def got(self, milliseconds: float) -> None:
        self.wait.add(milliseconds)
        with self._guard:
            self.in_use += 1
            self.peak = max(self.peak, self.in_use)

    def returned(self) -> None:
        with self._guard:
            self.in_use = max(0, self.in_use - 1)

    def summary(self) -> dict[str, Any]:
        return {"kind": self.name, "size": self.size, "max_overflow": self.max_overflow, "in_use": self.in_use, "abandoned": self.abandoned, "peak_in_use": self.peak, "timeouts": self.timeouts, "wait": self.wait.summary()}


class Probes:
    """Everything one process measures."""

    def __init__(self, process: str) -> None:
        self.process = process
        self.loop_lag = LagHistogram()
        self.pools: dict[int, PoolProbe] = {}
        self.null_pool_connects = 0
        self.saver_lock_wait = Durations()
        self.saver_lock_wait_by_writers: dict[str, Durations] = collections.defaultdict(Durations)
        self.saver_locks: list[TimedLock] = []
        self.checkpoint_put = Durations()
        self.checkpoint_put_writes = Durations()
        self.psycopg_pools: list[Any] = []
        self.statements = 0
        self.lanes: dict[str, dict[str, int]] = {}
        self.heartbeat_late = Durations()

    def record_lanes(self) -> None:
        """The peaks of each thread lane: a full lane leaves the loop healthy."""
        from inqtrix import aio

        for name, lane in aio.lane_snapshot().items():
            peak = self.lanes.setdefault(name, {"running": 0, "waiting": 0, "workers": lane.workers})
            peak["running"] = max(peak["running"], lane.running)
            peak["waiting"] = max(peak["waiting"], lane.waiting)

    def snapshot(self) -> dict[str, Any]:
        psycopg = []
        for pool in self.psycopg_pools:
            stats = pool.get_stats()
            psycopg.append({key: stats.get(key) for key in ("pool_size", "pool_min", "pool_max", "requests_num", "requests_queued", "requests_wait_ms", "connections_num")})
        return {
            "process": self.process,
            "loop_lag": self.loop_lag.summary(),
            "lanes": self.lanes,
            "pools": [probe.summary() for probe in self.pools.values()],
            "null_pool_connects": self.null_pool_connects,
            "statements": self.statements,
            "heartbeat_late": self.heartbeat_late.summary(),
            "saver": {
                "instances": len(self.saver_locks),
                "lock_wait": self.saver_lock_wait.summary(),
                "lock_wait_by_writers": {key: value.summary() for key, value in sorted(self.saver_lock_wait_by_writers.items())},
                "max_waiting": max((lock.max_waiting for lock in self.saver_locks), default=0),
                "put": self.checkpoint_put.summary(),
                "put_writes": self.checkpoint_put_writes.summary(),
            },
            "checkpointer_pools": psycopg,
        }


async def sample_loop_lag(
    record: Callable[[float], None],
    interval: float = _LOOP_INTERVAL,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Record how much later than asked this loop came back from a short sleep."""
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval)
        record(max(0.0, (time.perf_counter() - started - interval) * 1000))
        if on_tick is not None:
            on_tick()


def install_pool_probes(probes: Probes) -> None:
    """Time connection checkout on every queue pool and count NullPool connects."""
    from sqlalchemy.pool import NullPool, QueuePool

    original_get = QueuePool._do_get
    original_return = QueuePool._do_return_conn
    original_null = NullPool._do_get

    def probe_for(pool: Any) -> PoolProbe:
        return probes.pools.setdefault(id(pool), PoolProbe(pool))

    def timed_get(self: Any) -> Any:
        started = time.perf_counter()
        try:
            connection = original_get(self)
        except Exception:
            probe_for(self).failed((time.perf_counter() - started) * 1000)
            raise
        except BaseException:
            probe_for(self).gave_up((time.perf_counter() - started) * 1000)
            raise
        probe_for(self).got((time.perf_counter() - started) * 1000)
        return connection

    def counted_return(self: Any, record: Any) -> None:
        probe_for(self).returned()
        original_return(self, record)

    def counted_null(self: Any) -> Any:
        probes.null_pool_connects += 1
        return original_null(self)

    QueuePool._do_get = timed_get
    QueuePool._do_return_conn = counted_return
    NullPool._do_get = counted_null


def install_statement_probe(probes: Probes) -> Callable[..., None]:
    """Count the statements of every SQLAlchemy engine this process builds."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def counted(*_: Any) -> None:
        probes.statements += 1

    event.listen(Engine, "before_cursor_execute", counted)
    return counted


def install_saver_probes(probes: Probes) -> None:
    """Time the checkpoint saver's lock and writes, and watch its connection pool."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    original_init = PostgresSaver.__init__
    original_put = PostgresSaver.put
    original_put_writes = PostgresSaver.put_writes
    original_pool_init = ConnectionPool.__init__

    def timed_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        lock = TimedLock(probes.saver_lock_wait, self.lock, probes.saver_lock_wait_by_writers)
        self.lock = lock
        probes.saver_locks.append(lock)

    def timed_put(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_put(self, *args, **kwargs)
        finally:
            probes.checkpoint_put.add((time.perf_counter() - started) * 1000)

    def timed_put_writes(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_put_writes(self, *args, **kwargs)
        finally:
            probes.checkpoint_put_writes.add((time.perf_counter() - started) * 1000)

    def watched_pool_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_pool_init(self, *args, **kwargs)
        probes.psycopg_pools.append(self)

    PostgresSaver.__init__ = timed_init
    PostgresSaver.put = timed_put
    PostgresSaver.put_writes = timed_put_writes
    ConnectionPool.__init__ = watched_pool_init


def install_loop_probe(probes: Probes) -> None:
    """Sample the server's own event loop from the moment uvicorn serves."""
    import uvicorn

    original_serve = uvicorn.Server.serve

    async def serve(self: Any, *args: Any, **kwargs: Any) -> Any:
        sampler = asyncio.ensure_future(
            sample_loop_lag(probes.loop_lag.add, on_tick=probes.record_lanes)
        )
        try:
            return await original_serve(self, *args, **kwargs)
        finally:
            sampler.cancel()

    uvicorn.Server.serve = serve


def install_worker_loop_probe(probes: Probes) -> None:
    """Sample the worker's one event loop from the moment it serves."""
    from inqtrix.worker import __main__ as worker_main

    original_serve = worker_main._serve

    async def serve(worker: Any) -> None:
        sampler = asyncio.ensure_future(
            sample_loop_lag(probes.loop_lag.add, on_tick=probes.record_lanes)
        )
        try:
            await original_serve(worker)
        finally:
            sampler.cancel()

    worker_main._serve = serve


def install_heartbeat_probe(probes: Probes) -> None:
    """How much later than its interval each heartbeat of a worker loop comes.

    Every tick asks for the message ids, with or without a job, so the gap between
    two questions holds the loop's delay and the previous call to the queue.
    """
    from inqtrix.worker.loop import BaseWorkerLoop

    original = BaseWorkerLoop._heartbeat_message_ids
    previous: dict[int, float] = {}

    def timed(self: Any) -> Any:
        now = time.perf_counter()
        last = previous.get(id(self))
        if last is not None:
            probes.heartbeat_late.add(max(0.0, (now - last - self._heartbeat_seconds) * 1000))
        previous[id(self)] = now
        return original(self)

    BaseWorkerLoop._heartbeat_message_ids = timed


def lower_the_stall_threshold(milliseconds: float) -> None:
    """Diagnosis only: let the watchdog name what holds the loop for this long."""
    from inqtrix import aio

    aio._LAG_PROBE_SECONDS = milliseconds / 5000
    aio._STALL_CHECK_SECONDS = milliseconds / 5000
    aio._STALL_SECONDS = milliseconds / 1000


def serve_probes(probes: Probes, port: int = PORT) -> ThreadingHTTPServer:
    """Serve the snapshot on loopback from a daemon thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib names it
            if self.path != "/probes":
                self.send_error(404)
                return
            body = json.dumps(probes.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="load-probes", daemon=True).start()
    return server


def main() -> None:
    process = sys.argv[1] if len(sys.argv) > 1 else ""
    if process not in ("api", "worker"):
        raise SystemExit("usage: with_probes.py api|worker")
    probes = Probes(process)
    stall_ms = os.environ.get("INQTRIX_RUN_LOAD_STALL_MS", "").strip()
    if stall_ms:
        lower_the_stall_threshold(float(stall_ms))
    install_pool_probes(probes)
    install_statement_probe(probes)
    install_saver_probes(probes)
    serve_probes(probes)
    if process == "api":
        install_loop_probe(probes)
        from inqtrix.__main__ import main as run
    else:
        install_worker_loop_probe(probes)
        install_heartbeat_probe(probes)
        from inqtrix.worker.__main__ import main as run
    run()


if __name__ == "__main__":
    main()
