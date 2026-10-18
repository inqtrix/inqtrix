"""Behavior tests for the editor-persistence service.

Runs against the in-memory tier but asserts the contract both tiers honour:
owner isolation, the document-body lazy-load (list excludes the body,
get includes it), the idempotent autosave upsert, comment composite-PK
isolation + delete, the folder orphan rule, and payload validation.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import decode_cursor
from inqtrix.project.editor_memory import MemoryEditorStore
from inqtrix.project.editor_ports import (
    DocumentNotFound,
    EditorComment,
    FolderNotFound,
    SuggestionDraftRevisionConflict,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.services.collaboration_client import CollaborationProjection
from inqtrix.services.editor_persistence_service import (
    CollaborationProjectionUnavailable,
    EditorPersistenceService,
    EditorValidationError,
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
def service() -> EditorPersistenceService:
    return EditorPersistenceService(store=MemoryEditorStore(), durable=False)


async def _save_doc(
    service: EditorPersistenceService,
    *,
    document_id: str,
    caller_user_id: uuid.UUID | None,
    created_at: float,
    title: str = "Doc",
    body: str = "body text",
    folder_id: str | None = None,
) -> None:
    await service.save_document(
        id=document_id,
        title=title,
        content_markdown=body,
        folder_id=folder_id,
        source="blank",
        source_run_id=None,
        revision=1,
        diff_anchor_markdown=None,
        diff_anchor_updated_at=None,
        created_at=created_at,
        updated_at=created_at,
        caller_user_id=caller_user_id,
        workspace_id=None,
        visible_to=_scoped(caller_user_id) if caller_user_id else None,
    )


def _comment(comment_id: str, *, created_at: float, status: str = "open") -> dict:
    return {
        "id": comment_id,
        "comment_markdown": f"c-{comment_id}",
        "anchor": {"from": 0, "to": 5, "selectedText": "hello", "quoteBefore": "", "quoteAfter": ""},
        "kind": "collect",
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
    }


@pytest.mark.asyncio
async def test_document_body_is_lazy_listed_but_loaded_on_get(service) -> None:
    """list_documents returns metadata only (no body); get_document loads it."""
    await _save_doc(
        service, document_id="ed_1", caller_user_id=USER, created_at=1.0,
        body="the heavy body",
    )
    page, _ = await service.list_documents(
        caller_user_id=USER, workspace_id=None, limit=50, after=None
    )
    assert len(page) == 1
    assert page[0].content_markdown == ""  # body excluded from the list
    full = await service.get_document("ed_1", visible_to=_scoped(USER))
    assert full.content_markdown == "the heavy body"


@pytest.mark.asyncio
async def test_document_keyset_walks_with_tiebreaker(service) -> None:
    stamps = [10.0, 10.0, 20.0, 30.0, 30.0]
    for n, stamp in enumerate(stamps):
        await _save_doc(service, document_id=f"ed_{n}", caller_user_id=USER, created_at=stamp)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page, next_cursor = await service.list_documents(
            caller_user_id=USER, workspace_id=None, limit=2, after=cursor
        )
        seen.extend(d.id for d in page)
        if next_cursor is None:
            break
        cursor = decode_cursor(next_cursor)
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_document_upsert_preserves_owner_and_created_at(service) -> None:
    await _save_doc(service, document_id="ed_1", caller_user_id=USER_A, created_at=100.0, title="first")
    # base+1 (stored is 1) — a normal next save; created_at/owner must survive
    # even though this save passes a different created_at.
    await service.save_document(
        id="ed_1", title="second", content_markdown="new body", folder_id=None,
        source="pasted", source_run_id=None, revision=2,
        diff_anchor_markdown=None, diff_anchor_updated_at=None,
        created_at=999.0, updated_at=200.0,
        caller_user_id=USER_A, workspace_id=None, visible_to=_scoped(USER_A),
    )
    doc = await service.get_document("ed_1", visible_to=_scoped(USER_A))
    assert doc.title == "second"
    assert doc.content_markdown == "new body"
    assert doc.revision == 2
    assert doc.created_at == 100.0  # creation time stable
    assert doc.created_by_user_id == USER_A


@pytest.mark.asyncio
async def test_metadata_cas_preserves_sets_and_clears_diff_anchor(service) -> None:
    """Omitted, valued, and null anchor fields retain their tri-state meaning."""
    await _save_doc(
        service,
        document_id="ed_anchor",
        caller_user_id=USER_A,
        created_at=1.0,
    )

    renamed = await service.patch_document_metadata(
        "ed_anchor",
        expected_metadata_revision=1,
        title="Renamed",
        folder_id=None,
        set_folder_id=False,
        diff_anchor_markdown=None,
        set_diff_anchor_markdown=False,
        diff_anchor_updated_at=None,
        set_diff_anchor_updated_at=False,
        visible_to=_scoped(USER_A),
    )
    assert renamed.diff_anchor_markdown is None
    assert renamed.diff_anchor_updated_at is None

    anchored = await service.patch_document_metadata(
        "ed_anchor",
        expected_metadata_revision=2,
        title=None,
        folder_id=None,
        set_folder_id=False,
        diff_anchor_markdown="# Projected anchor",
        set_diff_anchor_markdown=True,
        diff_anchor_updated_at=123.5,
        set_diff_anchor_updated_at=True,
        visible_to=_scoped(USER_A),
    )
    assert anchored.diff_anchor_markdown == "# Projected anchor"
    assert anchored.diff_anchor_updated_at == 123.5

    cleared = await service.patch_document_metadata(
        "ed_anchor",
        expected_metadata_revision=3,
        title=None,
        folder_id=None,
        set_folder_id=False,
        diff_anchor_markdown=None,
        set_diff_anchor_markdown=True,
        diff_anchor_updated_at=None,
        set_diff_anchor_updated_at=True,
        visible_to=_scoped(USER_A),
    )
    assert cleared.diff_anchor_markdown is None
    assert cleared.diff_anchor_updated_at is None


@pytest.mark.asyncio
async def test_memory_comment_upsert_rejects_foreign_author_atomically(service) -> None:
    """Memory parity preflights every comment before changing any row."""
    await _save_doc(
        service,
        document_id="ed_private",
        caller_user_id=USER_A,
        created_at=1.0,
    )
    store = service.store
    owned = EditorComment(
        id="edc_owned",
        document_id="ed_private",
        comment_markdown="owner text",
        created_by_user_id=USER_A,
    )
    await store.upsert_comments(
        [owned],
        expected_document_id="ed_private",
        expected_document_owner_user_id=USER_A,
        expected_document_workspace_id=None,
        expected_document_content_mode="markdown",
        actor_user_id=USER_A,
    )

    with pytest.raises(DocumentNotFound):
        await store.upsert_comments(
            [
                replace(
                    owned,
                    id="edc_new",
                    comment_markdown="must not be inserted",
                    created_by_user_id=USER_B,
                ),
                replace(
                    owned,
                    comment_markdown="foreign overwrite",
                    created_by_user_id=USER_B,
                ),
            ],
            expected_document_id="ed_private",
            expected_document_owner_user_id=USER_A,
            expected_document_workspace_id=None,
            expected_document_content_mode="markdown",
            actor_user_id=USER_A,
        )

    comments, _ = await store.list_comments_page(
        "ed_private",
        created_by_user_id=None,
        limit=50,
        after=None,
    )
    assert comments == [owned]


@pytest.mark.asyncio
async def test_stale_document_scope_cannot_comment_after_id_reuse(service) -> None:
    """A recreated document id cannot inherit a stale writer's comment."""
    await _save_doc(
        service,
        document_id="ed_reused",
        caller_user_id=USER_A,
        created_at=1.0,
    )
    store = service.store
    stale = await store.get_document("ed_reused")
    await store.delete_document(
        "ed_reused", scope=ResourceScope.from_record(stale)
    )
    await _save_doc(
        service,
        document_id="ed_reused",
        caller_user_id=USER_B,
        created_at=2.0,
    )
    comment = EditorComment(
        id="edc_stale",
        document_id="ed_reused",
        comment_markdown="must not cross owners",
        created_by_user_id=USER_A,
    )

    with pytest.raises(DocumentNotFound):
        await store.upsert_comments(
            [comment],
            expected_document_id="ed_reused",
            expected_document_owner_user_id=USER_A,
            expected_document_workspace_id=None,
            expected_document_content_mode="markdown",
            actor_user_id=USER_A,
        )

    comments, _ = await store.list_comments_page(
        "ed_reused", limit=50, after=None
    )
    assert comments == []


