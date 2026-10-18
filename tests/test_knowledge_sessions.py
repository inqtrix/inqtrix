"""Behavior tests for the knowledge-session service (Wissensmodus, offline tier).

The durable tier behind the Ask view's saved sessions. Asserts the observable
contract: load-on-open (list = metadata, get = items body), per-owner scoping,
newest-first ordering, owner-only mutation, and that an update never reassigns
``created_at`` / ownership.
"""

from __future__ import annotations

import uuid

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.knowledge_sessions_memory import MemoryKnowledgeSessionStore
from inqtrix.project.knowledge_sessions_ports import (
    KnowledgeSessionGroupNotFound,
    KnowledgeSessionNotFound,
)
from inqtrix.services.knowledge_sessions_service import KnowledgeSessionsService


USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
STRANGER = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def _scoped(user_id: uuid.UUID) -> UserContext:
    return UserContext(
        principal=Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )
    )


@pytest.fixture()
def service() -> KnowledgeSessionsService:
    return KnowledgeSessionsService(store=MemoryKnowledgeSessionStore(), durable=False)


async def _save(
    service, sid, *, owner, title="S", items="[]", group_id=None,
    created_at=1.0, updated_at=1.0, workspace_id=None,
):
    return await service.save_session(
        id=sid, title=title, items_json=items, group_id=group_id,
        created_at=created_at, updated_at=updated_at, caller_user_id=owner,
        workspace_id=workspace_id,
        visible_to=_scoped(owner),
    )


async def _group(
    service, gid, *, owner, title="G", created_at=1.0, updated_at=1.0,
    workspace_id=None,
):
    return await service.save_group(
        id=gid, title=title, created_at=created_at, updated_at=updated_at,
        caller_user_id=owner, workspace_id=workspace_id, visible_to=_scoped(owner),
    )


@pytest.mark.asyncio
async def test_get_returns_items_body_list_returns_metadata_only(service) -> None:
    await _save(service, "ks_1", owner=USER, items='[{"q":"hi"}]')
    listed = await service.list_sessions(caller_user_id=USER, workspace_id=None)
    assert [s.id for s in listed] == ["ks_1"]
    assert listed[0].items_json == "[]"  # heavy body excluded from list
    assert listed[0].group_id is None
    full = await service.get_session("ks_1", visible_to=_scoped(USER))
    assert full.items_json == '[{"q":"hi"}]'


@pytest.mark.asyncio
async def test_list_scoped_to_owner(service) -> None:
    await _save(service, "ks_a", owner=ALICE)
    await _save(service, "ks_b", owner=BOB)
    alice = await service.list_sessions(caller_user_id=ALICE, workspace_id=None)
    assert [s.id for s in alice] == ["ks_a"]


@pytest.mark.asyncio
async def test_list_orders_newest_updated_first(service) -> None:
    await _save(service, "ks_old", owner=USER, created_at=1.0, updated_at=1.0)
    await _save(service, "ks_new", owner=USER, created_at=2.0, updated_at=5.0)
    listed = await service.list_sessions(caller_user_id=USER, workspace_id=None)
    assert [s.id for s in listed] == ["ks_new", "ks_old"]


@pytest.mark.asyncio
async def test_get_denies_non_owner(service) -> None:
    await _save(service, "ks_1", owner=USER)
    with pytest.raises(KnowledgeSessionNotFound):
        await service.get_session("ks_1", visible_to=_scoped(STRANGER))


@pytest.mark.asyncio
async def test_save_existing_by_non_owner_denied(service) -> None:
    await _save(service, "ks_1", owner=USER, title="orig")
    with pytest.raises(KnowledgeSessionNotFound):
        await _save(service, "ks_1", owner=STRANGER, title="hijack")
    full = await service.get_session("ks_1", visible_to=_scoped(USER))
    assert full.title == "orig"  # untouched


@pytest.mark.asyncio
async def test_run_claim_does_not_overwrite_an_existing_session(service) -> None:
    first = await service.claim_session(
        "ks_claimed",
        title="Original",
        caller_user_id=ALICE,
        workspace_id="ws-a",
        visible_to=_scoped(ALICE),
        created_at=1.0,
    )

    with pytest.raises(KnowledgeSessionNotFound):
        await service.claim_session(
            "ks_claimed",
            title="Hijack",
            caller_user_id=BOB,
            workspace_id="ws-b",
            visible_to=_scoped(BOB),
            created_at=2.0,
        )

    stored = await service.get_session("ks_claimed", visible_to=_scoped(ALICE))
    assert stored == first
    assert stored.title == "Original"
    assert stored.created_by_user_id == ALICE
    assert stored.workspace_id == "ws-a"


