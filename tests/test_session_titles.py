"""Title revisions protect manual choices independently of activity saves."""

from dataclasses import asdict
from uuid import UUID

import pytest

from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.project.chat_memory import MemoryChatStore
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.session_titles import (
    SessionTitleConflict,
    TitleState,
    title_after_save,
)

OWNER = UUID(int=101)
SCOPE = ResourceScope(OWNER, "workspace")


@pytest.fixture(params=["chat", "agent"])
def title_store(request):
    store = MemoryChatStore() if request.param == "chat" else MemoryAgentSessionStore()

    async def save(
        title="Initial question", revision=0, source="initial", updated_at=10.0, body=""
    ):
        kwargs = dict(
            id="subject",
            title=title,
            created_at=10.0,
            updated_at=updated_at,
            created_by_user_id=OWNER,
            workspace_id="workspace",
            group_id=None,
            title_revision=revision,
            title_source=source,
        )
        if request.param == "chat":
            return await store.upsert_thread(**kwargs, preview=body, source="api")
        return await store.upsert_session(**kwargs, items_json=body)

    async def get():
        return await (
            store.get_thread("subject")
            if request.param == "chat"
            else store.get_session("subject")
        )

    return store, save, get


@pytest.mark.asyncio
async def test_generated_title_replaces_initial_without_changing_activity(title_store):
    store, save, get = title_store
    await save()
    assert await store.apply_generated_title(
        "subject", title="Short topic", revision=0, scope=SCOPE
    )
    row = await get()
    assert (row.title, row.title_revision, row.title_source, row.updated_at) == (
        "Short topic",
        1,
        "generated",
        10.0,
    )
    assert not await store.apply_generated_title(
        "subject", title="Late duplicate", revision=0, scope=SCOPE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_title", ["My project", "Initial question"])
async def test_manual_choice_blocks_late_generation_even_when_text_is_unchanged(
    title_store, manual_title
):
    store, save, get = title_store
    await save()
    await save(manual_title, 0, "manual", updated_at=11)
    assert not await store.apply_generated_title(
        "subject", title="Model topic", revision=0, scope=SCOPE
    )
    row = await get()
    assert (row.title, row.title_source, row.title_revision) == (
        manual_title,
        "manual",
        1,
    )
    assert not await store.apply_generated_title(
        "subject", title="Still automatic", revision=1, scope=SCOPE
    )


@pytest.mark.asyncio
async def test_manual_choice_wins_when_model_committed_while_user_was_typing(
    title_store,
):
    store, save, get = title_store
    await save()
    await store.apply_generated_title(
        "subject", title="Model topic", revision=0, scope=SCOPE
    )
    await save("My project", 0, "manual", updated_at=11)
    row = await get()
    assert (row.title, row.title_revision, row.title_source) == (
        "My project",
        2,
        "manual",
    )


@pytest.mark.asyncio
async def test_stale_activity_save_keeps_manual_title_but_saves_other_metadata(
    title_store,
):
    _, save, _ = title_store
    await save()
    await save("My project", 0, "manual", updated_at=11)
    row = await save(
        "Initial question", 0, "initial", updated_at=99, body="new content"
    )
    assert (row.title, row.title_revision, row.title_source, row.updated_at) == (
        "My project",
        1,
        "manual",
        99,
    )
    assert getattr(row, "preview", getattr(row, "items_json", None)) == "new content"


@pytest.mark.asyncio
async def test_stale_generated_echo_cannot_revert_a_manual_rename(title_store):
    store, save, _ = title_store
    await save()
    await store.apply_generated_title(
        "subject", title="Model topic", revision=0, scope=SCOPE
    )
    await save("My project", 1, "manual")
    row = await save("Model topic", 1, "generated", updated_at=99)
    assert (row.title, row.title_revision, row.title_source) == (
        "My project",
        2,
        "manual",
    )


@pytest.mark.asyncio
async def test_initial_title_revision_changes_invalidate_older_generation(title_store):
    store, save, _ = title_store
    await save()
    await save("First real question", 0, "initial")
    assert not await store.apply_generated_title(
        "subject", title="Old topic", revision=0, scope=SCOPE
    )
    assert await store.apply_generated_title(
        "subject", title="New topic", revision=1, scope=SCOPE
    )


@pytest.mark.asyncio
async def test_generation_does_not_create_missing_or_foreign_sessions(title_store):
    store, save, _ = title_store
    with pytest.raises(KeyError):
        await store.apply_generated_title(
            "subject", title="Unexpected", revision=0, scope=SCOPE
        )
    await save()
    with pytest.raises(KeyError):
        await store.apply_generated_title(
            "subject",
            title="Unexpected",
            revision=0,
            scope=ResourceScope(UUID(int=202), "workspace"),
        )
    with pytest.raises(KeyError):
        await store.apply_generated_title(
            "subject",
            title="Unexpected",
            revision=0,
            scope=ResourceScope(OWNER, "other"),
        )


def test_legacy_saves_cannot_replace_a_versioned_title():
    assert title_after_save(
        TitleState("Old"), title="Legacy rename", revision=None, source="manual"
    ) == TitleState("Legacy rename", 1, "manual")
    current = TitleState("My project", 2, "manual")
    with pytest.raises(SessionTitleConflict):
        title_after_save(current, title="Old", revision=None, source="manual")


def test_stale_manual_save_returns_the_current_title_for_conflict_reconciliation():
    current = TitleState("Other manual choice", 3, "manual")
    assert (
        title_after_save(current, title="My older edit", revision=1, source="manual")
        == current
    )
    assert asdict(
        title_after_save(current, title="My newer edit", revision=3, source="manual")
    ) == {
        "title": "My newer edit",
        "title_revision": 4,
        "title_source": "manual",
    }


def test_stale_initial_metadata_cannot_restore_an_older_provisional_title():
    current = TitleState("New first question", 2, "initial")
    assert (
        title_after_save(
            current, title="Old first question", revision=0, source="initial"
        )
        == current
    )


def test_metadata_writers_cannot_manufacture_a_generated_title():
    current = TitleState("Initial", 0, "initial")
    assert (
        title_after_save(current, title="Pretend model", revision=0, source="generated")
        == current
    )
