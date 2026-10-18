"""Programmatic Alembic runner and the ``inqtrix-migrate`` entry point.

Wraps the Alembic command API with the packaged migration scripts so
deployments and tests never depend on the working directory containing
``alembic.ini``. The console script is the operator surface:

    INQTRIX_DATABASE_URL=postgresql+asyncpg://... uv run inqtrix-migrate

The CLI resolves its default URL through the
:class:`~inqtrix.settings.StorageSettings` bridge (so ``.env`` works
exactly like it does for ``python -m inqtrix``); the raw-environment
exception remains confined to the Alembic env script, which the bare
``alembic`` CLI loads without any settings bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from inqtrix.storage.migration_contract import (
    MIGRATION_TENANT_RLS_TABLES,
    RUNTIME_REQUIRED_FUNCTIONS,
    RUNTIME_REQUIRED_SEQUENCES,
    RUNTIME_VERSION_TABLE,
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_POLICY,
    TENANT_RLS_TABLES,
    postgres_direct_relation_acl_sql,
    postgres_role_can_set_sql,
    postgres_tenant_table_acl_sql,
    tenant_policy_expression_matches as _tenant_policy_expression_matches,
)

log = logging.getLogger("inqtrix")

_MIGRATIONS_PATH = Path(__file__).parent / "migrations"

MigrationRLSMode = Literal["auto", "owner", "bypass"]
"""Supported migration privilege strategies."""

_MIGRATION_ADVISORY_LOCK_KEY = "inqtrix:schema-migration"
_MIGRATION_LOCK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class MigrationTenantTable:
    """Catalog state for one tenant-scoped relation.

    Attributes:
        name: Unqualified relation name in the active schema.
        owner: PostgreSQL role that owns the relation.
        manageable: Whether the current role inherits the owner's authority.
        row_security: Whether row-level security is enabled.
        force_row_security: Whether row-level security also binds the owner.
        tenant_policy: Whether the canonical tenant policy exists.
        app_acl_valid: Whether ``inqtrix_app`` has the canonical direct grants
            without PUBLIC or application-role column ACLs/grant options.
    """

    name: str
    owner: str
    manageable: bool
    row_security: bool
    force_row_security: bool
    tenant_policy: bool
    app_acl_valid: bool


@dataclass(frozen=True)
class MigrationOwnedObject:
    """Catalog state for a non-table dependency managed by migrations.

    Attributes:
        name: Schema-local function signature or sequence name.
        kind: PostgreSQL object category used in operator diagnostics.
        exists: Whether the expected object resolves in the active schema.
        owner: PostgreSQL role that owns the object, if it exists.
        manageable: Whether the migration role inherits owner authority.
        app_acl_valid: Whether ``inqtrix_app`` has the exact head-revision
            privilege contract and no public function execution remains.
    """

    name: str
    kind: Literal["function", "sequence"]
    exists: bool
    owner: str | None
    manageable: bool
    app_acl_valid: bool


@dataclass(frozen=True)
class MigrationRoleReport:
    """Privilege and schema facts used before every managed migration.

    The report deliberately contains no connection URL or credential data. It
    is safe to surface in operator diagnostics.
    """

    current_user: str
    session_user: str
    server_version_num: int
    is_superuser: bool
    bypass_rls: bool
    can_create_role: bool
    schema_create: bool
    schema_usage: bool
    app_role_exists: bool
    app_role_secure: bool
    app_role_admin: bool
    version_table_exists: bool
    version_table_owner: str | None
    version_table_manageable: bool
    version_app_acl_valid: bool
    schema_revision: tuple[str, ...]
    tenant_tables: tuple[MigrationTenantTable, ...]
    runtime_dependencies: tuple[MigrationOwnedObject, ...]

    @property
    def existing_schema(self) -> bool:
        """Whether this connection targets an installed or partial schema."""
        return self.version_table_exists or bool(self.tenant_tables)

    @property
    def rls_exempt(self) -> bool:
        """Whether forced RLS cannot constrain this migration role."""
        return self.is_superuser or self.bypass_rls

    @property
    def dedicated_bypass(self) -> bool:
        """Whether explicit bypass mode uses a non-superuser BYPASSRLS role."""
        return self.bypass_rls and not self.is_superuser


def _validate_migration_url(database_url: str) -> None:
    """Require a direct async PostgreSQL URL without exposing its contents."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError(
            "migration database URL is not a valid SQLAlchemy URL"
        ) from exc
    if url.get_backend_name() != "postgresql" or url.get_driver_name() != (
        "asyncpg"
    ):
        raise ValueError(
            "migrations require a direct postgresql+asyncpg database URL"
        )


def _normalize_rls_mode(rls_mode: str) -> MigrationRLSMode:
    """Validate and narrow the public migration-mode value."""
    normalized = rls_mode.strip().lower()
    if normalized not in {"auto", "owner", "bypass"}:
        raise ValueError(
            "migration rls_mode must be one of: auto, owner, bypass"
        )
    return cast(MigrationRLSMode, normalized)


def _log_auto_rls_strategy(
    strategy: Literal["fresh", "owner", "bypass"],
    requested_mode: MigrationRLSMode,
) -> None:
    """Warn only when auto mode actually selects an RLS-exempt login."""
    if requested_mode == "auto" and strategy == "bypass":
        log.warning(
            "Migration RLS mode auto selected an RLS-exempt role; configure "
            "owner or bypass explicitly for managed production deployments"
        )


