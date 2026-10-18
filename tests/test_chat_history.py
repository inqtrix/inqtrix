"""Behavior tests for the chat-history persistence service.

Runs against the in-memory tier (the offline backend) but asserts the
observable contract both tiers must honour: owner isolation, the
idempotent autosave upsert, the keyset page (including the float-epoch
tiebreaker), message append idempotency, and the cascade/orphan rules.
The gated Postgres suite (``tests/storage/test_chat_postgres.py``)
re-runs the keyset/RLS half against a real database.
"""

from __future__ import annotations

import uuid

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import decode_cursor
from inqtrix.project.chat_memory import MemoryChatStore
from inqtrix.project.chat_ports import (
    ChatMessage,
    ThreadGroupNotFound,
    ThreadNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.services.chat_history_service import (
    ChatHistoryService,
    ChatValidationError,
)


USER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


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
def service() -> ChatHistoryService:
    return ChatHistoryService(store=MemoryChatStore(), durable=False)


async def _save_thread(
    service: ChatHistoryService,
    *,
    thread_id: str,
    caller_user_id: uuid.UUID | None,
    created_at: float,
    title: str = "T",
    group_id: str | None = None,
) -> None:
    await service.save_thread(
        id=thread_id,
        title=title,
        preview="",
        source="api",
        group_id=group_id,
        created_at=created_at,
        updated_at=created_at,
        caller_user_id=caller_user_id,
        workspace_id=None,
        visible_to=_scoped(caller_user_id) if caller_user_id else None,
    )


@pytest.mark.asyncio
async def test_thread_list_keyset_walks_without_skip_or_repeat(service) -> None:
    """The owner's threads page newest-first with no gaps or duplicates,
    INCLUDING across a created_at tie (the float-epoch tiebreaker)."""
    # Two pairs share a created_at to force the id tiebreaker.
    stamps = [10.0, 10.0, 20.0, 20.0, 30.0]
    for n, stamp in enumerate(stamps):
        await _save_thread(
            service, thread_id=f"ct_{n}", caller_user_id=USER_A, created_at=stamp
        )

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous bound
        page, next_cursor = await service.list_threads(
            caller_user_id=USER_A, workspace_id=None, limit=2, after=cursor
        )
        assert len(page) <= 2
        seen.extend(t.id for t in page)
        if next_cursor is None:
            break
        cursor = decode_cursor(next_cursor)

    assert len(seen) == len(set(seen)) == 5
    # Newest-first: highest created_at first.
    assert seen[0] == "ct_4"  # the lone 30.0
    assert set(seen[1:3]) == {"ct_2", "ct_3"}  # the 20.0 pair
    assert set(seen[3:5]) == {"ct_0", "ct_1"}  # the 10.0 pair


@pytest.mark.asyncio
async def test_thread_upsert_preserves_owner_and_created_at(service) -> None:
    """Re-saving a thread updates mutable metadata but never re-homes it
    or shifts its creation time (idempotent autosave)."""
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=100.0,
        title="first",
    )
    # The owner re-saves with a new title and a later updated_at.
    await service.save_thread(
        id="ct_1", title="second", preview="p", source="imported",
        group_id=None, created_at=999.0, updated_at=200.0,
        caller_user_id=USER_A, workspace_id=None, visible_to=_scoped(USER_A),
    )
    thread = await service.get_thread("ct_1", visible_to=_scoped(USER_A))
    assert thread.title == "second"
    assert thread.source == "imported"
    assert thread.created_at == 100.0  # NOT 999.0 — creation time is stable
    assert thread.updated_at == 200.0
    assert thread.created_by_user_id == USER_A


