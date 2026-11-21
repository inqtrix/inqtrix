"""Kernel proposals to editor documents in collaboration mode.

A document in collaboration mode accepts no agent proposal. The run learns
the mode from its own read: both editor read tools name it in their result
and record it in the artifact of their tool message. The patch gate's
predicate keeps the approval card away from a proposal to such a document,
and the tool body refuses it without calling the capability. A resumed
segment rebuilds the mode from that artifact only, so it judges a proposal
the way the segment that read did. Markdown documents keep the card and the
private patch.

The memory stack refuses a collaboration document at the projection barrier
(it runs no collaboration service), so these runs read through the real
editor read capabilities bound to a small document double, and a counting
double stands in for ``editor.patch.propose``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")

from fastapi.testclient import TestClient

from inqtrix.agents.context_sources import MATERIAL_DEPENDENCIES
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.kernel.deps import KernelDeps, kernel_deps, set_kernel_deps
from inqtrix.agents.kernel.policy import interrupt_config_for
from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.capabilities.catalog.editor import (
    PatchProposeInput,
    PatchProposeOutput,
    build_editor_capabilities,
)
from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    Effect,
)
from inqtrix.capabilities.registry import CapabilityRegistry
from inqtrix.project.editor_ports import DocumentNotFound, EditorDocument
from inqtrix.providers.base import ChatTurn, ToolCallRequest
from inqtrix.settings import AgentPlatformSettings

from tests.agents.test_kernel_read_tools_policy import (
    RecordingSearch,
    ScriptedToolLLM,
    _text_turn,
    _tool_turn,
    make_client,
)

NOTICE = (
    "Documents in collaboration mode do not accept agent proposals; "
    "describe the change in your answer instead."
)
"""The one wording, spelled out here so a changed text is a visible change."""

REFUSAL = (
    "Tool error (editor.collaboration_document): " + NOTICE
    + " Mention this restriction in the answer."
)

_EDITS = [{"position": "replace", "find": "Kennwert", "text": "Messwert"}]


@pytest.fixture(autouse=True)
def _clear_deps():
    yield
    set_kernel_deps(None)


# -- doubles ------------------------------------------------------------------ #


def _documents() -> dict[str, EditorDocument]:
    return {
        "ed_collab": EditorDocument(
            id="ed_collab",
            title="Gemeinsamer Bericht",
            content_markdown="Gemeinsamer Absatz mit Kennwert.",
            revision=5,
            content_mode="collaboration",
            collaboration_generation=2,
            collaboration_schema_version=1,
            collaboration_schema_hash="a" * 64,
            persisted_sequence=9,
            projection_sequence=9,
        ),
        "ed_md": EditorDocument(
            id="ed_md",
            title="Eigener Bericht",
            content_markdown="Eigener Absatz mit Kennwert.",
            revision=3,
        ),
    }


class _ModeDocuments:
    """The document side the real editor read capabilities are bound to."""

    def __init__(self) -> None:
        self.documents = _documents()

    async def get_document_context(
        self,
        document_id: str,
        *,
        visible_to: Any,
        comment_limit: int,
        comment_after: Any,
        include_comments: bool,
    ) -> tuple[EditorDocument, list[Any], str | None]:
        del visible_to, comment_limit, comment_after, include_comments
        return self._document(document_id), [], None

    async def get_document_for_ai(
        self, document_id: str, *, visible_to: Any, minimum: Any = None
    ) -> EditorDocument:
        del visible_to, minimum
        return self._document(document_id)

    def _document(self, document_id: str) -> EditorDocument:
        try:
            return self.documents[document_id]
        except KeyError:
            raise DocumentNotFound(document_id) from None


def _counting_proposals(calls: list[dict[str, Any]]) -> CapabilityDefinition:
    """``editor.patch.propose`` that only counts: every call is a proposal."""

    async def _handler(
        payload: PatchProposeInput, context: CapabilityContext
    ) -> PatchProposeOutput:
        del context
        calls.append(payload.model_dump())
        return PatchProposeOutput(
            patch_id="pch_counted",
            document_id=payload.document_id,
            status="pending",
            edit_count=len(payload.edits),
            revision_before=payload.expected_revision or 1,
        )

    return CapabilityDefinition(
        id="editor.patch.propose",
        summary="Count proposals.",
        input_model=PatchProposeInput,
        output_model=PatchProposeOutput,
        effect=Effect.WRITE,
        idempotent=False,
        handler=_handler,
    )


def _install_doubles(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Bind the run's editor readers to the document double, count proposals."""
    registry = client.container.capability_registry  # type: ignore[attr-defined]
    for definition in build_editor_capabilities(_ModeDocuments()):  # type: ignore[arg-type]
        monkeypatch.setitem(registry._items, definition.id, definition)
    proposals: list[dict[str, Any]] = []
    monkeypatch.setitem(
        registry._items, "editor.patch.propose", _counting_proposals(proposals)
    )
    return proposals


