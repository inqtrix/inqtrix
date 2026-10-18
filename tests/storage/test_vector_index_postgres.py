"""Postgres integration tests for the vector-index-record store (gated)."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.pagination import decode_cursor
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.vector_index_postgres import PostgresVectorIndexStore
from inqtrix.project.vector_index_ports import (
    VectorIndexHistoryEntry,
    VectorIndexMember,
    VectorIndexNotFound,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.asset_records_orm import asset_records, asset_sections
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.vector_index_orm import (
    vector_index_history,
    vector_index_members,
    vector_index_records,
)
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_ID = canonical_user_id("vector-index-user")
USER_1_ID = canonical_user_id("vector-index-user-1")
USER_2_ID = canonical_user_id("vector-index-user-2")
OTHER_USER_ID = canonical_user_id("vector-index-other-user")


@pytest.fixture(scope="session", autouse=True)
def vector_index_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def store():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail("INQTRIX_TEST_DATABASE_URL must connect as superuser/BYPASSRLS.")
            await session.execute(vector_index_history.delete())
            await session.execute(vector_index_members.delete())
            await session.execute(vector_index_records.delete())
            await session.execute(asset_records.delete())
            await session.execute(asset_sections.delete())
            await ensure_canonical_users(
                session,
                (USER_ID, USER_1_ID, USER_2_ID, OTHER_USER_ID),
            )
    vector_store = PostgresVectorIndexStore(engine=engine, app_role=APP_ROLE)
    yield vector_store
    await vector_store.aclose()


async def _save(
    store,
    iid,
    *,
    owner: uuid.UUID = USER_ID,
    created_at=1.0,
    members=(),
    history=(),
):
    await _ensure_member_assets(
        store,
        owner,
        tuple(member.file_id for member in members),
    )
    return await store.upsert_index(
        id=iid, title="Idx", handle="idx", model="text-embedding-3-large",
        dims=3072, status="ready", server_collection_id=None,
        server_collection_model=None, last_error=None,
        members=members, history=history, created_at=created_at, updated_at=created_at,
        created_by_user_id=owner, workspace_id=None,
    )


async def _ensure_member_assets(store, owner: uuid.UUID, asset_ids: tuple[str, ...]):
    if not asset_ids:
        return
    section_id = f"sec_{owner.hex}"
    async with store._session() as session:
        await session.execute(
            pg_insert(asset_sections)
            .values(
                id=section_id,
                tenant_id="default",
                created_by_user_id=owner,
                workspace_id=None,
                kind="custom",
                title="Fixture",
                created_at=1.0,
                updated_at=1.0,
            )
            .on_conflict_do_nothing(index_elements=[asset_sections.c.id])
        )
        for asset_id in asset_ids:
            await session.execute(
                pg_insert(asset_records)
                .values(
                    id=asset_id,
                    tenant_id="default",
                    created_by_user_id=owner,
                    workspace_id=None,
                    section_id=section_id,
                    title=asset_id,
                    label=asset_id,
                    file_name=f"{asset_id}.txt",
                    mime_type="text/plain",
                    origin="library",
                    size_bytes=1,
                    extracted_text="x",
                    lifecycle_status="active",
                    created_at=1.0,
                    updated_at=1.0,
                )
                .on_conflict_do_nothing(index_elements=[asset_records.c.id])
            )


@pytest.mark.asyncio
async def test_list_returns_full_records_with_children(store) -> None:
    # Insert members in a NON-alphabetical order: a file_id sort would flip
    # them, so this pins the client-array-order round-trip (the seq column).
    await _save(
        store, "vix_1",
        members=(
            VectorIndexMember("fa_2", "embedded", server_document_id="kd_2"),
            VectorIndexMember("fa_1", "pending"),
        ),
        history=(VectorIndexHistoryEntry("ok", 2, 1500, None, 1.0, 2.5),
                 VectorIndexHistoryEntry("error", 0, 9, "boom", 0.0, 0.4)),
    )
    page, _ = await store.list_indexes_page(
        created_by_user_id=USER_ID,
        workspace_id=None,
        limit=50,
        after=None,
    )
    record = page[0]
    assert [(m.file_id, m.state) for m in record.members] == [("fa_2", "embedded"), ("fa_1", "pending")]
    # The backend doc id persists (lets a post-reload "remove" delete the exact
    # document); a member without one round-trips as None.
    assert [m.server_document_id for m in record.members] == ["kd_2", None]
    # history preserves the supplied newest-first order (seq asc).
    assert [h.result for h in record.history] == ["ok", "error"]
    assert record.history[1].error == "boom"


@pytest.mark.asyncio
async def test_keyset_walk_and_owner_scope(store) -> None:
    for n, ts in enumerate([10.0, 10.0, 20.0, 30.0, 30.0]):
        await _save(store, f"vix_{n}", owner=USER_1_ID, created_at=ts)
    await _save(store, "vix_other", owner=USER_2_ID, created_at=5.0)
    seen, cursor = [], None
    for _ in range(10):
        pg, nxt = await store.list_indexes_page(
            created_by_user_id=USER_1_ID,
            workspace_id=None,
            limit=2,
            after=cursor,
        )
        seen.extend(r.id for r in pg)
        if nxt is None:
            break
        cursor = decode_cursor(nxt)
    assert len(seen) == len(set(seen)) == 5
    assert "vix_other" not in seen


@pytest.mark.asyncio
async def test_upsert_preserves_created_at_and_replaces_children(store) -> None:
    await _save(
        store, "vix_1", owner=USER_1_ID, created_at=100.0,
        members=(VectorIndexMember("fa_1", "embedded"), VectorIndexMember("fa_2", "embedded")),
        history=(VectorIndexHistoryEntry("ok", 2, 10, None, 1.0, 2.0),),
    )
    await _ensure_member_assets(store, USER_1_ID, ("fa_3",))
    await store.upsert_index(
        id="vix_1", title="renamed", handle="idx", model="text-embedding-3-large",
        dims=3072, status="stale", server_collection_id="kc_9",
        server_collection_model="text-embedding-3-large", last_error=None,
        members=(VectorIndexMember("fa_3", "pending"),),
        history=(VectorIndexHistoryEntry("cancelled", 1, 7, None, 3.0, 3.5),
                 VectorIndexHistoryEntry("ok", 2, 10, None, 1.0, 2.0)),
        created_at=999.0, updated_at=200.0,
        created_by_user_id=USER_1_ID, workspace_id=None,
    )
    record = await store.get_index("vix_1")
    assert record.title == "renamed"
    assert record.created_at == 100.0
    assert record.created_by_user_id == USER_1_ID
    assert [m.file_id for m in record.members] == ["fa_3"]
    assert [h.result for h in record.history] == ["cancelled", "ok"]


@pytest.mark.asyncio
async def test_cross_owner_index_id_collision_is_not_found(store) -> None:
    await _save(store, "vix_1", owner=USER_1_ID)

    with pytest.raises(VectorIndexNotFound):
        await _save(store, "vix_1", owner=OTHER_USER_ID)

    assert (await store.get_index("vix_1")).created_by_user_id == USER_1_ID


@pytest.mark.asyncio
async def test_delete_cascades_children(store) -> None:
    await _save(
        store, "vix_1", owner=USER_ID,
        members=(VectorIndexMember("fa_1", "embedded"),),
        history=(VectorIndexHistoryEntry("ok", 1, 10, None, 1.0, 2.0),),
    )
    await store.delete_index(
        "vix_1",
        scope=ResourceScope.from_record(await store.get_index("vix_1")),
    )
    async with store._session() as session:
        members = (await session.execute(
            select(func.count()).select_from(vector_index_members)
        )).scalar_one()
        history = (await session.execute(
            select(func.count()).select_from(vector_index_history)
        )).scalar_one()
    assert members == 0
    assert history == 0
