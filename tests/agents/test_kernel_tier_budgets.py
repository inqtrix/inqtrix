"""Tier source budgets at the kernel tool chokepoint (published == enforced)."""

from __future__ import annotations

import pytest

from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.exceptions import AgentPolicyDenied
from inqtrix.settings import AgentPlatformSettings


def _deps(
    tier: str, *, knowledge_searches: int = 0, web_searches: int = 0, **counts: int
) -> KernelDeps:
    """Deps of one run: its searches so far, and its source uses (*counts*)."""
    deps = KernelDeps(
        run_id="run_tier_budget",
        control=None,  # type: ignore[arg-type]
        platform=AgentPlatformSettings(),
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    deps.tier = tier
    deps.knowledge_searches = knowledge_searches
    deps.web_searches = web_searches
    deps.tool_use_counts.update(counts)
    return deps


def test_schnell_denies_the_second_knowledge_search_with_the_published_budget():
    """A step ceiling bounds turns, not the searches a tier promises: without
    a cap of its own a schnell run spends its turns on repeated searches. The
    tier table's knowledge budget is enforced here, like the web budget, and
    the model is told to answer from what it has."""
    _deps("schnell").require_tool_allowed("search_project_knowledge")
    with pytest.raises(AgentPolicyDenied) as denied:
        _deps("schnell", knowledge_searches=1).require_tool_allowed("search_project_knowledge")
    assert "at most 1 project-knowledge search" in str(denied.value)
    assert "answer from the results you already have" in str(denied.value)


def test_web_budget_reads_the_same_table():
    _deps("schnell").require_tool_allowed("web_instant")
    with pytest.raises(AgentPolicyDenied):
        _deps("schnell", web_searches=1).require_tool_allowed("web_instant")


def test_uncapped_tiers_and_tier_less_runs_keep_searching():
    for tier in ("gruendlich", "tief", ""):
        spent = {"knowledge_searches": 5, "web_searches": 5}
        _deps(tier, **spent).require_tool_allowed("search_project_knowledge")
        _deps(tier, **spent).require_tool_allowed("web_instant")


# -- a search cap counts searches, through the real tools ------------------- #


def _knowledge_world():
    """The real knowledge tools over a stored document, in the schnell tier."""
    pytest.importorskip("langchain")
    from tests.agents.test_kernel_cite_passages import _setup

    service, document, deps, tools = _setup()
    deps.tier = "schnell"
    return service, document, deps, tools


def _invoke(tools, name: str, arguments: dict, call_id: str):
    from tests.agents.test_source_coverage import _call

    return tools[name].invoke(_call(name, arguments, call_id))


def test_reading_a_document_spends_no_search_budget():
    """The schnell tier caps the searches of the knowledge base at one. A
    window read opens a document, it searches nothing: after reads the one
    search still runs, the second search is denied with the published budget,
    and the published counter of source uses counts reads and searches alike."""
    _service, document, deps, tools = _knowledge_world()
    for call_id in ("call_read_1", "call_read_2"):
        read = _invoke(tools, "read_project_document", {"document_id": document.id}, call_id)
        assert not str(read.content).startswith("Tool blocked"), read.content

    first = _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_1")
    assert not str(first.content).startswith("Tool blocked"), first.content
    second = _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_2")
    assert "at most 1 project-knowledge search" in str(second.content)

    assert deps.knowledge_searches == 1
    assert deps.tool_use_counts["knowledge"] == 3


def test_the_search_count_after_a_resume_equals_the_live_count():
    """A resumed segment recounts from the checkpointed tool messages. The
    recount follows the live rule: one per search that succeeded, nothing for
    a read, a citation or a search that was denied."""
    from types import SimpleNamespace

    from inqtrix.agents.context_sources import SOURCE_COVERAGE
    from inqtrix.agents.kernel.algorithm import _checkpointed_knowledge_searches
    from tests.agents.test_kernel_cite_passages import QUOTE, _cite

    _service, document, deps, tools = _knowledge_world()
    read = _invoke(tools, "read_project_document", {"document_id": document.id}, "call_read")
    (window,) = read.artifact[SOURCE_COVERAGE]
    start, end = window["read_ranges"][0]
    messages = [
        read,
        _cite(tools, document.id, window["version"], start, end, [QUOTE]),
        _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_1"),
        _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_2"),
    ]
    assert not str(messages[1].content).startswith("Tool")
    assert str(messages[3].content).startswith("Tool blocked")
    snapshot = SimpleNamespace(values={"messages": messages})
    assert _checkpointed_knowledge_searches(snapshot) == deps.knowledge_searches == 1


def test_a_search_that_failed_spends_no_search_budget():
    """The cap counts searches that ran. A search the knowledge store did
    not answer returned nothing to work with: it leaves the one search of
    the tier unspent, live and after a resume alike, so the next search
    still runs."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import _checkpointed_knowledge_searches
    from inqtrix.capabilities import CapabilityError

    _service, _document, deps, tools = _knowledge_world()
    registry = deps.capability_registry
    answered = registry.invoke
    failures = iter([CapabilityError("knowledge.store_unavailable", "The store did not respond.")])

    async def once_unavailable(capability_id, payload, context):
        if capability_id == "knowledge.search" and (failure := next(failures, None)):
            raise failure
        return await answered(capability_id, payload, context)

    deps.capability_registry = SimpleNamespace(invoke=once_unavailable)
    failed = _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_1")
    assert "knowledge store did not respond" in str(failed.content)
    assert (deps.knowledge_searches, deps.tool_use_counts.get("knowledge", 0)) == (0, 0)

    second = _invoke(tools, "search_project_knowledge", {"query": "review step"}, "call_search_2")
    assert not str(second.content).startswith("Tool"), second.content
    snapshot = SimpleNamespace(values={"messages": [failed, second]})
    assert _checkpointed_knowledge_searches(snapshot) == deps.knowledge_searches == 1


def test_a_source_use_that_is_no_search_spends_no_search_budget():
    """Each cap counts the successful calls of its own search tool, not every
    use of the source the tool belongs to."""
    _deps("schnell", knowledge=3, web=2).require_tool_allowed("search_project_knowledge")
    _deps("schnell", knowledge=3, web=2).require_tool_allowed("web_instant")


def test_a_resumed_run_still_knows_the_search_it_spent():
    """Each search waits for its approval, so the second one is decided in a
    later segment. That segment rebuilds its counters from the checkpoint:
    the search the run already spent still counts, and the second search is
    denied instead of run."""
    pytest.importorskip("langchain")
    from pydantic import BaseModel, Field

    from inqtrix.capabilities.contracts import CapabilityContext, CapabilityDefinition, Effect
    from tests.agents.test_kernel_read_tools_policy import (
        RecordingSearch,
        ScriptedToolLLM,
        _pending_tool_approval,
        _text_turn,
        _tool_turn,
        make_client,
        wait_status,
    )

    class _Input(BaseModel):
        query: str
        collection_ids: list[str] = Field(default_factory=list)
        top_k: int = 8

    class _Output(BaseModel):
        hits: list[dict] = Field(default_factory=list)

    searched: list[str] = []

    async def _handler(payload: BaseModel, context: CapabilityContext) -> BaseModel:
        del context
        searched.append(payload.query)  # type: ignore[attr-defined]
        return _Output()

    llm = ScriptedToolLLM([
        _tool_turn("call_k1", "search_project_knowledge", {"query": "Frage eins"}),
        _tool_turn("call_k2", "search_project_knowledge", {"query": "Frage zwei"}),
        _text_turn("Antwort."),
    ])
    client = make_client(llm, RecordingSearch())
    with client:
        client.container.capability_registry.register(  # type: ignore[attr-defined]
            CapabilityDefinition(
                id="knowledge.search", summary="Search project knowledge.",
                input_model=_Input, output_model=_Output, effect=Effect.READ,
                idempotent=True, handler=_handler,
            )
        )
        response = client.post("/v1/runs", json={
            "question": "Was steht in den Unterlagen?", "mode": "agent_kernel",
            "autonomy": "balanced", "agent_overrides": {"agent_tier": "schnell"},
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        for _ in range(2):
            wait_status(client, run_id, {"waiting_for_approval"})
            approval = _pending_tool_approval(client, run_id)
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                json={"decision": "approve"},
            )
            assert decided.status_code == 200, decided.text
        wait_status(client, run_id, {"completed"})
    replies = [
        str(message.get("content"))
        for message in llm.chat_calls[-1]["messages"] if message.get("role") == "tool"
    ]
    assert searched == ["Frage eins"]
    assert len(replies) == 2 and replies[1].startswith("Tool blocked:"), replies
    assert "at most 1 project-knowledge search" in replies[1]
