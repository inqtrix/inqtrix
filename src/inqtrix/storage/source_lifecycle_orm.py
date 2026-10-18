"""Canonical source lifecycle shared by upload, indexing, and deletion."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Float, Index, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import UUID

source_lifecycle_metadata = MetaData()

source_lifecycles = Table(
    "source_lifecycles",
    source_lifecycle_metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("source_id", Text, primary_key=True),
    Column("owner_key", Text, primary_key=True),
    Column("workspace_key", Text, primary_key=True),
    Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("epoch", BigInteger, nullable=False, server_default=text("1")),
    Column("operation_id", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
    Index("ix_source_lifecycles_state", "tenant_id", "state", "updated_at"),
)
