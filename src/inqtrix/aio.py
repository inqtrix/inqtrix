"""The one thread bridge of a process, and the loop it reaches.

A process has one event loop. Whatever has to leave it for a thread, and whatever
sync code has to reach it, goes through here:

* ``run_blocking``: short sync or CPU-heavy work on a bounded pool that is also the
  loop's default executor, so ``run_in_executor(None, ...)`` from a library lands in it.
* ``run_provider_blocking``: long sync provider calls on a lane of their own, so they
  never queue in front of the short work.
* ``run_legacy_segment``: a whole sync engine or job body on the transition lane; it
  binds the loop into the thread's context for ``call_from_thread``.
* ``call_from_thread``: sync code awaits a coroutine on that loop.

The owner of a process (the API, a worker, the test session) configures its loop
before anything else runs. A process that never had an owner, such as a library
user's script or notebook, gets the library loop instead: started on first need,
once, on a daemon thread of its own. Where an owner exists it never starts, so a
server never falls back to a second loop.

The three lanes stay apart because a legacy segment waits for provider calls and for
the loop: on one shared pool, waiters could hold every thread the work they wait for
needs. A full lane leaves the loop healthy, so the lanes are counted as well as the
loop's lag. Only the standard library is used here: this module sits below everything
that observes it.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from statistics import quantiles
from types import FrameType
from typing import Any, Generic, ParamSpec, TypeVar

log = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

_LAG_PROBE_SECONDS = 0.1
_LAG_WINDOW_SAMPLES = 3_000
_LAG_WARNING_SECONDS = 0.25
_LAG_WARNING_INTERVAL_SECONDS = 60.0
_LANE_WARNING_INTERVAL_SECONDS = 60.0
_STALL_SECONDS = 2.0
_STALL_CHECK_SECONDS = 0.25
_STACK_DEPTH = 30
_CLOSED_LOOP_CHECK_SECONDS = 5.0
_WAITING_HOOK_SECONDS = 0.1

BLOCKING_LANE = "blocking"
PROVIDER_LANE = "provider"
LEGACY_LANE = "legacy_segment"


class LoopNotConfigured(RuntimeError):
    """``call_from_thread`` found neither a bound nor a configured loop."""


class CalledOnLoopThread(RuntimeError):
    """``call_from_thread`` on a loop thread would wait for itself."""


class CalledFromBlockingPool(RuntimeError):
    """``call_from_thread`` on a ``run_blocking`` thread: pool threads would wait on pool threads."""


_thread_state = threading.local()
_bound_loop: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = contextvars.ContextVar(
    "inqtrix_bound_loop", default=None
)


def _mark_blocking_thread() -> None:
    _thread_state.blocking_lane = True


class _Lane(ThreadPoolExecutor):
    """A bounded pool that counts what runs and what waits, whoever submits to it."""

    def __init__(
        self,
        name: str,
        workers: int,
        initializer: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            max_workers=workers,
            thread_name_prefix=f"inqtrix-{name}",
            initializer=initializer,
        )
        self.lane_name = name
        self.workers = workers
        self.running = 0
        self.waiting = 0
        self._count_lock = threading.Lock()
        self._warned_at: float | None = None

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        with self._count_lock:
            self.waiting += 1
        try:
            future = super().submit(self._counted, fn, *args, **kwargs)
        except BaseException:
            with self._count_lock:
                self.waiting -= 1
            raise
        future.add_done_callback(self._forget_if_never_started)
        self._warn_when_full()
        return future

    def _counted(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        with self._count_lock:
            self.waiting -= 1
            self.running += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._count_lock:
                self.running -= 1

    def _forget_if_never_started(self, future: Future[Any]) -> None:
        if future.cancelled():
            with self._count_lock:
                self.waiting -= 1

    def _warn_when_full(self) -> None:
        with self._count_lock:
            if self.running + self.waiting <= self.workers:
                return
            now = time.monotonic()
            if (
                self._warned_at is not None
                and now - self._warned_at < _LANE_WARNING_INTERVAL_SECONDS
            ):
                return
            self._warned_at = now
            running, waiting = self.running, self.waiting
        log.warning(
            "Thread lane %s is full: %d running, %d waiting, %d workers.",
            self.lane_name,
            running,
            waiting,
            self.workers,
        )


@dataclass(frozen=True, slots=True)
class LoopLagSnapshot:
    """How late the loop's probe woke up in the recent window."""

    p50_ms: float
    p99_ms: float
    samples: int


