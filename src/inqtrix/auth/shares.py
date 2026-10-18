"""Direct user-to-resource sharing lifecycle.

The v0.2 contract deliberately has one share model: one active direct share
per recipient and resource, with explicit consent and optimistic revisions.
Owners are read from the resource itself; ``granted_by_user_id`` is audit
metadata and never a second ownership source.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Protocol, Sequence

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import (
    SHARE_PERMISSIONS_BY_RESOURCE_TYPE,
    AuditEntry,
    SharePermission,
    share_permissions_for_resource,
)

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuditSink, AuthorizationService
    from inqtrix.auth.principal import Principal
    from inqtrix.user_events import ResourceInvalidator

log = logging.getLogger("inqtrix")

SHAREABLE_RESOURCE_TYPES = frozenset(SHARE_PERMISSIONS_BY_RESOURCE_TYPE)


@dataclass(frozen=True)
class ShareRecord:
    """One direct share row."""

    id: str
    tenant_id: str
    recipient_user_id: uuid.UUID
    resource_type: str
    resource_id: str
    permission: SharePermission
    revision: int
    granted_by_user_id: uuid.UUID
    created_at: float
    accepted_at: float | None = None
    revoked_at: float | None = None
    revoked_by_user_id: uuid.UUID | None = None


@dataclass(frozen=True)
class InboxItem:
    """Title-enriched recipient lifecycle item."""

    share_id: str
    resource_type: str
    resource_id: str
    resource_title: str
    permission: SharePermission
    revision: int
    granted_by_user_id: uuid.UUID
    created_at: float
    accepted_at: float | None


@dataclass(frozen=True)
class OutgoingItem:
    """One owner resource grouped across active outgoing shares."""

    resource_type: str
    resource_id: str
    resource_title: str
    share_count: int
    pending_count: int


class ShareRemoval(StrEnum):
    """Observable lifecycle result of DELETE ``/v1/shares/{id}``."""

    REVOKED = "revoked"
    DECLINED = "declined"
    LEFT = "left"


@dataclass(frozen=True)
class ShareRemovalResult:
    """Removed row and its lifecycle meaning."""

    record: ShareRecord
    action: ShareRemoval


class ShareNotAllowed(Exception):
    """The share or resource must remain hidden from the caller."""


class ShareValidationError(ValueError):
    """The request is invalid and must produce no writes."""


class ShareBackendUnsupported(RuntimeError):
    """A known resource kind lacks a safe sharing boundary in this deployment."""


class ShareConflict(RuntimeError):
    """An active tuple exists or an optimistic revision is stale."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ShareAdminRepository(Protocol):
    """Persistence port for atomic direct-share commands and lifecycle views."""

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
    ) -> tuple[ShareRecord, ...]:
        """Insert every share or none; active tuple conflicts are explicit."""
        ...

    async def get_share(
        self, *, tenant_id: str, share_id: str
    ) -> ShareRecord | None:
        """Return one active share."""
        ...

    async def update_share_permission(
        self,
        *,
        tenant_id: str,
        share_id: str,
        permission: SharePermission,
        expected_revision: int,
        actor_user_id: uuid.UUID,
        restrict_to_members: bool = False,
    ) -> ShareRecord | None:
        """CAS-update permission and increment revision."""
        ...

    async def accept_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        recipient_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        restrict_to_members: bool = False,
    ) -> ShareRecord | None:
        """Accept pending or return the already accepted active row."""
        ...

    async def revoke_share_by_id(
        self,
        *,
        tenant_id: str,
        share_id: str,
        revoked_by_user_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> ShareRecord | None:
        """Soft-revoke one active row."""
        ...

    async def revoke_shares_for_resource(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        revoked_by_user_id: uuid.UUID,
    ) -> int:
        """Revoke every active share on a deleted resource."""
        ...

    async def list_shares_for_resource(
        self, *, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple[ShareRecord, ...]:
        """List active shares for an owner-managed resource."""
        ...

    async def shares_for_recipient(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        recipient_user_id: uuid.UUID,
    ) -> Mapping[str, ShareRecord]:
        """Accepted direct shares keyed by resource id."""
        ...

    async def inbox_for_recipient(
        self, *, tenant_id: str, recipient_user_id: uuid.UUID
    ) -> tuple[ShareRecord, ...]:
        """Pending and accepted active shares for one recipient."""
        ...

    async def list_active_shares(
        self, *, tenant_id: str
    ) -> tuple[ShareRecord, ...]:
        """List active lifecycle rows without treating audit fields as owner."""
        ...


OwnerResolver = Callable[[str, str], Awaitable[uuid.UUID | None]]
TitleResolver = Callable[[str, str], Awaitable[str | None]]


@dataclass(frozen=True)
class ShareFacts:
    """Owner and title of one shareable resource, read regardless of
    visibility: authorization happens against these facts."""

    owner_user_id: uuid.UUID | None
    title: str | None


# Resolves the facts of many resources of one type in one read: the ids it
# was asked for map to their facts, an id without a shareable resource is
# absent.
ShareFactsResolver = Callable[[str, Sequence[str]], Awaitable[Mapping[str, ShareFacts]]]


class ShareService:
    """Validate and orchestrate the direct-share lifecycle."""

    def __init__(
        self,
        *,
        shares: ShareAdminRepository,
        permissions: "AuthorizationService",
        owner_resolvers: Mapping[str, OwnerResolver],
        share_facts_resolvers: Mapping[str, ShareFactsResolver],
        user_lookup: Callable[[str, uuid.UUID], Awaitable[bool]],
        audit: "AuditSink | None" = None,
        restrict_to_members: bool = False,
        title_resolvers: Mapping[str, TitleResolver] | None = None,
        invalidator: "ResourceInvalidator | None" = None,
        unsupported_resource_types: Sequence[str] = (),
    ) -> None:
        if set(share_facts_resolvers) != set(owner_resolvers):
            # The owner listing reads every type through its batch resolver;
            # a type without one would silently drop out of it.
            raise ValueError(
                "share_facts_resolvers must cover exactly the owner_resolvers types"
            )
        self._shares = shares
        self._permissions = permissions
        self._owner_resolvers = dict(owner_resolvers)
        self._share_facts_resolvers = dict(share_facts_resolvers)
        self._title_resolvers = dict(title_resolvers or {})
        self._user_lookup = user_lookup
        self._audit = audit
        self._restrict_to_members = restrict_to_members
        self._invalidator = invalidator
        self._unsupported_resource_types = frozenset(unsupported_resource_types)

    @property
    def resource_types(self) -> tuple[str, ...]:
        """Shareable resource kinds wired by this deployment."""
        return tuple(sorted(self._owner_resolvers))

    @staticmethod
    def _require_grantable_permission(
        resource_type: str,
        permission: SharePermission,
    ) -> None:
        """Reject a grant level unsupported by the target resource kind."""
        allowed = share_permissions_for_resource(resource_type)
        if permission in allowed:
            return
        allowed_values = ", ".join(f"'{item.value}'" for item in allowed)
        raise ShareValidationError(
            "Permission is invalid for this resource type; "
            f"allowed values are {allowed_values}"
        )

    async def _require_owner(
        self, principal: "Principal", resource_type: str, resource_id: str
    ) -> uuid.UUID:
        self._require_supported_backend(resource_type)
        resolver = self._owner_resolvers.get(resource_type)
        if resolver is None or resource_type not in SHAREABLE_RESOURCE_TYPES:
            raise ShareValidationError("Unknown resource type: " + resource_type)
        owner_user_id = await resolver(principal.tenant_id, resource_id)
        if owner_user_id is None or owner_user_id != principal.user_id:
            raise ShareNotAllowed()
        return owner_user_id

    async def grant(
        self,
        principal: "Principal",
        *,
        resource_type: str,
        resource_id: str,
        invitees: Sequence[tuple[uuid.UUID, SharePermission]],
    ) -> tuple[ShareRecord, ...]:
        """Create a fully validated all-or-nothing invite batch."""
        owner_user_id = await self._require_owner(
            principal, resource_type, resource_id
        )
        if not invitees:
            raise ShareValidationError("At least one invitation is required")
        recipient_ids = [recipient for recipient, _permission in invitees]
        if len(recipient_ids) != len(set(recipient_ids)):
            raise ShareValidationError("Recipients must not be duplicated")

        for recipient_user_id, permission in invitees:
            self._require_grantable_permission(resource_type, permission)
            if recipient_user_id == owner_user_id:
                raise ShareValidationError("The owner does not need a share")

        if self._restrict_to_members:
            allowed = await self._permissions.share_workspace_filter(
                tenant_id=principal.tenant_id,
                grantor_user_id=owner_user_id,
                candidate_user_ids=tuple(recipient_ids),
            )
            if allowed != set(recipient_ids):
                for denied in sorted(set(recipient_ids) - allowed, key=str):
                    await self._deny_share(
                        principal, resource_type, resource_id, denied
                    )
                raise ShareNotAllowed()

        for recipient_user_id in recipient_ids:
            if not await self._user_lookup(
                principal.tenant_id, recipient_user_id
            ):
                raise ShareValidationError(
                    "User not found: " + str(recipient_user_id)
                )

        created = await self._shares.create_shares(
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            owner_user_id=owner_user_id,
            granted_by_user_id=principal.user_id,
            invitees=tuple(invitees),
            restrict_to_members=self._restrict_to_members,
        )
        if not getattr(self._shares, "atomic_share_effects", False):
            for record in created:
                await self._audit_event(
                    principal,
                    "share.granted",
                    resource_type,
                    resource_id,
                    {
                        "recipient_user_id": str(record.recipient_user_id),
                        "permission": record.permission.value,
                    },
                )
            await self._invalidate(
                tenant_id=principal.tenant_id,
                owner_user_id=owner_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                additional_targets=tuple(recipient_ids),
            )
        return created

    async def update_permission(
        self,
        principal: "Principal",
        *,
        share_id: str,
        permission: SharePermission,
        expected_revision: int,
    ) -> ShareRecord:
        """Change an active share without resetting recipient consent."""
        if expected_revision < 1:
            raise ShareValidationError("expected_revision must be positive")
        pointer = await self._shares.get_share(
            tenant_id=principal.tenant_id, share_id=share_id
        )
        if pointer is None:
            raise ShareNotAllowed()
        owner_user_id = await self._require_owner(
            principal, pointer.resource_type, pointer.resource_id
        )
        self._require_grantable_permission(pointer.resource_type, permission)
        updated = await self._shares.update_share_permission(
            tenant_id=principal.tenant_id,
            share_id=share_id,
            permission=permission,
            expected_revision=expected_revision,
            actor_user_id=principal.user_id,
            restrict_to_members=self._restrict_to_members,
        )
        if updated is None:
            raise ShareNotAllowed()
        if not getattr(self._shares, "atomic_share_effects", False):
            await self._audit_event(
                principal,
                "share.permission_updated",
                updated.resource_type,
                updated.resource_id,
                {
                    "recipient_user_id": str(updated.recipient_user_id),
                    "permission": updated.permission.value,
                    "revision": str(updated.revision),
                },
            )
            await self._invalidate(
                tenant_id=principal.tenant_id,
                owner_user_id=owner_user_id,
                resource_type=updated.resource_type,
                resource_id=updated.resource_id,
                additional_targets=(updated.recipient_user_id,),
            )
        return updated

    async def accept(
        self, principal: "Principal", *, share_id: str
    ) -> ShareRecord | None:
        """Accept a pending share; an accepted own share is idempotent."""
        pointer = await self._shares.get_share(
            tenant_id=principal.tenant_id, share_id=share_id
        )
        if pointer is None or pointer.recipient_user_id != principal.user_id:
            return None
        if pointer.permission not in share_permissions_for_resource(
            pointer.resource_type
        ):
            return None
        owner_user_id = await self._resolve_owner(
            principal.tenant_id, pointer.resource_type, pointer.resource_id
        )
        if owner_user_id is None:
            return None
        if self._restrict_to_members and not await self._permissions.share_workspace(
            tenant_id=principal.tenant_id,
            user_id_a=owner_user_id,
            user_id_b=pointer.recipient_user_id,
        ):
            return None
        accepted = await self._shares.accept_share_by_id(
            tenant_id=principal.tenant_id,
            share_id=share_id,
            recipient_user_id=principal.user_id,
            owner_user_id=owner_user_id,
            restrict_to_members=self._restrict_to_members,
        )
        if accepted is None:
            return None
        if pointer.accepted_at is None and not getattr(
            self._shares, "atomic_share_effects", False
        ):
            await self._audit_event(
                principal,
                "share.accepted",
                accepted.resource_type,
                accepted.resource_id,
                {"owner_user_id": str(owner_user_id)},
            )
            await self._invalidate(
                tenant_id=principal.tenant_id,
                owner_user_id=owner_user_id,
                resource_type=accepted.resource_type,
                resource_id=accepted.resource_id,
                additional_targets=(accepted.recipient_user_id,),
            )
        return accepted

    async def remove(
        self, principal: "Principal", *, share_id: str
    ) -> ShareRemovalResult | None:
        """Owner revoke or recipient decline/leave through one operation."""
        share = await self._shares.get_share(
            tenant_id=principal.tenant_id, share_id=share_id
        )
        if share is None:
            return None
        owner_user_id = await self._resolve_owner(
            principal.tenant_id, share.resource_type, share.resource_id
        )
        if principal.user_id == owner_user_id:
            action = ShareRemoval.REVOKED
        elif principal.user_id == share.recipient_user_id:
            action = (
                ShareRemoval.DECLINED
                if share.accepted_at is None
                else ShareRemoval.LEFT
            )
        else:
            return None
        removed = await self._shares.revoke_share_by_id(
            tenant_id=principal.tenant_id,
            share_id=share_id,
            revoked_by_user_id=principal.user_id,
            owner_user_id=owner_user_id,
        )
        if removed is None:
            return None
        if not getattr(self._shares, "atomic_share_effects", False):
            await self._audit_event(
                principal,
                f"share.{action.value}",
                removed.resource_type,
                removed.resource_id,
                {"recipient_user_id": str(removed.recipient_user_id)},
            )
            await self._invalidate(
                tenant_id=principal.tenant_id,
                owner_user_id=owner_user_id,
                resource_type=removed.resource_type,
                resource_id=removed.resource_id,
                additional_targets=(removed.recipient_user_id,),
            )
        return ShareRemovalResult(record=removed, action=action)

    async def list_for_resource(
        self, principal: "Principal", *, resource_type: str, resource_id: str
    ) -> tuple[ShareRecord, ...]:
        """List recipients; only the resource owner may manage sharing."""
        await self._require_owner(principal, resource_type, resource_id)
        return await self._shares.list_shares_for_resource(
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    async def inbox(self, principal: "Principal") -> tuple[InboxItem, ...]:
        """Return title-enriched pending and accepted lifecycle items."""
        records = await self._shares.inbox_for_recipient(
            tenant_id=principal.tenant_id,
            recipient_user_id=principal.user_id,
        )
        items: list[InboxItem] = []
        for record in records:
            if record.permission not in share_permissions_for_resource(
                record.resource_type
            ):
                continue
            title = await self._resolve_title(
                principal.tenant_id, record.resource_type, record.resource_id
            )
            if title is None:
                continue
            items.append(
                InboxItem(
                    share_id=record.id,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    resource_title=title,
                    permission=record.permission,
                    revision=record.revision,
                    granted_by_user_id=record.granted_by_user_id,
                    created_at=record.created_at,
                    accepted_at=record.accepted_at,
                )
            )
        return tuple(items)

    async def mine(self, principal: "Principal") -> tuple[OutgoingItem, ...]:
        """Return owner lifecycle rows grouped per resource."""
        if principal.user_id is None:
            return ()
        records = await self._shares.list_active_shares(
            tenant_id=principal.tenant_id
        )
        grouped: dict[tuple[str, str], list[ShareRecord]] = {}
        ids_by_type: dict[str, list[str]] = {}
        for record in records:
            key = (record.resource_type, record.resource_id)
            if key not in grouped:
                ids_by_type.setdefault(record.resource_type, []).append(
                    record.resource_id
                )
            grouped.setdefault(key, []).append(record)
        # One read per resource type, however many resources are shared;
        # ownership still comes from the resource, never from the share row.
        facts_by_type: dict[str, Mapping[str, ShareFacts]] = {}
        for resource_type, resource_ids in ids_by_type.items():
            self._require_supported_backend(resource_type)
            resolver = self._share_facts_resolvers.get(resource_type)
            facts_by_type[resource_type] = (
                await resolver(principal.tenant_id, resource_ids)
                if resolver is not None
                else {}
            )
        items: list[OutgoingItem] = []
        for (resource_type, resource_id), group in grouped.items():
            facts = facts_by_type[resource_type].get(resource_id)
            if (
                facts is None
                or facts.owner_user_id != principal.user_id
                or facts.title is None
            ):
                continue
            title = facts.title
            items.append(
                OutgoingItem(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_title=title,
                    share_count=len(group),
                    pending_count=sum(row.accepted_at is None for row in group),
                )
            )
        return tuple(items)

    async def accepted_for_recipient(
        self, principal: "Principal", *, resource_type: str
    ) -> Mapping[str, ShareRecord]:
        """Internal list-query seam; never exposed as a share HTTP route."""
        if (
            resource_type not in SHAREABLE_RESOURCE_TYPES
            or resource_type in self._unsupported_resource_types
        ):
            return {}
        records = await self._shares.shares_for_recipient(
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            recipient_user_id=principal.user_id,
        )
        allowed = share_permissions_for_resource(resource_type)
        return {
            resource_id: record
            for resource_id, record in records.items()
            if record.permission in allowed
        }

    async def _resolve_owner(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> uuid.UUID | None:
        self._require_supported_backend(resource_type)
        resolver = self._owner_resolvers.get(resource_type)
        if resolver is None:
            return None
        return await resolver(tenant_id, resource_id)

    async def _resolve_title(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> str | None:
        self._require_supported_backend(resource_type)
        resolver = self._title_resolvers.get(resource_type)
        if resolver is None:
            return None
        return await resolver(tenant_id, resource_id)

    def _require_supported_backend(self, resource_type: str) -> None:
        """Reject known resource kinds whose store cannot transact sharing."""
        if resource_type in self._unsupported_resource_types:
            raise ShareBackendUnsupported(
                f"Sharing is unsupported for {resource_type} in this deployment"
            )

    async def _deny_share(
        self,
        principal: "Principal",
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> None:
        log_authorization_denial(
            log,
            action="share",
            principal_kind=principal.kind,
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        await self._audit_event(
            principal,
            "share.denied",
            resource_type,
            resource_id,
            {
                "recipient_user_id": str(recipient_user_id),
                "reason": "not_workspace_member",
            },
        )

    async def _audit_event(
        self,
        principal: "Principal",
        action: str,
        resource_type: str,
        resource_id: str,
        detail: dict[str, str],
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            AuditEntry(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
            )
        )

    async def _invalidate(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        resource_type: str,
        resource_id: str,
        additional_targets: Sequence[uuid.UUID] = (),
    ) -> None:
        """Publish one memory-backend sharing invalidation fan-out."""
        if self._invalidator is None:
            return
        await self._invalidator.invalidate(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            scope="sharing",
            additional_targets=additional_targets,
        )
