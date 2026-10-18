"""Unit tests for the shared run-event expansion and store selection.

``expand_run_event`` is the single implementation of the snapshot-
companion rule both store backends emit through; the selection bridge
``build_run_store`` is the startup guard that rejects contradictory
backend combinations loudly.
"""

from __future__ import annotations

import pytest

from inqtrix.runs.shared import (
    access_permits_edit,
    build_run_summary,
    clipped_question,
    expand_run_event,
    question_was_clipped,
)
from inqtrix.server.container import build_run_store
from inqtrix.server.runs import RunStore
from inqtrix.settings import (
    QueueSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


def test_plain_event_passes_through_unexpanded():
    new_snapshot, events = expand_run_event(
        "inqtrix.run.queued",
        {"status": "queued", "queue_position": 1},
        status="queued",
    )
    assert new_snapshot is None
    assert events == [
        ("inqtrix.run.queued", {"status": "queued", "queue_position": 1})
    ]


def test_snapshot_payload_emits_companion_before_carrier():
    snapshot = {"current_node": "answer", "done": True}
    new_snapshot, events = expand_run_event(
        "inqtrix.run.completed",
        {"status": "completed", "snapshot": snapshot},
        status="completed",
    )
    assert new_snapshot == snapshot
    assert [event_type for event_type, _ in events] == [
        "inqtrix.run.snapshot",
        "inqtrix.run.completed",
    ]
    assert events[0][1] == {"status": "completed", "snapshot": snapshot}


def test_snapshot_event_itself_gets_no_companion():
    _, events = expand_run_event(
        "inqtrix.run.snapshot",
        {"status": "running", "snapshot": {"done": False}},
        status="running",
    )
    assert [event_type for event_type, _ in events] == ["inqtrix.run.snapshot"]


# ------------------------------------------------------------------ #
# access_permits_edit — the run-mutation gate predicate
# ------------------------------------------------------------------ #


def test_access_permits_edit_requires_explicit_access_annotation():
    """A missing annotation is malformed and must fail closed."""
    assert access_permits_edit(None) is False


@pytest.mark.parametrize("mode", ["owner", "unscoped"])
def test_access_permits_edit_allows_owner_modes(mode):
    assert access_permits_edit({"mode": mode}) is True


@pytest.mark.parametrize(
    "permission, allowed",
    [
        ("view", False),
        ("edit", True),
    ],
)
def test_access_permits_edit_respects_ordered_permission(permission, allowed):
    """A shared-in grant permits mutation only for ``edit``."""
    access = {"mode": "shared", "permission": permission}
    assert access_permits_edit(access) is allowed


def test_access_permits_edit_unknown_permission_denies():
    """An unrecognized permission string is treated as no access, not a crash."""
    assert (
        access_permits_edit({"mode": "shared", "permission": "bogus"})
        is False
    )


# ------------------------------------------------------------------ #
# build_run_store selection and startup guards
# ------------------------------------------------------------------ #


def test_default_settings_select_the_memory_store():
    store = build_run_store(Settings())
    assert isinstance(store, RunStore)


def test_valkey_queue_without_postgres_fails_loudly():
    settings = Settings(
        queue=QueueSettings(backend="valkey", valkey_url="redis://x:1/0")
    )
    with pytest.raises(RuntimeError, match="INQTRIX_STORAGE_BACKEND=postgres"):
        build_run_store(settings)


def test_valkey_queue_without_url_fails_loudly():
    settings = Settings(
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/inqtrix",
        ),
        queue=QueueSettings(backend="valkey"),
    )
    with pytest.raises(RuntimeError, match="INQTRIX_VALKEY_URL"):
        build_run_store(settings)


def test_postgres_storage_without_url_fails_loudly():
    settings = Settings(storage=StorageSettings(backend="postgres"))
    with pytest.raises(RuntimeError, match="INQTRIX_DATABASE_URL"):
        build_run_store(settings)


def test_postgres_storage_selects_the_durable_store():
    from inqtrix.runs.postgres_store import PostgresRunStore

    settings = Settings(
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/inqtrix",
        )
    )
    store = build_run_store(settings)
    assert isinstance(store, PostgresRunStore)


def test_postgres_store_uses_durable_retention_not_replay_ttl():
    """The durable store must keep terminal runs for the durable retention
    window, not the in-memory store's short replay TTL; otherwise completed
    research reports are pruned minutes after finishing.

    Distinct values pin the wiring: a revert to ``run_completed_ttl_seconds``
    would make this go red.
    """
    from inqtrix.runs.postgres_store import PostgresRunStore

    settings = Settings(
        server=ServerSettings(
            run_completed_ttl_seconds=300,
            run_durable_retention_seconds=7_776_000,
        ),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/inqtrix",
        ),
    )
    store = build_run_store(settings)
    assert isinstance(store, PostgresRunStore)
    assert store._completed_ttl_seconds == 7_776_000


def test_run_durable_retention_defaults_to_ninety_days():
    """The out-of-the-box durable retention is 90 days (operators can extend)."""
    assert ServerSettings().run_durable_retention_seconds == 90 * 86_400


