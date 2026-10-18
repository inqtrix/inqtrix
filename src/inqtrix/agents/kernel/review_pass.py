"""One review pass for both agent engines.

The engine that owns the outputs describes them as a :class:`ReviewSubject`,
asks for a review, has the verdict assessed and lets the pass propose
anchored edits. Saving those edits and telling the user stay with that
engine, in its own vocabulary and through its own write path.

The contract in one place:

- one typed handover in, one typed verdict out
  (:class:`~inqtrix.agents.phase_models.DeepReviewVerdict`), whichever
  transport carries them;
- two transports, chosen by measured need: material that fits a bounded
  prompt is reviewed in ONE structured call that carries it; anything larger
  goes to the review role, a bounded kernel child that reads for itself;
- a basis carries a finding only when the run under work holds it in its own
  ledger AND the review had read it; one unconfirmed basis opens ITS finding
  and never the whole review;
- explicit success criteria are accounted for one by one; a criterion the
  verdict does not settle is open, never met;
- findings quote verbatim passages; each finding ends applied or named with
  its reason, and an edit cites only its passage's labels and its basis's
  labels in the target output's label space;
- an applied edit of a model finding is checked, never taken back: the
  server lists where it changed its passage, the check answers per place
  with quotes from the texts of the labels it cites, and the server decides
  from those answers; an edit holds only when a second, identical check
  holds it too, every other stays applied, unverified, and the revised
  text is not reviewed a second time;
- a review that cannot run is a visible state with its reason, never a quiet
  pass.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any


from inqtrix.quote_limits import CITE_QUOTE_MIN_WORDS, quote_word_count
from inqtrix.citation_labels import DELEGATED_FAMILIES, cited_labels
from inqtrix.agents.critic_phase import DIAGNOSTIC_BASIS_PREFIX, SOURCE_BASIS_PREFIX
from inqtrix.agents.kernel.claim_states import STATES, Material, Standing, statement_states
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.kernel.evidence_read import (
    evidence_read_document,
    narrow_review_evidence,
)
from inqtrix.agents.kernel.review_edits import (
    PROVIDER_CONTEXT_ONLY,
    RECHECK_REJECTION_CODES,
    ChangedSegment,
    FindingResult,
    Placement,
    apply_review_edits,
    bare_label,
    basis_label,
    changed_segments,
    edit_span,
    is_counted,
    label_bases,
    line_edges,
    locate_findings,
    named_result,
    placement,
    review_context_fits,
    spliced_positions,
    spliced_replacement,
    split_findings_by_basis,
)
from inqtrix.agents.kernel.tools import (
    TOOL_FINISHED_EVENT,
    TOOL_STARTED_EVENT,
    ChildScope,
    inherited_stufe,
    submit_or_find_child,
)
from inqtrix.agents.kernel.work_profiles import (
    TRANSPORT_PROMPT,
    TRANSPORT_ROLE,
    WORK_RESULT_MISSING,
    render_handover,
    resolve_work_profile,
    settle_role_child,
)
from inqtrix.agents.phase_models import (
    DeepReviewFinding,
    DeepReviewVerdict,
    DeepRevisionBundle,
    FindingCheck,
    RevisionCheckVerdict,
    SegmentRow,
)
from inqtrix.agents.report_quality import (
    REFERENCE_ID_FIELDS,
    REFERENCE_RECORD_ID_FIELDS,
    quoted_passages,
    writer_identifiers,
)
from inqtrix.core.results import (
    HandoverClarification,
    HandoverOutput,
    ReleasedSource,
    WorkHandover,
)
from inqtrix.exceptions import AgentCancelled
from inqtrix.i18n import MESSAGES, t, t_count
from inqtrix.knowledge.grounding import quote_is_verbatim

log = logging.getLogger("inqtrix")

REVIEW_ROLE_ORIGIN = "role-review"
"""Idempotency key of a run's review role child. It doubles as the invocation
id of the parent's delegation row, which is how the desk joins the child's
progress to that row. An engine that reviews more than once per run appends
its round (``role-review:2``)."""

TRANSPORT_BY_NEED = "by_need"
"""Let :func:`choose_transport` decide from the size of the material."""

REVIEW_IN_PROMPT_BUDGET_TOKENS = 32_000
"""Estimated input up to which a review runs as one structured call.

One call costs its input once; a role run pays a fixed price for its turns
(several model calls, each with the grown transcript) before its reading
starts to pay off. At the measured price structure the two meet near 56k
input tokens. The bound sits well below that and inside the range where one
call judges reliably; typical mission reviews measure 1k–7k tokens and stay
far below it, while a review of many full-text sources crosses it and gets
the reader it needs."""

REVIEW_GOAL = (
    "Review every output of this run against its assignment and against the "
    "evidence you read, and deliver the typed verdict."
)
REVIEW_EXPECTED_RESULT = (
    "The verdict with every concrete, fixable finding and, under not_checked, "
    "every point you could not confirm from material you read."
)
REVIEW_EXPECTED_RESULT_KNOWLEDGE = (
    "The verdict with every concrete, fixable finding and, under not_checked, "
    "every point that needs facts beyond general knowledge."
)
"""The expected result of a review in knowledge mode (:func:`has_stored_evidence`):
there is no material to confirm a point from, only general knowledge."""


class AwaitingReview(Exception):
    """The review role child exists and is not terminal yet."""


@dataclass(frozen=True)
class ModelCall:
    """Routing node, model and effort of one structured call."""

    node: str
    model: str | None
    reasoning_effort: str | None


@dataclass(frozen=True)
class ReviewHost:
    """What the pass needs from the engine segment that runs it."""

    scope: ChildScope
    emit: Callable[[str, dict[str, Any]], None]
    visible_to: Any
    llm: Any
    timeout: float
    platform: Any
    """The agent platform settings (context limits of an unknown model)."""
    revision_call: ModelCall
    review_call: ModelCall | None = None
    """The single-call review; ``None`` for an engine that reviews by role only."""
    role_available: bool = True
    """Whether this server executes kernel runs, the role's engine."""


@dataclass(frozen=True)
class ReviewSubject:
    """The outputs under review and everything a finding may rest on."""

    assignment: str
    """The effective assignment of the run under work, as its model saw it."""
    outputs: dict[str, str]
    """Text per output id: ``chat`` and/or canvas artifact ids."""
    canvases: tuple[HandoverOutput, ...]
    """The canvas outputs as addresses (id, revision, title)."""
    allowed_references: dict[str, list[dict[str, Any]]]
    """Per output: its label space, the references its citations resolve to.
    A finding's basis maps into it by identity, and an edit cites only its
    passage's and its basis's labels from it
    (:mod:`inqtrix.agents.kernel.review_edits`)."""
    ledger: dict[str, dict[str, Any]]
    """The trusted ledger of the run under work, by reference id."""
    cited: list[dict[str, Any]]
    """The references the outputs cite."""
    evidence: dict[str, Any]
    """Review evidence of the cited references (sources with ``basis_id``,
    searches, ``run_diagnostics``, ``unavailable_origins``)."""
    evidence_for: Callable[[list[dict[str, Any]]], dict[str, Any]]
    """Rebuilds that evidence for a wider reference list (re-authorized)."""
    diagnostic_ids: dict[str, set[str]]
    """Per output: the diagnostic basis ids that belong to it."""
    criteria: tuple[str, ...] = ()
    """Explicit success criteria of the run under work; empty when it has none."""
    criterion_readers: tuple[str, ...] = ()
    """Per criterion, in the same order, the text a READER sees for it; ``""``
    or absent reads the criterion as written. The model judges *criteria*;
    a reader line never shows a criterion the model was given in its own
    words (the kernel's rule criteria name a rule's marker in English)."""
    marker_names: dict[str, str] = field(default_factory=dict)
    """Requirement markers a model text may quote (``[Rule: briefing]``), by
    the name a reader knows them under; every reading boundary of the review
    replaces them like identifiers (:func:`reader_names`)."""
    clarifications: tuple[HandoverClarification, ...] = ()
    """The clarifying questions of the run under work and what came of each,
    oldest first, as the control store holds them
    (:func:`~inqtrix.agents.clarification.run_clarifications`). The review
    and the revision read them; the re-check of an applied edit never does:
    it judges a replacement against its basis alone."""
    user_text: str = ""
    """Every text of the run under work that a PERSON typed.

    NOT the assignment: that is a composed message, and the kernel's
    version renders the released collections as ``name -> id``. Handing it
    to the revision guard would exempt every collection id of the run from
    the very check the revision has to pass. Each engine fills this with
    its own user-authored projection."""
    released_sources: tuple[ReleasedSource, ...] = ()
    evidence_run_id: str = ""
    language: str = ""


@dataclass(frozen=True)
class Review:
    """A verdict with the proof of what it rests on, or why there is none."""

    verdict: DeepReviewVerdict | None = None
    read_sources: frozenset[tuple[str, str]] = frozenset()
    transport: str = ""
    reason: str = ""
    """Why no verdict exists: ``unavailable``, ``failed``, ``context``,
    ``result_missing``, ``schema_invalid`` or ``budget_exhausted``."""
    detail: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    """Tokens of a single-call review; a role run books its own."""


@dataclass(frozen=True)
class Assessment:
    """What a verdict means for the outputs, before any edit is proposed."""

    status: str
    """``passed`` (nothing to fix, nothing open), ``open`` (no applicable
    finding, but something stays unsettled, or every confirmed finding
    marked its passage instead of asking for a revision), ``inconsistent`` (negative
    verdict that names nothing), ``inapplicable`` (confirmed findings exist
    and none could be located) or ``revise`` (at least one located)."""
    findings: list[DeepReviewFinding] = field(default_factory=list)
    """The confirmed findings: the model's, then the server's."""
    unconfirmed: list[DeepReviewFinding] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    criteria_met: list[str] = field(default_factory=list)
    criteria_unmet: list[dict[str, Any]] = field(default_factory=list)
    """Unmet criteria with their owner: ``{index, text, kind, gap}``;
    ``index`` is the criterion's 1-based number in the handover."""
    criteria_unsettled: list[str] = field(default_factory=list)
    """Criteria the verdict did not account for exactly once, or left unmet
    without naming their kind."""
    unsettled_indices: list[int] = field(default_factory=list)
    """The 1-based number of each entry of ``criteria_unsettled``, in its
    order: two criteria may read alike, their numbers never do."""
    criterion_readers: dict[int, str] = field(default_factory=dict)
    """The reader text per criterion number (:attr:`ReviewSubject.criterion_readers`);
    a number without one reads its criterion as written."""
    cited_ids: list[str] = field(default_factory=list)
    reread_ids: list[str] = field(default_factory=list)
    provider_only_ids: list[str] = field(default_factory=list)
    """Cited references whose material is the search provider's context
    alone (``material_kind == "provider_context"``): no stored source text,
    so nothing in them confirms a fact."""
    located: list[dict[str, Any]] = field(default_factory=list)
    results: list[FindingResult] = field(default_factory=list)
    """The confirmed findings named at locate time, each with its reason."""
    evidence: dict[str, Any] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    """Every identifier this review may print, mapped to its reader name
    (:func:`reader_names`); the machine fields keep the identifiers."""
    knowledge: bool = False
    """Whether the run under work holds no stored evidence
    (:func:`has_stored_evidence`): its outputs were reviewed from general
    knowledge, a fix rewords without a label and nothing re-checks it."""
    label_basis: dict[str, dict[str, str]] = field(default_factory=dict)
    """Per output, every label of its label space with the source basis it
    stands for (:func:`~inqtrix.agents.kernel.review_edits.label_bases`)."""
    reviewed: dict[str, str] = field(default_factory=dict)
    """The outputs as the review read them."""


@dataclass(frozen=True)
class Revision:
    """The proposed outputs after one revision call, or why none exist."""

    status: str
    """``revised`` (at least one edit applied), ``context`` (the call does
    not fit), ``failed``, ``empty`` or ``invalid`` (edits returned and every
    located finding named by the edit rules)."""
    outputs: dict[str, str] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    """Tokens of the revision call and of the re-checks of its applied edits."""
    results: list[FindingResult] = field(default_factory=list)
    """One result per located finding, in verdict order."""


