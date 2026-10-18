"""Shared ownership guards for client-addressed project resources.

Project resource ids are opaque, globally unique client identifiers.  A
collision must therefore never turn an upsert into a write to another user or
workspace.  This module keeps the Postgres conflict predicate and the memory
store parity check in one place so every persistence implementation enforces
the same contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession


_RecordT = TypeVar("_RecordT")


@dataclass(frozen=True)
class ResourceScope:
    """Immutable owner/workspace identity expected by one mutation."""

    created_by_user_id: uuid.UUID | None
    workspace_id: str | None

    @classmethod
    def from_record(cls, record: Any) -> "ResourceScope":
        """Capture the immutable scope authorized by the service layer."""
        return cls(
            created_by_user_id=record.created_by_user_id,
            workspace_id=record.workspace_id,
        )


def scoped_postgres_upsert(
    insert_stmt: Any,
    table: Any,
    values: Mapping[str, Any],
    mutable_columns: Sequence[str],
    *,
    extra_condition: Any | None = None,
    column_updates: Mapping[str, Any] | None = None,
) -> Any:
    """Build an idempotent upsert that cannot cross an ownership scope.

    Args:
        insert_stmt: PostgreSQL ``insert(table)`` statement.
        table: SQLAlchemy table containing the three scope columns.
        values: Complete values for the insert branch.
        mutable_columns: Columns updated when the same scoped id exists.
        extra_condition: Optional CAS or lifecycle predicate combined with the
            ownership guard for the update branch.
        column_updates: Expressions for selected mutable columns, evaluated
            atomically by the update. Ownership columns cannot be overridden.

    Returns:
        A PostgreSQL insert statement with a scope-guarded conflict update.
    """
    stmt = insert_stmt.values(**values)
    condition = and_(
        table.c.tenant_id == stmt.excluded.tenant_id,
        table.c.created_by_user_id.is_not_distinct_from(
            stmt.excluded.created_by_user_id
        ),
        table.c.workspace_id.is_not_distinct_from(stmt.excluded.workspace_id),
    )
    if extra_condition is not None:
        condition = and_(condition, extra_condition)
    updates = {
        column: getattr(stmt.excluded, column) for column in mutable_columns
    }
    if column_updates:
        if not set(column_updates).issubset(mutable_columns):
            raise ValueError("Column updates must name mutable columns only")
        updates.update(column_updates)
    return stmt.on_conflict_do_update(
        index_elements=[table.c.id],
        set_=updates,
        where=condition,
    )


async def require_scoped_parent(
    session: AsyncSession,
    *,
    table: Any,
    parent_id: str,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    not_found: Callable[[str], Exception],
    extra_condition: Any | None = None,
) -> None:
    """Lock and validate a parent inside the child's write transaction.

    The row lock prevents deletion and changes to mutable parent facts (for
    example a group's section) between validation and the child write.
    """
    conditions = [
        table.c.id == parent_id,
        table.c.tenant_id == tenant_id,
        table.c.created_by_user_id.is_not_distinct_from(created_by_user_id),
        table.c.workspace_id.is_not_distinct_from(workspace_id),
    ]
    if extra_condition is not None:
        conditions.append(extra_condition)
    query = (
        select(table.c.id)
        .where(*conditions)
        .with_for_update()
    )
    if (await session.execute(query)).scalar_one_or_none() is None:
        raise not_found(parent_id)


async def delete_scoped_postgres(
    session: AsyncSession,
    *,
    table: Any,
    resource_id: str,
    tenant_id: str,
    scope: ResourceScope,
    not_found: Callable[[str], Exception],
    extra_condition: Any | None = None,
) -> None:
    """Delete only the exact scope previously authorized by the service.

    The scope predicate closes the authorization/delete race: if the old row
    disappears and another user recreates the opaque id, this statement
    changes no row and returns the same indistinct not-found result.
    """
    conditions = [
        table.c.id == resource_id,
        table.c.tenant_id == tenant_id,
        table.c.created_by_user_id.is_not_distinct_from(
            scope.created_by_user_id
        ),
        table.c.workspace_id.is_not_distinct_from(scope.workspace_id),
    ]
    if extra_condition is not None:
        conditions.append(extra_condition)
    deleted_id = (
        await session.execute(
            delete(table).where(*conditions).returning(table.c.id)
        )
    ).scalar_one_or_none()
    if deleted_id is None:
        raise not_found(resource_id)


def require_memory_scope(
    record: _RecordT | None,
    *,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    resource_id: str,
    not_found: Callable[[str], Exception],
) -> _RecordT:
    """Return a memory record only when its immutable scope matches exactly."""
    if (
        record is None
        or getattr(record, "created_by_user_id", None) != created_by_user_id
        or getattr(record, "workspace_id", None) != workspace_id
    ):
        raise not_found(resource_id)
    return record
