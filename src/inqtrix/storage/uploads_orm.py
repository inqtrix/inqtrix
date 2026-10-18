"""SQLAlchemy Core schema for durable original-file upload operations."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

upload_metadata = MetaData()

upload_operations = Table(
    "upload_operations",
    upload_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # The migration enforces the tenant-scoped cross-metadata FK
    # ``(tenant_id, asset_id)`` to asset_records. This module owns a separate
    # SQLAlchemy MetaData collection, so declaring that external FK here would
    # be unresolved during SQL compilation.
    Column("asset_id", Text, nullable=False),
    Column("file_id", Text, nullable=False),
    Column("file_manifest", JSON, nullable=False),
    Column("binding", JSON, nullable=False),
    Column("status", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("workspace_id", Text, nullable=True),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("error", JSON, nullable=True),
    Column("claimed_by", Text, nullable=True),
    Column("attempt", Integer, nullable=False, server_default=text("1")),
    Column("event_seq", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "operation_id",
        name="uq_upload_operations_tenant_operation",
    ),
    Index(
        "ix_upload_operations_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
    ),
    Index("ix_upload_operations_status", "tenant_id", "status", "updated_at"),
    Index(
        "uq_upload_operations_active_asset",
        "tenant_id",
        "asset_id",
        text("COALESCE(created_by_user_id::text, '')"),
        text("COALESCE(workspace_id, '')"),
        unique=True,
        postgresql_where=text(
            "status IN ('running', 'queued', 'awaiting_bytes', 'upload_failed')"
        ),
    ),
)

upload_operation_events = Table(
    "upload_operation_events",
    upload_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("type", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("data", JSON, nullable=False, server_default=text("'{}'")),
    ForeignKeyConstraint(
        ["tenant_id", "operation_id"],
        ["upload_operations.tenant_id", "upload_operations.operation_id"],
        name="fk_upload_operation_events_tenant_operation",
        ondelete="CASCADE",
    ),
)

upload_operation_outbox = Table(
    "upload_operation_outbox",
    upload_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("available_at", Float, nullable=False),
    Column("dispatch_count", Integer, nullable=False, server_default=text("0")),
    Column("last_dispatched_at", Float, nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "operation_id"],
        ["upload_operations.tenant_id", "upload_operations.operation_id"],
        name="fk_upload_operation_outbox_tenant_operation",
        ondelete="CASCADE",
    ),
    Index("ix_upload_operation_outbox_due", "tenant_id", "available_at"),
)
