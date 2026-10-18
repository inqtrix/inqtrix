"""Direct regression tests for multi-signal stop heuristics."""

import logging

import pytest

from inqtrix.settings import AgentSettings
from inqtrix.strategies import MultiSignalStopCriteria


def _base_state(**overrides):
    state = {
        "round": 2,
        "question": "Sollen GKV-Leistungen privatisiert werden?",
        "uncovered_aspects": [],
        "source_tier_counts": {"primary": 1, "mainstream": 1, "low": 0},
        "claim_quality_score": 0.8,
        "claim_status_counts": {"verified": 2, "contested": 0, "unverified": 0},
        "claim_needs_primary_total": 0,
        "claim_needs_primary_supported": 0,
        "utility_scores": [],
        "done": False,
        "evidence_sufficiency": 5,
        "prev_citation_count": 0,
        "falsification_triggered": False,
        "competing_events": "",
        "prev_competing_events": "",
        "context": ["Block 1", "Block 2", "Block 3"],
    }
    state.update(overrides)
    return state


def test_check_stagnation_forces_stop_after_broad_low_confidence_search():
    strategy = MultiSignalStopCriteria(AgentSettings())

    conf, detected = strategy.check_stagnation(
        _base_state(),
        conf=4,
        prev_conf=4,
        n_citations=30,
        falsification_just_triggered=False,
    )

    assert detected is True
    assert conf == 4


def test_compute_utility_stops_uniformly_after_two_low_utility_rounds():
    # The former DE-policy utility-stop suppression is removed: two consecutive
    # low-utility rounds stop the loop regardless of question topic or how weak
    # the evidence still is.
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state(
        uncovered_aspects=["Status quo mit konkretem Datum"],
        source_tier_counts={"primary": 0, "mainstream": 0, "low": 1},
        claim_quality_score=0.2,
        claim_status_counts={"verified": 0, "contested": 0, "unverified": 2},
        claim_needs_primary_total=1,
        claim_needs_primary_supported=0,
        utility_scores=[0.1],
        evidence_sufficiency=1,
        prev_citation_count=30,
    )

    utility, should_stop = strategy.compute_utility(
        state,
        conf=4,
        prev_conf=4,
        n_citations=31,
    )

    assert utility < 0.15
    assert should_stop is True
    assert state["done"] is True


@pytest.mark.parametrize("status", ["verified", "attributed"])
def test_compute_utility_rewards_new_supported_claims(status):
    """Evidence gain counts supported claims, verified or attributed alike:
    a provider that returns no source text gains evidence like any other."""
    strategy = MultiSignalStopCriteria(AgentSettings())
    counts = {"verified": 0, "attributed": 0, "contested": 0, "unverified": 0}
    counts[status] = 3
    state = _base_state(
        utility_scores=[0.1],
        evidence_sufficiency=5,
        prev_citation_count=30,
        prev_supported_claim_count=0,
        claim_status_counts=counts,
    )

    utility, should_stop = strategy.compute_utility(
        state,
        conf=5,
        prev_conf=5,
        n_citations=30,
    )

    assert utility >= 0.3
    assert should_stop is False
    assert state["prev_supported_claim_count"] == 3


def test_check_plateau_suppressed_when_evidence_depth_gap_active():
    strategy = MultiSignalStopCriteria(AgentSettings(max_rounds=5))
    state = _base_state(
        round=2,
        _evidence_depth_gap_active=True,
        done=False,
    )

    should_stop = strategy.check_plateau(
        state,
        conf=8,
        prev_conf=8,
        stagnation_detected=False,
    )

    assert should_stop is False
    assert state["done"] is False


def test_extract_competing_events_skips_cap_when_same_event_persists_in_round_three():
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state(
        round=3,
        prev_competing_events="Verwechslung zwischen Entwurf A und Entwurf B",
        event_mode=True,
    )

    conf = strategy.extract_competing_events(
        state,
        "COMPETING_EVENTS: Verwechslung zwischen Entwurf A und Entwurf B\n",
        conf=strategy._confidence_stop,
    )

    assert conf == strategy._confidence_stop
    assert state["competing_events"] == "Verwechslung zwischen Entwurf A und Entwurf B"


def test_extract_competing_events_keeps_model_text_out_of_operational_logs(caplog):
    strategy = MultiSignalStopCriteria(AgentSettings())
    logger = logging.getLogger("inqtrix")
    previous_level = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)

    private_event = "PRIVATE_COMPETING_EVENT_TEXT"
    try:
        state = _base_state(event_mode=True)
        strategy.extract_competing_events(
            state,
            f"COMPETING_EVENTS: {private_event}\n",
            conf=5,
        )
        strategy.extract_competing_events(
            _base_state(event_mode=True),
            "COMPETING_EVENTS: Keine\n",
            conf=5,
        )
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)

    assert state["competing_events"] == private_event
    assert "competing_events_present=true" in caplog.text
    assert f"chars={len(private_event)}" in caplog.text
    assert "competing_events_present=false" in caplog.text
    assert private_event not in caplog.text
    assert "Keine" not in caplog.text


