"""What a job body's coroutines do on the shared loop.

A sync job body hands coroutines to the process loop. Whatever such a coroutine does
synchronously holds every request and every heartbeat of the process, and a sync store
name there refuses to run. Its callbacks, its computing and its long provider work
therefore go to a thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from inqtrix import aio
from inqtrix.knowledge import embedding_batches
from inqtrix.knowledge.stores import memory as memory_knowledge
from inqtrix.knowledge.stores import vector_index
from inqtrix.runs.indexing_memory import IndexingJobStore
from inqtrix.services import knowledge_service as knowledge_module
from inqtrix.services.indexing_service import IndexingService
from inqtrix.settings import KnowledgeSettings
from tests._process_loop import run_on_configured_loop
from tests.test_knowledge_engine import StubEmbeddings, make_knowledge_context, make_service
from tests.test_vector_index import _cv, _legacy_scope

_HANG_SECONDS = 10.0


def _on_the_loop(fn: Callable[[], Awaitable[Any]]) -> tuple[Any, int]:
    """Run a coroutine on a configured loop; its result and that loop's thread."""

    async def main() -> tuple[Any, int]:
        return await fn(), threading.get_ident()

    return run_on_configured_loop(main())


class _Contextualizer:
    def __init__(self, seen: dict[str, str]) -> None:
        self._seen = seen

    def contextualize(self, *, chunks: Any, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self._seen["contextualize"] = threading.current_thread().name
        return SimpleNamespace(
            texts=[chunk.text for chunk in chunks],
            contexts=[None] * len(chunks),
            marker=None,
            batch_count=1,
            omitted_chunks=0,
        )


def _service(**kwargs: Any) -> Any:
    return make_service(make_knowledge_context(embeddings=StubEmbeddings(), **kwargs))


def test_embedding_callbacks_leave_the_loop() -> None:
    """They write through the job's sync store names, which refuse on the loop."""
    seen: dict[str, int] = {}

    async def scenario() -> Any:
        return await embedding_batches.embed_in_slices(
            ["one", "two"],
            embed_fn=lambda texts: [[0.0] for _ in texts],
            on_batch=lambda number, total: seen.__setitem__("batch", threading.get_ident()),
        )

    _vectors, loop_thread = _on_the_loop(scenario)

    assert seen["batch"] != loop_thread


def test_a_rate_limit_wait_reports_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}
    attempts: list[int] = []

    class RateLimited(Exception):
        status_code = 429

    def embed(texts: list[str]) -> list[list[float]]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimited("slow down")
        return [[0.0] for _ in texts]

    monkeypatch.setattr(embedding_batches, "_rate_limit_wait_seconds", lambda exc: 0.0)

    async def scenario() -> Any:
        return await embedding_batches.embed_in_slices(
            ["one"],
            embed_fn=embed,
            on_wait=lambda number, total: seen.__setitem__("wait", threading.get_ident()),
        )

    _vectors, loop_thread = _on_the_loop(scenario)

    assert seen["wait"] != loop_thread


def test_chunking_and_contextualizing_leave_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large document's chunk spans re-encode megabytes; a heartbeat cannot wait for that."""
    seen: dict[str, Any] = {}
    real_chunking = knowledge_module.chunk_text_slices

    def watched_chunking(*args: Any, **kwargs: Any) -> Any:
        seen["chunking"] = threading.current_thread().name
        return real_chunking(*args, **kwargs)

    monkeypatch.setattr(knowledge_module, "chunk_text_slices", watched_chunking)
    service = _service(contextualizer=_Contextualizer(seen))

    async def scenario() -> Any:
        return await service._embed_text(
            title="T",
            text="Ein Absatz. " * 200,
            embedding_model="stub-embed-8",
            on_embedding_started=lambda: seen.__setitem__("started", threading.get_ident()),
        )

    _embedded, loop_thread = _on_the_loop(scenario)

    assert seen["chunking"].startswith("inqtrix-blocking")
    assert seen["contextualize"].startswith("inqtrix-legacy_segment")
    assert seen["started"] != loop_thread


