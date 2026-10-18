"""Allow the machine-carrier comment kind ``assistant_edit``.

Revision ID: 0080_assistant_edit_comment_kind
Revises: 0079_authorization_generation

A creator-private AI proposal is only publishable when a private draft
authorises exactly its ``patch_id``/``command_id`` beforehand, and the only
place a draft can live is ``editor_comments.suggestion_draft``. The editor
assistant has no comment of the user's to hang one on, so it needs a carrier
row of its own.

Reusing ``inline_edit`` for that would make every machine carrier look like a
note the user wrote: the private-notes list would fill with one entry per
edit, both counters would count them, and each would offer "generate
suggestion" back at the user. ``assistant_edit`` keeps the carrier out of
that surface while leaving the guard, the draft column and every existing
kind untouched.

Widening a CHECK constraint accepts every row the old one accepted, so no
data is rewritten and the downgrade is safe as long as no carrier rows
exist yet — which is why the downgrade deletes them explicitly rather than
failing on a constraint violation.
"""

from __future__ import annotations

from alembic import op

revision = "0080_assistant_edit_comment_kind"
down_revision = "0079_authorization_generation"
branch_labels = None
depends_on = None

_OLD_KINDS = "('collect', 'inline_edit', 'evidence_review')"
_NEW_KINDS = "('collect', 'inline_edit', 'evidence_review', 'assistant_edit')"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE editor_comments DROP CONSTRAINT ck_editor_comments_kind"
    )
    op.execute(
        "ALTER TABLE editor_comments ADD CONSTRAINT ck_editor_comments_kind "
        f"CHECK (kind IN {_NEW_KINDS})"
    )


def downgrade() -> None:
    # Carrier rows cannot satisfy the narrower constraint. They hold no user
    # content -- only the anchor a private draft needed -- so removing them is
    # the honest reversal, not a loss.
    op.execute("DELETE FROM editor_comments WHERE kind = 'assistant_edit'")
    op.execute(
        "ALTER TABLE editor_comments DROP CONSTRAINT ck_editor_comments_kind"
    )
    op.execute(
        "ALTER TABLE editor_comments ADD CONSTRAINT ck_editor_comments_kind "
        f"CHECK (kind IN {_OLD_KINDS})"
    )
