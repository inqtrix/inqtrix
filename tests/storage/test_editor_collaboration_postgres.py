"""PostgreSQL integration contracts for editor live collaboration.

The suite is deliberately gated on ``INQTRIX_TEST_DATABASE_URL`` and runs
the production store under the restricted application role. It covers the
transactional guarantees that memory fakes cannot prove: conversion CAS,
serialized sequence allocation, lease and instance fencing, patch metadata
co-commit, snapshot/projection consistency, retention, tombstones, and RLS.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from dataclasses import dataclass, replace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

import inqtrix.storage.editor_collaboration_postgres as collaboration_postgres
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.project.editor_collaboration_ports import (
    CollaborationCommentThread,
    CollaborationConflict,
    CollaborationDecision,
    CollaborationDocumentNotFound,
    CollaborationInstanceFenced,
    CollaborationInstanceLease,
    CollaborationLease,
    CollaborationLeaseInvalid,
    CollaborationPatchState,
    CollaborationPermission,
    CollaborationRateLimited,
    CollaborationSnapshot,
    CollaborationSuggestion,
    PersistCollaborationUpdate,
    PersistedCollaborationUpdate,
)
from inqtrix.project.editor_guest_links import (
    EditorDocumentShareLink,
    EditorGuestAccess,
    EditorGuestIdentity,
    EditorGuestLinkExpired,
)
from inqtrix.storage.auth_orm import auth_sessions
from inqtrix.storage.db import (
    build_engine,
    build_session_factory,
    tenant_session,
)
from inqtrix.storage.editor_collaboration_orm import (
    editor_collaboration_instances,
    editor_collaboration_leases,
    editor_collaboration_snapshots,
    editor_collaboration_updates,
)
from inqtrix.storage.editor_collaboration_postgres import (
    PostgresEditorCollaborationStore,
)
from inqtrix.storage.editor_guest_link_orm import (
    editor_document_guest_identities,
    editor_document_share_links,
)
from inqtrix.storage.editor_guest_link_postgres import (
    PostgresEditorGuestLinkStore,
)
from inqtrix.storage.editor_orm import (
    editor_comments,
    editor_documents,
    editor_folders,
)
from inqtrix.storage.editor_patch_orm import editor_patches
from inqtrix.storage.identity_orm import (
    audit_log,
    resource_shares,
    users,
    workspace_members,
    workspaces,
)
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
TENANT_A = "collaboration-postgres-a"
TENANT_B = "collaboration-postgres-b"
TENANTS = (TENANT_A, TENANT_B)
OWNER_A = canonical_user_id("collaboration-postgres-owner-a")
OWNER_B = canonical_user_id("collaboration-postgres-owner-b")
REVIEWER_A = canonical_user_id("collaboration-postgres-reviewer-a")
DOCUMENT_REVISION = 7
METADATA_REVISION = 3
SCHEMA_VERSION = 1
SCHEMA_HASH = hashlib.sha256(b"inqtrix-editor-schema-v1").hexdigest()


@dataclass(frozen=True)
class _DatabaseHarness:
    """Real database resources and a deterministic time origin for one test."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    store: PostgresEditorCollaborationStore
    now: float


@pytest.fixture(scope="session", autouse=True)
def collaboration_schema_migrated() -> Iterator[None]:
    """Apply the real migration chain once when the integration DB is enabled."""
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


async def _wipe_test_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Remove only rows owned by this module's reserved test tenants."""
    async with session_factory() as session:
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
            for table in (
                editor_collaboration_leases,
                editor_collaboration_updates,
                editor_collaboration_snapshots,
                editor_collaboration_instances,
                editor_patches,
                editor_comments,
                resource_shares,
                auth_sessions,
                editor_documents,
                editor_folders,
                user_events,
                audit_log,
            ):
                await session.execute(
                    table.delete().where(table.c.tenant_id.in_(TENANTS))
                )
            await ensure_canonical_users(
                session,
                (OWNER_A, REVIEWER_A),
                tenant_id=TENANT_A,
            )
            await ensure_canonical_users(
                session,
                (OWNER_B,),
                tenant_id=TENANT_B,
            )


@pytest_asyncio.fixture()
async def database() -> AsyncIterator[_DatabaseHarness]:
    """Yield the collaboration store over a clean, migrated real database."""
    engine = build_engine(TEST_DATABASE_URL)
    session_factory = build_session_factory(engine)
    await _wipe_test_tenants(session_factory)
    harness = _DatabaseHarness(
        engine=engine,
        session_factory=session_factory,
        store=PostgresEditorCollaborationStore(
            session_factory=session_factory,
            app_role=APP_ROLE,
            restrict_to_workspace_members=False,
        ),
        now=time.time(),
    )
    try:
        yield harness
    finally:
        await _wipe_test_tenants(session_factory)
        await engine.dispose()


def _sha256(payload: bytes) -> str:
    """Return the lowercase digest used by collaboration persistence."""
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_current_policy_cursor_is_tenant_scoped(
    database: _DatabaseHarness,
) -> None:
    """Each tenant sees only its greatest committed content-free event ID."""
    async with database.session_factory() as session:
        async with session.begin():
            tenant_b_cursor = (
                await session.execute(
                    insert(user_events)
                    .values(
                        tenant_id=TENANT_B,
                        target_user_id=OWNER_B,
                        scope="share:accepted",
                        resource_type="editor_document",
                        resource_id="ed_policy_b",
                    )
                    .returning(user_events.c.id)
                )
            ).scalar_one()
            tenant_a_cursor = (
                await session.execute(
                    insert(user_events)
                    .values(
                        tenant_id=TENANT_A,
                        target_user_id=OWNER_A,
                        scope="share:accepted",
                        resource_type="editor_document",
                        resource_id="ed_policy_a",
                    )
                    .returning(user_events.c.id)
                )
            ).scalar_one()

    assert await database.store.current_policy_cursor(
        tenant_id=TENANT_A
    ) == int(tenant_a_cursor)
    assert await database.store.current_policy_cursor(
        tenant_id=TENANT_B
    ) == int(tenant_b_cursor)


def _snapshot(
    *,
    tenant_id: str,
    document_id: str,
    sequence: int,
    state_update: bytes,
    projection: str,
    created_at: float,
) -> CollaborationSnapshot:
    """Build a hash-consistent snapshot accepted by the persistence port."""
    return CollaborationSnapshot(
        document_id=document_id,
        tenant_id=tenant_id,
        generation=1,
        covered_sequence=sequence,
        state_update=state_update,
        state_vector=f"vector-{sequence}".encode(),
        state_hash=_sha256(state_update),
        projection_hash=_sha256(projection.encode()),
        schema_version=SCHEMA_VERSION,
        schema_hash=SCHEMA_HASH,
        created_at=created_at,
    )


async def _seed_markdown_document(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    owner_user_id: uuid.UUID,
    markdown: str = "# Initial\n\nBody.",
) -> None:
    """Insert one legacy Markdown document through the bypass test connection."""
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_documents).values(
                    id=document_id,
                    tenant_id=tenant_id,
                    created_by_user_id=owner_user_id,
                    workspace_id=None,
                    title="Collaboration test",
                    content_markdown=markdown,
                    folder_id=None,
                    source="blank",
                    source_run_id=None,
                    revision=DOCUMENT_REVISION,
                    content_mode="markdown",
                    metadata_revision=METADATA_REVISION,
                    collaboration_generation=0,
                    collaboration_schema_version=None,
                    collaboration_schema_hash=None,
                    persisted_sequence=0,
                    projection_sequence=0,
                    projection_updated_at=None,
                    deleted_at=None,
                    diff_anchor_markdown=None,
                    diff_anchor_updated_at=None,
                    created_at=database.now,
                    updated_at=database.now,
                )
            )


async def _activate_document(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    owner_user_id: uuid.UUID,
    projection: str = "# Initial\n\nBody.",
) -> None:
    """Convert a seeded Markdown document with a valid generation-one snapshot."""
    snapshot = _snapshot(
        tenant_id=tenant_id,
        document_id=document_id,
        sequence=0,
        state_update=f"initial:{tenant_id}:{document_id}".encode(),
        projection=projection,
        created_at=database.now,
    )
    await database.store.enable_document(
        tenant_id=tenant_id,
        document_id=document_id,
        owner_user_id=owner_user_id,
        expected_revision=DOCUMENT_REVISION,
        expected_metadata_revision=METADATA_REVISION,
        schema_version=SCHEMA_VERSION,
        schema_hash=SCHEMA_HASH,
        snapshot=snapshot,
        projection_markdown=projection,
        now=database.now,
    )


async def _seed_browser_session(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    session_id: str,
) -> None:
    """Insert one live cookie session for lease authorization checks."""
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(auth_sessions).values(
                    id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    issuer="https://collaboration-tests.example",
                    subject=f"subject-{user_id.hex}",
                    email=f"{user_id.hex}@example.com",
                    display_name="Collaboration Tester",
                    groups=[],
                    csrf_random="ab" * 16,
                    created_at=database.now,
                    expires_at=database.now + 10_000.0,
                )
            )


async def _authorize_writer(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    user_id: uuid.UUID,
    permission: CollaborationPermission = "edit",
    instance_id: str = "node-primary",
    instance_lease_seconds: float = 5_000.0,
) -> tuple[CollaborationInstanceLease, CollaborationLease]:
    """Acquire the writer epoch and persist one live browser lease."""
    session_id = f"session:{tenant_id}:{document_id}:{user_id.hex}"
    await _seed_browser_session(
        database,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    instance = await database.store.acquire_instance(
        tenant_id=tenant_id,
        instance_id=instance_id,
        now=database.now,
        lease_seconds=instance_lease_seconds,
    )
    lease_id = uuid.uuid4()
    lease = CollaborationLease(
        lease_id=lease_id,
        token_hash=_sha256(f"token:{lease_id}".encode()),
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        user_id=user_id,
        permission=permission,
        session_id=session_id,
        issued_at=database.now,
        expires_at=database.now + 3_600.0,
        last_validated_at=database.now,
    )
    await database.store.issue_lease(
        lease,
        max_active=5,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    return instance, lease


def _direct_update(
    *,
    tenant_id: str,
    document_id: str,
    actor_user_id: uuid.UUID,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    payload: bytes,
    now: float,
    expected_sequence: int | None = None,
) -> PersistCollaborationUpdate:
    """Build one ordinary human edit update."""
    return PersistCollaborationUpdate(
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=lease.lease_id,
        actor_user_id=actor_user_id,
        update_hash=_sha256(payload),
        update_bytes=payload,
        actor_kind="human",
        change_kind="direct",
        expected_sequence=expected_sequence,
        now=now,
    )


def _suggestion_update(
    *,
    tenant_id: str,
    document_id: str,
    actor_user_id: uuid.UUID,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    payload: bytes,
    suggestions: tuple[CollaborationSuggestion, ...],
    patches: tuple[CollaborationPatchState, ...],
    now: float,
    expected_sequence: int | None = None,
) -> PersistCollaborationUpdate:
    """Build one human suggestion update with sidecar-validated descriptors."""
    return PersistCollaborationUpdate(
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=lease.lease_id,
        actor_user_id=actor_user_id,
        update_hash=_sha256(payload),
        update_bytes=payload,
        actor_kind="human",
        change_kind="suggestion",
        suggestion_ids=tuple(item.suggestion_id for item in suggestions),
        suggestions=suggestions,
        patches=patches,
        expected_sequence=expected_sequence,
        now=now,
    )


def _assistant_suggestion_update(
    *,
    tenant_id: str,
    document_id: str,
    actor_user_id: uuid.UUID,
    instance: CollaborationInstanceLease,
    payload: bytes,
    suggestions: tuple[CollaborationSuggestion, ...],
    patches: tuple[CollaborationPatchState, ...],
    command_id: uuid.UUID,
    now: float,
    expected_sequence: int | None = None,
) -> PersistCollaborationUpdate:
    """Build one server-side assistant publish without a browser lease."""
    return PersistCollaborationUpdate(
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=None,
        actor_user_id=actor_user_id,
        update_hash=_sha256(payload),
        update_bytes=payload,
        actor_kind="assistant",
        change_kind="suggestion",
        suggestion_ids=tuple(item.suggestion_id for item in suggestions),
        suggestions=suggestions,
        patches=patches,
        command_id=command_id,
        command_payload_hash=_sha256(f"publish:{command_id}".encode()),
        expected_sequence=expected_sequence,
        now=now,
    )


async def _create_human_patch(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    actor_user_id: uuid.UUID,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    patch_id: str | None = None,
    suggestion_id: str | None = None,
) -> tuple[str, str]:
    """Persist one initial insertion suggestion and return its identities."""
    actual_patch_id = patch_id or str(uuid.uuid4())
    actual_suggestion_id = suggestion_id or str(uuid.uuid4())
    descriptor = CollaborationSuggestion(
        suggestion_id=actual_suggestion_id,
        patch_id=actual_patch_id,
        author_id=actor_user_id,
        created_at=database.now + 1.0,
        kind="insertion",
    )
    patch_state = CollaborationPatchState(
        patch_id=actual_patch_id,
        author_id=actor_user_id,
        created_at=database.now + 1.0,
        active_suggestion_ids=(actual_suggestion_id,),
        kinds=("insertion",),
    )
    await database.store.append_update(
        _suggestion_update(
            tenant_id=tenant_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
            instance=instance,
            lease=lease,
            payload=f"suggestion:{actual_suggestion_id}".encode(),
            suggestions=(descriptor,),
            patches=(patch_state,),
            now=database.now + 1.0,
            expected_sequence=0,
        )
    )
    return actual_patch_id, actual_suggestion_id


@pytest.mark.asyncio
async def test_conversion_is_atomic_and_preserves_legacy_body_revision(
    database: _DatabaseHarness,
) -> None:
    """A failed CAS stores nothing; a valid conversion establishes all invariants."""
    document_id = "ed_collaboration_conversion"
    projection = "# Initial\n\nBody."
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        markdown=projection,
    )
    snapshot = _snapshot(
        tenant_id=TENANT_A,
        document_id=document_id,
        sequence=0,
        state_update=b"conversion-state",
        projection=projection,
        created_at=database.now,
    )

    with pytest.raises(CollaborationConflict) as conflict:
        await database.store.enable_document(
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
            expected_revision=DOCUMENT_REVISION - 1,
            expected_metadata_revision=METADATA_REVISION,
            schema_version=SCHEMA_VERSION,
            schema_hash=SCHEMA_HASH,
            snapshot=snapshot,
            projection_markdown=projection,
            now=database.now,
        )
    assert conflict.value.reason == "revision_conflict"

    async with database.session_factory() as session:
        document_before = (
            await session.execute(
                select(editor_documents).where(
                    editor_documents.c.id == document_id
                )
            )
        ).one()
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_snapshots)
            .where(editor_collaboration_snapshots.c.document_id == document_id)
        )
    assert document_before.content_mode == "markdown"
    assert document_before.collaboration_generation == 0
    assert snapshot_count == 0

    converted = await database.store.enable_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        expected_revision=DOCUMENT_REVISION,
        expected_metadata_revision=METADATA_REVISION,
        schema_version=SCHEMA_VERSION,
        schema_hash=SCHEMA_HASH,
        snapshot=snapshot,
        projection_markdown=projection,
        now=database.now,
    )

    assert converted.generation == 1
    assert converted.persisted_sequence == converted.projection_sequence == 0
    async with database.session_factory() as session:
        stored = (
            await session.execute(
                select(editor_documents).where(
                    editor_documents.c.id == document_id
                )
            )
        ).one()
        snapshots = (
            await session.execute(
                select(editor_collaboration_snapshots).where(
                    editor_collaboration_snapshots.c.document_id == document_id
                )
            )
        ).all()
    assert stored.content_mode == "collaboration"
    assert stored.revision == DOCUMENT_REVISION
    assert stored.metadata_revision == METADATA_REVISION + 1
    assert stored.collaboration_schema_version == SCHEMA_VERSION
    assert stored.collaboration_schema_hash == SCHEMA_HASH
    assert stored.content_markdown == projection
    assert len(snapshots) == 1
    assert snapshots[0].covered_sequence == 0


@pytest.mark.asyncio
async def test_concurrent_updates_allocate_once_and_replay_is_idempotent(
    database: _DatabaseHarness,
) -> None:
    """Concurrent writers receive consecutive sequences and a hash replay reuses one."""
    document_id = "ed_collaboration_sequences"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    first = _direct_update(
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
        payload=b"concurrent-update-a",
        now=database.now + 1.0,
    )
    second = _direct_update(
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
        payload=b"concurrent-update-b",
        now=database.now + 1.0,
    )

    results = await asyncio.gather(
        database.store.append_update(first),
        database.store.append_update(second),
    )
    assert {result.sequence for result in results} == {1, 2}
    assert all(not result.duplicate for result in results)

    oldest_update = first if results[0].sequence == 1 else second
    replay = await database.store.append_update(oldest_update)
    assert replay.duplicate is True
    assert replay.sequence == 1
    assert replay.persisted_sequence == 2

    async with database.session_factory() as session:
        sequences = list(
            (
                await session.execute(
                    select(editor_collaboration_updates.c.sequence)
                    .where(
                        editor_collaboration_updates.c.document_id == document_id
                    )
                    .order_by(editor_collaboration_updates.c.sequence)
                )
            ).scalars()
        )
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert sequences == [1, 2]
    assert persisted_sequence == 2


@pytest.mark.asyncio
async def test_activity_selects_optional_guest_actor_identity(
    database: _DatabaseHarness,
) -> None:
    """Activity rows expose the additive guest actor column for user edits too."""
    document_id = "ed_collaboration_activity_actor"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    await database.store.append_update(
        _direct_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"activity-actor-update",
            now=database.now + 1.0,
        )
    )

    activity = await database.store.list_activity(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before_sequence=None,
        author_user_id=None,
        change_kind=None,
        limit=10,
    )

    assert len(activity) == 1
    assert activity[0].actor_user_id == OWNER_A
    assert activity[0].actor_guest_identity_id is None


@pytest.mark.asyncio
async def test_activity_leaves_out_a_stored_row_that_changed_nothing_a_reader_sees(
    database: _DatabaseHarness,
) -> None:
    """A row whose summary says it changed nothing a reader sees (a
    formatting cleanup a writer sends after a reconnect) stays stored for the
    document's durability and replay, but the history lists no entry for it.
    A row whose summary makes no such claim, as every row written before the
    summary carried it, is listed as before; so is a formatting-only edit.
    The rows are left out before the limit applies, so a page stays full."""
    document_id = "ed_collaboration_activity_unchanged"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    summaries: list[dict[str, object]] = [
        {"edits": [], "omitted_edit_count": 0},
        {"changes_document": True, "edits": [], "omitted_edit_count": 1},
        {"changes_document": False, "edits": [], "omitted_edit_count": 0},
        {"changes_document": False, "edits": [], "omitted_edit_count": 0},
    ]
    for index, summary in enumerate(summaries):
        await database.store.append_update(
            replace(
                _direct_update(
                    tenant_id=TENANT_A,
                    document_id=document_id,
                    actor_user_id=OWNER_A,
                    instance=instance,
                    lease=lease,
                    payload=f"activity-unchanged-{index}".encode(),
                    now=database.now + 1.0 + index,
                ),
                change_summary=summary,
            )
        )

    listed = [
        row.sequence
        for row in await database.store.list_activity(
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            before_sequence=None,
            author_user_id=None,
            change_kind=None,
            limit=2,
        )
    ]
    async with database.session_factory() as session:
        stored = list(
            (
                await session.execute(
                    select(editor_collaboration_updates.c.sequence)
                    .where(
                        editor_collaboration_updates.c.document_id == document_id
                    )
                    .order_by(editor_collaboration_updates.c.sequence)
                )
            ).scalars()
        )

    assert listed == [2, 1]
    assert stored == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_update_hash_lookup_is_generation_and_tenant_scoped(
    database: _DatabaseHarness,
) -> None:
    """Reconnect lookup returns persisted coordinates without crossing RLS."""
    document_id = "ed_collaboration_hash_lookup"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    payload = b"lookup-persisted-update"
    update_hash = _sha256(payload)
    missing_hash = _sha256(b"lookup-missing-update")
    await database.store.append_update(
        _direct_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=payload,
            now=database.now + 1.0,
        )
    )

    found = await database.store.lookup_updates_by_hashes(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        update_hashes=(update_hash, missing_hash),
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        now=database.now + 2.0,
    )

    assert [(item.update_hash, item.sequence) for item in found] == [
        (update_hash, 1)
    ]
    with pytest.raises(CollaborationInstanceFenced):
        await database.store.lookup_updates_by_hashes(
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            update_hashes=(update_hash,),
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch + 1,
            now=database.now + 2.0,
        )
    instance_b = await database.store.acquire_instance(
        tenant_id=TENANT_B,
        instance_id="node-hash-lookup-b",
        now=database.now,
        lease_seconds=30.0,
    )
    with pytest.raises(CollaborationDocumentNotFound):
        await database.store.lookup_updates_by_hashes(
            tenant_id=TENANT_B,
            document_id=document_id,
            generation=1,
            update_hashes=(update_hash,),
            instance_id=instance_b.instance_id,
            instance_epoch=instance_b.epoch,
            now=database.now + 2.0,
        )
    with pytest.raises(CollaborationConflict) as conflict:
        await database.store.lookup_updates_by_hashes(
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=2,
            update_hashes=(update_hash,),
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            now=database.now + 2.0,
        )
    assert conflict.value.reason == "generation_conflict"


@pytest.mark.asyncio
async def test_append_requires_live_lease_and_current_instance_epoch(
    database: _DatabaseHarness,
) -> None:
    """A revoked lease blocks its writer and an expired epoch fences old Node state.

    Deleting the document revokes every lease on it without a successor.
    """
    document_id = "ed_collaboration_fencing"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        instance_id="node-old",
        instance_lease_seconds=10.0,
    )
    accepted = _direct_update(
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
        payload=b"authorized",
        now=database.now + 1.0,
    )
    assert (await database.store.append_update(accepted)).sequence == 1

    await database.store.tombstone_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        now=database.now + 2.0,
    )
    assert await _lease_revoked_at(database, lease) == database.now + 2.0
    with pytest.raises(CollaborationLeaseInvalid) as revoked:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"revoked",
                now=database.now + 3.0,
            )
        )
    # The lease check refuses before the deleted document is read; a
    # document or access refusal would carry another reason.
    assert revoked.value.reason == "lease_invalid"

    replacement = await database.store.acquire_instance(
        tenant_id=TENANT_A,
        instance_id="node-new",
        now=database.now + 11.0,
        lease_seconds=10.0,
    )
    assert replacement.epoch == instance.epoch + 1
    with pytest.raises(CollaborationInstanceFenced):
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"stale-instance",
                now=database.now + 12.0,
            )
        )

    async with database.session_factory() as session:
        update_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_updates)
            .where(editor_collaboration_updates.c.document_id == document_id)
        )
    assert update_count == 1


@pytest.mark.asyncio
async def test_current_instance_probe_is_live_and_tenant_scoped(
    database: _DatabaseHarness,
) -> None:
    """The public-probe read returns only the tenant's unexpired fencing row."""
    first = await database.store.acquire_instance(
        tenant_id=TENANT_A,
        instance_id="node-probe-a",
        now=database.now,
        lease_seconds=10.0,
    )
    tenant_b = await database.store.acquire_instance(
        tenant_id=TENANT_B,
        instance_id="node-probe-b",
        now=database.now,
        lease_seconds=30.0,
    )

    assert await database.store.get_current_instance(
        tenant_id=TENANT_A,
        now=database.now + 5.0,
    ) == first
    assert await database.store.get_current_instance(
        tenant_id=TENANT_A,
        now=database.now + 11.0,
    ) is None
    assert await database.store.get_current_instance(
        tenant_id=TENANT_B,
        now=database.now + 11.0,
    ) == tenant_b

    replacement = await database.store.acquire_instance(
        tenant_id=TENANT_A,
        instance_id="node-probe-a-replacement",
        now=database.now + 11.0,
        lease_seconds=10.0,
    )
    observed = await database.store.get_current_instance(
        tenant_id=TENANT_A,
        now=database.now + 12.0,
    )
    assert observed == replacement
    assert replacement.epoch == first.epoch + 1


@pytest.mark.asyncio
async def test_human_suggestion_co_commits_patch_create_and_membership_update(
    database: _DatabaseHarness,
) -> None:
    """Human suggestion updates create and evolve one patch in the same commits."""
    document_id = "ed_collaboration_human_patch"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )
    patch_id, first_id = await _create_human_patch(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
    )

    second_id = str(uuid.uuid4())
    second_descriptor = CollaborationSuggestion(
        suggestion_id=second_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 2.0,
        kind="deletion",
    )
    expanded_patch = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(first_id, second_id),
        kinds=("insertion", "deletion"),
    )
    second_result = await database.store.append_update(
        _suggestion_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"expand-human-patch",
            suggestions=(second_descriptor,),
            patches=(expanded_patch,),
            now=database.now + 2.0,
            expected_sequence=1,
        )
    )
    assert second_result.sequence == 2

    async with database.session_factory() as session:
        patch = (
            await session.execute(
                select(editor_patches).where(editor_patches.c.patch_id == patch_id)
            )
        ).mappings().one()
        updates = (
            await session.execute(
                select(
                    editor_collaboration_updates.c.sequence,
                    editor_collaboration_updates.c.suggestion_ids,
                )
                .where(editor_collaboration_updates.c.document_id == document_id)
                .order_by(editor_collaboration_updates.c.sequence)
            )
        ).all()
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert patch["source"] == "human"
    assert patch["status"] == "pending"
    assert patch["created_by_user_id"] == OWNER_A
    assert patch["collaboration_generation"] == 1
    assert patch["base_sequence"] == 0
    assert patch["suggestion_ids"] == [first_id, second_id]
    assert [item["suggestion_id"] for item in patch["edits"]] == [
        first_id,
        second_id,
    ]
    assert [row.sequence for row in updates] == [1, 2]
    assert updates[0].suggestion_ids == [first_id]
    assert updates[1].suggestion_ids == [second_id]
    assert persisted_sequence == 2


