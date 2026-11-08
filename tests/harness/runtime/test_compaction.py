"""The kernel's own compaction keeps the run's contracts, in both hooks.

The thread is never rewritten; the model sees a computed view. A compaction
archives what it evicts, folds the prior summary into the next one, keeps the
assignment, the user's answers and loaded skills outside the fence, and only
reports after the model call it served has returned. A failure before that
leaves no event and no record, and a retry meets the same archive section.

Before a summary, stage one replaces answered tool results that can be read
again with placeholders in the view; the summary follows only when that is
not enough, and a turn changes the cached prompt only where a result leaves
the newest few.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from copy import deepcopy

import pytest

pytest.importorskip("langchain")

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from inqtrix.agents.context_archive import archive_prefix, archived_result_id, archived_result_pointer
from inqtrix.agents.context_sources import SOURCE_COVERAGE, SourceCoverageRecord
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.agents.kernel.compaction import (
    CLEARED_RESULT_FLAG,
    COMPACTION_STATE,
    InqtrixCompactionMiddleware,
    RereadScope,
    narration_id,
    safe_cutoff,
    visible_view,
)
from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.telemetry import COMPACTION_PURPOSE, model_retry_activity
from inqtrix.exceptions import (
    AgentContextArchiveError,
    AgentModelCapacityError,
    AgentStructuredOutputError,
)
from inqtrix.harness.contracts.events import CONTEXT_COMPACTED_EVENT, NARRATION_EVENT
from inqtrix.observability.context import current_purpose
from inqtrix.providers.base import ChatTurn
from inqtrix.settings import AgentPlatformSettings
from inqtrix.untrusted_content import untrusted_fence
from tests.agents.test_harness_kernel_contract import _ScriptedProvider, _text_turn

BOTH_HOOKS = pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])


class _World:
    """One run's deps, its archive and events, and a scripted summary model."""

    def __init__(self, run_id: str, turns: list[ChatTurn], *, trigger: int = 1, budget: int = 0) -> None:
        self.provider = _ScriptedProvider(turns)
        self.model = build_tool_chat_model(self.provider)
        self.archives: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict]] = []
        self.deps = KernelDeps(
            run_id=run_id,
            control=None,
            platform=AgentPlatformSettings(),
            llm=self.provider,
            model=None,
            reasoning_effort=None,
            timeout=2.0,
            context_trigger_tokens=trigger,
            event_sink=lambda kind, payload: self.events.append((kind, payload)),
        )
        self.deps.context_input_budget = budget
        self.deps.append_context_archive = self._archive

    def _archive(self, title: str, text: str) -> str:
        self.archives.append((title, text))
        # Content-addressed like the run archive: the same text, the same id.
        return f"{self.deps.run_id}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"

    def compacted(self) -> list[dict]:
        return [payload for kind, payload in self.events if kind == CONTEXT_COMPACTED_EVENT]

    def narrations(self) -> list[dict]:
        return [payload for kind, payload in self.events if kind == NARRATION_EVENT]


def _messages(marker: str) -> list:
    return [
        HumanMessage(content=f"{marker} assignment: write the report"),
        AIMessage(
            content="",
            tool_calls=[{"id": f"{marker}c1", "name": "search", "args": {"q": "x"}}],
            usage_metadata={"input_tokens": 17, "output_tokens": 5, "total_tokens": 22},
        ),
        ToolMessage(content=f"{marker}RESULT " + "source content " * 400, tool_call_id=f"{marker}c1", name="search"),
        AIMessage(content=f"{marker} interim note"),
        HumanMessage(content="Continue."),
    ]


def _request(world: _World, messages: list, record=None, todos=None, tools=()) -> ModelRequest:
    state = {"messages": messages, "todos": todos or [{"content": "TASK-A", "status": "in_progress"}]}
    if record is not None:
        state[COMPACTION_STATE] = record
    return ModelRequest(model=world.model, messages=messages, state=state, tools=list(tools))


