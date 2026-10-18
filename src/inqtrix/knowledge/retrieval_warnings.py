"""The one contract from Knowledge search codes to their two wordings.

Stores report source-integrity decisions as typed, text-free
``RetrievalExclusion`` values and candidate-stage limits as
``RetrievalDegradation`` values.  This module is the single projection from
those to stable search codes, and the single registry of what each code
says:

* the English API message, read by the synchronous search route, the Agent
  capability, the kernel's search tool and the discovery digest;
* the reader wording (i18n stem, both languages), read by the mission's task
  answer, the memo and review notes and the reading record of a review
  diagnostic.

A caller never spells a message or picks a wording of its own. Aggregating
the entries of several searches into :class:`SearchNotice` values happens
here too, by code — never by text, which would merge different counts and
lose them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal

from inqtrix.i18n import t, t_count
from inqtrix.knowledge.stores.ports import RetrievalExclusion

log = logging.getLogger("inqtrix")


@dataclass(frozen=True)
class KnowledgeRetrievalWarning:
    """Bounded, evidence-free warning safe for API, run and UI projection."""

    code: str
    message: str
    reason: str
    stage: str
    count: int
    recommended_action: str | None = None

    def as_dict(self, *, include_message: bool = True) -> dict[str, object]:
        return {
            "code": self.code,
            **({"message": self.message} if include_message else {}),
            "reason": self.reason,
            "stage": self.stage,
            "count": self.count,
            "recommended_action": self.recommended_action,
        }


NoticeFamily = Literal["exclusion", "degradation", "scope"]


@dataclass(frozen=True)
class _Notice:
    """One registry entry: family, API message(s) and reader-wording stem."""

    family: NoticeFamily
    message: str
    #: A degradation's message when the final evidence count was still
    #: reached; it claims nothing about completeness either.
    message_complete: str = ""
    #: i18n stem of the reader wording; empty for an API-only code.
    words: str = ""


_DEGRADATION = _Notice(
    family="degradation",
    message=(
        "Vector search reached a technical candidate limit before reaching "
        "the requested final evidence count."
    ),
    message_complete=(
        "The vector candidate pool did not reach the depth requested for "
        "final ranking; the full final evidence count was nevertheless reached."
    ),
    words="search_notice_degraded",
)

_NOTICES: dict[str, _Notice] = {
    "chunks_require_reindex": _Notice(
        family="exclusion",
        message=(
            "Results without verifiable original text were excluded and "
            "require reindexing."
        ),
        words="search_notice_reindex",
    ),
    "chunks_pending_reconciliation": _Notice(
        family="exclusion",
        message=(
            "Vector results without a canonical database record were "
            "excluded; index reconciliation must complete."
        ),
        words="search_notice_reconcile",
    ),
    "duplicate_documents_collapsed": _Notice(
        family="exclusion",
        message=(
            "Documents with identical content were collapsed into one so that "
            "multiple evidence slots do not carry the same passages."
        ),
        words="search_notice_duplicate",
    ),
    "retrieval_candidates_excluded": _Notice(
        family="exclusion",
        message=(
            "Results were excluded by a knowledge-source integrity check; "
            "result coverage may be incomplete."
        ),
        words="search_notice_excluded",
    ),
    "vector_overfetch_cap": _DEGRADATION,
    "vector_candidate_stalled": _DEGRADATION,
    "knowledge.no_collections": _Notice(
        family="scope",
        message=(
            "No knowledge collection is accessible to this run; there is "
            "nothing to search."
        ),
        words="search_notice_no_collections",
    ),
    # Only the synchronous search route narrows a request to its visible
    # collections; the Agent capability refuses instead, so no run reads it.
    "collections_filtered": _Notice(
        family="scope",
        message=(
            "Some requested collections are not visible and were excluded "
            "from the search."
        ),
    ),
}

#: Exclusion reason -> search code. A reason outside it is projected as
#: ``retrieval_candidates_excluded`` instead of being dropped.
_WARNING_CONTRACT: dict[str, str] = {
    "source_unverified": "chunks_require_reindex",
    "canonical_chunk_unavailable": "chunks_pending_reconciliation",
    "duplicate_document": "duplicate_documents_collapsed",
}

_UNKNOWN_EXCLUSION_CODE = "retrieval_candidates_excluded"

_UNREGISTERED_MESSAGE = (
    "The search reported a notice this server cannot classify; result "
    "coverage may be incomplete."
)
"""What an unregistered code says on the API surface. The code itself stays
in the payload and the log, never in reader text."""

#: The codes that report protection, not a limit: a hit skipped because the
#: same passages already came from a found source.
_INFORMATIVE = frozenset({"duplicate_documents_collapsed"})


def warning_message(code: str, *, final_evidence_complete: bool | None = None) -> str:
    """The English API message of *code*.

    A degradation names whether the final evidence count was reached, so it
    needs *final_evidence_complete*. A code outside the registry is a
    contract break an AST test catches before it ships; at runtime it must
    not fail the search that reported it, so it gets the general message and
    one WARNING.
    """
    notice = _NOTICES.get(code)
    if notice is None:
        log.warning(
            "Knowledge search code %r is not registered; the reader gets the "
            "general message.",
            code,
        )
        return _UNREGISTERED_MESSAGE
    if notice.family != "degradation":
        return notice.message
    if final_evidence_complete is None:
        raise ValueError(f"degradation code {code!r} needs final_evidence_complete")
    return notice.message_complete if final_evidence_complete else notice.message


def notice_family(code: str) -> NoticeFamily | None:
    """The registry family of *code*; ``None`` for a code outside it."""
    notice = _NOTICES.get(code)
    return notice.family if notice is not None else None


def notice_severity(code: str) -> Literal["limit", "info"]:
    """``info`` for a duplicate collapse only; everything else is a limit.

    A degradation that still reached its final evidence count is a limit
    too: the selection may differ from an unbounded search. So is a code
    outside the registry and an unreadable entry (empty code).
    """
    return "info" if code in _INFORMATIVE else "limit"


def project_retrieval_exclusion_warnings(
    exclusions: Iterable[RetrievalExclusion],
) -> tuple[KnowledgeRetrievalWarning, ...]:
    """Aggregate exclusions losslessly and project stable warning codes.

    Repeated retrievals in a decomposed or gate-rewrite run can report the same
    exclusion kind. Counts are therefore accumulated by the full typed identity
    (reason, stage, recommended action), while first-seen order remains stable.
    The resulting count is explicitly a number of exclusion *observations*, not
    unique chunks: chunk ids deliberately do not cross this aggregate boundary,
    so the same unsafe point observed by two queries cannot be deduplicated
    safely. Unknown future reasons are deliberately surfaced through a generic
    warning instead of being silently discarded.
    """

    order: list[tuple[str, str, str | None]] = []
    counts: dict[tuple[str, str, str | None], int] = {}
    for exclusion in exclusions:
        key = (
            exclusion.reason,
            exclusion.stage,
            exclusion.recommended_action,
        )
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += exclusion.count

    warnings: list[KnowledgeRetrievalWarning] = []
    for reason, stage, recommended_action in order:
        code = _WARNING_CONTRACT.get(reason, _UNKNOWN_EXCLUSION_CODE)
        warnings.append(
            KnowledgeRetrievalWarning(
                code=code,
                message=warning_message(code),
                reason=reason,
                stage=stage,
                count=counts[(reason, stage, recommended_action)],
                recommended_action=recommended_action,
            )
        )
    return tuple(warnings)


@dataclass(frozen=True)
class SearchNotice:
    """What one code amounted to over a task's searches; text-free.

    ``count`` sums the entries' counts — observations, not unique hits: the
    same hit found by two searches counts twice. ``searches`` counts the
    searches with at least one entry of the code; ``incomplete`` those with a
    degradation that missed its final evidence count, and ``returned`` /
    ``requested`` is the tightest of them (fewest hits returned). The empty
    code stands for entries that could not be read.
    """

    code: str
    count: int = 0
    searches: int = 0
    incomplete: int = 0
    returned: int = 0
    requested: int = 0

    @property
    def severity(self) -> Literal["limit", "info"]:
        return notice_severity(self.code)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "SearchNotice":
        """A stored notice; anything unreadable is the unreadable notice."""
        if not isinstance(value, Mapping) or not isinstance(value.get("code"), str):
            return cls(code="")
        numbers = {
            name: value.get(name, 0)
            for name in ("count", "searches", "incomplete", "returned", "requested")
        }
        if any(_non_negative(number) is None for number in numbers.values()):
            return cls(code="")
        return cls(code=value["code"], **numbers)


def _non_negative(value: Any) -> int | None:
    """*value* as a count, ``None`` when it is not one (bools included)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _entry(value: Any) -> tuple[str, int, bool | None, tuple[int, int] | None]:
    """``(code, count, final_evidence_complete, (returned, requested))`` of
    one raw warning; code ``""`` when the entry cannot be read.

    The family comes from the registry, never from the fields present: the
    capability model gives every warning a ``final_evidence_complete`` of
    ``False``, and only a degradation's value means something.
    """
    if not isinstance(value, Mapping):
        return "", 0, None, None
    code = value.get("code")
    if not isinstance(code, str) or not code.strip():
        return "", 0, None, None
    family = notice_family(code)
    count = _non_negative(value.get("count", 0))
    if family == "exclusion":
        if count is None or count < 1:
            return "", 0, None, None
        return code, count, None, None
    if family == "degradation":
        complete = value.get("final_evidence_complete")
        returned = _non_negative(value.get("returned_hits"))
        requested = _non_negative(value.get("final_top_k"))
        if not isinstance(complete, bool) or returned is None or requested is None:
            return "", 0, None, None
        return code, count or 0, complete, (returned, requested)
    return code, count or 0, None, None


