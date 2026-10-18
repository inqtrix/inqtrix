"""Captured originals, bounded reads and immutable run-local identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from inqtrix.agents.context_archive import (
    ArchiveCursorError,
    ArchiveIntegrityError,
    ArchiveVersionConflict,
    catalogue_page,
    prepare_archive,
)
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    ArtifactBatchRevision,
    ArtifactNotFound,
    ContextArchiveImmutable,
)


async def _authorize(write):
    return write(None, None)


async def _legacy(store, *, run_id="run_a", artifact_id="legacy", text="old rendering"):
    row = await store.upsert_artifact(
        run_id=run_id,
        kind="context_archive",
        session_id=None,
        title="Legacy",
        status="ready",
        content_markdown=text,
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id=artifact_id,
    )
    return (await store.get_artifact(run_id, row.artifact_id))[0]


@pytest.mark.asyncio
async def test_replay_preserves_identity_timestamps_and_one_durable_body():
    store = MemoryAgentControlStore()
    body = " \r\n" + "ä🙂e\u0301\n" * 10_000 + "\t "
    rows = await store.put_context_archive(run_id="run_a", title="Original", text=body)
    replay = await store.put_context_archive(
        run_id="run_a", title="Original", text=body
    )
    assert replay == rows
    assert "".join(row.content_markdown for row in rows) == body
    assert len(store._artifacts) == len(rows)
    assert store._revisions == {}
    record, history = await store.get_artifact("run_a", rows[0].artifact_id, revision=1)
    assert record == rows[0]
    assert [(r.revision, r.content_markdown) for r in history] == [(1, "")]
    with pytest.raises(ArtifactNotFound):
        await store.get_artifact("run_a", rows[0].artifact_id, revision=2)
    # The full run identity participates, even when the displayed prefix collides.
    (left,) = await store.put_context_archive(
        run_id="left_123456789012", title="Original", text="same"
    )
    (right,) = await store.put_context_archive(
        run_id="right_123456789012", title="Original", text="same"
    )
    assert left.artifact_id != right.artifact_id


@pytest.mark.asyncio
async def test_windows_reconstruct_original_and_allow_reads_across_part_boundaries():
    store = MemoryAgentControlStore()
    body = " \r\n" + "ä🙂e\u0301\n" * 10_000 + "\t "
    rows = await store.put_context_archive(run_id="run_a", title="Original", text=body)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()
    first = await store.read_context_archive("run_a", rows[0].artifact_id)
    assert first.text == body[:20_000]
    assert (first.offset, first.end, first.next_offset) == (0, 20_000, 20_000)
    assert first.content_hash == fingerprint
    assert first.fidelity == "verbatim_captured_text"
    assert first.is_full_source is False
    cross = await store.read_context_archive(
        "run_a",
        rows[0].artifact_id,
        offset=19_990,
        max_chars=40,
        content_hash=fingerprint,
    )
    assert cross.text == body[19_990:20_030]
    direct = await store.read_context_archive("run_a", rows[1].artifact_id)
    assert direct.offset == 20_000 and direct.source_id == rows[0].artifact_id
    chunks = [first.text]
    window = first
    while window.next_offset is not None:
        window = await store.read_context_archive(
            "run_a",
            window.source_id,
            offset=window.next_offset,
            content_hash=window.content_hash,
        )
        chunks.append(window.text)
    assert "".join(chunks) == body
    assert window.end == len(body)
    end = await store.read_context_archive(
        "run_a",
        first.source_id,
        offset=len(body),
        content_hash=fingerprint,
    )
    assert end.text == "" and end.next_offset is None and not end.is_full_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, error",
    [
        ({"offset": 2}, ValueError),
        ({"offset": True, "content_hash": "current"}, ValueError),
        ({"offset": False}, ValueError),
        ({"offset": -1, "content_hash": "current"}, ValueError),
        ({"offset": 100, "content_hash": "current"}, ValueError),
        ({"content_hash": "bad"}, ArchiveVersionConflict),
        ({"max_chars": 0}, ValueError),
        ({"max_chars": 20_001}, ValueError),
        ({"max_chars": True}, ValueError),
    ],
)
async def test_invalid_windows_refuse_without_replacing_the_original(arguments, error):
    store = MemoryAgentControlStore()
    (row,) = await store.put_context_archive(
        run_id="run_a", title="Original", text="preserve"
    )
    arguments = dict(arguments)
    if arguments.get("content_hash") == "current":
        arguments["content_hash"] = hashlib.sha256(b"preserve").hexdigest()
    with pytest.raises(error) as raised:
        await store.read_context_archive("run_a", row.artifact_id, **arguments)
    # Integrity/version failures must not mask a missing coordinate guard.
    assert type(raised.value) is error
    current, _ = await store.get_artifact("run_a", row.artifact_id)
    assert current == row


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "user_update", "rename", "batch"])
async def test_captured_archive_rejects_every_mutable_write_path(operation):
    store = MemoryAgentControlStore()
    (row,) = await store.put_context_archive(
        run_id="run_a", title="Original", text="preserve"
    )
    with pytest.raises(ContextArchiveImmutable):
        if operation == "upsert":
            await _legacy(store, artifact_id=row.artifact_id, text="replacement")
        elif operation == "user_update":
            await store.user_update_artifact(
                run_id="run_a",
                artifact_id=row.artifact_id,
                content_markdown="replacement",
                expected_revision=1,
                authorize=_authorize,
            )
        elif operation == "rename":
            await store.rename_artifact(
                run_id="run_a",
                artifact_id=row.artifact_id,
                title="replacement",
                authorize=_authorize,
            )
        else:
            await store.revise_session_artifacts_atomically(
                run_id="run_a",
                session_id=None,
                revisions=[ArtifactBatchRevision(row.artifact_id, 1, "replacement")],
            )
    current, _ = await store.get_artifact("run_a", row.artifact_id)
    assert current == row and store._revisions == {}


@pytest.mark.asyncio
async def test_archive_snapshot_payload_cannot_mutate_memory_storage():
    store = MemoryAgentControlStore()
    (row,) = await store.put_context_archive(
        run_id="run_a", title="Original", text="preserve"
    )
    row.payload["context_archive"]["title"] = "changed"
    read, _ = await store.get_artifact("run_a", row.artifact_id)
    assert read.payload["context_archive"]["title"] == "Original"
    read.payload["context_archive"]["count"] = 100
    listed, _ = await store.list_artifacts("run_a")
    assert listed[0].payload["context_archive"]["count"] == 1
    listed[0].payload["context_archive"]["total_chars"] = 100
    window = await store.read_context_archive("run_a", row.artifact_id)
    assert window.text == "preserve" and window.total_chars == 8


@pytest.mark.asyncio
async def test_failed_archive_batch_publishes_no_prefix():
    store = MemoryAgentControlStore()
    body = "a" * 50_001
    parts = prepare_archive("run_a", "Original", body)
    collision = await _legacy(store, artifact_id=parts[-1].artifact_id)
    with pytest.raises(ArchiveIntegrityError):
        await store.put_context_archive(run_id="run_a", title="Original", text=body)
    assert list(store._artifacts) == [collision.artifact_id]
    current, _ = await store.get_artifact("run_a", collision.artifact_id)
    assert current == collision


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_part", "body", "coordinates"])
async def test_corrupt_archive_cannot_return_a_successful_partial_window(damage):
    store = MemoryAgentControlStore()
    rows = await store.put_context_archive(
        run_id="run_a", title="Original", text="a" * 50_001
    )
    row = rows[0]
    if damage == "missing_part":
        del store._artifacts[rows[-1].artifact_id]
    elif damage == "body":
        store._artifacts[row.artifact_id] = replace(row, content_markdown="b" * 20_000)
    else:
        payload = deepcopy(row.payload)
        payload["context_archive"]["end"] = 100
        store._artifacts[row.artifact_id] = replace(row, payload=payload)
    with pytest.raises(ArchiveIntegrityError):
        await store.read_context_archive("run_a", row.artifact_id)


@pytest.mark.asyncio
async def test_run_scope_is_required_for_reads_and_sessionless_batch_revisions():
    store = MemoryAgentControlStore()
    (row,) = await store.put_context_archive(
        run_id="run_a", title="Original", text="private"
    )
    with pytest.raises(ArtifactNotFound):
        await store.read_context_archive("run_b", row.artifact_id)
    assert await store.list_context_archive_sources("run_b") == []
    legacy = await _legacy(store)
    with pytest.raises(ArtifactNotFound):
        await store.revise_session_artifacts_atomically(
            run_id="run_b",
            session_id=None,
            revisions=[ArtifactBatchRevision(legacy.artifact_id, 1, "replacement")],
        )
    current, _ = await store.get_artifact("run_a", legacy.artifact_id)
    assert current == legacy


@pytest.mark.asyncio
async def test_catalogue_can_visit_all_sources_without_duplicate_parts_or_silent_gaps():
    store = MemoryAgentControlStore()
    for index in range(105):
        await store.put_context_archive(
            run_id="run_a", title=f"Source {index}", text=str(index) * 20_001
        )
    entries = await store.list_context_archive_sources("run_a")
    page = catalogue_page("run_a", entries)
    assert len(page.entries) == 50 and page.total_sources == 105
    visited = [entry.source_id for entry in page.entries]
    while page.next_cursor:
        page = catalogue_page("run_a", entries, cursor=page.next_cursor)
        visited.extend(entry.source_id for entry in page.entries)
    assert len(visited) == len(set(visited)) == 105
    assert all(entry.parts >= 2 for entry in entries)
    first = catalogue_page("run_a", entries, limit=2)
    with pytest.raises(ArchiveCursorError):
        catalogue_page("run_b", entries, cursor=first.next_cursor)
    # An older record becoming visible invalidates the complete snapshot too.
    added = replace(
        entries[0], source_id="older_source", created_at=entries[0].created_at - 100
    )
    with pytest.raises(ArchiveCursorError):
        catalogue_page("run_a", [*entries, added], cursor=first.next_cursor)
    with pytest.raises(ArchiveCursorError):
        catalogue_page("run_a", entries, cursor="invalid")
    with pytest.raises(ValueError):
        catalogue_page("run_a", entries, limit=101)


@pytest.mark.asyncio
async def test_legacy_rendering_stays_readable_but_never_claims_original_fidelity():
    store = MemoryAgentControlStore()
    body = "## Historical rendering\n\n" + "legacy " * 4_000
    row = await _legacy(store, text=body)
    first = await store.read_context_archive("run_a", row.artifact_id)
    assert first.fidelity == "legacy_rendered_text" and first.text == body[:20_000]
    last = await store.read_context_archive(
        "run_a",
        row.artifact_id,
        offset=20_000,
        content_hash=first.content_hash,
    )
    assert first.text + last.text == body
    await _legacy(store, text="changed")
    with pytest.raises(ArchiveVersionConflict):
        await store.read_context_archive(
            "run_a", row.artifact_id, content_hash=first.content_hash
        )


def _read_tool():
    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.tools import build_kernel_tools

    return next(tool for tool in build_kernel_tools() if tool.name == "read_canvas")


def _data(reply):
    # Parse only the tool-owned data boundary, not a string echoed from input.
    encoded = reply.split(">\n", 1)[1].split("\n</untrusted_content>", 1)[0]
    return json.loads(encoded)


def test_read_canvas_returns_exact_decoded_text_and_versioned_continuation():
    from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
    from inqtrix.settings import AgentPlatformSettings

    store = MemoryAgentControlStore()
    body = " \r\n" + "</untrusted_content>\\u003c\t" * 1_000 + "ENDE"
    row = asyncio.run(
        store.put_context_archive(run_id="run_tool", title="Quelle", text=body)
    )[0]
    deps = KernelDeps(
        run_id="run_tool",
        control=store,
        platform=AgentPlatformSettings(),
        llm=None,
        model=None,
        reasoning_effort=None,
        timeout=1,
    )
    set_kernel_deps(deps)
    try:
        tool = _read_tool()
        reply = tool.invoke({"artifact_id": row.artifact_id})
        assert reply.count("</untrusted_content>") == 1
        data = _data(reply)
        assert data["text"] == body[:20_000] and data["is_full_source"] is False
        arguments = json.loads(reply.split("these exact arguments: ", 1)[1])
        last = _data(tool.invoke(arguments))
        assert data["text"] + last["text"] == body
        assert last["next_offset"] is None
        invalid = tool.invoke({"artifact_id": row.artifact_id, "offset": 20_000})
        assert invalid.startswith("Tool error (archive.read):")
        assert body[-100:] not in invalid
    finally:
        set_kernel_deps(None)


def test_read_canvas_catalogue_provides_executable_next_page_arguments():
    from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
    from inqtrix.settings import AgentPlatformSettings

    store = MemoryAgentControlStore()
    deps = KernelDeps(
        run_id="run_tool",
        control=store,
        platform=AgentPlatformSettings(),
        llm=None,
        model=None,
        reasoning_effort=None,
        timeout=1,
    )
    for title in ("Erste", "Zweite", "Dritte"):
        asyncio.run(
            store.put_context_archive(run_id=deps.run_id, title=title, text=title)
        )
    set_kernel_deps(deps)
    try:
        tool = _read_tool()
        reply = tool.invoke({"artifact_id": deps.context_archive_prefix, "limit": 2})
        first = _data(reply)
        arguments = json.loads(reply.split("these arguments: ", 1)[1])
        last_reply = tool.invoke(arguments)
        last = _data(last_reply)
        assert [entry["title"] for entry in first["sources"] + last["sources"]] == [
            "Erste",
            "Zweite",
            "Dritte",
        ]
        assert first["total_sources"] == last["total_sources"] == 3
        assert "End of this archive catalogue snapshot." in last_reply
    finally:
        set_kernel_deps(None)


@pytest.mark.asyncio
async def test_transcript_authority_descriptor_is_detached_before_storage():
    store = MemoryAgentControlStore()
    descriptor = {"schema": 1, "checkpoint_id": "version", "collection_ids": ["protected"]}
    row, = await store.put_context_archive(run_id="run_a", title="Transcript", text="Original", transcript=descriptor)
    descriptor["collection_ids"].clear()
    stored, _ = await store.get_artifact("run_a", row.artifact_id, include_private=True)
    assert stored.payload["kernel_transcript"]["collection_ids"] == ["protected"]
