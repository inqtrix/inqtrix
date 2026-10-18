"""Content-free user invalidations for authoritative client refetches.

The stream carries only cache invalidation coordinates. It never transports
resource data, patches, or authorization decisions; every consumer must read
the corresponding HTTP list/detail endpoint again.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from inqtrix.auth.shares import ShareAdminRepository

USER_EVENT_RETENTION_SECONDS = 24 * 60 * 60
"""Default replay retention required by the v0.2 stream contract."""

USER_EVENT_REPLAY_LIMIT = 500
"""Maximum frames replayed before a reset/refetch is safer than a backlog."""


@dataclass(frozen=True)
class UserInvalidation:
    """One persisted invalidation addressed to exactly one user.

    Attributes:
        id: Monotonic server cursor. The SSE frame uses it as ``id``.
        tenant_id: Tenant defense scope.
        target_user_id: Canonical user UUID allowed to receive the frame.
        scope: Authoritative client slice to refresh, such as ``runs`` or
            ``shares``. It is a hint, never a permission result.
        resource_type: Optional resource kind for targeted cache eviction.
        resource_id: Optional stable server resource identifier.
        created_at: Unix timestamp used only for bounded retention.
    """

    id: int
    tenant_id: str
    target_user_id: uuid.UUID
    scope: str
    resource_type: str | None = None
    resource_id: str | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class UserEventPage:
    """One ordered replay page and its retention verdict."""

    events: tuple[UserInvalidation, ...]
    current_cursor: int
    reset_required: bool = False


class UserEventStore(Protocol):
    """Persistence contract shared by the memory and PostgreSQL streams."""

    async def append(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        scope: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> UserInvalidation:
        """Persist one content-free invalidation."""
        ...

    async def page_after(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        cursor: int,
        limit: int = USER_EVENT_REPLAY_LIMIT,
    ) -> UserEventPage:
        """Return events newer than *cursor* or request a reset."""
        ...

    async def current_cursor(self, *, tenant_id: str) -> int:
        """Return the latest global event id visible in the tenant."""
        ...

    async def wait_for_change(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        cursor: int,
        timeout: float,
    ) -> None:
        """Wait for likely new work; callers still re-read authoritatively."""
        ...


class ResourceInvalidator:
    """One fallback invalidation path for non-transactional memory stores.

    PostgreSQL repositories append the same effects inside their mutation
    transaction. Volatile repositories use this coordinator after their
    in-process mutation; keeping target expansion here avoids one bespoke
    broadcaster per resource service.
    """

    def __init__(
        self,
        *,
        shares: "ShareAdminRepository",
        events: UserEventStore,
    ) -> None:
        self._shares = shares
        self._events = events

    async def invalidate(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        scope: str,
        additional_targets: Sequence[uuid.UUID] = (),
    ) -> None:
        """Notify the owner, every active recipient, and explicit leavers."""
        active = await self._shares.list_shares_for_resource(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        targets = {record.recipient_user_id for record in active}
        targets.update(additional_targets)
        if owner_user_id is not None:
            targets.add(owner_user_id)
        for target_user_id in sorted(targets, key=str):
            await self._events.append(
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=scope,
                resource_type=resource_type,
                resource_id=resource_id,
            )

    async def revoke_deleted(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        scope: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Revoke volatile-store shares and retain recipients as targets."""
        active = await self._shares.list_shares_for_resource(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        recipients = tuple(record.recipient_user_id for record in active)
        if actor_user_id is not None:
            await self._shares.revoke_shares_for_resource(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                revoked_by_user_id=actor_user_id,
            )
        await self.invalidate(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
            additional_targets=recipients,
        )


class MemoryUserEventStore:
    """Bounded process-local invalidation queue with a condition wake-up.

    The store is deliberately finite. Falling behind the retained window or
    reconnecting with a cursor from an earlier process generation yields
    ``reset_required`` so the browser performs a broad refetch instead of
    pretending an incomplete replay is authoritative.
    """

    def __init__(
        self,
        *,
        retention_seconds: float = USER_EVENT_RETENTION_SECONDS,
        max_events: int = 10_000,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._retention_seconds = float(retention_seconds)
        self._events: deque[UserInvalidation] = deque(maxlen=max_events)
        self._condition = threading.Condition(threading.RLock())
        self._next_id = 1
        self._evicted_through = 0

    async def append(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        scope: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> UserInvalidation:
        return self.append_nowait(
            tenant_id=tenant_id,
            target_user_id=target_user_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    def append_nowait(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        scope: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> UserInvalidation:
        """Append synchronously for mutations already holding a memory lock."""
        normalized_scope = scope.strip()
        if not normalized_scope:
            raise ValueError("scope must be non-empty")
        with self._condition:
            self._cleanup_locked(time.time())
            if self._events.maxlen and len(self._events) == self._events.maxlen:
                self._evicted_through = max(
                    self._evicted_through, self._events[0].id
                )
            event = UserInvalidation(
                id=self._next_id,
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=normalized_scope,
                resource_type=resource_type,
                resource_id=resource_id,
                created_at=time.time(),
            )
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return event

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
        with self._condition:
            self._cleanup_locked(time.time())
            current = self._next_id - 1
            if cursor > current and cursor != 0:
                return UserEventPage((), current, reset_required=True)
            if cursor and cursor <= self._evicted_through:
                return UserEventPage((), current, reset_required=True)
            matches = [
                event
                for event in self._events
                if event.id > cursor
                and event.tenant_id == tenant_id
                and event.target_user_id == target_user_id
            ]
            if len(matches) > bounded_limit:
                return UserEventPage((), current, reset_required=True)
            return UserEventPage(tuple(matches), current)

    async def current_cursor(self, *, tenant_id: str) -> int:
        del tenant_id
        with self._condition:
            self._cleanup_locked(time.time())
            return self._next_id - 1

    async def wait_for_change(
        self,
        *,
        tenant_id: str,
        target_user_id: uuid.UUID,
        cursor: int,
        timeout: float,
    ) -> None:
        def _wait() -> None:
            with self._condition:
                self._condition.wait_for(
                    lambda: any(
                        event.id > cursor
                        and event.tenant_id == tenant_id
                        and event.target_user_id == target_user_id
                        for event in self._events
                    ),
                    timeout=max(0.0, timeout),
                )

        await asyncio.to_thread(_wait)

    def _cleanup_locked(self, now: float) -> None:
        cutoff = now - self._retention_seconds
        while self._events and self._events[0].created_at < cutoff:
            removed = self._events.popleft()
            self._evicted_through = max(self._evicted_through, removed.id)
