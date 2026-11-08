"""The kernel graph's state: the framework's agent state with a delta channel on ``messages``.

A kernel thread adds a message for every model turn and tool result. A plain
``add_messages`` channel stores the whole list at every checkpoint, so a run's
checkpoints grow with the square of its messages; the delta channel stores the
writes and folds them on load, with a full snapshot every
``MESSAGES_SNAPSHOT_FREQUENCY`` steps.
"""

from __future__ import annotations

from typing import Annotated, Any, Required

from langchain.agents.middleware import AgentState
from langchain_core.messages import AnyMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import add_messages

MESSAGES_SNAPSHOT_FREQUENCY = 50
"""Steps between two full snapshots of the message list in the checkpoints."""


def messages_reducer(state: list[AnyMessage] | None, writes: list[Any]) -> list[AnyMessage]:
    """Batch reducer of the delta channel: the public ``add_messages``, folded over the writes.

    The channel keeps the semantics of the default one: merge by message id,
    removal by ``RemoveMessage``, a full reset by ``REMOVE_ALL_MESSAGES``.
    """
    merged = list(state or [])
    for write in writes:
        merged = add_messages(merged, write)
    return merged


class KernelState(AgentState):
    """The state of the kernel graph; middleware adds its own keys."""

    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(messages_reducer, snapshot_frequency=MESSAGES_SNAPSHOT_FREQUENCY),
        ]
    ]