@pytest.mark.asyncio
async def test_scoped_owner_isolation(service) -> None:
    await _save_doc(service, document_id="ed_a", caller_user_id=USER_A, created_at=1.0)
    with pytest.raises(DocumentNotFound):
        await service.get_document("ed_a", visible_to=_scoped(USER_B))
    page, _ = await service.list_documents(
        caller_user_id=USER_B, workspace_id=None, limit=50, after=None
    )
    assert page == []
    with pytest.raises(DocumentNotFound):
        await service.delete_document("ed_a", visible_to=_scoped(USER_B))


@pytest.mark.asyncio
async def test_comments_upsert_list_and_cross_document_isolation(service) -> None:
    await _save_doc(service, document_id="ed_a", caller_user_id=USER, created_at=1.0)
    await _save_doc(service, document_id="ed_b", caller_user_id=USER, created_at=1.0)
    await service.save_comments(
        "ed_a",
        comments=[_comment("edc_dup", created_at=1.0)],
        visible_to=_scoped(USER),
    )
    # Same comment id in a DIFFERENT document must be a distinct row.
    await service.save_comments(
        "ed_b",
        comments=[{**_comment("edc_dup", created_at=1.0), "comment_markdown": "B-text"}],
        visible_to=_scoped(USER),
    )
    a_page, _ = await service.list_comments(
        "ed_a", limit=50, after=None, visible_to=_scoped(USER)
    )
    assert a_page[0].comment_markdown == "c-edc_dup"  # NOT clobbered by B
    b_page, _ = await service.list_comments(
        "ed_b", limit=50, after=None, visible_to=_scoped(USER)
    )
    assert b_page[0].comment_markdown == "B-text"


