"""Session-context builder contract.

Secures: durable-rows composition (question, Q/A lines, approval notes,
answer body), the deterministic trim policy with VISIBLE markers, the
artifact registry, and the never-raise degradation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import ClarificationRecord
from inqtrix.agents.session_context import (
    ONE_LINER_QUESTION_CHARS,
    RECENT_TURNS_VERBATIM,
    TOTAL_HISTORY_CHAR_BUDGET,
    TURN_BODY_FLOOR_CHARS,
    build_session_context,
    SessionContextPack,
    _VerbatimTurn,
    _within_budget,
)


class FakeRunStore:
    """Narrow stand-in for the run store surface the builder touches."""

    def __init__(
        self,
        summaries: list[dict[str, Any]],
        results: dict[str, dict[str, Any]] | None = None,
        full_questions: dict[str, str] | None = None,
    ) -> None:
        self._summaries = summaries
        self._results = results or {}
        # The real store clips the question on LIST rows and hands the full
        # text back only on the single-run read. A double that omits this
        # surface cannot see the bug that lived in the gap between them.
        self._full_questions = full_questions or {}
        self.get_calls: list[str] = []

    def list_session_runs(
        self, session_id: str, *, visible_to: Any = None
    ) -> list[dict[str, Any]]:
        return [
            dict(summary)
            for summary in self._summaries
            if summary.get("session_id") == session_id
        ]

    def result(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
        payload = self._results.get(run_id)
        if payload is None:
            raise KeyError(run_id)
        return dict(payload)

    def get(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
        self.get_calls.append(run_id)
        full = self._full_questions.get(run_id)
        if full is None:
            raise KeyError(run_id)
        return {"run_id": run_id, "question_full": full}


def _summary(
    run_id: str,
    question: str,
    *,
    status: str = "completed",
    session_id: str = "sess-1",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "question": question,
        "status": status,
        "session_id": session_id,
        "agent_overrides": {},
    }


def _pack(
    summaries: list[dict[str, Any]],
    *,
    results: dict[str, dict[str, Any]] | None = None,
    control: MemoryAgentControlStore | None = None,
    current_run_id: str = "run_current",
    full_questions: dict[str, str] | None = None,
    store: "FakeRunStore | None" = None,
) -> SessionContextPack:
    return build_session_context(
        "sess-1",
        run_store=store or FakeRunStore(summaries, results, full_questions),
        control=control or MemoryAgentControlStore(),
        run_async=asyncio.run,
        visible_to=None,
        current_run_id=current_run_id,
    )


def test_follow_up_history_contains_question_qa_and_answer():
    control = MemoryAgentControlStore()
    asyncio.run(
        control.create_clarification(
            ClarificationRecord(
                clarification_id="clr_1",
                run_id="run_1",
                question="Welcher Markt?",
                questions=(
                    {
                        "id": "q1",
                        "prompt": "Welcher Markt?",
                        "options": [
                            {"id": "q1_o1", "label": "Europa", "description": ""}
                        ],
                        "multi_select": False,
                    },
                ),
                answers={"q1": {"option_ids": ["q1_o1"], "text": ""}},
                status="answered",
            )
        )
    )
    pack = _pack(
        [_summary("run_1", "Erstelle eine Marktanalyse.")],
        results={"run_1": {"answer": "Die Marktlage ist stabil [W1]."}},
        control=control,
    )
    assert "User: Erstelle eine Marktanalyse." in pack.history_block
    assert (
        "Clarification: Welcher Markt? — Answer: Europa" in pack.history_block
    )
    assert "Agent: Die Marktlage ist stabil [W1]." in pack.history_block


def test_current_run_and_children_are_excluded():
    summaries = [
        _summary("run_1", "Frage eins."),
        {**_summary("run_child", "Kindlauf."), "kind": "agent_child"},
        _summary("run_current", "Aktuelle Frage."),
    ]
    pack = _pack(summaries, results={"run_1": {"answer": "Antwort eins."}})
    assert "Frage eins." in pack.history_block
    assert "Kindlauf." not in pack.history_block
    assert "Aktuelle Frage." not in pack.history_block


def test_older_turns_collapse_to_one_liners():
    summaries = [
        _summary(f"run_{i}", f"Frage Nummer {i}.") for i in range(6)
    ]
    results = {
        f"run_{i}": {"answer": f"Antwort {i}."} for i in range(6)
    }
    pack = _pack(summaries, results=results)
    # The oldest turns collapse; the newest RECENT_TURNS_VERBATIM keep
    # their full bodies.
    assert 'Earlier: "Frage Nummer 0."' in pack.history_block
    for i in range(6 - RECENT_TURNS_VERBATIM, 6):
        assert f"Agent: Antwort {i}." in pack.history_block
    assert "Agent: Antwort 0." not in pack.history_block


def test_the_history_block_uses_the_durable_question_not_the_clipped_row():
    """The list row is the DISPLAY projection and is already cut at 500.

    This is the cut that actually reached production: a standing
    requirement at the end of a long question ("answer in at most 80
    words") was gone from the follow-up prompt, and the reading "the model
    forgot it" was wrong -- the model never saw it. Fixing the builder's
    own cap was necessary and not sufficient while it still read the
    clipped row.
    """
    asked = (
        "Erklaere die Energiebilanz eines stationaeren Batteriespeichers. " * 8
        + "Wichtig fuer alles Weitere: Antworte in hoechstens 80 Woertern."
    )
    assert len(asked) > 500
    row = _summary("run_1", asked[:500] + "…")
    row["question_clipped"] = True
    store = FakeRunStore(
        [row, _summary("run_2", "Und bei LFP?")],
        {"run_1": {"answer": "Kurz."}, "run_2": {"answer": "Kurz."}},
        {"run_1": asked},
    )
    pack = _pack([], store=store)
    assert "Antworte in hoechstens 80 Woertern." in pack.history_block
    assert "…" not in pack.history_block
    assert store.get_calls == ["run_1"]


def test_an_unclipped_row_costs_no_extra_read():
    store = FakeRunStore(
        [_summary("run_1", "Kurze Frage?")], {"run_1": {"answer": "Kurz."}}
    )
    pack = _pack([], store=store)
    assert "Kurze Frage?" in pack.history_block
    assert store.get_calls == []


def test_an_unreachable_full_question_keeps_the_visible_row_cut():
    """Degrade visibly: the row's own marker stays, nothing is invented."""
    row = _summary("run_1", "Lange Frage" + "…")
    row["question_clipped"] = True
    store = FakeRunStore([row], {"run_1": {"answer": "Kurz."}})
    pack = _pack([], store=store)
    assert "Lange Frage…" in pack.history_block
    assert store.get_calls == ["run_1"]


def test_a_collapsed_older_question_says_that_it_was_shortened():
    """A bare ellipsis reads like something the user typed. Say it.

    The reminder line for an older turn keeps only the first
    ``ONE_LINER_QUESTION_CHARS`` characters. That is by design -- but the
    reader must be able to tell the cut from the user's own punctuation,
    the same way the verbatim path does.
    """
    long_question = "Vergleiche A und B. " * 20
    assert len(long_question) > ONE_LINER_QUESTION_CHARS
    summaries = [_summary("run_0", long_question)] + [
        _summary(f"run_{i}", f"Frage {i}") for i in range(1, 5)
    ]
    pack = _pack(
        summaries,
        results={f"run_{i}": {"answer": f"Antwort {i}."} for i in range(5)},
    )
    line = next(
        row
        for row in pack.history_block.splitlines()
        if row.startswith('Earlier: "Vergleiche')
    )
    assert "[... question shortened]" in line
    assert not line.startswith('Earlier: "Vergleiche A und B. Vergleiche'
                               ' A und B. …')


def test_a_short_older_question_carries_no_cut_marker():
    summaries = [
        _summary(f"run_{i}", f"Frage Nummer {i}.") for i in range(5)
    ]
    pack = _pack(
        summaries,
        results={f"run_{i}": {"answer": f"Antwort {i}."} for i in range(5)},
    )
    assert 'Earlier: "Frage Nummer 0." — run completed.' in pack.history_block
    assert "question shortened" not in pack.history_block


def test_total_budget_trims_visibly():
    # A verbatim turn can legitimately exceed the per-turn body cap via
    # long free-text clarification answers (uncapped by design — they
    # are user input); the TOTAL budget then drops the OLDEST turns and
    # marks the cut visibly.
    control = MemoryAgentControlStore()
    for i in range(4):
        asyncio.run(
            control.create_clarification(
                ClarificationRecord(
                    clarification_id=f"clr_{i}",
                    run_id=f"run_{i}",
                    question="Kontext?",
                    status="answered",
                    answer="Y" * 3000,
                )
            )
        )
    summaries = [_summary(f"run_{i}", f"Frage {i}.") for i in range(4)]
    results = {f"run_{i}": {"answer": "Antwort."} for i in range(4)}
    pack = _pack(summaries, results=results, control=control)
    assert len(pack.history_block) <= TOTAL_HISTORY_CHAR_BUDGET + 100
    assert pack.history_block.startswith(
        "[... older parts of the conversation shortened]"
    )
    # The newest turn always survives.
    assert "Frage 3." in pack.history_block


def _turn(question: str, answer: str) -> _VerbatimTurn:
    return _VerbatimTurn(head=f"User: {question}", body=f"Agent: {answer}")


def test_the_builder_itself_never_cuts_the_question_at_a_fixed_length():
    """Through the real builder, not just the budget function.

    The cut would hide HERE: a ``_verbatim_turn`` that cuts every earlier
    question at 500 characters before the budget is ever consulted drops a
    requirement written at the end of a long question ("at most 800
    words") from the follow-up prompt — and the trailing ellipsis looks
    like the user's own. A pin on ``_within_budget`` alone stays green when
    that cut returns.
    """
    requirement = "Antworte in hoechstens 800 Woertern."
    question = "Vergleiche A und B ausfuehrlich. " * 30 + requirement
    assert len(question) > 500

    pack = _pack(
        [_summary("run_1", question)],
        results={"run_1": {"answer": "Kurze Antwort."}},
    )

    assert question in pack.history_block
    assert requirement in pack.history_block
    assert "…" not in pack.history_block


def test_a_long_question_is_not_cut_while_the_budget_allows_it():
    """The defect this replaced: a fixed 500-character cut dropped standing
    requirements that sat at the END of a long question ("at most 800
    words"), and the trailing ellipsis read like something the user typed.
    The question is the smallest and most valuable line in the block."""
    question = "Vergleiche A und B. " * 40 + "Antworte in hoechstens 800 Woertern."
    assert len(question) > 500
    block = _within_budget([_turn(question, "Kurze Antwort.")])

    assert question in block
    assert "…" not in block
    assert "question shortened" not in block


def test_answer_bodies_are_spent_before_a_question_is_touched():
    """Bodies are the bulk and are model-written; questions are the user's
    own instruction. Pressure spends the cheap side first."""
    question = "Frage mit einer Vorgabe am Ende: antworte kurz. " * 20
    turns = [_turn(question, "Y" * 5000) for _ in range(3)]
    block = _within_budget(turns)

    assert len(block) <= TOTAL_HISTORY_CHAR_BUDGET
    assert block.count("[... shortened]") >= 1, "a body must have been squeezed"
    # every surviving turn still carries its question whole
    assert question in block


def test_squeezing_alone_can_be_enough_and_then_no_turn_is_dropped():
    """Four turns of 4000 characters fit once their bodies reach the floor,
    so nothing is dropped and the block makes no claim that it was."""
    turns = [_turn(f"Frage {index}.", "Y" * 4000) for index in range(4)]
    block = _within_budget(turns)

    assert len(block) <= TOTAL_HISTORY_CHAR_BUDGET
    assert "older parts of the conversation shortened" not in block
    for index in range(4):
        assert f"Frage {index}." in block


def test_whole_turns_drop_only_after_bodies_hit_the_floor():
    """Heads alone overflow here, so squeezing cannot save it and the oldest
    turns go — newest first out of the door is never the rule."""
    turns = [_turn("F" * 1500 + f" #{index}", "Y" * 4000) for index in range(6)]
    block = _within_budget(turns)

    assert len(block) <= TOTAL_HISTORY_CHAR_BUDGET
    assert "older parts of the conversation shortened" in block
    assert "#5" in block, "the newest turn survives"
    assert "#0" not in block, "the oldest turn is the one that went"
    # bodies were spent down to the floor before any turn was dropped
    assert "[... shortened]" in block
    for segment in block.split("Agent: ")[1:]:
        body = segment.split("[... shortened]")[0]
        assert len(body) <= TURN_BODY_FLOOR_CHARS + 1


def test_a_question_that_cannot_fit_is_cut_with_a_visible_marker():
    """Last resort, and never silent: a bare ellipsis is indistinguishable
    from one the user typed, so the cut says what it is."""
    block = _within_budget([_turn("X" * 9000, "kurz")])

    assert len(block) <= TOTAL_HISTORY_CHAR_BUDGET
    assert block.endswith("[... question shortened]")


def test_one_oversized_latest_turn_does_not_claim_older_turns_were_trimmed():
    block = _within_budget([_turn("X" * 9000, "kurz")])

    assert "older parts of the conversation shortened" not in block


def test_pruned_result_degrades_visibly_not_silently():
    pack = _pack([_summary("run_1", "Frage eins.")], results={})
    assert "Agent: (result no longer available)" in pack.history_block


def test_artifact_registry_lists_session_deliverables():
    control = MemoryAgentControlStore()
    asyncio.run(
        control.upsert_artifact(
            run_id="run_1",
            kind="memo",
            session_id="sess-1",
            title="Marktanalyse",
            status="ready",
            content_markdown="# Memo",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_test_memo",
        )
    )
    pack = _pack(
        [_summary("run_1", "Frage.")],
        results={"run_1": {"answer": "A."}},
        control=control,
    )
    assert len(pack.artifact_registry) == 1
    entry = pack.artifact_registry[0]
    assert entry["kind"] == "memo"
    assert entry["title"] == "Marktanalyse"
    assert entry["revision"] == 1
    # the derived file name rides the registry entry.
    assert entry["name"] == "marktanalyse.md"


def test_artifact_registry_names_are_unique_in_created_order():
    """Colliding titles get the -2 suffix by CREATED order."""
    control = MemoryAgentControlStore()
    for index, artifact_id in enumerate(["art_a", "art_b"], start=1):
        asyncio.run(
            control.upsert_artifact(
                run_id=f"run_{index}",
                kind="deliverable",
                session_id="sess-1",
                title="Bericht",
                status="ready",
                content_markdown="# Doc",
                payload={},
                refs=[],
                updated_by="agent",
                artifact_id=artifact_id,
            )
        )
    pack = _pack(
        [_summary("run_1", "Frage.")],
        results={"run_1": {"answer": "A."}},
        control=control,
    )
    names = {
        entry["artifact_id"]: entry["name"]
        for entry in pack.artifact_registry
    }
    assert names == {"art_a": "bericht.md", "art_b": "bericht-2.md"}


def test_prior_evidence_count_deduplicates_canonical_sources():
    control = MemoryAgentControlStore()
    shared = {"url": "https://example.com/report", "title": "Report"}
    for artifact_id in ("art_one", "art_two"):
        asyncio.run(
            control.upsert_artifact(
                run_id="run_1",
                kind="deliverable",
                session_id="sess-1",
                title=artifact_id,
                status="ready",
                content_markdown="Body",
                payload={},
                refs=[shared],
                updated_by="agent",
                artifact_id=artifact_id,
            )
        )

    pack = _pack(
        [_summary("run_1", "Frage.")],
        results={"run_1": {"answer": "A."}},
        control=control,
    )

    assert pack.prior_evidence_count == 1
    # The references behind the count travel with it, once per source,
    # exactly as the earlier turn stored them (label included).
    assert pack.prior_references == (shared,)


def test_effective_execution_response_form_precedes_legacy_override():
    summary = _summary("run_1", "Frage.")
    summary["snapshot"] = {"execution": {"response_form": "canvas"}}
    summary["agent_overrides"] = {"response_form": "chat"}

    pack = _pack([summary], results={"run_1": {"answer": "A."}})

    assert pack.last_response_form == "canvas"


def test_chat_evidence_survives_history_compaction_without_becoming_a_canvas():
    class RecordedControl(MemoryAgentControlStore):
        calls = []

        async def list_artifacts(self, run_id, **kwargs):
            self.calls.append((run_id, kwargs.get("kind")))
            return await super().list_artifacts(run_id, **kwargs)

    principal = object()

    class VisibleStore(FakeRunStore):
        def list_session_runs(self, session_id, *, visible_to=None):
            assert visible_to is principal
            return super().list_session_runs(session_id, visible_to=visible_to)

        def result(self, run_id, *, visible_to=None):
            assert visible_to is principal
            return super().result(run_id, visible_to=visible_to)

    control = RecordedControl()
    summaries = [_summary(f"run_{i}", f"Frage {i}") for i in range(5)]
    summaries.extend([
        _summary("run_failed", "Unfertig", status="failed"),
        _summary("run_other", "Andere Sitzung", session_id="sess-other"),
        _summary("run_current", "Aktueller Lauf"),
        {**_summary("run_child", "Kind"), "kind": "agent_child"},
    ])
    retained = {"url": "https://example.test/conditions", "label": "W4",
                "source_run_id": "run_0", "query_id": "query_0", "title": "Bedingungen"}
    for run_id in ("run_0", "run_failed", "run_other", "run_current", "run_child"):
        asyncio.run(control.upsert_artifact(
            run_id=run_id, artifact_id=f"art_{run_id}_evidence", kind="evidence_bundle",
            session_id=None, title="Evidence", status="ready", content_markdown="",
            payload={"web_search_ledger": {"searches": {}}},
            refs=[retained if run_id == "run_0" else {"url": f"https://example.test/{run_id}"}],
            updated_by="agent",
        ))
    pack = build_session_context(
        "sess-1", run_store=VisibleStore(summaries, {f"run_{i}": {"answer": "Antwort ohne Zitat."} for i in range(5)}),
        control=control, run_async=asyncio.run, visible_to=principal, current_run_id="run_current",
    )
    assert 'Earlier: "Frage 0"' in pack.history_block
    assert pack.prior_references == (retained,)
    assert pack.prior_evidence_count == 1
    assert pack.artifact_registry == ()
    assert control.calls == [(f"run_{i}", "evidence_bundle") for i in range(5)]


def test_prior_evidence_metadata_fills_canvas_lineage_without_replacing_its_label():
    control = MemoryAgentControlStore()
    canvas_ref = {"url": "https://example.test/conditions", "label": "W1"}
    run_ref = {**canvas_ref, "label": "W3", "source_run_id": "run_1", "query_id": "query_1"}
    for kind, session_id, refs in (("deliverable", "sess-1", [canvas_ref]), ("evidence_bundle", None, [run_ref])):
        asyncio.run(control.upsert_artifact(
            run_id="run_1", artifact_id="art_" + kind, kind=kind, session_id=session_id,
            title=kind, status="ready", content_markdown="", payload={}, refs=refs, updated_by="agent",
        ))
    pack = _pack([_summary("run_1", "Frage")], results={"run_1": {"answer": "Antwort"}}, control=control)
    assert pack.prior_references == ({**run_ref, "label": "W1"},)
    assert pack.prior_evidence_count == 1
    assert [r["artifact_id"] for r in pack.artifact_registry] == ["art_deliverable"]
    stored, _ = asyncio.run(control.get_artifact("run_1", "art_deliverable"))
    assert stored.refs == (canvas_ref,)


def test_prior_run_evidence_reads_every_metadata_page():
    class PagedControl(MemoryAgentControlStore):
        calls = 0

        async def list_artifacts(self, *args, **kwargs):
            self.calls += 1
            assert self.calls <= 2, "pagination repeated a page"
            return await super().list_artifacts(*args, **kwargs)

    control = PagedControl()
    for i in range(51):
        asyncio.run(control.upsert_artifact(
            run_id="run_1", artifact_id=f"art_evidence_{i}", kind="evidence_bundle", session_id=None,
            title="Evidence", status="ready", content_markdown="", payload={},
            refs=[{"url": f"https://example.test/{i}", "label": f"W{i+1}"}], updated_by="agent",
        ))
    pack = _pack([_summary("run_1", "Frage")], results={"run_1": {"answer": "Antwort"}}, control=control)
    assert pack.prior_evidence_count == 51
    assert {ref["url"] for ref in pack.prior_references} == {f"https://example.test/{i}" for i in range(51)}
    assert control.calls == 2


def test_follow_up_reads_prior_chat_evidence_and_rechecks_origin_access():
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.evidence_read import read_registered_evidence
    from inqtrix.server.runs import RunNotFound

    principal = object()
    source_run = "run_prior_search"
    control = MemoryAgentControlStore()
    ref = {"url": "https://example.test/conditions", "label": "W2",
           "source_run_id": source_run, "query_id": "query_1"}
    ledger = {"searches": {"query_1": {
        "source_run_id": source_run, "query_id": "query_1", "query": "Conditions",
        "provider_answer": "Eligible only after registration.", "citations": [{"url": ref["url"]}],
    }}}
    asyncio.run(control.upsert_artifact(
        run_id=source_run, artifact_id=f"art_{source_run[-12:]}_evidence", kind="evidence_bundle",
        session_id=None, title="Evidence", status="ready", content_markdown="",
        payload={"web_search_ledger": ledger}, refs=[ref], updated_by="agent",
    ))

    class VisibleStore(FakeRunStore):
        allowed = True

        def get(self, run_id, *, visible_to):
            assert visible_to is principal and run_id == source_run
            if not self.allowed:
                raise RunNotFound(run_id)
            return _summary(source_run, "Vorige Frage")

        def result(self, run_id, *, visible_to):
            self.get(run_id, visible_to=visible_to)
            return {"answer": "Kurzantwort"}

    store = VisibleStore([_summary(source_run, "Vorige Frage")])
    pack = build_session_context("sess-1", run_store=store, control=control,
                                 run_async=asyncio.run, visible_to=principal, current_run_id="run_followup")
    deps = KernelDeps(run_id="run_followup", session_id="sess-1", control=control,
                      platform=None, llm=None, model=None, reasoning_effort=None, timeout=1)
    deps.run_service = SimpleNamespace(run_store=store)
    deps.capability_context = SimpleNamespace(visible_to=principal)
    registered = deps.register_references(list(pack.prior_references))
    assert len(registered) == 1
    index = read_registered_evidence(deps)
    assert index["references"][0]["label"] == "W2"
    result = read_registered_evidence(deps, reference_id=registered[0]["reference_id"])
    assert result["searches"]["query_1"]["provider_answer"] == "Eligible only after registration."
    store.allowed = False
    denied = read_registered_evidence(deps, reference_id=registered[0]["reference_id"])
    assert denied["searches"] == {}
    assert denied["unavailable_origins"][0]["source_run_id"] == source_run


def test_control_store_outage_keeps_the_run_history(caplog):
    """Partial degradation: a broken control store costs the registry
    and the Q/A lines, NEVER the run-based history (each loudly)."""

    class BrokenControl:
        async def list_session_artifacts(self, session_id: str):
            raise RuntimeError("control down")

        async def list_clarifications(self, run_id: str):
            raise RuntimeError("control down")

        async def list_approvals(self, run_id: str):
            raise RuntimeError("control down")

    handler_target = logging.getLogger("inqtrix")
    handler_target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            pack = build_session_context(
                "sess-1",
                run_store=FakeRunStore(
                    [_summary("run_1", "Frage eins.")],
                    {"run_1": {"answer": "Antwort eins."}},
                ),
                control=BrokenControl(),  # type: ignore[arg-type]
                run_async=asyncio.run,
                visible_to=None,
                current_run_id="run_x",
            )
    finally:
        handler_target.removeHandler(caplog.handler)
    assert "User: Frage eins." in pack.history_block
    assert "Agent: Antwort eins." in pack.history_block
    assert pack.artifact_registry == ()
    assert any(
        "Artifact registry" in record.message for record in caplog.records
    )


def test_builder_never_raises(caplog):
    class BrokenStore:
        def list_session_runs(self, *args: Any, **kwargs: Any):
            raise RuntimeError("kaputt")

    handler_target = logging.getLogger("inqtrix")
    handler_target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            pack = build_session_context(
                "sess-1",
                run_store=BrokenStore(),
                control=MemoryAgentControlStore(),
                run_async=asyncio.run,
                visible_to=None,
                current_run_id="run_x",
            )
    finally:
        handler_target.removeHandler(caplog.handler)
    assert pack == SessionContextPack()
    assert any(
        "Session context" in record.message for record in caplog.records
    )
