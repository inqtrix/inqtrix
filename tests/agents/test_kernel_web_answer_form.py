"""The form an agent may ask of its quick web search.

``web_instant`` takes an optional ``answer_form``. Without one the search
is the one it always was. A table or a timeline reaches a search provider
that names the parameter; a provider that names none searches as usual and
the model is told. A name that is no form runs no search.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain")

from tests._answer_form_search import NOT_HANDED, FormSearch, PlainSearch
from tests.agents.test_kernel_read_tools_policy import (
    ScriptedToolLLM,
    _pending_tool_approval,
    _text_turn,
    _tool_turn,
    make_client,
    wait_status,
)

_QUESTION = "Wie hat sich die Inflationsrate in Deutschland entwickelt?"


def _run(
    search: Any,
    calls: list[dict[str, Any]],
    *,
    autonomy: str = "autonomous",
    edited: dict[str, Any] | None = None,
    approved: bool = False,
    until: str = "completed",
) -> dict[str, Any]:
    """One agent run whose model makes the *calls* of ``web_instant`` in turn, through the API.

    With *edited* the run stops at the approval of the first call and the
    approver replaces its arguments; with *approved* the approver lets the
    call pass as it is.

    Returns what the run left behind: the tool replies the model read, how
    the transcript settled each call, the parameters of every search in the
    run's search ledger by its id, the approvals, and the answer of the
    server to the edit.
    """
    llm = ScriptedToolLLM([
        *[_tool_turn(f"call_form_{index}", "web_instant", arguments) for index, arguments in enumerate(calls)],
        _text_turn("Antwort auf Basis der Suche."),
    ])
    client = make_client(llm, search)
    edit_status = None
    with client:
        response = client.post("/v1/runs", json={
            "question": "Wie steht es um die Inflation?", "mode": "agent_kernel", "autonomy": autonomy,
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        if edited is not None or approved:
            wait_status(client, run_id, {"waiting_for_approval"})
            approval = _pending_tool_approval(client, run_id)
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                json=(
                    {"decision": "approve"} if edited is None
                    else {"decision": "edit", "actions": [{"tool": "web_instant", "args": edited}]}
                ),
            )
            edit_status = decided.status_code
        summary = wait_status(client, run_id, {until, "failed"})
        assert summary["status"] == until, summary
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
        ledger: dict[str, Any] = {}
        for row in client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]:
            payload = client.get(f"/v1/runs/{run_id}/artifacts/{row['artifact_id']}").json().get("payload") or {}
            for query_id, record in (payload.get("web_search_ledger") or {}).get("searches", {}).items():
                ledger[query_id] = record["parameters"]
    replies = [
        str(message.get("content"))
        for message in llm.chat_calls[-1]["messages"]
        if message.get("role") == "tool"
    ]
    return {
        "replies": replies,
        "settled": [
            event["data"]["status"] for event in events
            if event["type"] == "inqtrix.agent.tool.finished" and event["data"]["tool"] == "web_instant"
        ],
        "ledger": ledger,
        "approvals": approvals,
        "edit_status": edit_status,
    }


@pytest.mark.parametrize("arguments", [{}, {"answer_form": None}, {"answer_form": "overview"}],
                         ids=["no form", "null", "overview"])
@pytest.mark.parametrize("provider", [PlainSearch, FormSearch], ids=["plain provider", "provider with forms"])
def test_a_search_without_a_form_is_the_search_it_was(provider: type, arguments: dict[str, Any]) -> None:
    """A model that fills every argument sends null for a form it does not
    choose; that is no form, as the approval card reads it."""
    search = provider()
    run = _run(search, [{"query": _QUESTION, **arguments}])
    (parameters,) = run["ledger"].values()
    assert search.calls == [{"query": _QUESTION, "answer_form": NOT_HANDED}]
    assert (run["settled"], "answer_form" in parameters, "answer form" in run["replies"][0]) == (["success"], False, False)


@pytest.mark.parametrize("form", ["table", "timeline"])
def test_the_form_a_call_names_reaches_a_provider_that_names_forms(form: str) -> None:
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION, "answer_form": form}])
    (parameters,) = run["ledger"].values()
    assert search.calls == [{"query": _QUESTION, "answer_form": form}]
    assert (parameters["answer_form"], parameters["answer_form_sent"], "answer form" in run["replies"][0]) == (
        form, True, False,
    )


def test_a_provider_that_names_no_form_searches_as_usual_and_the_model_is_told() -> None:
    """The model asked for a table and reads an answer in another form: it
    learns why instead of taking the provider's choice for its own."""
    search = PlainSearch()
    run = _run(search, [{"query": _QUESTION, "answer_form": "table"}])
    (parameters,) = run["ledger"].values()
    assert search.calls == [{"query": _QUESTION, "answer_form": NOT_HANDED}]
    assert (run["settled"], parameters["answer_form"], parameters["answer_form_sent"]) == (["success"], "table", False)
    assert run["replies"][0].startswith(
        "Note: the search provider takes no answer form, so the answer below comes in its usual form, "
        "not as the table that was asked for.\n\n"
    )