def _run(middleware, world: _World, request: ModelRequest, asynchronous: bool, seen: list):
    def handler(projected):
        seen.append(projected.messages)
        return ModelResponse(result=[AIMessage(content="Answer.")])

    async def async_handler(projected):
        return handler(projected)

    set_kernel_deps(world.deps)
    try:
        if asynchronous:
            return asyncio.run(middleware.awrap_model_call(request, async_handler))
        return middleware.wrap_model_call(request, handler)
    finally:
        set_kernel_deps(None)


@BOTH_HOOKS
def test_a_compaction_archives_the_evicted_history_and_leaves_the_state_alone(asynchronous):
    world = _World("runA", [_text_turn("Summary one.")])
    messages = _messages("A1")
    request = _request(world, messages)
    original = deepcopy(request.state)
    seen: list = []

    response = _run(InqtrixCompactionMiddleware(keep_messages=1), world, request, asynchronous, seen)

    assert request.state == original
    assert len(world.archives) == 1 and world.archives[0][0] == "Compacted history"
    assert "A1RESULT" in world.archives[0][1] and "A1 interim note" in world.archives[0][1]
    summary_input = world.provider.calls[0]["messages"][0]["content"]
    fenced = summary_input.split('<untrusted_content source="older-conversation">', 1)[1]
    assert "A1RESULT" in fenced.split("</untrusted_content>", 1)[0]
    record = response.command.update[COMPACTION_STATE]
    assert record["cutoff"] == 4 and record["generation"] == 1
    assert record["summary"] == "Summary one."
    assert record["archive_refs"] == [world.archives and f"runA_{hashlib.sha1(world.archives[0][1].encode()).hexdigest()[:8]}"]
    assert 0 < record["size_after"]
    view = seen[0]
    assert len(view) == 2 and view[1] is messages[4]
    assert view[0].content == record["message"]
    assert f"read_canvas(artifact_id='{record['archive_refs'][0]}')" in view[0].content
    assert "TASK-A" in view[0].content and "read_file" not in view[0].content
    [payload] = world.compacted()
    assert payload == {
        "archive_artifact_id": record["archive_refs"][0],
        "archive_section_ids": record["archive_refs"],
        "generation": 1,
        "messages_summarized": 4,
        "summary_chars": len("Summary one."),
        "trigger_tokens": 1,
    }
    [narration] = world.narrations()
    assert narration["narration_id"] == narration_id(1, record["archive_refs"])


@BOTH_HOOKS
def test_a_second_compaction_folds_the_prior_summary_and_archives_only_new_messages(asynchronous):
    first = _World("runB", [_text_turn("Earlier: the reader wants tables (from A1RESULT).")])
    messages = _messages("B1")
    response = _run(InqtrixCompactionMiddleware(keep_messages=1), first, _request(first, messages), asynchronous, [])
    record = response.command.update[COMPACTION_STATE]

    grown = [*messages, AIMessage(content="B2 second note " + "words " * 400), HumanMessage(content="Go on.")]
    second = _World("runB", [_text_turn("Folded summary.")])
    response = _run(
        InqtrixCompactionMiddleware(keep_messages=1), second, _request(second, grown, record), asynchronous, []
    )

    summary_input = second.provider.calls[0]["messages"][0]["content"]
    assert "the reader wants tables" in summary_input
    assert "B1RESULT" not in summary_input
    assert len(second.archives) == 1
    assert "B2 second note" in second.archives[0][1] and "B1RESULT" not in second.archives[0][1]
    assert "the reader wants tables" not in second.archives[0][1]
    new = response.command.update[COMPACTION_STATE]
    assert new["generation"] == 2 and new["cutoff"] == 6
    assert new["archive_refs"][0] == record["archive_refs"][0] and len(new["archive_refs"]) == 2
    assert second.compacted()[0]["messages_summarized"] == 2


