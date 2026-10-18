"""Widen ck_runs_status for the waiting_for_children park.

Revision ID: 0035_children_wait
Revises: 0034_agent_memory_feedback

An agent parent no longer block-polls its child research runs out of
the shared execution pool: it parks slot-free in the NEW
``waiting_for_children`` status and the store re-queues it when the
last child terminates. The ``ck_runs_status`` CHECK is rebuilt from the
status vocabulary deployed with this revision. The literal keeps later
application-enum changes from rewriting migration history. No columns change
-- the park reuses ``waiting_since`` and the 0029 waiting machinery.
"""

from __future__ import annotations

from alembic import op

revision = "0035_children_wait"
down_revision = "0034_agent_memory_feedback"
branch_labels = None
depends_on = None

_STATUS_VALUES = (
    "'queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
    "'waiting_for_children', 'completed', 'failed', 'cancelled', 'expired'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status "
        f"CHECK (status IN ({_STATUS_VALUES}))"
    )


def downgrade() -> None:
    # Pre-0035 code has no children wait: resolve parked rows as
    # cancelled (terminal, TTL-cleaned) instead of leaving values the
    # restored CHECK below would reject. Their in-flight children stay
    # untouched — they are ordinary runs that finish on their own.
    op.execute(
        "UPDATE runs SET status = 'cancelled', finished_at = waiting_since "
        "WHERE status = 'waiting_for_children'"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_status")
    # The historical value set is hardcoded — the live enum contains
    # the status this downgrade removes.
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status CHECK (status IN "
        "('queued', 'running', 'waiting_for_approval', 'waiting_for_input', "
        "'completed', 'failed', 'cancelled', 'expired'))"
    )
