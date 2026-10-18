"""The outcome record of a mission run (``inqtrix.agent.outcome``).

One record per run that leaves the algorithm with a decision, built from
the review's own report: what the review did, what stayed open and why.
The builder is pinned on literal states, the sending on the real graph with
a scripted model, the parent's view on the projection.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any

import pytest

from inqtrix.agents import algorithm, critic_phase, run_outcome
from inqtrix.agents.run_outcome import build_outcome, outcome_words
from inqtrix.agents.scheduler import TaskOutcome
from inqtrix.exceptions import AgentCancelled
from inqtrix.i18n import t
from inqtrix.providers.base import StructuredLLMResponse
from inqtrix.runs.shared import ANSWER_DRAFT_EVENT, OUTCOME_EVENT, expand_run_event
from inqtrix.search_result import GroundedSearchResult
from tests.agents.test_research_prospect import (
    NEUTRAL_REFERENCE,
    SATURATED_PLAN,
    SATURATED_READINGS,
    _critic_information_once,
    _saturated_analysis,
)
from tests.agents.test_workspace_agent import (
    PROFILE,
    FakeSearch,
    ScriptedLLM,
    _collection_client,
    _stored_events,
    approve_pending,
    make_agent_client,
    platform_without_web_preview,
    replan_delta,
    review_verdict,
    submit_agent,
    wait_status,
)
from tests.test_untrusted_content import PLANTED


def _records(client, run_id: str) -> list[dict[str, Any]]:
    return [
        event["data"] for event in _stored_events(client, run_id)
        if event["type"] == OUTCOME_EVENT
    ]


def _critic(**fields: Any) -> dict[str, Any]:
    """A review report as the review node stores it."""
    return {
        "verdict": "pass", "round": 1, "revision": "none", "accounting": "complete",
        "research_limits": [], "prospect_codes": [], "criteria_covered": [],
        "criteria_uncovered": [], "open_lines": [], "reach": {"reread": 0, "cited": 0},
        **fields,
    }


def _built(state: dict[str, Any], guard=str) -> dict[str, Any]:
    return build_outcome(state, run_id="run_r", final="completed", guard=guard)


# -- the builder ------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            {"critic": _critic(verdict="revise", revision="unverified"),
             "last_review": {"status": "completed", "round": 1}},
            ("completed", 1, "unverified", "complete"), id="applied-revision",
        ),
        pytest.param(
            {"critic": _critic(verdict="revise", revision="discarded"),
             "last_review": {"status": "completed", "round": 2}},
            ("completed", 2, "discarded", "complete"), id="discarded-revision",
        ),
        pytest.param(
            {"critic": _critic(accounting="incomplete"),
             "last_review": {"status": "completed", "round": 1}},
            ("completed", 1, "none", "incomplete"), id="unsettled-criterion",
        ),
        pytest.param(
            {"critic": None,
             "last_review": {"status": "unavailable", "round": 1, "reason": "failed"}},
            ("unavailable", 1, "none", "incomplete"), id="review-unavailable",
        ),
        pytest.param({}, ("not_run", 0, "none", "incomplete"), id="no-review"),
        pytest.param(
            {"critic": {"verdict": "pass", "criteria_uncovered": [{"text": "A", "kind": "information"}],
                        "open_points": ["A"]}},
            ("completed", 0, "none", "incomplete"), id="report-without-accounting",
        ),
    ],
)
def test_the_record_says_what_the_review_did(state, expected):
    """A missing field is never read as complete or as revised."""
    record = _built(state)
    assert (record["review"], record["round"], record["revision"], record["accounting"]) == expected
    assert record["outcome_id"] == "run_r:outcome"


def test_a_report_from_before_the_record_is_built_with_a_warning(caplog):
    """A checkpoint resumed across the upgrade carries a review report
    without its round, revision and accounting: the record reads them as
    missing, and says so. Turns red when the record is built silently."""
    old = {"critic": {"verdict": "pass", "criteria_uncovered": [], "open_points": []}}
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        record = _built(old)
    assert (record["review"], record["round"], record["revision"], record["accounting"]) == (
        "completed", 0, "none", "incomplete",
    )
    # The server's own words; a reword must be caught here.
    assert (
        "Outcome record built from a review report without last_review, round, "
        "revision, accounting (a checkpoint from before the record)"
    ) in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _built({"critic": _critic(), "last_review": {"status": "completed", "round": 1}})
        _built({})
    assert "Outcome record built" not in caplog.text


def test_a_named_finding_line_carries_its_reason():
    state = {
        "critic": _critic(verdict="revise", revision="discarded", open_lines=[
            {"kind": "finding", "text": "x", "reason": "anchor_missing"},
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    assert _built(state)["lines"][0]["reasons"] == ["anchor_missing"]


def test_a_finding_line_without_its_reason_is_built_with_a_warning(caplog):
    """A report written before the per-finding outcome: the line stands,
    without a reason, and the server says why. A reword is caught here."""
    state = {
        "critic": _critic(verdict="revise", revision="discarded", open_lines=[
            {"kind": "finding", "text": "x"},
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _built(state)
    assert "Outcome record lines built from a review report without their reasons" in caplog.text


def test_a_verdict_stored_without_remove_labels_or_criterion_index_still_validates():
    """A parked parent reads a role's verdict stored by an older release:
    a finding without ``remove_labels`` or ``criterion_index`` validates
    with their defaults."""
    from inqtrix.agents.kernel.work_profiles import resolve_work_profile

    stored = {
        "complete": False, "grounded": True, "contradictions_named": True,
        "findings": [{"target": "chat", "artifact_id": "", "finding": "x",
                      "kind": "output_requirement", "quote": "y", "basis_ids": []}],
        "criteria": [], "not_checked": [],
    }
    [item] = resolve_work_profile("review").result_model.model_validate(stored).findings
    assert (item.remove_labels, item.criterion_index) == ([], None)


def test_open_information_criteria_are_counted_per_code():
    state = {
        "critic": _critic(
            verdict="open",
            research_limits=["no_prospect"],
            prospect_codes=["web_not_needed"],
            criteria_uncovered=[
                {"index": 1, "text": "A", "kind": "information", "gap": "a"},
                {"index": 2, "text": "B", "kind": "information", "gap": "b"},
                {"index": 3, "text": "C", "kind": "output_requirement", "gap": "c"},
            ],
            open_lines=[
                {"kind": "information", "text": "Kriterium offen: A — a"},
                {"kind": "information", "text": "Kriterium offen: B — b"},
                {"kind": "output_requirement", "text": "Kriterium offen: C — c"},
                {"kind": "finding", "text": "Absatz 2 belegen."},
                {"kind": "not_checked", "text": "Quelle 3 nicht gelesen."},
            ],
        ),
        "last_review": {"status": "completed", "round": 1},
        "outcomes": {
            "t1": TaskOutcome(status="completed", document_analysis=SATURATED_READINGS["t1"]).to_state(),
        },
    }
    record = _built(state)
    assert record["limits"] == []
    assert record["codes"] == ["web_not_needed", "extraction_limit"]
    assert record["open"] == {
        "by_code": {"web_not_needed": 2, "extraction_limit": 2},
        "by_kind": {
            "information": 2, "text_defect": 1, "revised": 0, "marked": 0, "finding": 1, "not_checked": 1,
        },
    }
    assert [line["reasons"] for line in record["lines"]] == [
        ["web_not_needed", "extraction_limit"], ["web_not_needed", "extraction_limit"], [], [], [],
        ["extraction_limit"],
    ]


def test_a_passing_review_still_names_the_extraction_limit():
    state = {
        "critic": _critic(),
        "last_review": {"status": "completed", "round": 1},
        "outcomes": {
            "t1": TaskOutcome(status="completed", document_analysis=SATURATED_READINGS["t2"]).to_state(),
        },
    }
    record = _built(state)
    assert (record["codes"], record["open"]["by_code"]) == (["extraction_limit"], {})


def test_record_lines_pass_the_reader_guard():
    state = {
        "language": "de",
        "references": [{
            "label": "K1", "document_id": "kd_0123abcd", "chunk_index": 2,
            "title": "Anweisung", "excerpt": "Die Frist beträgt 30 Tage.",
        }],
        "critic": _critic(
            verdict="revise", revision="discarded",
            open_lines=[{"kind": "finding", "text": "Der Absatz zu kd_0123abcd ist nicht belegt."}],
        ),
        "last_review": {"status": "completed", "round": 1},
    }
    record = build_outcome(
        state, run_id="run_r", final="completed",
        guard=lambda text: algorithm._guard_reader_text(state, text),
    )
    assert "kd_0123abcd" not in json.dumps(record["lines"])
    assert record["lines"][0]["text"].startswith("Der Absatz zu ")


# -- revised criteria, applied findings, reach and the reading limit --------- #


def _finding_record(
    finding_id: str, state: str, reason: str = "", origin: str = "model", recheck: str = "",
    recheck_reason: str = "",
) -> dict[str, Any]:
    return {
        "finding_id": finding_id, "origin": origin, "target": "art_memo",
        "kind": "output_requirement", "criterion": None, "state": state, "reason": reason,
        "recheck": recheck, "recheck_reason": recheck_reason,
    }


def _revised_and_named(language: str = "de") -> dict[str, Any]:
    """A completed review whose revision applied the two findings of one
    criterion and named the finding of another, left two points unchecked
    and re-read all twelve cited references, over documents read to the end
    that held more than the analysis took over."""
    return {
        "language": language,
        "critic": _critic(
            verdict="revise", revision="partially_discarded",
            criteria_uncovered=[
                {"index": 2, "text": "Meldewege", "kind": "output_requirement",
                 "gap": "a", "state": "revised"},
                {"index": 3, "text": "Fristen", "kind": "output_requirement",
                 "gap": "b", "state": "open"},
            ],
            open_lines=[
                {"kind": "output_requirement", "text": "Meldewege bearbeitet.",
                 "reason": "criterion_revised"},
                {"kind": "output_requirement", "text": "Kriterium offen: Fristen", "reason": ""},
                {"kind": "not_checked", "text": "Quelle A nicht gelesen.", "reason": ""},
                {"kind": "not_checked", "text": "Quelle B nicht gelesen.", "reason": ""},
                {"kind": "finding", "text": "Fristen nennen.",
                 "reason": "segment_without_basis_label"},
            ],
            findings=[
                _finding_record("finding_1", "applied"),
                _finding_record("finding_2", "applied"),
                _finding_record("finding_3", "named", "segment_without_basis_label"),
            ],
            reach={"reread": 12, "cited": 12},
        ),
        "last_review": {"status": "completed", "round": 1},
        "outcomes": {
            "t1": TaskOutcome(status="completed", document_analysis=SATURATED_READINGS["t2"]).to_state(),
        },
    }


def test_revised_criteria_are_counted_apart_from_open_ones():
    by_kind = _built(_revised_and_named())["open"]["by_kind"]
    assert (by_kind["text_defect"], by_kind["revised"]) == (1, 1)


def test_a_verified_criterion_is_no_text_defect():
    """A criterion whose edits the re-check held was worked on like a
    revised one: it is no open text defect."""
    state = {
        "critic": _critic(verdict="revise", criteria_uncovered=[
            {"index": 1, "text": "A", "kind": "output_requirement", "gap": "a", "state": "verified"},
            {"index": 2, "text": "B", "kind": "output_requirement", "gap": "b", "state": "open"},
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    by_kind = _built(state)["open"]["by_kind"]
    assert (by_kind["text_defect"], by_kind["revised"]) == (1, 1)


def test_a_criterion_entry_without_its_state_reads_open():
    """A report written before its criteria carried a state never reads a
    criterion as revised."""
    state = {
        "critic": _critic(verdict="open", criteria_uncovered=[
            {"index": 1, "text": "A", "kind": "output_requirement", "gap": "a"},
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    assert _built(state)["open"]["by_kind"]["text_defect"] == 1


def test_the_extraction_limit_has_its_reader_line():
    """The code alone reached no reader: the record says what it means. The
    server's own words; a reword must be caught here."""
    state = {
        "language": "de",
        "critic": _critic(),
        "last_review": {"status": "completed", "round": 1},
        "outcomes": {
            "t1": TaskOutcome(status="completed", document_analysis=SATURATED_READINGS["t2"]).to_state(),
        },
    }
    assert _built(state)["lines"][-1] == {
        "kind": "limit",
        "text": (
            "In zu Ende gelesenen Dokumenten sah die Auswertung mehr Stellen, als "
            "sie übernehmen konnte."
        ),
        "reasons": ["extraction_limit"],
    }


