"""Static contracts for the optional collaboration deployment surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from inqtrix.settings import CollaborationSettings

_ROOT = Path(__file__).resolve().parent.parent

# The transport limits the session API returns to the browser, keyed by the
# variable Node reads and mapped to the API's setting. The browser can only
# pace and bound its frames correctly when API and Node resolve each variable
# to the same number in every deployment.
_PUBLISHED_TRANSPORT_LIMITS = {
    "INQTRIX_COLLABORATION_UPDATE_RATE_COUNT": "update_rate_count",
    "INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS": (
        "update_rate_window_seconds"
    ),
    "INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES": "max_queued_frames",
    "INQTRIX_COLLABORATION_MAX_FRAME_BYTES": "max_frame_bytes",
    "INQTRIX_COLLABORATION_MAX_QUEUED_BYTES": "max_queued_bytes",
}


def _node_integer_setting(source: str, name: str) -> tuple[int, int, int]:
    """Return default, minimum and maximum of one Node integer setting."""
    matches = re.findall(
        r"integer\(\s*env,\s*'" + name + r"',\s*([^,]+),\s*([^,]+),"
        r"\s*([^,)]+?),?\s*\)",
        source,
    )
    assert len(matches) == 1, f"{name} must be parsed exactly once by Node"
    default, minimum, maximum = (
        _node_integer_literal(expression) for expression in matches[0]
    )
    return default, minimum, maximum


def _node_integer_literal(expression: str) -> int:
    """Evaluate the integer products Node's settings parser is written with."""
    value = 1
    for factor in expression.split("*"):
        token = factor.strip().replace("_", "")
        value *= 1_048_576 if token == "MEBIBYTE" else int(token)
    return value


def _python_integer_setting(attribute: str) -> tuple[str, int, int, int]:
    """Return alias, default, minimum and maximum of one API setting."""
    field = CollaborationSettings.model_fields[attribute]
    minimum = next(item.ge for item in field.metadata if hasattr(item, "ge"))
    maximum = next(item.le for item in field.metadata if hasattr(item, "le"))
    assert field.alias is not None
    return field.alias, field.default, minimum, maximum


def _node_collaboration_env_names() -> set[str]:
    """Return environment names consumed by the Node settings parser."""
    source = "\n".join(
        (
            _ROOT / "apps" / "collaboration-server" / "src" / filename
        ).read_text(encoding="utf-8")
        for filename in ("config.ts", "verificationFault.ts")
    )
    return set(
        re.findall(
            r"INQTRIX_(?:API_INTERNAL_URL|COLLABORATION_[A-Z0-9_]+)",
            source,
        )
    )


def test_collaboration_image_uses_frozen_nonroot_node_runtime() -> None:
    """The service builds reproducibly and the final image runs without root."""
    dockerfile = (
        _ROOT / "deploy" / "docker" / "Dockerfile.collaboration"
    ).read_text(encoding="utf-8")
    node_pin = (
        "docker.io/library/node:22.23.1-bookworm-slim@"
        "sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3"
    )
    assert dockerfile.count(f"FROM {node_pin}") == 2
    assert "RUN npm ci" in dockerfile
    assert "npm --workspace @inqtrix/collaboration-server run build" in dockerfile

    runtime = dockerfile.split(f"FROM {node_pin} AS runtime", 1)[1]
    assert "RUN chgrp -R 0 /app && chmod -R g=u /app" in runtime
    assert "USER 1001" in runtime
    assert 'CMD ["node", "--enable-source-maps", "dist/main.cjs"]' in runtime
    assert "npm ci" not in runtime


