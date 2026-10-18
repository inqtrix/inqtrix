"""Separate title ownership from session activity timestamps.

Existing titles are manual: loading an older conversation must never initiate
automatic renaming. The revision is incremented by the row update, so a late
model result cannot overwrite a committed manual rename.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085_session_title_revision"
down_revision = "0084_revision_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("chat_threads", "agent_sessions"):
        op.add_column(
            table,
            sa.Column(
                "title_revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "title_source",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
        )
        op.create_check_constraint(
            f"ck_{table}_title_revision", table, "title_revision >= 0"
        )
        op.create_check_constraint(
            f"ck_{table}_title_source",
            table,
            "title_source IN ('initial', 'generated', 'manual')",
        )


def downgrade() -> None:
    for table in ("chat_threads", "agent_sessions"):
        op.drop_constraint(f"ck_{table}_title_source", table, type_="check")
        op.drop_constraint(f"ck_{table}_title_revision", table, type_="check")
        op.drop_column(table, "title_source")
        op.drop_column(table, "title_revision")
