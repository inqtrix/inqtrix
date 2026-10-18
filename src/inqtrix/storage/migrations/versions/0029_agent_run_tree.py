"""Agent run tree: kind/parent/root/session columns + waiting statuses.

Revision ID: 0029_agent_run_tree
Revises: 0028_shares_acceptance

The runs table learns its role in an agent tree (``kind``:
standard/agent/agent_child), its parent/root links, the agent-desk
``session_id`` grouping, and the ``waiting_since`` TTL anchor for the
two new parked statuses (``waiting_for_approval``/``waiting_for_input``).
All columns are additive with defaults, so historical rows and callers
stay byte-identical.

The ``ck_runs_status`` CHECK from migration 0003 is rebuilt from the status
vocabulary deployed with this revision. The literal snapshot keeps later
application-enum changes from rewriting migration history.
"""

from __future__ import annotations

from alembic import op

revision = "0029_agent_run_tree"
down_revision = "0028_shares_acceptance"
branch_labels = None
depends_on = None

_STATUS_VALUES = (
    "'queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
    "'waiting_for_children', 'completed', 'failed', 'cancelled', 'expired'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind text "
        "NOT NULL DEFAULT 'standard'"
    )
    op.execute(
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS parent_run_id text NULL"
    )
    op.execute(
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS root_run_id text NULL"
    )
    op.execute(
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS session_id text NULL"
    )
    op.execute(
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS waiting_since "
        "double precision NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_tenant_parent "
        "ON runs (tenant_id, parent_run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_tenant_session "
        "ON runs (tenant_id, session_id)"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status "
        f"CHECK (status IN ({_STATUS_VALUES}))"
    )


def downgrade() -> None:
    # Pre-0029 code has no waiting statuses: resolve parked rows as
    # cancelled (terminal, TTL-cleaned) instead of leaving values the
    # restored CHECK below would reject.
    op.execute(
        "UPDATE runs SET status = 'cancelled', finished_at = waiting_since "
        "WHERE status IN ('waiting_for_approval', 'waiting_for_input')"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    # The historical value set is hardcoded — the live enum contains
    # the waiting statuses this downgrade removes.
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status CHECK (status IN "
        "('queued', 'running', 'completed', 'failed', 'cancelled'))"
    )
    for column in (
        "waiting_since",
        "session_id",
        "root_run_id",
        "parent_run_id",
        "kind",
    ):
        op.execute(f"ALTER TABLE runs DROP COLUMN IF EXISTS {column}")
