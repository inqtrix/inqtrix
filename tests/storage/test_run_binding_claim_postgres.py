"""A claim compares the run's binding with the contracts the worker carries.

The verdict decides before anything runs: the same contract continues, a run
that never ran takes the worker's contract, a run with stored state from an
older contract ends visibly and loses its checkpoint after the commit, and a
newer contract or a mode the worker has not registered rolls the claim back
and leaves the run and its checkpoint as they were.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, update

from inqtrix.harness.contracts import binding as binding_module
from inqtrix.harness.contracts.binding import RunBindingNewer, WorkflowUnavailable
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.runs_orm import run_events, runs

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"


@pytest.fixture(scope="session", autouse=True)
def binding_schema_migrated():
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
def store(cleaned):
    def cleanup(run_id: str, mode: str) -> None:
        # Runs on a thread once the claim has ended the run; the shared test
        # database may hand it other tests' runs, which the assertions skip.
        cleaned.append((run_id, mode, _committed_status(run_id)))

    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=_Dispatch(),
        recover_orphans=False,
        max_concurrent=1000,
        max_queue_size=1000,
        completed_ttl_seconds=10**9,
        incognito_ttl_seconds=10**9,
        worker_id=f"pytest-binding-{uuid.uuid4().hex[:8]}",
        checkpoint_cleanup=cleanup,
    )
    yield store
    store.close()


def _cleaned_for(cleaned: list, run_id: str) -> list:
    return [entry for entry in cleaned if entry[0] == run_id]


def _submit(store, mode: str, contract: int, *, attempt: int = 0) -> str:
    run_id = store.submit(
        question="Was gilt?",
        stack_name="default",
        work=lambda _handle: None,
        mode=mode,
        workflow_contract=contract,
        request_payload={"question": "Was gilt?", "body": {"mode": mode}},
    )["run_id"]
    if attempt:

        async def stored_state() -> None:
            async with store._session("default") as session:
                await session.execute(
                    update(runs).where(runs.c.run_id == run_id).values(attempt=attempt)
                )

        store._call(stored_state())
    return run_id


def _row(store, run_id: str) -> dict:
    async def read() -> dict:
        async with store._session("default") as session:
            row = (
                await session.execute(
                    select(
                        runs.c.status,
                        runs.c.workflow_contract,
                        runs.c.attempt,
                        runs.c.claimed_by,
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


def _claim(store, run_id: str, contracts: dict[str, int]):
    return store.claim_for_execution(
        run_id, "default", allow_takeover=False, workflow_contracts=contracts
    )


def test_a_run_bound_to_the_contract_the_worker_carries_is_claimed(store, cleaned):
    run_id = _submit(store, "agent_kernel", 2, attempt=1)

    claimed = _claim(store, run_id, {"agent_kernel": 2, "research": 1})

    assert claimed is not None and claimed.attempt == 2
    assert _row(store, run_id)["workflow_contract"] == 2
    assert _cleaned_for(cleaned, run_id) == []


def test_a_run_that_never_ran_takes_the_contract_the_worker_carries(store, cleaned):
    run_id = _submit(store, "agent_kernel", 1)

    claimed = _claim(store, run_id, {"agent_kernel": 2})

    assert claimed is not None and claimed.attempt == 1
    row = _row(store, run_id)
    assert (row["status"], row["workflow_contract"]) == ("running", 2)
    assert _cleaned_for(cleaned, run_id) == []


def test_a_run_with_stored_state_from_an_older_contract_ends_and_loses_its_checkpoint(
    store, cleaned
):
    run_id = _submit(store, "agent_kernel", 1, attempt=1)

    assert _claim(store, run_id, {"agent_kernel": 2}) is None

    row = _row(store, run_id)
    assert (row["status"], row["workflow_contract"], row["attempt"]) == (
        "cancelled",
        1,
        1,
    )
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


def test_a_run_bound_to_a_newer_contract_rolls_back_and_waits(store, cleaned):
    run_id = _submit(store, "agent_kernel", 3, attempt=1)

    with pytest.raises(RunBindingNewer, match="agent_kernel contract 3"):
        _claim(store, run_id, {"agent_kernel": 2})

    assert _row(store, run_id) == {
        "status": "queued",
        "workflow_contract": 3,
        "attempt": 1,
        "claimed_by": None,
    }
    assert _last_event(store, run_id)[0] == "inqtrix.run.queued"
    assert _cleaned_for(cleaned, run_id) == []


def test_a_mode_the_worker_has_not_registered_keeps_its_run_and_checkpoint(
    store, cleaned
):
    run_id = _submit(store, "knowledge", 1, attempt=1)

    with pytest.raises(WorkflowUnavailable, match="knowledge"):
        _claim(store, run_id, {"agent_kernel": 1, "research": 1})

    assert _row(store, run_id) == {
        "status": "queued",
        "workflow_contract": 1,
        "attempt": 1,
        "claimed_by": None,
    }
    assert _cleaned_for(cleaned, run_id) == []


def test_a_retired_mode_ends_its_run_visibly(store, cleaned, monkeypatch):
    monkeypatch.setattr(
        binding_module, "RETIRED_WORKFLOWS", frozenset({"workspace_agent"})
    )
    run_id = _submit(store, "workspace_agent", 1, attempt=1)

    assert _claim(store, run_id, {"agent_kernel": 1}) is None

    assert _row(store, run_id)["status"] == "cancelled"
    assert _last_event(store, run_id) == (
        "inqtrix.run.cancelled",
        {
            "status": "cancelled",
            "reason": "workflow_retired",
            "bound_contract": 1,
        },
    )
    assert _cleaned_for(cleaned, run_id) == [(run_id, "workspace_agent", "cancelled")]


def test_a_duplicate_dispatch_of_a_run_running_elsewhere_is_left_alone(store, cleaned):
    """Only a run this claim could take is judged; another worker owns this one."""
    older = _submit(store, "agent_kernel", 1, attempt=1)
    newer = _submit(store, "agent_kernel", 3, attempt=1)

    async def running_elsewhere() -> None:
        async with store._session("default") as session:
            await session.execute(
                update(runs)
                .where(runs.c.run_id.in_((older, newer)))
                .values(status="running", claimed_by="another-worker")
            )

    store._call(running_elsewhere())

    assert _claim(store, older, {"agent_kernel": 2}) is None
    assert _claim(store, newer, {"agent_kernel": 2}) is None
    for run_id, contract in ((older, 1), (newer, 3)):
        assert _row(store, run_id) == {
            "status": "running",
            "workflow_contract": contract,
            "attempt": 1,
            "claimed_by": "another-worker",
        }
        assert _cleaned_for(cleaned, run_id) == []


def test_a_run_taken_over_from_a_silent_worker_under_an_older_contract_ends(store, cleaned):
    """A redelivered run whose worker stopped is judged like a queued one."""
    run_id = _submit(store, "agent_kernel", 1, attempt=1)

    async def running_on_a_lost_worker() -> None:
        async with store._session("default") as session:
            await session.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(status="running", claimed_by="lost-worker")
            )

    store._call(running_on_a_lost_worker())

    assert (
        store.claim_for_execution(
            run_id, "default", allow_takeover=True, workflow_contracts={"agent_kernel": 2}
        )
        is None
    )

    assert _row(store, run_id)["status"] == "cancelled"
    assert _last_event(store, run_id)[1]["reason"] == "workflow_contract_changed"
    assert _cleaned_for(cleaned, run_id) == [(run_id, "agent_kernel", "cancelled")]
