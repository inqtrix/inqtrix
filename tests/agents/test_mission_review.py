"""The mission's side of the shared review pass.

The mission measures, the review judges, the mission decides. These tests pin
the mission adapter at its own seams: what it hands over, how it waits for a
review role without repeating itself, that every research round gets its own
review, and that a review which cannot run leaves the memo standing, visibly.
The pass itself is pinned in ``test_review_pass``; whole-run behaviour in
``test_workspace_agent``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents import algorithm, critic_phase
from inqtrix.agents.kernel import review_pass
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.kernel.tools import ChildScope
from inqtrix.agents.research_prospect import Prospect
from inqtrix.agents.scheduler import TaskOutcome

from tests.agents.test_review_pass import CallLLM, _verdict, unquoted

UNQUOTED = unquoted("finding_1")
"""The re-check of the one applied edit: it quotes no basis, so the edit
stays applied and unverified."""

MEMO = "# Memo\n\n## Lage\n\nDie Frist beträgt 30 Tage [K1].\n\n## Grenze\n\nFehler."
REFERENCE = {
    "label": "K1", "document_id": "doc_1", "chunk_index": 4, "title": "Vertrag",
    "excerpt": "Die Frist beträgt 30 Tage.",
}


def _diagnostics(**kwargs: Any) -> dict[str, dict[str, Any]]:
    items = critic_phase.review_diagnostics(
        run_id="run_m",
        language="de",
        coverage={"paragraphs": 3, "cited_paragraphs": 3, "labels_used": 2},
        quote_checks=[],
        contradictions=[],
        memo_markdown="# Memo",
        **kwargs,
    )
    return {item["basis_id"].rsplit(":", 1)[1]: item for item in items}


# -- the mission measures ---------------------------------------------------------


def test_every_measurement_is_a_diagnostic_with_a_basis_id_and_its_method():
    diagnostics = _diagnostics()
    assert set(diagnostics) == {"text_measurements", "quote_checks", "contradictions"}
    for name, item in diagnostics.items():
        assert item["basis_id"] == f"diagnostic:run_m:{name}"
        assert item["source_run_id"] == "run_m"
        assert item["method"].strip()


def test_every_mission_diagnostic_carries_a_reader_title():
    """The review addresses a diagnostic by basis_id and names it by title;
    a title that fell back to its own message key would say nothing."""
    diagnostics = _diagnostics(
        execution={"plan_version": 1, "tasks": []},
        task_evidence_notes="Task t1: Konfidenz 6/10",
    )
    assert {name: item["title"] for name, item in diagnostics.items()} == {
        "text_measurements": "Textmaße der Ausgabe",
        "quote_checks": "Zitatprüfung der Ausgabe",
        "contradictions": "Bekannte Widersprüche des Laufs",
        "execution": "Plan- und Aufgabenstand des Laufs",
        "task_evidence_notes": "Belegnotizen der Teilaufgaben",
    }


def test_the_review_receives_the_measured_length_and_the_unverified_passage():
    items = critic_phase.review_diagnostics(
        run_id="run_m", language="de", coverage={}, contradictions=[],
        memo_markdown="# Kurz\n\nDer Text ist knapp. [W1]",
        quote_checks=[{"quote": "Originaltext ist hier nicht gespeichert.", "verified": False}],
    )
    by_name = {item["basis_id"].rsplit(":", 1)[1]: item for item in items}
    assert by_name["text_measurements"]["values"]["word_count"] == 6
    assert "including title, headings, evidence labels and code" in by_name["text_measurements"]["method"]
    assert "claim no other word count" in by_name["text_measurements"]["method"]
    assert by_name["quote_checks"]["values"]["not_verified"] == [
        "Originaltext ist hier nicht gespeichert."
    ]
    assert "a missing excerpt refutes neither the quote nor the fact" in by_name["quote_checks"]["method"]


def test_the_child_evidence_notes_are_a_diagnostic_only_when_there_are_any():
    note = "Task t1: Evidenzlage needs_review (93 % Einzelquellen), Konfidenz 6/10"
    assert "task_evidence_notes" not in _diagnostics()
    assert _diagnostics(task_evidence_notes=note)["task_evidence_notes"]["values"] == {"notes": note}


def test_the_execution_record_keeps_failed_and_skipped_states_visible():
    execution = {"plan_version": 3, "tasks": [
        {"task_id": "a", "tool_kind": "web_instant", "status": "failed"},
        {"task_id": "b", "tool_kind": "synthesis", "status": "skipped"},
    ]}
    item = _diagnostics(execution=execution)["execution"]
    assert item["values"] == execution
    assert "never against the output's description of it" in item["method"]
    assert "no judgement of content quality" in item["method"]


def test_a_contradiction_is_handed_over_with_both_positions():
    items = critic_phase.review_diagnostics(
        run_id="run_m", language="de", coverage={}, quote_checks=[],
        memo_markdown="Intern gilt eine Frist von 30 Tagen, extern werden 14 genannt.",
        contradictions=[{
            "internal_position": "Intern gilt eine Frist von 30 Tagen",
            "external_position": "Eine externe Quelle nennt eine andere Zahl",
        }, {"internal_position": "", "external_position": ""}],
    )
    known = items[2]["values"]["known"]
    assert [item["addressed_in_output"] for item in known] == [True, False]
    assert known[0]["external_position"] == "Eine externe Quelle nennt eine andere Zahl"


@pytest.mark.parametrize(
    "memo, anchors, unanchored",
    [
        pytest.param(
            'Analyse: "Die Marktlage bleibt unerwartet dynamisch" [W1].',
            ['"Die Marktlage bleibt unerwartet dynamisch"'], [], id="unique-quote-with-its-marks",
        ),
        pytest.param(
            'Erstens: "Die Marktlage bleibt unerwartet dynamisch" [W1].\n\n'
            'Zweitens: "Die Marktlage bleibt unerwartet dynamisch" [W1].',
            ['Erstens: "Die Marktlage bleibt unerwartet dynamisch" [W1].',
             'Zweitens: "Die Marktlage bleibt unerwartet dynamisch" [W1].'], [],
            id="repeated-quote-is-anchored-by-its-paragraphs",
        ),
        pytest.param(
            'Analyse: "Die Marktlage bleibt unerwartet dynamisch" [W1].\n\n'
            'Analyse: "Die Marktlage bleibt unerwartet dynamisch" [W1].',
            [], ["Die Marktlage bleibt unerwartet dynamisch"],
            id="identical-paragraphs-stay-open",
        ),
    ],
)
def test_an_unverified_web_quote_is_anchored_where_an_edit_can_replace_it(
    memo, anchors, unanchored
):
    findings, open_quotes = critic_phase.unverified_web_quote_findings(
        memo,
        [{"quote": "Die Marktlage bleibt unerwartet dynamisch", "verified": False}],
        target="artifact", artifact_id="art_memo",
        basis_id=critic_phase.quote_check_basis("run_m"),
        finding_text="Ersetzen.",
    )
    assert [item.quote for item in findings] == anchors
    assert all(item.kind == "run_diagnostic" for item in findings)
    assert all(item.basis_ids == ["diagnostic:run_m:quote_checks"] for item in findings)
    assert open_quotes == unanchored


# -- the ledger a review role re-reads -------------------------------------------------


def test_a_mission_without_web_searches_stores_its_ledger(monkeypatch):
    """A knowledge-only mission has evidence too; its review role reads it
    from the stored ledger exactly like that of a web mission."""
    stored: list[dict[str, Any]] = []

    async def upsert_artifact(**kwargs: Any) -> None:
        stored.append(kwargs)

    deps = SimpleNamespace(
        cancelled=lambda: False,
        control=SimpleNamespace(upsert_artifact=upsert_artifact),
        context=SimpleNamespace(run_id="run_00knowledge1", strategies=SimpleNamespace()),
        emit=lambda *args: None,
    )
    monkeypatch.setattr(algorithm, "_deps", lambda: deps)
    monkeypatch.setattr(algorithm, "_set_phase", lambda *args: None)
    outcome = TaskOutcome(status="completed", summary="ok", evidence=[dict(REFERENCE)])
    state = algorithm._node_evidence(
        {"outcomes": algorithm._outcomes_to_state({"t1": outcome})}
    )
    assert state["web_search_ledger"].get("searches") in (None, {})
    assert [item["kind"] for item in stored] == ["evidence_bundle"]
    # The id a review role derives from the run it was handed.
    assert stored[0]["artifact_id"] == "art_00knowledge1_evidence"
    assert [ref["label"] for ref in stored[0]["refs"]] == ["K1"]


# -- the mission decides -----------------------------------------------------------------


class RoleStore:
    """Run store of a mission whose review runs as a role child."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}

    def children(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.rows)

    def result(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
        return self.results[run_id]


REACHABLE = Prospect(reachable=True, branches=("web",), codes=(), inputs={})
"""A prospect with an open branch: the harness's deps hold no source facts,
so the review route reads this one unless a test names another."""


def _mission(
    monkeypatch, *, llm: Any = None, store: RoleStore | None = None,
    executes_kernel: bool = True, tier_policy: Any = None,
    prospect: Prospect = REACHABLE,
) -> tuple[SimpleNamespace, list[tuple[str, dict[str, Any]]], list[str]]:
    events: list[tuple[str, dict[str, Any]]] = []
    flushed: list[str] = []
    store = store or RoleStore()

    async def get_plan(run_id: str):
        return SimpleNamespace(version=1, status="approved"), []

    async def list_clarifications(run_id: str):
        return []

    def child_scope(state: dict[str, Any]) -> ChildScope:
        return ChildScope(
            run_id="run_m", run_service=SimpleNamespace(run_store=store), resolver=object(),
            principal=None, workspace_id="ws", session_id=str(state.get("session_id") or ""),
            stack_name="", source_policy=None, model_overrides={},
            knowledge_collection_ids=None, knowledge_scope_explicit=False, knowledge_scope=None,
            tier="tief", depth="deep", report_requirement="", report_rules=(),
        )

    deps = SimpleNamespace(
        cancelled=lambda: False,
        control=SimpleNamespace(get_plan=get_plan, list_clarifications=list_clarifications),
        context=SimpleNamespace(run_id="run_m"),
        platform=SimpleNamespace(max_replan_rounds=2, mission_time_budget_seconds=0),
        llm=llm if llm is not None else CallLLM([]),
        timeout=1.0, visible_to=None, tier_policy=tier_policy,
        resolved=lambda node: (f"{node}-model", "high"),
        resolver=SimpleNamespace(executes=lambda mode: executes_kernel),
        child_scope=child_scope,
        emit=lambda name, data: events.append((name, data)),
    )
    monkeypatch.setattr(algorithm, "_deps", lambda: deps)
    monkeypatch.setattr(algorithm, "_set_phase", lambda state, phase: state.__setitem__("phase", phase))
    monkeypatch.setattr(algorithm, "_skills_prompt_block", lambda *args: "")
    monkeypatch.setattr(
        algorithm, "_flush_deliverable",
        lambda deps, state, body, *, status: flushed.append(body),
    )
    monkeypatch.setattr(
        algorithm, "_research_prospect",
        lambda deps, state, *, plan, tasks, route: prospect,
    )
    return deps, events, flushed


def _state(**overrides: Any) -> dict[str, Any]:
    return {
        "question": "Welche Fristen gelten?", "language": "de",
        "memo_markdown": MEMO, "memo_title": "Memo", "artifact_id": "art_memo",
        "memo_base_revision": 2, "session_id": "session_1",
        "references": [dict(REFERENCE)], "success_criteria": ["Frist genannt"],
        "replan_rounds": 0, **overrides,
    }


def _kinds(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data.get("kind") or data.get("tool") or name for name, data in events]


TEXT_MEASUREMENTS = "diagnostic:run_m:text_measurements"


def _diagnostic_finding(quote: str, **overrides: Any) -> dict[str, Any]:
    """A finding on the memo that rests on the mission's text measurements."""
    return {
        "target": "artifact", "artifact_id": "art_memo", "kind": "run_diagnostic",
        "finding": "Konkret korrigieren.", "quote": quote, "basis_ids": [TEXT_MEASUREMENTS],
        **overrides,
    }


def _force_role(monkeypatch) -> list[dict[str, Any]]:
    """Make the material too large for one call and record role submissions."""
    submitted: list[dict[str, Any]] = []
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)

    def submit(scope: Any, **kwargs: Any) -> dict[str, Any]:
        rows = scope.run_service.run_store.rows
        known = next((row for row in rows if row["origin_key"] == kwargs["origin_key"]), None)
        if known is not None:
            # Idempotent like the real seam: the origin key finds the child.
            return known
        submitted.append(kwargs)
        row = {"run_id": f"run_role_{len(submitted)}", "status": "running",
               "origin_key": kwargs["origin_key"]}
        rows.append(row)
        return row

    monkeypatch.setattr(review_pass, "submit_or_find_child", submit)
    return submitted