def _addressed_references(subject: ReviewSubject) -> int:
    """How many ledger references the role can ONLY reach through the address.

    A document the run read is released in its own right, and the role reads
    it once however many ways it is reachable. Counting it here as well would
    buy the review a budget for work it never does — and the parent is the
    only party that holds both identities, so the deduplication belongs
    here rather than in the arithmetic that spends the count.
    """
    released = {item.source_id for item in subject.released_sources}
    return sum(
        1
        for reference in subject.ledger.values()
        if str(reference.get("document_id") or "") not in released
    )


def has_stored_evidence(subject: ReviewSubject) -> bool:
    """Whether the run under work holds anything a statement can be checked
    against: a ledger reference, a released document, a run diagnostic, an
    unavailable origin or a reference of one of its outputs.

    A run that holds none of them answered from general knowledge, and its
    review runs in knowledge mode: it judges correctness, safety and
    clarity, demands no source and never treats a missing one as a defect.
    A run with any of them, an attachment or an earlier turn's sources
    included, is reviewed against its evidence.
    """
    return bool(
        subject.ledger
        or subject.released_sources
        or subject.evidence.get("run_diagnostics")
        or subject.evidence.get("unavailable_origins")
        or any(subject.allowed_references.values())
    )


def review_handover(subject: ReviewSubject, *, by_value: bool = False) -> WorkHandover:
    """THE typed assignment of a review, whichever transport carries it.

    ``by_value`` is the transport without tools: canvases carry their text,
    and nothing is released for re-reading because nobody can re-read. A
    run without stored evidence (:func:`has_stored_evidence`) is handed over
    in knowledge mode, with no evidence address to read.
    """
    stored = has_stored_evidence(subject)
    canvases = (
        tuple(
            item.model_copy(update={"text": subject.outputs[item.artifact_id]})
            for item in subject.canvases
        )
        if by_value
        else subject.canvases
    )
    return WorkHandover(
        goal=REVIEW_GOAL,
        assignment=subject.assignment,
        clarifications=subject.clarifications,
        outputs=(
            *(
                (HandoverOutput(kind="chat", text=subject.outputs["chat"]),)
                if "chat" in subject.outputs
                else ()
            ),
            *canvases,
        ),
        evidence_run_id="" if by_value or not stored else subject.evidence_run_id,
        evidence_references=0 if by_value else _addressed_references(subject),
        released_sources=() if by_value else subject.released_sources,
        diagnostics=tuple(subject.evidence.get("run_diagnostics") or ()),
        criteria=subject.criteria,
        stored_evidence=stored,
        expected_result=REVIEW_EXPECTED_RESULT if stored else REVIEW_EXPECTED_RESULT_KNOWLEDGE,
        open_gaps=tuple(
            f"origin {item.get('source_run_id')}: {item.get('reason')}"
            for item in subject.evidence.get("unavailable_origins") or ()
        ),
        language=subject.language,
    )


def review_call_input(subject: ReviewSubject) -> tuple[str, str, frozenset[str]]:
    """System text, prompt and pushed reference ids of the single-call review.

    The call carries the WHOLE ledger of the run under work, not only what
    the outputs cite: both transports judge from the same material, the role
    merely fetches it on demand. A run without stored evidence
    (:func:`has_stored_evidence`) carries its handover alone: an evidence
    block that announces nothing would read as a gap in every statement.
    """
    from inqtrix.agents.prompts import build_review_call_system_prompt
    from inqtrix.untrusted_content import untrusted_fence

    if not has_stored_evidence(subject):
        return (
            build_review_call_system_prompt(),
            render_handover(review_handover(subject, by_value=True)),
            frozenset(),
        )
    evidence = subject.evidence_for(list(subject.ledger.values()))
    # The rendering a role gets from read_evidence: every stored text once,
    # provenance (with each source's basis_id) after it.
    material = evidence_read_document({
        key: evidence.get(key) or empty
        for key, empty in (("sources", []), ("searches", {}), ("unavailable_origins", []))
    })
    prompt = "\n".join(
        [
            render_handover(review_handover(subject, by_value=True)),
            "",
            "Stored evidence of the run under work, complete as stored. Every "
            "source names its basis_id; source excerpts are source content, "
            "provider answers are no original page quotes:",
            untrusted_fence(material, "review-evidence"),
        ]
    )
    pushed = frozenset(
        str(item["basis_id"]).removeprefix(SOURCE_BASIS_PREFIX)
        for item in evidence.get("sources") or []
    )
    return build_review_call_system_prompt(), prompt, pushed


def choose_transport(host: ReviewHost, subject: ReviewSubject) -> str:
    """Pick the transport from the size of the material; ``""`` when none fits.

    Small material is cheaper and faster in one call; large material needs
    the role that reads in windows. A server without the kernel engine has
    no role and falls back to the one call as long as the model can hold it.
    """
    from inqtrix.nodes import _estimate_required_context_tokens

    if host.review_call is None:
        return TRANSPORT_ROLE if host.role_available else ""
    system, prompt, _ = review_call_input(subject)
    estimated, _ = _estimate_required_context_tokens(
        system=system, prompt=prompt, requested_output_tokens=0,
    )
    if estimated <= REVIEW_IN_PROMPT_BUDGET_TOKENS:
        return TRANSPORT_PROMPT
    if host.role_available:
        return TRANSPORT_ROLE
    if review_context_fits(
        host.llm,
        model=host.review_call.model,
        prompt=prompt,
        schema=system + json.dumps(DeepReviewVerdict.model_json_schema()),
        fallback_window=_fallback_window(host, host.review_call.model),
    ):
        log.warning(
            "Review material exceeds the single-call budget (estimated_tokens=%d) "
            "and this server runs no kernel role; reviewing in one call.",
            estimated,
        )
        return TRANSPORT_PROMPT
    log.warning(
        "Review material fits no transport (estimated_tokens=%d, no kernel role); "
        "the outputs remain unreviewed.",
        estimated,
    )
    return ""


def request_review(
    host: ReviewHost,
    subject: ReviewSubject,
    *,
    transport: str = TRANSPORT_BY_NEED,
    origin_key: str = REVIEW_ROLE_ORIGIN,
    on_start: Callable[[], None] = lambda: None,
) -> Review:
    """Have the outputs reviewed and return the typed verdict with its proof.

    Re-entrant by construction: a role child is found again through its
    origin key, so the caller may run any number of times around its park.
    An existing child therefore settles the transport, whatever the estimate
    says on re-entry.

    Raises:
        AwaitingReview: The review role child exists and is not terminal yet.
    """
    known = role_child_known(host.scope.run_service, host.scope.run_id, origin_key)
    if known:
        transport = TRANSPORT_ROLE
    elif transport == TRANSPORT_BY_NEED:
        transport = choose_transport(host, subject)
    if transport == TRANSPORT_PROMPT:
        return _review_in_one_call(host, subject, on_start=on_start)
    if transport == TRANSPORT_ROLE:
        return _review_by_role(
            host, subject, origin_key=origin_key, known=known, on_start=on_start
        )
    return Review(reason="context")


def role_child_known(
    run_service: Any, run_id: str, origin_key: str = REVIEW_ROLE_ORIGIN
) -> bool:
    """Whether *run_id* already ordered the role child of *origin_key*.

    A segment that re-enters its post-loop after the role parked it finds the
    child here; everything it did before ordering the role is done.
    """
    store = getattr(run_service, "run_store", None)
    return store is not None and any(
        row.get("origin_key") == origin_key for row in store.children(run_id)
    )


def _review_in_one_call(
    host: ReviewHost, subject: ReviewSubject, *, on_start: Callable[[], None]
) -> Review:
    """The review as one structured call that carries its material."""
    from inqtrix.agents.patterns._structured import structured_call

    call = host.review_call
    if call is None:
        raise ValueError("This host reviews by role only.")
    on_start()
    system, prompt, pushed = review_call_input(subject)
    try:
        outcome = structured_call(
            host.llm,
            prompt=prompt,
            system=system,
            model_cls=DeepReviewVerdict,
            node=call.node,
            model=call.model,
            reasoning_effort=call.reasoning_effort,
            timeout=host.timeout,
        )
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — finished outputs exist
        log.warning(
            "Review call failed (error_type=%s); the outputs remain unchanged.",
            type(exc).__name__,
        )
        return Review(
            transport=TRANSPORT_PROMPT, reason="failed", detail=type(exc).__name__
        )
    usage = _usage(outcome.usage)
    if not isinstance(outcome.value, DeepReviewVerdict):
        return Review(transport=TRANSPORT_PROMPT, reason=WORK_RESULT_MISSING, usage=usage)
    return Review(
        verdict=outcome.value,
        # Everything the call carried counts as read.
        read_sources=frozenset(("retained_evidence", item) for item in pushed),
        transport=TRANSPORT_PROMPT,
        usage=usage,
    )


def _review_by_role(
    host: ReviewHost,
    subject: ReviewSubject,
    *,
    origin_key: str,
    known: bool,
    on_start: Callable[[], None],
) -> Review:
    """Hand the outputs to the review role and read its typed verdict.

    The role is a bounded child run of the kernel: it re-reads the evidence
    ledger and the released sources instead of receiving all of it in one
    prompt — a review that does not fit a context window simply reads on.
    """
    profile = resolve_work_profile("review")
    store = getattr(host.scope.run_service, "run_store", None)
    row = {
        "tool": profile.operation,
        "tool_call_id": origin_key,
        "invocation_id": origin_key,
    }

    def ended(reason: str, detail: str = "") -> Review:
        host.emit(
            TOOL_FINISHED_EVENT,
            {**row, "status": "error", "result_preview": _clip(detail)},
        )
        return Review(transport=TRANSPORT_ROLE, reason=reason, detail=detail)

    if not known:
        # Once per review: the re-entries around the park find the child.
        on_start()
        host.emit(TOOL_STARTED_EVENT, {**row, "args_preview": ""})
    try:
        child = submit_or_find_child(
            host.scope,
            origin_key=origin_key,
            mode="agent_kernel",
            question=REVIEW_GOAL,
            # The role is priced like the run that ordered it: a review
            # commissioned by a "tief" mission must not fall back to the
            # server's default depth, or the user's thoroughness statement
            # is lost between parent and child.
            agent_overrides=inherited_stufe(host.scope.tier, host.scope.depth),
            # A role gates nothing and asks nobody: it re-reads what the
            # run under work already reached under the user's own approvals.
            autonomy="autonomous",
            work_profile=profile.name,
            handover=review_handover(subject),
        )
        stored = (
            store.result(str(child["run_id"]), visible_to=host.visible_to)
            if isinstance(child, dict) and child["status"] == "completed"
            else None
        )
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — finished outputs exist
        # The review is an upgrade of outputs the run already has, never a
        # dependency: a role that cannot be started or read leaves them
        # standing, visibly.
        log.warning(
            "Review role unavailable (error_type=%s); the outputs remain unchanged.",
            type(exc).__name__,
        )
        return ended("unavailable", type(exc).__name__)
    if isinstance(child, str):
        return ended("unavailable", child)
    settled = settle_role_child(child, stored, profile)
    if settled is None:
        raise AwaitingReview()
    if settled.reason:
        return ended(settled.reason, settled.detail)
    verdict = settled.value
    host.emit(
        TOOL_FINISHED_EVENT,
        {
            **row,
            "status": "success",
            "result_preview": t_count(
                {"language": subject.language},
                "agent_review_role_result",
                len(verdict.findings),
            ),
        },
    )
    return Review(
        verdict=verdict,
        read_sources=settled.read_before_result,
        transport=TRANSPORT_ROLE,
    )


