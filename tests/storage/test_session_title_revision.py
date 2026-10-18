"""Real row-lock and ownership proofs for conversation title updates."""

import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from inqtrix.project.agent_sessions_postgres import PostgresAgentSessionStore
from inqtrix.project.chat_postgres import PostgresChatStore
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.session_titles import SessionTitleConflict, generated_title_update
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.chat_orm import chat_threads
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import canonical_user_id, ensure_canonical_users

pytestmark = pytest.mark.postgres
URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
OWNER = canonical_user_id("title-owner")
OTHER = canonical_user_id("title-other")
SCOPE = ResourceScope(OWNER, "workspace-one")


@pytest.fixture(scope="session", autouse=True)
def migrated_titles():
    if URL:
        run_migrations(URL)


@pytest_asyncio.fixture(params=["chat", "agent"])
async def target(request):
    engine = build_engine(URL)
    factory = build_session_factory(engine)
    async with factory() as session, session.begin():
        await ensure_canonical_users(session, (OWNER, OTHER))
    kind = request.param
    table = chat_threads if kind == "chat" else agent_sessions
    store = (
        PostgresChatStore(engine=engine, app_role="inqtrix_app")
        if kind == "chat"
        else PostgresAgentSessionStore(engine=engine, app_role="inqtrix_app")
    )
    resource_id = f"title-{uuid.uuid4()}"

    async def save(title="Initial", revision=0, source="initial", timestamp=10):
        common = dict(
            id=resource_id,
            title=title,
            title_revision=revision,
            title_source=source,
            created_at=10,
            updated_at=timestamp,
            created_by_user_id=OWNER,
            workspace_id="workspace-one",
            group_id=None,
        )
        return await (
            store.upsert_thread(**common, preview="content", source="api")
            if kind == "chat"
            else store.upsert_session(**common, items_json="content")
        )

    await save()
    yield store, save, table, resource_id, factory
    async with factory() as session, session.begin():
        await session.execute(table.delete().where(table.c.id == resource_id))
    await store.aclose()


@pytest.mark.asyncio
async def test_database_title_rules_match_manual_priority_and_preserve_activity(target):
    store, save, _, resource_id, _ = target
    assert await store.apply_generated_title(
        resource_id, title="Short topic", revision=0, scope=SCOPE
    )
    manual = await save("My project", 0, "manual", 11)
    assert (manual.title_revision, manual.title_source) == (2, "manual")
    stale = await save("Initial", 0, "initial", 99)
    assert (
        stale.title,
        stale.title_revision,
        stale.title_source,
        stale.updated_at,
    ) == ("My project", 2, "manual", 99)
    assert not await store.apply_generated_title(
        resource_id, title="Late", revision=0, scope=SCOPE
    )


@pytest.mark.asyncio
async def test_same_text_manual_choice_and_stale_legacy_save_remain_protected(target):
    store, save, _, resource_id, _ = target
    manual = await save("Initial", 0, "manual", 11)
    assert manual.title_revision == 1
    assert not await store.apply_generated_title(
        resource_id, title="Late", revision=0, scope=SCOPE
    )
    assert not await store.apply_generated_title(
        resource_id, title="Still automatic", revision=1, scope=SCOPE
    )
    with pytest.raises(SessionTitleConflict):
        await save("Legacy old name", None, "manual", 999)
    async with target[4]() as session:
        current = (
            await session.execute(
                select(target[2]).where(target[2].c.id == resource_id)
            )
        ).one()
    assert (current.title, current.title_revision, current.updated_at) == (
        "Initial",
        1,
        11,
    )


