"""Make durable Agent and Knowledge Desk session deletion resumable.

Revision ID: 0067_session_deletion_contract
Revises: 0066_quota_stock_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_session_deletion_contract"
down_revision = "0066_quota_stock_lifecycle"
branch_labels = None
depends_on = None

_SESSION_TABLES = ("agent_sessions", "knowledge_sessions")


def upgrade() -> None:
    for table in _SESSION_TABLES:
        op.add_column(
            table,
            sa.Column(
                "lifecycle_status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'active'"),
            ),
        )
        op.add_column(
            table,
            sa.Column("deletion_operation_id", sa.Text(), nullable=True),
        )
        op.add_column(table, sa.Column("deletion_stage", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("deletion_error", sa.Text(), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_lifecycle_status",
            table,
            "lifecycle_status IN ('active', 'deleting', 'delete_failed')",
        )
        op.create_index(
            f"ix_{table}_deletion_operation",
            table,
            ["tenant_id", "deletion_operation_id"],
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
        "'knowledge_collection', 'knowledge_document', 'agent_session', "
        "'knowledge_session')",
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
        "'session_data_removed', 'blobs_removed', 'metadata_removed', "
        "'residuals_verified', 'delete_failed', 'deleted')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Durable session deletion is irreversible: schema downgrade would "
        "discard cleanup receipts and could expose a partially deleted "
        "session as active. Restore the matching pre-upgrade database backup "
        "instead."
    )
