"""What a process may hold must be derived, and said when it will not fit.

The budget comes from the pool an operator sized and what the process holds
beside it. A figure kept by hand is wrong as soon as something is added without
anyone updating it, and a figure that is quietly too low is worse than none: it
reads as headroom that is not there.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

from inqtrix.settings import Settings
from inqtrix.storage import db as db_module
from inqtrix.storage.connection_budget import (
    _looks_pooled,
    report_connection_budget,
)

_URL = "postgresql+asyncpg://u:p@db.invalid:5432/inqtrix"


class _Captured(logging.Handler):
    """Collect records off the application logger, which does not propagate."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _messages(handler: _Captured) -> list[str]:
    return [r.getMessage() for r in handler.records]


@contextlib.contextmanager
def _capturing():
    """Attach a handler AND lower the logger level.

    The application logger runs at WARNING by default, which filters INFO
    records before any handler sees them — so attaching alone captures
    nothing.
    """
    handler = _Captured()
    logger = logging.getLogger("inqtrix")
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_only_pooled_engines_count_toward_the_budget() -> None:
    """A one-off probe holds nothing between operations, so it adds nothing."""
    before_engines, before_total = db_module.pooled_connection_budget()

    db_module.build_engine(_URL, null_pool=True)
    unchanged = db_module.pooled_connection_budget()
    assert unchanged == (before_engines, before_total)

    db_module.build_engine(_URL, pool_size=5, max_overflow=10)
    engines, total = db_module.pooled_connection_budget()
    assert engines == before_engines + 1
    assert total == before_total + 15


def test_budget_follows_the_configured_pool_sizes() -> None:
    """The figure is the operator's own numbers, not a constant."""
    _, before = db_module.pooled_connection_budget()
    db_module.build_engine(_URL, pool_size=2, max_overflow=3)
    _, after = db_module.pooled_connection_budget()
    assert after - before == 5


def test_a_new_pooled_store_is_counted_without_anyone_updating_a_list() -> None:
    """The property that makes the figure trustworthy over time.

    Counting happens where engines are made, so a store added later raises
    the reported budget on its own. A hand-kept enumeration would not.
    """
    _, before = db_module.pooled_connection_budget()
    # Stand-in for "somebody adds another pooled store next month".
    db_module.build_engine(_URL, pool_size=5, max_overflow=10)
    _, after = db_module.pooled_connection_budget()
    assert after > before, (
        "a pooled engine must raise the reported budget by itself; "
        "otherwise the number drifts the moment a store is added"
    )


@pytest.mark.parametrize(
    "url,pooled",
    [
        ("postgresql+asyncpg://u:p@pgbouncer:6432/inqtrix", True),
        ("postgresql+asyncpg://u:p@db:6432/inqtrix", True),
        ("postgresql+asyncpg://u:p@my-pooler.internal:5432/inqtrix", True),
        ("postgresql+asyncpg://u:p@db.invalid:5432/inqtrix", False),
        ("", False),
    ],
)
def test_transaction_pooler_is_recognised(url, pooled) -> None:
    """Behind a pooler the app-side total is not what the server sees."""
    assert _looks_pooled(url) is pooled


@pytest.mark.asyncio
async def test_pooler_urls_skip_the_comparison_and_say_so() -> None:
    """Comparing against max_connections behind a pooler would mislead."""
    with _capturing() as handler:
        await report_connection_budget(
            database_url="postgresql+asyncpg://u:p@pgbouncer:6432/inqtrix",
            process_label="API-Prozess",
            pool_size=15,
            pool_max_overflow=0,
        )

    assert any("transaction pooler" in m for m in _messages(handler))
    assert not any(r.levelno >= logging.WARNING for r in handler.records)


@pytest.mark.asyncio
async def test_unreachable_server_notes_once_and_keeps_going() -> None:
    """Startup must not hinge on the check succeeding."""
    with _capturing() as handler:
        # The host does not resolve, so reading the ceiling fails.
        await report_connection_budget(
            database_url=_URL,
            process_label="API-Prozess",
            pool_size=15,
            pool_max_overflow=0,
        )

    assert any("not checked against max_connections" in m for m in _messages(handler))
    assert not any(r.levelno >= logging.WARNING for r in handler.records)


