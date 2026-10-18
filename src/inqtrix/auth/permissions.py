"""Permission chokepoint: workspace membership, shares, and visibility.

This module is the single authority for authorization decisions:
routers and services ask :class:`AuthorizationService`; nothing else combines
membership and direct-share facts. Postgres row-level security is a
coarse tenant defense layer underneath, never the fine-grained truth.

Two deliberate behavioural anchors:

* **Legacy principals are unscoped.** The ``anonymous`` and ``static``
  principals (open server / static Bearer key) predate multi-user
  operation; for them every visibility question resolves to "no
  filtering" so existing single-tenant deployments behave
  bit-for-bit. Scoping activates only for ``oidc_session``/``pat``
  principals.
* **Denied is indistinguishable from absent.** Failed access checks
  raise not-found errors, never 403 — resource existence is itself
  information (OWASP guidance). Every denial is still loudly visible to
  operators via ``log.warning`` and the audit sink.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.principal import Principal, UserContext

log = logging.getLogger("inqtrix")


class WorkspaceRole(StrEnum):
    """Ordered coarse role of a member within one workspace.

    The order is total and ascending — every later role implies every
    earlier one. Comparisons go through :meth:`at_least` instead of
    enum identity so adding a role between two existing ones stays a
    one-line change.
    """

    VIEWER = "viewer"
    COMMENTER = "commenter"
    EDITOR = "editor"
    OWNER = "owner"

    def at_least(self, minimum: "WorkspaceRole") -> bool:
        """Whether this role grants at least *minimum*'s privileges."""
        return _WORKSPACE_ROLE_RANK[self] >= _WORKSPACE_ROLE_RANK[minimum]


_WORKSPACE_ROLE_RANK: dict[WorkspaceRole, int] = {
    role: rank for rank, role in enumerate(WorkspaceRole)
}


class SharePermission(StrEnum):
    """Permission granted by one direct user share."""

    VIEW = "view"
    SUGGEST = "suggest"
    EDIT = "edit"

    def at_least(self, minimum: "SharePermission") -> bool:
        """Whether this grant satisfies a requirement of *minimum*."""
        return _SHARE_PERMISSION_RANK[self] >= _SHARE_PERMISSION_RANK[minimum]


_SHARE_PERMISSION_RANK: dict[SharePermission, int] = {
    permission: rank for rank, permission in enumerate(SharePermission)
}

_VIEW_EDIT_SHARE_PERMISSIONS = (
    SharePermission.VIEW,
    SharePermission.EDIT,
)
_EDITOR_DOCUMENT_SHARE_PERMISSIONS = (
    SharePermission.VIEW,
    SharePermission.SUGGEST,
    SharePermission.EDIT,
)

SHARE_PERMISSIONS_BY_RESOURCE_TYPE: Mapping[
    str, tuple[SharePermission, ...]
] = MappingProxyType(
    {
        "run": _VIEW_EDIT_SHARE_PERMISSIONS,
        "knowledge_collection": _VIEW_EDIT_SHARE_PERMISSIONS,
        "prompt_template": _VIEW_EDIT_SHARE_PERMISSIONS,
        "skill_template": _VIEW_EDIT_SHARE_PERMISSIONS,
        "editor_document": _EDITOR_DOCUMENT_SHARE_PERMISSIONS,
    }
)
"""Grantable permissions for each resource in the direct-share system.

The tuple order is ascending access strength. Resource-specific validation
uses this policy in the service and persistence layers so adding an enum value
cannot silently widen existing resource contracts.
"""


def share_permissions_for_resource(
    resource_type: str,
) -> tuple[SharePermission, ...]:
    """Return the ordered direct-share permissions for a resource kind.

    Unknown and deliberately non-shareable resource kinds return an empty
    tuple. Ownership checks remain independent, allowing non-shareable owned
    resources such as uploaded files to keep using this authorization service.
    """
    return SHARE_PERMISSIONS_BY_RESOURCE_TYPE.get(resource_type, ())