@pytest.mark.asyncio
async def test_comment_reads_do_not_invoke_the_ai_projection_barrier(service) -> None:
    """Private comment reads authorize the document without flushing Node."""
    await _save_doc(
        service,
        document_id="ed_collaboration_comments",
        caller_user_id=USER,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document("ed_collaboration_comments")
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
    )
    projection_calls: list[str] = []

    async def project(**kwargs: Any) -> None:
        projection_calls.append(kwargs["document_id"])

    service.bind_collaboration_projector(project)

    comments, cursor = await service.list_comments(
        document.id,
        limit=50,
        after=None,
        visible_to=_scoped(USER),
    )

    assert comments == []
    assert cursor is None
    assert projection_calls == []


@pytest.mark.asyncio
async def test_ai_read_consumes_the_exact_projection_barrier_result(service) -> None:
    """A post-barrier update cannot make AI consume an N/N+1 row snapshot."""
    await _save_doc(
        service,
        document_id="ed_collaboration_ai_projection",
        caller_user_id=USER,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document("ed_collaboration_ai_projection")
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
        persisted_sequence=6,
        projection_sequence=6,
    )

    async def project(**kwargs: Any) -> CollaborationProjection:
        current = await store.get_document(kwargs["document_id"])
        store._documents[current.id] = replace(  # type: ignore[attr-defined]
            current,
            content_markdown="# Older stored projection",
            persisted_sequence=8,
            projection_sequence=7,
        )
        return CollaborationProjection(
            generation=1,
            sequence=7,
            markdown="# Exact projection",
            projection_hash="1" * 64,
            schema_hash="0" * 64,
            authoritative_sequence=7,
        )

    service.bind_collaboration_projector(project)

    result = await service.get_document_for_ai(
        document.id,
        visible_to=_scoped(USER),
    )

    assert result.content_markdown == "# Exact projection"
    assert result.persisted_sequence == 7
    assert result.projection_sequence == 7


@pytest.mark.asyncio
async def test_ai_read_rejects_a_non_current_projection_result(service) -> None:
    """AI consumers fail visibly if a projector violates the exact pair contract."""
    await _save_doc(
        service,
        document_id="ed_collaboration_ai_projection_conflict",
        caller_user_id=USER,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document(
        "ed_collaboration_ai_projection_conflict"
    )
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
    )

    async def project(**kwargs: Any) -> CollaborationProjection:
        del kwargs
        return CollaborationProjection(
            generation=1,
            sequence=7,
            markdown="# Stale projection",
            projection_hash="1" * 64,
            schema_hash="0" * 64,
            authoritative_sequence=8,
        )

    service.bind_collaboration_projector(project)

    with pytest.raises(CollaborationProjectionUnavailable):
        await service.get_document_for_ai(
            document.id,
            visible_to=_scoped(USER),
        )


