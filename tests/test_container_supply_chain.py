"""Static release contracts for first-party container images."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _ROOT / ".github" / "workflows"
_WORKFLOW_ARCHIVE = _ROOT / "docs" / "development" / "archived-github-actions"


def test_source_tree_contains_no_active_github_workflow_yaml() -> None:
    """A branch push cannot execute a workflow defined by this checkout."""
    active = [
        path
        for pattern in ("*.yml", "*.yaml")
        for path in _WORKFLOWS.glob(pattern)
    ]
    assert active == []


def test_disabled_workflow_snapshots_match_the_documented_checksums() -> None:
    """The quarantine remains immutable audit evidence, not executable YAML."""
    expected = {
        "ci.yml.disabled": (
            "1c2789f26b65316c8fac2d132efc0f27"
            "aa0cd5e27afababd52e9958cd3973dc2"
        ),
        "release-images.yml.disabled": (
            "6d7ef47243e77861fa63534c548973a9c"
            "a1dc0556cb290231d2842733ec59c85"
        ),
    }
    readme = (_WORKFLOW_ARCHIVE / "README.md").read_text(encoding="utf-8")

    assert {
        path.name
        for path in _WORKFLOW_ARCHIVE.iterdir()
        if path.is_file() and path.name != "README.md"
    } == set(expected)
    for filename, digest in expected.items():
        path = _WORKFLOW_ARCHIVE / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert digest in readme

    assert "not supported execution templates" in readme
    assert "arm64 image was not scanned" in readme


_COMPOSE_OVERRIDE = re.compile(r"^\$\{[A-Z0-9_]+:-(?P<default>.+)\}$")


def test_production_container_references_are_qualified_and_immutable() -> None:
    """Compose and bundled Helm defaults reject mutable image references."""
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    for name, service in compose["services"].items():
        image = service.get("image")
        if image is None:
            continue
        # An operator-overridable image must still SHIP a pinned default, so
        # the reference is asserted on what the stack runs unattended. The
        # override itself is an explicit act that carries its own pin, exactly
        # like `--set valkey.image=` on the Helm side.
        override = _COMPOSE_OVERRIDE.match(image)
        if override is not None:
            image = override.group("default")
        assert "$" not in image, f"{name} resolves to an uncontrolled reference"
        assert "/" in image, f"{name} uses an unqualified registry path"
        assert "@sha256:" in image, f"{name} uses a mutable image reference"
        assert not re.search(r":(?:latest|stable)(?:@|$)", image)

    values = yaml.safe_load(
        (_ROOT / "deploy" / "helm" / "inqtrix" / "values.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert values["image"]["allowUnpinned"] is False
    for component in ("postgres", "pgbouncer", "qdrant", "valkey", "s3"):
        image = values[component]["image"]
        match = re.fullmatch(
            r"(?P<repository>[^@\s]+):(?P<tag>[^:@\s]+)"
            r"@(?P<digest>sha256:[0-9a-f]{64})",
            image,
        )
        assert match, f"unexpected image contract for {component}"
        repository = match.group("repository")
        tag = match.group("tag")
        digest = match.group("digest")
        assert "/" in repository
        assert tag not in {"latest", "stable"}
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)


def _compose_service_images() -> dict[str, str]:
    """Compose service images with operator overrides resolved to defaults."""
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    images: dict[str, str] = {}
    for name, service in compose["services"].items():
        image = service.get("image")
        if image is None:
            continue
        override = _COMPOSE_OVERRIDE.match(image)
        images[name] = override.group("default") if override else image
    return images


def _helm_component_images() -> dict[str, str]:
    values = yaml.safe_load(
        (_ROOT / "deploy" / "helm" / "inqtrix" / "values.yaml").read_text(
            encoding="utf-8"
        )
    )
    return {
        component: values[component]["image"]
        for component in ("postgres", "pgbouncer", "qdrant", "valkey", "s3")
    }


# Bundled services that must run the SAME build on both deployment paths.
# Compose service name -> Helm component key.
_SHARED_BUNDLED_IMAGES = {
    "postgres": "postgres",
    "pgbouncer": "pgbouncer",
    "qdrant": "qdrant",
    "valkey": "valkey",
}


def test_bundled_service_images_match_between_compose_and_helm() -> None:
    """Docker and Kubernetes cannot silently drift onto different builds.

    Nothing structural ties the two pins together, so a version bump applied
    to one file and forgotten in the other produces a version skew that only
    shows up as behaviour differing per deployment path.
    """
    compose_images = _compose_service_images()
    helm_images = _helm_component_images()

    for service, component in _SHARED_BUNDLED_IMAGES.items():
        assert compose_images[service] == helm_images[component], (
            f"{service}: compose pins {compose_images[service]!r} but Helm "
            f"pins {helm_images[component]!r}; bump both or neither"
        )


def test_object_store_image_divergence_stays_deliberate() -> None:
    """The object store is the one bundled service that is NOT the same image.

    Compose bundles SeaweedFS while the chart bundles MinIO. That is a real
    product difference, not drift, so parity is deliberately not asserted for
    it above. This test pins the divergence so that unifying the two later is
    a conscious edit here rather than a silently weakened contract.
    """
    compose_images = _compose_service_images()
    helm_images = _helm_component_images()

    assert compose_images["seaweedfs"].startswith("docker.io/chrislusf/seaweedfs:")
    assert helm_images["s3"].startswith("docker.io/minio/minio:")
    assert "s3" not in _SHARED_BUNDLED_IMAGES.values()
