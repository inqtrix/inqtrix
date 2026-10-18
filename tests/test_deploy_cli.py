"""Operator CLI tests using only synthetic temporary Compose inputs."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from inqtrix.deploy.cli import main
from inqtrix.deploy.compose import ComposeRunner, DeployError
from inqtrix.deploy.preflight import read_dotenv


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_duplicate_dotenv_error_names_both_lines(tmp_path: Path) -> None:
    path = tmp_path / "stack.env"
    path.write_text(
        "FIRST=value\nDUPLICATE=one\nMIDDLE=value\nDUPLICATE=two\n",
        encoding="utf-8",
    )

    with pytest.raises(
        DeployError,
        match=r"duplicate dotenv key DUPLICATE .* at lines 2 and 4",
    ):
        read_dotenv(path)


def _project(
    tmp_path: Path,
    *,
    external: bool = False,
    owner_mode: bool = False,
) -> tuple[Path, Path, Path, Path | None]:
    compose_dir = tmp_path / "deploy" / "compose"
    compose_dir.mkdir(parents=True)
    for name in (
        "compose.stack.yaml",
        "compose.dev-ports.yaml",
        "compose.external-db.yaml",
        "compose.web-tls.yaml",
        "compose.web-nginx.yaml",
    ):
        (compose_dir / name).write_text(
            "name: inqtrix\nservices: {}\n",
            encoding="utf-8",
        )

    stack_env = tmp_path / "deploy" / ".env.stack"
    policy = "restricted" if external else "bundled_legacy"
    mode = "owner" if owner_mode else ("bypass" if external else "auto")
    stack_env.write_text(
        "\n".join(
            (
                "INQTRIX_ENV_FILE=deploy/.env.stack",
                "INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets",
                "INQTRIX_PG_USER=inqtrix",
                "INQTRIX_PG_DB=inqtrix",
                *(
                    ("INQTRIX_MIGRATION_ENV_FILE=deploy/.env.migrate",)
                    if external
                    else ()
                ),
                f"INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY={policy}",
                f"INQTRIX_MIGRATION_RLS_MODE={mode}",
                (
                    "INQTRIX_DATABASE_URL="
                    "postgresql+asyncpg://runtime_user:"
                    "${INQTRIX_DATABASE_PASSWORD}"
                    "@database.example.test:5432/inqtrix"
                    if external
                    else "INQTRIX_DATABASE_URL="
                    "postgresql+asyncpg://${INQTRIX_PG_USER}:"
                    "${INQTRIX_PG_PASSWORD}@postgres:5432/"
                    "${INQTRIX_PG_DB}"
                ),
                "",
            )
        ),
        encoding="utf-8",
    )

    secrets_env = tmp_path / "deploy" / ".env.stack.secrets"
    _write_private(
        secrets_env,
        "\n".join(
            (
                *(
                    ("INQTRIX_PG_PASSWORD=SyntheticCurrentPassword2026",)
                    if not external
                    else ()
                ),
                "INQTRIX_DATABASE_PASSWORD=SyntheticRuntime2026",
                "INQTRIX_SESSION_SECRET=SyntheticSessionSecret2026",
                "",
            )
        ),
    )

    migration_env: Path | None = None
    if external:
        migration_env = tmp_path / "deploy" / ".env.migrate"
        _write_private(
            migration_env,
            "INQTRIX_MIGRATION_DATABASE_URL="
            "postgresql+asyncpg://migration_user:SyntheticMigration2026"
            "@database.example.test:5432/inqtrix\n",
        )
    return tmp_path, stack_env, secrets_env, migration_env


def _install_fake_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    trace = tmp_path / "compose.trace"
    executable = tmp_path / "docker"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

arguments = sys.argv[1:]
joined = " ".join(arguments)
record = {{
    "arguments": arguments,
    "stack_env": os.environ.get("INQTRIX_ENV_FILE"),
    "secrets_env": os.environ.get("INQTRIX_SECRETS_FILE"),
    "migration_env": os.environ.get("INQTRIX_MIGRATION_ENV_FILE"),
    "web_bind_address": os.environ.get("INQTRIX_WEB_BIND_ADDRESS"),
}}
if " exec " in f" {{joined}} ":
    record["stdin_bytes"] = len(sys.stdin.read().encode("utf-8"))
with open(os.environ["FAKE_COMPOSE_TRACE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
if "ps --services --filter status=running" in joined:
    print(os.environ.get("FAKE_RUNNING_SERVICES", ""))
if arguments and arguments[-1] == "config":
    print(os.environ.get("FAKE_CONFIG_OUTPUT", ""))
failure = os.environ.get("FAKE_FAIL_CONTAINS", "")
if failure and failure in joined:
    print("synthetic compose failure", file=sys.stderr)
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    monkeypatch.setenv("FAKE_COMPOSE_TRACE", str(trace))
    for name in (
        "INQTRIX_DATABASE_URL",
        "INQTRIX_PG_PASSWORD",
        "INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY",
        "INQTRIX_MIGRATION_RLS_MODE",
        "INQTRIX_QUEUE_BACKEND",
        "INQTRIX_VALKEY_PASSWORD",
        "INQTRIX_VALKEY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return trace


def _arguments(
    root: Path,
    stack_env: Path,
    secrets_env: Path,
) -> list[str]:
    return [
        "--project-directory",
        str(root),
        "--engine",
        "docker",
        "--env-file",
        str(stack_env),
        "--secrets-file",
        str(secrets_env),
    ]


def _trace(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


_PROFILE_CONFIG: dict[str, dict[str, str]] = {
    "workers": {
        "INQTRIX_QUEUE_BACKEND": "valkey",
        "INQTRIX_VALKEY_URL": (
            "redis://:${INQTRIX_VALKEY_PASSWORD}@valkey:6379/0"
        ),
    },
    "s3": {
        "INQTRIX_OBJECT_STORE_BACKEND": "s3",
        "INQTRIX_S3_AUTH_MODE": "static",
        "INQTRIX_S3_ENDPOINT_URL": "http://seaweedfs:8333",
        "INQTRIX_S3_BUCKET": "synthetic-audit",
        "INQTRIX_S3_ADDRESSING_STYLE": "path",
        "INQTRIX_S3_BUCKET_PROVISIONING": "create_if_missing",
    },
    "knowledge": {
        "INQTRIX_KNOWLEDGE_ENABLED": "true",
        "INQTRIX_VECTOR_BACKEND": "qdrant",
        "INQTRIX_QDRANT_URL": "http://qdrant:6333",
    },
    "collaboration": {
        "INQTRIX_COLLABORATION_ENABLED": "true",
    },
    "oidc": {
        "INQTRIX_AUTH_MODE": "oidc",
        "INQTRIX_OIDC_ISSUER": "http://dex.localhost:5556/dex",
        "INQTRIX_OIDC_CLIENT_ID": "inqtrix-local",
    },
    "observability": {
        "INQTRIX_TRACING": "otlp",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://langfuse:3000/api/public/otel",
        # Standalone coherence: langfuse needs an object store; without
        # --profile s3 the topology must name an external endpoint.
        "INQTRIX_S3_ENDPOINT_URL": "https://s3.example.test",
    },
}
_PROFILE_SECRETS: dict[str, dict[str, str]] = {
    "workers": {
        "INQTRIX_VALKEY_PASSWORD": "SyntheticValkeyPassword2026",
    },
    "s3": {
        "INQTRIX_S3_ACCESS_KEY": "SyntheticS3Access2026",
        "INQTRIX_S3_SECRET_KEY": "SyntheticS3Secret2026",
    },
    "knowledge": {
        "INQTRIX_QDRANT_API_KEY": "SyntheticQdrantSecret2026",
    },
    "collaboration": {
        "INQTRIX_COLLABORATION_SECRET": (
            "SyntheticCollaborationSecretLongEnough2026"
        ),
    },
    "oidc": {
        "INQTRIX_OIDC_CLIENT_SECRET": "SyntheticOidcSecret2026",
    },
    "observability": {
        "LANGFUSE_CLICKHOUSE_PASSWORD": "SyntheticClickhouse2026",
        "LANGFUSE_VALKEY_PASSWORD": "SyntheticLfValkey2026",
        "LANGFUSE_SALT": "SyntheticLangfuseSalt2026",
        "LANGFUSE_ENCRYPTION_KEY": "0" * 64,
        "LANGFUSE_NEXTAUTH_SECRET": "SyntheticNextauth2026",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-synthetic-2026",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-synthetic-2026",
        "LANGFUSE_INIT_USER_PASSWORD": "SyntheticLfAdmin2026",
    },
}


def _configure_profile(
    stack_env: Path,
    secrets_env: Path,
    profile: str,
    *,
    overrides: dict[str, str] | None = None,
) -> None:
    configuration = dict(_PROFILE_CONFIG[profile])
    configuration.update(overrides or {})
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in configuration.items()),
        encoding="utf-8",
    )
    secrets = _PROFILE_SECRETS[profile]
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in secrets.items()),
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)


def test_status_uses_one_compose_prefix_and_the_ordered_env_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    assert main([*_arguments(root, stack_env, secrets_env), "status"]) == 0

    records = _trace(trace)
    assert len(records) == 1
    arguments = records[0]["arguments"]
    assert isinstance(arguments, list)
    project_directory = arguments.index("--project-directory")
    assert arguments[project_directory + 1] == str(
        root / "deploy" / "compose"
    )
    first_env = arguments.index("--env-file")
    second_env = arguments.index("--env-file", first_env + 1)
    assert arguments[first_env + 1] == str(secrets_env)
    assert arguments[second_env + 1] == str(stack_env)
    assert arguments[-1] == "ps"
    assert records[0]["stack_env"] == "deploy/.env.stack"
    assert records[0]["secrets_env"] == "deploy/.env.stack.secrets"
    announced = capsys.readouterr().err
    assert announced.startswith("+ docker compose ")
    assert str(secrets_env) in announced
    assert str(stack_env) in announced
    assert "SyntheticCurrentPassword2026" not in announced
    assert announced.rstrip().endswith(" ps")


def test_up_is_foreground_by_default_and_detach_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    base = _arguments(root, stack_env, secrets_env)

    assert main([*base, "up", "--build"]) == 0
    assert main([*base, "up", "--build", "--detach"]) == 0

    records = _trace(trace)
    assert records[0]["arguments"][-2:] == ["up", "--build"]
    assert records[1]["arguments"][-3:] == ["up", "-d", "--build"]


def test_dev_ports_uses_the_same_explicit_development_project_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--dev-ports",
            "status",
        ]
    )

    assert result == 0
    arguments = _trace(trace)[0]["arguments"]
    assert isinstance(arguments, list)
    project_name = arguments.index("--project-name")
    assert arguments[project_name + 1] == "inqtrix-dev"
    assert str(
        root / "deploy" / "compose" / "compose.dev-ports.yaml"
    ) in arguments


def test_bundled_pgbouncer_needs_no_migration_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8").replace(
            "@postgres:5432/",
            "@pgbouncer:6432/",
        ),
        encoding="utf-8",
    )
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--profile",
            "pgbouncer",
            "up",
            "--detach",
        ]
    )

    assert result == 0
    record = _trace(trace)[0]
    assert record["migration_env"] is None
    arguments = record["arguments"]
    assert isinstance(arguments, list)
    profile = arguments.index("--profile")
    assert arguments[profile + 1] == "pgbouncer"
    assert arguments[-2:] == ["up", "-d"]


def test_nginx_is_explicit_and_cannot_be_combined_with_python_tls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    base = _arguments(root, stack_env, secrets_env)
    nginx_override = (
        root / "deploy" / "compose" / "compose.web-nginx.yaml"
    )

    assert main([*base, "--web-nginx", "status"]) == 0
    arguments = _trace(trace)[0]["arguments"]
    assert isinstance(arguments, list)
    target_index = arguments.index(str(nginx_override))
    assert arguments[target_index - 1] == "-f"

    with pytest.raises(SystemExit) as exc_info:
        main([*base, "--web-nginx", "--web-tls", "status"])
    assert exc_info.value.code == 2
    assert len(_trace(trace)) == 1


def test_named_pair_drives_raw_env_files_and_service_path_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    named_stack = root / "deploy" / ".env.stack.synthetic"
    named_secrets = root / "deploy" / ".env.stack.secrets.synthetic"
    stack_text = stack_env.read_text(encoding="utf-8").replace(
        "INQTRIX_ENV_FILE=deploy/.env.stack\n"
        "INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets",
        "INQTRIX_ENV_FILE=deploy/.env.stack.synthetic\n"
        "INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets.synthetic",
    )
    named_stack.write_text(stack_text, encoding="utf-8")
    stack_env.unlink()
    secrets_env.rename(named_secrets)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    assert (
        main(
            [
                *_arguments(root, named_stack, named_secrets),
                "status",
            ]
        )
        == 0
    )

    record = _trace(trace)[0]
    arguments = record["arguments"]
    assert isinstance(arguments, list)
    first_env = arguments.index("--env-file")
    second_env = arguments.index("--env-file", first_env + 1)
    assert arguments[first_env + 1] == str(named_secrets)
    assert arguments[second_env + 1] == str(named_stack)
    assert record["stack_env"] == "deploy/.env.stack.synthetic"
    assert record["secrets_env"] == (
        "deploy/.env.stack.secrets.synthetic"
    )


def test_declared_ambient_override_is_rejected_but_undeclared_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    ambient_secret = "AmbientMustNotReplaceTheSelectedFile"
    monkeypatch.setenv("INQTRIX_PG_PASSWORD", ambient_secret)
    monkeypatch.setenv("INQTRIX_WEB_BIND_ADDRESS", "0.0.0.0")
    base = _arguments(root, stack_env, secrets_env)

    assert main([*base, "status"]) == 2
    error = capsys.readouterr().err
    assert "INQTRIX_PG_PASSWORD" in error
    assert ambient_secret not in error
    assert "unset those variables" in error
    assert _trace(trace) == []

    monkeypatch.setenv(
        "INQTRIX_PG_PASSWORD",
        "SyntheticCurrentPassword2026",
    )
    assert main([*base, "status"]) == 0
    records = _trace(trace)
    assert len(records) == 1
    assert records[0]["web_bind_address"] == "0.0.0.0"


def test_named_external_migration_pointer_is_exact_and_root_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
    )
    assert migration_env is not None
    named_stack = root / "deploy" / ".env.stack.azure"
    named_secrets = root / "deploy" / ".env.stack.secrets.azure"
    named_migration = root / "deploy" / ".env.migrate.secrets.azure"
    stack_text = (
        stack_env.read_text(encoding="utf-8")
        .replace(
            "INQTRIX_ENV_FILE=deploy/.env.stack",
            "INQTRIX_ENV_FILE=deploy/.env.stack.azure",
        )
        .replace(
            "INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets",
            "INQTRIX_SECRETS_FILE=deploy/.env.stack.secrets.azure",
        )
        .replace(
            "INQTRIX_MIGRATION_ENV_FILE=deploy/.env.migrate",
            (
                "INQTRIX_MIGRATION_ENV_FILE="
                "deploy/.env.migrate.secrets.azure"
            ),
        )
    )
    named_stack.write_text(stack_text, encoding="utf-8")
    stack_env.unlink()
    secrets_env.rename(named_secrets)
    migration_env.rename(named_migration)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, named_stack, named_secrets),
            "--external-db",
            "--migration-env-file",
            str(named_migration),
            "status",
        ]
    )

    assert result == 0
    record = _trace(trace)[0]
    assert record["migration_env"] == (
        "deploy/.env.migrate.secrets.azure"
    )

    named_stack.write_text(
        stack_text.replace(
            "deploy/.env.migrate.secrets.azure",
            "deploy/.env.migrate.secrets.wrong",
        ),
        encoding="utf-8",
    )
    trace.unlink()
    assert (
        main(
            [
                *_arguments(root, named_stack, named_secrets),
                "--external-db",
                "--migration-env-file",
                str(named_migration),
                "status",
            ]
        )
        == 2
    )
    assert _trace(trace) == []


def test_external_database_requires_only_its_referenced_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
    )
    assert migration_env is not None
    assert "INQTRIX_PG_PASSWORD" not in secrets_env.read_text(encoding="utf-8")
    trace = _install_fake_docker(tmp_path, monkeypatch)
    base = [
        *_arguments(root, stack_env, secrets_env),
        "--external-db",
        "--migration-env-file",
        str(migration_env),
    ]

    assert main([*base, "status"]) == 0
    trace.unlink()
    secrets_env.write_text(
        "INQTRIX_SESSION_SECRET=SyntheticSessionSecret2026\n",
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)

    assert main([*base, "status"]) == 2
    assert _trace(trace) == []


def test_start_rejects_all_active_placeholders_without_printing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8").replace(
            "SyntheticSessionSecret2026",
            "CHANGE_ME_SESSION_SECRET",
        ),
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    assert main([*_arguments(root, stack_env, secrets_env), "up"]) == 2
    error = capsys.readouterr().err
    assert "INQTRIX_SESSION_SECRET" in error
    assert "CHANGE_ME_SESSION_SECRET" not in error
    assert _trace(trace) == []


def test_workers_require_visible_valkey_topology_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8")
        + "INQTRIX_QUEUE_BACKEND=valkey\n"
        + "INQTRIX_VALKEY_URL="
        + "redis://:${INQTRIX_VALKEY_PASSWORD}@valkey:6379/0\n",
        encoding="utf-8",
    )
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8")
        + "INQTRIX_VALKEY_PASSWORD=SyntheticValkeyPassword2026\n",
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    base = [
        *_arguments(root, stack_env, secrets_env),
        "--profile",
        "workers",
    ]

    assert main([*base, "up", "--detach"]) == 0
    trace.unlink()
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8").replace(
            "SyntheticValkeyPassword2026",
            "change-me-valkey-password",
        ),
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)

    assert main([*base, "up", "--detach"]) == 2
    assert _trace(trace) == []


@pytest.mark.parametrize(
    ("engine", "accepted"),
    (("valkey", True), ("redis", True), ("keydb", False), ("Redis", False)),
)
def test_bundled_broker_engine_is_restricted_to_known_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engine: str,
    accepted: bool,
) -> None:
    """The engine selects a binary name, so an unknown value must fail here.

    Without the check it would reach the container as an unresolvable
    ``<engine>-server`` command and surface as a crash loop mid-``up``.
    """
    root, stack_env, secrets_env, _ = _project(tmp_path)
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8")
        + "INQTRIX_QUEUE_BACKEND=valkey\n"
        + "INQTRIX_VALKEY_URL="
        + "redis://:${INQTRIX_VALKEY_PASSWORD}@valkey:6379/0\n"
        + f"INQTRIX_BROKER_ENGINE={engine}\n",
        encoding="utf-8",
    )
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8")
        + "INQTRIX_VALKEY_PASSWORD=SyntheticValkeyPassword2026\n",
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    command = [
        *_arguments(root, stack_env, secrets_env),
        "--profile",
        "workers",
        "up",
        "--detach",
    ]

    if accepted:
        assert main(command) == 0
        return

    assert main(command) == 2
    assert "INQTRIX_BROKER_ENGINE" in capsys.readouterr().err
    assert _trace(trace) == []


def test_preflight_rejects_an_auth_mode_the_application_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A removed mode must fail in preflight, not in the api container.

    Without this the operator gets a green `up` and a crash-looping
    container whose pydantic literal_error names no remedy. The mode set
    is derived from AuthSettings, so removing a mode there closes this
    path in the same edit.
    """
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8") + "INQTRIX_AUTH_MODE=ldap\n",
        encoding="utf-8",
    )

    assert main([*_arguments(root, stack_env, secrets_env), "up"]) == 2
    error = capsys.readouterr().err
    assert "INQTRIX_AUTH_MODE=ldap" in error
    assert "not a supported mode" in error
    assert _trace(trace) == []


