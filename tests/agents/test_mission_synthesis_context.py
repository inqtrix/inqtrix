"""Mission writing preserves retained evidence, reference IDs and output structure."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

import pytest

from inqtrix.agents.evidence_context import synthesis_evidence
from inqtrix.agents.phase_models import ReportOutline
from inqtrix.agents.synthesis import run_outline
from inqtrix.providers.base import LLMResponse, StructuredLLMResponse
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from tests.agents.test_kernel_evidence_read import search
from tests.agents.test_workspace_agent import (
    OUTLINE, PROFILE, FakeSearch, ScriptedLLM, _memo_body,
    make_agent_client, recheck_unquoted, review_finding, review_verdict, submit_agent,
    wait_status,
)

GERMAN_DIRECTIVE = (
    "Antworte IMMER auf Deutsch, unabhängig von der Sprache der "
    "Recherche-Ergebnisse oder der Anweisungen in diesem Prompt — "
    "in korrekter deutscher Rechtschreibung mit Umlauten (ä, ö, ü, ß), "
    "nie als ae/oe/ue/ss."
)
"""The language directive that opens every writing and review prompt of a
run whose intake classified the question as German (``PROFILE``)."""


def test_synthesis_preserves_full_query_once_and_keeps_source_passages_distinct():
    row = search()
    row["provider_answer"] = "Die Regel gilt. ([Quelle](https://example.org/a))\n\n" + "Kontext. " * 100 + "Nur nach vorheriger Registrierung."
    row["citations"].append({"url": "https://example.org/b"})
    refs = [
        {"label": "W1", "url": "https://example.org/a", "source_run_id": "run_evidence_reader", "grounded_support": "(example.org)"},
        {"label": "W2", "url": "https://example.org/b", "source_run_id": "run_evidence_reader", "excerpt": "Original mit eigener Einschränkung. " * 20},
    ]
    foreign = search("query_foreign", origin="run_other")
    foreign["provider_answer"] = "FOREIGN MATERIAL"
    unrelated = search("query_unrelated", url="https://example.org/unselected")
    unrelated["provider_answer"] = "UNSELECTED MATERIAL"
    ledger = {"searches": {r["query_id"]: r for r in (row, foreign, unrelated)}}
    before = deepcopy((refs, ledger))
    rendered = synthesis_evidence(refs, ledger)
    assert rendered.count(row["provider_answer"]) == 1
    assert refs[1]["excerpt"] in rendered
    assert "Stored source excerpt:\n" + refs[1]["excerpt"] in rendered
    assert "Stored search answer for [W1], [W2] (no original source excerpt)" in rendered
    assert "FOREIGN MATERIAL" not in rendered
    assert "UNSELECTED MATERIAL" not in rendered
    assert "(example.org)" not in rendered
    assert (refs, ledger) == before


def test_synthesis_without_retained_query_names_the_context_gap():
    rendered = synthesis_evidence([
        {"label": "W1", "url": "https://example.org/a", "grounded_support": "Nur unter Vorbehalt."},
        {"label": "K1", "document_id": "document", "excerpt": "Vollständiger Originalauszug."},
    ], {})
    assert "complete search answer not available" in rendered
    assert "Nur unter Vorbehalt." in rendered
    assert "Stored source excerpt:\nVollständiger Originalauszug." in rendered
    assert synthesis_evidence([], {}) == "(no evidence)"


class OutlineLLM:
    def __init__(self, labels, *, native=True, empty=False):
        self.native, self.schema, self.calls = native, None, 0
        self.payload = {"title": "Ergebnis", "sections": [] if empty else [
            {"title": "Bedeutung", "focus": "Einordnen", "criterion_ids": [], "evidence_labels": labels},
        ]}

    def supports_structured_output(self, **kwargs):
        return self.native

    def complete_structured(self, prompt, *, schema, **kwargs):
        self.calls += 1
        self.schema = schema
        return StructuredLLMResponse(parsed=self.payload, content=json.dumps(self.payload), prompt_tokens=3, completion_tokens=2)

    def complete_with_metadata(self, prompt, **kwargs):
        self.calls += 1
        return LLMResponse(content=json.dumps(self.payload), prompt_tokens=3, completion_tokens=2)


def outline(llm, labels):
    return run_outline(llm, question="Auftrag", success_criteria=[], evidence_digest="Belege",
                       known_labels=labels, model=None, reasoning_effort=None, timeout=1)


@pytest.mark.parametrize("labels", [["W7", "K2"], ["W41"], []])
def test_outline_schema_offers_exactly_the_current_reference_ids(labels):
    llm = OutlineLLM(labels)
    result = outline(llm, labels)
    assert isinstance(result.value, ReportOutline)
    field = llm.schema["$defs"]["ReportSection"]["properties"]["evidence_labels"]
    if len(labels) > 1:
        assert field["items"]["enum"] == labels
    elif labels:
        assert field["items"]["const"] == labels[0]
    else:
        assert field["maxItems"] == 0
    assert result.value.sections[0].evidence_labels == labels
    assert llm.calls == 1


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("known,returned,empty", [(["W1"], ["K1"], False), ([], ["W1"], False), (["W1"], [], True)])
def test_invalid_outline_is_rejected_without_an_extra_call(native, known, returned, empty):
    llm = OutlineLLM(returned, native=native, empty=empty)
    result = outline(llm, known)
    assert result.value is None
    assert result.marker == "_structured_fallback"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert llm.calls == 1


@pytest.mark.parametrize("deliverable", ["canvas", "chat"])
def test_retained_context_reaches_writing_review_and_revision(monkeypatch, deliverable):
    answer = "Die Ausgangslage ist vorläufig. ([Quelle](https://example.com/markt))\n\nEine endgültige Aussage setzt die spätere Kontrolle voraus."

    class RetainedSearch(FakeSearch):
        def search(self, query, **kwargs):
            self.queries.append(query)
            return GroundedSearchResult(answer=answer, sources=[GroundedSource(url="https://example.com/markt", title="Quelle")])

    outline_payload = deepcopy(OUTLINE)
    outline_payload["sections"] = [
        {**outline_payload["sections"][0], "title": title} for title in ("Bedeutung", "Grenze")
    ]

    def section(prompt):
        if "Write the memo section 'Grenze'." in prompt:
            return {"markdown": "Kontrolle bleibt erforderlich [W1]."}
        return {"markdown": "Die Aussage bleibt vorläufig [W1]."}

    def review(prompt):
        # The finding rests on the stored source the review was handed. That
        # source holds only the provider's context, on which an evidence
        # finding on its own passage marks instead of revising; the
        # revision is reached by an output requirement.
        basis = re.search(r'"basis_id": "(source:ref_[0-9a-f]+)"', prompt).group(1)
        return review_verdict(prompt, grounded=False, findings=[review_finding(
            prompt, "Die Aussage bleibt vorläufig [W1].",
            kind="output_requirement", finding="Die Bedingung der Quelle fehlt.", basis_ids=(basis,),
        )])

    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {**PROFILE, "response_form": deliverable},
        "ReportOutline": outline_payload,
        "SectionText": section,
        "DeepReviewVerdict": review,
        "DeepRevisionBundle": {"edits": [
            {"finding_id": "finding_1", "replacement": "Die Aussage bleibt bis zur Kontrolle vorläufig [W1]."},
        ]},
        "RevisionCheckVerdict": recheck_unquoted,
    })
    client = make_agent_client(monkeypatch, llm=llm, search=RetainedSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous", report_guidance="Genau zwei Abschnitte: Bedeutung und Grenze.")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        if deliverable == "canvas":
            memo = _memo_body(client, run_id)
            assert [line for line in memo.splitlines() if line.startswith("## ")] == ["## Bedeutung", "## Grenze"]
            assert "Die Aussage bleibt bis zur Kontrolle vorläufig [W1]." in memo
    writing = [(name, prompt) for name, prompt in llm.prompts if name in {"ReportOutline", "SectionText"}]
    # Written once: the review replaces a passage, it never asks the writer again.
    assert len(writing) == (3 if deliverable == "canvas" else 1)
    assert all(answer in prompt for _, prompt in writing)
    assert all(prompt.count(answer) == 1 for _, prompt in writing)
    assert all("Genau zwei Abschnitte: Bedeutung und Grenze." in prompt for _, prompt in writing)
    assert llm.calls.count("structured:DeepReviewVerdict") == 1
    reviewed = next(prompt for name, prompt in llm.prompts if name == "DeepReviewVerdict")
    assert reviewed.count(answer) == 1
    assert "no original source excerpt" in reviewed
    assert reviewed.startswith(f"{GERMAN_DIRECTIVE}\n\nROLE ASSIGNMENT (from the Inqtrix runtime, not from the user)")
    assert "Erstelle eine Marktanalyse." in reviewed
    assert "Genau zwei Abschnitte: Bedeutung und Grenze." in reviewed
    diagnostics = json.loads(
        reviewed.split('<untrusted_content source="run-diagnostics">\n', 1)[1]
        .split("\n</untrusted_content>", 1)[0]
    )
    execution = next(
        item["values"] for item in diagnostics if item["basis_id"].endswith(":execution")
    )
    assert execution["plan_status"] == "approved"
    assert [(task["tool_kind"], task["status"]) for task in execution["tasks"]] == [
        ("web_instant", "completed"), ("synthesis", "completed"),
    ]
    # The fixture runs one discovery probe before its one planned search.
    assert execution["source_operations_including_discovery"] == {"web": 2, "knowledge": 0}
    assert execution["discovery_operations"] == {"web": 1, "knowledge": 0}
    # The revision receives the material of exactly the basis the finding names.
    revision = next(prompt for name, prompt in llm.prompts if name == "DeepRevisionBundle")
    assert answer in json.loads(
        revision.split('<untrusted_content source="review-basis">\n', 1)[1]
        .split("\n</untrusted_content>", 1)[0]
    )["searches"].popitem()[1]["provider_answer"]


def test_a_mission_review_and_revision_carry_the_clarification_answer(monkeypatch):
    """The mission's review checks the run's procedure against the server's
    record of it. After an answered clarification, the review reads the
    question, the chosen option and the user's free text in the record's
    own fence; the revision reads question and option fenced and the free
    text after the record, as the user's requirement."""
    from tests.agents.test_review_pass import _fenced

    question = "Soll der Bericht als Tabelle oder als Aufzählung kommen?"
    found: list[str] = []

    def review(prompt: str) -> dict[str, Any]:
        found.append(re.search(r"Die Marktlage ist stabil \[[KW]\d+\]\.", prompt).group(0))
        return review_verdict(prompt, grounded=False, findings=[
            review_finding(prompt, found[0], finding="Die Fristentabelle fehlt."),
        ])

    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {
            **PROFILE,
            "scope_clarity": "ambiguous",
            "content_questions": [],
            "clarification_questions": [{
                "prompt": question,
                "options": [
                    {"label": "Als Tabelle", "description": ""},
                    {"label": "Als Aufzählung", "description": ""},
                ],
                "multi_select": False,
                "subject": "assignment",
                "default_assumption": "Der Bericht kommt als Tabelle.",
            }],
        },
        "DeepReviewVerdict": review,
        "DeepRevisionBundle": lambda prompt: {"edits": [
            {"finding_id": "finding_1", "replacement": f"## Fristen\n\n{found[0]}"},
        ]},
        "RevisionCheckVerdict": recheck_unquoted,
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"waiting_for_input"})
        row = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o2"], "text": "mit Fristentabelle"}}},
        )
        assert answered.status_code == 200, answered.text
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    reviewed = next(prompt for name, prompt in llm.prompts if name == "DeepReviewVerdict")
    revision = next(prompt for name, prompt in llm.prompts if name == "DeepRevisionBundle")
    entry = {
        "number": 1, "round": 1, "question": question,
        "chosen_options": ["Als Aufzählung"], "status": "answered", "default_assumption": "",
    }
    assert json.loads(_fenced(reviewed, "run-clarifications")) == [
        {**entry, "free_text": "mit Fristentabelle"},
    ]
    assert json.loads(_fenced(revision, "run-clarifications")) == [entry]
    after_record = revision.split('<untrusted_content source="run-clarifications">', 1)[1]
    between = after_record.split("</untrusted_content>", 1)[1].split("Output bundle so far:", 1)[0]
    assert "mit Fristentabelle" in between
    assert "<untrusted_content" not in between


