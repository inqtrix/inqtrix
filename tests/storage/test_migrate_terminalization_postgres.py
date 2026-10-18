"""PostgreSQL safety tests for the explicit v0.2 work terminalization."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import (
    V02PreflightReport,
    _lock_v02_cutover_tables,
    _terminalize_v02_locked,
    _v02_index_terminal_statuses_sql,
    _v02_run_terminal_statuses_sql,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres


class _MigrationContract:
    """Minimal lock inventory used by the isolated integration schema."""

    _LOCK_TABLES = '"runs", "indexing_jobs"'


_TABLE_DDL = (
    "CREATE TABLE alembic_version (version_num text PRIMARY KEY)",
    """
    CREATE TABLE runs (
        run_id text PRIMARY KEY,
        tenant_id text NOT NULL,
        status text NOT NULL,
        snapshot json NOT NULL DEFAULT '{}',
        finished_at double precision,
        error json,
        event_seq integer NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE run_events (
        run_id text NOT NULL,
        sequence integer NOT NULL,
        tenant_id text NOT NULL,
        type text NOT NULL,
        created_at double precision NOT NULL,
        data json NOT NULL,
        PRIMARY KEY (run_id, sequence)
    )
    """,
    """
    CREATE TABLE indexing_jobs (
        job_id text PRIMARY KEY,
        tenant_id text NOT NULL,
        status text NOT NULL,
        total_documents integer NOT NULL DEFAULT 0,
        completed_documents integer NOT NULL DEFAULT 0,
        current_document_title text,
        finished_at double precision,
        error json,
        event_seq integer NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE indexing_job_events (
        job_id text NOT NULL,
        sequence integer NOT NULL,
        tenant_id text NOT NULL,
        type text NOT NULL,
        created_at double precision NOT NULL,
        data json NOT NULL,
        PRIMARY KEY (job_id, sequence),
        CONSTRAINT reject_platform_upgrade_event
            CHECK (type <> 'inqtrix.index.failed')
    )
    """,
)


async def _set_search_path(connection: AsyncConnection, schema: str) -> None:
    """Select the UUID-derived isolated schema for one transaction."""
    await connection.execute(
        text(f'SET LOCAL search_path TO "{schema}", public')
    )


@pytest_asyncio.fixture()
async def terminalization_database() -> AsyncIterator[
    tuple[AsyncEngine, str]
]:
    """Create only the tables exercised by the maintenance transaction."""
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    schema = f"inqtrix_v02_{uuid.uuid4().hex}"
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await _set_search_path(connection, schema)
            for statement in _TABLE_DDL:
                await connection.execute(text(statement))
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
async def test_v02_cutover_lock_fails_nowait_when_a_table_is_in_use(
    terminalization_database: tuple[AsyncEngine, str],
) -> None:
    engine, schema = terminalization_database
    async with engine.connect() as holder, engine.connect() as contender:
        async with holder.begin():
            await _set_search_path(holder, schema)
            await holder.execute(text("LOCK TABLE runs IN ACCESS SHARE MODE"))

            with pytest.raises(DBAPIError) as exc_info:
                async with contender.begin():
                    await _set_search_path(contender, schema)
                    await _lock_v02_cutover_tables(
                        contender,
                        _MigrationContract(),
                    )
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"


@pytest.mark.asyncio
async def test_v02_terminalization_rolls_back_both_lifecycles_on_event_error(
    terminalization_database: tuple[AsyncEngine, str],
) -> None:
    engine, schema = terminalization_database
    async with engine.begin() as connection:
        await _set_search_path(connection, schema)
        await connection.execute(
            text(
                "INSERT INTO runs "
                "(run_id, tenant_id, status, snapshot, event_seq) "
                "VALUES ('run_legacy', 'tenant-a', 'running', "
                "'{\"phase\": \"search\"}'::json, 5)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO indexing_jobs "
                "(job_id, tenant_id, status, total_documents, "
                "completed_documents, current_document_title, event_seq) "
                "VALUES ('ix_legacy', 'tenant-a', 'running', 4, 1, "
                "'Document 2', 3)"
            )
        )

    report = V02PreflightReport(
        schema_revision=("0044_agent_task_cancellation",),
        authority_issues=(),
        unsupported_active_shares=0,
        orphaned_active_share_resources=0,
        nonterminal_runs=1,
        nonterminal_reindex_jobs=1,
        required_tables_present=True,
        legacy_schema_compatible=True,
    )
    with pytest.raises(IntegrityError) as exc_info:
        async with engine.begin() as connection:
            await _set_search_path(connection, schema)
            await _lock_v02_cutover_tables(
                connection,
                _MigrationContract(),
            )
            await _terminalize_v02_locked(
                connection,
                report,
                run_terminal_statuses_sql=(
                    _v02_run_terminal_statuses_sql()
                ),
                indexing_terminal_statuses_sql=(
                    _v02_index_terminal_statuses_sql()
                ),
            )
    assert "reject_platform_upgrade_event" in str(exc_info.value)
    assert getattr(exc_info.value.orig, "sqlstate", None) == "23514"

    async with engine.begin() as connection:
        await _set_search_path(connection, schema)
        run_row = (
            await connection.execute(
                text(
                    "SELECT status, finished_at, error, event_seq FROM runs "
                    "WHERE run_id = 'run_legacy'"
                )
            )
        ).one()
        indexing_row = (
            await connection.execute(
                text(
                    "SELECT status, finished_at, error, event_seq "
                    "FROM indexing_jobs WHERE job_id = 'ix_legacy'"
                )
            )
        ).one()
        run_event_count = (
            await connection.execute(text("SELECT count(*) FROM run_events"))
        ).scalar_one()
        indexing_event_count = (
            await connection.execute(
                text("SELECT count(*) FROM indexing_job_events")
            )
        ).scalar_one()

    assert tuple(run_row) == ("running", None, None, 5)
    assert tuple(indexing_row) == ("running", None, None, 3)
    assert run_event_count == 0
    assert indexing_event_count == 0