@BOTH_HOOKS
def test_an_archive_failure_stops_before_any_paid_call(asynchronous):
    world = _World("runC", [_text_turn("never")])

    def broken(title, text):
        raise RuntimeError("archive down")

    world.deps.append_context_archive = broken
    with pytest.raises(AgentContextArchiveError):
        _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("C1")), asynchronous, [])

    assert world.provider.calls == []
    assert world.events == []


@BOTH_HOOKS
@pytest.mark.parametrize(
    ("finish_reason", "error"),
    [("length", AgentModelCapacityError), ("content_filter", AgentStructuredOutputError)],
)
def test_a_cut_summary_leaves_no_event_and_no_record(asynchronous, finish_reason, error):
    turn = ChatTurn(
        text="Half a summ", tool_calls=(), finish_reason=finish_reason, model="scripted",
        prompt_tokens=1, completion_tokens=1, raw=None,
    )
    world = _World("runD", [turn])
    seen: list = []
    with pytest.raises(error):
        _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("D1")), asynchronous, seen)

    assert seen == [] and world.events == []


@BOTH_HOOKS
def test_a_failed_summary_call_leaves_no_event_and_no_record(asynchronous):
    class Failing(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages})
            raise RuntimeError("provider down")

    world = _World("runE", [])
    world.provider = Failing([])
    world.model = build_tool_chat_model(world.provider)
    seen: list = []
    with pytest.raises(Exception, match="provider down"):
        _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("E1")), asynchronous, seen)

    assert len(world.provider.calls) == 1 and seen == [] and world.events == []


@BOTH_HOOKS
def test_a_summary_request_over_the_budget_is_refused_before_the_archive(asynchronous):
    world = _World("runF", [_text_turn("never")], budget=50)
    with pytest.raises(AgentModelCapacityError):
        _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("F1")), asynchronous, [])

    assert world.archives == [] and world.provider.calls == [] and world.events == []


@BOTH_HOOKS
def test_a_failing_model_call_leaves_no_event_and_the_retry_meets_the_same_ids(asynchronous):
    messages = _messages("G1")
    first = _World("runG", [_text_turn("Same summary.")])

    def failing(projected):
        raise RuntimeError("model call failed")

    async def failing_async(projected):
        failing(projected)

    set_kernel_deps(first.deps)
    try:
        with pytest.raises(RuntimeError, match="model call failed"):
            middleware = InqtrixCompactionMiddleware(keep_messages=1)
            request = _request(first, messages)
            if asynchronous:
                asyncio.run(middleware.awrap_model_call(request, failing_async))
            else:
                middleware.wrap_model_call(request, failing)
    finally:
        set_kernel_deps(None)
    assert first.events == []

    retry = _World("runG", [_text_turn("Another wording of the summary.")])
    response = _run(InqtrixCompactionMiddleware(keep_messages=1), retry, _request(retry, messages), asynchronous, [])

    assert retry.archives == first.archives
    record = response.command.update[COMPACTION_STATE]
    assert record["generation"] == 1
    [narration] = retry.narrations()
    assert narration["narration_id"] == narration_id(1, [f"runG_{hashlib.sha1(first.archives[0][1].encode()).hexdigest()[:8]}"])


def _loaded(skill_id: str) -> str:
    """The first line of a load_skill result that loaded the skill."""
    from inqtrix.agents.kernel.tools import SKILL_LOADED_MARKER

    return SKILL_LOADED_MARKER.format(skill_id=skill_id, revision="r1")


