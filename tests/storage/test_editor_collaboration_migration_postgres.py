"""Real PostgreSQL migration coverage for a non-bypass schema owner."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from inqtrix.storage import migrate
from inqtrix.storage.migration_contract import (
    RUNTIME_REQUIRED_SEQUENCES,
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_TABLES,
)
from inqtrix.storage.migrate import downgrade_migrations, run_migrations
from inqtrix.storage.runtime_contract import (
    DatabaseRuntimeContractError,
    verify_database_url_runtime_contract,
)


TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres


def _identifier(value: str) -> str:
    """Quote one generated PostgreSQL identifier defensively."""
    return '"' + value.replace('"', '""') + '"'


def _connection_kwargs(url: URL) -> dict[str, Any]:
    """Translate an SQLAlchemy async URL to asyncpg constructor arguments."""
    values: dict[str, Any] = {
        "user": url.username,
        "password": url.password,
        "database": url.database,
        "host": url.host,
        "port": url.port,
    }
    return {key: value for key, value in values.items() if value is not None}


async def _provision_database(
    admin_url: URL,
    *,
    role_name: str,
    password: str,
    database_name: str,
    bypass_rls: bool = False,
) -> None:
    """Create a disposable database owned by a non-bypass migration login."""
    connection = await asyncpg.connect(**_connection_kwargs(admin_url))
    try:
        is_superuser = await connection.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        if not bool(is_superuser):
            pytest.skip(
                "Migration-owner integration test requires a superuser URL "
                "to isolate one disposable role and database"
            )
        rls_attribute = "BYPASSRLS" if bypass_rls else "NOBYPASSRLS"
        await connection.execute(
            f"CREATE ROLE {_identifier(role_name)} LOGIN PASSWORD '{password}' "
            f"NOSUPERUSER {rls_attribute} NOCREATEDB NOCREATEROLE"
        )
        await connection.execute(
            f"GRANT inqtrix_app TO {_identifier(role_name)} WITH ADMIN OPTION"
        )
        await connection.execute(
            f"CREATE DATABASE {_identifier(database_name)} "
            f"OWNER {_identifier(role_name)}"
        )
    finally:
        await connection.close()


async def _transfer_schema_ownership(
    admin_database_url: URL,
    role_name: str,
) -> None:
    """Transfer every managed schema object to the external migration owner."""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        relations = await connection.fetch(
            "SELECT c.relkind, n.nspname, c.relname "
            "FROM pg_class AS c "
            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "ORDER BY c.relkind, c.relname"
        )
        relation_types = {
            "r": "TABLE",
            "p": "TABLE",
            "v": "VIEW",
            "m": "MATERIALIZED VIEW",
            "f": "FOREIGN TABLE",
        }
        for relation in relations:
            relkind = relation["relkind"]
            if isinstance(relkind, bytes):
                relkind = relkind.decode("ascii")
            await connection.execute(
                f"ALTER {relation_types[relkind]} "
                f"{_identifier(relation['nspname'])}."
                f"{_identifier(relation['relname'])} "
                f"OWNER TO {_identifier(role_name)}"
            )
        for sequence_name in RUNTIME_REQUIRED_SEQUENCES:
            if await connection.fetchval(
                "SELECT to_regclass($1) IS NOT NULL",
                f"public.{sequence_name}",
            ):
                await connection.execute(
                    f"ALTER SEQUENCE public.{_identifier(sequence_name)} "
                    f"OWNER TO {_identifier(role_name)}"
                )
        await connection.execute(
            "ALTER FUNCTION public.inqtrix_current_tenant_id() OWNER TO "
            f"{_identifier(role_name)}"
        )
    finally:
        await connection.close()


async def _seed_legacy_comments(
    owner_url: URL,
    owners: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Insert two tenants while preserving the pre-0048 forced-RLS state."""
    connection = await asyncpg.connect(**_connection_kwargs(owner_url))
    try:
        async with connection.transaction():
            await connection.execute(
                "LOCK TABLE users, editor_documents, editor_comments "
                "IN ACCESS EXCLUSIVE MODE"
            )
            for table in ("users", "editor_documents", "editor_comments"):
                await connection.execute(
                    f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"
                )
            await connection.execute("SET LOCAL row_security = off")
            for index, (tenant_id, owner_id) in enumerate(
                zip(("migration-a", "migration-b"), owners, strict=True),
                start=1,
            ):
                await connection.execute(
                    "INSERT INTO users "
                    "(id, tenant_id, issuer, subject, email, email_verified, "
                    "display_name, created_at) "
                    "VALUES ($1, $2, 'local', $3, $4, true, $5, now())",
                    owner_id,
                    tenant_id,
                    f"owner-{index}",
                    f"owner-{index}@example.invalid",
                    f"Owner {index}",
                )
                document_id = f"ed_migration_{index}"
                await connection.execute(
                    "INSERT INTO editor_documents "
                    "(id, tenant_id, created_by_user_id, workspace_id, title, "
                    "content_markdown, folder_id, source, source_run_id, "
                    "revision, diff_anchor_markdown, diff_anchor_updated_at, "
                    "created_at, updated_at) "
                    "VALUES ($1, $2, $3, NULL, $4, $5, NULL, 'blank', NULL, "
                    "1, NULL, NULL, 1.0, 1.0)",
                    document_id,
                    tenant_id,
                    owner_id,
                    f"Document {index}",
                    f"# Tenant {index}",
                )
                await connection.execute(
                    "INSERT INTO editor_comments "
                    "(id, document_id, tenant_id, comment_markdown, anchor, "
                    "kind, status, evidence_preset, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, '{}'::json, 'collect', 'open', "
                    "NULL, 1.0, 1.0)",
                    f"edc_migration_{index}",
                    document_id,
                    tenant_id,
                    f"Private comment {index}",
                )
            for table in ("users", "editor_documents", "editor_comments"):
                await connection.execute(
                    f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
                )
    finally:
        await connection.close()


