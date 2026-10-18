"""Agent memory candidate queue.

Revision ID: 0033_agent_memory
Revises: 0032_editor_agent_source
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)

MEMORY_SCOPES = ("user", "workspace", "project", "agent")
MEMORY_CATEGORIES = ("preference", "project_fact", "strategy", "correction")
MEMORY_CANDIDATE_STATUSES = ("pending", "accepted", "rejected")
MEMORY_FEEDBACK_VALUES = ("positive", "negative", "neutral")

agent_memory_metadata = MetaData()


def _values(items: tuple[str, ...]) -> str:
    return ", ".join(f"'{item}'" for item in items)


agent_memory_candidates = Table(
    "agent_memory_candidates",
    agent_memory_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("sub", Text, nullable=False),
    Column("candidate_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("reason", Text, nullable=False, server_default=text("''")),
    Column("confidence", Float, nullable=False, server_default=text("0")),
    Column("source_run_id", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("memory_id", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id", "sub", "candidate_id", name="pk_agent_memory_candidates"
    ),
    CheckConstraint(
        f"scope IN ({_values(MEMORY_SCOPES)})",
        name="ck_agent_memory_candidates_scope",
    ),
    CheckConstraint(
        f"category IN ({_values(MEMORY_CATEGORIES)})",
        name="ck_agent_memory_candidates_category",
    ),
    CheckConstraint(
        f"status IN ({_values(MEMORY_CANDIDATE_STATUSES)})",
        name="ck_agent_memory_candidates_status",
    ),
    Index(
        "ix_agent_memory_candidates_owner_status",
        "tenant_id",
        "sub",
        "status",
        "created_at",
    ),
)
"""User-scoped review queue for long-term memory candidates."""


agent_feedback = Table(
    "agent_feedback",
    agent_memory_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("sub", Text, nullable=False),
    Column("feedback_id", Text, nullable=False),
    Column("run_id", Text, nullable=False),
    Column("memory_id", Text, nullable=False, server_default=text("''")),
    Column("feedback", Text, nullable=False),
    Column("reason", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id", "sub", "feedback_id", name="pk_agent_feedback"
    ),
    CheckConstraint(
        f"feedback IN ({_values(MEMORY_FEEDBACK_VALUES)})",
        name="ck_agent_feedback_feedback",
    ),
    Index(
        "ix_agent_feedback_owner_created",
        "tenant_id",
        "sub",
        "created_at",
    ),
    Index(
        "ix_agent_feedback_owner_run",
        "tenant_id",
        "sub",
        "run_id",
        "created_at",
    ),
)
"""User-scoped feedback history for workspace-agent runs."""

revision = "0033_agent_memory"
down_revision = "0032_editor_agent_source"
branch_labels = None
depends_on = None

_APP_ROLE = "inqtrix_app"
_TABLES = ("agent_memory_candidates",)


def upgrade() -> None:
    bind = op.get_bind()
    agent_memory_candidates.create(bind=bind, checkfirst=True)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON agent_memory_candidates TO {_APP_ROLE}"
    )
    op.execute("ALTER TABLE agent_memory_candidates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_memory_candidates FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE schemaname = current_schema()
                  AND tablename = 'agent_memory_candidates'
                  AND policyname = 'tenant_isolation'
            ) THEN
                CREATE POLICY tenant_isolation ON agent_memory_candidates
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