@pytest.mark.asyncio
async def test_initial_and_legacy_title_changes_advance_a_real_revision(target):
    store, save, table, resource_id, factory = target
    changed = await save("First question", 0, "initial")
    assert changed.title_revision == 1
    stale = await save("Initial", 0, "initial")
    assert stale.title == "First question"
    assert not await store.apply_generated_title(
        resource_id, title="Old result", revision=0, scope=SCOPE
    )
    # A migrated legacy title begins manual at revision zero.
    async with factory() as session, session.begin():
        await session.execute(
            update(table)
            .where(table.c.id == resource_id)
            .values(title_source="manual", title_revision=0)
        )
    renamed = await save("Legacy edit", None, "manual")
    assert renamed.title_revision == 1


@pytest.mark.asyncio
async def test_generated_sql_retains_its_own_scope_and_lifecycle_predicates(target):
    store, _, table, resource_id, factory = target
    for scope in (
        ResourceScope(OTHER, "workspace-one"),
        ResourceScope(OWNER, "workspace-two"),
    ):
        async with store._session() as session:
            result = await session.execute(
                generated_title_update(
                    table,
                    resource_id=resource_id,
                    tenant_id="default",
                    scope=scope,
                    title="Wrong",
                    revision=0,
                )
            )
            assert result.scalar_one_or_none() is None
    if table is agent_sessions:
        async with factory() as session, session.begin():
            await session.execute(
                update(table)
                .where(table.c.id == resource_id)
                .values(lifecycle_status="deleting")
            )
        async with store._session() as session:
            result = await session.execute(
                generated_title_update(
                    table,
                    resource_id=resource_id,
                    tenant_id="default",
                    scope=SCOPE,
                    title="Wrong",
                    revision=0,
                )
            )
            assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_automatic_title_does_not_move_the_activity_timestamp(target):
    store, _, table, resource_id, factory = target
    assert await store.apply_generated_title(
        resource_id, title="Short topic", revision=0, scope=SCOPE
    )
    async with factory() as session:
        row = (
            await session.execute(select(table).where(table.c.id == resource_id))
        ).one()
    assert (row.title, row.title_revision, row.updated_at) == ("Short topic", 1, 10)


@pytest.mark.asyncio
async def test_title_commit_cannot_cross_owner_or_workspace(target):
    store, _, _, resource_id, _ = target
    for scope in (
        ResourceScope(OTHER, "workspace-one"),
        ResourceScope(OWNER, "workspace-two"),
    ):
        with pytest.raises(KeyError):
            await store.apply_generated_title(
                resource_id, title="Wrong", revision=0, scope=scope
            )


@pytest.mark.asyncio
async def test_title_revision_is_checked_after_a_concurrent_manual_transaction_commits(
    target,
):
    store, _, table, resource_id, factory = target
    # The first connection holds an uncommitted manual rename. The model's
    # UPDATE must wait for that row and then re-evaluate the old revision.
    async with factory() as writer:
        transaction = await writer.begin()
        await writer.execute(
            update(table)
            .where(table.c.id == resource_id)
            .values(
                title="My committed choice",
                title_source="manual",
                title_revision=table.c.title_revision + 1,
            )
        )
        async with store._session() as second:
            pid = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            pending = asyncio.create_task(
                second.execute(
                    generated_title_update(
                        table,
                        resource_id=resource_id,
                        tenant_id="default",
                        scope=SCOPE,
                        title="Stale model",
                        revision=0,
                    )
                )
            )
            try:
                deadline = time.monotonic() + 3
                blocked = False
                while time.monotonic() < deadline:
                    async with factory() as observer:
                        blocked = bool(
                            (
                                await observer.execute(
                                    text(
                                        "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
                                    ),
                                    {"pid": pid},
                                )
                            ).scalar_one()
                        )
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
                assert blocked, (
                    "The competing update must actually be waiting on the row lock"
                )
            finally:
                await transaction.commit()
            result = await asyncio.wait_for(pending, timeout=3)
            assert result.scalar_one_or_none() is None
    async with factory() as session:
        row = (
            await session.execute(select(table).where(table.c.id == resource_id))
        ).one()
    assert (row.title, row.title_revision) == ("My committed choice", 1)
