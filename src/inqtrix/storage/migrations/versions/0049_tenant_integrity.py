"""Enforce tenant-consistent run lineage and agent-control references.

Revision ID: 0049_tenant_integrity
Revises: 0048_editor_collaboration

Revision 0043 originally joined globally unique public identifiers without
also carrying the tenant boundary. This repair reruns those backfills with the
tenant in every join, rejects pre-existing cross-tenant references explicitly,
and makes the relational portions of that invariant durable in PostgreSQL.
"""

from __future__ import annotations

from alembic import op

revision = "0049_tenant_integrity"
down_revision = "0048_editor_collaboration"
branch_labels = None
depends_on = None

_TENANT_RLS_TABLES = (
    "account_preferences",
    "agent_feedback",
    "agent_memory_candidates",
    "agent_session_groups",
    "agent_sessions",
    "asset_groups",
    "asset_records",
    "asset_sections",
    "audit_log",
    "auth_flows",
    "auth_sessions",
    "chat_messages",
    "chat_thread_groups",
    "chat_threads",
    "editor_collaboration_instances",
    "editor_collaboration_leases",
    "editor_collaboration_snapshots",
    "editor_collaboration_updates",
    "editor_comments",
    "editor_documents",
    "editor_folders",
    "editor_patches",
    "files",
    "indexing_job_events",
    "indexing_jobs",
    "invitations",
    "knowledge_chunks",
    "knowledge_collections",
    "knowledge_documents",
    "knowledge_session_groups",
    "knowledge_sessions",
    "local_credentials",
    "personal_access_tokens",
    "prompt_templates",
    "quota_limits",
    "quota_usage_counters",
    "resource_shares",
    "run_approvals",
    "run_artifact_revisions",
    "run_artifacts",
    "run_clarifications",
    "run_events",
    "run_plan_tasks",
    "run_plans",
    "runs",
    "skill_templates",
    "tenant_security_state",
    "user_events",
    "users",
    "vector_index_history",
    "vector_index_members",
    "vector_index_records",
    "workspace_members",
    "workspaces",
)
"""Frozen 0049 table inventory; revisions never import mutable runtime code."""

_TENANT_REFERENCE_PREFLIGHT_SQL = """
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM runs AS child
        LEFT JOIN runs AS parent ON parent.run_id = child.parent_run_id
        WHERE child.parent_run_id IS NOT NULL
          AND (parent.run_id IS NULL
               OR child.tenant_id IS DISTINCT FROM parent.tenant_id)
    ) THEN
        RAISE EXCEPTION
            '0049 found an orphaned or cross-tenant run parent reference';
    END IF;
    IF EXISTS (
        SELECT 1 FROM runs AS child
        LEFT JOIN runs AS root ON root.run_id = child.root_run_id
        WHERE child.root_run_id IS NOT NULL
          AND (root.run_id IS NULL
               OR child.tenant_id IS DISTINCT FROM root.tenant_id)
    ) THEN
        RAISE EXCEPTION
            '0049 found an orphaned or cross-tenant run root reference';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_plans AS plan
        JOIN runs AS run ON run.run_id = plan.run_id
        WHERE plan.tenant_id IS DISTINCT FROM run.tenant_id
    ) THEN
        RAISE EXCEPTION
            '0049 found a run plan reference crossing tenant boundaries';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_plan_tasks AS task
        LEFT JOIN run_plans AS plan ON plan.plan_id = task.plan_id
        LEFT JOIN runs AS run ON run.run_id = task.run_id
        WHERE plan.plan_id IS NULL
           OR run.run_id IS NULL
           OR task.tenant_id IS DISTINCT FROM plan.tenant_id
           OR task.tenant_id IS DISTINCT FROM run.tenant_id
           OR task.run_id IS DISTINCT FROM plan.run_id
    ) THEN
        RAISE EXCEPTION
            '0049 found a plan task reference outside its tenant or run';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_plan_tasks AS task
        LEFT JOIN runs AS child ON child.run_id = task.child_run_id
        WHERE task.child_run_id IS NOT NULL
          AND (child.run_id IS NULL
               OR task.tenant_id IS DISTINCT FROM child.tenant_id)
    ) THEN
        RAISE EXCEPTION
            '0049 found an orphaned or cross-tenant task child run';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_approvals AS approval
        JOIN runs AS run ON run.run_id = approval.run_id
        WHERE approval.tenant_id IS DISTINCT FROM run.tenant_id
    ) THEN
        RAISE EXCEPTION
            '0049 found an approval run reference crossing tenant boundaries';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_approvals AS approval
        JOIN run_plans AS plan ON plan.plan_id = approval.subject_id
        WHERE approval.subject_type = 'plan'
          AND btrim(approval.subject_id) <> ''
          AND (approval.tenant_id IS DISTINCT FROM plan.tenant_id
               OR approval.run_id IS DISTINCT FROM plan.run_id)
    ) THEN
        RAISE EXCEPTION
            '0049 found an approval plan subject outside its tenant or run';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_clarifications AS clarification
        JOIN runs AS run ON run.run_id = clarification.run_id
        WHERE clarification.tenant_id IS DISTINCT FROM run.tenant_id
    ) THEN
        RAISE EXCEPTION
            '0049 found a clarification run reference crossing tenants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_artifacts AS artifact
        JOIN runs AS run ON run.run_id = artifact.run_id
        WHERE artifact.tenant_id IS DISTINCT FROM run.tenant_id
    ) THEN
        RAISE EXCEPTION
            '0049 found an artifact run reference crossing tenants';
    END IF;
    IF EXISTS (
        SELECT 1 FROM run_artifact_revisions AS artifact_revision
        JOIN run_artifacts AS artifact
          ON artifact.artifact_id = artifact_revision.artifact_id
        WHERE artifact_revision.tenant_id IS DISTINCT FROM artifact.tenant_id
    ) THEN
        RAISE EXCEPTION
            '0049 found an artifact revision crossing tenant boundaries';
    END IF;
END
$migration$;
"""

