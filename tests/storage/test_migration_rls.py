"""Behavioural tests for the managed migration role and RLS contract."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from inqtrix.storage import migrate
from inqtrix.storage.migration_contract import (
    RUNTIME_REQUIRED_FUNCTIONS,
    RUNTIME_REQUIRED_SEQUENCES,
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_TABLES,
)
from inqtrix.storage.migrate import (
    MigrationOwnedObject,
    MigrationRoleReport,
    MigrationTenantTable,
    _assert_database_sessions_drained,
    _assert_migration_postconditions,
    _assert_migration_role,
    _assert_schema_transition_quiesced,
    _invoke_alembic,
    _log_auto_rls_strategy,
    _maintain_owner_rls_tables,
    _resolve_target_revisions,
    _tenant_policy_expression_matches,
    build_alembic_config,
)


class _OwnerMaintenanceConnection:
    """Minimal synchronous catalog connection for owner-hook behavior."""

    def __init__(self) -> None:
        self.dialect = postgresql_dialect()
        self.catalog: list[tuple[str, int, bool]] = []
        self.statements: list[str] = []

    def execute(self, statement: object) -> list[tuple[str, int, bool]]:
        rendered = str(statement)
        self.statements.append(rendered)
        if rendered.startswith("SELECT relation.relname"):
            return list(self.catalog)
        return []


def _tenant_table(**overrides: Any) -> MigrationTenantTable:
    values = {
        "name": "runs",
        "owner": "migration_owner",
        "manageable": True,
        "row_security": True,
        "force_row_security": True,
        "tenant_policy": True,
        "app_acl_valid": True,
    }
    values.update(overrides)
    return MigrationTenantTable(**values)


def _owned_object(**overrides: Any) -> MigrationOwnedObject:
    values = {
        "name": "inqtrix_current_tenant_id()",
        "kind": "function",
        "exists": True,
        "owner": "migration_owner",
        "manageable": True,
        "app_acl_valid": True,
    }
    values.update(overrides)
    return MigrationOwnedObject(**values)


def _role_report(**overrides: Any) -> MigrationRoleReport:
    values = {
        "current_user": "migration_owner",
        "session_user": "migration_owner",
        "server_version_num": 150000,
        "is_superuser": False,
        "bypass_rls": False,
        "can_create_role": False,
        "schema_create": True,
        "schema_usage": True,
        "app_role_exists": True,
        "app_role_secure": True,
        "app_role_admin": True,
        "version_table_exists": True,
        "version_table_owner": "migration_owner",
        "version_table_manageable": True,
        "version_app_acl_valid": True,
        "schema_revision": ("0048_editor_collaboration",),
        "tenant_tables": (_tenant_table(),),
        "runtime_dependencies": (
            _owned_object(),
            _owned_object(name="audit_log_id_seq", kind="sequence"),
            _owned_object(name="user_events_id_seq", kind="sequence"),
        ),
    }
    values.update(overrides)
    return MigrationRoleReport(**values)


def test_auto_never_silently_enters_owner_maintenance() -> None:
    with pytest.raises(RuntimeError, match="choose rls_mode='owner'"):
        _assert_migration_role(
            _role_report(),
            rls_mode="auto",
            services_quiesced=True,
        )


def test_owner_maintenance_tracks_new_and_recreated_tables_by_oid() -> None:
    connection = _OwnerMaintenanceConnection()
    connection.catalog = [("runs", 101, True), ("unrelated", 999, True)]
    tracked: dict[str, int] = {}

    _maintain_owner_rls_tables(
        connection,
        tracked,
        lock_version_table=True,
    )

    assert tracked == {"runs": 101}
    assert any(
        "LOCK TABLE alembic_version, runs IN ACCESS EXCLUSIVE MODE"
        in statement
        for statement in connection.statements
    )
    assert sum(
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY" in statement
        for statement in connection.statements
    ) == 1

    connection.catalog = [("runs", 101, False)]
    _maintain_owner_rls_tables(connection, tracked)
    assert sum(
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY" in statement
        for statement in connection.statements
    ) == 1

    connection.catalog = [("runs", 101, True)]
    _maintain_owner_rls_tables(connection, tracked)
    assert sum(
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY" in statement
        for statement in connection.statements
    ) == 2

    connection.catalog = [
        ("run_plans", 202, True),
        ("runs", 303, True),
    ]
    _maintain_owner_rls_tables(connection, tracked)

    assert tracked == {"run_plans": 202, "runs": 303}
    assert sum(
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY" in statement
        for statement in connection.statements
    ) == 3
    assert any(
        "ALTER TABLE run_plans NO FORCE ROW LEVEL SECURITY" in statement
        for statement in connection.statements
    )


def test_alembic_owner_hook_refreshes_tables_after_each_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_connection = _OwnerMaintenanceConnection()
    tracked: dict[str, int] = {}
    config = build_alembic_config(
        "postgresql+asyncpg://migration.invalid/inqtrix"
    )

    class AsyncConnection:
        """Execute a synchronous callback like SQLAlchemy's async adapter."""

        async def run_sync(
            self,
            callback: Callable[[Any], Any],
        ) -> Any:
            return callback(sync_connection)

    def upgrade(active_config: Any, revision: str) -> None:
        assert revision == "head"
        assert active_config.attributes["connection"] is sync_connection
        hook = active_config.attributes["on_version_apply"]
        sync_connection.catalog = [("runs", 101, True)]
        hook()
        sync_connection.catalog = [
            ("run_plans", 202, True),
            ("runs", 101, False),
        ]
        hook()

    monkeypatch.setattr(migrate.command, "upgrade", upgrade)

    asyncio.run(
        _invoke_alembic(
            AsyncConnection(),
            config=config,
            revision="head",
            downgrade=False,
            owner_rls_tables=tracked,
        )
    )

    assert tracked == {"run_plans": 202, "runs": 101}
    assert "connection" not in config.attributes
    assert "on_version_apply" not in config.attributes


