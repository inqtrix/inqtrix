"""Paragraph-aware text chunking for the knowledge engine.

Deliberately simple: split on blank-line paragraph boundaries, pack
paragraphs greedily up to a character budget, and hard-split single
paragraphs that exceed the budget on sentence-ish boundaries. The chunker
is not layout-aware, and contextualization is a separate ingestion step
(:mod:`inqtrix.knowledge.contextualize`); the chunker stays a pure
function so swapping it never touches the stores or the algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHUNK_CHARS = 2_000
"""Default chunk budget (~500 tokens at 4 chars/token).

Comfortably below every catalogued embedding model's input limit while
keeping chunks topically coherent. Operators tune per deployment via
``KnowledgeSettings.chunk_max_chars``.
"""

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkSlice:
    """One source-exact chunk and its character span in the document.

    ``start``/``end`` use Python character offsets into the canonical document
    text passed to :func:`chunk_text_slices`.  Persistence converts them to
    UTF-8 byte offsets, which remain unambiguous at API boundaries.  Keeping
    the character offsets here lets contextualization slice the source without
    ever trying to rediscover a chunk through a text-prefix search.
    """

    text: str
    start: int
    end: int

    def utf8_span(self, document_text: str) -> tuple[int, int]:
        """Return the same span as UTF-8 byte offsets."""
        return (
            len(document_text[: self.start].encode("utf-8")),
            len(document_text[: self.end].encode("utf-8")),
        )


def chunk_text(text: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
    """Split *text* into retrieval chunks of at most *max_chars*.

    Args:
        text: The full document text. Surrounding whitespace is
            ignored; empty input yields no chunks.
        max_chars: Per-chunk character budget. Must be positive.

    Returns:
        Non-empty chunk strings in document order. Paragraphs are kept
        together where the budget allows; oversize paragraphs are
        split on sentence boundaries, and a single oversize sentence
        is hard-wrapped as a last resort (never dropped).

    Raises:
        ValueError: When *max_chars* is not positive — a silent
            zero-budget chunker would drop content.
    """
    return [item.text for item in chunk_text_slices(text, max_chars=max_chars)]


def chunk_text_slices(
    text: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS
) -> list[ChunkSlice]:
    """Split *text* while retaining exact, monotonic source spans.

    The historical chunker returned newly joined paragraph strings.  Those
    strings could not be mapped back safely when boilerplate repeated, because
    ingestion later searched for a short prefix.  This variant operates on
    offsets from the beginning and emits direct substrings.  Consequently
    ``document_text[slice.start:slice.end] == slice.text`` is an invariant for
    every result, including repeated and over-size content.

    The caller passes the canonical document text (the knowledge service strips
    surrounding whitespace once before calling this function).  Internal
    whitespace is deliberately preserved as source evidence.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    if not text:
        return []

    paragraph_spans: list[tuple[int, int]] = []
    cursor = 0
    for boundary in _PARAGRAPH_SPLIT.finditer(text):
        start, end = _trim_span(text, cursor, boundary.start())
        if start < end:
            paragraph_spans.append((start, end))
        cursor = boundary.end()
    start, end = _trim_span(text, cursor, len(text))
    if start < end:
        paragraph_spans.append((start, end))

    pieces: list[tuple[int, int]] = []
    for paragraph_start, paragraph_end in paragraph_spans:
        if paragraph_end - paragraph_start <= max_chars:
            pieces.append((paragraph_start, paragraph_end))
        else:
            pieces.extend(
                _split_oversize_paragraph_spans(
                    text,
                    paragraph_start,
                    paragraph_end,
                    max_chars,
                )
            )

    chunks: list[ChunkSlice] = []
    current_start: int | None = None
    current_end: int | None = None
    for piece_start, piece_end in pieces:
        if current_start is None:
            current_start, current_end = piece_start, piece_end
            continue
        assert current_end is not None
        if piece_end - current_start <= max_chars:
            current_end = piece_end
            continue
        chunks.append(
            ChunkSlice(
                text=text[current_start:current_end],
                start=current_start,
                end=current_end,
            )
        )
        current_start, current_end = piece_start, piece_end
    if current_start is not None and current_end is not None:
        chunks.append(
            ChunkSlice(
                text=text[current_start:current_end],
                start=current_start,
                end=current_end,
            )
        )
    return chunks


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Trim surrounding whitespace without losing the original offsets."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_oversize_paragraph_spans(
    text: str,
    paragraph_start: int,
    paragraph_end: int,
    max_chars: int,
) -> list[tuple[int, int]]:
    """Split one source paragraph into direct-substring spans."""
    paragraph = text[paragraph_start:paragraph_end]
    sentence_spans: list[tuple[int, int]] = []
    cursor = 0
    for boundary in _SENTENCE_BOUNDARY.finditer(paragraph):
        start, end = _trim_span(paragraph, cursor, boundary.start())
        if start < end:
            sentence_spans.append(
                (paragraph_start + start, paragraph_start + end)
            )
        cursor = boundary.end()
    start, end = _trim_span(paragraph, cursor, len(paragraph))
    if start < end:
        sentence_spans.append((paragraph_start + start, paragraph_start + end))

    pieces: list[tuple[int, int]] = []
    current_start: int | None = None
    current_end: int | None = None
    for sentence_start, sentence_end in sentence_spans:
        if sentence_end - sentence_start > max_chars:
            if current_start is not None and current_end is not None:
                pieces.append((current_start, current_end))
                current_start = current_end = None
            for offset in range(sentence_start, sentence_end, max_chars):
                pieces.append((offset, min(sentence_end, offset + max_chars)))
            continue
        if current_start is None:
            current_start, current_end = sentence_start, sentence_end
            continue
        if sentence_end - current_start <= max_chars:
            current_end = sentence_end
            continue
        assert current_end is not None
        pieces.append((current_start, current_end))
        current_start, current_end = sentence_start, sentence_end
    if current_start is not None and current_end is not None:
        pieces.append((current_start, current_end))
    return pieces
