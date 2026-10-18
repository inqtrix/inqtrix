"""Postgres backend for invitations and the admission lookup.

Same conventions as the other storage backends (tenant_session,
restricted role, forced RLS). The acceptance is the load-bearing
piece: one transaction flips every matching open invitation via a
guarded UPDATE (exactly-once across replicas — the loser of a
concurrent race matches zero rows) and creates the granted workspace
memberships with ``ON CONFLICT DO NOTHING`` so an existing — possibly
higher — role is never downgraded. Either everything lands or nothing
does.

Schema note: the ``invitations`` table (revision 0001) stores
timezone-aware datetimes and UUID ids; this module converts to the
domain dataclass's unix-second floats and string ids at the mapping
edge — the 0001 schema stays untouched.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from inqtrix.auth.invitations import DuplicateOpenInvitation, Invitation
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.storage.db import tenant_session
from inqtrix.storage.identity_orm import (
    invitations,
    workspace_members,
    workspaces,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _to_datetime(unix_seconds: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(unix_seconds, tz=dt.timezone.utc)


def _to_unix(value: dt.datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _record_from_row(row) -> Invitation:
    return Invitation(
        id=str(row.id),
        tenant_id=row.tenant_id,
        workspace_id=str(row.workspace_id),
        email=row.email,
        role=WorkspaceRole(row.role),
        invited_by_user_id=row.invited_by_user_id,
        created_at=_to_unix(row.created_at) or 0.0,
        expires_at=_to_unix(row.expires_at) or 0.0,
        accepted_at=_to_unix(row.accepted_at),
        accepted_by_user_id=row.accepted_by_user_id,
        revoked_at=_to_unix(row.revoked_at),
    )


async def accept_open_invitations(
    db: "AsyncSession",
    *,
    tenant_id: str,
    email: str,
    user_id: uuid.UUID,
    now: float,
) -> tuple[Invitation, ...]:
    """Consume matching invitations and grant memberships in one transaction.

    Workspace rows are locked before invitation rows. Workspace deletion uses
    the same order, preventing the workspace/invitation lock inversion that
    would otherwise deadlock a login against a concurrent deletion. Both the
    foreign-key parent and the invitation's open status are rechecked inside
    this transaction before the guarded update.
    """
    candidate_workspace_ids = tuple(
        (
            await db.execute(
                select(invitations.c.workspace_id)
                .where(
                    invitations.c.tenant_id == tenant_id,
                    func.lower(invitations.c.email) == email.lower(),
                    invitations.c.accepted_at.is_(None),
                    invitations.c.revoked_at.is_(None),
                    invitations.c.expires_at > _to_datetime(now),
                )
                .distinct()
                .order_by(invitations.c.workspace_id)
            )
        ).scalars()
    )
    if not candidate_workspace_ids:
        return ()
    locked_workspace_ids = tuple(
        (
            await db.execute(
                select(workspaces.c.id)
                .where(
                    workspaces.c.tenant_id == tenant_id,
                    workspaces.c.id.in_(candidate_workspace_ids),
                )
                .order_by(workspaces.c.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not locked_workspace_ids:
        return ()
    consumed = (
        await db.execute(
            update(invitations)
            .where(
                invitations.c.tenant_id == tenant_id,
                invitations.c.workspace_id.in_(locked_workspace_ids),
                func.lower(invitations.c.email) == email.lower(),
                invitations.c.accepted_at.is_(None),
                invitations.c.revoked_at.is_(None),
                invitations.c.expires_at > _to_datetime(now),
            )
            .values(accepted_at=_to_datetime(now), accepted_by_user_id=user_id)
            .returning(
                invitations.c.id,
                invitations.c.workspace_id,
                invitations.c.email,
                invitations.c.role,
                invitations.c.invited_by_user_id,
                invitations.c.created_at,
                invitations.c.expires_at,
            )
        )
    ).all()
    for row in consumed:
        await db.execute(
            pg_insert(workspace_members)
            .values(
                tenant_id=tenant_id,
                workspace_id=row.workspace_id,
                user_id=user_id,
                role=row.role,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "user_id"])
        )
    return tuple(
        Invitation(
            id=str(row.id),
            tenant_id=tenant_id,
            workspace_id=str(row.workspace_id),
            email=row.email,
            role=WorkspaceRole(row.role),
            invited_by_user_id=row.invited_by_user_id,
            created_at=_to_unix(row.created_at) or 0.0,
            expires_at=_to_unix(row.expires_at) or 0.0,
            accepted_at=now,
            accepted_by_user_id=user_id,
        )
        for row in consumed
    )


class PostgresInvitationRepository:
    """Durable invitations (admission survives restarts and replicas).

    Args:
        session_factory: Async session factory on the HTTP loop's
            engine.
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

    def _scope(self, tenant_id: str = "default"):
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def create(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        email: str,
        role: WorkspaceRole,
        invited_by_user_id: uuid.UUID,
        expires_at: float,
    ) -> Invitation:
        invitation_id = uuid.uuid4()
        try:
            async with self._scope(tenant_id) as db:
                await db.execute(
                    invitations.insert().values(
                        id=invitation_id,
                        tenant_id=tenant_id,
                        workspace_id=uuid.UUID(workspace_id),
                        email=email,
                        role=role.value,
                        invited_by_user_id=invited_by_user_id,
                        expires_at=_to_datetime(expires_at),
                    )
                )
        except IntegrityError as exc:
            # uq_invitations_open: one open invitation per
            # (workspace, lower(email)).
            raise DuplicateOpenInvitation(email) from exc
        return Invitation(
            id=str(invitation_id),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            email=email,
            role=role,
            invited_by_user_id=invited_by_user_id,
            created_at=time.time(),
            expires_at=expires_at,
        )

    async def list_for_workspace(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[Invitation, ...]:
        async with self._scope(tenant_id) as db:
            rows = (
                await db.execute(
                    select(invitations)
                    .where(
                        invitations.c.tenant_id == tenant_id,
                        invitations.c.workspace_id == uuid.UUID(workspace_id),
                    )
                    .order_by(invitations.c.created_at.desc())
                )
            ).all()
        return tuple(_record_from_row(row) for row in rows)

    async def revoke(
        self, *, tenant_id: str, workspace_id: str, invitation_id: str,
        now: float,
    ) -> bool:
        async with self._scope(tenant_id) as db:
            result = await db.execute(
                update(invitations)
                .where(
                    invitations.c.tenant_id == tenant_id,
                    invitations.c.workspace_id == uuid.UUID(workspace_id),
                    invitations.c.id == uuid.UUID(invitation_id),
                    invitations.c.accepted_at.is_(None),
                    invitations.c.revoked_at.is_(None),
                )
                .values(revoked_at=_to_datetime(now))
            )
        return bool(result.rowcount)

    async def has_open_for_email(
        self, *, tenant_id: str, email: str, now: float
    ) -> bool:
        async with self._scope(tenant_id) as db:
            row = (
                await db.execute(
                    select(invitations.c.id)
                    .where(
                        invitations.c.tenant_id == tenant_id,
                        func.lower(invitations.c.email) == email.lower(),
                        invitations.c.accepted_at.is_(None),
                        invitations.c.revoked_at.is_(None),
                        invitations.c.expires_at > _to_datetime(now),
                    )
                    .limit(1)
                )
            ).first()
        return row is not None

    async def accept_open_for_email(
        self, *, tenant_id: str, email: str, user_id: uuid.UUID, now: float
    ) -> tuple[Invitation, ...]:
        """One transaction: guarded consume + membership grants."""
        async with self._scope(tenant_id) as db:
            return await accept_open_invitations(
                db,
                tenant_id=tenant_id,
                email=email,
                user_id=user_id,
                now=now,
            )
