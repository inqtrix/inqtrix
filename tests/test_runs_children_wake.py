"""Children park-and-resume on the in-memory run store.

The contract: an agent parent that spawns child runs PARKS
(``waiting_for_children``, slot-free) instead of block-polling siblings
out of the shared execution pool. The store itself wakes the parent when
the LAST child terminates; the park-time self-heal closes the
child-finished-before-the-park race. These tests drive the store
directly with segment-aware work closures — exactly the shape the
checkpointed agent algorithm hands it.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.server.runs import RunStatus, RunStore


USER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _visible_to(user_id: uuid.UUID) -> UserContext:
    return UserContext(
        principal=Principal(user_id=user_id, kind="oidc_session")
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


def _store(**kwargs: Any) -> RunStore:
    kwargs.setdefault("max_concurrent", 1)
    kwargs.setdefault("max_queue_size", 10)
    kwargs.setdefault("completed_ttl_seconds", 30)
    kwargs.setdefault("event_buffer_size", 50)
    return RunStore(**kwargs)


def _submit_child(
    store: RunStore,
    parent_id: str,
    work,
    *,
    created_by_user_id: uuid.UUID | None = None,
) -> str:
    summary = store.submit(
        question="Kind-Recherche",
        stack_name="default",
        work=work,
        kind="agent_child",
        parent_run_id=parent_id,
        root_run_id=parent_id,
        created_by_user_id=created_by_user_id,
    )
    return summary["run_id"]


class _SegmentedParent:
    """Two-segment parent work: submit children + park, then complete.

    Mirrors the checkpointed algorithm: the first dispatch submits the
    children and parks ``waiting_for_children``; the re-dispatch (after
    the store woke the run) completes.
    """

    def __init__(
        self,
        store: RunStore,
        child_works: list[Callable[[Any], None]],
        *,
        park_gate: threading.Event | None = None,
        child_sub: uuid.UUID | None = None,
    ) -> None:
        self.store = store
        self.child_works = child_works
        self.park_gate = park_gate
        # Real agent children inherit the parent's verified subject; the
        # per-user cap test relies on that inheritance.
        self.child_sub = child_sub
        self.child_ids: list[str] = []
        self.segments = 0
        self.run_id = ""

    def __call__(self, handle) -> None:
        # The run id comes from the HANDLE: submit() dispatches before
        # its summary returns to the test thread, so a test-side
        # assignment would race the first segment.
        self.run_id = handle.run_id
        self.segments += 1
        if self.segments == 1:
            for work in self.child_works:
                self.child_ids.append(
                    _submit_child(
                        self.store,
                        self.run_id,
                        work,
                        created_by_user_id=self.child_sub,
                    )
                )
            if self.park_gate is not None:
                # Test-controlled ordering: hold the park until the
                # scenario says so (e.g. after a child went terminal).
                assert self.park_gate.wait(timeout=5.0)
            handle.wait(RunStatus.WAITING_FOR_CHILDREN)
            return
        handle.complete({"answer": "fertig", "metrics": {}})


def test_children_run_through_a_single_slot_without_deadlock() -> None:
    """Pool of 1, parent + child, no deadlock.

    A parent that block-polls its child while holding the only execution
    slot keeps the child from ever starting and wedges the pool until
    the child deadline. Parking frees the slot: the single slot serves
    parent segment 1, then the child, then the woken parent's segment 2.
    """
    store = _store(max_concurrent=1)

    def child_work(handle) -> None:
        handle.complete({"answer": "kind fertig", "metrics": {}})

    parent = _SegmentedParent(store, [child_work])
    summary = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        kind="agent",
    )
    parent.run_id = summary["run_id"]
    # Re-anchor: the closure needed its own run id before dispatch; the
    # store dispatches asynchronously, so setting it here races only
    # against the submit above, which has not been dispatched yet with
    # max_concurrent=1 and an empty pool... it may already run, so wait.
    _wait_until(
        lambda: store.get(parent.run_id)["status"] == "completed",
        timeout=5.0,
    )
    assert parent.segments == 2
    child_summary = store.get(parent.child_ids[0])
    assert child_summary["status"] == "completed"


def test_parent_wakes_only_after_the_last_child() -> None:
    store = _store(max_concurrent=3)
    release_first = threading.Event()
    release_second = threading.Event()

    def child_one(handle) -> None:
        assert release_first.wait(timeout=5.0)
        handle.complete({"answer": "eins", "metrics": {}})

    def child_two(handle) -> None:
        assert release_second.wait(timeout=5.0)
        handle.complete({"answer": "zwei", "metrics": {}})

    parent = _SegmentedParent(store, [child_one, child_two])
    parent.run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        kind="agent",
    )["run_id"]
    _wait_until(
        lambda: store.get(parent.run_id)["status"]
        == "waiting_for_children"
    )
    release_first.set()
    _wait_until(
        lambda: store.get(parent.child_ids[0])["status"] == "completed"
    )
    # One child done, one outstanding: the parent must still wait.
    assert store.get(parent.run_id)["status"] == "waiting_for_children"
    release_second.set()
    _wait_until(
        lambda: store.get(parent.run_id)["status"] == "completed"
    )
    assert parent.segments == 2


def test_park_self_heals_when_children_finished_first() -> None:
    """Lost-wakeup race: the last child terminates BEFORE the park."""
    store = _store(max_concurrent=2)
    park_gate = threading.Event()

    def child_work(handle) -> None:
        handle.complete({"answer": "kind fertig", "metrics": {}})

    parent = _SegmentedParent(store, [child_work], park_gate=park_gate)
    parent.run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        kind="agent",
    )["run_id"]
    _wait_until(lambda: len(parent.child_ids) == 1)
    _wait_until(
        lambda: store.get(parent.child_ids[0])["status"] == "completed"
    )
    # The child is terminal; its wake probe found the parent RUNNING
    # and no-oped. Only NOW does the parent park — the park-time
    # self-heal must flip it straight back instead of hanging.
    park_gate.set()
    _wait_until(
        lambda: store.get(parent.run_id)["status"] == "completed"
    )
    assert parent.segments == 2


def test_cancel_of_parked_parent_cascades_and_never_resumes() -> None:
    store = _store(max_concurrent=2)
    hold_child = threading.Event()

    def child_work(handle) -> None:
        # Cancelled before this fires; guard against a hung test.
        hold_child.wait(timeout=5.0)
        if handle.cancel_event.is_set():
            handle.cancel("cancelled")
            return
        handle.complete({"answer": "kind", "metrics": {}})

    parent = _SegmentedParent(store, [child_work])
    parent.run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        kind="agent",
    )["run_id"]
    _wait_until(
        lambda: store.get(parent.run_id)["status"]
        == "waiting_for_children"
    )
    store.cancel(parent.run_id)
    hold_child.set()
    _wait_until(
        lambda: store.get(parent.run_id)["status"] == "cancelled"
    )
    _wait_until(
        lambda: store.get(parent.child_ids[0])["status"] == "cancelled"
    )
    # The cascaded child terminal writes must not resurrect the parent.
    time.sleep(0.1)
    assert store.get(parent.run_id)["status"] == "cancelled"
    assert parent.segments == 1


def test_children_wait_ttl_cancels_with_children_timeout_reason() -> None:
    store = _store(max_concurrent=2, waiting_ttl_seconds=0.2)
    hold_child = threading.Event()

    def child_work(handle) -> None:
        hold_child.wait(timeout=10.0)
        handle.cancel("cancelled")

    parent = _SegmentedParent(store, [child_work])
    parent.run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        kind="agent",
    )["run_id"]
    _wait_until(
        lambda: store.get(parent.run_id)["status"]
        == "waiting_for_children"
    )
    # The TTL sweep runs lazily on store touches; poll until it fires.
    _wait_until(
        lambda: store.get(parent.run_id)["status"] == "cancelled",
        timeout=5.0,
    )
    subscription = store.subscribe(parent.run_id)
    reasons = [
        event["data"].get("reason")
        for event in subscription.replay
        if event["type"] == "inqtrix.run.cancelled"
    ]
    assert "children_timeout" in reasons
    hold_child.set()


def test_per_user_cap_bounds_only_the_noisy_subject() -> None:
    """1.3: the fairness cap is per-subject; others keep their share.

    Waiting (parked) runs hold no slot and must not eat the budget.
    """
    from inqtrix.server.runs import RunPerUserLimit

    store = _store(max_concurrent=1, max_concurrent_per_user=2)
    hold = threading.Event()

    def slow_work(handle) -> None:
        hold.wait(timeout=10.0)
        handle.complete({"answer": "ok", "metrics": {}})

    def submit(user_id: uuid.UUID):
        return store.submit(
            question="F",
            stack_name="default",
            work=slow_work,
            created_by_user_id=user_id,
        )

    try:
        submit(USER_A)
        submit(USER_A)
        with pytest.raises(RunPerUserLimit):
            submit(USER_A)
        # Another subject is not starved by user-a's burst.
        submit(USER_B)
        # Anonymous submissions are never capped.
        store.submit(
            question="F", stack_name="default", work=slow_work
        )
    finally:
        hold.set()


def test_per_user_cap_ignores_parked_runs() -> None:
    """A parked agent parent must not eat its owner's fairness budget.

    Cap 2, sub user-a. The parent is submitted (RUNNING=1) and submits
    its child (RUNNING=2 briefly), then parks. After the park user-a's
    counted in-flight is just the RUNNING child (=1) — the WAITING
    parent is excluded. Proof: one more user-a run is admitted (1<2);
    if the parked parent still counted it would be 2>=2 and rejected.
    A third run then trips the cap, and finishing the tree frees it.
    """
    from inqtrix.server.runs import RunPerUserLimit

    store = _store(max_concurrent=3, max_concurrent_per_user=2)
    hold_child = threading.Event()
    hold_filler = threading.Event()

    def child_work(handle) -> None:
        hold_child.wait(timeout=10.0)
        handle.complete({"answer": "kind", "metrics": {}})

    def filler_work(handle) -> None:
        hold_filler.wait(timeout=10.0)
        handle.complete({"answer": "x", "metrics": {}})

    def submit_filler():
        return store.submit(
            question="F",
            stack_name="default",
            work=filler_work,
            created_by_user_id=USER_A,
            created_by_tenant_id="default",
        )

    parent = _SegmentedParent(store, [child_work], child_sub=USER_A)
    try:
        store.submit(
            question="Agent",
            stack_name="default",
            work=parent,
            kind="agent",
            created_by_user_id=USER_A,
            created_by_tenant_id="default",
        )
        _wait_until(
            lambda: store.get(
                parent.run_id, visible_to=_visible_to(USER_A)
            )["status"]
            == "waiting_for_children"
        )
        # Parked parent excluded: counted in-flight is the child only,
        # so this run is admitted (would be rejected if parked counted).
        submit_filler()
        # Now child + filler = 2 = cap: the next one trips it.
        with pytest.raises(RunPerUserLimit):
            submit_filler()
        # Release everything: the tree completes and the budget frees.
        hold_child.set()
        hold_filler.set()
        _wait_until(
            lambda: store.get(
                parent.run_id, visible_to=_visible_to(USER_A)
            )["status"] == "completed"
        )
        store.submit(
            question="F",
            stack_name="default",
            work=lambda handle: handle.complete(
                {"answer": "x", "metrics": {}}
            ),
            created_by_user_id=USER_A,
            created_by_tenant_id="default",
        )
    finally:
        hold_child.set()
        hold_filler.set()


def test_per_user_cap_excludes_agent_parents() -> None:
    """A1xA1.3 review fix: an agent PARENT must not count against the cap.

    The parent is RUNNING while it submits its children; counting it
    would make it contend against its own children for the user's budget
    (self-starvation). Only slot-occupying standard runs + agent children
    count, so a RUNNING agent parent leaves the full cap free.
    """
    from inqtrix.server.runs import RunPerUserLimit

    store = _store(max_concurrent=4, max_concurrent_per_user=2)
    hold = threading.Event()

    def blocking(handle) -> None:
        hold.wait(timeout=10.0)
        handle.complete({"answer": "ok", "metrics": {}})

    try:
        # A RUNNING agent parent (kind='agent') owned by user-a.
        store.submit(
            question="Agent",
            stack_name="default",
            work=blocking,
            kind="agent",
            created_by_user_id=USER_A,
        )
        # The parent does NOT consume user-a's budget: both standard runs
        # are admitted despite the parent running.
        store.submit(
            question="F", stack_name="default", work=blocking,
            created_by_user_id=USER_A,
        )
        store.submit(
            question="F", stack_name="default", work=blocking,
            created_by_user_id=USER_A,
        )
        # Now 2 standard runs = cap: the third trips it (parent still free).
        with pytest.raises(RunPerUserLimit):
            store.submit(
                question="F", stack_name="default", work=blocking,
                created_by_user_id=USER_A,
            )
    finally:
        hold.set()