@pytest.mark.parametrize(
    ("profile", "required_name"),
    (
        ("s3", "INQTRIX_S3_ACCESS_KEY"),
        ("collaboration", "INQTRIX_COLLABORATION_SECRET"),
        ("knowledge", "INQTRIX_QDRANT_API_KEY"),
        ("oidc", "INQTRIX_OIDC_CLIENT_SECRET"),
        ("observability", "LANGFUSE_SALT"),
    ),
)
def test_optional_profiles_do_not_use_implicit_default_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    required_name: str,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--profile",
            profile,
            "up",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert required_name in error
    assert _trace(trace) == []


@pytest.mark.parametrize("profile", tuple(_PROFILE_CONFIG))
def test_optional_profile_accepts_only_its_coherent_bundled_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    _configure_profile(stack_env, secrets_env, profile)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--profile",
            profile,
            "up",
            "--detach",
        ]
    )

    assert result == 0
    assert len(_trace(trace)) == 1


@pytest.mark.parametrize("profile", tuple(_PROFILE_CONFIG))
def test_bundled_service_target_requires_its_matching_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    _configure_profile(stack_env, secrets_env, profile)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "up",
        ]
    )

    assert result == 2
    assert f"--profile {profile}" in capsys.readouterr().err
    assert _trace(trace) == []


