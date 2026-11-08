"""The layer table of the package, checked on every import.

A known violation stands in ``boundary_exceptions.py`` with its reason. The list is a
ratchet in both directions: a new violation is red, and so is an entry whose edge has
gone. A message names the file and line the scanner read, never a line written down by
hand. ``tests/web_gateway/test_architecture.py`` is the pattern for the smaller package.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.architecture.boundary_exceptions import BOUNDARY_EXCEPTIONS, BoundaryException
from tests.architecture.import_graph import (
    Edge,
    edge_allowed,
    iter_all_edges,
    iter_module_files,
    layer_of,
)

FRAMEWORKS = ("deepagents", "langchain", "langchain_core")
ALL_FRAMEWORKS = (*FRAMEWORKS, "langgraph")
LANGGRAPH_ALLOWED = ("inqtrix.research", "inqtrix.runs.checkpointing", "inqtrix.graph")


def _is_framework(module: str, names: Iterable[str] = ALL_FRAMEWORKS) -> bool:
    return module.split(".")[0] in names


def _in_adapter(module: str) -> bool:
    return module == "inqtrix.harness.runtime" or module.startswith("inqtrix.harness.runtime.")


def package_violations(edges: Iterable[Edge]) -> set[tuple[str, str]]:
    return {
        (edge.importer, edge.imported)
        for edge in edges
        if edge.imported.split(".")[0] == "inqtrix"
        and not edge_allowed(edge.importer, edge.imported)
    }


def framework_violations(edges: Iterable[Edge]) -> set[tuple[str, str]]:
    found = set()
    for edge in edges:
        if not _is_framework(edge.imported) or _in_adapter(edge.importer):
            continue
        if edge.imported.split(".")[0] == "langgraph" and edge.importer.startswith(LANGGRAPH_ALLOWED):
            continue
        found.add((edge.importer, edge.imported))
    return found


def private_name_violations(edges: Iterable[Edge]) -> set[tuple[str, str]]:
    found = set()
    for edge in edges:
        if not _is_framework(edge.imported):
            continue
        private = [part for part in edge.imported.split(".") if part.startswith("_")]
        private += [name for name in edge.names if name.startswith("_")]
        if private:
            found.add((edge.importer, f"{edge.imported}:{private[0]}"))
    return found


def locations(edges: Iterable[Edge]) -> dict[tuple[str, str], list[Edge]]:
    """The imports behind each edge, as the scanner read them."""
    found: dict[tuple[str, str], list[Edge]] = {}
    for edge in edges:
        found.setdefault((edge.importer, edge.imported), []).append(edge)
    return found


def describe(
    violations: Iterable[tuple[str, str]], where: dict[tuple[str, str], list[Edge]]
) -> str:
    """One line per violation with its places in file and line order; ``module:_name`` finds its module."""
    lines = []
    for importer, imported in violations:
        edges = sorted(where.get((importer, imported.partition(":")[0]), []), key=lambda e: (e.path, e.line))
        places = ", ".join(dict.fromkeys(edge.anchor for edge in edges)) or "no scanned import"
        lines.append(f"{importer} -> {imported} at {places}")
    return "\n".join(lines)


def _live_violations() -> tuple[set[tuple[str, str]], int, dict[tuple[str, str], list[Edge]]]:
    edges = list(iter_all_edges())
    return (
        package_violations(edges) | framework_violations(edges) | private_name_violations(edges),
        len(edges),
        locations(edges),
    )


def ratchet(
    live: set[tuple[str, str]], known: set[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return the violations nobody listed and the entries without a live edge."""
    return sorted(live - known), sorted(known - live)


def kernel_entries(entries: Iterable[BoundaryException]) -> list[str]:
    return [
        entry.importer
        for entry in entries
        if entry.importer == "inqtrix.harness" or entry.importer.startswith("inqtrix.harness.")
    ]


def test_every_module_belongs_to_exactly_one_layer() -> None:
    unassigned = sorted(
        module for module, _path in iter_module_files() if layer_of(module) is None
    )
    assert not unassigned, f"modules without a layer: {unassigned}"


def test_no_import_breaks_the_layer_table() -> None:
    live, checked, where = _live_violations()
    known = {(entry.importer, entry.imported) for entry in BOUNDARY_EXCEPTIONS}
    new, _gone = ratchet(live, known)
    print(f"edges checked {checked}, violations {len(new)}, exceptions live {len(live & known)}")
    assert not new, "new layer violations:\n" + describe(new, where)


