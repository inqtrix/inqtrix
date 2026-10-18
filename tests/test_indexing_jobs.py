"""Tests for durable collection-generation and document-revision jobs.

Covers the in-memory :class:`IndexingJobStore` (dispatch, operation-specific
serialization, queue cap, cancel, visibility, history retention) and the
workers' progress, quota, fencing, resume, and publication contracts.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.knowledge.contextualize import (
    ContextualizationDependencyError,
    ContextualizationValidationError,
    LLMChunkContextualizer,
)
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    CollectionMaintenanceActive,
    DocumentNotFound,
    GenerationValidationError,
    KnowledgeProviderContext,
)
from inqtrix.providers.base import LLMResponse
from inqtrix.providers.embeddings import EmbeddingProviderError
from inqtrix.quota.models import (
    QuotaAdjustmentConflict,
    QuotaDimension,
    QuotaSubject,
    estimate_tokens,
)
from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
from inqtrix.server.indexing import (
    IndexingJobConflict,
    IndexingJobHandle,
    IndexingJobNotFound,
    IndexingJobStore,
    IndexingQueueFull,
    IndexingResumeUnavailable,
    format_sse_event,
)
from inqtrix.services.indexing_service import (
    IndexingService,
    ReindexUnsupported,
    execute_reindex_job,
)
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import KnowledgeSettings
from tests.test_knowledge_engine import (
    StubEmbeddings,
    make_knowledge_context,
    make_service,
)

OWNER_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
STRANGER_USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Wait until *predicate* becomes true or fail the test."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


def _store(
    *,
    max_concurrent: int = 1,
    max_queue_size: int = 5,
    completed_ttl_seconds: int = 30,
    event_buffer_size: int = 50,
    history_limit: int = 10,
) -> IndexingJobStore:
    """Create a small reindex job store for unit tests."""
    return IndexingJobStore(
        max_concurrent=max_concurrent,
        max_queue_size=max_queue_size,
        completed_ttl_seconds=completed_ttl_seconds,
        event_buffer_size=event_buffer_size,
        history_limit=history_limit,
    )


def _complete_now(handle: IndexingJobHandle) -> None:
    """Trivial work that finishes immediately with no documents."""
    handle.begin(0)
    handle.complete()


def _submit(store: IndexingJobStore, *, collection_id: str, work=_complete_now, **kw):
    """Submit with sensible defaults for the store-only tests."""
    return store.submit(
        collection_id=collection_id,
        collection_name=kw.pop("collection_name", "Collection"),
        embedding_model=kw.pop("embedding_model", "stub-embed-8"),
        work=work,
        **kw,
    )


# ------------------------------------------------------------------ #
# Store: construction / settings
# ------------------------------------------------------------------ #


def test_from_settings_uses_reindex_knobs() -> None:
    settings = KnowledgeSettings(
        INQTRIX_REINDEX_MAX_CONCURRENT=2,
        INQTRIX_REINDEX_QUEUE_MAX_SIZE=7,
        INQTRIX_REINDEX_COMPLETED_TTL_SECONDS=99,
        INQTRIX_REINDEX_EVENT_BUFFER_SIZE=11,
        INQTRIX_REINDEX_HISTORY_LIMIT=3,
        INQTRIX_GENERATION_ROLLBACK_RETENTION_SECONDS=123,
    )
    store = IndexingJobStore.from_settings(settings)
    assert store._max_concurrent == 2
    assert store._max_queue_size == 7
    assert store._completed_ttl_seconds == 99
    assert settings.generation_rollback_retention_seconds == 123
    assert store._event_buffer_size == 11
    assert store._history_limit == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_concurrent": 0}, "max_concurrent must be >= 1"),
        ({"max_queue_size": -1}, "max_queue_size must be >= 0"),
        ({"completed_ttl_seconds": -1}, "completed_ttl_seconds must be >= 0"),
        ({"event_buffer_size": 0}, "event_buffer_size must be >= 1"),
        ({"history_limit": -1}, "history_limit must be >= 0"),
    ],
)
def test_store_rejects_invalid_limits(kwargs: dict, message: str) -> None:
    options = {
        "max_concurrent": 1,
        "max_queue_size": 1,
        "completed_ttl_seconds": 30,
        "event_buffer_size": 10,
        "history_limit": 5,
        **kwargs,
    }
    with pytest.raises(ValueError, match=message):
        IndexingJobStore(**options)


# ------------------------------------------------------------------ #
# Store: dispatch / lifecycle
# ------------------------------------------------------------------ #


def test_submit_dispatches_and_completes() -> None:
    store = _store()
    summary = _submit(store, collection_id="kc1")
    assert summary["status"] in {"queued", "running", "completed"}
    assert summary["collection_id"] == "kc1"
    assert (
        summary["events_url"]
        == f"/v1/knowledge/indexing-jobs/{summary['job_id']}/events"
    )
    # The submit wire shape is pinned literally and offline, so a field added
    # to the payload cannot ship unnoticed while the Postgres twin is skipped.
    assert set(summary) == {
        "job_id", "collection_id", "collection_name", "embedding_model",
        "operation_kind", "document_id", "revision_id", "index_id", "status",
        "queue_position", "workspace_id", "created_at", "started_at",
        "finished_at", "elapsed_seconds", "total_documents",
        "completed_documents", "percent", "snapshot", "error", "phase",
        "current_batch", "total_batches", "checkpoint", "generation_id",
        "fence_token", "events_url", "last_event_sequence",
    }
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    final = store.get(summary["job_id"])
    assert final["percent"] == 100
    assert final["snapshot"]["progress_estimate"] == 1.0


def test_document_delta_and_collection_generation_share_one_collection_slot() -> None:
    store = _store(max_concurrent=3)
    document_started = threading.Event()
    generation_started = threading.Event()
    release = threading.Event()

    def blocking(started: threading.Event):
        def work(handle: IndexingJobHandle) -> None:
            handle.begin(1)
            started.set()
            release.wait(timeout=2)
            handle.progress(completed_documents=1)

        return work

    document = _submit(
        store,
        collection_id="kc_shared",
        operation_kind="document_revision",
        document_id="kd_shared",
        revision_id="rev_shared",
        work=blocking(document_started),
    )
    _wait_until(document_started.is_set)
    generation = _submit(
        store,
        collection_id="kc_shared",
        operation_kind="collection_generation",
        generation_id="gen_shared",
        work=blocking(generation_started),
    )
    _wait_until(generation_started.is_set)

    with pytest.raises(IndexingJobConflict):
        _submit(
            store,
            collection_id="kc_shared",
            operation_kind="collection_generation",
            generation_id="gen_other",
        )
    assert store.has_active_job("kc_shared") is True

    release.set()
    _wait_until(lambda: store.get(document["job_id"])["status"] == "completed")
    _wait_until(lambda: store.get(generation["job_id"])["status"] == "completed")


def test_concurrent_revision_retries_return_one_active_job() -> None:
    store = _store(max_concurrent=2)
    callers_ready = threading.Barrier(3)
    work_started = threading.Event()
    release = threading.Event()
    work_calls = 0
    results: list[dict] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def work(handle: IndexingJobHandle) -> None:
        nonlocal work_calls
        work_calls += 1
        handle.begin(1)
        work_started.set()
        release.wait(timeout=2)
        handle.progress(completed_documents=1)

    def submit_retry() -> None:
        try:
            callers_ready.wait(timeout=2)
            summary = _submit(
                store,
                collection_id="kc_retry",
                operation_kind="document_revision",
                document_id="kd_retry",
                revision_id="rev_retry",
                work=work,
                created_by_user_id=OWNER_USER_ID,
                created_by_tenant_id="default",
            )
            with result_lock:
                results.append(summary)
        except BaseException as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=submit_retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    callers_ready.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert len(results) == 2
    assert results[0]["job_id"] == results[1]["job_id"]
    assert work_started.wait(timeout=2)
    assert work_calls == 1
    replay = store.subscribe(results[0]["job_id"]).replay
    assert sum(event["type"] == "inqtrix.index.queued" for event in replay) == 1
    collaborator_retry = _submit(
        store,
        collection_id="kc_retry",
        operation_kind="document_revision",
        document_id="kd_retry",
        revision_id="rev_retry",
        created_by_user_id=STRANGER_USER_ID,
        created_by_tenant_id="default",
    )
    assert collaborator_retry["job_id"] == results[0]["job_id"]

    release.set()
    _wait_until(lambda: store.get(results[0]["job_id"])["status"] == "completed")


def test_terminal_revision_failure_and_cancel_release_retry_slot() -> None:
    store = _store()

    def fail(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        raise RuntimeError("embedding failed")

    failed = _submit(
        store,
        collection_id="kc_retry_terminal",
        operation_kind="document_revision",
        document_id="kd_retry_terminal",
        revision_id="rev_retry_terminal",
        work=fail,
    )
    _wait_until(lambda: store.get(failed["job_id"])["status"] == "failed")

    after_failure = _submit(
        store,
        collection_id="kc_retry_terminal",
        operation_kind="document_revision",
        document_id="kd_retry_terminal",
        revision_id="rev_retry_terminal",
    )
    assert after_failure["job_id"] != failed["job_id"]
    _wait_until(lambda: store.get(after_failure["job_id"])["status"] == "completed")

    blocker_started = threading.Event()
    release = threading.Event()

    def block(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        blocker_started.set()
        release.wait(timeout=2)

    cancelling = _submit(
        store,
        collection_id="kc_retry_cancelled",
        operation_kind="document_revision",
        document_id="kd_retry_cancelled",
        revision_id="rev_retry_cancelled",
        work=block,
    )
    assert blocker_started.wait(timeout=2)
    store.cancel(cancelling["job_id"])
    release.set()
    _wait_until(lambda: store.get(cancelling["job_id"])["status"] == "cancelled")

    after_cancel = _submit(
        store,
        collection_id="kc_retry_cancelled",
        operation_kind="document_revision",
        document_id="kd_retry_cancelled",
        revision_id="rev_retry_cancelled",
    )
    assert after_cancel["job_id"] != cancelling["job_id"]
    _wait_until(lambda: store.get(after_cancel["job_id"])["status"] == "completed")


def test_paused_generation_resumes_alongside_active_document_delta() -> None:
    store = _store(max_concurrent=2)
    resumed_generation = threading.Event()
    document_started = threading.Event()
    release = threading.Event()
    generation_calls = 0

    def generation_work(handle: IndexingJobHandle) -> None:
        nonlocal generation_calls
        generation_calls += 1
        handle.begin(1)
        if generation_calls == 1:
            handle.pause_dependency("provider unavailable")
            return
        resumed_generation.set()
        release.wait(timeout=2)
        handle.progress(completed_documents=1)

    generation = _submit(
        store,
        collection_id="kc_resume_shared",
        operation_kind="collection_generation",
        generation_id="gen_resume_shared",
        work=generation_work,
    )
    _wait_until(
        lambda: store.get(generation["job_id"])["status"] == "paused_dependency"
    )

    def document_work(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        document_started.set()
        release.wait(timeout=2)
        handle.progress(completed_documents=1)

    document = _submit(
        store,
        collection_id="kc_resume_shared",
        operation_kind="document_revision",
        document_id="kd_resume_shared",
        revision_id="rev_resume_shared",
        work=document_work,
    )
    _wait_until(document_started.is_set)
    resumed = store.resume(generation["job_id"])
    assert resumed["status"] in {"queued", "running"}
    _wait_until(resumed_generation.is_set)

    release.set()
    _wait_until(lambda: store.get(document["job_id"])["status"] == "completed")
    _wait_until(lambda: store.get(generation["job_id"])["status"] == "completed")


def test_memory_retention_never_expires_a_paused_checkpoint() -> None:
    store = _store(completed_ttl_seconds=30)
    executions = 0

    def pause_once(handle: IndexingJobHandle) -> None:
        nonlocal executions
        executions += 1
        handle.begin(1)
        if executions == 1:
            handle.checkpoint_context_batch(
                "kd_memory_pause",
                {
                    "batch_index": 0,
                    "batch_size": 1,
                    "contexts": ["retained"],
                    "document_id": "kd_memory_pause",
                    "prompt_hash": "prompt-memory",
                    "total_batches": 2,
                },
            )
            handle.pause_dependency("provider unavailable")
            return
        handle.progress(completed_documents=1)

    summary = _submit(store, collection_id="kc_pause_ttl", work=pause_once)
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "paused_dependency")
    paused = store.get(summary["job_id"])
    with store._lock:
        store._records[summary["job_id"]].created_at -= 365 * 86_400

    assert store.get(summary["job_id"])["status"] == "paused_dependency"
    assert store.get(summary["job_id"])["checkpoint"] == paused["checkpoint"]
    store.resume(summary["job_id"])
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    assert executions == 2


def test_parallel_document_checkpoints_remain_independently_resumable() -> None:
    store = _store()
    observed: dict[str, list[dict]] = {}

    def work(handle: IndexingJobHandle) -> None:
        handle.begin(2)
        barrier = threading.Barrier(3)

        def checkpoint(document_id: str, context: str) -> None:
            barrier.wait()
            handle.checkpoint_context_batch(
                document_id,
                {
                    "batch_number": 1,
                    "batch_size": 1,
                    "contexts": [context],
                    "document_id": document_id,
                    "prompt_hash": f"prompt-{document_id}",
                    "total_batches": 2,
                },
            )

        workers = [
            threading.Thread(target=checkpoint, args=("kd_parallel_a", "a")),
            threading.Thread(target=checkpoint, args=("kd_parallel_b", "b")),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        observed["a_before"] = handle.context_batch_checkpoints("kd_parallel_a")
        observed["b_before"] = handle.context_batch_checkpoints("kd_parallel_b")
        handle.document_progress(
            "kd_parallel_a",
            "contextualization",
            current_batch=1,
            total_batches=2,
        )
        handle.document_progress(
            "kd_parallel_b",
            "contextualization",
            current_batch=1,
            total_batches=2,
        )
        handle.checkpoint_document("kd_parallel_a")
        observed["a_after"] = handle.context_batch_checkpoints("kd_parallel_a")
        observed["b_after"] = handle.context_batch_checkpoints("kd_parallel_b")
        handle.pause_dependency("retain unfinished document")

    summary = _submit(
        store,
        collection_id="kc_parallel_checkpoint",
        work=work,
    )
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "paused_dependency")

    assert len(observed["a_before"]) == 1
    assert len(observed["b_before"]) == 1
    assert observed["a_after"] == []
    assert observed["b_after"] == observed["b_before"]
    public = store.get(summary["job_id"])["checkpoint"]["contextualization"]
    assert public == {
        "active_documents": 1,
        "completed_batches": 1,
        "document_id": "kd_parallel_b",
        "total_batches": 2,
    }
    assert store.get(summary["job_id"])["checkpoint"]["document_progress"] == {
        "kd_parallel_b": {
            "current_batch": 1,
            "phase": "contextualization",
            "total_batches": 2,
        }
    }


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "cancelling",
        "paused_dependency",
        "paused_validation",
    ],
)
def test_postgres_retention_has_no_nonterminal_age_action(status: str) -> None:
    row = {
        "created_at": 0.0,
        "finished_at": None,
        "job_id": "ix_old_nonterminal",
        "status": status,
    }

    action = PostgresIndexingJobStore._maintenance_action_for_row(
        row,
        recovery_ids=set(),
        history_ids=set(),
        terminal_cutoff=time.time(),
    )

    assert action is None


@pytest.mark.parametrize("status", ["paused_dependency", "paused_validation"])
def test_postgres_restart_recovery_excludes_paused_statuses(status: str) -> None:
    row = {
        "created_at": 0.0,
        "finished_at": None,
        "job_id": "ix_paused_restart",
        "status": status,
    }

    action = PostgresIndexingJobStore._maintenance_action_for_row(
        row,
        recovery_ids={row["job_id"]},
        history_ids=set(),
        terminal_cutoff=time.time(),
    )

    assert action is None


def test_resume_reconstruction_rejects_missing_identity_without_transition() -> None:
    service = make_service(make_knowledge_context())
    jobs = _store()
    indexing = IndexingService(knowledge_service=service, job_store=jobs)

    def invalid_revision(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        handle.pause_validation("reserved identity missing")

    summary = _submit(
        jobs,
        collection_id="kc_invalid_resume",
        operation_kind="document_revision",
        document_id=None,
        revision_id=None,
        work=invalid_revision,
    )
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_validation")
    paused = jobs.get(summary["job_id"])

    with pytest.raises(IndexingResumeUnavailable):
        indexing.resume(summary["job_id"], principal=None)

    after = jobs.get(summary["job_id"])
    assert after["status"] == "paused_validation"
    assert after["checkpoint"] == paused["checkpoint"]


def test_document_revision_jobs_publish_only_the_latest_requested_source() -> None:
    class BlockingRevisionEmbeddings(StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.started = {
                "revision A": threading.Event(),
                "revision B": threading.Event(),
            }
            self.release = {
                "revision A": threading.Event(),
                "revision B": threading.Event(),
            }

        def embed_documents(self, texts, *, model=None):
            joined = "\n".join(texts)
            for marker in self.started:
                if marker in joined:
                    self.started[marker].set()
                    self.release[marker].wait(timeout=2)
            return super().embed_documents(texts, model=model)

    embeddings = BlockingRevisionEmbeddings()
    context = make_knowledge_context(embeddings=embeddings)
    service = make_service(context)
    jobs = _store(max_concurrent=3)
    indexing = IndexingService(knowledge_service=service, job_store=jobs)
    collection = asyncio.run(
        service.create_collection(name="C", embedding_model="stub-embed-8")
    )
    active_generation = collection.active_generation_id

    async def submit(text: str):
        return await indexing.submit_document_revision(
            collection=collection,
            title="Shared source",
            text=text,
            metadata={"source_id": "document:shared-source"},
        )

    first = asyncio.run(submit("revision A"))
    _wait_until(embeddings.started["revision A"].is_set)
    second = asyncio.run(submit("revision B"))
    _wait_until(embeddings.started["revision B"].is_set)
    third = asyncio.run(submit("revision C"))
    _wait_until(lambda: jobs.get(third["job_id"])["status"] == "completed")
    embeddings.release["revision A"].set()
    embeddings.release["revision B"].set()
    _wait_until(lambda: jobs.get(first["job_id"])["status"] == "superseded")
    _wait_until(lambda: jobs.get(second["job_id"])["status"] == "superseded")

    documents = asyncio.run(service.list_documents(collection.id))
    assert len(documents) == 1
    assert documents[0].id == third["document_id"]
    assert documents[0].text == "revision C"
    assert (
        asyncio.run(
            service.knowledge.store.get_collection(collection.id)
        ).active_generation_id
        == active_generation
    )


def test_document_revision_lost_response_retry_reuses_revision_and_job() -> None:
    class BlockingEmbeddings(StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def embed_documents(self, texts, *, model=None):
            if "same immutable source" in "\n".join(texts):
                self.started.set()
                self.release.wait(timeout=2)
            return super().embed_documents(texts, model=model)

    embeddings = BlockingEmbeddings()
    service = make_service(make_knowledge_context(embeddings=embeddings))
    jobs = _store(max_concurrent=2)
    indexing = IndexingService(knowledge_service=service, job_store=jobs)
    collection = asyncio.run(service.create_collection(name="C"))

    async def submit():
        return await indexing.submit_document_revision(
            collection=collection,
            title="Retry source",
            text="same immutable source",
            metadata={"source_id": "document:lost-response"},
        )

    first = asyncio.run(submit())
    assert embeddings.started.wait(timeout=2)
    retry = asyncio.run(submit())
    assert retry["document_id"] == first["document_id"]
    assert retry["revision_id"] == first["revision_id"]
    assert retry["job_id"] == first["job_id"]

    embeddings.release.set()
    _wait_until(lambda: jobs.get(first["job_id"])["status"] == "completed")
    assert embeddings.document_calls == 1


def test_document_revision_submission_keeps_request_loop_responsive() -> None:
    """The synchronous job-store bridge must never occupy the request loop."""

    release_submit = threading.Event()
    submit_entered = threading.Event()

    class YieldDependentStore(IndexingJobStore):
        def submit(self, **kwargs):
            submit_entered.set()
            if not release_submit.wait(timeout=1):
                raise AssertionError(
                    "the request loop could not run while job submission waited"
                )
            return super().submit(**kwargs)

    service = make_service(make_knowledge_context())
    jobs = YieldDependentStore(
        max_concurrent=1,
        max_queue_size=5,
        completed_ttl_seconds=30,
        event_buffer_size=50,
        history_limit=10,
    )
    indexing = IndexingService(knowledge_service=service, job_store=jobs)
    collection = asyncio.run(service.create_collection(name="C"))

    async def scenario() -> dict:
        async def release_after_submit_enters() -> None:
            await asyncio.to_thread(submit_entered.wait, 1)
            await asyncio.sleep(0)
            release_submit.set()

        release_task = asyncio.create_task(release_after_submit_enters())
        summary = await indexing.submit_document_revision(
            collection=collection,
            title="Responsive submission",
            text="canonical source body",
        )
        await release_task
        return summary

    summary = asyncio.run(scenario())
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "completed")


def test_document_revision_cancel_after_quota_before_publish_keeps_active_source() -> (
    None
):
    """The cancellation fence is rechecked after all provider work and usage."""

    service = make_service(make_knowledge_context())
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    owner = UserContext(principal=principal)
    collection = asyncio.run(
        service.create_collection(
            name="C",
            embedding_model="stub-embed-8",
            created_by_user_id=OWNER_USER_ID,
        )
    )
    previous = asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Canonical",
            text="previous canonical body",
            metadata={"source_id": "document:cancel-before-publish"},
            visible_to=owner,
        )
    )

    class BlockingQuotaReceipt:
        def __init__(self) -> None:
            self.recorded = threading.Event()
            self.release = threading.Event()
            self.calls: list[tuple[QuotaDimension, int, str]] = []

        def subject_for(self, who):
            assert who is principal
            return QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)

        def record_blocking_once(
            self,
            _subject,
            dimension: QuotaDimension,
            amount: int,
            *,
            adjustment_id: str,
        ) -> None:
            self.calls.append((dimension, amount, adjustment_id))
            self.recorded.set()
            self.release.wait(timeout=2)

    quota = BlockingQuotaReceipt()
    jobs = _store()
    indexing = IndexingService(
        knowledge_service=service,
        job_store=jobs,
        quota_service=quota,
    )
    summary = asyncio.run(
        indexing.submit_document_revision(
            collection=collection,
            title="Canonical",
            text="replacement must never become active",
            metadata={"source_id": previous.source_id},
            principal=principal,
            visible_to=owner,
        )
    )
    assert quota.recorded.wait(timeout=2)

    cancelling = jobs.cancel(summary["job_id"])
    assert cancelling["status"] == "cancelling"
    quota.release.set()
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "cancelled")

    final = jobs.get(summary["job_id"])
    current = asyncio.run(service.knowledge.store.get_document(previous.id))
    assert final["completed_documents"] == 0
    assert current.active_revision_id == previous.active_revision_id
    assert current.text == previous.text
    assert all(
        chunk.revision_id != summary["revision_id"]
        for chunk in service.knowledge.store._chunks[previous.id]
    )
    assert len(quota.calls) == 1
    assert quota.calls[0][0] == QuotaDimension.EMBEDDING_TOKENS
    assert quota.calls[0][1] > 0
    assert quota.calls[0][2] == (
        f"knowledge-revision:{summary['revision_id']}:embedding-tokens"
    )
    assert not any(
        event["type"] == "inqtrix.index.document_completed"
        for event in jobs.subscribe(summary["job_id"]).replay
    )


def test_document_revision_resume_keeps_active_source_until_quota_receipt() -> None:
    embeddings = StubEmbeddings()
    service = make_service(make_knowledge_context(embeddings=embeddings))
    owner = UserContext(principal=Principal(user_id=OWNER_USER_ID, kind="oidc_session"))
    collection = asyncio.run(
        service.create_collection(
            name="C",
            embedding_model="stub-embed-8",
            created_by_user_id=OWNER_USER_ID,
        )
    )
    initial = asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Published source",
            text="previous canonical body",
            metadata={"source_id": "document:quota-resume"},
            visible_to=owner,
        )
    )
    initial_revision_id = initial.active_revision_id

    class QuotaUnavailableOnce:
        def __init__(self) -> None:
            self.attempts: list[tuple[QuotaDimension, int, str]] = []

        def subject_for(self, principal):
            assert principal is not None
            return QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)

        def record_blocking_once(
            self,
            _subject,
            dimension: QuotaDimension,
            amount: int,
            *,
            adjustment_id: str,
        ) -> None:
            self.attempts.append((dimension, amount, adjustment_id))
            if len(self.attempts) == 1:
                raise RuntimeError("quota store unavailable")

    quota = QuotaUnavailableOnce()
    jobs = _store()
    indexing = IndexingService(
        knowledge_service=service,
        job_store=jobs,
        quota_service=quota,
    )
    summary = asyncio.run(
        indexing.submit_document_revision(
            collection=collection,
            title="Durable source",
            text="canonical revision body",
            metadata={"source_id": "document:quota-resume"},
            principal=owner.principal,
            visible_to=owner,
        )
    )
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_dependency")

    paused = jobs.get(summary["job_id"])
    calls_after_prepare = embeddings.document_calls
    document = asyncio.run(service.knowledge.store.get_document(summary["document_id"]))
    assert document.id == initial.id
    assert document.active_revision_id == initial_revision_id
    assert document.text == "previous canonical body"
    assert document.lifecycle_status == "active"
    assert paused["error"]["type"] == "quota_dependency_error"
    assert "was not activated" in paused["error"]["message"]
    assert paused["completed_documents"] == 0
    assert len(quota.attempts) == 1

    # Simulate a no-queue process restart: the durable identity/checkpoint
    # survives but the captured Python callable does not. Service-level resume
    # must rebuild the same document-revision operation before requeueing it.
    with jobs._lock:
        jobs._records[summary["job_id"]].work = None
    indexing.resume(summary["job_id"], principal=owner.principal)
    _wait_until(
        lambda: jobs.get(summary["job_id"])["status"]
        in {"completed", "failed", "paused_dependency", "paused_validation"}
    )
    result = jobs.get(summary["job_id"])
    assert result["status"] == "completed", result.get("error")

    assert embeddings.document_calls == calls_after_prepare + 1
    assert len(quota.attempts) == 2
    assert quota.attempts[0] == quota.attempts[1]
    assert quota.attempts[0][2] == (
        f"knowledge-revision:{summary['revision_id']}:embedding-tokens"
    )
    published = asyncio.run(
        service.knowledge.store.get_document(summary["document_id"])
    )
    assert published.active_revision_id == summary["revision_id"]
    events = jobs.subscribe(summary["job_id"]).replay
    assert (
        sum(event["type"] == "inqtrix.index.document_completed" for event in events)
        == 1
    )


def test_one_active_job_per_collection_conflicts() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        started.set()
        release.wait(timeout=2)
        handle.progress(completed_documents=1)
        handle.complete()

    store = _store(max_concurrent=1, max_queue_size=5)
    first = _submit(store, collection_id="kcA", work=blocking)
    _wait_until(started.is_set)
    with pytest.raises(IndexingJobConflict):
        _submit(store, collection_id="kcA")
    release.set()
    _wait_until(lambda: store.get(first["job_id"])["status"] == "completed")
    # A terminal job no longer blocks a fresh reindex of the same collection.
    again = _submit(store, collection_id="kcA")
    _wait_until(lambda: store.get(again["job_id"])["status"] == "completed")


def test_queue_full_raises() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        started.set()
        release.wait(timeout=2)
        handle.complete()

    store = _store(max_concurrent=1, max_queue_size=0)
    _submit(store, collection_id="kcA", work=blocking)
    _wait_until(started.is_set)
    with pytest.raises(IndexingQueueFull):
        _submit(store, collection_id="kcB")
    release.set()


def test_cancel_queued_job_marks_cancelled() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(handle: IndexingJobHandle) -> None:
        started.set()
        release.wait(timeout=2)
        handle.complete()

    store = _store(max_concurrent=1, max_queue_size=5)
    running = _submit(store, collection_id="kcA", work=blocking)
    _wait_until(started.is_set)
    queued = _submit(store, collection_id="kcB")
    assert store.get(queued["job_id"])["status"] == "queued"
    cancelled = store.cancel(queued["job_id"])
    assert cancelled["status"] == "cancelled"
    release.set()
    _wait_until(lambda: store.get(running["job_id"])["status"] == "completed")


def test_cancel_running_job_is_observed_by_worker() -> None:
    started = threading.Event()

    def cooperative(handle: IndexingJobHandle) -> None:
        handle.begin(3)
        started.set()
        # Spin until cancellation is requested, then exit cleanly.
        for _ in range(500):
            if handle.cancelled:
                handle.cancel("client_requested_cancel")
                return
            time.sleep(0.005)
        handle.complete()

    store = _store(max_concurrent=1)
    summary = _submit(store, collection_id="kcA", work=cooperative)
    _wait_until(started.is_set)
    store.cancel(summary["job_id"])
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "cancelled")


@pytest.mark.parametrize("failure_kind", ["dependency", "validation"])
def test_cancel_cannot_be_overwritten_by_concurrent_pause(
    failure_kind: str,
) -> None:
    """A provider/validation unwind after cancel converges to cancelled."""

    started = threading.Event()
    release = threading.Event()

    def failing_after_cancel(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        started.set()
        release.wait(timeout=2)
        if failure_kind == "dependency":
            raise ContextualizationDependencyError(
                error_type="contextualization_provider_timeout"
            )
        raise GenerationValidationError("invalid staged generation")

    store = _store(max_concurrent=1)
    summary = _submit(
        store,
        collection_id=f"kc-cancel-{failure_kind}",
        work=failing_after_cancel,
    )
    assert started.wait(timeout=2)
    cancelling = store.cancel(summary["job_id"])
    assert cancelling["status"] == "cancelling"
    release.set()

    _wait_until(lambda: store.get(summary["job_id"])["status"] == "cancelled")
    subscription = store.subscribe(summary["job_id"])
    try:
        event_types = [event["type"] for event in subscription.replay]
    finally:
        subscription.close()
    assert "inqtrix.index.cancelled" in event_types
    assert "inqtrix.index.paused_dependency" not in event_types
    assert "inqtrix.index.paused_validation" not in event_types


def test_cancelling_job_reserves_collection_until_worker_exits() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        started.set()
        release.wait(timeout=2)
        handle.complete()

    store = _store(max_concurrent=1)
    summary = _submit(store, collection_id="kcA", work=blocking)
    _wait_until(started.is_set)

    cancelling = store.cancel(summary["job_id"])
    assert cancelling["status"] == "cancelling"
    assert store.has_active_job("kcA") is True
    with pytest.raises(CollectionMaintenanceActive):
        store.run_collection_mutation("kcA", lambda: None)
    with pytest.raises(IndexingJobConflict):
        _submit(store, collection_id="kcA")

    release.set()
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "cancelled")
    assert store.has_active_job("kcA") is False
    assert store.run_collection_mutation("kcA", lambda: "landed") == "landed"


# ------------------------------------------------------------------ #
# Store: visibility / retention / events
# ------------------------------------------------------------------ #


def test_visibility_scopes_jobs_to_creator() -> None:
    store = _store()
    owner = UserContext(principal=Principal(user_id=OWNER_USER_ID, kind="oidc_session"))
    stranger = UserContext(
        principal=Principal(user_id=STRANGER_USER_ID, kind="oidc_session")
    )
    summary = _submit(
        store,
        collection_id="kc1",
        created_by_user_id=OWNER_USER_ID,
        created_by_tenant_id="default",
    )
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    assert len(store.list(visible_to=owner)) == 1
    assert store.list(visible_to=stranger) == []
    assert store.get(summary["job_id"], visible_to=owner)["status"] == "completed"
    with pytest.raises(IndexingJobNotFound):
        store.get(summary["job_id"], visible_to=stranger)


def test_history_capped_per_collection() -> None:
    store = _store(history_limit=2)
    seen: list[str] = []
    for _ in range(3):
        summary = _submit(store, collection_id="kcH")
        job_id = summary["job_id"]
        _wait_until(
            lambda jid=job_id: any(
                job["job_id"] == jid and job["status"] == "completed"
                for job in store.list(collection_id="kcH")
            )
        )
        seen.append(job_id)
    remaining = {job["job_id"] for job in store.list(collection_id="kcH")}
    assert len(remaining) == 2
    # The two NEWEST terminal records survive; the oldest is evicted.
    assert seen[0] not in remaining
    assert seen[1] in remaining
    assert seen[2] in remaining


def test_subscribe_replays_lifecycle_events() -> None:
    store = _store()
    summary = _submit(store, collection_id="kc1")
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    subscription = store.subscribe(summary["job_id"])
    try:
        types = [event["type"] for event in subscription.replay]
        last_sequence = subscription.replay[-1]["sequence"]
    finally:
        subscription.close()
    assert "inqtrix.index.queued" in types
    assert "inqtrix.index.started" in types
    assert "inqtrix.index.completed" in types
    assert types[-1] == "inqtrix.index.completed"
    assert store.get(summary["job_id"])["last_event_sequence"] == last_sequence

    tail = store.subscribe(
        summary["job_id"],
        after_sequence=last_sequence - 1,
    )
    try:
        assert [event["type"] for event in tail.replay] == [
            "inqtrix.index.completed"
        ]
    finally:
        tail.close()

    current = store.subscribe(
        summary["job_id"],
        after_sequence=last_sequence,
    )
    try:
        assert current.replay == []
    finally:
        current.close()


def test_format_sse_event_renders_frame() -> None:
    frame = format_sse_event(
        {"type": "inqtrix.index.progress", "job_id": "ix_1", "sequence": 2, "data": {}}
    )
    assert frame.startswith("event: inqtrix.index.progress\n")
    assert "\n\n" in frame


# ------------------------------------------------------------------ #
# Worker: execute_reindex_job + IndexingService
# ------------------------------------------------------------------ #


def _service_with_docs(
    *texts: str,
    owner_user_id: uuid.UUID | None = None,
) -> tuple[KnowledgeService, StubEmbeddings, object]:
    embeddings = StubEmbeddings()
    context = make_knowledge_context(embeddings=embeddings)
    service = make_service(context)
    visible_to = (
        UserContext(
            principal=Principal(
                user_id=owner_user_id,
                kind="oidc_session",
            )
        )
        if owner_user_id is not None
        else None
    )

    async def _seed():
        collection = await service.create_collection(
            name="C",
            created_by_user_id=owner_user_id,
        )
        for index, text in enumerate(texts):
            await service.add_document(
                collection_id=collection.id,
                title=f"Doc {index}",
                text=text,
                visible_to=visible_to,
            )
        return collection

    collection = asyncio.run(_seed())
    return service, embeddings, collection


def test_reindex_reembeds_every_document() -> None:
    service, embeddings, collection = _service_with_docs("alpha beta", "gamma delta")
    calls_before = embeddings.document_calls
    store = _store()
    indexing = IndexingService(knowledge_service=service, job_store=store)
    summary = indexing.submit(collection=collection)
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    final = store.get(summary["job_id"])
    assert final["total_documents"] == 2
    assert final["completed_documents"] == 2
    assert final["percent"] == 100
    # Both documents were embedded a second time by the reindex.
    assert embeddings.document_calls == calls_before + 2


def test_collection_reindex_processes_three_documents_concurrently(
    monkeypatch,
) -> None:
    service, _embeddings, collection = _service_with_docs(
        "alpha",
        "beta",
        "gamma",
        "delta",
    )
    real_reembed = service.reembed_document_with_receipt
    release = threading.Event()
    three_started = threading.Event()
    active_lock = threading.Lock()
    active = 0
    maximum_active = 0

    async def blocking_reembed(**kwargs):
        nonlocal active, maximum_active
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 3:
                three_started.set()
        try:
            await asyncio.to_thread(release.wait, 5)
            return await real_reembed(**kwargs)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(service, "reembed_document_with_receipt", blocking_reembed)
    store = _store()
    summary = IndexingService(
        knowledge_service=service,
        job_store=store,
    ).submit(collection=collection)
    try:
        _wait_until(three_started.is_set)
        replay = store.subscribe(summary["job_id"]).replay
        started = [
            event
            for event in replay
            if event["type"] == "inqtrix.index.document_started"
        ]
        completed = [
            event
            for event in replay
            if event["type"] == "inqtrix.index.document_completed"
        ]
        assert len(started) == 3
        assert completed == []
        assert maximum_active == 3
    finally:
        release.set()

    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    assert maximum_active == 3


def test_reindex_reloads_each_document_before_embedding(monkeypatch) -> None:
    service, _embeddings, collection = _service_with_docs("canonical text")
    store = service.knowledge.store
    canonical = asyncio.run(store.list_documents(collection.id))[0]

    async def stale_enumeration(_collection_id: str):
        return [replace(canonical, title="stale title", text="stale text")]

    observed: list[tuple[str, str]] = []
    real_reembed = service.reembed_document_with_receipt

    async def capture_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        if authority_check is not None:
            authority_check()
        observed.append((document.title, document.text))
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(store, "list_documents", stale_enumeration)
    monkeypatch.setattr(service, "reembed_document_with_receipt", capture_reembed)

    indexing_store = _store()
    indexing = IndexingService(knowledge_service=service, job_store=indexing_store)
    summary = indexing.submit(collection=collection)
    _wait_until(lambda: indexing_store.get(summary["job_id"])["status"] == "completed")
    assert observed == [(canonical.title, canonical.text)]


def test_reindex_repairs_staging_once_after_publication_validation_fault(
    monkeypatch,
) -> None:
    service, embeddings, collection = _service_with_docs(
        "canonical text",
        owner_user_id=OWNER_USER_ID,
    )
    store = service.knowledge.store
    real_activate = store.activate_generation
    activation_attempts = 0

    class IdempotentQuota:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.receipts: set[str] = set()

        def subject_for(self, principal):
            assert principal is not None
            return QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)

        def record_blocking_once(
            self,
            _subject,
            _dimension,
            _amount,
            *,
            adjustment_id: str,
        ) -> None:
            self.calls.append(adjustment_id)
            self.receipts.add(adjustment_id)

    quota = IdempotentQuota()

    async def fail_first_activation(**kwargs):
        nonlocal activation_attempts
        activation_attempts += 1
        if activation_attempts == 1:
            raise GenerationValidationError("fault-injected missing point")
        return await real_activate(**kwargs)

    monkeypatch.setattr(store, "activate_generation", fail_first_activation)
    indexing_store = _store()
    summary = IndexingService(
        knowledge_service=service,
        job_store=indexing_store,
        quota_service=quota,
    ).submit(
        collection=collection,
        principal=Principal(user_id=OWNER_USER_ID, kind="oidc_session"),
    )

    _wait_until(lambda: indexing_store.get(summary["job_id"])["status"] == "completed")

    assert activation_attempts == 2
    assert embeddings.document_calls >= 3
    assert len(quota.receipts) == 1
    assert len(quota.calls) == 1


def test_reindex_forwards_live_authority_and_canonical_actor(monkeypatch) -> None:
    service, _embeddings, collection = _service_with_docs(
        "canonical text",
        owner_user_id=OWNER_USER_ID,
    )
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    observed_actors: list[uuid.UUID | None] = []

    class RecordingAuthority:
        def __init__(self) -> None:
            self.calls = 0

        def check(self, collection_id: str, checked: Principal | None) -> None:
            assert collection_id == collection.id
            assert checked == principal
            self.calls += 1

    authority = RecordingAuthority()
    real_reembed = service.reembed_document_with_receipt

    async def capture_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        assert document.collection_id == collection.id
        assert embedding_model == collection.embedding_model
        assert authority_check is not None
        authority_check()
        observed_actors.append(actor_user_id)
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(service, "reembed_document_with_receipt", capture_reembed)
    job_store = _store()
    indexing = IndexingService(
        knowledge_service=service,
        job_store=job_store,
        authority=authority,
    )

    summary = indexing.submit(collection=collection, principal=principal)
    _wait_until(lambda: job_store.get(summary["job_id"])["status"] == "completed")

    assert observed_actors == [OWNER_USER_ID]
    assert authority.calls >= 4


def test_reindex_folds_concurrent_document_mutations_into_shadow_generation(
    monkeypatch,
) -> None:
    service, _embeddings, collection = _service_with_docs("alpha")
    document = asyncio.run(service.knowledge.store.list_documents(collection.id))[0]
    started = threading.Event()
    release = threading.Event()
    real_reembed = service.reembed_document_with_receipt

    async def blocking_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(service, "reembed_document_with_receipt", blocking_reembed)
    job_store = _store()
    indexing = IndexingService(knowledge_service=service, job_store=job_store)
    summary = indexing.submit(collection=collection)
    _wait_until(started.is_set)
    try:
        added = asyncio.run(
            service.add_document(
                collection_id=collection.id,
                title="delta",
                text="delta content",
            )
        )
        asyncio.run(service.delete_document(document.id))
        with pytest.raises(CollectionMaintenanceActive):
            asyncio.run(service.delete_collection(collection.id))
    finally:
        release.set()

    _wait_until(lambda: job_store.get(summary["job_id"])["status"] == "completed")
    documents = asyncio.run(service.knowledge.store.list_documents(collection.id))
    assert [item.id for item in documents] == [added.id]
    chunks = asyncio.run(service.knowledge.store.get_chunks(added.id))
    active = asyncio.run(service.knowledge.store.get_collection(collection.id))
    assert chunks
    assert {chunk.generation_id for chunk in chunks} == {active.active_generation_id}


def test_reindex_emits_one_document_completed_event_per_document() -> None:
    service, _embeddings, collection = _service_with_docs("alpha beta", "gamma delta")
    store = _store()
    indexing = IndexingService(knowledge_service=service, job_store=store)
    summary = indexing.submit(collection=collection)
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    replay = store.subscribe(summary["job_id"]).replay
    doc_events = [
        event for event in replay if event["type"] == "inqtrix.index.document_completed"
    ]
    started_events = [
        event for event in replay if event["type"] == "inqtrix.index.document_started"
    ]
    progress_events = [
        event for event in replay if event["type"] == "inqtrix.index.document_progress"
    ]
    assert len(started_events) == 2
    assert {event["data"]["document_id"] for event in started_events} == {
        event["data"]["document_id"] for event in doc_events
    }
    assert {event["data"]["document_id"] for event in progress_events} == {
        event["data"]["document_id"] for event in doc_events
    }
    # One per re-embedded document, each carrying its backend document id.
    assert len(doc_events) == 2
    assert all(event["data"]["outcome"] == "embedded" for event in doc_events)
    assert all(event["data"]["document_id"] for event in doc_events)
    # The per-document events land before the terminal completed event.
    types = [event["type"] for event in replay]
    last_doc = max(
        index
        for index, kind in enumerate(types)
        if kind == "inqtrix.index.document_completed"
    )
    assert types.index("inqtrix.index.completed") > last_doc
    for started in started_events:
        document_id = started["data"]["document_id"]
        start_index = next(
            index
            for index, event in enumerate(replay)
            if event["type"] == "inqtrix.index.document_started"
            and event["data"]["document_id"] == document_id
        )
        complete_index = next(
            index
            for index, event in enumerate(replay)
            if event["type"] == "inqtrix.index.document_completed"
            and event["data"]["document_id"] == document_id
        )
        document_progress_indices = [
            index
            for index, event in enumerate(replay)
            if event["type"] == "inqtrix.index.document_progress"
            and event["data"]["document_id"] == document_id
        ]
        assert document_progress_indices
        assert start_index < min(document_progress_indices) < complete_index


def test_reindex_emits_no_document_completed_for_a_vanished_document(
    monkeypatch,
) -> None:
    service, _embeddings, collection = _service_with_docs("alpha", "beta")
    store = _store()
    # One document is deleted between enumeration and re-embed (DocumentNotFound):
    # it must be skipped WITHOUT emitting a per-document event — only the
    # surviving document flips its file row.
    real_reembed = service.reembed_document_with_receipt

    async def vanishing_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        if document.title == "Doc 0":
            raise DocumentNotFound(document.id)
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(service, "reembed_document_with_receipt", vanishing_reembed)
    indexing = IndexingService(knowledge_service=service, job_store=store)
    summary = indexing.submit(collection=collection)
    _wait_until(
        lambda: store.get(summary["job_id"])["status"]
        in {"failed", "cancelled", "completed"}
    )
    assert store.get(summary["job_id"])["status"] == "failed"
    replay = store.subscribe(summary["job_id"]).replay
    doc_events = [
        event for event in replay if event["type"] == "inqtrix.index.document_completed"
    ]
    assert len(doc_events) == 1


def test_reindex_records_embedding_quota_per_document() -> None:
    service, _embeddings, collection = _service_with_docs(
        "alpha beta",
        "gamma delta",
        owner_user_id=OWNER_USER_ID,
    )
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")

    class FakeQuota:
        def __init__(self) -> None:
            self.records: list[tuple] = []
            self.receipts: set[str] = set()

        def subject_for(self, who):
            return (
                QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)
                if who
                else None
            )

        def record_blocking_once(
            self, subject, dimension, amount, *, adjustment_id
        ) -> None:
            self.records.append((subject, dimension, amount, adjustment_id))
            self.receipts.add(adjustment_id)

    quota = FakeQuota()
    store = _store()
    indexing = IndexingService(
        knowledge_service=service, job_store=store, quota_service=quota
    )
    summary = indexing.submit(collection=collection, principal=principal)
    _wait_until(lambda: store.get(summary["job_id"])["status"] == "completed")
    embedding_records = [
        rec for rec in quota.records if rec[1] == QuotaDimension.EMBEDDING_TOKENS
    ]
    assert len(quota.receipts) == 2
    assert len(embedding_records) == 2
    assert all(
        amount > 0 and adjustment_id in quota.receipts
        for _subject, _dim, amount, adjustment_id in embedding_records
    )


@pytest.mark.parametrize("operation_kind", ["generation", "document_revision"])
def test_embedding_quota_uses_exact_contextualized_provider_inputs(
    operation_kind: str,
) -> None:
    prefix = "retrieval-only enterprise pricing context " * 3

    class FixedContextLLM:
        def complete_with_metadata(self, prompt: str, **_kwargs) -> LLMResponse:
            count = prompt.count("CHUNK ")
            return LLMResponse(
                content=json.dumps([f"{prefix}{index}" for index in range(count)]),
                prompt_tokens=10,
                completion_tokens=5,
                model="stub-context",
                finish_reason="stop",
            )

    class CapturingEmbeddings(StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.inputs: list[list[str]] = []

        def embed_documents(self, texts, *, model=None):
            materialized = list(texts)
            self.inputs.append(materialized)
            return super().embed_documents(materialized, model=model)

    class CapturingQuota:
        def __init__(self) -> None:
            self.records: list[tuple[QuotaDimension, int, str]] = []

        def subject_for(self, principal):
            assert principal is not None
            return QuotaSubject(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
            )

        def record_blocking_once(
            self,
            _subject,
            dimension: QuotaDimension,
            amount: int,
            *,
            adjustment_id: str,
        ) -> None:
            self.records.append((dimension, amount, adjustment_id))

    store = MemoryKnowledgeStore()
    embeddings = CapturingEmbeddings()
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    owner = UserContext(principal=principal)
    raw_service = make_service(
        make_knowledge_context(store=store, embeddings=embeddings)
    )
    collection = asyncio.run(
        raw_service.create_collection(
            name="Exact metering",
            created_by_user_id=OWNER_USER_ID,
        )
    )
    quota = CapturingQuota()
    jobs = _store()
    contextualized_service = make_service(
        make_knowledge_context(
            store=store,
            embeddings=embeddings,
            contextualizer=LLMChunkContextualizer(FixedContextLLM()),
        )
    )
    indexing = IndexingService(
        knowledge_service=contextualized_service,
        job_store=jobs,
        quota_service=quota,
    )
    raw_text = "canonical source body"

    if operation_kind == "generation":
        asyncio.run(
            raw_service.add_document(
                collection_id=collection.id,
                title="Source",
                text=raw_text,
                visible_to=owner,
            )
        )
        summary = indexing.submit(collection=collection, principal=principal)
    else:
        summary = asyncio.run(
            indexing.submit_document_revision(
                collection=collection,
                title="Source",
                text=raw_text,
                metadata={"source_id": "document:exact-metering"},
                principal=principal,
                visible_to=owner,
            )
        )

    _wait_until(
        lambda: jobs.get(summary["job_id"])["status"]
        in {"completed", "failed", "paused_dependency", "paused_validation"}
    )
    result = jobs.get(summary["job_id"])
    assert result["status"] == "completed", result.get("error")
    provider_inputs = embeddings.inputs[-1]
    assert provider_inputs
    assert all(text.startswith(prefix) for text in provider_inputs)
    expected_amount = sum(estimate_tokens(text) for text in provider_inputs)
    embedding_records = [
        record
        for record in quota.records
        if record[0] is QuotaDimension.EMBEDDING_TOKENS
    ]
    assert len(embedding_records) == 1
    assert embedding_records[0][1] == expected_amount
    assert expected_amount > estimate_tokens(raw_text)


@pytest.mark.parametrize("crash_after_receipt", [False, True])
def test_reindex_resume_fences_receipt_before_document_checkpoint(
    crash_after_receipt: bool,
) -> None:
    service, embeddings, collection = _service_with_docs(
        "immutable generation source",
        owner_user_id=OWNER_USER_ID,
    )
    calls_before = embeddings.document_calls
    order: list[str] = []

    class SimulatedProcessCrash(BaseException):
        pass

    class IdempotentCrashQuota:
        def __init__(self) -> None:
            self.crash_pending = True
            self.calls: list[str] = []
            self.receipts: set[str] = set()

        def record_blocking_once(
            self,
            _subject,
            _dimension,
            _amount,
            *,
            adjustment_id: str,
        ) -> None:
            order.append("receipt")
            self.calls.append(adjustment_id)
            if self.crash_pending and not crash_after_receipt:
                self.crash_pending = False
                raise SimulatedProcessCrash
            self.receipts.add(adjustment_id)
            if self.crash_pending:
                self.crash_pending = False
                raise SimulatedProcessCrash

    class ResumeHandle:
        raw_by_user_choice = False

        def __init__(self) -> None:
            self.completed_document_ids: set[str] = set()
            self.embedding_receipts: dict[str, dict] = {}
            self.completed = False

        @property
        def cancelled(self) -> bool:
            return False

        def begin(self, _total: int) -> None:
            pass

        def progress(self, **_kwargs) -> None:
            pass

        def phase(self, _name: str, **_kwargs) -> None:
            pass

        def context_batch_checkpoints(self, _document_id: str) -> list[dict]:
            return []

        def checkpoint_context_batch(
            self, _document_id: str, _checkpoint: dict
        ) -> None:
            pass

        def document_completed(
            self, document_id: str, *, context_omitted_chunks: int = 0
        ) -> None:
            order.append("document_completed")
            assert document_id not in self.completed_document_ids

        def checkpoint_document(
            self,
            document_id: str,
            *,
            embedding_receipt: dict | None = None,
        ) -> None:
            order.append("checkpoint")
            self.completed_document_ids.add(document_id)
            if embedding_receipt is not None:
                self.embedding_receipts[document_id] = dict(embedding_receipt)

        def embedding_receipt(self, document_id: str) -> dict | None:
            receipt = self.embedding_receipts.get(document_id)
            return dict(receipt) if receipt is not None else None

        def complete(self) -> None:
            order.append("complete")
            self.completed = True

        def cancel(self, _reason: str) -> None:  # pragma: no cover - invariant
            raise AssertionError("unexpected cancellation")

    quota = IdempotentCrashQuota()
    handle = ResumeHandle()
    generation_id = "gen_quota_receipt_crash"
    quota_subject = QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)

    with pytest.raises(SimulatedProcessCrash):
        execute_reindex_job(
            handle,
            knowledge_service=service,
            collection_id=collection.id,
            embedding_model=collection.embedding_model,
            generation_id=generation_id,
            quota_service=quota,
            quota_subject=quota_subject,
            actor_user_id=OWNER_USER_ID,
        )

    assert handle.completed_document_ids == set()
    assert "document_completed" not in order
    assert "checkpoint" not in order
    active_after_crash = asyncio.run(
        service.knowledge.store.get_collection(collection.id)
    )
    assert active_after_crash.active_generation_id != generation_id

    execute_reindex_job(
        handle,
        knowledge_service=service,
        collection_id=collection.id,
        embedding_model=collection.embedding_model,
        generation_id=generation_id,
        quota_service=quota,
        quota_subject=quota_subject,
        actor_user_id=OWNER_USER_ID,
    )

    assert handle.completed is True
    assert len(handle.completed_document_ids) == 1
    assert len(quota.receipts) == 1
    assert len(quota.calls) == 2
    assert quota.calls[0] == quota.calls[1]
    assert embeddings.document_calls == calls_before + 2
    receipt_position = max(
        index for index, event in enumerate(order) if event == "receipt"
    )
    assert receipt_position < order.index("document_completed")
    assert order.index("document_completed") < order.index("checkpoint")
    published = asyncio.run(service.knowledge.store.get_collection(collection.id))
    assert published.active_generation_id == generation_id


def test_reindex_never_publishes_without_confirmed_quota_receipts() -> None:
    service, _embeddings, collection = _service_with_docs(
        "receipt-gated source",
        owner_user_id=OWNER_USER_ID,
    )
    original_generation = collection.active_generation_id

    class QuotaUnavailableOnce:
        def __init__(self) -> None:
            self.available = False
            self.calls: list[str] = []
            self.receipts: set[str] = set()

        def subject_for(self, principal):
            assert principal is not None
            return QuotaSubject(tenant_id="default", user_id=OWNER_USER_ID)

        def record_blocking_once(
            self,
            _subject,
            _dimension,
            _amount,
            *,
            adjustment_id: str,
        ) -> None:
            self.calls.append(adjustment_id)
            if not self.available:
                raise RuntimeError("quota database unavailable")
            self.receipts.add(adjustment_id)

    quota = QuotaUnavailableOnce()
    jobs = _store()
    indexing = IndexingService(
        knowledge_service=service,
        job_store=jobs,
        quota_service=quota,
    )
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    summary = indexing.submit(collection=collection, principal=principal)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_dependency")

    paused = jobs.get(summary["job_id"])
    assert paused["error"]["type"] == "quota_dependency_error"
    assert paused["checkpoint"]["completed_document_ids"] == []
    assert not any(
        event["type"] == "inqtrix.index.document_completed"
        for event in jobs.subscribe(summary["job_id"]).replay
    )
    assert (
        asyncio.run(
            service.knowledge.store.get_collection(collection.id)
        ).active_generation_id
        == original_generation
    )

    quota.available = True
    indexing.resume(summary["job_id"], principal=principal)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "completed")

    assert len(quota.receipts) == 1
    assert len(quota.calls) == 2
    assert quota.calls[0] == quota.calls[1]
    assert (
        asyncio.run(
            service.knowledge.store.get_collection(collection.id)
        ).active_generation_id
        == summary["generation_id"]
    )


@pytest.mark.parametrize("operation_kind", ["generation", "document_revision"])
def test_contradictory_quota_receipt_blocks_index_publication(
    operation_kind: str,
) -> None:
    service, _embeddings, collection = _service_with_docs(
        "previous canonical source",
        owner_user_id=OWNER_USER_ID,
    )
    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    owner = UserContext(principal=principal)
    previous_document = asyncio.run(
        service.knowledge.store.list_documents(collection.id)
    )[0]
    previous_generation = asyncio.run(
        service.knowledge.store.get_collection(collection.id)
    ).active_generation_id

    class ContradictoryQuota:
        def subject_for(self, who):
            assert who is not None
            return QuotaSubject(tenant_id=who.tenant_id, user_id=who.user_id)

        def record_blocking_once(
            self,
            _subject,
            _dimension,
            _amount,
            *,
            adjustment_id: str,
        ) -> None:
            raise QuotaAdjustmentConflict(adjustment_id)

    jobs = _store()
    indexing = IndexingService(
        knowledge_service=service,
        job_store=jobs,
        quota_service=ContradictoryQuota(),
    )
    if operation_kind == "generation":
        summary = indexing.submit(collection=collection, principal=principal)
    else:
        summary = asyncio.run(
            indexing.submit_document_revision(
                collection=collection,
                title=previous_document.title,
                text="replacement canonical source",
                metadata={"source_id": previous_document.source_id},
                principal=principal,
                visible_to=owner,
            )
        )

    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_validation")
    current_collection = asyncio.run(
        service.knowledge.store.get_collection(collection.id)
    )
    current_document = asyncio.run(
        service.knowledge.store.get_document(previous_document.id)
    )
    assert current_collection.active_generation_id == previous_generation
    assert current_document.active_revision_id == previous_document.active_revision_id
    assert current_document.text == previous_document.text
    assert jobs.get(summary["job_id"])["completed_documents"] == 0


@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_error_type"),
    [
        ("embedding_timeout", "paused_dependency", "embedding_provider_timeout"),
        ("embedding_validation", "failed", "server_error"),
        ("qdrant_503", "paused_dependency", "vector_store_unavailable"),
        ("qdrant_400", "failed", "server_error"),
    ],
)
def test_reindex_pauses_only_for_proven_dependency_failures(
    monkeypatch,
    failure_kind: str,
    expected_status: str,
    expected_error_type: str,
) -> None:
    service, _embeddings, collection = _service_with_docs("provider boundary")

    qdrant_error_type = type(
        "UnexpectedResponse",
        (RuntimeError,),
        {"__module__": "qdrant_client.http.exceptions"},
    )

    async def fail_reembed(**_kwargs):
        if failure_kind == "embedding_timeout":
            try:
                raise TimeoutError("socket deadline")
            except TimeoutError as cause:
                raise EmbeddingProviderError("embedding call failed") from cause
        if failure_kind == "embedding_validation":
            raise EmbeddingProviderError("embedding response count mismatch")
        error = qdrant_error_type("vector response")
        error.status_code = 503 if failure_kind == "qdrant_503" else 400
        raise error

    monkeypatch.setattr(service, "reembed_document_with_receipt", fail_reembed)
    jobs = _store()
    summary = IndexingService(
        knowledge_service=service,
        job_store=jobs,
    ).submit(collection=collection)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == expected_status)

    result = jobs.get(summary["job_id"])
    assert result["error"]["type"] == expected_error_type
    if expected_status == "paused_dependency":
        assert result["generation_id"] == summary["generation_id"]


@pytest.mark.parametrize(
    "error_type",
    [
        "contextualization_provider_timeout",
        "contextualization_provider_rate_limited",
        "contextualization_provider_unavailable",
        "contextualization_provider_circuit_open",
        "contextualization_circuit_state_unavailable",
    ],
)
def test_contextualization_dependency_keeps_its_precise_classification(
    monkeypatch,
    error_type: str,
) -> None:
    service, _embeddings, collection = _service_with_docs("contextualization boundary")

    async def fail_reembed(**_kwargs):
        raise ContextualizationDependencyError(error_type=error_type)

    monkeypatch.setattr(service, "reembed_document_with_receipt", fail_reembed)
    jobs = _store()
    summary = IndexingService(
        knowledge_service=service,
        job_store=jobs,
    ).submit(collection=collection)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_dependency")

    result = jobs.get(summary["job_id"])
    assert result["error"]["type"] == error_type
    assert not result["error"]["type"].startswith("vector_store_")


def test_reindex_cancel_between_documents_stops_early() -> None:
    service, embeddings, collection = _service_with_docs("alpha", "beta", "gamma")
    calls_before = embeddings.document_calls

    class FakeHandle:
        def __init__(self) -> None:
            self.progress_calls: list[int] = []
            self.document_completed_ids: list[str] = []
            self.completed = False
            self.cancel_reason: str | None = None

        @property
        def cancelled(self) -> bool:
            # Cancel becomes visible after the first document is processed.
            return len(self.document_completed_ids) >= 1

        def begin(self, total: int) -> None:
            self.total = total

        def progress(
            self, *, completed_documents: int, current_document_title: str = ""
        ) -> None:
            self.progress_calls.append(completed_documents)

        def document_completed(
            self, document_id: str, *, context_omitted_chunks: int = 0
        ) -> None:
            self.document_completed_ids.append(document_id)

        def complete(self) -> None:
            self.completed = True

        def cancel(self, reason: str = "cancelled") -> None:
            self.cancel_reason = reason

    handle = FakeHandle()
    execute_reindex_job(
        handle,
        knowledge_service=service,
        collection_id=collection.id,
        embedding_model=collection.embedding_model,
        max_parallel_documents=1,
    )
    assert handle.completed is False
    assert handle.cancel_reason == "client_requested_cancel"
    # Only the first document was re-embedded before the cancel took effect.
    assert embeddings.document_calls == calls_before + 1
    # And exactly that one document emitted a per-document completion event.
    assert len(handle.document_completed_ids) == 1


def test_dependency_pause_resumes_from_the_first_unfinished_batch() -> None:
    document_canary = "PRIVATE-DOCUMENT-TITLE-CANARY"
    provider_canaries = (
        "PRIVATE-PROVIDER-BODY-CANARY",
        "https://provider.invalid/private-canary",
        "request-id-private-canary",
    )
    store = MemoryKnowledgeStore()
    embeddings = StubEmbeddings()
    raw_service = make_service(
        make_knowledge_context(store=store, embeddings=embeddings)
    )
    collection = asyncio.run(raw_service.create_collection(name="Langtext"))
    paragraphs = [f"Abschnitt {n}. " + "x" * 1_050 for n in range(30)]
    asyncio.run(
        raw_service.add_document(
            collection_id=collection.id,
            title=document_canary,
            text="\n\n".join(paragraphs),
        )
    )

    class TimeoutOnceLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.failed = False

        def complete_with_metadata(self, prompt: str, **_kwargs) -> LLMResponse:
            self.prompts.append(prompt)
            if len(self.prompts) == 2 and not self.failed:
                self.failed = True
                raise TimeoutError(" ".join(provider_canaries))
            count = prompt.count("CHUNK ")
            return LLMResponse(
                content=json.dumps([f"Kontext {n}" for n in range(count)]),
                prompt_tokens=10,
                completion_tokens=5,
                model="stub-ctx",
                finish_reason="stop",
            )

    llm = TimeoutOnceLLM()
    contextualized_service = make_service(
        make_knowledge_context(
            store=store,
            embeddings=embeddings,
            contextualizer=LLMChunkContextualizer(llm),
        )
    )
    jobs = _store()
    indexing = IndexingService(
        knowledge_service=contextualized_service,
        job_store=jobs,
    )
    summary = indexing.submit(collection=collection)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_dependency")
    paused = jobs.get(summary["job_id"])
    assert paused["error"]["type"] == "contextualization_provider_timeout"
    error_payload = json.dumps(paused["error"])
    assert document_canary not in error_payload
    assert all(canary not in error_payload for canary in provider_canaries)
    pause_event = next(
        event
        for event in jobs.subscribe(summary["job_id"]).replay
        if event["type"] == "inqtrix.index.paused_dependency"
    )
    pause_payload = json.dumps(pause_event)
    assert document_canary not in pause_payload
    assert all(canary not in pause_payload for canary in provider_canaries)
    active_before_resume = asyncio.run(store.get_collection(collection.id))
    assert active_before_resume.active_generation_id == collection.active_generation_id
    assert paused["generation_id"] == summary["generation_id"]
    completed_before_resume = paused["checkpoint"]["contextualization"][
        "completed_batches"
    ]
    # Batches execute with bounded parallelism, so another in-flight batch may
    # finish after the first timeout is observed. Every successfully persisted
    # batch, not an assumed sequential prefix length, is the resume contract.
    assert (
        1
        <= completed_before_resume
        < paused["checkpoint"]["contextualization"]["total_batches"]
    )
    assert "contexts" not in json.dumps(paused["checkpoint"])
    first_prompt = llm.prompts[0]
    planned_batches = paused["checkpoint"]["contextualization"]["total_batches"]

    jobs.resume(summary["job_id"])
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "completed")
    resumed = jobs.get(summary["job_id"])
    active_after_resume = asyncio.run(store.get_collection(collection.id))
    assert resumed["generation_id"] == paused["generation_id"]
    assert active_after_resume.active_generation_id == paused["generation_id"]
    # Every successful planned batch ran once; only the timed-out provider
    # attempt is additional. In particular, batch 1 was not repeated.
    assert len(llm.prompts) == planned_batches + 1
    assert llm.prompts.count(first_prompt) == 1


def test_programming_contextualization_failure_is_redacted_in_terminal_event() -> (
    None
):
    document_canary = "PRIVATE-FAILED-DOCUMENT-TITLE-CANARY"
    provider_canaries = (
        "PRIVATE-FAILED-PROVIDER-BODY-CANARY",
        "https://provider.invalid/private-failed-canary",
        "request-id-private-failed-canary",
    )
    store = MemoryKnowledgeStore()
    embeddings = StubEmbeddings()
    raw_service = make_service(
        make_knowledge_context(store=store, embeddings=embeddings)
    )
    collection = asyncio.run(raw_service.create_collection(name="Failure privacy"))
    asyncio.run(
        raw_service.add_document(
            collection_id=collection.id,
            title=document_canary,
            text="Canonical source text.",
        )
    )

    class BrokenLLM:
        def complete_with_metadata(self, *_args, **_kwargs):
            raise RuntimeError(" ".join(provider_canaries))

    service = make_service(
        make_knowledge_context(
            store=store,
            embeddings=embeddings,
            contextualizer=LLMChunkContextualizer(BrokenLLM()),
        )
    )
    jobs = _store()
    summary = IndexingService(
        knowledge_service=service,
        job_store=jobs,
    ).submit(collection=collection)
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "failed")

    failed = jobs.get(summary["job_id"])
    assert failed["error"]["type"] == "server_error"
    error_payload = json.dumps(failed["error"])
    assert document_canary not in error_payload
    assert all(canary not in error_payload for canary in provider_canaries)
    failed_event = next(
        event
        for event in jobs.subscribe(summary["job_id"]).replay
        if event["type"] == "inqtrix.index.failed"
    )
    event_payload = json.dumps(failed_event)
    assert document_canary not in event_payload
    assert all(canary not in event_payload for canary in provider_canaries)


def test_paused_job_keeps_the_collection_generation_slot() -> None:
    jobs = _store()

    def pause(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        handle.pause_dependency("provider unavailable")

    first = _submit(jobs, collection_id="kc_one", work=pause)
    _wait_until(lambda: jobs.get(first["job_id"])["status"] == "paused_dependency")
    with pytest.raises(IndexingJobConflict):
        _submit(jobs, collection_id="kc_one")

    cancelled = jobs.cancel(first["job_id"])
    assert cancelled["status"] == "cancelled"
    replacement = _submit(jobs, collection_id="kc_one")
    _wait_until(lambda: jobs.get(replacement["job_id"])["status"] == "completed")


@pytest.mark.parametrize("raises", [False, True], ids=["work_pauses", "work_raises"])
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        ("paused_dependency", "contextualization_provider_unavailable"),
        ("paused_validation", "validation_error"),
    ],
    ids=["dependency", "validation"],
)
def test_every_landed_pause_logs_exactly_one_warning(
    caplog, raises, status, error_type
) -> None:
    # The service's work pauses its own handle; other work raises and the
    # store pauses it. Either way the pause is logged once, by the handle.
    jobs = _store()

    def pause(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        if status == "paused_dependency":
            if raises:
                raise ContextualizationDependencyError(error_type=error_type)
            handle.pause_dependency("provider unavailable", error_type=error_type)
            return
        if raises:
            raise ContextualizationValidationError("batch 2 returned no context")
        handle.pause_validation("batch 2 returned no context")

    def pause_records() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if "paused" in record.getMessage()
        ]

    with caplog.at_level("WARNING", logger="inqtrix"):
        summary = _submit(jobs, collection_id="kc_one", work=pause)
        _wait_until(lambda: bool(pause_records()))

    assert pause_records() == [
        f"Indexing job {summary['job_id']} paused (status={status}, "
        f"error_type={error_type})"
    ]


def test_explicit_raw_resume_keeps_generation_and_reports_distinct_terminal_state() -> (
    None
):
    jobs = _store()

    def pause_then_rebuild_raw(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        if not handle.raw_by_user_choice:
            handle.pause_dependency("provider unavailable")
            return
        handle.progress(completed_documents=1)
        handle.complete_raw_by_user_choice()

    summary = _submit(
        jobs,
        collection_id="kc_raw",
        generation_id="gen_raw",
        work=pause_then_rebuild_raw,
    )
    _wait_until(lambda: jobs.get(summary["job_id"])["status"] == "paused_dependency")

    resumed = jobs.resume_raw_by_user_choice(summary["job_id"])
    assert resumed["generation_id"] == "gen_raw"
    assert jobs.raw_by_user_choice(summary["job_id"]) is True
    _wait_until(
        lambda: jobs.get(summary["job_id"])["status"] == "ready_raw_by_user_choice"
    )

    final = jobs.get(summary["job_id"])
    assert final["generation_id"] == "gen_raw"
    event_types = [event["type"] for event in jobs.subscribe(summary["job_id"]).replay]
    assert "inqtrix.index.raw_rebuild_requested" in event_types
    assert event_types[-1] == "inqtrix.index.ready_raw_by_user_choice"


def test_submit_raises_when_store_lacks_reembed() -> None:
    class NoReembedStore(MemoryKnowledgeStore):
        reembed_document = None  # type: ignore[assignment]

    context = KnowledgeProviderContext(
        embeddings=StubEmbeddings(), store=NoReembedStore()
    )
    service = make_service(context)
    collection = asyncio.run(service.create_collection(name="C"))
    indexing = IndexingService(knowledge_service=service, job_store=_store())
    with pytest.raises(ReindexUnsupported):
        indexing.submit(collection=collection)


def test_submit_raises_when_store_cannot_serialize_reindex() -> None:
    class UnsafeStore(MemoryKnowledgeStore):
        @property
        def supports_safe_reindex(self) -> bool:
            return False

    context = KnowledgeProviderContext(embeddings=StubEmbeddings(), store=UnsafeStore())
    service = make_service(context)
    collection = asyncio.run(service.create_collection(name="C"))
    indexing = IndexingService(knowledge_service=service, job_store=_store())

    with pytest.raises(ReindexUnsupported, match="cannot safely serialize"):
        indexing.submit(collection=collection)


@pytest.mark.parametrize("operation_kind", ["generation", "document_revision"])
def test_a_batch_the_content_filter_blocks_completes_and_counts_the_omission(
    operation_kind: str,
) -> None:
    # The provider's content filter blocking a batch twice no longer pauses
    # the job: it completes, and the document's completion event says how
    # many chunks went without context.
    class AlwaysFilteredLLM:
        def complete_with_metadata(self, prompt: str, **_kwargs) -> LLMResponse:
            return LLMResponse(
                content="",
                prompt_tokens=10,
                completion_tokens=0,
                model="stub-ctx",
                finish_reason="content_filter",
            )

    principal = Principal(user_id=OWNER_USER_ID, kind="oidc_session")
    owner = UserContext(principal=principal)
    store = MemoryKnowledgeStore()
    raw_service = make_service(make_knowledge_context(store=store))
    collection = asyncio.run(
        raw_service.create_collection(name="Filtered", created_by_user_id=OWNER_USER_ID)
    )
    jobs = _store()
    indexing = IndexingService(
        knowledge_service=make_service(
            make_knowledge_context(
                store=store,
                contextualizer=LLMChunkContextualizer(AlwaysFilteredLLM()),
            )
        ),
        job_store=jobs,
    )
    if operation_kind == "generation":
        asyncio.run(
            raw_service.add_document(
                collection_id=collection.id,
                title="Source",
                text="blocked body",
                visible_to=owner,
            )
        )
        summary = indexing.submit(collection=collection, principal=principal)
    else:
        summary = asyncio.run(
            indexing.submit_document_revision(
                collection=collection,
                title="Source",
                text="blocked body",
                metadata={"source_id": "document:filtered"},
                principal=principal,
                visible_to=owner,
            )
        )

    _wait_until(
        lambda: jobs.get(summary["job_id"])["status"]
        in {"completed", "failed", "paused_dependency", "paused_validation"}
    )
    assert jobs.get(summary["job_id"])["status"] == "completed"
    completed = [
        event
        for event in jobs.subscribe(summary["job_id"]).replay
        if event["type"] == "inqtrix.index.document_completed"
    ]
    assert [event["data"]["context_omitted_chunks"] for event in completed] == [1]