def test_a_mission_writes_with_the_clarification_answer(monkeypatch):
    """The outline and every section are written with the run's
    clarifications: the question and the chosen option in the record's own
    fence, the user's free text after it, outside the fence, as the user's
    requirement. The estimate that picks a section's transport measures
    exactly the prompt the section is then written from."""
    from inqtrix.agents import synthesis
    from tests.agents.test_review_pass import _fenced

    question = "Soll der Bericht als Tabelle oder als Aufzählung kommen?"
    estimated: list[str] = []
    choose = synthesis.choose_section_transport

    def estimate(prompt: str, **kwargs: Any) -> str:
        estimated.append(prompt)
        return choose(prompt, **kwargs)

    monkeypatch.setattr(synthesis, "choose_section_transport", estimate)
    llm = ScriptedLLM(overrides={
        "AssignmentProfile": {
            **PROFILE,
            "scope_clarity": "ambiguous",
            "content_questions": [],
            "clarification_questions": [{
                "prompt": question,
                "options": [
                    {"label": "Als Tabelle", "description": ""},
                    {"label": "Als Aufzählung", "description": ""},
                ],
                "multi_select": False,
                "subject": "assignment",
                "default_assumption": "Der Bericht kommt als Tabelle.",
            }],
        },
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"waiting_for_input"})
        row = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o2"], "text": "mit Fristentabelle"}}},
        )
        assert answered.status_code == 200, answered.text
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    outlines = [prompt for name, prompt in llm.prompts if name == "ReportOutline"]
    sections = [prompt for name, prompt in llm.prompts if name == "SectionText"]
    assert (len(outlines), len(sections), len(estimated)) == (1, 2, 2)
    record = [{
        "number": 1, "round": 1, "question": question,
        "chosen_options": ["Als Aufzählung"], "status": "answered", "default_assumption": "",
    }]
    opened = '<untrusted_content source="run-clarifications">'
    assert [
        json.loads(_fenced(prompt, "run-clarifications") or "null") == record
        and "\n- 1: mit Fristentabelle"
        in prompt.partition(opened)[2].partition("</untrusted_content>")[2].split("\n\n", 1)[0]
        for prompt in (*outlines, *sections, *estimated)
    ] == [True] * 5
    assert sorted(estimated) == sorted(sections)


