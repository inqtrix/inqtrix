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

AGENT_MODE_IDS = ("agent_kernel",)
"""Registry ids a submission may name to start an AGENT run (``kind="agent"``).

Gates the agent-only request surface (autonomy, document_id,
response_form, session run-guard) in the runs router; a new agent-grade
algorithm joins here so every correlated surface widens together."""

AGENT_RUN_MODE_IDS = ("agent_kernel", "workspace_agent")
"""Every mode whose runs are or were agent runs, retired modes included.

Decides which runs take their checkpoint thread with them when they are
deleted, and is the twin of the desk's list of agent runs. A mode joins
when it starts agent runs and never leaves: its runs outlive its
retirement."""

AGENT_TOOL_DIRECTIVES = ("web_research", "rag_query")
"""Composer ``/``-function tokens: hard tool hints the
runs router whitelists (unknown directive = 400) and the kernel prompt
honors. Both the wire admission and the prompt injection read
THIS tuple."""

AGENT_SOURCE_IDS = ("web", "knowledge")
"""Source controls published by the Agent Desk capability manifest."""

AGENT_SOURCE_ACCESS = ("available", "disabled")
"""Allowed values of each source-control entry on the run wire."""
