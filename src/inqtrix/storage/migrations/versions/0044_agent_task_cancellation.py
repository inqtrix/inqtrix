"""Add honest task-level cancellation states.

Revision ID: 0044_agent_task_cancellation
Revises: 0043_agent_task_contract

The existing plan-task row remains the sole cancellation authority. A running
synchronous operation first enters ``cancel_requested`` and becomes
``cancelled`` only after its result has been discarded; pending tasks can move
directly to ``cancelled``.
"""

from __future__ import annotations

from alembic import op

revision = "0044_agent_task_cancellation"
down_revision = "0043_agent_task_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE run_plan_tasks "
        "DROP CONSTRAINT IF EXISTS ck_run_plan_tasks_status"
    )
    op.execute(
        "ALTER TABLE run_plan_tasks ADD CONSTRAINT "
        "ck_run_plan_tasks_status CHECK (status IN "
        "('pending', 'running', 'cancel_requested', 'cancelled', "
        "'completed', 'failed', 'insufficient_evidence', 'skipped'))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE run_plan_tasks SET status = CASE "
        "WHEN status = 'cancel_requested' THEN 'running' "
        "ELSE 'skipped' END "
        "WHERE status IN ('cancel_requested', 'cancelled')"
    )
    op.execute(
        "ALTER TABLE run_plan_tasks "
        "DROP CONSTRAINT IF EXISTS ck_run_plan_tasks_status"
    )
    op.execute(
        "ALTER TABLE run_plan_tasks ADD CONSTRAINT "
        "ck_run_plan_tasks_status CHECK (status IN "
        "('pending', 'running', 'completed', 'failed', "
        "'insufficient_evidence', 'skipped'))"
    )