def test_a_dispatching_store_carries_its_own_slot_count():
    """The base declares the attribute; each dispatching store must fill it.

    ``_max_concurrent`` is not only the dispatch ceiling: the queue-full
    admission check reads it as ``running >= self._max_concurrent``. A store
    that inherited a default of zero would not "dispatch nothing" -- it would
    read as permanently at capacity and reject work that should have been
    admitted, silently and only once the queue filled.
    """
    from inqtrix.runs.postgres_store import PostgresRunStore

    settings = Settings(
        server=ServerSettings(run_max_concurrent=7),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/inqtrix",
        ),
    )
    store = build_run_store(settings)
    assert isinstance(store, PostgresRunStore)
    assert store._max_concurrent == 7, (
        "the run store must size itself from the operator's run cap; an "
        "inherited default would read as permanently at capacity"
    )


def test_the_base_offers_no_silent_slot_default():
    """A forgotten assignment must raise, not read as zero.

    Zero is the worst possible fallback here: it is indistinguishable from
    'at capacity' to every caller that compares against it.
    """
    from inqtrix.runs.durable_store import DurableJobStoreBase

    assert not hasattr(DurableJobStoreBase, "_max_concurrent"), (
        "a class-level default turns a missing assignment into a silent "
        "rejection instead of a loud failure"
    )


# -- A clipped question stays reachable ------------------------------------


class _AskedRow:
    """Minimal record view: what the column holds vs what was asked."""

    def __init__(self, question: str, asked: str | None) -> None:
        self.question = question
        self.question_full = asked


def test_a_question_that_fits_is_not_marked_clipped() -> None:
    row = _AskedRow("Kurze Frage?", "Kurze Frage?")
    assert question_was_clipped(row) is False


def test_a_clipped_question_is_marked() -> None:
    asked = "A" * 900
    row = _AskedRow(clipped_question(asked), asked)
    assert question_was_clipped(row) is True


def test_a_question_the_user_ended_with_an_ellipsis_is_not_marked() -> None:
    """The flag compares lengths; it never sniffs a trailing ellipsis.

    Guessing from the character would put a dead "show more" control on
    a question the user deliberately trailed off.
    """
    asked = "Und was dann …"
    row = _AskedRow(clipped_question(asked), asked)
    assert clipped_question(asked) == asked
    assert question_was_clipped(row) is False


def test_a_missing_payload_never_claims_a_clip() -> None:
    """Older rows carry no durable question; they must not offer more."""
    assert question_was_clipped(_AskedRow("Frage", None)) is False
    assert question_was_clipped(_AskedRow("Frage", "")) is False


def test_the_row_view_lifts_the_question_from_where_it_actually_lives() -> None:
    """The submitted question is a TOP-LEVEL payload key.

    Written after a live check found the button missing: the lift had
    copied ``origin_key``'s path, and that one lives under ``body``. The
    decision tests above all passed, because they set ``question_full``
    by hand and never exercised the lookup.
    """
    from inqtrix.runs.postgres_store import _row_view

    asked = "A" * 900
    row = {
        "run_id": "run_x",
        "status": "queued",
        "question": clipped_question(asked),
        "stack_name": "default",
        "workspace_id": None,
        "mode": "agent_kernel",
        "agent_overrides": {},
        "created_at": 0.0,
        "started_at": None,
        "finished_at": None,
        "segment_count": 0,
        "event_seq": 0,
        "current_segment_id": None,
        "queued_since": None,
        "active_started_at": None,
        "active_seconds": 0.0,
        "waiting_seconds": 0.0,
        "queued_seconds": 0.0,
        "waiting_since": None,
        "snapshot": {},
        "error": None,
        "kind": "standard",
        "parent_run_id": None,
        "root_run_id": None,
        "session_id": None,
        "cancel_requested": False,
        # The real shape: question at the top, origin_key under body.
        "request_payload": {
            "question": asked,
            "body": {"origin_key": "ok_1", "mode": "agent_kernel"},
        },
    }
    view = _row_view(row)
    assert view.question_full == asked
    assert view.origin_key == "ok_1"
    assert question_was_clipped(view) is True


class _Record:
    """Minimal record view: only what ``build_run_summary`` reads."""

    def __init__(self, **fields):
        self.run_id = "run_1"
        self.status = "queued"
        self.question = "Was hat sich geaendert?"
        self.stack_name = "default"
        self.workspace_id = None
        self.mode = "agent_kernel"
        self.agent_overrides = {}
        self.created_at = 1_700_000_000.0
        self.started_at = 1_700_000_001.0
        self.finished_at = None
        self.snapshot = {}
        self.error = None
        for key, value in fields.items():
            setattr(self, key, value)


def test_an_agent_summary_states_where_the_event_stream_stood() -> None:
    """A client holding the summary AND the stream needs to know which is ahead.

    The decision endpoints answer with a summary read inside the
    transaction that re-queues the run, so it says ``queued`` while the
    worker already runs it.  Without this position a client cannot tell
    that summary from a newer one and shows a queue for working run.
    """
    summary = build_run_summary(
        _Record(kind="agent", event_seq=15),
        queue_position=1,
        access={"mode": "owner"},
    )

    assert summary["last_event_sequence"] == 15


def test_a_standard_summary_keeps_its_exact_historical_key_set() -> None:
    summary = build_run_summary(
        _Record(event_seq=15),
        queue_position=1,
        access={"mode": "owner"},
    )

    assert "last_event_sequence" not in summary


def test_a_record_view_without_the_field_still_summarizes() -> None:
    summary = build_run_summary(
        _Record(kind="agent"),
        queue_position=None,
        access={"mode": "owner"},
    )

    assert summary["last_event_sequence"] == 0
