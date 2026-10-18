"""Quota usage counters + limit overrides with tenant RLS.

Revision ID: 0007_quota
Revises: 0006_prompt_templates

Creates the two quota tables from their metadata snapshot and applies
the established security layering: DML grants for ``inqtrix_app`` and
ENABLE + FORCE row-level security with the fail-closed tenant policy
(InitPlan ``(SELECT ...)`` wrapper).
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)

quota_metadata = MetaData()

quota_usage_counters = Table(
    "quota_usage_counters",
    quota_metadata,
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("subject_sub", Text, nullable=False),
    Column("dimension", Text, nullable=False),
    Column("period_start", Float, nullable=False),
    Column("used", BigInteger, nullable=False, server_default=text("0")),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "subject_sub",
        "dimension",
        "period_start",
        name="pk_quota_usage_counters",
    ),
    Index("ix_quota_usage_subject", "tenant_id", "subject_sub"),
)

quota_limits = Table(
    "quota_limits",
    quota_metadata,
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("subject_sub", Text, nullable=False),
    Column("dimension", Text, nullable=False),
    Column("limit_value", BigInteger, nullable=False),
    Column("set_by_sub", Text, nullable=False),
    Column("set_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "subject_sub",
        "dimension",
        name="pk_quota_limits",
    ),
)

revision = "0007_quota"
down_revision = "0006_prompt_templates"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
_TABLES = ("quota_usage_counters", "quota_limits")


def upgrade() -> None:
    bind = op.get_bind()
    quota_metadata.create_all(bind=bind)

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
    bind = op.get_bind()
    quota_metadata.drop_all(bind=bind)
