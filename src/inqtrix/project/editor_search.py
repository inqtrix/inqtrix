"""Exact editor anchors over a whitespace-normalized search view."""

from __future__ import annotations

from typing import Any

EDITOR_SEARCH_MAX_MATCHES = 5
EDITOR_SEARCH_CONTEXT_CHARS = 80
EDITOR_SEARCH_QUERY_MAX = 300


def find_editor_anchors(
    content: str, query: str, *, offset: int = 0, limit: int = EDITOR_SEARCH_MAX_MATCHES
) -> tuple[list[dict[str, Any]], int]:
    """Whitespace-tolerant search mapped back to the ORIGINAL markdown.

    Mirrors the frontend anchor index (anchoring.ts): every whitespace
    run folds to one space for MATCHING, while a positions array maps
    each folded character back to its original offset — so a hit is
    returned as the EXACT original slice (a byte-true ``find`` candidate
    the server-side anchor resolver matches literally), plus original
    context windows as quote candidates. Case-sensitive by design, like
    both anchor resolvers. Returns (rendered matches, total count) —
    the caller names the not-rendered remainder visibly.
    """
    if not 0 <= offset <= len(content) or not 1 <= limit <= EDITOR_SEARCH_MAX_MATCHES:
        raise ValueError("Editor search range or match limit is invalid.")
    positions: list[int] = []
    folded_chars: list[str] = []
    for position, char in enumerate(content[offset:], start=offset):
        folded = " " if char.isspace() else char
        if folded == " " and (not folded_chars or folded_chars[-1] == " "):
            continue
        folded_chars.append(folded)
        positions.append(position)
    haystack = "".join(folded_chars)
    needle_chars: list[str] = []
    for char in query:
        folded = " " if char.isspace() else char
        if folded == " " and (not needle_chars or needle_chars[-1] == " "):
            continue
        needle_chars.append(folded)
    needle = "".join(needle_chars).rstrip()
    if not needle:
        return [], 0
    matches: list[dict[str, Any]] = []
    total = 0
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            break
        total += 1
        if len(matches) < limit:
            begin = positions[index]
            end = positions[index + len(needle) - 1] + 1
            matches.append(
                {
                    "find": content[begin:end],
                    "quote_before": content[
                        max(0, begin - EDITOR_SEARCH_CONTEXT_CHARS):begin
                    ],
                    "quote_after": content[
                        end:end + EDITOR_SEARCH_CONTEXT_CHARS
                    ],
                    "offset": begin,
                }
            )
        start = index + 1
    return matches, total

