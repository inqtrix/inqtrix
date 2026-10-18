"""Cross-owner collision contract shared by memory and Postgres stores."""

from __future__ import annotations

import uuid

import pytest

from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.project.agent_sessions_ports import AgentSessionGroupNotFound
from inqtrix.project.asset_records_memory import MemoryAssetStore
from inqtrix.project.asset_records_ports import SectionNotFound
from inqtrix.project.chat_memory import MemoryChatStore
from inqtrix.project.chat_ports import ThreadGroupNotFound, ThreadNotFound
from inqtrix.project.editor_memory import MemoryEditorStore
from inqtrix.project.editor_ports import DocumentNotFound, FolderNotFound
from inqtrix.project.knowledge_sessions_memory import MemoryKnowledgeSessionStore
from inqtrix.project.knowledge_sessions_ports import (
    KnowledgeSessionGroupNotFound,
    KnowledgeSessionNotFound,
)
from inqtrix.project.vector_index_memory import MemoryVectorIndexStore
from inqtrix.project.vector_index_ports import VectorIndexNotFound
from inqtrix.project.scoped_upsert import ResourceScope


ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.mark.asyncio
async def test_asset_collisions_and_parent_scope_are_indistinct_not_found() -> None:
    store = MemoryAssetStore()
    await store.upsert_section(
        id="section", kind="custom", title="Alice", created_at=1.0,
        updated_at=1.0, created_by_user_id=ALICE, workspace_id="ws-a",
    )

    with pytest.raises(SectionNotFound):
        await store.upsert_section(
            id="section", kind="custom", title="Bob", created_at=2.0,
            updated_at=2.0, created_by_user_id=BOB, workspace_id="ws-b",
        )
    with pytest.raises(SectionNotFound):
        await store.upsert_group(
            id="group", section_id="section", title="Foreign parent",
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )

    sections = await store.list_sections(
        created_by_user_id=ALICE, workspace_id="ws-a"
    )
    assert [(section.id, section.title) for section in sections] == [
        ("section", "Alice")
    ]