@pytest.mark.asyncio
async def test_scoped_owner_isolation(service) -> None:
    """A scoped caller never sees or hijacks another user's thread."""
    await _save_thread(
        service, thread_id="ct_a", caller_user_id=USER_A, created_at=1.0
    )
    # User B cannot read it, and it is absent from B's list.
    with pytest.raises(ThreadNotFound):
        await service.get_thread("ct_a", visible_to=_scoped(USER_B))
    page, _ = await service.list_threads(
        caller_user_id=USER_B, workspace_id=None, limit=50, after=None
    )
    assert page == []
    # User B cannot hijack A's thread id via a save.
    with pytest.raises(ThreadNotFound):
        await service.save_thread(
            id="ct_a", title="hijack", preview="", source="api",
            group_id=None, created_at=1.0, updated_at=2.0,
            caller_user_id=USER_B, workspace_id=None,
            visible_to=_scoped(USER_B),
        )
    # The owner still owns it unchanged.
    thread = await service.get_thread("ct_a", visible_to=_scoped(USER_A))
    assert thread.title == "T"


@pytest.mark.asyncio
async def test_unscoped_caller_sees_everything(service) -> None:
    """The anonymous/static principal (visible_to None) keeps the legacy
    full-visibility behaviour."""
    await _save_thread(
        service, thread_id="ct_x", caller_user_id=None, created_at=1.0
    )
    thread = await service.get_thread("ct_x", visible_to=None)
    assert thread.id == "ct_x"
    page, _ = await service.list_threads(
        caller_user_id=None, workspace_id=None, limit=50, after=None
    )
    assert [t.id for t in page] == ["ct_x"]


@pytest.mark.asyncio
async def test_message_append_idempotent_and_paginates(service) -> None:
    """Appending the same message id twice does not duplicate it, and the
    message page walks newest-first."""
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=1.0
    )
    payload = [
        {"id": f"cm_{n}", "role": "user", "content_markdown": f"m{n}",
         "created_at": float(n)}
        for n in range(3)
    ]
    await service.append_messages(
        "ct_1", messages=payload, visible_to=_scoped(USER_A)
    )
    # Re-append cm_0 with edited content: upsert, not duplicate.
    await service.append_messages(
        "ct_1",
        messages=[{"id": "cm_0", "role": "user", "content_markdown": "edited",
                   "created_at": 0.0}],
        visible_to=_scoped(USER_A),
    )
    page, next_cursor = await service.list_messages(
        "ct_1", limit=50, after=None, visible_to=_scoped(USER_A)
    )
    assert [m.id for m in page] == ["cm_2", "cm_1", "cm_0"]  # newest first
    assert next_cursor is None
    edited = next(m for m in page if m.id == "cm_0")
    assert edited.content_markdown == "edited"


@pytest.mark.asyncio
async def test_message_id_reuse_across_threads_does_not_overwrite(service) -> None:
    """A message id colliding with another thread's message must NOT
    overwrite it — identity is thread-scoped (the isolation invariant)."""
    await _save_thread(
        service, thread_id="ct_a", caller_user_id=USER_A, created_at=1.0
    )
    await _save_thread(
        service, thread_id="ct_b", caller_user_id=USER_B, created_at=1.0
    )
    await service.append_messages(
        "ct_a",
        messages=[{"id": "cm_dup", "role": "assistant",
                   "content_markdown": "A-secret", "created_at": 1.0}],
        visible_to=_scoped(USER_A),
    )
    # user-b appends the SAME id into their OWN thread.
    await service.append_messages(
        "ct_b",
        messages=[{"id": "cm_dup", "role": "user",
                   "content_markdown": "B-content", "created_at": 1.0}],
        visible_to=_scoped(USER_B),
    )
    a_page, _ = await service.list_messages(
        "ct_a", limit=50, after=None, visible_to=_scoped(USER_A)
    )
    a_msg = next(m for m in a_page if m.id == "cm_dup")
    assert a_msg.content_markdown == "A-secret"  # NOT clobbered
    assert a_msg.thread_id == "ct_a"
    b_page, _ = await service.list_messages(
        "ct_b", limit=50, after=None, visible_to=_scoped(USER_B)
    )
    b_msg = next(m for m in b_page if m.id == "cm_dup")
    assert b_msg.content_markdown == "B-content"
    assert b_msg.thread_id == "ct_b"