@pytest.mark.asyncio
async def test_private_assistant_publish_creates_patch_and_clears_draft_atomically(
    database: _DatabaseHarness,
) -> None:
    """A matching private draft authorizes one atomic shared patch publish."""
    document_id = "ed_collaboration_private_draft_publish"
    patch_id = str(uuid.uuid4())
    command_id = uuid.uuid4()
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_comments).values(
                    id="edc_private_ai",
                    document_id=document_id,
                    tenant_id=TENANT_A,
                    created_by_user_id=OWNER_A,
                    comment_markdown="Private rewrite",
                    anchor={},
                    kind="inline_edit",
                    status="open",
                    evidence_preset=None,
                    suggestion_draft={
                        "anchor_version": 1,
                        "change_summary": [],
                        "created_at": database.now,
                        "evidence": None,
                        "group_id": "editor-suggestion-group-publish",
                        "patch_id": patch_id,
                        "proposed_text": "Private provider output",
                        "publication_command_id": str(command_id),
                        "revision": 1,
                        "revision_history": [],
                        "suggestion_id": "editor-suggestion-publish",
                        "updated_at": database.now,
                        "warnings": [],
                    },
                    created_at=database.now,
                    updated_at=database.now,
                )
            )
    instance, _lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )

    suggestion_id = str(uuid.uuid4())
    descriptor = CollaborationSuggestion(
        suggestion_id=suggestion_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        kind="replacement",
    )
    patch_state = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(suggestion_id,),
        kinds=("replacement",),
    )
    result = await database.store.append_update(
        _assistant_suggestion_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            payload=b"publish-private-assistant-draft",
            suggestions=(descriptor,),
            patches=(patch_state,),
            command_id=command_id,
            now=database.now + 1.0,
            expected_sequence=0,
        )
    )

    async with database.session_factory() as session:
        draft = await session.scalar(
            select(editor_comments.c.suggestion_draft).where(
                editor_comments.c.tenant_id == TENANT_A,
                editor_comments.c.document_id == document_id,
                editor_comments.c.id == "edc_private_ai",
            )
        )
        patch = (
            await session.execute(
                select(editor_patches).where(
                    editor_patches.c.tenant_id == TENANT_A,
                    editor_patches.c.patch_id == patch_id,
                )
            )
        ).mappings().one()
    assert draft is None
    assert result.sequence == 1
    assert patch["source"] == "human"
    assert patch["created_by_user_id"] == OWNER_A
    assert patch["created_by_guest_identity_id"] is None
    assert patch["command_id"] == command_id
    assert patch["suggestion_ids"] == [suggestion_id]
    assert patch["edits"] == [
        {
            "suggestion_id": suggestion_id,
            "patch_id": patch_id,
            "author_id": str(OWNER_A),
            "created_at": database.now + 1.0,
            "kind": "replacement",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_command_id",
    [None, uuid.UUID("99999999-9999-4999-8999-999999999999")],
    ids=["missing-draft", "command-mismatch"],
)
async def test_private_assistant_publish_requires_matching_creator_draft(
    database: _DatabaseHarness,
    stored_command_id: uuid.UUID | None,
) -> None:
    """Assistant actor alone or a mismatched command cannot mint a shared patch."""
    document_id = "ed_collaboration_private_publish_without_draft"
    patch_id = str(uuid.uuid4())
    suggestion_id = str(uuid.uuid4())
    request_command_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    if stored_command_id is not None:
        async with database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(editor_comments).values(
                        id="edc_private_ai_mismatch",
                        document_id=document_id,
                        tenant_id=TENANT_A,
                        created_by_user_id=OWNER_A,
                        comment_markdown="Private command mismatch",
                        anchor={},
                        kind="inline_edit",
                        status="open",
                        evidence_preset=None,
                        suggestion_draft={
                            "anchor_version": 1,
                            "change_summary": [],
                            "created_at": database.now,
                            "evidence": None,
                            "group_id": "editor-suggestion-group-mismatch",
                            "patch_id": patch_id,
                            "proposed_text": "Private provider output",
                            "publication_command_id": str(stored_command_id),
                            "revision": 1,
                            "revision_history": [],
                            "suggestion_id": "editor-suggestion-mismatch",
                            "updated_at": database.now,
                            "warnings": [],
                        },
                        created_at=database.now,
                        updated_at=database.now,
                    )
                )
    instance, _lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )
    descriptor = CollaborationSuggestion(
        suggestion_id=suggestion_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        kind="replacement",
    )
    patch_state = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(suggestion_id,),
        kinds=("replacement",),
    )

    with pytest.raises(CollaborationConflict) as conflict:
        await database.store.append_update(
            _assistant_suggestion_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                payload=b"publish-without-private-draft",
                suggestions=(descriptor,),
                patches=(patch_state,),
                command_id=request_command_id,
                now=database.now + 1.0,
                expected_sequence=0,
            )
        )

    assert conflict.value.reason == "patch_not_found"
    async with database.session_factory() as session:
        patch_count = await session.scalar(
            select(func.count()).select_from(editor_patches).where(
                editor_patches.c.tenant_id == TENANT_A,
                editor_patches.c.patch_id == patch_id,
            )
        )
        update_count = await session.scalar(
            select(func.count()).select_from(editor_collaboration_updates).where(
                editor_collaboration_updates.c.tenant_id == TENANT_A,
                editor_collaboration_updates.c.document_id == document_id,
            )
        )
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.tenant_id == TENANT_A,
                editor_documents.c.id == document_id,
            )
        )
        draft_count = await session.scalar(
            select(func.count()).select_from(editor_comments).where(
                editor_comments.c.tenant_id == TENANT_A,
                editor_comments.c.document_id == document_id,
                editor_comments.c.suggestion_draft.is_not(None),
            )
        )
    assert patch_count == 0
    assert update_count == 0
    assert persisted_sequence == 0
    assert draft_count == (1 if stored_command_id is not None else 0)


@pytest.mark.asyncio
async def test_slash_insertion_can_be_superseded_by_one_structure_suggestion(
    database: _DatabaseHarness,
) -> None:
    """A sidecar-proven slash command may replace its durable patch member."""
    document_id = "ed_collaboration_slash_structure"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )
    patch_id, slash_id = await _create_human_patch(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
    )
    structure_id = str(uuid.uuid4())
    slash_descriptor = CollaborationSuggestion(
        suggestion_id=slash_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        kind="insertion",
    )
    structure_descriptor = CollaborationSuggestion(
        suggestion_id=structure_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        kind="structure",
    )
    replacement = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(structure_id,),
        kinds=("structure",),
        superseded_suggestion_ids=(slash_id,),
    )

    with pytest.raises(CollaborationConflict) as unproven:
        await database.store.append_update(
            _suggestion_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"unproven-slash-to-structure",
                suggestions=(slash_descriptor, structure_descriptor),
                patches=(replace(replacement, superseded_suggestion_ids=()),),
                now=database.now + 2.0,
                expected_sequence=1,
            )
        )
    assert unproven.value.reason == "patch_membership_shrink"

    result = await database.store.append_update(
        _suggestion_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"slash-to-structure",
            suggestions=(slash_descriptor, structure_descriptor),
            patches=(replacement,),
            now=database.now + 2.0,
            expected_sequence=1,
        )
    )

    assert result.sequence == 2
    async with database.session_factory() as session:
        patch = (
            await session.execute(
                select(editor_patches).where(editor_patches.c.patch_id == patch_id)
            )
        ).mappings().one()
        update_row = (
            await session.execute(
                select(editor_collaboration_updates)
                .where(
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.sequence == 2,
                )
            )
        ).mappings().one()
    assert patch["suggestion_ids"] == [structure_id]
    assert patch["edits"] == [
        {
            "suggestion_id": structure_id,
            "patch_id": patch_id,
            "author_id": str(OWNER_A),
            "created_at": database.now + 1.0,
            "kind": "structure",
        }
    ]
    assert update_row["suggestion_ids"] == [slash_id, structure_id]


@pytest.mark.asyncio
async def test_non_decision_update_cannot_empty_pending_patch_membership(
    database: _DatabaseHarness,
) -> None:
    """Postgres rejects a compromised sidecar's orphaned pending patch."""
    document_id = "ed_collaboration_empty_pending_patch"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )
    patch_id, suggestion_id = await _create_human_patch(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
    )
    emptied_patch = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(),
        kinds=(),
    )

    with pytest.raises(CollaborationConflict) as conflict:
        await database.store.append_update(
            _suggestion_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"empty-pending-patch",
                suggestions=(),
                patches=(emptied_patch,),
                now=database.now + 2.0,
                expected_sequence=1,
            )
        )

    assert conflict.value.reason == "patch_suggestions_empty"
    async with database.session_factory() as session:
        patch = (
            await session.execute(
                select(editor_patches).where(editor_patches.c.patch_id == patch_id)
            )
        ).mappings().one()
        update_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_updates)
            .where(editor_collaboration_updates.c.document_id == document_id)
        )
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert patch["status"] == "pending"
    assert patch["suggestion_ids"] == [suggestion_id]
    assert update_count == persisted_sequence == 1


@pytest.mark.asyncio
async def test_open_patch_preserves_exact_ai_edits_for_preview(
    database: _DatabaseHarness,
) -> None:
    """Publishing suggestion metadata never overwrites anchored AI edits."""
    document_id = "ed_collaboration_exact_preview"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    patch_id = str(uuid.uuid4())
    suggestion_id = str(uuid.uuid4())
    exact_edit = {
        "id": "edit-exact-1",
        "find": "old text",
        "text": "new text",
        "position": "replace",
        "quote_before": "before",
        "quote_after": "after",
    }
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_patches).values(
                    patch_id=patch_id,
                    tenant_id=TENANT_A,
                    document_id=document_id,
                    run_id=None,
                    source="agent",
                    status="pending",
                    edits=[exact_edit],
                    summary="Exact preview",
                    warnings=[],
                    revision_before=DOCUMENT_REVISION,
                    collaboration_generation=1,
                    base_sequence=0,
                    decision_sequence=None,
                    suggestion_ids=[],
                    applied_revision=None,
                    applied_edit_ids=None,
                    note="",
                    created_by_user_id=OWNER_A,
                    decided_by_user_id=None,
                    command_id=None,
                    created_at=database.now + 1.0,
                    decided_at=None,
                )
            )
    private_page = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind=None,
        limit=50,
    )
    assert private_page.patches == ()
    descriptor = CollaborationSuggestion(
        suggestion_id=suggestion_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        kind="replacement",
    )
    patch_state = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(suggestion_id,),
        kinds=("replacement",),
    )
    await database.store.append_update(
        _suggestion_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"publish-exact-preview",
            suggestions=(descriptor,),
            patches=(patch_state,),
            now=database.now + 2.0,
            expected_sequence=0,
        )
    )

    open_page = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=OWNER_A,
        suggestion_kind="replacement",
        limit=50,
    )
    open_patches = open_page.patches

    assert len(open_patches) == 1
    assert open_patches[0].suggestion_ids == (suggestion_id,)
    assert open_patches[0].kinds == ("replacement",)
    assert open_patches[0].exact_edits == (exact_edit,)


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    (("accept", "accepted"), ("reject", "rejected")),
)
@pytest.mark.asyncio
async def test_decision_update_co_commits_patch_terminal_state(
    database: _DatabaseHarness,
    decision: CollaborationDecision,
    expected_status: str,
) -> None:
    """Accept and reject decisions terminalize their patch at the update sequence."""
    document_id = f"ed_collaboration_decision_{decision}"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    patch_id, suggestion_id = await _create_human_patch(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
    )
    command_id = uuid.uuid4()
    payload = f"decision:{decision}:{patch_id}".encode()
    command_payload_hash = _sha256(
        f"decision-command:{decision}:{patch_id}".encode()
    )
    decision_update = PersistCollaborationUpdate(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=lease.lease_id,
        actor_user_id=OWNER_A,
        update_hash=_sha256(payload),
        update_bytes=payload,
        actor_kind="human",
        change_kind="decision",
        patches=(
            CollaborationPatchState(
                patch_id=patch_id,
                author_id=OWNER_A,
                created_at=database.now + 1.0,
                active_suggestion_ids=(),
                kinds=(),
            ),
        ),
        decision=decision,
        command_id=command_id,
        command_payload_hash=command_payload_hash,
        expected_sequence=1,
        now=database.now + 2.0,
    )
    decided = await database.store.append_update(decision_update)
    replay = await database.store.append_update(decision_update)
    assert decided.sequence == replay.sequence == 2
    assert decided.duplicate is False
    assert replay.duplicate is True

    async with database.session_factory() as session:
        patch = (
            await session.execute(
                select(editor_patches).where(editor_patches.c.patch_id == patch_id)
            )
        ).mappings().one()
        decision_row = (
            await session.execute(
                select(editor_collaboration_updates).where(
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.sequence == 2,
                )
            )
        ).one()
        update_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_updates)
            .where(editor_collaboration_updates.c.document_id == document_id)
        )
    assert patch["status"] == expected_status
    assert patch["decision_sequence"] == 2
    assert patch["decided_by_user_id"] == OWNER_A
    assert patch["command_id"] == command_id
    assert patch["applied_edit_ids"] == (
        [suggestion_id] if decision == "accept" else None
    )
    assert decision_row.change_kind == "decision"
    assert decision_row.command_id == command_id
    assert decision_row.command_payload_hash == command_payload_hash
    assert update_count == 2

    lookup = await database.store.lookup_command(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        command_id=command_id,
        command_payload_hash=command_payload_hash,
    )
    assert lookup is not None
    assert lookup.sequence == 2
    assert lookup.decision == decision
    assert lookup.patch_ids == (patch_id,)


@pytest.mark.asyncio
async def test_snapshot_and_projection_commit_together_and_conflict_rolls_back(
    database: _DatabaseHarness,
) -> None:
    """Advance snapshot and projection atomically; roll back a conflict."""
    document_id = "ed_collaboration_snapshot"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    await database.store.append_update(
        _direct_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"snapshot-tail",
            now=database.now + 1.0,
        )
    )
    projection = "# Projected\n\nDurable body."
    snapshot = _snapshot(
        tenant_id=TENANT_A,
        document_id=document_id,
        sequence=1,
        state_update=b"snapshot-state-one",
        projection=projection,
        created_at=database.now + 2.0,
    )
    await database.store.store_snapshot(
        snapshot,
        projection_markdown=projection,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        now=database.now + 2.0,
    )

    async with database.session_factory() as session:
        document = (
            await session.execute(
                select(editor_documents).where(editor_documents.c.id == document_id)
            )
        ).one()
        stored_snapshot = (
            await session.execute(
                select(editor_collaboration_snapshots).where(
                    editor_collaboration_snapshots.c.document_id == document_id,
                    editor_collaboration_snapshots.c.covered_sequence == 1,
                )
            )
        ).one()
    assert document.content_markdown == projection
    assert document.projection_sequence == 1
    assert document.projection_updated_at == database.now + 2.0
    assert stored_snapshot.projection_hash == _sha256(projection.encode())

    conflicting_projection = "# Conflicting projection"
    conflict_snapshot = _snapshot(
        tenant_id=TENANT_A,
        document_id=document_id,
        sequence=1,
        state_update=b"different-state-at-one",
        projection=conflicting_projection,
        created_at=database.now + 3.0,
    )
    with pytest.raises(CollaborationConflict) as conflict:
        await database.store.store_snapshot(
            conflict_snapshot,
            projection_markdown=conflicting_projection,
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            now=database.now + 3.0,
        )
    assert conflict.value.reason == "snapshot_conflict"

    async with database.session_factory() as session:
        unchanged = (
            await session.execute(
                select(editor_documents).where(editor_documents.c.id == document_id)
            )
        ).one()
        sequence_one_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_snapshots)
            .where(
                editor_collaboration_snapshots.c.document_id == document_id,
                editor_collaboration_snapshots.c.covered_sequence == 1,
            )
        )
    assert unchanged.content_markdown == projection
    assert unchanged.projection_sequence == 1
    assert sequence_one_count == 1


@pytest.mark.asyncio
async def test_load_state_returns_two_complete_snapshot_candidates(
    database: _DatabaseHarness,
) -> None:
    """A corrupt newest snapshot can be skipped without losing its prior tail."""
    document_id = "ed_collaboration_snapshot_fallback"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    for sequence in (1, 2):
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=f"fallback-update-{sequence}".encode(),
                now=database.now + sequence,
            )
        )
        if sequence == 1:
            projection = "# Snapshot one"
            await database.store.store_snapshot(
                _snapshot(
                    tenant_id=TENANT_A,
                    document_id=document_id,
                    sequence=1,
                    state_update=b"snapshot-one",
                    projection=projection,
                    created_at=database.now + 1.5,
                ),
                projection_markdown=projection,
                instance_id=instance.instance_id,
                instance_epoch=instance.epoch,
                now=database.now + 1.5,
            )

    loaded = await database.store.load_state(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
    )

    assert loaded.snapshot.covered_sequence == 1
    assert [item.sequence for item in loaded.updates] == [2]
    assert len(loaded.fallback_candidates) == 1
    fallback = loaded.fallback_candidates[0]
    assert fallback.snapshot.covered_sequence == 0
    assert [item.sequence for item in fallback.updates] == [1, 2]


@pytest.mark.asyncio
async def test_compaction_retention_and_tombstone_purge_are_bounded(
    database: _DatabaseHarness,
) -> None:
    """Compaction keeps two snapshots, ages payloads before metadata, then purges."""
    document_id = "ed_collaboration_retention"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        instance_lease_seconds=10_000.0,
    )
    for sequence in (1, 2):
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=f"retained-update-{sequence}".encode(),
                now=database.now + sequence,
            )
        )
        projection = f"# Projection {sequence}"
        await database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document_id,
                sequence=sequence,
                state_update=f"state-{sequence}".encode(),
                projection=projection,
                created_at=database.now + sequence + 0.25,
            ),
            projection_markdown=projection,
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            now=database.now + sequence + 0.25,
        )

    pruned, deleted = await database.store.compact(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        now=database.now + 100.0,
        payload_retention_seconds=50.0,
        metadata_retention_seconds=1_000.0,
    )
    assert (pruned, deleted) == (1, 0)
    async with database.session_factory() as session:
        updates = (
            await session.execute(
                select(editor_collaboration_updates)
                .where(editor_collaboration_updates.c.document_id == document_id)
                .order_by(editor_collaboration_updates.c.sequence)
            )
        ).all()
        snapshot_sequences = list(
            (
                await session.execute(
                    select(editor_collaboration_snapshots.c.covered_sequence)
                    .where(
                        editor_collaboration_snapshots.c.document_id == document_id
                    )
                    .order_by(
                        editor_collaboration_snapshots.c.covered_sequence.desc()
                    )
                )
            ).scalars()
        )
    assert [row.update_bytes for row in updates] == [
        None,
        b"retained-update-2",
    ]
    assert updates[0].payload_pruned_at == database.now + 100.0
    assert updates[1].payload_pruned_at is None
    assert snapshot_sequences == [2, 1]

    second_pruned, metadata_deleted = await database.store.compact(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        now=database.now + 5_000.0,
        payload_retention_seconds=50.0,
        metadata_retention_seconds=1_000.0,
    )
    assert second_pruned == 0
    assert metadata_deleted == 1

    next_generation = await database.store.tombstone_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        now=database.now + 6_000.0,
    )
    assert next_generation == 2
    assert (
        await database.store.purge_tombstones(
            tenant_id=TENANT_A,
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            now=database.now + 6_050.0,
            retention_seconds=100.0,
        )
        == 0
    )
    assert (
        await database.store.purge_tombstones(
            tenant_id=TENANT_A,
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            now=database.now + 6_200.0,
            retention_seconds=100.0,
        )
        == 1
    )

    async with database.session_factory() as session:
        document_count = await session.scalar(
            select(func.count())
            .select_from(editor_documents)
            .where(editor_documents.c.id == document_id)
        )
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_snapshots)
            .where(editor_collaboration_snapshots.c.document_id == document_id)
        )
        lease_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_leases)
            .where(editor_collaboration_leases.c.document_id == document_id)
        )
    assert document_count == snapshot_count == lease_count == 0


@pytest.mark.asyncio
async def test_rotation_reconstructs_one_replacement_after_a_lost_response(
    database: _DatabaseHarness,
) -> None:
    """A durable rotation command returns its first replacement on retry."""
    document_id = "ed_collaboration_rotation_retry"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    _instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    rotation_command_id = uuid.uuid4()
    replacement = CollaborationLease(
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"replacement-token-one"),
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        user_id=OWNER_A,
        permission="edit",
        session_id=original.session_id,
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=rotation_command_id,
        rotated_from_lease_id=original.lease_id,
    )

    first = await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=replacement,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    replay_candidate = replace(
        replacement,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"replacement-token-two"),
        issued_at=database.now + 2.0,
        expires_at=database.now + 3_602.0,
        last_validated_at=database.now + 2.0,
    )
    replayed = await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=replay_candidate,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    assert replayed == first == replacement
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(editor_collaboration_leases)
                .where(
                    editor_collaboration_leases.c.document_id == document_id
                )
                .order_by(editor_collaboration_leases.c.issued_at)
            )
        ).all()
    assert len(rows) == 2
    assert rows[0].revoked_at == database.now + 1.0
    assert rows[1].rotation_command_id == rotation_command_id


@pytest.mark.asyncio
async def test_update_started_before_rotation_uses_immediate_successor_authority(
    database: _DatabaseHarness,
) -> None:
    """An in-flight update survives an equal-authority lease rotation."""
    document_id = "ed_collaboration_inflight_rotation"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    update_started_before_rotation = _direct_update(
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=original,
        payload=b"inflight-update-across-rotation",
        now=database.now + 1.5,
    )
    rotation_command_id = uuid.uuid4()
    successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-rotation-successor"),
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=rotation_command_id,
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    persisted = await database.store.append_update(
        update_started_before_rotation
    )
    replayed = await database.store.append_update(
        update_started_before_rotation
    )

    assert persisted.sequence == 1
    assert persisted.persisted_sequence == 1
    assert persisted.duplicate is False
    assert replayed.sequence == 1
    assert replayed.persisted_sequence == 1
    assert replayed.duplicate is True


@pytest.mark.asyncio
async def test_update_started_before_rotation_obeys_successor_permission(
    database: _DatabaseHarness,
) -> None:
    """A rotated read-only authority cannot complete an earlier edit."""
    document_id = "ed_collaboration_inflight_rotation_downgrade"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-downgraded-successor"),
        permission="view",
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    with pytest.raises(CollaborationLeaseInvalid) as denied:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"inflight-edit-after-downgrade",
                now=database.now + 1.5,
            )
        )
    assert str(denied.value) == "permission_denied"


@pytest.mark.asyncio
async def test_update_started_before_rotation_does_not_follow_a_lease_chain(
    database: _DatabaseHarness,
) -> None:
    """Only an active direct successor can authorize an in-flight update."""
    document_id = "ed_collaboration_inflight_rotation_chain"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    first_successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-first-successor"),
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=first_successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    second_successor = replace(
        first_successor,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-second-successor"),
        issued_at=database.now + 2.0,
        expires_at=database.now + 3_602.0,
        last_validated_at=database.now + 2.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=first_successor.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=first_successor.lease_id,
        previous_token_hash=first_successor.token_hash,
        replacement=second_successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    with pytest.raises(CollaborationLeaseInvalid):
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"inflight-update-after-second-rotation",
                now=database.now + 2.5,
            )
        )


@pytest.mark.asyncio
async def test_update_started_before_rotation_requires_live_original_lifetime(
    database: _DatabaseHarness,
) -> None:
    """A successor cannot extend the lifetime of a stale captured lease."""
    document_id = "ed_collaboration_inflight_rotation_expired"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-expired-original-successor"),
        issued_at=database.now + 3_599.0,
        expires_at=database.now + 7_199.0,
        last_validated_at=database.now + 3_599.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    with pytest.raises(CollaborationLeaseInvalid):
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"inflight-update-after-original-expiry",
                now=database.now + 3_600.5,
            )
        )


@pytest.mark.asyncio
async def test_update_started_before_rotation_rejects_revoked_successor(
    database: _DatabaseHarness,
) -> None:
    """Revoking the successor still blocks a captured predecessor lease.

    Deleting the document revokes the successor without minting another.
    """
    document_id = "ed_collaboration_inflight_rotation_revoked"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-revoked-successor"),
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    await database.store.tombstone_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        now=database.now + 1.25,
    )
    assert await _lease_revoked_at(database, successor) == database.now + 1.25

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"inflight-update-after-explicit-revocation",
                now=database.now + 1.5,
            )
        )
    # The lease check refuses before the deleted document is read; a
    # document or access refusal would carry another reason.
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
async def test_update_started_before_rotation_requires_live_session(
    database: _DatabaseHarness,
) -> None:
    """A direct successor never bypasses current account-session checks."""
    document_id = "ed_collaboration_inflight_rotation_session"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"inflight-session-successor"),
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_601.0,
        last_validated_at=database.now + 1.0,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(auth_sessions)
                .where(auth_sessions.c.id == original.session_id)
                .values(expires_at=database.now + 1.25)
            )

    with pytest.raises(CollaborationLeaseInvalid) as denied:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"inflight-update-after-session-expiry",
                now=database.now + 1.5,
            )
        )
    assert str(denied.value) == "session_invalid"


def _rotation_stamped_after(
    original: CollaborationLease,
    *,
    issued_at: float,
    token: bytes,
) -> CollaborationLease:
    """Build the equal-authority successor a token refresh mints."""
    return replace(
        original,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(token),
        issued_at=issued_at,
        expires_at=issued_at + 3_600.0,
        last_validated_at=issued_at,
        rotation_command_id=uuid.uuid4(),
        rotated_from_lease_id=original.lease_id,
    )