def test_invalid_outline_closes_synthesis_without_fabricating_sections(monkeypatch):
    bad = deepcopy(OUTLINE)
    bad["sections"][0]["evidence_labels"] = ["K999"]
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(overrides={"ReportOutline": bad}))
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "failed"
        tasks = client.get(f"/v1/runs/{run_id}/plan").json()["tasks"]
        assert next(t for t in tasks if t["tool_kind"] == "synthesis")["status"] == "failed"
    assert "structured:SectionText" not in client.llm.calls


def test_a_negative_verdict_that_names_nothing_neither_passes_nor_revises(monkeypatch):
    llm = ScriptedLLM(overrides={
        "DeepReviewVerdict": lambda prompt: review_verdict(prompt, grounded=False),
    })
    client = make_agent_client(monkeypatch, llm=llm)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed"
        events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
    unavailable = [
        e["data"] for e in events
        if e["type"] == "inqtrix.agent.activity" and e["data"].get("kind") == "critic_unavailable"
    ]
    assert [item["reason"] for item in unavailable] == ["inconsistent"]
    kinds = [e["data"].get("kind") for e in events if e["type"] == "inqtrix.agent.activity"]
    assert "critic_revise" not in kinds
    assert llm.calls.count("structured:DeepReviewVerdict") == 1
    assert llm.calls.count("structured:SectionText") == 2



