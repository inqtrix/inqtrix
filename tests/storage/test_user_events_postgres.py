"""Integration tests for the Postgres user-event store's lazy retention.

Gated via the shared ``postgres`` marker like the sibling suites. The
suite pins three properties of the coalesced retention:

1. Retention still works: expired rows fall on the first touch.
2. The DELETE is coalesced: many polls, one statement per interval.
3. The DELETE scopes to the calling tenant EXPLICITLY -- proven with an
   RLS-free superuser session, so the filter cannot silently ride on the
   RLS policy (tenant_isolation, migration 0047).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from sqlalchemy import event, func, insert, select, text

from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from inqtrix.storage.user_events_postgres import PostgresUserEventStore
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

# The central conftest gate: skipped without the URL, a loud UsageError
# under INQTRIX_TEST_REQUIRE_INTEGRATION=1 -- never a silent green.
pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
TENANT_A = "default"
TENANT_B = "usrev-tenant-b"
USER_A = canonical_user_id("usrev-a")
USER_B = canonical_user_id("usrev-b")
USER_C = canonical_user_id("usrev-c")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


def _run(coro):
    """One fresh loop per step, engine built and disposed inside it.

    The async engine binds to the loop it first runs on; sharing one
    engine across sync test steps would trip the loop-affinity guard in
    storage.db. Same pattern as test_claim_schema_fence.py.
    """
    return asyncio.run(coro)


async def _wipe_events() -> None:
    """Post-scenario cleanup: leave no test rows behind."""
    engine = build_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(user_events.delete())
    finally:
        await engine.dispose()


async def _clear_events() -> None:
    """Wipe the table and guarantee the FK targets for both test users."""
    engine = build_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(user_events.delete())
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                await ensure_canonical_users(session, (USER_A, USER_C))
                await ensure_canonical_users(
                    session, (USER_B,), tenant_id=TENANT_B
                )
    finally:
        await engine.dispose()


async def _seed_event(
    tenant_id: str,
    target_user_id: uuid.UUID,
    *,
    age_seconds: float,
) -> int:
    """Insert one event with a forced age, as the superuser connection."""
    engine = build_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(user_events)
                    .values(
                        tenant_id=tenant_id,
                        target_user_id=target_user_id,
                        scope="test",
                        created_at=text(
                            f"now() - interval '{float(age_seconds)} seconds'"
                        ),
                    )
                    .returning(user_events.c.id)
                )
            ).scalar_one()
            return int(row)
    finally:
        await engine.dispose()


async def _count_events(tenant_id: str) -> int:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            return int(
                await conn.scalar(
                    select(func.count())
                    .select_from(user_events)
                    .where(user_events.c.tenant_id == tenant_id)
                )
            )
    finally:
        await engine.dispose()


class _StatementCounter:
    """Counts DELETE and SELECT statements against user_events."""

    def __init__(self, engine) -> None:
        self.deletes = 0
        self.selects = 0

        def _hook(conn, cursor, statement, parameters, context, executemany):
            if "user_events" not in statement:
                return
            head = statement.lstrip()[:6].upper()
            if head == "DELETE":
                self.deletes += 1
            elif head.startswith(("SELECT", "WITH")):
                # A CTE query ("WITH bounds AS ...") is a read too -- the
                # single-snapshot page statement starts exactly like that.
                self.selects += 1

        event.listen(engine.sync_engine, "before_cursor_execute", _hook)


def test_retention_deletes_expired_rows_on_first_touch() -> None:
    async def scenario() -> None:
        await _clear_events()
        await _seed_event(TENANT_A, USER_A, age_seconds=120.0)
        fresh_id = await _seed_event(TENANT_A, USER_A, age_seconds=0.0)
        engine = build_engine(TEST_DATABASE_URL)
        try:
            store = PostgresUserEventStore(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
                retention_seconds=60.0,
            )
            page = await store.page_after(
                tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
            )
        finally:
            await engine.dispose()
        assert [e.id for e in page.events] == [fresh_id], (
            "the expired row must be gone, the fresh one delivered"
        )
        assert await _count_events(TENANT_A) == 1
        await _wipe_events()

    _run(scenario())


def test_cleanup_is_coalesced_to_one_delete_per_interval() -> None:
    """25 polls, ONE retention DELETE -- then a new interval allows one more.

    Without coalescing, every poll of every open tab issues this tenant-wide
    DELETE (a sequential scan). This is the regression pin: revert any
    call site to an unconditional cleanup and the count here explodes.
    Deterministic by design: the interval is far larger than any test
    runtime, and the second window is opened by rewinding the deadline
    instead of sleeping -- a wall-clock budget would let a slow CI
    database produce the very defect this test guards against.
    """

    async def scenario() -> None:
        await _clear_events()
        await _seed_event(TENANT_A, USER_A, age_seconds=0.0)
        engine = build_engine(TEST_DATABASE_URL)
        try:
            counter = _StatementCounter(engine)
            store = PostgresUserEventStore(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
                cleanup_interval_seconds=3600.0,
            )
            for _ in range(25):
                await store.page_after(
                    tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
                )
            assert counter.deletes == 1, (
                f"expected one coalesced DELETE, saw {counter.deletes}"
            )
            store._cleanup_due[TENANT_A] = float("-inf")
            await store.page_after(
                tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
            )
            assert counter.deletes == 2, (
                "an elapsed interval must allow exactly one more DELETE"
            )
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def test_page_after_reads_bounds_and_rows_in_one_select() -> None:
    """Bounds and page rows must share ONE snapshot, i.e. one statement.

    Split into two SELECTs, a concurrent process's retention DELETE can
    commit in between: bounds computed before the sweep, rows after it --
    a partial replay delivered as complete, invisible to the reset check.
    """

    async def scenario() -> None:
        await _clear_events()
        await _seed_event(TENANT_A, USER_A, age_seconds=0.0)
        engine = build_engine(TEST_DATABASE_URL)
        try:
            counter = _StatementCounter(engine)
            store = PostgresUserEventStore(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
                cleanup_interval_seconds=3600.0,
            )
            # First call consumes the cleanup slot for this tenant.
            await store.page_after(
                tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
            )
            before = counter.selects
            page = await store.page_after(
                tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
            )
            assert len(page.events) == 1
            assert counter.selects - before == 1, (
                "bounds and rows must travel in one SELECT; "
                f"saw {counter.selects - before}"
            )
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def test_cleanup_scopes_to_the_calling_tenant_without_rls() -> None:
    """The explicit tenant filter must hold on an RLS-free connection.

    app_role="" skips the role switch, so the session keeps the
    superuser's BYPASSRLS -- if the DELETE relied on the RLS policy
    alone, tenant A's cleanup would erase tenant B's expired rows here.
    """

    async def scenario() -> None:
        await _clear_events()
        await _seed_event(TENANT_A, USER_A, age_seconds=120.0)
        await _seed_event(TENANT_B, USER_B, age_seconds=120.0)
        engine = build_engine(TEST_DATABASE_URL)
        try:
            store = PostgresUserEventStore(
                session_factory=build_session_factory(engine),
                app_role="",
                retention_seconds=60.0,
            )
            await store.page_after(
                tenant_id=TENANT_A, target_user_id=USER_A, cursor=0
            )
            assert await _count_events(TENANT_A) == 0
            assert await _count_events(TENANT_B) == 1, (
                "tenant A's cleanup must never touch tenant B's rows"
            )
            # And the per-tenant deadline map: B's FIRST touch still
            # cleans B -- A's cleanup must not have consumed B's slot.
            await store.page_after(
                tenant_id=TENANT_B, target_user_id=USER_B, cursor=0
            )
            assert await _count_events(TENANT_B) == 0
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def _poll_store(engine, *, app_role: str = APP_ROLE, poll_seconds: float = 0.1):
    return PostgresUserEventStore(
        session_factory=build_session_factory(engine),
        app_role=app_role,
        poll_seconds=poll_seconds,
    )


def test_waiting_streams_of_a_tenant_share_one_poll_per_interval() -> None:
    """20 open streams, one read per interval -- not one per stream."""

    async def scenario() -> None:
        await _clear_events()
        engine = build_engine(TEST_DATABASE_URL)
        try:
            counter = _StatementCounter(engine)
            store = _poll_store(engine, poll_seconds=0.1)
            await asyncio.gather(
                *(
                    store.wait_for_change(
                        tenant_id=TENANT_A,
                        target_user_id=USER_A if index % 2 else USER_C,
                        cursor=0,
                        timeout=1.0,
                    )
                    for index in range(20)
                )
            )
            assert counter.selects <= 12, (
                f"expected about one poll per interval, saw {counter.selects}"
            )
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def test_an_event_wakes_only_the_streams_of_its_user_within_two_intervals() -> None:
    async def scenario() -> None:
        await _clear_events()
        engine = build_engine(TEST_DATABASE_URL)
        try:
            store = _poll_store(engine, poll_seconds=0.1)
            loop = asyncio.get_running_loop()
            woken: dict[str, float] = {}

            async def wait(name: str, user: uuid.UUID) -> None:
                await store.wait_for_change(
                    tenant_id=TENANT_A, target_user_id=user, cursor=0, timeout=1.5
                )
                woken[name] = loop.time()

            waiting = [
                asyncio.create_task(wait("first tab", USER_A)),
                asyncio.create_task(wait("second tab", USER_A)),
                asyncio.create_task(wait("other user", USER_C)),
            ]
            await asyncio.sleep(0.25)
            appended_at = loop.time()
            await store.append(
                tenant_id=TENANT_A, target_user_id=USER_A, scope="test"
            )
            await asyncio.gather(*waiting)

            assert woken["first tab"] - appended_at <= 0.2 + 0.1
            assert woken["second tab"] - appended_at <= 0.2 + 0.1
            assert woken["other user"] - appended_at >= 1.0
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def test_an_event_of_another_tenant_never_wakes_a_stream_without_rls() -> None:
    """app_role="" keeps the superuser's BYPASSRLS, so only the poll's own
    tenant filter can keep tenant B's event from waking tenant A."""

    async def scenario() -> None:
        await _clear_events()
        engine = build_engine(TEST_DATABASE_URL)
        try:
            store = _poll_store(engine, app_role="", poll_seconds=0.05)
            loop = asyncio.get_running_loop()
            started = loop.time()
            waiting = asyncio.create_task(
                store.wait_for_change(
                    tenant_id=TENANT_A, target_user_id=USER_A, cursor=0, timeout=0.6
                )
            )
            await asyncio.sleep(0.1)
            await _seed_event(TENANT_B, USER_A, age_seconds=0.0)
            await waiting
            assert loop.time() - started >= 0.55
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())


