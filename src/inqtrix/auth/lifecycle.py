"""Atomic commands for canonical-user lifecycle changes.

Authentication transports prove an external identity; this service is the
single application boundary that turns that proof into durable Inqtrix state.
Creating/updating the user, granting the initial admin role, accepting pending
invitations, and creating the browser session are therefore one command rather
than a sequence of independently committed repository calls.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Protocol

from inqtrix.auth.invitations import RegistrationDenied
from inqtrix.auth.permissions import AuditEntry
from inqtrix.auth.provisioning import apply_admin_grant

if TYPE_CHECKING:
    from inqtrix.auth.credentials import CredentialStore, LocalCredential
    from inqtrix.auth.directory import MirroredUser, UserDirectory
    from inqtrix.auth.invitations import InvitationRepository
    from inqtrix.auth.pat import PatService
    from inqtrix.auth.sessions import AuthSession, SessionStore


class UserLifecycleStatus(StrEnum):
    """Outcome of an administrative lifecycle mutation."""

    UPDATED = "updated"
    NOT_FOUND = "not_found"
    LAST_ADMIN = "last_admin"
    #: The command cannot run yet; the payload names the reasons.
    REFUSED = "refused"
    #: The account is being erased; it cannot be changed any more.
    ERASING = "erasing"


class UserDisabledError(RuntimeError):
    """Raised when a disabled external binding attempts to log in."""


class AdminAuthorizationError(RuntimeError):
    """Raised when an admin command's effective actor is no longer authorized.

    Request guards are intentionally only an early rejection.  Every command
    that mutates another user's administrative state repeats the active-admin
    check inside its atomic authority boundary so a concurrent demotion or
    disable cannot race a previously admitted request.
    """


@dataclass(frozen=True)
class LoginCommand:
    """Verified identity facts and the browser session to establish."""

    tenant_id: str
    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str | None
    session: "AuthSession"
    is_admin: bool = False
    first_login_owner: bool = False
    invitation_required: bool = False


class UserLifecycleTransaction(Protocol):
    """Durable backend whose methods each commit exactly one transaction."""

    async def provision_login(self, command: LoginCommand) -> "MirroredUser":
        """Provision a login and its session atomically."""
        ...

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
        """Create mirror, credential, role, and optional session atomically."""
        ...

    async def set_role(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        role: Literal["admin", "user"],
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Apply an instance-role change under the tenant security lock."""
        ...

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Apply disable state and the complete cut-off cascade atomically."""
        ...

    async def reset_local_password(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        password_hash: str,
        actor_user_id: uuid.UUID,
    ) -> bool:
        """Replace a local password and purge browser sessions atomically."""
        ...


@dataclass(frozen=True)
class MemoryLifecycleInvalidation:
    """One content-free lifecycle invalidation retained by the memory port."""

    tenant_id: str
    target_user_id: uuid.UUID
    scope: str
    resource_type: str
    resource_id: str


class MemoryUserLifecycleTransaction:
    """Atomic lifecycle commands across the process-local auth stores.

    Every participating store lock is acquired before the first write and the
    complete in-memory state is snapshotted. Readers therefore see either the
    state before the command or the state after it; an exception restores every
    participating store before locks are released. This is the memory analogue
    of :class:`PostgresUserLifecycleTransaction`, not a sequence of independently
    committed repository calls.
    """

    def __init__(
        self,
        *,
        users: "UserDirectory",
        sessions: "SessionStore",
        invitations: "InvitationRepository | None" = None,
        credentials: "CredentialStore | None" = None,
        pat_store: object | None = None,
    ) -> None:
        from inqtrix.auth.credentials import MemoryCredentialStore
        from inqtrix.auth.directory import MemoryUserDirectory
        from inqtrix.auth.invitations import MemoryInvitationStore
        from inqtrix.auth.pat import MemoryPatStore
        from inqtrix.auth.sessions import MemorySessionStore

        if not isinstance(users, MemoryUserDirectory):
            raise TypeError("Memory lifecycle requires MemoryUserDirectory")
        if not isinstance(sessions, MemorySessionStore):
            raise TypeError("Memory lifecycle requires MemorySessionStore")
        if invitations is not None and not isinstance(
            invitations, MemoryInvitationStore
        ):
            raise TypeError("Memory lifecycle requires MemoryInvitationStore")
        if credentials is not None and not isinstance(
            credentials, MemoryCredentialStore
        ):
            raise TypeError("Memory lifecycle requires MemoryCredentialStore")
        if pat_store is not None and not isinstance(pat_store, MemoryPatStore):
            raise TypeError("Memory lifecycle requires MemoryPatStore")
        self._users = users
        self._sessions = sessions
        self._invitations = invitations
        self._credentials = credentials
        self._pat_store = pat_store
        self._command_lock = threading.RLock()
        self._event_sink: Callable[..., object] | None = None
        self.audit_entries: list[AuditEntry] = []
        self.invalidations: list[MemoryLifecycleInvalidation] = []

    @property
    def atomic_effects(self) -> bool:
        """Whether content-free invalidations are bound to the app stream."""
        return self._event_sink is not None

    def bind_user_event_sink(self, sink: Callable[..., object]) -> None:
        """Attach synchronous content-free delivery during app composition."""
        if not callable(sink):
            raise TypeError("sink must be callable")
        with self._command_lock:
            self._event_sink = sink

    def bind_authority_coordinator(self, coordinator: object) -> None:
        """Use the process-wide memory authority lock as the outer command lock."""
        bind = getattr(coordinator, "bind_lifecycle", None)
        if not callable(bind):
            raise TypeError("invalid memory authority coordinator")
        bind(self)

    def _ordered_locks(self) -> tuple[Any, ...]:
        """Return the one global acquisition order used by every command."""
        locks = [self._command_lock]
        if self._invitations is not None:
            locks.append(self._invitations._lock)
            locks.append(self._invitations._identity._lock)
        locks.append(self._users._lock)
        if self._credentials is not None:
            locks.append(self._credentials._lock)
        locks.append(self._sessions._lock)
        if self._pat_store is not None:
            locks.append(self._pat_store._lock)
        unique: list[Any] = []
        for lock in locks:
            if all(lock is not current for current in unique):
                unique.append(lock)
        return tuple(unique)

    def _snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "users": dict(self._users.users),
            "sessions": dict(self._sessions._sessions),
            "audit_length": len(self.audit_entries),
            "invalidation_length": len(self.invalidations),
        }
        if self._invitations is not None:
            snapshot["invitations"] = dict(self._invitations._invitations)
            snapshot["members"] = dict(self._invitations._identity._members)
        if self._credentials is not None:
            snapshot["credentials"] = dict(self._credentials._by_user)
        if self._pat_store is not None:
            snapshot["pats"] = dict(self._pat_store._tokens)
        return snapshot

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self._users.users.clear()
        self._users.users.update(snapshot["users"])
        self._sessions._sessions.clear()
        self._sessions._sessions.update(snapshot["sessions"])
        if self._invitations is not None:
            self._invitations._invitations.clear()
            self._invitations._invitations.update(snapshot["invitations"])
            self._invitations._identity._members.clear()
            self._invitations._identity._members.update(snapshot["members"])
        if self._credentials is not None:
            self._credentials._by_user.clear()
            self._credentials._by_user.update(snapshot["credentials"])
        if self._pat_store is not None:
            self._pat_store._tokens.clear()
            self._pat_store._tokens.update(snapshot["pats"])
        del self.audit_entries[int(snapshot["audit_length"]) :]
        del self.invalidations[int(snapshot["invalidation_length"]) :]

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """Hold all stores and roll every one back on command failure."""
        with ExitStack() as stack:
            for lock in self._ordered_locks():
                stack.enter_context(lock)
            snapshot = self._snapshot()
            try:
                yield
            except BaseException:
                self._restore(snapshot)
                raise

    def _active_admin_user_ids_locked(
        self, tenant_id: str
    ) -> set[uuid.UUID]:
        return {
            user.user_id
            for user in self._users.users.values()
            if user.tenant_id == tenant_id
            and user.instance_role == "admin"
            and user.disabled_at is None
        }

    def _require_admin_actor_locked(
        self, *, tenant_id: str, actor_user_id: uuid.UUID
    ) -> None:
        """Revalidate one active instance admin under the command lock."""
        key = self._users._key_for_user_id(tenant_id, actor_user_id)
        actor = self._users.users.get(key) if key is not None else None
        if (
            actor is None
            or actor.disabled_at is not None
            or actor.instance_role != "admin"
        ):
            raise AdminAuthorizationError(
                "instance-admin authority was revoked before commit"
            )

    def _record_effect_locked(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        user_id: uuid.UUID,
        detail: dict[str, str] | None = None,
    ) -> None:
        self.audit_entries.append(
            AuditEntry(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                resource_type="user",
                resource_id=str(user_id),
                detail=dict(detail or {}),
            )
        )
        targets = self._active_admin_user_ids_locked(tenant_id)
        targets.add(user_id)
        if actor_user_id is not None:
            targets.add(actor_user_id)
        for target_user_id in sorted(targets, key=str):
            invalidation = MemoryLifecycleInvalidation(
                tenant_id=tenant_id,
                target_user_id=target_user_id,
                scope="account",
                resource_type="user",
                resource_id=str(user_id),
            )
            self.invalidations.append(invalidation)
            if self._event_sink is not None:
                self._event_sink(
                    tenant_id=tenant_id,
                    target_user_id=target_user_id,
                    scope=invalidation.scope,
                    resource_type=invalidation.resource_type,
                    resource_id=invalidation.resource_id,
                )

    async def provision_login(self, command: LoginCommand) -> "MirroredUser":
        """Provision mirror, invitations, role, session, and effects atomically."""
        with self._atomic():
            existing = await self._users.find_user(
                tenant_id=command.tenant_id,
                issuer=command.issuer,
                subject=command.subject,
            )
            if existing is not None and existing.disabled_at is not None:
                raise UserDisabledError(command.subject)
            if command.invitation_required and existing is None:
                has_invitation = bool(
                    self._invitations is not None
                    and command.email
                    and await self._invitations.has_open_for_email(
                        tenant_id=command.tenant_id,
                        email=command.email,
                        now=time.time(),
                    )
                )
                if not has_invitation:
                    raise RegistrationDenied(
                        "Registration requires an invitation. Contact the "
                        "administrator."
                    )
            user = await self._users.record_login(
                tenant_id=command.tenant_id,
                issuer=command.issuer,
                subject=command.subject,
                email=command.email,
                email_verified=command.email_verified,
                display_name=command.display_name,
                canonical_user_id=command.session.user_id,
            )
            accepted = ()
            if self._invitations is not None and command.email:
                accepted = await self._invitations.accept_open_for_email(
                    tenant_id=command.tenant_id,
                    email=command.email,
                    user_id=user.user_id,
                    now=time.time(),
                )
            if command.invitation_required and existing is None and not accepted:
                raise RegistrationDenied(
                    "Registration requires an invitation. Contact the "
                    "administrator."
                )
            await apply_admin_grant(
                self._users,
                tenant_id=command.tenant_id,
                user_id=user.user_id,
                is_admin=command.is_admin,
                first_login_owner=command.first_login_owner,
            )
            await self._sessions.create(
                replace(command.session, user_id=user.user_id)
            )
            for invitation in accepted:
                self.audit_entries.append(
                    AuditEntry(
                        tenant_id=command.tenant_id,
                        actor_user_id=user.user_id,
                        action="invitation.accepted",
                        resource_type="registration",
                        resource_id=invitation.id,
                        detail={},
                    )
                )
            self._record_effect_locked(
                tenant_id=command.tenant_id,
                actor_user_id=user.user_id,
                action="user.login_admitted",
                user_id=user.user_id,
            )
            refreshed = await self._users.find_by_user_id(
                tenant_id=command.tenant_id, user_id=user.user_id
            )
            return refreshed or user

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
        """Create local credential, mirror, role, session, and effects atomically."""
        if self._credentials is None:
            raise RuntimeError("Local-account lifecycle requires credentials")
        with self._atomic():
            if not first_only:
                if actor_user_id is None:
                    raise AdminAuthorizationError(
                        "admin-created accounts require an effective actor"
                    )
                self._require_admin_actor_locked(
                    tenant_id=tenant_id, actor_user_id=actor_user_id
                )
            created = await self._credentials.create(
                credential, tenant_id=tenant_id, allow_first_only=first_only
            )
            if not created:
                return None
            user = await self._users.record_login(
                tenant_id=tenant_id,
                issuer="local",
                subject=credential.subject,
                email=credential.email,
                email_verified=True,
                display_name=credential.display_name,
                canonical_user_id=credential.user_id,
            )
            if role == "admin":
                await self._users.set_instance_role(
                    tenant_id=tenant_id, user_id=user.user_id, role="admin"
                )
            if session is not None:
                await self._sessions.create(session)
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id or user.user_id,
                action="user.created",
                user_id=user.user_id,
                detail={"instance_role": role},
            )
            refreshed = await self._users.find_by_user_id(
                tenant_id=tenant_id, user_id=user.user_id
            )
            return refreshed or user

    async def set_role(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        role: Literal["admin", "user"],
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Set an instance role with the last-admin invariant and effects."""
        with self._atomic():
            self._require_admin_actor_locked(
                tenant_id=tenant_id, actor_user_id=actor_user_id
            )
            target = await self._users.find_by_user_id(
                tenant_id=tenant_id, user_id=user_id
            )
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            changed = (
                await self._users.demote_if_not_last_admin(
                    tenant_id=tenant_id, user_id=user_id
                )
                if role == "user"
                else await self._users.set_instance_role(
                    tenant_id=tenant_id, user_id=user_id, role="admin"
                )
            )
            if not changed:
                return UserLifecycleStatus.LAST_ADMIN
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.role_updated",
                user_id=user_id,
                detail={"instance_role": role},
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
        """Set disabled state and execute the complete cut-off atomically."""
        with self._atomic():
            self._require_admin_actor_locked(
                tenant_id=tenant_id, actor_user_id=actor_user_id
            )
            target = await self._users.find_by_user_id(
                tenant_id=tenant_id, user_id=user_id
            )
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            if disabled_at is None:
                await self._users.set_disabled(
                    tenant_id=tenant_id, user_id=user_id, disabled_at=None
                )
            elif not await self._users.disable_if_not_last_admin(
                tenant_id=tenant_id,
                user_id=user_id,
                disabled_at=disabled_at,
            ):
                return UserLifecycleStatus.LAST_ADMIN
            if self._credentials is not None:
                await self._credentials.set_disabled(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    disabled_at=disabled_at,
                )
            if disabled_at is not None:
                await self._sessions.delete_for_user(user_id=user_id)
                if self._pat_store is not None:
                    await self._pat_store.revoke_all_for_owner(
                        tenant_id=tenant_id,
                        owner_user_id=user_id,
                        now=time.time(),
                    )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=(
                    "user.disabled" if disabled_at is not None else "user.enabled"
                ),
                user_id=user_id,
            )
            return UserLifecycleStatus.UPDATED

    async def reset_local_password(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        password_hash: str,
        actor_user_id: uuid.UUID,
    ) -> bool:
        """Replace a local password and purge browser sessions atomically."""
        if self._credentials is None:
            raise RuntimeError("Password lifecycle requires credentials")
        with self._atomic():
            self._require_admin_actor_locked(
                tenant_id=tenant_id, actor_user_id=actor_user_id
            )
            changed = await self._credentials.set_password(
                tenant_id=tenant_id,
                user_id=user_id,
                password_hash=password_hash,
            )
            if not changed:
                return False
            await self._sessions.delete_for_user(user_id=user_id)
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="user.password_reset",
                user_id=user_id,
            )
            return True


