"""Guarded owner migrations and bundled PostgreSQL password rotation."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager

from .compose import ComposeRunner, DeployError
from .preflight import (
    database_host,
    read_dotenv,
    validate_start,
)

_DATABASE_CLIENTS = ("api", "worker", "collaboration", "pgbouncer")
_OWNER_QUIESCE_SERVICES = (
    "web",
    "collaboration",
    "worker",
    "api",
    "pgbouncer",
)
_ROLL_FORWARD_SERVICES = ("api", "worker", "collaboration", "web")


def _confirm(value: str | None, project_name: str, operation: str) -> None:
    if value != project_name:
        raise DeployError(
            f"{operation} requires --confirm-project {project_name}"
        )


@contextmanager
def _maintenance_interrupts() -> Iterator[None]:
    """Turn termination signals into recoverable maintenance exceptions."""
    previous_handlers: dict[signal.Signals, object] = {}

    def interrupt(_signum: int, _frame: object) -> None:
        raise InterruptedError

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    for signum in handled_signals:
        previous_handlers[signum] = signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def run_owner_upgrade(
    runner: ComposeRunner,
    confirmation: str | None,
) -> None:
    """Build, quiesce clients, migrate once, and roll active workloads forward."""
    config = runner.config
    _confirm(confirmation, config.project_name, "owner-upgrade")
    if not config.external_db:
        raise DeployError("owner-upgrade requires --external-db")
    values = validate_start(config)
    if values.get("INQTRIX_MIGRATION_RLS_MODE") != "owner":
        raise DeployError(
            "owner-upgrade requires INQTRIX_MIGRATION_RLS_MODE=owner"
        )

    running = runner.running_services()
    running_set = set(running)
    build_targets = ["migrate"]
    build_targets.extend(
        service for service in _ROLL_FORWARD_SERVICES if service in running_set
    )
    runner.run(("build", *build_targets))

    stopped: list[str] = []
    with _maintenance_interrupts():
        try:
            for service in _OWNER_QUIESCE_SERVICES:
                if service in running_set:
                    runner.run(("stop", service))
                    stopped.append(service)
        except (DeployError, InterruptedError, KeyboardInterrupt):
            for service in reversed(stopped):
                try:
                    runner.run(("start", service))
                except (DeployError, InterruptedError, KeyboardInterrupt):
                    pass
            raise DeployError(
                "owner upgrade interrupted before migration; old database "
                "clients were requested to restart"
            ) from None

        try:
            runner.run(
                (
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    "INQTRIX_MIGRATION_RLS_MODE=owner",
                    "-e",
                    "INQTRIX_MIGRATION_SERVICES_QUIESCED=true",
                    "migrate",
                )
            )
        except (DeployError, InterruptedError, KeyboardInterrupt):
            raise DeployError(
                "owner migration outcome is not verified; database clients "
                "remain stopped"
            ) from None

        if "pgbouncer" in running_set:
            try:
                # PgBouncer is an optional database client, not an
                # application image build target. Recreate and health-check
                # it before reconnecting previously active workloads.
                runner.run(
                    (
                        "up",
                        "-d",
                        "--wait",
                        "--no-deps",
                        "--force-recreate",
                        "pgbouncer",
                    )
                )
            except (DeployError, InterruptedError, KeyboardInterrupt):
                raise DeployError(
                    "owner migration succeeded but PgBouncer recovery is not "
                    "verified; application workloads remain stopped"
                ) from None

        active = [
            service
            for service in _ROLL_FORWARD_SERVICES
            if service in running_set
        ]
        if active:
            try:
                runner.run(
                    (
                        "up",
                        "-d",
                        "--wait",
                        "--no-deps",
                        "--force-recreate",
                        *active,
                    )
                )
            except (DeployError, InterruptedError, KeyboardInterrupt):
                raise DeployError(
                    "owner migration succeeded but workload roll-forward "
                    "outcome is not verified; workloads may remain stopped "
                    "or partially recreated, so verify the schema and service "
                    "state before recovery"
                ) from None


def _password_sql(username: str, password: str) -> str:
    quoted_user = '"' + username.replace('"', '""') + '"'
    quoted_password = "'" + password.replace("'", "''") + "'"
    return f"ALTER ROLE {quoted_user} PASSWORD {quoted_password};\n"


def rotate_database_password(
    runner: ComposeRunner,
    confirmation: str | None,
) -> None:
    """Apply the password already staged in the selected secrets env file."""
    config = runner.config
    _confirm(confirmation, config.project_name, "db rotate-password")
    if config.external_db:
        raise DeployError(
            "db rotate-password manages only the bundled PostgreSQL service"
        )
    values = validate_start(config)
    database_url = values["INQTRIX_DATABASE_URL"]
    if "${INQTRIX_PG_PASSWORD}" not in read_dotenv(
        config.stack_env_file
    ).get("INQTRIX_DATABASE_URL", ""):
        raise DeployError(
            "bundled password rotation requires INQTRIX_DATABASE_URL to "
            "interpolate ${INQTRIX_PG_PASSWORD}"
        )
    if database_host(database_url) not in {"postgres", "pgbouncer"}:
        raise DeployError("bundled password rotation requires a bundled DSN")

    # Rotation applies exactly the credential already staged in the selected
    # private file. Ambient values are rejected by preflight and are never a
    # password source for the SQL mutation.
    new_password = read_dotenv(config.secrets_env_file)[
        "INQTRIX_PG_PASSWORD"
    ]
    running = set(runner.running_services())
    stopped: list[str] = []
    database_changed = False
    username = values.get("INQTRIX_PG_USER", "inqtrix")
    database = values.get("INQTRIX_PG_DB", "inqtrix")
    psql_arguments = (
        "exec",
        "-T",
        "postgres",
        "psql",
        "--username",
        username,
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=1",
    )
    try:
        with _maintenance_interrupts():
            try:
                for service in _DATABASE_CLIENTS:
                    if service in running:
                        runner.run(("stop", service))
                        stopped.append(service)
            except (DeployError, InterruptedError, KeyboardInterrupt) as exc:
                for service in reversed(stopped):
                    try:
                        runner.run(("start", service))
                    except (DeployError, InterruptedError, KeyboardInterrupt):
                        pass
                raise DeployError(
                    f"{exc}; password application did not begin and previously "
                    "stopped clients were requested to restart"
                ) from None

            try:
                runner.run_input(
                    psql_arguments,
                    _password_sql(username, new_password),
                )
                database_changed = True
            except (DeployError, InterruptedError, KeyboardInterrupt):
                raise DeployError(
                    "password application outcome is not verified; the selected "
                    "secrets file remains staged and database clients remain "
                    "stopped"
                ) from None

            runner.run(
                (
                    "up",
                    "-d",
                    "--wait",
                    "--no-deps",
                    "--force-recreate",
                    "postgres",
                )
            )
            active_clients = [
                service for service in _DATABASE_CLIENTS if service in running
            ]
            if active_clients:
                runner.run(
                    (
                        "up",
                        "-d",
                        "--wait",
                        "--no-deps",
                        "--force-recreate",
                        *active_clients,
                    )
                )
    except (DeployError, InterruptedError, KeyboardInterrupt) as exc:
        if database_changed:
            raise DeployError(
                f"{exc}; the database and selected secrets file use the "
                "staged password, "
                "but database clients may remain stopped"
            ) from None
        if isinstance(exc, DeployError):
            raise
        raise DeployError(
            "password rotation was interrupted before its outcome could be "
            "verified; database clients may remain stopped"
        ) from None
    finally:
        new_password = ""

    print(
        "PostgreSQL password applied from the selected secrets file; "
        "no credential was printed."
    )
