"""Static contracts for the editor-collaboration migration."""

from __future__ import annotations

import inspect
import re
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from inqtrix.storage.migrate import build_alembic_config
from inqtrix.storage.editor_collaboration_orm import (
    editor_collaboration_instances,
    editor_collaboration_leases,
    editor_collaboration_snapshots,
    editor_collaboration_updates,
)


def _migration():
    """Import the packaged collaboration revision."""
    return import_module(
        "inqtrix.storage.migrations.versions.0048_editor_collaboration"
    )


def _constraint_names(table: sa.Table) -> set[str]:
    """Return all explicitly named constraints on one frozen table."""
    return {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    }


def test_revision_is_frozen_and_extends_resource_sync() -> None:
    """The revision must be self-contained and fit Alembic's version field."""
    migration = _migration()
    source = inspect.getsource(migration)

    assert migration.revision == "0048_editor_collaboration"
    assert migration.down_revision == "0047_resource_sync"
    assert len(migration.revision) <= 32
    assert "from inqtrix" not in source
    assert "import inqtrix" not in source
    assert "_orm import" not in source

    script = ScriptDirectory.from_config(
        build_alembic_config("postgresql+asyncpg://example.invalid/inqtrix")
    )
    assert script.get_revision("0049_tenant_integrity").down_revision == (
        migration.revision
    )


def test_frozen_tables_encode_binary_authority_and_fencing() -> None:
    """The local metadata must carry the durable storage invariants."""
    migration = _migration()
    tables = migration._collaboration_metadata.tables

    assert set(tables) == {
        "editor_collaboration_updates",
        "editor_collaboration_snapshots",
        "editor_collaboration_leases",
        "editor_collaboration_instances",
    }
    assert all("tenant_id" in table.c for table in tables.values())

    updates = tables["editor_collaboration_updates"]
    assert tuple(column.name for column in updates.primary_key.columns) == (
        "document_id",
        "generation",
        "sequence",
    )
    assert isinstance(updates.c.update_bytes.type, sa.LargeBinary)
    assert updates.c.update_bytes.nullable is True
    assert isinstance(updates.c.suggestion_ids.type, postgresql.JSONB)
    assert isinstance(updates.c.command_id.type, postgresql.UUID)
    assert {
        "pk_editor_collaboration_updates",
        "ck_collaboration_updates_position",
        "ck_collaboration_updates_actor_kind",
        "ck_collaboration_updates_change_kind",
        "ck_collaboration_updates_human_actor",
        "ck_collaboration_updates_payload",
        "uq_collaboration_updates_document_hash",
    }.issubset(_constraint_names(updates))
    command_index = next(
        index
        for index in updates.indexes
        if index.name == "uq_collaboration_updates_command"
    )
    assert command_index.unique is True
    assert tuple(column.name for column in command_index.columns) == (
        "command_id",
    )
    assert str(command_index.dialect_options["postgresql"]["where"]) == (
        "command_id IS NOT NULL"
    )
    assert "ix_collaboration_updates_actor_user" in {
        index.name for index in updates.indexes
    }

    snapshots = tables["editor_collaboration_snapshots"]
    assert tuple(column.name for column in snapshots.primary_key.columns) == (
        "document_id",
        "generation",
        "covered_sequence",
    )
    assert isinstance(snapshots.c.state_update.type, sa.LargeBinary)
    assert isinstance(snapshots.c.state_vector.type, sa.LargeBinary)
    assert snapshots.c.state_update.nullable is False
    assert snapshots.c.schema_hash.nullable is False

    leases = tables["editor_collaboration_leases"]
    assert tuple(column.name for column in leases.primary_key.columns) == (
        "lease_id",
    )
    assert isinstance(leases.c.lease_id.type, postgresql.UUID)
    assert isinstance(leases.c.user_id.type, postgresql.UUID)
    assert leases.c.session_id.nullable is False
    assert "uq_editor_collaboration_leases_token_hash" in _constraint_names(
        leases
    )
    assert {
        "ix_collaboration_leases_user",
        "ix_collaboration_leases_session",
    }.issubset(index.name for index in leases.indexes)

    instances = tables["editor_collaboration_instances"]
    assert tuple(column.name for column in instances.primary_key.columns) == (
        "tenant_id",
        "slot",
    )
    assert str(instances.c.slot.server_default.arg) == "'primary'"
    assert "ck_collaboration_instances_primary_slot" in _constraint_names(
        instances
    )

    rendered = "\n".join(
        str(
            CreateTable(table).compile(
                dialect=postgresql.dialect()
            )
        )
        for table in tables.values()
    )
    rendered += "\n" + "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for table in tables.values()
        for index in table.indexes
    )
    assert "BYTEA" in rendered
    assert "JSONB" in rendered
    assert "generation >= 1 AND sequence >= 1" in rendered
    assert "update_bytes IS NULL AND payload_pruned_at IS NOT NULL" in rendered
    assert "slot = 'primary'" in rendered


