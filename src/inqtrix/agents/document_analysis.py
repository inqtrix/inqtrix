"""Whole-document analysis as a server-driven window sweep.

When a task needs documents read completely, no model has a reading
decision left to make: the server knows every window. The analysis is
therefore single structured calls over server-fetched windows (map), a
server-side verbatim check of every quote against the window the model saw
and the document's verified chunks (verify), and one call over the verified
findings only (reduce). Coverage, extraction gaps and citability are server
facts, not model discipline:

- every window of a selected document is analysed unless a named limit, a
  failure or a cancellation stops it, and a range counts as analysed only
  after its extraction validated;
- no model output changes what is read: the capabilities one analysis
  invokes are fixed (:data:`ANALYSIS_CAPABILITIES`);
- a quote becomes evidence only through ``knowledge.document.cite``;
- a window call sees only its own window: an instruction in one window
  reaches that window's extraction and, as a verified finding, the reduce —
  never another window's reading, the choice of evidence or a later prompt;
- an absence ("nothing answers the task") is claimed only when the reading
  was complete AND nothing verbatim was lost on the way to evidence.

Engine-neutral: the caller supplies the :class:`AnalysisPorts`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from inqtrix.agents.context_archive import ARCHIVE_READ_WINDOW_CHARS
from inqtrix.agents.context_sources import merge_ranges, unread_ranges
from inqtrix.agents.patterns._structured import structured_call
from inqtrix.agents.phase_models import DocumentAnalysisResult, WindowExtraction
from inqtrix.agents.prompts import (
    DOCUMENT_REDUCE_FRAME,
    build_document_analyst_system_prompt,
    document_analyst_frame,
    build_document_reduce_prompt,
    build_window_extraction_prompt,
)
from inqtrix.untrusted_content import neutralize_fence_delimiters
from inqtrix.agents.scheduler import run_in_pool
from inqtrix.capabilities.catalog.knowledge import CITE_QUOTE_CHARS_MAX, CITE_QUOTES_MAX
from inqtrix.capabilities.contracts import CapabilityError
from inqtrix.exceptions import AgentCancelled
from inqtrix.provider_failure_contract import classify_provider_failure
from inqtrix.i18n import t, t_count
from inqtrix.knowledge.grounding import quote_key
from inqtrix.quote_limits import CITE_QUOTE_MIN_WORDS
from inqtrix.knowledge.retrieval_warnings import (
    search_notice_lines,
    stored_search_notices,
)

log = logging.getLogger("inqtrix")

ANALYSIS_NODE = "agent_file_analysis"
"""The routing node of every sweep call (map and reduce)."""

READ_CAPABILITY = "knowledge.document.read"
CITE_CAPABILITY = "knowledge.document.cite"
ANALYSIS_CAPABILITIES = frozenset({READ_CAPABILITY, CITE_CAPABILITY})
"""The only capabilities an analysis invokes; the choice of documents is the
caller's (for the mission: its task's search)."""

WINDOW_OVERLAP_CHARS = 1_000
"""Characters two neighbouring windows share, so a passage cut at one
window's edge stands whole in the next (a quotable sentence is shorter)."""

WINDOW_STEP_CHARS = ARCHIVE_READ_WINDOW_CHARS - WINDOW_OVERLAP_CHARS
"""Characters one window advances over the previous one."""

FINDINGS_PER_WINDOW = 5
"""Passages one window contributes at most. The extraction prompt asks for
this bound and the server keeps it, counting any surplus visibly; at the
default window budget (40) the reduce prompt stays below ~64,000 tokens."""

UNCITABLE_STATUSES = frozenset({"spans_chunks", "chunk_unverified", "chunk_stale"})
"""Citation outcomes of a quote that IS verbatim in its window: the passage
exists, only no verified chunk of the current content carries it."""


@dataclass(frozen=True)
class Reason:
    """What one named limit of a reading blocks.

    ``coverage``: part of the document is not known to be analysed, so
    neither completeness nor absence can be claimed. ``evidence``: the text
    was analysed, but a passage the reading named did not become evidence,
    so absence cannot be claimed. ``source_bound``: a property of the stored
    document version; every re-reading of that version returns it again.
    """

    blocks: str
    source_bound: bool = False


REASONS: dict[str, Reason] = {
    "window_limit": Reason("coverage"),
    "provider_error": Reason("coverage"),
    "not_called_after_failure": Reason("coverage"),
    "content_refused": Reason("coverage"),
    "extraction_failed": Reason("coverage"),
    "cancelled": Reason("coverage"),
    "not_read": Reason("coverage"),
    "document_failed": Reason("coverage"),
    "no_text": Reason("coverage"),
    "documents_not_selected": Reason("coverage"),
    "pages_without_text": Reason("coverage", source_bound=True),
    "extraction_not_recorded": Reason("coverage", source_bound=True),
    "not_in_window": Reason("evidence"),
    "too_short": Reason("evidence"),
    "too_long": Reason("evidence"),
    "spans_chunks": Reason("evidence"),
    "chunk_unverified": Reason("evidence"),
    "chunk_stale": Reason("evidence"),
    "passages_over_limit": Reason("evidence"),
    "windows_possibly_more": Reason("evidence"),
    "analysis_stopped": Reason("evidence"),
}
"""Every limit a reading can name, and what it blocks. A reason missing here
blocks as much as its kind can (see :func:`reading_blockers`): the table is
closed on the blocking side."""

_ANALYST_SYSTEM_PROMPT = build_document_analyst_system_prompt(
    document_analyst_frame(
        passages_max=FINDINGS_PER_WINDOW,
        quote_chars_max=CITE_QUOTE_CHARS_MAX,
        quote_min_words=CITE_QUOTE_MIN_WORDS,
    )
)
"""The window call's system prompt, stating the limits the server enforces."""


def window_plan(total: int) -> list[tuple[int, int]]:
    """``[start, end)`` of every window of a *total*-character text (pure).

    Windows are :data:`ARCHIVE_READ_WINDOW_CHARS` long and overlap by
    :data:`WINDOW_OVERLAP_CHARS`: 4 windows for 66,389 characters, 32 for
    602,381.
    """
    if total <= 0:
        return []
    starts = [0]
    while starts[-1] + ARCHIVE_READ_WINDOW_CHARS < total:
        starts.append(starts[-1] + WINDOW_STEP_CHARS)
    return [(start, min(total, start + ARCHIVE_READ_WINDOW_CHARS)) for start in starts]


