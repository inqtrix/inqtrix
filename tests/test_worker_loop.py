"""Offline worker-loop tests: claim guards, fencing, ack ordering.

The loop is exercised job-by-job (``_start``/``_execute``) against
recording stubs — the full resolver/registry path runs with the
contract-suite provider stubs and a monkeypatched graph, so the worker
executes EXACTLY the code path the HTTP in-process runner uses.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pytest

from inqtrix.harness.contracts.binding import RunBindingNewer, WorkflowUnavailable
from inqtrix.logging_config import InqtrixLogFilter
from inqtrix.storage.migration_contract import (
    SCHEMA_HEAD_REVISION,
    SchemaHeadMismatch,
    assert_schema_head,
)
from inqtrix.exceptions import (
    AgentRateLimited,
    AgentTimeout,
    AzureOpenAIAPIError,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.runs.postgres_store import ClaimedRun
from inqtrix.runs.valkey_queue import QueuedJob
from inqtrix.server.container import build_container
from inqtrix.settings import Settings
from inqtrix.storage.runtime_contract import DatabaseRuntimeUnavailableError
from inqtrix.worker.loop import (
    FencedRunHandle,
    WorkerClaimGuardError,
    WorkerClaimUnavailableError,
    WorkerLoop,
)
from inqtrix.worker.__main__ import (
    _DatabaseClaimGuard,
    _verified_database_claim_contract,
    _wait_for_database_claim_contract,
)

from tests._process_loop import run_on_configured_loop as drive
from tests._store_doubles import BothNames
from tests.contract._app import StubLLM, StubSearch, minimal_agent_result


async def _no_sleep(_seconds: float) -> None:
    return None


class StubStore(BothNames):
    """Recording stand-in for the worker-facing store surface."""

    def __init__(
        self,
        *,
        claim_result: ClaimedRun | None,
        timeline: list[str] | None = None,
    ) -> None:
        self.claim_result = claim_result
        self.calls: list[tuple[str, Any]] = []
        self.timeline = timeline if timeline is not None else []
        self.worker_id = "worker-test"
        self.dispatch_status_value: str | None = "running"
        self.dispatch_status_error: BaseException | None = None

    def claim_for_execution(
        self, run_id, tenant_id, *, allow_takeover, workflow_contracts
    ):
        self.calls.append(("claim", run_id, allow_takeover))
        self.workflow_contracts = workflow_contracts
        return self.claim_result

    def end_outdated_waits(self, workflow_contracts):
        self.swept_with = workflow_contracts
        return 0

    def total_elapsed_seconds(self, run_id):
        assert self.claim_result is not None
        assert run_id == self.claim_result.run_id
        return 0.0

    def emit(self, run_id, event_type, payload=None, *, fence_attempt=None):
        self.calls.append(("emit", event_type, payload, fence_attempt))

    terminal_lands = True
    """Scripted outcome of terminal writes — ``False`` simulates a
    fenced-out zombie attempt."""

    def complete(self, run_id, result, *, snapshot=None, fence_attempt=None):
        self.calls.append(("complete", run_id, fence_attempt))
        self.timeline.append("complete")
        return self.terminal_lands

    def fail(self, run_id, message, *, error_type="server_error", fence_attempt=None):
        self.calls.append(("fail", run_id, message, error_type, fence_attempt))
        return self.terminal_lands

    def mark_cancelled(self, run_id, *, reason, fence_attempt=None):
        self.calls.append(("cancelled", run_id, reason, fence_attempt))
        return self.terminal_lands

    def cancel_requested_runs(self, run_ids):
        return set()

    def stale_queued_runs(self, *, older_than_seconds):
        return []

    def dispatch_status(self, run_id, tenant_id):
        self.calls.append(("dispatch_status", run_id, tenant_id))
        if self.dispatch_status_error is not None:
            raise self.dispatch_status_error
        return self.dispatch_status_value

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class StubQueue:
    """Recording stand-in for the queue surface the loop touches."""

    def __init__(self, timeline: list[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.timeline = timeline if timeline is not None else []

    async def ack(self, message_id):
        self.calls.append(("ack", message_id))
        self.timeline.append("ack")

    async def dead_letter(self, job, *, reason):
        self.calls.append(("dead_letter", job.run_id, reason))

    async def enqueue(self, *, run_id, tenant_id):
        self.calls.append(("enqueue", run_id))

    async def heartbeat(self, message_ids):
        self.calls.append(("heartbeat", tuple(message_ids)))


def make_loop(
    store,
    queue,
    monkeypatch,
    *,
    claim_guard=None,
    answer_publisher=None,
    concurrency=1,
) -> WorkerLoop:
    def fake_graph(question, **kwargs):
        return minimal_agent_result()

    monkeypatch.setattr("inqtrix.research.web_research.run_web_graph", fake_graph)
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    return WorkerLoop(
        store=store,
        queue=queue,
        resolver=container.resolver,
        registry=container.registry,
        runtime=container.runtime,
        concurrency=concurrency,
        max_attempts=3,
        heartbeat_seconds=15,
        claim_idle_seconds=90,
        answer_publisher=answer_publisher,
        claim_guard=claim_guard,
    )


def test_claim_guard_fails_before_pending_or_new_queue_claims(monkeypatch):
    store = StubStore(claim_result=None)

    class GuardedQueue(StubQueue):
        async def ensure_group(self):
            self.calls.append(("ensure_group",))

        async def claim_pending(self):
            raise AssertionError("pending claims must remain closed")

    queue = GuardedQueue()

    async def reject_claims() -> None:
        raise WorkerClaimGuardError("database schema changed")

    loop = make_loop(store, queue, monkeypatch, claim_guard=reject_claims)

    with pytest.raises(WorkerClaimGuardError, match="schema changed"):
        drive(loop.run_forever())

    assert queue.calls == [("ensure_group",)]
    assert store.calls == []


def test_database_claim_guard_coalesces_and_latches_failures(monkeypatch):
    calls: list[str] = []

    async def fail_contract(database_url, *, app_role, login_policy):
        calls.append(database_url)
        raise RuntimeError("unsafe database role")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_url_runtime_contract",
        fail_contract,
    )
    guard = _DatabaseClaimGuard(
        database_url="postgresql+asyncpg://runtime.invalid/inqtrix",
        app_role="inqtrix_app",
        login_policy="restricted",
        interval_seconds=60,
    )

    with pytest.raises(WorkerClaimGuardError, match="unsafe database role"):
        drive(guard())
    with pytest.raises(WorkerClaimGuardError, match="unsafe database role"):
        drive(guard())

    assert calls == ["postgresql+asyncpg://runtime.invalid/inqtrix"]


def test_database_claim_guard_coalesces_transient_outage_and_recovers(
    monkeypatch,
    caplog,
):
    now = [100.0]
    calls: list[str] = []

    async def recover_contract(database_url, *, app_role, login_policy):
        del app_role, login_policy
        calls.append(database_url)
        if len(calls) == 1:
            raise DatabaseRuntimeUnavailableError(
                "database temporarily unavailable"
            )

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_url_runtime_contract",
        recover_contract,
    )
    monkeypatch.setattr(
        "inqtrix.worker.__main__.time.monotonic",
        lambda: now[0],
    )
    caplog.set_level(logging.INFO, logger="inqtrix")
    guard = _DatabaseClaimGuard(
        database_url="postgresql+asyncpg://runtime.invalid/inqtrix",
        app_role="inqtrix_app",
        login_policy="restricted",
        interval_seconds=5,
    )

    with pytest.raises(Exception) as first:
        drive(guard())
    assert isinstance(first.value, WorkerClaimUnavailableError)

    with pytest.raises(Exception) as cached:
        drive(guard.verify_now())
    assert isinstance(cached.value, WorkerClaimUnavailableError)
    assert calls == ["postgresql+asyncpg://runtime.invalid/inqtrix"]

    now[0] += 5
    drive(guard())
    drive(guard())

    assert calls == [
        "postgresql+asyncpg://runtime.invalid/inqtrix",
        "postgresql+asyncpg://runtime.invalid/inqtrix",
    ]
    events = [getattr(record, "event", None) for record in caplog.records]
    assert events.count("worker.database_contract_unavailable") == 1
    assert events.count("worker.database_contract_recovered") == 1


def test_database_claim_guard_probes_slowly_when_healthy_and_quickly_after_an_outage(
    monkeypatch,
):
    """A healthy contract is re-proved once a minute, a lost one every 5 s.

    Every probe opens its own connection and reads thousands of catalog
    rows, so re-proving a healthy contract every few seconds was most of
    an idle worker's database traffic. Per-claim safety does not depend on
    the cadence (the schema fence runs inside every claim transaction); a
    database outage, however, must still release claims within seconds.
    """
    now = [100.0]
    outcomes: list[str] = []
    calls: list[float] = []

    async def contract(database_url, *, app_role, login_policy):
        del database_url, app_role, login_policy
        calls.append(now[0])
        if outcomes and outcomes.pop(0) == "unavailable":
            raise DatabaseRuntimeUnavailableError("database temporarily unavailable")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_url_runtime_contract",
        contract,
    )
    monkeypatch.setattr("inqtrix.worker.__main__.time.monotonic", lambda: now[0])
    guard = _DatabaseClaimGuard(
        database_url="postgresql+asyncpg://runtime.invalid/inqtrix",
        app_role="inqtrix_app",
        login_policy="restricted",
    )

    drive(guard())
    for moment in (110.0, 159.0):
        now[0] = moment
        drive(guard())
    assert calls == [100.0]

    now[0] = 160.0
    outcomes.append("unavailable")
    with pytest.raises(WorkerClaimUnavailableError):
        drive(guard())
    now[0] = 164.0
    with pytest.raises(WorkerClaimUnavailableError):
        drive(guard())
    assert calls == [100.0, 160.0]

    now[0] = 165.0
    drive(guard())
    assert calls == [100.0, 160.0, 165.0]


def test_worker_bootstrap_waits_for_transient_contract_recovery() -> None:
    calls = 0
    sleeps: list[float] = []

    async def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkerClaimUnavailableError(
                "database temporarily unavailable",
                retry_after_seconds=0.25,
            )

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    drive(_wait_for_database_claim_contract(guard, sleep=record_sleep))

    assert calls == 2
    assert sleeps == [0.25]


def test_the_bootstrap_failure_names_the_cause_that_occurred() -> None:
    """A dying process leaves one log line; it must be the true one.

    A message that asserted a schema-head hypothesis and suppressed the
    exception that occurred would make a role that cannot log in read as
    a pending migration. Both halves matter: the cause stands IN the
    message, because a chained traceback is not what an orchestrator
    surfaces, and it stands BEHIND it as ``__cause__``, because that is
    what a developer reads.
    """

    async def guard() -> None:
        raise WorkerClaimGuardError(
            "connection to postgresql://app:hunter2@db/inqtrix failed: "
            "password authentication failed"
        )

    with pytest.raises(RuntimeError) as failure:
        drive(_verified_database_claim_contract(guard, sleep=_no_sleep))

    assert "password authentication failed" in str(failure.value)
    assert isinstance(failure.value.__cause__, WorkerClaimGuardError)
    # The hypothesis is not asserted as the diagnosis.
    assert "schema head" not in str(failure.value)
    # And the cause is scrubbed on its way into the message.
    assert "hunter2" not in str(failure.value)


def test_the_bootstrap_still_waits_out_a_transient_outage() -> None:
    """Naming the cause must not turn a retryable outage into a fatality."""
    calls = 0

    async def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkerClaimUnavailableError(
                "database temporarily unavailable", retry_after_seconds=0.25
            )

    drive(_verified_database_claim_contract(guard, sleep=_no_sleep))
    assert calls == 2


def test_worker_bootstrap_keeps_permanent_contract_failure_fatal() -> None:
    sleeps: list[float] = []

    async def guard() -> None:
        raise WorkerClaimGuardError("unsafe database role")

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(WorkerClaimGuardError, match="unsafe database role"):
        drive(_wait_for_database_claim_contract(guard, sleep=record_sleep))

    assert sleeps == []


def test_worker_loop_waits_for_transient_startup_contract(
    monkeypatch,
) -> None:
    store = StubStore(claim_result=None)

    class RecoveringGuard:
        def __init__(self) -> None:
            self.calls = 0

        async def verify_now(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise WorkerClaimUnavailableError(
                    "database temporarily unavailable",
                    retry_after_seconds=0,
                )

        async def __call__(self) -> None:
            return None

    class StartupQueue(StubQueue):
        loop: WorkerLoop | None = None

        async def ensure_group(self):
            self.calls.append(("ensure_group",))

        async def claim_pending(self):
            self.calls.append(("claim_pending",))
            assert self.loop is not None
            self.loop.request_stop()
            return []

    guard = RecoveringGuard()
    queue = StartupQueue()
    loop = make_loop(store, queue, monkeypatch, claim_guard=guard)
    queue.loop = loop

    drive(loop.run_forever())

    assert guard.calls == 2
    assert queue.calls == [("ensure_group",), ("claim_pending",)]
    assert store.calls == []


def test_transient_claim_guard_pauses_every_claim_path(
    monkeypatch,
) -> None:
    store = StubStore(claim_result=claimed())
    queue = StubQueue()

    async def pause_claims() -> None:
        raise WorkerClaimUnavailableError(
            "database temporarily unavailable",
            retry_after_seconds=5,
        )

    loop = make_loop(store, queue, monkeypatch, claim_guard=pause_claims)

    with pytest.raises(WorkerClaimUnavailableError):
        drive(loop._tick())

    assert store.calls == []
    assert queue.calls == []


def test_worker_loop_survives_transient_contract_failure_after_startup(
    monkeypatch,
) -> None:
    store = StubStore(claim_result=None)

    class StartupQueue(StubQueue):
        async def ensure_group(self):
            self.calls.append(("ensure_group",))

        async def claim_pending(self):
            self.calls.append(("claim_pending",))
            return []

    queue = StartupQueue()
    loop = make_loop(store, queue, monkeypatch)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise WorkerClaimUnavailableError(
                "database temporarily unavailable",
                retry_after_seconds=0,
            )
        loop.request_stop()

    monkeypatch.setattr(loop, "_tick", tick)

    drive(loop.run_forever())

    assert ticks == 2
    assert queue.calls == [("ensure_group",), ("claim_pending",)]
    assert store.calls == []


def test_database_claim_guard_immediate_probe_bypasses_success_cache(
    monkeypatch,
):
    calls: list[str] = []

    async def pass_contract(database_url, *, app_role, login_policy):
        del app_role, login_policy
        calls.append(database_url)

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_url_runtime_contract",
        pass_contract,
    )
    guard = _DatabaseClaimGuard(
        database_url="postgresql+asyncpg://runtime.invalid/inqtrix",
        app_role="inqtrix_app",
        login_policy="restricted",
        interval_seconds=60,
    )

    drive(guard())
    drive(guard())
    drive(guard.verify_now())

    assert calls == [
        "postgresql+asyncpg://runtime.invalid/inqtrix",
        "postgresql+asyncpg://runtime.invalid/inqtrix",
    ]


def _error_lines_as_logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The ERROR lines after the log filter, which shows an exception by its type."""
    log_filter = InqtrixLogFilter(service="worker")
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    for record in errors:
        log_filter.filter(record)
    return [record.getMessage() for record in errors]


