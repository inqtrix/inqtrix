"""Each worker loop runs at the LOWER of two ceilings, and says when it does.

``INQTRIX_WORKER_CONCURRENCY`` sizes the research loop, whose threads spend
their time waiting on providers. The other loops spend real CPU — an upload
extracts text from the file itself — so a worker sized for provider
parallelism would run that many extractions at once on a CPU-limited pod.
Deletion already had its own ceiling; indexing and upload now follow the
same rule, and a ceiling that binds is logged rather than left to be
inferred from throughput.
"""

from __future__ import annotations

import ast
import contextlib
import logging
from pathlib import Path

import pytest

import inqtrix.worker.__main__ as worker_main
from inqtrix.settings import Settings


class _Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _messages(handler: _Captured) -> list[str]:
    return [r.getMessage() for r in handler.records]


@contextlib.contextmanager
def _capturing():
    """Attach a handler AND lower the level -- ``inqtrix`` does not propagate."""
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


class _IndexingWorkerLoop:
    """Stand-in: only the class NAME decides what is reported."""


class _UploadWorkerLoop:
    pass


class _DeletionWorkerLoop:
    pass


# The report keys on the real class names, so the stand-ins carry them.
_IndexingWorkerLoop.__name__ = "IndexingWorkerLoop"
_UploadWorkerLoop.__name__ = "UploadWorkerLoop"
_DeletionWorkerLoop.__name__ = "DeletionWorkerLoop"

_ALL_LOOPS = [_IndexingWorkerLoop(), _UploadWorkerLoop(), _DeletionWorkerLoop()]


_MARKER = "Worker workload limits"


def _settings(**overrides) -> Settings:
    settings = Settings()
    settings.queue.worker_concurrency = overrides.get("worker", 12)
    settings.knowledge.reindex_max_concurrent = overrides.get("indexing", 12)
    settings.server.upload_max_concurrent = overrides.get("upload", 12)
    settings.server.deletion_max_concurrent = overrides.get("deletion", 12)
    return settings


