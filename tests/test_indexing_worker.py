"""Offline tests for the reindex worker's fenced job handle.

The generic claim/ack/fence/reclaim machinery is exercised by
``test_worker_loop.py`` (the run loop inherits the same
:class:`~inqtrix.worker.loop.BaseWorkerLoop`); this suite pins the
reindex-specific piece — that the fenced handle threads its claim
attempt into every store write and records whether the terminal write
actually landed (the ack gate).
"""

from __future__ import annotations

import threading
import uuid

import pytest

from inqtrix.runs.indexing_postgres import ClaimedIndexingJob
from inqtrix.runs.indexing_queue import QueuedIndexingJob
from inqtrix.worker.indexing_loop import (
    FencedIndexingJobHandle,
    IndexingWorkerLoop,
)


class StubStore:
    """Recording stand-in for the durable store's handle surface."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.terminal_lands = True
        self.worker_id = "worker-test"

    def set_total(self, job_id, total_documents, *, fence_attempt=None):
        self.calls.append(("set_total", total_documents, fence_attempt))

    def progress(
        self,
        job_id,
        *,
        completed_documents,
        current_document_title="",
        fence_attempt=None,
    ):
        self.calls.append(("progress", completed_documents, fence_attempt))

    def complete(self, job_id, *, fence_attempt=None):
        self.calls.append(("complete", fence_attempt))
        return self.terminal_lands

    def fail(self, job_id, message, *, error_type="server_error", fence_attempt=None):
        self.calls.append(("fail", message, error_type, fence_attempt))
        return self.terminal_lands

    def mark_cancelled(self, job_id, *, reason, fence_attempt=None):
        self.calls.append(("cancelled", reason, fence_attempt))
        return self.terminal_lands

    def pause(self, job_id, status, message, *, error_type, fence_attempt=None):
        self.calls.append(("pause", status.value, fence_attempt))
        return self.terminal_lands

    def document_completed(
        self, job_id, document_id, *, fence_attempt=None, context_omitted_chunks=0
    ):
        self.calls.append(("document_completed", document_id, fence_attempt))
        if context_omitted_chunks:
            self.calls.append(
                ("context_omitted_chunks", document_id, context_omitted_chunks)
            )

    def document_started(self, job_id, document_id, *, fence_attempt=None):
        self.calls.append(("document_started", document_id, fence_attempt))

    def document_progress(
        self,
        job_id,
        document_id,
        phase,
        *,
        current_batch=0,
        total_batches=0,
        fence_attempt=None,
    ):
        self.calls.append(
            (
                "document_progress",
                document_id,
                phase,
                current_batch,
                total_batches,
                fence_attempt,
            )
        )

    def checkpoint_document(
        self,
        job_id,
        document_id,
        *,
        embedding_receipt=None,
        fence_attempt=None,
    ):
        self.calls.append(
            (
                "checkpoint_document",
                document_id,
                embedding_receipt,
                fence_attempt,
            )
        )


class StubQueue:
    """Recording queue surface used by direct worker-execution tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ack(self, message_id: str) -> None:
        self.calls.append(("ack", message_id))


def make_loop(
    *,
    store: StubStore,
    queue: StubQueue,
    authority=None,
) -> IndexingWorkerLoop:
    """Build a reindex loop without starting its claim machinery."""
    return IndexingWorkerLoop(
        store=store,
        queue=queue,
        knowledge_service=object(),
        concurrency=1,
        max_attempts=3,
        heartbeat_seconds=1.0,
        claim_idle_seconds=5.0,
        authority=authority,
    )


def queued_job() -> QueuedIndexingJob:
    """Return one deterministic dispatch record."""
    return QueuedIndexingJob(
        message_id="1-0",
        job_id="ix_1",
        tenant_id="default",
        delivery_count=1,
    )


def test_fenced_handle_threads_the_attempt_into_every_write():
    store = StubStore()
    handle = FencedIndexingJobHandle(store, "ix_1", threading.Event(), attempt=7)

    handle.begin(5)
    handle.progress(completed_documents=2, current_document_title="doc")
    handle.document_started("kd_1")
    handle.document_progress(
        "kd_1",
        "contextualization",
        current_batch=3,
        total_batches=8,
    )
    handle.document_completed("kd_1")
    receipt = {"adjustment_id": "knowledge-generation:one"}
    handle.checkpoint_document("kd_1", embedding_receipt=receipt)
    handle.complete()
    handle.fail("kaputt")
    handle.cancel("client_requested_cancel")

    assert ("set_total", 5, 7) in store.calls
    assert ("progress", 2, 7) in store.calls
    assert ("document_started", "kd_1", 7) in store.calls
    assert (
        "document_progress",
        "kd_1",
        "contextualization",
        3,
        8,
        7,
    ) in store.calls
    assert ("document_completed", "kd_1", 7) in store.calls
    assert ("checkpoint_document", "kd_1", receipt, 7) in store.calls
    assert ("complete", 7) in store.calls
    assert any(c[0] == "fail" and c[3] == 7 for c in store.calls)
    assert ("cancelled", "client_requested_cancel", 7) in store.calls
    assert handle.terminal_landed is True


