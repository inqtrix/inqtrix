"""How a worker process ends: a signal, a fatal claim failure, an undrained job."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix import aio
from inqtrix.settings import QueueSettings, Settings, StorageSettings
from inqtrix.worker import __main__ as worker_main
from inqtrix.worker.loop import WorkerClaimGuardError


class _Exited(BaseException):
    """Stands in for ``os._exit``, which would take the test run with it."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Loop:
    """A claim loop as ``_serve`` sees it."""

    def __init__(self, *, failure: Exception | None = None, drained: bool = True) -> None:
        self.failure = failure
        self.drained = drained
        self.stop = asyncio.Event()
        self.events: list[str] = []
        self.ignores_stop = False

    async def run_forever(self) -> None:
        self.events.append("claiming")
        if self.failure is not None:
            raise self.failure
        await self.stop.wait()
        if self.ignores_stop:
            await asyncio.sleep(3_600)
        # A blocking read still has to return.
        await asyncio.sleep(0.05)
        self.events.append("claims ended")

    def request_stop(self) -> None:
        self.events.append("stop")
        self.stop.set()

    async def drain(self, timeout: float) -> bool:
        self.events.append("drain")
        return self.drained


class _Recorder:
    store = SimpleNamespace()

    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    async def aclose(self) -> None:
        self.events.append("aclose")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()

    async def no_budget_report(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "inqtrix.storage.connection_budget.report_connection_budget", no_budget_report
    )
    monkeypatch.setattr(
        "inqtrix.worker.metrics.start_worker_metrics", lambda _settings, _engine: False
    )
    monkeypatch.setattr("inqtrix.usage.recorder.install_usage_recorder", lambda _settings, **_: recorder)
    monkeypatch.setattr("inqtrix.observability.otel.shutdown_tracing", lambda: None)

    def exit_for_real(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(os, "_exit", exit_for_real)
    return recorder


class _Engine:
    def __init__(self) -> None:
        self.disposed_on: list[asyncio.AbstractEventLoop] = []

    async def dispose(self) -> None:
        self.disposed_on.append(asyncio.get_running_loop())


def _worker(*loops: _Loop) -> worker_main._Worker:
    return worker_main._Worker(
        worker_id="worker-test",
        settings=Settings(),
        container=SimpleNamespace(
            agent_checkpointer=None,
            permission_service=SimpleNamespace(audit_sink=None),
            session_factory=None,
        ),
        loops=list(loops),
        stores=[],
        database_engine=_Engine(),
    )


async def _stop_by_signal(worker: worker_main._Worker) -> None:
    serving = asyncio.create_task(worker_main._serve(worker))
    while not all(loop.events for loop in worker.loops):
        await asyncio.sleep(0.01)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, timeout=10)


def test_a_signal_ends_claims_first_then_drains_and_returns(recorder: _Recorder) -> None:
    first, second = _Loop(), _Loop()

    asyncio.run(_stop_by_signal(_worker(first, second)))

    for loop in (first, second):
        assert loop.events == ["claiming", "stop", "claims ended", "drain"]
    assert recorder.events == ["start", "aclose"]


def test_an_undrained_job_leaves_through_exit_without_a_cancel(recorder: _Recorder) -> None:
    with pytest.raises(_Exited) as exited:
        asyncio.run(_stop_by_signal(_worker(_Loop(drained=False))))

    assert exited.value.code == 0
    assert recorder.events == ["start", "aclose"]


def test_leaving_through_exit_cancels_no_running_job(recorder: _Recorder) -> None:
    """Not cancelled: the entry goes to another worker through heartbeat silence."""

    async def scenario() -> asyncio.Task[None]:
        job = asyncio.create_task(asyncio.sleep(3_600), name="a running segment")
        with pytest.raises(_Exited):
            await _stop_by_signal(_worker(_Loop(drained=False)))
        assert not job.cancelling()
        assert not job.done()
        job.cancel()
        return job

    asyncio.run(scenario())


