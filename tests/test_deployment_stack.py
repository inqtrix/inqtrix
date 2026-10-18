"""Deployment contracts shared by the production Compose stack."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parent.parent


def test_compose_scopes_migration_environment_to_one_shot_job() -> None:
    """A privileged migration DSN must never enter API or worker env files."""
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    services = stack["services"]
    migrate = services["migrate"]

    assert migrate["command"] == ["inqtrix-migrate"]
    assert "env_file" not in migrate
    assert migrate["environment"]["INQTRIX_MIGRATION_RLS_MODE"] == (
        "${INQTRIX_MIGRATION_RLS_MODE:-auto}"
    )
    assert migrate["environment"]["INQTRIX_DATABASE_URL"] == (
        "postgresql+asyncpg://${INQTRIX_PG_USER:-inqtrix}:"
        "${INQTRIX_PG_PASSWORD:-}@postgres:5432/"
        "${INQTRIX_PG_DB:-inqtrix}"
    )
    assert "@postgres:5432" in migrate["environment"]["INQTRIX_DATABASE_URL"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )

    for service_name in ("api", "worker"):
        assert services[service_name]["env_file"] == [
            {
                "path": (
                    "../../${INQTRIX_SECRETS_FILE"
                    ":-deploy/.env.stack.secrets}"
                ),
                "required": True,
            },
            {
                "path": "../../${INQTRIX_ENV_FILE:-deploy/.env.stack}",
                "required": True,
            },
        ]
        assert services[service_name]["environment"][
            "INQTRIX_DATABASE_URL"
        ] == (
            "${INQTRIX_DATABASE_URL:?set INQTRIX_DATABASE_URL in the paired "
            "Compose env files}"
        )
        assert services[service_name]["environment"][
            "INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY"
        ] == "${INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY:-bundled_legacy}"
        assert services[service_name]["environment"][
            "INQTRIX_MIGRATION_DATABASE_URL"
        ] == ""
        mounts = services[service_name]["volumes"]
        assert any(
            mount.get("source") == "${INQTRIX_S3_CA_BUNDLE_HOST:-/dev/null}"
            and mount.get("target") == "/etc/inqtrix/object-store/ca.pem"
            for mount in mounts
            if isinstance(mount, dict)
        )

    healthcheck = " ".join(services["api"]["healthcheck"]["test"])
    assert "/readyz" in healthcheck
    assert "/health" not in healthcheck


def test_langfuse_worker_waits_for_the_schema_migrating_web_service() -> None:
    """Default seeds must not run before the official schema migrations."""
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )

    dependency = stack["services"]["langfuse-worker"]["depends_on"][
        "langfuse"
    ]
    assert dependency == {"condition": "service_healthy"}


def test_compose_external_db_override_detaches_bundled_postgres() -> None:
    """The external-database switch stays consistent across both files.

    Contract: (1) the default stack keeps the bundled postgres profile-free
    (it always starts), (2) every ``depends_on: postgres`` entry is marked
    ``required: false`` so the override can deactivate the service without
    failing dependents, and (3) the override moves ONLY the bundled
    database behind an inactive profile — migrate/api/worker keep running
    against the external DSN.
    """
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    services = stack["services"]
    assert "profiles" not in services["postgres"]

    dependents = [
        name
        for name, service in services.items()
        if "postgres" in (service.get("depends_on") or {})
    ]
    assert set(dependents) == {
        "pgbouncer",
        "migrate",
        "api",
        "worker",
        # observability profile: Langfuse rides the bundled Postgres with
        # its own database; with an external DB the operator provisions it
        # manually and sets LANGFUSE_DATABASE_URL (see the profile notes).
        "langfuse-init-db",
        "langfuse-worker",
        "langfuse",
    }
    for name in dependents:
        entry = services[name]["depends_on"]["postgres"]
        assert entry["condition"] == "service_healthy"
        assert entry["required"] is False, (
            f"depends_on.postgres of {name!r} must be required: false so the "
            "external-db override can deactivate the bundled database"
        )

    override = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.external-db.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert set(override["services"]) == {"migrate", "postgres"}
    assert override["services"]["postgres"]["profiles"] == [
        "bundled-db-disabled"
    ]
    assert override["services"]["migrate"]["env_file"] == [
        {
            "path": (
                "../../${INQTRIX_MIGRATION_ENV_FILE:"
                "?set a root-relative migration env file}"
            ),
            "required": True,
        }
    ]


def test_noqueue_override_pins_in_process_execution_to_the_api_service() -> None:
    """A worker-less stack must actually run in-process, not enqueue.

    The paired stack env may pin ``INQTRIX_QUEUE_BACKEND=valkey``; merely
    omitting the workers profile would then leave the API enqueueing into
    a broker that never starts. The override flips exactly one key on
    exactly one service — anything more would fork stack policy.
    """
    override = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.noqueue.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert override["name"] == "inqtrix"
    assert set(override) == {"name", "services"}
    assert set(override["services"]) == {"api"}
    assert override["services"]["api"] == {
        "environment": {"INQTRIX_QUEUE_BACKEND": "memory"}
    }


def test_compose_web_ingress_requires_explicit_non_loopback_bind() -> None:
    """LAN exposure is configurable without weakening the safe default."""
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert stack["services"]["web"]["ports"] == [
        "${INQTRIX_WEB_BIND_ADDRESS:-127.0.0.1}:${INQTRIX_WEB_PORT:-8080}:8080"
    ]


def test_dev_ports_override_is_loopback_only_and_changes_no_service_policy() -> None:
    override = yaml.safe_load(
        (
            _ROOT / "deploy" / "compose" / "compose.dev-ports.yaml"
        ).read_text(encoding="utf-8")
    )

    assert override["name"] == "inqtrix-dev"
    assert set(override) == {"name", "services"}
    assert set(override["services"]) == {
        "api",
        "collaboration",
        "postgres",
        "qdrant",
        "seaweedfs",
        "valkey",
    }
    for service in override["services"].values():
        assert set(service) == {"ports"}
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])


def test_bundled_qdrant_fails_closed_without_a_real_api_key() -> None:
    """Raw Compose must not bypass the deployment CLI's knowledge preflight."""
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    qdrant = stack["services"]["qdrant"]

    assert qdrant["environment"]["QDRANT__SERVICE__API_KEY"] == (
        "${INQTRIX_QDRANT_API_KEY:-}"
    )
    assert qdrant["command"][:2] == ["/usr/bin/bash", "-euc"]
    guard = qdrant["command"][2]
    assert "$${QDRANT__SERVICE__API_KEY}" in guard
    assert "CHANGE_ME" in guard
    assert "exec ./entrypoint.sh" in guard
    assert "QDRANT__SERVICE__API_KEY" not in " ".join(
        qdrant["healthcheck"]["test"]
    )


