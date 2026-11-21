"""Structured-output models of the agent's model steps.

Clarification, the sufficiency judge, memory reflection, document-analysis
windows, citation repair, the review and its re-check each parse into one of
these via
:func:`inqtrix.agents.patterns._structured.structured_call`. The
``complete_structured`` gotcha applies: EVERY field must be required for
the provider's native structured mode, so all fields carry explicit
values in the prompt contract and "nothing" is the empty string/list —
never an omitted key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inqtrix.constants import (
    CLARIFICATION_OPTION_LABEL_CHARS,
)
from inqtrix.quote_limits import CITE_QUOTE_MIN_WORDS


class ClarificationOptionModel(BaseModel):
    """One selectable answer option of a clarification question."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        description=(
            "Short answer option the user can pick "
            f"(<= {CLARIFICATION_OPTION_LABEL_CHARS} chars)."
        )
    )
    description: str = Field(
        description=(
            "Optional one-line explanation of the option ('' when the "
            "label is self-explanatory)."
        )
    )


class ClarificationQuestionModel(BaseModel):
    """One structured clarification question with pickable options.

    Free text is ALWAYS allowed besides the options, so the options only
    cover the LIKELY answers — the model never has to force an exhaustive
    enumeration. Stable ids are assigned deterministically by the
    sanitizer (:func:`inqtrix.agents.clarification.sanitize_questions`),
    never by the LLM.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        description="The full question to the user, in the answer language, one sentence."
    )
    options: list[ClarificationOptionModel] = Field(
        description=(
            "2-4 likely answer options (empty when only free text makes "
            "sense — the question is then asked without chips)."
        )
    )
    multi_select: bool = Field(
        description=(
            "True only when several options can apply at once "
            "(the user may then pick more than one)."
        )
    )


class SufficiencyJudgement(BaseModel):
    """Fast-tier coverage verdict (gate semantics)."""

    model_config = ConfigDict(extra="forbid")

    coverage: str = Field(description="covered | partial | uncovered")
    missing: list[str] = Field(
        description="Criteria without sufficient evidence (empty when covered)."
    )


class MemoryCandidateModel(BaseModel):
    """One proposed long-term memory extracted from a completed run."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(description="user | workspace | project | agent")
    category: str = Field(
        description="preference | project_fact | strategy | correction"
    )
    content: str = Field(
        description="Short editable memory text, not raw evidence."
    )
    reason: str = Field(description="Why retaining this may help later.")
    confidence: float = Field(description="Confidence from 0.0 to 1.0.")


class MemoryReflection(BaseModel):
    """Candidate-only long-term memory reflection output."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidateModel]


class WindowFinding(BaseModel):
    """One passage of a document window that serves the objective."""

    model_config = ConfigDict(extra="forbid")

    # No length bounds: a schema violation would discard the whole window,
    # so the server judges each quote instead (knowledge.document.cite).
    quote: str = Field(
        min_length=1,
        description=(
            "The passage copied character for character from THIS window, "
            "within the limits the instructions state."
        ),
    )
    claim: str = Field(
        min_length=1,
        description="What the passage establishes for the objective.",
    )
    criterion: int | None = Field(
        default=None,
        ge=1,
        description="Number of the criterion the passage answers, if any.",
    )


class WindowExtraction(BaseModel):
    """The relevant passages of ONE document window (map step)."""

    model_config = ConfigDict(extra="forbid")

    findings: list[WindowFinding] = Field(
        description="Relevant passages in window order; empty when none."
    )
    more_relevant: bool = Field(
        default=False,
        description="True when the window holds more relevant passages than listed.",
    )


class CitationRepairText(BaseModel):
    """A complete synthesis text after one citation-label repair."""

    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(
        description=(
            "The complete original Markdown with only unsupported citation "
            "labels corrected or removed."
        )
    )
    """Complete Markdown after the sole bounded citation repair call."""


class DeepReviewFinding(BaseModel):
    """One fixable Deep defect targeted at chat or a concrete canvas."""

    model_config = ConfigDict(extra="forbid")

    target: Literal["chat", "artifact"] = Field(
        description="Output target carrying the defect."
    )
    """Whether the defect belongs to the chat answer or a canvas artifact."""
    artifact_id: str = Field(
        description="Concrete artifact id for target=artifact, otherwise ''."
    )
    """Artifact target id; empty for chat findings."""
    finding: str = Field(
        description=(
            "Concrete, fixable defect, in words a reader understands: name a "
            "source by its evidence label and title, a run diagnostic or "
            "subtask by its title, never by an identifier."
        )
    )
    """Actionable issue for the single revision call."""
    kind: Literal[
        "evidence_error", "run_diagnostic", "output_requirement", "missing_information",
    ] = Field(description="The kind of ground needed to assess and fix this issue.")
    quote: str = Field(
        min_length=1,
        description="Exact, unique verbatim passage in the target to replace or extend.",
    )
    basis_ids: list[str] = Field(
        description=(
            "Ids of supporting sources or run diagnostics from the supplied "
            "context; no invented ids. The ONLY field of this finding that "
            "carries an identifier."
        ),
    )
    remove_labels: list[str] = Field(
        default_factory=list, description=(
            "Evidence labels standing in quote (e.g. 'K3') that this fix "
            "removes: a label citing the wrong source. Empty when no label "
            "leaves the passage."
        )
    )
    """Labels of the quoted passage the fix may drop; the server checks each
    against the passage before the revision call."""
    criterion_index: int | None = Field(
        default=None, description=(
            "Number of the success criterion, as the handover lists it, that "
            "this finding concerns; null when it concerns none or the "
            "handover lists none."
        )
    )
    """The criterion this finding concerns. Deliberately without bounds: a
    ``ge=1`` survives strict schemas and would make the whole verdict invalid
    over one index; the server counts an index the handover does not list as
    none, visibly."""


class ReviewCriterion(BaseModel):
    """One success criterion of the run under work, accounted for by the review.

    Deliberately without a cross-field validator: an unmet criterion that
    lacks its kind is settled by the server as an open point of its own. A
    schema error would discard the whole verdict over one sloppy entry.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(
        ge=1, description="Number of the criterion as the handover lists it."
    )
    """Number of the criterion in the handover; every number appears exactly once."""
    met: bool = Field(
        description=(
            "True only when the outputs demonstrably meet the criterion, "
            "judged from material you read."
        )
    )
    """Whether the outputs demonstrably meet the criterion."""
    kind: Literal["", "information", "output_requirement"] = Field(
        description=(
            "For an unmet criterion: 'information' when factual evidence is "
            "missing and only more research could close it, "
            "'output_requirement' when the evidence exists and the output "
            "falls short in content or form. '' when met."
        )
    )
    """Who owns an unmet criterion: research or the text."""
    gap: str = Field(
        description=(
            "For an unmet criterion: what exactly is missing, in one "
            "sentence a reader understands; name a basis by its title or "
            "evidence label, never by an identifier. '' when met."
        )
    )
    """What is missing; empty when met."""


