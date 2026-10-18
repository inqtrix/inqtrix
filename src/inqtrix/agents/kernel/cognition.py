"""Kernel cognition layer — the loop's own fast-tier self-judgment.

The ONE home for cheap metacognitive LLM calls the kernel makes about
its own progress. Today that is the adaptive
evidence-sufficiency judgement behind
:class:`~inqtrix.agents.kernel.middleware.KernelSufficiencyMiddleware`;
a later judge-before-delegate check joins HERE instead of growing a
second judge path.

Deliberately NOT here: a server-side web-query rewrite ("Weg B"). The
HITL gate publishes the model's tool args verbatim and the tool body
executes exactly those args (published == enforced) — rewriting the
query after the gate would search something the user never approved.
Query quality is steered in the prompt instead (``_KERNEL_TOOL_
DISCIPLINE`` and the ``web_instant`` docstring). Revisit only as a
``before_model`` step that runs BEFORE the gate, never inside a tool
body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from inqtrix.agents.evidence import evidence_digest, run_sufficiency_judgement
from inqtrix.model_routing import (
    describe_resolution,
    describe_unresolved_resolution,
)

if TYPE_CHECKING:
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.patterns._structured import StructuredOutcome

SUFFICIENCY_JUDGED_EVENT = "inqtrix.agent.sufficiency.judged"
"""Event type of one advisory sufficiency verdict (nudge or fallback)."""


def judge_kernel_sufficiency(deps: "KernelDeps") -> "StructuredOutcome":
    """One fast-tier coverage verdict over the segment's evidence.

    Mirrors the deep-review call pattern: the ``agent_sufficiency``
    node resolves through the tier map only (a request's model override
    targets the BRAIN, not the cheap coverage check), the resolution is
    emitted for the activity surface ("Beleglage wird bewertet"), and
    usage is booked into the segment accumulator.

    Returns:
        The :class:`StructuredOutcome`; ``value`` is ``None`` when the
        reply never validated — the caller degrades VISIBLY (event
        without nudge), never silently.
    """
    provider_models = getattr(deps.llm, "models", None)
    if provider_models is None:
        desc = describe_unresolved_resolution("agent_sufficiency", "")
    else:
        desc = describe_resolution(
            "agent_sufficiency",
            provider_models,
            "",
            requested_model="",
            requested_effort="",
        )
    deps.emit("inqtrix.node.model_resolution", desc)
    outcome = run_sufficiency_judgement(
        deps.llm,
        success_criteria=[deps.question] if deps.question else [],
        evidence_digest=evidence_digest(list(deps.evidence_refs.values())),
        clarified_context="\n".join(deps.clarified_answers),
        source_coverage=deps.source_coverage.digest(),
        model=desc.get("model") or None,
        reasoning_effort=desc.get("effort") or None,
        timeout=deps.timeout,
    )
    deps.book_usage(
        outcome.usage.get("prompt_tokens", 0),
        outcome.usage.get("completion_tokens", 0),
    )
    return outcome
