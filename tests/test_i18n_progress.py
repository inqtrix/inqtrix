"""Tests for progress wording that surfaces evidence quality correctly."""


from inqtrix.providers.attempts import report_retry
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
    assert "7 Belege angelegt" in message
    assert "Quellen verarbeitet" not in message
    assert "Records" not in message


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
    # The step counts the cross-checked basis whatever the status, the
    # footer verified statements only; the words keep the two apart.
    assert "gegengeprüft, unabhängig vom Stand: 1" in message
    assert "mehrfach belegt" not in message
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
    assert "2 primär, 3 etabliert, 1 interessengeleitet, 4 nicht eingeordnet, 0 minderwertig" in source_message
    assert "primary" not in source_message and "stakeholder" not in source_message
    assert "Bewertungssignal" in evaluate_message
    assert "Aussagenqualität 0.42, 12 Belege" in evaluate_message
    assert "offene Teilfragen" in evaluate_message
    assert "Claim" not in evaluate_message and "Records" not in evaluate_message
    assert "Berichtsabschnitt 2/4" in section_message
    assert "Risiken" in section_message


def test_emit_progress_without_severity_is_info_whatever_the_wording():
    """A step that states no severity reaches the run events as ``info``.

    The messages carry warning words of both languages, so a severity
    read from the wording turns this test red.
    """
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )
    messages = [
        "Analysiere Frage...",
        "Warnung: Kontextfenster für Modell unbekannt",
        "Recherche fehlgeschlagen: ALGO-FAIL, Fallback aktiv",
        "Anbieter instabil, Bericht unvollständig, Vorgabe verletzt",
        "Warning: context window unknown; search failed after retry 2/3",
        "Provider unstable, contract violated",
    ]

    for message in messages:
        emit_progress(state, message)

    assert [event_type for event_type, _ in events] == ["inqtrix.progress.message"] * len(messages)
    assert [payload["severity"] for _, payload in events] == ["info"] * len(messages)
    assert events[1][1]["snapshot"]["last_message"].startswith("Warnung")


def test_emit_progress_sends_the_stated_severity():
    """A stated severity reaches the run events as given: a step whose
    text names no problem is a warning because its sender says so."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    emit_progress(state, "Hinweis zum Ablauf der Recherche", severity="warning")

    assert events[0][1]["severity"] == "warning"


def test_emit_progress_unknown_severity_is_info():
    """An unknown severity value counts as none: the step is ``info``,
    even when its text carries warning words."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    emit_progress(state, "Warnung: Fallback aktiv", severity="bogus")

    assert events[0][1]["severity"] == "info"


def test_emit_progress_explicit_info_for_neutral_status():
    """A stated ``info`` stays ``info`` when the text carries a warning word."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    emit_progress(state, "Fallback-Modell aktiviert", severity="info")

    assert events[0][1]["severity"] == "info"


def test_provider_retry_context_emits_warning_progress():
    """Provider retry observers must surface immediately in the live UI."""
    events = []
    state = initial_state(
        "Was ist neu?",
        run_event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )

    with _provider_retry_progress_context(
        object(),
        state,
        kind="model",
        operation_label="Berichtsabschnitt",
    ):
        report_retry("model", {
            "provider": "TestProvider",
            "model": "test-model",
            "attempt": 1,
            "max_attempts": 5,
            "delay_seconds": 1.25,
            "error_code": "HTTP 503",
        })

    assert events[0][0] == "inqtrix.progress.message"
    assert events[0][1]["severity"] == "warning"
    assert "TestProvider: Wiederholung 1/5 bei Berichtsabschnitt" in events[0][1]["message"]
    assert "HTTP 503" in events[0][1]["message"]
