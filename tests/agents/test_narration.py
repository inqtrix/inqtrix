"""Narration lines: grounded prose from run artifacts, in the
run's answer language.

The transcript renders these payloads verbatim, so the builders must be
deterministic, bounded, and never invent content — and the sanitizer's
allowlist must pass every schema field through untouched.
"""

from __future__ import annotations

from inqtrix.agents.narration import first_sentences, task_narration
from inqtrix.runtime_logging import sanitize_event_payload
from inqtrix.harness.contracts.events import NARRATION_EVENT


def test_first_sentences_cuts_at_sentence_boundary() -> None:
    text = "Erster Satz. Zweiter Satz ist deutlich laenger. Dritter Satz."
    assert first_sentences(text, limit=30) == "Erster Satz."
    # Whole text under the limit passes through unchanged.
    assert first_sentences("Kurz.", limit=30) == "Kurz."
    # A single overlong sentence falls back to a word boundary + marker.
    long = "Wort " * 40
    cut = first_sentences(long, limit=50)
    assert cut.endswith("...")
    assert len(cut) <= 54


def test_task_lines() -> None:
    # task_narration carries no prose of its own: title and summary already
    # come from the run, so it needs no language and gets none.
    assert task_narration("Interne Sammlung", "Zwoelf Treffer. Details ...") \
        .startswith("Interne Sammlung: Zwoelf Treffer.")
    assert task_narration("Titel", "") == ""
    # A result's head can begin with a document's own "# Titel" marker —
    # heading markers never reach a transcript line.
    assert task_narration("Memo verfassen", "# EZB-Einlagensatz\n\n## Kurzfazit\n\nSatz eins.") \
        == "Memo verfassen: EZB-Einlagensatz Kurzfazit Satz eins."


def test_sanitizer_allowlist_passes_narration_payload_through() -> None:
    """The strict schema keeps every field the transcript renders and
    drops surprise keys (gotcha #9 discipline)."""
    payload = {
        "narration_id": "n-plan-1",
        "kind": "plan",
        "text": "Mein Plan (6 Aufgaben): Erst intern sichten.",
        "phase": "planning",
        "final": True,
        "surprise_secret": "nope",
    }
    clean = sanitize_event_payload(NARRATION_EVENT, dict(payload))
    assert clean == {
        "narration_id": "n-plan-1",
        "kind": "plan",
        "text": "Mein Plan (6 Aufgaben): Erst intern sichten.",
        "phase": "planning",
        "final": True,
    }


def test_sanitizer_bounds_narration_independently_from_evidence_text() -> None:
    clean = sanitize_event_payload(
        NARRATION_EVENT,
        {
            "narration_id": "n-bounded",
            "kind": "intent",
            "text": "Wort " * 200,
            "phase": "execution",
        },
    )

    assert clean["text"].endswith(" ...")
    assert len(clean["text"]) <= 400