@BOTH_HOOKS
def test_the_assignment_answers_and_skills_stay_outside_the_fence(asynchronous):
    world = _World("runH", [_text_turn("First."), _text_turn("Second.")])
    world.deps.clarified_answers = ["Answer: only 2025."]
    messages = [
        HumanMessage(content="H1 assignment: compare the offers"),
        AIMessage(content="", tool_calls=[{"id": "s1", "name": "load_skill", "args": {"name": "tables"}}]),
        ToolMessage(content=f"{_loaded('tables')}\nSKILL: always answer as a table", tool_call_id="s1", name="load_skill"),
        AIMessage(content="noted " + "words " * 400),
        HumanMessage(content="Continue."),
    ]
    response = _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), asynchronous, [])
    record = response.command.update[COMPACTION_STATE]
    grown = [*messages, AIMessage(content="more " + "words " * 400), HumanMessage(content="Next.")]
    seen: list = []
    response = _run(
        InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, grown, record), asynchronous, seen
    )

    content = seen[0][0].content
    fence = content.index('<untrusted_content source="conversation-summary">')
    for verbatim in ("H1 assignment: compare the offers", "Answer: only 2025.", "SKILL: always answer as a table"):
        assert content.index(verbatim) < fence


def test_a_refused_skill_load_is_not_restated_as_an_instruction():
    """A refused load_skill call loaded nothing; restated, it would contradict the run's state."""
    world = _World("runR", [_text_turn("First."), _text_turn("Second.")])
    refusal = "The skill 'audit' may only be activated by the user."
    messages = [
        HumanMessage(content="H1 assignment: compare the offers"),
        AIMessage(content="", tool_calls=[{"id": "s1", "name": "load_skill", "args": {"name": "audit"}}]),
        ToolMessage(content=refusal, tool_call_id="s1", name="load_skill"),
        AIMessage(content="noted " + "words " * 400),
        HumanMessage(content="Continue."),
    ]
    response = _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), False, [])
    record = response.command.update[COMPACTION_STATE]
    grown = [*messages, AIMessage(content="more " + "words " * 400), HumanMessage(content="Next.")]
    seen: list = []
    _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, grown, record), False, seen)

    content = seen[0][0].content
    assert "Instructions of the skills you loaded" not in content
    assert refusal not in content[: content.index('<untrusted_content source="conversation-summary">')]


def test_tool_calls_and_their_results_stay_together():
    messages = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "a", "name": "t", "args": {}}, {"id": "b", "name": "t", "args": {}}]),
        ToolMessage(content="ra", tool_call_id="a", name="t"),
        ToolMessage(content="rb", tool_call_id="b", name="t"),
        AIMessage(content="done"),
    ]

    assert safe_cutoff(messages, 2) == 1
    assert safe_cutoff(messages, 3) == 1
    assert safe_cutoff(messages, 1) == 4
    assert safe_cutoff(messages, 5) == 0


def test_both_hooks_build_the_same_record_and_view():
    results = []
    for asynchronous in (False, True):
        world = _World("runI", [_text_turn("Same.")])
        seen: list = []
        response = _run(
            InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("I1")), asynchronous, seen
        )
        results.append((response.command.update[COMPACTION_STATE], [message.content for message in seen[0]]))

    assert results[0] == results[1]


@BOTH_HOOKS
def test_a_compaction_that_does_not_shrink_the_view_sends_the_old_one_and_holds(asynchronous, caplog):
    world = _World("runJ", [_text_turn("A summary far longer than what it condenses. " * 200)], trigger=50)
    messages = [
        HumanMessage(content="J1 assignment"),
        # Enough to condense that a summary could shrink the view, so it is paid for.
        AIMessage(content="short " * 300),
        HumanMessage(content="Continue " + "x " * 10),
    ]
    seen: list = []
    with caplog.at_level(logging.WARNING, logger="inqtrix.agents.kernel.compaction"):
        response = _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), asynchronous, seen)

    assert len(world.provider.calls) == 1
    assert not hasattr(response, "command")
    assert seen[0] == messages
    assert world.events == []
    assert world.deps.compaction_hold_tokens > 0
    assert "did not shrink the view" in caplog.text
    calls = len(world.provider.calls)
    _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), asynchronous, [])
    assert len(world.provider.calls) == calls


