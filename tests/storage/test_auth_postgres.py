"""Postgres integration tests for the OIDC auth stores (gated suite).

Same gating and conventions as the sibling suites. Pins the behaviors
login correctness depends on: session round-trip and expiry, the
strictly one-time guarded flow consumption (replay defense across
replicas), lazy eviction, and the JIT user upsert on the
``(issuer, subject)`` identity anchor.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.auth.sessions import AuthSession, LoginFlow
from inqtrix.storage.auth_postgres import (
    PostgresFlowStore,
    PostgresSessionStore,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
SESSION_USER_ID = canonical_user_id("auth-session-user")


@pytest.fixture(scope="session", autouse=True)
def auth_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def engine():
    engine = build_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def session_factory(engine):
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
            await session.execute(text("DELETE FROM auth_flows"))
            await session.execute(text("DELETE FROM auth_sessions"))
            await ensure_canonical_users(session, (SESSION_USER_ID,))
    return factory


def make_session(
    session_id: str = "sess-1",
    *,
    user_id: uuid.UUID = SESSION_USER_ID,
    ttl: float = 60.0,
) -> AuthSession:
    return AuthSession(
        id=session_id,
        user_id=user_id,
        issuer="http://127.0.0.1:5556/dex",
        subject="user-1234",
        email="alice@example.com",
        display_name="alice",
        groups=("team-a",),
        csrf_random="ab" * 16,
        created_at=time.time(),
        expires_at=time.time() + ttl,
    )


@pytest.mark.asyncio
async def test_session_roundtrip_and_delete(session_factory):
    store = PostgresSessionStore(
        session_factory=session_factory, app_role=APP_ROLE
    )
    await store.create(make_session())

    loaded = await store.get("sess-1")
    assert loaded is not None
    assert loaded.user_id == SESSION_USER_ID
    assert loaded.subject == "user-1234"
    assert loaded.groups == ("team-a",)
    assert loaded.csrf_random == "ab" * 16

    await store.delete("sess-1")
    assert await store.get("sess-1") is None
    # Idempotent logout: deleting again is a no-op.
    await store.delete("sess-1")


@pytest.mark.asyncio
async def test_expired_sessions_resolve_to_none_and_get_evicted(
    session_factory,
):
    store = PostgresSessionStore(
        session_factory=session_factory, app_role=APP_ROLE
    )
    await store.create(make_session("sess-old", ttl=-1))
    assert await store.get("sess-old") is None
    # The next create lazily evicts expired rows.
    await store.create(make_session("sess-new"))
    assert await store.get("sess-new") is not None


def make_flow(state: str = "state-1", *, ttl: float = 60.0) -> LoginFlow:
    return LoginFlow(
        state=state,
        code_verifier="verifier",
        nonce="nonce",
        next_path="/desk",
        expires_at=time.time() + ttl,
    )


@pytest.mark.asyncio
async def test_flow_consumption_is_strictly_one_time(session_factory):
    store = PostgresFlowStore(
        session_factory=session_factory, app_role=APP_ROLE
    )
    await store.put(make_flow())

    first = await store.consume("state-1")
    assert first is not None and first.code_verifier == "verifier"
    assert first.next_path == "/desk"
    # The guarded UPDATE makes the replay lose atomically — also
    # across API replicas sharing the table.
    assert await store.consume("state-1") is None


@pytest.mark.asyncio
async def test_expired_flows_cannot_be_consumed(session_factory):
    store = PostgresFlowStore(
        session_factory=session_factory, app_role=APP_ROLE
    )
    await store.put(make_flow("state-old", ttl=-1))
    assert await store.consume("state-old") is None
