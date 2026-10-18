"""Canonical user fixtures shared by PostgreSQL storage contracts."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from inqtrix.storage.identity_orm import users

_TEST_USER_NAMESPACE = uuid.UUID("1e61ee88-e9bd-4d69-934d-485cc5fcce68")


def canonical_user_id(label: str) -> uuid.UUID:
    """Return a stable, valid UUID for one human-readable test label."""
    return uuid.uuid5(_TEST_USER_NAMESPACE, label)


async def ensure_canonical_users(
    session: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    *,
    tenant_id: str = "default",
) -> None:
    """Ensure active FK targets exist in an existing cleanup transaction."""
    for user_id in dict.fromkeys(user_ids):
        subject = f"storage-test-{user_id.hex}"
        statement = pg_insert(users).values(
            id=user_id,
            tenant_id=tenant_id,
            issuer="https://storage-tests.example",
            subject=subject,
            email=f"{subject}@example.com",
            email_verified=True,
            display_name=subject,
            disabled_at=None,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[users.c.id],
                set_={"disabled_at": None},
            )
        )