async def _seed_0042_agent_contract(owner_url: URL) -> None:
    """Seed tenant-separated plan approvals and run trees before 0043."""
    connection = await asyncpg.connect(**_connection_kwargs(owner_url))
    tables = ("runs", "run_plans", "run_plan_tasks", "run_approvals")
    try:
        async with connection.transaction():
            await connection.execute(
                f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE"
            )
            for table in tables:
                await connection.execute(
                    f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"
                )
            await connection.execute("SET LOCAL row_security = off")
            for index, tenant_id in enumerate(("tenant-a", "tenant-b"), start=1):
                root_id = f"run_root_{index}"
                child_id = f"run_child_{index}"
                plan_id = f"plan_{index}"
                await connection.execute(
                    "INSERT INTO runs "
                    "(run_id, tenant_id, status, question, created_at, kind, "
                    "parent_run_id, root_run_id, request_payload) VALUES "
                    "($1, $2, 'completed', 'Root', 1.0, 'agent', NULL, "
                    "NULL, '{}'::json), "
                    "($3, $2, 'completed', 'Child', 2.0, 'agent_child', $1, "
                    "NULL, '{\"body\": {\"token_budget\": 100}}'::json)",
                    root_id,
                    tenant_id,
                    child_id,
                )
                await connection.execute(
                    "INSERT INTO run_plans "
                    "(plan_id, tenant_id, run_id, version, created_at) "
                    "VALUES ($1, $2, $3, 1, 1.0)",
                    plan_id,
                    tenant_id,
                    root_id,
                )
                await connection.execute(
                    "INSERT INTO run_plan_tasks "
                    "(task_id, tenant_id, plan_id, run_id, ordinal, title, "
                    "tool_kind, child_run_id) "
                    "VALUES ($1, $2, $3, $4, 1, 'Task', 'synthesis', $5)",
                    f"task_{index}",
                    tenant_id,
                    plan_id,
                    root_id,
                    child_id,
                )
                await connection.execute(
                    "INSERT INTO run_approvals "
                    "(approval_id, tenant_id, run_id, kind, subject_type, "
                    "subject_id, payload, created_at) "
                    "VALUES ($1, $2, $3, 'plan', 'plan', '', "
                    "'{\"plan_version\": 1}'::json, 1.0)",
                    f"approval_{index}",
                    tenant_id,
                    root_id,
                )
            for table in tables:
                await connection.execute(
                    f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
                )
            await connection.execute(
                "GRANT USAGE ON SEQUENCE audit_log_id_seq TO PUBLIC"
            )
    finally:
        await connection.close()


async def _verify_head_agent_contract(admin_database_url: URL) -> None:
    """Verify repaired data and durable head-revision tenant constraints."""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        approvals = await connection.fetch(
            "SELECT tenant_id, subject_id FROM run_approvals "
            "ORDER BY tenant_id"
        )
        assert [tuple(row.values()) for row in approvals] == [
            ("tenant-a", "plan_1"),
            ("tenant-b", "plan_2"),
        ]
        children = await connection.fetch(
            "SELECT tenant_id, root_run_id, request_payload FROM runs "
            "WHERE kind = 'agent_child' ORDER BY tenant_id"
        )
        assert [
            (row["tenant_id"], row["root_run_id"])
            for row in children
        ] == [
            ("tenant-a", "run_root_1"),
            ("tenant-b", "run_root_2"),
        ]
        assert await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        ) == SCHEMA_HEAD_REVISION
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                "INSERT INTO run_plan_tasks "
                "(task_id, tenant_id, plan_id, run_id, ordinal, title, "
                "tool_kind) VALUES "
                "('task_mismatched_run', 'tenant-a', 'plan_1', "
                "'run_child_1', 99, 'Mismatch', 'synthesis')"
            )
        security = await connection.fetch(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY($1::text[])",
            ["runs", "run_plans", "run_approvals"],
        )
        assert len(security) == 3
        assert all(
            row["relrowsecurity"] and row["relforcerowsecurity"]
            for row in security
        )
        assert not bool(
            await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_class AS relation "
                "CROSS JOIN LATERAL aclexplode(COALESCE("
                "relation.relacl, acldefault('s', relation.relowner))) AS acl "
                "WHERE relation.relname = 'audit_log_id_seq' "
                "AND relation.relkind = 'S' AND acl.grantee = 0 "
                "AND acl.privilege_type = 'USAGE')"
            )
        )
    finally:
        await connection.close()


async def _corrupt_0048_task_scope(owner_url: URL) -> None:
    """Create pre-0049 task data whose plan and run cross tenant boundaries."""
    connection = await asyncpg.connect(**_connection_kwargs(owner_url))
    try:
        async with connection.transaction():
            await connection.execute(
                "LOCK TABLE run_plan_tasks IN ACCESS EXCLUSIVE MODE"
            )
            await connection.execute(
                "ALTER TABLE run_plan_tasks NO FORCE ROW LEVEL SECURITY"
            )
            await connection.execute("SET LOCAL row_security = off")
            await connection.execute(
                "UPDATE run_plan_tasks SET run_id = 'run_root_2' "
                "WHERE task_id = 'task_1' AND plan_id = 'plan_1'"
            )
            await connection.execute(
                "ALTER TABLE run_plan_tasks FORCE ROW LEVEL SECURITY"
            )
    finally:
        await connection.close()


