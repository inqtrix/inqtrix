"""Source-scoped write authority shared by ingestion and aggregate deletion.

The durable tombstone is deliberately independent of deletion receipts: a
receipt may expire, while a stable source id must never be resurrected by a
late request. Identity includes owner and workspace scope so one user cannot
block another user's coincidentally equal external id.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator

from sqlalchemy import insert, select, text, update

from inqtrix.storage.source_lifecycle_orm import source_lifecycles


class SourceLifecycleConflict(RuntimeError):
    """The source is absent, deleting, deleted, or its epoch is stale."""


class SourceLifecycleState(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


@dataclass(frozen=True)
class SourceScope:
    tenant_id: str
    source_id: str
    owner_user_id: uuid.UUID | None
    workspace_id: str | None

    @property
    def owner_key(self) -> str:
        return str(self.owner_user_id) if self.owner_user_id is not None else ""

    @property
    def workspace_key(self) -> str:
        return self.workspace_id or ""

    @property
    def is_asset(self) -> bool:
        return self.source_id.startswith("asset:")


@dataclass(frozen=True)
class SourceWritePermit:
    scope: SourceScope
    epoch: int


@dataclass(frozen=True)
class SourceDeletionPermit:
    scope: SourceScope
    epoch: int
    operation_id: str


@dataclass
class _MemoryLifecycle:
    state: SourceLifecycleState
    epoch: int
    operation_id: str | None = None


class MemorySourceLifecycleAuthority:
    """In-process authority whose guard is held through the store mutation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[SourceScope, _MemoryLifecycle] = {}

    def register_active(self, scope: SourceScope) -> SourceWritePermit:
        with self._lock:
            current = self._records.get(scope)
            if current is None:
                current = _MemoryLifecycle(SourceLifecycleState.ACTIVE, 1)
                self._records[scope] = current
            if current.state is not SourceLifecycleState.ACTIVE:
                raise SourceLifecycleConflict(scope.source_id)
            return SourceWritePermit(scope, current.epoch)

    def resolve_scope(self, *, tenant_id: str, source_id: str) -> SourceScope:
        """Resolve one server-registered identity without trusting a caller."""
        with self._lock:
            matches = [
                scope
                for scope in self._records
                if scope.tenant_id == tenant_id and scope.source_id == source_id
            ]
            if len(matches) != 1:
                raise SourceLifecycleConflict(source_id)
            return matches[0]

    @contextmanager
    def active_write(
        self,
        scope: SourceScope,
        *,
        expected_epoch: int | None = None,
        create_if_missing: bool,
    ) -> Iterator[SourceWritePermit]:
        with self._lock:
            current = self._records.get(scope)
            if current is None and create_if_missing:
                current = _MemoryLifecycle(SourceLifecycleState.ACTIVE, 1)
                self._records[scope] = current
            if (
                current is None
                or current.state is not SourceLifecycleState.ACTIVE
                or (
                    expected_epoch is not None
                    and current.epoch != expected_epoch
                )
            ):
                raise SourceLifecycleConflict(scope.source_id)
            yield SourceWritePermit(scope, current.epoch)

    def begin_delete(
        self, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        with self._lock:
            current = self._records.get(scope)
            if current is not None and current.state in {
                SourceLifecycleState.DELETING,
                SourceLifecycleState.DELETED,
            }:
                if current.operation_id != operation_id:
                    raise SourceLifecycleConflict(scope.source_id)
                return SourceDeletionPermit(
                    scope, current.epoch, operation_id
                )
            epoch = (current.epoch if current is not None else 0) + 1
            self._records[scope] = _MemoryLifecycle(
                SourceLifecycleState.DELETING, epoch, operation_id
            )
            return SourceDeletionPermit(scope, epoch, operation_id)

    def begin_delete_many(
        self,
        scopes: tuple[SourceScope, ...],
        *,
        operation_id: str,
    ) -> tuple[SourceDeletionPermit, ...]:
        """Atomically fence an aggregate of sources in the memory tier."""

        unique_scopes = tuple(dict.fromkeys(scopes))
        with self._lock:
            for scope in unique_scopes:
                current = self._records.get(scope)
                if (
                    current is not None
                    and current.state
                    in {SourceLifecycleState.DELETING, SourceLifecycleState.DELETED}
                    and current.operation_id != operation_id
                ):
                    raise SourceLifecycleConflict(scope.source_id)
            permits: list[SourceDeletionPermit] = []
            for scope in unique_scopes:
                current = self._records.get(scope)
                if (
                    current is not None
                    and current.state
                    in {SourceLifecycleState.DELETING, SourceLifecycleState.DELETED}
                ):
                    permits.append(
                        SourceDeletionPermit(scope, current.epoch, operation_id)
                    )
                    continue
                epoch = (current.epoch if current is not None else 0) + 1
                self._records[scope] = _MemoryLifecycle(
                    SourceLifecycleState.DELETING, epoch, operation_id
                )
                permits.append(SourceDeletionPermit(scope, epoch, operation_id))
            return tuple(permits)

    def finish_delete(self, permit: SourceDeletionPermit) -> None:
        with self._lock:
            current = self._records.get(permit.scope)
            if (
                current is None
                or current.epoch != permit.epoch
                or current.operation_id != permit.operation_id
                or current.state is not SourceLifecycleState.DELETING
            ):
                raise SourceLifecycleConflict(permit.scope.source_id)
            current.state = SourceLifecycleState.DELETED

    def complete_delete(self, permit: SourceDeletionPermit) -> None:
        """Complete deletion while retaining the durable identity tombstone."""
        self.finish_delete(permit)

    def complete_delete_many(
        self, permits: tuple[SourceDeletionPermit, ...]
    ) -> None:
        """Validate the whole aggregate before publishing any tombstone."""

        with self._lock:
            for permit in permits:
                current = self._records.get(permit.scope)
                if (
                    current is None
                    or current.epoch != permit.epoch
                    or current.operation_id != permit.operation_id
                    or current.state is not SourceLifecycleState.DELETING
                ):
                    raise SourceLifecycleConflict(permit.scope.source_id)
            for permit in permits:
                self._records[permit.scope].state = SourceLifecycleState.DELETED

    def validate_deletion(self, permit: SourceDeletionPermit) -> None:
        with self._lock:
            current = self._records.get(permit.scope)
            if (
                current is None
                or current.epoch != permit.epoch
                or current.operation_id != permit.operation_id
                or current.state
                not in {SourceLifecycleState.DELETING, SourceLifecycleState.DELETED}
            ):
                raise SourceLifecycleConflict(permit.scope.source_id)

    def get_deletion_permit(
        self, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        """Restore a worker permit from the durable in-memory authority row."""
        with self._lock:
            current = self._records.get(scope)
            if (
                current is None
                or current.operation_id != operation_id
                or current.state
                not in {SourceLifecycleState.DELETING, SourceLifecycleState.DELETED}
            ):
                raise SourceLifecycleConflict(scope.source_id)
            return SourceDeletionPermit(scope, current.epoch, operation_id)


class PostgresSourceLifecycleAuthority:
    """Row-locked authority used inside a caller-owned Postgres transaction."""

    @staticmethod
    async def _lock_scope(session, scope: SourceScope) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {
                "key": (
                    f"source:{scope.tenant_id}:{scope.source_id}:"
                    f"{scope.owner_key}:{scope.workspace_key}"
                )
            },
        )

    @staticmethod
    def _predicate(scope: SourceScope):
        return (
            source_lifecycles.c.tenant_id == scope.tenant_id,
            source_lifecycles.c.source_id == scope.source_id,
            source_lifecycles.c.owner_key == scope.owner_key,
            source_lifecycles.c.workspace_key == scope.workspace_key,
        )

    async def active_write(
        self,
        session,
        scope: SourceScope,
        *,
        expected_epoch: int | None = None,
        create_if_missing: bool,
    ) -> SourceWritePermit:
        await self._lock_scope(session, scope)
        row = (
            await session.execute(
                select(source_lifecycles)
                .where(*self._predicate(scope))
                .with_for_update()
            )
        ).mappings().first()
        if row is None and create_if_missing:
            await session.execute(
                insert(source_lifecycles).values(
                    tenant_id=scope.tenant_id,
                    source_id=scope.source_id,
                    owner_key=scope.owner_key,
                    workspace_key=scope.workspace_key,
                    owner_user_id=scope.owner_user_id,
                    workspace_id=scope.workspace_id,
                    state=SourceLifecycleState.ACTIVE.value,
                    epoch=1,
                    updated_at=time.time(),
                )
            )
            return SourceWritePermit(scope, 1)
        if (
            row is None
            or row["state"] != SourceLifecycleState.ACTIVE.value
            or (
                expected_epoch is not None
                and int(row["epoch"]) != expected_epoch
            )
        ):
            raise SourceLifecycleConflict(scope.source_id)
        return SourceWritePermit(scope, int(row["epoch"]))

    async def register_active(self, session, scope: SourceScope) -> SourceWritePermit:
        return await self.active_write(
            session, scope, create_if_missing=True
        )

    async def register_active_in_session(
        self, session, scope: SourceScope
    ) -> SourceWritePermit:
        """Register an asset in the caller's canonical write transaction."""
        return await self.register_active(session, scope)

    async def resolve_scope(
        self, session, *, tenant_id: str, source_id: str
    ) -> SourceScope:
        """Resolve one durable server-minted scope for a source identity."""
        rows = (
            await session.execute(
                select(
                    source_lifecycles.c.owner_user_id,
                    source_lifecycles.c.workspace_id,
                ).where(
                    source_lifecycles.c.tenant_id == tenant_id,
                    source_lifecycles.c.source_id == source_id,
                )
            )
        ).all()
        if len(rows) != 1:
            raise SourceLifecycleConflict(source_id)
        return SourceScope(
            tenant_id=tenant_id,
            source_id=source_id,
            owner_user_id=rows[0].owner_user_id,
            workspace_id=rows[0].workspace_id,
        )

    async def begin_delete(
        self, session, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        await self._lock_scope(session, scope)
        row = (
            await session.execute(
                select(source_lifecycles)
                .where(*self._predicate(scope))
                .with_for_update()
            )
        ).mappings().first()
        if row is not None and row["state"] in {
            SourceLifecycleState.DELETING.value,
            SourceLifecycleState.DELETED.value,
        }:
            if row["operation_id"] != operation_id:
                raise SourceLifecycleConflict(scope.source_id)
            return SourceDeletionPermit(
                scope, int(row["epoch"]), operation_id
            )
        epoch = (int(row["epoch"]) if row is not None else 0) + 1
        values = {
            "state": SourceLifecycleState.DELETING.value,
            "epoch": epoch,
            "operation_id": operation_id,
            "updated_at": time.time(),
        }
        if row is None:
            await session.execute(
                insert(source_lifecycles).values(
                    tenant_id=scope.tenant_id,
                    source_id=scope.source_id,
                    owner_key=scope.owner_key,
                    workspace_key=scope.workspace_key,
                    owner_user_id=scope.owner_user_id,
                    workspace_id=scope.workspace_id,
                    **values,
                )
            )
        else:
            await session.execute(
                update(source_lifecycles)
                .where(*self._predicate(scope))
                .values(**values)
            )
        return SourceDeletionPermit(scope, epoch, operation_id)

    async def begin_delete_in_session(
        self, session, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        """Fence a source in the caller's deletion-submit transaction."""
        return await self.begin_delete(
            session, scope, operation_id=operation_id
        )

    async def validate_deletion(
        self, session, permit: SourceDeletionPermit
    ) -> None:
        await self._lock_scope(session, permit.scope)
        row = (
            await session.execute(
                select(source_lifecycles)
                .where(*self._predicate(permit.scope))
                .with_for_update()
            )
        ).mappings().first()
        if (
            row is None
            or int(row["epoch"]) != permit.epoch
            or row["operation_id"] != permit.operation_id
            or row["state"]
            not in {
                SourceLifecycleState.DELETING.value,
                SourceLifecycleState.DELETED.value,
            }
        ):
            raise SourceLifecycleConflict(permit.scope.source_id)

    async def get_deletion_permit(
        self, session, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        """Restore a worker permit from the row locked in its transaction."""
        await self._lock_scope(session, scope)
        row = (
            await session.execute(
                select(source_lifecycles)
                .where(*self._predicate(scope))
                .with_for_update()
            )
        ).mappings().first()
        if (
            row is None
            or row["operation_id"] != operation_id
            or row["state"]
            not in {
                SourceLifecycleState.DELETING.value,
                SourceLifecycleState.DELETED.value,
            }
        ):
            raise SourceLifecycleConflict(scope.source_id)
        return SourceDeletionPermit(scope, int(row["epoch"]), operation_id)

    async def get_deletion_permit_in_session(
        self, session, scope: SourceScope, *, operation_id: str
    ) -> SourceDeletionPermit:
        return await self.get_deletion_permit(
            session, scope, operation_id=operation_id
        )

    async def validate_deletion_in_session(
        self, session, permit: SourceDeletionPermit
    ) -> None:
        await self.validate_deletion(session, permit)

    async def finish_delete(
        self, session, permit: SourceDeletionPermit
    ) -> None:
        await self.validate_deletion(session, permit)
        await session.execute(
            update(source_lifecycles)
            .where(*self._predicate(permit.scope))
            .values(
                state=SourceLifecycleState.DELETED.value,
                updated_at=time.time(),
            )
        )

    async def complete_delete(
        self, session, permit: SourceDeletionPermit
    ) -> None:
        """Complete deletion while retaining the durable identity tombstone."""
        await self.finish_delete(session, permit)

    async def complete_delete_in_session(
        self, session, permit: SourceDeletionPermit
    ) -> None:
        await self.complete_delete(session, permit)
