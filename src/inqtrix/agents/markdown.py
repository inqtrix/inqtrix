"""Deterministic Markdown hygiene for agent-generated content.

The web UI intentionally supports dollar-delimited KaTeX. A model-written
currency marker such as ``$1.5T`` can therefore pair with a later dollar sign
and turn an entire prose span into accidental inline math. This module escapes
only dollar delimiters that are not recognizable math, while leaving fenced
code, inline code, block math, and genuine inline formulas unchanged.
"""

from __future__ import annotations

import re

_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_URL_AT = re.compile(r"https?://[^\s<>\"']+")
_SIMPLE_MATH_ATOM = re.compile(
    r"(?:[A-Za-z\u0370-\u03ff][A-Za-z0-9_\u0370-\u03ff]*|"
    r"[+-]?(?:\d+(?:[.,]\d+)?))\Z"
)
_MATH_SIGNAL = re.compile(r"[\\{}_^=+*/<>]")
_SUBTRACTION_SIGNAL = re.compile(
    r"[A-Za-z0-9_)\]]\s*-\s*[A-Za-z0-9_(\[]"
)
_FUNCTION_MATH = re.compile(
    r"[A-Za-z\u0370-\u03ff][A-Za-z0-9_\u0370-\u03ff]*"
    r"\([^()\r\n]*\)\Z"
)
_VARIABLE_SEQUENCE = re.compile(
    r"[A-Za-z\u0370-\u03ff][A-Za-z0-9_\u0370-\u03ff]*"
    r"(?:\s*,\s*[A-Za-z\u0370-\u03ff]"
    r"[A-Za-z0-9_\u0370-\u03ff]*)+\Z"
)
_CODE_SPAN_ESCAPED_AMOUNT = re.compile(r"\\\$(?=\d)")
"""A model-escaped amount inside a code span (``\\$0,023``): the backslash
would render literally, and a code span needs no escaping."""
_CURRENCY_MAGNITUDE_PREFIX = re.compile(
    r"[+-]?\d[\d.,]*\s*(?:K|M|B|T|Mio\.?|Mrd\.?|Bio\.?|"
    r"Million|Billion|Trillion)\b",
    re.IGNORECASE,
)


def normalize_agent_markdown(markdown: str) -> str:
    """Prevent currency markers from becoming accidental KaTeX.

    Args:
        markdown: Model- or provider-generated Markdown. User-authored text
            should not be routed through this helper merely for display.

    Returns:
        Markdown with non-math dollar signs escaped as ``\\$``. Genuine
        ``$x$``/``$$...$$`` math and code spans are byte-preserved, with one
        exception: a model-escaped amount inside a code span (``\\$5``)
        loses its backslash, which would otherwise render. The operation is
        idempotent.
    """
    if "$" not in markdown:
        return markdown
    return "".join(
        r"\$"
        if kind == "lone_dollar"
        else _CODE_SPAN_ESCAPED_AMOUNT.sub("$", piece)
        if kind == "code_span"
        else piece
        for kind, piece in markdown_pieces(markdown)
    )


def normalize_writer_markdown(markdown: str) -> str:
    """The boundary every agent writer's text passes before it is stored.

    Escaped citation brackets a Markdown editor stored become raw ones
    (:func:`inqtrix.citation_labels.unescape_citations`), grouped citation
    brackets become single ones
    (:func:`inqtrix.citation_labels.canonicalize_citations`), then
    currency markers are escaped (:func:`normalize_agent_markdown`). Only
    for text an agent engine's writer produced: the knowledge engine's
    answer carries a quote protocol whose bytes grounding checks verbatim,
    and provider text is not a writer's.
    """
    from inqtrix.citation_labels import canonicalize_citations, unescape_citations

    return normalize_agent_markdown(canonicalize_citations(unescape_citations(markdown)))


def markdown_pieces(markdown: str) -> list[tuple[str, str]]:
    """*markdown* split into typed pieces whose concatenation is the input.

    Kinds: ``prose``; ``fence`` (a line of fenced code, its markers
    included); ``code_span``; ``math`` (``$$...$$`` blocks and inline
    ``$...$`` formulas, recognized as :func:`normalize_agent_markdown`
    recognizes them); ``url``; ``lone_dollar`` (a dollar sign that is no
    math delimiter). The one reading of Markdown structure for every pass
    that must leave code and formulas alone.
    """
    pieces: list[tuple[str, str]] = []
    fence_character = ""
    fence_width = 0
    block_math = False
    for line in markdown.splitlines(keepends=True):
        if fence_character:
            pieces.append(("fence", line))
            if _is_fence_close(line, fence_character, fence_width):
                fence_character = ""
                fence_width = 0
            continue

        opener = _FENCE_OPEN.match(line)
        if opener is not None:
            marker = opener.group(1)
            fence_character = marker[0]
            fence_width = len(marker)
            pieces.append(("fence", line))
            continue

        block_math = _split_non_fence_line(line, pieces, block_math=block_math)
    return pieces