async def _corrupt_0042_approval_payload(owner_url: URL) -> None:
    """Set a nonnumeric legacy plan version without bypassing the SQL guard."""
    connection = await asyncpg.connect(**_connection_kwargs(owner_url))
    try:
        async with connection.transaction():
            await connection.execute(
                "LOCK TABLE run_approvals IN ACCESS EXCLUSIVE MODE"
            )
            await connection.execute(
                "ALTER TABLE run_approvals NO FORCE ROW LEVEL SECURITY"
            )
            await connection.execute("SET LOCAL row_security = off")
            await connection.execute(
                "UPDATE run_approvals "
                "SET payload = '{\"plan_version\": \"broken\"}'::json "
                "WHERE approval_id = 'approval_1'"
            )
            await connection.execute(
                "ALTER TABLE run_approvals FORCE ROW LEVEL SECURITY"
            )
    finally:
        await connection.close()


async def _add_extra_permissive_policy(owner_url: URL) -> None:
    """Add a second permissive policy that would OR-open tenant isolation."""
    connection = await asyncpg.connect(**_connection_kwargs(owner_url))
    try:
        await connection.execute(
            "CREATE POLICY migration_test_allow_all ON runs "
            "FOR ALL USING (true) WITH CHECK (true)"
        )
    finally:
        await connection.close()


async def _verify_failed_integrity_upgrade(
    admin_database_url: URL,
    *,
    expected_revision: str,
    protected_table: str,
) -> None:
    """Verify a rejected upgrade preserved its revision and forced RLS."""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        assert await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        ) == expected_revision
        assert bool(
            await connection.fetchval(
                "SELECT relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = $1",
                protected_table,
            )
        )
    finally:
        await connection.close()


