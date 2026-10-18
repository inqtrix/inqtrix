"""Stock prompt templates seed once per scoped user, lazily.

The listing route is the chokepoint (every auth mode passes it right
after login because the client hydrates templates on app start). The
pinned contract: exactly one seeding per user ever — a deleted default
stays deleted, a grown library is never injected into, concurrent or
repeated listings cannot double-seed, and unscoped deployments never
seed. Plus the drift guard: the server module's four bodies stay
byte-identical to the demo rule files they were generated from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from inqtrix.content.default_prompt_seed import DEFAULT_PROMPT_SEEDS

from tests.test_prompt_template_routes import (
    OWNER,
    PAYLOAD,
    RECIPIENT,
    as_user,
    make_world,
)

EXPECTED_LABELS = {"briefing", "lektor", "summarizer", "translator"}


def _listed(client, user_id):
    response = client.get("/v1/prompt-templates", headers=as_user(user_id))
    assert response.status_code == 200
    return response.json()["data"]


def test_first_listing_seeds_the_four_stock_prompts_per_user():
    client, _container = make_world()
    listed = _listed(client, OWNER)
    assert {item["label"] for item in listed} == EXPECTED_LABELS
    assert all(item["access"] == {"mode": "owner"} for item in listed)
    # A second listing seeds nothing further (marker claimed).
    assert len(_listed(client, OWNER)) == 4
    # Every user gets their OWN four (no sharing, distinct rows).
    recipient_rows = _listed(client, RECIPIENT)
    assert {item["label"] for item in recipient_rows} == EXPECTED_LABELS
    owner_ids = {item["id"] for item in _listed(client, OWNER)}
    assert owner_ids.isdisjoint({item["id"] for item in recipient_rows})


def test_a_deleted_default_stays_deleted_forever():
    client, _container = make_world()
    listed = _listed(client, OWNER)
    lektor = next(item for item in listed if item["label"] == "lektor")
    deleted = client.delete(
        f"/v1/prompt-templates/{lektor['id']}", headers=as_user(OWNER)
    )
    assert deleted.status_code == 204
    # The next listing must NOT resurrect it — the marker outlives the row.
    remaining = _listed(client, OWNER)
    assert {item["label"] for item in remaining} == (
        EXPECTED_LABELS - {"lektor"}
    )
    # The hard case: with EVERY default deleted the user owns nothing
    # again — only the claimed marker now stands between them and a
    # re-seed. (With fewer deletions the owns-any guard would mask a
    # broken marker.)
    for item in remaining:
        assert client.delete(
            f"/v1/prompt-templates/{item['id']}", headers=as_user(OWNER)
        ).status_code == 204
    assert _listed(client, OWNER) == []


def test_a_grown_library_is_never_injected_into():
    client, _container = make_world()
    # The user creates a template BEFORE ever listing (e.g. the client
    # project sync pushed local rules up first).
    created = client.post(
        "/v1/prompt-templates", json=PAYLOAD, headers=as_user(OWNER)
    )
    assert created.status_code == 201
    listed = _listed(client, OWNER)
    # Only their own row — no stock prompts appeared beside it.
    assert [item["id"] for item in listed] == [created.json()["id"]]
    # And the marker is claimed: later listings stay uninjected too.
    assert len(_listed(client, OWNER)) == 1


def test_unscoped_listing_never_seeds():
    client, _container = make_world()
    response = client.get("/v1/prompt-templates")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_seed_module_matches_the_demo_rule_files_byte_exactly():
    """Drift guard: regenerate the module, never edit one side alone."""
    rules_dir = (
        Path(__file__).resolve().parents[1]
        / "apps"
        / "research-desk"
        / "src"
        / "features"
        / "project"
        / "demoContent"
        / "rules"
    )
    by_label = {seed["label"]: seed for seed in DEFAULT_PROMPT_SEEDS}
    assert set(by_label) == EXPECTED_LABELS
    for label in sorted(EXPECTED_LABELS):
        text = (rules_dir / f"{label}.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.index("\n---\n", 4)
        front = text[4:end]
        # Body handling mirrors the client's parseChatRule: everything
        # after the FIRST closing delimiter, left-stripped. (The
        # the briefing body contains a nested `---` horizontal rule —
        # splitting on the first delimiter only is load-bearing.)
        body = text[end + len("\n---\n") :].lstrip()
        seed = by_label[label]
        assert seed["content_markdown"] == body, label
        front_title = re.search(r'^title: "(.*)"$', front, re.M)
        assert front_title is not None
        assert seed["title"] == front_title.group(1), label
        front_category = re.search(r'^category: "(.*)"$', front, re.M)
        assert front_category is not None
        assert seed["category"] == front_category.group(1), label
        # Visibility too: a template that is attachable in the demo but
        # not for a real user (or the reverse) makes the shop window
        # lie about what the product does.
        front_visibility = re.search(r"^visibility: (\{.*\})$", front, re.M)
        assert front_visibility is not None, label
        demo_visibility = json.loads(front_visibility.group(1))
        assert {
            surface: bool(demo_visibility.get(surface, False))
            for surface in ("agent", "chat", "editor")
        } == {
            surface: bool(seed["visibility"].get(surface, False))
            for surface in ("agent", "chat", "editor")
        }, label