def _concurrency_ceilings(constructor: str) -> set[str]:
    """Names inside the ``concurrency=min(...)`` of one loop constructor.

    Anchored on the constructor rather than on the file: the same settings
    name also appears in the store built alongside the loop, so a
    file-wide search would stay green after the loop itself lost its cap.
    """
    tree = ast.parse(Path(worker_main.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == constructor
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "concurrency":
                continue
            value = keyword.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "min"
            ):
                return set()
            return {
                arg.attr
                for arg in value.args
                if isinstance(arg, ast.Attribute)
            }
    raise AssertionError(f"{constructor} is not constructed in the worker")


@pytest.mark.parametrize(
    "constructor,ceiling",
    [
        ("IndexingWorkerLoop", "reindex_max_concurrent"),
        ("UploadWorkerLoop", "upload_max_concurrent"),
        ("DeletionWorkerLoop", "deletion_max_concurrent"),
    ],
)
def test_every_cpu_bound_loop_is_capped_by_its_own_ceiling(
    constructor, ceiling
) -> None:
    """Checked at the call site: ``main()`` needs live Postgres and Valkey.

    Dropping the ``min`` would let one raised value fan out into loops
    wider than any ceiling allows, which is exactly the regression the
    ceilings exist to prevent.
    """
    assert _concurrency_ceilings(constructor) == {
        "worker_concurrency",
        ceiling,
    }, (
        f"{constructor} must run at min(worker_concurrency, {ceiling})"
    )


def test_a_binding_ceiling_is_named_with_its_alias() -> None:
    """Raising only the process-wide value must not look like it did nothing.

    The prose marker is asserted alongside the interpolated values: the two
    silence tests below key on it, so rewording the line has to fail HERE
    rather than quietly making those vacuous.
    """
    settings = _settings(worker=12, upload=2)
    with _capturing() as handler:
        worker_main._report_binding_ceilings(settings, _ALL_LOOPS)

    messages = [r.getMessage() for r in handler.records]
    named = [m for m in messages if "INQTRIX_UPLOAD_MAX_CONCURRENT" in m]
    assert named, "a binding ceiling must name the variable that caused it"
    assert "Upload 2" in named[-1]
    assert "12" in named[-1]
    assert _MARKER in named[-1], (
        "the silence tests key on this marker; reword it here and there too"
    )


def test_a_ceiling_is_not_reported_for_a_loop_this_process_never_built() -> None:
    """A worker without the knowledge profile runs no indexing loop.

    Naming its ceiling would describe work that cannot happen in this
    process, and would send an operator tuning a variable with no effect.
    """
    settings = _settings(worker=12, indexing=2, upload=2)
    with _capturing() as handler:
        worker_main._report_binding_ceilings(settings, [_UploadWorkerLoop()])

    reported = [m for m in _messages(handler) if _MARKER in m]
    assert reported, "the upload ceiling still binds and must be reported"
    assert "Upload 2" in reported[-1]
    assert "Indexing" not in reported[-1]


def test_the_alias_comes_from_the_field_not_from_a_second_copy() -> None:
    """A name spelled twice points at a stale variable after a rename."""
    from inqtrix.settings import KnowledgeSettings, ServerSettings

    assert worker_main._ceiling_alias(
        ServerSettings(), "upload_max_concurrent"
    ) == ServerSettings.model_fields["upload_max_concurrent"].alias
    assert worker_main._ceiling_alias(
        KnowledgeSettings(), "reindex_max_concurrent"
    ) == KnowledgeSettings.model_fields["reindex_max_concurrent"].alias


def test_nothing_is_said_when_no_ceiling_binds() -> None:
    """Silence is correct only when the process-wide value really governs."""
    settings = _settings()
    with _capturing() as handler:
        worker_main._report_binding_ceilings(settings, _ALL_LOOPS)

    assert not handler.records, (
        f"expected silence, got {_messages(handler)}"
    )


def test_an_equal_ceiling_does_not_count_as_binding() -> None:
    """Equal ceilings change nothing, so reporting them would be noise."""
    settings = _settings(worker=6, indexing=6, upload=6, deletion=6)
    with _capturing() as handler:
        worker_main._report_binding_ceilings(settings, _ALL_LOOPS)

    assert not handler.records, (
        f"expected silence, got {_messages(handler)}"
    )


def test_the_upload_store_carries_no_execution_slot_count() -> None:
    """A store that cannot dispatch must not accept a dispatch ceiling.

    ``DurableJobStoreBase._max_concurrent`` is read at exactly one place,
    ``_dispatch_locked``, which only runs for a store that appends to
    ``_pending``. The upload store never does -- its work is composed by
    UploadOperationService, and ``_make_handle`` refuses to build one. A
    ceiling passed in anyway is a number that governs nothing while
    reading, at both call sites, like the real upload limit.
    """
    import inspect

    from inqtrix.runs.upload_postgres import PostgresUploadOperationStore

    params = inspect.signature(PostgresUploadOperationStore.__init__).parameters
    assert "max_concurrent" not in params, (
        "the upload store cannot dispatch in-process; a slot count here is "
        "a second, inert control beside INQTRIX_UPLOAD_MAX_CONCURRENT"
    )


def test_the_shared_base_demands_no_slot_count() -> None:
    """A base must not require a ceiling its non-dispatching stores never use.

    That the three dispatching stores each SET it, and that the base offers
    no silent default, is pinned in tests/test_runs_shared.py -- next to the
    admission check that reads it.
    """
    import inspect

    from inqtrix.runs.durable_store import DurableJobStoreBase

    params = inspect.signature(DurableJobStoreBase.__init__).parameters
    assert "max_concurrent" not in params


def test_the_worker_counts_every_loop_at_the_width_it_runs() -> None:
    """The transient budget is the sum of what the loops actually run at.

    Counting the process-wide value for all four would overstate it and warn
    on deployments that fit; counting only the research loop would hide the
    other three. Both errors make the startup number worthless.
    """
    settings = _settings(worker=100, indexing=6, upload=6, deletion=2)
    widths = worker_main._effective_loop_widths(settings, _ALL_LOOPS)

    assert sorted(widths) == [2, 6, 6], (
        "each bound loop must be counted at its own ceiling"
    )

    class WorkerLoop:
        """The research loop, which no domain ceiling binds."""

    widths = worker_main._effective_loop_widths(
        settings, [WorkerLoop(), *_ALL_LOOPS]
    )
    assert sum(widths) == 100 + 6 + 6 + 2


def test_a_loop_this_process_never_built_adds_nothing_to_the_budget() -> None:
    """A worker without the knowledge profile opens no indexing connections."""
    settings = _settings(worker=100, indexing=6, upload=6, deletion=2)
    assert worker_main._effective_loop_widths(
        settings, [_UploadWorkerLoop()]
    ) == [6]
