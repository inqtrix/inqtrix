"""Postgres transaction backend for canonical-user lifecycle commands."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.invitations import RegistrationDenied
from inqtrix.auth.lifecycle import (
    AdminAuthorizationError,
    LoginCommand,
    UserDisabledError,
    UserLifecycleStatus,
)
from inqtrix.storage.auth_orm import auth_sessions
from inqtrix.storage.auth_postgres import (
    insert_auth_session,
    lock_tenant_security,
)
from inqtrix.storage.credentials_orm import local_credentials
from inqtrix.storage.db import tenant_session
from inqtrix.storage.identity_orm import active_admin, invitations, users
from inqtrix.storage.invitations_postgres import accept_open_invitations
from inqtrix.storage.resource_access import append_audit_row
from inqtrix.storage.pat_orm import personal_access_tokens
from inqtrix.storage.user_events_postgres import (
    append_instance_admin_invalidations,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from inqtrix.auth.credentials import LocalCredential
    from inqtrix.auth.directory import MirroredUser
    from inqtrix.auth.sessions import AuthSession


def _to_user(row) -> "MirroredUser":
    """Map one canonical user row to the domain record."""
    from inqtrix.auth.directory import MirroredUser

    return MirroredUser(
        user_id=row.id,
        tenant_id=row.tenant_id,
        issuer=row.issuer,
        subject=row.subject,
        email=row.email,
        email_verified=row.email_verified,
        display_name=row.display_name,
        disabled_at=(
            row.disabled_at.timestamp() if row.disabled_at is not None else None
        ),
        instance_role=row.instance_role,
        last_login_at=(
            row.last_login_at.timestamp()
            if row.last_login_at is not None
            else None
        ),
        default_workspace_id=row.default_workspace_id,
    )


class PostgresUserLifecycleTransaction:
    """Execute each lifecycle command in one tenant-scoped transaction."""

    atomic_effects = True
    """Audit and invalidations share the lifecycle database transaction."""

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role

    def _scope(self, tenant_id: str):
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    @staticmethod
    async def _lock_admin_actor_and_target(
        db: "AsyncSession",
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID,
        target_user_id: uuid.UUID | None = None,
    ) -> dict[uuid.UUID, object]:
        """Lock and revalidate the actor after the tenant security lock.

        User rows are locked in UUID order so every lifecycle command follows
        ``tenant_security_state -> users`` and concurrent commands cannot form
        an actor/target lock inversion.
        """
        requested_ids = {actor_user_id}
        if target_user_id is not None:
            requested_ids.add(target_user_id)
        rows = (
            await db.execute(
                select(users)
                .where(
                    users.c.tenant_id == tenant_id,
                    users.c.id.in_(sorted(requested_ids, key=str)),
                )
                .order_by(users.c.id)
                .with_for_update()
            )
        ).all()
        locked = {row.id: row for row in rows}
        actor = locked.get(actor_user_id)
        if (
            actor is None
            or actor.disabled_at is not None
            or actor.instance_role != "admin"
        ):
            raise AdminAuthorizationError(
                "instance-admin authority was revoked before commit"
            )
        return locked

    async def provision_login(self, command: LoginCommand) -> "MirroredUser":
        """Upsert user, accept invitations, grant admin, and mint session."""
        now = time.time()
        async with self._scope(command.tenant_id) as db:
            await lock_tenant_security(db, command.tenant_id)
            existing = (
                await db.execute(
                    select(users).where(
                        users.c.tenant_id == command.tenant_id,
                        users.c.issuer == command.issuer,
                        users.c.subject == command.subject,
                    )
                )
            ).first()
            if existing is not None and (
                existing.disabled_at is not None
                or existing.erasure_operation_id is not None
            ):
                # An account being erased is refused exactly like a disabled
                # one: its erasure lifted ``disabled_at`` only so the
                # deletion machine can act as its owner.
                raise UserDisabledError(command.subject)
            if command.invitation_required and existing is None:
                invitation = (
                    await db.execute(
                        select(invitations.c.id)
                        .where(
                            invitations.c.tenant_id == command.tenant_id,
                            func.lower(invitations.c.email)
                            == command.email.lower(),
                            invitations.c.accepted_at.is_(None),
                            invitations.c.revoked_at.is_(None),
                            invitations.c.expires_at
                            > dt.datetime.now(tz=dt.timezone.utc),
                        )
                        .limit(1)
                    )
                ).first()
                if invitation is None:
                    raise RegistrationDenied(
                        "Registration requires an invitation. Contact the "
                        "administrator."
                    )

            statement = pg_insert(users).values(
                id=command.session.user_id,
                tenant_id=command.tenant_id,
                issuer=command.issuer,
                subject=command.subject,
                email=command.email,
                email_verified=command.email_verified,
                display_name=command.display_name,
                last_login_at=func.now(),
            )
            row = (
                await db.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_users_tenant_issuer_subject",
                        set_={
                            "email": statement.excluded.email,
                            "email_verified": statement.excluded.email_verified,
                            "display_name": statement.excluded.display_name,
                            "last_login_at": func.now(),
                        },
                    ).returning(users)
                )
            ).first()
            assert row is not None

            accepted = ()
            if command.email:
                accepted = await accept_open_invitations(
                    db,
                    tenant_id=command.tenant_id,
                    email=command.email,
                    user_id=row.id,
                    now=now,
                )
            if command.invitation_required and existing is None and not accepted:
                raise RegistrationDenied(
                    "Registration requires an invitation. Contact the "
                    "administrator."
                )
            for invitation in accepted:
                await append_audit_row(
                    db,
                    tenant_id=command.tenant_id,
                    actor_user_id=row.id,
                    action="invitation.accepted",
                    resource_type="registration",
                    resource_id=invitation.id,
                )

            if command.is_admin:
                await db.execute(
                    update(users)
                    .where(users.c.id == row.id)
                    .values(instance_role="admin")
                )
            elif command.first_login_owner:
                existing_admin = (
                    await db.execute(
                        select(users.c.id)
                        .where(
                            users.c.tenant_id == command.tenant_id,
                            active_admin(),
                        )
                        .limit(1)
                    )
                ).first()
                if existing_admin is None:
                    await db.execute(
                        update(users)
                        .where(users.c.id == row.id)
                        .values(instance_role="admin")
                    )
            await insert_auth_session(
                db,
                tenant_id=command.tenant_id,
                session=replace(command.session, user_id=row.id),
            )
            refreshed = (
                await db.execute(select(users).where(users.c.id == row.id))
            ).first()
            assert refreshed is not None
            await append_audit_row(
                db,
                tenant_id=command.tenant_id,
                actor_user_id=row.id,
                action="user.login_admitted",
                resource_type="user",
                resource_id=str(row.id),
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=command.tenant_id,
                target_user_ids=(row.id,),
                scope="account",
                resource_type="user",
                resource_id=str(row.id),
            )
            return _to_user(refreshed)

    async def create_local_account(
        self,
        *,
        tenant_id: str,
        credential: "LocalCredential",
        role: Literal["admin", "user"],
        session: "AuthSession | None",
        first_only: bool,
        actor_user_id: uuid.UUID | None = None,
    ) -> "MirroredUser | None":
        """Create mirror, credential, role, and optional session together."""
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            if not first_only:
                if actor_user_id is None:
                    raise AdminAuthorizationError(
                        "admin-created accounts require an effective actor"
                    )
                await self._lock_admin_actor_and_target(
                    db,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                )
            if first_only:
                first = (
                    await db.execute(
                        select(local_credentials.c.user_id)
                        .where(local_credentials.c.tenant_id == tenant_id)
                        .limit(1)
                    )
                ).first()
                if first is not None:
                    return None
            duplicate = (
                await db.execute(
                    select(local_credentials.c.user_id)
                    .where(
                        local_credentials.c.tenant_id == tenant_id,
                        func.lower(local_credentials.c.email)
                        == credential.email.lower(),
                    )
                    .limit(1)
                )
            ).first()
            if duplicate is not None:
                return None
            row = (
                await db.execute(
                    users.insert()
                    .values(
                        id=credential.user_id,
                        tenant_id=tenant_id,
                        issuer="local",
                        subject=credential.subject,
                        email=credential.email,
                        email_verified=True,
                        display_name=credential.display_name,
                        instance_role=role,
                        last_login_at=func.now() if session is not None else None,
                    )
                    .returning(users)
                )
            ).first()
            assert row is not None
            await db.execute(
                local_credentials.insert().values(
                    user_id=credential.user_id,
                    tenant_id=tenant_id,
                    subject=credential.subject,
                    email=credential.email,
                    password_hash=credential.password_hash,
                    display_name=credential.display_name,
                    created_at=credential.created_at,
                    disabled_at=credential.disabled_at,
                )
            )
            if session is not None:
                await insert_auth_session(db, tenant_id=tenant_id, session=session)
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id or credential.user_id,
                action="user.created",
                resource_type="user",
                resource_id=str(credential.user_id),
                detail={"instance_role": role},
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=tenant_id,
                target_user_ids=(credential.user_id,),
                scope="account",
                resource_type="user",
                resource_id=str(credential.user_id),
            )
            return _to_user(row)

    async def set_role(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        role: Literal["admin", "user"],
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Apply one role change under the tenant security lock."""
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_admin_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            target = locked.get(user_id)
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            if target.erasure_operation_id is not None:
                return UserLifecycleStatus.ERASING
            if (
                role == "user"
                and target.instance_role == "admin"
                and target.disabled_at is None
            ):
                other_admin = (
                    await db.execute(
                        select(users.c.id)
                        .where(
                            users.c.tenant_id == tenant_id,
                            active_admin(),
                            users.c.id != user_id,
                        )
                        .limit(1)
                    )
                ).first()
                if other_admin is None:
                    return UserLifecycleStatus.LAST_ADMIN
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                .values(instance_role=role)
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.role_updated",
                resource_type="user",
                resource_id=str(user_id),
                detail={"instance_role": role},
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=tenant_id,
                target_user_ids=(user_id,),
                scope="account",
                resource_type="user",
                resource_id=str(user_id),
            )
            return UserLifecycleStatus.UPDATED

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Apply disable state and all dependent revocations atomically."""
        value = (
            dt.datetime.fromtimestamp(disabled_at, tz=dt.timezone.utc)
            if disabled_at is not None
            else None
        )
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_admin_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            target = locked.get(user_id)
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            if target.erasure_operation_id is not None:
                return UserLifecycleStatus.ERASING
            if (
                disabled_at is not None
                and target.instance_role == "admin"
                and target.disabled_at is None
            ):
                other_admin = (
                    await db.execute(
                        select(users.c.id)
                        .where(
                            users.c.tenant_id == tenant_id,
                            active_admin(),
                            users.c.id != user_id,
                        )
                        .limit(1)
                    )
                ).first()
                if other_admin is None:
                    return UserLifecycleStatus.LAST_ADMIN
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                .values(disabled_at=value)
            )
            if disabled_at is not None:
                await self._cut_off(
                    db, tenant_id=tenant_id, user_id=user_id, now=disabled_at
                )
            else:
                await db.execute(
                    update(local_credentials)
                    .where(
                        local_credentials.c.tenant_id == tenant_id,
                        local_credentials.c.user_id == user_id,
                    )
                    .values(disabled_at=None)
                )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=(
                    "user.disabled"
                    if disabled_at is not None
                    else "user.enabled"
                ),
                resource_type="user",
                resource_id=str(user_id),
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=tenant_id,
                target_user_ids=(user_id,),
                scope="account",
                resource_type="user",
                resource_id=str(user_id),
            )
            return UserLifecycleStatus.UPDATED

    async def inventory(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Count what an erasure of this account would reach.

        Read-only. The confirmation an administrator sees and the work
        the erasure performs walk the same plan, so the number on the
        dialog cannot drift away from what actually happens.
        """
        from inqtrix.services.user_erasure_inventory import collect_inventory

        async with self._scope(tenant_id) as db:
            return (
                await collect_inventory(db, user_id=user_id)
            ).to_payload()

    async def begin_erasure(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        operation_id: str,
    ) -> tuple[UserLifecycleStatus, dict[str, Any]]:
        """Cut an account off and mark it as erased by one operation.

        Every way in closes in this one transaction: browser sessions go,
        tokens are revoked, the local credential is refused, and the
        operation id on the ``users`` row makes every sign-in and every
        enable refuse the account from now on. ``disabled_at`` is lifted on
        purpose: resource authorization requires an active owner, and the
        deletion machine acts as this owner while it removes the account's
        aggregates. Nobody else can hold a principal for it any more.

        Idempotent: an account already being erased answers with the
        operation that erases it. ``REFUSED`` carries the reasons the
        erasure preview shows as blockers -- they come from the same
        function.
        """
        from inqtrix.services.user_erasure_inventory import erasure_blockers

        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_admin_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            target = locked.get(user_id)
            if target is None:
                return UserLifecycleStatus.NOT_FOUND, {}
            if target.erasure_operation_id is not None:
                return UserLifecycleStatus.UPDATED, {
                    "operation_id": target.erasure_operation_id
                }
            if target.instance_role == "admin" and target.disabled_at is None:
                other_admin = (
                    await db.execute(
                        select(users.c.id)
                        .where(
                            users.c.tenant_id == tenant_id,
                            active_admin(),
                            users.c.id != user_id,
                        )
                        .limit(1)
                    )
                ).first()
                if other_admin is None:
                    return UserLifecycleStatus.LAST_ADMIN, {}
            blockers = await erasure_blockers(db, user_id=user_id)
            if blockers:
                return UserLifecycleStatus.REFUSED, {"refused": list(blockers)}
            now = time.time()
            await self._cut_off(db, tenant_id=tenant_id, user_id=user_id, now=now)
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                .values(disabled_at=None, erasure_operation_id=operation_id)
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.erasure_started",
                resource_type="user",
                resource_id=str(user_id),
                detail={"operation_id": operation_id},
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=tenant_id,
                target_user_ids=(user_id,),
                scope="account",
                resource_type="user",
                resource_id=str(user_id),
            )
            return UserLifecycleStatus.UPDATED, {"operation_id": operation_id}

    async def erasure_operation_id(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> tuple[bool, str | None]:
        """Whether the account still exists, and the operation erasing it."""
        async with self._scope(tenant_id) as db:
            row = (
                await db.execute(
                    select(users.c.erasure_operation_id).where(
                        users.c.tenant_id == tenant_id,
                        users.c.id == user_id,
                    )
                )
            ).first()
        if row is None:
            return False, None
        return True, row.erasure_operation_id

    async def finish_erasure(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        operation_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> UserLifecycleStatus:
        """Remove the ``users`` row once nothing else names the account.

        The last step of an erasure, under the tenant's security lock like
        every other identity change and as short as one row. The audit row
        is written BEFORE the user row goes: its actor is the
        administrator, and the erased id is the resource it names.
        ``NOT_FOUND`` means an earlier attempt already finished.
        """
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            row = (
                await db.execute(
                    select(users.c.erasure_operation_id)
                    .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                    .with_for_update()
                )
            ).first()
            if row is None:
                return UserLifecycleStatus.NOT_FOUND
            if row.erasure_operation_id != operation_id:
                raise RuntimeError(
                    "the account is not being erased by this operation"
                )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.erased",
                resource_type="user",
                resource_id=str(user_id),
                detail={"operation_id": operation_id},
            )
            await db.execute(
                delete(users).where(
                    users.c.tenant_id == tenant_id, users.c.id == user_id
                )
            )
            return UserLifecycleStatus.UPDATED

    @staticmethod
    async def _cut_off(
        db: "AsyncSession",
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        now: float,
    ) -> None:
        """Close every way into the account: sessions, tokens, password."""
        await db.execute(
            update(local_credentials)
            .where(
                local_credentials.c.tenant_id == tenant_id,
                local_credentials.c.user_id == user_id,
            )
            .values(disabled_at=now)
        )
        await db.execute(
            delete(auth_sessions).where(
                auth_sessions.c.tenant_id == tenant_id,
                auth_sessions.c.user_id == user_id,
            )
        )
        await db.execute(
            update(personal_access_tokens)
            .where(
                personal_access_tokens.c.tenant_id == tenant_id,
                personal_access_tokens.c.owner_user_id == user_id,
                personal_access_tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def reset_local_password(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        password_hash: str,
        actor_user_id: uuid.UUID,
    ) -> bool:
        """Replace a credential hash and purge sessions in one transaction."""
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_admin_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            if user_id not in locked:
                return False
            result = await db.execute(
                update(local_credentials)
                .where(
                    local_credentials.c.tenant_id == tenant_id,
                    local_credentials.c.user_id == user_id,
                )
                .values(password_hash=password_hash)
            )
            if not result.rowcount:
                return False
            await db.execute(
                delete(auth_sessions).where(
                    auth_sessions.c.tenant_id == tenant_id,
                    auth_sessions.c.user_id == user_id,
                )
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.password_reset",
                resource_type="user",
                resource_id=str(user_id),
            )
            await append_instance_admin_invalidations(
                db,
                tenant_id=tenant_id,
                target_user_ids=(user_id,),
                scope="account",
                resource_type="user",
                resource_id=str(user_id),
            )
            return True