@pytest.mark.parametrize(
    ("stored", "named"),
    [("0079_other_head", "0079_other_head"), (None, "missing")],
    ids=["other_head", "no_row"],
)
def test_schema_fence_names_the_installed_and_the_expected_revision(stored, named):
    class Result:
        def scalar(self):
            return stored

    class Session:
        async def execute(self, statement):
            return Result()

    with pytest.raises(SchemaHeadMismatch) as raised:
        asyncio.run(assert_schema_head(Session()))

    assert raised.value.installed == named
    assert raised.value.expected == SCHEMA_HEAD_REVISION


def test_schema_fence_after_blocking_read_latches_and_never_acks(monkeypatch, caplog):
    """The staleness window behind the blocking read is fenced IN the
    claim transaction, not by a forced pre-probe.

    A schema head that moved while ``claim_new`` blocked surfaces as
    SchemaHeadMismatch from the store's own claim write. The loop must
    latch the shared guard (one domain's detection is a verdict about the
    whole process), escalate as a guard failure, and never ack the entry
    -- it belongs to an upgraded worker.
    """

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            self.calls.append(("claim_blocked", run_id))
            raise SchemaHeadMismatch(installed="0079", expected="0078")

    class OneJobQueue(StubQueue):
        loop: WorkerLoop | None = None

        def __init__(self) -> None:
            super().__init__()
            self.claims = 0

        async def ensure_group(self):
            self.calls.append(("ensure_group",))

        async def claim_pending(self):
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            return []

        async def claim_new(self, *, block_ms, count=1):
            assert block_ms > 0 and count >= 1
            self.claims += 1
            if self.claims > 3:
                # Regression guard: without the SchemaHeadMismatch clause
                # the loop would treat the fence hit as a transient outage
                # and retry forever on this endless queue. Stop it so the
                # test FAILS (DID NOT RAISE) instead of hanging the suite.
                assert self.loop is not None
                self.loop.request_stop()
                return []
            return [job()]

    class Guard:
        def __init__(self) -> None:
            self.latched: list[str] = []

        async def __call__(self) -> None:
            return None

        def latch_failure(self, message: str) -> None:
            self.latched.append(message)

    guard = Guard()
    store = FencedStore(claim_result=None)
    queue = OneJobQueue()
    loop = make_loop(store, queue, monkeypatch, claim_guard=guard)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")
    queue.loop = loop
    # Keep the bounded regression path fast: the transient-outage handler
    # sleeps this long between retries before the queue stops the loop.
    monkeypatch.setattr("inqtrix.worker.loop._ERROR_BACKOFF_SECONDS", 0.01)

    with pytest.raises(WorkerClaimGuardError, match="installed 0079, expected 0078"):
        drive(loop.run_forever())

    assert guard.latched, "the fence hit must stick the shared guard shut"
    assert _error_lines_as_logged(caplog) == [
        "Worker worker-test: schema head changed under this process; no further "
        "claims (installed=0079, expected=0078)."
    ]
    assert ("ack", job().message_id) not in queue.calls, (
        "the entry belongs to an upgraded worker; acking it loses the job"
    )