@pytest.mark.asyncio
async def test_update_received_before_its_rotation_was_stamped_uses_the_successor(
    database: _DatabaseHarness,
) -> None:
    """An update that arrived before a rotation committing first completes.

    The API stamps an update when its request arrives and a rotation when
    its own request starts, before either transaction locks the lease row,
    so the rotation's stamp can be the later one although it commits first.
    The update's lease was live and unrevoked when it arrived; it holds the
    same authority as an update arriving just after the rotation: exactly
    the immediate successor.
    """
    document_id = "ed_collaboration_rotation_stamped_after_arrival"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    received_at = database.now + 1.0
    successor = _rotation_stamped_after(
        original,
        issued_at=database.now + 1.5,
        token=b"rotation-stamped-after-arrival",
    )
    await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    persisted = await database.store.append_update(
        _direct_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=original,
            payload=b"update-received-before-rotation-stamp",
            now=received_at,
        )
    )

    assert persisted.sequence == 1
    assert persisted.persisted_sequence == 1
    assert persisted.duplicate is False


async def _wait_until_transactions_wait_on_locks(
    database: _DatabaseHarness,
    *,
    waiters: int = 1,
) -> None:
    """Return once ``waiters`` backends wait on a lock another one holds.

    PostgreSQL reports the wait itself (``pg_blocking_pids``), whichever row
    it is on: the contracts that use this pin which calls finish and in
    what order, not the row a call queues at. Fails instead of returning
    when the waiters do not appear, so a caller that relies on the ordering
    never proceeds on an interleaving it did not get.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        async with database.session_factory() as session:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND cardinality(pg_blocking_pids(pid)) > 0"
                )
            )
        if waiting is not None and waiting >= waiters:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"fewer than {waiters} transactions waited on a lock")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interleaving",
    [
        "rotation_commits_while_update_is_open",
        "update_waits_on_rotation_row_lock",
    ],
)
async def test_update_in_flight_across_a_concurrent_rotation_uses_the_successor(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    interleaving: str,
) -> None:
    """An update whose transaction began before a rotation committed completes.

    The update's transaction is open, holding the writer fence and its
    arrival stamp, before the rotation stamps and commits the successor.
    Either the rotation commits before the update reaches the lease row, or
    the update queues on the row the rotation holds and resumes on its
    commit. Both are ordinary lock orders of one in-flight update.
    """
    document_id = f"ed_collaboration_concurrent_rotation_{interleaving}"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    received_at = database.now + 1.0
    successor = _rotation_stamped_after(
        original,
        issued_at=database.now + 1.5,
        token=f"concurrent-rotation-{interleaving}".encode(),
    )

    fence_locked = asyncio.Event()
    release_update = asyncio.Event()
    lock_instance_fence = collaboration_postgres._lock_instance_fence

    async def pause_update_after_fence_lock(
        session: AsyncSession,
        *,
        tenant_id: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> object:
        locked = await lock_instance_fence(
            session,
            tenant_id=tenant_id,
            instance_id=instance_id,
            instance_epoch=instance_epoch,
            now=now,
        )
        fence_locked.set()
        await release_update.wait()
        return locked

    rotation_holds_row = asyncio.Event()
    release_rotation = asyncio.Event()
    sweep_expired_actor_leases = (
        collaboration_postgres._sweep_expired_actor_leases
    )

    async def pause_rotation_after_row_lock(
        session: AsyncSession,
        lease: CollaborationLease,
    ) -> None:
        rotation_holds_row.set()
        await release_rotation.wait()
        await sweep_expired_actor_leases(session, lease)

    monkeypatch.setattr(
        collaboration_postgres,
        "_lock_instance_fence",
        pause_update_after_fence_lock,
    )
    monkeypatch.setattr(
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        pause_rotation_after_row_lock,
    )
    update_task = asyncio.create_task(
        database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=f"update-across-rotation-{interleaving}".encode(),
                now=received_at,
            )
        )
    )
    rotation_task: asyncio.Task[CollaborationLease] | None = None
    try:
        await asyncio.wait_for(fence_locked.wait(), timeout=10.0)
        rotation_task = asyncio.create_task(
            database.store.rotate_lease(
                previous_lease_id=original.lease_id,
                previous_token_hash=original.token_hash,
                replacement=successor,
                max_issued_per_window=30,
                issued_since=database.now - 60.0,
            )
        )
        await asyncio.wait_for(rotation_holds_row.wait(), timeout=10.0)
        if interleaving == "rotation_commits_while_update_is_open":
            release_rotation.set()
            rotated = await asyncio.wait_for(rotation_task, timeout=10.0)
            assert not update_task.done()
            release_update.set()
        else:
            release_update.set()
            await _wait_until_transactions_wait_on_locks(database)
            assert not update_task.done()
            assert not rotation_task.done()
            release_rotation.set()
            rotated = await asyncio.wait_for(rotation_task, timeout=10.0)
        persisted = await asyncio.wait_for(update_task, timeout=10.0)
    finally:
        release_update.set()
        release_rotation.set()
        pending = [
            task
            for task in (update_task, rotation_task)
            if task is not None
        ]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    assert rotated == successor
    assert persisted.sequence == 1
    assert persisted.persisted_sequence == 1
    assert persisted.duplicate is False


@pytest.mark.asyncio
async def test_a_replayed_rotation_and_an_update_under_the_rotated_lease_do_not_deadlock(
    database: _DatabaseHarness,
) -> None:
    """A replayed rotation and an update under the rotated lease both finish.

    A browser that did not learn the outcome of a rotation replays it with
    the same command id, and until it learns the new lease its connection
    keeps signing updates with the rotated one. Such an update locks the
    rotated lease, then its successor. Both calls lock the document row
    before any lease row, so the one that holds the document finishes
    before the other reaches a lease row; the replay also locks the two
    lease rows in the update's order. If one of them locked a lease row
    before the document and the other the document before a lease row,
    each transaction could hold a row the other waits for, and the database
    would abort one of them.

    A third transaction holds the rotated lease row first, so the update,
    which already holds the document, queues on that row, and the replay
    queues behind the update at the document.
    """
    document_id = "ed_collaboration_rotation_replay_lock_order"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = await _rotate(
        database,
        original,
        issued_at=database.now + 1.0,
        token=b"rotation-replayed-while-update-in-flight",
    )
    replay = replace(
        successor,
        lease_id=uuid.uuid4(),
        token_hash=_sha256(b"rotation-replay-candidate"),
        issued_at=database.now + 2.0,
        expires_at=database.now + 3_602.0,
        last_validated_at=database.now + 2.0,
    )

    update_task: asyncio.Task[PersistedCollaborationUpdate] | None = None
    replay_task: asyncio.Task[CollaborationLease] | None = None
    try:
        async with database.session_factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(editor_collaboration_leases.c.lease_id)
                    .where(
                        editor_collaboration_leases.c.lease_id
                        == original.lease_id
                    )
                    .with_for_update()
                )
                update_task = asyncio.create_task(
                    database.store.append_update(
                        _direct_update(
                            tenant_id=TENANT_A,
                            document_id=document_id,
                            actor_user_id=OWNER_A,
                            instance=instance,
                            lease=original,
                            payload=b"update-under-lease-while-rotation-replays",
                            now=database.now + 1.5,
                        )
                    )
                )
                await _wait_until_transactions_wait_on_locks(database)
                replay_task = asyncio.create_task(
                    database.store.rotate_lease(
                        previous_lease_id=original.lease_id,
                        previous_token_hash=original.token_hash,
                        replacement=replay,
                        max_issued_per_window=30,
                        issued_since=database.now - 60.0,
                    )
                )
                await _wait_until_transactions_wait_on_locks(
                    database,
                    waiters=2,
                )
                assert not update_task.done()
                assert not replay_task.done()
        outcomes = await asyncio.wait_for(
            asyncio.gather(update_task, replay_task, return_exceptions=True),
            timeout=20.0,
        )
    finally:
        pending = [
            task for task in (update_task, replay_task) if task is not None
        ]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    failures = [
        outcome for outcome in outcomes if isinstance(outcome, BaseException)
    ]
    assert not failures, failures
    persisted, replayed = outcomes
    assert replayed == successor
    assert persisted.sequence == 1
    assert persisted.duplicate is False


# The tests below stamp an update at +1.0 and the rotation of its lease at
# +1.5: the update arrived first, the rotation committed first. That is the
# order in which an update is judged by the successor although the successor
# was stamped after the update's own arrival, so the refusals an update
# signed with a rotated lease must meet are pinned in this order too,
# wherever the order can occur.


async def _collaboration_document_of_owner_a(
    database: _DatabaseHarness,
    document_id: str,
) -> None:
    """Seed and convert one collaboration document owned by OWNER_A."""
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )


async def _lease_revoked_at(
    database: _DatabaseHarness,
    lease: CollaborationLease,
) -> float | None:
    """Read the stored revocation stamp of ``lease``."""
    async with database.session_factory() as session:
        return await session.scalar(
            select(editor_collaboration_leases.c.revoked_at).where(
                editor_collaboration_leases.c.lease_id == lease.lease_id
            )
        )


async def _rotate(
    database: _DatabaseHarness,
    original: CollaborationLease,
    *,
    issued_at: float,
    token: bytes,
    permission: CollaborationPermission | None = None,
    expires_at: float | None = None,
) -> CollaborationLease:
    """Rotate ``original`` through the store, as a token refresh does."""
    successor = _rotation_stamped_after(
        original,
        issued_at=issued_at,
        token=token,
    )
    if permission is not None:
        successor = replace(successor, permission=permission)
    if expires_at is not None:
        successor = replace(successor, expires_at=expires_at)
    return await database.store.rotate_lease(
        previous_lease_id=original.lease_id,
        previous_token_hash=original.token_hash,
        replacement=successor,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )


def _guest_direct_update(
    *,
    document_id: str,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    payload: bytes,
    now: float,
) -> PersistCollaborationUpdate:
    """Build one direct edit a guest writes under ``lease``."""
    return PersistCollaborationUpdate(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=lease.lease_id,
        update_hash=_sha256(payload),
        update_bytes=payload,
        change_kind="direct",
        now=now,
        **_actor_fields(lease),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_guest_update_received_before_its_rotation_was_stamped_uses_the_successor(
    database: _DatabaseHarness,
) -> None:
    """A guest's in-flight update completes like an account holder's.

    The successor is found from the lease rows, not from the actor kind:
    a guest lease rotated after the guest's update arrived authorizes that
    update through its immediate successor.
    """
    document_id = "ed_collaboration_guest_rotation_after_arrival"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=b"guest-rotation-after-arrival",
    )

    persisted = await database.store.append_update(
        _guest_direct_update(
            document_id=document_id,
            instance=instance,
            lease=original,
            payload=b"guest-update-received-before-rotation",
            now=database.now + 1.0,
        )
    )

    assert persisted.sequence == 1
    assert persisted.persisted_sequence == 1
    assert persisted.duplicate is False


@pytest.mark.asyncio
async def test_update_received_before_a_downgrading_rotation_obeys_the_successor_permission(
    database: _DatabaseHarness,
) -> None:
    """An update that arrived before a downgrade is judged by the successor.

    The member's share was lowered to view, and the next rotation minted a
    read-only successor. The edit carried the earlier edit lease, but the
    lease it is judged by is the successor, so it is refused for its
    permission rather than written under the lease it carried.
    """
    document_id = "ed_collaboration_rotation_after_arrival_downgrade"
    await _reviewed_document(database, document_id, reviewer_permission="edit")
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="edit",
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(resource_shares)
                .where(
                    resource_shares.c.resource_id == document_id,
                    resource_shares.c.recipient_user_id == REVIEWER_A,
                )
                .values(permission="view")
            )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=b"downgrading-rotation-after-arrival",
        permission="view",
    )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=REVIEWER_A,
                instance=instance,
                lease=original,
                payload=b"edit-received-before-downgrading-rotation",
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "permission_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "share_change",
    ["share_removed", "share_lowered_to_view"],
)
async def test_update_received_before_a_rotation_is_refused_after_the_share_changes(
    database: _DatabaseHarness,
    share_change: str,
) -> None:
    """Access lost after the rotation refuses an update that arrived before.

    The successor still carries edit, because the share changed only after
    the rotation; the update is refused by the current access check, which
    judges it after the lease rows are locked.
    """
    document_id = f"ed_collaboration_rotation_after_arrival_{share_change}"
    await _reviewed_document(database, document_id, reviewer_permission="edit")
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="edit",
    )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=f"rotation-after-arrival-{share_change}".encode(),
    )
    share = update(resource_shares).where(
        resource_shares.c.resource_id == document_id,
        resource_shares.c.recipient_user_id == REVIEWER_A,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                share.values(revoked_at=func.now())
                if share_change == "share_removed"
                else share.values(permission="view")
            )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=REVIEWER_A,
                instance=instance,
                lease=original,
                payload=f"edit-received-before-{share_change}".encode(),
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "access_revoked"


@pytest.mark.asyncio
async def test_update_received_before_a_rotation_is_refused_after_sign_out(
    database: _DatabaseHarness,
) -> None:
    """Signing out refuses an update that arrived before the rotation.

    Sign-out deletes the account session, and every lease bound to that
    session goes with it, the successor included. An update that arrived
    before the rotation therefore finds no lease at all; the successor
    lookup never gets the chance to authorize it.
    """
    document_id = "ed_collaboration_rotation_after_arrival_sign_out"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=b"rotation-after-arrival-sign-out",
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                auth_sessions.delete().where(
                    auth_sessions.c.id == original.session_id
                )
            )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"edit-received-before-sign-out",
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "successor_change",
    ["successor_revoked", "rotated_again"],
)
async def test_update_received_before_a_rotation_is_refused_when_the_successor_is_no_longer_current(
    database: _DatabaseHarness,
    successor_change: str,
) -> None:
    """Only a current immediate successor authorizes an earlier update.

    A revoked successor ends the tolerance at once, and a second rotation
    ends it after one step: the update is never followed down a chain.
    """
    document_id = f"ed_collaboration_rotation_after_arrival_{successor_change}"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=f"rotation-after-arrival-{successor_change}".encode(),
    )
    if successor_change == "successor_revoked":
        # Deleting the document revokes the successor without minting
        # another; the lease check refuses before the document is read.
        await database.store.tombstone_document(
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
            now=database.now + 1.75,
        )
        assert (
            await _lease_revoked_at(database, successor) == database.now + 1.75
        )
    else:
        await _rotate(
            database,
            successor,
            issued_at=database.now + 2.0,
            token=b"second-rotation-after-arrival",
        )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=f"edit-received-before-{successor_change}".encode(),
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
async def test_update_received_before_a_revocation_without_successor_is_refused(
    database: _DatabaseHarness,
) -> None:
    """A revocation that mints no successor refuses updates already in flight.

    The lease was live when the update arrived, but a revocation is
    immediate: it is not deferred until the updates stamped before it are
    written. Deleting the document revokes the lease without a successor;
    the lease check refuses before the deleted document is read.
    """
    document_id = "ed_collaboration_revocation_after_arrival"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    await database.store.tombstone_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        now=database.now + 1.5,
    )
    assert await _lease_revoked_at(database, original) == database.now + 1.5

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=b"edit-received-before-revocation",
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forgery",
    ["another_session", "stamp_of_another_revocation"],
)
async def test_update_received_before_a_rotation_rejects_a_successor_the_rotation_did_not_mint(
    database: _DatabaseHarness,
    forgery: str,
) -> None:
    """A successor row the rotation did not write authorizes nothing.

    The rows are written directly, bypassing the rotation's own checks. Each
    links to the revoked lease with a rotation command; one names a second
    live session of the same user, the other follows a revocation stamped
    by another write. Only the comparison of the two lease rows can refuse
    them, and the update arrived before both stamps.
    """
    document_id = f"ed_collaboration_rotation_after_arrival_forged_{forgery}"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    forged = _rotation_stamped_after(
        original,
        issued_at=database.now + 1.5,
        token=f"forged-successor-{forgery}".encode(),
    )
    revoked_at = forged.issued_at
    if forgery == "another_session":
        other_session_id = f"session:{TENANT_A}:{document_id}:other"
        await _seed_browser_session(
            database,
            tenant_id=TENANT_A,
            user_id=OWNER_A,
            session_id=other_session_id,
        )
        forged = replace(forged, session_id=other_session_id)
    else:
        revoked_at = database.now + 1.25
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(editor_collaboration_leases)
                .where(editor_collaboration_leases.c.lease_id == original.lease_id)
                .values(revoked_at=revoked_at)
            )
            await session.execute(
                insert(editor_collaboration_leases).values(
                    **collaboration_postgres._lease_values(forged)
                )
            )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=original,
                payload=f"edit-received-before-forged-{forgery}".encode(),
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
async def test_guest_update_received_before_a_rotation_is_refused_after_the_link_is_revoked(
    database: _DatabaseHarness,
) -> None:
    """Revoking a guest link refuses the guest's updates already in flight.

    The link revocation revokes every lease still open on the link,
    including the successor the rotation minted, so an update that arrived
    before the rotation finds no current successor.
    """
    document_id = "ed_collaboration_guest_rotation_after_arrival_link_revoked"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.5,
        token=b"guest-rotation-before-link-revocation",
    )
    assert original.guest_link_id is not None
    await PostgresEditorGuestLinkStore(
        session_factory=database.session_factory,
        app_role=APP_ROLE,
    ).revoke_link(
        tenant_id=TENANT_A,
        document_id=document_id,
        link_id=original.guest_link_id,
        actor_user_id=OWNER_A,
        expected_revision=1,
        command_id=uuid.uuid4(),
        command_payload_hash=_sha256(b"revoke-guest-link"),
        now=database.now + 1.75,
    )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _guest_direct_update(
                document_id=document_id,
                instance=instance,
                lease=original,
                payload=b"guest-update-received-before-link-revocation",
                now=database.now + 1.0,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
async def test_update_after_its_successor_expired_is_refused_within_the_original_lifetime(
    database: _DatabaseHarness,
) -> None:
    """An expired successor ends the tolerance before the original expires.

    A guest lease is capped by its link, so after the owner shortened the
    link the successor expires before its predecessor. The predecessor's
    remaining lifetime never outlasts its successor. A successor expires
    after it was stamped, so only an update arriving after the rotation
    can meet an expired successor; this refusal is pinned in that order.
    """
    document_id = "ed_collaboration_guest_rotation_successor_expired"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, original = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_expires_at = database.now + 1.25
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(editor_document_share_links)
                .where(editor_document_share_links.c.id == original.guest_link_id)
                .values(expires_at=link_expires_at)
            )
    await _rotate(
        database,
        original,
        issued_at=database.now + 1.0,
        token=b"guest-rotation-capped-by-link",
        expires_at=link_expires_at,
    )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _guest_direct_update(
                document_id=document_id,
                instance=instance,
                lease=original,
                payload=b"guest-update-after-successor-expiry",
                now=database.now + 1.5,
            )
        )
    assert refused.value.reason == "lease_invalid"


@pytest.mark.asyncio
async def test_session_rate_limit_serializes_one_user_across_documents(
    database: _DatabaseHarness,
) -> None:
    """Concurrent cold requests cannot bypass the user-wide issuance window."""
    document_ids = (
        "ed_collaboration_rate_a",
        "ed_collaboration_rate_b",
    )
    for document_id in document_ids:
        await _seed_markdown_document(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
        )
        await _activate_document(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
        )
    session_id = "session:collaboration-rate-across-documents"
    await _seed_browser_session(
        database,
        tenant_id=TENANT_A,
        user_id=OWNER_A,
        session_id=session_id,
    )
    leases = tuple(
        CollaborationLease(
            lease_id=uuid.uuid4(),
            token_hash=_sha256(f"rate-token:{document_id}".encode()),
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            user_id=OWNER_A,
            permission="edit",
            session_id=session_id,
            issued_at=database.now,
            expires_at=database.now + 3_600.0,
            last_validated_at=database.now,
        )
        for document_id in document_ids
    )

    results = await asyncio.gather(
        *(
            database.store.issue_lease(
                lease,
                max_active=5,
                max_issued_per_window=1,
                issued_since=database.now - 60.0,
            )
            for lease in leases
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, CollaborationLease) for result in results) == 1
    rate_errors = [
        result for result in results if isinstance(result, CollaborationRateLimited)
    ]
    assert [error.reason for error in rate_errors] == ["session_rate_limited"]
    async with database.session_factory() as session:
        lease_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_leases)
            .where(editor_collaboration_leases.c.tenant_id == TENANT_A)
        )
    assert lease_count == 1


@pytest.mark.asyncio
async def test_issuing_a_lease_sweeps_only_the_actors_stale_rows(
    database: _DatabaseHarness,
) -> None:
    """Row hygiene lives at the producer: minting a lease deletes the SAME
    actor's rows that expired beyond the grace window — expirations inside
    the grace and other actors' trails must survive."""
    document_id = "ed_collaboration_sweep"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    session_id = "session:collaboration-sweep"
    await _seed_browser_session(
        database,
        tenant_id=TENANT_A,
        user_id=OWNER_A,
        session_id=session_id,
    )
    grace = collaboration_postgres._LEASE_SWEEP_GRACE_SECONDS

    def expired_row(
        token: str, user_id: uuid.UUID, expires_at: float
    ) -> dict[str, object]:
        return {
            "lease_id": uuid.uuid4(),
            "token_hash": _sha256(token.encode()),
            "tenant_id": TENANT_A,
            "document_id": document_id,
            "generation": 1,
            "user_id": user_id,
            "permission": "edit",
            "session_id": session_id,
            "issued_at": expires_at - 60.0,
            "expires_at": expires_at,
        }

    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_collaboration_leases),
                [
                    expired_row(
                        "sweep:own-stale", OWNER_A, database.now - grace - 10.0
                    ),
                    expired_row(
                        "sweep:own-recent", OWNER_A, database.now - 30.0
                    ),
                    expired_row(
                        "sweep:other-stale", OWNER_B, database.now - grace - 10.0
                    ),
                ],
            )

    await database.store.issue_lease(
        CollaborationLease(
            lease_id=uuid.uuid4(),
            token_hash=_sha256(b"sweep:new"),
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            user_id=OWNER_A,
            permission="edit",
            session_id=session_id,
            issued_at=database.now,
            expires_at=database.now + 3_600.0,
            last_validated_at=database.now,
        ),
        max_active=5,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )

    async with database.session_factory() as session:
        remaining = set(
            (
                await session.execute(
                    select(editor_collaboration_leases.c.token_hash).where(
                        editor_collaboration_leases.c.tenant_id == TENANT_A
                    )
                )
            ).scalars()
        )
    assert _sha256(b"sweep:own-stale") not in remaining
    assert {
        _sha256(b"sweep:own-recent"),
        _sha256(b"sweep:other-stale"),
        _sha256(b"sweep:new"),
    } <= remaining


@pytest.mark.asyncio
async def test_simultaneous_cold_instance_acquire_has_one_fenced_loser(
    database: _DatabaseHarness,
) -> None:
    """The first primary-slot insert is stable under a true cold-start race."""
    results = await asyncio.gather(
        database.store.acquire_instance(
            tenant_id=TENANT_A,
            instance_id="node-cold-a",
            now=database.now,
            lease_seconds=15.0,
        ),
        database.store.acquire_instance(
            tenant_id=TENANT_A,
            instance_id="node-cold-b",
            now=database.now,
            lease_seconds=15.0,
        ),
        return_exceptions=True,
    )

    assert sum(
        isinstance(result, CollaborationInstanceLease) for result in results
    ) == 1
    assert sum(
        isinstance(result, CollaborationInstanceFenced) for result in results
    ) == 1


@pytest.mark.asyncio
async def test_takeover_fences_snapshot_compaction_and_tombstone_purge(
    database: _DatabaseHarness,
) -> None:
    """A takeover committed first prevents every stale maintenance mutation."""
    document_id = "ed_collaboration_atomic_fence"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    stale_instance, _lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        instance_id="node-stale",
        instance_lease_seconds=10.0,
    )
    await database.store.tombstone_document(
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
        now=database.now + 2.0,
    )
    current_instance = await database.store.acquire_instance(
        tenant_id=TENANT_A,
        instance_id="node-current",
        now=database.now + 11.0,
        lease_seconds=15.0,
    )
    assert current_instance.epoch == stale_instance.epoch + 1

    with pytest.raises(CollaborationInstanceFenced):
        await database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document_id,
                sequence=0,
                state_update=b"stale-snapshot",
                projection="# Stale",
                created_at=database.now + 12.0,
            ),
            projection_markdown="# Stale",
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 12.0,
        )
    with pytest.raises(CollaborationInstanceFenced):
        await database.store.compact(
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 12.0,
            payload_retention_seconds=0.0,
            metadata_retention_seconds=1.0,
        )
    with pytest.raises(CollaborationInstanceFenced):
        await database.store.purge_tombstones(
            tenant_id=TENANT_A,
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 12.0,
            retention_seconds=1.0,
        )

    async with database.session_factory() as session:
        document_count = await session.scalar(
            select(func.count())
            .select_from(editor_documents)
            .where(editor_documents.c.id == document_id)
        )
        stale_snapshot_count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_snapshots)
            .where(
                editor_collaboration_snapshots.c.document_id == document_id,
                editor_collaboration_snapshots.c.state_hash
                == _sha256(b"stale-snapshot"),
            )
        )
    assert document_count == 1
    assert stale_snapshot_count == 0


