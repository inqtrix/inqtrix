"""Behavior tests for the vector-index-record service (offline tier)."""

from __future__ import annotations

import uuid

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import decode_cursor
from inqtrix.project.vector_index_memory import MemoryVectorIndexStore
from inqtrix.project.vector_index_ports import (
    VectorIndexHistoryEntry,
    VectorIndexMember,
    VectorIndexMemberUnavailable,
    VectorIndexNotFound,
)
from inqtrix.source_authority import (
    MemorySourceLifecycleAuthority,
    SourceScope,
)
from inqtrix.services.vector_index_service import (
    VectorIndexService,
    VectorIndexValidationError,
)


USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SOMEONE_ELSE = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


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
def service() -> VectorIndexService:
    return VectorIndexService(store=MemoryVectorIndexStore(), durable=False)


async def _save(service, iid, *, owner, created_at=1.0, members=(), history=(),
                status="ready"):
    return await service.save_index(
        id=iid, title="Idx", handle="idx", model="text-embedding-3-large",
        dims=3072, status=status, server_collection_id=None,
        server_collection_model=None, last_error=None,
        members=members, history=history, created_at=created_at, updated_at=created_at,
        caller_user_id=owner, workspace_id=None, visible_to=_scoped(owner),
    )


@pytest.mark.asyncio
async def test_index_roundtrip_carries_members_and_history(service) -> None:
    await _save(
        service, "vix_1", owner=USER,
        members=(VectorIndexMember("fa_2", "embedded"), VectorIndexMember("fa_1", "pending")),
        history=(VectorIndexHistoryEntry("ok", 2, 1500, None, 1.0, 2.5),),
    )
    page, _ = await service.list_indexes(caller_user_id=USER, workspace_id=None, limit=50, after=None)
    assert len(page) == 1
    record = page[0]
    # Member array order is user-visible, so it must round-trip verbatim.
    assert [(m.file_id, m.state) for m in record.members] == [("fa_2", "embedded"), ("fa_1", "pending")]
    assert len(record.history) == 1
    assert record.history[0].duration_ms == 1500


@pytest.mark.asyncio
async def test_skipped_state_and_server_document_id_round_trip(service) -> None:
    # 'skipped' (terminal, no extractable text) MUST be an accepted, persisted
    # member state — otherwise an index holding a no-text doc 400s on save and the
    # whole record is lost. The backend-doc id (for exact "remove from index")
    # round-trips alongside it; a member without one reads back as None.
    await _save(
        service, "vix_skip", owner=USER,
        members=(
            VectorIndexMember("fa_1", "embedded", server_document_id="kd_1"),
            VectorIndexMember("fa_2", "skipped"),
        ),
    )
    page, _ = await service.list_indexes(caller_user_id=USER, workspace_id=None, limit=50, after=None)
    members = page[0].members
    assert [(m.file_id, m.state) for m in members] == [("fa_1", "embedded"), ("fa_2", "skipped")]
    assert [m.server_document_id for m in members] == ["kd_1", None]


@pytest.mark.asyncio
async def test_duplicate_members_collapsed_first_wins(service) -> None:
    await _save(
        service, "vix_1", owner=USER,
        members=(VectorIndexMember("fa_1", "embedded"),
                 VectorIndexMember("fa_2", "pending"),
                 VectorIndexMember("fa_1", "pending")),
    )
    page, _ = await service.list_indexes(caller_user_id=USER, workspace_id=None, limit=50, after=None)
    members = page[0].members
    # fa_1 collapsed to its first occurrence (state embedded), order preserved.
    assert [(m.file_id, m.state) for m in members] == [("fa_1", "embedded"), ("fa_2", "pending")]


