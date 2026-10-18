"""Shared brute-force throttling for password-protected editor guest links."""

from __future__ import annotations

from typing import Any, Protocol


class GuestLinkRateLimiter(Protocol):
    async def locked(self, key: str) -> bool: ...

    async def record_failure(self, key: str) -> None: ...

    async def reset(self, key: str) -> None: ...


class ValkeyGuestLinkRateLimiter:
    """Small atomic Valkey limiter shared by every API replica."""

    _RECORD_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if count >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
  redis.call('DEL', KEYS[1])
end
return count
"""

    def __init__(
        self,
        *,
        url: str,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
        client: Any | None = None,
    ) -> None:
        if not url.strip() and client is None:
            raise ValueError("A Valkey URL or client is required")
        if min(max_attempts, window_seconds, lockout_seconds) < 1:
            raise ValueError("Guest-link rate-limit values must be positive")
        if client is None:
            import valkey.asyncio as valkey

            client = valkey.Valkey.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        self._client = client
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds

    @staticmethod
    def _keys(key: str) -> tuple[str, str]:
        return (
            f"inqtrix:editor-guest:failures:{key}",
            f"inqtrix:editor-guest:locked:{key}",
        )

    async def locked(self, key: str) -> bool:
        _failures, locked = self._keys(key)
        return bool(await self._client.exists(locked))

    async def record_failure(self, key: str) -> None:
        failures, locked = self._keys(key)
        await self._client.eval(
            self._RECORD_SCRIPT,
            2,
            failures,
            locked,
            self._window_seconds,
            self._max_attempts,
            self._lockout_seconds,
        )

    async def reset(self, key: str) -> None:
        failures, locked = self._keys(key)
        await self._client.delete(failures, locked)
