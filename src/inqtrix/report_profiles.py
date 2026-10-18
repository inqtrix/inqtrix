"""Report profile types and runtime tuning presets.

Use :class:`ReportProfile` as the public switch for answer style and research
depth. The associated tuning bundle keeps profile-specific defaults in one
place so runtime code can consume them without duplicating literals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from inqtrix.prompt_language import heading_for

class ReportProfile(StrEnum):
    """Supported report styles for the research agent.

    ``schnell`` runs exactly ONE research round (the broad first-round
    STORM queries) and composes a short two-section answer — the
    latency-first profile for quick lookups and for agent child runs
    that need a fast external sweep rather than a full report.

    ``compact`` preserves the current concise answer style with lower latency.

    ``deep`` keeps more evidence in the pipeline and targets a denser,
    review-style report with broader citation coverage and explicit
    uncertainty handling.
    """

    SCHNELL = "schnell"
    COMPACT = "compact"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class AnswerSectionSpec:
    """One named section of the final synthesised answer.

    Each entry drives a single LLM call inside the answer composer:
    ``heading(language)`` becomes the markdown ``##``-header in the
    answer's language, ``prompt_instruction`` + ``length_guidance`` are
    merged into the per-section prompt. Provider output budgets stay in
    provider constructors, not in report profiles.

    Frozen + slotted so instances are safe to share across runs and
    cheap to compare in tests.

    Attributes:
        key: The section's identity, resolved to the heading text of the
            answer language through ``inqtrix.prompt_language.HEADINGS``
            (e.g. ``"summary"``); an unknown key is used verbatim, so an
            extension hook may name a custom section literally.
        prompt_instruction: Section-specific instruction appended to the
            shared answer-prompt scaffold. Should describe what the
            section must contain in business terms.
        length_guidance: Soft qualitative length hint phrased for the
            model. It should steer density and completeness without
            enforcing fixed token or word counts.
        required: When ``False``, the answer composer may skip this
            section if upstream evidence is insufficient. Default
            ``True`` — used today only by extension hooks; stock
            sections are all required.
        write_last: When ``True``, the composer renders this section
            AFTER all other sections in the same plan, even though it
            still appears at its declared position in the final
            answer. Used for summary-style sections (executive summary,
            summary) that should synthesize the rest of the body and
            therefore benefit from seeing the completed body in their
            ``report_so_far_summary`` and ``used_evidence_labels``.
        owns_evidence_limits: Whether this section owns the report's
            consolidated disclosure of evidence limits and open questions.
            At most one section may own it. Without an explicit owner, the
            last non-summary section takes responsibility.
    """

    key: str
    prompt_instruction: str
    length_guidance: str
    required: bool = True
    write_last: bool = False
    owns_evidence_limits: bool = False

    def heading(self, language: str | None) -> str:
        """The ``##`` heading text of this section in ``language``."""
        return heading_for(self.key, language)


def evidence_limits_section_index(sections: tuple[AnswerSectionSpec, ...]) -> int | None:
    """Return the one-based display index that owns evidence-limit disclosure.

    Ownership follows the section plan, independently of heading text or
    language. Empty plans have no owner; ambiguous explicit ownership is a
    configuration error.
    """
    owners = [index for index, section in enumerate(sections, 1) if section.owns_evidence_limits]
    if len(owners) > 1:
        raise ValueError("Only one answer section may own evidence limits")
    if owners:
        return owners[0]
    body = [index for index, section in enumerate(sections, 1) if not section.write_last]
    return body[-1] if body else len(sections) or None


