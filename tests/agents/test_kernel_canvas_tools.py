"""Kernel canvas tools: write_canvas CAS + always-gated patches.

write_canvas creates/updates session-scoped ``deliverable`` artifacts
with optimistic concurrency (visible conflict text, never a clobber);
``propose_editor_patch`` parks for approval in EVERY mode.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


class ScriptedToolLLM(LLMProvider):
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.models = ModelSettings(
            reasoning_model="base-model",
            tier_high_model="high-model",
            tier_mid_model="mid-model",
            tier_fast_model="fast-model",
        )
        self._turns = list(turns)
        self.chat_calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        raise AssertionError("kernel must use chat()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
        self.chat_calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("scripted provider ran out of turns")
        return self._turns.pop(0)


def _tool_turn(call_id: str, name: str, arguments: dict) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(id=call_id, name=name, arguments=arguments),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _text_turn(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def make_client(llm: ScriptedToolLLM) -> TestClient:
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    settings.agent_platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_KERNEL_ENABLED=True,
    )
    container = register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: __import__("asyncio").Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def seed_artifact(
    client: TestClient, *, run_id: str, kind: str, artifact_id: str
) -> None:
    """One artifact written the way a NON-kernel producer writes it.

    The mission owns its memo; this is the only way to get one into the
    store from a kernel test without running the mission engine. It
    rides an existing run because artifact writes are authorized
    against a live run record.
    """
    asyncio.run(
        client.container.agent_control_service.store.upsert_artifact(
            run_id=run_id,
            kind=kind,
            session_id="sess-canvas",
            title="Memo der Mission",
            status="ready",
            content_markdown="# Memo\n\nLangfassung.",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id=artifact_id,
            expected_revision=0,
        )
    )


def wait_status(
    client: TestClient, run_id: str, statuses: set[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never reached {statuses}")


def _submit(client: TestClient, *, autonomy: str = "autonomous") -> str:
    response = client.post(
        "/v1/runs",
        json={
            "question": "Entwirf eine E-Mail.",
            "mode": "agent_kernel",
            "autonomy": autonomy,
            "session_id": "sess-canvas",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def _artifacts(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    return client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]


def test_write_canvas_creates_then_updates_with_cas():
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_canvas1",
                "write_canvas",
                {
                    "title": "E-Mail-Entwurf",
                    "content_markdown": "# Entwurf V1",
                    "deliverable_kind": "email",
                },
            ),
            # The model updates using id + revision from the tool result.
            None,  # placeholder, replaced below after first result known
        ]
    )
    # Two-phase scripting is impossible without knowing the artifact id;
    # instead: the update turn is generated from the FIRST tool reply by
    # a callable-aware provider.

    class TwoPhase(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append(
                {"messages": list(messages), "tools": tools}
            )
            turn_index = len(self.chat_calls)
            if turn_index == 1:
                return _tool_turn(
                    "call_canvas1",
                    "write_canvas",
                    {
                        "title": "E-Mail-Entwurf",
                        "content_markdown": "# Entwurf V1",
                        "deliverable_kind": "email",
                    },
                )
            if turn_index == 2:
                reply = [
                    m for m in messages if m.get("role") == "tool"
                ][-1]["content"]
                artifact_id = reply.split("artifact_id ")[1].split(",")[0]
                return _tool_turn(
                    "call_canvas2",
                    "write_canvas",
                    {
                        "title": "E-Mail-Entwurf",
                        "content_markdown": "# Entwurf V2",
                        "deliverable_kind": "email",
                        "artifact_id": artifact_id,
                        "expected_revision": 1,
                    },
                )
            return _text_turn("Entwurf steht im Canvas.")

    llm = TwoPhase([])
    client = make_client(llm)
    with client:
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        rows = _artifacts(client, run_id)
        deliverables = [r for r in rows if r["kind"] == "deliverable"]
        assert len(deliverables) == 1
        row = deliverables[0]
        assert row["revision"] == 2
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{row['artifact_id']}"
        ).json()
        assert detail["content_markdown"] == "# Entwurf V2"
        assert detail["payload"] == {"deliverable_kind": "email"}


def test_write_canvas_normalizes_currency_but_preserves_math_and_code():
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_currency_canvas",
                "write_canvas",
                {
                    "title": "Markt-Memo",
                    "content_markdown": (
                        "Umsatz: US-$1.5T.\n\nFormel: $x$. "
                        "Code: `$raw`."
                    ),
                    "deliverable_kind": "memo",
                },
            ),
            _text_turn("Memo steht im Canvas."),
        ]
    )
    client = make_client(llm)
    with client:
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        row = next(
            item
            for item in _artifacts(client, run_id)
            if item["kind"] == "deliverable"
        )
        detail = client.get(
            f"/v1/runs/{run_id}/artifacts/{row['artifact_id']}"
        ).json()

    assert detail["content_markdown"] == (
        "Umsatz: US-\\$1.5T.\n\nFormel: $x$. Code: `$raw`."
    )


def test_write_canvas_stale_revision_is_a_visible_conflict():
    class StaleUpdate(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append(
                {"messages": list(messages), "tools": tools}
            )
            turn_index = len(self.chat_calls)
            if turn_index == 1:
                return _tool_turn(
                    "call_c1",
                    "write_canvas",
                    {
                        "title": "Memo",
                        "content_markdown": "# V1",
                        "deliverable_kind": "memo",
                    },
                )
            if turn_index == 2:
                reply = [
                    m for m in messages if m.get("role") == "tool"
                ][-1]["content"]
                artifact_id = reply.split("artifact_id ")[1].split(",")[0]
                return _tool_turn(
                    "call_c2",
                    "write_canvas",
                    {
                        "title": "Memo",
                        "content_markdown": "# stale",
                        "deliverable_kind": "memo",
                        "artifact_id": artifact_id,
                        "expected_revision": 7,
                    },
                )
            return _text_turn("Konflikt erkannt.")

    llm = StaleUpdate([])
    client = make_client(llm)
    with client:
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        rows = [
            r
            for r in _artifacts(client, run_id)
            if r["kind"] == "deliverable"
        ]
        # The stale write did NOT clobber: still revision 1.
        assert rows[0]["revision"] == 1
        conflict_reply = [
            m
            for m in llm.chat_calls[2]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "Revision conflict" in conflict_reply
        assert "revision 1" in conflict_reply


def test_write_canvas_cannot_update_another_sessions_artifact():
    class CrossSessionAttempt(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append({"messages": list(messages), "tools": tools})
            index = len(self.chat_calls)
            if index == 1:
                return _tool_turn(
                    "call_owner_create",
                    "write_canvas",
                    {"title": "Owner", "content_markdown": "# Original"},
                )
            if index == 2:
                reply = [
                    item for item in messages if item.get("role") == "tool"
                ][-1]["content"]
                self.victim_id = reply.split("artifact_id ")[1].split(",")[0]
                return _text_turn("Owner fertig.")
            if index == 3:
                return _tool_turn(
                    "call_foreign_update",
                    "write_canvas",
                    {
                        "title": "Foreign",
                        "content_markdown": "# Ueberschrieben",
                        "artifact_id": self.victim_id,
                        "expected_revision": 1,
                        "reference_ids": [],
                    },
                )
            return _text_turn("Fremdzugriff abgelehnt.")

    llm = CrossSessionAttempt([])
    client = make_client(llm)
    with client:
        owner_run = _submit(client)
        wait_status(client, owner_run, {"completed"})
        foreign = client.post(
            "/v1/runs",
            json={
                "question": "Versuche ein fremdes Canvas zu aendern.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-foreign",
            },
        )
        assert foreign.status_code == 202, foreign.text
        wait_status(client, foreign.json()["run_id"], {"completed"})

        detail = client.get(
            f"/v1/runs/{owner_run}/artifacts/{llm.victim_id}"
        ).json()
        assert detail["revision"] == 1
        assert detail["content_markdown"] == "# Original"
        reply = [
            item
            for item in llm.chat_calls[3]["messages"]
            if item.get("role") == "tool"
        ][-1]["content"]
        assert "not found" in reply


def test_kernel_continues_a_mission_document():
    """The operator's own workflow: the mission writes the report, the
    kernel refines it in the same session.

    An update path that accepted only ``deliverable`` would end this as a
    silent no-op with a wrong diagnosis: the kernel can READ the mission's
    memo, would answer "nicht gefunden" for a document it had just read,
    and the run would complete as if nothing had failed. From
    the user's side there is ONE document of the session; which engine
    wrote it is machinery.
    """
    memo_id = "art_sess-canvas_memo"

    class ContinueMemo(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append({"messages": list(messages), "tools": tools})
            # Call 1 belongs to the seed run (it only has to finish so a
            # real run record exists); the continuation is call 2.
            if len(self.chat_calls) == 2:
                return _tool_turn(
                    "call_continue",
                    "write_canvas",
                    {
                        "title": "Memo der Mission",
                        "content_markdown": "# Memo\n\nGekuerzte Fassung.",
                        "artifact_id": memo_id,
                        "expected_revision": 1,
                        "reference_ids": [],
                    },
                )
            return _text_turn("Dokument aktualisiert.")

    llm = ContinueMemo([])
    client = make_client(llm)
    with client:
        # A mission-written memo: same session, kind="memo".
        seed_run = _submit(client)
        wait_status(client, seed_run, {"completed"})
        seed_artifact(
            client, run_id=seed_run, kind="memo", artifact_id=memo_id
        )
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        detail = client.get(f"/v1/runs/{run_id}/artifacts/{memo_id}").json()
        assert detail["revision"] == 2, detail
        assert "Gekuerzte Fassung" in detail["content_markdown"]
        # The kind survives: one memo per session, one deliverable per
        # creation — continuing a document must not reclassify it.
        assert detail["kind"] == "memo"


def test_a_document_is_read_with_the_labels_it_uses():
    """Evidence labels belong to the document that cites them.

    A document written in another label space (a mission memo numbers from
    W1, a kernel session numbers on) cites ITS labels. A reader whose ledger
    already owns ``W1`` for a different source must be shown the label the
    text uses next to the global reference id — never only its own ledger
    label, under which the cited ``[W1]`` would resolve to the wrong source.
    """
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
    from inqtrix.agents.kernel.tools import build_kernel_tools

    store = MemoryAgentControlStore()
    asyncio.run(store.upsert_artifact(
        run_id="run_mission", kind="memo", session_id="sess_labels", title="Memo",
        status="ready", content_markdown="# Memo\n\nDie Frist endet am 9. Dezember [W1].",
        payload={}, updated_by="agent", artifact_id="art_labels_memo",
        refs=[{"label": "W1", "url": "https://example.org/frist", "title": "Quelle zur Frist"}],
    ))
    deps = KernelDeps(
        run_id="run_reader", control=store, platform=AgentPlatformSettings(),
        llm=None, model=None, reasoning_effort=None, timeout=1,
    )
    deps.session_id = "sess_labels"
    set_kernel_deps(deps)
    try:
        own = deps.register_references(
            [{"url": "https://example.org/anderes", "title": "Anderes Thema"}]
        )[0]
        assert own["label"] == "W1"
        read_canvas = next(t for t in build_kernel_tools() if t.name == "read_canvas")
        reply = read_canvas.invoke({"artifact_id": "art_labels_memo"})
    finally:
        set_kernel_deps(None)
    cited = next(
        ref for ref in deps.evidence_refs.values() if ref["url"].endswith("/frist")
    )
    # The reader's ledger renumbered the memo's source; the document did not.
    assert cited["label"] == "W2"
    line = next(row for row in reply.splitlines() if cited["reference_id"] in row)
    assert line.startswith("- Evidence [W1] — reference_id: " + cited["reference_id"])
    # The differing index label is named, so an index lookup is not misread.
    assert "[W2]" in line
    # The text itself is returned verbatim: review anchors quote it. It is
    # a document's content, so the reader gets it fenced as data.
    assert "Die Frist endet am 9. Dezember [W1]." in reply
    assert (
        '<untrusted_content source="canvas-document">\n'
        "# Memo\n\nDie Frist endet am 9. Dezember [W1].\n"
        "</untrusted_content>"
    ) in reply
    # A label another source owns in the reader's ledger is never attached
    # to the document's source by the listing.
    assert own["reference_id"] not in line


def test_an_update_keeps_the_document_name_and_says_so():
    """Writing new content is not renaming.

    The update passed the model's title straight through, so any turn
    could silently re-label a file the user had named — the same root
    the mission had. The name changes only through the explicit rename;
    a model that asked for a different one is told, never ignored in
    silence.
    """
    memo_id = "art_sess-canvas_named"

    class RenameByWriting(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append({"messages": list(messages), "tools": tools})
            if len(self.chat_calls) == 2:
                return _tool_turn(
                    "call_rename_attempt",
                    "write_canvas",
                    {
                        "title": "Ein ganz anderer Name",
                        "content_markdown": "# Neu\n\nInhalt.",
                        "artifact_id": memo_id,
                        "expected_revision": 1,
                        "reference_ids": [],
                    },
                )
            return _text_turn("Fertig.")

    llm = RenameByWriting([])
    client = make_client(llm)
    with client:
        seed_run = _submit(client)
        wait_status(client, seed_run, {"completed"})
        seed_artifact(
            client, run_id=seed_run, kind="memo", artifact_id=memo_id
        )
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        detail = client.get(f"/v1/runs/{run_id}/artifacts/{memo_id}").json()
        assert detail["title"] == "Memo der Mission", detail["title"]
        assert "Inhalt." in detail["content_markdown"]
        reply = [
            m
            for m in llm.chat_calls[2]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "The name stays" in reply


def test_write_canvas_refuses_a_non_document_artifact():
    """Machinery is not a file. The refusal must say WHY — the old text
    claimed "nicht gefunden" for artifacts it had found."""
    evidence_id = "art_sess-canvas_evidence"

    class TouchEvidence(ScriptedToolLLM):
        def chat(self, messages, *, tools=None, **kwargs):
            self.chat_calls.append({"messages": list(messages), "tools": tools})
            if len(self.chat_calls) == 2:
                return _tool_turn(
                    "call_evidence",
                    "write_canvas",
                    {
                        "title": "X",
                        "content_markdown": "# X",
                        "artifact_id": evidence_id,
                        "expected_revision": 1,
                        "reference_ids": [],
                    },
                )
            return _text_turn("Abgelehnt.")

    llm = TouchEvidence([])
    client = make_client(llm)
    with client:
        seed_run = _submit(client)
        wait_status(client, seed_run, {"completed"})
        seed_artifact(
            client,
            run_id=seed_run,
            kind="evidence_bundle",
            artifact_id=evidence_id,
        )
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        reply = [
            m
            for m in llm.chat_calls[2]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "is not a document" in reply
        assert "evidence_bundle" in reply
        assert "not found" not in reply


def test_invalid_deliverable_kind_is_a_visible_tool_error():
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_bad",
                "write_canvas",
                {
                    "title": "X",
                    "content_markdown": "# X",
                    "deliverable_kind": "poster",
                },
            ),
            _text_turn("Korrigiert."),
        ]
    )
    client = make_client(llm)
    with client:
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        assert (
            [r for r in _artifacts(client, run_id) if r["kind"] == "deliverable"]
            == []
        )
        reply = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "Unknown document kind" in reply


def test_write_canvas_rejects_unknown_citation_labels_loudly():
    """Citation parity with the mission engine: a canvas text citing
    labels that are not among the ATTACHED references is a visible tool
    error, never a silent write."""
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_uncited",
                "write_canvas",
                {
                    "title": "Memo",
                    "content_markdown": "Behauptung mit Beleg [W1].",
                    "deliverable_kind": "memo",
                },
            ),
            _tool_turn(
                "call_fixed",
                "write_canvas",
                {
                    "title": "Memo",
                    "content_markdown": "Behauptung ohne Beleg-Label.",
                    "deliverable_kind": "memo",
                },
            ),
            _text_turn("Memo steht im Canvas."),
        ]
    )
    client = make_client(llm)
    with client:
        run_id = _submit(client)
        wait_status(client, run_id, {"completed"})
        reply = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "unknown labels" in reply
        assert "W1" in reply
        deliverables = [
            r for r in _artifacts(client, run_id) if r["kind"] == "deliverable"
        ]
        assert len(deliverables) == 1


def test_propose_editor_patch_gates_even_in_autonomous():
    llm = ScriptedToolLLM(
        [
            _tool_turn(
                "call_patch1",
                "propose_editor_patch",
                {
                    "document_id": "doc_1",
                    "edits": [
                        {
                            "position": "replace",
                            "find": "alt",
                            "text": "neu",
                        }
                    ],
                    "summary": "Begriff ersetzen",
                },
            ),
            _text_turn("Patch vorgeschlagen."),
        ]
    )
    client = make_client(llm)
    with client:
        run_id = _submit(client, autonomy="autonomous")
        wait_status(client, run_id, {"waiting_for_approval"})
        rows = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        pending = [r for r in rows if r["status"] == "pending"]
        assert pending[0]["kind"] == "tool"
        action = pending[0]["payload"]["actions"][0]
        assert action["tool"] == "propose_editor_patch"
        assert action["args"]["document_id"] == "doc_1"

        client.post(
            f"/v1/runs/{run_id}/approvals/{pending[0]['approval_id']}",
            json={"decision": "approve"},
        )
        wait_status(client, run_id, {"completed"})
        # After consent the READ-BEFORE-PROPOSE enforcement denies
        # VISIBLY: no receipt for doc_1 in this run, so the proposal is
        # refused before the capability is even invoked.
        reply = [
            m
            for m in llm.chat_calls[1]["messages"]
            if m.get("role") == "tool"
        ][-1]["content"]
        assert "Tool error" in reply
        assert "editor.read_required" in reply
        assert "read_editor_document" in reply


def _seed_report(client, *, question: str, url: str) -> str:
    """Complete the report inside its worker before the run becomes terminal."""
    store = client.container.run_store
    payload = {
        "answer": "## Kurzfazit\n\nDie belegte Kernaussage [E1].\n",
        "references": [{"label": "E1", "url": url, "tier": "primary"}],
    }
    summary = store.submit(
        question=question,
        stack_name="default",
        work=lambda handle: handle.complete(payload),
        mode="research",
        kind="standard",
    )
    run_id = str(summary["run_id"])
    wait_status(client, run_id, {"completed"})
    assert store.result(run_id)["answer"] == payload["answer"]
    return run_id


def test_an_attached_report_becomes_citable_and_a_later_source_is_added():
    """The operator's use case in miniature.

    A report is attached; the kernel reads it, which imports the report's
    sources into THIS run's ledger under kernel labels. The report's own
    E-label is invisible to the kernel's citation check, so the import
    must translate it — and a report the user did NOT attach must stay
    out of reach even though the caller can see it."""
    llm = ScriptedToolLLM([])
    client = make_client(llm)
    with client:
        report_id = _seed_report(
            client,
            question="Stand der EU-Batterieverordnung?",
            url="https://example.org/bericht",
        )
        other_id = _seed_report(
            client,
            question="Nicht angehaengter Bericht?",
            url="https://example.org/zweiter",
        )
        llm._turns = [
            _tool_turn(
                "call_read", "read_research_report", {"report_id": report_id}
            ),
            _tool_turn(
                "call_forbidden",
                "read_research_report",
                {"report_id": other_id},
            ),
            _text_turn("Fertig."),
        ]
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe einen Sprechzettel.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-reports",
                "report_ids": [report_id],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed", "failed"})

        replies = [
            message["content"]
            for call in llm.chat_calls
            for message in call["messages"]
            if message.get("role") == "tool"
        ]
        assert replies, "the report was never read"
        attached_reply = replies[0]
        assert "Stand der EU-Batterieverordnung?" in attached_reply
        # Translated into a kernel label: an [E1] would be invisible to
        # the citation check — neither valid nor reported as invalid.
        # The BODY must cite the new label; the old one may only appear
        # in the translation note, which states the mapping openly.
        body = attached_reply.split("Note:")[0]
        assert "[W1]" in body
        assert "[E1]" not in body
        assert "[E1] -> [W1]" in attached_reply
        # The attachment is the consent, not the visibility.
        refused = [r for r in replies if "is not attached" in r]
        assert refused, f"reading an unattached report was not refused: {replies}"


def test_the_registry_line_reaches_a_real_run():
    """The name must be in the first user message, and the body must NOT."""
    llm = ScriptedToolLLM([])
    client = make_client(llm)
    with client:
        report_id = _seed_report(
            client, question="Batteriebericht?", url="https://example.org/x"
        )
        llm._turns = [_text_turn("Fertig.")]
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe einen Sprechzettel.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-registry",
                "report_ids": [report_id],
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"completed", "failed"})
        user_message = llm.chat_calls[0]["messages"][-1]["content"]
        assert report_id in user_message
        assert "Batteriebericht?" in user_message
        assert "read_research_report" in user_message
        # The body stays out: two real reports are ~107k characters.
        assert "Kurzfazit" not in user_message


def test_an_unknown_report_id_refuses_the_submission():
    llm = ScriptedToolLLM([_text_turn("x")])
    client = make_client(llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe einen Sprechzettel.",
                "mode": "agent_kernel",
                "session_id": "sess-unknown",
                "report_ids": ["run_gibt_es_nicht"],
            },
        )
        assert response.status_code == 400
        assert "run_gibt_es_nicht" in response.text


def test_an_uncited_answer_surfaces_no_sources_the_run_did_not_gather():
    """A run that only READ an attached report (its sources enter the
    ledger) and answered with a status line must not surface the whole
    ledger as "N Quellen" on that answer. The basis fallback belongs to
    runs that searched themselves; a citing answer still lists exactly
    what it cites."""
    llm = ScriptedToolLLM([])
    client = make_client(llm)
    with client:
        report_id = _seed_report(
            client,
            question="Stand der EU-Batterieverordnung?",
            url="https://example.org/bericht",
        )
        llm._turns = [
            _tool_turn("call_read_status", "read_research_report", {"report_id": report_id}),
            _text_turn("Die Antwort steht jetzt als Datei bereit."),
        ]
        response = client.post(
            "/v1/runs",
            json={
                "question": "Schreibe die Antwort als Datei.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-status-answer",
                "report_ids": [report_id],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["references"] == []

        llm._turns = [
            _tool_turn("call_read_cited", "read_research_report", {"report_id": report_id}),
            _text_turn("Die Verordnung gilt seit 2024 [W1]."),
        ]
        cited = client.post(
            "/v1/runs",
            json={
                "question": "Seit wann gilt die Verordnung?",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-status-answer",
                "report_ids": [report_id],
            },
        ).json()["run_id"]
        wait_status(client, cited, {"completed"})
        result = client.get(f"/v1/runs/{cited}/result").json()
        assert [ref["label"] for ref in result["references"]] == ["W1"]
