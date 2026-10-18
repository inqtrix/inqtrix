"""Workspace-agent trajectory catalog.

Every scenario runs the REAL wiring — container, routes, run store,
control store, capability registry — with a scripted LLM whose calls
record the trajectory (tool ORDER, not just the end state) and the
existing ``run_web_graph`` seam standing in for child research runs.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.providers.base import StructuredLLMResponse
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

# -- scripted providers ------------------------------------------------------ #

PROFILE = {
    "language": "de",
    "intent": "Marktanalyse erstellen",
    "scope_clarity": "clear",
    "needs_web": True,
    "needs_internal": True,
    "needs_files": False,
    "recency_sensitive": True,
    "contested_topic": False,
    "content_questions": ["Welche Anbieter sind im Markt aktiv?"],
    "clarification_questions": [],
    "response_form": "canvas",
    "success_criteria": ["Alle Anbieter mit Quelle belegt."],
}

DISCOVERY = {
    "known_facts": [
        {"fact": "Interner Bericht existiert", "source": "Interner Marktbericht", "fresh": True}
    ],
    "gaps": [
        {
            "gap_id": "g1",
            "kind": "outdated",
            "description": "Aktuelle Marktlage fehlt",
            "recommended_capability": "web_research",
            "blocking": False,
        }
    ],
    "questions_for_user": [],
}

PLAN = {
    "summary_markdown": "Eine Instant-Suche, dann Synthese.",
    "tasks": [
        {
            "id": "t1",
            "title": "Aktuelle Marktlage",
            "tool_kind": "web_instant",
            "gap_ids": ["g1"],
            "queries": [
                "Wie hat sich die relevante Marktlage im Jahr 2026 entwickelt?"
            ],
            "params": {"recency": "month"},
        },
        {
            "id": "s",
            "title": "Synthese",
            "tool_kind": "synthesis",
            "depends_on": ["t1"],
        },
    ],
    "success_criteria": ["Alle Anbieter mit Quelle belegt."],
}


def replan_delta(
    *new_tasks: dict[str, Any], summary: str = "Additiver Replan."
) -> dict[str, Any]:
    """Return the delta-only schema used after the initial plan."""
    return {
        "summary_markdown": summary,
        "new_tasks": list(new_tasks),
        "skip_task_ids": [],
        "assumptions": [],
    }

RESEARCH_PLAN = {
    **PLAN,
    "summary_markdown": "Ein expliziter Recherchelauf, dann Synthese.",
    "tasks": [
        {
            **PLAN["tasks"][0],
            "tool_kind": "web_research",
            "queries": ["Welche belastbaren Quellen beschreiben die Marktlage 2026?"],
            "params": {"profile": "compact", "recency": "month"},
        },
        PLAN["tasks"][1],
    ],
}

SUFFICIENCY = {"coverage": "covered", "missing": []}

OUTLINE = {
    "title": "Marktanalyse",
    "sections": [
        {
            "title": "Ergebnis",
            "focus": "Marktlage zusammenfassen",
            "criterion_ids": ["0"],
            "evidence_labels": ["W1"],
        },
        {
            "title": "Offene Punkte",
            "focus": "Luecken benennen",
            "criterion_ids": [],
            "evidence_labels": [],
        },
    ],
}

def review_verdict(prompt: str, **overrides: Any) -> dict[str, Any]:
    """A review verdict that accounts for every criterion the handover lists.

    Criteria named in ``unmet`` (index -> (kind, gap)) are open, the rest met.
    """
    unmet = overrides.pop("unmet", {})
    listed = prompt.split('<untrusted_content source="success-criteria">', 1)
    numbers = (
        [int(item) for item in re.findall(r"^(\d+)\. ", listed[1].split("</untrusted_content>", 1)[0], flags=re.M)]
        if len(listed) == 2
        else []
    )
    return {
        "complete": not unmet,
        "grounded": True,
        "contradictions_named": True,
        "findings": [],
        "criteria": [
            {"index": number, "met": number not in unmet,
             "kind": unmet.get(number, ("", ""))[0], "gap": unmet.get(number, ("", ""))[1]}
            for number in numbers
        ],
        "not_checked": [],
        **overrides,
    }


def recheck_unquoted(prompt: str) -> dict[str, Any]:
    """A re-check that quotes no basis for any applied edit the server lists
    in its frame: each stays applied and unverified."""
    from tests.agents.test_review_pass import unquoted

    return unquoted(*re.findall(r"^- (finding_\d+): target ", prompt, flags=re.M))


def text_measurements_basis(prompt: str) -> str:
    """The basis id of the mission's text measurements, as the review read it."""
    match = re.search(r'"basis_id": "(diagnostic:[^"]+:text_measurements)"', prompt)
    assert match, "the review carries the mission's text measurements"
    return match.group(1)


def review_finding(
    prompt: str, quote: str, *, finding: str = "Absatz genauer fassen.",
    kind: str = "output_requirement", basis_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One finding on the output the handover names: the memo canvas, else chat."""
    canvas = re.search(r"canvas artifact_id=(\S+) ", prompt)
    return {
        "target": "artifact" if canvas else "chat",
        "artifact_id": canvas.group(1) if canvas else "",
        "finding": finding, "kind": kind, "quote": quote, "basis_ids": list(basis_ids),
    }


def _outline_for_prompt(prompt: str) -> dict[str, Any]:
    """Make the test planner select evidence that the prompt actually exposes.

    Only the evidence block counts: the instructions above it name example
    labels (the requirement's precedence text does), which are no evidence.
    """
    outline = json.loads(json.dumps(OUTLINE))
    # Spelled out rather than read from the prompt module: a renamed
    # heading must fail here loudly instead of silently widening the
    # search to the instructions.
    head = "Evidence (label -> gist):"
    assert head in prompt, "outline prompt lost its evidence heading"
    evidence = prompt.split(head, 1)[1]
    match = re.search(r"\[([KW]\d+)\]", evidence)
    outline["sections"][0]["evidence_labels"] = (
        [match.group(1)] if match else []
    )
    return outline


def _section_for_prompt(prompt: str) -> dict[str, str]:
    """Lead with what the outline preselected for THIS section.

    Every section sees the run's whole evidence register, so a label
    found anywhere in the prompt does not identify the section it
    belongs to - a fixture keyed on that would make every section cite the
    same label and produce the same sentence, and an anchored review
    finding would have no unique passage left to replace. The preselection
    is read from the full-text block alone; a section without one writes
    without a citation, exactly as one that leads with nothing does.
    """
    head = "Evidence read in full (the outline preselected it for this section):"
    preselected = prompt.split(head, 1)[1] if head in prompt else ""
    # The block ends where the register or the sibling overview begins;
    # a sibling's label there is not this section's to cite.
    for boundary in ("Evidence register of this run", "The other sections of this memo"):
        preselected = preselected.split(boundary, 1)[0]
    match = re.search(r"\[([KW]\d+)\]", preselected)
    suffix = f" [{match.group(1)}]" if match else ""
    return {"markdown": f"Die Marktlage ist stabil{suffix}."}


class ScriptedLLM:
    """Deterministic structured/text replies keyed by schema name."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.models = ModelSettings(
            reasoning_model="base-model",
            tier_high_model="high-model",
            tier_mid_model="mid-model",
            tier_fast_model="fast-model",
        )
        self.calls: list[str] = []
        self.call_models: list[tuple[str, str | None]] = []
        self.prompts: list[tuple[str, str]] = []
        self.scripts: dict[str, Any] = {
            "AssignmentProfile": dict(PROFILE),
            "DiscoveryResult": dict(DISCOVERY),
            "ExecutionPlanModel": dict(PLAN),
            "SufficiencyJudgement": dict(SUFFICIENCY),
            "ContradictionReport": {"contradictions": []},
            "ReportOutline": _outline_for_prompt,
            "DeepReviewVerdict": review_verdict,
            "WindowExtraction": {"findings": [], "more_relevant": False},
            "DocumentAnalysisResult": {
                "summary": "Kernaussagen aus dem Dokument.",
                "criteria": [],
                "open_points": [],
            },
            "SectionText": _section_for_prompt,
        }
        self.scripts.update(overrides or {})

    def is_available(self) -> bool:
        return True

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self, prompt: str, *, schema_name: str, **kwargs: Any
    ) -> StructuredLLMResponse:
        self.calls.append(f"structured:{schema_name}")
        self.call_models.append((schema_name, kwargs.get("model")))
        self.prompts.append((schema_name, prompt))
        script = self.scripts.get(schema_name)
        parsed = (
            script(prompt) if callable(script) else dict(script or {})
        )
        return StructuredLLMResponse(
            parsed=parsed,
            content=json.dumps(parsed),
            prompt_tokens=7,
            completion_tokens=5,
        )

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append("complete:section")
        return SimpleNamespace(
            text="Die Marktlage ist stabil [W1].",
            usage={"prompt_tokens": 3, "completion_tokens": 4},
        )


class RetryNotifyingLLM(ScriptedLLM):
    """Scripted provider that exposes one transient retry observation."""

    def __init__(self) -> None:
        super().__init__()
        self._retry_callback: Callable[[dict[str, Any]], None] | None = None
        self._retry_emitted = False

    @contextmanager
    def observe_retries(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> Any:
        previous = self._retry_callback
        self._retry_callback = callback
        try:
            yield self
        finally:
            self._retry_callback = previous

    def complete_structured(
        self, prompt: str, *, schema_name: str, **kwargs: Any
    ) -> StructuredLLMResponse:
        if (
            schema_name == "AssignmentProfile"
            and not self._retry_emitted
            and self._retry_callback is not None
        ):
            self._retry_emitted = True
            self._retry_callback({
                "attempt": 1,
                "max_attempts": 3,
                "delay_seconds": 0.25,
                "error_code": "provider_timeout",
                "operation": "complete_structured",
            })
        return super().complete_structured(
            prompt,
            schema_name=schema_name,
            **kwargs,
        )

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        if not self._retry_emitted and self._retry_callback is not None:
            self._retry_emitted = True
            self._retry_callback({
                "attempt": 1,
                "max_attempts": 3,
                "delay_seconds": 0.25,
                "error_code": "provider_timeout",
                "operation": "complete",
            })
        return super().complete(prompt, **kwargs)


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def is_available(self) -> bool:
        return True

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        self.queries.append(query)
        return GroundedSearchResult(
            answer="Web-Vorschau",
            sources=[
                GroundedSource(
                    url="https://example.com/markt",
                    title="Marktbericht",
                    snippet="Der Markt waechst.",
                )
            ],
        )


class RetryNotifyingSearch(FakeSearch):
    """Search stub that exposes one provider retry to its active observer."""

    def __init__(self) -> None:
        super().__init__()
        self._retry_callback: Callable[[dict[str, Any]], None] | None = None
        self._retry_emitted = False

    @contextmanager
    def observe_retries(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> Any:
        previous = self._retry_callback
        self._retry_callback = callback
        try:
            yield self
        finally:
            self._retry_callback = previous

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        if not self._retry_emitted and self._retry_callback is not None:
            self._retry_emitted = True
            self._retry_callback({
                "attempt": 1,
                "max_attempts": 3,
                "delay_seconds": 0.2,
                "error_code": "provider_timeout",
                "operation": "search",
            })
        return super().search(query, **kwargs)


class NoEvidenceSearch:
    """Available provider whose answer contains no usable evidence."""

    def is_available(self) -> bool:
        return True

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        return GroundedSearchResult(answer="", sources=[])


def fake_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
    """Stands in for a child research run's graph execution."""
    sink = kwargs.get("run_event_sink")
    if sink is not None:
        sink(
            "inqtrix.node.started",
            {"node": "search", "snapshot": {"current_node": "search"}},
        )
    return {
        "answer": f"Kindbericht zu: {question}",
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        "result_state": {
            "answer": f"Kindbericht zu: {question}",
            "round": 1,
            "report_references": [
                {
                    "label": "E1",
                    "url": "https://example.com/quelle",
                    "tier": "mainstream",
                }
            ],
            "consolidated_claims": [],
        },
    }


def make_agent_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm: ScriptedLLM | None = None,
    platform: AgentPlatformSettings | None = None,
    strategies: SimpleNamespace | None = None,
    search: Any = None,
    max_tokens_per_run: int = 0,
) -> TestClient:
    monkeypatch.setattr(
        web_research_module, "run_web_graph", fake_child_graph
    )
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = platform or AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True
    )
    settings.quota.max_tokens_per_run = max_tokens_per_run
    scripted = llm or ScriptedLLM()
    container = register_routes(
        router,
        providers=SimpleNamespace(llm=scripted, search=search or FakeSearch()),
        strategies=strategies or SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.llm = scripted  # type: ignore[attr-defined]
    client.container = container  # type: ignore[attr-defined]
    return client


def platform_without_web_preview() -> AgentPlatformSettings:
    """Platform whose discovery sends no web probe, so a test's search
    double sees exactly the queries its plan hands out."""
    return AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW=False,
    )


def wait_status(
    client: TestClient, run_id: str, statuses: set[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never reached {statuses}")


def submit_agent(
    client: TestClient,
    *,
    question: str = "Erstelle eine Marktanalyse.",
    autonomy: str | None = None,
    session_id: str | None = None,
    knowledge_filters: dict[str, Any] | None = None,
    source_policy: dict[str, str] | None = None,
    tool_directives: list[str] | None = None,
    agent_overrides: dict[str, Any] | None = None,
    report_guidance: str | None = None,
    report_rule_ids: list[str] | None = None,
    expect_status: int = 202,
) -> str:
    body: dict[str, Any] = {
        "question": question,
        "mode": "workspace_agent",
    }
    if autonomy:
        body["autonomy"] = autonomy
    if session_id:
        body["session_id"] = session_id
    if knowledge_filters is not None:
        body["knowledge_filters"] = knowledge_filters
    if source_policy is not None:
        body["source_policy"] = source_policy
    if tool_directives is not None:
        body["tool_directives"] = tool_directives
    if agent_overrides is not None:
        body["agent_overrides"] = agent_overrides
    if report_guidance is not None:
        body["report_guidance"] = report_guidance
    if report_rule_ids is not None:
        body["report_rule_ids"] = report_rule_ids
    response = client.post("/v1/runs", json=body)
    assert response.status_code == expect_status, response.text
    if expect_status != 202:
        return response.text
    return response.json()["run_id"]


def approve_pending(client: TestClient, run_id: str) -> dict[str, Any]:
    approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
    pending = [a for a in approvals if a["status"] == "pending"]
    assert pending, approvals
    decided = client.post(
        f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
        json={"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text
    return decided.json()


def event_types(client: TestClient, run_id: str) -> list[str]:
    with client.stream("GET", f"/v1/runs/{run_id}/events") as stream:
        body = stream.read().decode("utf-8")
    return [
        json.loads(line[6:])["type"]
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def run_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    with client.stream("GET", f"/v1/runs/{run_id}/events") as stream:
        body = stream.read().decode("utf-8")
    return [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def human_wait_statuses(client: TestClient, run_id: str) -> list[str]:
    """Waiting statuses that stem from a HUMAN interrupt.

    The slot-free ``waiting_for_children`` park is store-internal (the
    last child's terminal write resumes the run) and is filtered out —
    tests about approval/clarification behaviour must not break merely
    because a plan spawned child runs.
    """
    with client.stream("GET", f"/v1/runs/{run_id}/events") as stream:
        body = stream.read().decode("utf-8")
    return [
        status
        for line in body.splitlines()
        if line.startswith("data: ")
        and (event := json.loads(line[6:]))["type"] == "inqtrix.run.waiting"
        and (status := event["data"]["status"]) != "waiting_for_children"
    ]


# -- scenarios ---------------------------------------------------------------- #


def test_structured_model_retries_use_agent_activity_channel(monkeypatch):
    llm = RetryNotifyingLLM()
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        retries = [
            event["data"]
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.activity"
            and event["data"].get("retry")
        ]

    assert len(retries) == 1
    assert retries[0]["scope"] == "run"
    assert retries[0]["phase"] == "intake"
    assert retries[0]["purpose"] == "Auftrag wird eingeordnet"
    assert retries[0]["retry"] == {
        "failed_attempt": 1,
        "next_attempt": 2,
        "max_attempts": 3,
        "delay_seconds": 0.25,
        "error_code": "provider_timeout",
    }


def test_a_document_window_call_surfaces_its_provider_retry():
    """The sweep's window calls report provider retries under their node,
    so a slow provider is visible on the file-analysis task."""
    from inqtrix.agents.document_analysis import ANALYSIS_NODE
    from inqtrix.agents.patterns._structured import (
        observe_structured_retries,
        structured_call,
    )
    from inqtrix.agents.phase_models import WindowExtraction

    class WindowRetryLLM(RetryNotifyingLLM):
        def complete_structured(self, prompt: str, *, schema_name: str, **kwargs: Any):
            if schema_name == "WindowExtraction" and self._retry_callback is not None:
                self._retry_callback({
                    "attempt": 1, "max_attempts": 3, "delay_seconds": 0.25,
                    "error_code": "provider_timeout", "operation": "complete_structured",
                })
            return super().complete_structured(prompt, schema_name=schema_name, **kwargs)

    observed: list[tuple[str, dict[str, Any]]] = []
    with observe_structured_retries(
        lambda node, notice: observed.append((node, notice))
    ):
        structured_call(
            WindowRetryLLM(), prompt="window", model_cls=WindowExtraction, node=ANALYSIS_NODE,
        )

    assert [(node, notice["operation"]) for node, notice in observed] == [
        ("agent_file_analysis", "complete_structured")
    ]


def test_balanced_full_arc_with_child_and_memo(monkeypatch):
    """Happy path: discovery -> plan approval -> child wave -> memo."""
    import inqtrix.agents.algorithm as agent_algorithm

    evidence_states = []
    original_evidence = agent_algorithm._node_evidence

    def capture_evidence(state):
        result = original_evidence(state)
        evidence_states.append(dict(result["sufficiency"]))
        return result

    monkeypatch.setattr(agent_algorithm, "_node_evidence", capture_evidence)
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": RESEARCH_PLAN})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        client.headers["x-inqtrix-workspace-id"] = "ws_mission_contract"
        run_id = submit_agent(
            client,
            session_id="sess-demo",
            tool_directives=["web_research"],
        )
        summary = wait_status(client, run_id, {"waiting_for_approval"})
        assert summary["kind"] == "agent"
        assert summary["workspace_id"] == "ws_mission_contract"
        # The stored row carries the effective autonomy mode (the
        # default resolves to balanced) so summaries can show it.
        assert summary["agent_overrides"]["autonomy"] == "balanced"

        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["status"] == "proposed"
        assert [task["task_id"] for task in plan["tasks"]] == ["t1", "s"]
        assert plan["tasks"][0]["params"]["profile"] == "compact"
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["subject_id"] == plan["plan_id"]

        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})

        # Exactly one child research run with the task's profile.
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        child = children[0]
        assert child["kind"] == "agent_child"
        assert child["workspace_id"] == "ws_mission_contract"
        assert child["agent_overrides"]["report_profile"] == "compact"
        assert child["status"] == "completed"
        completed_plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert completed_plan["status"] == "approved"
        completed_task = completed_plan["tasks"][0]
        assert completed_task["status"] == "completed"
        assert completed_task["child_run_id"] == child["run_id"]
        assert completed_task["result_summary"]
        assert completed_plan["tasks"][1]["status"] == "completed"

        # The memo artifact is ready and anchored to the session.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memos = [a for a in artifacts if a["kind"] == "memo"]
        assert len(memos) == 1
        assert memos[0]["status"] == "ready"
        assert memos[0]["session_id"] == "sess-demo"
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{memos[0]['artifact_id']}"
        ).json()
        assert "[W1]" in detail["content_markdown"]

        # Result payload carries the memo answer.
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert "Marktanalyse" in result["answer"]

        # Trajectory: intake -> discovery analyst -> plan -> (park) ->
        # outline -> sections -> critic. Successful tasks need no preliminary
        # coverage model; skipping is not a synthetic positive coverage score.
        calls = client.llm.calls
        assert calls[0] == "structured:AssignmentProfile"
        assert calls[1] == "structured:DiscoveryResult"
        assert calls[2] == "structured:ExecutionPlanModel"
        assert "structured:SufficiencyJudgement" not in calls
        assert evidence_states == [{"skipped_reason": "no_unresolved_outcome"}]
        skipped = [event["data"] for event in run_events(client, run_id)
                   if event["type"] == "inqtrix.agent.activity"
                   and event["data"].get("kind") == "sufficiency_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "no_unresolved_outcome"
        assert "coverage" not in skipped[0]
        assert calls.index("structured:ReportOutline") > calls.index("structured:ExecutionPlanModel")
        assert calls[-1] == "structured:DeepReviewVerdict"

        # Event catalog: phases, plan.proposed, approval flow, task
        # lifecycle, artifact stream, terminal.
        types = event_types(client, run_id)
        assert "inqtrix.agent.phase.changed" in types
        assert "inqtrix.agent.plan.proposed" in types
        assert "inqtrix.agent.approval.requested" in types
        assert types.index("inqtrix.agent.approval.requested") < types.index(
            "inqtrix.run.waiting"
        )
        assert "inqtrix.agent.task.started" in types
        assert "inqtrix.agent.task.finished" in types
        assert "inqtrix.agent.child.progress" in types
        assert "inqtrix.agent.artifact.created" in types
        assert types[-1] == "inqtrix.run.completed"


def test_research_child_receives_structured_recency_without_raw_params(
    monkeypatch,
):
    """A mission passes freshness structurally, not as prompt-shaped data."""
    captured: list[tuple[str, dict[str, Any]]] = []
    plan = json.loads(json.dumps(RESEARCH_PLAN))
    research_task = plan["tasks"][0]
    research_task["objective"] = "Belastbare aktuelle Quellen einordnen."
    research_task["expected_output"] = "Eine belegte Kurzbewertung."
    research_task["params"]["recency"] = "year"

    def capture_child(question: str, **kwargs: Any) -> dict[str, Any]:
        captured.append((question, kwargs))
        return fake_child_graph(question, **kwargs)

    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}),
    )
    monkeypatch.setattr(
        web_research_module, "run_web_graph", capture_child
    )

    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            tool_directives=["web_research"],
        )
        wait_status(client, run_id, {"completed"})

    assert len(captured) == 1
    question, kwargs = captured[0]
    assert kwargs["web_recency"] == "year"
    assert "Task: Aktuelle Marktlage" in question
    assert "Objective: Belastbare aktuelle Quellen einordnen." in question
    assert "Welche belastbaren Quellen" in question
    assert "Expected output: Eine belegte Kurzbewertung." in question
    assert "Tool parameters:" not in question


