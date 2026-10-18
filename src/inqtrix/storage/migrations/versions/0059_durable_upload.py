"""Add durable, retry-safe bound-file upload operations.

Revision ID: 0059_durable_upload
Revises: 0058_source_lifecycle

The operation ledger is written after request spooling but before the object
put.  Its outbox makes every non-terminal checkpoint recoverable.  The same
revision closes the historical ambiguity in ``asset_records.server_file_id``:
unsafe existing data aborts the migration with counts instead of being guessed
or silently deleted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059_durable_upload"
down_revision = "0058_source_lifecycle"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
_UPLOAD_TABLES = (
    "upload_operation_events",
    "upload_operation_outbox",
    "upload_operations",
)

_ASSET_FILE_BINDING_PREFLIGHT_SQL = """
DO $$
DECLARE
    duplicate_bindings bigint;
    dangling_bindings bigint;
    tenant_mismatches bigint;
    owner_mismatches bigint;
    workspace_mismatches bigint;
BEGIN
    SELECT count(*) INTO duplicate_bindings
    FROM (
        SELECT server_file_id
        FROM asset_records
        WHERE server_file_id IS NOT NULL
        GROUP BY server_file_id
        HAVING count(*) > 1
    ) AS duplicate_groups;

    SELECT count(*) INTO dangling_bindings
    FROM asset_records AS asset
    LEFT JOIN files AS original ON original.id = asset.server_file_id
    WHERE asset.server_file_id IS NOT NULL
      AND original.id IS NULL;

    SELECT count(*) INTO tenant_mismatches
    FROM asset_records AS asset
    JOIN files AS original ON original.id = asset.server_file_id
    WHERE asset.tenant_id IS DISTINCT FROM original.tenant_id;

    SELECT count(*) INTO owner_mismatches
    FROM asset_records AS asset
    JOIN files AS original ON original.id = asset.server_file_id
    WHERE asset.created_by_user_id IS DISTINCT FROM original.owner_user_id;

    SELECT count(*) INTO workspace_mismatches
    FROM asset_records AS asset
    JOIN files AS original ON original.id = asset.server_file_id
    WHERE asset.workspace_id IS DISTINCT FROM original.workspace_id;

    IF duplicate_bindings > 0
       OR dangling_bindings > 0
       OR tenant_mismatches > 0
       OR owner_mismatches > 0
       OR workspace_mismatches > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'Unsafe asset/file bindings block durable upload migration: '
                'duplicate_groups=%s dangling=%s tenant_mismatch=%s '
                'owner_mismatch=%s workspace_mismatch=%s. Resolve the exact '
                'rows explicitly; this migration never chooses or deletes a '
                'binding automatically.',
                duplicate_bindings,
                dangling_bindings,
                tenant_mismatches,
                owner_mismatches,
                workspace_mismatches
            );
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_ASSET_FILE_BINDING_PREFLIGHT_SQL)

    op.create_index(
        "uq_asset_records_server_file_id",
        "asset_records",
        ["server_file_id"],
        unique=True,
        postgresql_where=sa.text("server_file_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_asset_records_server_file_id_files",
        "asset_records",
        "files",
        ["server_file_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "asset_records",
        sa.Column("upload_operation_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_asset_records_upload_operation",
        "asset_records",
        ["tenant_id", "upload_operation_id"],
    )
    op.drop_constraint("ck_asset_records_upload_status", "asset_records", type_="check")
    op.create_check_constraint(
        "ck_asset_records_upload_status",
        "asset_records",
        "upload_status IN ('awaiting_upload', 'uploading', 'retrying', "
        "'finalizing', 'ready', 'failed', 'cancelled')",
    )

    op.create_table(
        "upload_operations",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("asset_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column(
            "file_manifest",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "binding",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
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
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "event_seq", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'queued', 'awaiting_bytes', "
            "'upload_failed', 'ready')",
            name="ck_upload_operations_status",
        ),
        sa.CheckConstraint(
            "stage IN ('prepared', 'object_stored', 'file_registered', "
            "'asset_bound', 'quota_booked', 'ready')",
            name="ck_upload_operations_stage",
        ),
    )
    op.create_index(
        "ix_upload_operations_owner_created",
        "upload_operations",
        ["tenant_id", "created_by_user_id", "created_at"],
    )
    op.create_index(
        "ix_upload_operations_status",
        "upload_operations",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "uq_upload_operations_active_asset",
        "upload_operations",
        [
            "tenant_id",
            "asset_id",
            sa.text("COALESCE(created_by_user_id::text, '')"),
            sa.text("COALESCE(workspace_id, '')"),
        ],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('running', 'queued', 'awaiting_bytes', 'upload_failed')"
        ),
    )

    op.create_table(
        "upload_operation_events",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("upload_operations.operation_id", ondelete="CASCADE"),
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
        "upload_operation_outbox",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("upload_operations.operation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column(
            "dispatch_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_dispatched_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_upload_operation_outbox_due",
        "upload_operation_outbox",
        ["tenant_id", "available_at"],
    )

    for table in _UPLOAD_TABLES:
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC, {APP_ROLE}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}"
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
    # There is no truthful automatic mapping from an in-flight durable upload
    # back to the old four-state asset contract.  Re-labelling an operation as
    # ``failed`` would destroy its recovery checkpoint and lie to operators.
    # Require an explicit, audited drain/remediation before any schema rollback.
    connection = op.get_bind()
    active = connection.execute(
        sa.text("SELECT count(*) FROM upload_operations " "WHERE status <> 'ready'")
    ).scalar_one()
    incompatible_assets = connection.execute(
        sa.text(
            "SELECT count(*) FROM asset_records "
            "WHERE upload_status NOT IN ('uploading', 'ready', 'failed', 'cancelled')"
        )
    ).scalar_one()
    if active or incompatible_assets:
        raise RuntimeError(
            "Durable upload downgrade blocked: "
            f"active_operations={active} "
            f"incompatible_asset_states={incompatible_assets}. "
            "Drain or remediate the exact rows explicitly before retrying."
        )

    op.drop_index(
        "ix_upload_operation_outbox_due",
        table_name="upload_operation_outbox",
    )
    op.drop_table("upload_operation_outbox")
    op.drop_table("upload_operation_events")
    op.drop_index("uq_upload_operations_active_asset", table_name="upload_operations")
    op.drop_index("ix_upload_operations_status", table_name="upload_operations")
    op.drop_index("ix_upload_operations_owner_created", table_name="upload_operations")
    op.drop_table("upload_operations")

    op.drop_constraint("ck_asset_records_upload_status", "asset_records", type_="check")
    op.create_check_constraint(
        "ck_asset_records_upload_status",
        "asset_records",
        "upload_status IN ('uploading', 'ready', 'failed', 'cancelled')",
    )
    op.drop_index("ix_asset_records_upload_operation", table_name="asset_records")
    op.drop_column("asset_records", "upload_operation_id")
    op.drop_constraint(
        "fk_asset_records_server_file_id_files",
        "asset_records",
        type_="foreignkey",
    )
    op.drop_index("uq_asset_records_server_file_id", table_name="asset_records")
