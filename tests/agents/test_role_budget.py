"""What a role may spend, and what every platform limit is for.

A measured deep run pins the defect these tests hold shut: the review role
of a ``tief`` mission was cut at 110% of a 30-call ceiling while its step
budget sat at 19% and its token budget at 26%. The ceiling was a constant,
its work was linear in the 22 sources it re-read, and the run that ordered
a thorough answer got a verification that ran out of room.

The contract here is therefore about DERIVATION, not about that number: a
verifying role's budget follows the size of its subject, between a floor
nobody falls below and the ceiling of the tier that ordered it. Two runs
with the same subject get the same budget; a run with a bigger subject gets
a bigger one; and no run gets a budget nobody decided.
"""

from __future__ import annotations

import typing

import pytest

from inqtrix.agents.kernel.algorithm import configured_kernel_limits
from inqtrix.agents.kernel.work_profiles import role_source_count
from inqtrix.core.results import HandoverOutput, ReleasedSource, WorkHandover
from inqtrix.settings import (
    AGENT_PLATFORM_LIMIT_PURPOSE,
    LIMIT_CAPACITY,
    LIMIT_THOROUGHNESS,
    AgentPlatformSettings,
)


def _handover(*, references: int = 0, released: int = 0, outputs: int = 1) -> WorkHandover:
    return WorkHandover(
        goal="Review every output of this run.",
        outputs=tuple(
            HandoverOutput(kind="chat", text="Eine Antwort.") for _ in range(outputs)
        ),
        evidence_run_id="run_parent" if references else "",
        evidence_references=references,
        released_sources=tuple(
            ReleasedSource(kind="knowledge_document", source_id=f"doc_{index}")
            for index in range(released)
        ),
    )


def _tool_budget(platform, handover, *, tier: str) -> int:
    tool_base, ceiling, _, _ = configured_kernel_limits(
        platform,
        depth="deep" if tier == "tief" else "normal",
        tier=tier,
        handover=handover,
    )
    # A role never extends: its ceiling IS its base.
    assert ceiling == tool_base
    return tool_base


# -- the budget is derived, never copied ------------------------------------ #


def test_a_verifying_role_is_paid_by_its_subject_and_not_by_a_constant():
    """THE regression: 22 sources must not buy exactly as much as one.

    What makes a constant budget a defect is not a shortfall but the
    blindness: the two handovers below assign work that differs by a
    factor of twenty, and a constant resolves both to the same number.
    """
    platform = AgentPlatformSettings()
    small = _tool_budget(platform, _handover(references=0), tier="tief")
    measured = _tool_budget(platform, _handover(references=21), tier="tief")

    assert measured > small
    # It covers what that run demonstrably attempted, with room to finish.
    assert measured >= 33


def test_the_subject_is_every_thing_the_role_has_to_work_through():
    """Outputs, addressed references and released sources, each counted once."""
    assert role_source_count(_handover(outputs=2, references=21, released=3)) == 26
    assert role_source_count(_handover(outputs=1)) == 1


@pytest.mark.parametrize("tier", ["gruendlich", "tief"])
def test_the_same_subject_buys_the_same_budget_however_the_handover_reads(tier):
    """Deterministic: the SIZE of the subject decides, and nothing else.

    Two runs of the same subject that got different budgets would make every
    exhaustion unreproducible, and an unreproducible exhaustion cannot be
    told apart from a model that simply gave up. So the derivation reads the
    three counts and no other part of the handover — not its wording, not
    its criteria, not how long its assignment happens to be.
    """
    platform = AgentPlatformSettings()
    subject = _handover(references=21, released=2)
    same_size = subject.model_copy(update={
        "goal": "Ein anderer, deutlich längerer Auftragstext für dieselbe Arbeit.",
        "assignment": "x" * 5_000,
        "criteria": ("erstens", "zweitens", "drittens"),
        "language": "en",
    })
    assert _tool_budget(platform, subject, tier=tier) == (
        _tool_budget(platform, same_size, tier=tier)
    )
    assert len(
        {_tool_budget(platform, subject, tier=tier) for _ in range(5)}
    ) == 1


# -- not poorer than the normal allowance, not richer than its tier --------- #


def test_a_role_with_few_sources_keeps_todays_allowance():
    """The floor: deriving a budget must not make any role poorer.

    One output and nothing to re-read prices out far below the constant the
    role had before; without the floor this cut would have taken budget away
    from exactly the small reviews that never had a problem.
    """
    platform = AgentPlatformSettings()
    assert _tool_budget(platform, _handover(), tier="tief") == (
        platform.kernel_max_tool_calls
    )


def test_a_role_with_many_sources_stops_at_the_reach_of_its_tier():
    """The ceiling: how far the tier that ordered the role may REACH.

    Not its base allowance. Read as the base, floor and ceiling coincide
    for every tier whose base IS the normal allowance - that is every tier
    but the deep one - and the derivation would be inert exactly where most
    runs live. The extension ceiling is what the operator already declared
    as the most one run of that tier may ever spend.
    """
    platform = AgentPlatformSettings()
    huge = _tool_budget(platform, _handover(references=500), tier="tief")
    assert huge == platform.kernel_max_tool_calls_extension_ceiling_deep


