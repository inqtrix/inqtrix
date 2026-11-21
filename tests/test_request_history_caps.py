"""The client-supplied history block must not shorten anything silently.

A cut is always visible and the full text stays reachable. This block
feeds the model directly, so an invisible cut makes the caller believe an
instruction is in force that the model never saw. The budget is spent from
the newest message backwards: the answer a follow-up asks about comes whole.
"""

from __future__ import annotations

from inqtrix.services.request_parsing import (
    HISTORY_CHAR_BUDGET,
    format_history,
)



def _turns(block: str) -> list[str]:
    """Logical turn lines: a turn fenced as ``client-history`` reads as its
    inner text, so cut, gap and window checks stay line by line."""
    turns: list[str] = []
    inside: list[str] | None = None
    for line in block.splitlines():
        if line == '<untrusted_content source="client-history">':
            inside = []
        elif line == "</untrusted_content>" and inside is not None:
            turns.append("\n".join(inside))
            inside = None
        elif inside is not None:
            inside.append(line)
        else:
            turns.append(line)
    return turns

def _messages(*earlier: tuple[str, str]) -> list[dict]:
    return [{"role": role, "content": content} for role, content in earlier] + [
        {"role": "user", "content": "Und jetzt?"},
    ]


def test_a_long_earlier_answer_reaches_the_follow_up_whole():
    # A follow-up that sees only the opening of a four-part answer says the
    # last exception cannot be checked.
    answer = "Ausnahme eins betrifft die Stromversorgung. " * 40 + "Ausnahme vier betrifft Wasser."
    assert len(answer) > 500

    block = format_history(_messages(("user", "Welche Ausnahmen gibt es?"), ("assistant", answer)))

    assert "Ausnahme vier betrifft Wasser." in block
    assert "shortened" not in block
    assert "omitted" not in block


def test_a_history_beyond_the_budget_is_cut_visibly_at_its_oldest_end():
    newest = "N" * (HISTORY_CHAR_BUDGET - 100)
    old_answer = "Alte Antwort. " * 20
    block = format_history(_messages(
        ("assistant", "Ganz alte Antwort."),
        ("assistant", old_answer),
        ("assistant", newest),
    ))

    lines = _turns(block)
    assert lines[0] == "[... 1 earlier message omitted]"
    assert lines[1].startswith("Assistant: Alte Antwort.")
    assert lines[1].endswith("[... message shortened]")
    assert lines[2] == f"Assistant: {newest}"
    assert "Ganz alte Antwort." not in block


def test_every_user_turn_keeps_its_opening_however_long_the_answers_grow():
    # A standing instruction from the first turn must survive two long
    # answers; the budget used to spend itself on the answers alone.
    block = format_history(_messages(
        ("user", "Vergleiche A und B. Antworte immer auf Englisch."),
        ("assistant", "E" * 20_000),
        ("user", "Und C? " + "Details. " * 100),
        ("assistant", "Z" * 15_000),
    ))

    lines = _turns(block)
    assert lines[0] == "User: Vergleiche A und B. Antworte immer auf Englisch."
    assert lines[1].startswith("Assistant: EEE") and lines[1].endswith("[... message shortened]")
    assert lines[2].startswith("User: Und C? Details.")
    assert lines[3] == "Assistant: " + "Z" * 15_000
    assert len(block) <= HISTORY_CHAR_BUDGET + 200


def test_a_gap_stands_where_the_messages_were_left_out():
    block = format_history(_messages(
        ("user", "Erste Frage."),
        ("assistant", "Alte Antwort."),
        ("user", "Zweite Frage."),
        ("assistant", "X" * HISTORY_CHAR_BUDGET),
    ))

    lines = _turns(block)
    assert lines[:3] == ["User: Erste Frage.", "[... 1 earlier message omitted]", "User: Zweite Frage."]
    assert lines[3].endswith("[... message shortened]")


def test_messages_beyond_the_window_are_counted_as_omitted():
    earlier = [("user", f"Frage {n}.") for n in range(25)]
    block = format_history(_messages(*earlier), max_messages=20)

    lines = _turns(block)
    assert lines[0] == "[... 5 earlier messages omitted]"
    assert lines[1] == "User: Frage 5."
    assert len(lines) == 21


def test_a_tool_result_is_not_labelled_as_the_user():
    block = format_history([
        {"role": "user", "content": "Rechne nach."},
        {"role": "tool", "content": "42"},
        {"role": "user", "content": "Und?"},
    ])
    assert _turns(block) == ["User: Rechne nach.", "Tool: 42"]
    assert '<untrusted_content source="client-history">\nTool: 42\n</untrusted_content>' in block


def test_a_short_message_carries_no_marker_and_stays_whole():
    block = format_history(_messages(("user", "Was ist ein Batteriespeicher?")))
    assert block == "User: Was ist ein Batteriespeicher?"
