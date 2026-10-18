"""Planning, writing and substantive review honor the selected model and effort.

Resolution events expose the chosen settings while classification keeps its
configured role. Missing effort and an explicit ``none`` are distinct choices.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.agents.test_workspace_agent import (
    ScriptedLLM,
    make_agent_client,
    wait_status,
)
from tests.agents.test_workspace_agent_skills import _create_skill


def _resolutions(client, run_id: str) -> dict[str, dict[str, Any]]:
    # format=json returns the replay buffer IMMEDIATELY — the SSE
    # stream would stay open on a PARKED (non-terminal) run and block
    # the test forever.
    events = client.get(
        f"/v1/runs/{run_id}/events", params={"format": "json"}
    ).json()["data"]
    return {
        event["data"]["node"]: event["data"]
        for event in events
        if event["type"] == "inqtrix.node.model_resolution"
    }


def _submit(client, body: dict[str, Any]) -> str:
    response = client.post(
        "/v1/runs",
        json={
            "question": "Erstelle eine Marktanalyse.",
            "mode": "workspace_agent",
            **body,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_explicit_model_and_effort_hit_only_thinking_nodes(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        run_id = _submit(
            client,
            {"agent_overrides": {"model": "opus-x", "effort": "xhigh"}},
        )
        wait_status(client, run_id, {"waiting_for_approval"})
        nodes = _resolutions(client, run_id)
        for node in ("agent_plan", "agent_synthesis", "agent_answer", "agent_deep_review"):
            assert nodes[node]["model"] == "opus-x", node
            assert nodes[node]["model_source"] == "explicit_request"
            assert nodes[node]["effort"] == "xhigh"
        # Assembly-line nodes keep the tier map untouched.
        assert nodes["agent_intake"]["model"] == "fast-model"
        assert nodes["agent_intake"]["model_source"] == "tier:fast"
        assert nodes["agent_intake"]["effort"] == ""


@pytest.mark.parametrize("tier, model, effort", [
    ("high", "high-model", "medium"),
    ("mid", "mid-model", "low"),
    ("fast", "fast-model", "none"),
])
def test_tier_override_scopes_the_same_way(monkeypatch, tier, model, effort):
    llm = ScriptedLLM()
    llm.models.tier_high_effort = "medium"
    llm.models.tier_mid_effort = "low"
    llm.models.tier_fast_effort = "none"
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = _submit(client, {"agent_overrides": {"model_tier": tier}})
        wait_status(client, run_id, {"waiting_for_approval"})
        nodes = _resolutions(client, run_id)
        for node in ("agent_plan", "agent_deep_review"):
            assert nodes[node]["model"] == model
            assert nodes[node]["model_source"] == f"tier:{tier}"
            assert nodes[node]["effort"] == effort
            assert nodes[node]["effort_source"] == f"tier:{tier}"
        assert nodes["agent_intake"]["model"] == "fast-model"


def test_skill_pin_scopes_to_thinking_nodes(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        skill_id = _create_skill(
            client,
            clarification_points=[],
            instructions_markdown="Arbeite knapp.",
            model_tier="high",
            effort="xhigh",
            # The default payload says never — that SKIPS the plan gate
            # and the test would wait for an approval that never comes.
            requires_plan="always",
        )
        run_id = _submit(client, {"skill_ids": [skill_id]})
        wait_status(client, run_id, {"waiting_for_approval"})
        nodes = _resolutions(client, run_id)
        for node in ("agent_plan", "agent_deep_review"):
            assert nodes[node]["model"] == "high-model"
            assert nodes[node]["effort"] == "xhigh"
            assert nodes[node]["effort_source"] == "explicit_request"
        # The pin must not inflate the cheap calls either.
        assert nodes["agent_intake"]["model"] == "fast-model"
        assert nodes["agent_intake"]["effort"] == ""


@pytest.mark.parametrize("overrides, model, effort, model_source, effort_source", [
    ({}, "high-model", "medium", "tier:high", "tier:high"),
    ({"model": "chosen-model"}, "chosen-model", "", "explicit_request", "provider_default"),
    ({"model": "chosen-model", "effort": "none"}, "chosen-model", "none", "explicit_request", "explicit_request"),
    ({"effort": "low"}, "high-model", "low", "tier:high", "explicit_request"),
])
def test_mission_review_preserves_selected_effort_semantics(
    monkeypatch, overrides, model, effort, model_source, effort_source,
):
    llm = ScriptedLLM()
    llm.models.tier_high_effort = "medium"
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = _submit(client, {"agent_overrides": overrides})
        wait_status(client, run_id, {"waiting_for_approval"})
        desc = _resolutions(client, run_id)["agent_deep_review"]
        assert (desc["model"], desc["effort"]) == (model, effort)
        assert (desc["model_source"], desc["effort_source"]) == (model_source, effort_source)


def test_mission_review_request_overrides_skill_model_settings(monkeypatch):
    client = make_agent_client(monkeypatch)
    with client:
        skill_id = _create_skill(
            client, clarification_points=[], instructions_markdown="Arbeite knapp.",
            model_tier="high", effort="xhigh", requires_plan="always",
        )
        run_id = _submit(client, {
            "skill_ids": [skill_id],
            "agent_overrides": {"model": "chosen-model", "model_tier": "mid", "effort": "none"},
        })
        wait_status(client, run_id, {"waiting_for_approval"})
        desc = _resolutions(client, run_id)["agent_deep_review"]
        assert (desc["model"], desc["tier"], desc["effort"]) == ("chosen-model", "mid", "none")
