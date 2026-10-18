"""What a subtask that stopped early hands up to its parent.

A failure branch that returned the error text and nothing else — no
stored result read, no evidence merged, no web-search ledger registered —
would throw away the evidence of a child that broke off AFTER producing
it, and worse, leave every one of its sources uncitable, because their
labels would never be registered in the parent's ledger. The
parent's own limit path already keeps partial evidence and says plainly
that the synthesis is incomplete; this is the same contract one level
down.
"""

from inqtrix.agents.kernel.tools import broken_off_child_body

NOTICE = "The subtask (deep_mission) failed: plan_invalid."


def test_partial_evidence_is_handed_up() -> None:
    """The regression: sources the child paid for reach the parent."""
    body = broken_off_child_body(
        NOTICE,
        text="Zwischenstand zum AI Act.",
        source_lines="[W1] https://example.org/a",
    )
    assert "Zwischenstand zum AI Act." in body
    assert "[W1] https://example.org/a" in body


def test_the_break_off_is_stated_before_the_content() -> None:
    """A partial result must never read like a finished one."""
    body = broken_off_child_body(
        NOTICE, text="Zwischenstand.", source_lines="[W1] https://x"
    )
    assert body.startswith(NOTICE)
    assert body.index("PARTIAL RESULT") < body.index("Zwischenstand.")


def test_the_parent_is_told_the_result_is_incomplete() -> None:
    body = broken_off_child_body(
        NOTICE, text="Zwischenstand.", source_lines=""
    )
    assert "incomplete" in body
    assert "name the gap in the answer" in body


def test_nothing_to_salvage_says_so() -> None:
    """No silent empty stretch presented as a result."""
    body = broken_off_child_body(NOTICE, text="", source_lines="")
    assert body.startswith(NOTICE)
    assert "No partial result is available" in body
    assert "PARTIAL RESULT is incomplete" not in body


def test_sources_alone_are_worth_handing_up() -> None:
    """A child may have searched without drafting: the searches are
    still paid work, and their labels must reach the parent's ledger."""
    body = broken_off_child_body(
        NOTICE, text="", source_lines="[W1] https://example.org/a"
    )
    assert "No partial result" not in body
    assert "[W1] https://example.org/a" in body


def test_a_label_rename_note_travels_with_the_partial() -> None:
    """Labels are translated in the tool; the note that says so must not
    be dropped just because the child broke off."""
    body = broken_off_child_body(
        NOTICE,
        text="Zwischenstand.",
        source_lines="[W7] https://x",
        rename_note="Hinweis: [W1] heisst im Elternlauf [W7].",
    )
    assert "heisst im Elternlauf [W7]" in body
