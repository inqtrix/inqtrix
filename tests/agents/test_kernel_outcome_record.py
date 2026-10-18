"""The outcome record of a conversational kernel run (``inqtrix.agent.outcome``).

The kernel reports its review in the mission's review-report shape, through
the same builder: one record per segment that leaves the algorithm with a
decision, none for a park and none for a role run. Every exit is pinned on
the real run path with a scripted model; the parked review stands beside
the role tests (``test_kernel_work_profiles.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("deepagents")

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import ArtifactRevisionConflict
from inqtrix.agents.kernel import algorithm as kernel_algorithm
from inqtrix.agents.kernel import review_pass
from inqtrix.exceptions import AgentCancelled
from inqtrix.i18n import t
from inqtrix.runs.shared import ANSWER_DRAFT_EVENT, CHILD_PROGRESS_EVENT, OUTCOME_EVENT

from tests.agents.test_kernel_deep_mode import (
    CanvasReviewLLM,
    StructuredScriptedLLM,
    _canvas_turn,
    _deep_run_over_a_report,
    _report_read_turn,
    _seeded_report,
)
from tests.agents.test_kernel_prompt_events import _text_turn, make_client, wait_status
from tests.agents.test_review_pass import unquoted
from tests.agents.test_kernel_walking_skeleton import (
    ScriptedToolLLM as SkeletonLLM,
    _answer_turn,
    _ask_turn,
    _two_read_calls_turn,
    make_kernel_client,
)

FINDING = {
    "target": "chat", "artifact_id": "", "finding": "Die Antwort braucht eine Gliederung.",
    "kind": "output_requirement", "quote": "Erste Antwort.", "basis_ids": [],
}
HEADING_EDIT = {"edits": [{"finding_id": "finding_1", "replacement": "## Lage\n\nErste Antwort."}]}
REPORT = {
    "answer": "Die Lage ist ruhig [E1].",
    "references": [{"label": "E1", "url": "https://example.org/lage"}],
}
"""A research report a run reads first: its source enters the run's ledger,
so the run's review answers to evidence and re-checks its applied edits (a
run without any source is reviewed from general knowledge, never re-checked)."""
NOT_RECHECKED = unquoted("finding_1")
"""The re-check of the heading edit: it quotes no basis, so the edit stays unverified."""


def _verdict(*findings: dict[str, Any], **flags: bool) -> dict[str, Any]:
    return {
        "complete": True, "grounded": True, "contradictions_named": True,
        "findings": list(findings), **flags,
    }


def _events(client, run_id: str) -> list[dict[str, Any]]:
    return client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]


def _records(client, run_id: str) -> list[dict[str, Any]]:
    return [event["data"] for event in _events(client, run_id) if event["type"] == OUTCOME_EVENT]


def _finished(client, overrides: dict[str, Any], **body: Any) -> str:
    """Submit one kernel run and wait for its terminal status, whichever it is."""
    response = client.post("/v1/runs", json={
        "question": "Wie ist die Lage?", "mode": "agent_kernel", "autonomy": "autonomous",
        "agent_overrides": overrides, **body,
    })
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    wait_status(client, run_id, {"completed", "failed", "cancelled"}, timeout=30)
    return run_id


def _answered(client, option_id: str = "") -> str:
    """A run parked at its first gate, answered with *option_id* (a free
    answer without one), to its end."""
    run_id = client.post(
        "/v1/runs", json={"question": "Auftrag.", "mode": "agent_kernel"}
    ).json()["run_id"]
    wait_status(client, run_id, {"waiting_for_input"})
    [row] = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"]
    answer = {"option_id": option_id} if option_id else {"answer": "Egal."}
    response = client.post(
        f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}", json=answer,
    )
    assert response.status_code == 200, response.text
    wait_status(client, run_id, {"completed", "failed", "cancelled"})
    return run_id


# -- no review, one record ------------------------------------------------------ #


def test_a_waived_run_records_its_review_as_waived():
    """A Stufe that waives the review still ends with a record, and it says
    the Stufe skipped the review instead of reading it as not run, in the
    language of the run's question."""
    client = make_client(StructuredScriptedLLM([_text_turn("Kurz und knapp.")], []))
    with client:
        records = _records(client, _finished(client, {"agent_tier": "schnell"}))
    assert [(r["review"], r["revision"], r["round"], r["summary"]) for r in records] == [
        ("waived", "none", 0, "Ohne Prüfung (Stufe)"),
    ]