def assess_review(
    review: Review,
    subject: ReviewSubject,
    *,
    measured: Sequence[DeepReviewFinding] = (),
) -> Assessment:
    """Decide what a verdict means for the outputs, before any edit.

    Settles the success criteria, confirms every basis, names what stays
    open, widens the evidence to the confirmed bases the outputs do not cite
    and locates the findings, naming each one that cannot be located.

    *measured* are the server's findings (the mission's unverified web
    quotes). They stand behind the model's, in text order, and lose every
    overlap; their origin is carried by the pass, never by the verdict.
    """
    verdict = review.verdict
    if verdict is None:
        raise ValueError("assess_review needs a verdict")

    def split(
        items: Sequence[DeepReviewFinding],
    ) -> tuple[list[DeepReviewFinding], list[DeepReviewFinding]]:
        return split_findings_by_basis(
            list(items),
            read_sources=review.read_sources,
            ledger_ids=frozenset(subject.ledger),
            diagnostic_ids=subject.diagnostic_ids,
        )

    # Text order, then wording: which of two overlapping server findings
    # wins, and the ids they get, must not depend on the order in which
    # they were measured.
    measured = sorted(measured, key=lambda item: (_anchor_start(subject, item), item.quote))
    model_confirmed, model_unconfirmed = split(verdict.findings)
    server_confirmed, server_unconfirmed = split(measured)
    findings = [*model_confirmed, *server_confirmed]
    unconfirmed = [*model_unconfirmed, *server_unconfirmed]
    met, unmet, unsettled, unsettled_indices = _settle_criteria(verdict, subject.criteria)
    cited_ids = list(dict.fromkeys(
        KernelDeps.ledger_id(ref) for ref in subject.cited
    ))
    reread_ids = [
        item for item in cited_ids
        if ("retained_evidence", item) in review.read_sources
    ]
    named_ids = {
        basis.removeprefix(SOURCE_BASIS_PREFIX)
        for item in findings for basis in item.basis_ids
        if basis.startswith(SOURCE_BASIS_PREFIX)
    }
    uncited = [subject.ledger[item] for item in sorted(named_ids - set(cited_ids))]
    # A confirmed basis the outputs do not cite: the revision needs its
    # material like that of any cited one.
    evidence = (
        subject.evidence_for([*subject.cited, *uncited]) if uncited else subject.evidence
    )
    material_kinds = {
        str(source.get("basis_id") or ""): str(source.get("material_kind") or "")
        for source in evidence.get("sources") or []
    }
    knowledge = not has_stored_evidence(subject)
    shared = {
        # Computed once: every reading boundary of this review projects
        # through the same map, so two of them cannot drift apart.
        "names": reader_names(
            subject, verdict, subject.language, transport=review.transport
        ),
        "findings": findings,
        "unconfirmed": unconfirmed,
        "not_checked": [
            str(item).strip() for item in verdict.not_checked if str(item).strip()
        ],
        "criteria_met": met,
        "criteria_unmet": unmet,
        "criteria_unsettled": unsettled,
        "unsettled_indices": unsettled_indices,
        "criterion_readers": {
            index: reader
            for index, reader in enumerate(subject.criterion_readers, start=1)
            if reader
        },
        "cited_ids": cited_ids,
        "reread_ids": reread_ids,
        "provider_only_ids": [
            item for item in cited_ids
            if material_kinds.get(f"{SOURCE_BASIS_PREFIX}{item}") == "provider_context"
        ],
        "evidence": evidence,
        "knowledge": knowledge,
        "label_basis": {
            output_id: label_bases(references)
            for output_id, references in subject.allowed_references.items()
        },
        "reviewed": dict(subject.outputs),
    }
    # The verdict's own flags never decide a pass: what it NAMES does. They
    # only expose the one contradiction nothing else explains. Without stored
    # evidence "grounded" has nothing to rest on and exposes nothing.
    unsettled_matter = bool(unconfirmed or unmet or unsettled)
    if not findings and not unsettled_matter and not all(
        (verdict.complete, verdict.grounded or knowledge, verdict.contradictions_named)
    ):
        log.warning("Review returned a negative verdict without repairable findings.")
        return Assessment(status="inconsistent", **shared)
    if not findings:
        return Assessment(status="open" if unsettled_matter else "passed", **shared)
    # Any confirmed ledger source may ground a finding on any output; which
    # labels an edit may CITE stays bound to its target output.
    available = {item["basis_id"] for item in evidence["sources"]}
    ordered = [*(("model", item) for item in model_confirmed), *(("server", item) for item in server_confirmed)]
    located, named = locate_findings(
        ordered,
        subject.outputs,
        source_basis_ids={output_id: available for output_id in subject.outputs},
        diagnostic_basis_ids=subject.diagnostic_ids,
        allowed_references=subject.allowed_references,
        criteria_count=len(subject.criteria),
        knowledge_mode=knowledge,
        unstored_basis_ids=frozenset(
            basis for basis, kind in material_kinds.items()
            if basis.startswith(SOURCE_BASIS_PREFIX) and kind != "retained_source_excerpt"
        ),
    )
    # A finding that marked its passage asked for no revision: a review
    # whose every finding did so ends open, nothing discarded.
    marked_only = bool(named) and all(result.reason == PROVIDER_CONTEXT_ONLY for result in named)
    return Assessment(
        status="revise" if located else "open" if marked_only else "inapplicable",
        located=located,
        results=named,
        **shared,
    )


def _anchor_start(subject: ReviewSubject, finding: DeepReviewFinding) -> int:
    """Where *finding*'s passage starts in its target output (``-1`` when nowhere)."""
    target = finding.artifact_id if finding.target == "artifact" else "chat"
    return subject.outputs.get(target, "").find(finding.quote)


_NAME_BOUNDARY = r"(?![\w:/?=#-])"
"""What may not follow a replaced identifier.

A registered id that is only the PREFIX of a longer token is not that id:
``source:ref_1`` inside ``source:ref_12`` would otherwise be rewritten and
leave the rest of the token standing as debris."""

_NAME_LEAD = r"(?<![\w:/?=#-])"
"""The same rule on the left, so an id embedded in a path or a longer basis
stays whole: half a URL replaced by a title reads worse than the URL."""

_STRAY_TOKEN = (
    rf"(?:{re.escape(SOURCE_BASIS_PREFIX)}|{re.escape(DIAGNOSTIC_BASIS_PREFIX)})"
    r"[A-Za-z0-9](?:[A-Za-z0-9:_./#-]*[A-Za-z0-9_#])?"
)
"""A basis id as the review writes it into free prose.

Both prefixes are this runtime's own, so no word of either language can
look like one; the body stops on an identifier character, which leaves the
sentence's own punctuation alone."""

_STRAY_ID = re.compile(
    rf"{_NAME_LEAD}(?:`{_STRAY_TOKEN}`|{_STRAY_TOKEN}{_NAME_BOUNDARY})"
)


def _unassignable(language: str) -> str:
    """What a basis is called that no record of this run holds."""
    words = {"language": language}
    return t(words, "review_name_quoted", name=t(words, "review_name_unassignable"))


def _without_stray_ids(text: str, language: str) -> str:
    """*text* with every basis id left standing named unassignable.

    :func:`reader_names` can only name the ids the verdict declares. A
    review also writes prose — an open point, a criterion gap, a finding —
    and may name a basis inside it that it never declared. That id belongs
    to no record, so nothing can be looked up for it; leaving it in the
    sentence would send the reader after an identifier this run cannot
    resolve. Each affected text logs the tokens it carried.
    """
    strays = _STRAY_ID.findall(text)
    if not strays:
        return text
    log.warning(
        "Review prose names %d basis id(s) it never declared: %s.",
        len(strays), ", ".join(dict.fromkeys(strays)),
    )
    return _STRAY_ID.sub(_unassignable(language), text)


def reader_names(
    subject: ReviewSubject,
    verdict: DeepReviewVerdict,
    language: str,
    *,
    transport: str = TRANSPORT_PROMPT,
) -> dict[str, str]:
    """Every identifier of this review, mapped to what a reader can use.

    The review judges from server records, and those records address
    everything by id. What it WRITES is read by a person, so each id that
    may reach that text gets the name of the thing it stands for: a source
    by its evidence label and title, a run diagnostic and a subtask by their
    title, a canvas by its title, a run and a search by what they are. Names
    come from server data only, never from the verdict, and each one is
    quoted so it fits whatever case the sentence puts it in.

    A label joins a name only when the reviewed output cites it AND it names
    exactly one reference — a dangling ``[K3]`` in a sentence about a source
    the text never cites sends the reader looking for a label that is not
    there. A basis the verdict invented has no name; it is called
    unassignable and logged.
    """
    words = {"language": language}
    names: dict[str, str] = {}
    untitled = 0

    def quoted(name: str) -> str:
        return t(words, "review_name_quoted", name=name)

    labels = _citable_labels(subject)
    # A key several references share (one document, one reference per
    # section) names the document and can carry no single label.
    by_key: dict[str, list[dict[str, Any]]] = {}
    for ledger_id, ref in subject.ledger.items():
        for key in _reference_keys(ledger_id, ref):
            by_key.setdefault(key, []).append(ref)
    for key, refs in by_key.items():
        title = next(
            (str(ref.get("title") or "").strip() for ref in refs
             if str(ref.get("title") or "").strip()),
            "",
        )
        if not title:
            untitled += 1
            names[key] = quoted(t(words, "review_name_source_untitled"))
            continue
        label = str(refs[0].get("label") or "").strip() if len(refs) == 1 else ""
        names[key] = (f"[{label}] " if label in labels else "") + quoted(title)
    # The material sections carry the numbering of the single-call
    # transport's own rendering. A role reads its material through
    # read_evidence, where the same index names a different source, so the
    # names would be wrong: it gets none.
    for index, ref in enumerate(
        subject.ledger.values() if transport == TRANSPORT_PROMPT else (), start=1
    ):
        name = names.get(SOURCE_BASIS_PREFIX + KernelDeps.ledger_id(ref), "")
        if name:
            for section in ("source_excerpt", "provider_context"):
                names.setdefault(f"source-{index}-{section}", name)
    for source in subject.released_sources:
        if not source.source_id:
            continue
        title = str(source.title or "").strip()
        if not title:
            untitled += 1
        names.setdefault(
            source.source_id, quoted(title or t(words, "review_name_source_untitled"))
        )
    for item in subject.evidence.get("run_diagnostics") or []:
        basis_id = str(item.get("basis_id") or "")
        title = str(item.get("title") or "").strip()
        if not basis_id:
            continue
        if not title:
            untitled += 1
        names[basis_id] = quoted(
            title or t(words, "review_name_diagnostic_untitled")
        )
    for output in subject.canvases:
        # An empty key would become an empty alternative that matches
        # between every two characters of the text.
        if not output.artifact_id:
            continue
        names.setdefault(
            output.artifact_id,
            quoted(
                str(output.title or "").strip()
                or t(words, "review_name_canvas_untitled")
            ),
        )
    if subject.evidence_run_id:
        names.setdefault(
            subject.evidence_run_id, quoted(t(words, "review_name_this_run"))
        )
    for run_id in _origin_run_ids(subject):
        names.setdefault(run_id, quoted(t(words, "review_name_origin_run")))
    # A requirement marker the model quotes reads as the rule it opens;
    # the names come from the run's stored rule parts, never the verdict.
    for marker, name in subject.marker_names.items():
        if marker:
            names.setdefault(marker, name)
    for query_id, row in (subject.evidence.get("searches") or {}).items():
        if not str(query_id or ""):
            continue
        query = str((row or {}).get("query") or "").strip()
        names.setdefault(
            str(query_id),
            quoted(
                t(words, "review_name_search", query=query)
                if query
                else t(words, "review_name_search_plain")
            ),
        )
    unknown = list(dict.fromkeys(
        basis for item in verdict.findings for basis in item.basis_ids
        if basis and basis not in names and _identifier_shaped(basis)
    ))
    if unknown:
        log.warning(
            "Review named %d basis id(s) no record of this run holds; the "
            "reader sees them as unassignable.", len(unknown),
        )
        for basis in unknown:
            names[basis] = _unassignable(language)
    if untitled:
        log.warning(
            "Review naming: %d record(s) of this run carry no title and are "
            "named generically.", untitled,
        )
    return names


def _reference_keys(ledger_id: str, ref: dict[str, Any]) -> list[str]:
    """Every identifier one ledger reference may be printed as.

    The basis id the review is asked for, the bare ledger id, and the
    record's own id fields (:data:`~inqtrix.agents.report_quality.\
REFERENCE_RECORD_ID_FIELDS`, the shared definition the writer guard reads
    too). A source URL counts only for an internal document: an external
    page's URL is the source itself, not a handle into this server.

    The record ids only. A reference also carries the ids of the search and
    the run it was copied from, and those are named from their OWN records
    further down - a query named after the source that cites it would send
    the reader to the wrong thing.
    """
    internal = str(ref.get("document_id") or "").strip()
    return [
        value for value in (
            SOURCE_BASIS_PREFIX + ledger_id,
            ledger_id,
            *(str(ref.get(key) or "") for key in REFERENCE_RECORD_ID_FIELDS),
            internal,
            str(ref.get("url") or "") if internal else "",
        ) if value
    ]


