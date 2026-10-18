"""Static contracts for the irreversible v0.2 identity migration."""

from __future__ import annotations

import inspect
from importlib import import_module
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from inqtrix.storage.migration_contract import SCHEMA_HEAD_REVISION

from inqtrix.storage.migrate import build_alembic_config


def _migration(name: str):
    """Import one packaged migration module by filename stem."""
    return import_module(f"inqtrix.storage.migrations.versions.{name}")


def test_packaged_revision_modules_have_no_runtime_imports() -> None:
    """Historical DDL must depend only on revision-local schema literals."""
    versions_dir = Path(_migration("0001_identity_schema").__file__ or "").parent
    offenders: dict[str, list[str]] = {}
    for path in sorted(versions_dir.glob("[0-9][0-9][0-9][0-9]_*.py")):
        runtime_imports = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(("from inqtrix", "import inqtrix"))
        ]
        if runtime_imports:
            offenders[path.name] = runtime_imports

    assert offenders == {}


def test_historical_create_migrations_are_frozen_snapshots() -> None:
    """Fresh installs must traverse each revision's frozen schema."""
    identity = _migration("0001_identity_schema")
    runs = _migration("0003_runs_durability")
    knowledge_sessions = _migration("0021_knowledge_sessions")
    user_events = _migration("0047_resource_sync_and_reindex")

    assert "inqtrix.storage.identity_orm" not in inspect.getsource(identity)
    assert "inqtrix.storage.runs_orm" not in inspect.getsource(runs)
    assert "inqtrix.storage.knowledge_sessions_orm" not in inspect.getsource(
        knowledge_sessions
    )
    assert "inqtrix.storage.user_event_orm" not in inspect.getsource(user_events)

    identity_tables = identity._identity_metadata.tables
    assert {"groups", "group_members"}.issubset(identity_tables)
    assert "created_by_sub" in identity_tables["workspaces"].c
    assert "created_by_user_id" not in identity_tables["workspaces"].c
    assert "subject_type" in identity_tables["resource_shares"].c
    assert "accepted_at" not in identity_tables["resource_shares"].c
    assert "instance_role" not in identity_tables["users"].c
    assert "default_workspace_id" not in identity_tables["users"].c

    run_table = runs._runs_metadata.tables["runs"]
    assert "created_by_sub" in run_table.c
    assert "created_by_user_id" not in run_table.c
    assert "kind" not in run_table.c
    assert "ix_runs_tenant_created" in {index.name for index in run_table.indexes}

    session_tables = knowledge_sessions._knowledge_sessions_metadata.tables
    assert set(session_tables) == {"knowledge_sessions"}
    assert "created_by_sub" in session_tables["knowledge_sessions"].c
    assert "group_id" not in session_tables["knowledge_sessions"].c

    event_table = user_events._user_event_metadata.tables["user_events"]
    assert tuple(event_table.c.keys()) == (
        "id",
        "tenant_id",
        "target_user_id",
        "scope",
        "resource_type",
        "resource_id",
        "created_at",
    )
    assert "ix_user_events_tenant_target_id" in {
        index.name for index in event_table.indexes
    }


