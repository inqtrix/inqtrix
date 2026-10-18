"""Admit deleting a whole comment thread, with its audit trail intact.

Deleting one contribution already leaves a tombstone; a thread had no such
state at all, so an accidental card could only be resolved, never removed.
The thread gains `deleted_at` — the row and its messages stay for the audit,
and every read path skips it, so the card is gone for the people in the
document.

Two guards have to be widened before the service can write the command, and
they fail in different ways: the check constraint on `last_command_kind`
rejects the write outright, which is how an unlisted kind surfaced as a bare
500 once before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0087_comment_thread_deletion"
down_revision = "0086_guest_link_token_rotation"
branch_labels = None
depends_on = None

_TABLE = "editor_collaboration_comment_threads"
_KIND = "ck_collaboration_comment_threads_command_kind"
_KIND_BEFORE = "last_command_kind IN ('create', 'resolve', 'reopen')"
_KIND_AFTER = "last_command_kind IN ('create', 'resolve', 'reopen', 'delete')"
_INDEX = "ix_collaboration_comment_threads_live"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("deleted_at", sa.Float(), nullable=True))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, _KIND_AFTER)
    # Every listing reads live threads only; without this the added predicate
    # would turn the existing status index into a filter-then-scan.
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "document_id", "generation", "updated_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # A deleted thread has no representation in the narrower schema. Dropping
    # the column alone would resurrect it in every list, so the rows go first.
    op.execute(f"DELETE FROM {_TABLE} WHERE deleted_at IS NOT NULL")
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, _KIND_BEFORE)
    op.drop_column(_TABLE, "deleted_at")