@pytest.mark.parametrize(
    ("configuration", "secrets"),
    (
        (
            {
                "INQTRIX_KNOWLEDGE_ENABLED": "true",
                "INQTRIX_VECTOR_BACKEND": "qdrant",
                "INQTRIX_QDRANT_URL": "https://qdrant.example.test:6333",
            },
            {
                "INQTRIX_QDRANT_API_KEY": "SyntheticManagedQdrant2026",
            },
        ),
        (
            {
                "INQTRIX_QUEUE_BACKEND": "valkey",
                "INQTRIX_VALKEY_URL": (
                    "rediss://:${INQTRIX_VALKEY_PASSWORD}"
                    "@cache.example.test:6380/0"
                ),
            },
            {
                "INQTRIX_VALKEY_PASSWORD": "SyntheticManagedValkey2026",
            },
        ),
        (
            {
                "INQTRIX_OBJECT_STORE_BACKEND": "s3",
                "INQTRIX_S3_AUTH_MODE": "static",
                "INQTRIX_S3_ENDPOINT_URL": "https://s3.example.test",
                "INQTRIX_S3_BUCKET": "managed-bucket",
                "INQTRIX_S3_ADDRESSING_STYLE": "path",
                "INQTRIX_S3_BUCKET_PROVISIONING": "existing",
            },
            {
                "INQTRIX_S3_ACCESS_KEY": "SyntheticManagedS3Access2026",
                "INQTRIX_S3_SECRET_KEY": "SyntheticManagedS3Secret2026",
            },
        ),
        (
            {
                "INQTRIX_AUTH_MODE": "oidc",
                "INQTRIX_OIDC_ISSUER": "https://idp.example.test",
                "INQTRIX_OIDC_CLIENT_ID": "managed-client",
            },
            {
                "INQTRIX_OIDC_CLIENT_SECRET": "SyntheticManagedOidc2026",
            },
        ),
    ),
)
def test_managed_external_service_targets_do_not_require_bundled_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configuration: dict[str, str],
    secrets: dict[str, str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in configuration.items()),
        encoding="utf-8",
    )
    secrets_env.write_text(
        secrets_env.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in secrets.items()),
        encoding="utf-8",
    )
    secrets_env.chmod(0o600)
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "up",
        ]
    )

    assert result == 0
    assert len(_trace(trace)) == 1


