"""Snapshot-based edits for the final review's existing outputs, decided per finding.

Every confirmed finding is located or named, and every located finding is
applied or named, each with one reason code (:data:`FINDING_REASONS`): a
finding that cannot be anchored, or an edit that breaks a rule, names that
finding alone and never takes the others with it.

The citation rule, per edit: it cites nothing but the labels its passage
keeps and the labels of its finding's source basis, both in the target
output's label space, and it leaves every kept label on the sentence it
stood on. A new sentence of a fix that rests on sources (``evidence_error``,
``missing_information``, ``output_requirement``) carries one of its basis
labels; a ``run_diagnostic`` fix adds no label.

The content rule, per edit and per output: the review corrects, qualifies or
attributes, it never removes content (:func:`_content_problem`). No sentence,
list item, table row or code block leaves without a replacement, a long
passage is never cut to a remnant, and a revision keeps the bulk of a long
output; a heading alone may go.

For the re-check of an applied edit the module also says, from server
values alone, where the edit changed its passage (:func:`changed_segments`)
and where its replacement stands in the delivered text (:func:`placement`).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

from inqtrix.agents.critic_phase import SOURCE_BASIS_PREFIX
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.phase_models import DeepReviewFinding, DeepRevisionBundle
from inqtrix.agents.report_quality import (
    added_identifiers,
    introduced_identifiers,
    malformed_citation_brackets,
    unknown_citation_labels,
)
from inqtrix.citation_labels import (
    DELEGATED_FAMILIES,
    HEADING_LINE,
    LIST_LINE,
    CitationSegment,
    canonicalize_citations,
    citation_pattern,
    citation_run_end,
    citation_segments,
    citation_spans,
    cited_labels,
    segment_is_new,
    segment_norm,
    segment_within,
    segment_words,
)
from inqtrix.strategies._claim_values import claim_values

log = logging.getLogger("inqtrix")

RECHECK_REJECTION_CODES: tuple[str, ...] = ("not_carried", "reservation_dropped", "off_finding")
"""The codes the re-check of an applied edit rejects it with, in precedence
order: an edit with several is settled with the first. A rejection takes
nothing back and names no finding: the edit stays applied and unverified,
and its code reaches the log alone, never a reader or a record; this tuple
is the one place a code is added."""

FINDING_REASONS: tuple[str, ...] = (
    "finding_invalid",
    "basis_invalid",
    "basis_outside_output",
    "remove_labels_invalid",
    "anchor_missing",
    "anchor_ambiguous",
    "anchor_overlap",
    "provider_context_only",
    "edit_not_single",
    "edit_unchanged",
    "edit_deletes",
    "edit_shrinks",
    "edit_rewrites_unchecked",
    "edit_drops_figure",
    "identifier_added",
    "citation_malformed",
    "label_not_citable",
    "quote_label_dropped",
    "label_moved",
    "segment_without_basis_label",
    "labels_whole_text",
    "output_emptied",
    "output_shrunk",
    "revision_context",
    "revision_failed",
    "revision_empty",
    "write_conflict",
    "store_error",
    "research_route",
    "cancelled",
    "revision_error",
)
"""Why a confirmed finding was not applied: the closed set of naming reasons.

Each reason has the reader sentence ``review_finding_reason_<code>``
(:mod:`inqtrix.i18n`); a reason without one is a defect, not a case to fall
through, and :class:`FindingResult` refuses any code outside this set."""

OVERLAP_EDIT = "overlap_edit"
"""The one reason an APPLIED result carries: a server finding that lost its
overlap and whose quote the winning edit removed; that edit carried the fix."""

PROVIDER_CONTEXT_ONLY = "provider_context_only"
"""The reason of a finding that marks its passage instead of revising it: an
``evidence_error`` whose every basis is a source the passage itself cites
and none of which holds stored source text. The review measured the passage
against the search provider's summary alone, and a statement that summary
leaves out is no wrong statement; the passage stays as written and is marked
for the reader. Such a result is no revision decision (:func:`is_counted`)."""

RECHECK_REASONS: tuple[str, ...] = (
    "no_source_text",
    "format_only",
    "too_large",
    "check_unavailable",
    "inconclusive",
    "knowledge_only",
)
"""Why the re-check left an applied model edit unverified, as a reader is told,
in the order the summary names them: its new text cites only sources without
stored text, it changes only formatting, it is too large for the check, the
check could not run, the check ran and settled nothing, or the run under work
holds no source at all and the edit rests on general knowledge. A rejected
edit carries none: its code is the server's reading and stands in the log.

Each reason has the reader words ``outcome_reason_<reason>``
(:mod:`inqtrix.i18n`), and :class:`FindingResult` refuses any other."""

_BASIS_LABEL_KINDS = ("evidence_error", "missing_information", "output_requirement")
"""The kinds whose new sentences carry a label of the finding's basis."""

PASSAGE_KEEP_WORDS = 25
"""Words from which a passage counts as long: its replacement keeps at least
:data:`PASSAGE_KEEP_SHARE` of them."""

PASSAGE_KEEP_SHARE = 0.6
"""The share of a long passage's words its replacement keeps. Measured on the
review's revisions: a fix kept 0.81 or more of a long passage, while every cut
to a remnant kept 0.39 or less, replacing paragraphs by one sentence about the
evidence."""

OUTPUT_KEEP_WORDS = 100
"""Words from which a whole output's revision is bounded by
:data:`OUTPUT_KEEP_SHARE`; a smaller output is guarded per edit alone."""

OUTPUT_KEEP_SHARE = 0.75
"""The share of a long output's words its revision keeps. Measured: the
review's revisions ended between 0.82 and 1.32 of their draft, while the one
that deleted section after section kept 0.10."""


def is_counted(origin: str, reason: str) -> bool:
    """Whether a result counts as a revision decision: every result but a
    server finding that lost its overlap (``anchor_overlap``,
    :data:`OVERLAP_EDIT`), whose fix, if any, was another finding's edit, and
    a marked passage (:data:`PROVIDER_CONTEXT_ONLY`), which never reached a
    revision."""
    if reason == PROVIDER_CONTEXT_ONLY:
        return False
    return not (origin == "server" and reason in ("anchor_overlap", OVERLAP_EDIT))


