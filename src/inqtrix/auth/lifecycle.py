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
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, Protocol

from inqtrix.auth.invitations import (
    CONFIRMED_EMAIL_REQUIRED,
    INVITATION_REQUIRED,
    RegistrationDenied,
)
from inqtrix.auth.permissions import AuditEntry

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
    #: Provisioning asked to enable an account it did not disable; the
    #: account stays disabled.
    KEPT_DISABLED = "kept_disabled"
    #: Provisioning names an account or a user name that is already taken
    #: (SCIM ``uniqueness``).
    CONFLICT = "conflict"
    #: Provisioning reached its hourly limit of erasure starts; nothing
    #: changed.
    LIMITED = "limited"


class UserDisabledError(RuntimeError):
    """Raised when a disabled external binding attempts to log in."""


class UserNotProvisionedError(RuntimeError):
    """Raised when provisioning owns account creation and a login has none.

    With SCIM the identity system creates every account; a sign-in of an
    identity it never provisioned is refused instead of creating one.
    """


class AdminAuthorizationError(RuntimeError):
    """Raised when an admin command's effective actor is no longer authorized.

    Request guards are intentionally only an early rejection.  Every command
    that mutates another user's administrative state repeats the active-admin
    check inside its atomic authority boundary so a concurrent demotion or
    disable cannot race a previously admitted request.
    """


@dataclass(frozen=True)
class AdminActor:
    """An instance administrator acting through the admin surface.

    Every command revalidates the administrator under its own locks, so a
    demotion or disable that commits first wins over an admitted request.
    """

    user_id: uuid.UUID


@dataclass(frozen=True)
class ProvisioningActor:
    """The identity provider acting through provisioning (SCIM).

    It needs no administrator, reaches only accounts bound under the
    identity source's issuer, and leaves no user id in the audit.
    """

    client: Literal["scim"] = "scim"


LifecycleActor = AdminActor | ProvisioningActor
"""Who runs an administrative lifecycle command."""


def audit_actor(actor: LifecycleActor) -> tuple[uuid.UUID | None, str]:
    """The audit row's ``actor_user_id`` and ``actor_type`` for *actor*."""
    if isinstance(actor, AdminActor):
        return actor.user_id, "user"
    return None, "provisioning"


def disable_source_of(actor: LifecycleActor) -> str:
    """The ``disabled_source`` a disable by *actor* records."""
    return "admin" if isinstance(actor, AdminActor) else "provisioning"


#: Audit detail of an enable that lifted a tombstone of provisioning.
RESTORED_FROM_DEPROVISIONING = {"restored_from_deprovisioning": "true"}


def restores_tombstone(target: Any, *, disabled_at: float | None) -> bool:
    """Whether an enable lifts the tombstone provisioning left on *target*.

    Only an administrator gets here with a tombstone: for provisioning a
    tombstone does not exist.
    """
    return disabled_at is None and target.scim_deleted_at is not None


def provisioning_issuer(source_issuer: str | None) -> str:
    """The only issuer whose accounts provisioning may reach.

    Raises:
        RuntimeError: There is no identity source; provisioning is then
            wired where it cannot exist.
    """
    if source_issuer is None:
        raise RuntimeError(
            "Provisioning reaches only accounts of the identity source, and "
            "this lifecycle has none"
        )
    return source_issuer


@dataclass(frozen=True)
class ExternalProfile:
    """An account as provisioning describes it.

    The SCIM ``User`` attributes Inqtrix keeps: ``userName``,
    ``displayName``, the ``name`` object, the primary e-mail (empty when
    none was given) and ``active``.
    """

    user_name: str
    display_name: str | None = None
    name: Mapping[str, str] | None = field(default=None, hash=False)
    email: str = ""
    active: bool = True


ProfileChange = Callable[[ExternalProfile], ExternalProfile]
"""A pure change from an account's current profile to its new one."""

#: Profile field -> the SCIM attribute that carries it (audit field names).
_SCIM_ATTRIBUTES = (
    ("user_name", "userName"),
    ("display_name", "displayName"),
    ("name", "name"),
    ("email", "emails"),
)


