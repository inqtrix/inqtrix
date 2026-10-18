"""Exercise the additive preset constraint against real, isolated schemas."""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config

URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.postgres
PREVIOUS = "0092_editor_library_upgrade"
HEAD = "0093_petrol_theme"


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_petrol_{uuid.uuid4().hex}"
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


async def add_preference(fixture, preset, index):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        user_id = uuid.uuid4()
        await connection.execute(text(
            "INSERT INTO users (id, tenant_id, issuer, subject, email) "
            "VALUES (:id, :tenant, 'local', :subject, :email)"
        ), {"id": user_id, "tenant": f"tenant-{index % 2}",
            "subject": str(user_id), "email": f"{user_id}@example.invalid"})
        await connection.execute(text(
            "INSERT INTO account_preferences "
            "(user_id, tenant_id, theme_preset, locale, theme, contrast_mode, user_bubble_tone, updated_at) "
            "VALUES (:id, :tenant, :preset, 'de', 'dark', 'high', 'mint', :stamp)"
        ), {"id": user_id, "tenant": f"tenant-{index % 2}", "preset": preset, "stamp": float(index)})


async def preferences(fixture):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        return [dict(row) for row in (await connection.execute(text(
            "SELECT user_id, tenant_id, theme_preset, locale, theme, contrast_mode, "
            "user_bubble_tone, updated_at FROM account_preferences ORDER BY updated_at"
        ))).mappings()]


@pytest.mark.asyncio
async def test_fresh_schema_accepts_petrol_and_rejects_unknown_presets(isolated_schema):
    await migrate(isolated_schema, HEAD)
    await add_preference(isolated_schema, "petrol", 0)
    assert (await preferences(isolated_schema))[0]["theme_preset"] == "petrol"
    with pytest.raises(IntegrityError):
        await add_preference(isolated_schema, "unknown", 1)


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_choices_and_downgrade_only_maps_petrol(isolated_schema):
    await migrate(isolated_schema, PREVIOUS)
    for index, preset in enumerate(("standard", "slate", "graphite", "sage")):
        await add_preference(isolated_schema, preset, index)
    before = await preferences(isolated_schema)
    await migrate(isolated_schema, HEAD)
    assert await preferences(isolated_schema) == before
    await add_preference(isolated_schema, "petrol", 4)
    await add_preference(isolated_schema, "petrol", 5)
    expected = [dict(row, theme_preset="standard") if row["theme_preset"] == "petrol" else row
                for row in await preferences(isolated_schema)]
    await migrate(isolated_schema, PREVIOUS, downgrade=True)
    assert await preferences(isolated_schema) == expected
    with pytest.raises(IntegrityError):
        await add_preference(isolated_schema, "petrol", 6)
    # A later upgrade must still accept the new choice.
    await migrate(isolated_schema, HEAD)
    await add_preference(isolated_schema, "petrol", 7)