async def _verify_upgrade(
    admin_database_url: URL,
    owners: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Verify every tenant was backfilled and forced RLS was restored."""
    connection = await asyncpg.connect(**_connection_kwargs(admin_database_url))
    try:
        rows = await connection.fetch(
            "SELECT tenant_id, created_by_user_id FROM editor_comments "
            "ORDER BY tenant_id"
        )
        assert [(row["tenant_id"], row["created_by_user_id"]) for row in rows] == [
            ("migration-a", owners[0]),
            ("migration-b", owners[1]),
        ]
        security = await connection.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY($1::text[]) ORDER BY relname",
            [
                "editor_comments",
                "editor_documents",
                "editor_patches",
                "resource_shares",
            ],
        )
        assert len(security) == 4
        assert all(row["relrowsecurity"] for row in security)
        assert all(row["relforcerowsecurity"] for row in security)
    finally:
        await connection.close()


async def _verify_failed_upgrade_rollback(admin_database_url: URL) -> None:
    """Verify a failed owner window leaves revision, schema, and FORCE intact."""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        assert await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        ) == "0047_resource_sync"
        assert await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runs' "
            "AND column_name = 'migration_failure_probe'"
        ) == 0
        assert bool(
            await connection.fetchval(
                "SELECT bool_and(c.relrowsecurity AND c.relforcerowsecurity) "
                "FROM pg_class AS c JOIN pg_namespace AS n "
                "ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND EXISTS (SELECT 1 FROM pg_attribute AS a "
                "WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' "
                "AND a.attnum > 0 AND NOT a.attisdropped)"
            )
        )
    finally:
        await connection.close()


async def _verify_downgrade(admin_database_url: URL) -> None:
    """Verify the preflight completed and revision-0047 RLS remains forced."""
    connection = await asyncpg.connect(**_connection_kwargs(admin_database_url))
    try:
        created_by_column = await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'editor_comments' "
            "AND column_name = 'created_by_user_id'"
        )
        assert created_by_column == 0
        forced = await connection.fetch(
            "SELECT relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY($1::text[])",
            [
                "editor_comments",
                "editor_documents",
                "editor_patches",
                "resource_shares",
            ],
        )
        assert len(forced) == 4
        assert all(row["relforcerowsecurity"] for row in forced)
        function_acl = await connection.fetchrow(
            "SELECT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
            "routine.proacl, acldefault('f', routine.proowner))) AS acl "
            "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "AS public_execute, EXISTS (SELECT 1 FROM aclexplode(COALESCE("
            "routine.proacl, acldefault('f', routine.proowner))) AS acl "
            "WHERE acl.grantee = (SELECT oid FROM pg_roles "
            "WHERE rolname = 'inqtrix_app') "
            "AND acl.privilege_type = 'EXECUTE') AS app_execute "
            "FROM pg_proc AS routine WHERE routine.oid = "
            "to_regprocedure('inqtrix_current_tenant_id()')"
        )
        assert function_acl is not None
        assert bool(function_acl["public_execute"])
        assert not bool(function_acl["app_execute"])
    finally:
        await connection.close()


async def _cleanup_database(
    admin_url: URL,
    *,
    role_name: str,
    database_name: str,
) -> None:
    """Remove the disposable database and login even after a failed assertion."""
    connection = await asyncpg.connect(**_connection_kwargs(admin_url))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await connection.execute(
            f"DROP DATABASE IF EXISTS {_identifier(database_name)}"
        )
        await connection.execute(f"DROP ROLE IF EXISTS {_identifier(role_name)}")
    finally:
        await connection.close()


async def _verify_head_rls_inventory(admin_database_url: URL) -> None:
    """Verify a multi-revision owner upgrade restored every head table."""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        revision = await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        rows = await connection.fetch(
            "SELECT relation.relname, relation.relrowsecurity, "
            "relation.relforcerowsecurity "
            "FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND relation.relkind IN ('r', 'p') "
            "AND relation.relname = ANY($1::text[])",
            list(TENANT_RLS_TABLES),
        )
        assert revision == SCHEMA_HEAD_REVISION
        assert {str(row["relname"]) for row in rows} == set(TENANT_RLS_TABLES)
        assert all(
            bool(row["relrowsecurity"])
            and bool(row["relforcerowsecurity"])
            for row in rows
        )
    finally:
        await connection.close()


async def _verify_runtime_rejects_catalog_and_acl_drift(
    admin_database_url: URL,
) -> None:
    """Prove readiness observes RLS and executable dependency drift."""
    dependency_owner = f"inqtrix_dependency_{uuid.uuid4().hex[:12]}"
    dependency_owner_created = False
    reporting_role = f"inqtrix_reporting_{uuid.uuid4().hex[:12]}"
    reporting_role_created = False
    original_function_owner = ""
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        await verify_database_url_runtime_contract(
            admin_database_url.render_as_string(hide_password=False),
            app_role="inqtrix_app",
            login_policy="bundled_legacy",
        )
        await connection.execute(
            f"CREATE ROLE {_identifier(reporting_role)} NOLOGIN NOSUPERUSER "
            "NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        )
        reporting_role_created = True
        await connection.execute(
            "GRANT SELECT (version_num) ON alembic_version TO "
            f"{_identifier(reporting_role)}"
        )
        await verify_database_url_runtime_contract(
            admin_database_url.render_as_string(hide_password=False),
            app_role="inqtrix_app",
            login_policy="bundled_legacy",
        )
        await connection.execute(
            "REVOKE SELECT (version_num) ON alembic_version FROM "
            f"{_identifier(reporting_role)}"
        )
        await connection.execute(
            f"DROP ROLE {_identifier(reporting_role)}"
        )
        reporting_role_created = False
        await connection.execute(
            "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="RLS must be enabled and forced.*runs",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "ALTER TABLE runs FORCE ROW LEVEL SECURITY"
        )

        await connection.execute(
            "REVOKE EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
            "FROM PUBLIC, inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="tenant functions",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
            "TO inqtrix_app"
        )

        await connection.execute(
            "REVOKE USAGE ON SEQUENCE audit_log_id_seq FROM inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="sequence grants.*audit_log_id_seq",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "GRANT USAGE ON SEQUENCE audit_log_id_seq TO inqtrix_app"
        )

        await connection.execute(
            "GRANT TRUNCATE ON runs TO inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="required table grants.*runs",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "REVOKE TRUNCATE ON runs FROM inqtrix_app"
        )

        await connection.execute("GRANT SELECT ON runs TO PUBLIC")
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="canonical application grants.*runs",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute("REVOKE SELECT ON runs FROM PUBLIC")

        await connection.execute(
            "GRANT INSERT ON alembic_version TO inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="SELECT-only access to alembic_version",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "REVOKE INSERT ON alembic_version FROM inqtrix_app"
        )

        await connection.execute(
            "GRANT UPDATE (version_num) ON alembic_version TO inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="SELECT-only access to alembic_version",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
        await connection.execute(
            "REVOKE UPDATE (version_num) ON alembic_version FROM inqtrix_app"
        )

        original_function_owner = str(
            await connection.fetchval(
                "SELECT owner.rolname FROM pg_proc AS routine "
                "JOIN pg_namespace AS namespace "
                "ON namespace.oid = routine.pronamespace "
                "JOIN pg_roles AS owner ON owner.oid = routine.proowner "
                "WHERE namespace.nspname = current_schema() "
                "AND routine.oid = "
                "to_regprocedure('inqtrix_current_tenant_id()')"
            )
        )
        await connection.execute(
            f"CREATE ROLE {_identifier(dependency_owner)} NOLOGIN "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        )
        dependency_owner_created = True
        await connection.execute(
            "ALTER FUNCTION inqtrix_current_tenant_id() OWNER TO "
            f"{_identifier(dependency_owner)}"
        )
        await connection.execute(
            f"GRANT {_identifier(dependency_owner)} TO inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="tenant functions",
        ):
            await verify_database_url_runtime_contract(
                admin_database_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
                login_policy="bundled_legacy",
            )
    finally:
        if reporting_role_created:
            await connection.execute(
                "REVOKE SELECT (version_num) ON alembic_version FROM "
                f"{_identifier(reporting_role)}"
            )
            await connection.execute(
                f"DROP ROLE IF EXISTS {_identifier(reporting_role)}"
            )
        if dependency_owner_created:
            await connection.execute(
                f"REVOKE {_identifier(dependency_owner)} FROM inqtrix_app"
            )
            if original_function_owner:
                await connection.execute(
                    "ALTER FUNCTION inqtrix_current_tenant_id() OWNER TO "
                    f"{_identifier(original_function_owner)}"
                )
            await connection.execute(
                f"DROP ROLE IF EXISTS {_identifier(dependency_owner)}"
            )
        await connection.execute(
            "REVOKE INSERT ON alembic_version FROM inqtrix_app"
        )
        await connection.execute(
            "REVOKE UPDATE (version_num) ON alembic_version FROM inqtrix_app"
        )
        await connection.execute("REVOKE SELECT ON runs FROM PUBLIC")
        await connection.execute(
            "REVOKE TRUNCATE ON runs FROM inqtrix_app"
        )
        await connection.execute(
            "GRANT EXECUTE ON FUNCTION inqtrix_current_tenant_id() "
            "TO inqtrix_app"
        )
        await connection.execute(
            "GRANT USAGE ON SEQUENCE audit_log_id_seq TO inqtrix_app"
        )
        await connection.execute(
            "ALTER TABLE runs FORCE ROW LEVEL SECURITY"
        )
        await connection.close()


async def _verify_runtime_rejects_migration_role_membership(
    admin_database_url: URL,
    migration_role: str,
) -> None:
    """Prove a restricted login cannot reach migration/database authority."""
    privileged_role = f"{migration_role}_escalation"
    custom_app_role = f"{migration_role}_custom"
    runtime_role = f"{migration_role}_runtime"
    runtime_password = f"test_{uuid.uuid4().hex}"
    runtime_url = admin_database_url.set(
        username=runtime_role,
        password=runtime_password,
    )
    database_name = str(admin_database_url.database)
    database_owner_changed = False
    original_schema_owner = ""
    schema_owner_changed = False
    connection = await asyncpg.connect(
        **_connection_kwargs(admin_database_url)
    )
    try:
        await connection.execute(
            f"CREATE ROLE {_identifier(runtime_role)} LOGIN NOINHERIT "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
            f"PASSWORD '{runtime_password}'"
        )
        await connection.execute(
            f"GRANT inqtrix_app TO {_identifier(runtime_role)}"
        )
        await connection.execute(
            f"CREATE ROLE {_identifier(custom_app_role)} NOLOGIN INHERIT "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        )
        await connection.execute(
            f"GRANT inqtrix_app TO {_identifier(custom_app_role)}"
        )
        await connection.execute(
            f"GRANT {_identifier(custom_app_role)} TO "
            f"{_identifier(runtime_role)}"
        )
        await verify_database_url_runtime_contract(
            runtime_url.render_as_string(hide_password=False),
            app_role="inqtrix_app",
        )
        await verify_database_url_runtime_contract(
            runtime_url.render_as_string(hide_password=False),
            app_role=custom_app_role,
        )
        await connection.execute(
            f"CREATE ROLE {_identifier(privileged_role)} NOLOGIN "
            "NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE"
        )
        await connection.execute(
            f"GRANT {_identifier(privileged_role)} TO inqtrix_app"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="SUPERUSER or BYPASSRLS role",
        ):
            await verify_database_url_runtime_contract(
                runtime_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
            )
        await connection.execute(
            f"REVOKE {_identifier(privileged_role)} FROM inqtrix_app"
        )
        await connection.execute(
            f"GRANT CREATE ON DATABASE {_identifier(database_name)} "
            f"TO {_identifier(runtime_role)}"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="forbidden direct/inherited/SET ROLE capabilities",
        ):
            await verify_database_url_runtime_contract(
                runtime_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
            )
        await connection.execute(
            f"REVOKE CREATE ON DATABASE {_identifier(database_name)} "
            f"FROM {_identifier(runtime_role)}"
        )

        await connection.execute(
            f"ALTER DATABASE {_identifier(database_name)} OWNER TO "
            f"{_identifier(runtime_role)}"
        )
        database_owner_changed = True
        await connection.execute(
            f"REVOKE CREATE ON DATABASE {_identifier(database_name)} "
            f"FROM {_identifier(runtime_role)}"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="runtime session login",
        ):
            await verify_database_url_runtime_contract(
                runtime_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
            )
        await connection.execute(
            f"ALTER DATABASE {_identifier(database_name)} OWNER TO "
            f"{_identifier(migration_role)}"
        )
        database_owner_changed = False

        original_schema_owner = str(
            await connection.fetchval(
                "SELECT owner.rolname FROM pg_namespace AS namespace "
                "JOIN pg_roles AS owner ON owner.oid = namespace.nspowner "
                "WHERE namespace.nspname = current_schema()"
            )
        )
        await connection.execute(
            f"GRANT CREATE ON DATABASE {_identifier(database_name)} "
            f"TO {_identifier(runtime_role)}"
        )
        await connection.execute(
            f"ALTER SCHEMA public OWNER TO {_identifier(runtime_role)}"
        )
        schema_owner_changed = True
        await connection.execute(
            f"REVOKE CREATE ON DATABASE {_identifier(database_name)} "
            f"FROM {_identifier(runtime_role)}"
        )
        await connection.execute(
            f"REVOKE CREATE ON SCHEMA public FROM {_identifier(runtime_role)}"
        )
        with pytest.raises(
            DatabaseRuntimeContractError,
            match="forbidden direct/inherited/SET ROLE capabilities",
        ):
            await verify_database_url_runtime_contract(
                runtime_url.render_as_string(hide_password=False),
                app_role="inqtrix_app",
            )
        await connection.execute(
            "ALTER SCHEMA public OWNER TO "
            f"{_identifier(original_schema_owner)}"
        )
        schema_owner_changed = False
    finally:
        if schema_owner_changed and original_schema_owner:
            await connection.execute(
                "ALTER SCHEMA public OWNER TO "
                f"{_identifier(original_schema_owner)}"
            )
        if database_owner_changed:
            await connection.execute(
                f"ALTER DATABASE {_identifier(database_name)} OWNER TO "
                f"{_identifier(migration_role)}"
            )
        await connection.execute(
            f"REVOKE CREATE ON DATABASE {_identifier(database_name)} "
            f"FROM {_identifier(runtime_role)}"
        )
        await connection.execute(
            f"REVOKE {_identifier(privileged_role)} FROM inqtrix_app"
        )
        await connection.execute(
            f"DROP ROLE IF EXISTS {_identifier(privileged_role)}"
        )
        await connection.execute(
            f"REVOKE inqtrix_app FROM {_identifier(runtime_role)}"
        )
        await connection.execute(
            f"REVOKE {_identifier(custom_app_role)} FROM "
            f"{_identifier(runtime_role)}"
        )
        await connection.execute(
            f"REVOKE inqtrix_app FROM {_identifier(custom_app_role)}"
        )
        await connection.execute(
            f"DROP ROLE IF EXISTS {_identifier(custom_app_role)}"
        )
        await connection.execute(
            f"DROP ROLE IF EXISTS {_identifier(runtime_role)}"
        )
        await connection.close()


@pytest.mark.parametrize(
    ("rls_mode", "bypass_rls"),
    (("owner", False), ("bypass", True)),
)
def test_head_upgrade_backfills_populated_tenants_for_managed_roles(
    rls_mode: str,
    bypass_rls: bool,
) -> None:
    """Both managed-role paths migrate populated two-tenant 0042 state."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_0043_{suffix}"
    database_name = f"inqtrix_0043_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    owner_url = admin_url.set(
        username=role_name,
        password=password,
        database=database_name,
    )
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
                bypass_rls=bypass_rls,
            )
        )
        admin_database_dsn = admin_database_url.render_as_string(
            hide_password=False
        )
        owner_dsn = owner_url.render_as_string(hide_password=False)
        run_migrations(
            admin_database_dsn,
            revision="0042_agent_session_integrity",
        )
        asyncio.run(
            _transfer_schema_ownership(admin_database_url, role_name)
        )
        asyncio.run(_seed_0042_agent_contract(owner_url))

        run_migrations(
            owner_dsn,
            rls_mode=rls_mode,
            services_quiesced=True,
        )
        asyncio.run(_verify_head_agent_contract(admin_database_url))
        asyncio.run(
            _verify_runtime_rejects_migration_role_membership(
                admin_database_url,
                role_name,
            )
        )
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


