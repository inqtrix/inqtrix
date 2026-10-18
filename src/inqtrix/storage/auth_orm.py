"""SQLAlchemy Core definitions of the auth-session schema.

Separate ``MetaData`` from the identity/content/runs schemas on
purpose: each migration's metadata is an immutable snapshot — the
session tables arrive with revision 0004.

Both tables hold short-lived state: sessions expire after the
configured absolute lifetime, flows after ten minutes; expired rows
are deleted lazily by the Postgres stores. Timestamps are unix-seconds
doubles, mirroring the in-memory records exactly.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

auth_metadata = MetaData()

auth_sessions = Table(
    "auth_sessions",
    auth_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("email", Text, nullable=True),
    Column("display_name", Text, nullable=True),
    Column("groups", JSONB, nullable=False, server_default=text("'[]'")),
    Column("csrf_random", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)
"""Authenticated browser sessions; the cookie carries only the opaque
``id``. No tokens are stored — the BFF discards them after login."""

auth_flows = Table(
    "auth_flows",
    auth_metadata,
    Column("state", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("code_verifier", Text, nullable=False),
    Column("nonce", Text, nullable=False),
    Column("next_path", Text, nullable=False, server_default=text("'/'")),
    Column("expires_at", Float, nullable=False),
    Column(
        "consumed",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
)
"""In-flight login transactions keyed by the OAuth ``state``;
consumption is a guarded one-time flip so replayed callbacks fail
even across API replicas."""