def test_mixed_wave_overlaps_child_and_instant_with_one_shared_limit(
    monkeypatch,
):
    """Independent child/local work starts together instead of serializing."""
    import threading

    child_started = threading.Event()
    local_started = threading.Event()

    mixed_plan = {
        "summary_markdown": "Recherche und Instant-Frage parallel.",
        "tasks": [
            {
                "id": "r",
                "title": "Quellen vertiefen",
                "tool_kind": "web_research",
                "gap_ids": ["g1"],
                "queries": ["Welche Quellen belegen die Marktlage 2026?"],
                "params": {"profile": "compact"},
            },
            {
                "id": "i",
                "title": "Aktuellen Fakt prüfen",
                "tool_kind": "web_instant",
                "gap_ids": ["g1"],
                "queries": ["Wie groß ist der Markt im Jahr 2026?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["r", "i"],
            },
        ],
    }

    class OverlapSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            local_started.set()
            assert child_started.wait(5), "child did not overlap instant work"
            return super().search(query, **kwargs)

    def overlapping_child(question: str, **kwargs: Any) -> dict[str, Any]:
        child_started.set()
        assert local_started.wait(5), "instant work did not overlap child"
        return fake_child_graph(question, **kwargs)

    llm = ScriptedLLM(overrides={"ExecutionPlanModel": mixed_plan})
    # No discovery web probe: only the planned instant task may meet the
    # child, so the overlap is the wave's, not the probe's.
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW=False,
        max_parallel_children=2,
    )
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=OverlapSearch(),
        platform=platform,
    )
    monkeypatch.setattr(
        web_research_module, "run_web_graph", overlapping_child
    )
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            tool_directives=["web_research"],
        )
        wait_status(client, run_id, {"completed"})

    assert child_started.is_set()
    assert local_started.is_set()


def test_mixed_wave_two_children_two_locals_respects_shared_limit(monkeypatch):
    """A larger mixed wave never marks or executes beyond shared capacity."""
    import threading

    lock = threading.Lock()
    active = 0
    max_active = 0
    child_calls = 0
    local_calls = 0

    def tracked_call(callback):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return callback()
        finally:
            with lock:
                active -= 1

    class CappedSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            nonlocal local_calls
            if not query.startswith("Instant-Cap-"):
                return super().search(query, **kwargs)
            local_calls += 1
            return tracked_call(
                lambda: super(CappedSearch, self).search(query, **kwargs)
            )

    def capped_child(question: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal child_calls
        child_calls += 1
        return tracked_call(lambda: fake_child_graph(question, **kwargs))

    tasks = [
        {
            "id": task_id,
            "title": f"Recherche {task_id}",
            "tool_kind": "web_research",
            "gap_ids": ["g1"],
            "queries": [f"Welche Belege liefert Research-Cap-{task_id}?"],
            "params": {"profile": "compact"},
        }
        for task_id in ("r1", "r2")
    ] + [
        {
            "id": task_id,
            "title": f"Instant {task_id}",
            "tool_kind": "web_instant",
            "gap_ids": ["g1"],
            "queries": [f"Instant-Cap-{task_id}"],
        }
        for task_id in ("i1", "i2")
    ]
    tasks.append(
        {
            "id": "s",
            "title": "Synthese",
            "tool_kind": "synthesis",
            "depends_on": ["r1", "r2", "i1", "i2"],
        }
    )
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW=False,
        max_parallel_children=2,
    )
    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(
            overrides={
                "ExecutionPlanModel": {
                    "summary_markdown": "Vier unabhaengige Aufgaben.",
                    "tasks": tasks,
                },
            }
        ),
        search=CappedSearch(),
        platform=platform,
    )
    monkeypatch.setattr(web_research_module, "run_web_graph", capped_child)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            tool_directives=["web_research"],
        )
        wait_status(client, run_id, {"completed"})
        stored = client.get(f"/v1/runs/{run_id}/plan").json()

    assert child_calls == 2
    assert local_calls == 2
    assert max_active == 2
    assert all(task["status"] != "running" for task in stored["tasks"])


def test_synthesis_failure_closes_its_plan_task(monkeypatch):
    """A provider failure cannot strand the synthesis row in running."""

    class FailingSynthesisLLM(ScriptedLLM):
        def complete_structured(
            self, prompt: str, *, schema_name: str, **kwargs: Any
        ) -> StructuredLLMResponse:
            if schema_name == "SectionText":
                raise RuntimeError("synthesis unavailable")
            return super().complete_structured(
                prompt, schema_name=schema_name, **kwargs
            )

    client = make_agent_client(monkeypatch, llm=FailingSynthesisLLM())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()

    synthesis_task = next(
        task for task in plan["tasks"] if task["tool_kind"] == "synthesis"
    )
    assert synthesis_task["status"] == "failed"
    assert synthesis_task["result_summary"] == "Synthese fehlgeschlagen."
    assert "structured:DeepReviewVerdict" not in client.llm.calls


def test_persisted_task_summaries_follow_the_run_language():
    """A summary outlives its run; it must still be in the run's language.

    These strings are written into the plan row and read back later in the
    session rail and the task list. Written in German whatever the user had
    chosen, they would be the one place an English UI keeps showing German
    prose.
    """
    from inqtrix.agents.algorithm import _cancelled_task_outcome

    assert (
        _cancelled_task_outcome(language="de").summary
        == "Aufgabe auf Nutzerwunsch abgebrochen."
    )
    assert (
        _cancelled_task_outcome(language="en").summary
        == "Task cancelled at the user's request."
    )
    # An unresolved language is not a silent German default.
    assert (
        _cancelled_task_outcome(language="").summary
        == "Task cancelled at the user's request."
    )


def test_the_cancel_summaries_a_user_actually_sees_are_translated():
    """The texts a Stop produces, pinned by key rather than by hand.

    The live cancel of an English mission surfaced exactly these two on
    the plan rows; an earlier line-based scan had missed them because
    both literals were split across two source lines.
    """
    from inqtrix.i18n import MESSAGES, t

    for key, de_fragment, en_fragment in (
        ("agent_synthesis_summary_cancelled", "Synthese abgebrochen", "Synthesis cancelled"),
        ("agent_task_summary_cancelled_midway", "Ausführung abgebrochen", "Execution cancelled"),
        ("agent_task_summary_not_restarted", "nicht erneut gestartet", "not restarted"),
        ("agent_task_summary_upstream_failed", "vorgelagerten Aufgaben", "upstream task failed"),
    ):
        assert key in MESSAGES, key
        assert de_fragment in t({"language": "de"}, key)
        assert en_fragment in t({"language": "en"}, key)
        # No silent German default for an unresolved language.
        assert t({"language": ""}, key) == t({"language": "en"}, key)


def test_mission_source_policy_is_enforced_and_inherited_by_child(monkeypatch):
    """A mission and its delegated research child share one source policy."""
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": RESEARCH_PLAN})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(
            client,
            source_policy={"web": "available", "knowledge": "disabled"},
            tool_directives=["web_research"],
        )
        waiting = wait_status(client, run_id, {"waiting_for_approval"})
        assert waiting["agent_overrides"]["source_policy"] == {
            "web": "available",
            "knowledge": "disabled",
        }
        approve_pending(client, run_id)
        completed = wait_status(client, run_id, {"completed"})

        children = client.get(
            f"/v1/runs/{run_id}/children"
        ).json()["data"]
        assert len(children) == 1
        assert children[0]["agent_overrides"]["source_policy"] == {
            "web": "available",
            "knowledge": "disabled",
        }
        execution = completed["snapshot"]["execution"]
        assert execution["source_policy"] == {
            "web": "available",
            "knowledge": "disabled",
        }
        assert execution["tool_use_counts"] == {
            "web": 1,
            "knowledge": 0,
        }


def test_mission_rejects_planned_task_for_disabled_source(monkeypatch):
    """A disabled source fails at plan validation before dispatch."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(
            client,
            source_policy={"web": "disabled", "knowledge": "available"},
        )
        summary = wait_status(client, run_id, {"failed"})
        assert "plan_invalid" in summary["error"]["message"]
        assert client.get(f"/v1/runs/{run_id}/children").json()["data"] == []
        assert client.container.providers.search.queries == []


def test_followup_turn_continues_the_session_memo_lineage(monkeypatch):
    """A second turn in the session READS the prior memo and CONTINUES
    the SAME artifact — it does not start a fresh one nor ignore prior text."""
    outline_prompts: list[str] = []

    def capture_outline(prompt: str) -> dict[str, Any]:
        outline_prompts.append(prompt)
        return dict(OUTLINE)

    llm = ScriptedLLM(overrides={"ReportOutline": capture_outline})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run1 = submit_agent(client, session_id="sess-lineage")
        wait_status(client, run1, {"waiting_for_approval"})
        approve_pending(client, run1)
        wait_status(client, run1, {"completed"})
        memo1 = [
            a
            for a in client.get(f"/v1/runs/{run1}/artifacts").json()["data"]
            if a["kind"] == "memo"
        ][0]
        # Turn 1's outline had no prior memo to continue.
        assert "Existing memo:" not in outline_prompts[-1]

        run2 = submit_agent(client, session_id="sess-lineage")
        wait_status(client, run2, {"waiting_for_approval"})
        approve_pending(client, run2)
        wait_status(client, run2, {"completed"})
        memo2 = [
            a
            for a in client.get(f"/v1/runs/{run2}/artifacts").json()["data"]
            if a["kind"] == "memo"
        ][0]

    # Same memo canvas (one per session), revision ADVANCED — continued,
    # not recreated.
    assert memo2["artifact_id"] == memo1["artifact_id"]
    assert memo2["revision"] > memo1["revision"]
    # Turn 2's outline prompt carried the turn-1 memo (the lineage read
    # reached synthesis) so the agent continues instead of restarting.
    assert "Existing memo:" in outline_prompts[-1]
    assert "Die Marktlage ist stabil" in outline_prompts[-1]


def test_autonomous_mode_skips_plan_interrupt(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        assert "inqtrix.agent.plan.proposed" in event_types(client, run_id)
        # Autonomy skips HUMAN decision waits; the slot-free
        # waiting_for_children park is not a human interrupt and may
        # legitimately appear whenever the plan spawns child runs.
        assert human_wait_statuses(client, run_id) == []
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 1
        assert plan["status"] == "approved"


def test_delegated_balanced_run_inherits_the_plan_approval(monkeypatch):
    """A delegated child's round-0 plan gate is covered by the delegation
    approval: whoever approved dispatching
    the child has seen its objective, so a balanced child completes without
    any human wait while the plan row still records an approved version."""
    client = make_agent_client(monkeypatch)
    store = client.container.run_service.run_store
    real_get = store.get
    observed_get_kwargs: list[dict[str, Any]] = []

    def child_row_get(run_id: str, **kwargs: Any) -> dict[str, Any]:
        # The run row is the algorithm's one truthful childhood source;
        # faking it here exercises the real read point in run().
        observed_get_kwargs.append(kwargs)
        return {**real_get(run_id, **kwargs), "parent_run_id": "run_parent"}

    monkeypatch.setattr(store, "get", child_row_get)
    with client:
        run_id = submit_agent(client, autonomy="balanced")
        wait_status(client, run_id, {"completed"})
        assert human_wait_statuses(client, run_id) == []
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 1
        assert plan["status"] == "approved"
    # The read must PRESENT the owner's visibility: the Postgres store's
    # authorization predicate answers RunNotFound to a visible_to-less
    # read of an owned (child) row — the in-memory twin masks that, so
    # the call shape is pinned here and the live stack proves the rest.
    assert observed_get_kwargs
    assert all("visible_to" in kwargs for kwargs in observed_get_kwargs)


def test_delegated_strict_run_keeps_every_gate(monkeypatch):
    """Permission beats delegation: a strict child still parks on its own
    gates — the delegation approval never widens strict."""
    client = make_agent_client(monkeypatch)
    store = client.container.run_service.run_store
    real_get = store.get

    def child_row_get(run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {**real_get(run_id, **kwargs), "parent_run_id": "run_parent"}

    monkeypatch.setattr(store, "get", child_row_get)
    with client:
        run_id = submit_agent(client, autonomy="strict")
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["status"] == "pending"


def test_strict_mode_approves_discovery_before_probes(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="strict")
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["kind"] == "discovery"
        assert approvals[0]["payload"]["probes"]
        # No discovery-analyst call happened before the approval.
        assert "structured:DiscoveryResult" not in client.llm.calls

        approve_pending(client, run_id)
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        kinds = {a["kind"]: a["status"] for a in approvals}
        assert kinds["discovery"] == "approved"
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})


def test_rejected_plan_completes_with_receipt(monkeypatch):
    """Rejecting the plan is a clean user decision, not a run failure:
    the run COMPLETES with a deterministic receipt as its chat answer and
    ``plan_decision=rejected`` in the result payload (the durable
    ``rejected`` status lives on the approval/plan rows)."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        rejected = client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "reject", "note": "Bitte enger fassen."},
        )
        assert rejected.status_code == 200
        wait_status(client, run_id, {"completed"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["status"] == "rejected"
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert "Plan abgelehnt" in result["answer"]
        assert "Bitte enger fassen." in result["answer"]
        # The receipt is durable as the run-local answer artifact, so a
        # reload rehydrates it like any other chat answer.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        assert any(artifact["kind"] == "answer" for artifact in artifacts)


def test_gruendlich_tier_honors_in_ceiling_task_profile(monkeypatch):
    """Published == enforced at EXECUTION: a validated per-task profile
    inside the tier ceiling must reach the child run, never be silently
    normalized back to the tier default."""
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": dict(RESEARCH_PLAN)})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(
            client, agent_overrides={"agent_tier": "gruendlich"}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        # The planner asked for compact (== gruendlich ceiling); the
        # tier DEFAULT is schnell — the explicit in-ceiling choice wins.
        assert children[0]["agent_overrides"]["report_profile"] == "compact"
        # The child carries the mission's Stufe, not an environment default.
        assert children[0]["agent_overrides"]["agent_tier"] == "gruendlich"
        assert children[0]["agent_overrides"]["depth"] == "normal"


def test_tief_mission_children_inherit_the_stufe(monkeypatch):
    """One Stufe per run tree: a ``tief`` mission's research child carries
    ``agent_tier=tief`` (bridged ``depth=deep``) and the PLANNED profile —
    the tier ceiling admits ``deep``, it does not force it."""
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": dict(RESEARCH_PLAN)})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "tief"})
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        overrides = children[0]["agent_overrides"]
        assert overrides["agent_tier"] == "tief"
        assert overrides["depth"] == "deep"
        assert overrides["report_profile"] == "compact"


def test_persisted_task_model_tier_does_not_reach_research_child(monkeypatch):
    from inqtrix.agents import algorithm as algorithm_module
    from inqtrix.agents.control_ports import PlanTaskRecord

    resolved_payloads = []
    submissions = []
    resolved = SimpleNamespace(mode="research", stack_name="chosen-stack")

    def resolve(payload):
        resolved_payloads.append(payload)
        return resolved

    def submit(**kwargs):
        submissions.append(kwargs)
        return {"run_id": "child"}

    deps = SimpleNamespace(
        resolver=SimpleNamespace(resolve=resolve),
        run_service=SimpleNamespace(submit=submit),
        context=SimpleNamespace(
            workspace_id="workspace", principal=None, run_id="parent",
            stack_name="chosen-stack",
        ),
        source_policy=SimpleNamespace(web="available", knowledge="disabled"),
        tier="tief",
        emit=lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(algorithm_module, "_existing_child_for_attempt", lambda *_a: None)
    task = PlanTaskRecord(
        task_id="t1", plan_id="plan", run_id="parent", ordinal=0,
        title="Research the missing evidence", tool_kind="web_research",
        queries=["Which evidence supports the claim?"],
        params={"profile": "compact", "model_tier": "high", "recency": "year"},
    )
    child_id = algorithm_module._submit_child_run(
        deps, {"session_id": "session"}, task, 1,
        research_policy=SimpleNamespace(allowed=True, profile="compact", max_profile="deep"),
    )

    assert child_id == "child"
    assert resolved_payloads == [{
        "mode": "research", "stack": "chosen-stack",
        "agent_overrides": {"report_profile": "compact", "agent_tier": "tief"},
    }]
    assert submissions[0]["resolved"] is resolved
    assert submissions[0]["web_recency"] == "year"
    assert submissions[0]["workspace_id"] == "workspace"
    assert submissions[0]["source_policy"] == {"web": "available", "knowledge": "disabled"}


def test_tier_never_influences_model_routing(monkeypatch):
    """Orthogonality contract: the Stufe changes budgets, never the
    model resolution — same nodes resolve to the same models."""
    def node_models(client: TestClient) -> dict[str, str | None]:
        return dict(client.llm.call_models)

    baseline = make_agent_client(monkeypatch)
    with baseline:
        run_id = submit_agent(baseline)
        wait_status(baseline, run_id, {"waiting_for_approval"})
        approve_pending(baseline, run_id)
        wait_status(baseline, run_id, {"completed"})
    tiered = make_agent_client(monkeypatch)
    with tiered:
        run_id = submit_agent(
            tiered, agent_overrides={"agent_tier": "tief"}
        )
        wait_status(tiered, run_id, {"waiting_for_approval"})
        approve_pending(tiered, run_id)
        wait_status(tiered, run_id, {"completed"})
    base_models = node_models(baseline)
    tier_models = node_models(tiered)
    shared = set(base_models) & set(tier_models)
    assert shared, "no shared nodes to compare"
    for node in shared:
        assert tier_models[node] == base_models[node], node


def test_report_guidance_on_non_plan_gate_is_a_loud_400(monkeypatch):
    """Guidance a gate cannot honor must never be silently dropped."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="strict")
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["kind"] == "discovery"
        response = client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={
                "decision": "approve",
                "report_guidance": "Bitte als Sprechzettel.",
            },
        )
        assert response.status_code == 400
        assert "report_guidance" in response.text


def test_tief_tier_replaces_an_unverified_web_quote_in_place(monkeypatch):
    """On the tief tier an unverified web-cited quote is a measured defect:
    it joins the findings of a passing review and is replaced where it
    stands — one review, one revision, the rest of the memo untouched."""
    quote = "Die Marktlage bleibt unerwartet dynamisch"

    def child_with_excerpt(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        result["result_state"]["report_references"][0]["excerpt"] = (
            "Der Auszug behandelt die Marktlage, enthaelt dieses Zitat "
            "aber nicht."
        )
        return result

    def section_with_quote(prompt: str) -> dict[str, str]:
        match = re.search(r"\[([KW]\d+)\]", prompt)
        label = f" [{match.group(1)}]" if match else ""
        if "Luecken benennen" in prompt:
            return {"markdown": f"Keine Luecken{label}."}
        return {"markdown": f'Analyse: "{quote}"{label}.'}

    def revision(prompt: str) -> dict[str, Any]:
        # The anchor owns the quotation marks, which a paraphrase drops, and
        # the citation after them, which it keeps.
        assert f'"quote": "\\"{quote}\\" [W1]"' in prompt and "quote_checks" in prompt
        return {"edits": [{"finding_id": "finding_1", "replacement": "die Marktlage gilt als dynamisch [W1]"}]}

    llm = ScriptedLLM(overrides={
        "SectionText": section_with_quote, "DeepRevisionBundle": revision,
    })
    client = make_agent_client(monkeypatch, llm=llm)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", child_with_excerpt
    )
    with client:
        run_id = submit_agent(
            client, agent_overrides={"agent_tier": "tief"}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        assert client.llm.calls.count("structured:DeepReviewVerdict") == 1
        assert client.llm.calls.count("structured:DeepRevisionBundle") == 1
        answer = client.get(f"/v1/runs/{run_id}/result").json()["answer"]
        assert quote not in answer
        assert "Analyse: die Marktlage gilt als dynamisch [W1]." in answer
        assert "Keine Luecken" in answer
        kinds = [
            event["data"].get("kind") for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.activity"
        ]
        assert {"critic_web_quote_escalation", "critic_revise", "deep_review_findings"} <= set(kinds)


def test_gruendlich_tier_keeps_unverified_quotes_advisory(monkeypatch):
    """Default tier: the same unverified web quote stays advisory —
    exactly one critic call, no forced revision."""
    quote = "Die Compliance-Kosten steigen 2026 um vierzig Prozent"

    def child_with_excerpt(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        result["result_state"]["report_references"][0]["excerpt"] = (
            "Der Auszug behandelt die Marktlage, enthaelt dieses Zitat "
            "aber nicht."
        )
        return result

    def section_with_quote(prompt: str) -> dict[str, str]:
        match = re.search(r"\[([KW]\d+)\]", prompt)
        label = f" [{match.group(1)}]" if match else ""
        return {"markdown": f'Analyse: "{quote}"{label}.'}

    llm = ScriptedLLM(overrides={"SectionText": section_with_quote})
    client = make_agent_client(monkeypatch, llm=llm)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", child_with_excerpt
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        assert client.llm.calls.count("structured:DeepReviewVerdict") == 1


def test_report_guidance_reaches_the_synthesis_prompts(monkeypatch):
    """Guidance attached to the plan approval must shape the report —
    it rides the decision payload and renders into the synthesis
    prompts (outline/sections/answer)."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        pending = [
            a
            for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ]
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={
                "decision": "approve",
                "report_guidance": "Gliedere als Sprechzettel fuer die GF.",
            },
        )
        assert decided.status_code == 200
        wait_status(client, run_id, {"completed"})
        synthesis_prompts = [
            prompt
            for name, prompt in client.llm.prompts
            if name in ("ReportOutline", "SectionText")
        ]
        assert synthesis_prompts, "synthesis never ran"
        assert any(
            "Output requirement" in prompt
            and "[Free requirement]" in prompt
            and "Sprechzettel" in prompt
            for prompt in synthesis_prompts
        )


def test_report_guidance_rejects_oversized_input(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        pending = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        response = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve", "report_guidance": "x" * 2001},
        )
        assert response.status_code == 400


def test_schnell_tier_answers_in_chat_without_gates(monkeypatch):
    """Stufe Schnell (tier table): intake -> plan -> execute -> chat
    answer. No discovery analyst, no clarification interrupt, no plan
    gate under balanced autonomy, an instant search instead of research
    children, and the critic pass skipped."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                # Even an ambiguous assignment must not park a schnell
                # run — open questions become plan assumptions.
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    {
                        "prompt": "Welcher Markt?",
                        "options": [
                            {"label": "Europa", "description": ""},
                            {"label": "USA", "description": ""},
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(
            client, agent_overrides={"agent_tier": "schnell"}
        )
        summary = wait_status(client, run_id, {"completed"})
        assert summary["agent_overrides"]["agent_tier"] == "schnell"
        assert summary["agent_overrides"]["depth"] == "normal"
        # Neither the discovery analyst nor the critic ever ran.
        assert "structured:DiscoveryResult" not in client.llm.calls
        assert "structured:DeepReviewVerdict" not in client.llm.calls
        # The deliverable is a CHAT answer, not a canvas memo.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        kinds = {artifact["kind"] for artifact in artifacts}
        assert "answer" in kinds
        assert "memo" not in kinds
        # No round ever parked the run.
        clarifications = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        assert clarifications == []
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert [a for a in approvals if a["status"] == "pending"] == []


def test_schnell_tier_still_gates_under_strict_autonomy(monkeypatch):
    """Permission beats speed: strict autonomy keeps its gates even on
    the fastest tier."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(
            client,
            autonomy="strict",
            agent_overrides={"agent_tier": "schnell"},
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})


