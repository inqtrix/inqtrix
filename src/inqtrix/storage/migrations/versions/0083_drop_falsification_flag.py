"""Drop the plan task's falsification flag.

Revision ID: 0083_drop_falsification_flag
Revises: 0082_prompt_seed_markers

The planner no longer plans verification, cross-check or falsification
tasks — checking the result is the critic's job after the synthesis — so
nothing sets the flag any more and nothing reads it: the plan model, the
control record, the API projection and the executing child's prompt lost
it in the same change. The column follows; historic rows keep their
other fields untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083_drop_falsification_flag"
down_revision = "0082_prompt_seed_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("run_plan_tasks", "is_falsification")


def downgrade() -> None:
    op.add_column(
        "run_plan_tasks",
        sa.Column(
            "is_falsification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
