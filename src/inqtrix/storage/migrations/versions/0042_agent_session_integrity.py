"""Enforce agent-session concurrency and memo-only singleton artifacts.

Revision ID: 0042_agent_session_integrity
Revises: 0041_skill_templates

Only a root ``kind='agent'`` run occupies the session execution lease.
Queued, running, and every parked waiting status retain it; terminal rows and
agent children do not.  The partial unique index is the cross-process race
backstop for concurrent API submissions.

The previous artifact index made every session artifact kind a singleton,
which contradicted the multi-deliverable contract already implemented by the
control stores.  Memo remains the sole session singleton; deliverables are
addressed by ``artifact_id`` and revision CAS.
"""

from __future__ import annotations

from alembic import op

revision = "0042_agent_session_integrity"
down_revision = "0041_skill_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM runs WHERE session_id IS NOT NULL "
        "AND kind = 'agent' AND parent_run_id IS NULL AND status IN "
        "('queued', 'running', 'waiting_for_approval', "
        "'waiting_for_input', 'waiting_for_children') "
        "GROUP BY session_id HAVING COUNT(*) > 1) THEN "
        "RAISE EXCEPTION 'Migration 0042 blocked: duplicate active root "
        "agent runs exist for a session; terminalize or reconcile the "
        "duplicates before retrying.'; END IF; END $$"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_active_agent_session "
        "ON runs (session_id) "
        "WHERE session_id IS NOT NULL AND kind = 'agent' "
        "AND parent_run_id IS NULL AND status IN "
        "('queued', 'running', 'waiting_for_approval', "
        "'waiting_for_input', 'waiting_for_children')"
    )
    op.execute("DROP INDEX IF EXISTS uq_run_artifacts_session_kind")
    op.execute("DROP INDEX IF EXISTS uq_run_artifacts_run_kind")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_artifacts_session_memo "
        "ON run_artifacts (session_id, kind) "
        "WHERE session_id IS NOT NULL AND kind = 'memo'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_artifacts_run_kind "
        "ON run_artifacts (run_id, kind) WHERE session_id IS NULL "
        "AND kind IN ('evidence_bundle', 'critic_report', "
        "'editor_patch', 'answer')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_run_artifacts_session_memo")
    op.execute("DROP INDEX IF EXISTS uq_run_artifacts_run_kind")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM run_artifacts WHERE session_id IS NOT NULL "
        "GROUP BY session_id, kind HAVING COUNT(*) > 1) THEN "
        "RAISE EXCEPTION 'Migration 0042 downgrade blocked: duplicate "
        "session artifacts exist; reconcile multi-deliverables before "
        "restoring the legacy singleton index.'; END IF; END $$"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_artifacts_session_kind "
        "ON run_artifacts (session_id, kind) "
        "WHERE session_id IS NOT NULL"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM run_artifacts WHERE session_id IS NULL "
        "GROUP BY run_id, kind HAVING COUNT(*) > 1) THEN "
        "RAISE EXCEPTION 'Migration 0042 downgrade blocked: duplicate "
        "sessionless run artifacts exist; reconcile multi-deliverables "
        "before restoring the legacy run singleton index.'; "
        "END IF; END $$"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_artifacts_run_kind "
        "ON run_artifacts (run_id, kind) WHERE session_id IS NULL"
    )
    op.execute("DROP INDEX IF EXISTS uq_runs_active_agent_session")
