"""Local email/password credential schema with tenant RLS.

Revision ID: 0008_local_credentials
Revises: 0007_quota

Creates the local-credentials table from its metadata snapshot (incl.
the functional unique index on lower(email)) and applies the established
security layering: DML grants for ``inqtrix_app`` and ENABLE + FORCE
row-level security with the fail-closed tenant policy (InitPlan
``(SELECT ...)`` wrapper). Backs ``INQTRIX_AUTH_MODE=local``.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import Column, Float, Index, MetaData, Table, Text, func, text

credentials_metadata = MetaData()

local_credentials = Table(
    "local_credentials",
    credentials_metadata,
    Column("subject", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("email", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("disabled_at", Float, nullable=True),
)
"""Local email/password accounts. Primary key ``subject`` serves login
re-lookup and admin actions; the functional unique index below enforces
one account per case-insensitive email within a tenant."""

# One account per (tenant, email) — case-insensitive so Foo@x and foo@x
# cannot both register. Defined after the table so it can reference the
# real column; create_all emits CREATE UNIQUE INDEX ... (tenant_id, lower(email)).
Index(
    "uq_local_credentials_email",
    local_credentials.c.tenant_id,
    func.lower(local_credentials.c.email),
    unique=True,
)

revision = "0008_local_credentials"
down_revision = "0007_quota"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    credentials_metadata.create_all(bind=bind)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON local_credentials "
        f"TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE local_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE local_credentials FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON local_credentials
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    credentials_metadata.drop_all(bind=bind)
