"""Record the write status on artifact revision snapshots.

Revision ID: 0084_revision_status
Revises: 0083_drop_falsification_flag

A memo is flushed section by section while it is written, and every
flush is a revision snapshot (diff source, crash recovery). The reader's
version picker listed all of them — a fresh memo showed "Revision 22",
and its early revisions were fragments. Each snapshot now carries the
artifact status of its write (``writing`` for a streaming flush, ``ready``
for a finished write or a user edit) so the picker can skip the
streaming ones. Historic rows cannot be told apart and keep the default
``ready``: their lists stay as they were.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084_revision_status"
down_revision = "0083_drop_falsification_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_artifact_revisions",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ready'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("run_artifact_revisions", "status")