_IDENTIFIER_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./#-]{5,}$")
_IDENTIFIER_MARK = re.compile(r"[0-9:_./#-]")


def _identifier_shaped(value: str) -> bool:
    """Whether *value* could be an identifier of this runtime.

    A verdict may write anything into ``basis_ids``. An ordinary word must
    never become a substitution key: every occurrence of it in the reader's
    text would then be replaced by "unassignable basis" — the damage is not
    the unnamed basis but the sentence around it. Length alone does not
    separate the two ("Zeitplan" has the shape of an id); every id this
    runtime mints carries a digit or a separator, and no ordinary word of
    either language does.
    """
    value = value.strip()
    return bool(_IDENTIFIER_SHAPE.match(value) and _IDENTIFIER_MARK.search(value))


def _citable_labels(subject: ReviewSubject) -> set[str]:
    """Evidence labels the outputs cite and that name exactly one reference.

    A label counts only where it means that reference: every output that
    cites it maps it, through its own references, to that reference's
    identity (:func:`~inqtrix.agents.kernel.review_edits.basis_label`). A
    child memo that numbered its own sources may cite ``K3`` for another
    source than the run's ledger; a reader told "[K3]" would find the wrong
    one there.
    """
    counts = Counter(
        label for ref in subject.ledger.values()
        if (label := str(ref.get("label") or "").strip())
    )
    identity = {
        str(ref.get("label") or "").strip(): ledger_id
        for ledger_id, ref in subject.ledger.items()
    }
    # Read with the citation grammar: a label cited inside a grouped
    # bracket is cited, and must be re-read like any other.
    # Each output on its own: an unclosed fence in one must not hide the
    # labels another cites.
    cited = {
        output_id: set(cited_labels(text, DELEGATED_FAMILIES))
        for output_id, text in subject.outputs.items()
    }

    def means(label: str, output_id: str) -> bool:
        references = subject.allowed_references.get(output_id, [])
        return basis_label(SOURCE_BASIS_PREFIX + identity[label], references) == label

    return {
        label for label, count in counts.items()
        if count == 1
        and any(label in labels for labels in cited.values())
        and all(means(label, output_id) for output_id, labels in cited.items() if label in labels)
    }


_CARRIED_ID_FIELDS = frozenset({
    *(field for field in REFERENCE_ID_FIELDS if field != "reference_id"),
    "basis_id", "mission_run_id", "invocation_id", "verified_sources",
})
"""The id fields of the records a revision call carries besides the
references: a diagnostic's basis and run ids, the documents a reading
record names, a search's ids, the sources a reading verified. A reference's
own id is read by :func:`~inqtrix.agents.report_quality.writer_identifiers`,
which tells a label-shaped one from an id. A plan's task id is no such id:
the planner writes it, and it may be a plain word a sentence uses."""


def _carried_identifiers(value: Any) -> set[str]:
    """Every string under a :data:`_CARRIED_ID_FIELDS` key anywhere in *value*.

    Records are walked whole: a reading record names the documents it read
    several levels down.
    """
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _CARRIED_ID_FIELDS:
                found.update(
                    entry for entry in (item if isinstance(item, (list, tuple)) else [item])
                    if isinstance(entry, str)
                )
            found |= _carried_identifiers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _carried_identifiers(item)
    return found


def revision_identifiers(subject: ReviewSubject, evidence: Mapping[str, Any]) -> list[str]:
    """Every identifier one revision call carried, sorted: no edit may copy one.

    The call carries every output's references, the source records of the
    bases it names, the run diagnostics with every id in them (a reading
    record names the documents it read), the searches and the runs that
    could not be read, the run under work and the canvases. A canvas edit
    guarded by that canvas's own references would let it copy another
    output's document id, the run id or a diagnostic's basis id into a
    reader's text.
    """
    references = [
        *(ref for refs in subject.allowed_references.values() for ref in refs),
        *(
            item["reference"] for item in evidence.get("sources") or []
            if isinstance(item.get("reference"), dict)
        ),
    ]
    values = set(writer_identifiers(references))
    values |= _carried_identifiers([
        evidence.get("sources") or [],
        evidence.get("run_diagnostics") or [],
        evidence.get("unavailable_origins") or [],
        list((evidence.get("searches") or {}).values()),
    ])
    values.update(evidence.get("searches") or {})
    values.update(basis for bases in subject.diagnostic_ids.values() for basis in bases)
    values.add(subject.evidence_run_id)
    values.update(output.artifact_id for output in subject.canvases)
    return sorted(value.strip() for value in values if value.strip())


def _origin_run_ids(subject: ReviewSubject) -> list[str]:
    """The runs whose records this review reads, other than the run itself."""
    evidence = subject.evidence
    candidates = [
        *(str(item.get(key) or "")
          for item in evidence.get("run_diagnostics") or []
          for key in ("source_run_id", "mission_run_id")),
        *(str(item.get("source_run_id") or "")
          for item in evidence.get("unavailable_origins") or []),
        *(str((row or {}).get("source_run_id") or "")
          for row in (evidence.get("searches") or {}).values()),
    ]
    return list(dict.fromkeys(
        run_id for run_id in candidates
        if run_id and run_id != subject.evidence_run_id
    ))


def readable(text: str, names: Mapping[str, str], language: str) -> str:
    """*text* with every identifier this review knows replaced by its name.

    One pass over one alternation, longest key first, so ``source:ref_1``
    wins over the bare ``ref_1`` inside it. Enclosing backticks go with the
    identifier: a name left in code ticks reads as an id again. Whatever
    still reads as a basis id afterwards is named unassignable — this is
    the review's one reading boundary, so the sweep belongs here and not at
    each call site, and *language* is required for exactly that reason.
    """
    if not text:
        return text
    if names:
        alternation = "|".join(
            re.escape(key) for key in sorted(names, key=len, reverse=True)
        )
        pattern = re.compile(
            rf"{_NAME_LEAD}(?:`(?P<ticked>{alternation})`"
            rf"|(?P<bare>{alternation}){_NAME_BOUNDARY})"
        )
        text = pattern.sub(
            lambda match: names[match.group("ticked") or match.group("bare")], text
        )
    return _without_stray_ids(text, language)


@dataclass(frozen=True)
class OpenLine:
    """One point a review leaves open, in the reader's language."""

    kind: str
    """``not_checked``, ``finding``, ``marked`` (a passage that stays as
    written with its mark, :data:`~inqtrix.agents.kernel.review_edits.PROVIDER_CONTEXT_ONLY`),
    ``information`` or ``output_requirement``."""
    text: str
    reason: str
    """The code of why it stayed open (``basis_unconfirmed``, a naming reason
    of :data:`~inqtrix.agents.kernel.review_edits.FINDING_REASONS`), the state
    ``criterion_revised`` of an unmet criterion its findings worked on,
    ``criterion_verified`` when the re-check held every one of those edits,
    or ``""``."""


def criterion_states(
    assessment: Assessment, results: Sequence[FindingResult]
) -> dict[int, tuple[str, int]]:
    """Per unmet criterion (by its ``index``): its state and how many counted
    model results concern it.

    An ``output_requirement`` criterion is ``revised`` when at least one such
    result concerns it and every one of them was applied: its gap in the
    reviewed draft was worked on, which never shows it met. It is
    ``verified`` when, beyond that, the re-check held every one of those
    edits: they fix what their findings name, every new text they state is
    carried by its sources, and a passage they drop is restated or asked
    for in their finding's own words (an edit that only removes rests on
    those words alone). Every other unmet criterion stays ``open``, an
    ``information`` criterion always, since an applied edit reaches no
    material the review had not read.
    """
    counted = [
        result for result in counted_results(results) if result.origin == "model"
    ]
    states: dict[int, tuple[str, int]] = {}
    for item in assessment.criteria_unmet:
        linked = [result for result in counted if result.criterion == item["index"]]
        revised = (
            item["kind"] == "output_requirement"
            and bool(linked)
            and all(result.state == "applied" for result in linked)
        )
        state = (
            "open" if not revised
            else "verified" if all(result.recheck == "held" for result in linked)
            else "revised"
        )
        states[item["index"]] = (state, len(linked))
    return states


def open_point_lines(
    assessment: Assessment,
    language: str,
    results: Sequence[FindingResult] = (),
) -> list[OpenLine]:
    """Everything the verdict itself leaves open, in the reader's language.

    What a review could NOT confirm is part of its outcome: a point without
    a confirmed basis and a criterion nobody settled are open, never passed.
    These lines reach the desk and the run's outcome record, so every
    identifier in them is replaced by the name of what it stands for. The
    kind says what a line is: ``not_checked`` (a point the review could not
    check, or a criterion it did not settle), ``finding`` (a finding without
    a confirmed basis, reason ``basis_unconfirmed``) or the unmet criterion's
    own kind (``information``, ``output_requirement``). An unmet output
    requirement that at least one counted model finding in *results*
    concerns, every such finding applied (:func:`criterion_states`), says
    so, and whether the re-check held those edits, and does not quote the
    gap of the reviewed draft, which the delivered text may not hold. The
    findings the outputs did not take over are :func:`finding_lines`.
    """
    words = {"language": language}
    states = criterion_states(assessment, results)
    readers = assessment.criterion_readers

    def criterion_line(item: Mapping[str, Any]) -> tuple[str, str, str]:
        state, linked = states[item["index"]]
        criterion = readers.get(item["index"], item["text"])
        if state in ("revised", "verified"):
            # Without sources nothing re-checks an edit: the line says where
            # the edit came from, not that it went unchecked.
            key = "knowledge" if assessment.knowledge and state == "revised" else state
            return (
                str(item["kind"]),
                t_count(words, f"review_criterion_{key}", linked, criterion=criterion),
                f"criterion_{state}",
            )
        return (
            str(item["kind"]),
            t(words, "review_criterion_open", criterion=criterion, gap=item["gap"])
            if item["gap"]
            else t(words, "review_criterion_open_plain", criterion=criterion),
            "",
        )

    def unsettled_reader(position: int, text: str) -> str:
        numbers = assessment.unsettled_indices
        return readers.get(numbers[position], text) if position < len(numbers) else text

    return [
        OpenLine(kind, readable(point, assessment.names, language), reason)
        for kind, point, reason in (
            *(("not_checked", item, "") for item in assessment.not_checked),
            *(
                (
                    "finding",
                    t(words, "review_basis_unconfirmed", finding=item.finding.strip()),
                    "basis_unconfirmed",
                )
                for item in assessment.unconfirmed
            ),
            *(criterion_line(item) for item in assessment.criteria_unmet),
            *(
                (
                    "not_checked",
                    t(words, "review_criterion_unsettled", criterion=unsettled_reader(position, item)),
                    "",
                )
                for position, item in enumerate(assessment.criteria_unsettled)
            ),
        )
    ]


def open_points(assessment: Assessment, language: str) -> list[str]:
    """The texts of :func:`open_point_lines`, in the same order."""
    return [line.text for line in open_point_lines(assessment, language)]


def finding_lines(
    results: Sequence[FindingResult],
    names: Mapping[str, str],
    language: str,
    *,
    draft_round: int | None = None,
    unverified: Collection[str] | None = None,
) -> list[OpenLine]:
    """One line per named finding, with its reason in reader words.

    A model finding reads as its own prose (``finding``); a server finding
    names its unverified quote (``not_checked``). A finding that marked its
    passage asked for no revision, so its line (``marked``) says what the
    mark says instead of a reason it was not applied. A server finding
    anchored by its paragraph holds every quote of that paragraph, so
    *unverified* (the quotes the server measured) keeps the line to the ones
    that failed. On the research route (*draft_round*) the draft is
    rewritten next, so a server line names the round instead of quoting a
    passage the next text may no longer hold.
    """
    words = {"language": language}
    lines: list[OpenLine] = []
    for result in results:
        if result.state != "named":
            continue
        if result.origin != "server":
            finding = readable(result.finding.strip(), names, language)
        elif draft_round is not None:
            finding = t(words, "review_quote_unverified_draft", round=draft_round)
        else:
            inner = [
                text for text, _passage in quoted_passages(result.quote)
                if unverified is None or text in unverified
            ]
            finding = t(words, "review_quote_unverified", quote=" … ".join(inner) or result.quote)
        if result.reason == PROVIDER_CONTEXT_ONLY:
            lines.append(OpenLine(
                "marked", t(words, "review_finding_marked", finding=finding), result.reason,
            ))
            continue
        lines.append(OpenLine(
            "not_checked" if result.origin == "server" else "finding",
            t(
                words,
                "review_finding_named",
                reason=t(words, f"review_finding_reason_{result.reason}"),
                finding=finding,
            ),
            result.reason,
        ))
    return lines


