"""Kernel child-run tools: slot-free waits + origin_key idempotency.

``run_web_research`` submits a REAL child research run, parks the parent
``waiting_for_children`` (no control row — child run rows are the
truth), and the resumed tool re-execution finds its child via
``origin_key`` instead of spawning a second one.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


class ScriptedToolLLM(LLMProvider):
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.models = ModelSettings(
            reasoning_model="base-model",
            tier_high_model="high-model",
            tier_mid_model="mid-model",
            tier_fast_model="fast-model",
        )
        self._turns = list(turns)
        self.chat_calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        raise AssertionError("kernel must use chat()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
        self.chat_calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("scripted provider ran out of turns")
        return self._turns.pop(0)


def delegated(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The children the model delegated; a Deep run also has its review role child."""
    return [child for child in children if not child.get("work_profile")]


def _tool_turn(call_id: str, name: str, arguments: dict) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(id=call_id, name=name, arguments=arguments),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _text_turn(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def fake_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
    # The child owns a complete V2 evidence ledger. Its presentation answer
    # cites the child label, but only the independently bound claim may cross
    # into the parent report.
    claim_text = f"Kindbericht zu: {question}"
    source_url = "https://www.bafin.de/SharedDocs/Tests/quelle.html"
    child_run_id = str(kwargs.get("run_id") or f"run_{abs(hash(question))}")
    return {
        "answer": f"{claim_text} [E1]",
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        "result_state": {
            "_run_id": child_run_id,
            "question": question,
            "answer": f"{claim_text} [E1]",
            "round": 1,
            "query_records": [
                {
                    "invocation_id": "invocation_1",
                    "query_id": "query_1",
                    "round": 0,
                    "query": question,
                    "provider": "StubSearch",
                }
            ],
            "source_records": {
                "source_1": {
                    "source_id": "source_1",
                    "url": source_url,
                    "canonical_url": source_url,
                    "domain": "bafin.de",
                    "tier": "primary",
                    "tier_reason": "test_regulator",
                }
            },
            "provider_citation_records": [
                {
                    "citation_id": "citation_1",
                    "query_id": "query_1",
                    "source_id": "source_1",
                    "canonical_url": source_url,
                    "title": "Primaerquelle",
                    "snippet": "",
                }
            ],
            "evidence_ledger": [
                {
                    "evidence_id": "evidence_1",
                    "source_id": "source_1",
                    "claims": [
                        {
                            "raw_claim_id": "raw_claim_1",
                            "claim_text": claim_text,
                            "evidence_snippet": claim_text,
                            "claim_type": "fact",
                            "needs_primary": True,
                            "source_ids": ["source_1"],
                            "source_urls": [source_url],
                        }
                    ],
                }
            ],
            "consolidated_claims_full": [
                {
                    "claim_id": "claim_1",
                    "claim_text": claim_text,
                    "claim_type": "fact",
                    "needs_primary": True,
                    "member_claim_ids": ["raw_claim_1"],
                    "status": "verified",
                }
            ],
            "required_aspects": ["Kindbericht"],
            "uncovered_aspects": [],
            "gaps": "",
            "_stop_reason": "confidence_reached",
            "report_references": [
                {
                    "label": "E1",
                    "url": source_url,
                    "title": "Primaerquelle",
                    "tier": "primary",
                }
            ],
        },
    }


def make_client(
    monkeypatch: pytest.MonkeyPatch,
    llm: ScriptedToolLLM,
    *,
    max_iterations: int | None = None,
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
    platform_kwargs: dict[str, Any] = dict(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    if max_iterations is not None:
        platform_kwargs["INQTRIX_AGENT_KERNEL_MAX_ITERATIONS"] = (
            max_iterations
        )
    settings.agent_platform = AgentPlatformSettings(**platform_kwargs)
    container = register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(4),
    )
    app.include_router(router)
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def wait_status(
    client: TestClient, run_id: str, statuses: set[str], *, timeout: float = 15.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never reached {statuses}")


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("selection", [
    {},
    {"model": "chosen-model"},
    {"model": "chosen-model", "effort": "none"},
    {"model_tier": "fast"},
    {"model_tier": "mid", "effort": "low"},
])
def test_mission_child_inherits_selection_without_resolved_kernel_defaults(
    monkeypatch, batched, selection,
):
    from inqtrix.core.results import AgentResult

    tool_name = "delegate_batch" if batched else "run_deep_mission"
    arguments = (
        {"assignments": [{"objective": "Kurzes Memo.", "mode": "deep_mission"}]}
        if batched else {"assignment": "Kurzes Memo."}
    )
    llm = ScriptedToolLLM([
        _tool_turn("call_selected_mission", tool_name, arguments),
        _text_turn("Auftrag erledigt."),
    ])
    client = make_client(monkeypatch, llm)
    received = []

    class Mission:
        id = "workspace_agent"
        display_name = "Mission"

        def capabilities(self):
            return {}

        def run(self, request, *, runtime, context):
            received.append({
                key: getattr(context.agent_settings, key)
                for key in ("model", "model_tier", "effort")
            })
            return AgentResult(answer="Kurzes Memo.", raw={"answer": "Kurzes Memo."})

    monkeypatch.setitem(client.container.registry._items, "workspace_agent", Mission())
    with client:
        response = client.post("/v1/runs", json={
            "question": "Lass ein kurzes Memo erstellen.",
            "mode": "agent_kernel", "autonomy": "autonomous",
            "agent_overrides": {"agent_tier": "tief", **selection},
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
        assert len(children) == 1
        assert children[0]["status"] == "completed"
        expected = {key: selection.get(key, "") for key in ("model", "model_tier", "effort")}
        assert received == [expected]
        stored = children[0]["agent_overrides"]
        assert {key: stored.get(key, "") for key in expected} == expected
        assert stored["agent_tier"] == "tief"
        assert stored["depth"] == "deep"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "inherits"),
    [
        ("run_deep_mission", {"assignment": "Kurzes Memo."}, True),
        (
            "delegate_batch",
            {"assignments": [{"objective": "Kurzes Memo.", "mode": "deep_mission"}]},
            False,
        ),
        ("run_web_research", {"question": "Sachfrage."}, False),
        (
            "delegate_batch",
            {"assignments": [{"objective": "Sachfrage.", "mode": "research"}]},
            False,
        ),
    ],
    ids=["single_mission", "batch_mission", "research", "batch_research"],
)
def test_only_a_single_mission_child_inherits_the_result_requirement(
    monkeypatch, tool_name, arguments, inherits,
):
    """In tier 'tief' the single mission's memo IS the deliverable, so
    the requirement the user attached must reach its writers — text and
    parts, frozen as the parent resolved them. A batch strand writes one
    of up to three memos, each announced as 'the deliverable'; inheriting
    a whole-deliverable rule there would ask for up to three briefings, so
    the kernel composes those itself. Research children deliver evidence,
    never the result, and get nothing."""
    import asyncio

    from inqtrix.core.results import AgentResult

    llm = ScriptedToolLLM([
        _tool_turn("call_requirement_child", tool_name, arguments),
        _text_turn("Auftrag erledigt."),
    ])
    client = make_client(monkeypatch, llm)
    received: list[tuple[str, tuple[dict[str, Any], ...]]] = []

    class Mission:
        id = "workspace_agent"
        display_name = "Mission"

        def capabilities(self):
            return {}

        def run(self, request, *, runtime, context):
            received.append((request.report_requirement, request.report_rules))
            return AgentResult(answer="Kurzes Memo.", raw={"answer": "Kurzes Memo."})

    monkeypatch.setitem(client.container.registry._items, "workspace_agent", Mission())
    research = tool_name == "run_web_research" or any(
        item["mode"] == "research" for item in arguments.get("assignments", ())
    )
    with client:
        rule = asyncio.run(client.container.prompt_template_service.create(
            {"title": "Briefing", "label": "briefing", "category": "instruction",
             "content_markdown": "Gliedere als Vorstandsbriefing.",
             "visibility": {"agent": True}},
            tenant_id="default", owner_user_id=None,
        ))
        response = client.post("/v1/runs", json={
            "question": "Erstelle das Briefing.",
            "mode": "agent_kernel", "autonomy": "autonomous",
            "agent_overrides": {"agent_tier": "tief"},
            "report_rule_ids": [rule.id],
            "report_guidance": "Fuer den Vorstand.",
            **({"tool_directives": ["web_research"]} if research else {}),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
        assert len(children) == 1, "no child was started — proves nothing"
        store = client.container.run_store
        parent_body = store.execution_request_body(run_id)
        child_body = store.execution_request_body(children[0]["run_id"])
    parts = [{"template_id": rule.id, "label": "briefing", "title": "Briefing", "revision": 1}]
    assert parent_body["report_rules"] == parts
    if inherits:
        assert child_body["report_requirement"] == parent_body["report_requirement"]
        assert child_body["report_rules"] == parts
        assert received == [(parent_body["report_requirement"], tuple(parts))]
    else:
        assert "report_requirement" not in child_body
        assert "report_rules" not in child_body
        assert received == ([] if research else [("", ())])


def test_only_a_mission_child_can_be_handed_the_requirement():
    """The requirement shapes a deliverable; a research child delivers
    evidence. Handing it one is a programming error, refused before any
    submission."""
    from inqtrix.agents.kernel.tools import ChildScope, submit_or_find_child

    scope = ChildScope(
        run_id="run_parent", run_service=None, resolver=None, principal=None,
        workspace_id=None, session_id="", stack_name="", source_policy=None,
        model_overrides={}, knowledge_collection_ids=None,
        knowledge_scope_explicit=False, knowledge_scope=None, tier="", depth="",
        report_requirement="[Free requirement]\nKurz.", report_rules=(),
    )
    with pytest.raises(ValueError, match="Only a mission child inherits"):
        submit_or_find_child(
            scope, origin_key="call_1", mode="research", question="Frage",
            agent_overrides={}, inherit_requirement=True,
        )


@pytest.mark.parametrize("batched", [False, True])
def test_research_child_keeps_own_model_settings(monkeypatch, batched):
    tool_name = "delegate_batch" if batched else "run_web_research"
    arguments = (
        {"assignments": [{"objective": "Sachfrage.", "mode": "research"}]}
        if batched else {"question": "Sachfrage."}
    )
    llm = ScriptedToolLLM([
        _tool_turn("call_independent_research", tool_name, arguments),
        _text_turn("Recherche erhalten."),
    ])
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post("/v1/runs", json={
            "question": "Recherche bitte.", "mode": "agent_kernel",
            "autonomy": "autonomous", "tool_directives": ["web_research"],
            "agent_overrides": {
                "agent_tier": "tief", "model": "chosen-model",
                "model_tier": "fast", "effort": "none",
            },
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
        assert len(children) == 1
        assert children[0]["mode"] == "research"
        assert children[0]["status"] == "completed"
        assert all(not children[0]["agent_overrides"].get(key) for key in ("model", "model_tier", "effort"))


def test_run_web_research_parks_and_returns_child_report(monkeypatch):
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_research1",
                "run_web_research",
                {"question": "Marktlage Klimaanlagen 2026"},
            ),
            _text_turn("Antwort auf Basis des Kindberichts."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        client.headers["x-inqtrix-workspace-id"] = "ws_kernel_contract"
        response = client.post(
            "/v1/runs",
            json={
                "question": "Analysiere die Marktlage.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-child",
                "tool_directives": ["web_research"],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        # Exactly ONE child despite the tool re-executing on resume —
        # the origin_key lookup found the submitted run.
        assert len(children) == 1
        child = children[0]
        assert child["kind"] == "agent_child"
        assert child["workspace_id"] == "ws_kernel_contract"
        assert child["origin_key"] == "call_research1"
        assert child["status"] == "completed"
        assert child["agent_overrides"]["report_profile"] == "compact"
        replay = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        projected = [
            event["data"]
            for event in replay
            if event["type"] == "inqtrix.agent.child.progress"
        ]
        assert projected
        assert {event["task_id"] for event in projected} == {
            "call_research1"
        }
        assert {event["attempt"] for event in projected} == {1}
        assert {event["child_run_id"] for event in projected} == {
            child["run_id"]
        }
        # The parent's transcript names what the child brought back — one
        # derived line (assignment: report head), like a mission task.
        lines = [
            event["data"]
            for event in replay
            if event["type"] == "inqtrix.agent.narration"
        ]
        assert [line["narration_id"] for line in lines] == [
            f"n-child-{child['run_id']}"
        ]
        assert lines[0]["text"].startswith(
            "Marktlage Klimaanlagen 2026: Kindbericht zu: Marktlage"
        )
        assert lines[0]["kind"] == "task"

        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == "Antwort auf Basis des Kindberichts."
        assert result["references"][0]["label"] == "W1"
        assert (
            result["references"][0]["url"]
            == "https://www.bafin.de/SharedDocs/Tests/quelle.html"
        )
        tool_replies = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        assert "Kindbericht zu: Marktlage Klimaanlagen 2026" in (
            tool_replies[0]["content"]
        )
        assert "Primaerquelle" in tool_replies[0]["content"]
        # The merged child ref surfaces its citable parent label, the
        # renumbering is visible, and the child TEXT's citations are
        # translated IN THE TOOL (a verbatim-copied old label would
        # resolve to the wrong parent source — the model is never
        # trusted with the mapping).
        content = tool_replies[0]["content"]
        assert "Evidence [W1] — reference_id: ref_" in content
        assert "[E1] -> [W1]" in content
        assert "Marktlage Klimaanlagen 2026 [W1]" in content
        assert "2026 [E1]" not in content


def test_child_report_preserves_provider_result_for_parent_synthesis(
    monkeypatch,
):
    false_claim = "Input costs 0.90 USD per 1M tokens."
    source_url = "https://prices.azure.com/api/retail/prices"

    def false_numeric_child_graph(
        question: str, **kwargs: Any
    ) -> dict[str, Any]:
        child_run_id = str(kwargs.get("run_id") or "run_false_numeric")
        return {
            "answer": f"{false_claim} [E1]",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "result_state": {
                "_run_id": child_run_id,
                "question": question,
                "answer": f"{false_claim} [E1]",
                "round": 1,
                "query_records": [
                    {
                        "invocation_id": "invocation_false",
                        "query_id": "query_false",
                        "round": 0,
                        "query": question,
                        "provider": "StubSearch",
                    }
                ],
                "source_records": {
                    "source_false": {
                        "source_id": "source_false",
                        "url": source_url,
                        "canonical_url": source_url,
                        "domain": "prices.azure.com",
                        "tier": "primary",
                        "tier_reason": "test_first_party",
                    }
                },
                "provider_citation_records": [
                    {
                        "citation_id": "citation_false",
                        "query_id": "query_false",
                        "source_id": "source_false",
                        "canonical_url": source_url,
                        "title": "Azure Retail Prices",
                        "snippet": false_claim,
                    }
                ],
                "evidence_ledger": [
                    {
                        "evidence_id": "evidence_false",
                        "source_id": "source_false",
                        "claims": [
                            {
                                "raw_claim_id": "raw_false",
                                "claim_text": false_claim,
                                "evidence_snippet": false_claim,
                                "claim_type": "fact",
                                "needs_primary": True,
                                "source_ids": ["source_false"],
                                "source_urls": [source_url],
                            }
                        ],
                    }
                ],
                "consolidated_claims_full": [
                    {
                        "claim_id": "claim_false",
                        "claim_text": false_claim,
                        "claim_type": "fact",
                        "needs_primary": True,
                        "member_claim_ids": ["raw_false"],
                        "status": "unverified",
                    }
                ],
                "required_aspects": ["Input price"],
                "uncovered_aspects": ["Input price"],
                "gaps": "Original price row missing",
                "_stop_reason": "max_rounds",
                "report_references": [
                    {
                        "label": "E1",
                        "url": source_url,
                        "title": "Azure Retail Prices",
                        "tier": "primary",
                    }
                ],
            },
        }

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_false_numeric",
                "run_web_research",
                {"question": "What is the exact model price?"},
            ),
            _text_turn("Die Recherche ist noch nicht ausreichend belegt."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(
        web_research_module,
        "run_web_graph",
        false_numeric_child_graph,
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Prüfe den exakten Modellpreis.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        tool_content = next(
            message["content"]
            for message in llm.chat_calls[1]["messages"]
            if message.get("role") == "tool"
        )
        result = client.get(f"/v1/runs/{run_id}/result").json()
        evidence = client.get(
            f"/v1/runs/{run_id}/artifacts/"
            f"art_{run_id[-12:]}_evidence"
        ).json()

    assert false_claim in tool_content
    assert '<untrusted_content source="subtask">' in tool_content
    assert "[W1]" in tool_content
    assert "Azure Retail Prices" in tool_content
    assert result["references"][0]["url"] == source_url
    assert result["references"][0]["query_id"] == "query_false"
    assert result["references"][0]["citation_id"] == "citation_false"
    ledger = evidence["payload"]["web_search_ledger"]
    search = ledger["searches"]["query_false"]
    assert search["query"] == "What is the exact model price?"
    assert search["provider"] == "StubSearch"
    assert search["citations"][0]["snippet"] == false_claim
    assert search["citations"][0]["url"] == source_url


def test_legacy_child_report_reaches_parent_without_page_read_contract(
    monkeypatch,
):
    legacy_answer = "Legacy answer claims the price is 0.90 USD. [E1]"
    legacy_url = "https://example.com/legacy-price"

    def legacy_child_graph(
        question: str, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "answer": legacy_answer,
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "result_state": {
                "answer": legacy_answer,
                "round": 1,
                "report_references": [
                    {
                        "label": "E1",
                        "url": legacy_url,
                        "title": "Legacy price page",
                        "tier": "primary",
                    }
                ],
                "consolidated_claims": [
                    {
                        "claim_text": "The price is 0.90 USD.",
                        "status": "verified",
                    }
                ],
            },
        }

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_legacy",
                "run_web_research",
                {"question": "Legacy price lookup"},
            ),
            _text_turn("Für diese Recherche fehlt ein Evidenzvertrag."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", legacy_child_graph
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Prüfe den Legacy-Preis.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        tool_content = next(
            message["content"]
            for message in llm.chat_calls[1]["messages"]
            if message.get("role") == "tool"
        )
        result = client.get(f"/v1/runs/{run_id}/result").json()
        artifacts = client.get(
            f"/v1/runs/{run_id}/artifacts"
        ).json()["data"]

    assert "Legacy answer claims the price is 0.90 USD. [W1]" in tool_content
    assert "Legacy price page" in tool_content
    assert '<untrusted_content source="subtask">' in tool_content
    assert result["references"][0]["url"] == legacy_url
    assert result["references"][0]["title"] == "Legacy price page"
    assert any(
        artifact["kind"] == "evidence_bundle" for artifact in artifacts
    )


def test_run_web_research_gates_in_balanced(monkeypatch):
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_research2",
                "run_web_research",
                {"question": "Externe Recherche"},
            ),
            _text_turn("Fertig."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Recherchiere.",
                "mode": "agent_kernel",
                "autonomy": "balanced",
                "tool_directives": ["web_research"],
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})
        # Nothing was submitted before consent.
        assert client.get(f"/v1/runs/{run_id}/children").json()["data"] == []
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        action = approvals[0]["payload"]["actions"][0]
        assert action["tool"] == "run_web_research"
        assert action["args"]["question"] == "Externe Recherche"

        client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "approve"},
        )
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1


def test_failed_child_is_a_visible_tool_result(monkeypatch):
    def broken_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Recherche-Backend nicht erreichbar")

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_research3",
                "run_web_research",
                {"question": "kaputt"},
            ),
            _text_turn("Ohne Kindbericht beantwortet."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", broken_child_graph
    )
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Recherchiere.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == "Ohne Kindbericht beantwortet."
        tool_replies = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        assert "The subtask (research) failed" in tool_replies[0]["content"]


def test_normal_kernel_can_adaptively_run_shared_research_child(monkeypatch):
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_unrequested",
                "run_web_research",
                {"question": "Ungefragter Recherchelauf"},
            ),
            _text_turn("Die Recherche wurde genutzt."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Beantworte eine einzelne Frage.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        tool_replies = [
            message
            for message in llm.chat_calls[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert "Tool blocked" not in tool_replies[0]["content"]


# -- delegate_batch (parallel fan-out) -------------------------------------- #


def test_delegate_batch_submits_all_before_single_park(monkeypatch):
    """Both children exist BEFORE the one park; results come back in
    assignment order as condensed (<=300-word) summaries with sources."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_batch1",
                "delegate_batch",
                {
                    "assignments": [
                        {"objective": "Strang A untersuchen", "mode": "research"},
                        {"objective": "Strang B untersuchen", "mode": "research"},
                    ]
                },
            ),
            _text_turn("Antwort auf Basis beider Unterauftraege."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        client.headers["x-inqtrix-workspace-id"] = "ws_batch"
        response = client.post(
            "/v1/runs",
            json={
                "question": "Analysiere beide Straenge.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-batch",
                "tool_directives": ["web_research"],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 2
        by_origin = {child["origin_key"]: child for child in children}
        assert set(by_origin) == {"call_batch1:0", "call_batch1:1"}
        # (Autonomy inheritance is a submit-side contract shared with the
        # single-child tools; the summary does not expose the field.)

        tool_replies = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        content = tool_replies[0]["content"]
        # Assignment order, one block per child, condensed summary text.
        first = content.find("## Subtask 1")
        second = content.find("## Subtask 2")
        assert 0 <= first < second, content[:300]
        assert "Kindbericht zu: Strang A untersuchen" in content
        assert "Kindbericht zu: Strang B untersuchen" in content


def _batch_with_preparation_turns() -> list[ChatTurn]:
    """A realistic single-segment batch trajectory: the model plans and
    refines todos, then fans out — all BEFORE the one park. This is the
    live failure class of the recursion recalibration: preparation turns
    plus the fan-out turn in one segment."""
    plan = [
        {"content": "Straenge identifizieren", "status": "in_progress"},
        {"content": "Fan-out starten", "status": "pending"},
        {"content": "Synthese schreiben", "status": "pending"},
    ]
    refined = [dict(plan[0], status="completed"), plan[1], plan[2]]
    started = [refined[0], dict(plan[1], status="in_progress"), plan[2]]
    return [
        _tool_turn("call_prep1", "write_todos", {"todos": plan}),
        _tool_turn("call_prep2", "write_todos", {"todos": refined}),
        _tool_turn("call_prep3", "write_todos", {"todos": started}),
        _tool_turn(
            "call_batch3",
            "delegate_batch",
            {
                "assignments": [
                    {"objective": "Strang A untersuchen", "mode": "research"},
                    {"objective": "Strang B untersuchen", "mode": "research"},
                ]
            },
        ),
        _text_turn("Synthese aus beiden Unterauftraegen."),
    ]


def test_delegate_batch_two_children_plus_synthesis_completes(monkeypatch):
    """The default recursion ceiling permits a prepared two-child batch.

    A batch run whose segment holds preparation turns plus the fan-out
    must complete under the DEFAULT iteration ceiling. One tool turn
    costs 8 super-steps and the answer turn 9 (both pinned in
    ``test_supersteps_per_tool_turn_is_pinned``), so this 4-tool-turn
    trajectory needs 41. The second half proves that a ceiling of 24
    fails before fan-out, so the success assertion is not tautological.
    """
    llm = ScriptedToolLLM(_batch_with_preparation_turns())
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Analysiere beide Straenge gruendlich.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", (
            "batch trajectory died under the DEFAULT recursion ceiling "
            f"— the recalibration regressed: {summary}"
        )

        children = client.get(
            f"/v1/runs/{run_id}/children"
        ).json()["data"]
        assert {child["origin_key"] for child in children} == {
            "call_batch3:0",
            "call_batch3:1",
        }
        assert {child["status"] for child in children} == {"completed"}
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == "Synthese aus beiden Unterauftraegen."

    # The same trajectory under a ceiling of 24 reaches the durable limit
    # decision before delegate_batch dispatches. It must remain visibly
    # paused with zero children until the user makes an explicit choice.
    old_llm = ScriptedToolLLM(_batch_with_preparation_turns())
    old_client = make_client(monkeypatch, old_llm, max_iterations=24)
    with old_client:
        run_id = old_client.post(
            "/v1/runs",
            json={
                "question": "Analysiere beide Straenge gruendlich.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        ).json()["run_id"]
        summary = wait_status(old_client, run_id, {"waiting_for_input"})
        assert summary["status"] == "waiting_for_input"
        assert old_client.get(
            f"/v1/runs/{run_id}/children"
        ).json()["data"] == []

        events = old_client.get(
            f"/v1/runs/{run_id}/events",
            params={"format": "json"},
        ).json()["data"]
        limit_events = [
            event
            for event in events
            if event["type"] == "inqtrix.agent.limit.reached"
        ]
        assert len(limit_events) == 1
        assert limit_events[0]["data"] == {
            "clarification_id": limit_events[0]["data"]["clarification_id"],
            "kind": "steps",
            "used": 25,
            "limit": 24,
            "next_limit": 48,
            # The published operator ceiling (normal == deep: 241).
            "ceiling": 241,
            "extendable": True,
            "choices": ["extend", "partial", "cancel"],
            "state": "waiting_for_input",
        }

        clarifications = old_client.get(
            f"/v1/runs/{run_id}/clarifications"
        ).json()["data"]
        assert len(clarifications) == 1
        decision = old_client.post(
            f"/v1/runs/{run_id}/clarifications/"
            f"{clarifications[0]['clarification_id']}",
            json={"option_id": "partial"},
        )
        assert decision.status_code == 200, decision.text
        completed = wait_status(old_client, run_id, {"completed", "failed"})
        assert completed["status"] == "completed", completed
        result = old_client.get(f"/v1/runs/{run_id}/result").json()
        assert "Partial result" in result["answer"]
        decided = [
            event
            for event in old_client.get(
                f"/v1/runs/{run_id}/events",
                params={"format": "json"},
            ).json()["data"]
            if event["type"] == "inqtrix.agent.limit.decided"
        ]
        assert [event["data"]["choice"] for event in decided] == ["partial"]


def test_delegate_batch_reports_partial_failure_per_assignment(monkeypatch):
    """One failed child yields a visible per-assignment failure line while
    the sibling's result survives (all-wait join, no batch abort)."""
    calls = {"count": 0}

    def flaky_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        if "B" in question:
            raise RuntimeError("Provider explodierte")
        return fake_child_graph(question, **kwargs)

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_batch2",
                "delegate_batch",
                {
                    "assignments": [
                        {"objective": "Strang A", "mode": "research"},
                        {"objective": "Strang B", "mode": "research"},
                    ]
                },
            ),
            _text_turn("Antwort mit benannter Luecke."),
        ]
    )
    client = make_client(monkeypatch, llm)
    # AFTER make_client: it patches run_web_graph itself and would
    # otherwise overwrite the flaky variant.
    monkeypatch.setattr(
        web_research_module, "run_web_graph", flaky_child_graph
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Beide Straenge.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        tool_replies = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        content = tool_replies[0]["content"]
        assert "Kindbericht zu: Strang A" in content
        assert "The subtask (research) failed" in content


def test_child_batch_guard_voids_child_next_to_ask_user():
    """The guard voids a child next to ``ask_user`` before dispatch.

    Without the guard, the child would submit and then both bodies would
    interrupt, leaving the child without a responsible parent path.
    """
    from langchain_core.messages import AIMessage

    from inqtrix.agents.kernel.middleware import (
        KernelChildBatchGuardMiddleware,
    )

    turn = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_ask_mix",
                "name": "ask_user",
                "args": {"question": "Welcher Markt?"},
            },
            {
                "id": "call_child_mix",
                "name": "run_web_research",
                "args": {"question": "Marktlage"},
            },
        ],
    )
    update = KernelChildBatchGuardMiddleware().after_model(
        {"messages": [turn]}, None
    )
    assert update is not None
    # Return shape: the AIMessage stays first, corrective answers follow.
    corrective = update["messages"][1:]
    assert [message.tool_call_id for message in corrective] == [
        "call_child_mix"
    ]
    assert "clarifying question" in corrective[0].content
    # The gate predicate skips these turns too (single answering
    # authority — HITL must not park an approval the guard voids).
    from inqtrix.agents.kernel.policy import _single_child_dispatch
    from types import SimpleNamespace

    request = SimpleNamespace(state={"messages": [turn]})
    assert _single_child_dispatch(request) is False


def test_child_batch_guard_blocks_parallel_single_child_calls(monkeypatch):
    """Two single-child tool calls in ONE turn dispatch nothing: zero
    submissions, corrective ToolMessages, the next turn proceeds."""
    llm = ScriptedToolLLM(
        [
            ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        id="call_par1",
                        name="run_deep_mission",
                        arguments={"assignment": "A"},
                    ),
                    ToolCallRequest(
                        id="call_par2",
                        name="run_deep_mission",
                        arguments={"assignment": "B"},
                    ),
                ),
                finish_reason="tool_calls",
                model="high-model",
                prompt_tokens=10,
                completion_tokens=5,
                raw=None,
            ),
            _text_turn("Verstanden — ich nutze delegate_batch."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Zwei parallele Auftraege.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        # NOTHING was submitted (pre-dispatch guard).
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert children == []
        corrective = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        assert len(corrective) == 2
        assert all(
            "delegate_batch" in str(m.get("content", ""))
            for m in corrective
        )
        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        assert any(
            e["type"] == "inqtrix.agent.child_batch.guarded"
            for e in events
        )
        # The interception is VISIBLE (No Silent Fallbacks): a narration
        # tells the user why nothing dispatched.
        assert any(
            e["type"] == "inqtrix.agent.narration"
            and str(e["data"].get("narration_id", "")).startswith(
                "batchguard_"
            )
            for e in events
        )


def test_parent_failure_cancels_running_children(monkeypatch):
    """A failed parent cancels its live children (orphan cleanup)."""
    import threading

    release = threading.Event()

    def hanging_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        release.wait(timeout=10)
        return fake_child_graph(question, **kwargs)

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_orph1",
                "run_web_research",
                {"question": "Haengender Auftrag"},
            ),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", hanging_child_graph
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Starte den Auftrag.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        # Parent parks on the child; the child hangs in its worker.
        wait_status(client, run_id, {"waiting_for_children"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1

        # Force the parent into FAILED via the store seam.
        store = client.container.run_store  # type: ignore[attr-defined]
        store.fail(run_id, "Simulierter Elternfehler")
        release.set()

        summary = wait_status(client, run_id, {"failed"})
        assert summary["status"] == "failed"
        child_row = wait_status(
            client,
            children[0]["run_id"],
            {"cancelled", "failed", "completed"},
        )
        assert child_row["status"] == "cancelled"


def test_delegate_batch_gates_in_balanced_with_verbatim_assignments(
    monkeypatch,
):
    """Standard mode parks ONE kind='tool' approval whose single action
    carries every assignment verbatim; approve submits and resumes."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_batch3",
                "delegate_batch",
                {
                    "assignments": [
                        {"objective": "Strang A", "mode": "research"},
                        {"objective": "Strang B", "mode": "research"},
                    ]
                },
            ),
            _text_turn("Antwort nach Freigabe."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Beide Straenge.",
                "mode": "agent_kernel",
                "autonomy": "balanced",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})
        # Gate BEFORE dispatch: nothing was submitted yet.
        assert client.get(
            f"/v1/runs/{run_id}/children"
        ).json()["data"] == []
        approvals = client.get(
            f"/v1/runs/{run_id}/approvals"
        ).json()["data"]
        pending = [a for a in approvals if a["status"] == "pending"]
        assert len(pending) == 1 and pending[0]["kind"] == "tool"
        action = pending[0]["payload"]["actions"][0]
        assert action["tool"] == "delegate_batch"
        assert action["args"]["assignments"] == [
            {"objective": "Strang A", "mode": "research"},
            {"objective": "Strang B", "mode": "research"},
        ]

        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 2


def test_balanced_parallel_child_calls_void_without_gate(monkeypatch):
    """Two child-tool calls in ONE balanced turn are voided by the guard
    WITHOUT parking a gate.

    The gate policy's ``when`` predicate skips a multi-child turn, so the
    approve-then-void bug is gone (an approval whose actions the guard
    then refuses) and so is the reject double-answer. Discriminator: with
    the OLD unconditional gate, HITL would batch both calls into one
    interrupt and the run would hang at ``waiting_for_approval`` — this
    test would time out at ``completed``.
    """
    llm = ScriptedToolLLM(
        [
            ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        id="call_bp1",
                        name="run_deep_mission",
                        arguments={"assignment": "A"},
                    ),
                    ToolCallRequest(
                        id="call_bp2",
                        name="run_deep_mission",
                        arguments={"assignment": "B"},
                    ),
                ),
                finish_reason="tool_calls",
                model="high-model",
                prompt_tokens=10,
                completion_tokens=5,
                raw=None,
            ),
            _text_turn("Verstanden — ich nutze delegate_batch."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Zwei parallele Auftraege.",
                "mode": "agent_kernel",
                "autonomy": "balanced",
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        # No gate was EVER parked (the predicate skipped it).
        assert client.get(
            f"/v1/runs/{run_id}/approvals"
        ).json()["data"] == []
        # Nothing submitted; both calls answered by the guard.
        assert client.get(
            f"/v1/runs/{run_id}/children"
        ).json()["data"] == []
        corrective = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        assert len(corrective) == 2
        events = client.get(
            f"/v1/runs/{run_id}/events?format=json"
        ).json()["data"]
        assert any(
            e["type"] == "inqtrix.agent.child_batch.guarded"
            for e in events
        )


def test_child_submissions_inherit_parent_autonomy(monkeypatch):
    """Children inherit the PARENT's autonomy at submit time.

    Closes the plan's ``run_web_research`` gap where children defaulted
    to no autonomy. Stack inheritance rides the same resolve payload but
    needs a multi-stack env (single-stack test mode resolves to ``""``),
    so it is asserted in the live PG E2E, not here.
    """
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_inh1", "run_web_research", {"question": "Frage"}
            ),
            _text_turn("Fertig."),
        ]
    )
    client = make_client(monkeypatch, llm)
    captured: list[dict[str, Any]] = []
    real_submit = client.container.run_service.submit

    def spy(**kwargs: Any) -> Any:
        if kwargs.get("kind") == "agent_child":
            captured.append(kwargs)
        return real_submit(**kwargs)

    client.container.run_service.submit = spy  # type: ignore[attr-defined]
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
    assert captured, "no child submission was captured"
    assert all(call["autonomy"] == "autonomous" for call in captured)
    assert captured[0]["origin_key"] == "call_inh1"
    assert captured[0]["parent_run_id"] == run_id


def test_child_submissions_inherit_parent_stufe(monkeypatch):
    """Children inherit the PARENT's Stufe (``agent_tier``) at submit time.

    A ``tief`` kernel that handed its children only ``depth`` (mission)
    or nothing (research) would let the request bridge blank the tier
    against the environment default — the mission would run without any
    tier policy and force DEEP on every research grandchild. The tier must
    ride the child request; the bridge derives depth from it.
    """
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_stufe1", "run_web_research", {"question": "Frage"}
            ),
            _text_turn("Fertig."),
        ]
    )
    client = make_client(monkeypatch, llm)
    captured: list[dict[str, Any]] = []
    real_submit = client.container.run_service.submit

    def spy(**kwargs: Any) -> Any:
        if kwargs.get("kind") == "agent_child":
            captured.append(kwargs)
        return real_submit(**kwargs)

    client.container.run_service.submit = spy  # type: ignore[attr-defined]
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "agent_overrides": {"agent_tier": "tief"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
    assert captured, "no child submission was captured"
    # The tier rides the request verbatim; NO depth is sent alongside it
    # (the bridge derives depth, sending both would be a 400 on drift).
    assert captured[0]["resolved"].agent_overrides == {
        "report_profile": "compact",
        "agent_tier": "tief",
    }
    # The stored child carries the resolved, consistent pair.
    assert len(children) == 1
    assert children[0]["agent_overrides"]["agent_tier"] == "tief"
    assert children[0]["agent_overrides"]["depth"] == "deep"
    assert children[0]["agent_overrides"]["report_profile"] == "compact"


def test_child_submissions_without_a_stufe_keep_the_depth_path(monkeypatch):
    """A parent without a Stufe still hands ``depth`` to its children."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_depth1", "run_web_research", {"question": "Frage"}
            ),
            _text_turn("Fertig."),
        ]
    )
    client = make_client(monkeypatch, llm)
    captured: list[dict[str, Any]] = []
    real_submit = client.container.run_service.submit

    def spy(**kwargs: Any) -> Any:
        if kwargs.get("kind") == "agent_child":
            captured.append(kwargs)
        return real_submit(**kwargs)

    client.container.run_service.submit = spy  # type: ignore[attr-defined]
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "agent_overrides": {"depth": "deep"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
    assert captured, "no child submission was captured"
    assert captured[0]["resolved"].agent_overrides == {
        "report_profile": "deep",
        "depth": "deep",
    }


@pytest.mark.parametrize("bulk", [False, True])
def test_child_report_names_a_capped_evidence_contract(monkeypatch, bulk):
    """A research child whose evidence audit capped its confidence tells
    the kernel so in its report — as an open point to name, not a source
    to cite — instead of arriving as a confident-sounding text."""

    def capped_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        if bulk:
            result["answer"] += "\n\n" + "Unveraenderter Detailkontext. " * 500
            result["result_state"]["answer"] = result["answer"]
        result["result_state"].update(
            {
                "final_confidence": 6,
                "evidence_contract_status": "needs_review",
                "evidence_depth_gap": {"active": True, "single_source_ratio": 0.93},
            }
        )
        return result

    llm = ScriptedToolLLM(
        [
            _tool_turn("call_capped", "run_web_research", {"question": "Frage"}),
            _text_turn("Antwort mit offenem Punkt."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(web_research_module, "run_web_graph", capped_child_graph)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
    # The tool result of the child report is what the model reads next.
    final_turn = [
        message for message in llm.chat_calls[-1]["messages"]
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if bulk:
        assert any("The complete tool result is above. Retained original:" in str(message) for message in final_turn)
    assert any(
        "Evidence state of the subtask: needs_review, confidence 6/10" in str(message)
        and "name it as an open point" in str(message)
        for message in final_turn
    ), [str(message)[:200] for message in final_turn]


def test_mission_child_memo_is_the_deliverable_and_arms_one_advisory(
    monkeypatch,
):
    """A completed mission child hands its memo on as THE deliverable.

    The tool result names the session document (title, artifact id,
    revision) instead of inviting a second canvas, and the very next
    model turn carries one advisory — no second research child to
    verify the mission — which is not repeated on later turns.
    """
    import asyncio

    from inqtrix.agents.control_ports import session_memo_artifact_id
    from inqtrix.core.results import AgentResult

    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_mission",
                "run_deep_mission",
                {"assignment": "KI-Neuigkeiten dieser Woche, ausfuehrlich"},
            ),
            _tool_turn(
                "call_read_memo",
                "read_canvas",
                {"artifact_id": "art_sess-mission_memo"},
            ),
            _text_turn("Kurzfazit — Details im Dokument 'Memo: KI-Woche'."),
        ]
    )
    client = make_client(monkeypatch, llm)
    store = client.container.agent_control_service.store  # type: ignore[attr-defined]

    class FakeMission:
        id = "workspace_agent"
        display_name = "Fake Mission"

        def capabilities(self) -> dict[str, Any]:
            return {}

        def run(self, request: Any, *, runtime: Any, context: Any) -> Any:
            run_id = str(context.run_id)
            assert request.session_id == "sess-mission"
            asyncio.run(
                store.upsert_artifact(
                    run_id=run_id,
                    kind="memo",
                    session_id=request.session_id,
                    title="Memo: KI-Woche",
                    status="final",
                    content_markdown="# Memo\n\nInhalt der Mission.",
                    payload={},
                    refs=[],
                    updated_by="mission",
                    artifact_id=session_memo_artifact_id(
                        request.session_id, run_id
                    ),
                )
            )
            refs = [{"label": "W1", "url": "https://example.org/conditions"}]
            ledger = {"searches": {"query_mission": {
                "query_id": "query_mission", "source_run_id": run_id,
                "query": "Welche Bedingungen gelten?",
                "provider_answer": "Die Registrierung muss vorher erfolgen.",
                "citations": [{"url": refs[0]["url"], "citation_id": "citation_mission"}],
            }}}
            asyncio.run(store.upsert_artifact(
                run_id=run_id, artifact_id=f"art_{run_id[-12:]}_evidence",
                kind="evidence_bundle", session_id=None, title="Agent evidence",
                status="ready", content_markdown="", refs=refs,
                payload={"web_search_ledger": ledger}, updated_by="agent",
            ))
            raw = {"answer": "Die Registrierung muss vorher erfolgen. [W1]",
                   "result_state": {"report_references": refs, "web_search_ledger": ledger}}
            return AgentResult(answer=str(raw["answer"]), raw=raw)

    registry = client.container.registry  # type: ignore[attr-defined]
    monkeypatch.setitem(registry._items, "workspace_agent", FakeMission())

    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Welche Neuigkeiten gibt es zu KI in dieser Woche?",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-mission",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})

        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert [(c["mode"], c["status"]) for c in children] == [
            ("workspace_agent", "completed")
        ]
        child_id = children[0]["run_id"]
        assert "web_search_ledger" not in client.get(f"/v1/runs/{child_id}/result").json()
        retained = client.get(
            f"/v1/runs/{run_id}/artifacts/art_{run_id[-12:]}_evidence"
        ).json()["payload"]["web_search_ledger"]["searches"]["query_mission"]
        assert retained["source_run_id"] == child_id
        assert retained["provider_answer"] == "Die Registrierung muss vorher erfolgen."

        # The tool result names the memo as the deliverable.
        tool_replies = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ]
        content = str(tool_replies[0]["content"])
        assert "is the deliverable of this assignment" in content
        assert "Memo: KI-Woche" in content
        assert "artifact_id art_sess-mission_memo" in content
        assert "revision 1" in content
        assert "write no second document" in content
        # The memo is read in full before the summary; the summary cites
        # its labels (the digest is only the head).
        assert "read_canvas(artifact_id)" in content

        # Exactly ONE advisory, on the turn right after the mission.
        def advisories(call_index: int) -> list[dict[str, Any]]:
            return [
                m
                for m in llm.chat_calls[call_index]["messages"]
                if m.get("role") == "user"
                and "The mission is complete" in str(m.get("content", ""))
            ]

        assert len(advisories(1)) == 1
        assert "read_canvas(artifact_id)" in str(advisories(1)[0].get("content", ""))
        assert len(advisories(2)) == 1  # history keeps it; not re-added
        assert llm.chat_calls[2]["messages"].count(
            advisories(1)[0]
        ) == 1
        # The kernel could read the memo it was pointed to.
        memo_replies = [
            m
            for m in llm.chat_calls[2]["messages"]
            if m.get("role") == "tool"
            and "Canvas document: Memo: KI-Woche" in str(m.get("content", ""))
        ]
        assert len(memo_replies) == 1

        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"].startswith("Kurzfazit")

        # The memo is announced on the PARENT stream as this turn's
        # deliverable (the file chip) with its title and revision — no
        # diff base and no line counts, because the parent cannot know
        # where the memo stood before the dispatch (it resumes later).
        # Mutation: dropping the emit fails this pin.
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
        handed = [
            e["data"]
            for e in events
            if e["type"] == "inqtrix.agent.artifact.updated"
            and e["data"].get("kind") == "memo"
        ]
        assert len(handed) == 1
        assert handed[0]["artifact_id"] == "art_sess-mission_memo"
        assert handed[0]["title"] == "Memo: KI-Woche"
        assert handed[0]["revision"] == 1
        assert "from_revision" not in handed[0]
        assert "lines_added" not in handed[0]


