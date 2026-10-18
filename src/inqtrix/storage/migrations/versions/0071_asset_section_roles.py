"""Give prepared file-library sections a scope-stable identity.

Revision ID: 0071_asset_section_roles
Revises: 0070_contextualization_circuits

``asset_sections.title`` remains presentation data and is intentionally not
unique.  The nullable ``semantic_role`` is server-owned: the three prepared
roles are unique per tenant/owner/workspace, while any number of ``custom``
sections may share a title.  Existing rows stay NULL.  They are not relabelled
from German titles or inferred from current references because either would
silently turn user data into product-owned data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071_asset_section_roles"
down_revision = "0070_contextualization_circuits"
branch_labels = None
depends_on = None

TABLE = "asset_sections"
PREPARED_ROLE_PREDICATE = (
    "semantic_role IN ('temporary', 'library', 'project_sources')"
)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("semantic_role", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_asset_sections_semantic_role",
        TABLE,
        "semantic_role IS NULL OR semantic_role IN "
        "('temporary', 'library', 'project_sources', 'custom')",
    )
    op.create_index(
        "uq_asset_sections_prepared_role_scope",
        TABLE,
        [
            "tenant_id",
            "created_by_user_id",
            "workspace_id",
            "semantic_role",
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text(PREPARED_ROLE_PREDICATE),
    )


def downgrade() -> None:
    raise RuntimeError(
        "This migration is irreversible: prepared section roles are part of "
        "the durable file-library identity contract. Restore the matching "
        "pre-upgrade backup instead."
    )
