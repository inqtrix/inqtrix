"""One decision about the web: what the assignment needs, read everywhere.

The intake's ``needs_web`` says whether a run searches the public web at
all. The discovery's web probe and the planner's web tasks in every round
read that one predicate, the user's explicit ask overrides it, and a plan
the server narrowed says so in the server's own words. Every model that
writes text a web tool sends out is told that the text leaves the server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents.clarification import answered_subjects
from inqtrix.agents.discovery import build_probe_plan
from inqtrix.agents.intake import assignment_needs_web
from inqtrix.agents.phase_models import NEEDS_WEB_MEANING, AssignmentProfile
from inqtrix.agents.planner import PlanningFailed, run_planner
from inqtrix.agents.prompts import (
    EXTERNAL_SEARCH_RULE,
    PROJECT_DOCUMENT_CURRENCY_RULE,
    SOURCES_ANSWER_DECIDES,
    build_agent_intake_prompt,
    build_agent_kernel_system_prompt,
    build_agent_planner_prompt,
)
from inqtrix.i18n import t
from inqtrix.agents.control_ports import PlanTaskRecord
from tests.agents.test_workspace_agent import (
    DISCOVERY,
    FakeSearch,
    PROFILE,
    ScriptedLLM,
    _answer_first_round,
    _collection_client,
    _question,
    approve_pending,
    make_agent_client,
    replan_delta,
    review_verdict,
    submit_agent,
    wait_status,
)

WEB_NOT_NEEDED = "The assignment does not need the web"
WEB_NOT_ALLOWED = "Web research is NOT allowed"

SYNTHESIS_ONLY_PLAN = {
    "summary_markdown": "Die Unterlagen zusammenfassen.",
    "tasks": [{"id": "s", "title": "Synthese", "tool_kind": "synthesis"}],
    "success_criteria": ["Alle Fristen mit Beleg."],
}

WEB_PLAN = {
    "summary_markdown": "Eine Websuche, dann Synthese.",
    "tasks": [
        {
            "id": "w",
            "title": "Aktuelle Vorgaben",
            "tool_kind": "web_instant",
            "queries": ["Welche Meldefristen nennt die Aufsicht 2026?"],
        },
        {
            "id": "s",
            "title": "Synthese",
            "tool_kind": "synthesis",
            "depends_on": ["w"],
        },
    ],
    "success_criteria": ["Alle Fristen mit Beleg."],
}


def _profile(**changes: Any) -> AssignmentProfile:
    return AssignmentProfile.model_validate({**PROFILE, **changes})


# -- the predicate ---------------------------------------------------------- #


@pytest.mark.parametrize(
    ("profile", "asked", "expected"),
    [
        (None, False, True),
        ({"needs_web": True}, False, True),
        ({"needs_web": False}, False, False),
        ({"needs_web": False}, True, True),
    ],
)
def test_the_assignment_decides_unless_the_user_chose(profile, asked, expected):
    reading = None if profile is None else _profile(**profile)
    assert (
        assignment_needs_web(reading, user_chose_sources=asked) is expected
    )


def test_intake_schema_and_prompt_state_one_binding_meaning():
    schema = AssignmentProfile.model_json_schema()
    assert schema["properties"]["needs_web"]["description"] == NEEDS_WEB_MEANING
    prompt = build_agent_intake_prompt(
        "Fasse die Unterlagen zusammen.", settings=_settings()
    )
    assert prompt.count(NEEDS_WEB_MEANING) == 1


def _settings():
    from inqtrix.agents.clarification import RunSettingsFacts

    return RunSettingsFacts(
        knowledge="pinned",
        collection_count=1,
        collections=(("Handbuch", 2),),
        web=True,
        pinned_form="",
        tier="gruendlich",
        cap=1,
        rounds_used=0,
        autonomy="autonomous",
    )


# -- discovery reads the same predicate ------------------------------------- #


def _web_probes(profile: AssignmentProfile, *, asked: bool) -> list[dict]:
    plan = build_probe_plan(
        profile,
        question="Fasse die Unterlagen zusammen.",
        collection_ids=["kc_1"],
        max_calls=8,
        web_preview_allowed=True,
        user_chose_sources=asked,
    )
    return [probe for probe in plan.probes if probe["kind"].startswith("web.")]


def test_discovery_sends_no_web_probe_the_assignment_does_not_need():
    assert _web_probes(_profile(needs_web=False), asked=False) == []


def test_discovery_probes_the_web_when_the_user_asked_for_it():
    assert len(_web_probes(_profile(needs_web=False), asked=True)) == 1


# -- the planner ------------------------------------------------------------ #


class _PlanLLM:
    """Returns one fixed plan; records every planner prompt."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.prompts: list[str] = []

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(self, prompt: str, **_kwargs: Any):
        from inqtrix.providers.base import StructuredLLMResponse

        self.prompts.append(prompt)
        return StructuredLLMResponse(
            parsed=dict(self.plan),
            content="",
            prompt_tokens=1,
            completion_tokens=1,
        )


