"""Generate deterministic third-party license notices for Inqtrix."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKDOWN_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
DEFAULT_JSON_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.json"
SURFACE_ORDER = {
    "python": 0,
    "python-dev": 1,
    "react-prod": 2,
    "react-dev": 3,
    "node-prod": 4,
    "node-dev": 5,
}

PYTHON_LICENSE_OVERRIDES: dict[str, tuple[str, str]] = {
    "azure-ai-projects": (
        "MIT",
        "manual override: Azure SDK for Python repository license",
    ),
    "azure-core": (
        "MIT",
        "manual override: Azure SDK for Python repository license",
    ),
    "azure-identity": (
        "MIT",
        "manual override: Azure SDK for Python repository license",
    ),
}

# JavaScript packages that ship a LICENSE file but omit the `license` field from
# package-lock.json, so the npm metadata carries no SPDX id and the automatic
# resolver would fail closed. Keyed by canonical npm name; mirrors
# PYTHON_LICENSE_OVERRIDES. Verify the bundled LICENSE upstream before adding
# an entry (khroma@2.1.0 ships an MIT license file, package.json omits it).
REACT_LICENSE_OVERRIDES: dict[str, tuple[str, str]] = {
    "khroma": (
        "MIT",
        "manual override: bundled LICENSE file; package.json omits the field",
    ),
}


class NoticeGenerationError(RuntimeError):
    """Raised when the notice inventory cannot be generated safely."""


@dataclass(frozen=True)
class NoticeEntry:
    """One resolved third-party package notice entry."""

    ecosystem: str
    name: str
    version: str
    license: str
    dependency_surface: str
    metadata_source: str


PythonMetadataProvider = Callable[[str], Any]


def canonical_name(name: str) -> str:
    """Return the PEP 503/npm-style normalized package key."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_applies(marker: str | None, *, extra: str = "") -> bool:
    """Return whether a PEP 508 marker applies to this generator run.

    Args:
        marker: Optional marker expression from ``uv.lock``.
        extra: Extra name to evaluate for optional dependency groups. The
            default empty string matches normal runtime dependency resolution.

    Raises:
        NoticeGenerationError: If the marker cannot be parsed or the marker
            evaluator is unavailable in the synced development environment.
    """
    if not marker:
        return True
    try:
        from packaging.markers import InvalidMarker, Marker, default_environment
    except ImportError as exc:  # pragma: no cover - release env guard
        raise NoticeGenerationError(
            "The `packaging` package is required to evaluate uv.lock markers. "
            "Run `uv sync --all-extras --dev` before generating notices."
        ) from exc

    environment = default_environment()
    environment["extra"] = extra
    try:
        return Marker(marker).evaluate(environment)
    except InvalidMarker as exc:
        raise NoticeGenerationError(f"Invalid dependency marker {marker!r} in uv.lock.") from exc


def _dependency_names(items: list[dict[str, Any]] | None, *, extra: str = "") -> set[str]:
    return {
        canonical_name(str(item["name"]))
        for item in items or []
        if isinstance(item, dict)
        and item.get("name")
        and _marker_applies(str(item.get("marker") or ""), extra=extra)
    }


def _closure(start: set[str], packages: Mapping[str, Mapping[str, Any]]) -> set[str]:
    seen: set[str] = set()
    pending = list(sorted(start))
    while pending:
        current = pending.pop()
        if current in seen or current not in packages:
            continue
        seen.add(current)
        for dep in _dependency_names(packages[current].get("dependencies")):
            if dep not in seen:
                pending.append(dep)
    return seen


def _metadata_get(metadata: Any, key: str) -> str:
    value = metadata.get(key) if hasattr(metadata, "get") else None
    if isinstance(value, str):
        return value.strip()
    return ""


