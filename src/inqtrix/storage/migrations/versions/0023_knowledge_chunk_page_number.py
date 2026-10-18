"""Add page_number to knowledge_chunks.

Revision ID: 0023_knowledge_chunk_page_number
Revises: 0022_vector_index_server_model

Stores a best-effort 1-based source PAGE NUMBER per chunk, captured at ingest by
overlapping the chunk text against per-page PDF text (PDFs only). Enables a
page-level "open PDF at page N" jump and a soft page highlight in the citation
UI — NOT exact bounding boxes. Nullable with no default: chunks ingested before
this field, non-PDF sources, and inconclusive mappings stay ``NULL`` (no page),
never a guessed value (No Silent Fallbacks).

``IF NOT EXISTS`` / ``IF EXISTS`` keep this idempotent across a freshly created
database (create_all reads an ORM that already carries the column) and an
already-migrated one. RLS is row-level and column-agnostic, so adding a column
needs no policy change.
"""

from __future__ import annotations

from alembic import op

revision = "0023_knowledge_chunk_page_number"
down_revision = "0022_vector_index_server_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_chunks "
        "ADD COLUMN IF NOT EXISTS page_number integer"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_chunks "
        "DROP COLUMN IF EXISTS page_number"
    )
