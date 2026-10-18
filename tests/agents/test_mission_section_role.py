"""A memo section too large for one call is written by a role that reads.

The transport of a section is decided in ONE place from the size of the
work order a single call would carry. Below the bound the section is written
in one call exactly as before; above it a bounded kernel child - the section
writer - reads the evidence it leads with through ``read_evidence`` and
delivers typed prose. What the section may cite never depends on which
transport wrote it, and the memo never stands in the order the sections
happened to finish.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents import algorithm, synthesis
from inqtrix.agents.kernel.work_profiles import (
    SECTION_WRITER,
    TRANSPORT_PROMPT,
    TRANSPORT_ROLE,
    resolve_work_profile,
)

pytest.importorskip("deepagents")

from inqtrix.providers.base import ChatTurn, ToolCallRequest  # noqa: E402
from inqtrix.settings import AgentPlatformSettings  # noqa: E402
from tests.agents.test_workspace_agent import (  # noqa: E402
    OUTLINE,
    ScriptedLLM,
    _memo_body,
    make_agent_client,
    run_events,
    submit_agent,
    wait_status,
)

KERNEL = AgentPlatformSettings(
    INQTRIX_AGENT_ALLOW_VOLATILE=True, INQTRIX_AGENT_KERNEL_ENABLED=True
)


class SectionRoleLLM(ScriptedLLM):
    """The mission's structured calls plus the tool loop of its section roles.

    A role first reads the first address its work order names, then
    delivers prose that cites that label - or, with ``deliver=False``, ends
    with free text instead of its typed result.
    """

    def __init__(self, *args: Any, deliver: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deliver = deliver
        self.role_turns: list[dict[str, Any]] = []

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
        names = {item["function"]["name"] for item in tools or []}
        # The role's whole surface: its profile's reader and its result, no
        # planner - the same shape the review role's surface is pinned to.
        assert names == resolve_work_profile(SECTION_WRITER).allowed_tools | {
            "SectionText",
        }, names
        prompt = "\n".join(
            str(item.get("content") or "") for item in messages if isinstance(item, dict)
        )
        title = re.search(r"Write the memo section '([^']+)'", prompt).group(1)
        read = any(isinstance(item, dict) and item.get("role") == "tool" for item in messages)
        self.role_turns.append({"title": title, "read": read, "prompt": prompt})
        addresses = re.findall(r"\[([KW]\d+)\][^\n]*\(reference_id: ([^)]+)\)", prompt)

        def turn(name: str, arguments: dict[str, Any], call_id: str) -> ChatTurn:
            return ChatTurn(
                text="", finish_reason="tool_calls", model="mid-model",
                prompt_tokens=7, completion_tokens=3, raw=None,
                tool_calls=(ToolCallRequest(id=call_id, name=name, arguments=arguments),),
            )

        if addresses and not read:
            return turn("read_evidence", {"reference_id": addresses[0][1]}, "call_read_1")
        if not self.deliver:
            return ChatTurn(
                text="Hier ist ein Abschnitt in freiem Text.", finish_reason="stop",
                model="mid-model", prompt_tokens=7, completion_tokens=3, raw=None,
                tool_calls=(),
            )
        label = addresses[0][0] if addresses else re.search(r"\[([KW]\d+)\]", prompt).group(1)
        return turn(
            "SectionText", {"markdown": f"Die Rolle schreibt {title} [{label}]."}, "call_section_1"
        )


def _outline_with_two_led_sections(prompt: str) -> dict[str, Any]:
    """Both sections lead with the first label the outline prompt exposes."""
    outline = json.loads(json.dumps(OUTLINE))
    label = re.search(r"\[([KW]\d+)\]", prompt).group(1)
    for entry in outline["sections"]:
        entry["evidence_labels"] = [label]
    return outline


def _activities(events: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [
        event["data"] for event in events
        if event["type"] == "inqtrix.agent.activity"
        and str(event["data"].get("kind", "")).startswith(prefix)
    ]


def _role_for(titles: set[str]):
    """A transport choice that sends exactly the named sections to the role."""

    def choose(prompt: str, *, role_available: bool) -> str:
        title = re.search(r"Write the memo section '([^']+)'", prompt).group(1)
        return TRANSPORT_ROLE if role_available and title in titles else TRANSPORT_PROMPT

    return choose


# -- the whole path ----------------------------------------------------------------------


def test_a_large_section_is_written_by_a_role_that_reads_its_evidence(monkeypatch):
    """Real graph, real run store: every section goes to the role here.

    Each role reads the evidence its section leads with before it writes;
    the mission submits every role before it parks ONCE, is woken by the
    last terminal write, and folds the sections in outline order.
    """
    monkeypatch.setattr(synthesis, "SECTION_IN_PROMPT_BUDGET_TOKENS", 1)
    llm = SectionRoleLLM(overrides={"ReportOutline": _outline_with_two_led_sections})
    client = make_agent_client(monkeypatch, llm=llm, platform=KERNEL)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        events = run_events(client, run_id)
        memo = _memo_body(client, run_id)
    titles = [entry["title"] for entry in OUTLINE["sections"]]
    # One role per section, each a kernel child that completed.
    assert [(row["mode"], row["status"]) for row in children] == [
        ("agent_kernel", "completed")
    ] * len(titles)
    # No section was written in one call.
    assert not [name for name, _ in llm.prompts if name == "SectionText"]
    # Each role read before it wrote - and what it read carried the stored
    # search answer of its parent mission, which is parked on it meanwhile.
    reads = [turn for turn in llm.role_turns if turn["read"]]
    assert {turn["title"] for turn in reads} == set(titles)
    assert all("Web-Vorschau" in turn["prompt"] for turn in reads)
    assert not any("no longer available" in turn["prompt"] for turn in reads)
    # Every role submitted before one park, never one park per section.
    assert [
        event["data"]["status"] for event in events if event["type"] == "inqtrix.run.waiting"
    ] == ["waiting_for_children"]
    rows = [
        (event["type"].rsplit(".", 1)[1], event["data"]["invocation_id"])
        for event in events if ".tool." in event["type"]
        and str(event["data"].get("invocation_id", "")).startswith("section:")
    ]
    assert sorted(rows) == sorted(
        [("started", f"section:1:{i}") for i in (1, 2)]
        + [("finished", f"section:1:{i}") for i in (1, 2)]
    )
    # The memo holds the roles' prose in outline order.
    positions = [memo.index(f"Die Rolle schreibt {title}") for title in titles]
    assert positions == sorted(positions), memo
    # What each role actually loaded of what it led with is measured.
    folded = _activities(events, "section_")
    assert [row["label"] for row in folded] == titles
    assert all(row["transport"] == "role" for row in folded)
    assert all(row["read_preselected"] == row["preselected"] for row in folded)
    # The synthesis task started once: the return from the park continues
    # it, it does not start it again.
    assert [
        event["data"]["tool_kind"] for event in events
        if event["type"].endswith("task.started")
        and event["data"].get("tool_kind") == "synthesis"
    ] == ["synthesis"]
    # The outline was written once, before the park - never again after it.
    assert [
        event["data"]["narration_id"] for event in events
        if event["data"].get("narration_id") == "n-synthesis"
    ] == ["n-synthesis"]


def test_a_section_that_fits_stays_in_one_call_beside_a_role(monkeypatch):
    """Only the section that needs the role gets one - and order holds.

    The FIRST section goes to the role, the second is written in one call
    before the park. The written second section waits in the progress until
    the first returns: the memo never stands in the order they finished.
    """
    first, second = (entry["title"] for entry in OUTLINE["sections"])
    monkeypatch.setattr(synthesis, "choose_section_transport", _role_for({first}))
    llm = SectionRoleLLM(overrides={"ReportOutline": _outline_with_two_led_sections})
    client = make_agent_client(monkeypatch, llm=llm, platform=KERNEL)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        events = run_events(client, run_id)
        memo = _memo_body(client, run_id)
    assert len(children) == 1
    one_call = [prompt for name, prompt in llm.prompts if name == "SectionText"]
    assert len(one_call) == 1 and f"Write the memo section '{second}'" in one_call[0]
    assert memo.index(f"Die Rolle schreibt {first}") < memo.index(f"## {second}"), memo
    folded = _activities(events, "section_")
    assert [(row["label"], row["transport"]) for row in folded] == [
        (first, "role"), (second, "prompt"),
    ]
    # Each section is announced once, in outline order.
    assert [
        event["data"]["narration_id"] for event in events
        if str(event["data"].get("narration_id", "")).startswith("n-section-")
    ] == ["n-section-0", "n-section-1"]


def test_a_role_that_does_not_deliver_leaves_its_section_to_one_call_visibly(monkeypatch):
    """Never a quiet gap, and never a whole synthesis lost to one role."""
    first, _ = (entry["title"] for entry in OUTLINE["sections"])
    monkeypatch.setattr(synthesis, "choose_section_transport", _role_for({first}))
    llm = SectionRoleLLM(
        deliver=False, overrides={"ReportOutline": _outline_with_two_led_sections}
    )
    client = make_agent_client(monkeypatch, llm=llm, platform=KERNEL)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        events = run_events(client, run_id)
    unavailable = _activities(events, "section_role_unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["status"] == "degraded" and unavailable[0]["label"] == first
    assert unavailable[0]["reason"] == "result_missing"
    # The section exists, written in one call.
    written = [prompt for name, prompt in llm.prompts if name == "SectionText"]
    assert any(f"Write the memo section '{first}'" in prompt for prompt in written)
    assert [row["transport"] for row in _activities(events, "section_evidence")][0] == "prompt"


def test_a_server_without_the_kernel_writes_every_section_in_one_call(monkeypatch, caplog):
    """No role to hand to: one call, and the log says the section was large."""
    monkeypatch.setattr(synthesis, "SECTION_IN_PROMPT_BUDGET_TOKENS", 1)
    llm = SectionRoleLLM(overrides={"ReportOutline": _outline_with_two_led_sections})
    # Switched off explicitly: the kernel is the shipping default, so a
    # platform without the switch would be a server that HAS a role.
    no_kernel = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True, INQTRIX_AGENT_KERNEL_ENABLED=False
    )
    client = make_agent_client(monkeypatch, llm=llm, platform=no_kernel)
    with client, caplog.at_level(logging.WARNING, logger="inqtrix"):
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
    assert children == []
    assert len([name for name, _ in llm.prompts if name == "SectionText"]) == 2
    assert any("runs no kernel role" in record.getMessage() for record in caplog.records)


def test_a_role_section_may_cite_exactly_what_a_one_call_section_may(monkeypatch):
    """The transport decides how material arrives, never what may be cited.

    The SAME section - the first of the outline - is written once by the role
    (first mission) and once in one call (second mission), and both reach the
    closing stage with the same citable labels. Each sample is keyed by its
    mission, so neither can be filled from a sibling section. The ledger is
    wider than the preselection, so "the preselection" and "preselection plus
    register" are different sets and a narrower role could not hide.
    """
    from tests.agents.test_mission_synthesis_context import WideSearch

    first, _ = (entry["title"] for entry in OUTLINE["sections"])
    seen: dict[str, list[str]] = {}
    mission = {"name": ""}
    real_finish = synthesis.finish_section

    def spy(llm: Any, markdown: str, **kwargs: Any) -> Any:
        if f"schreibt {first}" in markdown:
            seen.setdefault(mission["name"], list(kwargs["known_labels"]))
        return real_finish(llm, markdown, **kwargs)

    def one_call_section(prompt: str) -> dict[str, str]:
        title = re.search(r"Write the memo section '([^']+)'", prompt).group(1)
        label = re.search(r"\[([KW]\d+)\]", prompt).group(1)
        return {"markdown": f"Der Einzelaufruf schreibt {title} [{label}]."}

    monkeypatch.setattr(synthesis, "finish_section", spy)
    for name, transport in (("role", TRANSPORT_ROLE), ("prompt", TRANSPORT_PROMPT)):
        mission["name"] = name
        monkeypatch.setattr(
            synthesis, "choose_section_transport",
            _role_for({first}) if transport == TRANSPORT_ROLE else _role_for(set()),
        )
        llm = SectionRoleLLM(overrides={
            "ReportOutline": _outline_with_two_led_sections,
            "SectionText": one_call_section,
        })
        client = make_agent_client(
            monkeypatch, llm=llm, platform=KERNEL, search=WideSearch()
        )
        with client:
            run_id = submit_agent(client, autonomy="autonomous")
            assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
    assert set(seen) == {"role", "prompt"}, seen
    assert len(seen["role"]) > 1, "the ledger must be wider than the preselection"
    assert seen["role"] == seen["prompt"]


# -- the pieces --------------------------------------------------------------------------


def test_the_estimate_decides_the_transport_and_nothing_else(caplog):
    small = "Assignment:\nKurz.\n\nWrite the memo section 'A'."
    large = small + ("Beleg. " * 20_000)
    assert synthesis.choose_section_transport(small, role_available=True) == TRANSPORT_PROMPT
    assert synthesis.choose_section_transport(large, role_available=True) == TRANSPORT_ROLE
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        assert synthesis.choose_section_transport(large, role_available=False) == TRANSPORT_PROMPT
    assert any("runs no kernel role" in record.getMessage() for record in caplog.records)


def test_only_a_committed_park_of_this_round_counts_as_a_park():
    """A crash before the round's first park commits nothing, so it cannot
    look like the return from a park; and a new research round never
    continues the park of the round before it."""
    parked = {"synthesis_pending": True, "synthesis_progress": {"round": 1}, "replan_rounds": 0}
    assert algorithm._synthesis_parked(parked)
    assert not algorithm._synthesis_parked({**parked, "synthesis_pending": False})
    assert not algorithm._synthesis_parked({**parked, "replan_rounds": 1})
    assert not algorithm._synthesis_parked({"replan_rounds": 0})


def test_the_synthesis_waits_as_a_children_wait(monkeypatch):
    """Any other kind would park in a status the store never wakes."""
    import langgraph.types

    parked: list[Any] = []
    monkeypatch.setattr(langgraph.types, "interrupt", parked.append)
    monkeypatch.setattr(algorithm, "_deps", lambda: SimpleNamespace(cancelled=lambda: False))
    state: dict[str, Any] = {"synthesis_pending": True}
    assert algorithm._node_synthesis_wait(state) is state
    assert parked == [{"kind": "children"}]


def test_a_cancelled_mission_does_not_park_on_its_sections(monkeypatch):
    import langgraph.types

    monkeypatch.setattr(
        langgraph.types, "interrupt",
        lambda value: (_ for _ in ()).throw(AssertionError("must not park")),
    )
    monkeypatch.setattr(algorithm, "_deps", lambda: SimpleNamespace(cancelled=lambda: True))
    assert algorithm._node_synthesis_wait({}) == {"cancelled": True}


def test_the_section_writer_reads_and_writes_nothing_else():
    """Its surface is the reader of the memo's evidence, its result the prose,
    and it writes on the node that writes a section in one call."""
    from inqtrix.agents.phase_models import SectionText

    profile = resolve_work_profile(SECTION_WRITER)
    assert profile.allowed_tools == frozenset({"read_evidence"})
    assert profile.result_model is SectionText
    assert profile.routing_node == "agent_synthesis"


# -- recovery, prose guard and role rows -------------------------------------------------


def test_a_synthesis_already_completed_is_restored_and_routed_onward(monkeypatch):
    """A crash after the task was stored completed, before the node's
    checkpoint, must not send the restored synthesis back to its wait node.

    The checkpoint the resume starts from is the one the wake committed, so
    it still says "parked". Restored without lowering that, the router sent
    the synthesis back to the wait node; with no role left running the store
    woke it at once, and the run went round synthesize/wait forever, never
    reaching the review or finalize.
    """
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.control_ports import PlanRecord, PlanTaskRecord, session_memo_artifact_id

    visits: list[str] = []
    real_synthesize, real_wait = algorithm._node_synthesize, algorithm._node_synthesis_wait

    def visiting(name: str, node: Any) -> Any:
        def run(state: Any) -> Any:
            visits.append(name)
            return node(state)
        return run

    monkeypatch.setattr(algorithm, "_node_synthesize", visiting("synthesize", real_synthesize))
    monkeypatch.setattr(algorithm, "_node_synthesis_wait", visiting("synthesis_wait", real_wait))
    monkeypatch.setattr(algorithm, "_node_critic", lambda state: (visits.append("critic"), {})[1])
    monkeypatch.setattr(algorithm, "_node_finalize", lambda state: (visits.append("finalize"), {})[1])

    control = MemoryAgentControlStore()
    run_id = "run_restored"
    task = PlanTaskRecord(
        task_id="s", plan_id="plan-a", run_id=run_id, ordinal=0, title="s",
        tool_kind="synthesis", queries=("q",), status="completed",
    )
    asyncio.run(control.save_plan(
        run_id=run_id,
        plan=PlanRecord(plan_id="plan-a", run_id=run_id, version=1, status="approved", created_by="agent"),
        tasks=[task],
    ))
    artifact_id = session_memo_artifact_id(None, run_id)
    asyncio.run(control.upsert_artifact(
        run_id=run_id, kind="memo", session_id=None, title="T", status="writing",
        content_markdown="# T\n\nText", payload={}, refs=[], updated_by="agent",
        artifact_id=artifact_id,
    ))
    deps = SimpleNamespace(
        control=control, context=SimpleNamespace(run_id=run_id),
        emit=lambda *args, **kwargs: None, cancelled=lambda: False, tier_policy=None,
    )
    graph = algorithm._build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": run_id}}
    token = algorithm._DEPS.set(deps)
    try:
        # The checkpoint the wake committed: next is synthesize, still parked.
        graph.update_state(config, {
            "question": "q", "deliverable": "canvas", "replan_rounds": 0,
            "artifact_id": artifact_id, "synthesis_pending": True,
            "synthesis_progress": {"round": 1, "outline": {}, "sections": {}, "folded": 0},
            "cancelled": False,
        }, as_node="synthesis_wait")
        out = graph.invoke(None, config)
        for _ in range(2):
            if not out or not out.get("__interrupt__"):
                break
            out = graph.invoke(Command(resume={"kind": "children"}), config)
    finally:
        algorithm._DEPS.reset(token)
    assert "synthesis_wait" not in visits, visits
    assert visits[0] == "synthesize" and visits[-1] == "finalize", visits


def _one_role_section(monkeypatch, **llm_kwargs: Any):
    """A mission whose first section goes to the role; its run's events."""
    first, _ = (entry["title"] for entry in OUTLINE["sections"])
    monkeypatch.setattr(synthesis, "choose_section_transport", _role_for({first}))
    llm = SectionRoleLLM(
        overrides={"ReportOutline": _outline_with_two_led_sections}, **llm_kwargs
    )
    client = make_agent_client(monkeypatch, llm=llm, platform=KERNEL)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        events = run_events(client, run_id)
    return first, summary, events


