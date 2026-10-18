"""SQLAlchemy Core definitions of the agent control schema.

Own ``MetaData`` snapshot (migration 0030), separate from the runs/identity
schemas on purpose — each migration's metadata is immutable.

Design decisions:

* Every table is a CHILD of ``runs`` via ``ON DELETE CASCADE`` (the
  constraint is raw DDL in migration 0030 — ``runs`` lives in a different
  MetaData snapshot): the durable
  run retention window is the single retention authority for control data.
  The session memo artifact survives across turns because each turn
  re-anchors it onto the newest run (upsert), so its lifetime follows the
  LATEST run of its session.
* Timestamps are unix-seconds floats, JSON columns are text-preserving
  ``JSON`` (not JSONB) — both mirroring the runs schema so wire payloads
  round-trip byte-identically between the memory and Postgres stores.
* Status/kind columns are text + CHECK built from the port's canonical
  tuples in :mod:`inqtrix.agents.control_ports` (single vocabulary
  authority, no native PG enums — the 0003 precedent).
* ``run_plans`` has a unique ``(run_id, version)``; ``run_artifacts`` keeps
  a memo-only session singleton index plus the run-local kind index. Kernel
  deliverables are multi-valued and addressed exclusively by artifact id.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

from inqtrix.agents.control_ports import (
    APPROVAL_KINDS,
    APPROVAL_STATUSES,
    ARTIFACT_KINDS,
    ARTIFACT_STATUSES,
    CLARIFICATION_STATUSES,
    PLAN_STATUSES,
    TASK_STATUSES,
    TASK_TOOL_KINDS,
)

agent_control_metadata = MetaData()


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in options)


run_plans = Table(
    "run_plans",
    agent_control_metadata,
    Column("plan_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # FK to runs(run_id) ON DELETE CASCADE — added as raw DDL by
    # migration 0030: ``runs`` lives in another MetaData snapshot, so the
    # reference cannot be declared here.
    Column("run_id", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'proposed'")),
    Column("created_by", Text, nullable=False, server_default=text("'agent'")),
    Column("summary_markdown", Text, nullable=False, server_default=text("''")),
    Column("assumptions", JSON, nullable=False, server_default=text("'[]'")),
    Column("success_criteria", JSON, nullable=False, server_default=text("'[]'")),
    Column("reason", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("run_id", "version", name="uq_run_plans_run_version"),
    CheckConstraint(
        f"status IN ({_values(PLAN_STATUSES)})", name="ck_run_plans_status"
    ),
    CheckConstraint(
        "created_by IN ('agent', 'user')", name="ck_run_plans_created_by"
    ),
    Index("ix_run_plans_tenant_run", "tenant_id", "run_id"),
)
"""Plan VERSIONS (append-only); at most one non-superseded/rejected version
per run, enforced by the store's save path."""