# -- run helpers -------------------------------------------------------------- #


def _turn(*calls: ToolCallRequest) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=calls,
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _propose(call_id: str, document_id: str = "ed_collab") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="propose_editor_patch",
        arguments={
            "document_id": document_id,
            "edits": _EDITS,
            "summary": "Begriff ersetzen",
        },
    )


def _submit(client: TestClient, *, autonomy: str, document_id: str = "") -> str:
    body: dict[str, Any] = {
        "question": "Ersetze den Kennwert im Bericht.",
        "mode": "agent_kernel",
        "autonomy": autonomy,
    }
    if document_id:
        body["document_id"] = document_id
    response = client.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def _approvals(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return client.get(f"/v1/runs/{run_id}/approvals").json()["data"]


def _settle(
    client: TestClient, run_id: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Wait until the run ends or parks on a card nobody decided yet."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        status = summary["status"]
        if status in {"completed", "failed", "cancelled", "waiting_for_input"}:
            return summary
        if status == "waiting_for_approval" and any(
            row["status"] == "pending" for row in _approvals(client, run_id)
        ):
            return summary
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never settled")


def _pending(client: TestClient, run_id: str) -> dict[str, Any]:
    summary = _settle(client, run_id)
    assert summary["status"] == "waiting_for_approval", summary
    pending = [row for row in _approvals(client, run_id) if row["status"] == "pending"]
    assert len(pending) == 1, pending
    return pending[0]


def _decide(
    client: TestClient, run_id: str, approval: dict[str, Any], body: dict[str, Any]
) -> None:
    response = client.post(
        f"/v1/runs/{run_id}/approvals/{approval['approval_id']}", json=body
    )
    assert response.status_code == 200, response.text


def _actions(approval: dict[str, Any]) -> list[str]:
    return [action["tool"] for action in approval["payload"]["actions"]]


def _reply(llm: ScriptedToolLLM, call_id: str) -> str:
    """What the model was told about one tool call."""
    for chat in reversed(llm.chat_calls):
        for message in chat["messages"]:
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                return str(message["content"])
    raise AssertionError(f"the model never saw a result of {call_id}")


# -- the gate predicate ------------------------------------------------------- #


def _gate_call(args: Any) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": "propose_editor_patch", "args": args, "id": "call_gate"}
    )


@pytest.mark.parametrize("autonomy", ["strict", "balanced", "autonomous"])
def test_patch_gate_predicate_skips_only_a_read_collaboration_document(
    autonomy: str,
) -> None:
    config = interrupt_config_for(autonomy)["propose_editor_patch"]
    assert config["allowed_decisions"] == ["approve", "edit", "reject"]
    # The skipped call has no effect, so no user-triggerable path is
    # ungated: the gate stays classified as always gated.
    assert "user_conditional" not in config
    when = config["when"]
    # Without a segment context the gate fires.
    assert when(_gate_call({"document_id": "ed_collab"})) is True
    set_kernel_deps(
        SimpleNamespace(
            editor_read_modes={"ed_collab": "collaboration", "ed_md": "markdown"},
            tool_grants=frozenset(),
        )
    )
    assert when(_gate_call({"document_id": "ed_collab"})) is False
    assert when(_gate_call({"document_id": "ed_md"})) is True
    assert when(_gate_call({"document_id": "ed_unread"})) is True
    assert when(_gate_call({"document_id": ""})) is True
    assert when(_gate_call({"document_id": ["ed_collab"]})) is True
    assert when(_gate_call({})) is True
    # A run-wide grant of the tool never frees a proposal.
    set_kernel_deps(
        SimpleNamespace(
            editor_read_modes={}, tool_grants=frozenset({"propose_editor_patch"})
        )
    )
    assert when(_gate_call({"document_id": "ed_md"})) is True


# -- what a read tells the model, and what it records ------------------------- #


def _tool_deps(registry: CapabilityRegistry) -> KernelDeps:
    deps = KernelDeps(
        run_id="run_editor_modes",
        session_id="session_editor_modes",
        control=MemoryAgentControlStore(),
        platform=AgentPlatformSettings(),
        llm=None,
        model=None,
        reasoning_effort=None,
        timeout=1,
        capability_registry=registry,
        capability_context=CapabilityContext(principal=None),
    )
    set_kernel_deps(deps)
    return deps


def _call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"type": "tool_call", "name": name, "args": arguments, "id": call_id}


