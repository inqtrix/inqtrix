"""Whether another research round of a mission can reach unread material.

The rules are pure over gathered facts (``agents.research_prospect``); the
mission gathers the facts and decides with them where a replan is decided,
on the review route and at the replan gate. These tests pin the rules on
literals, the gathering on fake run dependencies, and the decision on the
real graph with a scripted model.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents import algorithm
from inqtrix.agents.document_analysis import REASONS
from inqtrix.agents.intake import WEB_NOT_NEEDED_NOTE_KEY
from inqtrix.agents.phase_models import AssignmentProfile
from inqtrix.agents.research_prospect import (
    CODE_RANK,
    COVERAGE_REACHABILITY,
    PROSPECT_INPUT_KEYS,
    Candidate,
    Prospect,
    ProspectFacts,
    ReadingView,
    held_versions,
    research_can_reach,
    select_documents,
    version_reach,
)
from inqtrix.agents.scheduler import TaskOutcome
from inqtrix.core.results import SourcePolicy
from inqtrix.i18n import t
from tests.agents.test_workspace_agent import (
    PROFILE,
    ScriptedLLM,
    _collection_client,
    _stored_events,
    make_agent_client,
    replan_delta,
    review_verdict,
    submit_agent,
    wait_status,
)

NOT_NEEDED = AssignmentProfile.model_validate({**PROFILE, "needs_web": False})
NEEDED = AssignmentProfile.model_validate({**PROFILE, "needs_web": True})


def _document(
    document_id: str = "kd_a",
    *,
    version: str = "v1",
    planned: int = 2,
    unanalysed: tuple[str, ...] = (),
    failure: str = "",
    total: int = 9_000,
    pages_without_text: tuple[int, ...] = (),
    extraction_status: str = "recorded",
    possibly_more: int = 0,
    over_limit: int = 0,
    read_to_end: bool | None = None,
) -> dict[str, Any]:
    """One analysed document as ``AnalysedDocument.model_dump`` stores it."""
    return {
        "document_id": document_id,
        "title": document_id,
        "version": version,
        "total_chars": total,
        "windows_planned": planned,
        "windows_analysed": planned - len(unanalysed),
        "unanalysed": [
            {"start": index, "end": index + 1, "reason": reason}
            for index, reason in enumerate(unanalysed)
        ],
        "text_read_to_end": (
            not unanalysed and not failure if read_to_end is None else read_to_end
        ),
        "failure": failure,
        "extraction_status": extraction_status,
        "pages_without_text": list(pages_without_text),
        "findings_over_limit": over_limit,
        "windows_possibly_more": possibly_more,
    }


def _record(*documents: dict[str, Any], not_selected: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "documents": list(documents),
        "not_selected": [
            {"document_id": item, "title": item, "reason": "document_limit"}
            for item in not_selected
        ],
        "windows_granted": sum(int(document["windows_planned"]) for document in documents),
    }


def _facts(readings: dict[str, dict[str, Any]] | None = None, **changes: Any) -> ProspectFacts:
    values: dict[str, Any] = {
        "readings": readings or {},
        "reading_order": {},
        "max_windows": 40,
        "windows_left": 66,
        "unread_collections": 0,
        "collection_unavailable": False,
        "web_allowed": True,
        "web_task_kinds_admitted": True,
        "web_capacity": True,
        "profile": NOT_NEEDED,
        "user_demanded_web": False,
        "sources_answered": False,
        "web_task_planned": False,
        "replan_headroom": 5,
        "failed_inputs": (),
    }
    values.update(changes)
    return ProspectFacts(**values)


SATURATED_READINGS = {
    "t1": _record(
        _document("kd_short", version="v_short", planned=1),
        _document("kd_long", version="v_long", planned=6, possibly_more=1),
    ),
    "t2": _record(
        _document("kd_short", version="v_short", planned=1),
        _document("kd_long", version="v_long", planned=6, possibly_more=5),
    ),
}
"""Two analyses that read both documents of the collection to the end, and
saw more passages in the long one than they could take over."""


# -- extraction_limit -------------------------------------------------------- #


@pytest.mark.parametrize(
    ("readings", "changes", "expected"),
    [
        pytest.param(
            SATURATED_READINGS, {}, (False, ("web_not_needed", "extraction_limit")),
            id="saturated-windows",
        ),
        pytest.param(
            {"t1": _record(_document("kd_long", planned=6))}, {},
            (False, ("web_not_needed",)), id="nothing-saturated",
        ),
        pytest.param(
            {"t1": _record(_document("kd_long", planned=6, over_limit=2))}, {},
            (False, ("web_not_needed", "extraction_limit")), id="passages-over-limit-only",
        ),
        pytest.param(
            {"t1": _record(_document("kd_long", planned=6, possibly_more=3, unanalysed=("cancelled",)))},
            {}, (False, ("source_bound_limit", "web_not_needed")), id="not-read-to-the-end",
        ),
        pytest.param(
            {"t1": _record(_document(
                "kd_long", planned=6, possibly_more=3, failure="knowledge.document_not_found",
                read_to_end=False,
            ))},
            {}, (False, ("source_bound_limit", "web_not_needed")), id="failed-document",
        ),
        pytest.param(
            SATURATED_READINGS, {"profile": NEEDED}, (True, ("extraction_limit",)),
            id="reachable-prospect-keeps-the-code",
        ),
    ],
)
def test_extraction_limit_is_named_until_windows_continue(readings, changes, expected):
    """Saturated windows in documents read to the end are an extraction
    limit on every answer, open or closed; a reading that did not reach the
    end or failed names none. Turns red when windows continue past a
    saturated one."""
    prospect = research_can_reach(_facts(readings, **changes))
    assert (prospect.reachable, prospect.codes) == expected


def test_a_saturated_version_is_not_read_again_until_windows_continue():
    """A version read to the end whose windows named the most passages they
    may contribute: a re-read would read the same windows again, so a later
    round's analysis does not select it — temporary until reading continues
    past a saturated window. Turns red when saturated windows count as
    reachable."""
    view = ReadingView({"t1": _record(_document("kd_a", planned=3, possibly_more=3))}, {"t1": 0})
    assert version_reach(view, "kd_a", "v1", max_windows=40, windows_left=66).state == "read_to_end"


# -- every branch ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("readings", "changes", "branch"),
    [
        pytest.param(
            {"t1": _record(_document("kd_a", planned=4, unanalysed=("provider_error",)))},
            {"windows_left": 10}, "coverage", id="coverage",
        ),
        pytest.param(
            {"t1": _record(_document("kd_a"), not_selected=("kd_b",))}, {}, "not_selected",
            id="not-selected",
        ),
        pytest.param(None, {"unread_collections": 1}, "collection", id="collection"),
        pytest.param(
            None, {"unread_collections": 1, "windows_left": 0}, "collection",
            id="collection-without-window-budget",
        ),
        pytest.param(None, {"profile": NEEDED}, "web", id="web"),
        pytest.param(None, {"profile": None}, "web", id="web-without-intake-reading"),
    ],
)
def test_each_reachable_branch_opens_the_planner(readings, changes, branch):
    prospect = research_can_reach(_facts(readings, **changes))
    assert (prospect.reachable, prospect.branches) == (True, (branch,))


@pytest.mark.parametrize(
    ("readings", "changes", "codes"),
    [
        pytest.param(
            {"t1": _record(_document("kd_a", planned=4, unanalysed=("provider_error",)))},
            {"windows_left": 0}, ("read_budget_exhausted", "web_not_needed"), id="budget",
        ),
        pytest.param(
            {"t1": _record(_document("kd_a"), not_selected=("kd_b",))},
            {"windows_left": 0}, ("read_budget_exhausted", "web_not_needed"),
            id="budget-for-a-found-document",
        ),
        pytest.param(
            {"t1": _record(_document("kd_a", pages_without_text=(2,)))}, {},
            ("source_bound_limit", "web_not_needed"), id="pages-without-text",
        ),
        pytest.param(
            {"t1": _record(_document("kd_a", total=0, planned=0))}, {},
            ("source_bound_limit", "web_not_needed"), id="no-text",
        ),
        pytest.param(
            {"t1": _record(_document(
                "kd_a", failure="knowledge.document_not_found", read_to_end=False,
            ))},
            {}, ("source_bound_limit", "web_not_needed"), id="document-failed",
        ),
        pytest.param(
            None, {"collection_unavailable": True},
            ("collection_unavailable", "web_not_needed"), id="collection-unavailable",
        ),
        pytest.param(None, {"web_allowed": False}, ("web_not_permitted",), id="web-not-permitted"),
        pytest.param(None, {}, ("web_not_needed",), id="web-not-needed"),
        pytest.param(
            {
                "t1": _record(_document(
                    "kd_a", planned=4, unanalysed=("provider_error",), possibly_more=0,
                )),
                "t2": _record(
                    _document("kd_b", pages_without_text=(1,)),
                    _document("kd_c", planned=3, possibly_more=2),
                ),
            },
            {"windows_left": 0, "collection_unavailable": True, "web_capacity": False},
            CODE_RANK[:4] + ("extraction_limit",), id="every-code-in-rank",
        ),
    ],
)
def test_every_unreachable_prospect_names_its_codes(readings, changes, codes):
    prospect = research_can_reach(_facts(readings, **changes))
    assert (prospect.reachable, prospect.codes) == (False, codes)


def test_the_reachability_table_covers_every_coverage_reason():
    assert set(COVERAGE_REACHABILITY) == {
        reason for reason, row in REASONS.items() if row.blocks == "coverage"
    }


def _single_row(reason: str, **document: Any) -> dict[str, dict[str, Any]]:
    shapes: dict[str, dict[str, Any]] = {
        "content_refused": {"unanalysed": ("content_refused",)},
        "no_text": {"total": 0, "planned": 0},
        "pages_without_text": {"pages_without_text": (3,)},
        "extraction_not_recorded": {"extraction_status": "not_recorded"},
        "document_failed": {"failure": "document_changed", "read_to_end": False},
    }
    shape = {"planned": 3, **shapes.get(reason, {"unanalysed": (reason,)}), **document}
    return {"t1": _record(_document("kd_a", **shape))}


@pytest.mark.parametrize(
    ("reason", "kind", "document", "reached"),
    [
        pytest.param("provider_error", "reachable", {}, True, id="provider_error"),
        pytest.param("not_called_after_failure", "reachable", {}, True, id="not_called_after_failure"),
        pytest.param("extraction_failed", "reachable", {}, True, id="extraction_failed"),
        pytest.param("not_read", "reachable", {}, True, id="not_read"),
        pytest.param("window_limit", "reachable", {}, True, id="window_limit-within-the-cap"),
        pytest.param("window_limit", "reachable", {"planned": 41}, False, id="window_limit-past-the-cap"),
        pytest.param("document_failed", "transient_only", {}, True, id="document_failed-version-changed"),
        pytest.param(
            "document_failed", "transient_only", {"failure": "knowledge.document_not_found"}, False,
            id="document_failed-not-found",
        ),
        pytest.param("content_refused", "returns", {}, False, id="content_refused"),
        pytest.param("no_text", "returns", {}, False, id="no_text"),
        pytest.param("pages_without_text", "returns", {}, False, id="pages_without_text"),
        pytest.param("extraction_not_recorded", "returns", {}, False, id="extraction_not_recorded"),
        pytest.param("cancelled", "stopped", {}, False, id="cancelled"),
    ],
)
def test_each_coverage_reason_is_reached_as_its_class_says(reason, kind, document, reached):
    """Every coverage reason, by what a re-read can do about it: the class
    stands in the table, and the prospect acts on it."""
    assert COVERAGE_REACHABILITY[reason] == kind
    prospect = research_can_reach(_facts(_single_row(reason, **document), windows_left=66))
    assert ("coverage" in prospect.branches) is reached


@pytest.mark.parametrize(
    ("readings", "windows_left", "state"),
    [
        pytest.param(_record(_document("kd_a", planned=3)), 66, "read_to_end", id="read-to-the-end"),
        pytest.param(_single_row("window_limit")["t1"], 66, "reachable", id="window_limit-windows-left"),
        pytest.param(_single_row("window_limit")["t1"], 2, "budget", id="window_limit-too-few-windows-left"),
        pytest.param(_single_row("window_limit", planned=41)["t1"], 66, "beyond_reread", id="past-the-cap"),
        pytest.param(_single_row("provider_error")["t1"], 66, "reachable", id="provider_error"),
        pytest.param(_single_row("document_failed")["t1"], 66, "reachable", id="document_changed"),
        pytest.param(
            _record(_document("kd_a", planned=3, pages_without_text=(2,))), 66, "read_to_end",
            id="pages_without_text-read-to-the-end",
        ),
        pytest.param(_single_row("cancelled")["t1"], 66, "beyond_reread", id="cancelled"),
    ],
)
def test_a_version_is_selectable_exactly_when_the_prospect_can_reach_it(readings, windows_left, state):
    """The document selection of a later round and the prospect read the
    same rows and the same window rule: a version is selected as a re-read
    exactly when the coverage branch would reach it. Which state a
    non-selectable version gets decides the reader's reason, so each is
    pinned. Turns red when a version counts as reachable whenever it was
    not read to the end, or when budget and beyond_reread trade places."""
    view = ReadingView({"t1": readings}, {"t1": 0})
    reach = version_reach(view, "kd_a", "v1", max_windows=40, windows_left=windows_left)
    prospect = research_can_reach(
        _facts({"t1": readings}, reading_order={"t1": 0}, web_allowed=False, windows_left=windows_left)
    )
    assert (reach.state, reach.state == "reachable") == (state, "coverage" in prospect.branches)


@pytest.mark.parametrize(
    ("failure", "state", "coverage"),
    [
        pytest.param("knowledge.document_not_found", "beyond_reread", False, id="not-found"),
        pytest.param("document_changed", "reachable", True, id="changed"),
    ],
)
def test_a_document_whose_readings_failed_before_the_first_window_is_judged_by_that_failure(
    failure, state, coverage,
):
    """A reading that failed at window 0 holds version '' and plans no
    window. Its failure row decides, as it does for the prospect: a code a
    later reading can get past is reachable, every other one is not read
    again, and the answer names the code, since nothing was ever read. The
    transient case is a record the reader never writes (a window-0 failure
    keeps the store's code): it shows only that one rule judges both.
    Turns red when such a document counts as unread, or when its failure
    is lost."""
    readings = {"t1": _record(_document("kd_a", version="", planned=0, failure=failure, read_to_end=False))}
    view = ReadingView(readings, {"t1": 0})
    reach = version_reach(view, "kd_a", "", max_windows=40, windows_left=66)
    prospect = research_can_reach(
        _facts(readings, reading_order={"t1": 0}, web_allowed=False, windows_left=66)
    )
    assert (held_versions(view, "kd_a"), reach.state, "coverage" in prospect.branches, reach.failure) == (
        frozenset({""}), state, coverage, failure,
    )


def test_a_document_no_reading_opened_names_the_latest_failure():
    """Two earlier readings failed to open the document with different
    codes: the one later in the plan says what the store answers now, and
    a version a reading opened names no failure. Turns red when the earlier
    code wins."""
    readings = {
        "t1": _record(_document("kd_a", version="", planned=0, failure="knowledge.store_unavailable",
                                read_to_end=False)),
        "t2": _record(_document("kd_a", version="", planned=0, failure="knowledge.document_not_found",
                                read_to_end=False)),
        "t3": _record(_document("kd_b")),
    }
    view = ReadingView(readings, {"t1": 0, "t2": 1, "t3": 2})
    assert (
        version_reach(view, "kd_a", "", max_windows=40, windows_left=66).failure,
        version_reach(view, "kd_b", "v1", max_windows=40, windows_left=66).failure,
    ) == ("knowledge.document_not_found", "")


REREAD_CANDIDATES = (
    Candidate("kd_new", "Neu", "unread", "", 0),
    Candidate("kd_r1", "R1", "reachable", "v1", 30),
    Candidate("kd_r2", "R2", "reachable", "v1", 30),
    Candidate("kd_done", "Fertig", "read_to_end", "v1", 2),
    Candidate("kd_far", "Weit", "beyond_reread", "v1", 50),
    Candidate("kd_q", "Ungeprüft", "unchecked", "", 0),
)
"""Found documents in rank order, as the selection of a later round meets them."""


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(
            4,
            (["kd_r1", "kd_new", "kd_q"], [("kd_r2", "window_limit")],
             [("kd_done", "read_to_end"), ("kd_far", "beyond_reread")], 1),
            id="four-documents",
        ),
        pytest.param(
            2,
            (["kd_r1", "kd_new"], [("kd_r2", "window_limit"), ("kd_q", "document_limit")],
             [("kd_done", "read_to_end"), ("kd_far", "beyond_reread")], 0),
            id="two-documents",
        ),
    ],
)
def test_rereads_are_read_first_and_only_while_their_windows_fit(limit, expected):
    """An analysis spends one window budget across its documents in order:
    a re-read behind a new document would be cut at the window limit again.
    Re-reads go first while their windows together fit, count toward the
    document limit, and one that no longer fits waits for a later round;
    a version already read as far as a re-read reaches is not read again,
    whatever the limit. Turns red when the rank order is kept, the
    cumulative window check or the document limit is dropped, the state is
    lost, or an unchecked document is not counted."""
    selection = select_documents(REREAD_CANDIDATES, limit=limit, max_windows=40, windows_left=80)
    assert (
        [document_id for document_id, _title in selection.selected],
        [(document_id, reason) for document_id, _title, reason in selection.not_selected],
        [(entry["document_id"], entry["state"]) for entry in selection.already_read],
        selection.unchecked,
    ) == expected


def test_a_found_document_is_reached_by_its_own_branch_not_as_a_row():
    assert COVERAGE_REACHABILITY["documents_not_selected"] == "not_selected"
    prospect = research_can_reach(_facts({"t1": _record(_document("kd_a"), not_selected=("kd_b",))}))
    assert prospect.branches == ("not_selected",)


# -- the supersede rule ------------------------------------------------------ #

LIMITED = _document("kd_x", planned=4, unanalysed=("provider_error",))
READ = _document("kd_x", planned=4)
IN_PLAN_ORDER = {"t1": 0, "t2": 1}
"""t1 stands before t2 in the plan: a replan appends t2 after t1 ran."""


@pytest.mark.parametrize(
    ("readings", "order", "coverage"),
    [
        pytest.param({"t1": _record(LIMITED), "t2": _record(READ)}, {}, False, id="limited-first"),
        pytest.param({"t2": _record(READ), "t1": _record(LIMITED)}, {}, False, id="read-first"),
        pytest.param({"a1": _record(READ), "t9": _record(LIMITED)}, {}, False, id="swapped-ids"),
        pytest.param({"t1": _record(LIMITED)}, {}, True, id="without-the-reading"),
        pytest.param(
            {"t1": _record(LIMITED), "t2": _record(_document("kd_x", planned=4, unanalysed=("cancelled",)))},
            IN_PLAN_ORDER, True, id="same-version-stopped-short",
        ),
        pytest.param(
            {"t1": _record(LIMITED), "t2": _record(_document("kd_x", version="v2", planned=4))},
            IN_PLAN_ORDER, False, id="another-version-read-to-the-end",
        ),
        pytest.param(
            {"t1": _record(LIMITED), "t2": _record(
                _document("kd_x", version="v2", planned=4, unanalysed=("cancelled",)),
            )},
            IN_PLAN_ORDER, True, id="other-version-stopped-short",
        ),
        pytest.param(
            {"t1": _record(LIMITED), "t2": _record(_document("kd_x", version="v2", planned=4))},
            {"t1": 0}, True, id="another-version-without-a-plan-position",
        ),
    ],
)
def test_a_row_another_reading_completed_no_longer_counts(readings, order, coverage):
    """Readings are stored without order: a row stops counting once some
    reading of the same version read to the end without it, whichever task
    id ran first, or once a reading of another version that stands later in
    the plan completed the document. A reading that stopped short voids
    nothing, and one without a plan position voids no other version."""
    prospect = research_can_reach(_facts(readings, reading_order=order, windows_left=10))
    assert ("coverage" in prospect.branches) is coverage


@pytest.mark.parametrize(
    ("order", "coverage"),
    [
        pytest.param({"t1": 0, "t2": 1}, True, id="the-limited-version-read-later"),
        pytest.param({"t1": 1, "t2": 0}, False, id="the-complete-version-read-later"),
    ],
)
def test_an_older_complete_version_leaves_a_newer_limit_open(order, coverage):
    """A complete reading of the version before says nothing about the
    version that replaced it: only the later of two versions decides.
    Turns red when any complete version voids every other version."""
    readings = {
        "t1": _record(_document("kd_a", version="v1", planned=4)),
        "t2": _record(_document("kd_a", version="v2", planned=4, unanalysed=("window_limit",))),
    }
    prospect = research_can_reach(_facts(readings, reading_order=order, windows_left=10))
    assert ("coverage" in prospect.branches) is coverage


@pytest.mark.parametrize(
    ("second", "order", "source_bound"),
    [
        pytest.param(_document("kd_x", planned=4), {}, False, id="read-without-the-limit"),
        pytest.param(_document("kd_x", planned=4, pages_without_text=(2,)), {}, True, id="same-limit"),
        pytest.param(
            _document("kd_x", version="v2", planned=4, pages_without_text=(2,)), IN_PLAN_ORDER, True,
            id="another-version-with-the-same-limit",
        ),
    ],
)
def test_a_stored_limit_counts_while_a_reading_of_its_version_still_holds_it(second, order, source_bound):
    """Two versions that both hold a limit keep the later one's: they never
    void each other."""
    first = _document("kd_x", planned=4, pages_without_text=(2,))
    prospect = research_can_reach(
        _facts({"t1": _record(first), "t2": _record(second)}, reading_order=order)
    )
    assert ("source_bound_limit" in prospect.codes) is source_bound


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        pytest.param(
            {"t1": _record(_document("kd_a"), not_selected=("kd_b",)), "t2": _record(_document("kd_b"))},
            False, id="found-first",
        ),
        pytest.param(
            {"t2": _record(_document("kd_b")), "t1": _record(_document("kd_a"), not_selected=("kd_b",))},
            False, id="read-first",
        ),
        pytest.param(
            {"t9": _record(_document("kd_a"), not_selected=("kd_b",)), "a1": _record(_document("kd_b"))},
            False, id="swapped-ids",
        ),
        pytest.param(
            {"t1": _record(_document("kd_a"), not_selected=("kd_b",))}, True, id="never-read",
        ),
    ],
)
def test_a_not_selected_document_read_later_no_longer_counts(readings, expected):
    prospect = research_can_reach(_facts(readings))
    assert ("not_selected" in prospect.branches) is expected


@pytest.mark.parametrize(
    ("with_the_new_version", "coverage"),
    [
        pytest.param(True, False, id="new-version-read-to-the-end"),
        pytest.param(False, True, id="old-version-only"),
    ],
)
def test_a_changed_document_read_to_the_end_voids_its_failure(with_the_new_version, coverage):
    """The failure records the old version: a version-keyed rule would keep
    it reachable after the new version was read completely."""
    readings = {
        "t1": _record(_document("kd_x", version="v1", failure="document_changed", read_to_end=False)),
    }
    if with_the_new_version:
        readings["t2"] = _record(_document("kd_x", version="v2"))
    prospect = research_can_reach(_facts(readings, reading_order=IN_PLAN_ORDER, windows_left=10))
    assert ("coverage" in prospect.branches) is coverage


# -- the window rule --------------------------------------------------------- #


@pytest.mark.parametrize(
    ("planned", "unanalysed", "expected"),
    [
        pytest.param(3, ("window_limit", "window_limit"), (False, ("source_bound_limit", "web_not_needed")),
                     id="past-the-task-cap"),
        pytest.param(1, ("window_limit",), (True, ()), id="within-the-task-cap"),
    ],
)
def test_window_limit_past_the_task_cap_is_unreachable_until_windows_continue(planned, unanalysed, expected):
    """A re-read starts at window 1 and stops at the per-analysis cap again:
    a document longer than the cap never gets further. Turns red when a
    reading continues where the last one stopped."""
    readings = {"t1": _record(_document("kd_long", planned=planned, unanalysed=unanalysed))}
    prospect = research_can_reach(_facts(readings, max_windows=1, windows_left=10))
    assert (prospect.reachable, prospect.codes) == expected


@pytest.mark.parametrize(
    ("windows_left", "expected"),
    [
        pytest.param(2, (False, ("read_budget_exhausted", "web_not_needed")), id="run-budget-cut"),
        pytest.param(30, (True, ()), id="shared-analysis-budget-cut"),
    ],
)
def test_a_window_limit_is_reached_only_with_windows_for_the_whole_document(windows_left, expected):
    """Cut by the run's budget, the next read gets no further than the last
    one; cut by the budget an analysis shares between its documents, a new
    analysis with enough windows does."""
    readings = {"t1": _record(_document("kd_b", planned=30, unanalysed=("window_limit",) * 20))}
    prospect = research_can_reach(_facts(readings, windows_left=windows_left))
    assert (prospect.reachable, prospect.codes) == expected


@pytest.mark.parametrize(
    ("windows_left", "expected"),
    [
        pytest.param(0, (False, ("read_budget_exhausted", "web_not_needed")), id="no-window-left"),
        pytest.param(1, (True, ()), id="one-window-left"),
    ],
)
def test_a_failure_before_the_first_window_still_needs_one_window(windows_left, expected):
    """A document that failed before it planned a window records none; a
    re-read still claims at least one. Turns red when such a row counts as
    reachable with no window left."""
    readings = {"t1": _record(_document("kd_a", planned=0, failure="document_changed", read_to_end=False))}
    prospect = research_can_reach(_facts(readings, windows_left=windows_left))
    assert (prospect.reachable, prospect.codes) == expected


def test_no_free_task_slot_closes_every_branch():
    prospect = research_can_reach(_facts(profile=NEEDED, unread_collections=1, replan_headroom=0))
    assert (prospect.reachable, prospect.branches, prospect.codes) == (False, (), ())
    assert prospect.inputs["replan_headroom"] == 0


@pytest.mark.parametrize(
    ("web_task_planned", "branches"),
    [
        pytest.param(True, ("web",), id="web-task-planned"),
        pytest.param(False, (), id="no-web-task-planned"),
    ],
)
def test_a_sources_answer_after_a_planned_web_task_keeps_the_web_open(web_task_planned, branches):
    prospect = research_can_reach(
        _facts(sources_answered=True, web_task_planned=web_task_planned)
    )
    assert prospect.branches == branches


@pytest.mark.parametrize(
    ("changes", "codes"),
    [
        pytest.param({}, (), id="web-permitted"),
        pytest.param({"web_allowed": False}, (), id="web-not-permitted"),
        pytest.param(
            {"readings": {}, "unread_collections": 0}, ("web_not_needed",),
            id="nothing-reachable-even-with-a-free-slot",
        ),
    ],
)
def test_a_full_plan_names_no_code_for_what_a_free_slot_would_reach(changes, codes):
    """A plan without a free task slot is its own limit: the codes say what
    closed the branches themselves, so a branch a free slot would open
    names none. Turns red when a full plan reads as if nothing were
    reachable."""
    values = {
        "readings": {"t1": _record(_document("kd_a", planned=4, unanalysed=("provider_error",)))},
        "windows_left": 10, "unread_collections": 1, "replan_headroom": 0, **changes,
    }
    prospect = research_can_reach(_facts(**values))
    assert (prospect.reachable, prospect.branches, prospect.codes) == (False, (), codes)
    words = {"language": "de"}
    expected = [t(words, "agent_critic_plan_task_limit_detail")]
    if codes:
        expected += [
            t(words, "agent_research_prospect_closed_detail", codes=t(words, "research_code_web_not_needed")),
            t(words, WEB_NOT_NEEDED_NOTE_KEY),
        ]
    assert algorithm._prospect_detail(words, prospect, route="critic") == " ".join(expected)


def _words(key: str, **values: Any) -> str:
    return t({"language": "de"}, key, **values)


@pytest.mark.parametrize(
    ("prospect", "expected"),
    [
        pytest.param(
            Prospect(False, (), ("web_not_needed", "extraction_limit"), {"replan_headroom": 3}),
            [
                _words(
                    "agent_research_prospect_closed_detail",
                    codes=_words("research_code_web_not_needed"),
                ),
                _words("agent_research_prospect_extraction_detail"),
                _words(WEB_NOT_NEEDED_NOTE_KEY),
            ],
            id="beside-a-closing-code",
        ),
        pytest.param(
            Prospect(False, (), ("extraction_limit",), {"replan_headroom": 0}),
            [
                _words("agent_critic_plan_task_limit_detail"),
                _words("agent_research_prospect_extraction_detail"),
            ],
            id="beside-a-full-plan",
        ),
        pytest.param(
            Prospect(True, ("web",), ("extraction_limit",), {"replan_headroom": 3}),
            [
                _words("agent_research_prospect_open_detail", branches=_words("research_branch_web")),
                _words("agent_research_prospect_extraction_detail"),
            ],
            id="on-an-open-answer",
        ),
    ],
)
def test_the_extraction_limit_never_stands_among_the_closing_codes(prospect, expected):
    """More passages than an analysis took over closes no branch, so it
    never follows "nothing unread is reachable": it has its own sentence,
    wherever the code stands. Turns red when it is listed as a closing
    code."""
    assert algorithm._prospect_detail({"language": "de"}, prospect, route="critic") == " ".join(expected)


INPUT_NAMES = frozenset({
    "windows_left", "reachable_coverage_rows", "unreachable_coverage_rows",
    "unread_not_selected", "unread_collections", "collection_unavailable", "web_allowed",
    "web_task_kinds_admitted", "web_capacity", "needs_web", "user_demanded_web",
    "sources_answered", "web_task_planned", "replan_headroom", "failed_inputs",
})
"""The inputs a prospect decision shows, written out: a name dropped or
renamed in the module's list and its dict alike is caught here."""


def test_the_inputs_are_counts_and_flags_only():
    prospect = research_can_reach(_facts(SATURATED_READINGS, failed_inputs=("web_task_planned",)))
    assert set(prospect.inputs) == set(PROSPECT_INPUT_KEYS) == INPUT_NAMES
    assert all(
        isinstance(value, (bool, int))
        for key, value in prospect.inputs.items()
        if key != "failed_inputs"
    )
    # Input names only: never a title or an id.
    assert prospect.inputs["failed_inputs"] == ["web_task_planned"]


@pytest.mark.parametrize(
    "codes",
    [
        pytest.param(("web_not_needed",), id="web-not-needed"),
        pytest.param(("web_not_permitted",), id="web-not-permitted"),
        pytest.param(("source_bound_limit", "web_not_needed"), id="with-another-code"),
    ],
)
def test_the_web_hint_stands_only_beside_web_not_needed(codes):
    words = {"language": "de"}
    prospect = Prospect(reachable=False, branches=(), codes=codes, inputs={})
    detail = algorithm._prospect_detail(words, prospect, route="critic")
    assert (t(words, WEB_NOT_NEEDED_NOTE_KEY) in detail) is ("web_not_needed" in codes)


@pytest.mark.parametrize(
    ("reachable", "route", "sentence"),
    [
        pytest.param(False, "gate", True, id="closed-at-the-gate"),
        pytest.param(False, "critic", False, id="closed-on-the-review-route"),
        pytest.param(True, "gate", False, id="open-at-the-gate"),
    ],
)
def test_the_gate_sentence_stands_only_where_the_gate_synthesizes(reachable, route, sentence):
    """Only a gate without prospect sends the round's evidence into the
    synthesis. Turns red when the review route, or an open gate, claims
    it."""
    words = {"language": "de"}
    prospect = Prospect(
        reachable=reachable, branches=("web",) if reachable else (),
        codes=() if reachable else ("web_not_needed",), inputs={"replan_headroom": 3},
    )
    detail = algorithm._prospect_detail(words, prospect, route=route)
    assert (t(words, "agent_research_prospect_gate_detail") in detail) is sentence


# -- gathering the facts ----------------------------------------------------- #


def _plan_row(version: int, created_by: str = "agent") -> SimpleNamespace:
    return SimpleNamespace(version=version, created_by=created_by, plan_id="plan_p")


def _task(task_id: str, tool_kind: str, *, ordinal: int = 0, **params: Any) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id, tool_kind=tool_kind, params=params, status="completed", ordinal=ordinal,
    )


