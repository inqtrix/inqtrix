"""Drift tripwire for the rendering-capabilities source of truth.

The block tells the model what the FRONTEND renderer actually supports
(MarkdownRenderer.tsx: remark-gfm, rehype-katex, bounded Shiki highlighting,
MermaidFigure). If a capability is ever removed from the renderer, this
test is the reminder to update BOTH places — and if someone rewords the
block, the consuming prompts must keep carrying it.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.prompts import (
    agent_answer_system_prompt,
    DEEP_REVIEW_RUBRIC,
    build_agent_kernel_system_prompt,
    build_deep_revision_prompt,
    build_review_call_system_prompt,
    build_work_profile_system_prompt,
)
from inqtrix.prompt_language import OUTPUT_FORMAT_BLOCK

RENDERER_FEATURES = ("GitHub-flavoured Markdown", "Tables", "Math", "Code", "mermaid")


@pytest.mark.parametrize("prompt", [
    agent_answer_system_prompt(),
    build_agent_kernel_system_prompt(),
    build_deep_revision_prompt("Compare evidence", "Output", []),
    build_review_call_system_prompt(),
    build_work_profile_system_prompt(DEEP_REVIEW_RUBRIC),
], ids=["answer", "kernel", "revision", "review_call", "review_role"])
def test_agent_writers_preserve_the_limits_of_their_evidence(prompt):
    assert "Preserve the conditions, scope and time reference" in prompt
    assert "A conditional statement is not a finding that its conditions are met." in prompt
    assert "A rule or a deadline is no proof of a current state." in prompt
    assert "Source roles, assessment labels and valid citations do not replace substantive support." in prompt
    assert "Keep the trigger of a deadline and the concrete scope of a rule." in prompt
    assert "Distinguish the actual speaker, the publisher and the document status." in prompt
    assert "A search provider's answer is a synthesis, not original page text." in prompt
    assert "Run-internal evidence notes rest on run data, not on external sources." in prompt
    assert "invent no dates, commitments or obligations." in prompt


def test_rendering_block_names_every_renderer_feature():
    block = OUTPUT_FORMAT_BLOCK
    for feature in RENDERER_FEATURES:
        assert feature in block, feature
    # Explicit non-capabilities stay named (skipHtml is on, no emojis).
    assert "no HTML" in block
    assert "no emojis" in block


def test_rendering_block_states_usage_rules():
    """The block carries RULES, not just a feature list — the table
    trigger, the no-decoration constraint, the plot bounds and the
    precedence of explicit format instructions."""
    block = OUTPUT_FORMAT_BLOCK
    assert "use a GFM table" in block
    assert "Nothing decorative" in block
    # Bounded data plots are promised (bars or lines only), and the
    # grammar list names every promised diagram type.
    assert "sequenceDiagram, classDiagram, stateDiagram-v2, erDiagram, gantt, pie, xychart-beta." in block
    assert "Plots: an xychart-beta diagram" in block
    assert "bars or lines only" in block
    assert "No horizontal rules" in block
    assert "its second line `accTitle: <short title>`" in block
    # In the project's own mermaid version a bracket inside a flowchart
    # node label is a PARSE ERROR (`A[v(t)]`), while
    # comma, colon, hyphen, slash, percent and umlauts parse fine — and
    # `sequenceDiagram` with `v(t)` is valid. The rule is therefore scoped
    # to flowchart labels and names brackets, nothing else. Without it the
    # model writes the natural `w(t)` and every such diagram breaks with a
    # visible parser error in the middle of the answer.
    assert "must be in double quotes" in block
    assert 'A["v(t)"]' in block
    assert '"120 Euro" stays "120 Euro", "$5" becomes "USD 5"' in block
    assert "override these defaults" in block


def test_the_answer_prompt_carries_the_block():
    assert OUTPUT_FORMAT_BLOCK in agent_answer_system_prompt()


def test_the_answer_prompt_requires_a_small_nonredundant_citation_set():
    prompt = agent_answer_system_prompt()
    assert "smallest sufficient" in prompt
    assert "non-redundant" in prompt


# --- sources appear exactly once (the curated list) --------------------------


def test_no_model_source_sections_is_word_identical_on_every_surface():
    """THE prohibition sentence rides every answer surface verbatim —
    chat answer, kernel system prompt and deep revision. A reworded copy
    on one surface is drift, not style."""
    from inqtrix.agents.prompts import (
        NO_MODEL_SOURCE_SECTIONS,
        build_agent_kernel_system_prompt,
        build_deep_revision_prompt,
    )

    assert "NO source, reference or link section of your own" in NO_MODEL_SOURCE_SECTIONS
    assert "no raw URLs" in NO_MODEL_SOURCE_SECTIONS
    surfaces = {
        "answer_system": agent_answer_system_prompt(),
        "kernel_system": build_agent_kernel_system_prompt(),
        "deep_revision": build_deep_revision_prompt("Auftrag", "Bundle", []),
    }
    for name, text in surfaces.items():
        assert NO_MODEL_SOURCE_SECTIONS in text, name


def test_kernel_citation_block_defines_the_label_style():
    from inqtrix.agents.prompts import build_agent_kernel_system_prompt

    prompt = build_agent_kernel_system_prompt()
    assert "Citation style:" in prompt
    from inqtrix.citation_labels import CITATION_FORMAT

    assert prompt.count(CITATION_FORMAT) == 1


def test_deep_review_rubric_does_not_reward_model_source_lists():
    """The 'or sources' escape hatch rewarded model-authored source
    sections in the Deep rubric — labels are the only accepted form."""
    assert "evidence labels" in DEEP_REVIEW_RUBRIC
    assert "or sources" not in DEEP_REVIEW_RUBRIC


def test_registry_line_leads_with_the_file_name():
    """Named documents render `- name.md (...)`; kinds without a name
    render `- (...)`. Titles are stored text and follow in a fence keyed
    by artifact_id; the header carries the address-by-name instruction."""
    from inqtrix.agents.prompts import build_agent_session_context_sections

    section = build_agent_session_context_sections(
        artifact_registry=(
            {
                "artifact_id": "art_1",
                "kind": "deliverable",
                "title": "Marktbericht",
                "revision": 3,
                "updated_by": "agent",
                "name": "marktbericht.md",
            },
            {
                "artifact_id": "art_2",
                "kind": "evidence_bundle",
                "title": "Belege",
                "revision": 1,
                "updated_by": "agent",
            },
        ),
    )
    assert "- marktbericht.md (artifact_id art_1, revision 3" in section
    assert "- (artifact_id art_2, revision 1" in section
    assert (
        '<untrusted_content source="artifact-titles">\n'
        "art_1: Marktbericht\nart_2: Belege\n</untrusted_content>"
    ) in section
    assert "towards the user" in section
    assert "by their file name" in section


def test_kernel_prompt_carries_the_file_name_discipline():
    """The model must address documents by file name, never by
    artifact_id — pinned on the composed kernel system prompt."""
    from inqtrix.agents.prompts import build_agent_kernel_system_prompt

    prompt = build_agent_kernel_system_prompt()
    assert "by their file name" in prompt
    assert "never by the artifact_id" in prompt


def test_conversational_surfaces_carry_the_writing_style_after_the_format_block():
    """Chat-style answers (kernel, chat answer) carry the house writing
    style right after the renderer block."""
    from inqtrix.prompt_language import OUTPUT_FORMAT_BLOCK, WRITING_STYLE_BLOCK

    kernel = build_agent_kernel_system_prompt()
    assert WRITING_STYLE_BLOCK in kernel
    assert kernel.index(OUTPUT_FORMAT_BLOCK) < kernel.index(WRITING_STYLE_BLOCK)
    assert WRITING_STYLE_BLOCK in agent_answer_system_prompt()
