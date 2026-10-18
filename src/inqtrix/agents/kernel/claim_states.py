"""What each cited statement of a reviewed output rests on: one state per statement.

A statement (:func:`statements`) is a run of sentences up to the citation
group that closes it, inside one paragraph, list item or table row
(:func:`~inqtrix.citation_labels.citation_units`). Sentences after the last
citation of their unit belong to no statement: what an output says without
a source the review judges, and its findings mark it. A sentence with fewer
than :data:`STATEMENT_MIN_WORDS` content words
(:func:`~inqtrix.strategies._claim_values.content_words`) states nothing on
its own: after a statement of its unit it lends that statement its
citations, otherwise it joins the next one. A line naming a web address
outside a link is a reference entry, and headings and code state nothing.

Each statement has exactly one of :data:`STATES` (:func:`statement_states`),
in this precedence:

- ``insufficient`` (reason ``finding``) when a confirmed evidence finding
  no revision applied still stands on its passage;
- ``verified`` when the stored text of a source it cites states it, as far
  as words can tell (:func:`~inqtrix.strategies._claim_values.page_states_claim`),
  and no revision the re-check did not hold wrote it;
- ``source_text`` when a source it cites holds stored text that does not
  state it that way;
- ``attributed`` when its sources hold nothing but the search provider's
  context;
- ``insufficient`` (``metadata_only``) when its sources hold nothing but
  their metadata.

An ``attributed`` statement that repeats a value of a passage a finding
disputes (same kind, same value) carries that state too (reason
``restates``): a short answer repeating a figure of a marked table rests on
the same evidence. A year alone stands in too many statements to tell a
repetition, so it carries no mark. What a finding disputes is what its
passage states, not the rest of the statement the passage stands in and not
when it holds: a value the passage does not name, and the date that only
frames its percentage or amount (``scope`` of
:class:`~inqtrix.strategies._claim_values.ClaimValues`), mark nothing, while
a repetition may name a disputed value in either role.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from inqtrix.citation_labels import (
    DELEGATED_FAMILIES,
    CitationSegment,
    canonicalize_citations,
    citation_units,
    segment_within,
    without_citations,
)
from inqtrix.strategies._claim_values import claim_values, content_words, page_states_claim

log = logging.getLogger("inqtrix")

STATES: tuple[str, ...] = ("verified", "source_text", "attributed", "insufficient")
"""Every state a statement can have, in the order a summary counts them."""

MARK_REASONS: tuple[str, ...] = ("finding", "metadata_only", "restates")
"""Why a statement is insufficiently supported; each has the reader words
``claim_mark_<reason>`` (:mod:`inqtrix.i18n`)."""

STATEMENT_MIN_WORDS = 2
"""Content words from which a sentence states something of its own.
Measured on recorded answers: author-year groups ("(Anbieter, 2026)") hold
one or none, while the shortest claims ("Die Förderung beträgt 30 Prozent")
hold two."""

_AUTHOR_YEAR = re.compile(r"\([^()\n]{1,80}?,\s*(?:19|20)\d{2}[a-z]?\)")
_WEB_ADDRESS = re.compile(r"https?://\S+")
_REFERENCE_ADDRESS = re.compile(r"(?<!\]\()https?://")


@dataclass(frozen=True)
class Statement:
    """One statement of an output (:func:`statements`)."""

    text: str
    """The statement as the output holds it, citations included."""
    labels: tuple[str, ...]
    """The labels of the citation group that closes it."""
    start: int
    end: int
    """Where it stands in the (canonicalized) output."""


@dataclass(frozen=True)
class Material:
    """What the review holds of one cited source."""

    kind: str
    """``retained_source_excerpt``, ``provider_context`` or ``source_metadata_only``."""
    excerpt: str = ""
    """The stored source text; empty for every other kind."""


@dataclass(frozen=True)
class Standing:
    """A confirmed evidence finding no revision applied: its passage and its
    prose in reader words."""

    quote: str
    finding: str


@dataclass(frozen=True)
class StatementState:
    """The state of one statement; ``reason`` and ``finding`` only for an
    ``insufficient`` one."""

    statement: Statement
    state: str
    reason: str = ""
    finding: str = ""
    """The standing finding's prose (reason ``finding``)."""


def claim_text(text: str, families: str = DELEGATED_FAMILIES) -> str:
    """*text* as the claim it states: without its citations, author-year
    groups and web addresses, its whitespace collapsed."""
    plain = _WEB_ADDRESS.sub(" ", _AUTHOR_YEAR.sub(" ", without_citations(text, families)))
    return " ".join(plain.split())


