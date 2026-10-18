"""Postgres integration tests for the PAT store (gated suite).

Same gating and conventions as the sibling suites. Pins the behaviors
token security depends on across replicas: the guarded revoke (live
row of the right owner flips exactly once), the single-statement
last-used throttle, and the disable-cascade helper.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

import inqtrix.storage.pat_postgres as pat_postgres_module
from inqtrix.auth.pat import (
    PatService,
    PatVerifier,
    PersonalAccessToken,
)
from inqtrix.auth.permissions import AuditEntry
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import audit_log
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.pat_postgres import PostgresPatStore
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_1_ID = canonical_user_id("pat-owner-1")
OWNER_2_ID = canonical_user_id("pat-owner-2")


class ActiveUserLookup:
    async def find_by_user_id(self, *, tenant_id, user_id):
        if tenant_id == "default" and user_id == OWNER_1_ID:
            return SimpleNamespace(disabled_at=None)
        return None


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@pytest.fixture(scope="session", autouse=True)
def pat_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def engine():
    engine = build_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def store(engine):
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user for cross-tenant cleanup."
                )
            await session.execute(
                text("DELETE FROM personal_access_tokens")
            )
            await session.execute(
                audit_log.delete().where(audit_log.c.resource_type == "pat")
            )
            await ensure_canonical_users(
                session,
                (OWNER_1_ID, OWNER_2_ID),
            )
    return PostgresPatStore(session_factory=factory, app_role=APP_ROLE)


def make_token(
    token_id: str = "tok1",
    *,
    owner_user_id: uuid.UUID = OWNER_1_ID,
    expires_at: float | None = None,
) -> PersonalAccessToken:
    return PersonalAccessToken(
        token_id=token_id,
        tenant_id="default",
        owner_user_id=owner_user_id,
        name="ci",
        secret_hmac="ab" * 32,
        created_at=time.time(),
        expires_at=expires_at,
        last_used_at=None,
        revoked_at=None,
        scopes=("runs:read",),
    )


@pytest.mark.asyncio
async def test_roundtrip_and_owner_listing(store):
    await store.create(make_token("tok1"))
    await store.create(make_token("tok2", owner_user_id=OWNER_2_ID))

    loaded = await store.get("tok1")
    assert loaded is not None
    assert loaded.owner_user_id == OWNER_1_ID
    assert loaded.scopes == ("runs:read",)
    assert loaded.expires_at is None

    listed = await store.list_for_owner(
        tenant_id="default",
        owner_user_id=OWNER_1_ID,
    )
    assert [token.token_id for token in listed] == ["tok1"]


@pytest.mark.asyncio
async def test_revoke_guards_owner_and_liveness(store):
    await store.create(make_token("tok1"))
    now = time.time()
    # Wrong owner never flips the row.
    assert not await store.revoke(
        tenant_id="default",
        token_id="tok1",
        owner_user_id=OWNER_2_ID,
        now=now,
    )
    assert await store.revoke(
        tenant_id="default",
        token_id="tok1",
        owner_user_id=OWNER_1_ID,
        now=now,
    )
    # Idempotent: a second revoke is a no-op.
    assert not await store.revoke(
        tenant_id="default",
        token_id="tok1",
        owner_user_id=OWNER_1_ID,
        now=now,
    )
    assert (await store.get("tok1")).revoked_at is not None


@pytest.mark.asyncio
async def test_concurrent_double_revoke_flips_once(store):
    await store.create(make_token("tok1"))
    now = time.time()
    results = await asyncio.gather(
        *(
            store.revoke(
                tenant_id="default",
                token_id="tok1",
                owner_user_id=OWNER_1_ID,
                now=now,
            )
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_last_used_throttle_is_one_guarded_statement(store):
    await store.create(make_token("tok1"))
    await store.touch_last_used("tok1", now=1_000.0, min_interval=300.0)
    assert (await store.get("tok1")).last_used_at == 1_000.0
    # Inside the interval: no write.
    await store.touch_last_used("tok1", now=1_100.0, min_interval=300.0)
    assert (await store.get("tok1")).last_used_at == 1_000.0
    # Past the interval: writes again.
    await store.touch_last_used("tok1", now=1_400.0, min_interval=300.0)
    assert (await store.get("tok1")).last_used_at == 1_400.0


@pytest.mark.asyncio
async def test_token_lifecycle_commits_the_sampled_audit_contract(
    store, engine
):
    token_id = "pat-audit-lifecycle"
    await store.create(make_token(token_id))
    assert await store.touch_last_used(
        token_id,
        now=1_000.0,
        min_interval=300.0,
    )
    assert not await store.touch_last_used(
        token_id,
        now=1_100.0,
        min_interval=300.0,
    )
    assert await store.revoke(
        tenant_id="default",
        token_id=token_id,
        owner_user_id=OWNER_1_ID,
        now=1_200.0,
    )

    factory = build_session_factory(engine)
    async with factory() as session:
        actions = (
            await session.execute(
                select(audit_log.c.action)
                .where(
                    audit_log.c.resource_type == "pat",
                    audit_log.c.resource_id == token_id,
                )
                .order_by(audit_log.c.id)
            )
        ).scalars().all()

    assert actions == ["pat.created", "pat.used", "pat.revoked"]


@pytest.mark.asyncio
async def test_bound_sink_does_not_duplicate_atomic_store_audit(
    store, engine
):
    audit = RecordingAudit()
    service = PatService(
        store=store,
        pepper="integration-pat-pepper",
        audit=audit,
    )
    verifier = PatVerifier(
        store=store,
        pepper="integration-pat-pepper",
        user_lookup=ActiveUserLookup(),
        audit=audit,
    )
    minted = await service.create_token(
        tenant_id="default",
        owner_user_id=OWNER_1_ID,
        name="atomic-audit",
    )
    await verifier.verify(minted.plaintext)
    await verifier.verify(minted.plaintext)
    assert await service.revoke_token(
        tenant_id="default",
        token_id=minted.record.token_id,
        owner_user_id=OWNER_1_ID,
    )

    factory = build_session_factory(engine)
    async with factory() as session:
        actions = (
            await session.execute(
                select(audit_log.c.action)
                .where(
                    audit_log.c.resource_type == "pat",
                    audit_log.c.resource_id == minted.record.token_id,
                )
                .order_by(audit_log.c.id)
            )
        ).scalars().all()

    assert actions == ["pat.created", "pat.used", "pat.revoked"]
    assert audit.entries == []


@pytest.mark.parametrize("operation", ["create", "use", "revoke"])
@pytest.mark.asyncio
async def test_audit_failure_rolls_back_the_token_mutation(
    store, monkeypatch, operation
):
    token_id = f"pat-audit-rollback-{operation}"
    if operation != "create":
        await store.create(make_token(token_id))

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(
        pat_postgres_module,
        "append_audit_row",
        fail_audit,
    )

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        if operation == "create":
            await store.create(make_token(token_id))
        elif operation == "use":
            await store.touch_last_used(
                token_id,
                now=1_000.0,
                min_interval=300.0,
            )
        else:
            await store.revoke(
                tenant_id="default",
                token_id=token_id,
                owner_user_id=OWNER_1_ID,
                now=1_000.0,
            )

    stored = await store.get(token_id)
    if operation == "create":
        assert stored is None
    elif operation == "use":
        assert stored is not None
        assert stored.last_used_at is None
    else:
        assert stored is not None
        assert stored.revoked_at is None


@pytest.mark.asyncio
async def test_disable_cascade_revokes_only_that_owner(store):
    await store.create(make_token("tok1"))
    await store.create(make_token("tok2"))
    await store.create(make_token("tok3", owner_user_id=OWNER_2_ID))
    revoked = await store.revoke_all_for_owner(
        tenant_id="default",
        owner_user_id=OWNER_1_ID,
        now=time.time(),
    )
    assert revoked == 2
    assert (await store.get("tok3")).revoked_at is None