_APPROVAL_SUBJECT_BACKFILL_SQL = """
UPDATE run_approvals AS approval
SET subject_id = plan.plan_id
FROM run_plans AS plan
WHERE approval.run_id = plan.run_id
  AND approval.tenant_id = plan.tenant_id
  AND approval.kind IN ('plan', 'replan')
  AND approval.subject_type = 'plan'
  AND btrim(approval.subject_id) = ''
  AND plan.version = CASE
      WHEN (approval.payload->>'plan_version') ~ '^[1-9][0-9]*$'
      THEN (approval.payload->>'plan_version')::integer
      ELSE NULL
  END
"""

_APPROVAL_SUBJECT_POSTCHECK_SQL = """
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM run_approvals AS approval
        LEFT JOIN run_plans AS plan
          ON plan.plan_id = approval.subject_id
         AND plan.tenant_id = approval.tenant_id
         AND plan.run_id = approval.run_id
        WHERE approval.subject_type = 'plan'
          AND (btrim(approval.subject_id) = '' OR plan.plan_id IS NULL)
    ) THEN
        RAISE EXCEPTION
            '0049 could not resolve every plan approval inside its tenant and run';
    END IF;
END
$migration$;
"""

_RUN_ROOT_LINEAGE_BACKFILL_SQL = """
WITH RECURSIVE run_tree AS (
    SELECT tenant_id, run_id, run_id AS canonical_root
    FROM runs
    WHERE parent_run_id IS NULL
    UNION ALL
    SELECT child.tenant_id, child.run_id, run_tree.canonical_root
    FROM runs AS child
    JOIN run_tree
      ON child.tenant_id = run_tree.tenant_id
     AND child.parent_run_id = run_tree.run_id
)
UPDATE runs AS target
SET root_run_id = run_tree.canonical_root
FROM run_tree
WHERE target.tenant_id = run_tree.tenant_id
  AND target.run_id = run_tree.run_id
  AND target.parent_run_id IS NOT NULL
  AND target.root_run_id IS DISTINCT FROM run_tree.canonical_root
"""

