"""Quote-then-answer grounding: deterministic verbatim-quote checks.

The answer prompt (``build_knowledge_answer_prompt(grounding=True)``)
requires the model to emit a ``QUOTES:`` block of verbatim,
``[K#]``-labelled quotes BEFORE the ``ANSWER:`` section. Writing the
quotes first conditions the subsequent answer on literal evidence
(WebGPT/GopherCite lineage — the cheapest proven grounding step), and
because the quotes claim to be verbatim they can be validated WITHOUT
another LLM call: a substring check against the exact evidence entries
the model saw, tolerant only to formatting (whitespace, Unicode/typography
and case) so a genuinely verbatim quote is not failed by curly quotes,
ligatures or line wrapping — never to paraphrase.

Failure policy (No Silent Fallbacks): when grounding is enabled, only a
fully parsed response whose every labelled quote verifies is publishable.
Malformed output and unverified quotes produce a typed rejection.  A single,
deterministic format repair may accept Markdown heading adornment around the
two required section labels; it never invents a quote, changes evidence text,
or calls another model.

Page-break artifacts: PDF extraction (pdfminer) writes the printed page
number and a form feed INTO sentences that span a page break
(``…environments\n\n11\n\n\x0c in which…``).  A model quoting such a
sentence naturally omits the page number and would fail the byte-strict
check even though the quote is faithful.  Every quote is therefore checked
against two surfaces of its assigned evidence entry: the raw text, and the
text with ``\x0c``-anchored page-break sequences removed
(:func:`strip_page_break_artifacts`).  The anchor keeps the tolerance
content-safe — a number without a form feed (an article number, a year,
a list item) is never touched, and quotes that DO include the page number
still verify against the raw surface.

Numbers are content, never formatting: apart from that anchored page-number
artifact, no fold changes the digits a reader sees.  A hyphen or dash next to
a digit stays (a range "3-5" never reads as "35"; a line break after it only
closes), and superscript, subscript, fraction and other number forms are
compared as written (the exponent in "10²" never reads as "102").
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

GROUNDING_MARKER_PARSED = "_knowledge_grounding_parsed"
GROUNDING_MARKER_FALLBACK = "_knowledge_grounding_fallback"
GROUNDING_MARKER_FORMAT_REPAIRED = "_knowledge_grounding_format_repaired"

_STRICT_QUOTE_HEADER = re.compile(
    r"^[ \t]*(?:\*\*)?QUOTES:(?:\*\*)?[ \t]*$"
)
_STRICT_ANSWER_HEADER = re.compile(
    r"^[ \t]*(?:\*\*)?ANSWER:(?:\*\*)?[ \t]*$"
)
_REPAIRED_QUOTE_HEADER = re.compile(
    r"^[ \t]*#{1,6}[ \t]+(?:\*\*)?QUOTES:(?:\*\*)?[ \t]*$"
)
_REPAIRED_ANSWER_HEADER = re.compile(
    r"^[ \t]*#{1,6}[ \t]+(?:\*\*)?ANSWER:(?:\*\*)?[ \t]*$"
)
_QUOTE_LINE = re.compile(
    r"^[ \t]*\[K(\d+)\][ \t]*(.+?)[ \t]*$"
)
_QUOTE_CHARS = "\"'„“”»«‚‘’"

# Typographic variants the model or a PDF text-layer may emit for the SAME
# character: smart quotes -> ASCII quotes, dashes -> hyphen, zero-width
# characters dropped. Folded so a genuinely verbatim quote still verifies even
# when the source PDF used curly quotes or an em dash. Deliberately limited to
# encoding/typography (never paraphrase).
_TYPOGRAPHY = str.maketrans(
    {
        "„": '"', "“": '"', "”": '"', "»": '"', "«": '"',
        "‚": "'", "‘": "'", "’": "'",
        "–": "-", "—": "-", "―": "-",
        "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
    }
)


@dataclass(frozen=True)
class QuoteCheck:
    """One quote from the model's ``QUOTES:`` block, with its verdict.

    Attributes:
        label: The evidence label the model attributed the quote to,
            e.g. ``"K1"`` — 1-based position in the evidence block.
        text: The quote text with surrounding quotation marks removed.
        verified: ``True`` when the quote occurs verbatim in the
            evidence entry the label points at, tolerant only to
            formatting (whitespace, Unicode/typography, case);
            ``False`` for paraphrases, ellipses, and out-of-range
            labels alike.
        artifact_tolerated: ``True`` when the quote verified ONLY through
            a tolerated extraction artifact — the page-break-artifact-free
            surface or a word broken at a line end — never via the raw
            evidence text: the audit trail of the bounded tolerance.
    """

    label: str
    text: str
    verified: bool
    artifact_tolerated: bool = False


class GroundingStatus(StrEnum):
    """Terminal decision of the deterministic Knowledge grounding gate."""

    VERIFIED = "verified"
    REJECTED_FORMAT = "rejected_format"
    REJECTED_QUOTE = "rejected_quote"


class GroundingFailureCode(StrEnum):
    """Stable failure vocabulary shared with native runs and Agent tasks."""

    FORMAT_INVALID = "knowledge_grounding_format_invalid"
    QUOTE_UNVERIFIED = "knowledge_grounding_quote_unverified"


def grounding_failure_message(
    code: GroundingFailureCode, *, language: str
) -> str:
    """Return the safe visible explanation for a rejected grounded answer."""

    german = str(language or "").lower().startswith("de")
    if code is GroundingFailureCode.QUOTE_UNVERIFIED:
        return (
            "Die Antwort wurde nicht veröffentlicht, weil mindestens ein "
            "als wörtlich ausgewiesenes Zitat nicht im zugeordneten "
            "Originalbeleg nachweisbar war."
            if german
            else "The answer was not published because at least one quote "
            "presented as verbatim could not be verified in its assigned "
            "original evidence."
        )
    return (
        "Die Antwort wurde nicht veröffentlicht, weil der erforderliche "
        "Belegblock nicht sicher gelesen und geprüft werden konnte."
        if german
        else "The answer was not published because the required evidence "
        "block could not be parsed and verified safely."
    )


@dataclass(frozen=True)
class GroundingReport:
    """Outcome of parsing and verifying one grounded answer.

    Attributes:
        answer: Publishable user-facing answer with the quote block stripped.
            Empty for every rejected result so callers cannot accidentally
            expose the unchecked model completion.
        quotes: Every parsed quote with its verification verdict —
            empty when the output shape cannot be parsed.
        marker: Stable audit marker for strict parse, deterministic format
            repair, or format rejection.
        status: Typed terminal decision.  Only ``VERIFIED`` is publishable.
        failure_code: Stable run/task error type for rejected results.
        format_repaired: Whether the one bounded Markdown-heading repair was
            required.  No repair changes quote or answer content.
    """

    answer: str
    quotes: list[QuoteCheck]
    marker: str
    status: GroundingStatus
    failure_code: GroundingFailureCode | None = None
    format_repaired: bool = False

    @property
    def publishable(self) -> bool:
        """Whether this report may become a normal Knowledge answer."""

        return self.status is GroundingStatus.VERIFIED


@dataclass(frozen=True)
class _ParsedGroundingShape:
    """Internal parse result before label-bound quote verification."""

    answer: str
    quotes: list[tuple[int, str]]
    format_repaired: bool


# Compatibility folding (NFKC) lets a verbatim quote survive how a text layer
# ENCODES a character: ligatures, full-width letters, no-break spaces,
# decomposed accents. Applied to a number character the same fold changes
# what the text SAYS: a superscript joins the digits before it (the exponent
# in "10²" reads "102", the footnote marker in "Article 5³" reads "53") and a
# fraction merges with them ("1½" reads "11⁄2"). A character is therefore
# folded only when neither it nor its fold contains a number character;
# every number form is compared exactly as written.
_NON_ASCII = re.compile(r"[^\x00-\x7f]+")


@functools.cache
def _compatibility_decomposition(char: str) -> str:
    """NFKD of one character, or the character itself when that fold would
    touch a number character."""
    decomposed = unicodedata.normalize("NFKD", char)
    if decomposed != char and (
        char.isnumeric() or any(part.isnumeric() for part in decomposed)
    ):
        return char
    return decomposed


def _fold_compatibility(text: str) -> str:
    """NFKC of *text*, with number forms kept as written.

    NFKC is compatibility decomposition followed by canonical composition,
    so decomposing character by character and composing once (NFC) is
    exactly NFKC wherever no number form is kept. ASCII never decomposes,
    and text already in NFKC holds no character to fold.
    """
    if unicodedata.is_normalized("NFKC", text):
        return text
    decomposed = _NON_ASCII.sub(
        lambda run: "".join(map(_compatibility_decomposition, run.group())),
        text,
    )
    return unicodedata.normalize("NFC", decomposed)


def _normalize(text: str) -> str:
    """Fold typographic/encoding differences so a verbatim quote still matches.

    The check stays a verbatim-substring match — only formatting is tolerated,
    never paraphrase. Compatibility folding (:func:`_fold_compatibility`)
    covers ligatures, full-width and compatibility forms and maps no-break
    spaces to plain spaces, but never touches a number character; smart
    quotes and dashes map to ASCII and zero-width characters drop
    (:data:`_TYPOGRAPHY`); whitespace runs collapse (line wrapping); case
    folds. A reworded quote still differs in its words and fails — no false
    ``verified``.
    """
    folded = _fold_compatibility(text).translate(_TYPOGRAPHY)
    return " ".join(folded.split()).casefold()


# A page break as pdfminer renders it: the printed page number framed by
# BLANK lines directly before the form feed (the corpus-evidenced shape),
# or a bare form feed. The form feed anchors the artifact; the blank-line
# frame keeps digits that merely end a wrapped text line (amounts, years,
# column values — single newline, no frame) out of reach, and a number
# AFTER the form feed is never touched at all. Anything the frame misses
# leaves the quote unverified and falls to the visible regeneration.
_PAGE_BREAK_ARTIFACT = re.compile(
    r"\n[ \t]*\n[ \t]*\d{1,4}[ \t]*\n[ \t]*\n\s*\x0c"
    r"|\n?[ \t]*\x0c"
)


def strip_page_break_artifacts(text: str) -> str:
    """Remove ``\\x0c``-anchored page-break sequences from evidence text.

    Idempotent and content-safe: only sequences containing a form feed are
    touched, and each is replaced by a newline so surrounding sentences
    join exactly as a reader joins them across a page break.
    """
    if "\x0c" not in text:
        return text
    return _PAGE_BREAK_ARTIFACT.sub("\n", text)


# A word broken at a line end as PDF extraction renders it: a hyphen followed
# by whitespace between two word characters ("high- risk", "clas- sified").
# A reader joins the halves; a faithful quote writes "high-risk" or
# "classified". Comparing both sides with every hyphen between two letters
# removed makes those readings equal without loosening anything else:
# differing words still differ. Hyphenation never splits a number, so a
# hyphen next to a number character is always a real one ("3-5",
# "2024-05-12", "COVID-19"): it stays, and only a line break after it
# closes — a range never reads as the number its digits would form.
_WORD_HYPHEN = re.compile(r"(?<=(\w))-\s*(?=(\w))")


def _join_word_hyphen(match: re.Match[str]) -> str:
    """Drop a hyphen between two letters; keep one next to a number."""
    before, after = match.group(1), match.group(2)
    return "-" if before.isnumeric() or after.isnumeric() else ""


def dehyphenate(text: str) -> str:
    """*text* with every hyphen (and the whitespace after it) between two
    word characters other than numbers removed, and a hyphen next to a
    number character kept with only the whitespace after it closed — the
    surface on which a line-break-hyphenated source and a faithful quote
    of it agree."""
    return _WORD_HYPHEN.sub(_join_word_hyphen, text)


def _quote_surfaces(evidence: str) -> tuple[str, ...]:
    """Both normalized surfaces one evidence entry offers to quotes.

    The raw surface keeps today's byte-strict behaviour (a quote that
    includes the printed page number still verifies); the artifact-free
    surface accepts the human reading of a sentence that spans a page
    break. Identical surfaces collapse to one. Line-break hyphenation is
    compared on both sides in the :func:`quote_key` form.
    """
    raw = _normalize(evidence)
    stripped_source = strip_page_break_artifacts(evidence)
    if stripped_source == evidence:
        return (raw,)
    stripped = _normalize(stripped_source)
    return (raw,) if stripped == raw else (raw, stripped)


def quote_key(text: str) -> str:
    """The identity two quotes share when they are the same passage.

    Folded by THE normalizer chain (compatibility forms, typography,
    whitespace, case) and then dehyphenated — the one form in which a quote
    and an evidence surface are compared. A passage quoted twice (once per
    overlapping window, once with a line-break hyphen or another dash) is
    recognised as one, while two different numbers never share a key.
    """
    return dehyphenate(_normalize(text))


def quote_is_verbatim(text: str, evidence_texts: list[str]) -> bool:
    """Whether *text* appears verbatim in ANY of the evidence texts.

    The agent-side quote check (``agents/report_quality.verify_quotes``)
    reuses the ONE normalizer chain through this helper instead of the
    ``check_grounding`` answer-block contract — that parser is bound to
    per-label indices and the ``ANSWER:`` scaffold, neither of which
    exists for memo quotes checked against the whole evidence ledger.

    Args:
        text: The quoted passage (without surrounding quote marks).
        evidence_texts: Stored source texts captured at retrieval time
            (internal chunks and web excerpts alike).

    Returns:
        ``True`` when the normalizer-folded quote is a substring of at
        least one evidence surface (raw or page-break-artifact-free).
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    dehyphenated = quote_key(text)
    return any(
        normalized in surface or dehyphenated in dehyphenate(surface)
        for evidence in evidence_texts
        for surface in _quote_surfaces(evidence)
    )


