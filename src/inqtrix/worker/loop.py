"""The worker loop: claim, execute, heartbeat, reclaim, reconcile.

Liveness/correctness model (sized by the 2026-06 research pass):

* At-least-once delivery + guarded job-row transitions = idempotent
  execution. The claim is a compare-and-set (``queued -> running``,
  ``attempt + 1``); terminal writes are fenced by
  ``(claimed_by, attempt)`` so a reclaimed zombie cannot overwrite the
  second attempt's result.
* The OWNING worker heartbeats its in-flight stream entries
  (``XCLAIM JUSTID``) so the reclaim idle threshold detects crashed
  workers within seconds-of-heartbeats, never the job duration.
* Takeover (claiming a ``running`` row) is allowed ONLY for
  redeliveries — fresh duplicate messages (e.g. from the reconciler)
  must not steal a healthy execution.
* Ack order is load-bearing: terminal state commits to Postgres
  FIRST, the stream entry is acked SECOND; a crash in the gap causes
  one redelivery that the state machine absorbs.
* Cross-process cancellation polls the job row's ``cancel_requested``
  column (the source of truth) and flips a local ``threading.Event``
  the work observes at node/document boundaries.
* Graceful shutdown stops claiming and drains in-flight jobs without
  cancelling them; on drain timeout the process exits and the
  heartbeat silence hands the jobs to another worker.
* Claim, heartbeat and cancel poll are tasks on the one process loop, so
  ``_active`` needs no lock: only the loop thread touches it, and it
  changes hands only at an ``await``. Job bodies stay synchronous on the
  transition lane until their engines are async.

The claim/heartbeat/reclaim/reconcile/cancel machinery is generic over
the job kind and lives in :class:`BaseWorkerLoop`; the run worker and
the reindex worker (:mod:`inqtrix.worker.indexing_loop`) differ only in
the execution body and a few store/queue method names, supplied via the
subclass hooks. Every lifecycle decision logs a marker (claim, skip,
takeover, dead-letter, cancel observed, fenced write discarded) —
Visibility takes precedence over cleverness.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Awaitable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar

from inqtrix import aio
from inqtrix.harness.contracts.binding import RunBindingNewer, WorkflowUnavailable
from inqtrix.storage.migration_contract import SchemaHeadMismatch
from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.auth.principal import Principal
from inqtrix.core.results import RunRequest
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.execution_failures import terminate_native_run
from inqtrix.observability.context import (
    bind_log_context,
)
from inqtrix.observability.otel import run_execute_span
from inqtrix.quota.models import QuotaSubject
from inqtrix.runs.records import RunHandle
from inqtrix.services.run_service import execute_run_request
from inqtrix.harness.contracts.events import ACTIVITY_EVENT

if TYPE_CHECKING:
    from inqtrix.core.algorithms import AlgorithmRegistry
    from inqtrix.core.context import RuntimeContext
    from inqtrix.runs.postgres_store import ClaimedRun, PostgresRunStore
    from inqtrix.runs.valkey_queue import QueuedJob, ValkeyRunQueue
    from inqtrix.services.agent_answer_publisher import AgentAnswerPublisher
    from inqtrix.services.agent_context import AgentContextResolver
    from inqtrix.services.execution_dependency_authority import (
        ExecutionDependencyAuthorizer,
    )
    from inqtrix.services.quota_service import QuotaService

log = logging.getLogger(__name__)

_CANCEL_POLL_SECONDS = 2.0
_RECONCILE_SECONDS = 60.0
_RECONCILE_MIN_AGE_SECONDS = 120.0
_RECONCILE_COOLDOWN_SECONDS = 600.0
_CLAIM_BLOCK_MS = 5_000
_ERROR_BACKOFF_SECONDS = 5.0
# How often a loop whose run claims stopped looks again whether it should end.
_CLAIMS_STOPPED_IDLE_SECONDS = 5.0
# A run that stopped a loop's claims holds them while it still waits for a
# worker: queued, or running under an owner this process cannot replace.
# Releasing on ``running`` would take an orphaned run over again after every
# idle period and spend its deliveries.
_CLAIMS_BLOCKING_STATUSES = frozenset({"queued", "running"})

TJob = TypeVar("TJob")
TClaimed = TypeVar("TClaimed")


class WorkerClaimGuardError(RuntimeError):
    """Raised when the deployment contract forbids further queue claims."""


class WorkerClaimUnavailableError(RuntimeError):
    """Pause claims while a transient contract probe cannot reach Postgres."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))


@dataclass
class _ActiveJob:
    """Book-keeping for one in-flight execution."""

    job: Any
    cancel_event: threading.Event
    segment: asyncio.Task[None] | None = None
    """Keeps the slot until the segment's thread has ended."""
    successor: Any | None = None
    """One queued follow-up dispatch held while a parked segment unwinds."""
    handoff_in_progress: bool = False
    """The successor occupies this slot while its row claim is landing."""


