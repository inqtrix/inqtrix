"""The transcript line that says what a tool was asked to do.

A preview is what a user READS while a tool runs. Built by serialising
the arguments and clipping the result, it would show a JSON dump cut
mid-token — rubble for the reader and unparseable for the surface, which
then renders it verbatim.
"""

import pytest

from inqtrix.agents.context_sources import SourceCoverageLedger
from inqtrix.agents.kernel.algorithm import (
    _PROSE_PREVIEW_LIMIT,
    _args_preview,
    _canvas_args_preview,
    _checkpointed_tool_event_ids,
    _evidence_args_preview,
    _observe_update,
)


def test_delegation_assignment_previews_as_prose() -> None:
    """The regression: a delegation is described, not dumped."""
    preview = _args_preview(
        {
            "assignment": "Erstelle ein belegtes Memo zum AI Act.",
            "depth": "deep",
        }
    )
    assert preview == "Erstelle ein belegtes Memo zum AI Act."
    assert not preview.startswith("{")


def test_a_long_assignment_is_cut_as_a_sentence_not_as_json() -> None:
    """Over the cap the line stays prose AND stays visibly cut."""
    preview = _args_preview({"assignment": "A" * 900, "depth": "deep"})
    assert preview.startswith("A")
    assert preview.endswith("…")
    assert len(preview) == _PROSE_PREVIEW_LIMIT + 1


def test_a_bare_query_still_previews_as_prose() -> None:
    """The one case that always worked must keep working."""
    assert _args_preview({"query": "EU AI Act Artikel 50"}) == (
        "EU AI Act Artikel 50"
    )


def test_query_wins_over_a_second_argument() -> None:
    """A scoped knowledge search is still described by its question."""
    preview = _args_preview(
        {"query": "Pflichten der Betreiber", "collection_ids": ["c1", "c2"]}
    )
    assert preview == "Pflichten der Betreiber"


def test_an_id_only_call_has_no_preview() -> None:
    """No intent argument: an identifier is no words for the reader, so the
    row shows the tool alone instead of a JSON dump with the id."""
    assert _args_preview({"artifact_id": "art_123"}) == ""
    assert _args_preview({"report_id": "run_8198"}) == ""


def test_a_document_body_never_reaches_the_prose_lane() -> None:
    """``write_canvas`` carries a whole document; the row shows the
    document's TITLE and nothing of its body, so the event log never
    becomes a second copy of an artifact."""
    preview = _args_preview(
        {"title": "Memo", "content_markdown": "x" * 40000}
    )
    assert preview == "Memo"
    assert "x" * 20 not in preview


def test_a_research_question_previews_as_prose() -> None:
    """``run_web_research`` asks a question; the row read it as JSON."""
    preview = _args_preview(
        {"question": "Vergleiche die Foerderbedingungen 2026.", "depth": "deep"}
    )
    assert preview == "Vergleiche die Foerderbedingungen 2026."


def test_a_long_title_is_still_visibly_clipped() -> None:
    preview = _args_preview({"title": "T" * 900, "content_markdown": "body"})
    assert preview.endswith("…")
    assert len(preview) == _PROSE_PREVIEW_LIMIT + 1


def test_evidence_preview_names_only_the_registered_selection() -> None:
    from threading import RLock
    from types import SimpleNamespace

    deps = SimpleNamespace(
        _evidence_lock=RLock(),
        evidence_refs={
            "ref_a": {"title": "Bedingungen des Förderprogramms", "url": "https://example.org/program"},
            "ref_b": {"url": "https://example.org/method"},
        },
        web_search_ledger={"searches": {
            "query_a": {"query": "Welche Voraussetzungen gelten?"},
        }},
    )
    assert _evidence_args_preview({"reference_id": "ref_a"}, deps) == "Bedingungen des Förderprogramms"
    assert _evidence_args_preview({"reference_id": "ref_b"}, deps) == "https://example.org/method"
    assert _evidence_args_preview({"query_id": "query_a"}, deps) == "Welche Voraussetzungen gelten?"
    # An id the run never registered names nothing, so the row shows the tool alone.
    assert _evidence_args_preview({"reference_id": "unknown"}, deps) == ""
    assert _evidence_args_preview({"reference_id": "ref_a", "query_id": "query_a"}, deps) == ""