def test_small_material_is_reviewed_in_one_call_under_the_shared_review_node(monkeypatch):
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    _, _events, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state())
    assert llm.calls[0]["model"] == "agent_deep_review-model"
    assert state["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    assert state["critic"]["verdict"] == "pass"
    assert state["critic"]["transport"] == "prompt"
    assert state["critic"]["criteria_covered"] == ["Frist genannt"]
    assert flushed == []
    # A single call has no reader: the memo travels verbatim, the knowledge
    # source with its basis id.
    prompt = llm.calls[0]["prompt"]
    assert "Die Frist beträgt 30 Tage [K1]." in prompt
    assert f"source:{KernelDeps.ledger_id(REFERENCE)}" in prompt
    assert "1. Frist genannt" in prompt


def test_large_material_goes_to_the_role_and_the_mission_waits_without_repeating_itself(monkeypatch):
    """The wait is the children wait: park, and find the SAME child on every
    re-entry — one submission, one role row, one announced phase."""
    submitted = _force_role(monkeypatch)
    _, events, _ = _mission(monkeypatch)
    state = algorithm._node_critic(_state())
    assert state["review_pending"] is True
    assert "critic" not in state or state.get("critic") is None
    first = list(events)
    assert [kwargs["origin_key"] for kwargs in submitted] == ["role-review:1"]
    handover = submitted[0]["handover"]
    assert submitted[0]["work_profile"] == "review"
    assert handover.evidence_run_id == "run_m"
    assert handover.criteria == ("Frist genannt",)
    # A session canvas travels as an address; the role reads it itself.
    assert [(item.kind, item.artifact_id, item.revision, item.text) for item in handover.outputs] == [
        ("canvas", "art_memo", 2, ""),
    ]
    assert {item["basis_id"] for item in handover.diagnostics} >= {
        "diagnostic:run_m:text_measurements", "diagnostic:run_m:execution",
    }
    # A wake that finds the role still running parks again: nothing is
    # submitted or announced a second time.
    again = algorithm._node_critic(state)
    assert again["review_pending"] is True
    assert len(submitted) == 1
    started = [data for name, data in events if name.endswith("tool.started")]
    assert len(started) == 1 and started[0]["invocation_id"] == "role-review:1"
    assert len(events) == len(first)


def test_the_wait_node_parks_as_a_children_wait(monkeypatch):
    import langgraph.types

    parked: list[Any] = []
    monkeypatch.setattr(langgraph.types, "interrupt", parked.append)
    _mission(monkeypatch)
    state = _state(review_pending=True)
    assert algorithm._node_review_wait(state) is state
    assert parked == [{"kind": "children"}]


def test_a_cancelled_mission_does_not_park_on_its_review(monkeypatch):
    import langgraph.types

    monkeypatch.setattr(
        langgraph.types, "interrupt",
        lambda value: (_ for _ in ()).throw(AssertionError("must not park")),
    )
    deps, _, _ = _mission(monkeypatch)
    deps.cancelled = lambda: True
    assert algorithm._node_review_wait(_state()) == {"cancelled": True}


def test_every_research_round_gets_a_review_of_its_own(monkeypatch):
    """The memo after more research is another memo: an earlier verdict must
    not settle it."""
    submitted = _force_role(monkeypatch)
    _mission(monkeypatch)
    algorithm._node_critic(_state(replan_rounds=0))
    algorithm._node_critic(_state(replan_rounds=1))
    assert [kwargs["origin_key"] for kwargs in submitted] == ["role-review:1", "role-review:2"]


def test_a_memo_without_a_session_or_below_a_user_edit_travels_verbatim(monkeypatch):
    """By address only when a reader can open exactly the text under review."""
    submitted = _force_role(monkeypatch)
    _mission(monkeypatch)
    algorithm._node_critic(_state(session_id=""))
    algorithm._node_critic(_state(replan_rounds=1, memo_user_prefix="Notiz der Nutzerin."))
    assert [kwargs["handover"].outputs[0].text for kwargs in submitted] == [MEMO, MEMO]


def _finished_role(store: RoleStore, *, status: str, work_result: dict[str, Any] | None) -> None:
    store.rows.append({"run_id": "run_role", "status": status, "origin_key": "role-review:1",
                       "error": {"message": "provider down"}})
    store.results["run_role"] = {"work_result": work_result}


@pytest.mark.parametrize(
    "status, work_result, reason",
    [
        pytest.param("failed", None, "failed", id="role-run-failed"),
        pytest.param(
            "completed",
            {"profile": "review", "status": "result_missing", "value": None,
             "detail": "", "read_before_result": []},
            "result_missing", id="role-delivered-nothing",
        ),
    ],
)
def test_a_review_that_cannot_run_leaves_the_memo_standing_and_says_so(
    monkeypatch, status, work_result, reason
):
    store = RoleStore()
    _finished_role(store, status=status, work_result=work_result)
    monkeypatch.setattr(
        review_pass, "submit_or_find_child",
        lambda scope, **kwargs: dict(store.rows[0]),
    )
    _, events, flushed = _mission(monkeypatch, store=store)
    state = algorithm._node_critic(_state(route="critic_research", critic={"verdict": "pass"}))
    assert state["route"] == "critic_unavailable"
    assert state["critic"] is None
    assert state["memo_markdown"] == MEMO and flushed == []
    unavailable = [data for _, data in events if data.get("kind") == "critic_unavailable"]
    assert [(item["status"], item["reason"]) for item in unavailable] == [("failed", reason)]
    assert "nicht als geprüft bestätigt" in unavailable[0]["detail"]


def test_a_malformed_role_result_never_reaches_the_mission_reader(monkeypatch, caplog):
    """Both engines read the same reader detail. A verdict that missed its
    schema is model debris — ids, half a schema — so the mission shows the
    one sentence the kernel shows and logs the raw text."""
    store = RoleStore()
    _finished_role(store, status="completed", work_result={
        "profile": "review", "status": "schema_invalid", "value": None,
        "detail": '{"complete": "kaputt", "basis": "source:ref_kd1"}',
        "read_before_result": [],
    })
    monkeypatch.setattr(
        review_pass, "submit_or_find_child",
        lambda scope, **kwargs: dict(store.rows[0]),
    )
    _, events, _flushed = _mission(monkeypatch, store=store)
    with caplog.at_level("WARNING", logger="inqtrix"):
        state = algorithm._node_critic(_state(route="critic_research", critic={"verdict": "pass"}))
    assert state["route"] == "critic_unavailable"
    detail = [
        data for _, data in events if data.get("kind") == "critic_unavailable"
    ][0]["detail"]
    assert detail.endswith(
        "Die Prüfung lieferte kein auswertbares Ergebnis; der Rohtext steht "
        "im Serverprotokoll."
    )
    assert "source:ref_kd1" not in detail
    assert "source:ref_kd1" in caplog.text


def test_a_role_verdict_is_applied_through_the_missions_own_write_path(monkeypatch):
    store = RoleStore()
    reference_id = KernelDeps.ledger_id(REFERENCE)
    _finished_role(store, status="completed", work_result={
        "profile": "review", "status": "completed", "detail": "",
        "read_before_result": [["canvas", "art_memo"], ["retained_evidence", reference_id]],
        "value": _verdict(
            complete=False,
            criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
            findings=[_diagnostic_finding("Fehler.", finding="Platzhalter entfernen.")],
        ),
    })
    monkeypatch.setattr(
        review_pass, "submit_or_find_child", lambda scope, **kwargs: dict(store.rows[0]),
    )
    llm = CallLLM([
        {"edits": [{"finding_id": "finding_1", "replacement": "Keine offenen Punkte."}]},
        UNQUOTED,
    ])
    _, events, flushed = _mission(monkeypatch, llm=llm, store=store)
    state = algorithm._node_critic(_state())
    assert flushed == [MEMO.replace("Fehler.", "Keine offenen Punkte.")]
    assert state["memo_markdown"] == flushed[0]
    assert state["critic"]["verdict"] == "revise" and state["critic"]["transport"] == "role"
    # The writer's node revises; the review node judged.
    assert llm.calls[0]["schema_name"] == "DeepRevisionBundle"
    assert llm.calls[0]["model"] == "agent_synthesis-model"
    # The bundle names the labels the memo may cite, compactly: a source's
    # material reaches the revision only through a basis a finding names.
    revision_prompt = llm.calls[0]["prompt"]
    assert '"refs": [{"document_id": "doc_1", "label": "K1", "title": "Vertrag"}]' in revision_prompt
    assert REFERENCE["excerpt"] not in revision_prompt
    kinds = _kinds(events)
    assert kinds.index("critic_revise") < kinds.index("deep_review_findings")


@pytest.mark.parametrize(
    ("language", "preview"),
    [("de", "2 Befunde gemeldet"), ("en", "2 findings reported")],
)
def test_the_role_row_counts_the_findings_the_review_reported(monkeypatch, language, preview):
    """The role row's preview counts what the review REPORTED - one finding
    here rests on a basis the ledger does not hold and stays unconfirmed, and
    still counts - as a localized one/other phrase. It once read "2 finding(s)"."""
    store = RoleStore()
    _finished_role(store, status="completed", work_result={
        "profile": "review", "status": "completed", "detail": "",
        "read_before_result": [["canvas", "art_memo"]],
        "value": _verdict(
            complete=False,
            criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
            findings=[
                {
                    "target": "artifact", "artifact_id": "art_memo", "kind": "output_requirement",
                    "finding": "Platzhalter entfernen.", "quote": "Fehler.", "basis_ids": [],
                },
                {
                    "target": "artifact", "artifact_id": "art_memo", "kind": "evidence_error",
                    "finding": "Frist nicht belegt.", "quote": "Die Frist beträgt 30 Tage",
                    "basis_ids": ["source:ref_not_in_the_ledger"],
                },
            ],
        ),
    })
    monkeypatch.setattr(
        review_pass, "submit_or_find_child", lambda scope, **kwargs: dict(store.rows[0]),
    )
    llm = CallLLM([
        {"edits": [{"finding_id": "finding_1", "replacement": "Keine offenen Punkte."}]},
        UNQUOTED,
    ])
    _, events, _ = _mission(monkeypatch, llm=llm, store=store)
    state = algorithm._node_critic(_state(language=language))
    finished = [
        data for name, data in events
        if name.endswith("tool.finished") and data.get("invocation_id") == "role-review:1"
    ]
    assert [data["result_preview"] for data in finished] == [preview]
    # The review's own findings line counts only what it applies; the row
    # reports what the role said, and the unconfirmed finding stays open.
    assert any("Frist nicht belegt." in line["text"] for line in state["critic"]["open_lines"])
    applied = next(data for _, data in events if data.get("kind") == "deep_review_findings")
    assert applied["label"] == ("Verifikation: 1 Befund" if language == "de" else "Verification: 1 finding")


def test_one_reported_finding_takes_the_singular(monkeypatch):
    """A single finding the ledger cannot confirm is still reported: the row
    reads "1 Befund gemeldet", not the plural and not a confirmed count of 0."""
    store = RoleStore()
    _finished_role(store, status="completed", work_result={
        "profile": "review", "status": "completed", "detail": "",
        "read_before_result": [["canvas", "art_memo"]],
        "value": _verdict(
            complete=False,
            criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
            findings=[{
                "target": "artifact", "artifact_id": "art_memo", "kind": "evidence_error",
                "finding": "Frist nicht belegt.", "quote": "Die Frist beträgt 30 Tage",
                "basis_ids": ["source:ref_not_in_the_ledger"],
            }],
        ),
    })
    monkeypatch.setattr(
        review_pass, "submit_or_find_child", lambda scope, **kwargs: dict(store.rows[0]),
    )
    _, events, _ = _mission(monkeypatch, llm=CallLLM([]), store=store)
    algorithm._node_critic(_state(language="de"))
    finished = [
        data for name, data in events
        if name.endswith("tool.finished") and data.get("invocation_id") == "role-review:1"
    ]
    assert [data["result_preview"] for data in finished] == ["1 Befund gemeldet"]


def test_a_measured_quote_no_edit_can_reach_stays_open_and_announces_no_revision(monkeypatch):
    quote = "Die Marktlage bleibt unerwartet dynamisch"
    paragraph = f'Analyse: "{quote}" [W1].'
    web = {"label": "W1", "url": "https://example.org/markt", "title": "Markt",
           "excerpt": "Der Auszug behandelt die Marktlage ohne dieses Zitat."}
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    _, events, flushed = _mission(
        monkeypatch, llm=llm, tier_policy=SimpleNamespace(verify="escalating"),
    )
    state = algorithm._node_critic(_state(
        memo_markdown=f"# Memo\n\n{paragraph}\n\n{paragraph}", references=[web],
    ))
    kinds = _kinds(events)
    assert "critic_web_quote_escalation" not in kinds and "critic_revise" not in kinds
    assert flushed == [] and len(llm.calls) == 1
    assert state["critic"]["verdict"] == "open"
    assert state["critic"]["open_lines"][0]["kind"] == "not_checked"
    assert quote in state["critic"]["open_lines"][0]["text"]


def test_a_server_without_the_kernel_never_tries_the_role(monkeypatch):
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)
    monkeypatch.setattr(
        review_pass, "submit_or_find_child",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no role on this server")),
    )
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    _mission(monkeypatch, llm=llm, executes_kernel=False)
    state = algorithm._node_critic(_state())
    assert state["critic"]["transport"] == "prompt"


