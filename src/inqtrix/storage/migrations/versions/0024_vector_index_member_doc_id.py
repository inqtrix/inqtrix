"""Add server_document_id to vector_index_members.

Revision ID: 0024_vector_index_member_doc_id
Revises: 0023_knowledge_chunk_page_number

Records the backend knowledge-document id each index member was ingested as, so
"remove from index" can delete the exact document from the searchable collection
without a full rebuild. Without this persisted, the id lived only in the
in-memory reducer and was lost on every reload, so a post-reload removal could
misreport local success while the document stayed searchable server-side.
Nullable with no default — existing rows stay ``NULL`` (ingested before this
was tracked) and are reconciled through stable source identity before exact
durable removal. Mirrors ``server_collection_model``.

``IF NOT EXISTS`` / ``IF EXISTS`` keep this idempotent across a freshly created
database (create_all reads an ORM that already carries the column) and an
already-migrated one.
"""

from __future__ import annotations

from alembic import op

revision = "0024_vector_index_member_doc_id"
down_revision = "0023_knowledge_chunk_page_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_members "
        "ADD COLUMN IF NOT EXISTS server_document_id text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_members "
        "DROP COLUMN IF EXISTS server_document_id"
    )
