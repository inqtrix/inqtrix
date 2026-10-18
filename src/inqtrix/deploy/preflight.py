"""Paired dotenv, topology validation, and redaction contracts."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import get_args
from urllib.parse import urlsplit

from inqtrix.settings import AuthSettings

from .compose import DeployConfig, DeployError

_DOTENV_RE = re.compile(
    r"^(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*="
    r"[ \t]*(?P<value>.*)$"
)
_EXPANSION_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)
_URL_SAFE_PASSWORD_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

_SUPPORTED_AUTH_MODES: frozenset[str] = frozenset(
    get_args(AuthSettings.model_fields["mode"].annotation)
)
"""Auth modes the application accepts, read from the settings literal.

Derived rather than restated so a mode removed from settings.py cannot
keep passing the preflight and then kill the api container on startup.
"""

REQUIRED_PROFILE_SECRETS: dict[str, tuple[str, ...]] = {
    "collaboration": ("INQTRIX_COLLABORATION_SECRET",),
    "knowledge": ("INQTRIX_QDRANT_API_KEY",),
    "oidc": ("INQTRIX_OIDC_CLIENT_SECRET",),
    # Mirrors every ":?"-required variable of the langfuse services;
    # without this check the stack fails mid-`up` on interpolation
    # instead of in preflight.
    "observability": (
        "LANGFUSE_CLICKHOUSE_PASSWORD",
        "LANGFUSE_VALKEY_PASSWORD",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY",
        "LANGFUSE_INIT_USER_PASSWORD",
    ),
}
"""Secrets a profile cannot start without.

Module level so the credential template can be pinned against it: the
name a profile is gated on must have a line to uncomment.
"""
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|PEPPER|TOKEN|API_KEY|ACCESS_KEY|"
    r"PRIVATE_KEY|CLIENT_SECRET|KEY_SEED|CREDENTIAL)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_NAME_RE = re.compile(
    r"(?:PASSWORD|PASSWD|SECRET|SECRET_?KEY|TOKEN|PEPPER|API_?KEY|ACCESS_?KEY|"
    r"PRIVATE_?KEY|KEY_?SEED|CREDENTIAL|AUTHORIZATION|DATABASE_URL|VALKEY_URL|"
    # Langfuse/observability secrets whose names do not end in the
    # generic suffixes: crypto salt, encryption key, the OTLP header
    # values (generic and per signal; they embed Basic-auth keys), and
    # the replay-CLI key pair.
    r"SALT|ENCRYPTION_?KEY|OTLP_?(?:(?:TRACES|METRICS|LOGS)_)?HEADERS|"
    r"REPLAY_?AUTH)$",
    re.IGNORECASE,
)
_YAML_KEY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):(?P<rest>.*)$"
)
_ENV_LIST_RE = re.compile(
    r"^(?P<indent>\s*-\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<userinfo>[^\s/@]+)@"
)
_PLACEHOLDER_MARKERS = (
    "CHANGE_ME",
    "CHANGE-ME",
    "CHANGEME",
    "YOUR_",
    "YOUR-",
    "<replace",
)


def require_file(path: Path, label: str) -> None:
    """Require one regular input file."""
    if not path.is_file():
        raise DeployError(f"{label} not found: {path}")


def require_private_file(path: Path, label: str) -> None:
    """Require a secret input with no POSIX group/world access."""
    require_file(path, label)
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise DeployError(
            f"{label} must not be group/world accessible: {path} "
            f"(mode {mode:04o}; expected 0600)"
        )


def _unquote_dotenv(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped[0] in {"'", '"'}:
        quote = stripped[0]
        if len(stripped) < 2 or stripped[-1] != quote:
            raise DeployError("dotenv value has an unmatched quote")
        body = stripped[1:-1]
        if quote == '"':
            body = (
                body.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r"\"", '"')
                .replace(r"\\", "\\")
            )
        return body
    comment = re.search(r"[ \t]+#", stripped)
    if comment is not None:
        stripped = stripped[: comment.start()].rstrip()
    return stripped


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse the deliberately simple Compose dotenv subset."""
    values: dict[str, str] = {}
    first_line_by_name: dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DeployError(f"dotenv file is not UTF-8: {path}") from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_RE.match(line)
        if match is None:
            raise DeployError(
                f"invalid dotenv assignment in {path} at line {line_number}"
            )
        name = match.group("name")
        if name in values:
            raise DeployError(
                f"duplicate dotenv key {name} in {path} at lines "
                f"{first_line_by_name[name]} and {line_number}"
            )
        try:
            values[name] = _unquote_dotenv(match.group("value"))
            first_line_by_name[name] = line_number
        except DeployError as exc:
            raise DeployError(
                f"invalid dotenv value for {name} in {path} "
                f"at line {line_number}"
            ) from exc
    return values


