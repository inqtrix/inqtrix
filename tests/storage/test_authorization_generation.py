"""Commit-ordered authorization generation: bumps, ordering, replicas.

Gated via the shared ``postgres`` marker. Covers the mutation table of
the stream-authorization design: every permission-relevant mutation must
advance the target user's generation INSIDE its own transaction, the
row lock must serialize concurrent bumps into commit order, and a second
replica must observe a bump through the database alone.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from inqtrix.storage.authorization_generation import (
    bump_authorization_generation,
    read_authorization_generation,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_events_postgres import append_user_invalidation
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
TENANT = "default"
USER = canonical_user_id("authgen-user")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


def _run(coro):
    """Fresh loop + engine per step (loop-affinity, as the sibling suites)."""
    return asyncio.run(coro)


async def _with_engine(fn):
    engine = build_engine(TEST_DATABASE_URL)
    try:
        return await fn(engine)
    finally:
        await engine.dispose()


async def _seed_user() -> None:
    async def op(engine):
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                await ensure_canonical_users(session, (USER,))

    await _with_engine(op)


async def _generation() -> int | None:
    async def op(engine):
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                return await read_authorization_generation(
                    session, tenant_id=TENANT, user_id=USER
                )

    return await _with_engine(op)


def test_the_invalidation_chokepoint_bumps_in_the_same_transaction() -> None:
    """Every existing writer (shares, workspace membership, lifecycle,
    admin invalidations) funnels through append_user_invalidation: one
    bump point covers the whole mutation table's existing rows."""

    async def scenario() -> None:
        await _seed_user()
        before = await _generation()

        async def op(engine):
            factory = build_session_factory(engine)
            async with factory() as session:
                async with session.begin():
                    await append_user_invalidation(
                        session,
                        tenant_id=TENANT,
                        target_user_id=USER,
                        scope="test",
                    )

        await _with_engine(op)
        assert await _generation() == (before or 0) + 1

        # Same-transaction proof THROUGH the chokepoint: a mutation that
        # fails after the append must take event AND bump back together
        # (an outbox-style separate connection would leak the bump).
        async def rollback_op(engine):
            factory = build_session_factory(engine)
            try:
                async with factory() as session:
                    async with session.begin():
                        await append_user_invalidation(
                            session,
                            tenant_id=TENANT,
                            target_user_id=USER,
                            scope="test",
                        )
                        raise RuntimeError("mutation failed after append")
            except RuntimeError:
                pass

        await _with_engine(rollback_op)
        assert await _generation() == (before or 0) + 1

    _run(scenario())


def test_a_rolled_back_mutation_bumps_nothing() -> None:
    """The bump rides the mutation's transaction: no commit, no bump —
    there is no outbox race and no second authority."""

    async def scenario() -> None:
        await _seed_user()
        before = await _generation()

        async def op(engine):
            factory = build_session_factory(engine)
            try:
                async with factory() as session:
                    async with session.begin():
                        await bump_authorization_generation(
                            session,
                            tenant_id=TENANT,
                            target_user_ids=(USER,),
                        )
                        raise RuntimeError("mutation failed after the bump")
            except RuntimeError:
                pass

        await _with_engine(op)
        assert await _generation() == before

    _run(scenario())


