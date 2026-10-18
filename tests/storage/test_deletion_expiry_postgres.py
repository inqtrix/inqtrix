"""Postgres tests for the terminal guarantee on deletion operations (gated).

A deletion receipt that stays ``queued``/``running`` forever is a dead end:
clients poll it without an exit, ``retry`` is rejected because it requires
``delete_failed``, and a second DELETE answers 409. These tests pin the
three ways out — the dispatch timeout for an operation nobody claimed, the
restart sweep for one whose in-process work closure died with its process,
and the lost-execution expiry for a RUNNING receipt whose executing thread
died without a terminal write.

Every store gets its OWN engine (it drives its own event loop, and asyncpg
pools are loop-affine), and the test's own assertions use short-lived
engines of their own for the same reason.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text

from inqtrix.runs.deletion_operations import (
    DeletionOperationStatus,
    DeletionStage,
    DeletionTargetKind,
    SessionDeletionContext,
)
from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.deletions_orm import (
    deletion_operation_events,
    deletion_operations,
)
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_ID = canonical_user_id("deletion-expiry-owner")
OTHER_USER_ID = canonical_user_id("deletion-unrelated-user")
WORKSPACE = "ws-expiry"


class _SilentQueue:
    """A dispatch channel nobody consumes.

    This is the deployment shape the timeout exists for: the row is
    enqueued and accepted, no worker ever claims it, and — unlike the
    no-queue case — no restart ever comes to clean it up.
    """

    def enqueue(self, *, operation_id: str, tenant_id: str) -> None:
        del operation_id, tenant_id

    def ack(self, message_id: str) -> None:
        del message_id


@pytest.fixture(scope="session", autouse=True)
def deletion_expiry_schema_migrated():
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
                await session.execute(deletion_operation_events.delete())
                await session.execute(deletion_operations.delete())
                await session.execute(agent_sessions.delete())
                await ensure_canonical_users(session, (OWNER_ID, OTHER_USER_ID))
    finally:
        await engine.dispose()
    yield


def _store(
    *,
    queue,
    dispatch_timeout_seconds: float = 240.0,
    worker_id: str = "expiry-test",
) -> PostgresDeletionOperationStore:
    return PostgresDeletionOperationStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        max_concurrent=1,
        completed_ttl_seconds=3600,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
        worker_id=worker_id,
    )


async def _seed_session(session_id: str, *, owner=OWNER_ID) -> None:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    insert(agent_sessions).values(
                        id=session_id,
                        tenant_id="default",
                        created_by_user_id=owner,
                        workspace_id=WORKSPACE,
                        title="Expiry",
                        items_json="[]",
                        lifecycle_status="active",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
    finally:
        await engine.dispose()


async def _session_row(session_id: str) -> dict:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            row = (
                (
                    await session.execute(
                        select(
                            agent_sessions.c.lifecycle_status,
                            agent_sessions.c.deletion_error,
                        ).where(agent_sessions.c.id == session_id)
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else {}
    finally:
        await engine.dispose()


def _submit_session_deletion(store, session_id: str, *, owner=OWNER_ID) -> str:
    summary = store.submit(
        target_kind=DeletionTargetKind.AGENT_SESSION,
        target_id=session_id,
        manifest=(),
        tenant_id="default",
        created_by_user_id=owner,
        workspace_id=WORKSPACE,
        work=lambda handle: None,
        session_context=SessionDeletionContext(
            target_kind=DeletionTargetKind.AGENT_SESSION,
            session_id=session_id,
        ),
        total_items=2,
    )
    return summary["operation_id"]


def _get(store, operation_id: str) -> dict:
    return store.get(
        operation_id,
        tenant_id="default",
        created_by_user_id=OWNER_ID,
        workspace_id=WORKSPACE,
    )


@pytest.mark.asyncio
async def test_unclaimed_operation_expires_and_frees_the_session() -> None:
    """The dead end ends: expiry makes the receipt terminal AND retryable."""

    await _seed_session("as_expire")
    store = _store(queue=_SilentQueue(), dispatch_timeout_seconds=0.0)
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, store, "as_expire")
        # submit runs (and throttles) an expiry pass of its own; the next
        # read triggers the one under test.
        store._last_expiry_check = None
        assert _get(store, operation_id)["status"] == (
            DeletionOperationStatus.DELETE_FAILED.value
        )
        summary = _get(store, operation_id)
        assert summary["error"]["type"] == "dispatch_timeout"

        row = await _session_row("as_expire")
        assert row["lifecycle_status"] == "delete_failed"
        assert row["deletion_error"]

        # Retry requires delete_failed, so this call is the proof that the
        # operation is reachable again instead of stuck behind a 409.
        retried = await asyncio.to_thread(
            store.retry,
            operation_id,
            tenant_id="default",
            created_by_user_id=OWNER_ID,
            workspace_id=WORKSPACE,
            work=lambda handle: None,
        )
        assert retried["status"] == DeletionOperationStatus.QUEUED.value
    finally:
        store.close()


@pytest.mark.asyncio
async def test_fresh_operation_survives_the_expiry_pass() -> None:
    """A healthy operation inside its window is never touched."""

    await _seed_session("as_fresh")
    store = _store(queue=_SilentQueue(), dispatch_timeout_seconds=240.0)
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, store, "as_fresh")
        store._last_expiry_check = None
        assert _get(store, operation_id)["status"] == (
            DeletionOperationStatus.QUEUED.value
        )
        row = await _session_row("as_fresh")
        assert row["lifecycle_status"] == "deleting"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_expiry_is_idempotent() -> None:
    """A second pass over an already-failed operation changes nothing."""

    await _seed_session("as_twice")
    store = _store(queue=_SilentQueue(), dispatch_timeout_seconds=0.0)
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, store, "as_twice")
        store._last_expiry_check = None
        first = _get(store, operation_id)
        store._last_expiry_check = None
        second = _get(store, operation_id)

        assert first["status"] == DeletionOperationStatus.DELETE_FAILED.value
        assert second["status"] == first["status"]
        assert second["error"]["type"] == "dispatch_timeout"
        assert second["finished_at"] == first["finished_at"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_sweep_fails_orphans_of_a_previous_process() -> None:
    """A row whose work closure died with its process is swept once."""

    await _seed_session("as_orphan")
    producer = _store(queue=_SilentQueue(), worker_id="previous-process")
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, producer, "as_orphan")
    finally:
        producer.close()

    # queue=None is the in-process shape, the only one resolve_orphan_sweep
    # lets sweep. The generous timeout keeps the dispatch-timeout path out
    # of this test, so the verdict can only come from the sweep.
    successor = _store(
        queue=None,
        dispatch_timeout_seconds=10_000.0,
        worker_id="new-process",
    )
    try:
        summary = _get(successor, operation_id)
        assert summary["status"] == DeletionOperationStatus.DELETE_FAILED.value
        assert summary["error"]["type"] == "server_restarted"

        row = await _session_row("as_orphan")
        assert row["lifecycle_status"] == "delete_failed"
    finally:
        successor.close()


@pytest.mark.asyncio
async def test_queue_mode_never_sweeps_live_worker_rows() -> None:
    """With a queue the rows belong to the workers, so no sweep may run."""

    await _seed_session("as_queued")
    producer = _store(queue=_SilentQueue(), worker_id="previous-process")
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, producer, "as_queued")
    finally:
        producer.close()

    successor = _store(
        queue=_SilentQueue(),
        dispatch_timeout_seconds=10_000.0,
        worker_id="new-process",
    )
    try:
        assert _get(successor, operation_id)["status"] == (
            DeletionOperationStatus.QUEUED.value
        )
    finally:
        successor.close()


def _orphan_running_operation(store, session_id: str, monkeypatch) -> str:
    """Produce a RUNNING receipt whose executing thread died terminal-less.

    The terminal write is broken while the in-process worker unwinds, so
    the row stays ``running`` with no ``_local`` owner — the exact shape a
    storage outage leaves behind.
    """
    import time as _time

    from inqtrix.runs import durable_store as _durable
    from inqtrix.runs.deletion_postgres import (
        PostgresDeletionOperationStore as _Store,
    )

    monkeypatch.setattr(_durable, "_TERMINAL_WRITE_ATTEMPTS", 1)
    monkeypatch.setattr(
        _durable, "_TERMINAL_WRITE_BACKOFF_START_SECONDS", 0.01
    )
    broken = {"on": True}
    original = _Store._terminal_db

    def _breakable(self, *args, **kwargs):
        if broken["on"]:

            async def _boom():
                raise RuntimeError("db down")

            return _boom()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_Store, "_terminal_db", _breakable)

    def _dying_work(handle):
        raise RuntimeError("kaputt")

    summary = store.submit(
        target_kind=DeletionTargetKind.AGENT_SESSION,
        target_id=session_id,
        manifest=(),
        tenant_id="default",
        created_by_user_id=OWNER_ID,
        workspace_id=WORKSPACE,
        work=_dying_work,
        session_context=SessionDeletionContext(
            target_kind=DeletionTargetKind.AGENT_SESSION,
            session_id=session_id,
        ),
        total_items=2,
    )
    operation_id = summary["operation_id"]
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        if operation_id not in store._local:
            break
        _time.sleep(0.05)
    else:
        pytest.fail("worker thread never unwound")
    broken["on"] = False
    return operation_id


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    # The worker thread deliberately dies re-raising after its terminal
    # write is broken — that unwind is exactly the scenario under test.
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
async def test_lost_running_operation_expires_and_stays_retryable(
    monkeypatch,
) -> None:
    """A RUNNING receipt without an executor converges via the expiry."""

    import inqtrix.runs.deletion_postgres as deletion_module

    await _seed_session("as_lost_running")
    store = _store(queue=None, dispatch_timeout_seconds=10_000.0)
    try:
        operation_id = await asyncio.to_thread(
            _orphan_running_operation,
            store, "as_lost_running", monkeypatch
        )
        monkeypatch.setattr(
            deletion_module, "_EXECUTION_LOST_GRACE_SECONDS", 0.0
        )
        store._last_expiry_check = None
        summary = _get(store, operation_id)
        assert summary["status"] == (
            DeletionOperationStatus.DELETE_FAILED.value
        )
        assert summary["error"]["type"] == "execution_lost"

        row = await _session_row("as_lost_running")
        assert row["lifecycle_status"] == "delete_failed"

        retried = await asyncio.to_thread(
            store.retry,
            operation_id,
            tenant_id="default",
            created_by_user_id=OWNER_ID,
            workspace_id=WORKSPACE,
            work=lambda handle: None,
        )
        assert retried["status"] in (
            DeletionOperationStatus.QUEUED.value,
            DeletionOperationStatus.RUNNING.value,
            DeletionOperationStatus.DELETED.value,
        )
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
async def test_queue_mode_never_expires_running_rows(monkeypatch) -> None:
    """RUNNING rows belong to workers in queue mode — no expiry may touch them."""

    import inqtrix.runs.deletion_postgres as deletion_module

    await _seed_session("as_worker_running")
    producer = _store(queue=None, dispatch_timeout_seconds=10_000.0)
    try:
        operation_id = await asyncio.to_thread(
            _orphan_running_operation,
            producer, "as_worker_running", monkeypatch
        )
    finally:
        producer.close()

    successor = _store(
        queue=_SilentQueue(),
        dispatch_timeout_seconds=10_000.0,
        worker_id="new-process",
    )
    try:
        monkeypatch.setattr(
            deletion_module, "_EXECUTION_LOST_GRACE_SECONDS", 0.0
        )
        successor._last_expiry_check = None
        assert _get(successor, operation_id)["status"] == (
            DeletionOperationStatus.RUNNING.value
        )
    finally:
        successor.close()


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
async def test_submit_pass_expires_stale_receipts_first(monkeypatch) -> None:
    """A fresh DELETE call heals unrelated lost receipts on its way in."""

    import inqtrix.runs.deletion_postgres as deletion_module

    await _seed_session("as_lost_before_submit")
    await _seed_session("as_new_target")
    store = _store(queue=None, dispatch_timeout_seconds=10_000.0)
    try:
        lost_id = await asyncio.to_thread(
            _orphan_running_operation,
            store, "as_lost_before_submit", monkeypatch
        )
        monkeypatch.setattr(
            deletion_module, "_EXECUTION_LOST_GRACE_SECONDS", 0.0
        )
        store._last_expiry_check = None
        await asyncio.to_thread(_submit_session_deletion, store, "as_new_target")
        summary = _get(store, lost_id)
        assert summary["status"] == (
            DeletionOperationStatus.DELETE_FAILED.value
        )
        assert summary["error"]["type"] == "execution_lost"
    finally:
        store.close()


async def _notifications(operation_id: str) -> list[dict]:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            rows = (
                await session.execute(
                    select(
                        user_events.c.tenant_id,
                        user_events.c.target_user_id,
                        user_events.c.scope,
                        user_events.c.resource_type,
                        user_events.c.resource_id,
                    )
                    .where(user_events.c.resource_id == operation_id)
                    .order_by(user_events.c.id)
                )
            ).mappings().all()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deletion_lifecycle_notifies_only_its_owner_without_progress_noise() -> None:
    await _seed_session("as_notify")
    store = _store(queue=_SilentQueue())
    try:
        operation_id = await asyncio.to_thread(_submit_session_deletion, store, "as_notify")
        expected = {
            "tenant_id": "default",
            "target_user_id": OWNER_ID,
            "scope": "deletion_operations",
            "resource_type": "deletion_operation",
            "resource_id": operation_id,
        }
        assert await _notifications(operation_id) == [expected]

        claimed = store.claim_for_execution(
            operation_id, "default", allow_takeover=False
        )
        assert claimed is not None
        assert store.progress(
            operation_id,
            stage=DeletionStage.SESSION_DATA_REMOVED,
            completed_items=1,
            total_items=2,
            fence_attempt=claimed.attempt,
        )
        assert await _notifications(operation_id) == [expected]

        assert not store.complete(operation_id, fence_attempt=claimed.attempt + 1)
        assert await _notifications(operation_id) == [expected]
        assert store.fail(operation_id, "Dependency unavailable", fence_attempt=claimed.attempt)
        assert await _notifications(operation_id) == [expected] * 2

        await asyncio.to_thread(
            store.retry,
            operation_id,
            tenant_id="default",
            created_by_user_id=OWNER_ID,
            workspace_id=WORKSPACE,
            work=lambda handle: None,
        )
        assert await _notifications(operation_id) == [expected] * 3
        retried = store.claim_for_execution(
            operation_id, "default", allow_takeover=False
        )
        assert retried is not None
        assert await _notifications(operation_id) == [expected] * 3
        assert store.complete(operation_id, fence_attempt=retried.attempt)
        assert await _notifications(operation_id) == [expected] * 4
        assert await _session_row("as_notify") == {}

        assert not store.complete(operation_id, fence_attempt=retried.attempt)
        assert await _notifications(operation_id) == [expected] * 4
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deletion_notification_rolls_back_with_its_receipt(monkeypatch) -> None:
    await _seed_session("as_notification_rollback")
    original = PostgresDeletionOperationStore._append_event_db
    attempted_ids: list[str] = []

    async def abort_after_event(self, session, operation_id, *args, **kwargs):
        await original(self, session, operation_id, *args, **kwargs)
        attempted_ids.append(operation_id)
        # Observe the actual signal before aborting the caller transaction.
        # A separately committed notification would survive this abort.
        assert (
            await session.execute(
                select(user_events.c.resource_id).where(
                    user_events.c.resource_id == operation_id
                )
            )
        ).scalar_one() == operation_id
        raise RuntimeError("abort notification transaction")

    monkeypatch.setattr(
        PostgresDeletionOperationStore, "_append_event_db", abort_after_event
    )
    store = _store(queue=_SilentQueue())
    try:
        with pytest.raises(RuntimeError, match="abort notification transaction"):
            await asyncio.to_thread(_submit_session_deletion, store, "as_notification_rollback")
        assert len(attempted_ids) == 1
        assert await _notifications(attempted_ids[0]) == []
        assert (await _session_row("as_notification_rollback"))["lifecycle_status"] == "active"

        engine = build_engine(TEST_DATABASE_URL)
        try:
            factory = build_session_factory(engine)
            async with factory() as session:
                assert (
                    await session.execute(
                        select(deletion_operations.c.operation_id).where(
                            deletion_operations.c.operation_id == attempted_ids[0]
                        )
                    )
                ).first() is None
        finally:
            await engine.dispose()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unowned_deletion_does_not_require_a_user_notification() -> None:
    await _seed_session("as_unowned_notification", owner=None)
    store = _store(queue=_SilentQueue())
    try:
        operation_id = await asyncio.to_thread(
            _submit_session_deletion,
            store, "as_unowned_notification", owner=None
        )
        assert await _notifications(operation_id) == []
        claimed = store.claim_for_execution(
            operation_id, "default", allow_takeover=False
        )
        assert claimed is not None
        assert store.complete(operation_id, fence_attempt=claimed.attempt)
        assert await _notifications(operation_id) == []
        assert await _session_row("as_unowned_notification") == {}
    finally:
        store.close()
