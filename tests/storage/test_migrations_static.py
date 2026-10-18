"""Static checks for Alembic migration graph constraints."""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from inqtrix.storage.migrate import build_alembic_config
from inqtrix.storage.migration_contract import (
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_TABLES,
    schema_head_revision,
)


def test_knowledge_history_backfill_is_set_based_and_span_qualified(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0060_knowledge_history"
    )

    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration._backfill_verified_legacy_hashes()

    assert len(executed) == 2
    statements = "\n".join(executed)
    assert "sha256(convert_to(document.text, 'UTF8'))" in statements
    assert "UPDATE knowledge_document_revisions AS revision" in statements
    assert "UPDATE knowledge_chunks AS chunk" in statements
    assert "source_start IS NOT NULL" in statements
    assert "source_end IS NOT NULL" in statements
    assert "SELECT tenant_id, id, active_revision_id, text" not in statements


def test_audit_session_sanitization_is_scoped_and_irreversible(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0075_audit_session_references"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    assert len(executed) == 3
    assert executed[0] == "LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE"
    sanitize = executed[1]
    postcondition = executed[2]
    assert "UPDATE audit_log" in sanitize
    assert "action = 'auth.logout'" in sanitize
    assert "resource_type = 'session'" in sanitize
    assert "resource_id !~ '^ses_[0-9a-f]{16}$'" in sanitize
    assert "inqtrix.audit.session.v1:" in sanitize
    assert "sha256(" in sanitize
    assert "RETURNING" not in sanitize
    assert "SELECT resource_id" not in sanitize
    assert "unsafe_count" in postcondition
    assert "resource_id !~ '^ses_[0-9a-f]{16}$'" in postcondition

    with pytest.raises(RuntimeError, match="irreversible.*Restore"):
        migration.downgrade()


def test_private_suggestion_draft_migration_is_nested_and_irreversible() -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0076_editor_private_suggestion_drafts"
    )
    source = inspect.getsource(migration.upgrade)

    assert '"suggestion_draft"' in source
    assert "postgresql.JSONB" in source
    assert "jsonb_typeof(suggestion_draft) = 'object'" in source
    assert "ux_editor_comments_private_draft_patch" in source
    assert "postgresql_where" in source
    with pytest.raises(RuntimeError, match="Restore"):
        migration.downgrade()


def test_release_integrity_migration_is_fail_closed_and_tenant_scoped() -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0068_release_integrity"
    )

    source_sql = migration._SOURCE_RECONCILIATION_SQL
    assert "count(DISTINCT candidate.asset_id)" in source_sql
    assert "resolved_hint_count" in source_sql
    assert "hint_count" in source_sql
    assert "collection_asset_claims" in source_sql
    assert "asset.tenant_id = document.tenant_id" in source_sql
    assert "unique_server_files" in source_sql

    preflight = migration._TENANT_REFERENCE_PREFLIGHT_SQL
    assert "cross-tenant ledger relationship" in preflight
    constraints = "\n".join(migration._TENANT_CONSTRAINTS)
    for relationship in (
        "fk_deletion_operation_assets_tenant_operation",
        "fk_deletion_operation_events_tenant_operation",
        "fk_upload_operations_tenant_asset",
        "fk_upload_operation_events_tenant_operation",
        "fk_upload_operation_outbox_tenant_operation",
        "fk_knowledge_revisions_tenant_document",
        "fk_knowledge_generations_tenant_collection",
    ):
        assert relationship in constraints
    assert "FOREIGN KEY (tenant_id" in constraints
    assert "FULL OUTER JOIN" in migration._QUOTA_POSTCONDITION_SQL

    quota_migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0066_quota_stock_lifecycle"
    )
    quota_source = inspect.getsource(quota_migration.upgrade)
    assert "op.get_bind().exec_driver_sql" in quota_source
    counter_rebuild = quota_source.index("INSERT INTO quota_usage_counters")
    postcondition = quota_source.index("op.execute(_QUOTA_POSTCONDITION_SQL)")
    force_rls = quota_source.rindex("FORCE ROW LEVEL SECURITY")
    assert counter_rebuild < postcondition < force_rls


