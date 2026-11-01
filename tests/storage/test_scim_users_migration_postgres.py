"""Run the SCIM users revision against real, isolated schemas."""
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
PREVIOUS = "0096_orphaned_source_deletion"
HEAD = "0097_scim_users"
SCIM_COLUMNS = ("scim_user_name", "scim_name", "scim_deleted_at", "disabled_source")
ISSUER = "https://idp.example.invalid"

# Each kind of SCIM state on its own, with the counter that must name it.
SCIM_STATES = {
    "binding": (
        "scim_user_name = 'alice.smith'",
        "scim_user_names",
    ),
    "name": (
        "scim_name = '{\"givenName\": \"Alice\", \"familyName\": \"Smith\"}'::jsonb",
        "scim_names",
    ),
    "tombstone": (
        "scim_deleted_at = now(), disabled_at = now()",
        "tombstones",
    ),
    "provisioning_disable": (
        "disabled_source = 'provisioning', disabled_at = now()",
        "provisioning_disables",
    ),
}


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_scim_{uuid.uuid4().hex}"
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


async def add_user(fixture, *, tenant="default", issuer=ISSUER, assignments=""):
    user_id = uuid.uuid4()
    await execute(
        fixture,
        "INSERT INTO users (id, tenant_id, issuer, subject, email) "
        "VALUES (:id, :tenant, :issuer, :subject, :email)",
        {
            "id": user_id,
            "tenant": tenant,
            "issuer": issuer,
            "subject": str(user_id),
            "email": f"{user_id}@example.invalid",
        },
    )
    if assignments:
        await execute(
            fixture, f"UPDATE users SET {assignments} WHERE id = :id", {"id": user_id}
        )
    return user_id


async def user_columns(fixture):
    _engine, schema = fixture
    rows = await execute(
        fixture,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = 'users'",
        {"schema": schema},
    )
    return {row.column_name for row in rows}


async def accounts(fixture):
    columns = sorted(await user_columns(fixture))
    rows = await execute(
        fixture, f"SELECT {', '.join(columns)} FROM users ORDER BY id"
    )
    return [dict(row._mapping) for row in rows]


async def installed_revision(fixture):
    rows = await execute(fixture, "SELECT version_num FROM alembic_version")
    return [row.version_num for row in rows]


@pytest.mark.asyncio
async def test_upgrade_adds_empty_columns_and_a_downgrade_without_scim_state_keeps_every_account(
    isolated_schema,
):
    await migrate(isolated_schema, PREVIOUS)
    active = await add_user(isolated_schema)
    disabled = await add_user(isolated_schema, assignments="disabled_at = now()")

    await migrate(isolated_schema, HEAD)

    assert set(SCIM_COLUMNS) <= await user_columns(isolated_schema)
    rows = {row["id"]: row for row in await accounts(isolated_schema)}
    assert {row[column] for row in rows.values() for column in SCIM_COLUMNS} == {None}
    # An administrator's disable written under the new revision: NULL means
    # the same, so the downgrade may drop it without changing the account.
    await execute(
        isolated_schema,
        "UPDATE users SET disabled_source = 'admin' WHERE id = :id",
        {"id": disabled},
    )
    expected = [
        {key: value for key, value in row.items() if key not in SCIM_COLUMNS}
        for row in await accounts(isolated_schema)
    ]

    await migrate(isolated_schema, PREVIOUS, downgrade=True)

    assert await installed_revision(isolated_schema) == [PREVIOUS]
    assert not set(SCIM_COLUMNS) & await user_columns(isolated_schema)
    assert await accounts(isolated_schema) == expected
    assert {row["id"] for row in expected if row["disabled_at"] is not None} == {disabled}
    assert active in {row["id"] for row in expected}
    indexes = await execute(
        isolated_schema,
        "SELECT indexname FROM pg_indexes WHERE schemaname = :schema "
        "AND indexname = 'uq_users_scim_user_name'",
        {"schema": isolated_schema[1]},
    )
    assert indexes == []
    # The revision can be applied again after its downgrade.
    await migrate(isolated_schema, HEAD)
    assert set(SCIM_COLUMNS) <= await user_columns(isolated_schema)


@pytest.mark.asyncio
async def test_a_user_name_is_unique_per_tenant_and_issuer_without_regard_to_case(
    isolated_schema,
):
    await migrate(isolated_schema, HEAD)
    await add_user(isolated_schema, assignments="scim_user_name = 'Alice.Smith'")
    # The same name under another issuer or in another tenant is a different
    # account; unbound accounts never collide.
    await add_user(
        isolated_schema,
        issuer="https://other-idp.example.invalid",
        assignments="scim_user_name = 'alice.smith'",
    )
    await add_user(
        isolated_schema, tenant="tenant-b", assignments="scim_user_name = 'alice.smith'"
    )
    await add_user(isolated_schema)
    await add_user(isolated_schema)

    with pytest.raises(IntegrityError, match="uq_users_scim_user_name"):
        await add_user(isolated_schema, assignments="scim_user_name = 'ALICE.SMITH'")


@pytest.mark.asyncio
async def test_disabled_source_accepts_only_admin_and_provisioning(isolated_schema):
    await migrate(isolated_schema, HEAD)
    await add_user(isolated_schema, assignments="disabled_source = 'admin'")
    await add_user(isolated_schema, assignments="disabled_source = 'provisioning'")

    with pytest.raises(IntegrityError, match="ck_users_disabled_source"):
        await add_user(isolated_schema, assignments="disabled_source = 'scim'")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(SCIM_STATES))
async def test_the_downgrade_refuses_with_counts_while_scim_state_exists(
    isolated_schema, state
):
    assignments, counter = SCIM_STATES[state]
    await migrate(isolated_schema, HEAD)
    # Counted over every tenant, never only the one of the connection.
    await add_user(isolated_schema, assignments=assignments)
    await add_user(isolated_schema, tenant="tenant-b", assignments=assignments)
    await add_user(isolated_schema, assignments="disabled_source = 'admin', disabled_at = now()")
    await add_user(isolated_schema)
    before = await accounts(isolated_schema)

    with pytest.raises(RuntimeError, match="Restore") as refused:
        await migrate(isolated_schema, PREVIOUS, downgrade=True)

    message = str(refused.value)
    assert f"{counter}=2" in message
    for other in {name for _sql, name in SCIM_STATES.values()} - {counter}:
        assert f"{other}=0" in message
    for value in ("alice", "Alice", "Smith", "example.invalid"):
        assert value not in message
    assert await installed_revision(isolated_schema) == [HEAD]
    assert set(SCIM_COLUMNS) <= await user_columns(isolated_schema)
    assert await accounts(isolated_schema) == before
