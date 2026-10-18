"""Record the suggestions an edit removed on the collaboration update row.

Revision ID: 0091_rejected_by_edit
Revises: 0090_editor_validation_rules

A writer with edit rights may delete text that holds pending suggestions. A
suggestion whose every proposed character such an update deletes is removed
by the update's actor: the store marks its patch rejected (or shrinks the
patch, when other suggestions of it remain) in the transaction that appends
the update, and writes one audit entry per removed suggestion naming its
author.

The update row names the suggestions it removed in
``rejected_suggestion_ids`` so the activity history can show the removal
with the edit that caused it. Only a plain human or guest edit can remove
suggestions this way: a decision row decides through its patches, and an
assistant, agent or system row never holds an edit lease. Existing rows
removed nothing and take the empty list.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0091_rejected_by_edit"
down_revision = "0090_editor_validation_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editor_collaboration_updates",
        sa.Column(
            "rejected_suggestion_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_collaboration_updates_rejected_suggestion_ids",
        "editor_collaboration_updates",
        "jsonb_typeof(rejected_suggestion_ids) = 'array'",
    )
    op.create_check_constraint(
        "ck_collaboration_updates_rejected_by_edit",
        "editor_collaboration_updates",
        "rejected_suggestion_ids = '[]'::jsonb OR ("
        "decision_outcome IS NULL "
        "AND change_kind IN ('direct', 'suggestion') "
        "AND actor_kind IN ('human', 'guest'))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_collaboration_updates_rejected_by_edit",
        "editor_collaboration_updates",
        type_="check",
    )
    op.drop_constraint(
        "ck_collaboration_updates_rejected_suggestion_ids",
        "editor_collaboration_updates",
        type_="check",
    )
    op.drop_column("editor_collaboration_updates", "rejected_suggestion_ids")