def test_answered_round_is_never_reasked_after_discovery(monkeypatch):
    """The discovery analyst sees the answered intake round in its prompt,
    and a rephrased duplicate in ``questions_for_user`` is
    deterministically skipped — the run reaches the plan gate without a
    second input interrupt, with a visible skip narration."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    {
                        "prompt": (
                            "Auf welchen KI-Markt soll sich die Analyse "
                            "primaer beziehen?"
                        ),
                        "options": [
                            {"label": "Generative KI", "description": ""},
                            {"label": "Gesamtmarkt", "description": ""},
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            },
            "DiscoveryResult": {
                **DISCOVERY,
                "gaps": [
                    {
                        "gap_id": "g1",
                        "kind": "unknown_scope",
                        "description": "Themenabgrenzung unklar",
                        "recommended_capability": "web_research",
                        "blocking": True,
                    }
                ],
                "questions_for_user": [
                    {
                        "prompt": (
                            "Welchen KI-Markt sollen wir thematisch "
                            "analysieren?"
                        ),
                        "options": [
                            {"label": "Generative KI", "description": ""},
                            {"label": "KI-Software", "description": ""},
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        clarifications = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{clarifications[0]['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o1"], "text": ""}}},
        )
        assert answered.status_code == 200
        # The rephrased question is deduplicated: the plan gate is the
        # NEXT interrupt, never a second input round.
        wait_status(client, run_id, {"waiting_for_approval"})
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()[
            "data"
        ]
        assert len(rounds) == 1
        # The analyst prompt carried the answered round verbatim.
        analyst_prompt = next(
            prompt
            for name, prompt in llm.prompts
            if name == "DiscoveryResult"
        )
        assert "Already clarified" in analyst_prompt
        assert "Generative KI" in analyst_prompt
        # The planner saw the answers too (same root: the plan must
        # honor what the user pinned down).
        planner_prompt = next(
            prompt
            for name, prompt in llm.prompts
            if name == "ExecutionPlanModel"
        )
        assert "Already clarified" in planner_prompt
        # The skip is visible, not silent.
        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        narration_texts = [
            str((event.get("data") or {}).get("text", ""))
            for event in events
            if "narration" in str(event.get("type", ""))
        ]
        assert any("übersprungen" in text for text in narration_texts)


def test_clarified_answers_reach_the_discovery_probes(monkeypatch):
    """Graph-level pin for ``clarified_context``: the answered
    round must survive the LangGraph state schema into the probe
    queries — undeclared state keys are dropped silently between nodes,
    which is exactly how this feature was once a no-op."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "content_questions": [],
                "clarification_questions": [
                    {
                        "prompt": "Auf welchen Markt beziehen?",
                        "options": [
                            {"label": "Generative KI", "description": ""},
                            {"label": "Gesamtmarkt", "description": ""},
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        clarifications = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{clarifications[0]['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o1"], "text": ""}}},
        )
        assert answered.status_code == 200
        wait_status(client, run_id, {"waiting_for_approval"})
        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        probe_queries = [
            str((event.get("data") or {}).get("query", ""))
            for event in events
            if event.get("type") == "inqtrix.agent.activity"
            and (event.get("data") or {}).get("phase") == "discovery"
        ]
        assert any(
            "Generative KI" in query for query in probe_queries
        ), probe_queries


def test_rejected_discovery_completes_with_receipt(monkeypatch):
    """The discovery gate (strict mode) follows the same rejection
    contract as the plan gate: clean completion with a receipt, no
    failure."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, autonomy="strict")
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["kind"] == "discovery"
        rejected = client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "reject"},
        )
        assert rejected.status_code == 200
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert "Erkundung abgelehnt" in result["answer"]


def test_ambiguous_assignment_asks_before_any_probe(monkeypatch):
    """Scenario 4: ask_user_first — no probe before the clarification."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    {
                        "prompt": "Welcher Markt?",
                        "options": [
                            {"label": "Europa", "description": ""},
                            {
                                "label": "USA",
                                "description": "Nordamerikanischer Markt",
                            },
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        # Only the intake call ran — no discovery, no plan.
        assert client.llm.calls == ["structured:AssignmentProfile"]
        clarifications = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        assert clarifications[0]["status"] == "pending"
        # The structured round carries sanitized questions with
        # deterministic ids; the single question mirrors into the
        # legacy options column.
        questions = clarifications[0]["questions"]
        assert [q["id"] for q in questions] == ["q1"]
        assert [o["id"] for o in questions[0]["options"]] == [
            "q1_o1",
            "q1_o2",
        ]
        assert clarifications[0]["options"] == questions[0]["options"]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{clarifications[0]['clarification_id']}",
            json={"answer": "Der europaeische Markt."},
        )
        assert answered.status_code == 200
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})


def test_collection_shared_during_clarification_cannot_expand_run_scope(
    monkeypatch,
):
    """A resumed run keeps the empty collection set admitted at submission."""

    class MutableKnowledge:
        def __init__(self) -> None:
            self.collections: list[SimpleNamespace] = []
            self.list_calls = 0

        async def list_collections(self, **_kwargs: Any) -> list[SimpleNamespace]:
            self.list_calls += 1
            return list(self.collections)

        async def assert_collections_visible(
            self, _collection_ids: list[str], **_kwargs: Any
        ) -> None:
            return None

    late_collection_plan = {
        "summary_markdown": "Eine neue Sammlung auswerten.",
        "tasks": [
            {
                "id": "late",
                "title": "Spaet geteilte Sammlung auswerten",
                "tool_kind": "rag_query",
                "gap_ids": ["g1"],
                "objective": "Interne Befunde pruefen.",
                "queries": ["Welche internen Befunde liegen vor?"],
                "params": {"collection_ids": ["kc_late"]},
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["late"],
            },
        ],
    }
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    {
                        "prompt": "Welcher Markt?",
                        "options": [],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    }
                ],
            },
            "ExecutionPlanModel": late_collection_plan,
        }
    )
    knowledge = MutableKnowledge()
    client = make_agent_client(monkeypatch, llm=llm)
    client.container.registry.get("workspace_agent")._knowledge = knowledge

    with client:
        run_id = submit_agent(
            client,
            knowledge_filters={"collection_ids": []},
        )
        wait_status(client, run_id, {"waiting_for_input"})

        knowledge.collections = [
            SimpleNamespace(
                id="kc_late",
                name="Later share",
                embedding_model="emb-1",
                document_count=1,
            )
        ]
        clarification = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{clarification['clarification_id']}",
            json={"answer": "Europa"},
        )
        assert answered.status_code == 200
        summary = wait_status(client, run_id, {"failed"})

    assert "plan_invalid" in summary["error"]["message"]
    assert "kc_late" in summary["error"]["message"]
    assert knowledge.list_calls == 0


# -- which questions a round asks, and what it assumes instead ---------------- #


def _question(
    prompt: str,
    *,
    subject: str = "assignment",
    assumption: str = "Der Lauf deckt den Gesamtmarkt ab.",
) -> dict[str, Any]:
    """One scripted proposed question with its subject and assumption."""
    return {
        "prompt": prompt,
        "options": [
            {"label": "Erste Wahl", "description": ""},
            {"label": "Zweite Wahl", "description": ""},
        ],
        "multi_select": False,
        "subject": subject,
        "default_assumption": assumption,
    }


SCOPE_QUESTION = _question(
    "Welche Wissenssammlung ist gemeint?",
    subject="knowledge_scope",
    assumption="Die festgelegte Sammlung ist gemeint.",
)


class _CollectionKnowledge:
    """Knowledge service stub: a fixed catalog, visibility always granted.

    ``fail_list`` makes that many next catalog reads fail.
    """

    def __init__(self, collections: list[SimpleNamespace]) -> None:
        self.collections = collections
        self.list_calls = 0
        self.fail_list = 0

    async def list_collections(self, **_kwargs: Any) -> list[SimpleNamespace]:
        self.list_calls += 1
        if self.fail_list:
            self.fail_list -= 1
            raise RuntimeError("catalog read failed")
        return list(self.collections)

    async def assert_collections_visible(
        self, _collection_ids: list[str], **_kwargs: Any
    ) -> None:
        return None


def _collection_client(
    monkeypatch: pytest.MonkeyPatch,
    llm: ScriptedLLM,
    *collections: tuple[str, str, int],
) -> TestClient:
    """An agent client whose run can pin knowledge collections.

    The admission probe of the knowledge store is answered as available;
    the mission reads the stub catalog.
    """
    import inqtrix.server.routers.runs as runs_router

    async def _available(_container: Any, **_kwargs: Any) -> tuple[bool, None]:
        return True, None

    monkeypatch.setattr(runs_router, "knowledge_availability", _available)
    client = make_agent_client(monkeypatch, llm=llm)
    knowledge = _CollectionKnowledge(
        [
            SimpleNamespace(
                id=collection_id,
                name=name,
                embedding_model="",
                document_count=documents,
            )
            for collection_id, name, documents in (
                collections or (("kc_abn", "Abnahme Langdokument", 2),)
            )
        ]
    )
    client.container.registry.get("workspace_agent")._knowledge = knowledge
    client.knowledge = knowledge  # type: ignore[attr-defined]
    return client


def _stored_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    """The run's events so far; the JSON read also serves a parked run,
    whose live stream would stay open."""
    return client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]


def _narrations(client: TestClient, run_id: str) -> dict[str, str]:
    """Narration id -> the text the transcript shows (latest upsert wins)."""
    return {
        event["data"]["narration_id"]: event["data"]["text"]
        for event in _stored_events(client, run_id)
        if event["type"] == "inqtrix.agent.narration"
    }


def _clarification_limits(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return [
        event["data"]
        for event in _stored_events(client, run_id)
        if event["type"] == "inqtrix.agent.limit.reached"
        and event["data"].get("kind") == "clarification_rounds"
    ]


def _answer_first_round(client: TestClient, run_id: str, answers: dict[str, Any]) -> None:
    clarification = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"][0]
    answered = client.post(
        f"/v1/runs/{run_id}/clarifications/{clarification['clarification_id']}",
        json={"answers": answers},
    )
    assert answered.status_code == 200, answered.text


def test_pinned_collection_question_is_settled_not_asked(monkeypatch):
    """The user pinned the collection: a question about WHICH collection is
    answered by the run's settings. It never parks the run, the transcript
    quotes it with the setting, and the intake saw the collection as a label
    without its id."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "underspecified",
                "clarification_questions": [SCOPE_QUESTION],
            }
        }
    )
    client = _collection_client(monkeypatch, llm)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            knowledge_filters={"collection_ids": ["kc_abn"]},
            agent_overrides={"agent_tier": "gruendlich"},
        )
        wait_status(client, run_id, {"completed"})
        assert human_wait_statuses(client, run_id) == []
        assert client.get(f"/v1/runs/{run_id}/clarifications").json()["data"] == []
        assert _clarification_limits(client, run_id) == []
        settled = _narrations(client, run_id)["n-clarify-settled-intake-0"]
    assert settled == (
        "Eine Rückfrage entfällt, weil die Einstellungen des Auftrags sie "
        "beantworten: „Welche Wissenssammlung ist gemeint?“ (festgelegte "
        "Sammlung „Abnahme Langdokument“)."
    )
    intake_prompt = next(p for name, p in llm.prompts if name == "AssignmentProfile")
    assert '- "Abnahme Langdokument" (2 documents)' in intake_prompt
    assert "kc_abn" not in intake_prompt


def test_inherited_scope_with_two_collections_still_asks(monkeypatch):
    """No explicit choice and two collections in the boundary: which one the
    assignment means is open, so the question is asked."""
    import inqtrix.agents.algorithm as agent_algorithm

    # The boundary the router resolves for an authenticated caller who
    # chose nothing: every visible collection, recorded as not explicit.
    monkeypatch.setattr(
        agent_algorithm,
        "pinned_knowledge_collection_ids",
        lambda _filters, *, scoped_principal: frozenset({"kc_a", "kc_b"}),
    )
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "underspecified",
                "clarification_questions": [SCOPE_QUESTION],
            }
        }
    )
    client = _collection_client(
        monkeypatch, llm, ("kc_a", "Handbuch", 3), ("kc_b", "Vertraege", 4)
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        narrations = _narrations(client, run_id)
    assert [q["prompt"] for q in rounds[0]["questions"]] == [SCOPE_QUESTION["prompt"]]
    assert not any(key.startswith("n-clarify-settled") for key in narrations)


def test_after_discovery_routes_on_questions_not_on_blocking_gaps(monkeypatch):
    """The second round exists for something to ASK: the analyst's question
    is asked although no gap is blocking."""
    llm = ScriptedLLM(
        overrides={
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [_question("Welcher Zeitraum soll gelten?")],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
    assert not any(gap["blocking"] for gap in DISCOVERY["gaps"])
    assert "structured:DiscoveryResult" in llm.calls
    assert [q["prompt"] for q in rounds[0]["questions"]] == [
        "Welcher Zeitraum soll gelten?"
    ]


def test_clarify_at_the_cap_with_nothing_to_ask_is_silent(monkeypatch):
    """At the round cap the node first works out what is left to ask. A
    rephrase of the answered round is skipped, nothing is left, and the run
    says nothing about a limit it did not hit."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    _question(
                        "Auf welchen KI-Markt soll sich die Analyse primaer beziehen?"
                    )
                ],
            },
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [
                    _question("Welchen KI-Markt sollen wir thematisch analysieren?")
                ],
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": ["q1_o1"], "text": ""}}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        narrations = _narrations(client, run_id)
        limits = _clarification_limits(client, run_id)
    assert limits == []
    assert not any(key.startswith("n-clarify-not-asked") for key in narrations)
    assert "übersprungen" in narrations["n-clarify-skip-discovery-1"]


def _run_to_the_cap(monkeypatch: pytest.MonkeyPatch, assumption: str):
    """A gruendlich run: the intake round is asked and answered, then the
    analyst proposes a new question the one-round cap keeps from being asked."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [_question("Welcher Markt ist gemeint?")],
            },
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [
                    _question("Welcher Zeitraum soll gelten?", assumption=assumption)
                ],
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    return client, llm


def test_clarification_limit_names_question_and_assumption(monkeypatch):
    assumption = "Der Lauf betrachtet die letzten zwölf Monate."
    client, llm = _run_to_the_cap(monkeypatch, assumption)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": [], "text": "Europa"}}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        limits = _clarification_limits(client, run_id)
        narrations = _narrations(client, run_id)
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
    assert len(rounds) == 1
    assert [(limit["suppressed"], limit["limit"], limit["used"]) for limit in limits] == [
        (1, 1, 1)
    ]
    assert narrations["n-clarify-not-asked-discovery-1"] == (
        "Das Rückfrage-Limit dieses Laufs ist erreicht; nicht gestellt: "
        "„Welcher Zeitraum soll gelten?“ – angenommen: Der Lauf betrachtet "
        "die letzten zwölf Monate."
    )
    assert assumption in plan["assumptions"]
    planner_prompt = next(p for name, p in llm.prompts if name == "ExecutionPlanModel")
    assert planner_prompt.count(assumption) == 1
    fence = '<untrusted_content source="run-assumptions">'
    fenced = planner_prompt.split(fence, 1)[1].split("</untrusted_content>", 1)[0]
    assert assumption in fenced
    clarified = planner_prompt.split("Already clarified", 1)[1].split(fence, 1)[0]
    assert "Europa" in clarified and assumption not in clarified


def test_a_question_without_an_assumption_stands_open_in_the_plan(monkeypatch):
    """The cap keeps a question out for which no assumption was proposed:
    the plan approval still lists it, named as an open question."""
    client, _llm = _run_to_the_cap(monkeypatch, "")
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": ["q1_o1"], "text": ""}}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
    assert (
        "Nicht gestellte Rückfrage ohne vorgeschlagene Annahme: „Welcher "
        "Zeitraum soll gelten?“"
    ) in plan["assumptions"]


def test_the_repeat_filter_reads_the_checkpoint_not_the_store(monkeypatch):
    """An earlier round's questions come from checkpointed state. With the
    store's round listing broken after the first answer, a rephrase of the
    answered question is still skipped instead of asked a second time —
    every segment filters exactly like the first pass."""
    from inqtrix.agents.control_memory import MemoryAgentControlStore

    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    _question(
                        "Auf welchen KI-Markt soll sich die Analyse primaer beziehen?"
                    )
                ],
            },
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [
                    _question("Welchen KI-Markt sollen wir thematisch analysieren?")
                ],
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "tief"})
        wait_status(client, run_id, {"waiting_for_input"})
        clarification = client.get(f"/v1/runs/{run_id}/clarifications").json()[
            "data"
        ][0]

        async def _listing_fails(self: Any, _run_id: str) -> list[Any]:
            raise RuntimeError("round listing failed")

        monkeypatch.setattr(
            MemoryAgentControlStore, "list_clarifications", _listing_fails
        )
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{clarification['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o1"], "text": ""}}},
        )
        assert answered.status_code == 200, answered.text
        wait_status(client, run_id, {"waiting_for_approval"})
        narrations = _narrations(client, run_id)
    assert "übersprungen" in narrations["n-clarify-skip-discovery-1"]


def test_a_collection_missing_from_the_listing_leaves_the_names_unread(monkeypatch):
    """The pinned collection is admitted but gone from the catalog listing
    (e.g. its deletion began): the intake states the boundary's count, never
    that no collection is released."""
    llm = ScriptedLLM()
    client = _collection_client(monkeypatch, llm)
    client.knowledge.collections = []  # type: ignore[attr-defined]
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            knowledge_filters={"collection_ids": ["kc_abn"]},
            agent_overrides={"agent_tier": "gruendlich"},
        )
        wait_status(
            client, run_id, {"completed", "failed", "waiting_for_approval"}
        )
    intake_prompt = next(p for name, p in llm.prompts if name == "AssignmentProfile")
    assert "1 collection, whose names could not be read" in intake_prompt
    assert "none is released" not in intake_prompt


def test_a_boundary_lost_before_planning_fails_with_its_code(monkeypatch):
    """The pinned collection turns invisible while the run waits on its
    discovery round: the resumed segment fails at planning with the
    capability code, not as an unexplained server error."""
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    llm = ScriptedLLM(
        overrides={
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [_question("Welcher Zeitraum soll gelten?")],
            }
        }
    )
    client = _collection_client(monkeypatch, llm)
    with client:
        run_id = submit_agent(
            client,
            knowledge_filters={"collection_ids": ["kc_abn"]},
            agent_overrides={"agent_tier": "gruendlich"},
        )
        wait_status(client, run_id, {"waiting_for_input"})

        async def _gone(_collection_ids: list[str], **_kwargs: Any) -> None:
            raise CollectionNotFound("kc_abn")

        client.knowledge.assert_collections_visible = _gone  # type: ignore[attr-defined]
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": ["q1_o1"], "text": ""}}
        )
        summary = wait_status(client, run_id, {"failed"})
    assert "knowledge.collection_not_found" in summary["error"]["message"]


def test_effective_clarification_cap_is_published_and_enforced(monkeypatch):
    """Operator cap 2, tier gruendlich 1: the event, the run snapshot and the
    capability manifest state the one cap the node enforces."""
    client, _llm = _run_to_the_cap(monkeypatch, "Die letzten zwölf Monate.")
    with client:
        assert client.container.settings.agent_platform.max_clarification_rounds == 2
        run_id = submit_agent(client, agent_overrides={"agent_tier": "gruendlich"})
        wait_status(client, run_id, {"waiting_for_input"})
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": ["q1_o1"], "text": ""}}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        limits = _clarification_limits(client, run_id)
        snapshots = [
            event["data"]["snapshot"]["execution"]["limits"]["clarification_rounds"]
            for event in _stored_events(client, run_id)
            if event["type"] == "inqtrix.agent.phase.changed"
        ]
        tiers = client.get("/v1/capabilities").json()["agent"]["tiers"]
    published = {entry["id"]: entry["clarification_rounds"] for entry in tiers}
    assert [limit["limit"] for limit in limits] == [1]
    assert {snapshot["limit"] for snapshot in snapshots} == {1}
    assert published["gruendlich"] == 1


def test_schnell_intake_questions_become_visible_assumptions(monkeypatch):
    """The speed tier asks nothing, but its intake questions do not vanish:
    each one is named with its assumption and stands in the plan."""
    assumption = "Der Lauf betrachtet den europäischen Markt."
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    _question("Welcher Markt ist gemeint?", assumption=assumption)
                ],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, agent_overrides={"agent_tier": "schnell"})
        wait_status(client, run_id, {"completed"})
        narrations = _narrations(client, run_id)
        limits = _clarification_limits(client, run_id)
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
    assert rounds == []
    assert narrations["n-clarify-not-asked-intake-0"] == (
        "In dieser Stufe stellt der Lauf keine Rückfragen. Offen war: "
        "„Welcher Markt ist gemeint?“ – angenommen: Der Lauf betrachtet den "
        "europäischen Markt."
    )
    assert [(limit["suppressed"], limit["limit"]) for limit in limits] == [(1, 0)]
    assert assumption in plan["assumptions"]


def test_round_overflow_is_named_not_cut_silently(monkeypatch):
    """Four proposed questions, three fit a round: the fourth is named with
    its assumption instead of disappearing behind a log line."""
    overflow_assumption = "Der Lauf nennt keine Preise."
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    _question("Welcher Markt ist gemeint?"),
                    _question("Welcher Zeitraum soll gelten?"),
                    _question("Welche Anbieter zählen dazu?"),
                    _question(
                        "Sollen Preise genannt werden?",
                        assumption=overflow_assumption,
                    ),
                ],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        overflow = _narrations(client, run_id)["n-clarify-not-asked-intake-0"]
        _answer_first_round(
            client,
            run_id,
            {
                f"q{index}": {"option_ids": [f"q{index}_o1"], "text": ""}
                for index in (1, 2, 3)
            },
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
    questions = rounds[0]["questions"]
    assert [q["id"] for q in questions] == ["q1", "q2", "q3"]
    # The wire shape carries the answerable question only.
    assert all(
        set(question) == {"id", "prompt", "options", "multi_select"}
        for question in questions
    )
    assert overflow == (
        "Die Runde ist voll (Obergrenze: 3); eine weitere Rückfrage nicht "
        "gestellt: „Sollen Preise genannt werden?“ – angenommen: Der Lauf "
        "nennt keine Preise."
    )
    assert overflow_assumption in plan["assumptions"]
    # The node re-executed on resume; the assumption was recorded once.
    analyst_prompt = next(p for name, p in llm.prompts if name == "DiscoveryResult")
    assert analyst_prompt.count("Sollen Preise genannt werden?") == 1


def test_clarify_narrations_keep_distinct_ids_per_source(monkeypatch):
    """A settled question at intake and another after discovery: both lines
    survive the transcript's upsert because each id names its source."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "underspecified",
                "clarification_questions": [SCOPE_QUESTION],
            },
            "DiscoveryResult": {
                **DISCOVERY,
                "questions_for_user": [
                    _question(
                        "Sollen weitere Sammlungen einbezogen werden?",
                        subject="knowledge_scope",
                    )
                ],
            },
        }
    )
    client = _collection_client(monkeypatch, llm)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            knowledge_filters={"collection_ids": ["kc_abn"]},
        )
        wait_status(client, run_id, {"completed"})
        narrations = _narrations(client, run_id)
    assert "Welche Wissenssammlung ist gemeint?" in narrations[
        "n-clarify-settled-intake-0"
    ]
    assert "Sollen weitere Sammlungen einbezogen werden?" in narrations[
        "n-clarify-settled-discovery-0"
    ]