def _head(content: str) -> str:
    """The part of a tool result before its first fence of document data."""
    return content.split("<untrusted_content", 1)[0]


def test_both_readers_name_the_mode_outside_the_data_and_record_it() -> None:
    registry = CapabilityRegistry()
    registry.register_all(build_editor_capabilities(_ModeDocuments()))  # type: ignore[arg-type]
    deps = _tool_deps(registry)
    tools = {tool.name: tool for tool in build_kernel_tools()}

    read = tools["read_editor_document"].invoke(
        _call("read_editor_document", {"document_id": "ed_collab"}, "call_read")
    )
    search = tools["search_editor_document"].invoke(
        _call(
            "search_editor_document",
            {"document_id": "ed_collab", "query": "Kennwert"},
            "call_search",
        )
    )
    markdown = tools["read_editor_document"].invoke(
        _call("read_editor_document", {"document_id": "ed_md"}, "call_markdown")
    )

    assert read.content.startswith(
        "[editor_read:ed_collab@5]\ndocument_id: ed_collab\nrevision: 5\n"
        "content_mode: collaboration\n" + NOTICE + "\n"
    )
    assert search.content.startswith(
        "[editor_read:ed_collab@5]\ncontent_mode: collaboration\n" + NOTICE + "\n"
    )
    assert "content_mode: markdown\n" in _head(markdown.content)
    assert NOTICE not in markdown.content
    # The record the receipt reconstruction trusts, next to the coverage.
    for result in (read, search):
        assert result.artifact["editor_read"] == {
            "document_id": "ed_collab",
            "revision": 5,
            "content_mode": "collaboration",
        }
    assert markdown.artifact["editor_read"] == {
        "document_id": "ed_md",
        "revision": 3,
        "content_mode": "markdown",
    }
    assert deps.editor_read_receipts == {"ed_collab": 5, "ed_md": 3}
    assert deps.editor_read_modes == {
        "ed_collab": "collaboration",
        "ed_md": "markdown",
    }