def test_a_claim_task_still_running_counts_as_not_drained(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim in flight can still start a segment: an empty active set proves nothing."""
    monkeypatch.setattr(worker_main, "_DRAIN_SECONDS", 0.1)
    stuck = _Loop()
    stuck.ignores_stop = True

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(_Exited) as exited:
            asyncio.run(_stop_by_signal(_worker(stuck)))

    assert exited.value.code == 0
    assert stuck.events[-1] == "drain"
    assert "1 claim loops did not end within the drain window" in caplog.text


def test_a_fatal_claim_failure_stops_every_loop_and_names_the_cause_once_drained(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_main, "_DRAIN_SECONDS", 0.2)
    healthy = _Loop()
    failing = _Loop(
        failure=WorkerClaimGuardError(
            "connection to postgresql://app:hunter2@db/inqtrix failed: role is unsafe"
        )
    )

    with pytest.raises(RuntimeError) as failure:
        asyncio.run(worker_main._serve(_worker(failing, healthy)))

    message = str(failure.value)
    assert "Worker claim contract failed" in message
    assert "role is unsafe" in message
    assert "hunter2" not in message
    # The chain stays cut: a driver error carries the connection string.
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True
    assert sorted(healthy.events) == sorted(["claiming", "stop", "claims ended", "drain"])
    assert healthy.events[-2:] == ["claims ended", "drain"]
    assert recorder.events == ["start", "aclose"]


def test_a_fatal_claim_failure_with_undrained_work_exits_with_a_failure_code(
    recorder: _Recorder,
) -> None:
    failing = _Loop(failure=WorkerClaimGuardError("unsafe"), drained=False)

    with pytest.raises(_Exited) as exited:
        asyncio.run(worker_main._serve(_worker(failing)))

    assert exited.value.code == 1


def test_a_failing_close_does_not_take_the_exit_decision_with_it(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A body still running must not meet a closed loop because a close failed."""

    async def fail_to_close() -> None:
        raise RuntimeError("store gone")

    monkeypatch.setattr(recorder, "aclose", fail_to_close)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(_Exited):
            asyncio.run(_stop_by_signal(_worker(_Loop(drained=False))))

    assert "closing the usage ledger failed at shutdown" in caplog.text


def test_a_fatal_failure_with_a_busy_lane_exits_with_a_failure_code(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        aio,
        "lane_snapshot",
        lambda _loop=None: {"blocking": aio.LaneSnapshot(running=1, waiting=0, workers=2)},
    )

    with pytest.raises(_Exited) as exited:
        asyncio.run(worker_main._serve(_worker(_Loop(failure=WorkerClaimGuardError("unsafe")))))

    assert exited.value.code == 1


def test_a_lane_still_busy_after_a_clean_drain_leaves_through_exit(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane's thread would otherwise hold the interpreter's exit for its whole job."""
    monkeypatch.setattr(
        aio,
        "lane_snapshot",
        lambda _loop=None: {"blocking": aio.LaneSnapshot(running=1, waiting=0, workers=1)},
    )

    with pytest.raises(_Exited) as exited:
        asyncio.run(_stop_by_signal(_worker(_Loop())))

    assert exited.value.code == 0


def test_a_signal_ends_the_retention_tasks(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended: list[bool] = []

    def retention(settings: Settings, stop_event: asyncio.Event) -> asyncio.Task[None]:
        del settings

        async def until_stopped() -> None:
            await stop_event.wait()
            ended.append(True)

        return asyncio.create_task(until_stopped())

    monkeypatch.setattr(worker_main, "_start_trace_retention", retention)

    asyncio.run(_stop_by_signal(_worker(_Loop())))

    assert ended == [True]


def test_every_job_store_closes_while_its_loop_still_runs(recorder: _Recorder) -> None:
    closed_on: list[asyncio.AbstractEventLoop] = []

    class _Store:
        async def aclose(self) -> None:
            closed_on.append(asyncio.get_running_loop())

    worker = _worker(_Loop())
    worker.stores.extend([_Store(), _Store()])

    asyncio.run(_stop_by_signal(worker))

    assert len(closed_on) == 2
    assert closed_on[0] is closed_on[1]


def test_a_body_still_running_keeps_its_store(recorder: _Recorder) -> None:
    closed: list[bool] = []

    class _Store:
        async def aclose(self) -> None:
            closed.append(True)

    worker = _worker(_Loop(drained=False))
    worker.stores.append(_Store())

    with pytest.raises(_Exited):
        asyncio.run(_stop_by_signal(worker))

    assert closed == []


def test_the_engine_goes_after_the_stores_and_the_last_ledger_rows(
    recorder: _Recorder, monkeypatch
) -> None:
    order: list[str] = []

    class _Store:
        async def aclose(self) -> None:
            order.append("store")

    class _Usage:
        store = None

        def start(self) -> None:
            pass

        async def aclose(self) -> None:
            order.append("usage")

    class _Engine:
        async def dispose(self) -> None:
            order.append("engine")

    monkeypatch.setattr(
        "inqtrix.usage.recorder.install_usage_recorder", lambda *_, **__: _Usage()
    )
    worker = _worker(_Loop())
    worker.database_engine = _Engine()
    worker.stores.append(_Store())

    asyncio.run(_stop_by_signal(worker))

    assert order == ["store", "usage", "engine"]


def test_a_body_still_running_keeps_the_engine(recorder: _Recorder) -> None:
    worker = _worker(_Loop(drained=False))

    with pytest.raises(_Exited):
        asyncio.run(_stop_by_signal(worker))

    assert worker.database_engine.disposed_on == []


def test_the_transition_lane_is_never_tighter_than_the_claim_slots(
    recorder: _Recorder,
) -> None:
    class _Reading(_Loop):
        lane: aio.LaneSnapshot | None = None

        async def run_forever(self) -> None:
            self.lane = aio.lane_snapshot()[aio.LEGACY_LANE]
            await super().run_forever()

    first, second = _Reading(), _Reading()

    asyncio.run(_stop_by_signal(_worker(first, second)))

    assert first.lane is not None
    settings = Settings().queue
    assert first.lane.workers == 2 * settings.worker_concurrency + settings.blocking_pool_size


def test_main_keeps_what_it_built_out_of_later_collections(monkeypatch) -> None:
    """Spared from every full collection: the composition, not the jobs after it."""
    order: list[str] = []

    async def verified(guard: Any) -> None:
        order.append("verified")

    async def serve(worker: Any) -> None:
        order.append("serve")

    monkeypatch.setattr(worker_main, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(worker_main, "check_retired_settings", lambda **kwargs: [])
    monkeypatch.setattr(worker_main, "log_status_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker_main,
        "Settings",
        lambda: Settings(
            storage=StorageSettings(
                backend="postgres", database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db"
            ),
            queue=QueueSettings(backend="valkey", valkey_url="redis://127.0.0.1:1/0"),
        ),
    )
    monkeypatch.setattr("inqtrix.auth.log_redaction.configure_stable_pseudonyms", lambda pepper: None)
    monkeypatch.setattr("inqtrix.observability.otel.setup_tracing", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_main, "_verified_database_claim_contract", verified)
    monkeypatch.setattr(
        worker_main, "_compose_worker", lambda settings, guard: order.append("compose")
    )
    monkeypatch.setattr(aio, "freeze_startup_heap", lambda: order.append("freeze"))
    monkeypatch.setattr(worker_main, "_serve", serve)

    worker_main.main()

    assert order == ["verified", "compose", "freeze", "serve"]
