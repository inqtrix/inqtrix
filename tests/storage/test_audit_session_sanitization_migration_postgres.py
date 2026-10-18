"""Real PostgreSQL coverage for durable logout-session sanitization."""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config
from inqtrix.storage.migration_contract import SCHEMA_HEAD_REVISION

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

_SOURCE_REVISION = "0074_llm_usage_run_index"
_TENANT_ID = "audit-session-sanitization"
_RAW_SESSION_ID = "A" * 43
_SAFE_SESSION_REFERENCE = "ses_0123456789abcdef"
_OTHER_ACTION_RESOURCE = "B" * 43
_OTHER_TYPE_RESOURCE = "C" * 43
_SAFE_REFERENCE = re.compile(r"^ses_[0-9a-f]{16}$")


async def _set_search_path(
    connection: AsyncConnection,
    schema: str,
    *,
    local: bool,
) -> None:
    qualifier = "LOCAL " if local else ""
    await connection.execute(
        text(f'SET {qualifier}search_path TO "{schema}"')
    )


async def _upgrade_schema(
    engine: AsyncEngine,
    schema: str,
    revision: str,
) -> None:
    def upgrade(sync_connection: object) -> None:
        config = build_alembic_config(TEST_DATABASE_URL)
        config.attributes["connection"] = sync_connection
        config.attributes["version_table_schema"] = schema
        command.upgrade(config, revision)

    async with engine.connect() as connection:
        await _set_search_path(connection, schema, local=False)
        await connection.commit()
        await connection.run_sync(upgrade)


async def _current_revision(engine: AsyncEngine, schema: str) -> str:
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        return str(
            (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one()
        )


@pytest_asyncio.fixture()
async def isolated_schema() -> AsyncIterator[tuple[AsyncEngine, str]]:
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    schema = f"inqtrix_audit_session_{uuid.uuid4().hex}"
    try:
        async with engine.begin() as connection:
            is_privileged = bool(
                (
                    await connection.execute(
                        text(
                            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
            )
            if not is_privileged:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user for migration tests"
                )
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield engine, schema
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_empty_audit_log_upgrades_to_head(
    isolated_schema: tuple[AsyncEngine, str],
) -> None:
    engine, schema = isolated_schema
    await _upgrade_schema(engine, schema, _SOURCE_REVISION)

    await _upgrade_schema(engine, schema, "head")

    assert await _current_revision(engine, schema) == SCHEMA_HEAD_REVISION
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        count = (
            await connection.execute(text("SELECT count(*) FROM audit_log"))
        ).scalar_one()
    assert count == 0


async def _seed_audit_rows(
    engine: AsyncEngine,
    schema: str,
) -> None:
    actor_id = uuid.uuid4()
    rows = (
        ("legacy-a", "auth.logout", "session", _RAW_SESSION_ID),
        ("legacy-b", "auth.logout", "session", _RAW_SESSION_ID),
        (
            "already-safe",
            "auth.logout",
            "session",
            _SAFE_SESSION_REFERENCE,
        ),
        (
            "other-action",
            "auth.login_failed",
            "session",
            _OTHER_ACTION_RESOURCE,
        ),
        (
            "other-type",
            "auth.logout",
            "user",
            _OTHER_TYPE_RESOURCE,
        ),
    )
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, issuer, subject, email, email_verified, "
                "display_name, instance_role) "
                "VALUES (:id, :tenant, 'local', 'owner', "
                "'owner@example.invalid', true, 'Owner', 'admin')"
            ),
            {"id": actor_id, "tenant": _TENANT_ID},
        )
        for marker, action, resource_type, resource_id in rows:
            await connection.execute(
                text(
                    "INSERT INTO audit_log "
                    "(tenant_id, actor_user_id, action, resource_type, "
                    "resource_id, detail, outcome, origin, correlation, "
                    "actor_pseudonym) VALUES "
                    "(:tenant, :actor, :action, :resource_type, "
                    ":resource_id, CAST(:detail AS jsonb), 'success', "
                    "CAST(:origin AS jsonb), CAST(:correlation AS jsonb), "
                    "'usr_0123456789abcdef')"
                ),
                {
                    "tenant": _TENANT_ID,
                    "actor": actor_id,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "detail": json.dumps({"marker": marker}),
                    "origin": json.dumps({"auth_method": "local"}),
                    "correlation": json.dumps({"request_id": "req-1"}),
                },
            )


async def _audit_rows(
    engine: AsyncEngine,
    schema: str,
) -> dict[str, dict[str, object]]:
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        rows = (
            await connection.execute(
                text(
                    "SELECT detail->>'marker' AS marker, action, "
                    "resource_type, resource_id, outcome, origin, "
                    "correlation, actor_pseudonym "
                    "FROM audit_log ORDER BY id"
                )
            )
        ).mappings()
        return {str(row["marker"]): dict(row) for row in rows}


@pytest.mark.asyncio
async def test_populated_audit_log_is_sanitized_and_idempotent(
    isolated_schema: tuple[AsyncEngine, str],
) -> None:
    engine, schema = isolated_schema
    await _upgrade_schema(engine, schema, _SOURCE_REVISION)
    await _seed_audit_rows(engine, schema)

    await _upgrade_schema(engine, schema, "head")

    assert await _current_revision(engine, schema) == SCHEMA_HEAD_REVISION
    rows = await _audit_rows(engine, schema)
    first = rows["legacy-a"]["resource_id"]
    second = rows["legacy-b"]["resource_id"]
    assert isinstance(first, str) and _SAFE_REFERENCE.fullmatch(first)
    assert second == first
    assert _RAW_SESSION_ID not in {first, second}
    assert rows["already-safe"]["resource_id"] == _SAFE_SESSION_REFERENCE
    assert rows["other-action"]["resource_id"] == _OTHER_ACTION_RESOURCE
    assert rows["other-type"]["resource_id"] == _OTHER_TYPE_RESOURCE
    for marker in ("legacy-a", "legacy-b", "already-safe"):
        row = rows[marker]
        assert row["outcome"] == "success"
        assert dict(row["origin"]) == {"auth_method": "local"}
        assert dict(row["correlation"]) == {"request_id": "req-1"}
        assert row["actor_pseudonym"] == "usr_0123456789abcdef"

    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0075_audit_session_references"
    )
    before = {
        marker: row["resource_id"] for marker, row in rows.items()
    }
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        await connection.execute(text(migration._SANITIZE_SQL))
        await connection.execute(text(migration._POSTCONDITION_SQL))
    after = {
        marker: row["resource_id"]
        for marker, row in (await _audit_rows(engine, schema)).items()
    }
    assert after == before