def test_irreversible_lifecycle_downgrades_never_delete_or_relabel() -> None:
    revisions = (
        "0057_asset_deletion_operations",
        "0058_source_lifecycle",
        "0060_knowledge_history",
        "0062_vector_index_deletion",
        "0065_generation_cleanup_contract",
        "0066_quota_stock_lifecycle",
        "0067_session_deletion_contract",
        "0068_release_integrity",
        "0069_knowledge_source_scope",
        "0070_contextualization_circuits",
        "0071_asset_section_roles",
        "0095_user_erasure_operation",
        "0096_orphaned_source_deletion",
    )
    for revision in revisions:
        migration = importlib.import_module(
            f"inqtrix.storage.migrations.versions.{revision}"
        )
        with pytest.raises(RuntimeError, match="Restore"):
            migration.downgrade()


def test_alembic_revision_ids_fit_default_version_table() -> None:
    """Alembic's default version table stores revision ids in varchar(32)."""
    script = ScriptDirectory.from_config(
        build_alembic_config("postgresql+asyncpg://example.invalid/inqtrix")
    )

    too_long = [
        revision.revision
        for revision in script.walk_revisions()
        if len(revision.revision) > 32
    ]

    assert too_long == []


def test_asset_section_roles_are_additive_and_never_title_backfilled() -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0071_asset_section_roles"
    )
    source = inspect.getsource(migration.upgrade)

    assert "semantic_role" in source
    assert "postgresql_nulls_not_distinct=True" in source
    assert "Bibliothek" not in source
    assert "Projekt-Quellen" not in source
    assert "UPDATE asset_sections" not in source
    assert "semantic_role IN ('temporary', 'library', 'project_sources')" in (
        migration.PREPARED_ROLE_PREDICATE
    )


def test_managed_migration_contract_rejects_offline_sql_and_bounds_only_locks() -> None:
    storage_path = Path(__file__).parents[2] / "src" / "inqtrix" / "storage"
    env_source = (storage_path / "migrations" / "env.py").read_text(
        encoding="utf-8"
    )
    assert "Alembic offline SQL is unsupported" in env_source
    assert "literal_binds=True" not in env_source

    from inqtrix.storage import migrate

    runner_source = inspect.getsource(migrate._run_schema_migrations)
    assert "SET LOCAL lock_timeout" in runner_source
    assert "statement_timeout" not in runner_source
    assert "no schema transition" in runner_source
    assert 'strategy == "owner" and transition' in runner_source


def test_revisions_0048_through_0068_publish_force_rls_after_global_work() -> None:
    """Tenant-wide data/FK work must finish before a new table becomes forced."""
    versions = (
        Path(__file__).parents[2]
        / "src"
        / "inqtrix"
        / "storage"
        / "migrations"
        / "versions"
    )
    revision_files = [
        path
        for path in versions.glob("00*.py")
        if 48 <= int(path.name[:4]) <= 68
    ]
    forced_revisions = {
        path.name[:4]
        for path in revision_files
        if "FORCE ROW LEVEL SECURITY" in path.read_text(encoding="utf-8")
    }
    assert forced_revisions == {
        "0048",
        "0051",
        "0053",
        "0057",
        "0058",
        "0059",
        "0060",
        "0066",
    }

    revision_0048 = importlib.import_module(
        "inqtrix.storage.migrations.versions.0048_editor_collaboration"
    )
    assert inspect.getsource(revision_0048.upgrade).rstrip().endswith(
        "_end_owner_rls_maintenance(_OWNER_MAINTENANCE_TABLES)"
    )

    revision_0051 = importlib.import_module(
        "inqtrix.storage.migrations.versions.0051_editor_collaboration_comments"
    )
    source_0051 = inspect.getsource(revision_0051.upgrade)
    assert source_0051.rfind(
        "ALTER TABLE editor_documents FORCE ROW LEVEL SECURITY"
    ) > source_0051.rfind("op.create_foreign_key")

    revision_0053 = importlib.import_module(
        "inqtrix.storage.migrations.versions.0053_editor_guest_links"
    )
    source_0053 = inspect.getsource(revision_0053.upgrade)
    assert source_0053.rfind("for table in TABLES") > source_0053.rfind(
        "op.create_foreign_key"
    )

    terminal_security_markers = {
        "0057_asset_deletion_operations": "op.create_index",
        "0058_source_lifecycle": "INSERT INTO source_lifecycles",
        "0059_durable_upload": "op.create_index",
        "0060_knowledge_history": "_backfill_verified_legacy_hashes()",
    }
    for revision, global_work_marker in terminal_security_markers.items():
        migration = importlib.import_module(
            f"inqtrix.storage.migrations.versions.{revision}"
        )
        upgrade_source = inspect.getsource(migration.upgrade)
        assert upgrade_source.rfind("FORCE ROW LEVEL SECURITY") > (
            upgrade_source.rfind(global_work_marker)
        )

    revision_0066 = importlib.import_module(
        "inqtrix.storage.migrations.versions.0066_quota_stock_lifecycle"
    )
    source_0066 = inspect.getsource(revision_0066.upgrade)
    assert source_0066.index("op.execute(_QUOTA_POSTCONDITION_SQL)") < (
        source_0066.rindex("FORCE ROW LEVEL SECURITY")
    )


