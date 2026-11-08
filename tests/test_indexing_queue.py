"""Offline reindex-queue tests against an in-process fake.

The reindex queue is the run queue's twin over the shared
:class:`~inqtrix.runs.stream_queue.StreamJobQueue`; this suite pins that
it dispatches on its OWN stream (never the run stream) and that
claim/ack/reclaim/dead-letter carry the ``job_id`` payload field.
"""

from __future__ import annotations

import fakeredis
import pytest

from inqtrix.runs.indexing_queue import (
    INDEX_DEAD_STREAM,
    INDEX_JOB_STREAM,
    ValkeyIndexingQueue,
)

GROUP = "workers"


@pytest.fixture()
def client():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def make_queue(client, consumer: str = "worker-a") -> ValkeyIndexingQueue:
    return ValkeyIndexingQueue(client=client, consumer=consumer)


@pytest.mark.asyncio
async def test_enqueue_claim_ack_roundtrip(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(job_id="ix_1", tenant_id="default")

    jobs = await queue.claim_new(block_ms=1)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == "ix_1"
    assert job.tenant_id == "default"
    assert job.delivery_count == 1

    await queue.ack(job.message_id)
    assert (await client.xpending(INDEX_JOB_STREAM, GROUP))["pending"] == 0
    assert await queue.claim_new(block_ms=1) == []


@pytest.mark.asyncio
async def test_uses_a_separate_stream_from_runs(client):
    """A reindex entry must never land on the run stream."""
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(job_id="ix_1", tenant_id="default")
    assert await client.xlen(INDEX_JOB_STREAM) == 1
    assert await client.exists("inqtrix:runs:jobs") == 0


@pytest.mark.asyncio
async def test_reclaim_takes_over_idle_entries_and_counts_delivery(client):
    owner = make_queue(client, consumer="worker-a")
    await owner.ensure_group()
    await owner.enqueue(job_id="ix_1", tenant_id="default")
    assert await owner.claim_new(block_ms=1)

    thief = make_queue(client, consumer="worker-b")
    jobs = await thief.reclaim(min_idle_ms=0)
    assert len(jobs) == 1
    assert jobs[0].job_id == "ix_1"
    assert jobs[0].delivery_count == 2


@pytest.mark.asyncio
async def test_dead_letter_moves_payload_and_acks(client):
    queue = make_queue(client)
    await queue.ensure_group()
    await queue.enqueue(job_id="ix_1", tenant_id="default")
    job = (await queue.claim_new(block_ms=1))[0]

    await queue.dead_letter(job, reason="max_attempts_exceeded")

    assert (await client.xpending(INDEX_JOB_STREAM, GROUP))["pending"] == 0
    dead = await client.xrange(INDEX_DEAD_STREAM)
    assert len(dead) == 1
    assert dead[0][1]["job_id"] == "ix_1"
    assert dead[0][1]["reason"] == "max_attempts_exceeded"