def test_a_section_without_a_preselection_still_sees_the_run_s_evidence(monkeypatch):
    """The outline's choice is where a section LEADS, not what exists.

    The outline is asked for "the smallest sufficient, non-redundant
    evidence set" and its schema invites an empty list, so an empty answer
    means "I preselected none" - never "there are none". Handing such a
    section an empty digest AND telling it nothing is citable turns that
    economy into a wall: the section then reports that no sources were
    available while the run's ledger held them.
    """
    outline = json.loads(json.dumps(OUTLINE))
    # The second section is the one the outline leaves without evidence.
    outline["sections"][1]["evidence_labels"] = []
    llm = ScriptedLLM(overrides={"ReportOutline": lambda prompt: outline})
    client = make_agent_client(monkeypatch, llm=llm, search=FakeSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
    sections = [prompt for name, prompt in llm.prompts if name == "SectionText"]
    assert len(sections) == len(outline["sections"])
    without = [
        prompt for prompt in sections
        if "The outline preselected no evidence for this section." in prompt
    ]
    assert len(without) == 1, "the fixture must leave exactly one section unpreselected"
    prompt = without[0]
    # It is told what it may reach for, and that the preselection is not a wall.
    assert "Evidence register of this run" in prompt
    assert "starting point, not a limit" in prompt
    assert re.search(r"\[[KW]\d+\]", prompt.split("Evidence register of this run", 1)[1])
    # And never the sentence that made it write "no sources available".
    assert "(no evidence)" not in prompt


def test_a_section_may_cite_a_label_its_outline_entry_never_named(monkeypatch):
    """Permeability is the point: seeing the register must mean citing it.

    A section that leads with nothing is still allowed to reach into the
    run's evidence. If the citable set stayed the outline's preselection,
    the register would be a list the section is shown and then punished
    for using - the repair pass would strip the citation and the reader
    would get the same unsupported prose as before.
    """
    outline = json.loads(json.dumps(OUTLINE))
    outline["sections"][1]["evidence_labels"] = []

    def section(prompt: str) -> dict[str, str]:
        # The unpreselected section cites a label only the register offers.
        register = prompt.split("Evidence register of this run", 1)
        if "The outline preselected no evidence for this section." in prompt:
            label = re.search(r"\[([KW]\d+)\]", register[1]).group(1)
            return {"markdown": f"Auch hier ist die Lage stabil [{label}]."}
        return {"markdown": "Die Marktlage ist stabil [W1]."}

    llm = ScriptedLLM(overrides={
        "ReportOutline": lambda prompt: outline,
        "SectionText": section,
    })
    client = make_agent_client(monkeypatch, llm=llm, search=FakeSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        memo = _memo_body(client, run_id)
    # The reached-for citation survives into the delivered memo, with its
    # label intact - no repair pass stripped it as unknown.
    assert re.search(r"Auch hier ist die Lage stabil \[[KW]\d+\]\.", memo), memo


def test_a_run_without_evidence_does_not_report_a_withheld_register(monkeypatch):
    """Nothing withheld where there was nothing to withhold.

    The degraded signal means "the outline preselected nothing although the
    run held evidence". A run that holds none cannot have withheld any, and
    a signal that fires there points at nothing - the rendered register is
    a sentinel string when empty, and a sentinel string is true.
    """
    outline = json.loads(json.dumps(OUTLINE))
    for entry in outline["sections"]:
        entry["evidence_labels"] = []
    class EmptySearch(FakeSearch):
        """A search that finds nothing, so the run holds no reference."""

        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            self.queries.append(query)
            return GroundedSearchResult(answer="", sources=[])

    llm = ScriptedLLM(overrides={"ReportOutline": lambda prompt: outline})
    client = make_agent_client(monkeypatch, llm=llm, search=EmptySearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        wait_status(client, run_id, {"completed", "failed"})
        events = client.get(
            f"/v1/runs/{run_id}/events", params={"format": "json"}
        ).json()["data"]
    rows = [
        item["data"] for item in events
        if item["type"] == "inqtrix.agent.activity"
        and item["data"].get("kind", "").startswith("section")
    ]
    assert rows, "the synthesis reported no section evidence at all"
    assert all(row["kind"] == "section_evidence" for row in rows), rows
    assert all(row["status"] == "completed" for row in rows), rows


class WideSearch(FakeSearch):
    """A search that finds many distinct sources, so one run holds more
    references than the narrowest register lets a section reach for."""

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        self.queries.append(query)
        return GroundedSearchResult(
            answer="Web-Vorschau",
            sources=[
                GroundedSource(
                    url=f"https://example.com/quelle-{index}",
                    title=f"Quelle {index}",
                    snippet=f"Befund {index}.",
                )
                for index in range(30)
            ],
        )


def _register_labels(prompt: str) -> list[str]:
    """The labels of the one-line register a section prompt lists, in order."""
    head = "cite any register label whose gist carries your statement.\n"
    if head not in prompt:
        return []
    block = prompt.split(head, 1)[1].split("\nQuote verbatim only", 1)[0]
    return re.findall(r"^\[([KW]\d+)\]", block, re.MULTILINE)


def _section_prompts(monkeypatch, *, tier: str, platform=None) -> list[str]:
    """Section prompts of one canvas mission at ``tier`` over a wide ledger."""
    llm = ScriptedLLM()
    client = make_agent_client(
        monkeypatch, llm=llm, search=WideSearch(), platform=platform
    )
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle eine Marktanalyse.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
                # A canvas is what gives a mission sections at all; schnell
                # defaults to a chat answer and writes them only on request.
                "response_form": "canvas",
                "agent_overrides": {"agent_tier": tier},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
    return [prompt for name, prompt in llm.prompts if name == "SectionText"]


def test_how_far_a_section_may_reach_follows_the_run_s_stufe(monkeypatch):
    """A Stufe that goes deeper lets its sections reach further.

    The register is what a section may cite, so its width is a quality
    lever of the Stufe - one operator constant for every depth would give
    a fast run the reach of a deep one, or a deep run the reach of a fast
    one. The ledger here holds more references than the narrowest Stufe
    allows, so the cut is observable.
    """
    from inqtrix.agents.tier_policy import TIER_POLICIES

    narrow = TIER_POLICIES["schnell"].section_register
    fast = _section_prompts(monkeypatch, tier="schnell")
    thorough = _section_prompts(monkeypatch, tier="gruendlich")
    assert fast and thorough
    fast_widths = {len(_register_labels(prompt)) for prompt in fast}
    thorough_widths = {len(_register_labels(prompt)) for prompt in thorough}
    # The fast Stufe is cut to its published width ...
    assert fast_widths == {narrow}, fast_widths
    # ... and the thorough one, over the same ledger, is not.
    assert min(thorough_widths) > narrow, thorough_widths


def test_a_register_switched_off_is_empty_not_the_whole_ledger(monkeypatch):
    """Off means off.

    The ranking the register shares with the full-text digest reads a
    budget of 0 as "no cap". Handing it the operator's 0 therefore gave
    every section the WHOLE ledger - the setting documented as switching
    the register off did the opposite of its name.
    """
    from inqtrix.settings import AgentPlatformSettings

    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET=0,
    )
    prompts = _section_prompts(monkeypatch, tier="tief", platform=platform)
    assert prompts
    assert all(_register_labels(prompt) == [] for prompt in prompts)
    assert all("Evidence register of this run" not in prompt for prompt in prompts)


def test_an_operator_width_replaces_every_stufe(monkeypatch):
    """A number set by the operator is the width, whatever the Stufe says."""
    from inqtrix.settings import AgentPlatformSettings

    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET=3,
    )
    prompts = _section_prompts(monkeypatch, tier="tief", platform=platform)
    assert prompts
    assert {len(_register_labels(prompt)) for prompt in prompts} == {3}


def test_a_register_switched_off_still_reports_the_outline_s_gap(monkeypatch):
    """The gap is the outline's, not the register's.

    "The outline preselected nothing although the run held evidence" is a
    fact about the run's ledger. With the register switched off the section
    reaches for nothing at all - the gap is wider, and it must not go quiet.
    """
    from inqtrix.settings import AgentPlatformSettings

    outline = json.loads(json.dumps(OUTLINE))
    outline["sections"][1]["evidence_labels"] = []
    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET=0,
    )
    llm = ScriptedLLM(overrides={"ReportOutline": lambda prompt: outline})
    client = make_agent_client(monkeypatch, llm=llm, search=FakeSearch(), platform=platform)
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
    rows = [
        item["data"] for item in events
        if item["type"] == "inqtrix.agent.activity"
        and item["data"].get("kind", "").startswith("section")
    ]
    gap = [row for row in rows if row["label"] == outline["sections"][1]["title"]]
    assert gap and gap[0]["kind"] == "section_without_preselection", rows
    assert gap[0]["status"] == "degraded"