def test_an_open_information_criterion_researches_while_the_budget_allows(monkeypatch):
    verdict = _verdict(
        complete=False,
        criteria=[{"index": 1, "met": False, "kind": "information", "gap": "Kein Beleg zur Frist."}],
    )
    _, events, _ = _mission(monkeypatch, llm=CallLLM([verdict]))
    state = algorithm._node_critic(_state())
    assert (state["route"], state["replan_rounds"]) == ("critic_research", 1)
    assert state["critic"]["verdict"] == "research"
    assert state["critic"]["criteria_uncovered"] == [
        {"index": 1, "text": "Frist genannt", "kind": "information",
         "gap": "Kein Beleg zur Frist.", "state": "open"}
    ]
    research = next(data for _, data in events if data.get("kind") == "critic_research")
    assert "Frist genannt" in research["detail"]

    _, events, _ = _mission(monkeypatch, llm=CallLLM([verdict]))
    spent = algorithm._node_critic(_state(replan_rounds=2))
    assert (spent["route"], spent["replan_rounds"]) == ("critic_research_exhausted", 2)
    assert "critic_research" not in _kinds(events)
    assert "critic_research_exhausted" in _kinds(events)
    assert spent["critic"]["verdict"] == "open"


CLOSED = Prospect(reachable=False, branches=(), codes=("web_not_needed",), inputs={"replan_headroom": 3})