def external_profile(user: "MirroredUser") -> ExternalProfile:
    """The profile provisioning sees for *user*."""
    return ExternalProfile(
        user_name=user.scim_user_name or "",
        display_name=user.display_name,
        name=dict(user.scim_name) if user.scim_name else None,
        email=user.email,
        active=user.disabled_at is None,
    )


def scim_fields(profile: ExternalProfile) -> str:
    """The SCIM attributes *profile* carries, for the audit (names only)."""
    present = [
        attribute
        for field_name, attribute in _SCIM_ATTRIBUTES
        if getattr(profile, field_name)
    ]
    return ",".join([*present, "active"])


def changed_scim_fields(
    before: ExternalProfile, after: ExternalProfile
) -> tuple[str, ...]:
    """The SCIM attributes a change touched, ``active`` aside."""
    return tuple(
        attribute
        for field_name, attribute in _SCIM_ATTRIBUTES
        if getattr(before, field_name) != getattr(after, field_name)
    )


@dataclass(frozen=True)
class ProvisioningOutcome:
    """The result of a provisioning command.

    ``user`` is the account after the command, ``None`` unless the command
    ran; ``adopted`` is true when provisioning bound an existing account.
    """

    status: UserLifecycleStatus
    user: "MirroredUser | None" = None
    adopted: bool = False


@dataclass(frozen=True)
class LoginCommand:
    """Verified identity facts and the browser session to establish.

    ``email`` and ``display_name`` are empty when the assertion does not
    carry them; :func:`merge_login_profile` decides what the user row keeps.
    ``provisioned_only`` admits only an account that already exists: with
    SCIM, provisioning is the only way an account is created.
    """

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
    provisioned_only: bool = False

    @property
    def accepts_invitations(self) -> bool:
        """Whether this login may accept open invitations for its email.

        Only a confirmed email does: an invitation grants workspace
        membership, and an unconfirmed address is anyone's to claim.
        """
        return bool(self.email) and self.email_verified


def checked_source_issuer(source_issuer: str | None) -> str | None:
    """Validate the issuer key whose admins the last-admin rules count.

    ``None`` counts admins of every issuer (every mode but ``gateway``).
    An empty key is a wiring error: it would count nobody, so the first
    login with ``first_login_owner`` would make everyone an admin.
    """
    if source_issuer is not None and not source_issuer:
        raise ValueError(
            "source_issuer must be None or the non-empty issuer key of the "
            "identity source"
        )
    return source_issuer


ROLE_SOURCE_LOGIN_CLAIM = "login_claim"
ROLE_SOURCE_FIRST_OWNER = "first_owner"


def login_role_source(command: LoginCommand) -> str:
    """Why a login granted the admin role: its claims or the first-owner rule."""
    return ROLE_SOURCE_LOGIN_CLAIM if command.is_admin else ROLE_SOURCE_FIRST_OWNER


@dataclass(frozen=True)
class LoginProfile:
    """The profile values a login leaves on the user row."""

    email: str
    email_verified: bool
    display_name: str


class StoredProfile(Protocol):
    """The profile columns of a stored user, in either backend."""

    email: str
    email_verified: bool
    display_name: str | None


def merge_login_profile(
    stored: StoredProfile | None, command: LoginCommand
) -> LoginProfile:
    """Profile values after this login, shared by both backends.

    A value the login does not carry never overwrites a stored one: without
    an email the stored email stays together with its verification flag,
    without a name the stored name stays. The name falls back to the email,
    then to the subject, and fills only a name that is still empty.
    """
    if command.email:
        email, email_verified = command.email, command.email_verified
    elif stored is not None:
        email, email_verified = stored.email, stored.email_verified
    else:
        email, email_verified = "", False
    stored_name = stored.display_name if stored is not None else None
    return LoginProfile(
        email=email,
        email_verified=email_verified,
        display_name=command.display_name or stored_name or email or command.subject,
    )