def test_registered_evidence_preview_keeps_the_visible_length_boundary() -> None:
    from threading import RLock
    from types import SimpleNamespace

    deps = SimpleNamespace(
        _evidence_lock=RLock(),
        evidence_refs={"ref_a": {"title": "T" * 900}},
        web_search_ledger={},
    )
    assert _evidence_args_preview({"reference_id": "ref_a"}, deps) == "T" * _PROSE_PREVIEW_LIMIT + "…"


def test_an_attached_report_previews_its_title_and_never_its_id() -> None:
    from threading import RLock
    from types import SimpleNamespace

    events = []
    deps = SimpleNamespace(
        _evidence_lock=RLock(),
        attached_report_titles={"run_8198": "ASR A3.5 Raumtemperatur"},
        evidence_refs={},
        web_search_ledger={},
        emitted_tool_start_ids={},
        tool_call_ordinals={},
        tool_calls_used=0,
        todo_in_progress_index=None,
        work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    message = SimpleNamespace(type="ai", id="message_r", tool_calls=[
        {"id": "call_r", "name": "read_research_report", "args": {"report_id": "run_8198"}},
        {"id": "call_u", "name": "read_research_report", "args": {"report_id": "run_gone"}},
    ])
    _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [data for kind, data in events if kind == "inqtrix.agent.tool.started"]
    assert [data["args_preview"] for data in started] == ["ASR A3.5 Raumtemperatur", ""]


def test_tool_start_event_uses_the_registered_evidence_title() -> None:
    from threading import RLock
    from types import SimpleNamespace

    events = []
    deps = SimpleNamespace(
        _evidence_lock=RLock(),
        evidence_refs={"ref_a": {"title": "Original study"}},
        web_search_ledger={},
        emitted_tool_start_ids={},
        tool_call_ordinals={},
        tool_calls_used=0,
        todo_in_progress_index=None,
        work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    message = SimpleNamespace(type="ai", id="message_a", tool_calls=[
        {"id": "call_a", "name": "read_evidence", "args": {"reference_id": "ref_a"}},
    ])
    _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [data for kind, data in events if kind == "inqtrix.agent.tool.started"]
    assert len(started) == 1
    assert started[0]["args_preview"] == "Original study"


@pytest.mark.parametrize("tool_name", ["write_todos", "read_canvas"])
def test_nameless_tool_rejection_keeps_the_requested_tool(tool_name: str) -> None:
    from types import SimpleNamespace

    events = []
    deps = SimpleNamespace(
        emitted_tool_start_ids={},
        emitted_tool_finish_ids=set(),
        tool_call_ordinals={},
        source_coverage=SourceCoverageLedger(),
        tool_calls_used=0,
        todo_in_progress_index=None,
        work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    call = SimpleNamespace(type="ai", id="message", tool_calls=[
        {"id": "call_a", "name": tool_name, "args": {}},
    ])
    reply = SimpleNamespace(
        type="tool", tool_call_id="call_a", name=None,
        status="error", content="Rejected by middleware",
    )
    _observe_update(deps, {"model": {"messages": [call]}}, lambda: {})
    _observe_update(deps, {"tools": {"messages": [reply]}}, lambda: {})
    # Replaying the same update must not invent another call or completion.
    _observe_update(deps, {"tools": {"messages": [reply]}}, lambda: {})
    finished = [data for kind, data in events if kind == "inqtrix.agent.tool.finished"]
    assert len(finished) == 1
    assert finished[0]["tool"] == tool_name
    assert finished[0]["status"] == "error"
    assert deps.tool_calls_used == 1


@pytest.mark.parametrize("dictionary_messages", [False, True])
def test_resumed_tool_rejection_keeps_checkpointed_tool_identity(
    dictionary_messages: bool,
) -> None:
    from types import SimpleNamespace

    call = {"id": "pending_call", "name": "write_todos", "args": {}}
    message = (
        {"role": "assistant", "id": "earlier", "tool_calls": [call]}
        if dictionary_messages
        else SimpleNamespace(type="ai", id="earlier", tool_calls=[call])
    )
    starts, finishes = _checkpointed_tool_event_ids(
        SimpleNamespace(values={"messages": [message]})
    )
    events = []
    deps = SimpleNamespace(
        emitted_tool_start_ids=starts,
        emitted_tool_finish_ids=finishes,
        tool_call_ordinals={},
        source_coverage=SourceCoverageLedger(),
        work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    reply = SimpleNamespace(
        type="tool", tool_call_id="pending_call", name="",
        status="error", content="Rejected by middleware",
    )
    _observe_update(deps, {"tools": {"messages": [reply]}}, lambda: {})
    assert len(events) == 1
    assert events[0][1]["tool"] == "write_todos"
    assert events[0][1]["invocation_id"] == "pending_call"


def test_tool_reply_name_is_preserved_without_a_recorded_start() -> None:
    from types import SimpleNamespace

    events = []
    deps = SimpleNamespace(
        emitted_tool_start_ids={}, emitted_tool_finish_ids=set(), tool_call_ordinals={},
        source_coverage=SourceCoverageLedger(), work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    reply = SimpleNamespace(
        type="tool", tool_call_id="call_a", name="read_canvas",
        status="success", content="Stored document",
    )
    _observe_update(deps, {"tools": {"messages": [reply]}}, lambda: {})
    assert events[0][1]["tool"] == "read_canvas"


@pytest.mark.parametrize("question, labels", [
    ("Lies bitte den archivierten Text.", (
        "Archivierter Text", "Archivierter Text ab Zeichen 77.915",
        "Archivübersicht", "Archivübersicht fortsetzen",
    )),
    ("Read the archived text.", (
        "Archived text", "Archived text from character 77,915",
        "Archive catalogue", "Continue archive catalogue",
    )),
])
def test_archive_tool_events_describe_continuations_without_exposing_arguments(question, labels):
    from copy import deepcopy
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps

    events = []
    deps = KernelDeps(
        run_id="run_archive_preview", question=question, control=None,
        platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    prefix = deps.context_archive_prefix
    calls = [
        {"artifact_id": prefix + "_source"},
        {"artifact_id": prefix + "_source", "offset": 77914, "content_hash": "a" * 64, "max_chars": 100},
        {"artifact_id": prefix, "limit": 2},
        {"artifact_id": prefix, "limit": 2, "cursor": "opaque_cursor"},
    ]
    original = deepcopy(calls)
    for index, arguments in enumerate(calls):
        message = SimpleNamespace(type="ai", id=f"message_{index}", tool_calls=[
            {"id": f"call_{index}", "name": "read_canvas", "args": arguments},
        ])
        _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [payload for kind, payload in events if kind == "inqtrix.agent.tool.started"]
    assert [payload["args_preview"] for payload in started] == list(labels)
    assert calls == original
    # A canvas document has no words here; its id travels as data.
    assert _canvas_args_preview({"artifact_id": "art_memo"}, deps) == ""


def test_a_canvas_document_read_carries_its_id_as_data_never_as_words() -> None:
    """The client knows a canvas document's title and names the row by it;
    the server sends the id beside an empty preview. A read of the run's own
    archive is worded by the server and carries no id."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps

    events = []
    deps = KernelDeps(
        run_id="run_canvas_preview", question="Lies bitte den archivierten Text.", control=None,
        platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    calls = [{"artifact_id": "art_memo"}, {"artifact_id": deps.context_archive_prefix}]
    for index, arguments in enumerate(calls):
        message = SimpleNamespace(type="ai", id=f"message_{index}", tool_calls=[
            {"id": f"call_{index}", "name": "read_canvas", "args": arguments},
        ])
        _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [payload for kind, payload in events if kind == "inqtrix.agent.tool.started"]
    assert (started[0]["args_preview"], started[0]["artifact_id"]) == ("", "art_memo")
    assert started[1]["args_preview"] == "Archivübersicht"
    assert "artifact_id" not in started[1]


@pytest.mark.parametrize("question, labels", [
    ("Lies bitte den Leitfaden vollständig.", (
        "Dokumenttext von Anfang", "Dokumenttext ab Zeichen 20.001",
        "Dokumenttext von Anfang", "Dokumenttext ab Zeichen 40.001", "Weitere Kommentare",
    )),
    ("Read the guide completely.", (
        "Document text from the start", "Document text from character 20,001",
        "Document text from the start", "Document text from character 40,001", "Further comments",
    )),
])
def test_document_window_tool_events_describe_positions_without_exposing_arguments(question, labels):
    """A knowledge or editor window previews as a position, never as an id dump."""
    from copy import deepcopy
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps

    events = []
    deps = KernelDeps(
        run_id="run_document_preview", question=question, control=None,
        platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    calls = [
        ("read_project_document", {"document_id": "kd_guide"}),
        ("read_project_document", {"document_id": "kd_guide", "offset": 20000,
                                   "content_hash": "a" * 64, "version": "rev_1:" + "a" * 64, "max_chars": 20000}),
        ("read_editor_document", {"document_id": "ed_guide", "include_comments": True}),
        ("read_editor_document", {"document_id": "ed_guide", "offset": 40000, "content_hash": "b" * 64,
                                  "version": "1:1:1:0:" + "b" * 64, "include_comments": False}),
        ("read_editor_document", {"document_id": "ed_guide", "comments_cursor": "opaque",
                                  "content_hash": "b" * 64, "version": "1:1:1:0:" + "b" * 64}),
    ]
    original = deepcopy(calls)
    for index, (tool, arguments) in enumerate(calls):
        message = SimpleNamespace(type="ai", id=f"message_{index}", tool_calls=[
            {"id": f"call_{index}", "name": tool, "args": arguments},
        ])
        _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [payload for kind, payload in events if kind == "inqtrix.agent.tool.started"]
    assert [payload["args_preview"] for payload in started] == list(labels)
    assert calls == original
    assert not any("kd_guide" in payload["args_preview"] or "ed_guide" in payload["args_preview"]
                   for payload in started)


@pytest.mark.parametrize("question, labels", [
    ("Lies bitte den Leitfaden vollständig.", (
        "Dokumenttext von Anfang, 2 Stellen", "Dokumenttext ab Zeichen 40.001, 1 Stelle",
    )),
    ("Read the guide completely.", (
        "Document text from the start, 2 passages", "Document text from character 40,001, 1 passage",
    )),
])
def test_a_citation_previews_its_window_and_count_without_identifiers_or_passages(question, labels):
    """A citation previews like the read it cites from, never as an id dump."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps

    events = []
    deps = KernelDeps(
        run_id="run_cite_preview", question=question, control=None,
        platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    calls = [
        {"document_id": "kd_guide", "version": "rev_1:" + "a" * 64, "window_start": 0,
         "window_end": 652, "passages": ["Erste wörtliche Stelle hier.", "Zweite wörtliche Stelle hier."]},
        {"document_id": "kd_guide", "version": "rev_1:" + "a" * 64, "window_start": 40000,
         "window_end": 60000, "passages": ["Eine wörtliche Stelle dort."]},
    ]
    for index, arguments in enumerate(calls):
        message = SimpleNamespace(type="ai", id=f"message_{index}", tool_calls=[
            {"id": f"call_{index}", "name": "cite_document_passages", "args": arguments},
        ])
        _observe_update(deps, {"model": {"messages": [message]}}, lambda: {})
    started = [payload for kind, payload in events if kind == "inqtrix.agent.tool.started"]
    assert [payload["args_preview"] for payload in started] == list(labels)
    assert not any(
        fragment in payload["args_preview"]
        for payload in started
        for fragment in ("kd_guide", "rev_1", "wörtliche Stelle")
    )
