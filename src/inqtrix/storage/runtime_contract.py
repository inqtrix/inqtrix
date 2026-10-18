"""Runtime-side PostgreSQL contract shared by API readiness and workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from inqtrix.storage.db import (
    build_engine,
    build_session_factory,
    tenant_session,
)
from inqtrix.storage.migration_contract import (
    RUNTIME_REQUIRED_FUNCTIONS,
    RUNTIME_REQUIRED_SEQUENCES,
    RUNTIME_VERSION_TABLE,
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_POLICY,
    TENANT_RLS_TABLES,
    WORM_TENANT_TABLES,
    postgres_direct_relation_acl_sql,
    postgres_role_can_set_sql,
    postgres_tenant_table_acl_sql,
    tenant_policy_expression_matches,
    worm_relname_sql,
)

_RUNTIME_PROBE_TENANT = "default"
# Append-only tables carry INSERT/SELECT only; UPDATE/DELETE on them is a
# privilege escalation, not a normal grant. Derived from ONE inventory so
# a new WORM table cannot be forgotten in one of the four assertions.
worm_relation_predicate = worm_relname_sql("relation")
DatabaseRuntimeLoginPolicy = Literal["restricted", "bundled_legacy"]


class DatabaseRuntimeContractError(RuntimeError):
    """Raised when a runtime connection is unsafe or on the wrong schema."""


class DatabaseRuntimeUnavailableError(RuntimeError):
    """Raised when the runtime contract cannot run for a transient outage."""


_TRANSIENT_DATABASE_SQLSTATES = frozenset(
    {
        "53300",  # too_many_connections
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)


def _database_sqlstate(exc: BaseException) -> str:
    """Return a driver SQLSTATE without parsing localized error text."""
    for candidate in (exc, getattr(exc, "orig", None)):
        if candidate is None:
            continue
        value = getattr(candidate, "sqlstate", None) or getattr(
            candidate,
            "pgcode",
            None,
        )
        if value:
            return str(value)
    return ""


def _is_database_runtime_unavailable(exc: BaseException) -> bool:
    """Classify known reachability failures without hiding verifier defects."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                OSError,
                TimeoutError,
                DisconnectionError,
                SQLAlchemyTimeoutError,
            ),
        ):
            return True
        sqlstate = _database_sqlstate(current)
        if sqlstate.startswith("08") or (
            sqlstate in _TRANSIENT_DATABASE_SQLSTATES
        ):
            return True
        if (
            isinstance(current, DBAPIError)
            and current.connection_invalidated
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class DatabaseRuntimeContract:
    """Verified effective database identity and installed schema revision."""

    revision: str
    effective_role: str
    session_role: str


def _assert_runtime_tenant_table_contract(rows: list[Any]) -> None:
    """Validate the complete head-revision RLS and runtime grant inventory."""
    table_rows = {str(row["relname"]): row for row in rows}
    missing = tuple(sorted(set(TENANT_RLS_TABLES) - set(table_rows)))
    if missing:
        raise DatabaseRuntimeContractError(
            "database schema is missing expected tenant tables: "
            + ", ".join(missing)
        )
    missing_tenant_key = tuple(
        name
        for name, row in table_rows.items()
        if not bool(row["has_tenant_id"])
    )
    if missing_tenant_key:
        raise DatabaseRuntimeContractError(
            "expected tenant tables are missing tenant_id: "
            + ", ".join(sorted(missing_tenant_key))
        )
    rls_drift = tuple(
        name
        for name, row in table_rows.items()
        if not (
            bool(row["relrowsecurity"])
            and bool(row["relforcerowsecurity"])
        )
    )
    if rls_drift:
        raise DatabaseRuntimeContractError(
            "tenant RLS must be enabled and forced on: "
            + ", ".join(sorted(rls_drift))
        )
    policy_drift = tuple(
        name
        for name, row in table_rows.items()
        if not (
            int(row["total_policy_count"]) == 1
            and str(row["policy_command"]) == "*"
            and bool(row["policy_permissive"])
            and bool(row["policy_is_public"])
            and tenant_policy_expression_matches(row["policy_using"])
            and tenant_policy_expression_matches(row["policy_check"])
        )
    )
    if policy_drift:
        raise DatabaseRuntimeContractError(
            "tenant tables do not have the canonical fail-closed policy: "
            + ", ".join(sorted(policy_drift))
        )
    privilege_drift = tuple(
        name
        for name, row in table_rows.items()
        if not (
            bool(row["can_select"])
            and bool(row["can_insert"])
            and (
                (
                    name in WORM_TENANT_TABLES
                    and not bool(row["can_update"])
                    and not bool(row["can_delete"])
                )
                or (
                    name not in WORM_TENANT_TABLES
                    and bool(row["can_update"])
                    and bool(row["can_delete"])
                )
            )
            and not bool(row["can_truncate"])
            and not bool(row["can_references"])
            and not bool(row["can_trigger"])
            and not bool(row["can_maintain"])
            and not bool(row["has_table_grant_option"])
            and not bool(row["has_forbidden_column_privileges"])
        )
    )
    if privilege_drift:
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role has excessive or incomplete "
            "required table grants on: "
            + ", ".join(sorted(privilege_drift))
        )
    direct_acl_drift = tuple(
        name
        for name, row in table_rows.items()
        if not bool(row["direct_acl_valid"])
    )
    if direct_acl_drift:
        raise DatabaseRuntimeContractError(
            "tenant tables require exact canonical application grants without "
            "PUBLIC or app-role column ACLs on: "
            + ", ".join(sorted(direct_acl_drift))
        )