def test_the_tool_body_refuses_a_read_collaboration_document_without_a_proposal() -> None:
    registry = CapabilityRegistry()
    registry.register_all(build_editor_capabilities(_ModeDocuments()))  # type: ignore[arg-type]
    proposals: list[dict[str, Any]] = []
    registry.register(_counting_proposals(proposals))
    _tool_deps(registry)
    tools = {tool.name: tool for tool in build_kernel_tools()}

    tools["read_editor_document"].invoke(
        _call("read_editor_document", {"document_id": "ed_collab"}, "call_read")
    )
    refused = tools["propose_editor_patch"].invoke(
        _call(
            "propose_editor_patch",
            {"document_id": "ed_collab", "edits": _EDITS},
            "call_refused",
        )
    )
    assert refused.content == REFUSAL
    assert proposals == []
    # Like every other refusal that reached the document, it names it.
    assert refused.artifact[MATERIAL_DEPENDENCIES] == {
        "editor_documents": [{"document_id": "ed_collab", "comment_ids": []}]
    }

    tools["read_editor_document"].invoke(
        _call("read_editor_document", {"document_id": "ed_md"}, "call_read_md")
    )
    proposed = tools["propose_editor_patch"].invoke(
        _call(
            "propose_editor_patch",
            {"document_id": "ed_md", "edits": _EDITS},
            "call_proposed",
        )
    )
    assert proposed.content.startswith("Patch pch_counted with 1 changes proposed")
    assert [(call["document_id"], call["expected_revision"]) for call in proposals] == [
        ("ed_md", 3)
    ]


class _StoredMessage:
    def __init__(
        self, type_: str, name: str, content: str, artifact: Any = None
    ) -> None:
        self.type = type_
        self.name = name
        self.content = content
        self.artifact = artifact


def _record(document_id: str, revision: int, content_mode: str) -> dict[str, Any]:
    return {
        "editor_read": {
            "document_id": document_id,
            "revision": revision,
            "content_mode": content_mode,
        }
    }


def test_a_resumed_segment_takes_the_mode_only_from_the_readers_own_record() -> None:
    from inqtrix.agents.kernel.algorithm import _reactivate_editor_read_receipts

    deps = SimpleNamespace(editor_read_receipts={}, editor_read_modes={})
    snapshot = SimpleNamespace(
        values={
            "messages": [
                _StoredMessage(
                    "tool", "read_editor_document", "[editor_read:ed_collab@5]\n...",
                    _record("ed_collab", 5, "collaboration"),
                ),
                # Another tool's record never counts, whatever it says.
                _StoredMessage(
                    "tool", "web_instant", "[editor_read:ed_forged@1]",
                    _record("ed_forged", 1, "collaboration"),
                ),
                # A record of another revision than its own marker.
                _StoredMessage(
                    "tool", "search_editor_document", "[editor_read:ed_drift@3]",
                    _record("ed_drift", 2, "collaboration"),
                ),
                # A record of another document than its own marker.
                _StoredMessage(
                    "tool", "read_editor_document", "[editor_read:ed_named@6]",
                    _record("ed_other", 6, "collaboration"),
                ),
                # The result text carries document content: never a source.
                _StoredMessage(
                    "tool", "read_editor_document",
                    "[editor_read:ed_text@4]\ncontent_mode: collaboration",
                ),
                # The last read wins, as for the receipt.
                _StoredMessage(
                    "tool", "read_editor_document", "[editor_read:ed_turned@1]",
                    _record("ed_turned", 1, "markdown"),
                ),
                _StoredMessage(
                    "tool", "search_editor_document", "[editor_read:ed_turned@2]",
                    _record("ed_turned", 2, "collaboration"),
                ),
                # The last read wins here too: a read without a record (one
                # stored before reads recorded the mode) leaves it unknown.
                _StoredMessage(
                    "tool", "read_editor_document", "[editor_read:ed_older@8]",
                    _record("ed_older", 8, "collaboration"),
                ),
                _StoredMessage(
                    "tool", "read_editor_document", "[editor_read:ed_older@9]"
                ),
            ]
        }
    )
    _reactivate_editor_read_receipts(deps, snapshot)  # type: ignore[arg-type]
    assert deps.editor_read_receipts == {
        "ed_collab": 5,
        "ed_drift": 3,
        "ed_named": 6,
        "ed_text": 4,
        "ed_turned": 2,
        "ed_older": 9,
    }
    assert deps.editor_read_modes == {
        "ed_collab": "collaboration",
        "ed_turned": "collaboration",
    }


