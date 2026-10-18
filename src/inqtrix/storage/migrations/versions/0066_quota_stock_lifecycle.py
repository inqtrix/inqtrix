"""Make stored-byte accounting converge on physical file lifecycles.

Revision ID: 0066_quota_stock_lifecycle
Revises: 0065_generation_cleanup_contract

Each file contributes through one durable stock row.  A deletion tombstone is
permanent, so a late upload receipt cannot charge bytes after that lifecycle
has been deleted.  Existing counters are rebuilt from the current file
registry instead of preserving historical adjustment-order drift.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066_quota_stock_lifecycle"
down_revision = "0065_generation_cleanup_contract"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
TABLE = "quota_stock_lifecycles"

_QUOTA_POSTCONDITION_SQL = """
DO $$
DECLARE counter_mismatches bigint;
DECLARE stock_mismatches bigint;
BEGIN
    WITH expected AS (
        SELECT tenant_id, owner_user_id AS subject_user_id,
               SUM(GREATEST(0, size_bytes)) AS used
        FROM files
        WHERE owner_user_id IS NOT NULL
        GROUP BY tenant_id, owner_user_id
    ), actual AS (
        SELECT tenant_id, subject_user_id, used
        FROM quota_usage_counters
        WHERE dimension = 'stored_bytes' AND period_start = 0
    )
    SELECT count(*) INTO counter_mismatches
    FROM expected FULL OUTER JOIN actual
      USING (tenant_id, subject_user_id)
    WHERE COALESCE(expected.used, 0) <> COALESCE(actual.used, 0);

    WITH expected AS (
        SELECT DISTINCT original.tenant_id,
               'file:' || original.id AS stock_key,
               original.owner_user_id AS subject_user_id,
               GREATEST(0, original.size_bytes) AS amount
        FROM files AS original
        JOIN asset_records AS asset
          ON asset.tenant_id = original.tenant_id
         AND asset.server_file_id = original.id
        WHERE original.owner_user_id IS NOT NULL
    ), actual AS (
        SELECT tenant_id, stock_key, subject_user_id, amount
        FROM quota_stock_lifecycles
        WHERE dimension = 'stored_bytes' AND NOT tombstoned
    )
    SELECT count(*) INTO stock_mismatches
    FROM expected FULL OUTER JOIN actual USING (tenant_id, stock_key)
    WHERE expected.stock_key IS NULL
       OR actual.stock_key IS NULL
       OR expected.subject_user_id <> actual.subject_user_id
       OR expected.amount <> actual.amount;

    IF counter_mismatches <> 0 OR stock_mismatches <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Quota lifecycle postcondition failed: counters=' ||
                      counter_mismatches || ', stock=' || stock_mismatches ||
                      '. Keep workloads quiesced and reconcile the exact rows.';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("stock_key", sa.Text(), nullable=False),
        sa.Column(
            "subject_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column(
            "amount", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "tombstoned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "stock_key", name="pk_quota_stock_lifecycles"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_quota_stock_lifecycles_amount"),
        sa.CheckConstraint(
            "NOT tombstoned OR amount = 0",
            name="ck_quota_stock_lifecycles_tombstone_zero",
        ),
    )
    op.create_index(
        "ix_quota_stock_lifecycles_subject",
        TABLE,
        ["tenant_id", "subject_user_id", "dimension"],
    )
    # Mixed-version workers must not reintroduce order-sensitive stored-byte
    # adjustments after the stock table is authoritative.  NOT VALID keeps
    # historical receipts while enforcing the constraint for every new row.
    # ``op.execute(str)`` routes through SQLAlchemy's text bind parser. The
    # colons in these constant LIKE patterns are application data, not bind
    # markers, so execute the static PostgreSQL DDL through the DBAPI driver.
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE quota_usage_adjustments
        ADD CONSTRAINT ck_quota_adjustments_no_file_stock
        CHECK (
            adjustment_id NOT LIKE 'asset-upload:%:stored-bytes'
            AND adjustment_id NOT LIKE 'asset-delete:%:stored-bytes'
        ) NOT VALID
        """
    )
    op.execute(
        """
        INSERT INTO quota_stock_lifecycles (
            tenant_id,
            stock_key,
            subject_user_id,
            dimension,
            amount,
            tombstoned,
            created_at,
            updated_at
        )
        SELECT
            original.tenant_id,
            'file:' || original.id,
            original.owner_user_id,
            'stored_bytes',
            GREATEST(0, original.size_bytes),
            false,
            original.created_at,
            EXTRACT(EPOCH FROM clock_timestamp())
        FROM files AS original
        JOIN asset_records AS asset
          ON asset.tenant_id = original.tenant_id
         AND asset.server_file_id = original.id
        WHERE original.owner_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE quota_usage_counters
        SET used = 0,
            updated_at = EXTRACT(EPOCH FROM clock_timestamp())
        WHERE dimension = 'stored_bytes' AND period_start = 0
        """
    )
    op.execute(
        """
        INSERT INTO quota_usage_counters (
            tenant_id,
            subject_user_id,
            dimension,
            period_start,
            used,
            updated_at
        )
        SELECT
            tenant_id,
            owner_user_id,
            'stored_bytes',
            0,
            SUM(GREATEST(0, size_bytes)),
            EXTRACT(EPOCH FROM clock_timestamp())
        FROM files
        WHERE owner_user_id IS NOT NULL
        GROUP BY tenant_id, owner_user_id
        ON CONFLICT (tenant_id, subject_user_id, dimension, period_start)
        DO UPDATE SET
            used = EXCLUDED.used,
            updated_at = EXCLUDED.updated_at
        """
    )
    op.execute(_QUOTA_POSTCONDITION_SQL)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Quota stock lifecycle is irreversible: schema downgrade would "
        "discard resource-level idempotency and deletion tombstones, allowing "
        "late work to charge one file lifecycle again. Restore the matching "
        "pre-upgrade database backup instead."
    )