async def _inspect_migration_role(connection: Any) -> MigrationRoleReport:
    """Read role, revision, ownership, and RLS state on one connection."""
    app_version_direct_acl = postgres_direct_relation_acl_sql(
        "relation",
        "app_role.oid",
        expected_privileges_sql="ARRAY['SELECT']::text[]",
    )
    app_tenant_table_acl = postgres_tenant_table_acl_sql(
        "relation", "app_role.oid"
    )
    app_can_set_version_owner = postgres_role_can_set_sql(
        "app_role.oid", "relation.relowner"
    )
    app_can_set_function_owner = postgres_role_can_set_sql(
        "app_role.oid", "routine.proowner"
    )
    app_can_set_sequence_owner = postgres_role_can_set_sql(
        "app_role.oid", "relation.relowner"
    )
    role = (
        await connection.execute(
            text(
                "SELECT current_user AS current_user_name, "
                "session_user AS session_user_name, "
                "current_setting('server_version_num')::integer "
                "AS server_version_num, role.rolsuper, "
                "role.rolbypassrls, role.rolcreaterole, "
                "has_schema_privilege(current_user, current_schema(), "
                "'CREATE') AS schema_create, "
                "has_schema_privilege(current_user, current_schema(), "
                "'USAGE') AS schema_usage, app_role.oid IS NOT NULL "
                "AS app_role_exists, COALESCE(NOT app_role.rolsuper "
                "AND NOT app_role.rolbypassrls AND NOT app_role.rolcanlogin "
                "AND NOT app_role.rolcreatedb AND NOT app_role.rolcreaterole "
                "AND NOT app_role.rolreplication, "
                "false) AS app_role_secure, EXISTS ("
                "SELECT 1 FROM pg_auth_members AS membership "
                "WHERE membership.roleid = app_role.oid "
                "AND membership.member = role.oid "
                "AND membership.admin_option"
                ") AS app_role_admin "
                "FROM pg_roles AS role LEFT JOIN pg_roles AS app_role "
                "ON app_role.rolname = 'inqtrix_app' "
                "WHERE role.rolname = current_user"
            )
        )
    ).mappings().one()
    schema_name = str(
        (await connection.execute(text("SELECT current_schema()"))).scalar_one()
    )
    version = (
        await connection.execute(
            text(
                "SELECT relation.oid IS NOT NULL AS object_exists, "
                "owner.rolname AS owner_name, COALESCE(pg_has_role("
                "current_user, relation.relowner, 'USAGE'), false) "
                "AS manageable, COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'SELECT'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'INSERT'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'UPDATE'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'DELETE'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'TRUNCATE'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'REFERENCES'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'TRIGGER'), false) "
                "AND NOT COALESCE(has_table_privilege(app_role.oid, "
                "relation.oid, 'SELECT WITH GRANT OPTION'), false) "
                "AND EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) "
                "AS acl WHERE acl.grantee = app_role.oid "
                "AND acl.privilege_type = 'SELECT' "
                "AND NOT acl.is_grantable) "
                "AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) "
                "AS acl WHERE acl.privilege_type = 'MAINTAIN' "
                "AND (acl.grantee = 0 OR COALESCE(pg_has_role(app_role.oid, "
                "acl.grantee, 'USAGE'), false))) "
                f"AND NOT COALESCE({app_can_set_version_owner}, false) "
                f"AND {app_version_direct_acl} "
                "AS app_acl_valid "
                "FROM (SELECT CAST(:schema_name AS TEXT) AS schema_name, "
                "CAST(:version_table AS TEXT) AS relation_name) AS expected "
                "LEFT JOIN pg_namespace AS namespace "
                "ON namespace.nspname = expected.schema_name "
                "LEFT JOIN pg_class AS relation "
                "ON relation.relnamespace = namespace.oid "
                "AND relation.relname = expected.relation_name "
                "AND relation.relkind IN ('r', 'p') "
                "LEFT JOIN pg_roles AS owner ON owner.oid = relation.relowner "
                "LEFT JOIN pg_roles AS app_role "
                "ON app_role.rolname = 'inqtrix_app'"
            ),
            {
                "schema_name": schema_name,
                "version_table": RUNTIME_VERSION_TABLE,
            },
        )
    ).mappings().one()
    revision_exists = bool(version["object_exists"])
    schema_revision: tuple[str, ...] = ()
    if revision_exists:
        preparer = connection.dialect.identifier_preparer
        qualified_version_table = (
            f"{preparer.quote(schema_name)}."
            f"{preparer.quote(RUNTIME_VERSION_TABLE)}"
        )
        schema_revision = tuple(
            str(row[0])
            for row in await connection.execute(
                text(
                    f"SELECT version_num FROM {qualified_version_table} "
                    "ORDER BY version_num"
                )
            )
        )
    tenant_rows = (
        await connection.execute(
            text(
                "SELECT relation.relname, owner.rolname AS owner_name, "
                "pg_has_role(current_user, relation.relowner, 'USAGE') "
                "AS manageable, relation.relrowsecurity, "
                "relation.relforcerowsecurity, ("
                "SELECT count(*) FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid"
                ") AS total_policy_count, ("
                "SELECT policy.polcmd::text FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid "
                "AND policy.polname = :policy_name"
                ") AS policy_command, ("
                "SELECT policy.polpermissive FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid "
                "AND policy.polname = :policy_name"
                ") AS policy_permissive, ("
                "SELECT policy.polroles = ARRAY[0::oid] "
                "FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid "
                "AND policy.polname = :policy_name"
                ") AS policy_is_public, ("
                "SELECT pg_get_expr(policy.polqual, policy.polrelid, true) "
                "FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid "
                "AND policy.polname = :policy_name"
                ") AS policy_using, ("
                "SELECT pg_get_expr(policy.polwithcheck, policy.polrelid, true) "
                "FROM pg_policy AS policy "
                "WHERE policy.polrelid = relation.oid "
                "AND policy.polname = :policy_name"
                f") AS policy_check, {app_tenant_table_acl} "
                "AS app_acl_valid "
                "FROM pg_class AS relation "
                "JOIN pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "JOIN pg_roles AS owner ON owner.oid = relation.relowner "
                "LEFT JOIN pg_roles AS app_role "
                "ON app_role.rolname = 'inqtrix_app' "
                "WHERE namespace.nspname = current_schema() "
                "AND relation.relkind IN ('r', 'p') "
                "AND EXISTS ("
                "SELECT 1 FROM pg_attribute AS attribute "
                "WHERE attribute.attrelid = relation.oid "
                "AND attribute.attname = 'tenant_id' "
                "AND attribute.attnum > 0 AND NOT attribute.attisdropped"
                ") ORDER BY relation.relname"
            ),
            {"policy_name": TENANT_RLS_POLICY},
        )
    ).mappings()
    tables = tuple(
        MigrationTenantTable(
            name=str(row["relname"]),
            owner=str(row["owner_name"]),
            manageable=bool(row["manageable"]),
            row_security=bool(row["relrowsecurity"]),
            force_row_security=bool(row["relforcerowsecurity"]),
            tenant_policy=(
                int(row["total_policy_count"]) == 1
                and str(row["policy_command"]) == "*"
                and bool(row["policy_permissive"])
                and bool(row["policy_is_public"])
                and _tenant_policy_expression_matches(row["policy_using"])
                and _tenant_policy_expression_matches(row["policy_check"])
            ),
            app_acl_valid=bool(row["app_acl_valid"]),
        )
        for row in tenant_rows
    )
    function_rows = (
        await connection.execute(
            text(
                "WITH expected(function_signature) AS ("
                "SELECT unnest(CAST(:function_signatures AS TEXT[]))"
                "), resolved AS ("
                "SELECT function_signature, to_regprocedure(format("
                "'%I.%s', current_schema(), function_signature)) "
                "AS function_oid FROM expected"
                ") SELECT resolved.function_signature, "
                "routine.oid IS NOT NULL AS object_exists, "
                "owner.rolname AS owner_name, COALESCE(pg_has_role("
                "current_user, routine.proowner, 'USAGE'), false) "
                "AS manageable, COALESCE(has_function_privilege("
                "app_role.oid, routine.oid, 'EXECUTE'), false) "
                "AND NOT COALESCE(has_function_privilege(app_role.oid, "
                "routine.oid, 'EXECUTE WITH GRANT OPTION'), false) "
                "AND EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "routine.proacl, acldefault('f', routine.proowner))) AS acl "
                "WHERE acl.grantee = app_role.oid "
                "AND acl.privilege_type = 'EXECUTE' "
                "AND NOT acl.is_grantable) "
                "AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "routine.proacl, acldefault('f', routine.proowner))) AS acl "
                "WHERE acl.grantee = 0 "
                "AND acl.privilege_type = 'EXECUTE') "
                f"AND NOT COALESCE({app_can_set_function_owner}, false) "
                "AS app_acl_valid "
                "FROM resolved LEFT JOIN pg_proc AS routine "
                "ON routine.oid = resolved.function_oid "
                "LEFT JOIN pg_roles AS owner ON owner.oid = routine.proowner "
                "LEFT JOIN pg_roles AS app_role "
                "ON app_role.rolname = 'inqtrix_app' "
                "ORDER BY resolved.function_signature"
            ),
            {"function_signatures": list(RUNTIME_REQUIRED_FUNCTIONS)},
        )
    ).mappings()
    sequence_rows = (
        await connection.execute(
            text(
                "WITH expected(sequence_name) AS ("
                "SELECT unnest(CAST(:sequence_names AS TEXT[]))"
                ") SELECT expected.sequence_name, "
                "relation.oid IS NOT NULL AS object_exists, "
                "owner.rolname AS owner_name, COALESCE(pg_has_role("
                "current_user, relation.relowner, 'USAGE'), false) "
                "AS manageable, COALESCE(has_sequence_privilege("
                "app_role.oid, relation.oid, 'USAGE'), false) "
                "AND NOT COALESCE(has_sequence_privilege(app_role.oid, "
                "relation.oid, 'SELECT'), false) "
                "AND NOT COALESCE(has_sequence_privilege(app_role.oid, "
                "relation.oid, 'UPDATE'), false) "
                "AND NOT COALESCE(has_sequence_privilege(app_role.oid, "
                "relation.oid, 'USAGE WITH GRANT OPTION'), false) "
                "AND EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('s', relation.relowner))) "
                "AS acl WHERE acl.grantee = app_role.oid "
                "AND acl.privilege_type = 'USAGE' "
                "AND NOT acl.is_grantable) "
                "AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('s', relation.relowner))) "
                "AS acl WHERE acl.grantee = 0) "
                f"AND NOT COALESCE({app_can_set_sequence_owner}, false) "
                "AS app_acl_valid FROM expected "
                "LEFT JOIN pg_namespace AS namespace "
                "ON namespace.nspname = current_schema() "
                "LEFT JOIN pg_class AS relation "
                "ON relation.relnamespace = namespace.oid "
                "AND relation.relname = expected.sequence_name "
                "AND relation.relkind = 'S' "
                "LEFT JOIN pg_roles AS owner ON owner.oid = relation.relowner "
                "LEFT JOIN pg_roles AS app_role "
                "ON app_role.rolname = 'inqtrix_app' "
                "ORDER BY expected.sequence_name"
            ),
            {"sequence_names": list(RUNTIME_REQUIRED_SEQUENCES)},
        )
    ).mappings()
    dependencies = tuple(
        MigrationOwnedObject(
            name=str(row["function_signature"]),
            kind="function",
            exists=bool(row["object_exists"]),
            owner=(
                str(row["owner_name"])
                if row["owner_name"] is not None
                else None
            ),
            manageable=bool(row["manageable"]),
            app_acl_valid=bool(row["app_acl_valid"]),
        )
        for row in function_rows
    ) + tuple(
        MigrationOwnedObject(
            name=str(row["sequence_name"]),
            kind="sequence",
            exists=bool(row["object_exists"]),
            owner=(
                str(row["owner_name"])
                if row["owner_name"] is not None
                else None
            ),
            manageable=bool(row["manageable"]),
            app_acl_valid=bool(row["app_acl_valid"]),
        )
        for row in sequence_rows
    )
    return MigrationRoleReport(
        current_user=str(role["current_user_name"]),
        session_user=str(role["session_user_name"]),
        server_version_num=int(role["server_version_num"]),
        is_superuser=bool(role["rolsuper"]),
        bypass_rls=bool(role["rolbypassrls"]),
        can_create_role=bool(role["rolcreaterole"]),
        schema_create=bool(role["schema_create"]),
        schema_usage=bool(role["schema_usage"]),
        app_role_exists=bool(role["app_role_exists"]),
        app_role_secure=bool(role["app_role_secure"]),
        app_role_admin=bool(role["app_role_admin"]),
        version_table_exists=revision_exists,
        version_table_owner=(
            str(version["owner_name"])
            if version["owner_name"] is not None
            else None
        ),
        version_table_manageable=bool(version["manageable"]),
        version_app_acl_valid=bool(version["app_acl_valid"]),
        schema_revision=schema_revision,
        tenant_tables=tables,
        runtime_dependencies=dependencies,
    )


