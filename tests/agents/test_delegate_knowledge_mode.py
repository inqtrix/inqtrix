"""Knowledge assignments in the agent's batch of subtasks.

``delegate_batch`` takes assignments in mode ``knowledge``: each runs as a
child of the knowledge run mode, with the knowledge profile of the parent's
tier, inside the parent's collection boundary and with the model the user
chose. An assignment that cannot run starts no child of the whole call.
What comes back as evidence is what the child's answer cites, with the
notes of its search, and the documents its result names bind the parent's
transcript.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import inqtrix.agents.kernel.algorithm as kernel_algorithm
import inqtrix.research.web_research as web_research_module
from inqtrix.agents.context_sources import MATERIAL_DEPENDENCIES
from inqtrix.agents.kernel.algorithm import _checkpointed_tool_use_counts, _knowledge_scope_names
from inqtrix.agents.kernel.deps import set_kernel_deps
from inqtrix.agents.kernel.policy import interrupt_config_for
from inqtrix.agents.kernel.tools import (
    SOURCE_TOOL_FAILURE_PREFIXES,
    SOURCE_USES,
    ChildScope,
    knowledge_child_refusal,
    submit_or_find_child,
)
from inqtrix.agents.source_policy import kernel_tool_allowed, require_kernel_tool_allowed
from inqtrix.core.results import AgentResult, SourcePolicy
from tests.agents.test_kernel_child_runs import (
    ScriptedToolLLM,
    _text_turn,
    _tool_turn,
    delegated,
    fake_child_graph,
    make_client,
    wait_status,
)

_DUTIES = "Welche Pflichten nennt die Richtlinie für Betreiber?"
_DEADLINES = "Welche Fristen nennt die Richtlinie?"


def _reference(index: int, document: str) -> dict[str, Any]:
    text = f"Wortlaut der Richtlinie, Abschnitt {index}."
    return {
        "label": f"K{index}", "url": f"inqtrix://sources/{document}?chunk={index}", "tier": "primary",
        "title": f"Richtlinie Abschnitt {index}", "document_id": document, "chunk_index": index,
        "excerpt": text, "source_text": text,
    }


_REFERENCES = [_reference(1, "doc_a"), _reference(2, "doc_b"), _reference(3, "doc_c")]
_LIMIT = {
    "reason": "vector_overfetch_cap", "retrieval_mode": "hybrid", "stage": "vector_candidate_pool",
    "requested_candidate_pool": 60, "returned_candidate_pool": 20, "final_top_k": 8,
    "final_evidence_complete": False, "requested_top_k": 8, "returned_hits": 3,
}
_DUPLICATES = {"code": "duplicate_documents_collapsed", "reason": "duplicate_document", "stage": "retrieval", "count": 2}


class _Knowledge:
    """A stand-in for a run mode a child runs in, the knowledge run mode
    unless a subclass names another: it records what each child was asked
    and with which settings, and answers with what the test hands it."""

    id = "knowledge"
    display_name = "Knowledge"
    contract_version = 1

    def __init__(self, *, answer: str = "", retrieval: dict[str, Any] | None = None) -> None:
        self.answer = answer
        self.retrieval = retrieval or {"degradations": []}
        self.requests: list[Any] = []
        self.models: list[dict[str, str]] = []

    def capabilities(self) -> dict[str, Any]:
        return {}

    def run(self, request: Any, *, runtime: Any, context: Any) -> AgentResult:
        self.requests.append(request)
        self.models.append({
            key: getattr(context.agent_settings, key) for key in ("model", "model_tier", "effort")
        })
        answer = self.answer or f"Zu „{request.question}“: Punkt eins [K1], Punkt zwei [K3]."
        return AgentResult(answer=answer, raw={
            "answer": answer,
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "result_state": {
                "answer": answer,
                "round": 1,
                "report_references": [dict(reference) for reference in _REFERENCES],
                "all_citations": [reference["url"] for reference in _REFERENCES],
                "knowledge_retrieval": self.retrieval,
            },
        })


def _run(
    monkeypatch: pytest.MonkeyPatch,
    assignments: list[dict[str, Any]],
    *,
    knowledge: _Knowledge | None,
    tier: str = "tief",
    selection: dict[str, str] | None = None,
    source_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One agent run whose model sends one batch of *assignments*, through the API.

    With *knowledge* the server knows the knowledge run mode (a stand-in);
    without, it does not. Returns what the run left behind: the tool reply
    the model read, how the transcript settled the call, the children the
    model delegated, the research questions that started, the source uses
    the run counted, the tools the model was offered, and from the
    transcript the run archived: what the stored tool message carries
    beside its text, and the documents a later reader must still hold.
    """
    llm = ScriptedToolLLM([
        _tool_turn("call_knowledge_batch", "delegate_batch", {"assignments": assignments}),
        _text_turn("Antwort auf Basis der Unteraufträge."),
    ])
    client = make_client(monkeypatch, llm)
    researched: list[str] = []

    def recording_child_graph(question: str, **kwargs: Any) -> dict[str, Any]:
        researched.append(question)
        return fake_child_graph(question, **kwargs)

    monkeypatch.setattr(web_research_module, "run_web_graph", recording_child_graph)
    if knowledge is not None:
        monkeypatch.setitem(client.container.registry._items, "knowledge", knowledge)
    with client:
        response = client.post("/v1/runs", json={
            "question": "Was verlangt die Richtlinie?",
            "mode": "agent_kernel",
            "autonomy": "autonomous",
            "tool_directives": ["web_research"],
            "agent_overrides": {"agent_tier": tier, **(selection or {})},
            **({"source_policy": source_policy} if source_policy else {}),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = delegated(client.get(f"/v1/runs/{run_id}/children").json()["data"])
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
        descriptor, stored = _archived_transcript(client, run_id)
    replies = [
        str(message.get("content"))
        for call in llm.chat_calls[1:]
        for message in call["messages"]
        if message.get("role") == "tool"
    ]
    (batch,) = [
        message for message in stored
        if message.get("type") == "tool" and message.get("name") == "delegate_batch"
    ]
    return {
        "reply": replies[0] if replies else "",
        "settled": [
            event["data"]["status"] for event in events
            if event["type"] == "inqtrix.agent.tool.finished" and event["data"]["tool"] == "delegate_batch"
        ],
        "children": children,
        "researched": researched,
        "counts": summary["snapshot"]["execution"]["tool_use_counts"],
        "offered": sorted(tool["function"]["name"] for tool in (llm.chat_calls[0].get("tools") or [])),
        "artifact": dict(batch.get("artifact") or {}),
        "materials": descriptor["materials"],
    }


def _archived_transcript(client: Any, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The transcript a finished run archived: what binds it, and its messages.

    The archive is the stored copy of the run's checkpoint messages, the one
    a later run reads; its descriptor names what that reader must still hold.
    """
    store = client.container.agent_control_service.store
    (entry,) = [
        entry
        for entry in asyncio.run(store.list_context_archive_sources(run_id, include_private=True))
        if entry.transcript is not None
    ]
    body, offset, content_hash = "", None, ""
    while True:
        window = asyncio.run(store.read_context_archive(
            run_id, entry.source_id, offset=offset, content_hash=content_hash, include_private=True,
        ))
        body += window.text
        if window.next_offset is None:
            return entry.transcript, json.loads(body)["messages"]
        offset, content_hash = window.next_offset, window.content_hash


def _marks(reply: str) -> int:
    return sum(reply.count(prefix) for prefix in SOURCE_TOOL_FAILURE_PREFIXES)


def _sources(reply: str) -> list[str]:
    return [line for line in reply.partition("Sources:\n")[2].splitlines() if line.startswith("- ")]


@pytest.mark.parametrize(("tier", "profile"), [("tief", "gruendlich"), ("gruendlich", "standard")])
def test_a_knowledge_assignment_runs_as_a_knowledge_child_with_the_profile_of_the_tier(
    monkeypatch, tier: str, profile: str,
) -> None:
    """The profile of a run without one is ``standard``: a child of a deep
    run that was handed none would search with the wrong width."""
    knowledge = _Knowledge()
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=knowledge, tier=tier)
    assert [(child["mode"], child["status"]) for child in run["children"]] == [("knowledge", "completed")]
    (request,) = knowledge.requests
    assert (request.question, request.knowledge_filters.get("profile")) == (_DUTIES, profile)
    assert run["settled"] == ["success"]


def test_a_knowledge_child_works_with_the_model_the_user_chose(monkeypatch) -> None:
    """The mission answered its knowledge questions with the model of the
    run; a child on the stack's default would answer with another."""
    knowledge = _Knowledge()
    _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=knowledge,
         selection={"model_tier": "mid", "effort": "low"})
    assert knowledge.models == [{"model": "", "model_tier": "mid", "effort": "low"}]


def test_each_assignment_of_a_mixed_batch_runs_as_one_child_and_reports_in_its_place(monkeypatch) -> None:
    """Three assignments are three children, each asked once, whatever their
    mode, and the reports come back in the order of the assignments. (That
    the children of one batch run at the same time is seen at the running
    stack, not here.)"""
    knowledge = _Knowledge()
    run = _run(monkeypatch, [
        {"objective": _DUTIES, "mode": "knowledge"},
        {"objective": "Aktuelle Urteile zur Richtlinie.", "mode": "research", "recency": "month"},
        {"objective": _DEADLINES, "mode": "knowledge"},
    ], knowledge=knowledge)
    assert sorted(child["mode"] for child in run["children"]) == ["knowledge", "knowledge", "research"]
    assert sorted(request.question for request in knowledge.requests) == sorted([_DUTIES, _DEADLINES])
    assert run["researched"] == ["Aktuelle Urteile zur Richtlinie."]
    blocks = run["reply"].split("\n\n## ")
    assert [block.removeprefix("## ").split("\n")[0] for block in blocks] == ["Subtask 1", "Subtask 2", "Subtask 3"]
    assert [_DUTIES in blocks[0], "Kindbericht zu: Aktuelle Urteile" in blocks[1], _DEADLINES in blocks[2]] == [True] * 3


def test_all_assignments_of_a_batch_are_submitted_before_its_one_park(monkeypatch) -> None:
    """The batch submits every child, then parks once: children of one batch
    run at the same time. Parking after the first submission would end in
    the same children and report, one after the other."""
    import inqtrix.agents.kernel.tools as kernel_tools

    submitted: list[str] = []
    seen_at_park: list[int] = []
    submit = kernel_tools.submit_or_find_child
    park = kernel_tools.interrupt

    def counting_submit(*args: Any, **kwargs: Any) -> Any:
        submitted.append(str(kwargs.get("origin_key")))
        return submit(*args, **kwargs)

    def counting_park(value: Any) -> Any:
        seen_at_park.append(len(submitted))
        return park(value)

    monkeypatch.setattr(kernel_tools, "submit_or_find_child", counting_submit)
    monkeypatch.setattr(kernel_tools, "interrupt", counting_park)

    run = _run(monkeypatch, [
        {"objective": _DUTIES, "mode": "knowledge"},
        {"objective": _DEADLINES, "mode": "knowledge"},
    ], knowledge=_Knowledge())

    assert sorted(child["mode"] for child in run["children"]) == ["knowledge", "knowledge"]
    assert seen_at_park and seen_at_park[0] == 2


def test_a_server_without_the_knowledge_mode_starts_no_child_of_the_batch(monkeypatch) -> None:
    """Asked to resolve a mode it does not know, the resolver raises and
    would end the parent run; the batch asks first and refuses as a failed
    tool step, before the research child of the same call is submitted."""
    run = _run(monkeypatch, [
        {"objective": "Aktuelle Urteile zur Richtlinie.", "mode": "research"},
        {"objective": _DUTIES, "mode": "knowledge"},
    ], knowledge=None)
    assert (run["children"], run["researched"], run["settled"]) == ([], [], ["error"])
    assert run["reply"] == (
        "Tool unavailable: assignment 2: project knowledge is not set up on this server. "
        "No subtask was started."
    )
    assert _marks(run["reply"]) == 1


def test_a_run_with_knowledge_switched_off_starts_no_knowledge_child(monkeypatch) -> None:
    knowledge = _Knowledge()
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=knowledge,
               source_policy={"web": "available", "knowledge": "disabled"})
    assert (run["children"], knowledge.requests, run["settled"]) == ([], [], ["error"])
    assert run["reply"].startswith("Tool blocked: assignment 1: ")
    assert run["reply"].endswith(" No subtask was started.")
    assert _marks(run["reply"]) == 1


def _scope(
    *, collections: frozenset[str] | None, executes: bool | None, run_service: Any = None, resolver: Any = None,
) -> ChildScope:
    if resolver is None and executes is not None:
        resolver = SimpleNamespace(executes=lambda mode: executes and mode == "knowledge")
    return ChildScope(
        run_id="run_parent", run_service=run_service or object(), resolver=resolver, principal=None, workspace_id=None,
        session_id="", stack_name="", source_policy=SourcePolicy(), model_overrides={},
        knowledge_collection_ids=collections, knowledge_scope_explicit=False, knowledge_scope=None,
        tier="tief", depth="",
    )


@pytest.mark.parametrize(
    ("collections", "executes", "refusal"),
    [
        (None, True, None),
        (frozenset({"col_1"}), True, None),
        (frozenset(), True, "Tool blocked: this run has no knowledge collection in reach."),
        (frozenset({"col_1"}), False, "Tool unavailable: project knowledge is not set up on this server."),
        (frozenset({"col_1"}), None, "Tool unavailable: project knowledge is not set up on this server."),
    ],
    ids=["no boundary", "collections", "empty boundary", "mode not set up", "no resolver"],
)
def test_a_knowledge_child_needs_a_collection_in_reach_and_the_knowledge_mode(
    collections: frozenset[str] | None, executes: bool | None, refusal: str | None,
) -> None:
    """With an explicitly empty boundary the child would answer "nothing
    relevant" without a search: the agent would read an absence nobody
    looked for."""
    assert knowledge_child_refusal(_scope(collections=collections, executes=executes)) == refusal


def test_a_knowledge_child_gets_the_collections_of_its_parent_beside_its_profile() -> None:
    """The profile and the boundary travel in one field of the child's
    request. A child that got the profile alone would have no collection
    to search; one that got the boundary alone, the wrong width."""
    submitted: dict[str, Any] = {}

    class _Resolver:
        def resolve(self, payload: dict[str, Any]) -> Any:
            return SimpleNamespace(knowledge_filters=dict(payload.get("knowledge_filters") or {}))

    class _Runs:
        run_store = SimpleNamespace(children=lambda run_id: [])

        def submit(self, **arguments: Any) -> dict[str, Any]:
            submitted.update(arguments)
            return {"run_id": "run_child"}

    scope = _scope(collections=frozenset({"col_b", "col_a"}), executes=True, run_service=_Runs(), resolver=_Resolver())
    child = submit_or_find_child(
        scope, origin_key="call:0", mode="knowledge", question=_DUTIES, agent_overrides={},
        autonomy="autonomous", knowledge_profile="gruendlich",
    )
    filters = submitted["resolved"].knowledge_filters
    assert child == {"run_id": "run_child"}
    assert (filters["profile"], filters["collection_ids"]) == ("gruendlich", ["col_a", "col_b"])


def test_a_time_window_on_a_knowledge_assignment_starts_no_child(monkeypatch) -> None:
    knowledge = _Knowledge()
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge", "recency": "week"}], knowledge=knowledge)
    assert (run["children"], run["settled"]) == ([], ["error"])
    assert run["reply"] == (
        "Tool error (subtask.assignment_invalid): assignment 1: recency applies to a research assignment only; "
        "a knowledge assignment takes none. Leave it out. No subtask was started."
    )


@pytest.mark.parametrize("mode", ["wissen", "deep_mission"])
def test_a_mode_the_batch_does_not_know_names_the_two_it_takes(monkeypatch, mode: str) -> None:
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": mode}], knowledge=_Knowledge())
    assert run["children"] == []
    assert run["reply"] == (
        "Tool error (subtask.assignment_invalid): assignment 1: objective and mode (research | knowledge) "
        "are mandatory. No subtask was started."
    )


@pytest.mark.parametrize(
    ("web", "knowledge", "allowed"),
    [("available", "available", True), ("disabled", "available", True),
     ("available", "disabled", True), ("disabled", "disabled", False)],
)
def test_the_batch_is_offered_while_the_web_or_project_knowledge_is_available(web: str, knowledge: str, allowed: bool) -> None:
    """Listed as a web tool, the batch vanished with the web although its
    knowledge assignments need none; listed nowhere, it would stay with
    both sources switched off."""
    policy = SourcePolicy(web=web, knowledge=knowledge)
    assert kernel_tool_allowed("delegate_batch", policy=policy) is allowed


def test_a_batch_refused_for_its_sources_names_both_switches() -> None:
    """The model reads the reason. "The web is off" would send it looking
    for a knowledge assignment that is refused as well."""
    nothing = SourcePolicy(web="disabled", knowledge="disabled")
    with pytest.raises(PermissionError, match="web=disabled and source_policy.knowledge=disabled"):
        require_kernel_tool_allowed("delegate_batch", policy=nothing)


def test_a_knowledge_batch_runs_with_the_web_switched_off_and_a_research_assignment_does_not(monkeypatch) -> None:
    knowledge = _Knowledge()
    offline = {"web": "disabled", "knowledge": "available"}
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=knowledge, source_policy=offline)
    assert ("delegate_batch" in run["offered"], [child["mode"] for child in run["children"]]) == (True, ["knowledge"])
    refused = _run(monkeypatch, [
        {"objective": _DUTIES, "mode": "knowledge"},
        {"objective": "Aktuelle Urteile zur Richtlinie.", "mode": "research"},
    ], knowledge=_Knowledge(), source_policy=offline)
    assert (refused["children"], refused["researched"], refused["settled"]) == ([], [], ["error"])
    assert refused["reply"].startswith("Tool blocked: assignment 2: ") and _marks(refused["reply"]) == 1