@pytest.mark.asyncio
async def test_comment_delete_and_document_cascade(service) -> None:
    await _save_doc(service, document_id="ed_a", caller_user_id=USER, created_at=1.0)
    await service.save_comments(
        "ed_a", comments=[_comment("edc_1", created_at=1.0)], visible_to=_scoped(USER)
    )
    await service.delete_comment("ed_a", "edc_1", visible_to=_scoped(USER))
    gone, _ = await service.list_comments(
        "ed_a", limit=50, after=None, visible_to=_scoped(USER)
    )
    assert gone == []
    # Re-add then delete the document -> comments cascade.
    await service.save_comments(
        "ed_a", comments=[_comment("edc_2", created_at=2.0)], visible_to=_scoped(USER)
    )
    await service.delete_document("ed_a", visible_to=_scoped(USER))
    with pytest.raises(DocumentNotFound):
        await service.get_document("ed_a", visible_to=_scoped(USER))


@pytest.mark.asyncio
async def test_assistant_edit_carrier_draft_keeps_its_edit_position(service) -> None:
    """Ein Assistentenlauf braucht eine Traegerzeile und die Einfuegeart.

    Der Waechter laesst eine assistant-Veroeffentlichung nur zu, wenn ein
    creator-privater Entwurf genau dieses patch_id/command_id vorher
    autorisiert hat, und ein Entwurf kann nur an einer Kommentarzeile
    haengen. Ein Assistentenlauf hat keinen Kommentar des Nutzers, also
    legt er eine eigene Zeile der Art ``assistant_edit`` an.

    Der Entwurf muss dabei ``edit_position`` und ``anchor_text`` tragen:
    ohne sie faellt der Uebernahmepfad auf "ersetzen" zurueck, und aus
    "nach dem Anker einfuegen" wird "den Anker ersetzen" -- stiller
    Textverlust im Dokument des Nutzers.
    """
    await _save_doc(
        service,
        document_id="ed_assistant_carrier",
        caller_user_id=USER_A,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document("ed_assistant_carrier")
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
    )
    await service.save_comments(
        document.id,
        comments=[
            {
                **_comment("edc_carrier", created_at=2.0),
                "kind": "assistant_edit",
            }
        ],
        visible_to=_scoped(USER_A),
    )

    created = await service.save_comment_suggestion_draft(
        document.id,
        "edc_carrier",
        expected_revision=0,
        payload={
            "anchor_text": "Der Ankersatz.",
            "anchor_version": 1,
            "change_summary": [],
            "edit_position": "after",
            "evidence": None,
            "group_id": "editor-suggestion-group-carrier",
            "patch_id": "77777777-7777-4777-8777-777777777777",
            "proposed_text": "Ein eingefuegter Satz.",
            "publication_command_id": "88888888-8888-4888-8888-888888888888",
            "suggestion_id": "editor-suggestion-carrier",
            "warnings": [],
        },
        visible_to=_scoped(USER_A),
    )

    assert created.edit_position == "after"
    assert created.anchor_text == "Der Ankersatz."

    # Servergegenlesung: die Angaben ueberleben den Speicher-Rundlauf,
    # denn genau darauf stuetzt sich der Uebernahmepfad nach einem Neuladen.
    [carrier], _ = await service.list_comments(
        document.id,
        limit=50,
        after=None,
        visible_to=_scoped(USER_A),
    )
    assert carrier.kind == "assistant_edit"
    assert carrier.suggestion_draft is not None
    assert carrier.suggestion_draft.edit_position == "after"
    assert carrier.suggestion_draft.anchor_text == "Der Ankersatz."


@pytest.mark.asyncio
async def test_suggestion_draft_rejects_an_unknown_edit_position(service) -> None:
    """Gegenprobe: die Angabe ist eine geschlossene Menge, kein freier String."""
    await _save_doc(
        service,
        document_id="ed_bad_position",
        caller_user_id=USER_A,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document("ed_bad_position")
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
    )
    await service.save_comments(
        document.id,
        comments=[
            {**_comment("edc_bad", created_at=2.0), "kind": "assistant_edit"}
        ],
        visible_to=_scoped(USER_A),
    )

    with pytest.raises(EditorValidationError):
        await service.save_comment_suggestion_draft(
            document.id,
            "edc_bad",
            expected_revision=0,
            payload={
                "anchor_version": 1,
                "change_summary": [],
                "edit_position": "sideways",
                "evidence": None,
                "group_id": "editor-suggestion-group-bad",
                "patch_id": "99999999-9999-4999-8999-999999999999",
                "proposed_text": "x",
                "publication_command_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "suggestion_id": "editor-suggestion-bad",
                "warnings": [],
            },
            visible_to=_scoped(USER_A),
        )


