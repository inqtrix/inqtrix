"""Mark incognito knowledge runs for their own short retention.

Revision ID: 0094_incognito_runs
Revises: 0093_petrol_theme

An incognito knowledge ask is deleted ``RUN_INCOGNITO_RETENTION_SECONDS``
after it ended instead of after the ordinary run retention. The run service
marks such a run in its request payload; the run store writes this column
from that mark once, at insert. Every existing run is an ordinary run and
takes ``false``. The partial index lets the retention sweep, which runs on
every store access, find the few incognito rows without scanning every
terminal run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094_incognito_runs"
down_revision = "0093_petrol_theme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "incognito",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_runs_incognito_finished",
        "runs",
        ["finished_at"],
        postgresql_where=sa.text("incognito"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_incognito_finished", table_name="runs")
    op.drop_column("runs", "incognito")
