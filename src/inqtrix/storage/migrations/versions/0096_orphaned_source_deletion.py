"""Delete a source that outlived its asset, section or document.

Revision ID: 0096_orphaned_source_deletion
Revises: 0095_user_erasure_operation

A source's lifecycle row is the only map to what the Knowledge store holds
for it. When the asset, section or document it belonged to is already gone
but the row still says the source is live, an account erasure settles it as
a deletion operation of the new target kind ``source``: clean up by the
exact plan, verify zero residuals, keep the tombstone.
"""

from __future__ import annotations

from alembic import op

revision = "0096_orphaned_source_deletion"
down_revision = "0095_user_erasure_operation"
branch_labels = None
depends_on = None

_KINDS_BEFORE = (
    "'asset', 'bulk', 'group', 'section', 'vector_index', "
    "'knowledge_collection', 'knowledge_document', 'agent_session', "
    "'knowledge_session', 'user'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        f"target_kind IN ({_KINDS_BEFORE}, 'source')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Orphaned-source deletion is irreversible: schema downgrade would "
        "discard the receipts of settled sources. Restore the matching "
        "pre-upgrade database backup instead."
    )
