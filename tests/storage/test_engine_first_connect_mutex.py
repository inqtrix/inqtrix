"""The once-per-engine connect mutex must survive per-call event loops.

With SQLAlchemy's first-run mutex, an ``asyncio.Lock`` bound to one loop,
a kernel tool call can hang forever: three parallel ``search_context``
calls, each on its own ``asyncio.run`` loop, make the first connections of
the agent-sessions engine in the worker process and contend on that lock.
One contender raises "bound to a different event loop", the other loses
its wakeup and sleeps in ``epoll_wait`` without a timer. These tests pin
the replacement mutex on the two properties that matter: it serializes
contenders across loops without losing anyone, and it never parks a shared
loop's thread while another coroutine holds it.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy.util.concurrency import await_only, greenlet_spawn

from inqtrix.storage.db import _LoopAgnosticOnceMutex, build_engine

_URL = "postgresql+asyncpg://user:pw@127.0.0.1:5432/db"


def _contend_from_a_fresh_loop(mutex, barrier, hold_seconds, errors, order) -> None:
    """One executor thread of the kernel: a fresh loop, a greenlet, the mutex."""

    async def main() -> None:
        def critical_section() -> None:
            with mutex:
                order.append(threading.current_thread().name)
                # The holder's dialect initialization awaits the database on
                # ITS loop; a blocking sleep keeps the lock contended for the
                # other loops without touching a database here.
                time.sleep(hold_seconds)

        await greenlet_spawn(critical_section)

    try:
        barrier.wait(timeout=5)
        asyncio.run(main())
    except BaseException as exc:  # a lost contender is the finding, never a silent pass
        errors.append(exc)


def test_contended_first_connections_from_three_loops_all_complete() -> None:
    mutex = _LoopAgnosticOnceMutex()
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []
    order: list[str] = []
    threads = [
        threading.Thread(
            target=_contend_from_a_fresh_loop,
            args=(mutex, barrier, 0.1, errors, order),
            name=f"loop-{index}",
            daemon=True,
        )
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert stuck == [], f"contenders never woke up: {stuck}"
    assert errors == []
    assert sorted(order) == ["loop-0", "loop-1", "loop-2"]


def test_the_mutex_never_parks_a_shared_loop() -> None:
    """Two coroutines on ONE loop (the API process): the holder awaits, the
    other must keep the loop running instead of blocking its thread."""

    mutex = _LoopAgnosticOnceMutex()
    order: list[str] = []

    async def contender(name: str) -> None:
        def critical_section() -> None:
            with mutex:
                order.append(name)
                await_only(asyncio.sleep(0.05))

        await greenlet_spawn(critical_section)

    async def main() -> None:
        await asyncio.wait_for(asyncio.gather(contender("a"), contender("b")), timeout=5)

    outcome: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(main())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run, name="shared-loop", daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the shared loop was parked by a contender"
    assert outcome == []
    assert sorted(order) == ["a", "b"]


@pytest.mark.parametrize("null_pool", [True, False])
def test_build_engine_installs_the_loop_agnostic_once_mutex(null_pool: bool) -> None:
    engine = build_engine(_URL, null_pool=null_pool)
    try:
        dispatch = engine.sync_engine.pool.dispatch
        for name in ("connect", "first_connect"):
            collection = getattr(dispatch, name).for_modify(dispatch)
            assert isinstance(collection._exec_once_mutex, _LoopAgnosticOnceMutex)
            # SQLAlchemy must hand back the preset mutex, never its own lock.
            assert collection._get_exec_once_mutex() is collection._exec_once_mutex
    finally:
        engine.sync_engine.dispose()
