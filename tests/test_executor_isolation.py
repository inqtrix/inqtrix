"""The blocking-work lanes must not be able to starve one another.

Long AI calls, event-stream readers and short reads have nothing in common
except that they block a thread. Sharing one pool lets the longest of them
decide whether the others get served at all: enough concurrent AI calls and an
event stream stops delivering, or a metrics scrape stops answering, while the
event loop sits idle.

These tests pin the separation itself, not an implementation detail — they
saturate one lane and require the others to stay responsive.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from inqtrix.providers.base import ProviderContext
from inqtrix.server.execution import ExecutionLanes
from inqtrix.settings import ServerSettings
from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM


def _providers() -> ProviderContext:
    return ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch())


def test_lanes_are_sized_independently() -> None:
    """Each lane carries its own width; neither borrows from the other."""
    lanes = ExecutionLanes(ai_workers=3, stream_workers=9)
    try:
        assert lanes.ai._max_workers == 3
        assert lanes.streams._max_workers == 9
        assert lanes.ai is not lanes.streams
        assert lanes.titles._max_workers == 2
        assert lanes.titles is not lanes.ai
    finally:
        lanes.close()


@pytest.mark.asyncio
async def test_pending_titles_do_not_hold_up_the_answer_executor():
    lanes = ExecutionLanes(ai_workers=2, stream_workers=1)
    release = threading.Event()
    occupied = threading.Barrier(3, timeout=2)

    def hold_title():
        occupied.wait()
        release.wait(timeout=3)

    loop = asyncio.get_running_loop()
    blocked = [loop.run_in_executor(lanes.titles, hold_title) for _ in range(2)]
    try:
        await asyncio.to_thread(occupied.wait)
        assert await asyncio.wait_for(loop.run_in_executor(lanes.ai, lambda: "answer"), timeout=0.5) == "answer"
    finally:
        release.set()
        await asyncio.gather(*blocked)
        lanes.close()


@pytest.mark.parametrize("ai_workers,stream_workers", [(0, 4), (4, 0), (-1, 1)])
def test_lane_sizes_below_one_are_rejected(ai_workers, stream_workers) -> None:
    """A zero-width lane would deadlock silently, so it fails loudly."""
    with pytest.raises(ValueError, match="at least one worker"):
        ExecutionLanes(ai_workers=ai_workers, stream_workers=stream_workers)


@pytest.mark.asyncio
async def test_saturated_ai_lane_leaves_stream_readers_responsive() -> None:
    """The behaviour this separation exists for.

    With every AI slot occupied by a call that will not return, a stream
    reader must still be served. Sharing one pool lets a busy chat decide
    whether a metrics scrape is served at all.
    """
    lanes = ExecutionLanes(ai_workers=2, stream_workers=2)
    release = threading.Event()
    occupied = threading.Barrier(3, timeout=5)

    def hold_forever() -> None:
        occupied.wait()
        release.wait(timeout=10)

    loop = asyncio.get_running_loop()
    blocked: list = []
    try:
        blocked = [loop.run_in_executor(lanes.ai, hold_forever) for _ in range(2)]
        # Both AI slots are now taken and will stay taken.
        await asyncio.to_thread(occupied.wait)

        served = await asyncio.wait_for(
            loop.run_in_executor(lanes.streams, lambda: "delivered"),
            timeout=2,
        )
        assert served == "delivered"

        # The shared default pool must be free too — that is where short
        # reads and the metrics scrape live.
        assert await asyncio.wait_for(
            asyncio.to_thread(lambda: "scraped"), timeout=2
        ) == "scraped"
    finally:
        release.set()
        await asyncio.gather(*blocked, return_exceptions=True)
        lanes.close()


@pytest.mark.asyncio
async def test_saturated_stream_lane_leaves_ai_lane_responsive() -> None:
    """The converse: many open streams must not block a new AI call."""
    lanes = ExecutionLanes(ai_workers=2, stream_workers=2)
    release = threading.Event()
    occupied = threading.Barrier(3, timeout=5)

    def hold_forever() -> None:
        occupied.wait()
        release.wait(timeout=10)

    loop = asyncio.get_running_loop()
    blocked: list = []
    try:
        blocked = [
            loop.run_in_executor(lanes.streams, hold_forever) for _ in range(2)
        ]
        await asyncio.to_thread(occupied.wait)

        answered = await asyncio.wait_for(
            loop.run_in_executor(lanes.ai, lambda: "answered"), timeout=2
        )
        assert answered == "answered"
    finally:
        release.set()
        await asyncio.gather(*blocked, return_exceptions=True)
        lanes.close()


def test_stream_reader_width_is_configurable_and_bounded() -> None:
    """Reader width is its own setting; viewer count is not run count."""
    assert ServerSettings(INQTRIX_STREAM_READER_WORKERS=8).stream_reader_workers == 8
    with pytest.raises(ValueError):
        ServerSettings(INQTRIX_STREAM_READER_WORKERS=0)


def test_shipped_reader_width_covers_the_shipped_run_cap() -> None:
    """The relationship the number comes from, not the number itself.

    Every run in flight can have a viewer, and past this many readers they
    are served in rotation -- events arrive late rather than promptly. A
    reader width below the run cap therefore turns a full slate of runs into
    visibly laggy progress, which is exactly the experience the caps exist
    to protect.
    """
    shipped = ServerSettings()
    run_cap = shipped.run_max_concurrent or shipped.max_concurrent
    assert shipped.stream_reader_workers >= run_cap, (
        "reader width must cover the run cap, or a full slate of runs "
        "delivers its events in rotation"
    )


@pytest.mark.asyncio
async def test_chat_runs_on_the_ai_lane_not_the_shared_pool() -> None:
    """The separation is worthless if a call site still reaches the shared pool.

    Sizing and wiring can both be right while a call site quietly passes
    ``None``. The lanes name their threads, so the thread a call actually ran
    on is observable — and that is what this asserts.
    """
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.agent_context import ResolvedAgentContext
    from inqtrix.services.chat_service import ChatService
    from inqtrix.settings import AgentSettings

    seen: dict[str, str] = {}

    class _RecordingAlgorithm:
        def capabilities(self) -> dict:
            return {"supports_chat_completions": True}

        def run(self, request, *, runtime, context):
            seen["thread"] = threading.current_thread().name
            return AgentResult(
                answer="ok",
                result_type="research_result",
                raw={"answer": "ok", "result_state": {}, "usage": {}},
            )

    class _Registry:
        def get(self, mode):
            return _RecordingAlgorithm()

    lanes = ExecutionLanes(ai_workers=2, stream_workers=2)
    try:
        await ChatService(registry=_Registry(), runtime=None).complete(
            question="Frage",
            history="",
            messages=[],
            resolved=ResolvedAgentContext(
                stack_name="",
                mode="research",
                providers=None,
                strategies=None,
                agent_settings=AgentSettings(),
                agent_overrides={},
                knowledge_filters={},
            ),
            chat_agent_settings=AgentSettings(),
            semaphore=asyncio.Semaphore(1),
            lanes=lanes,
        )
    finally:
        lanes.close()

    assert seen["thread"].startswith("inqtrix-ai"), (
        f"chat ran on {seen['thread']!r}; the shared pool names its threads "
        "asyncio_* and would mean the call site still passes None"
    )


def test_container_carries_lanes_sized_from_settings() -> None:
    """The wiring, not just the primitive.

    Routes reach the lanes through the container. If it stopped carrying
    them — or carried lanes sized from something other than the operator's
    settings — every route would quietly fall back to the shared pool that
    this separation exists to escape.
    """
    from inqtrix.server.container import build_container
    from inqtrix.settings import Settings

    settings = Settings(server=ServerSettings(
        MAX_CONCURRENT=5,
        INQTRIX_STREAM_READER_WORKERS=17,
    ))
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(5),
    )
    try:
        # Headroom over the admission ceiling, not parity: a disconnected
        # stream frees its admission slot while its thread runs on. Pinned
        # exactly, because ">" also accepts a hardcoded constant -- and the
        # shipped default of 100 would satisfy it while the operator's 5 was
        # ignored, which is the regression this test names.
        assert container.execution_lanes.ai._max_workers == 5 * 2
        assert container.execution_lanes.streams._max_workers == 17
    finally:
        container.execution_lanes.close()


def test_the_web_gateway_pool_covers_both_shipped_admission_caps() -> None:
    """The edge must not be the thing that refuses what the API admitted.

    Chat and native runs each admit up to their own cap, and every open
    event stream holds one upstream connection for the run's whole
    duration on top of that. A gateway pool sized at or below the sum of
    the caps turns success into a 503 for whatever asks next -- a page
    load, a quota poll -- while the API itself still had room.
    """
    from inqtrix_web_gateway import settings as gateway

    shipped = ServerSettings()
    admitted = shipped.max_concurrent + (
        shipped.run_max_concurrent or shipped.max_concurrent
    )
    assert gateway._DEFAULT_MAX_UPSTREAM_CONNECTIONS > admitted, (
        "the gateway pool must exceed chat + run admission, or the edge "
        "refuses load the API accepted"
    )
