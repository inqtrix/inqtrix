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

The claim/heartbeat/reclaim/reconcile/cancel machinery is generic over
the job kind and lives in :class:`BaseWorkerLoop`; the run worker and
the reindex worker (:mod:`inqtrix.worker.indexing_loop`) differ only in
the execution body and a few store/queue method names, supplied via the
subclass hooks. Every lifecycle decision logs a marker (claim, skip,
takeover, dead-letter, cancel observed, fenced write discarded) —
Visibility takes precedence over cleverness.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar

from inqtrix.storage.migration_contract import SchemaHeadMismatch
from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.auth.principal import Principal
from inqtrix.core.results import RunRequest
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.execution_failures import terminate_native_run
from inqtrix.observability.context import (
    bind_log_context,
    reset_log_context,
)
from inqtrix.observability.otel import run_execute_span
from inqtrix.quota.models import QuotaSubject
from inqtrix.server.runs import RunHandle
from inqtrix.services.run_service import execute_run_request

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

log = logging.getLogger("inqtrix")

_CANCEL_POLL_SECONDS = 2.0
_RECONCILE_SECONDS = 60.0
_RECONCILE_MIN_AGE_SECONDS = 120.0
_RECONCILE_COOLDOWN_SECONDS = 600.0
_CLAIM_BLOCK_MS = 5_000
_ERROR_BACKOFF_SECONDS = 5.0

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
    future: Future | None = None
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
        thread_prefix: Executor thread name prefix (for log clarity).
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
        claim_guard: Callable[[], None] | None = None,
        thread_prefix: str = "inqtrix-job",
    ) -> None:
        self._store = store
        self._queue = queue
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._heartbeat_seconds = heartbeat_seconds
        self._claim_idle_seconds = claim_idle_seconds
        self._claim_guard = claim_guard
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix=thread_prefix
        )
        self._active: dict[str, _ActiveJob] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._terminated = threading.Event()
        self._reenqueued: dict[str, float] = {}
        self._last_reclaim = 0.0
        self._last_reconcile = 0.0

    # -- subclass hooks --------------------------------------------------- #

    def _entity_id(self, job: TJob) -> str:
        """The durable id carried by *job* (``run_id`` / ``job_id``)."""
        raise NotImplementedError

    def _execute(
        self, job: TJob, claimed: TClaimed, cancel_event: threading.Event
    ) -> None:
        """Run the claimed job to a terminal state and ack on landing."""
        raise NotImplementedError

    def _stale_dispatch(self) -> list[tuple[str, str]]:
        """``(entity_id, tenant_id)`` rows stuck queued (reconcile feed)."""
        raise NotImplementedError

    def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        """Subset of watched ``{entity_id: tenant_id}`` with a pending cancel."""
        raise NotImplementedError

    def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        """Re-enqueue one dispatch message (reconciler)."""
        raise NotImplementedError

    def _is_successor_dispatch(self, job: TJob) -> bool:
        """Whether *job* is a real queued successor of an active segment.

        Durable reindex jobs never park and therefore keep the historical
        duplicate-ACK behaviour. The run worker overrides this hook and reads
        the authoritative row status so a resume/wake message is not mistaken
        for a reconciler duplicate while the previous segment is unwinding.
        """
        del job
        return False

    def _periodic_maintenance(self) -> None:
        """Run subsystem maintenance on the existing reconcile cadence."""


    # -- lifecycle -------------------------------------------------------- #

    def request_stop(self) -> None:
        """Stop claiming new jobs; in-flight jobs keep executing."""
        log.info(
            "Worker %s: shutdown requested; no new claims.",
            self._store.worker_id,
        )
        self._stop.set()

    def drain(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for in-flight jobs to finish.

        Returns:
            ``True`` when everything finished; ``False`` when jobs are
            still executing — the caller exits anyway and heartbeat
            silence hands those jobs to another worker.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active:
                    self._terminated.set()
                    return True
            time.sleep(0.5)
        with self._lock:
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

    def run_forever(self) -> None:
        """Main loop; returns after :meth:`request_stop`."""
        self._queue.ensure_group()
        if not self._wait_for_claim_contract(immediate=True):
            return
        # Own-PEL drain only finds entries when the consumer name is
        # stable across restarts (container hostname without the pid);
        # with the default boot-unique name, crash recovery runs via
        # the idle-based reclaim instead — this call is then a no-op.
        for job in self._queue.claim_pending():
            while not self._stop.is_set():
                try:
                    self._start(job, takeover=True)
                except WorkerClaimUnavailableError as exc:
                    self._stop.wait(exc.retry_after_seconds)
                    continue
                break

        heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="inqtrix-heartbeat", daemon=True
        )
        cancel_poll = threading.Thread(
            target=self._cancel_loop, name="inqtrix-cancel-poll", daemon=True
        )
        heartbeat.start()
        cancel_poll.start()

        while not self._stop.is_set():
            try:
                self._tick()
            except WorkerClaimUnavailableError as exc:
                self._stop.wait(exc.retry_after_seconds)
            except WorkerClaimGuardError:
                raise
            except SchemaHeadMismatch as exc:
                # The in-transaction fence fired: the claim rolled back and
                # the queue entry stays unacked for an upgraded worker.
                # Latch the shared guard so every loop of this process
                # stops claiming immediately -- detection in one domain is
                # a verdict about the process, not about the job.
                self._latch_claim_guard(str(exc))
                log.error(
                    "Worker %s: schema head changed under this process; no further "
                    "claims. %s",
                    self._store.worker_id,
                    exc,
                )
                raise WorkerClaimGuardError(str(exc)) from exc
            except Exception:  # noqa: BLE001 — survive transient outages
                log.warning(
                    "Worker %s: transient error in the claim loop; retrying in %.0fs.",
                    self._store.worker_id,
                    _ERROR_BACKOFF_SECONDS,
                    exc_info=True,
                )
                self._stop.wait(_ERROR_BACKOFF_SECONDS)

    def _wait_for_claim_contract(self, *, immediate: bool) -> bool:
        """Wait in-process for transient availability; reject fatal drift."""
        while not self._stop.is_set():
            try:
                self._verify_claim_contract(immediate=immediate)
            except WorkerClaimUnavailableError as exc:
                self._stop.wait(exc.retry_after_seconds)
                continue
            return True
        return False

    def _tick(self) -> None:
        """One claim-loop iteration (reclaim, reconcile, claim new)."""
        self._verify_claim_contract()
        now = time.monotonic()
        with self._lock:
            has_capacity = len(self._active) < self._concurrency
        if (
            now - self._last_reclaim >= self._heartbeat_seconds
            and has_capacity
        ):
            # Reclaim only with free capacity: an over-claimed job
            # would sit "running" in the database while parked in this
            # worker's queue, invisible to other reclaimers.
            self._last_reclaim = now
            for job in self._queue.reclaim(
                min_idle_ms=int(self._claim_idle_seconds * 1000),
                count=1,
            ):
                log.warning(
                    "Worker %s is taking over job %s after owner heartbeat silence "
                    "(delivery %d).",
                    self._store.worker_id,
                    self._entity_id(job),
                    job.delivery_count,
                )
                self._start(job, takeover=True)
        if now - self._last_reconcile >= _RECONCILE_SECONDS:
            self._last_reconcile = now
            self._reconcile()
        with self._lock:
            free_slots = self._concurrency - len(self._active)
        if free_slots < 1:
            time.sleep(0.5)
            return
        # Claim as many as this worker can actually run, not one per tick.
        # Every claim costs a queue round trip plus a durable claim write, so
        # filling N free slots one at a time takes N of them in sequence, and
        # a worker sized for many runs would take a long time to reach that
        # number while the work sits queued. The count is bounded by free
        # capacity, so a worker still never holds an entry it cannot start,
        # and other workers keep whatever they have room for.
        for job in self._queue.claim_new(
            block_ms=_CLAIM_BLOCK_MS, count=free_slots
        ):
            self._start(job, takeover=job.delivery_count > 1)

    def _latch_claim_guard(self, message: str) -> None:
        """Stick the shared guard shut after an in-transaction fence hit."""
        guard = self._claim_guard
        latch = getattr(guard, "latch_failure", None)
        if callable(latch):
            latch(message)

    def _verify_claim_contract(self, *, immediate: bool = False) -> None:
        """Fail closed before pending, reclaim, reconcile, or new claims.

        The coalesced call surfaces an already-latched failure and re-probes
        when the interval is due; the hard per-claim protection is the
        in-transaction schema fence (``assert_schema_head``) inside every
        durable-claim transaction. ``immediate=True`` (an uncoalesced probe
        via ``verify_now``) remains only for the startup waits, where no
        claim transaction exists yet to carry the fence. Plain callable
        guards remain supported for tests and embedders.
        """
        guard = self._claim_guard
        if guard is None:
            return
        if immediate:
            verify_now = getattr(guard, "verify_now", None)
            if callable(verify_now):
                verify_now()
                return
        guard()

    def _reconcile(self) -> None:
        """Re-enqueue queued rows whose dispatch message got lost.

        Re-enqueues each entity at most once per cooldown window: under
        sustained backlog every stale-but-healthy queued row would
        otherwise receive one duplicate per worker per minute (the
        duplicates are absorbed by ack-on-duplicate and the guarded
        claim, but flooding the stream helps nobody).
        """
        stale = self._stale_dispatch()
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
            self._enqueue_dispatch(entity_id, tenant_id)
        if len(self._reenqueued) > 1_000:
            cutoff = now - _RECONCILE_COOLDOWN_SECONDS
            self._reenqueued = {
                entity_id: sent
                for entity_id, sent in self._reenqueued.items()
                if sent >= cutoff
            }
        self._periodic_maintenance()

    def _dead_letter_exhausted(self, job: TJob, entity_id: str) -> None:
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
        claimed = self._store.claim_for_execution(
            entity_id, job.tenant_id, allow_takeover=True
        )
        if claimed is not None:
            self._store.fail(
                entity_id,
                "Maximale Anzahl Ausfuehrungsversuche erreicht.",
                error_type="max_retries_exceeded",
                fence_attempt=claimed.attempt,
            )
        self._queue.dead_letter(job, reason="max_attempts_exceeded")

    def _start(self, job: TJob, *, takeover: bool) -> None:
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
        self._verify_claim_contract()
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
            with self._lock:
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
                self._queue.ack(job.message_id)
                return
            # A queued row while the previous segment is still active is not
            # a reconciler duplicate: it is the durable resume/child-wake
            # successor. The status read deliberately happens outside the
            # worker lock. If it fails, the exception propagates and the
            # message remains unacked for redelivery — never degrade an
            # uncertain successor into an ACK.
            if not self._is_successor_dispatch(job):
                log.info(
                    "Worker %s: duplicate dispatch for active job %s acknowledged.",
                    self._store.worker_id,
                    entity_id,
                )
                self._queue.ack(job.message_id)
                return
            with self._lock:
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
            # A successor won the lock between the two reads. Keep the first
            # one and acknowledge this redundant message.
            self._queue.ack(job.message_id)
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
            self._dead_letter_exhausted(job, entity_id)
            return
        claimed = self._store.claim_for_execution(
            entity_id, job.tenant_id, allow_takeover=takeover
        )
        if claimed is None:
            log.info(
                "Worker %s: job %s cannot be claimed because it is terminal, missing or"
                " active on another worker; message acknowledged.",
                self._store.worker_id,
                entity_id,
            )
            self._queue.ack(job.message_id)
            return
        # Only first deliveries of run dispatches: a reclaimed message
        # keeps its ORIGINAL XADD timestamp, so redeliveries would fold
        # the previous attempt's runtime into "queue wait".
        if self._observes_queue_wait and job.delivery_count <= 1:
            _observe_queue_wait(job.message_id)
        # Register BEFORE submitting: a fast job's finally-pop must
        # find the entry, or it would linger in the active set forever.
        cancel_event = threading.Event()
        entry = _ActiveJob(job=job, cancel_event=cancel_event)
        with self._lock:
            self._active[entity_id] = entry
        try:
            entry.future = self._executor.submit(
                self._execute, job, claimed, cancel_event
            )
        except BaseException:
            with self._lock:
                self._active.pop(entity_id, None)
            raise

    def _finish_active(self, job: TJob, *, allow_successor: bool) -> None:
        """Release one execution slot and hand off its held successor.

        A successor is activated only after the old stream entry was ACKed.
        Otherwise the old entry could later be redelivered with takeover
        permission and fence out the newly started segment. The successor is
        kept as an active placeholder during its claim so capacity and drain
        never observe a false empty slot.
        """
        entity_id = self._entity_id(job)
        placeholder: _ActiveJob | None = None
        with self._lock:
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
        self._activate_successor(entity_id, placeholder)

    def _activate_successor(
        self, entity_id: str, placeholder: _ActiveJob
    ) -> None:
        """Claim and submit a pre-registered successor placeholder."""
        job = placeholder.job
        try:
            if self._stop.is_set():
                with self._lock:
                    if self._active.get(entity_id) is placeholder:
                        self._active.pop(entity_id, None)
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
            self._verify_claim_contract()
            if self._stop.is_set():
                with self._lock:
                    if self._active.get(entity_id) is placeholder:
                        self._active.pop(entity_id, None)
                log.info(
                    "Worker %s: follow-up dispatch for job %s stopped during the claim "
                    "check; redelivery remains responsible.",
                    self._store.worker_id,
                    entity_id,
                )
                return
            if job.delivery_count > self._max_attempts:
                self._dead_letter_exhausted(job, entity_id)
                with self._lock:
                    if self._active.get(entity_id) is placeholder:
                        self._active.pop(entity_id, None)
                return
            claimed = self._store.claim_for_execution(
                entity_id,
                job.tenant_id,
                # A successor is a fresh queued segment. A crash during this
                # handoff leaves the message for normal reclaim, whose regular
                # _start call supplies takeover=True later.
                allow_takeover=False,
            )
            if claimed is None:
                self._queue.ack(job.message_id)
                with self._lock:
                    if self._active.get(entity_id) is placeholder:
                        self._active.pop(entity_id, None)
                return
            # The row is RUNNING now. Open the placeholder for the NEXT
            # generation before submitting: ThreadPoolExecutor may start a
            # very fast segment before submit() returns, and that segment can
            # park/enqueue its own successor immediately. Leaving the handoff
            # flag set through submit would misclassify that legitimate next
            # successor as an extra duplicate.
            with self._lock:
                if self._active.get(entity_id) is placeholder:
                    placeholder.handoff_in_progress = False
            future = self._executor.submit(
                self._execute, job, claimed, placeholder.cancel_event
            )
            with self._lock:
                if self._active.get(entity_id) is placeholder:
                    placeholder.future = future
        except SchemaHeadMismatch as exc:
            with self._lock:
                if self._active.get(entity_id) is placeholder:
                    self._active.pop(entity_id, None)
            # The in-transaction fence fired on the successor claim. This
            # path runs on an executor-finally thread, so raising would be
            # unobserved -- latching the shared guard is what stops every
            # loop of this process, exactly as the _tick path does.
            self._latch_claim_guard(str(exc))
            log.error(
                "Worker %s: schema head changed under this process; no further claims, "
                "and the follow-up dispatch for job %s remains unacknowledged. %s",
                self._store.worker_id,
                entity_id,
                exc,
            )
        except BaseException:
            with self._lock:
                if self._active.get(entity_id) is placeholder:
                    self._active.pop(entity_id, None)
            # The successor entry remains unacked. Surface the failure, but do
            # not let an unobserved executor-finally exception disappear.
            log.exception(
                "Worker %s: follow-up dispatch for job %s could not be activated; "
                "redelivery remains responsible.",
                self._store.worker_id,
                entity_id,
            )

    def _heartbeat_message_ids(self) -> list[str]:
        """Snapshot active and held-successor stream ids under one lock."""
        with self._lock:
            message_ids: list[str] = []
            for active in self._active.values():
                message_ids.append(active.job.message_id)
                if active.successor is not None:
                    message_ids.append(active.successor.message_id)
            return message_ids

    def _heartbeat_loop(self) -> None:
        # Gated on _terminated, NOT _stop: heartbeats must continue
        # through the drain window or every draining job would be
        # reclaimed and double-executed by another worker.
        while not self._terminated.wait(self._heartbeat_seconds):
            message_ids = self._heartbeat_message_ids()
            if not message_ids:
                continue
            try:
                self._queue.heartbeat(message_ids)
            except Exception:  # noqa: BLE001 — heartbeat loss must be visible
                log.warning(
                    "Worker %s: heartbeat failed; active jobs may be reclaimed by other"
                    " workers.",
                    self._store.worker_id,
                    exc_info=True,
                )

    def _cancel_loop(self) -> None:
        # Gated on _terminated like the heartbeat: users must still be
        # able to cancel jobs that are draining.
        while not self._terminated.wait(_CANCEL_POLL_SECONDS):
            with self._lock:
                watched = {
                    self._entity_id(active.job): active.job.tenant_id
                    for active in self._active.values()
                    if not active.cancel_event.is_set()
                }
            if not watched:
                continue
            try:
                cancelled = self._cancel_requested(watched)
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
                with self._lock:
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
        claim_guard: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            store=store,
            queue=queue,
            concurrency=concurrency,
            max_attempts=max_attempts,
            heartbeat_seconds=heartbeat_seconds,
            claim_idle_seconds=claim_idle_seconds,
            claim_guard=claim_guard,
            thread_prefix="inqtrix-job",
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

    def _stale_dispatch(self) -> list[tuple[str, str]]:
        return self._store.stale_queued_runs(
            older_than_seconds=_RECONCILE_MIN_AGE_SECONDS
        )

    def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        return self._store.cancel_requested_runs(watched)

    def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        self._queue.enqueue(run_id=entity_id, tenant_id=tenant_id)

    def _is_successor_dispatch(self, job: "QueuedJob") -> bool:
        """Recognize a queued resume/wake behind an unwinding segment."""
        return (
            self._store.dispatch_status(job.run_id, job.tenant_id)
            == "queued"
        )

    def _execute(
        self,
        job: "QueuedJob",
        claimed: "ClaimedRun",
        cancel_event: threading.Event,
    ) -> None:
        handle = FencedRunHandle(
            self._store, job.run_id, cancel_event, claimed.attempt
        )
        # Correlation for the WHOLE segment: every log line in this
        # thread carries run_id/tenant (JSON mode), and the execution
        # span parents itself in the submitter's trace via the
        # traceparent persisted in request_payload. Worker threads are
        # reused — both bindings are undone in the outer finally. The
        # setup lives INSIDE the try so a telemetry failure can never
        # skip _finish_active (which would leak the worker slot / stream
        # message and silently wedge the dispatcher).
        telemetry_stack = ExitStack()
        log_tokens: dict = {}
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
            log_tokens = bind_log_context(
                run_id=job.run_id, tenant=job.tenant_id
            )
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
                    log_tokens.update(
                        bind_log_context(
                            user=stable_pseudonym("usr", actor_user_id)
                        )
                    )
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
                        "inqtrix.agent.activity",
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
                self._queue.ack(job.message_id)
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
        except Exception:  # noqa: BLE001 — Futures here are unobserved
            # A failing terminal write or ack must surface in the log,
            # not vanish inside a never-awaited Future; the message
            # stays pending and redelivery retries the job.
            log.exception(
                "Worker %s: terminal phase for run %s failed; redelivery remains "
                "responsible.",
                self._store.worker_id,
                job.run_id,
            )
            _count_worker_job("runs", "finalization_failed")
        finally:
            reset_log_context(log_tokens)
            _clear_feature_after_segment()
            telemetry_stack.close()
            self._finish_active(
                job, allow_successor=old_message_acked
            )


def _clear_feature_after_segment() -> None:
    """Reused threads must not leak feature label or ledger subject."""
    from inqtrix.observability.context import (
        clear_feature,
        clear_usage_subject,
    )

    clear_feature()
    clear_usage_subject()


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
