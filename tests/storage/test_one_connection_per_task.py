"""The guard against a task waiting for a second database connection sees it."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from inqtrix.storage.db import build_engine, build_session_factory
from tests import _connection_guard as connection_guard

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")


async def _one_statement(factory) -> None:
    async with factory() as session:
        await session.execute(text("SELECT 1"))


@pytest.mark.two_connections_on_purpose
async def test_a_second_session_inside_a_first_is_found() -> None:
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            await _one_statement(factory)
    finally:
        await engine.dispose()

    found = connection_guard.found()
    assert len(found) == 1
    assert "second connection at tests/storage/test_one_connection_per_task.py" in found[0]


@pytest.mark.two_connections_on_purpose
async def test_a_second_engine_hides_nothing() -> None:
    first = build_engine(TEST_DATABASE_URL)
    second = build_engine(TEST_DATABASE_URL)
    try:
        async with build_session_factory(first)() as session:
            await session.execute(text("SELECT 1"))
            await _one_statement(build_session_factory(second))
    finally:
        await first.dispose()
        await second.dispose()

    assert len(connection_guard.found()) == 1


async def test_sessions_one_after_the_other_and_side_by_side_are_fine() -> None:
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    try:
        await _one_statement(factory)
        await _one_statement(factory)
        await asyncio.gather(*(_one_statement(factory) for _ in range(5)))
    finally:
        await engine.dispose()

    assert connection_guard.found() == []