def counted_results(results: Sequence[FindingResult]) -> list[FindingResult]:
    """The results a revision decided (:func:`~inqtrix.agents.kernel.review_edits.is_counted`)."""
    return [result for result in results if is_counted(result.origin, result.reason)]


def revision_state(results: Sequence[FindingResult]) -> str:
    """What the revision decided (:func:`counted_results`): ``none``,
    ``verified`` (all applied, and the re-check held every one),
    ``unverified`` (all applied, at least one not held: rejected, unchecked
    or not confirmed, a failed re-check or a server finding, which the
    model re-check never judges), ``discarded`` (none applied) or
    ``partially_discarded``."""
    counted = counted_results(results)
    applied = sum(1 for result in counted if result.state == "applied")
    if not counted:
        return "none"
    if applied == len(counted):
        return (
            "verified" if all(result.recheck == "held" for result in counted)
            else "unverified"
        )
    return "partially_discarded" if applied else "discarded"


def unapplied_activity(
    results: Sequence[FindingResult],
    names: Mapping[str, str],
    language: str,
    *,
    unverified: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """The findings a revision left standing, as one activity for both engines.

    ``None`` unless :func:`revision_state` reads ``discarded`` or
    ``partially_discarded``; the label says which, the detail lists every
    named finding with its reason in reader words, in verdict order.
    """
    word = revision_state(results)
    if word not in ("discarded", "partially_discarded"):
        return None
    counted = counted_results(results)
    named = [result for result in verdict_order(counted) if result.state == "named"]
    log.warning("Review revision: %d of %d findings not applied.", len(named), len(counted))
    words = {"language": language}
    return {
        "kind": "critic_revision_discarded",
        "label": t(
            words,
            "agent_critic_revision_discarded_label"
            if word == "discarded"
            else "agent_critic_revision_partially_discarded_label",
        ),
        "detail": "; ".join(
            line.text
            for line in finding_lines(named, names, language, unverified=unverified)
        ),
    }


def located_results(assessment: Assessment, reason: str) -> list[FindingResult]:
    """Every located finding named with *reason*: it never reached an edit."""
    return [named_result(item, reason) for item in assessment.located]


def verdict_order(results: Sequence[FindingResult]) -> list[FindingResult]:
    """*results* in the verdict's order (``finding_1`` first), however they
    were collected: a locate-named finding and a revised one interleave."""
    return sorted(results, key=lambda result: int(result.finding_id.removeprefix("finding_")))


def report_facts(
    assessment: Assessment,
    results: Sequence[FindingResult],
    *,
    revision: str,
    language: str,
    draft_round: int | None = None,
    unverified: Collection[str] | None = None,
    outputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The review's facts in the one shape both engines report.

    ``revision`` (:func:`revision_state`), ``accounting``,
    ``criteria_uncovered`` (each with its ``state`` from
    :func:`criterion_states`: ``open``, ``revised`` or ``verified``), ``open_lines``
    (``kind``, ``text``, ``reason``), ``findings`` (one record per result,
    in verdict order), ``reach`` (how many cited references the review
    re-read, of how many cited) and ``basis`` (``knowledge`` for a review in
    knowledge mode, ``evidence`` otherwise); with *outputs*, the texts the
    run delivers, also ``claims`` and ``marks`` (:func:`claim_facts`);
    :func:`inqtrix.agents.run_outcome.build_outcome` reads them.
    *draft_round* and *unverified* are :func:`finding_lines`'.
    """
    ordered = verdict_order(results)
    states = criterion_states(assessment, ordered)
    return {
        **(claim_facts(assessment, ordered, outputs, language) if outputs is not None else {}),
        "revision": revision,
        "accounting": "incomplete" if assessment.criteria_unsettled else "complete",
        "criteria_uncovered": [
            {
                "index": item["index"], "text": item["text"], "kind": item["kind"],
                "gap": item["gap"], "state": states[item["index"]][0],
            }
            for item in assessment.criteria_unmet
        ],
        "open_lines": [
            asdict(line)
            for line in (
                *open_point_lines(assessment, language, ordered),
                *finding_lines(
                    ordered, assessment.names, language,
                    draft_round=draft_round, unverified=unverified,
                ),
            )
        ],
        "findings": [result.record() for result in ordered],
        "reach": {
            "reread": len(assessment.reread_ids),
            "cited": len(assessment.cited_ids),
            "provider_only": len(assessment.provider_only_ids),
        },
        "basis": "knowledge" if assessment.knowledge else "evidence",
    }


_NOT_ON_A_PASSAGE = frozenset({
    "finding_invalid", "basis_invalid", "basis_outside_output", "remove_labels_invalid",
})
"""Naming reasons of a finding that is invalid itself: it marks no passage."""


def claim_facts(
    assessment: Assessment,
    results: Sequence[FindingResult],
    outputs: Mapping[str, str],
    language: str,
) -> dict[str, Any]:
    """The state of every statement the run delivers
    (:mod:`~inqtrix.agents.kernel.claim_states`), per output in *outputs*.

    ``claims`` counts an output's statements by state (``total`` and one
    count per state); ``marks`` holds one entry per insufficiently supported
    statement, in text order: its ``output_id``, its passage as the output
    holds it (``quote``), its ``state``, its ``reason`` and a ``note`` in
    reader words. A confirmed ``evidence_error`` the revision did not apply
    marks its passage; an applied edit the re-check did not hold keeps the
    statements it wrote from reading verified. A review from general
    knowledge has no source to tell a state from and reports neither.
    """
    if assessment.knowledge:
        return {}
    words = {"language": language}
    sources = {
        str(source.get("basis_id") or ""): source
        for source in assessment.evidence.get("sources") or []
    }
    claims: dict[str, dict[str, int]] = {}
    marks: list[dict[str, Any]] = []
    for output_id, text in outputs.items():
        own = [result for result in results if result.output_id == output_id]
        materials: dict[str, Material] = {}
        for label, basis in assessment.label_basis.get(output_id, {}).items():
            if (source := sources.get(basis)) is None:
                continue
            kind = str(source.get("material_kind") or "")
            materials[label] = Material(
                kind,
                str(source.get("source_excerpt") or "") if kind == "retained_source_excerpt" else "",
            )
        unheld = any(result.state == "applied" and result.recheck != "held" for result in own)
        states = statement_states(
            text,
            materials=materials,
            standing=[
                Standing(result.quote, readable(result.finding.strip(), assessment.names, language))
                for result in own
                if result.state == "named"
                and result.kind == "evidence_error"
                and result.reason not in _NOT_ON_A_PASSAGE
            ],
            revised_from=assessment.reviewed.get(output_id) if unheld else None,
        )
        if not states:
            continue
        unmapped = {
            label for state in states for label in state.statement.labels if label not in materials
        }
        if unmapped:
            log.warning(
                "Statement states: %d cited labels of one output map to no source in the "
                "review's evidence; a statement citing only such labels reads as "
                "insufficiently supported.",
                len(unmapped),
            )
        counted = Counter(state.state for state in states)
        claims[output_id] = {"total": len(states), **{state: counted[state] for state in STATES}}
        marks.extend(
            {
                "output_id": output_id,
                "quote": state.statement.text,
                "state": state.state,
                "reason": state.reason,
                "note": t(words, f"claim_mark_{state.reason}", finding=state.finding),
            }
            for state in states
            if state.state == "insufficient"
        )
    return {"claims": claims, "marks": marks}


def waived_activity(
    level: str, *, tier: str, language: str
) -> dict[str, Any]:
    """This run verifies nothing, and says so — the contract, not a fallback.

    Without it "not verified" and "verification never ran" share the same
    shape on the wire: an absence. A reader cannot tell the two apart, and
    neither can the surface, so the run states its own review level.
    """
    return {
        "kind": "deep_review_waived",
        "scope": "run",
        "phase": "execution",
        "verify": level,
        "label": t(
            {"language": language},
            "review_waived_label" if tier else "review_waived_label_legacy",
            tier=tier,
        ),
        "detail": "",
    }


def unavailable_activity(review: Review, language: str) -> dict[str, Any]:
    """The run owed a review and did not get one — visibly, with its reason.

    The counterpart of :func:`waived_activity`: same absence on the
    surface, opposite meaning. The caller warns; this makes the run itself
    carry the difference. What the reader may be told comes from
    :func:`reader_detail`.
    """
    return {
        "kind": "deep_review_unavailable",
        "scope": "run",
        "phase": "execution",
        "transport": review.transport,
        "reason": review.reason,
        "label": t({"language": language}, "review_unavailable_label"),
        "detail": reader_detail(review, language),
    }


def reader_detail(review: Review, language: str) -> str:
    """What a reader may be told about a review without a verdict.

    No reason carries a reader detail of its own. Whatever a review leaves
    behind instead of a verdict is runtime text in the runtime's language —
    model debris, half a schema, an exception class, the limit a budget
    hit. Each reason therefore has ONE sentence, and the raw text goes to
    the log where it can be diagnosed. Both engines read this, so neither
    can show the debris the other hides.

    A reason without a sentence is a defect, not a case to fall through:
    the fall-through was how the runtime's English reached a German reader
    in the first place, and it would do so again for every reason added
    later. An unknown reason gets the sentence that is true of all of them
    and says so in the log.
    """
    words = {"language": language}
    key = f"review_unavailable_{review.reason}"
    if key not in MESSAGES:
        log.warning(
            "A review failed with the reason %r, for which no reader "
            "sentence exists; the reader gets the generic one. Raw detail: %s",
            review.reason,
            _clip(review.detail, 2_000),
        )
        return t(words, "review_unavailable_generic")
    if review.detail:
        log.warning(
            "Review unavailable (%s); raw detail: %s",
            review.reason,
            _clip(review.detail, 2_000),
        )
    return t(words, key)


def marked_results(results: Sequence[FindingResult]) -> list[FindingResult]:
    """The findings that marked their passage instead of asking for a
    revision (:data:`~inqtrix.agents.kernel.review_edits.PROVIDER_CONTEXT_ONLY`):
    the review's substance as much as a located finding, though no revision
    is asked for them."""
    return [result for result in results if result.reason == PROVIDER_CONTEXT_ONLY]


def findings_activity(
    assessment: Assessment, language: str, *, titles: Mapping[str, str]
) -> dict[str, Any]:
    """The located findings and the passages a finding marked, as one activity.

    They are the review's substance; a narration carries only their count.
    Emitted BEFORE the revision call, they stay visible when the revision
    fails or changes nothing. A finding that never anchored is not listed:
    its quote is model text, not the output's own. A marked passage
    (:data:`~inqtrix.agents.kernel.review_edits.PROVIDER_CONTEXT_ONLY`)
    stands among them in verdict order, flagged ``marked``: it asked for no
    revision and stays as written.
    """
    names = assessment.names
    built = [
        *(
            {
                "finding_id": str(item.get("finding_id") or ""), "output_id": str(item["output_id"]),
                "finding": str(item["finding"]), "quote": str(item["quote"]), "marked": False,
            }
            for item in assessment.located
        ),
        *(
            {
                "finding_id": result.finding_id, "output_id": result.output_id,
                "finding": result.finding, "quote": result.quote, "marked": True,
            }
            for result in marked_results(assessment.results)
        ),
    ]

    def order(position: int, entry: Mapping[str, Any]) -> int:
        # Verdict order where the ids tell it (finding_1 first).
        number = re.fullmatch(r"finding_(\d+)", entry["finding_id"])
        return int(number.group(1)) if number else position

    entries = [entry for _, entry in sorted(
        enumerate(built), key=lambda pair: order(pair[0], pair[1])
    )]

    def canvas(artifact_id: str) -> str:
        # Never the artifact id: a canvas without a title is a canvas
        # without a title, not a hexadecimal handle. The noun stands once —
        # "Canvas Canvas ohne Titel" is the stutter of a composed prefix.
        title = str(titles.get(artifact_id) or "").strip()
        return t(
            {"language": language},
            "review_findings_canvas" if title else "review_findings_canvas_untitled",
            title=title,
        )

    return {
        "kind": "deep_review_findings",
        "scope": "run",
        "phase": "execution",
        "label": t_count({"language": language}, "review_findings_label", len(entries)),
        # A target is named only where there is a choice: beside a canvas
        # the chat answer reads "In der Antwort"; a run whose only output is
        # the answer needs no target at all ("Chat:" named a mode).
        "detail": "; ".join(
            (
                canvas(str(item["output_id"]))
                if item["output_id"] != "chat"
                else t({"language": language}, "review_findings_chat")
                if titles
                else ""
            )
            + readable(str(item["finding"]).strip(), names, language)
            for item in entries
        ),
        # Reader-facing projection: the flagged passage and what is wrong
        # with it. The offsets and internal finding/basis ids that carry
        # the edit set stay inside the pass — a reader never sees them,
        # and the payload must not tempt a surface into showing them. The
        # quote is the output's own text and stays verbatim; without that
        # the surface could no longer find the passage it marks.
        "findings": [
            {
                "quote": str(item["quote"]),
                "finding": readable(
                    str(item["finding"]).strip(), names, language
                ),
                "target": str(item["output_id"]),
                **({"marked": True} if item["marked"] else {}),
            }
            for item in entries
        ],
    }


def _settle_criteria(
    verdict: DeepReviewVerdict, criteria: tuple[str, ...]
) -> tuple[list[str], list[dict[str, Any]], list[str], list[int]]:
    """Met, unmet-with-owner and unsettled criteria, in the handover's order,
    plus the number of each unsettled one.

    A criterion counts only when the verdict accounts for it exactly once.
    Left out, counted twice or unmet without its kind, it is unsettled: open,
    never met, and never a reason to research on its own.
    """
    counts = Counter(item.index for item in verdict.criteria)
    by_index = {item.index: item for item in verdict.criteria}
    met: list[str] = []
    unmet: list[dict[str, Any]] = []
    unsettled: list[str] = []
    unsettled_indices: list[int] = []
    for index, text in enumerate(criteria, start=1):
        entry = by_index.get(index) if counts[index] == 1 else None
        if entry is None or (not entry.met and not entry.kind):
            unsettled.append(text)
            unsettled_indices.append(index)
        elif entry.met:
            met.append(text)
        else:
            unmet.append(
                {"index": index, "text": text, "kind": entry.kind, "gap": entry.gap.strip()}
            )
    return met, unmet, unsettled, unsettled_indices


def revise_outputs(
    host: ReviewHost,
    subject: ReviewSubject,
    assessment: Assessment,
    *,
    output_bundle: str,
) -> Revision:
    """One revision call over the located findings, then the re-check of
    its applied edits (:func:`_recheck`).

    The call receives the evidence of exactly the bases they name; each
    finding's edit is applied or named with its reason
    (:func:`~inqtrix.agents.kernel.review_edits.apply_review_edits`). Each
    applied model edit is then checked, place by place, against the texts
    of the labels it cites, and the server settles it from that check: it
    holds only when a second, identical check holds it too, and no
    settlement takes an applied edit back, so the outputs are the ones the
    revision composed. In knowledge mode (:attr:`Assessment.knowledge`) the
    revision cites nothing and no source exists to check an edit against:
    every applied model edit carries ``knowledge_only`` instead of a
    re-check. The returned usage is that of every call.
    """
    from inqtrix.agents.patterns._structured import structured_call
    from inqtrix.agents.prompts import build_deep_revision_prompt

    call = host.revision_call
    located = assessment.located
    narrowed = narrow_review_evidence(
        assessment.evidence,
        (basis for item in located for basis in item["basis_ids"]),
    )
    prompt = build_deep_revision_prompt(
        subject.assignment,
        output_bundle,
        located,
        evidence_context=_block_json(_in_target_labels(narrowed, located, subject)),
        clarifications=subject.clarifications,
        language=subject.language or None,
        knowledge=assessment.knowledge,
    )
    if not review_context_fits(
        host.llm, model=call.model, prompt=prompt,
        schema=json.dumps(DeepRevisionBundle.model_json_schema()),
        fallback_window=_fallback_window(host, call.model),
    ):
        log.warning("Review revision context does not fit in full; outputs are retained.")
        return Revision(status="context", results=located_results(assessment, "revision_context"))
    try:
        outcome = structured_call(
            host.llm,
            prompt=prompt,
            model_cls=DeepRevisionBundle,
            node=call.node,
            model=call.model,
            reasoning_effort=call.reasoning_effort,
            timeout=host.timeout,
        )
    except AgentCancelled:
        # A cancel is the user's decision about the run, not a failed call.
        raise
    except Exception as exc:  # noqa: BLE001 — keep the finished outputs
        log.warning(
            "Review revision failed (error_type=%s); the original outputs are retained.",
            type(exc).__name__,
        )
        return Revision(status="failed", results=located_results(assessment, "revision_failed"))
    usage = _usage(outcome.usage)
    bundle = outcome.value
    if not isinstance(bundle, DeepRevisionBundle):
        log.warning("Review revision returned no content; the original outputs are retained.")
        return Revision(
            status="empty", usage=usage,
            results=located_results(assessment, "revision_empty"),
        )
    identifiers = revision_identifiers(subject, narrowed)
    revised, results = apply_review_edits(
        subject.outputs,
        located,
        bundle,
        allowed_references=subject.allowed_references,
        identifiers=identifiers,
        user_text=subject.user_text,
    )
    if assessment.knowledge:
        results = [
            replace(result, recheck_reason="knowledge_only")
            if result.state == "applied" and result.origin == "model" else result
            for result in results
        ]
        check_usage: dict[str, int] = {}
    else:
        results, check_usage = _recheck(host, subject, assessment, bundle, revised, results)
    usage = {key: value + check_usage.get(key, 0) for key, value in usage.items()}
    if not any(result.state == "applied" for result in results):
        log.warning(
            "Review revision applied none of its %d findings; the original outputs "
            "are retained.", len(results),
        )
        return Revision(status="invalid", usage=usage, results=results)
    return Revision(status="revised", outputs=revised, usage=usage, results=results)


def _block_json(value: Any) -> str:
    """*value* as a basis block is shown to a model, umlauts as they are; a
    quote of a reading record is checked against exactly this form."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _in_target_labels(
    evidence: Mapping[str, Any],
    located: Sequence[Mapping[str, Any]],
    subject: ReviewSubject,
    *,
    citable: Callable[[Mapping[str, Any]], Sequence[str]] = lambda item: item["may_add"],
) -> dict[str, Any]:
    """A basis block in the labels of the outputs its findings concern.

    A source's ledger label can name another source in a canvas that
    numbers its own; a reader of the block would cite it there. Each
    source therefore loses its ledger label and names, under ``cite_as``,
    the label it carries in each output where a finding may cite it:
    ``citable`` gives a finding's labels, by default the basis labels it
    may add (the revision); the re-check also passes the labels the
    passage keeps. A label is unambiguous in its output
    (:func:`~inqtrix.agents.kernel.review_edits.basis_label`), so it
    names exactly one source there.
    """
    sources = []
    for source in evidence.get("sources") or []:
        basis = str(source.get("basis_id") or "")
        cite_as = {
            str(item["output_id"]): label
            for item in located
            if (label := basis_label(
                basis, subject.allowed_references.get(str(item["output_id"]), [])
            )) in citable(item)
        }
        reference = {
            key: value for key, value in dict(source.get("reference") or {}).items()
            if key != "label"
        }
        sources.append({**source, "reference": reference, "cite_as": cite_as})
    return {**evidence, "sources": sources}


