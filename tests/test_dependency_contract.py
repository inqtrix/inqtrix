"""Repository-level dependency source-of-truth checks."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_NAME = re.compile(r"^([A-Za-z0-9_.-]+)")
_SPECIFIER = re.compile(r"([!<=>~].*)$")
_QDRANT_SERVER_PIN = re.compile(
    r"docker\.io/qdrant/qdrant:v(\d+)\.(\d+)\.\d+@sha256:[0-9a-f]{64}"
)
_QDRANT_SERVER_PIN_FILES = (
    "deploy/compose/compose.stack.yaml",
    "deploy/helm/inqtrix/values.yaml",
)


def _requirement_parts(value: str) -> tuple[str, str]:
    name_match = _NAME.match(value)
    assert name_match is not None, f"invalid requirement: {value!r}"
    specifier_match = _SPECIFIER.search(value)
    specifier = specifier_match.group(1) if specifier_match else ""
    return name_match.group(1).lower().replace("_", "-"), specifier


def test_web_gateway_group_matches_project_constraint_ranges() -> None:
    """The minimal image projection must not become a second version policy."""

    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    project_requirements = dict(
        _requirement_parts(value) for value in document["project"]["dependencies"]
    )
    gateway_requirements = dict(
        _requirement_parts(value)
        for value in document["dependency-groups"]["web-gateway"]
    )

    assert gateway_requirements
    assert gateway_requirements.keys() <= project_requirements.keys()
    for name, specifier in gateway_requirements.items():
        assert specifier == project_requirements[name], (
            f"{name} differs between project dependencies "
            f"({project_requirements[name]!r}) and web-gateway ({specifier!r})"
        )


def test_qdrant_client_floor_tracks_the_bundled_server_minor() -> None:
    """Qdrant upgrades the SDK before the server.

    An older client against a newer server is the unsupported direction, so
    raising the bundled server pin without the client floor lets an install
    without the lock pair a stale client with the new server.
    """

    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    requirements = dict(
        _requirement_parts(value)
        for value in document["project"]["optional-dependencies"]["knowledge-qdrant"]
    )
    floor = re.match(r">=(\d+)\.(\d+)", requirements["qdrant-client"])
    assert floor is not None, f"no floor in {requirements['qdrant-client']!r}"

    for path in _QDRANT_SERVER_PIN_FILES:
        pins = _QDRANT_SERVER_PIN.findall((_ROOT / path).read_text())
        assert len(pins) == 1, f"{path} must pin exactly one Qdrant server image"
        assert pins[0] == floor.groups(), (
            f"qdrant-client floor {requirements['qdrant-client']!r} does not track "
            f"the server minor {'.'.join(pins[0])} pinned in {path}"
        )


def test_dependency_groups_do_not_duplicate_the_pip_dev_extra() -> None:
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert "dev" in document["project"]["optional-dependencies"]
    assert "dev" not in document.get("dependency-groups", {})


def test_python_uses_only_the_project_lock() -> None:
    assert (_ROOT / "uv.lock").is_file()
    assert not (
        _ROOT / ("deploy/docker/requirements.web-" + "launcher.lock")
    ).exists()


def test_pytest_does_not_collect_manual_live_smoke_scripts() -> None:
    """A default suite must never import dotenv-backed scripts as tests."""
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())

    assert document["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def _module_pytestmark(path: Path) -> str | None:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            return ast.unparse(node.value)
    return None


def test_infrastructure_bound_modules_use_the_registered_markers() -> None:
    """One gate decides whether the persistence proofs run, not 25 copies."""
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    markers = document["tool"]["pytest"]["ini_options"]["markers"]
    assert {entry.split(":", 1)[0] for entry in markers} >= {"postgres", "qdrant"}

    expected = {
        path: "pytest.mark.postgres"
        for path in (_ROOT / "tests" / "storage").glob("*_postgres.py")
    }
    expected[_ROOT / "tests" / "test_qdrant_store.py"] = "pytest.mark.qdrant"

    for path, marker in expected.items():
        assert _module_pytestmark(path) == marker, (
            f"{path.name} must carry {marker}; the gate lives in tests/conftest.py"
        )
