"""Line-delta counting for artifact writes.

The +N/-M numbers are computed ONCE at the server write — both stores
already hold the previous body in memory there — and travel in the
durable artifact event payloads. The UI never recounts; a reload
replays the same events, so there is exactly one source of truth.
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# Loud guard, never a silent cap: beyond this many lines the
# quadratic matcher is skipped WITH a warning and the caller receives
# ``None`` — the badge is then honestly absent instead of silently
# wrong or slow inside the store transaction.
MAX_COUNTED_LINES = 20_000


def count_line_changes(previous: str, current: str) -> tuple[int, int] | None:
    """Return ``(lines_added, lines_removed)`` between two bodies.

    Line-based Myers diff; a ``replace`` block counts on both sides.
    ``None`` means "not counted" (guard tripped), never "no change".
    """
    old_lines = previous.splitlines()
    new_lines = current.splitlines()
    if max(len(old_lines), len(new_lines)) > MAX_COUNTED_LINES:
        log.warning(
            "Artifact line count skipped: %s/%s lines exceed the limit of %s; the "
            "change badge remains absent for this write.",
            len(old_lines),
            len(new_lines),
            MAX_COUNTED_LINES,
        )
        return None
    added = 0
    removed = 0
    # autojunk would misclassify popular lines (blank lines in long
    # documents) as junk and skew the counts — disable it.
    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added += new_end - new_start
        if tag in ("delete", "replace"):
            removed += old_end - old_start
    return added, removed