class DeepReviewVerdict(BaseModel):
    """Rubric verdict of the Deep verification pass.

    Checkable criteria only — completeness, groundedness, named
    contradictions. ``findings`` empty = the outputs pass. Every confirmed
    finding ends applied or named with its reason
    (:mod:`inqtrix.agents.kernel.review_edits`); a verdict triggers no review
    loop.
    """

    model_config = ConfigDict(extra="forbid")

    complete: bool = Field(
        description="True when the answer covers the full assignment."
    )
    """Whether the answer addresses every part of the assignment."""
    grounded: bool = Field(
        description=(
            "True when factual claims are supported by the supplied material "
            "at their stated scope, including conditions, attribution and time, "
            "or unsupported points are reported, as a finding on their passage or in "
            "not_checked. A valid label alone is insufficient."
        )
    )
    """Whether claims are evidenced, or their gaps reported in the verdict."""
    contradictions_named: bool = Field(
        description=(
            "True when uncertainties and counter-positions are named "
            "where relevant."
        )
    )
    """Whether relevant uncertainty/counter-evidence is surfaced."""
    findings: list[DeepReviewFinding] = Field(
        description=(
            "Concrete, fixable defects; EMPTY when the answer passes."
        )
    )
    """Actionable defects driving the single revision round."""
    criteria: list[ReviewCriterion] = Field(
        default_factory=list,
        description=(
            "One entry per success criterion the handover lists, by its "
            "number; EMPTY when the handover lists none."
        ),
    )
    """The handover's success criteria, each accounted for exactly once."""
    not_checked: list[str] = Field(
        default_factory=list,
        description=(
            "Points you could not positively confirm from material you "
            "actually read, each with the basis that was missing, named in "
            "reader words (its title or evidence label, never an "
            "identifier). Such a point is never reported as passed."
        ),
    )
    """What the review could not confirm from material it read; open, not passed."""


class DeepFindingRevision(BaseModel):
    """Replacement text for one server-identified, anchored finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(description="Exact server-assigned finding id from the request.")
    replacement: str = Field(
        description=(
            "Replace only that finding's quoted passage with its corrected, "
            "qualified or attributed text: it keeps the passage's content and "
            "is never empty."
        ),
    )


class DeepRevisionBundle(BaseModel):
    """Validated output of the one-and-only Deep revision call."""

    model_config = ConfigDict(extra="forbid")

    edits: list[DeepFindingRevision] = Field(
        description="Exactly one replacement per finding; never reproduce untouched output.",
    )


RecheckDimension = Literal[
    "same", "firmer_or_wider", "weaker_or_narrower", "contradicts", "not_in_basis", "unclear",
]
"""How a segment's new text compares with its quoted basis on one dimension."""