def test_tenant_integrity_is_the_single_migration_head() -> None:
    script = ScriptDirectory.from_config(
        build_alembic_config("postgresql+asyncpg://example.invalid/inqtrix")
    )

    assert script.get_current_head() == SCHEMA_HEAD_REVISION
    assert schema_head_revision() == SCHEMA_HEAD_REVISION


def test_execution_authority_migration_installs_import_and_reindex_contracts(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0046_execution_authority"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert "IF NOT EXISTS" not in statements
    assert "source_run_id" in statements
    assert "uq_runs_import_owner_source" in statements
    assert "execution_actor_user_id uuid" in statements
    assert "execution_scopes json NOT NULL DEFAULT '[]'::json" in statements
    assert "REFERENCES users(id) ON DELETE RESTRICT" in statements
    assert "'queued', 'running', 'cancelling'" in statements


def test_agent_task_cancellation_migration_keeps_one_task_status_constraint(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0044_agent_task_cancellation"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    assert any("DROP CONSTRAINT" in sql for sql in executed)
    assert any("cancel_requested" in sql and "cancelled" in sql for sql in executed)


def test_agent_task_migration_backfills_legacy_plan_approval_subjects(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0043_agent_task_execution_contract"
    )

    statement = migration._APPROVAL_SUBJECT_BACKFILL_SQL
    assert "UPDATE run_approvals AS approval" in statement
    assert "approval.status = 'pending'" not in statement
    assert "approval.payload->>'plan_version'" in statement
    assert "approval.run_id = plan.run_id" in statement
    assert "approval.tenant_id = plan.tenant_id" in statement
    assert "plan.version = CASE" in statement
    budget_statement = migration._LEGACY_CHILD_BUDGET_BACKFILL_SQL
    assert "kind = 'agent_child'" in budget_statement
    assert "- 'token_budget'" in budget_statement
    assert "waiting_for_children" in budget_statement
    root_statement = migration._RUN_ROOT_LINEAGE_BACKFILL_SQL
    assert "WITH RECURSIVE run_tree" in root_statement
    assert "child.tenant_id = run_tree.tenant_id" in root_statement
    assert "target.tenant_id = run_tree.tenant_id" in root_statement
    assert "SET root_run_id = run_tree.canonical_root" in root_statement
    executed: list[str] = []
    monkeypatch.setattr(
        migration.op,
        "execute",
        executed.append,
    )

    migration.upgrade()

    assert executed[0] == migration._TENANT_REFERENCE_PREFLIGHT_SQL
    assert executed[1] == statement
    assert executed[2] == migration._APPROVAL_SUBJECT_POSTCHECK_SQL
    assert budget_statement in executed
    assert root_statement in executed
    assert any("result_payload JSON" in sql for sql in executed)
    assert any("insufficient_evidence" in sql for sql in executed)


def test_tenant_integrity_migration_repairs_and_enforces_scoped_references(
    monkeypatch,
) -> None:
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions.0049_tenant_integrity"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    assert set(migration._TENANT_RLS_TABLES).issubset(TENANT_RLS_TABLES)
    assert set(TENANT_RLS_TABLES) - set(migration._TENANT_RLS_TABLES) == {
        "deletion_operation_assets",
        "deletion_operation_events",
        "quota_usage_adjustments",
        "quota_stock_lifecycles",
        "deletion_operations",
        "source_lifecycles",
        "upload_operation_events",
        "upload_operation_outbox",
        "upload_operations",
        "editor_collaboration_comment_messages",
        "editor_collaboration_comment_reads",
        "editor_collaboration_comment_threads",
        "editor_document_guest_identities",
        "editor_document_share_links",
        "contextualization_provider_circuits",
        "llm_usage",
        "knowledge_document_revisions",
        "knowledge_index_generations",
        "user_authorization_generations",
        "prompt_template_seed_markers",
    }
    assert executed[:4] == [
        migration._TENANT_REFERENCE_PREFLIGHT_SQL,
        migration._APPROVAL_SUBJECT_BACKFILL_SQL,
        migration._APPROVAL_SUBJECT_POSTCHECK_SQL,
        migration._RUN_ROOT_LINEAGE_BACKFILL_SQL,
    ]
    statements = "\n".join(executed)
    assert "approval.tenant_id = plan.tenant_id" in statements
    assert "plan.version = CASE" in statements
    assert "plan.plan_id IS NULL" in statements
    assert "child.tenant_id = run_tree.tenant_id" in statements
    assert "UNIQUE (tenant_id, run_id)" in statements
    assert "FOREIGN KEY (tenant_id, parent_run_id)" in statements
    assert "UNIQUE (tenant_id, plan_id, run_id)" in statements
    assert "FOREIGN KEY (tenant_id, plan_id, run_id)" in statements
    assert "FOREIGN KEY (tenant_id, child_run_id)" in statements
    assert "ON DELETE SET NULL (child_run_id)" in statements
    assert "FOREIGN KEY (tenant_id, artifact_id)" in statements
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE alembic_version "
        "FROM PUBLIC, inqtrix_app" in statements
    )
    assert (
        "REVOKE ALL PRIVILEGES (version_num) ON TABLE alembic_version "
        "FROM PUBLIC, inqtrix_app" in statements
    )
    assert "GRANT SELECT ON TABLE alembic_version TO inqtrix_app" in statements
    assert "FROM PUBLIC, inqtrix_app" in statements
    assert "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id()" in statements
    assert "GRANT USAGE ON SEQUENCE audit_log_id_seq" in statements
    assert "user_events_id_seq" in statements
    assert (
        "REVOKE ALL PRIVILEGES ON SEQUENCE audit_log_id_seq, "
        "user_events_id_seq FROM PUBLIC, inqtrix_app" in statements
    )
    for table_name in migration._TENANT_RLS_TABLES:
        assert (
            f"REVOKE ALL PRIVILEGES ON TABLE {table_name} " "FROM PUBLIC, inqtrix_app"
        ) in statements
        expected_grant = (
            "SELECT, INSERT"
            if table_name == "audit_log"
            else "SELECT, INSERT, UPDATE, DELETE"
        )
        assert (
            f"GRANT {expected_grant} ON TABLE {table_name} TO inqtrix_app" in statements
        )

    executed.clear()
    migration.downgrade()
    downgrade_statements = "\n".join(executed)
    assert (
        "REVOKE EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
        "FROM inqtrix_app" in downgrade_statements
    )
    assert (
        "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id() TO PUBLIC"
        in downgrade_statements
    )


def test_head_tenant_rls_inventory_is_sorted_and_complete() -> None:
    assert TENANT_RLS_TABLES == tuple(sorted(TENANT_RLS_TABLES))
    assert len(TENANT_RLS_TABLES) == len(set(TENANT_RLS_TABLES))
    for expected in (
        "runs",
        "run_approvals",
        "editor_collaboration_updates",
        "editor_document_guest_identities",
        "editor_document_share_links",
        "tenant_security_state",
    ):
        assert expected in TENANT_RLS_TABLES

    storage_path = Path(__file__).parents[2] / "src" / "inqtrix" / "storage"
    orm_tables: set[str] = set()
    for module_path in storage_path.glob("*_orm.py"):
        module = importlib.import_module(f"inqtrix.storage.{module_path.stem}")
        for value in vars(module).values():
            if not isinstance(value, sa.MetaData):
                continue
            orm_tables.update(
                table.name for table in value.tables.values() if "tenant_id" in table.c
            )
    assert orm_tables == set(TENANT_RLS_TABLES)


def test_shared_comment_migration_is_additive_and_fail_closed(
    monkeypatch,
) -> None:
    """The comments revision creates separate RLS tables and one doc cursor."""
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0051_editor_collaboration_comments"
    )
    executed: list[str] = []
    created_tables: list[str] = []
    added_columns: list[tuple[str, str]] = []
    foreign_keys: list[
        tuple[str, str, str, tuple[str, ...], tuple[str, ...], str | None]
    ] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda table, *args, **kwargs: created_tables.append(table),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        migration.op,
        "create_foreign_key",
        lambda name, source, referent, local, remote, **kwargs: (
            foreign_keys.append(
                (
                    name,
                    source,
                    referent,
                    tuple(local),
                    tuple(remote),
                    kwargs.get("ondelete"),
                )
            )
        ),
    )

    migration.upgrade()

    assert added_columns == [("editor_documents", "collaboration_comment_revision")]
    assert created_tables == list(migration.TABLES)
    user_foreign_keys = [key for key in foreign_keys if key[2] == "users"]
    assert user_foreign_keys == [
        (
            "fk_collaboration_comment_threads_creator",
            "editor_collaboration_comment_threads",
            "users",
            ("created_by_user_id",),
            ("id",),
            "RESTRICT",
        ),
        (
            "fk_collaboration_comment_threads_resolver",
            "editor_collaboration_comment_threads",
            "users",
            ("resolved_by_user_id",),
            ("id",),
            "RESTRICT",
        ),
        (
            "fk_collaboration_comment_messages_author",
            "editor_collaboration_comment_messages",
            "users",
            ("author_user_id",),
            ("id",),
            "RESTRICT",
        ),
        (
            "fk_collaboration_comment_reads_user",
            "editor_collaboration_comment_reads",
            "users",
            ("user_id",),
            ("id",),
            "RESTRICT",
        ),
    ]
    statements = "\n".join(executed)
    for table in migration.TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements
        assert f"CREATE POLICY tenant_isolation ON {table}" in statements
        assert (
            f"REVOKE ALL PRIVILEGES ON TABLE {table} " "FROM PUBLIC, inqtrix_app"
        ) in statements