@pytest.mark.asyncio
async def test_append_to_foreign_thread_denied(service) -> None:
    """A scoped non-owner cannot append into another user's thread."""
    await _save_thread(
        service, thread_id="ct_a", caller_user_id=USER_A, created_at=1.0
    )
    with pytest.raises(ThreadNotFound):
        await service.append_messages(
            "ct_a",
            messages=[{"id": "cm_x", "role": "user",
                       "content_markdown": "x", "created_at": 1.0}],
            visible_to=_scoped(USER_B),
        )


@pytest.mark.asyncio
async def test_stale_parent_scope_cannot_write_or_delete_after_id_reuse(
    service,
) -> None:
    """A delete/recreate race cannot redirect child mutations to a new owner."""
    await _save_thread(
        service, thread_id="ct_reused", caller_user_id=USER_A, created_at=1.0
    )
    store = service.store
    stale = await store.get_thread("ct_reused")
    await store.delete_thread(
        "ct_reused", scope=ResourceScope.from_record(stale)
    )
    await store.upsert_thread(
        id="ct_reused",
        title="B",
        preview="",
        source="api",
        group_id=None,
        created_at=2.0,
        updated_at=2.0,
        created_by_user_id=USER_B,
        workspace_id=None,
    )
    message = ChatMessage(
        id="cm_reused",
        thread_id="ct_reused",
        role="user",
        content_markdown="B-owned",
        created_at=2.0,
    )

    with pytest.raises(ThreadNotFound):
        await store.append_messages(
            [message],
            expected_created_by_user_id=USER_A,
            expected_workspace_id=None,
        )

    await store.append_messages(
        [message],
        expected_created_by_user_id=USER_B,
        expected_workspace_id=None,
    )
    with pytest.raises(ThreadNotFound):
        await store.delete_message(
            "ct_reused",
            "cm_reused",
            expected_created_by_user_id=USER_A,
            expected_workspace_id=None,
        )
    messages, _ = await store.list_messages_page(
        "ct_reused", limit=50, after=None
    )
    assert messages == [message]


async def _append(
    service: ChatHistoryService,
    *,
    thread_id: str,
    caller_user_id: uuid.UUID,
    message_ids: list[str],
) -> None:
    await service.append_messages(
        thread_id,
        messages=[
            {"id": mid, "role": "user", "content_markdown": mid,
             "created_at": float(n)}
            for n, mid in enumerate(message_ids)
        ],
        visible_to=_scoped(caller_user_id),
    )


async def _message_ids(
    service: ChatHistoryService, *, thread_id: str, caller_user_id: uuid.UUID
) -> list[str]:
    page, _ = await service.list_messages(
        thread_id, limit=50, after=None, visible_to=_scoped(caller_user_id)
    )
    return [m.id for m in page]


@pytest.mark.asyncio
async def test_delete_message_removes_only_that_message_for_the_owner(service) -> None:
    """The core contract: deleting one message drops it from the thread
    and leaves its siblings — the durable counterpart to the append-only
    push, so a reload does not resurrect it."""
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=1.0
    )
    await _append(
        service, thread_id="ct_1", caller_user_id=USER_A,
        message_ids=["cm_0", "cm_1", "cm_2"],
    )
    await service.delete_message(
        "ct_1", "cm_1", visible_to=_scoped(USER_A)
    )
    assert await _message_ids(
        service, thread_id="ct_1", caller_user_id=USER_A
    ) == ["cm_2", "cm_0"]  # cm_1 gone, order otherwise intact (newest-first)


