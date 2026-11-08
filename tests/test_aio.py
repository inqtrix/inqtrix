"""The one thread bridge: what crosses it, what it refuses, and what it counts."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from statistics import quantiles
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from langchain_core.runnables.config import run_in_executor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from inqtrix import aio
from inqtrix.observability.context import bind_log_context, current_log_context
from inqtrix.settings import QueueSettings

pytestmark = pytest.mark.refuses_the_bridge_on_purpose

T = TypeVar("T")

_HANG_SECONDS = 10.0
_WATCHDOG_THREAD = "inqtrix-loop-watchdog"

_SUBJECT: ContextVar[str] = ContextVar("test_aio_subject", default="unset")


@pytest.fixture(autouse=True)
def no_process_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests build the process loop themselves, the way its owner does."""
    monkeypatch.setattr(aio, "_states", {})
    monkeypatch.setattr(aio, "_default_loop", None)
    monkeypatch.setattr(aio, "_owner_configured", True)
    monkeypatch.setattr(aio, "_library_loop", None)


@pytest.fixture
def library_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A process no owner ever gave a loop: a library, a notebook, a script."""
    monkeypatch.setattr(aio, "_owner_configured", False)
    yield
    loop = aio._library_loop
    if loop is not None:
        aio.close_loop(loop)
        loop.call_soon_threadsafe(loop.stop)
        deadline = time.monotonic() + _HANG_SECONDS
        while not loop.is_closed():
            assert time.monotonic() < deadline, "the library loop never stopped"
            time.sleep(0.005)


@contextmanager
def process_loop(
    *,
    blocking: int = 2,
    provider: int = 2,
    legacy: int = 2,
    watch: bool = False,
) -> Iterator[asyncio.AbstractEventLoop]:
    """A configured loop on its own thread, the way a process owner runs it."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-process-loop", daemon=True)
    thread.start()
    aio.configure_loop(
        loop,
        blocking_workers=blocking,
        provider_workers=provider,
        legacy_segment_workers=legacy,
        watch=watch,
    )
    try:
        yield loop
    finally:
        aio.close_loop(loop)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_HANG_SECONDS)
        if not thread.is_alive():
            loop.close()


def on_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Drive a test's coroutine without the bridge; a hang fails the test."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=_HANG_SECONDS)


def on_thread(fn: Callable[[], T]) -> T:
    """Run *fn* on a foreign daemon thread; a hang fails the test, not the run."""
    outcome: Future[T] = Future()

    def target() -> None:
        try:
            outcome.set_result(fn())
        except BaseException as exc:
            outcome.set_exception(exc)

    threading.Thread(target=target, name="test-foreign-thread", daemon=True).start()
    return outcome.result(timeout=_HANG_SECONDS)


async def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _HANG_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.005)


async def _running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


async def _nothing() -> str:
    return "ran"


def _current_thread_name() -> str:
    return threading.current_thread().name


def _messages(caplog: pytest.LogCaptureFixture, fragment: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "inqtrix.aio" and fragment in record.getMessage()
    ]


class TestOneLoop:
    def test_threads_reach_the_process_loop(self) -> None:
        with process_loop() as loop:
            assert on_thread(lambda: aio.call_from_thread(_running_loop())) is loop

    def test_three_threads_for_200_rounds_share_a_contended_loop_bound_lock(self) -> None:
        """The regression of the per-call loops: a shared primitive binds to one loop."""

        async def make_lock() -> asyncio.Lock:
            return asyncio.Lock()

        with process_loop() as loop:
            lock = on_loop(loop, make_lock())
            entered = 0

            async def access() -> None:
                nonlocal entered
                async with lock:
                    await asyncio.sleep(0)
                    entered += 1

            def one_access() -> None:
                aio.call_from_thread(access())

            with ThreadPoolExecutor(3, thread_name_prefix="test-store-caller") as callers:
                for _ in range(200):
                    calls = [callers.submit(one_access) for _ in range(3)]
                    for call in calls:
                        call.result(timeout=_HANG_SECONDS)
            assert entered == 600

    def test_an_exception_of_the_coroutine_reaches_the_caller_unchanged(self) -> None:
        class Refused(RuntimeError):
            pass

        async def refuse() -> None:
            raise Refused("no")

        with process_loop():
            with pytest.raises(Refused, match="no"):
                on_thread(lambda: aio.call_from_thread(refuse()))