def test_the_record_counts_applied_findings_as_the_revision_does():
    """A server finding that lost its overlap was fixed, if at all, by
    another finding's edit: it is no applied finding of its own, as in the
    revision's state."""
    state = {
        "critic": _critic(verdict="revise", revision="partially_discarded", findings=[
            _finding_record("finding_1", "applied"),
            _finding_record("finding_2", "applied"),
            _finding_record("finding_3", "named", "anchor_missing"),
            _finding_record("finding_4", "applied", "overlap_edit", origin="server"),
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    assert _built(state)["applied"] == 2


def test_a_report_without_reach_is_built_with_a_warning(caplog):
    """A checkpoint resumed across the upgrade: its review report has no
    reach, so the summary names none, and the server says why. A reword is
    caught here."""
    critic = _critic()
    del critic["reach"]
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        record = _built({"critic": critic, "last_review": {"status": "completed", "round": 1}})
    assert ("reach" in record, (
        "Outcome record built from a review report without its reach (a checkpoint "
        "from before the report carried it); the summary names no reach."
    ) in caplog.text) == (False, True)


# -- the record's summary ------------------------------------------------------ #


@pytest.mark.parametrize(
    ("state", "final", "expected"),
    [
        pytest.param(
            _revised_and_named(), "completed",
            "Geprüft · 2 Befunde überarbeitet, nicht nachgeprüft · 1 Befund nicht "
            "angewendet · 2 Punkte nicht geprüft · 1 Kriterium offen · Lesegrenze · "
            "12 von 12 zitierten Belegen nachgelesen",
            id="revised-named-unchecked-limited",
        ),
        pytest.param(
            {"critic": _critic(reach={"reread": 1, "cited": 1}),
             "last_review": {"status": "completed", "round": 1}},
            "completed", "Geprüft · ohne Befund · 1 von 1 zitierten Beleg nachgelesen",
            id="passed",
        ),
        pytest.param(
            {"critic": _critic(reach={"reread": 1, "cited": 3, "provider_only": 2}),
             "last_review": {"status": "completed", "round": 1}},
            "completed",
            "Geprüft · ohne Befund · 1 von 3 zitierten Belegen nachgelesen · "
            "2 Belege nur mit Anbieter-Kontext, ohne gespeicherten Quelltext",
            id="provider-context-only-references",
        ),
        pytest.param(
            {"critic": _critic(open_lines=[
                {"kind": "not_checked", "text": "Quelle A nicht gelesen.", "reason": ""},
                {"kind": "not_checked", "text": "Quelle B nicht gelesen.", "reason": ""},
            ]), "last_review": {"status": "completed", "round": 1}},
            "completed", "Geprüft · 2 Punkte nicht geprüft", id="only-unchecked-points",
        ),
        pytest.param(
            {"critic": _critic(
                verdict="open",
                criteria_uncovered=[
                    {"index": 1, "text": "A", "kind": "output_requirement", "gap": "a", "state": "open"},
                ],
                open_lines=[{"kind": "output_requirement", "text": "Kriterium offen: A — a", "reason": ""}],
            ), "last_review": {"status": "completed", "round": 1}},
            "completed", "Geprüft · 1 Kriterium offen", id="open-criterion-without-a-finding",
        ),
        pytest.param(
            {"last_review": {"status": "waived", "round": 0}}, "completed",
            "Ohne Prüfung (Stufe)", id="waived",
        ),
        pytest.param(
            {"critic": None, "last_review": {"status": "unavailable", "round": 1, "reason": "failed"}},
            "completed", "Prüfung nicht auswertbar, Ergebnis ungeprüft", id="unavailable",
        ),
        pytest.param({}, "completed", "Ohne Prüfung beendet", id="not-run"),
        pytest.param({}, "cancelled", "Vor der Prüfung beendet", id="ended-before-the-review"),
    ],
)
def test_outcome_words_name_every_open_kind(state, final, expected):
    """"Ohne Befund" only when nothing stayed open: unchecked points and
    open criteria are named, never passed over. The server's own words; a
    reword must be caught here."""
    record = build_outcome(state, run_id="run_r", final=final, guard=str)
    assert outcome_words(record, "de") == expected


def test_no_outcome_word_calls_an_unchecked_edit_fixed():
    """An applied edit that nothing checked again is revised, never fixed."""
    summaries = [
        _built(_revised_and_named(language))["summary"] for language in ("de", "en")
    ]
    assert all("2 Befunde" in text or "2 findings" in text for text in summaries), (
        "precondition: the summaries count the applied findings"
    )
    assert [
        text for text in summaries
        if any(word in text.lower() for word in ("behoben", "fixed"))
    ] == []


def test_verified_findings_are_the_only_fixed_ones():
    """Only an applied edit the re-check held is called fixed; the other
    applied edit stays revised, not re-checked. The server's own words; a
    reword must be caught here."""
    state = {
        "critic": _critic(verdict="revise", revision="unverified", findings=[
            _finding_record("finding_1", "applied", recheck="held"),
            _finding_record("finding_2", "applied", recheck="held"),
            _finding_record("finding_3", "applied"),
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    assert (
        "2 Befunde behoben und nachgeprüft · 1 Befund überarbeitet, nicht nachgeprüft"
        in _built({**state, "language": "de"})["summary"]
    )


@pytest.mark.parametrize(("language", "expected"), [
    ("de", "Geprüft · 1 Befund behoben und nachgeprüft · 7 Befunde überarbeitet, nicht "
           "nachgeprüft (2 mit Belegen ohne gespeicherten Quelltext, 1 nur Formatierung, "
           "1 zu umfangreich, 1 ohne mögliche Nachprüfung, 1 ohne eindeutiges "
           "Prüfergebnis)"),
    ("en", "Reviewed · 1 finding fixed and re-checked · 7 findings revised, not re-checked "
           "(2 citing sources without stored text, 1 formatting only, 1 too large to "
           "re-check, 1 with no re-check possible, 1 with an inconclusive re-check)"),
])
def test_the_summary_says_why_revised_findings_stayed_unverified(language, expected):
    """Each revised finding the re-check did not hold is counted under its
    reason, in a fixed order. A server finding never reaches the re-check
    and has no reason, so the reasons may count fewer than the revised. The
    server's own words; a reword must be caught here."""
    state = {
        "language": language,
        "critic": _critic(verdict="revise", revision="unverified", findings=[
            _finding_record("finding_1", "applied", recheck="held"),
            _finding_record("finding_2", "applied", recheck_reason="no_source_text"),
            _finding_record("finding_3", "applied", recheck_reason="no_source_text"),
            _finding_record("finding_4", "applied", recheck_reason="format_only"),
            _finding_record("finding_5", "applied", recheck_reason="too_large"),
            _finding_record("finding_6", "applied", recheck_reason="check_unavailable"),
            _finding_record("finding_7", "applied", recheck_reason="inconclusive"),
            _finding_record("finding_8", "applied", origin="server"),
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    record = _built(state)
    assert (record["unverified_reasons"], record["summary"]) == ({
        "no_source_text": 2, "format_only": 1, "too_large": 1, "check_unavailable": 1,
        "inconclusive": 1,
    }, expected)


def test_every_recheck_reason_has_words_in_both_languages():
    """A reason without its words would reach a reader as a code."""
    from inqtrix.agents.kernel.review_edits import RECHECK_REASONS
    from inqtrix.i18n import MESSAGES

    assert RECHECK_REASONS, "precondition: the closed set is not empty"
    for reason in RECHECK_REASONS:
        entry = MESSAGES.get(f"outcome_reason_{reason}")
        assert entry is not None and {"de", "en"} <= set(entry), reason


def test_a_report_without_recheck_reasons_names_none_and_warns(caplog):
    """A checkpoint from before the findings carried their reasons reads as
    before, with a WARNING. The server's own words; a reword must be caught
    here."""
    finding = _finding_record("finding_1", "applied")
    del finding["recheck_reason"]
    state = {
        "language": "de",
        "critic": _critic(verdict="revise", revision="unverified", findings=[finding]),
        "last_review": {"status": "completed", "round": 1},
    }
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        record = _built(state)
    assert (record["unverified_reasons"], record["summary"], (
        "Outcome record built from a review report without re-check reasons (a "
        "checkpoint from before the findings carried them); the summary names none."
    ) in caplog.text) == ({}, "Geprüft · 1 Befund überarbeitet, nicht nachgeprüft", True)


def test_the_summary_is_built_from_counts_only():
    """The summary crosses to a delegating parent; a line's text, model
    prose or source text, never enters it."""
    state = {
        "critic": _critic(verdict="revise", revision="discarded", open_lines=[
            {"kind": "finding", "text": PLANTED, "reason": "anchor_missing"},
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    record = _built(state)
    assert record["lines"][0]["text"] == PLANTED, "precondition: the line carries the text"
    assert PLANTED not in record["summary"]


def test_the_summary_passes_the_reader_guard():
    """The summary is shown wherever the lines are not, so it passes the
    run's reader guard like every line."""
    record = _built(_revised_and_named(), guard=lambda text: f"<{text}>")
    assert (record["summary"][:8], record["summary"][-1]) == ("<Geprüft", ">")


def test_the_record_speaks_the_runs_language():
    assert _built(_revised_and_named("en"))["summary"] == (
        "Reviewed · 2 findings revised, not re-checked · 1 finding not applied · "
        "2 points not checked · 1 criterion open · reading limit · 12 of 12 cited "
        "references re-read"
    )


def test_the_record_crosses_the_event_boundary_whole():
    """Every store keeps and streams the payload the event boundary returns;
    its allowlist must know every field the record carries."""
    record = _built(_revised_and_named())
    _, [(event_type, stored)] = expand_run_event(OUTCOME_EVENT, record, status="running")
    assert (event_type, stored) == (OUTCOME_EVENT, record)


# -- the record on the real graph ------------------------------------------- #


def test_documents_read_to_the_end_without_web_need_name_both_codes(monkeypatch):
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
        wait_status(client, run_id, {"completed", "failed"})
        [record] = _records(client, run_id)
    assert record["codes"] == ["web_not_needed", "extraction_limit"]
    assert record["limits"] == []
    assert record["open"]["by_code"] == {"web_not_needed": 2, "extraction_limit": 2}


def test_a_review_that_cannot_run_is_recorded_as_unavailable(monkeypatch):
    """A negative verdict that names nothing is no usable review: the record
    says so, which only a checkpointed ``last_review`` can carry across the
    graph's node boundary."""
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": lambda prompt: review_verdict(prompt, complete=False),
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [record] = _records(client, run_id)
        unavailable = [
            event for event in _stored_events(client, run_id)
            if event["data"].get("kind") == "critic_unavailable"
        ]
    assert len(unavailable) == 1, "precondition: the review could not run"
    assert (record["review"], record["round"], record["accounting"]) == ("unavailable", 1, "incomplete")


def test_a_mission_record_carries_its_reviews_reach_and_summary(monkeypatch):
    """The mission stores its review report field by field: the reach reaches
    the record, and its details name it. The mission cites a web source
    kept only as the search provider's context, so the summary counts the
    memo's one statement as attributed by the provider, and the details say
    so of the reference in one aggregated line."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
        [record] = _records(client, run_id)
    assert (record["reach"], record["summary"], record["details"]) == (
        {"reread": 1, "cited": 1, "provider_only": 1},
        "Geprüft · 1 Aussage: 1 vom Anbieter zugeschrieben",
        "ohne Befund · 1 von 1 zitierten Beleg nachgelesen · "
        "1 Beleg nur mit Anbieter-Kontext, ohne gespeicherten Quelltext",
    )


def test_a_speed_tier_mission_records_its_review_as_waived(monkeypatch):
    """The Stufe skips the review: the record says so instead of reading
    the review as not run."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "schnell"})
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [record] = _records(client, run_id)
    assert (record["review"], record["summary"]) == ("waived", "Ohne Prüfung (Stufe)")


def test_a_reviewed_mission_shows_no_draft_and_publishes_once(monkeypatch):
    """The draft is the kernel's: a mission publishes its reviewed answer
    once, with no draft before it."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
        [record] = _records(client, run_id)
        kinds = [event["type"] for event in _stored_events(client, run_id)]
    assert (record["review"], kinds.count(ANSWER_DRAFT_EVENT),
            kinds.count("inqtrix.answer.started")) == ("completed", 0, 1)


INVALID_DELTA = replan_delta(
    {"id": "t2", "title": "Ohne Suchfrage", "tool_kind": "web_instant", "queries": []},
)
EMPTY_DELTA = replan_delta()


def _exit_run(monkeypatch, exit_kind: str) -> tuple[Any, str]:
    critic = _critic_information_once("Eine Gegenposition fehlt.")
    overrides: dict[str, Any] = {"DeepReviewVerdict": critic}
    if exit_kind == "replan_invalid":
        overrides["ReplanDeltaModel"] = INVALID_DELTA
    elif exit_kind == "replan_empty":
        overrides["ReplanDeltaModel"] = EMPTY_DELTA
    else:
        overrides["ReplanDeltaModel"] = replan_delta({
            "id": "t2", "title": "Gegenposition", "tool_kind": "web_instant",
            "queries": ["Welche Gegenposition nennt die Aufsicht?"],
        })
    if exit_kind == "no_gap":
        # The review's gap reaches the planner node unclassified: the one
        # way the node's own guard can still close a research round.
        monkeypatch.setattr(critic_phase, "uncovered_criteria", lambda critic, *, kind=None: [])
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(overrides=overrides))
    return client, exit_kind


@pytest.mark.parametrize("exit_kind", ["replan_invalid", "replan_empty", "no_gap", "replan_rejected"])
def test_a_research_round_that_ends_before_a_task_names_its_reason(monkeypatch, exit_kind):
    client, reason = _exit_run(monkeypatch, exit_kind)
    with client:
        if reason == "replan_rejected":
            run_id = submit_agent(client, autonomy="balanced")
            wait_status(client, run_id, {"waiting_for_approval"})
            approve_pending(client, run_id)
            wait_status(client, run_id, {"waiting_for_approval"})
            approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            [pending] = [item for item in approvals if item["status"] == "pending"]
            assert pending["kind"] == "replan", "precondition: the review's research round"
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{pending['approval_id']}",
                json={"decision": "reject"},
            )
            assert decided.status_code == 200, decided.text
        else:
            run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [record] = _records(client, run_id)
        exhausted = [
            event["data"] for event in _stored_events(client, run_id)
            if event["data"].get("kind") == "critic_research_exhausted"
        ]
    assert (record["limits"], record["open"]["by_code"]) == ([reason], {reason: 1})
    assert [(item["reason"], item["reasons"], item["codes"]) for item in exhausted] == [
        (reason, [reason], []),
    ]


def test_a_round_that_ends_before_a_task_carries_the_codes_of_its_prospect(monkeypatch):
    """The review's prospect opened the round and the planner node ends it:
    the codes cross the node boundary only in the checkpointed
    ``research_prospect``. Turns red when the stored prospect is lost
    between the nodes, or the exit names no codes of its own."""
    _saturated_analysis(monkeypatch)
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": True, "needs_files": True},
        "ExecutionPlanModel": SATURATED_PLAN,
        "DeepReviewVerdict": _critic_information_once("g1"),
        "ReplanDeltaModel": EMPTY_DELTA,
    })
    client = _collection_client(monkeypatch, llm, ("kc_abn", "Abnahme Langdokument", 2))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous", knowledge_filters={"collection_ids": ["kc_abn"]},
        )
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        [record] = _records(client, run_id)
        activities = [
            event["data"] for event in _stored_events(client, run_id)
            if event["type"] == "inqtrix.agent.activity"
        ]
    prospects = [item for item in activities if item.get("kind") == "research_prospect"]
    assert [(item["route"], item["reachable"], item["codes"]) for item in prospects] == [
        ("critic", True, ["extraction_limit"]),
    ], "precondition: an open prospect that names a code"
    assert [
        (item["reason"], item["codes"]) for item in activities
        if item.get("kind") == "critic_research_exhausted"
    ] == [("replan_empty", ["extraction_limit"])]
    assert record["open"]["by_code"] == {"replan_empty": 1, "extraction_limit": 1}


def test_the_record_lines_pass_the_runs_reader_guard(monkeypatch):
    """The run guards every line it sends: a review gap that names the
    collection a cited reference came from reaches the record with the
    identifier replaced. The review names the documents it read by their
    title itself, a collection id it leaves standing, so only the run's
    reader guard can catch this one. Turns red when the record's lines skip
    the reader guard."""
    reference = {**NEUTRAL_REFERENCE, "collection_id": "kc_abn"}

    def run_file_analysis(deps: Any, state: Any, task: Any) -> TaskOutcome:
        return TaskOutcome(
            status="completed", summary="Dokumentanalyse.", evidence=[dict(reference)],
            document_analysis=copy.deepcopy(SATURATED_READINGS[task.task_id]),
        )

    monkeypatch.setattr(algorithm, "_run_file_analysis", run_file_analysis)
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "needs_web": False, "needs_files": True},
        "ExecutionPlanModel": SATURATED_PLAN,
        "DeepReviewVerdict": lambda prompt: review_verdict(
            prompt, unmet={1: ("output_requirement", "Die Sammlung kc_abn fehlt im Text.")},
        ),
    })
    client = _collection_client(monkeypatch, llm, ("kc_abn", "Abnahme Langdokument", 2))
    with client:
        run_id = submit_agent(
            client, autonomy="autonomous", knowledge_filters={"collection_ids": ["kc_abn"]},
        )
        wait_status(client, run_id, {"completed", "failed"})
        [record] = _records(client, run_id)
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        stored = next(item for item in artifacts if item["kind"] == "critic_report")
        report = client.get(
            f"/v1/runs/{run_id}/artifacts/{stored['artifact_id']}"
        ).json()["payload"]
    [(kind, text)] = [(line["kind"], line["text"]) for line in report["open_lines"]]
    assert "kc_abn" in text, "precondition: the review left the identifier standing"
    [line] = [line for line in record["lines"] if line["kind"] != "limit"]
    assert (line["kind"], line["text"]) == (
        kind, text.replace("kc_abn", t({"language": "de"}, "agent_reader_identifier_removed")),
    )


# -- one record per exit ----------------------------------------------------- #


class _FailingSections(ScriptedLLM):
    def complete_structured(self, prompt: str, *, schema_name: str, **kwargs: Any) -> StructuredLLMResponse:
        if schema_name == "SectionText":
            raise RuntimeError("synthesis unavailable")
        return super().complete_structured(prompt, schema_name=schema_name, **kwargs)


class _CancelledSections(ScriptedLLM):
    def complete_structured(self, prompt: str, *, schema_name: str, **kwargs: Any) -> StructuredLLMResponse:
        if schema_name == "SectionText":
            raise AgentCancelled("Cancelled while writing.")
        return super().complete_structured(prompt, schema_name=schema_name, **kwargs)


def _completed(monkeypatch) -> tuple[Any, Any]:
    client = make_agent_client(monkeypatch)
    return client, lambda: submit_agent(client, autonomy="autonomous")


def _cooperative_cancel(monkeypatch) -> tuple[Any, Any]:
    started = threading.Event()
    release = threading.Event()

    class BlockingSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            started.set()
            release.wait(10)
            raise RuntimeError("search stopped by the cancel")

    client = make_agent_client(
        monkeypatch, search=BlockingSearch(), platform=platform_without_web_preview(),
    )

    def run() -> str:
        run_id = submit_agent(client, autonomy="autonomous")
        assert started.wait(10), "the task never started"
        assert client.post(f"/v1/runs/{run_id}/cancel").status_code == 200
        release.set()
        return run_id

    return client, run


def _token_budget(monkeypatch) -> tuple[Any, Any]:
    client = make_agent_client(monkeypatch, max_tokens_per_run=10)
    return client, lambda: submit_agent(client, autonomy="autonomous")


def _node_cancel(monkeypatch) -> tuple[Any, Any]:
    client = make_agent_client(monkeypatch, llm=_CancelledSections())
    return client, lambda: submit_agent(client, autonomy="autonomous")


def _state_failure(monkeypatch) -> tuple[Any, Any]:
    bad_plan = {"summary_markdown": "x", "tasks": [
        {"id": "t1", "title": "Nur Suche", "tool_kind": "rag_query"},
    ]}
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(overrides={"ExecutionPlanModel": bad_plan}))
    return client, lambda: submit_agent(client, autonomy="autonomous")


def _graph_error(monkeypatch) -> tuple[Any, Any]:
    client = make_agent_client(monkeypatch, llm=_FailingSections())
    return client, lambda: submit_agent(client, autonomy="autonomous")


@pytest.mark.parametrize(
    ("setup", "status", "final"),
    [
        pytest.param(_completed, "completed", "completed", id="completed"),
        pytest.param(_cooperative_cancel, "cancelled", "cancelled", id="cooperative-cancel"),
        pytest.param(_token_budget, "failed", "failed", id="token-budget"),
        pytest.param(_node_cancel, "cancelled", "cancelled", id="cancel-raised-in-a-node"),
        pytest.param(_state_failure, "failed", "failed", id="state-failure"),
        pytest.param(_graph_error, "failed", "failed", id="graph-error"),
    ],
)
def test_every_mission_boundary_sends_one_outcome_record(monkeypatch, setup, status, final):
    client, start = setup(monkeypatch)
    with client:
        run_id = start()
        summary = wait_status(client, run_id, {"completed", "failed", "cancelled"})
        records = _records(client, run_id)
    assert summary["status"] == status, summary
    assert [(record["outcome_id"], record["final"]) for record in records] == [
        (f"{run_id}:outcome", final),
    ]


def test_a_mission_cancelled_while_parked_has_no_outcome_record(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="balanced")
        wait_status(client, run_id, {"waiting_for_approval"})
        assert client.post(f"/v1/runs/{run_id}/cancel").status_code == 200
        wait_status(client, run_id, {"cancelled"})
        assert _records(client, run_id) == []


def test_a_failing_outcome_build_keeps_the_original_error(monkeypatch, caplog):
    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("BUILDER-BROKE")

    monkeypatch.setattr(run_outcome, "build_outcome", broken)
    client = make_agent_client(monkeypatch, llm=_FailingSections())
    with caplog.at_level(logging.WARNING, logger="inqtrix"), client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed", "cancelled"})
        assert _records(client, run_id) == []
    assert summary["status"] == "failed"
    assert "BUILDER-BROKE" not in json.dumps(summary)
    assert "synthesis unavailable" in json.dumps(summary)
    assert (
        "Run outcome record could not be sent (final=failed, error_type=RuntimeError)."
        in caplog.text
    )


def test_the_record_state_is_read_before_the_checkpoint_goes(monkeypatch):
    """The normal exit sends from the state in hand: a checkpointer that
    fails on delete cannot lose a finished run's record."""
    client = make_agent_client(monkeypatch)
    saver = client.container.registry.get("workspace_agent")._checkpointer

    def failing_delete(run_id: str) -> None:
        raise RuntimeError("checkpoint store down")

    monkeypatch.setattr(saver, "delete_thread", failing_delete)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        records = _records(client, run_id)
    assert summary["status"] == "failed", "precondition: the delete failed the run"
    assert "checkpoint store down" in json.dumps(summary)
    assert [record["final"] for record in records] == ["completed"]


def test_a_record_carries_the_review_report_it_was_built_from(monkeypatch):
    """The record's review facts are the stored report's: its round and the
    lines it left open, in the reader's language."""
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": lambda prompt: review_verdict(
            prompt, unmet={1: ("output_requirement", "Die Anbieter fehlen.")},
        ),
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
        [record] = _records(client, run_id)
    assert (record["review"], record["round"], record["revision"]) == ("completed", 1, "none")
    assert record["open"]["by_kind"] == {
        "information": 0, "text_defect": 1, "revised": 0, "marked": 0, "finding": 0, "not_checked": 0,
    }
    assert [line["kind"] for line in record["lines"]] == ["output_requirement"]
    assert "Die Anbieter fehlen." in record["lines"][0]["text"]



# -- a review from general knowledge ----------------------------------------- #


@pytest.mark.parametrize(("findings", "language", "expected"), [
    pytest.param(
        [_finding_record("finding_1", "applied", recheck_reason="knowledge_only"),
         _finding_record("finding_2", "applied", recheck_reason="knowledge_only")],
        "de", "Geprüft aus Modellwissen, ohne Quellen · 2 Befunde überarbeitet", id="de-revised",
    ),
    pytest.param([], "de", "Geprüft aus Modellwissen, ohne Quellen · ohne Befund", id="de-clean"),
    pytest.param(
        [_finding_record("finding_1", "applied", recheck_reason="knowledge_only")],
        "en", "Reviewed from general knowledge, without sources · 1 finding revised",
        id="en-revised",
    ),
])
def test_a_review_from_general_knowledge_names_its_basis_once(findings, language, expected):
    """Without stored evidence the review checks from general knowledge and
    the summary says so once; nothing is re-checked against a source, so no
    word says re-checked or not re-checked. The server's own words; a
    reword must be caught here."""
    state = {
        "language": language,
        "critic": _critic(
            verdict="revise" if findings else "pass",
            revision="unverified" if findings else "none",
            findings=findings, basis="knowledge",
        ),
        "last_review": {"status": "completed", "round": 1},
    }
    record = _built(state)
    assert (record["basis"], record["summary"]) == ("knowledge", expected)


def test_a_report_without_its_basis_reads_as_one_with_sources():
    """A checkpoint from before the basis was recorded reads as the review
    it was: one with sources, worded as before."""
    state = {
        "language": "de",
        "critic": _critic(verdict="revise", revision="unverified", findings=[
            _finding_record("finding_1", "applied", recheck_reason="no_source_text"),
        ]),
        "last_review": {"status": "completed", "round": 1},
    }
    record = _built(state)
    assert (record["basis"], record["summary"]) == (
        "evidence",
        "Geprüft · 1 Befund überarbeitet, nicht nachgeprüft (1 mit Belegen ohne gespeicherten "
        "Quelltext)",
    )


# -- the statements of the delivered text ------------------------------------- #


CLAIMS = {"chat": {"total": 24, "verified": 3, "source_text": 2, "attributed": 15, "insufficient": 4}}
MARK = {
    "output_id": "chat", "quote": "Der Bonus beträgt 16 Prozent. [W1]", "state": "insufficient",
    "reason": "finding", "note": "Nicht ausreichend belegt: W1 nennt keinen Prozentsatz.",
}


def _claimed(claims: dict[str, Any] = CLAIMS, **fields: Any) -> dict[str, Any]:
    return {
        "critic": _critic(
            claims=claims, marks=[dict(MARK)],
            reach={"reread": 0, "cited": 7, "provider_only": 7}, **fields,
        ),
        "last_review": {"status": "completed", "round": 1},
        "language": "de",
    }


@pytest.mark.parametrize(("language", "summary", "details"), [
    ("de",
     "Geprüft · 24 Aussagen: 3 verifiziert, 2 mit Quelltext, 15 vom Anbieter "
     "zugeschrieben, 4 nicht ausreichend belegt",
     "ohne Befund · 0 von 7 zitierten Belegen nachgelesen · 7 Belege nur mit "
     "Anbieter-Kontext, ohne gespeicherten Quelltext"),
    ("en",
     "Reviewed · 24 statements: 3 verified, 2 with source text, 15 attributed by the "
     "search provider, 4 insufficiently supported",
     "no findings · 0 of 7 cited references re-read · 7 references with "
     "search-provider context only, no stored source text"),
])
def test_the_summary_counts_the_statements_and_the_details_carry_the_rest(
    language, summary, details,
):
    """The summary is the plaque: how many statements carry which state;
    what the review did besides moves into the details. The server's own
    words; a reword must be caught here."""
    record = _built({**_claimed(), "language": language})
    assert (record["summary"], record["details"]) == (summary, details)


def test_a_plaque_without_findings_still_counts_its_statements():
    """A review that found nothing still says what the statements rest on."""
    claims = {"chat": {"total": 12, "verified": 0, "source_text": 0, "attributed": 12, "insufficient": 0}}
    assert _built(_claimed(claims))["summary"] == "Geprüft · 12 Aussagen: 12 vom Anbieter zugeschrieben"


def test_the_plaque_sums_the_statements_of_every_output():
    claims = {
        "chat": {"total": 1, "verified": 0, "source_text": 0, "attributed": 0, "insufficient": 1},
        "art_doc": {"total": 3, "verified": 1, "source_text": 0, "attributed": 2, "insufficient": 0},
    }
    record = _built(_claimed(claims))
    assert (record["claims"], record["summary"]) == (
        claims,
        "Geprüft · 4 Aussagen: 1 verifiziert, 2 vom Anbieter zugeschrieben, "
        "1 nicht ausreichend belegt",
    )


def test_one_statement_reads_in_the_singular():
    claims = {"chat": {"total": 1, "verified": 0, "source_text": 0, "attributed": 0, "insufficient": 1}}
    assert _built(_claimed(claims))["summary"] == "Geprüft · 1 Aussage: 1 nicht ausreichend belegt"


def test_without_statements_the_summary_reads_as_before():
    record = _built(_claimed({}))
    assert (record["summary"], "details" in record, "claims" in record) == (
        "Geprüft · ohne Befund · 0 von 7 zitierten Belegen nachgelesen · 7 Belege nur mit "
        "Anbieter-Kontext, ohne gespeicherten Quelltext",
        False, False,
    )


def test_a_marked_passage_is_no_finding_the_revision_left_unapplied():
    state = _claimed(open_lines=[{
        "kind": "marked", "text": "Nicht ausreichend belegt, nur am Text des "
        "Suchanbieters messbar: W1 nennt keinen Prozentsatz.", "reason": "provider_context_only",
    }])
    record = _built(state)
    assert (
        record["open"]["by_kind"]["finding"], record["open"]["by_kind"]["marked"],
        "nicht angewendet" in record["summary"] + record["details"],
    ) == (0, 1, False)


def test_a_marks_note_passes_the_reader_guard_and_its_quote_stays_the_texts_own():
    """The quote is the delivered text itself, which the reader reads anyway
    and the desk finds it by; the note is the review's prose."""
    record = _built(_claimed(), guard=lambda text: f"<{text}>")
    assert record["marks"] == [{**MARK, "note": f"<{MARK['note']}>"}]


def test_the_statements_cross_the_event_boundary_whole():
    record = _built(_claimed())
    assert {"claims", "marks", "details"} <= set(record), "precondition: the record carries them"
    _, [(event_type, stored)] = expand_run_event(OUTCOME_EVENT, record, status="running")
    assert (event_type, stored) == (OUTCOME_EVENT, record)
