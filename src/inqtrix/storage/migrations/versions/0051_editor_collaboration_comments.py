"""Add durable shared editor-comment threads.

Revision ID: 0051_editor_comments
Revises: 0050_context_archive

Team comments are deliberately separate from ``editor_comments`` (private
AI notes). A document-scoped monotonic revision supports incremental refetch,
while the existing ``user_events`` table is the transactional, content-free
outbox consumed by the collaboration sidecar.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_editor_comments"
down_revision = "0050_context_archive"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
TABLES = (
    "editor_collaboration_comment_threads",
    "editor_collaboration_comment_messages",
    "editor_collaboration_comment_reads",
)


def _install_tenant_security(table: str) -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC, {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
        "USING (tenant_id = (SELECT inqtrix_current_tenant_id())) "
        "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))"
    )


def upgrade() -> None:
    op.execute("LOCK TABLE editor_documents IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE editor_documents NO FORCE ROW LEVEL SECURITY")
    op.execute("SET LOCAL row_security = off")
    op.add_column(
        "editor_documents",
        sa.Column(
            "collaboration_comment_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_editor_documents_comment_revision",
        "editor_documents",
        "collaboration_comment_revision >= 0",
    )

    op.create_table(
        "editor_collaboration_comment_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "resolved_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.Float(), nullable=True),
        sa.Column("anchor", postgresql.JSONB(), nullable=False),
        sa.Column(
            "quote_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column(
            "last_command_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("last_command_payload_hash", sa.Text(), nullable=False),
        sa.Column("last_command_kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_editor_collaboration_comment_threads",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_collaboration_comment_threads_tenant_id",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND revision >= 1",
            name="ck_collaboration_comment_threads_position",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_collaboration_comment_threads_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(anchor) = 'object'",
            name="ck_collaboration_comment_threads_anchor",
        ),
        sa.CheckConstraint(
            "length(last_command_payload_hash) = 64",
            name="ck_collaboration_comment_threads_command_hash",
        ),
        sa.CheckConstraint(
            "last_command_kind IN ('create', 'resolve', 'reopen')",
            name="ck_collaboration_comment_threads_command_kind",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_by_user_id IS NULL "
            "AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_by_user_id IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="ck_collaboration_comment_threads_resolution",
        ),
    )
    op.create_index(
        "ix_collaboration_comment_threads_document_revision",
        "editor_collaboration_comment_threads",
        ["tenant_id", "document_id", "generation", "revision"],
    )
    op.create_index(
        "ix_collaboration_comment_threads_status_updated",
        "editor_collaboration_comment_threads",
        ["tenant_id", "document_id", "generation", "status", "updated_at"],
    )
    op.create_index(
        "uq_collaboration_comment_threads_command",
        "editor_collaboration_comment_threads",
        ["tenant_id", "last_command_id"],
        unique=True,
    )

    op.create_table(
        "editor_collaboration_comment_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column(
            "mention_user_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("edited_at", sa.Float(), nullable=True),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.Column(
            "last_command_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("last_command_payload_hash", sa.Text(), nullable=False),
        sa.Column("last_command_kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_editor_collaboration_comment_messages",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "thread_id"],
            [
                "editor_collaboration_comment_threads.tenant_id",
                "editor_collaboration_comment_threads.id",
            ],
            name="fk_collaboration_comment_messages_thread",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_collaboration_comment_messages_revision",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(mention_user_ids) = 'array'",
            name="ck_collaboration_comment_messages_mentions",
        ),
        sa.CheckConstraint(
            "length(last_command_payload_hash) = 64",
            name="ck_collaboration_comment_messages_command_hash",
        ),
        sa.CheckConstraint(
            "last_command_kind IN ('create', 'reply', 'edit', 'delete')",
            name="ck_collaboration_comment_messages_command_kind",
        ),
    )
    op.create_index(
        "ix_collaboration_comment_messages_thread_created",
        "editor_collaboration_comment_messages",
        ["tenant_id", "thread_id", "created_at", "id"],
    )
    op.create_index(
        "ix_collaboration_comment_messages_document_revision",
        "editor_collaboration_comment_messages",
        ["tenant_id", "document_id", "revision"],
    )
    op.create_index(
        "uq_collaboration_comment_messages_command",
        "editor_collaboration_comment_messages",
        ["tenant_id", "last_command_id"],
        unique=True,
    )

    op.create_table(
        "editor_collaboration_comment_reads",
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "last_read_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "document_id",
            "generation",
            "user_id",
            name="pk_editor_collaboration_comment_reads",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND last_read_revision >= 0",
            name="ck_collaboration_comment_reads_position",
        ),
    )

    op.create_foreign_key(
        "fk_collaboration_comment_threads_document",
        "editor_collaboration_comment_threads",
        "editor_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_reads_document",
        "editor_collaboration_comment_reads",
        "editor_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_creator",
        "editor_collaboration_comment_threads",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_resolver",
        "editor_collaboration_comment_threads",
        "users",
        ["resolved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_messages_author",
        "editor_collaboration_comment_messages",
        "users",
        ["author_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_reads_user",
        "editor_collaboration_comment_reads",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    for table in TABLES:
        _install_tenant_security(table)

    # Keep the referenced document relation unforced while PostgreSQL
    # validates the new composite foreign keys above. The managed migration
    # transaction and its final postcondition restore FORCE RLS atomically.
    op.execute("ALTER TABLE editor_documents FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)

    op.execute("LOCK TABLE editor_documents IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE editor_documents NO FORCE ROW LEVEL SECURITY")
    op.execute("SET LOCAL row_security = off")
    op.drop_constraint(
        "ck_editor_documents_comment_revision",
        "editor_documents",
        type_="check",
    )
    op.drop_column("editor_documents", "collaboration_comment_revision")
    op.execute("ALTER TABLE editor_documents FORCE ROW LEVEL SECURITY")
