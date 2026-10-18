"""Sufficiency gate: one fast-tier LLM call judging retrieved evidence.

The gate is the honesty mechanism of ``mode=knowledge``: instead of
always answering from the top-k (which fabricates confidence on
unanswerable questions), one small LLM call judges whether the
evidence carries the question — and may propose EXACTLY ONE rewritten
query for a second retrieval pass. Insufficient evidence after that
produces the honest "no relevant content" answer.

Failure policy (No Silent Fallbacks): an unparseable gate response
falls OPEN to "sufficient" — a cited answer from real evidence beats a
crashed request — but always with the loud ``_knowledge_gate_fallback``
marker in the log and the iteration state, never invisibly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from inqtrix.observability.context import bind_purpose
from inqtrix.prompts import build_knowledge_gate_prompt

log = logging.getLogger("inqtrix")

GATE_MARKER_PARSED = "_knowledge_gate_parsed"
GATE_MARKER_FALLBACK = "_knowledge_gate_fallback"

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one sufficiency evaluation.

    Attributes:
        sufficient: Whether the evidence carries the question.
        coverage: Three-way evidence coverage — ``"full"``,
            ``"partial"``, or ``"none"``. The refusal decision keys on
            THIS, not on ``sufficient``: live evaluation showed the
            binary verdict refusing answerable multi-aspect questions
            wholesale (DORA tier, false_refusal 0.56) although the
            answer prompt already discloses gaps explicitly. Only
            ``"none"`` (no relevant evidence at all) justifies the
            honest refusal; ``"partial"`` answers with the gaps named.
            A clean parse without the field maps ``sufficient`` to
            ``full``/``none`` (older-style responses stay safe).
        rewritten_query: At most one alternative query for the second
            retrieval pass; ``None`` when the model offered none.
        reason: The model's one-sentence justification (markers/logs).
        marker: ``_knowledge_gate_parsed`` for a clean parse,
            ``_knowledge_gate_fallback`` when the response was
            unparseable and the gate failed open.
    """

    sufficient: bool
    rewritten_query: str | None
    reason: str
    marker: str
    coverage: str = "full"


def evaluate_evidence(
    llm: Any,
    *,
    question: str,
    evidence_block: str,
    model: str | None,
    timeout: float,
    vocabulary_bridge: bool = False,
) -> tuple[GateDecision, dict[str, int]]:
    """Run the gate call and parse its strict-JSON verdict.

    Args:
        llm: The run's LLM provider (``complete_with_metadata``).
        question: The user question.
        evidence_block: The rendered evidence overview (same block the
            answer prompt receives — the gate judges what the answerer
            will actually see).
        model: Resolved fast-tier model id (``None`` lets the provider
            default apply).
        timeout: Per-call timeout in seconds.
        vocabulary_bridge: Select the technical-vocabulary rewrite
            prompt variant (profile-controlled); the default keeps the
            pre-profile prompt byte-identical.
    """
    prompt = build_knowledge_gate_prompt(
        question, evidence_block, vocabulary_bridge=vocabulary_bridge
    )
    with bind_purpose("knowledge_gate"):
        response = llm.complete_with_metadata(
            prompt, model=model, timeout=timeout
        )
    usage = {
        "prompt_tokens": getattr(response, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response, "completion_tokens", 0) or 0,
    }
    content = getattr(response, "content", "") or ""
    match = _JSON_BLOCK.search(content)
    if match is not None:
        try:
            payload = json.loads(match.group(0))
            rewritten = payload.get("rewritten_query")
            if not isinstance(rewritten, str) or not rewritten.strip():
                rewritten = None
            sufficient = bool(payload["sufficient"])
            coverage = str(payload.get("coverage", "")).strip().lower()
            if coverage not in ("full", "partial", "none"):
                # Older-style response without the field: map the
                # binary verdict conservatively.
                coverage = "full" if sufficient else "none"
            return (
                GateDecision(
                    sufficient=sufficient,
                    rewritten_query=rewritten,
                    reason=str(payload.get("reason", "")).strip(),
                    marker=GATE_MARKER_PARSED,
                    coverage=coverage,
                ),
                usage,
            )
        except (KeyError, ValueError, TypeError):
            pass
    log.warning(
        "Knowledge-Gate response could not be parsed; falling open to 'sufficient' "
        "(marker %s).",
        GATE_MARKER_FALLBACK,
    )
    return (
        GateDecision(
            sufficient=True,
            rewritten_query=None,
            reason="gate response unparseable",
            marker=GATE_MARKER_FALLBACK,
            coverage="full",
        ),
        usage,
    )
