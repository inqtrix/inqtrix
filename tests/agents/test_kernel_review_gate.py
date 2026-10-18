"""What decides a kernel run's review, and what carries it.

Two questions, never answered with one flag. WHETHER a run is
reviewed comes from the published report-quality level of its Stufe, so a
Stufe that promises verification in ``/v1/capabilities`` gets it in both
engines. HOW the review is carried is the shared pass's own decision from
the size of the material, so the Stufe table never has to publish a
transport it says nothing about.

The absence of a review is a statement here, never a silence: a Stufe that
verifies nothing says so, and a review that was owed and did not run says
that instead.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("deepagents")

from inqtrix.agents.phase_models import DeepReviewVerdict

from tests.agents.test_kernel_deep_mode import StructuredScriptedLLM
from tests.agents.test_kernel_prompt_events import (
    _text_turn,
    make_client,
    wait_status,
)

PASSING_VERDICT = DeepReviewVerdict(
    complete=True,
    grounded=True,
    contradictions_named=True,
    findings=[],
    criteria=[],
)


def _run(client, *, overrides: dict[str, Any]) -> str:
    response = client.post(
        "/v1/runs",
        json={
            "question": "Wie ist die Lage?",
            "mode": "agent_kernel",
            "autonomy": "autonomous",
            "agent_overrides": overrides,
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    summary = wait_status(client, run_id, {"completed", "failed"}, timeout=30)
    assert summary["status"] == "completed", summary
    return run_id


def _activities(client, run_id: str, kind: str) -> list[dict[str, Any]]:
    events = client.get(
        f"/v1/runs/{run_id}/events", params={"format": "json"}
    ).json()["data"]
    return [
        item["data"]
        for item in events
        if item["type"] == "inqtrix.agent.activity"
        and item["data"].get("kind") == kind
    ]


def _outcome_reviews(client, run_id: str) -> list[str]:
    """How each outcome record of the run says its review ended."""
    events = client.get(
        f"/v1/runs/{run_id}/events", params={"format": "json"}
    ).json()["data"]
    return [
        item["data"]["review"]
        for item in events
        if item["type"] == "inqtrix.agent.outcome"
    ]


def _reviews(llm: StructuredScriptedLLM) -> list[dict[str, Any]]:
    return [
        call
        for call in llm.structured_calls
        if call["schema_name"] == "DeepReviewVerdict"
    ]


def test_the_default_stufe_is_reviewed_although_its_depth_is_normal():
    """THE regression: gruendlich promises verification and now gets it.

    The Stufe maps onto depth ``normal`` for budgets, and the kernel used
    to read that flag for its review gate — so the composer's default
    Stufe published ``verify="standard"`` and verified nothing.
    """
    llm = StructuredScriptedLLM(
        [_text_turn("Die Lage ist ruhig.")], [PASSING_VERDICT]
    )
    client = make_client(llm)
    with client:
        run_id = _run(client, overrides={"agent_tier": "gruendlich"})
        assert len(_reviews(llm)) == 1
        assert _outcome_reviews(client, run_id) == ["completed"]
        assert _activities(client, run_id, "deep_review_waived") == []


def test_a_stufe_that_verifies_nothing_says_so_instead_of_staying_silent():
    """schnell waives its review — as a statement, not as an absence.

    Without the statement a waived review and a failed one look identical
    on the wire, and a reader cannot tell "this Stufe does not verify"
    from "verification was supposed to happen".
    """
    llm = StructuredScriptedLLM([_text_turn("Kurz und knapp.")], [])
    client = make_client(llm)
    with client:
        run_id = _run(client, overrides={"agent_tier": "schnell"})
        assert _reviews(llm) == []
        waived = _activities(client, run_id, "deep_review_waived")
        assert len(waived) == 1
        assert waived[0]["verify"] == "labels"
        assert "schnell" in waived[0]["label"]
        assert _outcome_reviews(client, run_id) == ["waived"]


def test_a_request_without_a_stufe_keeps_the_legacy_depth_semantics():
    """A pre-Stufen request behaves exactly as it did before the ladder."""
    normal = StructuredScriptedLLM([_text_turn("Direkte Antwort.")], [])
    client = make_client(normal)
    with client:
        run_id = _run(client, overrides={})
        assert _reviews(normal) == []
        waived = _activities(client, run_id, "deep_review_waived")
        assert len(waived) == 1
        # No Stufe to name, so the statement names the mode instead.
        assert "Tiefmodus" in waived[0]["label"]

    deep = StructuredScriptedLLM(
        [_text_turn("Ausfuehrliche Antwort.")], [PASSING_VERDICT]
    )
    client = make_client(deep)
    with client:
        run_id = _run(client, overrides={"depth": "deep"})
        assert len(_reviews(deep)) == 1
        assert _activities(client, run_id, "deep_review_waived") == []


def test_material_that_fits_is_reviewed_in_one_call_without_a_child_run():
    """The Stufe decides WHETHER, the material decides HOW.

    A bounded role that re-reads the released documents for itself is the
    right instrument above the budget and the most expensive one below it:
    its fixed cost is paid per model turn, the single call's once.
    """
    llm = StructuredScriptedLLM(
        [_text_turn("Die Lage ist ruhig.")], [PASSING_VERDICT]
    )
    client = make_client(llm)
    with client:
        run_id = _run(client, overrides={"depth": "deep"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert [child for child in children if child.get("work_profile")] == []
        assert len(_reviews(llm)) == 1
        assert _outcome_reviews(client, run_id) == ["completed"]


def test_the_review_call_follows_the_selected_model_not_the_deep_effort():
    """The review judges the work; it must not inherit the worker's effort.

    Deep raises the kernel's reasoning effort for its own loop. The review
    node carries its own routing, so the raise stays local instead of
    quietly making every verification a high-effort call.
    """
    llm = StructuredScriptedLLM(
        [_text_turn("Die Lage ist ruhig.")], [PASSING_VERDICT]
    )
    client = make_client(llm)
    with client:
        _run(client, overrides={"depth": "deep"})
        review = _reviews(llm)[0]
        assert review["reasoning_effort"] != "high"


def test_the_single_call_review_is_booked_on_the_run():
    """A review that spends on this run's model appears in its usage.

    Only the role carried its own booking, through the child run's own
    accounting — an unbooked in-prompt review would make verification look
    free to the quota and to every cost reading built on it. Measured
    against the SAME work at a Stufe that waives the review, so the
    difference is the review call and nothing else.
    """

    def usage_at(tier: str) -> dict[str, int]:
        llm = StructuredScriptedLLM(
            [_text_turn("Die Lage ist ruhig.")], [PASSING_VERDICT]
        )
        client = make_client(llm)
        with client:
            run_id = _run(client, overrides={"agent_tier": tier})
            return client.get(f"/v1/runs/{run_id}/result").json()["usage"]

    waived, reviewed = usage_at("schnell"), usage_at("gruendlich")
    # Every scripted model call costs 7 prompt and 3 completion tokens.
    assert reviewed["prompt_tokens"] - waived["prompt_tokens"] == 7
    assert reviewed["completion_tokens"] - waived["completion_tokens"] == 3


def test_a_review_that_was_owed_and_did_not_run_is_visible(caplog):
    """The opposite of a waived review, and it must not look the same.

    Same absence of a verdict, opposite meaning: here the Stufe promised
    verification and the run could not deliver it.
    """
    llm = StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [])

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no review today")

    llm.complete_structured = refuse  # type: ignore[method-assign]
    client = make_client(llm)
    with client, caplog.at_level("WARNING", logger="inqtrix"):
        run_id = _run(client, overrides={"agent_tier": "gruendlich"})
        unavailable = _activities(client, run_id, "deep_review_unavailable")
        assert len(unavailable) == 1
        assert unavailable[0]["reason"] == "failed"
        assert _outcome_reviews(client, run_id) == ["unavailable"]
        assert _activities(client, run_id, "deep_review_waived") == []
    assert any(
        "owes a review pass" in record.getMessage() for record in caplog.records
    )

def _narrations(client, run_id: str) -> list[str]:
    events = client.get(
        f"/v1/runs/{run_id}/events", params={"format": "json"}
    ).json()["data"]
    return [
        item["data"]["text"]
        for item in events
        if item.get("data", {}).get("narration_id") == "kernel_deep_review"
    ]


def test_a_failed_review_names_no_role_that_never_ran():
    """A failure text must describe what actually happened.

    The review has two transports; naming the role in every failure
    reports a child run to a reader who never had one. Every reason also
    needs its own text: an unregistered key renders as its own name, so a
    missing one puts an identifier on the reader's screen.
    """
    from inqtrix.agents.kernel.work_profiles import (
        WORK_RESULT_BUDGET_EXHAUSTED,
        WORK_RESULT_MISSING,
        WORK_RESULT_SCHEMA_INVALID,
    )
    from inqtrix.i18n import MESSAGES

    llm = StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [])

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no review today")

    llm.complete_structured = refuse  # type: ignore[method-assign]
    client = make_client(llm)
    with client:
        run_id = _run(client, overrides={"agent_tier": "gruendlich"})
        closing = _narrations(client, run_id)[-1]
        assert "Prüfrolle" not in closing and "review role" not in closing
        assert "Prüfung abgebrochen" in closing

    # Every reason the pass can end on has a text of its own.
    reasons = {
        "failed",
        "context",
        WORK_RESULT_MISSING,
        WORK_RESULT_SCHEMA_INVALID,
        WORK_RESULT_BUDGET_EXHAUSTED,
    }
    for reason in reasons:
        assert f"kernel_review_{reason}" in MESSAGES, reason


def test_each_attached_rule_is_judged_on_its_own_and_reads_by_its_title():
    """A rule the user attached is a criterion of the review, one per rule.

    The review reads it by its marker; the run's record names it by the
    title its pill showed, in the reader's language, and the line under
    the question says the review judges rule by rule.
    """
    import asyncio

    verdict = DeepReviewVerdict.model_validate({
        "complete": True, "grounded": True, "contradictions_named": True,
        "findings": [], "not_checked": [],
        "criteria": [{"index": 1, "met": False, "kind": "output_requirement",
                      "gap": "Abschnitt 12 fehlt."}],
    })
    llm = StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [verdict])
    client = make_client(llm)
    with client:
        rule = asyncio.run(client.container.prompt_template_service.create(
            {"title": "Executive Briefing", "label": "briefing", "category": "instruction",
             "content_markdown": "Gliedere in zwölf Abschnitte.", "visibility": {"agent": True}},
            tenant_id="default", owner_user_id=None,
        ))
        response = client.post("/v1/runs", json={
            "question": "Wie ist die Lage?", "mode": "agent_kernel", "autonomy": "autonomous",
            "agent_overrides": {"agent_tier": "gruendlich"}, "report_rule_ids": [rule.id],
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"}, timeout=30)
        assert summary["status"] == "completed", summary
        [review] = _reviews(llm)
        assert "Attached rule 1 of 1, [Rule: briefing] at revision 1" in review["prompt"]
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
        [attached] = [item["data"] for item in events if item["type"] == "inqtrix.agent.requirement.attached"]
        assert attached["review"] == "per_rule"
        lines = [
            line["text"]
            for item in events if item["type"] == "inqtrix.agent.outcome"
            for line in item["data"]["lines"]
        ]
        assert "Kriterium offen: Regel „Executive Briefing“ (Rev. 1) — Abschnitt 12 fehlt." in lines
