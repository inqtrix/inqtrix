"""Migration 0100 binds every run to the workflow contract it was accepted under.

Existing runs take the first contract. From then on a row names its mode and
its contract itself: neither column has a default any more. The downgrade
drops only the binding and gives ``mode`` its old default back.
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

PREVIOUS = "0099_runs_status_without_expired"
HEAD = "0100_runs_workflow_contract"


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_binding_{uuid.uuid4().hex}"
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


async def revision_of(fixture):
    return (await execute(fixture, "SELECT version_num FROM alembic_version"))[0][0]


async def binding_of(fixture, run_id):
    rows = await execute(
        fixture,
        "SELECT mode, workflow_contract FROM runs WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    return tuple(rows[0])


@pytest.mark.asyncio
async def test_existing_runs_take_the_first_contract_and_new_rows_name_their_own(
    isolated_schema,
):
    await migrate(isolated_schema, PREVIOUS)
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at) VALUES ('run-old', 'q', 1)",
    )
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at, mode, status) "
        "VALUES ('run-parked', 'q', 2, 'agent_kernel', 'waiting_for_approval')",
    )

    await migrate(isolated_schema, HEAD)

    assert await binding_of(isolated_schema, "run-old") == ("research", 1)
    assert await binding_of(isolated_schema, "run-parked") == ("agent_kernel", 1)
    with pytest.raises(DBAPIError, match="workflow_contract"):
        await execute(
            isolated_schema,
            "INSERT INTO runs (run_id, question, created_at, mode) "
            "VALUES ('run-unbound', 'q', 3, 'research')",
        )
    with pytest.raises(DBAPIError, match='"mode"'):
        await execute(
            isolated_schema,
            "INSERT INTO runs (run_id, question, created_at, workflow_contract) "
            "VALUES ('run-modeless', 'q', 4, 1)",
        )
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at, mode, workflow_contract) "
        "VALUES ('run-new', 'q', 5, 'agent_kernel', 2)",
    )
    assert await binding_of(isolated_schema, "run-new") == ("agent_kernel", 2)


@pytest.mark.asyncio
async def test_the_downgrade_drops_only_the_binding_and_the_upgrade_restores_it(
    isolated_schema,
):
    await migrate(isolated_schema, PREVIOUS)
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at, mode) "
        "VALUES ('run-knowledge', 'q', 1, 'knowledge')",
    )
    await migrate(isolated_schema, HEAD)
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at, mode, workflow_contract) "
        "VALUES ('run-research', 'q', 2, 'research', 1)",
    )

    await migrate(isolated_schema, PREVIOUS, downgrade=True)

    columns = await execute(
        isolated_schema,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'runs' "
        "AND column_name = 'workflow_contract'",
    )
    assert columns == []
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at) VALUES ('run-defaulted', 'q', 3)",
    )
    modes = await execute(isolated_schema, "SELECT run_id, mode FROM runs ORDER BY run_id")
    assert [tuple(row) for row in modes] == [
        ("run-defaulted", "research"),
        ("run-knowledge", "knowledge"),
        ("run-research", "research"),
    ]

    await migrate(isolated_schema, HEAD)
    assert await binding_of(isolated_schema, "run-knowledge") == ("knowledge", 1)
    assert await binding_of(isolated_schema, "run-research") == ("research", 1)
    assert await binding_of(isolated_schema, "run-defaulted") == ("research", 1)


@pytest.mark.asyncio
async def test_a_contract_below_one_is_refused(isolated_schema):
    await migrate(isolated_schema, HEAD)

    with pytest.raises(DBAPIError, match="ck_runs_workflow_contract"):
        await execute(
            isolated_schema,
            "INSERT INTO runs (run_id, question, created_at, mode, workflow_contract) "
            "VALUES ('run-zero', 'q', 1, 'research', 0)",
        )


@pytest.mark.asyncio
async def test_the_downgrade_stops_while_an_open_run_is_bound_above_the_first_contract(
    isolated_schema,
):
    """Coming back bound to 1, such a run would be judged by the wrong contract."""
    await migrate(isolated_schema, HEAD)
    await execute(
        isolated_schema,
        "INSERT INTO runs (run_id, question, created_at, mode, workflow_contract, status) "
        "VALUES ('run-parked', 'q', 1, 'agent_kernel', 2, 'waiting_for_approval'), "
        "('run-done', 'q', 2, 'agent_kernel', 2, 'completed')",
    )

    with pytest.raises(RuntimeError, match="1 open runs are bound to a workflow contract above 1"):
        await migrate(isolated_schema, PREVIOUS, downgrade=True)

    assert await binding_of(isolated_schema, "run-parked") == ("agent_kernel", 2)
    assert await revision_of(isolated_schema) == HEAD

    await execute(
        isolated_schema,
        "UPDATE runs SET status = 'cancelled' WHERE run_id = 'run-parked'",
    )
    await migrate(isolated_schema, PREVIOUS, downgrade=True)
    assert await revision_of(isolated_schema) == PREVIOUS
