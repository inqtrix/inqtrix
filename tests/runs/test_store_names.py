"""The two names of a store operation, and what a memory store's coroutine may do."""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any

import pytest

from inqtrix import aio
from inqtrix.runs.deletion_operations import DeletionOperationStore
from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.runs.indexing_memory import IndexingJobStore
from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
from inqtrix.runs.memory_store import RunStore
from inqtrix.runs.ports import RunStorePort
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.runs.shared import bridge_to_loop, finish_inline, inline_on_loop
from inqtrix.runs.upload_operations import MemoryUploadOperationStore
from inqtrix.runs.upload_postgres import PostgresUploadOperationStore

_STORES = (
    RunStore,
    PostgresRunStore,
    IndexingJobStore,
    PostgresIndexingJobStore,
    DeletionOperationStore,
    PostgresDeletionOperationStore,
    MemoryUploadOperationStore,
    PostgresUploadOperationStore,
)
_WIRING = frozenset(
    {
        "aclose",
        "astart",
        "bind_authority_coordinator",
        "bind_authorization",
        "bind_checkpoint_cleanup",
        "bind_session_retention",
        "bind_source_lifecycle_authority",
        "close",
        "document_publication_guard",
        "execution_control_guard",
        "from_settings",
        "run_collection_mutation",
        "start",
        "unsubscribe",
    }
)
"""What belongs to the store itself and to its assembly, not to the jobs it holds."""


def _operations(store: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(store, callable)
        if not name.startswith("_") and name not in _WIRING
    }


class _Bridging:
    """What ``bridge_to_loop`` needs of a store."""

    def _call(self, coro: Any) -> Any:
        return aio.call_from_thread(coro)

    async def aoperation(self, value: str, *, suffix: str = "") -> tuple[str, int]:
        """The operation."""
        return value + suffix, threading.get_ident()

    operation = bridge_to_loop(aoperation)


class _Inline:
    def operation(self, value: str, *, suffix: str = "") -> tuple[str, int]:
        """The operation."""
        return value + suffix, threading.get_ident()

    aoperation = inline_on_loop(operation)


def test_the_sync_name_runs_the_operation_on_the_process_loop(process_loop_for_sync_code) -> None:
    async def thread_of_the_loop() -> int:
        return threading.get_ident()

    loop_thread = asyncio.run_coroutine_threadsafe(
        thread_of_the_loop(), process_loop_for_sync_code
    ).result(timeout=10)

    assert _Bridging().operation("a", suffix="b") == ("ab", loop_thread)


def test_the_sync_name_keeps_what_a_reader_of_the_operation_sees() -> None:
    assert _Bridging.operation.__doc__ == "The operation."
    assert list(inspect.signature(_Bridging.operation).parameters) == ["self", "value", "suffix"]
    assert not inspect.iscoroutinefunction(_Bridging.operation)


def test_the_async_name_of_a_memory_operation_stays_on_the_loop() -> None:
    async def main() -> tuple[tuple[str, int], int]:
        return await _Inline().aoperation("a", suffix="b"), threading.get_ident()

    (value, thread), loop_thread = asyncio.run(main())

    assert value == "ab"
    assert thread == loop_thread


def test_a_coroutine_that_never_suspends_finishes_on_this_thread() -> None:
    async def inner() -> int:
        return threading.get_ident()

    async def outer() -> int:
        return await inner()

    assert finish_inline(outer()) == threading.get_ident()


def test_a_coroutine_keeps_its_exception() -> None:
    async def failing() -> None:
        raise ValueError("kept")

    with pytest.raises(ValueError, match="kept"):
        finish_inline(failing())


def test_a_coroutine_that_suspends_is_refused_and_closed() -> None:
    """Only a loop could serve it; hanging or dropping it silently would hide the bug."""

    async def suspending() -> None:
        await asyncio.sleep(0)

    coro = suspending()
    with pytest.raises(RuntimeError, match="suspended"):
        finish_inline(coro)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


@pytest.mark.parametrize("store", _STORES, ids=lambda store: store.__name__)
def test_every_operation_of_a_store_has_both_names(store: type) -> None:
    names = _operations(store)
    alone = [
        name
        for name in sorted(names)
        if f"a{name}" not in names and not (name.startswith("a") and name[1:] in names)
    ]
    assert alone == []


@pytest.mark.parametrize("store", _STORES, ids=lambda store: store.__name__)
def test_the_async_name_awaits_and_the_sync_name_waits(store: type) -> None:
    names = _operations(store)
    for name in sorted(names):
        if f"a{name}" not in names:
            continue
        assert inspect.iscoroutinefunction(getattr(store, f"a{name}")), f"a{name}"
        assert not inspect.iscoroutinefunction(getattr(store, name)), name


def test_the_port_names_both_names_and_the_stores_have_them() -> None:
    port = _operations(RunStorePort)
    assert [name for name in sorted(port) if name[0] != "a" and f"a{name}" not in port] == []
    for store in (RunStore, PostgresRunStore):
        assert sorted(port - _operations(store)) == [], store.__name__
