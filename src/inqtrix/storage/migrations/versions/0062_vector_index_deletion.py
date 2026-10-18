"""Make server-backed index and knowledge deletion durable operations.

Revision ID: 0062_vector_index_deletion
Revises: 0061_indexing_operation_kinds
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062_vector_index_deletion"
down_revision = "0061_indexing_operation_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deletion_operations",
        sa.Column(
            "context",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.drop_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        "target_kind IN ('asset', 'bulk', 'group', 'section', 'vector_index', "
        "'knowledge_collection', 'knowledge_document')",
    )
    op.drop_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        "stage IN ('queued', 'vector_index_detached', 'indexing_cancelled', "
        "'search_detached', 'vectors_removed', 'knowledge_removed', "
        "'blobs_removed', 'metadata_removed', 'residuals_verified', "
        "'delete_failed', 'deleted')",
    )
    op.drop_constraint(
        "ck_vector_index_records_status",
        "vector_index_records",
        type_="check",
    )
    op.create_check_constraint(
        "ck_vector_index_records_status",
        "vector_index_records",
        "status IN ('error', 'indexing', 'ready', 'stale', "
        "'deleting', 'delete_failed')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Vector-index deletion is irreversible: schema downgrade would "
        "discard exact cleanup checkpoints or relabel unfinished deletion as "
        "a generic error. Restore the matching pre-upgrade database backup "
        "instead."
    )
    op.drop_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        "stage IN ('queued', 'search_detached', 'vectors_removed', "
        "'knowledge_removed', 'blobs_removed', 'metadata_removed', "
        "'residuals_verified', 'delete_failed', 'deleted')",
    )
    op.drop_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        "target_kind IN ('asset', 'bulk', 'group', 'section')",
    )
    op.drop_column("deletion_operations", "context")
