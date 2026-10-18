"""Keyset-pagination indexes: append the id tiebreaker to the list sorts.

Revision ID: 0012_keyset_indexes
Revises: 0011_indexing_jobs

This revision prepares keyset/cursor pagination on the list endpoints. A
keyset cursor over ``created_at`` alone is unsafe here: ``created_at`` is a
float unix epoch (``time.time()``) on these tables, and bulk inserts /
seeds routinely share a timestamp — so a ``(created_at)`` boundary can skip
or repeat rows at a page edge. The cursor is therefore the tuple
``(created_at, <pk>)``, and the supporting index must carry the pk as the
tiebreaker after ``created_at``.

Replaces the two-column ``(scope, created_at)`` indexes with three-column
``(scope, created_at, pk)`` ones for the two lists that gain pagination
with this revision (runs, knowledge_documents). The new composite is a
strict superset of the old prefix, so nothing that used the old index
regresses. Idempotent in both directions so a fresh database (which built
the new index from the updated ORM via the earlier ``create_all``
migrations) and an already migrated one converge.

The other list endpoints (collections, prompt-templates, indexing-jobs,
shares) stay unpaginated at this revision — their lists are small or
bounded, or their visibility is a service-layer filter that a DB ``LIMIT``
would under-fill — so this revision leaves their indexes unchanged.
"""

from __future__ import annotations

from alembic import op

revision = "0012_keyset_indexes"
down_revision = "0011_indexing_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_tenant_created")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_tenant_created_id "
        "ON runs (tenant_id, created_at, run_id)"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_knowledge_documents_collection_created"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_collection_created_id "
        "ON knowledge_documents (collection_id, created_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_tenant_created_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_tenant_created "
        "ON runs (tenant_id, created_at)"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_knowledge_documents_collection_created_id"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_collection_created "
        "ON knowledge_documents (collection_id, created_at)"
    )