def _recheck(
    host: ReviewHost,
    subject: ReviewSubject,
    assessment: Assessment,
    bundle: DeepRevisionBundle,
    revised: Mapping[str, str],
    results: list[FindingResult],
) -> tuple[list[FindingResult], dict[str, int]]:
    """Check each applied model edit and mark the ones two checks hold.

    The server lists where each applied edit of a model finding changed its
    passage (:func:`~inqtrix.agents.kernel.review_edits.changed_segments`,
    read as the output holds the replacement,
    :func:`~inqtrix.agents.kernel.review_edits.spliced_replacement`) and
    where the replacement stands in the delivered text
    (:func:`~inqtrix.agents.kernel.review_edits.placement`). One structured
    call on the review node fills one row per segment: quotes from the basis
    texts of the labels the replacement cites (or from the reading record a
    ``run_diagnostic`` finding rests on), fixed dimensions, the kind the
    replacement and the basis give the content, and what became of the
    replaced text. The server decides each edit from its rows
    (:func:`settle_check`): it holds, it is rejected with its code, or it
    stays unchecked. A server finding never reaches the call; only the
    server measures it. The check judges the replacement, never the
    finding it answers.

    The check can only confirm. One answer of a model can flip on the same
    request, so when the first call holds an edit, a second call sends the
    same request once more, byte for byte, and an edit is marked ``held``
    only when that confirmation holds it too; when the first call holds no
    edit there is no second one. Every other edit stays as the revision
    applied it, unverified, a rejected one included: its code reaches the
    log alone, for a rejection by the first call in a WARNING that names
    the edit, for an edit the confirmation does not hold in the count
    below, which names no edit. Nothing is taken back, so the outputs stay
    the ones the revision composed. Every unverified edit but a rejected
    one carries the reason its code reads as (``recheck_reason``,
    :data:`~inqtrix.agents.kernel.review_edits.RECHECK_REASONS`), which
    the run's outcome names to the reader.

    An edit without a segment (it changes only punctuation, emphasis, a
    list mark's style or number, or a heading's level) is settled before
    the call and stays unchecked; so does an edit of more than
    :data:`RECHECK_SEGMENTS_MAX` segments, and every edit after the
    first that would take the call beyond :data:`RECHECK_CALL_SEGMENTS_MAX`
    segments, each with a WARNING that names the numbers. With nothing left
    to send there is no call.

    A check that cannot run (no review call, a context that does not fit,
    a failed first call or one without a usable verdict) leaves every
    applied edit unverified, with a WARNING. A cancel stops the run during
    either call, as it does during the revision call. Otherwise every edit
    settled without a hold or a rejection is counted per code in one
    WARNING, those without a segment and those over a bound included, and
    an edit the first call held and the confirmation did not as
    ``unconfirmed``, with the code the confirmation settled it with after
    a colon; a confirmation that fails or answers nothing usable has a
    WARNING of its own, and every edit the first call held is counted
    without a code. The model's reason is never logged.

    Returns one result per located finding in *results*' order and the
    tokens the calls spent.
    """
    from inqtrix.agents.patterns._structured import structured_call
    from inqtrix.agents.prompts import (
        build_revision_check_prompt,
        build_revision_check_system_prompt,
    )

    located = assessment.located
    by_id = {result.finding_id: result for result in results}
    applied = [item for item in located if by_id[str(item["finding_id"])].state == "applied"]
    checked = [item for item in applied if by_id[str(item["finding_id"])].origin == "model"]
    if not checked:
        return results, {}
    call = host.review_call
    if call is None:
        log.warning(
            "Revision re-check has no review call; the applied edits stand unverified."
        )
        return _settled(results, dict.fromkeys(
            (str(item["finding_id"]) for item in checked), "check_unavailable",
        )), {}
    replacements = {
        edit.finding_id: spliced_replacement(edit.replacement)
        for edit in bundle.edits
        if edit.finding_id in by_id
    }
    # The code each edit the re-check leaves unchecked is counted under.
    unchecked: dict[str, str] = {}
    segments: dict[str, list[ChangedSegment]] = {}
    oversized: list[str] = []
    textless: list[str] = []
    for item in checked:
        finding_id = str(item["finding_id"])
        found = changed_segments(
            str(item["quote"]),
            replacements[finding_id],
            **line_edges(
                subject.outputs[str(item["output_id"])], int(item["start"]), int(item["end"])
            ),
        )
        if not found:
            unchecked[finding_id] = "no_segment"
        elif len(found) > RECHECK_SEGMENTS_MAX:
            unchecked[finding_id] = "too_many_segments"
            oversized.append(f"{finding_id} ({len(found)} segments)")
        elif _no_quotable_basis(
            item, found, replacements[finding_id], subject, assessment.evidence
        ):
            unchecked[finding_id] = "no_source_text"
            textless.append(finding_id)
        else:
            segments[finding_id] = found
    if oversized:
        log.warning(
            "Revision re-check checks no edit of more than %d segments; unverified: %s.",
            RECHECK_SEGMENTS_MAX, ", ".join(oversized),
        )
    if textless:
        log.warning(
            "Revision re-check finds no stored source text to quote; unverified: %s.",
            ", ".join(textless),
        )
    sent: list[Mapping[str, Any]] = []
    carried = 0
    for item in checked:
        finding_id = str(item["finding_id"])
        if finding_id not in segments:
            continue
        if carried + len(segments[finding_id]) > RECHECK_CALL_SEGMENTS_MAX:
            break
        carried += len(segments[finding_id])
        sent.append(item)
    sent_ids = [str(item["finding_id"]) for item in sent]
    left = [finding_id for finding_id in segments if finding_id not in sent_ids]
    if left:
        unchecked.update(dict.fromkeys(left, "over_call_limit"))
        log.warning(
            "Revision re-check carries at most %d segments per call; unverified: %s.",
            RECHECK_CALL_SEGMENTS_MAX,
            ", ".join(f"{finding_id} ({len(segments[finding_id])} segments)" for finding_id in left),
        )
    if not sent:
        _warn_unchecked(unchecked, len(checked))
        return _settled(results, unchecked), {}
    placements = _placements(applied, sent, revised, replacements, originals=subject.outputs)
    basis_ids: list[str] = []
    for item in sent:
        references = subject.allowed_references.get(str(item["output_id"]), [])
        cited = cited_labels(replacements[str(item["finding_id"])], DELEGATED_FAMILIES)
        for reference in references:
            # Only a source with an unambiguous label in the output
            # (basis_label) has a basis text a quote under it can be checked
            # against; one two labels name, or a label two sources carry, has none.
            basis = f"{SOURCE_BASIS_PREFIX}{KernelDeps.ledger_id(dict(reference))}"
            if basis not in basis_ids and basis_label(basis, references) in cited:
                basis_ids.append(basis)
        if item["kind"] == "run_diagnostic":
            basis_ids.extend(
                str(basis) for basis in item["basis_ids"]
                if str(basis).startswith(DIAGNOSTIC_BASIS_PREFIX)
            )
    block = _in_target_labels(
        narrow_review_evidence(assessment.evidence, basis_ids),
        sent,
        subject,
        citable=lambda item: [*item["keep"], *item["may_add"]],
    )
    system = build_revision_check_system_prompt()
    prompt = build_revision_check_prompt(
        sent,
        replacements={str(item["finding_id"]): replacements[str(item["finding_id"])] for item in sent},
        segments=segments,
        placements=placements,
        evidence_context=_block_json(block),
    )
    if not review_context_fits(
        host.llm, model=call.model, prompt=prompt,
        schema=system + json.dumps(RevisionCheckVerdict.model_json_schema()),
        fallback_window=_fallback_window(host, call.model),
    ):
        log.warning(
            "Revision re-check context does not fit; the applied edits stand unverified."
        )
        return _settled(results, {**unchecked, **dict.fromkeys(sent_ids, "check_unavailable")}), {}

    def check() -> Any:
        # The one request, built once: the confirmation sends it again
        # byte for byte.
        return structured_call(
            host.llm,
            prompt=prompt,
            system=system,
            model_cls=RevisionCheckVerdict,
            node=call.node,
            model=call.model,
            reasoning_effort=call.reasoning_effort,
            timeout=host.timeout,
        )

    def settle(
        verdict: RevisionCheckVerdict, wanted: Collection[str]
    ) -> dict[str, tuple[str, str]]:
        # Each edit of *wanted* settled from *verdict*, in the order sent.
        unlisted = sum(1 for found in verdict.findings if found.finding_id not in sent_ids)
        if unlisted:
            log.warning(
                "Revision re-check ignored %d check(s) of a finding id it did not list.",
                unlisted,
            )
        return {
            finding_id: settle_check(
                [found for found in verdict.findings if found.finding_id == finding_id],
                segments[finding_id],
                finding=str(item["finding"]),
                cited=cited_labels(replacements[finding_id], DELEGATED_FAMILIES),
                basis_texts=_basis_texts(block, str(item["output_id"])),
                records=[
                    record for record in block.get("run_diagnostics") or []
                    if record.get("basis_id") in item["basis_ids"]
                ],
            )
            for item, finding_id in zip(sent, sent_ids)
            if finding_id in wanted
        }

    verdict, usage = _check_verdict(check, "re-check", "the applied edits")
    if verdict is None:
        return _settled(
            results, {**unchecked, **dict.fromkeys(sent_ids, "check_unavailable")},
        ), usage
    held: set[str] = set()
    for finding_id, (state, code) in settle(verdict, sent_ids).items():
        if state == "holds":
            held.add(finding_id)
        elif state == "rejected":
            # The code is the server's reading, not the reader's news: it
            # stands in the log alone, and the edit stays as applied.
            log.warning(
                "Revision re-check rejected edit %s (%s); it stands unverified.",
                finding_id, code,
            )
        else:
            unchecked[finding_id] = code
    if held:
        confirmation, spent = _check_verdict(
            check, "re-check confirmation", "the edits the first check held",
        )
        usage = {key: value + spent.get(key, 0) for key, value in usage.items()}
        # Only the edits the first check held are settled again.
        settled_again = settle(confirmation, held) if confirmation is not None else {}
        for finding_id in [finding_id for finding_id in sent_ids if finding_id in held]:
            state, code = settled_again.get(finding_id, ("", ""))
            if state != "holds":
                held.discard(finding_id)
                unchecked[finding_id] = f"unconfirmed:{code}" if code else "unconfirmed"
    _warn_unchecked(unchecked, len(checked))
    return _settled(results, unchecked, held), usage


