"""Offline Valkey-queue tests against an in-process fake.

The queue port is exercised with fakeredis (stream commands included),
so claim/ack/heartbeat/reclaim/dead-letter semantics are pinned without
a running broker — the suite stays zero-infrastructure. The fake answers a
blocking read at once and never binds to a loop, so what depends on either
is proved against a real server in ``tests/worker/test_async_claim.py``.
"""

from __future__ import annotations

import fakeredis
import pytest

from inqtrix.runs.valkey_queue import DEAD_STREAM, ValkeyRunQueue

STREAM = "inqtrix:runs:jobs"
GROUP = "workers"


@pytest.fixture()
def client():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def make_queue(client, consumer: str = "worker-a") -> ValkeyRunQueue:
    return ValkeyRunQueue(client=client, consumer=consumer)


@pytest.mark.asyncio
async def test_enqueue_claim_ack_roundtrip(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(run_id="run_1", tenant_id="default")

    jobs = await queue.claim_new(block_ms=1)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.run_id == "run_1"
    assert job.tenant_id == "default"
    assert job.delivery_count == 1

    await queue.ack(job.message_id)
    assert (await client.xpending(STREAM, GROUP))["pending"] == 0
    assert await queue.claim_new(block_ms=1) == []


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.ensure_group()


@pytest.mark.asyncio
async def test_claim_pending_drains_own_backlog_after_crash(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(run_id="run_1", tenant_id="default")
    claimed = await queue.claim_new(block_ms=1)
    assert claimed and claimed[0].run_id == "run_1"

    # Simulated process restart: same consumer name, fresh object.
    restarted = make_queue(client)
    backlog = await restarted.claim_pending()
    assert [job.run_id for job in backlog] == ["run_1"]


@pytest.mark.asyncio
async def test_reclaim_takes_over_idle_entries_and_counts_delivery(client):
    owner = make_queue(client, consumer="worker-a")
    await owner.ensure_group()
    await owner.enqueue(run_id="run_1", tenant_id="default")
    assert await owner.claim_new(block_ms=1)

    thief = make_queue(client, consumer="worker-b")
    jobs = await thief.reclaim(min_idle_ms=0)
    assert len(jobs) == 1
    assert jobs[0].run_id == "run_1"
    assert jobs[0].delivery_count == 2


@pytest.mark.asyncio
async def test_reclaim_takes_no_more_than_it_can_start(client):
    owner = make_queue(client, consumer="worker-a")
    await owner.ensure_group()
    for number in range(5):
        await owner.enqueue(run_id=f"run_{number}", tenant_id="default")
    assert len(await owner.claim_new(block_ms=1, count=5)) == 5

    jobs = await make_queue(client, consumer="worker-b").reclaim(min_idle_ms=0, count=2)

    assert [job.run_id for job in jobs] == ["run_0", "run_1"]


@pytest.mark.asyncio
async def test_heartbeat_keeps_ownership_and_pending_state(client):
    """Heartbeats must keep the entry pending under the same consumer.

    fakeredis deviates from real Valkey here: XCLAIM JUSTID wrongly
    increments ``times_delivered`` in the fake, so the
    no-retry-bump property (the reason JUSTID is load-bearing) can
    only be asserted against a real server — this test pins what the
    fake CAN verify: ownership and pending state survive heartbeats.
    """
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(run_id="run_1", tenant_id="default")
    job = (await queue.claim_new(block_ms=1))[0]

    await queue.heartbeat([job.message_id])
    await queue.heartbeat([job.message_id])

    pending = await client.xpending_range(
        STREAM, GROUP, min=job.message_id, max=job.message_id, count=1
    )
    assert len(pending) == 1
    assert pending[0]["consumer"] == "worker-a"


@pytest.mark.asyncio
async def test_client_uses_a_blocking_safe_socket_timeout(monkeypatch):
    """Regression guard for the latent durable-worker hang.

    A blocking ``XREADGROUP ... BLOCK 5000`` holds the socket open for the
    full claim window; with ``socket_timeout`` left at the library default
    the read deadline collapses onto that window and the read times out
    before the server's empty reply arrives — so every idle claim fails
    and the worker never picks up a job. The fakeredis-injected tests
    above never block, so only this test (exercising the real ``from_url``
    path) would catch a regression. The socket timeout MUST exceed the
    worker's blocking-claim duration.
    """
    from inqtrix.worker.loop import _CLAIM_BLOCK_MS

    captured: dict = {}

    def fake_from_url(url, **kwargs):
        captured.update(kwargs)
        return fakeredis.FakeAsyncRedis(decode_responses=True)

    from valkey.asyncio import Valkey

    monkeypatch.setattr(Valkey, "from_url", staticmethod(fake_from_url))
    assert ValkeyRunQueue(url="redis://ignored")._client is not None

    assert captured["socket_keepalive"] is True
    assert captured["socket_timeout"] > _CLAIM_BLOCK_MS / 1000


@pytest.mark.asyncio
async def test_dead_letter_moves_payload_and_acks(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(run_id="run_1", tenant_id="default")
    job = (await queue.claim_new(block_ms=1))[0]

    await queue.dead_letter(job, reason="max_attempts_exceeded")

    assert (await client.xpending(STREAM, GROUP))["pending"] == 0
    dead = await client.xrange(DEAD_STREAM)
    assert len(dead) == 1
    assert dead[0][1]["run_id"] == "run_1"
    assert dead[0][1]["reason"] == "max_attempts_exceeded"


@pytest.mark.asyncio
async def test_group_info_reports_consumers_and_depth(client):
    queue = make_queue(client)
    assert await queue.group_info() is None, "no stream yet reads as no group"
    await queue.ensure_group()
    await queue.enqueue(run_id="run_gi", tenant_id="default")
    info = await queue.group_info()
    assert info is not None
    assert info["depth"] == 1
    assert info["consumers"] == 0, "nobody claimed yet"
    claimed = await queue.claim_new(block_ms=1)
    assert claimed, "the enqueued dispatch must be claimable"
    info_after = await queue.group_info()
    assert info_after is not None
    assert info_after["consumers"] >= 1
    assert info_after["pending"] >= 1