def _required_runtime_dependency_names(
    report: MigrationRoleReport,
) -> set[str]:
    """Resolve dependencies that must exist at the report's source revision."""
    table_names = {table.name for table in report.tenant_tables}
    required = set(RUNTIME_REQUIRED_FUNCTIONS) if table_names else set()
    if "audit_log" in table_names:
        required.add("audit_log_id_seq")
    if "user_events" in table_names:
        required.add("user_events_id_seq")
    return required


def _assert_migration_role(
    report: MigrationRoleReport,
    *,
    rls_mode: MigrationRLSMode,
    services_quiesced: bool,
) -> Literal["fresh", "owner", "bypass"]:
    """Select one safe execution strategy or fail before schema mutation.

    ``auto`` deliberately never assumes owner maintenance for an installed
    database. The operator must opt into the maintenance window explicitly.
    A fresh database has no tenant rows to expose, so it can be installed by a
    normal schema owner without a service-quiescence assertion.
    """
    if report.server_version_num < 150000:
        raise RuntimeError(
            "Inqtrix schema migrations require PostgreSQL 15 or newer"
        )
    if not report.schema_usage or not report.schema_create:
        raise RuntimeError(
            "migration role requires USAGE and CREATE on the active schema"
        )
    if report.existing_schema and not report.app_role_exists:
        raise RuntimeError(
            "installed schema is missing the required inqtrix_app role; "
            "restore the NOLOGIN NOBYPASSRLS role and runtime memberships "
            "before migrating"
        )
    if report.app_role_exists and not report.app_role_secure:
        raise RuntimeError(
            "existing inqtrix_app role must be NOLOGIN, NOSUPERUSER, "
            "NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, and NOREPLICATION "
            "before migrations may run"
        )
    unexpected = tuple(
        table.name
        for table in report.tenant_tables
        if table.name not in MIGRATION_TENANT_RLS_TABLES
    )
    if unexpected:
        raise RuntimeError(
            "active schema contains tenant_id tables outside the packaged "
            "migration inventory: " + ", ".join(unexpected)
        )
    unmanaged = tuple(
        table.name for table in report.tenant_tables if not table.manageable
    )
    if unmanaged and not report.is_superuser:
        raise RuntimeError(
            "migration role does not own or inherit ownership of tenant "
            f"tables: {', '.join(unmanaged)}"
        )
    if (
        report.version_table_exists
        and not report.version_table_manageable
        and not report.is_superuser
    ):
        raise RuntimeError(
            "migration role does not own or inherit ownership of the active "
            "schema's alembic_version table"
        )
    dependency_by_name = {
        dependency.name: dependency
        for dependency in report.runtime_dependencies
    }
    missing_dependencies = tuple(
        sorted(
            dependency_name
            for dependency_name in _required_runtime_dependency_names(report)
            if dependency_name not in dependency_by_name
            or not dependency_by_name[dependency_name].exists
        )
    )
    if missing_dependencies:
        raise RuntimeError(
            "installed schema is missing required migration dependency "
            "objects: " + ", ".join(missing_dependencies)
        )
    unmanaged_dependencies = tuple(
        dependency.name
        for dependency in report.runtime_dependencies
        if dependency.exists and not dependency.manageable
    )
    if unmanaged_dependencies and not report.is_superuser:
        raise RuntimeError(
            "migration role does not own or inherit ownership of runtime "
            "dependency objects: " + ", ".join(unmanaged_dependencies)
        )
    drifted = tuple(
        table.name
        for table in report.tenant_tables
        if not (
            table.row_security
            and table.force_row_security
            and table.tenant_policy
        )
    )
    if drifted:
        raise RuntimeError(
            "tenant RLS contract is already inconsistent before migration: "
            + ", ".join(drifted)
        )
    if not report.existing_schema:
        if not report.app_role_exists and not (
            report.is_superuser or report.can_create_role
        ):
            raise RuntimeError(
                "fresh installation cannot create the required inqtrix_app "
                "role; ask the database administrator to pre-create it as "
                "NOLOGIN NOSUPERUSER NOBYPASSRLS and grant it to the "
                "migration role WITH ADMIN OPTION"
            )
        if report.app_role_exists and not (
            report.is_superuser or report.app_role_admin
        ):
            raise RuntimeError(
                "fresh installation requires ADMIN OPTION on the pre-created "
                "inqtrix_app role because revision 0001 grants that role to "
                "the migration login"
            )
        if rls_mode == "bypass" and not report.dedicated_bypass:
            raise RuntimeError(
                "migration rls_mode='bypass' requires a dedicated "
                "NOSUPERUSER BYPASSRLS role"
            )
        if rls_mode == "bypass":
            return "bypass"
        return "bypass" if report.rls_exempt else "fresh"
    if rls_mode == "auto":
        if report.rls_exempt:
            return "bypass"
        raise RuntimeError(
            "installed schema uses FORCE ROW LEVEL SECURITY but the migration "
            "role is neither SUPERUSER nor BYPASSRLS; choose rls_mode='owner' "
            "with services_quiesced=True or use a dedicated BYPASSRLS "
            "migration role"
        )
    if rls_mode == "bypass":
        if not report.dedicated_bypass:
            raise RuntimeError(
                "migration rls_mode='bypass' requires a dedicated "
                "NOSUPERUSER BYPASSRLS role"
            )
        return "bypass"
    if not services_quiesced:
        raise RuntimeError(
            "migration rls_mode='owner' on an installed schema requires "
            "services_quiesced=True after API, worker, and collaboration "
            "database sessions have been drained"
        )
    return "owner"


@dataclass(frozen=True)
class AuthorityReferenceIssue:
    """Unmappable legacy subject references in one authority column.

    Attributes:
        source: Qualified legacy authority column.
        orphaned: Non-null references matching no user in the same tenant.
        ambiguous: References matching more than one issuer-scoped user.
    """

    source: str
    orphaned: int
    ambiguous: int


@dataclass(frozen=True)
class V02PreflightReport:
    """Read-only readiness report for the destructive v0.2 cutover.

    ``ready`` is deliberately derived from blocking facts rather than stored:
    a report cannot claim success while a newly added blocker is non-zero.
    Non-terminal work is a maintenance action, not a schema ambiguity, but it
    still blocks migration until the operator terminates it explicitly. An
    orphaned active share resource is reported but is not a blocker because
    migration 0045 revokes exactly those rows under its table locks.
    """

    schema_revision: tuple[str, ...]
    authority_issues: tuple[AuthorityReferenceIssue, ...]
    unsupported_active_shares: int
    orphaned_active_share_resources: int
    nonterminal_runs: int
    nonterminal_reindex_jobs: int
    required_tables_present: bool
    legacy_schema_compatible: bool

    @property
    def ready(self) -> bool:
        """Whether the database can enter the v0.2 migration safely."""
        return (
            self.required_tables_present
            and self.legacy_schema_compatible
            and not self.authority_issues
            and self.unsupported_active_shares == 0
            and self.nonterminal_runs == 0
            and self.nonterminal_reindex_jobs == 0
        )

    def as_json_dict(self) -> dict[str, Any]:
        """Return the stable operator-facing JSON representation."""
        payload = asdict(self)
        payload["ready"] = self.ready
        return payload


@dataclass(frozen=True)
class V02TerminalizationReport:
    """Result of the explicit pre-v0.2 maintenance operation.

    Attributes:
        runs_terminalized: Legacy non-terminal runs moved to ``failed``.
        reindex_jobs_terminalized: Legacy non-terminal reindex jobs moved to
            ``failed``.
        reason: Stable machine-readable reason stored in every error payload.
    """

    runs_terminalized: int
    reindex_jobs_terminalized: int
    reason: str = "platform_upgrade"

    def as_json_dict(self) -> dict[str, Any]:
        """Return the stable operator-facing JSON representation."""
        return asdict(self)


_V02LockedAction = Callable[[Any, V02PreflightReport], Awaitable[Any]]


_V02_MIGRATION_MODULE = (
    "inqtrix.storage.migrations.versions.0045_canonical_user_ids"
)
_V02_LEGACY_REVISION = "0044_agent_task_cancellation"


def _v02_migration_contract() -> Any:
    """Load migration 0045 as the preflight's single schema inventory.

    The operator preflight and the locked migration must never maintain two
    authority-column lists. Importing the migration module is deliberate: the
    CLI audits the exact destructive revision it is about to execute.
    """
    return importlib.import_module(_V02_MIGRATION_MODULE)


