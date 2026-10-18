"""Persist provider/model contextualization circuits across workers.

Revision ID: 0070_contextualization_circuits
Revises: 0069_knowledge_source_scope

One tenant/provider/model row coordinates the open/cooldown/half-open
lifecycle for ingestion-time contextualization.  A leased half-open token
allows exactly one recovery probe across API and worker replicas.  The table
contains operational metadata only; no prompts, source text, credentials or
provider responses are persisted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070_contextualization_circuits"
down_revision = "0069_knowledge_source_scope"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
TABLE = "contextualization_provider_circuits"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("provider_key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'closed'"),
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cooldown_until",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("probe_token", sa.Text(), nullable=True),
        sa.Column("probe_lease_until", sa.Float(), nullable=True),
        sa.Column("last_error_type", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "provider_key",
            "model",
            name="pk_contextualization_provider_circuits",
        ),
        sa.CheckConstraint(
            "state IN ('closed', 'open', 'half_open')",
            name="ck_contextualization_provider_circuits_state",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_contextualization_provider_circuits_failures",
        ),
        sa.CheckConstraint(
            "(state = 'half_open' AND probe_token IS NOT NULL "
            "AND probe_lease_until IS NOT NULL) OR "
            "(state <> 'half_open' AND probe_token IS NULL "
            "AND probe_lease_until IS NULL)",
            name="ck_contextualization_provider_circuits_probe",
        ),
    )
    op.create_index(
        "ix_contextualization_circuits_state_cooldown",
        TABLE,
        ["tenant_id", "state", "cooldown_until"],
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}"
    )
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
        "The durable provider circuit is part of the indexing safety "
        "contract and cannot be removed in place. Restore the matching "
        "pre-upgrade backup instead."
    )
