"""The table of a document analysis: one state per document and field.

The pure part of the agent's ``analyze_document`` tool, beside the window
sweep of :mod:`inqtrix.agents.document_analysis`: the rules a call's input
must meet, the cells built from the server's reading facts, the record a run
keeps of one analysis and the text the agent's model reads. Nothing here
imports the kernel, so the module moves with the analysis when it becomes an
operation of its own.

A cell's state is a server fact over the verified passages and the reading
(:func:`table_cells`). Which field a passage serves, the window model says
(its ``criterion``); the server only checks that the number names a field.
No quote, claim or verdict of a window call leaves this module: the record
keeps every finding by its id, place and evidence id, and the text the model
reads carries states, reasons and evidence labels.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from inqtrix.agents.document_analysis import (
    FINDINGS_PER_WINDOW,
    DocumentAnalysisReport,
    coverage_lines,
    document_complete,
    reading_blockers,
)
from inqtrix.untrusted_content import neutralize_fence_delimiters, untrusted_fence

ANALYSIS_TOOL_NAME = "analyze_document"
"""The tool whose stored messages carry the reading records."""

RECORD_KEY = "document_analysis"
"""Key of the reading record in the artifact of the tool's message."""

RECORD_SCHEMA = 1

FIELDS_MAX = FINDINGS_PER_WINDOW
"""Fields one call may name. A window contributes at most this many
passages for all fields together, and a window that names that many counts
as possibly holding more, which keeps every cell of its document from
saying "not found"; more fields per call would make that the rule."""

FIELD_CHARS_MAX = 200
OBJECTIVE_CHARS_MAX = 1_000
"""Bounds of the texts every window call repeats."""

EVIDENCE_TEXTS_MAX = 12
EVIDENCE_TEXT_CHARS_MAX = 24_000
"""Verified section texts one result shows in full, by count and by
characters; every further one is listed with its reference id."""

PASSAGES = "passages"
NOT_FOUND = "not_found"
NOT_FOUND_IN_VERIFIED_PASSAGES = "not_found_in_verified_passages"
NOT_READ = "not_read"
CELL_STATES = (PASSAGES, NOT_FOUND, NOT_FOUND_IN_VERIFIED_PASSAGES, NOT_READ)
"""The states of a cell, exactly one per document and field (:func:`table_cells`)."""

UNASSIGNED = "passages_without_field"
"""Reason of an empty cell whose document holds verified passages that name
no field: such a passage may answer the field, so the cell claims no
absence."""

REASON_WORDS: dict[str, str] = {
    "window_limit": "the window limit stopped the reading",
    "provider_error": "the model provider failed",
    "not_called_after_failure": "not analysed after a provider failure",
    "content_refused": "the provider's content filter refused a window",
    "extraction_failed": "a window's analysis did not validate",
    "cancelled": "the analysis was stopped",
    "not_read": "not read",
    "document_failed": "the document could not be read",
    "no_text": "the document has no extracted text",
    "documents_not_selected": "found documents were not analysed",
    "pages_without_text": "pages without a text layer are not part of the text",
    "extraction_not_recorded": "the extraction coverage was not recorded",
    "not_in_window": "a passage the reading named was not verbatim in its window",
    "too_short": "a passage the reading named was too short to cite",
    "too_long": "a passage the reading named was too long to cite",
    "spans_chunks": "a passage the reading named crosses a section boundary",
    "chunk_unverified": "a passage the reading named stands in no verified section",
    "chunk_stale": "a passage the reading named belongs to another version",
    "passages_over_limit": "a window named more passages than it may contribute",
    "windows_possibly_more": "a window may hold further passages",
    "analysis_stopped": "the analysis was stopped",
    UNASSIGNED: "verified passages of this document name no field",
}
"""Every reason a cell can name, in words for the model; a code missing here
is named by its code (:func:`reason_words`)."""


@dataclass(frozen=True)
class AnalysisRequest:
    """A call's input after its rules: ids, objective and fields, trimmed."""

    document_ids: tuple[str, ...]
    objective: str
    fields: tuple[str, ...]


