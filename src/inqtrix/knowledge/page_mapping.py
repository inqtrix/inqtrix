"""Best-effort source-page mapping for knowledge chunks.

MarkItDown converts a PDF to one concatenated Markdown string with NO page
markers, and the chunker (:mod:`inqtrix.knowledge.chunking`) further strips
whitespace — so the source page of a chunk is lost by the time it is embedded.

This module recovers a best-effort 1-based PAGE NUMBER per chunk at ingest:

1. :func:`extract_pdf_page_texts` runs a lightweight second pass over the PDF
   bytes (pdfminer.six — the same engine MarkItDown uses under the hood) to get
   per-page text. PDFs only; any failure returns ``None`` (logged), never raises.
2. :func:`infer_chunk_pages` locates each chunk's text within the concatenated,
   whitespace/punctuation-normalized page text and maps its offset back to a
   page. A chunk that cannot be located maps to ``None`` — never a guess
   (No Silent Fallbacks).

This is deliberately PAGE-level, not bounding-box/quad level: it enables an
"open PDF at page N" jump and a soft page highlight. Exact span highlighting is
a separate, larger effort needing a layout-aware parser.

The second pdfminer pass roughly doubles a PDF's ingest parse cost; ingestion
runs in the background, so the trade for durable provenance is acceptable.
"""

from __future__ import annotations

import io
import logging
import re

log = logging.getLogger("inqtrix")

_PDF_MAGIC = b"%PDF"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MIN_OVERLAP_RATIO = 0.2
"""Minimum share of a chunk's word-trigrams that must appear on a page for it to
be mapped there. Below this the chunk maps to ``None`` rather than to a page it
only incidentally overlaps — a guard against guessing (No Silent Fallbacks)."""


def extract_pdf_page_texts(content: bytes) -> list[str] | None:
    """Per-page text of a PDF, or ``None`` for non-PDFs / on any failure.

    Best-effort and silent-failure-free: a corrupt or image-only PDF logs a
    warning and yields ``None`` (the document still ingests, just without page
    numbers) rather than breaking ingestion.
    """
    if not content.startswith(_PDF_MAGIC):
        return None
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer

        pages: list[str] = []
        for layout in extract_pages(io.BytesIO(content)):
            pages.append(
                "".join(
                    element.get_text()
                    for element in layout
                    if isinstance(element, LTTextContainer)
                )
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, visible, non-fatal
        log.warning(
            "PDF page extraction failed (error_type=%s); "
            "chunk page numbers unavailable",
            type(exc).__name__,
        )
        return None
    return pages or None


def _normalize(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to a single space, so
    Markdown formatting vs. raw PDF text differences do not break matching."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def _trigram_set(normalized: str) -> set[tuple[str, str, str]]:
    """Word-trigram set of normalized text. Trigrams tolerate the small
    insert/delete differences between a Markdown chunk and the raw page text
    (an exact substring match would not), while staying distinctive enough to
    separate pages. A fragment of fewer than 3 words yields a single short word
    tuple that cannot intersect a normal page's 3-grams, so it maps to None
    against any multi-word page (matching the No-Silent-Fallbacks intent — a
    too-short chunk is not guessed onto a page); the real paragraph chunker does
    not emit such fragments from prose."""
    words = normalized.split()
    if len(words) < 3:
        return {tuple(words)} if words else set()  # type: ignore[return-value]
    return {(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)}


def infer_chunk_pages(
    chunks: list[str], page_texts: list[str] | None
) -> list[int | None]:
    """Map each chunk to a best-effort 1-based source page by word-trigram
    overlap (the page sharing the most of the chunk's trigrams wins, ties
    resolve forward in document order).

    Args:
        chunks: Chunk texts in document order (use the PRE-contextualization
            source chunks — a synthetic prefix would not match the page text).
        page_texts: Per-page text in page order (from
            :func:`extract_pdf_page_texts`); ``None``/empty yields all-``None``.

    Returns:
        One ``int | None`` per chunk (same length, same order). ``None`` where
        the chunk overlaps no page beyond the minimum ratio (never guessed).
    """
    if not page_texts:
        return [None] * len(chunks)

    page_grams = [_trigram_set(_normalize(page)) for page in page_texts]
    result: list[int | None] = []
    cursor_page = 1  # forward bias: on a tie prefer the earliest page >= here
    unmapped = 0
    for chunk in chunks:
        grams = _trigram_set(_normalize(chunk))
        page: int | None = None
        if grams:
            best_score = 0
            best_page = 0
            for index, page_gram in enumerate(page_grams):
                score = len(grams & page_gram)
                page_no = index + 1
                # Strictly better wins; on a tie keep the earliest page that is
                # not behind the last match (monotonic forward progress).
                better = score > best_score or (
                    score == best_score
                    and score > 0
                    and best_page < cursor_page <= page_no
                )
                if better:
                    best_score = score
                    best_page = page_no
            if best_score >= max(1, int(len(grams) * _MIN_OVERLAP_RATIO)):
                page = best_page
                cursor_page = best_page
        if page is None:
            unmapped += 1
        result.append(page)

    if unmapped:
        log.warning(
            "Page mapping: %d of %d chunks could not be located in the PDF "
            "page text (stored without a page number)",
            unmapped,
            len(chunks),
        )
    return result
