"""A sweep the API process runs on a clock, owned by the app lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from inqtrix import aio

log = logging.getLogger(__name__)

_CLOSE_WAIT_SECONDS = 5.0


class PeriodicSweep:
    """Runs one sweep in the API process, the one driver in every tier.

    Construction is inert: the application lifespan starts the task once
    startup succeeded and closes it before the store the sweep uses. One task
    per instance; ``start`` and ``aclose`` are idempotent and a closed sweep
    starts again cleanly. A failing sweep logs one WARNING and the task goes
    on, unless the failure is one it can never recover from in this process.
    Closing waits five seconds for a pass in flight and then goes on; a pass
    that takes longer keeps running and can meet the closed store, the
    disposed engine and the stopped lanes, and ends with that error.
    """

    def __init__(
        self,
        *,
        name: str,
        label: str,
        sweep: Callable[[], Awaitable[Any]],
        interval_seconds: float,
        finished_by: tuple[type[Exception], ...] = (),
    ) -> None:
        """Bind one sweep to its clock.

        Args:
            name: Task name suffix (``inqtrix-<name>``).
            label: Subject of the WARNING a failing sweep logs.
            sweep: Executes one pass on the loop; sync work hands itself to a lane.
            interval_seconds: Wait before each pass, at least one second.
            finished_by: Failures that end the clock; the sweep has said why.
        """
        self._task_name = f"inqtrix-{name}"
        self._label = label
        self._sweep = sweep
        self._interval = max(1.0, float(interval_seconds))
        self._finished_by = finished_by
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the current lifespan owns a live task."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start one task on the running loop, or leave the running one untouched."""
        if self.running:
            return
        stop = asyncio.Event()
        self._stop = stop
        self._task = asyncio.create_task(self._run(stop), name=self._task_name)

    async def aclose(self) -> None:
        """Stop the task and make a later clean restart possible."""
        stop = self._stop
        task = self._task
        if stop is None or task is None:
            return
        stop.set()
        await asyncio.wait({task}, timeout=_CLOSE_WAIT_SECONDS)
        if task.done():
            self._task = None
            self._stop = None

    async def _run(self, stop: asyncio.Event) -> None:
        while not await aio.wait_for_event(stop, self._interval):
            try:
                await self._sweep()
            except self._finished_by:
                return
            except Exception as exc:  # noqa: BLE001 - the task outlives one sweep
                log.warning(
                    "%s sweep failed; the next one follows in %.0fs (error_type=%s)",
                    self._label,
                    self._interval,
                    type(exc).__name__,
                )
