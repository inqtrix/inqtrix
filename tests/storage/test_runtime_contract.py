"""Behavioral tests for the API/worker PostgreSQL runtime contract."""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from inqtrix.server.database_gate import install_database_contract_gate
from inqtrix.settings import Settings
from inqtrix.storage.migration_contract import (
    WORM_TENANT_TABLES,
    RUNTIME_REQUIRED_FUNCTIONS,
    RUNTIME_REQUIRED_SEQUENCES,
    RUNTIME_VERSION_TABLE,
    SCHEMA_HEAD_REVISION,
    TENANT_RLS_TABLES,
)
from inqtrix.storage.runtime_contract import (
    DatabaseRuntimeContractError,
    DatabaseRuntimeUnavailableError,
    verify_database_runtime_contract,
    verify_database_url_runtime_contract,
)


class _Result:
    def __init__(
        self,
        value: dict[str, object] | list[dict[str, object]] | None,
    ) -> None:
        self._value = value

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict[str, object] | None:
        assert not isinstance(self._value, list)
        return self._value

    def all(self) -> list[dict[str, object]]:
        assert isinstance(self._value, list)
        return self._value


class _Session:
    def __init__(
        self,
        row: dict[str, object],
        tenant_rows: list[dict[str, object]],
        function_rows: list[dict[str, object]],
        sequence_rows: list[dict[str, object]],
        capability_rows: list[dict[str, object]],
        policy_tenant: str,
    ) -> None:
        self.row = row
        self.tenant_rows = tenant_rows
        self.function_rows = function_rows
        self.sequence_rows = sequence_rows
        self.capability_rows = capability_rows
        self.policy_tenant = policy_tenant
        self.statement = ""

    async def execute(self, statement, parameters=None):
        rendered = str(statement)
        self.statement = f"{self.statement}\n{rendered}"
        if "runtime_policy_tenant" in rendered:
            assert parameters is None
            return _Result({"runtime_policy_tenant": self.policy_tenant})
        if "runtime_tenant_select_probe" in rendered:
            assert parameters is None
            return _Result([])
        if "runtime_identity_capabilities" in rendered:
            assert parameters == {
                "tenant_tables": list(TENANT_RLS_TABLES),
                "version_table": RUNTIME_VERSION_TABLE,
                "function_signatures": list(RUNTIME_REQUIRED_FUNCTIONS),
                "sequence_names": list(RUNTIME_REQUIRED_SEQUENCES),
            }
            return _Result(self.capability_rows)
        if "total_policy_count" in rendered:
            assert parameters == {
                "policy_name": "tenant_isolation",
                "tenant_tables": list(TENANT_RLS_TABLES),
            }
            return _Result(self.tenant_rows)
        if "to_regprocedure" in rendered:
            assert parameters == {
                "function_signatures": list(RUNTIME_REQUIRED_FUNCTIONS)
            }
            return _Result(self.function_rows)
        if "sequence_names" in rendered:
            assert parameters == {
                "sequence_names": list(RUNTIME_REQUIRED_SEQUENCES)
            }
            return _Result(self.sequence_rows)
        assert parameters == {
            "tenant_tables": list(TENANT_RLS_TABLES),
            "version_table": RUNTIME_VERSION_TABLE,
        }
        return _Result(self.row)


