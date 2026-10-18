"""Usage ledger: per-call consumption rows.

Revision ID: 0073_llm_usage
Revises: 0072_audit_read_model

``llm_usage`` is the durable per-user / per-model / per-feature
consumption history. Quota stays the enforcement authority (monthly
counters); the ledger stores the per-provider-call rows a later usage
UI only has to read. Written exclusively by the buffered recorder fed
from the provider wrappers — the same chokepoint that feeds spans and
metrics.

Rows are immutable: the application role holds INSERT/SELECT only
(the ``audit_log`` WORM contract — the shared ``WORM_TENANT_TABLES``
inventory drives BOTH the migration postconditions and the runtime
readiness probe), and ``llm_usage_prune(cutoff)`` is the ONE sanctioned
deletion door (retention default 24 months). Costs are deliberately NOT
stored — prices change; they derive at read time from the model-card
catalog.

The primary key uses an IDENTITY column on purpose: its sequence is
owned by the column and needs no separate grant, so — unlike the
``audit_log_id_seq`` era — there is no sequence ACL to revoke from
PUBLIC and none to add to ``RUNTIME_REQUIRED_SEQUENCES``. Table-level
INSERT is sufficient.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073_llm_usage"
down_revision = "0072_audit_read_model"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
TABLE = "llm_usage"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id", sa.BigInteger(), sa.Identity(always=False), primary_key=True
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "input_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "request_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "duration_ms",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "operation IN ('chat', 'text_completion', 'embeddings', "
            "'web_search')",
            name="ck_llm_usage_operation",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'timeout', 'cancelled', 'error')",
            name="ck_llm_usage_outcome",
        ),
    )
    op.create_index(
        "ix_llm_usage_tenant_user_created",
        TABLE,
        ["tenant_id", "user_id", "created_at"],
    )
    op.create_index(
        "ix_llm_usage_tenant_model_created",
        TABLE,
        ["tenant_id", "model", "created_at"],
    )
    # Retention prune scans by age alone.
    op.create_index("ix_llm_usage_created", TABLE, ["created_at"])
    # WORM contract: rows are immutable bookings; the retention function
    # below is the one deletion door (audit_log precedent).
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {APP_ROLE}")
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
    op.execute(
        """
        CREATE FUNCTION llm_usage_prune(cutoff double precision)
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            pruned bigint;
        BEGIN
            DELETE FROM llm_usage WHERE created_at < cutoff;
            GET DIAGNOSTICS pruned = ROW_COUNT;
            RETURN pruned;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION llm_usage_prune(double precision) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION llm_usage_prune(double precision) "
        "TO inqtrix_app"
    )


def downgrade() -> None:
    raise RuntimeError(
        "This migration is irreversible: llm_usage is the durable "
        "consumption ledger. Restore the matching pre-upgrade backup "
        "instead."
    )
