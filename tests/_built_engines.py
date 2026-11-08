"""What a test builds goes with the test.

A store shares the engine of its process and leaves it to whoever built it, which
in a test is the test. Connections cannot be closed once their event loop is gone,
so a test that runs its steps on loops of their own disposes within each step.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import event

from inqtrix.storage import db

_built: dict[Any, asyncio.AbstractEventLoop | None] = {}
_stranded: list[int] = []


def install() -> None:
    real = db.create_async_engine

    def recording(*args: Any, **kwargs: Any) -> Any:
        engine = real(*args, **kwargs)
        _built[engine] = None

        def note_the_loop(*_: Any) -> None:
            try:
                _built[engine] = asyncio.get_running_loop()
            except RuntimeError:
                pass

        event.listen(engine.sync_engine, "checkout", note_the_loop)
        return engine

    db.create_async_engine = recording


def _in_use_or_idle(engine: Any) -> int:
    pool = engine.pool
    return getattr(pool, "checkedin", int)() + getattr(pool, "checkedout", int)()


async def dispose() -> None:
    """At the end of a test with a loop: each engine on the loop it used."""
    current = asyncio.get_running_loop()
    while _built:
        engine, loop = _built.popitem()
        if loop is None or loop is current:
            await engine.dispose()
        elif loop.is_running():
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(engine.dispose(), loop)
            )
        elif loop.is_closed():
            _stranded.append(_in_use_or_idle(engine))
            engine.sync_engine.dispose(close=False)
        else:
            await asyncio.to_thread(loop.run_until_complete, engine.dispose())


def stranded() -> int:
    """At the end of a test: how many connections outlived their loop."""
    count = sum(_stranded)
    _stranded.clear()
    while _built:
        engine, loop = _built.popitem()
        if not _in_use_or_idle(engine):
            continue
        if loop is None or loop.is_closed():
            count += _in_use_or_idle(engine)
            engine.sync_engine.dispose(close=False)
        elif loop.is_running():
            asyncio.run_coroutine_threadsafe(engine.dispose(), loop).result(10)
        else:
            loop.run_until_complete(engine.dispose())
    return count