def _valid_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "effective_role": "inqtrix_app",
        "session_role": "inqtrix_runtime",
        "is_superuser": False,
        "bypasses_rls": False,
        "can_login": False,
        "can_create_database": False,
        "can_create_role": False,
        "can_replicate": False,
        "schema_usage": True,
        "schema_create": False,
        "tenant_id": "default",
        "row_security_enabled": True,
        "transaction_read_only": False,
        "probe_rls_active": True,
        "revision": SCHEMA_HEAD_REVISION,
        "version_table_exists": True,
        "version_resolves_in_current_schema": True,
        "version_can_select": True,
        "version_can_insert": False,
        "version_can_update": False,
        "version_can_delete": False,
        "version_can_truncate": False,
        "version_can_references": False,
        "version_can_trigger": False,
        "version_can_maintain": False,
        "version_can_assume_owner": False,
        "version_has_grant_option": False,
        "version_has_forbidden_column_privileges": False,
        "version_direct_acl_valid": True,
        "owns_rls_table": False,
        "can_assume_rls_owner": False,
        "can_assume_rls_bypass": False,
        "session_is_superuser": False,
        "session_bypasses_rls": False,
        "session_can_login": True,
        "session_can_create_database": False,
        "session_can_create_role": False,
        "session_can_replicate": False,
        "session_schema_create": False,
        "session_owns_rls_table": False,
        "session_owns_all_rls_tables": False,
        "session_can_assume_rls_owner": False,
        "session_can_assume_rls_bypass": False,
    }
    row.update(overrides)
    return row


def _valid_tenant_rows() -> list[dict[str, object]]:
    return [
        {
            "relname": table_name,
            "relrowsecurity": True,
            "relforcerowsecurity": True,
            "has_tenant_id": True,
            "can_select": True,
            "can_insert": True,
            "can_update": table_name not in WORM_TENANT_TABLES,
            "can_delete": table_name not in WORM_TENANT_TABLES,
            "can_truncate": False,
            "can_references": False,
            "can_trigger": False,
            "can_maintain": False,
            "has_table_grant_option": False,
            "has_forbidden_column_privileges": False,
            "direct_acl_valid": True,
            "total_policy_count": 1,
            "policy_command": "*",
            "policy_permissive": True,
            "policy_is_public": True,
            "policy_using": "tenant_id = inqtrix_current_tenant_id()",
            "policy_check": "tenant_id = inqtrix_current_tenant_id()",
        }
        for table_name in TENANT_RLS_TABLES
    ]


def _tenant_rows_with(
    table_name: str,
    **overrides: object,
) -> list[dict[str, object]]:
    rows = _valid_tenant_rows()
    target = next(row for row in rows if row["relname"] == table_name)
    target.update(overrides)
    return rows


def _valid_function_rows() -> list[dict[str, object]]:
    return [
        {
            "function_signature": signature,
            "function_exists": True,
            "can_execute": True,
            "execute_grant_option": False,
            "explicit_execute": True,
            "public_execute": False,
            "can_assume_owner": False,
        }
        for signature in RUNTIME_REQUIRED_FUNCTIONS
    ]


def _valid_sequence_rows() -> list[dict[str, object]]:
    return [
        {
            "sequence_name": sequence_name,
            "sequence_exists": True,
            "can_use": True,
            "can_select": False,
            "can_update": False,
            "usage_grant_option": False,
            "explicit_usage": True,
            "public_acl": False,
            "can_assume_owner": False,
        }
        for sequence_name in RUNTIME_REQUIRED_SEQUENCES
    ]


def _dependency_rows_with(
    rows: list[dict[str, object]],
    identifier_field: str,
    identifier: str,
    **overrides: object,
) -> list[dict[str, object]]:
    target = next(row for row in rows if row[identifier_field] == identifier)
    target.update(overrides)
    return rows


def _bind_tenant_session(
    monkeypatch,
    row: dict[str, object],
    *,
    tenant_rows: list[dict[str, object]] | None = None,
    function_rows: list[dict[str, object]] | None = None,
    sequence_rows: list[dict[str, object]] | None = None,
    capability_rows: list[dict[str, object]] | None = None,
    policy_tenant: str = "default",
    expected_app_role: str = "inqtrix_app",
) -> _Session:
    session = _Session(
        row,
        tenant_rows if tenant_rows is not None else _valid_tenant_rows(),
        function_rows if function_rows is not None else _valid_function_rows(),
        sequence_rows if sequence_rows is not None else _valid_sequence_rows(),
        capability_rows
        if capability_rows is not None
        else [
            {
                "identity_name": identity_name,
                "has_forbidden_capabilities": False,
            }
            for identity_name in ("effective", "session")
        ],
        policy_tenant,
    )

    @asynccontextmanager
    async def fake_tenant_session(
        session_factory,
        *,
        tenant_id: str,
        app_role: str,
    ):
        assert session_factory is _SESSION_FACTORY
        assert tenant_id == "default"
        assert app_role == expected_app_role
        yield session

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.tenant_session",
        fake_tenant_session,
    )
    return session


