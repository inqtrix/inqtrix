"""The last report profile and Stufe against real, isolated schemas.

The revision adds two account-preference columns that start without a pick,
refuses values outside their vocabularies, keeps every saved preference on
the way up, and removes exactly its own columns and rules on the way down.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config

URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.postgres

PREVIOUS = "0101_retire_mission_skill_tools"
REVISION = "0102_account_last_tiers"
COLUMNS = ("last_research_profile", "last_agent_tier")
KEPT = (
    "user_id, tenant_id, contrast_mode, locale, theme, theme_preset, user_bubble_tone, "
    "enable_agent_memory, chat_model_tier, agent_model_tier, updated_at"
)


@asynccontextmanager
async def _schema(engine):
    schema = f"inqtrix_last_tiers_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield engine, schema
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest_asyncio.fixture()
async def engine():
    engine = build_engine(URL, null_pool=True)
    async with engine.begin() as connection:
        privileged = (await connection.execute(text(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))).scalar_one()
        assert privileged, "Migration fixture needs a superuser or BYPASSRLS connection"
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def isolated_schema(engine):
    async with _schema(engine) as fixture:
        yield fixture


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
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


async def add_preference(fixture, index, **last_choices):
    user_id = uuid.uuid4()
    tenant = f"tenant-{index % 2}"
    await execute(fixture, (
        "INSERT INTO users (id, tenant_id, issuer, subject, email) "
        "VALUES (:id, :tenant, 'local', :subject, :email)"
    ), {"id": user_id, "tenant": tenant, "subject": str(user_id), "email": f"{user_id}@example.invalid"})
    columns = ", ".join(last_choices)
    values = ", ".join(f":{column}" for column in last_choices)
    await execute(fixture, (
        "INSERT INTO account_preferences (user_id, tenant_id, contrast_mode, locale, theme, "
        "theme_preset, user_bubble_tone, enable_agent_memory, chat_model_tier, "
        f"agent_model_tier, updated_at{', ' + columns if columns else ''}) "
        "VALUES (:id, :tenant, 'high', 'de', 'dark', 'sage', 'mint', true, 'mid', 'fast', "
        f":stamp{', ' + values if values else ''})"
    ), {"id": user_id, "tenant": tenant, "stamp": float(index), **last_choices})
    return user_id


async def kept_columns(fixture):
    return await execute(fixture, f"SELECT {KEPT} FROM account_preferences ORDER BY updated_at")


async def last_choices(fixture):
    return [
        (row["last_research_profile"], row["last_agent_tier"])
        for row in await execute(fixture, (
            "SELECT last_research_profile, last_agent_tier FROM account_preferences "
            "ORDER BY updated_at"
        ))
    ]


async def present_columns(fixture):
    _engine, schema = fixture
    rows = await execute(fixture, (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = 'account_preferences'"
    ), {"schema": schema})
    return {row["column_name"] for row in rows}


async def present_rules(fixture):
    _engine, schema = fixture
    rows = await execute(fixture, (
        "SELECT rule.conname FROM pg_constraint AS rule "
        "JOIN pg_class AS owner ON owner.oid = rule.conrelid "
        "JOIN pg_namespace AS space ON space.oid = owner.relnamespace "
        "WHERE space.nspname = :schema AND owner.relname = 'account_preferences' "
        "AND rule.conname LIKE 'ck_account_preferences_last_%'"
    ), {"schema": schema})
    return {row["conname"] for row in rows}


async def assert_refuses_unknown_picks(fixture, index):
    for column, value in (
        ("last_research_profile", "turbo"),
        ("last_research_profile", "tief"),
        ("last_agent_tier", "deep"),
        ("last_agent_tier", "standard"),
    ):
        with pytest.raises(IntegrityError):
            await add_preference(fixture, index, **{column: value})


@pytest.mark.asyncio
async def test_a_fresh_schema_stores_the_picks_and_refuses_unknown_ones(isolated_schema):
    await migrate(isolated_schema, REVISION)
    await add_preference(isolated_schema, 0, last_research_profile="compact", last_agent_tier="tief")
    await add_preference(isolated_schema, 1)

    assert await last_choices(isolated_schema) == [("compact", "tief"), ("", "")]
    await assert_refuses_unknown_picks(isolated_schema, 2)


@pytest.mark.asyncio
async def test_the_upgrade_keeps_every_saved_preference_and_starts_without_a_pick(isolated_schema):
    await migrate(isolated_schema, PREVIOUS)
    for index in range(3):
        await add_preference(isolated_schema, index)
    before = await kept_columns(isolated_schema)

    await migrate(isolated_schema, REVISION)

    assert await kept_columns(isolated_schema) == before
    assert await last_choices(isolated_schema) == [("", "")] * 3


@pytest.mark.asyncio
async def test_the_downgrade_removes_both_columns_and_their_rules_and_keeps_the_rest(isolated_schema):
    await migrate(isolated_schema, REVISION)
    await add_preference(isolated_schema, 0, last_research_profile="schnell", last_agent_tier="schnell")
    before = await kept_columns(isolated_schema)

    await migrate(isolated_schema, PREVIOUS, downgrade=True)

    assert not set(COLUMNS) & await present_columns(isolated_schema)
    assert await present_rules(isolated_schema) == set()
    assert await kept_columns(isolated_schema) == before

    # A later upgrade adds both again, without a pick for the kept row.
    await migrate(isolated_schema, REVISION)
    assert set(COLUMNS) <= await present_columns(isolated_schema)
    assert await last_choices(isolated_schema) == [("", "")]
    await assert_refuses_unknown_picks(isolated_schema, 1)


@pytest.mark.asyncio
async def test_each_schema_of_one_database_gets_its_own_rules(engine):
    """A rule name is unique per table, not per database.

    When another schema already carries a rule of the same name, a lookup
    that ignores the table would skip the rule here and leave the columns
    unguarded.
    """
    async with _schema(engine) as first, _schema(engine) as second:
        await migrate(first, REVISION)
        await migrate(second, REVISION)

        for fixture in (first, second):
            assert await present_rules(fixture) == {
                f"ck_account_preferences_{column}" for column in COLUMNS
            }
            await assert_refuses_unknown_picks(fixture, 0)
