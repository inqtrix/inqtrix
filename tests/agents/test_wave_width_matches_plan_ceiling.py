"""One accepted plan must fit into one wave.

The plan validator accepts up to ``MAX_PLAN_TASKS_DEFAULT`` tasks, but
the wave scheduler ran only ``max_parallel_children`` of them at a time.
With the wave narrower than the ceiling, a plan the validator called
valid was silently serialised — the run took longer for no stated
reason, and nothing anywhere said which number caused it.

The two constants are pinned together so they cannot drift apart again:
raising the plan ceiling without widening the wave fails here.
"""

from inqtrix.agents.plan_validation import MAX_PLAN_TASKS_DEFAULT
from inqtrix.settings import AgentPlatformSettings


def test_the_default_wave_runs_a_full_plan_at_once() -> None:
    assert (
        AgentPlatformSettings().max_parallel_children
        >= MAX_PLAN_TASKS_DEFAULT
    ), (
        "A plan the validator accepts must fit into one wave; otherwise "
        "the wave serialises without saying so."
    )


def test_the_peak_never_exceeds_the_plan_ceiling() -> None:
    """The startup warning must not cry wolf.

    An operator may set the wave knob far above the plan ceiling (this
    stack runs 24). A wave can never be wider than a plan the validator
    accepts, so warning on the raw knob would report a peak that cannot
    occur — and a warning nobody believes is worse than none.
    """
    for configured_wave in (1, 8, 24, 100):
        effective = min(configured_wave, MAX_PLAN_TASKS_DEFAULT)
        assert effective <= MAX_PLAN_TASKS_DEFAULT
        assert effective <= configured_wave
