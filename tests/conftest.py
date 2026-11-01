""" Shared test fixtures for inqtrix unit tests."""

from __future__ import annotations

import os

# A dependency that loads a ``.env`` when it is imported (magika does) must
# not reach the tests, wherever the checkout lies. python-dotenv honours this
# switch in every ``load_dotenv`` call; set before anything imports one.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import logging
import sys
import threading
import warnings

import pytest

from inqtrix import settings as settings_module
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import Settings
from inqtrix.strategies import (
    DefaultClaimConsolidator,
    DefaultSourceTiering,
    KeywordRiskScorer,
)
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