@pytest.mark.asyncio
async def test_update_preserves_created_at_and_owner(service) -> None:
    await _save(service, "ks_1", owner=USER, title="v1", created_at=1.0, updated_at=1.0)
    await _save(
        service, "ks_1", owner=USER, title="v2", items='[{"q":"x"}]',
        created_at=999.0, updated_at=9.0,
    )
    full = await service.get_session("ks_1", visible_to=_scoped(USER))
    assert (full.title, full.items_json) == ("v2", '[{"q":"x"}]')
    assert full.created_at == 1.0  # never reassigned on update
    assert full.updated_at == 9.0


@pytest.mark.asyncio
async def test_delete_removes_session(service) -> None:
    await _save(service, "ks_1", owner=USER)
    await service.delete_session("ks_1", visible_to=_scoped(USER))
    with pytest.raises(KnowledgeSessionNotFound):
        await service.get_session("ks_1", visible_to=_scoped(USER))


@pytest.mark.asyncio
async def test_delete_by_non_owner_denied(service) -> None:
    await _save(service, "ks_1", owner=USER)
    with pytest.raises(KnowledgeSessionNotFound):
        await service.delete_session("ks_1", visible_to=_scoped(STRANGER))
    assert (await service.get_session("ks_1", visible_to=_scoped(USER))).id == "ks_1"


@pytest.mark.asyncio
async def test_group_crud_and_session_group_id_round_trip(service) -> None:
    await _group(service, "kg_1", owner=USER, title="Folder")
    groups = await service.list_groups(caller_user_id=USER, workspace_id=None)
    assert [(g.id, g.title) for g in groups] == [("kg_1", "Folder")]

    saved = await _save(service, "ks_1", owner=USER, group_id="kg_1")
    assert saved.group_id == "kg_1"
    listed = await service.list_sessions(caller_user_id=USER, workspace_id=None)
    assert listed[0].group_id == "kg_1"
    full = await service.get_session("ks_1", visible_to=_scoped(USER))
    assert full.group_id == "kg_1"


@pytest.mark.asyncio
async def test_delete_group_orphans_sessions(service) -> None:
    await _group(service, "kg_1", owner=USER)
    await _save(service, "ks_1", owner=USER, group_id="kg_1")

    await service.delete_group("kg_1", visible_to=_scoped(USER))

    full = await service.get_session("ks_1", visible_to=_scoped(USER))
    assert full.group_id is None
    with pytest.raises(KnowledgeSessionGroupNotFound):
        await service.delete_group("kg_1", visible_to=_scoped(USER))


@pytest.mark.asyncio
async def test_session_group_must_belong_to_same_owner_and_workspace(service) -> None:
    await _group(service, "kg_1", owner=USER, workspace_id="ws_a")

    with pytest.raises(KnowledgeSessionGroupNotFound):
        await _save(
            service, "ks_foreign_owner", owner=STRANGER, group_id="kg_1",
            workspace_id="ws_a",
        )
    with pytest.raises(KnowledgeSessionGroupNotFound):
        await _save(
            service, "ks_foreign_workspace", owner=USER, group_id="kg_1",
            workspace_id="ws_b",
        )

    saved = await _save(
        service, "ks_1", owner=USER, group_id="kg_1", workspace_id="ws_a"
    )
    assert saved.group_id == "kg_1"


@pytest.mark.asyncio
async def test_group_access_mirrors_session_owner_and_workspace_rules(service) -> None:
    await _group(service, "kg_1", owner=USER, workspace_id="ws_a")
    assert [g.id for g in await service.list_groups(caller_user_id=USER, workspace_id="ws_a")] == ["kg_1"]
    assert await service.list_groups(caller_user_id=USER, workspace_id="ws_b") == []

    with pytest.raises(KnowledgeSessionGroupNotFound):
        await service.delete_group("kg_1", visible_to=_scoped(STRANGER))
    with pytest.raises(KnowledgeSessionGroupNotFound):
        await service.delete_group(
            "kg_1", visible_to=_scoped(USER), request_workspace_id="ws_b"
        )
    await service.delete_group(
        "kg_1", visible_to=_scoped(USER), request_workspace_id="ws_a"
    )
