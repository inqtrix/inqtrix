"""Shared scaffolding for the durable job stores (runs and reindex).

:class:`~inqtrix.runs.postgres_store.PostgresRunStore` and
:class:`~inqtrix.runs.indexing_postgres.PostgresIndexingJobStore` are
two durable stores over the same shape: one async implementation per operation
on the process loop, and a sync name for job bodies on threads.
The schema-agnostic plumbing is genuinely identical between them, so it
lives here once, the same way
:class:`~inqtrix.runs.stream_queue.StreamJobQueue` and
:class:`~inqtrix.worker.loop.BaseWorkerLoop` share the queue and
worker-loop mechanics:

* ``_call``, the name sync code on a thread uses for an async operation,
  plus ``aclose``/``_session``;
* the no-queue in-process dispatch (``_dispatch_pending`` /
  ``_run_segment``), a template method whose claim, handle, and
  terminal-on-success steps are subclass hooks;
* :class:`PollingJobSubscription`, the DB-polling SSE tail, parameterized
  by the terminal-event set and a thread label.

What stays per-store is everything schema-bearing: the SQL bodies, the
run store's share/audit/owner_user_id/result surface, and the indexing
store's history-cap / active-collection / progress semantics.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from queue import Queue
from typing import TYPE_CHECKING, Any, Coroutine

from inqtrix import aio
from inqtrix.observability.metrics_defs import active_metrics
from inqtrix.storage.db import build_session_factory, tenant_session
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

DEFAULT_TENANT = "default"

_SUBSCRIPTION_POLL_SECONDS = 0.3

# Worker-side terminal writes retry through transient storage outages:
# losing that one write leaves the row non-terminal with nobody executing
# it. 5 attempts with 1→2→4→8 s backoff sleeps; the cap only matters if
# the attempt count is ever raised. Worst case per exhausted loop is
# attempts x statement timeout plus the sleeps, during which the worker
# thread keeps holding its execution slot — bounded by configuration,
# never unbounded.
_TERMINAL_WRITE_ATTEMPTS = 5
_TERMINAL_WRITE_BACKOFF_START_SECONDS = 1.0
_TERMINAL_WRITE_BACKOFF_CAP_SECONDS = 15.0

# Lost-execution fence (no-queue mode): active rows whose id is absent
# from the in-process registry are terminalized on read. The grace period
# only shields the submit→claim and wake→dispatch windows — ownership is
# decided by registry membership, never by age.
_EXECUTION_LOST_GRACE_SECONDS = 60.0
_EXECUTION_LOST_CHECK_INTERVAL_SECONDS = 15.0


@dataclass
class _LocalJob:
    """In-process execution state for one job (no-queue mode only)."""

    work: Any = field(repr=False, default=None)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    parked: bool = False
    """The job's run sits in a waiting status (agent interrupt): the
    dispatch worker keeps ``work`` so a resume can re-dispatch it."""
    park_in_flight: bool = False
    """Set between ``mark_waiting`` and the parking worker's unwind
    (its ``finally``). A resume landing in that window must defer its
    re-dispatch to the unwind (``resume_requested``) — dispatching
    earlier would run the closure on two threads at once."""
    resume_requested: bool = False
    """A resume arrived while ``park_in_flight``; the parking worker's
    unwind performs the deferred re-dispatch."""


class PollingJobSubscription:
    """Event subscription backed by short-interval database polling.

    Offers the exact attribute trio the SSE route consumes from the
    in-memory subscription: ``replay`` (already-stored events), ``queue``
    (live events as they land in Postgres — including those written by
    worker processes), and ``close()``. The terminal-event set and the
    poller thread label are the only per-job-kind differences, so they
    are constructor arguments — the seam :class:`StreamJobQueue` uses.

    The backing store must expose
    ``_events_after(entity_id, tenant_id, after_sequence) -> list[dict]``
    and atomically register/start the poller through
    ``_start_subscription(subscription, thread)``.
    """

    def __init__(
        self,
        store: Any,
        entity_id: str,
        tenant_id: str,
        replay: list[dict[str, Any]],
        *,
        after_sequence: int = 0,
        terminal_events: frozenset[str],
        thread_label: str,
        stream: bool = True,
    ) -> None:
        self.entity_id = entity_id
        self.replay = replay
        self.queue: Queue = Queue()
        self._store = store
        self._tenant_id = tenant_id
        self._terminal_events = terminal_events
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        last_seq = (
            replay[-1]["sequence"]
            if replay
            else max(0, int(after_sequence))
        )
        replay_is_terminal = bool(
            replay and replay[-1]["type"] in terminal_events
        )
        # ``stream=False`` is the one-shot replay read (the ``?format=json``
        # polling fallback): no poller thread, no registration — and no
        # viewer-histogram observation, which must count STREAM joins only.
        # A 3s polling cadence counted as joins would drown the 5b evidence
        # gate in per-poll artifacts and read real overlap as 1.
        if not replay_is_terminal and stream:
            self._thread = threading.Thread(
                target=self._poll,
                args=(last_seq,),
                name=f"{thread_label}-{entity_id}",
                daemon=True,
            )
            self._store._start_subscription(self, self._thread)

    def _poll(self, last_seq: int) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    events = self._store._events_after(
                        self.entity_id, self._tenant_id, last_seq
                    )
                    failures = 0
                except Exception:  # noqa: BLE001 — retry transient outages
                    failures += 1
                    log.warning(
                        "Event poller for %s: database error (attempt %d); polling will"
                        " retry.",
                        self.entity_id,
                        failures,
                        exc_info=failures == 1,
                    )
                    # A transient blip must not freeze the SSE stream for
                    # good; back off and keep polling until the client
                    # disconnects (close()) or the job ends.
                    self._stop.wait(min(5.0, failures))
                    continue
                if self._stop.is_set():
                    return
                for event in events:
                    last_seq = event["sequence"]
                    self.queue.put(event)
                    if event["type"] in self._terminal_events:
                        return
                self._stop.wait(_SUBSCRIPTION_POLL_SECONDS)
        finally:
            self._store._unregister_subscription(self)

    def close(self) -> None:
        """Request poller shutdown without blocking the SSE event loop.

        Request routes call this method from an async-generator ``finally``
        block.  Joining there would turn a slow database read into a global
        API event-loop stall.  The poller remains registered until its
        ``finally`` block runs, so a simultaneous store shutdown still finds
        and drains it before the engine goes.
        """
        self._stop.set()

    def _drain_for_store_close(self) -> None:
        """Stop and join this poller before its store closes."""

        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._store._unregister_subscription(self)


def resolve_orphan_sweep(queue: Any, recover_orphans: bool | None) -> bool:
    """Whether this store instance may run the blanket orphan sweep.

    The sweep unconditionally fails every queued/running row and is only
    correct for the documented single-process no-queue deployment.
    ``recover_orphans`` therefore overrides the historical ``queue is
    None`` inference: the queue-backed worker constructs its store with
    ``queue=None`` (claim-mode wiring) but must NOT sweep — Valkey
    stream reclaim is its crash-recovery path. ``None`` keeps the
    inference for existing constructors (API single-process unchanged).
    """
    if recover_orphans is None:
        return queue is None
    return bool(recover_orphans)


class DurableJobStoreBase:
    """The schema-agnostic half of a durable Postgres job store.

    Subclasses set two class attributes (``_dispatch_thread_prefix``,
    ``_job_kind``) and implement the schema-bearing hooks (``_claim_db``,
    ``_make_handle``, ``_auto_complete``, plus the public ``fail`` the
    no-queue worker calls). The ``__init__`` here builds the session factory on
    the engine of the process and owns the in-process dispatch bookkeeping;
    subclasses add their own sizing/retention/audit state after calling
    ``super()``.
    """

    _dispatch_thread_prefix: str = "inqtrix-job"
    _job_kind: str = "Durable job"

    # In-process execution slots, set by each store that actually dispatches.
    # DELIBERATELY declared without a value: subclasses also read it as the
    # ceiling of their queue-full admission check, where a silent default
    # would not mean "dispatches nothing" but "already at capacity" -- an
    # inherited zero turns ``running >= max_concurrent`` permanently true and
    # rejects work that should have been admitted. A store that forgets the
    # assignment therefore raises AttributeError at first read instead of
    # quietly refusing jobs. A store that never dispatches (upload, whose work
    # is composed by UploadOperationService) never reads it at all.
    _max_concurrent: int

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        worker_id: str,
        queue: Any,
        recover_orphans: bool | None = None,
    ) -> None:
        self._session_factory = build_session_factory(engine)
        self._app_role = app_role
        self._worker_id = worker_id
        self._queue = queue
        # Guards the registry below for moments only, so the loop may take it:
        # nothing holds it across a bridge or an await.
        self._lock = threading.RLock()
        self._local: dict[str, _LocalJob] = {}
        self._pending: deque[str] = deque()
        self._running_count = 0
        self._dispatcher: asyncio.Task[None] | None = None
        self._segments: set[asyncio.Task[None]] = set()
        self._segment_threads: set[threading.Thread] = set()
        self._subscriptions: set[PollingJobSubscription] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        # In-process execution: queued/running rows from a previous
        # process are unrecoverable orphans (their work closures died
        # with it). Swept lazily on the first database touch; queue-mode
        # API processes never sweep — workers own those rows. The sweep
        # is decoupled from the queue-mode inference via
        # ``recover_orphans`` because the inference is WRONG for one
        # constructor: the queue-backed WORKER also builds its store
        # with ``queue=None`` (worker-claim wiring, not in-process
        # ownership) and must pass an explicit ``False`` — its
        # crash-recovery path is stream reclaim, and the blanket sweep
        # would fail every queued/running run of the deployment on
        # worker start.
        #
        # ``_recovers_orphans`` is the standing capability (it also
        # gates the lost-execution fence); ``_sweep_orphans`` is the
        # one-shot restart-sweep flag consumed by the first cleanup.
        self._recovers_orphans = resolve_orphan_sweep(queue, recover_orphans)
        self._sweep_orphans = self._recovers_orphans
        self._last_execution_lost_check: float | None = None

    # -- async bridge ----------------------------------------------------- #

    @property
    def worker_id(self) -> str:
        """Identity this process stamps into ``claimed_by``."""
        return self._worker_id

    def _call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run one operation on the process loop and wait for it, from a thread."""
        if self._closed:
            coro.close()
            raise RuntimeError(f"{self._job_kind} store is closed")
        if self._lock._is_owned():
            coro.close()
            raise RuntimeError(f"{self._job_kind} store lock held across the bridge")
        return aio.call_from_thread(coro)

    async def astart(self) -> None:
        """The work a store does once when its process starts serving."""

    async def aclose(self) -> None:
        """Drain local segments before the engine they write through goes.

        A no-queue store executes work on threads that reach the process loop
        for every checkpoint and terminal write. Losing the engine while
        such a body is still alive strands its durable write and can leave
        the checked-out transaction blocking unrelated cleanup. Shutdown
        therefore fences new local dispatch, requests cooperative
        cancellation, and waits for every body already running. There is no
        hidden drain timeout: an uncooperative dependency remains an explicit
        shutdown failure instead of being reported as safely closed.
        """
        if self._close_task is None:
            self._close_task = asyncio.get_running_loop().create_task(
                self._close_now(),
                name=f"{self._dispatch_thread_prefix}-close",
            )
        await asyncio.shield(self._close_task)

    def close(self) -> None:
        """Close from a thread; a job body cannot wait for itself to end."""
        if self._closed:
            return
        with self._lock:
            if threading.current_thread() in self._segment_threads:
                raise RuntimeError(
                    f"{self._job_kind} store cannot close from its worker thread"
                )
        self._call(self.aclose())

    async def _close_now(self) -> None:
        with self._lock:
            self._closing = True
            for local in self._local.values():
                local.cancel_event.set()
            subscriptions = tuple(self._subscriptions)

        # Stop database pollers while the engine is still alive. Otherwise an
        # unclosed SSE subscription can retain an idle transaction that blocks
        # schema maintenance indefinitely. Joining happens off the loop, which
        # a poller may still need.
        for subscription in subscriptions:
            await aio.run_blocking(subscription._drain_for_store_close)
        # A claim in flight still starts its segment, with the cancel set above.
        if self._dispatcher is not None:
            await asyncio.wait({self._dispatcher})
        while self._segments:
            await asyncio.wait(set(self._segments))
        await self._close_queue_client()
        self._closed = True

    async def _close_queue_client(self) -> None:
        """Close what the queue opened on this loop; it cannot outlive the loop."""
        queue = getattr(self, "_dispatch_queue", self._queue)
        close = getattr(queue, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:  # noqa: BLE001 - shutdown goes on, visibly
            log.warning(
                "%s store could not close its queue client (error_type=%s).",
                self._job_kind,
                type(exc).__name__,
            )

    def _start_subscription(
        self,
        subscription: PollingJobSubscription,
        thread: threading.Thread,
    ) -> None:
        """Atomically track and start a database event poller.

        Registration and ``Thread.start`` share the lifecycle lock with the
        subscription snapshot in :meth:`close`.  Without that boundary,
        shutdown can observe a registered subscription after its thread
        object has been assigned but before it has been started; joining that
        object raises ``RuntimeError`` and leaves the store half-closed.
        """
        with self._lock:
            if self._closing or self._closed:
                raise RuntimeError(f"{self._job_kind} store is closing")
            self._subscriptions.add(subscription)
            # Concurrency this entity just reached, counted under the
            # SAME lock that owns the registry. Feeds the deferred
            # shared-poller decision; no entity ids leave this method.
            concurrent = sum(
                1
                for existing in self._subscriptions
                if existing.entity_id == subscription.entity_id
            )
            metrics = active_metrics()
            if metrics is not None:
                metrics.observe_stream_viewers(
                    job_kind=self._job_kind, concurrent=concurrent
                )
            try:
                thread.start()
            except BaseException:
                self._subscriptions.discard(subscription)
                raise

    def _unregister_subscription(
        self, subscription: PollingJobSubscription
    ) -> None:
        """Forget a terminal or explicitly closed poller."""

        with self._lock:
            self._subscriptions.discard(subscription)

    def _session(self, tenant_id: str):
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    # -- in-process execution (no-queue mode) ----------------------------- #

    def _kick_dispatch(self) -> "asyncio.Task[None]":
        """The one task that claims and starts what waits on this store.

        Nothing else drives it, so the cancelled request of one caller never
        leaves another caller's job claimed with nobody executing it.
        """
        task = self._dispatcher
        if task is None or task.done():
            # Whoever happens to start it lends it no request id and no user.
            task = asyncio.get_running_loop().create_task(
                self._dispatch_pending(),
                name=f"{self._dispatch_thread_prefix}-dispatch",
                context=contextvars.Context(),
            )
            self._dispatcher = task
        return task

    async def _dispatch_now(self) -> None:
        """Return once what waits has started; a cancelled caller leaves it running."""
        await asyncio.shield(self._kick_dispatch())

    async def _dispatch_pending(self) -> None:
        while True:
            with self._lock:
                if (
                    self._closing
                    or self._running_count >= self._max_concurrent
                    or not self._pending
                ):
                    return
                entity_id = self._pending.popleft()
                local = self._local.get(entity_id)
                if local is None or local.work is None:
                    continue
                work, cancel_event = local.work, local.cancel_event
                # Reserved across the claim, so the ceiling holds while it waits.
                self._running_count += 1
            try:
                claimed = await self._claim_local_db(entity_id)
            except Exception:  # noqa: BLE001 — keep dispatch draining
                # The claim did not commit (or its outcome is unknown):
                # drop local ownership so the row converges through the
                # lost-execution path instead of staying exempt behind a
                # dead registry entry, and keep draining — stopping here
                # would strand the remaining pending ids with no future
                # dispatch trigger on an idle process.
                self._forget_unclaimed(entity_id)
                log.exception(
                    "%s %s could not be claimed; local execution discarded, and the row"
                    " will be terminated as lost.",
                    self._job_kind,
                    entity_id,
                )
                continue
            if claimed is None:
                self._forget_unclaimed(entity_id)
                continue
            task = asyncio.get_running_loop().create_task(
                self._run_segment(entity_id, work, cancel_event, claimed),
                name=f"{self._dispatch_thread_prefix}-{entity_id}",
            )
            self._segments.add(task)
            task.add_done_callback(self._segments.discard)

    def _forget_unclaimed(self, entity_id: str) -> None:
        with self._lock:
            self._local.pop(entity_id, None)
            self._running_count = max(0, self._running_count - 1)

    async def _run_segment(
        self,
        entity_id: str,
        work: Any,
        cancel_event: threading.Event,
        claimed: Any,
    ) -> None:
        """One job body on the transition lane, then the handoff on the loop."""
        crashed = True
        try:
            crashed = await asyncio.wrap_future(
                aio.start_legacy_segment(
                    partial(self._run_local, entity_id, work, cancel_event, claimed)
                )
            )
        finally:
            with self._lock:
                self._release_local(entity_id, crashed=crashed)
            self._kick_dispatch()

    def _enter_execution_telemetry(
        self, stack: Any, entity_id: str, claimed: Any
    ) -> None:
        """Open root span/log context for one no-queue execution.

        No-op by default; job stores whose executions should appear as
        traces (the run store) override this. Implementations register
        all teardown on ``stack`` — the worker closes it in its finally.
        """

    def _persist_terminal_outcome(self, entity_id: str, write: Any) -> None:
        """Retry one worker-side terminal write through storage outages.

        Only the in-process worker unwind uses this: losing THIS write
        leaves the row non-terminal with nobody executing it, and the
        outage that killed the work is exactly when the write fails. The
        write is CAS-guarded and therefore idempotent. Public
        ``complete()``/``fail()`` never retry — API callers get the
        error immediately. Per-attempt duration is bounded only by the
        engine's statement timeout.
        """
        delay = _TERMINAL_WRITE_BACKOFF_START_SECONDS
        for attempt in range(1, _TERMINAL_WRITE_ATTEMPTS + 1):
            try:
                write()
                return
            except Exception:  # noqa: BLE001 — retry transient outages
                if (
                    self._closing
                    or self._closed
                    or attempt == _TERMINAL_WRITE_ATTEMPTS
                ):
                    log.error(
                        "Terminal write for %s %s abandoned after %d attempts; the row "
                        "remains active until lost-execution detection terminates it on"
                        " the next read.",
                        self._job_kind,
                        entity_id,
                        attempt,
                        exc_info=True,
                    )
                    raise
                log.warning(
                    "Terminal write for %s %s failed (attempt %d/%d); retrying in %.0f "
                    "s.",
                    self._job_kind,
                    entity_id,
                    attempt,
                    _TERMINAL_WRITE_ATTEMPTS,
                    delay,
                    exc_info=attempt == 1,
                )
                time.sleep(delay)
                delay = min(delay * 2, _TERMINAL_WRITE_BACKOFF_CAP_SECONDS)

    # -- lost-execution fence (no-queue mode) ----------------------------- #

    async def _expire_lost_executions(self) -> bool:
        """Terminalize active rows this process should execute but does not.

        No-queue mode only (queue mode: workers own the rows and stream
        reclaim recovers them); read-triggered and throttled, so quiet
        deployments pay nothing. Rows in ``self._local`` are owned
        (running, parked, or pending dispatch) and never touched; the
        per-candidate status CAS in the terminal write makes a lost race
        harmless. A thread that is alive but stuck keeps its registry
        entry and is deliberately out of scope here — the statement
        timeout bounds those hangs into ordinary failures.

        Assumes the documented single-API-process no-queue deployment: a
        second process sharing the database would have its in-flight
        rows terminalized here.
        """
        if not self._recovers_orphans:
            return False
        now = time.monotonic()
        with self._lock:
            if self._closing or self._closed:
                return False
            last = self._last_execution_lost_check
            if (
                last is not None
                and now - last < _EXECUTION_LOST_CHECK_INTERVAL_SECONDS
            ):
                return False
            self._last_execution_lost_check = now
        candidates = await self._lost_execution_candidates_db(
            _EXECUTION_LOST_GRACE_SECONDS
        )
        if not candidates:
            return False
        with self._lock:
            owned = set(self._local)
        lost = [
            candidate for candidate in candidates if candidate not in owned
        ]
        if not lost:
            return False
        return bool(await self._expire_lost_executions_db(lost))

    async def _lost_execution_candidates_db(
        self, grace_seconds: float
    ) -> list[str]:
        """Ids of active rows older than the dispatch grace period."""
        raise NotImplementedError

    async def _expire_lost_executions_db(self, entity_ids: list[str]) -> bool:
        """Terminalize confirmed lost executions through the store's path.

        Returns ``True`` when an expiry pass ran over the candidates —
        the per-row status CAS inside absorbs races, so the return value
        only tells the caller that post-commit handoffs may need
        draining, not that every candidate changed.
        """
        raise NotImplementedError

    def _run_local(
        self,
        entity_id: str,
        work: Any,
        cancel_event: threading.Event,
        claimed: Any,
    ) -> bool:
        """Execute one job body in this thread; whether it crashed."""
        from contextlib import ExitStack

        current = threading.current_thread()
        with self._lock:
            self._segment_threads.add(current)
        handle = self._make_claimed_handle(entity_id, cancel_event, claimed)
        crashed = False
        telemetry_stack = ExitStack()
        try:
            try:
                self._enter_execution_telemetry(
                    telemetry_stack, entity_id, claimed
                )
            except Exception:  # noqa: BLE001 — telemetry never blocks a run
                log.warning(
                    "Telemetry setup for %s %s failed.",
                    self._job_kind,
                    entity_id,
                    exc_info=True,
                )
            work(handle)
            # Auto-complete safety net: the work body normally completes
            # the job itself, so this is usually a no-op (suppressed
            # warning) — the public complete()/fail() path keeps the
            # genuine fenced-out warning. An execution that PARKED its
            # run never auto-completes: between the park and this line a
            # resume may already have re-queued the run, and completing
            # it here would destroy the interrupt.
            if not getattr(handle, "parked", False):
                self._persist_terminal_outcome(
                    entity_id, lambda: self._auto_complete(entity_id)
                )
        except Exception as exc:  # noqa: BLE001 — workers terminate cleanly
            crashed = True
            log.exception("%s %s failed", self._job_kind, entity_id)
            # ``except ... as`` unbinds its name at block exit; the retry
            # closure needs a binding that survives.
            failure = exc
            self._persist_terminal_outcome(
                entity_id,
                lambda: self._terminate_work_exception(
                    handle, entity_id, failure
                ),
            )
        finally:
            # Telemetry teardown FIRST (the span must close inside this
            # thread), then the park handoff: only after this unwind may
            # the retained closure be dispatched again (a resume that
            # arrived earlier parked its request in ``resume_requested``
            # and is honored now).
            try:
                telemetry_stack.close()
            except Exception:  # noqa: BLE001 — never skip the lock block
                log.warning(
                    "Telemetry teardown for %s %s failed.",
                    self._job_kind,
                    entity_id,
                    exc_info=True,
                )
            with self._lock:
                self._segment_threads.discard(current)
        return crashed

    def _release_local(self, entity_id: str, *, crashed: bool) -> None:
        """The park handoff of one finished body; the caller holds the lock."""
        local = self._local.get(entity_id)
        if local is not None and local.parked and not crashed:
            local.park_in_flight = False
            if local.resume_requested:
                local.resume_requested = False
                local.parked = False
                self._pending.append(entity_id)
        else:
            self._local.pop(entity_id, None)
            if local is not None:
                local.work = None
        self._running_count = max(0, self._running_count - 1)

    # -- schema-bearing hooks (subclasses implement) ---------------------- #

    def _terminate_work_exception(
        self, handle: Any, entity_id: str, exc: BaseException
    ) -> None:
        """Persist one in-process work exception.

        Generic durable jobs retain the historical server-error behavior.
        Run stores override this hook to preserve their typed terminal
        vocabulary without coupling this shared base to run-domain modules.
        """
        del handle
        self.fail(entity_id, sanitize_error(exc))

    def _claim_db(self, entity_id: str, tenant_id: str, *, allow_takeover: bool):
        """Async CAS claim (queued -> running, attempt + 1)."""
        raise NotImplementedError

    async def _claim_local_db(self, entity_id: str) -> Any:
        """Claim work this process registered for in-process execution."""
        return await self._claim_db(entity_id, DEFAULT_TENANT, allow_takeover=False)

    def _make_handle(self, entity_id: str, cancel_event: threading.Event) -> Any:
        """Build the in-process (non-fenced) job handle."""
        raise NotImplementedError

    def _make_claimed_handle(
        self,
        entity_id: str,
        cancel_event: threading.Event,
        claimed: Any,
    ) -> Any:
        """Build an in-process handle with access to the durable claim.

        Most durable domains retain their historical handle. Indexing
        overrides this seam because publication itself must carry the exact
        attempt acquired by ``_claim_db`` even without an external queue.
        """

        del claimed
        return self._make_handle(entity_id, cancel_event)

    def _auto_complete(self, entity_id: str) -> None:
        """Terminal-on-success write with the no-op warning suppressed."""
        raise NotImplementedError

    def fail(self, entity_id: str, message: str, *, error_type: str = "server_error") -> Any:
        """Mark the job failed (public surface; used by the no-queue worker)."""
        raise NotImplementedError
