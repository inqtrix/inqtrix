"""When the pool checks a connection, and what a dead connection does to a caller."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from inqtrix.storage import db
from inqtrix.storage.db import build_engine

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

_SHORT_WINDOW = 0.05
_ROOMY_WINDOW = 0.5
_LONG_WINDOW = 60.0


@pytest_asyncio.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = build_engine(TEST_DATABASE_URL, pool_size=1, pool_timeout=5)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def pool_of_two() -> AsyncIterator[AsyncEngine]:
    engine = build_engine(TEST_DATABASE_URL, pool_size=2, pool_timeout=5)
    yield engine
    await engine.dispose()


@pytest.fixture()
def checks(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every check the pool runs, whichever way it is asked for."""
    dialect = engine.sync_engine.dialect
    do_ping = dialect.do_ping
    seen: list[object] = []

    def counting(dbapi_connection: object) -> bool:
        seen.append(dbapi_connection)
        return do_ping(dbapi_connection)

    monkeypatch.setattr(dialect, "do_ping", counting)
    return seen


async def _backend_of(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT pg_backend_pid()"))


async def _backends_of_both(engine: AsyncEngine) -> tuple[int, int]:
    """Take both connections of a pool of two; the first back is the first out again."""
    second_out = asyncio.Event()
    first_back = asyncio.Event()

    async def first() -> int:
        async with engine.connect() as connection:
            backend = await connection.scalar(text("SELECT pg_backend_pid()"))
            await second_out.wait()
        first_back.set()
        return backend

    async def second() -> int:
        async with engine.connect() as connection:
            backend = await connection.scalar(text("SELECT pg_backend_pid()"))
            second_out.set()
            await first_back.wait()
        return backend

    return tuple(await asyncio.gather(first(), second()))


async def _kill(backend: int) -> None:
    killer = build_engine(TEST_DATABASE_URL, null_pool=True)
    try:
        async with killer.connect() as connection:
            assert await connection.scalar(
                text("SELECT pg_terminate_backend(:backend)"), {"backend": backend}
            )
    finally:
        await killer.dispose()


@pytest.mark.asyncio
async def test_a_connection_in_steady_use_is_not_checked(
    engine: AsyncEngine, checks: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _LONG_WINDOW)
    backends = {await _backend_of(engine) for _ in range(5)}

    assert len(backends) == 1
    assert checks == []


@pytest.mark.asyncio
async def test_a_connection_left_idle_is_checked_once_per_checkout(
    engine: AsyncEngine, checks: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _ROOMY_WINDOW)
    first = await _backend_of(engine)
    assert checks == []

    await asyncio.sleep(_ROOMY_WINDOW * 1.5)
    assert await _backend_of(engine) == first
    assert len(checks) == 1

    # Back in use, so the window counts from this return and not from the first.
    assert await _backend_of(engine) == first
    assert len(checks) == 1


@pytest.mark.asyncio
async def test_a_connection_that_died_while_idle_never_reaches_the_caller(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _SHORT_WINDOW)
    first = await _backend_of(engine)
    await _kill(first)
    await asyncio.sleep(_SHORT_WINDOW * 2)

    assert await _backend_of(engine) != first


@pytest.mark.asyncio
async def test_a_failed_check_replaces_every_older_connection(
    pool_of_two: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _SHORT_WINDOW)
    first, second = await _backends_of_both(pool_of_two)
    await _kill(first)
    await asyncio.sleep(_SHORT_WINDOW * 2)

    # One at a time, so the first check has failed before the second connection comes
    # out. That one is alive, and the failed check replaces it all the same.
    assert await _backend_of(pool_of_two) not in {first, second}
    assert await _backend_of(pool_of_two) not in {first, second}


@pytest.mark.asyncio
async def test_a_check_that_fails_for_another_reason_reaches_the_caller(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _SHORT_WINDOW)
    dialect = engine.sync_engine.dialect
    first = await _backend_of(engine)
    await asyncio.sleep(_SHORT_WINDOW * 2)

    def refuse(dbapi_connection: object) -> bool:
        raise dialect.loaded_dbapi.Error("refused for another reason")

    monkeypatch.setattr(dialect, "do_ping", refuse)
    with pytest.raises(DBAPIError, match="another reason"):
        await _backend_of(engine)

    monkeypatch.undo()
    assert await _backend_of(engine) != first


@pytest.mark.asyncio
async def test_a_connection_that_died_a_moment_ago_fails_one_caller_and_the_pool_heals(
    pool_of_two: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_PING_AFTER_IDLE_SECONDS", _LONG_WINDOW)
    first, second = await _backends_of_both(pool_of_two)
    await _kill(first)
    await _kill(second)

    with pytest.raises(DBAPIError) as failure:
        await _backend_of(pool_of_two)
    assert failure.value.connection_invalidated is True

    assert not set(await _backends_of_both(pool_of_two)) & {first, second}
