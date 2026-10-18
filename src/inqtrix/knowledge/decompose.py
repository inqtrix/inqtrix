"""Query decomposition: split multi-aspect questions into sub-queries.

The deep-profile stage for the aggregation failure class: a question
like "Welche Pflichten gelten fuer Backups, Verschluesselung UND
Aufbewahrung?" scatters over several documents, and a single retrieval
lets the first aspect crowd the others out of the top-k. One fast-tier
LLM call splits such questions into 2-4 self-contained German
sub-queries; the caller retrieves each and interleaves the results so
EVERY aspect contributes candidates.

Decomposition does not REFORMULATE the question — that is the gate
rewrite's job (the single rewrite location). It only splits, which is
why it lives in its own module and stays independently testable and
switchable.

Failure policy (No Silent Fallbacks): an unparseable response yields
an EMPTY decomposition plus the loud ``_knowledge_decompose_fallback``
marker — the run proceeds single-query, visibly degraded, mirroring
the gate's fail-open contract.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from inqtrix.observability.context import bind_purpose
from inqtrix.prompts import build_knowledge_decompose_prompt

log = logging.getLogger("inqtrix")

DECOMPOSE_MARKER_PARSED = "_knowledge_decompose_parsed"
DECOMPOSE_MARKER_FALLBACK = "_knowledge_decompose_fallback"

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@dataclass(frozen=True)
class Decomposition:
    """Outcome of one decomposition call.

    Attributes:
        sub_queries: Self-contained German sub-queries; EMPTY when the
            question is single-aspect (the model answered ``[]``) or
            the response was unparseable — both proceed single-query,
            distinguishable via *marker*.
        marker: ``_knowledge_decompose_parsed`` for a clean parse,
            ``_knowledge_decompose_fallback`` when the response was
            unparseable and the stage degraded to a no-op.
    """

    sub_queries: tuple[str, ...]
    marker: str


def decompose_question(
    llm: Any,
    *,
    question: str,
    model: str | None,
    timeout: float,
    max_sub_queries: int = 4,
) -> tuple[Decomposition, dict[str, int]]:
    """Split a multi-aspect question via one fast-tier LLM call.

    Args:
        llm: The run's LLM provider (``complete_with_metadata``).
        question: The user question to split.
        model: Resolved fast-tier model id (``None`` lets the provider
            default apply).
        timeout: Per-call timeout in seconds.
        max_sub_queries: Upper bound enforced on the parsed list; the
            prompt asks for at most this many.

    Returns:
        The decomposition plus the call's token usage.
    """
    prompt = build_knowledge_decompose_prompt(
        question, max_sub_queries=max_sub_queries
    )
    with bind_purpose("knowledge_decompose"):
        response = llm.complete_with_metadata(
            prompt, model=model, timeout=timeout
        )
    usage = {
        "prompt_tokens": getattr(response, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response, "completion_tokens", 0) or 0,
    }
    content = getattr(response, "content", "") or ""
    match = _JSON_ARRAY.search(content)
    if match is not None:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, list) and all(
                isinstance(item, str) for item in payload
            ):
                sub_queries = tuple(
                    item.strip()
                    for item in payload[:max_sub_queries]
                    if item.strip()
                )
                # A single sub-query is no decomposition — treat it
                # like "single-aspect" instead of doubling retrieval
                # for a near-identical query.
                if len(sub_queries) < 2:
                    sub_queries = ()
                return (
                    Decomposition(
                        sub_queries=sub_queries,
                        marker=DECOMPOSE_MARKER_PARSED,
                    ),
                    usage,
                )
        except ValueError:
            pass
    log.warning(
        "Knowledge decomposition could not be parsed; continuing without decomposition "
        "(marker %s).",
        DECOMPOSE_MARKER_FALLBACK,
    )
    return (
        Decomposition(sub_queries=(), marker=DECOMPOSE_MARKER_FALLBACK),
        usage,
    )