@pytest.mark.parametrize(
    ("revision_name", "metadata_name", "legacy_authority"),
    (
        ("0002_content_files", "content_metadata", {"files": "owner_sub"}),
        ("0004_auth_sessions", "auth_metadata", {"auth_sessions": "sub"}),
        (
            "0005_personal_access_tokens",
            "pat_metadata",
            {"personal_access_tokens": "owner_sub"},
        ),
        (
            "0006_prompt_templates",
            "prompt_template_metadata",
            {"prompt_templates": "owner_sub"},
        ),
        (
            "0007_quota",
            "quota_metadata",
            {
                "quota_usage_counters": "subject_sub",
                "quota_limits": "subject_sub",
            },
        ),
        (
            "0008_local_credentials",
            "credentials_metadata",
            {"local_credentials": "subject"},
        ),
        (
            "0010_knowledge",
            "knowledge_metadata",
            {"knowledge_collections": "created_by_sub"},
        ),
        (
            "0011_indexing_jobs",
            "indexing_metadata",
            {"indexing_jobs": "created_by_sub"},
        ),
        (
            "0013_chat_history",
            "chat_metadata",
            {
                "chat_thread_groups": "created_by_sub",
                "chat_threads": "created_by_sub",
            },
        ),
        (
            "0014_editor_persistence",
            "editor_metadata",
            {
                "editor_folders": "created_by_sub",
                "editor_documents": "created_by_sub",
            },
        ),
        (
            "0015_asset_records",
            "asset_metadata",
            {
                "asset_sections": "created_by_sub",
                "asset_groups": "created_by_sub",
                "asset_records": "created_by_sub",
            },
        ),
        (
            "0016_vector_index_records",
            "vector_index_metadata",
            {"vector_index_records": "created_by_sub"},
        ),
        (
            "0017_account_preferences",
            "account_metadata",
            {"account_preferences": "sub"},
        ),
        (
            "0030_agent_control",
            "agent_control_metadata",
            {
                "run_approvals": "decided_by_sub",
                "run_clarifications": "answered_by_sub",
            },
        ),
        (
            "0030_agent_control",
            "agent_sessions_metadata",
            {
                "agent_session_groups": "created_by_sub",
                "agent_sessions": "created_by_sub",
            },
        ),
        (
            "0031_editor_patches",
            "editor_patch_metadata",
            {"editor_patches": "created_by_sub"},
        ),
        (
            "0033_agent_memory",
            "agent_memory_metadata",
            {"agent_memory_candidates": "sub"},
        ),
        (
            "0034_agent_memory_feedback",
            "agent_memory_metadata",
            {"agent_feedback": "sub"},
        ),
        (
            "0041_skill_templates",
            "skill_metadata",
            {"skill_templates": "owner_sub"},
        ),
    ),
)
def test_legacy_authority_create_revisions_do_not_import_live_orms(
    revision_name: str,
    metadata_name: str,
    legacy_authority: dict[str, str],
) -> None:
    """Every pre-cutover table must still be born with its subject column."""
    migration = _migration(revision_name)
    source = inspect.getsource(migration)
    metadata = getattr(migration, metadata_name)

    assert "_orm import" not in source
    for table_name, legacy_column in legacy_authority.items():
        table = metadata.tables[table_name]
        assert legacy_column in table.c
        assert not any(
            column.name.endswith("_user_id") or column.name == "user_id"
            for column in table.c
        )


def test_later_legacy_revisions_run_once_before_uuid_cutover(monkeypatch) -> None:
    """0026 and 0037 must target the subject columns their revisions own."""
    groups = _migration("0026_knowledge_session_groups")
    active_runs = _migration("0037_runs_sub_active_index")
    group_sql: list[str] = []
    run_sql: list[str] = []
    monkeypatch.setattr(groups.op, "execute", group_sql.append)
    groups.upgrade()
    monkeypatch.setattr(active_runs.op, "execute", run_sql.append)
    active_runs.upgrade()

    statements = "\n".join(group_sql)
    assert "CREATE TABLE knowledge_session_groups" in statements
    assert "created_by_sub text NULL" in statements
    assert "ADD COLUMN group_id" in statements
    assert "IF NOT EXISTS knowledge_session_groups" not in statements
    assert "ON runs (created_by_sub, status)" in "\n".join(run_sql)


def test_0045_authority_inventory_is_complete_and_explicit() -> None:
    """Every real 0044 authority column has exactly one migration rule."""
    migration = _migration("0045_canonical_user_ids")
    actual = {
        (spec.table, spec.legacy, spec.canonical)
        for spec in migration._AUTHORITY_COLUMNS
    }
    expected = {
        ("workspaces", "created_by_sub", "created_by_user_id"),
        ("workspace_members", "sub", "user_id"),
        ("invitations", "invited_by_sub", "invited_by_user_id"),
        ("invitations", "accepted_by_sub", "accepted_by_user_id"),
        ("audit_log", "actor_sub", "actor_user_id"),
        ("account_preferences", "sub", "user_id"),
        ("quota_usage_counters", "subject_sub", "subject_user_id"),
        ("quota_limits", "subject_sub", "subject_user_id"),
        ("quota_limits", "set_by_sub", "set_by_user_id"),
        ("files", "owner_sub", "owner_user_id"),
        ("runs", "created_by_sub", "created_by_user_id"),
        ("indexing_jobs", "created_by_sub", "created_by_user_id"),
        (
            "knowledge_collections",
            "created_by_sub",
            "created_by_user_id",
        ),
        ("prompt_templates", "owner_sub", "owner_user_id"),
        ("skill_templates", "owner_sub", "owner_user_id"),
        ("chat_thread_groups", "created_by_sub", "created_by_user_id"),
        ("chat_threads", "created_by_sub", "created_by_user_id"),
        ("editor_folders", "created_by_sub", "created_by_user_id"),
        ("editor_documents", "created_by_sub", "created_by_user_id"),
        ("editor_patches", "created_by_sub", "created_by_user_id"),
        ("asset_sections", "created_by_sub", "created_by_user_id"),
        ("asset_groups", "created_by_sub", "created_by_user_id"),
        ("asset_records", "created_by_sub", "created_by_user_id"),
        (
            "knowledge_session_groups",
            "created_by_sub",
            "created_by_user_id",
        ),
        ("knowledge_sessions", "created_by_sub", "created_by_user_id"),
        ("agent_session_groups", "created_by_sub", "created_by_user_id"),
        ("agent_sessions", "created_by_sub", "created_by_user_id"),
        ("vector_index_records", "created_by_sub", "created_by_user_id"),
        ("agent_memory_candidates", "sub", "user_id"),
        ("agent_feedback", "sub", "user_id"),
        ("run_approvals", "decided_by_sub", "decided_by_user_id"),
        (
            "run_clarifications",
            "answered_by_sub",
            "answered_by_user_id",
        ),
    }

    assert actual == expected
    assert len(actual) == len(migration._AUTHORITY_COLUMNS)
    assert migration._EXACT_AUTHORITY_TABLES == (
        "auth_sessions",
        "personal_access_tokens",
        "local_credentials",
    )