@pytest.mark.parametrize(
    ("profile", "overrides", "expected_name"),
    (
        (
            "s3",
            {"INQTRIX_OBJECT_STORE_BACKEND": "local"},
            "INQTRIX_OBJECT_STORE_BACKEND",
        ),
        (
            "s3",
            {"INQTRIX_S3_AUTH_MODE": "default"},
            "INQTRIX_S3_AUTH_MODE",
        ),
        (
            "s3",
            {"INQTRIX_S3_ENDPOINT_URL": "https://s3.example.test"},
            "INQTRIX_S3_ENDPOINT_URL",
        ),
        (
            "s3",
            {"INQTRIX_S3_BUCKET": ""},
            "INQTRIX_S3_BUCKET",
        ),
        (
            "s3",
            {"INQTRIX_S3_ADDRESSING_STYLE": "auto"},
            "INQTRIX_S3_ADDRESSING_STYLE",
        ),
        (
            "s3",
            {"INQTRIX_S3_BUCKET_PROVISIONING": "existing"},
            "INQTRIX_S3_BUCKET_PROVISIONING",
        ),
        (
            "knowledge",
            {"INQTRIX_KNOWLEDGE_ENABLED": "false"},
            "INQTRIX_KNOWLEDGE_ENABLED",
        ),
        (
            "knowledge",
            {"INQTRIX_VECTOR_BACKEND": "memory"},
            "INQTRIX_VECTOR_BACKEND",
        ),
        (
            "knowledge",
            {"INQTRIX_QDRANT_URL": "http://127.0.0.1:6333"},
            "INQTRIX_QDRANT_URL",
        ),
        (
            "collaboration",
            {"INQTRIX_COLLABORATION_ENABLED": "false"},
            "INQTRIX_COLLABORATION_ENABLED",
        ),
        (
            "oidc",
            {"INQTRIX_AUTH_MODE": "local"},
            "INQTRIX_AUTH_MODE",
        ),
        (
            "oidc",
            {"INQTRIX_OIDC_ISSUER": "https://idp.example.test"},
            "INQTRIX_OIDC_ISSUER",
        ),
        (
            "oidc",
            {"INQTRIX_OIDC_CLIENT_ID": "wrong-client"},
            "INQTRIX_OIDC_CLIENT_ID",
        ),
    ),
)
def test_optional_profile_rejects_topology_drift_before_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    overrides: dict[str, str],
    expected_name: str,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    _configure_profile(
        stack_env,
        secrets_env,
        profile,
        overrides=overrides,
    )
    trace = _install_fake_docker(tmp_path, monkeypatch)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--profile",
            profile,
            "up",
        ]
    )

    assert result == 2
    assert expected_name in capsys.readouterr().err
    assert _trace(trace) == []