def share_permissions_satisfying(
    resource_type: str,
    minimum: SharePermission,
) -> tuple[SharePermission, ...]:
    """Return valid grants that satisfy *minimum* for one resource kind.

    A minimum unsupported by the resource returns no grants. For editor
    documents this yields ``view|suggest|edit`` for a view requirement,
    ``suggest|edit`` for a suggest requirement, and ``edit`` for edit.
    """
    allowed = share_permissions_for_resource(resource_type)
    if minimum not in allowed:
        return ()
    return tuple(
        permission
        for permission in allowed
        if permission.at_least(minimum)
    )


class AccessMode(StrEnum):
    """How the current principal may see one resource."""

    UNSCOPED = "unscoped"
    OWNER = "owner"
    SHARED = "shared"


@dataclass(frozen=True)
class ResourceAccess:
    """Authoritative access annotation returned with resource DTOs."""

    mode: AccessMode
    permission: SharePermission | None = None

    def as_dict(self) -> dict[str, str]:
        """Serialize the public ``access`` contract."""
        payload = {"mode": self.mode.value}
        if self.mode is AccessMode.SHARED and self.permission is not None:
            payload["permission"] = self.permission.value
        return payload


def require_owned_access(
    *,
    owner_user_id: uuid.UUID | None,
    resource_tenant_id: str,
    resource_id: str,
    visible_to: UserContext | None,
    not_found: type[KeyError],
) -> ResourceAccess:
    """Authorize a non-shareable owned record.

    Project-local records and uploaded files are not share targets. Scoped
    principals therefore need exact ownership; unscoped modes may see only
    ownerless legacy rows.
    """
    if visible_to is None:
        if owner_user_id is None:
            return ResourceAccess(AccessMode.UNSCOPED)
        raise not_found(resource_id)
    principal = visible_to.principal
    if (
        principal.tenant_id == resource_tenant_id
        and principal.user_id == owner_user_id
    ):
        return ResourceAccess(AccessMode.OWNER)
    raise not_found(resource_id)


class WorkspaceNotFound(KeyError):
    """Raised when a workspace does not exist *for this principal*.

    Deliberately identical for "no such workspace" and "exists but the
    principal is not a member" — callers map it to HTTP 404.
    """


class ResourceNotFound(KeyError):
    """Raised when a resource access check fails for this principal.

    Same hiding rule as :class:`WorkspaceNotFound`: denial and absence
    are indistinguishable to the caller.
    """


class LastWorkspaceOwnerError(RuntimeError):
    """Raised when a membership mutation would remove the final owner."""


@dataclass(frozen=True)
class AuditEntry:
    """One append-only audit fact emitted by the permission layer.

    Attributes:
        tenant_id: Tenant the action happened in.
        actor_user_id: Canonical user that attempted the action.
        action: Verb-like action label (``"authz.denied"`` etc.).
        resource_type: Kind of resource acted on.
        resource_id: Identifier of the resource acted on.
        detail: Small free-form context (requested permission, route).
            Must never contain secrets or full request bodies.
        actor_type: ``user`` (default, every direct principal action) or
            ``agent`` — actions a workspace-agent run performs on the
            owner's behalf (``agent.*`` actions from the agent runtime).
            ``actor_user_id`` carries the effective actor's canonical UUID;
            the column distinguishes who acted, not resource ownership.
        outcome: ``success`` (default) | ``failure`` | ``denied`` —
            OCSF-oriented result of the action (migration 0072).
        origin: Request origin facts (``ip``, ``user_agent``,
            ``auth_method``). Empty for events without a request (worker
            jobs) — absence is meaningful, never fabricated.
        correlation: Join keys into logs and traces (``request_id``,
            ``run_id``, ``trace_id``).
        actor_pseudonym: Stable ``usr_<hex16>`` reference of the actor,
            computed at write time (the identifier logs/traces carry).
        workspace_id: Workspace scope of the action, when one applies.
    """

    tenant_id: str
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str
    detail: dict[str, str] = field(default_factory=dict)
    actor_type: str = "user"
    outcome: str = "success"
    origin: dict[str, str] = field(default_factory=dict)
    correlation: dict[str, str] = field(default_factory=dict)
    actor_pseudonym: str | None = None
    workspace_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        """Derive the actor pseudonym here, not at each writer.

        The read model (migration 0072) shows the actor ONLY as
        ``actor_pseudonym``; a writer that forgets it produces an
        actor-blank row in the admin panel and every export. Deriving
        it from ``actor_user_id`` at construction makes that
        impossible for all writers at once — an explicitly passed
        pseudonym still wins.
        """
        # A denial recorded as a success is worse than no row: the auditor
        # who filters the panel by outcome is told the request went through.
        # Derived here so no writer can get it wrong, the same way the
        # pseudonym is.
        if self.action.endswith(".denied") and self.outcome == "success":
            object.__setattr__(self, "outcome", "denied")
        if self.actor_pseudonym or self.actor_user_id is None:
            return
        from inqtrix.auth.log_redaction import stable_pseudonym

        object.__setattr__(
            self,
            "actor_pseudonym",
            stable_pseudonym("usr", self.actor_user_id),
        )


