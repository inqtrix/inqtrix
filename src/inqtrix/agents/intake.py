"""Intake and readiness routing.

One fast-tier structured call over the assignment plus a DETERMINISTIC
readiness router: the LLM describes the assignment, code decides where
to go next (ask the user first, plan straight away, or discover).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.source_policy import WEB_TASK_KINDS
from inqtrix.agents.phase_models import AssignmentProfile
from inqtrix.agents.prompts import (
    agent_intake_system_prompt,
    build_agent_intake_prompt,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from inqtrix.agents.clarification import RunSettingsFacts
    from inqtrix.providers.base import LLMProvider


def run_intake(
    llm: "LLMProvider",
    *,
    question: str,
    history: str,
    settings: "RunSettingsFacts",
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    skills_block: str = "",
    artifact_registry: tuple[dict, ...] | list[dict] = (),
    last_response_form: str = "",
    prior_evidence_count: int = 0,
    language: str | None = None,
) -> StructuredOutcome:
    """The intake structured call; value is an AssignmentProfile.

    ``settings`` are the run's fixed settings the prompt states, so the
    intake never proposes a question they already answer.
    """
    return structured_call(
        llm,
        prompt=build_agent_intake_prompt(
            question,
            history,
            settings=settings,
            skills_block=skills_block,
            artifact_registry=artifact_registry,
            last_response_form=last_response_form,
            prior_evidence_count=prior_evidence_count,
            language=language,
        ),
        model_cls=AssignmentProfile,
        node="agent_intake",
        system=agent_intake_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def route_readiness(profile: AssignmentProfile | None) -> str:
    """Deterministic readiness router (no LLM).

    ``ask_user_first`` when the assignment is ambiguous or carries
    clarification questions; ``plan_now`` for a clear, purely-web
    assignment without internal holdings; ``discover_first`` otherwise
    (the default). A failed intake (``None`` profile) discovers first —
    the safest route, and the failure is already marked loudly by the
    structured fallback.
    """
    if profile is None:
        return "discover_first"
    if (
        profile.scope_clarity == "ambiguous"
        or profile.clarification_questions
    ):
        return "ask_user_first"
    if (
        profile.scope_clarity == "clear"
        and profile.needs_web
        and not profile.needs_internal
        and not profile.needs_files
    ):
        return "plan_now"
    return "discover_first"


def assignment_needs_web(
    profile: AssignmentProfile | None, *, user_chose_sources: bool
) -> bool:
    """Whether this assignment may search the public web at all.

    One decision for every reader: the discovery's web probe and the
    planner's web tasks in every round. The user's own choice about the
    sources outranks the intake's reading (the caller knows its forms);
    the planner then follows what the user said. Without an intake reading
    the planner judges the need itself; otherwise the intake's reading
    decides. The source policy and the tier are separate gates the caller
    applies on top: this answers only what the assignment needs.

    The readiness router reads ``needs_web`` on its own: it asks whether
    an assignment is purely a web assignment, not whether it may use the
    web.
    """
    return user_chose_sources or profile is None or profile.needs_web


WEB_NOT_NEEDED_NOTE_KEY = "agent_plan_assumption_web_not_needed"
"""i18n key of the server's plan assumption that the web was left out."""


def without_stale_web_note(
    assumptions: "Sequence[str]", task_kinds: "Iterable[str]"
) -> list[str]:
    """*assumptions* minus the server's web note, in any language, when the
    plan holds a web task: a plan that searches the web cannot say it does
    not. The user's own plan version passes through here, since an edit
    sends the assumptions it was shown back unchanged."""
    if not WEB_TASK_KINDS & set(task_kinds):
        return list(assumptions)
    from inqtrix.i18n import MESSAGES

    notes = set(MESSAGES[WEB_NOT_NEEDED_NOTE_KEY].values())
    return [item for item in assumptions if item not in notes]