class BaseWorkerLoop(Generic[TJob, TClaimed]):
    """Generic claim-and-execute loop over a Valkey job stream.

    Subclasses supply the execution body and the few store/queue calls
    whose names differ by job kind (the dispatch id field, the stale-row
    feed, the cancel-request poll, the re-enqueue). Everything else —
    fencing, ack ordering, heartbeat, reclaim, self-reclaim handling,
    dead-lettering, reconcile, graceful drain — is shared.

    Args:
        store: Durable job store (claims, events, terminal writes). Must
            expose ``worker_id``, ``claim_for_execution`` and ``fail``.
        queue: Valkey queue bound to this worker's consumer name.
        concurrency: Parallel executions in this process.
        max_attempts: Delivery budget before dead-lettering.
        heartbeat_seconds: Idle-reset interval for in-flight entries.
        claim_idle_seconds: Reclaim threshold for entries whose owner
            stopped heartbeating.
        answer_publisher: The same durable Agent Desk answer publisher used by
            in-process RunService execution.
    """

    # inqtrix_run_queue_wait_seconds is a RUN metric; only the runs loop
    # opts in (indexing/deletion/upload dispatch waits would otherwise
    # pollute the unlabeled histogram).
    _observes_queue_wait = False

    def __init__(
        self,
        *,
        store: Any,
        queue: Any,
        concurrency: int,
        max_attempts: int,
        heartbeat_seconds: float,
        claim_idle_seconds: float,
        claim_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._heartbeat_seconds = heartbeat_seconds
        self._claim_idle_seconds = claim_idle_seconds
        self._claim_guard = claim_guard
        self._active: dict[str, _ActiveJob] = {}
        self._stop = asyncio.Event()
        self._terminated = asyncio.Event()
        self._watchers: list[asyncio.Task[None]] = []
        self._reenqueued: dict[str, float] = {}
        self._last_reclaim = 0.0
        self._last_reconcile = 0.0
        # Runs this process must not execute, by id with their tenant: while
        # one of them still waits for a worker, no further claims (see
        # _stop_claiming_for_binding and _release_ended_blocks); the active
        # entries finish.
        self._claims_blocked_by: dict[str, str] = {}

    # -- subclass hooks --------------------------------------------------- #

    def _entity_id(self, job: TJob) -> str:
        """The durable id carried by *job* (``run_id`` / ``job_id``)."""
        raise NotImplementedError

    def _execute(
        self, job: TJob, claimed: TClaimed, cancel_event: threading.Event
    ) -> bool:
        """Run the claimed job to a terminal state and ack on landing.

        Runs on a thread of the transition lane. Returns whether the stream
        entry was acknowledged, which is what lets a held successor start.
        """
        raise NotImplementedError

    async def _stale_dispatch(self) -> list[tuple[str, str]]:
        """``(entity_id, tenant_id)`` rows stuck queued (reconcile feed)."""
        raise NotImplementedError

    async def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        """Subset of watched ``{entity_id: tenant_id}`` with a pending cancel."""
        raise NotImplementedError

    async def _dispatch_status(self, entity_id: str, tenant_id: str) -> str | None:
        """Authoritative row status of *entity_id*; ``None`` when the row is gone."""
        raise NotImplementedError

    async def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        """Re-enqueue one dispatch message (reconciler)."""
        raise NotImplementedError

    async def _is_successor_dispatch(self, job: TJob) -> bool:
        """Whether *job* is a real queued successor of an active segment.

        Durable reindex jobs never park and therefore keep the historical
        duplicate-ACK behaviour. The run worker overrides this hook and reads
        the authoritative row status so a resume/wake message is not mistaken
        for a reconciler duplicate while the previous segment is unwinding.
        """
        del job
        return False

    async def _periodic_maintenance(self) -> None:
        """Run subsystem maintenance on the existing reconcile cadence."""


    # -- lifecycle -------------------------------------------------------- #

    def request_stop(self) -> None:
        """Stop claiming new jobs; in-flight jobs keep executing."""
        # DEBUG: every loop of the process gets this call at once; the
        # signal handler already wrote the one INFO line.
        log.debug(
            "Worker %s: loop stops claiming new jobs.",
            self._store.worker_id,
        )
        self._stop.set()

    async def drain(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for in-flight jobs to finish.

        Returns:
            ``True`` when everything finished; ``False`` when jobs are
            still executing — the caller exits anyway and heartbeat
            silence hands those jobs to another worker.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._active:
                self._terminated.set()
                return True
            await asyncio.sleep(0.5)
        remaining = list(self._active)
        if remaining:
            log.warning(
                "Worker %s: drain timed out; %d jobs will be reclaimed by another "
                "worker after heartbeat silence: %s",
                self._store.worker_id,
                len(remaining),
                ", ".join(remaining),
            )
        self._terminated.set()
        return not remaining

    async def run_forever(self) -> None:
        """Main loop; returns after :meth:`request_stop`."""
        await self._queue.ensure_group()
        if not await self._wait_for_claim_contract(immediate=True):
            return
        # Own-PEL drain only finds entries when the consumer name is
        # stable across restarts (container hostname without the pid);
        # with the default boot-unique name, crash recovery runs via
        # the idle-based reclaim instead — this call is then a no-op.
        for job in await self._queue.claim_pending():
            while not self._stop.is_set():
                try:
                    await self._start(job, takeover=True)
                except WorkerClaimUnavailableError as exc:
                    await aio.wait_for_event(self._stop, exc.retry_after_seconds)
                    continue
                except SchemaHeadMismatch as exc:
                    raise self._claim_fence_hit(exc) from exc
                except (RunBindingNewer, WorkflowUnavailable) as exc:
                    self._stop_claiming_for_binding(exc, job=job)
                break

        self._watchers = [
            asyncio.create_task(self._heartbeat_loop(), name="inqtrix-heartbeat"),
            asyncio.create_task(self._cancel_loop(), name="inqtrix-cancel-poll"),
        ]

        while not self._stop.is_set():
            try:
                await self._tick()
            except WorkerClaimUnavailableError as exc:
                await aio.wait_for_event(self._stop, exc.retry_after_seconds)
            except WorkerClaimGuardError:
                raise
            except SchemaHeadMismatch as exc:
                raise self._claim_fence_hit(exc) from exc
            except Exception:  # noqa: BLE001 — survive transient outages
                log.warning(
                    "Worker %s: transient error in the claim loop; retrying in %.0fs.",
                    self._store.worker_id,
                    _ERROR_BACKOFF_SECONDS,
                    exc_info=True,
                )
                await aio.wait_for_event(self._stop, _ERROR_BACKOFF_SECONDS)

    async def _wait_for_claim_contract(self, *, immediate: bool) -> bool:
        """Wait in-process for transient availability; reject fatal drift."""
        while not self._stop.is_set():
            try:
                await self._verify_claim_contract(immediate=immediate)
            except WorkerClaimUnavailableError as exc:
                await aio.wait_for_event(self._stop, exc.retry_after_seconds)
                continue
            return True
        return False

    async def _tick(self) -> None:
        """One claim-loop iteration (reclaim, reconcile, claim new)."""
        await self._verify_claim_contract()
        if self._claims_blocked_by:
            await self._release_ended_blocks()
        now = time.monotonic()
        free_slots = self._concurrency - len(self._active)
        if (
            not self._claims_blocked_by
            and now - self._last_reclaim >= self._heartbeat_seconds
            and free_slots > 0
        ):
            # Reclaim only what fits: the slot counter is the one limit of a
            # loop, and its lane is shared with the other loops of the process.
            self._last_reclaim = now
            for job in await self._queue.reclaim(
                min_idle_ms=int(self._claim_idle_seconds * 1000),
                count=free_slots,
            ):
                log.warning(
                    "Worker %s is taking over job %s after owner heartbeat silence "
                    "(delivery %d).",
                    self._store.worker_id,
                    self._entity_id(job),
                    job.delivery_count,
                )
                await self._start_claimed(job, takeover=True)
        if now - self._last_reconcile >= _RECONCILE_SECONDS:
            self._last_reconcile = now
            await self._reconcile()
        if self._claims_blocked_by:
            await aio.wait_for_event(self._stop, _CLAIMS_STOPPED_IDLE_SECONDS)
            return
        free_slots = self._concurrency - len(self._active)
        if free_slots < 1:
            await asyncio.sleep(0.5)
            return
        # Claim as many as this worker can actually run, not one per tick.
        # Every claim costs a queue round trip plus a durable claim write, so
        # filling N free slots one at a time takes N of them in sequence, and
        # a worker sized for many runs would take a long time to reach that
        # number while the work sits queued. The count is bounded by free
        # capacity, so a worker still never holds an entry it cannot start,
        # and other workers keep whatever they have room for.
        for job in await self._queue.claim_new(
            block_ms=_CLAIM_BLOCK_MS, count=free_slots
        ):
            await self._start_claimed(job, takeover=job.delivery_count > 1)

    async def _start_claimed(self, job: TJob, *, takeover: bool) -> None:
        """Start one entry the stream handed out; a run beyond this process stops the claims.

        The other entries of the same batch still start: each one's claim
        checks its own binding.
        """
        try:
            await self._start(job, takeover=takeover)
        except (RunBindingNewer, WorkflowUnavailable) as exc:
            self._stop_claiming_for_binding(exc, job=job)

    def _claim_fence_hit(self, exc: SchemaHeadMismatch) -> WorkerClaimGuardError:
        """Latch the shared guard for a schema fence hit; the caller raises the result.

        The claim rolled back and the queue entry stays unacked for a worker
        that matches it. A schema that changed under the process is a verdict
        about the process, not about the job, so every loop of this process
        stops.
        """
        self._latch_claim_guard(str(exc))
        log.error(
            "Worker %s: schema head changed under this process; no further "
            "claims (installed=%s, expected=%s).",
            self._store.worker_id,
            exc.installed,
            exc.expected,
        )
        return WorkerClaimGuardError(str(exc))

    def _latch_claim_guard(self, message: str) -> None:
        """Stick the shared guard shut after an in-transaction fence hit."""
        guard = self._claim_guard
        latch = getattr(guard, "latch_failure", None)
        if callable(latch):
            latch(message)

    async def _aclaim(
        self, entity_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> TClaimed | None:
        """Claim one entity through the store; a domain adds what its claim checks."""
        return await self._store.aclaim_for_execution(
            entity_id, tenant_id, allow_takeover=allow_takeover
        )

    def _stop_claiming_for_binding(
        self,
        exc: RunBindingNewer | WorkflowUnavailable,
        *,
        job: TJob,
        follow_up: str | None = None,
    ) -> None:
        """Stop this loop's claims while a run this process must not execute waits.

        The verdict is about the run's mode or contract, not about the
        process: only the run claims stop, while indexing, deletions and
        uploads go on and the shared guard stays open. Another worker has to
        take the entry, which stays unacknowledged; the active entries of
        this loop finish. The stop lasts until the run no longer waits for a
        worker (:meth:`_release_ended_blocks`).
        """
        self._claims_blocked_by[self._entity_id(job)] = job.tenant_id
        held = (
            f"; the follow-up dispatch for job {follow_up} remains unacknowledged"
            if follow_up is not None
            else ""
        )
        if isinstance(exc, RunBindingNewer):
            log.error(
                "Worker %s: run %s is bound to %s contract %d, newer than the %d "
                "this process carries; this worker claims no further runs until "
                "that run is cancelled, ends or parks, its other jobs go on%s.",
                self._store.worker_id,
                exc.run_id,
                exc.mode,
                exc.bound_contract,
                exc.worker_contract,
                held,
            )
            return
        log.warning(
            "Worker %s: run %s belongs to mode %s, which this process has not "
            "registered (registered: %s); this worker claims no further runs until "
            "that run is cancelled, ends or parks, its other jobs go on, and the run "
            "and its checkpoint stay%s.",
            self._store.worker_id,
            exc.run_id,
            exc.mode,
            ", ".join(exc.registered) or "none",
            held,
        )

    async def _release_ended_blocks(self) -> None:
        """Give the claims back once no blocking run waits for a worker any more.

        A blocking run with a pending cancel releases them: the next claim
        ends it in its cancel branch, which comes before the binding check. A
        run that is gone, has ended or parks (``waiting_*``, outside the
        stream) releases them as well. A queued run, or one running under an
        owner this process cannot replace, keeps them. A failing read keeps
        them too and surfaces through the claim loop's error handling.
        """
        cancelled = await self._cancel_requested(dict(self._claims_blocked_by))
        for entity_id, tenant_id in list(self._claims_blocked_by.items()):
            if entity_id in cancelled:
                state = "cancel requested"
            else:
                status = await self._dispatch_status(entity_id, tenant_id)
                if status in _CLAIMS_BLOCKING_STATUSES:
                    continue
                state = status or "gone"
            del self._claims_blocked_by[entity_id]
            log.info(
                "Worker %s: run %s no longer holds this worker's claims (%s).",
                self._store.worker_id,
                entity_id,
                state,
            )
        if not self._claims_blocked_by:
            log.info("Worker %s claims runs again.", self._store.worker_id)

    async def _verify_claim_contract(self, *, immediate: bool = False) -> None:
        """Fail closed before pending, reclaim, reconcile, or new claims.

        The coalesced call surfaces an already-latched failure and re-probes
        when the interval is due; the hard per-claim protection is the
        in-transaction schema fence (``assert_schema_head``) inside every
        durable-claim transaction. ``immediate=True`` (an uncoalesced probe
        via ``verify_now``) remains only for the startup waits, where no
        claim transaction exists yet to carry the fence. A guard without
        ``verify_now`` remains supported for tests and embedders.
        """
        guard = self._claim_guard
        if guard is None:
            return
        if immediate:
            verify_now = getattr(guard, "verify_now", None)
            if callable(verify_now):
                await verify_now()
                return
        await guard()

    async def _reconcile(self) -> None:
        """Re-enqueue queued rows whose dispatch message got lost.

        Re-enqueues each entity at most once per cooldown window: under
        sustained backlog every stale-but-healthy queued row would
        otherwise receive one duplicate per worker per minute (the
        duplicates are absorbed by ack-on-duplicate and the guarded
        claim, but flooding the stream helps nobody).
        """
        stale = await self._stale_dispatch()
        now = time.monotonic()
        for entity_id, tenant_id in stale:
            last_sent = self._reenqueued.get(entity_id)
            if (
                last_sent is not None
                and now - last_sent < _RECONCILE_COOLDOWN_SECONDS
            ):
                continue
            self._reenqueued[entity_id] = now
            log.warning(
                "Reconciler: job %s is stuck in queued status; resending its dispatch "
                "message.",
                entity_id,
            )
            await self._enqueue_dispatch(entity_id, tenant_id)
        if len(self._reenqueued) > 1_000:
            cutoff = now - _RECONCILE_COOLDOWN_SECONDS
            self._reenqueued = {
                entity_id: sent
                for entity_id, sent in self._reenqueued.items()
                if sent >= cutoff
            }
        await self._periodic_maintenance()

    async def _dead_letter_exhausted(self, job: TJob, entity_id: str) -> None:
        """Terminalize and dead-letter a job past its delivery budget.

        The takeover claim supplies the fence for the terminal write:
        superseding a possibly still-partitioned owner atomically turns
        its later writes into the existing visible fenced no-ops instead
        of racing this terminalization. A ``None`` claim means the row is
        already terminal or gone — row truth wins and only the message is
        parked. The upload store's ``fail`` REQUIRES the fence, so this
        path must never call an unfenced ``fail``. Row state is protected
        by the fence; provider SPEND is not — a partitioned owner keeps
        burning tokens until its next fenced write or cancel-poll
        observation, a deliberate property of the fencing model. A
        ``fail`` blip leaves the row RUNNING under this claim until the
        idle reclaim redelivers and retries this helper idempotently
        (about ``claim_idle_seconds`` later).
        """
        claimed = await self._aclaim(
            entity_id,
            job.tenant_id,
            allow_takeover=True,
        )
        if claimed is not None:
            await self._store.afail(
                entity_id,
                "Maximale Anzahl Ausfuehrungsversuche erreicht.",
                error_type="max_retries_exceeded",
                fence_attempt=claimed.attempt,
            )
        await self._queue.dead_letter(job, reason="max_attempts_exceeded")

    async def _start(self, job: TJob, *, takeover: bool) -> None:
        if self._stop.is_set():
            log.info(
                "Worker %s: dispatch for job %s not claimed after the shutdown "
                "boundary; the stream entry remains available for redelivery.",
                self._store.worker_id,
                self._entity_id(job),
            )
            return
        # The durable claim boundary carries its own in-transaction fence
        # (assert_schema_head inside every _claim_db), so no forced probe is
        # needed here -- the coalesced check below only surfaces an
        # already-latched failure cheaply.
        await self._verify_claim_contract()
        if self._stop.is_set():
            log.info(
                "Worker %s: dispatch for job %s stopped during the claim check; the "
                "stream entry remains available for redelivery.",
                self._store.worker_id,
                self._entity_id(job),
            )
            return
        entity_id = self._entity_id(job)
        while True:
            active = self._active.get(entity_id)
            active_message_id = active.job.message_id if active else None
            successor_message_id = (
                active.successor.message_id
                if active is not None and active.successor is not None
                else None
            )
            handoff_in_progress = bool(
                active is not None and active.handoff_in_progress
            )
            if active is None:
                break
            if job.message_id in {
                active_message_id,
                successor_message_id,
            }:
                # Self-reclaim: XAUTOCLAIM does not exclude the calling
                # consumer, so after a heartbeat gap a worker can reclaim its
                # OWN active or held-successor entry. Acking either would
                # destroy the job's crash-recovery entry.
                log.warning(
                    "Worker %s: self-reclaim for active job %s after heartbeat silence;"
                    " the entry remains held.",
                    self._store.worker_id,
                    entity_id,
                )
                return
            if handoff_in_progress or successor_message_id is not None:
                # One held successor is sufficient. Extra messages are true
                # duplicates and must not idle in the PEL until they can steal
                # the queued/running successor.
                log.info(
                    "Worker %s: additional duplicate dispatch for job %s acknowledged.",
                    self._store.worker_id,
                    entity_id,
                )
                await self._queue.ack(job.message_id)
                return
            # A queued row while the previous segment is still active is not
            # a reconciler duplicate: it is the durable resume/child-wake
            # successor. The state above may change during this status read.
            # If it fails, the exception propagates and the message remains
            # unacked for redelivery — never degrade an uncertain successor
            # into an ACK.
            if not await self._is_successor_dispatch(job):
                log.info(
                    "Worker %s: duplicate dispatch for active job %s acknowledged.",
                    self._store.worker_id,
                    entity_id,
                )
                await self._queue.ack(job.message_id)
                return
            current = self._active.get(entity_id)
            if current is not active:
                # The old execution completed during the row-status read;
                # re-evaluate and claim normally instead of losing the
                # successor in that handoff window.
                continue
            if current.successor is None and not current.handoff_in_progress:
                current.successor = job
                log.info(
                    "Worker %s: follow-up dispatch for parked job %s held until the"
                    " segment completes.",
                    self._store.worker_id,
                    entity_id,
                )
                return
            # A successor arrived during the status read. Keep the first one
            # and acknowledge this redundant message.
            await self._queue.ack(job.message_id)
            return
        if self._stop.is_set():
            log.info(
                "Worker %s: dispatch for job %s stopped before the database mutation; "
                "the stream entry remains available for redelivery.",
                self._store.worker_id,
                entity_id,
            )
            return
        if job.delivery_count > self._max_attempts:
            await self._dead_letter_exhausted(job, entity_id)
            return
        claimed = await self._aclaim(
            entity_id,
            job.tenant_id,
            allow_takeover=takeover,
        )
        if claimed is None:
            log.info(
                "Worker %s: job %s cannot be claimed because it is terminal, missing or"
                " active on another worker; message acknowledged.",
                self._store.worker_id,
                entity_id,
            )
            await self._queue.ack(job.message_id)
            return
        # Only first deliveries of run dispatches: a reclaimed message
        # keeps its ORIGINAL XADD timestamp, so redeliveries would fold
        # the previous attempt's runtime into "queue wait".
        if self._observes_queue_wait and job.delivery_count <= 1:
            _observe_queue_wait(job.message_id)
        entry = _ActiveJob(job=job, cancel_event=threading.Event())
        self._active[entity_id] = entry
        self._start_segment(job, claimed, entry)

    def _start_segment(self, job: TJob, claimed: TClaimed, entry: _ActiveJob) -> None:
        """Start one execution; its task holds the slot until the thread has ended."""
        entry.segment = asyncio.create_task(
            self._run_segment(job, claimed, entry.cancel_event),
            name=f"inqtrix-segment-{self._entity_id(job)}",
        )

    async def _run_segment(
        self, job: TJob, claimed: TClaimed, cancel_event: threading.Event
    ) -> None:
        acked = False
        try:
            acked = await aio.run_legacy_segment(
                partial(self._execute, job, claimed, cancel_event)
            )
        except Exception:  # noqa: BLE001 - nothing else observes this task
            log.exception(
                "Worker %s: segment for job %s ended with an error; redelivery remains "
                "responsible.",
                self._store.worker_id,
                self._entity_id(job),
            )
        await self._finish_active(job, allow_successor=acked)

    async def _finish_active(self, job: TJob, *, allow_successor: bool) -> None:
        """Release one execution slot and hand off its held successor.

        A successor is activated only after the old stream entry was ACKed.
        Otherwise the old entry could later be redelivered with takeover
        permission and fence out the newly started segment. The successor is
        kept as an active placeholder during its claim so capacity and drain
        never observe a false empty slot.
        """
        entity_id = self._entity_id(job)
        active = self._active.get(entity_id)
        if active is None or active.job.message_id != job.message_id:
            return
        successor = active.successor
        if successor is None or not allow_successor:
            self._active.pop(entity_id, None)
            if successor is not None:
                log.warning(
                    "Worker %s: follow-up dispatch for job %s remains "
                    "unacknowledged because the old stream entry was not safely "
                    "released; redelivery remains responsible.",
                    self._store.worker_id,
                    entity_id,
                )
            return
        placeholder = _ActiveJob(
            job=successor,
            cancel_event=threading.Event(),
            handoff_in_progress=True,
        )
        self._active[entity_id] = placeholder
        await self._activate_successor(entity_id, placeholder)

    def _drop_placeholder(self, entity_id: str, placeholder: _ActiveJob) -> None:
        if self._active.get(entity_id) is placeholder:
            self._active.pop(entity_id, None)

    async def _activate_successor(
        self, entity_id: str, placeholder: _ActiveJob
    ) -> None:
        """Claim and start a pre-registered successor placeholder."""
        job = placeholder.job
        try:
            if self._stop.is_set():
                self._drop_placeholder(entity_id, placeholder)
                log.info(
                    "Worker %s: follow-up dispatch for job %s not claimed during "
                    "shutdown; redelivery remains responsible.",
                    self._store.worker_id,
                    entity_id,
                )
                return
            # Successor claims carry the same in-transaction fence as every
            # other durable claim; the coalesced check only surfaces an
            # already-latched failure before spending work.
            await self._verify_claim_contract()
            if self._stop.is_set():
                self._drop_placeholder(entity_id, placeholder)
                log.info(
                    "Worker %s: follow-up dispatch for job %s stopped during the claim "
                    "check; redelivery remains responsible.",
                    self._store.worker_id,
                    entity_id,
                )
                return
            if job.delivery_count > self._max_attempts:
                await self._dead_letter_exhausted(job, entity_id)
                self._drop_placeholder(entity_id, placeholder)
                return
            claimed = await self._aclaim(
                entity_id,
                job.tenant_id,
                # A successor is a fresh queued segment. A crash during this
                # handoff leaves the message for normal reclaim, whose regular
                # _start call supplies takeover=True later.
                allow_takeover=False,
            )
            if claimed is None:
                await self._queue.ack(job.message_id)
                self._drop_placeholder(entity_id, placeholder)
                return
            # The row is RUNNING now. Open the placeholder for the NEXT
            # generation: a handoff flag still set when the segment parks would
            # misclassify its legitimate next successor as an extra duplicate.
            if self._active.get(entity_id) is placeholder:
                placeholder.handoff_in_progress = False
            self._start_segment(job, claimed, placeholder)
        except SchemaHeadMismatch as exc:
            self._drop_placeholder(entity_id, placeholder)
            # The in-transaction fence fired on the successor claim. This
            # path runs at the end of a segment's task, so raising would be
            # unobserved -- latching the shared guard is what stops every
            # loop of this process, exactly as the _tick path does.
            self._latch_claim_guard(str(exc))
            log.error(
                "Worker %s: schema head changed under this process; no further claims, "
                "and the follow-up dispatch for job %s remains unacknowledged "
                "(installed=%s, expected=%s).",
                self._store.worker_id,
                entity_id,
                exc.installed,
                exc.expected,
            )
        except (RunBindingNewer, WorkflowUnavailable) as exc:
            self._drop_placeholder(entity_id, placeholder)
            # Raising here would go unobserved, as for the schema fence.
            self._stop_claiming_for_binding(exc, job=job, follow_up=entity_id)
        except Exception:
            self._drop_placeholder(entity_id, placeholder)
            # The successor entry remains unacked. Surface the failure, but do
            # not let an unobserved task's exception disappear.
            log.exception(
                "Worker %s: follow-up dispatch for job %s could not be activated; "
                "redelivery remains responsible.",
                self._store.worker_id,
                entity_id,
            )

    def _heartbeat_message_ids(self) -> list[str]:
        """Active and held-successor stream ids."""
        message_ids: list[str] = []
        for active in self._active.values():
            message_ids.append(active.job.message_id)
            if active.successor is not None:
                message_ids.append(active.successor.message_id)
        return message_ids

    async def _heartbeat_loop(self) -> None:
        # Gated on _terminated, NOT _stop: heartbeats must continue
        # through the drain window or every draining job would be
        # reclaimed and double-executed by another worker.
        while not await aio.wait_for_event(self._terminated, self._heartbeat_seconds):
            try:
                message_ids = self._heartbeat_message_ids()
                if message_ids:
                    await self._queue.heartbeat(message_ids)
            except Exception:  # noqa: BLE001 — heartbeat loss must be visible
                log.warning(
                    "Worker %s: heartbeat failed; active jobs may be reclaimed by other"
                    " workers.",
                    self._store.worker_id,
                    exc_info=True,
                )

    async def _cancel_loop(self) -> None:
        # Gated on _terminated like the heartbeat: users must still be
        # able to cancel jobs that are draining.
        while not await aio.wait_for_event(self._terminated, _CANCEL_POLL_SECONDS):
            try:
                watched = {
                    self._entity_id(active.job): active.job.tenant_id
                    for active in self._active.values()
                    if not active.cancel_event.is_set()
                }
                if not watched:
                    continue
                cancelled = await self._cancel_requested(watched)
            except Exception:  # noqa: BLE001 — poller must not die silently
                log.warning(
                    "Cancellation poller: database query failed.",
                    exc_info=True,
                )
                continue
            for entity_id in cancelled:
                log.info(
                    "Worker %s: cancellation for job %s observed.",
                    self._store.worker_id,
                    entity_id,
                )
                active = self._active.get(entity_id)
                if active is not None:
                    active.cancel_event.set()


class FencedRunHandle(RunHandle):
    """Run handle whose terminal writes carry the claim fence.

    The fence ``(claimed_by, attempt)`` makes a zombie worker's late
    writes a visible no-op instead of overwriting the result of the
    attempt that superseded it.
    """

    def __init__(
        self,
        store: "PostgresRunStore",
        run_id: str,
        cancel_event: threading.Event,
        attempt: int,
    ) -> None:
        super().__init__(store, run_id, cancel_event)
        self._fence_attempt = attempt
        self.terminal_landed = False
        """Whether this attempt's terminal write actually landed —
        ``False`` means a superseding attempt fenced it out and the
        dispatch message must NOT be acked by this attempt."""
        self.terminal_outcome: str | None = None
        """Which terminal actually landed (completed|failed|cancelled) —
        the honest outcome label for the run_duration segment metric."""

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Emit one event, fenced to this claim attempt."""
        self._store.emit(
            self.run_id,
            event_type,
            payload or {},
            fence_attempt=self._fence_attempt,
        )

    @property
    def publication_fence_attempt(self) -> int:
        """Claim attempt that must own both answer-artifact writes."""
        return self._fence_attempt

    def complete(
        self,
        result: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Mark the run completed, fenced to this claim attempt."""
        self.terminal_landed = self._store.complete(
            self.run_id,
            result,
            snapshot=snapshot,
            fence_attempt=self._fence_attempt,
        )
        if self.terminal_landed:
            self.terminal_outcome = "completed"

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        """Mark the run failed, fenced to this claim attempt."""
        self.terminal_landed = self._store.fail(
            self.run_id,
            message,
            error_type=error_type,
            fence_attempt=self._fence_attempt,
        )
        if self.terminal_landed:
            self.terminal_outcome = "failed"

    def cancel(self, reason: str = "cancelled") -> None:
        """Mark the run cancelled, fenced to this claim attempt."""
        self.terminal_landed = self._store.mark_cancelled(
            self.run_id, reason=reason, fence_attempt=self._fence_attempt
        )
        if self.terminal_landed:
            self.terminal_outcome = "cancelled"

    def wait(self, status: Any) -> None:
        """Park the run, fenced to this claim attempt.

        The fence keeps a reclaimed zombie from parking a run the live
        attempt owns. After a successful park the worker loop ACKS the
        dispatch message like a terminal state — the resume re-enqueues
        a FRESH message and any worker continues from the persisted
        payload + checkpoint.
        """
        self._store.mark_waiting(
            self.run_id, status=status, fence_attempt=self._fence_attempt
        )
        self.parked = True


class WorkerLoop(BaseWorkerLoop["QueuedJob", "ClaimedRun"]):
    """Claim-and-execute loop for research runs.

    Args:
        store: Postgres run store (claims, events, terminal writes).
        queue: Valkey queue bound to this worker's consumer name.
        resolver: Agent context resolver — the worker re-resolves the
            persisted request body exactly like the HTTP path, so
            stacks/overrides/mode behave identically.
        registry: Algorithm registry shared with the API surface.
        runtime: App-level runtime context.
        concurrency: Parallel executions in this process.
        max_attempts: Delivery budget before dead-lettering.
        heartbeat_seconds: Idle-reset interval for in-flight entries.
        claim_idle_seconds: Reclaim threshold for entries whose owner
            stopped heartbeating.
    """

    _observes_queue_wait = True

    def __init__(
        self,
        *,
        store: "PostgresRunStore",
        queue: "ValkeyRunQueue",
        resolver: "AgentContextResolver",
        registry: "AlgorithmRegistry",
        runtime: "RuntimeContext",
        concurrency: int,
        max_attempts: int,
        heartbeat_seconds: float,
        claim_idle_seconds: float,
        quota_service: "QuotaService | None" = None,
        dependency_authorizer: "ExecutionDependencyAuthorizer | None" = None,
        answer_publisher: "AgentAnswerPublisher | None" = None,
        claim_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            store=store,
            queue=queue,
            concurrency=concurrency,
            max_attempts=max_attempts,
            heartbeat_seconds=heartbeat_seconds,
            claim_idle_seconds=claim_idle_seconds,
            claim_guard=claim_guard,
        )
        self._resolver = resolver
        self._registry = registry
        self._runtime = runtime
        # Metering for runs that execute off the API process. The worker
        # has no live principal, so token recording uses the canonical user
        # UUID reconstructed from the claimed run row (see _execute).
        self._quota_service = quota_service
        self._dependency_authorizer = dependency_authorizer
        self._answer_publisher = answer_publisher

    def _entity_id(self, job: "QueuedJob") -> str:
        return job.run_id

    async def _aclaim(
        self, entity_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> "ClaimedRun | None":
        # A run executes only under the contract it is bound to; the store
        # compares that binding with what this process registered.
        return await self._store.aclaim_for_execution(
            entity_id,
            tenant_id,
            allow_takeover=allow_takeover,
            workflow_contracts=self._registry.workflow_contracts(),
        )

    async def _periodic_maintenance(self) -> None:
        # Parked runs this process could no longer resume end at once, the
        # first time at startup, instead of at their next claim.
        ended = await self._store.aend_outdated_waits(
            self._registry.workflow_contracts()
        )
        if ended:
            log.info(
                "Worker %s ended %d parked runs that their workflow binding no "
                "longer lets resume.",
                self._store.worker_id,
                ended,
            )

    async def _stale_dispatch(self) -> list[tuple[str, str]]:
        return await self._store.astale_queued_runs(
            older_than_seconds=_RECONCILE_MIN_AGE_SECONDS,
        )

    async def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        return await self._store.acancel_requested_runs(watched)

    async def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        await self._queue.enqueue(run_id=entity_id, tenant_id=tenant_id)

    async def _dispatch_status(self, entity_id: str, tenant_id: str) -> str | None:
        return await self._store.adispatch_status(entity_id, tenant_id)

    async def _is_successor_dispatch(self, job: "QueuedJob") -> bool:
        """Recognize a queued resume/wake behind an unwinding segment."""
        return await self._dispatch_status(job.run_id, job.tenant_id) == "queued"

    def _execute(
        self,
        job: "QueuedJob",
        claimed: "ClaimedRun",
        cancel_event: threading.Event,
    ) -> bool:
        handle = FencedRunHandle(
            self._store, job.run_id, cancel_event, claimed.attempt
        )
        # Correlation for the WHOLE segment: every log line in this
        # thread carries run_id/tenant (JSON mode), and the execution
        # span parents itself in the submitter's trace via the
        # traceparent persisted in request_payload. The segment runs in a
        # copy of the context, so neither binding outlives it. The setup lives INSIDE the try so a telemetry failure reaches the
        # log below instead of the segment's task, like every other
        # failure of this body.
        telemetry_stack = ExitStack()
        old_message_acked = False
        try:
            telemetry_stack.enter_context(
                run_execute_span(
                    run_id=job.run_id,
                    tenant_id=job.tenant_id,
                    attempt=claimed.attempt,
                    payload=claimed.request_payload,
                )
            )
            bind_log_context(run_id=job.run_id, tenant=job.tenant_id)
            # Before the try: an exception during resolution still
            # reaches the segment observation below, which needs a
            # defined start time and run_request binding (setup time
            # then counts toward the failed segment — honest enough).
            segment_started = time.monotonic()
            run_request = None
            try:
                payload = claimed.request_payload
                if not payload:
                    raise RuntimeError(
                        ("Run has no saved assignment (request_payload is empty); "
                        "execution cannot start.")
                    )
                resolved = self._resolver.resolve(payload["body"])
                requested_stack = payload.get("body", {}).get("stack")
                if requested_stack and not resolved.stack_name:
                    # The API resolved this run against a named stack
                    # the worker does not know — executing on default
                    # providers would silently swap the model/search
                    # configuration underneath the user.
                    raise RuntimeError(
                        f'Stack {requested_stack!r} is not configured for this worker; worker and API must share the same stack configuration.'
                    )
                algorithm = self._registry.get(resolved.mode)
                body = payload.get("body", {}) or {}
                run_request = RunRequest(
                    mode=resolved.mode,
                    question=payload["question"],
                    history=payload.get("history", ""),
                    messages=payload.get("messages", []),
                    agent_overrides=resolved.agent_overrides,
                    knowledge_filters=resolved.knowledge_filters,
                    ui_language=str(body.get("ui_language", "") or ""),
                    autonomy=str(body.get("autonomy", "") or ""),
                    session_id=str(body.get("session_id", "") or ""),
                    document_id=str(body.get("document_id", "") or ""),
                    response_form=str(body.get("response_form", "") or ""),
                    skill_ids=tuple(
                        str(item) for item in (body.get("skill_ids") or [])
                    ),
                    skill_revisions={
                        str(key): int(value)
                        for key, value in (
                            body.get("skill_revisions") or {}
                        ).items()
                    },
                    tool_directives=tuple(
                        str(item)
                        for item in (body.get("tool_directives") or [])
                    ),
                    source_policy=body.get("source_policy") or {},
                    web_recency=body.get("web_recency") or None,
                    canvas_context=body.get("canvas_context") or None,
                    report_requirement=str(
                        body.get("report_requirement", "") or ""
                    ),
                    report_rules=tuple(
                        dict(item)
                        for item in (body.get("report_rules") or [])
                    ),
                    attached_reports=tuple(
                        dict(item)
                        for item in (body.get("attached_reports") or [])
                    ),
                    work_profile=str(body.get("work_profile", "") or ""),
                    handover=body.get("handover") or None,
                )
                # Reconstruct quota attribution from the persisted effective
                # actor UUID — the worker has no live principal, but the run's
                # token spend must still count toward that user's monthly
                # quota (the in-process path meters via the principal).
                actor_user_id = (
                    uuid.UUID(str(claimed.execution_actor_user_id))
                    if claimed.execution_actor_user_id is not None
                    else None
                )
                if actor_user_id is None and claimed.created_by_user_id is not None:
                    raise AuthorizationRevoked(
                        "run segment has no explicit effective actor"
                    )
                if actor_user_id is not None:
                    # Same stable pseudonym as in API logs/audit — the
                    # subject stays greppable across both processes.
                    bind_log_context(user=stable_pseudonym("usr", actor_user_id))
                quota_subject = None
                if (
                    self._quota_service is not None
                    and actor_user_id is not None
                    and claimed.created_by_tenant_id
                ):
                    quota_subject = QuotaSubject(
                        tenant_id=claimed.created_by_tenant_id,
                        user_id=actor_user_id,
                    )
                # The segment actor, never the owner by implication, scopes
                # tools, knowledge, child runs, quota, and audit.
                principal = None
                if (
                    actor_user_id is not None
                    and claimed.created_by_tenant_id
                ):
                    principal = Principal(
                        user_id=actor_user_id,
                        kind="oidc_session",
                        tenant_id=claimed.created_by_tenant_id,
                        role="member",
                        scopes=frozenset(claimed.execution_scopes),
                    )

                def _check_authority() -> None:
                    check_run = getattr(
                        self._store, "check_execution_authority", None
                    )
                    if callable(check_run):
                        check_run(job.run_id)
                    if self._dependency_authorizer is None:
                        # The actor and pinned-dependency probes live in
                        # the authorizer; a scoped segment must not run
                        # unchecked. Unscoped segments keep the historical
                        # behaviour (no authorizer, no dependency check).
                        if principal is not None:
                            raise AuthorizationRevoked(
                                "worker has no dependency authorizer"
                            )
                    else:
                        self._dependency_authorizer.check(
                            run_request,
                            principal,
                        )

                # Admission for this segment: a resumed segment carries a
                # NEW effective actor who was never checked in this
                # process, and the job may have been queued long ago. Fail
                # closed before burning provider tokens.
                _check_authority()
                requested_token_budget = int(
                    body.get("token_budget", 0) or 0
                ) or None
                if claimed.kind == "agent_child" and requested_token_budget:
                    # Pre-0043 mission planners embedded their own tiny caps in
                    # child replay payloads. Children now inherit only the
                    # operator's global run cap; trusted root overrides remain
                    # available for non-child runs.
                    log.warning(
                        "Legacy token budget in agent child %s ignored; operator limits"
                        " remain authoritative.",
                        job.run_id,
                    )
                    handle.emit(
                        ACTIVITY_EVENT,
                        {
                            "activity_id": (
                                f"legacy-child-budget:{job.run_id}"
                            ),
                            "scope": "task",
                            "phase": "execution",
                            "operation": "task.legacy_budget_ignored",
                            "detail": (
                                "Legacy task budget ignored"
                            ),
                            "status": "completed",
                            "task_id": str(
                                body.get("parent_task_id") or ""
                            ),
                            "fallback": True,
                        },
                    )
                    requested_token_budget = None
                segment_started = time.monotonic()  # narrow to pure execution
                execute_run_request(
                    handle,
                    algorithm=algorithm,
                    run_request=run_request,
                    resolved=resolved,
                    runtime=self._runtime,
                    principal=principal,
                    quota_service=self._quota_service,
                    quota_subject=quota_subject,
                    token_budget=requested_token_budget,
                    workspace_id=claimed.workspace_id,
                    trace_root=not body.get("parent_task_id"),
                    authority_check=(
                        _check_authority
                        if principal is not None
                        else None
                    ),
                    answer_publisher=self._answer_publisher,
                )
            except Exception as exc:  # noqa: BLE001 — terminal-write then ack
                log.exception("Worker run %s failed", job.run_id)
                terminate_native_run(handle, exc)
            # Segment metric AFTER terminal resolution (including the
            # exception path's terminate_native_run): the landed
            # terminal (completed|failed|cancelled) or the park is the
            # honest outcome. No landed terminal and no park = this
            # attempt was fenced out on EITHER path — skip; the winning
            # attempt records the segment.
            if getattr(handle, "parked", False):
                segment_outcome: str | None = "parked"
            else:
                segment_outcome = getattr(handle, "terminal_outcome", None)
            if segment_outcome is not None:
                _observe_run_segment(
                    mode=str(getattr(run_request, "mode", "") or ""),
                    outcome=segment_outcome,
                    seconds=time.monotonic() - segment_started,
                )
            if handle.terminal_landed or handle.parked:
                # Terminal state is committed (or the run is PARKED in a
                # waiting status — its resume re-enqueues a fresh
                # message); only now may the stream forget the job.
                aio.call_from_thread(self._queue.ack(job.message_id))
                old_message_acked = True
                _count_worker_job(
                    "runs",
                    "parked" if handle.parked else "terminal",
                )
            else:
                # Fenced out: a superseding attempt owns the run AND
                # this very message id — acking here would strip the
                # new owner's crash-recovery entry. Leave it; the
                # owner acks on completion.
                log.warning(
                    "Worker %s: run %s was taken over by another worker during "
                    "execution; result discarded, and the message remains with the new "
                    "owner.",
                    self._store.worker_id,
                    job.run_id,
                )
                _count_worker_job("runs", "fenced")
        except Exception:  # noqa: BLE001 - the one place that says what failed
            # A failing terminal write or ack must surface in the log with
            # this job's name; the message stays pending and redelivery
            # retries the job.
            log.exception(
                "Worker %s: terminal phase for run %s failed; redelivery remains "
                "responsible.",
                self._store.worker_id,
                job.run_id,
            )
            _count_worker_job("runs", "finalization_failed")
        finally:
            telemetry_stack.close()
        return old_message_acked


def _observe_queue_wait(message_id: str) -> None:
    """Queue-wait histogram from the Valkey stream id.

    Stream ids are server-assigned ``<ms-epoch>-<seq>`` at XADD time, so
    claim-time minus the id's timestamp IS the queue wait — no schema
    change needed. Malformed ids (tests, other queue impls) are skipped.
    """
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is None:
        return
    try:
        enqueued_ms = int(str(message_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return
    if enqueued_ms <= 0:
        return
    metrics.observe_queue_wait(
        seconds=max(0.0, time.time() - enqueued_ms / 1000.0)
    )


def _count_worker_job(loop_name: str, outcome: str) -> None:
    """worker_jobs_total feed — bounded outcome vocabulary."""
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.count_worker_job(loop=loop_name, outcome=outcome)


def _observe_run_segment(*, mode: str, outcome: str, seconds: float) -> None:
    """run_duration histogram feed (per execution SEGMENT — parked runs
    resume as fresh dispatches, so segments are the honest unit)."""
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.observe_run(
            mode=mode or "standard",
            outcome=outcome,
            duration_seconds=seconds,
        )