def test_review_semantics_migration_is_additive_and_schema_scoped(
    monkeypatch,
) -> None:
    """Review summaries are additive and only known schema rows are upgraded."""
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0052_editor_review_semantics"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert "ADD COLUMN change_summary jsonb NOT NULL" in statements
    assert "jsonb_build_object(" in statements
    assert '"omitted_edit_count":0' not in statements
    assert "ADD COLUMN decision_outcome text NULL" in statements
    assert "jsonb_typeof(change_summary) = 'object'" in statements
    assert "decision_outcome IN ('accepted', 'rejected')" in statements
    assert "UPDATE editor_documents" in statements
    assert "UPDATE editor_collaboration_snapshots" in statements
    assert "collaboration_schema_version = 1" in statements
    assert f"collaboration_schema_hash = '{migration._OLD_SCHEMA_HASH}'" in statements
    assert f"collaboration_schema_hash = '{migration._NEW_SCHEMA_HASH}'" in statements

    executed.clear()
    migration.downgrade()
    downgrade = "\n".join(executed)
    assert "DROP COLUMN decision_outcome" in downgrade
    assert "DROP COLUMN change_summary" in downgrade
    assert "SET collaboration_schema_version = 1" in downgrade


def test_markdown_read_migration_restamps_both_hash_columns(monkeypatch) -> None:
    """Der Umstempel-Schritt muss beide Traeger des Abdrucks erreichen.

    Der Abdruck steht auf zwei Spalten -- ``editor_documents`` und
    ``editor_collaboration_snapshots`` --, und der Ladepfad prueft beide
    gegeneinander. Bliebe eine zuruck, waere das Dokument nach dem Cutover
    unlesbar, waehrend die Migration Erfolg meldet.
    """
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0081_editor_markdown_read"
    )
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert "UPDATE editor_documents" in statements
    assert "UPDATE editor_collaboration_snapshots" in statements
    assert migration._NEW_SCHEMA_HASH in statements
    assert migration._OLD_SCHEMA_HASH in statements
    # Der Rueckweg muss beide Spalten ebenso erreichen, sonst ist ein
    # Downgrade eine Einbahnstrasse mit halbem Bestand.
    executed.clear()
    migration.downgrade()
    rueckweg = "\n".join(executed)
    assert "UPDATE editor_documents" in rueckweg
    assert "UPDATE editor_collaboration_snapshots" in rueckweg