def test_config_is_fail_closed_and_redacts_values_and_dsn_userinfo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "FAKE_CONFIG_OUTPUT",
        """
services:
  api:
    environment:
      INQTRIX_SESSION_SECRET: SyntheticSessionSecret2026
      INQTRIX_DATABASE_URL: postgresql+asyncpg://inqtrix:SyntheticCurrentPassword2026@postgres:5432/inqtrix
      INQTRIX_VALKEY_URL: redis://:SyntheticValkeyAmbientPassword@valkey:6379/0
      INQTRIX_S3_SECRET_KEY: AmbientOnlySyntheticS3Secret
      INQTRIX_EXAMPLE_KEY_SEED: AmbientOnlySyntheticKeySeed
""",
    )
    base = _arguments(root, stack_env, secrets_env)

    assert main([*base, "config"]) == 2
    assert _trace(trace) == []
    capsys.readouterr()

    assert main([*base, "config", "--redact"]) == 0
    captured = capsys.readouterr()
    assert "SyntheticSessionSecret2026" not in captured.out
    assert "SyntheticCurrentPassword2026" not in captured.out
    assert "SyntheticValkeyAmbientPassword" not in captured.out
    assert "AmbientOnlySyntheticS3Secret" not in captured.out
    assert "AmbientOnlySyntheticKeySeed" not in captured.out
    assert 'INQTRIX_DATABASE_URL: "***REDACTED***"' in captured.out
    assert 'INQTRIX_VALKEY_URL: "***REDACTED***"' in captured.out
    assert 'INQTRIX_S3_SECRET_KEY: "***REDACTED***"' in captured.out
    assert 'INQTRIX_EXAMPLE_KEY_SEED: "***REDACTED***"' in captured.out
    assert captured.err.startswith("+ docker compose ")
    assert captured.err.rstrip().endswith(" config")


