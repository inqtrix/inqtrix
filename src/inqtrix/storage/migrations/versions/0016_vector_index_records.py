"""Vector-index-record schema: records, members, capped run history.

Revision ID: 0016_vector_index_records
Revises: 0015_asset_records

Fourth slice of the project-persistence tier. Creates the
vector-index tables from their metadata snapshot and applies the
established security layering: DML grants for ``inqtrix_app`` +
ENABLE/FORCE row-level security with the fail-closed tenant policy,
identical to ``0013``/``0014``/``0015``.

CHECK constraints pin the index status, member state, and run result to
the frontend unions (``VectorIndexStatus`` / ``VectorIndexMemberState`` /
``VectorIndexRunResult``) so an out-of-domain write fails loudly at the
database boundary.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    BigInteger,
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

vector_index_metadata = MetaData()

vector_index_records = Table(
    "vector_index_records",
    vector_index_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("handle", Text, nullable=False, server_default=text("''")),
    Column("model", Text, nullable=False),
    Column("dims", Integer, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False, server_default=text("'stale'")),
    # The backend knowledge-collection id once the index was embedded on a
    # connected server; null = a simulated (demo/offline) index.
    Column("server_collection_id", Text, nullable=True),
    # The embedding model the server collection was BUILT with. Lets a reindex
    # tell "documents added" (same model -> incremental ingest) from "model
    # changed" (different -> full rebuild). Must persist across reload, else
    # every post-reload add falls back to a full rebuild.
    Column("server_collection_model", Text, nullable=True),
    # Visible failure message of the last server reindex attempt; null when
    # the last run succeeded (No-Silent-Fallbacks: a failed run must never
    # look like a merely stale index).
    Column("last_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_vector_index_records_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""One vector index (RAG file<->collection mapping). ``members`` and
``history`` live in the child tables. ``created_by_sub`` is the ownership
anchor (``None`` = unscoped/anonymous deployments)."""

vector_index_members = Table(
    "vector_index_members",
    vector_index_metadata,
    # COMPOSITE primary key (index_id, file_id): a member's identity is its
    # file within the index, never global — an upsert into index B can never
    # touch a same-file row in index A (the chat_messages isolation rule).
    Column(
        "index_id",
        Text,
        ForeignKey("vector_index_records.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("file_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # Membership order is user-visible (the frontend renders an index's
    # documents in array order), so it must round-trip deterministically.
    # ``seq`` is the 0-based position in the client's member array; the file
    # within the index is still the identity (the composite PK), unlike
    # history whose order IS its key.
    Column("seq", Integer, nullable=False, server_default=text("0")),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    # The backend knowledge-document id this member was ingested as, once known.
    # Lets "remove from index" delete the exact document from the searchable
    # collection without a full rebuild. Must persist across reload, else every
    # post-reload removal degrades to local-only while the document stays
    # searchable server-side. Null = ingested before this was tracked / offline.
    Column("server_document_id", Text, nullable=True),
)
"""The documents referenced by an index (n:m). Replaced wholesale on each
record upsert; cascade-deleted with the index. Read back in ``seq`` order
so both store tiers preserve the client's array order. Visibility inherits
from the parent record (owner-scoping lives at the record level)."""

vector_index_history = Table(
    "vector_index_history",
    vector_index_metadata,
    # COMPOSITE primary key (index_id, seq): a history entry has no client
    # identity, so its newest-first position IS its key. The frontend caps
    # the list at VECTOR_INDEX_HISTORY_LIMIT; the record upsert rewrites the
    # whole list, so seq is contiguous from 0.
    Column(
        "index_id",
        Text,
        ForeignKey("vector_index_records.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("result", Text, nullable=False),
    Column("documents", Integer, nullable=False, server_default=text("0")),
    Column("duration_ms", BigInteger, nullable=False, server_default=text("0")),
    Column("error", Text, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float, nullable=False),
)
"""Past reindex runs of an index, newest first (``seq`` ascending = newest
to oldest). Replaced wholesale on each record upsert; cascade-deleted with
the index."""

revision = "0016_vector_index_records"
down_revision = "0015_asset_records"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Parent before children: vector_index_records <- members / history.
_TABLES = (
    "vector_index_records",
    "vector_index_members",
    "vector_index_history",
)


def upgrade() -> None:
    bind = op.get_bind()
    vector_index_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE vector_index_records ADD CONSTRAINT ck_vector_index_records_status "
        "CHECK (status IN ('error', 'indexing', 'ready', 'stale'))"
    )
    op.execute(
        "ALTER TABLE vector_index_members ADD CONSTRAINT ck_vector_index_members_state "
        "CHECK (state IN ('pending', 'embedded'))"
    )
    op.execute(
        "ALTER TABLE vector_index_history ADD CONSTRAINT ck_vector_index_history_result "
        "CHECK (result IN ('cancelled', 'error', 'ok'))"
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
    vector_index_metadata.drop_all(bind=bind)
