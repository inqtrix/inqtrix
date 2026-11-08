"""The periodic sweep the API process runs: lifecycle and a failing pass."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from inqtrix.periodic_sweep import PeriodicSweep


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.asyncio
async def test_a_sweep_outlives_a_failing_pass_and_restarts_cleanly():
    calls: list[int] = []
    second = threading.Event()

    async def sweep() -> None:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("database unreachable")
        second.set()

    periodic = PeriodicSweep(
        name="indexing-auto-resume",
        label="Indexing auto-resume",
        sweep=sweep,
        interval_seconds=1.0,
    )
    messages = _Messages()
    logger = logging.getLogger("inqtrix")
    logger.addHandler(messages)
    try:
        assert periodic.running is False
        periodic.start()
        periodic.start()
        assert periodic.running is True
        assert [
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name() == "inqtrix-indexing-auto-resume"
        ] == ["inqtrix-indexing-auto-resume"]
        assert await asyncio.to_thread(second.wait, 5)
        await periodic.aclose()
    finally:
        logger.removeHandler(messages)

    assert periodic.running is False
    assert messages.messages == [
        "Indexing auto-resume sweep failed; the next one follows in 1s "
        "(error_type=RuntimeError)"
    ]
    periodic.start()
    assert periodic.running is True
    await periodic.aclose()
    assert periodic.running is False


@pytest.mark.asyncio
async def test_a_sweep_that_can_never_succeed_again_stops_the_clock():
    calls: list[int] = []

    async def sweep() -> None:
        calls.append(1)
        raise KeyError("for good")

    periodic = PeriodicSweep(
        name="x", label="X", sweep=sweep, interval_seconds=1.0, finished_by=(KeyError,)
    )
    periodic.start()
    assert periodic._task is not None
    await asyncio.wait_for(periodic._task, timeout=5)
    assert calls == [1]
    assert periodic.running is False
    await periodic.aclose()
