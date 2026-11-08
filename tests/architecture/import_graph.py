"""The layer table and the imports of ``src/inqtrix`` as data the boundary tests read."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT = SOURCE_ROOT / "inqtrix"

BASE_MODULES = (
    "aio",
    "citation_labels",
    "constants",
    "embedding_cards",
    "exceptions",
    "i18n",
    "json_helpers",
    "legal",
    "model_cards",
    "model_routing",
    "observability.semconv",
    "pagination",
    "prompt_language",
    "provider_failure_contract",
    "quote_limits",
    "search_result",
    "settings",
    "text",
    "untrusted_content",
    "urls",
    "version",
)
OBSERVABILITY_MODULES = (
    "execution_failures",
    "logging_config",
    "observability",
    "runtime_logging",
)
RESEARCH_MODULES = (
    "domains",
    "evidence",
    "evidence_claim_processing",
    "evidence_limits",
    "graph",
    "nodes",
    "prompts",
    "report_profiles",
    "research",
    "result",
    "scoring",
    "state",
    "strategies",
)
KNOWLEDGE_MODULES = ("contextualization_circuit", "indexing_failures", "knowledge")
PROVIDER_MODULES = ("providers",)
APPLICATION_MODULES = (
    "auth",
    "content",
    "core",
    "execution_authority",
    "project",
    "quota",
    "services",
    "usage",
)
INFRASTRUCTURE_MODULES = (
    "runs",
    "storage",
    "user_events",
)
COMPOSITION_MODULES = (
    "__main__",
    "agent",
    "deploy",
    "deployment",
    "parity",
    "periodic_sweep",
    "server",
    "worker",
)
# The modules whose ports and records the infrastructure adapts.
APP_PORT_MODULES = (
    "agents.control_ports",
    "agents.memory_ports",
    "content.ports",
    "content.prompt_templates",
    "content.skills",
    "contextualization_circuit",
    "knowledge.source_cleanup",
    "knowledge.stores.ports",
    "project.base_session_store",
    "project.editor_guest_links",
    "quota.models",
    "usage.grouping",
    "usage.models",
)
APP_PORT_SUFFIX = "_ports"

_LAYER_OF_PREFIX = {
    "harness.contracts": "K0",
    "harness": "K1",
    "harness.runtime": "K2",
    "harness.testing": "K3",
    **dict.fromkeys(OBSERVABILITY_MODULES, "Q"),
    **dict.fromkeys(BASE_MODULES, "B"),
    **dict.fromkeys(RESEARCH_MODULES, "F:research"),
    **dict.fromkeys(KNOWLEDGE_MODULES, "F:knowledge"),
    **dict.fromkeys(PROVIDER_MODULES, "F:providers"),
    "plugins": "P",
    "capabilities": "P",
    "agents": "R",
    **dict.fromkeys(APPLICATION_MODULES, "A"),
    **dict.fromkeys(INFRASTRUCTURE_MODULES, "I"),
    **dict.fromkeys(COMPOSITION_MODULES, "W"),
}


def layer_of(module: str) -> str | None:
    """Return the layer of a module of the package; the longest prefix wins."""
    if module == "inqtrix":
        return "W"
    parts = module.removeprefix("inqtrix.").split(".")
    for length in range(len(parts), 0, -1):
        layer = _LAYER_OF_PREFIX.get(".".join(parts[:length]))
        if layer is not None:
            return layer
    return None


def _is_app_port(module: str) -> bool:
    name = module.removeprefix("inqtrix.")
    return name in APP_PORT_MODULES or (
        name.startswith("project.") and name.endswith(APP_PORT_SUFFIX)
    )


def _same_plugin(importer: str, imported: str) -> bool:
    # ``plugins`` holds one package per plugin; ``capabilities`` is one package.
    depth = 3 if importer.startswith("inqtrix.plugins.") else 2
    return importer.split(".")[:depth] == imported.split(".")[:depth]


def edge_allowed(importer: str, imported: str) -> bool:
    """Whether the layer table permits ``importer`` to import ``imported``."""
    source, target = layer_of(importer), layer_of(imported)
    if source is None or target is None:
        return False
    if source == "W":
        return True
    if source in {"K0", "K1", "K2", "K3"}:
        reach = {
            "K0": {"K0"},
            "K1": {"K0", "K1"},
            "K2": {"K0", "K1", "K2"},
            "K3": {"K0", "K1", "K2", "K3"},
        }[source]
        return target in reach
    if target == "K0":
        return True
    if target in {"K1", "K2", "K3"}:
        # Everything outside the kernel reaches it through the facade only.
        return imported == "inqtrix.harness" and source in {"P", "R", "A"}
    if source == "B":
        return target == "B"
    if source == "Q":
        return target in {"B", "Q"} or imported.startswith("inqtrix.auth")
    if source.startswith("F:"):
        if target in {"B", "Q"} or target == source:
            return True
        if target == "F:providers":
            return True
        if importer.startswith("inqtrix.knowledge.stores"):
            return imported.startswith(("inqtrix.storage", "inqtrix.auth"))
        return False
    if source == "P":
        if target in {"B", "Q"} or target.startswith("F:"):
            return True
        return target == "P" and _same_plugin(importer, imported)
    if source == "R":
        return (
            target in {"B", "Q", "R"}
            or target.startswith("F:")
            or imported.startswith("inqtrix.content")
        )
    if source == "A":
        return (
            target in {"A", "B", "I", "Q", "R"}
            or target.startswith("F:")
        )
    if source == "I":
        return (
            target in {"B", "I", "Q"}
            or imported.startswith("inqtrix.auth")
            or _is_app_port(imported)
        )
    return False


@dataclass(frozen=True)
class Edge:
    """One import statement, resolved to the module it reaches."""

    importer: str
    imported: str
    names: tuple[str, ...]
    path: str
    line: int

    @property
    def anchor(self) -> str:
        return f"{self.path}:{self.line}"


def iter_module_files(root: Path = PACKAGE_ROOT) -> Iterator[tuple[str, Path]]:
    """Yield every module of the package with its dotted name, without migrations."""
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent)
        if "migrations" in relative.parts:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        yield ".".join(parts), path


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def iter_edges(
    module: str, path: Path, known_modules: frozenset[str]
) -> Iterator[Edge]:
    """Yield the imports of a file: at module level, in functions, under TYPE_CHECKING.

    A file that does not parse raises ``SyntaxError``, which fails the caller.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    shown = path.relative_to(SOURCE_ROOT.parent).as_posix() if path.is_relative_to(SOURCE_ROOT.parent) else str(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Edge(module, alias.name, (), shown, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _package_of(module, path).split(".")
                keep = len(base) - node.level + 1
                prefix = ".".join(base[:keep])
                source = f"{prefix}.{node.module}" if node.module else prefix
            else:
                source = node.module or ""
            plain_names: list[str] = []
            for alias in node.names:
                candidate = f"{source}.{alias.name}"
                if candidate in known_modules:
                    yield Edge(module, candidate, (), shown, node.lineno)
                else:
                    plain_names.append(alias.name)
            if plain_names:
                yield Edge(module, source, tuple(plain_names), shown, node.lineno)


def iter_all_edges(root: Path = PACKAGE_ROOT) -> Iterator[Edge]:
    files = dict(iter_module_files(root))
    known = frozenset(files)
    for module, path in files.items():
        yield from iter_edges(module, path, known)
