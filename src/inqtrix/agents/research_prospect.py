"""Whether another research round of a mission can reach unread material.

The review route and the replan gate ask the same question before a planner
is called: can research in this run still reach something it has not read?
Without that question a run whose review leaves an information criterion open
plans another round over the same documents, the same collections and a web
the assignment does not need, and ends where it began.

This module answers it over gathered facts and nothing else: no I/O, no run
dependencies, no state writes. The mission gathers the facts
(``algorithm._prospect_facts``) and owns every read and every warning of the
gathering; here only the rules live, so each of them is testable on a literal.

The answer names every branch that stays open (``coverage``, ``not_selected``,
``collection``, ``web``) or, when none would hold, every code that closed them,
in the fixed order of :data:`CODE_RANK`.

The document selection of a later round's file analysis reads the same rows
and the same window rule (:func:`version_reach`, :func:`select_documents`), so
"a re-read can reach it" means the same thing to the prospect and to the
selection.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from inqtrix.agents.document_analysis import reading_blockers
from inqtrix.agents.intake import assignment_needs_web
from inqtrix.agents.phase_models import AssignmentProfile

log = logging.getLogger("inqtrix")

REACHABLE = "reachable"
"""A re-read of the same document version can close the row (window rule)."""
TRANSIENT_ONLY = "transient_only"
"""Reachable only when the failure code is transient
(:data:`TRANSIENT_DOCUMENT_FAILURES`); every other code returns."""
RETURNS = "returns"
"""A property of the stored document version: every re-read returns it."""
STOPPED = "stopped"
"""The user stopped the reading; research does not overrule that."""
NOT_SELECTED = "not_selected"
"""Found but not read: the ``not_selected`` branch reads it, never a row."""

COVERAGE_REACHABILITY: dict[str, str] = {
    "provider_error": REACHABLE,
    "not_called_after_failure": REACHABLE,
    "extraction_failed": REACHABLE,
    "not_read": REACHABLE,
    "window_limit": REACHABLE,
    "document_failed": TRANSIENT_ONLY,
    "content_refused": RETURNS,
    "no_text": RETURNS,
    "pages_without_text": RETURNS,
    "extraction_not_recorded": RETURNS,
    "cancelled": STOPPED,
    "documents_not_selected": NOT_SELECTED,
}
"""Every coverage reason of ``document_analysis.REASONS``, by what a re-read
can do about it. Closed: a coverage reason missing here is unreachable and
logged, so a new reason never opens research by default."""

TRANSIENT_DOCUMENT_FAILURES = frozenset({"document_changed"})
"""Document failure codes a later reading can get past: the version changed
under the reading, and the next reading starts on the new one."""

RETURNS_ON_REREADING = frozenset(
    reason for reason, kind in COVERAGE_REACHABILITY.items() if kind == RETURNS
)
"""The coverage reasons every re-reading of a document version returns."""

BRANCH_ORDER = ("coverage", "not_selected", "collection", "web")
"""The branches in the order a prospect names them."""

CODE_RANK = (
    "read_budget_exhausted",
    "source_bound_limit",
    "collection_unavailable",
    "web_not_permitted",
    "web_not_needed",
    "extraction_limit",
)
"""Every code a prospect can name, in the order it names them."""

PROSPECT_INPUT_KEYS = (
    "windows_left",
    "reachable_coverage_rows",
    "unreachable_coverage_rows",
    "unread_not_selected",
    "unread_collections",
    "collection_unavailable",
    "web_allowed",
    "web_task_kinds_admitted",
    "web_capacity",
    "needs_web",
    "user_demanded_web",
    "sources_answered",
    "web_task_planned",
    "replan_headroom",
    "failed_inputs",
)
"""What a prospect decision shows as its inputs: counts and flags only, never
a title or an id. ``failed_inputs`` names the inputs whose read failed: each
stands at the value that closes its branch, which a true negative shows the
same way, so the list is what tells the two apart."""


@dataclass(frozen=True)
class ProspectFacts:
    """What the mission gathered for one prospect decision.

    ``readings`` maps a task id to its ``document_analysis`` record (a dict
    without order: nothing here may depend on it). ``reading_order`` maps a
    reading's task id to its position in the plan (``ordinal``), the only
    order the rules read. ``max_windows`` is the per-analysis window cap,
    ``windows_left`` what the run's window budget still grants.
    ``web_task_kinds_admitted`` says whether the skill and source allowlists
    admit a web task kind at all, ``web_capacity`` whether the tier still
    has room for one in this plan. ``replan_headroom`` is the number of task
    slots a replan may still fill. ``failed_inputs`` names the inputs of
    :data:`PROSPECT_INPUT_KEYS` whose read failed.
    """

    readings: Mapping[str, Mapping[str, Any]]
    reading_order: Mapping[str, int]
    max_windows: int
    windows_left: int
    unread_collections: int
    collection_unavailable: bool
    web_allowed: bool
    web_task_kinds_admitted: bool
    web_capacity: bool
    profile: AssignmentProfile | None
    user_demanded_web: bool
    sources_answered: bool
    web_task_planned: bool
    replan_headroom: int
    failed_inputs: tuple[str, ...]


@dataclass(frozen=True)
class Prospect:
    """The answer: which branches stay open, or which codes closed them."""

    reachable: bool
    branches: tuple[str, ...]
    codes: tuple[str, ...]
    inputs: dict[str, Any]


@dataclass(frozen=True)
class CoverageRow:
    """One coverage limit a reading names for one document version."""

    task_id: str
    """The reading that names it."""
    reason: str
    document_id: str
    version: str
    failure: str
    needed: int
    """Windows a re-read needs to reach the row: it starts at window 1, so
    at least the version's planned windows (and never fewer than one)."""


