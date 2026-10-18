"""Tests for the native in-memory run queue."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import pytest

import inqtrix.server.runs as runs_module
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.server.runs import (
    RunActive,
    RunHandle,
    RunNotFound,
    RunQueueFull,
    RunParentInactive,
    RunSessionActive,
    RunStatus,
    RunStore,
    format_sse_event,
)
from inqtrix.settings import ServerSettings


OWNER_1 = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OWNER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
INTRUDER = uuid.UUID("99999999-9999-4999-8999-999999999999")


def _visible_to(user_id: uuid.UUID) -> UserContext:
    """Build the canonical-user context required for owned runs."""
    return UserContext(principal=Principal(user_id=user_id, kind="oidc_session"))


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Wait until *predicate* becomes true or fail the test."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


def _store(
    *,
    max_concurrent: int = 1,
    max_queue_size: int = 1,
    completed_ttl_seconds: int = 30,
    event_buffer_size: int = 10,
) -> RunStore:
    """Create a small run store for unit tests."""
    return RunStore(
        max_concurrent=max_concurrent,
        max_queue_size=max_queue_size,
        completed_ttl_seconds=completed_ttl_seconds,
        event_buffer_size=event_buffer_size,
    )


def test_from_settings_uses_native_run_limits() -> None:
    settings = ServerSettings(
        MAX_CONCURRENT=2,
        RUN_MAX_CONCURRENT=1,
        RUN_QUEUE_MAX_SIZE=7,
        RUN_COMPLETED_TTL_SECONDS=9,
        RUN_EVENT_BUFFER_SIZE=11,
    )

    store = RunStore.from_settings(settings)

    assert store._max_concurrent == 1
    assert store.submit(
        question="q", stack_name="default", work=lambda handle: handle.complete({})
    )["status"] in {"queued", "running", "completed"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_concurrent": 0}, "max_concurrent must be >= 1"),
        ({"max_queue_size": -1}, "max_queue_size must be >= 0"),
        ({"completed_ttl_seconds": -1}, "completed_ttl_seconds must be >= 0"),
        ({"event_buffer_size": 0}, "event_buffer_size must be >= 1"),
    ],
)
def test_store_rejects_invalid_limits(kwargs: dict[str, int], message: str) -> None:
    options = {
        "max_concurrent": 1,
        "max_queue_size": 1,
        "completed_ttl_seconds": 30,
        "event_buffer_size": 10,
        **kwargs,
    }

    with pytest.raises(ValueError, match=message):
        RunStore(**options)


def test_submit_dispatches_first_run_and_keeps_second_queued() -> None:
    first_started = threading.Event()
    release_first = threading.Event()

    def blocking_work(handle: RunHandle) -> None:
        first_started.set()
        release_first.wait(timeout=1)
        handle.complete({"answer": "done"})

    store = _store(max_concurrent=1, max_queue_size=2)

    first = store.submit(question="first", stack_name="default", work=blocking_work)
    _wait_until(lambda: first_started.is_set())
    second = store.submit(
        question="second",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "second"}),
        agent_overrides={"max_rounds": 2},
    )

    assert store.get(first["run_id"])["status"] == "running"
    assert second["status"] == "queued"
    assert second["queue_position"] == 1
    assert second["agent_overrides"] == {"max_rounds": 2}

    release_first.set()
    _wait_until(lambda: store.get(second["run_id"])["status"] == "completed")


def test_submit_summary_includes_run_mode() -> None:
    store = _store(max_concurrent=1, max_queue_size=1)

    summary = store.submit(
        question="direct",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "done"}),
        mode="direct_llm",
    )

    assert summary["mode"] == "direct_llm"
    _wait_until(lambda: store.get(summary["run_id"])["status"] == "completed")
    assert store.get(summary["run_id"])["mode"] == "direct_llm"


def test_submit_clips_the_question_column_visibly() -> None:
    """The 500-char question bound must SHOW its cut (no silent caps).

    The full text keeps living in request_payload for execution/replay;
    the summary column is a display bound — a cut that hides itself made
    a delegated assignment end mid-word on the gate card."""
    store = _store(max_concurrent=1, max_queue_size=1)
    long_question = "Erstelle ein sehr ausfuehrliches Memo. " * 30

    summary = store.submit(
        question=long_question,
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "done"}),
        request_payload={"question": long_question, "body": {}},
    )

    shown = summary["question"]
    assert shown.endswith("…")
    assert len(shown) == 501
    assert shown[:-1] == long_question[:500]
    _wait_until(lambda: store.get(summary["run_id"])["status"] == "completed")

    short = store.submit(
        question="kurz",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "done"}),
    )
    assert short["question"] == "kurz"
    _wait_until(lambda: store.get(short["run_id"])["status"] == "completed")


def test_submit_retries_run_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["run_duplicate", "run_duplicate", "run_unique"])
    monkeypatch.setattr(runs_module, "new_run_id", lambda: next(ids))
    store = _store(max_concurrent=1, max_queue_size=2)

    first = store.submit(
        question="first",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "first"}),
    )
    second = store.submit(
        question="second",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "second"}),
    )

    assert first["run_id"] == "run_duplicate"
    assert second["run_id"] == "run_unique"
    _wait_until(lambda: store.get(second["run_id"])["status"] == "completed")


def test_list_and_get_can_filter_by_workspace_id() -> None:
    store = _store(max_concurrent=1, max_queue_size=2)
    run_a = store.submit(
        question="workspace a",
        stack_name="default",
        workspace_id="ws_browser_a",
        work=lambda handle: handle.complete({"answer": "a"}),
    )
    run_b = store.submit(
        question="workspace b",
        stack_name="default",
        workspace_id="ws_browser_b",
        work=lambda handle: handle.complete({"answer": "b"}),
    )

    summaries = store.list(workspace_id="ws_browser_a")

    assert [summary["run_id"] for summary in summaries] == [run_a["run_id"]]
    assert (
        store.get(run_a["run_id"], workspace_id="ws_browser_a")["workspace_id"]
        == "ws_browser_a"
    )
    with pytest.raises(RunNotFound):
        store.get(run_b["run_id"], workspace_id="ws_browser_a")


def test_delete_removes_terminal_run_for_owner() -> None:
    store = _store(max_queue_size=2)
    imported = store.import_completed_run(
        source_run_id="local_del_1",
        question="report",
        stack_name="default",
        result={"answer": "body"},
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )

    run_id = imported["run_id"]
    assert run_id != "local_del_1"
    store.delete(run_id, workspace_id="ws_owner", requester_user_id=OWNER_1)

    # The run is gone from the durable surface, so a reload cannot re-hydrate
    # it (the regression behind "deleted report comes back").
    assert store.list(workspace_id="ws_owner") == []
    with pytest.raises(RunNotFound):
        store.get(run_id, workspace_id="ws_owner")
    # Delete is not idempotent: a repeat is a clean 404, not a crash.
    with pytest.raises(RunNotFound):
        store.delete(run_id, workspace_id="ws_owner", requester_user_id=OWNER_1)


def test_project_child_run_outcome_keeps_only_cited_references() -> None:
    """The condensed child projection hands the parent only what the child's
    report cites; an examined-but-uncited source (``cited: False``) is not
    grounded evidence. Older payloads without the flag count as cited."""
    from inqtrix.agents.scheduler import project_child_run_outcome

    store = _store(max_concurrent=2, max_queue_size=4)
    child = store.submit(
        question="child",
        stack_name="default",
        work=lambda handle: handle.complete(
            {
                "answer": "Kindbericht [W1]",
                "references": [
                    {"label": "W1", "url": "https://example.com/q", "title": "Zitiert", "cited": True},
                    {"label": "W2", "url": "https://example.com/x", "title": "Nur gesichtet", "cited": False},
                    {"label": "W3", "url": "https://example.com/legacy", "title": "Alt"},
                ],
            }
        ),
    )
    child_id = child["run_id"]
    _wait_until(lambda: store.get(child_id)["status"] == "completed")
    outcome = project_child_run_outcome(store, child_id, 1)
    assert outcome is not None and outcome.status == "completed"
    assert [reference["url"] for reference in outcome.evidence] == [
        "https://example.com/q",
        "https://example.com/legacy",
    ]


def test_project_child_run_outcome_needs_visible_to_for_owned_child() -> None:
    """An owned run is invisible to a ``visible_to=None`` projection.

    The RLS visibility twin: ``visible_to=None`` sees only ownerless rows
    (``created_by_user_id IS NULL``), so projecting an authenticated
    user's child WITHOUT the owner context yields ``child_row_missing``.
    A child-report path that drops ``visible_to`` silently loses completed
    children on every authenticated run, and the unit harness's ownerless
    runs mask it, so this test PINS the contract the kernel/mission both
    depend on.
    """
    from inqtrix.agents.scheduler import project_child_run_outcome

    store = _store(max_concurrent=2, max_queue_size=4)
    # A plain owned run stands in for the child row: the projector reads
    # purely by run_id, so the visibility gating it exercises is identical
    # (kind is not part of the read authorization).
    child = store.submit(
        question="child",
        stack_name="default",
        work=lambda handle: handle.complete(
            {
                "answer": "Kindbericht [W1]",
                "references": [
                    {
                        "label": "W1",
                        "url": "https://example.com/q",
                        "title": "Quelle",
                    }
                ],
            }
        ),
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    child_id = child["run_id"]
    ctx = _visible_to(OWNER_1)
    _wait_until(lambda: store.get(child_id, visible_to=ctx)["status"] == "completed")

    # Without the owner context the owned child is hidden -> the exact
    # live symptom (child_row_missing), NOT the true outcome.
    blind = project_child_run_outcome(store, child_id, 1)
    assert blind is not None
    assert blind.status == "failed"
    assert blind.failure_reason == "child_row_missing"
    # With the owner context, the real completed outcome comes through.
    seen = project_child_run_outcome(store, child_id, 1, visible_to=ctx)
    assert seen is not None
    assert seen.status == "completed"


def test_owned_run_handle_reads_elapsed_time_without_public_visibility() -> None:
    store = _store(max_concurrent=1, max_queue_size=1)
    observed: list[float] = []

    def work(handle: RunHandle) -> None:
        observed.append(handle.total_elapsed_seconds())
        handle.complete({"answer": "done"})

    summary = store.submit(
        question="owned timing",
        stack_name="default",
        work=work,
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    owner = _visible_to(OWNER_1)
    _wait_until(
        lambda: store.get(summary["run_id"], visible_to=owner)["status"] == "completed"
    )

    assert observed and observed[0] >= 0.0
    with pytest.raises(RunNotFound):
        store.get(summary["run_id"])


def test_delete_refuses_non_owner_and_cross_workspace() -> None:
    store = _store(max_queue_size=2)
    imported = store.import_completed_run(
        source_run_id="local_del_2",
        question="report",
        stack_name="default",
        result={"answer": "body"},
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )

    # Owner-only is stronger than cancel: a different creator gets the
    # indistinct RunNotFound and the run survives.
    with pytest.raises(RunNotFound):
        store.delete(
            imported["run_id"],
            workspace_id="ws_owner",
            requester_user_id=INTRUDER,
        )
    # The right owner from the wrong namespace is denied too.
    with pytest.raises(RunNotFound):
        store.delete(
            imported["run_id"],
            workspace_id="ws_other",
            requester_user_id=OWNER_1,
        )
    assert [
        s["run_id"]
        for s in store.list(workspace_id="ws_owner", visible_to=_visible_to(OWNER_1))
    ] == [imported["run_id"]]


def test_delete_allows_legacy_run_without_recorded_owner() -> None:
    store = _store(max_queue_size=2)
    imported = store.import_completed_run(
        source_run_id="local_legacy",
        question="legacy report",
        stack_name="default",
        result={"answer": "body"},
        workspace_id="ws_legacy",
        created_by_user_id=None,
        created_by_tenant_id="default",
    )

    # A pre-scoping run has no recorded creator; gating it on the workspace
    # alone keeps it deletable instead of orphaned forever, while a foreign
    # namespace is still denied.
    with pytest.raises(RunNotFound):
        store.delete(
            imported["run_id"],
            workspace_id="ws_other",
            requester_user_id=None,
        )
    store.delete(
        imported["run_id"],
        workspace_id="ws_legacy",
        requester_user_id=None,
    )
    assert store.list(workspace_id="ws_legacy") == []


def test_delete_refuses_a_still_active_run() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_work(handle: RunHandle) -> None:
        started.set()
        release.wait(timeout=1)
        handle.complete({"answer": "done"})

    store = _store(max_queue_size=2)
    summary = store.submit(
        question="running",
        stack_name="default",
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
        work=blocking_work,
    )
    _wait_until(started.is_set)

    # Deleting a record an executing worker still holds would let its final
    # write resurrect a half-gone run, so it is refused until terminal.
    with pytest.raises(RunActive):
        store.delete(
            summary["run_id"],
            workspace_id="ws_owner",
            requester_user_id=OWNER_1,
        )

    release.set()
    _wait_until(
        lambda: store.get(
            summary["run_id"],
            workspace_id="ws_owner",
            visible_to=_visible_to(OWNER_1),
        )["status"]
        == "completed"
    )
    store.delete(
        summary["run_id"],
        workspace_id="ws_owner",
        requester_user_id=OWNER_1,
    )
    assert store.list(workspace_id="ws_owner") == []


def test_import_completed_run_persists_and_is_idempotent() -> None:
    store = _store(max_queue_size=2)
    first = store.import_completed_run(
        source_run_id="local_report_1",
        question="imported report",
        stack_name="default",
        result={"answer": "the report body", "metrics": {}},
        created_at=1000.0,
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    assert first["run_id"] != "local_report_1"
    assert first["status"] == "completed"
    # Lists + scopes to the owner's workspace, and the body is fetchable.
    assert [
        s["run_id"]
        for s in store.list(workspace_id="ws_owner", visible_to=_visible_to(OWNER_1))
    ] == [first["run_id"]]
    assert (
        store.result(first["run_id"], visible_to=_visible_to(OWNER_1))["answer"]
        == "the report body"
    )
    # Re-importing the OWNER's own run is an idempotent no-op (one row).
    again = store.import_completed_run(
        source_run_id="local_report_1",
        question="imported report",
        stack_name="default",
        result={"answer": "ignored on re-import"},
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    assert again["run_id"] == first["run_id"]
    assert len(store.list(visible_to=_visible_to(OWNER_1))) == 1


def test_import_completed_run_scopes_source_id_to_owner() -> None:
    store = _store(max_queue_size=2)
    owner_a = store.import_completed_run(
        source_run_id="local_shared_id",
        question="A",
        stack_name="default",
        result={"answer": "owner A body"},
        created_by_user_id=OWNER_A,
        created_by_tenant_id="default",
    )
    # The same client-local id belongs to a separate idempotency scope for B.
    other = store.import_completed_run(
        source_run_id="local_shared_id",
        question="B",
        stack_name="default",
        result={"answer": "owner B body"},
        created_by_user_id=OWNER_B,
        created_by_tenant_id="default",
    )
    assert other["run_id"] != owner_a["run_id"]
    assert (
        store.result(owner_a["run_id"], visible_to=_visible_to(OWNER_A))["answer"]
        == "owner A body"
    )
    assert (
        store.result(other["run_id"], visible_to=_visible_to(OWNER_B))["answer"]
        == "owner B body"
    )


def test_import_after_retention_allocates_a_new_server_id() -> None:
    store = _store(completed_ttl_seconds=0)
    first = store.import_completed_run(
        source_run_id="local_retained_report",
        question="A",
        stack_name="default",
        result={"answer": "first"},
        created_by_user_id=OWNER_A,
        created_by_tenant_id="default",
    )
    store._records[first["run_id"]].finished_monotonic = time.monotonic() - 1

    second = store.import_completed_run(
        source_run_id="local_retained_report",
        question="A",
        stack_name="default",
        result={"answer": "second"},
        created_by_user_id=OWNER_A,
        created_by_tenant_id="default",
    )

    assert second["run_id"] != first["run_id"]
    with pytest.raises(RunNotFound):
        store.get(first["run_id"])


def test_import_completed_run_rejects_non_terminal_status() -> None:
    store = _store()
    with pytest.raises(ValueError):
        store.import_completed_run(
            source_run_id="x",
            question="q",
            stack_name="default",
            result={},
            status="running",
        )


def test_queue_full_rejects_extra_waiting_run() -> None:
    release = threading.Event()
    store = _store(max_concurrent=1, max_queue_size=1)
    store.submit(
        question="running",
        stack_name="default",
        work=lambda handle: release.wait(timeout=1),
    )
    store.submit(
        question="queued",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )

    with pytest.raises(RunQueueFull):
        store.submit(
            question="overflow",
            stack_name="default",
            work=lambda handle: handle.complete({}),
        )

    release.set()


def test_cancel_queued_run_marks_it_terminal() -> None:
    release = threading.Event()
    store = _store(max_concurrent=1, max_queue_size=1)
    store.submit(
        question="running",
        stack_name="default",
        work=lambda handle: release.wait(timeout=1),
    )
    queued = store.submit(
        question="queued",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "never"}),
    )

    cancelled = store.cancel(queued["run_id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["queue_position"] is None
    assert store._records[queued["run_id"]].status is RunStatus.CANCELLED
    release.set()


def test_cancel_running_run_sets_worker_cancel_event() -> None:
    observed_cancel = threading.Event()

    def cancellable_work(handle: RunHandle) -> None:
        if handle.cancel_event.wait(timeout=1):
            observed_cancel.set()
            handle.cancel("client_requested_cancel")

    store = _store()
    run = store.submit(question="q", stack_name="default", work=cancellable_work)
    _wait_until(lambda: store.get(run["run_id"])["status"] == "running")

    cancelled = store.cancel(run["run_id"])

    assert cancelled["status"] == "running"
    _wait_until(lambda: observed_cancel.is_set())
    _wait_until(lambda: store.get(run["run_id"])["status"] == "cancelled")


def test_cancel_pending_summary_exposes_cancel_requested() -> None:
    """A running run with a pending cancel carries ``cancel_requested``.

    Pins the additive summary contract the delete flow relies on: absent
    before the cancel and after the terminal transition (historical shape
    byte-identical), ``True`` exactly while the cancel is pending.
    """
    release = threading.Event()

    def cancellable_work(handle: RunHandle) -> None:
        if handle.cancel_event.wait(timeout=5):
            handle.cancel("client_requested_cancel")
        release.set()

    store = _store()
    run = store.submit(question="q", stack_name="default", work=cancellable_work)
    _wait_until(lambda: store.get(run["run_id"])["status"] == "running")
    assert "cancel_requested" not in store.get(run["run_id"])

    cancelled = store.cancel(run["run_id"])

    assert cancelled["status"] == "running"
    assert cancelled["cancel_requested"] is True
    _wait_until(lambda: store.get(run["run_id"])["status"] == "cancelled")
    assert "cancel_requested" not in store.get(run["run_id"])


def test_completed_result_and_ttl_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monotonic = 1000.0
    monkeypatch.setattr(runs_module.time, "monotonic", lambda: monotonic)
    store = _store(completed_ttl_seconds=5)
    run = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "ok", "metrics": {"rounds": 1}}),
    )
    _wait_until(lambda: store.get(run["run_id"])["status"] == "completed")

    result = store.result(run["run_id"])

    assert result["run_id"] == run["run_id"]
    assert result["answer"] == "ok"
    monotonic = 1006.0
    _wait_until(lambda: _raises_not_found(lambda: store.get(run["run_id"])))


def test_an_incognito_run_ages_out_after_its_own_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incognito ask is kept only while its answer is fetched: past the
    incognito window it is gone, while an ordinary run of the same age stays
    for the ordinary retention."""
    monotonic = 1000.0
    monkeypatch.setattr(runs_module.time, "monotonic", lambda: monotonic)
    store = RunStore.from_settings(
        ServerSettings(
            RUN_MAX_CONCURRENT=2,
            RUN_COMPLETED_TTL_SECONDS=3600,
            RUN_INCOGNITO_RETENTION_SECONDS=60,
        )
    )

    def done(handle: RunHandle) -> None:
        handle.complete({"answer": "ok", "metrics": {"rounds": 1}})

    incognito = store.submit(
        question="q",
        stack_name="default",
        work=done,
        mode="knowledge",
        request_payload={"body": {"mode": "knowledge", "incognito": True}},
    )
    ordinary = store.submit(
        question="q",
        stack_name="default",
        work=done,
        mode="knowledge",
        request_payload={"body": {"mode": "knowledge"}},
    )
    _wait_until(
        lambda: all(
            store.get(run["run_id"])["status"] == "completed"
            for run in (incognito, ordinary)
        )
    )

    monotonic = 1059.0
    assert store.get(incognito["run_id"])["status"] == "completed"
    monotonic = 1061.0
    _wait_until(lambda: _raises_not_found(lambda: store.get(incognito["run_id"])))
    assert store.get(ordinary["run_id"])["status"] == "completed"