class MembershipRepository(Protocol):
    """Read port for workspace membership facts."""

    async def workspace_ids_for(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[str, ...]:
        """All workspace ids the canonical user is an active member of."""
        ...

    async def role_in_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, workspace_id: str
    ) -> WorkspaceRole | None:
        """The member's role, or ``None`` when not a member (or no
        such workspace — the two are indistinguishable by design)."""
        ...


class MembershipAdminRepository(Protocol):
    """Write/admin port for workspace and membership management.

    The instance-admin surface (create/list/rename/delete workspaces, assign
    and remove members) builds on this. It is deliberately separate from the
    read-only :class:`MembershipRepository` the permission chokepoint consumes:
    administration is the platform-admin axis, membership resolution is the
    per-request authorization path. Both the memory and Postgres identity
    backends implement it, so the admin router is wired identically in both
    modes (Baukasten).
    """

    async def create_workspace(
        self, *, tenant_id: str, name: str, created_by_user_id: uuid.UUID
    ) -> tuple[str, str]:
        """Create one workspace with *created_by_user_id* as its OWNER.

        Returns ``(workspace_id, name)``.
        """
        ...

    async def list_all_workspaces(
        self, *, tenant_id: str
    ) -> tuple[tuple[str, str, uuid.UUID, int], ...]:
        """Every workspace in the tenant as ``(id, name, created_by_user_id,
        member_count)``, name-sorted (the admin overview)."""
        ...

    async def rename_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        name: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Rename one workspace; ``False`` when it does not exist."""
        ...

    async def delete_workspace(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Delete one workspace and its memberships (cascade); ``False`` when
        it does not exist."""
        ...

    async def list_members(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[tuple[uuid.UUID, WorkspaceRole], ...] | None:
        """``(user_id, role)`` per member, UUID-sorted, or ``None`` when the
        workspace does not exist — distinct from an existing workspace whose
        membership is empty, so the router can answer 404 vs 200."""
        ...

    async def assign_member(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        role: WorkspaceRole,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Add a member or change an existing member's role (upsert);
        ``False`` when the workspace does not exist.

        Unlike invitation acceptance (never-downgrade), the admin sets the
        EXACT role — raising or lowering it — because positioning users is a
        deliberate administrative act.
        """
        ...

    async def set_existing_member_role(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        role: WorkspaceRole,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Set the exact role only when the membership already exists.

        The workspace and membership check belong to the same mutation
        transaction. ``False`` therefore means that either the workspace or
        the membership was absent at the command's serialization point; the
        command never inserts a row.
        """
        ...

    async def remove_member(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
    ) -> bool:
        """Remove one membership; ``False`` when the user is not a member."""
        ...


class ShareRepository(Protocol):
    """Read port for direct user-share grants on resources."""

    async def permission_for(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        recipient_user_id: uuid.UUID,
    ) -> SharePermission | None:
        """Accepted direct grant for the recipient, if one exists."""
        ...


class AuditSink(Protocol):
    """Write port for the append-only audit log."""

    async def record(self, entry: AuditEntry) -> None:
        """Persist one audit fact. Implementations must be append-only."""
        ...


_UNSCOPED_PRINCIPAL_KINDS = frozenset({"anonymous", "static"})


class AuthorizationService:
    """Single live authority for workspace and resource decisions.

    Workspace roles authorize workspace administration only. Resource access
    comes exclusively from the resource owner or one accepted direct share;
    the UI namespace called ``workspace_id`` never implies resource access.

    Args:
        members: Membership read port.
        shares: Share grant read port.
        audit: Append-only audit sink; receives every denial.
    """

    def __init__(
        self,
        *,
        members: MembershipRepository,
        shares: ShareRepository,
        audit: AuditSink,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
    ) -> None:
        self._members = members
        self._shares = shares
        self._audit = audit
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled

    async def authorization_generation(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> int | None:
        """Commit-ordered per-user generation, or None when unsupported.

        None means the membership backend has no generation (memory mode,
        api-key principals): frame gates then fall back to the full
        authorization chain on every frame — today's behavior, never a
        silently weaker check.
        """
        reader = getattr(self._members, "authorization_generation", None)
        if reader is None:
            return None
        return await reader(tenant_id=tenant_id, user_id=user_id)

    @property
    def audit_sink(self) -> AuditSink:
        """The append-only audit sink behind this service.

        Exposed so administrative surfaces (pseudonym resolution, the
        audit read API) write through the SAME sink instead of wiring a
        second one — one write path per deployment.
        """
        return self._audit

    async def resolve_user_context(
        self, principal: Principal
    ) -> UserContext | None:
        """Resolve server-side membership facts for a principal.

        Returns:
            ``None`` for the legacy ``anonymous``/``static`` principals
            — the explicit "no visibility filtering" marker that keeps
            single-tenant deployments bit-for-bit. For scoped
            principals, a :class:`UserContext` with memberships
            resolved from the repositories (never from client input).
        """
        if principal.kind in _UNSCOPED_PRINCIPAL_KINDS:
            return None
        if principal.user_id is None:
            raise ValueError("Scoped principal is missing canonical user_id")
        workspace_ids = await self._members.workspace_ids_for(
            tenant_id=principal.tenant_id, user_id=principal.user_id
        )
        return UserContext(
            principal=principal,
            workspace_ids=workspace_ids,
        )

    async def resolve_workspace(
        self,
        principal: Principal,
        workspace_id: str,
        *,
        min_role: WorkspaceRole = WorkspaceRole.VIEWER,
    ) -> str:
        """Validate that the principal may act in *workspace_id*.

        Unscoped legacy principals pass unconditionally (single-tenant
        owner semantics).

        Returns:
            The validated workspace id.

        Raises:
            WorkspaceNotFound: When the workspace does not exist, the
                principal is not a member, or the member's role is
                below *min_role* — indistinguishably.
        """
        if principal.kind in _UNSCOPED_PRINCIPAL_KINDS:
            return workspace_id
        if principal.user_id is None:
            raise WorkspaceNotFound(workspace_id)
        role = await self._members.role_in_workspace(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        if role is None or not role.at_least(min_role):
            await self._deny(
                principal,
                resource_type="workspace",
                resource_id=workspace_id,
                detail={"min_role": min_role.value, "role": getattr(role, "value", "")},
            )
            raise WorkspaceNotFound(workspace_id)
        return workspace_id

    async def resolve_resource_access(
        self,
        principal: Principal,
        *,
        owner_user_id: uuid.UUID | None,
        resource_tenant_id: str,
        resource_type: str,
        resource_id: str,
        minimum: SharePermission = SharePermission.VIEW,
    ) -> ResourceAccess | None:
        """Resolve current access without accepting caller-cached grants.

        Ownerless legacy rows are visible only to anonymous/static modes. A
        scoped principal therefore cannot acquire tenant-wide access merely
        because old data lacks an owner.
        """
        if resource_tenant_id != principal.tenant_id:
            return None
        if principal.kind in _UNSCOPED_PRINCIPAL_KINDS:
            if owner_user_id is None:
                return ResourceAccess(AccessMode.UNSCOPED)
            return None
        if principal.user_id is None:
            return None
        if owner_user_id is None:
            return None
        if owner_user_id == principal.user_id:
            return ResourceAccess(AccessMode.OWNER)
        if not self._sharing_enabled:
            return None
        satisfying_permissions = share_permissions_satisfying(
            resource_type,
            minimum,
        )
        shared = await self._shares.permission_for(
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            recipient_user_id=principal.user_id,
        )
        if shared not in satisfying_permissions:
            return None
        if self._restrict_to_workspace_members and not await self.share_workspace(
            tenant_id=principal.tenant_id,
            user_id_a=owner_user_id,
            user_id_b=principal.user_id,
        ):
            return None
        return ResourceAccess(AccessMode.SHARED, shared)

    async def can(
        self,
        principal: Principal,
        permission: SharePermission,
        *,
        owner_user_id: uuid.UUID | None,
        resource_tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Whether the live owner/share state grants *permission*."""
        return (
            await self.resolve_resource_access(
                principal,
                owner_user_id=owner_user_id,
                resource_tenant_id=resource_tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                minimum=permission,
            )
            is not None
        )

    async def share_workspace_filter(
        self,
        *,
        tenant_id: str,
        grantor_user_id: uuid.UUID,
        candidate_user_ids: Sequence[uuid.UUID],
    ) -> set[uuid.UUID]:
        """The subset of candidate user ids sharing a workspace with the
        grantor — the batch primitive behind workspace-scoped sharing.

        The grantor's workspace set is read ONCE (it is loop-invariant), then
        each candidate's memberships are intersected against it. A grantor
        with no workspace shares one with nobody, so the result is empty.
        Reads only the membership port — no change to the ``can``/share read
        semantics. :meth:`share_workspace` is the single-candidate convenience.
        """
        grantor_workspaces = set(
            await self._members.workspace_ids_for(
                tenant_id=tenant_id, user_id=grantor_user_id
            )
        )
        if not grantor_workspaces:
            return set()
        allowed: set[uuid.UUID] = set()
        for user_id in candidate_user_ids:
            candidate_workspaces = await self._members.workspace_ids_for(
                tenant_id=tenant_id, user_id=user_id
            )
            if any(
                workspace_id in grantor_workspaces
                for workspace_id in candidate_workspaces
            ):
                allowed.add(user_id)
        return allowed

    async def share_workspace(
        self, *, tenant_id: str, user_id_a: uuid.UUID, user_id_b: uuid.UUID
    ) -> bool:
        """Whether two users share at least one workspace.

        The single-candidate membership-boundary predicate for workspace-
        scoped sharing: a grant is permitted only between co-members.
        Delegates to :meth:`share_workspace_filter` so the rule lives once.
        """
        return bool(
            await self.share_workspace_filter(
                tenant_id=tenant_id,
                grantor_user_id=user_id_a,
                candidate_user_ids=(user_id_b,),
            )
        )

    async def require(
        self,
        principal: Principal,
        permission: SharePermission,
        *,
        owner_user_id: uuid.UUID | None,
        resource_tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> ResourceAccess:
        """Assert *permission* or raise :class:`ResourceNotFound`.

        Denials are audited and logged before raising so authorization
        failures are operator-visible even though the client sees an
        indistinct 404.
        """
        access = await self.resolve_resource_access(
            principal,
            owner_user_id=owner_user_id,
            resource_tenant_id=resource_tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            minimum=permission,
        )
        if access is None:
            await self._deny(
                principal,
                resource_type=resource_type,
                resource_id=resource_id,
                detail={"permission": permission.value},
            )
            raise ResourceNotFound(resource_id)
        return access

    async def _deny(
        self,
        principal: Principal,
        *,
        resource_type: str,
        resource_id: str,
        detail: dict[str, str],
    ) -> None:
        """Make one denial visible in the log and the audit trail."""
        log_authorization_denial(
            log,
            action="require",
            principal_kind=principal.kind,
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        # Local import: audit_service imports AuditEntry from this module.
        from inqtrix.services.audit_service import build_audit_entry

        await self._audit.record(
            build_audit_entry(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="authz.denied",
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                outcome="denied",
            )
        )
