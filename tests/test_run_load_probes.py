"""The load probes must show what they claim, before any load number is trusted.

Each test provokes the condition a probe exists to report: a blocked event loop,
a pool that is too small, a held checkpoint-saver lock. A probe that stayed quiet
here would report a healthy system no matter how the system behaves.
"""

from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_PROBES = Path(__file__).resolve().parent / "load" / "run-load" / "probes" / "with_probes.py"


@pytest.fixture(scope="module")
def probes_module():
    spec = importlib.util.spec_from_file_location("run_load_probes_under_test", _PROBES)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_a_blocking_call_on_the_loop_shows_as_lag(probes_module):
    lag = probes_module.LagHistogram(interval_ms=10)

    async def scenario(block: bool) -> None:
        sampler = asyncio.ensure_future(probes_module.sample_loop_lag(lag.add, interval=0.01))
        await asyncio.sleep(0.05)
        if block:
            time.sleep(0.3)
        await asyncio.sleep(0.05)
        sampler.cancel()

    asyncio.run(scenario(block=False))
    quiet = lag.summary()["max_ms"]
    asyncio.run(scenario(block=True))
    assert quiet < 150
    assert lag.summary()["max_ms"] >= 250


def test_a_stall_counts_the_wake_ups_it_swallowed(probes_module):
    lag = probes_module.LagHistogram(interval_ms=100)
    lag.add(1.0)
    assert lag.summary()["n"] == 1
    lag.add(500.0)
    summary = lag.summary()
    # 500, 400, 300, 200 and 100 ms: five ticks of the loop were late, not one.
    assert summary["n"] == 6
    assert summary["max_ms"] == 500.0
    limits = summary["limits_ms"]
    assert sum(count for limit, count in zip(limits, summary["counts"]) if limit >= 100) == 5
    assert len(summary["counts"]) == len(limits) + 1


def test_a_lag_beyond_the_last_limit_is_kept_and_not_lost(probes_module):
    lag = probes_module.LagHistogram(interval_ms=1_000_000)
    lag.add(2_000_000.0)
    summary = lag.summary()
    assert summary["limits_ms"][-1] < 1_000_000
    assert summary["counts"][-1] == 2
    assert summary["n"] == 2


def test_the_loop_probe_watches_the_loop_uvicorn_serves_on(probes_module):
    import uvicorn

    original_serve = uvicorn.Server.serve
    probes = probes_module.Probes("test")
    probes_module.install_loop_probe(probes)
    try:

        async def blocking_app(scope, receive, send):
            if scope["type"] == "lifespan":
                while (await receive())["type"] != "lifespan.shutdown":
                    await send({"type": "lifespan.startup.complete"})
                await send({"type": "lifespan.shutdown.complete"})
                return
            time.sleep(0.3)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        server = uvicorn.Server(uvicorn.Config(blocking_app, host="127.0.0.1", port=0, log_config=None))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.25)
            quiet = probes.loop_lag.summary()
            assert quiet["n"] >= 1
            assert quiet["max_ms"] < 150
            port = server.servers[0].sockets[0].getsockname()[1]
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
            time.sleep(0.2)
        finally:
            server.should_exit = True
            thread.join(10)
    finally:
        uvicorn.Server.serve = original_serve
    assert probes.loop_lag.summary()["max_ms"] >= 250


@pytest.fixture
def restored_pools():
    from sqlalchemy.pool import NullPool, QueuePool

    saved = QueuePool._do_get, QueuePool._do_return_conn, NullPool._do_get
    yield
    QueuePool._do_get, QueuePool._do_return_conn, NullPool._do_get = saved


def test_a_small_pool_shows_wait_and_its_peak(probes_module, restored_pools):
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import QueuePool

    probes = probes_module.Probes("test")
    probes_module.install_pool_probes(probes)
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=5, connect_args={"check_same_thread": False})
    first = engine.connect()
    waited = threading.Event()

    def second() -> None:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        waited.set()

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(0.3)
    first.close()
    assert waited.wait(5)
    thread.join()
    summary = next(iter(probes.pools.values())).summary()
    assert summary["peak_in_use"] == 1
    assert summary["in_use"] == 0
    assert summary["wait"]["max_ms"] >= 250
    assert summary["timeouts"] == 0


def test_a_checkout_that_times_out_stays_in_the_numbers(probes_module, restored_pools):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from sqlalchemy.pool import QueuePool

    probes = probes_module.Probes("test")
    probes_module.install_pool_probes(probes)
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.2, connect_args={"check_same_thread": False})
    held = engine.connect()
    with pytest.raises(PoolTimeout):
        engine.connect()
    held.close()
    summary = next(iter(probes.pools.values())).summary()
    assert summary["timeouts"] == 1
    assert summary["wait"]["max_ms"] >= 150


def test_a_caller_cancelled_while_it_waits_stays_in_the_numbers(probes_module, restored_pools):
    from sqlalchemy.pool import QueuePool

    def cancelled(self):
        raise asyncio.CancelledError

    QueuePool._do_get = cancelled
    probes = probes_module.Probes("test")
    probes_module.install_pool_probes(probes)
    pool = QueuePool(lambda: None, pool_size=1, max_overflow=0)

    with pytest.raises(asyncio.CancelledError):
        pool._do_get()

    summary = next(iter(probes.pools.values())).summary()
    assert summary["abandoned"] == 1
    assert summary["timeouts"] == 0
    assert summary["wait"]["n"] == 1