def _assert_runtime_dependency_contract(
    function_rows: list[Any],
    sequence_rows: list[Any],
) -> None:
    """Validate executable RLS and identity-sequence dependencies."""
    functions = {
        str(row["function_signature"]): row for row in function_rows
    }
    function_drift = tuple(
        signature
        for signature in RUNTIME_REQUIRED_FUNCTIONS
        if signature not in functions
        or not bool(functions[signature]["function_exists"])
        or not bool(functions[signature]["can_execute"])
        or bool(functions[signature]["execute_grant_option"])
        or not bool(functions[signature]["explicit_execute"])
        or bool(functions[signature]["public_execute"])
        or bool(functions[signature]["can_assume_owner"])
    )
    if function_drift:
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role cannot safely execute required "
            "tenant functions: " + ", ".join(function_drift)
        )
    sequences = {str(row["sequence_name"]): row for row in sequence_rows}
    sequence_drift = tuple(
        sequence_name
        for sequence_name in RUNTIME_REQUIRED_SEQUENCES
        if sequence_name not in sequences
        or not bool(sequences[sequence_name]["sequence_exists"])
        or not bool(sequences[sequence_name]["can_use"])
        or bool(sequences[sequence_name]["can_select"])
        or bool(sequences[sequence_name]["can_update"])
        or bool(sequences[sequence_name]["usage_grant_option"])
        or not bool(sequences[sequence_name]["explicit_usage"])
        or bool(sequences[sequence_name]["public_acl"])
        or bool(sequences[sequence_name]["can_assume_owner"])
    )
    if sequence_drift:
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role lacks the exact required "
            "sequence grants on: " + ", ".join(sequence_drift)
        )


def _assert_runtime_version_contract(row: Any) -> None:
    """Require the active schema's revision table with exact read-only ACL."""
    if not bool(row["version_table_exists"]) or not bool(
        row["version_resolves_in_current_schema"]
    ):
        raise DatabaseRuntimeContractError(
            "alembic_version must resolve from the active PostgreSQL schema"
        )
    if not (
        bool(row["version_can_select"])
        and not bool(row["version_can_insert"])
        and not bool(row["version_can_update"])
        and not bool(row["version_can_delete"])
        and not bool(row["version_can_truncate"])
        and not bool(row["version_can_references"])
        and not bool(row["version_can_trigger"])
        and not bool(row["version_can_maintain"])
        and not bool(row["version_has_grant_option"])
        and not bool(row["version_has_forbidden_column_privileges"])
        and not bool(row["version_can_assume_owner"])
        and bool(row["version_direct_acl_valid"])
    ):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role requires explicit SELECT-only "
            "access to alembic_version"
        )


def _assert_runtime_session_settings(row: Any) -> None:
    """Reject read-only or row-security-disabled runtime connections."""
    if not bool(row["row_security_enabled"]) or not bool(
        row["probe_rls_active"]
    ):
        raise DatabaseRuntimeContractError(
            "PostgreSQL runtime connection requires row_security=on with "
            "tenant RLS active"
        )
    if bool(row["transaction_read_only"]):
        raise DatabaseRuntimeContractError(
            "PostgreSQL runtime connection must allow write transactions"
        )


def _assert_runtime_role_capabilities(
    rows: list[Any],
    *,
    allow_legacy_session: bool,
) -> None:
    """Reject forbidden rights held directly, inherited, or via SET ROLE."""
    capabilities = {
        str(row["identity_name"]): bool(row["has_forbidden_capabilities"])
        for row in rows
    }
    missing = {"effective", "session"} - set(capabilities)
    dangerous = tuple(
        sorted(
            name
            for name, value in capabilities.items()
            if value and not (allow_legacy_session and name == "session")
        )
    )
    if missing or dangerous:
        raise DatabaseRuntimeContractError(
            "PostgreSQL runtime identities have missing or forbidden direct/"
            "inherited/SET ROLE capabilities: "
            f"missing={tuple(sorted(missing))!r}, dangerous={dangerous!r}"
        )


