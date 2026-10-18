"""Make active document-revision submission retry-idempotent.

Revision ID: 0064_revision_job_idempotency
Revises: 0063_durable_file_preparation

An immutable revision has at most one active indexing job. Terminal failures
and cancellations release the slot so an explicit retry can create a fresh
job, while a repeated request whose earlier response was lost resolves to the
still-active canonical job.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_revision_job_idempotency"
down_revision = "0063_durable_file_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_indexing_jobs_active_revision",
        "indexing_jobs",
        ["revision_id"],
        unique=True,
        postgresql_where=sa.text(
            "operation_kind = 'document_revision' AND "
            "status IN ('queued', 'running', 'cancelling', "
            "'paused_dependency', 'paused_validation')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_indexing_jobs_active_revision", table_name="indexing_jobs"
    )
