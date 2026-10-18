"""Persistence port for quota usage counters and limit overrides.

One port covers both because they are the same per-tenant concern and
always queried together by the service. Two implementations:
:class:`~inqtrix.quota.memory.MemoryQuotaStore` (zero-infrastructure
default) and the Postgres backend
(:class:`~inqtrix.storage.quota_postgres.PostgresQuotaStore`).
"""

from __future__ import annotations

import uuid
from typing import Protocol, Sequence, runtime_checkable

from inqtrix.quota.models import QuotaDimension, StockLifecycleState


@runtime_checkable
class QuotaStore(Protocol):
    """Atomic usage accounting + limit storage, per tenant."""

    async def add_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        period_start: float,
        amount: int,
    ) -> int:
        """Atomically add *amount* (may be negative for stock release)
        to the counter for the given window and return the new total.

        The window is selected entirely by the caller-supplied
        *period_start*: a new month is an unseen key (memory) / a fresh
        row (Postgres) starting at 0, so lazy rollover falls out of the
        keying — add_usage never compares against or rewrites a stale
        window. Stock totals never drop below 0.
        """
        ...

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
        """Apply one adjustment and counter update in the same transaction.

        Repeating ``adjustment_id`` with the same tenant, subject, dimension,
        and amount must return without changing usage again. A different
        ``period_start`` is a valid calendar-rollover replay and remains bound
        to the original receipt. Any other contradiction raises
        :class:`inqtrix.quota.models.QuotaAdjustmentConflict` without changing
        counters. This is the durable boundary used by retryable operations.
        """
        ...

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
        """Converge one resource stock and its aggregate counter atomically.

        A tombstone is permanent.  Calls that arrive after it may confirm the
        state but cannot charge the resource again.  Replays and either order
        of upload/delete therefore converge to the same counter value.
        """
        ...

    async def read_stock(
        self,
        *,
        stock_key: str,
        tenant_id: str,
    ) -> StockLifecycleState | None:
        """Read one canonical stock lifecycle state, if it exists."""
        ...

    async def read_usage(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID],
        dimensions: Sequence[QuotaDimension],
        now: float,
    ) -> dict[uuid.UUID, dict[QuotaDimension, int]]:
        """Current usage per subject per dimension for the active window.

        Flow dimensions report the current calendar month (a stale-period
        counter reads as 0 without being rewritten); stock dimensions
        report the running total. Missing counters are 0.
        """
        ...

    async def reset_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        now: float,
    ) -> None:
        """Zero a flow dimension's CURRENT-window counter (admin reset).

        Raises ``ValueError`` for a stock dimension: stock is freed by
        deletion, never reset, and zeroing it would decouple the counter
        from real occupancy. The contract is enforced in both backends,
        not left to caller discipline.
        """
        ...

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
        """:meth:`read_usage` and :meth:`get_limits` as ONE read.

        Admission, the meter and the admin overview always need both; a
        durable store answers them in one session instead of two.
        """
        ...

    async def get_limits(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID | None],
        dimensions: Sequence[QuotaDimension],
    ) -> dict[uuid.UUID | None, dict[QuotaDimension, int]]:
        """Stored limit values per subject per dimension.

        Only rows that exist are returned; absence means "fall through
        to the next layer" in :func:`~inqtrix.quota.models.effective_limit`.
        Pass ``DEFAULT_USER_ID`` among *subject_user_ids* to fetch the
        tenant-wide admin default alongside per-user overrides.
        """
        ...

    async def set_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
        value: int,
        set_by_user_id: uuid.UUID,
    ) -> None:
        """Upsert one limit (a per-user override or, with
        ``DEFAULT_USER_ID``, the tenant default). ``0`` stores an
        explicit unlimited."""
        ...

    async def clear_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
    ) -> None:
        """Remove one limit row so it falls through to the next layer."""
        ...

    async def list_subjects(self, *, tenant_id: str) -> list[uuid.UUID]:
        """Distinct real subjects that carry usage or an override.

        The admin overview source: every subject the tenant has metered
        or limited, EXCLUDING the ``DEFAULT_USER_ID`` sentinel (which is
        the tenant-wide default, surfaced separately). Subjects with no
        counter and no override are implicitly on the default and need
        no row. Order is unspecified.
        """
        ...
