"""Postgres backend for local email/password credentials.

Same conventions as the other auth stores: every operation runs inside
:func:`~inqtrix.storage.db.tenant_session` (restricted role + tenant GUC,
forced RLS), guarded UPDATEs are replica-safe, and the store shares the
identity bundle's HTTP-loop engine. The owner bootstrap is race-safe via
``INSERT ... WHERE NOT EXISTS`` (rowcount tells the winner); the
functional unique index on ``lower(email)`` makes duplicate emails fail
closed rather than silently creating a second account.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.credentials import LocalCredential
from inqtrix.storage.credentials_orm import local_credentials as creds
from inqtrix.storage.db import tenant_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT_TENANT = "default"


def _record_from_row(row) -> LocalCredential:
    return LocalCredential(
        user_id=row.user_id,
        subject=row.subject,
        email=row.email,
        password_hash=row.password_hash,
        display_name=row.display_name,
        created_at=row.created_at,
        disabled_at=row.disabled_at,
    )


class PostgresCredentialStore:
    """Durable local-credential store (accounts survive restarts/replicas).

    Args:
        session_factory: Async session factory on the HTTP loop's engine.
        app_role: Restricted role assumed per transaction.
    """

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role

    def _scope(self):
        return tenant_session(
            self._session_factory,
            tenant_id=DEFAULT_TENANT,
            app_role=self._app_role,
        )

    async def count(self, *, tenant_id: str) -> int:
        async with self._scope() as db:
            result = await db.execute(
                select(func.count())
                .select_from(creds)
                .where(creds.c.tenant_id == tenant_id)
            )
        return int(result.scalar_one())

    async def create(
        self,
        credential: LocalCredential,
        *,
        tenant_id: str,
        allow_first_only: bool = False,
    ) -> bool:
        async with self._scope() as db:
            if allow_first_only:
                # Race-safe owner bootstrap: a transaction-scoped advisory
                # lock serializes concurrent setup attempts, then we insert
                # only if the tenant is still empty. The loser sees count>0
                # and returns False — exactly one owner can ever be created.
                await db.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtext('inqtrix_owner_bootstrap'))"
                    )
                )
                count = (
                    await db.execute(
                        select(func.count())
                        .select_from(creds)
                        .where(creds.c.tenant_id == tenant_id)
                    )
                ).scalar_one()
                if int(count) > 0:
                    return False
            # Duplicate email/subject violates a constraint;
            # on_conflict_do_nothing turns that into a refused (False)
            # rather than an error, so the route returns a clean 409.
            result = await db.execute(
                pg_insert(creds)
                .values(
                    user_id=credential.user_id,
                    subject=credential.subject,
                    tenant_id=tenant_id,
                    email=credential.email,
                    password_hash=credential.password_hash,
                    display_name=credential.display_name,
                    created_at=credential.created_at,
                    disabled_at=credential.disabled_at,
                )
                .on_conflict_do_nothing()
            )
            return bool(result.rowcount)

    async def get_by_email(
        self, *, tenant_id: str, email: str
    ) -> LocalCredential | None:
        async with self._scope() as db:
            row = (
                await db.execute(
                    select(creds).where(
                        creds.c.tenant_id == tenant_id,
                        func.lower(creds.c.email) == email.strip().lower(),
                    )
                )
            ).first()
        return _record_from_row(row) if row is not None else None

    async def get_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> LocalCredential | None:
        async with self._scope() as db:
            row = (
                await db.execute(
                    select(creds).where(
                        creds.c.tenant_id == tenant_id,
                        creds.c.user_id == user_id,
                    )
                )
            ).first()
        return _record_from_row(row) if row is not None else None

    async def set_password(
        self, *, tenant_id: str, user_id: uuid.UUID, password_hash: str
    ) -> bool:
        async with self._scope() as db:
            result = await db.execute(
                update(creds)
                .where(
                    creds.c.tenant_id == tenant_id,
                    creds.c.user_id == user_id,
                )
                .values(password_hash=password_hash)
            )
        return bool(result.rowcount)

    async def set_disabled(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float | None
    ) -> bool:
        async with self._scope() as db:
            result = await db.execute(
                update(creds)
                .where(
                    creds.c.tenant_id == tenant_id,
                    creds.c.user_id == user_id,
                )
                .values(disabled_at=disabled_at)
            )
        return bool(result.rowcount)

    async def list(self, *, tenant_id: str) -> tuple[LocalCredential, ...]:
        async with self._scope() as db:
            rows = (
                await db.execute(
                    select(creds)
                    .where(creds.c.tenant_id == tenant_id)
                    .order_by(creds.c.created_at.asc())
                )
            ).all()
        return tuple(_record_from_row(row) for row in rows)