def test_validation_rules_migration_restamps_both_hash_columns(monkeypatch) -> None:
    """The re-stamp reaches both carriers of the fingerprint, in both directions.

    The fingerprint is stored on ``editor_documents`` and on
    ``editor_collaboration_snapshots``, and loading compares them. It also
    continues exactly where the previous re-stamp ended, so no document is
    left on a fingerprint no migration moves.
    """
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0090_editor_validation_rules"
    )
    previous = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0081_editor_markdown_read"
    )
    assert migration._OLD_SCHEMA_HASH == previous._NEW_SCHEMA_HASH
    assert migration._NEW_SCHEMA_HASH != migration._OLD_SCHEMA_HASH
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert "UPDATE editor_documents" in statements
    assert "UPDATE editor_collaboration_snapshots" in statements
    assert f"SET collaboration_schema_hash = '{migration._NEW_SCHEMA_HASH}'" in statements
    assert f"AND collaboration_schema_hash = '{migration._OLD_SCHEMA_HASH}'" in statements
    assert f"SET schema_hash = '{migration._NEW_SCHEMA_HASH}'" in statements
    assert f"WHERE schema_hash = '{migration._OLD_SCHEMA_HASH}'" in statements
    executed.clear()
    migration.downgrade()
    downgrade = "\n".join(executed)
    assert "UPDATE editor_documents" in downgrade
    assert "UPDATE editor_collaboration_snapshots" in downgrade
    assert f"SET collaboration_schema_hash = '{migration._OLD_SCHEMA_HASH}'" in downgrade
    assert f"AND collaboration_schema_hash = '{migration._NEW_SCHEMA_HASH}'" in downgrade
    assert f"SET schema_hash = '{migration._OLD_SCHEMA_HASH}'" in downgrade
    assert f"WHERE schema_hash = '{migration._NEW_SCHEMA_HASH}'" in downgrade