@dataclass(frozen=True, slots=True)
class LaneSnapshot:
    """What one lane is doing right now."""

    running: int
    waiting: int
    workers: int


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    blocking: _Lane
    provider: _Lane
    legacy: _Lane | None
    watch: bool
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_LAG_WINDOW_SAMPLES))
    samples_lock: threading.Lock = field(default_factory=threading.Lock)
    beat: float = 0.0
    thread_id: int | None = None
    stalls: int = 0
    lag_warned_at: float | None = None
    probe: asyncio.Task[None] | None = None
    stop_watch: threading.Event = field(default_factory=threading.Event)
    watcher: threading.Thread | None = None

    def lanes(self) -> tuple[_Lane, ...]:
        lanes = (self.blocking, self.provider)
        return lanes + (self.legacy,) if self.legacy is not None else lanes


_states: dict[asyncio.AbstractEventLoop, _LoopState] = {}
_states_lock = threading.Lock()
_default_loop: asyncio.AbstractEventLoop | None = None
_owner_configured = False
_library_loop: asyncio.AbstractEventLoop | None = None
_library_lock = threading.Lock()


def default_lane_size() -> int:
    """Threads of a lane nobody sized: the standard library's pool size."""
    return min(32, (os.cpu_count() or 1) + 4)


def configure_loop(
    loop: asyncio.AbstractEventLoop,
    *,
    blocking_workers: int,
    provider_workers: int,
    legacy_segment_workers: int = 0,
    watch: bool = True,
) -> None:
    """Make *loop* the process loop; called once per loop by whoever owns it.

    Installs the blocking lane as the loop's default executor and records the loop
    as the default of ``call_from_thread``. With *watch* a probe measures the loop's
    lag and a watchdog names the function of a stalled loop. From then on this
    process never starts the library loop, not even after the owner closed its loop.

    Args:
        loop: The loop this process runs on.
        blocking_workers: Threads of the short-work lane, also the default executor.
        provider_workers: Threads of the long provider lane.
        legacy_segment_workers: Threads of the transition lane. Zero means this
            process runs no whole sync segment on it.
        watch: Whether to start the lag probe and the stall watchdog.
    """
    global _owner_configured
    _owner_configured = True
    _install(
        loop,
        blocking_workers=blocking_workers,
        provider_workers=provider_workers,
        legacy_segment_workers=legacy_segment_workers,
        watch=watch,
    )


def _install(
    loop: asyncio.AbstractEventLoop,
    *,
    blocking_workers: int,
    provider_workers: int,
    legacy_segment_workers: int,
    watch: bool,
) -> None:
    global _default_loop
    state = _LoopState(
        loop=loop,
        blocking=_Lane(BLOCKING_LANE, blocking_workers, initializer=_mark_blocking_thread),
        provider=_Lane(PROVIDER_LANE, provider_workers),
        legacy=_Lane(LEGACY_LANE, legacy_segment_workers) if legacy_segment_workers > 0 else None,
        watch=watch,
    )
    loop.set_default_executor(state.blocking)
    with _states_lock:
        previous = _states.get(loop)
        _states[loop] = state
        _default_loop = loop
    if previous is not None:
        _stop_state(previous)
    if watch:
        state.beat = time.monotonic()
        loop.call_soon_threadsafe(_start_probe, state)
        state.watcher = threading.Thread(
            target=_watch_for_stalls,
            args=(state,),
            name="inqtrix-loop-watchdog",
            daemon=True,
        )
        state.watcher.start()