def test_the_role_s_prose_is_checked_for_every_id_the_role_could_see(monkeypatch):
    """A role reaches the whole ledger, so its check takes the whole ledger.

    Its work order names the preselection by ledger id and read_evidence
    renders the ledger with its provenance - and a mission reference often
    carries no id of its own, so a check against the references' own
    ``reference_id`` compared the prose with empty strings and let a copied
    id through to the reader. The provenance a role reads is not the ledger
    id alone: the search, citation and origin ids of a web record are in
    that same record, and each is copyable.
    """
    seen: list[list[str]] = []
    real_finish = synthesis.finish_section

    def spy(llm: Any, markdown: str, **kwargs: Any) -> Any:
        if markdown.startswith("Die Rolle schreibt"):
            seen.append(list(kwargs["internal_reference_ids"]))
        return real_finish(llm, markdown, **kwargs)

    monkeypatch.setattr(synthesis, "finish_section", spy)
    first, summary, _ = _one_role_section(monkeypatch)
    assert summary["status"] == "completed", summary
    assert seen, "the role's prose never reached the closing stage"
    ids = seen[0]
    assert ids and all(ids), ids
    assert any(value.startswith("ref_") for value in ids), ids
    # The lineage the web record carries beside its ledger id.
    assert {value.split("_", 1)[0] for value in ids} >= {
        "ref", "src", "query", "cit", "run",
    }, ids


