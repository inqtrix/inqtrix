"""PostgreSQL persistence for the user invalidation stream."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import BigInteger, delete, func, insert, select, text, true
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from inqtrix.storage.authorization_generation import (
    bump_authorization_generation,
)
from inqtrix.storage.db import tenant_session
from inqtrix.storage.identity_orm import active_admin, users
from inqtrix.storage.user_event_orm import user_events
from inqtrix.user_events import (
    USER_EVENT_REPLAY_LIMIT,
    USER_EVENT_RETENTION_SECONDS,
    UserEventPage,
    UserInvalidation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger("inqtrix")

# The newest event of each waiting user: one backward probe of
# ix_user_events_tenant_target_id per user, whatever the streams' cursors.
# A wake carries no data; the woken stream reads its own page.
_NEWEST_EVENT_PER_USER = text(
    """
    SELECT waiting.user_id, newest.id
    FROM unnest(CAST(:user_ids AS uuid[])) AS waiting(user_id)
    CROSS JOIN LATERAL (
        SELECT event.id
        FROM user_events AS event
        WHERE event.tenant_id = :tenant_id
          AND event.target_user_id = waiting.user_id
        ORDER BY event.id DESC
        LIMIT 1
    ) AS newest
    """
).columns(user_id=PG_UUID(as_uuid=True), id=BigInteger)


class _ChangeWaiter:
    """One waiting stream: its user, its cursor, and the event that wakes it."""

    __slots__ = ("cursor", "target_user_id", "woken")

    def __init__(self, target_user_id: uuid.UUID, cursor: int) -> None:
        self.cursor = cursor
        self.target_user_id = target_user_id
        self.woken = asyncio.Event()


class _TenantChangePoll:
    """The one poll of ``user_events`` that serves every waiting stream of
    a tenant in one event loop.

    Each interval it reads the newest event of every waiting user once and
    wakes the streams whose user has an event past their cursor. Event ids
    are commit-ordered per user (every insert first locks the user's
    authorization-generation row), so "an event past my cursor exists" is
    exactly the check a stream ran on its own before. A failed read wakes
    nobody; the streams keep their own timeout and the poll retries on the
    next interval.
    """

    def __init__(
        self, store: "PostgresUserEventStore", tenant_id: str
    ) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._task: asyncio.Task[None] | None = None
        self.waiters: set[_ChangeWaiter] = set()

    def add(self, waiter: _ChangeWaiter) -> None:
        self.waiters.add(waiter)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    def discard(self, waiter: _ChangeWaiter) -> bool:
        """Remove *waiter*; with the last one gone, stop the poll at once.
        Returns whether the poll has no waiters left."""
        self.waiters.discard(waiter)
        if self.waiters:
            return False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        return True

    async def _run(self) -> None:
        failures = 0
        while self.waiters:
            waiters = tuple(self.waiters)
            try:
                newest = await self._store._newest_event_ids(
                    self._tenant_id,
                    sorted({waiter.target_user_id for waiter in waiters}, key=str),
                )
            except Exception as exc:  # noqa: BLE001 - one bad read must not end the streams
                failures += 1
                if failures == 1:
                    log.warning(
                        "User event change poll failed (%s); waiting streams "
                        "keep their own timeout and the poll is retried every "
                        "interval.",
                        type(exc).__name__,
                    )
            else:
                if failures:
                    log.info(
                        "User event change poll recovered after %d failed attempts.",
                        failures,
                    )
                    failures = 0
                for waiter in waiters:
                    if newest.get(waiter.target_user_id, 0) > waiter.cursor:
                        waiter.woken.set()
            await asyncio.sleep(self._store._poll_seconds)


def _event_from_row(row: Any) -> UserInvalidation:
    return UserInvalidation(
        id=int(row.id),
        tenant_id=str(row.tenant_id),
        target_user_id=row.target_user_id,
        scope=str(row.scope),
        resource_type=(
            str(row.resource_type) if row.resource_type is not None else None
        ),
        resource_id=(
            str(row.resource_id) if row.resource_id is not None else None
        ),
        created_at=(
            row.created_at.timestamp() if row.created_at is not None else 0.0
        ),
    )


async def append_user_invalidation(
    session: "AsyncSession",
    *,
    tenant_id: str,
    target_user_id: uuid.UUID,
    scope: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> UserInvalidation:
    """Append an invalidation inside the caller's existing transaction.

    Mutation repositories use this helper after their resource/audit writes.
    A rollback therefore removes the invalidation too; there is no outbox race
    and no second event authority.
    """
    normalized_scope = scope.strip()
    if not normalized_scope:
        raise ValueError("scope must be non-empty")
    # Same transaction as the mutation AND the event: the user-row lock
    # makes the generation commit-ordered per user, and a rollback takes
    # event and generation back together. Callers with multiple targets
    # iterate in sorted order (deadlock protection).
    #
    # The event's foreign key share-locks the target's account row. Taken
    # only after the generation, it would let this writer and a transaction
    # that holds the account row (an erasure start locks its acting
    # administrator) and then bumps the same generation wait for each
    # other. Every writer therefore locks account row, generation, event --
    # in that order.
    await session.execute(
        select(users.c.id)
        .where(users.c.tenant_id == tenant_id, users.c.id == target_user_id)
        .with_for_update(read=True, key_share=True)
    )
    await bump_authorization_generation(
        session, tenant_id=tenant_id, target_user_ids=(target_user_id,)
    )
    row = (
        await session.execute(
            insert(user_events)
            .values(
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=normalized_scope,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            .returning(user_events)
        )
    ).one()
    return _event_from_row(row)


async def append_instance_admin_invalidations(
    session: "AsyncSession",
    *,
    tenant_id: str,
    target_user_ids: Sequence[uuid.UUID] = (),
    scope: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> tuple[UserInvalidation, ...]:
    """Invalidate affected users and every active instance administrator.

    Administrative list/detail views are instance-wide, so workspace and user
    mutations must wake administrators who are neither the actor nor a
    workspace member. The event remains content-free: it carries only the
    existing refetch coordinates, never mutation data or authorization state.
    """
    admin_user_ids = (
        await session.execute(
            select(users.c.id)
            .where(users.c.tenant_id == tenant_id, active_admin())
            .order_by(users.c.id)
        )
    ).scalars()
    targets = set(target_user_ids)
    targets.update(admin_user_ids)
    appended = []
    for target_user_id in sorted(targets, key=str):
        appended.append(
            await append_user_invalidation(
                session,
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=scope,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        )
    return tuple(appended)


class PostgresUserEventStore:
    """Tenant-scoped event replay with bounded polling and lazy retention.

    Retention is enforced lazily by the traffic that reads and writes the
    stream, exactly as before -- but coalesced: at most one retention
    DELETE per tenant per STORE INSTANCE per ``cleanup_interval_seconds``
    (one instance per api process in practice; the NullPool twin built
    for run threads never runs cleanup on Postgres, where repositories
    write invalidations atomically inside their own transactions).
    Uncoalesced, every ``page_after`` poll of every open browser tab paid
    a tenant-wide DELETE (a sequential scan -- the table has no
    ``created_at`` index) that, at a 24h retention, almost always deleted
    nothing. A deliberately NOT built alternative was a process-owned
    sweeper task: it would need lifespan wiring, tenant enumeration, and
    a second code path per topology, while this store's caller-scoped
    session already carries the right tenant and loop.
    """

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
        retention_seconds: float = USER_EVENT_RETENTION_SECONDS,
        poll_seconds: float = 0.5,
        cleanup_interval_seconds: float = 300.0,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if cleanup_interval_seconds <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        self._session_factory = session_factory
        self._app_role = app_role
        self._retention_seconds = float(retention_seconds)
        self._poll_seconds = float(poll_seconds)
        self._cleanup_interval_seconds = float(cleanup_interval_seconds)
        # Monotonic deadline per tenant. Per tenant, not per process: one
        # busy tenant must not starve another tenant's retention.
        self._cleanup_due: dict[str, float] = {}
        # One change poll per event loop and tenant; asyncio primitives
        # belong to the loop that created them.
        self._change_polls: dict[
            tuple[asyncio.AbstractEventLoop, str], _TenantChangePoll
        ] = {}

    def _session(
        self, tenant_id: str
    ) -> "AbstractAsyncContextManager[AsyncSession]":
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def append(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        scope: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> UserInvalidation:
        async with self._session(tenant_id) as session:
            await self._maybe_cleanup(session, tenant_id)
            return await append_user_invalidation(
                session,
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=scope,
                resource_type=resource_type,
                resource_id=resource_id,
            )

    async def page_after(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        cursor: int,
        limit: int = USER_EVENT_REPLAY_LIMIT,
    ) -> UserEventPage:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        bounded_limit = max(1, min(int(limit), USER_EVENT_REPLAY_LIMIT))
        async with self._session(tenant_id) as session:
            await self._maybe_cleanup(session, tenant_id)
            # Bounds and page rows travel in ONE statement, i.e. one READ
            # COMMITTED snapshot. As two statements, a CONCURRENT process's
            # retention DELETE could commit in between: bounds computed
            # before the sweep, rows after it -- a partial replay delivered
            # as complete, with the reset check blind to the gap. (Before
            # the coalesced retention this could not happen, because the
            # local DELETE always ran first in the same transaction.)
            bounds = (
                select(
                    func.min(user_events.c.id).label("oldest"),
                    func.max(user_events.c.id).label("current"),
                )
                .where(user_events.c.tenant_id == tenant_id)
                .cte("bounds")
            )
            page = (
                select(user_events)
                .where(
                    user_events.c.tenant_id == tenant_id,
                    user_events.c.target_user_id == target_user_id,
                    user_events.c.id > cursor,
                )
                .order_by(user_events.c.id)
                .limit(bounded_limit + 1)
                .subquery("page")
            )
            rows = (
                await session.execute(
                    select(bounds.c.oldest, bounds.c.current, page)
                    .select_from(bounds.outerjoin(page, true()))
                    .order_by(page.c.id)
                )
            ).all()
            head = rows[0]
            oldest = int(head.oldest) if head.oldest is not None else 0
            current = int(head.current) if head.current is not None else 0
            events = [row for row in rows if row.id is not None]
            if cursor > current and cursor != 0:
                return UserEventPage((), current, reset_required=True)
            if cursor and oldest and cursor < oldest - 1:
                return UserEventPage((), current, reset_required=True)
            if len(events) > bounded_limit:
                return UserEventPage((), current, reset_required=True)
            return UserEventPage(
                tuple(_event_from_row(row) for row in events), current
            )

    async def current_cursor(self, *, tenant_id: str) -> int:
        async with self._session(tenant_id) as session:
            await self._maybe_cleanup(session, tenant_id)
            value = await session.scalar(
                select(func.max(user_events.c.id)).where(
                    user_events.c.tenant_id == tenant_id
                )
            )
            return int(value or 0)

    async def wait_for_change(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        cursor: int,
        timeout: float,
    ) -> None:
        """Return once the user has an event past *cursor*, or after
        *timeout*. All waiting streams of the tenant in this event loop share
        one poll per ``poll_seconds``."""
        if timeout <= 0:
            return
        key = (asyncio.get_running_loop(), tenant_id)
        poll = self._change_polls.get(key)
        if poll is None:
            poll = self._change_polls[key] = _TenantChangePoll(self, tenant_id)
        waiter = _ChangeWaiter(target_user_id, cursor)
        poll.add(waiter)
        try:
            await asyncio.wait_for(waiter.woken.wait(), timeout)
        except TimeoutError:
            return
        finally:
            if poll.discard(waiter) and self._change_polls.get(key) is poll:
                del self._change_polls[key]

    async def _newest_event_ids(
        self, tenant_id: str, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """The id of each listed user's newest event in the tenant; users
        without an event are absent."""
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    _NEWEST_EVENT_PER_USER,
                    {"tenant_id": tenant_id, "user_ids": list(user_ids)},
                )
            ).all()
        return {row.user_id: int(row.id) for row in rows}

    async def _maybe_cleanup(
        self, session: "AsyncSession", tenant_id: str
    ) -> None:
        """Run the retention DELETE at most once per tenant per interval.

        The deadline moves BEFORE the DELETE runs: if the database is
        struggling, the failure propagates to the caller exactly as it
        always did, but the next polls do not hammer the same DELETE at a
        struggling database -- retention waits one interval instead. The
        unsynchronized check is deliberate: a concurrent race costs at
        worst one extra idempotent DELETE.

        The tenant filter is explicit even though the RLS policy
        (tenant_isolation, migration 0047) already scopes the session:
        retention must not depend on which enforcement mode the
        connection role happens to run under.
        """
        now = time.monotonic()
        if now < self._cleanup_due.get(tenant_id, 0.0):
            return
        self._cleanup_due[tenant_id] = now + self._cleanup_interval_seconds
        cutoff = datetime.now(UTC) - timedelta(seconds=self._retention_seconds)
        await session.execute(
            delete(user_events).where(
                user_events.c.tenant_id == tenant_id,
                user_events.c.created_at < cutoff,
            )
        )
