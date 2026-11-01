"""Deployment contracts shared by the production Compose stack."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parent.parent


def _stack_services() -> dict:
    return yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(encoding="utf-8")
    )["services"]


def test_compose_passes_only_threshold_and_format_to_each_service() -> None:
    """Gateway, collaboration server and migration job get the two logging
    values one by one, with empty defaults: an empty value keeps each
    service's own default and leaves room for ``INQTRIX_LOG_CONFIG``."""
    services = _stack_services()
    web = services["web"]["environment"]
    collaboration = services["collaboration"]["environment"]
    migrate = services["migrate"]["environment"]

    assert web["INQTRIX_LOG_LEVEL"] == "${INQTRIX_LOG_LEVEL:-}"
    assert web["INQTRIX_LOG_FORMAT"] == "${INQTRIX_LOG_FORMAT:-}"
    # The collaboration server always writes JSON; it reads the threshold.
    assert collaboration["INQTRIX_LOG_LEVEL"] == "${INQTRIX_LOG_LEVEL:-}"
    assert "INQTRIX_LOG_FORMAT" not in collaboration
    assert migrate["INQTRIX_LOG_LEVEL"] == "${INQTRIX_LOG_LEVEL:-}"
    assert migrate["INQTRIX_LOG_FORMAT"] == "${INQTRIX_LOG_FORMAT:-}"
    for name in ("web", "collaboration", "migrate"):
        assert "env_file" not in services[name]


def test_compose_stops_the_api_before_the_stores_it_probes() -> None:
    """Compose stops a service before what it depends on: the API's
    availability probes then never see Qdrant or Valkey gone during a stop.
    ``service_started`` leaves the start as it was."""
    depends_on = _stack_services()["api"]["depends_on"]

    for store in ("qdrant", "valkey"):
        assert depends_on[store] == {"condition": "service_started", "required": False}


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


_GATEWAY_HEADER_VARIABLES = (
    "INQTRIX_GATEWAY_JWT_HEADER",
    "INQTRIX_GATEWAY_SUBJECT_HEADER",
    "INQTRIX_GATEWAY_EMAIL_HEADER",
    "INQTRIX_GATEWAY_NAME_HEADER",
    "INQTRIX_GATEWAY_GROUPS_HEADER",
    "INQTRIX_GATEWAY_PROOF_HEADER",
    "INQTRIX_GATEWAY_RESOLVER_HEADERS",
)


def test_compose_hands_the_web_gateway_its_identity_edge() -> None:
    """The web service has no env_file: the header names, its own peer list
    and the mode reach it one by one, and no secret ever does."""
    web = _stack_services()["web"]["environment"]

    for name in _GATEWAY_HEADER_VARIABLES:
        assert web[name] == f"${{{name}:-}}"
    # The web gateway trusts the API gateway, the API server trusts the web
    # gateway: one variable name, two lists, so the web one has its own source.
    assert web["INQTRIX_GATEWAY_TRUSTED_PROXIES"] == "${INQTRIX_WEB_GATEWAY_TRUSTED_PROXIES:-}"
    assert web["INQTRIX_AUTH_MODE"] == "${INQTRIX_AUTH_MODE:-}"
    assert not [name for name in web if "SECRET" in name or "TOKEN" in name]


def test_nginx_entrypoint_refuses_the_gateway_mode() -> None:
    validator = _ROOT / "deploy" / "nginx" / "10-inqtrix-env.envsh"
    completed = subprocess.run(
        ["/bin/sh", "-c", '. "$1"', "sh", str(validator)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "INQTRIX_WEB_ADAPTER": "nginx",
            "INQTRIX_AUTH_MODE": "gateway",
        },
    )

    assert completed.returncode != 0
    assert "INQTRIX_AUTH_MODE=gateway" in completed.stderr
    assert "Python web adapter" in completed.stderr


