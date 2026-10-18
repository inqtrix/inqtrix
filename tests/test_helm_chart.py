"""Contract tests for the Kubernetes Helm chart (deploy/helm/inqtrix).

These render the chart with ``helm template`` and assert the invariants the
deployment relies on: a pinned non-root UID and fsGroup, bundled-service
auto-wiring, the topology checks around the default worker (its Postgres,
Valkey and shared object-store prerequisites, and the one-replica limit of
the single-pod variant), the fail-fast guard for a misconfigured database,
and secret-change rollout. They are skipped when the ``helm`` binary is not
on PATH (e.g. the offline CI image), exactly as the replay tests skip without
cassettes -- the chart logic is only verifiable against the real templating
engine.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent / "deploy" / "helm" / "inqtrix"
_ROOT = _CHART.parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not available"
)


def _template(
    *set_args: str,
    extra: list[str] | None = None,
    allow_unpinned: bool = True,
    inject_bundled_credentials: bool = True,
    single_pod: bool = True,
) -> str:
    """Render the chart, raising on a Helm error.

    Existing functional tests model local chart development and opt into
    tag-only first-party images. Dedicated supply-chain tests exercise the
    production default with ``allow_unpinned=False``.

    The chart default runs the worker, which needs Valkey and a shared object
    store on top of the database. A test that says nothing about the worker
    exercises something independent of it, so by default it renders the
    single-pod variant: ``worker.enabled=false`` is appended unless an
    argument already sets ``worker.enabled``. Tests of the default topology
    pass ``single_pod=False`` and supply its prerequisites themselves. The
    appended ``--set`` also overrides a values file passed through ``extra``.
    """
    effective_set_args = list(set_args)
    if single_pod and not any(
        item.startswith("worker.enabled=") for item in effective_set_args
    ):
        effective_set_args.append("worker.enabled=false")
    if inject_bundled_credentials:
        bundled_credentials = (
            (
                "postgres.enabled=true",
                "postgres.auth.password=",
                "postgres.auth.password=SyntheticPostgres2026",
            ),
            (
                "valkey.enabled=true",
                "valkey.password=",
                "valkey.password=SyntheticValkey2026",
            ),
            (
                "qdrant.enabled=true",
                "qdrant.apiKey=",
                "qdrant.apiKey=SyntheticQdrant2026",
            ),
            (
                "s3.enabled=true",
                "s3.secretKey=",
                "s3.secretKey=SyntheticMinio2026",
            ),
        )
        for enabled, prefix, credential in bundled_credentials:
            if enabled in effective_set_args and not any(
                item.startswith(prefix) for item in effective_set_args
            ):
                effective_set_args.append(credential)

    cmd = ["helm", "template", "rel", str(_CHART)]
    if allow_unpinned:
        cmd += ["--set", "image.allowUnpinned=true"]
    for item in effective_set_args:
        cmd += ["--set", item]
    if extra:
        cmd += extra
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _docs(rendered: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _node_collaboration_env_names() -> set[str]:
    """Return environment names consumed by the Node settings parser."""
    source = (
        _ROOT / "apps" / "collaboration-server" / "src" / "config.ts"
    ).read_text(encoding="utf-8")
    return set(
        re.findall(
            r"INQTRIX_(?:API_INTERNAL_URL|COLLABORATION_[A-Z0-9_]+)",
            source,
        )
    )


# A database source is mandatory for the default (postgres) storage backend, so
# every positive-path render supplies one.
_EXTERNAL = "secret.existingSecret=app-secret"
_MANAGED_EXTERNAL = (
    "secret.data.INQTRIX_DATABASE_URL="
    "postgresql+asyncpg://runtime:SyntheticDatabase2026@database:5432/inqtrix"
)


@pytest.mark.parametrize(
    ("set_args", "credential_name"),
    [
        (("postgres.enabled=true",), "postgres.auth.password"),
        (
            (_MANAGED_EXTERNAL, "valkey.enabled=true"),
            "valkey.password",
        ),
        (
            (_MANAGED_EXTERNAL, "qdrant.enabled=true"),
            "qdrant.apiKey",
        ),
        (
            (_MANAGED_EXTERNAL, "s3.enabled=true"),
            "s3.secretKey",
        ),
    ],
)
def test_bundled_services_require_explicit_credentials(
    set_args: tuple[str, ...],
    credential_name: str,
) -> None:
    with pytest.raises(RuntimeError, match=credential_name):
        _template(
            *set_args,
            inject_bundled_credentials=False,
        )


@pytest.mark.parametrize(
    ("set_args", "credential_name"),
    [
        (
            (
                "postgres.enabled=true",
                "postgres.auth.password=change-me-postgres",
            ),
            "postgres.auth.password",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "valkey.enabled=true",
                "valkey.password=change-me-valkey",
            ),
            "valkey.password",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "qdrant.enabled=true",
                "qdrant.apiKey=change-me-qdrant",
            ),
            "qdrant.apiKey",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "s3.enabled=true",
                "s3.secretKey=change-me-minio",
            ),
            "s3.secretKey",
        ),
    ],
)
def test_bundled_services_reject_placeholder_credentials(
    set_args: tuple[str, ...],
    credential_name: str,
) -> None:
    with pytest.raises(RuntimeError, match=credential_name):
        _template(
            *set_args,
            inject_bundled_credentials=False,
        )


def test_bundled_services_require_a_selected_secret() -> None:
    with pytest.raises(RuntimeError, match="secret.create=true"):
        _template(
            "postgres.enabled=true",
            "secret.create=false",
        )


@pytest.mark.parametrize(
    ("set_args", "message"),
    [
        (
            (
                "postgres.enabled=true",
                _MANAGED_EXTERNAL,
                "postgres.auth.password=",
            ),
            "bundled Postgres credentials",
        ),
        (
            (
                "postgres.enabled=true",
                "postgres.auth.password=SyntheticPostgres2026",
                "secret.data.INQTRIX_BUNDLED_POSTGRES_PASSWORD=Different2026",
            ),
            "bundled Postgres credentials",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "valkey.enabled=true",
                "valkey.password=SyntheticValkey2026",
                "secret.data.INQTRIX_BUNDLED_VALKEY_PASSWORD=Different2026",
            ),
            "bundled Valkey credentials",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "qdrant.enabled=true",
                "qdrant.apiKey=SyntheticQdrant2026",
                "secret.data.INQTRIX_QDRANT_API_KEY=change-me-qdrant",
            ),
            "bundled Qdrant credentials",
        ),
        (
            (
                _MANAGED_EXTERNAL,
                "s3.enabled=true",
                "s3.secretKey=SyntheticMinio2026",
                "secret.data.INQTRIX_S3_SECRET_KEY=DifferentMinio2026",
            ),
            "bundled MinIO credentials",
        ),
    ],
)
def test_bundled_credentials_reject_parallel_secret_data_sources(
    set_args: tuple[str, ...],
    message: str,
) -> None:
    """A bundled service cannot drift from its chart-derived connection."""
    with pytest.raises(RuntimeError, match=message):
        _template(*set_args, inject_bundled_credentials=False)


def test_null_bundled_credential_never_renders_literal_nil() -> None:
    """YAML null is empty input, not the credential string ``<nil>``."""
    with pytest.raises(RuntimeError, match="qdrant.apiKey"):
        _template(
            _MANAGED_EXTERNAL,
            "qdrant.enabled=true",
            extra=["--set-json", "qdrant.apiKey=null"],
            inject_bundled_credentials=False,
        )


def test_external_secret_remains_valid_for_bundled_service_credentials() -> None:
    """Helm cannot inspect an operator-managed Secret and does not invent data."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "qdrant.enabled=true",
            inject_bundled_credentials=False,
        )
    )
    qdrant = next(
        item
        for item in _by_kind(docs, "StatefulSet")
        if item["metadata"]["name"].endswith("-qdrant")
    )
    secret_ref = qdrant["spec"]["template"]["spec"]["containers"][0]["env"][
        -1
    ]["valueFrom"]["secretKeyRef"]
    assert secret_ref == {
        "name": "app-secret",
        "key": "INQTRIX_QDRANT_API_KEY",
    }