def _required_legacy_columns(migration: Any) -> dict[str, set[str]]:
    """Return the minimum 0044 columns needed for a safe 0045 target.

    This includes columns transformed by 0045 and preserved contract fields
    that 0045 deliberately does not recreate, notably share consent and
    lifecycle timestamps.
    """
    required: dict[str, set[str]] = {
        "users": {"id", "tenant_id", "issuer", "subject"},
        "auth_sessions": {"tenant_id", "issuer", "sub"},
        "personal_access_tokens": {
            "tenant_id",
            "owner_issuer",
            "owner_sub",
        },
        "local_credentials": {"tenant_id", "subject"},
        "resource_shares": {
            "id",
            "tenant_id",
            "subject_type",
            "subject_id",
            "resource_type",
            "resource_id",
            "permission",
            "granted_by_sub",
            "revoked_by_sub",
            "created_at",
            "accepted_at",
            "revoked_at",
        },
        "runs": {
            "run_id",
            "tenant_id",
            "status",
            "snapshot",
            "finished_at",
            "error",
            "event_seq",
        },
        "run_events": {
            "run_id",
            "sequence",
            "tenant_id",
            "type",
            "created_at",
            "data",
        },
        "indexing_jobs": {
            "job_id",
            "tenant_id",
            "operation_kind",
            "document_id",
            "revision_id",
            "status",
            "total_documents",
            "completed_documents",
            "current_document_title",
            "finished_at",
            "error",
            "event_seq",
        },
        "indexing_job_events": {
            "job_id",
            "sequence",
            "tenant_id",
            "type",
            "created_at",
            "data",
        },
        "alembic_version": {"version_num"},
    }
    for spec in (*migration._AUTHORITY_COLUMNS, *migration._SHARE_AUTHORITY_COLUMNS):
        required.setdefault(spec.table, {"tenant_id"}).add(spec.legacy)
    for _resource_type, (table_name, id_column) in (
        migration._SHARE_RESOURCE_TABLES.items()
    ):
        required.setdefault(table_name, {"tenant_id"}).add(id_column)
    return required


async def _lock_v02_cutover_tables(connection: Any, migration: Any) -> None:
    """Acquire the fail-fast lock boundary for destructive v0.2 work.

    Args:
        connection: Connection inside the cutover transaction.
        migration: Loaded 0045 migration contract providing its table
            inventory.

    Raises:
        DBAPIError: If a table is missing, already in use, or cannot be locked.
    """
    await connection.execute(text("SET LOCAL row_security = off"))
    await connection.execute(
        text(
            "LOCK TABLE alembic_version, "
            f"{migration._LOCK_TABLES}, run_events, "
            "indexing_job_events "
            "IN ACCESS EXCLUSIVE MODE NOWAIT"
        )
    )


async def _v02_preflight(
    database_url: str,
    *,
    locked_action: _V02LockedAction | None = None,
    managed_rls_mode: MigrationRLSMode | None = None,
    services_quiesced: bool = False,
) -> Any:
    """Inspect one legacy database, optionally acting under the same locks.

    Normal preflight calls are read-only. The destructive maintenance path
    supplies ``locked_action``: every table inspected by migration 0045 plus
    both lifecycle event tables and ``alembic_version`` is then locked before
    the audit, and the callback runs in that same transaction. This is the
    single TOCTOU-free seam for the v0.2 cutover.
    """
    from inqtrix.storage.db import build_engine

    migration = _v02_migration_contract()
    engine = build_engine(database_url, null_pool=True)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                owner_tables: tuple[str, ...] = ()
                if managed_rls_mode is not None:
                    role_report = await _inspect_migration_role(connection)
                    strategy = _assert_migration_role(
                        role_report,
                        rls_mode=managed_rls_mode,
                        services_quiesced=services_quiesced,
                    )
                    await _acquire_migration_advisory_lock(connection)
                    if strategy == "owner":
                        owner_tables = await _begin_owner_rls_maintenance(
                            connection,
                            role_report,
                        )
                elif locked_action is None:
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                if locked_action is not None:
                    await _lock_v02_cutover_tables(connection, migration)
                rows = await connection.execute(
                    text(
                        "SELECT table_name, column_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema()"
                    )
                )
                columns: dict[str, set[str]] = {}
                for table_name, column_name in rows:
                    columns.setdefault(str(table_name), set()).add(
                        str(column_name)
                    )

                required_columns = _required_legacy_columns(migration)
                required_tables = {
                    *migration._REQUIRED_TABLES,
                    *required_columns,
                }
                required_tables_present = required_tables.issubset(columns)
                required_columns_present = all(
                    expected.issubset(columns.get(table_name, set()))
                    for table_name, expected in required_columns.items()
                )

                schema_revision: tuple[str, ...] = ()
                if "alembic_version" in columns:
                    revisions = await connection.execute(
                        text(
                            "SELECT version_num FROM alembic_version "
                            "ORDER BY version_num"
                        )
                    )
                    schema_revision = tuple(str(row[0]) for row in revisions)
                legacy_schema_compatible = schema_revision == (
                    _V02_LEGACY_REVISION,
                ) and required_columns_present
                if legacy_schema_compatible:
                    relation_rows = await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = current_schema() AND "
                            "indexname IN ('uq_users_issuer_subject', "
                            "'uq_resource_shares_active')"
                        )
                    )
                    legacy_schema_compatible = {
                        str(row[0]) for row in relation_rows
                    } == {
                        "uq_users_issuer_subject",
                        "uq_resource_shares_active",
                    }

                authority_issues: list[AuthorityReferenceIssue] = []
                if {"tenant_id", "subject"}.issubset(
                    columns.get("users", set())
                ):
                    for spec in (
                        *migration._AUTHORITY_COLUMNS,
                        *migration._SHARE_AUTHORITY_COLUMNS,
                    ):
                        table_columns = columns.get(spec.table, set())
                        if not {"tenant_id", spec.legacy}.issubset(
                            table_columns
                        ):
                            continue
                        conditions = [f't."{spec.legacy}" IS NOT NULL']
                        conditions.append(f"({spec.predicate})")
                        statement = text(
                            "SELECT "
                            "count(*) FILTER (WHERE matches = 0) AS orphaned, "
                            "count(*) FILTER (WHERE matches > 1) AS ambiguous "
                            "FROM ("
                            f'SELECT (SELECT count(*) FROM users AS u '
                            f'WHERE u.tenant_id = t.tenant_id '
                            f'AND u.subject = t."{spec.legacy}") AS matches '
                            f'FROM "{spec.table}" AS t '
                            f"WHERE {' AND '.join(conditions)}"
                            ") AS authority_refs"
                        )
                        issue_row = (
                            await connection.execute(statement)
                        ).one()
                        orphaned = int(issue_row.orphaned or 0)
                        ambiguous = int(issue_row.ambiguous or 0)
                        if orphaned or ambiguous:
                            authority_issues.append(
                                AuthorityReferenceIssue(
                                    source=f"{spec.table}.{spec.legacy}",
                                    orphaned=orphaned,
                                    ambiguous=ambiguous,
                                )
                            )

                    exact_authorities = {
                        "auth_sessions": (
                            "sub",
                            "u.issuer = t.issuer",
                        ),
                        "personal_access_tokens": (
                            "owner_sub",
                            "u.issuer = t.owner_issuer",
                        ),
                        "local_credentials": (
                            "subject",
                            "u.issuer = 'local'",
                        ),
                    }
                    if set(exact_authorities) != set(
                        migration._EXACT_AUTHORITY_TABLES
                    ):
                        raise RuntimeError(
                            "v0.2 preflight exact-authority inventory drifted "
                            "from migration 0045"
                        )
                    for table_name, (
                        column_name,
                        issuer_predicate,
                    ) in exact_authorities.items():
                        if not {"tenant_id", column_name}.issubset(
                            columns.get(table_name, set())
                        ):
                            continue
                        exact_row = (
                            await connection.execute(
                                text(
                                    "SELECT "
                                    "count(*) FILTER (WHERE matches = 0) "
                                    "AS orphaned, "
                                    "count(*) FILTER (WHERE matches > 1) "
                                    "AS ambiguous "
                                    "FROM ("
                                    "SELECT (SELECT count(*) FROM users AS u "
                                    "WHERE u.tenant_id = t.tenant_id AND "
                                    f"{issuer_predicate} AND "
                                    f'u.subject = t."{column_name}") AS matches '
                                    f'FROM "{table_name}" AS t '
                                    f'WHERE t."{column_name}" IS NOT NULL'
                                    ") AS authority_refs"
                                )
                            )
                        ).one()
                        orphaned = int(exact_row.orphaned or 0)
                        ambiguous = int(exact_row.ambiguous or 0)
                        if orphaned or ambiguous:
                            authority_issues.append(
                                AuthorityReferenceIssue(
                                    source=f"{table_name}.{column_name}",
                                    orphaned=orphaned,
                                    ambiguous=ambiguous,
                                )
                            )

                unsupported_active_shares = 0
                orphaned_active_share_resources = 0
                share_columns = columns.get("resource_shares", set())
                legacy_share_columns = {
                    "subject_type",
                    "resource_type",
                    "resource_id",
                    "permission",
                    "revoked_at",
                }
                if legacy_share_columns.issubset(share_columns):
                    supported_types = ", ".join(
                        f"'{resource_type}'"
                        for resource_type in migration._SHARE_RESOURCE_TABLES
                    )
                    unsupported_active_shares = int(
                        (
                            await connection.execute(
                                text(
                                    "SELECT count(*) FROM resource_shares "
                                    "WHERE revoked_at IS NULL AND ("
                                    "subject_type <> 'user' OR "
                                    f"resource_type NOT IN ({supported_types}) OR "
                                    "permission NOT IN ('view', 'edit'))"
                                )
                            )
                        ).scalar_one()
                    )
                    orphan_terms: list[str] = []
                    for resource_type, (table_name, id_column) in (
                        migration._SHARE_RESOURCE_TABLES.items()
                    ):
                        if id_column not in columns.get(table_name, set()):
                            continue
                        orphan_terms.append(
                            "(s.resource_type = "
                            f"'{resource_type}' AND NOT EXISTS ("
                            f'SELECT 1 FROM "{table_name}" AS r '
                            "WHERE r.tenant_id = s.tenant_id "
                            f'AND r."{id_column}"::text = s.resource_id))'
                        )
                    if orphan_terms:
                        orphaned_active_share_resources = int(
                            (
                                await connection.execute(
                                    text(
                                        "SELECT count(*) FROM resource_shares AS s "
                                        "WHERE s.revoked_at IS NULL AND ("
                                        + " OR ".join(orphan_terms)
                                        + ")"
                                    )
                                )
                            ).scalar_one()
                        )

                nonterminal_runs = 0
                if {"status"}.issubset(columns.get("runs", set())):
                    nonterminal_runs = int(
                        (
                            await connection.execute(
                                text(
                                    "SELECT count(*) FROM runs WHERE status NOT IN "
                                    "('completed', 'failed', 'cancelled')"
                                )
                            )
                        ).scalar_one()
                    )
                nonterminal_reindex_jobs = 0
                if {"status"}.issubset(columns.get("indexing_jobs", set())):
                    nonterminal_reindex_jobs = int(
                        (
                            await connection.execute(
                                text(
                                    "SELECT count(*) FROM indexing_jobs "
                                    "WHERE status NOT IN "
                                    "('completed', 'failed', 'cancelled')"
                                )
                            )
                        ).scalar_one()
                    )

                report = V02PreflightReport(
                    schema_revision=schema_revision,
                    authority_issues=tuple(authority_issues),
                    unsupported_active_shares=unsupported_active_shares,
                    orphaned_active_share_resources=(
                        orphaned_active_share_resources
                    ),
                    nonterminal_runs=nonterminal_runs,
                    nonterminal_reindex_jobs=nonterminal_reindex_jobs,
                    required_tables_present=required_tables_present,
                    legacy_schema_compatible=legacy_schema_compatible,
                )
                result = (
                    await locked_action(connection, report)
                    if locked_action is not None
                    else report
                )
                await _restore_owner_rls(connection, owner_tables)
                return result
    finally:
        await engine.dispose()


