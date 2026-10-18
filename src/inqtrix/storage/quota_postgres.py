"""Postgres implementation of the quota store.

Same conventions as the other durable repositories: every operation
runs in one tenant-scoped transaction under the restricted app role,
RLS is the second defense behind the explicit ``tenant_id`` predicates.

Lazy rollover falls out of the schema for free: the window
(``period_start``) is part of the counter primary key, so a new month
is simply a new row at 0 — old months linger as harmless history and
``read_usage`` only ever looks at the active window.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Sequence

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.quota.models import (
    DEFAULT_USER_ID,
    STOCK_PERIOD,
    QuotaAdjustmentConflict,
    QuotaDimension,
    QuotaSubject,
    StockLifecycleState,
    current_period_start,
)
from inqtrix.storage.db import build_engine, build_session_factory, tenant_session
from inqtrix.storage.quota_orm import (
    quota_limits,
    quota_stock_lifecycles,
    quota_usage_adjustments,
    quota_usage_counters,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _active_period(dimension: QuotaDimension, now: float) -> float:
    return STOCK_PERIOD if dimension.is_stock else current_period_start(now)


def _read_usage_result(
    rows: Sequence[tuple[uuid.UUID, str, float, int]],
    subject_user_ids: Sequence[uuid.UUID],
    dimensions: Sequence[QuotaDimension],
    now: float,
) -> dict[uuid.UUID, dict[QuotaDimension, int]]:
    """Fold counter rows into the usage of each subject's active windows."""
    # Pre-fill 0 for every requested dimension so the shape matches
    # the memory store (a missing counter reads as 0, not absent).
    result: dict[uuid.UUID, dict[QuotaDimension, int]] = {
        user_id: {d: 0 for d in dimensions} for user_id in subject_user_ids
    }
    active = {d: _active_period(d, now) for d in dimensions}
    for user_id, dim_value, period_start, used in rows:
        dimension = QuotaDimension(dim_value)
        # Only the active window counts; a stale-period row reads as
        # absent (lazy rollover) without being rewritten here.
        if period_start == active[dimension]:
            result[user_id][dimension] = int(used)
    return result


async def _read_usage_in(
    session: "AsyncSession",
    *,
    tenant_id: str,
    subject_user_ids: Sequence[uuid.UUID],
    dimensions: Sequence[QuotaDimension],
    now: float,
) -> dict[uuid.UUID, dict[QuotaDimension, int]]:
    """Current usage per subject, read inside an open tenant session."""
    rows = (
        await session.execute(
            select(
                quota_usage_counters.c.subject_user_id,
                quota_usage_counters.c.dimension,
                quota_usage_counters.c.period_start,
                quota_usage_counters.c.used,
            ).where(
                quota_usage_counters.c.tenant_id == tenant_id,
                quota_usage_counters.c.subject_user_id.in_(list(subject_user_ids)),
                quota_usage_counters.c.dimension.in_([d.value for d in dimensions]),
            )
        )
    ).all()
    return _read_usage_result(rows, subject_user_ids, dimensions, now)


async def _get_limits_in(
    session: "AsyncSession",
    *,
    tenant_id: str,
    subject_user_ids: Sequence[uuid.UUID | None],
    dimensions: Sequence[QuotaDimension],
) -> dict[uuid.UUID | None, dict[QuotaDimension, int]]:
    """Stored limits per subject, read inside an open tenant session."""
    subs = list(subject_user_ids)
    real_user_ids = [user_id for user_id in subs if user_id is not None]
    includes_default = None in subs
    subject_filter = or_(
        quota_limits.c.subject_user_id.in_(real_user_ids),
        quota_limits.c.subject_user_id.is_(None) if includes_default else False,
    )
    rows = (
        await session.execute(
            select(
                quota_limits.c.subject_user_id,
                quota_limits.c.dimension,
                quota_limits.c.limit_value,
            ).where(
                quota_limits.c.tenant_id == tenant_id,
                subject_filter,
                quota_limits.c.dimension.in_([d.value for d in dimensions]),
            )
        )
    ).all()
    result: dict[uuid.UUID | None, dict[QuotaDimension, int]] = {}
    for sub, dim_value, value in rows:
        result.setdefault(sub, {})[QuotaDimension(dim_value)] = int(value)
    return result


