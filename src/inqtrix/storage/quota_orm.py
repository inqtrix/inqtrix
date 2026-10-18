"""SQLAlchemy Core definition of the quota schema.

Separate ``MetaData`` on purpose (immutable-snapshot rule): the tables
arrive with revision 0007. Timestamps and counters are unix-seconds
doubles, mirroring the in-memory store exactly.

Two tables: ``quota_usage_counters`` holds one row per
``(tenant, subject, dimension, period_start)`` window (a new window =
a new row, so old months stay as harmless history), and
``quota_limits`` holds the admin-set overrides plus the tenant default
(the sentinel ``__quota_default__`` subject). Neither ``subject_user_id``
is a foreign key into the users mirror — quota state outlives mirror
rows, same rule as PATs and templates.
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
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

quota_metadata = MetaData()

quota_usage_counters = Table(
    "quota_usage_counters",
    quota_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("subject_user_id", UUID(as_uuid=True), nullable=False),
    Column("dimension", Text, nullable=False),
    Column("period_start", Float, nullable=False),
    Column("used", BigInteger, nullable=False, server_default=text("0")),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "subject_user_id",
        "dimension",
        "period_start",
        name="pk_quota_usage_counters",
    ),
    Index("ix_quota_usage_subject", "tenant_id", "subject_user_id"),
)

quota_usage_adjustments = Table(
    "quota_usage_adjustments",
    quota_metadata,
    Column("adjustment_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("subject_user_id", UUID(as_uuid=True), nullable=False),
    Column("dimension", Text, nullable=False),
    Column("period_start", Float, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "adjustment_id NOT LIKE 'asset-upload:%:stored-bytes' AND "
        "adjustment_id NOT LIKE 'asset-delete:%:stored-bytes'",
        name="ck_quota_adjustments_no_file_stock",
    ),
    Index(
        "ix_quota_usage_adjustments_subject",
        "tenant_id",
        "subject_user_id",
        "created_at",
    ),
)
"""Idempotency receipts for retryable usage adjustments.

The counter mutation and receipt are committed together, so a worker crash can
repeat an adjustment without double-booking or permanently missing it.
"""

quota_stock_lifecycles = Table(
    "quota_stock_lifecycles",
    quota_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("stock_key", Text, nullable=False),
    Column("subject_user_id", UUID(as_uuid=True), nullable=False),
    Column("dimension", Text, nullable=False),
    Column("amount", BigInteger, nullable=False, server_default=text("0")),
    Column("tombstoned", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "stock_key",
        name="pk_quota_stock_lifecycles",
    ),
    CheckConstraint("amount >= 0", name="ck_quota_stock_lifecycles_amount"),
    CheckConstraint(
        "NOT tombstoned OR amount = 0",
        name="ck_quota_stock_lifecycles_tombstone_zero",
    ),
    Index(
        "ix_quota_stock_lifecycles_subject",
        "tenant_id",
        "subject_user_id",
        "dimension",
    ),
)
"""Resource-level truth behind order-independent stock counters."""

quota_limits = Table(
    "quota_limits",
    quota_metadata,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("subject_user_id", UUID(as_uuid=True), nullable=True),
    Column("dimension", Text, nullable=False),
    Column("limit_value", BigInteger, nullable=False),
    Column("set_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("set_at", Float, nullable=False),
    Index(
        "uq_quota_limits_user",
        "tenant_id",
        "subject_user_id",
        "dimension",
        unique=True,
        postgresql_where=text("subject_user_id IS NOT NULL"),
    ),
    Index(
        "uq_quota_limits_default",
        "tenant_id",
        "dimension",
        unique=True,
        postgresql_where=text("subject_user_id IS NULL"),
    ),
)