@pytest.mark.asyncio
async def test_budget_over_the_server_limit_warns(monkeypatch) -> None:
    """The whole point: an unfittable budget must be stated, not inferred."""
    import inqtrix.storage.connection_budget as module

    async def _tiny_ceiling(_url: str) -> int:
        return 10

    monkeypatch.setattr(module, "_server_max_connections", _tiny_ceiling)
    with _capturing() as handler:
        db_module.build_engine(_URL, pool_size=5, max_overflow=10)
        await report_connection_budget(
            database_url=_URL,
            process_label="API-Prozess",
            pool_size=5,
            pool_max_overflow=10,
            extra_connections=4,
            extra_label="Agent-Checkpointer",
        )

    warnings = [r for r in handler.records if r.levelno >= logging.WARNING]
    assert warnings, "an unfittable budget must warn"
    text = warnings[-1].getMessage()
    assert "max_connections=10" in text
    # Naming the knobs is the difference between a warning and a shrug.
    assert "INQTRIX_DATABASE_POOL_SIZE" in text
    assert "additional replica" in text


@pytest.mark.asyncio
async def test_connections_outside_the_engines_are_included(monkeypatch) -> None:
    """A pool on another driver is invisible to any engine count."""
    import inqtrix.storage.connection_budget as module

    async def _ceiling(_url: str) -> int:
        return 1_000_000

    monkeypatch.setattr(module, "_server_max_connections", _ceiling)
    with _capturing() as handler:
        await report_connection_budget(
            database_url=_URL,
            process_label="API-Prozess",
            pool_size=5,
            pool_max_overflow=10,
            extra_connections=4,
            extra_label="Agent-Checkpointer",
        )
    reported = [m for m in _messages(handler) if "connection budget |" in m]
    assert reported
    assert "Agent-Checkpointer" in reported[-1]


def test_checkpointer_reports_the_pool_it_actually_opens() -> None:
    """The handle's declared size must be the size its pool is built with."""
    import inspect

    from inqtrix.agents.checkpointing import CheckpointerHandle

    source = inspect.getsource(CheckpointerHandle)
    assert "max_size=self.max_connections" in source, (
        "the declared figure and the pool must come from one value, or the "
        "budget can report a number the pool does not honour"
    )
    # Since INQTRIX_AGENT_CHECKPOINTER_POOL_SIZE the figure is an
    # instance attribute fed from settings -- the budget reads the same
    # value the pool is built with.
    handle = CheckpointerHandle(database_url=None, max_connections=3)
    assert handle.max_connections == 3


def test_both_processes_wire_the_checkpointer_into_their_budget_line() -> None:
    """API and worker each pass the handle to report_connection_budget.

    Source pin in this file's own idiom: reverting either call site's
    extra_connections hunk would otherwise fail no test, and the budget
    line would silently drop a pool no engine count can see.
    """
    from pathlib import Path

    import inqtrix.server.app as app_module
    import inqtrix.worker.__main__ as worker_module

    for module in (app_module, worker_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "extra_connections=(" in source, module.__name__
        assert ".max_connections" in source.split("extra_connections=(", 1)[1][:220], (
            f"{module.__name__} must feed the checkpointer's ceiling into "
            "its budget line"
        )


@pytest.mark.asyncio
async def test_overflow_is_named_because_it_keeps_reconnecting() -> None:
    with _capturing() as handler:
        await report_connection_budget(
            database_url="postgresql+asyncpg://u:p@pgbouncer:6432/inqtrix",
            process_label="API process",
            pool_size=5,
            pool_max_overflow=10,
        )

    warnings = [r.getMessage() for r in handler.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "INQTRIX_DATABASE_POOL_MAX_OVERFLOW=10" in warnings[0]
    assert "keeps reconnecting" in warnings[0]


@pytest.mark.parametrize(
    ("slots", "warned"),
    [(14, False), (15, True), (16, True)],
)
@pytest.mark.asyncio
async def test_jobs_that_can_fill_the_pool_during_a_vector_write_are_named(
    slots: int, warned: bool
) -> None:
    with _capturing() as handler:
        await report_connection_budget(
            database_url="postgresql+asyncpg://u:p@pgbouncer:6432/inqtrix",
            process_label="worker process",
            pool_size=15,
            pool_max_overflow=0,
            vector_write_slots=slots,
        )

    warnings = [r.getMessage() for r in handler.records if r.levelno >= logging.WARNING]
    assert bool(warnings) is warned
    if warned:
        assert f"= {slots} jobs" in warnings[0]
        assert "INQTRIX_REINDEX_MAX_CONCURRENT" in warnings[0]
        assert "INQTRIX_DATABASE_POOL_SIZE" in warnings[0]


def test_the_default_job_limits_leave_room_in_the_default_pool() -> None:
    settings = Settings()

    assert (
        settings.knowledge.reindex_max_concurrent
        + settings.server.deletion_max_concurrent
    ) < settings.storage.pool_size + settings.storage.pool_max_overflow