def check_grounding(
    content: str, evidence_texts: list[str]
) -> GroundingReport:
    """Parse the quote block and verify each quote against its evidence.

    Args:
        content: Raw model output, expected to contain a ``QUOTES:``
            block followed by an ``ANSWER:`` section.
        evidence_texts: The chunks' SOURCE texts in label order
            (index 0 is ``K1``) — what the cited document actually
            contains, deliberately WITHOUT contextualization prefixes
            or rendering scaffolding.

    Returns:
        A :class:`GroundingReport`.  Missing/ambiguous headers, malformed quote
        lines, an empty quote block, or an empty answer are typed format
        rejections with no publishable answer.  A fully parsed response is
        still rejected unless every quote is verbatim in the evidence entry
        named by its label.
    """
    shape = _parse_grounding_shape(content, allow_heading_repair=False)
    if shape is None:
        # The sole repair is syntactic and bounded: Markdown heading markers
        # may adorn BOTH required headers.  Missing headers, invented section
        # names, prose in the quote block, or malformed quote lines remain a
        # rejection; no second model call and no fuzzy parser are allowed.
        shape = _parse_grounding_shape(content, allow_heading_repair=True)
    if shape is None:
        return GroundingReport(
            answer="",
            quotes=[],
            marker=GROUNDING_MARKER_FALLBACK,
            status=GroundingStatus.REJECTED_FORMAT,
            failure_code=GroundingFailureCode.FORMAT_INVALID,
        )

    surfaces = [_quote_surfaces(evidence) for evidence in evidence_texts]
    quotes: list[QuoteCheck] = []
    for index, text in shape.quotes:
        normalized = _normalize(text)
        # A quote that normalizes to nothing (zero-width characters only)
        # proves nothing — '' is a substring of every surface.
        in_range = bool(normalized) and 1 <= index <= len(evidence_texts)
        raw_verified = in_range and normalized in surfaces[index - 1][0]
        # Tolerated artifacts: a page break inside the sentence (the
        # artifact-free surfaces) and a word broken at a line end (both
        # sides compared in the quote_key form).
        dehyphenated = quote_key(text)
        verified = raw_verified or (
            in_range
            and any(
                normalized in surface or dehyphenated in dehyphenate(surface)
                for surface in surfaces[index - 1]
            )
        )
        quotes.append(
            QuoteCheck(
                label=f"K{index}",
                text=text,
                verified=verified,
                artifact_tolerated=verified and not raw_verified,
            )
        )
    if any(not quote.verified for quote in quotes):
        return GroundingReport(
            answer="",
            quotes=quotes,
            marker=(
                GROUNDING_MARKER_FORMAT_REPAIRED
                if shape.format_repaired
                else GROUNDING_MARKER_PARSED
            ),
            status=GroundingStatus.REJECTED_QUOTE,
            failure_code=GroundingFailureCode.QUOTE_UNVERIFIED,
            format_repaired=shape.format_repaired,
        )
    return GroundingReport(
        answer=shape.answer,
        quotes=quotes,
        marker=(
            GROUNDING_MARKER_FORMAT_REPAIRED
            if shape.format_repaired
            else GROUNDING_MARKER_PARSED
        ),
        status=GroundingStatus.VERIFIED,
        format_repaired=shape.format_repaired,
    )