@pytest.mark.asyncio
async def test_private_suggestion_draft_revision_privacy_and_cleanup(service) -> None:
    """A private AI draft survives reads but never crosses creator scope."""
    await _save_doc(
        service,
        document_id="ed_private_draft",
        caller_user_id=USER_A,
        created_at=1.0,
    )
    store = service.store
    document = await store.get_document("ed_private_draft")
    store._documents[document.id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
    )
    await service.save_comments(
        document.id,
        comments=[
            {
                **_comment("edc_private_ai", created_at=2.0),
                "kind": "inline_edit",
            }
        ],
        visible_to=_scoped(USER_A),
    )

    created = await service.save_comment_suggestion_draft(
        document.id,
        "edc_private_ai",
        expected_revision=0,
        payload={
            "anchor_version": 1,
            "change_summary": ["Tighten the wording."],
            "evidence": None,
            "group_id": "editor-suggestion-group-test",
            "patch_id": "66666666-6666-4666-8666-666666666666",
            "proposed_text": "A clearer private proposal.",
            "publication_command_id": "55555555-5555-4555-8555-555555555555",
            "suggestion_id": "editor-suggestion-test",
            "warnings": [],
        },
        visible_to=_scoped(USER_A),
    )
    assert created.revision == 1
    assert created.proposed_text == "A clearer private proposal."

    [owner_comment], _ = await service.list_comments(
        document.id,
        limit=50,
        after=None,
        visible_to=_scoped(USER_A),
    )
    assert owner_comment.suggestion_draft == created

    with pytest.raises(DocumentNotFound):
        await service.list_comments(
            document.id,
            limit=50,
            after=None,
            visible_to=_scoped(USER_B),
        )

    with pytest.raises(SuggestionDraftRevisionConflict) as stale:
        await service.save_comment_suggestion_draft(
            document.id,
            "edc_private_ai",
            expected_revision=0,
            payload={
                "proposed_text": "A stale overwrite.",
                "revision_source": "manual_edit",
            },
            visible_to=_scoped(USER_A),
        )
    assert stale.value.current_revision == 1

    revised = await service.save_comment_suggestion_draft(
        document.id,
        "edc_private_ai",
        expected_revision=1,
        payload={
            "proposed_text": "The revised private proposal.",
            "revision_source": "manual_edit",
        },
        visible_to=_scoped(USER_A),
    )
    assert revised.revision == 2
    assert revised.revision_history[0].proposed_text == created.proposed_text

    [autosaved] = await service.save_comments(
        document.id,
        comments=[
            {
                **_comment("edc_private_ai", created_at=2.0),
                "comment_markdown": "Updated note without draft payload",
                "kind": "inline_edit",
                "updated_at": 3.0,
            }
        ],
        visible_to=_scoped(USER_A),
    )
    assert autosaved.suggestion_draft == revised

    await service.delete_comment_suggestion_draft(
        document.id,
        "edc_private_ai",
        expected_revision=2,
        patch_id=revised.patch_id,
        visible_to=_scoped(USER_A),
    )
    [cleared], _ = await service.list_comments(
        document.id,
        limit=50,
        after=None,
        visible_to=_scoped(USER_A),
    )
    assert cleared.suggestion_draft is None

    recreated = await service.save_comment_suggestion_draft(
        document.id,
        "edc_private_ai",
        expected_revision=0,
        payload={
            "anchor_version": 1,
            "change_summary": [],
            "evidence": None,
            "group_id": "editor-suggestion-group-cleanup",
            "patch_id": "77777777-7777-4777-8777-777777777777",
            "proposed_text": "Removed with its private note.",
            "publication_command_id": "88888888-8888-4888-8888-888888888888",
            "suggestion_id": "editor-suggestion-cleanup",
            "warnings": [],
        },
        visible_to=_scoped(USER_A),
    )
    assert recreated.revision == 1
    await service.delete_comment(
        document.id,
        "edc_private_ai",
        visible_to=_scoped(USER_A),
    )
    comments, _ = await service.list_comments(
        document.id,
        limit=50,
        after=None,
        visible_to=_scoped(USER_A),
    )
    assert comments == []