def test_collaboration_production_artifact_is_consistently_commonjs() -> None:
    """Build, package, Docker, and context rules agree on dist/main.cjs."""
    service = _ROOT / "apps" / "collaboration-server"
    package = json.loads((service / "package.json").read_text(encoding="utf-8"))
    build = (service / "build.mjs").read_text(encoding="utf-8")
    dockerfile = (
        _ROOT / "deploy" / "docker" / "Dockerfile.collaboration"
    ).read_text(encoding="utf-8")
    dockerignore = (_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert package["scripts"]["build"] == "node build.mjs"
    assert package["scripts"]["pretest"] == "node build.mjs"
    assert package["scripts"]["start"] == "node dist/main.cjs"
    assert "format: 'cjs'" in build
    assert "outfile: 'dist/main.cjs'" in build
    assert 'CMD ["node", "--enable-source-maps", "dist/main.cjs"]' in dockerfile
    assert "apps/collaboration-server/dist" in dockerignore
    assert "dist/main.js" not in "\n".join((build, dockerfile, json.dumps(package)))


def test_compose_collaboration_profile_is_private_and_credential_scoped() -> None:
    """The optional Node service has no public or persistence/DB surface."""
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    services = compose["services"]
    collaboration = services["collaboration"]

    assert collaboration["profiles"] == ["collaboration"]
    assert collaboration["read_only"] is True
    assert collaboration["user"] == "1001:0"
    assert collaboration["cap_drop"] == ["ALL"]
    assert "ports" not in collaboration
    assert "volumes" not in collaboration
    assert "env_file" not in collaboration

    env = collaboration["environment"]
    assert env["INQTRIX_API_INTERNAL_URL"] == "http://api:5100"
    assert env["INQTRIX_COLLABORATION_SECRET"] == (
        "${INQTRIX_COLLABORATION_SECRET:-}"
    )
    assert env["INQTRIX_COLLABORATION_TENANT_ID"] == (
        "${INQTRIX_COLLABORATION_TENANT_ID:-default}"
    )
    assert env["INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS"] == (
        "${INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS:-60}"
    )
    assert env["INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES"] == (
        "${INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES:-32}"
    )
    assert env["INQTRIX_COLLABORATION_MAX_QUEUED_BYTES"] == (
        "${INQTRIX_COLLABORATION_MAX_QUEUED_BYTES:-8388608}"
    )
    assert env["INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES"] == (
        "${INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES:-4194304}"
    )
    assert env["INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS"] == (
        "${INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS:-1000}"
    )
    assert env["INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS"] == (
        "${INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS:-30000}"
    )
    assert set(env) == _node_collaboration_env_names()
    assert not any(
        marker in name
        for name in env
        for marker in ("DATABASE", "POSTGRES", "PG_", "S3", "VALKEY")
    )

    # API and worker load the SAME shared env file. When the operator turns
    # INQTRIX_COLLABORATION_ENABLED on there, EVERY process constructing
    # Settings needs coherent collaboration URLs. A worker without them
    # crash-loops on the settings validator and never claims queued runs.
    for service_name in ("api", "worker"):
        service_env = services[service_name]["environment"]
        assert service_env["INQTRIX_COLLABORATION_HTTP_URL"] == (
            "http://collaboration:1234"
        ), f"{service_name} must carry the private collaboration HTTP URL"
        assert service_env["INQTRIX_COLLABORATION_WS_URL"] == (
            "ws://collaboration:1234/collaboration"
        ), f"{service_name} must carry the private collaboration WS URL"
    assert "collaboration" not in services["api"]["depends_on"]
    assert "collaboration" not in services["web"]["depends_on"]
    assert services["web"]["environment"]["INQTRIX_EXTERNAL_SCHEME"] == (
        "${INQTRIX_EXTERNAL_SCHEME:-}"
    )


def test_published_transport_limits_share_default_and_bounds_with_node() -> None:
    """The API parses each published limit exactly as Node does.

    A value only one side accepts either stops that process at startup or,
    worse, lets the session announce a limit the service does not apply.
    """
    source = (
        _ROOT / "apps" / "collaboration-server" / "src" / "config.ts"
    ).read_text(encoding="utf-8")

    for name, attribute in _PUBLISHED_TRANSPORT_LIMITS.items():
        alias, default, minimum, maximum = _python_integer_setting(attribute)
        assert alias == name
        assert (default, minimum, maximum) == _node_integer_setting(
            source, name
        ), f"{name} must share default and bounds between API and Node"
    assert _python_integer_setting("max_queued_bytes") == (
        "INQTRIX_COLLABORATION_MAX_QUEUED_BYTES",
        8 * 1_048_576,
        64 * 1024,
        64 * 1_048_576,
    )


def test_compose_hands_the_api_the_transport_limits_node_enforces() -> None:
    """API and Node interpolate each published limit from the same variable.

    The API's env_file alone would miss a value that only reaches Node through
    Compose interpolation, and the session would then publish the default.
    """
    services = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )["services"]

    for name, attribute in _PUBLISHED_TRANSPORT_LIMITS.items():
        node_value = services["collaboration"]["environment"][name]
        assert services["api"]["environment"][name] == node_value, name
        assert node_value == (
            f"${{{name}:-{CollaborationSettings.model_fields[attribute].default}}}"
        ), f"{name} must default to the API's default"


