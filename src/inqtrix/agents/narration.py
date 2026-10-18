"""Narration lines for the Agent-Desk transcript.

The main window shows the agent "thinking along" as short prose in the
run's answer language (:mod:`inqtrix.i18n`).
Every line is built DETERMINISTICALLY from artifacts the run already
produced (discovery result, plan summary, task outcomes, memo outline) —
no extra LLM call, no invented chain-of-thought: it shows only what the
run's data carries, never storytelling. Paragraph granularity by
design: narration events are persisted rows that replay after a reload,
so the channel stays bounded (~10-30 events per run); the typewriter
feel is a frontend rendering concern.
"""

from __future__ import annotations

import re

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from inqtrix.i18n import t, t_count

if TYPE_CHECKING:
    from inqtrix.agents.phase_models import DiscoveryResult

NARRATION_EVENT = "inqtrix.agent.narration"


def emit_narration(
    emit: Callable[[str, dict[str, Any]], None],
    *,
    narration_id: str,
    kind: str,
    text: str,
    phase: str,
    guard: Callable[[str], str],
    final: bool = True,
) -> None:
    """THE one transcript line, for BOTH engines, silently skipping empties.

    One emitter because the transcript is one reader surface. If each
    engine built the payload itself, "does a narration line pass the
    writer guard" would have as many answers as there are emit sites — and
    the mission's answer would say nothing about the kernel's, where a
    clarification question, a review passage and a child's report would
    reach the reader as the model had written them.

    ``guard`` is the engine's own projection of that one question: which
    internal identifiers could this text have copied, and which of them
    did the USER write. It is required, not defaulted — a default would
    be a way to emit an unguarded line, which is the state this emitter
    exists to end.

    ``narration_id`` is DETERMINISTIC per emission site (plan version,
    task id, section index ...), never a running counter — checkpointed
    nodes may re-execute after an interrupt, and a stable id keeps the
    replayed line recognizable instead of multiplying.

    ``final`` is False only for a line that is still being written; the
    reader sees it replaced, not repeated.
    """
    if not text.strip():
        return
    emit(
        NARRATION_EVENT,
        {
            "narration_id": narration_id,
            "kind": kind,
            "text": guard(text),
            "phase": phase,
            "final": final,
        },
    )

_TEXT_LIMIT = 400
"""Per-line prose bound (mirrored by the sanitizer's text cap)."""


def first_sentences(text: str, limit: int = _TEXT_LIMIT) -> str:
    """Compact prose head: whole sentences up to *limit* characters.

    Falls back to a word-boundary cut when the first sentence alone
    exceeds the limit — a narration line must never end mid-word.
    """
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    head = normalized[:limit]
    sentence_end = max(
        head.rfind(". "), head.rfind("! "), head.rfind("? ")
    )
    if sentence_end >= limit // 3:
        return head[: sentence_end + 1]
    boundary = head.rfind(" ")
    if boundary < limit // 2:
        boundary = limit - 4
    return head[:boundary].rstrip() + " ..."


def discovery_narration(
    discovery: "DiscoveryResult | None", *, language: str
) -> str:
    """One paragraph on what the exploration established.

    The paragraph quotes the analyst's FIRST gap: the analyst contract
    lists the most important gap first and writes every gap description
    for the user — one plain sentence without search vocabulary or
    identifiers (``DiscoveryGap.description`` and the analyst prompt's
    rules). This builder adds only counted framing of its own.

    ``language`` is keyword-only and has NO default: a caller that forgets
    it fails loudly instead of silently emitting one fixed language.
    """
    if discovery is None:
        return ""
    words = {"language": language or ""}
    facts = len(discovery.known_facts)
    gaps = len(discovery.gaps)
    if facts == 0 and gaps == 0:
        return t(words, "agent_narration_discovery_empty")
    parts = [
        t(
            words,
            "agent_narration_discovery_counts",
            facts=t_count(words, "agent_narration_discovery_facts", facts),
            gaps=t_count(words, "agent_narration_discovery_gaps", gaps),
        )
    ]
    if discovery.gaps:
        parts.append(
            t(
                words,
                "agent_narration_discovery_top_gap",
                gap=first_sentences(discovery.gaps[0].description, 160),
            )
        )
    return " ".join(part for part in parts if part)


def plan_narration(
    summary_markdown: str, task_count: int, *, language: str
) -> str:
    """The plan's own summary prose, framed with the task count."""
    words = {"language": language or ""}
    summary = first_sentences(summary_markdown)
    if not summary:
        return t_count(words, "agent_narration_plan_proposal", task_count)
    return t_count(words, "agent_narration_plan_summary", task_count, summary=summary)


def task_narration(title: str, summary: str) -> str:
    """One line per finished task: its title plus the result head.

    Language-neutral by construction — title and summary already carry the
    run's language, so this function holds no prose of its own.

    Heading markers are dropped first: a report that opens with
    ``# Titel`` would otherwise put the marker into the transcript line
    ("Memo verfassen: # EZB-Einlagensatz ...").
    """
    head = first_sentences(re.sub(r"(?m)^#{1,6}[ \t]+", "", summary), 240)
    if not head:
        return ""
    return f"{title}: {head}" if title else head


def synthesis_narration(
    title: str, section_count: int, *, language: str
) -> str:
    """Announces the memo being written (the canvas shows it live)."""
    return t_count(
        {"language": language or ""}, "agent_narration_synthesis", section_count, title=title
    )


def section_narration(title: str, *, language: str) -> str:
    """One line per completed memo section."""
    return t(
        {"language": language or ""},
        "agent_narration_section_written",
        title=title,
    )