@pytest.mark.parametrize(
    "name",
    [
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    ],
)
def test_config_redacts_every_otlp_header_variable(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The OTel exporter reads a generic and a per-signal headers variable,
    # and the per-signal one wins; each carries the Basic-auth key pair.
    root, stack_env, secrets_env, _ = _project(tmp_path)
    _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "FAKE_CONFIG_OUTPUT",
        f"""
services:
  api:
    environment:
      {name}: Authorization=Basic U3ludGhldGljT3RscEtleVBhaXI=
""",
    )

    assert main([*_arguments(root, stack_env, secrets_env), "config", "--redact"]) == 0
    captured = capsys.readouterr()
    assert "U3ludGhldGljT3RscEtleVBhaXI=" not in captured.out
    assert f'{name}: "***REDACTED***"' in captured.out


def test_key_seed_is_forbidden_in_non_secret_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    key_seed = "SyntheticKeySeedMustRemainPrivate"
    stack_env.write_text(
        stack_env.read_text(encoding="utf-8")
        + f"INQTRIX_EXAMPLE_KEY_SEED={key_seed}\n",
        encoding="utf-8",
    )

    assert main([*_arguments(root, stack_env, secrets_env), "status"]) == 2
    error = capsys.readouterr().err
    assert "INQTRIX_EXAMPLE_KEY_SEED" in error
    assert key_seed not in error
    assert _trace(trace) == []