@dataclass(frozen=True, slots=True)
class ReportProfileTuning:
    """Bundled runtime tuning values associated with one report profile.

    A profile (``compact`` / ``deep``) selects an instance of this
    dataclass via :func:`tuning_for_report_profile`. Every numeric
    field is consumed by exactly one component (claim extraction,
    consolidation, answer composer, or evidence renderer), so adjusting
    one value affects only that component.

    Attributes:
        settings_overrides: ``AgentSettings`` fields auto-overridden
            when the profile is selected (see :class:`AgentSettings`
            ``with_report_profile_defaults``). Include profile-owned
            baseline values for every field that another profile may
            raise, so request-time profile switches can move in both
            directions while preserving explicit user settings.
        answer_sections: Ordered tuple of :class:`AnswerSectionSpec`
            describing the per-profile section layout.
        answer_claim_prompt_items: Maximum number of consolidated
            claims passed into the answer prompt's ``Claims`` block.
        claim_input_char_limit: Maximum text length (chars) per source
            forwarded to the claim-extraction call.
        claim_citation_cap: Maximum number of citations included with
            each extracted claim batch.
        claim_max_items: Maximum number of claims extracted per source
            in a single call.
        claim_source_url_cap: Maximum number of source URLs attached
            to a single consolidated claim.
        claim_ledger_cap: Maximum number of entries kept in the
            consolidated claim ledger across the whole run.
        materialize_max_total: Maximum total number of materialised
            (verified + contested + unverified) claims in the result
            view.
        materialize_max_unverified: Sub-cap for ``unverified`` claims
            within ``materialize_max_total``. Profiles may set this equal
            to ``materialize_max_total`` when hidden unverified-evidence
            truncation would be more harmful than a broader uncertainty view.
        prompt_evidence_record_char_limit: Maximum characters rendered
            per source block in the EvidenceLedger overview before its
            evidence lines are compacted (label, URL, and metadata are
            kept).
        prompt_evidence_total_char_budget: Maximum total characters for
            the rendered evidence overview handed to the answer
            composer (the writer's full record view).
        evaluate_evidence_record_char_limit: Per-source cap of the
            evaluator's compact record view (header plus a few bounded
            claim lines).
        evaluate_evidence_total_char_budget: Total budget of the
            evaluator's compact view, sized to hold every source the
            profile can collect so coverage is judged over the whole
            ledger.
            facing evidence view; records that do not fit are counted as
            omitted rather than dropped silently.
        min_report_eligible_evidence: Minimum report-eligible
            EvidenceRecords expected before early stopping is allowed,
            unless the run already hit ``max_rounds``.
    """

    settings_overrides: dict[str, int]
    answer_sections: tuple[AnswerSectionSpec, ...]
    answer_claim_prompt_items: int
    claim_input_char_limit: int
    claim_citation_cap: int
    claim_max_items: int
    claim_source_url_cap: int
    claim_ledger_cap: int
    materialize_max_total: int
    materialize_max_unverified: int
    prompt_evidence_record_char_limit: int
    prompt_evidence_total_char_budget: int
    evaluate_evidence_record_char_limit: int
    evaluate_evidence_total_char_budget: int
    min_report_eligible_evidence: int


_COMPACT_ANSWER_SECTIONS = (
    AnswerSectionSpec(
        key="summary",
        prompt_instruction=(
            "Answer the question directly and name the most important findings "
            "with a clear assessment. Build on the sections already written and "
            "reuse their evidence labels when you condense their statements. "
            "Write about the subject, not about the report: never describe what "
            "its sections, tables or diagrams contain."
        ),
        length_guidance="brief but complete; no bullet points",
        write_last=True,
    ),
    AnswerSectionSpec(
        key="key_points",
        prompt_instruction=(
            "Write substantial bullet points with the most important facts, "
            "figures and implications. Each point must stand on its own."
        ),
        length_guidance="as many bullet points as the evidence carries; no filler",
    ),
    AnswerSectionSpec(
        key="detail_analysis",
        prompt_instruction=(
            "Deepen the core aspects in sensible `###` subsections. Explain "
            "relationships, causes and effects reliably and without repetition."
        ),
        length_guidance="as long as the question needs; no artificial shortening",
    ),
    AnswerSectionSpec(
        key="context_outlook",
        prompt_instruction=(
            "Put the findings into context briefly and name the most important "
            "next developments or open questions."
        ),
        length_guidance="short and complete",
        owns_evidence_limits=True,
    ),
)