def _gather_deps(
    *,
    versions: dict[int, tuple[SimpleNamespace, list[SimpleNamespace]]] | None = None,
    tier: str = "",
    source_policy: SourcePolicy | None = None,
    per_run: int = 80,
) -> SimpleNamespace:
    versions = versions or {}

    async def list_plan_versions(run_id: str) -> list[SimpleNamespace]:
        return [plan for plan, _tasks in versions.values()]

    async def get_plan(run_id: str, *, version: int | None = None):
        return versions[version if version is not None else max(versions)]

    return SimpleNamespace(
        context=SimpleNamespace(run_id="run_p", providers=SimpleNamespace(search=object())),
        control=SimpleNamespace(list_plan_versions=list_plan_versions, get_plan=get_plan),
        capabilities=object(),
        source_policy=source_policy or SourcePolicy(),
        platform=SimpleNamespace(
            document_analysis_max_windows=40,
            document_analysis_max_windows_per_run=per_run,
            max_plan_tasks=8,
        ),
        skills=[],
        request=SimpleNamespace(tool_directives=[]),
        depth="normal",
        tier=tier,
        collection_scope=lambda: [],
        knowledge=None,
        visible_to=None,
    )


def _state(**changes: Any) -> dict[str, Any]:
    return {
        "question": "Q", "language": "de", "replan_rounds": 0, "outcomes": {},
        "profile": dict(PROFILE), **changes,
    }


