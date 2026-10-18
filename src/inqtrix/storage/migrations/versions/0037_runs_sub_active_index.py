"""Index created_by_sub over active runs for the per-user in-flight cap.

Revision ID: 0037_runs_sub_active_index
Revises: 0036_agent_memory_rls_guc

The optional per-user in-flight run cap COUNTs
``created_by_sub = ? AND kind != 'agent' AND status IN ('queued','running')``
inside every ``POST /v1/runs`` transaction. No ``runs`` index led with
``created_by_sub``; the tenant-leading indexes do not narrow by subject on a
single-tenant deployment, so the COUNT degraded to a status scan over every
active run fleet-wide per submit. A partial index on
``(created_by_sub, status)`` restricted to the two active statuses turns it
into a user-scoped index lookup (the ``kind != 'agent'`` residual is cheap on
that small set). Matches the ORM ``ix_runs_sub_active`` so fresh installs and
already-migrated databases converge; idempotent both directions.
"""

from __future__ import annotations

from alembic import op

revision = "0037_runs_sub_active_index"
down_revision = "0036_agent_memory_rls_guc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_sub_active "
        "ON runs (created_by_sub, status) "
        "WHERE status IN ('queued', 'running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_sub_active")
