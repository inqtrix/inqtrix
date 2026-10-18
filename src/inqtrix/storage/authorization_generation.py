"""Commit-ordered per-user authorization generation (own table).

The generation is a HINT for long-lived readers (SSE frame gates), never
an authorization decision: a moved value tells the reader to re-run the
full authoritative chain; over-invalidation is harmless. The bump runs
INSIDE the mutating transaction, so the generation row's exclusive lock
serializes concurrent permission mutations per user and the value is
commit-ordered — a sequence could not provide that (values are assigned
at ``nextval()`` and can surface out of commit order).

Deliberately NOT a users column: authorization reads hold ``FOR SHARE``
on the users row (``lock_active_users``), and updating that same row in
the same transactions is a share-to-exclusive upgrade — two concurrent
permission mutations then deadlock. The dedicated row has no other
readers, so its exclusive lock is a clean serialization point.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.storage.identity_orm import (
    user_authorization_generations,
    users,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def bump_authorization_generation(
    session: "AsyncSession",
    *,
    tenant_id: str,
    target_user_ids: Sequence[uuid.UUID],
) -> None:
    """Advance each target's generation inside the caller's transaction.

    Callers pass targets in a STABLE SORTED order (every existing
    invalidation writer sorts by ``str``): multi-user bumps then lock
    generation rows in the same order everywhere (deadlock protection
    among bumps; resource locks always precede the bump because every
    caller bumps after its resource writes).
    """
    for target_user_id in target_user_ids:
        stmt = pg_insert(user_authorization_generations).values(
            tenant_id=tenant_id,
            user_id=target_user_id,
            generation=1,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    user_authorization_generations.c.tenant_id,
                    user_authorization_generations.c.user_id,
                ],
                set_={
                    "generation": (
                        user_authorization_generations.c.generation + 1
                    )
                },
            )
        )


async def read_authorization_generation(
    session: "AsyncSession",
    *,
    tenant_id: str,
    user_id: uuid.UUID,
) -> int | None:
    """The user's current generation; 0 before the first bump.

    ``None`` means the USER is unknown (api-key principals): frame gates
    then fall back to the full chain. A known user without a generation
    row reads 0 — a stable value the gate can cache — so never-mutated
    users still benefit from the cheap path.
    """
    row = (
        await session.execute(
            select(user_authorization_generations.c.generation)
            .select_from(
                users.outerjoin(
                    user_authorization_generations,
                    (
                        user_authorization_generations.c.tenant_id
                        == users.c.tenant_id
                    )
                    & (user_authorization_generations.c.user_id == users.c.id),
                )
            )
            .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return int(row.generation) if row.generation is not None else 0
