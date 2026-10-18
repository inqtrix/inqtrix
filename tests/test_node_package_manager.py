"""Repository contracts for the single supported npm installation path."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFESTS = (
    ROOT / "package.json",
    ROOT / "apps" / "collaboration-server" / "package.json",
    ROOT / "apps" / "research-desk" / "package.json",
    ROOT / "packages" / "editor-schema" / "package.json",
)


def test_npm_is_the_only_javascript_dependency_source() -> None:
    """Keep one manifest/lock contract for every Node workspace."""
    dockerfiles = (
        ROOT / "deploy" / "docker" / "Dockerfile.collaboration",
        ROOT / "deploy" / "docker" / "Dockerfile.web",
    )

    assert (ROOT / "package-lock.json").is_file()
    assert not (ROOT / ("pnpm" + "-lock.yaml")).exists()
    assert not (ROOT / ("pnpm" + "-workspace.yaml")).exists()
    assert list((ROOT / "patches").glob("*.patch")) == []
    assert all(
        "COPY patches" not in path.read_text(encoding="utf-8")
        for path in dockerfiles
    )


def test_workspace_manifests_declare_only_npm() -> None:
    for manifest_path in WORKSPACE_MANIFESTS:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        engines = manifest.get("engines", {})
        assert engines.get("npm") == ">=10.9.0"
        removed_manager = "pn" + "pm"
        assert removed_manager not in engines
        assert not str(manifest.get("packageManager", "")).startswith(
            removed_manager + "@"
        )


def test_root_ui_commands_use_native_npm_workspaces() -> None:
    """Do not reintroduce a custom process wrapper around ordinary npm runs."""
    manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    ui_scripts = {
        name: command
        for name, command in manifest["scripts"].items()
        if name.startswith("ui:")
    }

    assert not (ROOT / "scripts" / "run_research_desk_ui.mjs").exists()
    assert all(
        "scripts/run_research_desk_ui.mjs" not in command
        for command in ui_scripts.values()
    )
    assert ui_scripts["ui:build"] == (
        "npm --workspace @inqtrix/research-desk run build"
    )