def test_the_authority_check_of_a_coroutine_runs_where_it_may_reach_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check reaches back to the loop, which a blocking-lane thread may not do."""
    seen: list[str] = []

    async def thread_of_the_loop() -> int:
        return threading.get_ident()

    def check() -> None:
        seen.append(threading.current_thread().name)
        aio.call_from_thread(thread_of_the_loop())

    service = _service()

    async def scenario() -> Any:
        collection = await service.create_collection(name="C", created_by_user_id=None)
        return await service.reembed_document_with_receipt(
            document=await service.add_document(
                collection_id=collection.id, title="D", text="Inhalt", visible_to=None
            ),
            embedding_model=collection.embedding_model,
            authority_check=check,
        )

    _on_the_loop(scenario)

    assert seen and all(name.startswith("inqtrix-legacy_segment") for name in seen)


def test_a_job_on_its_only_slot_finishes_with_one_spare_thread() -> None:
    """Its coroutine hops off the loop for the authority check and for every callback."""

    class _Authority:
        def __init__(self, jobs: IndexingJobStore) -> None:
            self._jobs = jobs
            self.threads: list[str] = []

        def check(self, collection_id: str, principal: Any) -> None:
            del principal
            self.threads.append(threading.current_thread().name)
            assert self._jobs.has_active_job(collection_id)

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        service = _service()
        collection = await service.create_collection(name="One slot")
        jobs = IndexingJobStore(
            max_concurrent=1,
            max_queue_size=5,
            completed_ttl_seconds=30,
            event_buffer_size=50,
            history_limit=5,
        )
        authority = _Authority(jobs)
        indexing = IndexingService(knowledge_service=service, job_store=jobs, authority=authority)
        summary = await indexing.submit_document_revision(
            collection=collection,
            title="Long enough to embed",
            text=" ".join(f"Satz {number} des Dokuments." for number in range(200)),
        )
        deadline = time.monotonic() + _HANG_SECONDS
        while True:
            job = await jobs.aget(summary["job_id"])
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job, authority.threads
            assert time.monotonic() < deadline, job
            await asyncio.sleep(0.01)

    job, threads = run_on_configured_loop(scenario(), legacy_segment_workers=2)

    assert job["status"] == "completed", job
    assert threads
    assert all(name.startswith("inqtrix-legacy_segment") for name in threads)


def test_an_exact_search_ranks_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cosine in pure Python over every chunk would hold every request of the process."""
    seen: list[str] = []
    real_cosine = memory_knowledge._cosine

    def watched(a: Any, b: Any) -> float:
        seen.append(threading.current_thread().name)
        return real_cosine(a, b)

    monkeypatch.setattr(memory_knowledge, "_cosine", watched)
    service = _service()

    async def scenario() -> Any:
        collection = await service.create_collection(name="Suche")
        await service.add_document(
            collection_id=collection.id, title="T", text="Ein Absatz. " * 50
        )
        return await service.knowledge.store.search(
            query_embedding=[1.0] * StubEmbeddings.DIM,
            collection_ids=[collection.id],
            top_k=3,
        )

    assert run_on_configured_loop(scenario())
    assert seen
    assert all(name.startswith("inqtrix-blocking") for name in seen)


def test_a_memory_vector_search_ranks_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real_cosine = vector_index._cosine

    def watched(a: Any, b: Any) -> float:
        seen.append(threading.current_thread().name)
        return real_cosine(a, b)

    monkeypatch.setattr(vector_index, "_cosine", watched)
    index = vector_index.MemoryVectorIndex()

    async def scenario() -> Any:
        await index.upsert(
            embedding_model="m",
            collection_id="c1",
            document_id="d1",
            vectors=[_cv("a", [1.0, 0.0]), _cv("b", [0.0, 1.0])],
        )
        return await index.search(
            embedding_model="m",
            query_embedding=[1.0, 0.0],
            scopes=[_legacy_scope("c1", "d1")],
            top_k=2,
        )

    assert [hit.chunk_id for hit in run_on_configured_loop(scenario())] == ["a", "b"]
    assert all(name.startswith("inqtrix-blocking") for name in seen)


def test_mapping_chunks_to_pages_leaves_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long PDF intersects every chunk with every page."""
    seen: dict[str, str] = {}
    real_mapping = knowledge_module.infer_chunk_pages

    def watched(*args: Any, **kwargs: Any) -> Any:
        seen["thread"] = threading.current_thread().name
        return real_mapping(*args, **kwargs)

    monkeypatch.setattr(knowledge_module, "infer_chunk_pages", watched)
    service = _service()

    async def scenario() -> None:
        collection = await service.create_collection(name="Seiten")
        await service.reserve_document_revision(
            collection_id=collection.id,
            title="T",
            text="Seite eins. Seite zwei.",
            page_texts=["Seite eins.", "Seite zwei."],
        )

    run_on_configured_loop(scenario())

    assert seen["thread"].startswith("inqtrix-blocking")


def _running_job(
    store: IndexingJobStore, cleanup: Callable[[], Awaitable[None]], release: threading.Event
) -> str:
    started = threading.Event()

    def work(handle: Any) -> None:
        started.set()
        assert release.wait(_HANG_SECONDS)

    summary = store.submit(
        collection_id="c",
        collection_name="C",
        embedding_model="m",
        work=work,
        cleanup=cleanup,
        generation_id=f"gen_{uuid.uuid4().hex}",
    )
    assert started.wait(_HANG_SECONDS)
    return summary["job_id"]


@pytest.mark.parametrize(
    "finish",
    [
        pytest.param(lambda store, job_id: store.fail(job_id, "kaputt"), id="fail"),
        pytest.param(lambda store, job_id: store.mark_cancelled(job_id, reason="x"), id="cancel"),
    ],
)
def test_a_memory_job_runs_its_cleanup_without_holding_the_store_lock(finish: Any) -> None:
    """The cleanup needs the loop, and the loop takes this lock for a moment."""
    store = IndexingJobStore.from_settings(KnowledgeSettings())
    held: list[bool] = []
    release = threading.Event()

    async def cleanup() -> None:
        held.append(store._lock._is_owned())

    job_id = _running_job(store, cleanup, release)
    finish(store, job_id)
    release.set()

    assert held == [False]


def test_a_memory_cancel_of_a_queued_job_runs_its_cleanup_without_the_lock() -> None:
    store = IndexingJobStore.from_settings(KnowledgeSettings())
    held: list[bool] = []
    release = threading.Event()

    async def cleanup() -> None:
        held.append(store._lock._is_owned())

    async def no_cleanup() -> None:
        return None

    first = _running_job(store, no_cleanup, release)
    with store._lock:
        store._max_concurrent = 1
    queued = store.submit(
        collection_id="other",
        collection_name="O",
        embedding_model="m",
        work=lambda handle: None,
        cleanup=cleanup,
        generation_id="gen_queued",
    )
    store.cancel(queued["job_id"])
    release.set()
    store.mark_cancelled(first, reason="done")

    assert held == [False]