@pytest.mark.asyncio
async def test_memory_document_delete_publishes_one_fallback_invalidation() -> None:
    invalidator = AsyncMock()
    service = EditorPersistenceService(
        store=MemoryEditorStore(),
        durable=False,
        invalidator=invalidator,
    )
    await _save_doc(
        service,
        document_id="ed_delete_invalidation",
        caller_user_id=USER,
        created_at=1.0,
    )

    await service.delete_document(
        "ed_delete_invalidation",
        visible_to=_scoped(USER),
    )

    invalidator.revoke_deleted.assert_awaited_once_with(
        tenant_id="default",
        owner_user_id=USER,
        resource_type="editor_document",
        resource_id="ed_delete_invalidation",
        scope="editor_documents",
        actor_user_id=USER,
    )


@pytest.mark.asyncio
async def test_atomic_document_delete_does_not_publish_fallback_effects() -> None:
    class AtomicMemoryEditorStore(MemoryEditorStore):
        @property
        def atomic_delete_resource_effects(self) -> bool:
            return True

    invalidator = AsyncMock()
    service = EditorPersistenceService(
        store=AtomicMemoryEditorStore(),
        durable=False,
        invalidator=invalidator,
    )
    await _save_doc(
        service,
        document_id="ed_atomic_delete",
        caller_user_id=USER,
        created_at=1.0,
    )

    await service.delete_document(
        "ed_atomic_delete",
        visible_to=_scoped(USER),
    )

    invalidator.revoke_deleted.assert_not_awaited()


@pytest.mark.asyncio
async def test_folder_delete_orphans_documents(service) -> None:
    await service.save_folder(
        id="edf_1", title="F", created_at=1.0, updated_at=1.0,
        caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
    )
    await _save_doc(
        service, document_id="ed_1", caller_user_id=USER, created_at=2.0, folder_id="edf_1"
    )
    await service.delete_folder("edf_1", visible_to=_scoped(USER))
    doc = await service.get_document("ed_1", visible_to=_scoped(USER))
    assert doc.folder_id is None
    with pytest.raises(FolderNotFound):
        await service.delete_folder("edf_1", visible_to=_scoped(USER))


@pytest.mark.asyncio
async def test_invalid_source_kind_status_rejected(service) -> None:
    with pytest.raises(EditorValidationError):
        await service.save_document(
            id="ed_1", title="D", content_markdown="", folder_id=None,
            source="bogus", source_run_id=None, revision=1,
            diff_anchor_markdown=None, diff_anchor_updated_at=None,
            created_at=1.0, updated_at=1.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )
    await _save_doc(service, document_id="ed_2", caller_user_id=USER, created_at=1.0)
    with pytest.raises(EditorValidationError):
        await service.save_comments(
            "ed_2",
            comments=[{**_comment("edc_1", created_at=1.0), "kind": "nope"}],
            visible_to=_scoped(USER),
        )
    with pytest.raises(EditorValidationError):
        await service.save_comments(
            "ed_2",
            comments=[{**_comment("edc_2", created_at=1.0), "status": "weird"}],
            visible_to=_scoped(USER),
        )


# -- revision CAS (stored == base) ------------------------------------------ #