@dataclass(frozen=True)
class FindingResult:
    """What became of one confirmed finding: applied, or named with its reason."""

    finding_id: str
    origin: str
    """``model`` (the review's verdict) or ``server`` (a measured finding)."""
    output_id: str
    kind: str
    criterion: int | None
    """The validated criterion number; ``None`` when it concerns none."""
    finding: str
    """The finding's prose; it stays out of :meth:`record`."""
    quote: str
    state: str
    """``applied`` or ``named``."""
    reason: str = ""
    recheck: str = ""
    """``held`` when the re-check of the applied edits held this one twice,
    in its check and in the confirmation; ``""`` when it was not checked,
    not held both times, or not applied."""
    recheck_reason: str = ""
    """Why the re-check left this applied edit unverified, one of
    :data:`RECHECK_REASONS`; ``""`` when it held, was rejected, never
    reached the re-check (a server finding) or was not applied."""

    def __post_init__(self) -> None:
        allowed = (
            FINDING_REASONS if self.state == "named"
            else ("", OVERLAP_EDIT) if self.state == "applied"
            else ()
        )
        if self.reason not in allowed:
            raise ValueError(
                f"Finding result {self.finding_id}: state {self.state!r} cannot "
                f"carry the reason {self.reason!r}."
            )
        if self.recheck not in ("", "held") or (
            self.recheck == "held" and self.state != "applied"
        ):
            raise ValueError(
                f"Finding result {self.finding_id}: state {self.state!r} cannot "
                f"carry the re-check {self.recheck!r}."
            )
        if self.recheck_reason and (
            self.recheck_reason not in RECHECK_REASONS
            or self.state != "applied"
            or self.recheck
        ):
            raise ValueError(
                f"Finding result {self.finding_id}: state {self.state!r} with the "
                f"re-check {self.recheck!r} cannot carry the re-check reason "
                f"{self.recheck_reason!r}."
            )

    def named(self, reason: str) -> "FindingResult":
        """This result named after all with *reason*: an applied edit whose
        revision was never written keeps nothing of its re-check."""
        return replace(self, state="named", reason=reason, recheck="", recheck_reason="")

    def record(self) -> dict[str, Any]:
        """The machine record of this result, without the finding's prose."""
        return {
            "finding_id": self.finding_id,
            "origin": self.origin,
            "target": self.output_id,
            "kind": self.kind,
            "criterion": self.criterion,
            "state": self.state,
            "reason": self.reason,
            "recheck": self.recheck,
            "recheck_reason": self.recheck_reason,
        }


def named_result(item: Mapping[str, Any], reason: str) -> FindingResult:
    """*item* (a located finding) named with *reason*."""
    return _result(item, "named", reason)


def applied_result(item: Mapping[str, Any], reason: str = "") -> FindingResult:
    """*item* (a located finding) applied."""
    return _result(item, "applied", reason)


def _result(item: Mapping[str, Any], state: str, reason: str) -> FindingResult:
    return FindingResult(
        finding_id=str(item["finding_id"]),
        origin=str(item["origin"]),
        output_id=str(item["output_id"]),
        kind=str(item["kind"]),
        criterion=item.get("criterion"),
        finding=str(item["finding"]),
        quote=str(item["quote"]),
        state=state,
        reason=reason,
    )


def review_context_fits(
    llm: Any, *, model: str | None, prompt: str, schema: str, fallback_window: int,
) -> bool:
    """Reuse the existing rough context estimate; never tokenize or trim evidence."""
    from inqtrix.model_cards import resolve_model_card
    from inqtrix.nodes import (
        _estimate_required_context_tokens,
        _llm_context_window_tokens,
        _llm_default_output_tokens,
    )

    card = resolve_model_card(model) if model else None
    window = card.context_window_tokens if card else _llm_context_window_tokens(llm)
    if window is None:
        log.warning("Deep model context window unknown; using the existing conservative"
                    " context limit.")
        window = fallback_window
    output = _llm_default_output_tokens(llm)
    if card:
        output = min(output, card.max_output_tokens)
    _, required = _estimate_required_context_tokens(
        system=schema, prompt=prompt, requested_output_tokens=output,
    )
    return required <= window


def split_findings_by_basis(
    findings: list[DeepReviewFinding],
    *,
    read_sources: frozenset[tuple[str, str]],
    ledger_ids: frozenset[str],
    diagnostic_ids: dict[str, set[str]],
) -> tuple[list[DeepReviewFinding], list[DeepReviewFinding]]:
    """Separate the findings whose every basis is confirmed from the rest.

    A source basis is confirmed when the run under work holds that reference
    in its own ledger AND the review role had read it before it wrote its
    verdict. Whether the reviewed output cites it does not matter: a role
    that reads the whole released ledger finds an omitted limitation exactly
    in a source the output left out. A reference that exists only in the
    role's ledger (the citation of a stored search answer it opened) cannot
    be confirmed here. A diagnostic basis is confirmed when the handover
    carried it for that output.

    One unconfirmed basis makes ITS finding an open point that is never
    applied; the other findings of the same verdict keep their standing.
    Citability is a separate question, answered where an edit is applied.
    """
    confirmed: list[DeepReviewFinding] = []
    unconfirmed: list[DeepReviewFinding] = []
    for finding in findings:
        output_id = finding.artifact_id if finding.target == "artifact" else "chat"
        known_diagnostics = diagnostic_ids.get(output_id, set())

        def holds(basis: str) -> bool:
            if basis.startswith(SOURCE_BASIS_PREFIX):
                reference_id = basis.removeprefix(SOURCE_BASIS_PREFIX)
                return (
                    reference_id in ledger_ids
                    and ("retained_evidence", reference_id) in read_sources
                )
            return basis in known_diagnostics

        target = confirmed if all(holds(basis) for basis in finding.basis_ids) else unconfirmed
        target.append(finding)
    return confirmed, unconfirmed


def basis_label(basis_id: str, references: Sequence[Mapping[str, Any]]) -> str:
    """The label a source basis carries in one output's label space, or ``""``.

    Mapped by the reference's identity (its ledger id), never by its label:
    a canvas that numbers its own sources may call another source ``K3``
    than the run's ledger does. Only an unambiguous label counts: exactly
    one reference there has that identity, it carries a label, and no other
    reference there carries the same one.
    """
    identity = basis_id.removeprefix(SOURCE_BASIS_PREFIX)
    if not identity:
        return ""
    holders = [ref for ref in references if KernelDeps.ledger_id(dict(ref)) == identity]
    if len(holders) != 1:
        return ""
    label = str(holders[0].get("label") or "").strip()
    if not label or sum(
        1 for ref in references if str(ref.get("label") or "").strip() == label
    ) != 1:
        return ""
    return label


