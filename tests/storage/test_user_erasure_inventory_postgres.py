"""The erasure preview counts against a real PostgreSQL, not a fake.

Every line of the preview is a query, and a query can be silently wrong
in a way no stub notices: it returns zero, the preview reports "nothing
to erase", and the erasure that follows leaves the data behind while
reporting success.  A JSONB containment whose right-hand side
serialises as a JSON *string* instead of an array is exactly that: it
matches nothing for every user while the SQL looks correct.  That is
why these run on the real engine, against the real operators.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa

from inqtrix.services.user_erasure import Disposition
from inqtrix.services.user_erasure_inventory import (
    ErasureInventory,
    _tables,
    collect_inventory,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

TENANT = "default"
_NOW = 1_700_000_000.0
# The schema pins a real digest length on every collaboration command.
_HASH = "0" * 64


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


def _run(coro):
    """One fresh loop per test; the async engine binds to its loop."""
    return asyncio.run(coro)


async def _seeded(build) -> ErasureInventory:
    """Seed inside a transaction, take the inventory, roll everything back.

    The rollback is the isolation: these tests write real rows into real
    tables and must leave the database exactly as they found it.
    """
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            transaction = await session.begin()
            try:
                subject = await build(session)
                await session.flush()
                return await collect_inventory(session, user_id=subject)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _line(inventory: ErasureInventory, table: str, column: str):
    for line in inventory.lines:
        if line.table == table and line.column == column:
            return line
    raise AssertionError(f"{table}.{column} missing from the inventory")


async def _document(session, *, document_id: str, owner: uuid.UUID) -> None:
    await session.execute(
        sa.insert(_tables()["editor_documents"]).values(
            id=document_id,
            tenant_id=TENANT,
            title="fixture",
            content_markdown="",
            created_by_user_id=owner,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


async def _thread(session, *, document_id: str, author: uuid.UUID) -> uuid.UUID:
    thread_id = uuid.uuid4()
    await session.execute(
        sa.insert(_tables()["editor_collaboration_comment_threads"]).values(
            id=thread_id,
            tenant_id=TENANT,
            document_id=document_id,
            generation=1,
            revision=1,
            anchor={},
            created_at=_NOW,
            updated_at=_NOW,
            last_command_id=uuid.uuid4(),
            last_command_payload_hash=_HASH,
            last_command_kind="create",
            created_by_user_id=author,
        )
    )
    return thread_id


async def _message(
    session,
    *,
    document_id: str,
    thread_id: uuid.UUID,
    author: uuid.UUID,
    mentions: list[str],
) -> None:
    await session.execute(
        sa.insert(_tables()["editor_collaboration_comment_messages"]).values(
            id=uuid.uuid4(),
            tenant_id=TENANT,
            document_id=document_id,
            thread_id=thread_id,
            revision=1,
            author_user_id=author,
            body_markdown="fixture",
            mention_user_ids=mentions,
            created_at=_NOW,
            last_command_id=uuid.uuid4(),
            last_command_payload_hash=_HASH,
            last_command_kind="create",
        )
    )


def test_a_mention_of_the_user_is_actually_found() -> None:
    """The regression: containment compares an ARRAY, never a string.

    A message somebody ELSE wrote that mentions the departing user is the
    only way this column ever matches, and the preview has to surface it:
    the mention is stripped, the message stays.
    """
    mentioned = uuid.uuid4()
    author = uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [mentioned, author])
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        await _document(session, document_id=document_id, owner=author)
        thread_id = await _thread(
            session, document_id=document_id, author=author
        )
        await _message(
            session,
            document_id=document_id,
            thread_id=thread_id,
            author=author,
            mentions=[str(mentioned)],
        )
        return mentioned

    inventory = _run(_seeded(build))
    line = _line(
        inventory, "editor_collaboration_comment_messages", "mention_user_ids"
    )

    assert line.rows == 1, (
        "a mention of this user inside somebody else's message was not "
        "found: the containment argument is not a JSON array"
    )
    assert line.disposition is Disposition.CLEAR_REFERENCE
    # The message belongs to its author and is not the mentioned user's
    # to lose.
    assert _line(
        inventory, "editor_collaboration_comment_messages", "author_user_id"
    ).rows == 0


def test_the_preview_counts_a_users_own_document() -> None:
    owner = uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [owner])
        await _document(
            session, document_id=f"doc_{uuid.uuid4().hex[:12]}", owner=owner
        )
        return owner

    inventory = _run(_seeded(build))

    assert _line(inventory, "editor_documents", "created_by_user_id").rows == 1
    assert inventory.deleted_rows >= 1


def test_foreign_messages_inside_an_owned_document_are_counted() -> None:
    """The number an administrator must see before confirming.

    Comment messages hang off the document, so deleting the document
    deletes the replies colleagues wrote inside it. That loss falls on
    people who were never asked and is invisible unless the preview
    names it.
    """
    owner = uuid.uuid4()
    colleague = uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [owner, colleague])
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        await _document(session, document_id=document_id, owner=owner)
        thread_id = await _thread(
            session, document_id=document_id, author=colleague
        )
        for _ in range(3):
            await _message(
                session,
                document_id=document_id,
                thread_id=thread_id,
                author=colleague,
                mentions=[],
            )
        # The owner's own replies sit in the SAME thread. They are not a
        # loss inflicted on anybody else, so the count must exclude them
        # -- without this pair the filter could be dropped entirely and
        # every number would still look right.
        for _ in range(2):
            await _message(
                session,
                document_id=document_id,
                thread_id=thread_id,
                author=owner,
                mentions=[],
            )
        return owner

    inventory = _run(_seeded(build))

    assert inventory.foreign_rows == 3
    assert inventory.to_payload()["foreign_rows"] == 3
    # Five messages live in the document; three of them are somebody
    # else's.
    assert _line(
        inventory, "editor_collaboration_comment_messages", "author_user_id"
    ).rows == 2


def test_an_account_with_nothing_reports_a_measured_zero() -> None:
    stranger = uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [stranger])
        return stranger

    inventory = _run(_seeded(build))

    assert inventory.deleted_rows == 0
    assert inventory.cleared_rows == 0
    assert inventory.foreign_rows == 0
    assert inventory.blockers == ()
    # Every rule still ran: a zero here is counted, not skipped.
    assert len(inventory.lines) > 50


def test_a_workspace_the_user_created_blocks_the_erasure() -> None:
    """The one case the plan refuses instead of guessing."""
    creator = uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [creator])
        await session.execute(
            sa.insert(_tables()["workspaces"]).values(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                name="shared workspace",
                created_by_user_id=creator,
            )
        )
        return creator

    inventory = _run(_seeded(build))

    assert inventory.blockers, "a workspace creator must block the erasure"
    assert "workspaces.created_by_user_id" in inventory.blockers[0]


def test_work_in_a_colleagues_document_is_counted_as_moving_to_the_placeholder() -> (
    None
):
    """The confirmation names what stays behind under "Deleted account"
    instead of folding it into what is deleted or cleared."""
    editor, owner = uuid.uuid4(), uuid.uuid4()

    async def build(session):
        await ensure_canonical_users(session, [editor, owner])
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        await _document(session, document_id=document_id, owner=owner)
        await session.execute(
            sa.insert(_tables()["editor_collaboration_updates"]).values(
                tenant_id=TENANT,
                document_id=document_id,
                generation=1,
                sequence=1,
                update_bytes=b"\x00",
                update_hash="0" * 64,
                actor_kind="human",
                change_kind="direct",
                actor_user_id=editor,
                created_at=_NOW,
            )
        )
        return editor

    inventory = _run(_seeded(build))

    line = _line(inventory, "editor_collaboration_updates", "actor_user_id")
    assert line.disposition is Disposition.REASSIGN
    assert inventory.reassigned_rows == 1
    assert inventory.to_payload()["reassigned_rows"] == 1
    assert inventory.blockers == ()
