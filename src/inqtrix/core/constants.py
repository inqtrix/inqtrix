"""Wire-contract constants shared across serving layers.

Single source for values that appear on multiple correlated wire
surfaces; duplicating them per module would let the surfaces drift
silently.
"""

MODEL_NAME = "research-agent"
"""Public model identifier on the OpenAI-compatible surface.

Appears in three correlated places clients match on: the ``model``
field of non-streaming chat completions, the ``model`` field of every
streaming chunk, and the ``id`` of the single ``/v1/models`` entry.
"""

AGENT_MODE_IDS = ("workspace_agent", "agent_kernel")
"""Registry ids whose runs are AGENT runs (``kind="agent"``).

Gates the agent-only request surface (autonomy, document_id,
response_form, session run-guard) in the runs router; a new agent-grade
algorithm joins here so every correlated surface widens together."""

AGENT_TOOL_DIRECTIVES = ("web_research", "rag_query")
"""Composer ``/``-function tokens: hard tool hints the
runs router whitelists (unknown directive = 400) and the planner/kernel
prompt honors. Both the wire admission and the prompt injection read
THIS tuple."""

"""One-shot Agent Desk execution routes admitted by ``POST /v1/runs``.

Unlike :data:`AGENT_TOOL_DIRECTIVES`, these are server-enforced routes,
not prompt hints.  Keeping the vocabulary here makes HTTP admission,
durable worker replay, capability discovery, and the algorithms consume
one source of truth.
"""

AGENT_SOURCE_IDS = ("web", "knowledge")
"""Source controls published by the Agent Desk capability manifest."""

AGENT_SOURCE_ACCESS = ("available", "disabled")
"""Allowed values of each source-control entry on the run wire."""
