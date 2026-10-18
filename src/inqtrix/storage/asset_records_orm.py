"""SQLAlchemy Core definitions of the file-asset-record schema.

The asset-record layer of the project-persistence tier: the local file
library's METADATA (sections, groups, asset records + their extracted
text), scoped per ``(tenant_id, created_by_user_id, workspace_id)`` like the
chat/editor entities. The original binaries already live in the object
store via the files registry (``content_orm.files``); an asset record
references that blob by ``server_file_id`` and adds the library
organization + the extracted text the local app uses.

Two-level hierarchy mirroring the local ProjectState:
``asset_sections`` (top) -> ``asset_groups`` (``section_id`` FK) ->
``asset_records`` (``section_id`` FK + nullable ``group_id`` FK). Deleting
a section cascades its groups and assets; deleting a group orphans its
assets to ungrouped (``group_id`` SET NULL) — exactly the reducer's
cascade rules.

Type decisions match the chat/editor ORMs: client-supplied prefixed ids
(``fsec_``/``fg_``/``fa_``) as the PK, unix-seconds ``Float`` timestamps,
``tenant_id`` for RLS, keyset index with the id tiebreaker. The heavy
``extracted_text`` is excluded from the asset LIST query (loaded on open),
the documents-body pattern. CHECK constraints pin the section kind / asset
origin / parse status to the frontend unions.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

asset_metadata = MetaData()

asset_sections = Table(
    "asset_sections",
    asset_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("kind", Text, nullable=False, server_default=text("'custom'")),
    Column("title", Text, nullable=False),
    # Server-owned identity. NULL is retained only for rows created before
    # the semantic-role contract; ordinary user sections use ``custom``.
    Column("semantic_role", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "semantic_role IS NULL OR semantic_role IN "
        "('temporary', 'library', 'project_sources', 'custom')",
        name="ck_asset_sections_semantic_role",
    ),
    Index(
        "ix_asset_sections_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
        "id",
    ),
    Index(
        "uq_asset_sections_prepared_role_scope",
        "tenant_id",
        "created_by_user_id",
        "workspace_id",
        "semantic_role",
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=text(
            "semantic_role IN ('temporary', 'library', 'project_sources')"
        ),
    ),
)
"""Top-level file-library sections (the library's outermost grouping)."""

asset_groups = Table(
    "asset_groups",
    asset_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
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
        "created_by_user_id",
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
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
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
    # Canonical server parse used for knowledge revision reservation.  It is
    # intentionally separate from the client-editable presentation body.
    Column("prepared_text", Text, nullable=True),
    Column("prepared_parser_id", Text, nullable=True),
    # Revision of the parser that produced prepared_text. NULL is "unknown"
    # (records prepared before it was recorded); it is never back-filled
    # from whatever parser happens to be installed later.
    Column("prepared_parser_revision", Text, nullable=True),
    Column("prepared_content_hash", Text, nullable=True),
    Column("prepared_file_sha256", Text, nullable=True),
    Column("prepared_page_texts", JSON, nullable=True),
    Column("prepared_at", Float, nullable=True),
    # Server-owned destructive lifecycle.  The client may display these
    # fields but never clears them through the normal asset PUT path.
    Column(
        "lifecycle_status",
        Text,
        nullable=False,
        server_default=text("'active'"),
    ),
    Column("deletion_operation_id", Text, nullable=True),
    Column("deletion_stage", Text, nullable=True),
    Column("deletion_error", Text, nullable=True),
    Column(
        "upload_status",
        Text,
        nullable=False,
        server_default=text("'ready'"),
    ),
    Column("upload_error", Text, nullable=True),
    Column("upload_operation_id", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint(
        "tenant_id", "id", name="uq_asset_records_tenant_id"
    ),
    Index(
        "ix_asset_records_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
        "id",
    ),
)
"""One file-asset record: the library metadata + extracted text wrapping a
files-registry blob (``server_file_id``). ``extracted_text`` is the heavy
body (lazy). Cascades with its section; orphans to ungrouped on group
delete. ``text_truncated`` is an int flag (0/1) — SQLAlchemy Core stays
dialect-portable without a Boolean column here."""
