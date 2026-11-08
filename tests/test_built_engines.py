"""The cleanup of a test's engines reports what it could not close."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests import _built_engines as built_engines


class _Pool:
    def __init__(self, idle: int = 0, in_use: int = 0) -> None:
        self._idle = idle
        self._in_use = in_use

    def checkedin(self) -> int:
        return self._idle

    def checkedout(self) -> int:
        return self._in_use


class _Engine:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.disposed = 0
        self.dropped = 0
        self.sync_engine = SimpleNamespace(dispose=self._drop)

    def _drop(self, *, close: bool) -> None:
        assert close is False
        self.dropped += 1

    async def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture(autouse=True)
def empty_lists():
    saved = dict(built_engines._built)
    built_engines._built.clear()
    built_engines._stranded.clear()
    yield
    built_engines._built.clear()
    built_engines._built.update(saved)
    built_engines._stranded.clear()


def _closed_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    loop.close()
    return loop


@pytest.mark.parametrize(
    ("pool", "expected"),
    [(_Pool(idle=2), 2), (_Pool(in_use=1), 1), (_Pool(), 0)],
)
def test_connections_on_a_dead_loop_are_reported(pool, expected) -> None:
    engine = _Engine(pool)
    built_engines._built[engine] = _closed_loop()

    assert built_engines.stranded() == expected
    assert engine.dropped == (1 if expected else 0)
    assert built_engines._built == {}


def test_connections_on_a_loop_that_still_exists_are_closed() -> None:
    engine = _Engine(_Pool(idle=1))
    loop = asyncio.new_event_loop()
    built_engines._built[engine] = loop
    try:
        assert built_engines.stranded() == 0
    finally:
        loop.close()
    assert engine.disposed == 1


def test_an_engine_on_another_loop_that_still_exists_is_closed_there() -> None:
    engine = _Engine(_Pool(idle=1))
    loop = asyncio.new_event_loop()
    built_engines._built[engine] = loop
    try:
        asyncio.run(built_engines.dispose())
    finally:
        loop.close()

    assert engine.disposed == 1
    assert engine.dropped == 0


def test_a_test_with_a_loop_reports_a_dead_one_too() -> None:
    gone = _Engine(_Pool(idle=3))
    here = _Engine(_Pool(idle=1))
    built_engines._built[gone] = _closed_loop()
    built_engines._built[here] = None

    asyncio.run(built_engines.dispose())

    assert here.disposed == 1
    assert gone.disposed == 0
    assert built_engines.stranded() == 3
