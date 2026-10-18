"""_flush_memo conflict reconciliation.

The session memo is one row shared across turns and the graph checkpoints
only at node boundaries. These tests pin that ``_flush_memo`` reconciles a
revision conflict by PROVENANCE (``updated_by``) instead of blindly
treating whatever it finds as a user edit:

* the agent's OWN partial write from a crashed attempt must be ADOPTED
  (no "manually edited" banner, no misattribution);
* a genuine user edit must be PRESERVED and appended below;
* a concurrently-inserted row on a fresh session must NOT be clobbered.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


from inqtrix.agents.algorithm import _MEMO_CONFLICT_SEPARATOR, _flush_memo
from inqtrix.agents.control_memory import MemoryAgentControlStore


class _FakeDeps:
    """The narrow surface ``_flush_memo`` touches (control + emit + run_id)."""

    def __init__(self, store: MemoryAgentControlStore, run_id: str) -> None:
        self.control = store
        self.context = SimpleNamespace(run_id=run_id)
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def _seed(store: MemoryAgentControlStore, *, run_id, session_id, artifact_id,
          content, updated_by, status="ready", revisions=1):
    """Advance a memo row to ``revisions`` under a given final provenance."""
    for index in range(revisions):
        last = index == revisions - 1
        asyncio.run(
            store.upsert_artifact(
                run_id=run_id,
                kind="memo",
                session_id=session_id,
                title="Memo",
                status=status if last else "writing",
                content_markdown=content if last else "partial",
                payload={},
                refs=[],
                updated_by=updated_by if last else "agent",
                artifact_id=artifact_id,
            )
        )


def test_flush_adopts_the_agents_own_partial_on_crash_retry():
    """DB ahead of the checkpointed base with updated_by='agent' (a crashed
    attempt's partial) is ADOPTED, never flagged as a user edit."""
    store = MemoryAgentControlStore()
    # The crashed attempt left the row at revision 2, authored by the agent.
    _seed(
        store,
        run_id="run-new",
        session_id="sess",
        artifact_id="art_sess_memo",
        content="# Partial vom Agenten",
        updated_by="agent",
        status="writing",
        revisions=2,
    )
    deps = _FakeDeps(store, run_id="run-new")
    # The retry re-enters synthesis with the checkpointed base reverted.
    state = {
        "session_id": "sess",
        "artifact_id": "art_sess_memo",
        "memo_base_revision": 1,
        "memo_title": "Memo",
    }

    _flush_memo(deps, state, "# Neuer Abschnitt", status="writing")

    record, _ = asyncio.run(store.get_artifact("run-new", "art_sess_memo"))
    assert record.content_markdown == "# Neuer Abschnitt"
    assert _MEMO_CONFLICT_SEPARATOR not in record.content_markdown
    assert state.get("memo_user_prefix", "") == ""
    assert all(
        e[0] != "inqtrix.agent.artifact.edit_conflict" for e in deps.events
    )
    # Base advanced past the adopted revision.
    assert state["memo_base_revision"] == 3


def test_flush_preserves_a_genuine_user_edit_and_appends_below():
    """A user edit since intake is kept as a prefix; the run's memo is
    appended below it and the conflict is surfaced exactly once."""
    store = MemoryAgentControlStore()
    _seed(
        store,
        run_id="run-old",
        session_id="sess",
        artifact_id="art_sess_memo",
        content="# Vom Nutzer bearbeitet",
        updated_by="user",
        status="ready",
        revisions=2,
    )
    deps = _FakeDeps(store, run_id="run-new")
    state = {
        "session_id": "sess",
        "artifact_id": "art_sess_memo",
        "memo_base_revision": 1,  # stale: the user advanced it to 2
        "memo_title": "Memo",
    }

    _flush_memo(deps, state, "# Agenten-Update A", status="writing")

    record, _ = asyncio.run(store.get_artifact("run-new", "art_sess_memo"))
    assert record.content_markdown.startswith("# Vom Nutzer bearbeitet")
    assert _MEMO_CONFLICT_SEPARATOR in record.content_markdown
    assert record.content_markdown.endswith("# Agenten-Update A")
    assert state["memo_user_prefix"] == "# Vom Nutzer bearbeitet"
    conflicts = [
        e for e in deps.events
        if e[0] == "inqtrix.agent.artifact.edit_conflict"
    ]
    assert len(conflicts) == 1


def test_flush_preserves_a_foreign_agent_runs_write() -> None:
    """An agent marker alone never authorizes a different run to overwrite."""
    store = MemoryAgentControlStore()
    _seed(
        store,
        run_id="run-foreign",
        session_id="sess",
        artifact_id="art_sess_memo",
        content="# Foreign agent output",
        updated_by="agent",
        status="writing",
        revisions=2,
    )
    deps = _FakeDeps(store, run_id="run-current")
    state = {
        "session_id": "sess",
        "artifact_id": "art_sess_memo",
        "memo_base_revision": 1,
        "memo_title": "Memo",
    }

    _flush_memo(deps, state, "# Current run output", status="writing")

    record, _ = asyncio.run(
        store.get_artifact("run-current", "art_sess_memo")
    )
    assert record.content_markdown.startswith("# Foreign agent output")
    assert record.content_markdown.endswith("# Current run output")
    assert _MEMO_CONFLICT_SEPARATOR in record.content_markdown
    assert len(
        [
            event
            for event in deps.events
            if event[0] == "inqtrix.agent.artifact.edit_conflict"
        ]
    ) == 1

    # A second flush of the SAME run keeps the user prefix and appends the
    # cumulative memo (never drops the preserved text, never re-conflicts).
    _flush_memo(deps, state, "# Agenten-Update A und B", status="writing")
    record2, _ = asyncio.run(
        store.get_artifact("run-current", "art_sess_memo")
    )
    assert record2.content_markdown.startswith("# Foreign agent output")
    assert record2.content_markdown.endswith("# Agenten-Update A und B")
    # Still exactly one conflict event across both flushes.
    conflicts = [
        e for e in deps.events
        if e[0] == "inqtrix.agent.artifact.edit_conflict"
    ]
    assert len(conflicts) == 1


def test_flush_on_fresh_session_catches_a_concurrent_insert():
    """A memo created (by the user or a racing run) between intake and the
    first flush is a conflict, not a silent clobber — expected_revision=0."""
    store = MemoryAgentControlStore()
    # Nothing at intake -> base 0. A concurrent USER insert appears.
    asyncio.run(
        store.upsert_artifact(
            run_id="run-other",
            kind="memo",
            session_id="sess",
            title="Memo",
            status="ready",
            content_markdown="# Nutzer war zuerst",
            payload={},
            refs=[],
            updated_by="user",
            artifact_id="art_sess_memo",
        )
    )
    deps = _FakeDeps(store, run_id="run-new")
    state = {"session_id": "sess", "memo_base_revision": 0, "memo_title": "Memo"}

    _flush_memo(deps, state, "# Agenten-Memo", status="writing")

    record, _ = asyncio.run(store.get_artifact("run-new", "art_sess_memo"))
    # The user's text survived (preserved as prefix), agent memo appended.
    assert record.content_markdown.startswith("# Nutzer war zuerst")
    assert _MEMO_CONFLICT_SEPARATOR in record.content_markdown
    assert any(
        e[0] == "inqtrix.agent.artifact.edit_conflict" for e in deps.events
    )


def test_flush_recovers_the_user_prefix_from_a_crashed_recovery_partial():
    """Compound race: a pre-crash partial already embedded a preserved user
    edit. On retry (checkpoint state reverted) the adopt path must recover
    that user text from the row, not overwrite it away."""
    store = MemoryAgentControlStore()
    # The crashed attempt wrote "userprefix + SEP + partial" as the agent.
    embedded = "# Vom Nutzer" + _MEMO_CONFLICT_SEPARATOR + "# Agent partial"
    _seed(
        store,
        run_id="run-new",
        session_id="sess",
        artifact_id="art_sess_memo",
        content=embedded,
        updated_by="agent",
        status="writing",
        revisions=2,
    )
    deps = _FakeDeps(store, run_id="run-new")
    # Retry: checkpointed prefix is gone, base reverted.
    state = {
        "session_id": "sess",
        "artifact_id": "art_sess_memo",
        "memo_base_revision": 1,
        "memo_title": "Memo",
    }

    _flush_memo(deps, state, "# Agent neu", status="writing")

    record, _ = asyncio.run(store.get_artifact("run-new", "art_sess_memo"))
    # The user's text survived the crash+retry, agent memo re-appended once.
    assert record.content_markdown.startswith("# Vom Nutzer")
    assert record.content_markdown.endswith("# Agent neu")
    assert record.content_markdown.count(_MEMO_CONFLICT_SEPARATOR) == 1
    assert state["memo_user_prefix"] == "# Vom Nutzer"


def test_flush_creates_the_memo_when_the_session_is_truly_empty():
    """No prior row -> a clean create at revision 1 with the created event."""
    store = MemoryAgentControlStore()
    deps = _FakeDeps(store, run_id="run-new")
    state = {"session_id": "sess", "memo_base_revision": 0, "memo_title": "Memo"}

    _flush_memo(deps, state, "# Erstes Memo", status="writing")

    record, _ = asyncio.run(store.get_artifact("run-new", "art_sess_memo"))
    assert record.revision == 1
    assert record.content_markdown == "# Erstes Memo"
    assert any(
        e[0] == "inqtrix.agent.artifact.created" for e in deps.events
    )


def test_flush_adopts_own_partial_on_crash_retry_of_a_sessionless_run():
    """A SESSION-LESS run's crash-retry must reconcile too.

    A session-less run (no ``session_id``) writes its memo under the
    run-derived ``artifact_id`` with ``session_id=None``. The old recovery
    read ``get_session_artifact("")`` — which filters on session_id equality
    and so can never match a ``NULL``-session row — so a crash-retry conflict
    found nothing, reset base to 0, re-conflicted, and exhausted the loop with
    ``RuntimeError("memo_flush_conflict_unresolved")``. Recovering by the
    run-scoped ``artifact_id`` (the same key the write used) adopts the
    agent's own partial exactly like the session case.
    """
    store = MemoryAgentControlStore()
    # Same run, resumed: seed and retry share run_id, so the run-derived
    # artifact_id is stable. The crashed attempt left revision 2 (agent).
    _seed(
        store,
        run_id="runX",
        session_id=None,
        artifact_id="art_runX_memo",
        content="# Partial vom Agenten",
        updated_by="agent",
        status="writing",
        revisions=2,
    )
    deps = _FakeDeps(store, run_id="runX")
    # Retry state: NO session_id, checkpointed base reverted to 1.
    state = {"memo_base_revision": 1, "memo_title": "Memo"}

    _flush_memo(deps, state, "# Neuer Abschnitt", status="writing")

    record, _ = asyncio.run(store.get_artifact("runX", "art_runX_memo"))
    assert record.content_markdown == "# Neuer Abschnitt"
    assert _MEMO_CONFLICT_SEPARATOR not in record.content_markdown
    assert state.get("memo_user_prefix", "") == ""
    assert all(
        e[0] != "inqtrix.agent.artifact.edit_conflict" for e in deps.events
    )
    # Adopted the crashed partial's revision and advanced past it.
    assert state["memo_base_revision"] == 3
