"""Runtime SQLAlchemy tables for secure editor guest links."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

editor_guest_link_metadata = MetaData()

editor_document_share_links = Table(
    "editor_document_share_links",
    editor_guest_link_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("label", Text, nullable=False),
    Column("permission", Text, nullable=False),
    Column("token_digest", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("revoked_at", Float, nullable=True),
    Column(
        "successful_open_count",
        BigInteger,
        nullable=False,
        server_default=text("0"),
    ),
    Column(
        "session_count",
        BigInteger,
        nullable=False,
        server_default=text("0"),
    ),
    Column("last_accessed_at", Float, nullable=True),
    Column("last_command_id", UUID(as_uuid=True), nullable=False),
    Column("last_command_payload_hash", Text, nullable=False),
    Column("last_command_kind", Text, nullable=False),
    UniqueConstraint(
        "tenant_id",
        "id",
        name="uq_editor_document_share_links_tenant_id",
    ),
    UniqueConstraint(
        "tenant_id",
        "token_digest",
        name="uq_editor_document_share_links_token_digest",
    ),
    UniqueConstraint(
        "tenant_id",
        "last_command_id",
        name="uq_editor_document_share_links_command",
    ),
    CheckConstraint(
        "generation >= 1 AND revision >= 1",
        name="ck_editor_document_share_links_position",
    ),
    CheckConstraint(
        "permission IN ('view', 'comment', 'suggest', 'edit')",
        name="ck_editor_document_share_links_permission",
    ),
    CheckConstraint(
        "length(token_digest) = 64 "
        "AND length(btrim(password_hash)) > 0 "
        "AND length(btrim(label)) BETWEEN 2 AND 24",
        name="ck_editor_document_share_links_secrets",
    ),
    CheckConstraint(
        "expires_at > created_at AND updated_at >= created_at "
        "AND (revoked_at IS NULL OR revoked_at >= created_at)",
        name="ck_editor_document_share_links_timestamps",
    ),
    CheckConstraint(
        "successful_open_count >= 0 AND session_count >= 0",
        name="ck_editor_document_share_links_stats",
    ),
    CheckConstraint(
        "length(last_command_payload_hash) = 64",
        name="ck_editor_document_share_links_command_hash",
    ),
    CheckConstraint(
        "last_command_kind IN "
        "('create', 'update', 'revoke', 'rotate_password', 'rotate_token')",
        name="ck_editor_document_share_links_command_kind",
    ),
    Index(
        "ix_editor_document_share_links_document",
        "tenant_id",
        "document_id",
        "created_at",
        "id",
    ),
    Index(
        "ix_editor_document_share_links_expiry",
        "tenant_id",
        "expires_at",
        postgresql_where=text("revoked_at IS NULL"),
    ),
)

editor_document_guest_identities = Table(
    "editor_document_guest_identities",
    editor_guest_link_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("link_id", UUID(as_uuid=True), nullable=False),
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("session_token_digest", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("last_seen_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked_at", Float, nullable=True),
    Column("open_count", BigInteger, nullable=False, server_default=text("1")),
    Column(
        "last_read_revision",
        BigInteger,
        nullable=False,
        server_default=text("0"),
    ),
    UniqueConstraint(
        "tenant_id",
        "id",
        name="uq_editor_document_guest_identities_tenant_id",
    ),
    UniqueConstraint(
        "tenant_id",
        "session_token_digest",
        name="uq_editor_document_guest_identities_session",
    ),
    CheckConstraint(
        "generation >= 1 AND open_count >= 1 AND last_read_revision >= 0",
        name="ck_editor_document_guest_identities_position",
    ),
    CheckConstraint(
        "display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 80",
        name="ck_editor_document_guest_identities_name",
    ),
    CheckConstraint(
        "length(session_token_digest) = 64",
        name="ck_editor_document_guest_identities_token",
    ),
    CheckConstraint(
        "last_seen_at >= created_at AND expires_at > created_at "
        "AND (revoked_at IS NULL OR revoked_at >= created_at)",
        name="ck_editor_document_guest_identities_timestamps",
    ),
    Index(
        "ix_editor_document_guest_identities_link",
        "tenant_id",
        "link_id",
        "last_seen_at",
    ),
    Index(
        "ix_editor_document_guest_identities_document",
        "tenant_id",
        "document_id",
        "last_seen_at",
    ),
)
