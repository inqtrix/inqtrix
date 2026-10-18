"""Local email/password credentials: hashing, store port, authenticator.

Native auth for ``INQTRIX_AUTH_MODE=local``. The identity anchor is a
synthetic stable subject minted at creation — never the email (email is
mutable profile data, identity is not). Local accounts plug into the
exact session/CSRF/PAT/user-mirror machinery of the OIDC BFF under the
synthetic issuer ``"local"``. The principal ``kind`` stays
``"oidc_session"``; only the issuer distinguishes the transport.

Discipline mirrors :mod:`inqtrix.auth.pat`: ONE hashing definition, a
dummy-hash verify on the user-miss path so an unknown email costs the
same as a wrong password (timing uniformity), and a single uniform
:class:`CredentialError` so a caller can never tell which check failed.
The plaintext password is never stored, never logged, never returned.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from inqtrix.auth.log_redaction import pseudonymous_log_reference

log = logging.getLogger("inqtrix")

LOCAL_ISSUER = "local"
"""Synthetic issuer that anchors local accounts in the ``(issuer,
subject)`` identity space shared with sessions, PATs, and the user mirror."""

_HASHER = PasswordHasher()
"""argon2id with the library defaults (memory-hard). The single hashing
authority — every hash/verify in the codebase routes through here."""

# Verified against on the user-miss path so a request for an unknown email
# does roughly the same argon2 work as a wrong password (no email-existence
# oracle via timing). Mirrors pat.py's _DUMMY_DIGEST.
_DUMMY_HASH = _HASHER.hash("inqtrix-dummy-password-for-uniform-timing")


def hash_password(password: str) -> str:
    """Return the argon2id hash of *password* (never stores plaintext)."""
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Verify *password* against *stored_hash*, returning a bool.

    Never raises on mismatch or a malformed hash — those collapse to
    ``False`` so callers branch on a value, not an exception.
    """
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether *stored_hash* should be re-hashed with current parameters."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def new_subject() -> str:
    """A stable 128-bit synthetic subject id for a new local account."""
    return secrets.token_hex(16)


@dataclass(frozen=True)
class LocalCredential:
    """One local account (the credential record).

    Attributes:
        subject: Synthetic stable id; the identity anchor with
            :data:`LOCAL_ISSUER`. Never the email.
        email: Login email (matched case-insensitively).
        password_hash: argon2id hash; the plaintext is never stored.
        display_name: Optional human-readable name.
        created_at: Unix seconds.
        disabled_at: Soft-disable timestamp; a disabled account is
            denied at login. ``None`` means active.
    """

    user_id: uuid.UUID
    subject: str
    email: str
    password_hash: str
    display_name: str | None
    created_at: float
    disabled_at: float | None = None


class CredentialError(RuntimeError):
    """Uniform local-auth failure.

    Unknown email, wrong password, and disabled account all raise this
    single error with the same message so the client cannot distinguish
    them (no account-existence or status oracle).
    """


class CredentialStore(Protocol):
    """Port for local-credential persistence (memory or Postgres)."""

    async def count(self, *, tenant_id: str) -> int:
        """Number of accounts in the tenant (drives the owner-setup gate)."""
        ...

    async def create(
        self, credential: LocalCredential, *, tenant_id: str, allow_first_only: bool = False
    ) -> bool:
        """Insert one account.

        With ``allow_first_only=True`` the insert succeeds only when the
        tenant has no accounts yet (the race-safe owner bootstrap).
        Returns ``True`` on insert, ``False`` when refused (table not
        empty under ``allow_first_only``, or duplicate email).
        """
        ...

    async def get_by_email(
        self, *, tenant_id: str, email: str
    ) -> LocalCredential | None:
        """The account for *email* (case-insensitive), or ``None``."""
        ...

    async def get_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> LocalCredential | None:
        """The account for *subject*, or ``None``."""
        ...

    async def set_password(
        self, *, tenant_id: str, user_id: uuid.UUID, password_hash: str
    ) -> bool:
        """Replace the password hash; ``True`` when a row changed."""
        ...

    async def set_disabled(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float | None
    ) -> bool:
        """Set/clear the soft-disable timestamp; ``True`` when a row changed."""
        ...

    async def list(self, *, tenant_id: str) -> tuple[LocalCredential, ...]:
        """All accounts in the tenant (admin listing)."""
        ...


