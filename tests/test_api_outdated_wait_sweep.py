"""Without a queue worker, the API process ends outdated parked runs at its start."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.server.app import end_outdated_waits_in_process, start_job_stores

CONTRACTS = {"agent_kernel": 7, "research": 1}


class _RunStore:
    def __init__(self, *, ended: int = 0, fails: bool = False) -> None:
        self.calls: list[Any] = []
        self._ended = ended
        self._fails = fails

    async def astart(self) -> None:
        self.calls.append("astart")

    async def aend_outdated_waits(self, workflow_contracts: dict[str, int]) -> int:
        self.calls.append(dict(workflow_contracts))
        if self._fails:
            raise RuntimeError("database gone")
        return self._ended


def _container(store: _RunStore, *, queue: str = "memory", storage: str = "postgres") -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(
            queue=SimpleNamespace(backend=queue),
            storage=SimpleNamespace(backend=storage),
        ),
        registry=SimpleNamespace(workflow_contracts=lambda: dict(CONTRACTS)),
        run_store=store,
    )


def test_the_api_process_ends_outdated_waits_with_its_registrys_contracts(caplog):
    store = _RunStore(ended=2)
    with caplog.at_level(logging.INFO, logger="inqtrix.server.app"):
        asyncio.run(start_job_stores(_container(store)))
    # After the store settled what the previous process left behind.
    assert store.calls == ["astart", CONTRACTS]
    assert "API process ended 2 parked runs" in caplog.text


@pytest.mark.parametrize(
    ("queue", "storage"),
    [("valkey", "postgres"), ("memory", "memory")],
    ids=["a-queue-worker-does-it", "no-durable-rows"],
)
def test_the_api_process_leaves_the_sweep_to_whoever_owns_it(queue, storage):
    store = _RunStore()
    asyncio.run(end_outdated_waits_in_process(_container(store, queue=queue, storage=storage)))
    assert store.calls == []


def test_a_failed_sweep_is_named_and_does_not_stop_the_start(caplog):
    store = _RunStore(fails=True)
    with caplog.at_level(logging.WARNING, logger="inqtrix.server.app"):
        asyncio.run(end_outdated_waits_in_process(_container(store)))
    assert store.calls == [CONTRACTS]
    assert "they stay parked until their wait expires" in caplog.text