@pytest.mark.asyncio
async def test_delete_message_is_idempotent(service) -> None:
    """Deleting an already-gone (or never-present) message is a no-op, not
    an error — a coalesced-burst or multi-device re-issue must not wedge
    the autosave retry loop."""
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=1.0
    )
    await _append(
        service, thread_id="ct_1", caller_user_id=USER_A, message_ids=["cm_0"]
    )
    await service.delete_message("ct_1", "cm_0", visible_to=_scoped(USER_A))
    # Second delete of the same id, and a delete of an unknown id: both quiet.
    await service.delete_message("ct_1", "cm_0", visible_to=_scoped(USER_A))
    await service.delete_message(
        "ct_1", "cm_never", visible_to=_scoped(USER_A)
    )
    assert await _message_ids(
        service, thread_id="ct_1", caller_user_id=USER_A
    ) == []


@pytest.mark.asyncio
async def test_delete_message_denied_for_a_foreign_caller(service) -> None:
    """A scoped non-owner cannot delete another user's message, and the
    denial is the indistinct ThreadNotFound (existence undisclosed); the
    message survives."""
    await _save_thread(
        service, thread_id="ct_a", caller_user_id=USER_A, created_at=1.0
    )
    await _append(
        service, thread_id="ct_a", caller_user_id=USER_A, message_ids=["cm_0"]
    )
    with pytest.raises(ThreadNotFound):
        await service.delete_message(
            "ct_a", "cm_0", visible_to=_scoped(USER_B)
        )
    assert await _message_ids(
        service, thread_id="ct_a", caller_user_id=USER_A
    ) == ["cm_0"]


@pytest.mark.asyncio
async def test_delete_message_blocked_across_a_different_workspace(service) -> None:
    """Defense-in-depth (mirrors the thread delete): a delete carrying a
    different project's workspace namespace is denied and leaves the row,
    never a silent cross-project drop."""
    await service.save_thread(
        id="ct_1", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=1.0, caller_user_id=USER_A,
        workspace_id="ws_a", visible_to=_scoped(USER_A),
    )
    await service.append_messages(
        "ct_1",
        messages=[{"id": "cm_0", "role": "user", "content_markdown": "x",
                   "created_at": 0.0}],
        visible_to=_scoped(USER_A),
    )
    with pytest.raises(ThreadNotFound):
        await service.delete_message(
            "ct_1", "cm_0", visible_to=_scoped(USER_A),
            request_workspace_id="ws_b",
        )
    assert await _message_ids(
        service, thread_id="ct_1", caller_user_id=USER_A
    ) == ["cm_0"]


@pytest.mark.asyncio
async def test_delete_thread_cascades_and_is_owner_only(service) -> None:
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=1.0
    )
    await service.append_messages(
        "ct_1",
        messages=[{"id": "cm_1", "role": "user", "content_markdown": "x",
                   "created_at": 1.0}],
        visible_to=_scoped(USER_A),
    )
    # A foreign user cannot delete it.
    with pytest.raises(ThreadNotFound):
        await service.delete_thread("ct_1", visible_to=_scoped(USER_B))
    # The owner can; the messages go with it.
    await service.delete_thread("ct_1", visible_to=_scoped(USER_A))
    with pytest.raises(ThreadNotFound):
        await service.get_thread("ct_1", visible_to=_scoped(USER_A))


@pytest.mark.asyncio
async def test_delete_thread_is_blocked_across_a_different_workspace(service) -> None:
    """Defense-in-depth: a delete issued from another project's UI namespace
    must not reach this project's rows, even for the same owner. The thread is
    owned by user-a in workspace ws_a; a delete carrying ws_b (the namespace of
    a different synced project) is denied as not-found and leaves the row."""
    await service.save_thread(
        id="ct_1", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=1.0, caller_user_id=USER_A,
        workspace_id="ws_a", visible_to=_scoped(USER_A),
    )
    with pytest.raises(ThreadNotFound):
        await service.delete_thread(
            "ct_1", visible_to=_scoped(USER_A), request_workspace_id="ws_b"
        )
    # Still present: the cross-workspace delete was a no-op, not a silent drop.
    assert (await service.get_thread("ct_1", visible_to=_scoped(USER_A))).id == "ct_1"
    # The owner in the SAME workspace deletes normally.
    await service.delete_thread(
        "ct_1", visible_to=_scoped(USER_A), request_workspace_id="ws_a"
    )
    with pytest.raises(ThreadNotFound):
        await service.get_thread("ct_1", visible_to=_scoped(USER_A))


