"""Kernel interrupt translation.

Two interrupt ORIGINS, one outer machinery (park -> control rows ->
decide/answer -> wake -> resume-from-rows):

* **A — the ``ask_user`` tool** raises ``interrupt()`` itself after an
  idempotent clarification create. Its payload is our own
  ``{"kind": "clarification", "id": ...}`` and the resume value is
  delivered as the tool-internal ``interrupt()`` return.
* **B — deepagents HITL middleware** (``interrupt_on`` policy gates)
  raises payloads shaped ``{"action_requests": [...], "review_configs":
  [...]}`` and resumes through ``{"decisions": [...]}`` (shape frozen by
  the harness contract tests).

:func:`translate_kernel_interrupt` is the ONE discriminator between the
two — the algorithm never pattern-matches payloads inline.
"""

from __future__ import annotations

import hashlib
from typing import Any

CLARIFICATION_INTERRUPT = "clarification"
"""Origin A: an ``ask_user`` round backed by a clarification row."""

TOOL_APPROVAL_INTERRUPT = "tool_approval"
"""Origin B: a deepagents HITL gate backed by a ``kind="tool"``
approval row; the policy gates map its resume."""

CHILDREN_INTERRUPT = "children"
"""Origin C: a child-run tool parked slot-free; NO control row backs it
(the child RUN rows are the truth) — the store wakes the parent when
the last child terminates, exactly the workspace-agent contract."""


def ask_user_clarification_id(run_id: str, tool_call_id: str) -> str:
    """The DETERMINISTIC clarification id of one ``ask_user`` call.

    Derived from the tool_call_id, which is frozen inside the
    checkpointed AIMessage — the tool function re-executes on resume and
    must find the row it created before parking. The id HASHES the full
    tool_call_id: provider ids share long constant prefixes
    (``call_...``, ``toolu_01...``), so a plain prefix slice would
    collide across rounds — round 2 would silently reuse round 1's
    answered row and strand the run parked with nothing pending.
    """
    digest = hashlib.sha1(tool_call_id.encode("utf-8")).hexdigest()[:8]
    return f"clr_{run_id[-12:]}_ask_{digest}"


def deliverable_artifact_id(run_id: str, tool_call_id: str) -> str:
    """The DETERMINISTIC artifact id of one ``write_canvas`` create.

    Same full-id hashing rationale as :func:`ask_user_clarification_id`
    (provider ids share constant prefixes): a resume re-execution of the
    creating tool call lands on the same row instead of duplicating the
    deliverable.
    """
    digest = hashlib.sha1(tool_call_id.encode("utf-8")).hexdigest()[:8]
    return f"art_{run_id[-12:]}_del_{digest}"


def translate_kernel_interrupt(payload: Any) -> tuple[str, dict[str, Any]]:
    """Classify one raw interrupt payload into (origin, payload).

    Returns ``(CLARIFICATION_INTERRUPT, {"id": ...})`` for origin A and
    ``(TOOL_APPROVAL_INTERRUPT, <full payload>)`` for origin B.

    Raises:
        ValueError: The payload matches neither origin — a deepagents
            upgrade changed the HITL shape (the loud upgrade tripwire;
            resuming blind would corrupt the run).
    """
    if isinstance(payload, dict):
        if payload.get("kind") == CLARIFICATION_INTERRUPT and payload.get(
            "id"
        ):
            return CLARIFICATION_INTERRUPT, {"id": str(payload["id"])}
        if payload.get("kind") == CHILDREN_INTERRUPT:
            return CHILDREN_INTERRUPT, {}
        actions = payload.get("action_requests")
        # Non-empty required: an actionless HITL payload has nothing to
        # approve — routing it would park a run no decision can wake.
        if isinstance(actions, list) and actions:
            return TOOL_APPROVAL_INTERRUPT, dict(payload)
    raise ValueError(
        f'Unknown kernel interrupt payload: {type(payload).__name__} {str(payload)[:200]!r}'
    )
