"""What a process may hold must be derived, and said when it will not fit.

The budget is the product of a pool size an operator chose and an engine
count they cannot see. A count kept by hand beside the engines is wrong as
soon as a store is added without anyone updating it, and a figure that is
quietly too low is worse than none: it reads as headroom that is not there.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

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
    """A NullPool store holds nothing between operations, so it adds nothing."""
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
            pool_size=5,
            pool_max_overflow=10,
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
            pool_size=5,
            pool_max_overflow=10,
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
async def test_the_run_lane_peak_counts_toward_the_comparison(monkeypatch) -> None:
    """The largest consumer the raised run cap creates must not be invisible.

    Run threads drive a NullPool bundle, so they hold nothing at rest and no
    pool count can see them. That is exactly why the comparison has to be
    told about them: a synchronised burst can ask for one connection per
    in-flight run, and a check that omits the term reports a budget that
    fits while the server refuses connections.
    """
    import inqtrix.storage.connection_budget as module

    async def _ceiling(_url: str) -> int:
        return 100

    # The pooled figure is a process-wide accumulator that every earlier test
    # building an engine adds to, so it is pinned here rather than inherited:
    # otherwise this test passes or fails by suite order, not by behaviour.
    monkeypatch.setattr(module, "_server_max_connections", _ceiling)
    # Patched at its source: report_connection_budget imports it inside the
    # function, so patching the importing module would miss it.
    monkeypatch.setattr(
        db_module, "pooled_connection_budget", lambda: (4, 60)
    )

    with _capturing() as quiet:
        await report_connection_budget(
            database_url=_URL,
            process_label="API-Prozess",
            pool_size=5,
            pool_max_overflow=10,
        )
    pooled_only = [r for r in quiet.records if r.levelno >= logging.WARNING]
    assert not pooled_only, (
        "60 pooled against max_connections=100 must fit; the run lane is "
        "what has to tip it over"
    )

    with _capturing() as loud:
        await report_connection_budget(
            database_url=_URL,
            process_label="API-Prozess",
            pool_size=5,
            pool_max_overflow=10,
            transient_peak=100,
            transient_label="Lauf-Threads, NullPool",
            transient_knob="RUN_MAX_CONCURRENT",
        )
    with_run_lane = [r for r in loud.records if r.levelno >= logging.WARNING]

    assert len(with_run_lane) > len(pooled_only), (
        "adding the run lane must be able to turn a budget that appeared to "
        "fit into one that does not; otherwise the term is decorative"
    )
    assert "RUN_MAX_CONCURRENT" in with_run_lane[-1].getMessage(), (
        "the warning must name the knob that bounds the transient peak"
    )
    reported = [m for m in _messages(loud) if "connection budget |" in m]
    assert "short-lived" in reported[-1]
    assert "Lauf-Threads" in reported[-1]


@pytest.mark.asyncio
async def test_the_named_remedy_follows_the_caller(monkeypatch) -> None:
    """Which knob bounds the transient peak differs per process.

    The API admits runs and is bounded by RUN_MAX_CONCURRENT; a worker
    executes already-admitted runs, where that variable never fires and
    INQTRIX_WORKER_CONCURRENCY is the width. A hardcoded remedy sends half
    the operators to a setting that does nothing where they read it.
    """
    import inqtrix.storage.connection_budget as module

    async def _ceiling(_url: str) -> int:
        return 10

    monkeypatch.setattr(module, "_server_max_connections", _ceiling)
    monkeypatch.setattr(db_module, "pooled_connection_budget", lambda: (4, 60))

    with _capturing() as handler:
        await report_connection_budget(
            database_url=_URL,
            process_label="Worker-Prozess",
            pool_size=5,
            pool_max_overflow=10,
            transient_peak=114,
            transient_label="Worker-Schleifen, NullPool",
            transient_knob="INQTRIX_WORKER_CONCURRENCY",
        )

    warnings = [r for r in handler.records if r.levelno >= logging.WARNING]
    assert warnings
    text = warnings[-1].getMessage()
    assert "INQTRIX_WORKER_CONCURRENCY" in text
    assert "RUN_MAX_CONCURRENT" not in text, (
        "naming the API's knob in a worker warning points at a setting that "
        "does not bound anything there"
    )