@pytest.mark.parametrize(
    ("modes", "counts"),
    [
        (["knowledge"], {"web": 0, "knowledge": 1}),
        (["knowledge", "knowledge"], {"web": 0, "knowledge": 1}),
        (["research"], {"web": 1, "knowledge": 0}),
        (["research", "knowledge"], {"web": 1, "knowledge": 1}),
    ],
    ids=["knowledge", "two knowledge", "research", "both"],
)
def test_a_batch_counts_each_source_it_reaches_once(monkeypatch, modes: list[str], counts: dict[str, int]) -> None:
    """The counters arm the sufficiency check and show what a run used; a
    knowledge batch counted as web would say the run searched the web."""
    run = _run(monkeypatch, [
        {"objective": f"{_DUTIES} ({index})", "mode": mode} for index, mode in enumerate(modes)
    ], knowledge=_Knowledge())
    assert run["counts"] == counts
    assert run["artifact"][SOURCE_USES] == sorted(source for source, count in counts.items() if count)


@pytest.mark.parametrize(
    ("artifact", "content", "counts"),
    [
        ({SOURCE_USES: ["knowledge"]}, "## Subtask 1", {"web": 0, "knowledge": 1}),
        ({SOURCE_USES: ["knowledge", "web"]}, "## Subtask 1", {"web": 1, "knowledge": 1}),
        ({SOURCE_USES: []}, "## Subtask 1", {"web": 0, "knowledge": 0}),
        (None, "## Subtask 1", {"web": 1, "knowledge": 0}),
        ({SOURCE_USES: ["knowledge"]}, "Tool blocked: assignment 1: no. No subtask was started.", {"web": 0, "knowledge": 0}),
        ({SOURCE_USES: ["editor", 7, "knowledge"]}, "## Subtask 1", {"web": 0, "knowledge": 1}),
    ],
    ids=["knowledge", "both", "missions only", "parked before the artifact", "refused", "names no counter has"],
)
def test_a_resumed_run_counts_a_batch_as_the_running_one_did(
    artifact: dict[str, Any] | None, content: str, counts: dict[str, int],
) -> None:
    """After a halt the counters are rebuilt from the stored tool messages.
    A batch stored before it named its sources counts as it always did: one
    use of the web. A stored name no counter stands for counts nothing and
    must not end the run that resumes."""
    snapshot = SimpleNamespace(values={"messages": [
        HumanMessage(content="Frage"),
        AIMessage(content="", tool_calls=[{"id": "b", "name": "delegate_batch", "args": {}}]),
        ToolMessage(content=content, tool_call_id="b", name="delegate_batch", artifact=artifact),
    ]})
    assert _checkpointed_tool_use_counts(snapshot) == counts