def _parse_grounding_shape(
    content: str, *, allow_heading_repair: bool
) -> _ParsedGroundingShape | None:
    """Parse exactly one quote section and one answer section.

    The repair path differs only in accepting a Markdown heading prefix on
    both required headers.  All other grammar stays identical and fail-closed.
    """

    lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    quote_header = (
        _REPAIRED_QUOTE_HEADER if allow_heading_repair else _STRICT_QUOTE_HEADER
    )
    answer_header = (
        _REPAIRED_ANSWER_HEADER if allow_heading_repair else _STRICT_ANSWER_HEADER
    )
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty or quote_header.fullmatch(lines[nonempty[0]]) is None:
        return None
    quote_index = nonempty[0]
    answer_indexes = [
        index
        for index in range(quote_index + 1, len(lines))
        if answer_header.fullmatch(lines[index]) is not None
    ]
    if len(answer_indexes) != 1:
        return None
    answer_index = answer_indexes[0]

    parsed_quotes: list[tuple[int, str]] = []
    for line in lines[quote_index + 1 : answer_index]:
        if not line.strip():
            continue
        match = _QUOTE_LINE.fullmatch(line)
        if match is None:
            return None
        text = match.group(2).strip().strip(_QUOTE_CHARS).strip()
        if not text:
            return None
        parsed_quotes.append((int(match.group(1)), text))
    answer = "\n".join(lines[answer_index + 1 :]).strip()
    if not parsed_quotes or not answer:
        return None
    return _ParsedGroundingShape(
        answer=answer,
        quotes=parsed_quotes,
        format_repaired=allow_heading_repair,
    )