async def _seed_release_integrity_contract(database_url: URL) -> None:
    connection = await asyncpg.connect(**_connection_kwargs(database_url))
    try:
        await connection.execute(
            """
            INSERT INTO asset_sections (
                id, tenant_id, kind, title, created_at, updated_at
            ) VALUES ('fsec_integrity', 'tenant-a', 'custom', 'Integrity', 1, 1);

            INSERT INTO files (
                id, tenant_id, file_name, content_type, size_bytes, sha256,
                object_key, created_at
            ) VALUES
                ('fl_integrity_a', 'tenant-a', 'a.txt', 'text/plain', 10,
                 'sha-a', 'integrity/a', 1),
                ('fl_integrity_b', 'tenant-a', 'b.txt', 'text/plain', 20,
                 'sha-b', 'integrity/b', 1),
                ('fl_integrity_c', 'tenant-a', 'c.txt', 'text/plain', 30,
                 'sha-c', 'integrity/c', 1);

            INSERT INTO asset_records (
                id, tenant_id, section_id, title, label, file_name, mime_type,
                server_file_id, created_at, updated_at
            ) VALUES
                ('fa_integrity_a', 'tenant-a', 'fsec_integrity', 'A', 'A',
                 'a.txt', 'text/plain', 'fl_integrity_a', 1, 1),
                ('fa_integrity_b', 'tenant-a', 'fsec_integrity', 'B', 'B',
                 'b.txt', 'text/plain', 'fl_integrity_b', 1, 1),
                ('fa_integrity_c', 'tenant-a', 'fsec_integrity', 'C', 'C',
                 'c.txt', 'text/plain', 'fl_integrity_c', 1, 1);

            INSERT INTO knowledge_collections (
                id, tenant_id, name, embedding_model, embedding_dim, created_at
            ) VALUES ('kc_integrity', 'tenant-a', 'Integrity', 'test', 3, 1);

            INSERT INTO knowledge_documents (
                id, collection_id, tenant_id, title, text, metadata, source_id,
                lifecycle_status, created_at
            ) VALUES
                ('kd_integrity_valid', 'kc_integrity', 'tenant-a', 'valid',
                 'valid', '{"fileId":"fa_integrity_a"}',
                 'asset:fa_integrity_a', 'active', 1),
                ('kd_integrity_conflict', 'kc_integrity', 'tenant-a', 'conflict',
                 'conflict', '{"fileId":"fa_integrity_a"}',
                 'asset:fa_integrity_b', 'active', 2),
                ('kd_integrity_dangling', 'kc_integrity', 'tenant-a', 'dangling',
                 'dangling', '{}', 'asset:missing', 'active', 3),
                ('kd_integrity_duplicate_a', 'kc_integrity', 'tenant-a', 'dup-a',
                 'dup-a', '{"fileId":"fa_integrity_c"}', NULL, 'active', 4),
                ('kd_integrity_duplicate_b', 'kc_integrity', 'tenant-a', 'dup-b',
                 'dup-b', '{"fileId":"fa_integrity_c"}', NULL, 'active', 5);
            """
        )
    finally:
        await connection.close()


