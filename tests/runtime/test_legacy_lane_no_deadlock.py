"""Whole sync segments that wait for a provider call never starve it.

A worker runs old sync engine code as one segment on the transition lane. When
that code calls a provider through the sync facade, the call goes to the process
loop and from there, like every Bedrock request, onto the provider lane. Were both
the same lane, segments holding every thread would wait for calls that can never
start. Here every transition thread is busy with such a segment at once.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from inqtrix import aio
from inqtrix.providers.attempts import call_on_loop
from tests.test_aio import on_loop, process_loop

_SEGMENTS = 2
_HANG_SECONDS = 5.0


@pytest.fixture(autouse=True)
def no_process_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """This test builds the process loop itself, the way its owner does."""
    monkeypatch.setattr(aio, "_states", {})
    monkeypatch.setattr(aio, "_default_loop", None)
    monkeypatch.setattr(aio, "_owner_configured", True)
    monkeypatch.setattr(aio, "_library_loop", None)


def _converse(question: str) -> str:
    """A blocking provider client, as boto3's ``converse`` is."""
    time.sleep(0.05)
    return f"Antwort auf {question}"


async def _achat(question: str) -> str:
    # The Bedrock adapter's path: the blocking client runs on the provider lane.
    return await aio.run_provider_blocking(_converse, question)


def test_segments_on_every_transition_thread_still_get_their_provider_calls() -> None:
    every_thread_busy = threading.Barrier(_SEGMENTS)

    def segment(index: int) -> str:
        every_thread_busy.wait(_HANG_SECONDS)
        # The sync facade every adapter offers to old code.
        return call_on_loop(_achat(f"Frage {index}"))

    async def worker() -> list[str]:
        segments = (aio.run_legacy_segment(lambda index=index: segment(index)) for index in range(_SEGMENTS))
        return await asyncio.wait_for(asyncio.gather(*segments), _HANG_SECONDS)

    with process_loop(legacy=_SEGMENTS, provider=1) as loop:
        assert on_loop(loop, worker()) == [f"Antwort auf Frage {index}" for index in range(_SEGMENTS)]
