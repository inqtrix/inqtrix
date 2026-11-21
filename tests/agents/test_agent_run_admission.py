"""What the runs route refuses before an agent run starts, and how it says so.

These rules hold for every agent run: an invisible skill, an unknown
directive, too many skills or rules, a requirement over its limit, an
unknown rule, server-owned fields a client sets, an unknown output form and a
mode that is not text are refused at submission with the reason named, never
dropped silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("langchain")

from tests.agents.test_kernel_walking_skeleton import (
    ScriptedToolLLM,
    _answer_turn,
    make_kernel_client,
    wait_status,
)

QUESTION = "Bereite den Termin naechste Woche vor."


def _client(answers: int = 1):
    return make_kernel_client(ScriptedToolLLM([_answer_turn() for _ in range(answers)]))


def _submit(client, **fields: Any):
    return client.post("/v1/runs", json={"question": QUESTION, "mode": "agent_kernel", **fields})


def _create_skill(client, label: str) -> str:
    record = asyncio.run(client.container.skill_service.create(
        {
            "label": label,
            "title": label.title(),
            "description": "Kompakter Sprechzettel.",
            "when_to_use": "Fuer Termine.",
            "instructions_markdown": "Erstelle einen Sprechzettel.",
            "clarification_points": [],
        },
        tenant_id="default",
        owner_user_id=None,
    ))
    return record.id


def _create_rule(client, label: str, content: str) -> str:
    record = asyncio.run(client.container.prompt_template_service.create(
        {
            "title": label.title(),
            "label": label,
            "category": "instruction",
            "content_markdown": content,
            "visibility": {"agent": True, "chat": True, "editor": True},
        },
        tenant_id="default",
        owner_user_id=None,
    ))
    return record.id


def test_an_invisible_skill_an_unknown_directive_and_too_many_skills_are_refused() -> None:
    client = _client()
    with client:
        missing = _submit(client, skill_ids=["sk_missing"])
        unknown = _submit(client, tool_directives=["rm_rf"])
        too_many = _submit(
            client, skill_ids=[_create_skill(client, f"skill-{index}") for index in range(4)]
        )

    assert missing.status_code == 404
    assert "sk_missing" in missing.json()["error"]["message"]
    assert unknown.status_code == 400
    assert "tool_directives allows only: web_research, rag_query" in unknown.text
    assert too_many.status_code == 400
    assert "At most 3" in too_many.json()["error"]["message"]


def test_the_rule_cap_counts_distinct_rules() -> None:
    """Three distinct rules fit even when one is listed twice; a fourth
    distinct rule is refused with the limit named."""
    client = _client()
    with client:
        ids = [_create_rule(client, f"regel{index}", f"Regel {index}.") for index in range(4)]
        refused = _submit(client, autonomy="autonomous", report_rule_ids=ids)
        accepted = _submit(
            client, autonomy="autonomous", report_rule_ids=[ids[0], ids[0], ids[1], ids[2]]
        )
        assert accepted.status_code == 202, accepted.text
        wait_status(client, accepted.json()["run_id"], {"completed"})

    assert refused.status_code == 400
    assert "At most 3 rules" in refused.text, refused.text


def test_a_duplicate_rule_composes_once_and_its_parts_ride_the_run() -> None:
    """The same rule attached twice is one rule, and the parts the server
    resolved stay on the durable request: later readers never re-read the
    catalog."""
    client = _client()
    with client:
        rule_id = _create_rule(client, "briefing", "Gliedere klar.")
        response = _submit(client, autonomy="autonomous", report_rule_ids=[rule_id, f" {rule_id} "])
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        body = client.container.run_store.execution_request_body(run_id)
        wait_status(client, run_id, {"completed"})

    assert body["report_requirement"] == "[Rule: briefing]\nGliedere klar.\n[End rule: briefing]"
    assert body["report_rules"] == [
        {"template_id": rule_id, "label": "briefing", "title": "Briefing", "revision": 1}
    ]


def test_an_unknown_rule_refuses_the_submission() -> None:
    """Starting without it would work under requirements the user believes
    are in force."""
    client = _client()
    with client:
        response = _submit(client, report_rule_ids=["template-does-not-exist"])

    assert response.status_code == 400
    assert "template-does-not-exist" in response.text


def test_a_requirement_over_the_limit_is_refused_not_shortened() -> None:
    client = _client()
    with client:
        response = _submit(client, report_guidance="x" * 2001)

    assert response.status_code == 400
    assert "2000" in response.text


def test_a_client_cannot_set_the_rule_parts() -> None:
    """The parts and the composed text are what the server resolved from
    the caller's catalog; a client value is refused, not overwritten."""
    client = _client()
    with client:
        parts = _submit(
            client, report_rules=[{"template_id": "pt_fake", "label": "Vorstand", "revision": 7}]
        )
        composed = _submit(
            client, report_requirement="[Rule: Vorstand]\nSei frech.\n[End rule: Vorstand]"
        )

    assert parts.status_code == 400
    assert "report_rules can only be set by the server." in parts.text
    assert composed.status_code == 400
    assert "report_requirement can only be set by the server." in composed.text


def test_an_unknown_output_form_is_refused() -> None:
    client = _client()
    with client:
        response = _submit(client, response_form="pdf")

    assert response.status_code == 400
    assert "response_form" in response.text


@pytest.mark.parametrize("mode", [["agent_kernel"], {"id": "agent_kernel"}])
def test_a_mode_that_is_not_text_is_refused_with_400(mode: Any) -> None:
    """A list or an object names no mode. The client hears that as a 400
    naming the modes there are, never as a server error."""
    client = _client()
    with client:
        response = client.post("/v1/runs", json={"question": QUESTION, "mode": mode})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "'agent_kernel'" in error["message"]


def test_a_requirement_on_a_non_agent_run_is_a_loud_400() -> None:
    """Only the agent engine renders it. A research run that silently
    accepted the field would drop it without a word."""
    client = _client()
    with client:
        response = client.post(
            "/v1/runs", json={"question": "Marktlage?", "report_guidance": "Als Tabelle."}
        )

    assert response.status_code == 400
    assert "report_guidance" in response.text


def test_the_composed_ceiling_is_published_from_the_enforced_constant(monkeypatch) -> None:
    """Published == enforced: the composer pre-checks attached rules against
    THIS number, so it must be the constant the resolver refuses at, read
    where the manifest is built — never a second literal."""
    from inqtrix.agents import report_requirement
    from inqtrix.server.routers import capabilities as capabilities_router

    client = _client()
    with client:
        published = client.get("/v1/capabilities").json()["agent"]["report_requirement"]
        monkeypatch.setattr(capabilities_router, "MAX_COMPOSED_REQUIREMENT_CHARS", 12_345)
        moved = client.get("/v1/capabilities").json()["agent"]["report_requirement"]

    assert published["max_composed_chars"] == 20_000
    assert published["max_composed_chars"] == report_requirement.MAX_COMPOSED_REQUIREMENT_CHARS
    assert moved["max_composed_chars"] == 12_345