def test_only_what_the_child_cites_becomes_evidence_of_the_parent(monkeypatch) -> None:
    """The child used three passages and cites two. A knowledge run marks
    all three as cited; handed on unread, the third would be evidence the
    parent could cite although no answer relies on it."""
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=_Knowledge())
    sources = _sources(run["reply"])
    assert len(sources) == 2
    assert ["Abschnitt 1" in sources[0], "Abschnitt 3" in sources[1], "Abschnitt 2" in run["reply"]] == [True, True, False]


def test_what_the_answer_cites_after_the_end_of_its_summary_stays_evidence(monkeypatch) -> None:
    """The parent reads the first 300 words of the child's answer. What the
    answer cites later is still what the child relied on, as with every
    child: dropped with the cut, it would be lost without a trace."""
    long_answer = "Punkt eins [K1]. " + "Weiteres Wort. " * 200 + "Punkt drei am Ende [K3]."
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=_Knowledge(answer=long_answer))
    sources = _sources(run["reply"])
    assert "Punkt drei am Ende" not in run["reply"]
    assert ["Abschnitt 1" in sources[0], "Abschnitt 3" in sources[1], len(sources)] == [True, True, 2]


_CUT_SHORT = "- The knowledge search hit a technical candidate limit (in one search) and returned 3 of 8 requested hits"
_SKIPPED = "- 2 hits were skipped (in one search"


