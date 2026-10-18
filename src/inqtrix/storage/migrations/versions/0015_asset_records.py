"""File-asset-record schema: sections, groups, asset records.

Revision ID: 0015_asset_records
Revises: 0014_editor_persistence

Third slice of the project-persistence tier. Creates the asset
tables from their metadata snapshot and applies the established security
layering: DML grants for ``inqtrix_app`` + ENABLE/FORCE row-level security
with the fail-closed tenant policy, identical to ``0013``/``0014``.

CHECK constraints pin the section kind, asset origin, and parse status to
the frontend unions (``FileSectionKind`` / ``FileAssetOrigin`` /
``FileParseStatus``) so an out-of-domain write fails loudly at the database
boundary.
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
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)

asset_metadata = MetaData()

asset_sections = Table(
    "asset_sections",
    asset_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("kind", Text, nullable=False, server_default=text("'custom'")),
    Column("title", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_asset_sections_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""Top-level file-library sections (the library's outermost grouping)."""

asset_groups = Table(
    "asset_groups",
    asset_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column(
        "section_id",
        Text,
        ForeignKey("asset_sections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("title", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_asset_groups_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""Mid-level groups within a section. Cascade-deleted with their section."""

asset_records = Table(
    "asset_records",
    asset_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column(
        "section_id",
        Text,
        ForeignKey("asset_sections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "group_id",
        Text,
        ForeignKey("asset_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("label", Text, nullable=False, server_default=text("''")),
    Column("file_name", Text, nullable=False, server_default=text("''")),
    Column("mime_type", Text, nullable=False, server_default=text("''")),
    Column("origin", Text, nullable=False, server_default=text("'library'")),
    Column("page_count", Integer, nullable=True),
    Column("parse_status", Text, nullable=False, server_default=text("'parsed'")),
    Column("parse_warning", Text, nullable=True),
    Column("text_truncated", Integer, nullable=False, server_default=text("0")),
    Column("size_bytes", BigInteger, nullable=False, server_default=text("0")),
    # The /v1/files blob reference (the binary lives in the object store);
    # null/absent = a local-only asset that was never uploaded.
    Column("server_file_id", Text, nullable=True),
    # Which parser produced extracted_text ("markitdown" server-side,
    # "client" browser-side); null = unknown (legacy/local-only rows).
    Column("parser_id", Text, nullable=True),
    # The heavy extracted text — excluded from the list query, loaded on open.
    Column("extracted_text", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_asset_records_owner_created",
        "tenant_id",
        "created_by_sub",
        "created_at",
        "id",
    ),
)
"""One file-asset record: the library metadata + extracted text wrapping a
files-registry blob (``server_file_id``). ``extracted_text`` is the heavy
body (lazy). Cascades with its section; orphans to ungrouped on group
delete. ``text_truncated`` is an int flag (0/1) — SQLAlchemy Core stays
dialect-portable without a Boolean column here."""

revision = "0015_asset_records"
down_revision = "0014_editor_persistence"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Parents before children: asset_sections <- asset_groups <- asset_records.
_TABLES = ("asset_sections", "asset_groups", "asset_records")


def upgrade() -> None:
    bind = op.get_bind()
    asset_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE asset_sections ADD CONSTRAINT ck_asset_sections_kind "
        "CHECK (kind IN ('temporary', 'custom'))"
    )
    op.execute(
        "ALTER TABLE asset_records ADD CONSTRAINT ck_asset_records_origin "
        "CHECK (origin IN ('chat', 'editor', 'library'))"
    )
    op.execute(
        "ALTER TABLE asset_records ADD CONSTRAINT ck_asset_records_parse_status "
        "CHECK (parse_status IN ('parsed', 'partial', 'unsupported', 'error'))"
    )
    for table in _TABLES:
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
    asset_metadata.drop_all(bind=bind)
