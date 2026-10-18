"""Content schema: files registry table with tenant RLS.

Revision ID: 0002_content_files
Revises: 0001_identity_schema

Creates the ``files`` table from the content metadata and applies the
same security layering as revision 0001: DML grants for the
``inqtrix_app`` role and ENABLE + FORCE row-level security with the
fail-closed tenant policy (the ``(SELECT ...)`` wrapper keeps the
helper call an InitPlan — once per query, not per row).
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
    Table,
    Text,
    text,
)

content_metadata = MetaData()

files = Table(
    "files",
    content_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("owner_sub", Text, nullable=False),
    Column("workspace_id", Text, nullable=True),
    Column("file_name", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("object_key", Text, nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Index("ix_files_tenant_owner", "tenant_id", "owner_sub"),
    Index("ix_files_tenant_created", "tenant_id", "created_at"),
)
"""Uploaded-file metadata; the bytes live in the object store under
``object_key``. Row-level security is layered on by migration 0002."""

revision = "0002_content_files"
down_revision = "0001_identity_schema"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    content_metadata.create_all(bind=bind)

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON files TO {APP_ROLE}")
    op.execute("ALTER TABLE files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE files FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON files
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    content_metadata.drop_all(bind=bind)