@pytest.mark.asyncio
async def test_index_keyset_walks(service) -> None:
    for n, ts in enumerate([10.0, 10.0, 20.0, 30.0, 30.0]):
        await _save(service, f"vix_{n}", owner=USER, created_at=ts)
    seen, cursor = [], None
    for _ in range(10):
        pg, nxt = await service.list_indexes(caller_user_id=USER, workspace_id=None, limit=2, after=cursor)
        seen.extend(r.id for r in pg)
        if nxt is None:
            break
        cursor = decode_cursor(nxt)
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_upsert_preserves_owner_and_replaces_children_wholesale(service) -> None:
    await _save(
        service, "vix_1", owner=USER, created_at=100.0,
        members=(VectorIndexMember("fa_1", "embedded"), VectorIndexMember("fa_2", "embedded")),
        history=(VectorIndexHistoryEntry("ok", 2, 10, None, 1.0, 2.0),
                 VectorIndexHistoryEntry("error", 0, 5, "boom", 0.0, 0.5)),
    )
    # Re-save with a different owner_user_id + created_at (must be ignored) and a
    # shrunk child set (must replace the old set wholesale, not merge).
    await service.save_index(
        id="vix_1", title="renamed", handle="idx", model="text-embedding-3-large",
        dims=3072, status="stale", server_collection_id="kc_9",
        server_collection_model="text-embedding-3-large", last_error=None,
        members=(VectorIndexMember("fa_3", "pending"),),
        history=(VectorIndexHistoryEntry("cancelled", 1, 7, None, 3.0, 3.5),),
        created_at=999.0, updated_at=200.0, caller_user_id=SOMEONE_ELSE,
        workspace_id=None, visible_to=_scoped(USER),
    )
    page, _ = await service.list_indexes(caller_user_id=USER, workspace_id=None, limit=50, after=None)
    record = page[0]
    assert record.title == "renamed"
    assert record.created_at == 100.0
    assert record.created_by_user_id == USER
    assert record.server_collection_id == "kc_9"
    assert record.server_collection_model == "text-embedding-3-large"
    assert [m.file_id for m in record.members] == ["fa_3"]
    assert [h.result for h in record.history] == ["cancelled"]


@pytest.mark.asyncio
async def test_owner_isolation(service) -> None:
    await _save(service, "vix_a", owner=USER_A)
    page, _ = await service.list_indexes(caller_user_id=USER_B, workspace_id=None, limit=50, after=None)
    assert page == []
    with pytest.raises(VectorIndexNotFound):
        await service.delete_index("vix_a", visible_to=_scoped(USER_B))


@pytest.mark.asyncio
async def test_validation(service) -> None:
    with pytest.raises(VectorIndexValidationError):
        await _save(service, "vix_x", owner=USER, status="bogus")
    with pytest.raises(VectorIndexValidationError):
        await _save(service, "vix_y", owner=USER,
                    members=(VectorIndexMember("fa_1", "weird"),))
    with pytest.raises(VectorIndexValidationError):
        await _save(service, "vix_z", owner=USER,
                    history=(VectorIndexHistoryEntry("nope", 1, 1, None, 1.0, 2.0),))


def test_history_payload_parser_rejects_malformed_input() -> None:
    """The router's body parsers reject malformed members/history as a client
    error (_PayloadError -> HTTP 400), never an uncaught 500."""
    from inqtrix.server.routers.vector_indexes import (
        _PayloadError,
        _parse_history,
        _parse_members,
    )

    # Non-int documents must not slip through int(...) into a 500.
    with pytest.raises(_PayloadError):
        _parse_history([{"documents": "abc", "started_at": 1.0, "finished_at": 2.0}])
    with pytest.raises(_PayloadError):
        _parse_history([{"duration_ms": [1], "started_at": 1.0, "finished_at": 2.0}])
    # Missing file_id is rejected; a well-formed member defaults its state.
    with pytest.raises(_PayloadError):
        _parse_members([{"state": "embedded"}])
    assert _parse_members([{"file_id": "fa_1"}])[0].state == "pending"


@pytest.mark.asyncio
async def test_bound_lifecycle_rejects_a_stale_member_after_delete() -> None:
    authority = MemorySourceLifecycleAuthority()
    scope = SourceScope(
        tenant_id="default",
        source_id="asset:fa_1",
        owner_user_id=USER,
        workspace_id=None,
    )
    authority.register_active(scope)
    store = MemoryVectorIndexStore()
    store.bind_source_lifecycle_authority(authority)
    bound = VectorIndexService(store=store)
    await _save(
        bound,
        "vix_1",
        owner=USER,
        members=(VectorIndexMember("fa_1", "embedded"),),
    )

    permit = authority.begin_delete(scope, operation_id="del_1")
    authority.complete_delete(permit)

    with pytest.raises(VectorIndexMemberUnavailable):
        await _save(
            bound,
            "vix_1",
            owner=USER,
            members=(VectorIndexMember("fa_1", "embedded"),),
        )
