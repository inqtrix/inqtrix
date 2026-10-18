"""Personal access tokens: machine credentials for the oidc mode.

Browser sessions cannot serve scripts, CI pipelines, or API clients —
a PAT is the per-user Bearer credential that fills that gap without
falling back to a shared static key (which would erase attribution
and per-client revocation).

Format and storage (binding design decisions):

* Plaintext shape ``ipat_<token_id>_<secret>``. The token id is hex
  on purpose — ``token_urlsafe`` may emit underscores, which would
  make the ``_``-separated format ambiguous; the secret MAY contain
  underscores because parsing splits exactly once from the left.
* The token id doubles as the public identifier (URL path parameter,
  :attr:`~inqtrix.auth.principal.Principal.pat_id`, audit resource
  id) and the primary key — no second surrogate id.
* Only ``HMAC-SHA256(pepper, secret)`` is stored. The pepper lives in
  the server environment, so a database leak alone does not allow
  offline verification of guesses. Rotating the pepper invalidates
  every token at once.
* Verification returns uniform 401s: malformed, unknown, wrong
  secret, expired, and revoked are indistinguishable to the caller;
  differentiation happens in server logs and the audit trail only.

Memory store is the zero-infrastructure default (process-local, with
the explicit caveat that tokens evaporate on restart); the Postgres
store under :mod:`inqtrix.storage.pat_postgres` makes tokens durable
and replica-safe.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.auth.principal import Principal

if TYPE_CHECKING:
    from inqtrix.auth.directory import UserDirectory
    from inqtrix.auth.permissions import AuditSink

log = logging.getLogger("inqtrix")

PAT_PREFIX = "ipat_"

LAST_USED_WRITE_INTERVAL_SECONDS = 300.0
"""Throttle for ``last_used_at`` writes: at most one update per token
per five minutes. The timestamp is operator forensics ("is this token
still in use?"), not an audit log — per-request writes would turn
every verification into a hot-row update."""

_UNAUTHORIZED_DETAIL = {
    "error": {
        "message": "Invalid access token",
        "type": "unauthorized",
    }
}

_DUMMY_DIGEST = hashlib.sha256(b"inqtrix-pat-dummy").hexdigest()
"""Compared against on unknown token ids so the row-miss path costs
one HMAC comparison like the row-hit path (timing uniformity)."""


@dataclass(frozen=True)
class PersonalAccessToken:
    """One stored token record (never carries the plaintext secret).

    Attributes:
        token_id: Public identifier AND primary key (hex).
        tenant_id: Tenant the token belongs to.
        owner_user_id: Canonical UUID of the owning user; verified requests
            act as this principal. External issuer/subject identifiers never
            enter the token contract.
        name: Operator-chosen label ("ci-runner"), display only.
        secret_hmac: Hex ``HMAC-SHA256(pepper, secret)``.
        created_at: Unix seconds.
        expires_at: Absolute expiry; ``None`` never expires.
        last_used_at: Throttled usage timestamp (forensics).
        revoked_at: Soft-revocation timestamp; revoked rows stay for
            audit but never verify again.
        scopes: Reserved capability scopes. Empty means full access of
            the owner's role — scopes are stored but not enforced; the
            storage shape exists so narrowing tokens later needs no
            migration.
    """

    token_id: str
    tenant_id: str
    owner_user_id: uuid.UUID
    name: str
    secret_hmac: str
    created_at: float
    expires_at: float | None
    last_used_at: float | None
    revoked_at: float | None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MintedPat:
    """Creation result: the record plus the ONE plaintext emission."""

    record: PersonalAccessToken
    plaintext: str


class PatLimitExceeded(Exception):
    """Raised when a user already holds the configured token maximum."""


def mint_pat_credentials() -> tuple[str, str]:
    """Generate ``(token_id, secret)`` for a new token.

    The id is 16 hex chars (64 bits — collision-safe at any realistic
    token count, and free of the ``_`` separator); the secret carries
    256 bits of entropy.
    """
    return secrets.token_hex(8), secrets.token_urlsafe(32)


def parse_pat(value: str) -> tuple[str, str] | None:
    """Split a presented token into ``(token_id, secret)``.

    Splits exactly once from the left so secrets containing
    underscores survive; any shape violation returns ``None`` (the
    caller maps that to the uniform 401).
    """
    if not value.startswith(PAT_PREFIX):
        return None
    remainder = value[len(PAT_PREFIX):]
    token_id, separator, secret = remainder.partition("_")
    if not separator or not token_id or not secret:
        return None
    return token_id, secret


def hash_pat_secret(pepper: str, secret: str) -> str:
    """The ONE peppered-HMAC definition (mint and verify share it)."""
    return hmac.new(
        pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def format_pat(token_id: str, secret: str) -> str:
    """Assemble the plaintext shape ``ipat_<token_id>_<secret>``."""
    return f"{PAT_PREFIX}{token_id}_{secret}"


class PatStore(Protocol):
    """Persistence port for personal access tokens."""

    @property
    def atomic_audit_effects(self) -> bool:
        """Whether create/use/revoke persist their audit rows atomically."""
        ...

    async def create(self, token: PersonalAccessToken) -> None: ...

    async def get(self, token_id: str) -> PersonalAccessToken | None: ...

    async def list_for_owner(
        self, *, tenant_id: str, owner_user_id: uuid.UUID
    ) -> tuple[PersonalAccessToken, ...]:
        """Non-revoked tokens of one owner, newest first."""
        ...

    async def revoke(
        self,
        *,
        tenant_id: str,
        token_id: str,
        owner_user_id: uuid.UUID,
        now: float,
    ) -> bool:
        """Guarded soft-revoke; ``True`` only when a LIVE row flipped.

        The owner columns are part of the guard so one user can never
        revoke another user's token by id.
        """
        ...

    async def touch_last_used(
        self, token_id: str, *, now: float, min_interval: float
    ) -> bool:
        """Throttled ``last_used_at`` update.

        Returns:
            ``True`` when the write actually happened — the sampling
            signal the ``pat.used`` audit row keys off (one row per
            throttle interval, matching the persisted timestamp).

        Durable stores may raise when the timestamp and its atomic audit row
        cannot both commit. :class:`PatVerifier` treats that bookkeeping
        failure as fail-safe while retaining the rollback.
        """
        ...

    async def revoke_all_for_owner(
        self, *, tenant_id: str, owner_user_id: uuid.UUID, now: float
    ) -> int:
        """Disable-cascade helper; returns the number of revoked rows."""
        ...


class MemoryPatStore:
    """Process-local token store (zero-infrastructure default).

    Tokens do not survive a restart — the composition root logs a
    visible warning when wiring this store in oidc mode.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, PersonalAccessToken] = {}
        self._lock = threading.RLock()

    @property
    def atomic_audit_effects(self) -> bool:
        """Memory mutations use the service/verifier audit fallback."""
        return False

    async def create(self, token: PersonalAccessToken) -> None:
        with self._lock:
            self._tokens[token.token_id] = token

    async def get(self, token_id: str) -> PersonalAccessToken | None:
        with self._lock:
            return self._tokens.get(token_id)

    async def list_for_owner(
        self, *, tenant_id: str, owner_user_id: uuid.UUID
    ) -> tuple[PersonalAccessToken, ...]:
        with self._lock:
            owned = [
                token
                for token in self._tokens.values()
                if token.tenant_id == tenant_id
                and token.owner_user_id == owner_user_id
                and token.revoked_at is None
            ]
        return tuple(
            sorted(owned, key=lambda token: token.created_at, reverse=True)
        )

    async def revoke(
        self,
        *,
        tenant_id: str,
        token_id: str,
        owner_user_id: uuid.UUID,
        now: float,
    ) -> bool:
        with self._lock:
            token = self._tokens.get(token_id)
            if (
                token is None
                or token.tenant_id != tenant_id
                or token.owner_user_id != owner_user_id
                or token.revoked_at is not None
            ):
                return False
            self._tokens[token_id] = dataclasses.replace(
                token, revoked_at=now
            )
            return True

    async def touch_last_used(
        self, token_id: str, *, now: float, min_interval: float
    ) -> bool:
        with self._lock:
            token = self._tokens.get(token_id)
            if token is None:
                return False
            if (
                token.last_used_at is not None
                and token.last_used_at > now - min_interval
            ):
                return False
            self._tokens[token_id] = dataclasses.replace(
                token, last_used_at=now
            )
            return True

    async def revoke_all_for_owner(
        self, *, tenant_id: str, owner_user_id: uuid.UUID, now: float
    ) -> int:
        with self._lock:
            revoked = 0
            for token_id, token in list(self._tokens.items()):
                if (
                    token.tenant_id == tenant_id
                    and token.owner_user_id == owner_user_id
                    and token.revoked_at is None
                ):
                    self._tokens[token_id] = dataclasses.replace(
                        token, revoked_at=now
                    )
                    revoked += 1
            return revoked