def test_a_register_cut_names_its_own_limit_and_a_chat_answer_cuts_none(monkeypatch, caplog):
    """A cut must point at the knob that made it.

    The register shares its ranking with the digest; reported under the
    digest's name, a register cut sent an operator to the wrong setting. And
    a chat answer writes no section, so it has no register to cut.
    """
    import logging

    from inqtrix.settings import AgentPlatformSettings

    platform = AgentPlatformSettings(
        INQTRIX_AGENT_ALLOW_VOLATILE=True,
        INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET=3,
    )

    def warnings_of(response_form: str) -> list[str]:
        caplog.clear()
        client = make_agent_client(monkeypatch, llm=ScriptedLLM(), search=WideSearch(), platform=platform)
        with client, caplog.at_level(logging.WARNING, logger="inqtrix"):
            response = client.post("/v1/runs", json={
                "question": "Erstelle eine Marktanalyse.", "mode": "workspace_agent",
                "autonomy": "autonomous", "response_form": response_form,
            })
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            assert wait_status(client, run_id, {"completed", "failed"})["status"] == "completed"
        return [record.getMessage() for record in caplog.records if "rank_evidence" in record.getMessage()]

    canvas = warnings_of("canvas")
    assert any("capped section_register to 3 of" in message for message in canvas), canvas
    assert not any("synthesis_evidence_budget to 3 " in message for message in canvas), canvas
    chat = warnings_of("chat")
    assert not any("section_register" in message for message in chat), chat


