"""The mission engine is gone; only the runs it stored remain as data.

None of its modules exists any more and no product module imports one. A
server never registers its mode and refuses a submission of it with a named
reason; the kernel has no tool that delegates to it. Its plan and task routes,
its capability fields, its routing nodes, its event constants and its
section-writer profile are gone, and each of its settings is a retired setting
that warns or stops the start. Stored mission runs stay readable: that is
pinned where the read routes are tested.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("langchain")

from tests.agents.test_kernel_walking_skeleton import ScriptedToolLLM, make_kernel_client

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "inqtrix"
PINS = Path(__file__).resolve().parent / "workflow_contracts.json"

RETIRED_MODULES = (
    "inqtrix.agents.algorithm",
    "inqtrix.agents.discovery",
    "inqtrix.agents.intake",
    "inqtrix.agents.patch_phase",
    "inqtrix.agents.plan_collections",
    "inqtrix.agents.plan_models",
    "inqtrix.agents.plan_validation",
    "inqtrix.agents.planner",
    "inqtrix.agents.replan",
    "inqtrix.agents.research_prospect",
    "inqtrix.agents.synthesis",
)
"""The modules of the mission engine."""

RETIRED_ROUTES = (
    ("GET", "/v1/runs/{run_id}/plan"),
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/result"),
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/cancel"),
)

RETIRED_SETTING_OUTCOMES = {
    "INQTRIX_AGENT_MAX_PARALLEL_CHILDREN": "warn",
    "INQTRIX_AGENT_DISCOVERY_MAX_TOOL_CALLS": "warn",
    "INQTRIX_AGENT_MAX_PLAN_TASKS": "warn",
    "INQTRIX_AGENT_MAX_REPLAN_ROUNDS": "warn",
    "INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS": "warn",
    "INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET": "warn",
    "INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET": "warn",
    "INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS": "warn",
    "INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW": "warn",
    "INQTRIX_AGENT_MAX_CLARIFICATION_ROUNDS": "abort",
    "INQTRIX_AGENT_DEFAULT_MODE": "abort",
}
"""The mission's settings and what setting one does now. A number for the
clarification rounds and a default mode were deliberate choices that ignoring
them would undo, so those two stop the start."""

RETIRED_NODES = (
    "agent_intake",
    "agent_discovery_analyst",
    "agent_plan",
    "agent_contradiction",
    "agent_synthesis",
    "agent_answer",
    "agent_answer_light",
    "agent_patch",
)

RETIRED_EVENT_CONSTANTS = (
    "PLAN_PROPOSED_EVENT",
    "PLAN_REVISED_EVENT",
    "PATCH_PROPOSED_EVENT",
    "TASK_STARTED_EVENT",
    "TASK_FINISHED_EVENT",
    "TASK_FAILED_EVENT",
    "TASK_CANCEL_REQUESTED_EVENT",
    "ARTIFACT_EDIT_CONFLICT_EVENT",
)
"""Constants of the event types only the mission wrote. The types stay values
of stored event rows."""


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SOURCE_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    """Every module one file imports, relative imports resolved."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                target = ".".join([*base, node.module] if node.module else base)
            else:
                target = node.module or ""
            found.add(target)
            # ``from inqtrix.agents import algorithm`` names the module too.
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_a_mission_module_no_longer_exists(module: str) -> None:
    assert importlib.util.find_spec(module) is None, f"{module} still exists"


def test_no_plan_module_is_left_in_the_agents_package() -> None:
    assert sorted(path.name for path in (SOURCE_ROOT / "agents").glob("plan_*.py")) == []


def test_no_product_module_imports_a_mission_module() -> None:
    importers = sorted(
        f"{_module_name(path)} -> {target}"
        for path in SOURCE_ROOT.rglob("*.py")
        for target in _imported_modules(path)
        if any(target == name or target.startswith(f"{name}.") for name in RETIRED_MODULES)
    )

    assert importers == [], (
        f"{', '.join(importers)}: nothing in the product may import the retired "
        "mission engine."
    )


def test_a_server_never_registers_the_mission() -> None:
    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        ids = client.container.registry.ids()

    assert "agent_kernel" in ids
    assert "workspace_agent" not in ids


def test_a_mission_submission_is_refused_as_retired() -> None:
    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        response = client.post(
            "/v1/runs",
            json={"question": "Fasse den EU AI Act zusammen.", "mode": "workspace_agent"},
        )

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["reason"] == "workflow_retired"
    assert "retired" in error["message"]
    assert "'agent_kernel'" in error["message"]


