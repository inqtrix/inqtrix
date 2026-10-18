"""Postgres backends for OIDC sessions, login flows, and the user mirror.

Same conventions as the other storage backends: every operation runs
inside :func:`~inqtrix.storage.db.tenant_session` (restricted role +
tenant GUC, forced RLS as the second defense layer). Used from the
HTTP server's event loop only — these stores share the identity
bundle's engine, never the run store's background-loop engine
(asyncpg pools are loop-affine).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.sessions import AuthSession, LoginFlow
from inqtrix.storage.authorization_generation import (
    bump_authorization_generation,
)
from inqtrix.storage.auth_orm import auth_flows, auth_sessions
from inqtrix.storage.db import tenant_session
from inqtrix.storage.identity_orm import active_admin, tenant_security_state, users

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from inqtrix.auth.directory import MirroredUser

DEFAULT_TENANT = "default"


async def insert_auth_session(
    db: "AsyncSession", *, tenant_id: str, session: AuthSession
) -> None:
    """Insert one browser session inside an existing transaction."""
    await db.execute(
        delete(auth_sessions).where(auth_sessions.c.expires_at <= time.time())
    )
    await db.execute(
        auth_sessions.insert().values(
            id=session.id,
            tenant_id=tenant_id,
            user_id=session.user_id,
            issuer=session.issuer,
            subject=session.subject,
            email=session.email,
            display_name=session.display_name,
            groups=list(session.groups),
            csrf_random=session.csrf_random,
            created_at=session.created_at,
            expires_at=session.expires_at,
        )
    )


async def lock_tenant_security(db: "AsyncSession", tenant_id: str) -> None:
    """Lock the stable tenant row used by first/last-admin commands."""
    await db.execute(
        pg_insert(tenant_security_state)
        .values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=[tenant_security_state.c.tenant_id])
    )
    await db.execute(
        select(tenant_security_state.c.tenant_id)
        .where(tenant_security_state.c.tenant_id == tenant_id)
        .with_for_update()
    )


class PostgresSessionStore:
    """Durable session store (login survives restarts and replicas).

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

    def _scope(self):
        return tenant_session(
            self._session_factory,
            tenant_id=DEFAULT_TENANT,
            app_role=self._app_role,
        )

    async def create(self, session: AuthSession) -> None:
        """Persist one session and lazily evict expired rows."""
        async with self._scope() as db:
            await insert_auth_session(
                db, tenant_id=DEFAULT_TENANT, session=session
            )

    async def get(self, session_id: str) -> AuthSession | None:
        """Return the live session or ``None`` (absent or expired)."""
        async with self._scope() as db:
            row = (
                await db.execute(
                    select(auth_sessions).where(
                        auth_sessions.c.id == session_id
                    )
                )
            ).mappings().first()
        if row is None or row["expires_at"] <= time.time():
            return None
        return AuthSession(
            id=row["id"],
            user_id=row["user_id"],
            issuer=row["issuer"],
            subject=row["subject"],
            email=row["email"],
            display_name=row["display_name"],
            groups=tuple(row["groups"] or []),
            csrf_random=row["csrf_random"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def delete(self, session_id: str) -> None:
        """Remove one session; missing ids are a no-op.

        The generation bump rides the same transaction: a logout of ONE
        session must drop that session's live streams within a frame,
        not only at the gate's time ceiling.
        """
        async with self._scope() as db:
            row = (
                await db.execute(
                    delete(auth_sessions)
                    .where(auth_sessions.c.id == session_id)
                    .returning(auth_sessions.c.user_id)
                )
            ).one_or_none()
            if row is not None and row.user_id is not None:
                await bump_authorization_generation(
                    db,
                    tenant_id=DEFAULT_TENANT,
                    target_user_ids=(row.user_id,),
                )

    async def delete_for_user(self, *, user_id: uuid.UUID) -> int:
        """Purge every session of one identity (admin disable cut-off)."""
        async with self._scope() as db:
            result = await db.execute(
                delete(auth_sessions).where(
                    auth_sessions.c.user_id == user_id,
                )
            )
            if int(result.rowcount or 0) > 0:
                await bump_authorization_generation(
                    db,
                    tenant_id=DEFAULT_TENANT,
                    target_user_ids=(user_id,),
                )
        return int(result.rowcount or 0)


class PostgresFlowStore:
    """Durable flow store — logins survive replica switches mid-flow."""

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

    async def put(self, flow: LoginFlow) -> None:
        """Persist one flow and lazily evict expired rows."""
        async with self._scope() as db:
            await db.execute(
                delete(auth_flows).where(
                    auth_flows.c.expires_at <= time.time()
                )
            )
            await db.execute(
                auth_flows.insert().values(
                    state=flow.state,
                    tenant_id=DEFAULT_TENANT,
                    code_verifier=flow.code_verifier,
                    nonce=flow.nonce,
                    next_path=flow.next_path,
                    expires_at=flow.expires_at,
                )
            )

    async def consume(self, state: str) -> LoginFlow | None:
        """One-time take via a guarded flip — replays lose atomically."""
        async with self._scope() as db:
            row = (
                await db.execute(
                    update(auth_flows)
                    .where(
                        auth_flows.c.state == state,
                        auth_flows.c.consumed.is_(False),
                        auth_flows.c.expires_at > time.time(),
                    )
                    .values(consumed=True)
                    .returning(
                        auth_flows.c.code_verifier,
                        auth_flows.c.nonce,
                        auth_flows.c.next_path,
                        auth_flows.c.expires_at,
                    )
                )
            ).first()
        if row is None:
            return None
        return LoginFlow(
            state=state,
            code_verifier=row[0],
            nonce=row[1],
            next_path=row[2],
            expires_at=row[3],
        )


class PostgresUserDirectory:
    """JIT user mirror keyed on ``(issuer, subject)``."""

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role

    @staticmethod
    async def _lock_tenant_security(db: "AsyncSession", tenant_id: str) -> None:
        """Serialize first/last-admin commands on one stable tenant row."""
        await lock_tenant_security(db, tenant_id)

    async def record_login(
        self,
        *,
        tenant_id: str,
        issuer: str,
        subject: str,
        email: str,
        email_verified: bool,
        display_name: str | None,
        canonical_user_id: uuid.UUID | None = None,
    ) -> "MirroredUser":
        """Upsert and return the mirror row for an external login binding."""
        from inqtrix.auth.directory import MirroredUser

        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            statement = pg_insert(users).values(
                **({"id": canonical_user_id} if canonical_user_id is not None else {}),
                tenant_id=tenant_id,
                issuer=issuer,
                subject=subject,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                # Set on the initial INSERT too (not only the re-login
                # UPDATE below) so a first-ever login records its timestamp,
                # matching the memory backend.
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
        return self._to_user(row, MirroredUser)

    @staticmethod
    def _to_user(row, mirrored_user_type):
        """Map one SQLAlchemy user row without duplicating profile semantics."""
        return mirrored_user_type(
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
            erasure_operation_id=row.erasure_operation_id,
        )

    async def find_user(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> "MirroredUser | None":
        """Admission lookup (:class:`~inqtrix.auth.directory.UserLookup`)."""
        from inqtrix.auth.directory import MirroredUser

        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            row = (
                await db.execute(
                    select(users).where(
                        users.c.issuer == issuer,
                        users.c.subject == subject,
                    )
                )
            ).first()
        if row is None:
            return None
        return self._to_user(row, MirroredUser)

    async def find_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> "MirroredUser | None":
        """Lookup by canonical UUID for every authorization path."""
        from inqtrix.auth.directory import MirroredUser

        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            row = (
                await db.execute(select(users).where(users.c.id == user_id))
            ).first()
        return self._to_user(row, MirroredUser) if row is not None else None

    async def profiles_for_user_ids(
        self, *, tenant_id: str, user_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, "MirroredUser"]:
        """``sub -> profile`` for share-listing enrichment (one query)."""
        from inqtrix.auth.directory import MirroredUser

        if not user_ids:
            return {}
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            rows = (
                await db.execute(
                    select(users).where(
                        users.c.tenant_id == tenant_id,
                        users.c.id.in_(list(user_ids)),
                    )
                )
            ).all()
        return {
            row.id: MirroredUser(
                user_id=row.id,
                tenant_id=row.tenant_id,
                issuer=row.issuer,
                subject=row.subject,
                email=row.email,
                email_verified=row.email_verified,
                display_name=row.display_name,
                disabled_at=(
                    row.disabled_at.timestamp()
                    if row.disabled_at is not None
                    else None
                ),
            )
            for row in rows
        }

    async def has_user_id(self, *, tenant_id: str, user_id: uuid.UUID) -> bool:
        """Active mirrored identity with this canonical UUID exists."""
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            row = (
                await db.execute(
                    select(users.c.id)
                    .where(
                        users.c.tenant_id == tenant_id,
                        users.c.id == user_id,
                        users.c.disabled_at.is_(None),
                    )
                    .limit(1)
                )
            ).first()
        return row is not None

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 10,
        exclude_user_id: uuid.UUID | None = None,
    ) -> tuple["MirroredUser", ...]:
        """Prefix search over email and display name (share typeahead)."""
        from inqtrix.auth.directory import MirroredUser

        needle = query.strip()
        if not needle:
            return ()
        pattern = needle.replace("%", "\\%").replace("_", "\\_") + "%"
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            rows = (
                await db.execute(
                    select(users)
                    .where(
                        users.c.tenant_id == tenant_id,
                        users.c.disabled_at.is_(None),
                        users.c.id != exclude_user_id,
                        or_(
                            users.c.email.ilike(pattern),
                            users.c.display_name.ilike(pattern),
                        ),
                    )
                    .order_by(users.c.email)
                    .limit(limit)
                )
            ).all()
        return tuple(
            MirroredUser(
                user_id=row.id,
                tenant_id=row.tenant_id,
                issuer=row.issuer,
                subject=row.subject,
                email=row.email,
                email_verified=row.email_verified,
                display_name=row.display_name,
                disabled_at=None,
            )
            for row in rows
        )

    async def set_instance_role(
        self, *, tenant_id: str, user_id: uuid.UUID, role: str
    ) -> bool:
        """Set the instance role; ``True`` when a row changed."""
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            result = await db.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(instance_role=role)
            )
        return bool(result.rowcount)

    async def resolve_default_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate: str
    ) -> str | None:
        """Return the user's project namespace, claiming ``candidate`` if unset.

        The ``default_workspace_id IS NULL`` predicate makes the claim a single
        set-only-once write under the row lock, so two concurrent first-logins
        converge on one namespace; the re-read in the same transaction then
        returns the canonical value (the just-claimed candidate, or one an
        earlier login already set). ``None`` only when the user row is absent
        (an un-mirrored identity)."""
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            await db.execute(
                update(users)
                .where(
                    users.c.id == user_id,
                    users.c.default_workspace_id.is_(None),
                )
                .values(default_workspace_id=candidate)
            )
            row = (
                await db.execute(
                    select(users.c.default_workspace_id).where(
                        users.c.id == user_id,
                    )
                )
            ).first()
        return row.default_workspace_id if row is not None else None

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
    ) -> bool:
        """Set/clear the mirror disable flag; ``True`` when a row changed.

        The full cut-off cascade (session purge + PAT revoke) is
        orchestrated by the admin router; this is the durable mirror flag
        that denies oidc re-admission. ``None`` re-enables.
        """
        import datetime as dt
        from inqtrix.storage.credentials_orm import local_credentials

        value = (
            dt.datetime.fromtimestamp(disabled_at, tz=dt.timezone.utc)
            if disabled_at is not None
            else None
        )
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            result = await db.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(disabled_at=value)
            )
            await db.execute(
                update(local_credentials)
                .where(local_credentials.c.user_id == user_id)
                .values(disabled_at=disabled_at)
            )
        return bool(result.rowcount)

    async def list_users(
        self, *, tenant_id: str
    ) -> tuple["MirroredUser", ...]:
        """All mirrored identities in the tenant (admin listing).

        The deleted-account placeholder is an implementation detail of
        erasure, not a person: it never appears here.
        """
        from inqtrix.auth.directory import MirroredUser
        from inqtrix.services.user_erasure import DELETED_ACCOUNT_ISSUER

        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            rows = (
                await db.execute(
                    select(users)
                    .where(
                        users.c.tenant_id == tenant_id,
                        users.c.issuer != DELETED_ACCOUNT_ISSUER,
                    )
                    .order_by(users.c.email)
                )
            ).all()
        return tuple(self._to_user(row, MirroredUser) for row in rows)

    async def count_admins(self, *, tenant_id: str) -> int:
        """Active instance-admins (the last-admin guard reads this)."""
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            result = await db.execute(
                select(func.count())
                .select_from(users)
                .where(
                    users.c.tenant_id == tenant_id,
                    active_admin(),
                )
            )
        return int(result.scalar_one())

    async def promote_if_no_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Promote this user to admin IFF no active admin exists yet.

        The first-login-owner bootstrap as ONE guarded statement: the
        ``NOT EXISTS`` is evaluated inside the UPDATE, so concurrent first
        logins across replicas promote exactly one user.
        """
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            await self._lock_tenant_security(db, tenant_id)
            has_admin = (
                await db.execute(
                    select(users.c.id)
                    .where(
                        users.c.tenant_id == tenant_id,
                        active_admin(),
                    )
                    .limit(1)
                )
            ).first()
            if has_admin is not None:
                return False
            result = await db.execute(
                update(users)
                .where(
                    users.c.id == user_id,
                    users.c.disabled_at.is_(None),
                )
                .values(instance_role="admin")
            )
        return bool(result.rowcount)

    async def demote_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Demote to ``user`` unless this is the last active admin (atomic)."""
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            await self._lock_tenant_security(db, tenant_id)
            target = (
                await db.execute(select(users).where(users.c.id == user_id))
            ).first()
            if target is None:
                return False
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
                    return False
            result = await db.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(instance_role="user")
            )
        return bool(result.rowcount)

    async def disable_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float
    ) -> bool:
        """Set the disable flag unless this is the last active admin (atomic)."""
        import datetime as dt

        from inqtrix.storage.credentials_orm import local_credentials
        from inqtrix.storage.pat_orm import personal_access_tokens as pats

        value = dt.datetime.fromtimestamp(disabled_at, tz=dt.timezone.utc)
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as db:
            await self._lock_tenant_security(db, tenant_id)
            target = (
                await db.execute(select(users).where(users.c.id == user_id))
            ).first()
            if target is None:
                return False
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
                    return False
            result = await db.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(disabled_at=value)
            )
            await db.execute(
                delete(auth_sessions).where(auth_sessions.c.user_id == user_id)
            )
            await db.execute(
                update(pats)
                .where(
                    pats.c.owner_user_id == user_id,
                    pats.c.revoked_at.is_(None),
                )
                .values(revoked_at=disabled_at)
            )
            await db.execute(
                update(local_credentials)
                .where(local_credentials.c.user_id == user_id)
                .values(disabled_at=disabled_at)
            )
        return bool(result.rowcount)