def _assert_runtime_identity_contract(
    row: Any,
    *,
    app_role: str,
    login_policy: DatabaseRuntimeLoginPolicy,
) -> DatabaseRuntimeContract:
    """Validate effective/session role shape and return safe diagnostics."""
    effective_role = str(row["effective_role"])
    session_role = str(row["session_role"])
    revision = str(row["revision"] or "")
    tenant_id = str(row["tenant_id"] or "")
    if app_role and effective_role != app_role:
        raise DatabaseRuntimeContractError(
            "tenant transaction did not assume the configured application role"
        )
    if bool(row["is_superuser"]) or bool(row["bypasses_rls"]):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role must be NOSUPERUSER NOBYPASSRLS"
        )
    if (
        (bool(row["can_login"]) and bool(app_role))
        or bool(row["can_create_database"])
        or bool(row["can_create_role"])
        or bool(row["can_replicate"])
    ):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role must be NOLOGIN NOCREATEDB "
            "NOCREATEROLE NOREPLICATION"
        )
    if not bool(row["schema_usage"]) or bool(row["schema_create"]):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role requires schema USAGE without "
            "schema CREATE"
        )
    if bool(row["owns_rls_table"]):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role must not own RLS tables"
        )
    if bool(row["can_assume_rls_owner"]):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role must not assume an RLS "
            "table-owner role"
        )
    if bool(row["can_assume_rls_bypass"]):
        raise DatabaseRuntimeContractError(
            "effective PostgreSQL runtime role must not assume a SUPERUSER or "
            "BYPASSRLS role"
        )
    if login_policy == "restricted":
        if (
            not bool(row["session_can_login"])
            or bool(row["session_is_superuser"])
            or bool(row["session_bypasses_rls"])
            or bool(row["session_can_create_database"])
            or bool(row["session_can_create_role"])
            or bool(row["session_can_replicate"])
            or bool(row["session_schema_create"])
        ):
            raise DatabaseRuntimeContractError(
                "PostgreSQL runtime session login must be LOGIN NOSUPERUSER "
                "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION and "
                "must not hold schema CREATE authority"
            )
        if (
            bool(row["session_owns_rls_table"])
            or bool(row["session_can_assume_rls_owner"])
            or bool(row["session_can_assume_rls_bypass"])
        ):
            raise DatabaseRuntimeContractError(
                "PostgreSQL runtime session login must not own or assume "
                "RLS-owner/BYPASS roles"
            )
    elif not (
        bool(row["session_can_login"])
        and bool(row["session_is_superuser"])
        and bool(row["session_owns_all_rls_tables"])
    ):
        raise DatabaseRuntimeContractError(
            "bundled_legacy accepts only the bundled LOGIN superuser that "
            "owns every RLS table"
        )
    if tenant_id != _RUNTIME_PROBE_TENANT:
        raise DatabaseRuntimeContractError(
            "tenant transaction did not retain its transaction-local tenant GUC"
        )
    if revision != SCHEMA_HEAD_REVISION:
        raise DatabaseRuntimeContractError(
            "database schema revision does not match the packaged Alembic head "
            f"(installed={revision or 'missing'}, expected={SCHEMA_HEAD_REVISION})"
        )
    return DatabaseRuntimeContract(
        revision=revision,
        effective_role=effective_role,
        session_role=session_role,
    )