def test_a_request_without_a_stufe_records_no_waiver():
    """A request without a Stufe skips the review by its legacy depth rule:
    no Stufe waived it, so the record never names one and reads the review
    as not run. The server's own words; a reword must be caught here."""
    client = make_client(StructuredScriptedLLM([_text_turn("Direkte Antwort.")], []))
    with client:
        records = _records(client, _finished(client, {}))
    assert [(r["review"], r["revision"], r["round"], r["summary"]) for r in records] == [
        ("not_run", "none", 0, "Ohne Prüfung beendet"),
    ]


# -- one record per exit -------------------------------------------------------- #


class _CancelledTurnLLM(StructuredScriptedLLM):
    """A cancel at the model boundary: the loop's first model call stops."""

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> Any:
        raise AgentCancelled("Run cancelled.")


class _RevisionCancelledLLM(StructuredScriptedLLM):
    """A cancel that reaches the revision call after the review located its finding."""

    def complete_structured(self, prompt: str, *, schema: Any, schema_name: str, **kwargs: Any) -> Any:
        if schema_name == "DeepRevisionBundle":
            raise AgentCancelled("Cancelled while revising.")
        return super().complete_structured(prompt, schema=schema, schema_name=schema_name, **kwargs)


class _RecheckCancelledLLM(StructuredScriptedLLM):
    """A cancel that reaches the re-check of the edit the revision applied."""

    def complete_structured(self, prompt: str, *, schema: Any, schema_name: str, **kwargs: Any) -> Any:
        if schema_name == "RevisionCheckVerdict":
            raise AgentCancelled("Cancelled while re-checking.")
        return super().complete_structured(prompt, schema=schema, schema_name=schema_name, **kwargs)


class _RepairCancelledLLM(StructuredScriptedLLM):
    """A cancel that reaches the bounded citation repair: the draft's, before
    the review (``at=1``), or the published answer's, after the review
    decided (``at=2``)."""

    def __init__(self, turns: list[Any], structured: list[Any], *, at: int) -> None:
        super().__init__(turns, structured)
        self.at = at
        self.repairs = 0

    def complete_structured(self, prompt: str, *, schema: Any, schema_name: str, **kwargs: Any) -> Any:
        if schema_name == "CitationRepairText":
            self.repairs += 1
            if self.repairs == self.at:
                raise AgentCancelled("Cancelled while repairing citations.")
        return super().complete_structured(prompt, schema=schema, schema_name=schema_name, **kwargs)


def _model_boundary_cancel(monkeypatch) -> tuple[Any, Any]:
    client = make_client(_CancelledTurnLLM([], []))
    return client, lambda: _finished(client, {"agent_tier": "schnell"})


def _token_budget(monkeypatch) -> tuple[Any, Any]:
    # The first segment books 52 tokens and parks; the resumed one stops at
    # its first model boundary on the 30-token cap.
    client = make_kernel_client(SkeletonLLM([_ask_turn(), _answer_turn()]), max_tokens_per_run=30)
    return client, lambda: _answered(client)


def _revision_cancelled(monkeypatch) -> tuple[Any, Any]:
    client = make_client(_RevisionCancelledLLM(
        [_text_turn("Erste Antwort.")], [_verdict(FINDING, complete=False)],
    ))
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _recheck_cancelled(monkeypatch) -> tuple[Any, Any]:
    llm = _RecheckCancelledLLM([], [_verdict(FINDING, complete=False), HEADING_EDIT])
    client = make_client(llm)

    def run() -> str:
        origin = _seeded_report(client, REPORT)
        llm._turns = [_report_read_turn(origin), _text_turn("Erste Antwort.")]
        return _finished(client, {"agent_tier": "gruendlich"}, report_ids=[origin])

    return client, run