def test_incognito_retention_defaults_to_fifteen_minutes_and_keeps_a_fetch_window() -> None:
    """Fifteen minutes by default and never below one minute, so a finished
    incognito answer cannot vanish before the browser has fetched it."""
    from pydantic import ValidationError

    assert ServerSettings().run_incognito_retention_seconds == 900
    with pytest.raises(ValidationError):
        ServerSettings(RUN_INCOGNITO_RETENTION_SECONDS=59)


def test_a_sweep_removes_an_expired_incognito_run_without_any_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incognito window holds while nobody reads the store: one sweep,
    which the API process runs on a clock, removes the expired ask, while an
    ordinary run of the same age and a still-running incognito ask stay."""
    monotonic = 1000.0
    monkeypatch.setattr(runs_module.time, "monotonic", lambda: monotonic)
    store = RunStore.from_settings(
        ServerSettings(
            RUN_MAX_CONCURRENT=3,
            RUN_COMPLETED_TTL_SECONDS=3600,
            RUN_INCOGNITO_RETENTION_SECONDS=60,
        )
    )
    release = threading.Event()

    def done(handle: RunHandle) -> None:
        handle.complete({"answer": "ok", "metrics": {"rounds": 1}})

    def held(handle: RunHandle) -> None:
        release.wait(timeout=5)
        done(handle)

    def ask(work: Callable[[RunHandle], None], *, incognito: bool) -> str:
        body = {"mode": "knowledge", **({"incognito": True} if incognito else {})}
        return store.submit(
            question="q",
            stack_name="default",
            work=work,
            mode="knowledge",
            request_payload={"body": body},
        )["run_id"]

    expired = ask(done, incognito=True)
    ordinary = ask(done, incognito=False)
    running = ask(held, incognito=True)
    try:
        _wait_until(
            lambda: store.get(expired)["status"] == "completed"
            and store.get(ordinary)["status"] == "completed"
            and store.get(running)["status"] == "running"
        )
        monotonic = 1061.0
        # Nobody has read the store since the window closed.
        assert expired in store._records

        store.sweep()

        assert expired not in store._records
        assert store._records[ordinary].status is RunStatus.COMPLETED
        assert store._records[running].status is RunStatus.RUNNING
    finally:
        release.set()


def test_subscribe_replays_buffered_events_and_receives_live_events() -> None:
    store = _store()
    started = threading.Event()
    release = threading.Event()

    def observable_work(handle: RunHandle) -> None:
        handle.emit("inqtrix.progress.message", {"message": "ready"})
        started.set()
        release.wait(timeout=2)
        handle.complete({})

    run = store.submit(question="q", stack_name="default", work=observable_work)
    _wait_until(lambda: started.is_set())

    subscription = store.subscribe(run["run_id"])
    try:
        event_types = [event["type"] for event in subscription.replay]
        assert "inqtrix.run.queued" in event_types
        assert "inqtrix.progress.message" in event_types

        # Live emit while the run still executes (a terminal run's log is
        # closed -- see test_emit_after_terminal_is_dropped_loudly).
        store.emit(run["run_id"], "inqtrix.debug", {"snapshot": {"round": 1}})
        snapshot_event: dict[str, Any] = subscription.queue.get(timeout=1)
        live_event: dict[str, Any] = subscription.queue.get(timeout=1)
        assert snapshot_event["type"] == "inqtrix.run.snapshot"
        assert snapshot_event["data"]["snapshot"]["round"] == 1
        assert live_event["type"] == "inqtrix.debug"
        assert live_event["data"]["snapshot"]["round"] == 1
        assert store.get(run["run_id"])["snapshot"]["round"] == 1
    finally:
        release.set()
        subscription.close()
    _wait_until(lambda: store.get(run["run_id"])["status"] == "completed")


def test_format_sse_event_uses_event_type_and_json_payload() -> None:
    frame = format_sse_event(
        {
            "type": "inqtrix.run.started",
            "run_id": "run_1",
            "sequence": 1,
            "data": {"status": "running"},
        }
    )

    assert frame.startswith("event: inqtrix.run.started\n")
    assert '"status": "running"' in frame
    assert frame.endswith("\n\n")


def _raises_not_found(action: Callable[[], object]) -> bool:
    try:
        action()
    except RunNotFound:
        return True
    return False


# ---------------------------------------------------------------------------
# agent run tree + waiting statuses
# ---------------------------------------------------------------------------


def test_standard_run_summary_keeps_historical_key_set() -> None:
    store = _store()

    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )

    for key in (
        "kind",
        "children_url",
        "plan_url",
        "artifacts_url",
        "parent_run_id",
        "root_run_id",
        "session_id",
    ):
        assert key not in summary
    _wait_until(lambda: store.get(summary["run_id"])["status"] == "completed")


def test_agent_run_summary_carries_tree_keys() -> None:
    store = _store(max_concurrent=2, max_queue_size=4)
    release_parent = threading.Event()

    parent = store.submit(
        question="auftrag",
        stack_name="default",
        work=lambda handle: (
            release_parent.wait(timeout=2),
            handle.complete({}),
        ),
        kind="agent",
        session_id="sess-1",
    )
    _wait_until(lambda: store.get(parent["run_id"])["status"] == "running")
    child = store.submit(
        question="teilaufgabe",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    release_parent.set()

    assert parent["kind"] == "agent"
    assert parent["children_url"] == f"/v1/runs/{parent['run_id']}/children"
    assert parent["plan_url"] == f"/v1/runs/{parent['run_id']}/plan"
    assert parent["artifacts_url"] == f"/v1/runs/{parent['run_id']}/artifacts"
    assert parent["session_id"] == "sess-1"
    assert child["kind"] == "agent_child"
    assert child["parent_run_id"] == parent["run_id"]
    assert child["root_run_id"] == parent["run_id"]
    _wait_until(lambda: store.get(child["run_id"])["status"] == "completed")
    _wait_until(lambda: store.get(parent["run_id"])["status"] == "completed")


def test_one_active_root_agent_run_per_session() -> None:
    store = _store(max_concurrent=2, max_queue_size=4)
    release = threading.Event()

    first = store.submit(
        question="first",
        stack_name="default",
        work=lambda handle: (
            release.wait(timeout=2),
            handle.complete({"answer": "done"}),
        ),
        kind="agent",
        session_id="sess-exclusive",
    )
    _wait_until(lambda: store.get(first["run_id"])["status"] == "running")

    with pytest.raises(RunSessionActive):
        store.submit(
            question="second",
            stack_name="default",
            work=lambda handle: handle.complete({}),
            kind="agent",
            session_id="sess-exclusive",
        )

    child = store.submit(
        question="child",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent_child",
        parent_run_id=first["run_id"],
        root_run_id=first["run_id"],
        session_id="sess-exclusive",
    )
    _wait_until(lambda: store.get(child["run_id"])["status"] == "completed")
    release.set()
    _wait_until(lambda: store.get(first["run_id"])["status"] == "completed")

    next_run = store.submit(
        question="next",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent",
        session_id="sess-exclusive",
    )
    _wait_until(lambda: store.get(next_run["run_id"])["status"] == "completed")


def test_waiting_lifecycle_parks_resumes_and_completes() -> None:
    store = _store(max_concurrent=1, max_queue_size=2)
    calls = {"count": 0}

    def segmented_work(handle: RunHandle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait(RunStatus.WAITING_FOR_APPROVAL)
            return
        handle.complete({"answer": "resumed"})

    summary = store.submit(
        question="agentlauf",
        stack_name="default",
        work=segmented_work,
        kind="agent",
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "waiting_for_approval")
    # The auto-complete safety net must NOT complete a parked run.
    time.sleep(0.05)
    waiting_summary = store.get(run_id)
    assert waiting_summary["status"] == "waiting_for_approval"
    first_started_at = waiting_summary["started_at"]
    assert waiting_summary["timing"]["segment_count"] == 1

    resumed = store.resume_run(run_id)

    # The in-memory store dispatches synchronously, so the returned
    # summary may already say running (same tolerance as submit).
    assert resumed["status"] in {"queued", "running"}
    _wait_until(lambda: store.get(run_id)["status"] == "completed")
    completed = store.get(run_id)
    assert calls["count"] == 2
    assert store.result(run_id)["answer"] == "resumed"
    assert completed["started_at"] == first_started_at
    assert completed["timing"]["segment_count"] == 2
    assert completed["timing"]["resume_count"] == 1
    assert completed["timing"]["waiting_seconds"] > 0

    subscription = store.subscribe(run_id)
    try:
        types = [event["type"] for event in subscription.replay]
        assert "inqtrix.run.waiting" in types
        queued_events = [
            event
            for event in subscription.replay
            if event["type"] == "inqtrix.run.queued"
        ]
        assert queued_events[-1]["data"].get("resumed") is True
        started_events = [
            event
            for event in subscription.replay
            if event["type"] == "inqtrix.run.started"
        ]
        resumed_events = [
            event
            for event in subscription.replay
            if event["type"] == "inqtrix.run.resumed"
        ]
        assert len(started_events) == 1
        assert len(resumed_events) == 1
        assert resumed_events[0]["data"]["segment_ordinal"] == 2
        assert (
            resumed_events[0]["data"]["segment_id"]
            != started_events[0]["data"]["segment_id"]
        )
    finally:
        subscription.close()


def test_mark_waiting_rejects_non_running_and_bad_status() -> None:
    store = _store()
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "completed")

    with pytest.raises(ValueError, match="not a waiting status"):
        store.mark_waiting(run_id, status=RunStatus.RUNNING)
    with pytest.raises(RunActive):
        store.mark_waiting(run_id, status=RunStatus.WAITING_FOR_INPUT)
    with pytest.raises(RunNotFound):
        store.mark_waiting("run_unbekannt", status=RunStatus.WAITING_FOR_INPUT)


def test_worker_write_methods_accept_fence_attempt_for_port_parity() -> None:
    """The worker (FencedRunHandle) calls ALL FIVE store write methods
    through RunStorePort with ``fence_attempt`` — emit/complete/fail/
    mark_cancelled/mark_waiting. The in-memory store must accept it on every
    one (ignored — no claim/reclaim in-process), NOT raise TypeError on an
    unexpected kwarg, which is exactly what a parity regression would do.

    On a terminal run the first four are absorbing no-ops; mark_waiting hits
    the RUNNING guard (RunActive). Either way the kwarg must be accepted and
    control flow must reach the method body.
    """
    store = _store()
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "completed")

    # Absorbing on a terminal run — accepted and no-op, never TypeError.
    store.emit(run_id, "inqtrix.test", {}, fence_attempt=7)
    store.complete(run_id, {}, fence_attempt=7)
    store.fail(run_id, "boom", fence_attempt=7)
    store.mark_cancelled(run_id, reason="x", fence_attempt=7)

    # Accepted and reaches the RUNNING guard (RunActive), not a TypeError.
    with pytest.raises(RunActive):
        store.mark_waiting(run_id, status=RunStatus.WAITING_FOR_INPUT, fence_attempt=7)


def test_execution_request_body_is_immutable_after_submission() -> None:
    """Control validation reads the admitted request, never caller aliases."""
    store = _store()
    request_payload = {
        "body": {"knowledge_filters": {"collection_ids": ["kc_initial"]}}
    }
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        request_payload=request_payload,
    )

    request_payload["body"]["knowledge_filters"]["collection_ids"].append("kc_late")
    first_read = store.execution_request_body(summary["run_id"])
    assert first_read["knowledge_filters"]["collection_ids"] == ["kc_initial"]

    first_read["knowledge_filters"]["collection_ids"].append("kc_other")
    second_read = store.execution_request_body(summary["run_id"])
    assert second_read["knowledge_filters"]["collection_ids"] == ["kc_initial"]


def test_resume_requires_a_waiting_run() -> None:
    store = _store()
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )
    _wait_until(lambda: store.get(summary["run_id"])["status"] == "completed")

    with pytest.raises(RunActive):
        store.resume_run(summary["run_id"])
    with pytest.raises(RunNotFound):
        store.resume_run("run_unbekannt")


def test_cancel_while_waiting_is_immediate_with_visible_reason() -> None:
    store = _store(max_concurrent=1, max_queue_size=2)

    def parking_work(handle: RunHandle) -> None:
        handle.wait("waiting_for_input")

    summary = store.submit(
        question="agentlauf", stack_name="default", work=parking_work
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "waiting_for_input")

    cancelled = store.cancel(run_id)

    assert cancelled["status"] == "cancelled"
    subscription = store.subscribe(run_id)
    try:
        last = subscription.replay[-1]
        assert last["type"] == "inqtrix.run.cancelled"
        assert last["data"]["reason"] == "cancelled_while_waiting"
    finally:
        subscription.close()
    with pytest.raises(RunActive):
        store.resume_run(run_id)


def test_cancel_cascades_over_the_child_tree() -> None:
    store = _store(max_concurrent=1, max_queue_size=4)
    parent_started = threading.Event()

    def parent_work(handle: RunHandle) -> None:
        parent_started.set()
        handle.cancel_event.wait(timeout=2)
        handle.cancel("client_requested_cancel")

    parent = store.submit(
        question="auftrag",
        stack_name="default",
        work=parent_work,
        kind="agent",
    )
    _wait_until(lambda: parent_started.is_set())
    child = store.submit(
        question="teilaufgabe",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    assert store.get(child["run_id"])["status"] == "queued"

    store.cancel(parent["run_id"])

    assert store.get(child["run_id"])["status"] == "cancelled"
    _wait_until(lambda: store.get(parent["run_id"])["status"] == "cancelled")


def test_children_lists_direct_children_newest_first() -> None:
    store = _store(max_concurrent=2, max_queue_size=6)
    release_parent = threading.Event()

    def parent_work(handle: RunHandle) -> None:
        release_parent.wait(timeout=2)
        handle.complete({})

    parent = store.submit(
        question="auftrag",
        stack_name="default",
        work=parent_work,
        kind="agent",
    )
    first = store.submit(
        question="erste",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent_child",
        parent_run_id=parent["run_id"],
    )
    time.sleep(0.02)
    second = store.submit(
        question="zweite",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent_child",
        parent_run_id=parent["run_id"],
    )
    unrelated = store.submit(
        question="fremd",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )
    for summary in (first, second, unrelated):
        _wait_until(
            lambda run_id=summary["run_id"]: store.get(run_id)["status"] == "completed"
        )

    children = store.children(parent["run_id"])

    assert [child["run_id"] for child in children] == [
        second["run_id"],
        first["run_id"],
    ]
    release_parent.set()
    _wait_until(lambda: store.get(parent["run_id"])["status"] == "completed")


def test_nested_children_use_canonical_root_and_cancel_recursively() -> None:
    """Caller lineage cannot split a tree; root cancel reaches every level."""
    store = _store(max_concurrent=3, max_queue_size=6)
    release = threading.Event()

    def blocking_work(handle: RunHandle) -> None:
        release.wait(timeout=2)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({})

    root = store.submit(
        question="root",
        stack_name="default",
        work=blocking_work,
        kind="agent",
    )
    child = store.submit(
        question="child",
        stack_name="default",
        work=blocking_work,
        kind="agent_child",
        parent_run_id=root["run_id"],
        root_run_id="caller-controlled-wrong-root",
    )
    grandchild = store.submit(
        question="grandchild",
        stack_name="default",
        work=blocking_work,
        kind="agent_child",
        parent_run_id=child["run_id"],
        root_run_id=child["run_id"],
    )

    assert child["root_run_id"] == root["run_id"]
    assert grandchild["root_run_id"] == root["run_id"]
    _summary, affected = store.cancel_tree(root["run_id"])
    assert set(affected) == {
        root["run_id"],
        child["run_id"],
        grandchild["run_id"],
    }
    release.set()
    for run in (root, child, grandchild):
        _wait_until(
            lambda run_id=run["run_id"]: store.get(run_id)["status"] == "cancelled"
        )


def test_origin_key_submit_or_find_is_atomic_in_memory() -> None:
    store = _store(max_concurrent=1, max_queue_size=6)
    release = threading.Event()

    def root_work(handle: RunHandle) -> None:
        release.wait(timeout=2)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({})

    root = store.submit(
        question="root",
        stack_name="default",
        work=root_work,
        kind="agent",
    )
    barrier = threading.Barrier(3)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def submit_same_origin() -> None:
        barrier.wait()
        try:
            results.append(
                store.submit(
                    question="same child",
                    stack_name="default",
                    work=lambda handle: handle.complete({}),
                    kind="agent_child",
                    parent_run_id=root["run_id"],
                    origin_key="task-1:attempt-1",
                )
            )
        except BaseException as exc:  # noqa: BLE001 - thread handoff
            errors.append(exc)

    threads = [threading.Thread(target=submit_same_origin) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert len(results) == 2
    assert results[0]["run_id"] == results[1]["run_id"]
    assert len(store.children(root["run_id"])) == 1
    store.cancel(root["run_id"])
    release.set()
    _wait_until(lambda: store.get(root["run_id"])["status"] == "cancelled")


def test_child_submit_racing_root_cancel_cannot_leave_active_descendant() -> None:
    store = _store(max_concurrent=1, max_queue_size=6)
    release = threading.Event()

    def root_work(handle: RunHandle) -> None:
        release.wait(timeout=2)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({})

    root = store.submit(
        question="root",
        stack_name="default",
        work=root_work,
        kind="agent",
    )
    barrier = threading.Barrier(3)
    submitted: list[dict[str, Any]] = []
    rejected: list[RunParentInactive] = []
    errors: list[BaseException] = []

    def submit_child() -> None:
        barrier.wait()
        try:
            submitted.append(
                store.submit(
                    question="racing child",
                    stack_name="default",
                    work=lambda handle: handle.complete({}),
                    kind="agent_child",
                    parent_run_id=root["run_id"],
                    origin_key="racing-origin",
                )
            )
        except RunParentInactive as exc:
            rejected.append(exc)
        except BaseException as exc:  # noqa: BLE001 - thread handoff
            errors.append(exc)

    def cancel_root() -> None:
        barrier.wait()
        try:
            store.cancel(root["run_id"])
        except BaseException as exc:  # noqa: BLE001 - thread handoff
            errors.append(exc)

    submit_thread = threading.Thread(target=submit_child)
    cancel_thread = threading.Thread(target=cancel_root)
    submit_thread.start()
    cancel_thread.start()
    barrier.wait()
    submit_thread.join(timeout=2)
    cancel_thread.join(timeout=2)

    assert not errors
    assert len(submitted) + len(rejected) == 1
    if submitted:
        assert store.get(submitted[0]["run_id"])["status"] == "cancelled"
    else:
        assert store.children(root["run_id"]) == []
    release.set()
    _wait_until(lambda: store.get(root["run_id"])["status"] == "cancelled")


def test_waiting_ttl_auto_cancels_with_approval_timeout() -> None:
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=10,
        waiting_ttl_seconds=0.05,
    )

    summary = store.submit(
        question="agentlauf",
        stack_name="default",
        work=lambda handle: handle.wait("waiting_for_approval"),
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "waiting_for_approval")
    time.sleep(0.1)

    # Any store touch runs the sweep.
    assert store.get(run_id)["status"] == "cancelled"
    subscription = store.subscribe(run_id)
    try:
        last = subscription.replay[-1]
        assert last["type"] == "inqtrix.run.cancelled"
        assert last["data"]["reason"] == "approval_timeout"
    finally:
        subscription.close()


def test_replay_after_filters_replay_only_by_sequence() -> None:
    from inqtrix.runs.shared import replay_after

    replay = [
        {"type": "a", "sequence": 1},
        {"type": "b", "sequence": 2},
        {"type": "c", "sequence": 3},
    ]

    assert replay_after(replay, None) == replay
    assert replay_after(replay, 1) == replay[1:]
    assert replay_after(replay, 3) == []
    assert replay_after(replay, 99) == []
    # Events without a sequence must never be dropped silently.
    unmarked = [{"type": "x"}]
    assert replay_after(unmarked, 5) == unmarked


def test_resume_during_park_unwind_defers_to_the_worker() -> None:
    """A resume racing the parking worker's unwind must not double-run.

    The closure parks, then keeps running briefly (simulated post-wait
    work). resume_run lands INSIDE that window: the dispatch must be
    deferred to the worker's unwind, the run must complete exactly once
    with exactly two closure invocations, never two concurrent ones.
    """
    store = _store(max_concurrent=2, max_queue_size=2)
    calls = {"count": 0}
    parked = threading.Event()
    release_unwind = threading.Event()

    def segmented_work(handle: RunHandle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait(RunStatus.WAITING_FOR_APPROVAL)
            parked.set()
            # Post-wait work: the worker has NOT unwound yet.
            release_unwind.wait(timeout=2)
            return
        handle.complete({"answer": "resumed"})

    summary = store.submit(
        question="agentlauf", stack_name="default", work=segmented_work
    )
    run_id = summary["run_id"]
    _wait_until(lambda: parked.is_set())

    resumed = store.resume_run(run_id)
    assert resumed["status"] == "queued"
    # Deferred: nothing dispatched while the first worker is alive.
    time.sleep(0.05)
    assert calls["count"] == 1
    assert store.get(run_id)["status"] == "queued"

    release_unwind.set()
    _wait_until(lambda: store.get(run_id)["status"] == "completed")
    assert calls["count"] == 2


def test_wait_with_pending_cancel_cancels_instead_of_parking() -> None:
    store = _store(max_concurrent=1, max_queue_size=2)
    running = threading.Event()
    proceed = threading.Event()

    def cancelled_then_waits(handle: RunHandle) -> None:
        running.set()
        proceed.wait(timeout=2)
        handle.wait("waiting_for_approval")

    summary = store.submit(
        question="agentlauf", stack_name="default", work=cancelled_then_waits
    )
    run_id = summary["run_id"]
    _wait_until(lambda: running.is_set())
    store.cancel(run_id)
    proceed.set()

    _wait_until(lambda: store.get(run_id)["status"] == "cancelled")
    subscription = store.subscribe(run_id)
    try:
        last = subscription.replay[-1]
        assert last["type"] == "inqtrix.run.cancelled"
        assert last["data"]["reason"] == "cancelled_while_waiting"
    finally:
        subscription.close()
    with pytest.raises(RunActive):
        store.resume_run(run_id)


def test_emit_after_terminal_is_dropped_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The event log ends with the terminal event — a post-terminal
    signal (e.g. an artifact edit after completion) must not reopen it,
    or SSE replays would never terminate."""
    store = _store()
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: handle.complete({}),
    )
    run_id = summary["run_id"]
    _wait_until(lambda: store.get(run_id)["status"] == "completed")

    with caplog.at_level("WARNING", logger="inqtrix"):
        store.emit(run_id, "inqtrix.agent.artifact.updated", {"revision": 2})

    assert any("discarded" in record.message for record in caplog.records)
    subscription = store.subscribe(run_id)
    try:
        assert subscription.replay[-1]["type"] == "inqtrix.run.completed"
    finally:
        subscription.close()


