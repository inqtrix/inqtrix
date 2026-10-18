"""Saved-conversation reads preserve originals and recheck their authority."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from inqtrix.agents.context_archive import text_hash
from inqtrix.agents.context_sources import ContextReadInput, ContextSearchInput, encode_source
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import ArtifactNotFound
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.capabilities import CapabilityContext, CapabilityError
from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.services.agent_sessions_service import AgentSessionsService
from inqtrix.services.agent_source_service import AgentSourceService
from inqtrix.server.runs import RunNotFound, RunRecord, RunStatus, RunStore


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
READER = uuid.UUID("22222222-2222-4222-8222-222222222222")


def identity(user=OWNER):
    return UserContext(Principal(user_id=user, kind="oidc_session"))


@pytest.fixture
def sources():
    runs = RunStore(max_concurrent=2, max_queue_size=10, completed_ttl_seconds=30, event_buffer_size=20)
    session_store = MemoryAgentSessionStore()
    sessions = AgentSessionsService(store=session_store, run_store=runs)
    control = MemoryAgentControlStore()
    service = AgentSourceService(sessions=sessions, run_store=runs, control=control)
    scope = identity()
    context = CapabilityContext(principal=scope.principal, visible_to=scope, workspace_id="desk", run_id="current")
    for index, run_id in enumerate(("old", "current")):
        runs._records[run_id] = RunRecord(
            run_id=run_id, question="Current task", stack_name="fixture", workspace_id="desk",
            created_at=index, work=None, created_by_user_id=OWNER, created_by_tenant_id="default",
            request_payload={"body": {}}, kind="agent", mode="agent_kernel", session_id="conversation",
            status=RunStatus.COMPLETED, result={"answer": "Published result"},
        )
    return SimpleNamespace(service=service, runs=runs, control=control, sessions=sessions,
                           session_store=session_store, context=context)


async def saved(sources):
    await sources.session_store.claim_session(id="conversation", title="Conversation", created_at=1,
                                             created_by_user_id=OWNER, workspace_id="desk")
    return sources


async def transcript(s, text, **dependencies):
    return await s.control.put_context_archive(run_id="old", title="Transcript", text=text,
        transcript={"schema": 1, "checkpoint_id": text_hash(text), "format": "checkpoint_json", **dependencies})


@pytest.mark.asyncio
async def test_private_transcript_is_excluded_from_product_artifacts_but_owner_can_read(sources):
    s = await saved(sources)
    rows = await transcript(s, " \r\nPrivate original ä🙂e\u0301\t ")
    source_id = encode_source("transcript", "old", rows[0].artifact_id, text_hash(rows[0].content_markdown))
    assert await s.control.list_artifacts("old") == ([], None)
    assert await s.control.list_context_archive_sources("old") == []
    with pytest.raises(ArtifactNotFound):
        await s.control.get_artifact("old", rows[0].artifact_id)
    with pytest.raises(ArtifactNotFound):
        await s.control.read_context_archive("old", rows[0].artifact_id)
    result = await s.service.read(ContextReadInput(source_id=source_id), s.context)
    assert result.text == " \r\nPrivate original ä🙂e\u0301\t "
    assert result.representation == "checkpoint_json"
    assert result._authorization_receipt["owner_origin_run_ids"] == ["old"]
    assert "authorization" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reader", "shared_origin", "workspace", "other_session", "deleted_session", "deleting_session", "no_scope"])
async def test_source_references_never_grant_private_session_access(sources, change):
    s = await saved(sources)
    rows = await transcript(s, "Private body")
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash("Private body"))
    context = s.context
    if change == "reader":
        scope = identity(READER)
        context = replace(context, principal=scope.principal, visible_to=scope)
        s.runs._share_lookup = lambda **kwargs: SharePermission.VIEW
    elif change == "shared_origin":
        s.runs._records["old"].created_by_user_id = READER
        s.runs._share_lookup = lambda **kwargs: SharePermission.VIEW
    elif change == "workspace":
        context = replace(context, workspace_id="other")
    elif change == "other_session":
        s.runs._records["old"].session_id = "other"
    elif change == "deleted_session":
        s.session_store._sessions.clear()
    elif change == "deleting_session":
        s.session_store._sessions["conversation"] = replace(s.session_store._sessions["conversation"], lifecycle_status="deleting")
    else:
        context = replace(context, visible_to=None)
    with pytest.raises(CapabilityError, match="unavailable"):
        await s.service.read(ContextReadInput(source_id=ref), context)


@pytest.mark.asyncio
async def test_literal_search_crosses_windows_and_reports_only_actually_scanned_ranges(sources):
    s = await saved(sources)
    text = "ä" * 19_998 + "a.[b]" + "z" * 100_000 + "a.[b]"
    rows = await transcript(s, text)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))
    result = await s.service.search(ContextSearchInput(query="a.[b]", source_id=ref), s.context)
    assert [(x["start"], x["end"]) for x in result.matches] == [(19_998, 20_003)]
    assert sum(x["end"] - x["start"] for x in result.scanned_ranges) == 100_000
    assert not result.scan_complete and not result.corpus_complete and result.next_cursor
    follow = await s.service.search(ContextSearchInput(query="a.[b]", source_id=ref, cursor=result.next_cursor), s.context)
    assert [(x["start"], x["end"]) for x in follow.matches] == [(120_003, 120_008)]
    assert follow.scan_complete and not follow.corpus_complete
    read = await s.service.read(ContextReadInput(**follow.matches[0]["read"]), s.context)
    assert read.text.endswith("a.[b]") and not read.is_full_source


@pytest.mark.asyncio
async def test_match_limit_continues_without_duplicate_overlapping_matches(sources):
    s = await saved(sources)
    text = "a" * 45
    rows = await transcript(s, text)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))
    matches, cursor = [], ""
    for _ in range(4):
        result = await s.service.search(ContextSearchInput(query="aa", source_id=ref, cursor=cursor), s.context)
        matches.extend(x["start"] for x in result.matches)
        if result.next_cursor is None:
            break
        cursor = result.next_cursor
    assert matches == list(range(44))


@pytest.mark.asyncio
async def test_old_full_question_and_changed_source_cursor(sources):
    s = await saved(sources)
    text = "Long instruction " * 400 + "Budget: 1200 EUR"
    s.runs._records["old"].question = text
    results = await s.service.search(ContextSearchInput(query="1200"), s.context)
    assert any(hit["source"]["kind"] == "question" and "1200 EUR" in hit["excerpt"] for hit in results.matches)
    assert any(item.unavailable_reason == "not_recorded" for item in results.unavailable_sources)
    assert not results.corpus_complete
    for index in range(51):
        await transcript(s, f"Document {index}")
    page = await s.service.search(ContextSearchInput(), s.context)
    assert len(page.sources) == 50 and page.next_cursor
    await transcript(s, "Newly available original")
    with pytest.raises(CapabilityError) as caught:
        await s.service.search(ContextSearchInput(cursor=page.next_cursor), s.context)
    assert caught.value.code == "context.cursor_changed"


@pytest.mark.asyncio
async def test_dependency_revocation_and_missing_file_adapter_are_explicit(sources):
    s = await saved(sources)
    rows = await transcript(s, "Knowledge original", collection_ids=["collection"])
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash("Knowledge original"))
    context = replace(s.context, knowledge_collection_ids=frozenset({"collection"}))
    class Knowledge:
        allowed = True
        async def assert_collections_visible(self, ids, *, visible_to):
            assert ids == ["collection"] and visible_to == context.visible_to
            if not self.allowed:
                raise LookupError("Collection revoked")
    s.service.knowledge = Knowledge()
    result = await s.service.read(ContextReadInput(source_id=ref), context)
    s.service.knowledge.allowed = False
    with pytest.raises(CapabilityError):
        await s.service.validate_receipt(context, result._authorization_receipt)
    rows = await transcript(s, "Attachment body", restricted_inputs=True)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash("Attachment body"))
    with pytest.raises(CapabilityError):
        await s.service.read(ContextReadInput(source_id=ref), s.context)


@pytest.mark.asyncio
async def test_owner_origin_dependency_does_not_accept_a_result_share(sources):
    s = await saved(sources)
    receipt = {"owner_origin_run_ids": ["old"]}
    await s.service.validate_receipt(s.context, receipt)
    s.runs._records["old"].created_by_user_id = READER
    s.runs._share_lookup = lambda **kwargs: SharePermission.VIEW
    with pytest.raises(CapabilityError):
        await s.service.validate_receipt(s.context, receipt)


@pytest.mark.asyncio
async def test_saved_session_owns_retention_until_deletion(sources):
    s = await saved(sources)
    old = s.runs._records["old"]
    old.finished_monotonic = time.monotonic() - 120
    s.runs._records["ordinary"] = replace(old, run_id="ordinary", kind="standard", session_id=None)
    assert s.runs.get("old", visible_to=s.context.visible_to)["run_id"] == "old"
    with pytest.raises(RunNotFound):
        s.runs.get("ordinary", visible_to=s.context.visible_to)
    s.session_store._sessions["conversation"] = replace(s.session_store._sessions["conversation"], lifecycle_status="deleting")
    assert s.runs.get("old", visible_to=s.context.visible_to)["run_id"] == "old"
    s.session_store._sessions.clear()
    with pytest.raises(RunNotFound):
        s.runs.get("old", visible_to=s.context.visible_to)


@pytest.mark.asyncio
@pytest.mark.parametrize("content, restricted", [
    ([{"type": "text", "text": "A text-only request"}], False),
    ([{"type": "file", "file_id": "attachment"}], True),
])
async def test_archive_provenance_distinguishes_plain_messages_from_file_inputs(sources, content, restricted):
    s = await saved(sources)
    s.runs._records["current"].request_payload = {"body": {"messages": content}}
    row, = await s.control.put_context_archive(run_id="current", title="Tool output", text="Captured text")
    ref = encode_source("archive", "current", row.artifact_id, text_hash("Captured text"))
    result = await s.service.read(ContextReadInput(source_id=ref), s.context)
    assert result._authorization_receipt["restricted_inputs"] is restricted


@pytest.mark.asyncio
async def test_source_metadata_cannot_expose_credentials_from_an_original_title(sources):
    s = await saved(sources)
    s.runs._records["old"].question = "See https://example.com/?api_key=private-title-value"
    result = await s.service.search(ContextSearchInput(), s.context)
    assert "private-title-value" not in result.model_dump_json()
    question = next(item for item in result.sources if item.kind == "question")
    assert "[REDACTED]" in question.title
    with pytest.raises(CapabilityError) as caught:
        await s.service.read(ContextReadInput(source_id=question.source_id), s.context)
    assert caught.value.code == "context.protected_content"


@pytest.mark.asyncio
async def test_checkpoint_cleanup_is_after_owner_and_terminal_checks(sources):
    from inqtrix.exceptions import RunCleanupFailed
    from inqtrix.server.runs import RunActive

    s = await saved(sources)
    calls = []
    s.runs.bind_checkpoint_cleanup(lambda run_id, mode: calls.append((run_id, mode)))
    with pytest.raises(RunNotFound):
        s.runs.delete("old", requester_user_id=READER)
    with pytest.raises(RunNotFound):
        s.runs.delete("old", requester_user_id=OWNER, workspace_id="other")
    s.runs._records["old"].status = RunStatus.RUNNING
    with pytest.raises(RunActive):
        s.runs.delete("old", requester_user_id=OWNER)
    assert calls == []
    s.runs._records["old"].status = RunStatus.COMPLETED
    def unavailable(run_id, mode):
        raise RunCleanupFailed("Unavailable")
    s.runs.bind_checkpoint_cleanup(unavailable)
    with pytest.raises(RunCleanupFailed):
        s.runs.delete("old", requester_user_id=OWNER)
    assert s.runs.get("old", visible_to=s.context.visible_to)["run_id"] == "old"
    s.runs.bind_checkpoint_cleanup(lambda run_id, mode: calls.append((run_id, mode)))
    s.runs.delete("old", requester_user_id=OWNER)
    assert calls == [("old", "agent_kernel")]
    with pytest.raises(RunNotFound):
        s.runs.get("old", visible_to=s.context.visible_to)


@pytest.mark.asyncio
async def test_retention_retries_failed_checkpoint_cleanup_without_losing_anchor(sources):
    from inqtrix.exceptions import RunCleanupFailed

    s = await saved(sources)
    record = s.runs._records["old"]
    record.session_id = None
    record.finished_monotonic = time.monotonic() - 120
    def unavailable(run_id, mode):
        raise RunCleanupFailed("Unavailable")
    s.runs.bind_checkpoint_cleanup(unavailable)
    assert s.runs.get("old", visible_to=s.context.visible_to)["run_id"] == "old"
    calls = []
    s.runs.bind_checkpoint_cleanup(lambda run_id, mode: calls.append((run_id, mode)))
    with pytest.raises(RunNotFound):
        s.runs.get("old", visible_to=s.context.visible_to)
    assert calls == [("old", "agent_kernel")]


@pytest.mark.asyncio
async def test_published_answers_precede_large_earlier_execution_sources(sources):
    s = await saved(sources)
    s.runs._records["old"].result = {"answer": "Earlier research. " * 8000}
    original = "Execution details. " * 8000 + "Original archive tail."
    archived = await transcript(s, original)
    s.runs._records["later"] = replace(s.runs._records["old"], run_id="later", created_at=3)
    answer = await s.control.upsert_artifact(
        run_id="later", session_id="conversation", kind="answer", title="Answer",
        artifact_id="later_answer",
        status="ready", content_markdown="The corrected budget is EUR 12500.",
        payload={}, refs=[], updated_by="agent",
    )
    first = await s.service.search(ContextSearchInput(query="corrected budget"), s.context)
    assert first.matches
    assert first.matches[0]["source"]["source_id"] == encode_source("artifact", "later", answer.artifact_id, "1")
    assert not first.scan_complete and first.next_search is not None
    # Prioritization must leave the complete archive reachable under the
    # same bounded search; it is ordering, never source suppression.
    ranges = list(first.scanned_ranges)
    page = first
    for _ in range(8):
        if page.next_search is None:
            break
        page = await s.service.search(page.next_search, s.context)
        ranges.extend(page.scanned_ranges)
    assert page.scan_complete and page.next_search is None
    archive_id = encode_source("transcript", "old", archived[0].artifact_id, text_hash(original))
    assert max(row["end"] for row in ranges if row["source_id"] == archive_id) == len(original)


@pytest.mark.asyncio
async def test_search_continuation_repeats_exact_query_scope_and_case(sources):
    s = await saved(sources)
    body = "x" * 100_001 + "Budget.[Ä]"
    rows = await transcript(s, body)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(body))
    first = await s.service.search(ContextSearchInput(
        query="Budget.[Ä]", source_id=ref, case_sensitive=True,
    ), s.context)
    assert first.matches == [] and not first.scan_complete
    assert first.next_search is not None
    assert first.next_search.model_dump() == {
        "query": "Budget.[Ä]", "source_id": ref, "case_sensitive": True,
        "cursor": first.next_cursor,
    }
    second = await s.service.search(first.next_search, s.context)
    assert [(hit["start"], hit["end"]) for hit in second.matches] == [(100_001, 100_011)]
    assert second.next_search is None and second.scan_complete
    assert not second.corpus_complete  # No claim that the caller read page one.


@pytest.mark.asyncio
async def test_scanned_ranges_carry_the_source_version(sources):
    """A scan span names the version it scanned, so coverage records key on it."""
    from inqtrix.agents.context_sources import decode_source

    s = await saved(sources)
    body = "Budget bleibt offen. " * 10  # fewer matches than one result page holds
    rows = await transcript(s, body)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(body))
    result = await s.service.search(ContextSearchInput(query="Budget", source_id=ref), s.context)
    (span,) = result.scanned_ranges
    assert span["source_id"] == ref
    assert span["version"] == decode_source(ref)[3] == text_hash(body)
    assert (span["start"], span["end"], span["total_chars"]) == (0, len(body), len(body))
    assert result.scan_complete and result.next_search is None


@pytest.mark.asyncio
async def test_catalogue_continuation_is_directly_callable(sources):
    s = await saved(sources)
    for number in range(55):
        await transcript(s, f"Original {number}")
    first = await s.service.search(ContextSearchInput(), s.context)
    assert first.next_search is not None
    assert first.next_search.model_dump() == {
        "query": "", "source_id": "", "case_sensitive": False,
        "cursor": first.next_cursor,
    }
    second = await s.service.search(first.next_search, s.context)
    assert second.sources and not ({row.source_id for row in first.sources} & {row.source_id for row in second.sources})
    assert second.next_search is None and second.scan_complete


@pytest.mark.parametrize("incomplete", [True, False])
def test_context_tool_explains_partial_search_before_source_data(incomplete):
    pytest.importorskip("deepagents")
    from inqtrix.agents.context_sources import ContextSearchOutput
    from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
    from inqtrix.agents.kernel.tools import build_kernel_tools
    from inqtrix.settings import AgentPlatformSettings

    from inqtrix.agents.context_sources import ContextSource

    later = ContextSource(source_id=encode_source("transcript", "later", "art", "v1"), kind="transcript",
                          title="Kernel transcript · Read the approved guide", origin_run_id="later", version="v1")

    async def invoke(capability, payload, context):
        return ContextSearchOutput(
            sources=[], query="Budget", scan_complete=not incomplete, corpus_complete=not incomplete,
            next_cursor="continuation" if incomplete else None,
            next_search=ContextSearchInput(query="Budget", cursor="continuation") if incomplete else None,
            unscanned_sources=[later] if incomplete else [], unscanned_total=3 if incomplete else 0,
        )
    deps = KernelDeps(
        run_id="current", control=MemoryAgentControlStore(), platform=AgentPlatformSettings(),
        llm=None, model=None, reasoning_effort=None, timeout=1,
        capability_registry=SimpleNamespace(invoke=invoke),
    )
    set_kernel_deps(deps)
    try:
        tool = next(item for item in build_kernel_tools() if item.name == "search_context")
        result = tool.invoke({"query": "Budget"})
    finally:
        set_kernel_deps(None)
    # Assert on the adapter-owned preamble, not text the source could echo.
    preamble = result.split('<untrusted_content source="conversation-source">', 1)[0]
    assert preamble.startswith("This search is incomplete.") is incomplete
    if incomplete:
        assert "exact next_search arguments" in preamble
        assert "A different query without a cursor scans the beginning again" in preamble
        # The remainder has an address: the adapter says how many sources the
        # scan has not reached and that one can be searched by its source_id.
        assert "unscanned_sources names 1 of 3 sources" in preamble
        assert "call search_context with its source_id" in preamble
    else:
        assert "unscanned_sources" not in preamble
    # The description tells the model where earlier tool output lives.
    assert "kernel transcript" in tool.description and "source_id" in tool.description


# -- a reader must be able to pick the turn and address the remainder -- #


async def _later_run(s, index, question):
    run_id = f"later{index}"
    s.runs._records[run_id] = replace(s.runs._records["old"], run_id=run_id, created_at=3 + index, question=question)
    return run_id


@pytest.mark.asyncio
async def test_run_bound_sources_name_the_turn_they_belong_to(sources):
    s = await saved(sources)
    question = "Read the approved onboarding guide completely and name the binding special rules for the pilot"
    s.runs._records["old"].question = question
    await transcript(s, "Window text of the guide.")
    await s.control.upsert_artifact(
        run_id="old", session_id="conversation", kind="answer", title="Answer", artifact_id="old_answer",
        status="ready", content_markdown="The rules are at the end.", payload={}, refs=[], updated_by="agent",
    )
    listing = await s.service.search(ContextSearchInput(query=""), s.context)
    titles = {(row.kind, row.origin_run_id): row.title for row in listing.sources}
    label = question[:80] + "…"
    assert titles[("transcript", "old")] == f"Kernel transcript · {label}"
    assert titles[("artifact", "old")] == f"Answer · {label}"
    assert titles[("result", "current")] == "Published answer · Current task"
    # The question source already IS the turn; it keeps its own excerpt.
    assert titles[("question", "old")] == question[:100]


@pytest.mark.asyncio
async def test_incomplete_search_names_the_sources_it_has_not_reached(sources):
    from inqtrix.agents.context_sources import SOURCE_UNSCANNED_LISTED

    s = await saved(sources)
    await s.control.upsert_artifact(
        run_id="old", session_id="conversation", kind="answer", title="Answer", artifact_id="old_answer",
        status="ready", content_markdown="Long earlier answer. " * 8000, payload={}, refs=[], updated_by="agent",
    )
    expected = []
    for index in range(14):
        run_id = await _later_run(s, index, f"Later question {index}")
        row = await s.control.upsert_artifact(
            run_id=run_id, session_id="conversation", kind="answer", title="Answer", artifact_id=f"{run_id}_answer",
            status="ready", content_markdown=f"Short answer {index}.", payload={}, refs=[], updated_by="agent",
        )
        expected.append(encode_source("artifact", run_id, row.artifact_id, "1"))
    first = await s.service.search(ContextSearchInput(query="absent phrase"), s.context)
    assert not first.scan_complete
    scanned = {row["source_id"] for row in first.scanned_ranges}
    listed = [row.source_id for row in first.unscanned_sources]
    # Bounded, in scan order, never a source this response already touched.
    assert len(listed) == SOURCE_UNSCANNED_LISTED
    assert listed == expected[:SOURCE_UNSCANNED_LISTED]
    assert not scanned.intersection(listed)
    assert first.unscanned_total == 15  # 14 later answers and the current result
    assert first.unscanned_sources[0].title == "Answer · Later question 0"
    page = first
    for _ in range(8):
        if page.next_search is None:
            break
        page = await s.service.search(page.next_search, s.context)
    assert page.scan_complete and page.unscanned_sources == [] and page.unscanned_total == 0


@pytest.mark.asyncio
async def test_targeted_search_reaches_a_late_transcript_without_paging(sources):
    s = await saved(sources)
    await s.control.upsert_artifact(
        run_id="old", session_id="conversation", kind="answer", title="Answer", artifact_id="old_answer",
        status="ready", content_markdown="Long earlier answer. " * 8000, payload={}, refs=[], updated_by="agent",
    )
    run_id = await _later_run(s, 0, "Read the approved guide")
    body = "Guide body. " * 200 + "The archive owner names a deputy before handover."
    await s.control.put_context_archive(
        run_id=run_id, title="Transcript", text=body,
        transcript={"schema": 1, "checkpoint_id": text_hash(body), "format": "checkpoint_json"},
    )
    first = await s.service.search(ContextSearchInput(query="names a deputy"), s.context)
    assert not first.matches and not first.scan_complete
    target = next(row for row in first.unscanned_sources if row.kind == "transcript")
    assert target.title == "Kernel transcript · Read the approved guide"
    direct = await s.service.search(ContextSearchInput(query="names a deputy", source_id=target.source_id), s.context)
    assert direct.scan_complete and direct.unscanned_sources == []
    assert [row["source"]["source_id"] for row in direct.matches] == [target.source_id]


# -- materials a run transferred re-authorized on every later read -- #


class _KnowledgeMaterials:
    """Stands in for KnowledgeService: collection admission plus document visibility."""

    def __init__(self) -> None:
        self.documents = {"kd1"}
        self.collections_ok = True

    async def assert_collections_visible(self, ids, *, visible_to):
        if not self.collections_ok:
            raise LookupError("Collection revoked")

    async def get_document(self, document_id, *, visible_to=None):
        if document_id not in self.documents:
            raise LookupError("Document deleted or hidden")
        return SimpleNamespace(id=document_id, active_revision_id="rev_changed_since")


class _EditorMaterials:
    """Stands in for EditorPersistenceService.assert_comments_visible."""

    def __init__(self) -> None:
        self.documents = {"ed1": {"c1", "c2"}}

    async def assert_comments_visible(self, document_id, comment_ids, *, visible_to):
        comments = self.documents.get(document_id)
        if comments is None or not set(comment_ids) <= comments:
            raise LookupError("Document or comment gone")
        return SimpleNamespace(id=document_id)


MATERIALS = {
    "knowledge_documents": [{"document_id": "kd1", "collection_id": "collection"}],
    "editor_documents": [{"document_id": "ed1", "comment_ids": ["c1"]}],
}


async def _material_transcript(s):
    text = "Window text of both documents"
    rows = await transcript(s, text, collection_ids=["collection"], materials=MATERIALS)
    return encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))


@pytest.mark.asyncio
async def test_transcript_materials_stay_readable_while_their_documents_exist(sources):
    s = await saved(sources)
    ref = await _material_transcript(s)
    context = replace(s.context, knowledge_collection_ids=frozenset({"collection"}))
    s.service.knowledge, s.service.editor = _KnowledgeMaterials(), _EditorMaterials()
    result = await s.service.read(ContextReadInput(source_id=ref), context)
    assert result.text == "Window text of both documents"
    # The receipt carries the materials, so the run re-checks them at every safepoint.
    assert result._authorization_receipt["materials"] == MATERIALS
    await s.service.validate_receipt(context, result._authorization_receipt)
    listing = await s.service.search(ContextSearchInput(), context)
    entry = next(row for row in listing.sources if row.source_id == ref)
    assert entry.available and entry.unavailable_reason == ""
    hits = await s.service.search(ContextSearchInput(query="Window text"), context)
    assert hits.matches and hits.matches[0]["source"]["source_id"] == ref


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke, code, reason", [
    ("knowledge_document", "context.material_unavailable", "material_unavailable"),
    ("editor_document", "context.material_unavailable", "material_unavailable"),
    ("comment", "context.material_unavailable", "material_unavailable"),
    ("editor_service", "context.material_unavailable", "material_unavailable"),
    ("knowledge_service", "context.not_found", "origin_scope_unavailable"),
    ("collection", "context.not_found", "origin_scope_unavailable"),
])
async def test_transcript_closes_when_a_transferred_material_is_gone(sources, revoke, code, reason):
    s = await saved(sources)
    ref = await _material_transcript(s)
    context = replace(s.context, knowledge_collection_ids=frozenset({"collection"}))
    knowledge, editor = _KnowledgeMaterials(), _EditorMaterials()
    s.service.knowledge, s.service.editor = knowledge, editor
    result = await s.service.read(ContextReadInput(source_id=ref), context)
    if revoke == "knowledge_document":
        knowledge.documents.clear()
    elif revoke == "editor_document":
        editor.documents.clear()
    elif revoke == "comment":
        editor.documents["ed1"] = {"c2"}
    elif revoke == "editor_service":
        s.service.editor = None
    elif revoke == "knowledge_service":
        s.service.knowledge = None
    elif revoke == "collection":
        knowledge.collections_ok = False
    with pytest.raises(CapabilityError) as caught:
        await s.service.read(ContextReadInput(source_id=ref), context)
    assert caught.value.code == code
    # The transfer already made is re-checked too: the run must stop, not continue on stale text.
    with pytest.raises(CapabilityError):
        await s.service.validate_receipt(context, result._authorization_receipt)
    listing = await s.service.search(ContextSearchInput(), context)
    entry = next(row for row in listing.sources if row.source_id == ref)
    assert (entry.available, entry.unavailable_reason) == (False, reason)
    hits = await s.service.search(ContextSearchInput(query="Window text"), context)
    assert hits.matches == [] and any(row.source_id == ref for row in hits.unavailable_sources)


@pytest.mark.asyncio
async def test_current_run_archive_is_not_locked_by_an_editing_target_alone(sources):
    s = await saved(sources)
    s.runs._records["current"].request_payload = {"body": {"document_id": "ed_target", "messages": [{"type": "text", "text": "Frage"}]}}
    row, = await s.control.put_context_archive(run_id="current", title="Tool output", text="Captured text")
    ref = encode_source("archive", "current", row.artifact_id, text_hash("Captured text"))
    result = await s.service.read(ContextReadInput(source_id=ref), s.context)
    assert result._authorization_receipt["restricted_inputs"] is False


class _Templates:
    """A prompt-library catalog: template id -> revision, visible only to
    the one user it is shared with (the fixture's reader by default).

    Visibility follows the caller like the real catalog does, so a lookup
    under any identity other than the reader's finds nothing."""

    def __init__(self, *, visible_for=OWNER, **revisions):
        self.visible_for = visible_for
        self.revisions = dict(revisions)

    async def list_visible(self, *, tenant_id, visible_to):
        user = visible_to.principal.user_id if visible_to is not None else None
        if user != self.visible_for:
            return []
        return [
            (SimpleNamespace(id=template_id, label=template_id, title=template_id.title(),
                             revision=revision, content_markdown="Regeltext"), None)
            for template_id, revision in self.revisions.items()
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "edited", "deleted", "unshared", "no_library"])
async def test_transcript_closes_when_a_bound_rule_changes_or_disappears(sources, change):
    """A transcript holds the full text of the rules its run worked under.
    After an edit, a deletion or a revoked share it must not stay readable
    through the transcript — bound by revision exactly like skills, and a
    missing catalog is never an implicit grant."""
    s = await saved(sources)
    text = "Transcript with the full rule text"
    rows = await transcript(s, text, rule_revisions={"pt_rule": 2})
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))
    s.service.prompt_templates = {
        "unchanged": _Templates(pt_rule=2),
        "edited": _Templates(pt_rule=3),
        "deleted": _Templates(),
        # Still there at the same revision for the user who shares it,
        # no longer for the reader.
        "unshared": _Templates(visible_for=READER, pt_rule=2),
        "no_library": None,
    }[change]
    if change == "unchanged":
        result = await s.service.read(ContextReadInput(source_id=ref), s.context)
        assert result.text == text
        assert result._authorization_receipt["rule_revisions"] == {"pt_rule": 2}
        return
    with pytest.raises(CapabilityError, match="unavailable"):
        await s.service.read(ContextReadInput(source_id=ref), s.context)
    listing = await s.service.search(ContextSearchInput(), s.context)
    entry = next(row for row in listing.sources if row.source_id == ref)
    assert (entry.available, entry.unavailable_reason) == (False, "origin_scope_unavailable")