def _archive_fails_after_a_revision(monkeypatch) -> tuple[Any, Any]:
    client = make_client(StructuredScriptedLLM(
        [_text_turn("Erste Antwort.")],
        [_verdict(FINDING, complete=False), HEADING_EDIT, NOT_RECHECKED],
    ))

    async def archive_down(**kwargs: Any) -> Any:
        raise RuntimeError("archive store down")

    monkeypatch.setattr(
        client.container.agent_control_service.store, "put_context_archive", archive_down,
    )
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _checkpoint_delete_fails(monkeypatch) -> tuple[Any, Any]:
    client = make_client(StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [_verdict()]))

    def delete_down(run_id: str) -> None:
        raise RuntimeError("checkpoint store down")

    saver = client.container.registry.get("agent_kernel")._checkpointer
    monkeypatch.setattr(saver, "delete_thread", delete_down)
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _graph_error(monkeypatch) -> tuple[Any, Any]:
    # A turn with neither text nor a tool call fails inside the graph.
    client = make_client(StructuredScriptedLLM([_text_turn("")], []))
    return client, lambda: _finished(client, {"agent_tier": "schnell"})


def _no_answer_text(monkeypatch) -> tuple[Any, Any]:
    # The loop ends, but its last AI message holds no text a reader can use.
    client = make_client(StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], []))
    monkeypatch.setattr(kernel_algorithm, "_final_answer", lambda messages: "")
    return client, lambda: _finished(client, {"agent_tier": "schnell"})


def _limit_cancel(monkeypatch) -> tuple[Any, Any]:
    client = make_kernel_client(SkeletonLLM([_two_read_calls_turn()]), max_tool_calls=1)
    return client, lambda: _answered(client, "cancel")


def _limit_partial(monkeypatch) -> tuple[Any, Any]:
    client = make_kernel_client(SkeletonLLM([_two_read_calls_turn()]), max_tool_calls=1)
    return client, lambda: _answered(client, "partial")


def _limit_archive_fails(monkeypatch) -> tuple[Any, Any]:
    # The park archives nothing (``_answered`` waits for it); the decided
    # choice archives the transcript first, and that write fails.
    client = make_kernel_client(SkeletonLLM([_two_read_calls_turn()]), max_tool_calls=1)

    async def archive_down(self: Any, **kwargs: Any) -> Any:
        raise RuntimeError("archive store down")

    monkeypatch.setattr(MemoryAgentControlStore, "put_context_archive", archive_down)
    return client, lambda: _answered(client, "cancel")


def _cancel_in_the_draft_check(monkeypatch) -> tuple[Any, Any]:
    # The answer cites a label no source carries, so the bounded citation
    # repair runs on the draft before the review; the cancel reaches it there.
    client = make_client(_RepairCancelledLLM(
        [_text_turn("Die Lage ist ruhig [K9].")], [_verdict()], at=1,
    ))
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _cancel_after_the_review(monkeypatch) -> tuple[Any, Any]:
    # The bounded citation repair runs on the draft and again after the
    # passing review; the cancel reaches the second.
    client = make_client(_RepairCancelledLLM(
        [_text_turn("Die Lage ist ruhig [K9].")],
        [{"markdown": "Die Lage ist ruhig."}, _verdict()], at=2,
    ))
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _revision_error(monkeypatch) -> tuple[Any, Any]:
    client = make_client(StructuredScriptedLLM(
        [_text_turn("Erste Antwort.")], [_verdict(FINDING, complete=False), HEADING_EDIT],
    ))

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("edit rule broke")

    monkeypatch.setattr(review_pass, "apply_review_edits", broken)
    return client, lambda: _finished(client, {"agent_tier": "gruendlich"})


def _reasons(record: dict[str, Any]) -> list[list[str]]:
    return [line["reasons"] for line in record["lines"]]


