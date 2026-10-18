"""Line-delta pipeline: count once at the write, travel in events.

Pins the three layers separately: the counter's semantics, the store
contract (only the RETURNED record carries numbers — reads stay None in
both stores by design), and the shared event payload builder every
agent-side emitter uses. The mission memo emitter is pinned through the
real ``_flush_memo``, whose payload must carry ``title`` as well.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from inqtrix.agents.algorithm import _flush_memo
from inqtrix.agents.artifact_lines import (
    MAX_COUNTED_LINES,
    count_line_changes,
)
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    ArtifactBatchRevision,
    ArtifactRecord,
    artifact_event_payload,
)


# -- counter ---------------------------------------------------------------


def test_counts_insert_delete_and_replace_on_both_sides():
    assert count_line_changes("a\nb\nc", "a\nx\nc\nd") == (2, 1)
    assert count_line_changes("a\nb", "a\nb") == (0, 0)
    assert count_line_changes("", "eins\nzwei") == (2, 0)
    assert count_line_changes("eins\nzwei", "") == (0, 2)


def test_size_guard_returns_none_never_a_wrong_number():
    huge = "x\n" * (MAX_COUNTED_LINES + 1)
    assert count_line_changes(huge, "x") is None
    assert count_line_changes("x", huge) is None


# -- shared event payload --------------------------------------------------


def test_event_payload_carries_numbers_only_when_counted():
    counted = ArtifactRecord(
        artifact_id="art_1",
        run_id="run-1",
        kind="deliverable",
        title="Bericht",
        revision=3,
        lines_added=5,
        lines_removed=2,
    )
    payload = artifact_event_payload(counted)
    assert payload["title"] == "Bericht"
    assert payload["updated_by"] == "agent"
    assert payload["from_revision"] == 2
    assert (payload["lines_added"], payload["lines_removed"]) == (5, 2)

    uncounted = ArtifactRecord(
        artifact_id="art_1", run_id="run-1", kind="memo", revision=1
    )
    bare = artifact_event_payload(uncounted)
    assert "lines_added" not in bare and "lines_removed" not in bare
    assert bare["from_revision"] == 0


# -- memory store contract -------------------------------------------------


def _upsert(store: MemoryAgentControlStore, content: str, **overrides: Any):
    kwargs: dict[str, Any] = dict(
        run_id="run-1",
        kind="deliverable",
        session_id="sess-1",
        title="Dok",
        status="ready",
        content_markdown=content,
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_doc",
    )
    kwargs.update(overrides)
    return asyncio.run(store.upsert_artifact(**kwargs))


def test_upsert_returns_the_delta_and_reads_stay_none():
    store = MemoryAgentControlStore()
    created = _upsert(store, "eins\nzwei")
    assert (created.lines_added, created.lines_removed) == (2, 0)
    updated = _upsert(store, "eins\ndrei\nvier")
    assert (updated.lines_added, updated.lines_removed) == (2, 1)
    # Read parity with Postgres: the stored row never re-serves a count.
    read, _ = asyncio.run(store.get_artifact("run-1", "art_doc"))
    assert read.lines_added is None and read.lines_removed is None


def test_batch_revise_returns_the_delta_per_row():
    store = MemoryAgentControlStore()
    _upsert(store, "eins\nzwei")
    rows = asyncio.run(
        store.revise_session_artifacts_atomically(
            run_id="run-2",
            session_id="sess-1",
            revisions=[
                ArtifactBatchRevision(
                    artifact_id="art_doc",
                    expected_revision=1,
                    content_markdown="eins\nzwei\ndrei",
                )
            ],
        )
    )
    assert (rows[0].lines_added, rows[0].lines_removed) == (1, 0)
    read, _ = asyncio.run(store.get_artifact("run-2", "art_doc"))
    assert read.lines_added is None


# -- mission memo emitter --------------------------------------------------


class _FakeDeps:
    def __init__(self, store: MemoryAgentControlStore, run_id: str) -> None:
        self.control = store
        self.context = SimpleNamespace(run_id=run_id)
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def test_memo_flush_event_carries_title_and_line_delta():
    store = MemoryAgentControlStore()
    deps = _FakeDeps(store, run_id="run-memo")
    state: dict[str, Any] = {
        "session_id": "sess-memo",
        "artifact_id": "",
        "memo_base_revision": 0,
        "memo_title": "Marktmemo",
    }
    _flush_memo(deps, state, "zeile eins\nzeile zwei", status="ready")
    event_type, payload = deps.events[-1]
    assert event_type == "inqtrix.agent.artifact.created"
    assert payload["kind"] == "memo"
    assert payload["title"] == "Marktmemo"
    assert (payload["lines_added"], payload["lines_removed"]) == (2, 0)

    _flush_memo(deps, state, "zeile eins\nzeile drei", status="ready")
    event_type, payload = deps.events[-1]
    assert event_type == "inqtrix.agent.artifact.updated"
    assert payload["title"] == "Marktmemo"
    assert payload["from_revision"] == 1
    assert (payload["lines_added"], payload["lines_removed"]) == (1, 1)