def _plan(llm: _PlanLLM, *, profile, web_allowed=True, asked=False, **kwargs):
    return run_planner(
        llm,
        question="Fasse die Unterlagen zusammen.",
        discovery=None,
        criteria=list(profile.success_criteria) if profile else [],
        covered_gap_ids=frozenset(),
        max_tasks=6,
        web_allowed=web_allowed,
        web_needed=assignment_needs_web(profile, user_chose_sources=asked),
        knowledge_allowed=True,
        model=None,
        reasoning_effort=None,
        timeout=5.0,
        **kwargs,
    )


def test_planner_refuses_a_web_task_the_assignment_does_not_need():
    llm = _PlanLLM(WEB_PLAN)
    with pytest.raises(PlanningFailed) as failed:
        _plan(llm, profile=_profile(needs_web=False))
    assert any(WEB_NOT_NEEDED in error for error in failed.value.errors)
    assert not any("not allowed" in error for error in failed.value.errors)
    # Both attempts were told why, in the assignment's words.
    assert len(llm.prompts) == 2
    assert all(WEB_NOT_NEEDED in prompt for prompt in llm.prompts)
    assert all(WEB_NOT_ALLOWED not in prompt for prompt in llm.prompts)
    assert all(EXTERNAL_SEARCH_RULE not in prompt for prompt in llm.prompts)


def test_planner_keeps_web_tasks_the_user_asked_for():
    llm = _PlanLLM(WEB_PLAN)
    plan, _usage = _plan(llm, profile=_profile(needs_web=False), asked=True)
    assert [task.tool_kind for task in plan.tasks] == ["web_instant", "synthesis"]
    assert llm.prompts[0].count(EXTERNAL_SEARCH_RULE) == 1


def test_planner_without_an_intake_reading_keeps_the_web():
    llm = _PlanLLM(WEB_PLAN)
    plan, _usage = _plan(llm, profile=None)
    assert plan.tasks[0].tool_kind == "web_instant"


def test_a_replan_keeps_web_tasks_that_already_ran():
    """A carried-over web task (the user's own, or planned before) cannot be
    removed by a replan; only a NEW one the assignment does not need is a
    repair error."""
    from inqtrix.agents.control_ports import PlanRecord

    previous = PlanRecord(
        plan_id="p1", run_id="r1", version=1, status="approved",
        created_by="user", summary_markdown="", assumptions=[],
    )
    done = PlanTaskRecord(
        task_id="w", plan_id="p1", run_id="r1", ordinal=0,
        title="Aktuelle Vorgaben", tool_kind="web_instant",
        queries=["Welche Meldefristen nennt die Aufsicht 2026?"],
        status="completed",
    )
    synthesis = PlanTaskRecord(
        task_id="s", plan_id="p1", run_id="r1", ordinal=1,
        title="Synthese", tool_kind="synthesis", depends_on=["w"],
        status="pending",
    )
    keep = _PlanLLM(
        {"summary_markdown": "Nichts Neues.", "new_tasks": [],
         "skip_task_ids": [], "assumptions": []}
    )
    plan, _usage = _plan(
        keep, profile=_profile(needs_web=False),
        previous_plan=previous, previous_tasks=[done, synthesis],
    )
    assert "w" in [task.id for task in plan.tasks]
    add = _PlanLLM(
        {"summary_markdown": "Noch eine Suche.", "skip_task_ids": [],
         "assumptions": [],
         "new_tasks": [{"id": "w2", "title": "Weitere Vorgaben",
                        "tool_kind": "web_instant",
                        "queries": ["Welche Fristen nennt die Behörde?"]}]}
    )
    with pytest.raises(PlanningFailed) as failed:
        _plan(add, profile=_profile(needs_web=False),
              previous_plan=previous, previous_tasks=[done, synthesis])
    assert any(
        WEB_NOT_NEEDED in error and "w2" in error and "w," not in error
        for error in failed.value.errors
    )


