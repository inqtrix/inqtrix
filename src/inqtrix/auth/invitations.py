"""Invitation-gated registration for the oidc mode.

With ``INQTRIX_REGISTRATION=invite`` an IdP-authenticated stranger is
NOT enough: a first-time login is admitted only when an open
invitation matches the login email, and the acceptance simultaneously
grants the invited workspace membership. The check runs at the OIDC
callback BEFORE any user record or session exists — a rejected
stranger leaves no trace beyond the audit entry.

Semantics (binding design decisions):

* Acceptance matches ALL open, unexpired, unrevoked invitations for
  ``lower(email)`` and consumes each exactly once (guarded UPDATE in
  the Postgres backend — replay-safe across replicas); each
  acceptance creates the workspace membership in the SAME
  transaction.
* EXISTING users always pass admission (they registered under an
  earlier policy or invitation) — and still collect newly opened
  invitations on their next login, which covers "invited after
  registration" without a separate accept endpoint.
* A disabled user (``users.disabled_at``) is denied in every
  registration mode.
* ``invite`` mode requires the postgres storage backend: memory
  invitations would evaporate on restart and lock everyone out. The
  settings bridge rejects the combination at startup.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

from inqtrix.auth.permissions import WorkspaceRole

if TYPE_CHECKING:
    from inqtrix.auth.directory import UserLookup
    from inqtrix.auth.identity_memory import MemoryIdentityStore
    from inqtrix.auth.permissions import AuditSink

log = logging.getLogger("inqtrix")

DEFAULT_INVITATION_TTL_DAYS = 14
"""Default invitation lifetime. Two weeks bridges vacations without
leaving admission doors open for quarters; per-invitation overrides
remain possible at creation."""


@dataclass(frozen=True)
class Invitation:
    """One invitation row.

    Attributes:
        id: Server-assigned identifier.
        tenant_id: Tenant scope.
        workspace_id: Workspace the acceptance joins.
        email: Invited address; matching is case-insensitive, and the
            address is an ADMISSION key only — identity stays the
            ``(issuer, subject)`` anchor after the first login.
        role: Membership role granted on acceptance.
        invited_by_user_id: Inviting member (audit).
        created_at: Unix seconds.
        expires_at: Absolute expiry (unix seconds).
        accepted_at: One-time consumption timestamp.
        accepted_by_user_id: Subject that consumed the invitation.
        revoked_at: Soft-revocation timestamp.
    """

    id: str
    tenant_id: str
    workspace_id: str
    email: str
    role: WorkspaceRole
    invited_by_user_id: uuid.UUID
    created_at: float
    expires_at: float
    accepted_at: float | None = None
    accepted_by_user_id: uuid.UUID | None = None
    revoked_at: float | None = None


class DuplicateOpenInvitation(Exception):
    """Raised when an open invitation for the email already exists."""


class RegistrationDenied(Exception):
    """Raised when admission policy rejects the login."""


class InvitationRepository(Protocol):
    """Persistence port for invitations."""

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
        """Insert one open invitation.

        Raises:
            DuplicateOpenInvitation: When an open invitation for the
                ``(workspace, lower(email))`` pair already exists.
        """
        ...

    async def list_for_workspace(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[Invitation, ...]:
        """Every invitation of the workspace, newest first."""
        ...

    async def revoke(
        self, *, tenant_id: str, workspace_id: str, invitation_id: str,
        now: float,
    ) -> bool:
        """Guarded soft-revoke of an OPEN invitation."""
        ...

    async def has_open_for_email(
        self, *, tenant_id: str, email: str, now: float
    ) -> bool:
        """Whether an open, unexpired invitation can admit this email."""
        ...

    async def accept_open_for_email(
        self, *, tenant_id: str, email: str, user_id: uuid.UUID, now: float
    ) -> tuple[Invitation, ...]:
        """Consume every open invitation matching the email.

        Each acceptance flips the row exactly once (guarded) AND
        creates the workspace membership atomically with it; existing
        memberships are never downgraded.
        """
        ...


class MemoryInvitationStore:
    """Process-local invitations for unit tests and demos.

    Takes the identity store so accepted memberships land in the SAME
    instance the permission layer reads — a second membership map
    would be a split brain.
    """

    def __init__(self, identity: "MemoryIdentityStore") -> None:
        self._identity = identity
        self._invitations: dict[str, Invitation] = {}
        self._lock = threading.RLock()

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
        with self._lock:
            for invitation in self._invitations.values():
                if (
                    invitation.workspace_id == workspace_id
                    and invitation.email.lower() == email.lower()
                    and invitation.accepted_at is None
                    and invitation.revoked_at is None
                ):
                    raise DuplicateOpenInvitation(email)
            invitation = Invitation(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                email=email,
                role=role,
                invited_by_user_id=invited_by_user_id,
                created_at=time.time(),
                expires_at=expires_at,
            )
            self._invitations[invitation.id] = invitation
            return invitation

    async def list_for_workspace(
        self, *, tenant_id: str, workspace_id: str
    ) -> tuple[Invitation, ...]:
        with self._lock:
            rows = [
                invitation
                for invitation in self._invitations.values()
                if invitation.tenant_id == tenant_id
                and invitation.workspace_id == workspace_id
            ]
        return tuple(
            sorted(rows, key=lambda row: row.created_at, reverse=True)
        )

    async def revoke(
        self, *, tenant_id: str, workspace_id: str, invitation_id: str,
        now: float,
    ) -> bool:
        with self._lock:
            invitation = self._invitations.get(invitation_id)
            if (
                invitation is None
                or invitation.tenant_id != tenant_id
                or invitation.workspace_id != workspace_id
                or invitation.accepted_at is not None
                or invitation.revoked_at is not None
            ):
                return False
            self._invitations[invitation_id] = replace(
                invitation, revoked_at=now
            )
            return True

    async def accept_open_for_email(
        self, *, tenant_id: str, email: str, user_id: uuid.UUID, now: float
    ) -> tuple[Invitation, ...]:
        accepted: list[Invitation] = []
        with self._lock, self._identity._lock:
            invitation_snapshot = dict(self._invitations)
            membership_snapshot = dict(self._identity._members)
            try:
                for invitation_id, invitation in list(self._invitations.items()):
                    if (
                        invitation.tenant_id == tenant_id
                        and invitation.email.lower() == email.lower()
                        and invitation.accepted_at is None
                        and invitation.revoked_at is None
                        and invitation.expires_at > now
                    ):
                        consumed = replace(
                            invitation,
                            accepted_at=now,
                            accepted_by_user_id=user_id,
                        )
                        self._invitations[invitation_id] = consumed
                        accepted.append(consumed)
                for invitation in accepted:
                    # Never downgrade an existing membership; mirrors the
                    # Postgres backend's on_conflict_do_nothing semantics.
                    existing_role = await self._identity.role_in_workspace(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=invitation.workspace_id,
                    )
                    if existing_role is None:
                        self._identity.add_member(
                            invitation.workspace_id, user_id, invitation.role
                        )
            except BaseException:
                self._invitations.clear()
                self._invitations.update(invitation_snapshot)
                self._identity._members.clear()
                self._identity._members.update(membership_snapshot)
                raise
        return tuple(accepted)

    async def has_open_for_email(
        self, *, tenant_id: str, email: str, now: float
    ) -> bool:
        with self._lock:
            return any(
                invitation.tenant_id == tenant_id
                and invitation.email.lower() == email.lower()
                and invitation.accepted_at is None
                and invitation.revoked_at is None
                and invitation.expires_at > now
                for invitation in self._invitations.values()
            )


class RegistrationGate:
    """Admission decision at the OIDC callback.

    Args:
        invitations: Invitation repository (acceptance side effects).
        users: Narrow mirror lookup — existing users always pass.
        registration: Active policy (``open`` keeps the historical
            admit-everyone behaviour).
        audit: Optional audit sink for denials.
    """

    def __init__(
        self,
        *,
        invitations: "InvitationRepository | None",
        users: "UserLookup",
        registration: Literal["open", "invite"],
        audit: "AuditSink | None" = None,
    ) -> None:
        if registration == "invite" and invitations is None:
            raise ValueError(
                "RegistrationGate in invite mode requires an "
                "InvitationRepository."
            )
        self._invitations = invitations
        self._users = users
        self._registration = registration
        self._audit = audit

    @property
    def registration(self) -> Literal["open", "invite"]:
        """Active admission policy for the atomic lifecycle re-check."""
        return self._registration

    async def admit(
        self, *, tenant_id: str, issuer: str, sub: str, email: str
    ) -> tuple[Invitation, ...]:
        """Admit one login; returns the invitations it consumed.

        Raises:
            RegistrationDenied: Disabled user (every mode), or
                first-time login without a matching open invitation
                (``invite`` mode). The message is deliberately generic
                — it must not confirm whether an invitation exists or
                existed for the address.
        """
        existing = await self._users.find_user(
            tenant_id=tenant_id, issuer=issuer, subject=sub
        )
        if existing is not None and existing.disabled_at is not None:
            await self._deny(tenant_id, sub, "disabled")
            raise RegistrationDenied("Account is disabled.")
        has_invitation = bool(
            self._invitations is not None
            and email
            and await self._invitations.has_open_for_email(
                tenant_id=tenant_id, email=email, now=time.time()
            )
        )
        if (
            self._registration == "invite"
            and existing is None
            and not has_invitation
        ):
            await self._deny(tenant_id, sub, "no_invitation")
            raise RegistrationDenied(
                "Registration requires an invitation. Contact the "
                "administrator."
            )
        return ()

    async def accept(
        self, *, tenant_id: str, email: str, user_id: uuid.UUID
    ) -> tuple[Invitation, ...]:
        """Consume invitations only after the canonical user exists."""
        if self._invitations is None or not email:
            return ()
        accepted = await self._invitations.accept_open_for_email(
            tenant_id=tenant_id,
            email=email,
            user_id=user_id,
            now=time.time(),
        )
        for invitation in accepted:
            await self._audit_event(
                tenant_id,
                user_id,
                "invitation.accepted",
                invitation.id,
            )
        return accepted

    async def _deny(self, tenant_id: str, sub: str, reason: str) -> None:
        del sub
        log.warning("Registration rejected: kind=oidc_session (%s).", reason)
        await self._audit_event(
            tenant_id, None, "registration.denied", reason
        )

    async def _audit_event(
        self,
        tenant_id: str,
        user_id: uuid.UUID | None,
        action: str,
        resource_id: str,
    ) -> None:
        if self._audit is None:
            return
        from inqtrix.auth.permissions import AuditEntry

        await self._audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                actor_user_id=user_id,
                action=action,
                resource_type="registration",
                resource_id=resource_id,
                detail={},
            )
        )
