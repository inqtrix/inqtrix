"""Migration 0101 rewrites stored skill tool lists that name the retired mission tool.

``run_deep_mission`` delegated to the mission engine, which is out of service.
A skill that allowed it allows a multi-step web research instead, in every
tenant: the tool keeps its place in the list, a list that already allowed the
research names it once, no list ends up empty, and the skill's revision stays.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config

URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.postgres

PREVIOUS = "0100_runs_workflow_contract"
HEAD = "0101_retire_mission_skill_tools"
MIGRATION_LOGGER = "inqtrix.storage.migrations.versions.0101_retire_deep_mission_skill_tools"


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_skills_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        privileged = (await connection.execute(text(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))).scalar_one()
        assert privileged, "Migration fixture needs a superuser or BYPASSRLS connection"
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


async def add_skill(fixture, skill_id, tools, *, tenant="default", revision=3):
    await execute(
        fixture,
        "INSERT INTO skill_templates (id, tenant_id, label, title, instructions_markdown, "
        "allowed_tools, created_at, updated_at, revision) "
        "VALUES (:id, :tenant, :label, 'Titel', 'Anleitung', CAST(:tools AS jsonb), 1, 2, :revision)",
        {"id": skill_id, "tenant": tenant, "label": skill_id, "tools": json.dumps(tools),
         "revision": revision},
    )


async def stored(fixture, skill_id):
    rows = await execute(
        fixture,
        "SELECT allowed_tools, revision, updated_at FROM skill_templates WHERE id = :id",
        {"id": skill_id},
    )
    return rows[0]


@pytest.mark.asyncio
async def test_the_mission_tool_becomes_the_web_research_in_place(isolated_schema, caplog):
    await migrate(isolated_schema, PREVIOUS)
    await add_skill(isolated_schema, "only-mission", ["run_deep_mission"])
    await add_skill(isolated_schema, "both", ["run_deep_mission", "run_web_research"])
    await add_skill(
        isolated_schema, "in-between", ["web_instant", "run_deep_mission", "write_canvas"],
        tenant="tenant-b",
    )
    await add_skill(isolated_schema, "untouched", ["web_instant"])
    await add_skill(isolated_schema, "unrestricted", [])

    with caplog.at_level(logging.INFO, logger=MIGRATION_LOGGER):
        await migrate(isolated_schema, HEAD)

    assert await stored(isolated_schema, "only-mission") == (["run_web_research"], 3, 2)
    assert await stored(isolated_schema, "both") == (["run_web_research"], 3, 2)
    assert await stored(isolated_schema, "in-between") == (
        ["web_instant", "run_web_research", "write_canvas"], 3, 2,
    )
    assert await stored(isolated_schema, "untouched") == (["web_instant"], 3, 2)
    assert await stored(isolated_schema, "unrestricted") == ([], 3, 2)
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(": 3." in message and "run_web_research" in message for message in warnings), warnings


@pytest.mark.asyncio
async def test_the_rewritten_lists_stay_valid_after_a_downgrade(isolated_schema):
    await migrate(isolated_schema, PREVIOUS)
    await add_skill(isolated_schema, "only-mission", ["run_deep_mission"])
    await migrate(isolated_schema, HEAD)

    await migrate(isolated_schema, PREVIOUS, downgrade=True)

    assert await stored(isolated_schema, "only-mission") == (["run_web_research"], 3, 2)
