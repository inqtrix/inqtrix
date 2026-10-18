"""One transactional authority boundary for process-local persistence.

The memory backend spans several repositories, but it still runs in one
process.  A single short ``RLock`` can therefore provide the same ordering
property that PostgreSQL transactions provide: user disablement, share
revocation, resource authorization, the final mutation, audit, and cache
invalidation become one observable operation.

Only in-memory dictionary work may run while this lock is held.  Network I/O,
provider calls, parsing, embedding, and other suspendable work must finish
before entering the coordinator.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from inqtrix.auth.permissions import SharePermission
from inqtrix.execution_authority import AuthorizationRevoked

if TYPE_CHECKING:
    from inqtrix.auth.directory import MemoryUserDirectory
    from inqtrix.auth.identity_memory import MemoryIdentityStore


@dataclass(frozen=True)
class MemoryResourceSnapshot:
    """Existence and canonical owner read under the coordinator lock."""

    exists: bool
    owner_user_id: uuid.UUID | None


MemoryResourceLookup = Callable[[str, str], MemoryResourceSnapshot]


class MemoryAuthorityCoordinator:
    """Serialize every security-relevant process-local state transition.

    The coordinator owns no duplicate user, share, or resource state.  It
    references the canonical memory repositories and evaluates the same small
    owner/direct-share rule at their final write boundary.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._users: MemoryUserDirectory | None = None
        self._identity: MemoryIdentityStore | None = None
        self._resources: dict[str, MemoryResourceLookup] = {}

    def bind_users(self, users: "MemoryUserDirectory") -> None:
        """Attach the canonical user directory and give it the shared lock."""
        if self._users is not None and self._users is not users:
            raise RuntimeError("memory authority already has a user directory")
        self._users = users
        users._lock = self.lock

    def bind_identity(self, identity: "MemoryIdentityStore") -> None:
        """Attach the direct-share/workspace store and give it the shared lock."""
        if self._identity is not None and self._identity is not identity:
            raise RuntimeError("memory authority already has an identity store")
        self._identity = identity
        identity._lock = self.lock
        identity._authority = self

    def bind_lifecycle(self, transaction: object) -> None:
        """Make the lifecycle command lock the same outer authority lock."""
        command_lock = getattr(transaction, "_command_lock", None)
        if command_lock is None:
            raise TypeError("memory lifecycle transaction has no command lock")
        setattr(transaction, "_command_lock", self.lock)

    def register_resource(
        self,
        resource_type: str,
        lookup: MemoryResourceLookup,
    ) -> None:
        """Register the sole owner/existence lookup for a shareable kind."""
        normalized = resource_type.strip()
        if not normalized or not callable(lookup):
            raise ValueError("resource type and lookup are required")
        current = self._resources.get(normalized)
        if current is not None and current != lookup:
            raise RuntimeError(f"memory authority already registered {normalized}")
        self._resources[normalized] = lookup

    def _require_bound(self) -> tuple["MemoryUserDirectory", "MemoryIdentityStore"]:
        if self._users is None or self._identity is None:
            raise AuthorizationRevoked("process-local authority is not fully composed")
        return self._users, self._identity

    def _active_user_locked(self, tenant_id: str, user_id: uuid.UUID) -> bool:
        users, _identity = self._require_bound()
        return users.is_active_nowait(tenant_id=tenant_id, user_id=user_id)

    def require_active_actor(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Fail closed when a scoped actor disappeared or was disabled."""
        if actor_user_id is None:
            return
        if not self._active_user_locked(tenant_id, actor_user_id):
            raise AuthorizationRevoked("memory actor is missing or disabled")

    @contextmanager
    def creation_guard(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> Iterator[None]:
        """Hold active-user admission across one owner resource creation."""
        with self.lock:
            self.require_active_actor(tenant_id=tenant_id, actor_user_id=actor_user_id)
            yield

    def _resource_snapshot_locked(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> MemoryResourceSnapshot:
        lookup = self._resources.get(resource_type)
        if lookup is None:
            raise AuthorizationRevoked(
                f"memory resource type {resource_type} is not registered"
            )
        return lookup(tenant_id, resource_id)

    def resource_snapshot(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> MemoryResourceSnapshot:
        """Return the canonical resource fact without introducing an owner cache."""
        with self.lock:
            return self._resource_snapshot_locked(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )

    @contextmanager
    def registered_resource_access_guard(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        minimum: SharePermission,
        owner_only: bool = False,
    ) -> Iterator[None]:
        """Resolve the canonical owner, then hold live authority through a write."""
        with self.lock:
            snapshot = self._resource_snapshot_locked(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            if not snapshot.exists:
                raise AuthorizationRevoked("memory resource is missing")
            with self.resource_access_guard(
                tenant_id=tenant_id,
                owner_user_id=snapshot.owner_user_id,
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                minimum=minimum,
                owner_only=owner_only,
            ):
                yield

    @contextmanager
    def resource_access_guard(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        minimum: SharePermission,
        owner_only: bool = False,
    ) -> Iterator[None]:
        """Revalidate live owner/share authority and hold it through a write."""
        with self.lock:
            _users, identity = self._require_bound()
            snapshot = self._resource_snapshot_locked(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            if not snapshot.exists or snapshot.owner_user_id != owner_user_id:
                raise AuthorizationRevoked("memory resource is missing or replaced")
            if actor_user_id is None:
                allowed = owner_user_id is None
            else:
                self.require_active_actor(
                    tenant_id=tenant_id, actor_user_id=actor_user_id
                )
                if actor_user_id == owner_user_id:
                    allowed = True
                elif owner_only or owner_user_id is None:
                    allowed = False
                else:
                    permission = identity._permission_for_locked(
                        tenant_id=tenant_id,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        recipient_user_id=actor_user_id,
                    )
                    allowed = permission is not None and permission.at_least(minimum)
                    if allowed and identity.restrict_to_workspace_members:
                        allowed = identity._users_share_workspace_locked(
                            tenant_id=tenant_id,
                            user_id_a=owner_user_id,
                            user_id_b=actor_user_id,
                        )
            if not allowed:
                raise AuthorizationRevoked(
                    "memory resource authority is missing or revoked"
                )
            yield

    def validate_share_grant(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_user_id: uuid.UUID,
        granted_by_user_id: uuid.UUID,
        recipient_user_ids: Sequence[uuid.UUID],
        restrict_to_members: bool,
    ) -> None:
        """Validate a complete direct-share batch under the final write lock."""
        _users, identity = self._require_bound()
        snapshot = self._resource_snapshot_locked(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if (
            not snapshot.exists
            or snapshot.owner_user_id != owner_user_id
            or granted_by_user_id != owner_user_id
        ):
            raise AuthorizationRevoked("share owner changed or resource vanished")
        user_ids = {owner_user_id, granted_by_user_id, *recipient_user_ids}
        if any(
            not self._active_user_locked(tenant_id, user_id) for user_id in user_ids
        ):
            raise AuthorizationRevoked("share participant is missing or disabled")
        if restrict_to_members and any(
            not identity._users_share_workspace_locked(
                tenant_id=tenant_id,
                user_id_a=owner_user_id,
                user_id_b=recipient_user_id,
            )
            for recipient_user_id in recipient_user_ids
        ):
            raise AuthorizationRevoked("share workspace boundary was revoked")

    def validate_share_removal(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        owner_user_id: uuid.UUID,
        recipient_user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> None:
        """Require a live owner or recipient before removing one share."""
        snapshot = self._resource_snapshot_locked(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if not snapshot.exists or snapshot.owner_user_id != owner_user_id:
            raise AuthorizationRevoked("share resource is missing or replaced")
        self.require_active_actor(tenant_id=tenant_id, actor_user_id=actor_user_id)
        if actor_user_id not in {owner_user_id, recipient_user_id}:
            raise AuthorizationRevoked("share removal actor is not permitted")

    def append_resource_effects(
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
        """Append audit and invalidations inside the held mutation boundary."""
        _users, identity = self._require_bound()
        identity._append_resource_effects_locked(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
            additional_targets=additional_targets,
        )

    def append_audit_row(
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
        """One audit index row WITHOUT invalidations (memory twin of
        ``resource_access.append_audit_row``) — service-start terminals
        land here atomically with the state change."""
        _users, identity = self._require_bound()
        identity._record_effect_locked(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            outcome=outcome,
            correlation=correlation,
            workspace_id=workspace_id,
        )

    def append_registered_resource_effects(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: str,
        scope: str,
        additional_targets: Sequence[uuid.UUID] = (),
    ) -> None:
        """Resolve the current owner and append resource effects under the lock."""
        snapshot = self._resource_snapshot_locked(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if not snapshot.exists:
            raise AuthorizationRevoked("memory resource is missing")
        self.append_resource_effects(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            owner_user_id=snapshot.owner_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
            additional_targets=additional_targets,
        )

    def revoke_deleted_resource(
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
        """Revoke shares and publish deletion effects before removing a row."""
        _users, identity = self._require_bound()
        identity._revoke_deleted_resource_locked(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            scope=scope,
        )
