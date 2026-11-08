"""The checkpoint-relevant shape of each workflow that resumes from a checkpoint.

A stored run resumes only into the shape that wrote it: the nodes and edges of
its graph, its state channels, the tools its open calls name together with their
argument schemas, the kinds of interrupt it can park on, the types its
checkpoints may hold beyond LangGraph's safe set, and the marker lines its
transcript is read back from. This module reads
that shape from the graphs the algorithms compile and compares two shapes.

A break of the shape raises the workflow's ``contract_version``; an added tool
is no break, because a stored run resumes with it
(``tests/harness/test_resume_with_added_tool.py``), and only needs re-pinning.
Re-pin with::

    uv run python -m tests.architecture.workflow_pins

which rewrites ``workflow_contracts.json`` next to this module and refuses a
break that keeps the pinned contract version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PIN_FILE = Path(__file__).with_name("workflow_contracts.json")

SAME = "same"
ADDITION = "addition"
BREAK = "break"

_KERNEL_AUTONOMY = ("strict", "balanced", "autonomous")
_SCHEMA_PROSE = frozenset({"description", "title", "examples"})
_SCHEMA_NAME_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


class _NoModel:
    """Stands in for the providers: compiling a graph calls no model."""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        raise AssertionError("compiling a workflow graph calls no model")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True


def _container() -> Any:
    from inqtrix.server.routes import create_router, register_routes
    from inqtrix.settings import (
        AgentPlatformSettings,
        AgentSettings,
        ModelSettings,
        ServerSettings,
        Settings,
        StorageSettings,
    )

    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    return register_routes(
        create_router(),
        providers=SimpleNamespace(llm=_NoModel(), search=_NoModel()),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )


def _structural(schema: Any) -> Any:
    """A JSON schema without its prose: wording is not a break.

    Prose is dropped only where it is a schema keyword. Under ``properties``
    and ``$defs`` the keys are names, so an argument called ``title`` stays.
    """
    if isinstance(schema, dict):
        kept: dict[str, Any] = {}
        for key, value in schema.items():
            if key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
                kept[key] = {name: _structural(item) for name, item in value.items()}
            elif key not in _SCHEMA_PROSE:
                kept[key] = _structural(value)
        return kept
    if isinstance(schema, list):
        return [_structural(item) for item in schema]
    return schema


def _schema_hash(tool: Any) -> str:
    """Hash of the full schema a call of the tool must match, nested types included."""
    schema = tool.tool_call_schema
    if not isinstance(schema, dict):
        schema = schema.model_json_schema()
    text = json.dumps(_structural(schema), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def graph_shape(graph: Any) -> dict[str, Any]:
    """Nodes, edges and state channels of one compiled graph."""
    drawable = graph.get_graph()
    channels: dict[str, dict[str, Any]] = {}
    for name, channel in sorted(graph.channels.items()):
        entry: dict[str, Any] = {"class": type(channel).__name__}
        frequency = getattr(channel, "snapshot_frequency", None)
        if frequency is not None:
            entry["snapshot_frequency"] = frequency
        channels[name] = entry
    return {
        "nodes": sorted(drawable.nodes),
        "edges": sorted(
            [edge.source, edge.target, bool(edge.conditional)]
            for edge in drawable.edges
        ),
        "channels": channels,
    }


def graph_tools(graph: Any) -> dict[str, str]:
    """Every tool the graph's tool node can run, with its schema hash."""
    node = graph.nodes.get("tools")
    if node is None:
        return {}
    return {
        name: _schema_hash(tool)
        for name, tool in sorted(node.bound.tools_by_name.items())
    }


def kernel_interrupts() -> dict[str, list[str]]:
    """The interrupt kinds the kernel parks on, with the payload keys each needs."""
    from inqtrix.agents.kernel.interrupts import (
        CHILDREN_INTERRUPT,
        CLARIFICATION_INTERRUPT,
        translate_kernel_interrupt,
    )

    payloads = (
        {"kind": CLARIFICATION_INTERRUPT, "id": "probe"},
        {"kind": CHILDREN_INTERRUPT},
        {
            "action_requests": [{"name": "probe", "args": {}}],
            "review_configs": [{"action_name": "probe", "allowed_decisions": []}],
        },
    )
    kinds: dict[str, list[str]] = {}
    for payload in payloads:
        try:
            origin, _routed = translate_kernel_interrupt(payload)
        except ValueError:
            origin = "rejected"
        kinds.setdefault(origin, sorted(payload))
    return kinds