def test_chart_managed_secret_rejects_placeholder_application_values() -> None:
    """Non-bundled secret.data values follow the same fail-closed rule."""
    with pytest.raises(
        RuntimeError,
        match=r"secret\.data\.INQTRIX_DATABASE_URL",
    ):
        _template(
            "secret.data.INQTRIX_DATABASE_URL=change-me-database",
            inject_bundled_credentials=False,
        )


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ("postgres.auth.username=user/name", "postgres.auth.username"),
        ("postgres.auth.database=", "postgres.auth.database"),
    ],
)
def test_bundled_postgres_rejects_ambiguous_dsn_components(
    argument: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _template("postgres.enabled=true", argument)


def test_production_images_require_immutable_digests() -> None:
    """The chart's default refuses mutable first-party release references."""
    with pytest.raises(RuntimeError, match="image.api.digest is required"):
        _template(_EXTERNAL, allow_unpinned=False)


def test_first_party_images_render_tag_and_digest() -> None:
    """Pinned references retain a readable tag and immutable sha256 identity."""
    api_digest = f"sha256:{'a' * 64}"
    web_digest = f"sha256:{'b' * 64}"
    collaboration_digest = f"sha256:{'c' * 64}"
    docs = _docs(
        _template(
            _EXTERNAL,
            "collaboration.enabled=true",
            "collaboration.secret.existingSecret=collaboration-secret",
            f"image.api.digest={api_digest}",
            f"image.web.digest={web_digest}",
            f"image.collaboration.digest={collaboration_digest}",
            allow_unpinned=False,
        )
    )
    images = {
        container["name"]: container["image"]
        for deployment in _by_kind(docs, "Deployment")
        for container in deployment["spec"]["template"]["spec"]["containers"]
    }
    assert images["api"] == (
        f"ghcr.io/inqtrix/inqtrix-api:0.2.0@{api_digest}"
    )
    assert images["web"] == (
        f"ghcr.io/inqtrix/inqtrix-web:0.2.0@{web_digest}"
    )
    assert images["collaboration"] == (
        "ghcr.io/inqtrix/inqtrix-collaboration:"
        f"0.2.0@{collaboration_digest}"
    )


def test_invalid_first_party_digest_fails_render() -> None:
    """Malformed digest values never reach a Kubernetes workload."""
    with pytest.raises(RuntimeError, match="64 lowercase hex"):
        _template(
            _EXTERNAL,
            "image.api.digest=sha256:NOT-A-DIGEST",
            allow_unpinned=False,
        )


def test_bundled_images_are_digest_pinned() -> None:
    """Every chart-bundled demo dependency has an immutable upstream identity."""
    docs = _docs(
        _template(
            "postgres.enabled=true",
            "pgbouncer.enabled=true",
            "qdrant.enabled=true",
            "valkey.enabled=true",
            "s3.enabled=true",
        )
    )
    images = [
        container["image"]
        for workload in (
            _by_kind(docs, "Deployment") + _by_kind(docs, "StatefulSet")
        )
        for container in workload["spec"]["template"]["spec"]["containers"]
    ]
    bundled = [
        image
        for image in images
        if any(
            marker in image
            for marker in (
                "postgres:",
                "pgbouncer:",
                "qdrant:",
                "valkey:",
                "minio:",
            )
        )
    ]
    assert len(bundled) == 5
    assert all("@sha256:" in image for image in bundled)


def test_bare_install_fails_without_database():
    """postgres backend (the default) with no DB source must fail at render time,
    not silently produce an empty INQTRIX_DATABASE_URL. The database is named
    first, before the worker's further prerequisites."""
    with pytest.raises(RuntimeError, match="no database is configured"):
        _template(single_pod=False)


def test_memory_backend_needs_no_database():
    rendered = _template(
        "config.INQTRIX_STORAGE_BACKEND=memory", "migrations.enabled=false"
    )
    assert _by_kind(_docs(rendered), "Deployment")


def test_collaboration_is_absent_by_default() -> None:
    """The default release keeps every legacy resource and config path off."""
    docs = _docs(_template(_EXTERNAL))
    names = {d.get("metadata", {}).get("name", "") for d in docs}
    assert not any(name.endswith("-collaboration") for name in names)

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert "INQTRIX_COLLABORATION_ENABLED" not in config
    assert "INQTRIX_COLLABORATION_HTTP_URL" not in config
    assert "INQTRIX_COLLABORATION_WS_URL" not in config

    api = next(
        d
        for d in _by_kind(docs, "Deployment")
        if d["metadata"]["name"].endswith("-api")
    )
    api_env = api["spec"]["template"]["spec"]["containers"][0].get(
        "env", []
    )
    assert api["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert not any(
        item["name"] == "INQTRIX_COLLABORATION_SECRET" for item in api_env
    )


def test_collaboration_renders_one_private_recreate_replica() -> None:
    """Enabled collaboration is private, secret-scoped, and never scalable."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "collaboration.enabled=true",
            "collaboration.replicaCount=4",
            "collaboration.secret.existingSecret=collaboration-secret",
            "config.INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS=7",
            "config.INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS=9",
            "config.INQTRIX_COLLABORATION_MAX_FRAME_BYTES=3145728",
            "config.INQTRIX_COLLABORATION_MAX_QUEUED_BYTES=524288",
            "config.INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES=11",
            "config.INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS=17",
            "config.INQTRIX_COLLABORATION_UPDATE_RATE_COUNT=77",
            "config.INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS=300",
            "config.INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS=9000",
            "config.INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES=262144",
            "config.INQTRIX_COLLABORATION_TENANT_ID=tenant-primary",
        )
    )
    deployment = next(
        d
        for d in _by_kind(docs, "Deployment")
        if d["metadata"]["name"].endswith("-collaboration")
    )
    service = next(
        d
        for d in _by_kind(docs, "Service")
        if d["metadata"]["name"].endswith("-collaboration")
    )

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {
            "name": "http",
            "port": 1234,
            "targetPort": "http",
            "protocol": "TCP",
        }
    ]

    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == (
        "ghcr.io/inqtrix/inqtrix-collaboration:0.2.0"
    )
    assert "volumes" not in pod
    assert "volumeMounts" not in container
    assert "envFrom" not in container
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True

    env = {item["name"]: item for item in container["env"]}
    assert env["INQTRIX_API_INTERNAL_URL"]["value"] == (
        "http://rel-inqtrix-api:5100"
    )
    assert env["INQTRIX_COLLABORATION_SECRET"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "collaboration-secret",
        "key": "INQTRIX_COLLABORATION_SECRET",
    }
    assert env["INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS"]["value"] == "7"
    assert env["INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS"]["value"] == (
        "9"
    )
    assert env["INQTRIX_COLLABORATION_MAX_FRAME_BYTES"]["value"] == (
        "3145728"
    )
    assert env["INQTRIX_COLLABORATION_TENANT_ID"]["value"] == "tenant-primary"
    assert env["INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS"][
        "value"
    ] == "17"
    assert env["INQTRIX_COLLABORATION_UPDATE_RATE_COUNT"]["value"] == "77"
    assert env["INQTRIX_COLLABORATION_MAX_QUEUED_BYTES"]["value"] == "524288"
    assert env["INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES"]["value"] == "11"
    assert env["INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS"]["value"] == "300"
    assert env["INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS"]["value"] == "9000"
    assert env["INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES"]["value"] == "262144"
    assert set(env) == _node_collaboration_env_names()
    assert not any("DATABASE" in name for name in env)

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_COLLABORATION_ENABLED"] == "true"
    assert config["INQTRIX_COLLABORATION_HTTP_URL"] == (
        "http://rel-inqtrix-collaboration:1234"
    )
    # The API reads the ConfigMap and returns these limits in every session;
    # Node must enforce the very same values.
    published_limits = {
        "INQTRIX_COLLABORATION_UPDATE_RATE_COUNT": "77",
        "INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS": "9",
        "INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES": "11",
        "INQTRIX_COLLABORATION_MAX_FRAME_BYTES": "3145728",
        "INQTRIX_COLLABORATION_MAX_QUEUED_BYTES": "524288",
    }
    assert {name: config[name] for name in published_limits} == (
        published_limits
    )
    assert {
        name: env[name]["value"] for name in published_limits
    } == published_limits
    assert config["INQTRIX_COLLABORATION_WS_URL"] == (
        "ws://rel-inqtrix-collaboration:1234/collaboration"
    )

    forbidden_kinds = {"HorizontalPodAutoscaler", "PodDisruptionBudget"}
    assert not any(
        d.get("kind") in forbidden_kinds
        and d.get("metadata", {}).get("name", "").endswith("-collaboration")
        for d in docs
    )
    assert not any(
        d.get("kind") == "PersistentVolumeClaim"
        and d.get("metadata", {}).get("name", "").endswith("-collaboration")
        for d in docs
    )

    api = next(
        d
        for d in _by_kind(docs, "Deployment")
        if d["metadata"]["name"].endswith("-api")
    )
    api_env = {
        item["name"]: item
        for item in api["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert api_env["INQTRIX_COLLABORATION_SECRET"]["valueFrom"][
        "secretKeyRef"
    ]["name"] == "collaboration-secret"


@pytest.mark.parametrize(
    "secret, message",
    [
        (None, "requires secret.data.INQTRIX_COLLABORATION_SECRET"),
        ("too-short", "must contain at least 32 characters"),
    ],
)
def test_collaboration_managed_secret_fails_loudly(
    secret: str | None, message: str
) -> None:
    args = ["postgres.enabled=true", "collaboration.enabled=true"]
    if secret is not None:
        args.append(f"secret.data.INQTRIX_COLLABORATION_SECRET={secret}")
    with pytest.raises(RuntimeError, match=message):
        _template(*args)


def test_pods_run_as_a_fixed_non_root_uid():
    rendered = _template(_EXTERNAL)
    docs = _docs(rendered)
    api = next(d for d in _by_kind(docs, "Deployment") if d["metadata"]["name"].endswith("-api"))
    pod_sc = api["spec"]["template"]["spec"]["securityContext"]
    assert pod_sc["runAsUser"] == 1001
    assert pod_sc["fsGroup"] == 1001


def test_tls_ingress_wires_the_public_origin_through_web_gateway() -> None:
    """TLS termination produces one trusted HTTPS origin at web and API."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "ingress.enabled=true",
            "ingress.host=desk.example",
            "ingress.tls.enabled=true",
            "ingress.tls.secretName=desk-tls",
        )
    )
    ingress = _by_kind(docs, "Ingress")[0]
    assert ingress["spec"]["tls"] == [
        {"hosts": ["desk.example"], "secretName": "desk-tls"}
    ]
    ingress_path = ingress["spec"]["rules"][0]["http"]["paths"][0]
    assert ingress_path["path"] == "/"
    assert ingress_path["pathType"] == "Prefix"
    assert ingress_path["backend"]["service"]["name"] == "rel-inqtrix-web"

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_PUBLIC_BASE_URL"] == "https://desk.example"

    web = next(
        document
        for document in _by_kind(docs, "Deployment")
        if document["metadata"]["name"].endswith("-web")
    )
    env = {
        item["name"]: item["value"]
        for item in web["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INQTRIX_EXTERNAL_SCHEME"] == "https"
    assert env["INQTRIX_PUBLIC_BASE_URL"] == "https://desk.example"


def test_explicit_public_origin_overrides_ingress_derivation() -> None:
    """Operators retain the documented explicit public URL precedence."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "ingress.enabled=true",
            "ingress.host=internal.example",
            "ingress.tls.enabled=true",
            "config.INQTRIX_PUBLIC_BASE_URL=https://public.example",
        )
    )
    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_PUBLIC_BASE_URL"] == "https://public.example"


@pytest.mark.parametrize(
    ("origin", "scheme"),
    [
        ("http://desk.example:8080", "http"),
        ("https://desk.example", "https"),
    ],
)
def test_explicit_public_origin_drives_the_same_web_scheme(
    origin: str,
    scheme: str,
) -> None:
    """A non-chart TLS boundary cannot create a gateway startup conflict."""
    docs = _docs(
        _template(
            _EXTERNAL,
            f"config.INQTRIX_PUBLIC_BASE_URL={origin}",
        )
    )
    web = next(
        item
        for item in _by_kind(docs, "Deployment")
        if item["metadata"]["name"].endswith("-web")
    )
    env = {
        item["name"]: item["value"]
        for item in web["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INQTRIX_PUBLIC_BASE_URL"] == origin
    assert env["INQTRIX_EXTERNAL_SCHEME"] == scheme


def test_explicit_public_origin_rejects_scheme_override_conflict() -> None:
    with pytest.raises(RuntimeError, match="must match the scheme"):
        _template(
            _EXTERNAL,
            "config.INQTRIX_PUBLIC_BASE_URL=https://desk.example",
            "web.externalScheme=http",
        )




def test_bundled_services_autowire_connections():
    # s3.enabled is part of the fixture: a worker pod shares the object
    # store with the API, and the chart refuses per-pod-disk "local"
    # whenever two pods would share it (see the local-store test below).
    rendered = _template(
        "postgres.enabled=true",
        "qdrant.enabled=true",
        "valkey.enabled=true",
        "worker.enabled=true",
        "s3.enabled=true",
    )
    docs = _docs(rendered)
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in docs}
    assert "StatefulSet/rel-inqtrix-postgres" in names
    assert "StatefulSet/rel-inqtrix-qdrant" in names
    assert "StatefulSet/rel-inqtrix-valkey" in names

    secret = _by_kind(docs, "Secret")[0]["stringData"]
    assert "rel-inqtrix-postgres:5432" in secret["INQTRIX_DATABASE_URL"]
    assert "rel-inqtrix-valkey:6379" in secret["INQTRIX_VALKEY_URL"]

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_QDRANT_URL"] == "http://rel-inqtrix-qdrant:6333"
    assert config["INQTRIX_VECTOR_BACKEND"] == "qdrant"
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"


def test_qdrant_mutable_paths_are_on_storage_volume():
    """Bundled Qdrant runs as non-root, so mutable paths must not target the
    image-owned /qdrant directory."""
    rendered = _template("postgres.enabled=true", "qdrant.enabled=true")
    qdrant = next(
        d
        for d in _by_kind(_docs(rendered), "StatefulSet")
        if d["metadata"]["name"].endswith("-qdrant")
    )
    container = qdrant["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}

    assert mounts["storage"] == "/qdrant/storage"
    assert env["QDRANT_INIT_FILE_PATH"] == "/qdrant/storage/.qdrant-initialized"
    assert env["QDRANT__STORAGE__STORAGE_PATH"] == "/qdrant/storage"
    assert env["QDRANT__STORAGE__SNAPSHOTS_PATH"] == "/qdrant/storage/snapshots"
    assert env["QDRANT__STORAGE__TEMP_PATH"] == "/qdrant/storage/tmp"


def test_azure_s3_stack_renders_freeform_env_values():
    rendered = _template(
        "postgres.enabled=true",
        "qdrant.enabled=true",
        "valkey.enabled=true",
        "worker.enabled=true",
        "config.INQTRIX_AUTH_MODE=local",
        "config.INQTRIX_LOCAL_REGISTRATION=closed",
        "config.INQTRIX_LLM_PROVIDER=azure",
        "config.AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com/",
        "config.INQTRIX_SEARCH_PROVIDER=azure_foundry",
        "config.AZURE_AI_PROJECT_ENDPOINT=https://example.services.ai.azure.com/api/projects/proj-default",
        "config.WEB_SEARCH_AGENT_NAME=web-search-agent",
        "config.WEB_SEARCH_AGENT_VERSION=2",
        "config.INQTRIX_EMBEDDING_PROVIDER=azure",
        "config.INQTRIX_EMBEDDING_AZURE_ENDPOINT=https://example.openai.azure.com/",
        "config.INQTRIX_EMBEDDING_MODEL=text-embedding-3-large",
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_ENDPOINT_URL=https://s3.example.test",
        "config.INQTRIX_S3_BUCKET=inqtrix-files",
        "config.INQTRIX_QUOTA_ENABLED=true",
        "secret.data.AZURE_OPENAI_API_KEY=dummy-openai-key",
        "secret.data.AZURE_AI_PROJECT_API_KEY=dummy-foundry-key",
        "secret.data.INQTRIX_EMBEDDING_AZURE_API_KEY=dummy-embedding-key",
        "secret.data.INQTRIX_S3_ACCESS_KEY=dummy-s3-access",
        "secret.data.INQTRIX_S3_SECRET_KEY=dummy-s3-secret",
        extra=[
            "--set-string",
            "config.REASONING_MODEL=gpt-5.4",
            "--set-string",
            "config.TIER_HIGH_MODEL=gpt-5.4",
            "--set-string",
            "config.TIER_MID_MODEL=gpt-5.4",
            "--set-string",
            "config.TIER_FAST_MODEL=gpt-5.4-mini",
            "--set-string",
            r"config.INQTRIX_SELECTABLE_CHAT_MODELS=gpt-5.4\,gpt-5.4-mini\,gpt-5.4-nano",
        ],
    )
    docs = _docs(rendered)

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_AUTH_MODE"] == "local"
    assert config["INQTRIX_LLM_PROVIDER"] == "azure"
    assert config["AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com/"
    assert config["REASONING_MODEL"] == "gpt-5.4"
    assert config["TIER_FAST_MODEL"] == "gpt-5.4-mini"
    assert config["INQTRIX_SELECTABLE_CHAT_MODELS"] == "gpt-5.4,gpt-5.4-mini,gpt-5.4-nano"
    assert config["INQTRIX_SEARCH_PROVIDER"] == "azure_foundry"
    assert config["AZURE_AI_PROJECT_ENDPOINT"] == "https://example.services.ai.azure.com/api/projects/proj-default"
    assert config["WEB_SEARCH_AGENT_NAME"] == "web-search-agent"
    assert config["WEB_SEARCH_AGENT_VERSION"] == "2"
    assert config["INQTRIX_EMBEDDING_PROVIDER"] == "azure"
    assert config["INQTRIX_EMBEDDING_AZURE_ENDPOINT"] == "https://example.openai.azure.com/"
    assert config["INQTRIX_EMBEDDING_MODEL"] == "text-embedding-3-large"
    assert config["INQTRIX_KNOWLEDGE_ENABLED"] == "true"
    assert config["INQTRIX_VECTOR_BACKEND"] == "qdrant"
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "s3"
    assert config["INQTRIX_S3_ENDPOINT_URL"] == "https://s3.example.test"
    assert config["INQTRIX_S3_BUCKET"] == "inqtrix-files"
    assert config["INQTRIX_QUOTA_ENABLED"] == "true"

    secret = _by_kind(docs, "Secret")[0]["stringData"]
    assert secret["AZURE_OPENAI_API_KEY"] == "dummy-openai-key"
    assert secret["AZURE_AI_PROJECT_API_KEY"] == "dummy-foundry-key"
    assert secret["INQTRIX_EMBEDDING_AZURE_API_KEY"] == "dummy-embedding-key"
    assert secret["INQTRIX_S3_ACCESS_KEY"] == "dummy-s3-access"
    assert secret["INQTRIX_S3_SECRET_KEY"] == "dummy-s3-secret"

    rendered_names = {item.get("metadata", {}).get("name", "").lower() for item in docs}
    assert not any("seaweed" in name for name in rendered_names)


def test_valkey_without_worker_does_not_switch_queue_backend():
    """Bundling Valkey without a worker must keep the in-process queue, not strand
    runs in a queue with no consumer."""
    rendered = _template("postgres.enabled=true", "valkey.enabled=true")
    config = _by_kind(_docs(rendered), "ConfigMap")[0]["data"]
    assert config.get("INQTRIX_QUEUE_BACKEND") != "valkey"


def test_bundled_password_with_url_reserved_chars_is_rejected():
    with pytest.raises(RuntimeError, match="URL-unreserved characters"):
        _template("postgres.enabled=true", "postgres.auth.password=p@ss:w/rd")


def test_migrate_hook_phase_tracks_database_origin():
    external = _template(_EXTERNAL)
    migrate_ext = next(
        d for d in _by_kind(_docs(external), "Job") if d["metadata"]["name"].endswith("-migrate")
    )
    assert migrate_ext["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"

    bundled = _template("postgres.enabled=true")
    migrate_bun = next(
        d for d in _by_kind(_docs(bundled), "Job") if d["metadata"]["name"].endswith("-migrate")
    )
    assert migrate_bun["metadata"]["annotations"]["helm.sh/hook"] == "post-install,pre-upgrade"


@pytest.mark.parametrize("rls_mode", ["owner", "bypass"])
def test_privileged_external_migrations_require_a_separate_secret(
    rls_mode: str,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="privileged migration credentials must remain outside",
    ):
        _template(_EXTERNAL, f"migrations.rlsMode={rls_mode}")


def test_migration_secret_is_scoped_to_job_and_owner_upgrade_is_confirmed():
    rendered = _template(
        _EXTERNAL,
        "migrations.databaseSecret.name=migration-database",
        "migrations.databaseSecret.key=direct-url",
        "migrations.rlsMode=bypass",
    )
    docs = _docs(rendered)
    migrate = next(
        document
        for document in _by_kind(docs, "Job")
        if document["metadata"]["name"].endswith("-migrate")
    )
    pod_spec = migrate["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "rel-inqtrix-migrate"
    assert pod_spec["automountServiceAccountToken"] is False
    env = {
        item["name"]: item
        for item in pod_spec["containers"][0]["env"]
    }
    assert env["INQTRIX_MIGRATION_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "migration-database",
        "key": "direct-url",
    }
    assert env["INQTRIX_MIGRATION_RLS_MODE"]["value"] == "bypass"
    assert "INQTRIX_DATABASE_URL" not in env

    for deployment in _by_kind(docs, "Deployment"):
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "migration-database" not in str(container.get("envFrom", []))
        component = deployment["metadata"]["name"].removeprefix("rel-inqtrix-")
        env = {item["name"]: item for item in container.get("env", [])}
        if component in {"api", "worker"}:
            assert env["INQTRIX_MIGRATION_DATABASE_URL"] == {
                "name": "INQTRIX_MIGRATION_DATABASE_URL",
                "value": "",
            }
        else:
            assert "INQTRIX_MIGRATION_DATABASE_URL" not in env

    with pytest.raises(RuntimeError, match="must differ from the runtime"):
        _template(
            _EXTERNAL,
            "migrations.databaseSecret.name=app-secret",
            "migrations.rlsMode=bypass",
        )
    with pytest.raises(RuntimeError, match="forbidden in the runtime"):
        _template(
            _EXTERNAL,
            "secret.data.INQTRIX_MIGRATION_DATABASE_URL=privileged",
        )

    with pytest.raises(RuntimeError, match="maintenanceConfirmed"):
        _template(
            _EXTERNAL,
            "migrations.databaseSecret.name=migration-database",
            "migrations.rlsMode=owner",
            extra=["--is-upgrade"],
        )
    with pytest.raises(RuntimeError, match="maintenanceConfirmed"):
        _template(
            _EXTERNAL,
            "migrations.databaseSecret.name=migration-database",
            "migrations.rlsMode=bypass",
            extra=["--is-upgrade"],
        )
    confirmed = _template(
        _EXTERNAL,
        "migrations.databaseSecret.name=migration-database",
        "migrations.rlsMode=owner",
        "migrations.maintenanceConfirmed=true",
        extra=["--is-upgrade"],
    )
    confirmed_docs = _docs(confirmed)
    assert "INQTRIX_MIGRATION_SERVICES_QUIESCED" in confirmed
    maintenance = next(
        document
        for document in _by_kind(confirmed_docs, "Job")
        if document["metadata"]["name"].endswith("-schema-maintenance")
    )
    assert maintenance["metadata"]["annotations"]["helm.sh/hook-weight"] == "-20"
    assert maintenance["spec"]["template"]["spec"]["serviceAccountName"] == (
        "rel-inqtrix-schema-maintenance"
    )
    assert maintenance["spec"]["template"]["spec"][
        "automountServiceAccountToken"
    ] is True
    command = maintenance["spec"]["template"]["spec"]["containers"][0][
        "command"
    ]
    assert command == [
        "python",
        "-m",
        "inqtrix.deployment.kubernetes_maintenance",
    ]
    role = next(
        document
        for document in _by_kind(confirmed_docs, "Role")
        if document["metadata"]["name"].endswith("-schema-maintenance")
    )
    assert {rule["resources"][0] for rule in role["rules"]} == {
        "deployments/scale",
        "horizontalpodautoscalers",
        "pods",
        "rolebindings",
    }
    scale_rule = next(
        rule
        for rule in role["rules"]
        if rule["resources"] == ["deployments/scale"]
    )
    assert "rel-inqtrix-pgbouncer" in scale_rule["resourceNames"]
    self_revoke = next(
        rule for rule in role["rules"] if rule["resources"] == ["rolebindings"]
    )
    assert self_revoke["verbs"] == ["delete"]
    maintenance_env = {
        item["name"]: item["value"]
        for item in maintenance["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert maintenance_env["INQTRIX_K8S_MAINTENANCE_ROLE_BINDING"] == (
        "rel-inqtrix-schema-maintenance"
    )


def test_schema_upgrade_reactivates_api_hpa_only_after_migration() -> None:
    # More than one API replica needs the worker, whose external Valkey URL
    # lives in the operator's Secret and is confirmed by the queue backend.
    owner = _docs(_template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "api.autoscaling.enabled=true",
        "api.autoscaling.minReplicas=2",
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_AUTH_MODE=default",
        "config.INQTRIX_S3_BUCKET=managed-bucket",
        "migrations.databaseSecret.name=migration-database",
        "migrations.rlsMode=owner",
        "migrations.maintenanceConfirmed=true",
        extra=["--is-upgrade"],
    ))
    owner_api = next(
        document for document in _by_kind(owner, "Deployment")
        if document["metadata"]["name"].endswith("-api")
    )
    assert owner_api["spec"]["replicas"] == 2

    bypass = _docs(_template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "api.autoscaling.enabled=true",
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_AUTH_MODE=default",
        "config.INQTRIX_S3_BUCKET=managed-bucket",
        "migrations.databaseSecret.name=migration-database",
        "migrations.rlsMode=bypass",
        "migrations.maintenanceConfirmed=true",
        extra=["--is-upgrade"],
    ))
    bypass_api = next(
        document for document in _by_kind(bypass, "Deployment")
        if document["metadata"]["name"].endswith("-api")
    )
    assert bypass_api["spec"]["replicas"] == 1


def test_byo_service_account_creates_none_and_requires_explicit_name() -> None:
    with pytest.raises(RuntimeError, match="requires an explicit"):
        _template(_EXTERNAL, "serviceAccount.create=false")

    rendered = _template(
        _EXTERNAL,
        "serviceAccount.create=false",
        "serviceAccount.name=inqtrix-runtime",
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "collaboration.enabled=true",
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_AUTH_MODE=default",
        "config.INQTRIX_S3_BUCKET=managed-bucket",
    )
    docs = _docs(rendered)
    assert _by_kind(docs, "ServiceAccount") == []
    for document in docs:
        if document.get("kind") not in {"Deployment", "Job", "Pod"}:
            continue
        pod_spec = (
            document["spec"]["template"]["spec"]
            if document["kind"] in {"Deployment", "Job"}
            else document["spec"]
        )
        assert pod_spec["serviceAccountName"] == "inqtrix-runtime"

    with pytest.raises(RuntimeError, match="annotations requires"):
        _template(
            _EXTERNAL,
            extra=[
                "--set-string",
                r"serviceAccount.api.annotations.eks\.amazonaws\.com/role-arn=ignored",
            ],
        )


def test_chart_version_tracks_chart_contract_changes() -> None:
    """Pinned so a version bump is a deliberate, reviewed act.

    Note what this does NOT do: it fires when the version changes without
    this pin being updated, not when the chart changes without the version
    being bumped. Detecting the latter is what
    ``test_chart_contract_digest_catches_an_unacknowledged_chart_change``
    below is for.
    """
    chart = yaml.safe_load((_CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart["version"] == "0.3.0"


def test_observability_tracing_env_renders_for_api_and_worker() -> None:
    rendered = _template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "s3.enabled=true",
        "observability.tracing.enabled=true",
        "observability.tracing.otlpEndpoint=http://langfuse-web.langfuse.svc:3000/api/public/otel",
        "observability.tracing.headersSecret.name=langfuse-otlp",
        "observability.tracing.uiUrl=https://langfuse.example",
        "observability.tracing.retentionDays=14",
    )
    # Both deployments share the helper: the api consumes the UI URL,
    # the worker the retention — extra vars are inert per process.
    assert rendered.count('value: "otlp"') >= 2
    assert rendered.count("INQTRIX_TRACE_UI_URL") == 2
    assert rendered.count("INQTRIX_TRACE_RETENTION_DAYS") == 2
    assert 'value: "14"' in rendered
    assert "langfuse-otlp" in rendered

    disabled = _template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "s3.enabled=true",
    )
    assert "INQTRIX_TRACING" not in disabled
    assert "INQTRIX_TRACE_UI_URL" not in disabled
    assert "INQTRIX_TRACE_RETENTION_DAYS" not in disabled


def test_observability_file_mode_requires_and_wires_shared_spool() -> None:
    # file mode + worker WITHOUT a shared claim: refused at render time
    # (a pod-local spool would hide worker spans from the admin export).
    with pytest.raises(RuntimeError, match="spoolClaim"):
        _template(
            _EXTERNAL,
            "worker.enabled=true",
            "config.INQTRIX_QUEUE_BACKEND=valkey",
            "s3.enabled=true",
            "observability.tracing.enabled=true",
            "observability.tracing.mode=file",
        )

    rendered = _template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "s3.enabled=true",
        "observability.tracing.enabled=true",
        "observability.tracing.mode=file",
        "observability.tracing.spoolClaim=trace-spool-rwx",
    )
    # Both deployments mount the claim and pin the spool dir onto it.
    assert rendered.count("claimName: trace-spool-rwx") == 2
    assert rendered.count("mountPath: /var/lib/inqtrix/traces") == 2
    assert rendered.count("INQTRIX_TRACE_SPOOL_DIR") == 2

    # API-only file mode needs no shared claim — render must succeed.
    api_only = _template(
        _EXTERNAL,
        "observability.tracing.enabled=true",
        "observability.tracing.mode=file",
    )
    assert "claimName" not in api_only or "trace-spool" not in api_only


def test_workload_identity_and_ca_are_scoped_to_api_and_worker():
    rendered = _template(
        _EXTERNAL,
        "worker.enabled=true",
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_AUTH_MODE=default",
        "config.INQTRIX_S3_BUCKET=managed-bucket",
        "serviceAccount.api.create=true",
        "serviceAccount.api.automountServiceAccountToken=true",
        "serviceAccount.worker.create=true",
        "serviceAccount.worker.automountServiceAccountToken=true",
        "s3.caBundle.existingConfigMap=managed-s3-ca",
        extra=[
            "--set-string",
            r"serviceAccount.api.annotations.eks\.amazonaws\.com/role-arn=api-role",
            "--set-string",
            r"serviceAccount.worker.annotations.eks\.amazonaws\.com/role-arn=worker-role",
        ],
    )
    docs = _docs(rendered)
    accounts = {
        account["metadata"]["name"]: account
        for account in _by_kind(docs, "ServiceAccount")
    }
    assert accounts["rel-inqtrix-api"]["metadata"]["annotations"] == {
        "eks.amazonaws.com/role-arn": "api-role"
    }
    assert accounts["rel-inqtrix-worker"]["metadata"]["annotations"] == {
        "eks.amazonaws.com/role-arn": "worker-role"
    }

    deployments = {
        deployment["metadata"]["name"].removeprefix("rel-inqtrix-"): deployment
        for deployment in _by_kind(docs, "Deployment")
    }
    for component in ("api", "worker"):
        pod_spec = deployments[component]["spec"]["template"]["spec"]
        assert pod_spec["serviceAccountName"] == f"rel-inqtrix-{component}"
        assert pod_spec["automountServiceAccountToken"] is True
        mounts = pod_spec["containers"][0]["volumeMounts"]
        assert any(mount["name"] == "object-store-ca" for mount in mounts)
        ca_volume = next(
            volume for volume in pod_spec["volumes"]
            if volume["name"] == "object-store-ca"
        )
        assert ca_volume["configMap"]["name"] == "managed-s3-ca"

    web_spec = deployments["web"]["spec"]["template"]["spec"]
    assert web_spec["serviceAccountName"] == "rel-inqtrix-web"
    assert web_spec["automountServiceAccountToken"] is False
    assert "object-store-ca" not in str(web_spec)
    migrate = next(
        document for document in _by_kind(docs, "Job")
        if document["metadata"]["name"].endswith("-migrate")
    )
    assert "object-store-ca" not in str(migrate)
    assert "rel-inqtrix-api" not in str(migrate["spec"]["template"]["spec"])
    assert migrate["spec"]["template"]["spec"]["serviceAccountName"] == (
        "rel-inqtrix-migrate"
    )

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_S3_CA_BUNDLE"] == "/etc/inqtrix/object-store/ca.crt"


def test_helm_smoke_uses_readiness_and_requires_configured_s3():
    rendered = _template(
        _EXTERNAL,
        "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "config.INQTRIX_S3_AUTH_MODE=default",
        "config.INQTRIX_S3_BUCKET=managed-bucket",
    )
    test_pod = next(
        document
        for document in _by_kind(_docs(rendered), "Pod")
        if document["metadata"]["name"].endswith("-test-health")
    )
    script = test_pod["spec"]["containers"][0]["command"][-1]
    assert "/readyz" in script
    assert 'get("object_store") != "ok"' in script
    assert test_pod["spec"]["automountServiceAccountToken"] is False
    assert test_pod["spec"]["serviceAccountName"] == "rel-inqtrix-test"


def test_database_runtime_login_policy_tracks_database_origin() -> None:
    external = _by_kind(_docs(_template(_EXTERNAL)), "ConfigMap")[0]["data"]
    bundled = _by_kind(
        _docs(_template("postgres.enabled=true")), "ConfigMap"
    )[0]["data"]

    assert external["INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY"] == "restricted"
    assert bundled["INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY"] == "bundled_legacy"

    explicit = _by_kind(
        _docs(
            _template(
                _EXTERNAL,
                "config.INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=bundled_legacy",
            )
        ),
        "ConfigMap",
    )[0]["data"]
    assert explicit["INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY"] == "bundled_legacy"


def test_secret_change_triggers_pod_rollout():
    """The api/worker pod template must carry a secret checksum so a changed
    secret value rolls the pods (envFrom alone would keep stale env)."""

    def api_checksum(*extra: str) -> str:
        docs = _docs(_template("postgres.enabled=true", "qdrant.enabled=true", *extra))
        api = next(d for d in _by_kind(docs, "Deployment") if d["metadata"]["name"].endswith("-api"))
        return api["spec"]["template"]["metadata"]["annotations"]["checksum/secret"]

    assert api_checksum() != api_checksum("secret.data.LITELLM_API_KEY=rotated-key")


def test_bundled_minio_renders_and_autowires_s3():
    """s3.enabled bundles a MinIO StatefulSet+Service and auto-wires the object-store
    backend/endpoint/bucket (ConfigMap) plus the access/secret keys (Secret)."""
    rendered = _template("postgres.enabled=true", "s3.enabled=true")
    docs = _docs(rendered)
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in docs}
    assert "StatefulSet/rel-inqtrix-minio" in names
    assert "Service/rel-inqtrix-minio" in names

    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "s3"
    assert config["INQTRIX_S3_ENDPOINT_URL"] == "http://rel-inqtrix-minio:9000"
    assert config["INQTRIX_S3_BUCKET"] == "inqtrix-files"
    assert config["INQTRIX_S3_AUTH_MODE"] == "static"
    assert config["INQTRIX_S3_ADDRESSING_STYLE"] == "path"
    assert config["INQTRIX_S3_BUCKET_PROVISIONING"] == "create_if_missing"

    secret = _by_kind(docs, "Secret")[0]["stringData"]
    assert secret["INQTRIX_S3_ACCESS_KEY"] == "inqtrix"
    assert secret["INQTRIX_S3_SECRET_KEY"] == "SyntheticMinio2026"


def test_existing_secret_supplies_bundled_service_credentials() -> None:
    """Backing-service pods read credentials from one selected Secret."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "qdrant.enabled=true",
            "valkey.enabled=true",
            "s3.enabled=true",
        )
    )
    statefulsets = {
        document["metadata"]["name"]: document
        for document in _by_kind(docs, "StatefulSet")
    }

    def environment(service: str) -> dict[str, dict]:
        statefulset = next(
            value
            for name, value in statefulsets.items()
            if name.endswith(f"-{service}")
        )
        return {
            item["name"]: item
            for item in statefulset["spec"]["template"]["spec"][
                "containers"
            ][0]["env"]
        }

    qdrant = environment("qdrant")["QDRANT__SERVICE__API_KEY"]
    assert qdrant["valueFrom"]["secretKeyRef"] == {
        "name": "app-secret",
        "key": "INQTRIX_QDRANT_API_KEY",
    }
    valkey = environment("valkey")["VALKEY_PASSWORD"]
    assert valkey["valueFrom"]["secretKeyRef"] == {
        "name": "app-secret",
        "key": "INQTRIX_BUNDLED_VALKEY_PASSWORD",
    }
    minio = environment("minio")
    assert minio["MINIO_ROOT_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "app-secret",
        "key": "INQTRIX_S3_ACCESS_KEY",
    }
    assert minio["MINIO_ROOT_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "app-secret",
        "key": "INQTRIX_S3_SECRET_KEY",
    }