@pytest.mark.asyncio
async def test_a_role_run_is_no_source_of_the_conversation(sources):
    """A role run (a review, a section writer) carries the requirement of
    the run it works for inside its handover, rule bodies included, but
    binds no rules of its own. The catalogue leaves it out, and a source
    id naming it must not open it either; the role run still reads its
    own archives, which lie inside its executing boundary."""
    s = await saved(sources)
    s.runs._records["old"].request_payload = {"body": {"work_profile": "review"}}
    text = "Role transcript with the full rule text"
    rows = await transcript(s, text)
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))
    with pytest.raises(CapabilityError, match="unavailable"):
        await s.service.read(ContextReadInput(source_id=ref), s.context)
    for kind, local_id, version in (("result", "", "result"), ("question", "", "request")):
        with pytest.raises(CapabilityError, match="unavailable"):
            await s.service.read(ContextReadInput(source_id=encode_source(kind, "old", local_id, version)),
                                 s.context)
    listing = await s.service.search(ContextSearchInput(), s.context)
    assert not any(row.origin_run_id == "old" for row in listing.sources)
    # The role run itself keeps reading its own archive.
    s.runs._records["current"].request_payload = {"body": {"work_profile": "review"}}
    row, = await s.control.put_context_archive(run_id="current", title="Tool output", text="Own text")
    own = encode_source("archive", "current", row.artifact_id, text_hash("Own text"))
    assert (await s.service.read(ContextReadInput(source_id=own), s.context)).text == "Own text"


