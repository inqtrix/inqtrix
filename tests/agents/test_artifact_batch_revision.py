"""Atomic multi-canvas revision contract."""

from __future__ import annotations

import asyncio

import pytest

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    ArtifactBatchRevision,
    ArtifactNotFound,
    ArtifactRevisionConflict,
)


def _create(
    store: MemoryAgentControlStore,
    artifact_id: str,
    *,
    session_id: str = "sess-1",
) -> None:
    asyncio.run(
        store.upsert_artifact(
            run_id="run-old",
            kind="deliverable",
            session_id=session_id,
            title=artifact_id,
            status="ready",
            content_markdown=f"old:{artifact_id}",
            payload={"stable": True},
            refs=[{"url": f"https://example.com/{artifact_id}"}],
            updated_by="agent",
            artifact_id=artifact_id,
        )
    )


def test_batch_revision_updates_all_and_preserves_metadata() -> None:
    store = MemoryAgentControlStore()
    _create(store, "art-a")
    _create(store, "art-b")

    rows = asyncio.run(
        store.revise_session_artifacts_atomically(
            run_id="run-new",
            session_id="sess-1",
            revisions=[
                ArtifactBatchRevision("art-a", 1, "new a"),
                ArtifactBatchRevision("art-b", 1, "new b"),
            ],
        )
    )

    assert [row.revision for row in rows] == [2, 2]
    assert [row.content_markdown for row in rows] == ["new a", "new b"]
    assert all(row.payload == {"stable": True} for row in rows)
    assert all(len(row.refs) == 1 for row in rows)


@pytest.mark.parametrize("failure", ["unknown", "conflict"])
def test_batch_revision_failure_changes_nothing(failure: str) -> None:
    store = MemoryAgentControlStore()
    _create(store, "art-a")
    _create(store, "art-b")
    second = (
        ArtifactBatchRevision("art-missing", 1, "new b")
        if failure == "unknown"
        else ArtifactBatchRevision("art-b", 9, "new b")
    )

    with pytest.raises(
        ArtifactNotFound if failure == "unknown" else ArtifactRevisionConflict
    ):
        asyncio.run(
            store.revise_session_artifacts_atomically(
                run_id="run-new",
                session_id="sess-1",
                revisions=[ArtifactBatchRevision("art-a", 1, "new a"), second],
            )
        )

    art_a = asyncio.run(store.get_session_artifact_by_id("sess-1", "art-a"))
    art_b = asyncio.run(store.get_session_artifact_by_id("sess-1", "art-b"))
    assert (art_a.revision, art_a.content_markdown) == (1, "old:art-a")
    assert (art_b.revision, art_b.content_markdown) == (1, "old:art-b")