_DEEP_ANSWER_SECTIONS = (
    AnswerSectionSpec(
        key="executive_summary",
        prompt_instruction=(
            "Answer the question directly and condense the most important findings "
            "with their current status. Build on the sections already written and "
            "reuse their evidence labels when you condense their statements. Adding "
            "few or no citations of your own is legitimate when the detailed "
            "sections already carry the evidence. Write about the subject, not "
            "about the report: never describe what its sections, tables or "
            "diagrams contain."
        ),
        length_guidance="brief, dense and complete; no bullet points",
        write_last=True,
    ),
    AnswerSectionSpec(
        key="background",
        prompt_instruction=(
            "Explain the starting point, the time frame and the relevant context."
        ),
        length_guidance="compact, with every necessary context anchor",
    ),
    AnswerSectionSpec(
        key="analysis",
        prompt_instruction=(
            "Write several sensible `###` subsections. Integrate relevant figures, "
            "comparisons, mechanisms, implementation questions and factual causal links."
        ),
        length_guidance="the most extensive section; depth follows the evidence, not a word limit",
    ),
    AnswerSectionSpec(
        key="perspectives",
        prompt_instruction=(
            "Present the essential perspectives separately and neutrally, including "
            "the driving actors, opposing positions and the view of those affected "
            "or implementing."
        ),
        length_guidance="clearly separated paragraphs; extent follows the number of well-supported perspectives",
    ),
    AnswerSectionSpec(
        key="risks",
        prompt_instruction=(
            "List precise bullet points on evidence limits, open points, "
            "methodological constraints or counter-arguments."
        ),
        length_guidance="only real risks and uncertainties; no obligatory filler",
        owns_evidence_limits=True,
    ),
    AnswerSectionSpec(
        key="conclusion",
        prompt_instruction=(
            "Draw a reliable overall assessment and name the most plausible next "
            "development path."
        ),
        length_guidance="short, clear and complete",
    ),
)


_SCHNELL_ANSWER_SECTIONS = (
    AnswerSectionSpec(
        key="summary",
        prompt_instruction=(
            "Answer the question directly in a few sentences and name the "
            "supporting findings with evidence labels. Build on the key points "
            "already written. Write about the subject, not about the report: "
            "never describe what its sections, tables or diagrams contain."
        ),
        length_guidance="very brief; no bullet points",
        write_last=True,
    ),
    AnswerSectionSpec(
        key="key_points",
        prompt_instruction=(
            "State the most important facts and figures as self-contained bullet "
            "points. No deepening, no repetition."
        ),
        length_guidance="only the best-supported points; no filler",
        owns_evidence_limits=True,
    ),
)


_SCHNELL_TUNING = ReportProfileTuning(
    # One round, first-iteration STORM breadth only: the profile exists
    # for latency (quick lookups, agent child runs), so every budget is
    # the smallest value that still yields a citable two-section answer.
    # Same field set as the other profiles (bidirectional request-time
    # profile switches, see ``settings_overrides`` docstring).
    settings_overrides={
        "max_rounds": 1,
        "min_rounds": 1,
        "confidence_stop": 6,
        "first_round_queries": 6,
        "answer_prompt_citations_max": 40,
        "reasoning_timeout": 600,
        "editor_assistant_timeout": 600,
        "claim_extract_timeout": 600,
        "search_timeout": 600,
        "max_total_seconds": 3600,
    },
    answer_sections=_SCHNELL_ANSWER_SECTIONS,
    answer_claim_prompt_items=12,
    claim_input_char_limit=16000,
    claim_citation_cap=6,
    claim_max_items=6,
    claim_source_url_cap=3,
    claim_ledger_cap=200,
    materialize_max_total=16,
    materialize_max_unverified=6,
    prompt_evidence_record_char_limit=1800,
    # One round of six searches with ~5 usable sources each needs ~60k
    # chars of full source blocks; a smaller budget drops whole searches
    # from the writer's view and a comparison question loses one of its
    # halves. ~16k tokens of evidence per section call stays far below the
    # 128k-token provider floor.
    prompt_evidence_total_char_budget=64000,
    evaluate_evidence_record_char_limit=900,
    evaluate_evidence_total_char_budget=40000,
    min_report_eligible_evidence=2,
)


