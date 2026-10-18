"""Postgres integration tests for the quota store (gated suite).

Parity with the memory store: atomic increment, lazy monthly rollover
that leaves history intact, stock-release clamping, reset, and limit
round-trip — all under the restricted app role with RLS as the second
defense.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.quota.models import (
    DEFAULT_USER_ID,
    STOCK_PERIOD,
    QuotaAdjustmentConflict,
    QuotaDimension,
    current_period_start,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.quota_postgres import PostgresQuotaStore
from tests.storage._canonical_users import canonical_user_id, ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_ID = canonical_user_id("quota-user")
USAGE_USER_ID = canonical_user_id("quota-usage-user")
LIMIT_USER_ID = canonical_user_id("quota-limit-user")
OWNER_ID = canonical_user_id("quota-admin")

JUNE = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc).timestamp()
JULY = dt.datetime(2026, 7, 2, 9, 0, tzinfo=dt.timezone.utc).timestamp()


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def store():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM quota_stock_lifecycles"))
            await session.execute(text("DELETE FROM quota_usage_adjustments"))
            await session.execute(text("DELETE FROM quota_usage_counters"))
            await session.execute(text("DELETE FROM quota_limits"))
            await ensure_canonical_users(
                session,
                (USER_ID, USAGE_USER_ID, LIMIT_USER_ID, OWNER_ID),
            )
    await engine.dispose()
    store = PostgresQuotaStore(database_url=TEST_DATABASE_URL, app_role=APP_ROLE)
    yield store
    # The store owns its NullPool engine; dispose it so the gated suite
    # leaves no engine lingering (parity with the other repo fixtures).
    await store.aclose()


@pytest.mark.asyncio
async def test_usage_and_limits_are_read_in_one_tenant_session(store, monkeypatch):
    """The meter's read costs one session, not one for usage and one for limits.

    Each session is a connection checkout plus role and tenant setup, and the
    meter asks every 30 seconds per visible tab.
    """
    june = current_period_start(JUNE)
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        period_start=june,
        amount=2,
    )
    for subject, value in ((USER_ID, 7), (DEFAULT_USER_ID, 3)):
        await store.set_limit(
            tenant_id="default",
            subject_user_id=subject,
            dimension=QuotaDimension.RUNS,
            value=value,
            set_by_user_id=OWNER_ID,
        )
    expected_usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JUNE,
    )
    expected_limits = await store.get_limits(
        tenant_id="default",
        subject_user_ids=[USER_ID, DEFAULT_USER_ID],
        dimensions=[QuotaDimension.RUNS],
    )
    opened: list[str] = []
    session = store._session

    def counting_session(tenant_id):
        opened.append(tenant_id)
        return session(tenant_id)

    monkeypatch.setattr(store, "_session", counting_session)

    usage, limits = await store.read_usage_with_limits(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        limit_subject_user_ids=[USER_ID, DEFAULT_USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JUNE,
    )

    assert (usage, limits) == (expected_usage, expected_limits)
    assert usage[USER_ID][QuotaDimension.RUNS] == 2
    assert limits[USER_ID][QuotaDimension.RUNS] == 7
    assert limits[DEFAULT_USER_ID][QuotaDimension.RUNS] == 3
    assert opened == ["default"]


@pytest.mark.asyncio
async def test_increment_accumulates_and_returns_total(store):
    june = current_period_start(JUNE)
    assert (
        await store.add_usage(
            tenant_id="default",
            subject_user_id=USER_ID,
            dimension=QuotaDimension.RUNS,
            period_start=june,
            amount=1,
        )
        == 1
    )
    assert (
        await store.add_usage(
            tenant_id="default",
            subject_user_id=USER_ID,
            dimension=QuotaDimension.RUNS,
            period_start=june,
            amount=2,
        )
        == 3
    )
    usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JUNE,
    )
    assert usage[USER_ID][QuotaDimension.RUNS] == 3


@pytest.mark.asyncio
async def test_adjustment_receipt_is_idempotent_across_month_rollover(store):
    june = current_period_start(JUNE)
    july = current_period_start(JULY)
    adjustment = {
        "adjustment_id": "pg-embedding-work:stable",
        "tenant_id": "default",
        "subject_user_id": USER_ID,
        "dimension": QuotaDimension.EMBEDDING_TOKENS,
        "amount": 17,
    }
    assert await store.add_usage_once(**adjustment, period_start=june) == 17
    assert await store.add_usage_once(**adjustment, period_start=june) == 17
    assert await store.add_usage_once(**adjustment, period_start=july) == 0
    june_usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.EMBEDDING_TOKENS],
        now=JUNE,
    )
    july_usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.EMBEDDING_TOKENS],
        now=JULY,
    )
    assert june_usage[USER_ID][QuotaDimension.EMBEDDING_TOKENS] == 17
    assert july_usage[USER_ID][QuotaDimension.EMBEDDING_TOKENS] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contradiction",
    [
        {"amount": 18},
        {"subject_user_id": USAGE_USER_ID},
        {"dimension": QuotaDimension.LLM_TOKENS},
        {"tenant_id": "another-tenant"},
    ],
)
async def test_adjustment_receipt_rejects_contradictory_replay(store, contradiction):
    june = current_period_start(JUNE)
    original = {
        "adjustment_id": "pg-embedding-work:conflict",
        "tenant_id": "default",
        "subject_user_id": USER_ID,
        "dimension": QuotaDimension.EMBEDDING_TOKENS,
        "period_start": june,
        "amount": 17,
    }
    await store.add_usage_once(**original)
    with pytest.raises(QuotaAdjustmentConflict):
        await store.add_usage_once(**{**original, **contradiction})
    usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.EMBEDDING_TOKENS],
        now=JUNE,
    )
    assert usage[USER_ID][QuotaDimension.EMBEDDING_TOKENS] == 17


@pytest.mark.asyncio
async def test_monthly_rollover_keeps_history(store):
    june = current_period_start(JUNE)
    july = current_period_start(JULY)
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        period_start=june,
        amount=5,
    )
    # New window reads 0; June row survives as history.
    july_usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JULY,
    )
    assert july_usage[USER_ID][QuotaDimension.RUNS] == 0
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        period_start=july,
        amount=1,
    )
    june_usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JUNE,
    )
    assert june_usage[USER_ID][QuotaDimension.RUNS] == 5


@pytest.mark.asyncio
async def test_stock_release_clamps_at_zero(store):
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.STORED_BYTES,
        period_start=STOCK_PERIOD,
        amount=1000,
    )
    assert (
        await store.add_usage(
            tenant_id="default",
            subject_user_id=USER_ID,
            dimension=QuotaDimension.STORED_BYTES,
            period_start=STOCK_PERIOD,
            amount=-1500,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_resource_stock_tombstone_wins_in_either_order(store):
    live = await store.reconcile_stock(
        stock_key="file:fl_pg_stock",
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.STORED_BYTES,
        desired_amount=321,
        tombstone=False,
    )
    assert live.amount == 321 and not live.tombstoned

    deleted = await store.reconcile_stock(
        stock_key="file:fl_pg_stock",
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.STORED_BYTES,
        desired_amount=0,
        tombstone=True,
    )
    late = await store.reconcile_stock(
        stock_key="file:fl_pg_stock",
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.STORED_BYTES,
        desired_amount=321,
        tombstone=False,
    )

    assert deleted == late
    assert (
        await store.read_stock(stock_key="file:fl_pg_stock", tenant_id="default")
        == deleted
    )
    usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.STORED_BYTES],
        now=JUNE,
    )
    assert usage[USER_ID][QuotaDimension.STORED_BYTES] == 0


@pytest.mark.asyncio
async def test_reset_zeroes_current_window(store):
    june = current_period_start(JUNE)
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        period_start=june,
        amount=200,
    )
    await store.reset_usage(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        now=JUNE,
    )
    usage = await store.read_usage(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
        now=JUNE,
    )
    assert usage[USER_ID][QuotaDimension.RUNS] == 0


@pytest.mark.asyncio
async def test_limits_roundtrip_and_clear(store):
    await store.set_limit(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        value=400,
        set_by_user_id=OWNER_ID,
    )
    await store.set_limit(
        tenant_id="default",
        subject_user_id=DEFAULT_USER_ID,
        dimension=QuotaDimension.RUNS,
        value=200,
        set_by_user_id=OWNER_ID,
    )
    limits = await store.get_limits(
        tenant_id="default",
        subject_user_ids=[USER_ID, DEFAULT_USER_ID],
        dimensions=[QuotaDimension.RUNS],
    )
    assert limits[USER_ID][QuotaDimension.RUNS] == 400
    assert limits[DEFAULT_USER_ID][QuotaDimension.RUNS] == 200
    # Re-set overwrites (upsert), not duplicates.
    await store.set_limit(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
        value=450,
        set_by_user_id=OWNER_ID,
    )
    limits = await store.get_limits(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
    )
    assert limits[USER_ID][QuotaDimension.RUNS] == 450
    await store.clear_limit(
        tenant_id="default",
        subject_user_id=USER_ID,
        dimension=QuotaDimension.RUNS,
    )
    after = await store.get_limits(
        tenant_id="default",
        subject_user_ids=[USER_ID],
        dimensions=[QuotaDimension.RUNS],
    )
    assert USER_ID not in after


@pytest.mark.asyncio
async def test_reset_rejects_stock_dimension(store):
    with pytest.raises(ValueError):
        await store.reset_usage(
            tenant_id="default",
            subject_user_id=USER_ID,
            dimension=QuotaDimension.STORED_BYTES,
            now=JUNE,
        )


@pytest.mark.asyncio
async def test_list_subjects_unions_usage_and_limits(store):
    june = current_period_start(JUNE)
    await store.add_usage(
        tenant_id="default",
        subject_user_id=USAGE_USER_ID,
        dimension=QuotaDimension.RUNS,
        period_start=june,
        amount=1,
    )
    await store.set_limit(
        tenant_id="default",
        subject_user_id=LIMIT_USER_ID,
        dimension=QuotaDimension.RUNS,
        value=5,
        set_by_user_id=OWNER_ID,
    )
    # The tenant default sentinel must NOT appear as a subject.
    await store.set_limit(
        tenant_id="default",
        subject_user_id=DEFAULT_USER_ID,
        dimension=QuotaDimension.RUNS,
        value=2,
        set_by_user_id=OWNER_ID,
    )
    subs = await store.list_subjects(tenant_id="default")
    assert set(subs) == {USAGE_USER_ID, LIMIT_USER_ID}
    assert DEFAULT_USER_ID not in subs
