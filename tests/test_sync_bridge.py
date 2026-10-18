"""Tests for run_coro_sync — the sync->async bridge.

Both branches must work: the no-running-loop path (run/reindex worker
threads, library mode) and the running-loop path (defensive offload to a
worker thread, so a future caller on the event loop never deadlocks).
"""

from __future__ import annotations

import asyncio

import pytest

from inqtrix.sync_bridge import run_coro_sync


async def _double(value: int) -> int:
    await asyncio.sleep(0)
    return value * 2


def test_runs_coroutine_without_running_loop() -> None:
    # Plain sync caller, no event loop in this thread -> asyncio.run path.
    assert run_coro_sync(_double(21)) == 42


def test_propagates_exception_without_running_loop() -> None:
    async def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_coro_sync(_boom())


@pytest.mark.asyncio
async def test_offload_branch_runs_from_within_a_running_loop() -> None:
    # Called from inside a running loop: must offload to a worker thread
    # and still return the result (no deadlock). asyncio.to_thread keeps
    # this test's own loop responsive while the bridge blocks its worker.
    result = await asyncio.to_thread(run_coro_sync, _double(5))
    assert result == 10
