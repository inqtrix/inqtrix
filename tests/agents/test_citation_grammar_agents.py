"""The agent engines read, write and repair citations with one grammar.

A grouped bracket (``[K2, W7]``) is written singly at the writer boundary
and counted like single brackets by every reader; an invented label inside
a group is as loud as a single one; a bracket no reader can parse (a range,
a slash, a prose join) is repaired, handed back or marked, never lost.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents.kernel.review_edits import apply_review_edits
from inqtrix.agents.markdown import normalize_agent_markdown, normalize_writer_markdown
from inqtrix.agents.phase_models import DeepRevisionBundle
from inqtrix.agents.report_quality import (
    CitationValidationFailed,
    citation_coverage,
    cited_references,
    malformed_citation_brackets,
    unknown_citation_labels,
    validate_and_repair_citations,
)
from inqtrix.citation_labels import CITATION_FORMAT
from inqtrix.providers.base import StructuredLLMResponse
from tests.agents.test_review_contract import finding, locate

LEDGER = [
    {"label": "K2", "reference_id": "ref_k2"},
    {"label": "W7", "reference_id": "ref_w7"},
    {"label": "K1", "reference_id": "ref_k1"},
]
"""Distinct identities: a revision maps a finding's basis by identity."""


# -- the boundaries ---------------------------------------------------------- #


def test_the_writer_boundary_writes_groups_singly_and_escapes_currency():
    assert normalize_writer_markdown("Satz.[K2, W7] kostet $5") == (
        "Satz.[K2] [W7] kostet \\$5"
    )


def test_the_writer_boundary_writes_escaped_editor_labels_raw():
    """A canvas body a Markdown editor stored carries ``\\[K2\\]``; an agent
    that rewrites the canvas must not carry the escape forward, or the
    citation is lost to every reader."""
    stored = (
        "Laut Editor \\[K2, W7\\] und \\[K1\\]\\[W7\\] kostet $5.\n"
        "\\[K1\\]: bleibt, Studie \\[K2\\](2020) auch."
    )
    written = normalize_writer_markdown(stored)
    assert written == (
        "Laut Editor [K2] [W7] und [K1] [W7] kostet \\$5.\n"
        "\\[K1\\]: bleibt, Studie \\[K2\\](2020) auch."
    )
    assert [ref["label"] for ref in cited_references(written, LEDGER)] == ["K2", "W7", "K1"]
    assert normalize_writer_markdown(written) == written


def test_the_knowledge_boundary_leaves_its_quote_protocol_alone():
    """The knowledge answer's quote lines are checked byte by byte: the
    currency boundary it passes must not rewrite a citation group."""
    line = '[K1, K2] "Wörtliches Zitat"'
    assert normalize_agent_markdown(line) == line


# -- the readers ------------------------------------------------------------- #


def test_a_grouped_citation_is_cited_and_counted():
    text = "Marktteilnehmer wäre.[K2, W7]"
    assert [ref["label"] for ref in cited_references(text, LEDGER)] == ["K2", "W7"]
    assert citation_coverage(text) == {
        "paragraphs": 1,
        "cited_paragraphs": 1,
        "labels_used": 2,
    }


def test_an_invented_label_in_a_group_is_unknown():
    assert unknown_citation_labels("Satz [K2, W99].", LEDGER) == ["W99"]
    assert unknown_citation_labels("Satz [W99](https://evil.example).", LEDGER) == ["W99"]


def test_a_bracket_no_reader_can_parse_is_named():
    assert malformed_citation_brackets("Satz [K3–K5]. Marker [unbelegt: K2].") == ["[K3–K5]"]


# -- the mission's bounded repair -------------------------------------------- #


class _RepairLLM:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.prompts: list[str] = []

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(self, prompt: str, **_kwargs: Any) -> StructuredLLMResponse:
        self.prompts.append(prompt)
        return StructuredLLMResponse(
            parsed={"markdown": self.markdown}, content="", prompt_tokens=1, completion_tokens=1
        )


def _repair(llm: _RepairLLM, markdown: str) -> str:
    repaired, _usage = validate_and_repair_citations(
        llm,
        markdown=markdown,
        known_labels=["K2", "W7"],
        usage={},
        system="system",
        model=None,
        reasoning_effort=None,
        timeout=5.0,
    )
    return repaired


def test_an_invented_label_in_a_group_is_repaired():
    llm = _RepairLLM("Satz [K2].")
    assert _repair(llm, "Satz [K2, W99].") == "Satz [K2]."
    assert len(llm.prompts) == 1