def _information_open(*indexes: int) -> dict[str, Any]:
    return _verdict(
        complete=False,
        criteria=[
            {"index": index, "met": False, "kind": "information", "gap": f"Lücke {index}."}
            for index in indexes
        ],
    )


def _exhausted(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [data for _, data in events if data.get("kind") == "critic_research_exhausted"]


def test_an_open_information_criterion_without_prospect_is_named_not_researched(monkeypatch):
    _, events, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1)]), prospect=CLOSED)
    state = algorithm._node_critic(_state())
    assert (
        state["route"], state["replan_rounds"],
        state["critic"]["research_limits"], state["critic"]["prospect_codes"],
    ) == ("critic_research_exhausted", 0, ["no_prospect"], ["web_not_needed"])
    [exhausted] = _exhausted(events)
    assert (exhausted["reason"], exhausted["codes"], exhausted["label"]) == (
        "no_prospect", ["web_not_needed"], "Nachrecherche ohne Aussicht",
    )
    assert "critic_research" not in _kinds(events)


def test_a_plan_without_a_free_task_slot_is_named_as_the_limit(monkeypatch):
    full = Prospect(reachable=False, branches=(), codes=(), inputs={"replan_headroom": 0})
    _, events, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1)]), prospect=full)
    state = algorithm._node_critic(_state())
    assert state["critic"]["research_limits"] == ["plan_task_limit"]
    [exhausted] = _exhausted(events)
    assert (exhausted["reason"], exhausted["label"]) == ("plan_task_limit", "Keine Aufgabe mehr planbar")
    assert "Höchstzahl an Aufgaben" in exhausted["detail"]


def test_every_limit_that_applies_is_named(monkeypatch):
    """The brake, the round cap and a closed prospect all apply: one
    decision names all three, in that order."""
    _, events, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1)]), prospect=CLOSED)
    state = algorithm._node_critic(_state(
        replan_rounds=2,
        critic={"verdict": "research", "evidence_identity": algorithm.evidence_identity([REFERENCE])},
    ))
    assert [data["reasons"] for data in _exhausted(events)] == [
        ["no_new_evidence", "replan_rounds", "no_prospect"],
    ]
    assert state["critic"]["research_limits"] == ["no_new_evidence", "replan_rounds", "no_prospect"]
    [exhausted] = _exhausted(events)
    assert exhausted["label"] == "Nachrecherche beendet"
    assert "keine neuen Belege" in exhausted["detail"] and "Replan-Limit" in exhausted["detail"]


def test_round_cap_and_time_budget_are_both_reported(monkeypatch):
    from inqtrix.agents.limit_contract import LIMIT_REACHED_EVENT

    deps, events, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1)]))
    deps.platform.mission_time_budget_seconds = 60
    monkeypatch.setattr(algorithm, "_mission_active_seconds", lambda deps: 61.0)
    algorithm._node_critic(_state(replan_rounds=2))
    assert [data["kind"] for name, data in events if name == LIMIT_REACHED_EVENT] == [
        "replan_rounds", "mission_time_budget",
    ]
    assert [data["reasons"] for data in _exhausted(events)] == [["replan_rounds", "mission_time_budget"]]


def test_the_research_decision_names_every_open_criterion(monkeypatch):
    _, events, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1, 2, 3)]))
    state = algorithm._node_critic(_state(success_criteria=["K-Eins", "K-Zwei", "K-Drei"]))
    assert state["route"] == "critic_research"
    [research] = [data for _, data in events if data.get("kind") == "critic_research"]
    assert all(text in research["detail"] for text in ("K-Eins", "K-Zwei", "K-Drei"))


def test_the_review_report_carries_its_round_and_the_criteria_it_numbered(monkeypatch):
    _, _, _ = _mission(monkeypatch, llm=CallLLM([_information_open(1, 2)]))
    state = algorithm._node_critic(_state(success_criteria=["K-Eins", "K-Zwei"], replan_rounds=1))
    critic = state["critic"]
    assert (critic["verdict"], critic["round"], critic["revision"], critic["accounting"]) == (
        "research", 2, "none", "complete",
    )
    assert state["last_review"] == {"status": "completed", "round": 2}
    assert [(item["index"], item["gap"]) for item in critic["criteria_uncovered"]] == [
        (1, "Lücke 1."), (2, "Lücke 2."),
    ]
    assert [line["kind"] for line in critic["open_lines"]] == ["information", "information"]
    # The list the review numbered: a replan names its open criteria by
    # these numbers only while the plan still holds it.
    assert critic["criteria"] == ["K-Eins", "K-Zwei"]


def test_an_applied_revision_is_recorded_as_unverified(monkeypatch):
    """The edit is applied and its re-check left it unchecked."""
    llm = CallLLM([
        _verdict(
            complete=False,
            criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
            findings=[_diagnostic_finding("Fehler.", finding="Platzhalter entfernen.")],
        ),
        {"edits": [{"finding_id": "finding_1", "replacement": "Keine offenen Punkte."}]},
        UNQUOTED,
    ])
    _, _, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state())
    assert flushed, "precondition: the edit was applied"
    assert (state["critic"]["revision"], state["critic"]["open_lines"]) == ("unverified", [])


