"""Add the agent-memory opt-in to account preferences.

Revision ID: 0038_account_memory_optin
Revises: 0037_runs_sub_active_index

Long-term agent memory becomes opt-in per user (privacy default OFF). The
column defaults to ``false`` so old rows and old clients resolve to
memory-less; a user turns it on in Settings and the agent then reads/writes
their long-term memory.
"""

from __future__ import annotations

from alembic import op

revision = "0038_account_memory_optin"
down_revision = "0037_runs_sub_active_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE account_preferences "
        "ADD COLUMN IF NOT EXISTS enable_agent_memory boolean NOT NULL "
        "DEFAULT false"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE account_preferences "
        "DROP COLUMN IF EXISTS enable_agent_memory"
    )