class TestRefusals:
    def test_on_the_loop_thread_it_refuses_instead_of_waiting_for_itself(self) -> None:
        pending = _nothing()

        async def main() -> None:
            aio.call_from_thread(pending)

        with process_loop() as loop:
            with pytest.raises(aio.CalledOnLoopThread):
                on_loop(loop, main())
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED

    def test_on_a_blocking_lane_thread_it_refuses_instead_of_waiting_on_its_own_pool(self) -> None:
        """Pool size 1: without the refusal the pool thread would wait for its own pool."""

        async def needs_the_pool() -> str:
            return await aio.run_blocking(_current_thread_name)

        pending = needs_the_pool()

        async def main() -> None:
            await aio.run_blocking(aio.call_from_thread, pending)

        with process_loop(blocking=1) as loop:
            with pytest.raises(aio.CalledFromBlockingPool):
                on_loop(loop, main())
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED

    def test_asyncio_to_thread_counts_as_the_blocking_lane(self) -> None:
        pending = _nothing()

        async def main() -> None:
            await asyncio.to_thread(aio.call_from_thread, pending)

        with process_loop() as loop:
            with pytest.raises(aio.CalledFromBlockingPool):
                on_loop(loop, main())

    def test_without_the_owners_loop_it_refuses_and_starts_none(self) -> None:
        """A process with an owner never falls back to a loop of its own."""
        pending = _nothing()
        with pytest.raises(aio.LoopNotConfigured):
            on_thread(lambda: aio.call_from_thread(pending))
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
        assert aio._library_loop is None

    def test_with_an_owner_a_thread_running_another_loop_is_still_refused(self) -> None:
        """The strict rule of servers, workers and the test session stays."""
        pending = _nothing()

        async def other_loop() -> None:
            aio.call_from_thread(pending)

        with process_loop():
            with pytest.raises(aio.CalledOnLoopThread):
                on_thread(lambda: asyncio.run(other_loop()))
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED

    def test_an_interrupted_wait_cancels_its_coroutine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``KeyboardInterrupt`` in the waiting thread must not orphan the work it waited for."""

        class Interrupted(BaseException):
            pass

        started = threading.Event()
        cancelled = threading.Event()

        async def waits_forever() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def interrupted_wait(*_args: Any, **_kwargs: Any) -> Any:
            started.wait(_HANG_SECONDS)
            raise Interrupted

        monkeypatch.setattr(aio, "wait_for_futures", interrupted_wait)
        with process_loop():
            with pytest.raises(Interrupted):
                on_thread(lambda: aio.call_from_thread(waits_forever()))
            assert cancelled.wait(_HANG_SECONDS)


class TestLibraryLoop:
    def test_without_an_owner_the_first_need_starts_one_library_loop(
        self, library_process: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="inqtrix.aio"):
            with ThreadPoolExecutor(4, thread_name_prefix="test-library-caller") as callers:
                calls = [
                    callers.submit(aio.call_from_thread, _running_loop()) for _ in range(8)
                ]
                loops = {call.result(timeout=_HANG_SECONDS) for call in calls}
        assert loops == {aio._library_loop}
        assert aio.default_loop() is aio._library_loop
        assert len(_messages(caplog, "background loop")) == 1

    def test_once_an_owner_configured_a_loop_no_library_loop_starts(
        self, library_process: None
    ) -> None:
        """A server that closed its loop at shutdown gets no second loop from a late call."""
        with process_loop():
            pass
        pending = _nothing()
        with pytest.raises(aio.LoopNotConfigured):
            on_thread(lambda: aio.call_from_thread(pending))
        assert aio._library_loop is None

    def test_a_thread_running_another_loop_may_wait_for_the_library_loop(
        self, library_process: None
    ) -> None:
        """A notebook cell calls sync code; it blocks its own loop, as every sync call does."""

        async def notebook_cell() -> asyncio.AbstractEventLoop:
            return aio.call_from_thread(_running_loop())

        reached = on_thread(lambda: asyncio.run(notebook_cell()))
        assert reached is aio._library_loop

    def test_the_library_loop_refuses_a_wait_for_itself(self, library_process: None) -> None:
        library = on_thread(lambda: aio.call_from_thread(_running_loop()))
        pending = _nothing()

        async def main() -> None:
            aio.call_from_thread(pending)

        with pytest.raises(aio.CalledOnLoopThread):
            on_loop(library, main())
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED

    def test_a_thread_starts_a_coroutine_and_collects_it_later(self) -> None:
        with process_loop() as loop:
            started = on_thread(lambda: aio.submit_from_thread(_running_loop()))
            assert started.result(timeout=_HANG_SECONDS) is loop

    def test_a_coroutine_from_another_loop_runs_on_the_process_loop(self) -> None:
        async def elsewhere() -> asyncio.AbstractEventLoop:
            return await aio.on_process_loop(_running_loop())

        with process_loop() as loop:
            assert on_thread(lambda: asyncio.run(elsewhere())) is loop
            assert on_loop(loop, elsewhere()) is loop

    def test_a_closed_loop_refuses_instead_of_hanging(self) -> None:
        loop = asyncio.new_event_loop()
        aio.configure_loop(loop, blocking_workers=1, provider_workers=1, watch=False)
        try:
            loop.close()
            pending = _nothing()
            with pytest.raises(aio.LoopNotConfigured):
                on_thread(lambda: aio.call_from_thread(pending))
        finally:
            aio.close_loop(loop)

    def test_the_lanes_refuse_a_loop_nobody_configured(self) -> None:
        async def main() -> None:
            await aio.run_blocking(_current_thread_name)

        with pytest.raises(aio.LoopNotConfigured):
            asyncio.run(main())

    def test_a_process_without_the_transition_lane_refuses_a_segment(self) -> None:
        async def main() -> None:
            await aio.run_legacy_segment(_current_thread_name)

        with process_loop(legacy=0) as loop:
            with pytest.raises(aio.LoopNotConfigured):
                on_loop(loop, main())


    def test_a_refusal_is_in_the_log_with_its_caller(self, caplog: pytest.LogCaptureFixture) -> None:
        """Catch-alls on the way up may swallow the exception; the line stays."""

        async def main() -> None:
            with pytest.raises(aio.CalledOnLoopThread):
                aio.call_from_thread(_nothing())

        with process_loop() as loop, caplog.at_level(logging.ERROR, logger="inqtrix.aio"):
            on_loop(loop, main())
        lines = _messages(caplog, "Bridge refused")
        assert len(lines) == 1
        assert "test_aio:main:" in lines[0]

    def test_a_thread_does_not_stay_at_a_loop_that_closes_under_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A job thread that nobody can join would keep the process from exiting."""
        monkeypatch.setattr(aio, "_CLOSED_LOOP_CHECK_SECONDS", 0.01)
        loop = asyncio.new_event_loop()
        aio.configure_loop(loop, blocking_workers=1, provider_workers=1, watch=False)
        try:
            pending = _nothing()
            outcome: Future[None] = Future()

            def wait_for_the_loop() -> None:
                try:
                    aio.call_from_thread(pending)
                except BaseException as exc:
                    outcome.set_exception(exc)

            threading.Thread(target=wait_for_the_loop, daemon=True).start()
            time.sleep(0.05)
            loop.close()
            with pytest.raises(aio.LoopNotConfigured):
                outcome.result(timeout=_HANG_SECONDS)
        finally:
            aio.close_loop(loop)
            pending.close()

    def test_a_coroutine_that_raises_timeout_reaches_the_caller_as_itself(self) -> None:
        async def main() -> None:
            raise TimeoutError("from the coroutine")

        with process_loop(), pytest.raises(TimeoutError, match="from the coroutine"):
            on_thread(lambda: aio.call_from_thread(main()))