def test_settled_round_survives_a_catalog_failure_on_resume(monkeypatch):
    """The settled decision reads no catalog. When the names cannot be read
    in the resumed segment, the round keeps its question set and layout, the
    line falls back to the collection count, and the run goes on."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    SCOPE_QUESTION,
                    _question("Welcher Markt ist gemeint?"),
                ],
            }
        }
    )
    client = _collection_client(monkeypatch, llm)
    with client:
        run_id = submit_agent(
            client, knowledge_filters={"collection_ids": ["kc_abn"]}
        )
        wait_status(client, run_id, {"waiting_for_input"})
        first = _narrations(client, run_id)["n-clarify-settled-intake-0"]
        client.knowledge.fail_list = 1  # type: ignore[attr-defined]
        _answer_first_round(
            client, run_id, {"q1": {"option_ids": ["q1_o2"], "text": ""}}
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        rounds = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
        resumed = _narrations(client, run_id)["n-clarify-settled-intake-0"]
    assert client.knowledge.fail_list == 0  # type: ignore[attr-defined]
    assert len(rounds) == 1
    assert [q["prompt"] for q in rounds[0]["questions"]] == ["Welcher Markt ist gemeint?"]
    assert "festgelegte Sammlung „Abnahme Langdokument“" in first
    assert resumed == (
        "Eine Rückfrage entfällt, weil die Einstellungen des Auftrags sie "
        "beantworten: „Welche Wissenssammlung ist gemeint?“ (1 festgelegte "
        "Sammlung)."
    )


def test_structured_round_answer_maps_options(monkeypatch):
    """A structured answers map resolves the round and lands in history."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "scope_clarity": "ambiguous",
                "clarification_questions": [
                    {
                        "prompt": "Welcher Markt?",
                        "options": [
                            {"label": "Europa", "description": ""},
                            {"label": "USA", "description": ""},
                        ],
                        "multi_select": False,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    },
                    {
                        "prompt": "Welche Aspekte?",
                        "options": [
                            {"label": "Preise", "description": ""},
                            {"label": "Anbieter", "description": ""},
                            {"label": "Regulierung", "description": ""},
                        ],
                        "multi_select": True,
                        "subject": "assignment",
                        "default_assumption": "Der Lauf deckt den Gesamtmarkt ab.",
                    },
                ],
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_input"})
        clarifications = client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        clarification_id = clarifications[0]["clarification_id"]
        # Validation matrix: partial answers are rejected loudly.
        partial = client.post(
            f"/v1/runs/{run_id}/clarifications/{clarification_id}",
            json={"answers": {"q1": {"option_ids": ["q1_o1"]}}},
        )
        assert partial.status_code == 400
        # Single-select rejects multiple picks.
        multi_on_single = client.post(
            f"/v1/runs/{run_id}/clarifications/{clarification_id}",
            json={
                "answers": {
                    "q1": {"option_ids": ["q1_o1", "q1_o2"]},
                    "q2": {"option_ids": ["q2_o1"]},
                }
            },
        )
        assert multi_on_single.status_code == 400
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{clarification_id}",
            json={
                "answers": {
                    "q1": {"option_ids": ["q1_o1"], "text": ""},
                    "q2": {
                        "option_ids": ["q2_o1", "q2_o3"],
                        "text": "Fokus auf B2B",
                    },
                }
            },
        )
        assert answered.status_code == 200
        assert answered.json()["answers"]["q2"]["option_ids"] == [
            "q2_o1",
            "q2_o3",
        ]
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})


def test_discovery_web_preview_gated_to_autonomous(monkeypatch):
    """Standard discovery makes no web contact; autonomous keeps the preview."""
    balanced = make_agent_client(monkeypatch)
    with balanced:
        run_id = submit_agent(balanced)  # default balanced
        wait_status(balanced, run_id, {"waiting_for_approval"})
        # The plan gate parks AFTER discovery ran — no web probe fired.
        assert balanced.container.providers.search.queries == []
        approve_pending(balanced, run_id)
        wait_status(balanced, run_id, {"completed"})

    autonomous = make_agent_client(
        monkeypatch,
        search=RetryNotifyingSearch(),
    )
    with autonomous:
        run_id = submit_agent(autonomous, autonomy="autonomous")
        wait_status(autonomous, run_id, {"completed"})
        # The preview ran during discovery (plus any plan web_instant).
        assert autonomous.container.providers.search.queries
        retry = next(
            event["data"]
            for event in run_events(autonomous, run_id)
            if event["type"] == "inqtrix.agent.activity"
            and event["data"].get("retry")
        )
        assert retry["scope"] == "discovery"
        assert retry["purpose"] == "Webvorschau in der Erkundung"


def test_discovery_activity_progress_is_scoped_per_operation(monkeypatch):
    goals = [f"Teilfrage {index}" for index in range(1, 5)]
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "content_questions": goals,
            }
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        activities = [
            event["data"]
            for event in run_events(client, run_id)
            if event["type"] == "inqtrix.agent.activity"
            and event["data"].get("scope") == "discovery"
            and event["data"].get("status") == "started"
        ]

    searches = [
        activity
        for activity in activities
        if activity["operation"] == "knowledge.search"
    ]
    assert [activity["current"] for activity in searches] == [1, 2, 3, 4]
    assert {activity["total"] for activity in searches} == {4}
    assert [activity["detail"] for activity in searches] == goals
    collections = [
        activity
        for activity in activities
        if activity["operation"] == "knowledge.collections.list"
    ]
    assert [(item["current"], item["total"]) for item in collections] == [
        (1, 1)
    ]


