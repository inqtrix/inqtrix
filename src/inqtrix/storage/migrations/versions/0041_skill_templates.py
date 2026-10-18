"""Skill schema with tenant RLS.

Revision ID: 0041_skill_templates
Revises: 0040_kernel_control_kinds

Creates the skill table from its metadata snapshot and applies the
established security layering: DML grants for ``inqtrix_app`` and
ENABLE + FORCE row-level security with the fail-closed tenant policy
(InitPlan ``(SELECT ...)`` wrapper) — the prompt-template recipe
(0006) verbatim.
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

skill_metadata = MetaData()

skill_templates = Table(
    "skill_templates",
    skill_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_sub", Text, nullable=True),
    Column("label", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("when_to_use", Text, nullable=False, server_default=text("''")),
    Column("instructions_markdown", Text, nullable=False),
    Column(
        "clarification_points",
        JSONB,
        nullable=False,
        server_default=text("'[]'"),
    ),
    Column("deliverable", Text, nullable=False, server_default=text("''")),
    Column(
        "allowed_tools", JSONB, nullable=False, server_default=text("'[]'")
    ),
    Column(
        "requires_plan", Text, nullable=False, server_default=text("'auto'")
    ),
    Column(
        "invocation",
        Text,
        nullable=False,
        server_default=text("'user_only'"),
    ),
    Column("argument_hint", Text, nullable=False, server_default=text("''")),
    Column("model_tier", Text, nullable=False, server_default=text("''")),
    Column("effort", Text, nullable=False, server_default=text("''")),
    Column(
        "include_in_autocomplete",
        Boolean,
        nullable=False,
        server_default=text("true"),
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "deliverable IN ('', 'chat', 'canvas', 'email', 'talking_points')",
        name="ck_skill_templates_deliverable",
    ),
    CheckConstraint(
        "requires_plan IN ('always', 'auto', 'never')",
        name="ck_skill_templates_requires_plan",
    ),
    CheckConstraint(
        "invocation IN ('user_only', 'model_allowed')",
        name="ck_skill_templates_invocation",
    ),
    Index("ix_skill_templates_owner", "tenant_id", "owner_sub"),
)

revision = "0041_skill_templates"
down_revision = "0040_kernel_control_kinds"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    skill_metadata.create_all(bind=bind)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON skill_templates "
        f"TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE skill_templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_templates FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON skill_templates
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    skill_metadata.drop_all(bind=bind)