# The writer guard of the mission engine. The evidence cards name an
# internal document by title, but an excerpt or a role's provenance read can
# still carry an id; the guard is what keeps it out of the reader's text.


def _knowledge_section_material():
    """One knowledge reference as the mission engine actually builds it.

    Built by ``merge_evidence`` from a document analysis: a label, the
    document identity - and NO ``reference_id``. That absence is the defect
    this guard site had.
    """
    return [{
        "label": "K1",
        "document_id": "doc-42",
        "chunk_index": 0,
        "title": "Interner Marktbericht",
        "excerpt": "Die Marktlage ist stabil.",
        "tasks": ["t1"],
    }]


def test_mission_section_guard_catches_a_document_id_in_prose():
    """A section that writes the document id it was shown is a leak.

    A mission reference has no ``reference_id``, so a guard that read only
    that field would check every section against a list of empty strings
    and let a copied document id reach the reader unnoticed.
    """
    from inqtrix.agents import algorithm
    from inqtrix.agents.report_quality import introduced_identifiers

    state = {"question": "Wie ist die Marktlage?", "report_guidance": ""}
    guard = algorithm._writer_guard(state, _knowledge_section_material())
    prose = "Die Marktlage ist stabil [K1] (doc-42)."

    assert "doc-42" in guard["internal_reference_ids"]
    assert introduced_identifiers(
        prose, guard["internal_reference_ids"], authored_by=guard["authored_by"]
    ) == ["doc-42"]
    # The label the reader is supposed to see is never an internal id.
    assert "K1" not in guard["internal_reference_ids"]


