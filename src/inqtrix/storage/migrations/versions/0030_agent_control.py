"""Agent control persistence: plans, approvals, clarifications, artifacts,
sessions.

Revision ID: 0030_agent_control
Revises: 0029_agent_run_tree

Creates the control tables the workspace-agent negotiates through
(``run_plans``/``run_plan_tasks``/``run_approvals``/``run_clarifications``/
``run_artifacts``/``run_artifact_revisions``) plus the ``agent_sessions``
pair, using the same private per-user structure as knowledge sessions. All
control tables are children of ``runs`` via ``ON DELETE CASCADE`` — the durable run
retention window (``run_durable_retention_seconds``, default 90 days) is
the single retention authority; the session memo artifact survives across
turns because every turn re-anchors it onto the newest run.

RLS follows the 0003/0021 precedent: GRANT to the app role, ENABLE + FORCE
ROW LEVEL SECURITY, fail-closed ``tenant_isolation`` policy.

The LangGraph checkpointer tables are deliberately NOT managed here:
they are library-owned (``AsyncPostgresSaver.setup()`` at container build),
and the checkpoint is a resumability cache, never the source of truth
for application data.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSON

PLAN_STATUSES = ("draft", "proposed", "approved", "rejected", "superseded")
APPROVAL_KINDS = ("discovery", "plan", "patch", "replan", "tool")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "edited")
CLARIFICATION_STATUSES = ("pending", "answered")
ARTIFACT_KINDS = (
    "memo",
    "evidence_bundle",
    "critic_report",
    "editor_patch",
    "answer",
    "deliverable",
)
ARTIFACT_STATUSES = ("writing", "ready")
TASK_TOOL_KINDS = (
    "web_research",
    "web_instant",
    "rag_query",
    "file_analysis",
    "synthesis",
)
TASK_STATUSES = (
    "pending",
    "running",
    "cancel_requested",
    "cancelled",
    "completed",
    "failed",
    "insufficient_evidence",
    "skipped",
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
    Column(
        "is_falsification",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
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
    Column("decided_by_sub", Text, nullable=True),
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
    # Structured gate-round payload (added by migration 0039): 1-3
    # questions with pickable options plus the per-question answers map.
    # The legacy question/options/answer/option_id columns stay
    # authoritative for whole-round free-text answers.
    Column("questions", JSON, nullable=False, server_default=text("'[]'")),
    Column("answers", JSON, nullable=False, server_default=text("'{}'")),
    Column(
        "default_assumption", Text, nullable=False, server_default=text("''")
    ),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("answer", Text, nullable=False, server_default=text("''")),
    Column("option_id", Text, nullable=False, server_default=text("''")),
    Column("answered_by_sub", Text, nullable=True),
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
    # The two upsert identities: one artifact per (session, kind)
    # across runs, one per (run, kind) for session-less diagnostics.
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
"""Artifact documents (canvas content): these rows are the single source of
truth for artifact content, and run events only signal that a row should be
re-read."""

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
    CheckConstraint(
        "created_by IN ('agent', 'user')",
        name="ck_run_artifact_revisions_created_by",
    ),
)
"""Append-only full-body snapshots per revision (revision diff source)."""
# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    text,
)

agent_sessions_metadata = MetaData()

agent_session_groups = Table(
    "agent_session_groups",
    agent_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_agent_session_groups_owner_created",
        "tenant_id",
        "created_by_sub",
        "workspace_id",
        "created_at",
        "id",
    ),
)
"""User-defined folders for agent sessions in the desk rail."""

agent_sessions = Table(
    "agent_sessions",
    agent_sessions_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_sub", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column(
        "group_id",
        Text,
        ForeignKey("agent_session_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # The client-side desk snapshot (timeline, follow-ups). Heavy body:
    # list queries exclude it (load-on-open), exactly like knowledge
    # sessions. The durable artifact CONTENT lives in run_artifacts, not
    # here — items_json only mirrors what the desk rendered.
    Column("items_json", Text, nullable=False, server_default=text("'[]'")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_agent_sessions_owner_updated",
        "tenant_id",
        "created_by_sub",
        "workspace_id",
        "updated_at",
        "id",
    ),
)
"""Saved agent-desk sessions; ``runs.session_id`` and
``run_artifacts.session_id`` reference these ids WITHOUT a foreign key
(sessions may be deleted while their runs age out on their own)."""

revision = "0030_agent_control"
down_revision = "0029_agent_run_tree"
branch_labels = None
depends_on = None

_APP_ROLE = "inqtrix_app"

_TABLES = (
    "run_plans",
    "run_plan_tasks",
    "run_approvals",
    "run_clarifications",
    "run_artifacts",
    "run_artifact_revisions",
    "agent_session_groups",
    "agent_sessions",
)


_RUNS_CASCADES = (
    "run_plans",
    "run_approvals",
    "run_clarifications",
    "run_artifacts",
)


def upgrade() -> None:
    bind = op.get_bind()
    agent_control_metadata.create_all(bind=bind)
    agent_sessions_metadata.create_all(bind=bind)
    # runs(run_id) lives in another MetaData snapshot, so the CASCADE
    # foreign keys are raw DDL here instead of ORM declarations.
    for table in _RUNS_CASCADES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_run "
            "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE"
        )
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP_ROLE}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            "USING (tenant_id = (SELECT inqtrix_current_tenant_id())) "
            "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))"
        )


def downgrade() -> None:
    agent_sessions_metadata.drop_all(bind=op.get_bind())
    agent_control_metadata.drop_all(bind=op.get_bind())
