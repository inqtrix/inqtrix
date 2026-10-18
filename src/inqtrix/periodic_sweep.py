"""A sweep the API process runs on a clock, owned by the app lifespan."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("inqtrix")


class PeriodicSweep:
    """Runs one sweep in the API process, the one driver in every tier.

    Construction is inert: the application lifespan starts the loop once
    startup succeeded and closes it before the store the sweep uses, so a
    sweep in flight never meets a closed store. One loop per instance;
    ``start`` and ``close`` are idempotent and a closed sweep starts again
    cleanly. A failing sweep logs one WARNING and the loop goes on.
    """

    def __init__(
        self,
        *,
        name: str,
        label: str,
        sweep: Callable[[], Any],
        interval_seconds: float,
    ) -> None:
        """Bind one sweep to its clock.

        Args:
            name: Thread name suffix (``inqtrix-<name>``).
            label: Subject of the WARNING a failing sweep logs.
            sweep: Executes one pass.
            interval_seconds: Wait before each pass, at least one second.
        """
        self._thread_name = f"inqtrix-{name}"
        self._label = label
        self._sweep = sweep
        self._interval = max(1.0, float(interval_seconds))
        self._lifecycle_lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Whether the current lifespan owns a live loop."""
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start one loop, or leave the already-running loop untouched."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(stop,),
                name=self._thread_name,
                daemon=True,
            )
            self._stop = stop
            self._thread = thread
            thread.start()

    def close(self) -> None:
        """Stop the loop and make a later clean restart possible."""
        with self._lifecycle_lock:
            stop = self._stop
            thread = self._thread
        if stop is None or thread is None:
            return
        stop.set()
        if thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5)
        with self._lifecycle_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._stop = None

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval):
            try:
                self._sweep()
            except Exception as exc:  # noqa: BLE001 - the loop outlives one sweep
                log.warning(
                    "%s sweep failed; the next one follows in %.0fs (error_type=%s)",
                    self._label,
                    self._interval,
                    type(exc).__name__,
                )