def test_schema_fence_on_successor_claim_latches_the_shared_guard(monkeypatch, caplog):
    """The successor-activation path must honour the same process verdict.

    _activate_successor runs on an executor-finally thread where raising
    would be unobserved; the invariant is that a fence hit there still
    latches the shared guard so every loop stops claiming -- not that it
    silently becomes a generic redelivery warning.
    """

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            self.calls.append(("claim_blocked", run_id))
            raise SchemaHeadMismatch(installed="0079", expected="0078")

    class Guard:
        def __init__(self) -> None:
            self.latched: list[str] = []

        async def __call__(self) -> None:
            return None

        def latch_failure(self, message: str) -> None:
            self.latched.append(message)

    guard = Guard()
    store = FencedStore(claim_result=None)
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch, claim_guard=guard)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )

    drive(loop._finish_active(old, allow_successor=True))

    assert guard.latched, (
        "a fence hit on the successor claim is a verdict about the process "
        "and must stick the shared guard shut"
    )
    assert _error_lines_as_logged(caplog) == [
        "Worker worker-test: schema head changed under this process; no further claims, "
        f"and the follow-up dispatch for job {successor.run_id} remains unacknowledged "
        "(installed=0079, expected=0078)."
    ]
    assert ("ack", successor.message_id) not in queue.calls, (
        "the successor entry belongs to an upgraded worker"
    )
    assert old.run_id not in loop._active, (
        "the placeholder must not survive a failed activation"
    )


def _lines_as_logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The loop's lines of one level after the log filter, which shows an exception by its type."""
    log_filter = InqtrixLogFilter(service="worker")
    records = [
        record
        for record in caplog.records
        if record.levelno == level and record.name == "inqtrix.worker.loop"
    ]
    for record in records:
        log_filter.filter(record)
    return [record.getMessage() for record in records]


_BINDING_FENCES = [
    pytest.param(
        lambda: RunBindingNewer(
            run_id="run_w1", mode="agent_kernel", bound_contract=3, worker_contract=2
        ),
        logging.ERROR,
        "Worker worker-test: run run_w1 is bound to agent_kernel contract 3, newer "
        "than the 2 this process carries; this worker claims no further runs until "
        "that run is cancelled, ends or parks, its other jobs go on{held}.",
        id="newer",
    ),
    pytest.param(
        lambda: WorkflowUnavailable(
            run_id="run_w1", mode="knowledge", registered=("research", "direct_llm")
        ),
        logging.WARNING,
        "Worker worker-test: run run_w1 belongs to mode knowledge, which this process "
        "has not registered (registered: research, direct_llm); this worker claims no "
        "further runs until that run is cancelled, ends or parks, its other jobs go on, "
        "and the run and its checkpoint stay{held}.",
        id="unavailable",
    ),
]


def test_the_run_worker_claims_with_the_contracts_its_registry_carries(monkeypatch):
    store = StubStore(claim_result=None)
    loop = make_loop(store, StubQueue(), monkeypatch)

    drive(loop._start(job(), takeover=False))

    assert store.workflow_contracts == loop._registry.workflow_contracts()
    assert store.workflow_contracts["research"] == 1


def test_the_run_worker_ends_outdated_waits_with_the_contracts_its_registry_carries(
    monkeypatch,
):
    store = StubStore(claim_result=None)
    loop = make_loop(store, StubQueue(), monkeypatch)

    drive(loop._periodic_maintenance())

    assert store.swept_with == loop._registry.workflow_contracts()


@pytest.mark.parametrize(("fence", "level", "line"), _BINDING_FENCES)
def test_a_binding_fence_stops_only_the_run_claims_and_never_acks(
    monkeypatch, caplog, fence, level, line
):
    """A run this process must not execute stops the run claims; the other job kinds go on.

    The verdict is about the run's mode or contract, so the shared guard of all
    loops stays open, and the loop claims nothing more while its active
    entries finish.
    """

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            raise fence()

    class OneJobQueue(StubQueue):
        def __init__(self) -> None:
            super().__init__()
            self.claims = 0

        async def ensure_group(self):
            return None

        async def claim_pending(self):
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            return []

        async def claim_new(self, *, block_ms, count=1):
            self.claims += 1
            return [job()] if self.claims == 1 else []

    class Guard:
        loop: WorkerLoop | None = None

        def __init__(self) -> None:
            self.latched: list[str] = []
            self.ticks = 0

        async def __call__(self) -> None:
            self.ticks += 1
            if self.ticks > 4:
                assert self.loop is not None
                self.loop.request_stop()

        def latch_failure(self, message: str) -> None:
            self.latched.append(message)

    guard = Guard()
    queue = OneJobQueue()
    loop = make_loop(FencedStore(claim_result=None), queue, monkeypatch, claim_guard=guard)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")
    guard.loop = loop
    monkeypatch.setattr("inqtrix.worker.loop._CLAIMS_STOPPED_IDLE_SECONDS", 0.01)

    drive(loop.run_forever())

    assert guard.latched == []
    assert queue.claims == 1, "after the fence the loop claims nothing more"
    assert _lines_as_logged(caplog, level) == [line.format(held="")]
    assert ("ack", job().message_id) not in queue.calls