@pytest.mark.asyncio
async def test_a_transferred_rule_is_rechecked_before_the_next_model_call(sources):
    s = await saved(sources)
    text = "Transcript with the full rule text"
    rows = await transcript(s, text, rule_revisions={"pt_rule": 2})
    ref = encode_source("transcript", "old", rows[0].artifact_id, text_hash(text))
    templates = _Templates(pt_rule=2)
    s.service.prompt_templates = templates
    result = await s.service.read(ContextReadInput(source_id=ref), s.context)
    await s.service.validate_receipt(s.context, result._authorization_receipt)
    templates.revisions["pt_rule"] = 3
    with pytest.raises(CapabilityError):
        await s.service.validate_receipt(s.context, result._authorization_receipt)


@pytest.mark.asyncio
async def test_a_run_keeps_reading_its_own_archive_after_its_rule_changes(sources):
    """Rules are frozen per run: editing the template mid-run changes
    nothing the run works under, so its own archives stay readable — the
    binding is for OTHER runs. A rule the archive took over from another
    run's transcript is not the run's own and stays bound."""
    s = await saved(sources)
    s.runs._records["current"].request_payload = {"body": {"report_rules": [
        {"template_id": "pt_own", "label": "briefing", "revision": 2},
    ]}}
    s.service.prompt_templates = _Templates(pt_own=3, pt_other=1)

    async def own_archive(text, rule_revisions):
        rows = await s.control.put_context_archive(
            run_id="current", title="Compacted history", text=text,
            transcript={"schema": 1, "checkpoint_id": text_hash(text), "format": "compaction_json",
                        "rule_revisions": rule_revisions},
        )
        return encode_source("transcript", "current", rows[0].artifact_id, text_hash(text))

    own = await own_archive("Compacted history of this run", {"pt_own": 2})
    result = await s.service.read(ContextReadInput(source_id=own), s.context)
    assert result.text == "Compacted history of this run"
    await s.service.validate_receipt(s.context, result._authorization_receipt)
    taken_over = await own_archive("History quoting an earlier transcript", {"pt_own": 2, "pt_other": 0})
    with pytest.raises(CapabilityError, match="unavailable"):
        await s.service.read(ContextReadInput(source_id=taken_over), s.context)