def test_helm_collaboration_defaults_match_the_published_api_defaults() -> None:
    """Without an override, the API's default is the value Node receives.

    The API reads the operator's value from the shared ConfigMap; when there
    is none it uses its own default, which the chart hands to Node as well.
    """
    template = (
        _ROOT
        / "deploy"
        / "helm"
        / "inqtrix"
        / "templates"
        / "deployment-collaboration.yaml"
    ).read_text(encoding="utf-8")

    for name, attribute in _PUBLISHED_TRANSPORT_LIMITS.items():
        defaults = re.findall(
            r'default (\d+) \(index \$settings "' + name + r'"\)',
            template,
        )
        assert defaults == [
            str(CollaborationSettings.model_fields[attribute].default)
        ], name


def test_web_runtime_targets_share_one_locked_gateway_contract() -> None:
    """Python is the default edge; nginx remains an explicit build target."""
    dockerfile = (
        _ROOT / "deploy" / "docker" / "Dockerfile.web"
    ).read_text(encoding="utf-8")
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    uv_lock = (_ROOT / "uv.lock").read_text(encoding="utf-8")
    nginx_override = yaml.safe_load(
        (
            _ROOT / "deploy" / "compose" / "compose.web-nginx.yaml"
        ).read_text(encoding="utf-8")
    )
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    web = compose["services"]["web"]

    assert dockerfile.count(" AS ui-build") == 1
    assert dockerfile.count(" AS gateway-build") == 1
    assert dockerfile.count(" AS web-python") == 1
    assert dockerfile.count(" AS web-nginx") == 1
    assert "python:3.12.13-slim-bookworm@sha256:" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "--only-group web-gateway" in dockerfile
    assert 'CMD ["python", "-m", "inqtrix_web_gateway"]' in dockerfile
    assert "COPY src/inqtrix_web_gateway" in dockerfile
    assert "nginxinc/nginx-unprivileged:" in dockerfile
    assert "deploy/nginx/inqtrix.conf.template" in dockerfile
    assert "web-gateway = [" in pyproject
    assert 'name = "inqtrix"' in uv_lock
    assert nginx_override["services"]["web"]["build"]["target"] == "web-nginx"
    assert "target" not in web["build"]
    assert web["environment"]["INQTRIX_BACKEND_URL"] == "http://api:5100"
    assert web["read_only"] is True
    assert web["user"] == "1001:0"
    assert web["cap_drop"] == ["ALL"]
    assert "INQTRIX_EXTERNAL_SCHEME=http" not in dockerfile
    assert "collaboration:1234" not in dockerfile