def test_valkey_probes_do_not_put_password_in_process_arguments() -> None:
    docs = _docs(
        _template(
            _MANAGED_EXTERNAL,
            "valkey.enabled=true",
        )
    )
    statefulset = next(
        document
        for document in _by_kind(docs, "StatefulSet")
        if document["metadata"]["name"].endswith("-valkey")
    )
    container = statefulset["spec"]["template"]["spec"]["containers"][0]

    for probe_name in ("readinessProbe", "livenessProbe"):
        command = " ".join(container[probe_name]["exec"]["command"])
        assert "REDISCLI_AUTH=" in command
        assert "valkey-cli ping" in command
        assert "valkey-cli -a" not in command


@pytest.mark.parametrize("engine", ("valkey", "redis"))
def test_bundled_broker_binaries_follow_the_selected_engine(
    engine: str,
) -> None:
    """Command and both probes name the binary family of the chosen engine.

    A mismatch would render a container whose command does not exist in its
    own image, which only surfaces as a crash loop in the cluster.
    """
    docs = _docs(
        _template(
            _MANAGED_EXTERNAL,
            "valkey.enabled=true",
            f"valkey.engine={engine}",
        )
    )
    container = next(
        document
        for document in _by_kind(docs, "StatefulSet")
        if document["metadata"]["name"].endswith("-valkey")
    )["spec"]["template"]["spec"]["containers"][0]

    assert container["command"][0] == f"{engine}-server"
    for probe_name in ("readinessProbe", "livenessProbe"):
        command = " ".join(container[probe_name]["exec"]["command"])
        assert f"{engine}-cli ping" in command
        assert "REDISCLI_AUTH=" in command