class PatVerifier:
    """Hot-path Bearer verification: plaintext in, Principal out.

    Args:
        store: The token persistence backend.
        pepper: Server-side HMAC pepper; empty is a wiring error and
            rejected here (the settings layer already enforces it for
            oidc mode — this guard catches direct construction).
        audit: Optional audit sink for BOUNDED rejection events (a
            correct secret on a revoked/expired row). Garbage tokens
            never reach the audit table — they are attacker-controlled
            volume and stay at log level.
    """

    def __init__(
        self,
        *,
        store: PatStore,
        pepper: str,
        user_lookup: "UserDirectory",
        audit: "AuditSink | None" = None,
    ) -> None:
        if not pepper.strip():
            raise ValueError(
                "PatVerifier requires a non-empty pepper."
            )
        self._store = store
        self._pepper = pepper
        self._user_lookup = user_lookup
        self._audit = audit

    def bind_audit(self, audit: "AuditSink") -> None:
        """Bind the one canonical sink after composition is assembled."""
        if self._audit is not None and self._audit is not audit:
            raise RuntimeError(
                "PatVerifier audit sink cannot be rebound to another instance"
            )
        self._audit = audit

    async def verify(self, bearer_value: str) -> Principal:
        """Verify one presented token or raise the uniform 401."""
        parsed = parse_pat(bearer_value.strip())
        if parsed is None:
            log.warning("PAT rejected: invalid token format.")
            raise self._unauthorized()
        token_id, secret = parsed
        record = await self._store.get(token_id)
        presented = hash_pat_secret(self._pepper, secret)
        if record is None:
            # Burn one comparison so row-miss and hash-miss timing
            # converge; the existence of a token id is low-value, the
            # mitigation is one line.
            hmac.compare_digest(presented, _DUMMY_DIGEST)
            log.warning(
                "PAT rejected: unknown token reference %s.",
                pseudonymous_log_reference("res", token_id),
            )
            raise self._unauthorized()
        if not hmac.compare_digest(presented, record.secret_hmac):
            log.warning(
                "PAT rejected: incorrect secret for token reference %s.",
                pseudonymous_log_reference("res", token_id),
            )
            raise self._unauthorized()
        now = time.time()
        if record.revoked_at is not None:
            await self._audit_rejection(record, "revoked")
            raise self._unauthorized()
        if record.expires_at is not None and record.expires_at <= now:
            await self._audit_rejection(record, "expired")
            raise self._unauthorized()
        user = await self._user_lookup.find_by_user_id(
            tenant_id=record.tenant_id, user_id=record.owner_user_id
        )
        if user is None or user.disabled_at is not None:
            await self._audit_rejection(record, "user_disabled")
            raise self._unauthorized()
        try:
            touched = await self._store.touch_last_used(
                record.token_id,
                now=now,
                min_interval=LAST_USED_WRITE_INTERVAL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — bookkeeping only
            touched = False
            log.warning(
                "PAT last_used update failed (error_type=%s); the request "
                "continues.",
                type(exc).__name__,
            )
        if (
            touched
            and self._audit is not None
            and not self._store.atomic_audit_effects
        ):
            # pat.used, SAMPLED on the same throttle as last_used_at:
            # one audit row per interval, never one per request.
            from inqtrix.services.audit_service import (
                AuditService,
                build_audit_entry,
            )

            await AuditService(self._audit).record(
                build_audit_entry(
                    tenant_id=record.tenant_id,
                    actor_user_id=record.owner_user_id,
                    action="pat.used",
                    resource_type="pat",
                    resource_id=record.token_id,
                    origin={"auth_method": "pat"},
                )
            )
        return Principal(
            user_id=record.owner_user_id,
            kind="pat",
            tenant_id=record.tenant_id,
            scopes=frozenset(record.scopes),
            pat_id=record.token_id,
        )

    async def _audit_rejection(
        self, record: PersonalAccessToken, reason: str
    ) -> None:
        """Bounded audit: only correct-secret-but-dead tokens land here."""
        log.warning(
            "PAT rejected: token reference %s is %s.",
            pseudonymous_log_reference("res", record.token_id),
            reason,
        )
        if self._audit is None:
            return
        from inqtrix.auth.permissions import AuditEntry

        await self._audit.record(
            AuditEntry(
                tenant_id=record.tenant_id,
                actor_user_id=record.owner_user_id,
                action="pat.rejected",
                resource_type="pat",
                resource_id=record.token_id,
                detail={"reason": reason},
            )
        )

    @staticmethod
    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=401,
            detail=_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )


class PatService:
    """Management surface behind the ``/api/auth/tokens`` routes.

    Args:
        store: The token persistence backend (shared with the
            verifier — one instance, no split brain).
        pepper: Server-side HMAC pepper.
        audit: Audit sink for create/revoke events.
        max_per_user: Active-token cap per owner (guardrail).
        default_ttl_days: Default lifetime for tokens created without
            an explicit expiry; ``0`` means non-expiring.
    """

    def __init__(
        self,
        *,
        store: PatStore,
        pepper: str,
        audit: "AuditSink | None" = None,
        max_per_user: int = 10,
        default_ttl_days: int = 0,
    ) -> None:
        if not pepper.strip():
            raise ValueError("PatService requires a non-empty pepper.")
        self._store = store
        self._pepper = pepper
        self._audit = audit
        self._max_per_user = max_per_user
        self._default_ttl_days = default_ttl_days

    def bind_audit(self, audit: "AuditSink") -> None:
        """Bind the one canonical sink after composition is assembled."""
        if self._audit is not None and self._audit is not audit:
            raise RuntimeError(
                "PatService audit sink cannot be rebound to another instance"
            )
        self._audit = audit

    async def create_token(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID,
        name: str,
        expires_in_days: int | None = None,
    ) -> MintedPat:
        """Mint one token; the plaintext exists only in the return value.

        Raises:
            PatLimitExceeded: When the owner already holds
                ``max_per_user`` active tokens. Count-then-insert is a
                guardrail: concurrent creates across replicas may
                briefly overshoot, which is documented and acceptable
                for a sprawl cap.
        """
        existing = await self._store.list_for_owner(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        now = time.time()
        active = [
            token
            for token in existing
            if token.expires_at is None or token.expires_at > now
        ]
        if len(active) >= self._max_per_user:
            raise PatLimitExceeded(str(self._max_per_user))
        ttl_days = (
            expires_in_days
            if expires_in_days is not None
            else (self._default_ttl_days or None)
        )
        token_id, secret = mint_pat_credentials()
        record = PersonalAccessToken(
            token_id=token_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            secret_hmac=hash_pat_secret(self._pepper, secret),
            created_at=now,
            expires_at=(
                now + ttl_days * 86_400.0 if ttl_days is not None else None
            ),
            last_used_at=None,
            revoked_at=None,
        )
        await self._store.create(record)
        await self._audit_event(record, "pat.created")
        return MintedPat(record=record, plaintext=format_pat(token_id, secret))

    async def list_tokens(
        self, *, tenant_id: str, owner_user_id: uuid.UUID
    ) -> tuple[PersonalAccessToken, ...]:
        return await self._store.list_for_owner(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )

    async def revoke_all_for_owner(
        self, *, tenant_id: str, owner_user_id: uuid.UUID
    ) -> int:
        """Revoke every live token of one owner (admin disable cut-off)."""
        return await self._store.revoke_all_for_owner(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            now=time.time(),
        )

    async def revoke_token(
        self,
        *,
        tenant_id: str,
        token_id: str,
        owner_user_id: uuid.UUID,
    ) -> bool:
        revoked = await self._store.revoke(
            tenant_id=tenant_id,
            token_id=token_id,
            owner_user_id=owner_user_id,
            now=time.time(),
        )
        if revoked:
            await self._audit_event(
                PersonalAccessToken(
                    token_id=token_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    name="",
                    secret_hmac="",
                    created_at=0.0,
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                ),
                "pat.revoked",
            )
        return revoked

    async def _audit_event(
        self, record: PersonalAccessToken, action: str
    ) -> None:
        if self._audit is None or self._store.atomic_audit_effects:
            return
        from inqtrix.auth.permissions import AuditEntry

        await self._audit.record(
            AuditEntry(
                tenant_id=record.tenant_id,
                actor_user_id=record.owner_user_id,
                action=action,
                resource_type="pat",
                resource_id=record.token_id,
                detail={},
            )
        )
