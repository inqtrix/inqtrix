"""How many searches of the knowledge base a run ran, counted where they ran.

The counter of source uses (``tool_use_counts["knowledge"]``) counts a read
window like a search. A surface that words it as searches shows three where
one ran, against a tier that allows one. A kernel run reports the searches
it ran itself (``knowledge_searches``); an engine that records none says so.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langchain")

from inqtrix.agents.kernel import tools as kernel_tools
from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.source_policy import SourcePolicy, execution_payload
from inqtrix.state import build_run_snapshot


def _payload(**counts):
    return execution_payload(
        effective_mode="agent_kernel",
        response_form="chat",
        depth="normal",
        model="m",
        reasoning_effort="",
        source_policy=SourcePolicy(),
        consent_reason="permission_policy",
        **counts,
    )


def _projected(payload: dict) -> dict:
    """The block as a snapshot, a result and an event carry it."""
    return build_run_snapshot(
        {"done": True, "execution": payload}, current_node="agent_kernel", last_message=""
    )["execution"]


def test_the_execution_block_names_searches_apart_from_source_uses():
    payload = _payload(tool_use_counts={"web": 0, "knowledge": 3}, knowledge_searches=1)
    assert (payload["tool_use_counts"]["knowledge"], payload["knowledge_searches"]) == (3, 1)
    # The typed projection is an allowlist: the count has to survive it.
    assert _projected(payload)["knowledge_searches"] == 1


def test_an_engine_that_records_no_searches_says_so():
    """Not recorded is null, never a zero a reader takes for "searched
    nothing": the mission counts its source uses only, and a payload from
    before the field carries none."""
    payload = _payload(tool_use_counts={"web": 0, "knowledge": 2})
    assert payload["knowledge_searches"] is None
    assert _projected(payload)["knowledge_searches"] is None
    legacy = {key: value for key, value in payload.items() if key != "knowledge_searches"}
    assert _projected(legacy)["knowledge_searches"] is None


def test_a_read_is_a_use_of_the_source_and_no_search():
    class _Registry:
        async def invoke(self, capability_id, payload, context):
            return SimpleNamespace()

    deps = KernelDeps(
        run_id="run_ks", control=None, platform=None, llm=None,
        model=None, reasoning_effort=None, timeout=1.0,
    )
    deps.capability_registry = _Registry()
    set_kernel_deps(deps)
    try:
        for capability in ("knowledge.document.read", "knowledge.document.read", "knowledge.search"):
            kernel_tools._invoke_capability_inner(capability, {})
    finally:
        set_kernel_deps(None)
    assert (deps.knowledge_searches, deps.tool_use_counts["knowledge"]) == (1, 3)


def test_a_kernel_run_publishes_the_searches_it_ran():
    """Through a real run: what the settings line of the desk reads."""
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

    async def _handler(payload: BaseModel, context: CapabilityContext) -> BaseModel:
        del payload, context
        return _Output()

    llm = ScriptedToolLLM([
        _tool_turn("call_k1", "search_project_knowledge", {"query": "Frage eins"}),
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
        wait_status(client, run_id, {"waiting_for_approval"})
        approval = _pending_tool_approval(client, run_id)
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}", json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        summary = wait_status(client, run_id, {"completed"})
    execution = summary["snapshot"]["execution"]
    assert (execution["knowledge_searches"], execution["tool_use_counts"]["knowledge"]) == (1, 1)
