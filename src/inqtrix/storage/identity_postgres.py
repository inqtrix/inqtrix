"""Postgres-backed identity repositories and audit sink.

One backend object implements every read port the
:class:`~inqtrix.auth.permissions.AuthorizationService` consumes plus the
audit sink, mirroring the shape of
:class:`~inqtrix.auth.identity_memory.MemoryIdentityStore` so the
service is wired identically in both modes.

Every operation runs inside :func:`~inqtrix.storage.db.tenant_session`
— one transaction, restricted application role, transaction-local
tenant GUC. The explicit ``tenant_id`` predicates in the queries are
layer 1; row-level security underneath is layer 2 catching the bugs.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import and_, delete, func, insert, select, text, union, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from inqtrix.auth.permissions import (
    AuditEntry,
    LastWorkspaceOwnerError,
    SharePermission,
    WorkspaceRole,
)
from inqtrix.storage.authorization_generation import (
    read_authorization_generation,
)
from inqtrix.storage.db import tenant_session
from inqtrix.storage.editor_orm import editor_documents
from inqtrix.storage.identity_orm import (
    audit_log,
    resource_shares,
    workspace_members,
    workspaces,
)
from inqtrix.storage.knowledge_orm import knowledge_collections
from inqtrix.storage.prompt_template_orm import prompt_templates
from inqtrix.storage.runs_orm import runs
from inqtrix.storage.resource_access import (
    append_audit_row,
    lock_active_users,
    lock_workspace_memberships,
)
from inqtrix.storage.skill_orm import skill_templates
from inqtrix.storage.user_events_postgres import (
    append_instance_admin_invalidations,
    append_user_invalidation,
)

if TYPE_CHECKING:
    from inqtrix.auth.shares import ShareRecord


_SHAREABLE_RESOURCE_OWNER_SOURCES = {
    "run": (
        runs,
        runs.c.run_id,
        runs.c.created_by_user_id,
        (),
    ),
    "knowledge_collection": (
        knowledge_collections,
        knowledge_collections.c.id,
        knowledge_collections.c.created_by_user_id,
        (),
    ),
    "prompt_template": (
        prompt_templates,
        prompt_templates.c.id,
        prompt_templates.c.owner_user_id,
        (),
    ),
    "skill_template": (
        skill_templates,
        skill_templates.c.id,
        skill_templates.c.owner_user_id,
        (),
    ),
    "editor_document": (
        editor_documents,
        editor_documents.c.id,
        editor_documents.c.created_by_user_id,
        (editor_documents.c.deleted_at.is_(None),),
    ),
}


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    """Parse a client-supplied id against the UUID column type.

    A malformed id can never match a row, so it maps to ``None`` —
    the same answer a non-member gets. Letting the string reach
    asyncpg instead would raise a DataError, turning "hidden" into a
    distinguishable 500 (an existence oracle).
    """
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


class PostgresIdentityBackend:
    """Identity read ports and audit sink over the identity schema.

    Args:
        session_factory: Factory from
            :func:`inqtrix.storage.db.build_session_factory`.
        app_role: Restricted Postgres role for
            :func:`~inqtrix.storage.db.tenant_session` (see
            ``StorageSettings.app_role``).
    """

    atomic_share_effects = True
    atomic_workspace_effects = True

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        app_role: str,
        restrict_to_workspace_members: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role
        self._restrict_to_workspace_members = restrict_to_workspace_members

    def _session(
        self, tenant_id: str
    ) -> AbstractAsyncContextManager["AsyncSession"]:
        """One tenant transaction with this backend's app role bound."""
        return tenant_session(
            self._session_factory, tenant_id=tenant_id, app_role=self._app_role
        )

    async def authorization_generation(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> int | None:
        """The user's commit-ordered authorization generation (frame hint)."""
        async with self._session(tenant_id) as session:
            return await read_authorization_generation(
                session, tenant_id=tenant_id, user_id=user_id
            )

    async def _append_share_effects(
        self,
        session: AsyncSession,
        *,
        row,
        owner_user_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        action: str,
        detail: dict[str, str] | None = None,
    ) -> None:
        """Write share audit plus owner/recipient invalidations atomically.

        The audit row goes through ``append_audit_row`` so every share event
        carries the actor pseudonym the admin panel reads and the logs use.
        The invalidation stays targeted rather than reusing
        ``append_resource_effects``: on a revocation the removed recipient is
        no longer among the resource's current shares, so the set-based form
        would leave exactly the person whose access just changed with a stale
        cache.
        """
        await append_audit_row(
            session,
            tenant_id=row.tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            detail=detail,
        )
        targets = {row.recipient_user_id}
        if owner_user_id is not None:
            targets.add(owner_user_id)
        # Sorted like every other invalidation writer: the generation
        # bump inside append_user_invalidation locks one row per target,
        # and a hash-ordered iteration would invert lock order against
        # concurrent sorted writers over the same user pair (deadlock).
        for target_user_id in sorted(targets, key=str):
            await append_user_invalidation(
                session,
                tenant_id=row.tenant_id,
                target_user_id=target_user_id,
                scope="sharing",
                resource_type=row.resource_type,
                resource_id=row.resource_id,
            )

    async def _append_workspace_effects(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        audit_resource_id: str,
        workspace_id: str,
        target_user_ids: Sequence[uuid.UUID],
        detail: dict[str, str] | None = None,
    ) -> None:
        """Write one workspace audit fact and affected-user invalidations."""
        await append_audit_row(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type="workspace",
            resource_id=audit_resource_id,
            detail=detail or {},
        )
        targets = set(target_user_ids)
        if actor_user_id is not None:
            targets.add(actor_user_id)
        await append_instance_admin_invalidations(
            session,
            tenant_id=tenant_id,
            target_user_ids=tuple(targets),
            scope="workspaces",
            resource_type="workspace",
            resource_id=workspace_id,
        )

    async def _shareable_resource_owner(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        lock: bool,
    ) -> uuid.UUID | None:
        """Read the canonical owner, optionally locking the resource row."""
        selected = _SHAREABLE_RESOURCE_OWNER_SOURCES.get(resource_type)
        if selected is None:
            return None
        table, id_column, owner_column, resource_filters = selected
        statement = select(owner_column).where(
            table.c.tenant_id == tenant_id,
            id_column == resource_id,
            *resource_filters,
        )
        if lock:
            statement = statement.with_for_update()
        owner = (await session.execute(statement)).scalar_one_or_none()
        if owner is None:
            return None
        return owner if isinstance(owner, uuid.UUID) else uuid.UUID(str(owner))

    async def _users_share_workspace(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        user_id_a: uuid.UUID,
        user_id_b: uuid.UUID,
    ) -> bool:
        first = workspace_members.alias("share_boundary_first")
        second = workspace_members.alias("share_boundary_second")
        return bool(
            await session.scalar(
                select(1)
                .select_from(
                    first.join(
                        second,
                        first.c.workspace_id == second.c.workspace_id,
                    )
                )
                .where(
                    first.c.tenant_id == tenant_id,
                    second.c.tenant_id == tenant_id,
                    first.c.user_id == user_id_a,
                    second.c.user_id == user_id_b,
                )
                .limit(1)
            )
        )

    async def _reconcile_workspace_shares(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        affected_user_ids: set[uuid.UUID] | None,
    ) -> int:
        """Revoke active shares outside the continuous workspace boundary.

        Resource rows are locked before their share rows in stable order, the
        same order used by edits and explicit revocation. Membership changes
        and the resulting revocations therefore commit as one visible state.
        """
        if not self._restrict_to_workspace_members:
            return 0
        pointer_columns = (
            resource_shares.c.id,
            resource_shares.c.recipient_user_id,
            resource_shares.c.resource_type,
            resource_shares.c.resource_id,
        )
        active_filters = (
            resource_shares.c.tenant_id == tenant_id,
            resource_shares.c.revoked_at.is_(None),
        )
        if affected_user_ids is None:
            pointer_query = select(*pointer_columns).where(*active_filters)
        else:
            affected = tuple(sorted(affected_user_ids, key=str))
            recipient_query = select(*pointer_columns).where(
                *active_filters,
                resource_shares.c.recipient_user_id.in_(affected),
            )
            # Candidate discovery includes tombstoned rows so a stale active
            # share reaches the live-owner check below and is revoked.
            owner_queries = tuple(
                select(*pointer_columns)
                .select_from(
                    resource_shares.join(
                        table,
                        and_(
                            resource_shares.c.tenant_id == table.c.tenant_id,
                            resource_shares.c.resource_type == resource_type,
                            resource_shares.c.resource_id == id_column,
                        ),
                    )
                )
                .where(
                    *active_filters,
                    owner_column.in_(affected),
                )
                for resource_type, (
                    table,
                    id_column,
                    owner_column,
                    _resource_filters,
                ) in _SHAREABLE_RESOURCE_OWNER_SOURCES.items()
            )
            candidates = union(recipient_query, *owner_queries).subquery(
                "workspace_share_candidates"
            )
            pointer_query = select(
                candidates.c.id,
                candidates.c.recipient_user_id,
                candidates.c.resource_type,
                candidates.c.resource_id,
            )
        pointers = (
            await session.execute(
                pointer_query.order_by(
                    pointer_query.selected_columns.resource_type,
                    pointer_query.selected_columns.resource_id,
                    pointer_query.selected_columns.id,
                )
            )
        ).all()
        revoked = 0
        for pointer in pointers:
            owner_user_id = await self._shareable_resource_owner(
                session,
                tenant_id=tenant_id,
                resource_type=pointer.resource_type,
                resource_id=pointer.resource_id,
                lock=True,
            )
            if (
                affected_user_ids is not None
                and pointer.recipient_user_id not in affected_user_ids
                and owner_user_id is not None
                and owner_user_id not in affected_user_ids
            ):
                continue
            if owner_user_id is not None and await self._users_share_workspace(
                session,
                tenant_id=tenant_id,
                user_id_a=owner_user_id,
                user_id_b=pointer.recipient_user_id,
            ):
                continue
            locked = (
                await session.execute(
                    select(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == pointer.id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None:
                continue
            if (
                locked.recipient_user_id != pointer.recipient_user_id
                or locked.resource_type != pointer.resource_type
                or locked.resource_id != pointer.resource_id
            ):
                continue
            await session.execute(
                update(resource_shares)
                .where(resource_shares.c.id == locked.id)
                .values(
                    revoked_at=func.now(),
                    revoked_by_user_id=actor_user_id,
                )
            )
            await self._append_share_effects(
                session,
                row=locked,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                action="share.workspace_boundary_revoked",
                detail={
                    "recipient_user_id": str(locked.recipient_user_id)
                },
            )
            revoked += 1
        return revoked

    async def reconcile_workspace_shares(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Reconcile every direct share before the API becomes ready."""
        async with self._session(tenant_id) as session:
            return await self._reconcile_workspace_shares(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                affected_user_ids=None,
            )

    # ------------------------------------------------------------- #
    # MembershipRepository
    # ------------------------------------------------------------- #

    async def workspace_ids_for(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[str, ...]:
        """All workspace ids the canonical user belongs to in the tenant."""
        async with self._session(tenant_id) as session:
            rows = await session.execute(
                select(workspace_members.c.workspace_id).where(
                    workspace_members.c.tenant_id == tenant_id,
                    workspace_members.c.user_id == user_id,
                )
            )
            return tuple(str(workspace_id) for (workspace_id,) in rows)

    async def role_in_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, workspace_id: str
    ) -> WorkspaceRole | None:
        """The member's role, or ``None`` for non-members, unknown
        workspaces, and malformed workspace ids alike (existence and
        id validity stay hidden)."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            row = await session.execute(
                select(workspace_members.c.role).where(
                    workspace_members.c.tenant_id == tenant_id,
                    workspace_members.c.user_id == user_id,
                    workspace_members.c.workspace_id == workspace_uuid,
                )
            )
            value = row.scalar_one_or_none()
            return WorkspaceRole(value) if value is not None else None

    # ------------------------------------------------------------- #
    # ShareAdminRepository (write/listing surface)
    # ------------------------------------------------------------- #

    async def create_shares(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_user_id: uuid.UUID,
        granted_by_user_id: uuid.UUID,
        invitees: Sequence[tuple[uuid.UUID, SharePermission]],
        restrict_to_members: bool = False,
    ) -> tuple["ShareRecord", ...]:
        """Insert a complete invite batch in one transaction.

        Existing active tuples and concurrent insert races are conflicts; they
        never update permission or carry forward consent. A later re-share
        after revoke therefore receives a new id and starts pending.
        """
        from inqtrix.auth.shares import (
            ShareConflict,
            ShareNotAllowed,
            ShareValidationError,
        )

        recipient_ids = [recipient for recipient, _permission in invitees]
        try:
            async with self._session(tenant_id) as session:
                if granted_by_user_id != owner_user_id:
                    raise ShareNotAllowed()
                all_user_ids = (owner_user_id, *recipient_ids)
                if not await lock_active_users(
                    session,
                    tenant_id=tenant_id,
                    user_ids=all_user_ids,
                ):
                    raise ShareValidationError("User not found or disabled")
                if restrict_to_members:
                    memberships = await lock_workspace_memberships(
                        session,
                        tenant_id=tenant_id,
                        user_ids=all_user_ids,
                    )
                    owner_workspaces = memberships[owner_user_id]
                    if any(
                        not owner_workspaces.intersection(memberships[recipient_id])
                        for recipient_id in recipient_ids
                    ):
                        raise ShareNotAllowed()
                locked_owner_user_id = await self._shareable_resource_owner(
                    session,
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    lock=True,
                )
                if locked_owner_user_id != owner_user_id:
                    raise ShareNotAllowed()
                existing = (
                    await session.execute(
                        select(resource_shares.c.id)
                        .where(
                            resource_shares.c.tenant_id == tenant_id,
                            resource_shares.c.resource_type == resource_type,
                            resource_shares.c.resource_id == resource_id,
                            resource_shares.c.recipient_user_id.in_(recipient_ids),
                            resource_shares.c.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).first()
                if existing is not None:
                    raise ShareConflict("An active share already exists")
                rows = (
                    await session.execute(
                        insert(resource_shares)
                        .values(
                            [
                                {
                                    "id": uuid.uuid4(),
                                    "tenant_id": tenant_id,
                                    "recipient_user_id": recipient_user_id,
                                    "resource_type": resource_type,
                                    "resource_id": resource_id,
                                    "permission": permission.value,
                                    "revision": 1,
                                    "granted_by_user_id": granted_by_user_id,
                                }
                                for recipient_user_id, permission in invitees
                            ]
                        )
                        .returning(resource_shares)
                    )
                ).all()
                for row in rows:
                    await self._append_share_effects(
                        session,
                        row=row,
                        owner_user_id=locked_owner_user_id,
                        actor_user_id=granted_by_user_id,
                        action="share.granted",
                        detail={
                            "recipient_user_id": str(row.recipient_user_id),
                            "permission": str(row.permission),
                        },
                    )
        except IntegrityError as exc:
            raise ShareConflict("An active share already exists") from exc
        return tuple(self._share_record(row) for row in rows)

    async def update_share_permission(
        self,
        *,
        tenant_id: str,
        share_id: str,
        permission: SharePermission,
        expected_revision: int,
        actor_user_id: uuid.UUID,
        restrict_to_members: bool = False,
    ) -> "ShareRecord | None":
        """CAS-update permission, preserving acceptance and share identity."""
        from inqtrix.auth.shares import ShareConflict, ShareNotAllowed

        share_uuid = _as_uuid(share_id)
        if share_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            pointer = (
                await session.execute(
                    select(resource_shares).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).first()
            if pointer is None:
                return None
            if not await lock_active_users(
                session,
                tenant_id=tenant_id,
                user_ids=(actor_user_id, pointer.recipient_user_id),
            ):
                raise ShareNotAllowed()
            if restrict_to_members:
                memberships = await lock_workspace_memberships(
                    session,
                    tenant_id=tenant_id,
                    user_ids=(actor_user_id, pointer.recipient_user_id),
                )
                if not memberships[actor_user_id].intersection(
                    memberships[pointer.recipient_user_id]
                ):
                    raise ShareNotAllowed()
            owner_user_id = await self._shareable_resource_owner(
                session,
                tenant_id=tenant_id,
                resource_type=pointer.resource_type,
                resource_id=pointer.resource_id,
                lock=True,
            )
            if owner_user_id != actor_user_id:
                raise ShareNotAllowed()
            row = (
                await session.execute(
                    update(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                    resource_shares.c.id == share_uuid,
                    resource_shares.c.revoked_at.is_(None),
                    resource_shares.c.revision == expected_revision,
                    )
                    .values(
                        permission=permission.value,
                        revision=resource_shares.c.revision + 1,
                    )
                    .returning(resource_shares)
                )
            ).first()
            if row is not None:
                await self._append_share_effects(
                    session,
                    row=row,
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    action="share.permission_updated",
                    detail={
                        "permission": str(row.permission),
                        "revision": str(row.revision),
                    },
                )
                return self._share_record(row)
            current = (
                await session.execute(
                    select(resource_shares.c.revision).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if current is None:
                return None
            raise ShareConflict(
                "The share has already changed",
                current_revision=int(current),
            )

    async def get_share(
        self, *, tenant_id: str, share_id: str
    ) -> "ShareRecord | None":
        share_uuid = _as_uuid(share_id)
        if share_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(resource_shares).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).first()
        return self._share_record(row) if row is not None else None

    async def revoke_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        revoked_by_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> "ShareRecord | None":
        share_uuid = _as_uuid(share_id)
        if share_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            pointer = (
                await session.execute(
                    select(resource_shares).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).first()
            if pointer is None:
                return None
            if not await lock_active_users(
                session,
                tenant_id=tenant_id,
                user_ids=(revoked_by_user_id,),
            ):
                return None
            locked_owner_user_id = await self._shareable_resource_owner(
                session,
                tenant_id=tenant_id,
                resource_type=pointer.resource_type,
                resource_id=pointer.resource_id,
                lock=True,
            )
            if revoked_by_user_id not in {
                locked_owner_user_id,
                pointer.recipient_user_id,
            }:
                return None
            if locked_owner_user_id != owner_user_id:
                return None
            row = (
                await session.execute(
                    update(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .values(
                        revoked_at=func.now(),
                        revoked_by_user_id=revoked_by_user_id,
                    )
                    .returning(resource_shares)
                )
            ).first()
            if row is not None:
                # Same three cases the memory twin derives, from the same
                # facts: who ended the share, and whether it was ever
                # accepted. A single "removed" action cannot answer the
                # question a rights audit asks.
                action = (
                    "share.declined"
                    if revoked_by_user_id == pointer.recipient_user_id
                    and pointer.accepted_at is None
                    else "share.left"
                    if revoked_by_user_id == pointer.recipient_user_id
                    else "share.revoked"
                )
                await self._append_share_effects(
                    session,
                    row=row,
                    owner_user_id=owner_user_id,
                    actor_user_id=revoked_by_user_id,
                    action=action,
                    detail={"recipient_user_id": str(row.recipient_user_id)},
                )
        return self._share_record(row) if row is not None else None

    async def accept_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        recipient_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        restrict_to_members: bool = False,
    ) -> "ShareRecord | None":
        """Accept pending or return the already accepted active row."""
        share_uuid = _as_uuid(share_id)
        if share_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            pointer = (
                await session.execute(
                    select(resource_shares).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.recipient_user_id == recipient_user_id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).first()
            if pointer is None:
                return None
            resolved_owner_user_id = await self._shareable_resource_owner(
                session,
                tenant_id=tenant_id,
                resource_type=pointer.resource_type,
                resource_id=pointer.resource_id,
                lock=False,
            )
            if resolved_owner_user_id != owner_user_id:
                return None
            if not await lock_active_users(
                session,
                tenant_id=tenant_id,
                user_ids=(owner_user_id, recipient_user_id),
            ):
                return None
            if restrict_to_members:
                memberships = await lock_workspace_memberships(
                    session,
                    tenant_id=tenant_id,
                    user_ids=(owner_user_id, recipient_user_id),
                )
                if not memberships[owner_user_id].intersection(
                    memberships[recipient_user_id]
                ):
                    return None
            locked_owner_user_id = await self._shareable_resource_owner(
                session,
                tenant_id=tenant_id,
                resource_type=pointer.resource_type,
                resource_id=pointer.resource_id,
                lock=True,
            )
            if locked_owner_user_id != owner_user_id:
                return None
            row = (
                await session.execute(
                    select(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.id == share_uuid,
                        resource_shares.c.recipient_user_id == recipient_user_id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return None
            changed = row.accepted_at is None
            if changed:
                row = (
                    await session.execute(
                        update(resource_shares)
                        .where(resource_shares.c.id == share_uuid)
                        .values(accepted_at=func.now())
                        .returning(resource_shares)
                    )
                ).one()
                await self._append_share_effects(
                    session,
                    row=row,
                    owner_user_id=locked_owner_user_id,
                    actor_user_id=recipient_user_id,
                    action="share.accepted",
                )
            return self._share_record(row)

    async def revoke_shares_for_resource(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        revoked_by_user_id: uuid.UUID,
    ) -> int:
        """Soft-revoke every active share on one resource (cleanup)."""
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                update(resource_shares)
                .where(
                    resource_shares.c.tenant_id == tenant_id,
                    resource_shares.c.resource_type == resource_type,
                    resource_shares.c.resource_id == resource_id,
                    resource_shares.c.revoked_at.is_(None),
                )
                .values(
                    revoked_at=func.now(),
                    revoked_by_user_id=revoked_by_user_id,
                )
                .returning(resource_shares)
                )
            ).all()
            for row in rows:
                await self._append_share_effects(
                    session,
                    row=row,
                    owner_user_id=revoked_by_user_id,
                    actor_user_id=revoked_by_user_id,
                    action="share.revoked_for_resource",
                )
        return len(rows)

    async def list_shares_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple["ShareRecord", ...]:
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == resource_type,
                        resource_shares.c.resource_id == resource_id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .order_by(resource_shares.c.created_at)
                )
            ).all()
        return tuple(self._share_record(row) for row in rows)

    async def inbox_for_recipient(
        self, *, tenant_id: str, recipient_user_id: uuid.UUID
    ) -> tuple["ShareRecord", ...]:
        """Active pending and accepted shares addressed to one user.

        The lifecycle inbox keeps pending rows so the recipient can consent
        to them and spans every supported resource kind in one query.
        """
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.revoked_at.is_(None),
                        resource_shares.c.recipient_user_id == recipient_user_id,
                    )
                    .order_by(resource_shares.c.created_at)
                )
            ).all()
        return tuple(self._share_record(row) for row in rows)

    async def list_active_shares(
        self, *, tenant_id: str
    ) -> tuple["ShareRecord", ...]:
        """Active lifecycle rows; ownership is resolved from resources."""
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(resource_shares)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .order_by(resource_shares.c.created_at)
                )
            ).all()
        return tuple(self._share_record(row) for row in rows)

    async def shares_for_recipient(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        recipient_user_id: uuid.UUID,
    ) -> dict[str, "ShareRecord"]:
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(resource_shares).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == resource_type,
                        resource_shares.c.revoked_at.is_(None),
                        resource_shares.c.accepted_at.isnot(None),
                        resource_shares.c.recipient_user_id == recipient_user_id,
                    )
                )
            ).all()
        best: dict[str, "ShareRecord"] = {}
        for row in rows:
            record = self._share_record(row)
            current = best.get(record.resource_id)
            if current is None or record.permission.at_least(
                current.permission
            ):
                best[record.resource_id] = record
        return best

    async def share_counts_for_resources(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_ids: Sequence[str],
    ) -> dict[str, int]:
        if not resource_ids:
            return {}
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(
                        resource_shares.c.resource_id,
                        func.count(resource_shares.c.id),
                    )
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == resource_type,
                        resource_shares.c.resource_id.in_(list(resource_ids)),
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .group_by(resource_shares.c.resource_id)
                )
            ).all()
        return {resource_id: count for resource_id, count in rows}

    @staticmethod
    def _share_record(row) -> "ShareRecord":
        from inqtrix.auth.shares import ShareRecord

        return ShareRecord(
            id=str(row.id),
            tenant_id=row.tenant_id,
            recipient_user_id=row.recipient_user_id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            permission=SharePermission(row.permission),
            revision=int(row.revision),
            granted_by_user_id=row.granted_by_user_id,
            created_at=(
                row.created_at.timestamp()
                if row.created_at is not None
                else 0.0
            ),
            accepted_at=(
                row.accepted_at.timestamp()
                if row.accepted_at is not None
                else None
            ),
            revoked_at=(
                row.revoked_at.timestamp()
                if row.revoked_at is not None
                else None
            ),
            revoked_by_user_id=row.revoked_by_user_id,
        )

    # ------------------------------------------------------------- #
    # Workspace admin (creation + listing)
    # ------------------------------------------------------------- #

    async def create_workspace(
        self, *, tenant_id: str, name: str, created_by_user_id: uuid.UUID
    ) -> tuple[str, str]:
        """Create one workspace with *created_by_user_id* as its OWNER, atomically.

        The instance-admin workspace surface (``/v1/admin/workspaces``) and
        the self-serve ``POST /v1/workspaces`` both land here; the workspace
        row and the creator's OWNER membership are written in one transaction.
        """
        workspace_id = uuid.uuid4()
        async with self._session(tenant_id) as session:
            await session.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id=tenant_id,
                    name=name,
                    created_by_user_id=created_by_user_id,
                )
            )
            await session.execute(
                insert(workspace_members).values(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=created_by_user_id,
                    role=WorkspaceRole.OWNER.value,
                )
            )
            await self._append_workspace_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=created_by_user_id,
                action="workspace.created",
                audit_resource_id=str(workspace_id),
                workspace_id=str(workspace_id),
                target_user_ids=(created_by_user_id,),
                detail={"name": name},
            )
        return str(workspace_id), name

    async def list_workspaces_for(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[tuple[str, str, WorkspaceRole], ...]:
        """``(id, name, role)`` per membership, name-sorted."""
        async with self._session(tenant_id) as session:
            rows = await session.execute(
                select(
                    workspaces.c.id,
                    workspaces.c.name,
                    workspace_members.c.role,
                )
                .join(
                    workspace_members,
                    workspace_members.c.workspace_id == workspaces.c.id,
                )
                .where(
                    workspaces.c.tenant_id == tenant_id,
                    workspace_members.c.user_id == user_id,
                )
                .order_by(workspaces.c.name)
            )
            return tuple(
                (str(workspace_id), name, WorkspaceRole(role))
                for workspace_id, name, role in rows
            )

    # ------------------------------------------------------------- #
    # MembershipAdminRepository (workspace + membership administration)
    # ------------------------------------------------------------- #

    async def list_all_workspaces(
        self, *, tenant_id: str
    ) -> tuple[tuple[str, str, uuid.UUID, int], ...]:
        """``(id, name, created_by_user_id, member_count)`` per tenant workspace.

        Outer-join so a workspace with zero members (every member removed)
        still reports with ``member_count == 0`` rather than vanishing.
        """
        async with self._session(tenant_id) as session:
            rows = await session.execute(
                select(
                    workspaces.c.id,
                    workspaces.c.name,
                    workspaces.c.created_by_user_id,
                    func.count(workspace_members.c.user_id),
                )
                .select_from(
                    workspaces.outerjoin(
                        workspace_members,
                        workspace_members.c.workspace_id == workspaces.c.id,
                    )
                )
                .where(workspaces.c.tenant_id == tenant_id)
                .group_by(
                    workspaces.c.id,
                    workspaces.c.name,
                    workspaces.c.created_by_user_id,
                )
                .order_by(workspaces.c.name)
            )
            return tuple(
                (str(workspace_id), name, created_by_user_id, int(count))
                for workspace_id, name, created_by_user_id, count in rows
            )

    async def rename_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        name: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Rename one workspace; ``False`` for unknown / malformed id."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return False
        async with self._session(tenant_id) as session:
            locked = (
                await session.execute(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.id == workspace_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                return False
            member_user_ids = tuple(
                (
                    await session.execute(
                        select(workspace_members.c.user_id)
                        .where(
                            workspace_members.c.tenant_id == tenant_id,
                            workspace_members.c.workspace_id == workspace_uuid,
                        )
                        .order_by(workspace_members.c.user_id)
                    )
                ).scalars()
            )
            await session.execute(
                update(workspaces)
                .where(workspaces.c.id == workspace_uuid)
                .values(name=name)
            )
            await self._append_workspace_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.renamed",
                audit_resource_id=workspace_id,
                workspace_id=workspace_id,
                target_user_ids=member_user_ids,
                detail={"name": name},
            )
        return True

    async def delete_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Delete a workspace and reconcile its shares atomically."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return False
        async with self._session(tenant_id) as session:
            locked = (
                await session.execute(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.id == workspace_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                return False
            affected_user_ids = set(
                (
                    await session.execute(
                        select(workspace_members.c.user_id)
                        .where(
                            workspace_members.c.tenant_id == tenant_id,
                            workspace_members.c.workspace_id == workspace_uuid,
                        )
                        .order_by(workspace_members.c.user_id)
                        .with_for_update()
                    )
                ).scalars()
            )
            result = await session.execute(
                delete(workspaces).where(
                    workspaces.c.tenant_id == tenant_id,
                    workspaces.c.id == workspace_uuid,
                )
            )
            await self._reconcile_workspace_shares(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                affected_user_ids=affected_user_ids,
            )
            await self._append_workspace_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.deleted",
                audit_resource_id=workspace_id,
                workspace_id=workspace_id,
                target_user_ids=tuple(affected_user_ids),
            )
        return int(result.rowcount or 0) > 0

    async def _workspace_exists(
        self, session: "AsyncSession", *, tenant_id: str, workspace_uuid: uuid.UUID
    ) -> bool:
        row = await session.execute(
            select(workspaces.c.id).where(
                workspaces.c.tenant_id == tenant_id,
                workspaces.c.id == workspace_uuid,
            )
        )
        return row.scalar_one_or_none() is not None

    async def list_members(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[tuple[uuid.UUID, WorkspaceRole], ...] | None:
        """``(user_id, role)`` per member, UUID-sorted, or ``None``."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return None
        async with self._session(tenant_id) as session:
            if not await self._workspace_exists(
                session, tenant_id=tenant_id, workspace_uuid=workspace_uuid
            ):
                return None
            rows = await session.execute(
                select(
                    workspace_members.c.user_id,
                    workspace_members.c.role,
                )
                .where(
                    workspace_members.c.tenant_id == tenant_id,
                    workspace_members.c.workspace_id == workspace_uuid,
                )
                .order_by(workspace_members.c.user_id)
            )
            return tuple((user_id, WorkspaceRole(role)) for user_id, role in rows)

    async def assign_member(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        role: WorkspaceRole,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Upsert one membership at the exact role; ``False`` when absent.

        Existence is checked first so an unknown workspace returns ``False``
        rather than surfacing the foreign-key violation as a 500.
        """
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return False
        async with self._session(tenant_id) as session:
            locked = (
                await session.execute(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.id == workspace_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                return False
            current_role = (
                await session.execute(
                    select(workspace_members.c.role).where(
                        workspace_members.c.tenant_id == tenant_id,
                        workspace_members.c.workspace_id == workspace_uuid,
                        workspace_members.c.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if current_role == WorkspaceRole.OWNER.value and role is not WorkspaceRole.OWNER:
                owner_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(workspace_members)
                        .where(
                            workspace_members.c.tenant_id == tenant_id,
                            workspace_members.c.workspace_id == workspace_uuid,
                            workspace_members.c.role == WorkspaceRole.OWNER.value,
                        )
                    )
                ).scalar_one()
                if int(owner_count) <= 1:
                    raise LastWorkspaceOwnerError(workspace_id)
            await session.execute(
                pg_insert(workspace_members)
                .values(
                    tenant_id=tenant_id,
                    workspace_id=workspace_uuid,
                    user_id=user_id,
                    role=role.value,
                )
                .on_conflict_do_update(
                    index_elements=[
                        workspace_members.c.workspace_id,
                        workspace_members.c.user_id,
                    ],
                    set_={"role": role.value},
                )
            )
            await self._append_workspace_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=(
                    "workspace.member_added"
                    if current_role is None
                    else "workspace.member_role_set"
                ),
                audit_resource_id=f"{workspace_id}:{user_id}",
                workspace_id=workspace_id,
                target_user_ids=(user_id,),
                detail={"role": role.value},
            )
        return True

    async def set_existing_member_role(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        role: WorkspaceRole,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Set an existing member's role with no insert-capable code path."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return False
        async with self._session(tenant_id) as session:
            locked_workspace = (
                await session.execute(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.id == workspace_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_workspace is None:
                return False
            current_role = (
                await session.execute(
                    select(workspace_members.c.role)
                    .where(
                        workspace_members.c.tenant_id == tenant_id,
                        workspace_members.c.workspace_id == workspace_uuid,
                        workspace_members.c.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current_role is None:
                return False
            if (
                current_role == WorkspaceRole.OWNER.value
                and role is not WorkspaceRole.OWNER
            ):
                owner_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(workspace_members)
                        .where(
                            workspace_members.c.tenant_id == tenant_id,
                            workspace_members.c.workspace_id == workspace_uuid,
                            workspace_members.c.role == WorkspaceRole.OWNER.value,
                        )
                    )
                ).scalar_one()
                if int(owner_count) <= 1:
                    raise LastWorkspaceOwnerError(workspace_id)
            result = await session.execute(
                update(workspace_members)
                .where(
                    workspace_members.c.tenant_id == tenant_id,
                    workspace_members.c.workspace_id == workspace_uuid,
                    workspace_members.c.user_id == user_id,
                )
                .values(role=role.value)
            )
            if not result.rowcount:
                return False
            await self._append_workspace_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.member_role_set",
                audit_resource_id=f"{workspace_id}:{user_id}",
                workspace_id=workspace_id,
                target_user_ids=(user_id,),
                detail={"role": role.value},
            )
        return True

    async def remove_member(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Remove one membership; ``False`` when not a member / unknown."""
        workspace_uuid = _as_uuid(workspace_id)
        if workspace_uuid is None:
            return False
        async with self._session(tenant_id) as session:
            locked = (
                await session.execute(
                    select(workspaces.c.id)
                    .where(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.id == workspace_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                return False
            role = (
                await session.execute(
                    select(workspace_members.c.role).where(
                        workspace_members.c.tenant_id == tenant_id,
                        workspace_members.c.workspace_id == workspace_uuid,
                        workspace_members.c.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if role is None:
                return False
            if role == WorkspaceRole.OWNER.value:
                owner_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(workspace_members)
                        .where(
                            workspace_members.c.tenant_id == tenant_id,
                            workspace_members.c.workspace_id == workspace_uuid,
                            workspace_members.c.role == WorkspaceRole.OWNER.value,
                        )
                    )
                ).scalar_one()
                if int(owner_count) <= 1:
                    raise LastWorkspaceOwnerError(workspace_id)
            result = await session.execute(
                delete(workspace_members).where(
                    workspace_members.c.tenant_id == tenant_id,
                    workspace_members.c.workspace_id == workspace_uuid,
                    workspace_members.c.user_id == user_id,
                )
            )
            if result.rowcount:
                await self._reconcile_workspace_shares(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    affected_user_ids={user_id},
                )
                await self._append_workspace_effects(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    action="workspace.member_removed",
                    audit_resource_id=f"{workspace_id}:{user_id}",
                    workspace_id=workspace_id,
                    target_user_ids=(user_id,),
                )
        return int(result.rowcount or 0) > 0

    # ------------------------------------------------------------- #
    # ShareRepository
    # ------------------------------------------------------------- #

    async def permission_for(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> SharePermission | None:
        """The recipient's one active, accepted direct permission."""
        async with self._session(tenant_id) as session:
            value = (
                await session.execute(
                    select(resource_shares.c.permission).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == resource_type,
                        resource_shares.c.resource_id == resource_id,
                        resource_shares.c.revoked_at.is_(None),
                        resource_shares.c.accepted_at.isnot(None),
                        resource_shares.c.recipient_user_id
                        == recipient_user_id,
                    )
                )
            ).scalar_one_or_none()
            return SharePermission(value) if value is not None else None

    # ------------------------------------------------------------- #
    # AuditSink
    # ------------------------------------------------------------- #

    async def list_audit_entries(
        self,
        *,
        tenant_id: str,
        action_prefix: str = "",
        actor_pseudonym: str = "",
        outcome: str = "",
        resource_type: str = "",
        resource_id: str = "",
        occurred_from: float | None = None,
        occurred_to: float | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Newest-first audit page for the admin panel (id-keyset).

        Returns ``(rows, next_before_id)`` — ``next_before_id`` is the
        cursor for the following page or ``None`` at the end. Rows are
        JSON-ready dicts; ``occurred_at`` is epoch seconds.
        """
        conditions = [audit_log.c.tenant_id == tenant_id]
        if action_prefix:
            escaped = (
                action_prefix.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            conditions.append(audit_log.c.action.like(f"{escaped}%"))
        if actor_pseudonym:
            conditions.append(
                audit_log.c.actor_pseudonym == actor_pseudonym
            )
        if outcome:
            conditions.append(audit_log.c.outcome == outcome)
        if resource_type:
            conditions.append(audit_log.c.resource_type == resource_type)
        if resource_id:
            conditions.append(audit_log.c.resource_id == resource_id)
        if occurred_from is not None:
            conditions.append(
                audit_log.c.occurred_at
                >= datetime.fromtimestamp(occurred_from, tz=timezone.utc)
            )
        if occurred_to is not None:
            conditions.append(
                audit_log.c.occurred_at
                <= datetime.fromtimestamp(occurred_to, tz=timezone.utc)
            )
        if before_id is not None:
            conditions.append(audit_log.c.id < before_id)
        page_size = max(1, min(int(limit), 200))
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(audit_log)
                    .where(*conditions)
                    .order_by(audit_log.c.id.desc())
                    .limit(page_size + 1)
                )
            ).mappings().all()
        items = [
            {
                "id": int(row["id"]),
                "occurred_at": row["occurred_at"].timestamp(),
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "actor_pseudonym": row["actor_pseudonym"],
                "actor_type": row["actor_type"],
                "outcome": row["outcome"],
                "workspace_id": (
                    str(row["workspace_id"]) if row["workspace_id"] else None
                ),
                "detail": dict(row["detail"] or {}),
                "origin": dict(row["origin"] or {}),
                "correlation": dict(row["correlation"] or {}),
            }
            for row in rows[:page_size]
        ]
        next_before = (
            items[-1]["id"] if len(rows) > page_size and items else None
        )
        return items, next_before

    async def prune_audit_log(self, *, days: int) -> int:
        """Delete audit rows older than ``days`` via the DEFINER door.

        The app role deliberately holds INSERT/SELECT only on
        ``audit_log``; ``audit_prune`` (migration 0072, SECURITY
        DEFINER) is the one sanctioned deletion path — retention is an
        instance-level policy, and only a row count comes back.

        Tenant scope depends on the FUNCTION OWNER (empirically
        verified): with an RLS-exempt owner (bundled superuser,
        BYPASSRLS migration role) the prune is cross-tenant; under
        ``INQTRIX_MIGRATION_RLS_MODE=owner`` FORCE RLS binds even the
        owner, so the prune only covers the calling session's tenant —
        the ``default`` tenant here, which equals every tenant in the
        current single-tenant deployments. A future multi-tenant rollout
        must revisit this before relying on 365-day retention.
        """
        async with self._session("default") as session:
            result = await session.execute(
                text(
                    "SELECT audit_prune(now() - make_interval(days => "
                    ":days))"
                ),
                {"days": int(days)},
            )
            return int(result.scalar() or 0)

    async def record(self, entry: AuditEntry) -> None:
        """Append one audit fact (INSERT-only grants on the table)."""
        async with self._session(entry.tenant_id) as session:
            await session.execute(
                insert(audit_log).values(
                    tenant_id=entry.tenant_id,
                    actor_user_id=entry.actor_user_id,
                    actor_type=entry.actor_type,
                    action=entry.action,
                    resource_type=entry.resource_type,
                    resource_id=entry.resource_id,
                    detail=dict(entry.detail),
                    outcome=entry.outcome,
                    origin=dict(entry.origin),
                    correlation=dict(entry.correlation),
                    actor_pseudonym=entry.actor_pseudonym,
                    workspace_id=entry.workspace_id,
                )
            )
