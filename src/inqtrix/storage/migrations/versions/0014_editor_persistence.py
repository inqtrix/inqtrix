"""Editor-persistence schema: folders, documents, comments.

Revision ID: 0014_editor_persistence
Revises: 0013_chat_history

Second slice of the project-persistence tier. Creates the editor
tables from their metadata snapshot and applies the established security
layering: DML grants for ``inqtrix_app`` and ENABLE + FORCE row-level
security with the fail-closed tenant policy (InitPlan ``(SELECT ...)``
wrapper), identical to ``0013_chat_history``.

The CHECK constraints pin source / comment kind / comment status / comment
evidence preset to the frontend unions (``EditorDocumentSource``,
``EditorCommentKind``, ``EditorCommentStatus``, ``EditorEvidencePreset``)
so an out-of-domain write fails loudly at the database boundary (No Silent
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
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON

editor_metadata = MetaData()

editor_folders = Table(
    "editor_folders",
    editor_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_editor_folders_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""Optional grouping of a user's editor documents (the document tree).
``created_by_sub`` is the ownership anchor (``None`` = unscoped/anonymous
deployments). Deleting a folder orphans its documents to ungrouped
(``ON DELETE SET NULL`` on ``editor_documents.folder_id``)."""

editor_documents = Table(
    "editor_documents",
    editor_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    # The heavy body. Excluded from the list endpoint (metadata only) and
    # loaded on open, the documents-equivalent of the lazy chat messages.
    Column("content_markdown", Text, nullable=False, server_default=text("''")),
    Column(
        "folder_id",
        Text,
        ForeignKey("editor_folders.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("source", Text, nullable=False, server_default=text("'blank'")),
    Column("source_run_id", Text, nullable=True),
    Column("revision", Integer, nullable=False, server_default=text("1")),
    Column("diff_anchor_markdown", Text, nullable=True),
    Column("diff_anchor_updated_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # Keyset-pagination index for the owner-scoped document list (newest
    # first) with the id tiebreaker; sort is by the stable created_at.
    Index(
        "ix_editor_documents_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""One editor document. ``content_markdown`` is the heavy body (lazy,
PUT with the document on autosave); ``revision``/``source``/
``source_run_id``/``diff_anchor_*`` round-trip the local
``EditorDocumentRecord``. ``folder_id`` is the (nullable) tree membership."""

editor_comments = Table(
    "editor_comments",
    editor_metadata,
    # COMPOSITE primary key (document_id, id): a comment's identity is
    # scoped to its document, never global — the same isolation rule the
    # chat_messages composite PK encodes (an autosave into document B can
    # never overwrite a same-id comment living in document A).
    Column("id", Text, primary_key=True),
    Column(
        "document_id",
        Text,
        ForeignKey("editor_documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("comment_markdown", Text, nullable=False, server_default=text("''")),
    # The positional anchor (block id, char range, surrounding quotes),
    # stored verbatim so a round-trip reconstructs the exact anchor.
    Column("anchor", JSON, nullable=False, server_default=text("'{}'")),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'open'")),
    Column("evidence_preset", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index("ix_editor_comments_document_created", "document_id", "created_at", "id"),
)
"""One anchored comment on a document. Unlike chat messages, comments are
independently mutated (resolve / edit / re-tag), so ``updated_at`` is the
autosave diff key. Visibility inherits from the parent document. Cascades
on document delete."""

revision = "0014_editor_persistence"
down_revision = "0013_chat_history"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Parents before children for FK creation/drop:
# editor_folders <- editor_documents <- editor_comments.
_TABLES = ("editor_folders", "editor_documents", "editor_comments")


def upgrade() -> None:
    bind = op.get_bind()
    editor_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE editor_documents ADD CONSTRAINT ck_editor_documents_source "
        "CHECK (source IN ('blank', 'imported-research-report', 'pasted'))"
    )
    op.execute(
        "ALTER TABLE editor_comments ADD CONSTRAINT ck_editor_comments_kind "
        "CHECK (kind IN ('collect', 'inline_edit', 'evidence_review'))"
    )
    op.execute(
        "ALTER TABLE editor_comments ADD CONSTRAINT ck_editor_comments_status "
        "CHECK (status IN ('open', 'resolved', 'stale'))"
    )
    op.execute(
        "ALTER TABLE editor_comments ADD CONSTRAINT ck_editor_comments_preset "
        "CHECK (evidence_preset IS NULL OR evidence_preset IN "
        "('add_sources', 'fact_check', 'verify_citations'))"
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
    editor_metadata.drop_all(bind=bind)
