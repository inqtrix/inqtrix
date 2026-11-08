"""The library facade without a process owner: a script, a notebook, an application.

Every case runs in one fresh interpreter, because the test session itself owns
a loop and the library loop only starts where nobody ever configured one. The
research graph is a fake that honours the cancel probe, so nothing leaves the
machine and no ``.env`` is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest

_SCRIPT = r"""
import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import inqtrix.graph as graph_module
from inqtrix import aio
from inqtrix.agent import AgentConfig, ResearchAgent

lines = []


class Collect(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())


bridge_log = logging.getLogger("inqtrix.aio")
bridge_log.addHandler(Collect())
bridge_log.setLevel(logging.INFO)

mode = {"name": "quick"}
finished = threading.Event()
saw_cancel = threading.Event()
ended = threading.Event()


def fake_run(question, *, history, progress_queue, providers, strategies, settings, cancel_event):
    try:
        if progress_queue is not None:
            progress_queue.put(("progress", "Schritt 1"))
        if mode["name"] == "slow":
            time.sleep(0.2)
            if progress_queue is not None:
                progress_queue.put(("progress", "Schritt 2"))
                progress_queue.put(("progress", "Schritt 3"))
            finished.set()
        elif mode["name"] == "until_cancelled":
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if cancel_event.wait(0.02):
                    saw_cancel.set()
                    break
        elif mode["name"] == "slow_to_stop":
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if cancel_event.wait(0.02):
                    saw_cancel.set()
                    time.sleep(0.3)
                    break
        return {"answer": f"Antwort auf {question}"}
    finally:
        ended.set()


graph_module.run = fake_run
ResearchAgent._ensure_initialised = lambda self: (None, SimpleNamespace(source_tiering=None), None)

out = {}
agent = ResearchAgent(AgentConfig())
out["research_twice"] = [agent.research("a").answer, agent.research("b").answer]
out["library_loop"] = aio._library_loop is not None and aio.default_loop() is aio._library_loop

with ThreadPoolExecutor(2) as callers:
    out["two_agents"] = list(
        callers.map(lambda question: ResearchAgent(AgentConfig()).research(question).answer, ["x", "y"])
    )


async def notebook_cell_sync():
    return agent.research("n").answer


out["foreign_loop_sync"] = asyncio.run(notebook_cell_sync())


async def notebook_cell_async():
    answer = (await agent.aresearch("m")).answer
    chunks = [chunk async for chunk in agent.astream("s")]
    return [answer, chunks]


out["foreign_loop_async"] = asyncio.run(notebook_cell_async())

mode["name"] = "slow"
stream = agent.stream("p")
first = next(stream)
out["reader_pause"] = [first, finished.wait(5)]
out["reader_rest"] = list(stream)

mode["name"] = "until_cancelled"
ended.clear()
stream = agent.stream("c")
next(stream)
stream.close()
out["early_close"] = [ended.is_set(), saw_cancel.is_set()]

ended.clear()
saw_cancel.clear()


async def early_aclose():
    from contextlib import aclosing

    async with aclosing(agent.astream("e")) as chunks:
        async for _chunk in chunks:
            break
    return [ended.is_set(), saw_cancel.is_set()]


out["early_aclose"] = asyncio.run(early_aclose())

saw_cancel.clear()


async def cancelled_aresearch():
    task = asyncio.ensure_future(agent.aresearch("k"))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


asyncio.run(cancelled_aresearch())
out["aresearch_cancel"] = saw_cancel.wait(5)

mode["name"] = "slow_to_stop"
ended.clear()
saw_cancel.clear()


async def cancelled_quiet_read():
    async def read():
        return [chunk async for chunk in agent.astream("q", include_progress=False)]

    task = asyncio.ensure_future(read())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return [ended.is_set(), saw_cancel.is_set()]


out["quiet_read_cancel"] = asyncio.run(cancelled_quiet_read())
out["background_loop_lines"] = [line for line in lines if "background loop" in line]
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def library_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """What the script saw, run once in a folder without ``.env``."""
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=tmp_path_factory.mktemp("library"),
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_research_runs_twice_on_the_one_library_loop(library_run: dict[str, Any]) -> None:
    assert library_run["research_twice"] == ["Antwort auf a", "Antwort auf b"]
    assert library_run["library_loop"] is True
    assert len(library_run["background_loop_lines"]) == 1


def test_two_agents_run_at_once_from_two_threads(library_run: dict[str, Any]) -> None:
    assert library_run["two_agents"] == ["Antwort auf x", "Antwort auf y"]


def test_a_notebook_cell_may_call_the_sync_and_the_async_methods(
    library_run: dict[str, Any],
) -> None:
    assert library_run["foreign_loop_sync"] == "Antwort auf n"
    answer, chunks = library_run["foreign_loop_async"]
    assert answer == "Antwort auf m"
    assert chunks == ["> Schritt 1\n", "---\n", "Antwort ", "auf ", "s"]


def test_the_run_goes_on_while_nobody_reads(library_run: dict[str, Any]) -> None:
    """The reader pauses after the first line; two more lines and the end arrive meanwhile."""
    first, finished_while_paused = library_run["reader_pause"]
    assert first == "> Schritt 1\n"
    assert finished_while_paused is True
    assert library_run["reader_rest"] == [
        "> Schritt 2\n",
        "> Schritt 3\n",
        "---\n",
        "Antwort ",
        "auf ",
        "p",
    ]


def test_closing_the_stream_early_ends_the_run_before_close_returns(
    library_run: dict[str, Any],
) -> None:
    ended, saw_cancel = library_run["early_close"]
    assert saw_cancel is True
    assert ended is True


def test_closing_the_async_stream_early_ends_the_run_before_aclose_returns(
    library_run: dict[str, Any],
) -> None:
    """``break`` alone leaves an async generator to Python's finalizer; ``aclosing`` ends it at once."""
    ended, saw_cancel = library_run["early_aclose"]
    assert saw_cancel is True
    assert ended is True


def test_a_cancelled_aresearch_ends_its_run(library_run: dict[str, Any]) -> None:
    assert library_run["aresearch_cancel"] is True


def test_a_cancelled_read_without_progress_waits_until_the_run_ended(
    library_run: dict[str, Any],
) -> None:
    """Without progress the read awaits the run itself; the cancel must not end it early."""
    ended, saw_cancel = library_run["quiet_read_cancel"]
    assert saw_cancel is True
    assert ended is True