def test_the_kernel_has_no_tool_that_delegates_to_the_mission() -> None:
    from inqtrix.agents.kernel.tools import build_kernel_tools

    names = {tool.name for tool in build_kernel_tools()}

    assert "run_deep_mission" not in names
    assert {"run_web_research", "delegate_batch"} <= names


def test_the_plan_and_task_routes_are_gone() -> None:
    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        routes = {
            (method, route.path)
            for route in client.app.routes
            for method in getattr(route, "methods", None) or ()
        }

    # The table is the one the agent control routes are bound into.
    assert ("GET", "/v1/runs/{run_id}/approvals") in routes
    assert sorted(set(RETIRED_ROUTES) & routes) == []


def test_capabilities_publish_no_field_of_the_mission() -> None:
    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        payload = client.get("/v1/capabilities").json()

    features, agent = payload["features"], payload["agent"]
    assert features["agent_kernel"] is True
    assert {"workspace_agent", "workspace_agent_durable"} & set(features) == set()
    assert {"max_parallel_children", "discovery_max_tool_calls", "max_plan_tasks"} & set(
        agent
    ) == set()
    assert "kernel" in agent["limits"] and "document_analysis" in agent["limits"]
    assert "mission" not in agent["limits"]
    assert set(agent["permission_modes"]) == {"strict", "balanced", "autonomous"}
    for mode, entry in agent["permission_modes"].items():
        assert "kernel_gated_tools" in entry, mode
        assert {"plan_gate", "web_replan_regate", "patch_gate"} & set(entry) == set(), mode
    assert [tier["id"] for tier in agent["tiers"]] == ["schnell", "gruendlich", "tief"]
    for tier in agent["tiers"]:
        assert "clarification_rounds" in tier, tier["id"]
        assert {"plan_gate", "section_register", "web_child_ceiling"} & set(tier) == set(), (
            tier["id"]
        )


def test_every_mission_setting_is_retired_with_its_outcome() -> None:
    from inqtrix.settings import RETIRED_SETTINGS, AgentPlatformSettings

    outcomes = {entry.name: entry.outcome for entry in RETIRED_SETTINGS}
    aliases = {field.alias for field in AgentPlatformSettings.model_fields.values()}

    assert {name: outcomes.get(name) for name in RETIRED_SETTING_OUTCOMES} == (
        RETIRED_SETTING_OUTCOMES
    )
    assert "INQTRIX_AGENT_KERNEL_ENABLED" in aliases
    assert sorted(aliases & set(RETIRED_SETTING_OUTCOMES)) == []


def test_a_configured_default_mode_stops_the_start_and_names_its_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.settings import check_retired_settings

    monkeypatch.setenv("INQTRIX_AGENT_DEFAULT_MODE", "workspace_agent")

    with pytest.raises(RuntimeError) as stop:
        check_retired_settings()

    message = str(stop.value)
    assert "Retired setting INQTRIX_AGENT_DEFAULT_MODE is set" in message
    assert "agent_kernel is the only agent mode" in message
    assert "workspace_agent" not in message


def test_no_routing_node_of_the_mission_is_left() -> None:
    from inqtrix.agents.telemetry import _MODEL_RETRY_CONTEXT
    from inqtrix.model_routing import NODE_TIER_ASSIGNMENT

    assert "agent_kernel" in NODE_TIER_ASSIGNMENT and "agent_kernel" in _MODEL_RETRY_CONTEXT
    assert sorted(set(RETIRED_NODES) & set(NODE_TIER_ASSIGNMENT)) == []
    assert sorted(set(RETIRED_NODES) & set(_MODEL_RETRY_CONTEXT)) == []


def test_no_event_constant_of_the_mission_is_left() -> None:
    from inqtrix.harness.contracts import events

    assert events.PHASE_CHANGED_EVENT == "inqtrix.agent.phase.changed"
    assert [name for name in RETIRED_EVENT_CONSTANTS if hasattr(events, name)] == []


def test_the_kernel_contract_carries_no_section_writer() -> None:
    from inqtrix.agents.kernel.work_profiles import _PROFILES

    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        contract = client.container.registry.get("agent_kernel").contract_version
    pinned = json.loads(PINS.read_text(encoding="utf-8"))["agent_kernel"]

    assert (contract, pinned["contract_version"]) == (7, 7)
    assert "review" in _PROFILES and "section_writer" not in _PROFILES
    assert "profile:review" in pinned["graphs"]
    assert "profile:section_writer" not in pinned["graphs"]
