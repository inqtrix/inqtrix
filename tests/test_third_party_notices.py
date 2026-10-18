"""Tests for generated third-party notice inventory."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_generator() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "generate_third_party_notices.py"
    spec = importlib.util.spec_from_file_location("generate_third_party_notices", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load notice generator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fixture_repo(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {"root-tool": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "fastapi"
version = "1.0.0"
dependencies = [
    { name = "starlette" },
]

[[package]]
name = "inqtrix"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "fastapi" },
    { name = "windows-only", marker = "python_version == '0'" },
]

[package.optional-dependencies]
dev = [
    { name = "pytest" },
]
ui = []

[package.dev-dependencies]
dev = []

[[package]]
name = "pytest"
version = "2.0.0"

[[package]]
name = "starlette"
version = "1.0.0"

[[package]]
name = "windows-only"
version = "1.0.0"
""",
        encoding="utf-8",
    )
    app_root = root / "apps" / "research-desk"
    app_root.mkdir(parents=True)
    (app_root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "1.0.0"},
                "devDependencies": {"vite": "2.0.0"},
            }
        ),
        encoding="utf-8",
    )
    server_root = root / "apps" / "collaboration-server"
    server_root.mkdir(parents=True)
    (server_root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"server-runtime": "1.0.0"},
                "devDependencies": {"server-tool": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    schema_root = root / "packages" / "editor-schema"
    schema_root.mkdir(parents=True)
    (schema_root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"shared-schema-runtime": "1.0.0"},
                "devDependencies": {"schema-tool": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    packages = {
        "react@1.0.0": {
            "name": "react",
            "version": "1.0.0",
            "license": "MIT",
            "dependencies": {"loose-envify": "1.0.0"},
            "peerDependencies": {"react-peer": "1.0.0", "typing-peer": "1.0.0"},
            "peerDependenciesMeta": {"typing-peer": {"optional": True}},
        },
        "react-peer@1.0.0": {
            "name": "react-peer",
            "version": "1.0.0",
            "license": "MIT",
            "peer": True,
        },
        "typing-peer@1.0.0": {
            "name": "typing-peer",
            "version": "1.0.0",
            "license": "MIT",
            "dev": True,
        },
        "loose-envify@1.0.0": {
            "name": "loose-envify",
            "version": "1.0.0",
            "license": "MIT",
        },
        "vite@2.0.0": {
            "name": "vite",
            "version": "2.0.0",
            "license": "MIT",
            "dependencies": {"rollup": "1.0.0"},
        },
        "rollup@1.0.0": {
            "name": "rollup",
            "version": "1.0.0",
            "license": "Apache-2.0",
            "peerDependencies": {"typing-peer": "1.0.0"},
        },
        "server-runtime@1.0.0": {
            "name": "server-runtime",
            "version": "1.0.0",
            "license": "MIT",
        },
        "shared-schema-runtime@1.0.0": {
            "name": "shared-schema-runtime",
            "version": "1.0.0",
            "license": "MIT",
        },
        "root-tool@1.0.0": {
            "name": "root-tool",
            "version": "1.0.0",
            "license": "MIT",
        },
        "server-tool@1.0.0": {
            "name": "server-tool",
            "version": "1.0.0",
            "license": "MIT",
        },
        "schema-tool@1.0.0": {
            "name": "schema-tool",
            "version": "1.0.0",
            "license": "MIT",
        },
    }
    lock_packages: dict[str, Any] = {
        "": {
            "name": "inqtrix",
            "devDependencies": {"root-tool": "1.0.0"},
        }
    }
    for package in packages.values():
        lock_packages[f"node_modules/{package['name']}"] = {
            key: value
            for key, value in package.items()
            if key != "name"
        }
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "inqtrix",
                "lockfileVersion": 3,
                "requires": True,
                "packages": lock_packages,
            }
        ),
        encoding="utf-8",
    )


def _metadata_provider(name: str) -> dict[str, Any]:
    return {
        "fastapi": {"License-Expression": "MIT"},
        "pytest": {"Classifier": ["License :: OSI Approved :: MIT License"]},
        "starlette": {"License-Expression": "BSD-3-Clause"},
    }[name]


def test_notice_documents_are_deterministic(tmp_path):
    generator = _load_generator()
    _write_fixture_repo(tmp_path)

    markdown, json_document = generator.build_documents(
        tmp_path,
        python_metadata_provider=_metadata_provider,
    )
    markdown_again, json_again = generator.build_documents(
        tmp_path,
        python_metadata_provider=_metadata_provider,
    )

    assert markdown == markdown_again
    assert json_document == json_again
    payload = json.loads(json_document)
    assert payload["schema_version"] == "inqtrix-third-party-notices-v1"
    assert {
        (item["name"], item["dependency_surface"])
        for item in payload["packages"]
    } == {
        ("fastapi", "python"),
        ("starlette", "python"),
        ("pytest", "python-dev"),
        ("react", "react-prod"),
        ("loose-envify", "react-prod"),
        # npm installs a peer dependency with the package that requires it,
        # so it ships with it; an optional one only comes with a package
        # that requires it, here a development tool.
        ("react-peer", "react-prod"),
        ("typing-peer", "react-dev"),
        ("vite", "react-dev"),
        ("rollup", "react-dev"),
        ("server-runtime", "node-prod"),
        ("shared-schema-runtime", "react-prod"),
        ("root-tool", "node-dev"),
        ("server-tool", "node-dev"),
        ("schema-tool", "node-dev"),
    }
    assert (
        "| react-prod | `react` | 1.0.0 | MIT | package-lock.json metadata |"
        in markdown
    )


def test_missing_license_metadata_fails_loudly(tmp_path):
    generator = _load_generator()
    _write_fixture_repo(tmp_path)

    def incomplete_metadata(name: str) -> dict[str, Any]:
        if name == "starlette":
            return {}
        return _metadata_provider(name)

    with pytest.raises(generator.NoticeGenerationError, match="Missing license metadata"):
        generator.build_documents(
            tmp_path,
            python_metadata_provider=incomplete_metadata,
        )


def test_check_outputs_detects_stale_generated_files(tmp_path):
    generator = _load_generator()
    _write_fixture_repo(tmp_path)

    generator.write_outputs(tmp_path, python_metadata_provider=_metadata_provider)
    assert generator.check_outputs(
        tmp_path,
        python_metadata_provider=_metadata_provider,
    ) == []

    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text("stale\n", encoding="utf-8")

    stale = generator.check_outputs(
        tmp_path,
        python_metadata_provider=_metadata_provider,
    )
    assert [path.name for path in stale] == ["THIRD_PARTY_NOTICES.md"]
