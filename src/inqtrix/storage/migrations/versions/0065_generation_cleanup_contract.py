"""Make generation retention crash-convergent and visibly retryable.

Revision ID: 0065_generation_cleanup_contract
Revises: 0064_revision_job_idempotency

Expired rollback generations leave ``rollback_available`` before vector
deletion starts. Interrupted cleanup remains either ``deleting`` or
``cleanup_failed`` so no database row advertises vectors that may already be
gone, and the worker can resume the exact generation idempotently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_generation_cleanup_contract"
down_revision = "0064_revision_job_idempotency"
branch_labels = None
depends_on = None


def _status_constraint(*, include_cleanup: bool) -> sa.CheckConstraint:
    statuses = [
        "building",
        "active",
        "rollback_available",
        "failed",
        "deleted",
    ]
    if include_cleanup:
        statuses.extend(("deleting", "cleanup_failed"))
    quoted = ", ".join(f"'{status}'" for status in statuses)
    return sa.CheckConstraint(
        f"status IN ({quoted})",
        name="ck_knowledge_index_generations_status",
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_index_generations_status",
        "knowledge_index_generations",
        type_="check",
    )
    op.create_check_constraint(
        _status_constraint(include_cleanup=True).name,
        "knowledge_index_generations",
        _status_constraint(include_cleanup=True).sqltext,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Generation cleanup is irreversible: schema downgrade would erase "
        "the distinction between unfinished cleanup and a failed build. "
        "Restore the matching pre-upgrade database backup instead."
    )