def test_extract_evidence_scores_emits_warning_on_parse_miss():
    """When EVIDENCE_* fields are missing, a warning is surfaced to SSE.

    The iteration_log already records ``_evidence_*_parsed=False`` flags,
    but a UI consumer relying on the live event stream would miss the
    fallback without an explicit ``emit_progress`` call.
    """
    events: list[tuple[str, dict]] = []
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state(
        language="de",
        _run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    # Eval text without EVIDENCE_CONSISTENCY / EVIDENCE_SUFFICIENCY tokens.
    strategy.extract_evidence_scores(state, "GAPS: nothing\n", conf=5)

    progress_events = [
        payload for event_type, payload in events
        if event_type == "inqtrix.progress.message"
    ]
    assert progress_events, "expected at least one progress event"
    assert any(p["severity"] == "warning" for p in progress_events)
    assert state["_evidence_consistency_parsed"] is False
    assert state["_evidence_sufficiency_parsed"] is False


def test_extract_evidence_scores_silent_when_both_present():
    """No spurious progress event when both scores parse cleanly."""
    events: list[tuple[str, dict]] = []
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state(
        language="de",
        _run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    strategy.extract_evidence_scores(
        state,
        "EVIDENCE_CONSISTENCY: 7\nEVIDENCE_SUFFICIENCY: 6\n",
        conf=5,
    )

    progress_events = [
        payload for event_type, payload in events
        if event_type == "inqtrix.progress.message"
    ]
    assert progress_events == []
    assert state["_evidence_consistency_parsed"] is True
    assert state["_evidence_sufficiency_parsed"] is True


def test_competing_events_are_an_event_mode_mechanism_only():
    """Outside event mode (a comparison or market question) the evaluator is
    not asked for COMPETING_EVENTS; text it volunteers anyway is ignored and
    never caps the confidence. Before, 29 of 29 evaluations of a comparison
    run carried text and the cap forced a second round 13 times."""
    strategy = MultiSignalStopCriteria(AgentSettings(confidence_stop=7))
    quiet = _base_state(event_mode=False, competing_events="alt")
    conf = strategy.extract_competing_events(
        quiet, "COMPETING_EVENTS: Reform A vs Reform B\n", conf=9
    )
    assert conf == 9
    assert quiet["competing_events"] == ""
    # In event mode the same text is recorded and caps a stop-ready confidence.
    live = _base_state(event_mode=True, round=1)
    capped = strategy.extract_competing_events(
        live, "COMPETING_EVENTS: Reform A vs Reform B\n", conf=9
    )
    assert live["competing_events"] == "Reform A vs Reform B"
    assert capped == 6


def test_a_missing_competing_events_line_resets_the_previous_rounds_text():
    """In event mode a round without the line means "no ambiguity this
    round": the previous text must not survive into the plan block, the
    answer prompt and the metric as if it had been confirmed again."""
    strategy = MultiSignalStopCriteria(AgentSettings(confidence_stop=7))
    state = _base_state(
        event_mode=True, round=2,
        competing_events="Reform A vs Reform B", prev_competing_events="Reform A vs Reform B",
    )
    conf = strategy.extract_competing_events(state, "CONFIDENCE: 8\nGAPS: Keine\n", conf=8)
    assert conf == 8
    assert state["competing_events"] == ""


def test_the_runs_stop_threshold_beats_the_strategys_construction_settings():
    """The strategy instance is shared by every run of the process; the
    evaluate node writes the run's threshold into ``s["_confidence_stop"]``
    and every cap reads it from there (fallback: the construction value)."""
    strategy = MultiSignalStopCriteria(AgentSettings(confidence_stop=6))
    strict = _base_state(event_mode=True, round=1, _confidence_stop=9)
    assert strategy.extract_competing_events(strict, "COMPETING_EVENTS: A vs B\n", conf=9) == 8
    default = _base_state(event_mode=True, round=1)
    assert strategy.extract_competing_events(default, "COMPETING_EVENTS: A vs B\n", conf=9) == 5



@pytest.mark.parametrize("line, delta", [
    ("CONTRADICTIONS: Yes, the sources fundamentally contradict each other.", 2),
    ("CONTRADICTIONS: Yes, one outlet reports a different date.", 1),
    ("CONTRADICTIONS: Ja, die Quellen widersprechen sich grundlegend.", 2),
])
def test_check_contradictions_reads_an_affirmative_lead_token(line, delta):
    """The evaluator answers Yes/No (the prompt is English); a German
    ``Ja`` still counts. Severity keywords cover both languages."""
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state()
    stop = strategy._stop_for(state)
    assert strategy.check_contradictions(state, line, stop) == stop - delta


@pytest.mark.parametrize("line", [
    "CONTRADICTIONS: No",
    "CONTRADICTIONS: Nein",
    "CONTRADICTIONS: None; the January figures agree across sources.",
])
def test_check_contradictions_ignores_negative_lines_and_incidental_ja(line):
    """A substring scan for ``ja`` capped the confidence on ``January``;
    only the leading token decides."""
    strategy = MultiSignalStopCriteria(AgentSettings())
    state = _base_state()
    stop = strategy._stop_for(state)
    assert strategy.check_contradictions(state, line, stop) == stop
