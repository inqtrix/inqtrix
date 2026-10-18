"""Drop the dead workspace_id column from knowledge_collections.

Revision ID: 0018_drop_knowledge_workspace_id
Revises: 0017_account_preferences

The column was created with the knowledge schema but never read or filtered
(the Postgres store wrote it always-NULL). Knowledge collections are the
cross-workspace SHARING surface — owner-scoped (``created_by_sub``) + ACL via
``resource_shares`` — NOT per-workspace project data, so a workspace dimension
is conceptually wrong here (see docs/architecture/
data-architecture.md). A dead always-NULL column is an inert field and
misleads readers into thinking knowledge is
workspace-scoped, so it is dropped.

``IF EXISTS`` / ``IF NOT EXISTS`` keep this idempotent across both a freshly
created database (the ``0010`` create_all now reads an ORM without the column,
so there is nothing to drop) and an already-migrated database (which still
carries the column from the old ``0010``).
"""

from __future__ import annotations

from alembic import op

revision = "0018_drop_knowledge_workspace_id"
down_revision = "0017_account_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_collections DROP COLUMN IF EXISTS workspace_id")


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_collections ADD COLUMN IF NOT EXISTS workspace_id text")
