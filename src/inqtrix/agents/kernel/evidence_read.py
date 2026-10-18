"""Read retained evidence through the kernel's existing ownership boundary.

Selections come from registered references or retained search ids, never from
model-supplied run ids or URLs. Source passages and provider synthesis remain
distinct. The same projection serves tool reads and final review.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Iterable

from inqtrix.agents.critic_phase import (
    DIAGNOSTIC_BASIS_PREFIX,
    SOURCE_BASIS_PREFIX,
)
from inqtrix.agents.document_analysis import (
    document_complete,
    document_reader_names,
    has_reading,
    reading_blockers,
    reading_predicates,
)
from inqtrix.agents.evidence_context import (
    project_evidence_context,
    reference_origins,
)
from inqtrix.i18n import t
from inqtrix.knowledge.retrieval_warnings import (
    search_notice_lines,
    stored_search_notices,
)

if TYPE_CHECKING:
    from inqtrix.agents.kernel.deps import KernelDeps

log = logging.getLogger("inqtrix")

EVIDENCE_READ_WINDOW_CHARS = 20_000
"""Material characters per explicit evidence read, independent of bulk offload.

The reply publishes the actual range and continuation. Offsets count Unicode
code points, not bytes or estimated tokens; every continuation checks the
rendered document's fingerprint after the caller reauthorizes its origins.
"""


def evidence_read_document(context: dict[str, Any]) -> str:
    """Put retained text before provenance without repeating its large fields.

    The original projection/ledger stays intact. Metadata links to named material
    sections where an identical field has already been rendered in full. Other
    fields, including citation mappings and historical usage, remain available.

    A role sees internal identifiers here BY CONSTRUCTION, and that is not a
    defect to fix in this function: the material header addresses each
    source by ``reference_id``/url/document_id so a continuation can name
    what it is reading, and the provenance section is the reference record
    serialized whole. The single-call evidence cards can name an internal
    document by title alone (``evidence_context.internal_reference_name``);
    this transport cannot, because reading IS addressing. What keeps the
    ids out of the reader's text is therefore the writer guard alone
    (``report_quality.introduced_identifiers``, applied at every writing
    site) — never the assumption that a writer was shown none.
    """
    metadata = deepcopy(context)
    material: list[str] = []
    for index, source in enumerate(metadata.get("sources", []), start=1):
        ref = source["reference"]
        identity = ref.get("reference_id") or ref.get("url") or ref.get("document_id") or str(index)
        for field, aliases, label in (
            ("source_excerpt", ("excerpt", "source_text"), "Stored source excerpt"),
            ("provider_context", ("grounded_support",), "Stored provider excerpt (no original source excerpt)"),
        ):
            text = source.get(field)
            if not isinstance(text, str) or not text:
                continue
            material_id = f"source-{index}-{field}"
            material.append(
                f"## {material_id}: {label}\n"
                f"Evidence: {ref.get('label') or ''} ({identity})\n"
                f"Source: {ref.get('title') or ref.get('url') or ''}\n\n{text}"
            )
            source.pop(field)
            source[field + "_material"] = material_id
            for alias in aliases:
                if ref.get(alias) == text:
                    ref.pop(alias)
                    ref[alias + "_material"] = material_id
    searches = metadata.get("searches", {})
    if isinstance(searches, dict):
        for index, (query_id, search) in enumerate(searches.items(), start=1):
            answer = search.get("provider_answer")
            if not isinstance(answer, str) or not answer:
                continue
            material_id = f"search-{index}"
            material.append(
                f"## {material_id}: Stored provider search answer "
                "(no original source excerpt)\n"
                f"query_id: {query_id}\n"
                f"Origin run: {search.get('source_run_id') or ''}\n"
                f"Search question: {search.get('query') or ''}\n\n{answer}"
            )
            search.pop("provider_answer")
            search["provider_answer_material"] = material_id
    if not material and "references" not in context:
        material.append(
            "No stored original excerpts or provider answers in this "
            "selection; only metadata and, where present, snippets."
        )
    material.append(
        "## Provenance and mapping\n"
        "Fields with _material point to the text sections named above. "
        "Usage and time data belong to the stored origin.\n\n"
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    )
    return "\n\n".join(material)


def evidence_read_window(
    context: dict[str, Any], *, offset: int = 0, content_hash: str = "",
) -> dict[str, Any]:
    """Read a bounded, version-checked range of an already authorized selection."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer.")
    document = evidence_read_document(context)
    fingerprint = hashlib.sha256(document.encode("utf-8")).hexdigest()
    if (offset > 0 or content_hash) and content_hash != fingerprint:
        raise ValueError(
            "The evidence selection or its content has changed, or the "
            "content_hash is missing. Read the same selection again from "
            "offset=0 without content_hash; do not mix different states."
        )
    if offset >= len(document) and offset != 0:
        raise ValueError("offset lies outside the evidence selection.")
    end = min(offset + EVIDENCE_READ_WINDOW_CHARS, len(document))
    return {
        "content": document[offset:end],
        "content_hash": fingerprint,
        "offset": offset,
        "end": end,
        "total_chars": len(document),
        "next_offset": end if end < len(document) else None,
    }