def test_the_window_budget_left_is_the_runs_budget_minus_what_analyses_claimed():
    outcomes = {
        "t1": TaskOutcome(status="completed", document_analysis={"windows_granted": 40}).to_state(),
        "t2": TaskOutcome(status="completed", document_analysis={"windows_granted": 38}).to_state(),
    }
    facts = algorithm._prospect_facts(
        _gather_deps(per_run=80), _state(outcomes=outcomes), plan=None, tasks=[],
    )
    assert (facts.windows_left, facts.max_windows) == (2, 40)


@pytest.mark.parametrize(
    ("tasks", "expected"),
    [
        pytest.param([_task("t1", "web_instant")], ((), ("web_not_permitted",)), id="instant-budget-used"),
        pytest.param([_task("t1", "rag_query")], (("web",), ()), id="instant-budget-free"),
    ],
)
def test_the_speed_tier_counts_every_web_instant_of_the_plan(tasks, expected):
    """The schnell tier forbids research and allows one web_instant per
    plan; a carried one uses it up."""
    deps = _gather_deps(tier="schnell")
    facts = algorithm._prospect_facts(deps, _state(), plan=None, tasks=tasks)
    prospect = research_can_reach(facts)
    assert (prospect.branches, prospect.codes) == expected


def test_skills_without_web_kinds_close_the_web(monkeypatch):
    monkeypatch.setattr(algorithm, "allowed_task_kinds", lambda skills: {"rag_query", "synthesis"})
    facts = algorithm._prospect_facts(_gather_deps(), _state(), plan=None, tasks=[])
    assert (facts.web_allowed, facts.web_task_kinds_admitted) == (True, False)
    assert research_can_reach(facts).codes == ("web_not_permitted",)


