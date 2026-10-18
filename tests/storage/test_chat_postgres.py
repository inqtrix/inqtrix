"""Postgres integration tests for the chat-history store (gated).

Same gating/conventions as the other storage suites: a disposable
database via ``INQTRIX_TEST_DATABASE_URL``, operations under the
restricted app role, RLS as the second defense layer. Verifies the
half of the :class:`~inqtrix.project.chat_ports.ChatStore` contract that
only a real database exercises: the SQL keyset page (tuple comparison +
the float-epoch id tiebreaker), the ``ON CONFLICT`` autosave upsert,
owner/workspace scoping in the query, FK cascade on thread delete, and
the ``ON DELETE SET NULL`` group orphan. The owner/share ACCESS rules
live in the service and are covered offline in ``test_chat_history.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.pagination import decode_cursor
from inqtrix.project.chat_postgres import PostgresChatStore
from inqtrix.project.chat_ports import ChatMessage, ThreadNotFound
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.storage.chat_orm import (
    chat_messages,
    chat_thread_groups,
    chat_threads,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_ID = canonical_user_id("chat-user")
USER_1_ID = canonical_user_id("chat-user-1")
USER_2_ID = canonical_user_id("chat-user-2")
USER_A_ID = canonical_user_id("chat-user-a")
USER_B_ID = canonical_user_id("chat-user-b")
OTHER_USER_ID = canonical_user_id("chat-other-user")


@pytest.fixture(scope="session", autouse=True)
def chat_schema_migrated():
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
            # Child -> parent order (FK), though CASCADE would handle it.
            await session.execute(chat_messages.delete())
            await session.execute(chat_threads.delete())
            await session.execute(chat_thread_groups.delete())
            await ensure_canonical_users(
                session,
                (
                    USER_ID,
                    USER_1_ID,
                    USER_2_ID,
                    USER_A_ID,
                    USER_B_ID,
                    OTHER_USER_ID,
                ),
            )
    chat_store = PostgresChatStore(engine=engine, app_role=APP_ROLE)
    yield chat_store
    await chat_store.aclose()


async def _save(
    store,
    thread_id,
    *,
    owner: uuid.UUID = USER_ID,
    workspace=None,
    created_at=1.0,
    group_id=None,
):
    return await store.upsert_thread(
        id=thread_id, title="T", preview="", source="api",
        group_id=group_id, created_at=created_at, updated_at=created_at,
        created_by_user_id=owner, workspace_id=workspace,
    )


async def _append_messages(
    store: PostgresChatStore,
    messages: list[ChatMessage],
    *,
    owner: uuid.UUID,
    workspace: str | None = None,
) -> list[ChatMessage]:
    return await store.append_messages(
        messages,
        expected_created_by_user_id=owner,
        expected_workspace_id=workspace,
    )


async def _delete_message(
    store: PostgresChatStore,
    thread_id: str,
    message_id: str,
    *,
    owner: uuid.UUID,
    workspace: str | None = None,
) -> None:
    await store.delete_message(
        thread_id,
        message_id,
        expected_created_by_user_id=owner,
        expected_workspace_id=workspace,
    )


@pytest.mark.asyncio
async def test_thread_keyset_walks_with_tiebreaker(store) -> None:
    """The DB keyset (tuple_(created_at, id) < cursor) pages the owner's
    threads with no gaps or duplicates, including across a created_at tie."""
    stamps = [10.0, 10.0, 20.0, 30.0, 30.0]
    for n, stamp in enumerate(stamps):
        await _save(store, f"ct_{n}", owner=USER_ID, created_at=stamp)

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page, next_cursor = await store.list_threads_page(
            created_by_user_id=USER_ID,
            workspace_id=None,
            limit=2,
            after=cursor,
        )
        assert len(page) <= 2
        seen.extend(t.id for t in page)
        if next_cursor is None:
            break
        cursor = decode_cursor(next_cursor)

    assert len(seen) == len(set(seen)) == 5
    assert {seen[0], seen[1]} == {"ct_3", "ct_4"}  # the 30.0 pair, first


@pytest.mark.asyncio
async def test_thread_list_scopes_to_owner_and_workspace(store) -> None:
    await _save(store, "ct_1", owner=USER_1_ID, workspace="w1", created_at=1.0)
    await _save(store, "ct_2", owner=USER_2_ID, workspace="w1", created_at=2.0)
    await _save(store, "ct_3", owner=USER_1_ID, workspace="w2", created_at=3.0)

    page, _ = await store.list_threads_page(
        created_by_user_id=USER_1_ID,
        workspace_id="w1",
        limit=50,
        after=None,
    )
    assert [t.id for t in page] == ["ct_1"]


@pytest.mark.asyncio
async def test_upsert_preserves_created_at_and_owner(store) -> None:
    await _save(store, "ct_1", owner=USER_1_ID, created_at=100.0)
    # Conflicting re-save with a different created_at + owner must not win.
    await store.upsert_thread(
        id="ct_1", title="second", preview="p", source="imported",
        group_id=None, created_at=999.0, updated_at=200.0,
        created_by_user_id=USER_1_ID, workspace_id=None,
    )
    thread = await store.get_thread("ct_1")
    assert thread.title == "second"
    assert thread.source == "imported"
    assert thread.created_at == 100.0
    assert thread.updated_at == 200.0
    assert thread.created_by_user_id == USER_1_ID


@pytest.mark.asyncio
async def test_cross_owner_thread_id_collision_is_not_found(store) -> None:
    await _save(store, "ct_1", owner=USER_1_ID, created_at=100.0)

    with pytest.raises(ThreadNotFound):
        await store.upsert_thread(
            id="ct_1", title="hijack", preview="", source="api",
            group_id=None, created_at=999.0, updated_at=200.0,
            created_by_user_id=OTHER_USER_ID, workspace_id=None,
        )

    assert (await store.get_thread("ct_1")).title == "T"


@pytest.mark.asyncio
async def test_message_append_idempotent_keyset_and_cascade(store) -> None:
    await _save(store, "ct_1", owner=USER_ID, created_at=1.0)
    messages = [
        ChatMessage(id=f"cm_{n}", thread_id="ct_1", role="user",
                    content_markdown=f"m{n}", created_at=float(n))
        for n in range(5)
    ]
    await _append_messages(store, messages, owner=USER_ID)
    # Re-append cm_0 with edited content: upsert, not a duplicate.
    await _append_messages(
        store,
        [ChatMessage(id="cm_0", thread_id="ct_1", role="user",
                     content_markdown="edited", created_at=0.0)],
        owner=USER_ID,
    )

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page, next_cursor = await store.list_messages_page(
            "ct_1", limit=2, after=cursor
        )
        seen.extend(m.id for m in page)
        if next_cursor is None:
            break
        cursor = decode_cursor(next_cursor)
    assert len(seen) == len(set(seen)) == 5
    assert seen[0] == "cm_4"  # newest first

    edited_page, _ = await store.list_messages_page("ct_1", limit=50, after=None)
    edited = next(m for m in edited_page if m.id == "cm_0")
    assert edited.content_markdown == "edited"

    # Deleting the thread cascades its messages.
    await store.delete_thread(
        "ct_1",
        scope=ResourceScope.from_record(await store.get_thread("ct_1")),
    )
    gone, _ = await store.list_messages_page("ct_1", limit=50, after=None)
    assert gone == []


@pytest.mark.asyncio
async def test_message_id_reuse_across_threads_is_isolated(store) -> None:
    """The composite (thread_id, id) PK keeps a re-used message id in one
    thread from overwriting the same id in another thread."""
    await _save(store, "ct_a", owner=USER_A_ID, created_at=1.0)
    await _save(store, "ct_b", owner=USER_B_ID, created_at=1.0)
    await _append_messages(store, [
        ChatMessage(id="cm_dup", thread_id="ct_a", role="assistant",
                    content_markdown="A-secret", created_at=1.0)
    ], owner=USER_A_ID)
    await _append_messages(store, [
        ChatMessage(id="cm_dup", thread_id="ct_b", role="user",
                    content_markdown="B-content", created_at=1.0)
    ], owner=USER_B_ID)
    a_page, _ = await store.list_messages_page("ct_a", limit=50, after=None)
    assert next(m for m in a_page if m.id == "cm_dup").content_markdown == (
        "A-secret"
    )
    b_page, _ = await store.list_messages_page("ct_b", limit=50, after=None)
    assert next(m for m in b_page if m.id == "cm_dup").content_markdown == (
        "B-content"
    )


@pytest.mark.asyncio
async def test_delete_message_is_composite_scoped_and_idempotent(store) -> None:
    """The DELETE is scoped on the composite (thread_id, id): it removes
    only the targeted row, leaves the same id living in another thread,
    and a repeat delete of a gone row is a quiet no-op (the autosave
    re-issue must not error)."""
    await _save(store, "ct_a", owner=USER_A_ID, created_at=1.0)
    await _save(store, "ct_b", owner=USER_B_ID, created_at=1.0)
    await _append_messages(store, [
        ChatMessage(id="cm_dup", thread_id="ct_a", role="assistant",
                    content_markdown="A", created_at=1.0),
        ChatMessage(id="cm_keep", thread_id="ct_a", role="user",
                    content_markdown="keep", created_at=2.0),
    ], owner=USER_A_ID)
    await _append_messages(store, [
        ChatMessage(id="cm_dup", thread_id="ct_b", role="user",
                    content_markdown="B", created_at=1.0)
    ], owner=USER_B_ID)

    await _delete_message(store, "ct_a", "cm_dup", owner=USER_A_ID)
    # Gone only from ct_a; a sibling and the foreign-thread namesake remain.
    a_page, _ = await store.list_messages_page("ct_a", limit=50, after=None)
    assert [m.id for m in a_page] == ["cm_keep"]
    b_page, _ = await store.list_messages_page("ct_b", limit=50, after=None)
    assert [m.id for m in b_page] == ["cm_dup"]

    # Idempotent: re-deleting the gone row and an unknown id both no-op.
    await _delete_message(store, "ct_a", "cm_dup", owner=USER_A_ID)
    await _delete_message(store, "ct_a", "cm_never", owner=USER_A_ID)
    a_page, _ = await store.list_messages_page("ct_a", limit=50, after=None)
    assert [m.id for m in a_page] == ["cm_keep"]


@pytest.mark.asyncio
async def test_stale_parent_scope_cannot_write_or_delete_after_id_reuse(
    store,
) -> None:
    await _save(store, "ct_reused", owner=USER_A_ID, created_at=1.0)
    await store.delete_thread(
        "ct_reused",
        scope=ResourceScope.from_record(await store.get_thread("ct_reused")),
    )
    await _save(store, "ct_reused", owner=USER_B_ID, created_at=2.0)
    message = ChatMessage(
        id="cm_reused",
        thread_id="ct_reused",
        role="user",
        content_markdown="B-owned",
        created_at=2.0,
    )

    with pytest.raises(ThreadNotFound):
        await _append_messages(store, [message], owner=USER_A_ID)

    await _append_messages(store, [message], owner=USER_B_ID)
    with pytest.raises(ThreadNotFound):
        await _delete_message(
            store, "ct_reused", "cm_reused", owner=USER_A_ID
        )
    messages, _ = await store.list_messages_page(
        "ct_reused", limit=50, after=None
    )
    assert messages == [message]


@pytest.mark.asyncio
async def test_group_delete_orphans_thread(store) -> None:
    """The FK ``ON DELETE SET NULL`` orphans a group's threads instead of
    deleting them."""
    group = await store.upsert_group(
        id="ctg_1", title="G", created_at=1.0, updated_at=1.0,
        created_by_user_id=USER_ID, workspace_id=None,
    )
    await _save(
        store,
        "ct_1",
        owner=USER_ID,
        created_at=2.0,
        group_id="ctg_1",
    )
    await store.delete_group(
        "ctg_1",
        scope=ResourceScope.from_record(group),
    )
    thread = await store.get_thread("ct_1")
    assert thread.group_id is None


@pytest.mark.asyncio
async def test_model_selection_survives_the_second_upsert(store) -> None:
    """The tier must survive a SECOND save, not just the first INSERT.

    A column missing from the ON CONFLICT update set is written once and then
    silently frozen — the user changes their pick, the request succeeds, and
    nothing changes. Only a second save exposes that.
    """
    await store.upsert_thread(
        id="ct_pg_pick", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=1.0, created_by_user_id=USER_ID,
        workspace_id=None,
        model_selection='{"model":"gpt-5.4-nano","tier":null,"effort":null}',
    )
    second = await store.upsert_thread(
        id="ct_pg_pick", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=2.0, created_by_user_id=USER_ID,
        workspace_id=None,
        model_selection='{"model":null,"tier":"high","effort":null}',
    )
    assert second.model_selection == '{"model":null,"tier":"high","effort":null}'
    fetched = await store.get_thread("ct_pg_pick")
    assert fetched.model_selection == '{"model":null,"tier":"high","effort":null}'


@pytest.mark.asyncio
async def test_model_selection_defaults_empty_for_legacy_writers(store) -> None:
    """A writer that predates the column leaves '' behind, never NULL."""
    thread = await _save(store, "ct_pg_legacy")
    assert thread.model_selection == ""