def test_an_unreadable_bracket_is_repaired_and_named_to_the_repair():
    llm = _RepairLLM("Satz [K2] [W7].")
    assert _repair(llm, "Satz [K2–W7].") == "Satz [K2] [W7]."
    prompt = llm.prompts[0]
    assert "keep only the labels written out in each bracket" in prompt
    fenced = prompt.split('<untrusted_content source="unreadable-citations">', 1)[1]
    assert fenced.split("</untrusted_content>", 1)[0].strip() == "[K2–W7]"


def test_a_repair_that_writes_a_group_passes_the_writer_boundary_first():
    llm = _RepairLLM("Satz [K2, W7].")
    assert _repair(llm, "Satz [W99].") == "Satz [K2] [W7]."


def test_a_repair_that_leaves_a_range_fails_loudly():
    llm = _RepairLLM("Satz [K2–W7].")
    with pytest.raises(CitationValidationFailed, match="malformed=1"):
        _repair(llm, "Satz [K2–W7].")


# -- the kernel's answer ----------------------------------------------------- #


def test_an_unreadable_citation_never_attaches_the_whole_ledger():
    from inqtrix.agents.kernel.algorithm import _result_references

    for answer in ("Satz [K3–K5].", "Satz [unbelegt: K3–K5]."):
        assert _result_references(answer, LEDGER, basis_fallback=True) == []


def test_a_grouped_citation_lists_exactly_its_members():
    from inqtrix.agents.kernel.algorithm import _result_references

    cited = _result_references("Satz [K2, W7].", LEDGER, basis_fallback=True)
    assert [ref["label"] for ref in cited] == ["K2", "W7"]


def test_an_unreadable_citation_is_marked_not_expanded():
    from inqtrix.agents.kernel.algorithm import _mark_unresolved_citation_labels

    marked = _mark_unresolved_citation_labels(
        "Satz [K3–K5]. Neu [W99].", ["W99"], language="de"
    )
    assert marked == "Satz [unbelegt: K3–K5]. Neu [unbelegt: W99]."


# -- review edits ------------------------------------------------------------ #


def _revise(replacement: str):
    """An evidence error resting on LEDGER's K2 and W7, fixed by *replacement*."""
    outputs = {"doc": "Falsch.", "chat": "Fertig."}
    allowed = {"doc": LEDGER, "chat": LEDGER}
    located, _ = locate(
        [finding(kind="evidence_error", basis_ids=["source:ref_k2", "source:ref_w7"])],
        outputs, allowed_references=allowed,
    )
    return apply_review_edits(
        outputs,
        located,
        DeepRevisionBundle(edits=[{"finding_id": "finding_1", "replacement": replacement}]),
        allowed_references=allowed,
        identifiers=[],
    )


def test_a_revision_splices_a_group_in_singly():
    assert _revise("Richtig [K2, W7].")[0]["doc"] == "Richtig [K2] [W7]."


def test_a_revision_with_an_invented_label_in_a_group_is_refused():
    assert [r.reason for r in _revise("Richtig [K2, W99].")[1]] == ["label_not_citable"]


def test_a_revision_with_an_unreadable_bracket_is_refused():
    assert [r.reason for r in _revise("Richtig [K3–K5].")[1]] == ["citation_malformed"]


def test_the_review_rereads_a_label_cited_only_in_a_group():
    from inqtrix.agents.kernel.review_pass import _citable_labels

    refs = {"a": {"label": "K2", "reference_id": "a"}, "b": {"label": "W7", "reference_id": "b"}}
    subject = SimpleNamespace(
        ledger=refs,
        outputs={"memo": "Marktteilnehmer wäre.[K2, W7]"},
        allowed_references={"memo": list(refs.values())},
    )
    assert _citable_labels(subject) == {"K2", "W7"}


# -- a child's evidence in the parent's labels ------------------------------- #


def test_a_childs_groups_links_and_renames_translate_in_one_pass():
    from inqtrix.agents.kernel.tools import translate_child_citations

    translated = translate_child_citations(
        "A [W1, W3]. B [E3](https://a.example). C [W9].",
        {"W1": "W3", "W3": "W5", "E3": "W12"},
        marker="unbelegt",
    )
    assert translated == "A [W3] [W5]. B [W12](https://a.example). C [unbelegt: W9]."