@pytest.mark.parametrize(
    ("questions", "line"),
    [
        (
            2,
            "Die Erkundung hat ihr Abrufbudget ausgeschöpft (Obergrenze: 2); "
            "1 geplanter Abruf wurde ausgelassen.",
        ),
        (
            4,
            "Die Erkundung hat ihr Abrufbudget ausgeschöpft (Obergrenze: 2); "
            "3 geplante Abrufe wurden ausgelassen.",
        ),
    ],
)
def test_discovery_limit_line_has_singular_and_plural(
    monkeypatch, questions, line
):
    """The budget is the only cut of the discovery searches: the transcript
    names the cap and counts what it left out, in the right number."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "content_questions": [
                    f"Welche Anbieter nennt Quelle {index}?"
                    for index in range(1, questions + 1)
                ],
            }
        }
    )
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        platform=AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_VOLATILE=True,
            INQTRIX_AGENT_DISCOVERY_MAX_TOOL_CALLS=2,
        ),
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        # The JSON listing, not the live stream: a parked run never closes it.
        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
    limits = [
        event["data"]
        for event in events
        if event["data"].get("narration_id") == "n-discovery-limit"
    ]
    assert [item["text"] for item in limits] == [line]
    reached = [
        event["data"]
        for event in events
        if event["data"].get("kind") == "discovery_tool_calls"
    ]
    assert [(item["limit"], item["omitted"]) for item in reached] == [
        (2, questions - 1)
    ]


def test_discovery_limit_line_exists_in_both_languages():
    from inqtrix.i18n import MESSAGES, t_count

    assert "agent_discovery_limit_narration" not in MESSAGES
    assert t_count(
        {"language": "en"}, "agent_discovery_limit_narration", 1, limit=2
    ) == (
        "The exploration used up its lookup budget (cap: 2); 1 planned lookup was "
        "left out."
    )
    assert t_count(
        {"language": "en"}, "agent_discovery_limit_narration", 3, limit=2
    ) == (
        "The exploration used up its lookup budget (cap: 2); 3 planned lookups "
        "were left out."
    )


def test_chat_deliverable_writes_answer_artifact_not_memo(monkeypatch):
    """Response_form=chat -> run-local answer artifact, NO memo."""
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "response_form": "chat"},
            "SectionText": {
                "markdown": "Direkte Antwort: Der Markt waechst [W1]."
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, session_id="sess-chat")
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        summary = wait_status(client, run_id, {"completed"})
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        kinds = {a["kind"] for a in artifacts}
        assert "answer" in kinds
        assert "memo" not in kinds
        answer_meta = [a for a in artifacts if a["kind"] == "answer"][0]
        # Run-local: the answer never joins the session-memo lineage.
        assert answer_meta["session_id"] is None
        assert answer_meta["status"] == "ready"
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{answer_meta['artifact_id']}"
        ).json()
        assert (
            detail["content_markdown"]
            == "Direkte Antwort: Der Markt waechst [W1]."
        )
        # AgentResult.answer == artifact body (one truth).
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == detail["content_markdown"]
        assert summary["status"] == "completed"


def test_response_form_request_override_forces_chat(monkeypatch):
    """The composer override beats the intake profile."""
    client = make_agent_client(monkeypatch)  # profile says canvas
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle eine Marktanalyse.",
                "mode": "workspace_agent",
                "session_id": "sess-override",
                "response_form": "chat",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        # The stored overrides mirror the form for summaries/audit.
        assert (
            response.json()["agent_overrides"]["response_form"] == "chat"
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        kinds = {
            a["kind"]
            for a in client.get(
                f"/v1/runs/{run_id}/artifacts"
            ).json()["data"]
        }
        assert "answer" in kinds
        assert "memo" not in kinds


def test_response_form_invalid_is_rejected(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Frage.",
                "mode": "workspace_agent",
                "response_form": "pdf",
            },
        )
        assert response.status_code == 400
        assert "response_form" in response.text


def test_answer_node_routes_from_approved_plan_tasks():
    """Answer routing uses authorized work, including a web task with zero
    results."""
    from inqtrix.agents.algorithm import answer_node_for

    assert answer_node_for([]) == "agent_answer_light"
    assert (
        answer_node_for([{"tool_kind": "rag_query"}, {"tool_kind": "synthesis"}])
        == "agent_answer_light"
    )
    assert (
        answer_node_for([{"tool_kind": "web_research"}, {"tool_kind": "synthesis"}])
        == "agent_answer"
    )


def test_follow_up_turn_receives_session_history(monkeypatch):
    """A follow-up turn's intake sees the prior turn's Q + answer."""
    captured: list[str] = []

    def capturing_profile(prompt: str) -> dict[str, Any]:
        captured.append(prompt)
        return dict(PROFILE)

    llm = ScriptedLLM()
    llm.scripts["AssignmentProfile"] = capturing_profile
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run1 = submit_agent(client, session_id="sess-k1")
        wait_status(client, run1, {"waiting_for_approval"})
        approve_pending(client, run1)
        wait_status(client, run1, {"completed"})
        run2 = submit_agent(client, session_id="sess-k1")
        wait_status(client, run2, {"waiting_for_approval"})
        approve_pending(client, run2)
        wait_status(client, run2, {"completed"})
    assert len(captured) >= 2
    follow_up = captured[1]
    assert "Conversation so far in this session" in follow_up
    assert "User: Erstelle eine Marktanalyse." in follow_up
    # The prior turn's memo body reaches the follow-up as the agent
    # answer (rows are truth — built server-side, not sent by the FE).
    assert "Agent: " in follow_up
    assert "Die Marktlage ist stabil" in follow_up
    # The first turn ran WITHOUT history (nothing prior in the session).
    assert "Conversation so far in this session" not in captured[0]


def test_plan_repair_retry_then_loud_failure(monkeypatch):
    """Scenario: two invalid plans -> run fails plan_invalid (no memo)."""
    bad_plan = {"summary_markdown": "x", "tasks": [
        {"id": "t1", "title": "Nur Suche", "tool_kind": "rag_query"}
    ]}
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": bad_plan})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client)
        summary = wait_status(client, run_id, {"failed"})
        assert "plan_invalid" in summary["error"]["message"]
        # Exactly two planner attempts (one repair round).
        assert client.llm.calls.count("structured:ExecutionPlanModel") == 2


@pytest.mark.parametrize("duration_ms", [7540, 1234])
def test_instant_search_retains_capability_duration(duration_ms):
    from inqtrix.agents.algorithm import _run_web_instant
    from inqtrix.agents.control_ports import PlanTaskRecord
    from inqtrix.capabilities.catalog.web import WebInstantOutput

    async def invoke(_capability, _payload, _context):
        return WebInstantOutput(
            query_id="query-duration", query="A factual question",
            provider="test", answer="A provider answer", sources=[],
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:00:08Z",
            duration_ms=duration_ms,
        )

    deps = SimpleNamespace(
        capabilities=SimpleNamespace(invoke=invoke),
        context=SimpleNamespace(
            run_id="run-duration", workspace_id=None, principal=None,
            providers=SimpleNamespace(search=None),
        ),
        visible_to=None, knowledge_collection_ids=(), emit=lambda *_: None,
    )
    task = PlanTaskRecord(
        task_id="t1", plan_id="plan-duration", run_id="run-duration",
        ordinal=0, title="Fact", tool_kind="web_instant",
        queries=("A factual question",),
    )
    outcome = _run_web_instant(deps, task)
    assert (
        outcome.web_search_ledger["searches"]["query-duration"]["duration_ms"]
        == duration_ms
    )


def test_web_instant_task_spawns_no_child(monkeypatch):
    """Scenario 13a: a punctual gap uses web_instant, zero children."""
    instant_plan = {
        "summary_markdown": "Ein Instant-Task.",
        "tasks": [
            {
                "id": "t1",
                "title": "Fakt nachschlagen",
                "tool_kind": "web_instant",
                "objective": "Aktuelles Marktvolumen kritisch pruefen.",
                "queries": [
                    "Welches belastbare Marktvolumen wird fuer 2026 berichtet?"
                ],
                "expected_output": "Eine belegte Kurzantwort mit Risiken.",
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": instant_plan})
    search = FakeSearch()
    client = make_agent_client(monkeypatch, llm=llm, search=search)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        approved_query = (
            "Welches belastbare Marktvolumen wird fuer 2026 berichtet?"
        )
        assert search.queries[-1] == approved_query
        assert search.queries.count(approved_query) == 1
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []
        # The instant source became a W-reference in the memo refs.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo = [a for a in artifacts if a["kind"] == "memo"][0]
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{memo['artifact_id']}"
        ).json()
        assert any(
            ref.get("url") == "https://example.com/markt"
            for ref in detail["refs"]
        )


def test_provider_grounded_instant_number_remains_available_to_mission(
    monkeypatch,
):
    """Azure answer context and its cited source survive mission synthesis."""
    synthesis_prompts: list[str] = []

    def section_text(prompt: str) -> dict[str, str]:
        synthesis_prompts.append(prompt)
        if "Available evidence:\n(no evidence)" in prompt:
            return {"markdown": "Keine weiteren offenen Punkte."}
        return {
            "markdown": "Der Markt erreicht US-$1.5T [W1].\n\nFormel $x$.",
        }

    class UrlOnlySearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            self.queries.append(query)
            return GroundedSearchResult(
                answer=(
                    "Gartner beziffert die weltweiten KI-Ausgaben 2025 "
                    "auf US-$1.5T "
                    "([Gartner](https://example.com/markt))."
                ),
                sources=[
                    GroundedSource(
                        url="https://example.com/markt",
                        title="Gartner",
                        snippet="",
                        origin="url_citation",
                    ),
                    GroundedSource(
                        url="https://example.com/zweite-quelle",
                        title="Second source",
                        snippet="Second evidence that belongs to W2 only.",
                    ),
                ],
            )

    llm = ScriptedLLM(overrides={"SectionText": section_text})
    client = make_agent_client(monkeypatch, llm=llm, search=UrlOnlySearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo = [item for item in artifacts if item["kind"] == "memo"][0]
        evidence_artifact = [
            item for item in artifacts if item["kind"] == "evidence_bundle"
        ][0]
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{memo['artifact_id']}"
        ).json()
        evidence_detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{evidence_artifact['artifact_id']}"
        ).json()
        result = client.get(f"/v1/runs/{run_id}/result").json()

    assert synthesis_prompts
    assert any(
        "weltweiten KI-Ausgaben 2025" in prompt
        for prompt in synthesis_prompts
    )
    assert all("Second evidence" not in prompt for prompt in synthesis_prompts)
    ledger_ref = next(
        item
        for item in evidence_detail["refs"]
        if item.get("url") == "https://example.com/markt"
    )
    assert ledger_ref.get("excerpt") in (None, "")
    assert "weltweiten KI-Ausgaben 2025" in ledger_ref["grounded_support"]
    assert "\\$" not in ledger_ref["grounded_support"]
    assert [
        ref["url"] for ref in detail["refs"]
    ] == ["https://example.com/markt"]
    assert [
        ref["url"] for ref in result["references"]
    ] == ["https://example.com/markt"]
    assert "US-\\$1.5T" in detail["content_markdown"]
    assert "$x$" in detail["content_markdown"]
    assert "Belegstatus" not in detail["content_markdown"]
    assert "weltweiten KI-Ausgaben 2025" in ledger_ref[
        "grounded_support"
    ]


def test_independent_instant_tasks_overlap_and_call_each_query_once(
    monkeypatch,
):
    """One plan wave runs independent Instant calls concurrently."""
    import threading

    questions = (
        "Welche belastbaren Quellen beziffern das Marktvolumen 2026?",
        "Welche Gegenbelege relativieren das Marktwachstum 2026?",
    )
    parallel_plan = {
        "summary_markdown": "Zwei unabhaengige Instant-Fragen.",
        "tasks": [
            {
                "id": "t1",
                "title": "Marktvolumen",
                "tool_kind": "web_instant",
                "queries": [questions[0]],
            },
            {
                "id": "t2",
                "title": "Gegenbelege",
                "tool_kind": "web_instant",
                "queries": [questions[1]],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1", "t2"],
            },
        ],
    }
    barrier = threading.Barrier(2)

    class ParallelSearch(FakeSearch):
        def __init__(self) -> None:
            super().__init__()
            self._lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            with self._lock:
                self.queries.append(query)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            barrier.wait(timeout=5)
            try:
                return GroundedSearchResult(
                    answer=f"Antwort zu {query}",
                    sources=[
                        GroundedSource(
                            url=f"https://example.com/{len(self.queries)}",
                            title="Quelle",
                            snippet="Beleg",
                        )
                    ],
                )
            finally:
                with self._lock:
                    self.active -= 1

    llm = ScriptedLLM(overrides={"ExecutionPlanModel": parallel_plan})
    search = ParallelSearch()
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=search,
        platform=platform_without_web_preview(),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})

    assert sorted(search.queries) == sorted(questions)
    assert all(search.queries.count(question) == 1 for question in questions)
    assert search.max_active == 2


def test_fast_instant_task_persists_before_its_slow_sibling(monkeypatch):
    """A completed work unit must not look active until its whole wave ends."""
    import threading

    fast_query = "Welche Quelle beantwortet die schnelle Frage?"
    slow_query = "Welche Quelle beantwortet die langsame Frage?"
    parallel_plan = {
        "summary_markdown": "Zwei zeitlich unterschiedliche Aufgaben.",
        "tasks": [
            {
                "id": "fast",
                "title": "Schnelle Frage",
                "tool_kind": "web_instant",
                "queries": [fast_query],
            },
            {
                "id": "slow",
                "title": "Langsame Frage",
                "tool_kind": "web_instant",
                "queries": [slow_query],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["fast", "slow"],
            },
        ],
    }
    fast_finished = threading.Event()
    slow_started = threading.Event()
    release_slow = threading.Event()

    class StaggeredSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            if query == slow_query:
                slow_started.set()
                assert release_slow.wait(5)
            elif query == fast_query:
                fast_finished.set()
            else:  # pragma: no cover - planner contract makes this impossible
                raise AssertionError(query)
            return GroundedSearchResult(
                answer=f"Antwort zu {query}",
                sources=[
                    GroundedSource(
                        url=f"https://example.com/{'fast' if query == fast_query else 'slow'}",
                        title="Quelle",
                        snippet="Beleg",
                    )
                ],
            )

    llm = ScriptedLLM(overrides={"ExecutionPlanModel": parallel_plan})
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=StaggeredSearch(),
        platform=platform_without_web_preview(),
    )
    try:
        with client:
            run_id = submit_agent(client, autonomy="autonomous")
            assert fast_finished.wait(5)
            assert slow_started.wait(5)

            deadline = time.monotonic() + 5
            task_statuses: dict[str, str] = {}
            while time.monotonic() < deadline:
                plan = client.get(f"/v1/runs/{run_id}/plan").json()
                task_statuses = {
                    task["task_id"]: task["status"]
                    for task in plan["tasks"]
                }
                if task_statuses.get("fast") == "completed":
                    break
                time.sleep(0.02)

            assert task_statuses["fast"] == "completed"
            assert task_statuses["slow"] == "running"
            release_slow.set()
            wait_status(client, run_id, {"completed"})
    finally:
        release_slow.set()


@pytest.mark.parametrize(
    ("language", "clarify"), [("de", False), ("de", True), ("en", True)]
)
def test_knowledge_hits_flow_through_capability_layer(
    monkeypatch, language, clarify
):
    """Scenario 1b: knowledge.search wire shape (``hits``) reaches both
    consumers — the discovery probe digest and the file analysis, which
    reads the found document completely and cites its verified chunk.

    Uses the REAL catalog input/output models through the real registry, so
    a key rename in the capability layer breaks this test instead of
    silently reporting "No hits" against a filled index.
    """
    from inqtrix.agents.context_sources import material_window
    from inqtrix.capabilities.catalog.knowledge import (
        CollectionsListInput,
        CollectionsListOutput,
        CollectionSummary,
        DocumentCiteInput,
        DocumentCiteOutput,
        DocumentReadInput,
        DocumentReadOutput,
        KnowledgeHit,
        KnowledgeSearchInput,
        KnowledgeSearchOutput,
        QuoteCitation,
    )
    from inqtrix.capabilities.contracts import CapabilityDefinition, Effect

    document_text = "Interner Marktbericht: Anbieter A fuehrt."
    chunk_evidence = {
        "chunk_id": "ch-1", "document_id": "doc-1",
        "collection_id": "c1", "chunk_index": 0, "title": "Interner Marktbericht",
        "excerpt": document_text, "page_number": None, "score": 0.0,
        "source_span": {"start": 0, "end": 41, "unit": "utf8_byte"},
        "revision_id": "rev-1", "generation_id": "gen-1",
        "provenance_status": "verified_span",
    }

    async def _read(payload, context):
        text, window = material_window(document_text, "rev-1", payload)
        return DocumentReadOutput(
            id="doc-1", collection_id="c1", title="Interner Marktbericht",
            text=text, chunk_count=1, window=window, representation="knowledge_text",
        )

    async def _cite(payload, context):
        return DocumentCiteOutput(
            document_id="doc-1", version=payload.version,
            citations=[
                QuoteCitation(index=index, status="cited", evidence=chunk_evidence)
                for index, _quote in enumerate(payload.quotes)
            ],
        )

    async def _list(payload, context):
        return CollectionsListOutput(
            collections=[
                CollectionSummary(
                    id="c1",
                    name="Marktberichte",
                    embedding_model="emb-1",
                    document_count=1,
                )
            ]
        )

    capability_queries: list[str] = []

    async def _search(payload, context):
        capability_queries.append(payload.query)
        return KnowledgeSearchOutput(
            query=payload.query,
            hits=[
                KnowledgeHit(
                    document_id="doc-1",
                    collection_id="c1",
                    document_title="Interner Marktbericht",
                    chunk_index=0,
                    chunk_id="ch-1",
                    rank=1,
                    excerpt="Interner Marktbericht: Anbieter A fuehrt.",
                    page_number=None,
                    score=0.9,
                    source_span={"start": 0, "end": 44, "unit": "utf8_byte"},
                    revision_id="rev-1",
                    generation_id="gen-1",
                    provenance_status="verified_span",
                )
            ],
        )

    analyst_prompts: list[str] = []

    def discovery_script(prompt: str) -> dict[str, Any]:
        analyst_prompts.append(prompt)
        return dict(DISCOVERY)

    file_plan = {
        "summary_markdown": "Ein Datei-Analyse-Task, dann Synthese.",
        "tasks": [
            {
                "id": "t1",
                "title": "Bericht auswerten",
                "tool_kind": "file_analysis",
                "gap_ids": ["g1"],
                "objective": "Interne Anbieterbefunde pruefen.",
                "queries": ["Anbieteruebersicht"],
                "expected_output": "Relevante interne Befunde mit Belegstellen.",
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {
                **PROFILE,
                "language": language,
                "scope_clarity": "ambiguous" if clarify else "clear",
                "clarification_questions": (
                    [
                        {
                            "prompt": "Which scope?",
                            "options": [],
                            "multi_select": False,
                            "subject": "assignment",
                            "default_assumption": "The run keeps the assignment's scope.",
                        }
                    ]
                    if clarify
                    else []
                ),
            },
            "ExecutionPlanModel": file_plan,
            "DiscoveryResult": discovery_script,
            "WindowExtraction": lambda prompt: {
                "findings": (
                    [{"quote": "Anbieter A fuehrt.", "claim": "Anbieter A fuehrt den Markt."}]
                    if "Anbieter A fuehrt." in prompt
                    else []
                ),
            },
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    client.container.capability_registry.register_all(
        [
            CapabilityDefinition(
                id="knowledge.collections.list",
                summary="test stub",
                input_model=CollectionsListInput,
                output_model=CollectionsListOutput,
                effect=Effect.READ,
                idempotent=True,
                handler=_list,
            ),
            CapabilityDefinition(
                id="knowledge.search",
                summary="test stub",
                input_model=KnowledgeSearchInput,
                output_model=KnowledgeSearchOutput,
                effect=Effect.READ,
                idempotent=True,
                handler=_search,
            ),
            CapabilityDefinition(
                id="knowledge.document.read",
                summary="test stub",
                input_model=DocumentReadInput,
                output_model=DocumentReadOutput,
                effect=Effect.READ,
                idempotent=True,
                handler=_read,
            ),
            CapabilityDefinition(
                id="knowledge.document.cite",
                summary="test stub",
                input_model=DocumentCiteInput,
                output_model=DocumentCiteOutput,
                effect=Effect.READ,
                idempotent=True,
                handler=_cite,
            ),
        ]
    )
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            question=(
                "Energiebilanz?" if language == "de" else "Battery storage?"
            ),
        )
        if clarify:
            wait_status(client, run_id, {"waiting_for_input"})
            assert llm.calls == ["structured:AssignmentProfile"]
            clarification = client.get(
                f"/v1/runs/{run_id}/clarifications"
            ).json()["data"][0]
            answered = client.post(
                f"/v1/runs/{run_id}/clarifications/"
                f"{clarification['clarification_id']}",
                json={
                    "answer": (
                        "Allgemeiner Überblick."
                        if language == "de"
                        else "General overview."
                    )
                },
            )
            assert answered.status_code == 200
        wait_status(client, run_id, {"completed"})
        # A persisted classifier result must also reach helpers whose only
        # context is the newly created segment deps after clarification.
        # Neither short question gives the fallback heuristic a clear signal.
        analysis_prompts = [
            p for name, p in llm.prompts
            if name in {"WindowExtraction", "DocumentAnalysisResult"}
        ]
        assert analysis_prompts, llm.calls
        expected = (
            "Antworte IMMER auf Deutsch"
            if language == "de"
            else "ALWAYS respond in English"
        )
        assert all(p.startswith(expected) for p in analysis_prompts)
        assert llm.calls.count("structured:AssignmentProfile") == 1
        # The hit reached the analyst digest by title and collection name,
        # never by its ids, and not as an empty search.
        assert analyst_prompts, llm.calls
        assert (
            '[S1.1] "Interner Marktbericht" · collection "Marktberichte": '
            "Interner Marktbericht: Anbieter A fuehrt."
        ) in analyst_prompts[0]
        assert "No passages found." not in analyst_prompts[0]
        assert "doc-1" not in analyst_prompts[0]
        # The search only SELECTED the document; the analysis read it
        # completely and its cited chunk became a K-reference on the memo.
        assert "structured:WindowExtraction" in llm.calls
        result = client.get(f"/v1/runs/{run_id}/tasks/t1/result").json()
        record = result["document_analysis"]
        assert record["complete"] is True
        # Search hits only select documents; the task's evidence is exactly
        # the verified chunk its quote was cited from.
        assert [
            (ref["document_id"], ref["chunk_index"], ref["provenance_status"])
            for ref in result["references"]
        ] == [("doc-1", 0, "verified_span")]
        assert [finding["quote"] for finding in record["findings"]] == ["Anbieter A fuehrt."]
        assert any(
            "Expected output: Relevante interne Befunde mit Belegstellen."
            in query
            for query in capability_queries
        )
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo = [a for a in artifacts if a["kind"] == "memo"][0]
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{memo['artifact_id']}"
        ).json()
        assert any(
            ref.get("document_id") == "doc-1" for ref in detail["refs"]
        )


def test_rag_query_task_completes_with_memo_k_references(monkeypatch):
    """Scenario: a rag_query task drives the registered ``knowledge`` mode
    to a COMPLETED run and its ``report_references`` become K-references on
    the memo.

    The default agent harness registers no ``knowledge`` algorithm, so
    _run_rag_query's success branch (reference aggregation) is only reached
    when one is present — the bad-plan and no-web scenarios exercise only
    its failure branches.
    """
    from inqtrix.core.results import AgentResult

    class _FakeKnowledgeAlgorithm:
        id = "knowledge"
        display_name = "Knowledge (test stub)"

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            answer = "Interne Befunde: Anbieter A fuehrt."
            assert context.event_sink is not None
            warning = {
                "code": "chunks_require_reindex",
                "message": "Treffer müssen neu indiziert werden.",
                "reason": "source_unverified",
                "stage": "canonical_hydration",
                "count": 2,
                "recommended_action": "reindex",
            }
            context.event_sink(
                "inqtrix.knowledge.retrieval.warning",
                warning,
            )
            return AgentResult(
                answer=answer,
                raw={
                    "answer": answer,
                    "usage": {"prompt_tokens": 4, "completion_tokens": 3},
                    "result_state": {
                        "answer": answer,
                        "knowledge_retrieval": {"warnings": [warning]},
                        "report_references": [
                            {
                                "url": None,
                                "title": "Interner Marktbericht",
                                "document_id": "doc-9",
                                "chunk_index": 0,
                                "source_text": "Anbieter A fuehrt.",
                            }
                        ],
                    },
                },
            )

    rag_plan = {
        "summary_markdown": "Ein RAG-Task, dann Synthese.",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Quellen befragen",
                "tool_kind": "rag_query",
                "gap_ids": ["g1"],
                "objective": "Interne Anbieterbefunde pruefen.",
                "queries": ["Anbieteruebersicht"],
                "expected_output": "Belegte interne Befunde.",
                "params": {"profile": "gruendlich"},
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": rag_plan})
    client = make_agent_client(monkeypatch, llm=llm)
    client.container.registry.register(_FakeKnowledgeAlgorithm())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        events = run_events(client, run_id)
        # A rag_query task never spawns child research runs.
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo = [a for a in artifacts if a["kind"] == "memo"][0]
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{memo['artifact_id']}"
        ).json()
        assert any(
            ref.get("document_id") == "doc-9" for ref in detail["refs"]
        )
        warnings = [
            event["data"]
            for event in events
            if event["type"] == "inqtrix.knowledge.retrieval.warning"
        ]
        assert warnings == [
            {
                "attempt": 1,
                "code": "chunks_require_reindex",
                "count": 2,
                "query_index": 1,
                "reason": "source_unverified",
                "recommended_action": "reindex",
                "stage": "canonical_hydration",
                "task_id": "t1",
            }
        ]


def test_rag_answer_without_references_is_insufficient_evidence(monkeypatch):
    """An answer string alone is not a completed evidence task."""
    from inqtrix.core.results import AgentResult

    class _UnreferencedKnowledgeAlgorithm:
        id = "knowledge"
        display_name = "Knowledge without evidence (test stub)"

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            answer = "Eine unreferenzierte interne Antwort."
            return AgentResult(
                answer=answer,
                raw={
                    "answer": answer,
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    "result_state": {
                        "answer": answer,
                        "report_references": [],
                    },
                },
            )

    plan = {
        "summary_markdown": "Interne Evidenz pruefen.",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Quellen befragen",
                "tool_kind": "rag_query",
                "gap_ids": ["g1"],
                "queries": ["Welche Belege liegen intern vor?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}),
    )
    client.container.registry.register(_UnreferencedKnowledgeAlgorithm())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        stored = client.get(f"/v1/runs/{run_id}/plan").json()

    task = next(item for item in stored["tasks"] if item["task_id"] == "t1")
    assert task["status"] == "insufficient_evidence"


def test_rag_grounding_rejection_fails_task_and_reaches_mission_audit(
    monkeypatch,
):
    """A returned Knowledge terminal failure is never a completed RAG task."""
    from inqtrix.core.results import AgentResult
    from inqtrix.knowledge.grounding import (
        GROUNDING_MARKER_FALLBACK,
        GroundingFailureCode,
        GroundingStatus,
    )

    safe_reason = (
        "Die Antwort wurde nicht veröffentlicht, weil der erforderliche "
        "Belegblock nicht sicher gelesen und geprüft werden konnte."
    )

    class _RejectedKnowledgeAlgorithm:
        id = "knowledge"
        display_name = "Knowledge grounding rejection (test stub)"

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            del request, runtime
            assert context.event_sink is not None
            context.event_sink(
                "inqtrix.knowledge.answer.retry",
                {
                    "attempt": 2,
                    "quotes_total": 1,
                    "quotes_unverified": 1,
                },
            )
            context.event_sink(
                "inqtrix.knowledge.grounding.checked",
                {
                    "marker": GROUNDING_MARKER_FALLBACK,
                    "status": GroundingStatus.REJECTED_FORMAT.value,
                    "failure_code": GroundingFailureCode.FORMAT_INVALID.value,
                    "format_repaired": False,
                    "quotes_total": 0,
                    "quotes_verified": 0,
                },
            )
            return AgentResult(
                answer=safe_reason,
                result_type="knowledge_result",
                raw={
                    "answer": safe_reason,
                    "usage": {"prompt_tokens": 13, "completion_tokens": 5},
                    "result_state": {
                        "answer": safe_reason,
                        "report_references": [],
                        "_terminal_failure": {
                            "type": GroundingFailureCode.FORMAT_INVALID.value,
                            "message": safe_reason,
                        },
                    },
                },
            )

    plan = {
        "summary_markdown": "Interne Evidenz prüfen.",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Quellen befragen",
                "tool_kind": "rag_query",
                "gap_ids": ["g1"],
                "queries": ["Welche Belege liegen intern vor?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}),
    )
    client.container.registry.register(_RejectedKnowledgeAlgorithm())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"failed"})
        stored = client.get(f"/v1/runs/{run_id}/plan").json()
        events = run_events(client, run_id)

    task = next(item for item in stored["tasks"] if item["task_id"] == "t1")
    assert task["status"] == "failed"
    grounding = [
        event["data"]
        for event in events
        if event["type"] == "inqtrix.knowledge.grounding.checked"
    ]
    assert grounding == [
        {
            "marker": GROUNDING_MARKER_FALLBACK,
            "status": GroundingStatus.REJECTED_FORMAT.value,
            "failure_code": GroundingFailureCode.FORMAT_INVALID.value,
            "format_repaired": False,
            "quotes_total": 0,
            "quotes_verified": 0,
            "task_id": "t1",
            "attempt": 1,
            "query_index": 1,
        }
    ]
    retry = [
        event["data"]
        for event in events
        if event["type"] == "inqtrix.knowledge.answer.retry"
    ]
    # The forwarder enriches with task_id/query_index but must NOT
    # clobber the event's own regeneration attempt (documented always 2).
    assert retry == [
        {
            "attempt": 2,
            "quotes_total": 1,
            "quotes_unverified": 1,
            "task_id": "t1",
            "query_index": 1,
        }
    ]
    failed = [
        event["data"]
        for event in events
        if event["type"] == "inqtrix.agent.task.failed"
        and event["data"].get("task_id") == "t1"
    ]
    assert failed[0]["failure"]["code"] == (
        GroundingFailureCode.FORMAT_INVALID.value
    )
    assert safe_reason in failed[0]["failure"]["message"]


def _put_target_document(client: TestClient, document_id: str) -> None:
    saved = client.put(
        f"/v1/editor/documents/{document_id}",
        json={
            "title": "Compliance-Roadmap",
            "content_markdown": (
                "# Compliance-Roadmap\n\n"
                "Alte Kernaussage zur Regulierung.\n\n"
                "Weitere Details folgen."
            ),
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
            "folder_id": None,
            "revision": 1,
            "source": "blank",
        },
    )
    assert saved.status_code == 200, saved.text


PATCH_INSTRUCT = {
    "assistant_message": "Kernaussage aktualisiert.",
    "edits": [
        {
            "find": "Alte Kernaussage zur Regulierung.",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "Neue Kernaussage auf Basis der Recherche [W1].",
            "note": "Aktualisiert.",
        }
    ],
    "warnings": [],
}


def test_document_target_patch_is_always_gated_and_never_self_applied(
    monkeypatch,
):
    """A document-targeted assignment proposes an editor patch and parks
    for the patch approval EVEN in autonomous mode (the write
    invariant). Approving resumes the run; the patch row stays
    PENDING — apply happens only through the user-driven route."""
    llm = ScriptedLLM(
        overrides={"inqtrix_editor_instruction_v1": dict(PATCH_INSTRUCT)}
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        _put_target_document(client, "doc-m7")
        response = client.post(
            "/v1/runs",
            json={
                "question": "Arbeite die Rechercheergebnisse ein.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
                "document_id": "doc-m7",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        # Autonomous skips the PLAN gate — the ONLY interrupt is the patch.
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        pending = [a for a in approvals if a["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["kind"] == "patch"
        assert pending[0]["subject_type"] == "editor_patch"
        patch_id = pending[0]["subject_id"]
        assert patch_id

        # The proposal row exists, source=agent, still pending.
        patches = client.get("/v1/editor/documents/doc-m7/patches").json()
        assert [p["patch_id"] for p in patches["data"]] == [patch_id]
        assert patches["data"][0]["source"] == "agent"
        assert patches["data"][0]["status"] == "pending"

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        wait_status(client, run_id, {"completed"})

        # Rows are the truth: the approval row records the decision.
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["kind"] == "patch"
        assert approvals[0]["status"] == "approved"
        # approval resumes the run, it never applies — the row is
        # still pending until the user hits the ONE apply route.
        detail = client.get(f"/v1/editor/patches/{patch_id}").json()
        assert detail["status"] == "pending"

        # The artifact row references the patch id only.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        kinds = {artifact["kind"] for artifact in artifacts}
        assert "editor_patch" in kinds

        # The user applies through route #16; the document text changes.
        applied = client.post(
            f"/v1/editor/patches/{patch_id}:apply",
            json={"expected_revision": detail["document_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied_edit_ids"] == ["ed_1"]
        document = client.get("/v1/editor/documents/doc-m7").json()
        assert "Neue Kernaussage auf Basis der Recherche" in (
            document["content_markdown"]
        )


def test_patch_rejection_ends_the_run_normally(monkeypatch):
    """Rejecting the patch is a normal outcome: the memo stays the
    deliverable, the run COMPLETES with patch_decision=rejected."""
    llm = ScriptedLLM(
        overrides={"inqtrix_editor_instruction_v1": dict(PATCH_INSTRUCT)}
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        _put_target_document(client, "doc-m7b")
        response = client.post(
            "/v1/runs",
            json={
                "question": "Arbeite die Rechercheergebnisse ein.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
                "document_id": "doc-m7b",
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})
        pending = [
            a
            for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ]
        client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "reject"},
        )
        summary = wait_status(client, run_id, {"completed"})
        assert summary["status"] == "completed"
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert approvals[0]["status"] == "rejected"
        # The reject decision cascades onto the patch ROW too — rejected
        # edits must never stay pending/appliable (review finding).
        patch_id = approvals[0]["subject_id"]
        detail = client.get(f"/v1/editor/patches/{patch_id}").json()
        assert detail["status"] == "rejected"
        applied = client.post(
            f"/v1/editor/patches/{patch_id}:apply",
            json={"expected_revision": detail["document_revision"]},
        )
        assert applied.status_code == 409
        # The memo artifact survives the rejection.
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        assert any(artifact["kind"] == "memo" for artifact in artifacts)


def test_schnell_tier_document_target_still_proposes_the_patch(monkeypatch):
    """Skipping the critic (labels tier) must never skip the deliverable:
    a document-targeted schnell run still reaches the patch phase."""
    llm = ScriptedLLM(
        overrides={"inqtrix_editor_instruction_v1": dict(PATCH_INSTRUCT)}
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        _put_target_document(client, "doc-schnell")
        response = client.post(
            "/v1/runs",
            json={
                "question": "Arbeite die Rechercheergebnisse ein.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
                "document_id": "doc-schnell",
                "agent_overrides": {"agent_tier": "schnell"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        pending = [a for a in approvals if a["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["kind"] == "patch"


def test_invisible_target_document_fails_loudly(monkeypatch):
    """A document id the owner cannot see is a hard, loud failure."""
    client = make_agent_client(monkeypatch)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Arbeite die Rechercheergebnisse ein.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
                "document_id": "doc-does-not-exist",
            },
        )
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"failed"})
        assert summary["error"]["message"] == "patch_document_not_found"


def test_cancel_while_waiting_cancels_cleanly(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        cancelled = client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert {task["status"] for task in plan["tasks"]} == {"skipped"}
        types = event_types(client, run_id)
        assert types[-1] == "inqtrix.run.cancelled"


def test_cancel_during_execution_ends_cancelled_not_failed(monkeypatch):
    """A client cancel DURING the task wave ends the run CANCELLED —
    the all-hard-failed check must not reinterpret tasks that failed
    BECAUSE of the cancel as ``all_tasks_failed``."""
    import threading

    started = threading.Event()
    release = threading.Event()

    class BlockingSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            started.set()
            release.wait(10)
            raise RuntimeError("Suche waehrend Cancel abgebrochen")

    instant_plan = {
        "summary_markdown": "Ein Instant-Task.",
        "tasks": [
            {
                "id": "t1",
                "title": "Fakt nachschlagen",
                "tool_kind": "web_instant",
                "gap_ids": ["g1"],
                "queries": ["Marktvolumen 2026"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    # No discovery web preview: the block must hit during EXECUTION.
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": instant_plan})
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=BlockingSearch(),
        platform=platform_without_web_preview(),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert started.wait(10), "web_instant task never started"
        cancel = client.post(f"/v1/runs/{run_id}/cancel")
        assert cancel.status_code == 200, cancel.text
        release.set()
        final = wait_status(client, run_id, {"cancelled", "failed"})
        assert final["status"] == "cancelled", final
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert all(task["status"] != "running" for task in plan["tasks"])
        assert plan["tasks"][0]["status"] == "failed"
        assert plan["tasks"][1]["status"] == "skipped"
        types = event_types(client, run_id)
        assert types[-1] == "inqtrix.run.cancelled"


def test_task_agent_cancel_marks_parent_and_remaining_tasks(monkeypatch):
    """A task-level AgentCancelled is folded before the parent cancels."""
    from inqtrix.exceptions import AgentCancelled

    class CancellingSearch(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            raise AgentCancelled("provider observed cancellation")

    client = make_agent_client(
        monkeypatch,
        search=CancellingSearch(),
        platform=platform_without_web_preview(),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        final = wait_status(client, run_id, {"cancelled", "failed"})
        assert final["status"] == "cancelled"
        plan = client.get(f"/v1/runs/{run_id}/plan").json()

    assert plan["tasks"][0]["status"] == "failed"
    assert plan["tasks"][1]["status"] == "skipped"


def test_operator_token_budget_fails_distinct_from_client_cancel(monkeypatch):
    client = make_agent_client(monkeypatch, max_tokens_per_run=10)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"failed", "cancelled"})
        events = run_events(client, run_id)

    assert summary["status"] == "failed"
    assert summary["error"]["type"] == "token_budget_exceeded"
    assert "server-side token budget" in summary["error"]["message"]
    token_limits = [
        event
        for event in events
        if event["type"] == "inqtrix.agent.limit.reached"
        and event["data"].get("kind") == "tokens"
    ]
    assert len(token_limits) == 1
    assert token_limits[0]["data"]["recoverable"] is False
    assert token_limits[0]["data"]["extendable"] is False


def test_budget_stop_persists_every_finished_parallel_task(monkeypatch):
    import inqtrix.agents.algorithm as agent_algorithm
    from inqtrix.agents.scheduler import TaskOutcome

    parallel_plan = {
        "summary_markdown": "Zwei parallele Aufgaben.",
        "tasks": [
            {
                "id": "t1",
                "title": "Erste Frage",
                "tool_kind": "web_instant",
                "queries": ["Welche Quelle beantwortet die erste Frage?"],
            },
            {
                "id": "t2",
                "title": "Zweite Frage",
                "tool_kind": "web_instant",
                "queries": ["Welche Quelle beantwortet die zweite Frage?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1", "t2"],
            },
        ],
    }
    llm = ScriptedLLM(overrides={"ExecutionPlanModel": parallel_plan})

    def completed_task(*_args: Any, **_kwargs: Any) -> TaskOutcome:
        return TaskOutcome(
            status="completed",
            summary="Belegt.",
            usage={"prompt_tokens": 6, "completion_tokens": 0},
        )

    monkeypatch.setattr(agent_algorithm, "_run_web_instant", completed_task)
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        max_tokens_per_run=40,
        platform=platform_without_web_preview(),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"failed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()

    assert summary["error"]["type"] == "token_budget_exceeded"
    assert [task["status"] for task in plan["tasks"][:2]] == [
        "completed",
        "completed",
    ]


def test_replan_appends_additive_version_balanced_auto(monkeypatch):
    """An internal read-only delta is auto-approved in balanced mode."""
    sufficiency_calls = {"n": 0}

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        sufficiency_calls["n"] += 1
        if sufficiency_calls["n"] == 1:
            return {"coverage": "uncovered", "missing": ["Quellen fehlen"]}
        return {"coverage": "covered", "missing": []}

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Bestand nachpruefen",
            "tool_kind": "rag_query",
            "queries": ["Marktlage Details intern"],
        }
    )
    llm = ScriptedLLM(
        overrides={
            "SufficiencyJudgement": sufficiency_script,
            "ReplanDeltaModel": delta,
        }
    )
    client = make_agent_client(
        monkeypatch, llm=llm, search=NoEvidenceSearch()
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})

        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 2
        assert [v["version"] for v in plan["versions"]] == [2, 1]
        tasks_by_id = {task["task_id"]: task for task in plan["tasks"]}
        assert tasks_by_id["t1"]["status"] == "insufficient_evidence"
        assert "structured:SufficiencyJudgement" in llm.calls
        assert tasks_by_id["t2"]["status"] != "pending"
        types = event_types(client, run_id)
        assert "inqtrix.agent.plan.revised" in types
        # Balanced auto-approval: exactly ONE HUMAN waiting interval
        # (children parks are store-internal and don't count).
        assert human_wait_statuses(client, run_id) == [
            "waiting_for_approval"
        ]


def test_replan_repairs_changed_completed_task_id_instead_of_skipping(
    monkeypatch,
):
    """Changed work under a completed id enters the planner repair loop."""
    sufficiency_calls = {"n": 0}

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        sufficiency_calls["n"] += 1
        if sufficiency_calls["n"] == 1:
            return {"coverage": "uncovered", "missing": ["Detail fehlt"]}
        return {"coverage": "covered", "missing": []}

    changed_id = replan_delta(
        {
            **PLAN["tasks"][0],
            "queries": ["Eine inhaltlich geaenderte Evidenzfrage?"],
        },
        summary="Ungueltiger Replan mit wiederverwendeter ID.",
    )
    repaired = replan_delta(
        {
            "id": "t2",
            "title": "Zusaetzliche Evidenzfrage",
            "tool_kind": "web_instant",
            "gap_ids": ["g1"],
            "queries": ["Welche Quelle schliesst die verbleibende Luecke?"],
        },
        summary="Additiver Replan mit neuer Task-ID.",
    )
    deltas = iter([changed_id, repaired])
    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(
            overrides={
                "ReplanDeltaModel": lambda prompt: next(deltas),
                "SufficiencyJudgement": sufficiency_script,
            }
        ),
        search=NoEvidenceSearch(),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()

    tasks = {task["task_id"]: task for task in plan["tasks"]}
    assert plan["version"] == 2
    assert set(tasks) == {"t1", "t2", "s"}
    assert tasks["t1"]["status"] == "insufficient_evidence"
    assert tasks["t2"]["status"] == "insufficient_evidence"
    assert tasks["t1"]["queries"] == PLAN["tasks"][0]["queries"]
    assert client.llm.calls.count("structured:ExecutionPlanModel") == 1
    assert client.llm.calls.count("structured:ReplanDeltaModel") == 2


def test_empty_replan_delta_proceeds_directly_to_synthesis(monkeypatch):
    """A model-confirmed no-op cannot trigger repair or another execute loop."""
    llm = ScriptedLLM(
        overrides={
            "SufficiencyJudgement": {
                "coverage": "uncovered",
                "missing": ["No additional source is justified"],
            },
            "ReplanDeltaModel": replan_delta(
                summary="Proceed with the visible evidence gap."
            ),
        }
    )
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=NoEvidenceSearch(),
    )

    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()

    assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
    assert plan["version"] == 2


def test_replan_with_web_delta_regates_in_balanced(monkeypatch):
    """A replan with new web queries parks again in Standard mode.

    The approved plan is the web-search consent, so unseen queries
    never run.
    """
    sufficiency_calls = {"n": 0}

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        sufficiency_calls["n"] += 1
        if sufficiency_calls["n"] == 1:
            return {"coverage": "uncovered", "missing": ["Quellen fehlen"]}
        return {"coverage": "covered", "missing": []}

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Nachrecherche",
            "tool_kind": "web_instant",
            "queries": ["Welche Quellen ergaenzen die Marktlage im Detail?"],
        }
    )
    llm = ScriptedLLM(
        overrides={
            "SufficiencyJudgement": sufficiency_script,
            "ReplanDeltaModel": delta,
        }
    )
    client = make_agent_client(
        monkeypatch, llm=llm, search=NoEvidenceSearch()
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        first = [
            a for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ][0]
        assert first["kind"] == "plan"
        assert "new_tasks" not in first["payload"]
        approve_pending(client, run_id)
        # The web replan parks AGAIN (no auto-approve for new queries).
        wait_status(client, run_id, {"waiting_for_approval"})
        second = [
            a for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ][0]
        # The replan gate says WHY and WHAT it adds — the not
        # yet executed source tasks of the new version, never the
        # synthesis task, never the already executed ones.
        assert second["kind"] == "replan"
        assert second["payload"]["reason"] == "replan"
        assert second["payload"]["plan_version"] == 2
        new_tasks = second["payload"]["new_tasks"]
        assert [(t["task_id"], t["title"], t["tool_kind"]) for t in new_tasks] == [
            ("t2", "Nachrecherche", "web_instant")
        ]
        assert all(isinstance(t["ordinal"], int) for t in new_tasks)
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        assert human_wait_statuses(client, run_id) == [
            "waiting_for_approval",
            "waiting_for_approval",
        ]


def test_report_guidance_can_be_cleared_at_a_later_gate(monkeypatch):
    """A requirement the user withdraws must actually go away.

    Stored and applied on truthiness alone, the emptied field would never
    reach the state: the first gate's wording would keep shaping every
    later section and the user would have no way to take it back.
    Presence is the decision — an absent key keeps what stands, an empty
    key clears it.
    """
    sufficiency_calls = {"n": 0}

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        sufficiency_calls["n"] += 1
        if sufficiency_calls["n"] == 1:
            return {"coverage": "uncovered", "missing": ["Quellen fehlen"]}
        return {"coverage": "covered", "missing": []}

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Nachrecherche",
            "tool_kind": "web_instant",
            "queries": ["Welche Quellen ergaenzen die Marktlage im Detail?"],
        }
    )
    llm = ScriptedLLM(
        overrides={
            "SufficiencyJudgement": sufficiency_script,
            "ReplanDeltaModel": delta,
        }
    )
    client = make_agent_client(
        monkeypatch, llm=llm, search=NoEvidenceSearch()
    )

    def decide(run_id: str, guidance: str) -> None:
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        pending = [a for a in approvals if a["status"] == "pending"]
        assert pending, approvals
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve", "report_guidance": guidance},
        )
        assert decided.status_code == 200, decided.text

    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        decide(run_id, "Schreibe ausschliesslich in Reimform.")
        wait_status(client, run_id, {"waiting_for_approval"})
        before = len(client.llm.prompts)
        decide(run_id, "")
        wait_status(client, run_id, {"completed"})
        after_clearing = [
            prompt
            for name, prompt in client.llm.prompts[before:]
            if name in ("ReportOutline", "SectionText", "AgentCriticReport")
        ]
        assert after_clearing, "synthesis never ran after the second gate"
        assert not any("Reimform" in prompt for prompt in after_clearing)


def test_a_renamed_session_memo_keeps_its_name(monkeypatch):
    """A name the user gave the document must survive the next turn.

    The synthesis re-titled the session memo on every canvas turn from
    the fresh outline, so a rename lasted exactly until the next
    request — the file in the transcript changed identity under the
    user, with no way to tell why. The name belongs to the document.
    """
    client = make_agent_client(monkeypatch)
    with client:
        first = submit_agent(client, session_id="sess-rename")
        wait_status(client, first, {"waiting_for_approval"})
        approve_pending(client, first)
        wait_status(client, first, {"completed"})
        memos = [
            row
            for row in client.get(f"/v1/runs/{first}/artifacts").json()["data"]
            if row["kind"] == "memo"
        ]
        assert memos, "the mission wrote no memo"
        memo_id = memos[0]["artifact_id"]

        renamed = client.patch(
            f"/v1/runs/{first}/artifacts/{memo_id}",
            json={"title": "Mein eigener Name"},
        )
        assert renamed.status_code == 200, renamed.text

        second = submit_agent(client, session_id="sess-rename")
        wait_status(client, second, {"waiting_for_approval"})
        approve_pending(client, second)
        wait_status(client, second, {"completed"})
        detail = client.get(
            f"/v1/runs/{second}/artifacts/{memo_id}"
        ).json()
        assert detail["title"] == "Mein eigener Name", detail["title"]
        # The turn still wrote: only the NAME is stable, not the content.
        assert detail["revision"] > memos[0]["revision"]


def test_a_rejected_subquery_keeps_the_verified_siblings(monkeypatch):
    """One unusable sub-answer must not throw away the checked ones.

    A rejected answer never enters the synthesis — that part is right.
    But a task that stopped at the FIRST rejection would let a single
    malformed evidence block discard every sibling query that had
    verified, and with a one-task plan the whole run would fail. The gap
    travels with the result instead: named in the answer, visible as a
    failed step, and the remaining evidence still reaches the report.
    """
    from inqtrix.core.results import AgentResult

    safe_reason = (
        "Die Antwort wurde nicht veröffentlicht, weil der erforderliche "
        "Belegblock nicht sicher gelesen und geprüft werden konnte."
    )

    class _OneBadOneGoodKnowledge:
        id = "knowledge"
        display_name = "Knowledge partial rejection (test stub)"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            del request, runtime, context
            self.calls += 1
            if self.calls == 1:
                return AgentResult(
                    answer=safe_reason,
                    result_type="knowledge_result",
                    raw={
                        "answer": safe_reason,
                        "usage": {},
                        "result_state": {
                            "answer": safe_reason,
                            "report_references": [],
                            "_terminal_failure": {
                                "type": "knowledge_grounding_format_invalid",
                                "message": safe_reason,
                            },
                        },
                    },
                )
            return AgentResult(
                answer="Belegte Teilantwort. [K1]",
                result_type="knowledge_result",
                raw={
                    "answer": "Belegte Teilantwort. [K1]",
                    "usage": {},
                    "result_state": {
                        "answer": "Belegte Teilantwort. [K1]",
                        "report_references": [
                            {
                                "label": "K1",
                                "title": "Internes Dokument",
                                "document_id": "doc-1",
                                "chunk_index": 0,
                                "source_text": "Belegter Satz.",
                            }
                        ],
                    },
                },
            )

    plan = {
        "summary_markdown": "Interne Evidenz prüfen.",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Quellen befragen",
                "tool_kind": "rag_query",
                "queries": ["Erste Teilfrage?", "Zweite Teilfrage?"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }
    client = make_agent_client(
        monkeypatch,
        llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}),
    )
    client.container.registry.register(_OneBadOneGoodKnowledge())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        stored = client.get(f"/v1/runs/{run_id}/plan").json()

    task = next(item for item in stored["tasks"] if item["task_id"] == "t1")
    assert task["status"] != "failed", task["status"]
    # The gap is NAMED, not swallowed …
    assert "Erste Teilfrage?" in task["result_summary"]
    # … and the verified sibling survived.
    assert "Belegte Teilantwort" in task["result_summary"]


def test_critic_research_routes_to_additive_replan(monkeypatch):
    critic_calls = {"n": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["n"] += 1
        if critic_calls["n"] == 1:
            return review_verdict(prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")})
        return review_verdict(prompt)

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Gegenposition pruefen",
            "tool_kind": "web_instant",
            "queries": [
                "Welche belastbaren Gegenbelege widersprechen der Marktlage 2026?"
            ],
        }
    )
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": delta,
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})

        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 2
        assert "t2" in [task["task_id"] for task in plan["tasks"]]
        assert client.llm.calls.count("structured:ExecutionPlanModel") == 1
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
        assert critic_calls["n"] == 2
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []
        types = event_types(client, run_id)
        assert "inqtrix.agent.plan.revised" in types
        assert "inqtrix.agent.activity" in types


def test_an_unmet_output_requirement_revises_the_memo_and_never_researches(monkeypatch):
    """A criterion the text falls short of belongs to the text: the named
    passage is replaced, no plan version follows, and the memo does not
    close with a research gap nobody has."""
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": lambda prompt: review_verdict(
            prompt,
            unmet={1: ("output_requirement", "Der Beleg steht nicht am Anbieter.")},
            findings=[review_finding(
                prompt, "Die Marktlage ist stabil [W1].",
                kind="run_diagnostic", basis_ids=(text_measurements_basis(prompt),),
            )],
        ),
        "DeepRevisionBundle": {"edits": [
            {"finding_id": "finding_1", "replacement": "Anbieter A führt den stabilen Markt [W1]."},
        ]},
        "RevisionCheckVerdict": recheck_unquoted,
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        assert client.get(f"/v1/runs/{run_id}/plan").json()["version"] == 1
        assert "structured:ReplanDeltaModel" not in llm.calls
        assert llm.calls.count("structured:DeepReviewVerdict") == 1
        answer = client.get(f"/v1/runs/{run_id}/result").json()["answer"]
        assert "Anbieter A führt den stabilen Markt [W1]." in answer
        assert "Noch offen:" not in answer
        decisions = [
            row["data"]["kind"]
            for row in client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
            if str(row.get("data", {}).get("kind", "")).startswith("critic_")
        ]
        assert decisions == ["critic_revise"]


def test_replan_round_budget_is_shared_by_gate_and_critic(monkeypatch):
    """The deterministic gate (insufficient evidence) and the critic
    ('research') draw from ONE shared ``replan_rounds`` budget: a gate
    replan plus a critic-research replan consume both of
    ``max_replan_rounds=2``, so a persistently research-demanding critic is
    exhausted after just a single research replan (no third plan version).

    Pins the current accounting — were the two increment sites to use
    separate budgets, the critic would win a second research replan and the
    plan would reach version 4 with four planner calls.
    """
    sufficiency = iter([{"coverage": "uncovered", "missing": ["Marktlage"]}])

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        return next(sufficiency, dict(SUFFICIENCY))

    critic_calls = {"n": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["n"] += 1
        return review_verdict(
                prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")}
            )

    def additive(*extra_ids: str) -> dict[str, Any]:
        return replan_delta(
            *[
            {
                "id": extra_id,
                "title": f"Zusatz {extra_id}",
                "tool_kind": "web_instant",
                "queries": [f"Zusatzfrage {extra_id}"],
            }
            for extra_id in extra_ids
            ]
        )

    deltas = iter([additive("t2"), additive("t3")])
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: next(deltas),
            "SufficiencyJudgement": sufficiency_script,
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(
        monkeypatch, llm=llm, search=NoEvidenceSearch()
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 3
        assert client.llm.calls.count("structured:ExecutionPlanModel") == 1
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 2
        assert critic_calls["n"] == 2


def _research_demanding_critic(counter: dict[str, int]):
    """A review that ALWAYS leaves the plan's criterion open for lack of information."""

    def critic_script(prompt: str) -> dict[str, Any]:
        counter["n"] += 1
        return review_verdict(prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")})

    return critic_script


class QuerySourceSearch(FakeSearch):
    """A search whose source depends on the query: new questions, new evidence."""

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        self.queries.append(query)
        slug = "-".join(query.lower().split())[:40]
        return GroundedSearchResult(
            answer="Web-Vorschau",
            sources=[GroundedSource(url=f"https://example.com/{slug}", title=query, snippet="Neu.")],
        )


def _additive_web_delta(task_id: str) -> dict[str, Any]:
    return replan_delta(
        {
            "id": task_id,
            "title": f"Zusatz {task_id}",
            "tool_kind": "web_instant",
            "queries": [f"Zusatzfrage {task_id}"],
        }
    )


def _limit_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return [
        e["data"]
        for e in run_events(client, run_id)
        if e["type"] == "inqtrix.agent.limit.reached"
    ]


def _exhausted_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return [
        e["data"]
        for e in run_events(client, run_id)
        if e["type"] == "inqtrix.agent.activity"
        and e["data"].get("kind") == "critic_research_exhausted"
    ]


def test_a_research_round_without_new_evidence_ends_research(monkeypatch):
    """Research that added no evidence cannot close what the review still
    finds open: the next round would ask the same sources the same
    questions. The critic ends the route after ONE fruitless round —
    visibly, with its own reason — instead of spending the round cap.

    Mutation: comparing the round counter instead of the evidence identity
    (or dropping the brake) lets the critic win a second replan.
    """
    critic_calls = {"n": 0}
    deltas = iter([_additive_web_delta("t2"), _additive_web_delta("t3")])
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: next(deltas),
            "DeepReviewVerdict": _research_demanding_critic(critic_calls),
        }
    )
    # The search returns the same source for every question.
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 2
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
        assert critic_calls["n"] == 2
        assert _limit_events(client, run_id) == []
        [stopped] = _exhausted_events(client, run_id)
        assert stopped["reason"] == "no_new_evidence"
        assert stopped["label"] == "Nachrecherche beendet"
        memo = _memo_body(client, run_id)
        assert "die letzte Nachrecherche fügte keine neuen Belege hinzu" in memo
        assert "Noch offen: Alle Anbieter mit Quelle belegt." in memo