class ReadingView(NamedTuple):
    """The readings a decision reads, and their plan positions.

    ``readings`` maps a task id to its ``document_analysis`` record,
    ``order`` a reading's task id to its position in the plan (see
    :class:`ProspectFacts`).
    """

    readings: Mapping[str, Mapping[str, Any]]
    order: Mapping[str, int]


REREAD_STATES = ("unread", "reachable", "read_to_end", "budget", "beyond_reread")
"""What a re-read of one document version could do, as :func:`version_reach`
names it."""


class VersionReach(NamedTuple):
    """One document version as a later reading would meet it."""

    state: str
    """A :data:`REREAD_STATES` value."""
    needed: int
    """Windows a re-read of the version needs (0 for an unread version)."""
    failure: str = ""
    """For version ``''``, which no reading opened: the code the latest of
    its readings failed with before the first window. ``''`` otherwise."""


@dataclass(frozen=True)
class Candidate:
    """One document a file analysis found, in rank order."""

    document_id: str
    title: str
    reach: str
    """A :data:`REREAD_STATES` value, or ``unchecked``: the current version
    could not be read, so the document counts as unread."""
    version: str
    needed: int
    failure: str = ""
    """See :attr:`VersionReach.failure`: set for a document no earlier
    reading could open."""


@dataclass(frozen=True)
class DocumentSelection:
    """What one file analysis reads, leaves out and does not read again."""

    selected: tuple[tuple[str, str], ...]
    """``(document_id, title)`` in reading order."""
    not_selected: tuple[tuple[str, str, str], ...]
    """``(document_id, title, reason)``: ``document_limit`` or ``window_limit``."""
    already_read: tuple[dict[str, str], ...]
    """``{document_id, title, version, state, failure}`` of the documents an
    earlier round read as far as a re-read reaches. ``failure`` is ``''``
    for a document an earlier round read; for one no earlier reading could
    open (version ``''``) it is the code that reading failed with, and the
    document was never read."""
    unchecked: int
    """Selected documents whose current version could not be read."""


