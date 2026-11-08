"""Bind every run to the workflow contract it was accepted under.

Revision ID: 0100_runs_workflow_contract
Revises: 0099_runs_status_without_expired

A run now carries ``(mode, workflow_contract)``: its mode and the workflow
contract it was accepted under. Existing runs were all accepted under the
first contract, so they take 1. Afterwards neither column has a default: every
write names the mode and the contract itself, and a row without them fails
instead of being bound to something it never asked for. A contract is at
least 1.

Adding the column with a constant default and dropping that default again
leaves existing rows at 1 without rewriting the table. The downgrade drops
only the binding and gives ``mode`` its old default back; an upgrade after it
binds every run to the first contract again. An open run bound to a later
contract would come back bound to the first one and be judged by it, so the
downgrade stops with their count while such runs exist; nothing is changed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100_runs_workflow_contract"
down_revision = "0099_runs_status_without_expired"
branch_labels = None
depends_on = None

_OPEN_STATUSES = (
    "'queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
    "'waiting_for_children'"
)


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "workflow_contract",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.alter_column("runs", "workflow_contract", server_default=None)
    op.alter_column("runs", "mode", server_default=None)
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_workflow_contract "
        "CHECK (workflow_contract >= 1)"
    )


def downgrade() -> None:
    later = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM runs WHERE workflow_contract <> 1 "
            f"AND status IN ({_OPEN_STATUSES})"
        )
    ).scalar_one()
    if later:
        raise RuntimeError(
            f"{later} open runs are bound to a workflow contract above 1. "
            "Cancel them before this downgrade; nothing was changed."
        )
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_workflow_contract")
    op.alter_column("runs", "mode", server_default=sa.text("'research'"))
    op.drop_column("runs", "workflow_contract")