def test_a_follow_up_turn_keeps_the_citations_of_the_previous_answer(
    monkeypatch,
):
    """A follow-up in the same session answered from the history cites
    the previous answer's sources; a citation check that knows only this
    run's own ledger marks every such label "[unbelegt: W…]". The
    session's retained references are therefore registered as known
    evidence before the first turn, labels preserved.

    Mutation: dropping the registration marks [W1] unsupported again.
    """
    import asyncio

    llm = ScriptedToolLLM(
        [
            _text_turn("Der Markt waechst."),
            _text_turn("Kurz: Der Markt waechst weiter [W1]."),
        ]
    )
    client = make_client(monkeypatch, llm)
    store = client.container.agent_control_service.store  # type: ignore[attr-defined]

    def submit(question: str) -> str:
        response = client.post(
            "/v1/runs",
            json={
                "question": question,
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-follow",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        return run_id

    with client:
        previous = submit("Wie entwickelt sich der Markt?")
        # The earlier turn's deliverable retained one cited source.
        asyncio.run(
            store.upsert_artifact(
                run_id=previous,
                kind="deliverable",
                session_id="sess-follow",
                title="Marktnotiz",
                status="ready",
                content_markdown="Der Markt waechst [W1].",
                payload={},
                refs=[
                    {
                        "label": "W1",
                        "url": "https://example.org/markt",
                        "title": "Marktbericht",
                    }
                ],
                updated_by="agent",
                artifact_id="art_prev_note",
            )
        )
        run_id = submit("Und wie geht es weiter?")
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert "[W1]" in result["answer"]
        assert "unbelegt" not in result["answer"]
        assert [r["url"] for r in result["references"]] == ["https://example.org/markt"]
        assert result["references"][0]["label"] == "W1"


def test_gruendlich_kernel_cannot_start_a_mission_but_delegates_research(
    monkeypatch,
):
    """The middle rung of the ladder, enforced at the chokepoint: a
    ``gruendlich`` kernel gets a VISIBLE denial for ``run_deep_mission``
    (naming the rung that has it); a batch with a ``deep_mission``
    assignment is denied as a whole (fail-closed, nothing dispatched),
    while a research-only batch dispatches and the child inherits the
    Stufe."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_mission",
                "run_deep_mission",
                {"assignment": "Alles zu KI dieser Woche"},
            ),
            _tool_turn(
                "call_batch_mixed",
                "delegate_batch",
                {
                    "assignments": [
                        {"objective": "Strang A untersuchen", "mode": "research"},
                        {"objective": "Strang B als Mission", "mode": "deep_mission"},
                    ]
                },
            ),
            _tool_turn(
                "call_batch_research",
                "delegate_batch",
                {
                    "assignments": [
                        {"objective": "Strang A untersuchen", "mode": "research"},
                    ]
                },
            ),
            _text_turn("Antwort aus dem Recherche-Kind."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Welche Neuigkeiten gibt es zu KI?",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-ladder",
                "agent_overrides": {"agent_tier": "gruendlich"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})

        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert [(c["mode"], c["origin_key"], c["status"]) for c in children] == [
            ("research", "call_batch_research:0", "completed")
        ]
        assert children[0]["agent_overrides"]["agent_tier"] == "gruendlich"

        def tool_reply(call_index: int) -> str:
            replies = [
                m
                for m in llm.chat_calls[call_index]["messages"]
                if m.get("role") == "tool"
            ]
            return str(replies[-1]["content"])

        denial = tool_reply(1)
        assert "Tool blocked" in denial
        assert "tier 'gruendlich'" in denial
        assert "only in the tier 'tief'" in denial
        mixed = tool_reply(2)
        assert mixed.startswith("Assignment 2: Tool blocked")
        assert "tier 'gruendlich'" in mixed
        assert "Kindbericht zu: Strang A untersuchen" in tool_reply(3)


def test_schnell_kernel_writes_a_canvas_when_the_user_asks_for_a_file(
    monkeypatch,
):
    """``write_canvas`` is never tier-blocked: the fastest Stufe still
    produces a document when the user asks for one."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_canvas",
                "write_canvas",
                {
                    "title": "Kurznotiz",
                    "content_markdown": "# Kurznotiz\n\nDrei Punkte.",
                    "deliverable_kind": "email",
                },
            ),
            _text_turn("Die Datei 'Kurznotiz' liegt im Canvas."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreib mir das als Datei in den Canvas.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-schnell-file",
                "agent_overrides": {"agent_tier": "schnell"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        reply = str(
            [
                m
                for m in llm.chat_calls[1]["messages"]
                if m.get("role") == "tool"
            ][0]["content"]
        )
        assert "Tool blocked" not in reply
        assert "artifact_id" in reply
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
        created = [
            e for e in events if e["type"] == "inqtrix.agent.artifact.created"
        ]
        deliverables = [
            e["data"].get("title")
            for e in created
            if e["data"].get("kind") == "deliverable"
        ]
        assert deliverables == ["Kurznotiz"]


@pytest.mark.parametrize("bulk", [False, True])
def test_child_report_names_a_partial_evidence_coverage(monkeypatch, bulk):
    """A research child whose evidence budget left sources unevaluated tells
    the kernel so in its report and on the transcript line — as a gap to
    name, never as 'no sources found' that would justify a second child."""

    def partial_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        if bulk:
            result["answer"] += "\n\n" + "Unveraenderter Detailkontext. " * 500
            result["result_state"]["answer"] = result["answer"]
        result["result_state"].update(
            {
                "final_confidence": 7,
                "evidence_contract_status": "clean",
                "rendered_evidence_record_count": 16,
                "omitted_evidence_record_count": 21,
                "omitted_evidence_by_query": [
                    {"query_id": "q1", "query": "A", "omitted": 12, "rendered": 0},
                    {"query_id": "q2", "query": "B", "omitted": 5, "rendered": 4},
                    {"query_id": "q3", "query": "C", "omitted": 4, "rendered": 6},
                ],
            }
        )
        return result

    llm = ScriptedToolLLM(
        [
            _tool_turn("call_partial", "run_web_research", {"question": "Frage"}),
            _text_turn("Antwort mit benannter Luecke."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(web_research_module, "run_web_graph", partial_child_graph)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()
    final_turn = llm.chat_calls[-1]["messages"]
    contents = [
        str(message.get("content", ""))
        for message in final_turn
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if bulk:
        assert any("The complete tool result is above. Retained original:" in content for content in contents)
    assert any(
        "Coverage of the subtask: partially evaluated (21 citable sources from 3 searches not evaluated)"
        in content
        and "do not read it as 'no sources found'" in content
        for content in contents
    ), [content for content in contents if "subtask" in content]
    narration = [
        e for e in (events.get("data") or events if isinstance(events, dict) else events)
        if isinstance(e, dict) and str(e.get("type", "")).endswith("narration")
        and str((e.get("payload") or e.get("data") or {}).get("narration_id", "")).startswith("n-child-")
    ]
    assert narration, "the kernel narrates every finished child"
    texts = [str((e.get("payload") or e.get("data") or {}).get("text", "")) for e in narration]
    # The transcript line follows the RUN's language; "Recherche bitte."
    # carries no confident language signal, so the wording falls back to
    # English (a detectably German question would read "teilweise
    # ausgewertet (21 belegfähige Quellen aus 3 Suchen nicht ausgewertet)").
    assert any("partially evaluated (21 citable sources from 3 searches not evaluated)" in t for t in texts), texts


def test_child_report_merges_only_the_references_the_child_cited(monkeypatch):
    """A source the child examined but did not cite (``cited: False``) stays
    in the child's report; it must not reach the parent as grounded
    evidence."""

    def examined_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        result = fake_child_graph(question, **kwargs)
        cited = dict(result["result_state"]["report_references"][0], cited=True)
        result["result_state"]["report_references"] = [
            cited,
            {
                "label": "E2",
                "url": "https://example.org/examined",
                "title": "Nur gesichtet",
                "tier": "mainstream",
                "cited": False,
            },
        ]
        return result

    llm = ScriptedToolLLM(
        [
            _tool_turn("call_cited", "run_web_research", {"question": "Frage"}),
            _text_turn("Antwort."),
        ]
    )
    client = make_client(monkeypatch, llm)
    monkeypatch.setattr(web_research_module, "run_web_graph", examined_child_graph)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Recherche bitte.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
    assert [reference["url"] for reference in result["references"]] == [
        "https://www.bafin.de/SharedDocs/Tests/quelle.html"
    ]
    tool_replies = [
        m for m in llm.chat_calls[1]["messages"] if m.get("role") == "tool"
    ]
    assert "example.org/examined" not in tool_replies[0]["content"]
