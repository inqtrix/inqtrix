"""Dependency pauses resume on their own: the in-memory tier end to end."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import pytest

from inqtrix.contextualization_circuit import MemoryContextualizationCircuitBreaker
from inqtrix.indexing_auto_resume import (
    SWEEP_INTERVAL_SECONDS,
    AutoResumePolicy,
    SweepReport,
)
from inqtrix.knowledge.contextualize import LLMChunkContextualizer
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.indexing import IndexingJobHandle, IndexingJobStore
from inqtrix.services.indexing_service import IndexingService
from inqtrix.settings import KnowledgeSettings, ServerSettings, Settings, StorageSettings

from tests.test_knowledge_engine import StubEmbeddings, make_knowledge_context, make_service
from tests.test_knowledge_routes import KnowledgeStubLLM, _StubSearch

NOW = 1_000.0


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _store(
    clock: Clock,
    *,
    attempts: int = 5,
    random_source: Callable[[], float] = lambda: 0.5,
) -> IndexingJobStore:
    return IndexingJobStore(
        max_concurrent=1,
        max_queue_size=5,
        completed_ttl_seconds=30,
        event_buffer_size=200,
        history_limit=10,
        auto_resume=AutoResumePolicy(max_attempts=attempts),
        clock=clock,
        random_source=random_source,
    )


def _pausing(error_type: str = "contextualization_provider_timeout"):
    """Work that pauses on the provider every time it runs."""

    def work(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        handle.pause_dependency("provider unavailable", error_type=error_type)

    return work


def _submit(jobs: IndexingJobStore, work) -> str:
    return jobs.submit(
        collection_id="kc_auto",
        collection_name="Auto",
        embedding_model="stub-embed-8",
        work=work,
    )["job_id"]


def _paused(jobs: IndexingJobStore, job_id: str, count: int) -> None:
    """Wait for the job's ``count``-th dependency pause and the end of its run.

    The memory store completes a job resumed between a run's pause and the
    end of that run instead of running it again; resuming only after the
    run ended keeps these tests off that window. One execution slot frees
    only when the run has ended, so a sentinel job that completes proves it.
    """
    _wait_until(lambda: (
        jobs.get(job_id)["status"] == "paused_dependency"
        and sum(
            1 for event in jobs.subscribe(job_id).replay
            if event["type"] == "inqtrix.index.paused_dependency"
        ) == count
    ))
    sentinel = jobs.submit(
        collection_id="kc_sentinel",
        collection_name="Sentinel",
        embedding_model="stub-embed-8",
        work=lambda handle: (handle.begin(0), handle.complete()),
    )["job_id"]
    _wait_until(lambda: jobs.get(sentinel)["status"] == "completed")


def _plan(jobs: IndexingJobStore, job_id: str) -> dict | None:
    return jobs.get(job_id)["checkpoint"].get("auto_resume")


def test_a_dependency_pause_plans_a_visible_automatic_resume():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    expected = {"attempts": 0, "max": 5, "next_at": NOW + 60.0, "stopped": None}
    assert _plan(jobs, job_id) == expected
    paused = [
        event for event in jobs.subscribe(job_id).replay
        if event["type"] == "inqtrix.index.paused_dependency"
    ]
    assert paused[-1]["data"]["auto_resume"] == expected


def test_a_pause_is_logged_before_the_plan_it_makes():
    class Messages(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    clock = Clock()
    jobs = _store(clock)
    messages = Messages()
    logger = logging.getLogger("inqtrix")
    logger.addHandler(messages)
    try:
        job_id = _submit(jobs, _pausing())
        _paused(jobs, job_id, 1)
    finally:
        logger.removeHandler(messages)

    mine = [message for message in messages.messages if job_id in message]
    assert mine == [
        f"Indexing job {job_id} paused (status=paused_dependency, "
        "error_type=contextualization_provider_timeout)",
        f"Indexing job {job_id} resumes automatically at 00:17:40Z (attempt 1/5)",
    ]


def test_a_validation_pause_waits_for_a_person():
    clock = Clock()
    jobs = _store(clock)

    def work(handle: IndexingJobHandle) -> None:
        handle.begin(1)
        handle.pause_validation("batch 2 returned no context")

    job_id = _submit(jobs, work)
    _wait_until(lambda: jobs.get(job_id)["status"] == "paused_validation")

    assert _plan(jobs, job_id) is None
    assert jobs.due_auto_resumes(now=NOW + 10_000) == []


def test_the_resume_comes_when_it_is_due_and_counts_the_attempt():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    assert jobs.due_auto_resumes(now=NOW + 59) == []
    jobs.resume(job_id, automatic=True, now=NOW + 59)
    assert jobs.get(job_id)["status"] == "paused_dependency"

    [due] = jobs.due_auto_resumes(now=NOW + 60)
    assert (due.job_id, due.error_type) == (job_id, "contextualization_provider_timeout")
    jobs.resume(job_id, automatic=True, now=NOW + 60)
    _paused(jobs, job_id, 2)

    assert _plan(jobs, job_id) == {
        "attempts": 1, "max": 5, "next_at": NOW + 120.0, "stopped": None,
    }
    resumed = [
        event for event in jobs.subscribe(job_id).replay
        if event["type"] == "inqtrix.index.resumed"
    ]
    assert resumed[-1]["data"]["automatic"] is True
    assert (resumed[-1]["data"]["attempt"], resumed[-1]["data"]["max"]) == (1, 5)


def test_five_resumes_without_progress_end_the_streak():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    for count in range(1, 6):
        _paused(jobs, job_id, count)
        jobs.resume(job_id, automatic=True, now=NOW + 10_000)
    _paused(jobs, job_id, 6)

    assert _plan(jobs, job_id) == {
        "attempts": 5, "max": 5, "next_at": None, "stopped": "exhausted",
    }
    assert jobs.due_auto_resumes(now=NOW + 100_000) == []


def test_progress_between_pauses_keeps_the_streak_at_its_start():
    clock = Clock()
    jobs = _store(clock)
    runs: list[int] = []

    def work(handle: IndexingJobHandle) -> None:
        runs.append(len(runs) + 1)
        handle.begin(10)
        handle.progress(completed_documents=len(runs))
        handle.pause_dependency(
            "vector store unavailable", error_type="vector_store_unavailable"
        )

    job_id = _submit(jobs, work)
    for count in range(1, 4):
        _paused(jobs, job_id, count)
        assert _plan(jobs, job_id)["attempts"] == 0
        jobs.resume(job_id, automatic=True, now=NOW + 10_000)
    _paused(jobs, job_id, 4)

    assert _plan(jobs, job_id) == {
        "attempts": 0, "max": 5, "next_at": NOW + 60.0, "stopped": None,
    }


def test_a_person_resuming_starts_the_count_again():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)
    jobs.resume(job_id, automatic=True, now=NOW + 60)
    _paused(jobs, job_id, 2)

    jobs.resume(job_id)
    _paused(jobs, job_id, 3)

    assert _plan(jobs, job_id)["attempts"] == 0


def test_a_streak_the_sweep_ends_is_named_and_due_no_more():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    jobs.stop_auto_resume(job_id, "access")

    assert _plan(jobs, job_id)["stopped"] == "access"
    assert jobs.due_auto_resumes(now=NOW + 10_000) == []
    stopped = [
        event for event in jobs.subscribe(job_id).replay
        if event["type"] == "inqtrix.index.auto_resume_stopped"
    ]
    assert stopped[-1]["data"] == {"reason": "access", "attempts": 0}


def test_a_cancelled_job_is_never_resumed():
    clock = Clock()
    jobs = _store(clock)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    jobs.cancel(job_id)
    jobs.resume(job_id, automatic=True, now=NOW + 10_000)

    assert jobs.get(job_id)["status"] == "cancelled"
    assert jobs.due_auto_resumes(now=NOW + 10_000) == []


def test_without_a_policy_pauses_wait_for_a_person():
    clock = Clock()
    jobs = _store(clock, attempts=0)
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    assert _plan(jobs, job_id) is None
    assert jobs.due_auto_resumes(now=NOW + 10_000) == []


class _TimeoutOnceLLM:
    """Times out on its first contextualization call, answers every later one."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_with_metadata(self, prompt: str, **_kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("provider timeout")
        return LLMResponse(
            content=json.dumps([f"Kontext {n}" for n in range(prompt.count("CHUNK "))]),
            prompt_tokens=10,
            completion_tokens=5,
            model="stub-ctx",
            finish_reason="stop",
        )