def test_first_party_images_pin_supported_official_runtimes() -> None:
    """Web, API, and collaboration use exact official multiarch bases."""
    api = (_ROOT / "deploy" / "docker" / "Dockerfile.api").read_text(
        encoding="utf-8"
    )
    collaboration = (
        _ROOT / "deploy" / "docker" / "Dockerfile.collaboration"
    ).read_text(encoding="utf-8")
    dockerfile = (
        _ROOT / "deploy" / "docker" / "Dockerfile.web"
    ).read_text(encoding="utf-8")

    python_pin = (
        "docker.io/library/python:3.12.13-slim-bookworm@"
        "sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    node_pin = (
        "docker.io/library/node:22.23.1-bookworm-slim@"
        "sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3"
    )
    uv_pin = (
        "ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim@"
        "sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
    )
    assert api.count(python_pin) == 1
    assert api.count(uv_pin) == 1
    assert dockerfile.count(python_pin) == 1
    assert dockerfile.count(node_pin) == 1
    assert dockerfile.count(uv_pin) == 1
    assert collaboration.count(node_pin) == 2


def test_compose_supply_chain_and_database_environment_contract() -> None:
    """Bundled images are immutable and one paired env owns DB credentials."""
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    services = compose["services"]
    image_services = (
        "postgres",
        "pgbouncer",
        "qdrant",
        "valkey",
        "seaweedfs",
        "dex",
    )
    for service_name in image_services:
        image = services[service_name]["image"]
        assert "@sha256:" in image, f"{service_name} must be digest-pinned"
        assert ":latest" not in image
        assert ":stable" not in image

    assert services["pgbouncer"]["image"].startswith(
        "ghcr.io/cloudnative-pg/pgbouncer:1.25.1-trixie@sha256:"
    )
    assert "secrets" not in compose
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == (
        "${INQTRIX_PG_PASSWORD:-}"
    )
    assert "POSTGRES_PASSWORD_FILE" not in services["postgres"]["environment"]
    pooler = services["pgbouncer"]
    assert pooler["profiles"] == ["pgbouncer"]
    assert pooler["environment"]["DB_PASSWORD"] == (
        "${INQTRIX_PG_PASSWORD:-}"
    )
    assert pooler["read_only"] is True
    assert pooler["cap_drop"] == ["ALL"]
    bootstrap = pooler["command"][0]
    assert "umask 077" in bootstrap
    assert "unset DB_PASSWORD" in bootstrap
    assert "/run/secrets" not in bootstrap
    valkey = services["valkey"]
    assert valkey["environment"]["INQTRIX_VALKEY_PASSWORD"] == (
        "${INQTRIX_VALKEY_PASSWORD:-}"
    )
    valkey_bootstrap = valkey["command"][0]
    assert "INQTRIX_VALKEY_PASSWORD:?" in valkey_bootstrap
    assert "change-me-valkey-password" not in valkey_bootstrap
    assert services["qdrant"]["environment"]["QDRANT__SERVICE__API_KEY"] == (
        "${INQTRIX_QDRANT_API_KEY:-}"
    )
    assert services["dex"]["environment"]["DEX_INQTRIX_CLIENT_SECRET"] == (
        "${INQTRIX_OIDC_CLIENT_SECRET:-}"
    )


def test_seaweedfs_env_auth_fails_closed_without_parallel_config_file() -> None:
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    seaweed = compose["services"]["seaweedfs"]

    assert seaweed["profiles"] == ["s3"]
    assert seaweed["environment"] == {
        "AWS_ACCESS_KEY_ID": "${INQTRIX_S3_ACCESS_KEY:-}",
        "AWS_SECRET_ACCESS_KEY": "${INQTRIX_S3_SECRET_KEY:-}",
    }
    bootstrap = seaweed["command"][0]
    assert "AWS_ACCESS_KEY_ID:?" in bootstrap
    assert "AWS_SECRET_ACCESS_KEY:?" in bootstrap
    assert "-s3.config" not in bootstrap
    assert all(
        "s3.json" not in str(volume)
        for volume in seaweed.get("volumes", ())
    )
    assert not (
        _ROOT / "deploy" / "compose" / ("seaweedfs-" + "s3.json")
    ).exists()