def preflight_v02(
    database_url: str,
    *,
    rls_mode: MigrationRLSMode | str = "auto",
    services_quiesced: bool = False,
) -> V02PreflightReport:
    """Run the v0.2 readiness audit under the migration role contract."""
    return asyncio.run(
        _v02_preflight(
            database_url,
            managed_rls_mode=_normalize_rls_mode(rls_mode),
            services_quiesced=services_quiesced,
        )
    )


_V02_PLATFORM_UPGRADE_REASON = "platform_upgrade"
_V02_RUN_UPGRADE_ERROR = {
    "message": "Run stopped for the v0.2 platform upgrade.",
    "type": _V02_PLATFORM_UPGRADE_REASON,
}
_V02_INDEX_UPGRADE_ERROR = {
    "message": "Reindex job stopped for the v0.2 platform upgrade.",
    "type": _V02_PLATFORM_UPGRADE_REASON,
}


def _status_values_sql(statuses: set[str]) -> str:
    """Render an internal enum value set as a trusted SQL literal list."""
    if not statuses or any(not status.isidentifier() for status in statuses):
        raise RuntimeError("invalid internal terminal status contract")
    return "(" + ", ".join(f"'{status}'" for status in sorted(statuses)) + ")"


def _v02_run_terminal_statuses_sql() -> str:
    """Return the run terminal statuses used by legacy terminalization."""
    from inqtrix.server.runs import TERMINAL_RUN_STATUSES

    return _status_values_sql(
        {status.value for status in TERMINAL_RUN_STATUSES}
    )


def _v02_index_terminal_statuses_sql() -> str:
    """Return the indexing terminal statuses used by legacy terminalization.

    Runs and indexing jobs are independent lifecycle machines. In particular,
    indexing can terminate in a deliberately published raw generation, which
    has no corresponding run state. Keeping the predicates separate prevents
    upgrade maintenance from rewriting a valid terminal index job.
    """
    from inqtrix.server.indexing import TERMINAL_INDEXING_STATUSES

    return _status_values_sql(
        {status.value for status in TERMINAL_INDEXING_STATUSES}
    )


