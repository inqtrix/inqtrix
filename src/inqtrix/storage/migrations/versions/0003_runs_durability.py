"""Durable run schema: runs and run_events with tenant RLS.

Revision ID: 0003_runs_durability
Revises: 0002_content_files

Creates the ``runs`` and ``run_events`` tables from the runs metadata
and applies the same security layering as revisions 0001/0002: DML
grants for the ``inqtrix_app`` role and ENABLE + FORCE row-level
security with the fail-closed tenant policy (the ``(SELECT ...)``
wrapper keeps the helper call an InitPlan — once per query, not per
row). The status CHECK constraint mirrors
:class:`~inqtrix.server.runs.RunStatus`; the lifecycle ordering itself
lives only in that application enum.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_runs_durability"
down_revision = "0002_content_files"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"

_STATUS_VALUES = (
    "'queued', 'running', 'completed', 'failed', 'cancelled', 'expired'"
)


# Frozen revision-0003 schema. Later revisions add the agent-tree columns and
# indexes before 0045 replaces ``created_by_sub`` with a canonical user UUID.
_runs_metadata = sa.MetaData()

_runs = sa.Table(
    "runs",
    _runs_metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column(
        "status", sa.Text, nullable=False, server_default=sa.text("'queued'")
    ),
    sa.Column(
        "mode", sa.Text, nullable=False, server_default=sa.text("'research'")
    ),
    sa.Column("question", sa.Text, nullable=False),
    sa.Column(
        "stack_name", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("workspace_id", sa.Text, nullable=True),
    sa.Column("created_by_sub", sa.Text, nullable=True),
    sa.Column("created_by_tenant_id", sa.Text, nullable=True),
    sa.Column(
        "agent_overrides",
        postgresql.JSON,
        nullable=False,
        server_default=sa.text("'{}'"),
    ),
    sa.Column("request_payload", postgresql.JSON, nullable=True),
    sa.Column(
        "snapshot",
        postgresql.JSON,
        nullable=False,
        server_default=sa.text("'{}'"),
    ),
    sa.Column("result", postgresql.JSON, nullable=True),
    sa.Column("error", postgresql.JSON, nullable=True),
    sa.Column(
        "cancel_requested",
        sa.Boolean,
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column("claimed_by", sa.Text, nullable=True),
    sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "event_seq", sa.Integer, nullable=False, server_default=sa.text("0")
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("started_at", sa.Float, nullable=True),
    sa.Column("finished_at", sa.Float, nullable=True),
    sa.Index("ix_runs_tenant_created", "tenant_id", "created_at"),
    sa.Index("ix_runs_tenant_status", "tenant_id", "status"),
)

_run_events = sa.Table(
    "run_events",
    _runs_metadata,
    sa.Column(
        "run_id",
        sa.Text,
        sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column(
        "data",
        postgresql.JSON,
        nullable=False,
        server_default=sa.text("'{}'"),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    _runs_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_status "
        f"CHECK (status IN ({_STATUS_VALUES}))"
    )
    for table in ("runs", "run_events"):
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
    _runs_metadata.drop_all(bind=bind)
