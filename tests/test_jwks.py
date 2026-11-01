"""The signing-key cache shared by the OIDC and the gateway token checks.

A known key id is served from the cache; an unknown one triggers at most one
refresh per cooldown for the whole cache, concurrent refreshes collapse into
one fetch, a failed refresh keeps the known keys up to a maximum age with a
WARNING, and the failure is remembered instead of retried on every request.
A key file is read once and never refreshed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey

from inqtrix.auth.jwks import (
    GATEWAY_JWKS_TTL_SECONDS,
    JWKS_MAX_STALE_SECONDS,
    JWKS_REFRESH_COOLDOWN_SECONDS,
    KID_LOG_LIMIT,
    OIDC_JWKS_TTL_SECONDS,
    JwksCache,
    KeysUnavailable,
    StaticKeys,
    load_key_file,
)

KEY_A = RSAKey.generate_key(2048, {"kid": "key-a", "alg": "RS256"})
KEY_B = RSAKey.generate_key(2048, {"kid": "key-b", "alg": "RS256"})


def _token(key: RSAKey, kid: str | None = None) -> str:
    header = {"alg": "RS256", "kid": kid if kid is not None else key.kid}
    return jose_jwt.encode(header, {"sub": "user-1"}, key)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Endpoint:
    """A scripted JWKS endpoint that counts its fetches."""

    def __init__(self, *keys: RSAKey) -> None:
        self.keys = list(keys)
        self.calls = 0
        self.fail = False
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> dict:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise OSError("endpoint down")
        return KeySet(self.keys).as_dict(private=False)


def _cache(endpoint: _Endpoint, clock: _Clock, ttl: float = GATEWAY_JWKS_TTL_SECONDS) -> JwksCache:
    return JwksCache(endpoint, cache="gateway", ttl_seconds=ttl, clock=clock)


def _kids(keys: KeySet) -> set[str]:
    return {key.kid for key in keys.keys}


def test_the_ttl_differs_per_mode() -> None:
    assert OIDC_JWKS_TTL_SECONDS == 6 * 3600.0
    assert GATEWAY_JWKS_TTL_SECONDS == 3600.0


def test_known_keys_come_from_the_cache_within_the_ttl() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        clock.now += GATEWAY_JWKS_TTL_SECONDS - 1
        await cache.keys_for(_token(KEY_A))

    asyncio.run(run())
    assert endpoint.calls == 1


def test_an_expired_ttl_fetches_again() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        clock.now += GATEWAY_JWKS_TTL_SECONDS + 1
        await cache.keys_for(_token(KEY_A))

    asyncio.run(run())
    assert endpoint.calls == 2


def test_an_unknown_kid_refreshes_once_per_cooldown() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> KeySet:
        await cache.keys_for(_token(KEY_A))
        endpoint.keys = [KEY_A, KEY_B]
        keys = await cache.keys_for(_token(KEY_B))
        await cache.keys_for(_token(KEY_B, kid="still-unknown"))
        return keys

    keys = asyncio.run(run())
    assert endpoint.calls == 2
    assert _kids(keys) == {"key-a", "key-b"}


def test_after_the_cooldown_an_unknown_kid_refreshes_again() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        await cache.keys_for(_token(KEY_A, kid="unknown-1"))
        clock.now += JWKS_REFRESH_COOLDOWN_SECONDS + 1
        await cache.keys_for(_token(KEY_A, kid="unknown-2"))

    asyncio.run(run())
    assert endpoint.calls == 3


def test_concurrent_requests_with_a_new_kid_share_one_fetch() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        endpoint.keys = [KEY_A, KEY_B]
        endpoint.gate = asyncio.Event()
        pending = [asyncio.create_task(cache.keys_for(_token(KEY_B))) for _ in range(50)]
        await asyncio.sleep(0)
        endpoint.gate.set()
        results = await asyncio.gather(*pending)
        assert all("key-b" in _kids(keys) for keys in results)

    asyncio.run(run())
    assert endpoint.calls == 2


def test_random_kids_cause_one_refresh_for_the_whole_cache() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        for index in range(50):
            await cache.keys_for(_token(KEY_A, kid=f"random-{index}"))

    asyncio.run(run())
    assert endpoint.calls == 2


def test_a_failed_refresh_keeps_known_keys_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> KeySet:
        await cache.keys_for(_token(KEY_A))
        clock.now += GATEWAY_JWKS_TTL_SECONDS + 1
        endpoint.fail = True
        return await cache.keys_for(_token(KEY_A))

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.jwks"):
        keys = asyncio.run(run())

    assert _kids(keys) == {"key-a"}
    assert any("JWKS refresh failed" in record.getMessage() for record in caplog.records)


def test_a_failed_refresh_leaves_an_unknown_kid_without_keys() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        endpoint.fail = True
        await cache.keys_for(_token(KEY_B))

    with pytest.raises(KeysUnavailable):
        asyncio.run(run())


@pytest.fixture()
def registry():
    from prometheus_client import CollectorRegistry

    from inqtrix.observability.metrics_defs import (
        active_metrics,
        build_call_metrics,
        set_active_metrics,
    )

    registry = CollectorRegistry()
    previous = active_metrics()
    set_active_metrics(build_call_metrics(registry))
    try:
        yield registry
    finally:
        set_active_metrics(previous)


def test_a_failed_refresh_is_counted_for_its_cache(registry) -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = JwksCache(endpoint, cache="oidc", ttl_seconds=GATEWAY_JWKS_TTL_SECONDS, clock=clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        endpoint.fail = True
        clock.now += GATEWAY_JWKS_TTL_SECONDS + 1
        await cache.keys_for(_token(KEY_A))

    asyncio.run(run())

    assert registry.get_sample_value("inqtrix_jwks_refresh_failures_total", {"cache": "oidc"}) == 1


def test_closing_the_cache_closes_its_endpoint() -> None:
    class _ClosingEndpoint(_Endpoint):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    endpoint = _ClosingEndpoint(KEY_A)

    asyncio.run(_cache(endpoint, _Clock()).aclose())

    assert endpoint.closed is True


@pytest.mark.parametrize("with_kid", [True, False], ids=["kid", "no-kid"])
def test_without_any_loadable_key_set_the_keys_are_unavailable(with_kid: bool) -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    endpoint.fail = True
    cache = _cache(endpoint, clock)
    token = (
        _token(KEY_A)
        if with_kid
        else jose_jwt.encode({"alg": "RS256"}, {"sub": "user-1"}, KEY_A)
    )

    with pytest.raises(KeysUnavailable):
        asyncio.run(cache.keys_for(token))


def test_a_failure_is_remembered_instead_of_retried_on_every_request() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        clock.now += GATEWAY_JWKS_TTL_SECONDS + 1
        endpoint.fail = True
        for _ in range(20):
            await cache.keys_for(_token(KEY_A))

    asyncio.run(run())
    assert endpoint.calls == 2


def test_keys_older_than_the_maximum_age_are_unavailable() -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        endpoint.fail = True
        clock.now += JWKS_MAX_STALE_SECONDS + 1
        await cache.keys_for(_token(KEY_A))

    with pytest.raises(KeysUnavailable):
        asyncio.run(run())


def test_the_kid_in_the_log_is_cut(caplog: pytest.LogCaptureFixture) -> None:
    endpoint, clock = _Endpoint(KEY_A), _Clock()
    cache = _cache(endpoint, clock)
    long_kid = "k" * 300

    async def run() -> None:
        await cache.keys_for(_token(KEY_A))
        await cache.keys_for(_token(KEY_A, kid=long_kid))

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.jwks"):
        asyncio.run(run())

    text = caplog.text
    assert repr("k" * KID_LOG_LIMIT) in text
    assert "k" * (KID_LOG_LIMIT + 1) not in text


def test_static_keys_never_refresh() -> None:
    keys = StaticKeys(KeySet([KEY_A]))

    async def run() -> tuple[KeySet, KeySet]:
        return await keys.keys_for(_token(KEY_A)), await keys.keys_for(_token(KEY_B))

    first, second = asyncio.run(run())
    assert _kids(first) == _kids(second) == {"key-a"}


def test_a_key_file_with_two_keys_is_read(tmp_path: Path) -> None:
    path = tmp_path / "gateway.jwks"
    path.write_text(json.dumps(KeySet([KEY_A, KEY_B]).as_dict(private=False)), encoding="utf-8")

    assert _kids(load_key_file(path)) == {"key-a", "key-b"}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"kty": "RSA"}),
        json.dumps({"keys": []}),
        json.dumps({"keys": [{key: value for key, value in KEY_A.as_dict(private=False).items() if key != "kid"}]}),
    ],
    ids=["not-json", "no-keys-member", "empty", "key-without-kid"],
)
def test_an_unusable_key_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "gateway.jwks"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_key_file(path)


def test_a_key_file_never_carries_private_material(tmp_path: Path) -> None:
    path = tmp_path / "gateway.jwks"
    path.write_text(json.dumps(KeySet([KEY_A]).as_dict(private=True)), encoding="utf-8")

    with pytest.raises(ValueError):
        load_key_file(path)


def test_oidc_uses_the_shared_cache_with_its_ttl() -> None:
    from inqtrix.auth.oidc import OidcClient

    client = OidcClient(
        issuer="https://idp.example",
        client_id="inqtrix",
        client_secret="secret",
        redirect_url="https://desk.example/api/auth/callback",
    )

    assert isinstance(client.jwks, JwksCache)
    assert client.jwks.ttl_seconds == OIDC_JWKS_TTL_SECONDS
