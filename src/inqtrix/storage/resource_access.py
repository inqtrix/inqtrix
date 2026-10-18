"""Transactional authorization helpers for shareable resource mutations.

HTTP and capability reads use :class:`AuthorizationService`. PostgreSQL
mutations additionally call this module inside their existing transaction so
a revoke, disable, membership removal, or resource deletion cannot slip
between authorization and persistence. The policy remains deliberately small:
owner or one accepted direct share, optionally bounded by a common workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import and_, exists, func, insert, literal, or_, select, update

from inqtrix.auth.permissions import (
    AccessMode,
    ResourceAccess,
    SharePermission,
    share_permissions_for_resource,
    share_permissions_satisfying,
)
from inqtrix.storage.identity_orm import (
    audit_log,
    resource_shares,
    users,
    workspace_members,
)
from inqtrix.storage.user_events_postgres import append_user_invalidation

if TYPE_CHECKING:
    from sqlalchemy import Column, Table
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class LockedResourceAccess:
    """Successful mutation authorization held by database row locks.

    ``share_permission`` is the grant of the accepted direct share that
    admitted a recipient, locked together with the resource. It is ``None``
    for the owner and for an ownerless row of an unscoped deployment, whose
    access satisfies every minimum.
    """

    owner_user_id: uuid.UUID | None
    share_permission: SharePermission | None = None

    def admits(self, resource_type: str, minimum: SharePermission) -> bool:
        """Return whether this locked access also satisfies ``minimum``.

        A caller that locked the access for a weaker minimum judges a
        stronger one here, without a second lock round trip. At most one
        direct share per recipient and resource is active
        (``uq_resource_shares_active``), so the locked grant is the only
        one that could satisfy it.
        """
        return self.share_permission is None or (
            self.share_permission
            in share_permissions_satisfying(resource_type, minimum)
        )


VISIBLE_SHARE_PERMISSION = "_inqtrix_share_permission"


def _live_resource_filters(
    resource_type: str,
    resource_table: "Table",
) -> tuple[Any, ...]:
    """Return resource-kind predicates that define a live authorization row."""
    if resource_type == "editor_document":
        return (resource_table.c.deleted_at.is_(None),)
    return ()


def visible_resource_select(
    *,
    resource_table: "Table",
    id_column: "Column[Any]",
    owner_column: "Column[Any]",
    resource_type: str,
    tenant_id: str,
    actor_user_id: uuid.UUID | None,
    restrict_to_workspace_members: bool,
    sharing_enabled: bool = True,
) -> Any:
    """Build one authoritative owned-or-accepted-shared list query.

    The returned statement selects every resource-table column plus a labelled
    direct-share permission. It is the common PostgreSQL list boundary for
    collections, prompts, skills, and editor documents; callers add only their
    ordering.
    """
    live_filters = _live_resource_filters(resource_type, resource_table)
    if actor_user_id is None:
        return select(
            resource_table,
            literal(None).label(VISIBLE_SHARE_PERMISSION),
        ).where(
            resource_table.c.tenant_id == tenant_id,
            *live_filters,
            owner_column.is_(None),
        )

    if not sharing_enabled:
        return select(
            resource_table,
            literal(None).label(VISIBLE_SHARE_PERMISSION),
        ).where(
            resource_table.c.tenant_id == tenant_id,
            *live_filters,
            owner_column == actor_user_id,
        )

    share = resource_shares.alias(f"visible_{resource_type}_share")
    valid_share_permissions = tuple(
        permission.value
        for permission in share_permissions_for_resource(resource_type)
    )
    share_join = and_(
        share.c.tenant_id == tenant_id,
        share.c.resource_type == resource_type,
        share.c.resource_id == id_column,
        share.c.recipient_user_id == actor_user_id,
        share.c.permission.in_(valid_share_permissions),
        share.c.accepted_at.isnot(None),
        share.c.revoked_at.is_(None),
    )
    shared_allowed = share.c.id.isnot(None)
    if restrict_to_workspace_members:
        owner_member = workspace_members.alias(
            f"visible_{resource_type}_owner_member"
        )
        actor_member = workspace_members.alias(
            f"visible_{resource_type}_actor_member"
        )
        shared_allowed = and_(
            shared_allowed,
            exists(
                select(1)
                .select_from(
                    owner_member.join(
                        actor_member,
                        owner_member.c.workspace_id == actor_member.c.workspace_id,
                    )
                )
                .where(
                    owner_member.c.tenant_id == tenant_id,
                    actor_member.c.tenant_id == tenant_id,
                    owner_member.c.user_id == owner_column,
                    actor_member.c.user_id == actor_user_id,
                )
            ),
        )
    active_actor = exists(
        select(1).where(
            users.c.tenant_id == tenant_id,
            users.c.id == actor_user_id,
            users.c.disabled_at.is_(None),
        )
    )
    return (
        select(resource_table, share.c.permission.label(VISIBLE_SHARE_PERMISSION))
        .select_from(resource_table.outerjoin(share, share_join))
        .where(
            resource_table.c.tenant_id == tenant_id,
            *live_filters,
            active_actor,
            or_(owner_column == actor_user_id, shared_allowed),
        )
    )


def listed_resource_access(
    *,
    owner_user_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    share_permission: str | None,
) -> ResourceAccess:
    """Translate a row admitted by :func:`visible_resource_select`."""
    if actor_user_id is None:
        return ResourceAccess(AccessMode.UNSCOPED)
    if owner_user_id == actor_user_id:
        return ResourceAccess(AccessMode.OWNER)
    if share_permission is None:
        raise RuntimeError("visible shared row is missing its permission")
    return ResourceAccess(
        AccessMode.SHARED,
        permission=SharePermission(share_permission),
    )


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def lock_active_users(
    session: "AsyncSession",
    *,
    tenant_id: str,
    user_ids: Sequence[uuid.UUID],
) -> bool:
    """Share-lock canonical users in UUID order and require all to be active.

    ``FOR SHARE`` still prevents a concurrent disable or delete from changing
    the authorization decision before this transaction commits.  Unlike
    ``FOR UPDATE``, it is compatible with the key-share lock PostgreSQL takes
    when transactional outbox rows reference ``users``.  That compatibility
    is important for collaboration mutations: they lock a document and then
    append user events while other actors authorize against the same document.
    """
    expected = tuple(sorted(set(user_ids), key=str))
    if not expected:
        return True
    rows = (
        await session.execute(
            select(users.c.id)
            .where(
                users.c.tenant_id == tenant_id,
                users.c.id.in_(expected),
                users.c.disabled_at.is_(None),
            )
            .order_by(users.c.id)
            .with_for_update(read=True)
        )
    ).scalars()
    return set(rows) == set(expected)


async def lock_workspace_memberships(
    session: "AsyncSession",
    *,
    tenant_id: str,
    user_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Lock existing membership rows deterministically and group them."""
    ordered_ids = tuple(sorted(set(user_ids), key=str))
    rows = (
        await session.execute(
            select(
                workspace_members.c.user_id,
                workspace_members.c.workspace_id,
            )
            .where(
                workspace_members.c.tenant_id == tenant_id,
                workspace_members.c.user_id.in_(ordered_ids),
            )
            .order_by(
                workspace_members.c.workspace_id,
                workspace_members.c.user_id,
            )
            .with_for_update()
        )
    ).all()
    grouped = {user_id: set() for user_id in ordered_ids}
    for user_id, workspace_id in rows:
        grouped[user_id].add(workspace_id)
    return grouped