@pytest.mark.parametrize(
    ("setup", "final", "field", "value"),
    [
        pytest.param(_model_boundary_cancel, "cancelled", "review", "not_run",
                     id="model-boundary-cancel"),
        pytest.param(_token_budget, "failed", "review", "not_run", id="token-budget"),
        # A located finding the revision never decided is named by the cancel.
        pytest.param(_revision_cancelled, "cancelled", "lines", [["cancelled"]],
                     id="revision-cancelled"),
        # ... and by a cancel during the re-check of the edit it applied.
        pytest.param(_recheck_cancelled, "cancelled", "lines", [["cancelled"]],
                     id="recheck-cancelled"),
        pytest.param(_archive_fails_after_a_revision, "failed", "revision", "unverified",
                     id="archive-fails-after-a-revision"),
        pytest.param(_checkpoint_delete_fails, "completed", "review", "completed",
                     id="checkpoint-delete-fails"),
        pytest.param(_graph_error, "failed", "review", "not_run", id="graph-error"),
        pytest.param(_no_answer_text, "failed", "review", "not_run", id="no-answer-text"),
        pytest.param(_limit_cancel, "cancelled", "review", "not_run", id="limit-cancel"),
        pytest.param(_limit_partial, "completed", "review", "not_run", id="limit-partial"),
        pytest.param(_limit_archive_fails, "failed", "review", "not_run",
                     id="limit-archive-fails"),
        # ... and by the error that stopped the revision.
        pytest.param(_revision_error, "failed", "lines", [["revision_error"]],
                     id="revision-error"),
        # A cancel that leaves the post-loop after the review keeps its verdict;
        # one that reaches the draft's check left the review unrun.
        pytest.param(_cancel_after_the_review, "cancelled", "review", "completed",
                     id="cancel-after-the-review"),
        pytest.param(_cancel_in_the_draft_check, "cancelled", "review", "not_run",
                     id="cancel-in-the-draft-check"),
    ],
)
def test_every_kernel_exit_sends_one_record(monkeypatch, setup, final, field, value):
    """Each exit sends exactly one record with the kernel's own decision,
    and the record carries what the review decided by then."""
    client, start = setup(monkeypatch)
    with client:
        records = _records(client, start())
    assert [
        (record["final"], _reasons(record) if field == "lines" else record[field])
        for record in records
    ] == [(final, value)]


def test_a_run_cancelled_in_its_review_keeps_the_draft_it_showed(monkeypatch):
    """The reader keeps what they read: a cancel during the review ends the
    run cancelled, with the draft it showed and no published answer."""
    client, start = _recheck_cancelled(monkeypatch)
    with client:
        run_id = start()
        status = client.get(f"/v1/runs/{run_id}").json()["status"]
        events = _events(client, run_id)
    assert (
        status,
        [event["data"]["markdown"] for event in events if event["type"] == ANSWER_DRAFT_EVENT],
        [event["type"] for event in events if event["type"] == "inqtrix.answer.started"],
    ) == ("cancelled", ["Erste Antwort."], [])


def test_a_cancel_in_the_draft_check_ends_like_a_cancel_in_the_review(monkeypatch):
    """The draft's check belongs to the review gate: a cancel there keeps the
    run's transcript, as a cancel in the review does, and returns the
    segment's spend for booking instead of leaving through an error."""
    client, start = _cancel_in_the_draft_check(monkeypatch)
    with client:
        run_id = start()
        status = client.get(f"/v1/runs/{run_id}").json()["status"]
        archives = asyncio.run(
            client.container.agent_control_service.store.list_context_archive_sources(
                run_id, include_private=True,
            )
        )
    assert (status, bool(archives)) == ("cancelled", True)


# -- the review's state in the record ------------------------------------------- #


def test_an_owed_review_that_failed_reads_unavailable():
    """The Stufe owed a review and none came: never read as not run."""
    llm = StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [])

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no review today")

    llm.complete_structured = refuse  # type: ignore[method-assign]
    client = make_client(llm)
    with client:
        records = _records(client, _finished(client, {"agent_tier": "gruendlich"}))
    assert [(r["review"], r["round"]) for r in records] == [("unavailable", 1)]


def test_an_inconsistent_verdict_reads_like_the_missions():
    """A negative verdict that names nothing is no usable review, in both engines."""
    client = make_client(StructuredScriptedLLM(
        [_text_turn("Die Lage ist ruhig.")], [_verdict(complete=False)],
    ))
    with client:
        records = _records(client, _finished(client, {"agent_tier": "gruendlich"}))
    assert [r["review"] for r in records] == ["unavailable"]