_SESSION_FACTORY = object()


@pytest.mark.asyncio
async def test_runtime_contract_uses_tenant_session_and_accepts_exact_head(
    monkeypatch,
) -> None:
    session = _bind_tenant_session(monkeypatch, _valid_row())

    contract = await verify_database_runtime_contract(
        _SESSION_FACTORY,
        app_role="inqtrix_app",
    )

    assert contract.revision == SCHEMA_HEAD_REVISION
    assert contract.effective_role == "inqtrix_app"
    assert contract.session_role == "inqtrix_runtime"
    assert "alembic_version" in session.statement
    assert "relrowsecurity" in session.statement
    assert "current_schema()" in session.statement
    assert "namespace.nspname = 'public'" not in session.statement
    assert "has_function_privilege" in session.statement
    assert "has_sequence_privilege" in session.statement
    assert "has_database_privilege" in session.statement
    assert "database.datdba" in session.statement
    assert "namespace.nspowner" in session.statement
    assert "pg_attribute" in session.statement
    assert "row_security_active" in session.statement
    assert "runtime_tenant_select_probe" in session.statement
    assert "THEN 'SET' ELSE 'MEMBER' END" in session.statement


@pytest.mark.asyncio
@pytest.mark.parametrize("app_role", ("custom_app", ""))
async def test_runtime_contract_preserves_configurable_restricted_app_role(
    monkeypatch,
    app_role: str,
) -> None:
    effective_role = app_role or "inqtrix_runtime"
    session = _bind_tenant_session(
        monkeypatch,
        _valid_row(
            effective_role=effective_role,
            can_login=not bool(app_role),
        ),
        expected_app_role=app_role,
    )

    contract = await verify_database_runtime_contract(
        _SESSION_FACTORY,
        app_role=app_role,
    )

    assert contract.effective_role == effective_role
    assert "rolname = 'inqtrix_app'" in session.statement


@pytest.mark.asyncio
async def test_runtime_contract_rejects_a_missing_expected_tenant_table(
    monkeypatch,
) -> None:
    rows = [
        row for row in _valid_tenant_rows() if row["relname"] != "runs"
    ]
    _bind_tenant_session(monkeypatch, _valid_row(), tenant_rows=rows)

    with pytest.raises(DatabaseRuntimeContractError, match="missing.*runs"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"has_tenant_id": False}, "missing tenant_id"),
        ({"relrowsecurity": False}, "enabled and forced"),
        ({"relforcerowsecurity": False}, "enabled and forced"),
        ({"total_policy_count": 2}, "canonical fail-closed policy"),
        ({"can_select": False}, "required table grants"),
        ({"can_insert": False}, "required table grants"),
        ({"can_update": False}, "required table grants"),
        ({"can_delete": False}, "required table grants"),
        ({"can_truncate": True}, "required table grants"),
        ({"can_references": True}, "required table grants"),
        ({"can_trigger": True}, "required table grants"),
        ({"can_maintain": True}, "required table grants"),
        ({"has_table_grant_option": True}, "required table grants"),
        ({"has_forbidden_column_privileges": True}, "required table grants"),
        ({"direct_acl_valid": False}, "canonical application grants"),
    ],
)
async def test_runtime_contract_rejects_tenant_catalog_drift(
    monkeypatch,
    override: dict[str, object],
    message: str,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(),
        tenant_rows=_tenant_rows_with("runs", **override),
    )

    with pytest.raises(DatabaseRuntimeContractError, match=message):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("privilege", ("can_update", "can_delete"))
