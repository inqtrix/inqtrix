"""Static contracts for identity-owned share resource mappings."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql

from inqtrix.storage.identity_postgres import PostgresIdentityBackend


OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class _ScalarResult:
    def scalar_one_or_none(self) -> uuid.UUID:
        return OWNER_ID


class _RecordingSession:
    def __init__(self) -> None:
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statement = statement
        return _ScalarResult()


class _QueryResult:
    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        one: Any | None = None,
    ) -> None:
        self._rows = rows or []
        self._one = one

    def all(self) -> list[Any]:
        return self._rows

    def one_or_none(self) -> Any | None:
        return self._one


class _ReconcileSession:
    def __init__(self, results: list[_QueryResult]) -> None:
        self.statements: list[Any] = []
        self._results = iter(results)

    async def execute(self, statement: Any) -> _QueryResult:
        self.statements.append(statement)
        return next(self._results)


@pytest.mark.asyncio
async def test_editor_document_owner_mapping_is_tenant_scoped_and_locked() -> None:
    backend = PostgresIdentityBackend(
        session_factory=cast(Any, None),
        app_role="inqtrix_app",
    )
    session = _RecordingSession()

    owner = await backend._shareable_resource_owner(
        cast(Any, session),
        tenant_id="tenant-a",
        resource_type="editor_document",
        resource_id="ed_1",
        lock=True,
    )

    assert owner == OWNER_ID
    assert session.statement is not None
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "editor_documents.tenant_id = 'tenant-a'" in sql
    assert "editor_documents.id = 'ed_1'" in sql
    assert "editor_documents.deleted_at IS NULL" in sql
    assert "FOR UPDATE" in sql


@pytest.mark.asyncio
async def test_targeted_reconciliation_revokes_deleted_resource_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PostgresIdentityBackend(
        session_factory=cast(Any, None),
        app_role="inqtrix_app",
        restrict_to_workspace_members=True,
    )
    recipient_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    share_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    share = SimpleNamespace(
        id=share_id,
        tenant_id="tenant-a",
        recipient_user_id=recipient_id,
        resource_type="editor_document",
        resource_id="ed_1",
    )
    session = _ReconcileSession(
        [
            _QueryResult(rows=[share]),
            _QueryResult(one=share),
            _QueryResult(),
        ]
    )

    async def missing_live_owner(*args: Any, **kwargs: Any) -> None:
        return None

    async def ignore_effects(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(backend, "_shareable_resource_owner", missing_live_owner)
    monkeypatch.setattr(backend, "_append_share_effects", ignore_effects)

    revoked = await backend._reconcile_workspace_shares(
        cast(Any, session),
        tenant_id="tenant-a",
        actor_user_id=OWNER_ID,
        affected_user_ids={OWNER_ID},
    )

    assert revoked == 1
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN editor_documents" in sql
    assert "editor_documents.created_by_user_id IN" in sql
    assert "editor_documents.deleted_at IS NULL" not in sql
