"""The time window an agent hands to its research children.

``run_web_research`` and the research assignments of ``delegate_batch``
take an optional ``recency``. It travels as the child's ``web_recency``
down to the research engine. Any other value, and a window on an
assignment that is no research, starts no child and comes back as a failed
tool result the model can read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")

from langchain_core.messages import ToolMessage

import inqtrix.research.web_research as web_research_module
from inqtrix.agents.kernel.algorithm import _checkpointed_tool_use_counts
from inqtrix.agents.kernel.deps import RESEARCH_WINDOWS
from inqtrix.agents.kernel.tools import SOURCE_TOOL_FAILURE_PREFIXES, _child_recency
from tests.agents.test_kernel_child_runs import (
    ScriptedToolLLM,
    _text_turn,
    _tool_turn,
    delegated,
    fake_child_graph,
    make_client,
    wait_status,
)

_NOT_HANDED = "not handed"


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    autonomy: str = "autonomous",
    until: str = "completed",
    edited: dict[str, Any] | None = None,
    tier: str = "",
) -> dict[str, Any]:
    """One agent run whose model calls *tool_name* once, through the API.

    With *edited* the run stops at the approval of the call, the approver
    replaces its arguments with *edited*, and the run goes on. With *tier*
    the run asks for that agent tier.

    Returns what the run left behind: the window each child received by its
    question (``_NOT_HANDED`` where it got none), the stored request of each
    child the model delegated, the tool reply the model read, how the
    transcript settled the call, those children and the approvals.
    """
    llm = ScriptedToolLLM([
        _tool_turn("call_child_recency", tool_name, arguments),
        _text_turn("Antwort auf Basis der Recherche."),
    ])
    client = make_client(monkeypatch, llm)
    received: dict[str, Any] = {}

    def recording_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        received[question] = kwargs.get("web_recency", _NOT_HANDED)
        return fake_child_graph(question, **kwargs)

    monkeypatch.setattr(web_research_module, "run_web_graph", recording_child_graph)
    with client:
        response = client.post("/v1/runs", json={
            "question": "Recherchiere die Lage.",
            "mode": "agent_kernel",
            "autonomy": autonomy,
            "tool_directives": ["web_research"],
            **({"agent_overrides": {"agent_tier": tier}} if tier else {}),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        if edited is not None:
            wait_status(client, run_id, {"waiting_for_approval"})
            (approval,) = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                json={"decision": "edit", "actions": [{"tool": tool_name, "args": edited}]},
            )
            assert decided.status_code == 200, decided.text
        summary = wait_status(client, run_id, {until, "failed"})
        assert summary["status"] == until, summary
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
        store = client.container.run_store
        requests = {
            child["question"]: store.execution_request_body(child["run_id"]) for child in children
        }
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
    replies = [
        str(message.get("content"))
        for call in llm.chat_calls[1:]
        for message in call["messages"]
        if message.get("role") == "tool"
    ]
    return {
        "received": received,
        "requests": requests,
        "reply": replies[0] if replies else "",
        "settled": [
            event["data"]["status"]
            for event in events
            if event["type"] == "inqtrix.agent.tool.finished" and event["data"]["tool"] == tool_name
        ],
        "children": children,
        "approvals": approvals,
    }


@pytest.mark.parametrize("window", ["day", "week", "month", "year"])
def test_a_research_child_receives_the_window_its_call_names(monkeypatch, window: str) -> None:
    question = "Was hat sich an der Förderung geändert?"
    run = _run(monkeypatch, "run_web_research", {"question": question, "recency": window})
    assert (run["received"], run["requests"][question].get("web_recency")) == ({question: window}, window)
    assert run["settled"] == ["success"]


def test_a_window_is_read_whatever_its_case_and_blanks(monkeypatch) -> None:
    question = "Was hat sich an der Förderung geändert?"
    run = _run(monkeypatch, "run_web_research", {"question": question, "recency": " Week "})
    assert run["received"] == {question: "week"}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"recency": ""}, {"recency": "  "}, {"recency": None}],
    ids=["left out", "empty", "blank", "null"],
)
def test_a_research_child_without_a_window_receives_none(monkeypatch, arguments: dict[str, Any]) -> None:
    question = "Wie funktioniert eine Wärmepumpe?"
    run = _run(monkeypatch, "run_web_research", {"question": question, **arguments})
    assert (run["received"], run["requests"][question].get("web_recency")) == ({question: _NOT_HANDED}, None)


def test_a_batch_hands_the_window_only_to_the_assignment_that_names_it(monkeypatch) -> None:
    run = _run(monkeypatch, "delegate_batch", {"assignments": [
        {"objective": "Aktuelle Urteile zur Förderung.", "mode": "research", "recency": "month"},
        {"objective": "Grundlagen der Förderung.", "mode": "research"},
        {"objective": "Geschichte der Förderung.", "mode": "research", "recency": ""},
    ]})
    assert run["received"] == {
        "Aktuelle Urteile zur Förderung.": "month",
        "Grundlagen der Förderung.": _NOT_HANDED,
        "Geschichte der Förderung.": _NOT_HANDED,
    }


def test_a_null_window_in_a_batch_counts_as_none(monkeypatch) -> None:
    """A model fills an optional key it does not need with null."""
    run = _run(monkeypatch, "delegate_batch", {"assignments": [
        {"objective": "Grundlagen der Förderung.", "mode": "research", "recency": None},
    ]})
    assert run["received"] == {"Grundlagen der Förderung.": _NOT_HANDED}


@pytest.mark.parametrize("window", ["hour", "2 weeks", "letzte Woche"])
def test_an_unknown_window_starts_no_child_and_tells_the_model(monkeypatch, window: str) -> None:
    """The run goes on: the model reads which values exist and may call
    again. Nothing is searched under a window nobody applies."""
    run = _run(monkeypatch, "run_web_research", {"question": "Was ist neu?", "recency": window})
    assert (run["children"], run["received"]) == ([], {})
    assert window in run["reply"] and "day, week, month, year" in run["reply"]


def test_an_unknown_window_in_a_batch_starts_none_of_its_children(monkeypatch) -> None:
    run = _run(monkeypatch, "delegate_batch", {"assignments": [
        {"objective": "Grundlagen der Förderung.", "mode": "research", "recency": "week"},
        {"objective": "Aktuelle Urteile zur Förderung.", "mode": "research", "recency": "hour"},
    ]})
    assert (run["children"], run["received"]) == ([], {})
    assert "assignment 2" in run["reply"] and "day, week, month, year" in run["reply"]


@pytest.mark.parametrize("window", ["week", "hour"], ids=["a known window", "an unknown window"])
def test_a_window_on_a_knowledge_assignment_is_refused_as_misplaced(monkeypatch, window: str) -> None:
    """A knowledge assignment searches project documents, so the window the
    model asked for would not be applied. The model learns that, whatever
    the value: told to pick another window, it would be refused again."""
    run = _run(monkeypatch, "delegate_batch", {"assignments": [
        {"objective": "Welche Pflichten nennt die Richtlinie?", "mode": "knowledge", "recency": window},
    ]})
    assert (run["children"], run["received"]) == ([], {})
    assert "assignment 1" in run["reply"] and "research assignment only" in run["reply"]
    assert "unknown recency" not in run["reply"]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("run_web_research", {"question": "Was ist neu?", "recency": "hour"}),
        ("delegate_batch", {"assignments": [
            {"objective": "Aktuelle Urteile zur Förderung.", "mode": "research", "recency": "hour"},
        ]}),
        ("delegate_batch", {"assignments": [
            {"objective": "Welche Pflichten nennt die Richtlinie?", "mode": "knowledge", "recency": "week"},
        ]}),
    ],
    ids=["single research", "batch research", "batch knowledge"],
)
def test_a_refused_window_is_a_failed_tool_step(monkeypatch, tool_name: str, arguments: dict[str, Any]) -> None:
    """A refusal the model reads as text is a failure under the one contract
    of the tool texts: the transcript settles the call as failed, and a run
    that parks afterwards does not count it as a use of the web."""
    run = _run(monkeypatch, tool_name, arguments)
    after_a_park = SimpleNamespace(values={"messages": [
        ToolMessage(content=run["reply"], name=tool_name, tool_call_id="call_child_recency"),
    ]})
    assert run["reply"].startswith(SOURCE_TOOL_FAILURE_PREFIXES), run["reply"]
    assert (run["settled"], _checkpointed_tool_use_counts(after_a_park)) == (
        ["error"], {"web": 0, "knowledge": 0},
    )


def test_the_approval_of_a_research_shows_its_window(monkeypatch) -> None:
    """Who approves sees the whole call: the question and the window."""
    run = _run(
        monkeypatch,
        "run_web_research",
        {"question": "Was hat sich an der Förderung geändert?", "recency": "week"},
        autonomy="balanced",
        until="waiting_for_approval",
    )
    (action,) = run["approvals"][0]["payload"]["actions"]
    assert (action["tool"], action["args"]["recency"], run["children"]) == ("run_web_research", "week", [])


@pytest.mark.parametrize(
    ("edited", "handed"),
    [("month", "month"), ("", _NOT_HANDED), (None, _NOT_HANDED)],
    ids=["another window", "emptied", "null"],
)
def test_the_window_an_approver_edits_is_the_one_the_child_receives(monkeypatch, edited: Any, handed: str) -> None:
    question = "Was hat sich an der Förderung geändert?"
    run = _run(
        monkeypatch,
        "run_web_research",
        {"question": question, "recency": "week"},
        autonomy="balanced",
        edited={"question": question, "recency": edited},
    )
    assert run["received"] == {question: handed}


def test_an_approver_may_name_a_window_the_call_left_out(monkeypatch) -> None:
    question = "Was hat sich an der Förderung geändert?"
    run = _run(
        monkeypatch,
        "run_web_research",
        {"question": question},
        autonomy="balanced",
        edited={"question": question, "recency": "year"},
    )
    assert run["received"] == {question: "year"}


def test_a_window_an_approver_mistypes_starts_no_child(monkeypatch) -> None:
    """The edit is accepted as text and the tool reads it like any other
    call: nothing is searched under a window nobody applies."""
    question = "Was hat sich an der Förderung geändert?"
    run = _run(
        monkeypatch,
        "run_web_research",
        {"question": question, "recency": "week"},
        autonomy="balanced",
        edited={"question": question, "recency": "2 Wochen"},
    )
    assert (run["children"], run["received"], run["settled"]) == ([], {}, ["error"])
    assert "2 Wochen" in run["reply"]


@pytest.mark.parametrize(
    ("value", "window"),
    [
        (None, None), ("", None), (" \n", None),
        ("day", "day"), ("YEAR", "year"), ("\tMonth ", "month"),
    ],
)
def test_the_check_reads_a_window_or_none(value: Any, window: str | None) -> None:
    assert _child_recency(value) == (window, "")


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("hour", "'hour'"), ("none", "'none'"), ("null", "'null'"), ("week month", "'week month'"),
        (7, "'7'"), (0, "'0'"), (True, "'True'"), (False, "'False'"), (["week"], '"[\'week\']"'),
        ("zwei\nWochen", "'zwei Wochen'"),
    ],
)
def test_the_check_names_what_is_no_window(value: Any, quoted: str) -> None:
    """Whatever is neither a window nor nothing is a problem that quotes
    the value on one line and names the windows there are."""
    window, problem = _child_recency(value)
    assert (window, problem) == (None, f"unknown recency {quoted}; the windows are day, week, month, year")


def test_the_check_quotes_a_long_value_visibly_shortened() -> None:
    """A text sent in place of a window does not return whole into the
    history of the model, and the cut shows."""
    window, problem = _child_recency("Quellen aus dem letzten Quartal " * 40)
    assert window is None
    assert problem == (
        "unknown recency 'Quellen aus dem letzten Quartal ...'; the windows are day, week, month, year"
    )


def test_the_windows_are_the_four_a_run_request_takes() -> None:
    """The tools name these four to the model and the approval card
    translates them. Another set of windows changes what both say and what
    a parked run finds when its call runs again: the card, the tool texts
    and the contract version of the workflow follow it."""
    assert RESEARCH_WINDOWS == ("day", "week", "month", "year")