def test_list_page_walks_the_full_history_without_gaps() -> None:
    """2.2: keyset paging returns every run exactly once, newest first."""
    store = _store(max_concurrent=5, max_queue_size=20)
    ids = []
    for i in range(7):
        summary = store.submit(
            question=f"q{i}",
            stack_name="default",
            work=lambda handle: handle.complete({}),
        )
        ids.append(summary["run_id"])
    _wait_until(lambda: all(store.get(rid)["status"] == "completed" for rid in ids))

    seen: list[str] = []
    after = None
    pages = 0
    while True:
        summaries, cursor = store.list_page(limit=3, after=after)
        pages += 1
        seen.extend(s["run_id"] for s in summaries)
        if cursor is None:
            break
        from inqtrix.pagination import decode_cursor

        after = decode_cursor(cursor)
        assert pages < 10  # guard against a non-terminating cursor
    # Every run once, newest-first (submission order reversed).
    assert seen == list(reversed(ids))
    assert len(seen) == len(set(seen))
    # Last page carries no cursor.
    assert store.list_page(limit=100)[1] is None


def test_list_page_matches_unbounded_list_ordering() -> None:
    store = _store(max_concurrent=5, max_queue_size=20)
    for i in range(4):
        store.submit(
            question=f"q{i}",
            stack_name="default",
            work=lambda handle: handle.complete({}),
        )
    unbounded = [s["run_id"] for s in store.list()]
    paged, cursor = store.list_page(limit=100)
    assert [s["run_id"] for s in paged] == unbounded
    assert cursor is None


