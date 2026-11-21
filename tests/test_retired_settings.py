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


@pytest.mark.parametrize("name", ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"])
def test_the_azure_service_principal_variables_only_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    # Azure is reached with keys only. Other Azure tools in the same
    # environment may still read these names, so the start continues.
    monkeypatch.setenv(name, SECRET_VALUE)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        found = settings_module.check_retired_settings()

    assert [entry.name for entry in found] == [name]
    assert (
        f"Retired setting {name} is set and has no effect; use AZURE_OPENAI_API_KEY "
        "and AZURE_AI_PROJECT_API_KEY (keys only) instead." in caplog.text
    )
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



_KERNEL_LIMITS = "the agent kernel's limits (INQTRIX_AGENT_KERNEL_*)"
_MISSION_LIMITS = {
    "INQTRIX_AGENT_MAX_PARALLEL_CHILDREN": (
        "RUN_MAX_CONCURRENT_PER_USER (child runs of a kernel batch count against it)"
    ),
    "INQTRIX_AGENT_DISCOVERY_MAX_TOOL_CALLS": _KERNEL_LIMITS,
    "INQTRIX_AGENT_MAX_PLAN_TASKS": _KERNEL_LIMITS,
    "INQTRIX_AGENT_MAX_REPLAN_ROUNDS": _KERNEL_LIMITS,
    "INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS": _KERNEL_LIMITS,
    "INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET": _KERNEL_LIMITS,
    "INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET": _KERNEL_LIMITS,
    "INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS": _KERNEL_LIMITS,
    "INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW": (
        "the run's Stufe (agent_tier) and permission mode, which decide its web research"
    ),
}
"""Limits of the retired mission engine with the replacement each names. No
engine reads them any more, so ignoring one undoes no choice."""

_MISSION_CHOICES = {
    "INQTRIX_AGENT_MAX_CLARIFICATION_ROUNDS": (
        "the run's Stufe (agent_tier), which decides its clarification rounds"
    ),
    "INQTRIX_AGENT_DEFAULT_MODE": "no setting (agent_kernel is the only agent mode)",
}
"""Mission settings that recorded a deliberate choice: a number of
clarification rounds narrows what a run may ask, and a default mode chose the
mission. Ignoring either would undo that choice, so the start stops."""


def _status_line(found: list[settings_module.RetiredSetting]) -> None:
    from inqtrix.logging_config import ResolvedLogging, log_status_line

    log_status_line(
        ResolvedLogging(
            service="api",
            target="console",
            level="INFO",
            format="text",
            file_path=None,
            web_level="WARNING",
        ),
        retired=found,
    )


@pytest.mark.parametrize("name", sorted(_MISSION_LIMITS))
def test_a_mission_limit_warns_and_stands_in_the_status_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    monkeypatch.setenv(name, SECRET_VALUE)

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        found = settings_module.check_retired_settings()
        _status_line(found)

    assert [entry.name for entry in found] == [name]
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == [
        f"Retired setting {name} is set and has no effect; use {_MISSION_LIMITS[name]} instead."
    ]
    assert f"retired_settings={name}" in caplog.text
    assert SECRET_VALUE not in caplog.text


@pytest.mark.parametrize("value", [SECRET_VALUE, "agent_kernel", "0"])
@pytest.mark.parametrize("name", sorted(_MISSION_CHOICES))
def test_a_deliberate_mission_choice_stops_the_start_with_its_replacement(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # The check reads names only: a harmless value stops the start as well.
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError) as stop:
        settings_module.check_retired_settings()

    message = str(stop.value)
    assert message == (
        f"Retired setting {name} is set; use {_MISSION_CHOICES[name]} instead. "
        "The service does not start until it is removed."
    )
    assert SECRET_VALUE not in message