@pytest.mark.parametrize(("fence", "level", "line"), _BINDING_FENCES)
def test_the_other_entries_of_a_batch_still_start_after_a_binding_fence(
    monkeypatch, caplog, fence, level, line
):
    """Each entry's claim checks its own binding; a healthy one of the same batch starts."""
    tried: list[str] = []

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            tried.append(run_id)
            if run_id == "run_w1":
                raise fence()
            return None

    class BatchQueue(StubQueue):
        async def claim_new(self, *, block_ms, count=1):
            return [
                job(),
                QueuedJob(message_id="3-0", run_id="run_ok", tenant_id="default", delivery_count=1),
            ]

    queue = BatchQueue()
    loop = make_loop(FencedStore(claim_result=None), queue, monkeypatch, concurrency=2)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")

    drive(loop._tick())

    assert tried == ["run_w1", "run_ok"]
    assert loop._claims_blocked_by == {"run_w1": "default"}


@pytest.mark.parametrize(("fence", "level", "line"), _BINDING_FENCES)
def test_a_run_this_process_cannot_judge_is_never_failed_on_its_last_delivery(
    monkeypatch, fence, level, line
):
    """Past the delivery budget, a run is failed only by a process allowed to judge it."""

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            raise fence()

    store = FencedStore(claim_result=None)
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)

    with pytest.raises((RunBindingNewer, WorkflowUnavailable)):
        drive(loop._start(job(delivery_count=4), takeover=True))

    assert "fail" not in store.names()
    assert not [call for call in queue.calls if call[0] in {"dead_letter", "ack"}]


@pytest.mark.parametrize(("fence", "level", "line"), _BINDING_FENCES)
def test_a_binding_fence_while_draining_own_entries_at_start_stops_the_run_claims(
    monkeypatch, caplog, fence, level, line
):
    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            raise fence()

    class OwnEntryQueue(StubQueue):
        def __init__(self) -> None:
            super().__init__()
            self.claims = 0

        async def ensure_group(self):
            return None

        async def claim_pending(self):
            return [job()]

        async def claim_new(self, *, block_ms, count=1):
            self.claims += 1
            return []

    class Guard:
        loop: WorkerLoop | None = None

        def __init__(self) -> None:
            self.latched: list[str] = []
            self.ticks = 0

        async def __call__(self) -> None:
            self.ticks += 1
            if self.ticks > 4:
                assert self.loop is not None
                self.loop.request_stop()

        async def verify_now(self) -> None:
            return None

        def latch_failure(self, message: str) -> None:
            self.latched.append(message)

    guard = Guard()
    queue = OwnEntryQueue()
    loop = make_loop(FencedStore(claim_result=None), queue, monkeypatch, claim_guard=guard)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")
    guard.loop = loop
    monkeypatch.setattr("inqtrix.worker.loop._CLAIMS_STOPPED_IDLE_SECONDS", 0.01)

    drive(loop.run_forever())

    assert guard.latched == []
    assert queue.claims == 0, "after the fence the loop claims nothing more"
    assert _lines_as_logged(caplog, level) == [line.format(held="")]
    assert ("ack", job().message_id) not in queue.calls


@pytest.mark.parametrize(("fence", "level", "line"), _BINDING_FENCES)
def test_a_binding_fence_on_a_successor_claim_stops_the_run_claims(
    monkeypatch, caplog, fence, level, line
):
    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            raise fence()

    class Guard:
        def __init__(self) -> None:
            self.latched: list[str] = []

        async def __call__(self) -> None:
            return None

        def latch_failure(self, message: str) -> None:
            self.latched.append(message)

    guard = Guard()
    queue = StubQueue()
    loop = make_loop(FencedStore(claim_result=None), queue, monkeypatch, claim_guard=guard)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(
        job=old, cancel_event=threading.Event(), successor=successor
    )

    drive(loop._finish_active(old, allow_successor=True))

    assert guard.latched == []
    assert loop._claims_blocked_by == {"run_w1": "default"}
    assert _lines_as_logged(caplog, level) == [
        line.format(
            held=f"; the follow-up dispatch for job {successor.run_id} "
            "remains unacknowledged"
        )
    ]
    assert ("ack", successor.message_id) not in queue.calls
    assert old.run_id not in loop._active


def test_stop_during_contract_probe_never_mutates_or_claims(monkeypatch):
    store = StubStore(claim_result=claimed())
    queue = StubQueue()

    class Guard:
        loop: WorkerLoop | None = None

        async def __call__(self) -> None:
            # The pre-claim check is the coalesced guard now -- the hard
            # fence lives inside the claim transaction itself. A stop
            # arriving during this check must still prevent any claim.
            assert self.loop is not None
            self.loop.request_stop()

    guard = Guard()
    loop = make_loop(store, queue, monkeypatch, claim_guard=guard)
    guard.loop = loop

    drive(loop._start(job(delivery_count=99), takeover=False))

    assert store.calls == []
    assert queue.calls == []


def test_blocking_queue_return_after_stop_never_claims(monkeypatch):
    store = StubStore(claim_result=claimed())

    class StoppingQueue(StubQueue):
        loop: WorkerLoop | None = None

        async def claim_new(self, *, block_ms, count=1):
            assert block_ms > 0
            assert count >= 1
            assert self.loop is not None
            self.loop.request_stop()
            return [job()]

    queue = StoppingQueue()
    loop = make_loop(store, queue, monkeypatch)
    queue.loop = loop
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")

    drive(loop._tick())

    assert store.calls == []
    assert queue.calls == []


def test_held_successor_rechecks_contract_before_database_claim(monkeypatch):
    store = StubStore(claim_result=claimed())
    queue = StubQueue()

    class Guard:
        async def __call__(self) -> None:
            # A latched guard surfaces here cheaply; the hard fence sits in
            # the claim transaction. Either way: no successor claim.
            raise WorkerClaimGuardError("worker revision is stale")

    loop = make_loop(store, queue, monkeypatch, claim_guard=Guard())
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )

    drive(loop._finish_active(old, allow_successor=True))

    assert "claim" not in store.names()
    assert queue.calls == []
    assert old.run_id not in loop._active


def test_held_successor_is_left_for_redelivery_during_shutdown(monkeypatch):
    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )
    loop.request_stop()

    drive(loop._finish_active(old, allow_successor=True))

    assert "claim" not in store.names()
    assert queue.calls == []
    assert old.run_id not in loop._active


def test_stop_during_successor_contract_probe_never_mutates_or_claims(
    monkeypatch,
) -> None:
    store = StubStore(claim_result=claimed())
    queue = StubQueue()

    class Guard:
        loop: WorkerLoop | None = None

        async def __call__(self) -> None:
            # Coalesced pre-check; the hard fence is in the claim itself.
            assert self.loop is not None
            self.loop.request_stop()

    guard = Guard()
    loop = make_loop(store, queue, monkeypatch, claim_guard=guard)
    guard.loop = loop
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    successor = QueuedJob(
        message_id=successor.message_id,
        run_id=successor.run_id,
        tenant_id=successor.tenant_id,
        delivery_count=99,
    )
    loop._active[old.run_id] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )

    drive(loop._finish_active(old, allow_successor=True))

    assert store.calls == []
    assert queue.calls == []
    assert old.run_id not in loop._active


@pytest.mark.parametrize(
    ("status", "cancel", "released"),
    [
        pytest.param("queued", False, False, id="still-queued"),
        pytest.param("running", False, False, id="running-elsewhere-or-orphaned"),
        pytest.param("running", True, True, id="cancel-requested"),
        pytest.param("cancelled", False, True, id="ended"),
        pytest.param("waiting_for_approval", False, True, id="parked"),
        pytest.param(None, False, True, id="gone"),
    ],
)
def test_the_claims_come_back_once_the_blocking_run_no_longer_waits_for_a_worker(
    monkeypatch, caplog, status, cancel, released
):
    """The stop lasts as long as its cause; a run that may still need a worker keeps it."""

    class FencedStore(StubStore):
        def claim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            raise RunBindingNewer(
                run_id=run_id, mode="agent_kernel", bound_contract=3, worker_contract=2
            )

        def cancel_requested_runs(self, run_ids):
            return {"run_w1"} if cancel else set()

    class OneThenEmptyQueue(StubQueue):
        def __init__(self) -> None:
            super().__init__()
            self.claims = 0

        async def claim_new(self, *, block_ms, count=1):
            self.claims += 1
            return [job()] if self.claims == 1 else []

    store = FencedStore(claim_result=None)
    store.dispatch_status_value = status
    queue = OneThenEmptyQueue()
    loop = make_loop(store, queue, monkeypatch)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")
    monkeypatch.setattr("inqtrix.worker.loop._CLAIMS_STOPPED_IDLE_SECONDS", 0.0)

    drive(loop._tick())
    assert loop._claims_blocked_by == {"run_w1": "default"}
    with caplog.at_level(logging.INFO, logger="inqtrix.worker.loop"):
        drive(loop._tick())

    assert (loop._claims_blocked_by == {}) is released
    assert queue.claims == (2 if released else 1)
    resumed = [
        record.getMessage()
        for record in caplog.records
        if record.name == "inqtrix.worker.loop" and record.levelno == logging.INFO
    ]
    assert (resumed == [
        "Worker worker-test: run run_w1 no longer holds this worker's claims "
        f"({'cancel requested' if cancel else status or 'gone'}).",
        "Worker worker-test claims runs again.",
    ]) is released


