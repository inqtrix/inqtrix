"""The periodic sweep the API process runs: lifecycle and a failing pass."""

from __future__ import annotations

import logging
import threading

from inqtrix.periodic_sweep import PeriodicSweep


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_sweep_outlives_a_failing_pass_and_restarts_cleanly():
    calls: list[int] = []
    second = threading.Event()

    def sweep() -> None:
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
            thread.name for thread in threading.enumerate()
            if thread.name == "inqtrix-indexing-auto-resume"
        ] == ["inqtrix-indexing-auto-resume"]
        assert second.wait(timeout=5)
        periodic.close()
    finally:
        logger.removeHandler(messages)

    assert periodic.running is False
    assert messages.messages == [
        "Indexing auto-resume sweep failed; the next one follows in 1s "
        "(error_type=RuntimeError)"
    ]
    periodic.start()
    assert periodic.running is True
    periodic.close()
    assert periodic.running is False