# -- the prompts ------------------------------------------------------------- #


def test_every_agent_writer_is_told_the_one_citation_form():
    from inqtrix.agents.prompts import (
        agent_answer_system_prompt,
        agent_synthesis_system_prompt,
        build_agent_citation_repair_prompt,
        build_agent_kernel_system_prompt,
        build_deep_revision_prompt,
    )

    surfaces = {
        "synthesis": agent_synthesis_system_prompt(),
        "answer": agent_answer_system_prompt(),
        "kernel": build_agent_kernel_system_prompt(),
        "repair": build_agent_citation_repair_prompt("Text", allowed_labels=["K1"]),
        "deep_revision": build_deep_revision_prompt("Auftrag", "Bundle", []),
    }
    for name, text in surfaces.items():
        assert text.count(CITATION_FORMAT) == 1, name


# -- the mission's section writer, end to end -------------------------------- #


def test_a_section_written_with_a_group_keeps_both_sources(monkeypatch):
    """The mission's outline shows each section its labels singly, and a
    section that writes a group anyway still cites both sources: the
    stored memo reads [W1] [W2], and its references carry W1 and W2."""
    from inqtrix.search_result import GroundedSearchResult, GroundedSource
    from tests.agents.test_workspace_agent import (
        OUTLINE,
        PROFILE,
        FakeSearch,
        ScriptedLLM,
        _memo_body,
        approve_pending,
        make_agent_client,
        submit_agent,
        wait_status,
    )

    plan = {
        "summary_markdown": "Zwei Suchen, dann Synthese.",
        "tasks": [
            {"id": "t1", "title": "Lage A", "tool_kind": "web_instant",
             "queries": ["Wie ist die Lage A 2026?"]},
            {"id": "t2", "title": "Lage B", "tool_kind": "web_instant",
             "queries": ["Wie ist die Lage B 2026?"]},
            {"id": "s", "title": "Synthese", "tool_kind": "synthesis", "depends_on": ["t1", "t2"]},
        ],
        "success_criteria": ["Beide Lagen belegt."],
    }
    outline = json.loads(json.dumps(OUTLINE))
    outline["sections"][0]["evidence_labels"] = ["W1", "W2"]
    llm = ScriptedLLM(
        overrides={
            "AssignmentProfile": dict(PROFILE),
            "ExecutionPlanModel": plan,
            "ReportOutline": outline,
            "SectionText": {"markdown": "Die Lage ist stabil.[W1, W2]"},
        }
    )
    class OneSourcePerQuery(FakeSearch):
        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            self.queries.append(query)
            return GroundedSearchResult(
                answer=f"Antwort zu {query}",
                sources=[
                    GroundedSource(
                        url=f"https://example.com/{len(self.queries)}",
                        title=f"Quelle {len(self.queries)}",
                        snippet="Beleg",
                    )
                ],
            )

    from tests.agents.test_workspace_agent import platform_without_web_preview

    client = make_agent_client(
        monkeypatch,
        llm=llm,
        search=OneSourcePerQuery(),
        platform=platform_without_web_preview(),
    )
    with client:
        run_id = submit_agent(client)
        wait_status(client, run_id, {"waiting_for_approval"})
        approve_pending(client, run_id)
        final = wait_status(client, run_id, {"completed", "failed"})
        memo = _memo_body(client, run_id)
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo_id = next(a["artifact_id"] for a in artifacts if a["kind"] == "memo")
        memo_refs = client.get(f"/v1/runs/{run_id}/artifacts/{memo_id}").json()["refs"]
    assert final["status"] == "completed", final
    assert "[W1] [W2]" in memo and "[W1, W2]" not in memo
    assert {ref["label"] for ref in memo_refs} >= {"W1", "W2"}
    section_prompts = [p for name, p in llm.prompts if name == "SectionText"]
    assert section_prompts and all("[W1, W2]" not in p for p in section_prompts)


# -- the kernel's canvas ----------------------------------------------------- #

from tests.agents.test_kernel_evidence_read import deps  # noqa: E402,F401 (fixture)


