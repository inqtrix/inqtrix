"""Pooled-engine sizing: settings reach the engine, NullPool ignores them.

Guards the 1000-user connection-budget contract: an operator setting
``INQTRIX_DATABASE_POOL_SIZE``/``..._MAX_OVERFLOW`` must actually change
the pool the engine builds (a silently ignored knob would let a
deployment believe it capped its budget when it did not).
"""

from __future__ import annotations

from sqlalchemy.pool import NullPool

from inqtrix.settings import StorageSettings
from inqtrix.storage.db import build_engine

_URL = "postgresql+asyncpg://user:pw@127.0.0.1:5432/db"


def test_pool_kwargs_reach_the_engine() -> None:
    engine = build_engine(_URL, pool_size=2, max_overflow=3, pool_timeout=7.0)
    try:
        pool = engine.pool
        assert pool.size() == 2
        assert pool._max_overflow == 3
        assert pool._timeout == 7.0
    finally:
        engine.sync_engine.dispose()


def test_defaults_stay_byte_identical_to_sqlalchemy() -> None:
    engine = build_engine(_URL)
    try:
        assert engine.pool.size() == 5
        assert engine.pool._max_overflow == 10
    finally:
        engine.sync_engine.dispose()


def test_null_pool_branch_ignores_sizing() -> None:
    engine = build_engine(_URL, null_pool=True, pool_size=99)
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        engine.sync_engine.dispose()


def test_storage_settings_bundle_matches_engine_kwargs() -> None:
    settings = StorageSettings(
        INQTRIX_DATABASE_POOL_SIZE=3,
        INQTRIX_DATABASE_POOL_MAX_OVERFLOW=4,
        INQTRIX_DATABASE_POOL_TIMEOUT_SECONDS=11.0,
    )
    engine = build_engine(_URL, **settings.pool_kwargs())
    try:
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 4
        assert engine.pool._timeout == 11.0
    finally:
        engine.sync_engine.dispose()


def test_command_timeout_travels_through_the_settings_bundle() -> None:
    settings = StorageSettings(
        INQTRIX_DATABASE_COMMAND_TIMEOUT_SECONDS=42.0,
    )
    assert settings.pool_kwargs()["command_timeout"] == 42.0
    engine = build_engine(_URL, **settings.pool_kwargs())
    try:
        assert isinstance(engine.pool.size(), int)
    finally:
        engine.sync_engine.dispose()


def test_command_timeout_zero_disables_the_ceiling() -> None:
    settings = StorageSettings(
        INQTRIX_DATABASE_COMMAND_TIMEOUT_SECONDS=0,
    )
    assert settings.pool_kwargs()["command_timeout"] is None
    engine = build_engine(_URL, **settings.pool_kwargs())
    try:
        assert isinstance(engine.pool.size(), int)
    finally:
        engine.sync_engine.dispose()


def test_null_pool_branch_accepts_the_ceiling() -> None:
    engine = build_engine(_URL, null_pool=True, command_timeout=42.0)
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        engine.sync_engine.dispose()
