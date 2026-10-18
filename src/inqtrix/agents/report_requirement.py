"""Composition of the result requirement set at submit time or at a plan gate.

The requirement can come from two places: prompt-library rules the user
attached, and the free text they typed. Both answer the same question —
what the result has to look like — so they travel as ONE value in one
state key and render in one prompt section.

Composition happens on the server when the requirement is set, and the
composed text is what gets stored: the run is a contract, so editing a
template afterwards must not silently rewrite a requirement a run is
already working under. The library entry contributes its text, not a
live link.

Every contribution carries an origin marker. Without them a review
finding of kind ``output_requirement`` cannot say WHICH requirement was
broken, and the read-back cannot tell a rule from typed text. A marker
is only an origin when no contributed text can forge one, so every
label and body passes the frame neutraliser below — the same one the
skill frame uses, because both frames share the writer's prompt.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from inqtrix.i18n import t

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

log = logging.getLogger(__name__)

FREE_REQUIREMENT_MARKER = "[Free requirement]"
"""Origin marker of the text the user typed (at submit time or at the gate)."""

REPORT_GUIDANCE_MAX_CHARS = 2_000
"""Ceiling for the typed half of the requirement, at BOTH entry points.

The requirement can be set before the run and at the plan gate, and the
two must agree: a text the composer accepts and the gate rejects (or the
reverse) would be a limit the user cannot predict.
"""

REPORT_RULE_IDS_MAX = 3
"""How many library rules one requirement may attach, at both entry points."""

MAX_COMPOSED_REQUIREMENT_CHARS = 20_000
"""Ceiling for the composed requirement.