def test_null_pool_connections_are_counted(probes_module, restored_pools):
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    probes = probes_module.Probes("test")
    probes_module.install_pool_probes(probes)
    engine = create_engine("sqlite://", poolclass=NullPool)
    for _ in range(3):
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    assert probes.null_pool_connects == 3


def test_a_held_saver_lock_shows_wait_and_queueing(probes_module):
    wait = probes_module.Durations()
    by_writers = collections.defaultdict(probes_module.Durations)
    lock = probes_module.TimedLock(wait, by_writers=by_writers)
    lock.acquire()

    def writer() -> None:
        with lock:
            time.sleep(0.01)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.25)
    lock.release()
    for thread in threads:
        thread.join(5)
    assert wait.summary()["max_ms"] >= 200
    assert lock.max_waiting == 2
    assert by_writers["1"].count >= 1
    assert by_writers["2"].summary()["max_ms"] >= 200


def test_the_saver_probe_replaces_the_lock_of_the_real_saver(probes_module):
    from unittest.mock import MagicMock

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    saved = PostgresSaver.__init__, PostgresSaver.put, PostgresSaver.put_writes, ConnectionPool.__init__
    probes = probes_module.Probes("test")
    probes_module.install_saver_probes(probes)
    try:
        saver = PostgresSaver(MagicMock())
    finally:
        PostgresSaver.__init__, PostgresSaver.put, PostgresSaver.put_writes, ConnectionPool.__init__ = saved
    assert isinstance(saver.lock, probes_module.TimedLock)
    with saver.lock:
        pass
    assert probes.snapshot()["saver"]["instances"] == 1
    assert probes.saver_lock_wait.count == 1


def test_the_statements_of_an_engine_are_counted(probes_module):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.engine import Engine

    probes = probes_module.Probes("test")
    listener = probes_module.install_statement_probe(probes)
    try:
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            for _ in range(3):
                connection.execute(text("select 1"))
    finally:
        event.remove(Engine, "before_cursor_execute", listener)
    assert probes.statements == 3
    assert probes.snapshot()["statements"] == 3


def test_the_snapshot_is_served_as_json(probes_module):
    probes = probes_module.Probes("test")
    probes.loop_lag.add(12.0)
    server = probes_module.serve_probes(probes, port=0)
    try:
        port = server.server_address[1]
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/probes", timeout=5))
    finally:
        server.shutdown()
    assert body["process"] == "test"
    assert body["loop_lag"]["n"] == 1
    assert body["loop_lag"]["max_ms"] == 12.0
    assert sum(body["loop_lag"]["counts"]) == 1
    assert body["saver"]["instances"] == 0


def test_a_heartbeat_that_comes_late_shows_how_late(probes_module, monkeypatch):
    from inqtrix.worker.loop import BaseWorkerLoop

    monkeypatch.setattr(BaseWorkerLoop, "_heartbeat_message_ids", lambda self: ["id"])
    probes = probes_module.Probes("worker")
    probes_module.install_heartbeat_probe(probes)
    loop = BaseWorkerLoop.__new__(BaseWorkerLoop)
    loop._heartbeat_seconds = 0.0

    assert BaseWorkerLoop._heartbeat_message_ids(loop) == ["id"]
    time.sleep(0.05)
    BaseWorkerLoop._heartbeat_message_ids(loop)

    summary = probes.snapshot()["heartbeat_late"]
    assert summary["n"] == 1
    assert summary["max_ms"] >= 40


def test_the_worker_probe_samples_the_loop_the_worker_serves_on(probes_module, monkeypatch):
    from inqtrix.worker import __main__ as worker_main

    probes = probes_module.Probes("worker")

    async def serve_with_a_blocked_loop(_worker) -> None:
        await asyncio.sleep(0.25)
        time.sleep(0.3)
        await asyncio.sleep(0.25)

    monkeypatch.setattr(worker_main, "_serve", serve_with_a_blocked_loop)
    probes_module.install_worker_loop_probe(probes)

    asyncio.run(worker_main._serve(None))

    assert probes.loop_lag.summary()["max_ms"] >= 150


def test_the_lanes_show_their_peak_use(probes_module):
    from inqtrix import aio

    probes = probes_module.Probes("worker")
    release = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        aio.configure_loop(loop, blocking_workers=2, provider_workers=1, watch=False)
        try:
            jobs = [asyncio.ensure_future(aio.run_blocking(release.wait, 5)) for _ in range(3)]
            await asyncio.sleep(0.05)
            probes.record_lanes()
            release.set()
            await asyncio.gather(*jobs)
            probes.record_lanes()
        finally:
            aio.close_loop(loop)

    asyncio.run(scenario())

    assert probes.snapshot()["lanes"]["blocking"] == {"running": 2, "waiting": 1, "workers": 2}


def test_the_diagnosis_lowers_the_watchdogs_threshold(probes_module, monkeypatch):
    from inqtrix import aio

    for name in ("_LAG_PROBE_SECONDS", "_STALL_CHECK_SECONDS", "_STALL_SECONDS"):
        monkeypatch.setattr(aio, name, getattr(aio, name))

    probes_module.lower_the_stall_threshold(50)

    assert aio._STALL_SECONDS == pytest.approx(0.05)
    assert aio._LAG_PROBE_SECONDS == pytest.approx(0.01)