#: The reason (:data:`~inqtrix.agents.kernel.review_edits.RECHECK_REASONS`) an
#: applied edit carries by the code the re-check left it unchecked under:
#: these codes left it unsent or the check unable to run; every other code
#: is a check that ran and settled nothing.
_UNCHECKED_REASONS = {
    "no_source_text": "no_source_text",
    "no_segment": "format_only",
    "too_many_segments": "too_large",
    "over_call_limit": "too_large",
    "check_unavailable": "check_unavailable",
}


def _settled(
    results: list[FindingResult], unchecked: Mapping[str, str], held: Collection[str] = (),
) -> list[FindingResult]:
    """*results* with each edit of *held* marked held and each edit of
    *unchecked* carrying the reason its code reads as."""
    return [
        replace(result, recheck="held") if result.finding_id in held
        else replace(
            result,
            recheck_reason=_UNCHECKED_REASONS.get(unchecked[result.finding_id], "inconclusive"),
        )
        if result.finding_id in unchecked
        else result
        for result in results
    ]


def _check_verdict(
    check: Callable[[], Any], step: str, standing: str
) -> tuple[RevisionCheckVerdict | None, dict[str, int]]:
    """One call of the re-check, *step* by name: its verdict and the tokens
    it spent, or ``None`` with a WARNING that names the step and what
    stands unverified (*standing*) when the call fails or answers nothing
    usable. A cancel is the user's decision about the run, not a failed
    check: it propagates."""
    try:
        outcome = check()
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — the applied edits stand
        log.warning(
            "Revision %s failed (error_type=%s); %s stand unverified.",
            step, type(exc).__name__, standing,
        )
        return None, {}
    usage = _usage(outcome.usage)
    if not isinstance(outcome.value, RevisionCheckVerdict):
        log.warning(
            "Revision %s returned no usable verdict (marker=%s); %s stand unverified.",
            step, outcome.marker, standing,
        )
        return None, usage
    return outcome.value, usage


def _warn_unchecked(unchecked: Mapping[str, str], count: int) -> None:
    """One WARNING that counts the applied edits the re-check left unchecked,
    per code (*unchecked* holds each edit's code by its finding id)."""
    if unchecked:
        log.warning(
            "Revision re-check left %d of %d applied edits unchecked (%s); they stand "
            "unverified.",
            len(unchecked),
            count,
            ", ".join(f"{name} {number}" for name, number in Counter(unchecked.values()).items()),
        )


def _placements(
    applied: Sequence[Mapping[str, Any]],
    sent: Sequence[Mapping[str, Any]],
    revised: Mapping[str, str],
    replacements: Mapping[str, str],
    *,
    originals: Mapping[str, str],
) -> dict[str, Placement]:
    """Where each replacement of *sent* stands in its delivered output.

    Every applied edit of an output, a server finding's included, moved the
    text after it, by the span the revision spliced
    (:func:`~inqtrix.agents.kernel.review_edits.edit_span` on *originals*, in
    order of position);
    :func:`~inqtrix.agents.kernel.review_edits.spliced_positions` places each
    replacement where *revised* holds it.
    """
    wanted = {str(item["finding_id"]) for item in sent}
    placements: dict[str, Placement] = {}
    for output_id in dict.fromkeys(str(item["output_id"]) for item in sent):
        spliced: list[tuple[str, tuple[int, int, str]]] = []
        after = 0
        for item in sorted(
            (item for item in applied if str(item["output_id"]) == output_id),
            key=lambda item: int(item["start"]),
        ):
            replacement = replacements[str(item["finding_id"])]
            start, end = edit_span(
                originals[output_id], int(item["start"]), int(item["end"]),
                replacement, after=after,
            )
            after = end
            spliced.append((str(item["finding_id"]), (start, end, replacement)))
        positions = spliced_positions([span for _finding_id, span in spliced])
        for (finding_id, _span), (start, end) in zip(spliced, positions):
            if finding_id in wanted:
                placements[finding_id] = placement(revised[output_id], start, end)
    return placements


def _no_quotable_basis(
    item: Mapping[str, Any],
    found: Sequence[ChangedSegment],
    replacement: str,
    subject: ReviewSubject,
    evidence: Mapping[str, Any],
) -> bool:
    """Whether no row of this edit could hold: it adds or changes text and
    cites sources, yet none of them has stored text and no reading record
    carries it, so every quote under its labels would fail the verbatim check.

    A web source kept only as the search provider's context has no source
    text (``source_excerpt``), and that context is never a basis. A removal
    the finding asks for holds without any source, so an edit that only
    deletes always goes to the re-check; so does one that cites no source.
    """
    if all(segment.kind == "removed" for segment in found):
        return False
    if any(str(basis).startswith(DIAGNOSTIC_BASIS_PREFIX) for basis in item["basis_ids"]):
        return False
    cited = cited_labels(replacement, DELEGATED_FAMILIES)
    if not cited:
        return False
    references = subject.allowed_references.get(str(item["output_id"]), [])
    texts = {
        str(source.get("basis_id") or ""): str(source.get("source_excerpt") or "")
        for source in evidence.get("sources") or []
    }
    return not any(
        texts.get(basis)
        for basis in (
            f"{SOURCE_BASIS_PREFIX}{KernelDeps.ledger_id(dict(reference))}"
            for reference in references
        )
        if basis_label(basis, references) in cited
    )


def _basis_texts(block: Mapping[str, Any], output_id: str) -> dict[str, str]:
    """Each label the basis block names in *output_id*, with its source's
    stored text (``source_excerpt``; empty when the source has none, so no
    quote under that label verifies).

    A label stands in the block only when it names exactly one source in
    that output (:func:`_in_target_labels`).
    """
    return {
        str(source["cite_as"][output_id]): str(source.get("source_excerpt") or "")
        for source in block.get("sources") or []
        if output_id in (source.get("cite_as") or {})
    }


RECHECK_SEGMENTS_MAX = 12
"""Most segments one applied edit may have and still be re-checked; an edit
with more stays applied and unverified, with a WARNING."""

RECHECK_CALL_SEGMENTS_MAX = 40
"""Most segments one re-check call carries. Edits are taken whole, in verdict
order, while the sum stays within this bound; the rest stays applied and
unverified, with a WARNING."""

RECORD_QUOTE_MIN_WORDS = 2
"""Words a quote from a reading record needs: a field and its value. A
source passage needs :data:`~inqtrix.quote_limits.CITE_QUOTE_MIN_WORDS`."""

_DEVIATIONS = frozenset({"firmer_or_wider", "weaker_or_narrower", "contradicts", "not_in_basis"})
"""The dimension values a row reports as a deviation from its basis."""

_RESERVATION_DEVIATIONS = _DEVIATIONS - {"firmer_or_wider"}
"""The deviations of ``reservations`` that are no dropped reservation."""