def _metadata_get_all(metadata: Any, key: str) -> list[str]:
    if hasattr(metadata, "get_all"):
        values = metadata.get_all(key) or []
    elif isinstance(metadata, Mapping):
        raw = metadata.get(key, [])
        values = raw if isinstance(raw, list) else [raw]
    else:
        values = []
    return [str(value).strip() for value in values if str(value).strip()]


def _clean_license_classifier(classifier: str) -> str:
    prefix = "License :: OSI Approved :: "
    if classifier.startswith(prefix):
        return classifier[len(prefix):]
    prefix = "License :: "
    if classifier.startswith(prefix):
        return classifier[len(prefix):]
    return classifier


def _license_from_metadata(name: str, metadata: Any) -> tuple[str, str]:
    expression = _metadata_get(metadata, "License-Expression")
    if expression and expression.upper() != "UNKNOWN":
        return expression, "python package metadata: License-Expression"

    classifiers = [
        _clean_license_classifier(item)
        for item in _metadata_get_all(metadata, "Classifier")
        if item.startswith("License ::")
    ]
    if classifiers:
        return " / ".join(sorted(set(classifiers))), "python package metadata: classifiers"

    license_field = _metadata_get(metadata, "License")
    if license_field and license_field.upper() != "UNKNOWN":
        if "\n" not in license_field and len(license_field) <= 160:
            return license_field, "python package metadata: License"
        return "license text in package metadata", "python package metadata: License"

    raise NoticeGenerationError(
        f"Missing license metadata for Python package {name!r}; "
        "add an explicit override only after verifying the upstream license."
    )


def _default_python_metadata_provider(name: str) -> Any:
    return importlib.metadata.distribution(name).metadata


def collect_python_entries(
    repo_root: Path,
    *,
    metadata_provider: PythonMetadataProvider = _default_python_metadata_provider,
) -> list[NoticeEntry]:
    """Collect Python notices from uv.lock and installed package metadata."""
    lock_path = repo_root / "uv.lock"
    if not lock_path.exists():
        raise NoticeGenerationError("uv.lock not found; cannot build Python notices.")

    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = {
        canonical_name(str(pkg["name"])): pkg
        for pkg in lock.get("package", [])
        if isinstance(pkg, dict) and pkg.get("name")
    }
    root = packages.get("inqtrix")
    if not root:
        raise NoticeGenerationError("uv.lock does not contain the editable inqtrix package.")

    runtime_roots = _dependency_names(root.get("dependencies"))
    dev_roots: set[str] = set()
    for group_name, group in (root.get("optional-dependencies") or {}).items():
        dev_roots.update(_dependency_names(group, extra=str(group_name)))
    for group_name, group in (root.get("dev-dependencies") or {}).items():
        dev_roots.update(_dependency_names(group, extra=str(group_name)))

    runtime = _closure(runtime_roots, packages)
    dev = _closure(dev_roots, packages)
    entries: list[NoticeEntry] = []
    for key in sorted((runtime | dev) - {"inqtrix"}):
        pkg = packages[key]
        name = str(pkg["name"])
        version = str(pkg.get("version", ""))
        if key in PYTHON_LICENSE_OVERRIDES:
            license_id, source = PYTHON_LICENSE_OVERRIDES[key]
        else:
            try:
                metadata = metadata_provider(name)
            except importlib.metadata.PackageNotFoundError as exc:
                raise NoticeGenerationError(
                    f"Installed metadata for Python package {name!r} was not found. "
                    "Run `uv sync --all-extras --dev` before generating notices."
                ) from exc
            license_id, source = _license_from_metadata(name, metadata)
        entries.append(
            NoticeEntry(
                ecosystem="python",
                name=name,
                version=version,
                license=license_id,
                dependency_surface="python" if key in runtime else "python-dev",
                metadata_source=source,
            )
        )
    return entries


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _npm_lock_package_name(path: str) -> str:
    """Derive an npm package name from a lockfile ``packages`` path."""

    marker = "node_modules/"
    if marker not in path:
        return ""
    return path.rsplit(marker, 1)[1]


