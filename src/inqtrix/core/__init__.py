"""Shared core contracts of the Inqtrix platform.

This package holds the algorithm-neutral vocabulary every execution
mode (web research, direct LLM, and the future knowledge/RAG and agent
task modes) speaks:

* :mod:`inqtrix.core.results` — ``RunRequest`` in, ``AgentResult`` out.
* :mod:`inqtrix.core.algorithms` — the ``AgentAlgorithm`` protocol and
  the ``AlgorithmRegistry`` that replaces mode branching.
* :mod:`inqtrix.core.context` — the app-level ``RuntimeContext`` and
  the per-run ``RunContext`` (resolved providers, settings, cancel
  token, event sink).
* :mod:`inqtrix.core.events` — the event-sink contract shared with the
  run store.

The architectural line for agent executions: ``HTTP router ->
application service -> AlgorithmRegistry -> AgentAlgorithm ->
providers/stores -> run events/result``. Streamed chat now follows this
same line — ``inqtrix.server.streaming`` dispatches ``algorithm.run``
with a per-request ``RunContext`` rather than binding the graph itself.
Known exceptions, kept deliberately and to be migrated with their own
phases: the editor and text routers orchestrate single LLM calls inline
(no agent run involved).
"""

from inqtrix.core.algorithms import (
    AgentAlgorithm,
    AlgorithmId,
    AlgorithmRegistry,
    UnknownAlgorithm,
)
from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.events import EventSink
from inqtrix.core.results import AgentResult, RunRequest

__all__ = [
    "AgentAlgorithm",
    "AgentResult",
    "AlgorithmId",
    "AlgorithmRegistry",
    "EventSink",
    "RunContext",
    "RunRequest",
    "RuntimeContext",
    "UnknownAlgorithm",
]
