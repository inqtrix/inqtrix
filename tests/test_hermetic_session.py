"""The test session reads no ``.env`` file, hides the machine's AWS setup and isolates logging."""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from inqtrix import settings as settings_module
from inqtrix.settings import ProviderSettings
from tests import _hermetic
from tests._hermetic import (
    dotenv_probe_after_magika,
    inqtrix_settings_classes,
    restore_loggers,
    restore_machine_aws_setup,
    snapshot_loggers,
)

MARKER = "zzdotenv7"


def test_no_dependency_loads_a_dotenv_file_into_the_session(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"ZZ_DOTENV_PROBE={MARKER}\n", encoding="utf-8")
    session = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    without_switch = {name: value for name, value in session.items() if name != "PYTHON_DOTENV_DISABLED"}

    # magika loads a ``.env`` when it is imported; the session switches that off.
    assert dotenv_probe_after_magika(tmp_path, without_switch) == MARKER
    assert dotenv_probe_after_magika(tmp_path, session) == ""


def test_the_settings_read_no_dotenv_file_in_the_working_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"AWS_PROFILE={MARKER}\nINQTRIX_LOG_ENABLED=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Asked for the file, the settings read it; by default the session's do not.
    assert ProviderSettings(_env_file=".env").aws_profile == MARKER
    assert ProviderSettings().aws_profile == ""
    # A retired name in that file stops a service start; here it is unseen.
    with pytest.raises(RuntimeError, match="INQTRIX_LOG_ENABLED"):
        settings_module.check_retired_settings(env_file=".env")
    assert settings_module.check_retired_settings() == []
    settings_classes = inqtrix_settings_classes()
    assert len(settings_classes) >= 16
    assert [
        settings_class.__qualname__
        for settings_class in settings_classes
        if settings_class.model_config.get("env_file") is not None
    ] == []


_PRINT_AWS_SETUP = (
    "import boto3; session = boto3.Session(); credentials = session.get_credentials(); "
    "print(','.join(sorted(session.available_profiles)) + '|' + (credentials.access_key if credentials else '-'))"
)


def test_the_session_ignores_the_aws_setup_of_the_machine(tmp_path: Path) -> None:
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[profile zz-machine]\nregion = eu-west-1\n", encoding="utf-8")
    (aws / "credentials").write_text(
        "[default]\naws_access_key_id = zzmachinekey\naws_secret_access_key = zzmachinesecret\n", encoding="utf-8"
    )
    session = {**os.environ, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    machine = {name: value for name, value in session.items() if not name.startswith("AWS_")}

    def aws_setup(env: dict[str, str]) -> str:
        return subprocess.run(
            [sys.executable, "-c", _PRINT_AWS_SETUP],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout.strip()

    # boto3 reads the machine's profiles and credentials; the session sees
    # neither, only fixed test credentials.
    assert aws_setup(machine) == "default,zz-machine|zzmachinekey"
    assert aws_setup(session) == "|testing"
    # A configuration pin: the session switches off the instance metadata service.
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_an_opt_in_test_gets_the_machine_aws_setup_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hermetic, "MACHINE_AWS_ENVIRONMENT", {"AWS_PROFILE": "zz-machine"})

    restore_machine_aws_setup(monkeypatch)

    assert {name: value for name, value in os.environ.items() if name.startswith("AWS_")} == {
        "AWS_PROFILE": "zz-machine"
    }


def test_restoring_the_loggers_resets_every_other_family_logger() -> None:
    tracked = logging.getLogger("inqtrix.zz_tracked")
    area = logging.getLogger("inqtrix.zz_area")
    tracked.setLevel(logging.ERROR)
    state = snapshot_loggers(["inqtrix.zz_tracked"])
    tracked.setLevel(logging.WARNING)
    area.setLevel(logging.DEBUG)
    area.addHandler(logging.NullHandler())
    area.propagate = False
    area.disabled = True

    restore_loggers(state)

    assert tracked.level == logging.ERROR
    assert (area.level, area.handlers, area.propagate, area.disabled) == (logging.NOTSET, [], True, False)


def _defines_fixture(path: Path, name: str) -> bool:
    """Whether *path* defines a function called *name* or a fixture registered under it."""

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if (
            isinstance(node, ast.keyword)
            and node.arg == "name"
            and isinstance(node.value, ast.Constant)
            and node.value.value == name
        ):
            return True
    return False


def test_no_test_module_replaces_the_logging_isolation(tmp_path: Path) -> None:
    tests = Path(__file__).resolve().parent
    root_conftest = tests / "conftest.py"
    by_keyword = tmp_path / "test_by_keyword.py"
    by_keyword.write_text(
        '@pytest.fixture(name="reset_inqtrix_logger")\ndef keep():\n    yield\n', encoding="utf-8"
    )
    # A fixture of the same name in a test module, also one registered with
    # ``name=``, would replace the autouse one from the root conftest for
    # every test of that module.
    assert _defines_fixture(root_conftest, "reset_inqtrix_logger")
    assert _defines_fixture(by_keyword, "reset_inqtrix_logger")
    replacing = [
        path.relative_to(tests).as_posix()
        for path in sorted(tests.rglob("*.py"))
        if path != root_conftest and _defines_fixture(path, "reset_inqtrix_logger")
    ]
    assert replacing == []