def test_runtime_orm_extends_the_frozen_0048_table_contract_additively() -> None:
    """Later revisions may add review fields without rewriting frozen 0048."""
    migration = _migration()
    runtime_tables = {
        table.name: table
        for table in (
            editor_collaboration_updates,
            editor_collaboration_snapshots,
            editor_collaboration_leases,
            editor_collaboration_instances,
        )
    }

    assert set(runtime_tables) == set(migration._collaboration_metadata.tables)
    for table_name, runtime_table in runtime_tables.items():
        frozen_table = migration._collaboration_metadata.tables[table_name]
        additive_columns = {
            "editor_collaboration_updates": {
                "actor_guest_identity_id",
                "change_summary",
                "decision_outcome",
                "rejected_suggestion_ids",
            },
            "editor_collaboration_leases": {
                "actor_kind",
                "guest_identity_id",
                "guest_link_id",
            },
        }.get(table_name, set())
        assert tuple(
            column
            for column in runtime_table.c.keys()
            if column not in additive_columns
        ) == tuple(frozen_table.c.keys())
        if table_name == "editor_collaboration_updates":
            assert isinstance(
                runtime_table.c.change_summary.type,
                postgresql.JSONB,
            )
            assert runtime_table.c.change_summary.nullable is False
            assert runtime_table.c.decision_outcome.nullable is True
            assert isinstance(
                runtime_table.c.rejected_suggestion_ids.type,
                postgresql.JSONB,
            )
            assert runtime_table.c.rejected_suggestion_ids.nullable is False
            assert _constraint_names(runtime_table) == (
                (
                    _constraint_names(frozen_table)
                    - {"ck_collaboration_updates_human_actor"}
                )
                | {
                    "ck_collaboration_updates_actor",
                    "ck_collaboration_updates_change_summary",
                    "ck_collaboration_updates_decision_outcome",
                    "ck_collaboration_updates_rejected_by_edit",
                    "ck_collaboration_updates_rejected_suggestion_ids",
                }
            )
            assert {index.name for index in runtime_table.indexes} == (
                {index.name for index in frozen_table.indexes}
                | {"ix_collaboration_updates_actor_guest"}
            )
        elif table_name == "editor_collaboration_leases":
            assert runtime_table.c.user_id.nullable is True
            assert runtime_table.c.session_id.nullable is True
            assert _constraint_names(runtime_table) == (
                _constraint_names(frozen_table)
                | {"ck_collaboration_leases_actor"}
            )
            actor_constraint = next(
                constraint
                for constraint in runtime_table.constraints
                if constraint.name == "ck_collaboration_leases_actor"
            )
            assert "session_id IS NOT NULL" in str(actor_constraint.sqltext)
            assert "session_id IS NULL" in str(actor_constraint.sqltext)
            assert {index.name for index in runtime_table.indexes} == (
                {index.name for index in frozen_table.indexes}
                | {"ix_collaboration_leases_guest_identity"}
            )
        else:
            assert _constraint_names(runtime_table) == _constraint_names(
                frozen_table
            )
            assert {index.name for index in runtime_table.indexes} == {
                index.name for index in frozen_table.indexes
            }


