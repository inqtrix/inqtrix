"""The loop guard itself: it reports a blocked loop and nothing else."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from inqtrix import aio
from tests._loop_guard import watch_for_blocked_loops

_BLOCK_SECONDS = 0.2


@pytest.mark.parametrize(
    "blocking_call",
    [
        pytest.param(lambda: time.sleep(_BLOCK_SECONDS), id="sleep"),
        pytest.param(lambda: threading.Event().wait(_BLOCK_SECONDS), id="thread-event"),
    ],
)
def test_a_blocking_call_on_a_new_loop_is_reported(
    monkeypatch: pytest.MonkeyPatch, blocking_call: Callable[[], object]
) -> None:
    async def handler_that_blocks() -> None:
        blocking_call()

    with watch_for_blocked_loops(monkeypatch) as slow:
        asyncio.run(handler_that_blocks())

    assert len(slow.messages) == 1
    assert "handler_that_blocks" in slow.messages[0]


def test_a_blocking_call_on_the_process_loop_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def coroutine_that_blocks() -> None:
        time.sleep(_BLOCK_SECONDS)

    with watch_for_blocked_loops(monkeypatch) as slow:
        aio.call_from_thread(coroutine_that_blocks())

    assert len(slow.messages) == 1
    assert "coroutine_that_blocks" in slow.messages[0]


def test_the_same_call_off_the_loop_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler_that_waits_elsewhere() -> None:
        await asyncio.to_thread(time.sleep, _BLOCK_SECONDS)

    with watch_for_blocked_loops(monkeypatch) as slow:
        asyncio.run(handler_that_waits_elsewhere())

    assert slow.messages == []


def test_a_loop_built_after_it_is_not_watched(monkeypatch: pytest.MonkeyPatch) -> None:
    with watch_for_blocked_loops(monkeypatch):
        pass

    loop = asyncio.new_event_loop()
    try:
        assert loop.get_debug() is False
    finally:
        loop.close()
