"""Postgres tests for the run store's lost-execution guarantee (gated).

A run row that stays ``queued``/``running`` while no process executes it
is a dead end: clients poll a spinner forever, cancel requests are never
observed, and the row holds an admission slot. These tests pin the three
ways out — the retried worker-side terminal write, the read-triggered
lost-execution fence, and the fail-first stuck-row failsafe — plus the
guards that keep owned, parked, and queue-mode rows untouched.

Every store gets its OWN engine (it drives its own event loop, and
asyncpg pools are loop-affine); the test's own assertions use
short-lived engines for the same reason.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.exc import OperationalError

from inqtrix.runs import durable_store
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.runs_orm import run_events, runs
from inqtrix.storage.migrate import run_migrations

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"


class _SilentQueue:
    """A dispatch channel nobody consumes (queue-mode store shape)."""

    def enqueue(self, *, run_id: str, tenant_id: str) -> None:
        del run_id, tenant_id

    def ack(self, message_id: str) -> None:
        del message_id


@pytest.fixture(scope="session", autouse=True)
def execution_lost_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                bypasses = (
                    await session.execute(
                        text(
                            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
                if not bypasses:
                    pytest.fail(
                        "INQTRIX_TEST_DATABASE_URL must connect as "
                        "superuser/BYPASSRLS."
                    )
                await session.execute(text("DELETE FROM run_events"))
                await session.execute(text("DELETE FROM runs"))
    finally:
        await engine.dispose()
    yield


@pytest.fixture()
def fast_backoff(monkeypatch):
    monkeypatch.setattr(
        durable_store, "_TERMINAL_WRITE_BACKOFF_START_SECONDS", 0.01
    )
    monkeypatch.setattr(
        durable_store, "_TERMINAL_WRITE_BACKOFF_CAP_SECONDS", 0.02
    )


@pytest.fixture()
def zero_grace(monkeypatch):
    monkeypatch.setattr(durable_store, "_EXECUTION_LOST_GRACE_SECONDS", 0.0)


def _build_store(*, queue=None) -> PostgresRunStore:
    return PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-lost-worker",
    )


@pytest.fixture()
def store():
    store = _build_store()
    yield store
    store.close()


def _submit(store, work, question="Frage zur Verlust-Erkennung?"):
    return store.submit(
        question=question,
        stack_name="default",
        work=work,
        request_payload={"question": "x", "body": {"mode": "research"}},
    )


def _reset_fence_throttle(store) -> None:
    store._last_execution_lost_check = None


def wait_for_status(store, run_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = store.get(run_id)
        if summary["status"] in statuses:
            return summary
        _reset_fence_throttle(store)
        time.sleep(0.05)
    pytest.fail(f"run {run_id} never reached {statuses}")


def _wait_until(predicate, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


async def _db_fetch(query):
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            return (await session.execute(query)).all()
    finally:
        await engine.dispose()


async def _db_execute(statement):
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                await session.execute(statement)
    finally:
        await engine.dispose()


async def _seed_run(
    run_id: str,
    *,
    status: str,
    age_seconds: float,
    queued_since_age: float | None = None,
    active_started_age: float | None = None,
    cancel_requested: bool = False,
    claimed_by: str | None = None,
    attempt: int = 0,
) -> None:
    now = time.time()
    await _db_execute(
        insert(runs).values(
            run_id=run_id,
            tenant_id="default",
            status=status,
            question="verwaiste Zeile",
            stack_name="default",
            created_at=now - age_seconds,
            queued_since=(
                now - queued_since_age if queued_since_age is not None else None
            ),
            active_started_at=(
                now - active_started_age
                if active_started_age is not None
                else None
            ),
            cancel_requested=cancel_requested,
            claimed_by=claimed_by,
            attempt=attempt,
        )
    )


async def _failed_event_count(run_id: str) -> int:
    rows = await _db_fetch(
        select(run_events.c.sequence).where(
            run_events.c.run_id == run_id,
            run_events.c.type == "inqtrix.run.failed",
        )
    )
    return len(rows)


def _flaky_terminal(monkeypatch, *, failures: int):
    """Make the next *failures* terminal writes raise, then delegate."""
    state = {"raised": 0}
    original = PostgresRunStore._terminal_db

    def _maybe_fail(self, *args, **kwargs):
        if state["raised"] < failures:
            state["raised"] += 1

            async def _boom():
                raise OperationalError(
                    "terminal write", None, Exception("db down")
                )

            return _boom()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PostgresRunStore, "_terminal_db", _maybe_fail)
    return state


@pytest.mark.asyncio
async def test_terminal_write_outage_is_retried_until_it_lands(
    store, monkeypatch, fast_backoff, caplog
):
    state = _flaky_terminal(monkeypatch, failures=2)

    def _work(handle):
        raise RuntimeError("kaputt")

    summary = _submit(store, _work)
    run_id = summary["run_id"]
    final = wait_for_status(store, run_id, {"failed"})
    assert final["error"]["type"] == "server_error"
    assert state["raised"] == 2
    assert await _failed_event_count(run_id) == 1
    assert any(
        "Terminal write" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    # The worker thread deliberately dies re-raising after its retries
    # are exhausted — that unwind is exactly the scenario under test.
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
async def test_exhausted_terminal_retry_converges_via_fence(
    store, monkeypatch, fast_backoff, zero_grace
):
    monkeypatch.setattr(durable_store, "_TERMINAL_WRITE_ATTEMPTS", 2)
    broken = {"on": True}
    original = PostgresRunStore._terminal_db

    def _breakable(self, *args, **kwargs):
        if broken["on"]:

            async def _boom():
                raise OperationalError(
                    "terminal write", None, Exception("db down")
                )

            return _boom()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PostgresRunStore, "_terminal_db", _breakable)

    def _work(handle):
        raise RuntimeError("kaputt")

    summary = _submit(store, _work)
    run_id = summary["run_id"]
    # The worker thread gives up and unwinds; the row stays running with
    # no registry entry — the fence's candidate shape.
    _wait_until(
        lambda: run_id not in store._local,
        message="worker unwind after exhausted retries",
    )
    broken["on"] = False
    _reset_fence_throttle(store)
    final = wait_for_status(store, run_id, {"failed"})
    assert final["error"]["type"] == "execution_lost"
    assert await _failed_event_count(run_id) == 1


def test_owned_run_is_never_fenced(store, zero_grace):
    release = threading.Event()

    def _work(handle):
        release.wait(timeout=30)
        handle.complete({"answer": "fertig"}, snapshot={"done": True})

    try:
        summary = _submit(store, _work)
        run_id = summary["run_id"]
        wait_for_status(store, run_id, {"running"})
        _reset_fence_throttle(store)
        assert store.get(run_id)["status"] == "running"
        _reset_fence_throttle(store)
        assert store.get(run_id)["status"] == "running"
    finally:
        release.set()
    wait_for_status(store, run_id, {"completed"})


@pytest.mark.asyncio
async def test_waiting_rows_are_not_fence_candidates(store, zero_grace):
    run_id = f"run_wait_{uuid.uuid4().hex}"
    now = time.time()
    await _db_execute(
        insert(runs).values(
            run_id=run_id,
            tenant_id="default",
            status="waiting_for_approval",
            question="wartet auf Freigabe",
            stack_name="default",
            created_at=now - 3600,
            waiting_since=now - 3600,
        )
    )
    _reset_fence_throttle(store)
    assert store.get(run_id)["status"] == "waiting_for_approval"


@pytest.mark.asyncio
async def test_claim_exception_drops_local_ownership_and_converges(
    store, monkeypatch, zero_grace, caplog
):
    broken = {"on": True}
    original = PostgresRunStore._claim_db

    def _breakable(self, *args, **kwargs):
        if broken["on"]:
            broken["on"] = False

            async def _boom():
                raise OperationalError("claim", None, Exception("db down"))

            return _boom()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PostgresRunStore, "_claim_db", _breakable)

    def _work(handle):
        handle.complete({"answer": "nie erreicht"})

    summary = _submit(store, _work)
    run_id = summary["run_id"]
    assert run_id not in store._local
    assert any(
        "could not be claimed" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )
    _reset_fence_throttle(store)
    final = wait_for_status(store, run_id, {"failed"})
    assert final["error"]["type"] == "execution_lost"


@pytest.mark.asyncio
async def test_aged_queued_row_without_owner_converges(store, zero_grace):
    run_id = f"run_orphan_{uuid.uuid4().hex}"
    await _seed_run(
        run_id, status="queued", age_seconds=300, queued_since_age=300
    )
    _reset_fence_throttle(store)
    final = wait_for_status(store, run_id, {"failed"})
    assert final["error"]["type"] == "execution_lost"
    assert await _failed_event_count(run_id) == 1


@pytest.mark.asyncio
async def test_lost_agent_child_wakes_parent_which_converges(
    store, zero_grace, caplog
):
    """The fence honors tree semantics: child terminal wakes the parent.

    A parent parked on its children survives a lost child as QUEUED (the
    wake), and — its closure being gone in no-queue mode — converges
    through the fence itself on the next read instead of hanging.
    """
    parent_id = f"run_parent_{uuid.uuid4().hex}"
    child_id = f"run_child_{uuid.uuid4().hex}"
    now = time.time()
    await _db_execute(
        insert(runs).values(
            run_id=parent_id,
            tenant_id="default",
            status="waiting_for_children",
            kind="agent",
            question="Eltern-Lauf",
            stack_name="default",
            created_at=now - 600,
            waiting_since=now - 300,
        )
    )
    await _db_execute(
        insert(runs).values(
            run_id=child_id,
            tenant_id="default",
            status="running",
            kind="agent_child",
            parent_run_id=parent_id,
            root_run_id=parent_id,
            question="Kind-Lauf",
            stack_name="default",
            created_at=now - 600,
            active_started_at=now - 300,
        )
    )
    _reset_fence_throttle(store)
    final_child = wait_for_status(store, child_id, {"failed"})
    assert final_child["error"]["type"] == "execution_lost"
    final_parent = wait_for_status(store, parent_id, {"failed"})
    assert final_parent["error"]["type"] == "execution_lost"
    assert any(
        "no execution remains" in record.message
        for record in caplog.records
    ), "the closure-less wake must be loud, never silent"


@pytest.mark.asyncio
async def test_restart_sweep_runs_eagerly_at_construction():
    queued_id = f"run_boot_q_{uuid.uuid4().hex}"
    running_id = f"run_boot_r_{uuid.uuid4().hex}"
    await _seed_run(
        queued_id, status="queued", age_seconds=10, queued_since_age=10
    )
    await _seed_run(
        running_id, status="running", age_seconds=10, active_started_age=5
    )
    store = _build_store()
    try:
        assert store._sweep_orphans is False
        rows = await _db_fetch(
            select(runs.c.run_id, runs.c.status, runs.c.error).where(
                runs.c.run_id.in_((queued_id, running_id))
            )
        )
        assert {row.status for row in rows} == {"failed"}
        assert {row.error["type"] for row in rows} == {"server_restarted"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_queue_mode_never_fences(zero_grace):
    run_id = f"run_worker_owned_{uuid.uuid4().hex}"
    await _seed_run(
        run_id, status="running", age_seconds=600, active_started_age=600
    )
    store = _build_store(queue=_SilentQueue())
    try:
        assert store._recovers_orphans is False
        _reset_fence_throttle(store)
        assert store.get(run_id)["status"] == "running"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stuck_row_failsafe_fails_first_and_deletes_later():
    # Queue-mode store: fence and restart sweep are OFF there, so this
    # pins the stuck-row failsafe itself — the mode-agnostic last exit.
    run_id = f"run_stuck_{uuid.uuid4().hex}"
    await _seed_run(
        run_id,
        status="running",
        age_seconds=8 * 86_400,
        active_started_age=8 * 86_400,
    )
    store = _build_store(queue=_SilentQueue())
    try:
        final = wait_for_status(store, run_id, {"failed"})
        assert final["error"]["type"] == "execution_lost"
        assert await _failed_event_count(run_id) == 1
        rows = await _db_fetch(
            select(runs.c.run_id).where(runs.c.run_id == run_id)
        )
        assert len(rows) == 1, "fail-first must keep the row for retention"
        await _db_execute(
            runs.update()
            .where(runs.c.run_id == run_id)
            .values(finished_at=time.time() - 400)
        )
        store.list()
        rows = await _db_fetch(
            select(runs.c.run_id).where(runs.c.run_id == run_id)
        )
        assert rows == [], (
            "terminal retention must delete the aged failed row"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_statement_timeout_bounds_a_hanging_statement():
    engine = build_engine(TEST_DATABASE_URL, command_timeout=0.2)
    try:
        factory = build_session_factory(engine)
        # asyncpg's client-side ceiling surfaces as a bare TimeoutError
        # through SQLAlchemy — the worker paths absorb it through their
        # ordinary ``except Exception`` handling.
        with pytest.raises(TimeoutError):
            async with factory() as session:
                await session.execute(text("SELECT pg_sleep(5)"))
    finally:
        await engine.dispose()


async def _event_count(run_id: str, event_type: str) -> int:
    rows = await _db_fetch(
        select(run_events.c.sequence).where(
            run_events.c.run_id == run_id,
            run_events.c.type == event_type,
        )
    )
    return len(rows)


@pytest.mark.asyncio
async def test_takeover_claim_with_pending_cancel_terminalizes_instead_of_executing():
    """A cancel that outlived its dead worker resolves at reclaim time."""
    run_id = f"run_cancelwait_{uuid.uuid4().hex}"
    await _seed_run(
        run_id,
        status="running",
        age_seconds=600,
        active_started_age=600,
        cancel_requested=True,
        claimed_by="dead-worker",
        attempt=1,
    )
    store = _build_store(queue=_SilentQueue())
    try:
        claimed = store.claim_for_execution(
            run_id, "default", allow_takeover=True
        )
        assert claimed is None, (
            "the reclaim must resolve the cancel, not open a doomed attempt"
        )
        rows = await _db_fetch(
            select(runs.c.status).where(runs.c.run_id == run_id)
        )
        assert rows[0].status == "cancelled"
        assert await _event_count(run_id, "inqtrix.run.cancelled") == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_queued_ttl_fails_unconsumed_run_with_typed_error():
    """A queued run nobody consumes ends typed, long before the hard cap."""
    stale_id = f"run_qttl_{uuid.uuid4().hex}"
    fresh_id = f"run_qfresh_{uuid.uuid4().hex}"
    # The FRESH-SUBMIT shape: first dispatch leaves queued_since NULL —
    # the sweep must fall back to created_at or it is blind to exactly
    # its target case.
    await _seed_run(stale_id, status="queued", age_seconds=90_000)
    await _seed_run(
        fresh_id, status="queued", age_seconds=90_000, queued_since_age=30
    )
    store = _build_store(queue=_SilentQueue())
    try:
        store.list()
        rows = await _db_fetch(
            select(runs.c.run_id, runs.c.status, runs.c.error).where(
                runs.c.run_id.in_((stale_id, fresh_id))
            )
        )
        by_id = {row.run_id: row for row in rows}
        assert by_id[stale_id].status == "failed"
        assert by_id[stale_id].error["type"] == "queued_timeout"
        assert await _event_count(stale_id, "inqtrix.run.failed") == 1
        assert by_id[fresh_id].status == "queued", (
            "a recently queued run (fresh queued_since) must survive"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stale_queued_feed_keys_on_queued_since():
    """The reconciler feed measures time IN QUEUE, not time since submit."""
    resumed_id = f"run_resumed_{uuid.uuid4().hex}"
    stale_id = f"run_stalefeed_{uuid.uuid4().hex}"
    await _seed_run(
        resumed_id, status="queued", age_seconds=900, queued_since_age=10
    )
    await _seed_run(
        stale_id, status="queued", age_seconds=900, queued_since_age=900
    )
    store = _build_store(queue=_SilentQueue())
    try:
        stale = dict(store.stale_queued_runs(older_than_seconds=120))
        assert stale_id in stale
        assert resumed_id not in stale, (
            "an old submit freshly re-queued is NOT a stale dispatch"
        )
    finally:
        store.close()


def _build_worker_store(worker_id: str) -> PostgresRunStore:
    return PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=_SilentQueue(),
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id=worker_id,
    )


@pytest.mark.asyncio
async def test_dead_letter_fail_cannot_be_overwritten_by_a_superseded_attempt():
    """The claim-first fence supersedes a partitioned owner atomically."""
    run_id = f"run_zombie_{uuid.uuid4().hex}"
    await _seed_run(
        run_id, status="queued", age_seconds=600, queued_since_age=600
    )
    owner = _build_worker_store("worker-partitioned")
    reaper = _build_worker_store("worker-reaper")
    try:
        first = owner.claim_for_execution(
            run_id, "default", allow_takeover=False
        )
        assert first is not None and first.attempt == 1
        second = reaper.claim_for_execution(
            run_id, "default", allow_takeover=True
        )
        assert second is not None and second.attempt == 2
        assert reaper.fail(
            run_id,
            "Maximale Anzahl Ausfuehrungsversuche erreicht.",
            error_type="max_retries_exceeded",
            fence_attempt=second.attempt,
        )
        assert owner.complete(
            run_id, {"answer": "zu spaet"}, fence_attempt=first.attempt
        ) is False, "the superseded attempt's write must be fenced out"
        rows = await _db_fetch(
            select(runs.c.status, runs.c.error).where(
                runs.c.run_id == run_id
            )
        )
        assert rows[0].status == "failed"
        assert rows[0].error["type"] == "max_retries_exceeded"
    finally:
        owner.close()
        reaper.close()


@pytest.mark.asyncio
async def test_first_delivery_claim_leaves_a_live_cancelling_owner_alone():
    """Without takeover authority a claim must not touch a RUNNING row.

    The two-phase cancel leaves the row RUNNING for its live owner to
    resolve; a duplicate first-delivery dispatch on another worker has no
    takeover authority and must neither claim nor terminalize it.
    """
    run_id = f"run_liveowner_{uuid.uuid4().hex}"
    await _seed_run(
        run_id,
        status="running",
        age_seconds=60,
        active_started_age=60,
        cancel_requested=True,
        claimed_by="live-worker",
        attempt=1,
    )
    bystander = _build_worker_store("worker-bystander")
    try:
        claimed = bystander.claim_for_execution(
            run_id, "default", allow_takeover=False
        )
        assert claimed is None
        rows = await _db_fetch(
            select(runs.c.status, runs.c.claimed_by).where(
                runs.c.run_id == run_id
            )
        )
        assert rows[0].status == "running", (
            "a non-takeover claim must leave the live owner's row intact"
        )
        assert rows[0].claimed_by == "live-worker"
    finally:
        bystander.close()
