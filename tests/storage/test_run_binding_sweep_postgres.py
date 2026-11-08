"""A parked run that its binding no longer lets resume ends at once, with its lineage.

Waiting for an approval, an answer or its children, such a run would only end at
its next claim, after a person decided for nothing. The worker's sweep ends it
visibly with the claim's reason. A run that ends this way cancels its active
children with ``parent_cancelled``; a child that ends this way wakes its parent.
A newer binding and a mode the worker has not registered stay parked; those an
instance admin cancels, after reading them in the operator list.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import select, update

from inqtrix.harness.contracts import binding as binding_module
from inqtrix.harness.contracts.binding import RunBindingNewer
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.storage.db import build_engine
from inqtrix.storage.identity_orm import audit_log, users
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.runs_orm import run_events, runs

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"


@pytest.fixture(scope="session", autouse=True)
def binding_sweep_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


class _Dispatch:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, *, run_id: str, tenant_id: str) -> None:
        del tenant_id
        self.enqueued.append(run_id)


def _committed_status(run_id: str) -> str | None:
    """The status other connections see, read on this thread's own loop."""

    async def read() -> str | None:
        engine = build_engine(TEST_DATABASE_URL, null_pool=True)
        try:
            async with engine.connect() as connection:
                return (
                    await connection.execute(
                        select(runs.c.status).where(runs.c.run_id == run_id)
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.fixture()
def cleaned():
    return []


@pytest.fixture()
def dispatch():
    return _Dispatch()


@pytest.fixture()
def store(cleaned, dispatch):
    def cleanup(run_id: str, mode: str) -> None:
        # The shared test database may hand it other tests' runs as well.
        cleaned.append((run_id, mode, _committed_status(run_id)))

    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=dispatch,
        recover_orphans=False,
        max_concurrent=1000,
        max_queue_size=1000,
        completed_ttl_seconds=10**9,
        incognito_ttl_seconds=10**9,
        worker_id=f"pytest-sweep-{uuid.uuid4().hex[:8]}",
        checkpoint_cleanup=cleanup,
    )
    yield store
    store.close()


def _cleaned_for(cleaned: list, run_id: str) -> list:
    return [entry for entry in cleaned if entry[0] == run_id]


def _submit(store, mode: str, contract: int, **lineage) -> str:
    return store.submit(
        question="Was gilt?",
        stack_name="default",
        work=lambda _handle: None,
        mode=mode,
        workflow_contract=contract,
        request_payload={"question": "Was gilt?", "body": {"mode": mode}},
        **lineage,
    )["run_id"]


def _child(store, parent_id: str, mode: str, contract: int) -> str:
    return _submit(
        store,
        mode,
        contract,
        kind="agent_child",
        parent_run_id=parent_id,
        root_run_id=parent_id,
    )


def _set(store, run_id: str, **values) -> None:
    async def write() -> None:
        async with store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id == run_id).values(**values))

    store._call(write())


def _park(store, run_id: str, status: str) -> None:
    _set(store, run_id, status=status, attempt=1, waiting_since=time.time())


def _row(store, run_id: str) -> dict:
    async def read() -> dict:
        async with store._session("default") as session:
            row = (
                await session.execute(
                    select(
                        runs.c.status, runs.c.cancel_requested, runs.c.workflow_contract
                    ).where(runs.c.run_id == run_id)
                )
            ).mappings().one()
        return dict(row)

    return store._call(read())


def _last_event(store, run_id: str) -> tuple[str, dict]:
    async def read() -> tuple[str, dict]:
        async with store._session("default") as session:
            row = (
                await session.execute(
                    select(run_events.c.type, run_events.c.data)
                    .where(run_events.c.run_id == run_id)
                    .order_by(run_events.c.sequence.desc())
                    .limit(1)
                )
            ).one()
        return str(row[0]), dict(row[1] or {})

    return store._call(read())


def _admin(store) -> uuid.UUID:
    """A user row for the admin an audit row names."""
    user_id = uuid.uuid4()

    async def write() -> None:
        async with store._session("default") as session:
            await session.execute(
                users.insert().values(
                    id=user_id,
                    tenant_id="default",
                    issuer="https://binding-tests.example",
                    subject=f"admin-{user_id.hex}",
                    email=f"admin-{user_id.hex}@example.com",
                )
            )

    store._call(write())
    return user_id


