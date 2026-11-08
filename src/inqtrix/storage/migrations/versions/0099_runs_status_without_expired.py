"""Remove ``expired`` from the run statuses.

Revision ID: 0099_runs_status_without_expired
Revises: 0098_project_namespace_backfill

No run reaches ``expired``: a wait that runs out ends the run as
``cancelled`` with its reason. The ``ck_runs_status`` CHECK still allowed the
value; this revision narrows it. A run that still carries it stops the
upgrade with its count, because only the operator can say what it should
become; nothing is rewritten. The downgrade allows the value again and loses
nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0099_runs_status_without_expired"
down_revision = "0098_project_namespace_backfill"
branch_labels = None
depends_on = None

_STATUS_VALUES = (
    "'queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
    "'waiting_for_children', 'completed', 'failed', 'cancelled'"
)


def upgrade() -> None:
    expired = op.get_bind().execute(
        sa.text("SELECT count(*) FROM runs WHERE status = 'expired'")
    ).scalar_one()
    if expired:
        raise RuntimeError(
            f"{expired} runs still have the status 'expired'. Set them to "
            "'cancelled' or 'failed' before this upgrade; nothing was changed."
        )
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status "
        f"CHECK (status IN ({_STATUS_VALUES}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    # The historical value set is hardcoded: the live enum no longer has it.
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status CHECK (status IN "
        "('queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
        "'waiting_for_children', 'completed', 'failed', 'cancelled', 'expired'))"
    )