def test_a_discarded_revision_keeps_its_findings_open(monkeypatch):
    llm = CallLLM([_verdict(
        complete=False,
        criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
        findings=[{
            "target": "artifact", "artifact_id": "art_memo", "kind": "output_requirement",
            "finding": "Den Abschnitt Ausblick ergänzen.", "quote": "Kein solcher Satz.", "basis_ids": [],
        }],
    )])
    _, events, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state())
    assert flushed == [] and "critic_revision_discarded" in _kinds(events)
    assert state["critic"]["revision"] == "discarded"
    assert state["critic"]["open_lines"] == [{
        "kind": "finding",
        "text": "Befund nicht angewendet (zitierte Stelle steht nicht im Text): "
                "Den Abschnitt Ausblick ergänzen.",
        "reason": "anchor_missing",
    }]


def test_a_criterion_the_verdict_left_unsettled_leaves_the_accounting_incomplete(monkeypatch):
    _, _, _ = _mission(monkeypatch, llm=CallLLM([_verdict(complete=False, criteria=[])]))
    state = algorithm._node_critic(_state())
    assert (state["critic"]["verdict"], state["critic"]["accounting"]) == ("open", "incomplete")
    assert [line["kind"] for line in state["critic"]["open_lines"]] == ["not_checked"]


REVIEW_ENDING_ACTIVITIES = {"critic_passed", "critic_open_points", "deep_review_coverage"}
"""Activity kinds no run emits: how a review ended is the outcome record's to
say, in both engines, and a surface that read an activity for it would build
its words from something other than the record."""


def test_how_a_review_ended_travels_only_in_the_outcome_record(monkeypatch):
    """A passing and an open mission review and a kernel review each end
    without an activity that states the review's outcome: the record is
    the one carrier."""
    pytest.importorskip("deepagents")
    from inqtrix.agents.phase_models import DeepReviewVerdict

    from tests.agents.test_kernel_deep_mode import StructuredScriptedLLM
    from tests.agents.test_kernel_prompt_events import _text_turn, make_client, wait_status

    met = [{"index": 1, "met": True, "kind": "", "gap": ""}]
    _, passing_events, _ = _mission(monkeypatch, llm=CallLLM([_verdict(criteria=met)]))
    passing = algorithm._node_critic(_state())
    _, open_events, _ = _mission(
        monkeypatch, llm=CallLLM([_verdict(complete=False, criteria=[])]),
    )
    left_open = algorithm._node_critic(_state())
    kinds = {data.get("kind") for _, data in [*passing_events, *open_events]}

    llm = StructuredScriptedLLM(
        [_text_turn("Die Lage ist ruhig.")],
        [DeepReviewVerdict(
            complete=True, grounded=True, contradictions_named=True,
            findings=[], criteria=[],
        )],
    )
    client = make_client(llm)
    with client:
        run_id = client.post("/v1/runs", json={
            "question": "Wie ist die Lage?", "mode": "agent_kernel",
            "autonomy": "autonomous", "agent_overrides": {"agent_tier": "gruendlich"},
        }).json()["run_id"]
        wait_status(client, run_id, {"completed", "failed"}, timeout=30)
        kernel_events = client.get(
            f"/v1/runs/{run_id}/events", params={"format": "json"}
        ).json()["data"]
    kinds |= {
        event["data"].get("kind") for event in kernel_events
        if event["type"] == "inqtrix.agent.activity"
    }
    kernel_reviews = [
        event["data"]["review"] for event in kernel_events
        if event["type"] == "inqtrix.agent.outcome"
    ]
    assert (
        passing["critic"]["verdict"], left_open["critic"]["verdict"], kernel_reviews,
        kinds & REVIEW_ENDING_ACTIVITIES,
    ) == ("pass", "open", ["completed"], set())


def test_a_review_that_cannot_run_records_its_round(monkeypatch):
    inconsistent = _verdict(complete=False, criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])
    _, _, _ = _mission(monkeypatch, llm=CallLLM([inconsistent]))
    state = algorithm._node_critic(_state(replan_rounds=1))
    assert state["critic"] is None
    assert state["last_review"] == {"status": "unavailable", "round": 2, "reason": "inconsistent"}


def test_the_review_gets_the_evidence_notes_the_writer_got(monkeypatch):
    flagged = TaskOutcome(
        status="completed", summary="ok",
        evidence_contract_status="needs_review", confidence=6, single_source_ratio=0.93,
    )
    note = algorithm._task_evidence_notes({"t1": flagged}, "de", titles={"t1": "Fristen prüfen"}, readings=False)
    assert note, "the fixture must be a flagged child"
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    _mission(monkeypatch, llm=llm)
    algorithm._node_critic(_state(outcomes=algorithm._outcomes_to_state({"t1": flagged})))
    prompt = llm.calls[0]["prompt"]
    assert "diagnostic:run_m:task_evidence_notes" in prompt
    assert "Konfidenz 6/10" in prompt


# -- the whole path: a mission parks on its review role and is woken by it ----------------


def test_a_mission_parks_on_its_review_role_and_continues_with_its_verdict(monkeypatch):
    """Real graph, real run store: the mission hands a large review to the
    role, waits slot-free as it waits for research children, is woken by the
    child's terminal write and applies the role's verdict. One child, one
    role row — however often the critic node was entered."""
    pytest.importorskip("deepagents")
    from inqtrix.providers.base import ChatTurn, ToolCallRequest
    from inqtrix.settings import AgentPlatformSettings
    from tests.agents.test_kernel_deep_mode import REVIEW_RESULT_CALL
    from tests.agents.test_workspace_agent import (
        ScriptedLLM, make_agent_client, review_finding, review_verdict, run_events,
        submit_agent, text_measurements_basis, wait_status,
    )

    class MissionWithRoleLLM(ScriptedLLM):
        """The mission's structured calls plus the tool loop of its role child."""

        role_prompts: list[str] = []

        def supports_tool_calls(self, *, model: str | None = None) -> bool:
            return True

        def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
            names = [item["function"]["name"] for item in tools or []]
            assert REVIEW_RESULT_CALL in names, names
            prompt = "\n".join(str(item.get("content") or "") for item in messages)
            self.role_prompts.append(prompt)
            verdict = review_verdict(
                prompt, complete=False,
                findings=[review_finding(
                    prompt, "Die Marktlage ist stabil [W1].",
                    kind="run_diagnostic", basis_ids=(text_measurements_basis(prompt),),
                )],
            )
            return ChatTurn(
                text="", finish_reason="tool_calls", model="high-model",
                prompt_tokens=7, completion_tokens=3, raw=None,
                tool_calls=(ToolCallRequest(
                    id="call_review_1", name=REVIEW_RESULT_CALL, arguments=verdict,
                ),),
            )

    # Any material is "large": the review must take the role.
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)
    llm = MissionWithRoleLLM(overrides={
        "DeepRevisionBundle": {"edits": [
            {"finding_id": "finding_1", "replacement": "Der Markt wächst moderat [W1]."},
        ]},
        # The edit is applied and its re-check leaves it unchecked.
        "RevisionCheckVerdict": UNQUOTED,
    })
    client = make_agent_client(
        monkeypatch, llm=llm,
        platform=AgentPlatformSettings(
            INQTRIX_AGENT_ALLOW_VOLATILE=True, INQTRIX_AGENT_KERNEL_ENABLED=True,
        ),
    )
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert [(row["mode"], row["status"]) for row in children] == [("agent_kernel", "completed")]
        events = run_events(client, run_id)
        rows = [
            (event["type"].rsplit(".", 1)[1], event["data"]["invocation_id"], event["data"].get("status"))
            for event in events if ".tool." in event["type"]
        ]
        assert rows == [("started", "role-review:1", None), ("finished", "role-review:1", "success")]
        # The wait held no execution slot: the run parked like a research wait.
        assert [
            event["data"]["status"] for event in events if event["type"] == "inqtrix.run.waiting"
        ] == ["waiting_for_children"]
        # The re-entry after the wait announces nothing a second time.
        assert [
            event["data"]["phase"] for event in events
            if event["type"] == "inqtrix.agent.phase.changed" and event["data"]["phase"] == "critic"
        ] == ["critic"]
        # The resumed segment decides and sends the one record; the park
        # sends none.
        assert [
            (event["data"]["review"], event["data"]["revision"]) for event in events
            if event["type"] == "inqtrix.agent.outcome"
        ] == [("completed", "unverified")]
        answer = client.get(f"/v1/runs/{run_id}/result").json()["answer"]
        assert "Der Markt wächst moderat [W1]." in answer
    # The role was handed the mission's assignment, criteria and ledger.
    assert "Alle Anbieter mit Quelle belegt." in llm.role_prompts[0]
    assert "read_evidence without arguments lists every reference" in llm.role_prompts[0]
    assert "structured:DeepReviewVerdict" not in llm.calls