def _indexing(
    clock: Clock,
    *,
    breaker: MemoryContextualizationCircuitBreaker | None = None,
    random_source: Callable[[], float] = lambda: 0.5,
) -> tuple[IndexingService, IndexingJobStore, str]:
    """A collection whose contextualized rebuild pauses once on a provider timeout."""
    store = MemoryKnowledgeStore()
    embeddings = StubEmbeddings()
    raw = make_service(make_knowledge_context(store=store, embeddings=embeddings))
    collection = asyncio.run(raw.create_collection(name="Auto"))
    asyncio.run(
        raw.add_document(
            collection_id=collection.id, title="Bericht", text="Abschnitt. " + "x" * 1_200
        )
    )
    contextualizer = LLMChunkContextualizer(_TimeoutOnceLLM(), provider_key="stub")
    if breaker is not None:
        contextualizer.bind_circuit_breaker(breaker)
    jobs = _store(clock, random_source=random_source)
    indexing = IndexingService(
        knowledge_service=make_service(
            make_knowledge_context(
                store=store, embeddings=embeddings, contextualizer=contextualizer
            )
        ),
        job_store=jobs,
    )
    return indexing, jobs, indexing.submit(collection=collection)["job_id"]


def test_the_sweep_continues_a_due_pause_as_its_creator_until_it_completes():
    clock = Clock()
    indexing, jobs, job_id = _indexing(clock)
    _paused(jobs, job_id, 1)

    assert indexing.resume_due_pauses(now=NOW + 59) == SweepReport()
    assert indexing.resume_due_pauses(now=NOW + 60) == SweepReport(resumed=1)
    _wait_until(lambda: jobs.get(job_id)["status"] == "completed")

    [resumed] = [
        event for event in jobs.subscribe(job_id).replay
        if event["type"] == "inqtrix.index.resumed"
    ]
    assert resumed["data"]["automatic"] is True
    assert (resumed["data"]["attempt"], resumed["data"]["max"]) == (1, 5)


