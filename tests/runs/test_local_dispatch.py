"""Executing a job in this process, without a queue: slots, claims, cancels, closing."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from inqtrix import aio
from inqtrix.runs import durable_store
from inqtrix.runs.durable_store import DurableJobStoreBase, _LocalJob
from inqtrix.storage.db import build_engine

_HANG_SECONDS = 10.0
_CALLER: ContextVar[str] = ContextVar("test_local_dispatch_caller", default="nobody")


class _Store(DurableJobStoreBase):
    """The shape of a store that executes locally; a test decides what a claim does."""

    _job_kind = "Test job"

    def __init__(self, *, max_concurrent: int = 1, claim: Callable[[str], Any] | None = None):
        super().__init__(
            engine=build_engine(
                "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none", null_pool=True
            ),
            app_role="",
            worker_id="test",
            queue=None,
            recover_orphans=False,
        )
        self._max_concurrent = max_concurrent
        self._claim = claim
        self.claims: list[str] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.dispose_after_claims: int | None = None

    async def aenqueue(self, entity_id: str, work: Any) -> None:
        with self._lock:
            self._local[entity_id] = _LocalJob(work=work)
            self._pending.append(entity_id)
        await self._dispatch_now()

    async def _claim_db(self, entity_id: str, tenant_id: str, *, allow_takeover: bool) -> Any:
        self.claims.append(entity_id)
        outcome = self._claim(entity_id) if self._claim is not None else SimpleNamespace()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome

    def _make_handle(self, entity_id: str, cancel_event: threading.Event) -> Any:
        return SimpleNamespace(entity_id=entity_id, cancel_event=cancel_event, parked=False)

    def _auto_complete(self, entity_id: str) -> None:
        self.completed.append(entity_id)

    def fail(self, entity_id: str, message: str, *, error_type: str = "server_error") -> None:
        self.failed.append((entity_id, message))


@pytest_asyncio.fixture
async def store_factory():
    stores: list[_Store] = []

    def build(**kwargs: Any) -> _Store:
        store = _Store(**kwargs)
        stores.append(store)
        return store

    yield build
    for store in stores:
        await store.aclose()


@pytest.fixture
def thread_store_factory():
    """For a test on a thread: the process loop serves it, so the sync names work here."""
    stores: list[_Store] = []

    def build(**kwargs: Any) -> _Store:
        store = _Store(**kwargs)
        stores.append(store)
        return store

    yield build
    for store in stores:
        store.close()


async def _until(condition: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + _HANG_SECONDS
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "it never happened"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_the_body_runs_on_the_transition_lane_with_a_context_of_its_own(store_factory) -> None:
    """The request that happens to dispatch a job lends it neither log context nor span."""
    store = store_factory()
    seen: dict[str, str] = {}

    def work(handle: Any) -> None:
        seen["thread"] = threading.current_thread().name
        seen["caller"] = _CALLER.get()

    _CALLER.set("the request")
    await store.aenqueue("a", work)
    await _until(lambda: store.completed == ["a"])

    assert seen["thread"].startswith("inqtrix-legacy_segment")
    assert seen["caller"] == "nobody"


@pytest.mark.asyncio
async def test_the_ceiling_holds_while_a_claim_waits(store_factory) -> None:
    claim_may_end = threading.Event()
    body_may_end = threading.Event()

    async def slow_claim() -> Any:
        while not claim_may_end.is_set():
            await asyncio.sleep(0.005)
        return SimpleNamespace()

    store = store_factory(max_concurrent=1, claim=lambda entity_id: slow_claim())
    bodies: list[str] = []

    def work(handle: Any) -> None:
        bodies.append(handle.entity_id)
        assert body_may_end.wait(_HANG_SECONDS)

    first = asyncio.create_task(store.aenqueue("a", work))
    await _until(lambda: store.claims == ["a"])
    second = asyncio.create_task(store.aenqueue("b", work))
    await asyncio.sleep(0.05)
    assert store.claims == ["a"], "the second claim started while the only slot was reserved"

    claim_may_end.set()
    await _until(lambda: bodies == ["a"])
    await asyncio.sleep(0.05)
    assert store.claims == ["a"], "the second claim started while the only slot was running"

    body_may_end.set()
    await _until(lambda: store.completed == ["a", "b"])
    await asyncio.gather(first, second)
    assert store._running_count == 0


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_job_running(store_factory) -> None:
    """One client's disconnect must not strand a job, least of all another's."""
    claim_may_end = threading.Event()

    async def slow_claim() -> Any:
        while not claim_may_end.is_set():
            await asyncio.sleep(0.005)
        return SimpleNamespace()

    store = store_factory(claim=lambda entity_id: slow_claim())
    caller = asyncio.create_task(store.aenqueue("a", lambda handle: None))
    await _until(lambda: store.claims == ["a"])

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    claim_may_end.set()

    await _until(lambda: store.completed == ["a"])
    await _until(lambda: store._running_count == 0)
    assert "a" not in store._local


@pytest.mark.asyncio
async def test_a_failed_claim_frees_its_slot_and_the_next_job_runs(store_factory) -> None:
    def claim(entity_id: str) -> Any:
        if entity_id == "broken":
            raise RuntimeError("database down")
        return SimpleNamespace()

    store = store_factory(claim=claim)
    await store.aenqueue("broken", lambda handle: None)
    await store.aenqueue("a", lambda handle: None)

    await _until(lambda: store.completed == ["a"])
    assert "broken" not in store._local
    await _until(lambda: store._running_count == 0)


@pytest.mark.asyncio
async def test_a_job_nobody_may_claim_frees_its_slot(store_factory) -> None:
    store = store_factory(claim=lambda entity_id: None if entity_id == "gone" else SimpleNamespace())
    await store.aenqueue("gone", lambda handle: None)
    await store.aenqueue("a", lambda handle: None)

    await _until(lambda: store.completed == ["a"])
    assert "gone" not in store._local
    await _until(lambda: store._running_count == 0)


@pytest.mark.asyncio
async def test_a_body_whose_terminal_write_fails_still_frees_its_slot(
    store_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(durable_store, "_TERMINAL_WRITE_ATTEMPTS", 1)
    store = store_factory()

    def raises(handle: Any) -> None:
        raise RuntimeError("the body failed")

    def terminal_write_fails(entity_id: str, message: str, *, error_type: str = "x") -> None:
        raise RuntimeError("the database is gone too")

    store.fail = terminal_write_fails  # type: ignore[method-assign]
    await store.aenqueue("a", raises)
    await _until(lambda: store._running_count == 0)
    assert "a" not in store._local

    store.fail = _Store.fail.__get__(store)  # type: ignore[method-assign]
    await store.aenqueue("b", lambda handle: None)
    await _until(lambda: store.completed == ["b"])


@pytest.mark.asyncio
async def test_a_resume_during_the_unwind_runs_the_body_once_more(store_factory) -> None:
    """The handoff comes after the body ended: two threads never run one closure."""
    store = store_factory()
    runs: list[int] = []

    def work(handle: Any) -> None:
        runs.append(threading.get_ident())
        if len(runs) == 1:
            handle.parked = True
            with store._lock:
                local = store._local[handle.entity_id]
                local.parked = True
                local.park_in_flight = True
                local.resume_requested = True

    await store.aenqueue("a", work)

    await _until(lambda: len(runs) == 2)
    await _until(lambda: store.completed == ["a"])
    await _until(lambda: store._running_count == 0)
    assert "a" not in store._local


@pytest.mark.asyncio
async def test_a_parked_job_keeps_its_work_and_its_slot_goes_free(store_factory) -> None:
    store = store_factory()

    def work(handle: Any) -> None:
        handle.parked = True
        with store._lock:
            local = store._local[handle.entity_id]
            local.parked = True
            local.park_in_flight = True

    await store.aenqueue("a", work)
    await _until(lambda: store._running_count == 0)

    local = store._local["a"]
    assert local.work is work
    assert local.park_in_flight is False
    assert store.completed == []


@pytest.mark.asyncio
async def test_the_dispatcher_lends_no_request_context(store_factory) -> None:
    """Its log lines would name the request and the user of whoever happened to start it."""
    seen: list[str] = []

    def claim(entity_id: str) -> Any:
        del entity_id
        seen.append(_CALLER.get())
        return SimpleNamespace()

    store = store_factory(claim=claim)
    _CALLER.set("the request")

    await store.aenqueue("a", lambda handle: None)
    await _until(lambda: store.completed == ["a"])

    assert seen == ["nobody"]


@pytest.mark.asyncio
async def test_two_closers_share_one_close(store_factory) -> None:
    store = store_factory()
    closes: list[int] = []

    async def count_the_close() -> None:
        closes.append(1)

    store._close_queue_client = count_the_close

    await asyncio.gather(store.aclose(), store.aclose())

    assert closes == [1]
    assert store._closed


@pytest.mark.asyncio
async def test_a_cancelled_closer_leaves_the_close_running(store_factory) -> None:
    store = store_factory()
    close_may_end = asyncio.Event()

    async def slow_close() -> None:
        await close_may_end.wait()

    store._close_queue_client = slow_close

    first = asyncio.create_task(store.aclose())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not store._closed

    close_may_end.set()
    await store.aclose()
    assert store._closed


def test_closing_waits_for_a_claim_in_flight_and_for_its_body(thread_store_factory) -> None:
    """Disposing the engine under a claim or a body would strand its durable write."""
    claim_entered = threading.Event()
    claim_may_end = threading.Event()
    body_entered = threading.Event()
    body_may_end = threading.Event()
    body_saw_the_cancel: list[bool] = []

    async def slow_claim() -> Any:
        claim_entered.set()
        while not claim_may_end.is_set():
            await asyncio.sleep(0.005)
        return SimpleNamespace()

    def work(handle: Any) -> None:
        body_saw_the_cancel.append(handle.cancel_event.is_set())
        body_entered.set()
        assert body_may_end.wait(_HANG_SECONDS)

    store = thread_store_factory(claim=lambda entity_id: slow_claim())
    caller = threading.Thread(target=lambda: store._call(store.aenqueue("a", work)), daemon=True)
    caller.start()
    assert claim_entered.wait(_HANG_SECONDS)

    closer = threading.Thread(target=store.close, daemon=True)
    closer.start()
    closer.join(0.1)
    assert closer.is_alive(), "closing did not wait for the claim in flight"

    claim_may_end.set()
    assert body_entered.wait(_HANG_SECONDS)
    closer.join(0.1)
    assert closer.is_alive(), "closing did not wait for the body"

    body_may_end.set()
    closer.join(_HANG_SECONDS)
    assert not closer.is_alive()
    assert body_saw_the_cancel == [True]
    assert store.completed == ["a"]


@pytest.mark.refuses_the_bridge_on_purpose
def test_the_sync_name_refuses_on_the_loop_and_in_the_pool(thread_store_factory) -> None:
    store = thread_store_factory()

    async def nothing() -> None:
        return None

    async def on_the_loop() -> None:
        with pytest.raises(aio.CalledOnLoopThread):
            store._call(nothing())
        with pytest.raises(aio.CalledFromBlockingPool):
            await aio.run_blocking(store._call, nothing())

    from tests._process_loop import run_on_configured_loop

    run_on_configured_loop(on_the_loop())


def test_the_store_lock_never_stays_held_across_the_bridge(thread_store_factory) -> None:
    """The loop takes that lock for a moment, so a thread holding it would wait for itself."""
    store = thread_store_factory()

    async def nothing() -> None:
        return None

    with store._lock, pytest.raises(RuntimeError, match="lock held across the bridge"):
        store._call(nothing())