@dataclass(frozen=True)
class AnalysisPorts:
    """What an engine lends one analysis.

    ``invoke`` runs one capability under the run's boundary and raises
    :class:`CapabilityError`; ``cancelled`` is the RUN's cancellation (the
    analysis raises :class:`AgentCancelled`); ``stopped`` is the TASK's
    (the analysis stops reading and returns what it has — the caller
    discards it); ``claim_windows`` grants up to *n* windows from the run's
    budget (thread-safe, the caller's bookkeeping); ``emit`` publishes one
    activity payload.
    """

    invoke: Callable[[str, dict[str, Any]], Any]
    llm: Any
    model: str | None
    reasoning_effort: str | None
    timeout: float
    language: str
    cancelled: Callable[[], bool]
    stopped: Callable[[], bool]
    emit: Callable[[dict[str, Any]], None]
    claim_windows: Callable[[int], int]
    max_windows: int
    max_parallel: int


class UnanalysedRange(BaseModel):
    start: int
    end: int
    reason: str


class AnalysedDocument(BaseModel):
    """What the server read of one document, and what came of it."""

    document_id: str
    title: str
    version: str = ""
    total_chars: int = 0
    windows_planned: int = 0
    windows_analysed: int = 0
    analysed_ranges: list[list[int]] = Field(default_factory=list)
    unanalysed: list[UnanalysedRange] = Field(default_factory=list)
    text_read_to_end: bool = False
    """Every character of the stored text of this version analysed. Not the
    same as the document being complete (see :func:`document_complete`)."""
    failure: str = ""
    extraction_status: str = "none"
    pages: int | None = None
    pages_without_text: list[int] = Field(default_factory=list)
    findings: int = 0
    findings_cited: int = 0
    findings_rejected: dict[str, int] = Field(default_factory=dict)
    findings_over_limit: int = 0
    windows_possibly_more: int = 0
    """Windows that named the most passages one window may contribute, or
    reported more: further passages may stand there."""


class AnalysisFinding(BaseModel):
    """One VERIFIED finding: its quote is in its window and in a cited chunk."""

    id: str
    document_id: str
    window: int
    quote: str
    claim: str
    criterion: int | None = None
    chunk_index: int
    page_number: int | None = None


class DocumentAnalysisReport(BaseModel):
    """Server-built record of one analysis (persisted with the task).

    Counts, ranges, reasons and extraction facts are server facts. The
    quotes, claims, summary, verdicts and open points are model text over
    verified passages: rendered for the reader, never into another prompt.
    """

    documents: list[AnalysedDocument] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    """The criteria every window was asked (the caller's task questions)."""
    findings: list[AnalysisFinding] = Field(default_factory=list)
    not_selected: list[dict[str, str]] = Field(default_factory=list)
    """Documents the caller found but did not select (its document limit, or
    a re-read the analysis's window budget no longer fits)."""
    already_read: list[dict[str, str]] = Field(default_factory=list)
    """Found documents an earlier round of this run already read, not read
    again: ``{document_id, title, version, state, failure}`` with ``state``
    ``read_to_end``, ``budget`` (a re-read would exceed the run's windows) or
    ``beyond_reread`` (a re-read cannot reach what stayed unread). An entry
    with a ``failure`` was never read: every earlier reading of it failed
    before its first window with that code, and it was not tried again
    (:func:`never_read`). They count as not selected
    (:func:`reading_blockers`)."""
    document_limit: int = 0
    retrieval_warnings: list[dict[str, Any]] = Field(default_factory=list)
    """What the caller's searches reported, as text-free search notices
    (``knowledge.retrieval_warnings.SearchNotice.as_dict``)."""
    windows_granted: int = 0
    """Windows this analysis claimed from the run's budget (read or not)."""
    stopped: bool = False
    """The task was cancelled while the analysis ran; what it read is kept."""
    collection_documents: int | None = None
    """Documents in the scope the caller searched (``None``: not known)."""
    absence_blocked_by: list[dict[str, Any]] = Field(default_factory=list)
    """Every limit that keeps the analysis from claiming completeness or
    absence (:func:`reading_blockers`)."""
    complete: bool = False
    """Output of :func:`reading_predicates`, stored for the API."""
    absence_supported: bool = False
    """Output of :func:`reading_predicates`, stored for the API."""
    summary: str = ""
    summary_status: str = "skipped"
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    open_points: list[str] = Field(default_factory=list)


def document_complete(document: dict[str, Any]) -> bool:
    """The whole document analysed: its stored text read to the end, and
    nothing known to be missing from that text."""
    return (
        not document.get("failure")
        and bool(document.get("text_read_to_end"))
        and int(document.get("total_chars") or 0) > 0
        and not document.get("pages_without_text")
        and document.get("extraction_status") != "not_recorded"
    )


