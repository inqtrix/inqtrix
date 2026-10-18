"""The one grammar of inline evidence labels in agent-written text.

Writers cite evidence with labels in square brackets: ``[K2]`` for a
project-knowledge passage, ``[W7]`` for a web source, ``[E3]`` for a
delegated research run's evidence. One label per bracket is the form every
reader parses, bare or as link text (``[W7](https://...)``). A grouped
bracket (``[K2, W7]``, ``[K2; W7]``) is the same citation written
differently: :func:`canonicalize_citations` rewrites it into single brackets
separated by a space before the text is stored, and the readers here count
its members either way.

What cannot be rewritten without guessing is reported by
:func:`malformed_citations`: a range (``[K3–K5]``), a slash (``[K2/W7]``), a
prose join (``[K2 und W7]``), a label next to words or a note
(``[K2: S. 14]``), or a group used as link text. A range is never expanded,
since expanding ``[K3–K5]`` would invent ``K4``. A bracket that opens with a
word and a colon is a platform marker (``[unbelegt: K2]``) and is neither a
citation nor malformed; a backslash-escaped bracket (``\\[K2\\]``, what a
Markdown editor stores) is literal text.

The grammar leaves fenced and inline code and formulas byte-identical (the
Markdown pieces of :func:`inqtrix.agents.markdown.markdown_pieces`); URLs
are read like prose, since a link destination ends at ``)`` and a label may
follow it directly. A ``$$`` that never closes is no formula: read as one,
it would hide every later citation. Digits and separators are ASCII, the
same as the frontend's copy of this grammar, which a shared fixture
(``tests/fixtures/citation_label_parity.json``) holds in parity.

A revision binds labels to sentences with the same reading
(:func:`citation_segments`): a replacement splits into headings, sentences and
code blocks, a citation run after a sentence's final mark belongs to that
sentence, and a code span or formula is opaque. Whether a segment is new is
decided on the words of its normalized text (:func:`segment_norm`): without
citations, list or heading markers, emphasis, case, spacing and punctuation,
so a shortened, reordered, re-emphasized or re-punctuated sentence of the
quoted passage is not new, while a reworded one is. :func:`citation_spans` says where a text
cites, so a splice can be checked for citations it changed outside its own
passage. These are Python only; the frontend reads no revision.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from inqtrix.agents.markdown import markdown_pieces

AGENT_FAMILIES = "KW"
"""The labels of an agent run's own evidence register: K (project knowledge)
and W (web)."""

DELEGATED_FAMILIES = "KWE"
"""The agent labels plus E, the labels a delegated research run brings along."""

CITATION_FORMAT = (
    "Cite with one evidence label per bracket; several labels stand as "
    "separate brackets divided by a space ([K1] [W2]), never grouped in one "
    "bracket ([K1, W2] or [K1; W2]), never adjacent ([K1][W2]), never as "
    "a range ([K1–K3]) and with nothing but the label inside the bracket "
    "([K1], not [K1: p. 4])."
)
"""The one citation-format sentence every agent writer, reviser and repair
prompt carries."""


@lru_cache(maxsize=None)
def _grammar(families: str) -> tuple[re.Pattern[str], ...]:
    label = rf"[{re.escape(families)}][0-9]+"
    separator = r"[ \t]*[,;][ \t]*"
    citation = re.compile(rf"\[({label})\]")
    group = re.compile(
        rf"(?<!\\)\[[ \t]*({label}(?:{separator}{label})+)[ \t]*\](?!\()"
    )
    group_as_link = re.compile(
        rf"(?<!\\)\[[ \t]*{label}(?:{separator}{label})+[ \t]*\](?=\()"
    )
    token = re.compile(rf"(?<![A-Za-z0-9])({label})(?![A-Za-z0-9])")
    return citation, group, group_as_link, token, re.compile(separator)


_ANY_BRACKET = re.compile(r"(?<!\\)\[([^\[\]\n]{1,200})\](?!\()")
_CONTAINER_MARKERS = re.compile(r"(?:[ \t]*(?:>|[-*+]|[0-9]{1,9}[.)]|\[[ xX]\]))*[ \t]*")
"""What may stand before a link reference definition on its line: block
quote and list markers (a task checkbox included) and indentation."""
_MARKER = re.compile(r"[ \t]*[^\W\d_]+[ \t]*:")
"""A platform marker opens with a word and a colon: ``[unbelegt: K2]``."""


def citation_pattern(families: str = AGENT_FAMILIES) -> re.Pattern[str]:
    """The single-label citation bracket of *families*; group 1 is the label.

    For callers that rewrite labels in place. Reading which labels a text
    cites goes through :func:`cited_labels`, which also counts groups.
    """
    return _grammar(families)[0]


def _reading_pieces(text: str) -> list[tuple[bool, str]]:
    """*text* as ``(is_prose, piece)``: code and closed formulas apart, the
    rest joined into prose (URLs and stray dollar signs included)."""
    return [(kind == "prose", piece) for kind, piece in _reading_kinds(text)]


def _reading_kinds(text: str) -> list[tuple[str, str]]:
    """*text* as ``(kind, piece)``, the reading of :func:`_reading_pieces`
    with the kind kept: ``prose``, ``fence`` (one line of fenced code, its
    markers included) or ``code`` (a code span or a closed formula)."""
    pieces = markdown_pieces(text)
    kinds = [kind for kind, _piece in pieces]
    opened: int | None = None
    for index, (kind, piece) in enumerate(pieces):
        if kind != "math":
            continue
        body = piece.rstrip("\r\n")
        if opened is None:
            if body.startswith("$$") and not (len(body) >= 4 and body.endswith("$$")):
                opened = index
        elif body.endswith("$$"):
            opened = None
    if opened is not None:
        for index in range(opened, len(pieces)):
            if kinds[index] == "math":
                kinds[index] = "prose"
    reading: list[tuple[str, str]] = []
    for kind, (_kind, piece) in zip(kinds, pieces):
        kind = (
            "prose" if kind in ("prose", "url", "lone_dollar")
            else "fence" if kind == "fence"
            else "code"
        )
        if reading and kind == "prose" and reading[-1][0] == "prose":
            reading[-1] = ("prose", reading[-1][1] + piece)
        else:
            reading.append((kind, piece))
    return reading


def canonicalize_citations(text: str, families: str = AGENT_FAMILIES) -> str:
    """*text* with every grouped citation bracket written as single brackets.

    ``[K2, W7]`` and ``[K2; W7]`` become ``[K2] [W7]``; a label repeated in
    one group is kept once. Only brackets made of labels of *families*
    change, and only in prose; a group followed by ``(`` (link text) stays
    and is malformed instead. Idempotent.
    """
    if "[" not in text:
        return text
    _citation, group, _group_as_link, _token, separator = _grammar(families)

    def single(match: re.Match[str]) -> str:
        labels: list[str] = []
        for label in separator.split(match.group(1)):
            if label not in labels:
                labels.append(label)
        return " ".join(f"[{label}]" for label in labels)

    return "".join(
        group.sub(single, piece) if is_prose else piece
        for is_prose, piece in _reading_pieces(text)
    )


@lru_cache(maxsize=None)
def _escaped_citation(families: str) -> re.Pattern[str]:
    """A backslash-escaped citation bracket of *families*, single or grouped;
    group 1 is its inside. A ``\\[`` after another backslash is none."""
    label = rf"[{re.escape(families)}][0-9]+"
    return re.compile(
        rf"(?<!\\)\\\[([ \t]*{label}(?:[ \t]*[,;][ \t]*{label})*[ \t]*)\\\]"
    )


_LINK_TEXT = re.compile(r"(?<!\\)!?\[(?:[^\[\]\\\n]|\\.)*\]\(")
"""The bracketed text of an inline link or image up to its ``(``, with no raw
bracket inside: ``[siehe \\[K1\\]](`` or ``![Abb.](``."""
_BARE_URL = re.compile(r"https?://[^\s<>\"')]+")
"""A bare URL up to the ``)`` that may end a link destination: the URL of
:func:`inqtrix.agents.markdown.markdown_pieces` without what follows it."""


def unescape_citations(text: str, families: str = AGENT_FAMILIES) -> str:
    """*text* with its backslash-escaped citation brackets written raw.

    A Markdown editor may store ``[K1]`` as ``\\[K1\\]``, which every reader
    takes for literal text; a writer that carries it forward would lose the
    citation. Only brackets of labels of *families* change, and only where the
    grammar reads prose: never in code or closed formulas. Whether a span is a
    formula is decided as if the labels were raw already, since the
    backslashes of an escape would otherwise count as a formula sign. A
    bracket stays escaped where a raw one would change what the Markdown
    means: before ``(`` it would open a link, and followed by ``:`` with only
    block quote or list markers before it on its line it would open a link
    reference definition. It also stays in the text of an inline link or
    image, as the editor writes it there, and inside a bare URL. Escaped
    brackets written back to back become single brackets divided by a space,
    the form of :func:`canonicalize_citations`. Idempotent.
    """
    if "\\[" not in text:
        return text
    pattern = _escaped_citation(families)
    # *text* with every escaped label raw and padded to its length: an offset
    # in this reading is the same offset in *text*.
    raw_reading = pattern.sub(lambda match: f" [{match.group(1)}] ", text)
    kept = _kept_spans(text)
    kept_index = 0
    out: list[str] = []
    position = 0
    for kind, piece in _reading_kinds(raw_reading):
        piece_end = position + len(piece)
        cursor = position
        previous_end = -1
        matches = pattern.finditer(text, position, piece_end) if kind == "prose" else ()
        for match in matches:
            start = match.start()
            while kept_index < len(kept) and kept[kept_index][1] <= start:
                kept_index += 1
            if kept_index < len(kept) and kept[kept_index][0] < start:
                continue
            following = text[match.end():match.end() + 1]
            if following == "(" or (
                following == ":" and _only_container_markers_before(text, start)
            ):
                continue
            out.append(text[cursor:start])
            if start == previous_end:
                out.append(" ")
            out.append(f"[{match.group(1)}]")
            cursor = previous_end = match.end()
        out.append(text[cursor:piece_end])
        position = piece_end
    return "".join(out)


def _kept_spans(text: str) -> list[tuple[int, int]]:
    """The spans of *text* whose escaped labels stay escaped, merged and in
    text order: the text of each inline link or image and each bare URL."""
    spans = sorted(
        [(match.start(), match.end()) for match in _LINK_TEXT.finditer(text)]
        + [(match.start(), match.end()) for match in _BARE_URL.finditer(text)]
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _only_container_markers_before(text: str, index: int) -> bool:
    """Whether only container markers stand before *index* on its line. Looks
    back no further than that line, so unescaping stays linear in the text."""
    newline = text.rfind("\n", 0, index)
    line_start = max(newline, text.rfind("\r", newline + 1, index)) + 1
    return _CONTAINER_MARKERS.fullmatch(text, line_start, index) is not None


def cited_labels(text: str, families: str = AGENT_FAMILIES) -> list[str]:
    """The labels *text* cites, in first-citation order, each once.

    A single bracket counts bare and as link text; a group counts like its
    single brackets, so the answer does not depend on whether *text* was
    canonicalized yet. Labels in code and formulas do not count.
    """
    return list(dict.fromkeys(label for _start, _end, label in citation_spans(text, families)))


def citation_spans(text: str, families: str = AGENT_FAMILIES) -> list[tuple[int, int, str]]:
    """Where *text* cites, as ``(start, end, label)`` in text order.

    The reading of :func:`cited_labels`, on *text* as given rather than
    canonicalized, so the positions are *text*'s own: a single bracket bare
    or as link text (its bracket only), and every label of a group with the
    group's span. Nothing in code, in formulas or in an escaped bracket.
    """
    citation, group, _group_as_link, _token, separator = _grammar(families)
    spans: list[tuple[int, int, str]] = []
    position = 0
    for is_prose, piece in _reading_pieces(text):
        if is_prose:
            for match in citation.finditer(piece):
                if match.start() > 0 and piece[match.start() - 1] == "\\":
                    continue
                spans.append((position + match.start(), position + match.end(), match.group(1)))
            for match in group.finditer(piece):
                spans.extend(
                    (position + match.start(), position + match.end(), label)
                    for label in separator.split(match.group(1))
                )
        position += len(piece)
    # Stable on the start alone: a group's labels keep their written order.
    return sorted(spans, key=lambda span: span[0])


def _malformed_spans(piece: str, families: str) -> list[tuple[int, int]]:
    """Positions of the brackets in one prose *piece* no reader can parse."""
    citation, _group, group_as_link, token, _separator = _grammar(families)
    spans = [(match.start(), match.end()) for match in group_as_link.finditer(piece)]
    for match in _ANY_BRACKET.finditer(piece):
        inner = match.group(1)
        if citation.fullmatch(match.group(0)) or _MARKER.match(inner):
            continue
        if inner.endswith("\\") or token.search(inner) is None:
            continue
        spans.append((match.start(), match.end()))
    return sorted(spans)


def malformed_citations(text: str, families: str = AGENT_FAMILIES) -> list[str]:
    """Label-bearing brackets in *text* that no reader can parse, verbatim.

    A bracket counts when it holds a label of *families* but is neither a
    single citation nor a group of labels only: a range, a slash, a prose
    join, a label next to words or a note, and a group used as link text.
    Platform markers (a word and a colon first), escaped brackets and
    ordinary link text never count. In text order.
    """
    found: list[str] = []
    for is_prose, piece in _reading_pieces(canonicalize_citations(text, families)):
        if is_prose:
            found.extend(
                piece[start:end] for start, end in _malformed_spans(piece, families)
            )
    return found


def mark_malformed_citations(
    text: str, marker: str, families: str = AGENT_FAMILIES
) -> str:
    """*text* with each bracket :func:`malformed_citations` names written as
    ``[marker: inside]`` where it stands, in prose only; a range is marked,
    never expanded, and code or a formula spelling the same bracket stays."""
    out: list[str] = []
    for is_prose, piece in _reading_pieces(canonicalize_citations(text, families)):
        if not is_prose:
            out.append(piece)
            continue
        cursor = 0
        for start, end in _malformed_spans(piece, families):
            out.append(piece[cursor:start])
            out.append(f"[{marker}: {piece[start + 1:end - 1].strip()}]")
            cursor = end
        out.append(piece[cursor:])
    return "".join(out)


@dataclass(frozen=True)
class CitationSegment:
    """One unit a revision's citation rule reads (:func:`citation_segments`)."""

    kind: str
    """``heading``, ``sentence`` or ``code`` (a run of fenced lines)."""
    text: str
    """The segment as it stands, citations included, outer whitespace off."""
    labels: tuple[str, ...]
    """The labels it cites (:func:`cited_labels`); none for code."""


HEADING_LINE = re.compile(r"[ \t]{0,3}#{1,6}[ \t]")
"""An ATX heading line: a segment of its own (:func:`citation_segments`)."""
LIST_LINE = re.compile(r"[ \t]*(?:[-*+]|\d+[.)])[ \t]+|[ \t]*\|")
"""A list item or a table row: each starts a unit of its own."""
_LINE_MARKER = re.compile(r"^[ \t]*(?:#{1,6}|[-*+]|\d+[.)])[ \t]+")
_EMPHASIS = re.compile(r"\*\*|__|\*|_")
_NO_SENTENCE_END = frozenset({
    "abs", "art", "nr", "s", "ziff", "bzw", "ca", "vgl", "dr", "prof", "bspw",
    "ggf", "inkl", "usw", "etc", "gem", "lit", "rn", "vs", "p", "pp", "cf",
    "e.g", "i.e",
})
"""Abbreviations whose dot ends no sentence, compared without the dot."""
_SENTENCE_OPENERS = "„\"“«(*_#"
_RUN_GAP = re.compile(r"[ \t]*")
"""What may stand before each citation of a run."""
_OPAQUE = "\x00"
"""What code and formulas read as while sentences are found: no mark, no label."""


@lru_cache(maxsize=None)
def _citation_with_link(families: str) -> re.Pattern[str]:
    """One citation of *families*, bare or as link text with its destination."""
    return re.compile(rf"(?<!\\){_grammar(families)[0].pattern}(?:\([^)\s]*\))?")


def citation_run_end(text: str, position: int, families: str = AGENT_FAMILIES) -> int:
    """Where the citation run that starts at *position* ends; *position* when none does.

    The run of :func:`citation_segments`: citations that count
    (:func:`citation_spans`), bare or as link text, each after optional
    spaces or tabs. It belongs to the sentence on its left, so a passage
    that ends where a run starts (``Satz.`` before `` [K12]``) owns it.
    """
    counted = {start: end for start, end, _label in citation_spans(text, families)}
    single = _citation_with_link(families)
    end = position
    while True:
        start = _RUN_GAP.match(text, end).end()
        if start not in counted:
            return end
        linked = single.match(text, start)
        end = linked.end() if linked else counted[start]


@lru_cache(maxsize=None)
def _sentence_end(families: str) -> re.Pattern[str]:
    """A sentence's final mark, closing quotes, then an optional citation run."""
    single = _citation_with_link(families).pattern
    return re.compile(
        r"(?P<mark>[.!?…]+)(?P<close>[\"'”’“»)]*)"
        rf"(?P<run>(?:[ \t]*{single})+)?"
        r"(?=\s|$)"
    )


@dataclass(frozen=True)
class CitationUnit:
    """One unit of :func:`citation_units`."""

    kind: str
    """``heading``, ``code`` or ``prose`` (one paragraph, list item or table row)."""
    segments: tuple[CitationSegment, ...]
    """The heading or the code block itself, or the sentences of the prose."""


def citation_segments(
    text: str,
    families: str = AGENT_FAMILIES,
    *,
    at_line_start: bool = True,
    at_line_end: bool = True,
) -> list[CitationSegment]:
    """*text* as headings, sentences and code blocks, in text order.

    Read through the grammar's Markdown reading (canonicalized first): a run
    of fenced lines is one ``code`` segment; a code span or formula stays
    inside its sentence, where it ends nothing and cites nothing. An ATX
    heading line is a segment of its own, a list item or table row starts a
    unit, a blank line ends one, other lines continue it. Inside a unit a
    sentence ends after ``.!?…`` and closing quotes: always when a citation
    run follows, which then belongs to that sentence, and otherwise only
    before an uppercase letter, a digit or an opening mark, and not after a
    single letter, a number or a listed abbreviation.

    *at_line_start* says whether *text* will stand at the start of a line:
    a replacement spliced in mid-line continues the line before it, so its
    first line is neither a heading nor a list item there. *at_line_end*
    is the same at its end: the text after the splice continues its last
    line, which a heading there would take into itself.

    The segments of :func:`citation_units`, read as one list.
    """
    return [
        segment
        for unit in citation_units(
            text, families, at_line_start=at_line_start, at_line_end=at_line_end
        )
        for segment in unit.segments
    ]


def citation_units(
    text: str,
    families: str = AGENT_FAMILIES,
    *,
    at_line_start: bool = True,
    at_line_end: bool = True,
) -> list[CitationUnit]:
    """*text* as its units in text order: a heading, a run of fenced lines,
    or the sentences of one paragraph, list item or table row, read as
    :func:`citation_segments` reads them."""
    text = canonicalize_citations(text, families)
    masked: list[str] = []
    fence_starts: set[int] = set()
    position = 0
    for kind, piece in _reading_kinds(text):
        if kind == "fence":
            fence_starts.add(position)
        position += len(piece)
        masked.append(
            piece if kind == "prose"
            else "".join(char if char.isspace() else _OPAQUE for char in piece)
        )
    reading = "".join(masked)
    ending = _sentence_end(families)
    units: list[CitationUnit] = []

    def sentences(start: int, end: int) -> None:
        cuts: list[tuple[int, int]] = []
        for match in ending.finditer(reading, start, end):
            run = match.group("run")
            if not run:
                following = reading[match.end():end].lstrip()
                if not following or not (
                    following[0].isupper()
                    or following[0].isdigit()
                    or following[0] in _SENTENCE_OPENERS
                ):
                    continue
                tokens = reading[start:match.start("mark")].split()
                word = re.sub(r"^\W+", "", tokens[-1]).casefold() if tokens else ""
                if (
                    word in _NO_SENTENCE_END
                    or (len(word) == 1 and word.isalpha())
                    or word.isdigit()
                ):
                    continue
            cut = match.end("run") if run else match.end("close")
            cuts.append((start, cut))
            start = cut
        cuts.append((start, end))
        prose = tuple(
            CitationSegment("sentence", sentence, tuple(cited_labels(sentence, families)))
            for left, right in cuts
            if (sentence := text[left:right].strip())
        )
        if prose:
            units.append(CitationUnit("prose", prose))

    unit: list[int] = []
    code: list[int] = []

    def close_unit() -> None:
        if unit:
            sentences(unit[0], unit[1])
            unit.clear()

    def close_code() -> None:
        if code:
            units.append(CitationUnit("code", (CitationSegment("code", text[code[0]:code[1]], ()),)))
            code.clear()

    offset = 0
    for index, line in enumerate(reading.splitlines(keepends=True)):
        start, offset = offset, offset + len(line)
        content = line.rstrip("\r\n\x0b\x0c\x1c\x1d\x1e\x85")
        end = start + len(content)
        if start in fence_starts:
            close_unit()
            code[:] = [code[0] if code else start, end]
            continue
        close_code()
        if not content.strip():
            close_unit()
            continue
        continues = (index == 0 and not at_line_start) or (
            not at_line_end and content == line
        )
        if not continues and HEADING_LINE.match(content):
            close_unit()
            heading = text[start:end].strip()
            units.append(CitationUnit("heading", (CitationSegment(
                "heading", heading, tuple(cited_labels(heading, families))
            ),)))
            continue
        if not continues and LIST_LINE.match(content):
            close_unit()
        unit[:] = [unit[0] if unit else start, end]
    close_unit()
    close_code()
    return units


def without_citations(text: str, families: str = AGENT_FAMILIES) -> str:
    """*text* (canonicalized) with every citation :func:`cited_labels` would
    count replaced by a space, bare or as link text with its destination;
    code, formulas and escaped brackets stay as they are."""
    single = _citation_with_link(families)
    return "".join(
        single.sub(" ", piece) if kind == "prose" else piece
        for kind, piece in _reading_kinds(canonicalize_citations(text, families))
    )


def segment_norm(text: str, families: str = AGENT_FAMILIES) -> str:
    """*text* reduced to what a revision's sentence comparison reads.

    Every citation :func:`cited_labels` would count becomes a space (one in
    code stays), heading and list markers and emphasis go, lines join with
    one space; then NFC, case folding and spacing before punctuation.
    """
    prose = without_citations(text, families)
    joined = " ".join(_LINE_MARKER.sub("", line) for line in prose.splitlines())
    joined = unicodedata.normalize("NFC", _EMPHASIS.sub("", joined)).casefold()
    joined = re.sub(r"\s+", " ", joined)
    joined = re.sub(r"\s+([.,;:!?)])", r"\1", joined)
    return re.sub(r"\(\s+", "(", joined).strip()


def segment_within(
    segment: CitationSegment, text: str, families: str = AGENT_FAMILIES
) -> bool:
    """Whether *segment* stands in *text*, compared as normalized word sequences.

    Only words count, never punctuation or sentence boundaries: a shortened
    sentence stands in the sentence it was cut from, and two sentences of
    *text* joined with a semicolon (or one split in two) stand in *text* as
    well, since they carry no word it did not hold. A segment with no word
    left to compare stands everywhere.
    """
    core = segment_words(segment.text, families)
    return not core or f" {core} " in f" {segment_words(text, families)} "


def segment_words(text: str, families: str) -> str:
    """The words of *text* after :func:`segment_norm`, one space apart."""
    return " ".join(re.findall(r"\w+", segment_norm(text, families)))


def segment_is_new(
    segment: CitationSegment, quote: str, families: str = AGENT_FAMILIES
) -> bool:
    """Whether *segment* adds text the quoted passage did not hold.

    A reordering of the quote's sentences, a truncated sentence or an
    emphasis-only change is not new; a reworded sentence is.
    """
    return not segment_within(segment, quote, families)