def test_a_closed_web_refuses_even_carried_web_tasks():
    """Only an open web keeps a carried-over web task silently: with the web
    closed, the repair error names it, in the policy's words."""
    from inqtrix.agents.control_ports import PlanRecord

    previous = PlanRecord(
        plan_id="p1", run_id="r1", version=1, status="approved",
        created_by="user", summary_markdown="", assumptions=[],
    )
    done = PlanTaskRecord(
        task_id="w", plan_id="p1", run_id="r1", ordinal=0,
        title="Aktuelle Vorgaben", tool_kind="web_instant",
        queries=["Welche Meldefristen nennt die Aufsicht 2026?"],
        status="completed",
    )
    synthesis = PlanTaskRecord(
        task_id="s", plan_id="p1", run_id="r1", ordinal=1,
        title="Synthese", tool_kind="synthesis", depends_on=["w"],
        status="pending",
    )
    keep = _PlanLLM(
        {"summary_markdown": "Nichts Neues.", "new_tasks": [],
         "skip_task_ids": [], "assumptions": []}
    )
    with pytest.raises(PlanningFailed) as failed:
        _plan(keep, profile=_profile(needs_web=False), web_allowed=False,
              previous_plan=previous, previous_tasks=[done, synthesis])
    assert any("not allowed" in error and "w." in error for error in failed.value.errors)


@pytest.mark.parametrize("needs_web", [True, False])
def test_a_closed_web_is_still_called_not_allowed(needs_web):
    llm = _PlanLLM(WEB_PLAN)
    with pytest.raises(PlanningFailed) as failed:
        _plan(llm, profile=_profile(needs_web=needs_web), web_allowed=False)
    assert any("not allowed" in error for error in failed.value.errors)
    assert not any(WEB_NOT_NEEDED in error for error in failed.value.errors)
    assert WEB_NOT_ALLOWED in llm.prompts[0]
    assert WEB_NOT_NEEDED not in llm.prompts[0]


# -- the rule for every author of an outgoing query ------------------------- #


def _planner_prompt(**kwargs: Any) -> str:
    return build_agent_planner_prompt(
        "Frage",
        "(no discovery performed)",
        [],
        max_tasks=6,
        **kwargs,
    )


@pytest.mark.parametrize("research_allowed", [True, False])
def test_every_planned_web_rule_says_the_text_leaves_the_server(research_allowed):
    prompt = _planner_prompt(
        web_allowed=True, research_allowed=research_allowed
    )
    assert prompt.count(EXTERNAL_SEARCH_RULE) == 1


def test_the_kernel_is_told_once_that_web_text_leaves_the_server():
    assert build_agent_kernel_system_prompt().count(EXTERNAL_SEARCH_RULE) == 1


@pytest.mark.parametrize("replan_mode", [False, True])
def test_planner_and_kernel_give_one_answer_to_an_outdated_document(replan_mode):
    extra = {"replan_mode": True, "replan_headroom": 2} if replan_mode else {}
    prompt = _planner_prompt(web_allowed=True, research_allowed=True, **extra)
    assert "outdated gaps -> web task" not in prompt
    assert prompt.count(PROJECT_DOCUMENT_CURRENCY_RULE) == 1
    kernel = build_agent_kernel_system_prompt()
    assert kernel.count(PROJECT_DOCUMENT_CURRENCY_RULE) == 1