async def _verify_quota_stock_guard(database_url: URL) -> None:
    """Verify that the real 0066 DDL preserved both literal guard patterns."""
    connection = await asyncpg.connect(**_connection_kwargs(database_url))
    try:
        revision = await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        assert revision == "0066_quota_stock_lifecycle"
        definition = await connection.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_quota_adjustments_no_file_stock'"
        )
        assert "asset-upload:%:stored-bytes" in str(definition)
        assert "asset-delete:%:stored-bytes" in str(definition)
    finally:
        await connection.close()


def test_quota_stock_guard_executes_colon_patterns_as_literal_ddl() -> None:
    """Revision 0066 must not parse the LIKE pattern as a bind parameter."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_quota_guard_{suffix}"
    database_name = f"inqtrix_quota_guard_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        database_dsn = admin_database_url.render_as_string(hide_password=False)
        run_migrations(database_dsn, revision="0065_generation_cleanup_contract")
        run_migrations(
            database_dsn,
            revision="0066_quota_stock_lifecycle",
            services_quiesced=True,
        )
        asyncio.run(_verify_quota_stock_guard(admin_database_url))
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


async def _verify_release_integrity_contract(database_url: URL) -> None:
    connection = await asyncpg.connect(**_connection_kwargs(database_url))
    try:
        rows = await connection.fetch(
            "SELECT id, source_id, lifecycle_status FROM knowledge_documents "
            "WHERE id LIKE 'kd_integrity_%' ORDER BY id"
        )
        by_id = {str(row["id"]): row for row in rows}
        assert by_id["kd_integrity_valid"]["source_id"] == "asset:fa_integrity_a"
        assert by_id["kd_integrity_valid"]["lifecycle_status"] == "active"
        for document_id in (
            "kd_integrity_conflict",
            "kd_integrity_dangling",
            "kd_integrity_duplicate_a",
            "kd_integrity_duplicate_b",
        ):
            assert by_id[document_id]["source_id"] is None
            assert by_id[document_id]["lifecycle_status"] == "quarantined"

        expected_constraints = {
            "fk_deletion_operation_assets_tenant_operation",
            "fk_deletion_operation_events_tenant_operation",
            "fk_upload_operations_tenant_asset",
            "fk_upload_operation_events_tenant_operation",
            "fk_upload_operation_outbox_tenant_operation",
            "fk_knowledge_revisions_tenant_document",
            "fk_knowledge_generations_tenant_collection",
        }
        constraints = await connection.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint WHERE conname = ANY($1::text[])",
            list(expected_constraints),
        )
        assert {str(row["conname"]) for row in constraints} == expected_constraints
        assert all(
            "FOREIGN KEY (tenant_id" in str(row["definition"])
            for row in constraints
        )

        await connection.execute(
            "INSERT INTO deletion_operations "
            "(operation_id, tenant_id, target_kind, target_id, created_at, updated_at) "
            "VALUES ('del_integrity', 'tenant-a', 'asset', 'fa_integrity_a', 1, 1)"
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                "INSERT INTO deletion_operation_events "
                "(operation_id, sequence, tenant_id, type, created_at, data) "
                "VALUES ('del_integrity', 1, 'tenant-b', 'created', 1, '{}')"
            )
    finally:
        await connection.close()


def test_populated_release_integrity_upgrade_is_fail_closed() -> None:
    """Populated 0067 data is reconciled before tenant FKs become active."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_integrity_{suffix}"
    database_name = f"inqtrix_integrity_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        database_dsn = admin_database_url.render_as_string(hide_password=False)
        run_migrations(database_dsn, revision="0067_session_deletion_contract")
        asyncio.run(_seed_release_integrity_contract(admin_database_url))
        run_migrations(database_dsn, services_quiesced=True)
        asyncio.run(_verify_release_integrity_contract(admin_database_url))
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