def test_the_sweep_waits_for_the_circuit_the_failed_call_opened():
    clock = Clock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    # Jitter at its low end: the plan is due after 48 s, the circuit cools for 60 s.
    indexing, jobs, job_id = _indexing(clock, breaker=breaker, random_source=lambda: 0.0)
    _paused(jobs, job_id, 1)
    assert _plan(jobs, job_id)["next_at"] == pytest.approx(NOW + 48.0)

    assert indexing.resume_due_pauses(now=NOW + 50) == SweepReport(deferred=1)
    assert jobs.get(job_id)["status"] == "paused_dependency"

    clock.now = NOW + 60
    assert indexing.resume_due_pauses(now=NOW + 60) == SweepReport(resumed=1)
    _wait_until(lambda: jobs.get(job_id)["status"] == "completed")


def test_a_pause_the_service_cannot_rebuild_ends_its_streak_visibly():
    clock = Clock()
    jobs = _store(clock)
    indexing = IndexingService(knowledge_service=make_service(), job_store=jobs)
    # Submitted without a generation: there is nothing to continue from.
    job_id = _submit(jobs, _pausing())
    _paused(jobs, job_id, 1)

    assert indexing.resume_due_pauses(now=NOW + 60) == SweepReport(stopped=1)

    assert _plan(jobs, job_id)["stopped"] == "unavailable"
    assert jobs.get(job_id)["status"] == "paused_dependency"
    assert indexing.resume_due_pauses(now=NOW + 10_000) == SweepReport()


@pytest.mark.parametrize(("attempts", "driven"), [(5, True), (0, False)])
def test_the_api_process_drives_automatic_resumes_unless_they_are_off(attempts, driven):
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=_StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
            knowledge=KnowledgeSettings(INQTRIX_REINDEX_AUTO_RESUME_ATTEMPTS=attempts),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(), store=MemoryKnowledgeStore(), default_top_k=4
        ),
    )

    assert container.indexing_service.job_store.auto_resume_policy.max_attempts == attempts
    assert (container.indexing_auto_resumer is not None) is driven
    if driven:
        # Inert until the application lifespan starts it.
        assert container.indexing_auto_resumer.running is False
        assert container.indexing_auto_resumer._interval == SWEEP_INTERVAL_SECONDS == 30.0