def retained_search_ledger(
    deps: KernelDeps, run_id: str, result: dict[str, Any],
) -> dict[str, Any]:
    """Read material after the caller has authorized this run's result.

    Research exports its ledger with the report. Agent runs retain it in
    their evidence artifact, which is also the Canvas provenance source.
    Missing material stays empty; storage failures reach the caller.
    """
    from inqtrix.agents.control_ports import ArtifactNotFound
    from inqtrix.agents.kernel.deps import run_coro

    ledger = result.get("web_search_ledger")
    if isinstance(ledger, dict):
        return ledger
    try:
        record, _ = run_coro(deps.control.get_artifact(
            run_id, f"art_{run_id[-12:]}_evidence",
        ))
    except ArtifactNotFound:
        return {}
    ledger = record.payload.get("web_search_ledger")
    return ledger if isinstance(ledger, dict) else {}


def load_retained_sources(
    deps: KernelDeps, source_run_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Authorize each retained origin before importing its completed result.

    Origins are supplied by trusted reference/ledger records. Missing origins
    are returned explicitly so callers also exclude stale local projections.
    The current run's own ledger needs no terminal result to be readable, and
    neither does the ledger a role's handover released: that run is the
    parent the role works for, parked on it while it reads.
    """
    loaded: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []
    store = getattr(deps.run_service, "run_store", None)
    for run_id in dict.fromkeys(source_run_ids):
        # Neither this run's own ledger nor the one its handover released
        # needs a completed result: the released run is the parent this
        # role works for, parked on the role while it reads. Demanding its
        # finished result would shut the role out of the very evidence it
        # was handed - its visibility was checked when it was hydrated.
        if run_id and run_id in (deps.run_id, deps.released_evidence_run_id):
            continue
        try:
            if store is None:
                raise LookupError("retained result reader unavailable")
            summary = store.get(run_id, visible_to=deps.visible_to)
            result = store.result(run_id, visible_to=deps.visible_to)
            if not isinstance(summary, dict) or not isinstance(result, dict):
                raise LookupError("invalid retained result")
            if summary.get("status") != "completed":
                raise LookupError("origin has no completed result")
            ledger = retained_search_ledger(deps, run_id, result)
        except Exception as exc:  # noqa: BLE001 — a visible partial read
            log.warning(
                "Evidence provenance unavailable (error_type=%s).",
                type(exc).__name__,
            )
            unavailable.append({
                "source_run_id": run_id,
                "status": "unavailable",
                "reason": "Origin no longer readable or not completed.",
            })
            continue
        if ledger:
            deps.register_web_search_ledger(ledger)
        loaded[run_id] = {"summary": summary, "result": result}
    return loaded, unavailable






def project_review_sources(
    references: list[dict[str, Any]],
    web_search_ledger: dict[str, Any],
    *,
    unavailable: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    """The review's view of references: every projected source names its basis id.

    Engine-neutral: a finding names its ground as ``source:<reference_id>``
    (:data:`~inqtrix.agents.critic_phase.SOURCE_BASIS_PREFIX`), whichever
    engine's ledger the reference comes from.
    """
    from inqtrix.agents.kernel.deps import KernelDeps

    context = project_evidence_context(
        references, web_search_ledger, unavailable=list(unavailable),
    )
    for item in context["sources"]:
        reference_id = KernelDeps.ledger_id(item["reference"])
        # A ledger that files by content hash stores no id on the record;
        # the projection names it, so text and basis id meet in one place.
        item["reference"] = {**item["reference"], "reference_id": reference_id}
        item["basis_id"] = SOURCE_BASIS_PREFIX + reference_id
    return context


READING_RECORD_METHOD = (
    "What the server read in one whole-document analysis of a subtask; server "
    "facts only. documents lists the documents analysed; documents_found counts "
    "every document the subtask's searches found (documents_not_selected of "
    "them were not selected; already_read ones an earlier round read, or "
    "could not open, and were not read here), out of collection_documents in "
    "the searched scope when that count is known. already_read lists found "
    "documents an earlier round of this run already read (state read_to_end, "
    "budget: a re-read would exceed the run's reading budget, beyond_reread: "
    "a re-read cannot reach what stayed unread); an entry with a failure was "
    "never read: every earlier reading of it failed before its first section "
    "with that code, and it was not tried again. This subtask read none of "
    "them, so its criteria were not asked of them. text_read_to_end: every "
    "character of the stored text "
    "of that version was analysed. document_complete: in addition, nothing is "
    "known to be missing from that stored text (no page without a text layer, "
    "extraction recorded). passages_verified: passages checked verbatim against "
    "the document and tied to a stored text section with a verified position; "
    "verified_sources names those sections. passages_not_taken_over: named "
    "passages that did not become evidence, by status - not_in_window (not "
    "verbatim in the section read), too_long and too_short (not checked), "
    "spans_chunks (verbatim, but across two stored text sections), "
    "chunk_unverified (verbatim, but no stored section with a verified position "
    "holds it), chunk_stale (only a section of an older version holds it). "
    "windows_possibly_more: sections of the reading that named the most "
    "passages one section may contribute, or reported more. absence_blocked_by: "
    "everything that prevents the statement that these documents hold nothing "
    "further on the subtask's criteria; its row documents_not_selected counts "
    "both the documents not selected and the already_read ones. source_bound "
    "marks a limit of the stored document version that every re-reading "
    "returns again."
)
"""Describes the reading record; the rule how a review may use it is part
of the trusted review rubric, never of the fenced diagnostic."""


def document_analysis_diagnostic(
    report: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    title: str,
    objective: str,
    evidence: list[dict[str, Any]],
    language: str,
) -> dict[str, Any] | None:
    """The reading record of one document analysis as a review diagnostic.

    The one builder for both engines. Server facts of the stored report
    only: no summary, claim, quote or verdict of the analysis model. The
    task's title and objective and the report's questions (what every
    window was asked) are plan text; document titles and plan text have
    their fence delimiters defused. ``language`` gives the record its reader
    title and names the document of every blocker, so a review that quotes a
    limit can say WHICH document it belongs to without printing an id.
    ``None`` when the record is no reading (:func:`~inqtrix.agents.
    document_analysis.has_reading`); a record that found only documents an
    earlier round already read is one.
    """
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.untrusted_content import neutralize_fence_delimiters

    if not has_reading(report):
        return None
    documents = list(report.get("documents") or [])
    not_selected = list(report.get("not_selected") or [])
    already_read = list(report.get("already_read") or [])
    complete, absence_supported = reading_predicates(report)
    # The reader name per document id, the same one the task's own lines use.
    names = document_reader_names(report, language)
    return {
        "basis_id": f"{DIAGNOSTIC_BASIS_PREFIX}{run_id}:{task_id}:document_analysis",
        "title": t(
            {"language": language},
            "review_diagnostic_document_analysis",
            title=neutralize_fence_delimiters(title),
        ),
        "source_run_id": run_id,
        "mission_run_id": run_id,
        "task_id": task_id,
        "scope": "document_analysis",
        "method": READING_RECORD_METHOD,
        "values": {
            "task": {
                "task_id": task_id,
                "title": neutralize_fence_delimiters(title),
                "objective": neutralize_fence_delimiters(objective),
                "criteria": [
                    neutralize_fence_delimiters(str(item)) for item in report.get("questions") or []
                ],
            },
            "documents": [
                {
                    "document_id": str(document.get("document_id") or ""),
                    "title": neutralize_fence_delimiters(str(document.get("title") or "")),
                    "version": str(document.get("version") or ""),
                    "characters": int(document.get("total_chars") or 0),
                    "failure": str(document.get("failure") or ""),
                    "text_read_to_end": bool(document.get("text_read_to_end")),
                    "document_complete": document_complete(document),
                    "windows_analysed": int(document.get("windows_analysed") or 0),
                    "windows_planned": int(document.get("windows_planned") or 0),
                    "unanalysed_ranges": [
                        {"start": gap["start"], "end": gap["end"], "reason": gap["reason"]}
                        for gap in document.get("unanalysed") or []
                    ],
                    "extraction_status": str(document.get("extraction_status") or "none"),
                    "pages": document.get("pages"),
                    "pages_without_text": list(document.get("pages_without_text") or []),
                    "passages_verified": int(document.get("findings_cited") or 0),
                    "passages_not_taken_over": dict(document.get("findings_rejected") or {}),
                    "passages_over_limit": int(document.get("findings_over_limit") or 0),
                    "windows_possibly_more": int(document.get("windows_possibly_more") or 0),
                }
                for document in documents
            ],
            "verified_sources": sorted(
                {SOURCE_BASIS_PREFIX + KernelDeps.ledger_id(ref)
                 for ref in evidence if KernelDeps.ledger_id(ref)}
            ),
            "already_read": [
                {
                    "document_id": str(entry.get("document_id") or ""),
                    "title": neutralize_fence_delimiters(str(entry.get("title") or "")),
                    "version": str(entry.get("version") or ""),
                    "state": str(entry.get("state") or ""),
                    "failure": str(entry.get("failure") or ""),
                }
                for entry in already_read
            ],
            "documents_found": len(documents) + len(not_selected) + len(already_read),
            "documents_not_selected": len(not_selected),
            "collection_documents": report.get("collection_documents"),
            "complete": complete,
            "absence_supported": absence_supported,
            # The reader copy of the blockers: the same rows, each with the
            # document a reader can name. A limit of the whole analysis has
            # no document and keeps the empty title it already carries.
            "absence_blocked_by": [
                {**blocker, "document_title": names.get(blocker["document_id"], "")}
                for blocker in reading_blockers(report)
            ],
            # Reader sentences in English, like every diagnostic value: the
            # review reads what the searches reported, never a code.
            "retrieval_warnings": search_notice_lines(
                stored_search_notices(report.get("retrieval_warnings")), "en"
            ),
        },
    }


def build_review_evidence(
    deps: KernelDeps,
    references: list[dict[str, Any]],
    *,
    mission_run_ids: list[str],
    language: str,
) -> dict[str, Any]:
    """One review snapshot, with existing result/task diagnostics kept scoped.

    ``language`` is the answer language of the run under review: every
    diagnostic carries the title a reader sees when the verdict names it.
    """
    from inqtrix.agents.control_ports import PlanNotFound
    from inqtrix.agents.kernel.deps import run_coro
    from inqtrix.untrusted_content import neutralize_fence_delimiters

    words = {"language": language}
    origins = list(dict.fromkeys([*reference_origins(references), *mission_run_ids]))
    loaded, unavailable = load_retained_sources(deps, origins)
    context = project_review_sources(
        references, deps.web_search_ledger, unavailable=unavailable,
    )
    diagnostics: list[dict[str, Any]] = []
    for run_id, data in loaded.items():
        metrics = data["result"].get("metrics")
        if isinstance(metrics, dict) and metrics.get("evidence_contract_status"):
            diagnostics.append({
                "basis_id": DIAGNOSTIC_BASIS_PREFIX + run_id,
                "title": t(words, "review_diagnostic_research_run"),
                "source_run_id": run_id, "scope": "research_run",
                "values": {key: metrics[key] for key in (
                    "evidence_contract_status", "confidence", "single_source_ratio",
                    "omitted_evidence_record_count", "omitted_evidence_query_count",
                ) if key in metrics},
            })
        if data["summary"].get("mode") != "workspace_agent":
            continue
        try:
            _, tasks = run_coro(deps.control.get_plan(run_id))
        except PlanNotFound:
            continue
        except Exception as exc:  # noqa: BLE001 — retain an explicit diagnostic gap
            log.warning("Task diagnostics could not be read (error_type=%s).", type(exc).__name__)
            context["unavailable_origins"].append({
                "source_run_id": run_id, "status": "diagnostics_unavailable",
                "reason": "Task diagnostics not readable.",
            })
            continue
        for task in tasks:
            values = task.result_payload or {}
            analysis = values.get("document_analysis") or {}
            if has_reading(analysis):
                reading = document_analysis_diagnostic(
                    analysis,
                    run_id=run_id,
                    task_id=task.task_id,
                    title=task.title,
                    objective=task.objective or task.title,
                    evidence=[dict(ref) for ref in values.get("evidence") or [] if isinstance(ref, dict)],
                    language=language,
                )
                if reading is not None:
                    diagnostics.append(reading)
            # Only a LIMIT is an open point; a duplicate collapse protects
            # the evidence and is no finding for the review.
            notes = search_notice_lines(
                stored_search_notices(values.get("search_notices")), "en",
                severity="limit",
            )
            if (
                not values.get("evidence_contract_status")
                and not values.get("omitted_evidence_count")
                and not notes
            ):
                continue
            diagnostics.append({
                "basis_id": f"{DIAGNOSTIC_BASIS_PREFIX}{run_id}:{task.task_id}",
                "title": t(
                    words, "review_diagnostic_mission_task",
                    title=neutralize_fence_delimiters(task.title),
                ),
                "source_run_id": task.child_run_id, "mission_run_id": run_id,
                "task_id": task.task_id, "scope": "mission_task",
                "values": {
                    **{key: values[key] for key in (
                        "evidence_contract_status", "confidence", "single_source_ratio",
                        "omitted_evidence_count", "omitted_evidence_query_count",
                    ) if key in values},
                    # What the task's searches reported as a limit, as
                    # reader sentences in English like the rest of this
                    # review's diagnostics.
                    **({"search_notes": notes} if notes else {}),
                },
            })
    context["run_diagnostics"] = diagnostics
    return context


def run_reading_diagnostics(
    run_id: str,
    entries: Iterable[dict[str, Any]],
    *,
    titles: dict[str, str],
    language: str,
) -> list[dict[str, Any]]:
    """What a run itself read of each source version, as review diagnostics.

    One diagnostic per entry of the run's coverage ledger
    (:meth:`~inqtrix.agents.context_sources.SourceCoverageLedger.entries`)
    whose source a review may re-read and of which the run transferred any
    text: the ranges it read, the ranges it left and whether it read the
    whole text. The record states what the server transferred, never what
    the run understood. *titles* names a source by its id; an untitled
    source is named by its id.
    """
    from inqtrix.agents.kernel.work_profiles import RELEASED_SOURCE_KINDS
    from inqtrix.untrusted_content import neutralize_fence_delimiters

    words = {"language": language}
    diagnostics: list[dict[str, Any]] = []
    for entry in entries:
        if entry["kind"] not in RELEASED_SOURCE_KINDS or not entry["read_units"]:
            continue
        source_id = str(entry["source_id"])
        version = str(entry["version"] or "")
        digest = hashlib.sha1(version.encode("utf-8")).hexdigest()[:8]
        diagnostics.append({
            "basis_id": (
                f"{DIAGNOSTIC_BASIS_PREFIX}{run_id}:reading:{entry['kind']}:{source_id}:{digest}"
            ),
            "title": t(
                words, "review_diagnostic_run_reading",
                title=neutralize_fence_delimiters(titles.get(source_id) or source_id),
            ),
            "source_run_id": run_id, "scope": "run_reading",
            "values": {
                "source": entry["kind"], "version": version, "unit": entry["unit"],
                "total": entry["total"],
                "read_ranges": [list(pair) for pair in entry["read_ranges"]],
                "unread_ranges": [list(pair) for pair in entry["unread_ranges"]],
                "read_completely": bool(entry["complete"]),
            },
        })
    return diagnostics


def own_reading_outputs(
    output_ids: Iterable[str], canvases: dict[str, Any], *, run_id: str,
) -> set[str]:
    """The outputs a run's own reading record belongs to: its chat answer and
    the canvases this run wrote. A canvas a delegated run wrote (a mission's
    memo) carries that run's reading record, never this one's."""
    return {
        output_id for output_id in output_ids
        if output_id == "chat"
        or getattr(canvases.get(output_id), "run_id", "") == run_id
    }


def narrow_review_evidence(
    evidence: dict[str, Any], basis_ids: Iterable[str],
) -> dict[str, Any]:
    """The review snapshot cut down to the bases the findings name.

    The revision repairs named passages from named bases. The whole ledger
    adds nothing to that and costs as much as the review itself.
    """
    wanted = set(basis_ids)
    sources = [item for item in evidence["sources"] if item.get("basis_id") in wanted]
    query_ids = {
        str(context.get("query_id") or "")
        for item in sources
        for context in item.get("citation_contexts") or []
    }
    return {
        **evidence,
        "sources": sources,
        "searches": {
            key: row for key, row in (evidence.get("searches") or {}).items()
            if key in query_ids
        },
        "run_diagnostics": [
            item for item in evidence.get("run_diagnostics") or []
            if item.get("basis_id") in wanted
        ],
    }


def read_registered_evidence(
    deps: KernelDeps, *, reference_id: str = "", query_id: str = ""
) -> dict[str, Any]:
    """Index retained evidence or resolve one registered selection."""
    if reference_id and query_id:
        raise ValueError("Choose reference_id OR query_id, not both.")
    if reference_id:
        references = deps.resolve_reference_ids([reference_id])
        _, unavailable = load_retained_sources(deps, reference_origins(references))
        return project_evidence_context(
            references, deps.web_search_ledger, unavailable=unavailable,
        )
    with deps._evidence_lock:
        searches = deepcopy(deps.web_search_ledger.get("searches") or {})
        references = deepcopy(list(deps.evidence_refs.values()))
    if query_id:
        if query_id not in searches:
            raise ValueError("Unknown query_id; read the evidence index first.")
        selected = searches[query_id]
        _, unavailable = load_retained_sources(
            deps, [selected["source_run_id"]] if selected.get("source_run_id") else [],
        )
        if unavailable:
            return {"sources": [], "searches": {}, "unavailable_origins": unavailable}
        # Only the explicitly selected search can introduce additional known
        # references. Reading does not assign claim verification or answer use.
        refs = [
            {
                "url": item.get("url"),
                "title": item.get("title"),
                "provider_snippet": item.get("snippet"),
                "grounded_support": item.get("grounded_support"),
            }
            for item in (selected.get("citations") or [])
            if isinstance(item, dict) and item.get("url")
        ]
        registered = deps.register_web_search_ledger(
            {"searches": {query_id: selected}}, refs,
        )
        context = project_evidence_context(
            registered, {"searches": {query_id: selected}},
        )
        # A source-less provider response is still readable, but not citable.
        context["searches"] = {query_id: selected}
        return context
    return {
        "references": [
            {key: ref.get(key) for key in (
                "reference_id", "label", "title", "url", "document_id",
                "source_run_ids", "query_ids",
            )}
            for ref in references
        ],
        "searches": [
            {"query_id": key, "query": row.get("query"), "status": row.get("status")}
            for key, row in searches.items() if isinstance(row, dict)
        ],
    }