def test_concurrent_bumps_serialize_on_the_row_lock() -> None:
    """The commit-order property a sequence cannot give.

    Transaction A bumps and HOLDS its lock; transaction B's bump must
    block until A commits (never overtake), and the final value reflects
    both — strictly ordered per user.
    """

    async def scenario() -> None:
        await _seed_user()
        before = await _generation() or 0

        async def op(engine):
            factory = build_session_factory(engine)
            a_holding = asyncio.Event()
            b_at_bump = asyncio.Event()
            release_a = asyncio.Event()
            b_committed_at: list[float] = []
            a_committed_at: list[float] = []

            async def txn_a() -> None:
                async with factory() as session:
                    async with session.begin():
                        await bump_authorization_generation(
                            session, tenant_id=TENANT, target_user_ids=(USER,)
                        )
                        a_holding.set()
                        await release_a.wait()
                a_committed_at.append(time.monotonic())

            async def txn_b() -> None:
                await a_holding.wait()
                async with factory() as session:
                    async with session.begin():
                        # Blocks on A's row lock until A commits. The
                        # signal fires BEFORE the statement so the parent
                        # can distinguish "blocked" from "not there yet".
                        b_at_bump.set()
                        await bump_authorization_generation(
                            session, tenant_id=TENANT, target_user_ids=(USER,)
                        )
                b_committed_at.append(time.monotonic())

            task_a = asyncio.create_task(txn_a())
            task_b = asyncio.create_task(txn_b())
            await asyncio.wait_for(a_holding.wait(), timeout=10)
            await asyncio.wait_for(b_at_bump.wait(), timeout=10)
            # B has ISSUED its bump: give it real time to overtake if the
            # row lock failed to block it (a plain sleep before B even
            # reached the UPDATE would pass vacuously).
            await asyncio.sleep(0.3)
            assert not task_b.done(), (
                "B committed while A held the row lock — the generation "
                "would not be commit-ordered"
            )
            release_a.set()
            # Bounded: a regressed lock that never releases must FAIL the
            # test, not hang the suite (no timeout plugin runs here).
            await asyncio.wait_for(
                asyncio.gather(task_a, task_b), timeout=10
            )
            assert a_committed_at[0] <= b_committed_at[0]

        await _with_engine(op)
        assert await _generation() == before + 2

    _run(scenario())


def test_a_second_replica_observes_the_bump_through_the_database() -> None:
    """No in-process state: replica B reads what replica A committed."""

    async def scenario() -> None:
        await _seed_user()
        engine_a = build_engine(TEST_DATABASE_URL)
        engine_b = build_engine(TEST_DATABASE_URL)
        try:
            factory_a = build_session_factory(engine_a)
            factory_b = build_session_factory(engine_b)
            async with factory_b() as session:
                async with session.begin():
                    before = await read_authorization_generation(
                        session, tenant_id=TENANT, user_id=USER
                    )
            async with factory_a() as session:
                async with session.begin():
                    await bump_authorization_generation(
                        session, tenant_id=TENANT, target_user_ids=(USER,)
                    )
            async with factory_b() as session:
                async with session.begin():
                    after = await read_authorization_generation(
                        session, tenant_id=TENANT, user_id=USER
                    )
        finally:
            await engine_a.dispose()
            await engine_b.dispose()
        assert after == (before or 0) + 1

    _run(scenario())


def test_an_unknown_user_reads_none_and_bumps_nothing() -> None:
    """Api-key principals have no users row: None routes the frame gate
    to the full chain, and a bump for them is a harmless no-op."""

    async def scenario() -> None:
        ghost = uuid.uuid4()

        async def op(engine):
            factory = build_session_factory(engine)
            async with factory() as session:
                async with session.begin():
                    await bump_authorization_generation(
                        session, tenant_id=TENANT, target_user_ids=(ghost,)
                    )
                    assert (
                        await read_authorization_generation(
                            session, tenant_id=TENANT, user_id=ghost
                        )
                        is None
                    )

        await _with_engine(op)

    _run(scenario())


def test_session_deletion_bumps_the_generation() -> None:
    """Mutation-table rows "Logout / einzelne Sitzung geloescht": a
    logged-out session's live streams must drop within a frame, not only
    at the gate's time ceiling."""
    from inqtrix.auth.sessions import AuthSession
    from inqtrix.storage.auth_postgres import PostgresSessionStore

    async def scenario() -> None:
        await _seed_user()

        async def op(engine):
            store = PostgresSessionStore(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
            )
            record = AuthSession(
                id=f"authgen-sess-{uuid.uuid4().hex[:8]}",
                user_id=USER,
                issuer="https://storage-tests.example",
                subject="authgen",
                email=None,
                display_name=None,
                groups=(),
                csrf_random="x" * 32,
                created_at=time.time(),
                expires_at=time.time() + 3600,
            )
            before = await _generation() or 0
            await store.create(record)
            await store.delete(record.id)
            assert await _generation() == before + 1, (
                "deleting one session must bump its user's generation"
            )
            # Unknown session id: no-op, no bump.
            await store.delete("authgen-missing")
            assert await _generation() == before + 1
            # Purge-all (admin disable cut-off) bumps once as well.
            await store.create(
                AuthSession(
                    id=f"authgen-sess-{uuid.uuid4().hex[:8]}",
                    user_id=USER,
                    issuer="https://storage-tests.example",
                    subject="authgen",
                    email=None,
                    display_name=None,
                    groups=(),
                    csrf_random="y" * 32,
                    created_at=time.time(),
                    expires_at=time.time() + 3600,
                )
            )
            await store.delete_for_user(user_id=USER)
            assert await _generation() == before + 2

        await _with_engine(op)

    _run(scenario())


