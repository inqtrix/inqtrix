"""Add truthful aggregate asset-deletion lifecycle and durable receipts.

Revision ID: 0057_asset_deletion
Revises: 0056_run_runtime

An asset remains addressable while its server-owned deletion operation removes
search evidence, vector memberships, knowledge records, and the original blob.
Only a zero-residual operation may remove the final metadata row.  Failed
operations retain their identifiers and checkpoint for an explicit retry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_asset_deletion"
down_revision = "0056_run_runtime"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
_TABLES = (
    "deletion_operations",
    "deletion_operation_assets",
    "deletion_operation_events",
    "quota_usage_adjustments",
)


def upgrade() -> None:
    op.add_column(
        "asset_records",
        sa.Column(
            "lifecycle_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )
    op.add_column(
        "asset_records",
        sa.Column("deletion_operation_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "asset_records",
        sa.Column("deletion_stage", sa.Text(), nullable=True),
    )
    op.add_column(
        "asset_records",
        sa.Column("deletion_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "asset_records",
        sa.Column(
            "upload_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ready'"),
        ),
    )
    op.add_column(
        "asset_records",
        sa.Column("upload_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_asset_records_lifecycle",
        "asset_records",
        "lifecycle_status IN ('active', 'deleting', 'delete_failed')",
    )
    op.create_check_constraint(
        "ck_asset_records_upload_status",
        "asset_records",
        "upload_status IN ('uploading', 'ready', 'failed', 'cancelled')",
    )
    op.create_index(
        "ix_asset_records_deletion_operation",
        "asset_records",
        ["tenant_id", "deletion_operation_id"],
    )

    op.create_table(
        "deletion_operations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "manifest",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "completed_items",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_items",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("workspace_id", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "error",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "event_seq",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "target_kind IN ('asset', 'bulk', 'group', 'section')",
            name="ck_deletion_operations_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'delete_failed', 'deleted')",
            name="ck_deletion_operations_status",
        ),
        sa.CheckConstraint(
            "stage IN ('queued', 'search_detached', 'vectors_removed', "
            "'knowledge_removed', 'blobs_removed', 'metadata_removed', "
            "'residuals_verified', 'delete_failed', 'deleted')",
            name="ck_deletion_operations_stage",
        ),
        sa.CheckConstraint(
            "completed_items >= 0 AND total_items >= 0 "
            "AND completed_items <= total_items",
            name="ck_deletion_operations_progress",
        ),
    )
    op.create_index(
        "ix_deletion_operations_owner_created",
        "deletion_operations",
        ["tenant_id", "created_by_user_id", "created_at"],
    )
    op.create_index(
        "ix_deletion_operations_status",
        "deletion_operations",
        ["tenant_id", "status"],
    )
    op.create_index(
        "uq_deletion_operations_active_target",
        "deletion_operations",
        [
            "tenant_id",
            "target_kind",
            "target_id",
            sa.text("COALESCE(created_by_user_id::text, '')"),
            sa.text("COALESCE(workspace_id, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "deletion_operation_assets",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("deletion_operations.operation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("asset_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_deletion_operation_assets_lookup",
        "deletion_operation_assets",
        ["tenant_id", "asset_id"],
    )
    op.create_table(
        "deletion_operation_events",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("deletion_operations.operation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_table(
        "quota_usage_adjustments",
        sa.Column("adjustment_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "subject_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Float(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_quota_usage_adjustments_subject",
        "quota_usage_adjustments",
        ["tenant_id", "subject_user_id", "created_at"],
    )

    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "Durable deletion is irreversible: schema downgrade would discard "
        "deletion checkpoints, audit receipts, quota receipts, and aggregate "
        "lifecycle state. Restore the matching pre-upgrade database backup "
        "instead."
    )