class UserLifecycleService:
    """Application service for login provisioning and admin lifecycle commands.

    Every scoped deployment must supply :class:`UserLifecycleTransaction`.
    There is deliberately no repository-by-repository fallback: PostgreSQL and
    memory both implement the same atomic command port, while unscoped auth
    modes never construct this service.
    """

    def __init__(
        self,
        *,
        users: "UserDirectory",
        sessions: "SessionStore",
        invitations: "InvitationRepository | None" = None,
        credentials: "CredentialStore | None" = None,
        pat_service: "PatService | None" = None,
        transaction: UserLifecycleTransaction | None = None,
    ) -> None:
        del users, sessions, invitations, credentials, pat_service
        if transaction is None:
            raise RuntimeError(
                "Scoped user lifecycle requires an atomic transaction backend"
            )
        self._transaction = transaction

    @property
    def atomic_effects(self) -> bool:
        """Whether the transaction commits audit and delivered invalidations."""
        return bool(getattr(self._transaction, "atomic_effects", False))

    def bind_user_event_sink(self, sink: Callable[..., object]) -> None:
        """Bind the optional synchronous sink exposed by a memory transaction."""
        bind = getattr(self._transaction, "bind_user_event_sink", None)
        if callable(bind):
            bind(sink)

    def bind_authority_coordinator(self, coordinator: object) -> None:
        """Bind the memory transaction to the one process authority lock."""
        bind = getattr(
            self._transaction, "bind_authority_coordinator", None
        )
        if callable(bind):
            bind(coordinator)

    async def provision_login(self, command: LoginCommand) -> "MirroredUser":
        """Create/update a canonical user and establish one session atomically."""
        if command.session.user_id is None:
            raise ValueError("Lifecycle login requires a canonical session user_id")
        return await self._transaction.provision_login(command)

    async def create_local_account(
        self,
        *,
        tenant_id: str,
        credential: "LocalCredential",
        role: Literal["admin", "user"],
        session: "AuthSession | None" = None,
        first_only: bool = False,
        actor_user_id: uuid.UUID | None = None,
    ) -> "MirroredUser | None":
        """Create one local account, optionally establishing its first session."""
        if session is not None and session.user_id != credential.user_id:
            raise ValueError("Credential and session user_id must match")
        return await self._transaction.create_local_account(
            tenant_id=tenant_id,
            credential=credential,
            role=role,
            session=session,
            first_only=first_only,
            actor_user_id=actor_user_id,
        )

    async def set_role(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        role: Literal["admin", "user"],
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Change an instance role while preserving the last-admin invariant."""
        return await self._transaction.set_role(
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            actor_user_id=actor_user_id,
        )

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
        actor_user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Set disable state; disabling also revokes sessions and PATs."""
        return await self._transaction.set_disabled(
            tenant_id=tenant_id,
            user_id=user_id,
            disabled_at=disabled_at,
            actor_user_id=actor_user_id,
        )

    async def inventory(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> dict:
        """Count what erasing this account would reach. Reads only.

        A backend without the capability answers with an empty mapping so
        the caller can say "unavailable" rather than present a zero as if
        it were a measurement.
        """
        collect = getattr(self._transaction, "inventory", None)
        if collect is None:
            return {}
        return await collect(tenant_id=tenant_id, user_id=user_id)

    @property
    def erases_accounts(self) -> bool:
        """Whether this backend can erase an account at all."""
        return hasattr(self._transaction, "begin_erasure")

    async def begin_erasure(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        operation_id: str,
    ) -> tuple[UserLifecycleStatus, dict]:
        """Cut an account off and mark it as erased by one operation.

        The erasure itself is that deletion operation; this is its start.
        An account already being erased answers with its operation.
        """
        begin = getattr(self._transaction, "begin_erasure", None)
        if begin is None:
            raise RuntimeError(
                "This lifecycle backend cannot erase accounts"
            )
        return await begin(
            tenant_id=tenant_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            operation_id=operation_id,
        )

    async def reset_local_password(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        password_hash: str,
        actor_user_id: uuid.UUID,
    ) -> bool:
        """Replace a local password and invalidate browser sessions together."""
        return await self._transaction.reset_local_password(
            tenant_id=tenant_id,
            user_id=user_id,
            password_hash=password_hash,
            actor_user_id=actor_user_id,
        )