def test_a_canvas_with_an_unreadable_citation_is_handed_back(deps):  # noqa: F811
    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.deps import run_coro, set_kernel_deps
    from inqtrix.agents.kernel.tools import build_kernel_tools

    first, second = deps.register_references([
        {"url": "https://example.org/a", "title": "A"},
        {"url": "https://example.org/b", "title": "B"},
    ])
    set_kernel_deps(deps)
    tool = next(t for t in build_kernel_tools() if t.name == "write_canvas")
    ids = [first["reference_id"], second["reference_id"]]
    reply = tool.func(title="Bericht", content_markdown="Beleg [W1–W2].",
                      tool_call_id="unreadable", reference_ids=ids)
    assert "cannot be read: [W1–W2]" in reply
    assert "not saved" in reply
    artifacts, _ = run_coro(deps.control.list_artifacts(deps.run_id, kind="deliverable"))
    assert artifacts == []
    tool.func(title="Bericht", content_markdown="Beleg [W1, W2].",
              tool_call_id="grouped", reference_ids=ids)
    artifacts, _ = run_coro(deps.control.list_artifacts(deps.run_id, kind="deliverable"))
    saved, _ = run_coro(deps.control.get_artifact(deps.run_id, artifacts[0].artifact_id))
    assert saved.content_markdown == "Beleg [W1] [W2]."


# -- every writer exit passes the writer boundary ---------------------------- #


class _SectionLLM:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(self, prompt: str, **_kwargs: Any) -> StructuredLLMResponse:
        return StructuredLLMResponse(
            parsed={"markdown": self.markdown}, content="", prompt_tokens=1, completion_tokens=1
        )


def test_the_mission_writer_exits_write_groups_singly():
    from inqtrix.agents.synthesis import assemble_memo, finish_section, write_chat_answer

    section, _usage = finish_section(
        _SectionLLM(""), "Satz [K2, W7].", usage={}, known_labels=None,
        internal_reference_ids=None, model=None, reasoning_effort=None, timeout=5.0,
    )
    assert section == "Satz [K2] [W7]."
    answer, _usage = write_chat_answer(
        _SectionLLM("Antwort [K2; W7]."), question="Frage", evidence_digest="",
        contradictions_digest="", history="", prior_memo="", model=None,
        reasoning_effort=None, timeout=5.0,
    )
    assert answer == "Antwort [K2] [W7]."
    assert "Satz [K2] [W7]." in assemble_memo("Titel", [("Teil", "Satz [K2, W7].")])


def test_the_kernel_answer_exit_writes_groups_singly():
    from inqtrix.agents.kernel.algorithm import _final_answer

    message = SimpleNamespace(type="ai", content="Satz [K2, W7].", invalid_tool_calls=None)
    assert _final_answer([message]) == "Satz [K2] [W7]."


def test_the_kernel_answer_repairs_an_unreadable_bracket():
    from inqtrix.agents.kernel.algorithm import _validate_kernel_answer_citations

    llm = _RepairLLM("Satz [K2] [W7].")
    events: list[tuple[str, dict]] = []
    stub = SimpleNamespace(
        user_text=lambda: "", question="Frage", check_abort=lambda: None, llm=llm,
        model=None, reasoning_effort=None, timeout=5.0,
        book_usage=lambda *_args: None, emit=lambda name, data: events.append((name, data)),
    )
    assert _validate_kernel_answer_citations(stub, "Satz [K2–W7].", LEDGER) == "Satz [K2] [W7]."
    assert "[K2–W7]" in llm.prompts[0]


