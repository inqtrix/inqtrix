"""TaskOutcome durable-checkpoint (de)serialization tolerance.

The outcomes map is threaded through the LangGraph checkpoint, which the
Postgres saver persists across process restarts and deploys — so a value
written by version N is rehydrated by version N+1's code. These tests pin
that a ``TaskOutcome`` schema change (a field added, removed, or renamed)
does NOT break resume of an in-flight run, and that the one unreconcilable
case fails loudly instead of with an opaque ``TypeError``.
"""

from __future__ import annotations

import json

import pytest

from inqtrix.agents.algorithm import _task_evidence_note
from inqtrix.agents.scheduler import CheckpointSchemaError, TaskOutcome


def test_to_state_from_state_round_trips():
    outcome = TaskOutcome(
        status="completed",
        summary="did the thing",
        evidence=[{"label": "src", "url": "https://x"}],
        claims=[{"text": "c"}],
        child_run_id="rn_child",
        failure_reason="",
        usage={"prompt_tokens": 12, "completion_tokens": 4},
        transient=False,
        evidence_contract_status="needs_review",
        confidence=6,
        single_source_ratio=0.93,
    )
    state = outcome.to_state()
    # Serializable plain data (goes through the JSON checkpoint serializer).
    assert json.loads(json.dumps(state)) == state
    assert TaskOutcome.from_state(state) == outcome


def test_from_state_drops_unknown_field_removed_since_checkpoint():
    # An older checkpoint carried a field this version renamed/removed.
    legacy = {"status": "failed", "summary": "s", "legacy_score": 0.9}
    outcome = TaskOutcome.from_state(legacy)
    assert outcome.status == "failed"
    assert outcome.summary == "s"
    assert not hasattr(outcome, "legacy_score")


def test_from_state_fills_field_added_since_checkpoint():
    # An older checkpoint lacks a field this version added — the default fills.
    old = {"status": "completed"}
    outcome = TaskOutcome.from_state(old)
    assert outcome.status == "completed"
    assert outcome.summary == ""
    assert outcome.evidence == []
    assert outcome.usage == {"prompt_tokens": 0, "completion_tokens": 0}
    assert outcome.transient is False
    assert outcome.evidence_contract_status == "unknown"
    assert outcome.confidence == 0
    assert outcome.single_source_ratio == 0.0


def test_from_state_raises_on_missing_required_field():
    # ``status`` has no default — the one drift that cannot be reconciled.
    with pytest.raises(CheckpointSchemaError):
        TaskOutcome.from_state({"summary": "no status here"})


def test_evidence_note_speaks_to_the_reader_not_the_pipeline() -> None:
    """The note lands verbatim under 'Offene Punkte' of the memo and in
    the activity row. Before it read ``Task t1: Evidenzlage needs_review
    (100 % Einzelquellen), Konfidenz 6/10`` — an internal check protocol
    in the deliverable. Now it names the sub-task by its plan title and says
    what is thin; the task's result summary never names it."""
    outcome = TaskOutcome(
        status="completed",
        summary="CANARY-SUMMARY: Förderbedingungen verglichen.",
        evidence_contract_status="needs_review",
        confidence=6,
        single_source_ratio=1.0,
    )
    note = _task_evidence_note("t1", outcome, "de", title="Förderbedingungen für Wärmepumpen 2026 vergleichen")
    assert note == (
        "Teilaufgabe „Förderbedingungen für Wärmepumpen 2026 vergleichen“: "
        "nur einfach belegt (alle Belege aus je einer Quelle), Konfidenz 6/10"
    )
    assert "CANARY" not in note
    for token in ("needs_review", "Task t1", "Evidenzlage", "%"):
        assert token not in note

    partial = _task_evidence_note(
        "t2",
        TaskOutcome(
            status="completed",
            summary="Hersteller",
            evidence_contract_status="source_context_only",
            confidence=4,
            single_source_ratio=0.93,
        ),
        "de",
        title="Hersteller",
    )
    assert partial == (
        "Teilaufgabe „Hersteller“: nur Kontextquellen ohne belastbaren Beleg "
        "(93 % der Belege aus je einer Quelle), Konfidenz 4/10"
    )
    # An unknown contract status is passed through verbatim (never
    # silently relabelled).
    odd = _task_evidence_note(
        "t3",
        TaskOutcome(status="completed", summary="", evidence_contract_status="odd", confidence=1),
        "de",
        title="Recht",
    )
    assert odd == "Teilaufgabe „Recht“: odd, Konfidenz 1/10"


def test_a_note_without_a_plan_title_uses_the_id_and_warns(caplog) -> None:
    """The id is the last handle, and naming a task by it is never silent."""
    outcome = TaskOutcome(status="completed", summary="Kopf", evidence_contract_status="odd", confidence=1)
    with caplog.at_level("WARNING", logger="inqtrix"):
        bare = _task_evidence_note("t3", outcome, "de", title=None)
    assert bare == "Teilaufgabe t3: odd, Konfidenz 1/10"
    assert any("has no plan title" in record.getMessage() for record in caplog.records)


def test_evidence_note_names_partial_coverage_of_a_child() -> None:
    """A child whose evidence budget left sources unevaluated is partial
    coverage: the note says so in reader words, with and without a capped
    contract, and the outcome's ``coverage`` says ``partial``."""
    clean_but_partial = TaskOutcome(
        status="completed",
        summary="Foerderung in Oesterreich",
        evidence_contract_status="clean",
        confidence=7,
        omitted_evidence_count=21,
        omitted_evidence_query_count=3,
    )
    assert clean_but_partial.coverage == "partial"
    assert _task_evidence_note("t1", clean_but_partial, "de", title="Foerderung in Oesterreich") == (
        "Teilaufgabe „Foerderung in Oesterreich“: teilweise ausgewertet "
        "(21 belegfähige Quellen aus 3 Suchen nicht ausgewertet), Konfidenz 7/10"
    )
    capped_and_partial = TaskOutcome(
        status="completed",
        summary="Fristen",
        evidence_contract_status="needs_review",
        confidence=5,
        single_source_ratio=1.0,
        omitted_evidence_count=1,
        omitted_evidence_query_count=1,
    )
    assert _task_evidence_note("t2", capped_and_partial, "de", title="Fristen") == (
        "Teilaufgabe „Fristen“: nur einfach belegt (alle Belege aus je einer Quelle), "
        "teilweise ausgewertet (1 belegfähige Quelle aus einer Suche nicht ausgewertet), "
        "Konfidenz 5/10"
    )
    complete = TaskOutcome(status="completed", summary="x", evidence_contract_status="clean")
    assert complete.coverage == "complete"
    # The coverage fields survive the checkpoint round trip.
    revived = TaskOutcome.from_state(clean_but_partial.to_state())
    assert (revived.omitted_evidence_count, revived.omitted_evidence_query_count) == (21, 3)
    assert revived.coverage == "partial"
