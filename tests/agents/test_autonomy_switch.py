"""INQTRIX_AGENT_ALLOW_AUTONOMOUS: an operator takes the Auto mode away.

The desk offers what /v1/capabilities publishes, and the server refuses what it
no longer offers, so a client that still asks for the mode gets no autonomous run.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.settings import AGENT_MODE_IDS, AgentPlatformSettings
from tests.agents.test_workspace_agent import make_agent_client, wait_status

_SETTLED = {"waiting_for_approval", "completed", "failed"}


def _platform(**fields: object) -> AgentPlatformSettings:
    return AgentPlatformSettings(INQTRIX_AGENT_ALLOW_VOLATILE=True, **fields)


def _capabilities(monkeypatch: pytest.MonkeyPatch, **fields: object) -> dict:
    client = make_agent_client(monkeypatch, platform=_platform(**fields))
    with client:
        return client.get("/v1/capabilities").json()["agent"]


def test_the_mode_stays_offered_unless_an_operator_turns_it_off(monkeypatch) -> None:
    assert AgentPlatformSettings().allow_autonomous is True
    agent = _capabilities(monkeypatch)

    assert agent["autonomy_modes"] == ["strict", "balanced", "autonomous"]
    assert agent["mode_presets"] == [
        {"id": "standard", "autonomy": "balanced"},
        {"id": "auto", "autonomy": "autonomous"},
    ]
    assert set(agent["permission_modes"]) == {"strict", "balanced", "autonomous"}


def test_a_server_without_the_mode_publishes_only_what_it_runs(monkeypatch) -> None:
    agent = _capabilities(monkeypatch, INQTRIX_AGENT_ALLOW_AUTONOMOUS=False)

    assert agent["autonomy_modes"] == ["strict", "balanced"]
    assert agent["mode_presets"] == [{"id": "standard", "autonomy": "balanced"}]
    assert set(agent["permission_modes"]) == {"strict", "balanced"}
    assert agent["default_autonomy"] == "balanced"


def test_the_setting_reads_the_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("INQTRIX_AGENT_ALLOW_AUTONOMOUS", "false")
    assert AgentPlatformSettings().allow_autonomous is False


def test_a_default_the_server_no_longer_offers_stops_the_start() -> None:
    with pytest.raises(ValidationError, match="INQTRIX_AGENT_ALLOW_AUTONOMOUS"):
        AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_AUTONOMOUS=False,
            INQTRIX_AGENT_DEFAULT_AUTONOMY="autonomous",
        )


def test_a_default_with_spaces_around_it_stops_the_start_too() -> None:
    # The kernel strips the configured default before it reads it.
    with pytest.raises(ValidationError, match="INQTRIX_AGENT_ALLOW_AUTONOMOUS"):
        AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_AUTONOMOUS=False,
            INQTRIX_AGENT_DEFAULT_AUTONOMY=" autonomous ",
        )


def test_an_agent_run_in_the_turned_off_mode_is_refused(monkeypatch) -> None:
    client = make_agent_client(
        monkeypatch, platform=_platform(INQTRIX_AGENT_ALLOW_AUTONOMOUS=False)
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe ein Memo.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
            },
        )
        listed = client.get("/v1/runs").json()["data"]

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "autonomy_not_allowed"
    assert listed == []


def test_the_refusal_covers_every_agent_engine() -> None:
    # The route decides on this set; the kernel is not registered in this fixture.
    assert {"workspace_agent", "agent_kernel"} <= set(AGENT_MODE_IDS)


def test_an_agent_run_in_the_standard_mode_still_starts(monkeypatch) -> None:
    client = make_agent_client(
        monkeypatch, platform=_platform(INQTRIX_AGENT_ALLOW_AUTONOMOUS=False)
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe ein Memo.",
                "mode": "workspace_agent",
                "autonomy": "balanced",
            },
        )
        assert response.status_code == 202, response.text
        wait_status(client, response.json()["run_id"], _SETTLED)


def test_the_same_request_runs_where_the_mode_is_on(monkeypatch) -> None:
    client = make_agent_client(monkeypatch, platform=_platform())
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe ein Memo.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
            },
        )
        assert response.status_code == 202, response.text
        wait_status(client, response.json()["run_id"], _SETTLED)


def test_the_field_stays_inert_for_a_run_that_is_no_agent(monkeypatch) -> None:
    client = make_agent_client(
        monkeypatch, platform=_platform(INQTRIX_AGENT_ALLOW_AUTONOMOUS=False)
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={"question": "Was ist Yjs?", "mode": "research", "autonomy": "autonomous"},
        )
        assert response.status_code != 403, response.text
        wait_status(client, response.json()["run_id"], _SETTLED)
