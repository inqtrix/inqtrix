"""Add imported-run identity and explicit execution authority.

Revision ID: 0046_execution_authority
Revises: 0045_canonical_user_ids

Imported reports no longer reuse a client-controlled id as the public run id.
``source_run_id`` is an owner-scoped idempotency key while ``run_id`` remains
server-generated and never reusable. The execution columns establish the
schema contract for the separate effective-actor/safepoint phase.

Reindex cancellation becomes an explicit active state. Consequently the
one-active-job index continues to reserve a collection while cancellation is
pending instead of briefly reopening it to mutations or another reindex.
"""

from __future__ import annotations

from alembic import op

revision = "0046_execution_authority"
down_revision = "0045_canonical_user_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Install import idempotency, actor leases, and cancelling status."""
    # Alembic executes this revision transactionally. Plain DDL deliberately
    # fails closed when an operator-created column or index already occupies a
    # v0.2 name with an unknown definition.
    op.execute("ALTER TABLE runs ADD COLUMN source_run_id text NULL")
    op.execute(
        "ALTER TABLE runs ADD COLUMN execution_actor_user_id uuid NULL"
    )
    op.execute(
        "ALTER TABLE runs ADD COLUMN execution_scopes json NOT NULL "
        "DEFAULT '[]'::json"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_runs_import_owner_source ON runs "
        "(tenant_id, created_by_user_id, source_run_id) "
        "WHERE created_by_user_id IS NOT NULL AND source_run_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_runs_import_unscoped_source ON runs "
        "(tenant_id, source_run_id) "
        "WHERE created_by_user_id IS NULL AND source_run_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT fk_runs_execution_actor_user "
        "FOREIGN KEY (execution_actor_user_id) REFERENCES users(id) "
        "ON DELETE RESTRICT"
    )

    op.execute(
        "ALTER TABLE indexing_jobs DROP CONSTRAINT ck_indexing_jobs_status"
    )
    op.execute(
        "ALTER TABLE indexing_jobs ADD CONSTRAINT ck_indexing_jobs_status "
        "CHECK (status IN "
        "('queued', 'running', 'cancelling', 'completed', 'failed', "
        "'cancelled', 'expired'))"
    )
    op.execute("DROP INDEX uq_indexing_jobs_active_collection")
    op.execute(
        "CREATE UNIQUE INDEX uq_indexing_jobs_active_collection "
        "ON indexing_jobs (collection_id) "
        "WHERE status IN ('queued', 'running', 'cancelling')"
    )


def downgrade() -> None:
    """Reject downgrade across the irreversible v0.2 identity boundary."""
    raise RuntimeError(
        "0046_execution_authority cannot be downgraded across the "
        "irreversible 0045 identity hard cut"
    )