def test_no_agent_imports_the_research_nodes() -> None:
    """``nodes`` still uses a helper of ``agents``: an edge back closes an import cycle."""
    edges = [
        edge.anchor
        for edge in iter_all_edges()
        if edge.imported == "inqtrix.nodes" and edge.importer.startswith("inqtrix.agents")
    ]
    assert not edges, edges


def test_every_exception_still_has_its_edge() -> None:
    live, _checked, _where = _live_violations()
    known = {(entry.importer, entry.imported) for entry in BOUNDARY_EXCEPTIONS}
    _new, gone = ratchet(live, known)
    assert not gone, "exceptions without a live edge:\n" + "\n".join(f"{a} -> {b}" for a, b in gone)


def test_an_exception_stands_only_once_and_says_why() -> None:
    keys = [(entry.importer, entry.imported) for entry in BOUNDARY_EXCEPTIONS]
    assert len(keys) == len(set(keys))
    assert all(entry.reason.strip() for entry in BOUNDARY_EXCEPTIONS)


def test_the_kernel_never_gets_an_exception() -> None:
    assert not kernel_entries(BOUNDARY_EXCEPTIONS)


def test_the_ratchet_turns_both_ways() -> None:
    new, gone = ratchet({("a", "b"), ("a", "c")}, {("a", "c"), ("x", "y")})
    assert new == [("a", "b")]
    assert gone == [("x", "y")]


def test_an_entry_for_the_kernel_is_seen() -> None:
    entries = [
        BoundaryException("inqtrix.harness", "inqtrix.urls", "why"),
        BoundaryException("inqtrix.harness.dispatch", "inqtrix.urls", "why"),
        BoundaryException("inqtrix.harnessing", "inqtrix.urls", "why"),
    ]
    assert kernel_entries(entries) == ["inqtrix.harness", "inqtrix.harness.dispatch"]