@pytest.mark.parametrize("form", ["list", "Table", ""])
def test_a_name_that_is_no_form_runs_no_search(form: str) -> None:
    """The refusal is a failed tool step, and nothing was searched or
    recorded under a form nobody knows."""
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION, "answer_form": form}])
    assert (search.calls, run["settled"], run["ledger"]) == ([], ["error"], {})
    assert "table" in run["replies"][0] and "timeline" in run["replies"][0]


def test_an_approver_sees_a_name_that_is_no_form_before_the_tool_refuses_it() -> None:
    """The approval card says that such a call will be refused. That holds
    only while the approval comes first and the refusal after it."""
    search = FormSearch()
    asked = _run(search, [{"query": _QUESTION, "answer_form": "list"}], autonomy="balanced",
                 until="waiting_for_approval")
    (action,) = asked["approvals"][0]["payload"]["actions"]
    assert (action["args"], asked["settled"], search.calls) == ({"query": _QUESTION, "answer_form": "list"}, [], [])
    run = _run(search, [{"query": _QUESTION, "answer_form": "list"}], autonomy="balanced", approved=True)
    assert (run["edit_status"], search.calls, run["settled"], run["ledger"]) == (200, [], ["error"], {})


def test_the_same_question_in_another_form_is_a_search_of_its_own() -> None:
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION}, {"query": _QUESTION, "answer_form": "table"}])
    forms = sorted(parameters.get("answer_form", "none") for parameters in run["ledger"].values())
    assert ([call["answer_form"] for call in search.calls], forms) == ([NOT_HANDED, "table"], ["none", "table"])


def test_the_approval_of_a_search_shows_its_form() -> None:
    run = _run(FormSearch(), [{"query": _QUESTION, "answer_form": "timeline"}],
               autonomy="balanced", until="waiting_for_approval")
    (action,) = run["approvals"][0]["payload"]["actions"]
    assert (action["tool"], action["args"]) == ("web_instant", {"query": _QUESTION, "answer_form": "timeline"})


@pytest.mark.parametrize(
    ("edited", "handed"),
    [("timeline", "timeline"), ("overview", NOT_HANDED), (None, NOT_HANDED)],
    ids=["another form", "back to the default", "null"],
)
def test_the_form_an_approver_chooses_is_the_one_the_search_runs_with(edited: str | None, handed: str) -> None:
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION, "answer_form": "table"}], autonomy="balanced",
               edited={"query": _QUESTION, "answer_form": edited})
    assert (run["edit_status"], search.calls) == (200, [{"query": _QUESTION, "answer_form": handed}])


def test_an_approver_may_choose_a_form_the_call_left_out() -> None:
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION}], autonomy="balanced",
               edited={"query": _QUESTION, "answer_form": "table"})
    assert (run["edit_status"], search.calls) == (200, [{"query": _QUESTION, "answer_form": "table"}])


def test_an_approver_cannot_choose_a_name_that_is_no_form() -> None:
    """The edit is refused where it is made, and the approval stays open."""
    search = FormSearch()
    run = _run(search, [{"query": _QUESTION, "answer_form": "table"}], autonomy="balanced",
               edited={"query": _QUESTION, "answer_form": "list"}, until="waiting_for_approval")
    assert (run["edit_status"], search.calls) == (400, [])
    assert [approval["status"] for approval in run["approvals"]] == ["pending"]
