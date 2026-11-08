"""Retry activity reads in the run's answer language.

These lines surface exactly when a provider or model call is being retried
— the moment a reader most needs to understand what is happening — so they
must not be pinned to one language.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.telemetry import model_retry_activity, provider_retry_activity


_NOTICE = {
    "attempt": 1,
    "max_attempts": 3,
    "delay_seconds": 2.0,
    "error_code": "timeout",
    "operation": "web.search.instant",
}


@pytest.mark.parametrize(
    ("language", "purpose", "retry_head"),
    [
        ("de", "Ausführungsplan wird erstellt", "Provider-Versuch 1/3"),
        ("en", "The execution plan is being created", "Provider attempt 1/3"),
    ],
)
def test_the_model_retry_label_follows_the_run_language(
    language: str, purpose: str, retry_head: str
) -> None:
    activity = model_retry_activity(
        dict(_NOTICE), node="agent_plan", language=language
    )
    assert activity["purpose"] == purpose
    assert activity["detail"].startswith(purpose)
    assert retry_head in activity["detail"]


def test_an_unresolved_language_reads_english_not_german() -> None:
    """No silent German default — the neutral fallback is English."""
    activity = model_retry_activity(dict(_NOTICE), node="agent_deep_review", language="")
    assert activity["purpose"] == "The result is being reviewed"
    assert "Provider attempt" in activity["detail"]


def test_the_provider_retry_text_carries_its_numbers_in_both_languages() -> None:
    german = provider_retry_activity(dict(_NOTICE), purpose="Suche", language="de")
    english = provider_retry_activity(dict(_NOTICE), purpose="Search", language="en")
    for activity in (german, english):
        # attempt numbers, error code and delay survive the translation
        assert "1/3" in activity["detail"] and "2/3" in activity["detail"]
        assert "timeout" in activity["detail"]
        assert "2.0 s" in activity["detail"]
    assert "fehlgeschlagen" in german["detail"]
    assert "failed" in english["detail"]


def test_every_model_call_names_its_retry_in_both_languages() -> None:
    """A purpose without its own text would show the raw message key."""
    from inqtrix.agents.telemetry import _MODEL_RETRY_CONTEXT

    for node in _MODEL_RETRY_CONTEXT:
        for language in ("de", "en"):
            purpose = model_retry_activity(dict(_NOTICE), node=node, language=language)["purpose"]
            assert not purpose.startswith("telemetry_"), (node, language)
