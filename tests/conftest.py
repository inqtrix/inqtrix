""" Shared test fixtures for inqtrix unit tests."""

from __future__ import annotations

import os

# A dependency that loads a ``.env`` when it is imported (magika does) must
# not reach the tests, wherever the checkout lies. python-dotenv honours this
# switch in every ``load_dotenv`` call; set before anything imports one.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import asyncio
import gc
import logging
import sys
import threading
import warnings
from typing import Any

import pytest
import pytest_asyncio

from inqtrix import aio
from inqtrix import settings as settings_module
from inqtrix.observability import context as observability_context
from inqtrix.runs.deletion_operations import DeletionOperationStore
from inqtrix.runs.indexing_memory import IndexingJobStore
from inqtrix.runs.memory_store import RunStore
from inqtrix.runs.upload_operations import MemoryUploadOperationStore
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import Settings
from inqtrix.strategies import (
    DefaultClaimConsolidator,
    DefaultSourceTiering,
    KeywordRiskScorer,
)
from tests import _built_engines as built_engines
from tests import _connection_guard as connection_guard
from tests import _store_guard as store_guard
from tests._loop_guard import watch_for_blocked_loops
from tests._hermetic import (
    hide_machine_aws_setup,
    restore_loggers,
    shield_settings_from_dotenv,
    snapshot_loggers,
)

# The settings read a ``.env`` in the working folder; some are built while
# the tests are collected, so they ignore it from here on. Nor does the
# machine's AWS setup reach a test: an AWS client is built the same way
# everywhere and can never reach an account.
shield_settings_from_dotenv()
hide_machine_aws_setup()
connection_guard.install()
built_engines.install()


# Threads start on demand; tests hold more jobs at once than a small lane has room for.
TEST_LANE_WORKERS = 64

INTEGRATION_MARKERS = {
    "postgres": "INQTRIX_TEST_DATABASE_URL",
    "qdrant": "INQTRIX_TEST_QDRANT_URL",
    "valkey": "INQTRIX_TEST_VALKEY_URL",
}

# Files whose event loops run under the guard: the tests that exercise the
# process loop itself and the code that shares it with every run and request.
LOOP_GUARDED_FILES = frozenset(
    {
        "tests/runs/test_no_loop_per_call.py",
        "tests/test_aio.py",
        "tests/test_routes.py",
        "tests/test_worker_loop.py",
        "tests/worker/test_async_claim.py",
    }
)
LOOP_GUARDED_PREFIXES = ("tests/agents/test_kernel_",)


def _is_loop_guarded(item: pytest.Item) -> bool:
    if item.get_closest_marker("blocks_the_loop_on_purpose"):
        return False
    path = item.path.relative_to(item.config.rootpath).as_posix()
    return path in LOOP_GUARDED_FILES or path.startswith(LOOP_GUARDED_PREFIXES)


def pytest_collection_finish(session):
    """Keep what importing the code built out of every later collection.

    The loop guard collects before each test it covers, and a full pass over all
    imported modules would cost that test a fifth of a second.
    """
    del session
    gc.collect()
    gc.freeze()


def pytest_collection_modifyitems(items):
    """Gate the infrastructure-bound suites on the URL that they need.

    Without the URL the persistence, isolation and migration proofs cannot
    run, so they are skipped. With ``INQTRIX_TEST_REQUIRE_INTEGRATION=1`` the
    same situation is an error instead: whoever asks for the release run must
    not be able to obtain a green result while those proofs are absent.

    An async test runs on a configured loop, as everything in a process does,
    and that loop outlasts the test's other fixtures, whose teardown may use it.
    The loop guard goes before it, so it sees every loop the test goes on to
    build.
    """
    for item in items:
        if item.get_closest_marker("asyncio"):
            item.fixturenames.insert(0, "configured_test_loop")
        if _is_loop_guarded(item):
            item.fixturenames.insert(0, "loop_guard")
        # Last to end: what is left after the test's own loop has closed.
        item.fixturenames.remove("no_connection_left_open")
        item.fixturenames.insert(0, "no_connection_left_open")
    require_integration = os.environ.get("INQTRIX_TEST_REQUIRE_INTEGRATION") == "1"
    for marker, variable in INTEGRATION_MARKERS.items():
        if os.environ.get(variable):
            continue
        gated = [item for item in items if item.get_closest_marker(marker)]
        if not gated:
            continue
        if require_integration:
            raise pytest.UsageError(
                f"INQTRIX_TEST_REQUIRE_INTEGRATION=1 requires {variable}: "
                f"{len(gated)} {marker} tests would be skipped."
            )
        skip = pytest.mark.skip(reason=f"{variable} not set ({marker} integration)")
        for item in gated:
            item.add_marker(skip)


