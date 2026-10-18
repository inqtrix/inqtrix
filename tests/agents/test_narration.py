"""Narration lines: grounded prose from run artifacts, in the
run's answer language.

The transcript renders these payloads verbatim, so the builders must be
deterministic, bounded, and never invent content — and the sanitizer's
allowlist must pass every schema field through untouched.
"""

from __future__ import annotations

from inqtrix.agents.narration import (
    NARRATION_EVENT,
    discovery_narration,
    first_sentences,
    plan_narration,
    section_narration,
    synthesis_narration,
    task_narration,
)
import pytest

from inqtrix.agents.phase_models import DiscoveryResult
from inqtrix.runtime_logging import sanitize_event_payload


def _discovery(facts: int, gaps: int) -> DiscoveryResult:
    return DiscoveryResult.model_validate(
        {
            "known_facts": [
                {"fact": f"Fakt {i}", "source": "Interner Marktbericht", "fresh": True}
                for i in range(facts)
            ],
            "gaps": [
                {
                    "gap_id": f"g{i}",
                    "kind": "missing",
                    "description": "Aktuelle Umsetzungslage fehlt.",
                    "recommended_capability": "web_research",
                    "blocking": False,
                }
                for i in range(gaps)
            ],
            "questions_for_user": [],
        }
    )


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


def test_discovery_narration_counts_facts_and_gaps() -> None:
    text = discovery_narration(_discovery(facts=3, gaps=2), language="de")
    assert "3 belegte Fakten" in text
    assert "2 offene Lücken" in text
    assert "Aktuelle Umsetzungslage fehlt." in text
    assert discovery_narration(None, language="de") == ""
    empty = discovery_narration(_discovery(facts=0, gaps=0), language="de")
    assert "weder" in empty


def test_discovery_narration_follows_the_answer_language() -> None:
    """An English run must not read a German transcript line."""
    english = discovery_narration(_discovery(facts=3, gaps=2), language="en")
    assert english.startswith("I identified 3 supported facts and 2 open gaps")
    assert "Fakten" not in english
    assert (
        discovery_narration(_discovery(facts=0, gaps=0), language="en")
        == "The exploration produced neither supported facts nor open gaps."
    )
    # An unresolved language resolves to English, never silently to German.
    unresolved = discovery_narration(_discovery(facts=1, gaps=1), language="")
    assert unresolved.startswith("I identified 1 supported fact and 1 open gap")


def test_discovery_narration_counts_each_noun_on_its_own() -> None:
    """Facts and gaps choose their number independently: each is a one/other
    pair set whole into the sentence frame, where four sentence keys once
    spelled out every combination."""
    cases = {
        (1, 3): (
            "Ich habe 1 belegten Fakt und 3 offene Lücken identifiziert.",
            "I identified 1 supported fact and 3 open gaps.",
        ),
        (2, 1): (
            "Ich habe 2 belegte Fakten und 1 offene Lücke identifiziert.",
            "I identified 2 supported facts and 1 open gap.",
        ),
        (0, 2): (
            "Ich habe 0 belegte Fakten und 2 offene Lücken identifiziert.",
            "I identified 0 supported facts and 2 open gaps.",
        ),
    }
    for (facts, gaps), (german, english) in cases.items():
        assert discovery_narration(_discovery(facts, gaps), language="de").startswith(german)
        assert discovery_narration(_discovery(facts, gaps), language="en").startswith(english)


def test_narration_singular_forms_are_whole_sentences_per_language() -> None:
    """A singular reads as a whole phrase in each language: a counted noun
    takes its own one/other pair, never a parenthetical or a guessed ending."""
    assert discovery_narration(_discovery(facts=1, gaps=1), language="de") \
        .startswith("Ich habe 1 belegten Fakt und 1 offene Lücke")
    assert plan_narration("", 1, language="de") == (
        "Ich schlage einen Plan mit 1 Aufgabe vor."
    )
    assert plan_narration("", 1, language="en") == (
        "I propose a plan with 1 task."
    )
    assert synthesis_narration("Memo", 1, language="en") == (
        "I am now writing the memo 'Memo' with 1 section."
    )


def test_the_answer_language_is_mandatory_at_every_narration_builder() -> None:
    """No default: a caller that forgets the language fails loudly instead
    of emitting one fixed language for every run (No Silent Fallbacks)."""
    with pytest.raises(TypeError):
        discovery_narration(_discovery(facts=1, gaps=1))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        plan_narration("", 2)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        synthesis_narration("Memo", 2)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        section_narration("Kern")  # type: ignore[call-arg]


def test_plan_and_task_and_synthesis_lines() -> None:
    assert plan_narration("Erst intern sichten, dann Web.", 6, language="de") == (
        "Mein Plan (6 Aufgaben): Erst intern sichten, dann Web."
    )
    assert plan_narration("", 4, language="de") == (
        "Ich schlage einen Plan mit 4 Aufgaben vor."
    )
    # task_narration carries no prose of its own: title and summary already
    # come from the run, so it needs no language and gets none.
    assert task_narration("Interne Sammlung", "Zwoelf Treffer. Details ...") \
        .startswith("Interne Sammlung: Zwoelf Treffer.")
    assert task_narration("Titel", "") == ""
    # The synthesis task's head can begin with the memo's own "# Titel"
    # marker — heading markers never reach a transcript line.
    assert task_narration("Memo verfassen", "# EZB-Einlagensatz\n\n## Kurzfazit\n\nSatz eins.") \
        == "Memo verfassen: EZB-Einlagensatz Kurzfazit Satz eins."
    assert synthesis_narration("Markteinschätzung", 4, language="de") == (
        "Ich schreibe jetzt das Memo 'Markteinschätzung' mit 4 Abschnitten."
    )
    assert section_narration("Kernaussagen", language="de") == (
        "Abschnitt 'Kernaussagen' geschrieben."
    )


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