async def lock_resource_access(
    session: "AsyncSession",
    *,
    tenant_id: str,
    actor_user_id: uuid.UUID | None,
    resource_type: str,
    resource_table: "Table",
    id_column: "Column[Any]",
    resource_id: str,
    owner_column: "Column[Any]",
    minimum: SharePermission,
    restrict_to_workspace_members: bool,
    sharing_enabled: bool = True,
    owner_only: bool = False,
) -> LockedResourceAccess | None:
    """Lock a resource and its applicable share, returning its owner.

    ``None`` means denied or absent. The result object can carry a ``None``
    owner for an authorized ownerless row in an unscoped deployment, and
    names the grant of the share that admitted a recipient.

    The rows are locked in one order: the actor's user row, then the
    owner's and the actor's workspace membership rows (only for an actor
    who is not the owner, when access is restricted to workspace members),
    then the resource row, then the share row. A caller that also locks
    rows hanging off the resource, such as a document's collaboration
    leases, calls this first.
    """
    live_filters = _live_resource_filters(resource_type, resource_table)
    preliminary = (
        await session.execute(
            select(id_column, owner_column).where(
                resource_table.c.tenant_id == tenant_id,
                id_column == resource_id,
                *live_filters,
            )
        )
    ).first()
    if preliminary is None:
        return None
    preliminary_owner_raw = preliminary[1]
    preliminary_owner = _as_uuid(preliminary_owner_raw)

    if actor_user_id is None:
        if preliminary_owner_raw is not None:
            return None
    else:
        if preliminary_owner is None:
            return None
        if not await lock_active_users(
            session,
            tenant_id=tenant_id,
            user_ids=(actor_user_id,),
        ):
            return None
        if (
            preliminary_owner != actor_user_id
            and restrict_to_workspace_members
        ):
            memberships = await lock_workspace_memberships(
                session,
                tenant_id=tenant_id,
                user_ids=(preliminary_owner, actor_user_id),
            )
            if not memberships[preliminary_owner].intersection(
                memberships[actor_user_id]
            ):
                return None

    locked = (
        await session.execute(
            select(id_column, owner_column)
            .where(
                resource_table.c.tenant_id == tenant_id,
                id_column == resource_id,
                *live_filters,
            )
            .with_for_update()
        )
    ).first()
    if locked is None:
        return None
    locked_owner_raw = locked[1]
    if locked_owner_raw != preliminary_owner_raw:
        return None
    locked_owner = _as_uuid(locked_owner_raw)
    if actor_user_id is None:
        return (
            None
            if locked_owner_raw is not None
            else LockedResourceAccess(owner_user_id=None)
        )
    if locked_owner == actor_user_id:
        return LockedResourceAccess(owner_user_id=locked_owner)
    if owner_only or not sharing_enabled:
        return None

    allowed_permissions = tuple(
        permission.value
        for permission in share_permissions_satisfying(
            resource_type,
            minimum,
        )
    )
    if not allowed_permissions:
        return None
    share = (
        await session.execute(
            select(resource_shares.c.id, resource_shares.c.permission)
            .where(
                resource_shares.c.tenant_id == tenant_id,
                resource_shares.c.resource_type == resource_type,
                resource_shares.c.resource_id == resource_id,
                resource_shares.c.recipient_user_id == actor_user_id,
                resource_shares.c.accepted_at.isnot(None),
                resource_shares.c.revoked_at.is_(None),
                resource_shares.c.permission.in_(allowed_permissions),
            )
            .with_for_update()
        )
    ).first()
    if share is None:
        return None
    return LockedResourceAccess(
        owner_user_id=locked_owner,
        share_permission=SharePermission(share.permission),
    )