run_plan_tasks = Table(
    "run_plan_tasks",
    agent_control_metadata,
    Column("task_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "plan_id",
        Text,
        ForeignKey("run_plans.plan_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("run_id", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("tool_kind", Text, nullable=False),
    Column("objective", Text, nullable=False, server_default=text("''")),
    Column("queries", JSON, nullable=False, server_default=text("'[]'")),
    Column("gap_ids", JSON, nullable=False, server_default=text("'[]'")),
    Column("depends_on", JSON, nullable=False, server_default=text("'[]'")),
    Column("budget", JSON, nullable=False, server_default=text("'{}'")),
    Column("params", JSON, nullable=False, server_default=text("'{}'")),
    Column("expected_output", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("child_run_id", Text, nullable=True),
    Column("result_summary", Text, nullable=False, server_default=text("''")),
    Column("result_payload", JSON, nullable=False, server_default=text("'{}'")),
    # Task ids are stable across plan versions (an edit keeps unchanged
    # tasks' ids), so the identity is per plan version: composite primary
    # key (task_id, plan_id).
    CheckConstraint(
        f"tool_kind IN ({_values(TASK_TOOL_KINDS)})",
        name="ck_run_plan_tasks_tool_kind",
    ),
    CheckConstraint(
        f"status IN ({_values(TASK_STATUSES)})",
        name="ck_run_plan_tasks_status",
    ),
    Index("ix_run_plan_tasks_tenant_plan", "tenant_id", "plan_id"),
)
"""Tasks of one plan version, served ordered by ``ordinal``."""

run_approvals = Table(
    "run_approvals",
    agent_control_metadata,
    Column("approval_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # FK to runs(run_id) ON DELETE CASCADE — added as raw DDL by
    # migration 0030: ``runs`` lives in another MetaData snapshot, so the
    # reference cannot be declared here.
    Column("run_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("subject_type", Text, nullable=False, server_default=text("''")),
    Column("subject_id", Text, nullable=False, server_default=text("''")),
    Column("payload", JSON, nullable=False, server_default=text("'{}'")),
    Column("decision", Text, nullable=False, server_default=text("''")),
    Column("decision_payload", JSON, nullable=False, server_default=text("'{}'")),
    Column("note", Text, nullable=False, server_default=text("''")),
    Column("decided_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("interrupt_key", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("decided_at", Float, nullable=True),
    CheckConstraint(
        f"kind IN ({_values(APPROVAL_KINDS)})", name="ck_run_approvals_kind"
    ),
    CheckConstraint(
        f"status IN ({_values(APPROVAL_STATUSES)})",
        name="ck_run_approvals_status",
    ),
    Index("ix_run_approvals_tenant_run", "tenant_id", "run_id"),
)
"""Human-in-the-loop approval requests; the pending -> decided CAS happens
inside the run store's resume transaction."""

run_clarifications = Table(
    "run_clarifications",
    agent_control_metadata,
    Column("clarification_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # FK to runs(run_id) ON DELETE CASCADE — added as raw DDL by
    # migration 0030: ``runs`` lives in another MetaData snapshot, so the
    # reference cannot be declared here.
    Column("run_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("options", JSON, nullable=False, server_default=text("'[]'")),
    # Structured gate-round payload (migration 0039): 1-3 questions with
    # pickable options plus the per-question answers map. The legacy
    # question/options/answer/option_id columns stay authoritative for
    # whole-round free-text answers.
    Column("questions", JSON, nullable=False, server_default=text("'[]'")),
    Column("answers", JSON, nullable=False, server_default=text("'{}'")),
    Column(
        "default_assumption", Text, nullable=False, server_default=text("''")
    ),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("answer", Text, nullable=False, server_default=text("''")),
    Column("option_id", Text, nullable=False, server_default=text("''")),
    Column("answered_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("answered_at", Float, nullable=True),
    CheckConstraint(
        f"status IN ({_values(CLARIFICATION_STATUSES)})",
        name="ck_run_clarifications_status",
    ),
    Index("ix_run_clarifications_tenant_run", "tenant_id", "run_id"),
)
"""Questions a run asked its user; answering resumes the run."""

run_artifacts = Table(
    "run_artifacts",
    agent_control_metadata,
    Column("artifact_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # FK to runs(run_id) ON DELETE CASCADE — added as raw DDL by
    # migration 0030: ``runs`` lives in another MetaData snapshot, so the
    # reference cannot be declared here.
    Column("run_id", Text, nullable=False),
    Column("session_id", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False, server_default=text("'ready'")),
    Column("revision", Integer, nullable=False, server_default=text("1")),
    Column("updated_by", Text, nullable=False, server_default=text("'agent'")),
    Column("content_markdown", Text, nullable=False, server_default=text("''")),
    Column("payload", JSON, nullable=False, server_default=text("'{}'")),
    Column("refs", JSON, nullable=False, server_default=text("'[]'")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        f"kind IN ({_values(ARTIFACT_KINDS)})", name="ck_run_artifacts_kind"
    ),
    CheckConstraint(
        f"status IN ({_values(ARTIFACT_STATUSES)})",
        name="ck_run_artifacts_status",
    ),
    CheckConstraint(
        "updated_by IN ('agent', 'user')", name="ck_run_artifacts_updated_by"
    ),
    Index("ix_run_artifacts_tenant_run_created", "tenant_id", "run_id", "created_at", "artifact_id"),
    # The two upsert identities: one artifact per (session, kind) across
    # runs, one per (run, kind) for session-less diagnostics.
    Index(
        "uq_run_artifacts_session_memo",
        "session_id",
        "kind",
        unique=True,
        postgresql_where=text(
            "session_id IS NOT NULL AND kind = 'memo'"
        ),
    ),
    Index(
        "uq_run_artifacts_run_kind",
        "run_id",
        "kind",
        unique=True,
        postgresql_where=text(
            "session_id IS NULL AND kind IN "
            "('evidence_bundle', 'critic_report', 'editor_patch', 'answer')"
        ),
    ),
)
"""Artifact documents: the single source of truth for canvas content."""

run_artifact_revisions = Table(
    "run_artifact_revisions",
    agent_control_metadata,
    Column(
        "artifact_id",
        Text,
        ForeignKey("run_artifacts.artifact_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("revision", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by", Text, nullable=False, server_default=text("'agent'")),
    Column("content_markdown", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'ready'")),
    CheckConstraint(
        "created_by IN ('agent', 'user')",
        name="ck_run_artifact_revisions_created_by",
    ),
)
"""Append-only full-body snapshots per revision (revision diff source).

``status`` is the artifact status the write carried (``writing`` for a
streaming section flush, ``ready`` otherwise): the reader's version list
skips streaming snapshots, diffs and recovery keep them."""