@pytest.mark.parametrize(
    ("retrieval", "header", "lines"),
    [
        ({"degradations": [_LIMIT]}, "Search note – the hit list is no proof of completeness:", [_CUT_SHORT]),
        ({"degradations": [], "warnings": [_DUPLICATES]}, "Search note:", [_SKIPPED]),
        ({"degradations": [_LIMIT], "warnings": [_DUPLICATES]},
         "Search note – the hit list is no proof of completeness:", [_CUT_SHORT, _SKIPPED]),
        ({"degradations": []}, "", []),
    ],
    ids=["a limit", "a note", "both", "nothing"],
)
def test_the_notes_of_the_child_search_reach_the_model_of_the_parent(
    monkeypatch, retrieval: dict[str, Any], header: str, lines: list[str],
) -> None:
    """A hit list the search cut short reads as complete unless the report
    says so, with what was asked for and what came back; only a limit may
    say that the list proves no completeness."""
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=_Knowledge(retrieval=retrieval))
    assert run["settled"] == ["success"]
    assert ("Search note" in run["reply"]) is bool(header)
    assert ("no proof of completeness:" in run["reply"]) is (_CUT_SHORT in lines)
    noted = run["reply"].partition(f"{header}\n")[2].split("\n\n")[0].splitlines() if header else []
    assert [line[:len(expected)] for line, expected in zip(noted, lines, strict=True)] == lines