@BOTH_HOOKS
def test_only_the_kept_part_above_the_trigger_compacts_nothing(asynchronous, caplog):
    world = _World("runK", [])
    messages = [HumanMessage(content="K1 " + "long " * 500)]
    with caplog.at_level(logging.WARNING, logger="inqtrix.agents.kernel.compaction"):
        for _ in range(2):
            _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), asynchronous, [])

    assert world.provider.calls == [] and world.events == [] and world.archives == []
    assert caplog.text.count("Kernel compaction skipped") == 1


@BOTH_HOOKS
def test_a_compaction_that_cannot_shrink_even_without_a_summary_asks_for_none(asynchronous, caplog):
    """Evicting only the assignment cannot shrink the view: it travels next to the summary anyway."""
    world = _World("runL", [_text_turn("never")])
    messages = [
        HumanMessage(content="L1 assignment: write the report"),
        AIMessage(content="L1 note " + "words " * 50),
        HumanMessage(content="Continue."),
    ]
    seen: list = []
    with caplog.at_level(logging.WARNING, logger="inqtrix.agents.kernel.compaction"):
        response = _run(InqtrixCompactionMiddleware(keep_messages=2), world, _request(world, messages), asynchronous, seen)

    assert not hasattr(response, "command")
    assert seen[0] == messages
    assert world.provider.calls == [] and world.archives == [] and world.events == []
    assert world.deps.compaction_hold_tokens > 0
    assert "would not shrink the view even without a summary" in caplog.text


@BOTH_HOOKS
def test_a_landed_compaction_ends_the_hold_of_an_earlier_attempt(asynchronous):
    world = _World("runM", [_text_turn("M " + "far too long " * 400), _text_turn("M summary.")])
    messages = [
        HumanMessage(content="M1 assignment"),
        AIMessage(content="short " * 300),
        HumanMessage(content="Continue " + "x " * 10),
    ]
    _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages), asynchronous, [])
    assert len(world.provider.calls) == 1 and world.deps.compaction_hold_tokens > 0

    grown = [*messages, AIMessage(content="M2 note " + "words " * 600), HumanMessage(content="Go on.")]
    response = _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, grown), asynchronous, [])

    assert response.command.update[COMPACTION_STATE]["generation"] == 1
    assert world.deps.compaction_hold_tokens == 0


def test_retries_of_the_summary_call_read_as_compaction():
    notice ={"attempt": 1, "max_attempts": 3, "delay_seconds": 0.5, "error_code": "rate_limited", "operation": "chat"}
    compaction = model_retry_activity(notice, node=COMPACTION_PURPOSE, language="en")
    other = model_retry_activity(notice, node="agent_model", language="en")

    assert compaction["purpose"] != other["purpose"]


@BOTH_HOOKS
def test_the_summary_call_runs_under_the_compaction_purpose(asynchronous):
    purposes = []

    class Recording(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            bound = current_purpose()
            purposes.append(bound.purpose if bound is not None else None)
            return super().chat(messages, **kwargs)

    world = _World("runL", [])
    world.provider = Recording([_text_turn("Summary.")])
    world.model = build_tool_chat_model(world.provider)
    _run(InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, _messages("L1")), asynchronous, [])

    assert purposes == [COMPACTION_PURPOSE]