@pytest.mark.asyncio
async def test_revision_cas_accepts_base_plus_one_rejects_stale_base(service) -> None:
    """A save writes only when the stored revision is EXACTLY its base.

    `revision` is base+1 (the client tracks its last-synced server revision
    as the base). A stale writer — one whose base is behind the server
    because it never saw a concurrent agent patch or peer edit — fails the
    CAS and gets a 409 to rebase, instead of silently clobbering. The
    forward jump that a monotonic guard would wave through (the
    data-loss shape) is exactly what must conflict.
    """
    from inqtrix.project.editor_ports import DocumentRevisionConflict

    await _save_doc(service, document_id="ed_cas", caller_user_id=USER, created_at=1.0)

    async def save(revision: int, body: str) -> None:
        await service.save_document(
            id="ed_cas", title="Doc", content_markdown=body, folder_id=None,
            source="blank", source_run_id=None, revision=revision,
            diff_anchor_markdown=None, diff_anchor_updated_at=None,
            created_at=1.0, updated_at=2.0,
            caller_user_id=USER, workspace_id=None, visible_to=_scoped(USER),
        )

    # Forward jump: stored is 1, this writer's base is 4 (revision 5) — it
    # never synced the current state. A monotonic guard accepts this, while
    # the compare-and-swap contract rejects it. Content remains untouched.
    with pytest.raises(DocumentRevisionConflict) as excinfo:
        await save(5, "stale writer with a higher counter")
    assert excinfo.value.current_revision == 1
    doc = await service.get_document("ed_cas", visible_to=_scoped(USER))
    assert doc.content_markdown == "body text"
    assert doc.revision == 1

    # base+1 (base == stored == 1): accepted.
    await save(2, "proper next revision")
    doc = await service.get_document("ed_cas", visible_to=_scoped(USER))
    assert doc.revision == 2
    assert doc.content_markdown == "proper next revision"

    # Same-base double write (both based on 1, one already won): rejected.
    with pytest.raises(DocumentRevisionConflict) as excinfo:
        await save(2, "same-base clobber")
    assert excinfo.value.current_revision == 2

    # Rewind (stale writer with a lower base): rejected.
    with pytest.raises(DocumentRevisionConflict):
        await save(1, "stale rewind")

    doc = await service.get_document("ed_cas", visible_to=_scoped(USER))
    assert doc.content_markdown == "proper next revision"
    assert doc.revision == 2


@pytest.mark.asyncio
async def test_document_facts_follow_the_access_rule_of_the_document() -> None:
    """The body-free facts are granted and refused exactly like the document:
    by the owner or a live accepted share in the caller's tenant."""
    from inqtrix.auth.identity_memory import MemoryIdentityStore
    from inqtrix.auth.permissions import AuthorizationService, SharePermission

    identity = MemoryIdentityStore()
    service = EditorPersistenceService(
        store=MemoryEditorStore(),
        authorization=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
    )
    await _save_doc(
        service,
        document_id="ed_facts",
        caller_user_id=USER_A,
        created_at=1.0,
        title="Facts",
        body="HEAVY BODY",
    )
    share = (
        await identity.create_shares(
            tenant_id="default",
            resource_type="editor_document",
            resource_id="ed_facts",
            owner_user_id=USER_A,
            granted_by_user_id=USER_A,
            invitees=[(USER_B, SharePermission.VIEW)],
        )
    )[0]
    await identity.accept_share_by_id(
        tenant_id="default",
        share_id=share.id,
        recipient_user_id=USER_B,
        owner_user_id=USER_A,
    )

    owner_facts = await service.get_document_facts(
        "ed_facts", visible_to=_scoped(USER_A)
    )
    assert (
        owner_facts.id,
        owner_facts.tenant_id,
        owner_facts.created_by_user_id,
        owner_facts.title,
        owner_facts.content_mode,
        owner_facts.collaboration_generation,
        owner_facts.deleted_at,
    ) == ("ed_facts", "default", USER_A, "Facts", "markdown", 0, None)
    assert not hasattr(owner_facts, "content_markdown")
    assert (
        await service.get_document_facts("ed_facts", visible_to=_scoped(USER_B))
    ).id == "ed_facts"
    with pytest.raises(DocumentNotFound):
        await service.get_document_facts(
            "ed_facts",
            visible_to=_scoped(USER_B),
            minimum=SharePermission.EDIT,
        )

    foreign_tenant = UserContext(
        principal=Principal(
            user_id=USER_A,
            kind="oidc_session",
            tenant_id="other-tenant",
            role="member",
        )
    )
    for caller in (_scoped(USER), foreign_tenant):
        with pytest.raises(DocumentNotFound):
            await service.get_document_facts("ed_facts", visible_to=caller)
        with pytest.raises(DocumentNotFound):
            await service.get_document("ed_facts", visible_to=caller)

    await identity.revoke_share_by_id(
        tenant_id="default",
        share_id=share.id,
        revoked_by_user_id=USER_A,
        owner_user_id=USER_A,
    )
    with pytest.raises(DocumentNotFound):
        await service.get_document_facts("ed_facts", visible_to=_scoped(USER_B))
    with pytest.raises(DocumentNotFound):
        await service.get_document_facts("ed_missing", visible_to=_scoped(USER_A))
