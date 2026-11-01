"""Signing keys for token checks, shared by the OIDC and the gateway mode.

One cache, two lifetimes: the OIDC client holds its keys for six hours, the
gateway for one. An unknown key id refreshes the set at most once per cooldown
for the whole cache; a cooldown per id would let a stream of random ids trigger
a fetch each. Concurrent refreshes collapse into one fetch. A failed refresh
keeps the known keys in use up to a maximum age, says so in a WARNING, and is
remembered with a backoff instead of being retried on every request. A key
file is read once at start and never refreshed. Key ids reach the log cut to
:data:`KID_LOG_LIMIT` characters.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from joserfc.jwk import KeySet

from inqtrix.observability.metrics_defs import active_metrics

log = logging.getLogger(__name__)

OIDC_JWKS_TTL_SECONDS = 6 * 3600.0
GATEWAY_JWKS_TTL_SECONDS = 3600.0
JWKS_REFRESH_COOLDOWN_SECONDS = 300.0
JWKS_MAX_STALE_SECONDS = 24 * 3600.0
JWKS_FAILURE_BACKOFF_SECONDS = 30.0
KID_LOG_LIMIT = 64


def fetched_without_tls(url: str) -> bool:
    """Whether *url* is set and fetches signing keys over anything but https.

    The keys prove every sign-in, so whoever controls a plain-text path to
    them can sign for any person; the start warns about it.
    """
    value = url.strip()
    return bool(value) and urllib.parse.urlsplit(value).scheme.lower() != "https"


class KeysUnavailable(RuntimeError):
    """No usable key set for a token.

    Raised when no key set could be loaded for an unknown key id, or when the
    held set is older than the maximum age.
    """


def token_kid(token: str) -> str | None:
    """The ``kid`` of a compact JWS header, or ``None`` when it has none."""
    try:
        header_segment = token.split(".")[0]
        padded = header_segment + "=" * (-len(header_segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 -- a malformed token fails its own check later
        return None
    kid = header.get("kid") if isinstance(header, dict) else None
    return kid if isinstance(kid, str) else None


def keyset_has_kid(keys: KeySet, kid: str) -> bool:
    """Whether the key set holds a key with this id."""
    return any(key.kid == kid for key in keys.keys)


class JwksCache:
    """The key set of a JWKS endpoint with the refresh rules of this module.

    Args:
        fetch: Returns the JWKS document of the endpoint.
        cache: Fixed name of this cache in the metrics (``oidc`` or
            ``gateway``).
        ttl_seconds: How long a fetched set counts as current.
        refresh_cooldown_seconds: Least time between two refreshes for an
            unknown key id, for the whole cache.
        max_stale_seconds: How long a set stays usable when refreshing fails.
        failure_backoff_seconds: Least time between two failing refreshes.
        clock: Monotonic clock (tests pass their own).
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        cache: str,
        ttl_seconds: float,
        refresh_cooldown_seconds: float = JWKS_REFRESH_COOLDOWN_SECONDS,
        max_stale_seconds: float = JWKS_MAX_STALE_SECONDS,
        failure_backoff_seconds: float = JWKS_FAILURE_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._cache = cache
        self.ttl_seconds = ttl_seconds
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._max_stale_seconds = max_stale_seconds
        self._failure_backoff_seconds = failure_backoff_seconds
        self._clock = clock
        self._keys: KeySet | None = None
        self._fetched_at = 0.0
        self._failed_at: float | None = None
        self._last_unknown_refresh: float | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    async def keys_for(self, token: str) -> KeySet:
        """The key set to verify this token with.

        Raises:
            KeysUnavailable: No set could be loaded for an unknown key id, or
                the held set is older than the maximum age.
        """
        kid = token_kid(token)
        generation = self._generation
        if self._keys is None or self._clock() - self._fetched_at >= self.ttl_seconds:
            await self._refresh(seen_generation=generation)
        keys = self._usable_keys()
        if kid is None or keyset_has_kid(keys, kid):
            return keys
        if self._lock.locked():
            # A refresh is in flight; its result decides for this token too.
            async with self._lock:
                pass
            return self._usable_keys()
        now = self._clock()
        if (
            self._last_unknown_refresh is not None
            and now - self._last_unknown_refresh < self._refresh_cooldown_seconds
        ):
            return keys
        self._last_unknown_refresh = now
        log.warning(
            "JWKS: unknown kid %r; refreshing the key set once (possible key rotation).",
            kid[:KID_LOG_LIMIT],
        )
        refreshed = await self._refresh(seen_generation=self._generation)
        keys = self._usable_keys()
        if not refreshed and not keyset_has_kid(keys, kid):
            raise KeysUnavailable("no key set could be loaded for an unknown key id")
        return keys

    async def _refresh(self, *, seen_generation: int) -> bool:
        """Fetch the set once for all waiting callers; ``True`` when it loaded."""
        async with self._lock:
            if self._generation != seen_generation:
                return True
            now = self._clock()
            if self._failed_at is not None and now - self._failed_at < self._failure_backoff_seconds:
                return False
            try:
                document = await self._fetch()
                keys = KeySet.import_key_set(dict(document))
            except Exception as error:  # noqa: BLE001 -- every failure keeps the known keys
                self._failed_at = now
                metrics = active_metrics()
                if metrics is not None:
                    metrics.count_jwks_refresh_failure(self._cache)
                log.warning(
                    "JWKS refresh failed (%s); known keys stay in use until they are %d s old.",
                    type(error).__name__,
                    int(self._max_stale_seconds),
                )
                return False
            self._keys = keys
            self._fetched_at = now
            self._failed_at = None
            self._generation += 1
            return True

    async def aclose(self) -> None:
        """Release the HTTP client of the endpoint, when it has one."""
        closer = getattr(self._fetch, "aclose", None)
        if closer is not None:
            await closer()

    def _usable_keys(self) -> KeySet:
        if self._keys is None:
            raise KeysUnavailable("no key set has been loaded")
        if self._clock() - self._fetched_at > self._max_stale_seconds:
            raise KeysUnavailable("the key set is older than the maximum age")
        return self._keys


class StaticKeys:
    """A key set read once, from a key file; never refreshed.

    A token whose key id is not in the set fails its signature check; it never
    triggers a refresh or a ``keys_unavailable`` answer.
    """

    def __init__(self, keys: KeySet) -> None:
        self._keys = keys

    async def keys_for(self, token: str) -> KeySet:
        """The fixed key set, whatever the token's key id."""
        return self._keys


def load_key_file(path: str | Path) -> KeySet:
    """Read a JWK Set file with one or more public keys, each with a ``kid``.

    Raises:
        ValueError: The file is unreadable, not a JWK Set, holds a key without
            ``kid`` or one that cannot be imported, or holds private material.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the key file as JSON ({type(error).__name__})") from None
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys:
        raise ValueError("the key file is not a JWK Set with at least one key under 'keys'")
    if any(not isinstance(key, dict) or not key.get("kid") for key in keys):
        raise ValueError("every key in the key file needs a 'kid'")
    try:
        key_set = KeySet.import_key_set({"keys": keys})
    except Exception as error:  # noqa: BLE001 -- the start check names the class only
        raise ValueError(f"the key file holds a key that cannot be imported ({type(error).__name__})") from None
    if any(key.is_private for key in key_set.keys):
        raise ValueError("the key file holds private key material; only public keys belong there")
    return key_set
