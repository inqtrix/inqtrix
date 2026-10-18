"""Postgres tests for the indexing store's lost-execution guarantee (gated).

An indexing row stuck in ``running``/``cancelling`` without an executing
thread blocks its whole collection: reindex submissions conflict, the
active-collection checks veto asset deletion, and ``cancelling`` has no
API exit at all. These tests pin the read-triggered fence that frees the
collection, the eager restart sweep, and the paused-rows guard.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text

from inqtrix.runs import durable_store
from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.indexing_orm import indexing_job_events, indexing_jobs
from inqtrix.storage.migrate import run_migrations

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
COLLECTION_ID = "col-lost"


@pytest.fixture(scope="session", autouse=True)
def indexing_lost_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                bypasses = (
                    await session.execute(
                        text(
                            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
                if not bypasses:
                    pytest.fail(
                        "INQTRIX_TEST_DATABASE_URL must connect as "
                        "superuser/BYPASSRLS."
                    )
                await session.execute(text("DELETE FROM indexing_job_events"))
                await session.execute(text("DELETE FROM indexing_jobs"))
                await session.execute(
                    text(
                        "INSERT INTO knowledge_collections "
                        "(id, tenant_id, name, embedding_model, "
                        "embedding_dim, created_at) VALUES "
                        "(:id, 'default', 'Lost fixture', "
                        "'text-embedding-3-large', 8, :created_at) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": COLLECTION_ID, "created_at": time.time()},
                )
    finally:
        await engine.dispose()
    yield


@pytest.fixture()
def zero_grace(monkeypatch):
    monkeypatch.setattr(durable_store, "_EXECUTION_LOST_GRACE_SECONDS", 0.0)


def _build_store() -> PostgresIndexingJobStore:
    return PostgresIndexingJobStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        history_limit=5,
        worker_id="pytest-index-lost",
    )


@pytest.fixture()
def store():
    store = _build_store()
    yield store
    store.close()


async def _seed_job(
    job_id: str,
    *,
    status: str,
    age_seconds: float,
    cancel_requested: bool = False,
) -> None:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                now = time.time()
                await session.execute(
                    insert(indexing_jobs).values(
                        job_id=job_id,
                        tenant_id="default",
                        collection_id=COLLECTION_ID,
                        collection_name="Lost fixture",
                        embedding_model="text-embedding-3-large",
                        generation_id=f"gen_{job_id}",
                        status=status,
                        cancel_requested=cancel_requested,
                        created_at=now - age_seconds,
                        started_at=now - age_seconds,
                    )
                )
    finally:
        await engine.dispose()


async def _job_row(job_id: str):
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            return (
                (
                    await session.execute(
                        select(
                            indexing_jobs.c.status,
                            indexing_jobs.c.error,
                            indexing_jobs.c.cancel_requested,
                        ).where(indexing_jobs.c.job_id == job_id)
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()


async def _failed_event_count(job_id: str) -> int:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            rows = (
                await session.execute(
                    select(indexing_job_events.c.sequence).where(
                        indexing_job_events.c.job_id == job_id,
                        indexing_job_events.c.type == "inqtrix.index.failed",
                    )
                )
            ).all()
            return len(rows)
    finally:
        await engine.dispose()


def wait_for_status(store, job_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = store.get(job_id)
        if summary["status"] in statuses:
            return summary
        store._last_execution_lost_check = None
        time.sleep(0.05)
    pytest.fail(f"job {job_id} never reached {statuses}")


@pytest.mark.asyncio
async def test_lost_cancelling_job_frees_the_collection(store, zero_grace):
    job_id = f"job_lost_{uuid.uuid4().hex}"
    await _seed_job(
        job_id, status="cancelling", age_seconds=300, cancel_requested=True
    )
    store._last_execution_lost_check = None
    assert store.has_active_job(COLLECTION_ID) is False
    row = await _job_row(job_id)
    assert row["status"] == "failed"
    assert row["error"]["type"] == "execution_lost"
    assert row["cancel_requested"] is True, (
        "the user's cancel intent must stay on the record"
    )
    assert await _failed_event_count(job_id) == 1

    def _work(handle):
        handle.begin(1)
        handle.progress(completed_documents=1, current_document_title="doc")
        handle.complete()

    summary = store.submit(
        collection_id=COLLECTION_ID,
        collection_name="Lost fixture",
        embedding_model="text-embedding-3-large",
        work=_work,
    )
    wait_for_status(store, summary["job_id"], {"completed"})


@pytest.mark.asyncio
async def test_restart_sweep_runs_eagerly_at_construction():
    job_id = f"job_boot_{uuid.uuid4().hex}"
    await _seed_job(job_id, status="running", age_seconds=10)
    store = _build_store()
    try:
        assert store._sweep_orphans is False
        row = await _job_row(job_id)
        assert row["status"] == "failed"
        assert row["error"]["type"] == "server_restarted"
        assert store.has_active_job(COLLECTION_ID) is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_paused_rows_are_never_fence_candidates(store, zero_grace):
    job_id = f"job_paused_{uuid.uuid4().hex}"
    await _seed_job(job_id, status="paused_dependency", age_seconds=3600)
    store._last_execution_lost_check = None
    store.get(job_id)
    row = await _job_row(job_id)
    assert row["status"] == "paused_dependency"
