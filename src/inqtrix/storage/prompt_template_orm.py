"""SQLAlchemy Core definition of the prompt-template schema.

Separate ``MetaData`` on purpose (immutable-snapshot rule): the table
arrives with revision 0006. Timestamps are unix-seconds doubles,
mirroring the in-memory records exactly.

``owner_user_id`` is the canonical UUID. The destructive v0.2 migration adds
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

prompt_template_metadata = MetaData()

prompt_templates = Table(
    "prompt_templates",
    prompt_template_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    Column("title", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("category", Text, nullable=True),
    Column("content_markdown", Text, nullable=False),
    Column("visibility", JSONB, nullable=False, server_default=text("'{}'")),
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
        "category IS NULL OR category IN "
        "('instruction', 'function', 'context')",
        name="ck_prompt_templates_category",
    ),
    Index("ix_prompt_templates_owner", "tenant_id", "owner_user_id"),
)

prompt_template_seed_markers = Table(
    "prompt_template_seed_markers",
    prompt_template_metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("seeded_at", Float, nullable=False),
)
"""One row per (tenant, user): the stock prompts were offered exactly
once. Claimed atomically WITH the template inserts (revision 0082), so a
deleted default stays deleted and concurrent first listings cannot
double-seed."""