@pytest.mark.parametrize("service", ("valkey", "langfuse-valkey"))
def test_bundled_broker_healthcheck_does_not_put_password_in_argv(
    service: str,
) -> None:
    """The broker CLI receives auth through its environment, not via ``-a``.

    The binary name is engine-selected, so the probe is asserted on the
    ``-cli`` suffix plus the interpolation default rather than on a fixed
    ``valkey-cli`` literal.
    """
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    probe = " ".join(stack["services"][service]["healthcheck"]["test"])

    assert "REDISCLI_AUTH=" in probe
    assert "${INQTRIX_BROKER_ENGINE:-valkey}-cli ping" in probe
    assert "-cli -a" not in probe


@pytest.mark.parametrize("service", ("valkey", "langfuse-valkey"))
def test_bundled_broker_server_and_probe_share_one_engine_selector(
    service: str,
) -> None:
    """One variable drives both binaries, so they cannot drift apart."""
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    definition = stack["services"][service]
    command = " ".join(definition["command"])

    assert "exec ${INQTRIX_BROKER_ENGINE:-valkey}-server" in command
    assert "exec valkey-server" not in command
    assert definition["image"].startswith("${INQTRIX_BROKER_IMAGE:-")


def test_direct_tls_uses_one_public_origin_across_runtime_services() -> None:
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    nginx = yaml.safe_load(
        (
            _ROOT / "deploy" / "compose" / "compose.web-nginx.yaml"
        ).read_text(encoding="utf-8")
    )
    tls = yaml.safe_load(
        (
            _ROOT / "deploy" / "compose" / "compose.web-tls.yaml"
        ).read_text(encoding="utf-8")
    )

    assert stack["services"]["web"]["environment"][
        "INQTRIX_WEB_ADAPTER"
    ] == "python"
    assert stack["services"]["web"]["environment"][
        "INQTRIX_DIRECT_TLS"
    ] == "false"
    assert nginx["services"]["web"]["environment"][
        "INQTRIX_WEB_ADAPTER"
    ] == "nginx"
    assert "init" not in nginx["services"]["web"]
    assert tls["services"]["web"]["environment"][
        "INQTRIX_DIRECT_TLS"
    ] == "true"
    expected_public_origin = (
        "${INQTRIX_PUBLIC_BASE_URL:?set the exact https public origin}"
    )
    assert tls["services"]["web"]["environment"][
        "INQTRIX_PUBLIC_BASE_URL"
    ] == expected_public_origin
    assert tls["services"]["api"]["environment"][
        "INQTRIX_PUBLIC_BASE_URL"
    ] == expected_public_origin
    assert tls["services"]["api"]["environment"][
        "INQTRIX_OIDC_INSECURE_DEV_COOKIES"
    ] == "false"
    assert tls["services"]["api"]["environment"][
        "INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP"
    ] == "false"
    assert tls["services"]["worker"]["environment"][
        "INQTRIX_PUBLIC_BASE_URL"
    ] == expected_public_origin