def close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Stop the probe and the watchdog, and shut the lanes of *loop* down."""
    global _default_loop
    with _states_lock:
        state = _states.pop(loop, None)
        if _default_loop is loop:
            _default_loop = next(reversed(_states), None)
    if state is not None:
        _stop_state(state)


def _library_loop_on_first_need() -> asyncio.AbstractEventLoop | None:
    """The loop of a process that never had an owner, started the first time it is needed.

    A library user's script or notebook reaches the providers' sync facades without
    owning a loop. One daemon thread runs this loop for the rest of the process,
    without probe and watchdog, because nothing in such a process reads them.
    """
    global _library_loop
    if _owner_configured:
        return None
    with _library_lock:
        if _library_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=_run_library_loop,
                args=(loop,),
                name="inqtrix-library-loop",
                daemon=True,
            ).start()
            size = default_lane_size()
            _install(
                loop,
                blocking_workers=size,
                provider_workers=size,
                legacy_segment_workers=size,
                watch=False,
            )
            _library_loop = loop
            log.info(
                "No process loop is configured; started a background loop for library use "
                "(%d threads per lane)",
                size,
            )
        return _library_loop


def _run_library_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        loop.run_forever()
    finally:
        loop.close()


def _stop_state(state: _LoopState) -> None:
    state.stop_watch.set()
    if state.probe is not None and not state.loop.is_closed():
        state.loop.call_soon_threadsafe(state.probe.cancel)
    for lane in state.lanes():
        lane.shutdown(wait=False, cancel_futures=True)


def _state_of_running_loop() -> _LoopState:
    loop = asyncio.get_running_loop()
    with _states_lock:
        state = _states.get(loop)
    if state is None:
        raise LoopNotConfigured("this loop was never given to configure_loop")
    return state


async def run_blocking(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run short sync or CPU-heavy work on the bounded pool, in a copy of this context.

    Pure Python CPU work only keeps the loop responsive here; the GIL gives it no
    capacity.
    """
    state = _state_of_running_loop()
    context = contextvars.copy_context()
    return await asyncio.wrap_future(state.blocking.submit(context.run, fn, *args, **kwargs))


class ProviderCall(Generic[T]):
    """A long provider call on its lane: await it, or watch its thread.

    A cancelled waiter gets ``CancelledError`` at once while the thread runs on to its
    timeout, so whoever holds a capacity slot frees it from ``future``, not from here.
    """

    def __init__(self, future: Future[T]) -> None:
        self.future = future

    def __await__(self) -> Generator[Any, None, T]:
        return asyncio.wrap_future(self.future).__await__()


