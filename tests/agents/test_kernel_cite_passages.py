"""Read document text becomes citable in the kernel.

A window read with ``read_project_document`` carries no evidence label. The
kernel turns passages it read into evidence only through the server's
verbatim check of ``knowledge.document.cite``, under the label a search hit on
the same section carries, only within what this run actually read, and the
review learns what the run read from the run's coverage ledger.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import ToolMessage

from inqtrix.agents.context_sources import (
    MATERIAL_DEPENDENCIES,
    SOURCE_COVERAGE,
    SourceCoverageLedger,
    SourceCoverageRecord,
    coverage_records_from_artifact,
)
from inqtrix.agents.kernel.algorithm import _checkpointed_tool_use_counts
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.kernel.evidence_read import own_reading_outputs, run_reading_diagnostics
from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.agents.prompts import build_agent_kernel_system_prompt
from inqtrix.agents.source_policy import kernel_tool_allowed
from inqtrix.core.results import SourcePolicy

from tests.agents.test_source_coverage import _call, _reader_deps
from tests.test_capabilities import ANON, _cite_fixture

QUOTE = "Paragraph 3 sentence 7 describes the review step 37."


def _setup():
    service, registry, document, _window = _cite_fixture()
    deps = _reader_deps(capability_registry=registry, capability_context=ANON)
    tools = {tool.name: tool for tool in build_kernel_tools()}
    return service, document, deps, tools


def _read(deps: KernelDeps, tools, document_id: str):
    """One window read; the kernel loop feeds the run's ledger from its artifact."""
    message = tools["read_project_document"].invoke(
        _call("read_project_document", {"document_id": document_id}, "call_read")
    )
    deps.source_coverage.absorb_many(coverage_records_from_artifact(message.artifact))
    (record,) = message.artifact[SOURCE_COVERAGE]
    return record


def _cite(tools, document_id: str, version: str, start: int, end: int, passages, call_id="call_cite"):
    return tools["cite_document_passages"].invoke(_call("cite_document_passages", {
        "document_id": document_id, "version": version,
        "window_start": start, "window_end": end, "passages": list(passages),
    }, call_id))


def _holder(service, document, quote: str):
    chunks = asyncio.run(service.document_chunks(document.id))
    return next(chunk for chunk in chunks if quote in chunk.source_text)


def test_a_read_passage_becomes_the_evidence_a_search_hit_on_its_section_is():
    service, document, deps, tools = _setup()
    record = _read(deps, tools, document.id)
    start, end = record["read_ranges"][0]
    cited = _cite(tools, document.id, record["version"], start, end, [QUOTE])

    [ref] = list(deps.evidence_refs.values())
    holder = _holder(service, document, QUOTE)
    assert (ref["document_id"], ref["chunk_index"]) == (document.id, holder.chunk_index)
    assert QUOTE in ref["excerpt"]
    assert cited.content.splitlines()[0] == (
        f"1. Evidence [{ref['label']}] — reference_id: {ref['reference_id']}"
    )
    # The identity of a search hit on the same section: no second label.
    [as_hit] = deps.register_references([{
        "document_id": document.id, "chunk_index": holder.chunk_index,
        "title": document.title, "excerpt": holder.source_text,
    }])
    assert (as_hit["reference_id"], as_hit["label"]) == (ref["reference_id"], ref["label"])
    assert len(deps.evidence_refs) == 1
    assert cited.artifact[MATERIAL_DEPENDENCIES] == {
        "knowledge_documents": [{"document_id": document.id, "collection_id": document.collection_id}],
    }


def test_only_what_this_run_read_is_citable():
    """A quote that is verbatim but was never transferred to this run stays
    unlabelled: citing is part of reading, not a way around it."""
    _service, document, deps, tools = _setup()
    unread = _cite(tools, document.id, "v-unknown", 0, 50, [QUOTE])
    assert unread.content.startswith("Not read in this run:")
    record = _read(deps, tools, document.id)
    start, end = record["read_ranges"][0]
    other_version = _cite(tools, document.id, "v-other", start, end, [QUOTE], "call_other")
    beyond = _cite(tools, document.id, record["version"], start, end + 1, [QUOTE], "call_beyond")
    assert other_version.content.startswith("Not read in this run:")
    assert beyond.content.startswith("Not read in this run:")
    assert deps.evidence_refs == {}


