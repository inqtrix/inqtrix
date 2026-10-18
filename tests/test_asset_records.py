"""Behavior tests for the file-asset-record service (offline tier)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import decode_cursor
from inqtrix.project.asset_records_memory import MemoryAssetStore
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetNotFound,
    AssetUploadConflict,
    GroupNotFound,
    SectionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.services.asset_records_service import (
    AssetRecordsService,
    AssetValidationError,
)


USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
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
def service() -> AssetRecordsService:
    return AssetRecordsService(store=MemoryAssetStore(), durable=False)


async def _section(service, sid, *, owner, created_at=1.0):
    await service.save_section(
        id=sid, kind="custom", title="S", created_at=created_at, updated_at=created_at,
        caller_user_id=owner, workspace_id=None, visible_to=_scoped(owner),
    )


async def _asset(service, aid, *, owner, section_id="fsec_1", group_id=None,
                 created_at=1.0, text="body"):
    await service.save_asset(
        id=aid, section_id=section_id, group_id=group_id, title="A", label="A",
        file_name="a.pdf", mime_type="application/pdf", origin="library",
        page_count=2, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, server_file_id=None,
        extracted_text=text, created_at=created_at, updated_at=created_at,
        caller_user_id=owner, workspace_id=None, visible_to=_scoped(owner),
    )


@pytest.mark.asyncio
async def test_prepared_sections_are_scope_idempotent_under_concurrent_first_loads(
    service,
) -> None:
    first, second = await asyncio.gather(
        service.ensure_default_sections(caller_user_id=USER, workspace_id="ws-a"),
        service.ensure_default_sections(caller_user_id=USER, workspace_id="ws-a"),
    )

    assert [section.semantic_role for section in first] == [
        "temporary",
        "library",
        "project_sources",
    ]
    assert [section.id for section in first] == [section.id for section in second]
    assert len(
        await service.list_sections(caller_user_id=USER, workspace_id="ws-a")
    ) == 3

    other_workspace = await service.ensure_default_sections(
        caller_user_id=USER,
        workspace_id="ws-b",
    )
    other_owner = await service.ensure_default_sections(
        caller_user_id=USER_B,
        workspace_id="ws-a",
    )
    assert {section.id for section in first}.isdisjoint(
        section.id for section in other_workspace
    )
    assert {section.id for section in first}.isdisjoint(
        section.id for section in other_owner
    )


@pytest.mark.asyncio
async def test_custom_titles_are_not_identity_and_renaming_releases_prepared_role(
    service,
) -> None:
    prepared = await service.ensure_default_sections(
        caller_user_id=USER,
        workspace_id=None,
    )
    library = next(
        section for section in prepared if section.semantic_role == "library"
    )
    await service.save_section(
        id="same-title-a",
        kind="custom",
        title="Bibliothek",
        created_at=2.0,
        updated_at=2.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=_scoped(USER),
    )
    await service.save_section(
        id="same-title-b",
        kind="custom",
        title="Bibliothek",
        created_at=3.0,
        updated_at=3.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=_scoped(USER),
    )

    renamed = await service.save_section(
        id=library.id,
        kind=library.kind,
        title="Meine Ablage",
        created_at=library.created_at,
        updated_at=4.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=_scoped(USER),
    )
    assert renamed.semantic_role == "custom"

    converged = await service.ensure_default_sections(
        caller_user_id=USER,
        workspace_id=None,
    )
    replacement = next(
        section for section in converged if section.semantic_role == "library"
    )
    assert replacement.id != library.id
    sections = await service.list_sections(
        caller_user_id=USER,
        workspace_id=None,
    )
    same_titles = [
        section for section in sections if section.title == "Bibliothek"
    ]
    assert {section.id for section in same_titles}.issuperset(
        {"same-title-a", "same-title-b", replacement.id}
    )


@pytest.mark.asyncio
async def test_asset_body_lazy_listed_loaded_on_get(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await _asset(service, "fa_1", owner=USER, text="the heavy extracted text")
    page, _ = await service.list_assets(caller_user_id=USER, workspace_id=None, limit=50, after=None)
    assert page[0].extracted_text == ""  # body excluded from list
    full = await service.get_asset("fa_1", visible_to=_scoped(USER))
    assert full.extracted_text == "the heavy extracted text"


@pytest.mark.asyncio
async def test_asset_keyset_walks(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    for n, ts in enumerate([10.0, 10.0, 20.0, 30.0, 30.0]):
        await _asset(service, f"fa_{n}", owner=USER, created_at=ts)
    seen, cursor = [], None
    for _ in range(10):
        pg, nxt = await service.list_assets(caller_user_id=USER, workspace_id=None, limit=2, after=cursor)
        seen.extend(a.id for a in pg)
        if nxt is None:
            break
        cursor = decode_cursor(nxt)
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_asset_upsert_preserves_owner_and_created_at(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await _asset(service, "fa_1", owner=USER, created_at=100.0, text="v1")
    await service.save_asset(
        id="fa_1", section_id="fsec_1", group_id=None, title="renamed", label="A",
        file_name="a.pdf", mime_type="application/pdf", origin="library",
        page_count=2, parse_status="parsed", parse_warning=None, text_truncated=False,
        size_bytes=10, server_file_id=None, extracted_text="v2",
        created_at=999.0, updated_at=200.0, caller_user_id=USER, workspace_id=None,
        visible_to=_scoped(USER),
    )
    asset = await service.get_asset("fa_1", visible_to=_scoped(USER))
    assert asset.title == "renamed"
    assert asset.extracted_text == "v2"
    assert asset.created_at == 100.0
    assert asset.created_by_user_id == USER


@pytest.mark.asyncio
async def test_parser_id_round_trips_as_metadata(service) -> None:
    """parser_id (parse provenance) survives save and rides the metadata
    list (not just the body GET), so the UI badge is correct after a
    keyset-paged reload. Defaults to None when the caller omits it."""
    await _section(service, "fsec_1", owner=USER)
    await service.save_asset(
        id="fa_srv", section_id="fsec_1", group_id=None, title="A", label="A",
        file_name="a.pdf", mime_type="application/pdf", origin="library",
        page_count=2, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, server_file_id=None,
        parser_id="markitdown", extracted_text="body", created_at=1.0,
        updated_at=1.0, caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    await _asset(service, "fa_local", owner=USER, created_at=2.0)  # no parser_id
    by_id = {
        a.id: a
        for a, _ in [
            (await service.get_asset("fa_srv", visible_to=_scoped(USER)), None),
            (await service.get_asset("fa_local", visible_to=_scoped(USER)), None),
        ]
    }
    assert by_id["fa_srv"].parser_id == "markitdown"
    assert by_id["fa_local"].parser_id is None
    page, _ = await service.list_assets(
        caller_user_id=USER, workspace_id=None, limit=50, after=None
    )
    assert {a.id: a.parser_id for a in page} == {
        "fa_srv": "markitdown",
        "fa_local": None,
    }


@pytest.mark.asyncio
async def test_owner_isolation(service) -> None:
    await _section(service, "fsec_1", owner=USER_A)
    await _asset(service, "fa_a", owner=USER_A)
    with pytest.raises(AssetNotFound):
        await service.get_asset("fa_a", visible_to=_scoped(USER_B))
    page, _ = await service.list_assets(caller_user_id=USER_B, workspace_id=None, limit=50, after=None)
    assert page == []


@pytest.mark.asyncio
async def test_section_delete_cascades_group_delete_orphans(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await service.save_group(
        id="fg_1", section_id="fsec_1", title="G", created_at=1.0, updated_at=1.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    await _asset(service, "fa_1", owner=USER, group_id="fg_1")
    # Delete the group -> asset orphans to ungrouped (group_id null).
    await service.delete_group("fg_1", visible_to=_scoped(USER))
    assert (await service.get_asset("fa_1", visible_to=_scoped(USER))).group_id is None
    # Delete the section -> its assets + groups cascade away.
    await service.delete_section("fsec_1", visible_to=_scoped(USER))
    with pytest.raises(AssetNotFound):
        await service.get_asset("fa_1", visible_to=_scoped(USER))


@pytest.mark.asyncio
async def test_validation(service) -> None:
    with pytest.raises(AssetValidationError):
        await service.save_section(
            id="fsec_x", kind="bogus", title="S", created_at=1.0, updated_at=1.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )
    await _section(service, "fsec_1", owner=USER)
    with pytest.raises(AssetValidationError):
        await _asset_bad_origin(service)

    with pytest.raises(AssetUploadConflict):
        await service.save_asset(
            id="fa_untrusted_binding", section_id="fsec_1", group_id=None,
            title="A", label="A", file_name="a.pdf",
            mime_type="application/pdf", origin="library", page_count=None,
            parse_status="parsed", parse_warning=None, text_truncated=False,
            size_bytes=10, server_file_id="fl_unvalidated", extracted_text="",
            created_at=1.0, updated_at=1.0, caller_user_id=USER,
            workspace_id=None, visible_to=_scoped(USER),
        )


async def _asset_bad_origin(service):
    await service.save_asset(
        id="fa_bad", section_id="fsec_1", group_id=None, title="A", label="A",
        file_name="a", mime_type="x", origin="bogus", page_count=None,
        parse_status="parsed", parse_warning=None, text_truncated=False, size_bytes=0,
        server_file_id=None, extracted_text="", created_at=1.0, updated_at=1.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )


# ------------------------------------------------------------------ #
# Upload binding (asset record created together with a file upload)
# ------------------------------------------------------------------ #


async def _bind(service, *, owner, visible_to, section_id="fsec_1",
                group_id=None, aid="fa_up", origin="library",
                parse_status="parsed"):
    await service.reserve_upload(
        id=aid, section_id=section_id, group_id=group_id, title="U", label="U",
        file_name="u.pdf", mime_type="application/pdf", origin=origin,
        page_count=None, parse_status=parse_status, parse_warning=None,
        text_truncated=False, size_bytes=10, created_at=1.0, updated_at=1.0,
        caller_user_id=owner, workspace_id=None, visible_to=visible_to,
    )
    return await service.bind_uploaded_file(
        id=aid, section_id=section_id, group_id=group_id, title="U", label="U",
        file_name="u.pdf", mime_type="application/pdf", origin=origin,
        page_count=None, parse_status=parse_status, parse_warning=None,
        text_truncated=False, size_bytes=10, server_file_id="fl_up",
        created_at=1.0, updated_at=1.0, caller_user_id=owner,
        workspace_id=None, visible_to=visible_to,
    )


@pytest.mark.asyncio
async def test_reserved_upload_finalizes_section_bound_asset_with_empty_body(service) -> None:
    """A bound upload yields a durable asset in the target section whose
    body is empty — the extracted text always arrives via the regular
    asset upsert, never through the upload path."""
    await _section(service, "fsec_1", owner=USER)
    bound = await _bind(service, owner=USER, visible_to=_scoped(USER))
    assert bound.section_id == "fsec_1"
    assert bound.server_file_id == "fl_up"
    full = await service.get_asset("fa_up", visible_to=_scoped(USER))
    assert full.extracted_text == ""
    assert full.upload_status == "ready"


@pytest.mark.asyncio
async def test_group_tombstone_fences_upload_finalization(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await service.save_group(
        id="fg_1",
        section_id="fsec_1",
        title="Group",
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=_scoped(USER),
    )
    await service.reserve_upload(
        id="fa_up",
        section_id="fsec_1",
        group_id="fg_1",
        title="U",
        label="U",
        file_name="u.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=None,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=10,
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=_scoped(USER),
    )
    await service.store.tombstone_group_id(
        "fg_1",
        scope=ResourceScope(USER, None),
    )

    with pytest.raises(AssetDeletionInProgress):
        await service.bind_uploaded_file(
            id="fa_up",
            section_id="fsec_1",
            group_id="fg_1",
            title="U",
            label="U",
            file_name="u.pdf",
            mime_type="application/pdf",
            origin="library",
            page_count=None,
            parse_status="parsed",
            parse_warning=None,
            text_truncated=False,
            size_bytes=10,
            server_file_id="fl_up",
            created_at=1.0,
            updated_at=2.0,
            caller_user_id=USER,
            workspace_id=None,
            visible_to=_scoped(USER),
        )


@pytest.mark.asyncio
async def test_reservation_is_idempotent_but_rejects_identity_reuse(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    first = await service.reserve_upload(
        id="fa_up", section_id="fsec_1", group_id=None, title="U", label="U",
        file_name="u.pdf", mime_type="application/pdf", origin="library",
        page_count=None, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, created_at=1.0, updated_at=1.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    repeated = await service.reserve_upload(
        id="fa_up", section_id="fsec_1", group_id=None, title="U", label="U",
        file_name="u.pdf", mime_type="application/pdf", origin="library",
        page_count=None, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, created_at=1.0, updated_at=2.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    assert first.id == repeated.id
    assert repeated.upload_status == "awaiting_upload"

    await _bind(service, owner=USER, visible_to=_scoped(USER))
    with pytest.raises(AssetUploadConflict):
        await service.reserve_upload(
            id="fa_up", section_id="fsec_1", group_id=None, title="U", label="U",
            file_name="different.pdf", mime_type="application/pdf", origin="library",
            page_count=None, parse_status="parsed", parse_warning=None,
            text_truncated=False, size_bytes=11, created_at=1.0, updated_at=3.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )


@pytest.mark.asyncio
async def test_normal_asset_put_cannot_clear_server_file_binding(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await _bind(service, owner=USER, visible_to=_scoped(USER))
    updated = await service.save_asset(
        id="fa_up", section_id="fsec_1", group_id=None, title="Renamed", label="U",
        file_name="stale-local.pdf", mime_type="text/plain", origin="library",
        page_count=1, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=0, server_file_id=None,
        extracted_text="parsed body", created_at=1.0, updated_at=5.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    assert updated.server_file_id == "fl_up"
    assert updated.file_name == "u.pdf"
    assert updated.mime_type == "application/pdf"
    assert updated.size_bytes == 10
    assert updated.extracted_text == "parsed body"
    assert (
        await service.find_asset_by_server_file_id("fl_up")
    ).id == "fa_up"


@pytest.mark.asyncio
async def test_one_original_file_cannot_bind_to_two_assets(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    await _bind(
        service,
        aid="fa_first",
        owner=USER,
        visible_to=_scoped(USER),
    )
    await service.reserve_upload(
        id="fa_second", section_id="fsec_1", group_id=None, title="U", label="U2",
        file_name="u.pdf", mime_type="application/pdf", origin="library",
        page_count=None, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, created_at=1.0, updated_at=1.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    with pytest.raises(AssetUploadConflict):
        await service.bind_uploaded_file(
            id="fa_second", section_id="fsec_1", group_id=None, title="U",
            label="U2", file_name="u.pdf", mime_type="application/pdf",
            origin="library", page_count=None, parse_status="parsed",
            parse_warning=None, text_truncated=False, size_bytes=10,
            server_file_id="fl_up", created_at=1.0, updated_at=1.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )


@pytest.mark.asyncio
async def test_bind_rejects_missing_and_foreign_sections(service) -> None:
    """Binding into a nonexistent or foreign section fails with the same
    indistinct not-found, so probing cannot distinguish the two."""
    with pytest.raises(SectionNotFound):
        await _bind(service, owner=USER_B, visible_to=_scoped(USER_B))
    await _section(service, "fsec_1", owner=USER_A)
    with pytest.raises(SectionNotFound):
        await _bind(service, owner=USER_B, visible_to=_scoped(USER_B))


@pytest.mark.asyncio
async def test_bind_rejects_foreign_or_mismatched_group(service) -> None:
    await _section(service, "fsec_1", owner=USER_A)
    await _section(service, "fsec_2", owner=USER_A)
    await service.save_group(
        id="fg_1", section_id="fsec_2", title="G", created_at=1.0, updated_at=1.0,
        caller_user_id=USER_A, workspace_id=None, visible_to=_scoped(USER_A),
    )
    # Group exists but belongs to a different section than the target.
    with pytest.raises(AssetValidationError):
        await _bind(service, owner=USER_A, visible_to=_scoped(USER_A), group_id="fg_1")
    # Foreign caller sees the group as not-found.
    await _section(service, "fsec_1b", owner=USER_B)
    with pytest.raises(GroupNotFound):
        await _bind(
            service, owner=USER_B, visible_to=_scoped(USER_B),
            section_id="fsec_1b", group_id="fg_1",
        )


@pytest.mark.asyncio
async def test_bind_validates_origin_like_save_asset(service) -> None:
    await _section(service, "fsec_1", owner=USER)
    with pytest.raises(AssetValidationError):
        await _bind(service, owner=USER, visible_to=_scoped(USER), origin="bogus")


@pytest.mark.asyncio
async def test_a_new_local_asset_is_ready_without_any_second_write(service) -> None:
    """The rejected fix would have flipped every local asset here.

    ``server_file_id=None`` legitimately means "local-only asset that was
    never uploaded" -- deriving the status from the column would read that
    as not-ready. The intent belongs to the caller: plain ``save_asset``
    creates a finished local asset, and it must be ``ready`` immediately,
    with no follow-up write involved.
    """
    await _section(service, "fsec_local", owner=USER)
    asset = await service.save_asset(
        id="fa_local", section_id="fsec_local", group_id=None, title="L",
        label="L", file_name="local.md", mime_type="text/markdown",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=5,
        server_file_id=None, extracted_text="hallo", created_at=1.0,
        updated_at=1.0, caller_user_id=USER, workspace_id=None,
        visible_to=_scoped(USER),
    )
    assert asset.upload_status == "ready"


@pytest.mark.asyncio
async def test_a_reservation_is_never_ready_even_for_one_write(service) -> None:
    """The atomicity property itself: the INSERT already carries the intent.

    A row born 'ready' (the column default) and corrected only by a SECOND
    transaction would, after a connection failure in between, leave an
    asset that looks like a complete file and has no bytes. Asserted at
    the store, bypassing the service's follow-up write, so this fails if
    the insert-only intent is ever dropped.
    """
    await _section(service, "fsec_r", owner=USER)
    row = await service._store.upsert_asset(
        id="fa_r", section_id="fsec_r", group_id=None, title="R", label="R",
        file_name="r.pdf", mime_type="application/pdf", origin="library",
        page_count=None, parse_status="parsed", parse_warning=None,
        text_truncated=False, size_bytes=10, server_file_id=None,
        extracted_text="", created_at=1.0, updated_at=1.0,
        created_by_user_id=USER, workspace_id=None,
        initial_upload_status="awaiting_upload",
    )
    assert row.upload_status == "awaiting_upload"


@pytest.mark.asyncio
async def test_the_insert_only_intent_cannot_reset_an_existing_row(service) -> None:
    """On an existing row the intent does nothing -- or a repeated
    reservation could pull a finalised asset back to awaiting_upload."""
    await _section(service, "fsec_keep", owner=USER)
    # The full legitimate path: reserve, then bind the bytes.
    await service.reserve_upload(
        id="fa_keep", section_id="fsec_keep", group_id=None, title="K",
        label="K", file_name="k.pdf", mime_type="application/pdf",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=10,
        created_at=1.0, updated_at=1.0, caller_user_id=USER,
        workspace_id=None, visible_to=_scoped(USER),
    )
    await service.bind_uploaded_file(
        id="fa_keep", section_id="fsec_keep", group_id=None, title="K",
        label="K", file_name="k.pdf", mime_type="application/pdf",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=10,
        server_file_id="fl_keep", created_at=1.0, updated_at=2.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    row = await service._store.upsert_asset(
        id="fa_keep", section_id="fsec_keep", group_id=None, title="K",
        label="K", file_name="k.pdf", mime_type="application/pdf",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=10,
        server_file_id=None, extracted_text="", created_at=1.0,
        updated_at=3.0, created_by_user_id=USER, workspace_id=None,
        initial_upload_status="awaiting_upload",
    )
    assert row.upload_status == "ready", (
        "an existing row keeps its stored status; the intent is insert-only"
    )
    assert row.server_file_id == "fl_keep"


@pytest.mark.asyncio
async def test_a_failed_row_returns_to_awaiting_on_a_retried_reservation(
    service,
) -> None:
    """Why the follow-up write stays: it is what rescues a 'failed' row."""
    await _section(service, "fsec_fail", owner=USER)
    await service.reserve_upload(
        id="fa_fail", section_id="fsec_fail", group_id=None, title="F",
        label="F", file_name="f.pdf", mime_type="application/pdf",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=10,
        created_at=1.0, updated_at=1.0, caller_user_id=USER,
        workspace_id=None, visible_to=_scoped(USER),
    )
    await service.mark_upload_failed(
        "fa_fail", visible_to=_scoped(USER), message="kaputt"
    )
    retried = await service.reserve_upload(
        id="fa_fail", section_id="fsec_fail", group_id=None, title="F",
        label="F", file_name="f.pdf", mime_type="application/pdf",
        origin="library", page_count=None, parse_status="parsed",
        parse_warning=None, text_truncated=False, size_bytes=10,
        created_at=1.0, updated_at=2.0, caller_user_id=USER,
        workspace_id=None, visible_to=_scoped(USER),
    )
    assert retried.upload_status == "awaiting_upload", (
        "deleting the follow-up write as 'now redundant' breaks exactly this"
    )


@pytest.mark.asyncio
async def test_reservation_survives_a_failing_follow_up_write(
    service, monkeypatch
) -> None:
    """The observed failure, replayed: write two dies, the row tells the truth.

    An asyncpg connection timeout between the two reservation writes left
    an asset that looked like a complete file and had no bytes. With the
    intent applied AT the insert, the follow-up write may fail freely --
    the stored row is already 'awaiting_upload', visible in the library as
    "Datei noch nicht uebertragen" with a retry affordance instead of as a
    normal file that 409s on indexing.
    """

    async def _dies(*args, **kwargs):
        raise ConnectionError("verbindung weg zwischen den beiden writes")

    await _section(service, "fsec_atomic", owner=USER)
    monkeypatch.setattr(service._store, "set_asset_upload_state", _dies)

    with pytest.raises(ConnectionError):
        await service.reserve_upload(
            id="fa_atomic", section_id="fsec_atomic", group_id=None,
            title="A", label="A", file_name="a.pdf",
            mime_type="application/pdf", origin="library", page_count=None,
            parse_status="parsed", parse_warning=None, text_truncated=False,
            size_bytes=10, created_at=1.0, updated_at=1.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )

    stored = await service._store.get_asset("fa_atomic")
    assert stored.upload_status == "awaiting_upload", (
        "the insert itself must carry the intent; before the fix this row "
        "was 'ready' with no bytes -- the exact observed corruption"
    )
    assert stored.server_file_id is None


@pytest.mark.asyncio
async def test_an_unknown_insert_intent_is_rejected_at_runtime(service) -> None:
    """The Literal binds only a typechecker, and none runs in this repo.

    Without the runtime guard a typo ("awaiting-upload", "reserved", ...)
    would land as a silent new state in the database. The guard lives in
    the ports module, derived from the Literal, and both backends call it
    before the INSERT.
    """
    from inqtrix.source_authority import MemorySourceLifecycleAuthority

    authority = MemorySourceLifecycleAuthority()
    service._store.bind_source_lifecycle_authority(authority)
    await _section(service, "fsec_guard", owner=USER)
    with pytest.raises(ValueError, match="initial_upload_status"):
        await service._store.upsert_asset(
            id="fa_guard", section_id="fsec_guard", group_id=None, title="G",
            label="G", file_name="g.pdf", mime_type="application/pdf",
            origin="library", page_count=None, parse_status="parsed",
            parse_warning=None, text_truncated=False, size_bytes=10,
            server_file_id=None, extracted_text="", created_at=1.0,
            updated_at=1.0, created_by_user_id=USER, workspace_id=None,
            initial_upload_status="awaiting-upload",
        )
    # Placement pin: the guard must fall BEFORE the source authority's
    # active_write(create_if_missing=True), which registers a lifecycle
    # with no rollback -- a guard inside the write path would leave a
    # phantom ACTIVE lifecycle for an asset row that never existed.
    assert not authority._records, (
        "rejected insert must not register a source lifecycle"
    )