@pytest.mark.parametrize(
    ("web_allowed", "knowledge_allowed"),
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_a_prompt_without_planned_web_asks_for_no_web_work(web_allowed, knowledge_allowed):
    prompt = _planner_prompt(
        web_allowed=web_allowed,
        web_needed=False,
        knowledge_allowed=knowledge_allowed,
        research_allowed=True,
    )
    assert "web task with recency" not in prompt
    assert "Every web_instant task" not in prompt
    assert "an outdated gap -> an open point for the synthesis" in prompt
    assert EXTERNAL_SEARCH_RULE not in prompt
    assert SOURCES_ANSWER_DECIDES not in prompt
    assert (WEB_NOT_NEEDED in prompt) is web_allowed
    # 'Not needed' claims no documents: the run may have none to read.
    assert "confined to the project's documents" not in prompt


def test_a_sources_answer_decides_only_where_the_web_is_planned():
    answered = _planner_prompt(web_allowed=True, research_allowed=True, sources_answered=True)
    assert answered.count(SOURCES_ANSWER_DECIDES) == 1
    closed = _planner_prompt(web_allowed=False, sources_answered=True)
    assert SOURCES_ANSWER_DECIDES not in closed


# -- the plan node: the reduction is visible in the server's words ---------- #


def _plan_assumptions(monkeypatch, *, profile: dict, **submit: Any) -> list[str]:
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": profile,
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, **submit)
        wait_status(client, run_id, {"waiting_for_approval"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        approve_pending(client, run_id)
    return list(plan["assumptions"])


NOT_NEEDED_DE = t({"language": "de"}, "agent_plan_assumption_web_not_needed")


def test_plan_names_the_web_it_left_out(monkeypatch):
    assumptions = _plan_assumptions(
        monkeypatch, profile={**PROFILE, "needs_web": False}
    )
    assert assumptions.count(NOT_NEEDED_DE) == 1


def test_plan_names_nothing_when_the_web_is_needed(monkeypatch):
    assumptions = _plan_assumptions(monkeypatch, profile=dict(PROFILE))
    assert NOT_NEEDED_DE not in assumptions


def test_plan_never_claims_the_assignment_left_out_a_closed_web(monkeypatch):
    assumptions = _plan_assumptions(
        monkeypatch,
        profile={**PROFILE, "needs_web": False},
        source_policy={"web": "disabled", "knowledge": "available"},
    )
    assert NOT_NEEDED_DE not in assumptions


def test_plan_names_nothing_when_the_user_asked_for_the_web(monkeypatch):
    assumptions = _plan_assumptions(
        monkeypatch,
        profile={**PROFILE, "needs_web": False},
        tool_directives=["web_research"],
    )
    assert NOT_NEEDED_DE not in assumptions


def test_the_web_not_needed_note_exists_in_both_languages():
    german = t({"language": "de"}, "agent_plan_assumption_web_not_needed")
    english = t({"language": "en"}, "agent_plan_assumption_web_not_needed")
    assert "Websuche" in german and "ausdrücklich im Auftrag" in german
    assert "Web search" in english and "explicitly in the assignment" in english


USER_WEB_TASKS = [
    {"id": "u1", "title": "Aktuelle Vorgaben", "tool_kind": "web_instant",
     "queries": ["Welche Meldewege nennt die Aufsicht?"]},
    {"id": "u1", "title": "Aktuelle Vorgaben", "tool_kind": "web_research",
     "queries": ["Welche Meldewege nennt die Aufsicht?"],
     "params": {"profile": "compact"}},
]


@pytest.mark.parametrize("user_task", USER_WEB_TASKS, ids=["instant", "research"])
def test_a_web_task_the_user_adds_opens_the_web_for_replans(monkeypatch, user_task):
    """The user adds a web task of either kind at the gate of a run whose
    intake left the web out. That is a choice for the web: the critic's later
    replan may plan web work, and the server's stale note leaves the plan."""
    critic_calls = {"count": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["count"] += 1
        if critic_calls["count"] == 1:
            return review_verdict(
                prompt, unmet={1: ("information", "Eine Frist fehlt.")}
            )
        return review_verdict(prompt)

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Fehlende Frist",
            "tool_kind": "web_instant",
            "queries": ["Welche Meldefrist nennt die Aufsicht 2026?"],
        },
        summary="Eine weitere Suche.",
    )
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
            "ReplanDeltaModel": delta,
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        first = client.get(f"/v1/runs/{run_id}/plan").json()
        assert NOT_NEEDED_DE in first["assumptions"]
        approval = client.get(f"/v1/runs/{run_id}/approvals").json()["data"][0]
        edited = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
            json={
                "decision": "edit",
                "plan": {
                    "summary_markdown": "Mit einer Websuche.",
                    "assumptions": list(first["assumptions"]),
                    # The approval card sends the criteria it shows: an
                    # edit without them empties the run's criteria.
                    "success_criteria": list(first["success_criteria"]),
                    "tasks": [
                        user_task,
                        {
                            "id": "s",
                            "title": "Synthese",
                            "tool_kind": "synthesis",
                            "depends_on": ["u1"],
                        },
                    ],
                },
            },
        )
        assert edited.status_code == 200, edited.text
        wait_status(client, run_id, {"waiting_for_approval"})
        replanned = client.get(f"/v1/runs/{run_id}/plan").json()
        approve_pending(client, run_id)
        final = wait_status(client, run_id, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert replanned["created_by"] == "agent"
    assert [task["task_id"] for task in replanned["tasks"]] == ["u1", "t2", "s"]
    assert NOT_NEEDED_DE not in replanned["assumptions"]


def test_the_server_note_is_stated_once_or_not_at_all():
    from inqtrix.agents import algorithm as agent_algorithm
    from inqtrix.agents.plan_models import ExecutionPlanModel

    plan = ExecutionPlanModel.model_validate(
        {**SYNTHESIS_ONLY_PLAN, "assumptions": ["A", NOT_NEEDED_DE]}
    )
    stated = agent_algorithm._with_server_assumption
    assert stated(plan, NOT_NEEDED_DE, present=True).assumptions == ["A", NOT_NEEDED_DE]
    assert stated(plan, NOT_NEEDED_DE, present=False).assumptions == ["A"]
    bare = stated(plan, NOT_NEEDED_DE, present=False)
    assert stated(bare, NOT_NEEDED_DE, present=True).assumptions == ["A", NOT_NEEDED_DE]


def test_a_readable_demand_is_the_users_choice_for_every_reader():
    from inqtrix.agents import algorithm as agent_algorithm

    def deps(*, directives=(), skill_kinds=None):
        return SimpleNamespace(
            request=SimpleNamespace(tool_directives=tuple(directives)),
            skills=skill_kinds,
        )

    original = agent_algorithm.allowed_task_kinds
    try:
        agent_algorithm.allowed_task_kinds = lambda skills: skills
        demanded = agent_algorithm._user_demanded_web
        assert not demanded(deps(), edit_consent=False)
        assert demanded(deps(directives=["web_research"]), edit_consent=False)
        assert demanded(deps(), edit_consent=True)
        assert demanded(deps(skill_kinds={"web_instant", "synthesis"}), edit_consent=False)
        assert not demanded(
            deps(skill_kinds={"web_instant", "rag_query", "synthesis"}),
            edit_consent=False,
        )
    finally:
        agent_algorithm.allowed_task_kinds = original


def test_only_an_answered_sources_question_counts_as_answered_sources():
    from inqtrix.agents import algorithm as agent_algorithm

    answered = agent_algorithm._sources_answered
    assert answered({"answered_subjects": ["sources"]})
    assert not answered({"answered_subjects": ["output_form", "assignment"]})
    assert not answered({})


@pytest.mark.parametrize(
    ("answers", "answer", "option_id", "expected"),
    [
        ({"q1": {"option_ids": ["q1_o1"]}}, "", "", {"sources"}),
        ({"q2": {"text": "Nur Europa"}}, "", "", {"assignment"}),
        ({"q1": {"option_ids": ["unknown"]}}, "", "", set()),
        ({}, "Beides bitte", "", {"sources", "assignment"}),
        ({}, "", "legacy_option", {"sources", "assignment"}),
        ({}, "", "", set()),
    ],
)
def test_answered_subjects_follow_what_the_user_answered(
    answers, answer, option_id, expected
):
    questions = [
        {"id": "q1", "subject": "sources",
         "options": [{"id": "q1_o1", "label": "Web und Unterlagen"}]},
        {"id": "q2", "subject": "assignment", "options": []},
    ]
    assert answered_subjects(
        questions=questions, answers=answers, answer=answer, option_id=option_id
    ) == expected


SOURCES_QUESTION = _question(
    "Sollen auch öffentliche Quellen geprüft werden?", subject="sources"
)


def _sources_client(monkeypatch, *, plan: dict, discovery: dict | None = None, search=None):
    overrides: dict[str, Any] = {
        "AssignmentProfile": {
            **PROFILE,
            "needs_web": False,
            "scope_clarity": "ambiguous",
            "clarification_questions": [SOURCES_QUESTION],
        },
        "ExecutionPlanModel": plan,
    }
    if discovery is not None:
        overrides["DiscoveryResult"] = discovery
    llm = ScriptedLLM(overrides=overrides)
    return make_agent_client(monkeypatch, llm=llm, search=search), llm


def _planner_prompts(llm) -> list[str]:
    return [p for name, p in llm.prompts if name == "ExecutionPlanModel"]


def test_a_sources_answer_lets_the_planner_follow_it(monkeypatch):
    """The run asks which sources to use and the user answers: the planner
    may plan the web and is told that the answer decides, not the intake's
    earlier reading."""
    client, llm = _sources_client(monkeypatch, plan=WEB_PLAN)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(client, run_id, {"q1": {"option_ids": ["q1_o1"]}})
        wait_status(client, run_id, {"waiting_for_approval"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        approve_pending(client, run_id)
    assert [task["tool_kind"] for task in plan["tasks"]][0] == "web_instant"
    assert NOT_NEEDED_DE not in plan["assumptions"]
    assert all(
        prompt.count(SOURCES_ANSWER_DECIDES) == 1 for prompt in _planner_prompts(llm)
    )


def test_a_whole_round_answer_about_sources_reaches_the_planner(monkeypatch):
    client, llm = _sources_client(monkeypatch, plan=SYNTHESIS_ONLY_PLAN)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        clarification = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{clarification['clarification_id']}",
            json={"answer": "Ja, auch öffentliche Quellen."},
        )
        assert answered.status_code == 200, answered.text
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
    assert _planner_prompts(llm)[0].count(SOURCES_ANSWER_DECIDES) == 1


def test_a_sources_answer_never_opens_the_discovery_probe(monkeypatch):
    """'Documents only' or not, code cannot read the answer's direction: the
    discovery sends no web probe, so neither the question nor the answer
    leaves the server before the planner has read the answer."""
    search = FakeSearch()
    question = "Fasse die Meldefristen aus unseren Unterlagen zusammen."
    client, llm = _sources_client(monkeypatch, plan=SYNTHESIS_ONLY_PLAN, search=search)
    with client:
        run_id = submit_agent(
            client,
            question=question,
            autonomy="autonomous",
            agent_overrides={"agent_tier": "gruendlich"},
        )
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(client, run_id, {"q1": {"option_ids": ["q1_o2"]}})
        final = wait_status(client, run_id, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert search.queries == []
    assert _planner_prompts(llm)[0].count(SOURCES_ANSWER_DECIDES) == 1


def test_a_sources_answer_outlives_a_later_round(monkeypatch):
    """Round one answers the sources, round two another subject: the
    planner still follows the sources answer."""
    discovery = {
        **DISCOVERY,
        "questions_for_user": [_question("Welcher Zeitraum soll gelten?")],
    }
    client, llm = _sources_client(monkeypatch, plan=WEB_PLAN, discovery=discovery)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "tief"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(client, run_id, {"q1": {"option_ids": ["q1_o1"]}})
        wait_status(client, run_id, {"waiting_for_input"})
        second = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        pending = next(item for item in second if item["status"] == "pending")
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{pending['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o1"]}}},
        )
        assert answered.status_code == 200, answered.text
        wait_status(client, run_id, {"waiting_for_approval"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        approve_pending(client, run_id)
    assert plan["tasks"][0]["tool_kind"] == "web_instant"
    assert _planner_prompts(llm)[0].count(SOURCES_ANSWER_DECIDES) == 1


def test_a_delegated_mission_sends_its_question_to_no_web_probe(monkeypatch):
    """A delegated mission's question is a model's text: its discovery sends
    it to no search provider, even in Auto."""
    search = FakeSearch()
    question = "Prüfe das Hinweisblatt Meldewege gegen die Aufsicht."
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": SYNTHESIS_ONLY_PLAN})
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    store = client.container.run_service.run_store
    real_get = store.get
    monkeypatch.setattr(
        store,
        "get",
        lambda run_id, **kwargs: {
            **real_get(run_id, **kwargs),
            "parent_run_id": "run_parent",
        },
    )
    with client:
        run_id = submit_agent(client, question=question, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
    assert all(question not in query for query in search.queries)


def test_a_users_own_question_still_gets_its_web_probe(monkeypatch):
    search = FakeSearch()
    question = "Welche Meldefristen gelten 2026 für Anbieter?"
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": SYNTHESIS_ONLY_PLAN})
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    with client:
        run_id = submit_agent(client, question=question, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
    assert any(question in query for query in search.queries)


def test_the_run_refuses_a_web_task_its_assignment_does_not_need(monkeypatch):
    """The plan node hands the assignment's need to the planner: a planner
    that insists on the web fails the first plan loudly, in the
    assignment's words."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": WEB_PLAN,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        final = wait_status(client, run_id, {"failed"})
    assert "does not need the web" in str(final.get("error"))
    planner_prompts = [p for name, p in llm.prompts if name == "ExecutionPlanModel"]
    assert len(planner_prompts) == 2
    assert all(WEB_NOT_NEEDED in prompt for prompt in planner_prompts)


def test_a_web_directive_lets_discovery_probe_what_the_intake_left_out(monkeypatch):
    search = FakeSearch()
    question = "Welche Meldefristen gelten 2026 für Anbieter?"
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    with client:
        run_id = submit_agent(
            client,
            question=question,
            autonomy="autonomous",
            tool_directives=["web_research"],
        )
        wait_status(client, run_id, {"completed", "failed"})
    assert any(question in query for query in search.queries)


def test_discovery_sends_no_probe_an_assignment_does_not_need(monkeypatch):
    """Auto, web open, no user demand, intake 'not needed': the run completes
    and the search provider never saw a query."""
    search = FakeSearch()
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        final = wait_status(client, run_id, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert search.queries == []


def _critic_once(gap: str):
    calls = {"count": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return review_verdict(prompt, unmet={1: ("information", gap)})
        return review_verdict(prompt)

    return critic_script


RAG_DELTA = replan_delta(
    {
        "id": "k2",
        "title": "Fristen nachlesen",
        "tool_kind": "rag_query",
        "queries": ["Welche Meldefristen nennen die Unterlagen?"],
    },
    summary="Eine Wissenssuche.",
)
WEB_DELTA = replan_delta(
    {
        "id": "t2",
        "title": "Fehlende Frist",
        "tool_kind": "web_instant",
        "queries": ["Welche Meldefrist nennt die Aufsicht 2026?"],
    },
    summary="Eine Websuche.",
)


UNREAD_COLLECTION = ("kc_handbuch", "Handbuch", 2)
"""A released collection no task of the run searches: it keeps the review's
research round open, so these replans happen without the web."""


def test_a_replan_still_names_the_web_it_leaves_out(monkeypatch):
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
            "ReplanDeltaModel": RAG_DELTA,
            "DeepReviewVerdict": _critic_once("Eine Frist fehlt."),
        }
    )
    client = _collection_client(monkeypatch, llm, UNREAD_COLLECTION)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        final = wait_status(client, run_id, {"completed", "failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
    assert final["status"] == "completed", final
    assert plan["version"] == 2
    assert plan["assumptions"].count(NOT_NEEDED_DE) == 1


def test_a_replan_refuses_a_web_task_nobody_chose(monkeypatch):
    """No user choice, intake 'not needed': the critic's web replan is refused
    in every attempt and discarded visibly; the run keeps its first plan."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
            "ReplanDeltaModel": WEB_DELTA,
            "DeepReviewVerdict": _critic_once("Eine Frist fehlt."),
        }
    )
    client = _collection_client(monkeypatch, llm, UNREAD_COLLECTION)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        final = wait_status(client, run_id, {"completed", "failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        discarded = [
            event["data"]
            for event in client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
            if event["data"].get("kind") == "replan_invalid"
        ]
    assert final["status"] == "completed", final
    assert plan["version"] == 1
    # The reason is machine data (`errors`); the reader sees the label only.
    assert discarded and all(
        any("does not need the web" in error for error in item["errors"])
        for item in discarded
    )
    assert all("detail" not in item for item in discarded)
    assert plan["assumptions"].count(NOT_NEEDED_DE) == 1


def _edit_at_the_gate(
    client, run_id: str, task: dict, assumptions: list[str], criteria: list[str]
) -> None:
    approval = client.get(f"/v1/runs/{run_id}/approvals").json()["data"][0]
    edited = client.post(
        f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
        json={
            "decision": "edit",
            "plan": {
                "summary_markdown": "Mit dem Web.",
                "assumptions": assumptions,
                # The approval card sends the criteria it shows: an edit
                # without them empties the run's criteria.
                "success_criteria": criteria,
                "tasks": [
                    task,
                    {"id": "s", "title": "Synthese", "tool_kind": "synthesis",
                     "depends_on": [task["id"]]},
                ],
            },
        },
    )
    assert edited.status_code == 200, edited.text


@pytest.mark.parametrize(
    "task",
    [
        {"id": "u1", "title": "Aktuelle Vorgaben", "tool_kind": "web_instant",
         "queries": ["Welche Meldewege nennt die Aufsicht?"]},
        {"id": "u1", "title": "Aktuelle Vorgaben", "tool_kind": "web_research",
         "queries": ["Welche Meldewege nennt die Aufsicht?"],
         "params": {"profile": "compact"}},
    ],
    ids=["instant", "research"],
)
def test_the_users_own_web_task_drops_the_stale_note(monkeypatch, task):
    """The edit sends back the assumptions it was shown; a user version that
    searches the web must not say the web is not planned."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        first = client.get(f"/v1/runs/{run_id}/plan").json()
        assert NOT_NEEDED_DE in first["assumptions"]
        _edit_at_the_gate(
            client, run_id, task, list(first["assumptions"]), list(first["success_criteria"])
        )
        final = wait_status(client, run_id, {"completed", "failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
    assert final["status"] == "completed", final
    assert plan["created_by"] == "user"
    assert plan["tasks"][0]["tool_kind"] == task["tool_kind"]
    assert NOT_NEEDED_DE not in plan["assumptions"]


def test_a_delegated_mission_follows_its_own_intake(monkeypatch):
    """A delegated mission sends no probe, but its planner reads its own
    intake: web needed there, web planned."""
    search = FakeSearch()
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": WEB_PLAN})
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    store = client.container.run_service.run_store
    real_get = store.get
    monkeypatch.setattr(
        store,
        "get",
        lambda run_id, **kwargs: {
            **real_get(run_id, **kwargs),
            "parent_run_id": "run_parent",
        },
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        final = wait_status(client, run_id, {"completed", "failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
    assert final["status"] == "completed", final
    assert plan["tasks"][0]["tool_kind"] == "web_instant"
    assert search.queries == [WEB_PLAN["tasks"][0]["queries"][0]]


def test_the_stale_note_leaves_only_a_plan_that_searches_the_web():
    from inqtrix.agents.intake import without_stale_web_note

    english = t({"language": "en"}, "agent_plan_assumption_web_not_needed")
    notes = ["A", NOT_NEEDED_DE, english]
    assert without_stale_web_note(notes, ["rag_query", "synthesis"]) == notes
    assert without_stale_web_note(notes, ["web_instant", "synthesis"]) == ["A"]
    assert without_stale_web_note(notes, ["web_research"]) == ["A"]


def test_a_replan_drops_the_note_a_stored_user_version_still_carries(monkeypatch):
    """A user version saved with the note beside its own web task (stored by a
    server that did not strip it yet): the next replan states the plan as it
    is, without the note."""
    from inqtrix.services import agent_control_service

    monkeypatch.setattr(
        agent_control_service,
        "without_stale_web_note",
        lambda assumptions, _kinds: list(assumptions),
    )
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "needs_web": False},
            "ExecutionPlanModel": SYNTHESIS_ONLY_PLAN,
            "ReplanDeltaModel": RAG_DELTA,
            "DeepReviewVerdict": _critic_once("Eine Frist fehlt."),
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        first = client.get(f"/v1/runs/{run_id}/plan").json()
        _edit_at_the_gate(
            client, run_id, USER_WEB_TASKS[0], list(first["assumptions"]),
            list(first["success_criteria"]),
        )
        stored = client.get(f"/v1/runs/{run_id}/plan").json()
        final = wait_status(client, run_id, {"completed", "failed"})
        replanned = client.get(f"/v1/runs/{run_id}/plan").json()
    assert NOT_NEEDED_DE in stored["assumptions"]
    assert final["status"] == "completed", final
    assert replanned["created_by"] == "agent"
    assert NOT_NEEDED_DE not in replanned["assumptions"]