def _v02_run_failure_events(
    snapshot: Mapping[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Build the standard snapshot-plus-failure event sequence for one run."""
    from inqtrix.runs.shared import expand_run_event

    return expand_run_event(
        "inqtrix.run.failed",
        {
            "status": "failed",
            "error": dict(_V02_RUN_UPGRADE_ERROR),
            "snapshot": dict(snapshot or {}),
        },
        status="failed",
    )[1]


def _v02_index_failure_payload(
    row: Mapping[str, Any],
    *,
    finished_at: float,
) -> dict[str, Any]:
    """Build the normal indexing failure payload from a locked legacy row."""
    from inqtrix.server.indexing import (
        IndexingJobRecord,
        IndexingJobStatus,
        build_indexing_job_summary,
    )

    record = IndexingJobRecord(
        job_id=str(row["job_id"]),
        collection_id="",
        collection_name="",
        embedding_model="",
        created_at=0.0,
        status=IndexingJobStatus.FAILED,
        finished_at=finished_at,
        total_documents=int(row["total_documents"] or 0),
        completed_documents=int(row["completed_documents"] or 0),
        current_document_title=str(row["current_document_title"] or ""),
        error=dict(_V02_INDEX_UPGRADE_ERROR),
    )
    snapshot = build_indexing_job_summary(record)["snapshot"]
    return {
        "status": "failed",
        "error": dict(_V02_INDEX_UPGRADE_ERROR),
        "snapshot": snapshot,
    }


def _assert_v02_terminalization_preflight(report: V02PreflightReport) -> None:
    """Refuse destructive maintenance while any non-work blocker remains."""
    without_live_work = replace(
        report,
        nonterminal_runs=0,
        nonterminal_reindex_jobs=0,
    )
    if not without_live_work.ready:
        raise RuntimeError(
            "v0.2 work terminalization refused: preflight has blockers "
            "other than non-terminal runs or reindex jobs"
        )


async def _assert_v02_database_quiescent(connection: Any) -> None:
    """Require the cutover transaction to be the database's only client.

    This database-level evidence complements the operator's explicit service
    shutdown assertion. It intentionally counts idle client backends too, so
    API/worker pools and transaction poolers must be drained. PostgreSQL cannot
    prove that a completely disconnected process has stopped; that process
    boundary remains the operator's responsibility.

    Args:
        connection: Direct PostgreSQL connection holding the cutover locks.

    Raises:
        RuntimeError: If any other client backend is attached to the database.
    """
    other_sessions = int(
        (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datid = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database()) "
                    "AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend'"
                )
            )
        ).scalar_one()
    )
    if other_sessions:
        raise RuntimeError(
            "v0.2 work terminalization refused: found "
            f"{other_sessions} other database client session(s); stop every "
            "API and worker process and drain any connection pooler first"
        )


async def _terminalize_v02_locked(
    connection: Any,
    preflight: V02PreflightReport,
    *,
    run_terminal_statuses_sql: str,
    indexing_terminal_statuses_sql: str,
) -> V02TerminalizationReport:
    """Persist every legacy terminal transition under the preflight locks.

    The caller must invoke this helper as the locked action of
    :func:`_v02_preflight`. Any inventory mismatch or event-write failure then
    rolls back both run and reindex transitions together.

    Args:
        connection: Connection inside the locked preflight transaction.
        preflight: Report computed by that same transaction.
        run_terminal_statuses_sql: Trusted SQL literal generated from the run
            status contract.
        indexing_terminal_statuses_sql: Trusted SQL literal generated from the
            independent indexing status contract.

    Returns:
        Counts of terminalized runs and reindex jobs.

    Raises:
        RuntimeError: If preflight, quiescence, or locked inventories disagree.
    """
    _assert_v02_terminalization_preflight(preflight)
    await _assert_v02_database_quiescent(connection)

    run_rows = (
        await connection.execute(
            text(
                "SELECT run_id, tenant_id, snapshot FROM runs "
                f"WHERE status NOT IN {run_terminal_statuses_sql} "
                "ORDER BY run_id FOR UPDATE"
            )
        )
    ).mappings().all()
    if len(run_rows) != preflight.nonterminal_runs:
        raise RuntimeError(
            "v0.2 work terminalization refused: locked run inventory "
            "does not match the preflight count"
        )
    indexing_rows = (
        await connection.execute(
            text(
                "SELECT job_id, tenant_id, total_documents, "
                "completed_documents, current_document_title "
                "FROM indexing_jobs "
                f"WHERE status NOT IN {indexing_terminal_statuses_sql} "
                "ORDER BY job_id FOR UPDATE"
            )
        )
    ).mappings().all()
    if len(indexing_rows) != preflight.nonterminal_reindex_jobs:
        raise RuntimeError(
            "v0.2 work terminalization refused: locked reindex inventory "
            "does not match the preflight count"
        )

    now = time.time()
    run_update = text(
        "UPDATE runs SET status = 'failed', finished_at = :finished_at, "
        "error = CAST(:error_json AS json), "
        "event_seq = event_seq + :event_count "
        "WHERE run_id = :run_id AND status NOT IN "
        f"{run_terminal_statuses_sql} RETURNING event_seq"
    )
    run_event_insert = text(
        "INSERT INTO run_events "
        "(run_id, sequence, tenant_id, type, created_at, data) "
        "VALUES (:run_id, :sequence, :tenant_id, :event_type, "
        ":created_at, CAST(:data_json AS json))"
    )
    for row in run_rows:
        events = _v02_run_failure_events(row["snapshot"])
        final_sequence = int(
            (
                await connection.execute(
                    run_update,
                    {
                        "run_id": row["run_id"],
                        "finished_at": now,
                        "error_json": json.dumps(
                            _V02_RUN_UPGRADE_ERROR,
                            ensure_ascii=False,
                        ),
                        "event_count": len(events),
                    },
                )
            ).scalar_one()
        )
        first_sequence = final_sequence - len(events) + 1
        for offset, (event_type, data) in enumerate(events):
            await connection.execute(
                run_event_insert,
                {
                    "run_id": row["run_id"],
                    "sequence": first_sequence + offset,
                    "tenant_id": row["tenant_id"],
                    "event_type": event_type,
                    "created_at": now,
                    "data_json": json.dumps(data, ensure_ascii=False),
                },
            )

    indexing_update = text(
        "UPDATE indexing_jobs SET status = 'failed', "
        "finished_at = :finished_at, error = CAST(:error_json AS json), "
        "event_seq = event_seq + 1 "
        "WHERE job_id = :job_id AND status NOT IN "
        f"{indexing_terminal_statuses_sql} RETURNING event_seq"
    )
    indexing_event_insert = text(
        "INSERT INTO indexing_job_events "
        "(job_id, sequence, tenant_id, type, created_at, data) "
        "VALUES (:job_id, :sequence, :tenant_id, "
        "'inqtrix.index.failed', :created_at, "
        "CAST(:data_json AS json))"
    )
    for row in indexing_rows:
        sequence = int(
            (
                await connection.execute(
                    indexing_update,
                    {
                        "job_id": row["job_id"],
                        "finished_at": now,
                        "error_json": json.dumps(
                            _V02_INDEX_UPGRADE_ERROR,
                            ensure_ascii=False,
                        ),
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            indexing_event_insert,
            {
                "job_id": row["job_id"],
                "sequence": sequence,
                "tenant_id": row["tenant_id"],
                "created_at": now,
                "data_json": json.dumps(
                    _v02_index_failure_payload(
                        row,
                        finished_at=now,
                    ),
                    ensure_ascii=False,
                ),
            },
        )

    return V02TerminalizationReport(
        runs_terminalized=len(run_rows),
        reindex_jobs_terminalized=len(indexing_rows),
    )


async def _terminalize_v02_legacy_work(
    database_url: str,
    *,
    rls_mode: MigrationRLSMode = "auto",
) -> V02TerminalizationReport:
    """Fail legacy in-flight work and append terminal events atomically.

    Every table inspected by the 0045 preflight plus both lifecycle event
    tables and ``alembic_version`` is locked exclusively with ``NOWAIT``.
    Preflight, quiescence proof, run transitions, reindex transitions, and all
    terminal events then share one database transaction. The command is valid
    only immediately before migration 0045 and never runs implicitly as part
    of Alembic.
    """
    run_terminal_statuses_sql = _v02_run_terminal_statuses_sql()
    indexing_terminal_statuses_sql = _v02_index_terminal_statuses_sql()

    async def locked_action(
        connection: Any,
        preflight: V02PreflightReport,
    ) -> V02TerminalizationReport:
        return await _terminalize_v02_locked(
            connection,
            preflight,
            run_terminal_statuses_sql=run_terminal_statuses_sql,
            indexing_terminal_statuses_sql=indexing_terminal_statuses_sql,
        )

    return await _v02_preflight(
        database_url,
        locked_action=locked_action,
        managed_rls_mode=rls_mode,
        services_quiesced=True,
    )


def terminalize_v02_legacy_work(
    database_url: str,
    *,
    services_stopped: bool = False,
    rls_mode: MigrationRLSMode | str = "auto",
) -> V02TerminalizationReport:
    """Run the explicit pre-0045 work terminalization synchronously.

    A fresh preflight is mandatory and may have no blocker other than the work
    this command is designed to terminate. The caller must first stop and drain
    every API and worker process: PostgreSQL locks cannot reveal a process that
    is still inside an external provider, tool, or vector-store call. Requiring
    this explicit assertion avoids presenting a database lock as process-level
    quiescence.

    Args:
        database_url: Direct PostgreSQL URL for the legacy database.
        services_stopped: Explicit operator assertion that API and worker
            processes have stopped and their database pools are drained.

    Returns:
        Counts of terminalized runs and reindex jobs.

    Raises:
        RuntimeError: If shutdown was not asserted or the locked safety checks
            fail.
    """
    if not services_stopped:
        raise RuntimeError(
            "v0.2 work terminalization requires every API and worker process "
            "to be stopped and services_stopped=True"
        )
    report = asyncio.run(
        _terminalize_v02_legacy_work(
            database_url,
            rls_mode=_normalize_rls_mode(rls_mode),
        )
    )
    log.warning(
        "v0.2 maintenance terminalized runs=%s reindex_jobs=%s "
        "reason=platform_upgrade",
        report.runs_terminalized,
        report.reindex_jobs_terminalized,
    )
    return report


def build_alembic_config(database_url: str) -> Config:
    """Build an Alembic config bound to the packaged migration scripts.

    Args:
        database_url: SQLAlchemy async URL the migrations run against.
            Constructor argument by design; only the CLI entry point
            below reads the environment.
    """
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_PATH))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _resolve_target_revisions(config: Config, revision: str) -> tuple[str, ...]:
    """Resolve one Alembic target before touching the database."""
    if revision.startswith(("+", "-")) or re.search(r"[+-][0-9]+$", revision):
        raise ValueError(
            "relative Alembic targets are not supported by the managed "
            "migration postcondition; pass an explicit revision"
        )
    if revision == "base":
        return ()
    revisions = ScriptDirectory.from_config(config).get_revisions(revision)
    return tuple(sorted(item.revision for item in revisions))


async def _acquire_migration_advisory_lock(connection: Any) -> None:
    """Serialize all Inqtrix schema transitions in the current database."""
    acquired = bool(
        (
            await connection.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended(:lock_name, 0))"
                ),
                {"lock_name": _MIGRATION_ADVISORY_LOCK_KEY},
            )
        ).scalar_one()
    )
    if not acquired:
        raise RuntimeError(
            "another Inqtrix schema migration already holds the database lock"
        )


def _schema_transition_required(
    report: MigrationRoleReport,
    expected_revisions: tuple[str, ...],
) -> bool:
    """Return whether Alembic would change an installed schema revision."""
    return report.schema_revision != expected_revisions


def _assert_schema_transition_quiesced(
    report: MigrationRoleReport,
    *,
    expected_revisions: tuple[str, ...],
    services_quiesced: bool,
) -> bool:
    """Require an explicit maintenance window for every installed transition.

    RLS authority and workload quiescence are independent contracts. A no-op
    invocation at the installed target remains safe during normal startup.
    """
    transition = _schema_transition_required(report, expected_revisions)
    if report.existing_schema and transition and not services_quiesced:
        raise RuntimeError(
            "migration of an installed schema requires "
            "services_quiesced=True after API, worker, collaboration, and "
            "connection-pool sessions have been stopped and drained; this "
            "requirement applies to auto, owner, and bypass RLS strategies"
        )
    return transition


async def _assert_database_sessions_drained(connection: Any) -> None:
    """Prove that the migration connection is the database's only client."""
    other_sessions = int(
        (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datid = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database()) "
                    "AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend'"
                )
            )
        ).scalar_one()
    )
    if other_sessions:
        raise RuntimeError(
            "migration refused before schema mutation: found "
            f"{other_sessions} other database client session(s); stop the "
            "API, worker, collaboration service, and pooler, drain their "
            "sessions, then retry"
        )


