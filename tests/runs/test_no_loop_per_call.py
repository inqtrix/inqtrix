"""Kernel code reaches the process loop, and what runs there stays off it."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from statistics import quantiles
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from inqtrix import aio
from inqtrix.agents.kernel.deps import run_coro
from inqtrix.capabilities.contracts import CapabilityContext, CapabilityDefinition, Effect
from inqtrix.capabilities.registry import CapabilityRegistry

_HANG_SECONDS = 10.0
_SUBJECT: ContextVar[str] = ContextVar("test_no_loop_per_call_subject", default="unset")


class _Input(BaseModel):
    value: str = ""


class _Output(BaseModel):
    value: str = ""


@pytest.fixture
def on_loop_thread() -> Iterator[Any]:
    """A check for whether the calling thread is the process loop's thread."""
    loop = aio.default_loop()
    assert loop is not None

    async def thread_of_the_loop() -> int:
        return threading.get_ident()

    ident = asyncio.run_coroutine_threadsafe(thread_of_the_loop(), loop).result(_HANG_SECONDS)
    yield lambda: threading.get_ident() == ident


def _registry(handler: Any) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            id="test.capability",
            summary="A test capability.",
            input_model=_Input,
            output_model=_Output,
            effect=Effect.READ,
            idempotent=True,
            handler=handler,
        )
    )
    return registry


class TestRunCoro:
    def test_it_runs_on_the_process_loop(self) -> None:
        async def running_loop() -> asyncio.AbstractEventLoop:
            return asyncio.get_running_loop()

        assert run_coro(running_loop()) is aio.default_loop()

    def test_it_never_starts_a_loop_when_there_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(aio, "_default_loop", None)

        async def nothing() -> None:
            return None

        with pytest.raises(aio.LoopNotConfigured):
            run_coro(nothing())

    def test_parallel_kernel_threads_share_one_loop_bound_lock(self) -> None:
        """The hang the per-call loops could end in: a lock binds to one loop."""

        async def make_lock() -> asyncio.Lock:
            return asyncio.Lock()

        lock = run_coro(make_lock())
        entered = 0

        async def access() -> None:
            nonlocal entered
            async with lock:
                await asyncio.sleep(0)
                entered += 1

        def one_access() -> None:
            run_coro(access())

        with ThreadPoolExecutor(3, thread_name_prefix="test-kernel-thread") as threads:
            for _ in range(200):
                calls = [threads.submit(one_access) for _ in range(3)]
                for call in calls:
                    call.result(timeout=_HANG_SECONDS)
        assert entered == 600

    def test_context_and_exceptions_cross_it_unchanged(self) -> None:
        class Refused(RuntimeError):
            pass

        async def read_then_refuse() -> None:
            raise Refused(_SUBJECT.get())

        token = _SUBJECT.set("the-run")
        try:
            with pytest.raises(Refused, match="the-run"):
                run_coro(read_then_refuse())
        finally:
            _SUBJECT.reset(token)


class TestCoroutinesOnTheSharedLoop:
    def test_a_capability_checks_its_authority_off_the_loop(self, on_loop_thread: Any) -> None:
        """The check reads the database synchronously; on the loop it would hold every run."""
        seen: list[bool] = []

        async def handler(payload: _Input, context: CapabilityContext) -> _Output:
            del context
            return _Output(value=payload.value)

        registry = _registry(handler)
        context = CapabilityContext(
            principal=None, authority_check=lambda: seen.append(on_loop_thread())
        )

        result = run_coro(registry.invoke("test.capability", {"value": "x"}, context))

        assert result.value == "x"
        assert seen == [False, False]

    def test_a_refused_authority_still_stops_the_capability(self) -> None:
        class Revoked(RuntimeError):
            pass

        ran: list[bool] = []

        async def handler(payload: _Input, context: CapabilityContext) -> _Output:
            del payload, context
            ran.append(True)
            return _Output()

        def refuse() -> None:
            raise Revoked

        with pytest.raises(Revoked):
            run_coro(
                _registry(handler).invoke(
                    "test.capability", {}, CapabilityContext(principal=None, authority_check=refuse)
                )
            )
        assert ran == []

    def test_the_source_service_awaits_the_run_store(self, on_loop_thread: Any) -> None:
        from inqtrix.services.agent_source_service import AgentSourceService

        seen: list[bool] = []

        class Runs:
            async def aget(self, run_id: str, **_scope: Any) -> dict[str, Any]:
                seen.append(on_loop_thread())
                return {"run_id": run_id, "access": {"mode": "owner"}, "session_id": ""}

        service = AgentSourceService(sessions=SimpleNamespace(), run_store=Runs(), control=None)
        context = CapabilityContext(
            principal=None,
            run_id="run_1",
            workspace_id="desk",
            authority_check=lambda: seen.append(on_loop_thread()),
        )

        run_coro(service.assert_scope(context))

        assert seen == [False, True], "the sync check runs off the loop, the store on it"


class TestProviderCallsOnTheSharedLoop:
    def test_thirty_long_searches_leave_short_work_fast(self) -> None:
        """A search can take minutes; the store calls of claim and cancel must not queue behind it."""
        from inqtrix.capabilities.catalog.web import build_web_capabilities
        from inqtrix.search_result import GroundedSearchResult

        release = threading.Event()

        class SlowSearch:
            def search(self, *_args: Any, **_kwargs: Any) -> GroundedSearchResult:
                release.wait(60.0)
                return GroundedSearchResult()

        registry = CapabilityRegistry()
        for definition in build_web_capabilities(search_provider=SlowSearch()):
            registry.register(definition)

        async def scenario() -> list[float]:
            searches = [
                asyncio.ensure_future(
                    registry.invoke(
                        "web.search.instant",
                        {"query": f"frage {number}"},
                        CapabilityContext(principal=None),
                    )
                )
                for number in range(30)
            ]
            await asyncio.sleep(0.05)
            durations: list[float] = []
            try:
                for _ in range(40):
                    started = time.monotonic()
                    await asyncio.wait_for(aio.run_blocking(time.monotonic), timeout=2.0)
                    durations.append(time.monotonic() - started)
            finally:
                release.set()
            await asyncio.gather(*searches)
            return durations

        async def on_a_process_loop() -> list[float]:
            loop = asyncio.get_running_loop()
            aio.configure_loop(loop, blocking_workers=4, provider_workers=30, watch=False)
            try:
                return await scenario()
            finally:
                aio.close_loop(loop)

        durations = asyncio.run(on_a_process_loop())
        assert quantiles(durations, n=20, method="inclusive")[18] < 0.05