def test_a_disabled_web_source_closes_the_web():
    deps = _gather_deps(source_policy=SourcePolicy(web="disabled"))
    facts = algorithm._prospect_facts(deps, _state(), plan=None, tasks=[])
    assert (facts.web_allowed, facts.web_task_kinds_admitted) == (False, False)
    assert research_can_reach(facts).codes == ("web_not_permitted",)


def test_the_prospect_reads_web_consent_without_writing_state():
    user_web = (_plan_row(1, created_by="user"), [_task("u1", "web_instant")])
    deps = _gather_deps(versions={1: user_web})
    state = _state(profile={**PROFILE, "needs_web": False})
    before = copy.deepcopy(state)
    facts = algorithm._prospect_facts(deps, state, plan=None, tasks=[])
    assert state == before
    # The consent the plan history holds reached the prospect.
    assert facts.user_demanded_web is True


def test_a_planned_web_task_is_read_from_every_plan_version():
    deps = _gather_deps(versions={
        1: (_plan_row(1), [_task("w", "web_instant"), _task("s", "synthesis")]),
        2: (_plan_row(2), [_task("f", "file_analysis"), _task("s", "synthesis")]),
    })
    assert algorithm._web_task_in_lineage(deps) is True


@pytest.mark.parametrize(
    ("store_down", "failed"),
    [
        pytest.param(True, ["user_demanded_web", "web_task_planned"], id="store-down"),
        pytest.param(False, [], id="true-negative"),
    ],
)
def test_a_failed_read_is_named_apart_from_a_true_negative(caplog, store_down, failed):
    """An unreadable plan history closes the web arms it feeds, as a plan
    without a web task would: the decision names the reads that failed, so
    the two read differently. Turns red when a failed read shows only as
    its closing value."""
    async def failing(run_id: str) -> list[Any]:
        raise RuntimeError("store down")

    deps = _gather_deps(versions={1: (_plan_row(1), [_task("f", "file_analysis")])})
    if store_down:
        deps.control.list_plan_versions = failing
    state = _state(answered_subjects=["sources"])
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        facts = algorithm._prospect_facts(deps, state, plan=None, tasks=[])
    inputs = research_can_reach(facts).inputs
    assert (inputs["web_task_planned"], inputs["user_demanded_web"]) == (False, False)
    assert inputs["failed_inputs"] == failed
    # The server's own words; a reword must be caught here.
    assert ("no web task counts as planned" in caplog.text) is store_down
    assert ("the user's web choice could not be read" in caplog.text) is store_down


