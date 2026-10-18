"""Contract for the SSE replay/live boundary marker.

A subscriber that attaches to a RUNNING run first receives the buffered
replay, then live frames — with nothing separating the two, a client
cannot tell history from news, and a reload into a running run replayed
its whole event story as if each line were new (the UI animated every
one). The stream therefore emits exactly one ``inqtrix.stream.live``
control frame between the replay and the live tail.

Pinned here:
- the marker appears exactly once, after every replayed frame and before
  the live tail;
- it is transport state: no ``sequence``, and it never appears in the
  persisted buffer (the ``?format=json`` page);
- a terminal run's stream (ends right after its replay) has NO marker —
  the existing byte-snapshot in ``test_sse_snapshot.py`` stays valid.

The TestClient does not deliver SSE chunks incrementally (the body
arrives once the stream ends), so the running-run test synchronises on
the STORE's subscriber registration instead of on stream reads: a
helper thread holds the run in RUNNING, waits until the events route has
attached its queue (``record.subscribers``), and only then releases the
worker. Everything the subscriber saw before that release is replay by
construction; the terminal tail is live.
"""

from __future__ import annotations

import json
import threading

import inqtrix.research.web_research as web_research_module

from tests.contract._app import (
    make_contract_client,
    minimal_agent_result,
    parse_sse_frames,
    wait_for_run_status,
)

MARKER_TYPE = "inqtrix.stream.live"


def test_running_stream_emits_one_marker_after_the_replay(monkeypatch):
    release = threading.Event()

    def blocking_run(question, **kwargs):
        event_sink = kwargs["run_event_sink"]
        event_sink(
            "inqtrix.progress.message",
            {"message": "Erste Zeile", "snapshot": {"current_node": "plan"}},
        )
        # Hold the run in RUNNING while the subscriber attaches, so the
        # frames above are REPLAY for it and everything after is live.
        assert release.wait(timeout=10.0), "test release latch timed out"
        return minimal_agent_result(answer="Fertig.")

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Läuft?"}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "running")

        # The in-memory store the routes serve from; reaching into
        # ``record.subscribers`` is what makes the race-free sequencing
        # possible (see module docstring).
        run_store = client.app.state.container.run_store
        record = run_store._records[run_id]  # noqa: SLF001 - test-only probe

        def release_once_subscribed() -> None:
            for _ in range(500):
                if record.subscribers:
                    break
                threading.Event().wait(0.01)
            release.set()

        releaser = threading.Thread(target=release_once_subscribed)
        releaser.start()
        try:
            with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
                assert response.status_code == 200
                body = response.read().decode("utf-8")
        finally:
            releaser.join(timeout=10.0)

        frames = [
            (name, json.loads(data))
            for name, data in parse_sse_frames(body)
            if data
        ]
        types = [payload["type"] for _name, payload in frames]

        assert types.count(MARKER_TYPE) == 1
        marker_index = types.index(MARKER_TYPE)
        _name, marker = frames[marker_index]
        # Transport state, not a run event: no sequence to disturb resume
        # cursors, but still addressed to the run.
        assert "sequence" not in marker
        assert marker["run_id"] == run_id

        # Everything before the marker is the replay; the progress line the
        # worker emitted before the subscription attached must be in it.
        replayed_types = types[:marker_index]
        assert "inqtrix.progress.message" in replayed_types
        assert "inqtrix.run.completed" not in replayed_types
        # The live tail (released only after subscribing) follows the marker.
        assert "inqtrix.run.completed" in types[marker_index + 1 :]

        # The marker is never persisted: the polling page serves the buffer.
        page = client.get(f"/v1/runs/{run_id}/events?format=json").json()
        page_types = [event["type"] for event in page["data"]]
        assert MARKER_TYPE not in page_types
        assert page["terminal"] is True


def test_terminal_stream_has_no_marker(monkeypatch):
    def fake_run(question, **kwargs):
        return minimal_agent_result(answer="Sofort fertig.")

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        created = client.post("/v1/runs", json={"question": "Kurz."}).json()
        run_id = created["run_id"]
        wait_for_run_status(client, run_id, "completed")

        with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
            body = response.read().decode("utf-8")

        types = [
            json.loads(data)["type"]
            for _name, data in parse_sse_frames(body)
            if data
        ]
        assert MARKER_TYPE not in types
        assert types[-1] == "inqtrix.run.completed"
