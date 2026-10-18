"""Text processing utilities."""

from __future__ import annotations

import re
from typing import Iterator

STOPWORDS: set[str] = {
    "der", "die", "das", "ein", "eine", "und", "oder", "ist", "sind", "hat",
    "haben", "wird", "wurde", "werden", "von", "mit", "fuer", "auf", "aus",
    "bei", "nach", "ueber", "vor", "zu", "als", "wie", "auch", "nicht",
    "sich", "des", "dem", "den", "es", "im", "in", "an", "um", "noch",
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "has",
    "have", "had", "be", "been", "for", "from", "with", "on", "at", "by",
    "to", "of", "in", "it", "its", "this", "that", "which", "what", "how",
    "not", "but", "also", "about", "as", "can", "do", "does", "did",
}

NEGATION_TOKENS: set[str] = {
    "kein", "keine", "keinen", "keinem", "keiner", "nicht", "ohne",
    "no", "not", "never", "none", "without",
}

_UMLAUT_EXPAND_MAP = str.maketrans({
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
})


def tokenize(text: str) -> list[str]:
    """Simple tokenization for heuristics."""
    return re.findall(r"[a-zA-Z0-9äöüÄÖÜß]+", (text or "").lower())


def norm_match_token(token: str) -> str:
    """Normalize token for matching (lowercase + umlaut expansion)."""
    return (token or "").lower().translate(_UMLAUT_EXPAND_MAP)


def is_none_value(text: str) -> bool:
    """Check if an LLM output field is 'Keine' / 'None' or a variant."""
    t = text.strip().lower()
    if t in ("keine", "none", "keine.", "none.", ""):
        return True
    for prefix in ("keine ", "keine\u2014", "keine\u00a0", "none ", "none\u2014"):
        if t.startswith(prefix):
            return True
    return False


def iter_word_chunks(text: str) -> Iterator[str]:
    """Yield text in word-sized chunks while preserving original spacing."""
    for match in re.finditer(r"\S+\s*|\s+", text or ""):
        yield match.group(0)


def iter_bounded_chunks(text: str, max_bytes: int) -> Iterator[str]:
    """Yield *text* in chunks of at most *max_bytes* UTF-8 bytes on word boundaries.

    Joining the chunks gives back *text* exactly. Whole word chunks are
    grouped, so no chunk splits a word or a multi-byte character; a single
    word longer than *max_bytes* forms a chunk of its own.
    """
    pending: list[str] = []
    size = 0
    for word in iter_word_chunks(text):
        length = len(word.encode("utf-8"))
        if pending and size + length > max_bytes:
            yield "".join(pending)
            pending, size = [], 0
        pending.append(word)
        size += length
    if pending:
        yield "".join(pending)


LOG_CUT_MARKER = " ..."
"""Closes a text :func:`shorten_visibly` cut that is no source passage: a
log excerpt, an audit preview, a diagnostic, a planner summary, or a title
or address in the evidence register, which carries no statement of its
own."""

PASSAGE_CUT_MARKER = " [shortened here; the passage continues]"
"""Closes a source passage a model reads shortened (an evidence register
line, a discovery excerpt), so the reader never takes the cut passage for
the whole one."""

_NUMBER_JOINER = "[.,:/'’\\-–]"
"""Joins two digit runs into one number when it stands between them with no
space: a decimal or thousands point, comma or apostrophe, a date, time or
fraction separator, a hyphen or an en dash."""

_GROUP_SPACE = "[ \u00a0\u2009\u202f]"
"""The space inside a digit-grouped number: a plain, no-break, thin or
narrow no-break space. A tab or a line break separates table cells
instead."""

_MAX_NUMBER_GROUPS = 6
"""Most digit groups one number has: ``1 500 000 000 000,50`` has six. A
longer chain is a table row or a list."""

_DIGIT_GROUP = re.compile(r"\d+")
_JOINED_DIGITS = re.compile(rf"\d+(?:{_NUMBER_JOINER}\d+)*")
_SPACE_GROUPED = re.compile(
    rf"(?<!\d)(?<!\d{_NUMBER_JOINER})\d{{1,3}}(?:{_GROUP_SPACE}\d{{3}})+(?!\d)"
)


