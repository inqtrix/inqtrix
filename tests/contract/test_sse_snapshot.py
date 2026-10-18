"""Snapshot tests for the native run SSE event stream.

Locks the frame byte format (``event:``/``data:`` lines), the event
envelope keys, the monotonically increasing sequence numbers, the
auto-emitted ``inqtrix.run.snapshot`` companion events, and the exact
event-type order for a successful run. The future Postgres/Valkey event
bus must reproduce this stream byte-compatibly (sequence assignment
included) for the replay path.
"""

from __future__ import annotations

import json

import inqtrix.research.web_research as web_research_module
from inqtrix.server.runs import format_sse_event

from tests.contract._app import (
    make_contract_client,
    minimal_agent_result,
    parse_sse_frames,
    wait_for_run_status,
)

EVENT_ENVELOPE_KEYS = {"type", "run_id", "sequence", "created_at", "data"}


def test_format_sse_event_byte_format():
    event = {
        "type": "inqtrix.run.queued",
        "run_id": "run_abc",
        "sequence": 1,
        "created_at": 1700000000.0,
        "data": {"status": "queued", "note": "Ümlaut bleibt"},
    }

    frame = format_sse_event(event)

    assert frame == (
        "event: inqtrix.run.queued\n"
        "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
    )
    # ensure_ascii=False keeps non-ASCII characters readable on the wire.
    assert "Ümlaut" in frame


def _run_to_completion_and_fetch_events(client) -> list[dict]:
    created = client.post("/v1/runs", json={"question": "Was ist neu?"}).json()
    run_id = created["run_id"]
    wait_for_run_status(client, run_id, "completed")

    with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.read().decode("utf-8")

    events = []
    for event_name, data in parse_sse_frames(body):
        payload = json.loads(data)
        assert event_name == payload["type"], "event: line must mirror payload type"
        events.append(payload)
    return events


def test_successful_run_event_sequence_snapshot(monkeypatch):
    def fake_run(question, **kwargs):
        event_sink = kwargs["run_event_sink"]
        event_sink(
            "inqtrix.node.started",
            {
                "node": "classify",
                "snapshot": {"current_node": "classify", "active_round": 0},
            },
        )
        return minimal_agent_result(answer="Zwei Worte.")

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        events = _run_to_completion_and_fetch_events(client)

    # Envelope contract for every event.
    for event in events:
        assert set(event.keys()) == EVENT_ENVELOPE_KEYS
        assert isinstance(event["sequence"], int)
        assert isinstance(event["created_at"], float)

    # Sequence numbers are 1-based, strictly increasing, gap-free.
    assert [event["sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )

    # Exact event-type order for a successful run. Every payload that
    # carries a "snapshot" dict triggers an auto-emitted
    # inqtrix.run.snapshot companion BEFORE the actual event.
    types = [event["type"] for event in events]
    assert types == [
        "inqtrix.run.queued",
        "inqtrix.run.snapshot",       # companion of run.started (empty snapshot)
        "inqtrix.run.started",
        "inqtrix.run.snapshot",       # companion of node.started
        "inqtrix.node.started",
        "inqtrix.answer.started",
        "inqtrix.output_text.delta",  # "Zwei Worte." (one byte-bounded chunk)
        "inqtrix.answer.ready",
        "inqtrix.run.snapshot",       # companion of run.completed
        "inqtrix.run.completed",
    ]

    queued = events[0]
    assert queued["data"]["status"] == "queued"
    assert queued["data"]["queue_position"] == 1

    started = events[2]
    assert started["data"]["status"] == "running"

    node_started = events[4]
    assert node_started["data"]["node"] == "classify"
    assert node_started["data"]["snapshot"] == {
        "current_node": "classify",
        "active_round": 0,
    }

    publication = events[5]["data"]
    deltas = [event["data"] for event in events[6:7]]
    assert all(
        delta["publication_id"] == publication["publication_id"]
        for delta in deltas
    )
    assert [delta["offset"] for delta in deltas] == [0]
    assert "".join(delta["delta"] for delta in deltas) == "Zwei Worte."
    assert events[7]["data"]["bytes"] == len("Zwei Worte.".encode("utf-8"))

    completed = events[-1]
    assert completed["data"]["status"] == "completed"
    assert completed["data"]["result_url"].endswith("/result")
    assert isinstance(completed["data"]["snapshot"], dict)


def test_snapshot_companion_mirrors_status_and_snapshot(monkeypatch):
    def fake_run(question, **kwargs):
        kwargs["run_event_sink"](
            "inqtrix.node.started",
            {"node": "plan", "snapshot": {"current_node": "plan"}},
        )
        return minimal_agent_result(answer="Ok.")

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        events = _run_to_completion_and_fetch_events(client)

    snapshots = [e for e in events if e["type"] == "inqtrix.run.snapshot"]
    assert snapshots, "auto-emitted snapshot events missing"
    for snapshot_event in snapshots:
        assert set(snapshot_event["data"].keys()) == {"status", "snapshot"}

    node_companion = events[[e["type"] for e in events].index("inqtrix.node.started") - 1]
    assert node_companion["type"] == "inqtrix.run.snapshot"
    assert node_companion["data"]["snapshot"] == {"current_node": "plan"}


def test_late_subscriber_replay_terminates_after_terminal_event(monkeypatch):
    """Subscribing after completion replays the buffer and closes the stream.

    The stream ending (rather than hanging on live events) IS the
    contract here — ``client.stream(...).read()`` returning proves the
    generator exited after replaying the terminal event.
    """
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **kw: minimal_agent_result()
    )

    with make_contract_client() as client:
        events_first = _run_to_completion_and_fetch_events(client)

        # Second subscription replays the identical buffered sequence.
        run_id = events_first[0]["run_id"]
        with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
            body = response.read().decode("utf-8")

    events_second = [json.loads(data) for _, data in parse_sse_frames(body)]
    assert events_second == events_first


def test_failed_run_emits_failed_event_with_sanitized_error(monkeypatch):
    def exploding_run(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(web_research_module, "run_web_graph", exploding_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Frage"}).json()
        run_id = created["run_id"]
        summary = wait_for_run_status(client, run_id, "failed")

        with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
            body = response.read().decode("utf-8")

    assert summary["error"] == {
        "message": "provider exploded",
        "type": "server_error",
    }
    events = [json.loads(data) for _, data in parse_sse_frames(body)]
    assert events[-1]["type"] == "inqtrix.run.failed"
    assert events[-1]["data"]["status"] == "failed"
    assert events[-1]["data"]["error"]["type"] == "server_error"