def test_rejected_by_edit_migration_adds_one_bounded_column(monkeypatch) -> None:
    """The update row names the suggestions its edit removed, and only a
    plain human or guest edit may name any.

    The constraint is the database's half of the rule: a decision decides
    through its patches, and an assistant, agent or system row holds no
    edit lease, so none of them may carry a removal by edit.
    """
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0091_rejected_by_edit"
    )
    assert migration.down_revision == "0090_editor_validation_rules"
    added: list[tuple[str, sa.Column]] = []
    checks: dict[str, tuple[str, str]] = {}
    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration.op, "add_column", lambda table, column: added.append((table, column))
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.__setitem__(name, (table, condition)),
    )

    migration.upgrade()

    assert [(table, column.name) for table, column in added] == [
        ("editor_collaboration_updates", "rejected_suggestion_ids")
    ]
    column = added[0][1]
    assert column.nullable is False
    assert str(column.server_default.arg) == "'[]'::jsonb"
    assert checks["ck_collaboration_updates_rejected_suggestion_ids"] == (
        "editor_collaboration_updates",
        "jsonb_typeof(rejected_suggestion_ids) = 'array'",
    )
    table, condition = checks["ck_collaboration_updates_rejected_by_edit"]
    assert table == "editor_collaboration_updates"
    assert "rejected_suggestion_ids = '[]'::jsonb OR (" in condition
    assert "decision_outcome IS NULL" in condition
    assert "change_kind IN ('direct', 'suggestion')" in condition
    assert "actor_kind IN ('human', 'guest')" in condition

    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, type_: dropped.append((name, type_)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, name: dropped.append((name, "column")),
    )
    migration.downgrade()
    assert dropped == [
        ("ck_collaboration_updates_rejected_by_edit", "check"),
        ("ck_collaboration_updates_rejected_suggestion_ids", "check"),
        ("rejected_suggestion_ids", "column"),
    ]