class MemoryCredentialStore:
    """Process-local credential store (zero-infrastructure default).

    Accounts do NOT survive a restart — the default is Postgres;
    this backend logs a warning when wired in local mode (No Silent
    Fallback) so an operator never mistakes it for durable storage.
    """

    def __init__(self) -> None:
        self._by_user: dict[tuple[str, uuid.UUID], LocalCredential] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _email_key(email: str) -> str:
        return email.strip().lower()

    async def count(self, *, tenant_id: str) -> int:
        with self._lock:
            return sum(1 for (t, _user_id) in self._by_user if t == tenant_id)

    async def create(
        self, credential: LocalCredential, *, tenant_id: str, allow_first_only: bool = False
    ) -> bool:
        with self._lock:
            if allow_first_only and any(
                t == tenant_id for (t, _user_id) in self._by_user
            ):
                return False
            email_key = self._email_key(credential.email)
            if any(
                t == tenant_id and self._email_key(c.email) == email_key
                for (t, _user_id), c in self._by_user.items()
            ):
                return False
            self._by_user[(tenant_id, credential.user_id)] = credential
            return True

    async def get_by_email(
        self, *, tenant_id: str, email: str
    ) -> LocalCredential | None:
        email_key = self._email_key(email)
        with self._lock:
            for (t, _user_id), c in self._by_user.items():
                if t == tenant_id and self._email_key(c.email) == email_key:
                    return c
        return None

    async def get_by_user_id(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> LocalCredential | None:
        with self._lock:
            return self._by_user.get((tenant_id, user_id))

    async def set_password(
        self, *, tenant_id: str, user_id: uuid.UUID, password_hash: str
    ) -> bool:
        with self._lock:
            existing = self._by_user.get((tenant_id, user_id))
            if existing is None:
                return False
            self._by_user[(tenant_id, user_id)] = replace(
                existing, password_hash=password_hash
            )
            return True

    async def set_disabled(
        self, *, tenant_id: str, user_id: uuid.UUID, disabled_at: float | None
    ) -> bool:
        with self._lock:
            existing = self._by_user.get((tenant_id, user_id))
            if existing is None:
                return False
            self._by_user[(tenant_id, user_id)] = replace(
                existing, disabled_at=disabled_at
            )
            return True

    async def list(self, *, tenant_id: str) -> tuple[LocalCredential, ...]:
        with self._lock:
            return tuple(
                c
                for (t, _user_id), c in self._by_user.items()
                if t == tenant_id
            )


class LocalAuthenticator:
    """Verifies an email/password against the credential store.

    Fail-closed and uniform: unknown email, wrong password, and disabled
    account all raise the same :class:`CredentialError`; the unknown-email
    path still burns one argon2 verify against :data:`_DUMMY_HASH` so it
    costs the same as a real attempt. Every rejection logs a WARNING for
    the operator (never a silent rejection) without naming the reason to
    the client.
    """

    def __init__(self, *, store: CredentialStore, tenant_id: str = "default") -> None:
        self._store = store
        self._tenant_id = tenant_id

    async def authenticate(self, email: str, password: str) -> LocalCredential:
        """Return the credential on success or raise :class:`CredentialError`."""
        credential = await self._store.get_by_email(
            tenant_id=self._tenant_id, email=email
        )
        if credential is None:
            # Spend a comparable amount of work so a missing account is
            # not distinguishable from a wrong password by timing.
            verify_password(_DUMMY_HASH, password)
            log.warning("Local login failed: unknown email.")
            raise CredentialError("Invalid credentials.")
        if not verify_password(credential.password_hash, password):
            log.warning(
                "Local login failed: wrong password for actor_ref=%s.",
                pseudonymous_log_reference("usr", credential.user_id),
            )
            raise CredentialError("Invalid credentials.")
        if credential.disabled_at is not None:
            log.warning(
                "Local login denied: account disabled actor_ref=%s.",
                pseudonymous_log_reference("usr", credential.user_id),
            )
            raise CredentialError("Invalid credentials.")
        return credential