def test_unknown_broker_engine_fails_the_render() -> None:
    with pytest.raises(RuntimeError, match="valkey.engine"):
        _template(
            _MANAGED_EXTERNAL,
            "valkey.enabled=true",
            "valkey.engine=keydb",
        )




def test_bundled_services_pin_a_non_root_uid():
    """The bundled services keep a fixed non-root UID so the data PVC is writable."""
    rendered = _template(
        _EXTERNAL,
        "qdrant.enabled=true",
        "valkey.enabled=true",
        "s3.enabled=true",
    )
    docs = _docs(rendered)
    for name, uid in {"qdrant": 1000, "valkey": 999, "minio": 1000}.items():
        sts = next(
            d for d in _by_kind(docs, "StatefulSet")
            if d["metadata"]["name"].endswith(f"-{name}")
        )
        sc = sts["spec"]["template"]["spec"]["securityContext"]
        assert sc["runAsUser"] == uid
        assert sc["fsGroup"] == uid


def test_bundled_minio_rejects_parallel_external_s3_config() -> None:
    """One enabled bundled service has one topology and credential source."""
    with pytest.raises(RuntimeError, match="bundled MinIO topology"):
        _template(
            "postgres.enabled=true",
            "s3.enabled=true",
            "config.INQTRIX_S3_ENDPOINT_URL=https://external.example.test",
        )
    # The conflict is named before the worker's topology check, which would
    # otherwise ask for the s3.enabled=true that is already set.
    with pytest.raises(RuntimeError, match="bundled MinIO topology"):
        _template(
            "postgres.enabled=true",
            "valkey.enabled=true",
            "s3.enabled=true",
            "config.INQTRIX_OBJECT_STORE_BACKEND=local",
            single_pod=False,
        )


