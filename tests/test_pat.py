"""Personal-access-token domain tests: mint, parse, verify, manage.

Pins the security contracts: the full verify matrix returns the
UNIFORM 401 (no stage leaks through the response), the throttled
last-used bookkeeping, the per-owner revoke guard, the sprawl cap,
and the disable-cascade helper.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from inqtrix.auth.pat import (
    MemoryPatStore,
    PatLimitExceeded,
    PatService,
    PatVerifier,
    format_pat,
    hash_pat_secret,
    mint_pat_credentials,
    parse_pat,
)
from inqtrix.auth.permissions import AuditEntry

PEPPER = "test-pepper"
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


class ActiveUserLookup:
    async def find_by_user_id(self, *, tenant_id, user_id):
        if tenant_id == "default" and user_id == USER_ID:
            return SimpleNamespace(disabled_at=None)
        return None


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def make_verifier(
    store: MemoryPatStore,
    *,
    pepper: str = PEPPER,
    audit: RecordingAudit | None = None,
) -> PatVerifier:
    return PatVerifier(
        store=store,
        pepper=pepper,
        user_lookup=ActiveUserLookup(),
        audit=audit,
    )


def make_service(store: MemoryPatStore | None = None, **kwargs) -> PatService:
    return PatService(store=store or MemoryPatStore(), pepper=PEPPER, **kwargs)


async def mint(
    service: PatService, *, name: str = "ci", expires_in_days: int | None = None
):
    return await service.create_token(
        tenant_id="default",
        owner_user_id=USER_ID,
        name=name,
        expires_in_days=expires_in_days,
    )


class TestMintAndParse:
    def test_roundtrip(self):
        token_id, secret = mint_pat_credentials()
        assert parse_pat(format_pat(token_id, secret)) == (token_id, secret)

    def test_secret_with_underscores_survives(self):
        """token_urlsafe may emit underscores — the single left split
        must keep them inside the secret."""
        assert parse_pat("ipat_abc123_se_cr_et") == ("abc123", "se_cr_et")

    @pytest.mark.parametrize(
        "value",
        ["", "ipat_", "ipat_onlyid", "ipat__secret", "pat_abc_def", "Bearer x"],
    )
    def test_malformed_shapes_parse_to_none(self, value):
        assert parse_pat(value) is None


class TestVerifyMatrix:
    @pytest.fixture()
    def setup(self):
        store = MemoryPatStore()
        service = make_service(store)
        verifier = make_verifier(store)
        return store, service, verifier

    async def assert_uniform_401(self, verifier, value):
        with pytest.raises(HTTPException) as excinfo:
            await verifier.verify(value)
        assert excinfo.value.status_code == 401
        assert (
            excinfo.value.detail["error"]["message"]
            == "Invalid access token"
        )
        assert excinfo.value.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.asyncio
    async def test_valid_token_resolves_pat_principal(self, setup):
        _store, service, verifier = setup
        minted = await mint(service)
        principal = await verifier.verify(minted.plaintext)
        assert principal.kind == "pat"
        assert principal.user_id == USER_ID
        assert principal.pat_id == minted.record.token_id
        assert principal.tenant_id == "default"

    @pytest.mark.asyncio
    async def test_wrong_secret_is_uniform_401(self, setup):
        _store, service, verifier = setup
        minted = await mint(service)
        await self.assert_uniform_401(
            verifier, format_pat(minted.record.token_id, "wrong-secret")
        )

    @pytest.mark.asyncio
    async def test_unknown_token_id_is_uniform_401(self, setup):
        _store, _service, verifier = setup
        await self.assert_uniform_401(verifier, "ipat_deadbeef_whatever")

    @pytest.mark.asyncio
    async def test_malformed_value_is_uniform_401(self, setup):
        _store, _service, verifier = setup
        await self.assert_uniform_401(verifier, "kein-token")

    @pytest.mark.asyncio
    async def test_revoked_token_is_uniform_401(self, setup):
        _store, service, verifier = setup
        minted = await mint(service)
        assert await service.revoke_token(
            tenant_id="default",
            token_id=minted.record.token_id,
            owner_user_id=USER_ID,
        )
        await self.assert_uniform_401(verifier, minted.plaintext)

    @pytest.mark.asyncio
    async def test_expired_token_is_uniform_401(self, setup, monkeypatch):
        store, service, verifier = setup
        minted = await mint(service, expires_in_days=1)
        monkeypatch.setattr(
            time, "time", lambda: minted.record.created_at + 2 * 86_400
        )
        await self.assert_uniform_401(verifier, minted.plaintext)

    @pytest.mark.asyncio
    async def test_pepper_mismatch_is_uniform_401(self, setup):
        store, service, _verifier = setup
        minted = await mint(service)
        other = make_verifier(store, pepper="other-pepper")
        await self.assert_uniform_401(other, minted.plaintext)

    def test_empty_pepper_is_a_wiring_error(self):
        with pytest.raises(ValueError, match="non-empty pepper"):
            PatVerifier(
                store=MemoryPatStore(),
                pepper="  ",
                user_lookup=ActiveUserLookup(),
            )


class TestLastUsedThrottle:
    @pytest.mark.asyncio
    async def test_two_verifies_inside_the_interval_write_once(
        self, monkeypatch
    ):
        store = MemoryPatStore()
        service = make_service(store)
        verifier = make_verifier(store)
        minted = await mint(service)
        base = minted.record.created_at
        monkeypatch.setattr(time, "time", lambda: base + 10)
        await verifier.verify(minted.plaintext)
        first = (await store.get(minted.record.token_id)).last_used_at
        assert first == base + 10
        monkeypatch.setattr(time, "time", lambda: base + 20)
        await verifier.verify(minted.plaintext)
        assert (await store.get(minted.record.token_id)).last_used_at == first
        monkeypatch.setattr(time, "time", lambda: base + 400)
        await verifier.verify(minted.plaintext)
        assert (
            await store.get(minted.record.token_id)
        ).last_used_at == base + 400


class TestAuditLifecycle:
    @pytest.mark.asyncio
    async def test_memory_store_records_one_sampled_lifecycle(
        self, monkeypatch
    ):
        store = MemoryPatStore()
        audit = RecordingAudit()
        service = make_service(store, audit=audit)
        verifier = make_verifier(store, audit=audit)

        minted = await mint(service)
        base = minted.record.created_at
        monkeypatch.setattr(time, "time", lambda: base + 10)
        await verifier.verify(minted.plaintext)
        await verifier.verify(minted.plaintext)
        assert await service.revoke_token(
            tenant_id="default",
            token_id=minted.record.token_id,
            owner_user_id=USER_ID,
        )

        assert [entry.action for entry in audit.entries] == [
            "pat.created",
            "pat.used",
            "pat.revoked",
        ]
        assert {
            entry.resource_id for entry in audit.entries
        } == {minted.record.token_id}
        assert audit.entries[1].origin["auth_method"] == "pat"

    def test_canonical_sink_binding_is_idempotent_but_not_replaceable(self):
        store = MemoryPatStore()
        first = RecordingAudit()
        second = RecordingAudit()
        service = make_service(store)
        verifier = make_verifier(store)

        service.bind_audit(first)
        service.bind_audit(first)
        verifier.bind_audit(first)
        verifier.bind_audit(first)

        with pytest.raises(RuntimeError, match="cannot be rebound"):
            service.bind_audit(second)
        with pytest.raises(RuntimeError, match="cannot be rebound"):
            verifier.bind_audit(second)


class TestManagement:
    @pytest.mark.asyncio
    async def test_cap_enforced_on_active_tokens(self):
        service = make_service(max_per_user=2)
        await mint(service, name="a")
        await mint(service, name="b")
        with pytest.raises(PatLimitExceeded):
            await mint(service, name="c")

    @pytest.mark.asyncio
    async def test_revoked_tokens_free_their_cap_slot(self):
        store = MemoryPatStore()
        service = make_service(store, max_per_user=1)
        minted = await mint(service)
        await service.revoke_token(
            tenant_id="default",
            token_id=minted.record.token_id,
            owner_user_id=USER_ID,
        )
        await mint(service, name="replacement")

    @pytest.mark.asyncio
    async def test_revoke_guards_on_the_owner(self):
        store = MemoryPatStore()
        service = make_service(store)
        minted = await mint(service)
        assert not await store.revoke(
            tenant_id="default",
            token_id=minted.record.token_id,
            owner_user_id=OTHER_USER_ID,
            now=time.time(),
        )
        assert not await store.revoke(
            tenant_id="other",
            token_id=minted.record.token_id,
            owner_user_id=USER_ID,
            now=time.time(),
        )

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent(self):
        store = MemoryPatStore()
        service = make_service(store)
        minted = await mint(service)
        kwargs = dict(
            tenant_id="default",
            token_id=minted.record.token_id,
            owner_user_id=USER_ID,
        )
        assert await service.revoke_token(**kwargs) is True
        assert await service.revoke_token(**kwargs) is False

    @pytest.mark.asyncio
    async def test_default_ttl_applies_only_without_explicit_expiry(self):
        service = make_service(default_ttl_days=7)
        defaulted = await mint(service, name="defaulted")
        assert defaulted.record.expires_at == pytest.approx(
            defaulted.record.created_at + 7 * 86_400, abs=5
        )
        explicit = await mint(service, name="explicit", expires_in_days=30)
        assert explicit.record.expires_at == pytest.approx(
            explicit.record.created_at + 30 * 86_400, abs=5
        )

    @pytest.mark.asyncio
    async def test_zero_default_ttl_means_non_expiring(self):
        service = make_service(default_ttl_days=0)
        minted = await mint(service)
        assert minted.record.expires_at is None

    @pytest.mark.asyncio
    async def test_disable_cascade_revokes_every_owner_token(self):
        store = MemoryPatStore()
        service = make_service(store, max_per_user=5)
        await mint(service, name="a")
        await mint(service, name="b")
        revoked = await store.revoke_all_for_owner(
            tenant_id="default",
            owner_user_id=USER_ID,
            now=time.time(),
        )
        assert revoked == 2
        assert await service.list_tokens(
            tenant_id="default",
            owner_user_id=USER_ID,
        ) == ()

    @pytest.mark.asyncio
    async def test_listing_never_exposes_secret_material(self):
        store = MemoryPatStore()
        service = make_service(store)
        minted = await mint(service)
        listed = await service.list_tokens(
            tenant_id="default",
            owner_user_id=USER_ID,
        )
        assert minted.plaintext not in repr(listed)
        # The stored hash never equals the secret half of the token.
        assert listed[0].secret_hmac != minted.plaintext.split("_", 2)[2]

    def test_hash_is_deterministic_and_pepper_bound(self):
        assert hash_pat_secret("p", "s") == hash_pat_secret("p", "s")
        assert hash_pat_secret("p", "s") != hash_pat_secret("q", "s")


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["unknown", "wrong_secret", "revoked"])
async def test_rejection_logs_use_resource_references(
    rejection, monkeypatch, caplog
):
    from inqtrix.auth import pat as pat_module

    store = MemoryPatStore()
    service = make_service(store)
    minted = await mint(service)
    token_id = minted.record.token_id
    bearer = minted.plaintext
    if rejection == "unknown":
        token_id = "deadbeef"
        bearer = format_pat(token_id, "synthetic-secret")
    elif rejection == "wrong_secret":
        bearer = format_pat(token_id, "synthetic-wrong-secret")
    else:
        await service.revoke_token(
            tenant_id="default", token_id=token_id, owner_user_id=USER_ID
        )

    references = []

    def reference(namespace, identifier):
        references.append((namespace, identifier))
        return "res_0123456789abcdef"

    monkeypatch.setattr(pat_module, "pseudonymous_log_reference", reference)
    caplog.set_level("WARNING", logger="inqtrix")
    caplog.clear()
    with pytest.raises(HTTPException) as rejected:
        await make_verifier(store).verify(bearer)

    assert rejected.value.status_code == 401
    assert rejected.value.detail == {
        "error": {"message": "Invalid access token", "type": "unauthorized"}
    }
    assert references == [("res", token_id)]
    expected = {
        "unknown": "PAT rejected: unknown token reference res_0123456789abcdef.",
        "wrong_secret": "PAT rejected: incorrect secret for token reference res_0123456789abcdef.",
        "revoked": "PAT rejected: token reference res_0123456789abcdef is revoked.",
    }
    messages = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("PAT rejected:")
    ]
    assert messages == [expected[rejection]]
    assert token_id not in messages[0]
    assert bearer not in messages[0]