def test_dex_uses_one_browser_and_compose_resolvable_issuer() -> None:
    compose = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "compose.stack.yaml").read_text(
            encoding="utf-8"
        )
    )
    dex = compose["services"]["dex"]
    config = yaml.safe_load(
        (_ROOT / "deploy" / "compose" / "dex-config.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert config["issuer"] == "http://dex.localhost:5556/dex"
    assert dex["networks"]["default"]["aliases"] == ["dex.localhost"]
    assert dex["ports"] == ["127.0.0.1:5556:5556"]
    assert "INQTRIX_DEX_PORT" not in (
        _ROOT / "deploy" / "compose" / "compose.stack.yaml"
    ).read_text(encoding="utf-8")


def test_direct_tls_override_reuses_existing_application_services() -> None:
    """Direct TLS configures the existing web, API, and worker services."""
    override = yaml.safe_load(
        (
            _ROOT
            / "deploy"
            / "compose"
            / "compose.web-tls.yaml"
        ).read_text(encoding="utf-8")
    )
    services = override["services"]
    assert set(services) == {"api", "worker", "web"}
    for service in services.values():
        assert "image" not in service
        assert "build" not in service
        assert "ports" not in service

    public_origin = (
        "${INQTRIX_PUBLIC_BASE_URL:?set the exact https public origin}"
    )
    api_environment = services["api"]["environment"]
    assert api_environment["INQTRIX_PUBLIC_BASE_URL"] == public_origin
    assert api_environment["INQTRIX_OIDC_INSECURE_DEV_COOKIES"] == "false"
    assert (
        api_environment["INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP"]
        == "false"
    )
    assert (
        services["worker"]["environment"]["INQTRIX_PUBLIC_BASE_URL"]
        == public_origin
    )

    web = services["web"]
    assert web["environment"]["INQTRIX_PUBLIC_BASE_URL"] == public_origin
    assert web["environment"]["INQTRIX_EXTERNAL_SCHEME"] == "https"
    assert web["environment"]["RESEARCH_DESK_SSL_CERTFILE"] == (
        "/run/inqtrix-tls/tls.crt"
    )
    assert web["environment"]["RESEARCH_DESK_SSL_KEYFILE"] == (
        "/run/inqtrix-tls/tls.key"
    )
    assert web["environment"]["RESEARCH_DESK_SSL_KEYFILE_PASSWORD"] == (
        "${RESEARCH_DESK_SSL_KEYFILE_PASSWORD:-}"
    )
    assert len(web["volumes"]) == 2
    assert all(volume["read_only"] is True for volume in web["volumes"])


def test_instance_probe_uses_the_same_public_fastapi_proxy_path() -> None:
    """Python gateway and Vite route the probe to FastAPI, never Node/SPA."""
    http_proxy = (
        _ROOT / "src" / "inqtrix_web_gateway" / "http_proxy.py"
    ).read_text(encoding="utf-8")
    websocket_proxy = (
        _ROOT / "src" / "inqtrix_web_gateway" / "websocket_proxy.py"
    ).read_text(encoding="utf-8")
    assert '@app.get("/collaboration/instance")' in http_proxy
    assert '@app.websocket("/collaboration")' in websocket_proxy
    assert "collaboration:1234" not in (
        http_proxy + websocket_proxy
    )

    vite = (
        _ROOT / "apps" / "research-desk" / "vite.config.ts"
    ).read_text(encoding="utf-8")
    vite_match = re.search(
        r"'/collaboration': \{(?P<body>.*?)\n\s*\}",
        vite,
        flags=re.DOTALL,
    )
    assert vite_match is not None
    assert "target: apiProxyTarget" in vite_match.group("body")
    assert "ws: true" in vite_match.group("body")