def test_the_claims_stay_stopped_while_one_of_two_blocking_runs_still_waits(monkeypatch):
    store = StubStore(claim_result=None)
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    loop._claims_blocked_by = {"run_a": "default", "run_b": "default"}
    statuses = {"run_a": "cancelled", "run_b": "queued"}
    monkeypatch.setattr(
        store, "dispatch_status", lambda run_id, tenant_id: statuses[run_id]
    )

    drive(loop._release_ended_blocks())

    assert loop._claims_blocked_by == {"run_b": "default"}


def job(delivery_count: int = 1) -> QueuedJob:
    return QueuedJob(
        message_id="1-0",
        run_id="run_w1",
        tenant_id="default",
        delivery_count=delivery_count,
    )


def successor_job(message_id: str = "2-0") -> QueuedJob:
    return QueuedJob(
        message_id=message_id,
        run_id="run_w1",
        tenant_id="default",
        delivery_count=1,
    )


def claimed(
    payload: dict | None = None,
    *,
    workspace_id: str | None = None,
    kind: str = "standard",
) -> ClaimedRun:
    return ClaimedRun(
        run_id="run_w1",
        tenant_id="default",
        attempt=1,
        request_payload=(
            payload
            if payload is not None
            else {
                "question": "Wie ist die Haftung geregelt?",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                },
            }
        ),
        workspace_id=workspace_id,
        kind=kind,
    )


def start_and_drain(loop: WorkerLoop, the_job: QueuedJob, *, takeover: bool) -> None:
    async def scenario() -> bool:
        await loop._start(the_job, takeover=takeover)
        return await loop.drain(timeout=10)

    assert drive(scenario()), "job did not finish in time"


def run_one(store, queue, monkeypatch, *, the_job=None) -> WorkerLoop:
    loop = make_loop(store, queue, monkeypatch)
    start_and_drain(loop, the_job or job(), takeover=False)
    return loop


def test_successful_execution_completes_then_acks(monkeypatch):
    timeline: list[str] = []
    store = StubStore(claim_result=claimed(), timeline=timeline)
    queue = StubQueue(timeline=timeline)

    run_one(store, queue, monkeypatch)

    assert ("claim", "run_w1", False) in store.calls
    assert ("complete", "run_w1", 1) in store.calls
    assert queue.calls == [("ack", "1-0")]
    # Terminal write precedes the ack (at-least-once ordering) — the
    # shared timeline makes the cross-object order assertable.
    assert timeline == ["complete", "ack"]


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (
            AgentRateLimited("model-a", RuntimeError("429")),
            "rate_limited",
        ),
        (AgentTimeout("deadline"), "run_timeout"),
        (ConnectionError("reset"), "temporary_transport"),
        (
            AzureOpenAIAPIError(
                model="deployment", status_code=503, message="unavailable"
            ),
            "upstream_5xx",
        ),
    ],
    ids=["rate-limit", "timeout", "transport", "upstream-5xx"],
)
def test_worker_persists_stable_execution_failure_type(monkeypatch, exc, error_type):
    def fail_execution(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fail_execution)
    store = StubStore(claim_result=claimed())
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    failure = next(call for call in store.calls if call[0] == "fail")
    assert failure[3] == error_type
    assert queue.calls == [("ack", "1-0")]


def test_worker_rehydrates_source_policy(monkeypatch):
    """Durable replay reconstructs the exact admitted Agent Desk policy."""
    captured = []

    def fake_execute(handle, *, run_request, **kwargs):
        captured.append((run_request, kwargs))
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Nur diese Nachricht im Web suchen.",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "source_policy": {
                        "web": "disabled",
                        "knowledge": "available",
                    },
                    "web_recency": "year",
                },
            },
            workspace_id="ws-agent",
        )
    )
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert len(captured) == 1
    assert captured[0][0].source_policy.model_dump() == {
        "web": "disabled",
        "knowledge": "available",
    }
    assert captured[0][0].web_recency == "year"
    assert captured[0][1]["workspace_id"] == "ws-agent"


def test_worker_rehydrates_the_interface_language(monkeypatch):
    captured = []

    def fake_execute(handle, *, run_request, **kwargs):
        captured.append(run_request)
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Was verlangt Measure 7.6 im Code of Practice?",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "ui_language": "de",
                },
            },
            workspace_id="ws-knowledge",
        )
    )

    run_one(store, StubQueue(), monkeypatch)

    assert [request.ui_language for request in captured] == ["de"]


def test_worker_passes_the_shared_answer_publisher_to_execution(monkeypatch):
    """Queue replay uses the same central publication contract as the API."""
    captured: list[Any] = []
    publisher = object()

    def fake_execute(handle, **kwargs):
        captured.append(kwargs.get("answer_publisher"))
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(
        store,
        queue,
        monkeypatch,
        answer_publisher=publisher,
    )

    start_and_drain(loop, job(), takeover=False)

    assert captured == [publisher]


def test_worker_ignores_legacy_child_budget_and_emits_stable_notice(
    monkeypatch,
):
    """Pre-migration child caps never narrow the operator run limit."""
    captured: list[dict[str, Any]] = []

    def fake_execute(handle, **kwargs):
        captured.append(kwargs)
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Legacy child",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "token_budget": 1800,
                    "parent_task_id": "task-legacy",
                },
            },
            kind="agent_child",
        )
    )
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert captured[0]["token_budget"] is None
    notices = [
        call
        for call in store.calls
        if call[0] == "emit" and call[1] == "inqtrix.agent.activity"
    ]
    assert len(notices) == 1
    assert notices[0][2] == {
        "activity_id": "legacy-child-budget:run_w1",
        "scope": "task",
        "phase": "execution",
        "operation": "task.legacy_budget_ignored",
        "detail": "Legacy task budget ignored",
        "status": "completed",
        "task_id": "task-legacy",
        "fallback": True,
    }


def test_unclaimable_job_is_acked_and_skipped(monkeypatch):
    store = StubStore(claim_result=None)
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert store.names() == ["claim"]
    assert queue.calls == [("ack", "1-0")]


def test_exhausted_delivery_budget_claims_then_fails_with_fence(monkeypatch):
    """Dead-lettering supersedes a possibly live attempt via a fenced claim.

    The takeover claim bumps the attempt and fences the terminal write,
    so a partitioned owner's later writes become visible no-ops instead
    of racing the terminalization — and the upload store's ``fail``
    REQUIRES the fence, so an unfenced call would crash this path.
    """
    fresh_claim = ClaimedRun(
        run_id="run_w1",
        tenant_id="default",
        attempt=7,
        request_payload={"question": "x", "body": {"mode": "research"}},
    )
    store = StubStore(claim_result=fresh_claim)
    queue = StubQueue()

    run_one(store, queue, monkeypatch, the_job=job(delivery_count=4))

    assert "claim" in store.names()
    fails = [call for call in store.calls if call[0] == "fail"]
    assert fails and fails[0][3] == "max_retries_exceeded"
    assert fails[0][4] == 7, (
        "the terminal write must be fenced to the FRESH claim attempt"
    )
    assert ("dead_letter", "run_w1", "max_attempts_exceeded") in queue.calls


