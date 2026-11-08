"""The job queue on a real Valkey: what a fake cannot show.

The fake answers a blocking read at once, never binds to a loop and counts a
``XCLAIM JUSTID`` as a delivery. So whether a waiting claim leaves the loop free,
whether one queue object serves two loops and whether a heartbeat stays out of the
dead-letter budget can only be read off a real server.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

# The worker builds its queue, and with it the client package, before its loop
# runs. These tests build theirs on the guarded loop, so the package comes in
# with this module, as the worker has it by the time its loop starts.
import valkey.asyncio  # noqa: F401

from inqtrix import aio
from inqtrix.runs.valkey_queue import ValkeyRunQueue
from inqtrix.worker import loop as worker_loop
from inqtrix.worker.loop import BaseWorkerLoop

pytestmark = pytest.mark.valkey

_HANG_SECONDS = 15.0


def _queue(*, stream: str, consumer: str = "worker-a") -> ValkeyRunQueue:
    return ValkeyRunQueue(
        url=os.environ["INQTRIX_TEST_VALKEY_URL"],
        consumer=consumer,
        stream=stream,
    )


@pytest.fixture
def stream() -> str:
    return f"inqtrix:test:{uuid.uuid4().hex}"


class _ClaimOnly(BaseWorkerLoop):
    """A claim loop with nothing to do but wait on its queue."""

    def _entity_id(self, job) -> str:
        return job.run_id

    async def _stale_dispatch(self) -> list[tuple[str, str]]:
        return []

    async def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        del watched
        return set()


class _NoStore:
    worker_id = "worker-test"


@pytest.mark.asyncio
async def test_a_claim_waiting_five_seconds_leaves_the_loop_free(monkeypatch, stream) -> None:
    """Every run of a worker shares this loop while its claims wait on the queue."""
    monkeypatch.setattr(aio, "_LAG_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(worker_loop, "_CLAIM_BLOCK_MS", 5_000)
    process_loop = asyncio.get_running_loop()
    aio.configure_loop(process_loop, blocking_workers=2, provider_workers=1, watch=True)
    queue = _queue(stream=stream)
    claim_loop = _ClaimOnly(
        store=_NoStore(),
        queue=queue,
        concurrency=1,
        max_attempts=3,
        heartbeat_seconds=60.0,
        claim_idle_seconds=120.0,
    )
    try:
        claiming = asyncio.create_task(claim_loop.run_forever())
        # A segment working on the same loop for longer than the claim's block.
        ended = asyncio.get_running_loop().time() + 5.5
        while asyncio.get_running_loop().time() < ended:
            await asyncio.sleep(0.01)
        claim_loop.request_stop()
        await asyncio.wait_for(claiming, timeout=_HANG_SECONDS)
        lag = aio.loop_lag(process_loop)
    finally:
        await claim_loop.drain(timeout=0)
        await queue.aclose()
        aio.close_loop(process_loop)
    assert lag.samples > 100
    assert lag.p99_ms < 50.0


@pytest.mark.asyncio
async def test_a_blocking_read_with_nothing_to_claim_returns_empty(stream) -> None:
    """The read deadline stays above the block; an idle claim is no timeout."""
    queue = _queue(stream=stream)
    try:
        await queue.ensure_group()
        assert await queue.claim_new(block_ms=1_000) == []
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_a_message_travels_from_enqueue_to_ack(stream) -> None:
    queue = _queue(stream=stream)
    try:
        await queue.ensure_group()
        await queue.enqueue(run_id="run_1", tenant_id="tenant-a")
        jobs = await queue.claim_new(block_ms=100)
        assert [(job.run_id, job.tenant_id, job.delivery_count) for job in jobs] == [
            ("run_1", "tenant-a", 1)
        ]
        await queue.ack(jobs[0].message_id)
        assert (await queue.group_info())["pending"] == 0
        assert await queue.claim_new(block_ms=100) == []
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_a_heartbeat_stays_out_of_the_dead_letter_budget(stream) -> None:
    queue = _queue(stream=stream)
    try:
        await queue.ensure_group()
        await queue.enqueue(run_id="run_1", tenant_id="default")
        job = (await queue.claim_new(block_ms=100))[0]
        await queue.heartbeat([job.message_id])
        await queue.heartbeat([job.message_id])
        assert await queue._delivery_count(job.message_id) == 1
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_a_reclaim_counts_a_delivery_and_a_dead_letter_acks(stream) -> None:
    owner = _queue(stream=stream, consumer="worker-a")
    thief = _queue(stream=stream, consumer="worker-b")
    try:
        await owner.ensure_group()
        await owner.enqueue(run_id="run_1", tenant_id="default")
        assert await owner.claim_new(block_ms=100)
        jobs = await thief.reclaim(min_idle_ms=0)
        assert [(job.run_id, job.delivery_count) for job in jobs] == [("run_1", 2)]
        await thief.dead_letter(jobs[0], reason="max_attempts_exceeded")
        assert (await thief.group_info())["pending"] == 0
    finally:
        await owner.aclose()
        await thief.aclose()


@pytest.mark.asyncio
async def test_a_reclaim_leaves_what_it_cannot_start(stream) -> None:
    owner = _queue(stream=stream, consumer="worker-a")
    thief = _queue(stream=stream, consumer="worker-b")
    try:
        await owner.ensure_group()
        for number in range(5):
            await owner.enqueue(run_id=f"run_{number}", tenant_id="default")
        assert len(await owner.claim_new(block_ms=100, count=5)) == 5
        jobs = await thief.reclaim(min_idle_ms=0, count=2)
        assert [job.run_id for job in jobs] == ["run_0", "run_1"]
        assert await owner._delivery_count(jobs[0].message_id) == 2
    finally:
        await owner.aclose()
        await thief.aclose()