_USED = [{"document_id": document, "collection_id": ""} for document in ("doc_a", "doc_b", "doc_c")]


def test_the_documents_a_knowledge_child_answered_from_bind_the_transcript(monkeypatch) -> None:
    """The report carries text written from project documents. A later run
    may read this transcript only while those documents are still its to
    read: all the child answered from, not only the two it cites."""
    run = _run(monkeypatch, [
        {"objective": _DUTIES, "mode": "knowledge"},
        {"objective": "Aktuelle Urteile zur Richtlinie.", "mode": "research"},
    ], knowledge=_Knowledge())
    assert len(_sources(run["reply"].split("\n\n## ")[0])) == 2
    assert run["artifact"][MATERIAL_DEPENDENCIES] == {"knowledge_documents": _USED}
    assert run["materials"]["knowledge_documents"] == _USED


@pytest.mark.parametrize(
    "answer",
    ["Die Richtlinie verlangt zwei Wochen Vorlauf.", "Zwei Wochen Vorlauf [K1: Abschnitt 1]."],
    ids=["no label", "a label this code cannot read"],
)
def test_an_answer_that_cites_nothing_readable_still_binds_its_documents(monkeypatch, answer: str) -> None:
    """Without a citation no evidence is handed on, but the text is, and it
    was written from the documents: unbound, it would outlive them."""
    run = _run(monkeypatch, [{"objective": _DUTIES, "mode": "knowledge"}], knowledge=_Knowledge(answer=answer))
    assert (_sources(run["reply"]), "wochen vorlauf" in run["reply"].lower()) == ([], True)
    assert run["materials"]["knowledge_documents"] == _USED