def test_research_with_new_evidence_continues(monkeypatch):
    """The brake measures evidence, not rounds: a research round that found
    something new may be followed by another one (up to the round cap)."""
    critic_calls = {"n": 0}
    deltas = iter([_additive_web_delta("t2"), _additive_web_delta("t3")])
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: next(deltas),
            "DeepReviewVerdict": _research_demanding_critic(critic_calls),
        }
    )
    client = make_agent_client(monkeypatch, llm=llm, search=QuerySourceSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 2
        assert all(event.get("reason") != "no_new_evidence" for event in _exhausted_events(client, run_id))


def test_a_delegated_mission_opens_at_most_one_critic_research_round(monkeypatch):
    """A delegated mission (kernel child) verifies twice already — its own
    critic round and the parent's deep-verify pass — so the platform cap of
    two critic research rounds narrows to ONE for it. The second demand ends
    the route visibly (``limit.reached`` with the narrowed limit, an activity
    that names the delegation rule) and the memo closes with the critic's
    remaining findings under "Offene Punkte" instead of a third synthesis.

    Mutation: DELEGATED_CRITIC_RESEARCH_ROUNDS = 2 (or dropping the
    narrowing) lets the critic win a second replan — plan version 3, two
    delta calls, three critic calls.
    """
    critic_calls = {"n": 0}
    deltas = iter([_additive_web_delta("t2"), _additive_web_delta("t3")])
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: next(deltas),
            "DeepReviewVerdict": _research_demanding_critic(critic_calls),
        }
    )
    # Each research round finds a new source, so it is the round cap — not
    # the no-new-evidence brake — that ends the research route here.
    client = make_agent_client(monkeypatch, llm=llm, search=QuerySourceSearch())
    store = client.container.run_service.run_store
    real_get = store.get

    def child_row_get(run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {**real_get(run_id, **kwargs), "parent_run_id": "run_parent"}

    monkeypatch.setattr(store, "get", child_row_get)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 2
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
        assert critic_calls["n"] == 2
        assert _limit_events(client, run_id) == [
            {
                "kind": "replan_rounds",
                "used": 1,
                "limit": 1,
                "ceiling": 1,
                "extendable": False,
                "recoverable": False,
                "reason": "deterministic_mission_budget",
                "state": "continued_with_visible_gap",
            }
        ]
        exhausted = _exhausted_events(client, run_id)
        assert len(exhausted) == 1
        assert exhausted[0]["label"] == "Recherche-Limit erreicht"
        assert "Unterauftrag" in exhausted[0]["detail"]
        memo = _memo_body(client, run_id)
        assert "## Offene Punkte" in memo
        assert "Noch offen: Alle Anbieter mit Quelle belegt." in memo
        # The critic's finding detail is checker vocabulary: it stays in
        # the critic report, not in the memo the reader gets.
        assert "Aktuelle Gegenposition fehlt." not in memo
        # The row the time budget reads carries the timing block the
        # budget is judged from (kind != standard → timing projected).
        assert summary["timing"]["active_seconds"] >= 0


def test_a_resynthesis_after_a_research_round_continues_the_first_memo(
    monkeypatch,
):
    """After the critic's research round a fresh outline would rebuild the
    memo from scratch — shorter, undated, titled with the outline's
    working title. The second outline call receives this run's first-pass
    memo as the memo to continue.

    Mutation: dropping ``state.get("memo_markdown") or`` from the prior
    memo hand-over leaves the second outline prompt without it.
    """
    critic_calls = {"n": 0}
    deltas = iter([_additive_web_delta("t2"), _additive_web_delta("t3")])
    outline_prompts: list[str] = []

    def capture_outline(prompt: str) -> dict[str, Any]:
        outline_prompts.append(prompt)
        return dict(OUTLINE)

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: next(deltas),
            "DeepReviewVerdict": _research_demanding_critic(critic_calls),
            "ReportOutline": capture_outline,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    store = client.container.run_service.run_store
    real_get = store.get

    def child_row_get(run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {**real_get(run_id, **kwargs), "parent_run_id": "run_parent"}

    monkeypatch.setattr(store, "get", child_row_get)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    assert len(outline_prompts) == 2
    assert "Existing memo:" not in outline_prompts[0]
    assert "Existing memo:" in outline_prompts[1]
    assert "do NOT start from scratch" in outline_prompts[1]


def test_the_mission_time_budget_closes_the_research_route_with_named_gaps(
    monkeypatch,
):
    """Once a mission has spent its active-time budget (default 12 min,
    gate waits excluded), the critic's research demand no longer opens a
    round: no delta call, plan version 1, a ``limit.reached`` of kind
    ``mission_time_budget`` with the spent/allowed seconds, an activity
    that names the budget, and the memo closes with the named gaps.

    Mutation: dropping ``not over_budget`` from the route condition lets
    the replan happen — one delta call, plan version 2.
    """
    from inqtrix.agents import algorithm as algorithm_module

    critic_calls = {"n": 0}
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: _additive_web_delta("t2"),
            "DeepReviewVerdict": _research_demanding_critic(critic_calls),
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    monkeypatch.setattr(
        algorithm_module, "_mission_active_seconds", lambda deps: 900.0
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 0
        assert critic_calls["n"] == 1
        assert client.get(f"/v1/runs/{run_id}/plan").json()["version"] == 1
        assert _limit_events(client, run_id) == [
            {
                "kind": "mission_time_budget",
                "used": 900,
                "limit": 720,
                "ceiling": 720,
                "extendable": False,
                "recoverable": False,
                "reason": "deterministic_mission_budget",
                "state": "continued_with_visible_gap",
            }
        ]
        exhausted = _exhausted_events(client, run_id)
        assert len(exhausted) == 1
        assert exhausted[0]["label"] == "Zeitbudget erreicht"
        assert "12 Minuten" in exhausted[0]["detail"]
        memo = _memo_body(client, run_id)
        assert "## Offene Punkte" in memo
        assert "Alle Anbieter mit Quelle belegt." in memo


def test_a_zero_time_budget_disables_the_mission_clock(monkeypatch):
    """``INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS=0`` disables the budget:
    the same 900 active seconds do not block the critic's research round
    (one delta call), and the clock is not even read."""
    from inqtrix.agents import algorithm as algorithm_module

    critic_calls = {"n": 0}
    verdicts = iter(["research"])

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["n"] += 1
        verdict = next(verdicts, "pass")
        if verdict == "pass":
            return review_verdict(prompt)
        return _research_demanding_critic({"n": 0})(prompt)

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: _additive_web_delta("t2"),
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        platform=AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_VOLATILE=True,
            INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS=0,
        ),
    )
    clock_reads = {"n": 0}

    def clock(deps: Any) -> float:
        clock_reads["n"] += 1
        return 900.0

    monkeypatch.setattr(algorithm_module, "_mission_active_seconds", clock)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
        assert critic_calls["n"] == 2
        assert clock_reads["n"] == 0
        assert _limit_events(client, run_id) == []


def test_mission_active_seconds_reads_the_timing_block_of_the_own_row():
    """The budget clock is the run row's ACTIVE seconds (gate waits are
    not the user's compute), read with the owner's visibility; a row
    without a timing block yields None (budget not judgeable), and so does
    a missing run id."""
    from inqtrix.agents import algorithm as algorithm_module

    rows: dict[str, dict[str, Any]] = {
        "run_x": {"timing": {"active_seconds": 12.5, "waiting_seconds": 300.0}}
    }
    calls: list[tuple[str, Any]] = []

    class Store:
        def get(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
            calls.append((run_id, visible_to))
            return rows[run_id]

    deps = SimpleNamespace(
        context=SimpleNamespace(run_id="run_x"),
        visible_to="owner",
        run_service=SimpleNamespace(run_store=Store()),
    )
    assert algorithm_module._mission_active_seconds(deps) == 12.5
    assert calls == [("run_x", "owner")]
    rows["run_x"] = {"status": "running"}
    assert algorithm_module._mission_active_seconds(deps) is None
    no_run = SimpleNamespace(
        context=SimpleNamespace(run_id=""), visible_to=None, run_service=None
    )
    assert algorithm_module._mission_active_seconds(no_run) is None


def test_critic_open_points_block_names_each_gap_once():
    """The closing block lists the uncovered success criteria once, in
    reader language, without the critic's finding details (checker
    vocabulary), and is empty when the critic left nothing open."""
    from inqtrix.agents import algorithm as algorithm_module

    critic = {
        "criteria_uncovered": [{"text": "Gegenposition belegt.", "kind": "information"}, {"text": " ", "kind": "information"}, {"text": "Gegenposition belegt.", "kind": "information"}],
        "findings": [
            {"kind": "criterion_unmet", "detail": "Quelle fehlt."},
            {"kind": "style", "detail": "Die Vorberechnung weist 30 Absaetze aus."},
        ],
    }
    block = algorithm_module._critic_open_points(critic, "# Memo\n\n## Fazit\n\nText.", "de")
    assert block.startswith("\n\n**Offene Punkte:**\n")
    assert "## " not in block
    assert block.count("Gegenposition belegt.") == 1
    assert "- Noch offen: Gegenposition belegt." in block
    assert "Quelle fehlt." not in block
    assert "Vorberechnung" not in block
    assert algorithm_module._critic_open_points(None) == ""
    assert algorithm_module._critic_open_points({"findings": [{"detail": "x"}]}) == ""


def test_critic_open_points_join_the_memo_s_own_closing_section():
    """Remaining gaps preserve the memo's existing section structure."""
    from inqtrix.agents import algorithm as algorithm_module

    critic = {"criteria_uncovered": [{"text": "Datierung belegt.", "kind": "information"}]}
    closing = "# Memo\n\n## Fazit\n\nText.\n\n## Offene Punkte\n\n- Eigener Punkt."
    block = algorithm_module._critic_open_points(critic, closing, "de")
    assert "## Offene Punkte" not in block
    assert block.startswith("\n\nDie Prüfung am Ende des Laufs")
    assert "- Noch offen: Datierung belegt." in block
    # Elsewhere, the note gets an inline label without another section.
    middle = "# Memo\n\n## Offene Punkte\n\n- a\n\n## Anhang\n\nText."
    addition = algorithm_module._critic_open_points(critic, middle, "de")
    assert "**Offene Punkte:**" in addition
    assert "## " not in addition
    assert algorithm_module._memo_ends_with_open_points("## OFFENE PUNKTE  \n\n- x", "de") is True
    assert algorithm_module._memo_ends_with_open_points("", "de") is False


def test_the_critic_decisions_are_visible_and_name_the_open_criteria(
    monkeypatch,
):
    """Every research demand is a visible decision that names the criteria
    it left open (so a reader and a parent's card learn WHY the mission
    keeps researching); how the last review ended is the outcome record's
    summary and its details, not a decision of its own. The memo's
    statements make the summary their count, so the review's own words
    stand in the details.

    Mutation: the generic research detail without the criteria, or a
    record without its details, fails this pin.
    """
    verdicts = iter(["research", "pass"])
    counter = {"n": 0}
    demanding = _research_demanding_critic(counter)

    def critic_script(prompt: str) -> dict[str, Any]:
        verdict = next(verdicts, "pass")
        return demanding(prompt) if verdict == "research" else review_verdict(prompt)

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: _additive_web_delta("t2"),
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        events = run_events(client, run_id)
        decisions = [
            e["data"]
            for e in events
            if e["type"] == "inqtrix.agent.activity"
            and str(e["data"].get("kind", "")).startswith("critic_")
        ]
        assert [d["kind"] for d in decisions] == ["critic_research"]
        assert "Alle Anbieter mit Quelle belegt." in decisions[0]["detail"]
        [record] = [e["data"] for e in events if e["type"] == "inqtrix.agent.outcome"]
        assert (record["review"], record["round"]) == ("completed", 2)
        assert (record["summary"].startswith("Geprüft · "), record["details"].startswith("ohne Befund")) == (
            True, True,
        )


def test_points_the_review_leaves_open_are_named_by_the_outcome_record(monkeypatch):
    """Nothing to fix and nothing to research, yet not everything settled:
    the record says so instead of reading as a clean pass. Only an open
    INFORMATION criterion closes the memo as a named gap; this one belongs
    to the text. The memo's statements make the summary their count; the
    open criterion stands in the record's details.

    Mutation: deriving the pass from the verdict's flags instead of from
    what it names fails this pin.
    """
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": lambda prompt: review_verdict(
            prompt, complete=True,
            unmet={1: ("output_requirement", "Die Anbieter sind nicht einzeln benannt.")},
        ),
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        events = run_events(client, run_id)
        decisions = [
            e["data"].get("kind")
            for e in events
            if e["type"] == "inqtrix.agent.activity"
            and str(e["data"].get("kind", "")).startswith("critic_")
        ]
        assert decisions == []
        [record] = [e["data"] for e in events if e["type"] == "inqtrix.agent.outcome"]
        assert record["details"].startswith("1 Kriterium offen")
        assert "structured:DeepRevisionBundle" not in llm.calls
        memo = _memo_body(client, run_id)
        assert "Noch offen:" not in memo
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        stored = next(item for item in artifacts if item["kind"] == "critic_report")
        report = client.get(
            f"/v1/runs/{run_id}/artifacts/{stored['artifact_id']}"
        ).json()["payload"]
        assert report["verdict"] == "open"
        assert report["criteria_uncovered"] == [
            {"index": 1, "text": "Alle Anbieter mit Quelle belegt.", "kind": "output_requirement",
             "gap": "Die Anbieter sind nicht einzeln benannt.", "state": "open"}
        ]
        assert report["open_lines"] == [{
            "kind": "output_requirement",
            "text": "Kriterium offen: Alle Anbieter mit Quelle belegt. — Die Anbieter sind nicht einzeln benannt.",
            "reason": "",
        }]


def test_no_web_plan_when_search_unavailable(monkeypatch):
    """Scenario 8: without web the planner is FORCED off web tasks."""
    rag_plan = {
        "summary_markdown": "Nur intern.",
        "tasks": [
            {
                "id": "t1",
                "title": "Interner Bestand",
                "tool_kind": "rag_query",
                "queries": ["Marktanalyse Bestand"],
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
    }

    plans = iter([dict(PLAN), rag_plan])

    def plan_script(prompt: str) -> dict[str, Any]:
        return next(plans)

    llm = ScriptedLLM(overrides={"ExecutionPlanModel": plan_script})
    monkeypatch.setattr(
        web_research_module, "run_web_graph", fake_child_graph
    )
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True
    )

    class NoSearch:
        def is_available(self) -> bool:
            return False

        def search(self, *a: Any, **k: Any) -> GroundedSearchResult:
            raise AssertionError("web must not be called")

    register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.llm = llm  # type: ignore[attr-defined]
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        # This harness has neither web nor a knowledge stack: the plan
        # is forced off web tasks (repair round), and with every
        # remaining task hard-failing the run ends LOUDLY instead of
        # synthesizing an evidence-free memo.
        summary = wait_status(client, run_id, {"failed"})
        assert "all_tasks_failed" in summary["error"]["message"]
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        kinds = [task["tool_kind"] for task in plan["tasks"]]
        assert "web_research" not in kinds and "web_instant" not in kinds
        synthesis_task = next(
            task for task in plan["tasks"] if task["tool_kind"] == "synthesis"
        )
        assert synthesis_task["status"] == "skipped"
        # The web plan was rejected via the repair round (2 attempts).
        assert llm.calls.count("structured:ExecutionPlanModel") == 2
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []


def test_contradictions_flow_into_critic_facts(monkeypatch):
    """Scenario 3: overlapping claims -> contradiction analysis -> the
    critic receives the contradiction count as a precomputed fact."""
    def child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "answer": f"Kindbericht: {question}",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "result_state": {
                "answer": "x",
                "round": 1,
                "report_references": [
                    {"label": "E1", "url": "https://example.com/a", "tier": "unknown"}
                ],
                "consolidated_claims": [
                    {
                        "claim_text": "Der Markt waechst stark",
                        "status": "verified",
                        "claim_type": "fact",
                        "needs_primary": False,
                        "status_reason": "",
                        "support_count": 2,
                        "contradict_count": 0,
                        "source_tier_counts": {},
                        "sources": ["https://example.com/a"],
                    },
                    {
                        "claim_text": "Der Markt waechst nicht stark",
                        "status": "contested",
                        "claim_type": "fact",
                        "needs_primary": False,
                        "status_reason": "",
                        "support_count": 1,
                        "contradict_count": 1,
                        "source_tier_counts": {},
                        "sources": ["https://example.com/b"],
                    },
                ],
            },
        }

    contradiction = {
        "contradictions": [
            {
                "internal_position": "Der Markt waechst stark",
                "external_position": "Der Markt waechst nicht stark",
                "severity": "hard",
                "likely_cause": "Unterschiedliche Zeitraeume",
            }
        ]
    }
    llm = ScriptedLLM(
        overrides={
            "ContradictionReport": contradiction,
            "ExecutionPlanModel": RESEARCH_PLAN,
        }
    )

    from inqtrix.strategies._claim_consolidation import DefaultClaimConsolidator
    from inqtrix.strategies._source_tiering import DefaultSourceTiering

    client = make_agent_client(
        monkeypatch,
        llm=llm,
        strategies=SimpleNamespace(claim_consolidation=DefaultClaimConsolidator(DefaultSourceTiering())),
    )
    monkeypatch.setattr(web_research_module, "run_web_graph", child_graph)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            tool_directives=["web_research"],
        )
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result  # completed with a payload
        # Contradiction analysis ran and its result reached result_state.
        assert "structured:ContradictionReport" in client.llm.calls
        assert client.llm.calls.index(
            "structured:ContradictionReport"
        ) < client.llm.calls.index("structured:ReportOutline")


