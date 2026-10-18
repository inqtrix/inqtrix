"""Server-side session and login-flow state for the OIDC BFF.

Two small stores behind ports (Baukasten):

* :class:`SessionStore` — authenticated browser sessions. The cookie
  carries only an opaque random id; claims and the CSRF secret live
  server-side, tokens are never stored at all (the BFF validates the
  id_token at login and discards it — Inqtrix calls no upstream APIs
  on the user's behalf).
* :class:`FlowStore` — transient login transactions keyed by the
  OAuth ``state`` value (PKCE verifier, nonce, post-login redirect).
  Consumption is strictly one-time: a replayed callback fails on the
  missing flow record, independent of the IdP's code single-use.

Memory implementations are the zero-infrastructure default (single
process); the Postgres implementations under
:mod:`inqtrix.storage.auth_postgres` make login work across API
replicas.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

FLOW_TTL_SECONDS = 600.0
"""Login transactions expire after ten minutes — long enough for a
password prompt plus MFA, short enough to bound replay windows."""


@dataclass(frozen=True)
class AuthSession:
    """One authenticated browser session (server-side record).

    Attributes:
        id: Opaque random identifier; the only value the cookie holds.
        user_id: Canonical local ``users.id`` UUID and the only
            authorization identity carried by the session.
        issuer: Issuer of the external login binding.
        subject: External provider subject retained only for profile refresh
            and diagnostics; it is never an authorization key.
        email: Email claim at login time (profile data, not identity).
        display_name: Resolved display username.
        groups: Group claims at login time.
        csrf_random: Per-session random half of the signed
            double-submit CSRF token.
        created_at: Unix seconds.
        expires_at: Absolute expiry (unix seconds); expired sessions
            resolve to 401.
    """

    id: str
    user_id: uuid.UUID
    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    groups: tuple[str, ...]
    csrf_random: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class LoginFlow:
    """One in-flight authorization-code transaction.

    Attributes:
        state: OAuth ``state`` value — the lookup key, single-use.
        code_verifier: PKCE verifier for the token exchange.
        nonce: OIDC nonce the returned id_token must echo.
        next_path: Relative SPA path to land on after login.
        expires_at: Absolute expiry (unix seconds).
    """

    state: str
    code_verifier: str
    nonce: str
    next_path: str
    expires_at: float


class SessionStore(Protocol):
    """Port for authenticated session persistence."""

    async def create(self, session: AuthSession) -> None:
        """Persist a new session record."""
        ...

    async def get(self, session_id: str) -> AuthSession | None:
        """Return the live session or ``None`` (absent or expired)."""
        ...

    async def delete(self, session_id: str) -> None:
        """Remove a session; missing ids are a no-op (idempotent logout)."""
        ...

    async def delete_for_user(self, *, user_id: uuid.UUID) -> int:
        """Purge every session of one identity (the disable/cut-off path).

        Returns the number of sessions removed. Used when an admin
        disables a user so live sessions die immediately instead of
        lingering until natural expiry.
        """
        ...


class FlowStore(Protocol):
    """Port for transient login-flow persistence."""

    async def put(self, flow: LoginFlow) -> None:
        """Persist a new flow record keyed by its state value."""
        ...

    async def consume(self, state: str) -> LoginFlow | None:
        """Atomically take the flow; ``None`` when absent, expired,
        or already consumed (replay defense)."""
        ...


def new_session_id() -> str:
    """256-bit URL-safe random session identifier."""
    return secrets.token_urlsafe(32)


class MemorySessionStore:
    """Process-local session store (zero-infrastructure default)."""

    def __init__(self) -> None:
        self._sessions: dict[str, AuthSession] = {}
        self._lock = threading.RLock()

    async def create(self, session: AuthSession) -> None:
        with self._lock:
            self._cleanup_locked()
            self._sessions[session.id] = session

    async def get(self, session_id: str) -> AuthSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= time.time():
                del self._sessions[session_id]
                return None
            return session

    async def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    async def delete_for_user(self, *, user_id: uuid.UUID) -> int:
        with self._lock:
            doomed = [
                session_id
                for session_id, session in self._sessions.items()
                if session.user_id == user_id
            ]
            for session_id in doomed:
                del self._sessions[session_id]
        return len(doomed)

    def _cleanup_locked(self) -> None:
        now = time.time()
        for session_id in [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]:
            del self._sessions[session_id]


class MemoryFlowStore:
    """Process-local flow store (zero-infrastructure default)."""

    def __init__(self) -> None:
        self._flows: dict[str, LoginFlow] = {}
        self._lock = threading.Lock()

    async def put(self, flow: LoginFlow) -> None:
        with self._lock:
            now = time.time()
            for state in [
                state
                for state, item in self._flows.items()
                if item.expires_at <= now
            ]:
                del self._flows[state]
            self._flows[flow.state] = flow

    async def consume(self, state: str) -> LoginFlow | None:
        with self._lock:
            flow = self._flows.pop(state, None)
        if flow is None or flow.expires_at <= time.time():
            return None
        return flow
