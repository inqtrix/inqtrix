"""Watch every event loop of a test for a single step that holds it up."""

from __future__ import annotations

import asyncio
import gc
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from inqtrix import aio

SLOW_STEP_SECONDS = 0.05


class SlowSteps(logging.Handler):
    """Collects what asyncio's debug mode says about a slow step."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Executing "):
            self.messages.append(message)


@contextmanager
def watch_for_blocked_loops(monkeypatch: pytest.MonkeyPatch) -> Iterator[SlowSteps]:
    """Report each step of a loop that runs longer than ``SLOW_STEP_SECONDS``.

    One thread drives a process's loop, so a call that blocks there holds up every
    run and request of the process. Debug mode times each step and logs the slow
    ones; ``set_debug`` stays forced on because a test runner may switch it off
    again after building its loop.
    """
    build_loop = asyncio.BaseEventLoop.__init__
    set_debug = asyncio.BaseEventLoop.set_debug

    def build_watched_loop(self, *args, **kwargs):
        build_loop(self, *args, **kwargs)
        self.slow_callback_duration = SLOW_STEP_SECONDS

    def keep_debug_on(self, enabled):
        del enabled
        set_debug(self, True)

    # A collection over everything earlier tests left behind lands on whichever
    # step is running; that says nothing about the step.
    gc.collect()
    gc.disable()
    # The loop sync code reaches was built before any test ran.
    process_loop = aio.default_loop()
    if process_loop is not None:
        process_loop.slow_callback_duration = SLOW_STEP_SECONDS
        set_debug(process_loop, True)
    slow = SlowSteps()
    logger = logging.getLogger("asyncio")
    logger.addHandler(slow)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio.BaseEventLoop, "__init__", build_watched_loop)
            patch.setattr(asyncio.BaseEventLoop, "set_debug", keep_debug_on)
            yield slow
    finally:
        gc.enable()
        logger.removeHandler(slow)
        if process_loop is not None and not process_loop.is_closed():
            set_debug(process_loop, False)