async def test_runtime_contract_keeps_audit_log_append_only(
    monkeypatch,
    privilege: str,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(),
        tenant_rows=_tenant_rows_with("audit_log", **{privilege: True}),
    )

    with pytest.raises(DatabaseRuntimeContractError, match="required table grants"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "override", "message"),
    [
        ("function", {"function_exists": False}, "tenant functions"),
        ("function", {"can_execute": False}, "tenant functions"),
        ("function", {"execute_grant_option": True}, "tenant functions"),
        ("function", {"explicit_execute": False}, "tenant functions"),
        ("function", {"public_execute": True}, "tenant functions"),
        ("function", {"can_assume_owner": True}, "tenant functions"),
        ("sequence", {"sequence_exists": False}, "sequence grants"),
        ("sequence", {"can_use": False}, "sequence grants"),
        ("sequence", {"can_select": True}, "sequence grants"),
        ("sequence", {"can_update": True}, "sequence grants"),
        ("sequence", {"usage_grant_option": True}, "sequence grants"),
        ("sequence", {"explicit_usage": False}, "sequence grants"),
        ("sequence", {"public_acl": True}, "sequence grants"),
        ("sequence", {"can_assume_owner": True}, "sequence grants"),
    ],
)
async def test_runtime_contract_rejects_dependency_acl_drift(
    monkeypatch,
    kind: str,
    override: dict[str, object],
    message: str,
) -> None:
    function_rows = _valid_function_rows()
    sequence_rows = _valid_sequence_rows()
    if kind == "function":
        function_rows = _dependency_rows_with(
            function_rows,
            "function_signature",
            RUNTIME_REQUIRED_FUNCTIONS[0],
            **override,
        )
    else:
        sequence_rows = _dependency_rows_with(
            sequence_rows,
            "sequence_name",
            RUNTIME_REQUIRED_SEQUENCES[0],
            **override,
        )
    _bind_tenant_session(
        monkeypatch,
        _valid_row(),
        function_rows=function_rows,
        sequence_rows=sequence_rows,
    )

    with pytest.raises(DatabaseRuntimeContractError, match=message):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"revision": "0048_old"}, "schema revision"),
        ({"is_superuser": True}, "NOSUPERUSER"),
        ({"bypasses_rls": True}, "NOBYPASSRLS"),
        ({"can_login": True}, "NOLOGIN"),
        ({"can_create_database": True}, "NOCREATEDB"),
        ({"can_create_role": True}, "NOCREATEROLE"),
        ({"can_replicate": True}, "NOREPLICATION"),
        ({"schema_usage": False}, "schema USAGE"),
        ({"schema_create": True}, "schema USAGE"),
        ({"owns_rls_table": True}, "must not own"),
        ({"can_assume_rls_owner": True}, "table-owner role"),
        ({"can_assume_rls_bypass": True}, "SUPERUSER or BYPASSRLS role"),
        ({"session_is_superuser": True}, "runtime session login"),
        ({"session_bypasses_rls": True}, "runtime session login"),
        ({"session_can_login": False}, "runtime session login"),
        ({"session_can_create_database": True}, "runtime session login"),
        ({"session_can_create_role": True}, "runtime session login"),
        ({"session_can_replicate": True}, "runtime session login"),
        ({"session_schema_create": True}, "runtime session login"),
        ({"session_owns_rls_table": True}, "must not own or assume"),
        ({"session_can_assume_rls_owner": True}, "must not own or assume"),
        ({"session_can_assume_rls_bypass": True}, "must not own or assume"),
        ({"tenant_id": ""}, "tenant GUC"),
        ({"row_security_enabled": False}, "row_security=on"),
        ({"probe_rls_active": False}, "row_security=on"),
        ({"transaction_read_only": True}, "write transactions"),
    ],
)
async def test_runtime_contract_rejects_unsafe_or_stale_state(
    monkeypatch,
    override: dict[str, object],
    message: str,
) -> None:
    _bind_tenant_session(monkeypatch, _valid_row(**override))

    with pytest.raises(DatabaseRuntimeContractError, match=message):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    (
        {"version_table_exists": False},
        {"version_resolves_in_current_schema": False},
        {"version_can_select": False},
        {"version_can_insert": True},
        {"version_can_update": True},
        {"version_can_delete": True},
        {"version_can_truncate": True},
        {"version_can_references": True},
        {"version_can_trigger": True},
        {"version_can_maintain": True},
        {"version_can_assume_owner": True},
        {"version_has_grant_option": True},
        {"version_has_forbidden_column_privileges": True},
        {"version_direct_acl_valid": False},
    ),
)
async def test_runtime_contract_rejects_version_table_drift(
    monkeypatch,
    override: dict[str, object],
) -> None:
    _bind_tenant_session(monkeypatch, _valid_row(**override))

    with pytest.raises(DatabaseRuntimeContractError, match="alembic_version"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_name", ("effective", "session"))
async def test_runtime_contract_rejects_forbidden_assumable_capabilities(
    monkeypatch,
    identity_name: str,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(),
        capability_rows=[
            {
                "identity_name": name,
                "has_forbidden_capabilities": name == identity_name,
            }
            for name in ("effective", "session")
        ],
    )

    with pytest.raises(
        DatabaseRuntimeContractError,
        match="forbidden direct/inherited/SET ROLE capabilities",
    ):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
async def test_runtime_contract_executes_policy_function_probe(
    monkeypatch,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(),
        policy_tenant="wrong-tenant",
    )

    with pytest.raises(DatabaseRuntimeContractError, match="policy function"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
        )


@pytest.mark.asyncio
async def test_bundled_legacy_accepts_only_owner_superuser_below_strict_app_role(
    monkeypatch,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(
            session_is_superuser=True,
            session_owns_rls_table=True,
            session_owns_all_rls_tables=True,
        ),
        capability_rows=[
            {
                "identity_name": "effective",
                "has_forbidden_capabilities": False,
            },
            {
                "identity_name": "session",
                "has_forbidden_capabilities": True,
            },
        ],
    )

    contract = await verify_database_runtime_contract(
        _SESSION_FACTORY,
        app_role="inqtrix_app",
        login_policy="bundled_legacy",
    )

    assert contract.session_role == "inqtrix_runtime"


@pytest.mark.asyncio
async def test_bundled_legacy_never_relaxes_the_effective_app_role(
    monkeypatch,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(
            can_login=True,
            session_is_superuser=True,
            session_owns_all_rls_tables=True,
        ),
    )

    with pytest.raises(DatabaseRuntimeContractError, match="NOLOGIN"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
            login_policy="bundled_legacy",
        )


@pytest.mark.asyncio
async def test_bundled_legacy_rejects_a_non_owner_bypass_login(
    monkeypatch,
) -> None:
    _bind_tenant_session(
        monkeypatch,
        _valid_row(
            session_bypasses_rls=True,
            session_owns_all_rls_tables=False,
        ),
    )

    with pytest.raises(DatabaseRuntimeContractError, match="bundled LOGIN"):
        await verify_database_runtime_contract(
            _SESSION_FACTORY,
            app_role="inqtrix_app",
            login_policy="bundled_legacy",
        )


@pytest.mark.asyncio
async def test_url_contract_always_disposes_probe_engine(monkeypatch) -> None:
    engine = SimpleNamespace(disposed=False)

    async def dispose() -> None:
        engine.disposed = True

    engine.dispose = dispose
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_engine",
        lambda database_url, *, null_pool: engine,
    )
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_session_factory",
        lambda active_engine: _SESSION_FACTORY,
    )

    async def fail_contract(session_factory, *, app_role: str, login_policy: str):
        assert login_policy == "restricted"
        raise DatabaseRuntimeContractError("stale")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        fail_contract,
    )

    with pytest.raises(DatabaseRuntimeContractError, match="stale"):
        await verify_database_url_runtime_contract(
            "postgresql+asyncpg://runtime.invalid/inqtrix",
            app_role="inqtrix_app",
        )
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_url_contract_types_transient_connectivity_failures(
    monkeypatch,
) -> None:
    engine = SimpleNamespace(disposed=False)

    async def dispose() -> None:
        engine.disposed = True

    engine.dispose = dispose
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_engine",
        lambda database_url, *, null_pool: engine,
    )
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_session_factory",
        lambda active_engine: _SESSION_FACTORY,
    )

    failure = socket.gaierror(-2, "temporary name resolution failure")

    async def fail_contract(session_factory, *, app_role: str, login_policy: str):
        raise failure

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        fail_contract,
    )

    with pytest.raises(Exception) as caught:
        await verify_database_url_runtime_contract(
            "postgresql+asyncpg://runtime.invalid/inqtrix",
            app_role="inqtrix_app",
        )

    assert isinstance(caught.value, DatabaseRuntimeUnavailableError)
    assert caught.value.__cause__ is failure
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_url_contract_does_not_hide_unknown_probe_failures(
    monkeypatch,
) -> None:
    engine = SimpleNamespace(disposed=False)

    async def dispose() -> None:
        engine.disposed = True

    engine.dispose = dispose
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_engine",
        lambda database_url, *, null_pool: engine,
    )
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_session_factory",
        lambda active_engine: _SESSION_FACTORY,
    )

    failure = RuntimeError("unexpected verifier bug")

    async def fail_contract(session_factory, *, app_role: str, login_policy: str):
        raise failure

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        fail_contract,
    )

    with pytest.raises(RuntimeError) as caught:
        await verify_database_url_runtime_contract(
            "postgresql+asyncpg://runtime.invalid/inqtrix",
            app_role="inqtrix_app",
        )

    assert caught.value is failure
    assert engine.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlstate", "expected_type"),
    [
        ("57P03", DatabaseRuntimeUnavailableError),
        ("28P01", OperationalError),
    ],
)
async def test_url_contract_classifies_sqlstate_without_hiding_auth_errors(
    monkeypatch,
    sqlstate: str,
    expected_type: type[BaseException],
) -> None:
    engine = SimpleNamespace(disposed=False)

    async def dispose() -> None:
        engine.disposed = True

    engine.dispose = dispose
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_engine",
        lambda database_url, *, null_pool: engine,
    )
    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.build_session_factory",
        lambda active_engine: _SESSION_FACTORY,
    )

    origin = SimpleNamespace(sqlstate=sqlstate)
    failure = OperationalError("runtime probe", {}, origin)

    async def fail_contract(session_factory, *, app_role: str, login_policy: str):
        raise failure

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        fail_contract,
    )

    with pytest.raises(Exception) as caught:
        await verify_database_url_runtime_contract(
            "postgresql+asyncpg://runtime.invalid/inqtrix",
            app_role="inqtrix_app",
        )

    assert isinstance(caught.value, expected_type)
    if sqlstate == "57P03":
        assert caught.value.__cause__ is failure
    else:
        assert caught.value is failure
    assert engine.disposed is True


def test_http_database_gate_allows_diagnostics_but_blocks_product_routes() -> None:
    settings = Settings()
    settings.storage.backend = "postgres"  # type: ignore[assignment]
    container = SimpleNamespace(settings=settings)
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/runs")
    async def runs() -> list[object]:
        return []

    install_database_contract_gate(app, container=container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        blocked = client.get("/v1/runs")
        assert blocked.status_code == 503
        assert blocked.json()["error"]["type"] == "database_not_ready"

        app.state.database_contract_ready = True
        assert client.get("/v1/runs").status_code == 200