def test_fenced_handle_carries_the_omitted_chunk_count_to_the_store():
    store = StubStore()
    handle = FencedIndexingJobHandle(store, "ix_1", threading.Event(), attempt=3)

    handle.document_completed("kd_1", context_omitted_chunks=5)

    assert ("document_completed", "kd_1", 3) in store.calls
    assert ("context_omitted_chunks", "kd_1", 5) in store.calls


@pytest.mark.parametrize("lands", [True, False], ids=["landed", "fenced_out"])
def test_fenced_handle_leaves_the_pause_log_to_its_store(caplog, lands):
    # The store knows whether a pause landed and logs it once, before the
    # resume plan it makes; a second line from the handle would double it.
    store = StubStore()
    store.terminal_lands = lands
    handle = FencedIndexingJobHandle(store, "ix_1", threading.Event(), attempt=4)

    with caplog.at_level("WARNING", logger="inqtrix"):
        handle.pause_dependency(
            "provider unavailable",
            error_type="contextualization_provider_unavailable",
        )
        handle.pause_validation("batch 2 returned no context")

    assert ("pause", "paused_dependency", 4) in store.calls
    assert ("pause", "paused_validation", 4) in store.calls
    assert handle.terminal_landed is lands
    assert [record.getMessage() for record in caplog.records] == []


def test_fenced_out_terminal_write_records_no_landing():
    """A superseded attempt's terminal write returns False — the worker
    must then NOT ack, so terminal_landed must reflect that."""
    store = StubStore()
    store.terminal_lands = False
    handle = FencedIndexingJobHandle(store, "ix_1", threading.Event(), attempt=2)

    handle.complete()

    assert handle.terminal_landed is False


def test_indexing_worker_uses_reconcile_cadence_for_generation_retention() -> None:
    class KnowledgeMaintenance:
        def __init__(self) -> None:
            self.calls = 0

        async def prune_expired_generations_all(self):
            self.calls += 1
            return {"collections": 0, "chunks": 0}

    knowledge = KnowledgeMaintenance()
    loop = IndexingWorkerLoop(
        store=StubStore(),
        queue=StubQueue(),
        knowledge_service=knowledge,
        concurrency=1,
        max_attempts=3,
        heartbeat_seconds=1.0,
        claim_idle_seconds=5.0,
    )
    try:
        loop._periodic_maintenance()
    finally:
        loop._executor.shutdown(wait=True)

    assert knowledge.calls == 1


def test_ownerless_worker_job_executes_with_an_explicit_null_actor(
    monkeypatch,
) -> None:
    store = StubStore()
    queue = StubQueue()
    observed: dict[str, object] = {}

    class RecordingAuthority:
        def check(self, collection_id, principal) -> None:
            observed["authority"] = (collection_id, principal)

    def execute(handle, **kwargs) -> None:
        observed["actor_user_id"] = kwargs["actor_user_id"]
        observed["quota_subject"] = kwargs["quota_subject"]
        kwargs["authority_check"]()
        handle.complete()

    monkeypatch.setattr(
        "inqtrix.worker.indexing_loop.execute_indexing_operation",
        execute,
    )
    loop = make_loop(
        store=store,
        queue=queue,
        authority=RecordingAuthority(),
    )
    try:
        loop._execute(
            queued_job(),
            ClaimedIndexingJob(
                job_id="ix_1",
                tenant_id="default",
                attempt=1,
                collection_id="kc_1",
                embedding_model="embedding-model",
            ),
            threading.Event(),
        )
    finally:
        loop._executor.shutdown(wait=True)

    assert observed == {
        "actor_user_id": None,
        "quota_subject": None,
        "authority": ("kc_1", None),
    }
    assert queue.calls == [("ack", "1-0")]


@pytest.mark.parametrize(
    ("created_by_user_id", "created_by_tenant_id"),
    [
        (uuid.UUID("11111111-1111-4111-8111-111111111111"), None),
        (None, "default"),
        (uuid.UUID("11111111-1111-4111-8111-111111111111"), ""),
    ],
)
def test_worker_fails_closed_on_incomplete_requester_attribution(
    monkeypatch,
    created_by_user_id,
    created_by_tenant_id,
) -> None:
    store = StubStore()
    queue = StubQueue()
    executed = False

    def execute(_handle, **_kwargs) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(
        "inqtrix.worker.indexing_loop.execute_indexing_operation",
        execute,
    )
    loop = make_loop(store=store, queue=queue)
    try:
        loop._execute(
            queued_job(),
            ClaimedIndexingJob(
                job_id="ix_1",
                tenant_id="default",
                attempt=4,
                collection_id="kc_1",
                embedding_model="embedding-model",
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
            ),
            threading.Event(),
        )
    finally:
        loop._executor.shutdown(wait=True)

    assert executed is False
    assert any(
        call[0] == "fail" and call[2] == "authorization_revoked" and call[3] == 4
        for call in store.calls
    )
    assert queue.calls == [("ack", "1-0")]