def number_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every number in *text*, in order.

    A number is what a reader takes as one value, so a cut inside it would
    leave a different value:

    - digit runs joined by a separator with no space between them
      (:data:`_NUMBER_JOINER`): ``1.500.000,50``, ``1'500``, ``26.09.2026``,
      ``2026-09-26``, ``12:30``, ``2023/24``, ``10–20``;
    - a lead group of one to three digits followed by groups of exactly
      three digits, each after one plain, no-break, thin or narrow no-break
      space: ``1 500 000``;
    - both at once: ``1 500 000,50``.

    What stays apart:

    - a group of other than three digits after a space: ``Artikel 5 2019``
      holds two numbers, and ``2023 100 000`` holds the year and
      ``100 000``;
    - a chain of more than :data:`_MAX_NUMBER_GROUPS` digit groups, however
      joined: a table row (``100 107 114 121 128 135 142``) or a list
      (``1,2,3,4,5,6,7``) holds one number per group, so every space in the
      row stays a word boundary;
    - digits across a tab or a line break (table cells).
    """
    candidates = [match.span() for match in _JOINED_DIGITS.finditer(text)]
    candidates += [match.span() for match in _SPACE_GROUPED.finditer(text)]
    candidates.sort()
    chains: list[list[int]] = []
    for start, end in candidates:
        if chains and start < chains[-1][1]:
            chains[-1][1] = max(chains[-1][1], end)
        else:
            chains.append([start, end])
    spans: list[tuple[int, int]] = []
    for start, end in chains:
        groups = [match.span() for match in _DIGIT_GROUP.finditer(text, start, end)]
        if len(groups) > _MAX_NUMBER_GROUPS:
            spans.extend(groups)
        else:
            spans.append((start, end))
    return spans


def _is_word_boundary(
    text: str, position: int, numbers: list[tuple[int, int]]
) -> bool:
    """Whether *text* may be cut at *position*: whitespace outside every
    number of *numbers* (:func:`number_spans` of *text*)."""
    return text[position].isspace() and not any(
        start < position < end for start, end in numbers
    )


def _out_of_number(
    position: int, numbers: list[tuple[int, int]], *, forward: bool
) -> int:
    """*position*, moved out of the number it falls strictly inside: back to
    the number's start, or forward to its end."""
    for start, end in numbers:
        if start < position < end:
            return end if forward else start
    return position


def shorten_visibly(text: object, limit: int, *, marker: str) -> str:
    """*text* on one line, at most *limit* characters, visibly shortened.

    Whitespace collapses to single spaces, so the text stays one line and a
    line break inside it cannot start a line of its own. A text within
    *limit* returns whole. A longer one is cut at the last word boundary
    that leaves room for *marker*, and *marker* follows; the result, the
    marker included, is never longer than *limit*.

    A cut never splits a number (:func:`number_spans`): a space inside a
    digit-grouped number (``1 500``) is no word boundary, and when a single
    overlong word leaves no boundary in the second half of the room, the cut
    falls inside that word but moves back to before any number it would
    split.

    Raises:
        ValueError: *limit* leaves no room for *marker*.
    """
    if limit <= len(marker):
        raise ValueError(
            f"limit {limit} leaves no room for the {len(marker)}-character cut marker"
        )
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    room = limit - len(marker)
    numbers = [span for span in number_spans(normalized) if span[0] < room]
    cut = next(
        (
            position
            for position in range(room, 0, -1)
            if _is_word_boundary(normalized, position, numbers)
        ),
        0,
    )
    if cut < room // 2:
        cut = max(cut, _out_of_number(room, numbers, forward=False))
    kept = normalized[:cut].rstrip()
    return f"{kept}{marker}" if kept else marker.lstrip()


def start_at_word(text: str, position: int, *, reach: int) -> int:
    """Where a passage taken from *position* onward starts on a whole word.

    The mirror of the cut in :func:`shorten_visibly`: the start moves
    forward past the first word boundary at or after ``position - 1``
    (whitespace outside every number, :func:`number_spans`) and the
    whitespace after it, so the passage never opens inside a word or a
    number. When no boundary lies within *reach* characters, a single
    overlong word straddles *position*; the start then stays inside that
    word but moves forward past any number it would split. A *position* of
    0 or less returns 0.
    """
    if position <= 0:
        return 0
    position = min(position, len(text))
    last = min(len(text), position + reach)
    numbers = [
        (start, end)
        for start, end in number_spans(text)
        if end >= position - 1 and start < last
    ]
    start = next(
        (
            index + 1
            for index in range(position - 1, last)
            if _is_word_boundary(text, index, numbers)
        ),
        None,
    )
    if start is None:
        start = _out_of_number(position, numbers, forward=True)
    while start < len(text) and text[start].isspace():
        start += 1
    return start
