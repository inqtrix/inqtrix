"""The client-supplied history block must not shorten anything silently.

A cut is always visible and the full text stays reachable. This block
feeds the model directly, so an invisible cut makes the caller believe an
instruction is in force that the model never saw.
"""

from __future__ import annotations

from inqtrix.services.request_parsing import (
    HISTORY_MESSAGE_CHAR_LIMIT,
    format_history,
)


def _messages(first: str) -> list[dict]:
    return [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "Kurze Antwort."},
        {"role": "user", "content": "Und jetzt?"},
    ]


def test_a_shortened_message_says_that_it_was_shortened():
    requirement = "Antworte in hoechstens 80 Woertern."
    long_message = "Vergleiche A und B ausfuehrlich. " * 20 + requirement
    assert len(long_message) > HISTORY_MESSAGE_CHAR_LIMIT

    block = format_history(_messages(long_message))

    assert "[... message shortened]" in block
    # The requirement really is gone -- that is the cost of the bound, and
    # exactly why it has to be legible instead of invisible.
    assert requirement not in block


def test_a_short_message_carries_no_marker_and_stays_whole():
    block = format_history(_messages("Was ist ein Batteriespeicher?"))
    assert "Was ist ein Batteriespeicher?" in block
    assert "shortened" not in block