class UserLifecycleTransaction(Protocol):
    """Durable backend whose methods each commit exactly one transaction."""

    @property
    def source_issuer(self) -> str | None:
        """Issuer whose admins the first-owner rule and the guards count."""
        ...

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
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Apply disable state and the complete cut-off cascade atomically."""
        ...

    async def provision_external_user(
        self,
        *,
        tenant_id: str,
        subject: str,
        profile: ExternalProfile,
    ) -> ProvisioningOutcome:
        """Create an account for provisioning, or adopt an unbound one."""
        ...

    async def update_external_profile(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        change: ProfileChange,
    ) -> ProvisioningOutcome:
        """Apply a provisioning change to an account of the source."""
        ...

    async def deprovision(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Disable an account for provisioning and leave a tombstone that
        gives up its user name."""
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
    committed repository calls; like it, the commands count admins themselves,
    and the first-owner rule and the last-admin guards count only active admins
    bound under ``source_issuer`` (every issuer when ``None``).
    """

    def __init__(
        self,
        *,
        users: "UserDirectory",
        sessions: "SessionStore",
        invitations: "InvitationRepository | None" = None,
        credentials: "CredentialStore | None" = None,
        pat_store: object | None = None,
        source_issuer: str | None = None,
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
        self._source_issuer = checked_source_issuer(source_issuer)
        self._command_lock = threading.RLock()
        self._event_sink: Callable[..., object] | None = None
        self.audit_entries: list[AuditEntry] = []
        self.invalidations: list[MemoryLifecycleInvalidation] = []

    @property
    def atomic_effects(self) -> bool:
        """Whether content-free invalidations are bound to the app stream."""
        return self._event_sink is not None

    @property
    def source_issuer(self) -> str | None:
        """Issuer whose admins the first-owner rule and the guards count."""
        return self._source_issuer

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
        self, tenant_id: str, *, issuer: str | None = None
    ) -> set[uuid.UUID]:
        """Active admins of the tenant, only those bound under *issuer* if set."""
        return {
            user.user_id
            for user in self._users.users.values()
            if user.tenant_id == tenant_id
            and user.instance_role == "admin"
            and user.disabled_at is None
            and (issuer is None or user.issuer == issuer)
        }

    def _is_last_counted_admin_locked(self, target: "MirroredUser") -> bool:
        """Whether *target* is an active admin and no other one counts.

        Counted are the active admins of the identity source, as in the
        Postgres guards; administrative wake-ups still reach every admin.
        """
        if target.instance_role != "admin" or target.disabled_at is not None:
            return False
        others = self._active_admin_user_ids_locked(
            target.tenant_id, issuer=self._source_issuer
        ) - {target.user_id}
        return not others

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
        actor_type: str = "user",
    ) -> None:
        self.audit_entries.append(
            AuditEntry(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                resource_type="user",
                resource_id=str(user_id),
                detail=dict(detail or {}),
                actor_type=actor_type,
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
            if command.provisioned_only and existing is None:
                raise UserNotProvisionedError("no account is provisioned for this identity")
            if command.invitation_required and existing is None:
                if not command.accepts_invitations:
                    raise RegistrationDenied(
                        CONFIRMED_EMAIL_REQUIRED, reason="email_unverified"
                    )
                has_invitation = self._invitations is not None and bool(
                    await self._invitations.has_open_for_email(
                        tenant_id=command.tenant_id,
                        email=command.email,
                        now=time.time(),
                    )
                )
                if not has_invitation:
                    raise RegistrationDenied(
                        INVITATION_REQUIRED, reason="no_invitation"
                    )
            profile = merge_login_profile(existing, command)
            user = await self._users.record_login(
                tenant_id=command.tenant_id,
                issuer=command.issuer,
                subject=command.subject,
                email=profile.email,
                email_verified=profile.email_verified,
                display_name=profile.display_name,
                canonical_user_id=command.session.user_id,
            )
            accepted = ()
            if self._invitations is not None and command.accepts_invitations:
                accepted = await self._invitations.accept_open_for_email(
                    tenant_id=command.tenant_id,
                    email=command.email,
                    user_id=user.user_id,
                    now=time.time(),
                )
            if command.invitation_required and existing is None and not accepted:
                raise RegistrationDenied(INVITATION_REQUIRED, reason="no_invitation")
            # Grant only, never revoke: the admin surface owns demotion and
            # its last-admin guard.
            grant_admin = command.is_admin or (
                command.first_login_owner
                and not self._active_admin_user_ids_locked(
                    command.tenant_id, issuer=self._source_issuer
                )
            )
            if grant_admin:
                await self._users.set_instance_role(
                    tenant_id=command.tenant_id,
                    user_id=user.user_id,
                    role="admin",
                )
            await self._sessions.create(
                replace(
                    command.session,
                    user_id=user.user_id,
                    email=profile.email or None,
                    display_name=profile.display_name,
                )
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
            current = refreshed or user
            if user.instance_role != "admin" and current.instance_role == "admin":
                self.audit_entries.append(
                    AuditEntry(
                        tenant_id=command.tenant_id,
                        actor_user_id=user.user_id,
                        action="user.role_updated",
                        resource_type="user",
                        resource_id=str(user.user_id),
                        detail={
                            "instance_role": "admin",
                            "source": login_role_source(command),
                        },
                    )
                )
            return current

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
            if role == "user" and self._is_last_counted_admin_locked(target):
                return UserLifecycleStatus.LAST_ADMIN
            await self._users.set_instance_role(
                tenant_id=tenant_id, user_id=user_id, role=role
            )
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
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Set disabled state and execute the complete cut-off atomically.

        The actor decides the source of a disable. Provisioning never takes
        over a disable it did not apply and lifts only its own; asked to
        lift an administrator's, it leaves the account disabled.
        """
        with self._atomic():
            if isinstance(actor, AdminActor):
                self._require_admin_actor_locked(
                    tenant_id=tenant_id, actor_user_id=actor.user_id
                )
            target = await self._reachable_locked(
                tenant_id=tenant_id, user_id=user_id, actor=actor
            )
            if target is None:
                return UserLifecycleStatus.NOT_FOUND
            return await self._apply_disable_locked(
                target=target, disabled_at=disabled_at, actor=actor
            )

    async def _reachable_locked(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        actor: LifecycleActor,
    ) -> "MirroredUser | None":
        """The target, or ``None`` when *actor* cannot reach it.

        Provisioning reaches only accounts bound under the identity source.
        """
        reachable = (
            provisioning_issuer(self._source_issuer)
            if isinstance(actor, ProvisioningActor)
            else None
        )
        target = await self._users.find_by_user_id(
            tenant_id=tenant_id, user_id=user_id
        )
        if target is None or (reachable is not None and target.issuer != reachable):
            return None
        return target

    async def _apply_disable_locked(
        self,
        *,
        target: "MirroredUser",
        disabled_at: float | None,
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Disable or enable *target* inside the caller's atomic block."""
        tenant_id, user_id = target.tenant_id, target.user_id
        actor_user_id, actor_type = audit_actor(actor)
        if isinstance(actor, ProvisioningActor) and target.disabled_at is not None:
            if disabled_at is not None:
                # Already disabled: the disable keeps its source.
                return UserLifecycleStatus.UPDATED
            if target.scim_deleted_at is not None:
                # A tombstone comes back only through an administrator.
                return UserLifecycleStatus.NOT_FOUND
            if target.disabled_source != "provisioning":
                self._record_effect_locked(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.scim_enable_ignored",
                    user_id=user_id,
                )
                return UserLifecycleStatus.KEPT_DISABLED
        if disabled_at is not None and self._is_last_counted_admin_locked(
            target
        ):
            return UserLifecycleStatus.LAST_ADMIN
        restored = restores_tombstone(target, disabled_at=disabled_at)
        await self._users.set_disabled(
            tenant_id=tenant_id,
            user_id=user_id,
            disabled_at=disabled_at,
            source=disable_source_of(actor) if disabled_at is not None else None,
        )
        if restored:
            self._users.update_user(
                tenant_id=tenant_id, user_id=user_id, scim_deleted_at=None
            )
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
            actor_type=actor_type,
            action=(
                "user.disabled" if disabled_at is not None else "user.enabled"
            ),
            user_id=user_id,
            detail=RESTORED_FROM_DEPROVISIONING if restored else None,
        )
        return UserLifecycleStatus.UPDATED

    def _user_name_taken_locked(
        self,
        *,
        tenant_id: str,
        issuer: str,
        user_name: str,
        owner_id: uuid.UUID | None,
    ) -> bool:
        """Whether another account of *issuer* carries *user_name* (any case)."""
        wanted = user_name.lower()
        return any(
            user.tenant_id == tenant_id
            and user.issuer == issuer
            and user.user_id != owner_id
            and user.scim_user_name is not None
            and user.scim_user_name.lower() == wanted
            for user in self._users.users.values()
        )

    async def provision_external_user(
        self,
        *,
        tenant_id: str,
        subject: str,
        profile: ExternalProfile,
    ) -> ProvisioningOutcome:
        """Create an account for provisioning, or adopt an unbound one.

        The rules of the Postgres backend: no session, no sign-in yet, an
        unconfirmed e-mail; adoption only of an account provisioning never
        bound; conflicts for a bound account, a tombstone, an account being
        erased and a user name another account of the source carries.
        """
        issuer = provisioning_issuer(self._source_issuer)
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        with self._atomic():
            existing = await self._users.find_user(
                tenant_id=tenant_id, issuer=issuer, subject=subject
            )
            if existing is not None and (
                existing.scim_user_name is not None
                or existing.scim_deleted_at is not None
                or existing.erasure_operation_id is not None
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            if self._user_name_taken_locked(
                tenant_id=tenant_id,
                issuer=issuer,
                user_name=profile.user_name,
                owner_id=existing.user_id if existing is not None else None,
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            scim_name = dict(profile.name) if profile.name else None
            if existing is None:
                from inqtrix.auth.directory import MirroredUser

                user = MirroredUser(
                    user_id=uuid.uuid4(),
                    issuer=issuer,
                    subject=subject,
                    email=profile.email,
                    email_verified=False,
                    display_name=profile.display_name,
                    tenant_id=tenant_id,
                    disabled_at=None if profile.active else time.time(),
                    disabled_source=None if profile.active else "provisioning",
                    scim_user_name=profile.user_name,
                    scim_name=scim_name,
                    created_at=time.time(),
                )
                self._users.insert_user(user)
                self._record_effect_locked(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.provisioned",
                    user_id=user.user_id,
                    detail={"fields": scim_fields(profile)},
                )
                return ProvisioningOutcome(UserLifecycleStatus.UPDATED, user)
            disable = not profile.active and existing.disabled_at is None
            if disable and self._is_last_counted_admin_locked(existing):
                return ProvisioningOutcome(UserLifecycleStatus.LAST_ADMIN)
            adopted = self._users.update_user(
                tenant_id=tenant_id,
                user_id=existing.user_id,
                scim_user_name=profile.user_name,
                scim_name=scim_name,
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                action="user.scim_adopted",
                user_id=existing.user_id,
            )
            if profile.active != (adopted.disabled_at is None):
                await self._apply_disable_locked(
                    target=adopted,
                    disabled_at=None if profile.active else time.time(),
                    actor=actor,
                )
            return ProvisioningOutcome(
                UserLifecycleStatus.UPDATED,
                await self._users.find_by_user_id(
                    tenant_id=tenant_id, user_id=existing.user_id
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

        The rules of the Postgres backend: the change runs inside the
        atomic block, a new e-mail is unconfirmed, ``active`` follows
        :meth:`set_disabled`, and roles never change here.
        """
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        with self._atomic():
            target = await self._reachable_locked(
                tenant_id=tenant_id, user_id=user_id, actor=actor
            )
            if (
                target is None
                or target.scim_deleted_at is not None
                or target.erasure_operation_id is not None
            ):
                return ProvisioningOutcome(UserLifecycleStatus.NOT_FOUND)
            before = external_profile(target)
            after = change(before)
            if after.user_name != before.user_name and self._user_name_taken_locked(
                tenant_id=tenant_id,
                issuer=target.issuer,
                user_name=after.user_name,
                owner_id=user_id,
            ):
                return ProvisioningOutcome(UserLifecycleStatus.CONFLICT)
            if (
                before.active
                and not after.active
                and self._is_last_counted_admin_locked(target)
            ):
                return ProvisioningOutcome(UserLifecycleStatus.LAST_ADMIN)
            fields = changed_scim_fields(before, after)
            if fields:
                target = self._users.update_user(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    # An empty user name leaves the account unbound.
                    scim_user_name=after.user_name or None,
                    display_name=after.display_name,
                    scim_name=dict(after.name) if after.name else None,
                    email=after.email,
                    email_verified=(
                        target.email_verified if after.email == before.email else False
                    ),
                )
                self._record_effect_locked(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    actor_type=actor_type,
                    action="user.scim_updated",
                    user_id=user_id,
                    detail={"fields": ",".join(fields)},
                )
            if after.active != before.active:
                await self._apply_disable_locked(
                    target=target,
                    disabled_at=None if after.active else time.time(),
                    actor=actor,
                )
            return ProvisioningOutcome(
                UserLifecycleStatus.UPDATED,
                await self._users.find_by_user_id(
                    tenant_id=tenant_id, user_id=user_id
                ),
            )

    async def deprovision(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Disable an account for provisioning and leave a tombstone.

        The rules of the Postgres backend: the tombstone gives up its user
        name, while its identity stays blocked.
        """
        actor = ProvisioningActor()
        actor_user_id, actor_type = audit_actor(actor)
        with self._atomic():
            target = await self._reachable_locked(
                tenant_id=tenant_id, user_id=user_id, actor=actor
            )
            if (
                target is None
                or target.scim_deleted_at is not None
                or target.erasure_operation_id is not None
            ):
                return UserLifecycleStatus.NOT_FOUND
            now = time.time()
            if target.disabled_at is None:
                status = await self._apply_disable_locked(
                    target=target, disabled_at=now, actor=actor
                )
                if status is UserLifecycleStatus.LAST_ADMIN:
                    return status
            self._users.update_user(
                tenant_id=tenant_id,
                user_id=user_id,
                scim_deleted_at=now,
                scim_user_name=None,
            )
            self._record_effect_locked(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                action="user.deprovisioned",
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

    @property
    def source_issuer(self) -> str | None:
        """Issuer whose admins the first-owner rule and the guards count."""
        return self._transaction.source_issuer

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
        actor: LifecycleActor,
    ) -> UserLifecycleStatus:
        """Set disable state; disabling also revokes sessions and PATs."""
        return await self._transaction.set_disabled(
            tenant_id=tenant_id,
            user_id=user_id,
            disabled_at=disabled_at,
            actor=actor,
        )

    async def provision_external_user(
        self,
        *,
        tenant_id: str,
        subject: str,
        profile: ExternalProfile,
    ) -> ProvisioningOutcome:
        """Create or adopt an account for provisioning (SCIM ``POST``)."""
        return await self._transaction.provision_external_user(
            tenant_id=tenant_id, subject=subject, profile=profile
        )

    async def update_external_profile(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        change: ProfileChange,
    ) -> ProvisioningOutcome:
        """Apply a provisioning change (SCIM ``PUT`` and ``PATCH``)."""
        return await self._transaction.update_external_profile(
            tenant_id=tenant_id, user_id=user_id, change=change
        )

    async def deprovision(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> UserLifecycleStatus:
        """Disable an account and leave a tombstone (SCIM ``DELETE``).

        The tombstone gives up its user name, so a new person of the source
        can take it; the identity itself stays blocked.
        """
        return await self._transaction.deprovision(
            tenant_id=tenant_id, user_id=user_id
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
        actor: LifecycleActor,
        operation_id: str,
        hourly_limit: int | None = None,
    ) -> tuple[UserLifecycleStatus, dict]:
        """Cut an account off and mark it as erased by one operation.

        The erasure itself is that deletion operation; this is its start.
        An account already being erased answers with its operation.
        *hourly_limit* bounds the erasures provisioning starts per hour.
        """
        begin = getattr(self._transaction, "begin_erasure", None)
        if begin is None:
            raise RuntimeError(
                "This lifecycle backend cannot erase accounts"
            )
        return await begin(
            tenant_id=tenant_id,
            user_id=user_id,
            actor=actor,
            operation_id=operation_id,
            hourly_limit=hourly_limit,
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
