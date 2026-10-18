"""In-memory identity backend: the no-infrastructure default.

Implements every read port consumed by
:class:`~inqtrix.auth.permissions.AuthorizationService` plus the audit
sink against plain dictionaries. This is the deployment default
(``INQTRIX_STORAGE_BACKEND=memory``): an empty store means scoped
principals have no memberships — they see nothing until granted —
while the legacy unscoped principals never consult it at all.

The mutation helpers (``add_workspace`` etc.) are the seam tests and
future admin surfaces use to arrange facts; the Postgres backend gets
its facts from the identity schema instead.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Sequence

from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.auth.permissions import (
    AuditEntry,
    SharePermission,
    WorkspaceRole,
    share_permissions_for_resource,
)

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
    from inqtrix.auth.shares import ShareRecord

log = logging.getLogger("inqtrix")


@dataclass(frozen=True)
class MemoryWorkspace:
    """One workspace fact in the in-memory identity store."""

    workspace_id: str
    tenant_id: str
    name: str
    created_by_user_id: uuid.UUID | None = None
    """The creator (admin) — surfaced by the admin overview; mirrors the
    Postgres ``workspaces.created_by_user_id`` column. Empty for workspaces
    arranged through the bare ``add_workspace`` test seam."""


@dataclass
class MemoryIdentityStore:
    """Dictionary-backed identity facts with a coarse lock.

    One instance implements the membership, direct-share, and audit ports
    (:class:`~inqtrix.auth.permissions.MembershipRepository`,
    ``ShareRepository``, ``AuditSink``) so tests
    wire a single object. Thread-safe because run workers and request
    handlers live on different threads.
    """

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _authority: "MemoryAuthorityCoordinator | None" = field(
        default=None, init=False, repr=False
    )
    restrict_to_workspace_members: bool = False
    sharing_enabled: bool = True
    _workspaces: dict[str, MemoryWorkspace] = field(default_factory=dict)
    _members: dict[tuple[str, uuid.UUID], WorkspaceRole] = field(default_factory=dict)
    _share_records: dict[str, "ShareRecord"] = field(default_factory=dict)
    """Full share history keyed by share id; active rows have no revoke time."""
    audit_entries: list[AuditEntry] = field(default_factory=list)
    """Recorded audit facts, oldest first (assert target in tests)."""
    audit_rows: list[dict] = field(default_factory=list)
    """Panel projection of the same facts, with the synthetic id and
    timestamp the frozen ``AuditEntry`` deliberately lacks (
    the memory twin of the Postgres audit_log read model)."""
    _event_sink: Callable[..., Any] | None = field(default=None, repr=False)
    _active_admin_user_ids: Callable[[str], Sequence[uuid.UUID]] | None = field(
        default=None, repr=False
    )

    @property
    def atomic_share_effects(self) -> bool:
        """Whether share writes include audit and bound invalidations."""
        return self._event_sink is not None

    @property
    def atomic_workspace_effects(self) -> bool:
        """Whether workspace writes include audit and bound invalidations."""
        return self._event_sink is not None

    def bind_user_event_sink(
        self,
        sink: Callable[..., Any],
        *,
        active_admin_user_ids: Callable[[str], Sequence[uuid.UUID]] | None = None,
    ) -> None:
        """Attach event delivery and the optional active-admin resolver."""
        if not callable(sink):
            raise TypeError("sink must be callable")
        with self._lock:
            self._event_sink = sink
            self._active_admin_user_ids = active_admin_user_ids

    def bind_authority_coordinator(
        self, coordinator: "MemoryAuthorityCoordinator"
    ) -> None:
        """Join the single process-local user/share/resource boundary."""
        coordinator.bind_identity(self)

    def _permission_for_locked(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> SharePermission | None:
        """Return one accepted permission while the identity lock is held."""
        if not self.sharing_enabled:
            return None
        matches = [
            record.permission
            for record in self._share_records.values()
            if record.tenant_id == tenant_id
            and record.recipient_user_id == recipient_user_id
            and record.resource_type == resource_type
            and record.resource_id == resource_id
            and record.revoked_at is None
            and record.accepted_at is not None
        ]
        if any(
            permission not in share_permissions_for_resource(resource_type)
            for permission in matches
        ):
            log.warning(
                "Ignoring invalid memory share permission for resource type %s",
                resource_type,
            )
            return None
        if len(matches) > 1:
            log.error(
                "Multiple active accepted memory shares for "
                "resource_type=%s resource_ref=%s recipient_ref=%s",
                resource_type,
                pseudonymous_log_reference("res", resource_id),
                pseudonymous_log_reference("usr", recipient_user_id),
            )
            return None
        return matches[0] if matches else None

    def _emit_resource_invalidation_locked(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        scope: str,
        additional_targets: Sequence[uuid.UUID] = (),
    ) -> None:
        """Append content-free events while the identity mutation lock is held."""
        if self._event_sink is None:
            return
        targets = {
            record.recipient_user_id
            for record in self._share_records.values()
            if record.tenant_id == tenant_id
            and record.resource_type == resource_type
            and record.resource_id == resource_id
            and record.revoked_at is None
        }
        targets.update(additional_targets)
        if self._active_admin_user_ids is not None:
            targets.update(self._active_admin_user_ids(tenant_id))
        if owner_user_id is not None:
            targets.add(owner_user_id)
        for target_user_id in sorted(targets, key=str):
            self._event_sink(
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope=scope,
                resource_type=resource_type,
                resource_id=resource_id,
            )

    def _record_effect_locked(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: dict[str, str] | None = None,
        outcome: str = "success",
        correlation: dict[str, str] | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Append one audit fact within the enclosing memory mutation."""
        from inqtrix.auth.log_redaction import stable_pseudonym

        entry = AuditEntry(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=dict(detail or {}),
            outcome=outcome,
            correlation=dict(correlation or {}),
            actor_pseudonym=(
                stable_pseudonym("usr", actor_user_id)
                if actor_user_id is not None
                else None
            ),
            workspace_id=workspace_id,
        )
        self.audit_entries.append(entry)
        self._append_audit_row_locked(entry)

    def _append_resource_effects_locked(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        owner_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: str,
        scope: str,
        additional_targets: Sequence[uuid.UUID] = (),
    ) -> None:
        """Append one resource audit and its invalidations atomically."""
        self._record_effect_locked(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        self._emit_resource_invalidation_locked(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
            additional_targets=additional_targets,
        )

    def _revoke_deleted_resource_locked(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        owner_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: str,
        scope: str,
    ) -> None:
        """Revoke every share and append deletion effects under one lock."""
        import time as _time

        active = [
            (share_id, record)
            for share_id, record in self._share_records.items()
            if record.tenant_id == tenant_id
            and record.resource_type == resource_type
            and record.resource_id == resource_id
            and record.revoked_at is None
        ]
        recipients = tuple(record.recipient_user_id for _share_id, record in active)
        for share_id, record in active:
            self._share_records[share_id] = replace(
                record,
                revoked_at=_time.time(),
                revoked_by_user_id=actor_user_id,
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="share.revoked_for_resource",
                resource_type=resource_type,
                resource_id=resource_id,
                detail={"recipient_user_id": str(record.recipient_user_id)},
            )
        self._append_resource_effects_locked(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
            additional_targets=recipients,
        )

    # ------------------------------------------------------------- #
    # Mutation seam (tests / future admin surface)
    # ------------------------------------------------------------- #

    def add_workspace(
        self, workspace_id: str, *, tenant_id: str = "default", name: str = ""
    ) -> None:
        """Create one workspace fact."""
        with self._lock:
            self._workspaces[workspace_id] = MemoryWorkspace(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                name=name or workspace_id,
            )

    async def create_workspace(
        self, *, tenant_id: str, name: str, created_by_user_id: uuid.UUID
    ) -> tuple[str, str]:
        """Create one workspace with its creator as OWNER (atomic here
        by the lock; the Postgres backend uses one transaction)."""
        import uuid as _uuid

        workspace_id = str(_uuid.uuid4())
        with self._lock:
            self._workspaces[workspace_id] = MemoryWorkspace(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                name=name,
                created_by_user_id=created_by_user_id,
            )
            self._members[(workspace_id, created_by_user_id)] = (
                WorkspaceRole.OWNER
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=created_by_user_id,
                action="workspace.created",
                resource_type="workspace",
                resource_id=workspace_id,
                detail={"name": name},
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=created_by_user_id,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
            )
        return workspace_id, name

    async def list_workspaces_for(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[tuple[str, str, WorkspaceRole], ...]:
        """``(id, name, role)`` per membership, name-sorted."""
        with self._lock:
            rows = [
                (
                    workspace_id,
                    self._workspaces[workspace_id].name,
                    role,
                )
                for (workspace_id, member_user_id), role in self._members.items()
                if member_user_id == user_id
                and self._workspaces.get(workspace_id) is not None
                and self._workspaces[workspace_id].tenant_id == tenant_id
            ]
        return tuple(sorted(rows, key=lambda row: row[1]))

    def add_member(
        self, workspace_id: str, user_id: uuid.UUID, role: WorkspaceRole
    ) -> None:
        """Add or update one workspace membership.

        The synchronous arrange/invitation seam (used by
        :class:`~inqtrix.auth.invitations.MemoryInvitationStore`). The async
        :meth:`assign_member` is the admin-surface counterpart.

        Raises:
            KeyError: When the workspace was never created — silently
                materializing it would hide arrangement bugs in tests.
        """
        with self._lock:
            if workspace_id not in self._workspaces:
                raise KeyError(f"unknown workspace: {workspace_id}")
            self._members[(workspace_id, user_id)] = role

    # ------------------------------------------------------------- #
    # MembershipAdminRepository (workspace + membership administration)
    # ------------------------------------------------------------- #

    async def list_all_workspaces(
        self, *, tenant_id: str
    ) -> tuple[tuple[str, str, uuid.UUID | None, int], ...]:
        """``(id, name, created_by_user_id, member_count)`` per tenant workspace."""
        with self._lock:
            rows = [
                (
                    workspace.workspace_id,
                    workspace.name,
                    workspace.created_by_user_id,
                    sum(
                        1
                        for (member_workspace_id, _sub) in self._members
                        if member_workspace_id == workspace.workspace_id
                    ),
                )
                for workspace in self._workspaces.values()
                if workspace.tenant_id == tenant_id
            ]
        return tuple(sorted(rows, key=lambda row: row[1]))

    async def rename_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        name: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Rename one workspace; ``False`` for unknown / foreign-tenant."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return False
            self._workspaces[workspace_id] = replace(workspace, name=name)
            members = tuple(
                user_id
                for (member_workspace_id, user_id) in self._members
                if member_workspace_id == workspace_id
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.renamed",
                resource_type="workspace",
                resource_id=workspace_id,
                detail={"name": name},
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=None,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
                additional_targets=members,
            )
            return True

    async def delete_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Delete the workspace and cascade its memberships; ``False`` when
        absent (mirrors the Postgres ON DELETE CASCADE)."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return False
            affected_user_ids = {
                user_id
                for (member_workspace_id, user_id) in self._members
                if member_workspace_id == workspace_id
            }
            del self._workspaces[workspace_id]
            for key in [
                member_key
                for member_key in self._members
                if member_key[0] == workspace_id
            ]:
                del self._members[key]
            self._reconcile_workspace_shares_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                affected_user_ids=affected_user_ids,
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.deleted",
                resource_type="workspace",
                resource_id=workspace_id,
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=None,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
                additional_targets=tuple(affected_user_ids),
            )
            return True

    async def list_members(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[tuple[uuid.UUID, WorkspaceRole], ...] | None:
        """``(user_id, role)`` per member, UUID-sorted, or ``None``."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return None
            rows = [
                (user_id, role)
                for (member_workspace_id, user_id), role in self._members.items()
                if member_workspace_id == workspace_id
            ]
        return tuple(sorted(rows, key=lambda row: row[0]))

    async def assign_member(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        role: WorkspaceRole,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Upsert one membership at the exact role; ``False`` when absent."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return False
            existing = self._members.get((workspace_id, user_id))
            if existing is WorkspaceRole.OWNER and role is not WorkspaceRole.OWNER:
                owner_count = sum(
                    member_role is WorkspaceRole.OWNER
                    for (member_workspace_id, _), member_role in self._members.items()
                    if member_workspace_id == workspace_id
                )
                if owner_count <= 1:
                    from inqtrix.auth.permissions import LastWorkspaceOwnerError

                    raise LastWorkspaceOwnerError(workspace_id)
            self._members[(workspace_id, user_id)] = role
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=(
                    "workspace.member_added"
                    if existing is None
                    else "workspace.member_role_set"
                ),
                resource_type="workspace",
                resource_id=f"{workspace_id}:{user_id}",
                detail={"role": role.value},
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=None,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
                additional_targets=(user_id,),
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
        """Update one existing membership without materializing a new row."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return False
            member_key = (workspace_id, user_id)
            existing = self._members.get(member_key)
            if existing is None:
                return False
            if existing is WorkspaceRole.OWNER and role is not WorkspaceRole.OWNER:
                owner_count = sum(
                    member_role is WorkspaceRole.OWNER
                    for (
                        member_workspace_id,
                        _member_user_id,
                    ), member_role in self._members.items()
                    if member_workspace_id == workspace_id
                )
                if owner_count <= 1:
                    from inqtrix.auth.permissions import LastWorkspaceOwnerError

                    raise LastWorkspaceOwnerError(workspace_id)
            self._members[member_key] = role
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.member_role_set",
                resource_type="workspace",
                resource_id=f"{workspace_id}:{user_id}",
                detail={"role": role.value},
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=None,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
                additional_targets=(user_id,),
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
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return False
            role = self._members.get((workspace_id, user_id))
            if role is None:
                return False
            if role is WorkspaceRole.OWNER:
                owner_count = sum(
                    member_role is WorkspaceRole.OWNER
                    for (member_workspace_id, _), member_role in self._members.items()
                    if member_workspace_id == workspace_id
                )
                if owner_count <= 1:
                    from inqtrix.auth.permissions import LastWorkspaceOwnerError

                    raise LastWorkspaceOwnerError(workspace_id)
            del self._members[(workspace_id, user_id)]
            self._reconcile_workspace_shares_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                affected_user_ids={user_id},
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="workspace.member_removed",
                resource_type="workspace",
                resource_id=f"{workspace_id}:{user_id}",
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=None,
                resource_type="workspace",
                resource_id=workspace_id,
                scope="workspaces",
                additional_targets=(user_id,),
            )
            return True

    def _users_share_workspace_locked(
        self,
        *,
        tenant_id: str,
        user_id_a: uuid.UUID,
        user_id_b: uuid.UUID,
    ) -> bool:
        """Whether two users retain a common workspace under the lock."""
        workspaces_a = {
            workspace_id
            for (workspace_id, member_user_id) in self._members
            if member_user_id == user_id_a
            and (workspace := self._workspaces.get(workspace_id)) is not None
            and workspace.tenant_id == tenant_id
        }
        return any(
            member_user_id == user_id_b and workspace_id in workspaces_a
            for (workspace_id, member_user_id) in self._members
        )

    def _reconcile_workspace_shares_locked(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        affected_user_ids: set[uuid.UUID] | None,
    ) -> int:
        """Revoke active shares that no longer satisfy the workspace rule."""
        if not self.restrict_to_workspace_members:
            return 0
        if self._authority is None:
            raise RuntimeError(
                "workspace share reconciliation requires the canonical "
                "memory authority coordinator"
            )
        revoked = 0
        for share_id, record in tuple(self._share_records.items()):
            if record.tenant_id != tenant_id or record.revoked_at is not None:
                continue
            resource = self._authority.resource_snapshot(
                tenant_id=tenant_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
            )
            owner_user_id = resource.owner_user_id if resource.exists else None
            if (
                owner_user_id is not None
                and affected_user_ids is not None
                and owner_user_id not in affected_user_ids
                and record.recipient_user_id not in affected_user_ids
            ):
                continue
            if owner_user_id is not None and self._users_share_workspace_locked(
                tenant_id=tenant_id,
                user_id_a=owner_user_id,
                user_id_b=record.recipient_user_id,
            ):
                continue
            self._share_records[share_id] = replace(
                record,
                revoked_at=time.time(),
                revoked_by_user_id=actor_user_id,
            )
            self.audit_entries.append(
                AuditEntry(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    action="share.workspace_boundary_revoked",
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    detail={
                        "recipient_user_id": str(record.recipient_user_id)
                    },
                )
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                scope="sharing",
                additional_targets=(record.recipient_user_id,),
            )
            revoked += 1
        return revoked

    async def reconcile_workspace_shares(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Reconcile every active direct share before serving requests."""
        with self._lock:
            return self._reconcile_workspace_shares_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                affected_user_ids=None,
            )

    def _write_share_locked(
        self,
        *,
        tenant_id: str,
        recipient_user_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
        permission: SharePermission,
        granted_by_user_id: uuid.UUID,
        accepted_at: float | None,
        revision: int = 1,
    ) -> "ShareRecord":
        """Write the one canonical lifecycle row for a direct share."""
        import time as _time
        import uuid as _uuid

        from inqtrix.auth.shares import ShareRecord

        share_id = str(_uuid.uuid4())
        record = ShareRecord(
            id=share_id,
            tenant_id=tenant_id,
            recipient_user_id=recipient_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permission=permission,
            revision=revision,
            granted_by_user_id=granted_by_user_id,
            created_at=_time.time(),
            accepted_at=accepted_at,
        )
        self._share_records[share_id] = record
        return record

    def add_share(
        self,
        *,
        recipient_user_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
        permission: SharePermission,
        tenant_id: str = "default",
        granted_by_user_id: uuid.UUID,
        accepted: bool = True,
    ) -> None:
        """Arrange one share tuple for tests and memory deployments.

        The synchronous seed/arrange seam. Defaults to ``accepted=True`` so
        arranged fixtures grant access immediately (mirroring the migration
        that backfills pre-existing rows as accepted); pass ``accepted=False``
        to seed a pending invitation awaiting consent.
        """
        import time as _time

        with self._lock:
            if any(
                record.tenant_id == tenant_id
                and record.recipient_user_id == recipient_user_id
                and record.resource_type == resource_type
                and record.resource_id == resource_id
                and record.revoked_at is None
                for record in self._share_records.values()
            ):
                raise ValueError("active share already exists")
            self._write_share_locked(
                tenant_id=tenant_id,
                recipient_user_id=recipient_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                permission=permission,
                granted_by_user_id=granted_by_user_id,
                accepted_at=_time.time() if accepted else None,
            )

    def revoke_share(
        self,
        *,
        recipient_user_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
        tenant_id: str = "default",
    ) -> None:
        """Revoke one share tuple; missing grants are a no-op."""
        import time as _time

        with self._lock:
            for share_id, record in tuple(self._share_records.items()):
                if (
                    record.tenant_id == tenant_id
                    and record.recipient_user_id == recipient_user_id
                    and record.resource_type == resource_type
                    and record.resource_id == resource_id
                    and record.revoked_at is None
                ):
                    self._share_records[share_id] = replace(
                        record, revoked_at=_time.time()
                    )

    # ------------------------------------------------------------- #
    # MembershipRepository
    # ------------------------------------------------------------- #

    async def workspace_ids_for(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[str, ...]:
        """All workspace ids the canonical user belongs to in the tenant."""
        with self._lock:
            return tuple(
                workspace_id
                for (workspace_id, member_user_id), _role in self._members.items()
                if member_user_id == user_id
                and (workspace := self._workspaces.get(workspace_id)) is not None
                and workspace.tenant_id == tenant_id
            )

    async def role_in_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, workspace_id: str
    ) -> WorkspaceRole | None:
        """The member's role, or ``None`` for non-members and unknown
        workspaces alike."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.tenant_id != tenant_id:
                return None
            return self._members.get((workspace_id, user_id))

    # ------------------------------------------------------------- #
    # ShareRepository
    # ------------------------------------------------------------- #

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
        """Insert an invite batch atomically, rejecting active duplicates."""
        from inqtrix.auth.shares import ShareConflict, ShareNotAllowed
        from inqtrix.execution_authority import AuthorizationRevoked

        with self._lock:
            if self._authority is not None:
                try:
                    self._authority.validate_share_grant(
                        tenant_id=tenant_id,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        owner_user_id=owner_user_id,
                        granted_by_user_id=granted_by_user_id,
                        recipient_user_ids=tuple(
                            recipient for recipient, _permission in invitees
                        ),
                        restrict_to_members=restrict_to_members,
                    )
                except AuthorizationRevoked as exc:
                    raise ShareNotAllowed() from exc
            active_keys = {
                (
                    record.recipient_user_id,
                    record.resource_type,
                    record.resource_id,
                )
                for record in self._share_records.values()
                if record.tenant_id == tenant_id and record.revoked_at is None
            }
            if any(
                (recipient_user_id, resource_type, resource_id) in active_keys
                for recipient_user_id, _permission in invitees
            ):
                raise ShareConflict("An active share already exists")
            created = tuple(
                self._write_share_locked(
                    tenant_id=tenant_id,
                    recipient_user_id=recipient_user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    permission=permission,
                    granted_by_user_id=granted_by_user_id,
                    accepted_at=None,
                )
                for recipient_user_id, permission in invitees
            )
            for record in created:
                self._record_effect_locked(
                    tenant_id=tenant_id,
                    actor_user_id=granted_by_user_id,
                    action="share.granted",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    detail={
                        "recipient_user_id": str(record.recipient_user_id),
                        "permission": record.permission.value,
                    },
                )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=granted_by_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                scope="sharing",
                additional_targets=tuple(
                    record.recipient_user_id for record in created
                ),
            )
            return created

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
        """CAS-update one active share while retaining consent."""
        from inqtrix.auth.shares import ShareConflict, ShareNotAllowed
        from inqtrix.execution_authority import AuthorizationRevoked

        with self._lock:
            record = self._share_records.get(share_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.revoked_at is not None
            ):
                return None
            if self._authority is not None:
                try:
                    self._authority.validate_share_grant(
                        tenant_id=tenant_id,
                        resource_type=record.resource_type,
                        resource_id=record.resource_id,
                        owner_user_id=actor_user_id,
                        granted_by_user_id=actor_user_id,
                        recipient_user_ids=(record.recipient_user_id,),
                        restrict_to_members=restrict_to_members,
                    )
                except AuthorizationRevoked as exc:
                    raise ShareNotAllowed() from exc
            if record.revision != expected_revision:
                raise ShareConflict(
                    "The share has already changed",
                    current_revision=record.revision,
                )
            updated = replace(
                record, permission=permission, revision=record.revision + 1
            )
            self._share_records[share_id] = updated
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="share.permission_updated",
                resource_type=updated.resource_type,
                resource_id=updated.resource_id,
                detail={
                    "recipient_user_id": str(updated.recipient_user_id),
                    "permission": updated.permission.value,
                    "revision": str(updated.revision),
                },
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=actor_user_id,
                resource_type=updated.resource_type,
                resource_id=updated.resource_id,
                scope="sharing",
                additional_targets=(updated.recipient_user_id,),
            )
            return updated

    async def accept_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        recipient_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        restrict_to_members: bool = False,
    ) -> "ShareRecord | None":
        """Accept pending or return an already accepted active share."""
        import time as _time
        from dataclasses import replace as _replace

        from inqtrix.execution_authority import AuthorizationRevoked

        with self._lock:
            record = self._share_records.get(share_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.recipient_user_id != recipient_user_id
                or record.revoked_at is not None
            ):
                return None
            if self._authority is not None:
                try:
                    self._authority.validate_share_grant(
                        tenant_id=tenant_id,
                        resource_type=record.resource_type,
                        resource_id=record.resource_id,
                        owner_user_id=owner_user_id,
                        granted_by_user_id=owner_user_id,
                        recipient_user_ids=(recipient_user_id,),
                        restrict_to_members=restrict_to_members,
                    )
                except AuthorizationRevoked:
                    return None
            if record.accepted_at is not None:
                return record
            accepted = _replace(record, accepted_at=_time.time())
            self._share_records[share_id] = accepted
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=recipient_user_id,
                action="share.accepted",
                resource_type=accepted.resource_type,
                resource_id=accepted.resource_id,
                detail={"owner_user_id": str(owner_user_id)},
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                resource_type=accepted.resource_type,
                resource_id=accepted.resource_id,
                scope="sharing",
                additional_targets=(accepted.recipient_user_id,),
            )
            return accepted

    async def get_share(
        self, *, tenant_id: str, share_id: str
    ) -> "ShareRecord | None":
        with self._lock:
            record = self._share_records.get(share_id)
        if (
            record is None
            or record.tenant_id != tenant_id
            or record.revoked_at is not None
        ):
            return None
        return record

    async def revoke_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        revoked_by_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> "ShareRecord | None":
        import time as _time

        from inqtrix.execution_authority import AuthorizationRevoked

        with self._lock:
            record = self._share_records.get(share_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.revoked_at is not None
            ):
                return None
            if self._authority is not None:
                try:
                    self._authority.validate_share_removal(
                        tenant_id=tenant_id,
                        resource_type=record.resource_type,
                        resource_id=record.resource_id,
                        owner_user_id=owner_user_id,
                        recipient_user_id=record.recipient_user_id,
                        actor_user_id=revoked_by_user_id,
                    )
                except AuthorizationRevoked:
                    return None
            revoked = replace(
                record,
                revoked_at=_time.time(),
                revoked_by_user_id=revoked_by_user_id,
            )
            self._share_records[share_id] = revoked
            action = (
                "share.declined"
                if revoked_by_user_id == record.recipient_user_id
                and record.accepted_at is None
                else "share.left"
                if revoked_by_user_id == record.recipient_user_id
                else "share.revoked"
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=revoked_by_user_id,
                action=action,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                detail={
                    "recipient_user_id": str(record.recipient_user_id)
                },
            )
            self._emit_resource_invalidation_locked(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                scope="sharing",
                additional_targets=(record.recipient_user_id,),
            )
            return revoked

    async def revoke_shares_for_resource(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        revoked_by_user_id: uuid.UUID,
    ) -> int:
        """Revoke every active share on one resource (deletion cleanup)."""
        import time as _time

        with self._lock:
            doomed = [
                share_id
                for share_id, record in self._share_records.items()
                if record.tenant_id == tenant_id
                and record.resource_type == resource_type
                and record.resource_id == resource_id
                and record.revoked_at is None
            ]
            for share_id in doomed:
                record = self._share_records[share_id]
                self._share_records[share_id] = replace(
                    record,
                    revoked_at=_time.time(),
                    revoked_by_user_id=revoked_by_user_id,
                )
                self._record_effect_locked(
                    tenant_id=tenant_id,
                    actor_user_id=revoked_by_user_id,
                    action="share.revoked",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    detail={
                        "recipient_user_id": str(record.recipient_user_id)
                    },
                )
            if doomed:
                self._emit_resource_invalidation_locked(
                    tenant_id=tenant_id,
                    owner_user_id=revoked_by_user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    scope="sharing",
                    additional_targets=tuple(
                        self._share_records[share_id].recipient_user_id
                        for share_id in doomed
                    ),
                )
            return len(doomed)

    async def list_shares_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple["ShareRecord", ...]:
        with self._lock:
            rows = [
                record
                for record in self._share_records.values()
                if record.tenant_id == tenant_id
                and record.resource_type == resource_type
                and record.resource_id == resource_id
                and record.revoked_at is None
            ]
        return tuple(sorted(rows, key=lambda record: record.created_at))

    async def shares_for_recipient(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        recipient_user_id: uuid.UUID,
    ) -> dict[str, "ShareRecord"]:
        best: dict[str, "ShareRecord"] = {}
        with self._lock:
            for record in self._share_records.values():
                if (
                    record.tenant_id != tenant_id
                    or record.resource_type != resource_type
                    or record.accepted_at is None
                    or record.revoked_at is not None
                    or record.recipient_user_id != recipient_user_id
                ):
                    continue
                current = best.get(record.resource_id)
                if current is None or record.permission.at_least(
                    current.permission
                ):
                    best[record.resource_id] = record
        return best

    async def inbox_for_recipient(
        self, *, tenant_id: str, recipient_user_id: uuid.UUID
    ) -> tuple["ShareRecord", ...]:
        """Active pending and accepted shares addressed to one user.

        Pending rows remain visible so the recipient inbox can offer consent;
        results are oldest first.
        """
        with self._lock:
            rows = [
                record
                for record in self._share_records.values()
                if record.tenant_id == tenant_id
                and record.recipient_user_id == recipient_user_id
                and record.revoked_at is None
            ]
        return tuple(sorted(rows, key=lambda record: record.created_at))

    async def list_active_shares(
        self, *, tenant_id: str
    ) -> tuple["ShareRecord", ...]:
        """Active lifecycle rows; ownership is resolved from resources."""
        with self._lock:
            rows = [
                record
                for record in self._share_records.values()
                if record.tenant_id == tenant_id
                and record.revoked_at is None
            ]
        return tuple(sorted(rows, key=lambda record: record.created_at))

    async def share_counts_for_resources(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_ids: Sequence[str],
    ) -> dict[str, int]:
        wanted = set(resource_ids)
        counts: dict[str, int] = {}
        with self._lock:
            for record in self._share_records.values():
                if (
                    record.tenant_id == tenant_id
                    and record.resource_type == resource_type
                    and record.resource_id in wanted
                    and record.revoked_at is None
                ):
                    counts[record.resource_id] = (
                        counts.get(record.resource_id, 0) + 1
                    )
        return counts

    async def permission_for(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> SharePermission | None:
        """Accepted direct grant for the recipient, if one exists."""
        with self._lock:
            return self._permission_for_locked(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                recipient_user_id=recipient_user_id,
            )

    def permission_for_sync(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> SharePermission | None:
        """Synchronous live lookup for the threaded in-memory run store."""
        with self._lock:
            return self._permission_for_locked(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                recipient_user_id=recipient_user_id,
            )

    def share_workspace_sync(
        self, *, tenant_id: str, user_id_a: uuid.UUID, user_id_b: uuid.UUID
    ) -> bool:
        """Whether two users currently share an in-memory workspace."""
        with self._lock:
            workspaces_a = {
                workspace_id
                for (workspace_id, user_id), _role in self._members.items()
                if user_id == user_id_a
                and (workspace := self._workspaces.get(workspace_id)) is not None
                and workspace.tenant_id == tenant_id
            }
            return any(
                user_id == user_id_b and workspace_id in workspaces_a
                for (workspace_id, user_id), _role in self._members.items()
            )

    @contextmanager
    def resource_access_guard_sync(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        minimum: SharePermission,
    ):
        """Hold the identity lock across one in-memory resource mutation.

        The run store already holds its own record lock when entering this
        guard. Share revoke uses only this identity lock, so the mutation and
        revoke have one observable order instead of a check/write window.
        """
        from inqtrix.execution_authority import AuthorizationRevoked

        if self._authority is not None:
            with self._authority.resource_access_guard(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                minimum=minimum,
            ):
                yield
            return
        with self._lock:
            if actor_user_id is None:
                allowed = owner_user_id is None
            elif actor_user_id == owner_user_id:
                allowed = True
            elif owner_user_id is None:
                allowed = False
            else:
                permission = self._permission_for_locked(
                    tenant_id=tenant_id,
                    recipient_user_id=actor_user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
                allowed = permission is not None and permission.at_least(minimum)
                if allowed and self.restrict_to_workspace_members:
                    allowed = self._users_share_workspace_locked(
                        tenant_id=tenant_id,
                        user_id_a=owner_user_id,
                        user_id_b=actor_user_id,
                    )
            if not allowed:
                raise AuthorizationRevoked(
                    "in-memory resource authority is missing or revoked"
                )
            yield

    # ------------------------------------------------------------- #
    # AuditSink
    # ------------------------------------------------------------- #

    def _append_audit_row_locked(self, entry: AuditEntry) -> None:
        import time as _time

        self.audit_rows.append(
            {
                "id": len(self.audit_rows) + 1,
                "occurred_at": _time.time(),
                "action": entry.action,
                "resource_type": entry.resource_type,
                "resource_id": entry.resource_id,
                "actor_pseudonym": entry.actor_pseudonym,
                "actor_type": entry.actor_type,
                "outcome": entry.outcome,
                "workspace_id": (
                    str(entry.workspace_id) if entry.workspace_id else None
                ),
                "detail": dict(entry.detail),
                "origin": dict(entry.origin),
                "correlation": dict(entry.correlation),
                "tenant_id": entry.tenant_id,
            }
        )

    async def record(self, entry: AuditEntry) -> None:
        """Append one audit fact (in-memory list, oldest first)."""
        with self._lock:
            self.audit_entries.append(entry)
            self._append_audit_row_locked(entry)

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
    ) -> tuple[list[dict], int | None]:
        """Newest-first audit page — memory twin of the Postgres reader."""
        with self._lock:
            rows = [dict(row) for row in self.audit_rows]
        filtered = []
        for row in reversed(rows):  # newest first
            if row.get("tenant_id") != tenant_id:
                continue
            if action_prefix and not row["action"].startswith(action_prefix):
                continue
            if (
                actor_pseudonym
                and row.get("actor_pseudonym") != actor_pseudonym
            ):
                continue
            if outcome and row["outcome"] != outcome:
                continue
            if resource_type and row["resource_type"] != resource_type:
                continue
            if resource_id and row["resource_id"] != resource_id:
                continue
            if (
                occurred_from is not None
                and row["occurred_at"] < occurred_from
            ):
                continue
            if occurred_to is not None and row["occurred_at"] > occurred_to:
                continue
            if before_id is not None and row["id"] >= before_id:
                continue
            row.pop("tenant_id", None)
            filtered.append(row)
        page_size = max(1, min(int(limit), 200))
        page = filtered[:page_size]
        next_before = (
            page[-1]["id"] if len(filtered) > page_size and page else None
        )
        return page, next_before

    async def prune_audit_log(self, *, days: int) -> int:
        """No-op twin of the Postgres prune (dev backend).

        In-memory entries live only for the process lifetime and carry
        no timestamps — time-based retention does not apply. Returns 0
        so callers can log honestly.
        """
        del days
        return 0
