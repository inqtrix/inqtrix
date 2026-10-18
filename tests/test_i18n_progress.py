"""Tests for progress wording that surfaces evidence quality correctly."""

from typing import Any

from inqtrix.i18n import t
from inqtrix.nodes import _provider_retry_progress_context
from inqtrix.state import emit_progress, initial_state


def test_search_progress_distinguishes_references_from_evidence():
    message = t(
        {"language": "de"},
        "search_sources_processed",
        n=5,
        citations=31,
        evidence=7,
    )

    assert "Suchantworten verarbeitet" in message
    assert "31 Referenzen gesammelt" in message
    assert "7 Evidenz-Records" in message
    assert "Quellen verarbeitet" not in message


def test_search_quality_progress_uses_claim_metrics():
    message = t(
        {"language": "de"},
        "search_quality_summary",
        verified_claims=1,
        attributed_claims=2,
        unverified_claims=5,
        cross_checked_claims=1,
    )

    assert "Aussagenlage" in message
    assert "verifiziert 1" in message
    assert "vom Anbieter zugeschrieben 2" in message
    assert "nicht belegt 5" in message
    assert "mehrfach belegt 1" in message
    assert "Quellenqualitaet" not in message


def test_new_quality_progress_messages_are_specific():
    source_message = t(
        {"language": "de"},
        "search_source_quality",
        primary=2,
        mainstream=3,
        stakeholder=1,
        unknown=4,
        low=0,
        quality="0.67",
    )
    evaluate_message = t(
        {"language": "de"},
        "evaluate_quality_summary",
        source_quality="0.67",
        claim_quality="0.42",
        evidence_records=12,
        open_aspects=1,
    )
    section_message = t(
        {"language": "de"},
        "answer_section_start",
        index=2,
        total=4,
        heading="Risiken",
    )

    assert "Quellenmix" in source_message
    assert "2 primary" in source_message
    assert "Bewertungssignal" in evaluate_message
    assert "12 Evidenz-Records" in evaluate_message
    assert "Report-Abschnitt 2/4" in section_message
    assert "Risiken" in section_message


def test_emit_progress_adds_severity_for_live_ui():
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    emit_progress(state, "Analysiere Frage...")
    emit_progress(state, "Warnung: Kontextfenster fuer Modell unbekannt")

    assert events[0][0] == "inqtrix.progress.message"
    assert events[0][1]["severity"] == "info"
    assert events[1][1]["severity"] == "warning"
    assert events[1][1]["snapshot"]["last_message"].startswith("Warnung")


def test_emit_progress_explicit_severity_overrides_heuristic():
    """Caller-supplied severity beats the keyword heuristic.

    Guards against i18n-string drift: a fallback message that does not
    contain ``"failed"``/``"fallback"`` keywords would be heuristically
    classified as ``info`` — explicit ``severity="warning"`` lifts it
    back to its intended severity without touching the i18n string.
    """
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    # Heuristic alone would classify this as "info" — no warning keyword.
    neutral_message = "Bewertung unvollstaendig (CONFIDENCE-Feld fehlt) — nutze Default 5"
    emit_progress(state, neutral_message, severity="warning")

    assert events[0][1]["severity"] == "warning"


def test_emit_progress_unknown_severity_falls_back_to_heuristic():
    """Unknown severity values are ignored, keeping the heuristic safe."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    emit_progress(state, "Warnung: Fallback aktiv", severity="bogus")

    # "Warnung" + "Fallback" keywords trigger the warning heuristic.
    assert events[0][1]["severity"] == "warning"


def test_emit_progress_explicit_info_for_neutral_status():
    """``severity="info"`` is honored even when the message has hints."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    # Heuristic would catch "Fallback" -> warning; explicit info wins.
    emit_progress(state, "Fallback-Modell aktiviert", severity="info")

    assert events[0][1]["severity"] == "info"


def test_provider_retry_context_emits_warning_progress():
    """Provider retry observers must surface immediately in the live UI."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    class RetryProvider:
        def observe_retries(self, callback: Any) -> Any:
            class _Context:
                def __enter__(self_inner: Any) -> Any:
                    callback({
                        "provider": "TestProvider",
                        "model": "test-model",
                        "attempt": 1,
                        "max_attempts": 5,
                        "delay_seconds": 1.25,
                        "error_code": "HTTP 503",
                    })
                    return self_inner

                def __exit__(self_inner: Any, exc_type: Any, exc: Any, tb: Any) -> bool:
                    return False

            return _Context()

    with _provider_retry_progress_context(
        RetryProvider(),
        state,
        operation_label="Antwort-Synthese",
    ):
        pass

    assert events[0][0] == "inqtrix.progress.message"
    assert events[0][1]["severity"] == "warning"
    assert "TestProvider-Retry 1/5" in events[0][1]["message"]
    assert "HTTP 503" in events[0][1]["message"]