def test_local_object_store_never_serves_more_than_one_pod():
    """Per-pod-disk blobs must not be shared: a second pod would 404 them.

    Without the worker, the one-replica limit of the single-pod variant
    refuses a second API pod, static or autoscaled; with the worker, the
    topology check refuses the local backend. Each case is matched on the
    prose its own check emits, so a reword must be caught here.
    """
    with pytest.raises(RuntimeError, match="supports exactly one API replica"):
        _template(_EXTERNAL, "api.replicaCount=2")
    # The autoscaling ceiling counts even with replicaCount=1.
    with pytest.raises(RuntimeError, match=r"exactly one API replica \(3 requested"):
        _template(
            _EXTERNAL,
            "api.autoscaling.enabled=true",
            "api.autoscaling.maxReplicas=3",
        )
    # A worker pod shares the store too.
    with pytest.raises(
        RuntimeError, match=r"needs: a shared object store \(s3\.enabled=true"
    ):
        _template(
            _MANAGED_EXTERNAL,
            "worker.enabled=true",
            "valkey.enabled=true",
        )
    # A shared store makes several pods renderable, and the replica hint
    # (2 API replicas + 1 worker) reaches the app config for its own
    # startup guard.
    rendered = _template(
        _MANAGED_EXTERNAL,
        "worker.enabled=true",
        "valkey.enabled=true",
        "s3.enabled=true",
        "api.replicaCount=2",
    )
    config = _by_kind(_docs(rendered), "ConfigMap")[0]["data"]
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "s3"
    assert config["INQTRIX_REPLICA_COUNT"] == "3"
    # One API pod on local storage keeps working (the single-pod variant).
    single = _template(_EXTERNAL)
    config = _by_kind(_docs(single), "ConfigMap")[0]["data"]
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "local"
    assert config["INQTRIX_REPLICA_COUNT"] == "1"