def test_a_kernel_revision_passes_the_writer_boundary():
    """The revised answer is writer text like the answer it revises."""
    from tests.agents.test_kernel_deep_mode import StructuredScriptedLLM
    from tests.agents.test_review_pass import unquoted
    from tests.agents.test_kernel_prompt_events import _text_turn, make_client, wait_status

    llm = StructuredScriptedLLM(
        [_text_turn("Erste Antwort.")],
        [
            {
                "complete": True, "grounded": False, "contradictions_named": True,
                "findings": [{
                    "target": "chat", "artifact_id": "", "finding": "Überschrift fehlt.",
                    "kind": "output_requirement", "quote": "Erste Antwort.", "basis_ids": [],
                }],
            },
            {"edits": [{"finding_id": "finding_1",
                        "replacement": "## Kostet $5 statt $6\n\nErste Antwort."}]},
            unquoted("finding_1"),
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={"question": "Was kostet es?", "mode": "agent_kernel",
                  "agent_overrides": {"depth": "deep"}},
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
    assert summary["status"] == "completed", summary
    assert result["answer"] == "## Kostet \\$5 statt \\$6\n\nErste Antwort."


# -- the review's corrections ------------------------------------------------ #


def test_a_repair_that_drops_an_unreadable_only_citation_keeps_the_ledger_off():
    from inqtrix.agents.kernel.algorithm import (
        _result_references,
        _validate_kernel_answer_citations,
    )

    stub = SimpleNamespace(
        user_text=lambda: "", question="Frage", check_abort=lambda: None,
        llm=_RepairLLM("Der Markt wächst stark."), model=None, reasoning_effort=None,
        timeout=5.0, book_usage=lambda *_args: None, emit=lambda *_args: None,
    )
    answer = _validate_kernel_answer_citations(stub, "Der Markt wächst stark [K1–K2].", LEDGER)
    assert "[unbelegt: K1–K2]" in answer or "[unsupported: K1–K2]" in answer
    assert _result_references(answer, LEDGER, basis_fallback=True) == []


def test_marking_reaches_prose_only():
    from inqtrix.agents.kernel.algorithm import _mark_unresolved_citation_labels

    marked = _mark_unresolved_citation_labels(
        "Schreibweise `[K3–K5]` ist falsch; hier [K3–K5].", [], language="de"
    )
    assert marked == "Schreibweise `[K3–K5]` ist falsch; hier [unbelegt: K3–K5]."


def test_the_review_reads_each_output_on_its_own():
    from inqtrix.agents.kernel.review_pass import _citable_labels

    refs = {"a": {"label": "K2", "reference_id": "a"}, "b": {"label": "W7", "reference_id": "b"}}
    subject = SimpleNamespace(
        ledger=refs,
        outputs={"doc": "Beispiel:\n```python\nprint(1) [K2]", "chat": "Fertig [W7]."},
        allowed_references={"doc": list(refs.values()), "chat": list(refs.values())},
    )
    assert _citable_labels(subject) == {"W7"}


def test_a_childs_unreadable_bracket_is_marked_before_translation():
    from inqtrix.agents.kernel.tools import translate_child_citations

    translated = translate_child_citations(
        "Werte steigen [E3–E5]. Und [E1].", {"E1": "W4"}, marker="unbelegt"
    )
    assert translated == "Werte steigen [unbelegt: E3–E5]. Und [W4]."


def test_a_child_label_the_parent_keeps_maps_to_itself():
    from inqtrix.agents.kernel.tools import child_label_mapping, translate_child_citations

    raw = [{"label": "W1", "url": "a"}, {"label": "W2", "url": "b"}]
    registered = {"a": {"label": "W1"}, "b": {"label": "W5"}}
    mapping = child_label_mapping(raw, registered, lambda ref: ref["url"])
    assert mapping == {"W1": "W1", "W2": "W5"}
    assert translate_child_citations("A [W1]. B [W2].", mapping, marker="unbelegt") == "A [W1]. B [W5]."


def test_the_knowledge_engine_normalizes_without_the_citation_grammar():
    """Its answer carries a quote protocol grounding reads byte by byte: the
    knowledge engine keeps the currency-only boundary, groups and all."""
    from inqtrix.knowledge import algorithm as knowledge_algorithm

    assert knowledge_algorithm.normalize_agent_markdown is normalize_agent_markdown
    assert normalize_agent_markdown('[K1, K2] "Zitat kostet $5"') == '[K1, K2] "Zitat kostet \\$5"'


def test_a_label_after_a_link_destination_is_cited_and_checked():
    text = "Laut [Bericht](https://a.org/x)[W7] und [Studie](https://a.org/y)[W99]."
    assert [ref["label"] for ref in cited_references(text, LEDGER)] == ["W7"]
    assert unknown_citation_labels(text, LEDGER) == ["W99"]


def test_an_escaped_bracket_is_literal_text():
    assert malformed_citation_brackets("Beleg \\[W7\\] aus dem Editor.") == []
    assert cited_references("Beleg \\[W7\\] aus dem Editor.", LEDGER) == []


def test_an_unclosed_double_dollar_hides_no_citation():
    text = "Preisniveau $$$ überzeugt [K1].\n\nGünstiger [W7] [W99]."
    assert [ref["label"] for ref in cited_references(text, LEDGER)] == ["W7", "K1"]
    assert unknown_citation_labels(text, LEDGER) == ["W99"]


def test_the_section_writer_role_is_told_the_one_citation_form():
    from inqtrix.agents.prompts import SECTION_WRITER_ROLE, build_work_profile_system_prompt

    assert build_work_profile_system_prompt(SECTION_WRITER_ROLE).count(CITATION_FORMAT) == 1
