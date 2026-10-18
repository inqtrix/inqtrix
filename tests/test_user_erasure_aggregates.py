"""How the account erasure deletes ONE aggregate through its surface.

The erasure registers each aggregate deletion in the account's name and
runs it itself, inline, under the deletion machine's fence: a queued
child could wait forever behind the erasure that waits for it. These
tests pin that loop with recording fakes; the enumeration of what an
account owns is proven against PostgreSQL elsewhere.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from inqtrix.runs.deletion_operations import (
    DeletionOperationConflict,
    DeletionOperationStatus,
    DeletionTargetKind,
)
from inqtrix.runs.deletion_postgres import ClaimedDeletionOperation
from inqtrix.server.runs import RunActive, RunNotFound
from inqtrix.services.user_erasure_aggregates import DeletionMachineAggregates
from inqtrix.services.user_erasure_executor import AggregateRef

TENANT = "default"
USER = uuid.uuid4()


class _Store:
    """The deletion store, recording registrations, claims and terminals."""

    def __init__(self, *, status="queued", existing=None):
        self.status = status
        self.existing = existing or {}
        self.submitted: list[dict] = []
        self.retried: list[tuple[str, bool]] = []
        self.claims: list[tuple[str, bool]] = []
        self.terminal: dict[str, tuple[str, str | None]] = {}

    def submit(self, *, dispatch=True, **kwargs):
        self.submitted.append({"dispatch": dispatch, **kwargs})
        return {"operation_id": "op-1", "status": self.status}

    def retry(self, operation_id, *, dispatch=True, **kwargs):
        self.retried.append((operation_id, dispatch))
        return {"operation_id": operation_id, "status": "queued"}

    def find_for_asset(self, asset_id, **kwargs):
        return self.existing.get(asset_id)

    def claim_for_execution(self, operation_id, tenant_id, *, allow_takeover):
        self.claims.append((operation_id, allow_takeover))
        return ClaimedDeletionOperation(
            operation_id=operation_id,
            tenant_id=tenant_id,
            attempt=1,
            target_kind=DeletionTargetKind.AGENT_SESSION,
            target_id="target",
            manifest=(),
            vector_index_context=None,
            knowledge_context=None,
            session_context=None,
            created_by_user_id=USER,
            workspace_id="ws-a",
        )

    def is_attempt_current(self, operation_id, *, fence_attempt):
        return True

    def progress(self, operation_id, **kwargs):
        return True

    def complete(self, operation_id, *, fence_attempt=None):
        self.terminal[operation_id] = ("deleted", None)
        return True

    def fail(self, operation_id, message, *, error_type="server_error", fence_attempt=None):
        self.terminal[operation_id] = ("delete_failed", message)
        return True

    def get_record(self, operation_id, **kwargs):
        status, message = self.terminal.get(operation_id, (self.status, None))
        return SimpleNamespace(
            status=DeletionOperationStatus(status),
            error={"message": message} if message else None,
        )


class _Service:
    """The deletion service: registers through the store it is given and
    executes what is claimed."""

    def __init__(self, *, failure=None, file_in_deletion=None):
        self._failure = failure
        self._file_in_deletion = file_in_deletion
        self.executed: list[str] = []
        self._store = None

    def with_operation_store(self, store):
        clone = _Service(
            failure=self._failure, file_in_deletion=self._file_in_deletion
        )
        clone.executed = self.executed
        clone._store = store
        return clone

    async def start_agent_session(self, target_id, **kwargs):
        return self._store.submit(target_kind="agent_session", target_id=target_id)

    async def start_orphaned_source(self, source_id, **kwargs):
        return self._store.submit(target_kind="source", target_id=source_id)

    async def start_section(self, section_id, **kwargs):
        if self._file_in_deletion is not None:
            # The service names the file that is already in deletion.
            raise DeletionOperationConflict(self._file_in_deletion)
        return self._store.submit(target_kind="section", target_id=section_id)

    def execute(self, handle, **kwargs):
        self.executed.append(handle.operation_id)
        if self._failure is not None:
            raise self._failure
        handle.complete()


class _Runs:
    def __init__(self, error=None):
        self._error = error
        self.deleted: list[str] = []
        self.cancelled: list[str] = []

    def delete(self, run_id, *, workspace_id, requester_user_id):
        if self._error is not None:
            raise self._error
        self.deleted.append(run_id)

    def cancel(self, run_id, *, workspace_id, visible_to):
        self.cancelled.append(run_id)


def _port(store, service=None, runs=None) -> DeletionMachineAggregates:
    return DeletionMachineAggregates(
        service=service or _Service(),
        store=store,
        run_store=runs or _Runs(),
        session_factory=None,
        app_role="inqtrix_app",
    )


def _delete(port, ref) -> None:
    port.delete(ref, tenant_id=TENANT, user_id=USER)


SESSION = AggregateRef("agent_session", ("s-1",), "ws-a")
SECTION = AggregateRef("section", ("fsec-1",), "ws-a")


def test_an_aggregate_deletion_is_registered_without_a_worker_and_run_inline():
    store, service = _Store(), _Service()

    _delete(_port(store, service), SESSION)

    assert [entry["dispatch"] for entry in store.submitted] == [False]
    assert store.claims == [("op-1", True)]
    assert service.executed == ["op-1"]
    assert store.terminal["op-1"] == ("deleted", None)


def test_an_aggregate_already_deleted_is_not_run_again():
    store, service = _Store(status="deleted"), _Service()

    _delete(_port(store, service), SESSION)

    assert store.claims == []
    assert service.executed == []


def test_a_failed_aggregate_deletion_is_reopened_without_a_worker():
    store, service = _Store(status="delete_failed"), _Service()

    _delete(_port(store, service), SESSION)

    assert store.retried == [("op-1", False)]
    assert service.executed == ["op-1"]


def test_an_aggregate_deletion_that_did_not_finish_stops_the_erasure_with_its_reason():
    store = _Store()
    service = _Service(failure=RuntimeError("object store unreachable"))

    with pytest.raises(RuntimeError, match="object store unreachable"):
        _delete(_port(store, service), SESSION)


def test_a_library_section_goes_as_one_aggregate_through_the_machine():
    store, service = _Store(), _Service()

    _delete(_port(store, service), SECTION)

    assert store.submitted == [
        {"dispatch": False, "target_kind": "section", "target_id": "fsec-1"}
    ]
    assert store.claims == [("op-1", True)]
    assert service.executed == ["op-1"]


def test_a_section_whose_files_are_in_deletion_finishes_the_holding_operation():
    store = _Store(
        existing={"fa-2": {"operation_id": "op-earlier", "status": "running"}}
    )
    service = _Service(file_in_deletion="fa-2")

    _delete(_port(store, service), SECTION)

    assert store.submitted == []
    assert store.claims == [("op-earlier", True)]
    assert service.executed == ["op-earlier"]


def test_an_orphaned_source_goes_as_one_aggregate_through_the_machine():
    store, service = _Store(), _Service()

    _delete(_port(store, service), AggregateRef("source", ("document:1",), None))

    assert store.submitted == [
        {"dispatch": False, "target_kind": "source", "target_id": "document:1"}
    ]
    assert store.claims == [("op-1", True)]
    assert service.executed == ["op-1"]


def test_a_refused_section_without_a_holding_operation_stops_the_erasure():
    service = _Service(file_in_deletion="fa-1")

    with pytest.raises(DeletionOperationConflict):
        _delete(_port(_Store(), service), SECTION)


def test_a_run_still_running_is_asked_to_stop_and_the_erasure_retries_later():
    runs = _Runs(error=RunActive("run-1"))

    with pytest.raises(RuntimeError, match="still stopping"):
        _delete(_port(_Store(), runs=runs), AggregateRef("run", ("run-1",), "ws-a"))

    assert runs.cancelled == ["run-1"]


def test_a_run_already_gone_is_done():
    runs = _Runs(error=RunNotFound("run-1"))

    _delete(_port(_Store(), runs=runs), AggregateRef("run", ("run-1",), "ws-a"))

    assert runs.cancelled == []