def test_exhausted_budget_on_terminal_row_dead_letters_without_fail(monkeypatch):
    """A row already terminal is respected: park the message, write nothing."""
    store = StubStore(claim_result=None)
    queue = StubQueue()

    run_one(store, queue, monkeypatch, the_job=job(delivery_count=4))

    assert [call for call in store.calls if call[0] == "fail"] == []
    assert ("dead_letter", "run_w1", "max_attempts_exceeded") in queue.calls


def test_empty_request_payload_fails_loudly(monkeypatch):
    store = StubStore(claim_result=claimed(payload={}))
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    fails = [call for call in store.calls if call[0] == "fail"]
    assert fails and "request_payload" in fails[0][2]
    assert fails[0][4] == 1
    assert ("ack", "1-0") in queue.calls


def test_redelivered_job_claims_with_takeover(monkeypatch):
    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)

    start_and_drain(loop, job(delivery_count=2), takeover=True)

    assert ("claim", "run_w1", True) in store.calls


def test_fenced_handle_threads_the_attempt_into_terminal_writes():
    store = StubStore(claim_result=None)
    import threading

    handle = FencedRunHandle(store, "run_w1", threading.Event(), attempt=7)
    handle.complete({"answer": "x"})
    handle.fail("kaputt")
    handle.cancel("client_requested_cancel")

    assert ("complete", "run_w1", 7) in store.calls
    assert any(call[0] == "fail" and call[4] == 7 for call in store.calls)
    assert ("cancelled", "run_w1", "client_requested_cancel", 7) in store.calls


@pytest.mark.parametrize("stale", [[("run_s1", "default")], []])
def test_reconciler_re_enqueues_stale_queued_rows(monkeypatch, stale):
    store = StubStore(claim_result=None)
    store.stale_queued_runs = lambda *, older_than_seconds: stale
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)

    drive(loop._reconcile())

    expected = [("enqueue", run_id) for run_id, _tenant in stale]
    assert queue.calls == expected


def test_reconciler_cooldown_suppresses_duplicate_floods(monkeypatch):
    store = StubStore(claim_result=None)
    store.stale_queued_runs = lambda *, older_than_seconds: [("run_s1", "default")]
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)

    drive(loop._reconcile())
    drive(loop._reconcile())

    assert queue.calls == [("enqueue", "run_s1")]


def test_self_reclaim_of_own_entry_is_never_acked(monkeypatch):
    """XAUTOCLAIM can hand a worker its OWN in-flight entry back;
    acking it would destroy the run's only crash-recovery entry."""
    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    drive(loop._start(job(), takeover=True))  # same message id "1-0"

    assert queue.calls == []
    assert "claim" not in store.names()


def test_duplicate_dispatch_for_active_run_is_acked_not_dropped(monkeypatch):
    """A silently dropped duplicate would idle in the PEL and mature
    into a takeover of the healthy execution — it must be acked."""
    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    duplicate = QueuedJob(
        message_id="2-0",
        run_id="run_w1",
        tenant_id="default",
        delivery_count=1,
    )
    drive(loop._start(duplicate, takeover=False))

    assert queue.calls == [("ack", "2-0")]
    assert ("dispatch_status", "run_w1", "default") in store.calls
    assert "claim" not in store.names()


def test_unsegmented_worker_keeps_historical_duplicate_ack(monkeypatch):
    """The Base default used by reindex never invents successor semantics."""
    from inqtrix.worker.loop import BaseWorkerLoop, _ActiveJob

    class _UnsegmentedLoop(BaseWorkerLoop[QueuedJob, ClaimedRun]):
        def _entity_id(self, queued: QueuedJob) -> str:
            return queued.run_id

        def _execute(self, queued, claimed_run, cancel_event) -> bool:
            del queued, claimed_run, cancel_event
            return True

        async def _stale_dispatch(self):
            return []

        async def _cancel_requested(self, watched):
            del watched
            return set()

        async def _enqueue_dispatch(self, entity_id, tenant_id) -> None:
            del entity_id, tenant_id

    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = _UnsegmentedLoop(
        store=store,
        queue=queue,
        concurrency=1,
        max_attempts=3,
        heartbeat_seconds=15,
        claim_idle_seconds=90,
    )
    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    drive(loop._start(successor_job(), takeover=False))

    assert queue.calls == [("ack", "2-0")]
    assert "dispatch_status" not in store.names()


def test_queued_successor_is_held_hearted_and_claimed_after_old_ack(
    monkeypatch,
):
    """A wake racing the old segment's unwind is never ACKed as a duplicate."""
    store = StubStore(claim_result=claimed())
    store.dispatch_status_value = "queued"
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(job=old, cancel_event=threading.Event())

    drive(loop._start(successor, takeover=False))

    assert queue.calls == []
    assert loop._heartbeat_message_ids() == ["1-0", "2-0"]
    assert loop._active[old.run_id].successor is successor

    started: list[tuple[Any, ...]] = []

    def _start_segment(*args):
        started.append(args)

    monkeypatch.setattr(loop, "_start_segment", _start_segment)
    drive(loop._finish_active(old, allow_successor=True))

    assert ("claim", "run_w1", False) in store.calls
    assert len(started) == 1
    active = loop._active[old.run_id]
    assert active.job is successor
    assert active.successor is None
    assert active.handoff_in_progress is False
    # The placeholder remains capacity/drain-visible until the successor's
    # segment releases it.
    assert drive(loop.drain(timeout=0)) is False


def test_second_successor_duplicate_is_acked_while_first_is_held(monkeypatch):
    store = StubStore(claim_result=claimed())
    store.dispatch_status_value = "queued"
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())
    drive(loop._start(successor_job("2-0"), takeover=False))
    drive(loop._start(successor_job("3-0"), takeover=False))

    assert queue.calls == [("ack", "3-0")]
    assert store.names().count("dispatch_status") == 1


def test_successor_status_read_error_never_degrades_to_ack(monkeypatch):
    store = StubStore(claim_result=claimed())
    store.dispatch_status_error = RuntimeError("database unavailable")
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    with pytest.raises(RuntimeError, match="database unavailable"):
        drive(loop._start(successor_job(), takeover=False))

    assert queue.calls == []
    assert loop._active["run_w1"].successor is None


def test_successor_stays_unacked_when_old_message_ack_failed(monkeypatch):
    store = StubStore(claim_result=claimed())
    store.dispatch_status_value = "queued"
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active["run_w1"] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )

    drive(loop._finish_active(old, allow_successor=False))

    assert queue.calls == []
    assert "claim" not in store.names()
    assert "run_w1" not in loop._active


def test_cancelled_successor_is_acked_without_execution(monkeypatch):
    store = StubStore(claim_result=None)
    store.dispatch_status_value = "queued"
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    successor = successor_job()
    loop._active["run_w1"] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=successor,
    )

    drive(loop._finish_active(old, allow_successor=True))

    assert queue.calls == [("ack", "2-0")]
    assert ("claim", "run_w1", False) in store.calls
    assert "run_w1" not in loop._active


def test_fast_successor_can_park_again_as_soon_as_its_segment_starts(
    monkeypatch,
):
    """A third segment is not mistaken for a duplicate of its predecessor."""
    store = StubStore(claim_result=claimed())
    store.dispatch_status_value = "queued"
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    from inqtrix.worker.loop import _ActiveJob

    old = job()
    second = successor_job("2-0")
    third = successor_job("3-0")
    loop._active["run_w1"] = _ActiveJob(
        job=old,
        cancel_event=threading.Event(),
        successor=second,
    )

    arrivals: list[asyncio.Future[None]] = []

    def _start_segment(*_args):
        # Segment 2 can park at once. Model that exact interleaving: its row
        # is queued when dispatch 3 reaches the claim loop.
        arrivals.append(asyncio.ensure_future(loop._start(third, takeover=False)))

    monkeypatch.setattr(loop, "_start_segment", _start_segment)

    async def scenario() -> None:
        await loop._finish_active(old, allow_successor=True)
        await asyncio.gather(*arrivals)

    drive(scenario())

    assert queue.calls == []
    active = loop._active["run_w1"]
    assert active.job is second
    assert active.successor is third
    assert active.handoff_in_progress is False
    assert loop._heartbeat_message_ids() == ["2-0", "3-0"]