def _expand_values(values: dict[str, str]) -> dict[str, str]:
    expanded = dict(values)
    # Values explicitly selected through the deployment files remain
    # authoritative while their expressions are resolved. Ambient variables
    # may supply names that are not declared in the selected files; conflicts
    # for declared names are rejected separately and never resolved silently.
    lookup = {**os.environ, **expanded}
    for _ in range(10):
        changed = False
        for key, value in tuple(expanded.items()):
            def replace(match: re.Match[str]) -> str:
                name = match.group("name")
                default = match.group("default")
                candidate = lookup.get(name, "")
                if candidate:
                    return candidate
                return default or ""

            new_value = _EXPANSION_RE.sub(replace, value)
            if new_value != value:
                expanded[key] = new_value
                lookup[key] = new_value
                changed = True
        if not changed:
            break
    return expanded


def _declared_environment(
    config: DeployConfig,
    *,
    include_migration: bool,
) -> dict[str, str]:
    values = read_dotenv(config.secrets_env_file)
    values.update(read_dotenv(config.stack_env_file))
    if include_migration and config.migration_env_file is not None:
        values.update(read_dotenv(config.migration_env_file))
    return _expand_values(values)


def validate_ambient_conflicts(config: DeployConfig) -> None:
    """Reject shell precedence that would silently replace selected inputs."""
    declared = _declared_environment(config, include_migration=True)
    conflicts = sorted(
        name
        for name, value in declared.items()
        if name in os.environ and os.environ[name] != value
    )
    if conflicts:
        raise DeployError(
            "process environment conflicts with the selected deployment "
            "files for: "
            + ", ".join(conflicts)
            + "; unset those variables or make them identical"
        )


def validate_pair_contract(config: DeployConfig) -> None:
    """Reject hidden DSNs, secret keys in config, and stale named pointers."""
    configuration = read_dotenv(config.stack_env_file)
    credentials = read_dotenv(config.secrets_env_file)
    forbidden = sorted(
        name for name in configuration if _SECRET_KEY_RE.search(name)
    )
    if forbidden:
        raise DeployError(
            "stack config must not define secret keys: "
            + ", ".join(forbidden)
        )
    if "INQTRIX_DATABASE_URL" not in configuration:
        raise DeployError(
            "INQTRIX_DATABASE_URL must be visibly declared in the stack config"
        )
    if "INQTRIX_DATABASE_URL" in credentials:
        raise DeployError(
            "INQTRIX_DATABASE_URL belongs in stack config, not the secrets file"
        )
    database_template = configuration["INQTRIX_DATABASE_URL"]
    if config.external_db:
        referenced_secrets = sorted(
            {
                match.group("name")
                for match in _EXPANSION_RE.finditer(database_template)
                if _SECRET_KEY_RE.search(match.group("name"))
            }
        )
        if not referenced_secrets:
            raise DeployError(
                "external INQTRIX_DATABASE_URL must visibly interpolate a "
                "secret variable from the selected secrets file"
            )
        missing_references = [
            name for name in referenced_secrets if name not in credentials
        ]
        if missing_references:
            raise DeployError(
                "external INQTRIX_DATABASE_URL references secret variables "
                "missing from the selected secrets file: "
                + ", ".join(missing_references)
            )
    elif "INQTRIX_PG_PASSWORD" not in credentials:
        raise DeployError(
            "bundled mode requires INQTRIX_PG_PASSWORD in the secrets file"
        )
    if "INQTRIX_VALKEY_URL" in credentials:
        raise DeployError(
            "INQTRIX_VALKEY_URL belongs in stack config, not the secrets file"
        )
    if "INQTRIX_VALKEY_URL" in configuration:
        if "${INQTRIX_VALKEY_PASSWORD}" not in configuration[
            "INQTRIX_VALKEY_URL"
        ]:
            raise DeployError(
                "INQTRIX_VALKEY_URL must visibly interpolate "
                "${INQTRIX_VALKEY_PASSWORD}"
            )
        if "INQTRIX_VALKEY_PASSWORD" not in credentials:
            raise DeployError(
                "INQTRIX_VALKEY_PASSWORD must be defined in the secrets file "
                "when INQTRIX_VALKEY_URL is configured"
            )
    expected_config = config.root_relative(
        config.stack_env_file,
        "stack env file",
    )
    expected_secrets = config.root_relative(
        config.secrets_env_file,
        "stack secrets env file",
    )
    if configuration.get("INQTRIX_ENV_FILE") != expected_config:
        raise DeployError(
            "INQTRIX_ENV_FILE must point to the selected root-relative "
            f"config path {expected_config}"
        )
    if configuration.get("INQTRIX_SECRETS_FILE") != expected_secrets:
        raise DeployError(
            "INQTRIX_SECRETS_FILE must point to the selected root-relative "
            f"secrets path {expected_secrets}"
        )
    if config.migration_env_file is not None:
        expected_migration = config.root_relative(
            config.migration_env_file,
            "migration env file",
        )
        if (
            configuration.get("INQTRIX_MIGRATION_ENV_FILE")
            != expected_migration
        ):
            raise DeployError(
                "INQTRIX_MIGRATION_ENV_FILE must point to the selected "
                f"root-relative migration path {expected_migration}"
            )
    validate_ambient_conflicts(config)