def test_upgrade_emits_legacy_extensions_fks_and_tenant_security(
    monkeypatch,
) -> None:
    """Upgrade DDL must join legacy state to the new secured tables."""
    migration = _migration()
    executed: list[str] = []
    create_calls: list[tuple[object, bool]] = []
    bind = object()

    monkeypatch.setattr(migration.op, "execute", executed.append)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration._collaboration_metadata,
        "create_all",
        lambda *, bind, checkfirst: create_calls.append((bind, checkfirst)),
    )

    migration.upgrade()

    assert create_calls == [(bind, False)]
    statements = "\n".join(executed)
    for column_contract in (
        "content_mode text NOT NULL DEFAULT 'markdown'",
        "metadata_revision bigint NOT NULL DEFAULT 1",
        "collaboration_generation bigint NOT NULL DEFAULT 0",
        "collaboration_schema_version integer NULL",
        "collaboration_schema_hash text NULL",
        "persisted_sequence bigint NOT NULL DEFAULT 0",
        "projection_sequence bigint NOT NULL DEFAULT 0",
        "projection_updated_at double precision NULL",
        "deleted_at double precision NULL",
    ):
        assert column_contract in statements
    assert "ck_editor_documents_collaboration_state" in statements
    assert "uq_editor_documents_tenant_document UNIQUE (tenant_id, id)" in statements
    assert "collaboration_schema_version IS NOT NULL" in statements
    assert "collaboration_schema_hash IS NOT NULL" in statements
    assert "projection_sequence <= persisted_sequence" in statements

    assert "CHECK (source IN ('suggest', 'instruct', 'agent', 'human'))" in statements
    assert "collaboration_generation bigint NULL" in statements
    assert "base_sequence bigint NULL" in statements
    assert "decision_sequence bigint NULL" in statements
    assert "suggestion_ids jsonb NOT NULL DEFAULT '[]'::jsonb" in statements
    assert "decided_by_user_id uuid NULL" in statements
    assert "command_id uuid NULL" in statements
    assert "fk_editor_patches_decided_by_user" in statements
    assert "collaboration_generation IS NOT NULL" in statements
    assert "base_sequence IS NOT NULL" in statements
    assert "ix_editor_patches_decided_by_user" in statements

    assert "UPDATE editor_comments AS comment" in statements
    assert "document.created_by_user_id" in statements
    assert "document.tenant_id = comment.tenant_id" in statements
    assert "fk_editor_comments_created_by_user" in statements
    assert "ix_editor_comments_created_by_user" in statements
    maintenance_lock = (
        f"LOCK TABLE {', '.join(migration._OWNER_MAINTENANCE_TABLES)} "
        "IN ACCESS EXCLUSIVE MODE"
    )
    assert executed[0] == maintenance_lock
    backfill_position = executed.index(migration._COMMENT_BACKFILL_SQL)
    assert executed[len(migration._OWNER_MAINTENANCE_TABLES) + 1] == (
        "SET LOCAL row_security = off"
    )
    for table in migration._OWNER_MAINTENANCE_TABLES:
        no_force = f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"
        force = f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
        assert executed.index(no_force) < backfill_position < executed.index(force)

    assert "'editor_document'" in statements
    assert "permission IN ('view', 'suggest', 'edit')" in statements
    assert (
        "resource_type IN ('run', 'knowledge_collection', "
        "'prompt_template', 'skill_template') AND permission IN ('view', 'edit')"
        in statements
    )

    for foreign_key in (
        "fk_collaboration_updates_document",
        "fk_collaboration_updates_actor_user",
        "fk_collaboration_snapshots_document",
        "fk_collaboration_leases_document",
        "fk_collaboration_leases_user",
        "fk_collaboration_leases_session",
    ):
        assert foreign_key in statements
    assert statements.count(
        "FOREIGN KEY (tenant_id, document_id) "
        "REFERENCES editor_documents(tenant_id, id) ON DELETE CASCADE"
    ) == 3
    assert statements.count("REFERENCES users(id) ON DELETE RESTRICT") == 4
    assert "REFERENCES auth_sessions(id) ON DELETE CASCADE" in statements

    for table in migration._COLLABORATION_TABLES:
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} "
            f"TO {migration.APP_ROLE}"
        ) in executed
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in executed
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in executed
        policy = next(
            statement
            for statement in executed
            if f"CREATE POLICY tenant_isolation ON {table}" in statement
        )
        assert "USING (tenant_id = (SELECT inqtrix_current_tenant_id()))" in policy
        assert "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))" in policy