def _react_package_key(name: str, version: str) -> str:
    return f"{canonical_name(name)}@{version}"


def _react_dependencies(package: Mapping[str, Any]) -> set[str]:
    """Names of the packages npm installs because *package* needs them.

    That includes its peer dependencies: npm installs a required peer with
    the package that requires it, so the peer ships wherever that package
    does. An optional peer is installed only when something else requires
    it, and is followed from there.
    """
    names: set[str] = set()
    for field in ("dependencies", "optionalDependencies"):
        raw = package.get(field, {})
        if isinstance(raw, Mapping):
            names.update(canonical_name(str(name)) for name in raw)
    peers = package.get("peerDependencies", {})
    peer_meta = package.get("peerDependenciesMeta", {})
    if not isinstance(peer_meta, Mapping):
        peer_meta = {}
    if isinstance(peers, Mapping):
        for name in peers:
            meta = peer_meta.get(name)
            if isinstance(meta, Mapping) and meta.get("optional") is True:
                continue
            names.add(canonical_name(str(name)))
    return names


def _react_closure(
    start_names: set[str],
    packages_by_key: Mapping[str, Mapping[str, Any]],
    keys_by_name: Mapping[str, set[str]],
) -> set[str]:
    seen: set[str] = set()
    pending: list[str] = []
    for name in sorted(start_names):
        pending.extend(sorted(keys_by_name.get(name, set())))

    while pending:
        key = pending.pop()
        if key in seen:
            continue
        package = packages_by_key.get(key)
        if not package:
            continue
        seen.add(key)
        for dep_name in _react_dependencies(package):
            for dep_key in sorted(keys_by_name.get(dep_name, set())):
                if dep_key not in seen:
                    pending.append(dep_key)
    return seen


def _license_from_react_package(package: Mapping[str, Any]) -> str:
    raw = package.get("license") or package.get("licenses")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, list) and raw:
        return " / ".join(sorted(str(item) for item in raw if str(item).strip()))
    raise NoticeGenerationError(
        f"Missing license metadata for JavaScript package {package.get('name')!r}; "
        "verify the upstream license before adding an override."
    )


def _manifest_dependency_names(manifest: Mapping[str, Any], field: str) -> set[str]:
    raw = manifest.get(field) or {}
    if not isinstance(raw, Mapping):
        raise NoticeGenerationError(f"package.json field {field!r} must be an object.")
    return {canonical_name(str(name)) for name in raw}


