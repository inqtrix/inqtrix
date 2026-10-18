"""What the kernel is told about keeping its task list current.

The list only changes when the model calls ``write_todos``. A delegation
is ONE tool call that can hold the run for many minutes, so a list
written before it keeps naming a finished step as the current one: in the
run that motivated this rule, "structuring the assignment" stood as
in-progress for fifty minutes while a whole mission ran beneath it.
"""

from inqtrix.agents.prompts import _KERNEL_TOOL_DISCIPLINE


def test_the_list_is_advanced_before_a_subtask_starts() -> None:
    doctrine = _KERNEL_TOOL_DISCIPLINE
    assert "Before you start a subtask" in doctrine
    assert "in_progress" in doctrine
    assert "completed" in doctrine


def test_the_rule_carries_its_reason() -> None:
    """A rule the model understands is a rule it follows: the prompt says
    WHY, not just what."""
    assert "can hold the run for many minutes" in _KERNEL_TOOL_DISCIPLINE