def collect_search_notices(per_search: Iterable[Iterable[Any]]) -> list[SearchNotice]:
    """Aggregate the raw warnings of several searches by code, first seen first.

    *per_search* holds one list of warning mappings per search (a
    degradation carries its ``reason`` as ``code``). An unreadable entry and
    a code without reader words stay as notices, so the reader sees a
    general sentence; each is logged once here, never by the renderer.
    """
    order: list[str] = []
    totals: dict[str, dict[str, Any]] = {}
    for entries in per_search:
        seen: set[str] = set()
        short: set[str] = set()
        for value in entries:
            code, count, complete, pair = _entry(value)
            total = totals.get(code)
            if total is None:
                order.append(code)
                total = totals[code] = {
                    "entries": 0, "count": 0, "searches": 0, "incomplete": 0,
                    "tightest": None, "raw_codes": set(),
                }
            total["entries"] += 1
            if not code and isinstance(value, Mapping):
                # The log names what the entry claimed to be; the notice
                # itself stays text-free.
                raw = value.get("code")
                if isinstance(raw, str) and raw.strip():
                    total["raw_codes"].add(raw.strip()[:60])
            total["count"] += count
            if code not in seen:
                seen.add(code)
                total["searches"] += 1
            if complete is False and pair is not None:
                if code not in short:
                    short.add(code)
                    total["incomplete"] += 1
                tightest = total["tightest"]
                if tightest is None or (pair[0], -pair[1]) < (tightest[0], -tightest[1]):
                    total["tightest"] = pair
    notices: list[SearchNotice] = []
    for code in order:
        total = totals[code]
        returned, requested = total["tightest"] or (0, 0)
        notice = SearchNotice(
            code=code,
            count=total["count"],
            searches=total["searches"],
            incomplete=total["incomplete"],
            returned=returned,
            requested=requested,
        )
        if not code:
            log.warning(
                "Knowledge search notices unreadable (entries=%d, searches=%d, "
                "codes=%s); the reader sees a general sentence.",
                total["entries"],
                notice.searches,
                sorted(total["raw_codes"]) or "none",
            )
        elif not _has_reader_words(code):
            log.warning(
                "Knowledge search notice %r has no reader words (entries=%d, "
                "searches=%d); the reader sees a general sentence.",
                code,
                total["entries"],
                notice.searches,
            )
        notices.append(notice)
    return notices