class TestStartedSegments:
    def test_it_runs_on_the_transition_lane_and_reaches_the_process_loop(self) -> None:
        async def thread_of_the_loop() -> int:
            return threading.get_ident()

        def body() -> tuple[str, int]:
            return threading.current_thread().name, aio.call_from_thread(thread_of_the_loop())

        with process_loop() as loop:
            name, loop_thread = aio.start_legacy_segment(body).result(timeout=_HANG_SECONDS)
            assert loop_thread == on_loop(loop, thread_of_the_loop())
        assert name.startswith("inqtrix-legacy_segment")

    def test_the_body_starts_from_a_fresh_context_like_a_new_thread(self) -> None:
        """The request that happens to start a job must not lend it its log context or span."""
        _SUBJECT.set("the request's")
        with process_loop():
            seen = aio.start_legacy_segment(_SUBJECT.get).result(timeout=_HANG_SECONDS)
        _SUBJECT.set("unset")
        assert seen == "unset"

    def test_what_a_body_binds_ends_with_it_on_a_reused_thread(self) -> None:
        """The lane reuses threads; the next job must not book under the last job's user."""

        def first() -> None:
            bind_log_context(run_id="run_first")
            _SUBJECT.set("the first body's")

        def second() -> tuple[dict[str, str], str]:
            return current_log_context(), _SUBJECT.get()

        with process_loop(legacy=1):
            aio.start_legacy_segment(first).result(timeout=_HANG_SECONDS)
            seen = aio.start_legacy_segment(second).result(timeout=_HANG_SECONDS)
        assert seen == ({}, "unset")

    def test_what_a_segment_binds_ends_with_it_on_a_reused_thread(self) -> None:
        def first() -> None:
            bind_log_context(run_id="run_first")
            _SUBJECT.set("the first body's")

        def second() -> tuple[dict[str, str], str]:
            return current_log_context(), _SUBJECT.get()

        async def main() -> tuple[dict[str, str], str]:
            await aio.run_legacy_segment(first)
            return await aio.run_legacy_segment(second)

        with process_loop(legacy=1) as loop:
            assert on_loop(loop, main()) == ({}, "unset")

    def test_bodies_on_every_slot_take_turns_on_the_spare_thread(self) -> None:
        """A coroutine a body waits for may hop once; what it hops to must not hop again."""
        slots = 4
        all_slots_taken = threading.Barrier(slots)

        async def hop() -> str:
            return await aio.run_legacy_segment(lambda: "checked")

        def body() -> str:
            all_slots_taken.wait(_HANG_SECONDS)
            return aio.call_from_thread(hop())

        async def main() -> list[str]:
            return await asyncio.gather(*(aio.run_legacy_segment(body) for _ in range(slots)))

        with process_loop(legacy=slots + 1) as loop:
            assert on_loop(loop, main()) == ["checked"] * slots

    def test_it_starts_from_any_thread_and_from_the_loop(self) -> None:
        async def main() -> str:
            return await asyncio.wrap_future(aio.start_legacy_segment(_current_thread_name))

        with process_loop() as loop:
            assert on_loop(loop, main()).startswith("inqtrix-legacy_segment")
            future = on_thread(lambda: aio.start_legacy_segment(_current_thread_name))
            assert future.result(timeout=_HANG_SECONDS).startswith("inqtrix-legacy_segment")

    def test_without_a_transition_lane_it_refuses(self) -> None:
        with process_loop(legacy=0), pytest.raises(aio.LoopNotConfigured):
            aio.start_legacy_segment(_current_thread_name)

    def test_without_a_process_loop_it_refuses(self) -> None:
        with pytest.raises(aio.LoopNotConfigured):
            aio.start_legacy_segment(_current_thread_name)