def _finding_on(quote: str, **finding: Any):
    """A review that names one passage and is otherwise satisfied.

    By default a run diagnostic on the text measurements: its edit may
    reword without a source basis.
    """
    calls = {"n": 0}

    def review_script(prompt: str) -> dict[str, Any]:
        calls["n"] += 1
        return review_verdict(
            prompt, grounded=False, findings=[review_finding(prompt, quote, **{
                "kind": "run_diagnostic",
                "basis_ids": (text_measurements_basis(prompt),),
                **finding,
            })],
        )

    return calls, review_script


def _discard_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return [
        event["data"]
        for event in run_events(client, run_id)
        if event["type"] == "inqtrix.agent.activity"
        and event["data"].get("kind") == "critic_revision_discarded"
    ]


def _memo_body(client: TestClient, run_id: str) -> str:
    artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
    memo = next(a for a in artifacts if a["kind"] == "memo")
    return client.get(
        f"/v1/runs/{run_id}/artifacts/{memo['artifact_id']}"
    ).json()["content_markdown"]


def test_a_finding_replaces_its_passage_and_nothing_else(monkeypatch):
    """The revision is an anchored edit, not a rewrite: the named passage
    changes, every other character of the memo stays, and the revised text
    is not reviewed a second time; only its edit is re-checked."""
    review_calls, review_script = _finding_on("Die Marktlage ist stabil [W1].")
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": review_script,
        "DeepRevisionBundle": {"edits": [
            {"finding_id": "finding_1", "replacement": "Der Markt wächst moderat [W1]."},
        ]},
        "RevisionCheckVerdict": recheck_unquoted,
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed"})
        assert summary["status"] == "completed"
        assert _discard_events(client, run_id) == []
        assert review_calls["n"] == 1
        assert llm.calls.count("structured:DeepRevisionBundle") == 1
        # The writer wrote two sections and was not asked again.
        assert llm.calls.count("structured:SectionText") == 2
        assert _memo_body(client, run_id) == (
            "# Marktanalyse\n\n## Ergebnis\n\nDer Markt wächst moderat [W1]."
            "\n\n## Offene Punkte\n\nDie Marktlage ist stabil."
        )
        revision_prompt = next(
            prompt for schema, prompt in llm.prompts if schema == "DeepRevisionBundle"
        )
        assert "replaces ONLY its quote" in revision_prompt


