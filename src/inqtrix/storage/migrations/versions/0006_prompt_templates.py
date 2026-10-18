"""Prompt-template schema with tenant RLS.

Revision ID: 0006_prompt_templates
Revises: 0005_personal_access_tokens

Creates the prompt-template table from its metadata snapshot and
applies the established security layering: DML grants for
``inqtrix_app`` and ENABLE + FORCE row-level security with the
fail-closed tenant policy (InitPlan ``(SELECT ...)`` wrapper).
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

prompt_template_metadata = MetaData()

prompt_templates = Table(
    "prompt_templates",
    prompt_template_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_sub", Text, nullable=True),
    Column("title", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("category", Text, nullable=True),
    Column("content_markdown", Text, nullable=False),
    Column("visibility", JSONB, nullable=False, server_default=text("'{}'")),
    Column(
        "include_in_autocomplete",
        Boolean,
        nullable=False,
        server_default=text("true"),
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "category IS NULL OR category IN "
        "('instruction', 'function', 'context')",
        name="ck_prompt_templates_category",
    ),
    Index("ix_prompt_templates_owner", "tenant_id", "owner_sub"),
)

revision = "0006_prompt_templates"
down_revision = "0005_personal_access_tokens"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    prompt_template_metadata.create_all(bind=bind)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON prompt_templates "
        f"TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE prompt_templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prompt_templates FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON prompt_templates
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    prompt_template_metadata.drop_all(bind=bind)