@pytest.mark.parametrize("mutation", ["snapshot", "compact", "purge"])
@pytest.mark.asyncio
async def test_takeover_waits_for_inflight_fenced_mutation_transaction(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Fence validation remains locked through each maintenance mutation."""
    document_id = f"ed_collaboration_fence_race_{mutation}"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    stale_instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        instance_id="node-race-stale",
        instance_lease_seconds=10.0,
    )
    if mutation in {"snapshot", "compact"}:
        persisted = await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=stale_instance,
                lease=lease,
                payload=f"fence-race-{mutation}".encode(),
                now=database.now + 1.0,
            )
        )
        assert persisted.sequence == 1
    if mutation == "compact":
        await database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document_id,
                sequence=1,
                state_update=b"compact-race-snapshot-one",
                projection="# Compact one",
                created_at=database.now + 1.5,
            ),
            projection_markdown="# Compact one",
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 1.5,
        )
        second = await database.store.append_update(
            _direct_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=stale_instance,
                lease=lease,
                payload=b"fence-race-compact-second",
                now=database.now + 2.0,
            )
        )
        assert second.sequence == 2
        await database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document_id,
                sequence=2,
                state_update=b"compact-race-snapshot-two",
                projection="# Compact two",
                created_at=database.now + 2.5,
            ),
            projection_markdown="# Compact two",
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 2.5,
        )
    if mutation == "purge":
        await database.store.tombstone_document(
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
            now=database.now + 2.0,
        )

    fence_locked = asyncio.Event()
    release_mutation = asyncio.Event()
    lock_instance_fence = collaboration_postgres._lock_instance_fence

    async def pause_after_fence_lock(
        session: AsyncSession,
        *,
        tenant_id: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> object:
        locked = await lock_instance_fence(
            session,
            tenant_id=tenant_id,
            instance_id=instance_id,
            instance_epoch=instance_epoch,
            now=now,
        )
        fence_locked.set()
        await release_mutation.wait()
        return locked

    monkeypatch.setattr(
        collaboration_postgres,
        "_lock_instance_fence",
        pause_after_fence_lock,
    )
    if mutation == "snapshot":
        mutation_call = database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document_id,
                sequence=1,
                state_update=b"race-snapshot",
                projection="# Race snapshot",
                created_at=database.now + 3.0,
            ),
            projection_markdown="# Race snapshot",
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 3.0,
        )
    elif mutation == "compact":
        mutation_call = database.store.compact(
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 3.0,
            payload_retention_seconds=0.0,
            metadata_retention_seconds=3_600.0,
        )
    else:
        mutation_call = database.store.purge_tombstones(
            tenant_id=TENANT_A,
            instance_id=stale_instance.instance_id,
            instance_epoch=stale_instance.epoch,
            now=database.now + 4.0,
            retention_seconds=1.0,
        )

    mutation_task = asyncio.create_task(mutation_call)
    await asyncio.wait_for(fence_locked.wait(), timeout=1.0)
    takeover_task = asyncio.create_task(
        database.store.acquire_instance(
            tenant_id=TENANT_A,
            instance_id="node-race-current",
            now=database.now + 11.0,
            lease_seconds=15.0,
        )
    )
    await asyncio.sleep(0.05)
    takeover_was_blocked = not takeover_task.done()
    release_mutation.set()
    await mutation_task
    current_instance = await takeover_task

    assert takeover_was_blocked
    assert current_instance.epoch == stale_instance.epoch + 1
    async with database.session_factory() as session:
        if mutation == "snapshot":
            stored = await session.scalar(
                select(func.count())
                .select_from(editor_collaboration_snapshots)
                .where(
                    editor_collaboration_snapshots.c.document_id == document_id,
                    editor_collaboration_snapshots.c.state_hash
                    == _sha256(b"race-snapshot"),
                )
            )
            assert stored == 1
        elif mutation == "compact":
            payload = await session.scalar(
                select(editor_collaboration_updates.c.update_bytes).where(
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.sequence == 1,
                )
            )
            assert payload is None
        else:
            remaining = await session.scalar(
                select(func.count())
                .select_from(editor_documents)
                .where(editor_documents.c.id == document_id)
            )
            assert remaining == 0


# Writer fence. Every fenced write transaction (update, hash lookup,
# snapshot, compaction, tombstone purge) reads the tenant's instance row
# FOR KEY SHARE. The contracts below pin what that mode must and must not
# do: writes of different documents and instance renewals never wait for
# writes in flight; a takeover waits for every write in flight and fences
# every later write of the old epoch; the document row still orders the
# writes of one document; and the guarantees that only a tenant-wide
# exclusive lock used to give (an answer to a standby at once, a hash lookup
# that sees an in-flight persist, a reused command id answered as a
# conflict) stay.


@dataclass(frozen=True)
class _Writable:
    """One collaboration document with a live writer epoch and lease."""

    tenant_id: str
    owner_user_id: uuid.UUID
    document_id: str
    instance: CollaborationInstanceLease
    lease: CollaborationLease

    def edit(
        self, payload: bytes, now: float, **changes: Any
    ) -> PersistCollaborationUpdate:
        """Build one direct edit of the owner under this lease."""
        return replace(
            _direct_update(
                tenant_id=self.tenant_id,
                document_id=self.document_id,
                actor_user_id=self.owner_user_id,
                instance=self.instance,
                lease=self.lease,
                payload=payload,
                now=now,
            ),
            **changes,
        )


async def _writable_documents(
    database: _DatabaseHarness,
    name: str,
    *,
    count: int = 1,
    tenant_id: str = TENANT_A,
    owner_user_id: uuid.UUID = OWNER_A,
    instance_id: str = "node-primary",
    instance_lease_seconds: float = 5_000.0,
) -> list[_Writable]:
    """Create ``count`` collaboration documents writable by their owner."""
    documents = []
    for index in range(count):
        document_id = f"ed_collaboration_fence_{name}_{index}"
        await _seed_markdown_document(
            database,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_user_id=owner_user_id,
        )
        await _activate_document(
            database,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_user_id=owner_user_id,
        )
        instance, lease = await _authorize_writer(
            database,
            tenant_id=tenant_id,
            document_id=document_id,
            user_id=owner_user_id,
            instance_id=instance_id,
            instance_lease_seconds=instance_lease_seconds,
        )
        documents.append(
            _Writable(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                document_id=document_id,
                instance=instance,
                lease=lease,
            )
        )
    return documents


@dataclass(frozen=True)
class _PausedCall:
    """Events of one store call held inside its write transaction."""

    reached: asyncio.Event
    release: asyncio.Event


_PAUSED_CALL: contextvars.ContextVar[_PausedCall | None] = contextvars.ContextVar(
    "collaboration_paused_call",
    default=None,
)


def _pause_inside(
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    name: str,
    *,
    after: bool,
) -> None:
    """Hold calls started by ``_start_paused`` at ``owner.name``.

    With ``after`` such a call holds right after ``name`` returned, so it
    keeps every lock taken so far; otherwise it holds right before ``name``
    runs. Calls started any other way pass through unchanged.
    """
    original = getattr(owner, name)

    async def held(*args: Any, **kwargs: Any) -> Any:
        paused = _PAUSED_CALL.get()
        if paused is not None and not after:
            paused.reached.set()
            await paused.release.wait()
        result = await original(*args, **kwargs)
        if paused is not None and after:
            paused.reached.set()
            await paused.release.wait()
        return result

    monkeypatch.setattr(owner, name, held)


def _start_paused(
    call: Callable[[], Coroutine[Any, Any, Any]],
) -> tuple["asyncio.Task[Any]", _PausedCall]:
    """Start ``call`` as a task that holds where ``_pause_inside`` says."""
    paused = _PausedCall(reached=asyncio.Event(), release=asyncio.Event())
    token = _PAUSED_CALL.set(paused)
    try:
        task = asyncio.create_task(call())
    finally:
        _PAUSED_CALL.reset(token)
    return task, paused


async def _waits_on_a_lock_before_it_finishes(
    database: _DatabaseHarness,
    task: "asyncio.Task[Any]",
    *,
    statement_like: str = "%",
) -> bool:
    """Return True once a backend running a statement like ``statement_like``
    waits on a lock another backend holds, and False once ``task`` finished.

    PostgreSQL reports the wait itself (``pg_blocking_pids``), so the answer
    does not depend on how fast the machine is: a fixed sleep cannot tell a
    call that waits from one that is merely slow.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if task.done():
            return False
        async with database.session_factory() as session:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock' "
                    "AND cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND query ILIKE :statement"
                ),
                {"statement": statement_like},
            )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    pytest.fail("the call neither finished nor waited on a lock within 10 s")


async def _update_count(database: _DatabaseHarness, document_id: str) -> int:
    """Count the stored updates of one document."""
    async with database.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_updates)
            .where(editor_collaboration_updates.c.document_id == document_id)
        )
    return int(count or 0)


