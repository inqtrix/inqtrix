"""Persistence helpers that prevent deleted resources from being resurrected."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from inqtrix.storage.deletions_orm import deletion_operations


async def reject_retained_deletion_target(
    session,
    *,
    target_kind: str,
    target_id: str,
    tenant_id: str,
    not_found: Callable[[str], Exception],
) -> None:
    """Reject recreation while the durable deletion receipt is retained.

    Session rows are removed only after their aggregate reaches zero residuals.
    The retained operation receipt then remains the write fence for stale tabs
    and delayed autosaves, without keeping user session content alive.
    """

    operation_id = await session.scalar(
        select(deletion_operations.c.operation_id)
        .where(
            deletion_operations.c.tenant_id == tenant_id,
            deletion_operations.c.target_kind == target_kind,
            deletion_operations.c.target_id == target_id,
        )
        .limit(1)
    )
    if operation_id is not None:
        raise not_found(target_id)
