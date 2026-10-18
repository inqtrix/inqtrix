"""SQLAlchemy Core table for the usage ledger (runtime shape).

Own MetaData snapshot (immutable-snapshot rule, see quota_orm). The
authoritative DDL lives in migration 0073 — including the server
defaults, CHECK constraints, indexes, RLS policy, and grants. This
module carries ONLY what the store's Core INSERT/SELECT statements need
(column names and types); it is deliberately not a full DDL mirror and
must never be used to create the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import MetaData, Table
from sqlalchemy.dialects import postgresql

usage_metadata = MetaData()

llm_usage = Table(
    "llm_usage",
    usage_metadata,
    sa.Column(
        "id", sa.BigInteger(), sa.Identity(always=False), primary_key=True
    ),
    sa.Column("tenant_id", sa.Text(), nullable=False),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("workspace_id", sa.Text(), nullable=True),
    sa.Column("run_id", sa.Text(), nullable=True),
    sa.Column("feature", sa.Text(), nullable=False),
    sa.Column("operation", sa.Text(), nullable=False),
    sa.Column("model", sa.Text(), nullable=False),
    sa.Column("input_tokens", sa.BigInteger(), nullable=False),
    sa.Column("output_tokens", sa.BigInteger(), nullable=False),
    sa.Column("request_count", sa.Integer(), nullable=False),
    sa.Column("duration_ms", sa.BigInteger(), nullable=False),
    sa.Column("outcome", sa.Text(), nullable=False),
    sa.Column("created_at", sa.Float(), nullable=False),
)