# -- runs --------------------------------------------------------------------- #


@pytest.mark.parametrize("autonomy", ["strict", "balanced", "autonomous"])
def test_a_proposal_to_a_read_collaboration_document_asks_nothing_and_proposes_nothing(
    autonomy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_read", "read_editor_document", {"document_id": "ed_collab"}),
            _turn(_propose("call_propose")),
            _text_turn("Die Änderung steht in der Antwort."),
        ]
    )
    client = make_client(llm, RecordingSearch())
    proposals = _install_doubles(client, monkeypatch)
    with client:
        run_id = _submit(client, autonomy=autonomy, document_id="ed_collab")
        if autonomy == "strict":
            # Strict reviews the read itself; that is the run's only card.
            read_card = _pending(client, run_id)
            assert _actions(read_card) == ["read_editor_document"]
            _decide(client, run_id, read_card, {"decision": "approve"})
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        cards = _approvals(client, run_id)
        assert [_actions(card) for card in cards] == (
            [["read_editor_document"]] if autonomy == "strict" else []
        )
        assert proposals == []
        assert _reply(llm, "call_propose") == REFUSAL
        # The target sentence names the exception the read result can raise.
        first_user = [
            message["content"]
            for message in llm.chat_calls[0]["messages"]
            if message.get("role") == "user"
        ]
        assert any(
            "document_id=ed_collab" in text
            and "unless the read result says the document is in collaboration mode"
            in text
            for text in first_user
        )


def test_a_resumed_segment_judges_the_proposal_like_the_segment_that_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_read", "read_editor_document", {"document_id": "ed_collab"}),
            _tool_turn("call_web", "web_instant", {"query": "Kennwert Definition"}),
            _turn(_propose("call_propose")),
            _text_turn("Die Änderung steht in der Antwort."),
        ]
    )
    search = RecordingSearch()
    client = make_client(llm, search)
    proposals = _install_doubles(client, monkeypatch)
    with client:
        run_id = _submit(client, autonomy="balanced")
        web_card = _pending(client, run_id)
        assert _actions(web_card) == ["web_instant"]
        _decide(client, run_id, web_card, {"decision": "approve"})
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        assert [_actions(card) for card in _approvals(client, run_id)] == [
            ["web_instant"]
        ]
        assert search.queries == ["Kennwert Definition"]
        assert proposals == []
        assert _reply(llm, "call_propose") == REFUSAL


def test_a_mixed_turn_parks_one_card_with_exactly_the_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_read", "read_editor_document", {"document_id": "ed_collab"}),
            _turn(
                _propose("call_propose"),
                ToolCallRequest(
                    id="call_web",
                    name="web_instant",
                    arguments={"query": "Kennwert Definition"},
                ),
            ),
            _text_turn("Die Änderung steht in der Antwort."),
        ]
    )
    search = RecordingSearch()
    client = make_client(llm, search)
    proposals = _install_doubles(client, monkeypatch)
    with client:
        run_id = _submit(client, autonomy="balanced")
        card = _pending(client, run_id)
        assert _actions(card) == ["web_instant"]
        assert card["payload"]["actions"][0]["args"] == {"query": "Kennwert Definition"}
        # The resumed segment must count the same one gated call.
        _decide(client, run_id, card, {"decision": "approve"})
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        assert len(_approvals(client, run_id)) == 1
        assert search.queries == ["Kennwert Definition"]
        assert proposals == []
        assert _reply(llm, "call_propose") == REFUSAL


