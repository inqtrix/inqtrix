"""SQLAlchemy Core definition of the knowledge-session schema (Wissensmodus).

The durable tier behind the Ask view's saved sessions — the knowledge
counterpart of chat threads. Knowledge Q&A was ephemeral (reducer-only); a
session persists a titled conversation so it survives reload + crosses devices,
scoped per ``(tenant_id, created_by_user_id, workspace_id)`` like the chat/editor/
asset entities.

Two tables: ``knowledge_session_groups`` stores optional folder metadata, and
``knowledge_sessions.group_id`` is a nullable FK with ``ON DELETE SET NULL``.
A session stores its ordered Q&A items as a JSON array in ``items_json``
(question + the rendered answer record). Sessions are bounded per user, so
unlike the chunked retrieval store this stays a single row per session — the
"save whole entity" sync the editor/asset tiers also use. The heavy
``items_json`` is excluded from the LIST query (the left panel needs only
titles); it loads on open via ``get_session`` (the documents-body pattern).

Type decisions match the sibling ORMs: client-supplied ``ks_`` id PK,
unix-seconds ``Float`` timestamps, ``tenant_id`` for RLS, a scoped keyset index
with the id tiebreaker.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

knowledge_sessions_metadata = MetaData()

knowledge_session_groups = Table(
    "knowledge_session_groups",
    knowledge_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_knowledge_session_groups_owner_created",
        "tenant_id",
        "created_by_user_id",
        "workspace_id",
        "created_at",
        "id",
    ),
)
"""Optional grouping of a user's Knowledge Desk sessions."""

knowledge_sessions = Table(
    "knowledge_sessions",
    knowledge_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column(
        "group_id",
        Text,
        ForeignKey("knowledge_session_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # The ordered Q&A items as a JSON array (question + answer record). Heavy;
    # excluded from the list query, loaded on open.
    Column("items_json", Text, nullable=False, server_default=text("'[]'")),
    Column("lifecycle_status", Text, nullable=False, server_default=text("'active'")),
    Column("deletion_operation_id", Text, nullable=True),
    Column("deletion_stage", Text, nullable=True),
    Column("deletion_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "lifecycle_status IN ('active', 'deleting', 'delete_failed')",
        name="ck_knowledge_sessions_lifecycle_status",
    ),
    Index(
        "ix_knowledge_sessions_owner_updated",
        "tenant_id",
        "created_by_user_id",
        "workspace_id",
        "updated_at",
        "id",
    ),
    Index(
        "ix_knowledge_sessions_deletion_operation",
        "tenant_id",
        "deletion_operation_id",
    ),
)