def label_bases(references: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Every label of one output's label space with the source basis it
    stands for (:func:`basis_label` read from the label's side). A label
    two references there carry stands for neither."""
    labels = Counter(str(ref.get("label") or "").strip() for ref in references)
    return {
        label: SOURCE_BASIS_PREFIX + KernelDeps.ledger_id(dict(ref))
        for ref in references
        if (label := str(ref.get("label") or "").strip()) and labels[label] == 1
    }


def locate_findings(
    findings: Sequence[tuple[str, DeepReviewFinding]],
    outputs: Mapping[str, str],
    *,
    source_basis_ids: Mapping[str, set[str]],
    diagnostic_basis_ids: Mapping[str, set[str]],
    allowed_references: Mapping[str, list[dict[str, Any]]],
    criteria_count: int,
    knowledge_mode: bool = False,
    unstored_basis_ids: Collection[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[FindingResult]]:
    """Resolve every finding against the same unmodified output snapshot.

    *findings* are ``(origin, finding)`` in verdict order, the model's first
    and the server's last; ids are ``finding_{n}`` over that list, so an id
    never shifts when an earlier finding is named. The first failing check
    names a finding, in this order: target or wording invalid; a basis
    outside the output's bases, or missing for the kind (a source for
    ``evidence_error`` and ``missing_information``, a diagnostic for
    ``run_diagnostic``); a source basis without an unambiguous label in the
    target output (:func:`basis_label`; a ``run_diagnostic`` cites none); an
    invalid ``remove_labels`` entry; a missing or ambiguous anchor; an
    anchor that overlaps a finding located before it in the same output.

    A citation run right after the quote belongs to the passage: the house
    style cites after the period (``Satz. [K12]``), and a label left outside
    the passage could neither stay on its sentence nor be removed. The
    located item's ``quote`` is that passage.

    An overlap is decided here, once: the earlier finding in verdict order
    wins, a finding named before its anchor was checked claims no span, and
    a winner whose edit is later refused does not revive its loser.

    *knowledge_mode* marks every located item of a run without stored
    evidence (``knowledge``): it has no label to cite, so its new sentences
    need none (:func:`_edit_problem`). Only this explicit mark lifts the
    rule; an empty basis in a run with sources never does.

    *unstored_basis_ids* are the source bases known to hold no stored source
    text, only the search provider's context or a source's metadata. A
    finding that passes every check above and rests on nothing else than
    such sources of its own passage, a passage that cites no other source,
    is no edit: it marks its passage (:data:`PROVIDER_CONTEXT_ONLY`), claims
    its span against a later finding, and is named. A located evidence
    finding resting only on such sources keeps every sentence of its
    passage (``keep_sentences``); every located item carries the labels of
    such sources in its output (``unstored_labels``), from which its edit's
    numbers are guarded (:func:`_edit_problem`). Without the set nothing is
    known to lack stored text, and no finding is held to these rules.

    Returns the located items and the results of the named findings.
    """
    located: list[dict[str, Any]] = []
    named: list[FindingResult] = []
    claimed: list[tuple[str, int, int]] = []
    for index, (origin, finding) in enumerate(findings, start=1):
        finding_id = f"finding_{index}"
        target = finding.artifact_id if finding.target == "artifact" else "chat"
        raw = finding.criterion_index
        criterion = raw if raw is not None and 1 <= raw <= criteria_count else None
        if raw is not None and criterion is None:
            log.warning(
                "Review finding %s names criterion %r, which the handover does not "
                "list (%d criteria); it counts as none.",
                finding_id, raw, criteria_count,
            )
        sources = source_basis_ids.get(target, set())
        mapped = [
            basis_label(basis, allowed_references.get(target, []))
            for basis in finding.basis_ids if basis in sources
        ]
        text = outputs.get(target, "")
        start = text.find(finding.quote) if finding.quote else -1
        end = (
            citation_run_end(text, start + len(finding.quote), DELEGATED_FAMILIES)
            if start >= 0 else start + len(finding.quote)
        )
        passage = text[start:end] if start >= 0 else finding.quote
        quoted = cited_labels(passage, DELEGATED_FAMILIES)
        remove = _validated_removals(finding.remove_labels, quoted)
        item = {
            **finding.model_dump(),
            "quote": passage,
            "finding_id": finding_id,
            "origin": origin,
            "output_id": target,
            "start": start,
            "end": end,
            "criterion": criterion,
        }
        reason = _locate_problem(
            finding, target, outputs,
            sources=sources,
            diagnostics=diagnostic_basis_ids.get(target, set()),
            mapped=mapped, remove=remove, start=start, end=end, claimed=claimed,
        )
        if reason:
            log.warning("Review finding %s not located: %s.", finding_id, reason)
            named.append(named_result(item, reason))
            continue
        claimed.append((target, start, end))
        references = allowed_references.get(target, [])
        unstored_labels = sorted({
            label for basis in unstored_basis_ids if (label := basis_label(basis, references))
        })
        source_bases = [basis for basis in finding.basis_ids if basis.startswith(SOURCE_BASIS_PREFIX)]
        unchecked = (
            not knowledge_mode
            and bool(source_bases)
            and all(basis in unstored_basis_ids for basis in source_bases)
        )
        if (
            unchecked
            and finding.kind == "evidence_error"
            and len(source_bases) == len(finding.basis_ids)
            and set(mapped) <= set(quoted) <= set(unstored_labels)
        ):
            log.info(
                "Review finding %s marks its passage: its sources hold no stored text.",
                finding_id,
            )
            named.append(named_result(item, PROVIDER_CONTEXT_ONLY))
            continue
        located.append({
            **item,
            "keep": [label for label in quoted if label not in (remove or [])],
            "remove": list(remove or []),
            "may_add": [] if finding.kind == "run_diagnostic" else mapped,
            "knowledge": knowledge_mode,
            "unstored_labels": unstored_labels,
            "keep_sentences": unchecked and finding.kind in ("evidence_error", "missing_information"),
        })
    return located, named


def bare_label(written: str) -> str:
    """The label *written* names, as a model may write it: without its outer
    whitespace and one pair of surrounding brackets with the whitespace
    inside them. ``[K5]``, `` K5 `` and ``[ K5 ]`` are ``K5``; ``[[K5]]``
    is ``[K5]``, which names no label."""
    label = str(written).strip()
    if label.startswith("[") and label.endswith("]"):
        label = label[1:-1].strip()
    return label


def _validated_removals(entries: Sequence[str], quoted: Sequence[str]) -> list[str] | None:
    """The labels *entries* name, each standing in the quote; ``None`` when one does not.

    An entry is read as :func:`bare_label` reads it.
    """
    removed: list[str] = []
    for entry in entries:
        label = bare_label(entry)
        if not citation_pattern(DELEGATED_FAMILIES).fullmatch(f"[{label}]") or label not in quoted:
            return None
        if label not in removed:
            removed.append(label)
    return removed


def _locate_problem(
    finding: DeepReviewFinding,
    target: str,
    outputs: Mapping[str, str],
    *,
    sources: set[str],
    diagnostics: set[str],
    mapped: Sequence[str],
    remove: list[str] | None,
    start: int,
    end: int,
    claimed: Sequence[tuple[str, int, int]],
) -> str:
    """The first locate check *finding* fails, as its reason; ``""`` when none."""
    if (
        target not in outputs
        or (finding.target == "chat" and finding.artifact_id)
        or not finding.finding.strip()
        or not finding.quote.strip()
    ):
        return "finding_invalid"
    basis = set(finding.basis_ids)
    if not basis <= sources | diagnostics:
        return "basis_invalid"
    if finding.kind in ("evidence_error", "missing_information") and not basis & sources:
        return "basis_invalid"
    if finding.kind == "run_diagnostic" and not basis & diagnostics:
        return "basis_invalid"
    if finding.kind != "run_diagnostic" and not all(mapped):
        return "basis_outside_output"
    if remove is None:
        return "remove_labels_invalid"
    if start < 0:
        return "anchor_missing"
    if outputs[target].find(finding.quote, start + 1) >= 0:
        return "anchor_ambiguous"
    if any(
        output == target and start < other_end and other_start < end
        for output, other_start, other_end in claimed
    ):
        return "anchor_overlap"
    return ""


def apply_review_edits(
    outputs: Mapping[str, str],
    located: Sequence[Mapping[str, Any]],
    bundle: DeepRevisionBundle,
    *,
    allowed_references: Mapping[str, list[dict[str, Any]]],
    identifiers: Sequence[str],
    user_text: str = "",
) -> tuple[dict[str, str], list[FindingResult]]:
    """Validate each finding's edit, then splice in what survives.

    Per finding, in verdict order, its one replacement (grouped citations
    written singly first) must change its passage, keep its content
    (:func:`_content_problem`), copy no internal identifier the passage did
    not carry, add no unreadable bracket and keep the citation rule of this
    module. Then, per output, the surviving edits are composed from the
    ORIGINAL snapshot in offset order, backwards so no replacement moves
    another's anchor, and the whole text is checked after each: an edit is
    named when it is the first to empty the output, to cut a long output
    below :data:`OUTPUT_KEEP_SHARE` of its words, to add an unknown label or
    an unreadable bracket, to change a citation of the
    text it left untouched (an opened fence or a lone backtick turns every
    later citation into code), or to add an identifier occurrence. Every
    check is relative to the original, so a defect the text already carried
    (a user's ``[K3–K5]``) blocks no edit elsewhere.

    Identifiers are checked per replacement and on the whole text, so an
    identifier "move" (removed in one edit, added in another) is refused at
    the adding edit, whichever comes first.

    *identifiers* are every identifier the revision call carried; the ones
    in ``user_text`` — what a person typed — are theirs to repeat.

    Returns every output (unchanged ones as given) and one result per
    located finding, in verdict order.
    """
    known = {str(item["finding_id"]) for item in located}
    unknown = sorted({edit.finding_id for edit in bundle.edits} - known)
    if unknown:
        log.warning(
            "Revision names finding ids the review did not locate (%s); they are ignored.",
            ", ".join(unknown),
        )
    edits: dict[str, list[str]] = {}
    for edit in bundle.edits:
        edits.setdefault(edit.finding_id, []).append(edit.replacement)
    results: dict[str, FindingResult] = {}
    surviving: dict[str, list[tuple[Mapping[str, Any], str]]] = {}
    for item in located:
        finding_id = str(item["finding_id"])
        replacements = edits.get(finding_id, [])
        if len(replacements) != 1:
            results[finding_id] = _refused(
                item, "edit_not_single", f"{len(replacements)} replacements"
            )
            continue
        # A replacement is writer text: grouped citations are written singly
        # before it is checked, so the check reads what is spliced in.
        replacement = spliced_replacement(replacements[0])
        problem = _edit_problem(
            item, replacement, identifiers, user_text,
            **line_edges(
                outputs[str(item["output_id"])], int(item["start"]), int(item["end"])
            ),
        )
        if problem:
            results[finding_id] = _refused(item, *problem)
            continue
        surviving.setdefault(str(item["output_id"]), []).append((item, replacement))
    revised = dict(outputs)
    for output_id, pairs in surviving.items():
        original = outputs[output_id]
        references = allowed_references.get(output_id, [])
        accepted: list[tuple[int, int, str]] = []
        for item, replacement in sorted(pairs, key=lambda pair: int(pair[0]["start"])):
            start, end = edit_span(
                original, int(item["start"]), int(item["end"]), replacement,
                after=accepted[-1][1] if accepted else 0,
            )
            spans = [*accepted, (start, end, replacement)]
            problem = _composition_problem(
                original, spans, references, identifiers, user_text
            )
            if problem:
                results[str(item["finding_id"])] = _refused(item, *problem)
                continue
            accepted.append(spans[-1])
            results[str(item["finding_id"])] = applied_result(item)
        revised[output_id] = _spliced(original, accepted)
    return revised, [results[str(item["finding_id"])] for item in located]


def spliced_replacement(replacement: str) -> str:
    """The text an edit's *replacement* is checked and spliced in as.

    A replacement is writer text: its grouped citations are written singly
    first. Every reader of an applied edit reads this one string, so what is
    checked is what the output holds.
    """
    return canonicalize_citations(replacement, DELEGATED_FAMILIES)


def line_edges(original: str, start: int, end: int) -> dict[str, bool]:
    """Whether the passage ``[start, end)`` of *original* starts and ends a line.

    The edges :func:`~inqtrix.citation_labels.citation_segments` reads a
    replacement of that passage with: one spliced in mid-line continues the
    line around it.
    """
    return {
        "at_line_start": start == 0 or original[start - 1] == "\n",
        "at_line_end": end == len(original) or original[end] in "\r\n",
    }


def edit_span(
    original: str, start: int, end: int, replacement: str, *, after: int = 0,
) -> tuple[int, int]:
    """The span an edit replaces in *original*.

    Its passage ``[start, end)``; for an empty replacement of whole lines
    also one line end: the one after them, or at the end of the text the one
    before, unless an earlier edit of the same text, ending at *after*,
    already took it. Only a heading leaves without a replacement (content
    never does, :func:`_content_problem`); its line then leaves no blank
    line behind, and a heading between blank lines keeps a blank line on
    each side, so its neighbours never merge. Every reader of an applied edit's place
    (:func:`apply_review_edits`, the re-check's placements) takes the span
    from here, in order of position.
    """
    edges = line_edges(original, start, end)
    if replacement or start == end or not (edges["at_line_start"] and edges["at_line_end"]):
        return start, end
    if original.startswith("\r\n", end):
        return start, end + 2
    if original.startswith("\n", end):
        return start, end + 1
    if end == len(original) and start > 0:
        back = 2 if original[max(0, start - 2):start] == "\r\n" else 1
        if start - back >= after:
            return start - back, end
    return start, end


def spliced_positions(spans: Sequence[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """Where each ``(start, end, replacement)`` of *spans* stands once all of
    them are spliced into their text (:func:`_spliced`), in the order given.

    Each replacement before a span shifts it by its change in length; the
    spans do not overlap.
    """
    placed: dict[int, tuple[int, int]] = {}
    shift = 0
    for index in sorted(range(len(spans)), key=lambda position: spans[position]):
        start, end, replacement = spans[index]
        placed[index] = (start + shift, start + shift + len(replacement))
        shift += len(replacement) - (end - start)
    return [placed[index] for index in range(len(spans))]


@dataclass(frozen=True)
class ChangedSegment:
    """One place where an applied edit changed its passage (:func:`changed_segments`)."""

    id: str
    """``s1``, ``s2``, ... per edit, in text order."""
    kind: str
    """``changed`` (text on both sides), ``added`` (new text only) or
    ``removed`` (replaced text only)."""
    before: str
    """The replaced text of this place; ``""`` when it is added."""
    after: str
    """The new text of this place; ``""`` when it is removed."""


_CLAUSE_BREAK = re.compile(r"(?<=[;:])\s+")
"""Where :func:`changed_segments` splits a sentence into clauses."""


def changed_segments(
    quote: str, replacement: str, *, at_line_start: bool, at_line_end: bool,
) -> list[ChangedSegment]:
    """The places where *replacement* changed *quote*, as the re-check lists them.

    Both sides are read in units: the headings, sentences and code blocks of
    :func:`~inqtrix.citation_labels.citation_segments` (read with the edit's
    line edges, :func:`line_edges`), each sentence split once more after a
    ``;`` or ``:`` that whitespace follows. A unit is compared by its form
    (:func:`_comparison_units`), its words
    (:func:`~inqtrix.citation_labels.segment_words`), the marks that say as
    much as a word (:func:`_meaning_marks`) and its labels, so punctuation
    and emphasis change nothing while a word, a label, such a mark or the
    form does: a sentence that becomes a list item, a heading or a table
    row is a place. Every run of units that differs is one place, ``changed``,
    ``added`` or ``removed`` by the sides it has, its text the stretch from
    its first to its last unit on each side. A word dropped from or added
    to a clause therefore makes a ``changed`` place with both sides, never a
    lone removal or addition, and the same inputs give the same places.

    *replacement* is read as it is spliced in (:func:`spliced_replacement`),
    *quote* in the same canonical form.
    """
    edges = {"at_line_start": at_line_start, "at_line_end": at_line_end}
    old_text = canonicalize_citations(quote, DELEGATED_FAMILIES)
    new_text = spliced_replacement(replacement)
    old = _comparison_units(old_text, **edges)
    new = _comparison_units(new_text, **edges)
    matcher = SequenceMatcher(
        None,
        [_unit_key(unit, form) for unit, _span, form in old],
        [_unit_key(unit, form) for unit, _span, form in new],
        autojunk=False,
    )
    places: list[tuple[str, str, str]] = []
    for tag, old_first, old_end, new_first, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = _stretch(old_text, old[old_first:old_end])
        after = _stretch(new_text, new[new_first:new_end])
        kind = "changed" if before and after else "added" if after else "removed"
        places.append((kind, before, after))
    return [
        ChangedSegment(id=f"s{number}", kind=kind, before=before, after=after)
        for number, (kind, before, after) in enumerate(places, start=1)
    ]


def _comparison_units(
    text: str, *, at_line_start: bool, at_line_end: bool,
) -> list[tuple[CitationSegment, tuple[int, int], str]]:
    """*text*'s units for :func:`changed_segments`, each with its span in
    *text* and its form.

    Every unit is a substring of *text*, the canonical form
    :func:`~inqtrix.citation_labels.citation_segments` reads, so each is
    found after the one before it. A heading or a code block is its own
    form (``heading``, ``code``). A sentence takes the form of the line
    group it stands in, and its clauses take the sentence's: ``list_item``
    or ``table_row`` when it opens a list line or a table row
    (:data:`~inqtrix.citation_labels.LIST_LINE`) that does not continue the
    text around the edit (the first line when the edit does not start a
    line, the last line without a line break when it does not end one);
    otherwise ``sentence`` when it is the first unit, follows a heading or
    a code block, or a blank line stands between it and the sentence
    before; otherwise the form of the sentence before it.
    """
    lines = text.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))
    units: list[tuple[CitationSegment, tuple[int, int], str]] = []
    position = 0
    form = "sentence"
    previous: tuple[str, int] | None = None
    for segment in citation_segments(
        text, DELEGATED_FAMILIES, at_line_start=at_line_start, at_line_end=at_line_end,
    ):
        start = text.find(segment.text, position)
        position = start + len(segment.text)
        if segment.kind != "sentence":
            units.append((segment, (start, position), segment.kind))
            previous = (segment.kind, position)
            continue
        index = bisect_right(line_starts, start) - 1
        line = lines[index]
        continues = (index == 0 and not at_line_start) or (
            index == len(lines) - 1 and not at_line_end and line.splitlines() == [line]
        )
        opened = (
            LIST_LINE.match(text, line_starts[index])
            if not continues and not text[line_starts[index]:start].strip()
            else None
        )
        if opened:
            form = "table_row" if opened.group().strip() == "|" else "list_item"
        elif (
            previous is None
            or previous[0] != "sentence"
            or _line_breaks(text[previous[1]:start]) >= 2
        ):
            form = "sentence"
        clause_position = start
        for part in _CLAUSE_BREAK.split(segment.text):
            if clause := part.strip():
                clause_start = text.find(clause, clause_position)
                clause_position = clause_start + len(clause)
                units.append((
                    CitationSegment(
                        "sentence", clause, tuple(cited_labels(clause, DELEGATED_FAMILIES)),
                    ),
                    (clause_start, clause_position),
                    form,
                ))
        previous = ("sentence", position)
    return units


def _line_breaks(gap: str) -> int:
    """The line breaks in *gap*, counted as :meth:`str.splitlines` splits."""
    return sum(1 for piece in gap.splitlines(keepends=True) if piece.splitlines() != [piece])


def _unit_key(
    unit: CitationSegment, form: str,
) -> tuple[str, str, tuple[tuple[int, str], ...], tuple[str, ...]]:
    """What :func:`changed_segments` compares a unit by: its form, its words,
    the marks that say as much as a word (:func:`_meaning_marks`) and its
    labels."""
    return (
        form,
        segment_words(unit.text, DELEGATED_FAMILIES),
        _meaning_marks(unit.text),
        unit.labels,
    )


_MARK_PIECE = re.compile(r"(\w+)|[^\w\s]")
"""A word, or one character that is neither a word character nor whitespace."""

_SIGNS_ANYWHERE = frozenset("%‰§<>≤≥⩽⩾≠≈")
"""Marks that say as much as a word wherever they stand: percent, permille
and section signs, and order and approximation signs, which prose hardly
ever uses as punctuation and which may stand apart from their number
(``< €5``, ``< -3``, ``< ca. 5``); currency signs (Unicode category ``Sc``)
too. Markup counts the same way: a ``>`` that marks a quoted line or ends
an arrow (``->``) is such a mark, so a quoted passage broken into lines at
other words, or ``->`` written as ``→``, is a segment."""

_COMPARISON_SIGNS = frozenset("=~")
"""Marks that say as much as a word before a digit, whitespace between
allowed; elsewhere they are often an arrow or a filler (``=>``,
``~ sofort``)."""

_NUMBER_SIGNS = frozenset("+-−–±")
"""Marks that say as much as a word before a digit, directly or after one
currency sign or one more such sign (``-€5``, ``+-3``), unless they
directly follow a word character (``AB-12``). A sign apart from its number
counts nothing: with whitespace after it or after the currency sign
(``− 5``, ``-€ 5``) or with a currency code between them (``-EUR 5``)."""

_DIGIT = re.compile(r"\d")
_DIGIT_AHEAD = re.compile(r"\s*\d")
_WORD_CHARACTER = re.compile(r"\w")


def _number_ahead(norm: str, index: int) -> bool:
    """Whether a digit stands at *index* of *norm*, directly or after one
    currency sign or one of :data:`_NUMBER_SIGNS`."""
    ahead = norm[index:index + 1]
    if ahead and (ahead in _NUMBER_SIGNS or unicodedata.category(ahead) == "Sc"):
        index += 1
    return bool(_DIGIT.match(norm, index))


def _meaning_marks(text: str) -> tuple[tuple[int, str], ...]:
    """The marks of *text* that say as much as a word, each with the number
    of words before it, so a mark that moves counts too (``−3 bis 3``
    against ``3 bis −3``).

    Read on :func:`~inqtrix.citation_labels.segment_norm`, words counted as
    :func:`~inqtrix.citation_labels.segment_words` reads them. A mark (a
    character that is neither a word character nor whitespace) counts when
    it is a percent, permille, section or currency sign or an order or
    approximation sign (``<``, ``≤``, ``≠``, ``≈``), wherever it stands; an
    ``=`` or ``~`` before a digit; a sign before a digit, directly or after
    one currency sign or one more sign (``-€5``, ``+-3``), and not directly
    after a word character; or any mark directly between two digits
    (``1,500``, ``1.500``, ``3-5``, ``10:30``). Other punctuation, brackets
    and quotes count nothing; emphasis is gone already.
    """
    norm = segment_norm(text, DELEGATED_FAMILIES)
    marks: list[tuple[int, str]] = []
    words = 0
    for piece in _MARK_PIECE.finditer(norm):
        if piece.group(1):
            words += 1
            continue
        mark, index = piece.group(), piece.start()
        before, after = norm[index - 1:index], norm[index + 1:index + 2]
        if (
            mark in _SIGNS_ANYWHERE
            or unicodedata.category(mark) == "Sc"
            or (mark in _COMPARISON_SIGNS and _DIGIT_AHEAD.match(norm, index + 1))
            or (
                mark in _NUMBER_SIGNS
                and _number_ahead(norm, index + 1)
                and not _WORD_CHARACTER.fullmatch(before)
            )
            or (_DIGIT.fullmatch(before) and _DIGIT.fullmatch(after))
        ):
            marks.append((words, mark))
    return tuple(marks)


def _stretch(text: str, units: Sequence[tuple[CitationSegment, tuple[int, int], str]]) -> str:
    """The text from the first to the last of *units*; ``""`` for none."""
    if not units:
        return ""
    return text[units[0][1][0]:units[-1][1][1]]


@dataclass(frozen=True)
class Placement:
    """Where an applied replacement stands in the text it was spliced into.

    It shows the re-check what the replacement's place makes of it; it is
    never a basis. A field longer than its bound ends visibly with
    :data:`PLACEMENT_SHORTENED`.
    """

    heading: str
    """The nearest ATX heading above the replacement; ``""`` when none."""
    list_intro: str
    """For a replacement in a list or holding one: the last line before the
    list that is no item, also from the replacement itself; ``""`` when a
    heading stands there, or when the replacement neither stands in nor
    holds a list."""
    text_before: str
    """The text before the replacement on its line, or else the nearest
    non-blank line above it."""
    text_after: str
    """The text after the replacement on its line, or else the nearest
    non-blank line below it."""


PLACEMENT_SHORTENED = "… [shortened]"
"""How a shortened placement field ends; never cut silently."""

PLACEMENT_HEADING_MAX_CHARS = 200
"""Longest heading a placement shows, marker included."""

PLACEMENT_LINE_MAX_CHARS = 300
"""Longest other placement field, marker included."""

_WORD = re.compile(r"\w")
"""Text on a replacement's own line counts for :func:`placement` when it holds one."""


def placement(text: str, start: int, end: int) -> Placement:
    """Where the replacement at ``[start, end)`` of *text* stands.

    *text* is the delivered text, every applied edit spliced in, and
    ``[start, end)`` the replacement's span there
    (:func:`spliced_positions`). A line is a heading or a list line as
    :func:`~inqtrix.citation_labels.citation_segments` reads it
    (:data:`~inqtrix.citation_labels.HEADING_LINE`,
    :data:`~inqtrix.citation_labels.LIST_LINE`, a table row included). Text
    on the replacement's own line counts only when it holds a word: a lone
    list mark there leaves the neighbouring line to show.
    """
    first = text.rfind("\n", 0, start) + 1
    last = text.find("\n", end)
    last = len(text) if last < 0 else last
    above = [line for line in text[:first].splitlines() if line.strip()]
    below = [line for line in text[last:].splitlines() if line.strip()]
    on_line_before, on_line_after = text[first:start], text[end:last]
    return Placement(
        heading=_bounded(
            next((line for line in reversed(above) if HEADING_LINE.match(line)), ""),
            PLACEMENT_HEADING_MAX_CHARS,
        ),
        list_intro=_bounded(_list_intro(text, start, end), PLACEMENT_LINE_MAX_CHARS),
        text_before=_bounded(
            on_line_before if _WORD.search(on_line_before) else (above[-1] if above else ""),
            PLACEMENT_LINE_MAX_CHARS,
        ),
        text_after=_bounded(
            on_line_after if _WORD.search(on_line_after) else (below[0] if below else ""),
            PLACEMENT_LINE_MAX_CHARS,
        ),
    )


def _list_intro(text: str, start: int, end: int) -> str:
    """The intro of the list the replacement at ``[start, end)`` stands in or holds.

    The list starts at the replacement's own line when that is a list line,
    or else at the first list line inside the replacement; its intro is the
    nearest non-blank line above that is no list line. ``""`` when there is
    no such list, no such line, or a heading stands there.
    """
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((offset, line.rstrip("\r\n")))
        offset += len(line)
    own = next(
        (index for index, (line_start, line) in enumerate(lines)
         if line_start <= start <= line_start + len(line)),
        len(lines),
    )
    anchor = next(
        (index for index, (line_start, line) in enumerate(lines)
         if (index == own or start <= line_start < end) and LIST_LINE.match(line)),
        None,
    )
    if anchor is None:
        return ""
    for _line_start, line in reversed(lines[:anchor]):
        if not line.strip() or LIST_LINE.match(line):
            continue
        return "" if HEADING_LINE.match(line) else line
    return ""


def _bounded(line: str, limit: int) -> str:
    """*line* without its outer whitespace, shortened visibly to *limit* characters."""
    line = line.strip()
    if len(line) <= limit:
        return line
    return f"{line[:limit - len(PLACEMENT_SHORTENED) - 1].rstrip()} {PLACEMENT_SHORTENED}"


def _refused(item: Mapping[str, Any], reason: str, detail: str) -> FindingResult:
    """*item* named with *reason*; the English detail goes to the log only."""
    log.warning("Review edit %s not applied: %s (%s).", item["finding_id"], reason, detail)
    return named_result(item, reason)


def _spliced(original: str, spans: Sequence[tuple[int, int, str]]) -> str:
    """*original* with each ``(start, end, replacement)`` spliced in, backwards."""
    text = original
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _prose_words(text: str) -> int:
    """The words *text* holds outside its headings: what a reader takes in as
    content (:func:`~inqtrix.citation_labels.segment_words`)."""
    return sum(
        len(segment_words(segment.text, DELEGATED_FAMILIES).split())
        for segment in citation_segments(text, DELEGATED_FAMILIES)
        if segment.kind != "heading"
    )


def _content_problem(
    quote: str, replacement: str, *, at_line_start: bool, at_line_end: bool,
) -> tuple[str, str] | None:
    """The content rule of one edit, as ``(reason, detail)``, or ``None``.

    The review corrects, qualifies or attributes; it never removes content.
    No place where the edit changes its passage (:func:`changed_segments`)
    may lose every word it held outside headings, so a sentence, list item,
    table row or code block is never dropped, nor turned into a heading or a
    mark; a heading alone may go. A long passage (:data:`PASSAGE_KEEP_WORDS`)
    keeps :data:`PASSAGE_KEEP_SHARE` of its words, so a fix never cuts it to
    a remnant that speaks about the evidence instead of the matter.
    """
    places = changed_segments(
        quote, replacement, at_line_start=at_line_start, at_line_end=at_line_end,
    )
    lost = [
        place.id for place in places
        if _prose_words(place.before) and not _prose_words(place.after)
    ]
    if lost:
        return "edit_deletes", f"{len(lost)} place(s) lose their content: {', '.join(lost)}"
    held = _prose_words(quote)
    kept = _prose_words(replacement)
    if held >= PASSAGE_KEEP_WORDS and kept < PASSAGE_KEEP_SHARE * held:
        return "edit_shrinks", f"{kept} of {held} words"
    return None


def _cites_only_unstored(item: Mapping[str, Any], replacement: str) -> bool:
    """Whether *replacement* cites no source with stored text, in a run where
    the sources without it are known (``unstored_labels``): nothing the
    re-check could quote stands behind its new text."""
    unstored = set(item.get("unstored_labels") or ())
    return bool(unstored) and set(cited_labels(replacement, DELEGATED_FAMILIES)) <= unstored


def _unit_words(text: str, *, at_line_start: bool, at_line_end: bool) -> list[str]:
    """The words of each sentence, list item, table row or code block of
    *text*, labels aside (:func:`~inqtrix.citation_labels.segment_words`);
    headings are structure and not counted."""
    return [
        words
        for segment in citation_segments(
            text, DELEGATED_FAMILIES, at_line_start=at_line_start, at_line_end=at_line_end,
        )
        if segment.kind != "heading" and (words := segment_words(segment.text, DELEGATED_FAMILIES))
    ]


def _reworded_units(
    item: Mapping[str, Any], quote: str, replacement: str, *, at_line_start: bool, at_line_end: bool,
) -> int:
    """How many units of *quote* an edit of a ``keep_sentences`` finding
    rewords while citing only sources without stored text.

    Such a fix cannot be confirmed: it may move a label to the source that
    carries the statement and add a sentence with its own label, and every
    unit of its passage stands in it with the same words.
    """
    if not item.get("keep_sentences") or not _cites_only_unstored(item, replacement):
        return 0
    edges = {"at_line_start": at_line_start, "at_line_end": at_line_end}
    kept = Counter(_unit_words(replacement, **edges))
    reworded = 0
    for words in _unit_words(quote, **edges):
        if kept[words] > 0:
            kept[words] -= 1
        else:
            reworded += 1
    return reworded


def _dropped_numbers(item: Mapping[str, Any], quote: str, replacement: str) -> str:
    """Why an edit loses a number of its passage, for the log, or ``""``.

    A fix from general knowledge (``knowledge``) may change a number but
    never drop one: it keeps at least as many. A fix citing only sources
    without stored text keeps every number of its passage, however it
    writes it (:func:`stated_numbers`). A fix citing stored text is
    re-checked against it instead, and a run diagnostic speaks for the run.
    """
    if item.get("kind") == "run_diagnostic":
        return ""
    held, kept = stated_numbers(quote), stated_numbers(replacement)
    if item.get("knowledge"):
        return f"{len(held)} numbers held, {len(kept)} kept" if len(kept) < len(held) else ""
    if not _cites_only_unstored(item, replacement):
        return ""
    lost = held - kept
    return f"{len(lost)} number(s) dropped" if lost else ""


_MATH_SPAN = re.compile(r"\$([^$\n]+)\$")
_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")
_ORDINAL = re.compile(r"(?m)^(\s*(?:#{1,6}\s+)?(?:[-*+]\s+)?)\d{1,3}[.)](?=\s)")
_GROUPED = re.compile(r"\d{1,3}(?:[.\u202f\u00a0 ]\d{3})+(?:,\d+)?")
_NUMBER = re.compile(r"\d{1,3}(?:[.\u202f\u00a0 ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")


def stated_numbers(text: str) -> set[str]:
    """The numbers *text* states, each read by its value so that one number
    reads the same however it is written: a date by its calendar date, an
    amount or a percentage by its number (unit and currency aside,
    :func:`~inqtrix.strategies._claim_values.claim_values`), a formula as
    plain text, a grouped or decimal number by its value. A citation label
    and a list or heading ordinal state no number."""
    plain = citation_pattern(DELEGATED_FAMILIES).sub(
        " ", canonicalize_citations(text or "", DELEGATED_FAMILIES)
    )
    plain = _MATH_SPAN.sub(lambda match: _math_text(match.group(1)), plain)
    plain = _ORDINAL.sub(lambda match: match.group(1), plain)
    values = claim_values(plain)
    found = {
        value.value if value.kind == "date" else _canonical_number(value.value.split(" ")[0])
        for value in (*values.values, *values.scope)
    }
    blanked = list(plain)
    for start, end in values.spans:
        blanked[start:end] = " " * (end - start)
    found.update(_canonical_number(match.group(0)) for match in _NUMBER.finditer("".join(blanked)))
    return found


def _math_text(formula: str) -> str:
    """A LaTeX formula as the plain text it reads as: grouping braces
    (``28{.}000``) read as their mark, commands and braces dropped."""
    text = formula.replace("{.}", ".").replace("{,}", ",").replace("\\,", "")
    return _LATEX_COMMAND.sub(" ", text).replace("{", "").replace("}", "")


def _canonical_number(written: str) -> str:
    """*written* by its value: grouping marks out (``8.400``, ``10 000``), a
    decimal comma read as a point, trailing zeros dropped (``0,30`` is ``0.3``)."""
    digits = re.sub(r"[.\u202f\u00a0 ]", "", written) if _GROUPED.fullmatch(written) else written
    try:
        return format(Decimal(digits.replace(",", ".")).normalize(), "f")
    except InvalidOperation:
        return written


def _edit_problem(
    item: Mapping[str, Any],
    replacement: str,
    identifiers: Sequence[str],
    user_text: str,
    *,
    at_line_start: bool,
    at_line_end: bool,
) -> tuple[str, str] | None:
    """The first per-finding check *replacement* fails, as ``(reason, detail)``.

    The detail is English for the log: labels, identifiers or counts, never
    the replacement's prose.
    """
    quote = str(item["quote"])
    if replacement == canonicalize_citations(quote, DELEGATED_FAMILIES):
        return "edit_unchanged", "the replacement equals its passage"
    lost = _content_problem(
        quote, replacement, at_line_start=at_line_start, at_line_end=at_line_end,
    )
    if lost:
        return lost
    reworded = _reworded_units(
        item, quote, replacement, at_line_start=at_line_start, at_line_end=at_line_end,
    )
    if reworded:
        return "edit_rewrites_unchecked", f"{reworded} unit(s) reworded"
    dropped = _dropped_numbers(item, quote, replacement)
    if dropped:
        return "edit_drops_figure", dropped
    copied = added_identifiers(
        replacement,
        introduced_identifiers(replacement, list(identifiers), authored_by=user_text),
        replacing=quote,
    )
    if copied:
        return "identifier_added", ", ".join(copied)
    unreadable = Counter(
        malformed_citation_brackets(replacement, families=DELEGATED_FAMILIES)
    ) - Counter(malformed_citation_brackets(quote, families=DELEGATED_FAMILIES))
    if unreadable:
        return "citation_malformed", ", ".join(unreadable)
    keep = list(item["keep"])
    may_add = list(item["may_add"])
    cited = cited_labels(replacement, DELEGATED_FAMILIES)
    foreign = [label for label in cited if label not in {*keep, *may_add}]
    if foreign:
        return "label_not_citable", ", ".join(foreign)
    dropped = [label for label in keep if label not in cited]
    if dropped:
        return "quote_label_dropped", ", ".join(dropped)
    edges = {"at_line_start": at_line_start, "at_line_end": at_line_end}
    segments = citation_segments(replacement, DELEGATED_FAMILIES, **edges)
    moved = _moved_labels(
        segments,
        citation_segments(quote, DELEGATED_FAMILIES, **edges),
        quote,
        remove=set(item["remove"]),
        may_add=set(may_add),
    )
    if moved:
        return "label_moved", ", ".join(moved)
    # A run without stored evidence has no label to cite (``knowledge``).
    if item["kind"] in _BASIS_LABEL_KINDS and not item.get("knowledge"):
        unlabeled = [
            segment.kind
            for segment in segments
            if segment.kind != "heading"
            and segment_is_new(segment, quote, DELEGATED_FAMILIES)
            and not set(segment.labels) & set(may_add)
        ]
        if unlabeled:
            return "segment_without_basis_label", (
                f"{len(unlabeled)} new {'/'.join(dict.fromkeys(unlabeled))} segment(s) "
                f"without one of: {', '.join(may_add) or 'no basis label'}"
            )
    return None


def _moved_labels(
    segments: Sequence[CitationSegment],
    quoted: Sequence[CitationSegment],
    quote: str,
    *,
    remove: set[str],
    may_add: set[str],
) -> list[str]:
    """Labels that left the sentence they stood on.

    A segment that is not new carries only the labels of the quote
    sentence(s) holding it (all of the quote's when no single one does),
    less the removed ones, plus the basis labels. And a quote sentence that
    alone holds such segments keeps each of its labels, less the removed
    ones, on at least one of them: a surviving sentence keeps its label, a
    split sentence keeps it on one part, a rewritten sentence is new and may
    carry it anew.
    """
    everything = {label for segment in quoted for label in segment.labels}
    owned: dict[int, list[CitationSegment]] = {}
    moved: list[str] = []
    for segment in segments:
        if segment_is_new(segment, quote, DELEGATED_FAMILIES):
            continue
        holders = [
            index for index, sentence in enumerate(quoted)
            if segment_within(segment, sentence.text, DELEGATED_FAMILIES)
        ]
        standing = (
            {label for index in holders for label in quoted[index].labels}
            if holders else everything
        )
        allowed = (standing - remove) | may_add
        moved.extend(label for label in segment.labels if label not in allowed)
        if len(holders) == 1:
            owned.setdefault(holders[0], []).append(segment)
    for index, group in owned.items():
        carried = {label for segment in group for label in segment.labels}
        moved.extend(
            label for label in quoted[index].labels
            if label not in remove and label not in carried
        )
    return list(dict.fromkeys(moved))


def _composition_problem(
    original: str,
    spans: Sequence[tuple[int, int, str]],
    references: list[dict[str, Any]],
    identifiers: Sequence[str],
    user_text: str,
) -> tuple[str, str] | None:
    """The first whole-text check *original* with *spans* spliced in fails."""
    composed = _spliced(original, spans)
    if original.strip() and not composed.strip():
        return "output_emptied", "the output would be empty"
    held = _prose_words(original)
    if held >= OUTPUT_KEEP_WORDS:
        kept = _prose_words(composed)
        if kept < OUTPUT_KEEP_SHARE * held:
            return "output_shrunk", f"{kept} of {held} words"
    unknown = set(
        unknown_citation_labels(composed, references, families=DELEGATED_FAMILIES)
    ) - set(unknown_citation_labels(original, references, families=DELEGATED_FAMILIES))
    grown = Counter(
        malformed_citation_brackets(composed, families=DELEGATED_FAMILIES)
    ) - Counter(malformed_citation_brackets(original, families=DELEGATED_FAMILIES))
    changed = _untouched_citations_changed(original, spans, composed)
    if unknown or grown or changed:
        return "labels_whole_text", ", ".join([*sorted(unknown), *grown, *changed])
    added = added_identifiers(
        composed,
        introduced_identifiers(composed, list(identifiers), authored_by=user_text),
        replacing=original,
    )
    if added:
        return "identifier_added", ", ".join(added)
    return None


def _untouched_citations_changed(
    original: str, spans: Sequence[tuple[int, int, str]], composed: str
) -> list[str]:
    """Labels whose citations in the text the edits left alone read differently.

    Each citation of *original* outside every replaced passage must still
    count at its shifted place in *composed*, and no citation may appear
    there that the original did not count: a replacement that opens a fence,
    leaves a lone backtick or escapes the bracket after it changes what the
    untouched text cites.
    """
    ordered = sorted(spans)
    placed = spliced_positions(ordered)

    def moved(start: int, end: int) -> tuple[int, int] | None:
        offset = 0
        for span_start, span_end, replacement in ordered:
            if end <= span_start:
                break
            if start < span_end:
                return None
            offset += len(replacement) - (span_end - span_start)
        return start + offset, end + offset

    before = {
        (*position, label)
        for start, end, label in citation_spans(original, DELEGATED_FAMILIES)
        if (position := moved(start, end)) is not None
    }
    after = {
        (start, end, label)
        for start, end, label in citation_spans(composed, DELEGATED_FAMILIES)
        if not any(start < new_end and new_start < end for new_start, new_end in placed)
    }
    return sorted({label for *_position, label in before ^ after})