_refused_bridges: list[str] = []
# The strict loop lookup the session replaces with its loan (``loops_borrow_nothing``).
_strict_loop_state: list[Any] = []


@pytest.fixture(scope="session", autouse=True)
def process_loop_for_sync_code():
    """The loop that sync code under test reaches through ``aio.call_from_thread``.

    A process owner configures one loop before anything runs on threads; a test
    that drives such code directly needs the same. Whatever configures a loop of
    its own (a lifespan, an async test) takes over until it closes that loop.

    ``asyncio.run`` and a ``TestClient`` without ``with`` open loops nobody
    configures; they borrow this loop's lanes. Every refused bridge is recorded,
    because the callers on the way up may swallow the exception.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-process-loop", daemon=True)
    thread.start()
    aio.configure_loop(
        loop,
        blocking_workers=TEST_LANE_WORKERS,
        provider_workers=TEST_LANE_WORKERS,
        legacy_segment_workers=TEST_LANE_WORKERS,
        watch=False,
    )
    strict_state = aio._state_of_running_loop
    strict_refusal = aio._refusal
    _strict_loop_state.append(strict_state)

    def state_or_borrowed() -> Any:
        try:
            return strict_state()
        except aio.LoopNotConfigured:
            state = aio._state_of(None)
            if state is None:
                raise
            return state

    def recorded_refusal(error: RuntimeError) -> RuntimeError:
        _refused_bridges.append(str(error))
        return strict_refusal(error)

    aio._state_of_running_loop = state_or_borrowed
    aio._refusal = recorded_refusal
    store_guard.install(
        (RunStore, IndexingJobStore, DeletionOperationStore, MemoryUploadOperationStore)
    )
    yield loop
    aio._state_of_running_loop = strict_state
    aio._refusal = strict_refusal
    aio.close_loop(loop)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    if not thread.is_alive():
        loop.close()


@pytest.fixture()
def loops_borrow_nothing(monkeypatch):
    """A loop nobody configured borrows no lanes, as in a process of its own.

    The session lends its loop's lanes to every loop, which hides code that
    needs a configured loop but runs on a caller's own loop.
    """
    monkeypatch.setattr(aio, "_state_of_running_loop", _strict_loop_state[0])


@pytest.fixture(autouse=True)
def context_ends_with_the_test():
    """In a process a job body has a context of its own; a test calling one shares the test's."""
    yield
    for field in observability_context._FIELDS.values():
        field.set("")
    observability_context._FEATURE.set("")
    observability_context.clear_usage_subject()


@pytest.fixture(autouse=True)
def no_refused_bridge(request):
    """A refused bridge fails its test even when a catch-all swallowed the exception."""
    _refused_bridges.clear()
    yield
    if _refused_bridges and not request.node.get_closest_marker("refuses_the_bridge_on_purpose"):
        pytest.fail(
            f"{len(_refused_bridges)} bridges refused: {_refused_bridges[0]}", pytrace=False
        )


@pytest.fixture(autouse=True)
def no_connection_left_open():
    """Connections of a loop that is gone cannot be closed, and block migrations."""
    yield
    stranded = built_engines.stranded()
    if stranded:
        pytest.fail(f"{stranded} database connections outlived their loop", pytrace=False)


@pytest.fixture(autouse=True)
def one_connection_per_task(request):
    """A task that opens a second database connection fails its test."""
    connection_guard.reset()
    yield
    found = connection_guard.found()
    if found and not request.node.get_closest_marker("two_connections_on_purpose"):
        pytest.fail(f"{len(found)} tasks held two connections: {found[0]}", pytrace=False)


@pytest_asyncio.fixture
async def configured_test_loop(process_loop_for_sync_code):
    del process_loop_for_sync_code
    loop = asyncio.get_running_loop()
    aio.configure_loop(
        loop,
        blocking_workers=TEST_LANE_WORKERS,
        provider_workers=TEST_LANE_WORKERS,
        legacy_segment_workers=TEST_LANE_WORKERS,
        watch=False,
    )
    try:
        yield
    finally:
        await built_engines.dispose()
        aio.close_loop(loop)


