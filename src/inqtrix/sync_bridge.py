"""Run an async coroutine to completion from synchronous code.

The knowledge store/service is async (uniform with the platform's
asyncpg persistence layer), but two synchronous contexts still consume
it: the research graph (node functions executing on a run-worker thread)
and the reindex worker. This bridge runs a coroutine to completion from
such sync code.

It is robust to both situations: when the calling thread has no running
event loop (the run/reindex worker threads, library mode) it uses
``asyncio.run`` directly; when a loop IS already running in the calling
thread, blocking on it would deadlock, so the coroutine is executed on a
fresh single-use worker thread with its own loop. Stores reached this way
use loop-agnostic NullPool engines, so a per-call loop is safe (the same
property the quota store relies on for ``record_blocking``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_coro_sync(coro: "Coroutine[Any, Any, T]") -> T:
    """Execute *coro* to completion and return its result, from sync code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running in this thread; run the coroutine on a
    # dedicated worker thread to avoid blocking (and deadlocking) it.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()