def test_the_change_poll_probes_each_user_without_the_stream_cursor() -> None:
    """One index probe per waiting user: the poll reads each user's newest
    event, so an old cursor never makes it walk the events since then."""

    async def scenario() -> None:
        await _clear_events()
        for _ in range(3):
            await _seed_event(TENANT_A, USER_A, age_seconds=0.0)
        engine = build_engine(TEST_DATABASE_URL)
        try:
            seen: list[tuple[str, object]] = []

            def record(conn, cursor, statement, parameters, context, executemany):
                if "user_events" in statement:
                    seen.append((statement, parameters))

            event.listen(engine.sync_engine, "before_cursor_execute", record)
            store = _poll_store(engine, poll_seconds=0.05)
            await store.wait_for_change(
                tenant_id=TENANT_A,
                target_user_id=USER_A,
                cursor=1,
                timeout=1.0,
            )
            polls = [
                (statement, parameters)
                for statement, parameters in seen
                if "LIMIT 1" in statement.upper()
            ]
            assert polls, "the woken stream must have been polled"
            for statement, parameters in polls:
                assert "ORDER BY" in statement.upper()
                assert 1 not in tuple(parameters or ()), (
                    "the poll must not filter by the stream cursor"
                )
        finally:
            await engine.dispose()
            await _wipe_events()

    _run(scenario())

