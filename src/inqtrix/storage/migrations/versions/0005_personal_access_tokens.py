"""Personal-access-token schema with tenant RLS.

Revision ID: 0005_personal_access_tokens
Revises: 0004_auth_sessions

Creates the PAT table from its metadata snapshot and applies the
established security layering: DML grants for ``inqtrix_app`` and
ENABLE + FORCE row-level security with the fail-closed tenant policy
(InitPlan ``(SELECT ...)`` wrapper).
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import Column, Float, Index, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

pat_metadata = MetaData()

personal_access_tokens = Table(
    "personal_access_tokens",
    pat_metadata,
    Column("token_id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_issuer", Text, nullable=False),
    Column("owner_sub", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("secret_hmac", Text, nullable=False),
    Column("scopes", JSONB, nullable=False, server_default=text("'[]'")),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=True),
    Column("last_used_at", Float, nullable=True),
    Column("revoked_at", Float, nullable=True),
    Index("ix_pat_owner", "tenant_id", "owner_issuer", "owner_sub"),
)
"""Personal access tokens. The primary key serves the verification
lookup; ``ix_pat_owner`` serves listing and the disable cascade. No
index on ``secret_hmac`` — it is never queried, only compared."""

revision = "0005_personal_access_tokens"
down_revision = "0004_auth_sessions"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    pat_metadata.create_all(bind=bind)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON personal_access_tokens "
        f"TO {APP_ROLE}"
    )
    op.execute(
        "ALTER TABLE personal_access_tokens ENABLE ROW LEVEL SECURITY"
    )
    op.execute("ALTER TABLE personal_access_tokens FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON personal_access_tokens
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    pat_metadata.drop_all(bind=bind)