def test_the_readings_are_ordered_by_their_plan_position(caplog):
    """The plan position is the order the supersede rule reads; a reading
    outside the plan has none, visibly."""
    outcomes = {
        task_id: TaskOutcome(status="completed", document_analysis=_record(_document("kd_a"))).to_state()
        for task_id in ("t1", "t2", "t9")
    }
    tasks = [_task("t1", "file_analysis", ordinal=0), _task("t2", "file_analysis", ordinal=3)]
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        facts = algorithm._prospect_facts(
            _gather_deps(), _state(outcomes=outcomes), plan=None, tasks=tasks,
        )
    assert dict(facts.reading_order) == {"t1": 0, "t2": 3}
    assert "readings of tasks t9 have no position in the current plan" in caplog.text


class _Catalog:
    def __init__(self, *entries: tuple[str, int]) -> None:
        self.entries = entries

    async def list_collections(self, **_kwargs: Any) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(id=collection_id, name=collection_id, embedding_model="", document_count=count)
            for collection_id, count in self.entries
        ]


def _collection_deps(*entries: tuple[str, int], withdrawn: frozenset[str] = frozenset()) -> SimpleNamespace:
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    deps = _gather_deps()
    deps.knowledge = _Catalog(*entries)
    deps.collection_scope = lambda: [collection_id for collection_id, _count in entries]

    def assert_collections(collection_ids: list[str]) -> list[str]:
        if set(collection_ids) & withdrawn:
            raise CollectionNotFound("Collection is outside the admitted run scope.")
        return list(collection_ids)

    deps.assert_collections = assert_collections
    return deps


