"""Deleted settings that are still set are reported, never silently ignored.

``RETIRED_SETTINGS`` names every deleted setting with its replacement and its
outcome: ``warn`` (the rule, one WARNING line) or ``abort`` where silently
ignoring the value would undo a deliberate operator choice (the start stops
with a ``RuntimeError`` that names each such setting and its replacement). The
report names the variable and its replacement, never the value.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from inqtrix import settings as settings_module

_OLD_LOG_VARIABLES = (
    "INQTRIX_LOG_ENABLED",
    "INQTRIX_LOG_CONSOLE",
    "INQTRIX_LOG_INCLUDE_WEB",
)
SECRET_VALUE = "value-MARKER-91c2"

# The suite's conftest removes every retired name from the environment and
# points the settings dotenv file at nothing, so each test sets exactly what
# it checks.


def test_old_log_variables_are_abort_entries() -> None:
    entries = {entry.name: entry for entry in settings_module.RETIRED_SETTINGS}

    for name in _OLD_LOG_VARIABLES:
        assert entries[name].outcome == "abort"
        assert entries[name].replacement


@pytest.mark.parametrize("name", _OLD_LOG_VARIABLES)
def test_old_log_variable_aborts_the_start_with_a_replacement_hint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    monkeypatch.setenv(name, SECRET_VALUE)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(RuntimeError) as stop:
            settings_module.check_retired_settings()

    message = str(stop.value)
    replacement = {entry.name: entry for entry in settings_module.RETIRED_SETTINGS}[name].replacement
    assert f"Retired setting {name} is set; use {replacement} instead." in message
    assert replacement.split()[0].startswith("INQTRIX_LOG_")
    assert SECRET_VALUE not in message
    # The exception carries the report; the process hook shows it once.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_every_abort_entry_that_is_set_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INQTRIX_LOG_ENABLED", SECRET_VALUE)
    monkeypatch.setenv("INQTRIX_LOG_INCLUDE_WEB", SECRET_VALUE)

    with pytest.raises(RuntimeError) as stop:
        settings_module.check_retired_settings()

    message = str(stop.value)
    assert "Retired setting INQTRIX_LOG_ENABLED is set" in message
    assert "Retired setting INQTRIX_LOG_INCLUDE_WEB is set" in message
    assert SECRET_VALUE not in message


def test_retired_value_in_the_env_file_is_found(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"INQTRIX_LOG_CONSOLE={SECRET_VALUE}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Retired setting INQTRIX_LOG_CONSOLE is set") as stop:
        settings_module.check_retired_settings(env_file=env_file)

    assert SECRET_VALUE not in str(stop.value)


def test_the_dotenv_file_of_the_settings_is_read_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retired name in the ``.env`` the service reads stops the start too."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"INQTRIX_LOG_ENABLED={SECRET_VALUE}\n", encoding="utf-8")
    monkeypatch.setitem(settings_module._SETTINGS_MODEL_CONFIG, "env_file", str(env_file))

    with pytest.raises(RuntimeError, match="Retired setting INQTRIX_LOG_ENABLED is set") as stop:
        settings_module.check_retired_settings()

    assert SECRET_VALUE not in str(stop.value)


def test_warn_entry_is_reported_and_the_start_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entry = settings_module.RetiredSetting(
        name="INQTRIX_TEST_RETIRED_KNOB",
        replacement="INQTRIX_TEST_NEW_KNOB",
        outcome="warn",
    )
    monkeypatch.setattr(settings_module, "RETIRED_SETTINGS", (entry,))
    monkeypatch.setenv("INQTRIX_TEST_RETIRED_KNOB", SECRET_VALUE)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        found = settings_module.check_retired_settings()

    assert found == [entry]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "INQTRIX_TEST_RETIRED_KNOB" in warnings[0].getMessage()
    assert "INQTRIX_TEST_NEW_KNOB" in warnings[0].getMessage()
    assert SECRET_VALUE not in caplog.text


def test_nothing_set_passes_silently(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="inqtrix"):
        found = settings_module.check_retired_settings()

    assert found == []
    assert caplog.records == []


def test_forwarded_allow_ips_stops_the_start_in_gateway_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inqtrix reads X-Forwarded-For itself and runs uvicorn without proxy
    # headers; in gateway mode a forwarded-IP allow list would sit beside the
    # identity boundary without any effect, so the start stops.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", SECRET_VALUE)
    monkeypatch.setenv("INQTRIX_AUTH_MODE", "gateway")

    with pytest.raises(RuntimeError, match="Retired setting FORWARDED_ALLOW_IPS is set") as stop:
        settings_module.check_retired_settings()

    assert SECRET_VALUE not in str(stop.value)


@pytest.mark.parametrize("mode", [None, "none", "apikey", "oidc", "local"])
def test_forwarded_allow_ips_only_warns_in_the_other_modes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str | None
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", SECRET_VALUE)
    if mode is None:
        monkeypatch.delenv("INQTRIX_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("INQTRIX_AUTH_MODE", mode)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        found = settings_module.check_retired_settings()

    assert [entry.name for entry in found] == ["FORWARDED_ALLOW_IPS"]
    assert "Retired setting FORWARDED_ALLOW_IPS is set and has no effect" in caplog.text
    assert SECRET_VALUE not in caplog.text


def test_the_mode_in_the_env_file_decides_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("INQTRIX_AUTH_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"INQTRIX_AUTH_MODE=gateway\nFORWARDED_ALLOW_IPS={SECRET_VALUE}\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="Retired setting FORWARDED_ALLOW_IPS is set") as stop:
        settings_module.check_retired_settings(env_file=env_file)

    assert SECRET_VALUE not in str(stop.value)