@pytest.mark.asyncio
async def test_a_plain_archive_of_another_run_is_bound_by_that_runs_rules(sources):
    s = await saved(sources)
    await transcript(s, "Private transcript", rule_revisions={"pt_rule": 2})
    row, = await s.control.put_context_archive(run_id="old", title="Tool output", text="Captured text")
    ref = encode_source("archive", "old", row.artifact_id, text_hash("Captured text"))
    templates = _Templates(pt_rule=2)
    s.service.prompt_templates = templates
    assert (await s.service.read(ContextReadInput(source_id=ref), s.context)).text == "Captured text"
    templates.revisions["pt_rule"] = 3
    with pytest.raises(CapabilityError, match="unavailable"):
        await s.service.read(ContextReadInput(source_id=ref), s.context)


def test_receipts_bind_rule_revisions_and_refuse_conflicting_ones():
    from inqtrix.agents.context_sources import merge_source_receipts

    # Control: receipts without rules keep the shape they always had.
    assert "rule_revisions" not in merge_source_receipts([{"owner_origin_run_ids": ["old"]}])
    merged = merge_source_receipts([{"rule_revisions": {"pt": 1}}, {"rule_revisions": {"pt": 1, "pq": 2}}])
    assert merged["rule_revisions"] == {"pt": 1, "pq": 2}
    with pytest.raises(ValueError, match="conflicting rule versions"):
        merge_source_receipts([{"rule_revisions": {"pt": 1}}, {"rule_revisions": {"pt": 2}}])