def _field_key(field: str) -> str:
    return " ".join(field.split()).casefold()


def checked_request(
    document_ids: Sequence[str],
    objective: str,
    fields: Sequence[str],
    *,
    document_limit: int,
) -> AnalysisRequest | str:
    """The call's input after its rules, or the sentence that refuses it.

    Ids are trimmed, none empty, none twice, at most *document_limit*. The
    objective is not empty and at most :data:`OBJECTIVE_CHARS_MAX`
    characters. One to :data:`FIELDS_MAX` fields, each one line, not empty,
    at most :data:`FIELD_CHARS_MAX` characters, none repeating another in
    spacing or case (two fields alike split one passage's answer).
    """
    ids = [str(item).strip() for item in document_ids]
    if not ids:
        return "Name at least one document id."
    if any(not item for item in ids):
        return "A document id is empty."
    for item in ids:
        if ids.count(item) > 1:
            return f"Document {item} is named more than once; name each document once."
    if len(ids) > document_limit:
        return f"One call analyses at most {document_limit} documents; split them over several calls."
    clean = objective.strip()
    if not clean:
        return "The objective is empty; say what the analysis is for."
    if len(clean) > OBJECTIVE_CHARS_MAX:
        return f"The objective has more than {OBJECTIVE_CHARS_MAX} characters; state it shorter."
    names = [str(item).strip() for item in fields]
    if not names:
        return "Name at least one field."
    if len(names) > FIELDS_MAX:
        return f"One call takes at most {FIELDS_MAX} fields; split them over several calls."
    keys: list[str] = []
    for number, name in enumerate(names, start=1):
        if not name:
            return f"Field {number} is empty."
        if len(name.splitlines()) != 1:
            return f"Field {number} spans several lines; name each field in one line."
        if len(name) > FIELD_CHARS_MAX:
            return f"Field {number} has more than {FIELD_CHARS_MAX} characters."
        key = _field_key(name)
        if key in keys:
            return f"Field {number} repeats field {keys.index(key) + 1}; name each field once."
        keys.append(key)
    return AnalysisRequest(tuple(ids), clean, tuple(names))


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def table_cells(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One cell per analysed document and field, in document order, then field order.

    ``passages`` when the reading named a verified passage of the document
    for the field. The server knows that much, not whether the passage
    states the field: a passage can also show that the document does not,
    so what the document says stands in the passage's text. Such a cell
    also carries the ``reasons`` of the limits of its document's reading (a
    named passage that did not become evidence, a verified passage that
    names no field): with one, the passages alone never show that the
    document lacks the field. Without a passage: ``not_read`` when the reading of THIS document is incomplete
    (a limit of the kind ``coverage``) or, in a stored mission report, its task stopped the analysis;
    ``not_found_in_verified_passages`` when a passage the reading named did
    not become evidence, or a verified passage of the document names no
    field; ``not_found`` otherwise, that is after a complete reading with
    every named passage verified. A limit of another document never changes
    a cell of this one.
    """
    fields = list(record.get("questions") or [])
    blockers = reading_blockers(dict(record))
    stopped = bool(record.get("stopped"))
    findings = list(record.get("findings") or [])
    cells: list[dict[str, Any]] = []
    for document in record.get("documents") or []:
        document_id = str(document.get("document_id") or "")
        own = [blocker for blocker in blockers if blocker["document_id"] == document_id]
        coverage = [blocker for blocker in own if blocker["blocks"] == "coverage"]
        evidence = [blocker for blocker in own if blocker["blocks"] != "coverage"]
        passages = [finding for finding in findings if finding.get("document_id") == document_id]
        unassigned = any(not finding.get("criteria") for finding in passages)
        for number in range(1, len(fields) + 1):
            hits = [finding for finding in passages if number in (finding.get("criteria") or [])]
            cell: dict[str, Any] = {"document_id": document_id, "field": number}
            if hits:
                cell |= {
                    "state": PASSAGES,
                    "reference_ids": _unique(str(finding.get("reference_id") or "") for finding in hits),
                }
                limits = _unique(
                    [*(blocker["reason"] for blocker in evidence), *([UNASSIGNED] if unassigned else [])]
                )
                if limits:
                    cell["reasons"] = limits
            elif coverage or stopped:
                cell |= {
                    "state": NOT_READ,
                    "reasons": _unique(
                        [*(blocker["reason"] for blocker in coverage), *(["analysis_stopped"] if stopped else [])]
                    ),
                    "source_bound": any(blocker["source_bound"] for blocker in coverage),
                }
            elif evidence or unassigned:
                cell |= {
                    "state": NOT_FOUND_IN_VERIFIED_PASSAGES,
                    "reasons": _unique(
                        [*(blocker["reason"] for blocker in evidence), *([UNASSIGNED] if unassigned else [])]
                    ),
                }
            else:
                cell["state"] = NOT_FOUND
            cells.append(cell)
    return cells


_MODEL_TEXT_OF_REPORT = {"summary", "summary_status", "criteria", "open_points"}
_MODEL_TEXT_OF_FINDING = ("quote", "claim")


def reading_record(
    report: DocumentAnalysisReport,
    *,
    objective: str,
    references: Mapping[tuple[str, int], str],
    call_id: str = "",
) -> dict[str, Any]:
    """The run's record of one analysis: the report without model text, and its cells.

    The findings keep their id, document, window, field numbers, section and
    page and the reference id the run registered for their section
    (*references*, by document id and section index); their quotes and
    claims stay out, as do the reduce's summary, verdicts and open points.
    *call_id* names the tool call, so the review can name the record.
    """
    data = report.model_dump(exclude=_MODEL_TEXT_OF_REPORT)
    data["findings"] = [
        {
            **{key: value for key, value in finding.items() if key not in _MODEL_TEXT_OF_FINDING},
            "reference_id": references.get((str(finding["document_id"]), int(finding["chunk_index"])), ""),
        }
        for finding in data["findings"]
    ]
    data["schema"] = RECORD_SCHEMA
    data["call_id"] = call_id
    data["objective"] = objective
    data["cells"] = table_cells(data)
    return data


def records_of(artifacts: Iterable[Any]) -> list[dict[str, Any]]:
    """The reading records among tool artifacts, in order (malformed ones skipped)."""
    records: list[dict[str, Any]] = []
    for artifact in artifacts:
        record = artifact.get(RECORD_KEY) if isinstance(artifact, Mapping) else None
        if isinstance(record, Mapping) and isinstance(record.get("documents"), list):
            records.append(dict(record))
    return records


def windows_granted(records: Iterable[Mapping[str, Any]]) -> int:
    """Windows the run's analyses claimed from its window budget, read or not."""
    return sum(max(0, int(record.get("windows_granted") or 0)) for record in records)