def statements(text: str, families: str = DELEGATED_FAMILIES) -> list[Statement]:
    """The statements of *text* (canonicalized first), in text order."""
    text = canonicalize_citations(text, families)
    found: list[Statement] = []
    cursor = 0
    for unit in citation_units(text, families):
        closed: list[Statement] = []
        first = -1
        for segment in unit.segments:
            start = text.find(segment.text, cursor)
            if start < 0:
                # The segments are the text's own, in order; a miss is a
                # defect of the reading, and the segment stays uncounted.
                log.warning("A segment of an output was not found where its reading stood; it is not counted.")
                continue
            end = cursor = start + len(segment.text)
            if unit.kind != "prose":
                continue
            fragment = len(set(content_words(claim_text(segment.text, families)))) < STATEMENT_MIN_WORDS
            if fragment and closed and first < 0:
                if segment.labels:
                    last = closed[-1]
                    closed[-1] = Statement(
                        text[last.start:end],
                        tuple(dict.fromkeys((*last.labels, *segment.labels))),
                        last.start,
                        end,
                    )
                continue
            if first < 0:
                first = start
            if segment.labels:
                closed.append(Statement(text[first:end], segment.labels, first, end))
                first = -1
        found.extend(item for item in closed if not _REFERENCE_ADDRESS.search(item.text))
    return found


def statement_states(
    text: str,
    *,
    materials: Mapping[str, Material],
    standing: Sequence[Standing] = (),
    revised_from: str | None = None,
    families: str = DELEGATED_FAMILIES,
) -> list[StatementState]:
    """The state of every statement of *text* (:func:`statements`).

    *materials* maps each label of the output to what the review holds of
    its source; a label without an entry holds nothing. *standing* are the
    confirmed evidence findings no revision applied: each marks every
    statement its passage touches, wherever the text holds that passage, and
    the values of a passage that marks a statement are what a repetition
    restates. *revised_from* is the reviewed text when an edit of this
    output was applied and the re-check did not hold it: a statement that
    text did not hold is the revision's, and it is never ``verified``.
    """
    text = canonicalize_citations(text, families)
    passages = [
        (start, start + len(quote), item, _telling_values(claim_text(quote, families), scope=False))
        for item in standing
        if (quote := canonicalize_citations(item.quote, families))
        for start in _occurrences(text, quote)
    ]
    states: list[StatementState] = []
    disputed: set[tuple[str, str]] = set()
    for statement in statements(text, families):
        touching = [
            (item, values)
            for start, end, item, values in passages
            if start < statement.end and statement.start < end
        ]
        for _item, values in touching:
            disputed |= values
        states.append(
            StatementState(statement, "insufficient", "finding", touching[0][0].finding)
            if touching
            else _own_state(statement, materials, revised_from, families)
        )
    return [
        replace(state, state="insufficient", reason="restates")
        if state.state == "attributed"
        and disputed & _telling_values(claim_text(state.statement.text, families))
        else state
        for state in states
    ]


def _own_state(
    statement: Statement,
    materials: Mapping[str, Material],
    revised_from: str | None,
    families: str,
) -> StatementState:
    """The state *statement* has from its own citations."""
    held = [materials[label] for label in statement.labels if label in materials]
    excerpts = [
        item.excerpt for item in held
        if item.kind == "retained_source_excerpt" and item.excerpt.strip()
    ]
    if excerpts:
        written = revised_from is not None and not segment_within(
            CitationSegment("sentence", statement.text, ()), revised_from, families
        )
        claim = claim_text(statement.text, families)
        verified = not written and any(page_states_claim(claim, excerpt) for excerpt in excerpts)
        return StatementState(statement, "verified" if verified else "source_text")
    if any(item.kind == "provider_context" for item in held):
        return StatementState(statement, "attributed")
    return StatementState(statement, "insufficient", "metadata_only")


def _telling_values(plain: str, *, scope: bool = True) -> set[tuple[str, str]]:
    """The values of a claim text a reader can tell it by: every one but a
    year alone, which stands in too many sentences. Without *scope* the date
    that only says when a percentage or amount of the claim holds stays out."""
    values = claim_values(plain)
    return {
        (value.kind, value.value)
        for value in (*values.values, *(values.scope if scope else ()))
        if not (value.kind == "date" and len(value.value) == 4)
    }


def _occurrences(text: str, quote: str) -> list[int]:
    """Every start of *quote* in *text*, left to right, none overlapping."""
    found: list[int] = []
    start = text.find(quote)
    while start >= 0:
        found.append(start)
        start = text.find(quote, start + len(quote))
    return found