class PostgresQuotaStore:
    """Quota usage + limits over the ``quota_*`` tables.

    Two instances serve one deployment, the same split as every other
    durable repository (request path pooled, run threads NullPool):

    * Built from *database_url*, the store owns a NullPool engine and is
      loop-agnostic. Run threads record their spend through
      ``asyncio.run`` on a throwaway loop, and asyncpg connections are
      loop-affine, so this lane opens a fresh connection per call.
    * Built from *session_factory*, the store borrows the API's pooled
      request-path engine. Admission, the composer meter (every 30
      seconds per visible tab) and the admin overview run on the request
      loop, where a connect per call would turn ordinary idle browsing
      into a stream of new database connections.

    Args:
        app_role: Restricted Postgres role for the tenant sessions.
        database_url: Async SQLAlchemy URL (``postgresql+asyncpg://...``)
            for an owned NullPool engine.
        session_factory: A borrowed session factory (the request path's
            pooled engine); the store never disposes it.

    Raises:
        ValueError: Unless exactly one of *database_url* and
            *session_factory* is given.
    """

    def __init__(
        self,
        *,
        app_role: str,
        database_url: str | None = None,
        session_factory: "async_sessionmaker[AsyncSession] | None" = None,
    ) -> None:
        if (database_url is None) == (session_factory is None):
            raise ValueError(
                "PostgresQuotaStore needs exactly one of database_url or session_factory"
            )
        self._engine = (
            build_engine(database_url, null_pool=True) if database_url is not None else None
        )
        self._session_factory = (
            session_factory
            if session_factory is not None
            else build_session_factory(self._engine)
        )
        self._app_role = app_role

    async def aclose(self) -> None:
        """Dispose the owned engine at shutdown (lifecycle parity).

        Must run on a live event loop, not a throwaway ``asyncio.run``
        one — the API lifespan teardown calls this. NullPool holds no
        idle connections, so this only releases the engine/dialect
        machinery, but keeps the store's lifecycle consistent with the
        run store's ``close``. A borrowed session factory belongs to its
        owner and is left alone.
        """
        if self._engine is not None:
            await self._engine.dispose()

    def _session(self, tenant_id: str) -> "AbstractAsyncContextManager[AsyncSession]":
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def add_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        period_start: float,
        amount: int,
    ) -> int:
        now = time.time()
        seed = max(0, amount)
        async with self._session(tenant_id) as session:
            stmt = pg_insert(quota_usage_counters).values(
                tenant_id=tenant_id,
                subject_user_id=subject_user_id,
                dimension=dimension.value,
                period_start=period_start,
                used=seed,
                updated_at=now,
            )
            # Clamp at 0 so a stock release never drives the level
            # negative; a new window simply lands as a fresh row.
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    quota_usage_counters.c.tenant_id,
                    quota_usage_counters.c.subject_user_id,
                    quota_usage_counters.c.dimension,
                    quota_usage_counters.c.period_start,
                ],
                set_={
                    "used": func.greatest(0, quota_usage_counters.c.used + amount),
                    "updated_at": now,
                },
            ).returning(quota_usage_counters.c.used)
            new_used = (await session.execute(stmt)).scalar_one()
        return int(new_used)

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
        now = time.time()
        async with self._session(tenant_id) as session:
            inserted = (
                await session.execute(
                    pg_insert(quota_usage_adjustments)
                    .values(
                        adjustment_id=adjustment_id,
                        tenant_id=tenant_id,
                        subject_user_id=subject_user_id,
                        dimension=dimension.value,
                        period_start=period_start,
                        amount=amount,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[quota_usage_adjustments.c.adjustment_id]
                    )
                    .returning(quota_usage_adjustments.c.adjustment_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                receipt = (
                    await session.execute(
                        select(
                            quota_usage_adjustments.c.tenant_id,
                            quota_usage_adjustments.c.subject_user_id,
                            quota_usage_adjustments.c.dimension,
                            quota_usage_adjustments.c.period_start,
                            quota_usage_adjustments.c.amount,
                        ).where(
                            quota_usage_adjustments.c.adjustment_id == adjustment_id
                        )
                    )
                ).one_or_none()
                if receipt is None or (
                    receipt.tenant_id != tenant_id
                    or receipt.subject_user_id != subject_user_id
                    or receipt.dimension != dimension.value
                    or int(receipt.amount) != amount
                ):
                    raise QuotaAdjustmentConflict(adjustment_id)
                # The original receipt remains authoritative after calendar
                # rollover; a later period is not a contradictory replay and
                # must not receive a second charge.
                current = (
                    await session.execute(
                        select(quota_usage_counters.c.used).where(
                            quota_usage_counters.c.tenant_id == tenant_id,
                            quota_usage_counters.c.subject_user_id == subject_user_id,
                            quota_usage_counters.c.dimension == dimension.value,
                            quota_usage_counters.c.period_start == period_start,
                        )
                    )
                ).scalar_one_or_none()
                return int(current or 0)
            seed = max(0, amount)
            stmt = pg_insert(quota_usage_counters).values(
                tenant_id=tenant_id,
                subject_user_id=subject_user_id,
                dimension=dimension.value,
                period_start=period_start,
                used=seed,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    quota_usage_counters.c.tenant_id,
                    quota_usage_counters.c.subject_user_id,
                    quota_usage_counters.c.dimension,
                    quota_usage_counters.c.period_start,
                ],
                set_={
                    "used": func.greatest(0, quota_usage_counters.c.used + amount),
                    "updated_at": now,
                },
            ).returning(quota_usage_counters.c.used)
            return int((await session.execute(stmt)).scalar_one())

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
        """Converge one resource stock and aggregate counter transactionally."""

        if not dimension.is_stock:
            raise ValueError("stock lifecycle requires a stock dimension")
        if desired_amount < 0:
            raise ValueError("stock amount cannot be negative")
        if not stock_key.strip():
            raise ValueError("stock key cannot be empty")
        now = time.time()
        async with self._session(tenant_id) as session:
            await session.execute(
                pg_insert(quota_stock_lifecycles)
                .values(
                    tenant_id=tenant_id,
                    stock_key=stock_key,
                    subject_user_id=subject_user_id,
                    dimension=dimension.value,
                    amount=0,
                    tombstoned=False,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        quota_stock_lifecycles.c.tenant_id,
                        quota_stock_lifecycles.c.stock_key,
                    ]
                )
            )
            row = (
                (
                    await session.execute(
                        select(quota_stock_lifecycles)
                        .where(
                            quota_stock_lifecycles.c.tenant_id == tenant_id,
                            quota_stock_lifecycles.c.stock_key == stock_key,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                row["subject_user_id"] != subject_user_id
                or row["dimension"] != dimension.value
            ):
                raise ValueError("stock key already belongs to another quota subject")
            current_amount = int(row["amount"])
            is_tombstoned = tombstone or bool(row["tombstoned"])
            next_amount = 0 if is_tombstoned else desired_amount
            await session.execute(
                update(quota_stock_lifecycles)
                .where(
                    quota_stock_lifecycles.c.tenant_id == tenant_id,
                    quota_stock_lifecycles.c.stock_key == stock_key,
                )
                .values(
                    amount=next_amount,
                    tombstoned=is_tombstoned,
                    updated_at=now,
                )
            )
            delta = next_amount - current_amount
            if delta:
                stmt = pg_insert(quota_usage_counters).values(
                    tenant_id=tenant_id,
                    subject_user_id=subject_user_id,
                    dimension=dimension.value,
                    period_start=STOCK_PERIOD,
                    used=max(0, delta),
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        quota_usage_counters.c.tenant_id,
                        quota_usage_counters.c.subject_user_id,
                        quota_usage_counters.c.dimension,
                        quota_usage_counters.c.period_start,
                    ],
                    set_={
                        "used": func.greatest(0, quota_usage_counters.c.used + delta),
                        "updated_at": now,
                    },
                )
                await session.execute(stmt)
        return StockLifecycleState(
            stock_key=stock_key,
            subject=QuotaSubject(tenant_id=tenant_id, user_id=subject_user_id),
            dimension=dimension,
            amount=next_amount,
            tombstoned=is_tombstoned,
        )

    async def read_stock(
        self,
        *,
        stock_key: str,
        tenant_id: str,
    ) -> StockLifecycleState | None:
        """Read one canonical resource stock under tenant RLS."""

        async with self._session(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        select(quota_stock_lifecycles).where(
                            quota_stock_lifecycles.c.tenant_id == tenant_id,
                            quota_stock_lifecycles.c.stock_key == stock_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        dimension = QuotaDimension(str(row["dimension"]))
        return StockLifecycleState(
            stock_key=str(row["stock_key"]),
            subject=QuotaSubject(
                tenant_id=str(row["tenant_id"]),
                user_id=row["subject_user_id"],
            ),
            dimension=dimension,
            amount=int(row["amount"]),
            tombstoned=bool(row["tombstoned"]),
        )

    async def read_usage(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID],
        dimensions: Sequence[QuotaDimension],
        now: float,
    ) -> dict[uuid.UUID, dict[QuotaDimension, int]]:
        if not subject_user_ids or not dimensions:
            return _read_usage_result([], subject_user_ids, dimensions, now)
        async with self._session(tenant_id) as session:
            return await _read_usage_in(
                session,
                tenant_id=tenant_id,
                subject_user_ids=subject_user_ids,
                dimensions=dimensions,
                now=now,
            )

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
        async with self._session(tenant_id) as session:
            usage = (
                await _read_usage_in(
                    session,
                    tenant_id=tenant_id,
                    subject_user_ids=subject_user_ids,
                    dimensions=dimensions,
                    now=now,
                )
                if subject_user_ids and dimensions
                else _read_usage_result([], subject_user_ids, dimensions, now)
            )
            limits = (
                await _get_limits_in(
                    session,
                    tenant_id=tenant_id,
                    subject_user_ids=limit_subject_user_ids,
                    dimensions=dimensions,
                )
                if limit_subject_user_ids and dimensions
                else {}
            )
        return usage, limits

    async def reset_usage(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID,
        dimension: QuotaDimension,
        now: float,
    ) -> None:
        if dimension.is_stock:
            # Stock is freed by deletion, never reset; zeroing it would
            # decouple the counter from real occupancy. Loud bug signal.
            raise ValueError(f"cannot reset stock dimension {dimension.value}")
        active = _active_period(dimension, now)
        stamped = time.time()
        async with self._session(tenant_id) as session:
            stmt = pg_insert(quota_usage_counters).values(
                tenant_id=tenant_id,
                subject_user_id=subject_user_id,
                dimension=dimension.value,
                period_start=active,
                used=0,
                updated_at=stamped,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    quota_usage_counters.c.tenant_id,
                    quota_usage_counters.c.subject_user_id,
                    quota_usage_counters.c.dimension,
                    quota_usage_counters.c.period_start,
                ],
                set_={"used": 0, "updated_at": stamped},
            )
            await session.execute(stmt)

    async def get_limits(
        self,
        *,
        tenant_id: str,
        subject_user_ids: Sequence[uuid.UUID | None],
        dimensions: Sequence[QuotaDimension],
    ) -> dict[uuid.UUID | None, dict[QuotaDimension, int]]:
        if not subject_user_ids or not dimensions:
            return {}
        async with self._session(tenant_id) as session:
            return await _get_limits_in(
                session,
                tenant_id=tenant_id,
                subject_user_ids=subject_user_ids,
                dimensions=dimensions,
            )

    async def set_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
        value: int,
        set_by_user_id: uuid.UUID,
    ) -> None:
        now = time.time()
        async with self._session(tenant_id) as session:
            stmt = pg_insert(quota_limits).values(
                tenant_id=tenant_id,
                subject_user_id=subject_user_id,
                dimension=dimension.value,
                limit_value=value,
                set_by_user_id=set_by_user_id,
                set_at=now,
            )
            index_elements = [quota_limits.c.tenant_id]
            if subject_user_id is not None:
                index_elements.append(quota_limits.c.subject_user_id)
            index_elements.append(quota_limits.c.dimension)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                index_where=text(
                    "subject_user_id IS NOT NULL"
                    if subject_user_id is not None
                    else "subject_user_id IS NULL"
                ),
                set_={
                    "limit_value": value,
                    "set_by_user_id": set_by_user_id,
                    "set_at": now,
                },
            )
            await session.execute(stmt)

    async def clear_limit(
        self,
        *,
        tenant_id: str,
        subject_user_id: uuid.UUID | None,
        dimension: QuotaDimension,
    ) -> None:
        async with self._session(tenant_id) as session:
            await session.execute(
                delete(quota_limits).where(
                    quota_limits.c.tenant_id == tenant_id,
                    quota_limits.c.subject_user_id == subject_user_id,
                    quota_limits.c.dimension == dimension.value,
                )
            )

    async def list_subjects(self, *, tenant_id: str) -> list[uuid.UUID]:
        async with self._session(tenant_id) as session:
            counter_subs = (
                (
                    await session.execute(
                        select(quota_usage_counters.c.subject_user_id)
                        .where(quota_usage_counters.c.tenant_id == tenant_id)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            limit_subs = (
                (
                    await session.execute(
                        select(quota_limits.c.subject_user_id)
                        .where(quota_limits.c.tenant_id == tenant_id)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        subs = set(counter_subs) | set(limit_subs)
        subs.discard(DEFAULT_USER_ID)
        return sorted(subs)