def test_compose_mounts_the_folder_of_a_resolver_of_its_own_read_only() -> None:
    """INQTRIX_GATEWAY_RESOLVER loads its module from this folder; unset, the
    bind is /dev/null, which Python skips on its path."""
    api = _stack_services()["api"]

    assert api["environment"]["PYTHONPATH"] == "/opt/inqtrix-plugins"
    assert any(
        isinstance(mount, dict)
        and mount.get("source") == "${INQTRIX_GATEWAY_RESOLVER_DIR:-/dev/null}"
        and mount.get("target") == "/opt/inqtrix-plugins"
        and mount.get("read_only") is True
        for mount in api["volumes"]
    )


# ------------------------------------------------------------------ #
# The local acceptance of the gateway mode (overlay, realm, oauth2-proxy)
# ------------------------------------------------------------------ #

_COMPOSE = _ROOT / "deploy" / "compose"
_IDENTITY_HEADER_VARIABLES = _GATEWAY_HEADER_VARIABLES[:-1]


def _acceptance() -> dict:
    return yaml.safe_load((_COMPOSE / "compose.gateway-acceptance.yaml").read_text(encoding="utf-8"))


def _acceptance_proxy(assertion: str) -> dict:
    path = _COMPOSE / f"gateway-acceptance-oauth2-proxy-{assertion}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _acceptance_realm() -> dict:
    return json.loads((_COMPOSE / "gateway-acceptance-realm.json").read_text(encoding="utf-8"))


def _proxy_flags() -> dict[str, list[str]]:
    flags: dict[str, list[str]] = {}
    for argument in _acceptance()["services"]["oauth2-proxy"]["command"]:
        name, _, value = argument.partition("=")
        flags.setdefault(name, []).append(value)
    return flags