def test_an_id_the_user_wrote_reaches_the_mission_guard_from_every_turn():
    """The user's words of THIS turn and of the conversation both count.

    ``history`` carries the user's earlier turns verbatim, and a canvas
    passage the run preserves is the user's own text as well. An id in any
    of them was not introduced by this model call.
    """
    from inqtrix.agents import algorithm
    from inqtrix.agents.report_quality import introduced_identifiers

    references = _knowledge_section_material()
    prose = "Die Marktlage ist stabil [K1] (doc-42)."
    for key, text in (
        ("question", "Siehe doc-42."),
        ("report_guidance", "Siehe doc-42."),
        ("clarified_context", "Siehe doc-42."),
        ("memo_user_prefix", "Siehe doc-42."),
        # The transcript's own shapes, not a hand-made line: the user's
        # turn, and the ANSWER half of a clarification round.
        ("history", "User: Siehe doc-42.\nAgent: verstanden."),
        ("history", "Clarification: Welches Dokument? — Answer: doc-42 bitte"),
        ("history", "Nutzer: Siehe doc-42."),
    ):
        guard = algorithm._writer_guard({"question": "", key: text}, references)
        assert introduced_identifiers(
            prose, guard["internal_reference_ids"], authored_by=guard["authored_by"]
        ) == [], (key, text)