@pytest.mark.asyncio
async def test_knowledge_collision_and_group_scope_are_indistinct_not_found() -> None:
    store = MemoryKnowledgeSessionStore()
    await store.upsert_group(
        id="group", title="Alice", created_at=1.0, updated_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    await store.upsert_session(
        id="session", title="Alice", items_json="[]", group_id="group",
        created_at=1.0, updated_at=1.0, created_by_user_id=ALICE,
        workspace_id="ws-a",
    )

    with pytest.raises(KnowledgeSessionGroupNotFound):
        await store.upsert_session(
            id="other", title="Bob", items_json="[]", group_id="group",
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )
    with pytest.raises(KnowledgeSessionNotFound):
        await store.upsert_session(
            id="session", title="Bob", items_json="[]", group_id=None,
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )
    assert (await store.get_session("session")).title == "Alice"


@pytest.mark.asyncio
async def test_chat_collision_and_group_scope_are_indistinct_not_found() -> None:
    store = MemoryChatStore()
    await store.upsert_group(
        id="group", title="Alice", created_at=1.0, updated_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    await store.upsert_thread(
        id="thread", title="Alice", preview="", source="api", group_id="group",
        created_at=1.0, updated_at=1.0, created_by_user_id=ALICE,
        workspace_id="ws-a",
    )

    with pytest.raises(ThreadGroupNotFound):
        await store.upsert_thread(
            id="other", title="Bob", preview="", source="api", group_id="group",
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )
    with pytest.raises(ThreadNotFound):
        await store.upsert_thread(
            id="thread", title="Bob", preview="", source="api", group_id=None,
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )
    assert (await store.get_thread("thread")).title == "Alice"


@pytest.mark.asyncio
async def test_editor_collision_and_folder_scope_are_indistinct_not_found() -> None:
    store = MemoryEditorStore()
    await store.upsert_folder(
        id="folder", title="Alice", created_at=1.0, updated_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    await store.upsert_document(
        id="document", title="Alice", content_markdown="A", folder_id="folder",
        source="api", source_run_id=None, revision=1,
        diff_anchor_markdown=None, diff_anchor_updated_at=None, created_at=1.0,
        updated_at=1.0, created_by_user_id=ALICE, workspace_id="ws-a",
    )

    with pytest.raises(FolderNotFound):
        await store.upsert_document(
            id="other", title="Bob", content_markdown="B", folder_id="folder",
            source="api", source_run_id=None, revision=1,
            diff_anchor_markdown=None, diff_anchor_updated_at=None, created_at=2.0,
            updated_at=2.0, created_by_user_id=BOB, workspace_id="ws-b",
        )
    with pytest.raises(DocumentNotFound):
        await store.upsert_document(
            id="document", title="Bob", content_markdown="B", folder_id=None,
            source="api", source_run_id=None, revision=2,
            diff_anchor_markdown=None, diff_anchor_updated_at=None, created_at=2.0,
            updated_at=2.0, created_by_user_id=BOB, workspace_id="ws-b",
        )
    assert (await store.get_document("document")).title == "Alice"


@pytest.mark.asyncio
async def test_vector_and_agent_session_writes_keep_the_original_scope() -> None:
    vectors = MemoryVectorIndexStore()
    values = dict(
        id="index", title="Alice", handle="alice", model="embedding", dims=3,
        status="ready", server_collection_id=None, server_collection_model=None,
        last_error=None, members=(), history=(), created_at=1.0, updated_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    await vectors.upsert_index(**values)
    with pytest.raises(VectorIndexNotFound):
        await vectors.upsert_index(
            **{
                **values,
                "title": "Bob",
                "created_by_user_id": BOB,
                "workspace_id": "ws-b",
            }
        )
    assert (await vectors.get_index("index")).title == "Alice"

    agents = MemoryAgentSessionStore()
    await agents.upsert_group(
        id="group", title="Alice", created_at=1.0, updated_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    with pytest.raises(AgentSessionGroupNotFound):
        await agents.upsert_session(
            id="session", title="Bob", items_json="[]", group_id="group",
            created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
            workspace_id="ws-b",
        )


@pytest.mark.asyncio
async def test_stale_scoped_delete_cannot_remove_a_recreated_foreign_row() -> None:
    store = MemoryChatStore()
    alice = await store.upsert_thread(
        id="thread", title="Alice", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=1.0, created_by_user_id=ALICE,
        workspace_id="ws-a",
    )
    stale_scope = ResourceScope.from_record(alice)
    await store.delete_thread("thread", scope=stale_scope)
    await store.upsert_thread(
        id="thread", title="Bob", preview="", source="api", group_id=None,
        created_at=2.0, updated_at=2.0, created_by_user_id=BOB,
        workspace_id="ws-b",
    )

    with pytest.raises(ThreadNotFound):
        await store.delete_thread("thread", scope=stale_scope)

    assert (await store.get_thread("thread")).title == "Bob"


@pytest.mark.asyncio
async def test_metadata_cas_hides_revision_after_delete_and_foreign_recreate() -> None:
    store = MemoryEditorStore()
    alice = await store.upsert_document(
        id="document", title="Alice", content_markdown="A", folder_id=None,
        source="api", source_run_id=None, revision=1,
        diff_anchor_markdown=None, diff_anchor_updated_at=None, created_at=1.0,
        updated_at=1.0, created_by_user_id=ALICE, workspace_id="ws-a",
    )
    stale_scope = ResourceScope.from_record(alice)
    await store.delete_document("document", scope=stale_scope)
    await store.upsert_document(
        id="document", title="Bob", content_markdown="B", folder_id=None,
        source="api", source_run_id=None, revision=1,
        diff_anchor_markdown=None, diff_anchor_updated_at=None, created_at=2.0,
        updated_at=2.0, created_by_user_id=BOB, workspace_id="ws-b",
    )

    with pytest.raises(DocumentNotFound):
        await store.patch_document_metadata(
            document_id="document",
            expected_metadata_revision=1,
            title="stale write",
            folder_id=None,
            set_folder_id=False,
            diff_anchor_markdown=None,
            set_diff_anchor_markdown=False,
            diff_anchor_updated_at=None,
            set_diff_anchor_updated_at=False,
            updated_at=3.0,
            scope=stale_scope,
        )

    assert (await store.get_document("document")).title == "Bob"