def test_nginx_entrypoint_rejects_the_direct_tls_adapter_conflict() -> None:
    validator = _ROOT / "deploy" / "nginx" / "10-inqtrix-env.envsh"
    completed = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "INQTRIX_WEB_ADAPTER": "nginx",
            "INQTRIX_DIRECT_TLS": "true",
        },
    )

    assert completed.returncode != 0
    assert (
        "INQTRIX_DIRECT_TLS=true is supported only by the Python web adapter"
        in completed.stderr
    )
    assert "compose.web-nginx.yaml with compose.web-tls.yaml" in (
        completed.stderr
    )

    stale_python_tls = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "INQTRIX_WEB_ADAPTER": "nginx",
            "INQTRIX_DIRECT_TLS": "false",
            "RESEARCH_DESK_SSL_CERTFILE": "/synthetic/tls.crt",
            "RESEARCH_DESK_SSL_KEYFILE": "/synthetic/tls.key",
        },
    )
    assert stale_python_tls.returncode != 0
    assert (
        "RESEARCH_DESK_SSL_* direct-TLS variables are supported only by the "
        "Python web adapter"
    ) in stale_python_tls.stderr
    assert "nginx requires external TLS termination" in stale_python_tls.stderr


def test_nginx_public_origin_rejects_a_conflicting_external_scheme() -> None:
    validator = _ROOT / "deploy" / "nginx" / "10-inqtrix-env.envsh"
    completed = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "INQTRIX_WEB_ADAPTER": "nginx",
            "INQTRIX_DIRECT_TLS": "false",
            "INQTRIX_PUBLIC_BASE_URL": "https://desk.example",
            "INQTRIX_EXTERNAL_SCHEME": "http",
        },
    )

    assert completed.returncode != 0
    assert (
        "INQTRIX_EXTERNAL_SCHEME must match the scheme in "
        "INQTRIX_PUBLIC_BASE_URL"
    ) in completed.stderr


def test_nginx_rejects_unverified_https_backend() -> None:
    """The optional adapter cannot silently skip upstream TLS verification."""
    validator = _ROOT / "deploy" / "nginx" / "10-inqtrix-env.envsh"
    completed = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "INQTRIX_WEB_ADAPTER": "nginx",
            "INQTRIX_DIRECT_TLS": "false",
            "INQTRIX_BACKEND_URL": "https://api.internal:5100",
        },
    )

    assert completed.returncode != 0
    assert "only a private HTTP backend" in completed.stderr


def test_nginx_public_origin_uses_one_explicit_or_runtime_source(
    tmp_path: Path,
) -> None:
    validator = _ROOT / "deploy" / "nginx" / "10-inqtrix-env.envsh"
    command = (
        '. "$1" && printf "%s\\n%s\\n" '
        '"$INQTRIX_FORWARDED_SCHEME" "$INQTRIX_FORWARDED_HOST"'
    )
    base_environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "INQTRIX_WEB_ADAPTER": "nginx",
        "INQTRIX_DIRECT_TLS": "false",
        "NGINX_ENVSUBST_OUTPUT_DIR": str(tmp_path / "nginx-conf"),
    }

    runtime_origin = subprocess.run(
        ["/bin/sh", "-c", command, "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env=base_environment,
    )
    assert runtime_origin.returncode == 0
    assert runtime_origin.stdout.splitlines() == ["$scheme", "$http_host"]

    explicit_origin = subprocess.run(
        ["/bin/sh", "-c", command, "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **base_environment,
            "INQTRIX_PUBLIC_BASE_URL": "https://desk.example:8443",
            "INQTRIX_EXTERNAL_SCHEME": "https",
        },
    )
    assert explicit_origin.returncode == 0
    assert explicit_origin.stdout.splitlines() == [
        "https",
        "desk.example:8443",
    ]


def test_bundled_postgres_connection_ceiling_is_configurable() -> None:
    """The bundled server must be sizeable without editing the stack file.

    An api and a worker together ask for more connections than the image
    default allows, and each process reports its own budget at startup —
    so the operator can see the shortfall but, without this, cannot act
    on it except by abandoning the bundled database.
    """
    stack = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    command = stack["services"]["postgres"]["command"]
    assert command[0] == "postgres"
    assert "-c" in command
    setting = command[command.index("-c") + 1]
    name, _, value = setting.partition("=")
    assert name == "max_connections"
    # Interpolated, so an operator sets it without touching this file, and
    # the image default stays in force for anyone who does not.
    assert value.startswith("${INQTRIX_BUNDLED_POSTGRES_MAX_CONNECTIONS")
    assert value.endswith(":-300}")
