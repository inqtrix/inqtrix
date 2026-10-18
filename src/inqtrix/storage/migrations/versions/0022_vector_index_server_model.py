"""Add server_collection_model to vector_index_records.

Revision ID: 0022_vector_index_server_model
Revises: 0021_knowledge_sessions

Records the embedding model the server knowledge-collection was BUILT with, so a
reindex can tell "documents added" (same model -> incremental ingest of the new
members) from "model changed" (different -> full rebuild with a fresh
dimension). Without this persisted, every post-reload add fell back to a full
rebuild (re-embedding the whole collection). Nullable with no default — existing
rows stay ``NULL`` (unknown build model), which reads as a mismatch so the next
reindex heals via a rebuild. Mirrors ``server_collection_id``.

``IF NOT EXISTS`` / ``IF EXISTS`` keep this idempotent across a freshly created
database (the create_all reads an ORM that already carries the column) and an
already-migrated one.
"""

from __future__ import annotations

from alembic import op

revision = "0022_vector_index_server_model"
down_revision = "0021_knowledge_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_records "
        "ADD COLUMN IF NOT EXISTS server_collection_model text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_records "
        "DROP COLUMN IF EXISTS server_collection_model"
    )