def collect_react_entries(repo_root: Path) -> list[NoticeEntry]:
    """Collect browser and Node workspace notices from the canonical npm lock."""
    root_manifest_path = repo_root / "package.json"
    app_manifest_path = repo_root / "apps" / "research-desk" / "package.json"
    server_manifest_path = repo_root / "apps" / "collaboration-server" / "package.json"
    schema_manifest_path = repo_root / "packages" / "editor-schema" / "package.json"
    lock_path = repo_root / "package-lock.json"
    if not root_manifest_path.exists():
        raise NoticeGenerationError("Root package.json not found.")
    if not app_manifest_path.exists():
        raise NoticeGenerationError("React app package.json not found.")
    if not server_manifest_path.exists():
        raise NoticeGenerationError("Collaboration server package.json not found.")
    if not schema_manifest_path.exists():
        raise NoticeGenerationError("Editor schema package.json not found.")
    if not lock_path.exists():
        raise NoticeGenerationError(
            "package-lock.json not found; cannot build JavaScript notices."
        )

    root_manifest = _read_json(root_manifest_path)
    lock_document = _read_json(lock_path)
    if lock_document.get("lockfileVersion") != 3:
        raise NoticeGenerationError(
            "package-lock.json must use lockfileVersion 3."
        )

    app_manifest = _read_json(app_manifest_path)
    server_manifest = _read_json(server_manifest_path)
    schema_manifest = _read_json(schema_manifest_path)
    schema_prod_roots = _manifest_dependency_names(schema_manifest, "dependencies")
    react_prod_roots = (
        _manifest_dependency_names(app_manifest, "dependencies") | schema_prod_roots
    )
    react_dev_roots = _manifest_dependency_names(app_manifest, "devDependencies")
    node_prod_roots = (
        _manifest_dependency_names(server_manifest, "dependencies") | schema_prod_roots
    )
    node_dev_roots = (
        _manifest_dependency_names(root_manifest, "devDependencies")
        | _manifest_dependency_names(server_manifest, "devDependencies")
        | _manifest_dependency_names(schema_manifest, "devDependencies")
    )

    packages_by_key: dict[str, dict[str, Any]] = {}
    keys_by_name: dict[str, set[str]] = {}
    locked_packages = lock_document.get("packages")
    if not isinstance(locked_packages, Mapping):
        raise NoticeGenerationError("package-lock.json packages must be an object.")
    for path, raw_package in locked_packages.items():
        if not isinstance(path, str) or not isinstance(raw_package, Mapping):
            continue
        name = str(raw_package.get("name") or _npm_lock_package_name(path))
        version = str(raw_package.get("version") or "")
        if not name or not version:
            continue
        package = dict(raw_package)
        package["name"] = name
        key = _react_package_key(name, version)
        packages_by_key.setdefault(key, package)
        keys_by_name.setdefault(canonical_name(name), set()).add(key)

    react_prod = _react_closure(react_prod_roots, packages_by_key, keys_by_name)
    node_prod = _react_closure(node_prod_roots, packages_by_key, keys_by_name)
    react_dev = _react_closure(react_dev_roots, packages_by_key, keys_by_name)
    node_dev = _react_closure(node_dev_roots, packages_by_key, keys_by_name)
    entries: list[NoticeEntry] = []
    for key in sorted(react_prod | node_prod | react_dev | node_dev):
        package = packages_by_key[key]
        name = str(package["name"])
        override = REACT_LICENSE_OVERRIDES.get(canonical_name(name))
        if override is not None:
            license_id, source = override
        else:
            license_id = _license_from_react_package(package)
            source = "package-lock.json metadata"
        if key in react_prod:
            ecosystem = "react"
            dependency_surface = "react-prod"
        elif key in node_prod:
            ecosystem = "node"
            dependency_surface = "node-prod"
        elif key in react_dev:
            ecosystem = "react"
            dependency_surface = "react-dev"
        else:
            ecosystem = "node"
            dependency_surface = "node-dev"
        entries.append(
            NoticeEntry(
                ecosystem=ecosystem,
                name=name,
                version=str(package["version"]),
                license=license_id,
                dependency_surface=dependency_surface,
                metadata_source=source,
            )
        )
    return entries


def build_notice_entries(
    repo_root: Path = REPO_ROOT,
    *,
    python_metadata_provider: PythonMetadataProvider = _default_python_metadata_provider,
) -> list[NoticeEntry]:
    """Build the complete notice inventory for the current checkout."""
    entries = [
        *collect_python_entries(repo_root, metadata_provider=python_metadata_provider),
        *collect_react_entries(repo_root),
    ]
    return sorted(
        entries,
        key=lambda item: (
            SURFACE_ORDER[item.dependency_surface],
            canonical_name(item.name),
            item.version,
        ),
    )