def test_an_edit_citing_a_source_the_memo_does_not_have_is_discarded_visibly(monkeypatch):
    """An edit cites only its passage's labels and its basis's labels. A
    replacement with a foreign label names its finding: ONE visible
    activity in reader words, the memo as it was, no second review."""
    review_calls, review_script = _finding_on("Die Marktlage ist stabil [W1].")
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": review_script,
        "DeepRevisionBundle": {"edits": [
            {"finding_id": "finding_1", "replacement": "Der Markt wächst moderat [W9]."},
        ]},
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed"})
        assert summary["status"] == "completed"
        discarded = _discard_events(client, run_id)
        assert len(discarded) == 1
        # One name per language for the reviewing step: Prüfung / Review.
        assert discarded[0]["label"] == "Prüfung: Überarbeitung verworfen"
        assert discarded[0]["detail"] == (
            "Befund nicht angewendet (Ersetzung zitiert einen Beleg außerhalb "
            "dieses Befunds): Absatz genauer fassen."
        )
        assert review_calls["n"] == 1
        memo = _memo_body(client, run_id)
        assert "Die Marktlage ist stabil [W1]." in memo
        assert "[W9]" not in memo


def test_a_finding_that_cannot_be_anchored_leaves_the_memo_and_says_so(monkeypatch):
    """A quote that is not in the text anchors nothing: no revision call is
    spent, the decision is visible, the memo stands."""
    review_calls, review_script = _finding_on("Diesen Satz gibt es im Memo nicht.")
    llm = ScriptedLLM(overrides={"DeepReviewVerdict": review_script})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        discarded = _discard_events(client, run_id)
        assert len(discarded) == 1
        assert "(zitierte Stelle steht nicht im Text)" in discarded[0]["detail"]
        assert "structured:DeepRevisionBundle" not in llm.calls
        assert "Die Marktlage ist stabil [W1]." in _memo_body(client, run_id)


def test_a_discarded_chat_revision_is_visible_and_the_answer_stands(monkeypatch):
    """The chat deliverable shares the rule: a revision that changes nothing
    is one visible activity, the answer stands, no second review."""
    review_calls, review_script = _finding_on("Der Markt waechst [W1].")
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": {**PROFILE, "response_form": "chat"},
            "DeepReviewVerdict": review_script,
            "SectionText": {"markdown": "Direkte Antwort: Der Markt waechst [W1]."},
            "DeepRevisionBundle": {"edits": [
                {"finding_id": "finding_1", "replacement": "Der Markt waechst [W1]."},
            ]},
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed"})
        assert summary["status"] == "completed"
        discarded = _discard_events(client, run_id)
        assert len(discarded) == 1
        assert "(Überarbeitung ließ die Stelle unverändert)" in discarded[0]["detail"]
        assert review_calls["n"] == 1
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == "Direkte Antwort: Der Markt waechst [W1]."


def test_edit_decision_reloads_user_plan_before_execution(monkeypatch):
    """The edited plan (user version) is what executes."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        edited_plan = {
            "summary_markdown": "Vom Nutzer verschlankt.",
            "tasks": [
                {
                    "id": "u1",
                    "title": "Nur Instant-Suche",
                    "tool_kind": "web_instant",
                    "queries": ["Marktvolumen"],
                },
                {
                    "id": "s",
                    "title": "Synthese",
                    "tool_kind": "synthesis",
                    "depends_on": ["u1"],
                },
            ],
        }
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "edit", "plan": edited_plan},
        )
        assert decided.status_code == 200, decided.text
        wait_status(client, run_id, {"completed"})
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["created_by"] == "user"
        assert [task["task_id"] for task in plan["tasks"]] == ["u1", "s"]
        # The USER plan executed: instant task only, no children.
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []


def test_user_research_edit_consent_survives_critic_replan(monkeypatch):
    """A later agent-authored version cannot erase explicit user consent."""
    critic_calls = {"count": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["count"] += 1
        if critic_calls["count"] == 1:
            return review_verdict(prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")})
        return review_verdict(prompt)

    delta = replan_delta(
        {
            "id": "t2",
            "title": "Gegenposition vertiefen",
            "tool_kind": "web_research",
            "queries": [
                "Welche belastbaren Gegenbelege widersprechen der "
                "Markteinschaetzung 2026?"
            ],
            "params": {"profile": "compact", "recency": "year"},
        },
        summary="Zweiter kompakter Rechercheauftrag.",
    )
    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": delta,
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)

    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approval = client.get(
            f"/v1/runs/{run_id}/approvals"
        ).json()["data"][0]
        edited = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
            json={"decision": "edit", "plan": RESEARCH_PLAN},
        )
        assert edited.status_code == 200, edited.text

        # The critic's agent-authored research replan still re-gates its new
        # web question in balanced mode, but policy validation must retain the
        # user's earlier compact-research consent.
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})

        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 3
        assert plan["created_by"] == "agent"
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 2
        assert {
            child["agent_overrides"]["report_profile"]
            for child in children
        } == {"compact"}
        assert client.llm.calls.count("structured:ExecutionPlanModel") == 1
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1


def test_a_submitted_requirement_shapes_a_run_that_never_gates(monkeypatch):
    """Three mission paths never reach a plan gate — autonomous, the
    speed tier and delegated children. Before the submit-time entry point
    they had NO way to state how the result has to look."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            report_guidance="Gliedere als Sprechzettel fuer die GF.",
        )
        wait_status(client, run_id, {"completed"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        assert not [a for a in approvals if a["kind"] in ("plan", "replan")], (
            "autonomous must not gate — otherwise this proves nothing"
        )
        synthesis_prompts = [
            prompt
            for name, prompt in client.llm.prompts
            if name in ("ReportOutline", "SectionText")
        ]
        assert synthesis_prompts, "synthesis never ran"
        assert any(
            "Output requirement" in prompt
            and "[Free requirement]" in prompt
            and "Sprechzettel" in prompt
            for prompt in synthesis_prompts
        )


def test_the_plan_gate_replaces_a_submitted_requirement(monkeypatch):
    """The gate is the later word: a user who refines the requirement
    there must not end up with both texts fighting in the prompt."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(client, report_guidance="Als Fliesstext.")
        wait_status(client, run_id, {"waiting_for_approval"})
        pending = [
            a
            for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ]
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve", "report_guidance": "Als Tabelle."},
        )
        assert decided.status_code == 200
        wait_status(client, run_id, {"completed"})
        synthesis_prompts = [
            prompt
            for name, prompt in client.llm.prompts
            if name in ("ReportOutline", "SectionText")
        ]
        assert synthesis_prompts, "synthesis never ran"
        assert any("Als Tabelle." in prompt for prompt in synthesis_prompts)
        assert not any(
            "Als Fliesstext." in prompt for prompt in synthesis_prompts
        )


def test_an_approval_that_says_nothing_keeps_the_submitted_requirement(
    monkeypatch,
):
    """Presence, not truthiness. Approving a plan without touching the
    requirement field means "the plan is fine", not "drop my
    requirement"."""
    client = make_agent_client(monkeypatch)
    with client:
        run_id = submit_agent(
            client, report_guidance="Gliedere als Sprechzettel fuer die GF."
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        pending = [
            a
            for a in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if a["status"] == "pending"
        ]
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200
        wait_status(client, run_id, {"completed"})
        synthesis_prompts = [
            prompt
            for name, prompt in client.llm.prompts
            if name in ("ReportOutline", "SectionText")
        ]
        assert synthesis_prompts, "synthesis never ran"
        assert any("Sprechzettel" in prompt for prompt in synthesis_prompts)


def test_a_requirement_on_a_non_agent_run_is_a_loud_400(monkeypatch):
    """Only the agent engines render it. A research run that silently
    accepted the field would drop it without a word."""
    client = make_agent_client(monkeypatch)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Marktlage?",
                "report_guidance": "Als Tabelle.",
            },
        )
        assert response.status_code == 400
        assert "report_guidance" in response.text


def test_a_requirement_over_the_limit_is_refused_not_shortened(monkeypatch):
    """No silent caps: a text the composer would have to shorten is
    refused with its limit named."""
    client = make_agent_client(monkeypatch)
    with client:
        body = submit_agent(
            client,
            report_guidance="x" * 2001,
            expect_status=400,
        )
        assert "2000" in body


def test_the_composed_ceiling_is_published_from_the_enforced_constant(
    monkeypatch,
):
    """Published == enforced: the composer pre-checks attached rules
    against THIS number, so it must be the constant the resolver refuses
    at, read where the manifest is built — never a second literal."""
    from inqtrix.agents import report_requirement
    from inqtrix.server.routers import capabilities as capabilities_router

    client = make_agent_client(monkeypatch)
    with client:
        published = client.get("/v1/capabilities").json()["agent"][
            "report_requirement"
        ]
        assert published["max_composed_chars"] == 20_000
        assert (
            published["max_composed_chars"]
            == report_requirement.MAX_COMPOSED_REQUIREMENT_CHARS
        )
        monkeypatch.setattr(
            capabilities_router, "MAX_COMPOSED_REQUIREMENT_CHARS", 12_345
        )
        assert (
            client.get("/v1/capabilities").json()["agent"][
                "report_requirement"
            ]["max_composed_chars"]
            == 12_345
        )


def test_an_unknown_rule_refuses_the_submission(monkeypatch):
    """An id the caller cannot see fails loudly: starting the run without
    it would work under requirements the user believes are in force."""
    client = make_agent_client(monkeypatch)
    with client:
        body = submit_agent(
            client,
            report_rule_ids=["template-does-not-exist"],
            expect_status=400,
        )
        assert "template-does-not-exist" in body


def test_a_delegated_child_does_not_inherit_the_requirement(monkeypatch):
    """The requirement describes the RESULT the user sees. A research
    child produces evidence for the parent, not the deliverable — giving
    it the parent's report format would shape the wrong artifact. The
    parent's state carries a requirement here; the child submit must not
    pass it on."""
    from inqtrix.agents import algorithm as algorithm_module
    from inqtrix.agents.control_ports import PlanTaskRecord

    submitted: list[dict[str, Any]] = []

    class _RecordingRunService:
        run_store = SimpleNamespace(
            list=lambda **kwargs: [], get=lambda *a, **k: {}
        )

        def submit(self, **kwargs: Any) -> dict[str, Any]:
            submitted.append(kwargs)
            return {"run_id": "run_child"}

    deps = SimpleNamespace(
        run_service=_RecordingRunService(),
        resolver=SimpleNamespace(
            resolve=lambda payload: SimpleNamespace(
                mode="research", stack_name="default"
            )
        ),
        context=SimpleNamespace(
            workspace_id=None,
            principal=None,
            run_id="run_parent",
            stack_name="",
        ),
        source_policy=SimpleNamespace(web="allowed", knowledge="allowed"),
        emit=lambda *a, **k: None,
        visible_to=None,
        run_id="run_parent",
        # The child submit reads the mission's Stufe (children inherit it);
        # this stand-in runs without one, like a legacy depth request.
        tier="",
    )
    monkeypatch.setattr(
        algorithm_module, "require_task_allowed", lambda *a, **k: None
    )
    monkeypatch.setattr(
        algorithm_module, "coerce_source_policy", lambda _deps: None
    )
    monkeypatch.setattr(
        algorithm_module, "_existing_child_for_attempt", lambda *a, **k: None
    )
    # A REAL record, not a stand-in: a stub would silently drift from
    # the fields the child submit actually reads.
    task = PlanTaskRecord(
        task_id="t1",
        plan_id="plan_v1",
        run_id="run_parent",
        ordinal=0,
        title="Marktlage recherchieren",
        tool_kind="web_research",
        objective="Marktzahlen 2026 belegen",
        queries=["Marktlage 2026"],
    )
    try:
        algorithm_module._submit_child_run(
            deps,
            {
                "run_id": "run_parent",
                "report_guidance": "Gliedere als Sprechzettel fuer die GF.",
                "session_id": None,
            },
            task,
            1,
            research_policy=SimpleNamespace(
                allowed=True, profile="standard", max_profile="standard"
            ),
        )
    except Exception:  # noqa: BLE001 - the stub ends after the submit
        pass
    assert submitted, "the child was never submitted — proves nothing"
    assert not submitted[0].get("report_requirement")


def _oversized_delta(prompt: str) -> dict[str, Any]:
    """Eight new tasks on top of the kept plan: always over the ceiling."""
    del prompt
    return replan_delta(
        *[
            {
                "id": f"n{i}",
                "title": f"Zusatz {i}",
                "tool_kind": "web_instant",
                "queries": [f"Zusatzfrage {i}?"],
            }
            for i in range(1, 9)
        ]
    )


def _replan_invalid_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return [
        event["data"]
        for event in run_events(client, run_id)
        if event["type"] == "inqtrix.agent.activity"
        and event["data"].get("kind") == "replan_invalid"
    ]


def test_discarded_gate_replan_continues_with_the_previous_plan(monkeypatch):
    """The replan gate asks for more evidence, the planner twice returns a
    delta over the ceiling: the run does not die as plan_invalid with all
    gathered evidence. It discards the round visibly and synthesizes from
    the evidence in hand; the prompt names the ceiling and headroom."""
    sufficiency = iter([{"coverage": "uncovered", "missing": ["Marktlage"]}])

    def sufficiency_script(prompt: str) -> dict[str, Any]:
        return next(sufficiency, dict(SUFFICIENCY))

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": _oversized_delta,
            "SufficiencyJudgement": sufficiency_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm, search=NoEvidenceSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert not summary.get("error")
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 1
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 2
        events = _replan_invalid_events(client, run_id)
        assert len(events) == 1
        assert events[0]["label"] == "Replan verworfen, weiter mit dem letzten Plan"
        # The reader sees the label; the planner's own errors stay machine data.
        assert "detail" not in events[0]
        assert "Too many tasks (10, allowed are 8)" in " ".join(events[0]["errors"])
        assert _memo_body(client, run_id)
        replan_prompt = next(
            prompt
            for schema, prompt in client.llm.prompts
            if schema == "ReplanDeltaModel"
        )
        assert "at most 8 tasks" in replan_prompt
        assert "room for at most 6 new tasks" in replan_prompt
        # Ten structured calls at 7/5 tokens each — INCLUDING the two
        # failed planner attempts, which are spend like any other call.
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert len(client.llm.calls) == 10
        assert result["usage"] == {
            "prompt_tokens": 70,
            "completion_tokens": 50,
            "total_tokens": 120,
            # The mission keeps a cumulative count, so this is the run.
            "scope": "run",
        }


def test_discarded_critic_replan_keeps_the_memo(monkeypatch):
    """On the critic route a discarded replan keeps the memo the critic
    judged: no second synthesis, no second critic pass, one visible row."""
    critic_calls = {"n": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["n"] += 1
        if critic_calls["n"] == 1:
            return review_verdict(prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")})
        return review_verdict(prompt)

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": _oversized_delta,
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 2
        assert client.llm.calls.count("structured:ReportOutline") == 1
        assert critic_calls["n"] == 1
        assert client.get(f"/v1/runs/{run_id}/plan").json()["version"] == 1
        assert len(_replan_invalid_events(client, run_id)) == 1
        assert "Die Marktlage ist stabil" in _memo_body(client, run_id)


def test_child_needs_review_reaches_synthesis_review_and_the_run_stream(monkeypatch):
    """A research child whose evidence audit capped its confidence used to
    vanish silently: memo, review and run stream never mentioned it. The
    note now rides the section prompts, the review's run diagnostics and one
    activity — the same wording on every surface."""
    def child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        result["result_state"].update(
            {
                "final_confidence": 6,
                "evidence_contract_status": "needs_review",
                "evidence_depth_gap": {
                    "active": True,
                    "single_source_ratio": 0.93,
                    "supported_count": 14,
                    "single_source_count": 13,
                    "cross_checked_count": 1,
                },
            }
        )
        return result

    llm = ScriptedLLM(overrides={"ExecutionPlanModel": dict(RESEARCH_PLAN)})
    client = make_agent_client(monkeypatch, llm=llm)
    monkeypatch.setattr(web_research_module, "run_web_graph", child_graph)
    with client:
        run_id = submit_agent(client, autonomy="autonomous", tool_directives=["web_research"])
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        reviews = [
            e["data"] for e in run_events(client, run_id)
            if e["type"] == "inqtrix.agent.activity"
            and e["data"].get("operation") == "task.evidence_needs_review"
        ]
        assert len(reviews) == 1
        # ONE reader wording on every surface: the activity row, every
        # section prompt and the review's diagnostics carry the same note —
        # naming the sub-task, never a task id or a status code.
        note = reviews[0]["detail"]
        assert note.startswith("Teilaufgabe „")
        assert "nur einfach belegt" in note
        assert "93 % der Belege aus je einer Quelle" in note
        assert "Konfidenz 6/10" in note
        assert "needs_review" not in note and "Task t1" not in note
        section_prompts = [p for s_, p in client.llm.prompts if s_ == "SectionText"]
        assert section_prompts and all(note in p for p in section_prompts)
        # The review reads it as a run diagnostic it can name as a basis.
        review_prompt = next(p for s_, p in client.llm.prompts if s_ == "DeepReviewVerdict")
        assert json.dumps(note, ensure_ascii=False)[1:-1] in review_prompt
        assert f"diagnostic:{run_id}:task_evidence_notes" in review_prompt
        task = client.get(f"/v1/runs/{run_id}/tasks/t1/result").json()
        assert task["metrics"]["evidence_contract_status"] == "needs_review"
        assert task["metrics"]["confidence"] == 6


def test_mission_result_usage_sums_parked_segments(monkeypatch):
    """The mission publishes its run total too: after the plan-approval
    park the result's usage equals the token counter of the execution
    limits, not the resumed segment's delta."""
    client = make_agent_client(monkeypatch, max_tokens_per_run=10**6)
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        used = result["execution"]["limits"]["tokens"]["used"]
        assert result["usage"]["total_tokens"] == used
        assert result["metrics"]["prompt_tokens"] == result["usage"]["prompt_tokens"]
        # intake + discovery + plan ran BEFORE the park (7 prompt tokens
        # each in the scripted provider) — they are part of the total.
        assert result["usage"]["prompt_tokens"] >= 3 * 7


def test_empty_critic_replan_keeps_the_memo_instead_of_resynthesizing(monkeypatch):
    """The critic asked for research, the plan had no room and the planner
    answered with an empty delta: the memo the critic judged stands, the
    gap is one visible activity — the ten memo sections are not rewritten
    from unchanged evidence."""
    critic_calls = {"n": 0}

    def critic_script(prompt: str) -> dict[str, Any]:
        critic_calls["n"] += 1
        if critic_calls["n"] == 1:
            return review_verdict(prompt, unmet={1: ("information", "Eine belastbare Gegenposition fehlt.")})
        return review_verdict(prompt)

    llm = ScriptedLLM(
        overrides={
            "ReplanDeltaModel": lambda prompt: replan_delta(),
            "DeepReviewVerdict": critic_script,
        }
    )
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert client.llm.calls.count("structured:ReplanDeltaModel") == 1
        assert client.llm.calls.count("structured:ReportOutline") == 1
        assert critic_calls["n"] == 1
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 2
        assert plan["status"] == "approved"
        exhausted = [
            e["data"] for e in run_events(client, run_id)
            if e["type"] == "inqtrix.agent.activity"
            and e["data"].get("kind") == "critic_research_exhausted"
        ]
        assert len(exhausted) == 1
        assert "keine neuen Aufgaben" in exhausted[0]["detail"]
        assert "Die Marktlage ist stabil" in _memo_body(client, run_id)


def _narration_ids(client: TestClient, run_id: str) -> list[str]:
    events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
    return [
        str(event["data"].get("narration_id") or "")
        for event in events
        if event["type"] == "inqtrix.agent.narration"
    ]


def test_memo_sections_are_written_concurrently_and_folded_in_outline_order(
    monkeypatch,
):
    """The two outline sections are written at the same time — a
    barrier both must reach proves the overlap — while the memo is
    assembled and narrated in outline order (section 0 before 1)."""
    import threading

    barrier = threading.Barrier(len(OUTLINE["sections"]), timeout=10)
    broken: list[bool] = []

    def concurrent_section(prompt: str) -> dict[str, str]:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            broken.append(True)
        return _section_for_prompt(prompt)

    assert AgentPlatformSettings().synthesis_max_parallel_sections == 8
    # A Tief wave of up to eight research children x ~20 references must
    # reach the outline in full (a measured mission collected 116).
    assert AgentPlatformSettings().synthesis_evidence_budget == 160
    llm = ScriptedLLM(overrides={"SectionText": concurrent_section})
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        assert broken == [], "sections were written one after another"
        assert llm.calls.count("structured:SectionText") == 2
        ids = _narration_ids(client, run_id)
        assert ids.index("n-section-0") < ids.index("n-section-1")
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"].index("## Ergebnis") < result["answer"].index(
            "## Offene Punkte"
        )


def test_memo_sections_run_sequentially_when_the_cap_is_one(monkeypatch):
    """``INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS=1`` restores the
    sequential loop: never two sections in flight, same outline order."""
    import threading
    import time as _time

    lock = threading.Lock()
    in_flight = {"now": 0, "peak": 0}

    def counted_section(prompt: str) -> dict[str, str]:
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        _time.sleep(0.05)
        with lock:
            in_flight["now"] -= 1
        return _section_for_prompt(prompt)

    llm = ScriptedLLM(overrides={"SectionText": counted_section})
    client = make_agent_client(
        monkeypatch,
        llm=llm,
        platform=AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_VOLATILE=True,
            INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS=1,
        ),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        assert in_flight["peak"] == 1
        assert llm.calls.count("structured:SectionText") == 2
        ids = _narration_ids(client, run_id)
        assert ids.index("n-section-0") < ids.index("n-section-1")