@pytest.mark.asyncio
async def test_persists_of_different_documents_do_not_wait_for_each_other(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persist that holds its fence, lease, access and document locks does
    not hold up a persist of another document of the same owner: the fence
    is shared, and every other row it locks belongs to its own document."""
    first, second = await _writable_documents(
        database, "independent_documents", count=2
    )
    _pause_inside(
        monkeypatch, database.store, "_persist_patch_states", after=False
    )
    held, paused = _start_paused(
        lambda: database.store.append_update(
            first.edit(b"independent-first", database.now + 1.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        other = asyncio.create_task(
            database.store.append_update(
                second.edit(b"independent-second", database.now + 1.0)
            )
        )
        other_waited = await _waits_on_a_lock_before_it_finishes(database, other)
    finally:
        paused.release.set()
    other_persisted = await other
    held_persisted = await held

    assert other_waited is False
    assert other_persisted.sequence == 1
    assert held_persisted.sequence == 1


@pytest.mark.asyncio
async def test_takeover_waits_for_an_inflight_update_of_the_old_epoch(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The takeover's FOR UPDATE waits for an update that holds the fence
    under the old epoch, so that update commits first, and every later
    update of the old epoch finds no fence row. FOR KEY SHARE conflicts only
    with FOR UPDATE: without the takeover's explicit lock, or with any
    weaker one, the takeover would not wait and the old epoch's update
    would land after it."""
    (document,) = await _writable_documents(
        database,
        "takeover_after_inflight_update",
        instance_id="node-old",
        instance_lease_seconds=10.0,
    )
    _pause_inside(
        monkeypatch, collaboration_postgres, "_lock_instance_fence", after=True
    )
    inflight, paused = _start_paused(
        lambda: database.store.append_update(
            document.edit(b"inflight-old-epoch", database.now + 9.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        takeover = asyncio.create_task(
            database.store.acquire_instance(
                tenant_id=TENANT_A,
                instance_id="node-new",
                now=database.now + 11.0,
                lease_seconds=15.0,
            )
        )
        takeover_waited = await _waits_on_a_lock_before_it_finishes(
            database,
            takeover,
            statement_like="%FROM editor_collaboration_instances%FOR UPDATE%",
        )
    finally:
        paused.release.set()
    persisted = await inflight
    replacement = await takeover

    assert takeover_waited is True
    assert persisted.sequence == 1
    assert replacement.instance_id == "node-new"
    assert replacement.epoch == document.instance.epoch + 1
    with pytest.raises(CollaborationInstanceFenced):
        await database.store.append_update(
            document.edit(b"after-takeover-old-epoch", database.now + 9.5)
        )
    assert await _update_count(database, document.document_id) == 1


class _HoldAfterInstanceUpdate:
    """Session proxy that holds its transaction right after it updated the
    instance row: a takeover has written the new epoch but not committed."""

    def __init__(self, session: AsyncSession, paused: _PausedCall) -> None:
        self._session = session
        self._paused = paused

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.execute(statement, *args, **kwargs)
        if (
            getattr(statement, "is_update", False)
            and getattr(statement, "table", None) is editor_collaboration_instances
        ):
            self._paused.reached.set()
            await self._paused.release.wait()
        return result


@pytest.mark.asyncio
async def test_update_arriving_during_a_takeover_is_fenced_after_it_commits(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update whose clock still lies inside the old lease reaches the
    fence while a takeover has written the new epoch but not committed. It
    waits for the takeover and is then refused: it never writes under the
    epoch the takeover replaced."""
    (document,) = await _writable_documents(
        database,
        "update_during_takeover",
        instance_id="node-old",
        instance_lease_seconds=10.0,
    )
    original_session = database.store._session

    @contextlib.asynccontextmanager
    async def session_for(tenant_id: str) -> AsyncIterator[Any]:
        async with original_session(tenant_id) as session:
            paused = _PAUSED_CALL.get()
            yield (
                session
                if paused is None
                else _HoldAfterInstanceUpdate(session, paused)
            )

    monkeypatch.setattr(database.store, "_session", session_for)
    takeover, paused = _start_paused(
        lambda: database.store.acquire_instance(
            tenant_id=TENANT_A,
            instance_id="node-new",
            now=database.now + 11.0,
            lease_seconds=15.0,
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        stale = asyncio.create_task(
            database.store.append_update(
                document.edit(b"stale-epoch-during-takeover", database.now + 9.0)
            )
        )
        stale_waited = await _waits_on_a_lock_before_it_finishes(
            database,
            stale,
            statement_like="%FROM editor_collaboration_instances%",
        )
    finally:
        paused.release.set()
    replacement = await takeover

    with pytest.raises(CollaborationInstanceFenced):
        await stale
    assert stale_waited is True
    assert replacement.epoch == document.instance.epoch + 1
    assert await _update_count(database, document.document_id) == 0


@pytest.mark.asyncio
async def test_instance_renewal_does_not_wait_for_inflight_updates(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal changes no key column, so it does not conflict with the
    writers' FOR KEY SHARE: the lease is extended while an update of the same
    instance holds the fence."""
    (document,) = await _writable_documents(database, "renewal_beside_update")
    _pause_inside(
        monkeypatch, collaboration_postgres, "_lock_instance_fence", after=True
    )
    inflight, paused = _start_paused(
        lambda: database.store.append_update(
            document.edit(b"inflight-beside-renewal", database.now + 1.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        renewal = asyncio.create_task(
            database.store.renew_instance(
                tenant_id=TENANT_A,
                instance_id=document.instance.instance_id,
                epoch=document.instance.epoch,
                now=database.now + 2.0,
                lease_seconds=5_000.0,
            )
        )
        renewal_waited = await _waits_on_a_lock_before_it_finishes(
            database, renewal
        )
    finally:
        paused.release.set()
    renewed = await renewal
    persisted = await inflight

    assert renewal_waited is False
    assert renewed.epoch == document.instance.epoch
    assert renewed.lease_expires_at == pytest.approx(database.now + 5_002.0)
    assert persisted.sequence == 1


@pytest.mark.asyncio
async def test_takeover_is_not_starved_by_old_writers_once_the_lease_expired(
    database: _DatabaseHarness,
) -> None:
    """Old-instance writers that hand the fence on without a gap stop
    matching it when the lease expires, so a takeover queued behind them gets
    in after the writes already in flight."""
    (document,) = await _writable_documents(
        database, "takeover_after_expiry", instance_id="node-old"
    )
    instance = document.instance
    start = time.time()
    await database.store.renew_instance(
        tenant_id=TENANT_A,
        instance_id=instance.instance_id,
        epoch=instance.epoch,
        now=start,
        lease_seconds=0.8,
    )
    stop = asyncio.Event()
    holders: list[tuple["asyncio.Task[None]", asyncio.Event]] = []

    async def hold_fence(locked: asyncio.Event, release: asyncio.Event) -> None:
        async with tenant_session(
            database.session_factory, tenant_id=TENANT_A, app_role=APP_ROLE
        ) as session:
            await collaboration_postgres._lock_instance_fence(
                session,
                tenant_id=TENANT_A,
                instance_id=instance.instance_id,
                instance_epoch=instance.epoch,
                now=time.time(),
            )
            locked.set()
            await release.wait()

    def start_holder() -> tuple["asyncio.Task[None]", asyncio.Event, asyncio.Event]:
        locked, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(hold_fence(locked, release))
        holders.append((task, release))
        return task, locked, release

    async def relay() -> None:
        """Hand the fence from one old writer to the next: the current one
        commits only after the next one holds the fence, or after 50 ms when
        the next one cannot get it."""
        current, locked, release = start_holder()
        await locked.wait()
        while not stop.is_set():
            following, following_locked, following_release = start_holder()
            reached = asyncio.create_task(following_locked.wait())
            await asyncio.wait(
                {reached, following},
                timeout=0.05,
                return_when=asyncio.FIRST_COMPLETED,
            )
            reached.cancel()
            release.set()
            await current
            if following.done() and following.exception() is not None:
                return
            await following_locked.wait()
            current, locked, release = (
                following,
                following_locked,
                following_release,
            )
        release.set()
        with contextlib.suppress(Exception):
            await current

    relay_task = asyncio.create_task(relay())
    await asyncio.sleep(max(0.0, start + 0.85 - time.time()))
    try:
        replacement = await asyncio.wait_for(
            database.store.acquire_instance(
                tenant_id=TENANT_A,
                instance_id="node-new",
                now=time.time(),
                lease_seconds=15.0,
            ),
            timeout=1.5,
        )
    finally:
        stop.set()
        relay_task.cancel()
        with contextlib.suppress(BaseException):
            await relay_task
        for _task, release in holders:
            release.set()
        await asyncio.gather(
            *(task for task, _release in holders), return_exceptions=True
        )

    assert replacement.epoch == instance.epoch + 1


@pytest.mark.asyncio
async def test_fenced_paths_lock_the_instance_row_once_for_key_share(
    database: _DatabaseHarness,
) -> None:
    """Each fenced path touches the instance row with exactly one statement,
    a SELECT ... FOR KEY SHARE. A second statement on the row could upgrade
    the lock (a deadlock between two holders), and any stronger mode would
    make writes of different documents, or a renewal, wait. SQLAlchemy's
    ``with_for_update(key_share=True)`` alone emits FOR NO KEY UPDATE, which
    this also refuses."""
    (document,) = await _writable_documents(database, "single_fence_statement")
    fence = {
        "instance_id": document.instance.instance_id,
        "instance_epoch": document.instance.epoch,
    }
    calls: dict[str, Callable[[], Coroutine[Any, Any, Any]]] = {
        "append_update": lambda: database.store.append_update(
            document.edit(b"single-fence", database.now + 1.0)
        ),
        "lookup_updates_by_hashes": lambda: database.store.lookup_updates_by_hashes(
            tenant_id=TENANT_A,
            document_id=document.document_id,
            generation=1,
            update_hashes=(_sha256(b"single-fence"),),
            now=database.now + 1.0,
            **fence,
        ),
        "store_snapshot": lambda: database.store.store_snapshot(
            _snapshot(
                tenant_id=TENANT_A,
                document_id=document.document_id,
                sequence=1,
                state_update=b"single-fence-snapshot",
                projection="# Single fence",
                created_at=database.now + 1.0,
            ),
            projection_markdown="# Single fence",
            now=database.now + 1.0,
            **fence,
        ),
        "compact": lambda: database.store.compact(
            tenant_id=TENANT_A,
            document_id=document.document_id,
            generation=1,
            now=database.now + 1.0,
            payload_retention_seconds=0.0,
            metadata_retention_seconds=3_600.0,
            **fence,
        ),
        "purge_tombstones": lambda: database.store.purge_tombstones(
            tenant_id=TENANT_A,
            now=database.now + 1.0,
            retention_seconds=1.0,
            **fence,
        ),
    }
    statements: list[str] = []

    def record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "editor_collaboration_instances" in statement:
            statements.append(" ".join(statement.split()))

    seen: dict[str, list[str]] = {}
    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        for name, call in calls.items():
            statements.clear()
            await call()
            seen[name] = list(statements)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)

    assert set(seen) == set(calls)
    for name, recorded in seen.items():
        assert len(recorded) == 1, (name, recorded)
        assert recorded[0].startswith("SELECT "), (name, recorded)
        assert recorded[0].endswith(" FOR KEY SHARE"), (name, recorded)


@pytest.mark.asyncio
async def test_standby_acquire_is_refused_at_once_while_updates_hold_the_fence(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standby asking for the slot while the live instance writes gets its
    refusal before any lock: writers that hand the shared fence on would
    keep a FOR UPDATE waiting for as long as they overlap."""
    (document,) = await _writable_documents(database, "standby_beside_update")
    _pause_inside(
        monkeypatch, collaboration_postgres, "_lock_instance_fence", after=True
    )
    inflight, paused = _start_paused(
        lambda: database.store.append_update(
            document.edit(b"inflight-beside-standby", database.now + 1.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        standby = asyncio.create_task(
            database.store.acquire_instance(
                tenant_id=TENANT_A,
                instance_id="node-standby",
                now=database.now + 1.0,
                lease_seconds=15.0,
            )
        )
        standby_waited = await _waits_on_a_lock_before_it_finishes(
            database, standby
        )
    finally:
        paused.release.set()
    persisted = await inflight

    with pytest.raises(
        CollaborationInstanceFenced, match="primary instance lease is active"
    ):
        await standby
    assert standby_waited is False
    assert persisted.sequence == 1


@pytest.mark.asyncio
async def test_hash_lookup_waits_for_an_inflight_persist_of_its_document(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hash lookup that resolves an unknown persist outcome waits for a
    persist of the same document that holds the document row, and then finds
    its sequence instead of answering "not found" a moment too early."""
    (document,) = await _writable_documents(database, "lookup_waits")
    _pause_inside(
        monkeypatch, database.store, "_persist_patch_states", after=False
    )
    persist, paused = _start_paused(
        lambda: database.store.append_update(
            document.edit(b"inflight-before-lookup", database.now + 1.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        lookup = asyncio.create_task(
            database.store.lookup_updates_by_hashes(
                tenant_id=TENANT_A,
                document_id=document.document_id,
                generation=1,
                update_hashes=(_sha256(b"inflight-before-lookup"),),
                instance_id=document.instance.instance_id,
                instance_epoch=document.instance.epoch,
                now=database.now + 1.0,
            )
        )
        lookup_waited = await _waits_on_a_lock_before_it_finishes(
            database, lookup
        )
    finally:
        paused.release.set()
    persisted = await persist
    found = await lookup

    assert lookup_waited is True
    assert persisted.sequence == 1
    assert [(item.update_hash, item.sequence) for item in found] == [
        (_sha256(b"inflight-before-lookup"), 1)
    ]


@pytest.mark.asyncio
async def test_command_reused_on_another_document_is_a_conflict(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent persists that carry one command id on different
    documents: one commits, the other is answered as a command conflict
    (409), never with the unique index's raw violation (500)."""
    first, second = await _writable_documents(
        database, "command_reuse", count=2
    )
    command = {
        "command_id": uuid.uuid4(),
        "command_payload_hash": _sha256(b"reused-command"),
    }
    _pause_inside(
        monkeypatch, database.store, "_persist_patch_states", after=False
    )
    held, paused = _start_paused(
        lambda: database.store.append_update(
            first.edit(b"command-first-document", database.now + 1.0, **command)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        other = asyncio.create_task(
            database.store.append_update(
                second.edit(
                    b"command-second-document", database.now + 1.0, **command
                )
            )
        )
        # Release only once the other persist waits or has finished, so it
        # decides while the first one is still in flight.
        await _waits_on_a_lock_before_it_finishes(database, other)
    finally:
        paused.release.set()
    held_result, other_result = await asyncio.gather(
        held, other, return_exceptions=True
    )

    assert isinstance(held_result, PersistedCollaborationUpdate), held_result
    assert held_result.sequence == 1
    assert isinstance(other_result, CollaborationConflict), other_result
    assert other_result.reason == "command_conflict"
    assert await _update_count(database, second.document_id) == 0


@pytest.mark.asyncio
async def test_a_command_id_in_flight_in_one_tenant_does_not_hold_up_another_tenant(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock that orders persists of one command id is scoped to the
    tenant: a persist in another tenant that carries the same id is not held
    up by it."""
    (document_a,) = await _writable_documents(database, "command_tenant_a")
    (document_b,) = await _writable_documents(
        database,
        "command_tenant_b",
        tenant_id=TENANT_B,
        owner_user_id=OWNER_B,
    )
    command = {
        "command_id": uuid.uuid4(),
        "command_payload_hash": _sha256(b"command-in-two-tenants"),
    }

    class _Abandoned(Exception):
        """Ends the held persist without writing, so only the wait counts."""

    original = database.store._persist_patch_states
    reached, release = asyncio.Event(), asyncio.Event()

    async def hold_then_abandon(session: AsyncSession, **kwargs: Any) -> Any:
        if kwargs["update_record"].tenant_id == TENANT_A:
            reached.set()
            await release.wait()
            raise _Abandoned
        return await original(session, **kwargs)

    monkeypatch.setattr(database.store, "_persist_patch_states", hold_then_abandon)
    held = asyncio.create_task(
        database.store.append_update(
            document_a.edit(b"command-tenant-a", database.now + 1.0, **command)
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        other = asyncio.create_task(
            database.store.append_update(
                document_b.edit(b"command-tenant-b", database.now + 1.0, **command)
            )
        )
        other_waited = await _waits_on_a_lock_before_it_finishes(database, other)
    finally:
        release.set()
    persisted = await other

    with pytest.raises(_Abandoned):
        await held
    assert other_waited is False
    assert persisted.sequence == 1


@pytest.mark.asyncio
async def test_updates_of_one_document_still_serialize_on_its_row(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two members' leases on one document: the second persist waits for the
    first and takes the next sequence."""
    (document,) = await _writable_documents(database, "member_order")
    session_id = f"session:{document.document_id}:second"
    await _seed_browser_session(
        database, tenant_id=TENANT_A, user_id=OWNER_A, session_id=session_id
    )
    lease_id = uuid.uuid4()
    second_lease = CollaborationLease(
        lease_id=lease_id,
        token_hash=_sha256(f"token:{lease_id}".encode()),
        tenant_id=TENANT_A,
        document_id=document.document_id,
        generation=1,
        user_id=OWNER_A,
        permission="edit",
        session_id=session_id,
        issued_at=database.now,
        expires_at=database.now + 3_600.0,
        last_validated_at=database.now,
    )
    await database.store.issue_lease(
        second_lease,
        max_active=5,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    _pause_inside(
        monkeypatch, database.store, "_persist_patch_states", after=False
    )
    first, paused = _start_paused(
        lambda: database.store.append_update(
            document.edit(b"member-order-first", database.now + 1.0)
        )
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        second = asyncio.create_task(
            database.store.append_update(
                replace(document, lease=second_lease).edit(
                    b"member-order-second", database.now + 1.0
                )
            )
        )
        second_waited = await _waits_on_a_lock_before_it_finishes(
            database, second
        )
    finally:
        paused.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert [getattr(result, "sequence", result) for result in results] == [1, 2]
    assert second_waited is True


@pytest.mark.asyncio
async def test_guest_updates_of_one_document_still_serialize_on_its_row(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guests take no access row lock, so only the document row orders two
    guests' persists of one document."""
    document_id = "ed_collaboration_fence_guest_order"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    writers = [
        await _authorize_guest(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            created_by_user_id=OWNER_A,
            permission="edit",
            display_name=f"Gast {index}",
        )
        for index in range(2)
    ]

    def guest_edit(index: int, payload: bytes) -> PersistCollaborationUpdate:
        instance, lease = writers[index]
        return _guest_direct_update(
            document_id=document_id,
            instance=instance,
            lease=lease,
            payload=payload,
            now=database.now + 1.0,
        )

    _pause_inside(
        monkeypatch, database.store, "_persist_patch_states", after=False
    )
    first, paused = _start_paused(
        lambda: database.store.append_update(guest_edit(0, b"guest-order-first"))
    )
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=5.0)
        second = asyncio.create_task(
            database.store.append_update(guest_edit(1, b"guest-order-second"))
        )
        second_waited = await _waits_on_a_lock_before_it_finishes(
            database, second
        )
    finally:
        paused.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert [getattr(result, "sequence", result) for result in results] == [1, 2]
    assert second_waited is True


@pytest.mark.asyncio
async def test_patch_membership_cannot_shrink_or_change_under_another_actor(
    database: _DatabaseHarness,
) -> None:
    """Only the patch author may monotonically add active suggestion IDs."""
    document_id = "ed_collaboration_patch_membership"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
        permission="suggest",
    )
    patch_id, first_id = await _create_human_patch(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        actor_user_id=OWNER_A,
        instance=instance,
        lease=lease,
    )
    second_id = str(uuid.uuid4())
    second_descriptor = CollaborationSuggestion(
        suggestion_id=second_id,
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 2.0,
        kind="insertion",
    )
    expanded = CollaborationPatchState(
        patch_id=patch_id,
        author_id=OWNER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(first_id, second_id),
        kinds=("insertion",),
    )
    await database.store.append_update(
        _suggestion_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"expand-membership",
            suggestions=(second_descriptor,),
            patches=(expanded,),
            now=database.now + 2.0,
            expected_sequence=1,
        )
    )
    shrunken = replace(expanded, active_suggestion_ids=(second_id,))

    with pytest.raises(CollaborationConflict) as shrink_conflict:
        await database.store.append_update(
            _suggestion_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"shrink-membership",
                suggestions=(),
                patches=(shrunken,),
                now=database.now + 3.0,
                expected_sequence=2,
            )
        )
    assert shrink_conflict.value.reason == "patch_membership_shrink"

    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(editor_patches)
                .where(editor_patches.c.patch_id == patch_id)
                .values(created_by_user_id=OWNER_B)
            )
    foreign_authored = replace(expanded, author_id=OWNER_B)
    with pytest.raises(CollaborationConflict) as author_conflict:
        await database.store.append_update(
            _suggestion_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                actor_user_id=OWNER_A,
                instance=instance,
                lease=lease,
                payload=b"foreign-author-membership",
                suggestions=(),
                patches=(foreign_authored,),
                now=database.now + 4.0,
                expected_sequence=2,
            )
        )
    assert author_conflict.value.reason == "patch_author_conflict"

    async with database.session_factory() as session:
        patch = (
            await session.execute(
                select(editor_patches).where(editor_patches.c.patch_id == patch_id)
            )
        ).mappings().one()
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert patch["suggestion_ids"] == [first_id, second_id]
    assert persisted_sequence == 2


@pytest.mark.asyncio
async def test_open_patch_keyset_pages_past_two_hundred(
    database: _DatabaseHarness,
) -> None:
    """Published patches paginate completely while private AI rows stay hidden."""
    document_id = "ed_collaboration_many_open_patches"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    published_ids = tuple(str(uuid.uuid4()) for _ in range(205))
    private_ids = tuple(str(uuid.uuid4()) for _ in range(3))
    rows: list[dict[str, object]] = []
    for index, patch_id in enumerate(published_ids):
        suggestion_id = str(uuid.uuid4())
        created_at = database.now + index
        rows.append(
            {
                "patch_id": patch_id,
                "tenant_id": TENANT_A,
                "document_id": document_id,
                "run_id": None,
                "source": "human",
                "status": "pending",
                "edits": [
                    {
                        "suggestion_id": suggestion_id,
                        "patch_id": patch_id,
                        "author_id": str(OWNER_A),
                        "created_at": created_at,
                        "kind": "insertion",
                    }
                ],
                "summary": "",
                "warnings": [],
                "revision_before": DOCUMENT_REVISION,
                "collaboration_generation": 1,
                "base_sequence": 0,
                "decision_sequence": None,
                "suggestion_ids": [suggestion_id],
                "applied_revision": None,
                "applied_edit_ids": None,
                "note": "",
                "created_by_user_id": OWNER_A,
                "decided_by_user_id": None,
                "command_id": None,
                "created_at": created_at,
                "decided_at": None,
            }
        )
    for index, patch_id in enumerate(private_ids, start=len(published_ids)):
        rows.append(
            {
                "patch_id": patch_id,
                "tenant_id": TENANT_A,
                "document_id": document_id,
                "run_id": None,
                "source": "agent",
                "status": "pending",
                "edits": [{"id": f"private-{index}", "text": "private"}],
                "summary": "Private AI work",
                "warnings": [],
                "revision_before": DOCUMENT_REVISION,
                "collaboration_generation": 1,
                "base_sequence": 0,
                "decision_sequence": None,
                "suggestion_ids": [],
                "applied_revision": None,
                "applied_edit_ids": None,
                "note": "",
                "created_by_user_id": OWNER_A,
                "decided_by_user_id": None,
                "command_id": None,
                "created_at": database.now + index,
                "decided_at": None,
            }
        )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(insert(editor_patches), rows)

    first = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind=None,
        limit=200,
    )
    assert len(first.patches) == 200
    assert first.next_cursor is not None
    second = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=first.next_cursor,
        author_user_id=None,
        suggestion_kind=None,
        limit=200,
    )
    observed_ids = tuple(
        patch.patch_id for patch in (*first.patches, *second.patches)
    )
    assert len(second.patches) == 5
    assert second.next_cursor is None
    assert set(observed_ids) == set(published_ids)
    assert set(observed_ids).isdisjoint(private_ids)


@pytest.mark.asyncio
async def test_open_patch_kind_filter_precedes_keyset_limit(
    database: _DatabaseHarness,
) -> None:
    """Interleaved kinds yield complete matching pages beyond the raw row cap."""
    document_id = "ed_collaboration_open_patch_kind_pages"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    rows: list[dict[str, object]] = []
    expected_ids: list[str] = []
    for index in range(410):
        patch_id = str(uuid.uuid4())
        suggestion_id = str(uuid.uuid4())
        created_at = database.now + index
        is_modification = index % 2 == 0
        is_human = index % 4 in {0, 1}
        if is_modification:
            expected_ids.append(patch_id)
        if is_human:
            edits = [
                {
                    "suggestion_id": suggestion_id,
                    "patch_id": patch_id,
                    "author_id": str(OWNER_A),
                    "created_at": created_at,
                    "kind": (
                        "modification" if is_modification else "insertion"
                    ),
                }
            ]
        else:
            edits = [
                {
                    "id": f"edit-{index}",
                    "find": "old" if is_modification else "",
                    "text": "new",
                    "position": "replace" if is_modification else "append",
                }
            ]
        rows.append(
            {
                "patch_id": patch_id,
                "tenant_id": TENANT_A,
                "document_id": document_id,
                "run_id": None,
                "source": "human" if is_human else "agent",
                "status": "pending",
                "edits": edits,
                "summary": "",
                "warnings": [],
                "revision_before": DOCUMENT_REVISION,
                "collaboration_generation": 1,
                "base_sequence": 0,
                "decision_sequence": None,
                "suggestion_ids": [suggestion_id],
                "applied_revision": None,
                "applied_edit_ids": None,
                "note": "",
                "created_by_user_id": OWNER_A,
                "decided_by_user_id": None,
                "command_id": None,
                "created_at": created_at,
                "decided_at": None,
            }
        )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(insert(editor_patches), rows)

    first = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind="replacement",
        limit=200,
    )
    assert len(first.patches) == 200
    assert first.next_cursor is not None
    second = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=first.next_cursor,
        author_user_id=None,
        suggestion_kind="replacement",
        limit=200,
    )

    observed = (*first.patches, *second.patches)
    assert len(second.patches) == 5
    assert second.next_cursor is None
    assert all("replacement" in patch.kinds for patch in observed)
    assert {patch.patch_id for patch in observed} == set(expected_ids)


@pytest.mark.asyncio
async def test_open_patches_order_by_creation_whichever_unit_stamped_them(
    database: _DatabaseHarness,
) -> None:
    """Agent patches carry Unix seconds, typed and AI suggestions epoch
    milliseconds, older AI suggestions Unix seconds. The open list pages
    newest first by when each was made, never all seconds before all
    milliseconds."""
    document_id = "ed_collaboration_open_patch_units"
    await _seed_markdown_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    await _activate_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        owner_user_id=OWNER_A,
    )
    # One moment in Unix seconds and 10, 20 and 30 seconds after it, each in
    # the unit its writer uses.
    stamps = (
        ("agent", 1_790_942_600.0),
        ("human", 1_790_942_610_000.0),
        ("human", 1_790_942_620.0),
        ("human", 1_790_942_630_000.0),
    )
    oldest_first: list[str] = []
    rows: list[dict[str, object]] = []
    for source, created_at in stamps:
        patch_id = str(uuid.uuid4())
        suggestion_id = str(uuid.uuid4())
        oldest_first.append(patch_id)
        rows.append(
            {
                "patch_id": patch_id,
                "tenant_id": TENANT_A,
                "document_id": document_id,
                "run_id": None,
                "source": source,
                "status": "pending",
                "edits": (
                    [
                        {
                            "suggestion_id": suggestion_id,
                            "patch_id": patch_id,
                            "author_id": str(OWNER_A),
                            "created_at": created_at,
                            "kind": "insertion",
                        }
                    ]
                    if source == "human"
                    else [
                        {
                            "id": "edit-1",
                            "find": "",
                            "text": "neu",
                            "position": "append",
                        }
                    ]
                ),
                "summary": "",
                "warnings": [],
                "revision_before": DOCUMENT_REVISION,
                "collaboration_generation": 1,
                "base_sequence": 0,
                "decision_sequence": None,
                "suggestion_ids": [suggestion_id],
                "applied_revision": None,
                "applied_edit_ids": None,
                "note": "",
                "created_by_user_id": OWNER_A,
                "decided_by_user_id": None,
                "command_id": None,
                "created_at": created_at,
                "decided_at": None,
            }
        )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(insert(editor_patches), rows)

    first = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind=None,
        limit=2,
    )
    assert first.next_cursor is not None
    second = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=first.next_cursor,
        author_user_id=None,
        suggestion_kind=None,
        limit=2,
    )
    paged = [patch.patch_id for patch in (*first.patches, *second.patches)]
    assert paged == list(reversed(oldest_first))
    assert second.next_cursor is None
    # Each patch keeps the time as it was written.
    assert [patch.created_at for patch in (*first.patches, *second.patches)] == [
        created_at for _source, created_at in reversed(stamps)
    ]


@pytest.mark.asyncio
async def test_restricted_role_enforces_tenant_rls_for_reads_and_writes(
    database: _DatabaseHarness,
) -> None:
    """The app role sees its tenant only and cannot forge another tenant row."""
    document_a = "ed_collaboration_rls_a"
    document_b = "ed_collaboration_rls_b"
    for tenant_id, document_id, owner_user_id in (
        (TENANT_A, document_a, OWNER_A),
        (TENANT_B, document_b, OWNER_B),
    ):
        await _seed_markdown_document(
            database,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_user_id=owner_user_id,
        )
        await _activate_document(
            database,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_user_id=owner_user_id,
        )

    with pytest.raises(CollaborationDocumentNotFound):
        await database.store.load_state(
            tenant_id=TENANT_A,
            document_id=document_b,
        )

    async with tenant_session(
        database.session_factory,
        tenant_id=TENANT_A,
        app_role=APP_ROLE,
    ) as session:
        visible_snapshots = (
            await session.execute(
                select(
                    editor_collaboration_snapshots.c.tenant_id,
                    editor_collaboration_snapshots.c.document_id,
                )
            )
        ).all()
    assert [(row.tenant_id, row.document_id) for row in visible_snapshots] == [
        (TENANT_A, document_a)
    ]

    with pytest.raises(DBAPIError):
        async with tenant_session(
            database.session_factory,
            tenant_id=TENANT_A,
            app_role=APP_ROLE,
        ) as session:
            await session.execute(
                insert(editor_collaboration_instances).values(
                    slot="primary",
                    tenant_id=TENANT_B,
                    instance_id="forged-cross-tenant-instance",
                    epoch=1,
                    lease_expires_at=database.now + 10.0,
                    updated_at=database.now,
                )
            )

    loaded_b = await database.store.load_state(
        tenant_id=TENANT_B,
        document_id=document_b,
    )
    assert loaded_b.document.tenant_id == TENANT_B
    assert loaded_b.snapshot.covered_sequence == 0


# -- Rejection by edit -------------------------------------------------------
#
# A writer with edit rights may delete or rewrite text that holds pending
# suggestions. The sidecar names the suggestions such an update removed; the
# store rejects them in the update's transaction, with the actor as the one
# who decided, and writes one audit entry per suggestion naming its author.


async def _share_document(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    recipient_user_id: uuid.UUID,
    granted_by_user_id: uuid.UUID,
    permission: str,
) -> uuid.UUID:
    """Grant and accept one member share of a document."""
    share_id = uuid.uuid4()
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(resource_shares).values(
                    id=share_id,
                    tenant_id=tenant_id,
                    recipient_user_id=recipient_user_id,
                    resource_type="editor_document",
                    resource_id=document_id,
                    permission=permission,
                    granted_by_user_id=granted_by_user_id,
                    accepted_at=func.now(),
                )
            )
    return share_id


async def _authorize_guest(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    created_by_user_id: uuid.UUID,
    permission: CollaborationPermission,
    display_name: str,
) -> tuple[CollaborationInstanceLease, CollaborationLease]:
    """Open one guest link with one guest identity and a live guest lease."""
    link_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    now = database.now
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_document_share_links).values(
                    id=link_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    generation=1,
                    label="Review",
                    permission=permission,
                    token_digest=_sha256(f"link:{link_id}".encode()),
                    password_hash="argon2-test-hash",
                    created_by_user_id=created_by_user_id,
                    revision=1,
                    expires_at=now + 86_400.0,
                    created_at=now,
                    updated_at=now,
                    last_command_id=uuid.uuid4(),
                    last_command_payload_hash=_sha256(f"cmd:{link_id}".encode()),
                    last_command_kind="create",
                )
            )
            await session.execute(
                insert(editor_document_guest_identities).values(
                    id=identity_id,
                    tenant_id=tenant_id,
                    link_id=link_id,
                    document_id=document_id,
                    generation=1,
                    display_name=display_name,
                    session_token_digest=_sha256(f"guest:{identity_id}".encode()),
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + 86_400.0,
                )
            )
    instance = await database.store.acquire_instance(
        tenant_id=tenant_id,
        instance_id="node-primary",
        now=now,
        lease_seconds=5_000.0,
    )
    lease_id = uuid.uuid4()
    lease = CollaborationLease(
        lease_id=lease_id,
        token_hash=_sha256(f"token:{lease_id}".encode()),
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        user_id=None,
        permission=permission,
        session_id=None,
        issued_at=now,
        expires_at=now + 3_600.0,
        last_validated_at=now,
        actor_kind="guest",
        guest_identity_id=identity_id,
        guest_link_id=link_id,
    )
    await database.store.issue_lease(
        lease,
        max_active=5,
        max_issued_per_window=30,
        issued_since=now - 60.0,
    )
    return instance, lease


def _actor_fields(lease: CollaborationLease) -> dict[str, object]:
    """The actor union of an update written under one lease."""
    if lease.actor_kind == "guest":
        return {
            "actor_kind": "guest",
            "actor_user_id": None,
            "actor_guest_identity_id": lease.guest_identity_id,
        }
    return {"actor_kind": "human", "actor_user_id": lease.user_id}


async def _propose(
    database: _DatabaseHarness,
    *,
    tenant_id: str,
    document_id: str,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    suggestion_ids: tuple[str, ...],
    now: float,
) -> str:
    """Persist one patch of insertions written under ``lease``."""
    author_id = lease.guest_identity_id or lease.user_id
    assert author_id is not None
    patch_id = str(uuid.uuid4())
    payload = f"propose:{patch_id}".encode()
    await database.store.append_update(
        PersistCollaborationUpdate(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=1,
            instance_id=instance.instance_id,
            instance_epoch=instance.epoch,
            lease_id=lease.lease_id,
            update_hash=_sha256(payload),
            update_bytes=payload,
            change_kind="suggestion",
            suggestion_ids=suggestion_ids,
            suggestions=tuple(
                CollaborationSuggestion(
                    suggestion_id=suggestion_id,
                    patch_id=patch_id,
                    author_id=author_id,
                    created_at=now,
                    kind="insertion",
                )
                for suggestion_id in suggestion_ids
            ),
            patches=(
                CollaborationPatchState(
                    patch_id=patch_id,
                    author_id=author_id,
                    created_at=now,
                    active_suggestion_ids=suggestion_ids,
                    kinds=("insertion",),
                ),
            ),
            now=now,
            **_actor_fields(lease),  # type: ignore[arg-type]
        )
    )
    return patch_id


def _rejecting_update(
    *,
    tenant_id: str,
    document_id: str,
    instance: CollaborationInstanceLease,
    lease: CollaborationLease,
    payload: bytes,
    rejected: tuple[str, ...],
    patches: tuple[CollaborationPatchState, ...],
    now: float,
    change_kind: str = "direct",
    suggestions: tuple[CollaborationSuggestion, ...] = (),
) -> PersistCollaborationUpdate:
    """An edit that removed suggestions, as the sidecar reports it."""
    return PersistCollaborationUpdate(
        tenant_id=tenant_id,
        document_id=document_id,
        generation=1,
        instance_id=instance.instance_id,
        instance_epoch=instance.epoch,
        lease_id=lease.lease_id,
        update_hash=_sha256(payload),
        update_bytes=payload,
        change_kind=change_kind,  # type: ignore[arg-type]
        suggestion_ids=tuple(item.suggestion_id for item in suggestions),
        suggestions=suggestions,
        patches=patches,
        rejected_suggestion_ids=rejected,
        now=now,
        **_actor_fields(lease),  # type: ignore[arg-type]
    )


def _emptied(
    patch_id: str, author_id: uuid.UUID, created_at: float
) -> CollaborationPatchState:
    return CollaborationPatchState(
        patch_id=patch_id,
        author_id=author_id,
        created_at=created_at,
        active_suggestion_ids=(),
        kinds=(),
    )


async def _patch_row(database: _DatabaseHarness, patch_id: str) -> dict[str, object]:
    async with database.session_factory() as session:
        return dict(
            (
                await session.execute(
                    select(editor_patches).where(editor_patches.c.patch_id == patch_id)
                )
            ).mappings().one()
        )


async def _rejection_audit(
    database: _DatabaseHarness, document_id: str
) -> list[dict[str, object]]:
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(
                    audit_log.c.actor_user_id,
                    audit_log.c.actor_type,
                    audit_log.c.detail,
                )
                .where(
                    audit_log.c.resource_id == document_id,
                    # Spelled out: the audit trail's action name is part of
                    # the contract its readers query by.
                    audit_log.c.action == "editor.collaboration_suggestion.removed_by_edit",
                )
                .order_by(audit_log.c.id)
            )
        ).all()
    return [
        {
            "actor_user_id": row.actor_user_id,
            "actor_type": row.actor_type,
            "detail": dict(row.detail),
        }
        for row in rows
    ]


async def _reviewed_document(
    database: _DatabaseHarness, document_id: str, reviewer_permission: str = "suggest"
) -> None:
    """OWNER_A's collaboration document, shared with REVIEWER_A."""
    await _seed_markdown_document(
        database, tenant_id=TENANT_A, document_id=document_id, owner_user_id=OWNER_A
    )
    await _activate_document(
        database, tenant_id=TENANT_A, document_id=document_id, owner_user_id=OWNER_A
    )
    await _share_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        recipient_user_id=REVIEWER_A,
        granted_by_user_id=OWNER_A,
        permission=reviewer_permission,
    )


@pytest.mark.asyncio
async def test_an_edit_removes_another_members_suggestion_in_its_transaction(
    database: _DatabaseHarness,
) -> None:
    """The patch is decided as rejected by the editor at the edit's sequence,
    the row names the suggestion, and the audit entry names author and actor."""
    document_id = "ed_collaboration_reject_member"
    await _reviewed_document(database, document_id)
    _, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=reviewer_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )

    persisted = await database.store.append_update(
        _rejecting_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=editor_lease,
            payload=b"delete-the-paragraph",
            rejected=(suggestion_id,),
            patches=(_emptied(patch_id, REVIEWER_A, database.now + 1.0),),
            now=database.now + 2.0,
        )
    )

    assert persisted.sequence == 2
    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "rejected"
    assert patch["decided_by_user_id"] == OWNER_A
    assert patch["decided_by_guest_identity_id"] is None
    assert patch["decision_sequence"] == 2
    assert patch["decided_at"] == database.now + 2.0
    assert patch["applied_edit_ids"] is None
    assert patch["suggestion_ids"] == [suggestion_id]
    async with database.session_factory() as session:
        row = (
            await session.execute(
                select(
                    editor_collaboration_updates.c.change_kind,
                    editor_collaboration_updates.c.rejected_suggestion_ids,
                ).where(
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.sequence == 2,
                )
            )
        ).one()
    assert row.change_kind == "direct"
    assert row.rejected_suggestion_ids == [suggestion_id]
    assert await _rejection_audit(database, document_id) == [
        {
            "actor_user_id": OWNER_A,
            "actor_type": "user",
            "detail": {
                "generation": 1,
                "sequence": 2,
                "suggestion_id": suggestion_id,
                "patch_id": patch_id,
                "author_kind": "user",
                "author_user_id": str(REVIEWER_A),
                "patch_rejected": True,
                "actor_kind": "user",
                "actor_user_id": str(OWNER_A),
                "suggestion_kind": "insertion",
            },
        }
    ]
    rejections = await database.store.list_suggestion_rejections(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        sequences=(2,),
        limit=10,
    )
    assert [(item.suggestion_id, item.author_user_id) for item in rejections] == [
        (suggestion_id, REVIEWER_A)
    ]
    activity = await database.store.list_activity(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before_sequence=None,
        author_user_id=None,
        change_kind=None,
        limit=10,
    )
    assert activity[0].rejected_suggestion_ids == (suggestion_id,)
    open_patches = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind=None,
        limit=10,
    )
    assert open_patches.patches == ()


@pytest.mark.asyncio
async def test_an_edit_that_removes_one_suggestion_of_a_patch_keeps_the_rest_pending(
    database: _DatabaseHarness,
) -> None:
    document_id = "ed_collaboration_reject_shrink"
    await _reviewed_document(database, document_id)
    _, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    first, second = sorted((str(uuid.uuid4()), str(uuid.uuid4())))
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=reviewer_lease,
        suggestion_ids=(first, second),
        now=database.now + 1.0,
    )

    await database.store.append_update(
        _rejecting_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=editor_lease,
            payload=b"delete-one-suggestion",
            rejected=(first,),
            patches=(
                CollaborationPatchState(
                    patch_id=patch_id,
                    author_id=REVIEWER_A,
                    created_at=database.now + 1.0,
                    active_suggestion_ids=(second,),
                    kinds=("insertion",),
                ),
            ),
            now=database.now + 2.0,
        )
    )

    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "pending"
    assert patch["suggestion_ids"] == [second]
    assert [item["suggestion_id"] for item in patch["edits"]] == [second]
    [entry] = await _rejection_audit(database, document_id)
    assert entry["detail"]["suggestion_id"] == first
    assert entry["detail"]["patch_rejected"] is False


@pytest.mark.parametrize("change_kind", ["direct", "suggestion"])
@pytest.mark.asyncio
async def test_removal_by_edit_needs_edit_access_now_not_only_an_edit_lease(
    database: _DatabaseHarness,
    change_kind: str,
) -> None:
    """A writer downgraded to suggest keeps an edit lease for its lifetime;
    rejecting another person's suggestion checks the current access."""
    document_id = f"ed_collaboration_reject_downgrade_{change_kind}"
    await _reviewed_document(database, document_id, reviewer_permission="edit")
    _, author_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    instance, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="edit",
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=author_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(resource_shares)
                .where(
                    resource_shares.c.resource_id == document_id,
                    resource_shares.c.recipient_user_id == REVIEWER_A,
                )
                .values(permission="suggest")
            )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _rejecting_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                instance=instance,
                lease=reviewer_lease,
                payload=f"downgraded-{change_kind}".encode(),
                rejected=(suggestion_id,),
                patches=(_emptied(patch_id, OWNER_A, database.now + 1.0),),
                now=database.now + 2.0,
                change_kind=change_kind,
            )
        )

    assert refused.value.reason == "access_revoked"
    assert (await _patch_row(database, patch_id))["status"] == "pending"
    assert await _rejection_audit(database, document_id) == []