def test_downgrade_removes_collaboration_and_restores_0047_contracts(
    monkeypatch,
) -> None:
    """Downgrade must be executable and restore legacy share/patch domains."""
    migration = _migration()
    executed: list[str] = []
    drop_calls: list[tuple[object, bool]] = []
    bind = object()

    monkeypatch.setattr(migration.op, "execute", executed.append)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration._collaboration_metadata,
        "drop_all",
        lambda *, bind, checkfirst: drop_calls.append((bind, checkfirst)),
    )

    migration.downgrade()

    assert drop_calls == [(bind, False)]
    statements = "\n".join(executed)
    preflight_position = executed.index(migration._DOWNGRADE_PREFLIGHT_SQL)
    assert "projection_sequence <> persisted_sequence" in executed[preflight_position]
    assert (
        "refuses downgrade while collaboration projections are stale"
        in executed[preflight_position]
    )
    maintenance_lock = (
        f"LOCK TABLE {', '.join(migration._OWNER_MAINTENANCE_TABLES)} "
        "IN ACCESS EXCLUSIVE MODE"
    )
    assert executed[0] == maintenance_lock
    assert preflight_position == len(migration._OWNER_MAINTENANCE_TABLES) + 2
    assert (
        "DELETE FROM resource_shares WHERE resource_type = 'editor_document' "
        "OR permission = 'suggest'"
    ) in statements
    assert (
        "CHECK (resource_type IN ('run', 'knowledge_collection', "
        "'prompt_template', 'skill_template'))"
    ) in statements
    assert "CHECK (permission IN ('view', 'edit'))" in statements
    assert (
        "UPDATE editor_patches SET source = 'suggest' WHERE source = 'human'"
        in statements
    )
    assert "CHECK (source IN ('suggest', 'instruct', 'agent'))" in statements

    for column in (
        "editor_comments DROP COLUMN created_by_user_id",
        "editor_patches DROP COLUMN command_id",
        "editor_patches DROP COLUMN decided_by_user_id",
        "editor_patches DROP COLUMN suggestion_ids",
        "editor_patches DROP COLUMN decision_sequence",
        "editor_patches DROP COLUMN base_sequence",
        "editor_patches DROP COLUMN collaboration_generation",
        "editor_documents DROP COLUMN deleted_at",
        "editor_documents DROP COLUMN projection_updated_at",
        "editor_documents DROP COLUMN projection_sequence",
        "editor_documents DROP COLUMN persisted_sequence",
        "editor_documents DROP COLUMN collaboration_schema_hash",
        "editor_documents DROP COLUMN collaboration_schema_version",
        "editor_documents DROP COLUMN collaboration_generation",
        "editor_documents DROP COLUMN metadata_revision",
        "editor_documents DROP COLUMN content_mode",
    ):
        assert column in statements
    assert "DROP CONSTRAINT uq_editor_documents_tenant_document" in statements

    delete_share_position = statements.index("DELETE FROM resource_shares")
    restored_share_position = statements.index(
        "ADD CONSTRAINT ck_resource_shares_type",
        delete_share_position,
    )
    assert delete_share_position < restored_share_position
    map_human_position = statements.index("SET source = 'suggest'")
    restored_patch_position = statements.index(
        "ADD CONSTRAINT ck_editor_patches_source",
        map_human_position,
    )
    assert map_human_position < restored_patch_position

    for table in migration._OWNER_MAINTENANCE_TABLES:
        no_force = f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"
        force = f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
        assert executed.index(no_force) < preflight_position < executed.index(force)


def test_owner_rls_maintenance_rejects_empty_tables(monkeypatch) -> None:
    """A programming error cannot create an owner-only RLS window."""
    migration = _migration()
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    with pytest.raises(ValueError, match="requires tables"):
        migration._begin_owner_rls_maintenance(())
    with pytest.raises(ValueError, match="requires tables"):
        migration._end_owner_rls_maintenance(())

    assert executed == []


def test_postgres_identifiers_fit_the_server_limit() -> None:
    """Constraint and index names must not be silently truncated by Postgres."""
    migration = _migration()
    identifiers = {
        table.name
        for table in migration._collaboration_metadata.tables.values()
    }
    for table in migration._collaboration_metadata.tables.values():
        identifiers.update(_constraint_names(table))
        identifiers.update(
            index.name for index in table.indexes if index.name is not None
        )
    raw_statements = (
        *migration._DOCUMENT_UPGRADE_SQL,
        *migration._PATCH_UPGRADE_SQL,
        *migration._COMMENT_UPGRADE_SQL,
        *migration._SHARE_UPGRADE_SQL,
        *migration._CROSS_METADATA_FK_SQL,
    )
    identifiers.update(
        match.group(1)
        for statement in raw_statements
        for match in re.finditer(
            r"\b(?:CONSTRAINT|INDEX)\s+([a-z][a-z0-9_]*)",
            statement,
        )
    )

    assert max(map(len, identifiers)) <= 63
