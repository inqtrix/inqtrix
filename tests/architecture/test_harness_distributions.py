"""What the kernel may load: its layers reach only the distributions they name."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

from tests.architecture.import_graph import PACKAGE_ROOT, SOURCE_ROOT, iter_module_files

ALLOWED_DISTRIBUTIONS = {
    "inqtrix.harness.contracts": {"pydantic", "pydantic-core", "typing-extensions"},
    "inqtrix.harness.runtime": {
        "langchain",
        "langchain-core",
        "langgraph",
        "opentelemetry-api",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
    },
    "inqtrix.harness.testing": {
        "langchain",
        "langchain-core",
        "langgraph",
        "opentelemetry-api",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
    },
    "inqtrix.harness": {"opentelemetry-api", "pydantic", "pydantic-core", "typing-extensions"},
}


def _allowed_for(module: str) -> set[str]:
    for prefix in ("inqtrix.harness.contracts", "inqtrix.harness.runtime", "inqtrix.harness.testing"):
        if module == prefix or module.startswith(f"{prefix}."):
            return ALLOWED_DISTRIBUTIONS[prefix]
    return ALLOWED_DISTRIBUTIONS["inqtrix.harness"]


def foreign_distributions(module: str, source: str) -> set[str]:
    """Return the third-party top-level imports a kernel module may not have."""
    distributions = packages_distributions()
    found = set()
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top == "inqtrix":
                continue
            owners = {owner.lower() for owner in distributions.get(top, [top])}
            if not owners & _allowed_for(module):
                found.add(top)
    return found


def _kernel_modules() -> list[tuple[str, Path]]:
    return [
        (module, path)
        for module, path in iter_module_files()
        if module == "inqtrix.harness" or module.startswith("inqtrix.harness.")
    ]


def test_the_kernel_exists_and_its_layers_stay_within_their_distributions() -> None:
    modules = _kernel_modules()
    assert modules
    foreign = {
        module: names
        for module, path in modules
        if (names := foreign_distributions(module, path.read_text(encoding="utf-8")))
    }
    assert not foreign, foreign


@pytest.mark.parametrize(
    ("module", "source", "foreign"),
    [
        ("inqtrix.harness.contracts.x", "import pydantic\nimport json", set()),
        ("inqtrix.harness.contracts.x", "import langchain_core", {"langchain_core"}),
        ("inqtrix.harness.contracts.x", "import opentelemetry", {"opentelemetry"}),
        ("inqtrix.harness.dispatch", "import httpx", {"httpx"}),
        ("inqtrix.harness.dispatch", "import langgraph.types", {"langgraph"}),
        ("inqtrix.harness.runtime.x", "import langgraph.types\nimport langchain_core", set()),
    ],
)
def test_a_foreign_distribution_is_seen(module: str, source: str, foreign: set[str]) -> None:
    assert foreign_distributions(module, source) == foreign


def _loads(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )


_PROBE = textwrap.dedent(
    f"""
    import sys
    sys.path.insert(0, {str(SOURCE_ROOT)!r})
    import inqtrix.harness
    print(sorted(
        name for name in ("inqtrix.harness.runtime", "langchain", "langchain_core", "langgraph")
        if name in sys.modules
    ))
    """
)


def test_importing_the_facade_loads_no_framework() -> None:
    result = _loads(_PROBE)
    assert result.returncode == 0, result.stderr[-400:]
    assert result.stdout.strip() == "[]"


def test_the_facade_does_not_need_opentelemetry() -> None:
    """The development extra always installs it, so its absence has to be simulated."""
    code = textwrap.dedent(
        f"""
        import importlib.abc
        import sys

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "opentelemetry" or name.startswith("opentelemetry."):
                    raise ModuleNotFoundError(name)
                return None

        sys.meta_path.insert(0, Block())
        sys.path.insert(0, {str(SOURCE_ROOT)!r})
        sys.modules["inqtrix"] = type(sys)("inqtrix")
        sys.modules["inqtrix"].__path__ = [{str(PACKAGE_ROOT)!r}]
        import inqtrix.harness
        import inqtrix.harness.contracts.events
        print("ok")
        """
    )
    result = _loads(code)
    assert result.returncode == 0, result.stderr[-400:]
    assert result.stdout.strip() == "ok"
