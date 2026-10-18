"""Persist immutable run starts and execution-segment timing.

Revision ID: 0056_run_runtime
Revises: 0055_knowledge_revision

Runs keep their first start timestamp across human and child waits.  Bounded
timing accumulators and a current segment identity make resumes auditable
without retaining a second mutable execution history outside the run events.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_run_runtime"
down_revision = "0055_knowledge_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column(
            "segment_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("current_segment_id", sa.Text(), nullable=True),
        sa.Column("current_segment_reason", sa.Text(), nullable=True),
        sa.Column("queued_since", sa.Float(), nullable=True),
        sa.Column("active_started_at", sa.Float(), nullable=True),
        sa.Column(
            "active_seconds",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "waiting_seconds",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "queued_seconds",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    ):
        op.add_column("runs", column)

    # Historical rows have no segment boundary events to split active and
    # waiting time perfectly. Preserve their observable wall duration as the
    # active accumulator; all newly written transitions are exact.
    op.execute(
        """
        UPDATE runs
        SET segment_count = CASE WHEN started_at IS NULL THEN 0 ELSE 1 END,
            current_segment_id = CASE
                WHEN started_at IS NULL THEN NULL
                ELSE 'seg_' || substr(md5(run_id), 1, 20) || '_1'
            END,
            current_segment_reason = CASE
                WHEN started_at IS NULL THEN NULL ELSE 'legacy'
            END,
            queued_since = NULL,
            active_started_at = CASE
                WHEN status = 'running' THEN started_at ELSE NULL
            END,
            queued_seconds = 0,
            active_seconds = CASE
                WHEN finished_at IS NOT NULL AND started_at IS NOT NULL
                    THEN GREATEST(0, finished_at - started_at)
                WHEN waiting_since IS NOT NULL AND started_at IS NOT NULL
                    THEN GREATEST(0, waiting_since - started_at)
                ELSE 0
            END
        """
    )


def downgrade() -> None:
    for name in (
        "queued_seconds",
        "waiting_seconds",
        "active_seconds",
        "active_started_at",
        "queued_since",
        "current_segment_reason",
        "current_segment_id",
        "segment_count",
    ):
        op.drop_column("runs", name)
