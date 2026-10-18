"""Order-independent resource stock accounting contracts."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from inqtrix.quota.memory import MemoryQuotaStore
from inqtrix.quota.models import QuotaDimension

USER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _used(store: MemoryQuotaStore) -> int:
    usage = asyncio.run(
        store.read_usage(
            tenant_id="default",
            subject_user_ids=[USER],
            dimensions=[QuotaDimension.STORED_BYTES],
            now=0,
        )
    )
    return usage[USER][QuotaDimension.STORED_BYTES]


def _reconcile(
    store: MemoryQuotaStore,
    *,
    amount: int,
    tombstone: bool,
):
    return asyncio.run(
        store.reconcile_stock(
            stock_key="file:fl_stock",
            tenant_id="default",
            subject_user_id=USER,
            dimension=QuotaDimension.STORED_BYTES,
            desired_amount=amount,
            tombstone=tombstone,
        )
    )


def test_upload_then_delete_and_late_upload_converge_to_tombstone() -> None:
    store = MemoryQuotaStore()

    assert _reconcile(store, amount=128, tombstone=False).amount == 128
    assert _used(store) == 128
    deleted = _reconcile(store, amount=0, tombstone=True)
    assert deleted.amount == 0 and deleted.tombstoned
    assert _used(store) == 0

    late = _reconcile(store, amount=128, tombstone=False)
    assert late.amount == 0 and late.tombstoned
    assert _used(store) == 0


def test_delete_before_upload_and_duplicate_retries_never_charge() -> None:
    store = MemoryQuotaStore()

    first = _reconcile(store, amount=0, tombstone=True)
    second = _reconcile(store, amount=0, tombstone=True)
    late_upload = _reconcile(store, amount=512, tombstone=False)

    assert first == second == late_upload
    assert _used(store) == 0


def test_live_stock_retry_can_reconcile_exact_amount_without_double_charge() -> None:
    store = MemoryQuotaStore()

    _reconcile(store, amount=64, tombstone=False)
    _reconcile(store, amount=64, tombstone=False)
    _reconcile(store, amount=96, tombstone=False)

    assert _used(store) == 96


def test_stock_key_cannot_move_between_quota_subjects() -> None:
    store = MemoryQuotaStore()
    _reconcile(store, amount=64, tombstone=False)

    with pytest.raises(ValueError, match="another quota subject"):
        asyncio.run(
            store.reconcile_stock(
                stock_key="file:fl_stock",
                tenant_id="default",
                subject_user_id=uuid.uuid4(),
                dimension=QuotaDimension.STORED_BYTES,
                desired_amount=64,
                tombstone=False,
            )
        )

    assert _used(store) == 64
