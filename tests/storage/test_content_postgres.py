"""Postgres integration tests for the file registry (gated suite).

Same gating and conventions as the identity suite: a disposable
database via ``INQTRIX_TEST_DATABASE_URL``, every operation under the
restricted app role, RLS as the second defense layer.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from inqtrix.content.memory import MemoryFileRegistry
from inqtrix.content.ports import FileNotFound, FileRecord
from inqtrix.storage.content_orm import files
from inqtrix.storage.content_postgres import PostgresFileRegistry
from inqtrix.storage.db import build_engine, build_session_factory, tenant_session
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
ALICE_USER_ID = canonical_user_id("content-alice")
BOB_USER_ID = canonical_user_id("content-bob")
MALLORY_USER_ID = canonical_user_id("content-mallory")


@pytest.fixture(scope="session", autouse=True)
def content_schema_migrated():
    """Ensure the schema is at head regardless of module run order.

    The identity suite owns the up-down-up round-trip; this module
    only needs head (idempotent when already there).
    """
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def engine():
    engine = build_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def registry(engine):
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            # Cross-tenant cleanup needs RLS bypass (FORCE binds even
            # the owner) — same fail-fast as the identity suite.
            bypasses = (
                await session.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user (cross-tenant cleanup)."
                )
            await session.execute(files.delete())
            await ensure_canonical_users(
                session,
                (ALICE_USER_ID, BOB_USER_ID, MALLORY_USER_ID),
            )
    return PostgresFileRegistry(session_factory=factory, app_role=APP_ROLE)


def make_record(
    file_id: str = "fl_test1",
    *,
    tenant_id: str = "default",
    owner_user_id: uuid.UUID = ALICE_USER_ID,
    workspace_id: str | None = None,
    created_at: float | None = None,
) -> FileRecord:
    return FileRecord(
        id=file_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        file_name="vertrag.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        sha256="ab" * 32,
        object_key=f"tenants/{tenant_id}/files/{file_id}",
        created_at=created_at if created_at is not None else time.time(),
    )


@pytest.mark.asyncio
async def test_create_get_list_delete_roundtrip(registry):
    record = make_record()
    await registry.create(record)

    assert await registry.get("fl_test1", tenant_id="default") == record

    listed = await registry.list(
        tenant_id="default", owner_user_id=ALICE_USER_ID, workspace_id=None
    )
    assert listed == [record]

    deleted = await registry.delete("fl_test1", tenant_id="default")
    assert deleted.object_key == record.object_key
    with pytest.raises(FileNotFound):
        await registry.get("fl_test1", tenant_id="default")


@pytest.mark.asyncio
async def test_listing_facets_and_ordering(registry):
    older = make_record("fl_old", created_at=100.0)
    newer = make_record("fl_new", created_at=200.0)
    foreign_owner = make_record(
        "fl_bob", owner_user_id=BOB_USER_ID, created_at=300.0
    )
    tagged = make_record(
        "fl_ws", workspace_id="ws-ui-0001", created_at=400.0
    )
    for record in (older, newer, foreign_owner, tagged):
        await registry.create(record)

    by_owner = await registry.list(
        tenant_id="default", owner_user_id=ALICE_USER_ID, workspace_id=None
    )
    assert [item.id for item in by_owner] == ["fl_ws", "fl_new", "fl_old"]

    unscoped = await registry.list(
        tenant_id="default", owner_user_id=None, workspace_id=None
    )
    assert [item.id for item in unscoped] == [
        "fl_ws",
        "fl_bob",
        "fl_new",
        "fl_old",
    ]

    by_namespace = await registry.list(
        tenant_id="default", owner_user_id=None, workspace_id="ws-ui-0001"
    )
    assert [item.id for item in by_namespace] == ["fl_ws"]


@pytest.mark.asyncio
async def test_cross_tenant_files_are_invisible(registry, engine):
    await registry.create(make_record("fl_a", tenant_id="tenant-a"))

    with pytest.raises(FileNotFound):
        await registry.get("fl_a", tenant_id="tenant-b")
    assert (
        await registry.list(
            tenant_id="tenant-b", owner_user_id=None, workspace_id=None
        )
        == []
    )

    # RLS layer 2: even a raw scoped query sees zero foreign rows.
    factory = build_session_factory(engine)
    async with tenant_session(
        factory, tenant_id="tenant-b", app_role=APP_ROLE
    ) as session:
        rows = (await session.execute(select(files.c.id))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_files_table_query_without_tenant_context_fails_loudly(engine):
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text(f'SET LOCAL ROLE "{APP_ROLE}"'))
            with pytest.raises(Exception, match="tenant_id"):
                await session.execute(select(files.c.id))


@pytest.mark.asyncio
async def test_memory_and_postgres_registry_agree(registry):
    """Port-parity guard: same arrangement, same answers."""
    memory = MemoryFileRegistry()
    record = make_record()
    for backend in (memory, registry):
        await backend.create(record)
        assert await backend.get(record.id, tenant_id="default") == record
        with pytest.raises(FileNotFound):
            await backend.get(record.id, tenant_id="tenant-x")
        assert await backend.list(
            tenant_id="default",
            owner_user_id=MALLORY_USER_ID,
            workspace_id=None,
        ) == []
        await backend.delete(record.id, tenant_id="default")
        with pytest.raises(FileNotFound):
            await backend.delete(record.id, tenant_id="default")

@pytest.mark.asyncio
async def test_files_cross_tenant_insert_violates_with_check(engine):
    """The files table carries the same RLS WITH CHECK as every other
    tenant table (revision 0002) — a foreign tenant_id insert fails."""
    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    factory = build_session_factory(engine)
    record = make_record("fl_withcheck", tenant_id="tenant-b")
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(
            factory, tenant_id="tenant-a", app_role=APP_ROLE
        ) as session:
            await session.execute(
                insert(files).values(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    owner_user_id=record.owner_user_id,
                    workspace_id=record.workspace_id,
                    file_name=record.file_name,
                    content_type=record.content_type,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    object_key=record.object_key,
                    created_at=record.created_at,
                )
            )
