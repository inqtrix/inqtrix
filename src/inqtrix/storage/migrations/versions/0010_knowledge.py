"""Canonical knowledge schema: collections, documents, chunks.

Revision ID: 0010_knowledge
Revises: 0009_instance_role

Creates the three knowledge tables from their metadata snapshot and
applies the established security layering: DML grants for ``inqtrix_app``
and ENABLE + FORCE row-level security with the fail-closed tenant policy
(InitPlan ``(SELECT ...)`` wrapper), identical to ``0007_quota``.

This is the relational source of truth for the knowledge engine in the
``postgres`` storage tier; chunk VECTORS live in Qdrant (or the
in-process vector index), never in these tables.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Boolean,
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

knowledge_metadata = MetaData()

knowledge_collections = Table(
    "knowledge_collections",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("name", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("embedding_dim", Integer, nullable=False),
    Column("created_by_sub", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("ix_knowledge_collections_tenant_created", "tenant_id", "created_at"),
)
"""Logical knowledge collections. ``embedding_model``/``embedding_dim``
are immutable after creation; ``created_by_sub`` is the ownership anchor
(``None`` = legacy/unscoped, visible to all — the established rule). Knowledge
is the cross-workspace SHARING surface (owner + ACL via resource_shares), NOT
per-workspace project data, so there is deliberately NO workspace_id dimension
here (see docs/architecture/data-architecture.md). A dead
always-NULL workspace_id column was dropped in migration 0018."""

knowledge_documents = Table(
    "knowledge_documents",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "collection_id",
        Text,
        ForeignKey("knowledge_collections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("title", Text, nullable=False),
    Column("text", Text, nullable=False, server_default=text("''")),
    Column("metadata", JSON, nullable=False, server_default=text("'{}'")),
    Column("chunk_count", Integer, nullable=False, server_default=text("0")),
    Column(
        "vector_synced",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("created_at", Float, nullable=False),
    # Keyset-pagination index with the id tiebreaker (created_at is a float
    # epoch that can collide); supersedes (collection_id, created_at).
    Index(
        "ix_knowledge_documents_collection_created_id",
        "collection_id",
        "created_at",
        "id",
    ),
)
"""Ingested documents with their full canonical ``text`` (the source of
truth retrieval hydrates from and reindex re-embeds from). ``metadata``
is stored verbatim. ``vector_synced`` is the reconcile flag (see module
docstring)."""

knowledge_chunks = Table(
    "knowledge_chunks",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "document_id",
        Text,
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("collection_id", Text, nullable=False),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("chunk_index", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("source_text", Text, nullable=False, server_default=text("''")),
    Column("page_number", Integer, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("ix_knowledge_chunks_document_index", "document_id", "chunk_index"),
)
"""Per-chunk metadata: the embedded ``text`` (may carry a
contextualization prefix) and the pre-contextualization ``source_text``
quote verification runs against. The DENSE/sparse VECTORS are NOT stored
here — they live in Qdrant, keyed by this chunk ``id``. Retrieval returns
chunk ids from Qdrant and hydrates text/source_text + document title from
these rows."""

revision = "0010_knowledge"
down_revision = "0009_instance_role"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Order matters for FK creation/drop: parents before children.
_TABLES = (
    "knowledge_collections",
    "knowledge_documents",
    "knowledge_chunks",
)


def upgrade() -> None:
    bind = op.get_bind()
    knowledge_metadata.create_all(bind=bind)

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
    knowledge_metadata.drop_all(bind=bind)