def readings_of(
    outcomes: Mapping[str, Any] | None,
    task_ids: Collection[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Every ``document_analysis`` record of *outcomes* by task id.

    Only the readings of *task_ids* when given (an empty collection gives
    none).
    """
    return {
        str(task_id): dict(outcome["document_analysis"])
        for task_id, outcome in (outcomes or {}).items()
        if isinstance(outcome, Mapping)
        and outcome.get("document_analysis")
        and (task_ids is None or task_id in task_ids)
    }


def document_windows_used(outcomes: Mapping[str, Any] | None) -> int:
    """Windows the run's recorded analyses claimed from its window budget."""
    return sum(
        int((outcome.get("document_analysis") or {}).get("windows_granted", 0) or 0)
        for outcome in (outcomes or {}).values()
        if isinstance(outcome, dict)
    )


def _documents(
    readings: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Every analysed document with the task id of the reading that holds it."""
    return [
        (str(task_id), dict(document))
        for task_id, record in readings.items()
        for document in (record or {}).get("documents") or []
        if isinstance(document, Mapping)
    ]


def _read_to_the_end(document: Mapping[str, Any]) -> bool:
    return not document.get("failure") and bool(document.get("text_read_to_end"))


def _windows_needed(document: Mapping[str, Any]) -> int:
    """Windows a re-read of *document*'s version needs (see :attr:`CoverageRow.needed`)."""
    return max(1, int(document.get("windows_planned") or 0))


def _document_rows(task_id: str, document: Mapping[str, Any]) -> list[CoverageRow]:
    return [
        CoverageRow(
            task_id=task_id,
            reason=str(blocker["reason"]),
            document_id=str(blocker["document_id"]),
            version=str(document.get("version") or ""),
            failure=str(document.get("failure") or ""),
            needed=_windows_needed(document),
        )
        for blocker in reading_blockers({"documents": [dict(document)]})
        if blocker["blocks"] == "coverage" and blocker["document_id"]
    ]


def _superseded(
    row: CoverageRow,
    documents: list[tuple[str, dict[str, Any]]],
    order: Mapping[str, int],
) -> bool:
    """Whether some reading made *row* stale, whatever order it is stored in.

    A reading of the same version that read to the end without the row's
    reason reached what the row names, whenever it ran. A reading of another
    version that read to the end voids the row only when its task stands
    after the row's in the plan (*order*): the document changed and a later
    reading completed the new version. An earlier complete reading says
    nothing about a version that came after it, and of two versions that
    both hold a limit the later one's stays. A reading without a plan
    position voids no other version's row. A document failure goes the same
    way, since a reading to the end holds no failure.

    The plan position is the order a replan appends tasks in; a user edit
    that reorders tasks, or two readings of one wave that saw the document
    change between them, can put the older version later, and then that
    pair is judged as if the later position ran later.
    """
    row_position = order.get(row.task_id)
    for task_id, document in documents:
        if str(document.get("document_id") or "") != row.document_id:
            continue
        if not _read_to_the_end(document):
            continue
        if str(document.get("version") or "") == row.version:
            if not any(
                item.reason == row.reason for item in _document_rows(task_id, document)
            ):
                return True
            continue
        position = order.get(task_id)
        if row_position is not None and position is not None and position > row_position:
            return True
    return False


def counting_coverage_rows(
    readings: Mapping[str, Mapping[str, Any]],
    order: Mapping[str, int],
) -> list[CoverageRow]:
    """Every coverage row of every reading that no other reading made stale."""
    documents = _documents(readings)
    return [
        row
        for task_id, document in documents
        for row in _document_rows(task_id, document)
        if not _superseded(row, documents, order)
    ]


def _row_reach(row: CoverageRow, *, max_windows: int, windows_left: int) -> str:
    """``reachable``, ``budget`` (blocked by the run's windows) or ``unreachable``."""
    kind = COVERAGE_REACHABILITY.get(row.reason)
    if kind is None:
        log.warning(
            "Coverage reason %s has no reachability class; it counts as unreachable.",
            row.reason,
        )
        return "unreachable"
    if kind == TRANSIENT_ONLY and row.failure not in TRANSIENT_DOCUMENT_FAILURES:
        return "unreachable"
    if kind not in (REACHABLE, TRANSIENT_ONLY):
        return "unreachable"
    if row.needed > max_windows:
        # A re-read starts at window 1 and stops at the per-analysis cap
        # again: past the cap, no reading of this run reaches the row.
        return "unreachable"
    if row.needed > windows_left:
        return "budget"
    return "reachable"


def held_versions(view: ReadingView, document_id: str) -> frozenset[str]:
    """Every version of *document_id* a reading of *view* holds.

    ``''`` is the version of a reading that failed before its first window.
    """
    return frozenset(
        str(document.get("version") or "")
        for _task_id, document in _documents(view.readings)
        if str(document.get("document_id") or "") == document_id
    )


def version_reach(
    view: ReadingView,
    document_id: str,
    version: str,
    *,
    max_windows: int,
    windows_left: int,
) -> VersionReach:
    """What a re-read of *document_id* at *version* could still reach.

    ``unread``: no reading of *view* holds that version. ``reachable``: a
    counting coverage row of the version is reachable under the window rule
    (:func:`_row_reach`). ``read_to_end``: a reading read the version to the
    end, and what its rows name returns on every re-reading. ``budget``: a
    re-read would exceed the run's remaining windows. ``beyond_reread``: a
    re-read cannot reach what stayed unread (past the per-analysis cap, a
    stored limit, a stop by the user, or no counting row at all).

    Selectable iff ``unread`` or ``reachable`` - exactly the coverage branch
    of :func:`research_can_reach` for that version. A document whose every
    reading failed before its first window holds version ``''``; asked for
    ``''`` it is judged by that failure row like any other. A window-0
    failure is recorded with the store's own code and is never transient
    (``document_changed`` needs a version pinned at window 0), so such a
    document is ``beyond_reread``, and ``failure`` names the code: it was
    never read. A version read to the end whose windows named the most
    passages they may contribute stays ``read_to_end``: until readings
    continue past a saturated window, a re-read would read the same windows
    again.
    """
    holding = [
        (task_id, document)
        for task_id, document in _documents(view.readings)
        if str(document.get("document_id") or "") == document_id
        and str(document.get("version") or "") == version
    ]
    if not holding:
        return VersionReach("unread", 0)
    needed = max(_windows_needed(document) for _task_id, document in holding)
    # No reading opened version '': the latest one's code says why.
    failure = (
        ""
        if version
        else max(
            (view.order.get(task_id, -1), str(document.get("failure") or ""))
            for task_id, document in holding
        )[1]
    )
    reach = [
        _row_reach(row, max_windows=max_windows, windows_left=windows_left)
        for row in counting_coverage_rows(view.readings, view.order)
        if row.document_id == document_id and row.version == version
    ]
    if "reachable" in reach:
        return VersionReach("reachable", needed, failure)
    if any(_read_to_the_end(document) for _task_id, document in holding):
        return VersionReach("read_to_end", needed, failure)
    if "budget" in reach:
        return VersionReach("budget", needed, failure)
    return VersionReach("beyond_reread", needed, failure)


def select_documents(
    candidates: Sequence[Candidate],
    *,
    limit: int,
    max_windows: int,
    windows_left: int,
) -> DocumentSelection:
    """The documents one file analysis reads, from *candidates* in rank order.

    A version an earlier round read as far as a re-read reaches
    (``read_to_end``, ``budget``, ``beyond_reread``) is not read again, nor
    is one no earlier reading could open (its entry names the failure). A
    reachable re-read is read first: the analysis spends one window budget
    across its documents in order, so a re-read behind a new document would
    be cut at the window limit again and read its first windows a second
    time for nothing. Re-reads are admitted while their windows together fit
    the analysis's budget (``min(max_windows, windows_left)``); one that no
    longer fits is not selected for ``window_limit`` and stays reachable for
    a later round. At most *limit* documents are read, re-reads included;
    the rest are not selected for ``document_limit``. ``unchecked`` counts
    the selected documents whose current version could not be read.
    """
    rereads: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    not_selected: list[tuple[str, str, str]] = []
    already_read: list[dict[str, str]] = []
    unchecked = 0
    used = 0
    fits = min(max_windows, windows_left)
    for candidate in candidates:
        if candidate.reach in {"read_to_end", "budget", "beyond_reread"}:
            already_read.append(
                {
                    "document_id": candidate.document_id,
                    "title": candidate.title,
                    "version": candidate.version,
                    "state": candidate.reach,
                    "failure": candidate.failure,
                }
            )
            continue
        if candidate.reach == "reachable" and used + candidate.needed > fits:
            not_selected.append((candidate.document_id, candidate.title, "window_limit"))
            continue
        if len(rereads) + len(others) >= limit:
            not_selected.append((candidate.document_id, candidate.title, "document_limit"))
            continue
        if candidate.reach == "reachable":
            used += candidate.needed
            rereads.append((candidate.document_id, candidate.title))
            continue
        if candidate.reach == "unchecked":
            unchecked += 1
        others.append((candidate.document_id, candidate.title))
    return DocumentSelection(
        selected=(*rereads, *others),
        not_selected=tuple(not_selected),
        already_read=tuple(already_read),
        unchecked=unchecked,
    )


def unattempted_not_selected(readings: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Documents a search found and no reading of the run ever attempted."""
    attempted = {
        str(document.get("document_id") or "") for _task_id, document in _documents(readings)
    }
    return sorted(
        {
            str(entry.get("document_id") or "")
            for record in readings.values()
            for entry in (record or {}).get("not_selected") or []
            if isinstance(entry, Mapping)
        }
        - attempted
        - {""}
    )


def extraction_limited(readings: Mapping[str, Mapping[str, Any]]) -> bool:
    """A document read to the end showed more passages than were taken over."""
    return any(
        _read_to_the_end(document)
        and (
            int(document.get("windows_possibly_more") or 0) > 0
            or int(document.get("findings_over_limit") or 0) > 0
        )
        for _task_id, document in _documents(readings)
    )


def research_can_reach(facts: ProspectFacts) -> Prospect:
    """Whether another research round can reach unread material, and why not.

    Branches: ``coverage`` (a counting row a re-read reaches within the
    window rule), ``not_selected`` (a found document no reading attempted,
    while windows are left), ``collection`` (an admitted collection no
    knowledge task searched; it needs no window budget) and ``web``
    (permitted, and needed as :func:`~inqtrix.agents.intake.
    assignment_needs_web` reads the assignment). A replan without a free
    task slot closes every branch, and is no code of its own: the codes say
    what closed the branches themselves, so they are named only when no
    branch would hold even with a free slot. ``extraction_limit`` stands on
    every answer whose readings saw more passages than they took over.
    """
    rows = counting_coverage_rows(facts.readings, facts.reading_order)
    reach = [
        _row_reach(row, max_windows=facts.max_windows, windows_left=facts.windows_left)
        for row in rows
    ]
    unread = unattempted_not_selected(facts.readings)
    needs_web = assignment_needs_web(
        facts.profile,
        user_chose_sources=facts.user_demanded_web
        or (facts.sources_answered and facts.web_task_planned),
    )
    web_permitted = (
        facts.web_allowed and facts.web_task_kinds_admitted and facts.web_capacity
    )
    open_branches = {
        "coverage": "reachable" in reach,
        "not_selected": bool(unread) and facts.windows_left > 0,
        "collection": facts.unread_collections > 0,
        "web": web_permitted and needs_web,
    }
    branches = (
        tuple(name for name in BRANCH_ORDER if open_branches[name])
        if facts.replan_headroom > 0
        else ()
    )
    applicable: set[str] = set()
    if not any(open_branches.values()):
        if "budget" in reach or (unread and facts.windows_left <= 0):
            applicable.add("read_budget_exhausted")
        if "unreachable" in reach:
            applicable.add("source_bound_limit")
        if facts.collection_unavailable:
            applicable.add("collection_unavailable")
        if not web_permitted:
            applicable.add("web_not_permitted")
        elif not needs_web:
            applicable.add("web_not_needed")
    if extraction_limited(facts.readings):
        applicable.add("extraction_limit")
    inputs = {
        "windows_left": facts.windows_left,
        "reachable_coverage_rows": reach.count("reachable"),
        "unreachable_coverage_rows": len(reach) - reach.count("reachable"),
        "unread_not_selected": len(unread),
        "unread_collections": facts.unread_collections,
        "collection_unavailable": facts.collection_unavailable,
        "web_allowed": facts.web_allowed,
        "web_task_kinds_admitted": facts.web_task_kinds_admitted,
        "web_capacity": facts.web_capacity,
        "needs_web": needs_web,
        "user_demanded_web": facts.user_demanded_web,
        "sources_answered": facts.sources_answered,
        "web_task_planned": facts.web_task_planned,
        "replan_headroom": facts.replan_headroom,
        "failed_inputs": [key for key in PROSPECT_INPUT_KEYS if key in facts.failed_inputs],
    }
    return Prospect(
        reachable=bool(branches),
        branches=branches,
        codes=tuple(code for code in CODE_RANK if code in applicable),
        inputs=inputs,
    )
