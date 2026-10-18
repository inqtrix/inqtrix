"""Chat-history persistence schema: thread groups, threads, messages.

Revision ID: 0013_chat_history
Revises: 0012_keyset_indexes

This revision opens the project-persistence tier. Creates the chat
tables from their metadata snapshot and applies the established security
layering: DML grants for ``inqtrix_app`` and ENABLE + FORCE row-level
security with the fail-closed tenant policy (InitPlan ``(SELECT ...)``
wrapper), identical to ``0010_knowledge`` and ``0011_indexing_jobs``.

The ``role`` and ``source`` CHECK constraints pin the only legal values
to the frontend ``ChatRole`` / ``ChatThreadRecord.source`` unions so an
out-of-domain write fails loudly at the database boundary (No Silent
Fallbacks) rather than corrupting a round-trip.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON

chat_metadata = MetaData()

chat_thread_groups = Table(
    "chat_thread_groups",
    chat_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_chat_thread_groups_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""Optional grouping of a user's chat threads. ``created_by_sub`` is the
ownership anchor (``None`` = unscoped/anonymous deployments, the single
implicit owner). Listed by ``created_at`` (groups are few — no keyset
page), the id keeps the index unique for the tiebreaker convention."""

chat_threads = Table(
    "chat_threads",
    chat_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("preview", Text, nullable=False, server_default=text("''")),
    Column("source", Text, nullable=False, server_default=text("'api'")),
    Column(
        "group_id",
        Text,
        ForeignKey("chat_thread_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # Keyset-pagination index for the owner-scoped thread list (newest
    # first), with the id tiebreaker. List sort is by created_at (stable)
    # — NOT updated_at, which mutates on every message and would shift
    # the cursor under the reader (the frontend re-sorts by updated_at
    # for display).
    Index(
        "ix_chat_threads_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""One chat conversation. ``source`` (``api``/``imported``/``mock``) and
``group_id`` round-trip the local ``ChatThreadRecord``; ``preview`` is
the denormalized last-line shown in the thread list. Deleting a group
orphans its threads to ungrouped (``ON DELETE SET NULL``), never deletes
the conversations."""

chat_messages = Table(
    "chat_messages",
    chat_metadata,
    # COMPOSITE primary key (thread_id, id): a message's identity is
    # scoped to its thread, not global. With the client message id as the
    # sole PK, an autosave into thread B could silently overwrite a
    # same-id message that lives in thread A (an isolation break) — a
    # conflict on (thread_id, id) can only ever touch a row already in the
    # caller's own thread, so a cross-thread id reuse inserts a fresh
    # owned row instead of hijacking another user's message.
    Column("id", Text, primary_key=True),
    Column(
        "thread_id",
        Text,
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("role", Text, nullable=False),
    Column("content_markdown", Text, nullable=False, server_default=text("''")),
    Column("metadata", JSON, nullable=False, server_default=text("'{}'")),
    Column("created_at", Float, nullable=False),
    # Keyset-pagination index within a thread (oldest first when read;
    # the conversation order). created_at is stable for messages (they
    # are not re-timestamped), so the cursor is gap-free.
    Index("ix_chat_messages_thread_created", "thread_id", "created_at", "id"),
)
"""One message in a thread. ``content_markdown`` is the rendered body;
``metadata`` holds the verbatim optional client fields (``attachments``,
``chainTrace``, ``modelResolution``) so a round-trip reconstructs the
exact ``ChatMessageRecord``. Visibility inherits from the parent thread
(documents-inherit-from-collection pattern). Cascades on thread delete."""

revision = "0013_chat_history"
down_revision = "0012_keyset_indexes"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Parents before children for FK creation/drop:
# chat_thread_groups <- chat_threads <- chat_messages.
_TABLES = ("chat_thread_groups", "chat_threads", "chat_messages")


def upgrade() -> None:
    bind = op.get_bind()
    chat_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE chat_threads ADD CONSTRAINT ck_chat_threads_source "
        "CHECK (source IN ('api', 'imported', 'mock'))"
    )
    op.execute(
        "ALTER TABLE chat_messages ADD CONSTRAINT ck_chat_messages_role "
        "CHECK (role IN ('user', 'assistant'))"
    )
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    chat_metadata.drop_all(bind=bind)
