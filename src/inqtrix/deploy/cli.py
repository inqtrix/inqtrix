"""Argument parsing and command routing for ``inqtrix-deploy``."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .compose import ComposeRunner, DeployConfig, DeployError
from .maintenance import rotate_database_password, run_owner_upgrade
from .preflight import (
    redact_text,
    require_file,
    require_private_file,
    sensitive_values,
    validate_pair_contract,
    validate_start,
)

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _resolve_path(root: Path, value: Path | None, default: str) -> Path:
    path = value if value is not None else Path(default)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _confirm(value: str | None, project_name: str, operation: str) -> None:
    if value != project_name:
        raise DeployError(
            f"{operation} requires --confirm-project {project_name}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inqtrix-deploy",
        description="Operate the canonical Inqtrix Compose stack safely.",
    )
    parser.add_argument(
        "--project-directory",
        type=Path,
        default=Path.cwd(),
        help="Repository checkout containing deploy/compose (default: cwd).",
    )
    parser.add_argument(
        "--project-name",
        help=(
            "Compose project name (default: inqtrix, or inqtrix-dev with "
            "--dev-ports)."
        ),
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument("--migration-env-file", type=Path)
    parser.add_argument(
        "--engine",
        choices=("auto", "docker", "podman"),
        default=os.environ.get("INQTRIX_DEPLOY_ENGINE", "auto"),
    )
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--dev-ports", action="store_true")
    parser.add_argument("--external-db", action="store_true")
    web_runtime = parser.add_mutually_exclusive_group()
    web_runtime.add_argument(
        "--web-tls",
        action="store_true",
        help="Enable direct TLS on the default Python web gateway.",
    )
    web_runtime.add_argument(
        "--web-nginx",
        action="store_true",
        help="Select the explicit nginx web target (external TLS termination).",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    up = commands.add_parser("up")
    up.add_argument("--build", action="store_true")
    up.add_argument("--detach", action="store_true")
    up.add_argument("services", nargs="*")

    down = commands.add_parser("down")
    down.add_argument("--volumes", action="store_true")
    down.add_argument("--remove-orphans", action="store_true")
    down.add_argument("--confirm-project")

    commands.add_parser("status")

    logs = commands.add_parser("logs")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--tail", type=int)
    logs.add_argument("services", nargs="*")

    restart = commands.add_parser("restart")
    restart.add_argument("services", nargs="*")

    build = commands.add_parser("build")
    build.add_argument("--pull", action="store_true")
    build.add_argument("--no-cache", action="store_true")
    build.add_argument("services", nargs="*")

    config = commands.add_parser("config")
    config.add_argument(
        "--redact",
        action="store_true",
        help="Required: redact credentials and credential-bearing DSNs.",
    )

    owner = commands.add_parser("owner-upgrade")
    owner.add_argument("--confirm-project")

    database = commands.add_parser("db")
    database_commands = database.add_subparsers(
        dest="database_command",
        required=True,
    )
    rotate = database_commands.add_parser("rotate-password")
    rotate.add_argument("--confirm-project")
    return parser


def _config_from_args(args: argparse.Namespace) -> DeployConfig:
    root = args.project_directory.resolve()
    stack = root / "deploy" / "compose" / "compose.stack.yaml"
    compose_files = [stack]
    if args.dev_ports:
        compose_files.append(
            root / "deploy" / "compose" / "compose.dev-ports.yaml"
        )
    if args.external_db:
        compose_files.append(
            root / "deploy" / "compose" / "compose.external-db.yaml"
        )
    if args.web_tls:
        compose_files.append(
            root / "deploy" / "compose" / "compose.web-tls.yaml"
        )
    if args.web_nginx:
        compose_files.append(
            root / "deploy" / "compose" / "compose.web-nginx.yaml"
        )
    project_name = args.project_name or (
        "inqtrix-dev" if args.dev_ports else "inqtrix"
    )
    if _PROJECT_RE.fullmatch(project_name) is None:
        raise DeployError(
            "project name must start with a lowercase letter or digit and "
            "contain only lowercase letters, digits, underscores, or hyphens"
        )
    for path in compose_files:
        require_file(path, "Compose file")

    stack_env = _resolve_path(
        root,
        args.env_file,
        "deploy/.env.stack",
    )
    secrets_env = _resolve_path(
        root,
        args.secrets_file,
        "deploy/.env.stack.secrets",
    )
    migration_env = (
        _resolve_path(root, args.migration_env_file, "")
        if args.migration_env_file is not None
        else None
    )
    require_file(stack_env, "stack env file")
    require_private_file(secrets_env, "stack secrets env file")
    if migration_env is not None:
        require_private_file(migration_env, "migration env file")

    config = DeployConfig(
        project_directory=root,
        project_name=project_name,
        compose_files=tuple(path.resolve() for path in compose_files),
        stack_env_file=stack_env,
        secrets_env_file=secrets_env,
        migration_env_file=migration_env,
        profiles=tuple(dict.fromkeys(args.profile)),
        engine=args.engine,
        external_db=args.external_db,
    )
    validate_pair_contract(config)
    return config


def _dispatch(args: argparse.Namespace, config: DeployConfig) -> None:
    runner = ComposeRunner(config)
    if args.command == "up":
        values = validate_start(config)
        if values.get("INQTRIX_MIGRATION_RLS_MODE") == "owner":
            raise DeployError(
                "owner-mode migrations require the guarded owner-upgrade command"
            )
        command = ["up"]
        if args.detach:
            command.append("-d")
        if args.build:
            command.append("--build")
        command.extend(args.services)
        runner.run(command)
        return
    if args.command == "down":
        if args.volumes:
            _confirm(args.confirm_project, config.project_name, "down --volumes")
        command = ["down"]
        if args.volumes:
            command.append("--volumes")
        if args.remove_orphans:
            command.append("--remove-orphans")
        runner.run(command)
        return
    if args.command == "status":
        runner.run(("ps",))
        return
    if args.command == "logs":
        command = ["logs"]
        if args.follow:
            command.append("--follow")
        if args.tail is not None:
            if args.tail < 0:
                raise DeployError("--tail must be zero or greater")
            command.extend(("--tail", str(args.tail)))
        command.extend(args.services)
        runner.run(command)
        return
    if args.command == "restart":
        runner.run(("restart", *args.services))
        return
    if args.command == "build":
        command = ["build"]
        if args.pull:
            command.append("--pull")
        if args.no_cache:
            command.append("--no-cache")
        command.extend(args.services)
        runner.run(command)
        return
    if args.command == "config":
        if not args.redact:
            raise DeployError(
                "config output may contain credentials; pass --redact"
            )
        rendered = runner.capture(("config",))
        sys.stdout.write(redact_text(rendered, sensitive_values(config)))
        return
    if args.command == "owner-upgrade":
        run_owner_upgrade(runner, args.confirm_project)
        return
    if args.command == "db" and args.database_command == "rotate-password":
        rotate_database_password(
            runner,
            args.confirm_project,
        )
        return
    raise DeployError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deployment CLI and return a process-style exit status."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        config = _config_from_args(args)
        _dispatch(args, config)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