def test_a_role_row_closes_when_the_closing_stage_fails(monkeypatch):
    """Whatever ends a section ends its row: no started row without a finish.

    The failure is raised in the ROLE's closing stage only - a failure in a
    sibling written in one call would end the run before the role returns,
    and the row would never reach the stage this test is about.
    """
    real_finish = synthesis.finish_section

    def failing_for_the_role(llm: Any, markdown: str, **kwargs: Any) -> Any:
        if markdown.startswith("Die Rolle schreibt"):
            raise RuntimeError("closing stage down")
        return real_finish(llm, markdown, **kwargs)

    monkeypatch.setattr(synthesis, "finish_section", failing_for_the_role)
    _, summary, events = _one_role_section(monkeypatch)
    assert summary["status"] == "failed"
    rows = [
        (event["type"].rsplit(".", 1)[1], event["data"].get("status"))
        for event in events if ".tool." in event["type"]
        and event["data"].get("invocation_id") == "section:1:1"
    ]
    assert rows == [("started", None), ("finished", "error")], rows


def _role_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event["data"] for event in events
        if event["type"].endswith("tool.finished")
        and event["data"].get("invocation_id") == "section:1:1"
    ]


def test_a_role_row_counts_what_the_role_read_in_the_run_language(monkeypatch):
    """The preview is a localized count; it once read "1 source(s) read"."""
    _, summary, events = _one_role_section(monkeypatch)
    assert summary["status"] == "completed", summary
    assert [row["result_preview"] for row in _role_rows(events)] == ["1 Beleg nachgelesen"]


