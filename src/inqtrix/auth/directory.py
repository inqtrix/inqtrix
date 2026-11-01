"""Local user mirror: JIT provisioning keyed on ``(issuer, subject)``.

The identity anchor is the pair — subjects are only unique per issuer
and email is mutable profile data, never identity (the ``users``
table's ``UNIQUE(issuer, subject)`` pins this). Every successful OIDC
login upserts the mirror row so collaboration features (share
typeaheads, audit display names) have a local source without ever
proxying the IdP's admin API.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from inqtrix.auth.credentials import LOCAL_ISSUER

#: The tenant's deleted-account placeholder: the actor that REASSIGN rules
#: point at. It is an ordinary ``users`` row so every foreign key and
#: CHECK holds, but it is disabled from birth, has no credential, can never
#: sign in, and the admin listing hides it. One per tenant, created on
#: first use.
DELETED_ACCOUNT_ISSUER = "inqtrix:system"

#: Issuers that belong to Inqtrix itself. No external identity source may
#: bind users under them: a gateway issuer named ``local`` would reach the
#: password accounts, and one named like the system issuer the placeholder.
RESERVED_ISSUERS: frozenset[str] = frozenset({LOCAL_ISSUER, DELETED_ACCOUNT_ISSUER})


@dataclass(frozen=True)
class MirroredUser:
    """One mirrored identity (memory backend's record shape).

    Attributes:
        tenant_id: Tenant owning the canonical identity.
        disabled_at: Soft-disable timestamp; a disabled user is denied
            at login admission. ``None`` (the default, and the value
            for rows written before the field existed) means active.
    """

    user_id: uuid.UUID
    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str | None
    tenant_id: str = "default"
    disabled_at: float | None = None
    # Instance-wide role for the admin surface (admin|user). Additive
    # default so every existing record / serialized row reads as a regular
    # user (mirrors the disabled_at additive precedent).
    instance_role: str = "user"
    # Last successful login (unix seconds); audit column for the admin list.
    last_login_at: float | None = None
    # The user's canonical project namespace (a ``ws_...`` string), adopted on
    # first authenticated boot or claimed by the first write without a
    # namespace, so the project follows the user across devices and clients.
    # Additive default ``None`` = not yet adopted (mirrors the disabled_at /
    # instance_role additive precedent).
    default_workspace_id: str | None = None
    # The deletion operation erasing this account; ``None`` unless an
    # administrator started its erasure (only the Postgres backend erases).
    erasure_operation_id: str | None = None
    # Who disabled the account: ``admin`` or ``provisioning``. ``None`` while
    # it is enabled, and for a disable from before the source was recorded,
    # which counts as an administrator's.
    disabled_source: str | None = None
    # Provisioning state: the SCIM userName, the SCIM name object and the
    # time the identity provider deleted the account (a tombstone). ``None``
    # for every account provisioning never touched.
    scim_user_name: str | None = None
    scim_name: Mapping[str, str] | None = field(default=None, hash=False)
    scim_deleted_at: float | None = None
    # When the account came into being (unix seconds); ``None`` for records
    # built before the field existed.
    created_at: float | None = None


class UserDirectory(Protocol):
    """Port for the local user mirror."""

    async def find_user(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> "MirroredUser | None":
        """The mirrored identity (incl. instance_role), or ``None``."""
        ...

    async def find_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> "MirroredUser | None":
        """Return the user with canonical *user_id*, or ``None``."""
        ...

    async def profiles_for_user_ids(
        self, *, tenant_id: str, user_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, "MirroredUser"]:
        """Resolve display profiles for a bounded set of canonical user ids."""
        ...

    async def set_instance_role(
        self, *, tenant_id: str, user_id: uuid.UUID, role: str
    ) -> bool:
        """Set the instance role (admin|user); ``True`` when a row changed."""
        ...

    async def resolve_default_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate: str
    ) -> str | None:
        """Return the user's canonical project namespace, ADOPTING ``candidate``
        on the first call (when none is set yet).

        Idempotent claim-on-first-use: if the user already has a
        ``default_workspace_id`` it is returned unchanged (``candidate`` is
        ignored), so every device and client converges on the namespace of the
        first authenticated boot or the first write without a namespace, and the
        data follows the user. The write is atomic (set-only-when-NULL) so
        concurrent first uses cannot split the namespace. Returns ``None`` only
        when the user row is absent (an un-mirrored identity): a session start
        then falls back to the browser-local id and a write keeps no namespace.
        """
        ...

    async def list_users(
        self, *, tenant_id: str
    ) -> tuple["MirroredUser", ...]:
        """All mirrored identities in the tenant (admin listing)."""
        ...

    async def list_users_page(
        self,
        *,
        tenant_id: str,
        issuer: str,
        user_name: str | None = None,
        subject: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> tuple[int, tuple["MirroredUser", ...]]:
        """One page of the accounts provisioning sees, and their total."""
        ...


class UserLookup(Protocol):
    """Narrow read port for admission checks.

    A separate protocol on purpose: growing :class:`UserDirectory`
    would silently break implementations elsewhere; admission only
    needs this one lookup.
    """

    async def find_user(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> MirroredUser | None:
        """The mirrored identity, or ``None`` for first-time logins."""
        ...


class MemoryUserDirectory:
    """Process-local mirror (zero-infrastructure default)."""

    def __init__(self) -> None:
        self.users: dict[tuple[str, str, str], MirroredUser] = {}
        self._lock = threading.RLock()

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
    ) -> MirroredUser:
        import time
        from dataclasses import replace

        with self._lock:
            key = (tenant_id, issuer, subject)
            existing = self.users.get(key)
            if existing is not None:
                # Only the profile and the login timestamp refresh; disable,
                # role, project namespace and provisioning state stay as they
                # are, so a re-login never orphans data or a SCIM binding.
                user = replace(
                    existing,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                    last_login_at=time.time(),
                )
            else:
                now = time.time()
                user = MirroredUser(
                    user_id=canonical_user_id or uuid.uuid4(),
                    issuer=issuer,
                    subject=subject,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                    tenant_id=tenant_id,
                    last_login_at=now,
                    created_at=now,
                )
            self.users[key] = user
            return user

    def _key_for_user_id(
        self, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[str, str, str] | None:
        """Return the external-binding key for a canonical UUID.

        The caller must hold ``self._lock``.
        """
        return next(
            (
                key
                for key, user in self.users.items()
                if user.tenant_id == tenant_id and user.user_id == user_id
            ),
            None,
        )

    async def resolve_default_workspace(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate: str
    ) -> str | None:
        """Return the adopted project namespace, claiming ``candidate`` if unset."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return None
            if existing.default_workspace_id is not None:
                return existing.default_workspace_id
            assert key is not None
            self.users[key] = replace(
                existing, default_workspace_id=candidate
            )
            return candidate

    async def set_instance_role(
        self, *, tenant_id: str, user_id: uuid.UUID, role: str
    ) -> bool:
        """Set the instance role; ``True`` when a row changed."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return False
            assert key is not None
            self.users[key] = replace(
                existing, instance_role=role
            )
            return True

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
        source: str | None = None,
    ) -> bool:
        """Set/clear the soft-disable timestamp and who set it.

        ``True`` when a row changed.
        """
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return False
            assert key is not None
            self.users[key] = replace(
                existing, disabled_at=disabled_at, disabled_source=source
            )
            return True

    async def list_users(
        self, *, tenant_id: str
    ) -> tuple[MirroredUser, ...]:
        """All mirrored identities (admin listing)."""
        with self._lock:
            return tuple(
                sorted(
                    (
                        user
                        for user in self.users.values()
                        if user.tenant_id == tenant_id
                    ),
                    key=lambda user: user.email,
                )
            )

    async def list_users_page(
        self,
        *,
        tenant_id: str,
        issuer: str,
        user_name: str | None = None,
        subject: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> tuple[int, tuple[MirroredUser, ...]]:
        """One page of the accounts provisioning sees, and their total.

        Accounts bound under *issuer*; tombstones and accounts being erased
        never appear, disabled ones do. ``user_name`` matches without regard
        to case, ``subject`` exactly; ``start_index`` counts from 1. Pages
        follow the order in which the accounts were mirrored.
        """
        wanted = user_name.lower() if user_name is not None else None
        with self._lock:
            matches = [
                user
                for user in self.users.values()
                if user.tenant_id == tenant_id
                and user.issuer == issuer
                and user.scim_deleted_at is None
                and user.erasure_operation_id is None
                and (
                    wanted is None
                    or (user.scim_user_name or "").lower() == wanted
                )
                and (subject is None or user.subject == subject)
            ]
        offset = max(start_index, 1) - 1
        return len(matches), tuple(matches[offset : offset + max(count, 0)])

    def insert_user(self, user: MirroredUser) -> None:
        """Mirror an account that provisioning created (no sign-in yet)."""
        with self._lock:
            key = (user.tenant_id, user.issuer, user.subject)
            if key in self.users:
                raise ValueError("this identity is already mirrored")
            self.users[key] = user

    def update_user(
        self, *, tenant_id: str, user_id: uuid.UUID, **changes: object
    ) -> MirroredUser:
        """Replace fields of one mirrored account and return it."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            if key is None:
                raise KeyError(user_id)
            self.users[key] = replace(self.users[key], **changes)
            return self.users[key]

    def active_admin_user_ids_nowait(
        self, tenant_id: str
    ) -> tuple[uuid.UUID, ...]:
        """Return active instance-admin UUIDs for synchronous memory fan-out."""
        with self._lock:
            return tuple(
                sorted(
                    (
                        user.user_id
                        for user in self.users.values()
                        if user.tenant_id == tenant_id
                        and user.instance_role == "admin"
                        and user.disabled_at is None
                    ),
                    key=str,
                )
            )

    def is_active_nowait(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Return current active status under the shared memory boundary."""
        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            user = self.users.get(key) if key is not None else None
            return user is not None and user.disabled_at is None

    async def find_user(
        self, *, tenant_id: str, issuer: str, subject: str
    ) -> MirroredUser | None:
        with self._lock:
            return self.users.get((tenant_id, issuer, subject))

    async def find_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> MirroredUser | None:
        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            return self.users.get(key) if key is not None else None

    async def profiles_for_user_ids(
        self, *, tenant_id: str, user_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, MirroredUser]:
        """``user_id -> profile`` for share-listing enrichment (one pass)."""
        wanted = set(user_ids)
        with self._lock:
            return {
                user.user_id: user
                for user in self.users.values()
                if user.tenant_id == tenant_id and user.user_id in wanted
            }

    async def has_user_id(self, *, tenant_id: str, user_id: uuid.UUID) -> bool:
        """Whether an active mirrored user carries this canonical UUID."""
        with self._lock:
            return any(
                user.tenant_id == tenant_id
                and user.user_id == user_id
                and user.disabled_at is None
                for user in self.users.values()
            )

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        limit: int = 10,
        exclude_user_id: uuid.UUID | None = None,
    ) -> tuple[MirroredUser, ...]:
        """Prefix search over email and display name (share typeahead).

        Disabled users never appear; the caller is excluded so the
        picker cannot offer self-shares.
        """
        needle = query.strip().lower()
        if not needle:
            return ()
        with self._lock:
            matches = [
                user
                for user in self.users.values()
                if user.tenant_id == tenant_id
                and user.disabled_at is None
                and user.user_id != exclude_user_id
                and (
                    user.email.lower().startswith(needle)
                    or (user.display_name or "").lower().startswith(needle)
                )
            ]
        return tuple(
            sorted(matches, key=lambda user: user.email)[:limit]
        )