# -- the reading record: what a document analysis read reaches the review ------- #


def _reading(**document: Any) -> dict[str, Any]:
    base = {
        "document_id": "doc_1", "title": "Vertrag", "total_chars": 9_000, "windows_planned": 1,
        "windows_analysed": 1, "text_read_to_end": True, "unanalysed": [], "failure": "",
        "extraction_status": "recorded", "pages": 2, "pages_without_text": [], "findings_cited": 2,
        "findings_rejected": {}, "findings_over_limit": 0, "windows_possibly_more": 0,
    }
    return {
        "documents": [{**base, **document}], "questions": ["Welche Fristen gelten?"],
        "findings": [{"id": "F1", "document_id": "doc_1", "quote": "CANARY-QUOTE", "claim": "CANARY-CLAIM"}],
        "summary": "CANARY-SUMMARY", "open_points": ["CANARY-OPEN"], "not_selected": [],
        "collection_documents": 1,
    }


SECOND_CHUNK = {"label": "K2", "document_id": "doc_1", "chunk_index": 7, "title": "Vertrag",
                "excerpt": "Die Kündigung erfolgt schriftlich."}


def _plan_with(deps: Any, title: str = "Vertrag vollständig lesen") -> None:
    task = SimpleNamespace(task_id="t1", title=title, tool_kind="file_analysis", status="completed",
                           objective="Fristen belegen.")

    async def get_plan(run_id: str):
        return SimpleNamespace(version=1, status="approved"), [task]

    async def list_clarifications(run_id: str):
        return []

    deps.control = SimpleNamespace(get_plan=get_plan, list_clarifications=list_clarifications)


def test_mission_review_receives_the_reading_record_for_a_clean_complete_reading(monkeypatch):
    """Round B: the review got no fact about a complete reading, could not
    confirm 'all deadlines listed' and sent the mission back to research
    twice. A clean reading must reach it as its own record."""
    outcome = TaskOutcome(status="completed", summary="Dokumentanalyse", document_analysis=_reading(),
                          evidence=[dict(REFERENCE), dict(SECOND_CHUNK)])
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    deps, _events, _ = _mission(monkeypatch, llm=llm)
    _plan_with(deps)
    algorithm._node_critic(_state(outcomes=algorithm._outcomes_to_state({"t1": outcome})))
    prompt = llm.calls[0]["prompt"]
    assert "diagnostic:run_m:t1:document_analysis" in prompt
    assert '"document_complete": true' in prompt and '"text_read_to_end": true' in prompt
    assert '"title": "Vertrag vollständig lesen"' in prompt
    # Verified by the analysis, cited nowhere in the memo: measured, named.
    uncited = "source:" + KernelDeps.ledger_id(SECOND_CHUNK)
    assert f'"uncited_verified_sources": ["{uncited}"]' in prompt
    # One carrier per fact: the reading does not come back as a note.
    assert "task_evidence_notes" not in prompt
    for canary in ("CANARY-SUMMARY", "CANARY-OPEN", "CANARY-CLAIM", "CANARY-QUOTE"):
        assert canary not in prompt


def test_review_task_evidence_notes_do_not_repeat_the_reading(monkeypatch):
    limited = TaskOutcome(status="completed", summary="Dokumentanalyse",
                          document_analysis=_reading(pages_without_text=[2]), evidence=[dict(REFERENCE)])
    capped = TaskOutcome(status="completed", summary="ok", evidence_contract_status="needs_review",
                         confidence=6, single_source_ratio=0.93)
    llm = CallLLM([_verdict(criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}])])
    deps, _events, _ = _mission(monkeypatch, llm=llm)
    _plan_with(deps)
    algorithm._node_critic(_state(outcomes=algorithm._outcomes_to_state({"t1": limited, "t2": capped})))
    prompt = llm.calls[0]["prompt"]
    assert '"pages_without_text": [2]' in prompt
    notes = prompt.split("diagnostic:run_m:task_evidence_notes", 1)[1].split("}", 1)[0]
    assert "Konfidenz 6/10" in notes and "Textschicht" not in notes


def test_the_review_rubric_says_how_a_reading_record_counts():
    """The rule stands in the trusted rubric, never inside the fenced record."""
    from inqtrix.agents.kernel.evidence_read import READING_RECORD_METHOD
    from inqtrix.agents.prompts import DEEP_REVIEW_RUBRIC

    assert "counts as read for exactly what it records" in DEEP_REVIEW_RUBRIC
    assert "a limit it names stays open" in DEEP_REVIEW_RUBRIC
    assert "several records about the same material are not independent confirmations" in DEEP_REVIEW_RUBRIC
    # The fenced description only describes; it addresses no reader.
    for rule_word in ("counts as read", "you", "confirm", "treat", "claim", "must"):
        assert rule_word not in READING_RECORD_METHOD.lower(), rule_word


def test_the_review_rubric_asks_to_report_what_the_material_does_not_carry():
    """What the material does not carry is reported - a finding on its
    passage, or not_checked - and never written into an output as open;
    which statements rest on the search provider's context the server shows
    the reader itself, so the rubric demands no attribution formula."""
    from inqtrix.agents.phase_models import DeepReviewVerdict
    from inqtrix.agents.prompts import DEEP_REVIEW_RUBRIC

    assert "Provider context shows what the search provider wrote, not what the source says" in DEEP_REVIEW_RUBRIC
    assert "named as open" not in DEEP_REVIEW_RUBRIC
    assert "honestly named" not in DEEP_REVIEW_RUBRIC
    assert "according to [W3]" not in DEEP_REVIEW_RUBRIC
    assert "it stays as written and is reported" in DEEP_REVIEW_RUBRIC
    grounded = DeepReviewVerdict.model_fields["grounded"].description or ""
    assert "explicitly open" not in grounded
    assert "reported" in grounded


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (60, "von 1 Minute Rechenzeit"),
        (90, "von 1,5 Minuten Rechenzeit"),
        (100, "von 1,7 Minuten Rechenzeit"),
        (720, "von 12 Minuten Rechenzeit"),
    ],
)
def test_the_time_budget_line_counts_its_minutes(monkeypatch, seconds, words):
    """A one-minute budget read "1 Minuten"; only a whole single minute takes
    the singular, a fraction stays plural, rounded to one decimal and written
    with the German decimal comma."""
    events: list[dict[str, Any]] = []
    deps = SimpleNamespace(
        platform=SimpleNamespace(mission_time_budget_seconds=seconds),
        emit=lambda name, data: events.append(data),
    )
    monkeypatch.setattr(algorithm, "_critic_research_cap", lambda state, deps: 5)
    monkeypatch.setattr(algorithm, "_mission_active_seconds", lambda deps: seconds + 1.0)
    state = {"language": "de", "replan_rounds": 0}
    budget = algorithm._research_budget(state, deps)
    assert budget.limits == ("mission_time_budget",)
    algorithm._emit_research_exhausted(
        state, deps, limits=budget.limits, budget=budget, prospect=None,
    )
    [exhausted] = [data for data in events if data.get("kind") == "critic_research_exhausted"]
    assert words in exhausted["detail"]


# -- every finding ends applied or named -------------------------------------- #

SETTLED = [{"index": 1, "met": True, "kind": "", "gap": ""}]
THREE = "# Memo\n\nAlpha.\n\nBeta.\n\nGamma.\n\nGamma."
QUOTE = "Die Marktlage bleibt unerwartet dynamisch"
QUOTED_MEMO = f'# Memo\n\nAnalyse: "{QUOTE}" [W1].'
WEB = {"label": "W1", "url": "https://example.org/markt", "title": "Markt",
       "excerpt": "Der Auszug behandelt die Marktlage ohne dieses Zitat."}
ESCALATING = SimpleNamespace(verify="escalating")


