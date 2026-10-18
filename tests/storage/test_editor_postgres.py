"""Postgres integration tests for the editor-persistence store (gated).

Same gating/conventions as the other storage suites. Verifies the half of
the :class:`~inqtrix.project.editor_ports.EditorStore` contract only a real
database exercises: the SQL keyset page (tuple comparison + id tiebreaker),
the body-excluding list vs the body-loading get, the ON CONFLICT autosave
upsert, owner/workspace scoping, comment composite-PK isolation, FK cascade
on document delete, and the ON DELETE SET NULL folder orphan. Owner/share
access rules live in the service (covered offline in
``test_editor_persistence.py``).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text

import inqtrix.project.editor_postgres as editor_postgres
from inqtrix.pagination import decode_cursor
from inqtrix.project.editor_postgres import PostgresEditorStore
from inqtrix.project.editor_ports import (
    DocumentNotFound,
    EditorComment,
    EditorSuggestionDraft,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.editor_orm import (
    editor_comments,
    editor_documents,
    editor_folders,
)
from inqtrix.storage.identity_orm import audit_log, resource_shares
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_ID = canonical_user_id("editor-user")
USER_1_ID = canonical_user_id("editor-user-1")
USER_2_ID = canonical_user_id("editor-user-2")
OTHER_USER_ID = canonical_user_id("editor-other-user")


@pytest.fixture(scope="session", autouse=True)
def editor_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def store():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)

    async def reset(*, seed_users: bool) -> None:
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
                await session.execute(
                    user_events.delete().where(
                        user_events.c.resource_type == "editor_document"
                    )
                )
                await session.execute(
                    audit_log.delete().where(
                        audit_log.c.resource_type == "editor_document"
                    )
                )
                await session.execute(editor_comments.delete())
                await session.execute(
                    resource_shares.delete().where(
                        resource_shares.c.resource_type == "editor_document"
                    )
                )
                await session.execute(editor_documents.delete())
                await session.execute(editor_folders.delete())
                if seed_users:
                    await ensure_canonical_users(
                        session,
                        (USER_ID, USER_1_ID, USER_2_ID, OTHER_USER_ID),
                    )

    await reset(seed_users=True)
    editor_store = PostgresEditorStore(engine=engine, app_role=APP_ROLE)
    try:
        yield editor_store
    finally:
        # Leave the disposable shared integration database as clean as this
        # fixture found it.  A setup-only wipe leaks the final test's document
        # into the next module, where its RESTRICT owner FK correctly prevents
        # identity-fixture cleanup and makes the suite order-dependent.
        try:
            await reset(seed_users=False)
        finally:
            await editor_store.aclose()


async def _save_doc(
    store,
    document_id,
    *,
    owner: uuid.UUID = USER_ID,
    workspace=None,
    created_at=1.0,
    body="body",
    folder_id=None,
):
    return await store.upsert_document(
        id=document_id, title="D", content_markdown=body, folder_id=folder_id,
        source="blank", source_run_id=None, revision=1,
        diff_anchor_markdown=None, diff_anchor_updated_at=None,
        created_at=created_at, updated_at=created_at,
        created_by_user_id=owner, workspace_id=workspace,
    )


def _comment(
    comment_id,
    document_id,
    *,
    created_at=1.0,
    body="c",
    author: uuid.UUID | None = USER_ID,
):
    return EditorComment(
        id=comment_id, document_id=document_id, comment_markdown=body,
        anchor={"from": 0, "to": 1, "selectedText": "x"},
        kind="collect", status="open", created_at=created_at, updated_at=created_at,
        created_by_user_id=author,
    )


async def _upsert_comments(
    store: PostgresEditorStore,
    document_id: str,
    comments: list[EditorComment],
    *,
    actor: uuid.UUID = USER_ID,
    owner: uuid.UUID = USER_ID,
    workspace: str | None = None,
    content_mode: str = "markdown",
) -> list[EditorComment]:
    return await store.upsert_comments(
        comments,
        expected_document_id=document_id,
        expected_document_owner_user_id=owner,
        expected_document_workspace_id=workspace,
        expected_document_content_mode=content_mode,
        actor_user_id=actor,
    )


@pytest.mark.asyncio
async def test_patch_document_metadata_bumps_revision_via_sql_update(store) -> None:
    """The metadata PATCH executes its SQL UPDATE end-to-end.

    Pins the statement construction itself: a missing SQLAlchemy ``update``
    import turns collaboration-metadata autosave into a ``NameError`` 500.
    Only the real Postgres store executes this path, so the gated suite must
    exercise it.
    """
    document_id = "doc-patch-metadata"
    await _save_doc(store, document_id)

    patched = await store.patch_document_metadata(
        document_id=document_id,
        expected_metadata_revision=1,
        title="Renamed",
        folder_id=None,
        set_folder_id=False,
        diff_anchor_markdown=None,
        set_diff_anchor_markdown=False,
        diff_anchor_updated_at=None,
        set_diff_anchor_updated_at=False,
        updated_at=2.0,
        scope=ResourceScope(created_by_user_id=USER_ID, workspace_id=None),
    )

    assert patched.title == "Renamed"
    assert patched.metadata_revision == 2
    assert patched.updated_at == 2.0


@pytest.mark.asyncio
async def test_agent_artifact_source_passes_the_check_constraint(store) -> None:
    """Migration 0032: the export source is accepted at the DB level."""
    saved = await store.upsert_document(
        id="ed_agent",
        title="Agent-Memo",
        content_markdown="# Memo",
        folder_id=None,
        source="agent-artifact",
        source_run_id="run_x",
        revision=1,
        diff_anchor_markdown=None,
        diff_anchor_updated_at=None,
        created_at=1.0,
        updated_at=1.0,
        created_by_user_id=USER_ID,
        workspace_id=None,
    )
    assert saved.source == "agent-artifact"
    assert saved.source_run_id == "run_x"


@pytest.mark.asyncio
async def test_document_list_excludes_body_get_includes_it(store) -> None:
    await _save_doc(
        store,
        "ed_1",
        owner=USER_ID,
        created_at=1.0,
        body="HEAVY BODY",
    )
    page, _ = await store.list_documents_page(
        created_by_user_id=USER_ID, workspace_id=None, limit=50, after=None
    )
    assert page[0].content_markdown == ""  # list never transfers the body
    full = await store.get_document("ed_1")
    assert full.content_markdown == "HEAVY BODY"


@pytest.mark.asyncio
async def test_document_facts_match_the_document_without_reading_any_body(
    store,
) -> None:
    """The facts read returns what ``get_document`` returns for identity,
    ownership and mode, omits deleted and foreign-tenant rows, and never
    selects a Markdown body column."""
    await _save_doc(store, "ed_live", owner=USER_ID, body="HEAVY BODY")
    await _save_doc(store, "ed_deleted", owner=USER_ID, body="GONE")
    async with store._engine.begin() as connection:
        await connection.execute(
            editor_documents.update()
            .where(editor_documents.c.id == "ed_deleted")
            .values(deleted_at=5.0)
        )
        await connection.execute(
            editor_documents.insert().values(
                id="ed_foreign",
                tenant_id="other-tenant",
                title="Foreign",
                content_markdown="FOREIGN BODY",
                source="blank",
                revision=1,
                created_at=1.0,
                updated_at=1.0,
            )
        )
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine.sync_engine, "before_cursor_execute", record)
    try:
        facts = await store.get_document_facts(
            ["ed_live", "ed_deleted", "ed_foreign", "ed_missing"]
        )
    finally:
        event.remove(store._engine.sync_engine, "before_cursor_execute", record)

    full = await store.get_document("ed_live")
    assert set(facts) == {"ed_live"}
    live = facts["ed_live"]
    assert (
        live.id,
        live.tenant_id,
        live.created_by_user_id,
        live.title,
        live.content_mode,
        live.collaboration_generation,
        live.deleted_at,
    ) == (
        full.id,
        full.tenant_id,
        full.created_by_user_id,
        full.title,
        full.content_mode,
        full.collaboration_generation,
        full.deleted_at,
    )
    reads = [
        statement for statement in statements if "editor_documents" in statement
    ]
    assert len(reads) == 1
    assert "content_markdown" not in reads[0]
    assert "diff_anchor_markdown" not in reads[0]
    assert await store.get_document_facts([]) == {}


@pytest.mark.asyncio
async def test_document_keyset_walks_with_tiebreaker_and_scopes(store) -> None:
    for n, stamp in enumerate([10.0, 10.0, 20.0, 30.0, 30.0]):
        await _save_doc(
            store,
            f"ed_{n}",
            owner=USER_1_ID,
            workspace="w1",
            created_at=stamp,
        )
    await _save_doc(
        store,
        "ed_other",
        owner=USER_2_ID,
        workspace="w1",
        created_at=5.0,
    )

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page, next_cursor = await store.list_documents_page(
            created_by_user_id=USER_1_ID,
            workspace_id="w1",
            limit=2,
            after=cursor,
        )
        seen.extend(d.id for d in page)
        if next_cursor is None:
            break
        cursor = decode_cursor(next_cursor)
    assert len(seen) == len(set(seen)) == 5
    assert "ed_other" not in seen  # owner-scoped


@pytest.mark.asyncio
async def test_upsert_preserves_created_at_and_owner(store) -> None:
    await _save_doc(
        store,
        "ed_1",
        owner=USER_1_ID,
        created_at=100.0,
        body="v1",
    )
    # base+1 (stored is 1) — a normal next save; created_at/owner survive.
    await store.upsert_document(
        id="ed_1", title="second", content_markdown="v2", folder_id=None,
        source="pasted", source_run_id=None, revision=2,
        diff_anchor_markdown=None, diff_anchor_updated_at=None,
        created_at=999.0, updated_at=200.0,
        created_by_user_id=USER_1_ID, workspace_id=None,
    )
    doc = await store.get_document("ed_1")
    assert doc.content_markdown == "v2"
    assert doc.revision == 2
    assert doc.created_at == 100.0
    assert doc.created_by_user_id == USER_1_ID


@pytest.mark.asyncio
async def test_cross_owner_document_id_collision_is_not_found(store) -> None:
    await _save_doc(
        store, "ed_1", owner=USER_1_ID, created_at=100.0, body="Alice"
    )

    with pytest.raises(DocumentNotFound):
        await store.upsert_document(
            id="ed_1", title="hijack", content_markdown="Bob", folder_id=None,
            source="blank", source_run_id=None, revision=2,
            diff_anchor_markdown=None, diff_anchor_updated_at=None,
            created_at=999.0, updated_at=200.0,
            created_by_user_id=OTHER_USER_ID, workspace_id=None,
        )

    assert (await store.get_document("ed_1")).content_markdown == "Alice"


@pytest.mark.asyncio
async def test_comment_composite_pk_isolation_and_cascade(store) -> None:
    await _save_doc(store, "ed_a", owner=USER_ID, created_at=1.0)
    await _save_doc(store, "ed_b", owner=USER_ID, created_at=1.0)
    await _upsert_comments(
        store, "ed_a", [_comment("edc_dup", "ed_a", body="A-text")]
    )
    await _upsert_comments(
        store, "ed_b", [_comment("edc_dup", "ed_b", body="B-text")]
    )
    a_page, _ = await store.list_comments_page("ed_a", limit=50, after=None)
    assert next(c for c in a_page if c.id == "edc_dup").comment_markdown == "A-text"
    b_page, _ = await store.list_comments_page("ed_b", limit=50, after=None)
    assert next(c for c in b_page if c.id == "edc_dup").comment_markdown == "B-text"
    # Deleting the document cascades its comments.
    await store.delete_document(
        "ed_a",
        scope=ResourceScope.from_record(await store.get_document("ed_a")),
    )
    gone, _ = await store.list_comments_page("ed_a", limit=50, after=None)
    assert gone == []


@pytest.mark.asyncio
async def test_document_delete_commits_audit_and_user_invalidations(store) -> None:
    document_id = "ed_delete_effects"
    await _save_doc(store, document_id, owner=USER_ID)
    share_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with store._session_factory() as session:
        async with session.begin():
            await session.execute(
                resource_shares.insert().values(
                    id=share_id,
                    tenant_id="default",
                    recipient_user_id=USER_1_ID,
                    resource_type="editor_document",
                    resource_id=document_id,
                    permission="view",
                    revision=1,
                    granted_by_user_id=USER_ID,
                    created_at=now,
                    accepted_at=now,
                )
            )

    await store.delete_document(
        document_id,
        scope=ResourceScope(
            created_by_user_id=USER_ID,
            workspace_id=None,
        ),
    )

    assert store.atomic_delete_resource_effects is True
    with pytest.raises(DocumentNotFound):
        await store.get_document(document_id)
    async with store._session_factory() as session:
        events = (
            await session.execute(
                select(
                    user_events.c.target_user_id,
                    user_events.c.scope,
                    user_events.c.resource_type,
                    user_events.c.resource_id,
                )
                .where(user_events.c.resource_id == document_id)
                .order_by(user_events.c.target_user_id)
            )
        ).all()
        audits = (
            await session.execute(
                select(
                    audit_log.c.actor_user_id,
                    audit_log.c.action,
                    audit_log.c.resource_type,
                    audit_log.c.resource_id,
                ).where(audit_log.c.resource_id == document_id)
            )
        ).all()
        shares = (
            await session.execute(
                select(
                    resource_shares.c.id,
                    resource_shares.c.revoked_at,
                    resource_shares.c.revoked_by_user_id,
                ).where(
                    resource_shares.c.id == share_id
                )
            )
        ).all()

    assert {
        (
            event.target_user_id,
            event.scope,
            event.resource_type,
            event.resource_id,
        )
        for event in events
    } == {
        (
            USER_ID,
            "editor_documents",
            "editor_document",
            document_id,
        ),
        (
            USER_1_ID,
            "editor_documents",
            "editor_document",
            document_id,
        ),
    }
    assert [
        (
            audit.actor_user_id,
            audit.action,
            audit.resource_type,
            audit.resource_id,
        )
        for audit in audits
    ] == [
        (
            USER_ID,
            "editor_document.deleted",
            "editor_document",
            document_id,
        )
    ]
    assert len(shares) == 1
    assert shares[0].id == share_id
    assert shares[0].revoked_at is not None
    assert shares[0].revoked_by_user_id == USER_ID


@pytest.mark.asyncio
async def test_document_delete_rolls_back_when_resource_effects_fail(
    store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = "ed_delete_effects_rollback"
    await _save_doc(store, document_id, owner=USER_ID)
    append_resource_effects = editor_postgres.append_resource_effects

    async def append_then_fail(*args, **kwargs) -> None:
        await append_resource_effects(*args, **kwargs)
        raise RuntimeError("forced resource-effect failure")

    monkeypatch.setattr(
        editor_postgres,
        "append_resource_effects",
        append_then_fail,
    )

    with pytest.raises(RuntimeError, match="forced resource-effect failure"):
        await store.delete_document(
            document_id,
            scope=ResourceScope(
                created_by_user_id=USER_ID,
                workspace_id=None,
            ),
        )

    assert (await store.get_document(document_id)).id == document_id
    async with store._session_factory() as session:
        event_count = (
            await session.execute(
                select(user_events.c.id).where(
                    user_events.c.resource_id == document_id
                )
            )
        ).all()
        audit_count = (
            await session.execute(
                select(audit_log.c.id).where(
                    audit_log.c.resource_id == document_id
                )
            )
        ).all()
    assert event_count == []
    assert audit_count == []


@pytest.mark.asyncio
async def test_comment_upsert_atomically_rejects_foreign_author(store) -> None:
    """A conflicting private comment owner rolls back the entire batch."""
    await _save_doc(store, "ed_private_comments", owner=USER_ID)
    await _upsert_comments(
        store,
        "ed_private_comments",
        [
            _comment(
                "edc_owned",
                "ed_private_comments",
                body="owner text",
                author=USER_ID,
            )
        ],
    )

    from inqtrix.project.editor_ports import DocumentNotFound

    with pytest.raises(DocumentNotFound):
        await _upsert_comments(
            store,
            "ed_private_comments",
            [
                _comment(
                    "edc_owned",
                    "ed_private_comments",
                    body="foreign overwrite",
                    author=USER_2_ID,
                ),
                _comment(
                    "edc_new",
                    "ed_private_comments",
                    body="must roll back",
                    author=USER_2_ID,
                ),
            ],
            actor=USER_ID,
        )

    comments, _ = await store.list_comments_page(
        "ed_private_comments", limit=50, after=None
    )
    assert [(comment.id, comment.comment_markdown) for comment in comments] == [
        ("edc_owned", "owner text")
    ]


@pytest.mark.asyncio
async def test_stale_document_scope_cannot_comment_after_id_reuse(store) -> None:
    await _save_doc(store, "ed_reused", owner=USER_1_ID, created_at=1.0)
    await store.delete_document(
        "ed_reused",
        scope=ResourceScope.from_record(await store.get_document("ed_reused")),
    )
    await _save_doc(store, "ed_reused", owner=USER_2_ID, created_at=2.0)
    comment = _comment(
        "edc_stale",
        "ed_reused",
        body="must not cross owners",
        author=USER_1_ID,
    )

    with pytest.raises(DocumentNotFound):
        await _upsert_comments(
            store,
            "ed_reused",
            [comment],
            actor=USER_1_ID,
            owner=USER_1_ID,
        )

    comments, _ = await store.list_comments_page(
        "ed_reused", limit=50, after=None
    )
    assert comments == []


@pytest.mark.asyncio
async def test_comment_write_revalidates_live_share_inside_store_transaction(
    store,
) -> None:
    await _save_doc(store, "ed_shared", owner=USER_ID, created_at=1.0)
    share_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with store._session_factory() as session:
        async with session.begin():
            await session.execute(
                resource_shares.insert().values(
                    id=share_id,
                    tenant_id="default",
                    recipient_user_id=USER_1_ID,
                    resource_type="editor_document",
                    resource_id="ed_shared",
                    permission="edit",
                    revision=1,
                    granted_by_user_id=USER_ID,
                    created_at=now,
                    accepted_at=now,
                )
            )

    await _upsert_comments(
        store,
        "ed_shared",
        [
            _comment(
                "edc_shared",
                "ed_shared",
                body="accepted",
                author=USER_1_ID,
            )
        ],
        actor=USER_1_ID,
        owner=USER_ID,
    )

    async with store._session_factory() as session:
        async with session.begin():
            await session.execute(
                resource_shares.update()
                .where(resource_shares.c.id == share_id)
                .values(revoked_at=datetime.now(timezone.utc))
            )

    with pytest.raises(DocumentNotFound):
        await _upsert_comments(
            store,
            "ed_shared",
            [
                _comment(
                    "edc_after_revoke",
                    "ed_shared",
                    body="must be rejected",
                    author=USER_1_ID,
                )
            ],
            actor=USER_1_ID,
            owner=USER_ID,
        )


@pytest.mark.asyncio
async def test_discarding_a_private_suggestion_draft_clears_it_to_sql_null(
    store,
) -> None:
    """A discarded private AI draft is gone, and a new one can start.

    The column is JSONB: a Python ``None`` bound as an UPDATE value is
    serialized as the JSON ``null`` literal, which the object-only CHECK
    constraint rejects, so the discard fails and the draft stays. The
    cleared state is SQL NULL, the value of a comment that never had a
    draft, and the revision-0 guard of the next draft depends on it.
    """
    document_id = "ed_shared_private_draft"
    comment_id = "edc_private_draft"
    await _save_doc(store, document_id, owner=USER_ID)
    now = datetime.now(timezone.utc)
    async with store._session_factory() as session:
        async with session.begin():
            await session.execute(
                resource_shares.insert().values(
                    id=uuid.uuid4(),
                    tenant_id="default",
                    recipient_user_id=USER_1_ID,
                    resource_type="editor_document",
                    resource_id=document_id,
                    permission="edit",
                    revision=1,
                    granted_by_user_id=USER_ID,
                    created_at=now,
                    accepted_at=now,
                )
            )
    await _upsert_comments(
        store,
        document_id,
        [_comment(comment_id, document_id, author=USER_1_ID)],
        actor=USER_1_ID,
        owner=USER_ID,
    )
    authority = {
        "expected_document_owner_user_id": USER_ID,
        "expected_document_workspace_id": None,
        "expected_document_content_mode": "markdown",
        "actor_user_id": USER_1_ID,
    }
    draft = EditorSuggestionDraft(
        suggestion_id="editor-suggestion-discard",
        group_id="editor-suggestion-group-discard",
        patch_id=str(uuid.uuid4()),
        publication_command_id=str(uuid.uuid4()),
        proposed_text="Private provider output",
        created_at=1.0,
        updated_at=1.0,
    )
    stored = await store.save_comment_suggestion_draft(
        document_id=document_id,
        comment_id=comment_id,
        draft=draft,
        expected_revision=0,
        **authority,
    )
    assert stored.revision == 1

    await store.delete_comment_suggestion_draft(
        document_id=document_id,
        comment_id=comment_id,
        expected_revision=1,
        patch_id=draft.patch_id,
        **authority,
    )

    async with store._session_factory() as session:
        cleared = (
            await session.execute(
                select(
                    editor_comments.c.suggestion_draft.is_(None),
                    func.jsonb_typeof(editor_comments.c.suggestion_draft),
                ).where(
                    editor_comments.c.document_id == document_id,
                    editor_comments.c.id == comment_id,
                )
            )
        ).one()
    assert tuple(cleared) == (True, None)
    comment = await store.get_comment(
        document_id, comment_id, created_by_user_id=USER_1_ID
    )
    assert comment.suggestion_draft is None

    restarted = await store.save_comment_suggestion_draft(
        document_id=document_id,
        comment_id=comment_id,
        draft=replace(
            draft,
            patch_id=str(uuid.uuid4()),
            publication_command_id=str(uuid.uuid4()),
        ),
        expected_revision=0,
        **authority,
    )
    assert restarted.revision == 1
    assert restarted.patch_id != draft.patch_id


@pytest.mark.asyncio
async def test_only_its_creator_discards_a_private_suggestion_draft(
    store,
) -> None:
    """A private AI draft in a shared document is discarded by its creator
    only. The owner and another editor of the document, who may both write
    comments there, hold the comment id and the exact revision and patch
    guard, and still get the answer for a missing draft: not a revision
    conflict, which would reveal that a draft exists and how often it was
    revised. The draft stays. Once the creator's share is revoked, the
    creator can no longer discard it either."""
    document_id = "ed_shared_draft_scope"
    comment_id = "edc_draft_scope"
    await _save_doc(store, document_id, owner=USER_ID)
    now = datetime.now(timezone.utc)
    shares = {USER_1_ID: uuid.uuid4(), USER_2_ID: uuid.uuid4()}
    async with store._session_factory() as session:
        async with session.begin():
            for recipient, share_id in shares.items():
                await session.execute(
                    resource_shares.insert().values(
                        id=share_id,
                        tenant_id="default",
                        recipient_user_id=recipient,
                        resource_type="editor_document",
                        resource_id=document_id,
                        permission="edit",
                        revision=1,
                        granted_by_user_id=USER_ID,
                        created_at=now,
                        accepted_at=now,
                    )
                )
    await _upsert_comments(
        store,
        document_id,
        [_comment(comment_id, document_id, author=USER_1_ID)],
        actor=USER_1_ID,
        owner=USER_ID,
    )

    def authority(actor: uuid.UUID) -> dict:
        return {
            "expected_document_owner_user_id": USER_ID,
            "expected_document_workspace_id": None,
            "expected_document_content_mode": "markdown",
            "actor_user_id": actor,
        }

    draft = EditorSuggestionDraft(
        suggestion_id="editor-suggestion-scope",
        group_id="editor-suggestion-group-scope",
        patch_id=str(uuid.uuid4()),
        publication_command_id=str(uuid.uuid4()),
        proposed_text="Private provider output of the creator",
        created_at=1.0,
        updated_at=1.0,
    )
    await store.save_comment_suggestion_draft(
        document_id=document_id,
        comment_id=comment_id,
        draft=draft,
        expected_revision=0,
        **authority(USER_1_ID),
    )

    async def draft_revision() -> int | None:
        async with store._session_factory() as session:
            stored = (
                await session.execute(
                    select(editor_comments.c.suggestion_draft).where(
                        editor_comments.c.document_id == document_id,
                        editor_comments.c.id == comment_id,
                    )
                )
            ).scalar_one()
        return None if stored is None else stored["revision"]

    for intruder in (USER_ID, USER_2_ID):
        with pytest.raises(editor_postgres.SuggestionDraftNotFound):
            await store.delete_comment_suggestion_draft(
                document_id=document_id,
                comment_id=comment_id,
                expected_revision=1,
                patch_id=draft.patch_id,
                **authority(intruder),
            )
        assert await draft_revision() == 1

    async with store._session_factory() as session:
        async with session.begin():
            await session.execute(
                resource_shares.update()
                .where(resource_shares.c.id == shares[USER_1_ID])
                .values(revoked_at=datetime.now(timezone.utc))
            )
    with pytest.raises(DocumentNotFound):
        await store.delete_comment_suggestion_draft(
            document_id=document_id,
            comment_id=comment_id,
            expected_revision=1,
            patch_id=draft.patch_id,
            **authority(USER_1_ID),
        )
    assert await draft_revision() == 1


@pytest.mark.asyncio
async def test_folder_delete_orphans_documents(store) -> None:
    folder = await store.upsert_folder(
        id="edf_1", title="F", created_at=1.0, updated_at=1.0,
        created_by_user_id=USER_ID, workspace_id=None,
    )
    await _save_doc(
        store,
        "ed_1",
        owner=USER_ID,
        created_at=2.0,
        folder_id="edf_1",
    )
    await store.delete_folder(
        "edf_1",
        scope=ResourceScope.from_record(folder),
    )
    doc = await store.get_document("ed_1")
    assert doc.folder_id is None


@pytest.mark.asyncio
async def test_revision_cas_on_conflict_where(store) -> None:
    """The ON CONFLICT WHERE is an exact CAS (stored == base).

    A forward jump — a stale writer whose base is behind the server — is the
    data-loss shape and must 409, not pass. Only base+1 (stored == base)
    writes.
    """
    from inqtrix.project.editor_ports import DocumentRevisionConflict

    await _save_doc(store, "ed_cas", body="first")  # stored revision 1

    async def save(revision: int, body: str):
        return await store.upsert_document(
            id="ed_cas", title="D", content_markdown=body, folder_id=None,
            source="blank", source_run_id=None, revision=revision,
            diff_anchor_markdown=None, diff_anchor_updated_at=None,
            created_at=1.0, updated_at=2.0,
            created_by_user_id=USER_ID, workspace_id=None,
        )

    # Forward jump (base 4 over stored 1): the monotonic guard passed this;
    # the CAS suppresses it via the ON CONFLICT WHERE. Content untouched.
    with pytest.raises(DocumentRevisionConflict) as excinfo:
        await save(5, "stale higher counter")
    assert excinfo.value.current_revision == 1
    unchanged = await store.get_document("ed_cas")
    assert unchanged.content_markdown == "first"
    assert unchanged.revision == 1
    # base+1 (stored == 1): accepted.
    doc = await save(2, "next revision")
    assert doc.revision == 2
    # Same-base double write: suppressed.
    with pytest.raises(DocumentRevisionConflict) as excinfo:
        await save(2, "clobber")
    assert excinfo.value.current_revision == 2
    # Rewind: suppressed too.
    with pytest.raises(DocumentRevisionConflict):
        await save(1, "rewind")
    fresh = await store.get_document("ed_cas")
    assert fresh.content_markdown == "next revision"
    assert fresh.revision == 2
    # The brand-new-id INSERT branch stays unaffected by the guard.
    created = await _save_doc(store, "ed_cas_new", body="fresh")
    assert created.revision == 1