def test_fenced_out_attempt_does_not_ack_the_message(monkeypatch):
    """When a superseding worker owns the run (and the message), the
    zombie's ack would strip the new owner's crash-recovery entry."""
    store = StubStore(claim_result=claimed())
    store.terminal_lands = False
    queue = StubQueue()

    loop = make_loop(store, queue, monkeypatch)
    start_and_drain(loop, job(), takeover=False)

    assert ("complete", "run_w1", 1) in store.calls
    assert ("ack", "1-0") not in queue.calls


def test_a_tick_claims_up_to_the_free_capacity_not_one_job(monkeypatch):
    """Filling N slots must cost one round trip, not N of them.

    Every claim is a queue read plus a durable claim write. Claiming one
    job per tick makes the time to reach full concurrency scale with the
    concurrency itself: a worker sized for a hundred runs spent most of a
    minute getting there while the work sat queued, so raising
    INQTRIX_WORKER_CONCURRENCY bought far less than it appeared to.
    """
    seen: list[int] = []

    class CountingQueue(StubQueue):
        async def claim_new(self, *, block_ms, count=1):
            seen.append(count)
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            return []

    queue = CountingQueue()
    loop = make_loop(
        StubStore(claim_result=None), queue, monkeypatch, concurrency=25
    )
    # Skip the reclaim and reconcile branches; the claim is what is under
    # test, and both are exercised by their own tests above.
    loop._last_reclaim = loop._last_reconcile = time.monotonic()
    drive(loop._tick())

    assert seen, "the tick must attempt a claim"
    assert seen[0] == 25, (
        "an idle worker must ask for every slot it can fill; asking for one "
        "makes the ramp to full concurrency scale with the concurrency"
    )


def test_a_tick_reclaims_only_what_it_can_start(monkeypatch):
    """The slot counter is the one limit of a loop, on the takeover path too."""
    asked: list[int] = []

    class CountingQueue(StubQueue):
        async def claim_new(self, *, block_ms, count=1):
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            asked.append(count)
            return []

    loop = make_loop(StubStore(claim_result=None), CountingQueue(), monkeypatch, concurrency=4)
    loop._last_reconcile = time.monotonic()
    loop._active.update({"a": object(), "b": object(), "c": object()})

    drive(loop._tick())

    assert asked == [1]


def test_a_worker_never_claims_more_than_it_can_start(monkeypatch):
    """The bound that keeps a greedy batch from stranding work.

    A claimed entry is owned by this consumer: claiming past capacity
    would leave it pending here, running nowhere, and invisible to the
    other workers that had room for it.
    """
    seen: list[int] = []

    class CountingQueue(StubQueue):
        async def claim_new(self, *, block_ms, count=1):
            seen.append(count)
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            return []

    queue = CountingQueue()
    loop = make_loop(
        StubStore(claim_result=None), queue, monkeypatch, concurrency=4
    )
    # Skip the reclaim and reconcile branches; the claim is what is under
    # test, and both are exercised by their own tests above.
    loop._last_reclaim = loop._last_reconcile = time.monotonic()
    # Three of four slots already taken.
    loop._active.update({"a": object(), "b": object(), "c": object()})
    drive(loop._tick())

    assert seen == [1], f"expected exactly the one free slot, got {seen}"


def test_worker_rehydrates_canvas_context(monkeypatch):
    """Durable replay reconstructs the typed canvas attachment.

    The rehydration list in worker/loop.py is the one place an additive
    request field can silently vanish between API and worker — this pin
    fails the moment ``canvas_context`` is dropped from it.
    """
    captured = []

    def fake_execute(handle, *, run_request, **kwargs):
        captured.append(run_request)
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    context = {
        "artifact_id": "art_ctx1",
        "revision": 3,
        "comments": [
            {
                "artifact_id": "art_ctx1",
                "revision": 3,
                "quote": "Der Umsatz stieg.",
                "quote_before": "",
                "quote_after": "",
                "comment": "Bitte Zahl ergaenzen.",
            }
        ],
    }
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Arbeite die Kommentare ein.",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "canvas_context": context,
                },
            },
            workspace_id="ws-agent",
        )
    )
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert len(captured) == 1
    rehydrated = captured[0].canvas_context
    assert rehydrated is not None
    assert rehydrated.model_dump(mode="json") == context


def test_worker_rehydrates_the_rule_parts(monkeypatch):
    """The parts a run is frozen under survive durable replay; without
    them a worker segment would bind its transcript to no rule at all."""
    captured = []

    def fake_execute(handle, *, run_request, **kwargs):
        captured.append(run_request)
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    parts = [{"template_id": "pt_brief", "label": "briefing", "revision": 3}]
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Erstelle ein Briefing.",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "report_requirement": (
                        "[Rule: briefing]\nKurz.\n[End rule: briefing]"
                    ),
                    "report_rules": parts,
                },
            },
            workspace_id="ws-agent",
        )
    )
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert len(captured) == 1
    assert captured[0].report_rules == (
        {"template_id": "pt_brief", "label": "briefing", "revision": 3},
    )


def test_worker_rehydrates_the_role_fields(monkeypatch):
    """A role child is executed by a worker from its stored request alone.

    Without its profile the worker would run the child as a conversation and
    publish an answer; without its handover the role would have no assignment.
    """
    captured = []

    def fake_execute(handle, *, run_request, **kwargs):
        captured.append(run_request)
        handle.complete(minimal_agent_result())

    monkeypatch.setattr("inqtrix.worker.loop.execute_run_request", fake_execute)
    handover = {
        "goal": "Review the outputs.",
        "assignment": "Frage: Wie ist die Lage?",
        "clarifications": [],
        # A judging role carries no work order of its own.
        "brief": "",
        "outputs": [{"kind": "chat", "artifact_id": "", "revision": 0, "title": "", "text": "Antwort [W1]."}],
        "evidence_run_id": "run_parent",
        "evidence_references": 3,
        "released_sources": [
            {"kind": "knowledge_document", "source_id": "kd_1", "version": "abc", "title": ""}
        ],
        "diagnostics": [],
        "criteria": ["Die Lage ist mit Quelle belegt."],
        "expected_result": "The typed verdict.",
        "open_gaps": [],
        "language": "de",
    }
    store = StubStore(
        claim_result=claimed(
            payload={
                "question": "Review the outputs.",
                "history": "",
                "messages": [],
                "body": {
                    "mode": "research",
                    "agent_overrides": {},
                    "knowledge_filters": {},
                    "work_profile": "review",
                    "handover": handover,
                },
            },
            workspace_id="ws-agent",
        )
    )
    queue = StubQueue()

    run_one(store, queue, monkeypatch)

    assert len(captured) == 1
    assert captured[0].work_profile == "review"
    # A request stored before the handover said whether its run holds
    # stored evidence reads as one that does.
    assert captured[0].handover.model_dump(mode="json") == {**handover, "stored_evidence": True}


def test_only_a_cause_that_is_sanitized_by_construction_stays_in_the_chain():
    """Naming a cause and printing it raw are two different things.

    The message says what happened, scrubbed. The chain is a second
    channel: ``raise ... from exc`` makes the interpreter print the
    cause's own repr and traceback verbatim, above the new one, and a
    driver exception carries the connection string it failed with - the
    reason ``urls.py`` scrubs URL userinfo at all. A
    ``WorkerClaimGuardError`` is safe there because its text is scrubbed
    when it is built; anything else reaching that broad ``except`` is not.
    """
    import ast
    import inspect

    from inqtrix.worker import __main__ as worker

    raises = [
        node for node in ast.walk(ast.parse(inspect.getsource(worker)))
        if isinstance(node, ast.Raise) and node.cause is not None
        and not (isinstance(node.cause, ast.Constant) and node.cause.value is None)
    ]
    for node in raises:
        chained = ast.unparse(node.cause)
        assert "isinstance" in chained and "WorkerClaimGuardError" in chained, (
            f"line {node.lineno} chains {chained!r}; a cause that is not "
            "sanitized by construction must not stand in the chain"
        )