_COMPACT_TUNING = ReportProfileTuning(
    settings_overrides={
        "max_rounds": 2,
        "min_rounds": 1,
        "confidence_stop": 7,
        "first_round_queries": 6,
        "answer_prompt_citations_max": 60,
        "reasoning_timeout": 600,
        "editor_assistant_timeout": 600,
        "claim_extract_timeout": 600,
        "search_timeout": 600,
        "max_total_seconds": 3600,
    },
    answer_sections=_COMPACT_ANSWER_SECTIONS,
    answer_claim_prompt_items=20,
    claim_input_char_limit=24000,
    claim_citation_cap=8,
    claim_max_items=8,
    claim_source_url_cap=4,
    claim_ledger_cap=400,
    materialize_max_total=24,
    materialize_max_unverified=8,
    prompt_evidence_record_char_limit=2200,
    # Two rounds x six searches x ~5 sources x ~2.2k chars ~ 130k chars
    # of candidates; 100k keeps every search visible in the common case
    # (~25k tokens per section call under the 128k-token floor) while the
    # round-robin cut thins evenly above it.
    prompt_evidence_total_char_budget=100000,
    evaluate_evidence_record_char_limit=900,
    evaluate_evidence_total_char_budget=60000,
    min_report_eligible_evidence=3,
)

_DEEP_TUNING = ReportProfileTuning(
    # Same first-round breadth and stop threshold as COMPACT: DEEP's depth
    # comes from the mandatory second round, the third-round headroom and
    # the larger evidence and output budgets, not from a wider first wave.
    settings_overrides={
        "max_rounds": 3,
        "min_rounds": 2,
        "confidence_stop": 7,
        "first_round_queries": 6,
        "answer_prompt_citations_max": 500,
        "reasoning_timeout": 600,
        "editor_assistant_timeout": 600,
        "claim_extract_timeout": 600,
        "search_timeout": 600,
        "max_total_seconds": 3600,
    },
    answer_sections=_DEEP_ANSWER_SECTIONS,
    answer_claim_prompt_items=40,
    claim_input_char_limit=48000,
    claim_citation_cap=20,
    claim_max_items=24,
    claim_source_url_cap=6,
    claim_ledger_cap=800,
    materialize_max_total=48,
    materialize_max_unverified=48,
    prompt_evidence_record_char_limit=2600,
    # Raised so the record-driven evidence overview holds substantially more
    # than the 60-80 records a 110k-char budget rendered; the answer prompt
    # consumes this single view, and ~180k chars (~45k tokens) stay inside
    # the REQUIRED_CONTEXT_WINDOW_TOKENS floor (128k) every provider must
    # meet, with room for the running report and the section output.
    prompt_evidence_total_char_budget=180000,
    evaluate_evidence_record_char_limit=900,
    evaluate_evidence_total_char_budget=100000,
    min_report_eligible_evidence=8,
)


def tuning_for_report_profile(profile: ReportProfile | str) -> ReportProfileTuning:
    """Return the runtime tuning bundle for ``profile``.

    Args:
        profile: Either a :class:`ReportProfile` enum value or its
            string representation (``"compact"`` / ``"deep"``). Any
            value that does not parse to a known profile silently
            falls back to ``ReportProfile.COMPACT`` to keep callers
            robust against malformed config.

    Returns:
        The frozen :class:`ReportProfileTuning` instance for the
        profile. The same instance is returned for repeat calls (the
        underlying tuning objects are module-level constants), so
        identity-based caching by callers is safe.
    """
    try:
        normalized = ReportProfile(profile)
    except ValueError:
        normalized = ReportProfile.COMPACT
    if normalized is ReportProfile.DEEP:
        return _DEEP_TUNING
    if normalized is ReportProfile.SCHNELL:
        return _SCHNELL_TUNING
    return _COMPACT_TUNING


def settings_overrides_for_report_profile(profile: ReportProfile | str) -> dict[str, int]:
    """Return the ``AgentSettings`` overrides implied by ``profile``.

    Args:
        profile: Either a :class:`ReportProfile` enum value or its
            string representation. Unknown values fall back to
            ``ReportProfile.COMPACT``.

    Returns:
        A new ``dict`` mapping ``AgentSettings`` field names to the
        profile-specific override values. Caller may mutate the returned
        dict freely — it is a fresh copy of the bundle's
        ``settings_overrides``.
    """
    return dict(tuning_for_report_profile(profile).settings_overrides)