def test_a_read_and_a_proposal_in_one_turn_keep_the_card_and_never_propose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inqtrix.agents.kernel import tools as kernel_tools

    allowed = kernel_tools._require_allowed
    waited: list[bool] = []

    def _after_the_read(tool_name: str) -> str | None:
        # The tools of one turn run side by side. The proposal waits for the
        # read of its own turn: in that order a missing mode check would reach
        # the capability, so the order is the one worth pinning.
        if tool_name == "propose_editor_patch":
            deadline = time.monotonic() + 5.0
            while "ed_collab" not in kernel_deps().editor_read_receipts:
                if time.monotonic() > deadline:
                    waited.append(False)
                    return allowed(tool_name)
                time.sleep(0.01)
            waited.append(True)
        return allowed(tool_name)

    monkeypatch.setattr(kernel_tools, "_require_allowed", _after_the_read)
    llm = ScriptedToolLLM(
        [
            _turn(
                ToolCallRequest(
                    id="call_read",
                    name="read_editor_document",
                    arguments={"document_id": "ed_collab"},
                ),
                _propose("call_propose"),
            ),
            _text_turn("Die Änderung steht in der Antwort."),
        ]
    )
    client = make_client(llm, RecordingSearch())
    proposals = _install_doubles(client, monkeypatch)
    with client:
        run_id = _submit(client, autonomy="balanced")
        # Before the read ran, the mode is unknown: the card stays.
        card = _pending(client, run_id)
        assert _actions(card) == ["propose_editor_patch"]
        _decide(client, run_id, card, {"decision": "approve"})
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        assert waited == [True]
        assert proposals == []
        assert _reply(llm, "call_propose") == REFUSAL


def test_an_edit_that_moves_the_proposal_onto_a_collaboration_document_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_read_md", "read_editor_document", {"document_id": "ed_md"}),
            _tool_turn(
                "call_read_collab", "read_editor_document", {"document_id": "ed_collab"}
            ),
            _turn(_propose("call_propose", document_id="ed_md")),
            _text_turn("Erledigt."),
        ]
    )
    client = make_client(llm, RecordingSearch())
    proposals = _install_doubles(client, monkeypatch)
    with client:
        run_id = _submit(client, autonomy="autonomous")
        card = _pending(client, run_id)
        assert _actions(card) == ["propose_editor_patch"]
        assert card["payload"]["actions"][0]["args"]["document_id"] == "ed_md"
        _decide(
            client,
            run_id,
            card,
            {
                "decision": "edit",
                "actions": [
                    {
                        "tool": "propose_editor_patch",
                        "args": {
                            "document_id": "ed_collab",
                            "edits": _EDITS,
                            "summary": "Begriff ersetzen",
                        },
                    }
                ],
            },
        )
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        assert proposals == []
        assert _reply(llm, "call_propose") == REFUSAL


def test_a_markdown_document_keeps_its_card_and_gets_its_private_patch() -> None:
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_read", "read_editor_document", {"document_id": "ed_doc"}),
            _turn(_propose("call_propose", document_id="ed_doc")),
            _text_turn("Vorgeschlagen."),
        ]
    )
    client = make_client(llm, RecordingSearch())
    with client:
        seeded = client.put(
            "/v1/editor/documents/ed_doc",
            json={
                "title": "Bericht",
                "content_markdown": "Erster Absatz mit Kennwert.",
                "source": "blank",
                "revision": 3,
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        assert seeded.status_code == 200, seeded.text
        run_id = _submit(client, autonomy="autonomous", document_id="ed_doc")
        card = _pending(client, run_id)
        assert _actions(card) == ["propose_editor_patch"]
        _decide(client, run_id, card, {"decision": "approve"})
        summary = _settle(client, run_id)
        assert summary["status"] == "completed", summary
        read = _reply(llm, "call_read")
        assert "content_mode: markdown\n" in _head(read)
        assert NOTICE not in read
        assert _reply(llm, "call_propose").startswith("Patch pch_")
        patches = client.get("/v1/editor/documents/ed_doc/patches").json()["data"]
        assert [(patch["source"], patch["status"]) for patch in patches] == [
            ("agent", "pending")
        ]