def test_a_withdrawn_collection_is_named_and_warned(caplog):
    deps = _collection_deps(("kc_a", 2), ("kc_b", 2), withdrawn=frozenset({"kc_b"}))
    tasks = [_task("t1", "rag_query", collection_ids=["kc_a"])]
    outcomes = {"t1": {"status": "completed"}}
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = algorithm._unread_collections(deps, tasks, outcomes)
    assert result == (0, True, False)
    # The server's own words; a reword must be caught here.
    assert (
        "Collection kc_b counts as unavailable for the prospect "
        "(error_type=CollectionNotFound)."
    ) in caplog.text


@pytest.mark.parametrize(
    ("tasks", "outcomes", "expected"),
    [
        pytest.param(
            [_task("t1", "rag_query", collection_ids=["kc_a"])], {"t1": {"status": "completed"}},
            (1, False, False), id="one-collection-searched",
        ),
        pytest.param(
            [_task("t1", "rag_query")], {"t1": {"status": "insufficient_evidence"}},
            (0, False, False), id="the-whole-scope-searched",
        ),
        pytest.param(
            [_task("t1", "rag_query", collection_ids=["kc_a"])], {"t1": {"status": "failed"}},
            (2, False, False), id="a-failed-search-searched-nothing",
        ),
    ],
)
def test_an_unread_collection_is_one_no_executed_task_searched(tasks, outcomes, expected):
    deps = _collection_deps(("kc_a", 2), ("kc_b", 3), ("kc_empty", 0))
    assert algorithm._unread_collections(deps, tasks, outcomes) == expected


class _FailingCatalog:
    async def list_collections(self, **_kwargs: Any) -> list[SimpleNamespace]:
        raise RuntimeError("catalog down")


@pytest.mark.parametrize(
    ("knowledge", "expected", "failed"),
    [
        pytest.param(_FailingCatalog(), (0, True, True), ["unread_collections"], id="catalog-down"),
        pytest.param(None, (0, False, True), ["unread_collections"], id="no-catalog-wired"),
        pytest.param(_Catalog(("kc_a", 2), ("kc_b", 2)), (1, False, False), [], id="catalog-read"),
    ],
)
def test_an_unreadable_catalog_is_named_as_a_failed_input(knowledge, expected, failed):
    """A catalog that cannot be read leaves the count at the value that
    closes the branch, as an empty catalog would: the decision names the
    failed read. Turns red when the count reads as a true zero."""
    deps = _collection_deps(("kc_a", 2), ("kc_b", 2))
    deps.knowledge = knowledge
    tasks = [_task("t1", "rag_query", collection_ids=["kc_a"])]
    outcomes = {"t1": {"status": "completed"}}
    assert algorithm._unread_collections(deps, tasks, outcomes) == expected
    facts = algorithm._prospect_facts(deps, _state(outcomes=outcomes), plan=None, tasks=tasks)
    assert research_can_reach(facts).inputs["failed_inputs"] == failed


# -- the decision on the real graph ----------------------------------------- #

