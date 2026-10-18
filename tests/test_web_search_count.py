"""How many web searches a run ran, counted where the provider ran them.

The budget counter (``tool_use_counts["web"]``) takes a delegated research
as one web use and a planned query as a search; the run line read it as a
search count and showed seven searches where six ran. A run now reports
the searches it ran itself (``web_searches``) and each research child the
searches its provider executed (``executed_queries``); the desk adds them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import ToolMessage

from inqtrix.agents.kernel import tools as kernel_tools
from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.source_policy import SourcePolicy, execution_payload
from inqtrix.runs.shared import build_child_progress_payload
from inqtrix.state import build_run_snapshot


def test_a_research_run_names_the_searches_its_provider_ran():
    """Planned queries are not searches: a deadline can stop a round before
    its provider ran them. Every executed search left a query record."""
    snapshot = build_run_snapshot(
        {"queries": ["a", "b", "c"], "query_records": [{"query_id": "q1"}, {"query_id": "q2"}]},
        current_node="search",
        last_message="",
    )
    # A run that records no searches (a mission, a role) names none, so a
    # parent cannot take it for a research that ran zero searches.
    agent = build_run_snapshot({"done": True}, current_node="agent", last_message="")
    assert (snapshot["total_queries"], snapshot["executed_queries"], "executed_queries" in agent) == (
        3, 2, False,
    )


def test_the_parent_learns_the_searches_its_research_child_ran():
    projected = build_child_progress_payload(
        child_run_id="run_child",
        parent_task_id="call_abc",
        run_status="running",
        event_type="inqtrix.node.finished",
        payload={"node": "search"},
        snapshot={"total_queries": 3, "executed_queries": 2},
        attempt=1,
    )
    assert projected["snapshot"]["executed_queries"] == 2


def test_the_execution_block_carries_the_searches_the_run_ran_itself():
    payload = execution_payload(
        effective_mode="agent_kernel",
        response_form="chat",
        depth="normal",
        model="m",
        reasoning_effort="",
        source_policy=SourcePolicy(),
        consent_reason="permission_policy",
        tool_use_counts={"web": 2, "knowledge": 0},
        web_searches=1,
    )
    assert (payload["tool_use_counts"]["web"], payload["web_searches"]) == (2, 1)


def _deps() -> KernelDeps:
    return KernelDeps(
        run_id="run_ws", control=None, platform=None, llm=None,
        model=None, reasoning_effort=None, timeout=1.0,
    )


def test_an_instant_search_counts_and_a_delegation_does_not():
    """The budget takes a delegation as one web use; the provider ran no
    search for the parent there, its child counts its own."""

    class _Registry:
        async def invoke(self, capability_id, payload, context):
            return SimpleNamespace(answer="", sources=[], prompt_tokens=0, completion_tokens=0)

    deps = _deps()
    deps.capability_registry = _Registry()
    set_kernel_deps(deps)
    try:
        kernel_tools._invoke_capability_inner("web.search.instant", {"query": "Frist"})
        deps.record_source_tool_use("web")  # what a delegation spends of the budget
    finally:
        set_kernel_deps(None)
    assert (deps.web_searches, deps.tool_use_counts["web"]) == (1, 2)


def test_a_resumed_kernel_segment_recounts_its_searches_from_the_checkpoint():
    """A park ends the segment; the count comes back from the checkpointed
    tool messages, success only, instant searches only."""
    from inqtrix.agents.kernel.algorithm import _checkpointed_web_searches

    messages = [
        ToolMessage(content="Antwort [W1]", name="web_instant", tool_call_id="c1"),
        ToolMessage(content="Provider explodierte", name="web_instant", tool_call_id="c2", status="error"),
        ToolMessage(
            content="Tool blocked: The 'schnell' tier allows at most 1 web search(es) per run.",
            name="web_instant", tool_call_id="c3",
        ),
        ToolMessage(content="Bericht [W2]", name="run_web_research", tool_call_id="c4"),
        ToolMessage(content="Antwort [W3]", name="web_instant", tool_call_id="c5"),
    ]
    assert _checkpointed_web_searches(SimpleNamespace(values={"messages": messages})) == 2


def test_a_mission_counts_the_instant_searches_it_ran():
    """A web task runs its one search; a research task is a delegation
    whose child counts its own; discovery probes are searches."""
    from inqtrix.agents.algorithm import _mission_web_searches

    tasks = [
        SimpleNamespace(task_id="t1", tool_kind="web_instant", queries=["a"]),
        SimpleNamespace(task_id="t2", tool_kind="web_instant", queries=["c"]),
        SimpleNamespace(task_id="t3", tool_kind="web_research", queries=["d", "e"]),
        SimpleNamespace(task_id="t4", tool_kind="rag_query", queries=["f"]),
    ]
    outcomes = {
        "t1": SimpleNamespace(status="completed"),
        "t2": SimpleNamespace(status="failed"),
        "t3": SimpleNamespace(status="completed"),
        "t4": SimpleNamespace(status="completed"),
    }
    assert _mission_web_searches(tasks, outcomes, base_counts={"web": 2, "knowledge": 1}) == 3


def test_a_kernel_run_counts_its_searches_across_its_parks():
    """Each search waits for its approval, so the second one runs in a later
    segment: the count that segment reports comes back from the checkpoint,
    and it is the number of searches the provider ran."""
    from tests.agents.test_kernel_read_tools_policy import (
        RecordingSearch,
        ScriptedToolLLM,
        _pending_tool_approval,
        _submit,
        _text_turn,
        _tool_turn,
        make_client,
        wait_status,
    )

    llm = ScriptedToolLLM([
        _tool_turn("call_w1", "web_instant", {"query": "Frage eins"}),
        _tool_turn("call_w2", "web_instant", {"query": "Frage zwei"}),
        _text_turn("Antwort."),
    ])
    search = RecordingSearch()
    client = make_client(llm, search)
    with client:
        run_id = _submit(client, autonomy="balanced")
        for _ in range(2):
            wait_status(client, run_id, {"waiting_for_approval"})
            approval = _pending_tool_approval(client, run_id)
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                json={"decision": "approve"},
            )
            assert decided.status_code == 200, decided.text
        summary = wait_status(client, run_id, {"completed"})
    execution = summary["snapshot"]["execution"]
    assert (len(search.queries), execution["web_searches"]) == (2, 2)


def test_a_mission_reports_the_searches_its_provider_ran(monkeypatch):
    """The provider's own record is the oracle: the web task's search and
    discovery's web probe count, and nothing else does."""
    from tests.agents.test_workspace_agent import (
        FakeSearch,
        ScriptedLLM,
        make_agent_client,
        submit_agent,
        wait_status,
    )

    plan = {
        "summary_markdown": "Ein Instant-Task.",
        "tasks": [
            {
                "id": "t1",
                "title": "Fakt nachschlagen",
                "tool_kind": "web_instant",
                "objective": "Aktuelles Marktvolumen kritisch pruefen.",
                "queries": ["Welches belastbare Marktvolumen wird fuer 2026 berichtet?"],
                "expected_output": "Eine belegte Kurzantwort mit Risiken.",
            },
            {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": ["t1"]},
        ],
    }
    search = FakeSearch()
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(overrides={"ExecutionPlanModel": plan}), search=search)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed"})
    executed = summary["snapshot"]["execution"]["web_searches"]
    assert (executed, executed >= 1) == (len(search.queries), True)