def test_the_models_own_words_never_excuse_an_identifier():
    """The guard's haystack is the USER's text, and a transcript is not.

    A transcript carries both voices. Reading it whole would let one id the
    model leaked - into its own answer, into its own clarification question
    - exempt every later copy of itself, for the rest of the session: the
    guard would certify exactly what it exists to catch.
    """
    from inqtrix.agents import algorithm
    from inqtrix.agents.report_quality import introduced_identifiers

    references = _knowledge_section_material()
    prose = "Die Marktlage ist stabil [K1] (doc-42)."
    for key, text in (
        ("memo_markdown", "Siehe doc-42."),
        ("history", "Agent: Laut doc-42 ist die Lage stabil."),
        ("history", "Assistent: Laut doc-42 ist die Lage stabil."),
        ("history", "Clarification: Meinst du doc-42? — Answer: ja"),
        # A continuation line belongs to the turn above it - including
        # after a user turn ended, which is the only way to tell the two
        # readings apart.
        ("history", "User: ok\nAgent: Die Lage ist stabil.\nBeleg: doc-42."),
    ):
        guard = algorithm._writer_guard({"question": "", key: text}, references)
        assert introduced_identifiers(
            prose, guard["internal_reference_ids"], authored_by=guard["authored_by"]
        ) == ["doc-42"], (key, text)


def test_every_mission_writing_call_is_given_the_guard(monkeypatch):
    """The section writer receives both guard arguments, not just the ids.

    A guard argument built at the site proves nothing until the site hands
    it over: the authored_by half decides whether a correct answer goes
    into the repair round.
    """
    from inqtrix.agents import synthesis

    seen: list[dict[str, Any]] = []
    real_write = synthesis.write_section

    def spy(llm: Any, **kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return real_write(llm, **kwargs)

    monkeypatch.setattr(synthesis, "write_section", spy)
    client = make_agent_client(monkeypatch, llm=ScriptedLLM(), search=FakeSearch())
    with client:
        run_id = submit_agent(client, autonomy="autonomous")
        summary = wait_status(client, run_id, {"completed", "failed"})
    assert summary["status"] == "completed", summary
    assert seen, "no section was written"
    for call in seen:
        assert call["internal_reference_ids"], call["internal_reference_ids"]
        assert summary["question"] in call["authored_by"]


def test_writer_cards_name_internal_documents_without_their_id():
    """The card names an internal document by title, never by address.

    ``Source: {url or document_id}`` put the exact identifier the writer
    guard then has to keep out of the prose in front of the model. A web
    source keeps its URL - there the address IS what a reader looks up.
    """
    from inqtrix.agents.evidence import evidence_digest

    internal = {
        "label": "K1", "document_id": "doc-42", "chunk_index": 0,
        "url": "inqtrix://documents/doc-42#chunk-0",
        "title": "Interner Marktbericht", "excerpt": "Die Lage ist stabil.",
    }
    untitled = {
        "label": "K2", "document_id": "doc-99", "chunk_index": 0,
        "url": "inqtrix://documents/doc-99#chunk-0",
    }
    web = {"label": "W1", "url": "https://example.org/a", "title": "Marktbericht"}

    card = synthesis_evidence([internal, untitled, web], {})
    assert "[K1] Interner Marktbericht" in card
    assert "[K2] untitled document" in card
    assert "doc-42" not in card and "doc-99" not in card
    assert "inqtrix://" not in card
    # The web card keeps its reader-facing identity.
    assert "Source: https://example.org/a" in card

    # The register line beside the card follows the same rule: a reference
    # without a passage falls back to its NAME, not to its address.
    register = evidence_digest([internal, untitled, web])
    assert "[K2] untitled document" in register
    assert "doc-99" not in register and "inqtrix://" not in register
    assert "[W1] Marktbericht" in register