RecheckActorReach = Literal[
    "same", "omitted", "firmer_or_wider", "weaker_or_narrower", "contradicts",
    "not_in_basis", "unclear",
]
""":data:`RecheckDimension` for actors and reach, plus ``omitted``: the new
text names no actor where the basis names one, and names no other."""

RecheckKind = Literal[
    "duty", "deadline", "expectation", "permission", "example_or_indicator",
    "open_force_item", "fact", "reading_limit", "no_claim", "unclear",
]
"""What a text makes of a content: the kinds the re-check compares."""


class BasisQuote(BaseModel):
    """One passage a re-check row rests on; the server checks it verbatim."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        description=(
            "The passage's label as the output writes it (for example K3); "
            "empty for a field of this run's reading record."
        ),
    )
    quote: str = Field(
        description=(
            "The passage's words copied exactly and continuously in their own "
            f"language: no ellipsis, at least {CITE_QUOTE_MIN_WORDS} words; for the reading "
            "record a field with its value exactly as the record writes it."
        ),
    )


class SegmentRow(BaseModel):
    """The re-check's answers about one segment the server lists.

    The quotes come before every judgement. Deliberately without a
    cross-field validator: the server settles each finding from its rows
    (:func:`~inqtrix.agents.kernel.review_pass.settle_check`), and a schema
    error would discard every check of the call over one sloppy row. For a
    removed segment only ``before``, ``restated_in`` and ``finding_quote``
    count.
    """

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(description="Exact server-assigned segment id from the request.")
    # No length bound: a schema violation would discard the whole call, so
    # the server checks every quote instead.
    basis: list[BasisQuote] = Field(
        description=(
            "Zero to four passages that carry the segment's new text, each "
            "checked verbatim by the server; empty for new text that states "
            "nothing about a source and for a removed segment."
        ),
    )
    force: RecheckDimension = Field(
        description=(
            "Duty, expectation, permission or example as the new text states "
            "it, compared with the quoted basis as shown."
        ),
    )
    bound: RecheckDimension = Field(
        description=(
            "Numbers, periods, dates and counts with their direction as the "
            "new text states them, compared with the quoted basis."
        ),
    )
    actor_reach: RecheckActorReach = Field(
        description=(
            "Who is obliged, acts or receives and what a condition applies "
            "to, compared with the quoted basis; omitted when the new text "
            "names no actor where the basis names one, and names no other."
        ),
    )
    reservations: RecheckDimension = Field(
        description=(
            "Conditions, exceptions and qualifiers, compared with the quoted "
            "basis; firmer_or_wider when the new text drops one the basis "
            "attaches."
        ),
    )
    links: RecheckDimension = Field(
        description=(
            "Links and quantifiers (because, therefore, only, all, and/or) "
            "the new text states, compared with the quoted basis."
        ),
    )
    presents_as: RecheckKind = Field(
        description=(
            "What the whole replacement, read in its place, makes of this "
            "segment's content."
        ),
    )
    basis_shows: RecheckKind = Field(
        description="What the quoted basis, as shown, makes of the same content.",
    )
    already_in_replaced_text: bool = Field(
        description=(
            "True only for a segment with replaced text where every "
            "difference from same stood in the replaced text the same way."
        ),
    )
    before: Literal["none", "restated", "change_asked", "not_asked"] = Field(
        description=(
            "For a segment with replaced text, over everything that text "
            "stated: restated, change_asked or not_asked; none for a segment "
            "without replaced text."
        ),
    )
    restated_in: str = Field(
        description=(
            "For a removed segment that is restated: the id of the segment "
            "that restates it; otherwise empty."
        ),
    )
    finding_quote: str = Field(
        description=(
            "For change_asked: the finding's words that ask to remove or "
            "change exactly what is gone or changed, copied exactly, at least "
            f"{CITE_QUOTE_MIN_WORDS}; otherwise empty."
        ),
    )


class FindingCheck(BaseModel):
    """The re-check of one applied replacement: one row per listed segment."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(description="Exact server-assigned finding id from the request.")
    rows: list[SegmentRow] = Field(
        description="One row per segment id the server lists for this finding, in its order.",
    )
    fixes_named_defect: Literal["yes", "partly", "no"] = Field(
        description="Whether the replacement fixes the defect its finding names.",
    )
    reason: str = Field(
        description=(
            "One sentence naming what decided the rows; source labels, never "
            "identifiers."
        ),
    )
    """For the trace only: the server never logs it and never shows it."""


class RevisionCheckVerdict(BaseModel):
    """Validated output of the re-check of a revision's applied edits: the
    rows of each check and nothing beside them; the server settles each
    edit from them."""

    model_config = ConfigDict(extra="forbid")

    findings: list[FindingCheck] = Field(
        description="Exactly one check per finding id the server lists.",
    )
