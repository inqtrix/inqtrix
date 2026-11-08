"""Stores share the engine of their process, and none of them owns it."""

from __future__ import annotations

import json
import logging
import math
import os

import pytest
import sqlalchemy as sa

from inqtrix.project.chat_postgres import PostgresChatStore
from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.storage.db import (
    _dumps_json_column,
    build_engine,
    build_process_engine,
    build_session_factory,
)
from inqtrix.settings import StorageSettings

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
APP_ROLE = ""


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_closing_a_store_leaves_the_engine_to_the_others() -> None:
    engine = build_engine(TEST_DATABASE_URL, pool_size=1, max_overflow=0)
    closing = PostgresDeletionOperationStore(
        engine=engine,
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=1,
        completed_ttl_seconds=60,
        recover_orphans=False,
        worker_id="closes-first",
    )
    other = build_session_factory(engine)
    async with other() as session:
        await session.execute(sa.text("SELECT 1"))
    pool_in_use = engine.pool

    await closing.aclose()

    assert engine.pool is pool_in_use
    assert engine.pool.checkedin() == 1
    async with other() as session:
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_many_first_connections_at_once_do_not_wait_for_each_other() -> None:
    """One loop, one engine: the library's own first-connect lock is enough."""
    import asyncio

    engine = build_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=0)
    factory = build_session_factory(engine)

    async def one() -> int:
        async with factory() as session:
            return (await session.execute(sa.text("SELECT 1"))).scalar_one()

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(one() for _ in range(20))), timeout=20
        )
    finally:
        await engine.dispose()
    assert results == [1] * 20


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_project_store_sits_on_the_engine_it_is_given() -> None:
    engine = build_engine(TEST_DATABASE_URL)
    store = PostgresChatStore(engine=engine, app_role=APP_ROLE)

    assert store._session_factory.kw["bind"] is engine
    assert not hasattr(store, "aclose")


def test_the_one_builder_reads_its_sizes_from_the_settings() -> None:
    engine = build_process_engine(
        StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
            INQTRIX_DATABASE_POOL_SIZE=3,
            INQTRIX_DATABASE_POOL_MAX_OVERFLOW=1,
            INQTRIX_DATABASE_POOL_TIMEOUT_SECONDS=7,
        )
    )
    try:
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 1
        assert engine.pool._timeout == 7
    finally:
        engine.sync_engine.dispose()


def test_the_one_builder_has_nothing_to_build_without_postgres() -> None:
    assert build_process_engine(StorageSettings(backend="memory")) is None


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1.5, "b": [1, 2, {"c": None}], "d": "ä", "e": True},
        [],
        {},
        "text",
        1,
        None,
        {"nested": {"deep": [0.1, 1e300, -2.5]}},
    ],
)
def test_finite_values_stay_byte_identical(value) -> None:
    """Event payloads are replayed verbatim; the format must not move."""
    assert _dumps_json_column(value) == json.dumps(value)


def test_numbers_that_are_not_finite_become_null_and_are_counted(caplog) -> None:
    value = {
        "private key": math.nan,
        "list": [math.inf, 1.0, {"other private key": -math.inf}],
        "tuple": (math.nan,),
        "kept": 2.5,
    }

    with caplog.at_level(logging.WARNING, logger="inqtrix.storage.db"):
        text = _dumps_json_column(value)

    assert json.loads(text) == {
        "private key": None,
        "list": [None, 1.0, {"other private key": None}],
        "tuple": [None],
        "kept": 2.5,
    }
    message = caplog.messages[-1]
    assert "4 numbers" in message
    assert "private key" not in message


def test_a_key_stays_what_it_was_and_is_not_reported_as_fixed(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.storage.db"):
        text = _dumps_json_column({math.nan: "a key has no null"})

    assert text == json.dumps({math.nan: "a key has no null"})
    assert caplog.messages == []


def test_a_write_that_still_fails_is_not_reported_as_fixed(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.storage.db"):
        with pytest.raises(TypeError):
            _dumps_json_column({"score": math.nan, "not json": object()})

    assert caplog.messages == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_write_with_nan_reaches_postgres_as_null() -> None:
    engine = build_engine(TEST_DATABASE_URL)
    table = sa.Table(
        "json_column_probe",
        sa.MetaData(),
        sa.Column("value", sa.JSON),
        prefixes=["TEMPORARY"],
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(table.create)
            await connection.execute(table.insert().values(value={"score": math.nan}))
            stored = (await connection.execute(sa.select(table.c.value))).scalar_one()
    finally:
        await engine.dispose()

    assert stored == {"score": None}
