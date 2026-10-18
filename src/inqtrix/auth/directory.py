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
from dataclasses import dataclass
from typing import Protocol


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
    # first authenticated boot so the project follows the user across devices.
    # Additive default ``None`` = not yet adopted (mirrors the disabled_at /
    # instance_role additive precedent).
    default_workspace_id: str | None = None
    # The deletion operation erasing this account; ``None`` unless an
    # administrator started its erasure (only the Postgres backend erases).
    erasure_operation_id: str | None = None


class UserDirectory(Protocol):
    """Port for the local user mirror."""

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
        """Upsert and return one identity on successful login."""
        ...

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
        ignored), so every device converges on the namespace adopted by the
        first authenticated boot — the data follows the user. The write is
        atomic (set-only-when-NULL) so concurrent first-logins cannot split the
        namespace. Returns ``None`` only when the user row is absent (an
        un-mirrored identity), so the caller falls back to the browser-local id.
        """
        ...

    async def set_disabled(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        disabled_at: float | None,
    ) -> bool:
        """Set/clear the mirror disable flag; ``True`` when a row changed."""
        ...

    async def list_users(
        self, *, tenant_id: str
    ) -> tuple["MirroredUser", ...]:
        """All mirrored identities in the tenant (admin listing)."""
        ...

    async def count_admins(self, *, tenant_id: str) -> int:
        """Active instance-admin count (the last-admin guard)."""
        ...

    async def promote_if_no_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Promote to admin iff no active admin exists (first-login owner)."""
        ...

    async def demote_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Demote to ``user`` atomically, guarding the last-admin invariant.

        The check ("is this the last active admin?") and the write are ONE
        operation so two concurrent demotions cannot both pass a stale count
        and leave the instance with zero admins. Returns ``True`` when the
        demotion was applied (including the no-op case where the target was
        not an active admin), ``False`` ONLY when the target is the last
        active admin (the caller maps that to 409) or the row is absent.
        """
        ...

    async def disable_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float
    ) -> bool:
        """Set the disable flag atomically, guarding the last-admin invariant.

        Same contract as :meth:`demote_if_not_last_admin`: ``True`` when the
        flag was set (including disabling a non-admin), ``False`` only when
        the target is the last active admin or the row is absent. The rest of
        the cut-off cascade (sessions, PATs, the local credential) is left to
        the admin router and runs only after this returns ``True``.
        """
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

        with self._lock:
            key = (tenant_id, issuer, subject)
            existing = self.users.get(key)
            user = MirroredUser(
                user_id=(
                    existing.user_id
                    if existing is not None
                    else (canonical_user_id or uuid.uuid4())
                ),
                issuer=issuer,
                subject=subject,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                tenant_id=tenant_id,
                # Preserve admin-managed state (disable + role) across logins;
                # only profile fields and the login timestamp refresh.
                disabled_at=(
                    existing.disabled_at if existing is not None else None
                ),
                instance_role=(
                    existing.instance_role if existing is not None else "user"
                ),
                last_login_at=time.time(),
                # Preserve the adopted project namespace across logins (like
                # disabled_at / instance_role) so re-login does not orphan data.
                default_workspace_id=(
                    existing.default_workspace_id if existing is not None else None
                ),
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
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float | None
    ) -> bool:
        """Set/clear the soft-disable timestamp; ``True`` when a row changed."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return False
            assert key is not None
            self.users[key] = replace(
                existing, disabled_at=disabled_at
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

    async def count_admins(self, *, tenant_id: str) -> int:
        """Active instance-admins (the last-admin guard reads this)."""
        with self._lock:
            return sum(
                1
                for u in self.users.values()
                if u.tenant_id == tenant_id
                and u.instance_role == "admin"
                and u.disabled_at is None
            )

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

    async def promote_if_no_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Promote this user to admin IFF no active admin exists yet.

        The first-login-owner bootstrap, atomic under the lock: exactly
        the first authenticated user in a fresh deployment becomes admin.
        """
        from dataclasses import replace

        with self._lock:
            has_admin = any(
                u.instance_role == "admin" and u.disabled_at is None
                for u in self.users.values()
                if u.tenant_id == tenant_id
            )
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if has_admin or existing is None:
                return False
            assert key is not None
            self.users[key] = replace(
                existing, instance_role="admin"
            )
            return True

    def _is_last_active_admin(
        self, existing: MirroredUser, user_id: uuid.UUID
    ) -> bool:
        """Whether *existing* is an active admin with no other active admin.

        Caller must hold ``self._lock``. The basis of the atomic last-admin
        guards: if removing this user's active-admin status leaves none, the
        guarded op refuses.
        """
        if existing.instance_role != "admin" or existing.disabled_at is not None:
            return False
        return not any(
            u.tenant_id == existing.tenant_id
            and u.instance_role == "admin"
            and u.disabled_at is None
            and u.user_id != user_id
            for u in self.users.values()
        )

    async def demote_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> bool:
        """Demote to ``user`` unless this is the last active admin (atomic)."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return False
            if self._is_last_active_admin(existing, user_id):
                return False
            assert key is not None
            self.users[key] = replace(
                existing, instance_role="user"
            )
            return True

    async def disable_if_not_last_admin(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float
    ) -> bool:
        """Set the disable flag unless this is the last active admin (atomic)."""
        from dataclasses import replace

        with self._lock:
            key = self._key_for_user_id(tenant_id, user_id)
            existing = self.users.get(key) if key is not None else None
            if existing is None:
                return False
            if self._is_last_active_admin(existing, user_id):
                return False
            assert key is not None
            self.users[key] = replace(
                existing, disabled_at=disabled_at
            )
            return True

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