def _is_fence_close(line: str, character: str, width: int) -> bool:
    stripped = line.lstrip(" \t")
    if len(line) - len(stripped) > 3:
        return False
    marker_width = len(stripped) - len(stripped.lstrip(character))
    remainder = stripped[marker_width:].strip()
    return marker_width >= width and not remainder


def _split_non_fence_line(
    line: str, pieces: list[tuple[str, str]], *, block_math: bool
) -> bool:
    """Append the typed pieces of one non-fence *line*; return block-math state."""
    prose: list[str] = []

    def emit(kind: str, text: str) -> None:
        if prose:
            pieces.append(("prose", "".join(prose)))
            prose.clear()
        pieces.append((kind, text))

    index = 0
    while index < len(line):
        if block_math:
            close = _find_unescaped(line, "$$", index)
            if close < 0:
                emit("math", line[index:])
                return True
            emit("math", line[index : close + 2])
            index = close + 2
            block_math = False
            continue

        if line[index] == "`":
            width = _run_width(line, index, "`")
            delimiter = "`" * width
            close = line.find(delimiter, index + width)
            if close < 0:
                # An unmatched backtick is literal Markdown, not a code span;
                # keep the delimiter but continue with the prose after it.
                prose.append(delimiter)
                index += width
                continue
            emit("code_span", line[index : close + width])
            index = close + width
            continue

        url_match = _URL_AT.match(line, index)
        if url_match is not None:
            # A dollar sign in a URL query is data, not a Markdown math
            # delimiter. Preserve the entire destination byte-for-byte;
            # Markdown parsers handle link destinations before KaTeX.
            emit("url", url_match.group(0))
            index = url_match.end()
            continue

        if line.startswith("$$", index) and not _is_escaped(line, index):
            close = _find_unescaped(line, "$$", index + 2)
            if close < 0:
                emit("math", line[index:])
                return True
            emit("math", line[index : close + 2])
            index = close + 2
            continue

        if line[index] != "$" or _is_escaped(line, index):
            prose.append(line[index])
            index += 1
            continue

        close = _find_inline_math_close(line, index + 1)
        if (
            close >= 0
            and not _closes_before_an_amount(line, close)
            and _looks_like_inline_math(line[index + 1 : close])
        ):
            emit("math", line[index : close + 1])
            index = close + 1
            continue

        emit("lone_dollar", "$")
        index += 1
    if prose:
        pieces.append(("prose", "".join(prose)))
    return block_math


def _closes_before_an_amount(line: str, close: int) -> bool:
    """Whether the candidate closing ``$`` opens another currency amount.

    ``5,120 × $0.023 = **$117.76**`` pairs two amounts into one span that
    looks like math (it contains ``=``); a closing dollar that is itself
    followed by digits is the start of the next amount, never the end of a
    formula (KaTeX would not end a formula in front of a digit either).
    """
    tail = line[close + 1 : close + 3]
    return bool(tail) and (tail[0].isdigit() or (tail[0] == "." and tail[1:2].isdigit()))


def _find_inline_math_close(line: str, start: int) -> int:
    index = start
    while index < len(line) and line[index] not in "\r\n":
        if (
            line[index] == "$"
            and not _is_escaped(line, index)
            and not line.startswith("$$", index)
        ):
            return index
        index += 1
    return -1


def _find_unescaped(text: str, needle: str, start: int) -> int:
    index = text.find(needle, start)
    while index >= 0:
        if not _is_escaped(text, index):
            return index
        index = text.find(needle, index + len(needle))
    return -1


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _run_width(text: str, start: int, character: str) -> int:
    cursor = start
    while cursor < len(text) and text[cursor] == character:
        cursor += 1
    return cursor - start


def _looks_like_inline_math(content: str) -> bool:
    value = content.strip()
    if not value or "\n" in value or "\r" in value:
        return False
    if len(value) > 160 and "\\" not in value:
        return False
    if len(value.split()) > 6 and "\\" not in value:
        return False
    if _CURRENCY_MAGNITUDE_PREFIX.match(value):
        return False
    if "**" in value or "__" in value:
        # Markdown emphasis never occurs inside a formula; it marks prose
        # such as ``$0.12** pro Monat; Formel $`` between an amount and a
        # genuine formula that follows later on the line.
        return False
    if _SIMPLE_MATH_ATOM.fullmatch(value):
        return True
    if _FUNCTION_MATH.fullmatch(value):
        return True
    if _VARIABLE_SEQUENCE.fullmatch(value):
        return True
    if _MATH_SIGNAL.search(value):
        return True
    return bool(_SUBTRACTION_SIGNAL.search(value))
