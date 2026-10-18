"""SQLAlchemy Core definitions of the durable run schema.

Separate ``MetaData`` from the identity and content schemas on
purpose: each migration's metadata is an immutable snapshot — run
tables arrive with their own revision (0003).

Type decisions:

* ``run_id`` is text (``run_...``) — the existing public run
  identifier, unchanged from the in-memory store.
* All timestamps are unix-seconds doubles, mirroring the in-memory
  :class:`~inqtrix.server.runs.RunRecord` and the pinned wire format
  (``created_at``/``started_at``/``finished_at`` are float epochs in
  run summaries and event envelopes).
* JSON columns use the text-preserving ``JSON`` type, NOT ``JSONB``:
  JSONB normalizes key order, which would break the byte-level SSE
  replay parity with the in-memory store (``json.dumps`` of the
  loaded dict must reproduce the original frame).
* ``status`` is text + CHECK; the lifecycle ordering lives only in
  :class:`~inqtrix.server.runs.RunStatus` (single ordering authority,
  no native PG enums).
* ``event_seq`` on the run row is the per-run sequence allocator:
  ``UPDATE ... SET event_seq = event_seq + 1 RETURNING event_seq``
  serializes on the row lock, which keeps sequences gap-free and
  strictly increasing across processes — the property the byte-level
  SSE contract pins.
* ``claimed_by`` + ``attempt`` are the worker fencing pair: a claim
  increments ``attempt`` and every terminal write is guarded by both,
  so a zombie worker whose job was reclaimed cannot overwrite the
  second attempt's result.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

runs_metadata = MetaData()

runs = Table(
    "runs",
    runs_metadata,
    Column("run_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "status",
        Text,
        nullable=False,
        server_default=text("'queued'"),
    ),
    Column("mode", Text, nullable=False, server_default=text("'research'")),
    # Agent-tree columns (migration 0029): role of the run in an agent
    # tree plus its parent/root links. ``session_id`` is the shared durable
    # relation to a saved Agent or Knowledge session. All remain additive
    # with defaults; summaries omit the defaults entirely.
    Column("kind", Text, nullable=False, server_default=text("'standard'")),
    Column("parent_run_id", Text, nullable=True),
    Column("root_run_id", Text, nullable=True),
    Column("session_id", Text, nullable=True),
    # An incognito knowledge ask (0094): deleted after its own short
    # retention window; the partial index below serves that sweep.
    Column("incognito", Boolean, nullable=False, server_default=text("false")),
    # TTL anchor while a run sits in a waiting status (0029).
    Column("waiting_since", Float, nullable=True),
    Column("question", Text, nullable=False),
    Column("stack_name", Text, nullable=False, server_default=text("'default'")),
    Column("workspace_id", Text, nullable=True),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("created_by_tenant_id", Text, nullable=True),
    Column("source_run_id", Text, nullable=True),
    Column("execution_actor_user_id", UUID(as_uuid=True), nullable=True),
    Column(
        "execution_scopes",
        JSON,
        nullable=False,
        server_default=text("'[]'"),
    ),
    Column("agent_overrides", JSON, nullable=False, server_default=text("'{}'")),
    Column("request_payload", JSON, nullable=True),
    Column("snapshot", JSON, nullable=False, server_default=text("'{}'")),
    Column("result", JSON, nullable=True),
    Column("error", JSON, nullable=True),
    Column(
        "cancel_requested",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("claimed_by", Text, nullable=True),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("event_seq", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    # Execution timing is accumulated at lifecycle boundaries. ``started_at``
    # remains the immutable first-start timestamp; every later dispatch uses a
    # separately identified segment.
    Column("segment_count", Integer, nullable=False, server_default=text("0")),
    Column("current_segment_id", Text, nullable=True),
    Column("current_segment_reason", Text, nullable=True),
    Column("queued_since", Float, nullable=True),
    Column("active_started_at", Float, nullable=True),
    Column("active_seconds", Float, nullable=False, server_default=text("0")),
    Column("waiting_seconds", Float, nullable=False, server_default=text("0")),
    Column("queued_seconds", Float, nullable=False, server_default=text("0")),
    # Keyset-pagination index: the id tiebreaker after created_at is
    # mandatory — created_at is a float epoch and collides on bulk inserts,
    # so a (created_at, run_id) cursor needs both columns to page without
    # skipping or repeating rows. Supersedes the old (tenant_id, created_at).
    Index("ix_runs_tenant_created_id", "tenant_id", "created_at", "run_id"),
    Index("ix_runs_tenant_status", "tenant_id", "status"),
    # Child listing plus saved-session grouping.
    Index("ix_runs_tenant_parent", "tenant_id", "parent_run_id"),
    Index("ix_runs_tenant_session", "tenant_id", "session_id"),
    Index(
        "ix_runs_incognito_finished",
        "finished_at",
        postgresql_where=text("incognito"),
    ),
    Index(
        "uq_runs_import_owner_source",
        "tenant_id",
        "created_by_user_id",
        "source_run_id",
        unique=True,
        postgresql_where=text(
            "created_by_user_id IS NOT NULL AND source_run_id IS NOT NULL"
        ),
    ),
    Index(
        "uq_runs_import_unscoped_source",
        "tenant_id",
        "source_run_id",
        unique=True,
        postgresql_where=text(
            "created_by_user_id IS NULL AND source_run_id IS NOT NULL"
        ),
    ),
    # Per-user in-flight cap COUNT (created_by_user_id + active status) on every
    # submit; the tenant-leading indexes don't narrow by user on a
    # single-tenant deployment. Partial over the two active statuses so it
    # indexes only the tiny live set (0037).
    Index(
        "ix_runs_user_id_active",
        "created_by_user_id",
        "status",
        postgresql_where=text("status IN ('queued', 'running')"),
    ),
    Index(
        "uq_runs_active_agent_session",
        "session_id",
        unique=True,
        postgresql_where=text(
            "session_id IS NOT NULL AND kind = 'agent' "
            "AND parent_run_id IS NULL AND status IN "
            "('queued', 'running', 'waiting_for_approval', "
            "'waiting_for_input', 'waiting_for_children')"
        ),
    ),
)
"""Durable run records — the source of truth once
``INQTRIX_STORAGE_BACKEND=postgres`` is active; the Valkey stream only
carries dispatch messages. The status CHECK constraint is added in
migration 0003 (values from :class:`~inqtrix.server.runs.RunStatus`)."""

run_events = Table(
    "run_events",
    runs_metadata,
    Column(
        "run_id",
        Text,
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("sequence", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("type", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("data", JSON, nullable=False, server_default=text("'{}'")),
)
"""Per-run event log in emission order; ``(run_id, sequence)`` is the
primary key and ``sequence`` is allocated from ``runs.event_seq`` so
replay reproduces the in-memory SSE stream byte-compatibly."""
