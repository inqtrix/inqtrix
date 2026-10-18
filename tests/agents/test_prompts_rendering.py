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
    agent_synthesis_system_prompt,
    build_agent_answer_prompt,
    build_agent_outline_prompt,
    build_agent_section_prompt,
    DEEP_REVIEW_RUBRIC,
    build_agent_kernel_system_prompt,
    build_deep_revision_prompt,
    build_review_call_system_prompt,
    build_work_profile_system_prompt,
    rendering_capabilities_block,
)

RENDERER_FEATURES = ("GitHub-flavoured Markdown", "Tables", "Math", "Code", "mermaid")


@pytest.mark.parametrize("prompt", [
    agent_answer_system_prompt(),
    agent_synthesis_system_prompt(),
    build_agent_kernel_system_prompt(),
    build_deep_revision_prompt("Compare evidence", "Output", []),
    build_review_call_system_prompt(),
    build_work_profile_system_prompt(DEEP_REVIEW_RUBRIC),
], ids=["answer", "synthesis", "kernel", "revision", "review_call", "review_role"])
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


def test_mission_writers_keep_explicit_structure_ahead_of_default_sections():
    system = agent_synthesis_system_prompt()
    assert "Name what is unsupported within the given structure." in system
    assert "belongs in the section 'Open points'" not in system
    outline = build_agent_outline_prompt("Auftrag", [], "Beleg")
    assert "Explicit requirements on the number, order and titles of the sections take precedence." in outline
    assert "add no mandatory section" in outline
    assert "always 'Open points'" not in outline
    prompts = (
        build_agent_section_prompt("Auftrag", "Teil", "Fokus", "Beleg", "", evidence_notes="Grenze"),
        build_agent_answer_prompt("Auftrag", "Beleg", "", evidence_notes="Grenze"),
    )
    assert all("within the given structure" in prompt for prompt in prompts)
    assert all("under 'Open points'" not in prompt for prompt in prompts)


def test_rendering_block_names_every_renderer_feature():
    block = rendering_capabilities_block()
    for feature in RENDERER_FEATURES:
        assert feature in block, feature
    # Explicit non-capabilities stay named (skipHtml is on, no emojis).
    assert "no HTML" in block
    assert "no emojis" in block


def test_rendering_block_states_usage_rules():
    """The block carries RULES, not just a feature list — the table
    trigger, the no-decoration constraint, the plot bounds and the
    precedence of explicit format instructions."""
    block = rendering_capabilities_block()
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
    assert 'write "USD 5", never "$5"' in block
    assert "override these defaults" in block


def test_answer_and_synthesis_prompts_carry_the_block():
    block = rendering_capabilities_block()
    assert block in agent_answer_system_prompt()
    assert block in agent_synthesis_system_prompt()


def test_synthesis_prompts_require_a_small_nonredundant_citation_set():
    prompts = (
        agent_answer_system_prompt(),
        agent_synthesis_system_prompt(),
        build_agent_outline_prompt("Auftrag", ["Kriterium"], "[W1] Fakt"),
        build_agent_section_prompt(
            "Auftrag", "Kapitel", "Fokus", "[W1] Fakt", ""
        ),
        build_agent_answer_prompt("Auftrag", "[W1] Fakt", ""),
    )

    for prompt in prompts:
        assert "smallest sufficient" in prompt
        assert "non-redundant" in prompt
    assert "1-3 labels" in prompts[1]
    assert "never all available labels" in prompts[3]


# --- sources appear exactly once (the curated list) --------------------------


def test_no_model_source_sections_is_word_identical_on_every_surface():
    """THE prohibition sentence rides every answer surface verbatim —
    chat answer (system + closing), memo section, kernel system prompt
    and deep revision. A reworded copy on one surface is drift, not
    style."""
    from inqtrix.agents.prompts import (
        NO_MODEL_SOURCE_SECTIONS,
        build_agent_kernel_system_prompt,
        build_deep_revision_prompt,
    )

    assert "NO source, reference or link section of your own" in NO_MODEL_SOURCE_SECTIONS
    assert "no raw URLs" in NO_MODEL_SOURCE_SECTIONS
    surfaces = {
        "answer_system": agent_answer_system_prompt(),
        "synthesis_system": agent_synthesis_system_prompt(),
        "answer_prompt": build_agent_answer_prompt("Frage?", "(keine)", ""),
        "section_prompt": build_agent_section_prompt(
            "Frage?", "Abschnitt", "Fokus", "(keine)", ""
        ),
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
    """Named documents render `- name.md — Titel (...)`; kinds
    without a name keep the plain line, and the header carries the
    address-by-name instruction."""
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
    assert (
        "- marktbericht.md — Marktbericht (artifact_id art_1, revision 3"
        in section
    )
    assert "- Belege (artifact_id art_2" in section
    assert "towards the user" in section
    assert "by their file name" in section


def test_kernel_prompt_carries_the_file_name_discipline():
    """The model must address documents by file name, never by
    artifact_id — pinned on the composed kernel system prompt."""
    from inqtrix.agents.prompts import build_agent_kernel_system_prompt

    prompt = build_agent_kernel_system_prompt()
    assert "by their file name" in prompt
    assert "never by the artifact_id" in prompt


def test_writing_prompts_carry_the_child_evidence_notes():
    """A child's capped confidence reaches every writing surface as a
    note to name within the given structure — never as a citable source."""
    note = "Task t1: Evidenzlage needs_review (93 % Einzelquellen), Konfidenz 6/10"
    heading = "Reading and evidence notes from subtasks"
    section = build_agent_section_prompt(
        "Frage?", "Abschnitt", "Fokus", "(keine)", "", evidence_notes=note
    )
    answer = build_agent_answer_prompt("Frage?", "(keine)", "", evidence_notes=note)
    for prompt in (section, answer):
        assert heading in prompt and note in prompt
        assert "never cite them as evidence" in prompt
        # The writer is told to render the note for readers, not as a
        # check protocol (two blind reviews named the raw line).
        assert "in reader language — without task ids or status codes" in prompt
    assert heading not in build_agent_section_prompt(
        "Frage?", "Abschnitt", "Fokus", "(keine)", ""
    )
    assert heading not in build_agent_answer_prompt("Frage?", "(keine)", "")


def test_conversational_surfaces_carry_the_writing_style_after_the_format_block():
    """Chat-style answers (kernel, mission chat answer) carry the house
    writing style right after the renderer block; memo sections follow their
    outline and template instead."""
    from inqtrix.prompt_language import OUTPUT_FORMAT_BLOCK, WRITING_STYLE_BLOCK

    kernel = build_agent_kernel_system_prompt()
    assert WRITING_STYLE_BLOCK in kernel
    assert kernel.index(OUTPUT_FORMAT_BLOCK) < kernel.index(WRITING_STYLE_BLOCK)
    assert WRITING_STYLE_BLOCK in agent_answer_system_prompt()
    assert WRITING_STYLE_BLOCK not in agent_synthesis_system_prompt()