def run_provider_blocking(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> ProviderCall[T]:
    """Start a long sync provider call on its own lane, in a copy of this context."""
    state = _state_of_running_loop()
    context = contextvars.copy_context()
    return ProviderCall(state.provider.submit(context.run, fn, *args, **kwargs))


async def run_legacy_segment(fn: Callable[[], T]) -> T:
    """Run a whole sync engine or job body on the transition lane.

    The thread gets a copy of this context with the running loop bound into it, so its
    ``call_from_thread`` reaches the loop that started the segment. The lane is never
    tighter than the claim's slots; a cancelled waiter leaves the thread running.
    """
    state = _state_of_running_loop()
    if state.legacy is None:
        raise LoopNotConfigured("this loop was configured without a transition lane")
    context = contextvars.copy_context()
    context.run(_bound_loop.set, asyncio.get_running_loop())
    return await asyncio.wrap_future(state.legacy.submit(context.run, fn))


def start_legacy_segment(fn: Callable[[], T]) -> Future[T]:
    """Start a job body on the transition lane from any thread; nobody has to wait for it.

    The body gets a fresh context, as a new thread would: neither the log context nor
    the span of the request that happened to start it goes along. It reaches the
    process loop through ``call_from_thread``.
    """
    state = _state_of(None)
    if state is None or state.legacy is None:
        raise LoopNotConfigured("no process loop with a transition lane")
    context = contextvars.Context()
    context.run(_bound_loop.set, state.loop)
    return state.legacy.submit(context.run, fn)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _on_blocking_lane() -> bool:
    return bool(getattr(_thread_state, "blocking_lane", False))


def _target_loop() -> asyncio.AbstractEventLoop | None:
    """The loop bound by a segment, else the configured one, else the library loop."""
    return _bound_loop.get() or _default_loop or _library_loop_on_first_need()


def _is_library(loop: asyncio.AbstractEventLoop | None) -> bool:
    """Whether *loop* is the library loop, or would be once the first need starts it."""
    return loop is _library_loop if loop is not None else not _owner_configured


def _refusal_for(loop: asyncio.AbstractEventLoop | None) -> RuntimeError | None:
    """Why this thread must not wait for *loop*, or ``None`` if it may.

    Nobody waits for their own loop, and no ``run_blocking`` thread waits at all. A
    thread that runs another loop (a notebook cell, an application's own asyncio)
    may wait for the library loop only, blocking its own loop as any sync call does;
    in a process with an owner every loop thread is refused, as before.
    """
    running = _running_loop()
    if running is not None and (running is loop or not _is_library(loop)):
        return CalledOnLoopThread("call_from_thread on a loop thread; await the coroutine instead")
    if _on_blocking_lane():
        return CalledFromBlockingPool(
            "call_from_thread on a run_blocking thread; await the coroutine on the loop instead"
        )
    return None


def may_wait_for_loop() -> bool:
    """Whether this thread may wait for the loop it would reach."""
    return _refusal_for(_bound_loop.get() or _default_loop) is None


def _assert_may_wait_for_loop() -> None:
    """Refuse on a thread that must not wait for the loop it would reach."""
    _assert_may_wait_for(_bound_loop.get() or _default_loop)


def _assert_may_wait_for(loop: asyncio.AbstractEventLoop | None) -> None:
    """Refuse on a thread that must not wait for *loop*, and say so in the log.

    Several callers on the way up catch every exception, so the raise alone could
    vanish.

    Raises:
        CalledOnLoopThread: This thread runs a loop and would wait for itself.
        CalledFromBlockingPool: Pool threads would wait on pool threads.
    """
    error = _refusal_for(loop)
    if error is not None:
        raise _refusal(error)


def _refusal(error: RuntimeError) -> RuntimeError:
    log.error("Bridge refused: %s; called from: %s", error, _projected_stack(sys._getframe(2)))
    return error


def _submit(
    coro: Coroutine[Any, Any, T],
) -> tuple[asyncio.AbstractEventLoop, Future[T]]:
    loop = _target_loop()
    try:
        _assert_may_wait_for(loop)
        if loop is None or loop.is_closed():
            raise LoopNotConfigured("no process loop; a process owner must call configure_loop first")
    except (CalledOnLoopThread, CalledFromBlockingPool, LoopNotConfigured):
        coro.close()
        raise
    return loop, asyncio.run_coroutine_threadsafe(coro, loop)


def submit_from_thread(coro: Coroutine[Any, Any, T]) -> Future[T]:
    """Start *coro* on the loop from sync code that collects the result later.

    The same loop and the same refusals as ``call_from_thread``; whoever stops
    waiting for the returned future cancels it.

    Raises:
        CalledOnLoopThread: This thread runs a loop and would wait for itself.
        CalledFromBlockingPool: Pool threads would wait on pool threads.
        LoopNotConfigured: There is no loop to reach.
    """
    return _submit(coro)[1]


def call_from_thread(
    coro: Coroutine[Any, Any, T],
    *,
    while_waiting: Callable[[], None] | None = None,
) -> T:
    """Await *coro* on the loop from sync code and return its result.

    Reaches the loop bound by ``run_legacy_segment``, else the configured one, else
    the library loop of a process without an owner, carrying the caller's context.
    Allowed from the transition lane and foreign threads. If the wait itself ends
    with an exception (an interrupt, a failing *while_waiting*, a closed loop), the
    coroutine is cancelled, so no work runs on for a caller that left.

    Args:
        coro: The coroutine to run on the loop.
        while_waiting: Called on this thread every ``_WAITING_HOOK_SECONDS`` until the
            coroutine is done, for work that must happen on the waiting thread. If it
            raises, the coroutine is cancelled and the exception propagates.

    Raises:
        CalledOnLoopThread: This thread runs a loop and would wait for itself.
        CalledFromBlockingPool: Pool threads would wait on pool threads.
        LoopNotConfigured: There is no loop to reach: the owner's loop is gone.
    """
    loop, future = _submit(coro)
    interval = _CLOSED_LOOP_CHECK_SECONDS if while_waiting is None else _WAITING_HOOK_SECONDS
    try:
        while not wait_for_futures((future,), timeout=interval).done:
            if while_waiting is not None:
                while_waiting()
            if loop.is_closed():
                raise LoopNotConfigured("the process loop closed while a thread waited for it")
    except BaseException:
        future.cancel()
        raise
    return future.result()


async def on_process_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Await *coro* on the process loop, from whichever loop the caller runs on.

    On the process loop this is a plain ``await``. From another loop (a notebook,
    an application's own asyncio) the coroutine runs on the process loop, the
    library loop where no owner configured one, while the caller's loop goes on;
    cancelling the caller cancels the coroutine.

    Raises:
        LoopNotConfigured: There is no loop to reach: the owner's loop is gone.
    """
    loop = _target_loop()
    if loop is None or loop.is_closed():
        coro.close()
        raise LoopNotConfigured("no process loop; a process owner must call configure_loop first")
    if asyncio.get_running_loop() is loop:
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


async def wait_for_event(event: asyncio.Event, seconds: float) -> bool:
    """Whether *event* is set within *seconds*; the loop's ``threading.Event.wait``."""
    try:
        async with asyncio.timeout(seconds):
            await event.wait()
    except TimeoutError:
        return event.is_set()
    return True


def default_loop() -> asyncio.AbstractEventLoop | None:
    """The configured process loop, if there is one."""
    return _default_loop


def _state_of(loop: asyncio.AbstractEventLoop | None) -> _LoopState | None:
    with _states_lock:
        target = loop if loop is not None else _default_loop
        return _states.get(target) if target is not None else None


def loop_lag(loop: asyncio.AbstractEventLoop | None = None) -> LoopLagSnapshot:
    """The probe's lag over the recent window, of *loop* or the process loop."""
    state = _state_of(loop)
    if state is None:
        return LoopLagSnapshot(p50_ms=0.0, p99_ms=0.0, samples=0)
    with state.samples_lock:
        samples = list(state.samples)
    if not samples:
        return LoopLagSnapshot(p50_ms=0.0, p99_ms=0.0, samples=0)
    if len(samples) == 1:
        only = samples[0] * 1000.0
        return LoopLagSnapshot(p50_ms=only, p99_ms=only, samples=1)
    cuts = quantiles(samples, n=100, method="inclusive")
    return LoopLagSnapshot(p50_ms=cuts[49] * 1000.0, p99_ms=cuts[98] * 1000.0, samples=len(samples))


def lane_snapshot(loop: asyncio.AbstractEventLoop | None = None) -> dict[str, LaneSnapshot]:
    """What each lane of *loop* or the process loop is doing right now."""
    state = _state_of(loop)
    if state is None:
        return {}
    return {
        lane.lane_name: LaneSnapshot(running=lane.running, waiting=lane.waiting, workers=lane.workers)
        for lane in state.lanes()
    }


def stall_count(loop: asyncio.AbstractEventLoop | None = None) -> int:
    """How many stalls the watchdog of *loop* or the process loop has seen."""
    state = _state_of(loop)
    return state.stalls if state is not None else 0


def _start_probe(state: _LoopState) -> None:
    if not state.stop_watch.is_set():
        state.thread_id = threading.get_ident()
        state.probe = state.loop.create_task(_probe_lag(state), name="inqtrix-loop-lag")


async def _probe_lag(state: _LoopState) -> None:
    while True:
        started = time.monotonic()
        await asyncio.sleep(_LAG_PROBE_SECONDS)
        now = time.monotonic()
        state.beat = now
        lag = max(0.0, now - started - _LAG_PROBE_SECONDS)
        with state.samples_lock:
            state.samples.append(lag)
        if lag < _LAG_WARNING_SECONDS:
            continue
        if (
            state.lag_warned_at is None
            or now - state.lag_warned_at >= _LAG_WARNING_INTERVAL_SECONDS
        ):
            state.lag_warned_at = now
            log.warning("Event loop lag: %.0f ms.", lag * 1000.0)


def _watch_for_stalls(state: _LoopState) -> None:
    """Name the running function once per stall; the probe only reports after one ends."""
    reported = False
    while not state.stop_watch.wait(_STALL_CHECK_SECONDS):
        late = time.monotonic() - state.beat
        if late <= _STALL_SECONDS:
            reported = False
            continue
        if reported or state.thread_id is None:
            continue
        reported = True
        state.stalls += 1
        log.warning(
            "Event loop stalled for %.1f s; its thread is in: %s",
            late,
            _projected_stack(sys._current_frames().get(state.thread_id)),
        )


def freeze_startup_heap() -> None:
    """Keep what a process built at start out of every later full collection.

    Modules, routes and the composition live as long as the process, yet a full
    collection walks them all and holds the loop for 100 to 200 ms each time. Only a
    process's own entry point calls this: a program embedding the app owns its collector.
    """
    gc.collect()
    gc.freeze()


def _projected_stack(frame: FrameType | None) -> str:
    """Module, function and line of each frame, innermost first; never source or locals."""
    entries: list[str] = []
    while frame is not None and len(entries) < _STACK_DEPTH:
        module = frame.f_globals.get("__name__", "?")
        entries.append(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    return " < ".join(entries)