_AUDIT_ROLE_CAN_SET = postgres_role_can_set_sql(
    "identity.role_oid", "candidate.oid"
)
_RUNTIME_ROLE_CAPABILITY_AUDIT_SQL = f"""
WITH identities(identity_name, role_oid) AS (
    SELECT 'effective', oid FROM pg_roles WHERE rolname = current_user
    UNION ALL
    SELECT 'session', oid FROM pg_roles WHERE rolname = session_user
), runtime_identity_capabilities AS (
    SELECT identity.identity_name, candidate.*
    FROM identities AS identity
    JOIN pg_roles AS candidate
      ON candidate.oid = identity.role_oid
      OR COALESCE({_AUDIT_ROLE_CAN_SET}, false)
)
SELECT
    identity_name,
    bool_or(
        candidate.rolsuper
        OR candidate.rolbypassrls
        OR candidate.rolcreatedb
        OR candidate.rolcreaterole
        OR candidate.rolreplication
        OR has_schema_privilege(
            candidate.oid, current_schema(), 'CREATE'
        )
        OR has_database_privilege(
            candidate.oid, current_database(), 'CREATE'
        )
        OR EXISTS (
            SELECT 1
            FROM pg_database AS database
            WHERE database.datname = current_database()
              AND (
                  database.datdba = candidate.oid
                  OR pg_has_role(
                      candidate.oid, database.datdba, 'USAGE'
                  )
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_namespace AS namespace
            WHERE namespace.nspname = current_schema()
              AND (
                  namespace.nspowner = candidate.oid
                  OR pg_has_role(
                      candidate.oid, namespace.nspowner, 'USAGE'
                  )
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = current_schema()
              AND relation.relkind IN ('r', 'p')
              AND relation.relname = ANY(CAST(:tenant_tables AS TEXT[]))
              AND (
                  relation.relowner = candidate.oid
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'TRUNCATE'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'REFERENCES'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'TRIGGER'
                  )
                  OR (
                      {worm_relation_predicate}
                      AND (
                          has_table_privilege(
                              candidate.oid, relation.oid, 'UPDATE'
                          )
                          OR has_table_privilege(
                              candidate.oid, relation.oid, 'DELETE'
                          )
                      )
                  )
                  OR has_table_privilege(
                      candidate.oid,
                      relation.oid,
                      'SELECT WITH GRANT OPTION'
                  )
                  OR has_table_privilege(
                      candidate.oid,
                      relation.oid,
                      'INSERT WITH GRANT OPTION'
                  )
                  OR has_table_privilege(
                      candidate.oid,
                      relation.oid,
                      'UPDATE WITH GRANT OPTION'
                  )
                  OR has_table_privilege(
                      candidate.oid,
                      relation.oid,
                      'DELETE WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'REFERENCES'
                  )
                  OR (
                      {worm_relation_predicate}
                      AND has_any_column_privilege(
                          candidate.oid,
                          relation.oid,
                          'UPDATE'
                      )
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'SELECT WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'INSERT WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'UPDATE WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'REFERENCES WITH GRANT OPTION'
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM aclexplode(COALESCE(
                          relation.relacl,
                          acldefault('r', relation.relowner)
                      )) AS acl
                      WHERE acl.privilege_type = 'MAINTAIN'
                        AND (
                            acl.grantee = 0
                            OR pg_has_role(
                                candidate.oid, acl.grantee, 'USAGE'
                            )
                        )
                  )
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = current_schema()
              AND relation.relkind IN ('r', 'p')
              AND relation.relname = :version_table
              AND (
                  relation.relowner = candidate.oid
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'INSERT'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'UPDATE'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'DELETE'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'TRUNCATE'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'REFERENCES'
                  )
                  OR has_table_privilege(
                      candidate.oid, relation.oid, 'TRIGGER'
                  )
                  OR has_table_privilege(
                      candidate.oid,
                      relation.oid,
                      'SELECT WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid, relation.oid, 'INSERT'
                  )
                  OR has_any_column_privilege(
                      candidate.oid, relation.oid, 'UPDATE'
                  )
                  OR has_any_column_privilege(
                      candidate.oid, relation.oid, 'REFERENCES'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'SELECT WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'INSERT WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'UPDATE WITH GRANT OPTION'
                  )
                  OR has_any_column_privilege(
                      candidate.oid,
                      relation.oid,
                      'REFERENCES WITH GRANT OPTION'
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM aclexplode(COALESCE(
                          relation.relacl,
                          acldefault('r', relation.relowner)
                      )) AS acl
                      WHERE acl.privilege_type = 'MAINTAIN'
                        AND (
                            acl.grantee = 0
                            OR pg_has_role(
                                candidate.oid, acl.grantee, 'USAGE'
                            )
                        )
                  )
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_proc AS routine
            JOIN pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = current_schema()
              AND routine.oid IN (
                  SELECT to_regprocedure(format(
                      '%I.%s', current_schema(), function_signature
                  ))
                  FROM unnest(CAST(:function_signatures AS TEXT[]))
                      AS expected(function_signature)
              )
              AND (
                  routine.proowner = candidate.oid
                  OR has_function_privilege(
                      candidate.oid,
                      routine.oid,
                      'EXECUTE WITH GRANT OPTION'
                  )
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = current_schema()
              AND relation.relkind = 'S'
              AND relation.relname = ANY(
                  CAST(:sequence_names AS TEXT[])
              )
              AND (
                  relation.relowner = candidate.oid
                  OR has_sequence_privilege(
                      candidate.oid, relation.oid, 'SELECT'
                  )
                  OR has_sequence_privilege(
                      candidate.oid, relation.oid, 'UPDATE'
                  )
                  OR has_sequence_privilege(
                      candidate.oid,
                      relation.oid,
                      'USAGE WITH GRANT OPTION'
                  )
              )
        )
    ) AS has_forbidden_capabilities
FROM runtime_identity_capabilities AS candidate
GROUP BY identity_name
ORDER BY identity_name
"""