def _has_reader_words(code: str) -> bool:
    notice = _NOTICES.get(code)
    return notice is not None and bool(notice.words)


def stored_search_notices(values: Any) -> list[SearchNotice]:
    """The notices a task or a reading record stored (``as_dict`` values).

    Entries that cannot be read — a record written before the notices were
    typed holds plain English strings — fold into ONE unreadable notice and
    are logged once here: the reader gets one general sentence, not the same
    sentence per entry.
    """
    stored = [SearchNotice.from_dict(value) for value in list(values or [])]
    readable = [notice for notice in stored if notice.code]
    unreadable = len(stored) - len(readable)
    if not unreadable:
        return stored
    log.warning(
        "Stored search notices unreadable (entries=%d, readable=%d); the "
        "reader sees one general sentence.",
        unreadable,
        len(readable),
    )
    return [*readable, SearchNotice(code="")]


def search_notice_lines(
    notices: Iterable[SearchNotice],
    language: str,
    *,
    severity: Literal["limit", "info"] | None = None,
) -> list[str]:
    """One reader sentence per notice, in *language*; pure.

    Counts are hits or exclusions over searches, never documents. A code
    without reader words and an unreadable entry read as a general sentence:
    the code itself stays in the log and the event, never in reader text.
    *severity* keeps only the limits or only the informative notices.
    """
    words = {"language": language}
    lines: list[str] = []
    for notice in notices:
        if severity is not None and notice.severity != severity:
            continue
        lines.append(_notice_words(words, notice))
    return lines


def _notice_words(words: dict[str, str], notice: SearchNotice) -> str:
    if not notice.code:
        return t(words, "search_notice_unreadable")
    if not _has_reader_words(notice.code):
        return t(words, "search_notice_unknown")
    contract = _NOTICES[notice.code]
    searches = t_count(words, "search_notice_searches", notice.searches)
    if contract.family == "degradation":
        if notice.incomplete:
            return t_count(
                words, contract.words, notice.requested,
                returned=notice.returned, searches=searches,
            )
        return t(words, f"{contract.words}_complete", searches=searches)
    if contract.family == "exclusion":
        return t_count(words, contract.words, notice.count, searches=searches)
    return t(words, contract.words)