def test_volume_deletion_requires_exact_project_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    base = _arguments(root, stack_env, secrets_env)

    assert main([*base, "down", "--volumes"]) == 2
    assert _trace(trace) == []

    assert (
        main(
            [
                *base,
                "down",
                "--volumes",
                "--confirm-project",
                "inqtrix",
            ]
        )
        == 0
    )
    assert _trace(trace)[0]["arguments"][-2:] == ["down", "--volumes"]


def test_owner_upgrade_builds_before_drain_and_rolls_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
        owner_mode=True,
    )
    assert migration_env is not None
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "FAKE_RUNNING_SERVICES",
        "api\nworker\ncollaboration\nweb\npgbouncer",
    )
    arguments = [
        *_arguments(root, stack_env, secrets_env),
        "--external-db",
        "--migration-env-file",
        str(migration_env),
        "owner-upgrade",
        "--confirm-project",
        "inqtrix",
    ]

    assert main(arguments) == 0

    joined = [" ".join(record["arguments"]) for record in _trace(trace)]
    assert "ps --services --filter status=running" in joined[0]
    build = next(index for index, call in enumerate(joined) if " build " in call)
    stop = next(index for index, call in enumerate(joined) if " stop api" in call)
    stop_web = next(
        index for index, call in enumerate(joined) if " stop web" in call
    )
    migrate = next(
        index
        for index, call in enumerate(joined)
        if " run --rm --no-deps " in call
    )
    restore_pooler = next(
        index
        for index, call in enumerate(joined)
        if " up -d --wait --no-deps --force-recreate pgbouncer" in call
    )
    roll_forward = next(
        index
        for index, call in enumerate(joined)
        if " up -d --wait --no-deps --force-recreate api " in call
    )
    stop_pooler = next(
        index
        for index, call in enumerate(joined)
        if " stop pgbouncer" in call
    )
    assert build < stop_web < stop < stop_pooler < migrate
    assert migrate < restore_pooler < roll_forward
    assert "INQTRIX_MIGRATION_RLS_MODE=owner" in joined[migrate]
    assert "INQTRIX_MIGRATION_SERVICES_QUIESCED=true" in joined[migrate]
    assert all(
        record["migration_env"] == "deploy/.env.migrate"
        for record in _trace(trace)
    )


def test_owner_upgrade_does_not_create_an_inactive_web_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
        owner_mode=True,
    )
    assert migration_env is not None
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api")

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--external-db",
            "--migration-env-file",
            str(migration_env),
            "owner-upgrade",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 0
    joined = [" ".join(record["arguments"]) for record in _trace(trace)]
    assert not any(" stop web" in call for call in joined)
    roll_forward = next(
        call
        for call in joined
        if " up -d --wait --no-deps --force-recreate " in call
    )
    assert roll_forward.endswith(" api")
    assert " web" not in roll_forward


def test_owner_roll_forward_interrupt_is_an_operator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
        owner_mode=True,
    )
    assert migration_env is not None
    _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api\nweb")
    original_run = ComposeRunner.run

    def interrupt_roll_forward(
        self: ComposeRunner,
        arguments: tuple[str, ...] | list[str],
    ) -> None:
        if tuple(arguments[:5]) == (
            "up",
            "-d",
            "--wait",
            "--no-deps",
            "--force-recreate",
        ):
            raise KeyboardInterrupt
        original_run(self, arguments)

    monkeypatch.setattr(ComposeRunner, "run", interrupt_roll_forward)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--external-db",
            "--migration-env-file",
            str(migration_env),
            "owner-upgrade",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "roll-forward outcome is not verified" in error
    assert "Traceback" not in error


def test_owner_upgrade_leaves_clients_stopped_after_ambiguous_migration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, migration_env = _project(
        tmp_path,
        external=True,
        owner_mode=True,
    )
    assert migration_env is not None
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api\nworker\nweb")
    monkeypatch.setenv("FAKE_FAIL_CONTAINS", "run --rm --no-deps")

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "--external-db",
            "--migration-env-file",
            str(migration_env),
            "owner-upgrade",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 2
    assert "remain stopped" in capsys.readouterr().err
    joined = [" ".join(record["arguments"]) for record in _trace(trace)]
    assert not any(" start " in call for call in joined)
    assert not any(
        " up -d --wait --no-deps --force-recreate " in call
        for call in joined
    )