def reading_blockers(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every limit that keeps an analysis from claiming completeness or absence.

    One entry per (reason, document): ``{reason, document_id, count,
    blocks, source_bound}``; ``document_id`` is empty for a limit of the
    whole analysis. An empty list means the analysis may claim absence.
    ``documents_not_selected`` counts the found documents this analysis did
    not read: the ones it did not select and the ones an earlier round
    already read (their criteria were never asked of them).
    """
    blockers: list[dict[str, Any]] = []

    def add(reason: str, document_id: str, count: int, kind: str) -> None:
        if count <= 0:
            return
        row = REASONS.get(reason, Reason(kind))
        blockers.append(
            {
                "reason": reason,
                "document_id": document_id,
                "count": count,
                "blocks": row.blocks,
                "source_bound": row.source_bound,
            }
        )

    for document in report.get("documents") or []:
        document_id = str(document.get("document_id") or "")
        if document.get("failure"):
            add("document_failed", document_id, 1, "coverage")
            continue
        if int(document.get("total_chars") or 0) <= 0:
            add("no_text", document_id, 1, "coverage")
        gaps: dict[str, int] = {}
        for gap in document.get("unanalysed") or []:
            gaps[str(gap["reason"])] = gaps.get(str(gap["reason"]), 0) + 1
        for reason, count in gaps.items():
            add(reason, document_id, count, "coverage")
        add("pages_without_text", document_id, len(document.get("pages_without_text") or []), "coverage")
        add(
            "extraction_not_recorded",
            document_id,
            int(document.get("extraction_status") == "not_recorded"),
            "coverage",
        )
        for status, count in (document.get("findings_rejected") or {}).items():
            add(str(status), document_id, int(count), "evidence")
        add("passages_over_limit", document_id, int(document.get("findings_over_limit") or 0), "evidence")
        add("windows_possibly_more", document_id, int(document.get("windows_possibly_more") or 0), "evidence")
    add(
        "documents_not_selected",
        "",
        len(report.get("not_selected") or []) + len(report.get("already_read") or []),
        "coverage",
    )
    add("analysis_stopped", "", int(bool(report.get("stopped"))), "evidence")
    return blockers


def has_reading(report: Mapping[str, Any] | None) -> bool:
    """Whether *report* is a reading: it read documents, or found only
    documents an earlier round already read."""
    return bool(report) and bool(report.get("documents") or report.get("already_read"))


def never_read(entry: Mapping[str, Any]) -> bool:
    """Whether an ``already_read`` entry names a document no earlier reading
    could open: each failed before its first window, with ``failure``."""
    return bool(entry.get("failure"))


def reading_predicates(report: dict[str, Any]) -> tuple[bool, bool]:
    """``(complete, absence_supported)`` of an analysis record — the one source.

    Complete: every selected document analysed to its end with nothing known
    missing, and every found document selected — the predicate for gap
    coverage. Absence supported: complete, and every passage the reading
    named became evidence — the only case that allows the words "nothing
    answers the task".
    """
    blockers = reading_blockers(report)
    complete = bool(report.get("documents")) and not any(
        blocker["blocks"] == "coverage" for blocker in blockers
    )
    return complete, complete and not blockers


@dataclass
class DocumentAnalysisOutcome:
    report: DocumentAnalysisReport
    evidence: list[dict[str, Any]]
    usage: dict[str, int]
    halted: BaseException | None = None
    """The provider failure that stopped the window calls, if one did."""


@dataclass
class _Window:
    document: int
    number: int
    count: int
    start: int
    end: int
    text: str


def _add_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
    for key in ("prompt_tokens", "completion_tokens"):
        total[key] = total.get(key, 0) + int((usage or {}).get(key, 0) or 0)


def document_version(
    invoke: Callable[[str, dict[str, Any]], Any], document_id: str
) -> str | None:
    """The current version of *document_id*, read the way window 0 reads it.

    ``None`` when the store cannot say (a :class:`CapabilityError`, logged):
    ``''`` is the version of a reading that failed at window 0 and would
    match it. Every other error propagates.
    """
    try:
        output = invoke(READ_CAPABILITY, {"document_id": document_id, "offset": 0, "max_chars": 1})
    except CapabilityError as exc:
        log.warning(
            "Document selection: the current version of document %s is unavailable "
            "(code=%s); it stays a candidate.",
            document_id,
            exc.code,
        )
        return None
    return output.window.version


def _read_windows(
    ports: AnalysisPorts,
    document_id: str,
    title: str,
    budget: list[int],
) -> tuple[AnalysedDocument, int, list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    """Read one document's windows in order, pinned to window 0's version.

    Returns the document record, the windows claimed from the budgets, the
    windows read and the planned windows left unread with their reason.
    ``budget`` is the analysis's remaining allowance (one-element list,
    mutated). Claimed windows stay claimed even when a read fails, so the
    run's booking matches what was granted. A version change after window 0
    fails the whole document: a mix of two versions is no reading of either.
    """
    try:
        first = ports.invoke(
            READ_CAPABILITY,
            {"document_id": document_id, "offset": 0, "max_chars": ARCHIVE_READ_WINDOW_CHARS},
        )
    except CapabilityError as exc:
        return AnalysedDocument(document_id=document_id, title=title, failure=exc.code), 0, [], []
    window = first.window
    facts = first.extraction or {}
    document = AnalysedDocument(
        document_id=document_id,
        title=first.title,
        version=window.version,
        total_chars=window.total_chars,
        extraction_status=getattr(first, "extraction_status", "none"),
        pages=facts.get("pages"),
        pages_without_text=list(facts.get("pages_without_text") or []),
    )
    plan = window_plan(window.total_chars)
    document.windows_planned = len(plan)
    granted = ports.claim_windows(min(len(plan), budget[0])) if plan else 0
    budget[0] -= granted
    texts: list[tuple[int, int, str]] = []
    for number, (start, end) in enumerate(plan[:granted], start=1):
        if ports.cancelled():
            raise AgentCancelled("Document analysis cancelled.")
        if ports.stopped():
            break
        if number == 1:
            text = first.text[: end - start]
        else:
            try:
                output = ports.invoke(
                    READ_CAPABILITY,
                    {
                        "document_id": document_id,
                        "offset": start,
                        "max_chars": end - start,
                        "version": window.version,
                        "content_hash": window.content_hash,
                    },
                )
            except CapabilityError as exc:
                document.failure = (
                    "document_changed" if exc.code == "context.version_changed" else exc.code
                )
                return document, granted, [], []
            text = output.text
        texts.append((start, end, text))
    unread = [(start, end, "cancelled") for start, end in plan[len(texts):granted]]
    unread.extend((start, end, "window_limit") for start, end in plan[granted:])
    return document, granted, texts, unread


def _extract(
    ports: AnalysisPorts,
    job: _Window,
    *,
    objective: str,
    criteria: list[str],
    title: str,
    total: int,
) -> tuple[WindowExtraction | None, dict[str, int]]:
    outcome = structured_call(
        ports.llm,
        prompt=build_window_extraction_prompt(
            objective,
            criteria,
            title=title,
            text=job.text,
            start=job.start,
            end=job.end,
            total=total,
            index=job.number,
            count=job.count,
            language=ports.language,
        ),
        system=_ANALYST_SYSTEM_PROMPT,
        model_cls=WindowExtraction,
        node=ANALYSIS_NODE,
        model=ports.model,
        reasoning_effort=ports.reasoning_effort,
        timeout=ports.timeout,
    )
    value = outcome.value
    assert value is None or isinstance(value, WindowExtraction)
    return value, outcome.usage


def _gap_pieces(
    start: int, end: int, windows: list[tuple[int, int, str]]
) -> list[tuple[int, int, str]]:
    """One unanalysed gap cut into pieces, each with its window's reason.

    Windows overlap, so a position belongs to the FIRST unanalysed window
    covering it; a position no such window covers was simply not read.
    """
    pieces: list[tuple[int, int, str]] = []
    cursor = start
    for w_start, w_end, reason in sorted(windows):
        if w_end <= cursor or w_start >= end:
            continue
        if w_start > cursor:
            pieces.append((cursor, w_start, "not_read"))
            cursor = w_start
        stop = min(w_end, end)
        if stop > cursor:
            pieces.append((cursor, stop, reason))
            cursor = stop
    if cursor < end:
        pieces.append((cursor, end, "not_read"))
    merged: list[tuple[int, int, str]] = []
    for piece in pieces:
        if merged and merged[-1][2] == piece[2] and merged[-1][1] == piece[0]:
            merged[-1] = (merged[-1][0], piece[1], piece[2])
        else:
            merged.append(piece)
    return merged


def _close_coverage(
    document: AnalysedDocument, unanalysed_windows: list[tuple[int, int, str]]
) -> None:
    """Analysed ranges merged; every other character named with its reason.

    The gaps are the complement of the analysed ranges (a window's own
    bounds overstate what is missing, because windows overlap), cut where
    the reason changes.
    """
    analysed = merge_ranges(tuple(pair) for pair in document.analysed_ranges)
    document.analysed_ranges = [[start, end] for start, end in analysed]
    document.unanalysed = [
        UnanalysedRange(start=piece_start, end=piece_end, reason=reason)
        for gap_start, gap_end in unread_ranges(analysed, document.total_chars)
        for piece_start, piece_end, reason in _gap_pieces(gap_start, gap_end, unanalysed_windows)
    ]
    document.text_read_to_end = not document.unanalysed


def _document_labels(report: DocumentAnalysisReport) -> dict[str, str]:
    return {doc.document_id: f"Document {index}" for index, doc in enumerate(report.documents, start=1)}


def coverage_lines(report: DocumentAnalysisReport) -> str:
    """Server facts of an analysis in English (the reduce prompt's frame).

    Documents are named by number: their titles are user text and travel
    only inside the fence, with the findings.
    """
    labels = _document_labels(report)
    lines: list[str] = []
    for doc in report.documents:
        label = labels[doc.document_id]
        if doc.failure:
            lines.append(f"- {label}: not analysed ({doc.failure}).")
            continue
        record = doc.model_dump()
        if document_complete(record):
            state = f"read completely ({doc.total_chars} characters, {doc.windows_analysed} windows)"
        elif doc.text_read_to_end:
            state = f"stored text read to its end ({doc.total_chars} characters), but not shown to hold the whole document"
        else:
            state = (
                f"analysed {doc.analysed_ranges} of {doc.total_chars} characters; not analysed "
                + ", ".join(f"[{gap.start}, {gap.end}) ({gap.reason})" for gap in doc.unanalysed)
            )
        if doc.pages_without_text:
            state += (
                f"; {len(doc.pages_without_text)} of {doc.pages} pages have no text layer "
                f"(pages {', '.join(str(page) for page in doc.pages_without_text)})"
            )
        elif doc.extraction_status == "not_recorded":
            state += "; extraction coverage was not recorded"
        lost = {status: count for status, count in doc.findings_rejected.items() if count}
        if lost:
            state += "; named passages that did not become evidence: " + ", ".join(
                f"{count} {status}" for status, count in sorted(lost.items())
            )
        if doc.findings_over_limit or doc.windows_possibly_more:
            state += "; further relevant passages may stand beyond those taken over"
        lines.append(f"- {label}: {state}.")
    if report.not_selected:
        lines.append(f"- Found but not analysed: {len(report.not_selected)} further document(s).")
    failed = sum(1 for entry in report.already_read if never_read(entry))
    if len(report.already_read) > failed:
        lines.append(
            "- Found but not read again in this analysis (an earlier round of this run "
            "read them; this task's criteria were not asked of them): "
            f"{len(report.already_read) - failed} further document(s)."
        )
    if failed:
        lines.append(
            "- Found but not tried again in this analysis (an earlier round of this run "
            "could not read them; this task's criteria were not asked of them): "
            f"{failed} further document(s)."
        )
    return "\n".join(lines)


def analyse_documents(
    ports: AnalysisPorts,
    *,
    objective: str,
    criteria: list[str],
    documents: list[tuple[str, str]],
    not_selected: list[tuple[str, str, str]] | None = None,
    already_read: list[dict[str, str]] | None = None,
    document_limit: int = 0,
    retrieval_warnings: list[dict[str, Any]] | None = None,
    collection_documents: int | None = None,
) -> DocumentAnalysisOutcome:
    """Analyse *documents* (``(id, title)`` pairs) completely; see the module docstring.

    *not_selected* are documents the caller found but will not read, each
    ``(id, title, reason)``; *already_read* the found documents an earlier
    round already read, or could not open (``{document_id, title, version,
    state, failure}``). Both keep the analysis from counting as complete.
    With no *documents* nothing is read: no capability and no model call.
    Raises :class:`AgentCancelled` when the run is cancelled. A provider failure
    stops the remaining window calls (one failure, not N), keeps what was
    analysed and comes back as ``halted``: the report and its window booking
    survive even when nothing could be analysed, and the caller decides the
    task's outcome.
    """
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import operation_span
    from inqtrix.providers.base import provider_cancel_scope

    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    report = DocumentAnalysisReport(
        questions=list(criteria),
        not_selected=[
            {"document_id": document_id, "title": title, "reason": reason}
            for document_id, title, reason in (not_selected or [])
        ],
        already_read=[dict(entry) for entry in already_read or []],
        document_limit=document_limit,
        retrieval_warnings=list(retrieval_warnings or []),
        collection_documents=collection_documents,
    )
    with operation_span(
        semconv.SPAN_DOCUMENT_ANALYSIS,
        {semconv.INQTRIX_DOCUMENT_COUNT: len(documents)},
    ) as span, provider_cancel_scope(ports.cancelled):
        budget = [max(0, int(ports.max_windows))]
        jobs: list[_Window] = []
        unanalysed_windows: dict[int, list[tuple[int, int, str]]] = {}
        for index, (document_id, title) in enumerate(documents):
            document, granted, texts, unread = _read_windows(ports, document_id, title, budget)
            report.documents.append(document)
            report.windows_granted += granted
            unanalysed_windows[index] = list(unread)
            jobs.extend(
                _Window(index, number, document.windows_planned, start, end, text)
                for number, (start, end, text) in enumerate(texts, start=1)
            )
        outcomes, halted = _map_windows(
            ports, report, jobs, objective=objective, criteria=criteria, usage=usage
        )
        pending: dict[int, list[tuple[_Window, Any]]] = {}
        for job in jobs:
            document = report.documents[job.document]
            value, reason = outcomes[(job.document, job.number)]
            if value is None:
                unanalysed_windows[job.document].append((job.start, job.end, reason))
                continue
            document.windows_analysed += 1
            document.analysed_ranges.append([job.start, job.end])
            # A window that named as many passages as it may is saturated:
            # further ones may stand there even when it did not say so.
            document.windows_possibly_more += int(
                value.more_relevant or len(value.findings) >= FINDINGS_PER_WINDOW
            )
            kept = value.findings[:FINDINGS_PER_WINDOW]
            document.findings_over_limit += len(value.findings) - len(kept)
            document.findings += len(kept)
            pending.setdefault(job.document, []).extend((job, finding) for finding in kept)
        for index, document in enumerate(report.documents):
            if not document.failure:
                _close_coverage(document, unanalysed_windows[index])
        stopped = ports.stopped()
        evidence: list[dict[str, Any]] = []
        if not stopped:
            evidence = _cite_findings(ports, report, pending)
        for document in report.documents:
            # Settled after citing, which can still fail a document; a document
            # with no analysed window did not complete ("completed · 0/N").
            _emit_row(
                ports, document,
                "failed" if document.failure or not document.windows_analysed else "completed",
            )
        report.stopped = stopped
        record = report.model_dump()
        report.absence_blocked_by = reading_blockers(record)
        report.complete, report.absence_supported = reading_predicates(record)
        if report.findings and not stopped:
            _reduce(ports, report, objective=objective, criteria=criteria, usage=usage)
        _warn_limits(report)
        if span is not None:
            span.set_attribute(semconv.INQTRIX_DOCUMENT_WINDOWS_PLANNED, sum(d.windows_planned for d in report.documents))
            span.set_attribute(semconv.INQTRIX_DOCUMENT_WINDOWS_ANALYSED, sum(d.windows_analysed for d in report.documents))
            span.set_attribute(semconv.INQTRIX_DOCUMENT_QUOTES_CITED, len(report.findings))
            span.set_attribute(
                semconv.INQTRIX_DOCUMENT_QUOTES_REJECTED,
                sum(sum(d.findings_rejected.values()) for d in report.documents),
            )
            span.set_attribute(
                semconv.INQTRIX_DOCUMENT_PAGES_WITHOUT_TEXT,
                sum(len(d.pages_without_text) for d in report.documents),
            )
            span.set_attribute(
                semconv.INQTRIX_DOCUMENT_EXTRACTION_UNRECORDED,
                sum(d.extraction_status == "not_recorded" for d in report.documents),
            )
    return DocumentAnalysisOutcome(report=report, evidence=evidence, usage=usage, halted=halted)


def _emit_row(ports: AnalysisPorts, document: AnalysedDocument, status: str, *, current: int | None = None) -> None:
    """One live row per document, counted against ALL its planned windows:
    a budget stop settles at "4/32", never "4/4"."""
    ports.emit(
        {
            "kind": "reading",
            "operation": "document_analysis",
            "activity_id": f"document-analysis:{document.document_id}",
            "status": status,
            "detail": document.title,
            "document_id": document.document_id,
            "current": document.windows_analysed if current is None else current,
            "total": document.windows_planned,
        }
    )


def _map_windows(
    ports: AnalysisPorts,
    report: DocumentAnalysisReport,
    jobs: list[_Window],
    *,
    objective: str,
    criteria: list[str],
    usage: dict[str, int],
) -> tuple[dict[tuple[int, int], tuple[WindowExtraction | None, str]], BaseException | None]:
    """Extract every window; each result is ``(value, reason-when-none)``.

    Usage is booked as each call returns, so a failure later in the pool
    never drops the cost of the calls before it. A failure of the provider
    stops the calls not yet made (one failure, not N) and is returned; a
    content refusal of one window is that window's own reason and stops
    nothing — it is bound to that window's text, not to the provider.
    """
    lock = threading.Lock()
    halted: list[BaseException] = []
    processed = {index: 0 for index in range(len(report.documents))}

    def run_one(_position: int, job: _Window) -> tuple[_Window, WindowExtraction | None, str]:
        if ports.cancelled():
            raise AgentCancelled("Document analysis cancelled.")
        if ports.stopped():
            return job, None, "cancelled"
        with lock:
            if halted:
                return job, None, "not_called_after_failure"
        document = report.documents[job.document]
        try:
            value, call_usage = _extract(
                ports, job, objective=objective, criteria=criteria,
                title=document.title, total=document.total_chars,
            )
        except AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — one provider failure stops the rest, visibly
            if classify_provider_failure(exc).content_refused:
                log.warning(
                    "Document window %d of %d was refused by the provider's content "
                    "filter; only that window stays unanalysed.",
                    job.number,
                    job.count,
                )
                return job, None, "content_refused"
            with lock:
                halted.append(exc)
            log.warning(
                "Document window call failed (error_type=%s); the remaining windows of "
                "this analysis are not called.",
                type(exc).__name__,
            )
            return job, None, "provider_error"
        with lock:
            _add_usage(usage, call_usage)
        return job, value, "extraction_failed" if value is None else ""

    def fold(result: Any) -> None:
        job, _value, _reason = result
        processed[job.document] += 1
        _emit_row(ports, report.documents[job.document], "started", current=processed[job.document])

    results = run_in_pool(
        jobs,
        run_one,
        max(1, int(ports.max_parallel)),
        skipped=lambda _position, job: (job, None, "not_called_after_failure"),
        on_result=fold,
        thread_name_prefix="inqtrix-document-window",
    )
    outcomes = {(job.document, job.number): (value, reason) for job, value, reason in results}
    return outcomes, (halted[0] if halted else None)


def _cite_findings(
    ports: AnalysisPorts,
    report: DocumentAnalysisReport,
    pending: dict[int, list[tuple[_Window, Any]]],
) -> list[dict[str, Any]]:
    """Verify every finding through the cite capability; keep the cited ones.

    Quotes longer than the capability accepts are rejected here (named,
    counted) instead of failing the call; the rest travel in batches of
    what one call carries.
    """
    evidence: list[dict[str, Any]] = []
    cited_chunks: set[tuple[str, int]] = set()
    seen_quotes: set[tuple[str, str]] = set()
    for index, document in enumerate(report.documents):
        findings = pending.get(index, [])
        if not findings or document.failure:
            continue
        citable = []
        for job, finding in findings:
            if len(finding.quote) > CITE_QUOTE_CHARS_MAX:
                document.findings_rejected["too_long"] = document.findings_rejected.get("too_long", 0) + 1
            else:
                citable.append((job, finding))
        citations: list[Any] = []
        try:
            for batch_start in range(0, len(citable), CITE_QUOTES_MAX):
                batch = citable[batch_start : batch_start + CITE_QUOTES_MAX]
                citations.extend(
                    ports.invoke(
                        CITE_CAPABILITY,
                        {
                            "document_id": document.document_id,
                            "version": document.version,
                            "quotes": [
                                {"text": finding.quote, "start": job.start, "end": job.end}
                                for job, finding in batch
                            ],
                        },
                    ).citations
                )
        except CapabilityError as exc:
            document.failure = (
                "document_changed" if exc.code == "context.version_changed" else exc.code
            )
            continue
        for (job, finding), citation in zip(citable, citations, strict=True):
            if citation.status != "cited" or citation.evidence is None:
                document.findings_rejected[citation.status] = (
                    document.findings_rejected.get(citation.status, 0) + 1
                )
                continue
            key = (document.document_id, quote_key(finding.quote))
            if key in seen_quotes:
                continue  # the same passage again, from the window overlap
            seen_quotes.add(key)
            document.findings_cited += 1
            chunk = (document.document_id, int(citation.evidence["chunk_index"]))
            if chunk not in cited_chunks:
                cited_chunks.add(chunk)
                evidence.append(dict(citation.evidence))
            report.findings.append(
                AnalysisFinding(
                    id=f"F{len(report.findings) + 1}",
                    document_id=document.document_id,
                    window=job.number,
                    quote=finding.quote,
                    claim=finding.claim,
                    criterion=finding.criterion,
                    chunk_index=chunk[1],
                    page_number=citation.evidence.get("page_number"),
                )
            )
    return evidence


def _reduce(
    ports: AnalysisPorts,
    report: DocumentAnalysisReport,
    *,
    objective: str,
    criteria: list[str],
    usage: dict[str, int],
) -> None:
    labels = _document_labels(report)
    counts = {doc.document_id: doc.windows_planned for doc in report.documents}
    titles = "\n".join(f"{labels[doc.document_id]}: {doc.title}" for doc in report.documents)
    findings = titles + "\n\n" + "\n".join(
        f"{finding.id} ({labels[finding.document_id]}"
        + (f", page {finding.page_number}" if finding.page_number else "")
        + f", window {finding.window} of {counts[finding.document_id]}): "
        + f'"{finding.quote}" — {finding.claim}'
        for finding in report.findings
    )
    try:
        outcome = structured_call(
            ports.llm,
            prompt=build_document_reduce_prompt(
                objective,
                criteria,
                findings=findings,
                coverage=coverage_lines(report),
                language=ports.language,
            ),
            system=build_document_analyst_system_prompt(DOCUMENT_REDUCE_FRAME),
            model_cls=DocumentAnalysisResult,
            node=ANALYSIS_NODE,
            model=ports.model,
            reasoning_effort=ports.reasoning_effort,
            timeout=ports.timeout,
        )
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — the reduce is optional; the evidence stands
        report.summary_status = "failed"
        log.warning(
            "Document analysis reduce failed (error_type=%s); the verified findings "
            "stay as evidence.",
            type(exc).__name__,
        )
        return
    _add_usage(usage, outcome.usage)
    value = outcome.value
    if not isinstance(value, DocumentAnalysisResult):
        report.summary_status = "failed"
        log.warning(
            "Document analysis reduce failed; the verified findings stay as evidence."
        )
        return
    known = {finding.id for finding in report.findings}
    report.summary = value.summary
    report.summary_status = "ok"
    report.open_points = list(value.open_points)
    report.criteria = [
        {
            "criterion": verdict.criterion,
            "verdict": _server_verdict(
                verdict.verdict,
                [ref for ref in verdict.findings if ref in known],
                report.absence_blocked_by,
            ),
            "findings": [ref for ref in verdict.findings if ref in known],
        }
        for verdict in value.criteria
        if 1 <= verdict.criterion <= len(criteria)
    ]


def _server_verdict(verdict: str, findings: list[str], blockers: list[dict[str, Any]]) -> str:
    """The verdict the server can stand behind.

    An answer needs a verified passage behind it; an absence is a claim
    about the whole document and holds only when nothing blocks it. The
    downgrade names its cause: a part not analysed, or passages the reading
    named that did not become evidence.
    """
    if verdict in {"answered", "partly_answered"} and not findings:
        return "no_verified_passage"
    if verdict == "not_addressed" and blockers:
        if any(blocker["blocks"] == "coverage" for blocker in blockers):
            return "not_found_in_analysed_part"
        return "not_found_in_verified_passages"
    return verdict


def _warn_limits(report: DocumentAnalysisReport) -> None:
    """One WARNING per document with a limit, and one for the analysis's own
    limits — read through :func:`reading_blockers`, so the log names exactly
    what keeps the analysis from claiming completeness or absence."""
    by_document: dict[str, list[str]] = {}
    for blocker in reading_blockers(report.model_dump()):
        by_document.setdefault(blocker["document_id"], []).append(
            f"{blocker['reason']}={blocker['count']}"
        )
    for document_id, limits in by_document.items():
        if document_id:
            log.warning("Document analysis of %s left limits: %s", document_id, ", ".join(limits))
        else:
            log.warning(
                "Document analysis left limits: %s (document limit %d).",
                ", ".join(limits),
                report.document_limit,
            )


def _reason_words(words: dict[str, str], reason: str) -> str:
    key = f"document_analysis_reason_{reason.replace('.', '_')}"
    text = t(words, key)
    return t(words, "document_analysis_reason_technical", code=reason) if text == key else text


def _title(document: dict[str, Any], words: dict[str, str], number: int) -> str:
    """A document title for a reader line: user text, its fence delimiters defused.

    ``number`` is the document's place in this record — the analysed
    documents in reading order, then the ones no analysis selected, then the
    ones an earlier round already read. A
    document the upload left without a title is named by that number and
    never by its id: an id tells a reader nothing and reads as bookkeeping
    that leaked into the text.
    """
    title = str(document.get("title") or "").strip()
    return (
        neutralize_fence_delimiters(title)
        if title
        else t(words, "document_analysis_untitled_document", number=number)
    )


def _record_documents(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every document of one record: in reading order, then the unselected
    ones, then the ones an earlier round already read."""
    return [
        *(report.get("documents") or []),
        *(report.get("not_selected") or []),
        *(report.get("already_read") or []),
    ]


def _reader_titles(report: dict[str, Any], words: dict[str, str]) -> list[str]:
    """The reader name of every document of one record, in the record's order.

    THE numbering: every reader of a record takes its titles from here, so
    an untitled document cannot be "Nr. 2" in one line and "Nr. 3" in the
    next.
    """
    return [
        _title(document, words, number)
        for number, document in enumerate(_record_documents(report), start=1)
    ]


def document_reader_names(report: dict[str, Any], language: str) -> dict[str, str]:
    """Reader name per document id of one analysis record.

    For the records that address a document by its id (the reading
    diagnostic's blockers): they keep the id AND carry the name the task's
    own lines use, so both readers of the record say the same thing.
    """
    names: dict[str, str] = {}
    for document, title in zip(
        _record_documents(report), _reader_titles(report, {"language": language})
    ):
        document_id = str(document.get("document_id") or "")
        if document_id:
            names.setdefault(document_id, title)
    return names


def _loss_lines(words: dict[str, str], title: str, rejected: dict[str, int]) -> list[str]:
    """One true sentence per class of named passages that did not become evidence."""
    lines: list[str] = []
    uncitable = 0
    for status, count in sorted(rejected.items()):
        if not count:
            continue
        if status in UNCITABLE_STATUSES:
            uncitable += int(count)
        elif status == "not_in_window":
            lines.append(t_count(words, "document_analysis_not_verbatim", int(count), title=title))
        elif status == "too_long":
            lines.append(
                t_count(words, "document_analysis_unchecked_long", int(count), title=title,
                        limit=CITE_QUOTE_CHARS_MAX)
            )
        elif status == "too_short":
            lines.append(
                t_count(words, "document_analysis_unchecked_short", int(count), title=title,
                        min_words=CITE_QUOTE_MIN_WORDS)
            )
        else:
            lines.append(
                t_count(words, "document_analysis_unchecked_other", int(count), title=title, code=status)
            )
    if uncitable:
        lines.insert(0, t_count(words, "document_analysis_uncitable", uncitable, title=title))
    return lines


_ALREADY_READ_CODES = {
    "budget": "research_code_read_budget_exhausted",
    "beyond_reread": "research_code_source_bound_limit",
}
"""Why a document an earlier round read only in part is not read again, in
the words of the research prospect's codes."""


def analysis_notes(report: dict[str, Any], language: str) -> list[str]:
    """Reader lines naming what an analysis did NOT establish — server facts only.

    Empty when every found document was read completely, nothing is known
    to be missing from its text, every named passage became evidence and at
    least one was verified. These lines may reach later prompts; no model
    text ever enters them.
    """
    words = {"language": language}
    lines: list[str] = []
    documents = list(report.get("documents") or [])
    titles = _reader_titles(report, words)
    for title, document in zip(titles, documents):
        if document.get("failure"):
            lines.append(
                t(words, "document_analysis_failed", title=title,
                  reason=_reason_words(words, str(document["failure"])))
            )
            continue
        if int(document.get("total_chars") or 0) <= 0:
            lines.append(t(words, "document_analysis_no_text", title=title))
            continue
        if not document.get("text_read_to_end"):
            ranges = "; ".join(
                t(words, "document_analysis_range", start=gap["start"], end=gap["end"],
                  reason=_reason_words(words, str(gap["reason"])))
                for gap in document.get("unanalysed") or []
            )
            state = "document_analysis_read_partly" if document.get("windows_analysed") else "document_analysis_not_analysed"
            lines.append(t(words, state, title=title, ranges=ranges))
        missing = list(document.get("pages_without_text") or [])
        if missing:
            lines.append(
                t_count(words, "document_analysis_pages_without_text", len(missing), title=title,
                        pages=document.get("pages"), list=", ".join(str(page) for page in missing))
            )
        elif document.get("extraction_status") == "not_recorded":
            lines.append(t(words, "document_analysis_extraction_unrecorded", title=title))
        lines.extend(_loss_lines(words, title, dict(document.get("findings_rejected") or {})))
        more = int(document.get("windows_possibly_more") or 0) or int(bool(document.get("findings_over_limit")))
        if more:
            lines.append(t_count(words, "document_analysis_more", more, title=title, limit=FINDINGS_PER_WINDOW))
    not_selected = list(report.get("not_selected") or [])
    by_reason: dict[str, list[str]] = {}
    # The numbering runs on: an unselected document without a title is named
    # apart from every analysed one.
    for title, item in zip(titles[len(documents):], not_selected):
        by_reason.setdefault(str(item.get("reason") or ""), []).append(title)
    for reason, names in by_reason.items():
        if reason == "document_limit":
            lines.append(
                t_count(words, "document_analysis_not_selected_document_limit",
                        int(report.get("document_limit") or 0), titles=", ".join(names))
            )
        elif reason == "window_limit":
            # Only a re-read is left out for the window budget: an earlier
            # round read part of it, and a later round still can.
            lines.append(t(words, "document_analysis_reread_not_fitted", titles=", ".join(names)))
        else:
            lines.append(
                t(words, "document_analysis_not_selected_unknown_reason",
                  reason=_reason_words(words, reason), titles=", ".join(names))
            )
    already_read = list(report.get("already_read") or [])
    by_state: dict[str, list[str]] = {}
    by_failure: dict[str, list[str]] = {}
    for title, item in zip(titles[len(documents) + len(not_selected):], already_read):
        if never_read(item):
            # No earlier reading opened it: never "read", whatever its state.
            by_failure.setdefault(str(item["failure"]), []).append(title)
            continue
        by_state.setdefault(str(item.get("state") or ""), []).append(title)
    for state, names in by_state.items():
        if state == "read_to_end":
            lines.append(t(words, "document_analysis_already_read_to_end", titles=", ".join(names)))
            continue
        code = _ALREADY_READ_CODES.get(state)
        lines.append(
            t(words, "document_analysis_already_read_partly",
              reason=t(words, code) if code else _reason_words(words, state),
              titles=", ".join(names))
        )
    for failure, names in by_failure.items():
        lines.append(
            t(words, "document_analysis_already_failed",
              reason=_reason_words(words, failure), titles=", ".join(names))
        )
    in_scope = report.get("collection_documents")
    found = len(documents) + len(not_selected) + len(already_read)
    if isinstance(in_scope, int) and found < in_scope:
        # "the other one" presupposes a found document: nothing found is its
        # own sentence.
        lines.append(
            t_count(words, "document_analysis_scope_none", in_scope)
            if found == 0
            else t_count(
                words, "document_analysis_scope_found", in_scope - found, found=found,
                scope=t_count(words, "document_analysis_scope_documents", in_scope),
            )
        )
    limits = search_notice_lines(
        stored_search_notices(report.get("retrieval_warnings")), language, severity="limit"
    )
    if limits:
        lines.append(t(words, "document_analysis_retrieval_warnings", warnings=" ".join(limits)))
    if not report.get("findings") and any(not doc.get("failure") for doc in documents):
        _complete, absence_supported = reading_predicates(report)
        lines.append(
            t(words, "document_analysis_complete_negative")
            if absence_supported
            else t_count(words, "document_analysis_partial_negative", len(documents))
        )
    return lines


def analysis_facts(report: dict[str, Any], language: str) -> list[str]:
    """Reader lines of an analysis: what was read (positive facts), then its limits.

    Server facts only, like :func:`analysis_notes`; the one renderer behind
    the reader's answer and every prompt that needs to know what was read.
    An informative search notice (a duplicate collapse) is a fact here and
    never a limit.
    """
    words = {"language": language}
    lines: list[str] = []
    for title, document in zip(
        _reader_titles(report, words), report.get("documents") or []
    ):
        if document.get("failure") or int(document.get("total_chars") or 0) <= 0:
            continue
        chars = int(document.get("total_chars") or 0)
        if document_complete(document):
            lines.append(t(words, "document_analysis_read_completely", title=title, chars=chars))
        elif document.get("text_read_to_end"):
            lines.append(t(words, "document_analysis_text_read_to_end", title=title, chars=chars))
    informative = search_notice_lines(
        stored_search_notices(report.get("retrieval_warnings")), language, severity="info"
    )
    if informative:
        lines.append(t(words, "document_analysis_retrieval_warnings", warnings=" ".join(informative)))
    return lines + analysis_notes(report, language)


def analysis_summary(report: dict[str, Any], language: str) -> str:
    """The task result line — server-rendered, so it may enter later prompts."""
    words = {"language": language}
    documents = list(report.get("documents") or [])
    entries = list(report.get("already_read") or [])
    failed = sum(1 for entry in entries if never_read(entry))
    already_read = len(entries) - failed
    if not documents and entries:
        # Nothing was read here: the line says what the earlier rounds did,
        # read the documents or fail to open them.
        parts = []
        if already_read:
            parts.append(t_count(words, "document_analysis_summary_already_read", already_read))
        if failed:
            parts.append(
                t_count(words, "document_analysis_summary_already_failed_part", failed)
                if parts
                else t_count(words, "document_analysis_summary_already_failed", failed)
            )
        return " ".join(parts)
    readable = [doc for doc in documents if not doc.get("failure")]
    read = sum(bool(doc.get("text_read_to_end")) for doc in readable)
    parts = [t_count(words, "document_analysis_summary_read", len(documents), read=read)]
    not_whole = sum(
        1 for doc in readable if doc.get("text_read_to_end") and not document_complete(doc)
    )
    if not_whole:
        parts.append(
            t(words, "document_analysis_summary_extraction_single")
            if len(documents) == 1
            else t_count(words, "document_analysis_summary_extraction", not_whole)
        )
    parts.append(t_count(words, "document_analysis_summary_cited", len(report.get("findings") or [])))
    if already_read:
        parts.append(t_count(words, "document_analysis_summary_already_read_part", already_read))
    if failed:
        parts.append(t_count(words, "document_analysis_summary_already_failed_part", failed))
    return " ".join(parts)


def _verdict_words(words: dict[str, str], verdict: str, documents: int) -> str:
    if verdict == "not_addressed":
        return t_count(words, "document_analysis_verdict_not_addressed", documents)
    return t(words, f"document_analysis_verdict_{verdict}")


def analysis_answer(report: dict[str, Any], language: str) -> str:
    """The reader's task answer: summary, criteria, verified passages, reading facts.

    The model summary, claims and open points appear here for the READER
    only; no agent prompt reads a task's answer.
    """
    words = {"language": language}
    documents = {doc.get("document_id"): doc for doc in report.get("documents") or []}
    names = document_reader_names(report, language)
    parts = [analysis_summary(report, language)]
    if report.get("summary_status") == "ok" and report.get("summary"):
        parts.append(str(report["summary"]))
    elif report.get("summary_status") == "failed":
        parts.append(t(words, "document_analysis_summary_failed"))
    criteria = list(report.get("criteria") or [])
    if criteria:
        parts.append(
            f"**{t(words, 'document_analysis_criteria_heading')}**\n"
            + "\n".join(
                f"- {item['criterion']}: "
                + _verdict_words(words, str(item["verdict"]), len(documents))
                + (f" ({', '.join(item.get('findings') or [])})" if item.get("findings") else "")
                for item in criteria
            )
        )
    findings = list(report.get("findings") or [])
    if findings:
        unnamed = {
            str(finding["document_id"]) for finding in findings
            if str(finding["document_id"]) not in names
        }
        if unnamed:
            # A passage from a document this record does not list: the line
            # keeps the passage and says nothing about its origin.
            log.warning(
                "Document analysis answer: %d verified passage(s) name a "
                "document that is not in the record.", len(unnamed),
            )
        parts.append(
            f"**{t(words, 'document_analysis_passages_heading')}**\n"
            + "\n".join(
                f"- {finding['id']}: "
                + t(words, "document_analysis_quote", quote=finding["quote"])
                + (
                    " — " + names[str(finding["document_id"])]
                    if str(finding["document_id"]) in names
                    else ""
                )
                + (
                    ", " + t(words, "document_analysis_page", page=finding["page_number"])
                    if finding.get("page_number")
                    else ""
                )
                for finding in findings
            )
        )
    open_points = list(report.get("open_points") or [])
    if open_points:
        parts.append(
            f"**{t(words, 'document_analysis_open_points_heading')}**\n"
            + "\n".join(f"- {point}" for point in open_points)
        )
    facts = analysis_facts(report, language)
    if facts:
        parts.append("\n".join(f"- {line}" for line in facts))
    return "\n\n".join(parts)