def build_json_document(entries: list[NoticeEntry]) -> str:
    """Render the machine-readable notice inventory."""
    payload = {
        "schema_version": "inqtrix-third-party-notices-v1",
        "generated_by": "scripts/generate_third_party_notices.py",
        "sources": {
            "python_lock": "uv.lock",
            "javascript_lock": "package-lock.json",
            "javascript_note": "package-lock.json is the sole JavaScript dependency source.",
            "javascript_packages": [
                "package.json",
                "apps/research-desk/package.json",
                "apps/collaboration-server/package.json",
                "packages/editor-schema/package.json",
            ],
        },
        "packages": [asdict(entry) for entry in entries],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _markdown_table(entries: list[NoticeEntry]) -> list[str]:
    lines = [
        "| Surface | Package | Version | License | Metadata source |",
        "|---|---|---:|---|---|",
    ]
    for entry in entries:
        lines.append(
            "| "
            + " | ".join(
                [
                    entry.dependency_surface,
                    f"`{entry.name}`",
                    entry.version,
                    entry.license,
                    entry.metadata_source,
                ]
            )
            + " |"
        )
    return lines


def build_markdown_document(entries: list[NoticeEntry]) -> str:
    """Render the human-readable notice inventory."""
    summary = {
        surface: sum(1 for entry in entries if entry.dependency_surface == surface)
        for surface in SURFACE_ORDER
    }
    lines = [
        "# Third-Party Notices",
        "",
        "This file is generated by `uv run python "
        "scripts/generate_third_party_notices.py` or, after the documented "
        "pip installation, `python scripts/generate_third_party_notices.py`.",
        "Do not edit it manually.",
        "",
        "The inventory is provided for license-notice transparency only and is not legal advice.",
        "JavaScript package data is based on every shipping workspace manifest,",
        "and the canonical npm `package-lock.json`.",
        "",
        "## Summary",
        "",
        "| Surface | Packages |",
        "|---|---:|",
    ]
    for surface in SURFACE_ORDER:
        lines.append(f"| {surface} | {summary[surface]} |")
    lines.extend(["", "## Python Dependencies", ""])
    lines.extend(_markdown_table([entry for entry in entries if entry.ecosystem == "python"]))
    lines.extend(["", "## JavaScript Dependencies", ""])
    lines.extend(_markdown_table([entry for entry in entries if entry.ecosystem != "python"]))
    lines.append("")
    return "\n".join(lines)


def build_documents(
    repo_root: Path = REPO_ROOT,
    *,
    python_metadata_provider: PythonMetadataProvider = _default_python_metadata_provider,
) -> tuple[str, str]:
    """Return ``(markdown, json)`` generated for ``repo_root``."""
    entries = build_notice_entries(repo_root, python_metadata_provider=python_metadata_provider)
    return build_markdown_document(entries), build_json_document(entries)


def _write_if_needed(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def write_outputs(
    repo_root: Path = REPO_ROOT,
    *,
    python_metadata_provider: PythonMetadataProvider = _default_python_metadata_provider,
) -> None:
    """Write generated notice files to the repository root."""
    markdown, json_document = build_documents(
        repo_root,
        python_metadata_provider=python_metadata_provider,
    )
    _write_if_needed(repo_root / DEFAULT_MARKDOWN_PATH.name, markdown)
    _write_if_needed(repo_root / DEFAULT_JSON_PATH.name, json_document)


def check_outputs(
    repo_root: Path = REPO_ROOT,
    *,
    python_metadata_provider: PythonMetadataProvider = _default_python_metadata_provider,
) -> list[Path]:
    """Return generated files whose checked-in content is stale."""
    markdown, json_document = build_documents(
        repo_root,
        python_metadata_provider=python_metadata_provider,
    )
    expected = {
        repo_root / DEFAULT_MARKDOWN_PATH.name: markdown,
        repo_root / DEFAULT_JSON_PATH.name: json_document,
    }
    stale: list[Path] = []
    for path, content in expected.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            stale.append(path)
    return stale


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    try:
        if args.check:
            stale = check_outputs(repo_root)
            if stale:
                paths = ", ".join(str(path.relative_to(repo_root)) for path in stale)
                print(
                    f"Third-party notices are stale: {paths}. "
                    "Run `uv run python scripts/generate_third_party_notices.py` "
                    "or `python scripts/generate_third_party_notices.py` in the "
                    "documented pip environment.",
                    file=sys.stderr,
                )
                return 1
            return 0
        write_outputs(repo_root)
        return 0
    except NoticeGenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
