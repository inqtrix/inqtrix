"""Postgres transaction backend for canonical-user lifecycle commands."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.invitations import (
    CONFIRMED_EMAIL_REQUIRED,
    INVITATION_REQUIRED,
    RegistrationDenied,
)
from inqtrix.auth.lifecycle import (
    RESTORED_FROM_DEPROVISIONING,
    AdminActor,
    AdminAuthorizationError,
    ExternalProfile,
    LifecycleActor,
    LoginCommand,
    ProfileChange,
    ProvisioningActor,
    ProvisioningOutcome,
    UserDisabledError,
    UserLifecycleStatus,
    UserNotProvisionedError,
    audit_actor,
    changed_scim_fields,
    checked_source_issuer,
    disable_source_of,
    external_profile,
    login_role_source,
    merge_login_profile,
    provisioning_issuer,
    restores_tombstone,
    scim_fields,
)
from inqtrix.storage.auth_orm import auth_sessions
from inqtrix.storage.auth_postgres import (
    PostgresUserDirectory,
    insert_auth_session,
    lock_tenant_security,
)
from inqtrix.storage.credentials_orm import local_credentials
from inqtrix.storage.db import tenant_session
from inqtrix.storage.identity_orm import active_admin, audit_log, invitations, users
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


class PostgresUserLifecycleTransaction:
    """Execute each lifecycle command in one tenant-scoped transaction.

    Args:
        session_factory: Async session factory on the caller's loop.
        app_role: Restricted role assumed per transaction.
        source_issuer: Issuer key of the active identity source. The
            first-owner rule and the last-admin guards count only active
            admins bound under it; ``None`` counts every issuer.
    """

    atomic_effects = True
    """Audit and invalidations share the lifecycle database transaction."""

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
        source_issuer: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role
        self._source_issuer = checked_source_issuer(source_issuer)

    @property
    def source_issuer(self) -> str | None:
        """Issuer whose admins the first-owner rule and the guards count."""
        return self._source_issuer

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

    async def _lock_actor_and_target(
        self,
        db: "AsyncSession",
        *,
        tenant_id: str,
        actor: LifecycleActor,
        target_user_id: uuid.UUID,
    ) -> dict[uuid.UUID, object]:
        """Lock the target and revalidate the actor after the tenant lock.

        An administrator is locked and revalidated together with the target.
        Provisioning locks only the target and reaches it only when it is
        bound under the identity source's issuer; any other account is
        absent for it.
        """
        if isinstance(actor, AdminActor):
            return await self._lock_admin_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor.user_id,
                target_user_id=target_user_id,
            )
        issuer = provisioning_issuer(self._source_issuer)
        row = (
            await db.execute(
                select(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == target_user_id)
                .with_for_update()
            )
        ).first()
        if row is None or row.issuer != issuer:
            return {}
        return {row.id: row}

    async def provision_login(self, command: LoginCommand) -> "MirroredUser":
        """Upsert user, accept invitations, grant admin, and mint session."""
        from inqtrix.auth.directory import MirroredUser

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
            if command.provisioned_only and existing is None:
                raise UserNotProvisionedError("no account is provisioned for this identity")
            if command.invitation_required and existing is None:
                if not command.accepts_invitations:
                    raise RegistrationDenied(
                        CONFIRMED_EMAIL_REQUIRED, reason="email_unverified"
                    )
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
                        INVITATION_REQUIRED, reason="no_invitation"
                    )

            # ``existing`` was read under the tenant lock that every
            # lifecycle command writing the profile takes, so the merge
            # cannot race another profile update.
            profile = merge_login_profile(existing, command)
            statement = pg_insert(users).values(
                id=command.session.user_id,
                tenant_id=command.tenant_id,
                issuer=command.issuer,
                subject=command.subject,
                email=profile.email,
                email_verified=profile.email_verified,
                display_name=profile.display_name,
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
            if command.accepts_invitations:
                accepted = await accept_open_invitations(
                    db,
                    tenant_id=command.tenant_id,
                    email=command.email,
                    user_id=row.id,
                    now=now,
                )
            if command.invitation_required and existing is None and not accepted:
                raise RegistrationDenied(INVITATION_REQUIRED, reason="no_invitation")
            for invitation in accepted:
                await append_audit_row(
                    db,
                    tenant_id=command.tenant_id,
                    actor_user_id=row.id,
                    action="invitation.accepted",
                    resource_type="registration",
                    resource_id=invitation.id,
                )

            grant_admin = command.is_admin
            if not grant_admin and command.first_login_owner:
                grant_admin = (
                    await db.execute(
                        select(users.c.id)
                        .where(
                            users.c.tenant_id == command.tenant_id,
                            active_admin(self._source_issuer),
                        )
                        .limit(1)
                    )
                ).first() is None
            if grant_admin:
                await db.execute(
                    update(users)
                    .where(users.c.id == row.id)
                    .values(instance_role="admin")
                )
            await insert_auth_session(
                db,
                tenant_id=command.tenant_id,
                session=replace(
                    command.session,
                    user_id=row.id,
                    email=profile.email or None,
                    display_name=profile.display_name,
                ),
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
            # ``row`` carries the role from before this login: the upsert
            # never touches it.
            if grant_admin and row.instance_role != "admin":
                await append_audit_row(
                    db,
                    tenant_id=command.tenant_id,
                    actor_user_id=row.id,
                    action="user.role_updated",
                    resource_type="user",
                    resource_id=str(row.id),
                    detail={
                        "instance_role": "admin",
                        "source": login_role_source(command),
                    },
                )
            await append_instance_admin_invalidations(
                db,
                tenant_id=command.tenant_id,
                target_user_ids=(row.id,),
                scope="account",
                resource_type="user",
                resource_id=str(row.id),
            )
            return PostgresUserDirectory._to_user(refreshed, MirroredUser)

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
        from inqtrix.auth.directory import MirroredUser

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
            return PostgresUserDirectory._to_user(row, MirroredUser)

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
                            active_admin(self._source_issuer),
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
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Apply disable state and all dependent revocations atomically.

        The actor decides the source of a disable. Provisioning never takes
        over a disable it did not apply and lifts only its own; asked to
        lift an administrator's, it leaves the account disabled.
        """
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor=actor,
                target_user_id=user_id,
            )
            target = locked.get(user_id)
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            if target.erasure_operation_id is not None:
                return UserLifecycleStatus.ERASING
            return await self._apply_disable(
                db,
                tenant_id=tenant_id,
                target=target,
                disabled_at=disabled_at,
                actor=actor,
            )

    @staticmethod
    async def _wake_admins(db: "AsyncSession", *, tenant_id: str, user_id: uuid.UUID) -> None:
        """Wake every active administrator's views of *user_id*."""
        await append_instance_admin_invalidations(
            db,
            tenant_id=tenant_id,
            target_user_ids=(user_id,),
            scope="account",
            resource_type="user",
            resource_id=str(user_id),
        )

    async def _apply_disable(
        self,
        db: "AsyncSession",
        *,
        tenant_id: str,
        target,
        disabled_at: float | None,
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Disable or enable a locked target inside the caller's transaction."""
        user_id = target.id
        actor_user_id, actor_type = audit_actor(actor)
        if isinstance(actor, ProvisioningActor) and target.disabled_at is not None:
            if disabled_at is not None:
                # Already disabled: the disable keeps its source.
                return UserLifecycleStatus.UPDATED
            if target.scim_deleted_at is not None:
                # A tombstone comes back only through an administrator.
                return UserLifecycleStatus.NOT_FOUND
            if target.disabled_source != "provisioning":
                await append_audit_row(
                    db,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.scim_enable_ignored",
                    resource_type="user",
                    resource_id=str(user_id),
                )
                return UserLifecycleStatus.KEPT_DISABLED
        if disabled_at is not None and await self._is_last_admin_of_source(
            db, tenant_id=tenant_id, target=target
        ):
            return UserLifecycleStatus.LAST_ADMIN
        value = (
            dt.datetime.fromtimestamp(disabled_at, tz=dt.timezone.utc)
            if disabled_at is not None
            else None
        )
        restored = restores_tombstone(target, disabled_at=disabled_at)
        values: dict[str, object] = {
            "disabled_at": value,
            "disabled_source": (
                disable_source_of(actor) if disabled_at is not None else None
            ),
        }
        if restored:
            values["scim_deleted_at"] = None
        await db.execute(
            update(users)
            .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
            .values(**values)
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
            actor_type=actor_type,
            action=(
                "user.disabled"
                if disabled_at is not None
                else "user.enabled"
            ),
            resource_type="user",
            resource_id=str(user_id),
            detail=RESTORED_FROM_DEPROVISIONING if restored else None,
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

    async def _is_last_admin_of_source(
        self, db: "AsyncSession", *, tenant_id: str, target
    ) -> bool:
        """Whether disabling *target* would leave the source without an admin."""
        if target.instance_role != "admin" or target.disabled_at is not None:
            return False
        other_admin = (
            await db.execute(
                select(users.c.id)
                .where(
                    users.c.tenant_id == tenant_id,
                    active_admin(self._source_issuer),
                    users.c.id != target.id,
                )
                .limit(1)
            )
        ).first()
        return other_admin is None

    async def _user_name_taken(
        self,
        db: "AsyncSession",
        *,
        tenant_id: str,
        issuer: str,
        user_name: str,
        owner_id: uuid.UUID | None,
    ) -> bool:
        """Whether another account of *issuer* carries *user_name*.

        Compared without regard to case, like the unique index. A tombstone
        carries none: deprovisioning gives it up.
        """
        condition = [
            users.c.tenant_id == tenant_id,
            users.c.issuer == issuer,
            func.lower(users.c.scim_user_name) == func.lower(user_name),
        ]
        if owner_id is not None:
            condition.append(users.c.id != owner_id)
        return (
            await db.execute(select(users.c.id).where(*condition).limit(1))
        ).first() is not None

    @staticmethod
    async def _provisioned_user(
        db: "AsyncSession", *, tenant_id: str, user_id: uuid.UUID
    ) -> "MirroredUser":
        from inqtrix.auth.directory import MirroredUser

        row = (
            await db.execute(
                select(users).where(
                    users.c.tenant_id == tenant_id, users.c.id == user_id
                )
            )
        ).one()
        return PostgresUserDirectory._to_user(row, MirroredUser)

    async def provision_external_user(
        self,
        *,
        tenant_id: str,
        subject: str,
        profile: ExternalProfile,
    ) -> ProvisioningOutcome:
        """Create an account for provisioning, or adopt an unbound one.

        A new account has no session and no sign-in yet, and its e-mail
        stays unconfirmed until a sign-in brings the identity provider's
        word. An account the source already holds under *subject* is
        adopted when provisioning never bound it; a bound account, a
        tombstone or an account being erased is a conflict, and so is a
        user name another account of the source carries.
        """
        issuer = provisioning_issuer(self._source_issuer)
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            existing = (
                await db.execute(
                    select(users)
                    .where(
                        users.c.tenant_id == tenant_id,
                        users.c.issuer == issuer,
                        users.c.subject == subject,
                    )
                    .with_for_update()
                )
            ).first()
            if existing is not None and (
                existing.scim_user_name is not None
                or existing.scim_deleted_at is not None
                or existing.erasure_operation_id is not None
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            if await self._user_name_taken(
                db,
                tenant_id=tenant_id,
                issuer=issuer,
                user_name=profile.user_name,
                owner_id=existing.id if existing is not None else None,
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            scim_name = dict(profile.name) if profile.name else None
            if existing is None:
                user_id = uuid.uuid4()
                now = time.time()
                await db.execute(
                    insert(users).values(
                        id=user_id,
                        tenant_id=tenant_id,
                        issuer=issuer,
                        subject=subject,
                        email=profile.email,
                        email_verified=False,
                        display_name=profile.display_name,
                        scim_user_name=profile.user_name,
                        scim_name=scim_name,
                        disabled_at=(
                            None
                            if profile.active
                            else dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
                        ),
                        disabled_source=None if profile.active else "provisioning",
                    )
                )
                await append_audit_row(
                    db,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.provisioned",
                    resource_type="user",
                    resource_id=str(user_id),
                    detail={"fields": scim_fields(profile)},
                )
                await self._wake_admins(db, tenant_id=tenant_id, user_id=user_id)
                return ProvisioningOutcome(
                    UserLifecycleStatus.UPDATED,
                    await self._provisioned_user(
                        db, tenant_id=tenant_id, user_id=user_id
                    ),
                )
            disable = not profile.active and existing.disabled_at is None
            if disable and await self._is_last_admin_of_source(
                db, tenant_id=tenant_id, target=existing
            ):
                return ProvisioningOutcome(UserLifecycleStatus.LAST_ADMIN)
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == existing.id)
                .values(scim_user_name=profile.user_name, scim_name=scim_name)
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                action="user.scim_adopted",
                resource_type="user",
                resource_id=str(existing.id),
            )
            if profile.active == (existing.disabled_at is None):
                await self._wake_admins(db, tenant_id=tenant_id, user_id=existing.id)
            if profile.active != (existing.disabled_at is None):
                await self._apply_disable(
                    db,
                    tenant_id=tenant_id,
                    target=existing,
                    disabled_at=None if profile.active else time.time(),
                    actor=actor,
                )
            return ProvisioningOutcome(
                UserLifecycleStatus.UPDATED,
                await self._provisioned_user(
                    db, tenant_id=tenant_id, user_id=existing.id
                ),
                adopted=True,
            )

    async def update_external_profile(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        change: ProfileChange,
    ) -> ProvisioningOutcome:
        """Apply a provisioning change to an account of the source.

        *change* maps the account's current profile to the new one under
        the tenant lock, so a change and a concurrent sign-in never overwrite
        each other. A new e-mail is unconfirmed; ``active`` follows the
        disable rules of :meth:`set_disabled`; roles never change here.
        """
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_actor_and_target(
                db, tenant_id=tenant_id, actor=actor, target_user_id=user_id
            )
            target = locked.get(user_id)
            if (
                target is None
                or target.scim_deleted_at is not None
                or target.erasure_operation_id is not None
            ):
                return ProvisioningOutcome(UserLifecycleStatus.NOT_FOUND)
            before = external_profile(
                await self._provisioned_user(db, tenant_id=tenant_id, user_id=user_id)
            )
            after = change(before)
            if after.user_name != before.user_name and await self._user_name_taken(
                db,
                tenant_id=tenant_id,
                issuer=target.issuer,
                user_name=after.user_name,
                owner_id=user_id,
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            if (
                before.active
                and not after.active
                and await self._is_last_admin_of_source(
                    db, tenant_id=tenant_id, target=target
                )
            ):
                return ProvisioningOutcome(UserLifecycleStatus.LAST_ADMIN)
            fields = changed_scim_fields(before, after)
            if fields:
                values: dict[str, Any] = {
                    # An empty user name leaves the account unbound.
                    "scim_user_name": after.user_name or None,
                    "display_name": after.display_name,
                    "scim_name": dict(after.name) if after.name else None,
                    "email": after.email,
                }
                if after.email != before.email:
                    values["email_verified"] = False
                await db.execute(
                    update(users)
                    .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                    .values(**values)
                )
                await append_audit_row(
                    db,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.scim_updated",
                    resource_type="user",
                    resource_id=str(user_id),
                    detail={"fields": ",".join(fields)},
                )
                if after.active == before.active:
                    await self._wake_admins(db, tenant_id=tenant_id, user_id=user_id)
            if after.active != before.active:
                await self._apply_disable(
                    db,
                    tenant_id=tenant_id,
                    target=target,
                    disabled_at=None if after.active else time.time(),
                    actor=actor,
                )
            return ProvisioningOutcome(
                UserLifecycleStatus.UPDATED,
                await self._provisioned_user(db, tenant_id=tenant_id, user_id=user_id),
            )

    async def deprovision(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Disable an account for provisioning and leave a tombstone.

        One transaction: the disable from provisioning with the complete
        cut-off (an administrator's disable keeps its source), then the
        tombstone that neither a sign-in nor provisioning brings back. The
        tombstone gives up its user name, so a new person of the source can
        take it; an administrator who lifts the tombstone gets the account
        back unbound, until provisioning binds it again.
        """
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_actor_and_target(
                db, tenant_id=tenant_id, actor=actor, target_user_id=user_id
            )
            target = locked.get(user_id)
            if (
                target is None
                or target.scim_deleted_at is not None
                or target.erasure_operation_id is not None
            ):
                return UserLifecycleStatus.NOT_FOUND
            now = time.time()
            if target.disabled_at is None:
                status = await self._apply_disable(
                    db,
                    tenant_id=tenant_id,
                    target=target,
                    disabled_at=now,
                    actor=actor,
                )
                if status is UserLifecycleStatus.LAST_ADMIN:
                    return status
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                .values(
                    scim_deleted_at=dt.datetime.fromtimestamp(
                        now, tz=dt.timezone.utc
                    ),
                    scim_user_name=None,
                )
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                action="user.deprovisioned",
                resource_type="user",
                resource_id=str(user_id),
            )
            if target.disabled_at is not None:
                # The disable above woke the administrators otherwise.
                await self._wake_admins(db, tenant_id=tenant_id, user_id=user_id)
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
        actor: LifecycleActor,
        operation_id: str,
        hourly_limit: int | None = None,
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
        function; when provisioning was refused, the audit keeps them, since
        nobody sees its answer. With *hourly_limit*, provisioning starts at
        most that many erasures an hour (counted under the tenant lock, so
        exactly, also across processes); beyond it, ``LIMITED`` changes
        nothing.
        """
        from inqtrix.services.user_erasure_inventory import erasure_blockers

        async with self._scope(tenant_id) as db:
            await lock_tenant_security(db, tenant_id)
            locked = await self._lock_actor_and_target(
                db,
                tenant_id=tenant_id,
                actor=actor,
                target_user_id=user_id,
            )
            target = locked.get(user_id)
            if target is None:
                return UserLifecycleStatus.NOT_FOUND, {}
            if isinstance(actor, ProvisioningActor) and target.scim_deleted_at is None:
                # Provisioning erases only what it deprovisioned: an account an
                # administrator restored in between is no longer its to erase.
                return UserLifecycleStatus.NOT_FOUND, {}
            actor_user_id, actor_type = audit_actor(actor)
            if target.erasure_operation_id is not None:
                return UserLifecycleStatus.UPDATED, {
                    "operation_id": target.erasure_operation_id
                }
            if await self._is_last_admin_of_source(
                db, tenant_id=tenant_id, target=target
            ):
                return UserLifecycleStatus.LAST_ADMIN, {}
            if hourly_limit is not None and (
                await self._erasures_started_last_hour(
                    db, tenant_id=tenant_id, actor_type=actor_type
                )
                >= hourly_limit
            ):
                return UserLifecycleStatus.LIMITED, {}
            blockers = await erasure_blockers(db, user_id=user_id)
            if blockers:
                if isinstance(actor, ProvisioningActor):
                    await append_audit_row(
                        db,
                        tenant_id=tenant_id,
                        actor_user_id=actor_user_id,
                        actor_type=actor_type,
                        action="user.erasure_refused",
                        resource_type="user",
                        resource_id=str(user_id),
                        detail={"reasons": list(blockers)},
                    )
                    await self._wake_admins(db, tenant_id=tenant_id, user_id=user_id)
                return UserLifecycleStatus.REFUSED, {"refused": list(blockers)}
            now = time.time()
            await self._cut_off(db, tenant_id=tenant_id, user_id=user_id, now=now)
            await db.execute(
                update(users)
                .where(users.c.tenant_id == tenant_id, users.c.id == user_id)
                .values(
                    disabled_at=None,
                    disabled_source=None,
                    erasure_operation_id=operation_id,
                    # The identity system may give the name to someone new
                    # while the erasure runs.
                    scim_user_name=None,
                )
            )
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
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

    @staticmethod
    async def _erasures_started_last_hour(
        db: "AsyncSession", *, tenant_id: str, actor_type: str
    ) -> int:
        """Erasure starts of *actor_type* in the tenant within the last hour."""
        return (
            await db.execute(
                select(func.count())
                .select_from(audit_log)
                .where(
                    audit_log.c.tenant_id == tenant_id,
                    audit_log.c.action == "user.erasure_started",
                    audit_log.c.actor_type == actor_type,
                    audit_log.c.occurred_at
                    > func.now() - dt.timedelta(hours=1),
                )
            )
        ).scalar_one()

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
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Remove the ``users`` row once nothing else names the account.

        The last step of an erasure, under the tenant's security lock like
        every other identity change and as short as one row. The audit row
        is written BEFORE the user row goes: its actor is the one who
        started the erasure (an administrator, or provisioning), and the
        erased id is the resource it names. ``NOT_FOUND`` means an earlier
        attempt already finished.
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
            actor_user_id, actor_type = audit_actor(actor)
            await append_audit_row(
                db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
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
