"""Execution originals and source receipts survive cleanup and compaction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from inqtrix.agents.context_sources import SOURCE_RECEIPT
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.kernel.transcript import archive_checkpoint
from inqtrix.capabilities import CapabilityContext, CapabilityError
from inqtrix.exceptions import AgentContextArchiveError
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.settings import AgentPlatformSettings


def dependencies():
    return KernelDeps(run_id="transcript_run", control=MemoryAgentControlStore(),
        platform=AgentPlatformSettings(), llm=None, model=None, reasoning_effort=None, timeout=10,
        capability_context=CapabilityContext(principal=None, run_id="transcript_run"))


def snapshot(messages):
    return SimpleNamespace(values={"messages": messages, "todos": [{"content": "Keep task", "status": "pending"}],
                                   "kernel_usage": {"prompt_tokens": 15}},
                           config={"configurable": {"checkpoint_id": "checkpoint-version"}})


def test_checkpoint_keeps_structured_calls_invalid_arguments_and_exact_tool_body():
    deps = dependencies()
    messages = [HumanMessage(content="Bitte beachte ä🙂e\u0301."),
        AIMessage(content="", tool_calls=[{"id": "valid", "name": "search_context", "args": {"query": "Budget"}}],
                  invalid_tool_calls=[{"id": "invalid", "name": "read_context", "args": "{", "error": "Incomplete arguments"}]),
        ToolMessage(content=" \r\nExact result\t ", tool_call_id="valid", name="search_context",
                    artifact={SOURCE_RECEIPT: {"owner_origin_run_ids": ["earlier"]}})]
    state = snapshot(messages)
    source_id = archive_checkpoint(deps, state, None)
    window = asyncio.run(deps.control.read_context_archive(deps.run_id, source_id, include_private=True))
    body = json.loads(window.text)
    assert body["messages"] == [message.model_dump(mode="json") for message in messages]
    assert body["coverage"] == "checkpoint_messages"
    assert body["todos"] == [{"content": "Keep task", "status": "pending"}]
    assert body["usage"] == {"prompt_tokens": 15}
    record, _ = asyncio.run(deps.control.get_artifact(deps.run_id, source_id, include_private=True))
    assert record.payload["kernel_transcript"]["owner_origin_run_ids"] == ["earlier"]
    assert archive_checkpoint(deps, state, None) == source_id
    assert len(deps.control._artifacts) == 1


@pytest.mark.parametrize("failure", ["store", "state"])
def test_failed_transcript_capture_is_loud_and_preserves_checkpoint(failure, caplog):
    deps = dependencies()
    state = snapshot([HumanMessage(content="Original"), AIMessage(content="Answer")])
    before = list(state.values["messages"])
    def broken_state():
        raise ConnectionError("Checkpoint unavailable")
    async def broken_store(**kwargs):
        raise ConnectionError("Archive unavailable")
    if failure == "store":
        deps.control.put_context_archive = broken_store
    else:
        state_reader = broken_state
    state_reader = state if failure == "store" else state_reader
    with pytest.raises(AgentContextArchiveError, match="checkpoint was retained"):
        archive_checkpoint(deps, state_reader, None)
    assert archive_checkpoint(deps, state_reader, None, required=False) == ""
    assert "checkpoint was retained" in caplog.text
    assert state.values["messages"] == before


@pytest.mark.parametrize("location", ["tool_message", "compacted_state", "canvas_message"])
def test_source_authority_is_restored_and_checked_after_resume(location):
    deps = dependencies()
    receipt = {"owner_origin_run_ids": ["old_run"]}
    state = snapshot([])
    if location == "compacted_state":
        state.values[SOURCE_RECEIPT] = receipt
    else:
        state.values["messages"] = [ToolMessage(content="Historical data", tool_call_id="read",
            name="read_canvas" if location == "canvas_message" else "read_context",
            artifact={SOURCE_RECEIPT: receipt})]
    deps.hydrate_source_receipts(state)
    class SourceService:
        revoked = False
        calls = 0
        async def validate_receipt(self, context, actual):
            self.calls += 1
            assert actual["owner_origin_run_ids"] == ["old_run"]
            if self.revoked:
                raise CapabilityError("context.not_found", "Source unavailable")
    service = SourceService()
    deps.source_service = service
    deps.check_abort()
    service.revoked = True
    with pytest.raises(AuthorizationRevoked):
        deps.check_abort()
    assert service.calls == 2


def test_terminal_archive_failure_leaves_real_kernel_checkpoint(monkeypatch):
    from tests.agents.test_kernel_walking_skeleton import ScriptedToolLLM, _answer_turn, wait_status

    from tests.agents.test_kernel_read_tools_policy import make_client

    client = make_client(ScriptedToolLLM([_answer_turn()]), None)
    async def broken_store(**kwargs):
        raise ConnectionError("Archive unavailable")
    with client:
        monkeypatch.setattr(client.container.agent_control_service.store, "put_context_archive", broken_store)
        run_id = client.post("/v1/runs", json={"question": "Prüfe die Daten.", "mode": "agent_kernel"}).json()["run_id"]
        wait_status(client, run_id, {"failed"})
        handle = client.container.agent_checkpointer
        assert handle.saver().get_tuple({"configurable": {"thread_id": run_id}}) is not None
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        assert not any(item["kind"] == "answer" for item in artifacts)
    set_kernel_deps(None)


def test_native_run_delete_keeps_anchor_on_cache_failure_and_purges_on_retry(monkeypatch):
    from tests.agents.test_kernel_walking_skeleton import ScriptedToolLLM, _answer_turn, wait_status
    from tests.agents.test_kernel_read_tools_policy import make_client

    client = make_client(ScriptedToolLLM([_answer_turn()]), None)
    async def broken_archive(**kwargs):
        raise ConnectionError("Archive unavailable")
    with client:
        monkeypatch.setattr(client.container.agent_control_service.store, "put_context_archive", broken_archive)
        run_id = client.post("/v1/runs", json={"question": "Prüfe die Daten.", "mode": "agent_kernel"}).json()["run_id"]
        wait_status(client, run_id, {"failed"})
        handle = client.container.agent_checkpointer
        original = handle.delete_thread_strict
        def unavailable(_run_id):
            raise ConnectionError("Checkpoint store unavailable")
        monkeypatch.setattr(handle, "delete_thread_strict", unavailable)
        response = client.delete(f"/v1/runs/{run_id}")
        assert response.status_code == 503 and response.json()["error"]["type"] == "run_cleanup_failed"
        assert client.get(f"/v1/runs/{run_id}").status_code == 200
        assert handle.saver().get_tuple({"configurable": {"thread_id": run_id}}) is not None
        monkeypatch.setattr(handle, "delete_thread_strict", original)
        assert client.delete(f"/v1/runs/{run_id}").status_code == 204
        assert client.get(f"/v1/runs/{run_id}").status_code == 404
        assert handle.saver().get_tuple({"configurable": {"thread_id": run_id}}) is None


def test_transcript_descriptor_names_materials_from_producing_tools_only():
    """Documents a run transferred become named dependencies; relayed text never does."""
    from inqtrix.agents.context_sources import MATERIAL_DEPENDENCIES

    knowledge = {MATERIAL_DEPENDENCIES: {"knowledge_documents": [{"document_id": "kd1", "collection_id": "coll"}]}}
    editor = {MATERIAL_DEPENDENCIES: {"editor_documents": [{"document_id": "ed1", "comment_ids": ["c2", "c1"]}]}}
    relayed = {MATERIAL_DEPENDENCIES: {"knowledge_documents": [{"document_id": "kd_relayed", "collection_id": "coll"}]}}
    messages = [
        HumanMessage(content="Frage"),
        AIMessage(content="", tool_calls=[
            {"id": "k", "name": "read_project_document", "args": {}},
            {"id": "e", "name": "read_editor_document", "args": {}},
            {"id": "w", "name": "web_instant", "args": {}},
        ]),
        ToolMessage(content="window", tool_call_id="k", name="read_project_document", artifact=knowledge),
        ToolMessage(content="editor window", tool_call_id="e", name="read_editor_document", artifact=editor),
        ToolMessage(content="web answer", tool_call_id="w", name="web_instant", artifact=relayed),
    ]
    deps = dependencies()
    source_id = archive_checkpoint(deps, snapshot(messages), None)
    record, _ = asyncio.run(deps.control.get_artifact(deps.run_id, source_id, include_private=True))
    descriptor = record.payload["kernel_transcript"]
    assert descriptor["materials"] == {
        "knowledge_documents": [{"document_id": "kd1", "collection_id": "coll"}],
        "editor_documents": [{"document_id": "ed1", "comment_ids": ["c1", "c2"]}],
    }
    assert "coll" in descriptor["collection_ids"]
    # Reading documents does not lock the transcript; only raw file or
    # image inputs do.
    assert descriptor["restricted_inputs"] is False
    locked = dependencies()
    locked.context_has_external_inputs = True
    locked_id = archive_checkpoint(locked, snapshot(messages), None)
    locked_record, _ = asyncio.run(locked.control.get_artifact(locked.run_id, locked_id, include_private=True))
    assert locked_record.payload["kernel_transcript"]["restricted_inputs"] is True


def test_transcript_descriptor_binds_rule_revisions_like_skills():
    """A later run of the conversation may read this transcript, and the
    transcript holds the full text of every rule the run worked under —
    its own and any it read from an earlier transcript. Both are bound
    by revision, exactly like skills."""
    deps = dependencies()
    deps.report_rules = (
        {"template_id": "pt_own", "label": "briefing", "revision": 2},
    )
    messages = [
        HumanMessage(content="Frage"),
        AIMessage(content="", tool_calls=[{"id": "r", "name": "read_context", "args": {}}]),
        ToolMessage(content="Earlier transcript", tool_call_id="r", name="read_context",
                    artifact={SOURCE_RECEIPT: {"rule_revisions": {"pt_earlier": 5}}}),
    ]
    source_id = archive_checkpoint(deps, snapshot(messages), None)
    record, _ = asyncio.run(deps.control.get_artifact(deps.run_id, source_id, include_private=True))
    assert record.payload["kernel_transcript"]["rule_revisions"] == {"pt_own": 2, "pt_earlier": 5}
    # Control: a run without rules writes the descriptor it always wrote.
    plain = dependencies()
    plain_id = archive_checkpoint(plain, snapshot([HumanMessage(content="Frage")]), None)
    plain_record, _ = asyncio.run(plain.control.get_artifact(plain.run_id, plain_id, include_private=True))
    assert "rule_revisions" not in plain_record.payload["kernel_transcript"]


def test_a_kernel_run_binds_its_attached_rule_in_its_transcript():
    """End to end: the parts the router resolved reach the kernel's
    dependencies and from there the archived transcript; the source
    service that later authorizes it can see the template catalog."""
    from tests.agents.test_kernel_walking_skeleton import ScriptedToolLLM, _answer_turn, wait_status
    from tests.agents.test_kernel_read_tools_policy import make_client

    client = make_client(ScriptedToolLLM([_answer_turn()]), None)
    with client:
        container = client.container
        assert container.agent_source_service.prompt_templates is container.prompt_template_service
        record = asyncio.run(container.prompt_template_service.create(
            {"title": "Briefing", "label": "briefing", "category": "instruction",
             "content_markdown": "Gliedere klar.", "visibility": {"agent": True}},
            tenant_id="default", owner_user_id=None,
        ))
        response = client.post("/v1/runs", json={
            "question": "Pruefe die Daten.", "mode": "agent_kernel", "report_rule_ids": [record.id],
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        descriptors = [
            artifact.payload["kernel_transcript"]
            for artifact in container.agent_control_service.store._artifacts.values()
            if artifact.run_id == run_id and "kernel_transcript" in artifact.payload
        ]
        assert descriptors, "the run archived no transcript"
        assert all(item["rule_revisions"] == {record.id: 1} for item in descriptors)
    set_kernel_deps(None)
