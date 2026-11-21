"""Narration lines for the Agent-Desk transcript.

The main window shows the agent "thinking along" as short prose in the
run's answer language (:mod:`inqtrix.i18n`).
Every line is built DETERMINISTICALLY from what the run already produced
(the outcome of a delegated task, the documents it writes) — no extra LLM
call, no invented chain-of-thought: it shows only what the
run's data carries, never storytelling. Paragraph granularity by
design: narration events are persisted rows that replay after a reload,
so the channel stays bounded (~10-30 events per run); the typewriter
feel is a frontend rendering concern.
"""

from __future__ import annotations

import re

from collections.abc import Callable
from typing import Any

from inqtrix.harness.contracts.events import NARRATION_EVENT


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
    """THE one transcript line, silently skipping empties.

    One emitter because the transcript is one reader surface. If each emit
    site built the payload itself, "does a narration line pass the writer
    guard" would have as many answers as there are emit sites, and a
    clarification question, a review passage or a child's report could
    reach the reader as the model had written them.

    ``guard`` is the run's own projection of that one question: which
    internal identifiers could this text have copied, and which of them
    did the USER write. It is required, not defaulted — a default would
    be a way to emit an unguarded line, which is the state this emitter
    exists to end.

    ``narration_id`` is DETERMINISTIC per emission site (a review, a
    child's report, a limit ...), never a running counter — checkpointed
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