Not a truncation — a refusal. Attached rules are whole documents (the
shipped 'Executive Briefing' template alone is ~17k characters) and the
requirement is repeated into every writing prompt of the run, so a stack
of them is a real cost. Exceeding this fails loudly and names the
offender; nothing is ever shortened behind the user's back.
"""


_QUOTE = r"['\"\u2018\u2019\u201a\u201b\u201c\u201d\u201e`]"
"""The quotes a skill label may stand in: straight ones, typographic single
and double ones (U+2018, U+2019, U+201C, U+201D), low and reversed ones
(U+201A, U+201B, U+201E) and a backtick."""

_LABEL_BRACKETS = str.maketrans({"[": "(", "]": ")", "\uff3b": "(", "\uff3d": ")"})
"""Every bracket a label may close a marker with — ASCII and fullwidth
(U+FF3B, U+FF3D) — folded to a parenthesis."""

_GAP = r"[\s\u00ad\u200b-\u200d\u2060\ufeff]"
"""What may stand in a gap of a frame marker: whitespace, the soft hyphen
(U+00AD) and the invisible format characters zero-width space, non-joiner
and joiner (U+200B to U+200D), word joiner (U+2060) and byte order mark
(U+FEFF). A reader does not see them, so ``End`` and ``rule`` joined by a
zero-width joiner look like ``End rule``."""

_FRAME_MARKER_OPENING = re.compile(
    rf"[\[\uff3b](?={_GAP}*(?:rule{_GAP}*:|end{_GAP}+rule{_GAP}*:"
    rf"|free{_GAP}+requirement{_GAP}*[\]\uff3d]"
    rf"|skill{_GAP}*{_QUOTE}|end{_GAP}+of{_GAP}+skill{_GAP}*{_QUOTE}))",
    re.IGNORECASE,
)
"""The opening bracket of the frame-marker spellings the neutraliser
recognises: ``[Rule:``, ``[End rule:``, ``[Free requirement]``, ``[Skill '``
and ``[End of skill '`` in any letter case, opened by an ASCII or a
fullwidth bracket (U+FF3B; the free requirement may also close with
U+FF3D), with any character of :data:`_GAP` after the bracket, between
the words and before the colon or quote, and with any quote of
:data:`_QUOTE` around a skill label.

Not recognised: letters from other scripts that look like Latin ones (a
Cyrillic small ie, U+0435, in ``rule``), a fullwidth colon (U+FF1A), and
``[Skill:`` — a spelling the skill frame does not use."""


def neutralize_frame_label(label: str) -> str:
    """*label* folded to one line, its brackets defused.

    Labels sit INSIDE a frame marker. Template labels are free text
    without a pattern or a length limit, so a line break or a bracket
    in one would close the marker early and forge a frame of its own.
    Fullwidth brackets read like ASCII ones and fold the same way.
    """
    return " ".join(label.split()).translate(_LABEL_BRACKETS)


def neutralize_frame_body(text: str) -> str:
    """*text* with every frame-marker spelling the neutraliser recognises
    (:data:`_FRAME_MARKER_OPENING`) defused.

    The contributed text of a rule, a skill or the typed requirement
    stands between an opener and a closer. A marker inside it would end
    the frame early and continue as a frame of another origin — a shared
    rule posing as the user's typed text, a skill posing as a rule. Only
    the opening bracket of a marker changes (to ``(``), so the text stays
    readable, keeps its length and every other bracket — citation labels,
    numbered markers, Markdown links — is left untouched.
    """
    return _FRAME_MARKER_OPENING.sub("(", text)


_BLOCK_SEPARATOR = "\n\n"
"""The joint between two blocks of a composed requirement: one blank line."""


def rule_requirement_marker(label: str) -> str:
    """Origin marker of one attached library rule."""
    return f"[Rule: {label}]"


def rule_requirement_closer(label: str) -> str:
    """Closing marker of one attached library rule."""
    return f"[End rule: {label}]"


def rule_requirement_block(label: str, content: str) -> str:
    """The framed block one attached rule contributes (``""`` without a body).

    Label and body pass the frame neutraliser, so the block's own opener
    and closer are the only markers in it.
    """
    body = neutralize_frame_body(content.strip())
    if not body:
        return ""
    name = neutralize_frame_label(label)
    return f"{rule_requirement_marker(name)}\n{body}\n{rule_requirement_closer(name)}"


def compose_requirement_blocks(
    rule_blocks: "Sequence[str]", free_text: str = ""
) -> str:
    """Framed rule blocks in order, then the typed text under its marker.

    The one place that joins a requirement: a fresh composition and one
    that keeps rule blocks a run is already frozen under (a plan gate that
    leaves the selection alone) produce the same text from the same parts.
    """
    parts = [block for block in rule_blocks if block]
    typed = neutralize_frame_body(free_text.strip())
    if typed:
        parts.append(f"{FREE_REQUIREMENT_MARKER}\n{typed}")
    return _BLOCK_SEPARATOR.join(parts)


def compose_report_requirement(
    *,
    free_text: str = "",
    rules: "Sequence[tuple[str, str]]" = (),
) -> str:
    """The requirement block, rules first, typed text last.

    Args:
        free_text: What the user typed (at submit time or at the gate).
        rules: ``(label, content)`` of each attached library rule, in
            attachment order. Labels are resolved server-side from the
            caller's own catalog — a client-supplied label would be a
            free-text channel into the prompt.

    Returns:
        The composed block, or ``""`` when there is nothing to say.
        Labels and bodies pass the frame neutraliser, so no contribution
        can open or close a frame; text without marker spellings composes
        byte for byte.
    """
    return compose_requirement_blocks(
        [rule_requirement_block(label, content) for label, content in rules],
        free_text,
    )


def split_composed_requirement(
    composed: str, labels: "Sequence[str]"
) -> tuple[list[str], str] | None:
    """The rule blocks (one per label, in order) and the typed text of
    *composed* — or ``None`` when it is not exactly that composition.

    A run is frozen under the composed text, not under the catalog: the
    rule bodies it works under exist nowhere else once a template is
    edited or deleted. Keeping a rule at a plan gate, or counting the
    typed half for the transcript, therefore reads them back from that
    text. The read is exact rather than heuristic because the neutraliser
    leaves each block's own opener and closer as the only markers in it,
    and the result must compose back to *composed* byte for byte. A text
    composed before markers were defused, or one whose parts were not
    kept, does not split.

    Args:
        composed: A requirement as :func:`compose_report_requirement`
            made it.
        labels: The labels of the rules it was composed from, in
            attachment order (the stored rule parts).
    """
    rest = composed
    blocks: list[str] = []
    for label in labels:
        folded = neutralize_frame_label(label)
        opener = f"{rule_requirement_marker(folded)}\n"
        closer = f"\n{rule_requirement_closer(folded)}"
        if not rest.startswith(opener):
            return None
        end = rest.find(closer, len(opener))
        if end < 0:
            return None
        block = rest[: end + len(closer)]
        if rule_requirement_block(label, block[len(opener):end]) != block:
            return None
        blocks.append(block)
        rest = rest[end + len(closer):]
        if rest:
            if not rest.startswith(_BLOCK_SEPARATOR):
                return None
            rest = rest[len(_BLOCK_SEPARATOR):]
    typed = ""
    if rest:
        prefix = f"{FREE_REQUIREMENT_MARKER}\n"
        if not rest.startswith(prefix):
            return None
        typed = rest[len(prefix):]
    if compose_requirement_blocks(blocks, typed) != composed:
        return None
    return blocks, typed


REQUIREMENT_REVIEW_NONE = "none"
"""The run owes no review pass (its tier skips it): the requirement shaped
the writing, and nothing checked the result against it afterwards."""

REQUIREMENT_REVIEW_ASSIGNMENT = "assignment"
"""The run's review pass checks the result against the assignment the
requirement is part of — as a whole, without a verdict per rule."""

REQUIREMENT_REVIEW_PER_RULE = "per_rule"
"""The run's review pass judges every attached rule on its own, one
criterion per rule (:func:`rule_review_criteria`), besides the assignment as
a whole. Only an engine that builds those criteria reports it (the kernel)."""


def reviews_rule_by_rule(
    rules: "Sequence[Mapping[str, Any]]", *, review_owed: bool
) -> bool:
    """Whether a run's review judges each attached rule on its own.

    The one predicate behind the event's ``per_rule`` and the criteria a
    review receives, so the line under the question never promises a
    per-rule review that has no criterion to judge.
    """
    return review_owed and bool(rules)


def _rule_marker(part: "Mapping[str, Any]") -> str:
    """The marker *part*'s block opens with in the composed requirement."""
    return rule_requirement_marker(neutralize_frame_label(str(part["label"])))


def rule_review_criteria(rules: "Sequence[Mapping[str, Any]]") -> tuple[str, ...]:
    """One review criterion per attached rule, in attachment order.

    Model text: the review reads the composed requirement, so each criterion
    names its rule by the marker that rule's block carries there, and by its
    position, because two rules may share a label and a revision. How a rule
    ranks against the assignment, the typed requirement, the answers to
    clarifying questions and the other rules is the precedence stated with
    the requirement; the criterion points there instead of restating it.
    """
    count = len(rules)
    return tuple(
        f"Attached rule {position} of {count}, {_rule_marker(part)} at revision "
        f"{int(part['revision'])}: the deliverables follow this rule wherever it "
        "applies, as the precedence stated with the output requirement decides."
        for position, part in enumerate(rules, start=1)
    )


def _rule_title(part: "Mapping[str, Any]") -> str:
    """The name a reader knows *part* by: the title it was attached under,
    else its label, on one line."""
    return " ".join(str(part.get("title") or part["label"]).split())


def rule_reader_names(
    rules: "Sequence[Mapping[str, Any]]", language: str
) -> tuple[str, ...]:
    """Each attached rule as a reader knows it, in *language*: by the title
    it was attached under and the revision the run is frozen at."""
    words = {"language": language}
    return tuple(
        t(words, "review_rule_name", title=_rule_title(part), revision=int(part["revision"]))
        for part in rules
    )


def rule_marker_names(
    rules: "Sequence[Mapping[str, Any]]", language: str
) -> dict[str, str]:
    """Every rule marker a model text may quote, by the name a reader knows.

    Rules that share a label share a marker; such a marker names the handle
    both carry, never one of the two titles.
    """
    words = {"language": language}
    readers = rule_reader_names(rules, language)
    by_marker: dict[str, list[int]] = {}
    for position, part in enumerate(rules):
        by_marker.setdefault(_rule_marker(part), []).append(position)
    return {
        marker: (
            readers[positions[0]]
            if len(positions) == 1
            else t(words, "review_rule_handle",
                   label=" ".join(str(rules[positions[0]]["label"]).split()))
        )
        for marker, positions in by_marker.items()
    }


def rule_part(part: "Mapping[str, Any]") -> dict[str, Any]:
    """The stored part of one attached rule: id, label, revision and —
    only when one was actually kept — the title its pill showed.

    The one place that shapes a part. A part stored before titles were
    kept stays without one: a reader falls back to the label, and no
    producer invents a title the rule was never attached under.
    """
    shaped: dict[str, Any] = {
        "template_id": str(part["template_id"]),
        "label": str(part["label"]),
    }
    title = str(part.get("title") or "")
    if title.strip():
        shaped["title"] = title
    shaped["revision"] = int(part["revision"])
    return shaped


def _rule_parts(rules: "Sequence[Mapping[str, Any]]") -> list[dict[str, Any]]:
    return [rule_part(part) for part in rules]


def requirement_parts(
    composed: str, rules: "Sequence[Mapping[str, Any]]"
) -> dict[str, Any] | None:
    """What a composed requirement was made of, in the form a plan-gate
    decision stores it: ``{"free_text", "rules": [{template_id, label,
    title, revision}]}``.

    *rules* are the stored parts; the typed text is read back from
    *composed* (:func:`split_composed_requirement`). ``None`` when the
    text does not split into those parts — the caller says what it cannot
    offer then.
    """
    parts = _rule_parts(rules)
    split = split_composed_requirement(
        composed, [part["label"] for part in parts]
    )
    if split is None:
        return None
    return {"free_text": split[1], "rules": parts}


def requirement_attached_payload(
    composed: str,
    rules: "Sequence[Mapping[str, Any]]",
    *,
    review_owed: bool,
    run_id: str,
    rule_by_rule: bool = False,
) -> dict[str, Any]:
    """The record of a requirement a run starts under, for its transcript.

    Each rule by id, label and the revision the run is frozen under (the
    one thing the typed question never shows), the typed half by its
    length only, and whether a review pass checks the result: ``per_rule``
    when the engine judges each attached rule on its own (*rule_by_rule*,
    the kernel), ``assignment`` when the pass checks the assignment as a
    whole, ``none`` when the tier owes none. A
    composition that does not split into the stored parts — a run queued
    before the parts were kept — reports its typed length as ``None`` and
    says so in a WARNING instead of guessing.
    """
    parts = requirement_parts(composed, rules)
    if parts is None:
        log.warning(
            "Run %s: the result requirement does not split into its %d "
            "attached rule(s) and typed text; its typed length is reported "
            "as unknown.",
            run_id,
            len(rules),
        )
    return {
        "rules": _rule_parts(rules),
        "free_text_chars": None if parts is None else len(parts["free_text"]),
        "review": (
            REQUIREMENT_REVIEW_PER_RULE
            if rule_by_rule and reviews_rule_by_rule(rules, review_owed=review_owed)
            else REQUIREMENT_REVIEW_ASSIGNMENT
            if review_owed
            else REQUIREMENT_REVIEW_NONE
        ),
    }


def composed_requirement_is_oversized(composed: str) -> bool:
    """Whether the composed requirement exceeds the visible ceiling."""
    return len(composed) > MAX_COMPOSED_REQUIREMENT_CHARS