@pytest.fixture
def loop_guard(monkeypatch, process_loop_for_sync_code):
    """Fail a test when one step of any of its event loops blocks that loop."""
    del process_loop_for_sync_code
    with watch_for_blocked_loops(monkeypatch) as slow:
        yield
    if slow.messages:
        pytest.fail(
            f"{len(slow.messages)} steps blocked an event loop: " + "; ".join(slow.messages[:5]),
            pytrace=False,
        )


class _StubLLM:
    """Minimal stub satisfying ProviderContext.llm for strategy tests."""

    def complete(self, *a, **kw):
        return ""


    def is_available(self):
        return False


class _StubSearch:
    """Minimal stub satisfying ProviderContext.search for strategy tests."""

    def search(self, *a, **kw):
        return GroundedSearchResult()

    def is_available(self):
        return False


_PROCESS_LOGGERS = (
    "inqtrix",
    "inqtrix.web_gateway",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "alembic",
    "alembic.runtime.migration",
    "httpx",
    "httpcore",
    "websockets",
    "openai",
    "asyncio",
    "py.warnings",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.sdk.trace.export",
)
# These switch on the debug logging of the OpenAI and Anthropic SDKs.
_SDK_DEBUG_SWITCHES = frozenset({"OPENAI_LOG", "ANTHROPIC_LOG"})


@pytest.fixture(autouse=True)
def reset_inqtrix_logger(monkeypatch):
    """Isolate the process logging state around every test.

    ``configure_logging`` configures the root logger, the product and web
    loggers and the warning and exception hooks of the whole process. Without
    a reset that state leaks into whatever test runs next. Every test starts
    with a neutral, root-capturable ``inqtrix`` logger, and the previous state
    is restored afterwards. Every other logger of the ``inqtrix.`` tree loses
    the level, handlers and switches the test gave it, so a level set for one
    area ends with the test.

    The developer's logging configuration never reaches a test: no
    ``INQTRIX_LOG_*``, retired variable or SDK debug switch from the process
    environment, and no ``.env`` file (kept out of the session at the top of
    this module). A service ``main()`` under test therefore starts the same on
    every machine.
    """
    retired_names = {entry.name for entry in settings_module.RETIRED_SETTINGS}
    for name in list(os.environ):
        if name.startswith("INQTRIX_LOG_") or name in retired_names | _SDK_DEBUG_SWITCHES:
            monkeypatch.delenv(name, raising=False)
    root = logging.getLogger()
    root_state = (list(root.handlers), root.level)
    logger_state = snapshot_loggers(_PROCESS_LOGGERS)
    hooks = (sys.excepthook, threading.excepthook, warnings.showwarning)

    inqtrix_logger = logging.getLogger("inqtrix")
    for handler in list(inqtrix_logger.handlers):
        inqtrix_logger.removeHandler(handler)
    inqtrix_logger.setLevel(logging.NOTSET)
    inqtrix_logger.propagate = True

    yield

    for handler in list(root.handlers):
        if handler not in root_state[0]:
            root.removeHandler(handler)
            handler.close()
    for handler in root_state[0]:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(root_state[1])
    restore_loggers(logger_state)
    logging.captureWarnings(False)
    sys.excepthook, threading.excepthook, warnings.showwarning = hooks


@pytest.fixture()
def recording(monkeypatch):
    """Record every span opened through ``inqtrix.observability.otel``.

    The module's tracer handle is swapped for a proxy bound to a test-local
    provider, so nothing is installed globally and suites stay isolated.
    Yields the in-memory exporter.
    """
    from inqtrix.observability import otel as otel_module
    from tests._tracing_helpers import ProxyTrace, in_memory_provider

    provider, exporter = in_memory_provider()
    monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    yield exporter
    provider.shutdown()


@pytest.fixture
def settings():
    """A default Settings instance."""
    return Settings()


@pytest.fixture
def tiering():
    """A DefaultSourceTiering strategy."""
    return DefaultSourceTiering()


@pytest.fixture
def consolidator(tiering):
    """A DefaultClaimConsolidator strategy."""
    return DefaultClaimConsolidator(source_tiering=tiering)


@pytest.fixture
def risk_scorer():
    """A KeywordRiskScorer strategy."""
    return KeywordRiskScorer()