def test_bundled_password_rotation_uses_the_staged_pair_and_is_secret_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api\nworker\nweb")
    new_password = "SyntheticRotatedPassword2026"
    secrets_before = secrets_env.read_text(encoding="utf-8")
    staged_secrets = secrets_before.replace(
        "INQTRIX_PG_PASSWORD=SyntheticCurrentPassword2026",
        f"INQTRIX_PG_PASSWORD={new_password}",
    )
    _write_private(secrets_env, staged_secrets)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "db",
            "rotate-password",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    assert new_password not in captured.out
    assert new_password not in captured.err
    assert secrets_env.read_text(encoding="utf-8") == staged_secrets
    assert stat.S_IMODE(secrets_env.stat().st_mode) == 0o600
    records = _trace(trace)
    serialized = json.dumps(records)
    assert new_password not in serialized
    assert any(record.get("stdin_bytes", 0) for record in records)
    joined = [" ".join(record["arguments"]) for record in records]
    alter = next(index for index, call in enumerate(joined) if " exec -T postgres " in call)
    recreate_db = next(
        index
        for index, call in enumerate(joined)
        if " up -d --wait --no-deps --force-recreate postgres" in call
    )
    recreate_clients = next(
        index
        for index, call in enumerate(joined)
        if " up -d --wait --no-deps --force-recreate api worker" in call
    )
    assert alter < recreate_db < recreate_clients


def test_password_rotation_waits_for_clients_before_reporting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api")
    monkeypatch.setenv(
        "FAKE_FAIL_CONTAINS",
        "up -d --wait --no-deps --force-recreate api",
    )
    staged_secrets = secrets_env.read_text(encoding="utf-8").replace(
        "INQTRIX_PG_PASSWORD=SyntheticCurrentPassword2026",
        "INQTRIX_PG_PASSWORD=SyntheticRotatedPassword2026",
    )
    _write_private(secrets_env, staged_secrets)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "db",
            "rotate-password",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert "PostgreSQL password applied" not in captured.out
    assert "database clients may remain stopped" in captured.err
    joined = [" ".join(record["arguments"]) for record in _trace(trace)]
    assert any(
        " up -d --wait --no-deps --force-recreate api" in call
        for call in joined
    )


def test_password_rotation_ambiguous_failure_leaves_clients_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api\nworker\nweb")
    monkeypatch.setenv("FAKE_FAIL_CONTAINS", "exec -T postgres")
    staged_secrets = secrets_env.read_text(encoding="utf-8").replace(
        "INQTRIX_PG_PASSWORD=SyntheticCurrentPassword2026",
        "INQTRIX_PG_PASSWORD=SyntheticRotatedPassword2026",
    )
    _write_private(secrets_env, staged_secrets)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "db",
            "rotate-password",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 2
    assert "outcome is not verified" in capsys.readouterr().err
    assert secrets_env.read_text(encoding="utf-8") == staged_secrets
    joined = [" ".join(record["arguments"]) for record in _trace(trace)]
    assert any(" exec -T postgres " in call for call in joined)
    assert not any(" start " in call for call in joined)
    assert not any(" force-recreate " in call for call in joined)


def test_password_rotation_post_alter_interrupt_is_an_operator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    _install_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RUNNING_SERVICES", "api")
    staged_secrets = secrets_env.read_text(encoding="utf-8").replace(
        "INQTRIX_PG_PASSWORD=SyntheticCurrentPassword2026",
        "INQTRIX_PG_PASSWORD=SyntheticRotatedPassword2026",
    )
    _write_private(secrets_env, staged_secrets)
    original_run = ComposeRunner.run

    def interrupt_database_recreate(
        self: ComposeRunner,
        arguments: tuple[str, ...] | list[str],
    ) -> None:
        if tuple(arguments[:3]) == ("up", "-d", "--wait"):
            raise KeyboardInterrupt
        original_run(self, arguments)

    monkeypatch.setattr(ComposeRunner, "run", interrupt_database_recreate)

    result = main(
        [
            *_arguments(root, stack_env, secrets_env),
            "db",
            "rotate-password",
            "--confirm-project",
            "inqtrix",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "database and selected secrets file use the staged password" in error
    assert "Traceback" not in error


def test_secret_files_with_broad_permissions_are_rejected_before_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, stack_env, secrets_env, _ = _project(tmp_path)
    trace = _install_fake_docker(tmp_path, monkeypatch)
    secrets_env.chmod(0o644)

    assert main([*_arguments(root, stack_env, secrets_env), "status"]) == 2
    assert _trace(trace) == []