SATURATED_PLAN = {
    "summary_markdown": "Beide Dokumente vollständig lesen.",
    "tasks": [
        {"id": "t1", "title": "Anweisung lesen", "tool_kind": "file_analysis",
         "objective": "Fristen belegen.", "queries": ["Fristen der Anweisung"]},
        {"id": "t2", "title": "Anweisung gegenlesen", "tool_kind": "file_analysis",
         "objective": "Fristen gegenprüfen.", "queries": ["Fristen gegenprüfen"]},
        {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": ["t1", "t2"]},
    ],
    "success_criteria": [
        "Alle Fristen belegt.", "Alle Ausnahmen belegt.", "Zuständigkeit genannt.",
        "Meldeweg genannt.", "Quellen genannt.",
    ],
}

NEUTRAL_REFERENCE = {
    "label": "K1", "document_id": "kd_short", "chunk_index": 0, "title": "Anweisung",
    "excerpt": "Die Frist beträgt 30 Tage.",
}


def _saturated_analysis(monkeypatch) -> None:
    def run_file_analysis(deps: Any, state: Any, task: Any) -> TaskOutcome:
        return TaskOutcome(
            status="completed", summary="Dokumentanalyse.",
            evidence=[dict(NEUTRAL_REFERENCE)],
            document_analysis=copy.deepcopy(SATURATED_READINGS[task.task_id]),
        )

    monkeypatch.setattr(algorithm, "_run_file_analysis", run_file_analysis)


def _critic_information_once(*gaps: str):
    calls = {"n": 0}

    def critic(prompt: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            return review_verdict(prompt)
        return review_verdict(
            prompt, unmet={index: ("information", gap) for index, gap in enumerate(gaps, start=1)},
        )

    return critic


def _prospects(client, run_id: str) -> list[dict[str, Any]]:
    return [
        event["data"] for event in _stored_events(client, run_id)
        if event["type"] == "inqtrix.agent.activity"
        and event["data"].get("kind") == "research_prospect"
    ]


def test_documents_read_to_the_end_without_web_need_open_no_research_round(monkeypatch):
    """Both documents read to the end, the web not needed, no collection
    left: the review's open criteria cannot send the run back to research.
    The planner is not called, and the decision names why."""
    _saturated_analysis(monkeypatch)
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": False, "needs_files": True},
        "ExecutionPlanModel": SATURATED_PLAN,
        "DeepReviewVerdict": _critic_information_once("g1", "g2"),
    })
    client = _collection_client(monkeypatch, llm, ("kc_abn", "Abnahme Langdokument", 2))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous", knowledge_filters={"collection_ids": ["kc_abn"]},
        )
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [prospect] = _prospects(client, run_id)
    assert llm.calls.count("structured:ReplanDeltaModel") == 0
    assert (prospect["route"], prospect["reachable"], prospect["codes"]) == (
        "critic", False, ["web_not_needed", "extraction_limit"],
    )


@pytest.mark.parametrize("option", ["q1_o1", "q1_o2"])
def test_a_sources_answer_alone_opens_no_research_round(monkeypatch, option):
    """Code cannot read whether an answer about the sources includes the
    web; only a web task planned after it keeps the web open."""
    from tests.agents.test_web_need import SOURCES_QUESTION
    from tests.agents.test_workspace_agent import _answer_first_round

    _saturated_analysis(monkeypatch)
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {
            **PROFILE, "needs_web": False, "needs_files": True,
            "scope_clarity": "ambiguous", "clarification_questions": [SOURCES_QUESTION],
        },
        "ExecutionPlanModel": SATURATED_PLAN,
        "DeepReviewVerdict": _critic_information_once("g1"),
    })
    client = _collection_client(monkeypatch, llm, ("kc_abn", "Abnahme Langdokument", 2))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous", knowledge_filters={"collection_ids": ["kc_abn"]},
        )
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(client, run_id, {"q1": {"option_ids": [option]}})
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [prospect] = _prospects(client, run_id)
    assert prospect["inputs"]["sources_answered"] is True
    assert llm.calls.count("structured:ReplanDeltaModel") == 0


def test_an_unread_collection_opens_the_planner(monkeypatch):
    """A released collection no task searched is material research can
    still reach: the review's research round happens, decided once."""
    def run_file_analysis(deps: Any, state: Any, task: Any) -> TaskOutcome:
        return TaskOutcome(
            status="completed", summary="Dokumentanalyse.", evidence=[dict(NEUTRAL_REFERENCE)],
            document_analysis=_record(_document("kd_a1")),
        )

    monkeypatch.setattr(algorithm, "_run_file_analysis", run_file_analysis)
    plan = {
        **SATURATED_PLAN,
        "tasks": [
            {**SATURATED_PLAN["tasks"][0], "params": {"collection_ids": ["kc_a"]}},
            {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": ["t1"]},
        ],
    }
    delta = replan_delta({
        "id": "k2", "title": "Sammlung B durchsuchen", "tool_kind": "rag_query",
        "queries": ["Fristen in Sammlung B"], "params": {"collection_ids": ["kc_b"]},
    })
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": False, "needs_files": True},
        "ExecutionPlanModel": plan,
        "ReplanDeltaModel": delta,
        "DeepReviewVerdict": _critic_information_once("g1"),
    })
    client = _collection_client(monkeypatch, llm, ("kc_a", "A", 2), ("kc_b", "B", 3))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous",
            knowledge_filters={"collection_ids": ["kc_a", "kc_b"]},
        )
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        prospects = _prospects(client, run_id)
    assert llm.calls.count("structured:ReplanDeltaModel") == 1
    [prospect] = prospects
    assert (prospect["route"], prospect["branches"]) == ("critic", ["collection"])
    assert set(prospect["inputs"]) == INPUT_NAMES
    assert prospect["activity_id"] == "prospect:critic:1"


def _one_replan(monkeypatch, unmet: dict[int, tuple[str, str]]) -> list[str]:
    """The unread-collection run above with a review that leaves *unmet*
    open once: the prompts of its one replan."""
    def run_file_analysis(deps: Any, state: Any, task: Any) -> TaskOutcome:
        return TaskOutcome(
            status="completed", summary="Dokumentanalyse.", evidence=[dict(NEUTRAL_REFERENCE)],
            document_analysis=_record(_document("kd_a1")),
        )

    calls = {"n": 0}

    def critic(prompt: str) -> dict[str, Any]:
        calls["n"] += 1
        return review_verdict(prompt, unmet=unmet if calls["n"] == 1 else {})

    monkeypatch.setattr(algorithm, "_run_file_analysis", run_file_analysis)
    plan = {
        **SATURATED_PLAN,
        "tasks": [
            {**SATURATED_PLAN["tasks"][0], "params": {"collection_ids": ["kc_a"]}},
            {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": ["t1"]},
        ],
        "success_criteria": ["Kriterium A", "Kriterium B"],
    }
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {
            **PROFILE, "needs_web": False, "needs_files": True, "success_criteria": ["PROFILE-ONLY"],
        },
        "ExecutionPlanModel": plan,
        "ReplanDeltaModel": replan_delta({
            "id": "k2", "title": "Sammlung B durchsuchen", "tool_kind": "rag_query",
            "queries": ["Fristen in Sammlung B"], "params": {"collection_ids": ["kc_b"]},
        }),
        "DeepReviewVerdict": critic,
    })
    client = _collection_client(monkeypatch, llm, ("kc_a", "A", 2), ("kc_b", "B", 3))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous", knowledge_filters={"collection_ids": ["kc_a", "kc_b"]},
        )
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    return [prompt for name, prompt in llm.prompts if name == "ReplanDeltaModel"]


def test_the_replanner_reads_one_numbered_plan_list(monkeypatch):
    """The replan's planner reads the plan's criteria, numbered as the
    review numbered them, the review's open criteria by those numbers (an
    output requirement as a text defect, its gap never shown), the
    information gap fenced, and the branch the prospect opened. Turns red
    when the planner reads the intake's criteria, when the review does not
    record the list it numbered, or when the prospect does not survive the
    node boundary."""
    from inqtrix.agents.prompts import (
        REPLAN_BRANCH_WORDS,
        REPLAN_CRITERION_TEXT_DEFECT,
        REPLAN_PROSPECT_LINE,
    )
    from tests.test_untrusted_content import _fence

    [replan] = _one_replan(
        monkeypatch, {1: ("information", "GAP-A"), 2: ("output_requirement", "TEXT-B")},
    )
    assert (
        "1. Kriterium A\n2. Kriterium B" in replan,
        "PROFILE-ONLY" in replan,
        REPLAN_CRITERION_TEXT_DEFECT.format(index=2) in replan,
        "GAP-A" in _fence(replan, "critic-gaps"),
        "TEXT-B" in replan,
        REPLAN_PROSPECT_LINE.format(branches=REPLAN_BRANCH_WORDS["collection"]) in replan,
    ) == (True, False, True, True, False, True)