async def verify_database_runtime_contract(
    session_factory: Any,
    *,
    app_role: str,
    login_policy: DatabaseRuntimeLoginPolicy = "restricted",
) -> DatabaseRuntimeContract:
    """Verify the schema and restricted tenant transaction contract.

    The probe intentionally runs through :func:`tenant_session`, exactly like
    production repository calls. A successful result proves that role
    membership, the transaction-local tenant GUC, Alembic-head visibility and
    the effective role's non-privileged shape all work together.

    Args:
        session_factory: Async SQLAlchemy session factory used by the runtime.
        app_role: Restricted role selected by every tenant transaction. Empty
            is supported only when the login itself is non-privileged and does
            not own an RLS table.
        login_policy: ``restricted`` requires a least-privilege session login.
            ``bundled_legacy`` accepts only the historical bundled superuser
            that owns every RLS table; the effective app role remains strict.

    Returns:
        The installed revision and effective role, for sanitized operator logs.

    Raises:
        DatabaseRuntimeContractError: If the schema is stale, the tenant GUC is
            missing, or the effective runtime role is privileged/table-owning.
        Exception: Database connectivity, permission and ``SET ROLE`` errors
            propagate to the bounded readiness wrapper.
    """
    if login_policy not in {"restricted", "bundled_legacy"}:
        raise ValueError(f"unsupported database runtime login policy: {login_policy}")
    role_can_set_version_owner = postgres_role_can_set_sql(
        "role.oid", "version_relation.relowner"
    )
    role_can_set_table_owner = postgres_role_can_set_sql(
        "role.oid", "relation.relowner"
    )
    role_can_set_privileged = postgres_role_can_set_sql(
        "role.oid", "privileged_role.oid"
    )
    login_can_set_table_owner = postgres_role_can_set_sql(
        "login_role.oid", "relation.relowner"
    )
    login_can_set_privileged = postgres_role_can_set_sql(
        "login_role.oid", "privileged_role.oid"
    )
    role_can_set_function_owner = postgres_role_can_set_sql(
        "(SELECT oid FROM pg_roles WHERE rolname = current_user)",
        "routine.proowner",
    )
    role_can_set_sequence_owner = postgres_role_can_set_sql(
        "(SELECT oid FROM pg_roles WHERE rolname = current_user)",
        "relation.relowner",
    )
    canonical_app_role_oid = (
        "(SELECT oid FROM pg_roles WHERE rolname = 'inqtrix_app')"
    )
    version_direct_acl = postgres_direct_relation_acl_sql(
        "version_relation",
        canonical_app_role_oid,
        expected_privileges_sql="ARRAY['SELECT']::text[]",
    )
    tenant_table_direct_acl = postgres_tenant_table_acl_sql(
        "relation",
        canonical_app_role_oid,
    )
    async with tenant_session(
        session_factory,
        tenant_id=_RUNTIME_PROBE_TENANT,
        app_role=app_role,
    ) as session:
        result = await session.execute(
            text(
                f"""
                SELECT
                    current_user::text AS effective_role,
                    session_user::text AS session_role,
                    role.rolsuper AS is_superuser,
                    role.rolbypassrls AS bypasses_rls,
                    role.rolcanlogin AS can_login,
                    role.rolcreatedb AS can_create_database,
                    role.rolcreaterole AS can_create_role,
                    role.rolreplication AS can_replicate,
                    has_schema_privilege(
                        current_user, current_schema(), 'USAGE'
                    ) AS schema_usage,
                    has_schema_privilege(
                        current_user, current_schema(), 'CREATE'
                    ) AS schema_create,
                    login_role.rolsuper AS session_is_superuser,
                    login_role.rolbypassrls AS session_bypasses_rls,
                    login_role.rolcanlogin AS session_can_login,
                    login_role.rolcreatedb AS session_can_create_database,
                    login_role.rolcreaterole AS session_can_create_role,
                    login_role.rolreplication AS session_can_replicate,
                    has_schema_privilege(
                        session_user, current_schema(), 'CREATE'
                    ) AS session_schema_create,
                    current_setting('inqtrix.tenant_id', true) AS tenant_id,
                    current_setting('row_security')::boolean
                        AS row_security_enabled,
                    current_setting('transaction_read_only')::boolean
                        AS transaction_read_only,
                    COALESCE(
                        row_security_active(probe_relation.oid),
                        false
                    ) AS probe_rls_active,
                    version_relation.oid IS NOT NULL
                        AS version_table_exists,
                    COALESCE(
                        to_regclass(:version_table) = version_relation.oid,
                        false
                    ) AS version_resolves_in_current_schema,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'SELECT'
                    ), false) AS version_can_select,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'INSERT'
                    ), false) AS version_can_insert,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'UPDATE'
                    ), false) AS version_can_update,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'DELETE'
                    ), false) AS version_can_delete,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'TRUNCATE'
                    ), false) AS version_can_truncate,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'REFERENCES'
                    ), false) AS version_can_references,
                    COALESCE(has_table_privilege(
                        current_user, version_relation.oid, 'TRIGGER'
                    ), false) AS version_can_trigger,
                    (
                        COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'SELECT WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'INSERT WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'UPDATE WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'DELETE WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'TRUNCATE WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'REFERENCES WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_table_privilege(
                            current_user,
                            version_relation.oid,
                            'TRIGGER WITH GRANT OPTION'
                        ), false)
                    ) AS version_has_grant_option,
                    (
                        COALESCE(has_any_column_privilege(
                            current_user, version_relation.oid, 'INSERT'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user, version_relation.oid, 'UPDATE'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user, version_relation.oid, 'REFERENCES'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user,
                            version_relation.oid,
                            'SELECT WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user,
                            version_relation.oid,
                            'INSERT WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user,
                            version_relation.oid,
                            'UPDATE WITH GRANT OPTION'
                        ), false)
                        OR COALESCE(has_any_column_privilege(
                            current_user,
                            version_relation.oid,
                            'REFERENCES WITH GRANT OPTION'
                        ), false)
                    ) AS version_has_forbidden_column_privileges,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            version_relation.relacl,
                            acldefault('r', version_relation.relowner)
                        )) AS acl
                        WHERE acl.privilege_type = 'MAINTAIN'
                          AND (
                              acl.grantee = 0
                              OR pg_has_role(
                                  role.oid, acl.grantee, 'USAGE'
                              )
                          )
                    ) AS version_can_maintain,
                    COALESCE({role_can_set_version_owner}, false)
                        AS version_can_assume_owner,
                    {version_direct_acl} AS version_direct_acl_valid,
                    (
                        SELECT version_num
                        FROM alembic_version
                    ) AS revision,
                    EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = current_schema()
                          AND relation.relkind IN ('r', 'p')
                          AND relation.relname = ANY(
                              CAST(:tenant_tables AS TEXT[])
                          )
                          AND relation.relowner = role.oid
                    ) AS owns_rls_table,
                    EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = current_schema()
                          AND relation.relkind IN ('r', 'p')
                          AND relation.relname = ANY(
                              CAST(:tenant_tables AS TEXT[])
                          )
                          AND relation.relowner <> role.oid
                          AND COALESCE(
                              {role_can_set_table_owner}, false
                          )
                    ) AS can_assume_rls_owner,
                    EXISTS (
                        SELECT 1
                        FROM pg_roles AS privileged_role
                        WHERE privileged_role.oid <> role.oid
                          AND (
                              privileged_role.rolsuper
                              OR privileged_role.rolbypassrls
                          )
                          AND COALESCE(
                              {role_can_set_privileged}, false
                          )
                    ) AS can_assume_rls_bypass,
                    EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = current_schema()
                          AND relation.relkind IN ('r', 'p')
                          AND relation.relname = ANY(
                              CAST(:tenant_tables AS TEXT[])
                          )
                          AND relation.relowner = login_role.oid
                    ) AS session_owns_rls_table,
                    NOT EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = current_schema()
                          AND relation.relkind IN ('r', 'p')
                          AND relation.relname = ANY(
                              CAST(:tenant_tables AS TEXT[])
                          )
                          AND relation.relowner <> login_role.oid
                    ) AS session_owns_all_rls_tables,
                    EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = current_schema()
                          AND relation.relkind IN ('r', 'p')
                          AND relation.relname = ANY(
                              CAST(:tenant_tables AS TEXT[])
                          )
                          AND relation.relowner <> login_role.oid
                          AND COALESCE(
                              {login_can_set_table_owner}, false
                          )
                    ) AS session_can_assume_rls_owner,
                    EXISTS (
                        SELECT 1
                        FROM pg_roles AS privileged_role
                        WHERE privileged_role.oid <> login_role.oid
                          AND (
                              privileged_role.rolsuper
                              OR privileged_role.rolbypassrls
                          )
                          AND COALESCE(
                              {login_can_set_privileged}, false
                          )
                    ) AS session_can_assume_rls_bypass
                FROM pg_roles AS role
                JOIN pg_roles AS login_role
                  ON login_role.rolname = session_user
                LEFT JOIN pg_namespace AS version_namespace
                  ON version_namespace.nspname = current_schema()
                LEFT JOIN pg_class AS version_relation
                  ON version_relation.relnamespace = version_namespace.oid
                 AND version_relation.relname = :version_table
                 AND version_relation.relkind IN ('r', 'p')
                LEFT JOIN pg_namespace AS probe_namespace
                  ON probe_namespace.nspname = current_schema()
                LEFT JOIN pg_class AS probe_relation
                  ON probe_relation.relnamespace = probe_namespace.oid
                 AND probe_relation.relname = 'runs'
                 AND probe_relation.relkind IN ('r', 'p')
                WHERE role.rolname = current_user
                """
            ),
            {
                "tenant_tables": list(TENANT_RLS_TABLES),
                "version_table": RUNTIME_VERSION_TABLE,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DatabaseRuntimeContractError(
                "effective PostgreSQL runtime role is not visible in pg_roles"
            )
        _assert_runtime_version_contract(row)
        _assert_runtime_session_settings(row)
        tenant_result = await session.execute(
            text(
                f"""
                SELECT
                    relation.relname,
                    relation.relrowsecurity,
                    relation.relforcerowsecurity,
                    EXISTS (
                        SELECT 1
                        FROM pg_attribute AS attribute
                        WHERE attribute.attrelid = relation.oid
                          AND attribute.attname = 'tenant_id'
                          AND attribute.attnum > 0
                          AND NOT attribute.attisdropped
                    ) AS has_tenant_id,
                    has_table_privilege(
                        current_user, relation.oid, 'SELECT'
                    ) AS can_select,
                    has_table_privilege(
                        current_user, relation.oid, 'INSERT'
                    ) AS can_insert,
                    has_table_privilege(
                        current_user, relation.oid, 'UPDATE'
                    ) AS can_update,
                    has_table_privilege(
                        current_user, relation.oid, 'DELETE'
                    ) AS can_delete,
                    has_table_privilege(
                        current_user, relation.oid, 'TRUNCATE'
                    ) AS can_truncate,
                    has_table_privilege(
                        current_user, relation.oid, 'REFERENCES'
                    ) AS can_references,
                    has_table_privilege(
                        current_user, relation.oid, 'TRIGGER'
                    ) AS can_trigger,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            relation.relacl,
                            acldefault('r', relation.relowner)
                        )) AS acl
                        WHERE acl.privilege_type = 'MAINTAIN'
                          AND (
                              acl.grantee = 0
                              OR pg_has_role(
                                  current_user, acl.grantee, 'USAGE'
                              )
                          )
                    ) AS can_maintain,
                    (
                        has_table_privilege(
                            current_user,
                            relation.oid,
                            'SELECT WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'INSERT WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'UPDATE WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'DELETE WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'TRUNCATE WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'REFERENCES WITH GRANT OPTION'
                        )
                        OR has_table_privilege(
                            current_user,
                            relation.oid,
                            'TRIGGER WITH GRANT OPTION'
                        )
                    ) AS has_table_grant_option,
                    (
                        has_any_column_privilege(
                            current_user, relation.oid, 'REFERENCES'
                        )
                        OR (
                            {worm_relation_predicate}
                            AND has_any_column_privilege(
                                current_user, relation.oid, 'UPDATE'
                            )
                        )
                        OR has_any_column_privilege(
                            current_user,
                            relation.oid,
                            'SELECT WITH GRANT OPTION'
                        )
                        OR has_any_column_privilege(
                            current_user,
                            relation.oid,
                            'INSERT WITH GRANT OPTION'
                        )
                        OR has_any_column_privilege(
                            current_user,
                            relation.oid,
                            'UPDATE WITH GRANT OPTION'
                        )
                        OR has_any_column_privilege(
                            current_user,
                            relation.oid,
                            'REFERENCES WITH GRANT OPTION'
                        )
                    ) AS has_forbidden_column_privileges,
                    (
                        SELECT count(*)
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                    ) AS total_policy_count,
                    (
                        SELECT policy.polcmd::text
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                          AND policy.polname = :policy_name
                    ) AS policy_command,
                    (
                        SELECT policy.polpermissive
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                          AND policy.polname = :policy_name
                    ) AS policy_permissive,
                    (
                        SELECT policy.polroles = ARRAY[0::oid]
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                          AND policy.polname = :policy_name
                    ) AS policy_is_public,
                    (
                        SELECT pg_get_expr(
                            policy.polqual, policy.polrelid, true
                        )
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                          AND policy.polname = :policy_name
                    ) AS policy_using,
                    (
                        SELECT pg_get_expr(
                            policy.polwithcheck, policy.polrelid, true
                        )
                        FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                          AND policy.polname = :policy_name
                    ) AS policy_check,
                    {tenant_table_direct_acl} AS direct_acl_valid
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND relation.relkind IN ('r', 'p')
                  AND relation.relname = ANY(
                      CAST(:tenant_tables AS TEXT[])
                  )
                ORDER BY relation.relname
                """
            ),
            {
                "policy_name": TENANT_RLS_POLICY,
                "tenant_tables": list(TENANT_RLS_TABLES),
            },
        )
        tenant_rows = tenant_result.mappings().all()
        function_result = await session.execute(
            text(
                f"""
                WITH expected(function_signature) AS (
                    SELECT unnest(CAST(:function_signatures AS TEXT[]))
                ), resolved AS (
                    SELECT
                        function_signature,
                        to_regprocedure(
                            format(
                                '%I.%s',
                                current_schema(),
                                function_signature
                            )
                        ) AS function_oid
                    FROM expected
                )
                SELECT
                    function_signature,
                    function_oid IS NOT NULL AS function_exists,
                    COALESCE(
                        has_function_privilege(
                            current_user, function_oid, 'EXECUTE'
                        ),
                        false
                    ) AS can_execute,
                    COALESCE(
                        has_function_privilege(
                            current_user,
                            function_oid,
                            'EXECUTE WITH GRANT OPTION'
                        ),
                        false
                    ) AS execute_grant_option,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            routine.proacl,
                            acldefault('f', routine.proowner)
                        )) AS acl
                        WHERE acl.grantee = (
                            SELECT oid FROM pg_roles
                            WHERE rolname = 'inqtrix_app'
                        )
                          AND acl.privilege_type = 'EXECUTE'
                          AND NOT acl.is_grantable
                    ) AS explicit_execute,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            routine.proacl,
                            acldefault('f', routine.proowner)
                        )) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                    ) AS public_execute,
                    COALESCE({role_can_set_function_owner}, false)
                        AS can_assume_owner
                FROM resolved
                LEFT JOIN pg_proc AS routine
                  ON routine.oid = function_oid
                ORDER BY function_signature
                """
            ),
            {"function_signatures": list(RUNTIME_REQUIRED_FUNCTIONS)},
        )
        function_rows = function_result.mappings().all()
        sequence_result = await session.execute(
            text(
                f"""
                WITH expected(sequence_name) AS (
                    SELECT unnest(CAST(:sequence_names AS TEXT[]))
                )
                SELECT
                    expected.sequence_name,
                    relation.oid IS NOT NULL AS sequence_exists,
                    COALESCE(
                        has_sequence_privilege(
                            current_user, relation.oid, 'USAGE'
                        ),
                        false
                    ) AS can_use,
                    COALESCE(
                        has_sequence_privilege(
                            current_user, relation.oid, 'SELECT'
                        ),
                        false
                    ) AS can_select,
                    COALESCE(
                        has_sequence_privilege(
                            current_user, relation.oid, 'UPDATE'
                        ),
                        false
                    ) AS can_update,
                    COALESCE(
                        has_sequence_privilege(
                            current_user,
                            relation.oid,
                            'USAGE WITH GRANT OPTION'
                        ),
                        false
                    ) AS usage_grant_option,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            relation.relacl,
                            acldefault('s', relation.relowner)
                        )) AS acl
                        WHERE acl.grantee = (
                            SELECT oid FROM pg_roles
                            WHERE rolname = 'inqtrix_app'
                        )
                          AND acl.privilege_type = 'USAGE'
                          AND NOT acl.is_grantable
                    ) AS explicit_usage,
                    EXISTS (
                        SELECT 1
                        FROM aclexplode(COALESCE(
                            relation.relacl,
                            acldefault('s', relation.relowner)
                        )) AS acl
                        WHERE acl.grantee = 0
                    ) AS public_acl,
                    COALESCE({role_can_set_sequence_owner}, false)
                        AS can_assume_owner
                FROM expected
                LEFT JOIN pg_namespace AS namespace
                  ON namespace.nspname = current_schema()
                LEFT JOIN pg_class AS relation
                  ON relation.relnamespace = namespace.oid
                 AND relation.relname = expected.sequence_name
                 AND relation.relkind = 'S'
                ORDER BY expected.sequence_name
                """
            ),
            {"sequence_names": list(RUNTIME_REQUIRED_SEQUENCES)},
        )
        sequence_rows = sequence_result.mappings().all()
        capability_result = await session.execute(
            text(_RUNTIME_ROLE_CAPABILITY_AUDIT_SQL),
            {
                "tenant_tables": list(TENANT_RLS_TABLES),
                "version_table": RUNTIME_VERSION_TABLE,
                "function_signatures": list(RUNTIME_REQUIRED_FUNCTIONS),
                "sequence_names": list(RUNTIME_REQUIRED_SEQUENCES),
            },
        )
        capability_rows = capability_result.mappings().all()
        _assert_runtime_tenant_table_contract(tenant_rows)
        _assert_runtime_dependency_contract(function_rows, sequence_rows)
        contract = _assert_runtime_identity_contract(
            row,
            app_role=app_role,
            login_policy=login_policy,
        )
        _assert_runtime_role_capabilities(
            capability_rows,
            allow_legacy_session=login_policy == "bundled_legacy",
        )
        try:
            policy_probe = (
                await session.execute(
                    text(
                        "SELECT inqtrix_current_tenant_id() "
                        "AS runtime_policy_tenant"
                    )
                )
            ).mappings().one_or_none()
            await session.execute(
                text(
                    "SELECT run_id AS runtime_tenant_select_probe "
                    "FROM runs LIMIT 0"
                )
            )
        except SQLAlchemyError as exc:
            raise DatabaseRuntimeContractError(
                "tenant policy execution or protected-table probe failed"
            ) from exc
        if policy_probe is None or str(
            policy_probe["runtime_policy_tenant"] or ""
        ) != _RUNTIME_PROBE_TENANT:
            raise DatabaseRuntimeContractError(
                "tenant policy function did not resolve the transaction-local "
                "tenant GUC"
            )
    return contract


async def verify_database_url_runtime_contract(
    database_url: str,
    *,
    app_role: str,
    login_policy: DatabaseRuntimeLoginPolicy = "restricted",
) -> DatabaseRuntimeContract:
    """Verify one direct runtime URL without retaining a connection pool.

    This is the worker bootstrap variant. It keeps engine construction in one
    shared contract instead of letting each process implement a subtly
    different pre-claim check.

    Raises:
        DatabaseRuntimeUnavailableError: A known DNS, connection, capacity or
            timeout failure prevented verification.
        DatabaseRuntimeContractError: The reachable database violates the
            runtime role, schema, permission or tenant contract.
        Exception: Unexpected verifier defects and unclassified configuration
            failures remain visible and fatal to the worker.
    """
    engine = build_engine(database_url, null_pool=True)
    try:
        try:
            return await verify_database_runtime_contract(
                build_session_factory(engine),
                app_role=app_role,
                login_policy=login_policy,
            )
        except DatabaseRuntimeUnavailableError:
            raise
        except Exception as exc:
            if _is_database_runtime_unavailable(exc):
                raise DatabaseRuntimeUnavailableError(
                    "database runtime contract probe is temporarily unavailable"
                ) from exc
            raise
    finally:
        await engine.dispose()