class TestLanes:
    def test_short_work_runs_on_the_blocking_lane(self) -> None:
        with process_loop() as loop:
            name = on_loop(loop, aio.run_blocking(_current_thread_name))
        assert name.startswith("inqtrix-blocking")

    def test_a_library_call_of_the_default_executor_lands_on_the_blocking_lane(self) -> None:
        async def main() -> str:
            return await run_in_executor(None, _current_thread_name)

        with process_loop() as loop:
            assert on_loop(loop, main()).startswith("inqtrix-blocking")

    def test_a_segment_runs_on_the_transition_lane(self) -> None:
        with process_loop() as loop:
            name = on_loop(loop, aio.run_legacy_segment(_current_thread_name))
        assert name.startswith("inqtrix-legacy_segment")

    def test_a_provider_call_runs_on_the_provider_lane(self) -> None:
        async def main() -> str:
            return await aio.run_provider_blocking(_current_thread_name)

        with process_loop() as loop:
            assert on_loop(loop, main()).startswith("inqtrix-provider")

    def test_thirty_long_provider_calls_leave_short_work_fast(self) -> None:
        release = threading.Event()

        async def main() -> list[float]:
            calls = [aio.run_provider_blocking(release.wait, 60.0) for _ in range(30)]
            durations: list[float] = []
            try:
                for _ in range(40):
                    started = time.monotonic()
                    await asyncio.wait_for(aio.run_blocking(_current_thread_name), timeout=2.0)
                    durations.append(time.monotonic() - started)
            finally:
                release.set()
            await asyncio.gather(*calls)
            return durations

        with process_loop(blocking=4, provider=30) as loop:
            durations = on_loop(loop, main())
        assert quantiles(durations, n=20, method="inclusive")[18] < 0.05

    def test_a_cancelled_waiter_returns_at_once_while_the_provider_thread_runs_on(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def long_call() -> str:
            started.set()
            release.wait(_HANG_SECONDS)
            return "late"

        async def wait_for(call: aio.ProviderCall[str]) -> str:
            return await call

        async def main() -> tuple[bool, str]:
            call = aio.run_provider_blocking(long_call)
            await aio.run_blocking(started.wait, _HANG_SECONDS)
            waiter = asyncio.ensure_future(wait_for(call))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            still_running = not call.future.done()
            release.set()
            return still_running, await asyncio.wrap_future(call.future)

        with process_loop() as loop:
            assert on_loop(loop, main()) == (True, "late")


class TestContext:
    def test_run_blocking_copies_the_context_and_changes_stay_behind(self) -> None:
        def in_thread() -> str:
            seen = _SUBJECT.get()
            _SUBJECT.set("thread")
            return seen

        async def main() -> tuple[str, str]:
            _SUBJECT.set("caller")
            seen = await aio.run_blocking(in_thread)
            return seen, _SUBJECT.get()

        with process_loop() as loop:
            assert on_loop(loop, main()) == ("caller", "caller")

    def test_a_provider_call_copies_the_context(self) -> None:
        async def main() -> str:
            _SUBJECT.set("caller")
            return await aio.run_provider_blocking(_SUBJECT.get)

        with process_loop() as loop:
            assert on_loop(loop, main()) == "caller"

    def test_call_from_thread_carries_the_callers_context_to_the_loop(self) -> None:
        async def read_subject() -> str:
            return _SUBJECT.get()

        def caller() -> str:
            _SUBJECT.set("foreign-thread")
            return aio.call_from_thread(read_subject())

        with process_loop():
            assert on_thread(caller) == "foreign-thread"

    def test_a_segment_sees_its_context_and_reaches_the_loop_that_started_it(self) -> None:
        """Two configured loops: the default is the other one, as in a user's own loop."""

        async def read_on_loop() -> tuple[asyncio.AbstractEventLoop, str]:
            return asyncio.get_running_loop(), _SUBJECT.get()

        def segment() -> tuple[str, tuple[asyncio.AbstractEventLoop, str]]:
            return _SUBJECT.get(), aio.call_from_thread(read_on_loop())

        async def main() -> tuple[str, tuple[asyncio.AbstractEventLoop, str]]:
            _SUBJECT.set("budget-guard")
            return await aio.run_legacy_segment(segment)

        with process_loop() as starter, process_loop() as default:
            assert aio.default_loop() is default
            seen, (reached, inner) = on_loop(starter, main())
        assert seen == "budget-guard"
        assert reached is starter
        assert inner == "budget-guard"

    @pytest.mark.parametrize(
        "bridge",
        ["run_blocking", "run_provider_blocking", "run_legacy_segment", "call_from_thread"],
    )
    def test_the_request_id_survives_every_bridge(self, bridge: str) -> None:
        async def read_on_loop() -> dict[str, str]:
            return current_log_context()

        def on_foreign_thread() -> dict[str, str]:
            bind_log_context(request_id="req_bridge")
            return aio.call_from_thread(read_on_loop())

        async def main() -> dict[str, str]:
            bind_log_context(request_id="req_bridge")
            if bridge == "run_blocking":
                return await aio.run_blocking(current_log_context)
            if bridge == "run_provider_blocking":
                return await aio.run_provider_blocking(current_log_context)
            return await aio.run_legacy_segment(current_log_context)

        with process_loop() as loop:
            if bridge == "call_from_thread":
                context = on_thread(on_foreign_thread)
            else:
                context = on_loop(loop, main())
        assert context == {"request_id": "req_bridge"}


def blocking_culprit(seconds: float) -> str:
    secret_local = "CANARY-LOCAL-VALUE"
    time.sleep(seconds)  # CANARY-SOURCE-LINE
    return secret_local


@pytest.mark.blocks_the_loop_on_purpose
class TestWatching:
    @pytest.fixture
    def fast_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(aio, "_LAG_PROBE_SECONDS", 0.01)
        monkeypatch.setattr(aio, "_STALL_SECONDS", 0.2)
        monkeypatch.setattr(aio, "_STALL_CHECK_SECONDS", 0.02)

    def test_a_stall_names_the_blocking_function_once_without_source_or_locals(
        self, fast_probe: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def main() -> None:
            await asyncio.sleep(0.05)
            blocking_culprit(0.5)
            await asyncio.sleep(0.05)

        with caplog.at_level(logging.WARNING, logger="inqtrix.aio"):
            with process_loop(watch=True) as loop:
                on_loop(loop, main())
                assert aio.stall_count(loop) == 1
        stalls = _messages(caplog, "stalled")
        assert len(stalls) == 1
        assert f"{__name__}:blocking_culprit:" in stalls[0]
        assert "CANARY" not in stalls[0]

    def test_a_second_stall_is_reported_again(
        self, fast_probe: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def main() -> None:
            await asyncio.sleep(0.05)
            blocking_culprit(0.4)
            await asyncio.sleep(0.1)
            blocking_culprit(0.4)
            await asyncio.sleep(0.05)

        with caplog.at_level(logging.WARNING, logger="inqtrix.aio"):
            with process_loop(watch=True) as loop:
                on_loop(loop, main())
                assert aio.stall_count(loop) == 2
        assert len(_messages(caplog, "stalled")) == 2

    def test_a_late_loop_shows_in_the_lag_and_warns_once(
        self,
        fast_probe: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(aio, "_LAG_WARNING_SECONDS", 0.05)

        async def main() -> None:
            await asyncio.sleep(0.05)
            time.sleep(0.12)
            await asyncio.sleep(0.05)
            time.sleep(0.12)
            await asyncio.sleep(0.05)

        with caplog.at_level(logging.WARNING, logger="inqtrix.aio"):
            with process_loop(watch=True) as loop:
                on_loop(loop, main())
                lag = aio.loop_lag(loop)
        assert lag.samples > 2
        assert lag.p99_ms >= 50.0
        assert lag.p50_ms < lag.p99_ms
        assert len(_messages(caplog, "Event loop lag")) == 1

    def test_a_quiet_loop_reports_nothing(
        self, fast_probe: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="inqtrix.aio"):
            with process_loop(watch=True) as loop:
                on_loop(loop, asyncio.sleep(0.4))
                lag = aio.loop_lag(loop)
                assert aio.stall_count(loop) == 0
        assert lag.samples > 0
        assert _messages(caplog, "stalled") == []

    def test_without_watching_there_is_no_probe_and_no_watchdog(self, fast_probe: None) -> None:
        with process_loop(watch=False) as loop:
            on_loop(loop, asyncio.sleep(0.05))
            assert aio.loop_lag(loop).samples == 0
            assert _WATCHDOG_THREAD not in {thread.name for thread in threading.enumerate()}

    def test_closing_a_loop_ends_its_watchdog(self, fast_probe: None) -> None:
        def watchdogs() -> list[threading.Thread]:
            return [thread for thread in threading.enumerate() if thread.name == _WATCHDOG_THREAD]

        with process_loop(watch=True):
            assert len(watchdogs()) == 1
        for thread in watchdogs():
            thread.join(timeout=_HANG_SECONDS)
        assert watchdogs() == []


class TestLaneCounts:
    def test_a_full_lane_is_counted_and_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        release = threading.Event()

        async def main(
            loop: asyncio.AbstractEventLoop,
        ) -> tuple[aio.LaneSnapshot, aio.LaneSnapshot]:
            jobs = [
                asyncio.ensure_future(aio.run_blocking(release.wait, _HANG_SECONDS))
                for _ in range(3)
            ]

            def lane() -> aio.LaneSnapshot:
                return aio.lane_snapshot(loop)[aio.BLOCKING_LANE]

            await _until(lambda: lane().running == 1 and lane().waiting == 2)
            full = lane()
            release.set()
            await asyncio.gather(*jobs)
            await _until(lambda: lane().running == 0)
            return full, lane()

        with caplog.at_level(logging.WARNING, logger="inqtrix.aio"):
            with process_loop(blocking=1) as loop:
                full, drained = on_loop(loop, main(loop))
        assert full == aio.LaneSnapshot(running=1, waiting=2, workers=1)
        assert drained == aio.LaneSnapshot(running=0, waiting=0, workers=1)
        warnings = _messages(caplog, "is full")
        assert len(warnings) == 1
        assert aio.BLOCKING_LANE in warnings[0]

    def test_every_lane_is_counted_on_its_own(self) -> None:
        with process_loop(blocking=3, provider=4, legacy=5) as loop:
            assert aio.lane_snapshot(loop) == {
                aio.BLOCKING_LANE: aio.LaneSnapshot(running=0, waiting=0, workers=3),
                aio.PROVIDER_LANE: aio.LaneSnapshot(running=0, waiting=0, workers=4),
                aio.LEGACY_LANE: aio.LaneSnapshot(running=0, waiting=0, workers=5),
            }

    def test_a_cancelled_waiting_job_leaves_the_count(self) -> None:
        release = threading.Event()

        async def main(loop: asyncio.AbstractEventLoop) -> aio.LaneSnapshot:
            busy = asyncio.ensure_future(aio.run_blocking(release.wait, _HANG_SECONDS))
            queued = asyncio.ensure_future(aio.run_blocking(release.wait, _HANG_SECONDS))

            def lane() -> aio.LaneSnapshot:
                return aio.lane_snapshot(loop)[aio.BLOCKING_LANE]

            await _until(lambda: lane().waiting == 1)
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
            after_cancel = lane()
            release.set()
            await busy
            return after_cancel

        with process_loop(blocking=1) as loop:
            assert on_loop(loop, main(loop)).waiting == 0

    def test_a_refused_job_leaves_no_count_behind(self) -> None:
        with process_loop() as loop:
            lanes = aio._state_of(loop)
            assert lanes is not None
            lanes.blocking.shutdown(wait=False)
            with pytest.raises(RuntimeError):
                lanes.blocking.submit(_current_thread_name)
            assert aio.lane_snapshot(loop)[aio.BLOCKING_LANE].waiting == 0


class TestMetrics:
    def test_a_scrape_reads_lag_stalls_and_lanes_from_the_bridge(self) -> None:
        prometheus_client = pytest.importorskip("prometheus_client")
        from inqtrix.observability.metrics_defs import build_call_metrics

        registry = prometheus_client.CollectorRegistry()
        build_call_metrics(registry)

        with process_loop(blocking=3, watch=True) as loop:
            on_loop(loop, asyncio.sleep(0.25))
            assert registry.get_sample_value(
                "inqtrix_event_loop_lag_milliseconds", {"quantile": "0.99"}
            ) == pytest.approx(aio.loop_lag(loop).p99_ms, abs=50.0)
            assert registry.get_sample_value("inqtrix_event_loop_stalls_total") == 0.0
            assert (
                registry.get_sample_value(
                    "inqtrix_thread_lane_jobs", {"lane": "blocking", "state": "running"}
                )
                == 0.0
            )
            assert (
                registry.get_sample_value("inqtrix_thread_lane_workers", {"lane": "blocking"})
                == 3.0
            )

    def test_a_scrape_without_a_loop_reads_zero_and_no_lanes(self) -> None:
        prometheus_client = pytest.importorskip("prometheus_client")
        from inqtrix.observability.metrics_defs import build_call_metrics

        registry = prometheus_client.CollectorRegistry()
        build_call_metrics(registry)

        assert (
            registry.get_sample_value("inqtrix_event_loop_lag_milliseconds", {"quantile": "0.99"})
            == 0.0
        )
        assert (
            registry.get_sample_value("inqtrix_thread_lane_workers", {"lane": "blocking"}) is None
        )


class TestSettings:
    def test_the_lanes_default_to_the_standard_librarys_pool_size(self) -> None:
        settings = QueueSettings()
        expected = min(32, (os.cpu_count() or 1) + 4)
        assert aio.default_lane_size() == expected
        assert settings.blocking_pool_size == expected
        assert settings.provider_pool_size == expected

    def test_the_blocking_lane_keeps_a_second_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Closing a store holds one thread while the runs it waits for need another."""
        monkeypatch.setenv("INQTRIX_BLOCKING_POOL_SIZE", "1")
        with pytest.raises(ValueError, match="INQTRIX_BLOCKING_POOL_SIZE"):
            QueueSettings()

    def test_the_lanes_follow_their_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INQTRIX_BLOCKING_POOL_SIZE", "3")
        monkeypatch.setenv("INQTRIX_PROVIDER_POOL_SIZE", "7")
        settings = QueueSettings()
        assert (settings.blocking_pool_size, settings.provider_pool_size) == (3, 7)


class TestConfiguration:
    def test_closing_the_newest_loop_gives_the_default_back_to_the_older_one(self) -> None:
        with process_loop() as older:
            with process_loop() as newer:
                assert aio.default_loop() is newer
            assert aio.default_loop() is older
        assert aio.default_loop() is None

    def test_configuring_a_loop_again_replaces_its_lanes(self) -> None:
        with process_loop(blocking=1) as loop:
            first = aio._state_of(loop)
            aio.configure_loop(loop, blocking_workers=2, provider_workers=1, watch=False)
            assert first is not None
            assert first.blocking._shutdown
            assert aio.lane_snapshot(loop)[aio.BLOCKING_LANE].workers == 2
            assert on_loop(loop, aio.run_blocking(_current_thread_name)).startswith(
                "inqtrix-blocking"
            )

    def test_closing_an_unknown_loop_changes_nothing(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            aio.close_loop(loop)
        finally:
            loop.close()
        assert aio.default_loop() is None


def _pooled_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=1, max_overflow=0)


@pytest.mark.postgres
class TestPooledConnection:
    """asyncpg binds a connection to its loop: one loop per process can keep a pool."""

    def test_two_calls_from_different_threads_use_the_same_pooled_connection(self) -> None:
        url = os.environ["INQTRIX_TEST_DATABASE_URL"]

        async def build() -> AsyncEngine:
            return _pooled_engine(url)

        async def backend_pid(engine: AsyncEngine) -> int:
            async with engine.connect() as connection:
                return int((await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one())

        with process_loop() as loop:
            engine = on_loop(loop, build())
            try:
                first = on_thread(lambda: aio.call_from_thread(backend_pid(engine)))
                second = on_thread(lambda: aio.call_from_thread(backend_pid(engine)))
            finally:
                on_loop(loop, engine.dispose())
        assert first == second

    def test_parallel_threads_for_200_rounds_never_trip_over_the_pooled_connection(self) -> None:
        url = os.environ["INQTRIX_TEST_DATABASE_URL"]

        async def build() -> AsyncEngine:
            return _pooled_engine(url)

        async def read(engine: AsyncEngine) -> int:
            async with engine.connect() as connection:
                return int((await connection.execute(text("SELECT 1"))).scalar_one())

        with process_loop() as loop:
            engine = on_loop(loop, build())
            try:
                with ThreadPoolExecutor(3, thread_name_prefix="test-store-caller") as callers:
                    for _ in range(200):
                        calls = [
                            callers.submit(lambda: aio.call_from_thread(read(engine)))
                            for _ in range(3)
                        ]
                        assert [call.result(timeout=_HANG_SECONDS) for call in calls] == [1, 1, 1]
            finally:
                on_loop(loop, engine.dispose())


def test_the_startup_heap_is_collected_before_it_is_frozen(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        aio,
        "gc",
        SimpleNamespace(
            collect=lambda: calls.append("collect"),
            freeze=lambda: calls.append("freeze"),
        ),
    )

    aio.freeze_startup_heap()

    assert calls == ["collect", "freeze"]