def test_0045_installs_locked_direct_shares_and_restricting_user_fks(
    monkeypatch,
) -> None:
    """The emitted DDL pins the security and direct-share hard-cut contract."""
    migration = _migration("0045_canonical_user_ids")
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert statements.count("complete 0044 schema") == 2
    assert "LOCK TABLE" in statements
    assert "IN ACCESS EXCLUSIVE MODE" in statements
    assert "non-terminal runs" in statements
    assert "non-terminal reindex jobs" in statements
    assert "active group/file/comment/manage" in statements
    assert "no (tenant_id, issuer, subject) user match" in statements
    assert "more than one issuer-scoped user match" in statements
    assert "conflicting auth session legacy/canonical" in statements
    assert "conflicting PAT legacy/canonical" in statements

    assert "UPDATE resource_shares AS s\nSET revoked_at = now()" in statements
    for owner_column in (
        "runs AS r",
        "knowledge_collections AS r",
        "prompt_templates AS r",
        "skill_templates AS r",
        "r.created_by_user_id IS NULL",
        "r.owner_user_id IS NULL",
    ):
        assert owner_column in migration._REVOKE_ORPHAN_SHARES_SQL
    assert "DELETE FROM resource_shares" in statements
    assert statements.index("DELETE FROM resource_shares") < statements.index(
        "ALTER TABLE resource_shares ALTER COLUMN recipient_user_id SET NOT NULL"
    )
    assert statements.index("DELETE FROM resource_shares") < statements.index(
        "ALTER TABLE resource_shares ALTER COLUMN granted_by_user_id SET NOT NULL"
    )
    assert "DROP TABLE group_members" in statements
    assert "DROP TABLE groups" in statements
    assert "DROP COLUMN IF EXISTS subject_type" in statements
    assert "DROP COLUMN IF EXISTS subject_id" in statements
    assert "revision bigint NOT NULL DEFAULT 1" in statements
    assert "CHECK (permission IN ('view', 'edit'))" in statements
    for resource_type in (
        "run",
        "knowledge_collection",
        "prompt_template",
        "skill_template",
    ):
        assert f"'{resource_type}'" in statements
    assert "tenant_id, recipient_user_id, resource_type, resource_id" in statements
    assert "ADD CONSTRAINT ck_resource_shares_type" in statements

    for table, column in migration._FOREIGN_KEYS:
        constraint = f"fk_{table}_{column}_users"
        assert constraint in statements
    assert statements.count("REFERENCES users(id) ON DELETE RESTRICT") == len(
        migration._FOREIGN_KEYS
    )


def test_pure_tenant_lock_row_matches_0045_contract() -> None:
    """The ORM and migration expose no mutable state on the tenant lock row."""
    from inqtrix.storage.identity_orm import (
        resource_shares,
        tenant_security_state,
    )

    migration = _migration("0045_canonical_user_ids")
    assert tuple(tenant_security_state.c.keys()) == ("tenant_id",)
    assert "ck_resource_shares_type" in {
        constraint.name for constraint in resource_shares.constraints
    }
    assert any(
        "CREATE TABLE IF NOT EXISTS tenant_security_state "
        "(tenant_id text PRIMARY KEY)" == statement
        for statement in migration._TENANT_SECURITY_STATE_SQL
    )
    assert "pure one-column lock row" in migration._final_schema_verify_sql()


