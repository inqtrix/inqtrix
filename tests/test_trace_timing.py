"""The parallelism numbers must tell side by side from one after another.

Later phases compare these numbers before and after running tools and searches
concurrently. A reading that looks the same either way would prove nothing, so each
test feeds both forms with the span timestamps a trace export carries.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent / "load" / "run-load" / "trace_timing.py"
_ORIGIN = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def timing():
    spec = importlib.util.spec_from_file_location("trace_timing_under_test", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _span(kind: str, name: str, start: float, end: float, parent: str = "run") -> dict:
    stamp = lambda seconds: (_ORIGIN + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    return {"type": kind, "name": name, "startTime": stamp(start), "endTime": stamp(end), "parentObservationId": parent}


def _document(*spans: dict) -> dict:
    return {"payload": {"observations": list(spans)}}


def test_two_tools_of_a_turn_that_run_at_once_show_as_overlap(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "chat", 0, 1),
        _span("TOOL", "fake_one", 1, 11),
        _span("TOOL", "fake_two", 1, 11),
    )])["tools"]
    assert summary["turns_that_overlapped"] == 1
    assert summary["parallelism"] == 2.0
    assert summary["peak_at_once"] == 2


def test_two_tools_of_a_turn_one_after_another_show_none(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "chat", 0, 1),
        _span("TOOL", "fake_one", 1, 11),
        _span("TOOL", "fake_two", 11, 21),
    )])["tools"]
    assert summary["turns_with_several_tools"] == 1
    assert summary["turns_that_overlapped"] == 0
    assert summary["parallelism"] == 1.0
    assert summary["peak_at_once"] == 1


def test_tools_of_two_turns_and_of_two_runs_are_not_one_turn(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "chat", 0, 1),
        _span("TOOL", "first_turn", 1, 2),
        _span("GENERATION", "chat", 2, 3),
        _span("TOOL", "second_turn", 3, 4),
        _span("TOOL", "other_run", 1, 4, parent="child"),
    )])["tools"]
    assert summary["turns"] == 3
    assert summary["turns_with_several_tools"] == 0
    assert summary["parallelism"] is None


def test_a_round_of_parallel_searches_takes_as_long_as_its_slowest(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "web_search", 0, 10, parent="round"),
        _span("GENERATION", "web_search", 0, 8, parent="round"),
    )])["research"]
    assert summary["rounds"] == 1
    assert summary["searches_per_round_median"] == 2
    assert summary["wall_seconds_p50"] == 10
    assert summary["wall_to_slowest_search_p50"] == 1


def test_a_round_of_searches_one_after_another_takes_their_sum(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "web_search", 0, 10, parent="round"),
        _span("GENERATION", "web_search", 10, 18, parent="round"),
    )])["research"]
    assert summary["wall_seconds_p50"] == 18
    assert summary["wall_to_slowest_search_p50"] == 1.8


def test_rounds_stay_apart(timing):
    summary = timing.summarize([_document(
        _span("GENERATION", "web_search", 0, 10, parent="round-one"),
        _span("GENERATION", "web_search", 100, 110, parent="round-two"),
    )])["research"]
    assert summary["rounds"] == 2
    assert summary["wall_seconds_p95"] == 10


def test_knowledge_retrieval_is_grouped_by_its_sub_questions(timing):
    summary = timing.summarize([_document(
        _span("SPAN", "knowledge.retrieve", 0, 2, parent="one"),
        _span("SPAN", "knowledge.retrieve", 0, 2, parent="two"),
        _span("SPAN", "knowledge.retrieve", 2, 4, parent="two"),
    )])["knowledge"]
    assert summary == {
        "1": {"runs": 1, "retrieval_wall_seconds_p50": 2.0, "retrieval_busy_seconds_p50": 2.0},
        "2": {"runs": 1, "retrieval_wall_seconds_p50": 4.0, "retrieval_busy_seconds_p50": 4.0},
    }


def test_the_tool_reads_a_directory_of_exports_and_prints_only_numbers(timing, tmp_path, monkeypatch, capsys):
    document = _document(_span("TOOL", "fake_one", 0, 1), _span("TOOL", "fake_two", 0, 1))
    document["payload"]["observations"][0]["input"] = "geheimer Text"
    (tmp_path / "one.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["trace_timing.py", str(tmp_path)])
    timing.main()
    output = capsys.readouterr().out
    assert json.loads(output)["traces"] == 1
    assert "geheimer" not in output