def _package(tmp_path: Path, files: dict[str, str]) -> list[Edge]:
    root = tmp_path / "inqtrix"
    for name, source in {"__init__.py": "", **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return list(iter_all_edges(root))


@pytest.mark.parametrize(
    "source",
    [
        "from inqtrix.server.runs import RunStatus",
        "def f():\n    from inqtrix.server.runs import RunStatus",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from inqtrix.server.runs import RunStatus",
        "import inqtrix.server.runs",
    ],
)
def test_every_form_of_an_import_is_seen_in_the_kernel(tmp_path: Path, source: str) -> None:
    edges = _package(tmp_path, {"harness/registry.py": source})
    assert package_violations(edges) == {("inqtrix.harness.registry", "inqtrix.server.runs")}


def test_a_relative_import_is_resolved(tmp_path: Path) -> None:
    edges = _package(
        tmp_path,
        {
            "runs/__init__.py": "",
            "runs/x.py": "from ..server import runs",
            "server/__init__.py": "",
            "server/runs.py": "",
        },
    )
    assert package_violations(edges) == {("inqtrix.runs.x", "inqtrix.server.runs")}


def test_a_violation_names_the_line_the_scanner_read(tmp_path: Path) -> None:
    edges = _package(
        tmp_path,
        {
            # The nested import comes first in the file but second in the
            # scanner's breadth-first walk: only sorting puts line 2 first.
            "harness/registry.py": (
                "def f():\n    from inqtrix.server.runs import RunStatus\n"
                + "\n" * 8
                + "from inqtrix.server.runs import RunStatus"
            ),
            "harness/runtime/a.py": "from deepagents.middleware.summarization import _X",
        },
    )
    where = locations(edges)
    found = package_violations(edges) | private_name_violations(edges)
    root = tmp_path / "inqtrix"
    registry = root / "harness/registry.py"
    assert describe(sorted(found), where).splitlines() == [
        f"inqtrix.harness.registry -> inqtrix.server.runs at {registry}:2, {registry}:11",
        "inqtrix.harness.runtime.a -> deepagents.middleware.summarization:_X"
        f" at {root / 'harness/runtime/a.py'}:1",
    ]
    assert describe([("inqtrix.x", "inqtrix.y")], where) == "inqtrix.x -> inqtrix.y at no scanned import"


def test_a_file_that_does_not_parse_fails(tmp_path: Path) -> None:
    with pytest.raises(SyntaxError):
        _package(tmp_path, {"text.py": "def broken(:"})


@pytest.mark.parametrize(
    ("importer", "imported", "allowed"),
    [
        ("inqtrix.text", "inqtrix.urls", True),
        ("inqtrix.text", "inqtrix.agents.x", False),
        ("inqtrix.observability.otel", "inqtrix.auth.log_redaction", True),
        ("inqtrix.observability.otel", "inqtrix.runs.shared", False),
        ("inqtrix.knowledge.algorithm", "inqtrix.providers.base", True),
        ("inqtrix.providers.base", "inqtrix.knowledge.algorithm", False),
        ("inqtrix.knowledge.algorithm", "inqtrix.state", False),
        ("inqtrix.knowledge.stores.memory", "inqtrix.auth.shares", True),
        ("inqtrix.knowledge.stores.memory", "inqtrix.storage.db", True),
        ("inqtrix.knowledge.algorithm", "inqtrix.storage.db", False),
        ("inqtrix.agents.harness", "inqtrix.content.skills", True),
        ("inqtrix.agents.harness", "inqtrix.runs.shared", False),
        ("inqtrix.services.run_service", "inqtrix.runs.shared", True),
        ("inqtrix.services.run_service", "inqtrix.server.runs", False),
        ("inqtrix.runs.shared", "inqtrix.auth.permissions", True),
        ("inqtrix.runs.shared", "inqtrix.services.audit_service", False),
        ("inqtrix.storage.x", "inqtrix.project.editor_ports", True),
        ("inqtrix.storage.x", "inqtrix.project.other", False),
        ("inqtrix.storage.x", "inqtrix.quota.models", True),
        ("inqtrix.runs.x", "inqtrix.contextualization_circuit", True),
        ("inqtrix.runs.x", "inqtrix.knowledge.source_cleanup", True),
        ("inqtrix.runs.x", "inqtrix.knowledge.contextualize", False),
        ("inqtrix.storage.x", "inqtrix.content.prompt_templates", True),
        ("inqtrix.storage.x", "inqtrix.usage.grouping", True),
        ("inqtrix.server.routers.runs", "inqtrix.services.run_service", True),
        ("inqtrix.harness.contracts.events", "inqtrix.harness.dispatch", False),
        ("inqtrix.harness.dispatch", "inqtrix.harness.contracts.events", True),
        ("inqtrix.harness.dispatch", "inqtrix.harness.runtime.x", False),
        ("inqtrix.harness.runtime.x", "inqtrix.harness.dispatch", True),
        ("inqtrix.harness.dispatch", "inqtrix.urls", False),
        ("inqtrix.capabilities.x", "inqtrix.harness.contracts.events", True),
        ("inqtrix.capabilities.x", "inqtrix.harness", True),
        ("inqtrix.capabilities.x", "inqtrix.harness.dispatch", False),
        ("inqtrix.agents.x", "inqtrix.harness.runtime.y", False),
        ("inqtrix.runs.x", "inqtrix.harness", False),
        ("inqtrix.plugins.canvas.ops", "inqtrix.plugins.editor.ops", False),
        ("inqtrix.plugins.canvas.ops", "inqtrix.plugins.canvas.ports", True),
        ("inqtrix.text", "inqtrix", False),
    ],
)
def test_the_layer_table(importer: str, imported: str, allowed: bool) -> None:
    assert edge_allowed(importer, imported) is allowed


def test_an_unknown_module_has_no_layer() -> None:
    assert layer_of("inqtrix.scratch_mod") is None
    assert edge_allowed("inqtrix.scratch_mod", "inqtrix.urls") is False


def test_a_framework_stays_behind_the_adapter(tmp_path: Path) -> None:
    edges = _package(
        tmp_path,
        {
            "plugins/canvas/operations.py": "import langchain_core",
            "harness/runtime/adapter.py": "import langchain_core\nimport deepagents",
            "runs/checkpointing.py": "import langgraph.types",
            "runs/other.py": "import langgraph.types",
            "research/x.py": "import langgraph.types\nimport langchain_core",
        },
    )
    assert framework_violations(edges) == {
        ("inqtrix.plugins.canvas.operations", "langchain_core"),
        ("inqtrix.runs.other", "langgraph.types"),
        ("inqtrix.research.x", "langchain_core"),
    }


def test_a_private_name_of_a_framework_is_seen(tmp_path: Path) -> None:
    edges = _package(
        tmp_path,
        {
            "harness/runtime/a.py": "from deepagents.middleware.summarization import _X",
            "harness/runtime/b.py": "from langgraph._internal import thing",
            "harness/runtime/c.py": "from deepagents import fine",
            "text.py": "from json import _private",
        },
    )
    assert private_name_violations(edges) == {
        ("inqtrix.harness.runtime.a", "deepagents.middleware.summarization:_X"),
        ("inqtrix.harness.runtime.b", "langgraph._internal:_internal"),
    }