@pytest.mark.parametrize("actor", ["member", "guest"])
@pytest.mark.asyncio
async def test_a_suggestion_row_under_an_edit_lease_needs_edit_access_now(
    database: _DatabaseHarness,
    actor: str,
) -> None:
    """The service judges an edit lease's update with edit rights, so a row
    it reports as a suggestion may also change text directly or shorten
    another person's pending suggestion. A writer downgraded to suggest keeps
    the lease for its lifetime; the row checks the current access, as the
    lease's own introspection does."""
    document_id = f"ed_collaboration_edit_lease_suggestion_{actor}"
    await _reviewed_document(database, document_id, reviewer_permission="edit")
    if actor == "member":
        instance, lease = await _authorize_writer(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            user_id=REVIEWER_A,
            permission="edit",
        )
        downgrade = (
            update(resource_shares)
            .where(
                resource_shares.c.resource_id == document_id,
                resource_shares.c.recipient_user_id == REVIEWER_A,
            )
            .values(permission="suggest")
        )
    else:
        instance, lease = await _authorize_guest(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            created_by_user_id=OWNER_A,
            permission="edit",
            display_name="Gina Gast",
        )
        downgrade = (
            update(editor_document_share_links)
            .where(editor_document_share_links.c.id == lease.guest_link_id)
            .values(permission="suggest")
        )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(downgrade)

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await _propose(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=lease,
            suggestion_ids=(str(uuid.uuid4()),),
            now=database.now + 1.0,
        )

    # A member's access is read from the document's shares, a guest's from
    # the link, which the lease branch already requires at the minimum.
    assert refused.value.reason == (
        "access_revoked" if actor == "member" else "session_invalid"
    )
    async with database.session_factory() as session:
        stored = (
            await session.execute(
                select(func.count()).where(
                    editor_collaboration_updates.c.document_id == document_id
                )
            )
        ).scalar_one()
    assert stored == 0


@pytest.mark.asyncio
async def test_removal_by_edit_is_refused_under_a_suggest_lease(
    database: _DatabaseHarness,
) -> None:
    document_id = "ed_collaboration_reject_suggest_lease"
    await _reviewed_document(database, document_id)
    _, author_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    instance, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=author_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )

    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await database.store.append_update(
            _rejecting_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                instance=instance,
                lease=reviewer_lease,
                payload=b"suggest-lease-rejects",
                rejected=(suggestion_id,),
                patches=(_emptied(patch_id, OWNER_A, database.now + 1.0),),
                now=database.now + 2.0,
                change_kind="suggestion",
            )
        )

    assert refused.value.reason == "permission_denied"
    assert (await _patch_row(database, patch_id))["status"] == "pending"


@pytest.mark.asyncio
async def test_a_guest_editor_removes_a_members_suggestion(
    database: _DatabaseHarness,
) -> None:
    """The patch author is read from its own creator column, whatever kind of
    actor rejects it; the audit entry names the guest identity."""
    document_id = "ed_collaboration_reject_by_guest"
    await _reviewed_document(database, document_id)
    _, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    instance, guest_lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=reviewer_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )

    await database.store.append_update(
        _rejecting_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=guest_lease,
            payload=b"guest-deletes-the-paragraph",
            rejected=(suggestion_id,),
            patches=(_emptied(patch_id, REVIEWER_A, database.now + 1.0),),
            now=database.now + 2.0,
        )
    )

    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "rejected"
    assert patch["decided_by_user_id"] is None
    assert patch["decided_by_guest_identity_id"] == guest_lease.guest_identity_id
    [entry] = await _rejection_audit(database, document_id)
    assert entry["actor_user_id"] is None
    assert entry["actor_type"] == "guest"
    assert entry["detail"]["actor_kind"] == "guest"
    assert entry["detail"]["actor_guest_identity_id"] == str(guest_lease.guest_identity_id)
    assert entry["detail"]["guest_identity_id"] == str(guest_lease.guest_identity_id)
    assert entry["detail"]["author_kind"] == "user"
    assert entry["detail"]["author_user_id"] == str(REVIEWER_A)


@pytest.mark.asyncio
async def test_a_member_removes_a_guests_suggestion_named_as_its_author(
    database: _DatabaseHarness,
) -> None:
    document_id = "ed_collaboration_reject_guest_author"
    await _reviewed_document(database, document_id)
    instance, guest_lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="suggest",
        display_name="Gustav Gast",
    )
    _, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    guest_id = guest_lease.guest_identity_id
    assert guest_id is not None
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=guest_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )
    assert (await _patch_row(database, patch_id))["created_by_guest_identity_id"] == guest_id
    pending = await database.store.list_open_patches(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        before=None,
        author_user_id=None,
        suggestion_kind=None,
        limit=10,
    )
    assert [
        (item.patch_id, item.author_user_id, item.author_guest_identity_id)
        for item in pending.patches
    ] == [(patch_id, None, guest_id)]

    await database.store.append_update(
        _rejecting_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=editor_lease,
            payload=b"member-deletes-the-guest-suggestion",
            rejected=(suggestion_id,),
            patches=(_emptied(patch_id, guest_id, database.now + 1.0),),
            now=database.now + 2.0,
        )
    )

    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "rejected"
    assert patch["decided_by_user_id"] == OWNER_A
    [entry] = await _rejection_audit(database, document_id)
    assert entry["detail"]["author_kind"] == "guest"
    assert entry["detail"]["author_guest_identity_id"] == str(guest_id)
    removals = await database.store.list_suggestion_rejections(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        sequences=(entry["detail"]["sequence"],),  # type: ignore[arg-type]
        limit=10,
    )
    assert [
        (item.suggestion_id, item.author_user_id, item.author_guest_identity_id)
        for item in removals
    ] == [(suggestion_id, None, guest_id)]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown_id", "rejected_suggestion_unknown"),
        ("still_active", "rejected_suggestion_unknown"),
        ("foreign_patch_without_removal", "patch_author_conflict"),
        ("foreign_patch_shrunk_without_rejection", "patch_author_conflict"),
        ("foreign_patch_grows", "patch_author_conflict"),
        ("no_patch_listed", "rejected_suggestion_unknown"),
    ],
)
@pytest.mark.asyncio
async def test_removal_by_edit_obeys_the_membership_contract(
    database: _DatabaseHarness,
    case: str,
    reason: str,
) -> None:
    """A removed id was pending in a listed patch and is gone from it; a
    foreign patch may only lose the suggestions the update removed."""
    document_id = f"ed_collaboration_reject_contract_{case}"
    await _reviewed_document(database, document_id)
    _, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    first, second = sorted((str(uuid.uuid4()), str(uuid.uuid4())))
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=reviewer_lease,
        suggestion_ids=(first, second),
        now=database.now + 1.0,
    )
    remaining = CollaborationPatchState(
        patch_id=patch_id,
        author_id=REVIEWER_A,
        created_at=database.now + 1.0,
        active_suggestion_ids=(second,),
        kinds=("insertion",),
    )
    rejected: tuple[str, ...] = (first,)
    patches: tuple[CollaborationPatchState, ...] = (remaining,)
    if case == "unknown_id":
        rejected = (first, str(uuid.uuid4()))
    elif case == "still_active":
        patches = (replace(remaining, active_suggestion_ids=(first, second)),)
    elif case == "foreign_patch_without_removal":
        # The update rejects a suggestion of another patch and lists this
        # foreign patch unchanged besides it.
        other_patch = await _propose(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=reviewer_lease,
            suggestion_ids=(str(uuid.uuid4()),),
            now=database.now + 1.5,
        )
        other = (await _patch_row(database, other_patch))["suggestion_ids"]
        rejected = (str(other[0]),)
        patches = (
            replace(remaining, active_suggestion_ids=(first, second)),
            _emptied(other_patch, REVIEWER_A, database.now + 1.5),
        )
    elif case == "foreign_patch_shrunk_without_rejection":
        patches = (_emptied(patch_id, REVIEWER_A, database.now + 1.0),)
    elif case == "foreign_patch_grows":
        patches = (
            replace(remaining, active_suggestion_ids=(second, str(uuid.uuid4()))),
        )
    elif case == "no_patch_listed":
        patches = ()

    sequence_before = 3 if case == "foreign_patch_without_removal" else 2
    with pytest.raises(CollaborationConflict) as refused:
        await database.store.append_update(
            _rejecting_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                instance=instance,
                lease=editor_lease,
                payload=f"contract-{case}".encode(),
                rejected=rejected,
                patches=patches,
                now=database.now + 2.0,
            )
        )

    assert refused.value.reason == reason
    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "pending"
    assert patch["suggestion_ids"] == [first, second]
    assert await _rejection_audit(database, document_id) == []
    async with database.session_factory() as session:
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert persisted_sequence == sequence_before - 1


@pytest.mark.parametrize(
    "mutate",
    [
        "decision",
        "descriptors_on_direct",
        "without_lease",
        "duplicate_ids",
    ],
)
@pytest.mark.asyncio
async def test_removal_by_edit_is_refused_outside_a_plain_edit(
    database: _DatabaseHarness,
    mutate: str,
) -> None:
    """Only a person's edit under a lease names removed suggestions; a direct
    edit carries no descriptors."""
    document_id = f"ed_collaboration_reject_shape_{mutate}"
    await _reviewed_document(database, document_id)
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    suggestion_id = str(uuid.uuid4())
    record = _rejecting_update(
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=editor_lease,
        payload=f"shape-{mutate}".encode(),
        rejected=(suggestion_id,),
        patches=(_emptied(str(uuid.uuid4()), REVIEWER_A, database.now),),
        now=database.now + 2.0,
    )
    if mutate == "decision":
        record = replace(
            record,
            change_kind="decision",
            decision="reject",
            decision_outcome="rejected",
            command_id=uuid.uuid4(),
            command_payload_hash=_sha256(b"decision"),
        )
    elif mutate == "descriptors_on_direct":
        other = str(uuid.uuid4())
        record = replace(
            record,
            suggestion_ids=(other,),
            suggestions=(
                CollaborationSuggestion(
                    suggestion_id=other,
                    patch_id=record.patches[0].patch_id,
                    author_id=OWNER_A,
                    created_at=database.now,
                    kind="insertion",
                ),
            ),
        )
    elif mutate == "without_lease":
        record = replace(record, lease_id=None, command_id=uuid.uuid4(), command_payload_hash=_sha256(b"x"))
    elif mutate == "duplicate_ids":
        record = replace(record, rejected_suggestion_ids=(suggestion_id, suggestion_id))

    with pytest.raises(ValueError):
        await database.store.append_update(record)


@pytest.mark.asyncio
async def test_an_edit_that_removes_the_writers_own_suggestion_withdraws_it(
    database: _DatabaseHarness,
) -> None:
    """A writer with edit rights who deletes their own pending suggestion
    withdraws it: the patch is rejected by its own author, and the audit
    entry names the same person as author and actor."""
    document_id = "ed_collaboration_reject_own"
    await _reviewed_document(database, document_id)
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        instance=instance,
        lease=editor_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )

    persisted = await database.store.append_update(
        _rejecting_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            instance=instance,
            lease=editor_lease,
            payload=b"delete-the-own-suggestion",
            rejected=(suggestion_id,),
            patches=(_emptied(patch_id, OWNER_A, database.now + 1.0),),
            now=database.now + 2.0,
        )
    )

    assert persisted.sequence == 2
    patch = await _patch_row(database, patch_id)
    assert patch["status"] == "rejected"
    assert patch["decided_by_user_id"] == OWNER_A
    assert patch["decision_sequence"] == 2
    [entry] = await _rejection_audit(database, document_id)
    assert entry["actor_user_id"] == OWNER_A
    assert entry["detail"]["author_kind"] == "user"
    assert entry["detail"]["author_user_id"] == str(OWNER_A)
    assert entry["detail"]["actor_user_id"] == str(OWNER_A)
    assert entry["detail"]["patch_rejected"] is True


@pytest.mark.asyncio
async def test_an_edit_cannot_remove_a_suggestion_of_another_document(
    database: _DatabaseHarness,
) -> None:
    """A removal names only suggestions pending in the edited document: a
    known patch of another document the writer can also edit stays pending,
    and nothing is appended or audited."""
    other_document_id = "ed_collaboration_reject_other_doc"
    document_id = "ed_collaboration_reject_this_doc"
    await _reviewed_document(database, other_document_id)
    await _reviewed_document(database, document_id)
    _, reviewer_lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=other_document_id,
        user_id=REVIEWER_A,
        permission="suggest",
    )
    instance, editor_lease = await _authorize_writer(
        database, tenant_id=TENANT_A, document_id=document_id, user_id=OWNER_A
    )
    suggestion_id = str(uuid.uuid4())
    patch_id = await _propose(
        database,
        tenant_id=TENANT_A,
        document_id=other_document_id,
        instance=instance,
        lease=reviewer_lease,
        suggestion_ids=(suggestion_id,),
        now=database.now + 1.0,
    )

    with pytest.raises(CollaborationConflict) as refused:
        await database.store.append_update(
            _rejecting_update(
                tenant_id=TENANT_A,
                document_id=document_id,
                instance=instance,
                lease=editor_lease,
                payload=b"reject-across-documents",
                rejected=(suggestion_id,),
                patches=(_emptied(patch_id, REVIEWER_A, database.now + 1.0),),
                now=database.now + 2.0,
            )
        )

    assert refused.value.reason == "patch_not_found"
    assert (await _patch_row(database, patch_id))["status"] == "pending"
    assert await _rejection_audit(database, document_id) == []
    assert await _rejection_audit(database, other_document_id) == []
    async with database.session_factory() as session:
        persisted_sequence = await session.scalar(
            select(editor_documents.c.persisted_sequence).where(
                editor_documents.c.id == document_id
            )
        )
    assert persisted_sequence == 0


# -- Lock order of deleting and revoking -------------------------------------
#
# Every transaction that touches a document's lease rows locks the document
# row first, after the user and membership rows its access check locks: an
# update, a lease rotation, deleting the document and revoking a guest link.
# Deleting or revoking therefore waits for a write or a rotation in flight,
# or the other way round, instead of holding a row the other one waits for;
# PostgreSQL would abort one of two such transactions as a deadlock. The
# interleavings below are forced: a call is held inside its transaction, or
# a third transaction holds a row, and PostgreSQL itself reports the wait
# (``pg_blocking_pids``) before the test lets the calls go on.


async def _settle(*tasks: "asyncio.Task[Any] | None") -> list[Any]:
    """Wait for every started task and return its result or its exception."""
    started = [task for task in tasks if task is not None]
    try:
        return await asyncio.wait_for(
            asyncio.gather(*started, return_exceptions=True),
            timeout=30.0,
        )
    finally:
        for task in started:
            if not task.done():
                task.cancel()
        await asyncio.gather(*started, return_exceptions=True)


def _guest_links(database: _DatabaseHarness) -> PostgresEditorGuestLinkStore:
    """The guest-link store over the test database."""
    return PostgresEditorGuestLinkStore(
        session_factory=database.session_factory,
        app_role=APP_ROLE,
    )


async def _revoke_guest_link(
    database: _DatabaseHarness,
    *,
    document_id: str,
    link_id: uuid.UUID | None,
    now: float,
) -> Any:
    """Revoke one guest link of an OWNER_A document at revision 1."""
    assert link_id is not None
    command_id = uuid.uuid4()
    return await _guest_links(database).revoke_link(
        tenant_id=TENANT_A,
        document_id=document_id,
        link_id=link_id,
        actor_user_id=OWNER_A,
        expected_revision=1,
        command_id=command_id,
        command_payload_hash=_sha256(str(command_id).encode()),
        now=now,
    )


def _delete_owner_a_document(
    database: _DatabaseHarness,
    document_id: str,
    *,
    now: float,
) -> "asyncio.Task[int]":
    """Start deleting one OWNER_A document as its owner."""
    return asyncio.create_task(
        database.store.tombstone_document(
            tenant_id=TENANT_A,
            document_id=document_id,
            owner_user_id=OWNER_A,
            now=now,
        )
    )


@pytest.mark.parametrize("writer", ["owner", "guest"])
@pytest.mark.asyncio
async def test_deleting_a_document_waits_for_an_update_that_holds_its_lease(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    """Deleting waits for an update in flight, which commits first.

    The update holds its lease row when the delete arrives. The delete
    revokes every lease of the document while it holds the document row,
    and the update needs that row too. The update locks the document before
    its lease, so the delete waits at the document, the update is persisted,
    and the delete then revokes the lease.
    """
    document_id = f"ed_collaboration_delete_during_update_{writer}"
    await _collaboration_document_of_owner_a(database, document_id)
    if writer == "owner":
        instance, lease = await _authorize_writer(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            user_id=OWNER_A,
        )
        record = _direct_update(
            tenant_id=TENANT_A,
            document_id=document_id,
            actor_user_id=OWNER_A,
            instance=instance,
            lease=lease,
            payload=b"owner-update-while-the-document-is-deleted",
            now=database.now + 1.0,
        )
    else:
        instance, lease = await _authorize_guest(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            created_by_user_id=OWNER_A,
            permission="edit",
            display_name="Gina Gast",
        )
        record = _guest_direct_update(
            document_id=document_id,
            instance=instance,
            lease=lease,
            payload=b"guest-update-while-the-document-is-deleted",
            now=database.now + 1.0,
        )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_lock_append_lease",
        after=True,
    )
    update_task, paused = _start_paused(
        lambda: database.store.append_update(record)
    )
    delete_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        delete_task = _delete_owner_a_document(
            database, document_id, now=database.now + 2.0
        )
        delete_waited = await _waits_on_a_lock_before_it_finishes(
            database, delete_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(update_task, delete_task)

    assert delete_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    persisted, generation = outcomes
    assert persisted.sequence == 1
    assert generation == 2
    assert await _lease_revoked_at(database, lease) == database.now + 2.0


@pytest.mark.asyncio
async def test_revoking_a_guest_link_waits_for_an_update_that_holds_its_lease(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking a guest link waits for the guest's update in flight.

    The revocation locks the document, then the link, then the leases
    opened through the link. The guest's update locks the document before
    its lease, so the revocation waits at the document, the update is
    persisted, and the revocation then revokes the lease.
    """
    document_id = "ed_collaboration_link_revoked_during_update"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_lock_append_lease",
        after=True,
    )
    update_task, paused = _start_paused(
        lambda: database.store.append_update(
            _guest_direct_update(
                document_id=document_id,
                instance=instance,
                lease=lease,
                payload=b"guest-update-while-the-link-is-revoked",
                now=database.now + 1.0,
            )
        )
    )
    revoke_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        revoke_task = asyncio.create_task(
            _revoke_guest_link(
                database,
                document_id=document_id,
                link_id=lease.guest_link_id,
                now=database.now + 2.0,
            )
        )
        revoke_waited = await _waits_on_a_lock_before_it_finishes(
            database, revoke_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(update_task, revoke_task)

    assert revoke_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    persisted, link = outcomes
    assert persisted.sequence == 1
    assert link.revoked_at == database.now + 2.0
    assert await _lease_revoked_at(database, lease) == database.now + 2.0


@pytest.mark.asyncio
async def test_deleting_a_document_waits_for_a_member_rotation(
    database: _DatabaseHarness,
) -> None:
    """A rotation of a member's lease and deleting the document both finish.

    A third transaction holds the member's user row. The rotation locks the
    rows its access check needs before any lease row, so it waits at that
    user row holding no lease. Deleting revokes the member's share and
    leases; its event for the member waits for the same user row. Once the
    row is free, the delete commits and the rotation finds its lease
    revoked. A rotation that locked its lease first would hold the lease the
    delete waits for, while waiting for the document the delete holds.
    """
    document_id = "ed_collaboration_delete_during_member_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    await _share_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        recipient_user_id=REVIEWER_A,
        granted_by_user_id=OWNER_A,
        permission="edit",
    )
    _, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 1.0,
        token=b"member-rotation-while-the-document-is-deleted",
    )
    rotation_task = delete_task = None
    try:
        async with database.session_factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(users.c.id)
                    .where(users.c.id == REVIEWER_A)
                    .with_for_update()
                )
                rotation_task = asyncio.create_task(
                    database.store.rotate_lease(
                        previous_lease_id=lease.lease_id,
                        previous_token_hash=lease.token_hash,
                        replacement=successor,
                        max_issued_per_window=30,
                        issued_since=database.now - 60.0,
                    )
                )
                await _wait_until_transactions_wait_on_locks(database)
                delete_task = _delete_owner_a_document(
                    database, document_id, now=database.now + 2.0
                )
                await _wait_until_transactions_wait_on_locks(database, waiters=2)
    finally:
        outcomes = await _settle(rotation_task, delete_task)

    rotated, generation = outcomes
    assert generation == 2, outcomes
    assert isinstance(rotated, CollaborationLeaseInvalid), outcomes
    assert rotated.reason == "lease_revoked"
    assert await _lease_revoked_at(database, lease) == database.now + 2.0


@pytest.mark.asyncio
async def test_deleting_a_document_waits_for_a_guest_rotation(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting waits for a guest's rotation in flight, which commits first.

    A guest's rotation runs no access check of a person, but the successor
    it inserts checks the document foreign key, which locks the document
    row. The rotation takes that lock before its lease rows, so the delete
    waits at the document, the rotation commits, and the delete revokes
    the successor too.
    """
    document_id = "ed_collaboration_delete_during_guest_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 1.0,
        token=b"guest-rotation-while-the-document-is-deleted",
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        after=True,
    )
    rotation_task, paused = _start_paused(
        lambda: database.store.rotate_lease(
            previous_lease_id=lease.lease_id,
            previous_token_hash=lease.token_hash,
            replacement=successor,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
    )
    delete_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        delete_task = _delete_owner_a_document(
            database, document_id, now=database.now + 2.0
        )
        delete_waited = await _waits_on_a_lock_before_it_finishes(
            database, delete_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(rotation_task, delete_task)

    assert delete_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    rotated, generation = outcomes
    assert rotated == successor
    assert generation == 2
    assert await _lease_revoked_at(database, successor) == database.now + 2.0


@pytest.mark.asyncio
async def test_an_update_and_a_rotation_of_its_lease_do_not_deadlock(
    database: _DatabaseHarness,
) -> None:
    """A member's update and a rotation of its lease both finish.

    A third transaction holds the member's user row, so both calls queue
    there before they lock a document or lease row. Once it is free, the
    one that takes the document first finishes before the other reaches a
    lease row. If only one of the two locked the document before its lease,
    each could hold a row the other waits for.
    """
    document_id = "ed_collaboration_member_update_during_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    await _share_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        recipient_user_id=REVIEWER_A,
        granted_by_user_id=OWNER_A,
        permission="edit",
    )
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=REVIEWER_A,
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 1.5,
        token=b"member-rotation-while-an-update-is-in-flight",
    )
    update_task = rotation_task = None
    try:
        async with database.session_factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(users.c.id)
                    .where(users.c.id == REVIEWER_A)
                    .with_for_update()
                )
                update_task = asyncio.create_task(
                    database.store.append_update(
                        _direct_update(
                            tenant_id=TENANT_A,
                            document_id=document_id,
                            actor_user_id=REVIEWER_A,
                            instance=instance,
                            lease=lease,
                            payload=b"member-update-while-its-lease-rotates",
                            now=database.now + 1.0,
                        )
                    )
                )
                await _wait_until_transactions_wait_on_locks(database)
                rotation_task = asyncio.create_task(
                    database.store.rotate_lease(
                        previous_lease_id=lease.lease_id,
                        previous_token_hash=lease.token_hash,
                        replacement=successor,
                        max_issued_per_window=30,
                        issued_since=database.now - 60.0,
                    )
                )
                await _wait_until_transactions_wait_on_locks(database, waiters=2)
    finally:
        outcomes = await _settle(update_task, rotation_task)

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    persisted, rotated = outcomes
    assert persisted.sequence == 1
    assert rotated == successor