def test_a_gruendlich_run_commissions_a_gruendlich_review():
    """Counter-case to the ceiling: the tier that pays decides the maximum.

    A role inherits the Stufe of its parent, so a large subject in a
    ``gruendlich`` run is capped by the ``gruendlich`` reach and never
    quietly buys the deep one. The default tier owes a verification like
    any other, so its budget has to MOVE with the subject - a constant
    there would leave the measured defect standing on the path most runs
    take.
    """
    platform = AgentPlatformSettings()
    big = _handover(references=500)
    assert _tool_budget(platform, big, tier="gruendlich") == (
        platform.kernel_max_tool_calls_extension_ceiling
    )
    assert _tool_budget(platform, big, tier="tief") == (
        platform.kernel_max_tool_calls_extension_ceiling_deep
    )
    # It moves with the subject, and the small subject still gets the floor.
    assert _tool_budget(platform, _handover(references=21), tier="gruendlich") > (
        platform.kernel_max_tool_calls
    )
    assert _tool_budget(platform, _handover(), tier="gruendlich") == (
        platform.kernel_max_tool_calls
    )


def test_the_floor_wins_where_an_operator_configured_a_smaller_tier():
    """A role that cannot read is worse than a role that reads a little much.

    Both bounds are promises, and a configuration in which the deep
    allowance sits under the normal one makes them contradict. The floor
    wins, because the other order would leave a role below the budget it
    had before any of this was derived.
    """
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=30,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_DEEP=10,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_EXTENSION_CEILING_DEEP=10,
    )
    assert _tool_budget(platform, _handover(references=99), tier="tief") == 30


def test_the_step_budget_stays_priced_from_the_tool_budget():
    """The role's turns still follow its calls — one pricing rule, not two.

    Under the operator's step ceiling the rule is the only thing that sets
    the number; at and above it, capacity takes over. Both halves are
    pinned here so a later change cannot quietly replace the rule with a
    second constant.
    """
    from inqtrix.agents.kernel.algorithm import (
        _ANSWER_TURN_SUPERSTEPS,
        _SUPERSTEPS_PER_TOOL_TURN,
    )

    # A ceiling high enough that the pricing rule alone decides.
    roomy = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_EXTENSION_CEILING_DEEP=10_000,
    )
    tool_base, _, step_base, step_ceiling = configured_kernel_limits(
        roomy, depth="deep", tier="tief", handover=_handover(references=21)
    )
    assert step_base == step_ceiling == (
        _ANSWER_TURN_SUPERSTEPS + _SUPERSTEPS_PER_TOOL_TURN * tool_base
    )


def test_a_run_without_a_handover_keeps_the_operator_ceilings():
    """The conversational budgets are untouched: they answer another question."""
    platform = AgentPlatformSettings()
    assert configured_kernel_limits(platform, depth="normal", tier="gruendlich") == (
        platform.kernel_max_tool_calls,
        platform.kernel_max_tool_calls_extension_ceiling,
        platform.kernel_max_iterations,
        platform.kernel_max_iterations_extension_ceiling,
    )


# -- the rate is an operator setting, not a number inside the arithmetic ---- #


def test_the_rate_and_the_base_are_read_from_the_settings():
    """Both parts of the formula move when the operator moves them.

    The defaults are estimates, so the only defensible place for them is
    a setting an operator can re-measure against. A rate baked into the
    arithmetic would freeze an estimate into the product, and nothing here
    would notice.
    """
    base = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE=0,
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE=1.0,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=1,
    )
    subject = _handover(references=9)  # 1 output + 9 references = 10
    assert _tool_budget(base, subject, tier="tief") == 10

    doubled = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE=0,
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE=2.0,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=1,
    )
    assert _tool_budget(doubled, subject, tier="tief") == 20

    with_base = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE=4,
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE=1.0,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=1,
    )
    assert _tool_budget(with_base, subject, tier="tief") == 14


def test_a_fractional_rate_never_buys_less_than_the_reads_it_prices():
    """Rounding goes up: half a call is a call the role still has to make."""
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE=0,
        INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE=1.5,
        INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS=1,
    )
    # 1 output + 2 references = 3 sources -> 4.5 calls, rounded up.
    assert _tool_budget(platform, _handover(references=2), tier="tief") == 5


# -- every numeric limit is classified -------------------------------------- #


def _numeric_limits() -> set[str]:
    """Every numeric field of the platform settings, derived from the model.

    Optional numerics count: a limit typed ``int | None`` is still a limit,
    and a predicate that only saw bare ints would let the next one in
    unclassified.
    """
    names: set[str] = set()
    for name, field in AgentPlatformSettings.model_fields.items():
        annotation = field.annotation
        if annotation in (int, float) or any(
            argument in (int, float) for argument in typing.get_args(annotation)
        ):
            names.add(name)
    return names


