"""Auth-session schema: auth_sessions and auth_flows with tenant RLS.

Revision ID: 0004_auth_sessions
Revises: 0003_runs_durability

Creates the OIDC BFF's server-side state tables from the auth
metadata and applies the established security layering: DML grants
for ``inqtrix_app`` and ENABLE + FORCE row-level security with the
fail-closed tenant policy (InitPlan ``(SELECT ...)`` wrapper).
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

auth_metadata = MetaData()

auth_sessions = Table(
    "auth_sessions",
    auth_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("sub", Text, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("email", Text, nullable=True),
    Column("display_name", Text, nullable=True),
    Column("groups", JSONB, nullable=False, server_default=text("'[]'")),
    Column("csrf_random", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)
"""Authenticated browser sessions; the cookie carries only the opaque
``id``. No tokens are stored — the BFF discards them after login."""

auth_flows = Table(
    "auth_flows",
    auth_metadata,
    Column("state", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("code_verifier", Text, nullable=False),
    Column("nonce", Text, nullable=False),
    Column("next_path", Text, nullable=False, server_default=text("'/'")),
    Column("expires_at", Float, nullable=False),
    Column(
        "consumed",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
)
"""In-flight login transactions keyed by the OAuth ``state``;
consumption is a guarded one-time flip so replayed callbacks fail
even across API replicas."""

revision = "0004_auth_sessions"
down_revision = "0003_runs_durability"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    auth_metadata.create_all(bind=bind)

    for table in ("auth_sessions", "auth_flows"):
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
    auth_metadata.drop_all(bind=bind)
