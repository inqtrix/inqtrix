"""What a delegating kernel run learns while its review role reads.

A role run's progress lives in its tool calls. Without them the parent's
line read "Führt Aufgaben aus" for minutes. Only the tool's NAME and the
server's call counter cross — never the call's arguments or its result,
which name the material the role reads — only for a kernel role child,
and at a bounded rate: a mission child's own tool rows stay its own. The
rate is counted by the call's position in the role's sequence of calls,
so a batch of parallel calls crosses like the same calls one by one.
"""

import json

from inqtrix.runs.shared import (
    CHILD_TOOL_PROGRESS_EVERY,
    build_child_progress_payload,
    should_project_child_event,
)

FINISHED = "inqtrix.agent.tool.finished"


def _finished(tool: str, used: int, *, ordinal: int | None = None, **extra) -> dict:
    return {
        "tool": tool,
        "tool_call_id": f"call_{used}",
        **({"call_ordinal": ordinal} if ordinal is not None else {}),
        "status": "success",
        "result_preview": "Belegtext aus dem Bericht",
        "snapshot": {
            "phase": "execution",
            "execution": {"limits": {"tool_calls": {"used": used, "limit": 30}}},
        },
        **extra,
    }


def test_a_role_childs_tool_steps_cross_at_a_bounded_rate() -> None:
    crossed = [
        used
        for used in range(1, 21)
        if should_project_child_event(
            FINISHED, _finished("read_evidence", used, ordinal=used), work_profile="review"
        )
    ]
    assert crossed == [1, *range(CHILD_TOOL_PROGRESS_EVERY, 21, CHILD_TOOL_PROGRESS_EVERY)]


def test_a_batch_of_parallel_calls_crosses_by_each_calls_position() -> None:
    """Eight reads in one turn all settle with the counter at 11; sampled by
    that counter none of them crossed and the line read "1 Schritt" for the
    whole review. Each call's own position (4 to 11) crosses at 5 and 10."""
    crossed = [
        ordinal
        for ordinal in range(4, 12)
        if should_project_child_event(
            FINISHED, _finished("read_evidence", 11, ordinal=ordinal), work_profile="review"
        )
    ]
    assert crossed == [5, 10]


def test_a_step_without_its_position_does_not_cross() -> None:
    """The counter alone says how many calls settled, not which one this is;
    a step without its position stays in the child (fail closed)."""
    assert not should_project_child_event(
        FINISHED, _finished("read_evidence", 5), work_profile="review"
    )


def test_a_mission_childs_tool_rows_and_a_roles_result_call_stay_their_own() -> None:
    # A mission child has no work profile; its section rows stay its own.
    assert not should_project_child_event(FINISHED, _finished("write_section", 5, ordinal=5))
    # The role's typed result is no step of its work.
    assert not should_project_child_event(
        FINISHED,
        _finished("DeepReviewVerdict", 5, ordinal=5, result_call=True),
        work_profile="review",
    )
    # Without a payload or a server counter there is nothing to say.
    assert not should_project_child_event(FINISHED, None, work_profile="review")
    assert not should_project_child_event(
        FINISHED, {"tool": "read_evidence", "snapshot": {}}, work_profile="review"
    )


def test_only_the_tools_name_and_the_server_counter_cross() -> None:
    projected = build_child_progress_payload(
        child_run_id="run_role",
        parent_task_id="call_review",
        run_status="running",
        event_type=FINISHED,
        payload=_finished("read_evidence", 5, args_preview="K12 Seite 4"),
        snapshot={},
        attempt=1,
    )
    assert (projected["tool"], projected["tool_calls"]) == ("read_evidence", 5)
    text = json.dumps(projected, ensure_ascii=False)
    assert "Belegtext" not in text and "K12" not in text
    assert "result_preview" not in text and "args_preview" not in text


def test_the_parent_reads_its_review_roles_steps_while_it_works(monkeypatch) -> None:
    """End to end: a Deep kernel turn whose review runs as a role child.

    The parent's feed carries the role's reading steps by name and count;
    the role's own record marks its typed result call, which never
    crosses.
    """
    import pytest

    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel import review_pass
    from tests.agents.test_kernel_deep_mode import (
        StructuredScriptedLLM,
        _deep_run_over_a_report,
        make_client,
    )

    # The material of this small turn fits one call; the role is what is
    # under test, so the budget sends it there (as the Deep-mode tests do).
    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)

    llm = StructuredScriptedLLM([], [])
    llm.role_reads = [lambda prompt: ("read_evidence", {})]
    llm.review_verdict = lambda prompt: {
        "complete": True, "grounded": True, "contradictions_named": True,
        "findings": [], "criteria": [], "not_checked": [],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "web_search_ledger": {"searches": {}},
        }, "Fünf Jahre Garantie [W1].", "sess-role-progress")
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
        steps = [
            item["data"] for item in events
            if item["type"] == "inqtrix.agent.child.progress"
            and item["data"].get("event_type") == FINISHED
        ]
        assert [(step["tool"], step["tool_calls"]) for step in steps] == [("read_evidence", 1)]
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        [role] = [child for child in children if child.get("work_profile") == "review"]
        role_events = client.get(
            f"/v1/runs/{role['run_id']}/events", params={"format": "json"}
        ).json()["data"]
        finished = {
            item["data"]["tool"]: item["data"].get("result_call", False)
            for item in role_events if item["type"] == FINISHED
        }
        assert finished == {"read_evidence": False, "DeepReviewVerdict": True}


def test_the_parent_sees_a_parallel_batch_of_reads_cross_by_position(monkeypatch) -> None:
    """End to end: the review role reads six times in ONE turn.

    The server's counter is 6 when each of them settles, so sampled by the
    counter none of them crossed. Each call carries the position it started
    at, and the batch crosses at its first and its fifth call; the line
    still shows the server's count.
    """
    import pytest

    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel import review_pass
    from inqtrix.providers.base import ChatTurn, ToolCallRequest
    from tests.agents.test_kernel_deep_mode import (
        REVIEW_RESULT_CALL,
        StructuredScriptedLLM,
        _deep_run_over_a_report,
        make_client,
    )

    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)

    class ParallelReads(StructuredScriptedLLM):
        def __init__(self) -> None:
            super().__init__([], [])
            self.batch_sent = False

        def chat(self, messages, *, tools=None, **kwargs):
            names = [item["function"]["name"] for item in tools or []]
            if REVIEW_RESULT_CALL in names and not self.batch_sent:
                self.batch_sent = True
                return ChatTurn(
                    text="",
                    tool_calls=tuple(
                        ToolCallRequest(id=f"call_batch_{n}", name="read_evidence", arguments={})
                        for n in range(6)
                    ),
                    finish_reason="tool_calls",
                    model="high-model",
                    prompt_tokens=7,
                    completion_tokens=3,
                    raw=None,
                )
            return super().chat(messages, tools=tools, **kwargs)

    llm = ParallelReads()
    llm.review_verdict = lambda prompt: {
        "complete": True, "grounded": True, "contradictions_named": True,
        "findings": [], "criteria": [], "not_checked": [],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "web_search_ledger": {"searches": {}},
        }, "Fünf Jahre Garantie [W1].", "sess-role-batch")
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
    steps = [
        item["data"] for item in events
        if item["type"] == "inqtrix.agent.child.progress"
        and item["data"].get("event_type") == FINISHED
    ]
    assert [(step["tool"], step["tool_calls"]) for step in steps] == [
        ("read_evidence", 6),
        ("read_evidence", 6),
    ]
