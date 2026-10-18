"""Web evidence is supplied by the search API, without subsequent page access."""

from copy import deepcopy
from dataclasses import replace
import asyncio
import json
import socket
import subprocess

import pytest

from inqtrix.agents.evidence_context import synthesis_evidence
from inqtrix.agents.kernel.deps import KernelDeps, run_coro
from inqtrix.agents.kernel.evidence_read import (
    build_review_evidence,
    evidence_read_document,
    read_registered_evidence,
)
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.capabilities import CapabilityContext, build_capability_registry
from inqtrix.auth.principal import Principal
from inqtrix.nodes import search
from inqtrix.providers.base import ProviderContext
from inqtrix.report_profiles import ReportProfile
from inqtrix.result import ResearchResult
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state
from inqtrix.strategies import create_default_strategies
from tests.test_claim_assessment_scope import _LLM


ANSWER = "The permission applies only after registration."
URL = "https://source.invalid/conditions"
UNRELATED_TEXT = "This retained page body is not search API material."


class SearchAPI:
    def __init__(self):
        self.calls = 0

    def search(self, *args, **kwargs):
        self.calls += 1
        return GroundedSearchResult(
            answer=ANSWER,
            sources=[GroundedSource(URL, title="Conditions", snippet="API snippet", rank=1)],
        )


@pytest.fixture
def no_page_access(monkeypatch):
    # The provider is supplied in-process. Any further DNS lookup, connection,
    # or helper process would be an independent retrieval, outside its result.
    def forbidden(*args, **kwargs):
        pytest.fail("Web evidence must not access linked pages or launch a fetch worker")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_instant_search_returns_api_material_without_page_access(no_page_access):
    provider = SearchAPI()
    registry = build_capability_registry(search_provider=provider)
    result = asyncio.run(registry.invoke(
        "web.search.instant", {"query": "Conditions"},
        CapabilityContext(principal=Principal(user_id=None, kind="anonymous")),
    ))
    assert provider.calls == 1
    assert result.answer == ANSWER
    assert result.sources[0].url == URL
    assert result.sources[0].snippet == "API snippet"
    assert "source_documents" not in result.model_dump()


@pytest.mark.parametrize("profile", [ReportProfile.SCHNELL, ReportProfile.COMPACT, ReportProfile.DEEP])
def test_research_extracts_only_api_material_in_each_profile(no_page_access, profile):
    inputs = []

    class Extraction:
        def extract(self, text, citations, question, **kwargs):
            inputs.append(text)
            return [], 0, 0

    provider = SearchAPI()
    settings = AgentSettings(report_profile=profile, first_round_queries=1)
    strategies = replace(create_default_strategies(settings), claim_extraction=Extraction())
    state = initial_state("What are the conditions?", run_id="run-provider-only", max_total_seconds=30)
    state["queries"] = ["Conditions"]
    # Unrelated collections in a restored checkpoint are never model material.
    state["source_documents"] = {"document": {"text": UNRELATED_TEXT, "status": "captured"}}
    search(state, providers=ProviderContext(llm=_LLM(), search=provider), strategies=strategies, settings=settings)
    assert provider.calls == 1
    assert len(inputs) == 1
    assert ANSWER in inputs[0] and "API snippet" in inputs[0]
    assert UNRELATED_TEXT not in inputs[0]
    result = ResearchResult.from_raw({"result_state": state, "web_source_ids": [URL]}).to_export_payload()
    assert "source_documents" not in result.get("web_search_ledger", {})
    assert UNRELATED_TEXT not in json.dumps(result)


def test_retained_read_review_and_mission_synthesis_use_only_provider_ledger():
    control = MemoryAgentControlStore()
    deps = KernelDeps(
        run_id="run-provider-reader", session_id="session-provider-reader",
        control=control, platform=None, llm=None, model=None,
        reasoning_effort=None, timeout=1,
    )
    row = {
        "query_id": "query", "source_run_id": deps.run_id,
        "query": "Conditions", "provider_answer": ANSWER,
        "citations": [{"url": URL, "title": "Conditions", "snippet": "API snippet"}],
    }
    ledger = {"searches": {"query": row}, "source_documents": {"document": {"text": UNRELATED_TEXT}}}
    run_coro(control.upsert_artifact(
        run_id=deps.run_id, artifact_id=f"art_{deps.run_id[-12:]}_evidence",
        kind="evidence_bundle", session_id=None, title="Evidence", status="ready",
        content_markdown="", payload={"web_search_ledger": ledger},
        refs=[{"url": URL, "label": "W1", "source_run_id": deps.run_id, "query_id": "query"}],
        updated_by="agent",
    ))
    deps.hydrate_evidence()
    assert "source_documents" not in deps.web_search_ledger
    ref, = deps.evidence_refs.values()
    read = read_registered_evidence(deps, reference_id=ref["reference_id"])
    query_read = read_registered_evidence(deps, query_id="query")
    review = build_review_evidence(deps, [ref], mission_run_ids=[], language="de")
    for context in (read, query_read, review):
        assert context["searches"]["query"]["provider_answer"] == ANSWER
        assert UNRELATED_TEXT not in evidence_read_document(context)
    assert ANSWER in synthesis_evidence([ref], ledger)
    assert UNRELATED_TEXT not in synthesis_evidence([ref], ledger)
    assert deps.tool_use_counts == {"web": 0, "knowledge": 0}


@pytest.mark.parametrize("surface", ["result", "artifact"])
def test_public_evidence_exposes_provider_records_without_other_collections(monkeypatch, surface):
    from tests.agents.test_kernel_read_tools_policy import (
        ScriptedToolLLM, _submit, _text_turn, make_client, wait_status,
    )

    client = make_client(ScriptedToolLLM([_text_turn("Report.")]), SearchAPI())
    ledger = {
        "searches": {"query": {"provider_answer": ANSWER, "citations": [{"url": URL}]}},
        "source_documents": {"document": {"text": UNRELATED_TEXT}},
    }
    with client:
        run_id = _submit(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed"})
        payload = {"answer": "Report.", "web_search_ledger": deepcopy(ledger)}
        if surface == "result":
            monkeypatch.setattr(client.container.run_store, "result", lambda *args, **kwargs: deepcopy(payload))
            response = client.get(f"/v1/runs/{run_id}/result")
            projected = response.json()
        else:
            run_coro(client.container.agent_control_service.store.upsert_artifact(
                run_id=run_id, artifact_id="artifact-provider-evidence", kind="evidence_bundle",
                session_id=None, title="Evidence", status="ready", content_markdown="",
                payload=payload, refs=[], updated_by="agent",
            ))
            response = client.get(f"/v1/runs/{run_id}/artifacts/artifact-provider-evidence")
            projected = response.json().get("payload", {})
        assert response.status_code == 200, response.text
        assert projected["web_search_ledger"]["searches"] == ledger["searches"]
        assert UNRELATED_TEXT not in response.text
        assert "source_documents" not in projected["web_search_ledger"]
        assert payload["web_search_ledger"] == ledger
