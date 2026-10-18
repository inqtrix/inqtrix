"""SQLAlchemy Core definition of the skill schema.

Separate ``MetaData`` on purpose (immutable-snapshot rule): the table
arrives with revision 0041. Timestamps are unix-seconds doubles,
mirroring the in-memory records exactly.

``owner_user_id`` follows the prompt-template UUID rule. Migration 0045 adds
the physical cross-metadata foreign key with ``ON DELETE RESTRICT``; ``NULL``
remains reserved for ownerless records in unscoped deployments.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

skill_metadata = MetaData()

skill_templates = Table(
    "skill_templates",
    skill_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    Column("label", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("when_to_use", Text, nullable=False, server_default=text("''")),
    Column("instructions_markdown", Text, nullable=False),
    Column(
        "clarification_points",
        JSONB,
        nullable=False,
        server_default=text("'[]'"),
    ),
    Column("deliverable", Text, nullable=False, server_default=text("''")),
    Column(
        "allowed_tools", JSONB, nullable=False, server_default=text("'[]'")
    ),
    Column(
        "requires_plan", Text, nullable=False, server_default=text("'auto'")
    ),
    Column(
        "invocation",
        Text,
        nullable=False,
        server_default=text("'user_only'"),
    ),
    Column("argument_hint", Text, nullable=False, server_default=text("''")),
    Column("model_tier", Text, nullable=False, server_default=text("''")),
    Column("effort", Text, nullable=False, server_default=text("''")),
    Column(
        "include_in_autocomplete",
        Boolean,
        nullable=False,
        server_default=text("true"),
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column(
        "revision", BigInteger, nullable=False, server_default=text("1")
    ),
    CheckConstraint(
        "deliverable IN ('', 'chat', 'canvas', 'email', 'talking_points')",
        name="ck_skill_templates_deliverable",
    ),
    CheckConstraint(
        "requires_plan IN ('always', 'auto', 'never')",
        name="ck_skill_templates_requires_plan",
    ),
    CheckConstraint(
        "invocation IN ('user_only', 'model_allowed')",
        name="ck_skill_templates_invocation",
    ),
    Index("ix_skill_templates_owner", "tenant_id", "owner_user_id"),
)
