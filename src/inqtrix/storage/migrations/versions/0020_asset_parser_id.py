"""Add parser_id provenance to asset_records.

Revision ID: 0020_asset_parser_id
Revises: 0019_users_default_workspace

Records which parser produced an asset's ``extracted_text`` so the UI can show
it transparently: ``markitdown`` for the server-side parser ladder, ``client``
for the browser fallback. Nullable with no default — existing rows stay
``NULL`` (unknown provenance), which the frontend renders as no badge rather
than guessing. Mirrors the ``server_file_id`` column added with the schema.

``IF NOT EXISTS`` / ``IF EXISTS`` keep this idempotent across a freshly created
database (the ``0015`` create_all now reads an ORM that already carries the
column, so there is nothing to add) and an already-migrated database.
"""

from __future__ import annotations

from alembic import op

revision = "0020_asset_parser_id"
down_revision = "0019_users_default_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE asset_records ADD COLUMN IF NOT EXISTS parser_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE asset_records DROP COLUMN IF EXISTS parser_id")
