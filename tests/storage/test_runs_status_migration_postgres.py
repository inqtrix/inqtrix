"""Migration 0099 removes ``expired`` from the run statuses.

No run reaches that status; a wait that runs out ends the run as ``cancelled``.
A run that still carries it stops the upgrade instead of being rewritten, and
the downgrade allows the value again.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config

URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.postgres

PREVIOUS = "0098_project_namespace_backfill"
HEAD = "0099_runs_status_without_expired"


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_status_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield engine, schema
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def migrate(fixture, revision, *, downgrade=False):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))

        def apply(sync):
            config = build_alembic_config(URL)
            config.attributes["connection"] = sync
            config.attributes["version_table_schema"] = schema
            (command.downgrade if downgrade else command.upgrade)(config, revision)

        await connection.run_sync(apply)


async def execute(fixture, statement, parameters=None):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        result = await connection.execute(text(statement), parameters or {})
        return result.all() if result.returns_rows else []


async def add_run(fixture, run_id, status):
    await execute(
        fixture,
        "INSERT INTO runs (run_id, question, created_at, status) "
        "VALUES (:run_id, 'question', 1, :status)",
        {"run_id": run_id, "status": status},
    )


async def status_of(fixture, run_id):
    rows = await execute(
        fixture, "SELECT status FROM runs WHERE run_id = :run_id", {"run_id": run_id}
    )
    return rows[0][0]


async def revision_of(fixture):
    return (await execute(fixture, "SELECT version_num FROM alembic_version"))[0][0]


@pytest.mark.asyncio
async def test_a_run_that_still_has_the_status_expired_stops_the_upgrade(isolated_schema):
    await migrate(isolated_schema, PREVIOUS)
    await add_run(isolated_schema, "run-expired", "expired")

    with pytest.raises(RuntimeError, match="1 runs still have the status 'expired'"):
        await migrate(isolated_schema, HEAD)

    assert await status_of(isolated_schema, "run-expired") == "expired"
    assert await revision_of(isolated_schema) == PREVIOUS


@pytest.mark.asyncio
async def test_no_run_takes_the_status_expired_after_the_upgrade_and_the_downgrade_allows_it(
    isolated_schema,
):
    await migrate(isolated_schema, PREVIOUS)
    await add_run(isolated_schema, "run-done", "completed")
    await migrate(isolated_schema, HEAD)

    with pytest.raises(DBAPIError, match="ck_runs_status"):
        await execute(
            isolated_schema,
            "UPDATE runs SET status = 'expired' WHERE run_id = 'run-done'",
        )
    await add_run(isolated_schema, "run-waiting", "waiting_for_children")

    await migrate(isolated_schema, PREVIOUS, downgrade=True)
    await execute(
        isolated_schema, "UPDATE runs SET status = 'expired' WHERE run_id = 'run-done'"
    )
    assert await status_of(isolated_schema, "run-done") == "expired"
    assert await status_of(isolated_schema, "run-waiting") == "waiting_for_children"
