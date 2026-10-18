"""ONE query doctrine across every Agent-Desk site.

Everywhere the agent platform instructs query shape it asks for a
self-contained, naturally phrased evidence question, never for a keyword
chain. These pins are SITE-precise: each site must carry the doctrine
wording itself, so a later edit cannot silently revive keyword
instructions at one surface while the others moved on. The research-desk
pipeline (graph.py, src/inqtrix/prompts.py) is deliberately NOT covered
by this doctrine.
"""

from __future__ import annotations

import inspect

import pytest

from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.agents.phase_models import AssignmentProfile, QuickWebQuery
from inqtrix.agents.plan_models import PlanTaskModel
from inqtrix.agents.prompts import (
    build_agent_kernel_system_prompt,
    build_agent_planner_prompt,
)
from inqtrix.prompt_language import QUESTION_LANGUAGE_DIRECTIVE, directive_for

DOCTRINE = "self-contained, naturally phrased evidence question"
ANTI_KEYWORD = "no keyword chain"


def test_kernel_answer_closure_keeps_requested_next_steps_and_relevant_limits():
    prompt = build_agent_kernel_system_prompt()
    assert "without unsolicited follow-up offers" in prompt
    assert "without reproducing internal run rules" in prompt
    assert "Explicitly requested next steps" in prompt
    assert "necessary clarifying questions" in prompt
    assert "substantively relevant limits or open points are kept" in prompt


def test_kernel_system_prompt_carries_the_doctrine() -> None:
    prompt = build_agent_kernel_system_prompt()
    assert DOCTRINE in prompt
    assert ANTI_KEYWORD in prompt
    assert "SEARCH QUERY" not in prompt
    assert "keywords" not in prompt
    # The recency rule folds the meant time range INTO the evidence
    # question instead of a provider-side recency filter alone.
    assert "put the intended period" in prompt
    assert "explicitly into the evidence question" in prompt


def test_web_instant_tool_description_carries_the_doctrine() -> None:
    tools = build_kernel_tools()
    web_instant = next(tool for tool in tools if tool.name == "web_instant")
    # The docstring wraps lines; compare the flowed text, not the wrapping.
    description = " ".join(web_instant.description.split())
    assert "self-contained, naturally phrased evidence question" in description
    assert "keyword chain" in description
    assert "SEARCH QUERY" not in description
    assert "keywords" not in description


def test_planner_prompt_carries_the_doctrine_in_both_web_branches() -> None:
    for research_allowed in (False, True):
        prompt = build_agent_planner_prompt(
            "Wie entwickelt sich der Markt?",
            "(keine Erkundung)",
            ["Kriterium"],
            max_tasks=6,
            web_allowed=True,
            research_allowed=research_allowed,
            research_profile="deep" if research_allowed else None,
        )
        assert "naturally phrased evidence question" in prompt
        assert "no keyword chain" in prompt
        assert "SEARCH QUERY" not in prompt


def test_structured_schemas_carry_the_doctrine() -> None:
    quick = QuickWebQuery.model_json_schema()
    assert "evidence question" in quick["properties"]["query"]["description"]
    assert "keyword chain" in quick["properties"]["query"]["description"]
    # The intake's content questions are the discovery's first searches,
    # verbatim: the schema itself states the doctrine and keeps form and
    # process requirements out, for the native structured path that never
    # sees the prompt's prose.
    questions = AssignmentProfile.model_json_schema()["properties"][
        "content_questions"
    ]["description"]
    assert "self-contained, naturally phrased questions" in questions
    assert "never keyword chains" in questions
    assert "searched verbatim" in questions
    assert "An atomic assignment has exactly one." in questions
    assert "belong in success_criteria, never here" in questions
    task = PlanTaskModel.model_json_schema()
    assert "questions" in task["properties"]["queries"]["description"]
    assert "keyword chains" in task["properties"]["queries"]["description"]


def test_replan_prompt_states_the_ceiling_and_the_headroom() -> None:
    """A limit stated only by the validator burns the repair round on
    arithmetic the model cannot know: the replan prompt names the ceiling
    and the number of new tasks it still admits."""
    kwargs = dict(max_tasks=8, web_allowed=True, replan_mode=True)
    prompt = build_agent_planner_prompt(
        "Frage?", "(keine)", [], replan_headroom=1, **kwargs
    )
    assert "at most 8 tasks" in prompt
    assert "at most 1 new tasks" in prompt
    zero = build_agent_planner_prompt(
        "Frage?", "(keine)", [], replan_headroom=0, **kwargs
    )
    assert "NO room" in zero
    assert "new tasks. Every id" not in zero
    with pytest.raises(ValueError):
        build_agent_planner_prompt("Frage?", "(keine)", [], **kwargs)
    first = build_agent_planner_prompt(
        "Frage?", "(keine)", [], max_tasks=8, web_allowed=True
    )
    assert "room for" not in first
    assert "at most 8 tasks" in first


def test_planner_prompt_does_not_offer_task_model_selection() -> None:
    prompt = build_agent_planner_prompt(
        "Frage?", "(keine)", [], max_tasks=8, web_allowed=True
    )
    assert "params.model_tier" not in prompt


def test_planner_prompt_forbids_verification_tasks_in_the_first_plan() -> None:
    """Verification is the critic's job after the synthesis: a planned
    verification/cross-check/falsification task would check three times
    over (planned task, critic research round, parent deep verify) and
    it re-searches known sources. Only the first-plan branch states this;
    the replan branch carries no falsification rule at all."""
    first_plan = build_agent_planner_prompt(
        "Wie entwickelt sich der Markt?",
        "(keine Erkundung)",
        ["Kriterium"],
        max_tasks=6,
        web_allowed=True,
        research_allowed=True,
        research_profile="deep",
    )
    assert (
        "NO verification, cross-check or falsification tasks"
        in first_plan
    )
    assert "the critic checks the results after the synthesis" in first_plan
    assert "is_falsification=true" not in first_plan