@pytest.mark.asyncio
async def test_group_delete_orphans_threads(service) -> None:
    """Deleting a group ungroups its threads (never deletes them)."""
    await service.save_group(
        id="ctg_1", title="G", created_at=1.0, updated_at=1.0,
        caller_user_id=USER_A, workspace_id=None, visible_to=_scoped(USER_A),
    )
    await _save_thread(
        service, thread_id="ct_1", caller_user_id=USER_A, created_at=2.0,
        group_id="ctg_1",
    )
    await service.delete_group("ctg_1", visible_to=_scoped(USER_A))
    thread = await service.get_thread("ct_1", visible_to=_scoped(USER_A))
    assert thread.group_id is None
    with pytest.raises(ThreadGroupNotFound):
        await service.delete_group("ctg_1", visible_to=_scoped(USER_A))


@pytest.mark.asyncio
async def test_invalid_role_and_source_rejected(service) -> None:
    """Out-of-domain role/source fail at the service (clean 400), before
    the database CHECK constraint."""
    with pytest.raises(ChatValidationError):
        await service.save_thread(
            id="ct_1", title="T", preview="", source="bogus",
            group_id=None, created_at=1.0, updated_at=1.0,
            caller_user_id=USER_A, workspace_id=None,
            visible_to=_scoped(USER_A),
        )
    await _save_thread(
        service, thread_id="ct_2", caller_user_id=USER_A, created_at=1.0
    )
    with pytest.raises(ChatValidationError):
        await service.append_messages(
            "ct_2",
            messages=[{"id": "cm_1", "role": "system",
                       "content_markdown": "x", "created_at": 1.0}],
            visible_to=_scoped(USER_A),
        )


@pytest.mark.asyncio
async def test_thread_model_selection_round_trips_and_survives_second_save(service) -> None:
    """The picked model sticks to the thread across saves.

    The overwrite on the SECOND save is the load-bearing half: a column
    missing from the upsert's mutable set is written once and then silently
    frozen, and only a second save exposes that.
    """
    user = uuid.uuid4()
    selection = '{"model":"gpt-5.4-nano","tier":null,"effort":null}'
    await service.save_thread(
        id="ct_pick", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=1.0, caller_user_id=user,
        workspace_id=None, visible_to=_scoped(user),
        model_selection=selection,
    )
    stored = await service.get_thread("ct_pick", visible_to=_scoped(user))
    assert stored.model_selection == selection

    changed = '{"model":null,"tier":"high","effort":null}'
    await service.save_thread(
        id="ct_pick", title="T", preview="", source="api", group_id=None,
        created_at=1.0, updated_at=2.0, caller_user_id=user,
        workspace_id=None, visible_to=_scoped(user),
        model_selection=changed,
    )
    stored = await service.get_thread("ct_pick", visible_to=_scoped(user))
    assert stored.model_selection == changed


@pytest.mark.asyncio
async def test_thread_without_selection_stays_empty(service) -> None:
    """'' is the no-pick value old rows and old clients resolve to."""
    user = uuid.uuid4()
    await _save_thread(
        service, thread_id="ct_plain", caller_user_id=user, created_at=1.0,
    )
    stored = await service.get_thread("ct_plain", visible_to=_scoped(user))
    assert stored.model_selection == ""


@pytest.mark.asyncio
async def test_thread_model_selection_is_length_capped(service) -> None:
    user = uuid.uuid4()
    with pytest.raises(ChatValidationError):
        await service.save_thread(
            id="ct_blob", title="T", preview="", source="api", group_id=None,
            created_at=1.0, updated_at=1.0, caller_user_id=user,
            workspace_id=None, visible_to=_scoped(user),
            model_selection="x" * 2001,
        )