def _gates(
    assignments: Any, *, grants: set[str], explicit: bool, knowledge_batch: bool = False, **arguments: Any,
) -> bool:
    """Whether Standard asks before a batch of *assignments*, sent with further *arguments*.

    *knowledge_batch*: the user granted, for the run, a batch whose card
    named the collections.
    """
    when = interrupt_config_for("balanced")["delegate_batch"]["when"]
    try:
        set_kernel_deps(SimpleNamespace(
            tool_grants=frozenset(grants), knowledge_scope_explicit=explicit, knowledge_batch_granted=knowledge_batch,
        ))
        return when(SimpleNamespace(tool_call={"args": {"assignments": assignments, **arguments}}))
    finally:
        set_kernel_deps(None)


_KNOWLEDGE_BATCH = [{"objective": _DUTIES, "mode": "knowledge"}]
_RESEARCH_BATCH = [{"objective": "Aktuelle Urteile.", "mode": "research"}]


@pytest.mark.parametrize(
    ("assignments", "grants", "explicit", "asks"),
    [
        (_RESEARCH_BATCH, set(), False, True),
        (_RESEARCH_BATCH, {"delegate_batch"}, False, False),
        (_KNOWLEDGE_BATCH, set(), True, True),
        (_KNOWLEDGE_BATCH, {"delegate_batch"}, True, False),
        (_KNOWLEDGE_BATCH, {"delegate_batch"}, False, True),
        (_RESEARCH_BATCH + _KNOWLEDGE_BATCH, {"delegate_batch"}, False, True),
        (_KNOWLEDGE_BATCH, {"delegate_batch", "search_project_knowledge"}, False, False),
        (_KNOWLEDGE_BATCH, {"search_project_knowledge"}, False, True),
    ],
    ids=[
        "research, no grant", "research, granted", "knowledge in a chosen scope, no grant",
        "knowledge in a chosen scope, granted", "knowledge in a scope nobody chose, batch granted",
        "mixed in a scope nobody chose, batch granted", "both granted", "only the knowledge search granted",
    ],
)
def test_a_grant_for_the_run_covers_no_knowledge_assignment_in_a_scope_the_user_did_not_choose(
    assignments: list[dict[str, Any]], grants: set[str], explicit: bool, asks: bool,
) -> None:
    """A grant given at a batch of web research would otherwise let a later
    batch search project knowledge in a scope the user never chose, which
    the agent's own knowledge search asks about. A scope the user chose
    (collections, or the switch for everything visible) asks nothing more."""
    assert _gates(assignments, grants=grants, explicit=explicit) is asks


