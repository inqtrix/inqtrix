"""An invalidation never deadlocks with a holder of the target's account row.

Writing an invalidation bumps the target's authorization generation and
appends an event whose foreign key share-locks the target's ``users`` row.
A transaction that already holds that row -- an account erasure start locks
its acting administrator -- and then bumps the same generation must not meet
a writer that bumped first and now waits for the row: the writer takes the
row before the generation.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select, text

from inqtrix.storage.authorization_generation import bump_authorization_generation
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import users
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from inqtrix.storage.user_events_postgres import append_user_invalidation
from tests.storage._canonical_users import ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

TENANT = "default"


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


async def _until_waiting_for_a_lock(factory, pid: int) -> None:
    """Return once ``pid`` waits for a lock; the interleaving is the test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while True:
        # A fresh transaction per look: activity statistics are snapshotted
        # per transaction.
        async with factory() as probe:
            waiting = (
                await probe.execute(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar()
        if waiting:
            return
        if loop.time() > deadline:
            raise AssertionError("the invalidation writer never waited for a lock")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_an_invalidation_takes_the_account_row_before_the_generation() -> None:
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    factory = build_session_factory(engine)
    admin = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await ensure_canonical_users(session, [admin], tenant_id=TENANT)
    try:
        async with factory() as holder, factory() as writer:
            # The holder locks the administrator's row, as an erasure start does.
            await holder.execute(
                select(users.c.id).where(users.c.id == admin).with_for_update()
            )
            writer_pid = (
                await writer.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()

            async def invalidate() -> None:
                await append_user_invalidation(
                    writer, tenant_id=TENANT, target_user_id=admin, scope="probe"
                )
                await writer.commit()

            writing = asyncio.create_task(invalidate())
            await _until_waiting_for_a_lock(factory, writer_pid)

            # ...then bumps the same administrator's generation and commits.
            await bump_authorization_generation(
                holder, tenant_id=TENANT, target_user_ids=(admin,)
            )
            await holder.commit()
            await asyncio.wait_for(writing, timeout=10)
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(user_events).where(user_events.c.target_user_id == admin)
                )
                await session.execute(
                    text(
                        "DELETE FROM user_authorization_generations WHERE user_id = :id"
                    ),
                    {"id": admin},
                )
                await session.execute(delete(users).where(users.c.id == admin))
        await engine.dispose()
