"""SQLAlchemy Core definition of the personal-access-token schema.

Separate ``MetaData`` on purpose (immutable-snapshot rule): the table
arrives with revision 0005. Timestamps are unix-seconds doubles,
mirroring the in-memory records and the session tables exactly.

The token id is both the primary key and the public identifier; only the
peppered HMAC of the secret is stored. ``owner_user_id`` is the canonical local
identity. Migration 0045 adds its cross-metadata ``users(id) ON DELETE
RESTRICT`` foreign key; token resolution additionally checks current user
status on every request.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Index, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

pat_metadata = MetaData()

personal_access_tokens = Table(
    "personal_access_tokens",
    pat_metadata,
    Column("token_id", Text, primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("owner_user_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("secret_hmac", Text, nullable=False),
    Column("scopes", JSONB, nullable=False, server_default=text("'[]'")),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=True),
    Column("last_used_at", Float, nullable=True),
    Column("revoked_at", Float, nullable=True),
    Index("ix_pat_owner", "tenant_id", "owner_user_id"),
)
"""Personal access tokens. The primary key serves the verification
lookup; ``ix_pat_owner`` serves listing and the disable cascade. No
index on ``secret_hmac`` — it is never queried, only compared."""
