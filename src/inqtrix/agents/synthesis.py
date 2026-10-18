"""Synthesize the memo into the canvas artifact.

Outline first, then ONE call per section; every finished section flushes
into the memo artifact (revision++) so the canvas streams section-wise
without a delta protocol; the artifact row is the source of truth.
Citation validation, quote grounding, and coverage stats live in
``report_quality`` (one definition shared with the kernel); this module
re-exports the names its callers and tests already import.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import Field, create_model

from inqtrix.agents.markdown import normalize_writer_markdown
from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.phase_models import (
    ReportOutline,
    ReportSection,
    SectionText,
)
from inqtrix.agents.prompts import (
    agent_answer_system_prompt,
    agent_synthesis_system_prompt,
    build_agent_answer_prompt,
    build_agent_outline_prompt,
    build_agent_section_prompt,
)
from inqtrix.agents.report_quality import (
    CitationValidationFailed,
    citation_coverage,
    cited_references,
    introduced_identifiers,
    unknown_citation_labels,
    validate_and_repair_citations,
    verify_quotes,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inqtrix.core.results import HandoverClarification
    from inqtrix.providers.base import LLMProvider

log = logging.getLogger("inqtrix")

__all__ = [
    "CitationValidationFailed",
    "assemble_memo",
    "citation_coverage",
    "cited_references",
    "SECTION_IN_PROMPT_BUDGET_TOKENS",
    "choose_section_transport",
    "finish_section",
    "repair_heading",
    "run_outline",
    "section_prompt",
    "unknown_citation_labels",
    "verify_quotes",
    "write_chat_answer",
    "write_section",
]


def run_outline(
    llm: "LLMProvider",
    *,
    question: str,
    success_criteria: list[str],
    evidence_digest: str,
    known_labels: list[str],
    prior_memo: str = "",
    skills_block: str = "",
    user_guidance: str = "",
    evidence_notes: str = "",
    clarifications: "Sequence[HandoverClarification]" = (),
    language: str | None = None,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
) -> StructuredOutcome:
    """The outline call, constrained to the supplied evidence labels.

    ``prior_memo`` carries a previous memo to continue. Reference choices
    are bounded in both the provider schema and local response validation.
    ``clarifications`` are the run's, as the store holds them.
    """
    labels = tuple(dict.fromkeys(known_labels))
    section_model = create_model(
        "ReportSection",
        __base__=ReportSection,
        evidence_labels=(
            list[Literal[labels]] if labels else list[str],
            Field(description="Only supplied evidence labels; empty when none apply.",
                  **({} if labels else {"max_length": 0})),
        ),
    )
    outline_model = create_model(
        "ReportOutline",
        __base__=ReportOutline,
        sections=(list[section_model], Field(min_length=1)),
    )
    return structured_call(
        llm,
        prompt=build_agent_outline_prompt(
            question,
            success_criteria,
            evidence_digest,
            prior_memo=prior_memo,
            user_guidance=user_guidance,
            skills_block=skills_block,
            evidence_notes=evidence_notes,
            clarifications=clarifications,
            language=language,
        ),
        model_cls=outline_model,
        node="agent_synthesis",
        system=agent_synthesis_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


SECTION_IN_PROMPT_BUDGET_TOKENS = 16_000
"""Estimated section prompt size above which a section role beats one call.

