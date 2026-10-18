"""SQLAlchemy Core definitions of the agent-sessions schema.

Agent sessions use the same structural pattern as
:mod:`inqtrix.storage.knowledge_sessions_orm`: private per-user sessions
with a heavy ``items_json`` body (the client-side desk snapshot), optional
grouping, and keyset-ready owner indexes. Created by migration 0030.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

agent_sessions_metadata = MetaData()

agent_session_groups = Table(
    "agent_session_groups",
    agent_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_agent_session_groups_owner_created",
        "tenant_id",
        "created_by_user_id",
        "workspace_id",
        "created_at",
        "id",
    ),
)
"""User-defined folders for agent sessions in the desk rail."""

agent_sessions = Table(
    "agent_sessions",
    agent_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("title_revision", Integer, nullable=False, server_default=text("0")),
    Column("title_source", Text, nullable=False, server_default=text("'manual'")),
    CheckConstraint("title_revision >= 0", name="ck_agent_sessions_title_revision"),
    CheckConstraint("title_source IN ('initial', 'generated', 'manual')", name="ck_agent_sessions_title_source"),
    Column(
        "group_id",
        Text,
        ForeignKey("agent_session_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # The client-side desk snapshot (timeline, follow-ups). Heavy body:
    # list queries exclude it (load-on-open), exactly like knowledge
    # sessions. The durable artifact CONTENT lives in run_artifacts, not
    # here — items_json only mirrors what the desk rendered.
    Column("items_json", Text, nullable=False, server_default=text("'[]'")),
    Column("lifecycle_status", Text, nullable=False, server_default=text("'active'")),
    Column("deletion_operation_id", Text, nullable=True),
    Column("deletion_stage", Text, nullable=True),
    Column("deletion_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "lifecycle_status IN ('active', 'deleting', 'delete_failed')",
        name="ck_agent_sessions_lifecycle_status",
    ),
    Index(
        "ix_agent_sessions_owner_updated",
        "tenant_id",
        "created_by_user_id",
        "workspace_id",
        "updated_at",
        "id",
    ),
    Index(
        "ix_agent_sessions_deletion_operation",
        "tenant_id",
        "deletion_operation_id",
    ),
)
"""Saved agent-desk sessions; ``runs.session_id`` and
``run_artifacts.session_id`` reference these ids WITHOUT a foreign key
(sessions may be deleted while their runs age out on their own)."""