@pytest.mark.asyncio
async def test_an_earlier_approval_names_its_rules_without_their_bodies(sources):
    """The kernel reads earlier decisions of the conversation as sources.
    A plan-gate decision stores the composed requirement with the full
    rule bodies for the mission's own resume; a reader gets the parts and
    the typed text instead, like the approvals API."""
    import json

    from inqtrix.agents.control_ports import ApprovalRecord

    s = await saved(sources)
    parts = {"free_text": "Kurz.", "rules": [{"template_id": "pt_rule", "label": "briefing", "revision": 2}]}
    s.control._approvals["apr_gate"] = ApprovalRecord(
        approval_id="apr_gate", run_id="old", kind="plan", status="approved", decision="approve",
        decision_payload={
            "report_guidance": "[Rule: briefing]\nGeheimer Regeltext.\n[End rule: briefing]\n\n[Free requirement]\nKurz.",
            "report_requirement": parts,
        },
        created_at=5,
    )
    result = await s.service.read(ContextReadInput(source_id=encode_source("approval", "old", "apr_gate", "decision")),
                                  s.context)
    assert json.loads(result.text)["decision_payload"] == {"report_requirement": parts}
    assert "Geheimer Regeltext." not in result.text
    # A decision without parts hands out no composed text either.
    s.control._approvals["apr_typed"] = ApprovalRecord(
        approval_id="apr_typed", run_id="old", kind="plan", status="approved", decision="approve",
        decision_payload={"report_guidance": "Als Tabelle."}, created_at=6,
    )
    typed = await s.service.read(ContextReadInput(source_id=encode_source("approval", "old", "apr_typed", "decision")),
                                 s.context)
    assert json.loads(typed.text)["decision_payload"] == {}
