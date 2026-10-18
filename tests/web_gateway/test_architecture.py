"""Architecture guards for the dependency-light web gateway package."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "inqtrix_web_gateway"
)

# Dependencies flow from policy/adapters toward composition and process entry.
# A lower layer importing app or cli would create a cycle or hidden runtime
# configuration boundary.
ALLOWED_INTERNAL_IMPORTS = {
    "__init__": {"app", "settings"},
    "__main__": {"cli"},
    "app": {
        "http_proxy",
        "logging",
        "security_headers",
        "settings",
        "static",
        "websocket_proxy",
    },
    "cli": {"app", "logging", "settings"},
    "headers": {"settings"},
    "http_proxy": {"headers", "logging", "settings"},
    "logging": set(),
    # A leaf on purpose: the baseline header policy depends on nothing, so it
    # cannot be weakened by a change elsewhere in the edge.
    "security_headers": set(),
    "settings": set(),
    "static": set(),
    "websocket_proxy": {"headers", "settings"},
}


def _internal_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            imports.add(node.module.split(".", 1)[0])
        else:
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
    return imports


def test_gateway_modules_follow_one_way_dependency_graph() -> None:
    """Every module stays within its explicit architectural dependency set."""
    observed_modules = {path.stem for path in PACKAGE_ROOT.glob("*.py")}
    assert observed_modules == set(ALLOWED_INTERNAL_IMPORTS)
    for module, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        path = PACKAGE_ROOT / f"{module}.py"
        assert _internal_imports(path) <= allowed


def test_gateway_has_no_legacy_monolith() -> None:
    """The former script/facade cannot silently grow back beside the package."""
    repo_root = PACKAGE_ROOT.parents[1]
    assert not (PACKAGE_ROOT / "gateway.py").exists()
    assert not (repo_root / "scripts" / "run_research_desk.py").exists()