def test_auto_strategy_warning_is_limited_to_rls_exempt_roles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="inqtrix")

    _log_auto_rls_strategy("fresh", "auto")
    assert not caplog.records

    _log_auto_rls_strategy("bypass", "auto")
    assert len(caplog.records) == 1
    assert "selected an RLS-exempt role" in caplog.records[0].getMessage()


def test_preflight_rejects_postgresql_before_version_15() -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL 15"):
        _assert_migration_role(
            _role_report(server_version_num=140012),
            rls_mode="bypass",
            services_quiesced=False,
        )


def test_owner_mode_requires_quiescence_only_for_an_installed_schema() -> None:
    with pytest.raises(RuntimeError, match="services_quiesced=True"):
        _assert_migration_role(
            _role_report(),
            rls_mode="owner",
            services_quiesced=False,
        )
    assert (
        _assert_migration_role(
            _role_report(),
            rls_mode="owner",
            services_quiesced=True,
        )
        == "owner"
    )


def test_installed_schema_transition_requires_quiescence_independent_of_rls() -> None:
    with pytest.raises(RuntimeError, match="applies to auto, owner, and bypass"):
        _assert_schema_transition_quiesced(
            _role_report(),
            expected_revisions=("0068_release_integrity",),
            services_quiesced=False,
        )

    assert _assert_schema_transition_quiesced(
        _role_report(),
        expected_revisions=("0068_release_integrity",),
        services_quiesced=True,
    )


def test_installed_schema_noop_does_not_require_maintenance_window() -> None:
    report = _role_report(schema_revision=("0068_release_integrity",))
    assert not _assert_schema_transition_quiesced(
        report,
        expected_revisions=("0068_release_integrity",),
        services_quiesced=False,
    )


def test_migration_refuses_other_database_client_sessions() -> None:
    class Result:
        def scalar_one(self) -> int:
            return 2

    class Connection:
        async def execute(self, _statement: object) -> Result:
            return Result()

    with pytest.raises(RuntimeError, match="2 other database client session"):
        asyncio.run(_assert_database_sessions_drained(Connection()))