def effective_environment(config: DeployConfig) -> dict[str, str]:
    """Resolve Secrets → Config, matching raw Compose and service env_file."""
    validate_pair_contract(config)
    return _declared_environment(config, include_migration=False)


def contains_placeholder(value: str) -> bool:
    upper = value.upper()
    return any(marker.upper() in upper for marker in _PLACEHOLDER_MARKERS)


def database_host(database_url: str) -> str:
    normalized = re.sub(
        r"^(postgres(?:ql)?)\+[A-Za-z0-9_]+",
        r"\1",
        database_url,
        count=1,
    )
    try:
        return (urlsplit(normalized).hostname or "").lower()
    except ValueError as exc:
        raise DeployError("INQTRIX_DATABASE_URL is not a valid PostgreSQL URL") from exc


def is_url_safe_password(value: str) -> bool:
    return _URL_SAFE_PASSWORD_RE.fullmatch(value) is not None


def _targets_bundled_service(
    value: str,
    *,
    scheme: str,
    host: str,
    port: int,
    path: str | None = "",
    allow_userinfo: bool = False,
) -> bool:
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        return False
    path_matches = (
        True
        if path is None
        else parsed.path.rstrip("/") == path.rstrip("/")
    )
    userinfo_matches = allow_userinfo or (
        parsed.username is None and parsed.password is None
    )
    return (
        parsed.scheme == scheme
        and (parsed.hostname or "").lower() == host
        and parsed_port == port
        and path_matches
        and userinfo_matches
        and not parsed.query
        and not parsed.fragment
    )


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_start(config: DeployConfig) -> dict[str, str]:
    """Validate all topology-sensitive inputs before creating containers."""
    raw_config = read_dotenv(config.stack_env_file)
    credentials = read_dotenv(config.secrets_env_file)
    values = effective_environment(config)
    placeholders = sorted(
        name
        for name, value in values.items()
        if value and contains_placeholder(value)
    )
    if placeholders:
        raise DeployError(
            "active placeholder values remain for: " + ", ".join(placeholders)
        )
    auth_mode = values.get("INQTRIX_AUTH_MODE", "").strip()
    if auth_mode and auth_mode not in _SUPPORTED_AUTH_MODES:
        # The api container would otherwise start and die on a pydantic
        # literal_error that names no remedy. Catching it here is the whole
        # point of a preflight.
        raise DeployError(
            f"INQTRIX_AUTH_MODE={auth_mode} is not a supported mode; "
            "choose one of "
            + ", ".join(sorted(_SUPPORTED_AUTH_MODES))
        )
    # Selects the bundled brokers' server/CLI binaries by interpolation, so an
    # unknown value would surface as an unresolvable container command mid-`up`
    # instead of here. Checked unconditionally: it drives the workers broker AND
    # the separate langfuse one.
    broker_engine = values.get("INQTRIX_BROKER_ENGINE", "").strip()
    if broker_engine and broker_engine not in {"valkey", "redis"}:
        raise DeployError(
            'INQTRIX_BROKER_ENGINE must be "valkey" or "redis" (got '
            f"{broker_engine!r}); it selects the bundled broker's "
            "server and CLI binaries"
        )
    database_url = values.get("INQTRIX_DATABASE_URL", "").strip()
    if not database_url:
        raise DeployError(
            "INQTRIX_DATABASE_URL is required in the paired stack env files"
        )
    if contains_placeholder(database_url):
        raise DeployError("INQTRIX_DATABASE_URL still contains a placeholder")

    if config.external_db:
        if "pgbouncer" in config.profiles:
            raise DeployError(
                "the bundled pgbouncer profile cannot front an external database"
            )
        if database_host(database_url) in {"postgres", "pgbouncer"}:
            raise DeployError(
                "--external-db requires a runtime DSN outside the bundled stack"
            )
        if values.get("INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY") != "restricted":
            raise DeployError(
                "--external-db requires "
                "INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY=restricted"
            )
        if config.migration_env_file is None:
            raise DeployError(
                "--external-db requires --migration-env-file with a direct "
                "privileged migration DSN"
            )
        migration_values = read_dotenv(config.migration_env_file)
        migration_url = migration_values.get(
            "INQTRIX_MIGRATION_DATABASE_URL", ""
        ).strip()
        if not migration_url or contains_placeholder(migration_url):
            raise DeployError(
                "INQTRIX_MIGRATION_DATABASE_URL is missing or still contains "
                "a placeholder"
            )
    else:
        if config.migration_env_file is not None:
            raise DeployError(
                "--migration-env-file is valid only with --external-db"
            )
        raw_url = raw_config["INQTRIX_DATABASE_URL"]
        if "${INQTRIX_PG_PASSWORD}" not in raw_url:
            raise DeployError(
                "bundled INQTRIX_DATABASE_URL must visibly interpolate "
                "${INQTRIX_PG_PASSWORD}"
            )
        password = values.get("INQTRIX_PG_PASSWORD", "")
        if not password or contains_placeholder(password):
            raise DeployError(
                "INQTRIX_PG_PASSWORD is missing or still contains a placeholder"
            )
        if not is_url_safe_password(password):
            raise DeployError(
                "INQTRIX_PG_PASSWORD must use only A-Z, a-z, 0-9, dot, "
                "underscore, tilde, or hyphen"
            )
        expected_host = "pgbouncer" if "pgbouncer" in config.profiles else "postgres"
        if database_host(database_url) != expected_host:
            raise DeployError(
                f"the selected profiles require INQTRIX_DATABASE_URL host "
                f"{expected_host!r}"
            )

    if (
        "knowledge" not in config.profiles
        and _targets_bundled_service(
            values.get("INQTRIX_QDRANT_URL", ""),
            scheme="http",
            host="qdrant",
            port=6333,
        )
    ):
        raise DeployError(
            "INQTRIX_QDRANT_URL targets the bundled qdrant service; add "
            "--profile knowledge or configure an external endpoint"
        )
    if (
        "workers" not in config.profiles
        and values.get("INQTRIX_QUEUE_BACKEND", "").strip() == "valkey"
        and _targets_bundled_service(
            values.get("INQTRIX_VALKEY_URL", ""),
            scheme="redis",
            host="valkey",
            port=6379,
            path=None,
            allow_userinfo=True,
        )
    ):
        raise DeployError(
            "INQTRIX_QUEUE_BACKEND=valkey and INQTRIX_VALKEY_URL target the "
            "bundled valkey service; add --profile workers or configure an "
            "external endpoint"
        )
    if (
        "s3" not in config.profiles
        and values.get("INQTRIX_OBJECT_STORE_BACKEND", "").strip() == "s3"
        and _targets_bundled_service(
            values.get("INQTRIX_S3_ENDPOINT_URL", ""),
            scheme="http",
            host="seaweedfs",
            port=8333,
        )
    ):
        raise DeployError(
            "INQTRIX_OBJECT_STORE_BACKEND=s3 and INQTRIX_S3_ENDPOINT_URL "
            "target the bundled seaweedfs service; add --profile s3 or "
            "configure an external endpoint"
        )
    if (
        "collaboration" not in config.profiles
        and _is_enabled(values.get("INQTRIX_COLLABORATION_ENABLED", ""))
    ):
        raise DeployError(
            "INQTRIX_COLLABORATION_ENABLED=true selects the Compose-injected "
            "internal collaboration service; add --profile collaboration"
        )
    if (
        "oidc" not in config.profiles
        and values.get("INQTRIX_OIDC_ISSUER", "").strip()
        == "http://dex.localhost:5556/dex"
    ):
        raise DeployError(
            "INQTRIX_OIDC_ISSUER targets the bundled Dex service; add "
            "--profile oidc or configure an external issuer"
        )
    if "observability" not in config.profiles and values.get(
        "INQTRIX_TRACING", ""
    ).strip() == "otlp":
        # The exporter honours BOTH standard variables (the
        # traces-specific one wins and is used verbatim) — check both,
        # path-agnostic on host+port, so neither spelling slips through
        # to a mid-`up` connection failure.
        for endpoint_name in (
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        ):
            if _targets_bundled_service(
                values.get(endpoint_name, ""),
                scheme="http",
                host="langfuse",
                port=3000,
                path=None,
            ):
                raise DeployError(
                    f"{endpoint_name} targets the bundled langfuse "
                    "service; add --profile observability or configure "
                    "an external OTLP endpoint"
                )

    if "s3" in config.profiles:
        for name in ("INQTRIX_S3_ACCESS_KEY", "INQTRIX_S3_SECRET_KEY"):
            if name not in credentials:
                raise DeployError(
                    f"{name} must be defined in the selected secrets file"
                )
            value = values.get(name, "")
            if not value or contains_placeholder(value):
                raise DeployError(f"{name} is required for the s3 profile")
        s3_contract = {
            "INQTRIX_OBJECT_STORE_BACKEND": "s3",
            "INQTRIX_S3_AUTH_MODE": "static",
            "INQTRIX_S3_ADDRESSING_STYLE": "path",
            "INQTRIX_S3_BUCKET_PROVISIONING": "create_if_missing",
        }
        for name, expected in s3_contract.items():
            if values.get(name, "").strip() != expected:
                raise DeployError(
                    f"the s3 profile requires {name}={expected}"
                )
        if not values.get("INQTRIX_S3_BUCKET", "").strip():
            raise DeployError(
                "the s3 profile requires a non-empty INQTRIX_S3_BUCKET"
            )
        if not _targets_bundled_service(
            values.get("INQTRIX_S3_ENDPOINT_URL", ""),
            scheme="http",
            host="seaweedfs",
            port=8333,
        ):
            raise DeployError(
                "the s3 profile requires INQTRIX_S3_ENDPOINT_URL to target "
                "http://seaweedfs:8333"
            )
    if "workers" in config.profiles:
        if values.get("INQTRIX_QUEUE_BACKEND") != "valkey":
            raise DeployError(
                "the workers profile requires INQTRIX_QUEUE_BACKEND=valkey"
            )
        for name in ("INQTRIX_VALKEY_PASSWORD", "INQTRIX_VALKEY_URL"):
            if not values.get(name, "").strip():
                raise DeployError(f"{name} is required for the workers profile")
        valkey_url = values["INQTRIX_VALKEY_URL"]
        try:
            parsed_valkey = urlsplit(valkey_url)
        except ValueError as exc:
            raise DeployError("INQTRIX_VALKEY_URL is not a valid Redis URL") from exc
        if (
            parsed_valkey.scheme != "redis"
            or (parsed_valkey.hostname or "").lower() != "valkey"
            or parsed_valkey.port not in {None, 6379}
        ):
            raise DeployError(
                "the bundled workers profile requires INQTRIX_VALKEY_URL "
                "to target redis://valkey:6379"
            )
    for profile, names in REQUIRED_PROFILE_SECRETS.items():
        if profile not in config.profiles:
            continue
        missing = [
            name
            for name in names
            if name not in credentials or not values.get(name, "").strip()
        ]
        if missing:
            raise DeployError(
                f"the {profile} profile requires: " + ", ".join(missing)
            )

    if "observability" in config.profiles:
        # Langfuse stores raw trace events in S3: without the bundled
        # seaweedfs (--profile s3) an external object store must be
        # configured, or the stack fails mid-`up` on the bucket init.
        s3_endpoint = values.get("INQTRIX_S3_ENDPOINT_URL", "").strip()
        s3_is_external = bool(s3_endpoint) and not _targets_bundled_service(
            s3_endpoint, scheme="http", host="seaweedfs", port=8333
        )
        if "s3" not in config.profiles and not s3_is_external:
            raise DeployError(
                "the observability profile needs an object store for "
                "langfuse raw events: add --profile s3 or set "
                "INQTRIX_S3_* to an external S3 endpoint"
            )
        # With an external database the bundled postgres does not run,
        # so the langfuse database cannot be auto-provisioned there.
        if config.external_db and not values.get(
            "LANGFUSE_DATABASE_URL", ""
        ).strip():
            raise DeployError(
                "the observability profile with --external-db requires "
                "LANGFUSE_DATABASE_URL (create the langfuse database on "
                "the external server and point this URL at it)"
            )

    if "knowledge" in config.profiles:
        if not _is_enabled(values.get("INQTRIX_KNOWLEDGE_ENABLED", "")):
            raise DeployError(
                "the knowledge profile requires "
                "INQTRIX_KNOWLEDGE_ENABLED=true"
            )
        if values.get("INQTRIX_VECTOR_BACKEND", "").strip() != "qdrant":
            raise DeployError(
                "the knowledge profile requires "
                "INQTRIX_VECTOR_BACKEND=qdrant"
            )
        if not _targets_bundled_service(
            values.get("INQTRIX_QDRANT_URL", ""),
            scheme="http",
            host="qdrant",
            port=6333,
        ):
            raise DeployError(
                "the knowledge profile requires INQTRIX_QDRANT_URL to "
                "target http://qdrant:6333"
            )

    if "collaboration" in config.profiles and not _is_enabled(
        values.get("INQTRIX_COLLABORATION_ENABLED", "")
    ):
        raise DeployError(
            "the collaboration profile requires "
            "INQTRIX_COLLABORATION_ENABLED=true"
        )

    if "oidc" in config.profiles:
        if values.get("INQTRIX_AUTH_MODE", "").strip() != "oidc":
            raise DeployError(
                "the oidc profile requires INQTRIX_AUTH_MODE=oidc"
            )
        if (
            values.get("INQTRIX_OIDC_ISSUER", "").strip()
            != "http://dex.localhost:5556/dex"
        ):
            raise DeployError(
                "the oidc profile requires INQTRIX_OIDC_ISSUER="
                "http://dex.localhost:5556/dex"
            )
        if values.get("INQTRIX_OIDC_CLIENT_ID", "").strip() != "inqtrix-local":
            raise DeployError(
                "the oidc profile requires "
                "INQTRIX_OIDC_CLIENT_ID=inqtrix-local"
            )

    return values