def _audit(store, run_id: str) -> list[tuple[str, uuid.UUID | None, dict]]:
    async def read() -> list[tuple[str, uuid.UUID | None, dict]]:
        async with store._session("default") as session:
            rows = (
                await session.execute(
                    select(audit_log.c.action, audit_log.c.actor_user_id, audit_log.c.detail)
                    .where(audit_log.c.resource_id == run_id)
                    .order_by(audit_log.c.id)
                )
            ).all()
        return [(str(row[0]), row[1], dict(row[2] or {})) for row in rows]

    return store._call(read())


def test_a_parked_run_bound_to_an_older_contract_ends_at_once(store, cleaned):
    run_id = _submit(store, "agent_kernel", 1)
    _park(store, run_id, "waiting_for_approval")

    store.end_outdated_waits({"agent_kernel": 2})

    assert _row(store, run_id)["status"] == "cancelled"
    assert _last_event(store, run_id) == (
        "inqtrix.run.cancelled",
        {
            "status": "cancelled",
            "reason": "workflow_contract_changed",
            "bound_contract": 1,
            "worker_contract": 2,
        },
    )
    assert _cleaned_for(cleaned, run_id) == [(run_id, "agent_kernel", "cancelled")]


def test_a_parked_run_of_a_retired_mode_ends_at_once(store, cleaned, monkeypatch):
    monkeypatch.setattr(
        binding_module, "RETIRED_WORKFLOWS", frozenset({"workspace_agent"})
    )
    run_id = _submit(store, "workspace_agent", 1)
    _park(store, run_id, "waiting_for_input")

    store.end_outdated_waits({"agent_kernel": 1})

    assert _last_event(store, run_id)[1]["reason"] == "workflow_retired"
    assert _cleaned_for(cleaned, run_id) == [(run_id, "workspace_agent", "cancelled")]


def test_newer_bindings_and_unregistered_modes_stay_parked(store, cleaned):
    newer = _submit(store, "agent_kernel", 3)
    unregistered = _submit(store, "knowledge", 1)
    current = _submit(store, "agent_kernel", 2)
    for run_id in (newer, unregistered, current):
        _park(store, run_id, "waiting_for_approval")

    store.end_outdated_waits({"agent_kernel": 2, "research": 1})

    for run_id in (newer, unregistered, current):
        assert _row(store, run_id)["status"] == "waiting_for_approval"
        assert _cleaned_for(cleaned, run_id) == []


def test_a_child_ended_for_its_binding_wakes_its_parent(store, cleaned, dispatch):
    parent = _submit(store, "agent_kernel", 2, kind="agent")
    child = _child(store, parent, "agent_kernel", 1)
    _park(store, child, "waiting_for_approval")
    _park(store, parent, "waiting_for_children")

    store.end_outdated_waits({"agent_kernel": 2})

    assert _row(store, child)["status"] == "cancelled"
    assert _last_event(store, child)[1]["reason"] == "workflow_contract_changed"
    assert _row(store, parent)["status"] == "queued"
    assert parent in dispatch.enqueued
    assert _cleaned_for(cleaned, parent) == []


def test_a_parent_ended_by_the_sweep_cancels_its_active_children(store, cleaned):
    parent = _submit(store, "agent_kernel", 1, kind="agent")
    queued = _child(store, parent, "research", 1)
    running = _child(store, parent, "research", 1)
    waiting = _child(store, parent, "agent_kernel", 2)
    _set(store, running, status="running", attempt=1, claimed_by="another-worker")
    _park(store, waiting, "waiting_for_approval")
    _park(store, parent, "waiting_for_children")

    store.end_outdated_waits({"agent_kernel": 2, "research": 1})

    assert _last_event(store, parent)[1]["reason"] == "workflow_contract_changed"
    for child in (queued, waiting):
        assert _row(store, child)["status"] == "cancelled"
        assert _last_event(store, child)[1]["reason"] == "parent_cancelled"
    assert _row(store, running)["status"] == "running"
    assert _row(store, running)["cancel_requested"] is True
    assert _last_event(store, running) == (
        "inqtrix.run.cancel_requested",
        {"status": "running", "reason": "parent_cancelled"},
    )