def test_pat_revocation_bumps_the_generation() -> None:
    """Mutation-table row "einzelner PAT-Widerruf"."""
    from inqtrix.auth.pat import PersonalAccessToken
    from inqtrix.storage.pat_postgres import PostgresPatStore

    async def scenario() -> None:
        await _seed_user()

        async def op(engine):
            store = PostgresPatStore(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
            )
            token_id = f"authgen{uuid.uuid4().hex[:10]}"
            await store.create(
                PersonalAccessToken(
                    token_id=token_id,
                    tenant_id=TENANT,
                    owner_user_id=USER,
                    name="authgen-test",
                    secret_hmac="0" * 64,
                    created_at=time.time(),
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                )
            )
            before = await _generation() or 0
            assert await store.revoke(
                tenant_id=TENANT,
                token_id=token_id,
                owner_user_id=USER,
                now=time.time(),
            )
            assert await _generation() == before + 1
            # Double revoke is guarded: no second bump.
            assert not await store.revoke(
                tenant_id=TENANT,
                token_id=token_id,
                owner_user_id=USER,
                now=time.time(),
            )
            assert await _generation() == before + 1

        await _with_engine(op)

    _run(scenario())


def test_the_service_reader_chain_reaches_the_backend() -> None:
    """AuthorizationService -> PostgresIdentityBackend -> table.

    The routes wire the gate through this chain via getattr: a renamed
    backend method would silently degrade every stream to
    full-chain-per-frame forever (warn-once, security intact, the whole
    phase's win gone). Memory backends return None by contract.
    """
    from inqtrix.auth.identity_memory import MemoryIdentityStore
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.storage.identity_postgres import PostgresIdentityBackend

    async def scenario() -> None:
        await _seed_user()

        async def op(engine):
            backend = PostgresIdentityBackend(
                session_factory=build_session_factory(engine),
                app_role=APP_ROLE,
            )
            service = AuthorizationService(
                members=backend, shares=backend, audit=backend
            )
            before = await service.authorization_generation(
                tenant_id=TENANT, user_id=USER
            )
            assert before is not None, "known user must read a generation"
            factory = build_session_factory(engine)
            async with factory() as session:
                async with session.begin():
                    await bump_authorization_generation(
                        session, tenant_id=TENANT, target_user_ids=(USER,)
                    )
            after = await service.authorization_generation(
                tenant_id=TENANT, user_id=USER
            )
            assert after == before + 1

        await _with_engine(op)

        memory = MemoryIdentityStore()
        service = AuthorizationService(
            members=memory, shares=memory, audit=memory
        )
        assert (
            await service.authorization_generation(
                tenant_id=TENANT, user_id=USER
            )
            is None
        ), "memory backend has no generation: gate falls back to full chain"

    _run(scenario())


@pytest.fixture(scope="module", autouse=True)
def _cleanup_suite_rows():
    """Clean every row this suite creates (clean-every-fixture rule)."""
    yield
    if not TEST_DATABASE_URL:
        return

    async def wipe() -> None:
        from sqlalchemy import text as sql_text

        async def op(engine):
            async with engine.begin() as conn:
                await conn.execute(
                    sql_text(
                        "DELETE FROM personal_access_tokens "
                        "WHERE name = 'authgen-test'"
                    )
                )
                await conn.execute(
                    sql_text("DELETE FROM user_events WHERE scope = 'test'")
                )
                await conn.execute(
                    sql_text(
                        "DELETE FROM user_authorization_generations "
                        "WHERE user_id = :uid"
                    ),
                    {"uid": USER},
                )

        await _with_engine(op)

    _run(wipe())