def test_fresh_install_validates_cluster_role_provisioning() -> None:
    fresh = {
        "version_table_exists": False,
        "schema_revision": (),
        "tenant_tables": (),
        "runtime_dependencies": (
            _owned_object(exists=False, owner=None, manageable=False),
            _owned_object(
                name="audit_log_id_seq",
                kind="sequence",
                exists=False,
                owner=None,
                manageable=False,
            ),
            _owned_object(
                name="user_events_id_seq",
                kind="sequence",
                exists=False,
                owner=None,
                manageable=False,
            ),
        ),
    }
    with pytest.raises(RuntimeError, match="cannot create.*inqtrix_app"):
        _assert_migration_role(
            _role_report(
                **fresh,
                app_role_exists=False,
                app_role_secure=False,
                app_role_admin=False,
            ),
            rls_mode="owner",
            services_quiesced=False,
        )
    with pytest.raises(RuntimeError, match="ADMIN OPTION"):
        _assert_migration_role(
            _role_report(**fresh, app_role_admin=False),
            rls_mode="owner",
            services_quiesced=False,
        )
    assert (
        _assert_migration_role(
            _role_report(
                **fresh,
                app_role_exists=False,
                app_role_secure=False,
                app_role_admin=False,
                can_create_role=True,
            ),
            rls_mode="owner",
            services_quiesced=False,
        )
        == "fresh"
    )
    assert (
        _assert_migration_role(
            _role_report(
                version_table_exists=False,
                schema_revision=(),
                tenant_tables=(),
            ),
            rls_mode="owner",
            services_quiesced=False,
        )
        == "fresh"
    )


def test_bypass_mode_requires_a_dedicated_non_superuser_role() -> None:
    with pytest.raises(RuntimeError, match="NOSUPERUSER BYPASSRLS"):
        _assert_migration_role(
            _role_report(),
            rls_mode="bypass",
            services_quiesced=False,
        )
    with pytest.raises(RuntimeError, match="NOSUPERUSER BYPASSRLS"):
        _assert_migration_role(
            _role_report(is_superuser=True),
            rls_mode="bypass",
            services_quiesced=False,
        )
    assert (
        _assert_migration_role(
            _role_report(bypass_rls=True),
            rls_mode="bypass",
            services_quiesced=False,
        )
        == "bypass"
    )


def test_preflight_rejects_ownership_and_rls_drift_before_mutation() -> None:
    with pytest.raises(RuntimeError, match="missing.*inqtrix_app"):
        _assert_migration_role(
            _role_report(
                app_role_exists=False,
                app_role_secure=False,
                app_role_admin=False,
            ),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="NOLOGIN"):
        _assert_migration_role(
            _role_report(app_role_secure=False),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="does not own"):
        _assert_migration_role(
            _role_report(tenant_tables=(_tenant_table(manageable=False),)),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="alembic_version"):
        _assert_migration_role(
            _role_report(version_table_manageable=False),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="outside the packaged"):
        _assert_migration_role(
            _role_report(tenant_tables=(_tenant_table(name="external_rows"),)),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="already inconsistent"):
        _assert_migration_role(
            _role_report(
                tenant_tables=(_tenant_table(force_row_security=False),)
            ),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="missing required.*dependency"):
        _assert_migration_role(
            _role_report(
                runtime_dependencies=(
                    _owned_object(exists=False, owner=None, manageable=False),
                )
            ),
            rls_mode="owner",
            services_quiesced=True,
        )
    with pytest.raises(RuntimeError, match="runtime dependency objects"):
        _assert_migration_role(
            _role_report(
                runtime_dependencies=(_owned_object(manageable=False),)
            ),
            rls_mode="owner",
            services_quiesced=True,
        )


def test_postcondition_rejects_tenant_table_ownership_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(_connection: object) -> MigrationRoleReport:
        return _role_report(
            tenant_tables=(_tenant_table(manageable=False),)
        )

    monkeypatch.setattr(migrate, "_inspect_migration_role", inspect)

    with pytest.raises(RuntimeError, match="changed tenant-table ownership"):
        asyncio.run(
            _assert_migration_postconditions(
                object(),
                expected_revisions=("0048_editor_collaboration",),
            )
        )


