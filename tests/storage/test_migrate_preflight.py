"""Behavioural contract for the destructive v0.2 migration preflight."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from inqtrix.storage import migrate
from inqtrix.storage.migrate import (
    AuthorityReferenceIssue,
    V02PreflightReport,
    V02TerminalizationReport,
    _assert_v02_database_quiescent,
    _assert_v02_terminalization_preflight,
    _required_legacy_columns,
    _terminalize_v02_legacy_work,
    _terminalize_v02_locked,
    _v02_index_failure_payload,
    _v02_migration_contract,
    _v02_preflight,
    _v02_run_failure_events,
)


def _report(**overrides: Any) -> V02PreflightReport:
    values = {
        "schema_revision": ("0044_agent_task_cancellation",),
        "authority_issues": (),
        "unsupported_active_shares": 0,
        "orphaned_active_share_resources": 0,
        "nonterminal_runs": 0,
        "nonterminal_reindex_jobs": 0,
        "required_tables_present": True,
        "legacy_schema_compatible": True,
    }
    values.update(overrides)
    return V02PreflightReport(**values)


def test_v02_preflight_is_ready_only_without_destructive_blockers() -> None:
    assert _report().ready is True

    blockers = (
        {"required_tables_present": False},
        {"legacy_schema_compatible": False},
        {
            "authority_issues": (
                AuthorityReferenceIssue(
                    source="runs.created_by_sub",
                    orphaned=1,
                    ambiguous=0,
                ),
            )
        },
        {"unsupported_active_shares": 1},
        {"nonterminal_runs": 1},
        {"nonterminal_reindex_jobs": 1},
    )

    assert all(_report(**blocker).ready is False for blocker in blockers)


def test_v02_preflight_reports_orphan_shares_for_locked_revocation() -> None:
    report = _report(orphaned_active_share_resources=3)

    assert report.ready is True
    assert report.orphaned_active_share_resources == 3


def test_v02_preflight_json_contains_derived_ready_state() -> None:
    report = _report(unsupported_active_shares=2)

    payload = report.as_json_dict()

    assert payload["ready"] is False
    assert payload["unsupported_active_shares"] == 2
    assert payload["schema_revision"] == ("0044_agent_task_cancellation",)


def test_v02_preflight_uses_the_0045_authority_inventory() -> None:
    migration = _v02_migration_contract()

    required = _required_legacy_columns(migration)

    for spec in (*migration._AUTHORITY_COLUMNS, *migration._SHARE_AUTHORITY_COLUMNS):
        assert {"tenant_id", spec.legacy}.issubset(required[spec.table])
    assert set(migration._EXACT_AUTHORITY_TABLES) == {
        "auth_sessions",
        "personal_access_tokens",
        "local_credentials",
    }
    assert set(migration._SHARE_RESOURCE_TABLES) == {
        "run",
        "knowledge_collection",
        "prompt_template",
        "skill_template",
    }
    assert {"created_at", "accepted_at"}.issubset(
        required["resource_shares"]
    )
    assert {"snapshot", "finished_at", "error", "event_seq"}.issubset(
        required["runs"]
    )
    assert {"run_id", "sequence", "data"}.issubset(
        required["run_events"]
    )
    assert {
        "job_id",
        "operation_kind",
        "document_id",
        "revision_id",
        "total_documents",
        "completed_documents",
        "current_document_title",
        "finished_at",
        "error",
        "event_seq",
    }.issubset(required["indexing_jobs"])
    assert {"job_id", "sequence", "data"}.issubset(
        required["indexing_job_events"]
    )


def test_v02_terminalization_accepts_only_live_work_as_a_blocker() -> None:
    _assert_v02_terminalization_preflight(
        _report(nonterminal_runs=2, nonterminal_reindex_jobs=1)
    )

    with pytest.raises(RuntimeError, match="blockers other than"):
        _assert_v02_terminalization_preflight(
            _report(
                nonterminal_runs=2,
                unsupported_active_shares=1,
            )
        )


def test_v02_run_terminalization_uses_normal_terminal_event_order() -> None:
    events = _v02_run_failure_events({"phase": "search"})

    assert [event_type for event_type, _data in events] == [
        "inqtrix.run.snapshot",
        "inqtrix.run.failed",
    ]
    assert events[0][1] == {
        "status": "failed",
        "snapshot": {"phase": "search"},
    }
    assert events[1][1]["error"]["type"] == "platform_upgrade"
    assert events[1][1]["snapshot"] == {"phase": "search"}


def test_v02_index_terminalization_uses_normal_failure_snapshot() -> None:
    payload = _v02_index_failure_payload(
        {
            "job_id": "ix_legacy",
            "total_documents": 4,
            "completed_documents": 1,
            "current_document_title": "Document 2",
        },
        finished_at=20.0,
    )

    assert payload["status"] == "failed"
    assert payload["error"]["type"] == "platform_upgrade"
    assert payload["snapshot"] == {
        "completed_documents": 1,
        "total_documents": 4,
        "progress_estimate": 0.25,
        "current_document_title": "Document 2",
        "phase": "queued",
        "current_batch": 0,
        "total_batches": 0,
    }


def test_v02_locked_preflight_runs_action_inside_its_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyResult:
        def __iter__(self) -> Iterator[tuple[str, str]]:
            return iter(())

    class Transaction:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> None:
            self.connection.in_transaction = True

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: Any,
        ) -> bool:
            self.connection.in_transaction = False
            return False

    class Connection:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.in_transaction = False

        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: Any,
        ) -> bool:
            return False

        def begin(self) -> Transaction:
            return Transaction(self)

        async def execute(self, statement: Any) -> EmptyResult:
            self.calls.append(str(statement))
            return EmptyResult()

    class Engine:
        def __init__(self) -> None:
            self.connection = Connection()
            self.disposed = False

        def connect(self) -> Connection:
            return self.connection

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    from inqtrix.storage import db as storage_db

    monkeypatch.setattr(
        storage_db,
        "build_engine",
        lambda _url, *, null_pool: engine,
    )

    async def action(
        connection: Any,
        report: V02PreflightReport,
    ) -> str:
        assert connection is engine.connection
        assert connection.in_transaction is True
        assert report.ready is False
        return "acted"

    result = asyncio.run(
        _v02_preflight(
            "postgresql+asyncpg://operator@db/inqtrix",
            locked_action=action,
        )
    )

    assert result == "acted"
    assert engine.connection.in_transaction is False
    assert engine.disposed is True
    assert "SET LOCAL row_security = off" in engine.connection.calls
    assert any(
        "ACCESS EXCLUSIVE MODE NOWAIT" in sql
        for sql in engine.connection.calls
    )


def test_v02_terminalization_refuses_other_database_clients() -> None:
    class Result:
        def scalar_one(self) -> int:
            return 2

    class Connection:
        async def execute(self, statement: Any) -> Result:
            assert "pg_stat_activity" in str(statement)
            return Result()

    with pytest.raises(RuntimeError, match="2 other database client"):
        asyncio.run(_assert_v02_database_quiescent(Connection()))


def test_v02_terminalization_writes_both_lifecycle_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(
            self,
            *,
            scalar: int | None = None,
            rows: tuple[dict[str, Any], ...] = (),
        ) -> None:
            self._scalar = scalar
            self._rows = rows

        def scalar_one(self) -> int:
            assert self._scalar is not None
            return self._scalar

        def mappings(self) -> Result:
            return self

        def all(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self,
            statement: Any,
            parameters: dict[str, Any] | None = None,
        ) -> Result:
            sql = str(statement)
            self.calls.append((sql, dict(parameters or {})))
            if sql.startswith("SELECT count(*) FROM pg_stat_activity"):
                return Result(scalar=0)
            if sql.startswith("SELECT run_id"):
                return Result(
                    rows=(
                        {
                            "run_id": "run_legacy",
                            "tenant_id": "tenant-a",
                            "snapshot": {"phase": "search"},
                        },
                    )
                )
            if sql.startswith("UPDATE runs"):
                return Result(scalar=7)
            if sql.startswith("SELECT job_id"):
                return Result(
                    rows=(
                        {
                            "job_id": "ix_legacy",
                            "tenant_id": "tenant-a",
                            "total_documents": 4,
                            "completed_documents": 1,
                            "current_document_title": "Document 2",
                        },
                    )
                )
            if sql.startswith("UPDATE indexing_jobs"):
                return Result(scalar=4)
            return Result()

    connection = Connection()
    action_active = False

    def locked_now() -> float:
        assert action_active is True
        return 20.0

    async def locked_preflight(
        database_url: str,
        *,
        locked_action: Any,
        managed_rls_mode: str,
        services_quiesced: bool,
    ) -> V02TerminalizationReport:
        nonlocal action_active
        assert database_url == "postgresql+asyncpg://operator@db/inqtrix"
        assert managed_rls_mode == "auto"
        assert services_quiesced is True
        action_active = True
        try:
            return await locked_action(
                connection,
                _report(nonterminal_runs=1, nonterminal_reindex_jobs=1),
            )
        finally:
            action_active = False

    monkeypatch.setattr(migrate, "_v02_preflight", locked_preflight)
    monkeypatch.setattr(migrate.time, "time", locked_now)

    report = asyncio.run(
        _terminalize_v02_legacy_work("postgresql+asyncpg://operator@db/inqtrix")
    )

    assert report == V02TerminalizationReport(
        runs_terminalized=1,
        reindex_jobs_terminalized=1,
    )

    run_events = [
        params
        for sql, params in connection.calls
        if sql.startswith("INSERT INTO run_events")
    ]
    assert [event["sequence"] for event in run_events] == [6, 7]
    assert {event["created_at"] for event in run_events} == {20.0}
    assert [event["event_type"] for event in run_events] == [
        "inqtrix.run.snapshot",
        "inqtrix.run.failed",
    ]
    assert json.loads(run_events[-1]["data_json"])["error"]["type"] == (
        "platform_upgrade"
    )

    indexing_events = [
        params
        for sql, params in connection.calls
        if sql.startswith("INSERT INTO indexing_job_events")
    ]
    assert len(indexing_events) == 1
    assert indexing_events[0]["sequence"] == 4
    assert json.loads(indexing_events[0]["data_json"])["error"]["type"] == (
        "platform_upgrade"
    )


def test_v02_locked_terminalization_rejects_non_work_preflight_blocker() -> None:
    class Connection:
        async def execute(self, statement: Any) -> Any:
            pytest.fail(f"database must not be touched: {statement}")

    with pytest.raises(RuntimeError, match="blockers other than"):
        asyncio.run(
            _terminalize_v02_locked(
                Connection(),
                _report(unsupported_active_shares=1),
                run_terminal_statuses_sql=(
                    "('cancelled', 'completed', 'failed')"
                ),
                indexing_terminal_statuses_sql=(
                    "('cancelled', 'completed', 'failed')"
                ),
            )
        )


def test_v02_terminalization_requires_explicit_service_shutdown() -> None:
    with pytest.raises(RuntimeError, match="services_stopped=True"):
        migrate.terminalize_v02_legacy_work(
            "postgresql+asyncpg://operator@db/inqtrix"
        )


def test_v02_terminalization_cli_exits_without_running_migrations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def terminalize(
        database_url: str,
        *,
        services_stopped: bool,
        rls_mode: str,
    ) -> V02TerminalizationReport:
        assert services_stopped is True
        assert rls_mode == "auto"
        seen.append(database_url)
        return V02TerminalizationReport(
            runs_terminalized=3,
            reindex_jobs_terminalized=2,
        )

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail(
            "main must not run a migration or separate preflight in "
            "terminalization mode"
        )

    monkeypatch.setattr(migrate, "terminalize_v02_legacy_work", terminalize)
    monkeypatch.setattr(migrate, "run_migrations", unexpected)
    monkeypatch.setattr(migrate, "preflight_v02", unexpected)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inqtrix-migrate",
            "--database-url",
            "postgresql+asyncpg://operator@db/inqtrix",
            "--terminalize-v02-work",
            "--confirm-services-stopped",
        ],
    )

    migrate.main()

    assert seen == ["postgresql+asyncpg://operator@db/inqtrix"]
    assert json.loads(capsys.readouterr().out) == {
        "reason": "platform_upgrade",
        "reindex_jobs_terminalized": 2,
        "runs_terminalized": 3,
    }


def test_v02_terminalization_cli_requires_shutdown_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("terminalization must not start without confirmation")

    monkeypatch.setattr(migrate, "terminalize_v02_legacy_work", unexpected)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inqtrix-migrate",
            "--database-url",
            "postgresql+asyncpg://operator@db/inqtrix",
            "--terminalize-v02-work",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        migrate.main()

    assert exc_info.value.code == 2
