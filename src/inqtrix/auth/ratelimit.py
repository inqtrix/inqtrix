"""Login brute-force throttling for the password modes (local).

A small sliding-window-plus-lockout limiter keyed on ``(identifier,
source_ip)``: after too many FAILED attempts inside the window the key is
locked for a cooldown. The uniform-timing credential check already denies an
email-enumeration oracle; this caps the guess RATE once an email is known.

Constructor-first configuration: thresholds arrive as arguments and the
clock is injectable for tests. The default backend is process-local in-memory
— correct for a single-node deployment; a multi-replica deployment should add
per-IP throttling at the reverse proxy / WAF (documented in
docs/deployment/production-checklist.md), since the in-memory counters are not
shared across replicas. The limiter is a Protocol so a Redis-backed
implementation can drop in later without touching the routes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Protocol


class LoginRateLimiter(Protocol):
    """Port for login throttling (memory default; swappable)."""

    def locked(self, key: str) -> bool:
        """Whether *key* is currently in a lockout window."""
        ...

    def record_failure(self, key: str) -> None:
        """Record one failed attempt for *key* (may start a lockout)."""
        ...

    def reset(self, key: str) -> None:
        """Clear *key*'s failure history (called on a successful login)."""
        ...


@dataclass
class _KeyState:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class MemoryLoginRateLimiter:
    """Process-local sliding-window limiter with a lockout.

    Memory is bounded two ways so a flood of distinct keys (an attacker can
    mint them by rotating the identifier, or the source IP when
    ``trusted_proxy_hops`` is misconfigured too high — see :func:`client_ip`)
    cannot grow the table without limit: every
    write opportunistically drops keys whose window AND lockout have both
    elapsed, and a hard ``max_keys`` ceiling evicts the least-recently-touched
    entry beyond that. The throttle is a best-effort defence anyway (the
    per-request argon2 cost is the real rate gate, and multi-replica
    deployments add edge throttling) — so evicting a stale or oldest key never
    weakens a real lockout in practice.

    Args:
        max_attempts: Failed attempts within *window_seconds* that trip a
            lockout. Must be >= 1.
        window_seconds: Rolling window over which failures accumulate.
        lockout_seconds: How long a tripped key stays locked.
        max_keys: Hard ceiling on tracked keys (oldest evicted past it).
        clock: Monotonic time source (injected for deterministic tests).
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: float,
        lockout_seconds: float,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._entries: "OrderedDict[str, _KeyState]" = OrderedDict()
        self._lock = threading.Lock()

    def _is_stale(self, state: _KeyState, now: float) -> bool:
        """A key with no in-window failures and no live lockout is forgettable."""
        if state.locked_until > now:
            return False
        cutoff = now - self._window
        return not any(stamp > cutoff for stamp in state.failures)

    def _sweep(self, now: float) -> None:
        """Drop every fully-elapsed key. Caller holds the lock."""
        for key in [k for k, s in self._entries.items() if self._is_stale(s, now)]:
            del self._entries[key]

    def locked(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            state = self._entries.get(key)
            if state is None:
                return False
            if state.locked_until > now:
                self._entries.move_to_end(key)
                return True
            # Not (or no longer) locked. Forget the key ONLY when it is fully
            # stale — a not-yet-locked key still accumulating in-window
            # failures must keep its history (else each lock check would reset
            # the counter and the threshold would never be reached).
            if self._is_stale(state, now):
                del self._entries[key]
            return False

    def record_failure(self, key: str) -> None:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._sweep(now)
            state = self._entries.get(key) or _KeyState()
            state.failures = [s for s in state.failures if s > cutoff]
            state.failures.append(now)
            if len(state.failures) >= self._max_attempts:
                state.locked_until = now + self._lockout
            self._entries[key] = state
            self._entries.move_to_end(key)
            # Hard ceiling: evict the least-recently-touched keys.
            while len(self._entries) > self._max_keys:
                self._entries.popitem(last=False)

    def reset(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)


_UNKNOWN_CLIENT = "?"


def client_ip(request, trusted_proxy_hops: int = 1) -> str:
    """Best-effort client IP for throttling, honouring a trusted-proxy depth.

    ``X-Forwarded-For`` is a chain each proxy APPENDS its own peer to; the
    left-most entries are whatever the original client sent and are therefore
    attacker-controlled. Reading the left-most hop (the historical behaviour)
    let a client mint a fresh throttle key per request by rotating a spoofed
    value, defeating the login lockout entirely. Both supported web adapters
    (the packaged Python gateway and the explicit nginx alternative) APPEND
    the real peer on the right and strip nothing, so the trustworthy value is
    the hop our own infrastructure wrote, counted from the right.

    Args:
        request: The incoming Starlette/FastAPI request.
        trusted_proxy_hops: Number of reverse proxies between the client and
            this server that append to ``X-Forwarded-For``. ``1`` (default)
            fits the single bundled proxy: the right-most hop is the real
            client and is not client-spoofable. ``0`` means the server is
            exposed directly with no trusted proxy — every forwarded header
            is ignored and only the socket peer is used. ``n`` trusts the
            right-most ``n`` hops as infrastructure and reads the client from
            ``chain[-n]``. Set it to EXACTLY the number of proxies in front:
            a value larger than the real chain lets a client backfill the
            gap and spoof again.

    Returns:
        The resolved client IP string, or ``"?"`` when no address is
        available. Used only as a throttle key, never for authorization, and
        the limiter is memory-bounded, so even a spoofed value at worst
        throttles the spoofer and is evicted; it is no amplification
        primitive.
    """
    peer = getattr(request, "client", None)
    peer_host = peer.host if peer is not None else None
    if trusted_proxy_hops < 1:
        return peer_host or _UNKNOWN_CLIENT
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if len(hops) >= trusted_proxy_hops:
        return hops[-trusted_proxy_hops]
    # Chain shorter than the declared trusted depth (header missing, or fewer
    # real proxies than configured): fail safe to the socket peer rather than
    # trust a possibly client-supplied value.
    return peer_host or _UNKNOWN_CLIENT
