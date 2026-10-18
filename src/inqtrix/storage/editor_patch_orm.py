"""SQLAlchemy Core definitions of the editor-patch schema.

Own ``MetaData`` snapshot (migration 0031), separate from the editor and
runs schemas on purpose — each migration's metadata is immutable.

Design decisions (the 0030 pattern):

* ``document_id`` is a raw-DDL FK to ``editor_documents(id)`` with
  ``ON DELETE CASCADE`` and ``run_id`` a raw-DDL FK to ``runs(run_id)``
  with ``ON DELETE SET NULL`` — both parents live in other MetaData
  snapshots, so the constraints are added by migration 0031, not here.
  The document owns the patch's lifetime; run retention only detaches
  the back-reference.
* Timestamps are floats in Unix seconds, except ``created_at``, which
  keeps the unit of its writer (see
  :func:`~inqtrix.project.editor_patch_ports.patch_created_at_ms`); JSON
  columns are text-preserving ``JSON`` (not JSONB) — wire payloads
  round-trip byte-identically between the memory and Postgres stores.
* ``source``/``status`` are text + CHECK built from the port's canonical
  tuples in :mod:`inqtrix.project.editor_patch_ports` (single vocabulary
  authority, no native PG enums).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    case,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID

from inqtrix.project.editor_patch_ports import (
    PATCH_SOURCES,
    PATCH_STATUSES,
    UNIX_SECONDS_UPPER_BOUND,
)

editor_patch_metadata = MetaData()


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in options)


editor_patches = Table(
    "editor_patches",
    editor_patch_metadata,
    Column("patch_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # Both FKs (document_id -> editor_documents(id) ON DELETE CASCADE,
    # run_id -> runs(run_id) ON DELETE SET NULL) are raw DDL in migration
    # 0031 — the parents live in other MetaData snapshots (module docstring).
    Column("document_id", Text, nullable=False),
    Column("run_id", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("edits", JSON, nullable=False, server_default=text("'[]'")),
    Column("summary", Text, nullable=False, server_default=text("''")),
    Column("warnings", JSON, nullable=False, server_default=text("'[]'")),
    Column("revision_before", Integer, nullable=False),
    Column("collaboration_generation", BigInteger, nullable=True),
    Column("base_sequence", BigInteger, nullable=True),
    Column("decision_sequence", BigInteger, nullable=True),
    Column(
        "suggestion_ids",
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
    Column("applied_revision", Integer, nullable=True),
    Column("applied_edit_ids", JSON, nullable=True),
    Column("note", Text, nullable=False, server_default=text("''")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("created_by_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("decided_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("decided_by_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("command_id", UUID(as_uuid=True), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("decided_at", Float, nullable=True),
    CheckConstraint(
        f"source IN ({_values(PATCH_SOURCES)})", name="ck_editor_patches_source"
    ),
    CheckConstraint(
        f"status IN ({_values(PATCH_STATUSES)})", name="ck_editor_patches_status"
    ),
    CheckConstraint(
        "(collaboration_generation IS NULL "
        "AND base_sequence IS NULL AND decision_sequence IS NULL) OR "
        "(collaboration_generation >= 1 AND base_sequence >= 0 "
        "AND (decision_sequence IS NULL OR decision_sequence >= 1))",
        name="ck_editor_patches_collaboration_state",
    ),
    CheckConstraint(
        "jsonb_typeof(suggestion_ids) = 'array'",
        name="ck_editor_patches_suggestion_ids",
    ),
    Index(
        "ix_editor_patches_tenant_document",
        "tenant_id",
        "document_id",
        "created_at",
        "patch_id",
    ),
    Index(
        "ix_editor_patches_collaboration_command",
        "tenant_id",
        "document_id",
        "collaboration_generation",
        "command_id",
        postgresql_where=text("command_id IS NOT NULL"),
    ),
)
"""Proposed anchored document edits with their apply/reject lifecycle;
the ``pending -> accepted/rejected`` CAS happens in the store's mark
writes."""

editor_patch_created_at_ms = case(
    (
        editor_patches.c.created_at < UNIX_SECONDS_UPPER_BOUND,
        editor_patches.c.created_at * 1_000,
    ),
    else_=editor_patches.c.created_at,
)
"""``created_at`` in epoch milliseconds, the SQL form of
:func:`~inqtrix.project.editor_patch_ports.patch_created_at_ms`. Every order
of patches by creation time, and its keyset cursor, compares through it."""
