"""A gate environment must hold every optional dependency the tests skip on.

``pytest.importorskip`` suits a contributor with a partial install. In a gate it
reports green while whole contract files never ran, so the full suite turns red
here instead of skipping them silently.
"""

from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]


def _skipped_modules(root: Path) -> set[str]:
    """Collect the module names of every literal ``importorskip`` under ``root``."""
    names: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "importorskip"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                names.add(node.args[0].value)
    return names


def test_the_scan_sees_the_kernel_contracts():
    """A scan that finds nothing would make the check below pass on its own."""
    assert "langchain" in _skipped_modules(TESTS_ROOT)


def test_every_skippable_module_is_installed():
    missing = sorted(
        name for name in _skipped_modules(TESTS_ROOT) if find_spec(name) is None
    )
    assert missing == [], (
        f"Tests would skip silently without {missing}. "
        "Install every extra with `uv sync --all-extras`."
    )