def test_incognito_runs_migration_adds_one_flag_and_its_sweep_index(monkeypatch) -> None:
    """Every existing run is an ordinary run; the retention sweep finds the
    few incognito rows through a partial index instead of scanning every
    terminal run on each cleanup."""
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0094_incognito_runs"
    )
    assert migration.down_revision == "0093_petrol_theme"
    added: list[tuple[str, sa.Column]] = []
    indexes: list[tuple[str, str, list[str], str]] = []
    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration.op, "add_column", lambda table, column: added.append((table, column))
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, columns, postgresql_where: indexes.append(
            (name, table, columns, str(postgresql_where))
        ),
    )

    migration.upgrade()

    assert [(table, column.name) for table, column in added] == [("runs", "incognito")]
    column = added[0][1]
    assert isinstance(column.type, sa.Boolean)
    assert column.nullable is False
    assert str(column.server_default.arg) == "false"
    assert indexes == [("ix_runs_incognito_finished", "runs", ["finished_at"], "incognito")]

    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, table_name: dropped.append((name, "index")),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, name: dropped.append((name, "column")),
    )
    migration.downgrade()
    assert dropped == [("ix_runs_incognito_finished", "index"), ("incognito", "column")]


def test_library_upgrade_migration_restamps_both_hash_columns(monkeypatch) -> None:
    """The re-stamp after the editor libraries moved reaches both carriers of
    the fingerprint, in both directions, and continues exactly where the
    previous re-stamp ended, so no document is left on a fingerprint no
    migration moves.
    """
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0092_editor_library_upgrade"
    )
    previous = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0090_editor_validation_rules"
    )
    assert migration.down_revision == "0091_rejected_by_edit"
    assert migration._OLD_SCHEMA_HASH == previous._NEW_SCHEMA_HASH
    assert migration._NEW_SCHEMA_HASH != migration._OLD_SCHEMA_HASH
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    statements = "\n".join(executed)
    assert "UPDATE editor_documents" in statements
    assert "UPDATE editor_collaboration_snapshots" in statements
    assert f"SET collaboration_schema_hash = '{migration._NEW_SCHEMA_HASH}'" in statements
    assert f"AND collaboration_schema_hash = '{migration._OLD_SCHEMA_HASH}'" in statements
    assert f"SET schema_hash = '{migration._NEW_SCHEMA_HASH}'" in statements
    assert f"WHERE schema_hash = '{migration._OLD_SCHEMA_HASH}'" in statements
    executed.clear()
    migration.downgrade()
    downgrade = "\n".join(executed)
    assert "UPDATE editor_documents" in downgrade
    assert "UPDATE editor_collaboration_snapshots" in downgrade
    assert f"SET collaboration_schema_hash = '{migration._OLD_SCHEMA_HASH}'" in downgrade
    assert f"AND collaboration_schema_hash = '{migration._NEW_SCHEMA_HASH}'" in downgrade
    assert f"SET schema_hash = '{migration._OLD_SCHEMA_HASH}'" in downgrade
    assert f"WHERE schema_hash = '{migration._NEW_SCHEMA_HASH}'" in downgrade