def test_a_parent_ended_at_its_claim_cancels_its_running_child(store, cleaned):
    parent = _submit(store, "agent_kernel", 1, kind="agent")
    running = _child(store, parent, "research", 1)
    _set(store, running, status="running", attempt=1, claimed_by="another-worker")
    _set(store, parent, attempt=1)

    assert (
        store.claim_for_execution(
            parent,
            "default",
            allow_takeover=False,
            workflow_contracts={"agent_kernel": 2, "research": 1},
        )
        is None
    )

    assert _row(store, parent)["status"] == "cancelled"
    assert _row(store, running)["cancel_requested"] is True
    assert _last_event(store, running)[1]["reason"] == "parent_cancelled"


def test_the_operator_list_counts_open_runs_and_names_those_outside_a_contract(store):
    """A mode of its own keeps the counts free of other tests' runs."""
    mode = f"probe_{uuid.uuid4().hex[:8]}"
    old_parked = _submit(store, mode, 1)
    _park(store, old_parked, "waiting_for_approval")
    old_queued = _submit(store, mode, 1)
    newer = _submit(store, mode, 3)
    finished = _submit(store, mode, 1)
    _set(store, finished, status="completed")

    below = store.binding_overview(mode=mode, below=2, above=None, limit=10)
    above = store.binding_overview(mode=mode, below=None, above=2, limit=10)
    cut = store.binding_overview(mode=mode, below=2, above=None, limit=1)

    assert below["counts"] == [
        {"mode": mode, "status": "queued", "workflow_contract": 1, "runs": 1},
        {"mode": mode, "status": "queued", "workflow_contract": 3, "runs": 1},
        {"mode": mode, "status": "waiting_for_approval", "workflow_contract": 1, "runs": 1},
    ]
    assert [run["run_id"] for run in below["runs"]] == [old_parked, old_queued]
    assert below["runs"][0] == {
        "run_id": old_parked,
        "mode": mode,
        "status": "waiting_for_approval",
        "workflow_contract": 1,
    }
    assert (below["runs_total"], below["truncated"]) == (2, False)
    assert [run["run_id"] for run in above["runs"]] == [newer]
    assert ([run["run_id"] for run in cut["runs"]], cut["runs_total"], cut["truncated"]) == (
        [old_parked],
        2,
        True,
    )


def test_a_parent_woken_by_a_claim_goes_out_with_that_claim(store, dispatch):
    parent = _submit(store, "agent_kernel", 2, kind="agent")
    child = _child(store, parent, "agent_kernel", 1)
    _set(store, child, attempt=1)
    _park(store, parent, "waiting_for_children")
    dispatch.enqueued.clear()

    assert (
        store.claim_for_execution(
            child, "default", allow_takeover=False, workflow_contracts={"agent_kernel": 2}
        )
        is None
    )

    assert _row(store, parent)["status"] == "queued"
    assert dispatch.enqueued == [parent]


def test_an_admin_cancel_ends_the_runs_of_a_mode_this_server_cannot_run(store):
    """Bound above the server's contract, or every run of a mode it does not register."""
    mode = f"probe_{uuid.uuid4().hex[:8]}"
    above_parked = _submit(store, mode, 3)
    _park(store, above_parked, "waiting_for_approval")
    above_queued = _submit(store, mode, 3)
    current = _submit(store, mode, 2)
    older = _submit(store, mode, 1)
    admin = _admin(store)

    outcome = store.cancel_outside_binding(
        mode=mode, workflow_contracts={mode: 2}, actor_user_id=admin
    )

    assert outcome == {"cancelled": 2, "cancel_requested": 0}
    for run_id in (above_parked, above_queued):
        assert _row(store, run_id)["status"] == "cancelled"
        assert _last_event(store, run_id) == (
            "inqtrix.run.cancelled",
            {"status": "cancelled", "reason": "cancelled_by_admin"},
        )
        assert _audit(store, run_id)[-1] == (
            "run.cancelled_by_admin",
            admin,
            {"mode": mode, "bound_contract": 3, "server_contract": 2},
        )
    assert _row(store, current)["status"] == "queued"
    assert _row(store, older)["status"] == "queued"

    assert store.cancel_outside_binding(
        mode=mode, workflow_contracts={"research": 1}, actor_user_id=admin
    ) == {"cancelled": 2, "cancel_requested": 0}
    assert _row(store, current)["status"] == "cancelled"
    assert _row(store, older)["status"] == "cancelled"
    assert _audit(store, older)[-1][2] == {
        "mode": mode,
        "bound_contract": 1,
        "server_contract": None,
    }


