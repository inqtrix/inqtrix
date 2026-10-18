"""SQLAlchemy Core definition for user-scoped invalidation events."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

user_event_metadata = MetaData()

user_events = Table(
    "user_events",
    user_event_metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "target_user_id",
        UUID(as_uuid=True),
        nullable=False,
    ),
    Column("scope", Text, nullable=False),
    Column("resource_type", Text, nullable=True),
    Column("resource_id", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Index(
        "ix_user_events_tenant_target_id",
        "tenant_id",
        "target_user_id",
        "id",
    ),
)
