"""Upload worker loop: dead-letter, ack parity, and finalization guard.

The stub store pins the REAL ``PostgresUploadOperationStore`` surface —
most importantly ``fail`` with its keyword-only, defaultless
``fence_attempt`` — so the loop's dead-letter path cannot silently drift
back to an unfenced call that would crash against the real store.
"""

from __future__ import annotations

from typing import Any

from inqtrix.runs.upload_operations import UploadAttemptSuperseded
from inqtrix.runs.upload_queue import QueuedUploadOperation
from inqtrix.worker.upload_loop import UploadWorkerLoop


class StubUploadStore:
    """Recording stand-in pinning the real upload-store signatures."""

    def __init__(
        self,
        *,
        claim_result: Any | None,
        attempt_current: bool = False,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.claim_result = claim_result
        self.attempt_current = attempt_current
        self.worker_id = "upload-worker-test"

    def claim_for_execution(self, operation_id, tenant_id, *, allow_takeover):
        self.calls.append(("claim", operation_id, allow_takeover))
        return self.claim_result

    def fail(
        self,
        operation_id: str,
        message: str,
        *,
        tenant_id: str = "default",
        fence_attempt: int,
        error_type: str = "server_error",
        awaiting_bytes: bool = False,
    ) -> bool:
        # Keyword-only fence WITHOUT default — the real store's contract.
        self.calls.append(("fail", operation_id, error_type, fence_attempt))
        return True

    def is_attempt_current(
        self, operation_id, *, tenant_id="default", fence_attempt
    ) -> bool:
        self.calls.append(("is_attempt_current", operation_id, fence_attempt))
        return self.attempt_current

    def cancel_requested_operations(self, operation_ids):
        return set()

    def stale_dispatches(self, *, older_than_seconds):
        return []

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class StubUploadQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def ack(self, message_id):
        self.calls.append(("ack", message_id))

    def dead_letter(self, job, *, reason):
        self.calls.append(("dead_letter", job.operation_id, reason))

    def enqueue(self, *, operation_id, tenant_id):
        self.calls.append(("enqueue", operation_id))

    def heartbeat(self, message_ids):
        return None


class StubUploadService:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.executed: list[str] = []

    def execute_claimed(self, claimed) -> None:
        self.executed.append(claimed.operation_id)
        if self.outcome is not None:
            raise self.outcome


class _Claimed:
    def __init__(self, attempt: int = 3) -> None:
        self.operation_id = "up_w1"
        self.tenant_id = "default"
        self.attempt = attempt
        self.record = None


def _job(delivery_count: int = 1) -> QueuedUploadOperation:
    return QueuedUploadOperation(
        message_id="1-0",
        operation_id="up_w1",
        tenant_id="default",
        delivery_count=delivery_count,
    )


def _loop(store, queue, service) -> UploadWorkerLoop:
    return UploadWorkerLoop(
        store=store,
        queue=queue,
        service=service,
        concurrency=1,
        max_attempts=3,
        heartbeat_seconds=60.0,
        claim_idle_seconds=60.0,
    )


def _run_one(store, queue, service, *, job=None) -> None:
    loop = _loop(store, queue, service)
    loop._start(job or _job(), takeover=False)
    assert loop.drain(timeout=10), "upload job did not finish in time"


def test_exhausted_delivery_budget_claims_then_fails_with_fence():
    """The dead-letter path must satisfy the fenced ``fail`` contract."""
    store = StubUploadStore(claim_result=_Claimed(attempt=4))
    queue = StubUploadQueue()

    loop = _loop(store, queue, StubUploadService())
    loop._start(_job(delivery_count=4), takeover=True)

    assert ("claim", "up_w1", True) in store.calls
    fails = [call for call in store.calls if call[0] == "fail"]
    assert fails == [("fail", "up_w1", "max_retries_exceeded", 4)]
    assert ("dead_letter", "up_w1", "max_attempts_exceeded") in queue.calls


def test_exhausted_budget_on_terminal_row_dead_letters_without_fail():
    store = StubUploadStore(claim_result=None)
    queue = StubUploadQueue()

    loop = _loop(store, queue, StubUploadService())
    loop._start(_job(delivery_count=4), takeover=True)

    assert [call for call in store.calls if call[0] == "fail"] == []
    assert ("dead_letter", "up_w1", "max_attempts_exceeded") in queue.calls


def test_ack_only_after_a_persisted_outcome():
    """A still-current attempt means nothing landed — no ack, redelivery."""
    store = StubUploadStore(
        claim_result=_Claimed(), attempt_current=True
    )
    queue = StubUploadQueue()

    _run_one(store, queue, StubUploadService(RuntimeError("kaputt")))

    assert ("is_attempt_current", "up_w1", 3) in store.calls
    assert [call for call in queue.calls if call[0] == "ack"] == []


def test_visible_failure_with_landed_outcome_acks():
    store = StubUploadStore(
        claim_result=_Claimed(), attempt_current=False
    )
    queue = StubUploadQueue()

    _run_one(store, queue, StubUploadService(RuntimeError("kaputt")))

    assert ("ack", "1-0") in queue.calls


def test_superseded_attempt_never_acks():
    store = StubUploadStore(claim_result=_Claimed())
    queue = StubUploadQueue()

    _run_one(
        store, queue, StubUploadService(UploadAttemptSuperseded("neuer"))
    )

    assert [call for call in queue.calls if call[0] == "ack"] == []


def test_finalization_failure_is_logged_not_swallowed(caplog):
    """An ack exception must leave a loud trace, never an unobserved Future."""

    class _BrokenAckQueue(StubUploadQueue):
        def ack(self, message_id):
            raise RuntimeError("ack kaputt")

    store = StubUploadStore(claim_result=_Claimed(), attempt_current=False)
    queue = _BrokenAckQueue()

    _run_one(store, queue, StubUploadService())

    assert any(
        "terminal phase for upload operation" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    ), "the finalization guard must log the swallowed exception"


class _PreparingService(StubUploadService):
    """Queues missing preparations, or fails doing so."""

    def __init__(self, *, queued=(), failure: Exception | None = None) -> None:
        super().__init__()
        self.queued = list(queued)
        self.failure = failure

    def queue_missing_preparations(self):
        if self.failure is not None:
            raise self.failure
        return list(self.queued)


class _StaleStore(StubUploadStore):
    def stale_dispatches(self, *, older_than_seconds):
        return [("up_stale", "default")]


def test_queued_preparations_are_dispatched_by_the_stale_pass():
    queue = StubUploadQueue()
    loop = _loop(
        _StaleStore(claim_result=None),
        queue,
        _PreparingService(queued=[("up_prep", "default")]),
    )

    # The stale rows go back to the base loop; the queued preparation is
    # dispatched here, once, without the "stuck in queued" resend.
    assert loop._stale_dispatch() == [("up_stale", "default")]
    assert queue.calls == [("enqueue", "up_prep")]


def test_a_failing_preparation_pass_never_blocks_the_stale_pass(caplog):
    loop = _loop(
        _StaleStore(claim_result=None),
        StubUploadQueue(),
        _PreparingService(failure=RuntimeError("database unavailable")),
    )

    with caplog.at_level("WARNING"):
        assert loop._stale_dispatch() == [("up_stale", "default")]
    assert "Missing upload preparations could not be queued" in caplog.text