@pytest.mark.parametrize(
    "assignments", [_KNOWLEDGE_BATCH, _RESEARCH_BATCH + _KNOWLEDGE_BATCH], ids=["knowledge", "mixed"],
)
def test_a_run_grant_at_a_card_that_named_the_collections_covers_later_knowledge_batches(
    assignments: list[dict[str, Any]],
) -> None:
    """The user approved, for the run, a batch whose card named the
    collections its assignments search: a later batch searching that
    boundary asks nothing more, as the button said."""
    assert _gates(assignments, grants={"delegate_batch"}, explicit=False, knowledge_batch=True) is False


def _two_batches(monkeypatch: pytest.MonkeyPatch, first: list[dict[str, Any]], second: list[dict[str, Any]]) -> dict[str, Any]:
    """A Standard run over a scope nobody chose whose model sends two batches, through the API.

    The user grants the first batch for the run. Returns how the run went
    on: its status after the grant, the card of the first batch and the
    approvals the run asked for.
    """
    llm = ScriptedToolLLM([
        _tool_turn("call_batch_one", "delegate_batch", {"assignments": first}),
        _tool_turn("call_batch_two", "delegate_batch", {"assignments": second}),
        _text_turn("Antwort auf Basis der Unteraufträge."),
    ])
    client = make_client(monkeypatch, llm)
    monkeypatch.setitem(client.container.registry._items, "knowledge", _Knowledge())
    # The test server has no collection catalog; the card names the
    # collections by the real rule, read from a stand-in catalog.
    naming = kernel_algorithm._knowledge_scope_names
    monkeypatch.setattr(kernel_algorithm, "_knowledge_scope_names", lambda _deps, actions: naming(
        SimpleNamespace(capability_registry=_Catalog(["Richtlinien"]), capability_context=None), actions,
    ))
    with client:
        response = client.post("/v1/runs", json={
            "question": "Was verlangt die Richtlinie?",
            "mode": "agent_kernel",
            "autonomy": "balanced",
            "tool_directives": ["web_research"],
            "agent_overrides": {"agent_tier": "tief"},
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})
        (card,) = [row for row in client.get(f"/v1/runs/{run_id}/approvals").json()["data"] if row["status"] == "pending"]
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{card['approval_id']}", json={"decision": "approve", "approval_scope": "run"},
        )
        assert decided.status_code == 200, decided.text
        summary = wait_status(client, run_id, {"completed", "waiting_for_approval", "failed"})
        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
    return {"status": summary["status"], "card": card, "approvals": len(approvals)}