def test_head_postcondition_rejects_runtime_dependency_acl_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(_connection: object) -> MigrationRoleReport:
        return _role_report(
            schema_revision=(SCHEMA_HEAD_REVISION,),
            tenant_tables=tuple(
                _tenant_table(name=table_name)
                for table_name in TENANT_RLS_TABLES
            ),
            runtime_dependencies=(
                *(
                    _owned_object(
                        name=function_signature,
                        kind="function",
                        app_acl_valid=False,
                    )
                    for function_signature in RUNTIME_REQUIRED_FUNCTIONS
                ),
                *(
                    _owned_object(name=sequence_name, kind="sequence")
                    for sequence_name in RUNTIME_REQUIRED_SEQUENCES
                ),
            ),
        )

    monkeypatch.setattr(migrate, "_inspect_migration_role", inspect)

    with pytest.raises(
        RuntimeError,
        match="runtime-dependency privilege contract mismatch",
    ):
        asyncio.run(
            _assert_migration_postconditions(
                object(),
                expected_revisions=(SCHEMA_HEAD_REVISION,),
            )
        )


def test_head_postcondition_rejects_tenant_table_acl_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(_connection: object) -> MigrationRoleReport:
        return _role_report(
            schema_revision=(SCHEMA_HEAD_REVISION,),
            tenant_tables=tuple(
                _tenant_table(
                    name=table_name,
                    app_acl_valid=table_name != "runs",
                )
                for table_name in TENANT_RLS_TABLES
            ),
        )

    monkeypatch.setattr(migrate, "_inspect_migration_role", inspect)

    with pytest.raises(RuntimeError, match="tenant-table privilege.*runs"):
        asyncio.run(
            _assert_migration_postconditions(
                object(),
                expected_revisions=(SCHEMA_HEAD_REVISION,),
            )
        )


def test_head_postcondition_rejects_version_table_acl_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(_connection: object) -> MigrationRoleReport:
        return _role_report(
            schema_revision=(SCHEMA_HEAD_REVISION,),
            version_app_acl_valid=False,
            tenant_tables=tuple(
                _tenant_table(name=table_name)
                for table_name in TENANT_RLS_TABLES
            ),
        )

    monkeypatch.setattr(migrate, "_inspect_migration_role", inspect)

    with pytest.raises(RuntimeError, match="alembic_version privilege"):
        asyncio.run(
            _assert_migration_postconditions(
                object(),
                expected_revisions=(SCHEMA_HEAD_REVISION,),
            )
        )


def test_dedicated_migration_url_is_the_only_url_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def run(database_url: str, **kwargs: Any) -> None:
        seen["database_url"] = database_url
        seen.update(kwargs)

    monkeypatch.setattr(migrate, "_run_schema_migrations", run)

    migrate.run_migrations(
        "postgresql+asyncpg://runtime.invalid/inqtrix",
        migration_database_url=(
            "postgresql+asyncpg://migration.invalid/inqtrix"
        ),
        rls_mode="owner",
        services_quiesced=True,
    )

    assert seen == {
        "database_url": "postgresql+asyncpg://migration.invalid/inqtrix",
        "revision": "head",
        "rls_mode": "owner",
        "services_quiesced": True,
        "downgrade": False,
    }


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+aiosqlite:///tmp/inqtrix.db",
        "postgresql+psycopg://operator@db/inqtrix",
    ),
)
def test_migration_url_requires_direct_async_postgresql(
    database_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"postgresql\+asyncpg"):
        migrate._validate_migration_url(database_url)


def test_migration_url_accepts_asyncpg_without_opening_it() -> None:
    migrate._validate_migration_url(
        "postgresql+asyncpg://operator@db/inqtrix"
    )


@pytest.mark.parametrize(
    "expression",
    (
        "tenant_id = inqtrix_current_tenant_id()",
        "tenant_id = (SELECT inqtrix_current_tenant_id())",
        (
            "tenant_id = (( SELECT inqtrix_current_tenant_id() AS "
            "inqtrix_current_tenant_id))"
        ),
    ),
)
def test_tenant_policy_accepts_only_the_canonical_equality(
    expression: str,
) -> None:
    assert _tenant_policy_expression_matches(expression) is True
    assert _tenant_policy_expression_matches(f"{expression} OR true") is False


@pytest.mark.parametrize("revision", ("-1", "+2", "head-1"))
def test_relative_revision_targets_fail_before_database_access(
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="explicit revision"):
        _resolve_target_revisions(
            build_alembic_config(
                "postgresql+asyncpg://example.invalid/inqtrix"
            ),
            revision,
        )