@pytest.mark.asyncio
async def test_a_guest_update_and_a_rotation_of_its_lease_do_not_deadlock(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guest's update waits for a rotation of its lease in flight.

    The rotation holds the guest's lease, and the successor it inserts
    checks the document foreign key. It locks the document row before the
    lease, so the update, which needs the document before its lease too,
    waits for the rotation and then continues under the successor.
    """
    document_id = "ed_collaboration_guest_update_during_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    instance, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 1.5,
        token=b"guest-rotation-while-an-update-is-in-flight",
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        after=True,
    )
    rotation_task, paused = _start_paused(
        lambda: database.store.rotate_lease(
            previous_lease_id=lease.lease_id,
            previous_token_hash=lease.token_hash,
            replacement=successor,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
    )
    update_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        update_task = asyncio.create_task(
            database.store.append_update(
                _guest_direct_update(
                    document_id=document_id,
                    instance=instance,
                    lease=lease,
                    payload=b"guest-update-while-its-lease-rotates",
                    now=database.now + 1.0,
                )
            )
        )
        update_waited = await _waits_on_a_lock_before_it_finishes(
            database, update_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(rotation_task, update_task)

    assert update_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    rotated, persisted = outcomes
    assert rotated == successor
    assert persisted.sequence == 1


@pytest.mark.asyncio
async def test_deleting_after_a_rotation_stamped_later_revokes_it(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting revokes a lease minted after the delete's own stamp.

    The API stamps a request when it arrives, so a rotation that arrived
    after the delete can still lock the document first and commit a
    successor issued after the delete's stamp. The delete revokes that
    successor no earlier than it was issued; a revocation stamped before
    its lease would violate the lease timestamps and fail the delete.
    """
    document_id = "ed_collaboration_delete_after_a_later_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 3.0,
        token=b"rotation-stamped-after-the-delete",
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        after=True,
    )
    rotation_task, paused = _start_paused(
        lambda: database.store.rotate_lease(
            previous_lease_id=lease.lease_id,
            previous_token_hash=lease.token_hash,
            replacement=successor,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
    )
    delete_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        delete_task = _delete_owner_a_document(
            database, document_id, now=database.now + 2.0
        )
        delete_waited = await _waits_on_a_lock_before_it_finishes(
            database, delete_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(rotation_task, delete_task)

    assert delete_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    rotated, generation = outcomes
    assert rotated == successor
    assert generation == 2
    assert await _lease_revoked_at(database, successor) == database.now + 3.0


@pytest.mark.asyncio
async def test_revoking_a_link_after_a_guest_rotation_stamped_later_revokes_it(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking a guest link revokes a guest lease minted after its stamp.

    A guest's rotation that arrived after the revocation can lock the
    document first and commit a successor issued after the revocation's
    stamp. The revocation stamps that successor no earlier than it was
    issued.
    """
    document_id = "ed_collaboration_link_revoked_after_a_later_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    successor = _rotation_stamped_after(
        lease,
        issued_at=database.now + 3.0,
        token=b"guest-rotation-stamped-after-the-revocation",
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        after=True,
    )
    rotation_task, paused = _start_paused(
        lambda: database.store.rotate_lease(
            previous_lease_id=lease.lease_id,
            previous_token_hash=lease.token_hash,
            replacement=successor,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
    )
    revoke_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        revoke_task = asyncio.create_task(
            _revoke_guest_link(
                database,
                document_id=document_id,
                link_id=lease.guest_link_id,
                now=database.now + 2.0,
            )
        )
        revoke_waited = await _waits_on_a_lock_before_it_finishes(
            database, revoke_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(rotation_task, revoke_task)

    assert revoke_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    rotated, link = outcomes
    assert rotated == successor
    assert link.revoked_at == database.now + 2.0
    assert await _lease_revoked_at(database, successor) == database.now + 3.0


@pytest.mark.asyncio
async def test_revoking_a_link_after_a_guest_identity_stamped_later_revokes_it(
    database: _DatabaseHarness,
) -> None:
    """Revoking a guest link revokes a guest identity opened after its stamp.

    Opening a link stamps the new guest identity with its own request time
    while it holds the link row, so an identity stamped after the
    revocation's request can commit before the revocation locks the link.
    The revocation stamps such an identity no earlier than it was created,
    and every older identity with its own time.
    """
    document_id = "ed_collaboration_link_revoked_after_a_later_identity"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    assert lease.guest_link_id is not None
    late_identity_id = uuid.uuid4()
    await _guest_links(database).create_guest_identity(
        EditorGuestIdentity(
            id=late_identity_id,
            tenant_id=TENANT_A,
            link_id=lease.guest_link_id,
            document_id=document_id,
            generation=1,
            display_name="Gero Gast",
            session_token_digest=_sha256(f"guest:{late_identity_id}".encode()),
            created_at=database.now + 3.0,
            last_seen_at=database.now + 3.0,
            expires_at=database.now + 86_000.0,
        ),
        verified_token_digest=_sha256(f"link:{lease.guest_link_id}".encode()),
        verified_password_hash="argon2-test-hash",
        stats_enabled=True,
        now=database.now + 3.0,
    )

    link = await _revoke_guest_link(
        database,
        document_id=document_id,
        link_id=lease.guest_link_id,
        now=database.now + 2.0,
    )

    assert link.revoked_at == database.now + 2.0
    async with database.session_factory() as session:
        stamps = dict(
            (
                await session.execute(
                    select(
                        editor_document_guest_identities.c.id,
                        editor_document_guest_identities.c.revoked_at,
                    ).where(
                        editor_document_guest_identities.c.link_id
                        == lease.guest_link_id
                    )
                )
            ).all()
        )
    assert stamps == {
        lease.guest_identity_id: database.now + 2.0,
        late_identity_id: database.now + 3.0,
    }
    assert await _lease_revoked_at(database, lease) == database.now + 2.0


# -- Opening a guest session across a credential rotation --------------------
#
# Unlocking a link resolves it by its token digest, checks the password
# against the hash it read without a lock, and only then opens the guest
# session in a second transaction that locks the link row. Rotating the
# password or the token replaces that pair under the same row lock and
# revokes the sessions that exist when it runs. The session is opened only
# if the link still carries the pair the unlock checked, so an unlock that
# checked the replaced pair yields no session in any order of the two.


async def _resolve_guest_link(
    links: PostgresEditorGuestLinkStore,
    *,
    link_id: uuid.UUID,
    now: float,
) -> Any:
    """Resolve a link from ``_authorize_guest`` the way an unlock does."""
    link, _title = await links.resolve_link(
        tenant_id=TENANT_A,
        token_digest=_sha256(f"link:{link_id}".encode()),
        now=now,
    )
    return link


async def _open_guest_session_with_verified_pair(
    links: PostgresEditorGuestLinkStore,
    link: Any,
    *,
    identity_id: uuid.UUID,
    now: float,
) -> Any:
    """Open a guest session with the pair of the link an unlock resolved."""
    return await links.create_guest_identity(
        EditorGuestIdentity(
            id=identity_id,
            tenant_id=link.tenant_id,
            link_id=link.id,
            document_id=link.document_id,
            generation=link.generation,
            display_name="Late Guest",
            session_token_digest=_sha256(f"guest:{identity_id}".encode()),
            created_at=now,
            last_seen_at=now,
            expires_at=link.expires_at,
        ),
        verified_token_digest=link.token_digest,
        verified_password_hash=link.password_hash,
        stats_enabled=True,
        now=now,
    )


async def _change_guest_link(
    links: PostgresEditorGuestLinkStore,
    *,
    document_id: str,
    link_id: uuid.UUID,
    change: str,
    now: float,
) -> Any:
    """Rotate the password or the token, or lower the permission, of a link
    from ``_authorize_guest`` at revision 1."""
    command_id = uuid.uuid4()
    return await links.update_link(
        tenant_id=TENANT_A,
        document_id=document_id,
        link_id=link_id,
        actor_user_id=OWNER_A,
        permission="suggest" if change == "permission" else None,
        expires_at=None,
        password_hash="argon2-rotated-hash" if change == "password" else None,
        token_digest=(
            _sha256(b"rotated-link-token") if change == "token" else None
        ),
        expected_revision=1,
        command_id=command_id,
        command_payload_hash=_sha256(str(command_id).encode()),
        command_kind=(
            "update" if change == "permission" else f"rotate_{change}"
        ),
        now=now,
    )


async def _issue_guest_edit_lease(
    database: _DatabaseHarness,
    *,
    document_id: str,
    identity_id: uuid.UUID,
    link_id: uuid.UUID,
    now: float,
) -> CollaborationLease:
    """Issue an edit lease to one guest identity of a link."""
    lease_id = uuid.uuid4()
    lease = CollaborationLease(
        lease_id=lease_id,
        token_hash=_sha256(f"late-token:{lease_id}".encode()),
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        user_id=None,
        permission="edit",
        session_id=None,
        issued_at=now,
        expires_at=now + 3_600.0,
        last_validated_at=now,
        actor_kind="guest",
        guest_identity_id=identity_id,
        guest_link_id=link_id,
    )
    await database.store.issue_lease(
        lease,
        max_active=5,
        max_issued_per_window=30,
        issued_since=now - 60.0,
    )
    return lease


async def _guest_identity_revoked_at(
    database: _DatabaseHarness, identity_id: uuid.UUID
) -> float | None | str:
    """The revocation stamp of one guest identity, or ``"absent"``."""
    async with database.session_factory() as session:
        row = (
            await session.execute(
                select(editor_document_guest_identities.c.revoked_at).where(
                    editor_document_guest_identities.c.id == identity_id
                )
            )
        ).one_or_none()
    return "absent" if row is None else row.revoked_at


async def _guest_link_open_counts(
    database: _DatabaseHarness, link_id: uuid.UUID
) -> tuple[int, int]:
    """The successful-open and session counters of one guest link."""
    async with database.session_factory() as session:
        row = (
            await session.execute(
                select(
                    editor_document_share_links.c.successful_open_count,
                    editor_document_share_links.c.session_count,
                ).where(editor_document_share_links.c.id == link_id)
            )
        ).one()
    return int(row.successful_open_count), int(row.session_count)


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", ["password", "token"])
async def test_an_unlock_verified_against_a_rotated_credential_opens_no_session(
    database: _DatabaseHarness,
    rotated: str,
) -> None:
    """A rotation that commits between an unlock's check and its session
    leaves that unlock without a session and without a lease.

    The unlock checked the pair it resolved; the rotation then replaced
    one half and revoked every session that existed. Opening the session
    afterwards compares the checked pair with the link's current one and
    refuses like a revoked link, so the rotation also ends the unlock that
    was still in flight.
    """
    document_id = f"ed_collaboration_unlock_after_{rotated}_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id = lease.guest_link_id
    assert link_id is not None
    links = _guest_links(database)
    link = await _resolve_guest_link(
        links, link_id=link_id, now=database.now + 1.0
    )
    await _change_guest_link(
        links,
        document_id=document_id,
        link_id=link_id,
        change=rotated,
        now=database.now + 2.0,
    )
    identity_id = uuid.uuid4()

    with pytest.raises(EditorGuestLinkExpired):
        await _open_guest_session_with_verified_pair(
            links, link, identity_id=identity_id, now=database.now + 1.0
        )

    assert await _guest_identity_revoked_at(database, identity_id) == "absent"
    assert await _guest_link_open_counts(database, link_id) == (0, 0)
    with pytest.raises(CollaborationDocumentNotFound):
        await _issue_guest_edit_lease(
            database,
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            now=database.now + 3.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", ["password", "token"])
async def test_an_unlock_waiting_on_a_credential_rotation_opens_no_session(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    rotated: str,
) -> None:
    """An unlock whose session waits on a rotation's lock of the link row is
    refused once the rotation commits.

    The rotation holds the link row with the new pair and has already
    revoked the sessions it could see. The waiting session reads the row
    as the rotation committed it, so the comparison happens against the
    new pair rather than the one the unlock resolved before the wait.
    """
    document_id = f"ed_collaboration_unlock_waits_on_{rotated}_rotation"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id = lease.guest_link_id
    assert link_id is not None
    links = _guest_links(database)
    link = await _resolve_guest_link(
        links, link_id=link_id, now=database.now + 1.0
    )
    identity_id = uuid.uuid4()
    _pause_inside(monkeypatch, links, "_revoke_guest_sessions", after=True)
    rotation_task, paused = _start_paused(
        lambda: _change_guest_link(
            links,
            document_id=document_id,
            link_id=link_id,
            change=rotated,
            now=database.now + 2.0,
        )
    )
    open_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        open_task = asyncio.create_task(
            _open_guest_session_with_verified_pair(
                links, link, identity_id=identity_id, now=database.now + 1.0
            )
        )
        open_waited = await _waits_on_a_lock_before_it_finishes(
            database, open_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(rotation_task, open_task)

    assert open_waited
    rotated_link, opened = outcomes
    assert not isinstance(rotated_link, BaseException), rotated_link
    assert rotated_link.revision == 2
    assert isinstance(opened, EditorGuestLinkExpired), opened
    assert await _guest_identity_revoked_at(database, identity_id) == "absent"
    assert await _guest_link_open_counts(database, link_id) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", ["password", "token"])
async def test_a_rotation_waiting_on_an_unlock_revokes_the_session_it_opened(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    rotated: str,
) -> None:
    """A rotation that waits for an unlock which opened its session opens
    no way in.

    The unlock still finds the pair it checked and inserts its session,
    which holds the link row and the document's key share; the rotation
    waits for it at the document, and its revocation then sees that
    session and revokes it, so neither the session nor a lease outlives
    the rotation.
    """
    document_id = f"ed_collaboration_{rotated}_rotation_waits_on_unlock"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id = lease.guest_link_id
    assert link_id is not None
    links = _guest_links(database)
    link = await _resolve_guest_link(
        links, link_id=link_id, now=database.now + 1.0
    )
    identity_id = uuid.uuid4()
    _pause_inside(monkeypatch, links, "_guest_document", after=False)
    open_task, paused = _start_paused(
        lambda: _open_guest_session_with_verified_pair(
            links, link, identity_id=identity_id, now=database.now + 1.0
        )
    )
    rotation_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        rotation_task = asyncio.create_task(
            _change_guest_link(
                links,
                document_id=document_id,
                link_id=link_id,
                change=rotated,
                now=database.now + 2.0,
            )
        )
        rotation_waited = await _waits_on_a_lock_before_it_finishes(
            database, rotation_task
        )
    finally:
        paused.release.set()
        outcomes = await _settle(open_task, rotation_task)

    assert rotation_waited
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    opened, rotated_link = outcomes
    assert opened.identity.id == identity_id
    assert rotated_link.revision == 2
    assert (
        await _guest_identity_revoked_at(database, identity_id)
        == database.now + 2.0
    )
    with pytest.raises(EditorGuestLinkExpired):
        await links.resolve_guest_identity(
            tenant_id=TENANT_A,
            session_token_digest=_sha256(f"guest:{identity_id}".encode()),
            now=database.now + 3.0,
        )
    with pytest.raises(CollaborationDocumentNotFound):
        await _issue_guest_edit_lease(
            database,
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            now=database.now + 3.0,
        )


@pytest.mark.asyncio
async def test_an_unlock_across_a_permission_change_opens_a_session(
    database: _DatabaseHarness,
) -> None:
    """Changing only the permission between an unlock's check and its
    session keeps the unlock: the pair it checked is still the link's.

    The session carries the permission the link has when it opens, not
    the one the unlock resolved.
    """
    document_id = "ed_collaboration_unlock_across_a_permission_change"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id = lease.guest_link_id
    assert link_id is not None
    links = _guest_links(database)
    link = await _resolve_guest_link(
        links, link_id=link_id, now=database.now + 1.0
    )
    await _change_guest_link(
        links,
        document_id=document_id,
        link_id=link_id,
        change="permission",
        now=database.now + 2.0,
    )
    identity_id = uuid.uuid4()

    opened = await _open_guest_session_with_verified_pair(
        links, link, identity_id=identity_id, now=database.now + 1.0
    )

    assert link.permission == "edit"
    assert opened.identity.id == identity_id
    assert opened.link.permission == "suggest"
    assert opened.link.revision == 2
    assert await _guest_identity_revoked_at(database, identity_id) is None
    assert await _guest_link_open_counts(database, link_id) == (1, 1)


# -- Revoking or rotating a link against a guest's own transaction -----------
#
# Refreshing a guest session and opening one lock rows that revoking a link
# or rotating its password or token locks as well: the document row, the
# link row and the link's guest identities. Each of these transactions takes
# them in that order, so the one that arrives second waits for the first
# instead of holding a row the first one waits for. The tests below hold one
# side inside its transaction right before one of its statements, run the
# other side until it finishes or PostgreSQL reports it waiting, and then
# let both go on. In every interleaving the revocation or rotation commits,
# neither side is aborted as a deadlock, and no guest identity or lease of
# the link outlives it.


class _HeldSession:
    """Forward to a store's session; hold before the call's Nth statement."""

    def __init__(self, inner: AsyncSession, hold_before: int) -> None:
        self._inner = inner
        self._hold_before = hold_before
        self._statements = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._statements += 1
        paused = _PAUSED_CALL.get()
        if paused is not None and self._statements == self._hold_before:
            paused.reached.set()
            await paused.release.wait()
        return await self._inner.execute(*args, **kwargs)


def _hold_before_statement(
    monkeypatch: pytest.MonkeyPatch,
    store: PostgresEditorGuestLinkStore | PostgresEditorCollaborationStore,
    statement: int,
) -> None:
    """Hold calls of ``store`` started by ``_start_paused`` right before
    their ``statement``-th statement, with every lock taken so far.

    Only statements run through ``execute`` are counted and can be held
    before; a ``scalar`` call passes uncounted.
    """
    original = store._session

    @contextlib.asynccontextmanager
    async def held(tenant_id: str) -> AsyncIterator[Any]:
        async with original(tenant_id) as session:
            yield _HeldSession(session, statement)

    monkeypatch.setattr(store, "_session", held)


async def _revoke_or_rotate(
    links: PostgresEditorGuestLinkStore,
    *,
    action: str,
    document_id: str,
    link_id: uuid.UUID,
    now: float,
) -> Any:
    """Revoke a link from ``_authorize_guest``, or rotate its password or
    token, at revision 1."""
    if action != "revoke":
        return await _change_guest_link(
            links,
            document_id=document_id,
            link_id=link_id,
            change=action,
            now=now,
        )
    command_id = uuid.uuid4()
    return await links.revoke_link(
        tenant_id=TENANT_A,
        document_id=document_id,
        link_id=link_id,
        actor_user_id=OWNER_A,
        expected_revision=1,
        command_id=command_id,
        command_payload_hash=_sha256(str(command_id).encode()),
        now=now,
    )


async def _interleave(
    database: _DatabaseHarness,
    held_call: Callable[[], Coroutine[Any, Any, Any]],
    other_call: Callable[[], Coroutine[Any, Any, Any]],
) -> tuple[Any, Any]:
    """Run ``other_call`` while ``held_call`` holds where it was told to.

    ``other_call`` starts once ``held_call`` holds, or once it finished when
    it has fewer statements than the hold point, and runs until it finishes
    or PostgreSQL reports it waiting on a lock; then both go on. Returns
    both results or exceptions, held call first.
    """
    held_task, paused = _start_paused(held_call)
    other_task = None
    try:
        reached = asyncio.create_task(paused.reached.wait())
        try:
            await asyncio.wait(
                {reached, held_task},
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            reached.cancel()
        assert paused.reached.is_set() or held_task.done()
        other_task = asyncio.create_task(other_call())
        await _waits_on_a_lock_before_it_finishes(database, other_task)
    finally:
        paused.release.set()
        outcomes = await _settle(held_task, other_task)
    held, other = outcomes
    return held, other


async def _assert_the_owner_change_won(
    database: _DatabaseHarness,
    links: PostgresEditorGuestLinkStore,
    *,
    action: str,
    link_id: uuid.UUID,
    owner_outcome: Any,
    guest_outcome: Any,
    guest_identity_ids: tuple[uuid.UUID, ...],
) -> None:
    """The revocation or rotation committed, the guest side either finished
    before it or was refused, and no guest identity or lease of the link is
    live."""
    assert isinstance(owner_outcome, EditorDocumentShareLink), owner_outcome
    assert isinstance(
        guest_outcome, (EditorGuestAccess, EditorGuestLinkExpired)
    ), guest_outcome
    if isinstance(guest_outcome, EditorGuestAccess):
        # A guest call that finished saw the link before the owner's change
        # and a live session: one that read the link after the change, or
        # stamped a session the change had revoked, let the guest in after
        # the change committed.
        assert guest_outcome.link.revision == 1, guest_outcome.link
        assert guest_outcome.link.revoked_at is None, guest_outcome.link
        assert guest_outcome.identity.revoked_at is None, (
            guest_outcome.identity
        )
    assert owner_outcome.revision == 2
    async with database.session_factory() as session:
        link = (
            await session.execute(
                select(editor_document_share_links).where(
                    editor_document_share_links.c.id == link_id
                )
            )
        ).one()
        live_identities = await session.scalar(
            select(func.count())
            .select_from(editor_document_guest_identities)
            .where(
                editor_document_guest_identities.c.link_id == link_id,
                editor_document_guest_identities.c.revoked_at.is_(None),
            )
        )
        live_leases = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_leases)
            .where(
                editor_collaboration_leases.c.guest_link_id == link_id,
                editor_collaboration_leases.c.revoked_at.is_(None),
            )
        )
    assert link.revision == 2
    if action == "revoke":
        assert link.revoked_at is not None
    elif action == "password":
        assert link.password_hash == "argon2-rotated-hash"
    else:
        assert link.token_digest == _sha256(b"rotated-link-token")
    assert live_identities == 0
    assert live_leases == 0
    for identity_id in guest_identity_ids:
        with pytest.raises(EditorGuestLinkExpired):
            await links.resolve_guest_identity(
                tenant_id=TENANT_A,
                session_token_digest=_sha256(f"guest:{identity_id}".encode()),
                now=database.now + 3.0,
            )


_GUEST_REFRESH_HOLDS = [("guest", statement) for statement in range(2, 7)]
_GUEST_UNLOCK_HOLDS = [("guest", statement) for statement in range(2, 6)]
_OWNER_HOLDS = [("owner", statement) for statement in range(2, 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["password", "token", "revoke"])
@pytest.mark.parametrize(
    ("held", "hold_before"),
    _GUEST_REFRESH_HOLDS + _OWNER_HOLDS,
)
async def test_a_revocation_or_rotation_wins_against_a_guest_refresh(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    held: str,
    hold_before: int,
) -> None:
    """A guest refresh in flight never aborts a revocation or a rotation.

    The refresh reads the guest's session and stamps it; the owner's
    change locks the document and the link and revokes the link's guest
    identities and leases. Whichever of the two holds its first rows when
    the other arrives, the other waits for it, the owner's change commits,
    and the session it ended no longer resolves.
    """
    document_id = f"ed_collaboration_{action}_vs_refresh_{held}_{hold_before}"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    guest_links, owner_links = _guest_links(database), _guest_links(database)
    _hold_before_statement(
        monkeypatch,
        guest_links if held == "guest" else owner_links,
        hold_before,
    )

    def refresh() -> Coroutine[Any, Any, Any]:
        return guest_links.resolve_guest_identity(
            tenant_id=TENANT_A,
            session_token_digest=_sha256(f"guest:{identity_id}".encode()),
            now=database.now + 1.0,
        )

    def change() -> Coroutine[Any, Any, Any]:
        return _revoke_or_rotate(
            owner_links,
            action=action,
            document_id=document_id,
            link_id=link_id,
            now=database.now + 2.0,
        )

    if held == "guest":
        guest_outcome, owner_outcome = await _interleave(
            database, refresh, change
        )
    else:
        owner_outcome, guest_outcome = await _interleave(
            database, change, refresh
        )

    await _assert_the_owner_change_won(
        database,
        _guest_links(database),
        action=action,
        link_id=link_id,
        owner_outcome=owner_outcome,
        guest_outcome=guest_outcome,
        guest_identity_ids=(identity_id,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["password", "token", "revoke"])
@pytest.mark.parametrize(
    ("held", "hold_before"),
    _GUEST_UNLOCK_HOLDS + _OWNER_HOLDS,
)
async def test_a_revocation_or_rotation_wins_against_a_guest_unlock(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    held: str,
    hold_before: int,
) -> None:
    """A guest unlock in flight never aborts a revocation or a rotation.

    The unlock opens a new guest session on the link; the owner's change
    locks the document and the link and revokes the link's guest
    identities and leases. Whichever of the two holds its first rows when
    the other arrives, the other waits for it, the owner's change commits,
    and a session the unlock opened before it no longer resolves.
    """
    document_id = f"ed_collaboration_{action}_vs_unlock_{held}_{hold_before}"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    guest_links, owner_links = _guest_links(database), _guest_links(database)
    link = await _resolve_guest_link(
        guest_links, link_id=link_id, now=database.now + 1.0
    )
    new_identity_id = uuid.uuid4()
    _hold_before_statement(
        monkeypatch,
        guest_links if held == "guest" else owner_links,
        hold_before,
    )

    def unlock() -> Coroutine[Any, Any, Any]:
        return _open_guest_session_with_verified_pair(
            guest_links,
            link,
            identity_id=new_identity_id,
            now=database.now + 1.0,
        )

    def change() -> Coroutine[Any, Any, Any]:
        return _revoke_or_rotate(
            owner_links,
            action=action,
            document_id=document_id,
            link_id=link_id,
            now=database.now + 2.0,
        )

    if held == "guest":
        guest_outcome, owner_outcome = await _interleave(
            database, unlock, change
        )
    else:
        owner_outcome, guest_outcome = await _interleave(
            database, change, unlock
        )

    await _assert_the_owner_change_won(
        database,
        _guest_links(database),
        action=action,
        link_id=link_id,
        owner_outcome=owner_outcome,
        guest_outcome=guest_outcome,
        guest_identity_ids=(identity_id, new_identity_id),
    )


# -- Issuing a guest lease against revoking, rotating or deleting -------------
#
# Issuing a lease to a guest checks the guest's identity and link, deletes
# the guest's leases that expired more than a day ago and inserts the new
# lease, whose foreign key takes the document row FOR KEY SHARE. Revoking
# the link, rotating its password or token and deleting the document lock
# the document row before they revoke the document's or the link's leases.
# The issue takes the document row FOR KEY SHARE before its access check
# and before any lease row, so whichever of the two arrives second waits
# for the first: the owner's change revokes a lease issued before it, and
# an issue that waited for it is refused. The test below holds either side
# right before each of its statements, runs the other until it finishes or
# PostgreSQL reports it waiting, and then lets both go on.

_GUEST_ISSUE_HOLDS = [("guest", statement) for statement in range(2, 7)]
# Deleting the document locks the owner's user row and the document row
# before the shares and the leases, and stamps the document after them;
# holds up to the statement after that stamp cover every document, share
# and lease row it locks.
_DELETE_HOLDS = [("owner", statement) for statement in range(2, 9)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "held", "hold_before"),
    [
        (action, held, hold_before)
        for action in ("password", "token", "revoke", "delete")
        for held, hold_before in _GUEST_ISSUE_HOLDS
        + (_DELETE_HOLDS if action == "delete" else _OWNER_HOLDS)
    ],
)
async def test_a_revocation_rotation_or_deletion_wins_against_a_guest_lease_issue(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    held: str,
    hold_before: int,
) -> None:
    """A guest lease issue in flight never aborts the owner's change, and
    no lease outlives it.

    The guest also holds a lease that expired more than a day ago: the
    issue deletes it and the owner's change revokes it, so both write that
    row. Whichever side holds its rows when the other arrives, the other
    waits for it or passes it; the revocation, rotation or deletion
    commits, a lease issued before it carries its revocation stamp, an
    issue that came after it is refused, and no lease of the document is
    left live.
    """
    document_id = f"ed_collaboration_{action}_vs_guest_issue_{held}_{hold_before}"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    stale_id = uuid.uuid4()
    stale = replace(
        lease,
        lease_id=stale_id,
        token_hash=_sha256(f"stale:{stale_id}".encode()),
        issued_at=database.now - 200_000.0,
        expires_at=database.now - 100_000.0,
        last_validated_at=None,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_collaboration_leases).values(
                    **collaboration_postgres._lease_values(stale)
                )
            )
    owner_store = PostgresEditorCollaborationStore(
        session_factory=database.session_factory,
        app_role=APP_ROLE,
        restrict_to_workspace_members=False,
    )
    owner_links = _guest_links(database)
    _hold_before_statement(
        monkeypatch,
        (
            database.store
            if held == "guest"
            else owner_store
            if action == "delete"
            else owner_links
        ),
        hold_before,
    )

    def issue() -> Coroutine[Any, Any, Any]:
        return _issue_guest_edit_lease(
            database,
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            now=database.now + 1.0,
        )

    def change() -> Coroutine[Any, Any, Any]:
        if action == "delete":
            return owner_store.tombstone_document(
                tenant_id=TENANT_A,
                document_id=document_id,
                owner_user_id=OWNER_A,
                now=database.now + 2.0,
            )
        return _revoke_or_rotate(
            owner_links,
            action=action,
            document_id=document_id,
            link_id=link_id,
            now=database.now + 2.0,
        )

    if held == "guest":
        issued, changed = await _interleave(database, issue, change)
    else:
        changed, issued = await _interleave(database, change, issue)

    if action == "delete":
        assert changed == 2, changed
    else:
        assert isinstance(changed, EditorDocumentShareLink), changed
        assert changed.revision == 2
    if isinstance(issued, CollaborationLease):
        assert await _lease_revoked_at(database, issued) == database.now + 2.0
    elif action == "delete":
        assert isinstance(issued, CollaborationConflict), issued
        assert issued.reason == "generation_conflict"
    else:
        assert isinstance(issued, CollaborationDocumentNotFound), issued
    async with database.session_factory() as session:
        live_leases = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_leases)
            .where(
                editor_collaboration_leases.c.document_id == document_id,
                editor_collaboration_leases.c.revoked_at.is_(None),
            )
        )
    assert live_leases == 0


# -- A guest's comment against revoking or rotating the link -----------------
#
# Every comment mutation of a guest (a new thread, a reply, editing a
# message, resolving or deleting a thread) locks the document row and then
# checks the guest's identity and link. Revoking the link and rotating its
# password or token lock the document row first as well, so whichever of the
# two arrives second waits for the first: a comment that holds the document
# commits before the owner's change, and one that waited for the change
# reads the revoked access and is refused. The test below holds the comment
# right before its access check and right after it, and the owner's change
# right before each of its statements. Past its access check the comment
# only writes rows under the document lock, which the owner's change waits
# for before any other row.
#
# Both sides append an invalidation for the document's owner. Events of one
# user are numbered in commit order (each append first locks the user's
# authorization generation), so the owner's events tell which side
# committed first.

_GUEST_COMMENT_HOLDS = [("guest", statement) for statement in (2, 3)]
_GUEST_COMMENT_KINDS = ("create", "reply", "edit", "resolve", "delete")


async def _guest_comment(
    database: _DatabaseHarness,
    kind: str,
    *,
    document_id: str,
    identity_id: uuid.UUID,
    link_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    expected_revision: int,
    now: float,
) -> Any:
    """Run one comment mutation of ``kind`` as the guest of a link."""
    command_id = uuid.uuid4()
    actor: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "document_id": document_id,
        "generation": 1,
        "actor_user_id": None,
        "actor_guest_identity_id": identity_id,
        "guest_link_id": link_id,
        "thread_id": thread_id,
        "expected_revision": expected_revision,
        "command_id": command_id,
        "command_payload_hash": _sha256(str(command_id).encode()),
        "now": now,
    }
    store = database.store
    if kind == "create":
        return await store.create_comment_thread(
            **actor,
            message_id=message_id,
            anchor={"type": "test"},
            quote_text="Body.",
            body_markdown="A guest's thread",
            mention_user_ids=(),
        )
    if kind == "reply":
        return await store.add_comment_reply(
            **actor,
            message_id=uuid.uuid4(),
            body_markdown="A guest's reply",
            mention_user_ids=(),
        )
    if kind == "edit":
        return await store.update_comment_message(
            **actor,
            message_id=message_id,
            body_markdown="A guest's edited thread",
            mention_user_ids=(),
            delete_message=False,
        )
    if kind == "resolve":
        return await store.set_comment_thread_status(
            **actor, status="resolved", can_moderate=False
        )
    return await store.delete_comment_thread(**actor)


async def _owner_event_scopes_after(
    database: _DatabaseHarness,
    document_id: str,
    after_id: int,
) -> list[str]:
    """The scopes of the owner's events on a document, in commit order."""
    async with database.session_factory() as session:
        rows = await session.execute(
            select(user_events.c.scope)
            .where(
                user_events.c.target_user_id == OWNER_A,
                user_events.c.resource_id == document_id,
                user_events.c.id > after_id,
            )
            .order_by(user_events.c.id)
        )
        return [str(scope) for scope in rows.scalars()]


async def _comment_revision(database: _DatabaseHarness, document_id: str) -> int:
    """Read the document's shared-comment revision."""
    async with database.session_factory() as session:
        revision = await session.scalar(
            select(editor_documents.c.collaboration_comment_revision).where(
                editor_documents.c.id == document_id
            )
        )
    return int(revision)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "action", "held", "hold_before"),
    [
        (kind, action, held, hold_before)
        for kind in _GUEST_COMMENT_KINDS
        for action in ("password", "token", "revoke")
        for held, hold_before in _GUEST_COMMENT_HOLDS + _OWNER_HOLDS
    ],
)
async def test_a_revocation_or_rotation_wins_against_a_guest_comment(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    action: str,
    held: str,
    hold_before: int,
) -> None:
    """No guest comment lands after a revocation or rotation of its link.

    Whichever side holds its rows when the other arrives, the other waits
    for it, and the owner's change commits. A comment that held the document
    first committed before the change; a comment that reaches the document
    while the owner's change holds it is refused once the change committed,
    and the comments stay as they were.
    """
    document_id = (
        f"ed_collaboration_{action}_vs_guest_{kind}_{held}_{hold_before}"
    )
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    thread_id, message_id = uuid.uuid4(), uuid.uuid4()
    if kind != "create":
        started = await _guest_comment(
            database,
            "create",
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            thread_id=thread_id,
            message_id=message_id,
            expected_revision=0,
            now=database.now + 0.5,
        )
        assert started.revision == 1
    revision_before = await _comment_revision(database, document_id)
    async with database.session_factory() as session:
        events_before = int(
            await session.scalar(
                select(func.coalesce(func.max(user_events.c.id), 0))
            )
        )
    owner_links = _guest_links(database)
    _hold_before_statement(
        monkeypatch,
        database.store if held == "guest" else owner_links,
        hold_before,
    )

    def comment() -> Coroutine[Any, Any, Any]:
        return _guest_comment(
            database,
            kind,
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            thread_id=thread_id,
            message_id=message_id,
            expected_revision=revision_before,
            now=database.now + 1.0,
        )

    def change() -> Coroutine[Any, Any, Any]:
        return _revoke_or_rotate(
            owner_links,
            action=action,
            document_id=document_id,
            link_id=link_id,
            now=database.now + 2.0,
        )

    if held == "guest":
        commented, changed = await _interleave(database, comment, change)
    else:
        changed, commented = await _interleave(database, change, comment)

    assert isinstance(changed, EditorDocumentShareLink), changed
    assert changed.revision == 2
    scopes = await _owner_event_scopes_after(
        database, document_id, events_before
    )
    if held == "guest":
        # The guest's comment started first and committed, before the
        # owner's change: a comment that committed after it would carry
        # its event after the change's.
        assert isinstance(commented, CollaborationCommentThread), commented
        assert scopes == [
            "collaboration_comment_changed",
            "collaboration_guest_policy",
        ]
        assert await _comment_revision(database, document_id) == (
            revision_before + 1
        )
    else:
        # The owner's change held the document from its first statement,
        # so the comment could only go on after the change committed.
        assert isinstance(commented, CollaborationDocumentNotFound), commented
        assert scopes == ["collaboration_guest_policy"]
        assert await _comment_revision(database, document_id) == (
            revision_before
        )


# -- Comments of several people at the same time ------------------------------
#
# A new thread overlaps no other thread, so it carries no revision
# precondition: two people who comment from the same view both keep their
# comment, whoever commits first. A reply, an edit, a resolve or a delete
# changes one thread and is refused only when that thread changed. Every
# comment mutation takes the document's next comment revision under the
# document row lock, so a reader at the view catches up on all of them.


async def _member_comment(
    database: _DatabaseHarness,
    kind: str,
    *,
    document_id: str,
    actor_user_id: uuid.UUID,
    thread_id: uuid.UUID,
    expected_revision: int,
    now: float,
) -> CollaborationCommentThread:
    """Start a thread (``create``) or answer one (``reply``) as a member."""
    command_id = uuid.uuid4()
    actor: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "document_id": document_id,
        "generation": 1,
        "actor_user_id": actor_user_id,
        "thread_id": thread_id,
        "message_id": uuid.uuid4(),
        "mention_user_ids": (),
        "expected_revision": expected_revision,
        "command_id": command_id,
        "command_payload_hash": _sha256(str(command_id).encode()),
        "now": now,
    }
    if kind == "create":
        return await database.store.create_comment_thread(
            **actor,
            anchor={"type": "test"},
            quote_text="Body.",
            body_markdown="A member's thread",
        )
    return await database.store.add_comment_reply(
        **actor, body_markdown="A member's reply"
    )


