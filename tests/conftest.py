""" Shared test fixtures for inqtrix unit tests."""

from __future__ import annotations

import logging
import os

import pytest

from inqtrix.logging_config import _WEB_LOGGER_NAMES
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import Settings
from inqtrix.strategies import (
    DefaultClaimConsolidator,
    DefaultSourceTiering,
    KeywordRiskScorer,
)


INTEGRATION_MARKERS = {
    "postgres": "INQTRIX_TEST_DATABASE_URL",
    "qdrant": "INQTRIX_TEST_QDRANT_URL",
}


def pytest_collection_modifyitems(items):
    """Gate the infrastructure-bound suites on the URL that they need.

    Without the URL the persistence, isolation and migration proofs cannot
    run, so they are skipped. With ``INQTRIX_TEST_REQUIRE_INTEGRATION=1`` the
    same situation is an error instead: whoever asks for the release run must
    not be able to obtain a green result while those proofs are absent.
    """
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


@pytest.fixture(autouse=True)
def reset_inqtrix_logger():
    """Isolate inqtrix logger state around every test.

    ``create_app`` (and the example/server tests that build it) calls
    ``configure_logging``, which sets ``propagate=False`` and attaches
    handlers on ``logging.getLogger("inqtrix")``. Without a reset that leaks
    into whatever test runs next; in default (alphabetical) collection order
    a ``create_app`` test can precede a ``caplog``-based test and silently
    break it because ``caplog`` relies on propagation to the root logger.
    Restoring handlers/level/propagate here keeps the leak from crossing test
    boundaries (Gotcha #1 / Test-Order-Hygiene). Web-server loggers that the
    uvicorn ``dictConfig`` tests reconfigure are restored for the same reason.
    """
    inqtrix_logger = logging.getLogger("inqtrix")
    previous_handlers = list(inqtrix_logger.handlers)
    previous_level = inqtrix_logger.level
    previous_propagate = inqtrix_logger.propagate

    web_state = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _WEB_LOGGER_NAMES
    }

    for handler in list(inqtrix_logger.handlers):
        inqtrix_logger.removeHandler(handler)
    # Collection can import ``inqtrix.__main__`` before the first fixture is
    # entered. That module configures the product logger with
    # ``propagate=False``. Merely removing its handlers therefore still makes
    # caplog-based tests silent for the whole run. Give every test a neutral,
    # root-capturable logger and restore the collection-time state afterwards.
    inqtrix_logger.setLevel(logging.NOTSET)
    inqtrix_logger.propagate = True

    yield

    for handler in list(inqtrix_logger.handlers):
        inqtrix_logger.removeHandler(handler)
        handler.close()

    inqtrix_logger.setLevel(previous_level)
    inqtrix_logger.propagate = previous_propagate
    for handler in previous_handlers:
        inqtrix_logger.addHandler(handler)

    for name, (handlers, level, propagate) in web_state.items():
        web_logger = logging.getLogger(name)
        for handler in list(web_logger.handlers):
            web_logger.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
        for handler in handlers:
            web_logger.addHandler(handler)
        web_logger.setLevel(level)
        web_logger.propagate = propagate


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
