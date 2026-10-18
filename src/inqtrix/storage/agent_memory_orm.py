"""SQLAlchemy Core definition of the workspace-agent memory candidate schema."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from inqtrix.agents.memory_ports import (
    MEMORY_CANDIDATE_STATUSES,
    MEMORY_CATEGORIES,
    MEMORY_FEEDBACK_VALUES,
    MEMORY_SCOPES,
)

agent_memory_metadata = MetaData()


def _values(items: tuple[str, ...]) -> str:
    return ", ".join(f"'{item}'" for item in items)


agent_memory_candidates = Table(
    "agent_memory_candidates",
    agent_memory_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("candidate_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("reason", Text, nullable=False, server_default=text("''")),
    Column("confidence", Float, nullable=False, server_default=text("0")),
    Column("source_run_id", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("memory_id", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id", "user_id", "candidate_id", name="pk_agent_memory_candidates"
    ),
    CheckConstraint(
        f"scope IN ({_values(MEMORY_SCOPES)})",
        name="ck_agent_memory_candidates_scope",
    ),
    CheckConstraint(
        f"category IN ({_values(MEMORY_CATEGORIES)})",
        name="ck_agent_memory_candidates_category",
    ),
    CheckConstraint(
        f"status IN ({_values(MEMORY_CANDIDATE_STATUSES)})",
        name="ck_agent_memory_candidates_status",
    ),
    Index(
        "ix_agent_memory_candidates_owner_status",
        "tenant_id",
        "user_id",
        "status",
        "created_at",
    ),
)
"""User-scoped review queue for long-term memory candidates."""


agent_feedback = Table(
    "agent_feedback",
    agent_memory_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("feedback_id", Text, nullable=False),
    Column("run_id", Text, nullable=False),
    Column("memory_id", Text, nullable=False, server_default=text("''")),
    Column("feedback", Text, nullable=False),
    Column("reason", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id", "user_id", "feedback_id", name="pk_agent_feedback"
    ),
    CheckConstraint(
        f"feedback IN ({_values(MEMORY_FEEDBACK_VALUES)})",
        name="ck_agent_feedback_feedback",
    ),
    Index(
        "ix_agent_feedback_owner_created",
        "tenant_id",
        "user_id",
        "created_at",
    ),
    Index(
        "ix_agent_feedback_owner_run",
        "tenant_id",
        "user_id",
        "run_id",
        "created_at",
    ),
)
"""User-scoped feedback history for workspace-agent runs."""