def _seconds(duration: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return sum(int(amount) * units[unit] for amount, unit in re.findall(r"(\d+)([smh])", duration))


def test_the_gateway_acceptance_pins_its_images_and_stays_on_loopback() -> None:
    services = _acceptance()["services"]

    for name in ("keycloak", "oauth2-proxy"):
        image = services[name]["image"]
        assert re.fullmatch(r"quay\.io/[^:@\s]+:[^:@\s]+@sha256:[0-9a-f]{64}", image), name
        assert not re.search(r":(?:latest|nightly)@", image), name
        assert all(port.startswith("127.0.0.1:") for port in services[name]["ports"]), name


def test_oauth2_proxy_passes_the_token_in_its_own_header_never_by_a_flag() -> None:
    # --pass-access-token would use X-Forwarded-Access-Token, a start error.
    flags = _proxy_flags()
    gateway = _acceptance()["x-gateway-mode"]

    assert "--pass-access-token" not in flags
    assert "--pass-authorization-header" not in flags
    assert flags["--skip-jwt-bearer-tokens"] == ["false"]
    assert flags["--reverse-proxy"] == ["false"]
    assert flags["--trusted-proxy-ip"] and "0.0.0.0/0" not in flags["--trusted-proxy-ip"]
    [injected] = _acceptance_proxy("jwt")["injectRequestHeaders"]
    assert injected["name"] == gateway["INQTRIX_GATEWAY_JWT_HEADER"]
    assert injected["name"].lower() != "authorization"
    assert injected["values"] == [{"claimSource": {"claim": "access_token"}}]


def test_oauth2_proxy_renews_the_token_and_waits_for_the_longest_request() -> None:
    refresh = _seconds(_proxy_flags()["--cookie-refresh"][0])

    assert 0 < refresh < _acceptance_realm()["accessTokenLifespan"]
    for assertion in ("jwt", "header"):
        [upstream] = _acceptance_proxy(assertion)["upstreamConfig"]["upstreams"]
        # A chat answer without streaming waits up to 3,630 s for its first byte.
        assert _seconds(upstream["timeout"]) > 3630, assertion
        assert upstream["proxyWebSockets"] is True, assertion


def test_the_header_assertion_sets_every_identity_header_and_the_proof_from_a_secret() -> None:
    gateway = _acceptance()["x-gateway-mode"]
    injected = {item["name"]: item["values"] for item in _acceptance_proxy("header")["injectRequestHeaders"]}
    proof = gateway["INQTRIX_GATEWAY_PROOF_HEADER"]

    # Every identity header the API reads in this assertion is one oauth2-proxy
    # sets, and so clears when a client sends it.
    assert set(injected) == {gateway[name] for name in _IDENTITY_HEADER_VARIABLES[1:] if name in gateway}
    assert injected[proof] and all(set(value) == {"secretSource"} for value in injected[proof])
    for name, values in injected.items():
        if name != proof:
            assert all(set(value) == {"claimSource"} for value in values), name


def test_the_edge_trusts_only_oauth2_proxy_and_the_api_only_the_edge() -> None:
    overlay = _acceptance()
    subnet = ipaddress.ip_network(overlay["networks"]["default"]["ipam"]["config"][0]["subnet"])
    proxy = overlay["services"]["oauth2-proxy"]["networks"]["default"]["ipv4_address"]
    web = overlay["services"]["web"]
    edge = web["networks"]["default"]["ipv4_address"]

    assert web["environment"]["INQTRIX_GATEWAY_TRUSTED_PROXIES"] == f"{proxy}/32"
    assert overlay["x-gateway-mode"]["INQTRIX_GATEWAY_TRUSTED_PROXIES"] == f"{edge}/32"
    assert ipaddress.ip_address(proxy) in subnet and ipaddress.ip_address(edge) in subnet
    # The edge strips every identity header the API reads.
    for name in _IDENTITY_HEADER_VARIABLES:
        assert web["environment"].get(name) == overlay["x-gateway-mode"].get(name), name
    for service in ("api", "worker"):
        assert overlay["services"][service]["environment"]["INQTRIX_AUTH_MODE"] == "gateway", service


def test_the_test_realm_grants_the_user_scope_only_to_the_sign_in_of_a_person() -> None:
    realm = _acceptance_realm()
    gateway = _acceptance()["x-gateway-mode"]
    scopes = {scope["name"]: scope for scope in realm["clientScopes"]}
    clients = {client["clientId"]: client for client in realm["clients"]}
    user_scope = gateway["INQTRIX_GATEWAY_JWT_USER_SCOPE"]
    sign_in, service = clients["inqtrix-gateway"], clients["inqtrix-service"]

    assert scopes[user_scope]["attributes"]["include.in.token.scope"] == "true"
    assert {"basic", user_scope} <= set(sign_in["defaultClientScopes"])
    assert sign_in["serviceAccountsEnabled"] is False
    assert sign_in["directAccessGrantsEnabled"] is False
    assert sign_in["attributes"]["client.use.lightweight.access.token.enabled"] == "false"
    assert service["serviceAccountsEnabled"] is True
    assert user_scope not in service["defaultClientScopes"] + service["optionalClientScopes"]
    for client in (sign_in, service):
        assert any(
            mapper["protocolMapper"] == "oidc-audience-mapper"
            and mapper["config"]["included.custom.audience"] == gateway["INQTRIX_GATEWAY_JWT_AUDIENCE"]
            for mapper in client["protocolMappers"]
        ), client["clientId"]


def test_keycloak_keeps_one_fixed_issuer_for_browser_proxy_and_api() -> None:
    command = _acceptance()["services"]["keycloak"]["command"]
    hostname = [argument.partition("=")[2] for argument in command if argument.startswith("--hostname=")]
    issuer = f"{hostname[0]}/realms/{_acceptance_realm()['realm']}"

    assert len(hostname) == 1
    assert not any("hostname-strict" in argument for argument in command)
    assert _acceptance()["x-gateway-mode"]["INQTRIX_GATEWAY_JWT_ISSUER"] == issuer
    for assertion in ("jwt", "header"):
        [provider] = _acceptance_proxy(assertion)["providers"]
        assert provider["oidcConfig"]["issuerURL"] == issuer, assertion


def test_the_overlay_and_the_realm_hold_no_secret() -> None:
    overlay = _acceptance()
    realm = _acceptance_realm()

    for client in realm["clients"]:
        assert re.fullmatch(r"\$\{[A-Z_]+\}", client["secret"]), client["clientId"]
    for user in realm["users"]:
        assert all(re.fullmatch(r"\$\{[A-Z_]+\}", credential["value"]) for credential in user["credentials"])
    environments = [overlay["x-gateway-mode"]] + [
        service.get("environment", {}) for service in overlay["services"].values()
    ]
    for environment in environments:
        for name, value in environment.items():
            if re.search(r"SECRET|PASSWORD|TOKEN", name):
                assert re.fullmatch(r"\$\{[A-Z_]+:[?-][^}]*\}", str(value)), name
    # oauth2-proxy reads its secrets from files and the environment, never from
    # a flag or its own configuration.
    for flag in _proxy_flags():
        assert not re.search(r"secret|password", flag), flag
    for assertion in ("jwt", "header"):
        proxy = _acceptance_proxy(assertion)
        for provider in proxy["providers"]:
            assert "clientSecret" not in provider and provider["clientSecretFile"].startswith("/run/secrets/")
        for header in proxy["injectRequestHeaders"]:
            for value in header["values"]:
                source = value.get("secretSource")
                if source is not None:
                    assert set(source) == {"fromFile"}, header["name"]


def test_only_the_ways_without_a_signed_in_person_skip_the_sign_in() -> None:
    routes = _proxy_flags()["--skip-auth-route"]
    # oauth2-proxy searches a route anywhere in the path, so every route is
    # anchored, and member paths that contain a guest part stay signed in.
    signed_in = (
        "/",
        "/api/auth/session",
        "/v1/runs",
        "/v1/editor/documents",
        "/v1/editor/documents/doc_1/collaboration/session",
        "/v1/assets/sections",
        "/v1/scim/v2/Users",
    )

    assert all(route.startswith("^") for route in routes), routes
    for path in ("/s/token", "/v1/editor/share-links/token:unlock", "/v1/editor/guest/session", "/collaboration", "/assets/app.js"):
        assert any(re.search(route, path) for route in routes), path
    for path in signed_in:
        assert not any(re.search(route, path) for route in routes), path


def test_a_signed_out_tab_starts_no_sign_in_and_parallel_sign_ins_keep_their_cookies() -> None:
    # The app polls /health: as an API route, a signed-out tab gets 401 there
    # instead of a sign-in redirect that starts a new flow every few seconds.
    # With one CSRF cookie per sign-in, a flow started in another tab cannot
    # overwrite the one the person completes, which Keycloak would otherwise
    # refuse at the callback (PKCE mismatch).
    flags = _proxy_flags()
    api_routes = flags["--api-route"]

    for path in ("/health", "/api/auth/session", "/v1/runs"):
        assert any(re.search(route, path) for route in api_routes), path
    for path in ("/", "/healthy-page", "/s/token"):
        assert not any(re.search(route, path) for route in api_routes), path
    assert flags["--cookie-csrf-per-request"] == ["true"]


def test_signing_out_ends_the_identity_provider_session_without_asking() -> None:
    # oauth2-proxy ends the Keycloak session server to server with the
    # person's ID token, so Keycloak asks no confirmation and no other tab
    # signs in again through it; the browser then returns to the start page,
    # where Keycloak asks for the password again.
    from urllib.parse import parse_qs, urlsplit

    logout = urlsplit(_acceptance()["x-gateway-mode"]["INQTRIX_GATEWAY_LOGOUT_URL"])

    assert (logout.scheme, logout.netloc, logout.path) == ("", "", "/oauth2/sign_out")
    assert parse_qs(logout.query) == {"rd": ["/"]}
    for assertion in ("jwt", "header"):
        (provider,) = _acceptance_proxy(assertion)["providers"]
        backend = urlsplit(provider["backendLogoutURL"])
        endpoint = f"{backend.scheme}://{backend.netloc}{backend.path}"
        assert endpoint == provider["oidcConfig"]["issuerURL"] + "/protocol/openid-connect/logout", assertion
        assert parse_qs(backend.query) == {"id_token_hint": ["{id_token}"]}, assertion

