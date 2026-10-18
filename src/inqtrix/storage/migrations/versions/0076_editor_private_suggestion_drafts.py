"""Persist creator-private editor AI drafts on their owning private comment.

The JSONB value is an explicitly revisioned nested resource. Ordinary comment
autosave never writes it; dedicated API/store guards own create, revision and
discard. Document and comment deletion already provide the required cascade.

Revision ID: 0076_editor_private_drafts
Revises: 0075_audit_session_references
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0076_editor_private_drafts"
down_revision = "0075_audit_session_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editor_comments",
        sa.Column(
            "suggestion_draft",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_editor_comments_suggestion_draft_object",
        "editor_comments",
        "suggestion_draft IS NULL OR "
        "jsonb_typeof(suggestion_draft) = 'object'",
    )
    op.create_index(
        "ux_editor_comments_private_draft_patch",
        "editor_comments",
        ["tenant_id", sa.text("(suggestion_draft ->> 'patch_id')")],
        unique=True,
        postgresql_where=sa.text("suggestion_draft IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "This irreversible migration may contain unpublished private AI work. "
        "Restore the matching pre-upgrade backup instead of dropping that "
        "user data."
    )
