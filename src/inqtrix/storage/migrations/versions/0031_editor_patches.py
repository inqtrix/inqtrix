"""Editor patches: persisted anchored-edit proposals.

Revision ID: 0031_editor_patches
Revises: 0030_agent_control

Creates ``editor_patches``, the persisted proposal-and-decision table for
anchored document edits (editor suggest/instruct and the workspace-agent
patch phase). A patch is a child of its editor DOCUMENT (``ON DELETE
CASCADE`` — deleting the document removes its patch history); the
optional ``run_id`` back-reference uses ``ON DELETE SET NULL`` so run
retention never destroys the applied-edit truth.

RLS follows the 0003/0030 precedent: GRANT to the app role, ENABLE +
FORCE ROW LEVEL SECURITY, fail-closed ``tenant_isolation`` policy.
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
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON

PATCH_SOURCES = ("suggest", "instruct", "agent")
PATCH_STATUSES = ("pending", "accepted", "rejected")

editor_patch_metadata = MetaData()


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in options)


editor_patches = Table(
    "editor_patches",
    editor_patch_metadata,
    Column("patch_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # Both FKs (document_id -> editor_documents(id) ON DELETE CASCADE,
    # run_id -> runs(run_id) ON DELETE SET NULL) are raw DDL in migration
    # 0031 — the parents live in other MetaData snapshots (module docstring).
    Column("document_id", Text, nullable=False),
    Column("run_id", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("edits", JSON, nullable=False, server_default=text("'[]'")),
    Column("summary", Text, nullable=False, server_default=text("''")),
    Column("warnings", JSON, nullable=False, server_default=text("'[]'")),
    Column("revision_before", Integer, nullable=False),
    Column("applied_revision", Integer, nullable=True),
    Column("applied_edit_ids", JSON, nullable=True),
    Column("note", Text, nullable=False, server_default=text("''")),
    Column("created_by_sub", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("decided_at", Float, nullable=True),
    CheckConstraint(
        f"source IN ({_values(PATCH_SOURCES)})", name="ck_editor_patches_source"
    ),
    CheckConstraint(
        f"status IN ({_values(PATCH_STATUSES)})", name="ck_editor_patches_status"
    ),
    Index(
        "ix_editor_patches_tenant_document",
        "tenant_id",
        "document_id",
        "created_at",
        "patch_id",
    ),
)
"""Proposed anchored document edits with their apply/reject lifecycle;
the ``pending -> accepted/rejected`` CAS happens in the store's mark
writes."""

revision = "0031_editor_patches"
down_revision = "0030_agent_control"
branch_labels = None
depends_on = None

_APP_ROLE = "inqtrix_app"

_TABLE = "editor_patches"


def upgrade() -> None:
    bind = op.get_bind()
    editor_patch_metadata.create_all(bind=bind)
    # editor_documents(id) and runs(run_id) live in other MetaData
    # snapshots, so the foreign keys are raw DDL here instead of ORM
    # declarations (the 0030 pattern).
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT fk_{_TABLE}_document "
        "FOREIGN KEY (document_id) REFERENCES editor_documents(id) "
        "ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT fk_{_TABLE}_run "
        "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}"
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} FOR ALL "
        "USING (tenant_id = (SELECT inqtrix_current_tenant_id())) "
        "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))"
    )


def downgrade() -> None:
    editor_patch_metadata.drop_all(bind=op.get_bind())
