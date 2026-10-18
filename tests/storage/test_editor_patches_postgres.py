"""Postgres integration tests for the editor-patch store (gated suite).

Lockstep with the memory tier in ``tests/test_editor_patches.py``: same
lifecycle state machine, same error types, same replay identities. What
only this suite can prove: migration 0031's real constraints — the
``pending -> decided`` CAS on the row, and the ``ON DELETE CASCADE``
from ``editor_documents``.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchRevisionConflict,
)
from inqtrix.project.editor_postgres import PostgresEditorStore
from inqtrix.services.editor_patch_service import EditorPatchService
from inqtrix.services.editor_persistence_service import EditorPersistenceService
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.editor_patch_postgres import PostgresEditorPatchStore
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_USER_ID = canonical_user_id("editor-patch-owner")

_DOC = "# Titel\n\nAlpha beta gamma.\n\nDelta epsilon zeta."


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def wiped():
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
                    "superuser/BYPASSRLS user for cross-tenant cleanup."
                )
            # Patches cascade with their documents; wipe both explicitly.
            await session.execute(text("DELETE FROM editor_patches"))
            await session.execute(text("DELETE FROM editor_comments"))
            await session.execute(text("DELETE FROM editor_documents"))
            await session.execute(text("DELETE FROM editor_folders"))
            await ensure_canonical_users(session, (OWNER_USER_ID,))
    await engine.dispose()
    yield


@pytest_asyncio.fixture()
async def patch_store(wiped):
    store = PostgresEditorPatchStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    yield store
    await store.aclose()


@pytest_asyncio.fixture()
async def persistence(wiped):
    store = PostgresEditorStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    yield EditorPersistenceService(store=store, durable=True)
    await store.aclose()


@pytest.fixture()
def service(patch_store, persistence):
    return EditorPatchService(
        store=patch_store,
        editor_persistence=persistence,
        audit=None,
        durable=True,
    )


async def _seed_document(
    persistence: EditorPersistenceService, *, revision: int = 3
) -> None:
    await persistence.save_document(
        id="ed_doc",
        title="Bericht",
        content_markdown=_DOC,
        folder_id=None,
        source="blank",
        source_run_id=None,
        revision=revision,
        diff_anchor_markdown=None,
        diff_anchor_updated_at=None,
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=None,
        workspace_id=None,
        visible_to=None,
    )


def _raw_edits() -> list[dict]:
    return [
        {
            "find": "Alpha beta gamma.",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "Alpha verbessert.",
            "note": "Straffung",
        },
        {
            "find": "NICHT VORHANDEN",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "x",
            "note": "",
        },
    ]


@pytest.mark.asyncio
async def test_patch_lifecycle_lockstep(service, persistence):
    await _seed_document(persistence)

    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="instruct",
        edits=_raw_edits(),
        summary="Zwei Aenderungen",
        warnings=["Ein Anker unsicher"],
        created_by_user_id=OWNER_USER_ID,
        visible_to=None,
    )
    assert patch.status == "pending"
    assert patch.revision_before == 3
    assert [edit["id"] for edit in patch.edits] == ["ed_1", "ed_2"]

    fetched, document_revision = await service.get_patch(
        patch.patch_id, visible_to=None
    )
    # The JSON round-trip preserves the edit shape byte-identically.
    assert fetched.edits == patch.edits
    assert fetched.warnings == ("Ein Anker unsicher",)
    assert document_revision == 3

    with pytest.raises(PatchRevisionConflict) as conflict:
        await service.apply(patch.patch_id, expected_revision=2, visible_to=None)
    assert conflict.value.current_revision == 3
    assert conflict.value.revision_before == 3

    applied = await service.apply(
        patch.patch_id, expected_revision=3, visible_to=None
    )
    assert applied.status == "accepted"
    assert applied.applied_revision == 4
    assert applied.applied_edit_ids == ("ed_1",)
    document = await persistence.get_document("ed_doc", visible_to=None)
    assert document.revision == 4
    assert "Alpha verbessert." in document.content_markdown

    replay = await service.apply(
        patch.patch_id, expected_revision=3, visible_to=None
    )
    assert replay.applied_revision == 4
    assert replay.applied_edit_ids == ("ed_1",)

    with pytest.raises(PatchAlreadyDecided):
        await service.apply(patch.patch_id, expected_revision=4, visible_to=None)
    with pytest.raises(PatchAlreadyDecided):
        await service.reject(patch.patch_id, note="zu spaet", visible_to=None)


@pytest.mark.asyncio
async def test_reject_flow_and_list_filters(service, persistence):
    await _seed_document(persistence)
    first = await service.propose(
        document_id="ed_doc", run_id=None, source="suggest",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=None, visible_to=None,
    )
    second = await service.propose(
        document_id="ed_doc", run_id=None, source="agent",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=None, visible_to=None,
    )

    rejected = await service.reject(
        first.patch_id, note="Passt nicht.", visible_to=None
    )
    assert rejected.status == "rejected"
    assert rejected.note == "Passt nicht."
    replay = await service.reject(first.patch_id, note="anders", visible_to=None)
    assert replay.note == "Passt nicht."

    everything = await service.list_for_document(
        "ed_doc", status=None, visible_to=None
    )
    assert [p.patch_id for p in everything] == [second.patch_id, first.patch_id]
    pending = await service.list_for_document(
        "ed_doc", status="pending", visible_to=None
    )
    assert [p.patch_id for p in pending] == [second.patch_id]


@pytest.mark.asyncio
async def test_patches_cascade_with_their_document(service, persistence):
    await _seed_document(persistence)
    await service.propose(
        document_id="ed_doc", run_id=None, source="instruct",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=None, visible_to=None,
    )

    await persistence.delete_document("ed_doc", visible_to=None)

    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM editor_patches")
                )
            ).scalar_one()
            assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_patches_list_newest_first_whichever_unit_stamped_them(
    patch_store, persistence
):
    """The agent patch API stamps Unix seconds; a patch materialized from a
    collaboration suggestion carries the suggestion's time, epoch
    milliseconds (Unix seconds for older AI suggestions). Newest first is by
    when they were made, and each keeps its time as written."""
    await _seed_document(persistence)
    stamps = {
        "pch_agent_oldest": 1_790_942_600.0,
        "pch_typed": 1_790_942_610_000.0,
        "pch_ai_in_seconds": 1_790_942_620.0,
        "pch_ai_newest": 1_790_942_630_000.0,
    }
    for patch_id, created_at in stamps.items():
        await patch_store.create(
            EditorPatchRecord(
                patch_id=patch_id, document_id="ed_doc", created_at=created_at
            )
        )

    listed = await patch_store.list_for_document("ed_doc")

    newest_first = [
        "pch_ai_newest",
        "pch_ai_in_seconds",
        "pch_typed",
        "pch_agent_oldest",
    ]
    assert [patch.patch_id for patch in listed] == newest_first
    assert [patch.created_at for patch in listed] == [
        stamps[patch_id] for patch_id in newest_first
    ]
