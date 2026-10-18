"""Event-sink contract shared by algorithms and the run store.

An event sink is the callable an algorithm uses to emit structured
progress events for one run. The shape matches the existing
``run_event_sink`` parameter of ``inqtrix.graph.run`` and the
``RunHandle.emit`` method of the in-memory run store, so wiring an
algorithm into the native ``/v1/runs`` surface is a pass-through. The
callable alias is deliberately the whole contract.
"""

from __future__ import annotations

from typing import Any, Callable

EventSink = Callable[[str, dict[str, Any]], None]
"""Callable emitting one ``(event_type, payload)`` event for a run."""