def test_a_failed_role_row_marks_a_shortened_reason(monkeypatch):
    """A role that cannot start keeps its reason on the row, bounded - and a
    cut reason says it was cut instead of ending mid-word."""
    from inqtrix.agents.kernel import tools as kernel_tools

    reason = "Die Rolle konnte nicht gestartet werden, weil " + "der Dienst nicht antwortet " * 20
    monkeypatch.setattr(kernel_tools, "submit_or_find_child", lambda scope, **kwargs: reason)
    _, summary, events = _one_role_section(monkeypatch)
    assert summary["status"] == "completed", summary
    [row] = _role_rows(events)
    assert row["status"] == "error"
    preview = row["result_preview"]
    assert len(preview) <= 200 and preview.endswith(" ...")
    assert reason.startswith(preview[: -len(" ...")])


def test_a_section_role_is_priced_like_the_mission_that_ordered_it(monkeypatch):
    """The Stufe travels to EVERY role, not only to the reviewing one.

    A section role that submits with no overrides resolves against the
    server default, so the budget its brief now prices would have nothing
    to move: floor and ceiling would meet at the normal allowance whatever
    the section addresses. The mission states its Stufe on the child, the
    same way the review does.
    """
    from inqtrix.agents.kernel import tools as kernel_tools

    monkeypatch.setattr(synthesis, "SECTION_IN_PROMPT_BUDGET_TOKENS", 1)
    seen: list[dict[str, object]] = []
    real = kernel_tools.submit_or_find_child

    def spy(scope, **kwargs):
        seen.append(dict(kwargs))
        return real(scope, **kwargs)

    monkeypatch.setattr(kernel_tools, "submit_or_find_child", spy)
    llm = SectionRoleLLM(overrides={"ReportOutline": _outline_with_two_led_sections})
    client = make_agent_client(monkeypatch, llm=llm, platform=KERNEL)
    with client:
        run_id = submit_agent(
            client,
            autonomy="autonomous",
            agent_overrides={"agent_tier": "tief"},
        )
        summary = wait_status(client, run_id, {"completed", "failed"})
    assert summary["status"] == "completed", summary
    assert seen, "no section role was submitted"
    # Every section role carries the mission's own price class.
    assert all(call["agent_overrides"] for call in seen), [
        call["agent_overrides"] for call in seen
    ]