@pytest.mark.parametrize(("failure", "reason"), [
    pytest.param(ArtifactRevisionConflict(2), "write_conflict", id="write-conflict"),
    pytest.param(RuntimeError("artifact store down"), "store_error", id="store-error"),
])
def test_a_write_failure_after_a_held_recheck_names_the_findings(monkeypatch, failure, reason):
    """The whole revision stays discarded: the finding whose edit the
    re-check held is named with the write failure as its reason, and an
    edit that was never written held nothing.

    A heading never holds by the server's table, so the settlement is
    stubbed to hold: the subject is what a write failure does to a held
    edit, not how an edit comes to hold."""
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)
    settled: list[str] = []

    def holds(*args: Any, **kwargs: Any) -> tuple[str, str]:
        settled.append("holds")
        return "holds", ""

    monkeypatch.setattr(review_pass, "settle_check", holds)
    llm = CanvasReviewLLM([], [])
    client = make_client(llm)

    async def fail(**kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(
        client.container.agent_control_service.store, "revise_session_artifacts_atomically",
        fail,
    )
    with client:
        origin = _seeded_report(client, REPORT)
        llm._turns = [
            _report_read_turn(origin), _canvas_turn(), _text_turn("Chat bleibt unveraendert."),
        ]
        records = _records(client, _finished(
            client, {"depth": "deep"},
            question="Erstelle und pruefe ein Canvas.", session_id="sess-outcome-write-failure",
            report_ids=[origin],
        ))
    # Settled twice: a hold takes the check and its confirmation.
    assert ([(r["revision"], _reasons(r)) for r in records], settled) == (
        [("discarded", [[reason]])], ["holds", "holds"],
    )


# -- who sends, and what a line may carry ---------------------------------------- #


def test_a_role_run_sends_no_record(monkeypatch):
    """A role is a child run; a record of its own would cross to the parent
    as the parent's. Driven through the role's graph error, an exit that
    sends for a conversational run."""
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)
    # Without a scripted verdict the role's model call fails inside its graph.
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort.")], [])
    client = make_client(llm)
    with client:
        run_id = _finished(client, {"depth": "deep"})
        [role] = [
            child for child in client.get(f"/v1/runs/{run_id}/children").json()["data"]
            if child.get("work_profile")
        ]
        role_status = client.get(f"/v1/runs/{role['run_id']}").json()["status"]
        role_events = _events(client, role["run_id"])
        parent_events = _events(client, run_id)
    # A role that failed before its graph ran would read ``failed`` too and
    # send nothing whatever the gate does; the one model call pins the exit.
    assert (role_status, len(llm.structured_calls)) == ("failed", 1), (
        "precondition: the role failed at its own model call inside its graph"
    )
    assert (
        [event for event in role_events if event["type"] == OUTCOME_EVENT],
        [
            event["data"] for event in parent_events
            if event["type"] == CHILD_PROGRESS_EVENT and {"review", "revision"} & set(event["data"])
        ],
    ) == ([], [])


def test_kernel_record_lines_pass_the_reader_guard():
    """A finding's prose names a collection the run read; the review's reader
    names leave that id standing, so only the run's reader guard can take it
    out of the record."""
    llm = StructuredScriptedLLM([], [_verdict(
        {**FINDING, "finding": "Die Sammlung kc_guard01 fehlt in der Antwort.",
         "quote": "Diesen Satz gibt es nicht."},
        complete=False,
    )])
    payload = {
        "answer": "Fünf Jahre bei Registrierung [E1].",
        "references": [{
            "label": "E1", "url": "https://example.org/warranty", "title": "Garantie",
            "collection_id": "kc_guard01",
        }],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(
            client, llm, payload, "Fünf Jahre Garantie [W1].", "sess-outcome-guard",
        )
        events = _events(client, run_id)
    [detail] = [
        event["data"]["detail"] for event in events
        if event["type"] == "inqtrix.agent.activity"
        and event["data"].get("kind") == "critic_revision_discarded"
    ]
    assert "kc_guard01" in detail, "precondition: the id survives the review's reader names"
    [record] = [event["data"] for event in events if event["type"] == OUTCOME_EVENT]
    assert [line["text"] for line in record["lines"]] == [
        detail.replace("kc_guard01", t({"language": "de"}, "agent_reader_identifier_removed")),
    ]