Derived from measured fixed costs, not guessed: one structured call pays
about 2,133 tokens once, a role about 5,309 tokens per model turn, and a
role that reads before it writes needs at least two turns. With ``M`` the
section's material and ``f`` the share of it the role actually loads, the
role wins once ``8,485 < (1 - f) * M``, so the crossing lies at
``8,485 / (1 - f)``. ``f`` cannot be known before a role has run: at
``f = 0.5`` the crossing lies near 17,000, and this bound sits just below
it - no section under it is cheaper as a role as long as the role loads at
least about half of its material. A role that loads less would win
earlier (near 11,300 at ``f = 0.25``); a role that loads everything
(``f = 1``) never wins on cost at all, only on fitting material that one
call cannot hold. Measured memo sections have a median of about 1,300 and a
90th percentile of about 6,500 tokens: the role is for the tail, and the
bound is to be revisited once ``f`` has been measured there.
"""


def choose_section_transport(prompt: str, *, role_available: bool) -> str:
    """How ONE section is written - the only place that decides it.

    From the size of the work order the single call would carry. A section
    that fits is written in one call; one that does not goes to the section
    role, which reads its evidence instead of receiving all of it. A server
    that runs no role writes every section in one call, and says so for a
    section that should have gone to one.
    """
    from inqtrix.agents.kernel.work_profiles import TRANSPORT_PROMPT, TRANSPORT_ROLE
    from inqtrix.nodes import _estimate_required_context_tokens

    estimated, _ = _estimate_required_context_tokens(
        system=agent_synthesis_system_prompt(),
        prompt=prompt,
        requested_output_tokens=0,
    )
    if estimated <= SECTION_IN_PROMPT_BUDGET_TOKENS:
        return TRANSPORT_PROMPT
    if role_available:
        return TRANSPORT_ROLE
    log.warning(
        "Section material exceeds the single-call budget (estimated_tokens=%d) "
        "and this server runs no kernel role; writing it in one call.",
        estimated,
    )
    return TRANSPORT_PROMPT


def section_prompt(
    *,
    question: str,
    section_title: str,
    section_focus: str,
    evidence_digest: str,
    evidence_register: str,
    contradictions_digest: str,
    skills_block: str = "",
    user_guidance: str = "",
    evidence_notes: str = "",
    outline_overview: str = "",
    clarifications: "Sequence[HandoverClarification]" = (),
    language: str | None = None,
) -> str:
    """The prompt one section is written from in one call.

    One constructor for the call itself and for the size estimate that
    decides whether the section goes to one call at all, so the estimate
    measures exactly what would be sent - the run's clarifications
    included.
    """
    return build_agent_section_prompt(
        question,
        section_title,
        section_focus,
        evidence_digest,
        contradictions_digest,
        evidence_register=evidence_register,
        outline_overview=outline_overview,
        skills_block=skills_block,
        user_guidance=user_guidance,
        evidence_notes=evidence_notes,
        clarifications=clarifications,
        language=language,
    )


def write_section(
    llm: "LLMProvider",
    *,
    question: str,
    section_title: str,
    section_focus: str,
    evidence_digest: str,
    evidence_register: str,
    contradictions_digest: str,
    skills_block: str = "",
    user_guidance: str = "",
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    known_labels: list[str] | None = None,
    evidence_notes: str = "",
    internal_reference_ids: list[str] | None = None,
    authored_by: str = "",
    outline_overview: str = "",
    clarifications: "Sequence[HandoverClarification]" = (),
    language: str | None = None,
) -> tuple[str, dict[str, int]]:
    """One section's prose; returns ``(markdown, usage)``.

    Structured output carries Markdown and per-call usage together so the
    phase machine can book each completed section exactly once.

    ``evidence_digest`` is what the outline preselected for THIS section,
    rendered in full. ``evidence_register`` is every other reference of the
    run as one line each. The parameter carries no default on purpose: a
    default would silently restore the state in which an outline that
    preselected nothing left the section believing the run had no evidence.
    """
    prompt = section_prompt(
        question=question,
        section_title=section_title,
        section_focus=section_focus,
        evidence_digest=evidence_digest,
        evidence_register=evidence_register,
        contradictions_digest=contradictions_digest,
        skills_block=skills_block,
        user_guidance=user_guidance,
        evidence_notes=evidence_notes,
        outline_overview=outline_overview,
        clarifications=clarifications,
        language=language,
    )
    outcome = structured_call(
        llm,
        prompt=prompt,
        model_cls=SectionText,
        node="agent_synthesis",
        system=agent_synthesis_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    section = outcome.value
    return finish_section(
        llm,
        section.markdown if isinstance(section, SectionText) else "",
        usage=outcome.usage,
        known_labels=known_labels,
        internal_reference_ids=internal_reference_ids,
        authored_by=authored_by,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def finish_section(
    llm: "LLMProvider",
    markdown: str,
    *,
    usage: dict[str, int],
    known_labels: list[str] | None,
    internal_reference_ids: list[str] | None,
    authored_by: str = "",
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
) -> tuple[str, dict[str, int]]:
    """The one closing stage of a section, whichever transport wrote it.

    Normalization and the citation check with its single repair. A section
    role hands its prose to this same stage with the same citable labels,
    so what a section may cite never depends on how it was written.
    """
    normalized = normalize_writer_markdown(markdown.strip())
    if known_labels is None:
        return normalized, usage
    return validate_and_repair_citations(
        llm,
        markdown=normalized,
        known_labels=known_labels,
        internal_reference_ids=internal_reference_ids,
        authored_by=authored_by,
        usage=usage,
        system=agent_synthesis_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def write_chat_answer(
    llm: "LLMProvider",
    *,
    question: str,
    evidence_digest: str,
    contradictions_digest: str,
    history: str,
    prior_memo: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    skills_block: str = "",
    user_guidance: str = "",
    known_labels: list[str] | None = None,
    evidence_notes: str = "",
    internal_reference_ids: list[str] | None = None,
    authored_by: str = "",
    language: str | None = None,
) -> tuple[str, dict[str, int]]:
    """The single chat-form answer call; ``(markdown, usage)``.

    The chat deliverable skips the outline/section loop: one direct
    conversational answer over the same evidence digest, citation labels
    still mandatory. Structured output carries per-call usage just as it
    does for :func:`write_section`.
    """
    outcome = structured_call(
        llm,
        prompt=build_agent_answer_prompt(
            question,
            evidence_digest,
            contradictions_digest,
            history=history,
            prior_memo=prior_memo,
            skills_block=skills_block,
            user_guidance=user_guidance,
            evidence_notes=evidence_notes,
            language=language,
        ),
        model_cls=SectionText,
        node="agent_answer",
        system=agent_answer_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    section = outcome.value
    text = section.markdown if isinstance(section, SectionText) else ""
    normalized = normalize_writer_markdown(text.strip())
    if known_labels is None:
        return normalized, outcome.usage
    return validate_and_repair_citations(
        llm,
        markdown=normalized,
        known_labels=known_labels,
        internal_reference_ids=internal_reference_ids,
        authored_by=authored_by,
        usage=outcome.usage,
        system=agent_answer_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def assemble_memo(
    title: str, sections: list[tuple[str, str]]
) -> str:
    """Join finished sections into the memo body.

    Both heading levels are OUTLINE text, not section text: the bodies
    arrive already checked, the headings around them were written by the
    outline call and are checked where that call is adopted
    (:func:`repair_heading`).
    """
    parts = [f"# {title}"]
    for section_title, body in sections:
        parts.append(f"## {section_title}\n\n{body}")
    return normalize_writer_markdown("\n\n".join(parts))


def repair_heading(
    llm: "LLMProvider",
    heading: str,
    *,
    known_labels: list[str],
    internal_reference_ids: list[str],
    authored_by: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
) -> tuple[str, dict[str, int]]:
    """One memo heading, with any identifier the model introduced taken out.

    The outline call sees the same evidence cards the section writer
    sees, so it can copy the same identifiers — but its output reaches
    the reader as a heading, and the guard that checks a section checks
    only the section's body. This is the heading's own round, and it is
    the SAME round: :func:`validate_and_repair_citations`, bounded to one
    repair, raising rather than publishing what it could not clean.

    A clean heading costs nothing — no call is made. ``known_labels``
    travels because the repair judges citation labels too, and a heading
    that legitimately cites one must keep it.

    Returns the heading and what the repair spent; ``{}`` when no repair
    was needed.
    """
    leaked = introduced_identifiers(
        heading, internal_reference_ids, authored_by=authored_by
    )
    if not leaked:
        return heading, {}
    log.warning(
        "Memo heading needs bounded repair (internal_ids=%d).", len(leaked)
    )
    repaired, usage = validate_and_repair_citations(
        llm,
        markdown=f"# {heading}",
        known_labels=known_labels,
        internal_reference_ids=internal_reference_ids,
        authored_by=authored_by,
        usage={},
        system=agent_synthesis_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    cleaned = _heading_text(repaired)
    if not cleaned:
        raise CitationValidationFailed(
            "The repair of a memo heading returned no usable heading.",
            usage=usage,
        )
    return cleaned, usage


def _heading_text(markdown: str) -> str:
    """The heading line of a repaired one-line document, without its marks.

    The repair returns Markdown because that is what it was given. Only
    the first non-empty line can be the heading; anything the model added
    below it is prose this caller never asked for.
    """
    for line in markdown.splitlines():
        if stripped := line.strip().lstrip("#").strip():
            return stripped
    return ""