def test_latest_restamp_migration_hash_matches_the_built_fingerprint() -> None:
    """The hand-written target hash is the one the code actually builds.

    The value is computed in TypeScript (``getEditorSchemaFingerprint``) and
    written down by hand in Python. A single swapped hex digit would make
    every stored document unreadable: the migration would re-stamp every row
    successfully and the break would only show when a document is opened.
    """
    import json
    import subprocess

    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0092_editor_library_upgrade"
    )
    root = Path(__file__).resolve().parents[2]
    script = (
        "import('./packages/editor-schema/dist/index.js')"
        ".then(async (m) => console.log(await m.getEditorSchemaFingerprint()))"
    )
    built_module = root / "packages/editor-schema/dist/index.js"
    if not built_module.exists():
        pytest.skip(
            "packages/editor-schema is not built; the comparison needs the "
            "built package (npm run build in the package)."
        )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        cwd=root,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    built_fingerprint = result.stdout.strip()

    assert built_fingerprint == migration._NEW_SCHEMA_HASH, (
        "The fingerprint the code builds and the migration's target differ: "
        f"code {built_fingerprint}, migration {migration._NEW_SCHEMA_HASH}. Either the "
        "schema changed again without a new re-stamp migration, or the value "
        "is mistyped."
    )
    assert json.dumps(built_fingerprint)  # The value is a plain string.


def test_orphaned_source_migration_adds_one_target_kind_and_keeps_the_others(
    monkeypatch,
) -> None:
    """A source that outlived its aggregate is deleted as an operation of its
    own kind; every kind that existed before stays accepted."""
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0096_orphaned_source_deletion"
    )
    assert migration.down_revision == "0095_user_erasure_operation"
    dropped: list[tuple[str, str, str]] = []
    created: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, type_: dropped.append((name, table, type_)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: created.setdefault(name, (table, condition)),
    )

    migration.upgrade()

    assert dropped == [
        ("ck_deletion_operations_target_kind", "deletion_operations", "check")
    ]

    def values(condition: str) -> set[str]:
        return set(re.findall(r"'([a-z_]+)'", condition))

    previous = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0095_user_erasure_operation"
    )
    kinds = values(created["ck_deletion_operations_target_kind"][1])
    assert kinds == values(previous._KINDS_BEFORE) | {"user", "source"}
    assert values(migration._KINDS_BEFORE) == values(previous._KINDS_BEFORE) | {"user"}


def test_user_erasure_migration_marks_accounts_and_extends_the_deletion_vocabulary(
    monkeypatch,
) -> None:
    """An account erasure is a deletion operation of its own kind with two
    checkpoints of its own; the account row names the operation erasing it.
    Every kind and stage that existed before stays accepted."""
    migration = importlib.import_module(
        "inqtrix.storage.migrations.versions." "0095_user_erasure_operation"
    )
    assert migration.down_revision == "0094_incognito_runs"
    added: list[tuple[str, sa.Column]] = []
    dropped: list[tuple[str, str, str]] = []
    created: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(
        migration.op, "add_column", lambda table, column: added.append((table, column))
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, type_: dropped.append((name, table, type_)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: created.setdefault(name, (table, condition)),
    )

    migration.upgrade()

    assert [(table, column.name) for table, column in added] == [
        ("users", "erasure_operation_id")
    ]
    assert isinstance(added[0][1].type, sa.Text)
    assert added[0][1].nullable is True
    assert sorted(dropped) == [
        ("ck_deletion_operations_stage", "deletion_operations", "check"),
        ("ck_deletion_operations_target_kind", "deletion_operations", "check"),
    ]

    def values(condition: str) -> set[str]:
        return set(re.findall(r"'([a-z_]+)'", condition))

    kinds = values(created["ck_deletion_operations_target_kind"][1])
    stages = values(created["ck_deletion_operations_stage"][1])
    assert kinds == values(migration._KINDS_BEFORE) | {"user"}
    assert stages == values(migration._STAGES_BEFORE) | {
        "aggregates_removed",
        "rows_removed",
    }