def current_shapes() -> dict[str, dict[str, Any]]:
    """The shape of every checkpointed workflow, as its algorithm compiles it."""
    from inqtrix.agents.kernel.work_profiles import _PROFILES
    from inqtrix.core.results import SourcePolicy

    registry = _container().registry
    kernel = registry.get("agent_kernel")
    graphs: dict[str, Any] = {}
    tools: dict[str, str] = {}
    variants = [(autonomy, autonomy, None) for autonomy in _KERNEL_AUTONOMY]
    variants += [
        (f"profile:{name}", "strict", profile) for name, profile in sorted(_PROFILES.items())
    ]
    for label, autonomy, profile in variants:
        graph = kernel._compiled_graph(
            autonomy, source_policy=SourcePolicy(), max_tool_calls=30, profile=profile
        )
        graphs[label] = graph_shape(graph)
        tools.update(graph_tools(graph))
    mission = registry.get("workspace_agent")
    # One checkpointer serves both workflows, so both carry its type list.
    from inqtrix.agents.checkpointing import CHECKPOINT_TYPES

    checkpoint_types = [list(entry) for entry in sorted(CHECKPOINT_TYPES)]
    from inqtrix.agents.kernel.tools import EDITOR_READ_MARKER, SKILL_LOADED_MARKER

    return {
        "agent_kernel": {
            "contract_version": kernel.contract_version,
            "graphs": graphs,
            "tools": dict(sorted(tools.items())),
            "interrupts": kernel_interrupts(),
            "checkpoint_types": checkpoint_types,
            # A resumed segment restores active skills and read receipts from
            # these lines of the stored transcript.
            "transcript_markers": [SKILL_LOADED_MARKER, EDITOR_READ_MARKER],
        },
        "workspace_agent": {
            "contract_version": mission.contract_version,
            "graphs": {"mission": graph_shape(mission._compiled_graph())},
            "tools": {},
            "interrupts": {},
            "checkpoint_types": checkpoint_types,
        },
    }


def compare(pinned: dict[str, Any], current: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify the change from a pinned shape to the current one.

    A tool or a graph variant that only appears is an addition: no stored run
    was written by it. Everything else that differs is a break: a node, an
    edge, a channel or its kind in a variant, a variant that disappeared, an
    interrupt kind, the types a checkpoint may hold, the format of a
    transcript marker, and a tool that disappeared or changed its arguments.
    """
    breaks: list[str] = []
    additions: list[str] = []
    pinned_graphs = pinned.get("graphs", {})
    current_graphs = current.get("graphs", {})
    for variant, shape in pinned_graphs.items():
        if variant not in current_graphs:
            breaks.append(f"graph {variant} removed")
        elif current_graphs[variant] != shape:
            breaks.append(f"graph {variant} changed")
    additions += [
        f"graph {variant} added" for variant in current_graphs if variant not in pinned_graphs
    ]
    if pinned.get("interrupts") != current.get("interrupts"):
        breaks.append("interrupts")
    if pinned.get("checkpoint_types") != current.get("checkpoint_types"):
        breaks.append("checkpoint types")
    if pinned.get("transcript_markers") != current.get("transcript_markers"):
        breaks.append("transcript markers")
    pinned_tools = pinned.get("tools", {})
    current_tools = current.get("tools", {})
    for name, schema in pinned_tools.items():
        if name not in current_tools:
            breaks.append(f"tool {name} removed")
        elif current_tools[name] != schema:
            breaks.append(f"tool {name} changed its arguments")
    additions += [
        f"tool {name} added" for name in current_tools if name not in pinned_tools
    ]
    if breaks:
        return BREAK, breaks
    if additions:
        return ADDITION, additions
    return SAME, []


def load_pins() -> dict[str, dict[str, Any]]:
    return json.loads(PIN_FILE.read_text(encoding="utf-8"))


def render(shapes: dict[str, dict[str, Any]]) -> str:
    return json.dumps(shapes, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    shapes = current_shapes()
    pins = load_pins() if PIN_FILE.exists() else {}
    for mode, shape in shapes.items():
        pinned = pins.get(mode)
        if pinned is None or pinned["contract_version"] != shape["contract_version"]:
            continue
        verdict, changes = compare(pinned, shape)
        if verdict == BREAK:
            print(
                f"{mode}: {', '.join(changes)} is a break; raise its contract_version "
                "before re-pinning.",
                file=sys.stderr,
            )
            return 1
    PIN_FILE.write_text(render(shapes), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
