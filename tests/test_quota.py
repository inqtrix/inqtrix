"""Quota domain model + the in-memory store.

Pins the resolution rules (the three layers under the operator ceiling),
the calendar-month window math, lazy rollover, atomic increment with
stock-release clamping, reset, and limit storage.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from inqtrix.quota.memory import MemoryQuotaStore
from inqtrix.quota.models import (
    DEFAULT_USER_ID,
    STOCK_PERIOD,
    DimensionUsage,
    QuotaAdjustmentConflict,
    QuotaDimension,
    QuotaExceeded,
    current_period_start,
    effective_limit,
    period_end,
)

JUNE = dt.datetime(2026, 6, 13, 12, 0, tzinfo=dt.timezone.utc).timestamp()
JULY = dt.datetime(2026, 7, 2, 9, 0, tzinfo=dt.timezone.utc).timestamp()
DEC = dt.datetime(2026, 12, 20, 0, 0, tzinfo=dt.timezone.utc).timestamp()
USER_1 = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
OWNER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_USAGE = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
USER_LIMIT = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


# ---------------------------------------------------------------------------
# Period math
# ---------------------------------------------------------------------------


def test_period_start_is_month_first_utc():
    start = current_period_start(JUNE)
    assert dt.datetime.fromtimestamp(start, tz=dt.timezone.utc) == dt.datetime(
        2026, 6, 1, tzinfo=dt.timezone.utc
    )


def test_period_end_rolls_into_next_month_and_year():
    june_start = current_period_start(JUNE)
    assert dt.datetime.fromtimestamp(
        period_end(june_start), tz=dt.timezone.utc
    ) == dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    dec_start = current_period_start(DEC)
    assert dt.datetime.fromtimestamp(
        period_end(dec_start), tz=dt.timezone.utc
    ) == dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)


def test_stock_period_has_no_end():
    assert period_end(STOCK_PERIOD) == 0.0


# ---------------------------------------------------------------------------
# Effective-limit resolution
# ---------------------------------------------------------------------------


def test_override_wins_over_default_wins_over_env():
    assert (
        effective_limit(
            override=300, tenant_default=200, env_default=100, env_ceiling=0
        )
        == 300
    )
    assert (
        effective_limit(
            override=None, tenant_default=200, env_default=100, env_ceiling=0
        )
        == 200
    )
    assert (
        effective_limit(
            override=None, tenant_default=None, env_default=100, env_ceiling=0
        )
        == 100
    )


def test_zero_is_explicit_unlimited_not_fall_through():
    # An admin who sets a user to 0 means unlimited (capped only by the
    # ceiling), NOT "fall through to the tenant default".
    assert (
        effective_limit(override=0, tenant_default=200, env_default=100, env_ceiling=0)
        is None
    )
    assert (
        effective_limit(
            override=0, tenant_default=200, env_default=100, env_ceiling=500
        )
        == 500
    )


def test_ceiling_caps_every_layer():
    assert (
        effective_limit(
            override=900, tenant_default=None, env_default=0, env_ceiling=500
        )
        == 500
    )
    assert (
        effective_limit(
            override=None, tenant_default=None, env_default=0, env_ceiling=500
        )
        == 500
    )  # env_default unlimited, ceiling still binds
    assert (
        effective_limit(
            override=None, tenant_default=None, env_default=0, env_ceiling=0
        )
        is None
    )  # nothing set anywhere -> unlimited


# ---------------------------------------------------------------------------
# DimensionUsage view
# ---------------------------------------------------------------------------


def test_dimension_usage_remaining_and_allows():
    capped = DimensionUsage(
        dimension=QuotaDimension.RUNS, used=198, limit=200, period_start=JUNE
    )
    assert capped.remaining == 2
    assert capped.allows(2) is True
    assert capped.allows(3) is False
    unlimited = DimensionUsage(
        dimension=QuotaDimension.RUNS, used=999, limit=None, period_start=JUNE
    )
    assert unlimited.remaining is None
    assert unlimited.allows(10_000) is True


def test_quota_exceeded_carries_facts():
    exc = QuotaExceeded(
        dimension=QuotaDimension.LLM_TOKENS,
        limit=5_000_000,
        used=5_000_000,
        reset_at=period_end(current_period_start(JUNE)),
    )
    assert exc.dimension is QuotaDimension.LLM_TOKENS
    assert exc.limit == 5_000_000
    assert "llm_tokens" in str(exc)


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------


@pytest.fixture()
def store():
    return MemoryQuotaStore()


def _add(store, user_id: uuid.UUID, dim, period, amount):
    return asyncio.run(
        store.add_usage(
            tenant_id="default",
            subject_user_id=user_id,
            dimension=dim,
            period_start=period,
            amount=amount,
        )
    )


def _read(store, user_ids: list[uuid.UUID], dims, now):
    return asyncio.run(
        store.read_usage(
            tenant_id="default",
            subject_user_ids=user_ids,
            dimensions=dims,
            now=now,
        )
    )


def test_increment_accumulates_within_a_window(store):
    june = current_period_start(JUNE)
    assert _add(store, USER_1, QuotaDimension.RUNS, june, 1) == 1
    assert _add(store, USER_1, QuotaDimension.RUNS, june, 1) == 2
    usage = _read(store, [USER_1], [QuotaDimension.RUNS], JUNE)
    assert usage[USER_1][QuotaDimension.RUNS] == 2


def test_adjustment_receipt_is_idempotent_across_month_rollover(store):
    june = current_period_start(JUNE)
    july = current_period_start(JULY)
    adjustment = {
        "adjustment_id": "embedding-work:stable",
        "tenant_id": "default",
        "subject_user_id": USER_1,
        "dimension": QuotaDimension.EMBEDDING_TOKENS,
        "amount": 17,
    }
    assert asyncio.run(store.add_usage_once(**adjustment, period_start=june)) == 17
    assert asyncio.run(store.add_usage_once(**adjustment, period_start=june)) == 17
    assert asyncio.run(store.add_usage_once(**adjustment, period_start=july)) == 0
    assert (
        _read(store, [USER_1], [QuotaDimension.EMBEDDING_TOKENS], JUNE)[USER_1][
            QuotaDimension.EMBEDDING_TOKENS
        ]
        == 17
    )
    assert (
        _read(store, [USER_1], [QuotaDimension.EMBEDDING_TOKENS], JULY)[USER_1][
            QuotaDimension.EMBEDDING_TOKENS
        ]
        == 0
    )


@pytest.mark.parametrize(
    "contradiction",
    [
        {"amount": 18},
        {"subject_user_id": USER_2},
        {"dimension": QuotaDimension.LLM_TOKENS},
        {"tenant_id": "another-tenant"},
    ],
)
def test_adjustment_receipt_rejects_contradictory_replay(store, contradiction):
    june = current_period_start(JUNE)
    original = {
        "adjustment_id": "embedding-work:conflict",
        "tenant_id": "default",
        "subject_user_id": USER_1,
        "dimension": QuotaDimension.EMBEDDING_TOKENS,
        "period_start": june,
        "amount": 17,
    }
    asyncio.run(store.add_usage_once(**original))
    with pytest.raises(QuotaAdjustmentConflict):
        asyncio.run(store.add_usage_once(**{**original, **contradiction}))
    assert (
        _read(store, [USER_1], [QuotaDimension.EMBEDDING_TOKENS], JUNE)[USER_1][
            QuotaDimension.EMBEDDING_TOKENS
        ]
        == 17
    )


def test_lazy_rollover_starts_next_month_at_zero(store):
    june = current_period_start(JUNE)
    july = current_period_start(JULY)
    _add(store, USER_1, QuotaDimension.RUNS, june, 5)
    # July reads as 0 (the June counter belongs to a past window) without
    # being rewritten.
    assert (
        _read(store, [USER_1], [QuotaDimension.RUNS], JULY)[USER_1][QuotaDimension.RUNS]
        == 0
    )
    assert _add(store, USER_1, QuotaDimension.RUNS, july, 1) == 1
    # June's history is untouched.
    assert (
        _read(store, [USER_1], [QuotaDimension.RUNS], JUNE)[USER_1][QuotaDimension.RUNS]
        == 5
    )


def test_stock_release_clamps_at_zero_and_never_rolls(store):
    _add(store, USER_1, QuotaDimension.STORED_BYTES, STOCK_PERIOD, 1000)
    assert _add(store, USER_1, QuotaDimension.STORED_BYTES, STOCK_PERIOD, -400) == 600
    # Over-release clamps at 0, not negative.
    assert _add(store, USER_1, QuotaDimension.STORED_BYTES, STOCK_PERIOD, -5000) == 0
    # Stock ignores the calendar month entirely.
    assert (
        _read(store, [USER_1], [QuotaDimension.STORED_BYTES], JULY)[USER_1][
            QuotaDimension.STORED_BYTES
        ]
        == 0
    )


def test_reset_zeroes_current_window(store):
    june = current_period_start(JUNE)
    _add(store, USER_1, QuotaDimension.RUNS, june, 200)
    asyncio.run(
        store.reset_usage(
            tenant_id="default",
            subject_user_id=USER_1,
            dimension=QuotaDimension.RUNS,
            now=JUNE,
        )
    )
    assert (
        _read(store, [USER_1], [QuotaDimension.RUNS], JUNE)[USER_1][QuotaDimension.RUNS]
        == 0
    )


def test_reset_rejects_stock_dimension(store):
    """Stock is freed by deletion, never reset — the store enforces it."""
    with pytest.raises(ValueError):
        asyncio.run(
            store.reset_usage(
                tenant_id="default",
                subject_user_id=USER_1,
                dimension=QuotaDimension.STORED_BYTES,
                now=JUNE,
            )
        )


def test_limits_roundtrip_and_clear(store):
    asyncio.run(
        store.set_limit(
            tenant_id="default",
            subject_user_id=USER_1,
            dimension=QuotaDimension.RUNS,
            value=400,
            set_by_user_id=OWNER,
        )
    )
    asyncio.run(
        store.set_limit(
            tenant_id="default",
            subject_user_id=DEFAULT_USER_ID,
            dimension=QuotaDimension.RUNS,
            value=200,
            set_by_user_id=OWNER,
        )
    )
    limits = asyncio.run(
        store.get_limits(
            tenant_id="default",
            subject_user_ids=[USER_1, DEFAULT_USER_ID],
            dimensions=[QuotaDimension.RUNS],
        )
    )
    assert limits[USER_1][QuotaDimension.RUNS] == 400
    assert limits[DEFAULT_USER_ID][QuotaDimension.RUNS] == 200

    asyncio.run(
        store.clear_limit(
            tenant_id="default",
            subject_user_id=USER_1,
            dimension=QuotaDimension.RUNS,
        )
    )
    after = asyncio.run(
        store.get_limits(
            tenant_id="default",
            subject_user_ids=[USER_1],
            dimensions=[QuotaDimension.RUNS],
        )
    )
    assert USER_1 not in after


def test_usage_isolated_per_tenant_and_subject(store):
    june = current_period_start(JUNE)
    _add(store, USER_1, QuotaDimension.RUNS, june, 3)
    usage = _read(store, [USER_1, USER_2], [QuotaDimension.RUNS], JUNE)
    assert usage[USER_1][QuotaDimension.RUNS] == 3
    assert usage[USER_2][QuotaDimension.RUNS] == 0


def test_list_subjects_unions_usage_and_limits(store):
    """The admin-overview source: usage subjects union limit subjects,
    with the tenant-default sentinel excluded (memory/Postgres parity)."""
    june = current_period_start(JUNE)
    _add(store, USER_USAGE, QuotaDimension.RUNS, june, 1)
    asyncio.run(
        store.set_limit(
            tenant_id="default",
            subject_user_id=USER_LIMIT,
            dimension=QuotaDimension.RUNS,
            value=5,
            set_by_user_id=OWNER,
        )
    )
    asyncio.run(
        store.set_limit(
            tenant_id="default",
            subject_user_id=DEFAULT_USER_ID,
            dimension=QuotaDimension.RUNS,
            value=2,
            set_by_user_id=OWNER,
        )
    )
    user_ids = asyncio.run(store.list_subjects(tenant_id="default"))
    assert set(user_ids) == {USER_USAGE, USER_LIMIT}
    assert DEFAULT_USER_ID not in user_ids