def _event_types(store, run_id: str) -> list[str]:
    async def read() -> list[str]:
        async with store._session("default") as session:
            return [
                str(value)
                for value in (
                    await session.execute(
                        select(run_events.c.type)
                        .where(run_events.c.run_id == run_id)
                        .order_by(run_events.c.sequence)
                    )
                ).scalars()
            ]

    return store._call(read())


def test_a_run_already_asked_to_stop_counts_apart_and_is_not_asked_again(store):
    """The list counts a running run with a pending cancel as asked; a second cancel leaves it alone.

    Counted as cancellable, the run was offered again after the first cancel,
    and a second cancel asked it again and wrote a second admin audit row.
    """
    mode = f"probe_{uuid.uuid4().hex[:8]}"
    running = _submit(store, mode, 3)
    _set(store, running, status="running", attempt=1, claimed_by="a-live-worker")
    admin = _admin(store)
    contracts = {mode: 2}

    first = store.cancel_outside_binding(
        mode=mode, workflow_contracts=contracts, actor_user_id=admin
    )
    overview = store.binding_overview(mode=mode, below=None, above=2, limit=10)
    second = store.cancel_outside_binding(
        mode=mode, workflow_contracts=contracts, actor_user_id=admin
    )

    assert first == {"cancelled": 0, "cancel_requested": 1}
    assert overview["counts"] == [
        {"mode": mode, "status": "cancel_requested", "workflow_contract": 3, "runs": 1}
    ]
    assert [run["status"] for run in overview["runs"]] == ["cancel_requested"]
    assert second == {"cancelled": 0, "cancel_requested": 0}
    assert _event_types(store, running).count("inqtrix.run.cancel_requested") == 1
    assert [row[0] for row in _audit(store, running)].count("run.cancelled_by_admin") == 1


def test_an_admin_cancel_takes_the_children_along_and_ends_the_stop_cycle(store):
    """A run no worker of this version may take ends at its next claim instead of stopping it."""
    mode = f"probe_{uuid.uuid4().hex[:8]}"
    parent = _submit(store, mode, 3, kind="agent")
    waiting_child = _child(store, parent, "research", 1)
    _park(store, waiting_child, "waiting_for_approval")
    _park(store, parent, "waiting_for_children")
    stuck = _submit(store, mode, 3)
    _set(store, stuck, status="running", attempt=1, claimed_by="a-worker-that-is-gone")
    contracts = {mode: 2, "research": 1}
    with pytest.raises(RunBindingNewer):
        store.claim_for_execution(
            stuck, "default", allow_takeover=True, workflow_contracts=contracts
        )

    outcome = store.cancel_outside_binding(
        mode=mode, workflow_contracts=contracts, actor_user_id=_admin(store)
    )

    assert outcome == {"cancelled": 1, "cancel_requested": 1}
    assert _row(store, parent)["status"] == "cancelled"
    assert _last_event(store, waiting_child) == (
        "inqtrix.run.cancelled",
        {"status": "cancelled", "reason": "parent_cancelled"},
    )
    assert _row(store, stuck) == {
        "status": "running",
        "cancel_requested": True,
        "workflow_contract": 3,
    }
    assert (
        store.claim_for_execution(
            stuck, "default", allow_takeover=True, workflow_contracts=contracts
        )
        is None
    )
    assert _row(store, stuck)["status"] == "cancelled"
    assert _last_event(store, stuck) == (
        "inqtrix.run.cancelled",
        {"status": "cancelled", "reason": "cancelled_by_admin"},
    )
