"""An indexing job goes through its embedding phase with its progress in the real tables.

The job body runs on a thread. The coroutine it hands to the loop calls the job's
callbacks and its authority check, and those use the sync store names, which refuse on
the loop. Only a real store reaches that bridge. One slot and one spare thread are what
a process has at its tightest.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
from inqtrix.services.indexing_service import IndexingService
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests._process_loop import run_on_configured_loop
from tests.test_knowledge_engine import StubEmbeddings, make_knowledge_context, make_service

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
APP_ROLE = "inqtrix_app"

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)


async def _on_a_separate_engine(statement: str, **values: Any) -> None:
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    try:
        async with build_session_factory(engine)() as session:
            async with session.begin():
                await session.execute(text(statement), values)
    finally:
        await engine.dispose()


async def _finished(jobs: PostgresIndexingJobStore, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while True:
        job = await jobs.aget(job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        assert time.monotonic() < deadline, job
        await asyncio.sleep(0.05)


class _Authority:
    """Checks with a sync store name, as the real authority reads through its stores."""

    def __init__(self, jobs: PostgresIndexingJobStore) -> None:
        self._jobs = jobs
        self.threads: list[str] = []

    def check(self, collection_id: str, principal: Any) -> None:
        del principal
        self.threads.append(threading.current_thread().name)
        assert self._jobs.has_active_job(collection_id)


def test_a_document_revision_runs_through_its_embedding_phase() -> None:
    async def scenario() -> None:
        embeddings = StubEmbeddings()
        service = make_service(make_knowledge_context(embeddings=embeddings))
        collection = await service.create_collection(name=f"Embedding {uuid.uuid4().hex}")
        await _on_a_separate_engine(
            "INSERT INTO knowledge_collections "
            "(id, tenant_id, name, embedding_model, embedding_dim, created_at) "
            "VALUES (:id, 'default', :name, 'stub-embed-8', 8, :now)",
            id=collection.id,
            name=collection.name,
            now=time.time(),
        )
        engine = build_engine(TEST_DATABASE_URL)
        jobs = PostgresIndexingJobStore(
            engine=engine,
            app_role=APP_ROLE,
            queue=None,
            max_concurrent=1,
            max_queue_size=10,
            completed_ttl_seconds=300,
            history_limit=5,
            worker_id="pytest-embedding-phase",
        )
        try:
            authority = _Authority(jobs)
            indexing = IndexingService(
                knowledge_service=service, job_store=jobs, authority=authority
            )
            summary = await indexing.submit_document_revision(
                collection=collection,
                title="Long enough to embed",
                text=" ".join(f"Satz {number} des Dokuments." for number in range(200)),
            )

            job = await _finished(jobs, summary["job_id"])

            assert job["status"] == "completed", job
            assert embeddings.document_calls > 0
            assert authority.threads
            assert all(name.startswith("inqtrix-legacy_segment") for name in authority.threads)
        finally:
            await jobs.aclose()
            await engine.dispose()
            await _on_a_separate_engine(
                "DELETE FROM indexing_jobs WHERE collection_id = :id", id=collection.id
            )
            await _on_a_separate_engine(
                "DELETE FROM knowledge_collections WHERE id = :id", id=collection.id
            )

    run_on_configured_loop(scenario(), legacy_segment_workers=2)
