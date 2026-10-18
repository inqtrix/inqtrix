"""SQLAlchemy Core definitions of the content schema (files).

Separate ``MetaData`` from the identity schema on purpose: migration
0001 creates ``identity_metadata`` via ``create_all`` and must stay an
immutable snapshot — content tables arrive with their own revision.

Type decisions:

* ``id`` is text (``fl_...``) — uniform with the platform's other
  public identifiers (``run_``, ``kc_``, ``kd_``) so share tuples and
  log lines need no UUID special-casing.
* ``created_at`` is a unix-seconds double, mirroring
  :class:`~inqtrix.content.ports.FileRecord` exactly — the registry is
  a dumb persistence of that record, not a second time model.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

content_metadata = MetaData()

files = Table(
    "files",
    content_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("file_name", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("object_key", Text, nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Index("ix_files_tenant_owner", "tenant_id", "owner_user_id"),
    Index("ix_files_tenant_created", "tenant_id", "created_at"),
)
"""Uploaded-file metadata; the bytes live in the object store under
``object_key``. Row-level security is layered on by migration 0002."""