def test_owner_upgrade_tracks_tables_created_between_source_and_head() -> None:
    """A normal owner can cross revisions that introduce forced-RLS tables."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_multirev_{suffix}"
    database_name = f"inqtrix_multirev_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    owner_url = admin_url.set(
        username=role_name,
        password=password,
        database=database_name,
    )
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        admin_database_dsn = admin_database_url.render_as_string(
            hide_password=False
        )
        owner_dsn = owner_url.render_as_string(hide_password=False)
        run_migrations(admin_database_dsn, revision="0029_agent_run_tree")
        asyncio.run(
            _transfer_schema_ownership(admin_database_url, role_name)
        )

        run_migrations(
            owner_dsn,
            rls_mode="owner",
            services_quiesced=True,
        )

        asyncio.run(_verify_head_rls_inventory(admin_database_url))
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


def test_runtime_contract_rejects_catalog_and_acl_drift() -> None:
    """Runtime readiness fails closed on RLS and dependency drift."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_readiness_{suffix}"
    database_name = f"inqtrix_readiness_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        run_migrations(
            admin_database_url.render_as_string(hide_password=False)
        )

        asyncio.run(
            _verify_runtime_rejects_catalog_and_acl_drift(admin_database_url)
        )
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


@pytest.mark.parametrize(
    "invalid_case",
    ("malformed_approval", "cross_tenant_task"),
)
def test_upgrade_rejects_invalid_legacy_integrity_atomically(
    invalid_case: str,
) -> None:
    """0043/0049 reject malformed legacy state without partial upgrades."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_invalid_{suffix}"
    database_name = f"inqtrix_invalid_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    owner_url = admin_url.set(
        username=role_name,
        password=password,
        database=database_name,
    )
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        admin_database_dsn = admin_database_url.render_as_string(
            hide_password=False
        )
        owner_dsn = owner_url.render_as_string(hide_password=False)
        run_migrations(
            admin_database_dsn,
            revision="0042_agent_session_integrity",
        )
        asyncio.run(
            _transfer_schema_ownership(admin_database_url, role_name)
        )
        asyncio.run(_seed_0042_agent_contract(owner_url))
        if invalid_case == "malformed_approval":
            asyncio.run(_corrupt_0042_approval_payload(owner_url))
            target_revision = "0043_agent_task_contract"
            expected_error = "0043 could not resolve every plan approval"
            expected_revision = "0042_agent_session_integrity"
            protected_table = "run_approvals"
        else:
            run_migrations(
                owner_dsn,
                revision="0048_editor_collaboration",
                rls_mode="owner",
                services_quiesced=True,
            )
            asyncio.run(_corrupt_0048_task_scope(owner_url))
            target_revision = "head"
            expected_error = "0049 found a plan task"
            expected_revision = "0048_editor_collaboration"
            protected_table = "run_plan_tasks"

        with pytest.raises(DBAPIError, match=expected_error):
            run_migrations(
                owner_dsn,
                revision=target_revision,
                rls_mode="owner",
                services_quiesced=True,
            )
        asyncio.run(
            _verify_failed_integrity_upgrade(
                admin_database_url,
                expected_revision=expected_revision,
                protected_table=protected_table,
            )
        )
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


def test_preflight_rejects_additional_permissive_tenant_policy() -> None:
    """A second permissive policy cannot OR-open the tenant predicate."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_policy_{suffix}"
    database_name = f"inqtrix_policy_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    owner_url = admin_url.set(
        username=role_name,
        password=password,
        database=database_name,
    )
    admin_database_url = admin_url.set(database=database_name)

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        admin_database_dsn = admin_database_url.render_as_string(
            hide_password=False
        )
        owner_dsn = owner_url.render_as_string(hide_password=False)
        run_migrations(admin_database_dsn)
        asyncio.run(
            _transfer_schema_ownership(admin_database_url, role_name)
        )
        asyncio.run(_add_extra_permissive_policy(owner_url))

        with pytest.raises(RuntimeError, match="already inconsistent.*runs"):
            run_migrations(
                owner_dsn,
                rls_mode="owner",
                services_quiesced=True,
            )
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )


def test_0048_upgrade_and_downgrade_work_for_non_bypass_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORCE RLS cannot filter a normal external migration owner silently."""
    run_migrations(TEST_DATABASE_URL)
    admin_url = make_url(TEST_DATABASE_URL)
    suffix = uuid.uuid4().hex[:12]
    role_name = f"inqtrix_migration_{suffix}"
    database_name = f"inqtrix_migration_{suffix}"
    password = f"test_{uuid.uuid4().hex}"
    owner_url = admin_url.set(
        username=role_name,
        password=password,
        database=database_name,
    )
    admin_database_url = admin_url.set(database=database_name)
    owners = (uuid.uuid4(), uuid.uuid4())

    try:
        asyncio.run(
            _provision_database(
                admin_url,
                role_name=role_name,
                password=password,
                database_name=database_name,
            )
        )
        owner_dsn = owner_url.render_as_string(hide_password=False)
        admin_database_dsn = admin_database_url.render_as_string(
            hide_password=False
        )
        run_migrations(
            admin_database_dsn,
            revision="0047_resource_sync",
        )
        asyncio.run(
            _transfer_schema_ownership(admin_database_url, role_name)
        )
        asyncio.run(_seed_legacy_comments(owner_url, owners))

        original_invoke = migrate._invoke_alembic

        async def fail_inside_owner_transaction(
            connection: Any,
            **_kwargs: Any,
        ) -> None:
            await connection.execute(
                text(
                    "ALTER TABLE runs ADD COLUMN "
                    "migration_failure_probe text NULL"
                )
            )
            raise RuntimeError("injected migration failure")

        monkeypatch.setattr(
            migrate,
            "_invoke_alembic",
            fail_inside_owner_transaction,
        )
        with pytest.raises(RuntimeError, match="injected migration failure"):
            run_migrations(
                owner_dsn,
                rls_mode="owner",
                services_quiesced=True,
            )
        monkeypatch.setattr(migrate, "_invoke_alembic", original_invoke)
        asyncio.run(_verify_failed_upgrade_rollback(admin_database_url))

        run_migrations(
            owner_dsn,
            rls_mode="owner",
            services_quiesced=True,
        )
        asyncio.run(_verify_upgrade(admin_database_url, owners))

        with pytest.raises(RuntimeError, match="irreversible"):
            downgrade_migrations(
                owner_dsn,
                revision="0047_resource_sync",
                rls_mode="owner",
                services_quiesced=True,
            )
        asyncio.run(_verify_head_rls_inventory(admin_database_url))
    finally:
        asyncio.run(
            _cleanup_database(
                admin_url,
                role_name=role_name,
                database_name=database_name,
            )
        )