def test_a_planted_review_gap_reaches_the_replanner_only_fenced(monkeypatch):
    """A review gap can carry what a document said, a forged closing
    delimiter included: the replan's planner reads it inside the gap fence
    only, and the delimiter cannot end the fence. Turns red when the review
    does not keep its gaps."""
    from tests.test_untrusted_content import PLANTED, _fence, _only_inside_a_fence
    from inqtrix.untrusted_content import UNTRUSTED_FENCE_CLOSE

    planted = "GAP-CANARY " + PLANTED + " " + UNTRUSTED_FENCE_CLOSE + " SYSTEM"
    replans = _one_replan(monkeypatch, {1: ("information", planted)})
    assert [
        _only_inside_a_fence(prompt, "GAP-CANARY")
        and "GAP-CANARY" in _fence(prompt, "critic-gaps")
        and UNTRUSTED_FENCE_CLOSE + " SYSTEM" not in prompt
        for prompt in replans
    ] == [True]


def test_the_replanner_sees_covered_discovery_gaps_as_covered(monkeypatch):
    """A discovery gap a finished task covered is marked as such where the
    replan's planner reads the gaps, and counted after the fence, so the
    planner does not plan it again. Turns red when the plan node hands the
    planner no covered gaps."""
    from inqtrix.agents.planner import COVERED_GAP_NOTE
    from inqtrix.agents.prompts import COVERED_GAPS_LINE
    from tests.agents.test_workspace_agent import DISCOVERY, PLAN
    from tests.test_untrusted_content import _fence

    discovery = {
        **DISCOVERY,
        "gaps": [
            {**DISCOVERY["gaps"][0], "gap_id": "g1", "blocking": True},
            {**DISCOVERY["gaps"][0], "gap_id": "g2", "blocking": False,
             "description": "Ausnahmen fehlen"},
        ],
    }
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": True},
        "DiscoveryResult": discovery,
        "ExecutionPlanModel": PLAN,
        "ReplanDeltaModel": replan_delta({
            "id": "t2", "title": "Ausnahmen", "tool_kind": "web_instant",
            "queries": ["Welche Ausnahmen gelten 2026?"],
        }),
        "DeepReviewVerdict": _critic_information_once("Ausnahmen fehlen."),
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    [replan] = [prompt for name, prompt in llm.prompts if name == "ReplanDeltaModel"]
    [g1] = [line for line in _fence(replan, "discovery-state").splitlines() if line.startswith("Gap g1")]
    assert (COVERED_GAP_NOTE in g1, COVERED_GAPS_LINE.format(count=1) in replan) == (True, True)


# -- the replan gate --------------------------------------------------------- #


def _reference(document_id: str, excerpt: str) -> dict[str, Any]:
    return {
        "label": "K1", "document_id": document_id, "chunk_index": 0,
        "title": "Anweisung", "excerpt": excerpt,
    }


def _analysis_by_task(monkeypatch, outcomes: dict[str, TaskOutcome]) -> None:
    def run_file_analysis(deps: Any, state: Any, task: Any) -> TaskOutcome:
        return copy.deepcopy(outcomes[task.task_id])

    monkeypatch.setattr(algorithm, "_run_file_analysis", run_file_analysis)


def _analysis_plan(*task_ids: str) -> dict[str, Any]:
    return {
        "summary_markdown": "Dokumente lesen.",
        "tasks": [
            *(
                {"id": task_id, "title": f"Anweisung {task_id}", "tool_kind": "file_analysis",
                 "objective": "Fristen belegen.", "queries": [f"Fristen {task_id}"]}
                for task_id in task_ids
            ),
            {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": list(task_ids)},
        ],
        "success_criteria": ["Alle Fristen belegt."],
    }


INSUFFICIENT = TaskOutcome(status="insufficient_evidence", summary="Nichts gefunden.")


def _gate_before_the_first_review(monkeypatch) -> tuple[ScriptedLLM, str]:
    _analysis_by_task(monkeypatch, {
        "t1": TaskOutcome(
            status="completed", summary="Gelesen.",
            evidence=[_reference("kd_a", "GATE-EVIDENCE-A: Die Frist beträgt 30 Tage.")],
            document_analysis=_record(_document("kd_a")),
        ),
        "t2": INSUFFICIENT,
    })
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": False, "needs_files": True},
        "ExecutionPlanModel": _analysis_plan("t1", "t2"),
        "SufficiencyJudgement": {"coverage": "uncovered", "missing": ["Fristen"]},
    })
    return llm, "GATE-EVIDENCE-A"


def _gate_after_a_research_round(monkeypatch) -> tuple[ScriptedLLM, str]:
    # The replan's new analyses stay by decision: this test is about the
    # gate after they ran, not about the plan-time check.
    monkeypatch.setattr(algorithm, "_rejects_new_analysis", lambda *_args, **_kwargs: False)
    _analysis_by_task(monkeypatch, {
        "t1": TaskOutcome(
            status="completed", summary="Gelesen.",
            evidence=[_reference("kd_a", "Die Frist beträgt 30 Tage.")],
            document_analysis=_record(_document("kd_a"), not_selected=("kd_b",)),
        ),
        "t3": TaskOutcome(
            status="completed", summary="Gelesen.",
            evidence=[_reference("kd_b", "GATE-EVIDENCE-B: Die Ausnahme gilt 14 Tage.")],
            document_analysis=_record(_document("kd_b")),
        ),
        "t4": INSUFFICIENT,
    })
    delta = replan_delta(
        {"id": "t3", "title": "Anweisung B", "tool_kind": "file_analysis",
         "objective": "Ausnahmen belegen.", "queries": ["Ausnahmen Anweisung B"]},
        {"id": "t4", "title": "Anweisung C", "tool_kind": "file_analysis",
         "objective": "Zuständigkeit belegen.", "queries": ["Zuständigkeit"]},
    )
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": False, "needs_files": True},
        "ExecutionPlanModel": _analysis_plan("t1"),
        "ReplanDeltaModel": delta,
        "SufficiencyJudgement": {"coverage": "uncovered", "missing": ["Zuständigkeit"]},
        "DeepReviewVerdict": _critic_information_once("Ausnahmen fehlen."),
    })
    return llm, "GATE-EVIDENCE-B"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(_gate_before_the_first_review, (0, 0), id="before-the-first-review"),
        pytest.param(_gate_after_a_research_round, (1, 1), id="after-a-research-round"),
    ],
)
def test_a_gate_replan_without_prospect_synthesizes_the_evidence_in_hand(monkeypatch, setup, expected):
    """The gate would replan, but nothing unread is reachable: the round's
    evidence goes into the synthesis, and no round is counted for research
    that never ran."""
    llm, canary = setup(monkeypatch)
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        rounds = client.get(f"/v1/runs/{run_id}/result").json()["execution"]["limits"]["replan_rounds"]["used"]
        gates = [prospect for prospect in _prospects(client, run_id) if prospect["route"] == "gate"]
    outlines = [prompt for name, prompt in llm.prompts if name == "ReportOutline"]
    assert (llm.calls.count("structured:ReplanDeltaModel"), rounds) == expected
    assert canary in outlines[-1]
    [gate] = gates
    assert (gate["reachable"], gate["round"]) == (False, expected[1] + 1)
    assert t({"language": "de"}, "agent_research_prospect_gate_detail") in gate["detail"]
