"""Knowledge-session schema: saved Ask sessions (Wissensmodus).

Revision ID: 0021_knowledge_sessions
Revises: 0020_asset_parser_id

Adds the durable tier for knowledge Ask sessions (previously reducer-only /
ephemeral). ``knowledge_sessions`` holds titled conversations with Q&A items as
a JSON body, scoped per ``(tenant_id, created_by_sub, workspace_id)``. The
folder table intentionally arrives in migration 0026. Same security layering
as 0013/0014/0015: DML grant for ``inqtrix_app`` + ENABLE/FORCE row-level
security with the fail-closed tenant policy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_knowledge_sessions"
down_revision = "0020_asset_parser_id"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
_TABLES = ("knowledge_sessions",)


# Frozen revision-0021 schema. Folder groups and ``group_id`` arrive in 0026;
# importing the live two-table ORM here would create that later schema early.
_knowledge_sessions_metadata = sa.MetaData()

_knowledge_sessions = sa.Table(
    "knowledge_sessions",
    _knowledge_sessions_metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("created_by_sub", sa.Text, nullable=True),
    sa.Column("workspace_id", sa.Text, nullable=True),
    sa.Column("title", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column(
        "items_json", sa.Text, nullable=False, server_default=sa.text("'[]'")
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Index(
        "ix_knowledge_sessions_owner_updated",
        "tenant_id",
        "created_by_sub",
        "workspace_id",
        "updated_at",
        "id",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    _knowledge_sessions_metadata.create_all(bind=bind)

    for table in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
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
    _knowledge_sessions_metadata.drop_all(bind=bind)
