"""Real PostgreSQL traversal tests for the irreversible identity cutover.

Each test owns an isolated schema in a disposable database.  The legacy
fixtures are created by Alembic itself through revision 0044; no hand-written
approximation of the old schema is used.  This makes the tests sensitive to
historical migration drift as well as to the 0045 data transformation.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config
from inqtrix.storage.migration_contract import SCHEMA_HEAD_REVISION

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

# 0044 is a frozen historical point and stays a literal. The head is derived,
# so adding a migration cannot leave this test pinning a stale revision.
_LEGACY_REVISION = "0044_agent_task_cancellation"
_TENANT_ID = "cutover-test"


async def _set_search_path(
    connection: AsyncConnection,
    schema: str,
    *,
    local: bool,
) -> None:
    # Keep the migration namespace exclusive.  SQLAlchemy's PostgreSQL
    # ``has_table(..., schema=None)`` check follows the complete search path;
    # including ``public`` would therefore make a populated public schema look
    # like this fresh test schema already owns the historical tables.  The
    # subsequent unqualified policy DDL would then mutate public instead of the
    # isolated fixture.  PostgreSQL still searches ``pg_catalog`` implicitly.
    qualifier = "LOCAL " if local else ""
    await connection.execute(text(f'SET {qualifier}search_path TO "{schema}"'))


async def _upgrade_schema(
    engine: AsyncEngine,
    schema: str,
    revision: str,
) -> None:
    """Run one Alembic target on a dedicated connection/search path."""

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
    """Provide one empty schema in the disposable integration database."""
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    schema = f"inqtrix_identity_cutover_{uuid.uuid4().hex}"
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


@pytest_asyncio.fixture()
async def legacy_schema(
    isolated_schema: tuple[AsyncEngine, str],
) -> tuple[AsyncEngine, str]:
    """Create the exact packaged 0044 schema through Alembic."""
    engine, schema = isolated_schema
    await _upgrade_schema(engine, schema, _LEGACY_REVISION)
    assert await _current_revision(engine, schema) == _LEGACY_REVISION
    return engine, schema


@pytest.mark.asyncio
async def test_fresh_install_traverses_frozen_history_to_head(
    isolated_schema: tuple[AsyncEngine, str],
) -> None:
    """A new database must traverse every immutable revision to current head."""
    engine, schema = isolated_schema

    await _upgrade_schema(engine, schema, "head")

    assert await _current_revision(engine, schema) == SCHEMA_HEAD_REVISION
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        columns = {
            str(row.column_name)
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'resource_shares'"
                    ),
                    {"schema": schema},
                )
            )
        }
    assert "recipient_user_id" in columns
    assert "subject_id" not in columns


async def _seed_supported_cutover_data(
    engine: AsyncEngine,
    schema: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, issuer, subject, email) VALUES "
                "(:owner_id, :tenant_id, 'https://idp.example', "
                "'legacy-owner', 'owner@example.com'), "
                "(:recipient_id, :tenant_id, 'https://idp.example', "
                "'legacy-recipient', 'recipient@example.com')"
            ),
            {
                "owner_id": owner_id,
                "recipient_id": recipient_id,
                "tenant_id": _TENANT_ID,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO auth_sessions "
                "(id, tenant_id, sub, issuer, email, display_name, groups, "
                "csrf_random, created_at, expires_at) VALUES "
                "('sess_legacy', :tenant_id, 'legacy-owner', "
                "'https://idp.example', 'owner@example.com', 'Owner', "
                "'[]'::jsonb, 'csrf', 1, 9999999999)"
            ),
            {"tenant_id": _TENANT_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO personal_access_tokens "
                "(token_id, tenant_id, owner_issuer, owner_sub, name, "
                "secret_hmac, scopes, created_at) VALUES "
                "('pat_legacy', :tenant_id, 'https://idp.example', "
                "'legacy-owner', 'Legacy PAT', 'digest', '[]'::jsonb, 1)"
            ),
            {"tenant_id": _TENANT_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO prompt_templates "
                "(id, tenant_id, owner_sub, title, label, content_markdown, "
                "created_at, updated_at) VALUES "
                "('pt_legacy', :tenant_id, 'legacy-owner', 'Prompt', "
                "'Prompt', 'Body', 1, 1)"
            ),
            {"tenant_id": _TENANT_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO resource_shares "
                "(id, tenant_id, subject_type, subject_id, resource_type, "
                "resource_id, permission, granted_by_sub, accepted_at) "
                "VALUES "
                "(:supported_id, :tenant_id, 'user', 'legacy-recipient', "
                "'prompt_template', 'pt_legacy', 'edit', 'legacy-owner', "
                "now()), "
                "(:orphan_id, :tenant_id, 'user', 'legacy-recipient', "
                "'prompt_template', 'pt_missing', 'view', 'legacy-owner', "
                "now())"
            ),
            {
                "supported_id": uuid.uuid4(),
                "orphan_id": uuid.uuid4(),
                "tenant_id": _TENANT_ID,
            },
        )
    return owner_id, recipient_id


@pytest.mark.asyncio
async def test_0044_supported_authorities_and_shares_reach_current_head(
    legacy_schema: tuple[AsyncEngine, str],
) -> None:
    """Sessions, PATs and supported direct shares preserve their identity."""
    engine, schema = legacy_schema
    owner_id, recipient_id = await _seed_supported_cutover_data(engine, schema)

    await _upgrade_schema(engine, schema, "head")

    assert await _current_revision(engine, schema) == SCHEMA_HEAD_REVISION
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        session_row = (
            await connection.execute(
                text(
                    "SELECT user_id, subject FROM auth_sessions "
                    "WHERE id = 'sess_legacy'"
                )
            )
        ).one()
        pat_owner = (
            await connection.execute(
                text(
                    "SELECT owner_user_id FROM personal_access_tokens "
                    "WHERE token_id = 'pat_legacy'"
                )
            )
        ).scalar_one()
        prompt_owner = (
            await connection.execute(
                text(
                    "SELECT owner_user_id FROM prompt_templates "
                    "WHERE id = 'pt_legacy'"
                )
            )
        ).scalar_one()
        shares = (
            await connection.execute(
                text(
                    "SELECT resource_id, recipient_user_id, "
                    "granted_by_user_id, revision, accepted_at, revoked_at "
                    "FROM resource_shares ORDER BY resource_id"
                )
            )
        ).all()

    assert tuple(session_row) == (owner_id, "legacy-owner")
    assert pat_owner == owner_id
    assert prompt_owner == owner_id
    assert len(shares) == 2
    shares_by_resource = {row.resource_id: row for row in shares}
    orphaned = shares_by_resource["pt_missing"]
    supported = shares_by_resource["pt_legacy"]
    assert orphaned.resource_id == "pt_missing"
    assert orphaned.revoked_at is not None
    assert supported.resource_id == "pt_legacy"
    assert supported.recipient_user_id == recipient_id
    assert supported.granted_by_user_id == owner_id
    assert supported.revision == 1
    assert supported.accepted_at is not None
    assert supported.revoked_at is None


async def _seed_authority_failure(
    engine: AsyncEngine,
    schema: str,
    case: str,
) -> None:
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        owner_id = uuid.uuid4()
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, issuer, subject, email) VALUES "
                "(:owner_id, :tenant_id, 'https://idp-a.example', "
                "'legacy-owner', 'owner-a@example.com')"
            ),
            {"owner_id": owner_id, "tenant_id": _TENANT_ID},
        )
        if case == "ambiguous":
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, tenant_id, issuer, subject, email) VALUES "
                    "(:user_id, :tenant_id, 'https://idp-b.example', "
                    "'legacy-owner', 'owner-b@example.com')"
                ),
                {"user_id": uuid.uuid4(), "tenant_id": _TENANT_ID},
            )
        if case in {"ambiguous", "orphaned"}:
            owner_sub = "legacy-owner" if case == "ambiguous" else "missing"
            await connection.execute(
                text(
                    "INSERT INTO files "
                    "(id, tenant_id, owner_sub, file_name, content_type, "
                    "size_bytes, sha256, object_key, created_at) VALUES "
                    "('fl_failure', :tenant_id, :owner_sub, 'file.txt', "
                    "'text/plain', 1, 'digest', :object_key, 1)"
                ),
                {
                    "tenant_id": _TENANT_ID,
                    "owner_sub": owner_sub,
                    "object_key": f"failure/{case}",
                },
            )
        else:
            await connection.execute(
                text(
                    "INSERT INTO resource_shares "
                    "(id, tenant_id, subject_type, subject_id, resource_type, "
                    "resource_id, permission, granted_by_sub, accepted_at) "
                    "VALUES (:share_id, :tenant_id, 'group', 'legacy-group', "
                    "'prompt_template', 'pt_missing', 'view', "
                    "'legacy-owner', now())"
                ),
                {"share_id": uuid.uuid4(), "tenant_id": _TENANT_ID},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("ambiguous", "more than one issuer-scoped user match"),
        ("orphaned", "no (tenant_id, issuer, subject) user match"),
        ("unsupported", "active group/file/comment/manage"),
    ),
)
async def test_0045_rejects_unsafe_legacy_authority_atomically(
    legacy_schema: tuple[AsyncEngine, str],
    case: str,
    message: str,
) -> None:
    """Ambiguous, orphaned and unsupported authority never partially cut over."""
    engine, schema = legacy_schema
    await _seed_authority_failure(engine, schema, case)

    with pytest.raises(DBAPIError, match=re.escape(message)):
        await _upgrade_schema(engine, schema, "head")

    assert await _current_revision(engine, schema) == _LEGACY_REVISION
    async with engine.begin() as connection:
        await _set_search_path(connection, schema, local=True)
        legacy_columns = {
            str(row.column_name)
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'resource_shares'"
                    ),
                    {"schema": schema},
                )
            )
        }
    assert "subject_id" in legacy_columns
    assert "recipient_user_id" not in legacy_columns
