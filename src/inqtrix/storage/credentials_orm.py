"""SQLAlchemy Core definition of the local-credentials schema.

Separate ``MetaData`` on purpose (immutable-snapshot rule): the table
arrives with revision 0008. Timestamps are unix-seconds doubles,
mirroring the session/PAT tables. The synthetic ``subject`` is the
primary key; only the argon2 hash is stored, never the plaintext. Email
is unique per tenant (case-insensitive via a lower() functional index)
so a login email maps to exactly one account.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Index, MetaData, Table, Text, func, text
from sqlalchemy.dialects.postgresql import UUID

credentials_metadata = MetaData()

local_credentials = Table(
    "local_credentials",
    credentials_metadata,
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "tenant_id", Text, nullable=False, server_default=text("'default'")
    ),
    Column("email", Text, nullable=False),
    Column("subject", Text, nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("disabled_at", Float, nullable=True),
)
"""Local email/password accounts. Primary key ``subject`` serves login
re-lookup and admin actions; the functional unique index below enforces
one account per case-insensitive email within a tenant."""

# One account per (tenant, email) — case-insensitive so Foo@x and foo@x
# cannot both register. Defined after the table so it can reference the
# real column; create_all emits CREATE UNIQUE INDEX ... (tenant_id, lower(email)).
Index(
    "uq_local_credentials_email",
    local_credentials.c.tenant_id,
    func.lower(local_credentials.c.email),
    unique=True,
)