def test_a_run_grant_at_a_knowledge_batch_covers_the_next_knowledge_batch(monkeypatch) -> None:
    """Granted at a card that named the collections, the next batch over
    the same boundary runs without a second card."""
    run = _two_batches(monkeypatch, _KNOWLEDGE_BATCH, [{"objective": _DEADLINES, "mode": "knowledge"}])
    assert "knowledge_scope" in run["card"]["payload"]
    assert (run["status"], run["approvals"]) == ("completed", 1)


def test_a_run_grant_at_a_research_batch_still_asks_at_a_knowledge_batch(monkeypatch) -> None:
    """A card of research alone named no collections: its grant for the run
    covers no knowledge assignment, which asks."""
    run = _two_batches(monkeypatch, _RESEARCH_BATCH, _KNOWLEDGE_BATCH)
    assert "knowledge_scope" not in run["card"]["payload"]
    assert (run["status"], run["approvals"]) == ("waiting_for_approval", 2)


def test_a_knowledge_batch_grant_leaves_the_agents_own_search_asking() -> None:
    """The grant covers batches. The agent's own search over a scope
    nobody chose still asks for itself."""
    when = interrupt_config_for("balanced")["search_project_knowledge"]["when"]
    try:
        set_kernel_deps(SimpleNamespace(
            tool_grants=frozenset({"delegate_batch"}), knowledge_scope_explicit=False, knowledge_batch_granted=True,
        ))
        assert when(SimpleNamespace(tool_call={"args": {"query": "Pflichten"}})) is True
    finally:
        set_kernel_deps(None)


def test_no_argument_of_the_call_silences_the_question(monkeypatch) -> None:
    """The agent's own knowledge search asks nothing once the call names
    its collections. A batch takes none: a model that added them to the
    call would otherwise search a scope nobody chose, unasked."""
    assert _gates(_KNOWLEDGE_BATCH, grants={"delegate_batch"}, explicit=False, collection_ids=["col_1"]) is True


@pytest.mark.parametrize("mode", [" knowledge ", "knowledge\n"])
def test_the_gate_reads_the_mode_of_an_assignment_as_the_tool_does(monkeypatch, mode: str) -> None:
    """The tool reads a mode without the spaces around it. A gate that read
    it literally would take the assignment for no knowledge assignment and
    let it pass the question."""
    padded = [{"objective": _DUTIES, "mode": mode}]
    run = _run(monkeypatch, padded, knowledge=_Knowledge())
    assert [child["mode"] for child in run["children"]] == ["knowledge"]
    assert _gates(padded, grants={"delegate_batch"}, explicit=False) is True


@pytest.mark.parametrize("assignments", ["knowledge", [None, "knowledge"], None], ids=["text", "no objects", "nothing"])
def test_arguments_the_tool_will_refuse_ask_nothing_more(assignments: Any) -> None:
    """The gate reads the call before the tool checks its form. Arguments
    that hold no assignment the gate can read name no knowledge assignment:
    they owe no further question, and they must not end the run at the
    gate."""
    assert _gates(assignments, grants={"delegate_batch"}, explicit=False) is False
    assert _gates(assignments, grants=set(), explicit=False) is True


class _Catalog:
    """A collection catalog that records whether it was asked."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.asked = 0

    async def invoke(self, capability_id: str, payload: dict[str, Any], _context: object) -> Any:
        self.asked += 1
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in self.names])


@pytest.mark.parametrize(
    ("assignments", "names", "asked"),
    [
        (_KNOWLEDGE_BATCH, ["Richtlinien", "Verträge"], 1),
        (_RESEARCH_BATCH + _KNOWLEDGE_BATCH, ["Richtlinien", "Verträge"], 1),
        (_RESEARCH_BATCH, None, 0),
    ],
    ids=["knowledge", "mixed", "research"],
)
def test_the_card_of_a_batch_with_a_knowledge_assignment_names_the_collections(
    assignments: list[dict[str, Any]], names: list[str] | None, asked: int,
) -> None:
    """The user decides about a search in project knowledge; the card says
    in which collections, as it does for the agent's own knowledge search."""
    catalog = _Catalog(["Verträge", "Richtlinien"])
    deps = SimpleNamespace(capability_registry=catalog, capability_context=None)
    action = {"tool": "delegate_batch", "args": {"assignments": assignments}}
    assert (_knowledge_scope_names(deps, [action]), catalog.asked) == (names, asked)