_UPGRADE_CONSTRAINTS = (
    "ALTER TABLE runs ADD CONSTRAINT uq_runs_tenant_run "
    "UNIQUE (tenant_id, run_id)",
    "ALTER TABLE run_plans ADD CONSTRAINT uq_run_plans_tenant_plan_run "
    "UNIQUE (tenant_id, plan_id, run_id)",
    "ALTER TABLE run_artifacts ADD CONSTRAINT uq_run_artifacts_tenant_artifact "
    "UNIQUE (tenant_id, artifact_id)",
    "ALTER TABLE runs ADD CONSTRAINT fk_runs_tenant_parent "
    "FOREIGN KEY (tenant_id, parent_run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE runs ADD CONSTRAINT fk_runs_tenant_root "
    "FOREIGN KEY (tenant_id, root_run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_plans ADD CONSTRAINT fk_run_plans_tenant_run "
    "FOREIGN KEY (tenant_id, run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_plan_tasks ADD CONSTRAINT "
    "fk_run_plan_tasks_tenant_plan_run "
    "FOREIGN KEY (tenant_id, plan_id, run_id) "
    "REFERENCES run_plans (tenant_id, plan_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_plan_tasks ADD CONSTRAINT fk_run_plan_tasks_tenant_run "
    "FOREIGN KEY (tenant_id, run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_plan_tasks ADD CONSTRAINT "
    "fk_run_plan_tasks_tenant_child_run "
    "FOREIGN KEY (tenant_id, child_run_id) "
    "REFERENCES runs (tenant_id, run_id) "
    "ON DELETE SET NULL (child_run_id)",
    "ALTER TABLE run_approvals ADD CONSTRAINT fk_run_approvals_tenant_run "
    "FOREIGN KEY (tenant_id, run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_clarifications ADD CONSTRAINT "
    "fk_run_clarifications_tenant_run FOREIGN KEY (tenant_id, run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_artifacts ADD CONSTRAINT fk_run_artifacts_tenant_run "
    "FOREIGN KEY (tenant_id, run_id) "
    "REFERENCES runs (tenant_id, run_id) ON DELETE CASCADE",
    "ALTER TABLE run_artifact_revisions ADD CONSTRAINT "
    "fk_run_artifact_revisions_tenant_artifact "
    "FOREIGN KEY (tenant_id, artifact_id) "
    "REFERENCES run_artifacts (tenant_id, artifact_id) ON DELETE CASCADE",
)

_DOWNGRADE_CONSTRAINTS = (
    ("run_artifact_revisions", "fk_run_artifact_revisions_tenant_artifact"),
    ("run_artifacts", "fk_run_artifacts_tenant_run"),
    ("run_clarifications", "fk_run_clarifications_tenant_run"),
    ("run_approvals", "fk_run_approvals_tenant_run"),
    ("run_plan_tasks", "fk_run_plan_tasks_tenant_child_run"),
    ("run_plan_tasks", "fk_run_plan_tasks_tenant_run"),
    ("run_plan_tasks", "fk_run_plan_tasks_tenant_plan_run"),
    ("run_plans", "fk_run_plans_tenant_run"),
    ("runs", "fk_runs_tenant_root"),
    ("runs", "fk_runs_tenant_parent"),
    ("run_artifacts", "uq_run_artifacts_tenant_artifact"),
    ("run_plans", "uq_run_plans_tenant_plan_run"),
    ("runs", "uq_runs_tenant_run"),
)


def upgrade() -> None:
    """Repair prior backfills and enforce tenant-consistent references."""
    op.execute(_TENANT_REFERENCE_PREFLIGHT_SQL)
    op.execute(_APPROVAL_SUBJECT_BACKFILL_SQL)
    op.execute(_APPROVAL_SUBJECT_POSTCHECK_SQL)
    op.execute(_RUN_ROOT_LINEAGE_BACKFILL_SQL)
    for statement in _UPGRADE_CONSTRAINTS:
        op.execute(statement)
    for table_name in _TENANT_RLS_TABLES:
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE {table_name} "
            "FROM PUBLIC, inqtrix_app"
        )
        privileges = (
            "SELECT, INSERT"
            if table_name == "audit_log"
            else "SELECT, INSERT, UPDATE, DELETE"
        )
        op.execute(
            f"GRANT {privileges} ON TABLE {table_name} TO inqtrix_app"
        )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE alembic_version "
        "FROM PUBLIC, inqtrix_app"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES (version_num) ON TABLE alembic_version "
        "FROM PUBLIC, inqtrix_app"
    )
    op.execute("GRANT SELECT ON TABLE alembic_version TO inqtrix_app")
    op.execute(
        "REVOKE ALL PRIVILEGES ON FUNCTION "
        "inqtrix_current_tenant_id() FROM PUBLIC, inqtrix_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
        "TO inqtrix_app"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON SEQUENCE "
        "audit_log_id_seq, user_events_id_seq FROM PUBLIC, inqtrix_app"
    )
    op.execute(
        "GRANT USAGE ON SEQUENCE audit_log_id_seq, user_events_id_seq "
        "TO inqtrix_app"
    )


def downgrade() -> None:
    """Remove the additive integrity constraints without reverting data."""
    op.execute(
        "REVOKE EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
        "FROM inqtrix_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id() TO PUBLIC"
    )
    op.execute(
        "GRANT SELECT ON SEQUENCE user_events_id_seq TO inqtrix_app"
    )
    op.execute("REVOKE SELECT ON TABLE alembic_version FROM inqtrix_app")
    for table_name, constraint_name in _DOWNGRADE_CONSTRAINTS:
        op.execute(
            f"ALTER TABLE {table_name} DROP CONSTRAINT {constraint_name}"
        )