def test_the_plan_ceilings_have_one_definition() -> None:
    """The validator, the settings default, the control-service default
    and the planner prompt all read the ceilings from ``inqtrix.constants``
    — a literal 8 at any of those sites would bypass the single edit point.
    Mutation: a literal in settings or the service fails this pin once the
    constant moves."""

    from inqtrix.agents import plan_validation
    from inqtrix.constants import MAX_PLAN_TASKS_DEFAULT, MAX_TASK_QUERIES
    from inqtrix.services.agent_control_service import AgentControlService
    from inqtrix.settings import AgentPlatformSettings

    assert plan_validation.MAX_PLAN_TASKS_DEFAULT is MAX_PLAN_TASKS_DEFAULT
    assert AgentPlatformSettings.model_fields["max_plan_tasks"].default == MAX_PLAN_TASKS_DEFAULT
    service_default = inspect.signature(AgentControlService.__init__).parameters["max_plan_tasks"].default
    assert service_default == MAX_PLAN_TASKS_DEFAULT
    prompt = build_agent_planner_prompt(
        "Frage", "(keine Erkundung)", ["K"], max_tasks=6, web_allowed=True,
        research_allowed=True, research_profile="deep",
    )
    assert f"1-{MAX_TASK_QUERIES} concrete guiding questions" in prompt


def test_the_outline_prompt_asks_for_the_finished_memo_s_title():
    """The outline's title IS the finished memo's title, and the prompt
    says so: read as a heading over the outline itself, a resynthesis
    titles the memo 'Memo-Gliederung: ...' instead of naming its subject."""
    from inqtrix.agents.prompts import build_agent_outline_prompt

    prompt = build_agent_outline_prompt("Frage", ["K1"], "- [W1] x")
    assert "the title of the finished memo" in prompt
    assert "as the heading above the document" in prompt
    # The orthography rule reaches the body but not the headings on its own,
    # which leaves ASCII titles like "Naechste EZB-Ratssitzung" behind the
    # correctly spelled text — so the title rule names the orthography too.
    assert "title and section titles in the correct orthography" in prompt


def test_the_section_prompt_demands_the_target_language_s_orthography():
    """Evidence reports carry ASCII German, so without the rule a section
    copies 'fuer'/'zaehlt' straight out of them. The orthography rule
    therefore rides the German answer-language directive, which is the
    FIRST line of the section prompt (the prompt body is English)."""
    from inqtrix.agents.prompts import build_agent_section_prompt

    prompt = build_agent_section_prompt(
        "Frage", "Kurzfazit", "Fokus", "- [W1] x", "", language="de"
    )
    assert prompt.split("\n", 1)[0] == directive_for("de")
    assert "Umlauten (ä, ö, ü, ß)" in prompt
    assert "nie als ae/oe/ue/ss" in prompt
    neutral = build_agent_section_prompt("Frage", "Kurzfazit", "Fokus", "- [W1] x", "")
    assert neutral.split("\n", 1)[0] == QUESTION_LANGUAGE_DIRECTIVE


def test_the_revision_prompt_demands_the_target_language_s_orthography():
    """A review revision writes new prose into a finished text; without the
    rule it could reintroduce ASCII umlauts the writer avoided — the German
    directive with the rule is the FIRST line of the revision prompt."""
    from inqtrix.agents.prompts import build_deep_revision_prompt

    prompt = build_deep_revision_prompt("Frage", "{}", [], language="de")
    assert prompt.split("\n", 1)[0] == directive_for("de")
    assert "Umlauten (ä, ö, ü, ß)" in prompt
    assert "nie als ae/oe/ue/ss" in prompt
    neutral = build_deep_revision_prompt("Frage", "{}", [])
    assert neutral.split("\n", 1)[0] == QUESTION_LANGUAGE_DIRECTIVE


def test_kernel_output_routing_decides_by_form_not_by_length() -> None:
    """Routing on a line count sends explanation questions to the canvas:
    under a "roughly under 15 lines" threshold any long explanation reads
    as a self-contained artifact. Length is not a measure of
    document-worthiness — the form of the result is.
    """
    prompt = build_agent_kernel_system_prompt()
    assert "no matter how long the answer turns out" in prompt
    assert "Length is not a criterion" in prompt
    assert "a long explanation stays an explanation" in prompt
    # The line-count threshold must not creep back in.
    assert "under 15 lines" not in prompt


def test_kernel_output_routing_keeps_the_document_text_out_of_the_chat() -> None:
    """A document whose text is repeated in the chat is written twice,
    paragraph for paragraph. Demanding the short note only for an UPDATE
    of an existing document leaves the first creation unruled and is
    enough to produce that duplicate, so the rule covers writing and
    updating alike, and "short" carries a countable bound instead of an
    adjective.
    """
    prompt = build_agent_kernel_system_prompt()
    assert "Whenever you write or update a canvas document" in prompt
    assert "at most three sentences" in prompt
    assert "You never repeat the document text in the chat" in prompt


def test_kernel_output_routing_keeps_its_document_addressing_rules() -> None:
    """Routing by form is only half of the output contract: an explicit
    wish of the user outranks every criterion, an existing document is
    addressed through the session context's expected_revision rather than
    by artifact_id, and an unclear target is asked about, not guessed."""
    prompt = build_agent_kernel_system_prompt()
    assert "An explicit wish of the user always overrides these criteria" in prompt
    assert "expected_revision from the session context" in prompt
    assert "never by the artifact_id" in prompt
    assert "ask with ask_user" in prompt
