"""In-process quota store: the zero-infrastructure default.

Thread-safe like the other memory backends. Counters are keyed by
``(tenant_id, subject_user_id, dimension, period_start)`` — exactly the
Postgres primary key — so both backends keep per-window history and
read the active window identically; lazy rollover falls out of the
keying (a new month is a new key at 0). Limits live in a parallel
dict. All check-then-write happens under the lock, so increments stay
atomic.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Sequence

from inqtrix.quota.models import (
    DEFAULT_USER_ID,
    STOCK_PERIOD,
    QuotaAdjustmentConflict,
    QuotaDimension,
    QuotaSubject,
    StockLifecycleState,
    current_period_start,
)

_CounterKey = tuple[str, uuid.UUID, QuotaDimension, float]
_LimitKey = tuple[str, uuid.UUID | None, QuotaDimension]


@dataclass(frozen=True)
class _AdjustmentReceipt:
    tenant_id: str
    subject_user_id: uuid.UUID
    dimension: QuotaDimension
    period_start: float
    amount: int


def _active_period(dimension: QuotaDimension, now: float) -> float:
    return STOCK_PERIOD if dimension.is_stock else current_period_start(now)


class MemoryQuotaStore:
    """Thread-safe in-process implementation of the quota store port."""

    def __init__(self) -> None:
        self._counters: dict[_CounterKey, int] = {}
        self._limits: dict[_LimitKey, int] = {}
        self._adjustments: dict[str, _AdjustmentReceipt] = {}
        self._stock: dict[tuple[str, str], StockLifecycleState] = {}
        self._lock = threading.RLock()

    async def add_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        period_start: float,
        amount: int,
    ) -> int:
        key: _CounterKey = (tenant_id, subject_user_id, dimension, period_start)
        with self._lock:
            # max(0, ...) clamps a stock release; a new window is simply
            # an unseen key that starts at 0.
            new_used = max(0, self._counters.get(key, 0) + amount)
            self._counters[key] = new_used
            return new_used

    async def add_usage_once(
        self,
        *,
        adjustment_id: str,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        period_start: float,
        amount: int,
    ) -> int:
        counter_key: _CounterKey = (
            tenant_id,
            subject_user_id,
            dimension,
            period_start,
        )
        proposed = _AdjustmentReceipt(
            tenant_id=tenant_id,
            subject_user_id=subject_user_id,
            dimension=dimension,
            period_start=period_start,
            amount=amount,
        )
        with self._lock:
            existing = self._adjustments.get(adjustment_id)
            if existing is not None:
                if (
                    existing.tenant_id != proposed.tenant_id
                    or existing.subject_user_id != proposed.subject_user_id
                    or existing.dimension != proposed.dimension
                    or existing.amount != proposed.amount
                ):
                    raise QuotaAdjustmentConflict(adjustment_id)
                # A replay after calendar rollover remains attached to the
                # original receipt period and does not charge the new month.
                return self._counters.get(counter_key, 0)
            new_used = max(0, self._counters.get(counter_key, 0) + amount)
            self._counters[counter_key] = new_used
            self._adjustments[adjustment_id] = proposed
            return new_used

    async def reconcile_stock(
        self,
        *,
        stock_key: str,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        desired_amount: int,
        tombstone: bool,
    ) -> StockLifecycleState:
        """Converge a resource contribution and counter under one lock."""

        if not dimension.is_stock:
            raise ValueError("stock lifecycle requires a stock dimension")
        if desired_amount < 0:
            raise ValueError("stock amount cannot be negative")
        if not stock_key.strip():
            raise ValueError("stock key cannot be empty")
        lifecycle_key = (tenant_id, stock_key)
        counter_key: _CounterKey = (
            tenant_id,
            subject_user_id,
            dimension,
            STOCK_PERIOD,
        )
        subject = QuotaSubject(tenant_id=tenant_id, user_id=subject_user_id)
        with self._lock:
            current = self._stock.get(lifecycle_key)
            if current is not None and (
                current.subject != subject or current.dimension is not dimension
            ):
                raise ValueError("stock key already belongs to another quota subject")
            current_amount = current.amount if current is not None else 0
            is_tombstoned = tombstone or bool(current and current.tombstoned)
            next_amount = 0 if is_tombstoned else desired_amount
            state = StockLifecycleState(
                stock_key=stock_key,
                subject=subject,
                dimension=dimension,
                amount=next_amount,
                tombstoned=is_tombstoned,
            )
            self._stock[lifecycle_key] = state
            self._counters[counter_key] = max(
                0,
                self._counters.get(counter_key, 0) + next_amount - current_amount,
            )
            return state

    async def read_stock(
        self,
        *,
        stock_key: str,
        tenant_id: str,
    ) -> StockLifecycleState | None:
        """Read one resource stock under the same accounting lock."""

        with self._lock:
            return self._stock.get((tenant_id, stock_key))

    async def read_usage(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID],
        dimensions: Sequence[QuotaDimension],
        now: float,
    ) -> dict[uuid.UUID, dict[QuotaDimension, int]]:
        result: dict[uuid.UUID, dict[QuotaDimension, int]] = {}
        with self._lock:
            for sub in subject_user_ids:
                result[sub] = {
                    dimension: self._counters.get(
                        (
                            tenant_id,
                            sub,
                            dimension,
                            _active_period(dimension, now),
                        ),
                        0,
                    )
                    for dimension in dimensions
                }
        return result

    async def reset_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        now: float,
    ) -> None:
        if dimension.is_stock:
            # Resetting a stock level to 0 would decouple it from real
            # object-store occupancy. Stock is freed by deletion, never
            # reset — a stock reset is a caller bug, surfaced loudly.
            raise ValueError(f"cannot reset stock dimension {dimension.value}")
        with self._lock:
            self._counters[
                (
                    tenant_id,
                    subject_user_id,
                    dimension,
                    _active_period(dimension, now),
                )
            ] = 0

    async def read_usage_with_limits(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID],
        limit_subject_user_ids: Sequence[uuid.UUID | None],
        dimensions: Sequence[QuotaDimension],
        now: float,
    ) -> tuple[
        dict[uuid.UUID, dict[QuotaDimension, int]],
        dict[uuid.UUID | None, dict[QuotaDimension, int]],
    ]:
        usage = await self.read_usage(
            tenant_id=tenant_id,
            subject_user_ids=subject_user_ids,
            dimensions=dimensions,
            now=now,
        )
        limits = await self.get_limits(
            tenant_id=tenant_id,
            subject_user_ids=limit_subject_user_ids,
            dimensions=dimensions,
        )
        return usage, limits

    async def get_limits(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID | None],
        dimensions: Sequence[QuotaDimension],
    ) -> dict[uuid.UUID | None, dict[QuotaDimension, int]]:
        wanted = set(dimensions)
        result: dict[uuid.UUID | None, dict[QuotaDimension, int]] = {}
        with self._lock:
            for (t_id, subject_user_id, dimension), value in self._limits.items():
                if (
                    t_id == tenant_id
                    and subject_user_id in subject_user_ids
                    and dimension in wanted
                ):
                    result.setdefault(subject_user_id, {})[dimension] = value
        return result

    async def set_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
        value: int,
        set_by_user_id: uuid.UUID,
    ) -> None:
        with self._lock:
            self._limits[(tenant_id, subject_user_id, dimension)] = value

    async def clear_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
    ) -> None:
        with self._lock:
            self._limits.pop((tenant_id, subject_user_id, dimension), None)

    async def list_subjects(self, *, tenant_id: str) -> list[uuid.UUID]:
        with self._lock:
            user_ids = {key[1] for key in self._counters if key[0] == tenant_id}
            user_ids.update(key[1] for key in self._limits if key[0] == tenant_id)
        user_ids.discard(DEFAULT_USER_ID)
        return sorted(user_ids)