def test_each_passage_that_cannot_be_cited_names_why():
    service, document, deps, tools = _setup()
    record = _read(deps, tools, document.id)
    start, end = record["read_ranges"][0]
    chunks = sorted(asyncio.run(service.document_chunks(document.id)), key=lambda c: c.chunk_index)
    boundary = chunks[0].source_end
    straddle = document.text[boundary - 30: chunks[1].source_start + 30]
    cited = _cite(tools, document.id, record["version"], start, end, [
        QUOTE,
        "two words",
        "Paragraph 3 sentence 7 explains the review step 37.",
        "word " * 300,
        straddle,
    ])
    lines = cited.content.splitlines()
    assert lines[0].startswith("1. Evidence [")
    assert lines[1].startswith("2. Not citable: too short")
    assert lines[2].startswith("3. Not citable: not verbatim in that window")
    assert lines[3].startswith("4. Not citable: longer than 1200 characters")
    assert lines[4].startswith("5. Not citable: it crosses a section boundary")
    assert len(deps.evidence_refs) == 1


def test_citing_follows_the_permission_to_read():
    _service, document, deps, tools = _setup()
    record = _read(deps, tools, document.id)
    start, end = record["read_ranges"][0]
    disabled = SourcePolicy(web="disabled", knowledge="disabled")
    assert not kernel_tool_allowed("cite_document_passages", policy=disabled)
    deps.allowed_tool_union = {"search_project_knowledge"}
    blocked = _cite(tools, document.id, record["version"], start, end, [QUOTE])
    assert blocked.content.startswith("Tool blocked")
    deps.allowed_tool_union = {"read_project_document"}
    allowed = _cite(tools, document.id, record["version"], start, end, [QUOTE], "call_allowed")
    assert allowed.content.startswith("1. Evidence [")


def test_a_citation_spends_no_source_budget():
    """A citation labels text the run already read; it reaches no new source."""
    _service, document, deps, tools = _setup()
    record = _read(deps, tools, document.id)
    start, end = record["read_ranges"][0]
    before = dict(deps.tool_use_counts)
    cited = _cite(tools, document.id, record["version"], start, end, [QUOTE])
    assert deps.tool_use_counts == before
    snapshot = SimpleNamespace(values={"messages": [
        ToolMessage(content=cited.content, name="cite_document_passages", tool_call_id="call_cite"),
    ]})
    assert _checkpointed_tool_use_counts(snapshot) == {"web": 0, "knowledge": 0}


def test_the_kernel_is_told_that_read_text_needs_its_own_citation():
    prompt = build_agent_kernel_system_prompt()
    assert (
        "Text from read_project_document carries no evidence label: to cite it, pass "
        "the exact passages to cite_document_passages and cite only the label it "
        "returns for each; never cite a search hit's label for text its excerpt does "
        "not contain."
    ) in prompt


# -- what the run read, for its review ---------------------------------------- #

def _ledger(*records: SourceCoverageRecord) -> list[dict]:
    ledger = SourceCoverageLedger()
    ledger.absorb_many(records)
    return ledger.entries()


def test_the_review_learns_what_the_run_read_of_each_source():
    entries = _ledger(
        SourceCoverageRecord(kind="knowledge_document", source_id="kd_full", version="v1",
                             total=50_000, read_ranges=[(0, 20_000), (19_000, 50_000)]),
        SourceCoverageRecord(kind="knowledge_document", source_id="kd_part", version="v2",
                             total=40_000, read_ranges=[(0, 20_000)]),
        SourceCoverageRecord(kind="research_report", source_id="run_report", version="r",
                             total=900, read_ranges=[(0, 900)]),
        SourceCoverageRecord(kind="editor_document", source_id="ed_scan", version="e",
                             total=1_000, scanned_ranges=[(0, 1_000)]),
    )
    full, part = run_reading_diagnostics(
        "run_abc", entries, titles={"kd_full": "Handbuch"}, language="de",
    )
    assert full["basis_id"].startswith("diagnostic:run_abc:reading:knowledge_document:kd_full:")
    assert full["source_run_id"] == "run_abc" and full["scope"] == "run_reading"
    assert full["title"] == "Lesestand dieses Laufs für ‚Handbuch‘"
    assert full["values"] == {
        "source": "knowledge_document", "version": "v1", "unit": "chars", "total": 50_000,
        "read_ranges": [[0, 50_000]], "unread_ranges": [], "read_completely": True,
    }
    assert part["values"]["read_completely"] is False
    assert part["values"]["unread_ranges"] == [[20_000, 40_000]]
    assert part["title"] == "Lesestand dieses Laufs für ‚kd_part‘"
    assert part["basis_id"] != full["basis_id"]


def test_the_runs_reading_belongs_to_what_the_run_wrote():
    """A delegated mission's memo has its own reading record; the kernel's
    reading must not stand in for it."""
    canvases = {
        "art_own": SimpleNamespace(run_id="run_abc"),
        "art_mission": SimpleNamespace(run_id="run_mission"),
    }
    assert own_reading_outputs(["chat", "art_own", "art_mission"], canvases, run_id="run_abc") == {
        "chat", "art_own",
    }