def test_bundled_pgbouncer_pools_app_url_but_not_migrate():
    """The app routes through the pooler; Alembic stays direct."""
    # Requires the bundled Postgres.
    with pytest.raises(RuntimeError, match="requires postgres.enabled"):
        _template(_EXTERNAL, "pgbouncer.enabled=true")
    rendered = _template(
        "postgres.enabled=true",
        "pgbouncer.enabled=true",
    )
    docs = _docs(rendered)
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in docs}
    assert "Deployment/rel-inqtrix-pgbouncer" in names
    assert "Service/rel-inqtrix-pgbouncer" in names

    secret = _by_kind(docs, "Secret")[0]["stringData"]
    app_url = secret["INQTRIX_DATABASE_URL"]
    assert "rel-inqtrix-pgbouncer:6432" in app_url
    assert "prepared_statement_cache_size=0" in app_url

    migrate = [
        d for d in _by_kind(docs, "Job")
        if "migrate" in d["metadata"]["name"]
    ][0]
    env = migrate["spec"]["template"]["spec"]["containers"][0]["env"]
    direct = [e for e in env if e["name"] == "INQTRIX_DATABASE_URL"][0]
    assert "rel-inqtrix-postgres:5432" in direct["value"]
    assert "pgbouncer" not in direct["value"]

    pooler = [
        d for d in _by_kind(docs, "Deployment")
        if "pgbouncer" in d["metadata"]["name"]
    ][0]
    container = pooler["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == (
        "ghcr.io/cloudnative-pg/pgbouncer:1.25.1-trixie@"
        "sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8"
    )
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["command"] == ["/bin/sh", "-ec"]
    command = container["args"][0]
    assert "umask 077" in command
    assert "unset DB_PASSWORD" in command
    assert "exec /usr/sbin/pgbouncer" in command
    env = {e["name"]: e for e in container["env"]}
    assert env["DB_USER"]["value"] == "inqtrix"
    assert "value" not in env["DB_PASSWORD"]
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "rel-inqtrix",
        "key": "INQTRIX_BUNDLED_POSTGRES_PASSWORD",
    }
    config = next(
        d
        for d in _by_kind(docs, "ConfigMap")
        if d["metadata"]["name"].endswith("-pgbouncer")
    )["data"]["pgbouncer.ini"]
    assert "pool_mode = transaction" in config
    assert "max_prepared_statements = 200" in config
    assert "* = host=rel-inqtrix-postgres port=5432" in config

    postgres = next(
        d
        for d in _by_kind(docs, "StatefulSet")
        if d["metadata"]["name"].endswith("-postgres")
    )
    postgres_env = {
        item["name"]: item
        for item in postgres["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "value" not in postgres_env["POSTGRES_PASSWORD"]
    assert postgres_env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "rel-inqtrix",
        "key": "INQTRIX_BUNDLED_POSTGRES_PASSWORD",
    }


def test_pgbouncer_disabled_keeps_direct_app_url():
    rendered = _template("postgres.enabled=true")
    secret = _by_kind(_docs(rendered), "Secret")[0]["stringData"]
    assert "rel-inqtrix-postgres:5432" in secret["INQTRIX_DATABASE_URL"]


@pytest.mark.parametrize(
    ("enabled", "credential", "suffix"),
    [
        ("qdrant.enabled=true", "qdrant.apiKey", "qdrant"),
        ("valkey.enabled=true", "valkey.password", "valkey"),
        ("s3.enabled=true", "s3.secretKey", "minio"),
    ],
)
def test_bundled_credential_change_rolls_its_backing_service(
    enabled: str,
    credential: str,
    suffix: str,
) -> None:
    """Chart-managed Secret changes alter the backing Pod template checksum."""

    def checksum(value: str) -> str:
        docs = _docs(
            _template(
                _MANAGED_EXTERNAL,
                enabled,
                f"{credential}={value}",
            )
        )
        workload = next(
            item
            for item in _by_kind(docs, "StatefulSet")
            if item["metadata"]["name"].endswith(f"-{suffix}")
        )
        return workload["spec"]["template"]["metadata"]["annotations"][
            "checksum/secret"
        ]

    assert checksum("SyntheticCredentialAlpha2026") != checksum(
        "SyntheticCredentialBeta2026"
    )


def _api_deployment(docs: list[dict]) -> dict:
    return [
        d for d in _by_kind(docs, "Deployment")
        if d["metadata"]["name"] == "rel-inqtrix-api"
    ][0]


def test_metrics_disabled_by_default():
    """No INQTRIX_METRICS_ENABLED and no scrape annotations by default."""
    docs = _docs(_template(_EXTERNAL))
    config = [
        d for d in _by_kind(docs, "ConfigMap")
        if d["metadata"]["name"] == "rel-inqtrix"
    ][0]["data"]
    assert "INQTRIX_METRICS_ENABLED" not in config
    annotations = (
        _api_deployment(docs)["spec"]["template"]["metadata"].get(
            "annotations", {}
        )
    )
    assert "prometheus.io/scrape" not in annotations


def test_metrics_enabled_sets_flag_and_scrape_annotations():
    """The toggle wires the env flag and the classic scrape annotations."""
    docs = _docs(_template(_EXTERNAL, "metrics.enabled=true"))
    config = [
        d for d in _by_kind(docs, "ConfigMap")
        if d["metadata"]["name"] == "rel-inqtrix"
    ][0]["data"]
    assert config["INQTRIX_METRICS_ENABLED"] == "true"
    annotations = _api_deployment(docs)["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert annotations["prometheus.io/scrape"] == "true"
    assert annotations["prometheus.io/path"] == "/metrics"

    # podAnnotations=false keeps the flag but drops the scrape annotations
    # (for a PodMonitor/ServiceMonitor-based Prometheus instead).
    docs2 = _docs(
        _template(
            _EXTERNAL, "metrics.enabled=true", "metrics.podAnnotations=false"
        )
    )
    annotations2 = (
        _api_deployment(docs2)["spec"]["template"]["metadata"].get(
            "annotations", {}
        )
    )
    assert "prometheus.io/scrape" not in annotations2


def _web_env(docs: list[dict]) -> dict[str, str | None]:
    """Extract the web container environment as a name->value map."""
    web = [
        d
        for d in _by_kind(docs, "Deployment")
        if d["metadata"]["name"].endswith("-web")
    ][0]
    return {
        item["name"]: item.get("value")
        for item in web["spec"]["template"]["spec"]["containers"][0]["env"]
    }




def test_chart_edge_sets_two_trusted_proxy_hops() -> None:
    """Ingress plus web gateway put TWO hops into X-Forwarded-For.

    With the app default of 1 the login rate limiter would key every client
    on the edge pod address, collapsing all users into a single lockout
    bucket (a third party spraying failed logins at a victim's identifier
    would lock the victim out).
    """
    for edge_args in (("ingress.enabled=true",),):
        docs = _docs(_template(_EXTERNAL, *edge_args))
        config = [
            d
            for d in _by_kind(docs, "ConfigMap")
            if d["metadata"]["name"] == "rel-inqtrix"
        ][0]["data"]
        assert config["INQTRIX_TRUSTED_PROXY_HOPS"] == "2"


def test_no_edge_leaves_trusted_proxy_hops_unset() -> None:
    """Port-forward topology has one proxy hop; the app default fits."""
    docs = _docs(_template(_EXTERNAL))
    config = [
        d
        for d in _by_kind(docs, "ConfigMap")
        if d["metadata"]["name"] == "rel-inqtrix"
    ][0]["data"]
    assert "INQTRIX_TRUSTED_PROXY_HOPS" not in config


def test_explicit_trusted_proxy_hops_wins_over_derived() -> None:
    """An extra outer load balancer needs a deeper explicit hop count."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "ingress.enabled=true",
            extra=["--set-string", "config.INQTRIX_TRUSTED_PROXY_HOPS=3"],
        )
    )
    config = [
        d
        for d in _by_kind(docs, "ConfigMap")
        if d["metadata"]["name"] == "rel-inqtrix"
    ][0]["data"]
    assert config["INQTRIX_TRUSTED_PROXY_HOPS"] == "3"


def test_web_body_size_tracks_max_file_bytes() -> None:
    """The gateway receives the API limit and derives its guarded headroom."""
    docs = _docs(
        _template(
            _EXTERNAL,
            extra=["--set-string", "config.INQTRIX_MAX_FILE_BYTES=524288000"],
        )
    )
    env = _web_env(docs)
    assert env["INQTRIX_MAX_FILE_BYTES"] == "524288000"
    assert "INQTRIX_PROXY_MAX_BODY_BYTES" not in env


def test_web_body_size_defaults_to_image_value() -> None:
    """Without the config key the gateway's 100 MiB + headroom defaults apply."""
    docs = _docs(_template(_EXTERNAL))
    env = _web_env(docs)
    assert "INQTRIX_MAX_FILE_BYTES" not in env
    assert "INQTRIX_PROXY_MAX_BODY_BYTES" not in env


def test_web_external_scheme_override_pins_https() -> None:
    """web.externalScheme covers TLS terminators the chart does not own.

    Without it the web gateway forwards X-Forwarded-Proto: http behind an
    external load balancer, and the collaboration WebSocket origin check
    rejects every session even with a correct INQTRIX_PUBLIC_BASE_URL.
    """
    docs = _docs(_template(_EXTERNAL, "web.externalScheme=https"))
    assert _web_env(docs)["INQTRIX_EXTERNAL_SCHEME"] == "https"


def test_web_body_size_rejects_non_byte_values() -> None:
    """Legacy size suffixes cannot silently produce a divergent proxy cap."""
    with pytest.raises(RuntimeError, match="plain bytes"):
        _template(
            _EXTERNAL,
            extra=["--set-string", "config.INQTRIX_MAX_FILE_BYTES=100m"],
        )


def test_web_extra_env_accepts_explicit_proxy_byte_cap() -> None:
    """Operators may deliberately override the gateway's derived headroom."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "web.extraEnv[0].name=INQTRIX_PROXY_MAX_BODY_BYTES",
            extra=[
                "--set-string",
                "web.extraEnv[0].value=209715200",
                "--set-string",
                "config.INQTRIX_MAX_FILE_BYTES=524288000",
            ],
        )
    )
    env = _web_env(docs)
    assert env["INQTRIX_MAX_FILE_BYTES"] == "524288000"
    assert env["INQTRIX_PROXY_MAX_BODY_BYTES"] == "209715200"


def test_bundled_postgres_carries_the_configured_connection_ceiling():
    """The bundled server must be sizeable, not stuck on the image default.

    An api and a worker together ask for more connections than the image
    default allows, and without a lever the only remedy is to abandon the
    bundled database entirely.
    """
    rendered = _template(
        "postgres.enabled=true",
        "postgres.maxConnections=250",
    )
    stateful = _by_kind(_docs(rendered), "StatefulSet")
    postgres = [
        d for d in stateful if d["metadata"]["name"].endswith("-postgres")
    ]
    assert postgres, "the bundled database should render a StatefulSet"
    container = postgres[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["postgres", "-c", "max_connections=250"]


def test_bundled_postgres_ships_above_the_image_ceiling():
    """The shipped run cap needs more connections than the image allows.

    Run threads open a connection per database operation, and an api plus a
    worker each add their own pooled budget on top.
    """
    rendered = _template("postgres.enabled=true")
    stateful = _by_kind(_docs(rendered), "StatefulSet")
    postgres = [
        d for d in stateful if d["metadata"]["name"].endswith("-postgres")
    ]
    container = postgres[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["postgres", "-c", "max_connections=300"]
    # A ceiling raised without the memory to hold it trades one failure for
    # another, so the two move together or not at all.
    memory = container["resources"]["limits"]["memory"]
    assert memory == "2Gi", (
        "raising max_connections without raising memory buys an OOM instead "
        "of the connections"
    )


@pytest.mark.parametrize("value", ["0", "-5", "abc", "12.5"])
def test_invalid_connection_ceiling_fails_the_render(value):
    """A ceiling below one must fail here, not at container start.

    A server that refuses to start takes the whole release down with a
    message from Postgres rather than from the value that caused it.
    """
    # Anchored on the message the guard itself emits, not on the value being
    # echoed back: every unrelated render failure quotes the --set argument
    # too, so a looser match would go green for the wrong reason.
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template(
            "postgres.enabled=true",
            f"postgres.maxConnections={value}",
        )


# Digest of every file under the chart, updated together with the version
# above. Recompute with:
#   python -c "import hashlib,pathlib;c=pathlib.Path('deploy/helm/inqtrix');\
#h=hashlib.sha256();[(h.update(p.relative_to(c).as_posix().encode()),h.update(b'\0'),\
#h.update(p.read_bytes()),h.update(b'\0')) for p in sorted(x for x in c.rglob('*') \
#if x.is_file())];print(h.hexdigest())"
_CHART_CONTRACT_DIGEST = (
    "49ce17a4ead3a9060fc74d1b2364d9c15ebdf88623f5dc886f3a35c9bc3a8b83"
)


def _chart_files() -> list:
    """Every file under the chart, in a stable order.

    No suffix filter: NOTES.txt is a rendered template and .helmignore
    decides what ships, so a filter that admitted only YAML would leave
    real chart content outside a gate whose whole job is to notice change.
    """
    return sorted(p for p in _CHART.rglob("*") if p.is_file())


def _chart_contract_digest() -> str:
    """Hash the chart's files, path included."""
    digest = hashlib.sha256()
    for path in _chart_files():
        digest.update(path.relative_to(_CHART).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_chart_contract_digest_catches_an_unacknowledged_chart_change() -> None:
    """Catch the direction the version pin above cannot see.

    Precisely what this guarantees, and what it does not: any edit to any
    chart file turns this red, so a chart change cannot reach review
    unnoticed -- which is how a chart edit has already shipped unbumped.
    It cannot force the version to MOVE; someone can update the digest
    alone. What it forces is a deliberate edit here, in a hunk a reviewer
    sees next to the chart change, with the message below telling them the
    version belongs in the same commit.

    Comments count deliberately: in ``values.yaml`` they are the
    operator-facing contract, and a comment that quietly stops matching
    the value it describes is exactly the drift worth failing on.
    """
    assert _chart_contract_digest() == _CHART_CONTRACT_DIGEST, (
        "the chart changed: bump `version` in Chart.yaml, then update "
        "_CHART_CONTRACT_DIGEST and the pin in "
        "test_chart_version_tracks_chart_contract_changes. The recompute "
        "command is in the comment above the constant."
    )


def test_the_digest_covers_every_file_the_chart_ships() -> None:
    """A gate with a blind spot is worse than none: it reads as coverage."""
    hashed = {p.relative_to(_CHART).as_posix() for p in _chart_files()}
    # Walked independently of the helper under test, so a filter creeping
    # back into _chart_files() shows up as a difference rather than being
    # mirrored on both sides.
    on_disk = set()
    for root, _dirs, names in os.walk(_CHART):
        for name in names:
            on_disk.add(
                Path(root).joinpath(name).relative_to(_CHART).as_posix()
            )
    assert hashed == on_disk, f"not hashed: {sorted(on_disk - hashed)}"
    # Named explicitly because these are the files a suffix filter drops.
    assert "templates/NOTES.txt" in hashed
    assert ".helmignore" in hashed


def test_gateway_pool_value_renders_only_when_set():
    """The typed value must not restate the gateway's built-in default.

    config:/extraConfig: cannot reach the web pod -- it has no envFrom --
    so this values key is the only discoverable way to size the gateway
    pool. Unset, nothing renders and the gateway's own default is the
    single source of truth.
    """
    unset = _template("postgres.enabled=true")
    assert "INQTRIX_MAX_UPSTREAM_CONNECTIONS" not in unset

    rendered = _template(
        "postgres.enabled=true", "web.maxUpstreamConnections=768"
    )
    web = [
        d
        for d in _by_kind(_docs(rendered), "Deployment")
        if d["metadata"]["name"].endswith("-web")
    ]
    env = web[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    values = {e["name"]: e.get("value") for e in env}
    assert values["INQTRIX_MAX_UPSTREAM_CONNECTIONS"] == "768"


def test_gateway_pool_zero_fails_the_render_instead_of_meaning_unset():
    """An explicit 0 must fail loudly, never silently read as 'unset'."""
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template("postgres.enabled=true", "web.maxUpstreamConnections=0")


def test_gateway_pool_bool_and_float_fail_instead_of_coercing(tmp_path):
    """Helm's int turns true into 1 and truncates a float64 512.9 to 512.

    The bool sails past a positivity check that runs AFTER the coercion
    (true -> 1). The float trap is values-file-only: --set delivers the
    STRING "512.9", which the old chart already rejected via int -> 0 ->
    positivity check; a real YAML float64 512.9 was silently truncated to
    512. So the float leg must reach the guard as a genuine float64,
    through a values file -- a --set probe pins nothing.
    """
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template("postgres.enabled=true", "web.maxUpstreamConnections=true")
    values = tmp_path / "float.yaml"
    values.write_text("web:\n  maxUpstreamConnections: 512.9\n")
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template("postgres.enabled=true", extra=["-f", str(values)])


def test_postgres_max_connections_bool_fails_instead_of_meaning_one():
    """Same trap next door: true -> int 1 -> Postgres with max_connections=1."""
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template("postgres.enabled=true", "postgres.maxConnections=true")


def test_leading_zero_strings_fail_instead_of_meaning_octal():
    """sprig's int parses base 0: the STRING "0512" would mean octal 330.

    Only the string channel is guardable: an UNQUOTED values-file 0512 is
    YAML-1.1 octal, so helm's parser hands the chart the integer 330
    before any template code runs -- indistinguishable from writing 330.
    That parser semantic is a documented limit, not a chart fallback.
    """
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template(
            "postgres.enabled=true",
            extra=["--set-string", "web.maxUpstreamConnections=0512"],
        )
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        _template(
            "postgres.enabled=true",
            extra=["--set-string", "postgres.maxConnections=0512"],
        )


def test_replica_count_cannot_be_supplied_by_the_operator():
    """The chart derives it completely; a user value is redundant or wrong.

    Too low is caught by the chart's own guard, but too HIGH passes the
    render and then crash-loops every api pod at startup via the app-side
    local-store guard -- a failure pointing away from its cause.
    """
    with pytest.raises(RuntimeError, match="is derived by the chart"):
        _template(
            "postgres.enabled=true",
            "config.INQTRIX_REPLICA_COUNT=5",
        )


def test_replica_count_counts_actual_worker_replicas():
    """worker.enabled counts the worker replicas, not a flat 1."""
    rendered = _template(
        "postgres.enabled=true",
        "valkey.enabled=true",
        "s3.enabled=true",
        "s3.secretKey=SyntheticMinio2026Key",
        "worker.enabled=true",
        "worker.replicaCount=3",
        "api.replicaCount=2",
    )
    configmaps = _by_kind(_docs(rendered), "ConfigMap")
    data = {}
    for cm in configmaps:
        data.update(cm.get("data") or {})
    assert data["INQTRIX_REPLICA_COUNT"] == "5", (
        "2 api replicas + 3 worker replicas are 5 sharers, not 3"
    )


# -- Default worker topology --------------------------------------------------
#
# The worker is on by default. It needs the Postgres run store, a Valkey queue
# and a shared object store; without it the API runs every job itself and
# supports exactly one replica. The tests below render with single_pod=False
# wherever the default itself is the subject.

_EXTERNAL_S3 = (
    "config.INQTRIX_OBJECT_STORE_BACKEND=s3",
    "config.INQTRIX_S3_AUTH_MODE=default",
    "config.INQTRIX_S3_BUCKET=managed-bucket",
)


def _deployment(docs: list[dict], component: str) -> dict | None:
    return next(
        (
            d
            for d in _by_kind(docs, "Deployment")
            if d["metadata"]["name"] == f"rel-inqtrix-{component}"
        ),
        None,
    )


def _object_store_volume(deployment: dict) -> dict:
    return next(
        volume
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "objectstore"
    )


def _notes(*set_args: str) -> str:
    """Render NOTES.txt offline through a client-side dry-run install."""
    cmd = [
        "helm",
        "install",
        "rel",
        str(_CHART),
        "--dry-run=client",
        "--set",
        "image.allowUnpinned=true",
    ]
    for item in set_args:
        cmd += ["--set", item]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.split("NOTES:", 1)[1]


def test_worker_runs_by_default_in_the_reference_topology() -> None:
    """With its prerequisites bundled, a release without any worker value
    renders the worker and dispatches through the Valkey queue."""
    docs = _docs(
        _template(
            "postgres.enabled=true",
            "valkey.enabled=true",
            "s3.enabled=true",
            single_pod=False,
        )
    )
    worker = _deployment(docs, "worker")
    assert worker is not None, "the worker is part of the default topology"
    assert worker["spec"]["replicas"] == 1
    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "s3"
    assert config["INQTRIX_REPLICA_COUNT"] == "2"


def test_default_install_names_every_missing_worker_prerequisite() -> None:
    """One render failure lists everything the default worker still lacks,
    plus the single-pod way out, instead of revealing it one retry at a time.
    Asserted on the topology check's own prose."""
    with pytest.raises(RuntimeError) as failure:
        _template(_MANAGED_EXTERNAL, single_pod=False)
    message = str(failure.value)
    assert "worker.enabled=true (the chart default)" in message
    assert "needs: a Valkey queue (valkey.enabled=true" in message
    assert "; a shared object store (s3.enabled=true" in message
    assert "INQTRIX_STORAGE_BACKEND=postgres (found" not in message
    assert "set worker.enabled=false for the single-pod variant" in message


def test_worker_derives_the_valkey_queue_for_an_external_broker() -> None:
    """Every Valkey URL source that reaches the pods enables the queue.

    The chart cannot read a user-managed Secret, so a URL kept only there
    must be confirmed through the queue backend; otherwise API and worker
    would start without a queue URL after an upgrade.
    """
    in_secret_data = _docs(
        _template(
            _MANAGED_EXTERNAL,
            "secret.data.INQTRIX_VALKEY_URL=redis://:SyntheticValkey2026@broker:6379/0",
            *_EXTERNAL_S3,
            single_pod=False,
        )
    )
    assert _deployment(in_secret_data, "worker") is not None
    config = _by_kind(in_secret_data, "ConfigMap")[0]["data"]
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"

    in_extra_config = _docs(
        _template(
            _MANAGED_EXTERNAL,
            "extraConfig.INQTRIX_VALKEY_URL=redis://broker:6379/0",
            *_EXTERNAL_S3,
            single_pod=False,
        )
    )
    config = _by_kind(in_extra_config, "ConfigMap")[0]["data"]
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"

    with pytest.raises(
        RuntimeError,
        match=r"secret\.existingSecret, confirmed with config\.INQTRIX_QUEUE_BACKEND=valkey",
    ):
        _template(_EXTERNAL, *_EXTERNAL_S3, single_pod=False)
    confirmed = _docs(
        _template(
            _EXTERNAL,
            "config.INQTRIX_QUEUE_BACKEND=valkey",
            *_EXTERNAL_S3,
            single_pod=False,
        )
    )
    assert _deployment(confirmed, "worker") is not None


def test_worker_requires_the_postgres_run_store() -> None:
    """The worker claims runs from Postgres; a memory store strands them."""
    with pytest.raises(
        RuntimeError, match=r'needs: INQTRIX_STORAGE_BACKEND=postgres \(found "memory"\)'
    ):
        _template(
            "valkey.enabled=true",
            "s3.enabled=true",
            "config.INQTRIX_STORAGE_BACKEND=memory",
            "migrations.enabled=false",
            single_pod=False,
        )


def test_explicit_memory_queue_contradicts_the_worker() -> None:
    """An explicit in-process queue would win over the derived one and leave
    the worker without work while the API runs every job itself."""
    with pytest.raises(RuntimeError, match="contradicts worker.enabled=true"):
        _template(
            _MANAGED_EXTERNAL,
            "valkey.enabled=true",
            "s3.enabled=true",
            "config.INQTRIX_QUEUE_BACKEND=memory",
            single_pod=False,
        )


def test_in_process_runs_refuse_a_second_api_replica() -> None:
    """Without the worker each API process fails the runs another process
    executes as lost, so the single-pod variant allows exactly one replica,
    also with a shared object store."""
    with pytest.raises(RuntimeError, match=r"exactly one API replica \(2 requested"):
        _template(_MANAGED_EXTERNAL, "s3.enabled=true", "api.replicaCount=2")
    with pytest.raises(RuntimeError, match=r"exactly one API replica \(3 requested"):
        _template(
            _MANAGED_EXTERNAL,
            "s3.enabled=true",
            "api.autoscaling.enabled=true",
            "api.autoscaling.maxReplicas=3",
        )
    single = _docs(_template(_MANAGED_EXTERNAL, "s3.enabled=true"))
    assert _deployment(single, "worker") is None
    assert _deployment(single, "api")["spec"]["replicas"] == 1


def test_valkey_queue_without_a_worker_is_refused() -> None:
    """Queued runs need a consumer; without the worker none would start."""
    with pytest.raises(RuntimeError, match="renders none, so queued runs would never start"):
        _template(_MANAGED_EXTERNAL, "config.INQTRIX_QUEUE_BACKEND=valkey")


def test_worker_grace_period_covers_its_drain_window() -> None:
    """Kubernetes must not kill a draining worker before its drain window
    ends, and the schema-upgrade hook must outwait the grace period."""
    source = (_ROOT / "src" / "inqtrix" / "worker" / "__main__.py").read_text(
        encoding="utf-8"
    )
    drain = float(re.search(r"^_DRAIN_SECONDS = ([0-9.]+)$", source, re.M).group(1))
    docs = _docs(
        _template(
            "postgres.enabled=true",
            "valkey.enabled=true",
            "s3.enabled=true",
            single_pod=False,
        )
    )
    grace = _deployment(docs, "worker")["spec"]["template"]["spec"][
        "terminationGracePeriodSeconds"
    ]
    assert grace >= drain + 15, (grace, drain)
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert grace < values["migrations"]["maintenanceTimeoutSeconds"]


def test_collaboration_memory_follows_the_heap_rule() -> None:
    """Node caps its heap at about half the container limit, so the limit is
    twice the request, and the request covers the idle footprint."""
    docs = _docs(
        _template(
            _EXTERNAL,
            "collaboration.enabled=true",
            "collaboration.secret.existingSecret=collaboration-secret",
        )
    )
    resources = _deployment(docs, "collaboration")["spec"]["template"]["spec"][
        "containers"
    ][0]["resources"]
    assert resources["requests"]["memory"] == "512Mi"
    assert resources["limits"]["memory"] == "1Gi"

    def mebibytes(value: str) -> int:
        number, unit = re.fullmatch(r"(\d+)(Mi|Gi)", value).groups()
        return int(number) * (1024 if unit == "Gi" else 1)

    assert mebibytes(resources["limits"]["memory"]) == 2 * mebibytes(
        resources["requests"]["memory"]
    )


def test_object_store_volume_is_mounted_only_for_the_local_backend() -> None:
    """Only the local backend reads the PVC; mounting it into API and worker
    with S3 would leave one ReadWriteOnce claimant Pending on another node."""
    shared = _docs(
        _template(
            _MANAGED_EXTERNAL,
            "valkey.enabled=true",
            "s3.enabled=true",
            "persistence.enabled=true",
            single_pod=False,
        )
    )
    assert any(
        claim["metadata"]["name"] == "rel-inqtrix-objectstore"
        for claim in _by_kind(shared, "PersistentVolumeClaim")
    ), "the claim stays until persistence is disabled"
    for component in ("api", "worker"):
        volume = _object_store_volume(_deployment(shared, component))
        assert volume == {"name": "objectstore", "emptyDir": {}}, component

    local = _docs(_template(_MANAGED_EXTERNAL, "persistence.enabled=true"))
    volume = _object_store_volume(_deployment(local, "api"))
    assert volume["persistentVolumeClaim"] == {
        "claimName": "rel-inqtrix-objectstore"
    }


def test_render_gate_values_cover_the_worker() -> None:
    """The documented gate renders the default topology, worker included."""
    docs = _docs(
        _template(
            extra=["-f", str(_CHART / "values-dev.yaml")],
            single_pod=False,
        )
    )
    assert _deployment(docs, "worker") is not None
    config = _by_kind(docs, "ConfigMap")[0]["data"]
    assert config["INQTRIX_QUEUE_BACKEND"] == "valkey"
    assert config["INQTRIX_OBJECT_STORE_BACKEND"] == "s3"


def test_notes_describe_the_object_store_the_release_actually_uses() -> None:
    """S3 configured through extraConfig is a shared store and draws no
    warning; a leftover local volume next to S3 is named with its way out;
    the single-pod variant states its limits."""
    shared_via_extra = _notes(
        _EXTERNAL,
        "config.INQTRIX_QUEUE_BACKEND=valkey",
        "extraConfig.INQTRIX_OBJECT_STORE_BACKEND=s3",
        "extraConfig.INQTRIX_S3_BUCKET=managed-bucket",
    )
    assert "WARNING" not in shared_via_extra
    assert "persistence.enabled keeps the local object-store volume" not in (
        shared_via_extra
    )
    assert "Single-pod variant" not in shared_via_extra

    leftover = _notes(
        "postgres.enabled=true",
        "postgres.auth.password=SyntheticPostgres2026",
        "valkey.enabled=true",
        "valkey.password=SyntheticValkey2026",
        "s3.enabled=true",
        "s3.secretKey=SyntheticMinio2026",
        "persistence.enabled=true",
    )
    assert "persistence.enabled keeps the local object-store volume" in leftover

    single_pod = _notes(
        "postgres.enabled=true",
        "postgres.auth.password=SyntheticPostgres2026",
        "worker.enabled=false",
    )
    assert "Single-pod variant (worker.enabled=false)" in single_pod