def test_one_shared_middleware_keeps_parallel_runs_apart():
    """The compiled graph, and with it the middleware, serves concurrent runs."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from inqtrix.agents.kernel.deps import kernel_deps

    rendezvous = Barrier(2, timeout=5)

    class PerRun(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            run_id = kernel_deps().run_id
            rendezvous.wait()
            return _text_turn(f"Summary for {run_id}.")

    middleware = InqtrixCompactionMiddleware(keep_messages=1)

    def compact(run_id):
        world = _World(run_id, [])
        world.provider = PerRun([])
        world.model = build_tool_chat_model(world.provider)
        response = _run(middleware, world, _request(world, _messages(run_id), todos=[{"content": f"{run_id}TASK", "status": "pending"}]), False, [])
        return response.command.update[COMPACTION_STATE], world

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compact, ["runM1", "runM2"]))
    for own, other, (record, world) in zip(["runM1", "runM2"], ["runM2", "runM1"], results, strict=True):
        assert f"Summary for {own}." in record["message"]
        assert f"{own}TASK" in record["message"] and other not in record["message"]
        assert own in world.archives[0][1] and other not in world.archives[0][1]


def test_the_trigger_never_lies_above_the_input_budget_of_any_model_card():
    """Above the budget the input check of the model call acts first, and compaction never runs."""
    from inqtrix.agents.kernel.algorithm import (
        capped_context_trigger,
        resolve_context_trigger_tokens,
    )
    from inqtrix.model_cards import MODEL_CARDS, card_input_budget

    platform = AgentPlatformSettings()
    for card in MODEL_CARDS:
        budget = card_input_budget(card)
        trigger = capped_context_trigger(resolve_context_trigger_tokens(platform, card.id), budget)
        assert 0 < trigger <= budget, card.id

    # Without a card the budget is not known; a cap of 0 would switch compaction off.
    assert capped_context_trigger(128_000, 0) == 128_000


@pytest.mark.parametrize(
    ("bound", "expected"),
    [(COMPACTION_PURPOSE, COMPACTION_PURPOSE), ("agent_answer", "agent_kernel"), (None, "agent_kernel")],
)
def test_the_kernel_provider_keeps_the_purpose_of_a_compaction_only(bound, expected):
    """A summary inside a kernel turn is billed and traced as compaction, not as a kernel turn."""
    from contextlib import nullcontext

    from inqtrix.agents.kernel.algorithm import _DepsChatProvider
    from inqtrix.observability.context import bind_purpose

    purposes = []

    class Recording(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            seen = current_purpose()
            purposes.append(seen.purpose if seen is not None else None)
            return super().chat(messages, **kwargs)

    world = _World("runN", [])
    world.deps.llm = Recording([_text_turn("ok")])
    set_kernel_deps(world.deps)
    try:
        with bind_purpose(bound) if bound else nullcontext():
            _DepsChatProvider().chat([{"role": "user", "content": "x"}])
    finally:
        set_kernel_deps(None)

    assert purposes == [expected]


PREFIX = archive_prefix("runS")
SCOPE = RereadScope(PREFIX, frozenset({"read_canvas", "read_context"}))


@tool
def read_canvas(artifact_id: str) -> str:
    """Read an archived result."""
    return ""


def _round(index: int, name: str, content: str, *, args: dict | None = None, artifact=None) -> list:
    call_id = f"c{index}"
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args or {"q": str(index)}}]),
        ToolMessage(content=content, tool_call_id=call_id, name=name, artifact=artifact),
    ]


def _archived(index: int, body: int = 40) -> str:
    """A result the run archive keeps in full, shaped like the offload writes it."""
    return (
        f"R{index} hits\n"
        + untrusted_fence(f"R{index}BODY " + "x" * body + f" quotes [W9] and ref_{0xFFF:016x}", "knowledge-base")
        + f"\nEvidence [K{index}], reference_id: ref_{index:016x}\n\n"
        + archived_result_pointer(f"{PREFIX}_part{index}")
    )


def _thread(*rounds: list) -> list:
    messages = [HumanMessage(content="assignment: compare the offers")]
    for part in rounds:
        messages.extend(part)
    return [*messages, AIMessage(content="noted"), HumanMessage(content="Continue.")]


def _per_message(view: list) -> int:
    return 100 * len(view)


def _view(messages: list, trigger: int = 500) -> list:
    return visible_view(messages, None, trigger=trigger, count=_per_message, scope=SCOPE)


def _cleared(view: list) -> list[str]:
    return [
        message.tool_call_id
        for message in view
        if isinstance(message, ToolMessage) and message.additional_kwargs.get(CLEARED_RESULT_FLAG)
    ]


def _result(view: list, call_id: str) -> ToolMessage:
    return next(m for m in view if isinstance(m, ToolMessage) and m.tool_call_id == call_id)


def test_stage_one_clears_answered_results_in_the_view_and_leaves_the_state_alone():
    messages = _thread(*(_round(i, "search_project_knowledge", _archived(i)) for i in range(1, 7)))
    state = deepcopy(messages)

    view = _view(messages)

    assert messages == state
    assert _cleared(view) == ["c1", "c2", "c3"]
    assert _result(view, "c1").content == (
        "[Result of search_project_knowledge cleared from this view to save input. "
        f"Full text: read_canvas(artifact_id='{PREFIX}_part1'). "
        "Evidence labels in it: [K1]. Reference ids in it: ref_0000000000000001.]"
    )
    for call_id in ("c4", "c5", "c6"):
        assert _result(view, call_id).content == _result(messages, call_id).content
    assert [m.content for m in _view(messages)] == [m.content for m in view]
    assert _cleared(_view(messages, trigger=0)) == []


def test_a_source_window_names_the_call_that_reads_it_again():
    def window(version: str) -> dict:
        record = SourceCoverageRecord(
            kind="conversation_source", source_id="s1", version=version, total=900, read_ranges=[(0, 900)]
        )
        return {SOURCE_COVERAGE: [record.model_dump()]}

    args = {"source_id": "s1", "offset": 0, "max_chars": 20000}
    messages = _thread(
        _round(1, "read_context", "window one", args=args, artifact=window("v:abc")),
        _round(2, "read_context", "window two", args=args, artifact=window("")),
        _round(3, "web_search", "a result nobody keeps"),
        *(_round(i, "search_project_knowledge", _archived(i)) for i in range(4, 7)),
    )

    view = _view(messages)

    assert _cleared(view) == ["c1"]
    assert _result(view, "c1").content == (
        "[Result of read_context cleared from this view to save input. Read it again with "
        'read_context(max_chars=20000, offset=0, source_id="s1"); it read source version v:abc.]'
    )


def test_questions_skills_the_newest_and_unanswered_results_stay_verbatim():
    messages = [
        HumanMessage(content="assignment"),
        *_round(1, "ask_user", "Answer: only 2025.\n\n" + archived_result_pointer(f"{PREFIX}_q")),
        *_round(2, "load_skill", "SKILL: tables\n\n" + archived_result_pointer(f"{PREFIX}_s")),
        *(part for i in range(3, 7) for part in _round(i, "search_project_knowledge", _archived(i))),
        AIMessage(
            content="",
            tool_calls=[{"id": f"b{i}", "name": "search_project_knowledge", "args": {"q": f"b{i}"}} for i in range(1, 6)],
        ),
        *(ToolMessage(content=_archived(10 + i), tool_call_id=f"b{i}", name="search_project_knowledge") for i in range(1, 6)),
    ]

    view = _view(messages)

    # The batch is the newest and not answered yet; the four searches before it are.
    assert _cleared(view) == ["c3", "c4", "c5", "c6"]


def test_only_this_runs_archive_pointer_outside_every_fence_counts():
    foreign = "R1 hits\n\n" + archived_result_pointer(f"{archive_prefix('runOther')}_part1")
    fenced = "R2 hits\n" + untrusted_fence("text\n\n" + archived_result_pointer(f"{PREFIX}_part2"), "web")
    messages = _thread(
        _round(1, "search_project_knowledge", foreign),
        _round(2, "web_search", fenced),
        *(_round(i, "search_project_knowledge", _archived(i)) for i in range(3, 7)),
    )

    assert _cleared(_view(messages)) == ["c3"]
    assert archived_result_id(fenced, PREFIX) is None
    assert archived_result_id(foreign, PREFIX) is None


def test_a_result_whose_reader_the_call_lacks_stays_verbatim():
    """A placeholder never names a tool the model cannot call in this turn."""
    window = SourceCoverageRecord(kind="conversation_source", source_id="s1", version="v:abc", total=9, read_ranges=[(0, 9)])
    messages = _thread(
        _round(1, "read_context", "window", artifact={SOURCE_COVERAGE: [window.model_dump()]}),
        *(_round(i, "search_project_knowledge", _archived(i)) for i in range(2, 6)),
    )

    def cleared_with(*tools: str) -> list[str]:
        scope = RereadScope(PREFIX, frozenset(tools))
        return _cleared(visible_view(messages, None, trigger=500, count=_per_message, scope=scope))

    assert cleared_with("read_canvas", "read_context") == ["c1", "c2"]
    assert cleared_with("read_context") == ["c1"]
    assert cleared_with("read_canvas") == ["c2"]


def test_the_offload_writes_the_pointer_stage_one_reads():
    from inqtrix.agents.kernel.tools import _offload_bulky_result

    world = _World("runS", [])
    world.deps.context_offload_chars = 50
    world.deps.append_context_archive_chunked = lambda title, text: [f"{PREFIX}_offloaded"]
    set_kernel_deps(world.deps)
    try:
        text = _offload_bulky_result("long " * 40, tool="search_project_knowledge", reference_lines="- Evidence [K1]")
    finally:
        set_kernel_deps(None)

    assert archived_result_id(text, PREFIX) == f"{PREFIX}_offloaded"


def test_above_the_threshold_a_turn_changes_the_view_only_where_a_result_leaves_the_newest():
    """The cached prompt holds up to the result that leaves the newest three."""
    messages = [HumanMessage(content="assignment")]
    for i in range(1, 41):
        messages += [*_round(i, "search_project_knowledge", _archived(i)), AIMessage(content=f"noted {i}")]
    previous: list = []
    for n in range(1, len(messages) + 1):
        view = visible_view(messages[:n], None, trigger=2000, count=_per_message, scope=SCOPE)
        if _per_message(previous) >= 2000:
            stable = [index for index, m in enumerate(previous) if isinstance(m, ToolMessage)][-3]
            assert [m.content for m in view[:stable]] == [m.content for m in previous[:stable]], n
        previous = view

    assert len(_cleared(previous)) == 37


@BOTH_HOOKS
def test_stage_one_alone_keeps_the_view_under_the_trigger_without_a_summary(asynchronous):
    world = _World("runS", [_text_turn("never")], trigger=3000)
    messages = _thread(
        *(_round(i, "search_project_knowledge", _archived(i, body=8000)) for i in range(1, 4)),
        *(_round(i, "web_search", f"R{i} short") for i in range(4, 7)),
    )
    seen: list = []

    response = _run(
        InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages, tools=[read_canvas]), asynchronous, seen
    )

    assert world.provider.calls == [] and world.events == [] and world.archives == []
    assert not hasattr(response, "command")
    assert _cleared(seen[0]) == ["c1", "c2", "c3"]


@BOTH_HOOKS
def test_stage_two_condenses_the_view_it_saw_and_archives_the_originals(asynchronous):
    world = _World("runS", [_text_turn("Summary.")], trigger=3000)
    messages = _thread(
        *(_round(i, "search_project_knowledge", _archived(i, body=8000)) for i in range(1, 4)),
        *(_round(i, "web_search", f"R{i}PLAIN " + "y" * 8000) for i in range(4, 7)),
    )
    seen: list = []

    response = _run(
        InqtrixCompactionMiddleware(keep_messages=1), world, _request(world, messages, tools=[read_canvas]), asynchronous, seen
    )

    summary_input = world.provider.calls[0]["messages"][0]["content"]
    assert "R1BODY" not in summary_input and "R4PLAIN" in summary_input
    assert f"Full text: read_canvas(artifact_id='{PREFIX}_part1')" in summary_input
    [(title, archived)] = world.archives
    assert "R1BODY" in archived and "R4PLAIN" in archived and "cleared from this view" not in archived
    record = response.command.update[COMPACTION_STATE]
    assert record["cutoff"] == len(messages) - 1
    assert [m.content for m in seen[0]] == [record["message"], "Continue."]
