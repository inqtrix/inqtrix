"""SQLAlchemy Core schema for durable aggregate deletion operations."""

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

deletion_metadata = MetaData()

deletion_operations = Table(
    "deletion_operations",
    deletion_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("target_kind", Text, nullable=False),
    Column("target_id", Text, nullable=False),
    Column("manifest", JSON, nullable=False, server_default=text("'[]'")),
    Column("context", JSON, nullable=False, server_default=text("'{}'")),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("stage", Text, nullable=False, server_default=text("'queued'")),
    Column("completed_items", Integer, nullable=False, server_default=text("0")),
    Column("total_items", Integer, nullable=False, server_default=text("0")),
    Column("workspace_id", Text, nullable=True),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("error", JSON, nullable=True),
    Column("claimed_by", Text, nullable=True),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("event_seq", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "operation_id",
        name="uq_deletion_operations_tenant_operation",
    ),
    Index(
        "ix_deletion_operations_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
    ),
    Index("ix_deletion_operations_status", "tenant_id", "status"),
    Index(
        "uq_deletion_operations_active_target",
        "tenant_id",
        "target_kind",
        "target_id",
        text("COALESCE(created_by_user_id::text, '')"),
        text("COALESCE(workspace_id, '')"),
        unique=True,
        postgresql_where=text("status IN ('queued', 'running')"),
    ),
)

deletion_operation_events = Table(
    "deletion_operation_events",
    deletion_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("type", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("data", JSON, nullable=False, server_default=text("'{}'")),
    ForeignKeyConstraint(
        ["tenant_id", "operation_id"],
        ["deletion_operations.tenant_id", "deletion_operations.operation_id"],
        name="fk_deletion_operation_events_tenant_operation",
        ondelete="CASCADE",
    ),
)

deletion_operation_assets = Table(
    "deletion_operation_assets",
    deletion_metadata,
    Column("operation_id", Text, primary_key=True),
    Column("asset_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "operation_id"],
        ["deletion_operations.tenant_id", "deletion_operations.operation_id"],
        name="fk_deletion_operation_assets_tenant_operation",
        ondelete="CASCADE",
    ),
    Index(
        "ix_deletion_operation_assets_lookup",
        "tenant_id",
        "asset_id",
    ),
)