@pytest.mark.asyncio
async def test_comments_sent_together_from_one_view_all_land(
    database: _DatabaseHarness,
) -> None:
    """Two new threads and a reply sent at the same time all commit.

    The owner and a guest each start a thread while a reviewer answers an
    existing one, all three from the comment revision they last loaded. Each
    takes its own revision, and a reader at that revision gets all three. A
    second reply that still carries the answered thread's old revision is
    refused: a change to one thread keeps that thread's precondition.
    """
    document_id = "ed_collaboration_comments_from_one_view"
    await _collaboration_document_of_owner_a(database, document_id)
    await _share_document(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        recipient_user_id=REVIEWER_A,
        granted_by_user_id=OWNER_A,
        permission="edit",
    )
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="suggest",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    answered_id = uuid.uuid4()
    answered = await _member_comment(
        database,
        "create",
        document_id=document_id,
        actor_user_id=REVIEWER_A,
        thread_id=answered_id,
        expected_revision=0,
        now=database.now + 0.5,
    )
    view = await _comment_revision(database, document_id)
    assert view == answered.revision == 1
    owner_thread_id, guest_thread_id = uuid.uuid4(), uuid.uuid4()

    sent = await asyncio.gather(
        _member_comment(
            database,
            "create",
            document_id=document_id,
            actor_user_id=OWNER_A,
            thread_id=owner_thread_id,
            expected_revision=view,
            now=database.now + 1.0,
        ),
        _guest_comment(
            database,
            "create",
            document_id=document_id,
            identity_id=identity_id,
            link_id=link_id,
            thread_id=guest_thread_id,
            message_id=uuid.uuid4(),
            expected_revision=view,
            now=database.now + 1.0,
        ),
        _member_comment(
            database,
            "reply",
            document_id=document_id,
            actor_user_id=REVIEWER_A,
            thread_id=answered_id,
            expected_revision=answered.revision,
            now=database.now + 1.0,
        ),
        return_exceptions=True,
    )

    assert all(
        isinstance(result, CollaborationCommentThread) for result in sent
    ), sent
    assert sorted(result.revision for result in sent) == [
        view + 1,
        view + 2,
        view + 3,
    ]
    assert await _comment_revision(database, document_id) == view + 3
    page = await database.store.list_comment_threads(
        tenant_id=TENANT_A,
        document_id=document_id,
        generation=1,
        actor_user_id=OWNER_A,
        since_revision=view,
        status="all",
        limit=50,
    )
    assert page.current_revision == view + 3
    assert {thread.thread_id for thread in page.threads} == {
        owner_thread_id,
        guest_thread_id,
        answered_id,
    }

    with pytest.raises(CollaborationConflict) as refused:
        await _member_comment(
            database,
            "reply",
            document_id=document_id,
            actor_user_id=OWNER_A,
            thread_id=answered_id,
            expected_revision=answered.revision,
            now=database.now + 2.0,
        )
    assert refused.value.reason == "comment_revision_conflict"
    assert await _comment_revision(database, document_id) == view + 3


@pytest.mark.parametrize("actor", ["member", "guest"])
@pytest.mark.asyncio
async def test_an_introspection_that_finds_the_access_gone_revokes_the_lease(
    database: _DatabaseHarness,
    actor: str,
) -> None:
    """A lease whose holder lost the access it grants stays revoked.

    Lowering a member's share or a guest link to suggest leaves an edit
    lease row live. The next introspection of that lease, the check every
    token renewal of an open connection runs, refuses it and revokes it at
    the introspection's time, and the revocation commits with the refusal:
    raising the access again does not bring the lease back, while a lease
    issued afterwards passes the same introspection.
    """
    document_id = f"ed_collaboration_introspection_revokes_{actor}"
    await _reviewed_document(database, document_id, reviewer_permission="edit")
    if actor == "member":
        _, lease = await _authorize_writer(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            user_id=REVIEWER_A,
            permission="edit",
        )
        grant = update(resource_shares).where(
            resource_shares.c.resource_id == document_id,
            resource_shares.c.recipient_user_id == REVIEWER_A,
        )
    else:
        _, lease = await _authorize_guest(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            created_by_user_id=OWNER_A,
            permission="edit",
            display_name="Gina Gast",
        )
        grant = update(editor_document_share_links).where(
            editor_document_share_links.c.id == lease.guest_link_id
        )

    async def set_permission(permission: str) -> None:
        async with database.session_factory() as session:
            async with session.begin():
                await session.execute(grant.values(permission=permission))

    def introspect(
        introspected: CollaborationLease, now: float
    ) -> Coroutine[Any, Any, CollaborationLease]:
        return database.store.introspect_lease(
            tenant_id=TENANT_A,
            lease_id=introspected.lease_id,
            token_hash=introspected.token_hash,
            now=now,
        )

    await set_permission("suggest")
    with pytest.raises(CollaborationLeaseInvalid) as refused:
        await introspect(lease, database.now + 1.0)
    assert refused.value.reason == "access_revoked"
    assert await _lease_revoked_at(database, lease) == database.now + 1.0

    await set_permission("edit")
    later_id = uuid.uuid4()
    later = replace(
        lease,
        lease_id=later_id,
        token_hash=_sha256(f"later:{later_id}".encode()),
        issued_at=database.now + 2.0,
        expires_at=database.now + 3_600.0,
        last_validated_at=None,
    )
    await database.store.issue_lease(
        later,
        max_active=5,
        max_issued_per_window=30,
        issued_since=database.now - 60.0,
    )
    validated = await introspect(later, database.now + 3.0)
    assert validated.lease_id == later_id
    assert validated.last_validated_at == database.now + 3.0
    with pytest.raises(CollaborationLeaseInvalid) as still_refused:
        await introspect(lease, database.now + 3.0)
    assert still_refused.value.reason == "access_revoked"
    assert await _lease_revoked_at(database, lease) == database.now + 1.0


# -- Refreshing a guest session against the same guest's lease ---------------
#
# A guest lease row references its guest identity and its guest link, and
# inserting one checks both foreign keys with FOR KEY SHARE, in the
# alphabetical order of their internal trigger names, which can take the
# identity before the link. A refresh holds the link FOR UPDATE and locks
# the identity only FOR NO KEY UPDATE, the mode of its own later update,
# which a foreign-key check does not wait for. A lease issued or rotated for
# the same guest while the refresh holds the link therefore waits at the
# link without holding a lock the refresh waits for. The test below holds
# either call right before each of its statements, runs the other until it
# finishes or PostgreSQL reports it waiting, and then lets both go on.

_REFRESH_HOLDS = [("refresh", statement) for statement in range(2, 7)]
_LEASE_CALL_HOLDS = {
    "issue": [("lease", statement) for statement in range(2, 7)],
    "rotate": [("lease", statement) for statement in range(2, 9)],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_call", "held", "hold_before"),
    [
        (lease_call, held, hold_before)
        for lease_call in ("issue", "rotate")
        for held, hold_before in _REFRESH_HOLDS + _LEASE_CALL_HOLDS[lease_call]
    ],
)
async def test_a_guest_refresh_and_the_same_guests_lease_call_both_commit(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
    lease_call: str,
    held: str,
    hold_before: int,
) -> None:
    """A guest refresh and a lease call of the same guest never deadlock.

    The refresh stamps the guest's identity and link; issuing or rotating
    the guest's lease inserts a lease row that references both. Whichever
    of the two holds its rows when the other arrives, the other waits for
    it or passes it, and both commit: the identity carries the refresh's
    stamp and the new lease is live.
    """
    document_id = f"ed_collaboration_refresh_vs_{lease_call}_{held}_{hold_before}"
    await _collaboration_document_of_owner_a(database, document_id)
    _, lease = await _authorize_guest(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        created_by_user_id=OWNER_A,
        permission="edit",
        display_name="Gina Gast",
    )
    link_id, identity_id = lease.guest_link_id, lease.guest_identity_id
    assert link_id is not None and identity_id is not None
    guest_links = _guest_links(database)
    _hold_before_statement(
        monkeypatch,
        guest_links if held == "refresh" else database.store,
        hold_before,
    )
    refreshed_at = database.now + 1.0
    leased_at = database.now + 1.5

    def refresh() -> Coroutine[Any, Any, Any]:
        return guest_links.resolve_guest_identity(
            tenant_id=TENANT_A,
            session_token_digest=_sha256(f"guest:{identity_id}".encode()),
            now=refreshed_at,
        )

    def lease_for_the_guest() -> Coroutine[Any, Any, Any]:
        if lease_call == "issue":
            return _issue_guest_edit_lease(
                database,
                document_id=document_id,
                identity_id=identity_id,
                link_id=link_id,
                now=leased_at,
            )
        return _rotate(
            database,
            lease,
            issued_at=leased_at,
            token=f"rotated:{lease.lease_id}".encode(),
        )

    if held == "refresh":
        refreshed, leased = await _interleave(
            database, refresh, lease_for_the_guest
        )
    else:
        leased, refreshed = await _interleave(
            database, lease_for_the_guest, refresh
        )

    assert isinstance(refreshed, EditorGuestAccess), refreshed
    assert isinstance(leased, CollaborationLease), leased
    assert refreshed.identity.last_seen_at == refreshed_at
    async with database.session_factory() as session:
        identity = (
            await session.execute(
                select(editor_document_guest_identities).where(
                    editor_document_guest_identities.c.id == identity_id
                )
            )
        ).one()
        new_lease = (
            await session.execute(
                select(editor_collaboration_leases).where(
                    editor_collaboration_leases.c.lease_id == leased.lease_id
                )
            )
        ).one()
    assert identity.last_seen_at == refreshed_at
    assert identity.revoked_at is None
    assert new_lease.revoked_at is None
    assert new_lease.guest_identity_id == identity_id
    if lease_call == "rotate":
        assert await _lease_revoked_at(database, lease) == leased_at


@pytest.mark.asyncio
async def test_a_restricted_member_update_and_lease_issue_do_not_deadlock(
    database: _DatabaseHarness,
) -> None:
    """Workspace membership rows stay ahead of the document row.

    With ``restrict_to_workspace_members``, a member's access check locks
    the owner's and the member's membership rows before the document. A
    third transaction holds the document: the member's update queues there
    holding the membership rows, and a lease issue of the member queues on
    those rows. Once the document is free, both finish. An update that
    locked the document before its access check would hold the document
    while the issue holds the membership rows the update needs next.
    """
    workspace_id = uuid.uuid4()
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id=TENANT_A,
                    name="Collaboration lock order",
                    created_by_user_id=OWNER_A,
                )
            )
            for member in (OWNER_A, REVIEWER_A):
                await session.execute(
                    insert(workspace_members).values(
                        tenant_id=TENANT_A,
                        workspace_id=workspace_id,
                        user_id=member,
                        role=WorkspaceRole.EDITOR.value,
                    )
                )
    store = PostgresEditorCollaborationStore(
        session_factory=database.session_factory,
        app_role=APP_ROLE,
        restrict_to_workspace_members=True,
    )
    document_id = "ed_collaboration_restricted_update_during_issue"
    session_id = "session:collaboration-restricted-lock-order"

    def member_lease(issued_at: float) -> CollaborationLease:
        lease_id = uuid.uuid4()
        return CollaborationLease(
            lease_id=lease_id,
            token_hash=_sha256(f"token:{lease_id}".encode()),
            tenant_id=TENANT_A,
            document_id=document_id,
            generation=1,
            user_id=REVIEWER_A,
            permission="edit",
            session_id=session_id,
            issued_at=issued_at,
            expires_at=database.now + 3_600.0,
            last_validated_at=None,
        )

    update_task = issue_task = None
    try:
        await _collaboration_document_of_owner_a(database, document_id)
        await _share_document(
            database,
            tenant_id=TENANT_A,
            document_id=document_id,
            recipient_user_id=REVIEWER_A,
            granted_by_user_id=OWNER_A,
            permission="edit",
        )
        await _seed_browser_session(
            database,
            tenant_id=TENANT_A,
            user_id=REVIEWER_A,
            session_id=session_id,
        )
        instance = await store.acquire_instance(
            tenant_id=TENANT_A,
            instance_id="node-primary",
            now=database.now,
            lease_seconds=5_000.0,
        )
        lease = await store.issue_lease(
            member_lease(database.now),
            max_active=5,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
        async with database.session_factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(editor_documents.c.id)
                    .where(editor_documents.c.id == document_id)
                    .with_for_update()
                )
                update_task = asyncio.create_task(
                    store.append_update(
                        _direct_update(
                            tenant_id=TENANT_A,
                            document_id=document_id,
                            actor_user_id=REVIEWER_A,
                            instance=instance,
                            lease=lease,
                            payload=b"restricted-member-update",
                            now=database.now + 1.0,
                        )
                    )
                )
                await _wait_until_transactions_wait_on_locks(database)
                issue_task = asyncio.create_task(
                    store.issue_lease(
                        member_lease(database.now + 1.0),
                        max_active=5,
                        max_issued_per_window=30,
                        issued_since=database.now - 60.0,
                    )
                )
                await _wait_until_transactions_wait_on_locks(database, waiters=2)
        outcomes = await _settle(update_task, issue_task)
    finally:
        await _settle(update_task, issue_task)
        async with database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    workspaces.delete().where(workspaces.c.id == workspace_id)
                )

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    persisted, issued = outcomes
    assert persisted.sequence == 1
    assert issued.user_id == REVIEWER_A


@pytest.mark.asyncio
async def test_an_update_under_a_long_expired_lease_leaves_that_lease_unlocked(
    database: _DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update under a long-expired lease is refused without waiting.

    Issuing a lease to a person deletes the person's leases that expired
    more than a day ago on every document, while the issue holds only the
    document it issues for. An update of another document signed with such
    a lease is refused at once, even while the issue holds the deleted row:
    an expired lease grants nothing, so the update never locks it.
    """
    document_id = "ed_collaboration_update_under_a_swept_lease"
    issued_document_id = "ed_collaboration_issue_sweeping_the_lease"
    await _collaboration_document_of_owner_a(database, document_id)
    await _collaboration_document_of_owner_a(database, issued_document_id)
    instance, lease = await _authorize_writer(
        database,
        tenant_id=TENANT_A,
        document_id=document_id,
        user_id=OWNER_A,
    )
    stale_id = uuid.uuid4()
    stale = replace(
        lease,
        lease_id=stale_id,
        token_hash=_sha256(f"stale:{stale_id}".encode()),
        issued_at=database.now - 200_000.0,
        expires_at=database.now - 100_000.0,
        last_validated_at=None,
    )
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(editor_collaboration_leases).values(
                    **collaboration_postgres._lease_values(stale)
                )
            )
    fresh_id = uuid.uuid4()
    fresh = replace(
        lease,
        lease_id=fresh_id,
        token_hash=_sha256(f"fresh:{fresh_id}".encode()),
        document_id=issued_document_id,
        issued_at=database.now + 1.0,
        expires_at=database.now + 3_600.0,
        last_validated_at=None,
    )
    _pause_inside(
        monkeypatch,
        collaboration_postgres,
        "_sweep_expired_actor_leases",
        after=True,
    )
    issue_task, paused = _start_paused(
        lambda: database.store.issue_lease(
            fresh,
            max_active=5,
            max_issued_per_window=30,
            issued_since=database.now - 60.0,
        )
    )
    update_task = None
    try:
        await asyncio.wait_for(paused.reached.wait(), timeout=10.0)
        update_task = asyncio.create_task(
            database.store.append_update(
                _direct_update(
                    tenant_id=TENANT_A,
                    document_id=document_id,
                    actor_user_id=OWNER_A,
                    instance=instance,
                    lease=stale,
                    payload=b"update-under-a-swept-lease",
                    now=database.now + 1.0,
                )
            )
        )
        waited = await _waits_on_a_lock_before_it_finishes(database, update_task)
    finally:
        paused.release.set()
        outcomes = await _settle(issue_task, update_task)

    issued, refused = outcomes
    assert waited is False, outcomes
    assert issued == fresh, outcomes
    assert isinstance(refused, CollaborationLeaseInvalid), outcomes
    assert refused.reason == "lease_invalid"
    async with database.session_factory() as session:
        swept = await session.scalar(
            select(func.count())
            .select_from(editor_collaboration_leases)
            .where(editor_collaboration_leases.c.lease_id == stale_id)
        )
    assert swept == 0
