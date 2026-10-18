"""Deterministic Docker/Podman Compose process execution."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class DeployError(RuntimeError):
    """An operator-facing failure that is safe to print."""


@dataclass(frozen=True)
class DeployConfig:
    """Resolved local inputs for one Compose invocation."""

    project_directory: Path
    project_name: str
    compose_files: tuple[Path, ...]
    stack_env_file: Path
    secrets_env_file: Path
    migration_env_file: Path | None
    profiles: tuple[str, ...]
    engine: str
    external_db: bool

    def root_relative(self, path: Path, label: str) -> str:
        """Return a Compose root-relative pointer or reject hidden path drift."""
        try:
            return path.relative_to(self.project_directory).as_posix()
        except ValueError as exc:
            raise DeployError(
                f"{label} must be inside the project directory so raw Compose "
                "and service env_file resolve the same named pair"
            ) from exc


class ComposeRunner:
    """Build and execute one deterministic Compose command prefix."""

    def __init__(self, config: DeployConfig) -> None:
        self.config = config
        self._prefix = self._build_prefix()
        self._environment = self._build_environment()

    def _build_prefix(self) -> list[str]:
        executable = self.config.engine
        if executable == "auto":
            executable = next(
                (
                    candidate
                    for candidate in ("docker", "podman")
                    if shutil.which(candidate)
                ),
                "",
            )
            if not executable:
                raise DeployError(
                    "neither docker nor podman is available; select --engine"
                )
        elif shutil.which(executable) is None:
            raise DeployError(f"{executable} is not available on PATH")

        command = [
            executable,
            "compose",
            "--project-directory",
            # Compose treats this as the base for every relative build,
            # mount, and service env_file path. The canonical stack is
            # authored relative to deploy/compose, while the CLI's
            # project_directory is the repository root used as cwd.
            str(self.config.compose_files[0].parent),
            "--project-name",
            self.config.project_name,
        ]
        for path in self.config.compose_files:
            command.extend(("-f", str(path)))
        # The visible config DSN references values from the secrets file.
        command.extend(("--env-file", str(self.config.secrets_env_file)))
        command.extend(("--env-file", str(self.config.stack_env_file)))
        for profile in self.config.profiles:
            command.extend(("--profile", profile))
        return command

    def _build_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["INQTRIX_ENV_FILE"] = self.config.root_relative(
            self.config.stack_env_file,
            "stack env file",
        )
        environment["INQTRIX_SECRETS_FILE"] = self.config.root_relative(
            self.config.secrets_env_file,
            "stack secrets env file",
        )
        if self.config.migration_env_file is not None:
            environment["INQTRIX_MIGRATION_ENV_FILE"] = (
                self.config.root_relative(
                    self.config.migration_env_file,
                    "migration env file",
                )
            )
        else:
            environment.pop("INQTRIX_MIGRATION_ENV_FILE", None)
        return environment

    def _announce(self, arguments: Sequence[str]) -> None:
        from .preflight import redact_text, sensitive_values

        command = shlex.join([*self._prefix, *arguments])
        safe_command = redact_text(command, sensitive_values(self.config))
        print(f"+ {safe_command}", file=sys.stderr, flush=True)

    def run(self, arguments: Sequence[str]) -> None:
        self._announce(arguments)
        try:
            completed = subprocess.run(
                [*self._prefix, *arguments],
                cwd=self.config.project_directory,
                env=self._environment,
                check=False,
            )
        except OSError as exc:
            raise DeployError(
                f"could not execute {self._prefix[0]} compose"
            ) from exc
        if completed.returncode != 0:
            raise DeployError(
                f"Compose command failed with exit status {completed.returncode}"
            )

    def capture(self, arguments: Sequence[str]) -> str:
        from .preflight import redact_text, sensitive_values

        self._announce(arguments)
        try:
            completed = subprocess.run(
                [*self._prefix, *arguments],
                cwd=self.config.project_directory,
                env=self._environment,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise DeployError(
                f"could not execute {self._prefix[0]} compose"
            ) from exc
        if completed.returncode != 0:
            details = redact_text(
                completed.stderr.strip(),
                sensitive_values(self.config),
            )
            suffix = f": {details}" if details else ""
            raise DeployError(
                f"Compose command failed with exit status "
                f"{completed.returncode}{suffix}"
            )
        return completed.stdout

    def run_input(self, arguments: Sequence[str], input_text: str) -> None:
        from .preflight import redact_text, sensitive_values

        self._announce(arguments)
        try:
            completed = subprocess.run(
                [*self._prefix, *arguments],
                cwd=self.config.project_directory,
                env=self._environment,
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
            )
        except OSError as exc:
            raise DeployError(
                f"could not execute {self._prefix[0]} compose"
            ) from exc
        if completed.returncode != 0:
            details = redact_text(
                completed.stderr.strip(),
                sensitive_values(self.config),
            )
            suffix = f": {details}" if details else ""
            raise DeployError(
                f"Compose command failed with exit status "
                f"{completed.returncode}{suffix}"
            )

    def running_services(self) -> tuple[str, ...]:
        output = self.capture(
            ("ps", "--services", "--filter", "status=running")
        )
        return tuple(line.strip() for line in output.splitlines() if line.strip())