def _three_findings(monkeypatch):
    llm = CallLLM([
        _verdict(complete=False, criteria=SETTLED, findings=[
            _diagnostic_finding("Alpha.", finding="Alpha genauer."),
            _diagnostic_finding("Beta.", finding="Beta genauer."),
            _diagnostic_finding("Gamma.", finding="Gamma genauer."),
        ]),
        {"edits": [
            {"finding_id": "finding_1", "replacement": "Alpha ergänzt."},
            {"finding_id": "finding_2", "replacement": "Beta ergänzt."},
        ]},
        unquoted("finding_1", "finding_2"),
    ])
    _, events, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state(memo_markdown=THREE))
    return state, events, flushed


def test_three_findings_one_ambiguous_revises_two_and_names_one(monkeypatch):
    """The ambiguous anchor names its finding alone; the rows stand in
    the verdict's order."""
    state, _, _ = _three_findings(monkeypatch)
    critic = state["critic"]
    assert ([(f["finding_id"], f["state"], f["reason"]) for f in critic["findings"]], critic["revision"]) == (
        [("finding_1", "applied", ""), ("finding_2", "applied", ""),
         ("finding_3", "named", "anchor_ambiguous")],
        "partially_discarded",
    )


def test_a_partial_revision_says_so_in_reader_words(monkeypatch):
    _, events, _ = _three_findings(monkeypatch)
    [discarded] = [data for _, data in events if data.get("kind") == "critic_revision_discarded"]
    assert (discarded["label"], discarded["detail"]) == (
        "Prüfung: Überarbeitung teilweise verworfen",
        "Befund nicht angewendet (zitierte Stelle steht mehrfach im Text): Gamma genauer.",
    )


def test_the_revision_detail_lists_the_named_findings_in_the_verdicts_order(monkeypatch):
    """A finding named at locate time is collected before the one the
    revision named; the reader reads them in the verdict's order."""
    llm = CallLLM([
        _verdict(complete=False, criteria=SETTLED, findings=[
            _diagnostic_finding("Alpha.", finding="Alpha genauer."),
            _diagnostic_finding("Gamma.", finding="Gamma genauer."),
        ]),
        {"edits": [{"finding_id": "finding_1", "replacement": "Alpha."}]},
    ])
    _, events, _ = _mission(monkeypatch, llm=llm)
    algorithm._node_critic(_state(memo_markdown="# Memo\n\nAlpha.\n\nGamma.\n\nGamma."))
    [discarded] = [data for _, data in events if data.get("kind") == "critic_revision_discarded"]
    assert discarded["detail"] == (
        "Befund nicht angewendet (Überarbeitung ließ die Stelle unverändert): Alpha genauer.; "
        "Befund nicht angewendet (zitierte Stelle steht mehrfach im Text): Gamma genauer."
    )


def test_a_finding_keeps_its_validated_criterion(monkeypatch):
    """The per-finding record carries the checked criterion number, which
    the handover's criteria fold will read."""
    llm = CallLLM([
        _verdict(complete=False, criteria=SETTLED,
                 findings=[_diagnostic_finding("Fehler.", criterion_index=1)]),
        {"edits": [{"finding_id": "finding_1", "replacement": "Korrigiert."}]},
        UNQUOTED,
    ])
    _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state())
    assert [row["criterion"] for row in state["critic"]["findings"]] == [1]


def _escalating(monkeypatch, *, verdict: dict[str, Any], replies: list[Any], memo: str = QUOTED_MEMO):
    llm = CallLLM([verdict, *replies])
    _, events, flushed = _mission(monkeypatch, llm=llm, tier_policy=ESCALATING)
    state = algorithm._node_critic(_state(memo_markdown=memo, references=[dict(WEB)]))
    return state, events, flushed


def _server(critic: dict[str, Any]) -> dict[str, Any]:
    [row] = [row for row in critic["findings"] if row["origin"] == "server"]
    return row


def test_a_server_finding_beside_an_unconfirmed_model_finding_is_applied(monkeypatch):
    """An unconfirmed model finding owns no passage. The server's
    passage owns the citation after it, which its fix keeps."""
    state, _, flushed = _escalating(monkeypatch, verdict=_verdict(
        complete=False, criteria=SETTLED,
        findings=[{**_diagnostic_finding(f'Analyse: "{QUOTE}" [W1].'),
                   "kind": "evidence_error", "basis_ids": ["source:ref_nowhere"]}],
    ), replies=[{"edits": [
        {"finding_id": "finding_1", "replacement": "die Marktlage gilt als dynamisch [W1]"},
    ]}])
    assert (
        [(row["origin"], row["state"]) for row in state["critic"]["findings"]],
        [f'"{QUOTE}"' in body for body in flushed],
    ) == ([("server", "applied")], [False])


def _winner(replacement: str, **overrides: Any) -> dict[str, Any]:
    """A confirmed model finding on the quote's paragraph, and its edit."""
    return {
        "verdict": _verdict(complete=False, criteria=SETTLED, findings=[
            _diagnostic_finding(f'Analyse: "{QUOTE}" [W1].', **overrides),
        ]),
        "replies": [{"edits": [{"finding_id": "finding_1", "replacement": replacement}]}, UNQUOTED],
    }


def test_a_losing_server_quote_the_winner_removed_leaves_no_line(monkeypatch):
    """The winning edit carried the fix."""
    state, _, _ = _escalating(monkeypatch, **_winner("Analyse: die Marktlage bleibt dynamisch [W1]."))
    critic = state["critic"]
    server = _server(critic)
    assert (
        [line for line in critic["open_lines"] if line["kind"] == "not_checked"],
        server["state"], server["reason"],
    ) == ([], "applied", "overlap_edit")


def test_a_losing_server_quote_that_still_stands_is_named(monkeypatch):
    """The quote the reader gets is still unverified."""
    state, _, _ = _escalating(monkeypatch, **_winner(f'Analyse: "{QUOTE}" [W1]. Ergänzt.'))
    lines = [line for line in state["critic"]["open_lines"] if line["kind"] == "not_checked"]
    assert [(line["reason"], QUOTE in line["text"]) for line in lines] == [("anchor_overlap", True)]


def test_a_losing_server_quote_left_without_a_web_citation_is_settled_and_logged(
    monkeypatch, caplog
):
    """The quote stands, but its paragraph cites no web source any more: it
    no longer leans on one, and the log says which case this was."""
    with caplog.at_level("INFO", logger="inqtrix"):
        state, _, _ = _escalating(
            monkeypatch, **_winner(f'Analyse: "{QUOTE}".', remove_labels=["W1"]),
        )
    server = _server(state["critic"])
    assert (
        (server["state"], server["reason"]),
        "stands without a web citation after the overlapping edit" in caplog.text,
    ) == (("applied", "overlap_edit"), True)


def test_a_server_line_quotes_only_what_the_server_measured(monkeypatch):
    """The unverified quote stands twice, so each paragraph holding it is an
    anchor; the second paragraph also holds a verified quote, which its line
    must not call unverified."""
    verified = "Der Auszug behandelt die Marktlage"
    second = f'A sagt "{verified}", B sagt "{QUOTE}" [W1].'
    state, events, _ = _escalating(
        monkeypatch,
        verdict=_verdict(criteria=SETTLED),
        replies=[{"edits": [
            {"finding_id": "finding_1",
             "replacement": "Einleitung: die Marktlage gilt als dynamisch [W1]."},
            {"finding_id": "finding_2", "replacement": second},
        ]}],
        memo=f'# Memo\n\nEinleitung: "{QUOTE}" [W1].\n\n{second}',
    )
    line = (
        "Befund nicht angewendet (Überarbeitung ließ die Stelle unverändert): Wörtliches "
        f"Zitat mit Web-Beleg ohne Treffer im gespeicherten Auszug: „{QUOTE}“"
    )
    [discarded] = [data for _, data in events if data.get("kind") == "critic_revision_discarded"]
    assert (
        [entry["text"] for entry in state["critic"]["open_lines"] if entry["kind"] == "not_checked"],
        discarded["detail"],
    ) == ([line], line)