def sensitive_values(config: DeployConfig) -> tuple[str, ...]:
    values: dict[str, str] = {}
    for path in (
        config.secrets_env_file,
        config.stack_env_file,
        config.migration_env_file,
    ):
        if path is not None and path.is_file():
            values.update(read_dotenv(path))
    expanded = _expand_values(values)
    sensitive = {
        value
        for key, value in expanded.items()
        if value and len(value) >= 4 and _SENSITIVE_NAME_RE.search(key)
    }
    return tuple(sorted(sensitive, key=len, reverse=True))


def redact_text(text: str, secret_values: Iterable[str]) -> str:
    """Redact env values, sensitive YAML keys, and URL userinfo."""
    redacted = text
    for value in secret_values:
        redacted = redacted.replace(value, "***REDACTED***")
    redacted = _CREDENTIAL_URL_RE.sub(
        lambda match: f"{match.group('scheme')}***:***@",
        redacted,
    )
    lines: list[str] = []
    for line in redacted.splitlines():
        env_match = _ENV_LIST_RE.match(line)
        if env_match and _SENSITIVE_NAME_RE.search(env_match.group("key")):
            line = (
                f"{env_match.group('indent')}{env_match.group('key')}="
                "***REDACTED***"
            )
        else:
            key_match = _YAML_KEY_RE.match(line)
            if key_match and _SENSITIVE_NAME_RE.search(key_match.group("key")):
                line = (
                    f"{key_match.group('indent')}{key_match.group('key')}: "
                    '"***REDACTED***"'
                )
        lines.append(line)
    suffix = "\n" if redacted.endswith("\n") else ""
    return "\n".join(lines) + suffix
