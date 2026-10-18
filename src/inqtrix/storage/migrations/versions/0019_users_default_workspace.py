"""Add users.default_workspace_id (cross-device per-user project namespace).

Revision ID: 0019_users_default_workspace
Revises: 0018_drop_knowledge_workspace_id

Persists the user's canonical project namespace (a ``ws_...`` string), adopted
from the browser's namespace on the first authenticated boot and returned in
/api/auth/session so every device scopes the user's project to the SAME
namespace -- the data follows the user across browsers/devices instead of being
stranded under a per-browser random id. Nullable: NULL until first adopted; this
is a project UI-namespace anchor, NOT an authorization input (auth is
``created_by_sub``) and NOT the server-side collaboration ``workspaces`` table.

``IF NOT EXISTS`` keeps it idempotent across a freshly created database (the
identity ``create_all`` now reads an ORM that already declares the column) and an
already-migrated one (which lacks it).
"""

from __future__ import annotations

from alembic import op

revision = "0019_users_default_workspace"
down_revision = "0018_drop_knowledge_workspace_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_workspace_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS default_workspace_id")