_SAME_KINDS = frozenset({
    *((kind, kind) for kind in (
        "open_force_item", "example_or_indicator", "permission", "expectation",
        "duty", "deadline", "fact", "reading_limit",
    )),
    ("duty", "deadline"),
    ("deadline", "duty"),
})
"""``(presents_as, basis_shows)`` pairs that state a content as its basis shows it."""

_FIRMER_KINDS = frozenset(
    (strong, weak)
    for strong in ("duty", "deadline")
    for weak in ("open_force_item", "example_or_indicator", "permission", "expectation")
)
"""``(presents_as, basis_shows)`` pairs that state a content firmer than its
basis: only a duty or a deadline over a weaker kind. Between the weaker kinds
the borders blur (an example that grants an exemption is a permission too),
so those pairs are left unchecked, never rejected."""

_LIST_ITEM_START = re.compile(r"\A\s*(?:\(\s*(?:\d{1,3}|[a-z]{1,2})\s*\)|(?:\d{1,3}|[a-z])[.)])\s")
"""A text or a line that opens with an enumeration mark: ``(3)``, ``(h)``,
``(iv)``, ``3.``, ``a)`` (and ``3)``, ``a.``, ``( 3 )``)."""

_SENTENCE_CLOSED = re.compile(r"\.[\"'”’“»)\]]*(?:\[\d{1,3}\]|\d{1,3})?$")
"""The end of a line that closes a sentence with a full stop: any number of
closing quotes or brackets (``"``, ``'``, ``”``, ``’``, ``“``, ``»``, ``)``,
``]``) and then one footnote mark (``[3]``) or one bare footnote number
(``.3``), each of up to three digits, may follow the stop; nothing else
may."""


def settle_check(
    checks: Sequence[FindingCheck],
    segments: Sequence[ChangedSegment],
    *,
    finding: str,
    cited: Sequence[str],
    basis_texts: Mapping[str, str],
    records: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    """The server's decision on one applied edit from the re-check's rows.

    *checks* are every check the call returned for the edit's finding,
    *segments* the places the server listed for it
    (:func:`~inqtrix.agents.kernel.review_edits.changed_segments`), *finding*
    the finding's own words, *cited* the labels its replacement cites,
    *basis_texts* the source text of each of them that names exactly one
    source in the output, and *records* the reading records the finding
    rests on. A closed table, in this order:

    - not exactly one check: ``not_checked`` (``missing``, ``duplicate``);
    - no segment: ``not_checked`` (``no_segment``); rows that do not name
      every segment exactly once: ``not_checked`` (``rows_incomplete``);
    - one invalid row (:func:`_row_defect`): ``not_checked``
      (``row_invalid:<code>``), never a rejection;
    - a deviation on a row with new text, a verified quote and a claim
      (``presents_as`` is not ``no_claim``), unless the replaced text had it
      (``already_in_replaced_text``), or a removal no words of the finding
      ask for: ``rejected`` with the first code of
      :data:`~inqtrix.agents.kernel.review_edits.RECHECK_REJECTION_CODES`
      it found;
    - a deviation the replaced text had: ``not_checked`` (``pre_existing``);
    - an ``unclear`` dimension or a kind pair that is not the same
      (:func:`_kind_relation`; a row that states no claim never is):
      ``not_checked`` (``unclear``);
    - new text without a verified quote: ``not_checked`` (``no_basis``);
    - a fix that is not whole: ``not_checked`` (``not_fixed``);
    - otherwise ``holds``.

    A hold needs everything, a rejection one deviation a verified quote
    shows. No single field of a row unlocks a hold; the choice of quote, an
    open item and ``same`` together can: the server checks where a quote
    stands, not which item the row speaks of. Returns the outcome
    (``holds``, ``rejected`` or ``not_checked``) and its code (``""`` for
    ``holds``).
    """
    if not checks:
        return "not_checked", "missing"
    if len(checks) > 1:
        return "not_checked", "duplicate"
    [check] = checks
    if not segments:
        return "not_checked", "no_segment"
    if Counter(row.segment_id for row in check.rows) != Counter(
        segment.id for segment in segments
    ):
        return "not_checked", "rows_incomplete"
    rows = {row.segment_id: row for row in check.rows}
    with_new_text = {segment.id for segment in segments if segment.kind != "removed"}
    for segment in segments:
        defect = _row_defect(
            rows[segment.id], segment, finding=finding, cited=cited,
            basis_texts=basis_texts, records=records, with_new_text=with_new_text,
        )
        if defect:
            return "not_checked", f"row_invalid:{defect}"
    codes: list[str] = []
    pre_existing = unclear = no_basis = False
    for segment in segments:
        row = rows[segment.id]
        if segment.kind != "added" and row.before == "not_asked":
            codes.append("off_finding")
        if segment.kind == "removed":
            continue
        relation = _kind_relation(row.presents_as, row.basis_shows)
        deviates = (
            relation == "firmer"
            or row.reservations in _RESERVATION_DEVIATIONS
            or any(
                value in _DEVIATIONS
                for value in (row.force, row.bound, row.links, row.actor_reach)
            )
        )
        dropped = row.reservations == "firmer_or_wider"
        if (deviates or dropped) and row.already_in_replaced_text:
            pre_existing = True
        elif row.basis and row.presents_as != "no_claim":
            codes.extend(
                code for code, present in (
                    ("not_carried", deviates), ("reservation_dropped", dropped),
                ) if present
            )
        unclear = unclear or relation != "same" or "unclear" in (
            row.force, row.bound, row.actor_reach, row.reservations, row.links,
        )
        no_basis = no_basis or not row.basis
    if codes:
        found = [code for code in RECHECK_REJECTION_CODES if code in codes]
        if len(found) > 1:
            log.warning(
                "Revision re-check found the rejection codes %s on one edit; the "
                "first names it.", ", ".join(found),
            )
        return "rejected", found[0]
    if pre_existing:
        return "not_checked", "pre_existing"
    if unclear:
        return "not_checked", "unclear"
    if no_basis:
        return "not_checked", "no_basis"
    if check.fixes_named_defect != "yes":
        return "not_checked", "not_fixed"
    return "holds", ""


def _row_defect(
    row: SegmentRow,
    segment: ChangedSegment,
    *,
    finding: str,
    cited: Sequence[str],
    basis_texts: Mapping[str, str],
    records: Sequence[Mapping[str, Any]],
    with_new_text: Collection[str],
) -> str:
    """What makes *row* unusable for *segment*, as its code; ``""`` when nothing.

    A row with new text quotes only labels its replacement cites that name
    exactly one source (a label read as
    :func:`~inqtrix.agents.kernel.review_edits.bare_label` reads it, so
    ``[K5]`` is ``K5``), each quote verbatim in that source's text and long
    enough to prove something (:data:`CITE_QUOTE_MIN_WORDS`), or a reading
    record the finding rests on, as the basis block shows it (with its
    umlauts) and with :data:`RECORD_QUOTE_MIN_WORDS`. ``open_force_item``
    needs every quote inside the list its source text begins inside
    (:func:`_leading_list_run`): the server checks where a quote stands,
    never which item the row speaks of. A row with replaced text says what
    became of it (``before``): restated in a segment with new text, asked
    for by words that stand in the finding, or not asked; a row without
    replaced text says none and cannot carry what the replaced text had.
    """
    if segment.kind != "removed":
        shown = [_block_json(dict(record)) for record in records]
        labels = [bare_label(quote.label) for quote in row.basis]
        for quote, label in zip(row.basis, labels):
            if not label:
                if not records:
                    return "record_without_diagnostic"
                if quote_word_count(quote.quote) < RECORD_QUOTE_MIN_WORDS:
                    return "quote_too_short"
                if not quote_is_verbatim(quote.quote, shown):
                    return "quote_not_verbatim"
                continue
            if label not in cited:
                return "basis_label_not_cited"
            if label not in basis_texts:
                return "basis_label_unresolved"
            if quote_word_count(quote.quote) < CITE_QUOTE_MIN_WORDS:
                return "quote_too_short"
            if not quote_is_verbatim(quote.quote, [basis_texts[label]]):
                return "quote_not_verbatim"
        if row.basis_shows == "open_force_item" and not all(
            (run := _leading_list_run(basis_texts.get(label, "") if label else ""))
            and quote_is_verbatim(quote.quote, [run])
            for quote, label in zip(row.basis, labels)
        ):
            return "open_item_outside_leading_list"
    if segment.kind == "added":
        if row.before != "none":
            return "before_on_added_segment"
        if row.already_in_replaced_text:
            return "already_without_replaced_text"
        return ""
    if row.before == "none":
        return "before_missing"
    if row.before == "restated" and row.restated_in not in (
        with_new_text if segment.kind == "removed" else ("", segment.id)
    ):
        return "restated_in_invalid"
    if row.before == "change_asked":
        if quote_word_count(row.finding_quote) < CITE_QUOTE_MIN_WORDS:
            return "finding_quote_too_short"
        if not quote_is_verbatim(row.finding_quote, [finding]):
            return "finding_quote_not_verbatim"
    return ""


def _kind_relation(presents_as: str, basis_shows: str) -> str:
    """How the kind a replacement gives a content compares with its basis.

    ``same`` on the diagonal (a duty and a deadline are equally strong),
    ``firmer`` only for a duty or a deadline over a weaker kind (an
    expectation, a permission, an example or an open list item),
    ``not_checked`` for every other pair, ``unclear`` and ``no_claim``
    included: only ``firmer`` rejects, only the diagonal can hold.
    """
    pair = (presents_as, basis_shows)
    if pair in _SAME_KINDS:
        return "same"
    if pair in _FIRMER_KINDS:
        return "firmer"
    return "not_checked"


def _leading_list_run(text: str) -> str:
    """The start of *text* that stands inside the list *text* begins inside.

    *text* begins inside a list when it opens with an enumeration mark
    (:data:`_LIST_ITEM_START`). The run takes the lines from there (the
    pieces between ``\\n``) that open with such a mark or continue an item
    (they start in lowercase). It ends before a line that opens with
    neither when a blank line (a line of whitespace only, ``\\r`` and
    no-break spaces included) stands before it, or when the line with text
    before it closes a sentence with a full stop (:data:`_SENTENCE_CLOSED`:
    closing quotes or brackets and a footnote mark or number may follow the
    stop): the sentence after a closed last item stands outside, while a
    line broken inside its item stays in. The run ends at the end of its
    last line, before that line's ``\\n``. ``""`` when *text* opens with no
    mark.

    It says where a quote stands, never which item a row speaks of. A mark
    the pattern does not know, a capitalized continuation, a line inside an
    item that ends with a full stop (``e.g.`` before a name) or with a
    number read as a footnote (``2.5``, ``1.000``) before a capitalized
    line, or a page number ends the run early: an item after it is then
    outside, and a row that quotes it with an open item leaves the whole
    edit unchecked: never held, and not rejected either where another of
    its rows shows a deviation. The run ends late where a last item ends
    its line in any other way and no blank line follows it: with no full
    stop (``;``, ``!``, ``?``, ``…`` or none), or with a full stop that
    something outside :data:`_SENTENCE_CLOSED` follows (a second footnote
    mark ``[2][3]``, a superscript ``²``, a closing ``«``, ``‘`` or ``‹``,
    emphasis ``**``, a number of four digits); and a text whose lines break
    without ``\\n`` (at ``\\r`` alone, U+2028 or U+0085) is one line, so the
    run is all of it. The sentence after such an item then stands inside
    the run, and a quote from it can carry ``open_force_item``.
    """
    if not _LIST_ITEM_START.match(text):
        return ""
    end = 0
    position = 0
    previous = ""
    for raw in text.split("\n"):
        line = raw.strip()
        line_end = position + len(raw)
        position = line_end + 1
        if not line:
            previous = ""
            continue
        if not (_LIST_ITEM_START.match(line) or line[0].islower()) and (
            not previous or _SENTENCE_CLOSED.search(previous)
        ):
            return text[:end]
        end = line_end
        previous = line
    return text[:end]


def _fallback_window(host: ReviewHost, model: str | None) -> int:
    """The context limit of a model without a card: the platform's own rule."""
    from inqtrix.agents.kernel.algorithm import resolve_context_trigger_tokens

    return resolve_context_trigger_tokens(host.platform, model)


def _usage(usage: Any) -> dict[str, int]:
    return {
        "prompt_tokens": int((usage or {}).get("prompt_tokens", 0) or 0),
        "completion_tokens": int((usage or {}).get("completion_tokens", 0) or 0),
    }


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