async def append_audit_row(
    session: "AsyncSession",
    *,
    tenant_id: str,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_type: str = "user",
    detail: dict[str, object] | None = None,
    outcome: str = "success",
    correlation: dict[str, str] | None = None,
    origin: dict[str, str] | None = None,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """One audit row in the caller transaction — WITHOUT invalidations.

    The in-tx twin of the sink write, for index rows (service starts,
    terminals) that must land atomically with their state change but
    have no share-cache side effects. Keeps this module the single
    owner of in-transaction audit inserts.

    The envelope (actor pseudonym, request origin, correlation join keys)
    is derived from the ambient context here, exactly as
    :func:`~inqtrix.services.audit_service.build_audit_entry` derives it for
    the sink path. Two entry points that computed different envelopes left
    whole action families unfilterable in the admin panel depending on which
    one happened to write them.
    """
    from inqtrix.auth.log_redaction import stable_pseudonym
    from inqtrix.services.audit_service import ambient_audit_envelope

    ambient_origin, ambient_correlation = ambient_audit_envelope()
    if correlation:
        ambient_correlation.update(
            {k: str(v) for k, v in correlation.items() if v}
        )
    if origin:
        ambient_origin.update({k: str(v) for k, v in origin.items() if v})

    await session.execute(
        insert(audit_log).values(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail or {},
            outcome=outcome,
            origin=ambient_origin,
            correlation=ambient_correlation,
            actor_pseudonym=(
                stable_pseudonym("usr", actor_user_id)
                if actor_user_id is not None
                else None
            ),
            workspace_id=workspace_id,
        )
    )


async def append_resource_effects(
    session: "AsyncSession",
    *,
    tenant_id: str,
    actor_user_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str,
    scope: str,
    additional_targets: Sequence[uuid.UUID] = (),
    actor_type: str = "user",
    detail: dict[str, object] | None = None,
    outcome: str = "success",
    correlation: dict[str, str] | None = None,
) -> None:
    """Append audit and content-free invalidations in the caller transaction.

    ``outcome``/``correlation`` fill the 0072 read-model columns; the
    actor pseudonym is stamped here so every row in the admin panel
    carries the same ``usr_<hex16>`` reference the logs and traces use.
    """
    await append_audit_row(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        outcome=outcome,
        correlation=correlation,
    )
    recipients = (
        await session.execute(
            select(resource_shares.c.recipient_user_id).where(
                resource_shares.c.tenant_id == tenant_id,
                resource_shares.c.resource_type == resource_type,
                resource_shares.c.resource_id == resource_id,
                resource_shares.c.revoked_at.is_(None),
            )
        )
    ).scalars()
    targets = set(recipients).union(additional_targets)
    if owner_user_id is not None:
        targets.add(owner_user_id)
    if actor_user_id is not None:
        targets.add(actor_user_id)
    for target_user_id in sorted(targets, key=str):
        await append_user_invalidation(
            session,
            tenant_id=tenant_id,
            target_user_id=target_user_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
        )


async def revoke_resource_shares(
    session: "AsyncSession",
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    revoked_by_user_id: uuid.UUID | None,
) -> tuple[uuid.UUID, ...]:
    """Lock and revoke all active shares before deleting their resource."""
    rows = (
        await session.execute(
            select(
                resource_shares.c.id,
                resource_shares.c.recipient_user_id,
            )
            .where(
                resource_shares.c.tenant_id == tenant_id,
                resource_shares.c.resource_type == resource_type,
                resource_shares.c.resource_id == resource_id,
                resource_shares.c.revoked_at.is_(None),
            )
            .order_by(resource_shares.c.id)
            .with_for_update()
        )
    ).all()
    if rows:
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
        )
    return tuple(row.recipient_user_id for row in rows)