# --------------------------------------------------------------------- #
# On the process loop
# --------------------------------------------------------------------- #


def test_a_segment_runs_on_the_transition_lane_with_a_slot_held(monkeypatch):
    loop = make_loop(StubStore(claim_result=claimed()), StubQueue(), monkeypatch)
    seen: list[str] = []

    def execute(*_args) -> bool:
        seen.append(threading.current_thread().name)
        return True

    monkeypatch.setattr(loop, "_execute", execute)

    start_and_drain(loop, job(), takeover=False)

    assert seen[0].startswith("inqtrix-legacy_segment")
    assert loop._active == {}


def test_a_segment_that_raises_frees_its_slot_and_says_so(monkeypatch, caplog):
    loop = make_loop(StubStore(claim_result=claimed()), StubQueue(), monkeypatch)

    def execute(*_args) -> bool:
        raise RuntimeError("the body broke")

    monkeypatch.setattr(loop, "_execute", execute)

    with caplog.at_level(logging.ERROR, logger="inqtrix"):
        start_and_drain(loop, job(), takeover=False)

    assert loop._active == {}
    assert "segment for job run_w1 ended with an error" in caplog.text


def test_a_failed_ack_keeps_the_held_successor_for_redelivery(monkeypatch):
    """The successor may start only once the old stream entry is gone."""
    from inqtrix.worker.loop import _ActiveJob

    class BrokenAck(StubQueue):
        async def ack(self, message_id):
            raise RuntimeError("valkey unavailable")

    store = StubStore(claim_result=claimed())
    loop = make_loop(store, BrokenAck(), monkeypatch)
    old = job()
    loop._active[old.run_id] = _ActiveJob(
        job=old, cancel_event=threading.Event(), successor=successor_job()
    )

    drive(loop._run_segment(old, claimed(), threading.Event()))

    assert "claim" not in store.names()
    assert loop._active == {}


def test_a_landed_ack_lets_the_held_successor_start(monkeypatch):
    from inqtrix.worker.loop import _ActiveJob

    store = StubStore(claim_result=claimed())
    queue = StubQueue()
    loop = make_loop(store, queue, monkeypatch)
    old = job()
    successor = successor_job()
    loop._active[old.run_id] = _ActiveJob(
        job=old, cancel_event=threading.Event(), successor=successor
    )
    started: list[Any] = []
    monkeypatch.setattr(loop, "_start_segment", lambda the_job, *_rest: started.append(the_job))

    drive(loop._run_segment(old, claimed(), threading.Event()))

    assert queue.calls == [("ack", "1-0")]
    assert started == [successor]


def test_the_shell_reaches_the_store_through_its_async_names(monkeypatch):
    """A sync name would wait for the database on the loop, or on a pool thread for the loop."""

    class WatchingStore(StubStore):
        def __init__(self) -> None:
            super().__init__(claim_result=claimed())
            self.reached: set[str] = set()

        async def aclaim_for_execution(
            self, run_id, tenant_id, *, allow_takeover, workflow_contracts
        ):
            self.reached.add("claim")
            return self.claim_result

        async def afail(self, run_id, message, *, error_type="server_error", fence_attempt=None):
            self.reached.add("fail")
            return True

        async def adispatch_status(self, run_id, tenant_id):
            self.reached.add("dispatch_status")
            return "running"

        async def astale_queued_runs(self, *, older_than_seconds):
            self.reached.add("stale")
            return []

        async def acancel_requested_runs(self, run_ids):
            self.reached.add("cancel")
            return set()

        def _refused(self, *args, **kwargs):
            raise AssertionError("the sync name was used")

        claim_for_execution = fail = dispatch_status = _refused
        stale_queued_runs = cancel_requested_runs = _refused

    store = WatchingStore()
    loop = make_loop(store, StubQueue(), monkeypatch)

    async def scenario() -> None:
        await loop._start(job(delivery_count=99), takeover=False)
        await loop._is_successor_dispatch(job())
        await loop._stale_dispatch()
        await loop._cancel_requested({"run_w1": "default"})

    drive(scenario())

    assert store.reached == {"claim", "fail", "dispatch_status", "stale", "cancel"}


def test_heartbeats_go_on_through_the_drain_and_end_with_it(monkeypatch):
    """A draining job without heartbeats would be taken over and run twice."""
    from inqtrix.worker.loop import _ActiveJob

    queue = StubQueue()
    loop = make_loop(StubStore(claim_result=None), queue, monkeypatch)
    loop._heartbeat_seconds = 0.01
    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    async def scenario() -> None:
        loop.request_stop()
        heartbeat = asyncio.create_task(loop._heartbeat_loop())
        await asyncio.sleep(0.1)
        assert not heartbeat.done()
        loop._active.clear()
        assert await loop.drain(timeout=1)
        await asyncio.wait_for(heartbeat, timeout=2)

    drive(scenario())

    assert ("heartbeat", ("1-0",)) in queue.calls


def test_a_failing_heartbeat_leaves_the_heartbeat_running(monkeypatch, caplog):
    from inqtrix.worker.loop import _ActiveJob

    class BrokenHeartbeat(StubQueue):
        beats = 0

        async def heartbeat(self, message_ids):
            self.beats += 1
            raise RuntimeError("valkey unavailable")

    queue = BrokenHeartbeat()
    loop = make_loop(StubStore(claim_result=None), queue, monkeypatch)
    loop._heartbeat_seconds = 0.01
    loop._active["run_w1"] = _ActiveJob(job=job(), cancel_event=threading.Event())

    async def scenario() -> None:
        heartbeat = asyncio.create_task(loop._heartbeat_loop())
        await asyncio.sleep(0.1)
        assert not heartbeat.done()
        loop._terminated.set()
        await asyncio.wait_for(heartbeat, timeout=2)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        drive(scenario())

    assert queue.beats > 1
    assert "heartbeat failed" in caplog.text


def test_the_cancel_poll_reaches_a_running_segment(monkeypatch):
    from inqtrix.worker.loop import _ActiveJob

    class Cancelling(StubStore):
        def cancel_requested_runs(self, run_ids):
            return set(run_ids)

    monkeypatch.setattr("inqtrix.worker.loop._CANCEL_POLL_SECONDS", 0.01)
    loop = make_loop(Cancelling(claim_result=None), StubQueue(), monkeypatch)
    entry = _ActiveJob(job=job(), cancel_event=threading.Event())
    loop._active["run_w1"] = entry

    async def scenario() -> None:
        poll = asyncio.create_task(loop._cancel_loop())
        await asyncio.sleep(0.1)
        loop._terminated.set()
        await asyncio.wait_for(poll, timeout=2)

    drive(scenario())

    assert entry.cancel_event.is_set()


def test_stopping_needs_no_claim_to_return_early(monkeypatch):
    """A stop waits out a claim's block, so a delivered message is never stranded."""
    closed: list[bool] = []

    class WaitingQueue(StubQueue):
        async def ensure_group(self):
            return None

        async def claim_pending(self):
            return []

        async def reclaim(self, *, min_idle_ms, count=10):
            return []

        async def claim_new(self, *, block_ms, count=1):
            try:
                await asyncio.sleep(0.05)
                return []
            finally:
                closed.append(not asyncio.current_task().cancelling())

    loop = make_loop(StubStore(claim_result=None), WaitingQueue(), monkeypatch)
    loop._last_reclaim = float("inf")
    loop._last_reconcile = float("inf")

    async def scenario() -> None:
        claiming = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.01)
        loop.request_stop()
        await asyncio.wait_for(claiming, timeout=2)
        loop._terminated.set()

    drive(scenario())

    assert closed == [True]