def test_v02_migration_chain_and_downgrades_are_irreversible() -> None:
    """The v0.2 migration chain is linear and preserves irreversible cuts."""
    script = ScriptDirectory.from_config(
        build_alembic_config("postgresql+asyncpg://example.invalid/inqtrix")
    )

    assert script.get_revision("0045_canonical_user_ids").down_revision == (
        "0044_agent_task_cancellation"
    )
    assert script.get_revision("0046_execution_authority").down_revision == (
        "0045_canonical_user_ids"
    )
    assert script.get_revision("0047_resource_sync").down_revision == (
        "0046_execution_authority"
    )
    assert script.get_revision("0048_editor_collaboration").down_revision == (
        "0047_resource_sync"
    )
    assert script.get_revision("0049_tenant_integrity").down_revision == (
        "0048_editor_collaboration"
    )
    assert script.get_revision("0050_context_archive").down_revision == (
        "0049_tenant_integrity"
    )
    assert script.get_revision("0051_editor_comments").down_revision == (
        "0050_context_archive"
    )
    assert script.get_revision("0052_editor_review").down_revision == (
        "0051_editor_comments"
    )
    assert script.get_revision("0053_editor_guest_links").down_revision == (
        "0052_editor_review"
    )
    assert script.get_revision("0054_guest_lease_sessions").down_revision == (
        "0053_editor_guest_links"
    )
    assert script.get_revision("0055_knowledge_revision").down_revision == (
        "0054_guest_lease_sessions"
    )
    assert script.get_revision("0056_run_runtime").down_revision == (
        "0055_knowledge_revision"
    )
    assert script.get_revision("0057_asset_deletion").down_revision == (
        "0056_run_runtime"
    )
    assert script.get_revision("0058_source_lifecycle").down_revision == (
        "0057_asset_deletion"
    )
    assert script.get_revision("0059_durable_upload").down_revision == (
        "0058_source_lifecycle"
    )
    assert script.get_revision("0060_knowledge_history").down_revision == (
        "0059_durable_upload"
    )
    assert script.get_revision("0061_indexing_operation_kinds").down_revision == (
        "0060_knowledge_history"
    )
    assert script.get_revision("0062_vector_index_deletion").down_revision == (
        "0061_indexing_operation_kinds"
    )
    assert script.get_revision("0063_durable_file_preparation").down_revision == (
        "0062_vector_index_deletion"
    )
    assert script.get_revision("0064_revision_job_idempotency").down_revision == (
        "0063_durable_file_preparation"
    )
    assert script.get_revision("0065_generation_cleanup_contract").down_revision == (
        "0064_revision_job_idempotency"
    )
    assert script.get_revision("0066_quota_stock_lifecycle").down_revision == (
        "0065_generation_cleanup_contract"
    )
    assert script.get_revision("0067_session_deletion_contract").down_revision == (
        "0066_quota_stock_lifecycle"
    )
    assert script.get_revision("0068_release_integrity").down_revision == (
        "0067_session_deletion_contract"
    )
    assert script.get_revision("0069_knowledge_source_scope").down_revision == (
        "0068_release_integrity"
    )
    assert script.get_revision("0070_contextualization_circuits").down_revision == (
        "0069_knowledge_source_scope"
    )
    assert script.get_revision("0071_asset_section_roles").down_revision == (
        "0070_contextualization_circuits"
    )
    assert script.get_revision("0072_audit_read_model").down_revision == (
        "0071_asset_section_roles"
    )
    assert script.get_revision("0073_llm_usage").down_revision == (
        "0072_audit_read_model"
    )
    assert script.get_revision("0074_llm_usage_run_index").down_revision == (
        "0073_llm_usage"
    )
    assert script.get_revision(
        "0075_audit_session_references"
    ).down_revision == ("0074_llm_usage_run_index")
    # Derived, not spelled: the head lives in migration_contract and a second
    # literal here would drift the moment a migration lands.
    assert script.get_current_head() == SCHEMA_HEAD_REVISION

    for name in (
        "0045_canonical_user_ids",
        "0046_execution_authority",
        "0047_resource_sync_and_reindex",
    ):
        with pytest.raises(RuntimeError, match="irreversible"):
            _migration(name).downgrade()


def test_durable_upload_migration_refuses_ambiguous_data_and_lossy_rollback() -> None:
    migration = _migration("0059_durable_upload")
    source = inspect.getsource(migration)

    assert "duplicate_bindings" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "dangling_bindings" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "tenant_mismatches" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "owner_mismatches" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "workspace_mismatches" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "Resolve the exact" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "rows explicitly" in migration._ASSET_FILE_BINDING_PREFLIGHT_SQL
    assert "UPDATE asset_records SET upload_status = 'failed'" not in source
    assert "Durable upload downgrade blocked" in source