def test_every_numeric_platform_limit_is_classified():
    """The classification is derived from the model, never restated.

    A limit nobody classified is a limit nobody asked the question about:
    does this protect the deployment, or does it decide how thorough a run
    is? The two have opposite answers when a Stufe promises depth, and the
    only moment to answer is when the limit is written.

    SCOPE, stated so it cannot be mistaken for more: this covers the agent
    PLATFORM limits, the ones a Stufe reaches. The research engine's own
    limits (``AgentSettings``: ``max_rounds``, ``min_rounds``,
    ``first_round_queries``, ``confidence_stop``, ``max_total_seconds`` and
    ten more) decide how much work a run does and are thoroughness limits by
    the same definition — but that engine has no Stufen at all, so "follows
    the Stufe" is not a question one can ask of them. Classifying them here
    would produce a label, not a guard. Whether the research engine should
    read the Stufe is a separate question; a new limit in ``AgentSettings``
    therefore passes this test unnoticed.
    """
    classified = set(AGENT_PLATFORM_LIMIT_PURPOSE)
    numeric = _numeric_limits()
    assert classified == numeric, (
        "a numeric platform limit has no purpose; unclassified: "
        f"{sorted(numeric - classified)}, classified but not a limit: "
        f"{sorted(classified - numeric)}"
    )
    assert set(AGENT_PLATFORM_LIMIT_PURPOSE.values()) <= {
        LIMIT_CAPACITY,
        LIMIT_THOROUGHNESS,
    }


def test_no_capacity_limit_has_a_deep_variant():
    """Capacity is tier-blind BY DEFINITION, so a deep twin contradicts it.

    Deriving this from the field names rather than listing the pairs is
    what gives the classification teeth: the day someone adds a deep
    variant of a concurrency limit, they are claiming that a thorough run
    makes the machine bigger, and that claim fails here.
    """
    numeric = _numeric_limits()
    tier_blind = {
        name
        for name, purpose in AGENT_PLATFORM_LIMIT_PURPOSE.items()
        if purpose == LIMIT_CAPACITY
    }
    with_twin = {name for name in tier_blind if f"{name}_deep" in numeric}
    assert not with_twin, (
        "a capacity limit varies by tier and is therefore not capacity: "
        f"{sorted(with_twin)}"
    )


def test_the_role_budget_settings_are_thoroughness_not_capacity():
    """They decide how much a verification reads, not what the box can hold."""
    assert AGENT_PLATFORM_LIMIT_PURPOSE["kernel_role_tool_calls_base"] == (
        LIMIT_THOROUGHNESS
    )
    assert AGENT_PLATFORM_LIMIT_PURPOSE["kernel_role_tool_calls_per_source"] == (
        LIMIT_THOROUGHNESS
    )


def test_a_canvas_this_run_wrote_and_read_back_is_priced_once():
    """The overlap the handover itself reveals.

    ``canvas`` is a released kind, so a canvas the run produced and then
    read again arrives as an output AND as a released source. The role
    reads that one document once, so it costs once. Trimming the released
    list instead would take away the permission the role needs to re-read
    it - the list is the permission, not the price.
    """
    from inqtrix.agents.kernel.work_profiles import role_source_count
    from inqtrix.core.results import HandoverOutput, ReleasedSource, WorkHandover

    memo = HandoverOutput(kind="canvas", artifact_id="art_memo", revision=2)
    both = WorkHandover(
        goal="Review it.",
        outputs=(memo,),
        released_sources=(
            ReleasedSource(kind="canvas", source_id="art_memo", version="2"),
            ReleasedSource(kind="knowledge_document", source_id="kd_1", version="7"),
        ),
        evidence_references=4,
    )
    # 1 output + 4 references + 1 released source that is not the output.
    assert role_source_count(both) == 6


def test_the_operator_step_ceiling_binds_a_role_too():
    """Capacity stays the operator's answer, whatever the subject buys.

    The tool budget follows the subject because thoroughness is what the
    user chose. Super-steps are a different question: the operator capped
    them once, for wall clock against the edge timeout that
    ``kernel_max_iterations_deep`` warns about, and a derived budget that
    walked past that cap would be deriving capacity.

    This also pulls roles back UNDER that cap. Priced from the old constant
    a role took 9 + 8x30 = 249 super-steps - already above the 241 an
    operator allows any conversation to reach. The eight steps are one tool
    turn, and calls are spent in parallel batches.
    """
    from inqtrix.agents.kernel.algorithm import configured_kernel_limits

    platform = AgentPlatformSettings()
    for references in (0, 21, 500):
        _, _, steps, ceiling = configured_kernel_limits(
            platform, depth="deep", tier="tief", handover=_handover(references=references)
        )
        assert steps == ceiling <= platform.kernel_max_iterations_extension_ceiling_deep

    _, _, huge_steps, _ = configured_kernel_limits(
        platform, depth="deep", tier="tief", handover=_handover(references=500)
    )
    assert huge_steps == platform.kernel_max_iterations_extension_ceiling_deep