def _quoted_table(connection: Any, table_name: str) -> str:
    """Quote one catalog-derived relation name for a utility statement."""
    return str(connection.dialect.identifier_preparer.quote(table_name))


def _maintain_owner_rls_tables(
    connection: Any,
    tracked_relation_oids: dict[str, int],
    *,
    lock_version_table: bool = False,
) -> None:
    """Lock and unforce tenant tables first seen in an owner migration.

    Alembic invokes this helper after every revision. Tracking relation OIDs,
    rather than only names, also covers a downgrade that drops and recreates a
    relation with the same name. A revision that re-applies FORCE to an
    existing OID is unforced again while its transaction-held lock is reused.
    """
    rows = connection.execute(
        text(
            "SELECT relation.relname, relation.oid, "
            "relation.relforcerowsecurity FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND relation.relkind IN ('r', 'p') "
            "ORDER BY relation.relname"
        )
    )
    allowed = set(MIGRATION_TENANT_RLS_TABLES)
    tenant_relations = tuple(
        (str(row[0]), int(row[1]), bool(row[2]))
        for row in rows
        if str(row[0]) in allowed
    )
    discovered = tuple(
        (name, relation_oid)
        for name, relation_oid, _is_forced in tenant_relations
        if tracked_relation_oids.get(name) != relation_oid
    )
    lock_names = tuple(name for name, _oid in discovered)
    if lock_version_table:
        lock_names = (*lock_names, "alembic_version")
    if lock_names:
        quoted = ", ".join(
            _quoted_table(connection, table_name)
            for table_name in sorted(lock_names)
        )
        connection.execute(
            text(f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE")
        )
    for table_name, relation_oid, is_forced in tenant_relations:
        if (
            tracked_relation_oids.get(table_name) == relation_oid
            and not is_forced
        ):
            continue
        connection.execute(
            text(
                "ALTER TABLE "
                f"{_quoted_table(connection, table_name)} "
                "NO FORCE ROW LEVEL SECURITY"
            )
        )
        tracked_relation_oids[table_name] = relation_oid


async def _begin_owner_rls_maintenance(
    connection: Any,
    report: MigrationRoleReport,
) -> dict[str, int]:
    """Lock tenant state and remove only FORCE RLS inside this transaction."""
    tracked_relation_oids: dict[str, int] = {}

    def begin(sync_connection: Any) -> None:
        _maintain_owner_rls_tables(
            sync_connection,
            tracked_relation_oids,
            lock_version_table=report.version_table_exists,
        )

    await connection.run_sync(begin)
    # This does not bypass RLS. It makes any missed forced table fail loudly
    # instead of letting a cross-tenant migration operate on a filtered subset.
    await connection.execute(text("SET LOCAL row_security = off"))
    return tracked_relation_oids


async def _begin_bypass_schema_maintenance(
    connection: Any,
    report: MigrationRoleReport,
) -> None:
    """Lock installed tenant state without changing FORCE-RLS attributes."""
    lock_names = tuple(table.name for table in report.tenant_tables)
    if report.version_table_exists:
        lock_names = (*lock_names, RUNTIME_VERSION_TABLE)
    if not lock_names:
        return
    quoted = ", ".join(
        _quoted_table(connection, table_name)
        for table_name in sorted(set(lock_names))
    )
    await connection.execute(
        text(f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE")
    )


async def _restore_owner_rls(
    connection: Any,
    table_names: tuple[str, ...],
) -> None:
    """Restore FORCE RLS for every maintenance table that still exists."""
    if not table_names:
        return
    existing_rows = await connection.execute(
        text(
            "SELECT relation.relname FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = current_schema() "
            "AND relation.relkind IN ('r', 'p')"
        )
    )
    existing = {str(row[0]) for row in existing_rows}
    for table_name in table_names:
        if table_name not in existing:
            continue
        await connection.execute(
            text(
                "ALTER TABLE "
                f"{_quoted_table(connection, table_name)} "
                "FORCE ROW LEVEL SECURITY"
            )
        )


async def _assert_migration_postconditions(
    connection: Any,
    *,
    expected_revisions: tuple[str, ...],
) -> None:
    """Verify target revision and the complete tenant security invariant."""
    report = await _inspect_migration_role(connection)
    if report.schema_revision != expected_revisions:
        raise RuntimeError(
            "migration target revision mismatch: expected "
            f"{expected_revisions!r}, found {report.schema_revision!r}"
        )
    unmanaged = tuple(
        table.name for table in report.tenant_tables if not table.manageable
    )
    if unmanaged and not report.is_superuser:
        raise RuntimeError(
            "migration changed tenant-table ownership beyond the migration "
            "role's authority: " + ", ".join(unmanaged)
        )
    if (
        report.version_table_exists
        and not report.version_table_manageable
        and not report.is_superuser
    ):
        raise RuntimeError(
            "migration changed alembic_version ownership beyond the "
            "migration role's authority"
        )
    dependency_by_name = {
        dependency.name: dependency
        for dependency in report.runtime_dependencies
    }
    missing_dependencies = tuple(
        sorted(
            dependency_name
            for dependency_name in _required_runtime_dependency_names(report)
            if dependency_name not in dependency_by_name
            or not dependency_by_name[dependency_name].exists
        )
    )
    if missing_dependencies:
        raise RuntimeError(
            "migration left required runtime dependencies missing: "
            + ", ".join(missing_dependencies)
        )
    unmanaged_dependencies = tuple(
        dependency.name
        for dependency in report.runtime_dependencies
        if dependency.exists and not dependency.manageable
    )
    if unmanaged_dependencies and not report.is_superuser:
        raise RuntimeError(
            "migration changed runtime-dependency ownership beyond the "
            "migration role's authority: "
            + ", ".join(unmanaged_dependencies)
        )
    drifted = tuple(
        table.name
        for table in report.tenant_tables
        if not (
            table.row_security
            and table.force_row_security
            and table.tenant_policy
        )
    )
    if drifted:
        raise RuntimeError(
            "migration left tenant RLS disabled, unforced, or without its "
            "policy on: " + ", ".join(drifted)
        )
    if expected_revisions == (SCHEMA_HEAD_REVISION,):
        if not report.version_table_exists or not report.version_app_acl_valid:
            raise RuntimeError(
                "head alembic_version privilege contract mismatch: runtime "
                "requires explicit SELECT only"
            )
        actual = {table.name for table in report.tenant_tables}
        missing = tuple(sorted(set(TENANT_RLS_TABLES) - actual))
        legacy = tuple(sorted(actual - set(TENANT_RLS_TABLES)))
        if missing or legacy:
            raise RuntimeError(
                "head tenant-table inventory mismatch: "
                f"missing={missing!r}, legacy={legacy!r}"
            )
        table_acl_drift = tuple(
            sorted(
                table.name
                for table in report.tenant_tables
                if not table.app_acl_valid
            )
        )
        if table_acl_drift:
            raise RuntimeError(
                "head tenant-table privilege contract mismatch: "
                + ", ".join(table_acl_drift)
            )
        expected_dependencies = set(RUNTIME_REQUIRED_FUNCTIONS) | set(
            RUNTIME_REQUIRED_SEQUENCES
        )
        missing_head_dependencies = tuple(
            sorted(
                dependency_name
                for dependency_name in expected_dependencies
                if dependency_name not in dependency_by_name
                or not dependency_by_name[dependency_name].exists
            )
        )
        if missing_head_dependencies:
            raise RuntimeError(
                "head runtime-dependency inventory mismatch: missing="
                f"{missing_head_dependencies!r}"
            )
        acl_drift = tuple(
            sorted(
                dependency.name
                for dependency in report.runtime_dependencies
                if dependency.exists and not dependency.app_acl_valid
            )
        )
        if acl_drift:
            raise RuntimeError(
                "head runtime-dependency privilege contract mismatch: "
                + ", ".join(acl_drift)
            )


async def _invoke_alembic(
    connection: Any,
    *,
    config: Config,
    revision: str,
    downgrade: bool,
    owner_rls_tables: dict[str, int] | None = None,
) -> None:
    """Run Alembic on the caller's live transaction and connection."""

    def invoke(sync_connection: Any) -> None:
        config.attributes["connection"] = sync_connection
        if owner_rls_tables is not None:

            def maintain_after_revision(**_kwargs: Any) -> None:
                _maintain_owner_rls_tables(
                    sync_connection,
                    owner_rls_tables,
                )

            config.attributes["on_version_apply"] = maintain_after_revision
        else:
            config.attributes.pop("on_version_apply", None)
        try:
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
        finally:
            config.attributes.pop("on_version_apply", None)
            config.attributes.pop("connection", None)

    await connection.run_sync(invoke)


async def _run_schema_migrations(
    database_url: str,
    *,
    revision: str,
    rls_mode: MigrationRLSMode,
    services_quiesced: bool,
    downgrade: bool,
) -> None:
    """Apply one schema transition under the managed RLS contract."""
    from inqtrix.storage.db import build_engine

    _validate_migration_url(database_url)
    config = build_alembic_config(database_url)
    expected_revisions = _resolve_target_revisions(config, revision)
    engine = build_engine(database_url, null_pool=True)
    try:
        try:
            async with engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(
                        text(
                            "SET LOCAL lock_timeout = "
                            f"'{_MIGRATION_LOCK_TIMEOUT_SECONDS}s'"
                        )
                    )
                    report = await _inspect_migration_role(connection)
                    transition = _assert_schema_transition_quiesced(
                        report,
                        expected_revisions=expected_revisions,
                        services_quiesced=services_quiesced,
                    )
                    strategy = _assert_migration_role(
                        report,
                        rls_mode=rls_mode,
                        services_quiesced=(
                            services_quiesced or not transition
                        ),
                    )
                    await _acquire_migration_advisory_lock(connection)
                    if report.existing_schema and transition:
                        await _assert_database_sessions_drained(connection)
                    owner_tables: dict[str, int] | None = None
                    if strategy == "fresh" or (
                        strategy == "owner" and transition
                    ):
                        owner_tables = await _begin_owner_rls_maintenance(
                            connection,
                            report,
                        )
                    else:
                        _log_auto_rls_strategy(strategy, rls_mode)
                        if report.existing_schema and transition:
                            await _begin_bypass_schema_maintenance(
                                connection,
                                report,
                            )
                    await _invoke_alembic(
                        connection,
                        config=config,
                        revision=revision,
                        downgrade=downgrade,
                        owner_rls_tables=owner_tables,
                    )
                    await _restore_owner_rls(
                        connection,
                        tuple(owner_tables) if owner_tables is not None else (),
                    )
                    await _assert_migration_postconditions(
                        connection,
                        expected_revisions=expected_revisions,
                    )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise RuntimeError(
                    "migration lock acquisition exceeded "
                    f"{_MIGRATION_LOCK_TIMEOUT_SECONDS} seconds; PostgreSQL "
                    "rolled back the transaction and no schema transition "
                    "was published. Keep services stopped, drain database "
                    "sessions, and retry."
                ) from exc
            raise
    finally:
        await engine.dispose()


def preflight_migration_role(database_url: str) -> MigrationRoleReport:
    """Inspect migration privileges without changing schema or RLS state."""
    from inqtrix.storage.db import build_engine

    async def inspect() -> MigrationRoleReport:
        _validate_migration_url(database_url)
        engine = build_engine(database_url, null_pool=True)
        try:
            async with engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    return await _inspect_migration_role(connection)
        finally:
            await engine.dispose()

    return asyncio.run(inspect())


def run_migrations(
    database_url: str,
    *,
    revision: str = "head",
    migration_database_url: str | None = None,
    rls_mode: MigrationRLSMode | str = "auto",
    services_quiesced: bool = False,
) -> None:
    """Upgrade the database schema to *revision* (default: head).

    Synchronous by design — the async migration engine lives inside
    this runner, so this must be called from a context without a running event
    loop (startup hooks and test fixtures use a thread when needed).

    Args:
        database_url: Runtime database URL retained as the backwards-compatible
            fallback for bundled installations.
        revision: Alembic target; defaults to the single packaged head.
        migration_database_url: Optional dedicated migration URL. When set, it
            is the only URL opened by this operation.
        rls_mode: ``auto`` accepts only a fresh schema or an RLS-exempt role;
            ``bypass`` requires a dedicated non-superuser ``BYPASSRLS`` role;
            ``owner`` opens a locked, transaction-scoped FORCE-RLS maintenance
            boundary.
        services_quiesced: Required for every change to an installed schema,
            independent of RLS strategy, after every application and pooler
            database session is drained. A no-op invocation at the installed
            target does not require a maintenance window.
    """
    effective_url = migration_database_url or database_url
    asyncio.run(
        _run_schema_migrations(
            effective_url,
            revision=revision,
            rls_mode=_normalize_rls_mode(rls_mode),
            services_quiesced=services_quiesced,
            downgrade=False,
        )
    )


def downgrade_migrations(
    database_url: str,
    *,
    revision: str,
    migration_database_url: str | None = None,
    rls_mode: MigrationRLSMode | str = "auto",
    services_quiesced: bool = False,
) -> None:
    """Downgrade the schema under the same role and RLS safety contract."""
    effective_url = migration_database_url or database_url
    asyncio.run(
        _run_schema_migrations(
            effective_url,
            revision=revision,
            rls_mode=_normalize_rls_mode(rls_mode),
            services_quiesced=services_quiesced,
            downgrade=True,
        )
    )


def main() -> None:
    """Console entry point: migrate the configured database to head."""
    parser = argparse.ArgumentParser(
        prog="inqtrix-migrate",
        description=(
            "Apply the Inqtrix platform schema migrations through the role "
            "and tenant-RLS safety contract."
        ),
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="SQLAlchemy async URL (overrides INQTRIX_DATABASE_URL).",
    )
    parser.add_argument(
        "--migration-database-url",
        default="",
        help=(
            "Dedicated migration URL (break-glass CLI override for "
            "INQTRIX_MIGRATION_DATABASE_URL; never passed to runtimes)."
        ),
    )
    parser.add_argument(
        "--rls-mode",
        choices=("auto", "owner", "bypass"),
        default="",
        help=(
            "Migration RLS strategy (default: INQTRIX_MIGRATION_RLS_MODE "
            "or auto)."
        ),
    )
    parser.add_argument(
        "--confirm-services-quiesced",
        action="store_true",
        help=(
            "Assert that API, worker, and collaboration database sessions "
            "are drained for owner-mode migration maintenance."
        ),
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="Target revision (default: head).",
    )
    maintenance_mode = parser.add_mutually_exclusive_group()
    maintenance_mode.add_argument(
        "--preflight-v02",
        action="store_true",
        help=(
            "Run the read-only v0.2 identity/share readiness audit and "
            "exit without applying migrations."
        ),
    )
    parser.add_argument(
        "--confirm-services-stopped",
        action="store_true",
        help=(
            "Required with --terminalize-v02-work: confirm that every API "
            "and worker process has stopped and drained."
        ),
    )
    maintenance_mode.add_argument(
        "--terminalize-v02-work",
        action="store_true",
        help=(
            "Atomically fail all legacy non-terminal runs and reindex jobs "
            "with reason platform_upgrade, then exit without migrating."
        ),
    )
    args = parser.parse_args()

    from inqtrix.settings import StorageSettings

    settings = StorageSettings()
    if args.migration_database_url:
        runtime_database_url = args.database_url or settings.database_url
        migration_database_url = args.migration_database_url
    elif args.database_url:
        # Preserve the historical CLI override as an explicit break-glass
        # choice even when a migration URL exists in the settings source.
        runtime_database_url = args.database_url
        migration_database_url = ""
    else:
        runtime_database_url = settings.database_url
        migration_database_url = settings.migration_database_url
    database_url = migration_database_url or runtime_database_url
    if not database_url:
        raise SystemExit(
            "No database URL: pass --migration-database-url/--database-url "
            "or configure INQTRIX_MIGRATION_DATABASE_URL/"
            "INQTRIX_DATABASE_URL."
        )
    rls_mode = args.rls_mode or settings.migration_rls_mode
    services_quiesced = bool(
        args.confirm_services_quiesced
        or settings.migration_services_quiesced
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.confirm_services_stopped and not args.terminalize_v02_work:
        parser.error(
            "--confirm-services-stopped is valid only with "
            "--terminalize-v02-work"
        )
    if args.preflight_v02:
        report = preflight_v02(
            database_url,
            rls_mode=rls_mode,
            services_quiesced=services_quiesced,
        )
        print(json.dumps(report.as_json_dict(), indent=2, sort_keys=True))
        if not report.ready:
            raise SystemExit(2)
        return
    if args.terminalize_v02_work:
        if not args.confirm_services_stopped:
            parser.error(
                "--terminalize-v02-work requires "
                "--confirm-services-stopped"
            )
        report = terminalize_v02_legacy_work(
            database_url,
            services_stopped=True,
            rls_mode=rls_mode,
        )
        print(json.dumps(report.as_json_dict(), indent=2, sort_keys=True))
        return
    run_migrations(
        runtime_database_url or database_url,
        revision=args.revision,
        migration_database_url=migration_database_url or None,
        rls_mode=rls_mode,
        services_quiesced=services_quiesced,
    )
    log.info("Migrations applied (revision=%s).", args.revision)


if __name__ == "__main__":
    main()
