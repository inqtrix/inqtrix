"""The account-preferences route stores and returns the last composer picks.

The desk saves the whole preference row on every change. A field the route
does not read is silently stored as "no pick", and a field it does not return
never reaches another device, so both directions go through the real
application with a signed-in user.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from inqtrix.providers.base import ProviderContext
from inqtrix.server.app import create_app
from inqtrix.settings import (
    AuthSettings,
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import StubLLM, StubSearch

_PASSWORD = "correct-horse-battery"
_ROW = {
    "contrast_mode": "standard",
    "locale": "de",
    "theme": "dark",
    "theme_preset": "petrol",
    "user_bubble_tone": "gray",
    "enable_agent_memory": False,
    "chat_model_tier": "",
    "agent_model_tier": "",
    "updated_at": 1.0,
}


def _signed_in_owner() -> tuple[TestClient, dict[str, str]]:
    app = create_app(
        settings=Settings(
            auth=AuthSettings(
                mode="local",
                session_secret="s" * 32,
                pat_pepper="p" * 32,
                oidc_insecure_dev_cookies=True,
            ),
            quota=QuotaSettings(enabled=False),
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
    )
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    setup = client.post(
        "/api/setup/owner",
        json={"email": "owner@example.com", "password": _PASSWORD, "display_name": "Owner"},
    )
    assert setup.status_code == 201, setup.text
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    return client, {"X-CSRF-Token": csrf}


def _last_choices(body: dict) -> tuple[str, str]:
    return body["last_research_profile"], body["last_agent_tier"]


def test_a_save_that_omits_the_last_choices_stores_no_pick() -> None:
    """An older page sends the row without the two fields; that is no pick."""
    client, headers = _signed_in_owner()

    saved = client.put("/v1/account/preferences", json=_ROW, headers=headers)

    assert saved.status_code == 200, saved.text
    assert _last_choices(saved.json()) == ("", "")
    assert _last_choices(client.get("/v1/account/preferences").json()) == ("", "")


def test_a_null_last_choice_is_no_pick() -> None:
    """``null`` must not become the string ``'None'``, which the domain refuses."""
    client, headers = _signed_in_owner()

    saved = client.put(
        "/v1/account/preferences",
        json={**_ROW, "last_research_profile": None, "last_agent_tier": None},
        headers=headers,
    )

    assert saved.status_code == 200, saved.text
    assert _last_choices(saved.json()) == ("", "")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("last_research_profile", "turbo", "unknown last report profile"),
        ("last_research_profile", "tief", "unknown last report profile"),
        ("last_agent_tier", "deep", "unknown last agent tier"),
        ("last_agent_tier", 7, "unknown last agent tier"),
    ],
)
def test_a_last_choice_outside_its_domain_is_refused(field, value, message) -> None:
    client, headers = _signed_in_owner()

    refused = client.put(
        "/v1/account/preferences", json={**_ROW, field: value}, headers=headers,
    )

    assert refused.status_code == 400, refused.text
    # The service's own wording: a 400 from another field would name that field.
    assert refused.json()["error"]["message"].startswith(message)
    assert client.get("/v1/account/preferences").status_code == 404


def test_saved_last_choices_come_back_and_a_later_save_replaces_them() -> None:
    client, headers = _signed_in_owner()

    first = client.put(
        "/v1/account/preferences",
        json={**_ROW, "last_research_profile": "compact", "last_agent_tier": "tief"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert _last_choices(client.get("/v1/account/preferences").json()) == ("compact", "tief")

    second = client.put(
        "/v1/account/preferences",
        json={
            **_ROW,
            "last_research_profile": "schnell",
            "last_agent_tier": "gruendlich",
            "updated_at": 2.0,
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert _last_choices(client.get("/v1/account/preferences").json()) == (
        "schnell",
        "gruendlich",
    )