def test_child_projection_is_bounded_and_preserves_parent_snapshot() -> None:
    store = _store(
        max_concurrent=1,
        max_queue_size=4,
        event_buffer_size=64,
    )
    release_parent = threading.Event()

    def parent_work(handle: RunHandle) -> None:
        handle.emit(
            "inqtrix.agent.phase.changed",
            {
                "phase": "execution",
                "snapshot": {
                    "current_node": "agent_execute",
                    "phase": "execution",
                },
            },
        )
        release_parent.wait(timeout=2)
        handle.complete({"answer": "done"})

    parent = store.submit(
        question="parent",
        stack_name="default",
        work=parent_work,
        kind="agent",
        session_id="session-projection",
    )
    _wait_until(
        lambda: store.get(parent["run_id"])["snapshot"].get("current_node")
        == "agent_execute"
    )
    child = store.submit(
        question="child",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "child"}),
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
        session_id="session-projection",
        request_payload={
            "question": "child",
            "body": {
                "mode": "research",
                "parent_task_id": "task-1",
                "parent_task_attempt": 2,
            },
        },
    )

    def projected_events() -> list[dict[str, Any]]:
        subscription = store.subscribe(parent["run_id"])
        try:
            return [
                event
                for event in subscription.replay
                if event["type"] == "inqtrix.agent.child.progress"
            ]
        finally:
            subscription.close()

    queued_count = len(projected_events())
    assert queued_count == 1
    store.emit(
        child["run_id"],
        "inqtrix.output_text.delta",
        {"delta": "provider output"},
    )
    assert len(projected_events()) == queued_count
    store.emit(
        child["run_id"],
        "inqtrix.node.started",
        {"node": "search", "snapshot": {"current_node": "search"}},
    )
    projected = projected_events()
    assert len(projected) == queued_count + 1
    assert projected[-1]["data"]["task_id"] == "task-1"
    assert projected[-1]["data"]["attempt"] == 2
    assert projected[-1]["data"]["snapshot"]["current_node"] == "search"
    assert store.get(parent["run_id"])["snapshot"]["current_node"] == ("agent_execute")

    store.emit(
        child["run_id"],
        "inqtrix.knowledge.grounding.checked",
        {
            "status": "rejected_quote",
            "failure_code": "knowledge_grounding_quote_unverified",
            "marker": "_knowledge_grounding_parsed",
            "format_repaired": False,
            "quotes_total": 2,
            "quotes_verified": 1,
            # Outside the registered event schema: it must not cross either
            # the child audit sanitizer or the bounded parent projection.
            "quote_text": "private source passage",
        },
    )
    projected = projected_events()
    grounding = projected[-1]["data"]
    assert grounding["event_type"] == "inqtrix.knowledge.grounding.checked"
    assert grounding["status"] == "rejected_quote"
    assert grounding["failure_code"] == (
        "knowledge_grounding_quote_unverified"
    )
    assert grounding["metrics"] == {
        "quotes_total": 2,
        "quotes_verified": 1,
    }
    assert "quote_text" not in grounding

    store.emit(
        child["run_id"],
        "inqtrix.knowledge.answer.retry",
        {
            "attempt": 2,
            "quotes_total": 2,
            "quotes_unverified": 1,
            # Outside the registered schema: must not cross the boundary.
            "quote_text": "private source passage",
        },
    )
    projected = projected_events()
    retry = projected[-1]["data"]
    assert retry["event_type"] == "inqtrix.knowledge.answer.retry"
    # The regeneration attempt travels under its own metrics key so the
    # task-attempt field cannot clobber it.
    assert retry["metrics"] == {
        "quotes_total": 2,
        "quotes_unverified": 1,
        "answer_attempt": 2,
    }
    assert "quote_text" not in retry

    store.emit(
        child["run_id"],
        "inqtrix.knowledge.retrieval.degraded",
        {
            "reason": "vector_overfetch_cap",
            "retrieval_mode": "hybrid",
            "stage": "vector_candidate_pool",
            "requested_candidate_pool": 40,
            "returned_candidate_pool": 12,
            "final_top_k": 8,
            "final_evidence_complete": False,
            "requested_top_k": 8,
            "returned_hits": 3,
            "candidate_cap": 64,
            "query": "private user query",
            "source_ids": ["private-source"],
        },
    )
    projected = projected_events()
    retrieval = projected[-1]["data"]
    assert retrieval["event_type"] == "inqtrix.knowledge.retrieval.degraded"
    assert retrieval["reason"] == "vector_overfetch_cap"
    assert retrieval["retrieval_mode"] == "hybrid"
    assert retrieval["stage"] == "vector_candidate_pool"
    assert retrieval["requested_candidate_pool"] == 40
    assert retrieval["returned_candidate_pool"] == 12
    assert retrieval["final_top_k"] == 8
    assert retrieval["final_evidence_complete"] is False
    assert retrieval["requested_top_k"] == 8
    assert retrieval["returned_hits"] == 3
    assert retrieval["candidate_cap"] == 64
    assert "query" not in retrieval
    assert "source_ids" not in retrieval

    store.emit(
        child["run_id"],
        "inqtrix.knowledge.retrieval.warning",
        {
            "code": "chunks_require_reindex",
            "message": "Treffer müssen neu indiziert werden.",
            "reason": "source_unverified",
            "stage": "canonical_hydration",
            "count": 2,
            "recommended_action": "reindex",
            "query": "private user query",
            "source_ids": ["private-source"],
            "excerpt": "private source passage",
        },
    )
    projected = projected_events()
    warning = projected[-1]["data"]
    assert warning["event_type"] == "inqtrix.knowledge.retrieval.warning"
    assert warning["code"] == "chunks_require_reindex"
    assert warning["reason"] == "source_unverified"
    assert warning["stage"] == "canonical_hydration"
    assert warning["count"] == 2
    assert warning["recommended_action"] == "reindex"
    assert "message" not in warning
    assert "query" not in warning
    assert "source_ids" not in warning
    assert "excerpt" not in warning

    release_parent.set()
    _wait_until(lambda: store.get(parent["run_id"])["status"] == "completed")
