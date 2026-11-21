"""Which runs take their checkpoint thread with them when they are deleted.

Every mode whose runs are or were agent runs keeps its threads deletable,
registered or retired: a retired mode's runs still exist, and deleting one
must not leave its thread behind. A run that was never claimed has no thread,
so its deletion never needs the checkpoint store.
"""

from __future__ import annotations

import time

import pytest

from inqtrix.agents.checkpointing import cleanup_run_checkpoint
from inqtrix.core.constants import AGENT_MODE_IDS, AGENT_RUN_MODE_IDS
from inqtrix.exceptions import RunCleanupFailed


class _Threads:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread_strict(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def test_a_retired_agent_mode_still_deletes_its_thread() -> None:
    threads = _Threads()

    cleanup_run_checkpoint(threads, "run_mission", "workspace_agent")

    assert threads.deleted == ["run_mission"]


@pytest.mark.parametrize(
    ("mode", "never_claimed"),
    [("research", False), ("workspace_agent", True), ("agent_kernel", True)],
)
def test_runs_without_a_thread_never_need_the_checkpoint_store(
    mode: str, never_claimed: bool
) -> None:
    # No handle at all: a call would raise RunCleanupFailed.
    cleanup_run_checkpoint(None, "run_1", mode, never_claimed=never_claimed)


def test_a_claimed_agent_run_without_the_checkpoint_store_fails_visibly() -> None:
    with pytest.raises(RunCleanupFailed):
        cleanup_run_checkpoint(None, "run_1", "workspace_agent")


def test_the_agent_run_modes_are_pinned_and_cover_every_admitted_engine() -> None:
    assert AGENT_RUN_MODE_IDS == ("agent_kernel", "workspace_agent")
    assert set(AGENT_MODE_IDS) <= set(AGENT_RUN_MODE_IDS)


@pytest.mark.parametrize("mode", ["workspace_agent", "agent_kernel"])
def test_deleting_a_run_through_the_container_removes_its_thread(mode: str) -> None:
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.base import empty_checkpoint

    from tests.agents.test_kernel_walking_skeleton import ScriptedToolLLM, make_kernel_client

    client = make_kernel_client(ScriptedToolLLM([]))
    with client:
        store = client.container.run_store
        summary = store.submit(
            question="Alte Frage",
            stack_name="default",
            work=lambda handle: handle.complete({"answer": "fertig"}),
            mode=mode,
            workflow_contract=1,
        )
        run_id = summary["run_id"]
        deadline = time.monotonic() + 5
        while store.get(run_id)["status"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        saver = client.container.agent_checkpointer.saver()
        config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
        saver.put(config, empty_checkpoint(), {}, {})
        assert saver.get_tuple(config) is not None

        store.delete(run_id)

        assert saver.get_tuple(config) is None
