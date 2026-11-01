"""Keep ``.env`` files, the machine's AWS setup and leftover logging state out of the tests.

Two ways would bring a ``.env`` in without a test asking for it: a dependency
that calls ``load_dotenv`` when it is imported (magika, through markitdown),
and the ``env_file`` of the Inqtrix settings, which pydantic-settings reads
relative to the working folder. boto3 would read the machine's profiles,
shared files and instance metadata. The root ``conftest.py`` closes all of
them before the tests are collected; an opt-in test that talks to a real
account gets the machine's AWS setup back for its own duration.

Loggers are process state as well. After every test the root ``conftest.py``
restores the loggers it tracks and takes from every other logger of the
``inqtrix.`` tree the level, handlers and switches the test gave it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic_settings import BaseSettings

import inqtrix.logging_config  # noqa: F401 - defines LoggingSettings
import inqtrix.settings

# The product's ``env_file`` of every settings class, recorded before the
# session switches them off, so that a test of the ``.env`` lookup can
# restore exactly what the product would read.
PRODUCT_ENV_FILES: dict[type[BaseSettings], Any] = {}

# The machine's ``AWS_*`` variables, set aside when the session hides them.
MACHINE_AWS_ENVIRONMENT: dict[str, str] = {}

_SESSION_AWS_ENVIRONMENT = {
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
}

_PRINT_PROBE_AFTER_MAGIKA = "import os, magika; print(os.environ.get('ZZ_DOTENV_PROBE', ''))"

LoggerState = dict[str, tuple[list[logging.Handler], int, bool, bool]]


def inqtrix_settings_classes() -> list[type[BaseSettings]]:
    """Every settings class defined in the ``inqtrix`` package."""
    found: set[type[BaseSettings]] = set()
    pending = list(BaseSettings.__subclasses__())
    while pending:
        candidate = pending.pop()
        pending.extend(candidate.__subclasses__())
        if candidate.__module__.split(".", 1)[0] == "inqtrix":
            found.add(candidate)
    return sorted(found, key=lambda candidate: candidate.__qualname__)


def shield_settings_from_dotenv() -> None:
    """Make every Inqtrix settings class ignore ``.env`` files."""
    for settings_class in inqtrix_settings_classes():
        PRODUCT_ENV_FILES.setdefault(settings_class, settings_class.model_config.get("env_file"))
        settings_class.model_config["env_file"] = None
    # ``check_retired_settings`` reads the module dict, not a class copy.
    inqtrix.settings._SETTINGS_MODEL_CONFIG["env_file"] = None


def hide_machine_aws_setup() -> None:
    """No AWS variable of the shell, shared file, instance metadata or real key in the session."""
    for name in [name for name in os.environ if name.startswith("AWS_")]:
        MACHINE_AWS_ENVIRONMENT.setdefault(name, os.environ.pop(name))
    os.environ.update(_SESSION_AWS_ENVIRONMENT)


def restore_machine_aws_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give an opt-in test that talks to a real account the machine's AWS setup back."""
    for name in [name for name in os.environ if name.startswith("AWS_")]:
        monkeypatch.delenv(name)
    for name, value in MACHINE_AWS_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def snapshot_loggers(names: Iterable[str]) -> LoggerState:
    """Handlers, level, propagation and switch of each named logger."""

    state: LoggerState = {}
    for name in names:
        logger = logging.getLogger(name)
        state[name] = (list(logger.handlers), logger.level, logger.propagate, logger.disabled)
    return state


def restore_loggers(state: LoggerState) -> None:
    """Restore *state* and reset every other logger of the ``inqtrix.`` tree a test changed.

    The module loggers start without a level, handlers or switches of their
    own, and nothing outside a test gives them any.
    """

    restored = dict(state)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        changed = isinstance(logger, logging.Logger) and (
            logger.level or logger.handlers or not logger.propagate or logger.disabled
        )
        if changed and name.startswith("inqtrix."):
            restored.setdefault(name, ([], logging.NOTSET, True, False))
    for name, (handlers, level, propagate, disabled) in restored.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


def dotenv_probe_after_magika(folder: Path, env: dict[str, str]) -> str:
    """``ZZ_DOTENV_PROBE`` as a child process sees it after importing magika in *folder*."""
    # With ``python -c`` python-dotenv starts its search in the working folder.
    return subprocess.run(
        [sys.executable, "-c", _PRINT_PROBE_AFTER_MAGIKA],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip()