def analysed_documents(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every document the run's analyses opened, as reading facts.

    One entry per analysis and document that did not fail:
    ``{document_id, version, total_chars, complete, analysed_ranges,
    unanalysed, windows_planned, windows_analysed}``.
    """
    entries: list[dict[str, Any]] = []
    for record in records:
        for document in record.get("documents") or []:
            if document.get("failure") or not document.get("version"):
                continue
            entries.append(
                {
                    "document_id": str(document.get("document_id") or ""),
                    "version": str(document.get("version") or ""),
                    "total_chars": int(document.get("total_chars") or 0),
                    "complete": document_complete(document),
                    "analysed_ranges": [list(pair) for pair in document.get("analysed_ranges") or []],
                    "unanalysed": [
                        {"start": int(gap["start"]), "end": int(gap["end"]), "reason": str(gap["reason"])}
                        for gap in document.get("unanalysed") or []
                    ],
                    "windows_planned": int(document.get("windows_planned") or 0),
                    "windows_analysed": int(document.get("windows_analysed") or 0),
                }
            )
    return entries


def reason_words(reason: str) -> str:
    """One reason of a cell in words for the model, or its code."""
    return REASON_WORDS.get(reason, reason)


def evidence_split(
    references: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """The references whose text a result shows in full, and the rest.

    In order, while both bounds hold (:data:`EVIDENCE_TEXTS_MAX`,
    :data:`EVIDENCE_TEXT_CHARS_MAX`); the first reference that would cross
    one goes to the rest with every later one, so the shown ones are a
    prefix of the order.
    """
    shown: list[Mapping[str, Any]] = []
    used = 0
    for index, reference in enumerate(references):
        size = len(str(reference.get("excerpt") or ""))
        if len(shown) >= EVIDENCE_TEXTS_MAX or used + size > EVIDENCE_TEXT_CHARS_MAX:
            return shown, list(references[index:])
        shown.append(reference)
        used += size
    return shown, []


def _cell_line(cell: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    state = cell["state"]
    if state == PASSAGES:
        marks = ", ".join(
            f"[{labels[reference_id]}]" if reference_id in labels else reference_id
            for reference_id in cell.get("reference_ids") or []
        )
        limits = "; ".join(reason_words(str(reason)) for reason in cell.get("reasons") or [])
        return f"verified passages {marks}" + (f"; limits of this document's reading: {limits}" if limits else "")
    if state == NOT_FOUND:
        return "not found (the document was read completely and every passage the reading named was verified)"
    reasons = "; ".join(reason_words(str(reason)) for reason in cell.get("reasons") or [])
    if state == NOT_READ:
        bound = "; a re-reading of this version returns this again" if cell.get("source_bound") else ""
        return f"not read: {reasons}{bound}"
    return f"not found in the verified passages: {reasons}"


def analysis_text(
    report: DocumentAnalysisReport,
    record: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    evidence_blocks: Sequence[str],
    overflow_lines: Sequence[str],
    scope_documents: int | None,
    run_windows: tuple[int, int] | None,
) -> str:
    """What the agent's model reads of one analysis: server facts, labels, evidence text.

    Documents are named by number and id; a title is document data and
    stands only inside its fence. *labels* maps a reference id to the
    evidence label the run gave it; *evidence_blocks* are the shown
    evidence texts and *overflow_lines* the listed rest, both rendered by
    the caller. *scope_documents* is the number of documents in the run's
    collections when known; *run_windows* is ``(used, limit)`` of the run's
    window budget, given when a window limit stopped a reading.
    """
    fields = list(record.get("questions") or [])
    documents = list(record.get("documents") or [])
    cells = list(record.get("cells") or [])
    coverage = coverage_lines(report).splitlines()
    lines = [
        f"Document analysis by the server: {len(documents)} document(s), {len(fields)} field(s). "
        "Every state below is a server fact over what the server read and verified; which field a "
        "passage serves, the window model said.",
        "Fields:",
        *(f"{number}. {neutralize_fence_delimiters(field)}" for number, field in enumerate(fields, start=1)),
    ]
    for number, document in enumerate(documents, start=1):
        document_id = str(document.get("document_id") or "")
        lines += [
            "",
            f"Document {number} (id {document_id}); its title, as document data:",
            untrusted_fence(str(document.get("title") or ""), "knowledge-base"),
        ]
        reading = next(
            (line.split(": ", 1)[1] for line in coverage if line.startswith(f"- Document {number}: ")), ""
        )
        if reading:
            lines.append(f"Reading: {reading}")
        for cell in cells:
            if cell["document_id"] == document_id:
                lines.append(f"- Field {cell['field']}: {_cell_line(cell, labels)}")
        unassigned = _unique(
            str(finding.get("reference_id") or "")
            for finding in record.get("findings") or []
            if finding.get("document_id") == document_id and not finding.get("criteria")
        )
        if unassigned:
            marks = ", ".join(f"[{labels[rid]}]" if rid in labels else rid for rid in unassigned)
            lines.append(f"Verified passages of this document that name no field: {marks}")
    if evidence_blocks:
        lines += ["", "Evidence (the verified text of each cited section):", *evidence_blocks]
    if overflow_lines:
        lines += [
            "",
            "Further evidence, not shown here; read it with read_evidence and its reference_id:",
            *overflow_lines,
        ]
    lines.append("")
    if scope_documents is not None:
        lines.append(
            f"The run's collections hold {scope_documents} document(s); this call analysed "
            f"{len(documents)}. Say nothing about the documents it did not analyse."
        )
    if run_windows is not None:
        used, limit = run_windows
        lines.append(
            f"Windows of this run: {used} of {limit} used. A further call reads its documents "
            "again from the first window."
        )
    lines.append(
        "What a document says about a field stands in the text of the verified passages of its "
        "cell; a passage can also be named for a field to show that the document does not state "
        "it. Say that a document does not state something only for a cell in the state not_found, "
        "or for a cell with verified passages and no limit of the reading, of a document read "
        "completely, whose text you have read (shown above, or with read_evidence) and which do not "
        "state it; name every other state and every limit with its reason."
    )
    return "\n".join(lines)


def analysis_failure(report: DocumentAnalysisReport, halted: BaseException | None) -> str | None:
    """The failure code of a call that delivered no reading, else ``None``.

    ``document_unreadable`` when no document opened; ``provider_error`` when
    the provider failed before any window was analysed. A reading cut short
    after its first window is a result, with its limits in its cells.
    """
    if not any(not document.failure for document in report.documents):
        return "document_unreadable"
    if halted is not None and not any(document.windows_analysed for document in report.documents):
        return "provider_error"
    return None


def also_read(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[tuple[int, int]]]:
    """The ranges the run's analyses read of each document version, for the coverage ledger.

    Keyed like a ledger entry (``knowledge_document``, document id,
    version); only documents whose reading did not fail. What the server
    analysed is no text the model read: the ledger counts these ranges only
    where it asks whether a source is still open.
    """
    ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for entry in analysed_documents(records):
        key = ("knowledge_document", entry["document_id"], entry["version"])
        ranges.setdefault(key, []).extend((int(start), int(end)) for start, end in entry["analysed_ranges"])
    return ranges


_JUDGE_STATES = {
    PASSAGES: "verified passage",
    NOT_FOUND: "not found after a complete reading",
    NOT_FOUND_IN_VERIFIED_PASSAGES: "not found in the verified passages",
    NOT_READ: "not read",
}


def digest_lines(
    records: Iterable[Mapping[str, Any]], *, labels: Mapping[str, str] | None = None,
) -> list[str]:
    """What the run's analyses read and found, for the coverage judge.

    One line per document the server read, then one per field with its
    state and, for verified passages, their evidence labels (*labels* maps
    a reference id to its label). The judge sees only the start of each
    evidence text; a whole section the analysis cited holds its passage
    anywhere, so the cell says for which field the reading named it. A
    document the server read is read, though the model saw only its
    verified passages.
    """
    labels = labels or {}
    lines: list[str] = []
    for record in records:
        fields = list(record.get("questions") or [])
        cells = list(record.get("cells") or [])
        for entry in analysed_documents([record]):
            version = entry["version"] if len(entry["version"]) <= 16 else entry["version"][:16] + "…"
            total = entry["total_chars"]
            if entry["complete"]:
                state = f"read completely ({total} chars)"
            else:
                ranges = ", ".join(f"[{start}, {end})" for start, end in entry["analysed_ranges"]) or "nothing"
                gaps = ", ".join(f"[{gap['start']}, {gap['end']}) ({gap['reason']})" for gap in entry["unanalysed"])
                state = f"{ranges} of {total} chars" + (f"; not analysed {gaps}" if gaps else "")
            lines.append(
                f"- knowledge_document {entry['document_id']} (version {version}): analysed by the server: {state}"
            )
            for cell in cells:
                if cell.get("document_id") != entry["document_id"] or not 1 <= int(cell["field"]) <= len(fields):
                    continue
                marks = " ".join(
                    f"[{labels[reference_id]}]"
                    for reference_id in cell.get("reference_ids") or []
                    if reference_id in labels
                )
                found = _JUDGE_STATES.get(str(cell["state"]), str(cell["state"]))
                field = neutralize_fence_delimiters(" ".join(str(fields[int(cell["field"]) - 1]).split()))
                lines.append(f"  field \"{field}\": {found}" + (f" {marks}" if marks else ""))
    return lines