def test_a_losing_paragraph_is_judged_by_the_quote_the_server_measured(monkeypatch, caplog):
    """The loser's paragraph also holds a verified quote, which still stands
    after the winning edit; the quote it was measured for does not."""
    verified = "Der Auszug behandelt die Marktlage"
    with caplog.at_level("INFO", logger="inqtrix"):
        state, _, _ = _escalating(
            monkeypatch,
            verdict=_verdict(complete=False, criteria=SETTLED, findings=[
                _diagnostic_finding(f'B sagt "{QUOTE}" [W1].'),
            ]),
            replies=[{"edits": [
                {"finding_id": "finding_1",
                 "replacement": "B sagt, die Marktlage sei dynamisch [W1]."},
                {"finding_id": "finding_2",
                 "replacement": "Einleitung: die Marktlage gilt als dynamisch [W1]."},
            ]}, UNQUOTED],
            memo=f'# Memo\n\nEinleitung: "{QUOTE}" [W1].\n\nA sagt "{verified}", B sagt "{QUOTE}" [W1].',
        )
    server = [row for row in state["critic"]["findings"] if row["finding_id"] == "finding_3"]
    assert (
        [(row["origin"], row["state"], row["reason"]) for row in server],
        "finding_3: its quote no longer stands after the overlapping edit" in caplog.text,
    ) == ([("server", "applied", "overlap_edit")], True)


def test_settling_without_a_server_loser_measures_nothing(monkeypatch):
    """Outside the escalating tier no server finding exists, and the
    delivered text is not verified a second time."""
    from inqtrix.agents.kernel.review_edits import FindingResult

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the delivered text was verified without a server loser")

    monkeypatch.setattr(algorithm.synthesis, "verify_quotes", unexpected)
    results = [FindingResult(
        finding_id="finding_1", origin="model", output_id="art_memo", kind="run_diagnostic",
        criterion=None, finding="x", quote="Alpha.", state="applied",
    )]
    assert algorithm._settle_server_losers(results, "Alpha.", [], frozenset()) == results


def test_a_basis_the_memo_does_not_cite_is_cited_by_its_fix(monkeypatch):
    """The memo's label space is the whole register."""
    from inqtrix.agents.report_quality import cited_references

    appendix = {"label": "K16", "document_id": "doc_16", "chunk_index": 1,
                "title": "Anhang", "excerpt": "Fehler sind im Anhang belegt."}
    llm = CallLLM([
        _verdict(complete=False, criteria=SETTLED, findings=[{
            "target": "artifact", "artifact_id": "art_memo", "kind": "evidence_error",
            "finding": "Beleg fehlt.", "quote": "Fehler.",
            "basis_ids": [f"source:{KernelDeps.ledger_id(appendix)}"],
        }]),
        {"edits": [{"finding_id": "finding_1", "replacement": "Fehler, belegt [K16]."}]},
        UNQUOTED,
    ])
    _, _, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state(references=[dict(REFERENCE), appendix]))
    assert (
        state["critic"]["findings"][0]["state"],
        [[ref["label"] for ref in cited_references(body, state["references"])] for body in flushed],
    ) == ("applied", [["K1", "K16"]])


def _information_open_with(*findings: dict[str, Any]) -> dict[str, Any]:
    return _verdict(
        complete=False, findings=list(findings),
        criteria=[{"index": 1, "met": False, "kind": "information", "gap": "Lücke."}],
    )


def test_the_research_route_names_its_findings_with_a_reason(monkeypatch):
    """Nothing is applied before research; each finding stays open with why."""
    llm = CallLLM([_information_open_with(
        _diagnostic_finding("Alpha."), _diagnostic_finding("Beta."),
    )])
    _, _, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state(memo_markdown="# Memo\n\nAlpha.\n\nAlpha.\n\nBeta."))
    assert (
        [line["reason"] for line in state["critic"]["open_lines"] if line["kind"] == "finding"],
        flushed, [call["schema_name"] for call in llm.calls],
    ) == (["anchor_ambiguous", "research_route"], [], ["DeepReviewVerdict"])


def test_a_research_route_server_line_names_the_round_not_the_draft(monkeypatch):
    """The draft is rewritten next: its quote may not stand in the next text."""
    state, _, _ = _escalating(monkeypatch, verdict=_information_open_with(), replies=[])
    [line] = [line for line in state["critic"]["open_lines"] if line["kind"] == "not_checked"]
    assert (line["reason"], QUOTE in line["text"], "Runde 1" in line["text"]) == (
        "research_route", False, True,
    )


def test_a_research_route_server_loser_keeps_its_line_and_counts_for_nothing(monkeypatch):
    """Not measured again before research: the next round measures anew."""
    state, _, _ = _escalating(monkeypatch, verdict=_information_open_with(
        _diagnostic_finding(f'Analyse: "{QUOTE}" [W1].'),
    ), replies=[])
    critic = state["critic"]
    assert (
        [line["reason"] for line in critic["open_lines"] if line["kind"] == "not_checked"],
        critic["revision"],
    ) == (["anchor_overlap"], "none")


def test_the_escalation_promises_no_fix(monkeypatch):
    """Announced before any outcome is known, so it promises none; a
    rewording that promises a fix again must be caught here."""
    _, events, _ = _escalating(monkeypatch, verdict=_verdict(criteria=SETTLED), replies=[
        {"edits": [{"finding_id": "finding_1",
                    "replacement": "die Marktlage gilt als dynamisch [W1]"}]},
    ])
    [escalation] = [data for _, data in events if data.get("kind") == "critic_web_quote_escalation"]
    assert escalation["detail"] == (
        "Wörtliche Zitate mit Web-Beleg wurden nicht in den gespeicherten Auszügen "
        "gefunden; sie gehen als Befunde in die Prüfung ein."
    )


def test_the_reflection_digest_lists_every_finding_in_english():
    """The memory reflection reads the whole outcome, not its first eight
    findings, and in the prompt's language."""
    critic = {
        "verdict": "revise",
        "review": {"findings": [
            {"kind": "output_requirement", "finding": f"Befund {index}."} for index in range(9)
        ]},
        "findings": [
            {"finding_id": "finding_9", "origin": "model", "kind": "output_requirement",
             "state": "named", "reason": "anchor_missing", "target": "art_memo", "criterion": None},
        ],
    }
    digest = algorithm._critic_digest({"critic": critic})
    assert (
        len([line for line in digest.splitlines() if line.startswith("- ")]),
        algorithm._critic_digest({}),
    ) == (10, "(no critic report)")


WEB_REFERENCE = {
    "label": "W1", "url": "https://example.org/w1", "title": "Anbieterquelle",
    "grounded_support": "Die Frist wurde neu geregelt.",
}
WEB_MEMO = "# Memo\n\n## Lage\n\nDie Frist beträgt 30 Tage. [W1]\n\n## Grenze\n\nKeine."


def test_a_mission_finding_only_the_providers_summary_could_check_marks_and_ends_open(monkeypatch):
    """The mission asks no revision for a passage its review could only
    measure against the search provider's summary: the memo stays as
    written, the passage is listed as marked, and the review ends open with
    the finding and its reason, nothing discarded. The report counts the
    memo's statement as insufficiently supported and marks its passage."""
    identity = KernelDeps.ledger_id(dict(WEB_REFERENCE))
    llm = CallLLM([_verdict(
        complete=True, grounded=False,
        criteria=[{"index": 1, "met": True, "kind": "", "gap": ""}],
        findings=[{
            "target": "artifact", "artifact_id": "art_memo", "kind": "evidence_error",
            "finding": "W1 nennt keine Frist.", "quote": "Die Frist beträgt 30 Tage.",
            "basis_ids": [f"source:{identity}"],
        }],
    )])
    _, events, flushed = _mission(monkeypatch, llm=llm)
    state = algorithm._node_critic(_state(memo_markdown=WEB_MEMO, references=[dict(WEB_REFERENCE)]))
    assert flushed == [] and len(llm.calls) == 1
    assert not {"critic_revise", "critic_revision_discarded"} & set(_kinds(events))
    [listed] = [data for _, data in events if data.get("kind") == "deep_review_findings"]
    assert [item.get("marked") for item in listed["findings"]] == [True]
    assert state["critic"]["verdict"] == "open"
    assert [(line["kind"], line["reason"]) for line in state["critic"]["open_lines"]] == [
        ("marked", "provider_context_only"),
    ]
    assert state["critic"]["claims"] == {"art_memo": {
        "total": 1, "verified": 0, "source_text": 0, "attributed": 0, "insufficient": 1,
    }}
    assert [(mark["quote"], mark["reason"]) for mark in state["critic"]["marks"]] == [
        ("Die Frist beträgt 30 Tage. [W1]", "finding"),
    ]
