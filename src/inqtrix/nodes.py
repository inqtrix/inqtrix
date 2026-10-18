"""State machine node functions for the research agent.

Each node takes the state dict plus providers and strategies as keyword
arguments.  The five nodes correspond to the five phases of the research
loop: classify, plan, search, evaluate, answer.

Extracted from ``_original_agent.py`` and adapted to use the provider /
strategy / settings abstractions defined in the ``inqtrix`` package.
"""

from dataclasses import dataclass
import contextvars
import datetime as dt
import inspect
import json
import logging
import re
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from threading import Lock
from typing import Any, Callable, Iterator, NamedTuple, Sequence, TypeVar

from openai import OpenAIError

from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.constants import (
    CONTEXT_WINDOW_SAFETY_TOKENS,
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
)
from inqtrix.domains import (
    MODEL_REASON_PREFIX,
    PLACEMENT_FIELDS,
    ROLE_TO_TIER,
    UNJUDGED_REASON,
    placement_kind,
    strategy_placed,
)
from inqtrix.prompt_language import (
    LANGUAGE_NAMES_EN,
    date_line,
    resolve_answer_language,
)
from inqtrix.evidence import (
    pinned_tiers_from_records,
    EvidenceOverview,
    assemble_evidence_records,
    audit_answer_evidence_bindings,
    derive_claim_ledger_from_evidence,
    evidence_id_for_citation,
    merge_evidence_records,
    project_claim_verification_to_evidence,
    RECORD_VIEW_COMPACT,
    evidence_cut_words,
    render_evidence_ledger_overview,
    select_section_evidence_records,
)
from inqtrix.evidence_claim_processing import (
    claim_extraction_text as _shared_claim_extraction_text,
    claim_provider_refs as _shared_claim_provider_refs,
)
from inqtrix.search_result import GroundedSearchResult
from inqtrix.exceptions import (
    AgentCancelled,
    AgentModelCapacityError,
    AgentRateLimited,
    AgentTimeout,
    AnthropicAPIError,
    AzureOpenAIAPIError,
    BedrockAPIError,
)
from inqtrix.i18n import MESSAGES, t, t_count
from inqtrix.observability.context import bind_purpose
from inqtrix.json_helpers import (
    parse_json_string_list,
    parse_json_string_list_with_status,
)
from inqtrix.prompts import (
    ANSWER_CONTRACTS,
    build_evaluate_format_suffix,
    build_answer_section_system_prompt,
    build_answer_section_user_prompt,
)
from inqtrix.providers.base import (
    DEFAULT_LLM_FANOUT,
    LLMResponse,
    ProviderContext,
    _check_deadline,
    get_llm_provider_capabilities,
    get_search_provider_capabilities,
    is_model_capacity_error,
)
from inqtrix.report_profiles import AnswerSectionSpec, ReportProfile, evidence_limits_section_index
from inqtrix.runtime_logging import (
    forensic_enabled,
    make_record_id,
    normalize_source_provenance,
    sanitize_grounded_search_result,
)
from inqtrix.model_routing import describe_resolution, describe_unresolved_resolution
from inqtrix.scoring import SUPPORTED_STATUSES, append_score_snapshot, claim_assessments
from inqtrix.settings import AgentSettings
from inqtrix.state import (
    append_iteration_log,
    check_cancel_event,
    emit_progress,
    emit_run_event,
    track_tokens,
)
from inqtrix.strategies._source_tiering import quality_from_tiers
from inqtrix.strategies import ClaimExtractionStrategy, ProviderCitationRef, StrategyContext
from inqtrix.text import (
    LOG_CUT_MARKER,
    NEGATION_TOKENS,
    STOPWORDS,
    is_none_value,
    shorten_visibly,
    tokenize,
)
from inqtrix.urls import (
    count_allowed_links,
    domain_from_url,
    normalize_url,
    sanitize_error,
    sanitize_answer_links,
    scrub_credential_urls,
    today,
)

log = logging.getLogger("inqtrix")

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_ALLOWED_FINISH_REASONS = {"", "stop", "end_turn"}
_SOURCE_TIER_SORT_ORDER = {
    "primary": 0,
    "mainstream": 1,
    "stakeholder": 2,
    "unknown": 3,
    "low": 4,
}
_ADDITIONAL_LINK_LIMIT = 10
_LATER_ROUND_QUERY_MIN = 6
_EVIDENCE_DEPTH_MIN_SUPPORTED_CLAIMS = 3

# Single canonical confidence ceiling per evidence-contract status. The answer
# node computes the contract once, stores it on state, and clamps confidence via
# this table -- no per-status if/else scattered across the node. Ordering
# encodes evidence strength: algorithm_failed (synthesis unusable) < source-
# context-only (no claim-grounded sentence) < needs_review (a cited URL has no
# backing record) < clean (uncapped). Statuses absent here ("clean", "unknown")
# impose no cap.
_CONTRACT_CONFIDENCE_CAP: dict[str, int] = {
    "algorithm_failed": 3,
    "source_context_only": 4,
    "needs_review": 6,
}


class AnswerAppendixSections(NamedTuple):
    sections: list[str]
    references: list[dict[str, str]]
    additional_links: list[str]

# Crosscheck-Planner bonus applied to consolidated claims whose
# ``verification_basis == "quality_source"``.
#
# Rationale: that basis is set in ``DefaultClaimConsolidator.consolidate()``
# (`strategies/_claim_consolidation.py`) for claims whose only support is
# one mainstream source. The claim is supported (verified when the source's
# own text was checked, attributed when only the provider ties it to the
# source), but the *next* research round should actively try to
# corroborate it with an independent second source. This bonus, stacked on
# the generic +5 for any supported claim with ``citation_count < 2`` further
# down in :func:`_select_crosscheck_targets`, gives single-quality-source
# claims a total ranking score of +9, putting them at the front of the
# crosscheck-target queue.
#
# Coupled to: ``quality_source`` structure in ``consolidate()``.
# Changing one without the other will misalign the crosscheck planner
# with the verification rule. See also gotchas.md #47.
_CROSSCHECK_BONUS_QUALITY_SOURCE = 4
_STORM_SLOT_INSTRUCTIONS: tuple[tuple[str, str], ...] = (
    ("technical", "technical mechanisms, model details, or direct primary technical sources"),
    ("practical", "real-world deployments, user impact, or adoption evidence"),
    ("critical", "limitations, risks, failures, or credible counterarguments"),
    ("comparative", "comparisons with alternatives, competitors, or prior baselines"),
    ("historical", "timeline, prior context, and what changed recently"),
    ("latest", "latest developments, announcements, and dated updates"),
    ("data", "primary statistics, filings, benchmarks, or raw data"),
    ("stakeholder", "affected stakeholders, practitioners, institutions, or markets"),
    ("regulatory", "regulatory, legal, policy, or compliance evidence"),
    ("economic", "market, cost, funding, labor, or business implications"),
)


class SearchOutcome(NamedTuple):
    """One search call's typed result plus an optional non-fatal degrade notice.

    The notice is load-bearing: the search node counts it to report
    "X von Y Suchanfragen fehlgeschlagen" and to log per-query details, so a
    degraded search stays visible instead of looking like an
    ordinary empty result.
    """

    result: GroundedSearchResult
    notice: str | None
    started_at: str
    finished_at: str


def _domain_filter_for_query_text(
    query: str,
    *,
    base_domain_filter: list[str] | None = None,
) -> list[str] | None:
    """Return provider domain filters implied by one query string."""
    ql = (query or "").lower()
    domains: list[str] = []
    for match in re.finditer(r"(?:^|\s|\()site:([^\s)]+)", ql):
        dom = match.group(1).strip()
        dom = dom.replace("https://", "").replace("http://", "")
        dom = dom.split("/")[0].strip()
        dom = dom.strip(" ,.;:()[]{}<>\\\"'")
        if dom and dom not in domains:
            domains.append(dom)
    if domains:
        return domains
    return list(base_domain_filter or [])


def _target_query_count_for_round(round_index: int, settings: AgentSettings) -> int:
    """Return the planned and executed query width for one search round."""
    first_round = max(1, int(getattr(settings, "first_round_queries", 1) or 1))
    if int(round_index or 0) <= 0:
        return first_round
    return max(_LATER_ROUND_QUERY_MIN, first_round - 2)


def _claim_extraction_text(
    answer_text: str,
    citation_records: list[dict[str, Any]],
    *,
    max_sources: int = 8,
) -> str:
    """Use the shared provider-grounded prompt projection."""
    return _shared_claim_extraction_text(
        answer_text,
        citation_records,
        max_sources=max_sources,
    )



def _claim_provider_refs(citation_records: list[dict[str, Any]]) -> list[ProviderCitationRef]:
    """Use the shared provider-ref projection for deterministic binding."""
    return _shared_claim_provider_refs(citation_records)


def _claim_carries_numbers(claim_text: str) -> bool:
    """Whether a claim states a number — the topic-neutral reason to treat it
    as central for cross-checking. (A word list of "central" topics used to
    sit next to this rule; it was overfitting to a handful of runs.)"""
    return bool(re.search(r"\d", claim_text or ""))


def _claim_citation_count(claim: dict[str, Any]) -> int:
    """Count distinct citation URLs backing one consolidated claim."""
    urls: list[str] = []
    for citation in claim.get("citation_set", []) or []:
        url = citation.get("url", "") if isinstance(citation, dict) else citation
        normalized = normalize_url(str(url))
        if normalized and normalized not in urls:
            urls.append(normalized)
    for url in claim.get("source_urls", []) or []:
        normalized = normalize_url(str(url))
        if normalized and normalized not in urls:
            urls.append(normalized)
    return len(urls)


def _evidence_depth_gap(s: dict[str, Any]) -> dict[str, Any]:
    """Diagnose whether report evidence is too shallow for early stopping.

    Reads the consolidated claim ledger directly -- verification standing,
    cross-check basis, and per-claim citation counts all live there, so no
    separate report-evidence-bundle list is needed.
    """
    consolidated = claim_assessments(s)
    # Supported claims, verified and attributed alike: the depth of the
    # support structure, not whether a provider returned source text.
    supported_claims = [
        claim for claim in consolidated
        if claim.get("status") in SUPPORTED_STATUSES
    ]
    supported_count = len(supported_claims)
    cross_checked_count = sum(
        1 for claim in supported_claims
        if claim.get("verification_basis") == "cross_checked"
    )
    single_source_count = sum(
        1 for claim in supported_claims
        if _claim_citation_count(claim) < 2
    )
    quality_single_count = sum(
        1 for claim in supported_claims
        if claim.get("verification_basis") == "quality_source"
        and _claim_citation_count(claim) < 2
    )
    central_quality_single_count = sum(
        1 for claim in supported_claims
        if claim.get("verification_basis") == "quality_source"
        and _claim_citation_count(claim) < 2
        and _claim_carries_numbers(str(claim.get("claim_text", "") or ""))
    )
    single_source_ratio = (
        single_source_count / supported_count
        if supported_count > 0
        else 0.0
    )

    reasons: list[str] = []
    if (
        supported_count >= _EVIDENCE_DEPTH_MIN_SUPPORTED_CLAIMS
        and cross_checked_count == 0
    ):
        reasons.append("no_cross_checked_claims")
    if (
        supported_count >= _EVIDENCE_DEPTH_MIN_SUPPORTED_CLAIMS
        and single_source_ratio > 0.5
    ):
        reasons.append("majority_single_source_claims")
    if central_quality_single_count > 0:
        reasons.append("central_claim_single_quality_source")

    active = bool(reasons)
    gap = ""
    if active:
        gap = (
            "Report evidence is still too single-source; central claims need "
            "independent cross-checks or primary sources."
        )
    return {
        "active": active,
        "reason": ",".join(reasons),
        "gap": gap,
        "supported_count": supported_count,
        "cross_checked_count": cross_checked_count,
        "single_source_count": single_source_count,
        "single_source_ratio": round(single_source_ratio, 3),
        "quality_source_single_count": quality_single_count,
        "central_single_quality_source_count": central_quality_single_count,
    }


def _select_crosscheck_targets(
    consolidated_claims: list[dict[str, Any]],
    *,
    max_targets: int = 3,
) -> list[dict[str, Any]]:
    """Select a small set of claims that deserve independent verification.

    Reads only the consolidated claim ledger -- there is no separate
    report-evidence-bundle list. A claim's own citation set and
    support counts already carry everything needed to decide whether it
    is still single-sourced and deserves a cross-check query.
    """
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, claim in enumerate(consolidated_claims):
        claim_text = " ".join(str(claim.get("claim_text", "") or "").split())
        if not claim_text:
            continue
        status = str(claim.get("status", "unverified") or "unverified")
        basis = str(claim.get("verification_basis", "") or "")
        needs_primary = bool(claim.get("needs_primary", False))
        support_count = int(claim.get("support_count", 0) or 0)
        independent_count = int(claim.get("independent_support_count", 0) or 0)
        citation_count = len(claim.get("citation_set", []) or [])
        score = 0
        if (
            status in SUPPORTED_STATUSES
            and basis != "cross_checked"
            and citation_count < 2
        ):
            score += 5
        if basis == "quality_source":
            score += _CROSSCHECK_BONUS_QUALITY_SOURCE
        if status == "unverified":
            score += 3
        if needs_primary:
            score += 2
        if support_count < 2 or independent_count < 2:
            score += 2
        if basis in {"missing_primary_source", "weak_evidence"}:
            score += 2
        if _claim_carries_numbers(claim_text):
            score += 2
        if score <= 0:
            continue

        domains: list[str] = []
        for url in claim.get("source_urls", []) or []:
            domain = domain_from_url(str(url))
            if domain and domain not in domains:
                domains.append(domain)
        candidates.append((
            score,
            index,
            {
                "claim_id": str(claim.get("claim_id", "") or claim.get("signature", "") or ""),
                "claim_text": claim_text[:260],
                "status": status,
                "verification_basis": basis,
                "needs_primary": needs_primary,
                "support_count": support_count,
                "independent_support_count": independent_count,
                "citation_count": citation_count,
                "source_domains": domains[:4],
            },
        ))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [target for _, _, target in candidates[: max(0, int(max_targets or 0))]]


def _append_query_slot(
    slots: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    slot_type: str,
    instruction: str,
    **extra: Any,
) -> None:
    """Append a de-duplicated research slot."""
    normalized_instruction = " ".join(instruction.split())
    if not normalized_instruction:
        return
    key = (slot_type, normalized_instruction.lower()[:220])
    if key in seen:
        return
    seen.add(key)
    slot = {"slot_type": slot_type, "instruction": normalized_instruction}
    for name, value in extra.items():
        if value not in (None, "", [], {}):
            slot[name] = value
    slots.append(slot)


def _build_query_slots(
    s: dict[str, Any],
    *,
    target_count: int,
    crosscheck_targets: list[dict[str, Any]],
    evidence_depth_gap: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build one concrete research slot for each planned search query."""
    target_count = max(1, int(target_count or 1))
    slots: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    question = str(s.get("question", "") or "").strip()
    round_index = int(s.get("round", 0) or 0)

    # One slot per still-open sub-question, verbatim: the sub-question IS
    # the search assignment (a rubric label like "Cross-Check" is not a
    # topic to search for).
    for aspect in (s.get("uncovered_aspects", []) or [])[:target_count]:
        _append_query_slot(
            slots,
            seen,
            "gap",
            f"Answer this still-open sub-question with direct evidence: {aspect}",
            aspect=str(aspect),
        )
        if len(slots) >= target_count:
            return slots[:target_count]

    gap_text = " ".join(str(s.get("gaps", "") or "").split())
    if gap_text and not is_none_value(gap_text):
        _append_query_slot(
            slots,
            seen,
            "gap",
            f"Resolve this explicit evidence gap: {gap_text}",
            gap=gap_text[:240],
        )

    max_crosscheck_slots = max(1, target_count // 2) if round_index > 0 else 0
    for target in crosscheck_targets[:max_crosscheck_slots]:
        claim_text = str(target.get("claim_text", "") or "")
        domains = list(target.get("source_domains", []) or [])
        _append_query_slot(
            slots,
            seen,
            "crosscheck",
            "Find an independent source that confirms or refutes this claim "
            f"without relying on the existing domains: {claim_text}",
            target_claim=claim_text,
            avoid_domains=domains,
            claim_id=str(target.get("claim_id", "") or ""),
            verification_basis=str(target.get("verification_basis", "") or ""),
        )
        if len(slots) >= target_count:
            return slots[:target_count]

    if evidence_depth_gap.get("active") and len(slots) < target_count:
        target_claim = (
            str(crosscheck_targets[0].get("claim_text", "") or "")
            if crosscheck_targets
            else question
        )
        _append_query_slot(
            slots,
            seen,
            "primary_source",
            "Find the original primary source or official data behind this central claim "
            f"instead of another summary article: {target_claim}",
            evidence_depth_reason=evidence_depth_gap.get("reason", ""),
        )

    # Data verification is a contract decision, not a digit in the question
    # ("2030" made every question a numbers check): only extraction and
    # claim checks, or an existing cross-check target, warrant the slot.
    answer_contract = str(s.get("answer_contract", "general") or "general")
    needs_data_slot = (
        answer_contract in {"data_extraction", "claim_check"}
        or bool(crosscheck_targets)
    )
    if needs_data_slot and len(slots) < target_count:
        _append_query_slot(
            slots,
            seen,
            "data_verification",
            "Which primary data, benchmark, filing, or official statistics verify "
            "the key numbers of the main question?",
        )

    if round_index > 0 and len(slots) < target_count:
        _append_query_slot(
            slots,
            seen,
            "counterevidence",
            "What credible evidence challenges or qualifies the strongest emerging answer to the main question?",
        )

    perspective_index = 0
    while len(slots) < target_count:
        label, instruction = _STORM_SLOT_INSTRUCTIONS[
            (round_index + perspective_index) % len(_STORM_SLOT_INSTRUCTIONS)
        ]
        _append_query_slot(
            slots,
            seen,
            "storm_perspective",
            f"Use the {label} perspective to find {instruction} for the main question",
            perspective=label,
        )
        perspective_index += 1
        if perspective_index > len(_STORM_SLOT_INSTRUCTIONS) + target_count:
            break

    return slots[:target_count]


def _fallback_query_for_slot(
    slot: dict[str, Any],
    *,
    question: str,
    search_language: str,
) -> str:
    """Create a complete fallback question for one slot if parsing underfills."""
    slot_type = str(slot.get("slot_type", "") or "")
    claim = str(slot.get("target_claim", "") or "")
    gap = str(slot.get("gap", "") or "")
    aspect = str(slot.get("aspect", "") or "")
    english = (search_language or "").lower().startswith("en")
    if english:
        if slot_type == "crosscheck" and claim:
            return f"Which independent sources confirm or refute the claim that {claim}?"
        if slot_type == "primary_source" and (claim or question):
            return f"What is the original primary source for {claim or question}?"
        if slot_type == "gap" and aspect:
            return aspect
        if slot_type == "gap" and gap:
            return f"Which sources directly answer the open evidence gap {gap}?"
        if slot_type == "counterevidence":
            return f"What credible evidence challenges the main claims about {question}?"
        if slot_type == "data_verification":
            return f"Which primary data sources verify the key numbers about {question}?"
        return f"What does the {slot.get('perspective', 'specific')} perspective show about {question}?"
    if slot_type == "crosscheck" and claim:
        return f"Welche unabhängigen Quellen bestätigen oder widerlegen die Aussage, dass {claim}?"
    if slot_type == "primary_source" and (claim or question):
        return f"Was ist die Primärquelle für {claim or question}?"
    if slot_type == "gap" and aspect:
        return aspect
    if slot_type == "gap" and gap:
        return f"Welche Quellen beantworten die offene Evidenzlücke {gap} direkt?"
    if slot_type == "counterevidence":
        return f"Welche glaubwürdige Evidenz widerspricht den wichtigsten Aussagen zu {question}?"
    if slot_type == "data_verification":
        return f"Welche Primärdaten verifizieren die zentralen Zahlen zu {question}?"
    return f"Was zeigt die Perspektive {slot.get('perspective', 'spezifisch')} zu {question}?"


def _trace_evidence_overview(
    overview: EvidenceOverview,
    *,
    budget_chars: int,
    view: str,
) -> None:
    """Put the evidence cut on the active node span and in the log, always on.

    The prompt footer tells the model that records were dropped; this
    tells the operator — as attributes (counts, budget) and one span
    event naming the dropped labels and query ids. URLs are content and
    travel only under the content-capture policy. A cut that drops
    records raises the observation level to WARNING: in normal operation
    the budget must not decide what the writer sees.
    """
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import add_span_event, enrich_current_span

    attributes: dict[str, Any] = {
        semconv.INQTRIX_EVIDENCE_RENDERED_COUNT: int(
            overview.rendered_record_count
        ),
        semconv.INQTRIX_EVIDENCE_OMITTED_COUNT: int(
            overview.omitted_record_count
        ),
        semconv.INQTRIX_EVIDENCE_OMITTED_QUERY_COUNT: len(
            overview.omitted_query_ids
        ),
        semconv.INQTRIX_EVIDENCE_BUDGET_CHARS: int(budget_chars),
    }
    if overview.omitted_record_count > 0:
        attributes[semconv.LANGFUSE_OBSERVATION_LEVEL] = (
            semconv.OBSERVATION_LEVEL_WARNING
        )
        attributes[semconv.LANGFUSE_OBSERVATION_STATUS_MESSAGE] = (
            f"{overview.omitted_record_count} Belege ausserhalb des "
            f"Evidenz-Budgets ({budget_chars} Zeichen)"
        )
    enrich_current_span(attributes)
    if overview.omitted_record_count <= 0:
        return
    log.warning(
        "Evidence budget (%s): %d of %d report-eligible sources from %d searches not "
        "rendered (budget %d characters).",
        view,
        overview.omitted_record_count,
        overview.omitted_record_count + overview.rendered_record_count,
        len(overview.omitted_query_ids),
        budget_chars,
    )
    payload: dict[str, Any] = {
        "view": view,
        "omitted_count": int(overview.omitted_record_count),
        "rendered_count": int(overview.rendered_record_count),
        "budget_chars": int(budget_chars),
        "query_ids": list(overview.omitted_query_ids),
        "labels": [
            record.get("label", "") for record in overview.omitted_records
        ],
        "evidence_ids": [
            record.get("evidence_id", "") for record in overview.omitted_records
        ],
    }
    # URLs are content: only under the composed capture policy, the same
    # gate every other content attribute in the process passes.
    from inqtrix.observability.content import active_content_policy

    if active_content_policy().capture_content:
        payload["urls"] = [
            record.get("url", "") for record in overview.omitted_records
        ]
    add_span_event(semconv.EVIDENCE_OMITTED_EVENT, payload)


def _append_forensic_event(
    s: dict[str, Any],
    settings: AgentSettings,
    *,
    event: str,
    node: str,
    payload: dict[str, Any],
) -> None:
    """Append one forensic event through the canonical iteration-log path."""
    if not forensic_enabled(settings):
        return
    append_iteration_log(
        s,
        {
            "event": event,
            "node": node,
            "timestamp": time.time(),
            **payload,
        },
        testing_mode=settings.testing_mode,
    )


def _emit_node_model_resolution_warning(
    s: dict[str, Any],
    settings: AgentSettings,
    *,
    node: str,
    desc: dict[str, str],
    reason: str,
    message: str,
) -> None:
    """Surface a model-resolution fallback on every visible diagnostics channel."""
    log.warning(message)
    emit_progress(s, f"Warnung: {message}", severity="warning")
    append_iteration_log(
        s,
        {
            "event": "node_model_resolution_warning",
            "node": node,
            "timestamp": time.time(),
            "reason": reason,
            **desc,
        },
        testing_mode=settings.testing_mode,
    )


def _remember_node_model_resolution(
    s: dict[str, Any],
    node: str,
    desc: dict[str, str],
) -> None:
    """Store the exact node routing used by this run for result surfaces."""
    resolutions = dict(s.get("node_model_resolutions") or {})
    resolutions[node] = dict(desc)
    s["node_model_resolutions"] = resolutions


def _resolve_node_llm(
    s: dict[str, Any],
    settings: AgentSettings,
    providers: ProviderContext,
    node: str,
) -> tuple[str, str]:
    """Resolve ``(model, reasoning_effort)`` for a call site and surface the choice.

    Single entry point used by every node so model and reasoning selection is
    uniform across the whole graph. Routes through
    :func:`inqtrix.model_routing.describe_resolution` using the per-run
    ``settings.model_tier`` selection, then emits the resolution on the live
    and forensic channels so the model/effort/tier actually used for each node
    is visible, exactly as resolved and never guessed:

    * ``inqtrix.node.model_resolution`` as a native run-event -> the React live
      view (whenever a run-event sink is wired).
    * ``node_model_resolution`` as a forensic iteration-log event -> the audit
      log (profile-gated).

    Both carry ``model_source``/``effort_source``, so a ``reasoning_model``
    default that grips instead of a tier is explicit, not silent. When the
    provider exposes no ``models`` attribute, or resolution produces an empty
    model, the fallback is also surfaced via warning log, progress event, and a
    ``node_model_resolution_warning`` iteration-log marker.

    Args:
        s: Mutable agent state (receives both events).
        settings: The active :class:`~inqtrix.settings.AgentSettings`; its
            ``model_tier`` field carries an optional per-run tier override.
        providers: The provider context; ``providers.llm.models`` is the
            resolution source.
        node: Call-site name (see
            :data:`inqtrix.model_routing.NODE_TIER_ASSIGNMENT`).

    Returns:
        A ``(model, reasoning_effort)`` tuple. ``reasoning_effort`` is ``""``
        when the tier inherits the provider default. Both are empty strings
        when the provider exposes no ``models`` attribute.
    """
    models = getattr(providers.llm, "models", None)
    requested_tier = (getattr(settings, "model_tier", "") or "").strip() or None
    # The chat/editor model picker selects a concrete model for the direct-chat
    # path only; research nodes always stay on tier routing (bewusste Grenze).
    requested_model = ""
    requested_effort = ""
    if node == "direct_chat":
        requested_model = (getattr(settings, "model", "") or "").strip()
        requested_effort = (getattr(settings, "effort", "") or "").strip()
    if models is None and not requested_model:
        desc = describe_unresolved_resolution(node, requested_tier)
        _remember_node_model_resolution(s, node, desc)
        emit_run_event(s, "inqtrix.node.model_resolution", dict(desc))
        _append_forensic_event(
            s,
            settings,
            event="node_model_resolution",
            node=node,
            payload=desc,
        )
        _emit_node_model_resolution_warning(
            s,
            settings,
            node=node,
            desc=desc,
            reason="provider_models_missing",
            message=(
                f"Node '{node}' could not resolve a model: provider "
                f"{type(providers.llm).__name__} exposes no 'models' attribute; "
                "the provider's own default will apply unseen."
            ),
        )
        return "", ""
    desc = describe_resolution(
        node,
        models,
        requested_tier,
        requested_model=requested_model,
        requested_effort=requested_effort,
    )
    _remember_node_model_resolution(s, node, desc)
    emit_run_event(s, "inqtrix.node.model_resolution", dict(desc))
    _append_forensic_event(
        s,
        settings,
        event="node_model_resolution",
        node=node,
        payload=desc,
    )
    if not desc["model"]:
        _emit_node_model_resolution_warning(
            s,
            settings,
            node=node,
            desc=desc,
            reason="empty_resolved_model",
            message=(
                f"Node '{node}' resolved an empty model "
                f"(source={desc['model_source']}): the provider's own default "
                "will apply unseen."
            ),
        )
    return desc["model"], desc["effort"]


def _resolve_answer_fallback_model(
    settings: AgentSettings,
    providers: ProviderContext,
    primary_answer_model: str,
) -> str | None:
    """Resolve the optional answer fallback model through the central router."""
    models = getattr(providers.llm, "models", None)
    if models is None:
        return None
    requested_tier = (getattr(settings, "model_tier", "") or "").strip() or None
    desc = describe_resolution("evaluate", models, requested_tier)
    fallback_model = desc["model"]
    if not fallback_model or fallback_model == primary_answer_model:
        return None
    return fallback_model


def _consume_optional(strategy: object, method_name: str) -> Any:
    """Call an optional per-call channel of a strategy (``consume_…``) if it
    exists; a strategy without it yields nothing."""
    consumer = getattr(strategy, method_name, None)
    return consumer() if callable(consumer) else None


def _claim_extract_accepts(strategy: ClaimExtractionStrategy, *names: str) -> bool:
    """Whether *strategy*'s ``extract`` accepts every kwarg in ``names``
    (an explicit parameter or ``**kwargs``): optional kwargs are only passed
    to extractors whose signature takes them, so a custom strategy on an
    older signature keeps working."""
    try:
        params = inspect.signature(strategy.extract).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(name in params for name in names)


def _claim_extract_accepts_routing(strategy: ClaimExtractionStrategy) -> bool:
    """Return whether *strategy*'s ``extract`` accepts the routing kwargs.

    The search node resolves a model + reasoning effort for ``claim_extract``
    and forwards them to ``extract`` as ``model`` / ``reasoning_effort``. Those
    kwargs were added to the strategy contract after the fact, so a custom
    strategy written against the older signature would raise ``TypeError`` if
    they were passed unconditionally. This guard keeps the Baukasten contract
    backward compatible: the kwargs are a best-effort hint and are only passed
    to extractors whose signature actually accepts them (an explicit ``model``
    parameter, or ``**kwargs``). A pre-existing strategy on the old signature
    keeps working and simply uses its own model.
    """
    try:
        params = inspect.signature(strategy.extract).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "model" in params and "reasoning_effort" in params


def _claim_fanout_width(n_inputs: int, llm_provider: object) -> int:
    """Simultaneous claim-extraction LLM calls to run (3.3).

    Bounded by the LLM provider's OWN declared concurrency
    (``max_llm_concurrency``), NOT the search provider's width — pacing
    the per-result LLM fan-out to the search cap is what bursts the LLM
    endpoint into a 429. A provider that declares no cap falls back to
    :data:`~inqtrix.providers.base.DEFAULT_LLM_FANOUT` (never 0). Never
    exceeds the input count, never below 1.
    """
    cap = (
        get_llm_provider_capabilities(llm_provider).max_concurrency
        or DEFAULT_LLM_FANOUT
    )
    return max(1, min(n_inputs, cap))


def _strict_algorithm_failure_mode(settings: AgentSettings) -> bool:
    """Return whether core algorithm failures must block normal reports."""
    return (
        settings.report_profile is ReportProfile.DEEP
        or str(getattr(settings, "observability_profile", "summary")).lower() == "forensic"
    )


def _record_algorithm_failure(
    s: dict[str, Any],
    settings: AgentSettings,
    *,
    node: str,
    phase: str,
    reason: str,
    message: str,
    blocking: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a visible core-path failure without treating it as a repair path."""
    entry: dict[str, Any] = {
        "phase": phase,
        "reason": reason,
        "message": message,
        "blocking": bool(blocking),
        "round": int(s.get("round", 0) or 0),
        "run_id": str(s.get("_run_id", "") or ""),
    }
    if details:
        entry.update(details)
    failures = s.setdefault("algorithm_failures", [])
    if isinstance(failures, list):
        failures.append(entry)
    log.warning("ALGO-FAIL phase=%s reason=%s", phase, reason)
    _append_forensic_event(
        s,
        settings,
        event="algorithm_failure",
        node=node,
        payload=entry,
    )
    return entry


def _blocking_algorithm_failures(s: dict[str, Any]) -> list[dict[str, Any]]:
    """Return algorithm failures that must prevent normal report synthesis."""
    return [
        failure
        for failure in s.get("algorithm_failures", []) or []
        if isinstance(failure, dict) and bool(failure.get("blocking"))
    ]


def _llm_context_window_tokens(llm: Any) -> int | None:
    """Return a positive configured context window from an LLM provider."""
    value = getattr(llm, "context_window_tokens", None)
    if callable(value):
        value = value()
    if isinstance(value, int) and value > 0:
        return value
    raw = getattr(llm, "_context_window_tokens", None)
    return raw if isinstance(raw, int) and raw > 0 else None


def _llm_default_output_tokens(llm: Any) -> int:
    """Return the provider's configured reasoning output budget."""
    raw = getattr(llm, "_default_max_tokens", None)
    return raw if isinstance(raw, int) and raw > 0 else DEFAULT_LLM_MAX_OUTPUT_TOKENS


def _estimate_required_context_tokens(
    *,
    system: str | None,
    prompt: str,
    requested_output_tokens: int,
) -> tuple[int, int]:
    """Estimate input and total token capacity for one LLM call."""
    char_count = len(system or "") + len(prompt or "")
    estimated_input_tokens = max(1, (char_count + 3) // 4)
    estimated_required_tokens = (
        estimated_input_tokens
        + max(0, int(requested_output_tokens or 0))
        + CONTEXT_WINDOW_SAFETY_TOKENS
    )
    return estimated_input_tokens, estimated_required_tokens


def _check_llm_context_capacity(
    s: dict[str, Any],
    settings: AgentSettings,
    *,
    providers: ProviderContext,
    node: str,
    phase: str,
    model: str,
    system: str | None,
    prompt: str,
    requested_output_tokens: int,
) -> dict[str, int | str | bool | None]:
    """Warn or fail before sending prompts that exceed configured capacity."""
    estimated_input_tokens, estimated_required_tokens = _estimate_required_context_tokens(
        system=system,
        prompt=prompt,
        requested_output_tokens=requested_output_tokens,
    )
    required_floor = int(getattr(settings, "required_context_window_tokens", 0) or 0)
    required_tokens = max(required_floor, estimated_required_tokens)
    context_window_tokens = _llm_context_window_tokens(providers.llm)
    details: dict[str, int | str | bool | None] = {
        "phase": phase,
        "model": model,
        "context_window_tokens": context_window_tokens,
        "required_context_window_tokens": required_floor,
        "estimated_input_tokens": estimated_input_tokens,
        "requested_output_tokens": int(requested_output_tokens or 0),
        "context_window_safety_tokens": CONTEXT_WINDOW_SAFETY_TOKENS,
        "estimated_required_context_tokens": estimated_required_tokens,
    }

    if context_window_tokens is None:
        warning_key = f"_context_window_unknown_warned_{model or 'default'}"
        if not s.get(warning_key):
            s[warning_key] = True
            log.warning(
                "CAPACITY-WARN context_window unknown (model=%s, phase=%s, "
                "estimated_required_context_tokens=%d, required_context_window_tokens=%d)",
                model or "-",
                phase,
                estimated_required_tokens,
                required_floor,
            )
            emit_progress(
                s,
                t(
                    s,
                    "context_window_unknown_warning",
                    model=model or "-",
                    required=required_floor,
                    estimated=estimated_required_tokens,
                ),
            )
            _append_forensic_event(
                s,
                settings,
                event="model_capacity_warning",
                node=node,
                payload={**details, "reason": "context_window_unknown"},
            )
        return details

    if context_window_tokens < required_tokens:
        reason = (
            "below_required_floor"
            if context_window_tokens < required_floor
            else "estimated_requirement_exceeds_window"
        )
        message = (
            f"Model context window {context_window_tokens} tokens is below "
            f"required {required_tokens} tokens for phase {phase}."
        )
        emit_progress(
            s,
            t(
                s,
                "context_window_too_small",
                model=model or "-",
                window=context_window_tokens,
                required=required_tokens,
            ),
        )
        _record_algorithm_failure(
            s,
            settings,
            node=node,
            phase="context_window",
            reason=reason,
            message=message,
            blocking=True,
            details=details,
        )
        raise AgentModelCapacityError(model or "", phase, message)

    return details


def _query_id_for(s: dict[str, Any], *, round_index: int, query_index: int, query: str) -> str:
    return make_record_id(
        "qry",
        s.get("_run_id", ""),
        round_index,
        query_index,
        query,
    )


def _tier_explanations_for_urls(
    urls: list[str],
    strategies: StrategyContext,
) -> dict[str, dict[str, str]]:
    explain = getattr(strategies.source_tiering, "explain_url", None)
    explanations: dict[str, dict[str, str]] = {}
    for url in urls:
        canonical = normalize_url(str(url))
        if not canonical:
            continue
        if callable(explain):
            raw = explain(canonical)
            explanations[canonical] = {
                "tier": str(raw.get("tier", "unknown")),
                "tier_reason": str(raw.get("tier_reason", "")),
            }
        else:
            explanations[canonical] = {
                "tier": strategies.source_tiering.tier_for_url(canonical),
                "tier_reason": "tier_for_url",
            }
    return explanations


def _pin_source_roles(
    s: dict[str, Any],
    records: list[dict[str, Any]],
    roles: list[dict[str, str]],
) -> tuple[int, int]:
    """Pin the extractor's role judgements in the run's source registry.

    Precedence: a placement by the configured tiering strategy (the
    structural floor or a custom strategy's own reason) wins over the
    model; the first model judgement of a source wins over later ones (a
    conflict is a WARNING and a counter, never a silent flip); only an
    unjudged source takes a judgement. The registry entry is the ONE holder
    of a placement; records and ledger entries take it from there
    (:func:`_with_registry_placement`, :func:`_refresh_ledger_placement`).

    Returns ``(applied, conflicts)``.
    """
    registry: dict[str, dict[str, Any]] = s.setdefault("source_records", {})
    by_url = {str(rec.get("canonical_url", "")): rec for rec in records}
    applied = conflicts = 0
    for judgement in roles:
        record = by_url.get(str(judgement.get("url", "")))
        if record is None:
            continue
        role = str(judgement.get("role", ""))
        pinned = registry.get(str(record.get("source_id", "")), record)
        current = str(pinned.get("tier_reason") or "")
        if strategy_placed(current):
            continue
        model_reason = f"{MODEL_REASON_PREFIX}{role}"
        if placement_kind(current)[0] == "model":
            if current != model_reason:
                conflicts += 1
                log.warning(
                    "Conflicting source role for %s: keeping %s, discarding %s (first "
                    "judgment takes precedence)",
                    pinned.get("domain", ""),
                    current,
                    model_reason,
                )
            continue
        pinned["tier"] = ROLE_TO_TIER[role]
        pinned["tier_reason"] = model_reason
        pinned["role_reason"] = str(judgement.get("reason", "") or "")
        applied += 1
    return applied, conflicts


def _recount_source_roles(s: dict[str, Any], *, conflicts: int = 0) -> dict[str, int]:
    """The run's placement counters, recounted over the registry.

    ``strategy`` = placed by the configured tiering strategy (structural
    floor or custom), ``model`` = judged by the extraction call,
    ``unjudged`` = neither; ``conflicts`` accumulates the judgements that
    contradicted a pinned one. The one owner of ``source_role_counts``.
    """
    counts = s.setdefault(
        "source_role_counts",
        {"strategy": 0, "model": 0, "unjudged": 0, "conflicts": 0},
    )
    tally = {"strategy": 0, "model": 0, "unjudged": 0}
    for record in (s.get("source_records", {}) or {}).values():
        kind, _ = placement_kind(str(record.get("tier_reason") or ""))
        tally["model" if kind == "model" else "unjudged" if kind == "unjudged" else "strategy"] += 1
    counts.update(tally)
    counts["conflicts"] = int(counts.get("conflicts", 0)) + int(conflicts)
    return counts


def _with_registry_placement(
    s: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    """The per-query source record with the registry's placement fields."""
    pinned = (s.get("source_records", {}) or {}).get(str(record.get("source_id", "")))
    if pinned is None or pinned is record:
        return record
    return {**record, **{field: pinned.get(field, record.get(field, "")) for field in PLACEMENT_FIELDS}}


def _refresh_ledger_placement(s: dict[str, Any]) -> int:
    """Copy the registry's placement onto every evidence-ledger record.

    A source judged in a later query or round than the one whose record
    reached the ledger first would otherwise keep its pre-judgement tier
    there — and the derived claim ledger, the consolidator and the report's
    reference list read the ledger. Returns the number of records changed.
    """
    registry = s.get("source_records", {}) or {}
    changed = 0
    for record in s.get("evidence_ledger", []) or []:
        pinned = registry.get(str(record.get("source_id", "")))
        if pinned is None:
            continue
        placement = {field: pinned.get(field, "") for field in PLACEMENT_FIELDS}
        if any(record.get(field, "") != value for field, value in placement.items()):
            record.update(placement)
            changed += 1
    return changed


_EVIDENCE_LABEL_RE = re.compile(r"\[(E\d+)\](?:\(https?://[^\s)]+\))?")
_ADJACENT_EVIDENCE_LINK_RE = re.compile(
    r"(\[E\d+\]\(https?://[^\s)]+\))(?=\[E\d+\]\()"
)


def _extract_evidence_labels(text: str) -> set[str]:
    """Return per-source evidence labels (``E1`` ..) used in answer text."""
    return {match.group(1) for match in _EVIDENCE_LABEL_RE.finditer(text or "")}


def _expand_bare_evidence_label_links(
    answer_text: str,
    label_urls: dict[str, str],
) -> tuple[str, int]:
    """Convert bare evidence labels such as ``[E12]`` into Markdown links.

    ``label_urls`` is :attr:`EvidenceOverview.label_urls` -- the single
    label/URL map derived from the EvidenceLedger overview.
    """
    if not answer_text:
        return answer_text, 0
    expanded = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal expanded
        label = match.group(1)
        url = label_urls.get(label)
        if not url:
            return f"[{label}: nicht zugeordnet]"
        expanded += 1
        return f"[{label}]({url})"

    linked = re.sub(r"\[(E\d+)\](?!\()", replace, answer_text)
    linked = _ADJACENT_EVIDENCE_LINK_RE.sub(r"\1 ", linked)
    return linked, expanded


def _compact_section_summary(heading: str, text: str, max_chars: int = 900) -> str:
    """Return a deterministic compact summary of one rendered section."""
    compact = " ".join((text or "").split())
    if len(compact) > max_chars:
        compact = compact[:max_chars].rstrip() + "..."
    return f"{heading}: {compact}" if compact else ""


def _build_answer_prompt_diagnostics(
    state_data: dict[str, Any],
    s: dict[str, Any],
) -> dict[str, Any]:
    """Return compact counters that explain answer-prompt evidence density.

    All counters describe the single EvidenceLedger overview view -- there
    are no parallel report-evidence / prompt-evidence-unit / rendered-
    context channels to account for.
    """
    evidence_ledger = list(s.get("evidence_ledger", []) or [])
    report_eligible = [r for r in evidence_ledger if r.get("report_eligible")]
    return {
        "evidence_record_count": len(evidence_ledger),
        "report_eligible_evidence_count": len(report_eligible),
        "claimless_evidence_count": sum(
            1 for record in evidence_ledger if not record.get("claims")
        ),
        "rendered_evidence_record_count": int(
            state_data.get("rendered_evidence_record_count", 0) or 0
        ),
        "omitted_evidence_record_count": int(
            state_data.get("omitted_evidence_record_count", 0) or 0
        ),
        "evidence_overview_chars": len(state_data.get("evidence_overview", "") or ""),
        "visible_evidence_label_count": int(
            state_data.get("visible_evidence_label_count", 0) or 0
        ),
        "allowed_citation_count": len(state_data.get("allowed_citations", []) or []),
        "all_citation_count": len(s.get("all_citations", []) or []),
        "consolidated_claim_count": len(claim_assessments(s)),
    }


@dataclass(slots=True)
class _AnswerCompositionResult:
    answer: str
    finish_reason: str
    section_logs: list[dict[str, Any]]
    composition_aborted: bool = False
    consecutive_empty_at_break: int = 0
    sections_planned: int = 0
    sections_attempted: int = 0


def _llm_complete_with_metadata(
    llm: Any,
    prompt: str,
    **kwargs: Any,
) -> LLMResponse:
    """Return an ``LLMResponse`` even for custom providers without metadata support."""
    state = kwargs.pop("state", None)
    complete_with_metadata = getattr(llm, "complete_with_metadata", None)
    if callable(complete_with_metadata):
        response = complete_with_metadata(prompt, state=None, **kwargs)
        if isinstance(response, LLMResponse):
            normalized = response
        else:
            content = getattr(response, "content", None)
            normalized = LLMResponse(content=str(content if content is not None else response))
    else:
        normalized = LLMResponse(
            content=llm.complete(prompt, **kwargs),
            model=str(kwargs.get("model") or ""),
        )

    if state is not None:
        track_tokens(state, normalized)
    return normalized


def _provider_retry_reason(notice: dict[str, Any]) -> str:
    """Return a compact retry reason safe for progress output."""
    for key in ("error_code", "status_code", "error_type", "reason"):
        value = notice.get(key)
        if value not in (None, ""):
            return str(value)
    return "transient_error"


@contextmanager
def _provider_retry_progress_context(
    provider: object,
    s: dict[str, Any],
    *,
    operation_label: str,
    testing_mode: bool = False,
) -> Iterator[object]:
    """Bind retry progress and run cancellation to one provider call.

    Two thread-local scopes are entered together: retry attempts become
    live warning progress events, and the run's ``_cancel_event`` (when
    present) becomes a provider-level cancel probe so in-flight retry
    ladders and backoff sleeps stop promptly after a cancel request.
    Entering here — inside fan-out worker functions — puts both bindings
    on the thread that actually performs the provider call.
    """
    from inqtrix.providers.base import (
        observe_provider_retries,
        provider_cancel_scope,
    )

    def _on_retry(notice: dict[str, Any]) -> None:
        provider_label = str(notice.get("provider") or type(provider).__name__)
        model_label = str(notice.get("model") or "").strip() or "unknown-model"
        attempt = int(notice.get("attempt", 0) or 0)
        max_attempts = int(notice.get("max_attempts", 0) or 0)
        delay = float(notice.get("delay_seconds", 0.0) or 0.0)
        reason = _provider_retry_reason(notice)
        emit_progress(
            s,
            t(
                s,
                "provider_retry_attempt",
                provider=provider_label,
                attempt=attempt,
                max_attempts=max_attempts,
                operation=operation_label,
                model=model_label,
                delay=f"{delay:.1f}",
                reason=reason,
            ),
            severity="warning",
        )
        append_iteration_log(
            s,
            {
                "event": "provider_retry",
                "_provider_retry": True,
                "node": str(s.get("_current_node") or ""),
                "operation": operation_label,
                "provider": provider_label,
                "model": model_label,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_seconds": delay,
                "reason": reason,
                "configured_timeout_seconds": notice.get(
                    "configured_timeout_seconds"
                ),
                "effective_timeout_seconds": notice.get(
                    "effective_timeout_seconds"
                ),
                "transport_timeout_seconds": notice.get(
                    "transport_timeout_seconds"
                ),
            },
            testing_mode=testing_mode,
        )

    cancel_event = s.get("_cancel_event")
    probe = cancel_event.is_set if cancel_event is not None else None
    with provider_cancel_scope(probe):
        with observe_provider_retries(provider, _on_retry) as bound:
            yield bound


_FanoutItem = TypeVar("_FanoutItem")
_FanoutResult = TypeVar("_FanoutResult")

# How often the fan-out coordinator re-checks the run's cancel event while
# waiting for worker futures. Internal wake-up granularity, not a setting.
_FANOUT_CANCEL_POLL_SECONDS: float = 0.5


def _map_cancellable(
    s: dict[str, Any],
    fn: Callable[[_FanoutItem], _FanoutResult],
    items: Sequence[_FanoutItem],
    *,
    max_workers: int,
    operation_label: str,
    testing_mode: bool = False,
) -> list[_FanoutResult]:
    """Run *fn* over *items* on a thread pool, honouring run cancellation.

    Without a ``_cancel_event`` in *s* this is exactly
    ``list(ThreadPoolExecutor.map(fn, items))`` (library mode stays
    byte-identical). With one, the coordinator re-checks the event every
    ``_FANOUT_CANCEL_POLL_SECONDS`` instead of blocking until every item
    returns: queued items are cancelled, the abandonment is made visible
    on all three channels (log, warning progress event, iteration-log
    marker ``cancel_abandoned_work``), the still-running calls are awaited
    (they stop early through the provider cancel probe bound inside the
    worker) and :class:`AgentCancelled` is raised.

    Args:
        s: Agent state; read for ``_cancel_event`` and used for progress
            and iteration-log emission.
        fn: Per-item worker executed on the pool threads.
        items: Materialized work list; results keep this order.
        max_workers: Thread-pool width (callers keep their existing
            provider-derived sizing).
        operation_label: Localized operation name for the progress text.
        testing_mode: Forwarded to :func:`append_iteration_log`.

    Returns:
        Results in submission order.

    Raises:
        AgentCancelled: When the run's cancel event is observed. Worker
            exceptions re-raise like ``ex.map``: the lowest-submission-index
            failure propagates, and queued (not yet started) items are
            cancelled instead of still being executed against a provider
            that already failed fatally.
    """
    cancel_event = s.get("_cancel_event")
    # Pool threads start with EMPTY contextvars; snapshot the submitting
    # thread's context ONCE PER ITEM (a Context cannot be entered
    # concurrently) so the metrics feature label and other ambient
    # context survive the fan-out (scheduler precedent).
    item_contexts = [contextvars.copy_context() for _ in items]
    if cancel_event is None:
        # Keep the literal ex.map construct: its result iterator cancels
        # the queued remainder when an item fails — the submit-and-
        # gather shape would run every queued item to completion first.
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            return list(
                ex.map(
                    lambda ctx, item: ctx.run(fn, item),
                    item_contexts,
                    items,
                )
            )

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures: list[Future[_FanoutResult]] = [
            ex.submit(ctx.run, fn, item)
            for ctx, item in zip(item_contexts, items)
        ]
        pending: set[Future[_FanoutResult]] = set(futures)
        failed = False
        while pending and not failed:
            if cancel_event.is_set():
                abandoned = sum(1 for future in pending if future.cancel())
                in_flight = [
                    future for future in pending if not future.cancelled()
                ]
                node = str(s.get("_current_node") or "")
                log.warning(
                    "Cancel requested during %s fan-out (node=%s): "
                    "abandoned %d of %d calls, waiting for %d in-flight.",
                    operation_label,
                    node,
                    abandoned,
                    len(items),
                    len(in_flight),
                )
                emit_progress(
                    s,
                    t(
                        s,
                        "cancel_fanout_abandoned",
                        abandoned=abandoned,
                        total=len(items),
                        operation=operation_label,
                        in_flight=len(in_flight),
                    ),
                    severity="warning",
                )
                append_iteration_log(
                    s,
                    {
                        "event": "cancel_abandoned_work",
                        "node": node,
                        "operation": operation_label,
                        "abandoned": abandoned,
                        "in_flight": len(in_flight),
                        "total": len(items),
                    },
                    testing_mode=testing_mode,
                )
                # In-flight calls stop at the next provider cancel probe;
                # waiting for them keeps the pool free of orphan threads
                # that would emit into an already-terminal run.
                wait(in_flight)
                raise AgentCancelled(
                    "Run cancelled during parallel provider calls."
                )
            done, pending = wait(
                pending,
                timeout=_FANOUT_CANCEL_POLL_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            failed = any(
                not future.cancelled() and future.exception() is not None
                for future in done
            )
        if failed:
            # ex.map parity: a fatal worker failure abandons the queued
            # remainder instead of still executing it (each queued item
            # would serve its own retry ladder against a provider that
            # already failed), then the lowest-index failure propagates.
            for future in pending:
                future.cancel()
            wait([future for future in pending if not future.cancelled()])
        for future in futures:
            if not future.cancelled():
                future.result()
        return [future.result() for future in futures]


def _repair_answer_markdown_tail(answer: str) -> str:
    """Close obviously unbalanced markdown markers before appending diagnostics."""
    repaired = answer.rstrip()
    if not repaired:
        return repaired
    if repaired.count("```") % 2 == 1:
        repaired += "\n```"
    if repaired.count("**") % 2 == 1:
        repaired += "**"
    return repaired


def _finish_reason_indicates_limit(finish_reason: str) -> bool:
    """Return whether the provider reported a non-standard stop condition."""
    return (finish_reason or "").strip().lower() not in _ALLOWED_FINISH_REASONS


def _collect_truncation_signals(
    answer: str,
    *,
    subject_label: str,
) -> list[str]:
    """Collect content-based truncation signals independent of provider metadata."""
    reasons: list[str] = []
    stripped = answer.rstrip()
    if not stripped:
        reasons.append(f"{subject_label} ist leer")
        return reasons

    if stripped.count("**") % 2 == 1:
        reasons.append(f"Unbalancierte Markdown-Fettschrift am Ende {subject_label.lower()}")
    if stripped.count("```") % 2 == 1:
        reasons.append(f"Offener Markdown-Codeblock am Ende {subject_label.lower()}")

    last_line = stripped.splitlines()[-1].strip()
    plain_last_line = re.sub(r"[*_`#>-]", "", last_line).strip()
    if (
        plain_last_line
        and not last_line.startswith(("#", "- ", "* ", ">", "1. "))
        and len(plain_last_line) < 24
        and plain_last_line[-1].isalnum()
        and not re.search(r"[.!?:;\)\]]$", plain_last_line)
    ):
        reasons.append(
            f"{subject_label} ends in a short fragment without sentence closure"
        )
    return reasons


def _normalize_generated_section(answer: str, heading: str) -> str:
    """Remove a duplicated top-level heading and trim accidental spillover."""
    lines = answer.lstrip().splitlines()
    if lines:
        first_line = re.sub(r"^[#\s]+", "", lines[0]).strip().rstrip(":")
        if first_line.casefold() == heading.casefold():
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]

    kept_lines: list[str] = []
    seen_content = False
    for line in lines:
        if seen_content and line.startswith("## "):
            break
        if line.strip():
            seen_content = True
        kept_lines.append(line)
    return "\n".join(kept_lines).strip()


def _detect_incomplete_section(
    answer: str,
    *,
    finish_reason: str,
) -> tuple[list[str], bool]:
    """Return hard truncation reasons and whether a provider limit was hit."""
    reasons = _collect_truncation_signals(answer, subject_label="section")
    limit_hit = _finish_reason_indicates_limit(finish_reason)
    if limit_hit and reasons:
        reasons.insert(
            0, f"provider stop reason: {(finish_reason or '').strip().lower()}"
        )

    return _dedupe_reasons(reasons), limit_hit


def _join_rendered_sections(sections: list[str]) -> str:
    """Join the rendered sections; the headings carry the structure.

    No horizontal rule between sections: the renderer draws none beneath
    headings either, and a rule between every section is the visual noise
    the answer format forbids the model to write.
    """
    return "\n\n".join(section.strip() for section in sections if section.strip())


def _dedupe_reasons(reasons: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def _compose_answer_sections(
    s: dict,
    *,
    providers: ProviderContext,
    settings: AgentSettings,
    state_data: dict[str, Any],
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> _AnswerCompositionResult:
    """Compose the final answer section by section to avoid hard output truncation.

    Every section is fed a section-scoped slice of the single canonical
    EvidenceLedger overview. Citation labels are stable across sections
    (passed in via ``state_data["evidence_label_by_id"]``), so ``[E12]``
    means the same source everywhere and the running ``used_evidence_labels``
    memory genuinely spreads source coverage across the report.
    """
    completed_headings: list[str] = []
    section_logs: list[dict[str, Any]] = []
    finish_reason = ""
    tuning = settings.report_tuning
    answer_sections = tuple(tuning.answer_sections)
    evidence_limits_owner = evidence_limits_section_index(answer_sections)
    section_count = max(1, len(answer_sections))
    # The answer node is resolved once in ``answer()``; the model + reasoning
    # effort are passed in here and used for every section. The fallback retry
    # passes its own model with an empty effort so a reasoning-induced failure
    # is not repeated on the retry.
    answer_model = model or ""
    answer_effort = reasoning_effort or ""
    # ``section_focus_record_cap`` only controls the size of the soft
    # ``section_focus_labels`` hint in the user prompt. The LLM still sees
    # the FULL evidence overview in the system prompt -- the hint is a
    # relevance suggestion, not a hard filter.
    section_focus_record_cap = max(6, min(20, int(48 / section_count) or 6))
    consecutive_empty = 0
    _MAX_CONSECUTIVE_EMPTY = 2
    composition_aborted = False
    consecutive_empty_at_break = 0
    sections_attempted = 0
    report_so_far_summary = ""
    used_evidence_labels: set[str] = set()
    evidence_ledger = list(s.get("evidence_ledger", []) or [])
    label_by_evidence_id = dict(state_data.get("evidence_label_by_id", {}) or {})
    visible_evidence_labels = set(state_data.get("visible_evidence_labels", []) or [])
    required_aspects = list(state_data.get("required_aspects", []) or [])
    full_allowed_citation_count = len(state_data.get("allowed_citations", []) or [])

    # Write order vs display order: sections flagged ``write_last`` (Executive
    # Summary in DEEP, Kurzfazit in COMPACT) are rendered AFTER the body so
    # they see the completed body in ``report_so_far_summary`` and
    # ``used_evidence_labels`` and can synthesise it. They still appear at
    # their declared position in the final answer.
    write_plan: list[tuple[int, AnswerSectionSpec]] = [
        (display_index, section_spec)
        for display_index, section_spec in enumerate(answer_sections, 1)
        if not section_spec.write_last
    ] + [
        (display_index, section_spec)
        for display_index, section_spec in enumerate(answer_sections, 1)
        if section_spec.write_last
    ]
    rendered_by_display_index: dict[int, str] = {}

    heading_language = s.get("language")
    for index, section_spec in write_plan:
        check_cancel_event(s)
        sections_attempted += 1
        section_heading = section_spec.heading(heading_language)
        # Counted in WRITE order: a write_last section is written (and so
        # announced) after the body, so its display slot is not its
        # position — "2/2" before "1/2" read as counting backwards. The
        # snapshot carries the position typed, so no reader parses text.
        s["_writing_section"] = (sections_attempted, len(answer_sections))
        emit_progress(
            s,
            t(
                s,
                "answer_section_start",
                index=sections_attempted,
                total=len(answer_sections),
                heading=section_heading,
            ),
        )
        provider_output_budget = _llm_default_output_tokens(providers.llm)
        focus_records = select_section_evidence_records(
            evidence_ledger,
            section_key=section_spec.key,
            required_aspects=required_aspects,
            used_labels=used_evidence_labels,
            label_by_evidence_id=label_by_evidence_id,
            max_records=section_focus_record_cap,
        )
        section_focus_labels = sorted(
            {
                label_by_evidence_id[str(record.get("evidence_id", "") or "")]
                for record in focus_records
                if str(record.get("evidence_id", "") or "") in label_by_evidence_id
                and label_by_evidence_id[str(record.get("evidence_id", "") or "")]
                in visible_evidence_labels
            }
        )
        # Each section sees the FULL EvidenceLedger overview via the shared
        # ``state_data`` -- no scoping. The selection above is converted into
        # a soft ``section_focus_labels`` hint in the user prompt so the LLM
        # knows which records the section heuristic considered most relevant,
        # but the LLM remains free to cite any source from the full overview.
        section_system = build_answer_section_system_prompt(
            state_data,
            heading=section_heading,
            instruction=section_spec.prompt_instruction,
            length_guidance=section_spec.length_guidance,
            section_position=index,
            section_total=len(answer_sections),
            owns_evidence_limits=index == evidence_limits_owner,
        )
        section_prompt = build_answer_section_user_prompt(
            s["question"],
            heading=section_heading,
            instruction=section_spec.prompt_instruction,
            completed_headings=completed_headings,
            report_so_far_summary=report_so_far_summary,
            used_evidence_labels=sorted(used_evidence_labels),
            section_focus_labels=section_focus_labels,
            synthesizing_existing=bool(section_spec.write_last),
        )
        capacity_details = _check_llm_context_capacity(
            s,
            settings,
            providers=providers,
            node="answer",
            phase=f"answer_section:{section_spec.key}",
            model=answer_model,
            system=section_system,
            prompt=section_prompt,
            requested_output_tokens=provider_output_budget,
        )
        with _provider_retry_progress_context(
            providers.llm,
            s,
            operation_label=(
                f"{t(s, 'retry_operation_answer_synthesis')}: "
                f"{section_heading}"
            ),
            testing_mode=settings.testing_mode,
        ):
            response = _llm_complete_with_metadata(
                providers.llm,
                section_prompt,
                system=section_system,
                deadline=s["deadline"],
                state=s,
                model=answer_model,
                reasoning_effort=answer_effort,
                max_output_tokens=None,
                timeout=settings.reasoning_timeout,
            )
        finish_reason = str(getattr(response, "finish_reason", "") or "")
        section_body = _normalize_generated_section(response.content, section_heading)
        section_body = _repair_answer_markdown_tail(section_body)
        section_reasons, limit_hit = _detect_incomplete_section(
            section_body,
            finish_reason=finish_reason,
        )
        section_prompt_tokens = int(getattr(response, "prompt_tokens", 0) or 0)
        section_completion_tokens = int(getattr(response, "completion_tokens", 0) or 0)
        # Provider-reported effective max_tokens (post-clamp). Falls back to
        # the provider default because report profiles do not own token
        # budgets.
        section_request_max_tokens = (
            int(getattr(response, "request_max_tokens", 0) or 0)
            or provider_output_budget
        )
        # Utilization against the budget that was actually applied.
        section_token_utilization = (
            round(section_completion_tokens / section_request_max_tokens, 3)
            if section_request_max_tokens
            else 0.0
        )
        # Reflect the per-call reasoning effort actually routed to this section,
        # not the provider's global thinking flag: a graded effort turns thinking
        # on, "none" forces it off, and "" inherits the provider default.
        if answer_effort == "none":
            thinking_likely_active = False
        elif answer_effort:
            thinking_likely_active = True
        else:
            thinking_likely_active = bool(getattr(providers.llm, "_thinking", None))
        visible_tokens_estimate = max(0, round(len(section_body) / 4))
        if thinking_likely_active:
            thinking_tokens_estimate = max(
                0, section_completion_tokens - visible_tokens_estimate
            )
        else:
            thinking_tokens_estimate = 0
        section_log_entry: dict[str, Any] = {
            "key": section_spec.key,
            "heading": section_heading,
            "position": index,
            "request_max_tokens": section_request_max_tokens,
            "required": section_spec.required,
            "model": answer_model,
            "content_length": len(section_body),
            "content_preview": section_body[:240],
            "finish_reason": finish_reason,
            "limit_hit": limit_hit,
            "incomplete": bool(section_reasons),
            "incomplete_reasons": section_reasons,
            "accepted_with_limit": bool(limit_hit and not section_reasons),
            "prompt_tokens": section_prompt_tokens,
            "completion_tokens": section_completion_tokens,
            "thinking_likely_active": thinking_likely_active,
            "visible_tokens_estimate": visible_tokens_estimate,
            "token_utilization": section_token_utilization,
            "system_prompt_chars": len(section_system),
            "user_prompt_chars": len(section_prompt),
            "section_focus_record_count": len(focus_records),
            "section_focus_labels": section_focus_labels[:20],
            "section_allowed_citation_count": full_allowed_citation_count,
            "section_scoped_evidence": True,
            "used_evidence_labels": sorted(used_evidence_labels)[:30],
            "estimated_input_tokens": capacity_details.get("estimated_input_tokens", 0),
            "requested_output_tokens": provider_output_budget,
            "estimated_required_context_tokens": capacity_details.get(
                "estimated_required_context_tokens", 0
            ),
            "context_window_tokens": capacity_details.get("context_window_tokens"),
            "required_context_window_tokens": capacity_details.get(
                "required_context_window_tokens", 0
            ),
        }
        if thinking_likely_active:
            section_log_entry["thinking_tokens_estimate"] = thinking_tokens_estimate
        _append_forensic_event(
            s,
            settings,
            event="answer_section",
            node="answer",
            payload=section_log_entry,
        )
        section_logs.append(section_log_entry)
        if limit_hit:
            emit_progress(
                s,
                t(
                    s,
                    "compose_section_token_limit",
                    heading=section_heading,
                    finish_reason=finish_reason,
                    request_max=section_request_max_tokens,
                    completion=section_completion_tokens,
                ),
            )
            log.warning(
                "TRACE section %d/%d: token limit hit "
                "(finish_reason=%s, request_max_tokens=%d, "
                "completion_tokens=%d, content_length=%d)",
                index,
                len(answer_sections),
                finish_reason,
                section_request_max_tokens,
                section_completion_tokens,
                len(section_body),
            )
        if section_reasons:
            emit_progress(
                s,
                t(
                    s,
                    "compose_section_truncation",
                    heading=section_heading,
                    reasons=", ".join(section_reasons),
                ),
            )
            log.warning(
                "TRACE section %d/%d: truncation signals detected "
                "(reasons=%s, finish_reason=%s)",
                index,
                len(answer_sections),
                section_reasons,
                finish_reason,
            )
        if section_body:
            rendered_by_display_index[index] = (
                f"## {section_heading}\n\n{section_body}"
            )
            completed_headings.append(section_heading)
            used_evidence_labels.update(_extract_evidence_labels(section_body))
            section_summary = _compact_section_summary(
                section_heading,
                section_body,
            )
            if section_summary:
                report_so_far_summary = (
                    f"{report_so_far_summary}\n{section_summary}".strip()
                )
                if len(report_so_far_summary) > 2400:
                    report_so_far_summary = "..." + report_so_far_summary[-2400:]
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY:
                composition_aborted = True
                consecutive_empty_at_break = consecutive_empty
                emit_progress(
                    s,
                    t(
                        s,
                        "compose_aborted",
                        n=consecutive_empty,
                        heading=section_heading,
                    ),
                )
                log.warning(
                    "TRACE compose: aborting after %d consecutive empty sections "
                    "(position=%d/%d, finish_reason=%s)",
                    consecutive_empty,
                    index,
                    len(answer_sections),
                    finish_reason or "unknown",
                )
                break
    # The writer is done: later snapshots name no section any more.
    s.pop("_writing_section", None)

    # Assemble in DISPLAY order regardless of write order so the final
    # answer reads as planned: Executive Summary / Kurzfazit at the top
    # even though they were written last.
    rendered_sections = [
        rendered_by_display_index[idx]
        for idx in sorted(rendered_by_display_index)
    ]
    answer_text = _join_rendered_sections(rendered_sections)

    return _AnswerCompositionResult(
        # The same currency guard as the agent answers: a bare "$5" in
        # prose must not become inline math in the renderer.
        answer=normalize_agent_markdown(answer_text.strip()),
        finish_reason=finish_reason,
        section_logs=section_logs,
        composition_aborted=composition_aborted,
        consecutive_empty_at_break=consecutive_empty_at_break,
        sections_planned=len(answer_sections),
        sections_attempted=sections_attempted,
    )


def _missing_terminal_sections(answer: str, profile: ReportProfile) -> list[str]:
    """Return expected closing sections that are absent from the answer."""
    if profile is ReportProfile.DEEP:
        expected = (
            "## Perspektiven / Positionen",
            "## Risiken / Unsicherheiten",
            "## Fazit / Ausblick",
        )
    else:
        expected = ("## Einordnung / Ausblick",)
    return [section for section in expected if section not in answer]


def _detect_incomplete_answer(
    answer: str,
    *,
    finish_reason: str,
    report_profile: ReportProfile,
) -> list[str]:
    """Return diagnostic reasons when the answer appears truncated."""
    reasons = _collect_truncation_signals(answer, subject_label="answer")
    if _finish_reason_indicates_limit(finish_reason) and reasons:
        reasons.insert(
            0, f"provider stop reason: {(finish_reason or '').strip().lower()}"
        )

    if reasons:
        missing_sections = _missing_terminal_sections(answer, report_profile)
        if missing_sections:
            reasons.append(
                "Fehlende Abschlussabschnitte: "
                + ", ".join(missing_sections)
            )

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def _answer_text(language: str | None, key: str, **kwargs: Any) -> str:
    """A user-visible answer text (appendix, notice, footer) in the run's language."""
    return t({"language": language or ""}, key, **kwargs)


def _extract_used_reference_links(
    answer: str,
    allowed_urls: set[str],
    *,
    language: str | None,
) -> list[tuple[str, str]]:
    """Extract allowed markdown links from the answer in first-seen order."""
    references: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for match in _MARKDOWN_LINK_RE.finditer(answer):
        label = match.group(1).strip() or _answer_text(language, "answer_reference_label")
        url = normalize_url(match.group(2))
        if url not in allowed_urls or url in seen_urls:
            continue
        seen_urls.add(url)
        references.append((label, url))
    return references


def _format_reference_entries(
    entries: list[tuple[str, str]],
    *,
    heading: str,
    intro: str = "",
) -> str:
    """Render one deterministic markdown list of labelled source links."""
    lines = [heading] + ([intro] if intro else [])
    for label, url in entries:
        domain = domain_from_url(url)
        suffix = f" — {domain}" if domain else ""
        lines.append(f"- [{label}]({url}){suffix}")
    return "\n".join(lines)


def _format_report_reference_records(
    entries: list[tuple[str, str]],
    strategies: StrategyContext,
    *,
    tier_by_url: dict[str, str] | None = None,
    title_by_url: dict[str, str] | None = None,
    cited_urls: set[str] | None = None,
    placement_by_url: dict[str, dict[str, str]] | None = None,
    statements_by_url: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Return the structured twin of the rendered reference appendix.

    A record carries the page title the ledger recorded for its URL —
    every consumer that hands the references on (the Agent Desk's child
    report, the reference rail) shows the title, and a record without
    one would fall back to the bare URL although the provider delivered
    the title with the citation. ``cited`` says whether the
    answer body cites the source (``## Referenzen``) or only saw it
    (``## Weitere geprüfte Quellen``); ``None`` marks every entry cited.
    """
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    known_tiers = tier_by_url or {}
    known_titles = title_by_url or {}
    for label, raw_url in entries:
        url = normalize_url(raw_url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        record: dict[str, Any] = {
            "label": str(label or "Quelle"),
            "url": url,
            "tier": known_tiers.get(url) or strategies.source_tiering.tier_for_url(url),
            "cited": True if cited_urls is None else url in cited_urls,
        }
        title = known_titles.get(url, "")
        if title:
            record["title"] = title
        placement = (placement_by_url or {}).get(url, {})
        if placement.get("tier_reason"):
            # How the tier came about (structural floor, extractor judgement,
            # unjudged) and the extractor's evidence for a judged role.
            record["tier_reason"] = placement["tier_reason"]
            if placement.get("role_reason"):
                record["role_reason"] = placement["role_reason"]
        statements = (statements_by_url or {}).get(url)
        if statements:
            record["statements"] = [dict(item) for item in statements]
        records.append(record)
    return records


class _LedgerReferenceFacts(NamedTuple):
    """What the evidence ledger knows per normalized URL."""

    tier_by_url: dict[str, str]
    title_by_url: dict[str, str]
    placement_by_url: dict[str, dict[str, str]]
    """URL -> the placement fields (``tier``, ``tier_reason``, ``role_reason``)."""
    statements_by_url: dict[str, list[dict[str, Any]]]
    """URL -> every statement the evidence layer ties to the source, in
    ledger order, once each: ``text``, ``status`` and ``checked`` (this
    source's own text was checked and states it)."""


def _reference_facts_from_evidence_ledger(
    evidence_ledger: list[dict[str, Any]],
) -> _LedgerReferenceFacts:
    """Map normalized evidence URLs to their ledger source tier and title.

    One walk over the records: the record's own tier/title cover its
    canonical URL, every citation in its ``citation_set`` covers the
    cited URL (citations carry the provider's page title). First
    occurrence wins, as before for tiers.
    """
    tiers: dict[str, str] = {}
    titles: dict[str, str] = {}
    placements: dict[str, dict[str, str]] = {}
    statements: dict[str, list[dict[str, Any]]] = {}
    for record in evidence_ledger:
        tier = str(record.get("tier", "") or "")
        record_title = str(record.get("title", "") or "")
        canonical_url = normalize_url(record.get("canonical_url", ""))
        if canonical_url:
            # What the ledger attributes to this source, from every search
            # that cited it: the reader of a reference can see each statement
            # the report could rest on, not only the first citation's context.
            listed = statements.setdefault(canonical_url, [])
            for claim in record.get("claims", []) or []:
                text = " ".join(str(claim.get("claim_text", "") or "").split())
                if not text or any(item["text"] == text for item in listed):
                    continue
                listed.append({
                    "text": text,
                    "status": str(claim.get("verification_status", "") or "unverified"),
                    "checked": bool(claim.get("checked_here")),
                })
            if tier:
                tiers.setdefault(canonical_url, tier)
                placements.setdefault(
                    canonical_url,
                    {field: str(record.get(field, "") or "") for field in PLACEMENT_FIELDS},
                )
            if record_title:
                titles.setdefault(canonical_url, record_title)
        for citation in record.get("citation_set", []) or []:
            if not isinstance(citation, dict):
                continue
            citation_url = normalize_url(citation.get("url", ""))
            if not citation_url:
                continue
            if tier:
                tiers.setdefault(citation_url, tier)
            citation_title = str(citation.get("title", "") or "")
            if citation_title:
                titles.setdefault(citation_url, citation_title)
    return _LedgerReferenceFacts(
        tier_by_url=tiers,
        title_by_url=titles,
        placement_by_url=placements,
        statements_by_url={url: items for url, items in statements.items() if items},
    )


def _append_uncited_allowed_references(
    entries: list[tuple[str, str]],
    *,
    allowed_citations: list[str],
    label_urls: dict[str, str],
) -> list[tuple[str, str]]:
    """Append every rendered online evidence source missing from references."""
    merged = list(entries)
    seen_urls = {normalize_url(url) for _, url in merged if normalize_url(url)}
    label_by_url: dict[str, str] = {}
    for label, raw_url in sorted(
        label_urls.items(),
        key=lambda item: int(item[0][1:]) if re.fullmatch(r"E\d+", item[0]) else 10**9,
    ):
        url = normalize_url(raw_url)
        if url:
            label_by_url.setdefault(url, label)

    for index, raw_url in enumerate(allowed_citations, 1):
        url = normalize_url(str(raw_url))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append((label_by_url.get(url, f"Quelle {index}"), url))
    return merged


def _select_additional_links(
    citations: list[str],
    *,
    excluded_urls: set[str],
    prompt_citation_urls: set[str],
    strategies: StrategyContext,
    limit: int = _ADDITIONAL_LINK_LIMIT,
) -> list[str]:
    """Select a curated set of additional links not used in the report body.

    Note:
        This is intentionally separate from
        :meth:`ClaimConsolidationStrategy.select_answer_citations`. The
        latter ranks by claim score for in-prompt citations; this helper
        ranks raw citations by source tier with strict domain diversity for
        the post-answer "Weiterfuehrende Links" section. They share the
        per-domain diversity idea but produce different orderings on
        purpose — keep them aligned by behaviour, not by code path.
    """
    ranked_candidates: list[tuple[int, int, int, str, str]] = []
    seen_urls: set[str] = set()

    for index, raw_url in enumerate(citations):
        normalized = normalize_url(str(raw_url))
        if not normalized or normalized in excluded_urls or normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        tier = strategies.source_tiering.tier_for_url(normalized)
        if tier == "low":
            continue
        ranked_candidates.append(
            (
                _SOURCE_TIER_SORT_ORDER.get(tier, _SOURCE_TIER_SORT_ORDER["unknown"]),
                0 if normalized in prompt_citation_urls else 1,
                index,
                domain_from_url(normalized),
                normalized,
            )
        )

    ranked_candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    selected: list[str] = []
    deferred: list[tuple[int, int, int, str, str]] = []
    seen_domains: set[str] = set()

    for candidate in ranked_candidates:
        _, _, _, domain, url = candidate
        if domain and domain in seen_domains:
            deferred.append(candidate)
            continue
        selected.append(url)
        if domain:
            seen_domains.add(domain)
        if len(selected) >= limit:
            return selected

    for candidate in deferred:
        selected.append(candidate[4])
        if len(selected) >= limit:
            break

    return selected


def _format_additional_links(urls: list[str], strategies: StrategyContext) -> str:
    """Render a deterministic markdown section for curated extra links."""
    lines = ["## Weiterfuehrende Links"]
    for url in urls:
        domain = domain_from_url(url) or url
        tier = strategies.source_tiering.tier_for_url(url)
        lines.append(f"- [{domain}]({url}) — Tier: {tier}")
    return "\n".join(lines)


def _evidence_omission_note(
    omitted_by_query: list[dict[str, Any]], *, language: str | None
) -> str:
    """The reader's sentence for a budget cut: how many sources of which
    searches the report did not evaluate. Empty when nothing was cut."""
    lost = [
        entry
        for entry in omitted_by_query
        if int(entry.get("omitted", 0) or 0) > 0
    ]
    if not lost:
        return ""
    total = sum(int(entry.get("omitted", 0) or 0) for entry in lost)
    queries = ", ".join(
        f"„{str(entry.get('query') or entry.get('query_id') or '').strip()}“"
        for entry in lost
    )
    return _answer_text(
        language,
        "answer_omission_note",
        cut=evidence_cut_words(total, len(lost), language=language),
        queries=queries,
    )


def _build_answer_appendix_sections(
    answer: str,
    *,
    allowed_citations: list[str],
    label_urls: dict[str, str] | None = None,
    strategies: StrategyContext,
    incomplete_reasons: list[str],
    finish_reason: str,
    answer_contract: str = "general",
    tier_by_url: dict[str, str] | None = None,
    title_by_url: dict[str, str] | None = None,
    omitted_by_query: list[dict[str, Any]] | None = None,
    placement_by_url: dict[str, dict[str, str]] | None = None,
    statements_by_url: dict[str, list[dict[str, Any]]] | None = None,
    language: str | None,
) -> AnswerAppendixSections:
    """Build optional post-answer sections without affecting the stats footer.

    ``language`` is the run's answer language: every heading and note of
    the appendix is user-visible answer text and reads in that language.

    ``allowed_citations`` is the EvidenceOverview-derived citation allowlist
    (the union of visible source-block URLs). References are the links the
    answer body actually used; additional links are allowlist sources the
    body left uncited. ``omitted_by_query`` (the overview's budget cut per
    search) becomes a completeness note that names how many sources of
    which searches the report did not evaluate — a reader must never take
    a budget cut for "nothing found".
    """
    sections: list[str] = []

    allowed_citation_urls = {
        normalize_url(url)
        for url in allowed_citations
        if normalize_url(url)
    }

    completeness_lines: list[str] = []
    if incomplete_reasons:
        completeness_lines.append(_answer_text(language, "answer_completeness_status"))
        if finish_reason:
            completeness_lines.append(
                _answer_text(language, "answer_completeness_provider_stop", finish_reason=finish_reason)
            )
        completeness_lines.append(
            _answer_text(language, "answer_completeness_diagnosis", reasons="; ".join(incomplete_reasons))
        )
        completeness_lines.append(_answer_text(language, "answer_completeness_hint"))
    omission_line = _evidence_omission_note(omitted_by_query or [], language=language)
    if omission_line:
        completeness_lines.append(omission_line)
    if completeness_lines:
        sections.append(
            "\n".join([_answer_text(language, "answer_completeness_heading"), *completeness_lines])
        )

    # Only what the body cites is a reference; every other visible source
    # is listed as examined. An incomplete answer that cites nothing has no
    # references — promoting the whole allowlist would overstate the
    # evidence base exactly where the report is weakest. A reference list
    # that mixes examined with cited sources reads as evidence the text
    # never used.
    cited_entries = _extract_used_reference_links(
        answer, allowed_citation_urls, language=language
    )
    cited_count = len(cited_entries)
    all_entries = _append_uncited_allowed_references(
        cited_entries,
        allowed_citations=allowed_citations,
        label_urls=label_urls or {},
    )
    uncited_entries = all_entries[cited_count:]

    reference_records = _format_report_reference_records(
        all_entries,
        strategies,
        tier_by_url=tier_by_url,
        title_by_url=title_by_url,
        cited_urls={normalize_url(url) for _, url in cited_entries},
        placement_by_url=placement_by_url,
        statements_by_url=statements_by_url,
    )
    reference_urls: set[str] = {record["url"] for record in reference_records}

    references_heading = _answer_text(language, "answer_references_heading")
    if cited_entries:
        sections.append(_format_reference_entries(cited_entries, heading=references_heading))
    else:
        sections.append(
            f"{references_heading}\n{_answer_text(language, 'answer_references_none')}"
        )
    if uncited_entries:
        sections.append(
            _format_reference_entries(
                uncited_entries,
                heading=_answer_text(language, "answer_examined_heading"),
                intro=_answer_text(language, "answer_examined_intro"),
            )
        )

    additional_links: list[str] = []
    if answer_contract not in {"claim_check", "data_extraction"}:
        additional_links = _select_additional_links(
            allowed_citations,
            excluded_urls=reference_urls,
            prompt_citation_urls=allowed_citation_urls,
            strategies=strategies,
        )
        if additional_links:
            sections.append(_format_additional_links(additional_links, strategies))

    return AnswerAppendixSections(
        sections=sections,
        references=reference_records,
        additional_links=additional_links,
    )


def _answer_segments(answer: str) -> list[str]:
    """Split answer text into compact binding segments."""
    segments = [
        segment.strip()
        for segment in re.split(r"(?<=[.!?])\s+|\n{2,}", answer or "")
        if segment.strip()
    ]
    return segments or ([answer.strip()] if (answer or "").strip() else [])


def _claim_plausible_in_segment(
    claim: dict[str, Any],
    segment_tokens: set[str],
    normalized_segment: str,
) -> bool:
    """Heuristic: does the segment plausibly carry this consolidated claim?

    Uses the same primitives as :class:`ClaimConsolidationStrategy`: the
    consolidated claim signature is normalised text, and ``tokenize``
    produces lowercase word-tokens. A binding is plausible when either
    (a) the claim signature appears as a substring of the lowercased
    segment, or (b) the claim's content tokens overlap with the
    segment's content tokens by at least three non-stopword,
    non-negation, length>=3 tokens. The thresholds are intentionally
    permissive enough that paraphrased re-uses of the claim still
    match, but strict enough that a citation that merely shares its
    URL with multiple unrelated claims does not promote all of them
    to ``"matched"``.
    """
    signature = str(claim.get("signature", "")).strip().lower()
    if signature and signature in normalized_segment:
        return True
    claim_tokens = {
        t for t in tokenize(str(claim.get("claim_text", "")))
        if len(t) >= 3 and t not in STOPWORDS and t not in NEGATION_TOKENS
    }
    if not claim_tokens:
        return False
    overlap = claim_tokens & segment_tokens
    return len(overlap) >= 3


def _build_answer_claim_bindings(
    answer_body: str,
    *,
    consolidated_claims: list[dict[str, Any]],
    allowed_citations: list[str],
    provider_citation_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map final answer-body links to the claims and sources they support.

    The function scans only ``answer_body`` (the prose produced by the
    LLM, captured **before** any appendix sections such as references
    or further-reading lists were appended). Each markdown link inside
    a body segment is matched against the consolidated claim ledger
    using :func:`_claim_plausible_in_segment`. A claim that shares a
    URL with the citation only becomes ``binding_status="matched"``
    when the segment plausibly carries it; otherwise the row degrades
    to ``"source_only_binding"`` so the URL-claim relationship is
    visible without falsely promoting unrelated claims to ``matched``.
    Citations without any consolidated claim continue to emit
    ``"citation_without_claim"``.

    The optional ``provider_citation_records`` argument lets each
    binding carry the per-query ``citation_id`` for the URL, so log
    consumers can join an answer-side binding straight to the
    forensic ``provider_citation_record`` event without re-deriving
    the citation identity from the URL.
    """
    citation_urls = {normalize_url(url) for url in allowed_citations if normalize_url(url)}

    citation_ids_by_url: dict[str, list[str]] = {}
    for rec in provider_citation_records:
        canonical = str(rec.get("canonical_url", "")).strip()
        if not canonical:
            canonical = normalize_url(str(rec.get("url", "")))
        cid = str(rec.get("citation_id", "")).strip()
        if canonical and cid:
            citation_ids_by_url.setdefault(canonical, []).append(cid)

    claims_by_url: dict[str, list[dict[str, Any]]] = {}
    for claim in consolidated_claims:
        for url in claim.get("source_urls", []) or []:
            canonical = normalize_url(str(url))
            if canonical:
                claims_by_url.setdefault(canonical, []).append(claim)

    bindings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for segment_index, segment in enumerate(_answer_segments(answer_body), start=1):
        normalized_segment = segment.lower()
        segment_preview = shorten_visibly(segment, 500, marker=LOG_CUT_MARKER)
        segment_tokens = {
            t for t in tokenize(segment)
            if len(t) >= 3 and t not in STOPWORDS and t not in NEGATION_TOKENS
        }
        for match in _MARKDOWN_LINK_RE.finditer(segment):
            canonical = normalize_url(match.group(2))
            if not canonical or canonical not in citation_urls:
                continue
            source_id = make_record_id("src", canonical)
            citation_id = (citation_ids_by_url.get(canonical) or [""])[0]
            related_claims = claims_by_url.get(canonical, [])

            if not related_claims:
                binding_key = (str(segment_index), canonical, "")
                if binding_key in seen:
                    continue
                seen.add(binding_key)
                bindings.append(
                    {
                        "binding_id": make_record_id(
                            "bind",
                            segment_index,
                            canonical,
                            "unmatched",
                        ),
                        "answer_segment_id": f"segment_{segment_index}",
                        "answer_segment_preview": segment_preview,
                        "citation_url": canonical,
                        "source_id": source_id,
                        "citation_id": citation_id,
                        "claim_id": "",
                        "claim_status": "unmatched",
                        "binding_status": "citation_without_claim",
                    }
                )
                continue

            matched_any = False
            for claim in related_claims:
                claim_id = str(claim.get("claim_id", ""))
                binding_key = (str(segment_index), canonical, claim_id)
                if binding_key in seen:
                    continue
                if not _claim_plausible_in_segment(claim, segment_tokens, normalized_segment):
                    continue
                seen.add(binding_key)
                matched_any = True
                bindings.append(
                    {
                        "binding_id": make_record_id(
                            "bind",
                            segment_index,
                            canonical,
                            claim_id,
                        ),
                        "answer_segment_id": f"segment_{segment_index}",
                        "answer_segment_preview": segment_preview,
                        "citation_url": canonical,
                        "source_id": source_id,
                        "citation_id": citation_id,
                        "claim_id": claim_id,
                        "claim_status": str(claim.get("status", "unverified")),
                        "binding_status": "matched",
                    }
                )

            if not matched_any:
                binding_key = (str(segment_index), canonical, "source_only")
                if binding_key in seen:
                    continue
                seen.add(binding_key)
                bindings.append(
                    {
                        "binding_id": make_record_id(
                            "bind",
                            segment_index,
                            canonical,
                            "source_only",
                        ),
                        "answer_segment_id": f"segment_{segment_index}",
                        "answer_segment_preview": segment_preview,
                        "citation_url": canonical,
                        "source_id": source_id,
                        "citation_id": citation_id,
                        "claim_id": "",
                        "claim_status": "source_only",
                        "binding_status": "source_only_binding",
                    }
                )
    return bindings


# ======================================================================= #
# 1. classify
# ======================================================================= #


def _effective_web_recency(s: dict[str, Any], inferred: str) -> str:
    """Resolve explicit delegated recency ahead of classifier inference."""
    return str(s.get("_web_recency") or inferred)


def _event_mode(s: dict[str, Any]) -> bool:
    """Whether the run asks about events — the classifier's own decision
    (answer form ``news_briefing`` or query type ``news``). Only then do
    competing events exist: the evaluator's COMPETING_EVENTS line, the plan
    and answer blocks and the confidence cap. The recency window is not a
    proxy: a delegated comparison child may carry ``week`` from its parent
    and the classifier's default ``month`` says nothing about events."""
    return (
        str(s.get("answer_contract", "") or "") == "news_briefing"
        or str(s.get("query_type", "") or "") == "news"
    )



def _set_answer_language(s: dict, *, classified: str | None, stage: str) -> None:
    """Resolve and record the answer language of the run.

    Order: classifier code, then the text heuristic; without either signal
    the run keeps the heuristic seed of :func:`initial_state` for its
    progress texts, records ``language_source = "unresolved"`` and warns.
    The answer prompt then carries the language-neutral directive instead
    of a guessed language.
    """
    resolved = resolve_answer_language(s.get("question", ""), classified=classified)
    s["language_source"] = resolved.source
    if resolved.code:
        s["language"] = resolved.code
        return
    log.warning(
        "Answer language unresolved at %s (classifier=%r, no text signal); "
        "progress texts keep %r, the answer directive stays language-neutral",
        stage,
        classified,
        s.get("language"),
    )


def _resolve_search_language(s: dict, requested: str | None) -> str:
    """The language the search queries are written in.

    The classifier's ``SEARCH_LANGUAGE`` wins; otherwise the answer language.
    When neither is known the queries are written in English, visibly.
    """
    code = (requested or "").strip().lower()[:2]
    if code:
        return code
    if s.get("language_source") != "unresolved" and s.get("language"):
        return str(s["language"])
    log.warning(
        "Search language unresolved (no SEARCH_LANGUAGE, answer language %r); "
        "search queries are written in English",
        s.get("language_source"),
    )
    return "en"


def classify(
    s: dict,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict:
    """Analyse the incoming question and seed the initial research state.

    Args:
        s: Mutable AgentState-compatible dict. Reads the question,
            deadline, and history; writes language, query type, risk flags,
            and aspect hints.
        providers: Active LLM and search providers.
        strategies: Runtime strategies for risk scoring and downstream
            claim/context handling.
        settings: Agent behavior settings used for risk scoring and
            timeout handling.

    Returns:
        The mutated state dict with classification results.

    Raises:
        AgentRateLimited: Propagated when the upstream classification
            model hard-fails on rate limiting.

    Example:
        >>> classify(state, providers=providers, strategies=strategies, settings=settings)
        {'query_type': 'general', 'language': 'de', ...}
    """
    check_cancel_event(s)
    emit_progress(s, t(s, "classify_start"))
    _t0 = time.monotonic()
    _classify_fallback: dict[str, Any] = {}

    # Drain effort/model-incompatibility warnings collected by the
    # provider at construction time. Each warning is shown to the user via
    # progress feed AND mirrored to log so it's not silently buried in the
    # provider's __init__ log line that runs once before any agent activity.
    _effort_warnings_consumer = getattr(
        providers.llm, "consume_effort_config_warnings", None,
    )
    if callable(_effort_warnings_consumer):
        for _warning in _effort_warnings_consumer() or []:
            emit_progress(s, t(s, "classify_warning_hint", warning=_warning), severity="warning")
            log.warning("CONFIG: %s", _warning)
    s["risk_score"] = strategies.risk_scoring.score(s["question"])
    s["high_risk"] = s["risk_score"] >= settings.high_risk_score_threshold
    classify_model, classify_effort = _resolve_node_llm(s, settings, providers, "classify")

    try:
        _check_deadline(s["deadline"])
        with _provider_retry_progress_context(
            providers.llm,
            s,
            operation_label=t(s, "retry_operation_classify"),
            testing_mode=settings.testing_mode,
        ):
            d = providers.llm.complete(
                f"{date_line(today())}\n\n"
                f"Analyse this question in TWO parts:\n\n"
                f"=== PART 1: CLASSIFICATION ===\n"
                f"1. Does it need a current web search? "
                f"(current events, prices, statistics, new technologies, "
                f"changeable facts → ALWAYS search)\n"
                f"2. In which language is the question written?\n"
                f"3. In which language are the best search results found? "
                f"(e.g. programming/tech/science → often English, "
                f"local topics/politics/law → the language of the question)\n"
                f"4. How current must the results be?\n"
                f"   - NONE: timeless facts (mathematics, history, definitions)\n"
                f"   - MONTH: current developments, latest versions\n"
                f"   - WEEK: news of the last days, current events\n"
                f"   - DAY: breaking news, live data, today's events\n"
                f"   - HOUR: real-time events, breaking news happening right now\n"
                f"5. Which search type fits best?\n"
                f"   - GENERAL: standard web search\n"
                f"   - ACADEMIC: scientific questions, studies, papers\n"
                f"   - NEWS: news, current events, reports\n"
                f"6. Which ANSWER FORM does the question demand?\n"
                f"   - GENERAL: explanation, overview, assessment\n"
                f"   - COMPARISON: two or more objects are set against each other by criteria\n"
                f"   - CLAIM_CHECK: ONLY with an explicit intent to check ('Is it true that ...', "
                f"'Is it correct that ...')\n"
                f"   - DATA_EXTRACTION: concrete figures, values, time series\n"
                f"   - NEWS_BRIEFING: what happened recently (the news of a period)\n"
                f"   Rules: a request for evidence, sources or citations is a work instruction, "
                f"not a CLAIM_CHECK. For a work order (task/objective/expected output) "
                f"classify the factual question behind it.\n\n"
                f"=== PART 2: DECOMPOSITION ===\n"
                f"Break the question into 1-4 substantive sub-questions for more targeted research. "
                f"Every sub-question names subject, object/region and period and is a factual question, "
                f"never a method (no 'check evidence', no 'compare sources'). "
                f"For COMPARISON: one sub-question per object plus the comparison itself.\n"
                f"Write the sub-questions in the language of the question.\n"
                f"When the question is simple enough, return it unchanged as the single sub-question.\n\n"
                f"TEMPORAL ANCHORING:\n"
                f"- Interpret relative time references (recently, lately, the other day) "
                f"always relative to today's date ({today()}).\n"
                f"- 'recently' = the last 2-4 weeks before today's date.\n"
                f"- ADD NO concrete years that you do not know from the question.\n"
                f"- Instead of guessing '2025' or '2026', use 'recent' or the date.\n\n"
                f"Question: {s['question']}\n\n"
                f"Reply EXACTLY in this format:\n"
                f"DECISION: SEARCH or DIRECT\n"
                f"LANGUAGE: language code of the question (e.g. de, en, fr)\n"
                f"SEARCH_LANGUAGE: language code for the best search (e.g. en, de)\n"
                f"RECENCY: NONE or HOUR or DAY or WEEK or MONTH\n"
                f"TYPE: GENERAL or ACADEMIC or NEWS\n"
                f"ANSWER_CONTRACT: GENERAL or COMPARISON or CLAIM_CHECK or DATA_EXTRACTION or NEWS_BRIEFING\n"
                f"SUB_QUESTIONS: JSON array of 1-4 sub-questions as strings",
                deadline=s["deadline"],
                model=classify_model,
                reasoning_effort=classify_effort,
                timeout=settings.reasoning_timeout,
                state=s,
            )
        s["done"] = bool(re.search(r"DECISION:\s*DIRECT", d, re.IGNORECASE))

        # The answer language comes from the classifier, else from the text
        # heuristic; it is never defaulted silently. ``s["language"]`` keeps
        # the heuristic seed from initial_state (progress texts) when the
        # resolution stays open, and ``language_source`` says which it was.
        m_lang = re.search(r"LANGUAGE:\s*(\w+)", d)
        _set_answer_language(
            s, classified=m_lang.group(1) if m_lang else None, stage="classify",
        )

        m_search_lang = re.search(r"SEARCH_LANGUAGE:\s*(\w+)", d)
        s["search_language"] = _resolve_search_language(
            s, m_search_lang.group(1) if m_search_lang else None,
        )

        # Extract recency requirement
        m_recency = re.search(r"RECENCY:\s*(\w+)", d)
        recency_raw = m_recency.group(1).strip().upper() if m_recency else "NONE"
        recency_map = {
            "HOUR": "hour",
            "DAY": "day",
            "WEEK": "week",
            "MONTH": "month",
            "NONE": "",
        }
        s["recency"] = _effective_web_recency(
            s, recency_map.get(recency_raw, "")
        )

        # Extract query type
        m_type = re.search(r"TYPE:\s*(\w+)", d)
        type_raw = m_type.group(1).strip().upper() if m_type else "GENERAL"
        type_map = {"ACADEMIC": "academic", "NEWS": "news", "GENERAL": "general"}
        s["query_type"] = type_map.get(type_raw, "general")

        # Fallback: keyword-based detection in case LLM misses academic questions
        if s["query_type"] != "academic":
            q_lower = s["question"].lower()
            academic_keywords = (
                "paper", "studie", "study", "preprint", "doi",
                "publikation", "publication", "arxiv", "veroeffentlich",
                "publish", "journal", "conference", "peer-review",
            )
            if any(kw in q_lower for kw in academic_keywords):
                _prev_type = s["query_type"]
                s["query_type"] = "academic"
                log.info("TRACE classify: type override %s->academic (keyword fallback)", _prev_type)

        # The answer form comes from the model. A keyword regex would read
        # every "belegen" as a claim check, so each delegated child would run
        # as a fact-check with rubric aspects. An unknown or missing value is a
        # visible fallback to "general", never a silent guess.
        m_contract = re.search(r"ANSWER_CONTRACT:\s*([A-Za-z_]+)", d)
        contract_raw = m_contract.group(1).strip().lower() if m_contract else ""
        if contract_raw in ANSWER_CONTRACTS:
            s["answer_contract"] = contract_raw
            s["answer_contract_source"] = "llm"
        else:
            contract_reason = f"invalid:{contract_raw}" if contract_raw else "missing"
            s["answer_contract"] = "general"
            s["answer_contract_source"] = "fallback_invalid"
            log.warning(
                "Classify: ANSWER_CONTRACT %s -> 'general' (model=%s)",
                contract_reason,
                classify_model,
            )
            emit_progress(
                s,
                t(s, "classify_contract_fallback", reason=contract_reason),
                severity="warning",
            )

        # The sub-questions (part 2) ARE the coverage checklist: content
        # questions with object, region and period; the evaluator judges
        # which of them the evidence answers.
        m_sub = re.search(r"SUB_QUESTIONS:\s*(\[.*)", d, re.DOTALL)
        sub_q_text = m_sub.group(1) if m_sub else ""
        parsed_sub_questions = parse_json_string_list(sub_q_text, fallback=[], max_items=4)
        if parsed_sub_questions:
            s["sub_questions"] = parsed_sub_questions
            s["aspects_source"] = "llm"
        else:
            s["sub_questions"] = [s["question"]]
            s["aspects_source"] = "fallback"
            log.warning(
                "Classify: SUB_QUESTIONS missing or unparseable; the question itself is"
                " the only subquestion (model=%s)",
                classify_model,
            )
            emit_progress(
                s, t(s, "classify_subquestions_fallback"), severity="warning"
            )
        s["required_aspects"] = list(s["sub_questions"])
        s["uncovered_aspects"] = list(s["required_aspects"])
        s["aspect_coverage"] = 0.0
        s["event_mode"] = _event_mode(s)

        # Trace logging
        log.info(
            "TRACE classify: decision=%s lang=%s search_lang=%s recency=%s type=%s sub_q=%d risk=%d high_risk=%s model=%s",
            "DIRECT" if s["done"] else "SEARCH",
            s["language"], s["search_language"], s["recency"] or "NONE", s["query_type"],
            len(s["sub_questions"]), s["risk_score"], s["high_risk"], classify_model,
        )

        if s["done"]:
            emit_progress(s, t(s, "classify_direct_answer"))
        else:
            hints: list[str] = []
            if s["search_language"] != s["language"]:
                sl = s["search_language"]
                lang_key = f"lang_name_{sl}"
                lang_name = t(s, lang_key) if lang_key in MESSAGES else sl
                hints.append(t(s, "search_in_lang", lang_name=lang_name))
            if s["recency"]:
                recency_key = f"recency_{s['recency']}"
                label = t(s, recency_key) if recency_key in MESSAGES else s["recency"]
                hints.append(t(s, "recency_label", label=label))
            if s["query_type"] != "general":
                type_key = f"type_{s['query_type']}"
                hints.append(t(s, type_key) if type_key in MESSAGES else s["query_type"])

            hint_str = f" ({', '.join(hints)})" if hints else ""
            emit_progress(s, t(s, "classify_search_required", hints=hint_str))

            if len(s["sub_questions"]) > 1:
                sub_q_display = ", ".join(
                    f"'{q}'" for q in s["sub_questions"][:3]
                )
                emit_progress(
                    s,
                    t(
                        s,
                        "classify_subquestions",
                        n=len(s["sub_questions"]),
                        subs=sub_q_display,
                    ),
                )
    except AgentRateLimited:
        raise
    except (
        OpenAIError,
        AgentTimeout,
        AnthropicAPIError,
        AzureOpenAIAPIError,
        BedrockAPIError,
    ) as exc:
        # Fail-safe: on classification error do NOT fall back to direct answer.
        # Conservatively continue researching with robust defaults — but make
        # the fallback visible in progress, logs and the iteration trace.
        _exc_label = type(exc).__name__
        log.warning(
            "Classify fallback activated (error_code=%s); using deterministic defaults",
            _exc_label,
        )
        emit_progress(s, t(s, "classify_failed", label=_exc_label), severity="warning")
        s["done"] = False
        _set_answer_language(s, classified=None, stage="classify_fallback")
        s["search_language"] = _resolve_search_language(s, s.get("search_language"))
        s["query_type"] = strategies.risk_scoring.infer_query_type(s["question"])
        s["answer_contract"] = "general"
        s["answer_contract_source"] = "fallback_error"
        s["recency"] = _effective_web_recency(
            s, "month" if s["query_type"] == "news" else ""
        )
        s["sub_questions"] = [s["question"]]
        s["required_aspects"] = [s["question"]]
        s["aspects_source"] = "fallback"
        s["uncovered_aspects"] = list(s["required_aspects"])
        s["aspect_coverage"] = 0.0
        s["event_mode"] = _event_mode(s)
        _classify_fallback = {
            "fallback": "classify_default",
            "fallback_reason": _exc_label,
            "fallback_message": str(exc)[:300],
        }

    append_iteration_log(s, {
        "node": "classify",
        "timestamp": time.time(),
        "duration_s": round(time.monotonic() - _t0, 3),
        "decision": "DIRECT" if s["done"] else "SEARCH",
        "question_length": len(s.get("question", "")),
        "history_length": len(s.get("history", "")),
        "lang": s["language"],
        "search_lang": s["search_language"],
        "recency": s["recency"] or "NONE",
        "type": s["query_type"],
        "answer_contract": s.get("answer_contract", "general"),
        "answer_contract_source": s.get("answer_contract_source", ""),
        "aspects_source": s.get("aspects_source", ""),
        "event_mode": bool(s.get("event_mode", False)),
        "sub_questions": s["sub_questions"],
        "sub_question_count": len(s["sub_questions"]),
        "risk_score": s["risk_score"],
        "high_risk": s["high_risk"],
        "model": classify_model,
        "required_aspects": s.get("required_aspects", []),
        "_classify_parsed": not bool(_classify_fallback),
        "_classify_fallback": bool(_classify_fallback),
        **_classify_fallback,
    }, testing_mode=settings.testing_mode)
    return s


# ======================================================================= #
# 2. plan
# ======================================================================= #


def plan(
    s: dict,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict:
    """Generate the next batch of research queries.

    Args:
        s: Mutable AgentState-compatible dict. Reads the current round,
            required aspects, gaps, and related questions; writes new
            planned queries and related planning metadata.
        providers: Active LLM and search providers.
        strategies: Runtime strategies used to derive quality terms and
            other planning hints.
        settings: Agent behavior settings controlling round limits and
            first-round breadth.

    Returns:
        The mutated state dict with updated query planning.

    Raises:
        AgentRateLimited: Propagated when the planning model hard-fails
            on upstream rate limiting.

    Example:
        >>> plan(state, providers=providers, strategies=strategies, settings=settings)
        {'queries': ['gkv reform 2026', ...], ...}
    """
    check_cancel_event(s)
    emit_progress(
        s,
        t(s, "plan_start", round=s["round"] + 1, max_rounds=settings.max_rounds),
    )
    _t0 = time.monotonic()
    _plan_fallback: dict[str, Any] = {}
    _plan_algorithm_failure: dict[str, Any] = {}
    target_query_count = _target_query_count_for_round(s["round"], settings)
    sub_question_count = len(s.get("sub_questions") or [])
    uncovered_aspect_count = len(s.get("uncovered_aspects") or [])
    query_generation_mode = "round0_breadth" if s["round"] == 0 else "gap_fill"
    perspective_instruction = ""
    deep_review_instruction = ""
    alternative_instruction = ""
    competing_instruction = ""
    reformulation_instruction = ""
    falsification_instruction = ""
    crosscheck_instruction = ""
    crosscheck_targets: list[dict[str, Any]] = []
    query_slots: list[dict[str, Any]] = []
    evidence_depth_gap = _evidence_depth_gap(s)
    s["evidence_depth_gap"] = evidence_depth_gap
    s["_evidence_depth_gap_active"] = bool(evidence_depth_gap.get("active"))
    try:
        _check_deadline(s["deadline"])
        is_deep = settings.report_profile is ReportProfile.DEEP

        # Build prompt with sub-questions and gap info
        sub_q_info = ""
        if s["sub_questions"]:
            sub_q_info = f"Sub-questions: {json.dumps(s['sub_questions'], ensure_ascii=False)}\n"

        uncovered_info = ""
        # The open list repeats the sub-questions verbatim before the first
        # evaluation; the prompt names them once ("Sub-questions") and the gap
        # slots carry them, so the block only appears once it differs.
        if s.get("uncovered_aspects") and list(s.get("uncovered_aspects") or []) != list(
            s.get("sub_questions") or []
        ):
            uncovered_info = (
                f"Sub-questions still open (according to the evaluation):\n"
                f"{json.dumps(s['uncovered_aspects'], ensure_ascii=False)}\n"
                f"At least one query MUST answer an open sub-question directly.\n"
            )

        gap_info = ""
        if s["gaps"]:
            gap_info = f"Information still missing: {s['gaps']}\n"

        # The query language is always stated: the prompt is English, so
        # without the line the model would write English queries for every
        # question.
        search_lang = s.get("search_language") or s.get("language") or "en"
        search_lang_name = LANGUAGE_NAMES_EN.get(search_lang, search_lang)
        if search_lang == "en" and s.get("language") != "en":
            lang_instruction = (
                "IMPORTANT: write the search queries in ENGLISH; English sources "
                "serve this topic best.\n"
            )
        else:
            lang_instruction = (
                f"IMPORTANT: write the search queries in {search_lang_name}.\n"
            )

        # Perspective diversity (STORM-inspired)
        perspective_instruction = ""
        if is_deep or s["round"] > 0:
            perspective_instruction = (
                "PERSPECTIVE DIVERSITY: look at the topic from a DIFFERENT perspective "
                "than the previous queries. Possible perspectives:\n"
                "- Technical/mechanistic: how exactly does it work?\n"
                "- Practical/application: how is it used?\n"
                "- Critical/limitations: what are the limits and problems?\n"
                "- Comparative: how does it compare to alternatives?\n"
                "- Historical/context: how did it develop?\n"
                "- Current/future: what are the latest developments?\n\n"
            )

        deep_review_instruction = ""
        if is_deep:
            deep_review_instruction = (
                "DEEP REVIEW MODE:\n"
                "Cover the question systematically from several perspectives. Search not only for the main thesis "
                "but also for opposing positions, the views of those affected and of stakeholders, figures/primary "
                "sources and possible alternatives or comparisons.\n"
            )
            if s["round"] == 0:
                deep_review_instruction += (
                    "In the first round the queries should cover sub-questions as different as possible. "
                    "Avoid variants of the same search. While slots are available, at least one query each "
                    "should target the status quo, stakeholders/positions, risks/counter-arguments, "
                    "figures/primary sources and alternatives/comparison.\n\n"
                )
            else:
                deep_review_instruction += (
                    "Use later rounds deliberately for open perspectives and evidence gaps instead of repetitions.\n\n"
                )

        # Alternative hypothesis search
        alternative_instruction = ""
        if s["round"] == 1:
            alternative_instruction = (
                "IMPORTANT — ALTERNATIVE HYPOTHESES:\n"
                "At least one of your queries MUST search for ALTERNATIVE events/answers.\n"
                f"{date_line(today())} The results so far could describe an OLDER event "
                "that is not the one the user means.\n"
                "Search deliberately for the MOST RECENT matching event — "
                "e.g. 'stock market crash AI February 2026' or 'latest AI selloff this week'.\n"
                "When the question says 'recently/lately/the other day', at least one query must "
                f"explicitly cover the period of the last 2-4 weeks before {today()}.\n\n"
            )

        # Competing events: force targeted comparison queries
        competing_instruction = ""
        competing = s.get("competing_events", "")
        if competing:
            competing_instruction = (
                f"IMPORTANT — COMPETING EXPLANATIONS:\n"
                f"The evaluation identified the following possible events/answers:\n"
                f"{competing}\n\n"
                f"Your queries MUST clarify deliberately which event is MORE RECENT and MORE RELEVANT.\n"
                f"Search for DIRECT comparisons, exact dates and specific details "
                f"that allow an unambiguous assignment.\n"
                f"At least one query must search for the NEWEST of the competing events "
                f"with an explicit date/period.\n\n"
            )

        # Aggressive reformulation on low confidence after round 1
        reformulation_instruction = ""
        if s["round"] >= 2 and s.get("final_confidence", 5) <= 4:
            reformulation_instruction = (
                "ATTENTION: the searches so far returned hardly any relevant results "
                f"(confidence: {s.get('final_confidence', '?')}/10 after {s['round']} rounds).\n"
                "You MUST change the search strategy FUNDAMENTALLY:\n"
                "1. QUESTION THE PREMISE: perhaps what is described does not exist at all, "
                "or the user confuses or mixes different things. "
                "Search instead for what ACTUALLY exists.\n"
                "2. Search for the BROADER topic: e.g. instead of 'DeepSeek paper on X' "
                "search 'DeepSeek latest papers 2026 list of all publications'\n"
                "3. Search for the people/organisations involved and their latest work\n"
                "4. Rephrase COMPLETELY — other terms, synonyms, higher-level categories\n"
                "5. Search for discussions/news ABOUT the topic instead of the topic itself\n\n"
            )

        # Falsification mode (FVA-RAG-inspired)
        falsification_instruction = ""
        if s.get("falsification_triggered", False):
            falsification_query_count = min(2, target_query_count)
            falsification_instruction = (
                "FALSIFICATION MODE ACTIVE:\n"
                "The research so far has repeatedly found NO convincing evidence for the "
                "claim/premise in the question. Now we search deliberately for "
                "COUNTER-EVIDENCE to test the premise.\n\n"
                f"At least {falsification_query_count} of your {target_query_count} "
                "queries MUST be falsification queries:\n"
                "- '[claim] debunked' or '[claim] myth'\n"
                "- '[topic] does not exist' or '[topic] never happened'\n"
                "- '[topic] hoax' or '[topic] refuted'\n"
                "- '[topic] misinformation' or '[topic] false claim'\n\n"
                "One further query MUST search for the closest matter that ACTUALLY "
                "exists: e.g. instead of 'Gemini discontinued' -> "
                "'Google Gemini current status 2026' or 'What has [organisation] "
                "ACTUALLY published?'\n\n"
                "GOAL: either we find evidence that the premise is false "
                "(-> high-confidence answer 'does not exist'), or we still find "
                "the correct matter.\n\n"
            )

        answer_contract = s.get("answer_contract", "general")
        contract_instruction = ""
        if answer_contract == "claim_check":
            contract_instruction = (
                "ANSWER CONTRACT: claim check.\n"
                "- Plan few, targeted queries for direct evidence and counter-evidence.\n"
                "- Prioritise official/primary sources and independent cross-checks.\n"
                "- Avoid broad stakeholder or essay context searches when they do not test the claim.\n\n"
            )
        elif answer_contract == "comparison":
            contract_instruction = (
                "ANSWER CONTRACT: comparison.\n"
                "- Plan separate queries per compared object with the same criteria "
                "(same quantities, periods, units).\n"
                "- One query may search for the comparison itself; no query searches for "
                "'claims', 'counter-evidence' or 'cross-checks' as a topic.\n\n"
            )
        elif answer_contract == "data_extraction":
            contract_instruction = (
                "ANSWER CONTRACT: data extraction.\n"
                "- Plan source paths per metric, period and company.\n"
                "- Search first for investor-relations, filing or official statistics sources, then for plausibility sources.\n"
                "- Every hard figure needs a direct source; avoid generic news overviews.\n\n"
            )
        elif answer_contract == "news_briefing":
            contract_instruction = (
                "ANSWER CONTRACT: news briefing.\n"
                "- Breadth is allowed, but central events need a cross-check by at least two sources.\n"
                "- Search for date, primary source and established reporting per event.\n\n"
            )

        crosscheck_targets = (
            _select_crosscheck_targets(
                claim_assessments(s),
                max_targets=max(1, target_query_count // 2),
            )
            if s["round"] > 0
            else []
        )
        if crosscheck_targets:
            crosscheck_instruction = (
                "CROSS-CHECK TARGETS:\n"
                "At least one query should confirm or refute one of the following statements "
                "with an independent source. Use domains other than the source_domains named "
                "so far where possible, and search for exact figures, dates or names.\n"
                f"{json.dumps(crosscheck_targets, ensure_ascii=False)}\n\n"
            )

        query_slots = _build_query_slots(
            s,
            target_count=target_query_count,
            crosscheck_targets=crosscheck_targets,
            evidence_depth_gap=evidence_depth_gap,
        )
        slot_instruction = ""
        if query_slots:
            slot_instruction = (
                "RESEARCH SLOTS:\n"
                f"Generate EXACTLY {target_query_count} search questions: exactly one individual "
                "search question per slot, in the same order as the slots. Every query "
                "must cover the concrete slot task and must not be a mere keyword "
                "chain. The main question stands under 'Question:' and applies to "
                "every slot.\n"
                f"{json.dumps(query_slots, ensure_ascii=False)}\n\n"
            )

        round_zero_query_instruction = (
            f"Generate exactly {target_query_count} diverse search queries that broadly cover different aspects, hypotheses and perspectives of the question"
            if s["round"] == 0
            else f"Generate exactly {target_query_count} precise, specific search queries"
        )

        plan_model, plan_effort = _resolve_node_llm(s, settings, providers, "plan")
        with _provider_retry_progress_context(
            providers.llm,
            s,
            operation_label=t(s, "retry_operation_plan"),
            testing_mode=settings.testing_mode,
        ):
            q = providers.llm.complete(
                f"{date_line(today())}\n\n"
                f"{round_zero_query_instruction} for a web search.\n"
                f"Every query should be 5-15 words long and contain concrete context.\n"
                f"Phrase every query as a COMPLETE QUESTION (what, which, how, when, where, why). "
                f"No bare keyword lists.\n"
                f"BAD: 'AI development' (keyword chain, too vague)\n"
                f"GOOD: 'Which breakthroughs in large language models appeared in 2025?'\n\n"
                f"{reformulation_instruction}"
                f"{falsification_instruction}"
                f"{competing_instruction}"
                f"{alternative_instruction}"
                f"{perspective_instruction}"
                f"{deep_review_instruction}"
                f"{contract_instruction}"
                f"{crosscheck_instruction}"
                f"{slot_instruction}"
                f"{lang_instruction}"
                f"Question: {s['question']}\n"
                f"{sub_q_info}"
                f"{uncovered_info}"
                f"{gap_info}"
                f"Previous queries: {s['queries']}\n"
                f"Previous results: {len(s.get('evidence_ledger', []) or [])} evidence records\n\n"
                f"Generate queries that deliver NEW information instead of repeating what is known, "
                f"and phrase them as questions.\n"
                f"Reply ONLY with a JSON array of strings. Example: [\"query1\", \"query2\"]",
                deadline=s["deadline"],
                model=plan_model,
                reasoning_effort=plan_effort,
                timeout=settings.reasoning_timeout,
                state=s,
            )
    except AgentRateLimited:
        raise
    except (
        OpenAIError,
        AgentTimeout,
        AnthropicAPIError,
        AzureOpenAIAPIError,
        BedrockAPIError,
    ) as exc:
        _exc_label = type(exc).__name__
        _failure_reason = f"Planung fehlgeschlagen ({_exc_label})"
        emit_progress(s, t(s, "plan_failed", label=_exc_label), severity="warning")
        _record_algorithm_failure(
            s,
            settings,
            node="plan",
            phase="plan_query_generation",
            reason=f"provider_{_exc_label}",
            message=f"{_failure_reason}; verwende Fallback-Query",
            blocking=False,
            details={"fallback": "original_question"},
        )
        q = ""
        query_generation_mode = "fallback_provider_error"
        _plan_algorithm_failure = {
            "_plan_algorithm_failure": True,
            "_plan_algorithm_failure_phase": "query_generation",
            "_plan_algorithm_failure_reason": f"provider_{_exc_label}",
        }
        _plan_fallback = {
            "fallback": "plan_default",
            "fallback_reason": _exc_label,
            "fallback_message": str(exc)[:300],
        }

    _max_items = target_query_count
    _fallback_query = [s["question"]]
    new_q, _query_parse_status = parse_json_string_list_with_status(
        q,
        fallback=_fallback_query,
        max_items=_max_items,
    )
    if query_slots and len(new_q) < _max_items:
        fallback_seen = {query.strip() for query in new_q if query.strip()}
        for slot in query_slots[len(new_q):]:
            candidate = _fallback_query_for_slot(
                slot,
                question=s.get("question", ""),
                search_language=s.get("search_language") or s.get("language") or "",
            )
            if candidate and candidate not in fallback_seen:
                new_q.append(candidate)
                fallback_seen.add(candidate)
            if len(new_q) >= _max_items:
                break
    _fell_back_to_original_question = (
        len(new_q) == 1
        and (new_q[0] or "").strip() == (s.get("question", "") or "").strip()
    )
    if not _plan_algorithm_failure and _query_parse_status != "parsed":
        _failure_reason = t(s, "research_query_plan_not_json")
        emit_progress(
            s,
            t(s, "plan_query_generation_failed", reason=_failure_reason),
            severity="warning",
        )
        _record_algorithm_failure(
            s,
            settings,
            node="plan",
            phase="plan_query_generation",
            reason=_query_parse_status,
            message=f"{_failure_reason}; verwende Fallback-Query",
            blocking=False,
            details={"fallback": "original_question"},
        )
        query_generation_mode = "fallback_invalid_json"
        _plan_algorithm_failure = {
            "_plan_algorithm_failure": True,
            "_plan_algorithm_failure_phase": "query_generation",
            "_plan_algorithm_failure_reason": _query_parse_status,
        }
    elif not _plan_algorithm_failure and _fell_back_to_original_question:
        _failure_reason = t(s, "research_plan_query_fallback_reason")
        emit_progress(
            s,
            t(s, "plan_query_generation_failed", reason=_failure_reason),
            severity="warning",
        )
        _record_algorithm_failure(
            s,
            settings,
            node="plan",
            phase="plan_query_generation",
            reason="fallback_original_question",
            message=f"{_failure_reason}; verwende Fallback-Query",
            blocking=False,
            details={"fallback": "original_question"},
        )
        query_generation_mode = "fallback_original_question"
        _plan_algorithm_failure = {
            "_plan_algorithm_failure": True,
            "_plan_algorithm_failure_phase": "query_generation",
            "_plan_algorithm_failure_reason": "fallback_original_question",
        }

    # Deduplicate, preserve order
    seen = set(s["queries"])
    added = 0
    for query in new_q:
        if query not in seen:
            s["queries"].append(query)
            seen.add(query)
            added += 1

    # Inform user about generated queries and active strategies
    strategies_active = [
        t(s, key)
        for active, key in (
            (deep_review_instruction, "research_strategy_deep_perspectives"),
            (perspective_instruction, "research_strategy_storm_perspectives"),
            (falsification_instruction, "research_strategy_falsification"),
            (alternative_instruction, "research_strategy_alternative"),
            (competing_instruction, "research_strategy_competing"),
            (reformulation_instruction, "research_strategy_reformulation"),
            (crosscheck_instruction, "research_strategy_crosscheck"),
        )
        if active
    ]
    if added > 0:
        basis_hint = t_count(s, "research_query_basis", sub_question_count)
        if uncovered_aspect_count and uncovered_aspect_count < sub_question_count:
            basis_hint += ", " + t(
                s, "research_query_basis_open", count=uncovered_aspect_count
            )
        strategy_hint_parts = [basis_hint]
        if strategies_active:
            strategy_hint_parts.append(", ".join(strategies_active))
        strategy_hint = f" ({'; '.join(strategy_hint_parts)})"
        emit_progress(
            s,
            t(s, "plan_new_queries", added=added, strategy_hint=strategy_hint),
        )

    # Exact queries are durable run-audit data.  They must not be mirrored to
    # ordinary stdout where operators outside the run's authorization scope
    # may read them.  The protected query records below retain the complete
    # text; the operational log needs only cardinality and stable ids.
    log.info(
        "TRACE plan: round=%d new_query_count=%d total=%d",
        s["round"], len(new_q), len(s["queries"]),
    )

    # If no new queries: answer directly (prevents infinite loop)
    if added == 0:
        emit_progress(s, t(s, "plan_no_more_queries"))
        log.info("No new search queries generated; ending research")
        s["done"] = True

    append_iteration_log(s, {
        "node": "plan",
        "timestamp": time.time(),
        "duration_s": round(time.monotonic() - _t0, 3),
        "round": s["round"],
        "new_queries": new_q,
        "new_query_count": len(new_q),
        "added_queries": added,
        "target_query_count": target_query_count,
        "sub_question_count": sub_question_count,
        "planning_basis": {
            "sub_question_count": sub_question_count,
                "uncovered_aspect_count": uncovered_aspect_count,
            "active_strategies": strategies_active,
            "crosscheck_target_count": len(crosscheck_targets),
            "query_slot_count": len(query_slots),
            "query_slot_types": [slot.get("slot_type", "") for slot in query_slots],
            "evidence_depth_gap_active": bool(evidence_depth_gap.get("active")),
        },
        "crosscheck_targets": crosscheck_targets,
        "query_slots": query_slots,
        "query_slot_count": len(query_slots),
        "query_slot_types": [slot.get("slot_type", "") for slot in query_slots],
        "crosscheck_target_count": len(crosscheck_targets),
        "evidence_depth_gap": evidence_depth_gap,
        "query_generation_mode": query_generation_mode,
        "done_no_new_queries": added == 0,
        "_plan_fallback": bool(_plan_fallback),
        "_plan_parse_status": _query_parse_status,
        "_plan_algorithm_failure": bool(_plan_algorithm_failure),
        "_plan_stored_queries": added,
        "total_queries": len(s["queries"]),
        "required_aspects": s.get("required_aspects", []),
        "answer_contract": s.get("answer_contract", "general"),
        "uncovered_aspects": s.get("uncovered_aspects", []),
        **_plan_fallback,
        **_plan_algorithm_failure,
    }, testing_mode=settings.testing_mode)
    return s


# ======================================================================= #
# 3. search
# ======================================================================= #


def search(
    s: dict,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict:
    """Execute the current query batch and merge search evidence into state.

    Args:
        s: Mutable AgentState-compatible dict. Reads queued queries,
            offsets, and deadline; writes context blocks, citations,
            token counters, claims, and round progress.
        providers: Active LLM and search providers.
        strategies: Runtime strategies for claim extraction
            and consolidation.
        settings: Agent behavior settings controlling batch width,
            timeouts, and logging/test instrumentation.

    Returns:
        The mutated state dict after search and claim
        extraction complete or short-circuit.

    Raises:
        AgentRateLimited: Propagated when a provider surfaces a fatal
            rate limit that must abort the run.

    Example:
        >>> search(state, providers=providers, strategies=strategies, settings=settings)
        {'all_citations': ['https://...'], 'context': ['...'], ...}
    """
    check_cancel_event(s)
    _t0 = time.monotonic()
    _batch = _target_query_count_for_round(s["round"], settings)
    offset = s["search_offset"]
    new_q = s["queries"][offset:offset + _batch]
    s["search_offset"] = offset + len(new_q)
    emit_progress(
        s,
        t(
            s,
            "search_start",
            n=len(new_q),
            round=s["round"] + 1,
            max_rounds=settings.max_rounds,
        ),
    )

    if not new_q:
        # No queries left -> go straight to answer
        s["done"] = True
        s["round"] += 1
        append_iteration_log(s, {
            "node": "search",
            "timestamp": time.time(),
            "duration_s": round(time.monotonic() - _t0, 3),
            "round": s["round"] - 1,
            "queries_executed": 0,
            "queries": [],
            "sources_found": 0,
            "total_citations": len(s["all_citations"]),
            "evidence_record_count": len(s.get("evidence_ledger", []) or []),
            "skipped": "no_queries",
        }, testing_mode=settings.testing_mode)
        return s

    try:
        _check_deadline(s["deadline"])
    except AgentTimeout:
        s["done"] = True
        s["round"] += 1
        append_iteration_log(s, {
            "node": "search",
            "timestamp": time.time(),
            "duration_s": round(time.monotonic() - _t0, 3),
            "round": s["round"] - 1,
            "queries_executed": 0,
            "queries": [],
            "sources_found": 0,
            "total_citations": len(s["all_citations"]),
            "evidence_record_count": len(s.get("evidence_ledger", []) or []),
            "skipped": "deadline_exceeded",
        }, testing_mode=settings.testing_mode)
        return s

    search_capabilities = get_search_provider_capabilities(providers.search)

    # Build search parameters from classify results
    search_kwargs: dict[str, Any] = {
        "deadline": s["deadline"],
    }

    if search_capabilities.supports("search_context_size"):
        search_kwargs["search_context_size"] = "high"  # Always max depth

    # Recency filter
    recency = s.get("recency", "")
    if recency and search_capabilities.supports("recency_filter"):
        search_kwargs["recency_filter"] = recency

    # Language filter via API (more reliable than prompt instruction)
    search_lang = s.get("search_language", "")
    if search_lang and search_capabilities.supports("language_filter"):
        search_kwargs["language_filter"] = [search_lang]

    # Query type -> search_mode
    query_type = s.get("query_type", "general")
    if query_type == "academic" and search_capabilities.supports("search_mode"):
        search_kwargs["search_mode"] = "academic"

    # Explicit `site:...` operators still become provider allow-lists. Normal
    # queries intentionally do not carry a default domain blocklist so search
    # breadth is not silently constrained before source-tiering can inspect it.
    _base_domain_filter: list[str] | None = None
    _forensic = forensic_enabled(settings)
    _collect_query_details = settings.testing_mode or log.isEnabledFor(logging.DEBUG) or _forensic

    def _consume_nonfatal_notice(obj: object) -> str | None:
        consumer = getattr(obj, "consume_nonfatal_notice", None)
        if callable(consumer):
            return consumer()
        return None

    def _consume_retry_notices(obj: object) -> list[dict[str, Any]]:
        consumer = getattr(obj, "consume_retry_notices", None)
        if not callable(consumer):
            return []
        notices = consumer()
        if not isinstance(notices, list):
            return []
        return [dict(item) for item in notices if isinstance(item, dict)]

    def _emit_retry_progress(
        notices: list[dict[str, Any]],
        *,
        operation_label: str,
    ) -> None:
        notices = [notice for notice in notices if not notice.get("progress_emitted")]
        if not notices:
            return
        provider_label = str(notices[-1].get("provider") or type(providers.llm).__name__)
        models = sorted({
            str(item.get("model") or "").strip()
            for item in notices
            if str(item.get("model") or "").strip()
        })
        model_label = models[0] if len(models) == 1 else f"{len(models)} models"
        emit_progress(
            s,
            t(
                s,
                "provider_retry_observed",
                provider=provider_label,
                count=len(notices),
                operation=operation_label,
                model=model_label,
            ),
            severity="warning",
        )

    def _domain_filter_for_query(q: str) -> list[str] | None:
        return _domain_filter_for_query_text(q, base_domain_filter=_base_domain_filter)

    # Request related questions in first round
    if s["round"] == 0 and search_capabilities.supports("return_related"):
        search_kwargs["return_related"] = True

    _query_domain_filters = [_domain_filter_for_query(q) for q in new_q]
    # Unwrap the tracing decorator: the timeline must show the REAL
    # provider (AzureFoundryBingSearch, ...), never the wrapper class.
    from inqtrix.runtime_logging import unwrap_provider

    _search_provider_label = type(unwrap_provider(providers.search)).__name__
    s.setdefault("query_records", [])
    s.setdefault("source_records", {})
    s.setdefault("provider_citation_records", [])
    s.setdefault("evidence_ledger", [])
    s.setdefault("answer_evidence_bindings", [])

    # Register every invocation before the fan-out starts. A provider failure
    # can abort the node before its ordinary evidence projection is reached;
    # these stable slots plus the protected start event preserve the exact
    # redacted query and let the failure path terminalise every submitted item.
    _round_query_records: list[dict[str, Any]] = []
    _query_started_monotonic: list[float] = []
    _query_record_lock = Lock()
    _search_parameters = {
        key: value
        for key, value in search_kwargs.items()
        if key != "deadline"
    }
    for _qi, q in enumerate(new_q):
        query_id = _query_id_for(
            s,
            round_index=s["round"],
            query_index=offset + _qi,
            query=q,
        )
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        query_record = {
            "query_id": query_id,
            "invocation_id": query_id,
            "round": s["round"],
            "query_index": offset + _qi,
            "query": scrub_credential_urls(q),
            "domain_filter": _query_domain_filters[_qi] or [],
            "provider": _search_provider_label,
            "parameters": dict(_search_parameters)
            | {"domain_filter": _query_domain_filters[_qi] or []},
            "status": "running",
            "notice": "",
            "started_at": started_at,
            "finished_at": "",
            "duration_ms": 0,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }
        _round_query_records.append(query_record)
        _query_started_monotonic.append(time.monotonic())
        s["query_records"].append(query_record)
        _append_forensic_event(
            s,
            settings,
            event="query_invocation_started",
            node="search",
            payload=query_record,
        )
        emit_run_event(
            s,
            "inqtrix.research.query.started",
            {
                key: query_record[key]
                for key in (
                    "invocation_id",
                    "query_id",
                    "round",
                    "query_index",
                    "query",
                    "domain_filter",
                    "provider",
                    "parameters",
                    "status",
                    "started_at",
                )
            },
        )

    def _finalize_query_record(
        query_index: int,
        *,
        status: str,
        notice: str = "",
        result: GroundedSearchResult | None = None,
    ) -> None:
        finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
        duration_ms = max(
            0,
            int(
                (time.monotonic() - _query_started_monotonic[query_index])
                * 1000
            ),
        )
        with _query_record_lock:
            record = _round_query_records[query_index]
            # The fan-out coordinator may terminalise an abandoned slot after
            # a sibling failed. A late worker must never regress that terminal
            # audit record.
            if record.get("status") != "running":
                return
            record.update(
                {
                    "status": status,
                    "notice": shorten_visibly(
                        scrub_credential_urls(notice),
                        512,
                        marker=LOG_CUT_MARKER,
                    ),
                    "finished_at": finished_at,
                    "duration_ms": duration_ms,
                    "usage": {
                        "prompt_tokens": max(
                            0, int(getattr(result, "prompt_tokens", 0) or 0)
                        ),
                        "completion_tokens": max(
                            0,
                            int(getattr(result, "completion_tokens", 0) or 0),
                        ),
                    },
                }
            )

    def _emit_query_finished_events() -> None:
        for record in _round_query_records:
            _append_forensic_event(
                s,
                settings,
                event="query_invocation_finished",
                node="search",
                payload=record,
            )
            emit_run_event(
                s,
                "inqtrix.research.query.finished",
                {
                    key: record[key]
                    for key in (
                        "invocation_id",
                        "query_id",
                        "round",
                        "query_index",
                        "provider",
                        "status",
                        "notice",
                        "started_at",
                        "finished_at",
                        "duration_ms",
                        "usage",
                    )
                },
            )

    # Parallel search
    def _search_one(item: tuple[int, str, list[str] | None]) -> SearchOutcome:
        query_index, q, domain_filter = item
        effective_domain_filter = (
            domain_filter if search_capabilities.supports("domain_filter") else None
        )
        operation_label = f"{t(s, 'retry_operation_search')} {query_index + 1}/{len(new_q)}"
        try:
            with _provider_retry_progress_context(
                providers.search,
                s,
                operation_label=operation_label,
                testing_mode=settings.testing_mode,
            ):
                result = providers.search.search(
                    q,
                    domain_filter=effective_domain_filter,
                    **search_kwargs,
                )
            if not isinstance(result, GroundedSearchResult):
                raise TypeError(
                    "SearchProvider.search() must return GroundedSearchResult"
                )
            notice = _consume_nonfatal_notice(providers.search)
            _finalize_query_record(
                query_index,
                # An empty provider result is still a successfully completed
                # invocation. Evidence sufficiency is decided later and must
                # not be conflated with transport/provider failure.
                status="completed",
                notice=str(notice or ""),
                result=result,
            )
            record = _round_query_records[query_index]
            return SearchOutcome(
                result,
                notice,
                str(record["started_at"]),
                str(record["finished_at"]),
            )
        except Exception as exc:
            _finalize_query_record(
                query_index,
                status=(
                    "cancelled"
                    if isinstance(exc, AgentCancelled)
                    else "failed"
                ),
                notice=sanitize_error(exc),
            )
            raise

    provider_cap = int(getattr(search_capabilities, "max_concurrency", 0) or 0)
    _n_workers = min(
        len(new_q),
        settings.first_round_queries,
        provider_cap or len(new_q),
    )

    try:
        _outcomes = _map_cancellable(
            s,
            _search_one,
            [
                (query_index, q, domain_filter)
                for query_index, (q, domain_filter) in enumerate(
                    zip(new_q, _query_domain_filters, strict=True)
                )
            ],
            max_workers=_n_workers,
            operation_label=t(s, "retry_operation_search"),
            testing_mode=settings.testing_mode,
        )
    except Exception as exc:
        terminal_status = (
            "cancelled" if isinstance(exc, AgentCancelled) else "failed"
        )
        for query_index, record in enumerate(_round_query_records):
            if record.get("status") == "running":
                _finalize_query_record(
                    query_index,
                    status=terminal_status,
                    notice=(
                        "fan-out cancelled before this query completed"
                        if terminal_status == "cancelled"
                        else "fan-out stopped after a sibling query failed"
                    ),
                )
        _emit_query_finished_events()
        raise
    _emit_query_finished_events()
    check_cancel_event(s)
    # Provider prose, snippets and URLs are untrusted. Redact credential-
    # bearing URLs before any claim prompt, checkpoint, ledger or UI event can
    # observe them. Inqtrix does not follow those URLs; they remain user-facing
    # provenance anchors for the provider-grounded search result.
    results = [
        sanitize_grounded_search_result(outcome.result) for outcome in _outcomes
    ]
    notices = [outcome.notice for outcome in _outcomes]

    _source_records_by_query: dict[int, list[dict[str, Any]]] = {}
    _citation_records_by_query: dict[int, list[dict[str, Any]]] = {}
    _round_new_source_ids: list[str] = []
    _existing_citation_ids = {
        str(record.get("citation_id", ""))
        for record in s.get("provider_citation_records", [])
    }
    for _qi, q in enumerate(new_q):
        r = results[_qi]
        query_record = _round_query_records[_qi]
        query_id = str(query_record["query_id"])

        _all_source_urls = [src.url for src in r.sources if src.url]
        tier_explanations = _tier_explanations_for_urls(
            _all_source_urls,
            strategies,
        )
        source_records, citation_records = normalize_source_provenance(
            r,
            query_id=query_id,
            provider=_search_provider_label,
            tier_explanations=tier_explanations,
        )
        _source_records_by_query[_qi] = source_records
        _citation_records_by_query[_qi] = citation_records

        source_registry: dict[str, dict[str, Any]] = s["source_records"]
        for source_record in source_records:
            sid = source_record["source_id"]
            if sid in source_registry:
                continue
            source_registry[sid] = source_record
            # The forensic source_record event is emitted after this round's
            # roles are pinned, so it carries the placement, not the floor.
            _round_new_source_ids.append(sid)

        for citation_record in citation_records:
            citation_id = str(citation_record.get("citation_id", ""))
            if citation_id and citation_id not in _existing_citation_ids:
                _existing_citation_ids.add(citation_id)
                s["provider_citation_records"].append(citation_record)
            _append_forensic_event(
                s,
                settings,
                event="provider_citation_record",
                node="search",
                payload=citation_record,
            )

        _append_forensic_event(
            s,
            settings,
            event="query_record",
            node="search",
            payload={
                **query_record,
                "source_ids": [record["source_id"] for record in source_records],
                "citation_ids": [record["citation_id"] for record in citation_records],
            },
        )

    # Azure Web Grounding is the complete web-evidence boundary. The linked
    # pages remain user-verifiable destinations and are not fetched here.
    _query_details: list[dict[str, Any]] = []
    if _collect_query_details:
        for _qi, q in enumerate(new_q):
            r = results[_qi]
            _detail: dict[str, Any] = {
                "query_id": _round_query_records[_qi]["query_id"],
                "round": s["round"],
                "query_index": offset + _qi,
                "query": q,
                "domain_filter": _query_domain_filters[_qi] or [],
                "provider_notice": notices[_qi] or "",
                "answer_length": len(r.answer),
                "citation_count": len(r.citation_urls),
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
            }
            if r.citation_urls:
                _detail["urls"] = [normalize_url(u) for u in r.citation_urls[:5]]
            _query_details.append(_detail)

    _search_fallbacks = sum(1 for n in notices if n)
    # "Empty" means the query yielded nothing usable -- neither a synthesized
    # answer NOR citable sources. A source-only result (no prose but real
    # sources) is ingested as evidence, so counting it here would emit a
    # misleading "no results" notice for a query that did return sources. This
    # mirrors the source-only ingestion gate (a result is usable if it carries
    # an answer or sources).
    _empty_without_notice = sum(
        1 for res, n in zip(results, notices)
        if not res.answer and not res.sources and not n
    )
    if _search_fallbacks:
        emit_progress(
            s,
            t(
                s,
                "search_failed_n_of_m",
                failed=_search_fallbacks,
                total=len(new_q),
            ),
            severity="warning",
        )
    if _empty_without_notice:
        emit_progress(
            s,
            t(
                s,
                "search_empty_n_of_m",
                empty=_empty_without_notice,
                total=len(new_q),
            ),
        )

    # Aggregate token usage from Sonar searches
    _search_prompt_tokens = 0
    _search_completion_tokens = 0
    for r in results:
        _search_prompt_tokens += r.prompt_tokens
        _search_completion_tokens += r.completion_tokens

    # Phase 1: structured claim extraction for later consolidation.
    tuning = settings.report_tuning
    _claim_inputs: list[tuple[int, str, list[str], list[ProviderCitationRef], dict[str, str]]] = []
    _registry_now: dict[str, dict[str, Any]] = s.get("source_records", {}) or {}
    for _qi, r in enumerate(results):
        query_citations = _citation_records_by_query.get(_qi, [])
        if not r.answer:
            continue
        claim_text = _claim_extraction_text(
            r.answer,
            query_citations,
            max_sources=tuning.claim_citation_cap,
        )
        if not claim_text.strip():
            continue
        claim_citations = [
            normalize_url(str(record.get("canonical_url", "") or ""))
            for record in query_citations
            if normalize_url(str(record.get("canonical_url", "") or ""))
        ] or list(r.citation_urls)
        # Sources the run already placed (floor, custom strategy, an earlier
        # judgement) are fixed on the card: the model does not judge them
        # again, and their judgement would be discarded anyway.
        fixed_roles = {
            str(record.get("canonical_url", "")): (
                placement_kind(str(record.get("tier_reason", "") or ""))[1]
                or f"strategy:{record.get('tier', 'unknown')}"
            )
            for record in (_registry_now.get(str(rec.get("source_id", ""))) or rec for rec in _source_records_by_query.get(_qi, []))
            if str(record.get("tier_reason", "") or "") not in ("", UNJUDGED_REASON)
        }
        _claim_inputs.append((
            _qi,
            claim_text,
            claim_citations,
            _claim_provider_refs(query_citations),
            fixed_roles,
        ))

    _claim_results: dict[int, tuple[list[dict[str, Any]], int, int]] = {}
    _claim_metadata: dict[int, dict[str, Any]] = {}
    _claim_roles: dict[int, list[dict[str, str]]] = {}
    _claim_fallbacks = 0
    _claim_warnings: dict[int, str] = {}
    _claim_retry_notices: list[dict[str, Any]] = []
    if _claim_inputs:
        # Resolve claim_extract once per round (not per hit) so the model/effort
        # routing is uniform with every other node and emits a single visible
        # resolution event, then thread the result into each worker. The routing
        # kwargs are a best-effort hint: only forward them to a strategy whose
        # extract() accepts them, so a custom strategy on the older signature
        # keeps working (Baukasten backward-compat).
        _ce_model, _ce_effort = _resolve_node_llm(s, settings, providers, "claim_extract")
        _ce_routing: dict[str, Any] = (
            {"model": _ce_model, "reasoning_effort": _ce_effort}
            if _claim_extract_accepts_routing(strategies.claim_extraction)
            else {}
        )
        _ce_takes_fixed_roles = _claim_extract_accepts(
            strategies.claim_extraction, "fixed_roles"
        )

        def _do_claim_extract(
            item: tuple[int, str, list[str], list[ProviderCitationRef], dict[str, str]],
        ) -> tuple[
            int,
            tuple[list[dict[str, Any]], int, int],
            str | None,
            dict[str, Any],
            list[dict[str, Any]],
            list[dict[str, str]],
        ]:
            idx, text, citations, provider_refs, fixed_roles = item
            _ce_fixed: dict[str, Any] = (
                {"fixed_roles": fixed_roles} if _ce_takes_fixed_roles and fixed_roles else {}
            )
            # Claim extraction is its own purpose inside the search node
            # (its own model, its own share of the tokens); the pool
            # copies the context, so the binding lands on the pool thread.
            with bind_purpose("claim_extract"), _provider_retry_progress_context(
                providers.llm,
                s,
                operation_label=t(s, "retry_operation_claim_extraction"),
                testing_mode=settings.testing_mode,
            ):
                result_tuple = strategies.claim_extraction.extract(
                    text,
                    citations,
                    s.get("question", ""),
                    deadline=s["deadline"],
                    provider_refs=provider_refs,
                    text_char_limit=tuning.claim_input_char_limit,
                    citation_cap=tuning.claim_citation_cap,
                    max_claims=tuning.claim_max_items,
                    source_url_limit=tuning.claim_source_url_cap,
                    **_ce_routing,
                    **_ce_fixed,
                )
            metadata = _consume_optional(strategies.claim_extraction, "consume_extraction_metadata")
            roles = _consume_optional(strategies.claim_extraction, "consume_source_roles")
            return (
                idx,
                result_tuple,
                _consume_nonfatal_notice(strategies.claim_extraction),
                metadata if isinstance(metadata, dict) else {},
                _consume_retry_notices(providers.llm),
                roles if isinstance(roles, list) else [],
            )

        # Size the claim fan-out by the LLM provider's OWN concurrency,
        # NOT the search width (_n_workers, derived from the search
        # provider): claim extraction is one LLM call per search result,
        # and pacing it to the search cap (up to first_round_queries)
        # bursts the LLM endpoint into a 429. The pool width IS the
        # limiter (one fan-out at a time, sequential with the already-
        # finished search pool), so no separate semaphore is needed.
        _llm_workers = _claim_fanout_width(len(_claim_inputs), providers.llm)
        for idx, result_tuple, warning, metadata, retry_notices, roles in _map_cancellable(
            s,
            _do_claim_extract,
            _claim_inputs,
            max_workers=_llm_workers,
            operation_label=t(s, "retry_operation_claim_extraction"),
            testing_mode=settings.testing_mode,
        ):
            _claim_results[idx] = result_tuple
            _claim_retry_notices.extend(retry_notices)
            if roles:
                _claim_roles[idx] = roles
            if metadata:
                _claim_metadata[idx] = metadata
            if warning:
                _claim_fallbacks += 1
                _claim_warnings[idx] = warning
    s["_claim_extraction_attempts_total"] = (
        int(s.get("_claim_extraction_attempts_total", 0) or 0)
        + len(_claim_inputs)
    )
    s["_claim_extraction_failures_total"] = (
        int(s.get("_claim_extraction_failures_total", 0) or 0)
        + _claim_fallbacks
    )

    _emit_retry_progress(
        _claim_retry_notices,
        operation_label=t(s, "retry_operation_claim_extraction"),
    )
    if _claim_fallbacks:
        emit_progress(
            s,
            t(
                s,
                "claim_extract_failed",
                failed=_claim_fallbacks,
                total=len(_claim_inputs),
            ),
            severity="warning",
        )
        _all_claim_inputs_failed = _claim_inputs and _claim_fallbacks == len(_claim_inputs)
        _record_algorithm_failure(
            s,
            settings,
            node="search",
            phase="claim_extraction",
            reason=(
                "round_all_sources_failed"
                if _all_claim_inputs_failed
                else "round_some_sources_failed"
            ),
            message=(
                "All claim-extraction calls in this search round failed; "
                "the round produced no structured claim path."
                if _all_claim_inputs_failed
                else "Some claim-extraction calls in this search round failed; "
                "the report must not be marked as fully clean."
            ),
            blocking=False,
            details={
                "failed": _claim_fallbacks,
                "total": len(_claim_inputs),
                "claim_notice_samples": list(_claim_warnings.values())[:3],
            },
        )

    _claim_valid_empty = sum(
        1
        for idx, (claims, _, _) in _claim_results.items()
        if idx not in _claim_warnings and not claims
    )
    if _claim_valid_empty:
        log.warning(
            "TRACE search: %d/%d claim extractions returned no structured claims despite search context",
            _claim_valid_empty,
            len(_claim_inputs),
        )
        emit_progress(
            s,
            t(
                s,
                "claim_extract_empty",
                empty=_claim_valid_empty,
                total=len(_claim_inputs),
            ),
        )

    _claim_prompt_tokens = 0
    _claim_completion_tokens = 0
    for _, pt, ct in _claim_results.values():
        _claim_prompt_tokens += pt
        _claim_completion_tokens += ct
    _claim_mode_counts: dict[str, int] = {}
    _claim_unknown_provider_ref_count = 0
    _claim_unbound_count = 0
    _role_invalid = 0
    for metadata in _claim_metadata.values():
        mode = str(metadata.get("claim_extraction_mode") or "unknown")
        _claim_mode_counts[mode] = _claim_mode_counts.get(mode, 0) + 1
        _claim_unknown_provider_ref_count += int(
            metadata.get("unknown_provider_ref_count", 0) or 0
        )
        _claim_unbound_count += int(metadata.get("unbound_claim_count", 0) or 0)
        _role_invalid += int(metadata.get("source_role_invalid_count", 0) or 0)
    # Pin the whole round's judgements before any evidence record is built
    # (every query's judgements are in hand now), then count over the
    # registry what the round left unjudged — not the per-call remainders,
    # which double-count a source that sat on two cards.
    _role_applied = _role_conflicts = 0
    for _qi in range(len(results)):
        applied, conflicts = _pin_source_roles(
            s, _source_records_by_query.get(_qi, []), _claim_roles.get(_qi, [])
        )
        _role_applied += applied
        _role_conflicts += conflicts
    _recount_source_roles(s, conflicts=_role_conflicts)
    _registry_after_pin = s.get("source_records", {}) or {}
    for sid in _round_new_source_ids:
        pinned = _registry_after_pin.get(sid)
        if pinned is not None:
            _append_forensic_event(
                s, settings, event="source_record", node="search", payload=pinned
            )
    _role_unjudged_now = sum(
        1
        for sid in _round_new_source_ids
        if placement_kind(str((_registry_after_pin.get(sid) or {}).get("tier_reason") or ""))[0] == "unjudged"
    )
    if _role_unjudged_now or _role_invalid:
        # A source the extractor did not judge stays unjudged (unknown) —
        # visible here and on the trace, never a silent guess.
        log.warning(
            "Source roles: %d judged, %d unjudged, %d invalid (round=%d)",
            _role_applied, _role_unjudged_now, _role_invalid, int(s.get("round", 0) or 0),
        )
        emit_progress(
            s,
            t(s, "search_source_roles", judged=_role_applied, missing=_role_unjudged_now, invalid=_role_invalid),
            severity="warning",
        )
    for idx, metadata in _claim_metadata.items():
        omitted_chars = int(metadata.get("input_omitted_chars", 0) or 0)
        omitted_citations = int(metadata.get("citation_omitted_count", 0) or 0)
        source_prompt_omissions = [
            record
            for record in _citation_records_by_query.get(idx, [])
            if int(
                record.get("claim_prompt_original_omitted_chars", 0)
                or 0
            )
            > 0
        ]
        if omitted_chars or omitted_citations or source_prompt_omissions:
            query_id = (
                str(_round_query_records[idx].get("query_id", "") or "")
                if idx < len(_round_query_records)
                else ""
            )
            s.setdefault("evidence_omissions", []).append(
                {
                    "stage": "prompt",
                    "record_ids": [
                        str(record.get("source_id", "") or "")
                        for record in source_prompt_omissions
                        if str(record.get("source_id", "") or "")
                    ]
                    or ([query_id] if query_id else []),
                    "count": max(
                        1,
                        omitted_citations + len(source_prompt_omissions),
                    ),
                    "reason": "claim_extraction_prompt_budget",
                    "details": {
                        "input_omitted_chars": omitted_chars,
                        "citation_omitted_count": omitted_citations,
                        "source_projection_omission_count": len(
                            source_prompt_omissions
                        ),
                    },
                }
            )

    # Surface claim-binding health once per round when claims could not be
    # bound to a source. This is the visible counterpart to the forensic
    # `_claim_unbound_claims` / `_claim_unknown_provider_refs` markers, and it
    # explains a later `needs_review` / `source_context_only` contract
    # downgrade while the report is still being built.
    if _claim_unbound_count or _claim_unknown_provider_ref_count:
        emit_progress(
            s,
            t(
                s,
                "search_claim_binding_issues",
                unbound=_claim_unbound_count,
                unknown=_claim_unknown_provider_ref_count,
            ),
            severity="warning",
        )

    # Phase 2: Sequential context assembly (state access, not parallelisable)
    focus_stems = strategies.claim_consolidation.focus_stems_from_question(
        s.get("question", ""))
    if s.get("answer_contract") == "news_briefing":
        focus_stems = set()

    sources_found = 0
    _ledger_dropped_total = 0
    _kept_claim_ids: list[str] = []
    _sources_summary: list[dict[str, Any]] = []
    for _qi, r in enumerate(results):
        # Ingest a result that carries EITHER a synthesized answer OR cited
        # sources. GroundedSearchResult.sources is independent of .answer, so a
        # provider may return citable sources without prose. Successfully read
        # original content has already entered the normal claim extraction,
        # attribution, authority and consolidation path above; only an empty or
        # failed extraction remains source-context-only. Skip only when both
        # provider answer and discovered sources are empty.
        if not r.answer and not r.sources:
            continue

        if r.citation_urls:
            # Normalise URLs and collect globally (deduplicated)
            for url in r.citation_urls:
                normalized = normalize_url(url)
                if normalized not in s["all_citations"]:
                    s["all_citations"].append(normalized)
        sources_found += 1

        # Fill claim ledger with structured assertions
        extracted_claims = _claim_results.get(_qi, ([], 0, 0))[0]
        kept_claims = 0
        query_id = _round_query_records[_qi]["query_id"]
        # Every record of the query takes the registry's placement (the
        # round's roles are pinned) so the ledger and the claims agree.
        _source_records_by_query[_qi] = [
            _with_registry_placement(s, record)
            for record in _source_records_by_query.get(_qi, [])
        ]
        source_id_by_url = {
            record["canonical_url"]: record["source_id"]
            for record in _source_records_by_query.get(_qi, [])
        }
        citation_ids_by_url: dict[str, list[str]] = {}
        evidence_ids_by_url: dict[str, str] = {}
        # A claim's date is the provider's date of the citation it binds to,
        # never a model guess (an extractor asked for one invents it).
        citation_date_by_id: dict[str, str] = {}
        for record in _citation_records_by_query.get(_qi, []):
            _date = " ".join(str(record.get("source_date", "") or "").split())
            if _date and _date.lower() != "unknown":
                citation_date_by_id[str(record.get("citation_id", "") or "")] = _date
            citation_ids_by_url.setdefault(record["canonical_url"], []).append(
                record["citation_id"]
            )
            evidence_ids_by_url[record["canonical_url"]] = evidence_id_for_citation(
                query_id, record
            )
        query_claim_entries: list[dict[str, Any]] = []
        for claim in extracted_claims:
            claim_text = str(claim.get("claim_text", "")).strip()
            if len(claim_text) < 12:
                continue
            if not strategies.claim_consolidation.claim_matches_focus_stems(claim_text, focus_stems):
                continue
            signature = str(claim.get("signature", "")).strip(
            ) or strategies.claim_consolidation.claim_signature(claim_text)
            if not signature:
                continue
            source_urls = [
                normalize_url(u)
                for u in claim.get("source_urls", [])
                if u
            ][: tuning.claim_source_url_cap]
            source_ids = [
                source_id_by_url[url]
                for url in source_urls
                if url in source_id_by_url
            ]
            citation_ids: list[str] = []
            for url in source_urls:
                citation_ids.extend(citation_ids_by_url.get(url, []))
            evidence_ids = [
                evidence_ids_by_url[url]
                for url in source_urls
                if url in evidence_ids_by_url
            ]
            raw_claim_id = make_record_id(
                "raw_claim",
                s.get("_run_id", ""),
                s["round"],
                query_id,
                signature,
                kept_claims,
            )
            entry = {
                "raw_claim_id": raw_claim_id,
                "claim_text": claim_text,
                "evidence_snippet": str(claim.get("evidence_snippet", "")).strip(),
                "claim_type": str(claim.get("claim_type", "fact")),
                "polarity": str(claim.get("polarity", "affirmed")),
                "needs_primary": bool(claim.get("needs_primary", False)),
                "provider_refs": list(claim.get("provider_refs", []) or []),
                "source_urls": source_urls,
                "source_ids": source_ids,
                "citation_ids": citation_ids,
                "evidence_ids": evidence_ids,
                "binding_status": str(claim.get("binding_status", "unbound")),
                "verification_status": "unverified",
                "verification_basis": "",
                "supporting_evidence_ids": [],
                "supporting_domain_count": 0,
                "published_date": _citation_published_date(citation_ids, citation_date_by_id),
                "signature": signature,
                "round": s["round"],
                "query": new_q[_qi] if _qi < len(new_q) else "",
                "query_id": query_id,
            }
            query_claim_entries.append(entry)
            kept_claims += 1
            _kept_claim_ids.append(raw_claim_id)
            _append_forensic_event(
                s,
                settings,
                event="claim_record",
                node="search",
                payload={
                    "raw_claim_id": raw_claim_id,
                    "query_id": query_id,
                    "signature": signature,
                    "claim_text": claim_text,
                    "evidence_snippet": entry["evidence_snippet"],
                    "claim_type": entry["claim_type"],
                    "polarity": entry["polarity"],
                    "needs_primary": entry["needs_primary"],
                    "source_ids": source_ids,
                    "citation_ids": citation_ids,
                    "provider_refs": entry["provider_refs"],
                    "binding_status": entry["binding_status"],
                    "evidence_ids": evidence_ids,
                    "source_urls": source_urls,
                    "published_date": entry["published_date"],
                    "round": s["round"],
                },
            )

        s.setdefault("query_synthesis", {})[query_id] = {
            "query": new_q[_qi] if _qi < len(new_q) else "",
            "round": s["round"],
            "provider_answer": r.answer,
            "citation_urls_by_rank": {
                str(src.rank): src.url
                for src in r.sources
                if src.rank and src.url
            },
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
        }
        _append_forensic_event(
            s,
            settings,
            event="query_synthesis",
            node="search",
            payload={
                "query_id": query_id,
                **s["query_synthesis"][query_id],
            },
        )

        evidence_records = assemble_evidence_records(
            query_id=query_id,
            query=new_q[_qi] if _qi < len(new_q) else "",
            provider=_search_provider_label,
            source_records=_source_records_by_query.get(_qi, []),
            citation_records=_citation_records_by_query.get(_qi, []),
            claim_entries=query_claim_entries,
        )
        s["evidence_ledger"] = merge_evidence_records(
            s.get("evidence_ledger", []),
            evidence_records,
        )
        for evidence_record in evidence_records:
            _append_forensic_event(
                s,
                settings,
                event="evidence_record",
                node="search",
                payload=evidence_record,
            )

        if _collect_query_details:
            _entry = dict(_query_details[_qi]) if _qi < len(_query_details) else {
                "query": new_q[_qi] if _qi < len(new_q) else "?",
            }
            _entry["claims_extracted"] = len(extracted_claims)
            _entry["claims_kept"] = kept_claims
            _entry["claim_extraction_valid_empty"] = (
                _qi in _claim_results and _qi not in _claim_warnings and not extracted_claims
            )
            if _qi in _claim_metadata:
                _metadata = _claim_metadata[_qi]
                _entry["claim_extraction_mode"] = str(
                    _metadata.get("claim_extraction_mode") or ""
                )
                _entry["claim_extraction_schema"] = str(
                    _metadata.get("claim_extraction_schema") or ""
                )
                _entry["claim_extraction_structured_supported"] = bool(
                    _metadata.get("claim_extraction_structured_supported")
                )
                for _count_key in (
                    "claim_extraction_raw_claim_count",
                    "claim_extraction_normalized_claim_count",
                    "claim_extraction_filtered_claim_count",
                    "unknown_provider_ref_count",
                    "unbound_claim_count",
                    "provider_refs_rebound",
                    "provider_refs_unchecked",
                ):
                    if _count_key in _metadata:
                        _entry[_count_key] = int(_metadata.get(_count_key, 0) or 0)
            _entry["evidence_record_count"] = len(evidence_records)
            _entry["evidence_context_source_count"] = sum(
                len(record.get("citation_set", []) or [])
                for record in evidence_records
            )
            _entry["source_ids"] = [
                record["source_id"] for record in _source_records_by_query.get(_qi, [])
            ]
            _entry["citation_ids"] = [
                record["citation_id"] for record in _citation_records_by_query.get(_qi, [])
            ]
            if _qi in _claim_warnings:
                _entry["claim_notice"] = _claim_warnings[_qi]
            if extracted_claims:
                _entry["claims_sample"] = [
                    str(c.get("claim_text", "")).strip()
                    for c in extracted_claims[:3]
                    if str(c.get("claim_text", "")).strip()
                ]
            _sources_summary.append(_entry)
            _append_forensic_event(
                s,
                settings,
                event="query_summary",
                node="search",
                payload=_entry,
            )
    # Refresh retained evidence before projecting the claim ledger: the
    # projection copies tiers and must include judgements from this round.
    _refresh_ledger_placement(s)
    claim_ledger = derive_claim_ledger_from_evidence(
        s.get("evidence_ledger", []) or [],
    )
    if len(claim_ledger) > tuning.claim_ledger_cap:
        _dropped_claims = claim_ledger[:-tuning.claim_ledger_cap]
        _ledger_dropped_total += len(_dropped_claims)
        s.setdefault("evidence_omissions", []).append(
            {
                "stage": "claim",
                "record_ids": [
                    str(item.get("raw_claim_id", "") or "")
                    for item in _dropped_claims
                    if item.get("raw_claim_id")
                ],
                "count": len(_dropped_claims),
                "reason": "claim_ledger_projection_budget",
            }
        )
        claim_ledger = claim_ledger[-tuning.claim_ledger_cap:]

    emit_progress(
        s,
        t(
            s,
            "search_sources_processed",
            n=sources_found,
            citations=len(s["all_citations"]),
            evidence=len(s.get("evidence_ledger", []) or []),
        ),
    )

    # Source quality over the pinned tiers of the run's registry (structural
    # floor or extractor judgement); the strategy covers only a citation the
    # run never registered as a source.
    _pinned_tier_by_url = pinned_tiers_from_records(s.get("source_records", {}) or {})
    tier_counts, quality_score = quality_from_tiers(
        _pinned_tier_by_url.get(url) or strategies.source_tiering.tier_for_url(url)
        for url in s["all_citations"]
    )
    s["source_tier_counts"] = tier_counts
    s["source_quality_score"] = quality_score
    consolidated_claims = strategies.claim_consolidation.consolidate(claim_ledger)
    s["consolidated_claims_full"] = consolidated_claims
    materialized_claims = strategies.claim_consolidation.materialize(
        consolidated_claims,
        max_total=tuning.materialize_max_total,
        max_unverified=tuning.materialize_max_unverified,
    )
    if len(consolidated_claims) > len(materialized_claims):
        visible_ids = {
            str(item.get("claim_id", "") or "")
            for item in materialized_claims
        }
        omitted_claim_ids = [
            str(item.get("claim_id", "") or "")
            for item in consolidated_claims
            if str(item.get("claim_id", "") or "") not in visible_ids
        ]
        s.setdefault("evidence_omissions", []).append(
            {
                "stage": "prompt",
                "record_ids": omitted_claim_ids,
                "count": len(omitted_claim_ids),
                "reason": "claim_materialization_budget",
            }
        )
    s["consolidated_claims"] = materialized_claims
    claim_counts, claim_quality, np_total, np_supported = strategies.claim_consolidation.quality_metrics(
        consolidated_claims)
    s["claim_status_counts"] = claim_counts
    s["claim_quality_score"] = claim_quality
    s["claim_needs_primary_total"] = np_total
    s["claim_needs_primary_supported"] = np_supported
    s["evidence_ledger"] = project_claim_verification_to_evidence(
        s.get("evidence_ledger", []),
        consolidated_claims,
    )
    # Verification standing counts read directly from the consolidated claim
    # ledger -- the run maintains no parallel report-evidence-bundle list.
    # The EvidenceLedger plus its projected verification fields is the
    # single source of truth for both the research loop and the final answer.
    _verified_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "verified"
    )
    _attributed_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "attributed"
    )
    _contested_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "contested"
    )
    _cross_checked_claim_count = sum(
        1 for claim in consolidated_claims
        if claim.get("verification_basis") == "cross_checked"
    )
    _primary_supported_claim_count = sum(
        1 for claim in consolidated_claims
        if claim.get("status") in SUPPORTED_STATUSES
        and claim.get("verification_basis") == "primary_source"
    )
    _single_source_count = sum(
        1 for claim in consolidated_claims
        if claim.get("status") in SUPPORTED_STATUSES and _claim_citation_count(claim) < 2
    )
    _report_eligible_count = sum(
        1 for record in s.get("evidence_ledger", []) or []
        if record.get("report_eligible")
    )
    evidence_depth_gap = _evidence_depth_gap(s)
    s["evidence_depth_gap"] = evidence_depth_gap
    s["_evidence_depth_gap_active"] = bool(evidence_depth_gap.get("active"))
    _append_forensic_event(
        s,
        settings,
        event="evidence_verification_projection",
        node="search",
        payload={
            "evidence_count": len(s.get("evidence_ledger", [])),
            "verified_claim_supports": sum(
                1
                for record in s.get("evidence_ledger", [])
                for claim in record.get("claims", []) or []
                if claim.get("verification_status") == "verified"
            ),
            "attributed_claim_supports": sum(
                1
                for record in s.get("evidence_ledger", [])
                for claim in record.get("claims", []) or []
                if claim.get("verification_status") == "attributed"
            ),
            "contested_claim_supports": sum(
                1
                for record in s.get("evidence_ledger", [])
                for claim in record.get("claims", []) or []
                if claim.get("verification_status") == "contested"
            ),
            "unverified_claim_supports": sum(
                1
                for record in s.get("evidence_ledger", [])
                for claim in record.get("claims", []) or []
                if claim.get("verification_status") == "unverified"
            ),
            "supporting_evidence_link_count": sum(
                len(claim.get("supporting_evidence_ids", []) or [])
                for record in s.get("evidence_ledger", [])
                for claim in record.get("claims", []) or []
            ),
        },
    )
    _append_forensic_event(
        s,
        settings,
        event="evidence_selection",
        node="search",
        payload={
            "consolidated_claim_count": len(consolidated_claims),
            "materialized_claim_count": len(materialized_claims),
            "verified_claim_count": _verified_claim_count,
            "attributed_claim_count": _attributed_claim_count,
            "primary_supported_claim_count": _primary_supported_claim_count,
            "contested_claim_count": _contested_claim_count,
            "cross_checked_claim_count": _cross_checked_claim_count,
            "single_source_count": _single_source_count,
            "report_eligible_evidence_count": _report_eligible_count,
            "evidence_depth_gap": evidence_depth_gap,
        },
    )
    for claim in consolidated_claims:
        _append_forensic_event(
            s,
            settings,
            event="claim_merge",
            node="search",
            payload={
                "claim_id": claim.get("claim_id", ""),
                "signature": claim.get("signature", ""),
                "member_claim_ids": claim.get("member_claim_ids", []),
                "status": claim.get("status", ""),
                "status_reason": claim.get("status_reason", ""),
                "verification_basis": claim.get("verification_basis", ""),
                "evidence_snippets": claim.get("evidence_snippets", []),
                "support_count": claim.get("support_count", 0),
                "supporting_evidence_ids": claim.get("supporting_evidence_ids", []),
                "supporting_domain_count": claim.get("supporting_domain_count", 0),
                "contradicting_evidence_ids": claim.get("contradicting_evidence_ids", []),
                "contradict_count": claim.get("contradict_count", 0),
                "values": claim.get("values", []),
                "conflicting_claim_ids": claim.get("conflicting_claim_ids", []),
                "source_ids": claim.get("source_ids", []),
                "citation_ids": claim.get("citation_ids", []),
                "source_urls": claim.get("source_urls", []),
                "round_first_seen": claim.get("round_first_seen", 0),
                "round_last_updated": claim.get("round_last_updated", 0),
            },
        )

    log.info(
        "TRACE search: round=%d query_count=%d sources_found=%d "
        "total_citations=%d evidence_records=%d claims=%d "
        "claim_quality=%.2f",
        s["round"], len(new_q), sources_found, len(s["all_citations"]),
        len(s.get("evidence_ledger", []) or []), len(consolidated_claims),
        claim_quality,
    )

    # Aspect coverage is estimated directly from the EvidenceLedger: each
    # report-eligible record's claims and snippet, plus the provider
    # synthesis, form the text corpus. There is no separate prunable context
    # channel -- the ledger keeps every record, and the answer-time overview
    # renderer applies the only budget.
    # Coverage is the evaluator's judgement (COVERED_ASPECTS in the next
    # evaluate call), not a substring scan over the evidence text: a token
    # like 'oder' would count a rubric label as covered.
    s["round"] += 1

    emit_progress(
        s,
        t(
            s,
            "search_source_quality",
            primary=int(tier_counts.get("primary", 0) or 0),
            mainstream=int(tier_counts.get("mainstream", 0) or 0),
            stakeholder=int(tier_counts.get("stakeholder", 0) or 0),
            unknown=int(tier_counts.get("unknown", 0) or 0),
            low=int(tier_counts.get("low", 0) or 0),
            quality=f"{quality_score:.2f}",
        ),
    )
    emit_progress(
        s,
        t(
            s,
            "search_quality_summary",
            verified_claims=_verified_claim_count,
            unverified_claims=max(
                0,
                len(consolidated_claims)
                - _verified_claim_count
                - _attributed_claim_count
                - _contested_claim_count,
            ),
            attributed_claims=_attributed_claim_count,
            cross_checked_claims=_cross_checked_claim_count,
        ),
    )
    _search_score_snapshot = append_score_snapshot(s, phase="search")
    _append_forensic_event(
        s,
        settings,
        event="score_snapshot",
        node="search",
        payload=_search_score_snapshot,
    )

    # Aggregate token usage from provider search and claim extraction.
    s["total_prompt_tokens"] += (
        _search_prompt_tokens
        + _claim_prompt_tokens
    )
    s["total_completion_tokens"] += (
        _search_completion_tokens
        + _claim_completion_tokens
    )

    append_iteration_log(s, {
        "node": "search",
        "timestamp": time.time(),
        "duration_s": round(time.monotonic() - _t0, 3),
        "round": s["round"] - 1,
        "worker_count": _n_workers,
        "queries_executed": len(new_q),
        "target_query_count": _batch,
        "queries": new_q,
        "query_record_ids": [record["query_id"] for record in _round_query_records],
        "search_parameters": {
            "search_context_size": search_kwargs.get("search_context_size") or "",
            "recency_filter": search_kwargs.get("recency_filter") or "",
            "language_filter": search_kwargs.get("language_filter", []),
            "search_mode": search_kwargs.get("search_mode") or "",
            "return_related": bool(search_kwargs.get("return_related")),
            "supported_parameters": sorted(search_capabilities.supported_parameters),
        },
        "search_fallbacks": _search_fallbacks,
        "claim_fallbacks": _claim_fallbacks,
        "_claim_extraction_fallback": _claim_fallbacks > 0,
        "claim_extraction_modes": _claim_mode_counts,
        "claim_extraction_attempts_total": s.get("_claim_extraction_attempts_total", 0),
        "claim_extraction_failures_total": s.get("_claim_extraction_failures_total", 0),
        "unknown_provider_ref_count": _claim_unknown_provider_ref_count,
        "unbound_claim_count": _claim_unbound_count,
        "_claim_unknown_provider_refs": _claim_unknown_provider_ref_count > 0,
        "_claim_unbound_claims": _claim_unbound_count > 0,
        "claim_valid_empty": _claim_valid_empty,
        "_claim_extraction_empty": _claim_valid_empty > 0,
        "algorithm_failure_count": len(s.get("algorithm_failures", []) or []),
        "blocking_algorithm_failure_count": len(_blocking_algorithm_failures(s)),
        "algorithm_failures": (s.get("algorithm_failures", []) or [])[-10:],
        "sources_found": sources_found,
        "total_citations": len(s["all_citations"]),
        "source_tier_counts": s.get("source_tier_counts", {}),
        "source_quality_score": s.get("source_quality_score", 0.0),
        "source_record_ids": [
            record["source_id"]
            for records in _source_records_by_query.values()
            for record in records
        ],
        "provider_citation_record_ids": [
            record["citation_id"]
            for records in _citation_records_by_query.values()
            for record in records
        ],
        "evidence_record_count": len(s.get("evidence_ledger", [])),
        "report_eligible_evidence_count": _report_eligible_count,
        "verified_claim_count": _verified_claim_count,
        "attributed_claim_count": _attributed_claim_count,
        "cross_checked_claim_count": _cross_checked_claim_count,
        "primary_supported_claim_count": _primary_supported_claim_count,
        "single_source_count": _single_source_count,
        "evidence_depth_gap": evidence_depth_gap,
        "claim_ledger_size": len(claim_ledger),
        "claim_record_ids": _kept_claim_ids,
        "claim_ledger_dropped": _ledger_dropped_total,
        "consolidated_claims_count": len(claim_assessments(s)),
        "claim_status_counts": s.get("claim_status_counts", {}),
        "claim_quality_score": s.get("claim_quality_score", 0.0),
        "claim_needs_primary_total": s.get("claim_needs_primary_total", 0),
        "claim_needs_primary_supported": s.get("claim_needs_primary_supported", 0),
        "aspect_coverage": s.get("aspect_coverage", 0.0),
        "uncovered_aspects": s.get("uncovered_aspects", []),
        "sources_summary": _sources_summary,
    }, testing_mode=settings.testing_mode)
    return s


@dataclass(slots=True)
class ConfidenceGuardrailResult:
    """Outcome of the post-evaluation confidence guardrails.

    Attributes:
        confidence: Clamped confidence after all guardrails ran.
        gap_suggestion: First gap text proposed by a guardrail, or
            ``None`` if no guardrail proposed one. The caller only
            applies it when ``s["gaps"]`` is still empty.
        reasons: Human-readable trace of each guardrail that actually
            changed the confidence value. Used for telemetry and log
            diagnostics.
    """

    confidence: int
    gap_suggestion: str | None
    reasons: list[str]


def _apply_evaluated_coverage(
    s: dict[str, Any], eval_text: str, *, evaluate_model: str
) -> None:
    """Read the evaluator's COVERED_ASPECTS and update the open sub-questions.

    The evaluator judges coverage (it sees every source in the compact
    view); before, a substring scan over the evidence text decided, so a
    rubric label with a common token counted as covered and a real
    sub-question with method words never did. A missing or unparsable
    line leaves the open list as it is and says so (WARNING + status
    line), never a silent "all covered".
    """
    aspects = list(s.get("required_aspects", []) or [])
    if not aspects:
        s["uncovered_aspects"] = []
        s["aspect_coverage"] = 1.0
        s["coverage_source"] = "none"
        return
    m = re.search(r"COVERED_ASPECTS:\s*(\[[^\]]*\])", eval_text)
    indices: Any = None
    if m:
        try:
            indices = json.loads(m.group(1))
        except ValueError:
            indices = None
    if not isinstance(indices, list):
        s["coverage_source"] = "missing" if not m else "unparsable"
        log.warning(
            "Evaluate: COVERED_ASPECTS %s (round=%d, model=%s); open subquestions "
            "remain open",
            s["coverage_source"],
            int(s.get("round", 0) or 0),
            evaluate_model,
        )
        emit_progress(s, t(s, "evaluate_coverage_missing"), severity="warning")
        return
    covered: set[int] = set()
    usable = 0
    for index in indices:
        if isinstance(index, bool):
            continue
        try:
            number = int(str(index).strip())
        except (TypeError, ValueError):
            continue
        usable += 1
        if 1 <= number <= len(aspects):
            covered.add(number)
    if indices and not usable:
        s["coverage_source"] = "unparsable"
        log.warning(
            "Evaluate: COVERED_ASPECTS has no usable indices %r (round=%d, model=%s); "
            "open subquestions remain open",
            indices[:6],
            int(s.get("round", 0) or 0),
            evaluate_model,
        )
        emit_progress(s, t(s, "evaluate_coverage_missing"), severity="warning")
        return
    s["uncovered_aspects"] = [
        aspect for position, aspect in enumerate(aspects, 1) if position not in covered
    ]
    s["aspect_coverage"] = round(len(covered) / len(aspects), 3)
    s["coverage_source"] = "llm"


def _citation_published_date(
    citation_ids: list[str], citation_date_by_id: dict[str, str]
) -> str:
    """The provider date of the first bound citation that has one, else ``unknown``."""
    for citation_id in citation_ids:
        date = citation_date_by_id.get(str(citation_id))
        if date:
            return date
    return "unknown"


def has_report_bundles(claims: list[dict[str, Any]]) -> bool:
    """Whether the consolidated *claims* give an answer something to stand
    on: a supported claim (verified or attributed) or a contested one. A
    provider that returns no source text attributes every claim; that is
    still a report basis, so the guardrail reads support, not checking."""
    return any(
        claim.get("status") in (*SUPPORTED_STATUSES, "contested")
        for claim in claims
    )


def contested_conflict_count(claims: list[dict[str, Any]]) -> int:
    """How many conflicts the consolidated *claims* hold: one per claim
    signature with a ``contested`` claim. A negation contests one claim of
    its signature, a value conflict both of its sides; either is one
    conflict."""
    return len({
        str(claim.get("signature", "") or "")
        for claim in claims
        if claim.get("status") == "contested"
    })


def apply_confidence_guardrails(
    conf: int,
    *,
    has_citations: bool,
    primary_n: int,
    mainstream_n: int,
    low_n: int,
    uncovered_aspects: list[str],
    contested_conflicts: int,
    needs_primary: bool,
    existing_gap: str,
    has_evidence_records: bool = False,
    has_claims: bool = True,
    has_report_bundles: bool = True,
    language: str = "",
) -> ConfidenceGuardrailResult:
    """Couple confidence to source quality and aspect coverage.

    Collects the five LLM-independent guardrails of ``evaluate`` in one
    place. The function is pure: it neither
    reads nor writes state, which makes the interaction between the
    individual clamps explicit and unit-testable.

    The gap suggestion follows the same first-writer-wins semantics as
    the legacy inline code: if the caller already has a gap message
    stored, no suggestion is emitted; otherwise the first guardrail
    whose condition fires proposes the gap text.

    Args:
        conf: Baseline confidence after LLM evaluation and prior
            stop-criteria strategies.
        has_citations: ``True`` when at least one citation URL is
            present in state.
        has_evidence_records: ``True`` when the search node created any
            structured EvidenceRecord rows.
        has_claims: ``True`` when claim consolidation produced at
            least one answer-facing claim.
        has_report_bundles: ``True`` when supported or contested report
            evidence is available for final answer grounding
            (:func:`has_report_bundles`).
        primary_n: Count of primary-tier citations.
        mainstream_n: Count of mainstream-tier citations.
        low_n: Count of low-tier citations.
        uncovered_aspects: Required aspects still uncovered after the
            latest round.
        contested_conflicts: Count of conflicts among the consolidated
            claims: claim signatures with at least one ``contested``
            claim. A value conflict contests both of its sides, so
            counting contested claims would count it twice.
        needs_primary: ``True`` when the extractor flagged claims as
            primary-needing and none of them is verified — the claims'
            own state; a primary source elsewhere in the citations does
            not verify them.
        existing_gap: Current ``state["gaps"]`` value. When truthy, no
            gap suggestion is emitted.

    Returns:
        ConfidenceGuardrailResult: Clamped confidence, optional gap
        suggestion, and a list of reason tags for each guardrail that
        changed the confidence.
    """
    reasons: list[str] = []
    gap_suggestion: str | None = None
    words = {"language": language or ""}

    def propose(text: str) -> None:
        nonlocal gap_suggestion
        if not existing_gap and gap_suggestion is None:
            gap_suggestion = text

    if not has_citations:
        new_conf = min(conf, 6)
        if new_conf != conf:
            reasons.append(f"no_citations:conf {conf}->{new_conf}")
            conf = new_conf
        propose("Keine belastbaren Quellen gefunden.")
    if has_citations and has_evidence_records and not has_claims:
        new_conf = min(conf, 5)
        if new_conf != conf:
            reasons.append(f"no_structured_evidence:conf {conf}->{new_conf}")
            conf = new_conf
        propose(t(words, "research_gap_no_structured_claims"))
    elif has_citations and has_claims and not has_report_bundles:
        new_conf = min(conf, 6)
        if new_conf != conf:
            reasons.append(f"no_report_bundles:conf {conf}->{new_conf}")
            conf = new_conf
        propose(t(words, "research_gap_no_report_bundles"))
    if low_n > (primary_n + mainstream_n) and conf > 7:
        reasons.append(f"low_quality_majority:conf {conf}->7")
        conf = 7
    if needs_primary and conf > 8:
        reasons.append(f"needs_primary_missing:conf {conf}->8")
        conf = 8
        propose(t(words, "research_gap_primary_missing"))
    if len(uncovered_aspects) > 0 and conf > 8:
        reasons.append(f"aspects_uncovered:conf {conf}->8")
        conf = 8
        propose(
            t(words, "research_gap_aspects_open", items=", ".join(uncovered_aspects[:2]))
        )
    if contested_conflicts >= 2 and conf > 7:
        reasons.append(f"contested_conflicts>=2:conf {conf}->7")
        conf = 7
        propose("Mehrere zentrale Aussagen sind zwischen Quellen umstritten.")

    return ConfidenceGuardrailResult(
        confidence=conf,
        gap_suggestion=gap_suggestion,
        reasons=reasons,
    )


# ======================================================================= #
# 4. evaluate
# ======================================================================= #


def evaluate(
    s: dict,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict:
    """Evaluate evidence quality, stop criteria, and remaining gaps.

    Args:
        s: Mutable AgentState-compatible dict. Reads accumulated
        evidence, claims, and prior confidence; writes quality
            metrics, stop decisions, and gap information for later rounds.
        providers: Active LLM and search providers.
        strategies: Runtime strategies for source quality, claim
            consolidation, risk coverage, and stop criteria.
        settings: Agent behavior settings controlling risk scoring and
            stopping thresholds.

    Returns:
        The mutated state dict with refreshed quality metrics and stop
        status.

    Raises:
        AgentRateLimited: Propagated when the evaluation model hard-fails
            on upstream rate limiting.

    Example:
        >>> evaluate(state, providers=providers, strategies=strategies, settings=settings)
        {'final_confidence': 8, 'done': True, ...}
    """
    check_cancel_event(s)
    emit_progress(
        s,
        t(s, "evaluate_start", round=s["round"], max_rounds=settings.max_rounds),
    )
    _t0 = time.monotonic()
    _stagnation_detected = False
    _evaluate_fallback: dict[str, Any] = {}
    _confidence_parsed = True
    _confidence_clamped = False
    if s.get("done"):
        s["_stop_reason"] = s.get("_stop_reason") or "already_done"
        append_iteration_log(s, {
            "node": "evaluate",
            "timestamp": time.time(),
            "duration_s": round(time.monotonic() - _t0, 3),
            "confidence": s.get("final_confidence", 0),
            "skipped": "already_done",
            "_stop_reason": s.get("_stop_reason", "already_done"),
        }, testing_mode=settings.testing_mode)
        return s
    try:
        _check_deadline(s["deadline"])
    except AgentTimeout:
        s["done"] = True
        s["_stop_reason"] = "deadline_exceeded"
        append_iteration_log(s, {
            "node": "evaluate",
            "timestamp": time.time(),
            "duration_s": round(time.monotonic() - _t0, 3),
            "confidence": s.get("final_confidence", 0),
            "skipped": "deadline_exceeded",
            "_stop_reason": s.get("_stop_reason", "deadline_exceeded"),
        }, testing_mode=settings.testing_mode)
        return s

    # Read metrics that the search node already computed for this round.
    # Re-running quality_from_urls / consolidate / materialize / quality_metrics
    # here would duplicate work without changing the result: nothing between
    # the end of search and the start of evaluate mutates citations, the
    # claim_ledger, or the materialize tuning.
    tuning = settings.report_tuning
    tier_counts: dict[str, int] = s.get("source_tier_counts", {}) or {}
    quality_score = float(s.get("source_quality_score", 0.0) or 0.0)
    consolidated_claims = claim_assessments(s)
    claim_counts: dict[str, int] = s.get("claim_status_counts", {}) or {}
    claim_quality = float(s.get("claim_quality_score", 0.0) or 0.0)
    claim_np_total = int(s.get("claim_needs_primary_total", 0) or 0)
    claim_np_supported = int(s.get("claim_needs_primary_supported", 0) or 0)
    evidence_depth_gap = _evidence_depth_gap(s)
    s["evidence_depth_gap"] = evidence_depth_gap
    s["_evidence_depth_gap_active"] = bool(evidence_depth_gap.get("active"))
    report_eligible_evidence_count = sum(
        1
        for record in s.get("evidence_ledger", []) or []
        if record.get("report_eligible")
    )
    # The evaluator judges breadth: every report-eligible source in the
    # compact view (header + claim lines, no passages) under the profile's
    # evaluator budget, sized to hold the whole ledger of that profile. A
    # full-block excerpt would show the evaluator a fraction of what the
    # writer saw, so "GAPS" would name sources the run already had.
    _evaluate_budget_chars = tuning.evaluate_evidence_total_char_budget
    _evaluate_overview = render_evidence_ledger_overview(
        s.get("evidence_ledger", []) or [],
        max_total_chars=_evaluate_budget_chars,
        max_record_chars=tuning.evaluate_evidence_record_char_limit,
        query_synthesis=s.get("query_synthesis", {}),
        record_view=RECORD_VIEW_COMPACT,
    )
    evaluate_evidence_overview = _evaluate_overview.markdown
    _trace_evidence_overview(
        _evaluate_overview, budget_chars=_evaluate_budget_chars, view="evaluate"
    )

    evaluate_model, evaluate_effort = _resolve_node_llm(s, settings, providers, "evaluate")
    # The stop cascade caps against the RUN's stop threshold (profile
    # override applied), not the value the shared strategy was built with.
    s["_confidence_stop"] = int(settings.confidence_stop)

    # Previous-round context block (confidence stability):
    # Surfaces the evaluator's own prior CONFIDENCE / GAPS to the LLM so the
    # next bewertung is a delta against the last run, not an isolated fresh
    # rating. The evaluate format instruction trains the model to keep
    # the value monotone unless new contradictions or competing events
    # surface; this block provides the comparison anchor.
    previous_round_hint = ""
    if s["round"] >= 1:
        _prev_conf_for_hint = int(s.get("final_confidence", 0) or 0)
        if _prev_conf_for_hint > 0:
            _prev_gaps_for_hint = (s.get("gaps", "") or "").strip() or "None"
            _new_citations_for_hint = max(
                0,
                len(s.get("all_citations", []))
                - int(s.get("prev_citation_count", 0) or 0),
            )
            previous_round_hint = (
                "\n\nPREVIOUS ROUND CONTEXT:\n"
                f"PREVIOUS ROUND (round {s['round'] - 1}): "
                f"CONFIDENCE={_prev_conf_for_hint}, GAPS=\"{_prev_gaps_for_hint}\".\n"
                f"In this round {_new_citations_for_hint} new sources "
                f"were added.\n"
            )

    # Repeated, independent Azure searches can make absence visible. The
    # evaluator must still phrase that conclusion according to the observed
    # provider results rather than claiming an exhaustive crawl.
    negative_evidence_hint = ""
    if s["round"] >= 2:
        _prev_conf = s.get("final_confidence", 0)
        _n_citations = len(s.get("all_citations", []))
        negative_evidence_hint = (
            "\n\nIMPORTANT — NEGATIVE EVIDENCE:\n"
            f"{s['round']} search rounds with {len(s['queries'])} queries have already been run "
            f"and {_n_citations} sources collected.\n"
        )
        if _prev_conf > 0 and _prev_conf <= 4:
            negative_evidence_hint += (
                f"The confidence in the previous round was also only {_prev_conf}/10.\n"
                f"When nothing substantial has improved despite {_n_citations} searched sources, "
                f"that is a STRONG signal: the premise of the question is probably FALSE.\n"
                f"In that case set CONFIDENCE to 7-9 — 'it does not exist' is a "
                f"high-confidence finding after extensive research.\n"
            )
        negative_evidence_hint += (
            "When the research CONSISTENTLY finds no evidence for the claim/assumption in the question, "
            "that IS a result. 'It does not exist' or 'the assumption is false' "
            "are valid answers with HIGH confidence (7-9).\n"
            "So judge not only whether you found WHAT was asked, "
            "but also whether you searched enough to say with certainty that it does NOT exist.\n"
        )

    # --- LLM call: evaluate information quality ---
    _eval_raw = ""
    quality_hint = (
        "\nSOURCE QUALITY:\n"
        f"- primary={tier_counts.get('primary', 0)}, "
        f"mainstream={tier_counts.get('mainstream', 0)}, "
        f"stakeholder={tier_counts.get('stakeholder', 0)}, "
        f"unknown={tier_counts.get('unknown', 0)}, "
        f"low={tier_counts.get('low', 0)}.\n"
        f"- Overall quality score: {quality_score:.2f} (0-1).\n"
        "- When central statements are supported only by stakeholder or low-quality sources, "
        "reduce CONFIDENCE and set GAPS accordingly.\n"
        "- Separate strictly between neutral fact and actor claim "
        "(e.g. a party organisation, trade association, lobby group).\n\n"
    )
    aspect_hint = ""
    required_aspects_list = list(s.get("required_aspects", []) or [])
    if required_aspects_list:
        numbered = "\n".join(
            f"{index}. {aspect}" for index, aspect in enumerate(required_aspects_list, 1)
        )
        aspect_hint = (
            "SUB-QUESTION COVERAGE:\n"
            f"{numbered}\n"
            "- Judge per sub-question whether the research results answer it directly, "
            "and name the numbers of the answered ones in COVERED_ASPECTS.\n"
            "- When a sub-question is open, STATUS cannot be SUFFICIENT.\n\n"
        )
    event_mode = bool(s.get("event_mode", False))
    # The temporal-consistency rule keys on the question's own wording
    # ("vor kurzem") and applies to every question; only the competing-
    # events mechanism is an event-question mechanism.
    temporal_hint = (
        f"TEMPORAL CONSISTENCY:\n"
        f"- When the question contains relative time references ('recently', 'lately', 'the other day'), "
        f"check whether the events found fit today's date ({today()}) in time.\n"
        f"- An event from 12+ months ago is NOT 'recent'.\n"
        f"- When the events found do not fit in time, set GAPS "
        f"to 'a more recent event was not found' and CONFIDENCE to at most 5.\n\n"
    )
    event_hint = (
        (
            "SEVERAL MATCHING EVENTS:\n"
            "- When several events could match the description, "
            "set GAPS to 'possibly a different or more recent event is meant' "
            "and reduce CONFIDENCE.\n\n"
            "COMPETING EXPLANATIONS:\n"
            "- When you find DIFFERENT possible events/answers in the research results "
            "that could match the question, list them.\n"
            "- Reply in the COMPETING_EVENTS line with a short list: "
            "'event A (date) vs event B (date)' or 'None'.\n\n"
        )
        if event_mode
        else ""
    )
    claim_hint = (
        "CLAIM LEDGER:\n"
        f"- verified={claim_counts.get('verified', 0)}, "
        f"attributed={claim_counts.get('attributed', 0)}, "
        f"contested={claim_counts.get('contested', 0)}, "
        f"unverified={claim_counts.get('unverified', 0)}.\n"
        "- verified: a source's own text was checked and states the claim. "
        "attributed: the search provider's answer attributes the claim to its "
        "sources and no source text was checked. Attributed claims suffice for "
        "an answer that attributes them to their sources; on their own they are "
        "no reason for INSUFFICIENT.\n"
        f"- Claim quality score: {claim_quality:.2f} (0-1).\n"
        f"- Claims requiring a primary source, supported: {claim_np_supported}/{claim_np_total}.\n"
        "- When central claims are contested/unverified, reduce CONFIDENCE and "
        "set STATUS to INSUFFICIENT.\n"
        "- The counters refer to the complete assessment. The following "
        "claim list is a bounded selection; the evidence overview remains the source base:\n"
        + strategies.claim_consolidation.claims_prompt_view(
            s.get("consolidated_claims", []) or [],
            max_items=tuning.answer_claim_prompt_items,
        )
        + "\n\n"
    )
    evidence_depth_hint = ""
    if evidence_depth_gap.get("active"):
        evidence_depth_hint = (
            "EVIDENCE DEPTH GAP:\n"
            f"- Diagnosis: {evidence_depth_gap.get('reason', '')}.\n"
            f"- Supported claims: {evidence_depth_gap['supported_count']}, "
            f"independently confirmed claims: {evidence_depth_gap['cross_checked_count']}, "
            f"single-source: {evidence_depth_gap['single_source_count']}.\n"
            "- When central statements are predominantly single-source, STATUS is "
            "SUFFICIENT only when the remaining uncertainty is explicitly small. Otherwise set "
            "GAPS to independent cross-checks or primary sources.\n\n"
        )
    try:
        eval_prompt = (
            f"{date_line(today())}\n\n"
            f"Judge whether the researched information is sufficient "
            f"to answer the question completely and correctly.\n\n"
            + temporal_hint
            + event_hint
            + quality_hint
            + aspect_hint
            + claim_hint
            + evidence_depth_hint
            + f"Question: {s['question']}\n\n"
            + "Research results (evidence overview):\n"
            + (evaluate_evidence_overview or "(no citable evidence yet)")
            + "\n"
            + previous_round_hint
            + negative_evidence_hint
            + build_evaluate_format_suffix(
                event_mode=event_mode, aspect_count=len(required_aspects_list)
            )
        )
        with _provider_retry_progress_context(
            providers.llm,
            s,
            operation_label=t(s, "retry_operation_evaluate"),
            testing_mode=settings.testing_mode,
        ):
            a = providers.llm.complete(
                eval_prompt,
                deadline=s["deadline"],
                model=evaluate_model,
                reasoning_effort=evaluate_effort,
                timeout=settings.reasoning_timeout,
                state=s,
            )
        _eval_raw = a

        # --- Parse base values from LLM response ---
        m_conf = re.search(r"CONFIDENCE:\s*(\d+)", a)
        if m_conf:
            # The evaluator is asked for 0-10, but nothing downstream enforces
            # that: every guardrail below is a conditional min() cap, and
            # ResearchMetrics.confidence carries no range. An unclamped
            # "CONFIDENCE: 42" would satisfy conf >= confidence_stop on the
            # spot and end the run with a fabricated metric in the report.
            _conf_raw = int(m_conf.group(1))
            conf = max(0, min(10, _conf_raw))
            if conf != _conf_raw:
                _confidence_clamped = True
                log.warning(
                    "Evaluate parse warning: CONFIDENCE outside 0-10 (raw=%d, round=%d,"
                    " model=%s); clamped to %d",
                    _conf_raw, s["round"], evaluate_model, conf,
                )
                emit_progress(
                    s,
                    t(s, "evaluate_confidence_out_of_range",
                      raw=_conf_raw, clamped=conf),
                    severity="warning",
                )
        else:
            conf = 5
            _confidence_parsed = False
            log.warning(
                "Evaluate parse warning: CONFIDENCE field missing in LLM response "
                "(round=%d, model=%s); using default 5",
                s["round"], evaluate_model,
            )
            emit_progress(s, t(s, "evaluate_confidence_missing"), severity="warning")

        m_gaps = re.search(r"GAPS:\s*(.+?)(?:\n|$)", a)
        gaps = m_gaps.group(1).strip() if m_gaps else ""
        s["gaps"] = "" if is_none_value(gaps) else gaps
        if evidence_depth_gap.get("active") and not s.get("gaps"):
            s["gaps"] = str(evidence_depth_gap.get("gap", "") or "")

        m_status = re.search(r"STATUS:\s*(SUFFICIENT|INSUFFICIENT)", a, re.IGNORECASE)
        s["evaluate_status"] = m_status.group(1).lower() if m_status else "unparsed"
        _apply_evaluated_coverage(s, a, evaluate_model=evaluate_model)

        # --- Apply heuristics ---
        conf = strategies.stop_criteria.check_contradictions(s, a, conf)
        conf = strategies.stop_criteria.extract_competing_events(s, a, conf)
        conf = strategies.stop_criteria.extract_evidence_scores(s, a, conf)

    except AgentRateLimited:
        raise
    except (
        OpenAIError,
        AgentTimeout,
        AnthropicAPIError,
        AzureOpenAIAPIError,
        BedrockAPIError,
    ) as exc:
        # No fail-open: on evaluate error stay conservative.
        _exc_label = type(exc).__name__
        log.warning(
            "Evaluate fallback activated (error_code=%s, round=%d, model=%s)",
            _exc_label, s["round"], evaluate_model,
        )
        emit_progress(s, t(s, "evaluate_failed", label=_exc_label), severity="warning")
        s["evaluate_status"] = "fallback"
        s["coverage_source"] = "fallback"
        conf = min(max(s.get("final_confidence", 0), 5), settings.confidence_stop - 2)
        _confidence_parsed = False
        if not s.get("gaps"):
            s["gaps"] = t(s, "research_gaps_evaluation_incomplete")
        _evaluate_fallback = {
            "fallback": "evaluate_default",
            "fallback_reason": _exc_label,
            "fallback_message": str(exc)[:300],
        }

    # Guardrails: couple confidence to source quality and aspect coverage.
    # "Primary source needed" is the extractor's per-claim judgement (numbers
    # with a unit, statutes, official decisions) with none of them supported —
    # not a word list over the question, which fired on every cost question.
    # Support, verified or attributed alike: the guardrail must not fire only
    # because a provider returns no source text.
    needs_primary = (
        int(s.get("claim_needs_primary_total", 0) or 0) > 0
        and int(s.get("claim_needs_primary_supported", 0) or 0) == 0
    )
    primary_n = int(tier_counts.get("primary", 0))
    mainstream_n = int(tier_counts.get("mainstream", 0))
    low_n = int(tier_counts.get("low", 0))
    verified_claims = int(claim_counts.get("verified", 0))
    attributed_claims = int(claim_counts.get("attributed", 0))
    contested_claims = int(claim_counts.get("contested", 0))
    unverified_claims = int(claim_counts.get("unverified", 0))

    _guardrail_result = apply_confidence_guardrails(
        conf,
        has_citations=bool(s.get("all_citations")),
        has_evidence_records=bool(s.get("evidence_ledger")),
        has_claims=bool(consolidated_claims),
        has_report_bundles=has_report_bundles(consolidated_claims),
        primary_n=primary_n,
        mainstream_n=mainstream_n,
        low_n=low_n,
        uncovered_aspects=list(s.get("uncovered_aspects", [])),
        contested_conflicts=contested_conflict_count(consolidated_claims),
        needs_primary=needs_primary,
        existing_gap=s.get("gaps", "") or "",
        language=s.get("language", "") or "",
    )
    conf = _guardrail_result.confidence
    if _guardrail_result.gap_suggestion and not s.get("gaps"):
        s["gaps"] = _guardrail_result.gap_suggestion
    if _guardrail_result.reasons:
        log.info(
            "TRACE evaluate: guardrail_reasons=%s",
            _guardrail_result.reasons,
        )
    if (
        bool(s.get("all_citations"))
        and bool(s.get("evidence_ledger"))
        and not consolidated_claims
    ):
        s["evidence_sufficiency"] = min(int(s.get("evidence_sufficiency", 0) or 0), 3)

    # --- Post-LLM stop heuristics ---
    _prev_conf = s.get("final_confidence", 0)
    _n_citations = len(s.get("all_citations", []))

    _falsification_just_triggered = (
        strategies.stop_criteria.check_falsification(
            s, conf, _prev_conf
        )
    )
    conf, _stagnation_detected = strategies.stop_criteria.check_stagnation(
        s, conf, _prev_conf, _n_citations, _falsification_just_triggered)
    _utility, _utility_stop = strategies.stop_criteria.compute_utility(
        s, conf, _prev_conf, _n_citations)
    _done_after_utility = bool(s.get("done"))

    s["final_confidence"] = conf

    # Diagnostic snapshot for `confidence_unjustified_drop`:
    # capture prev_competing_events BEFORE check_plateau rotates it. The marker
    # surfaces when the LLM lowered CONFIDENCE without naming a new
    # CONTRADICTION or COMPETING_EVENT to justify it — a sign that the
    # evaluate-format stability instruction was not honoured by the
    # current model.
    _competing_unchanged_for_marker = (
        (s.get("competing_events") or "").strip()
        == (s.get("prev_competing_events") or "").strip()
    )

    _plateau_stop = strategies.stop_criteria.check_plateau(
        s, conf, _prev_conf, _stagnation_detected)
    _done_after_plateau = bool(s.get("done"))

    _m_contra_for_marker = re.search(
        r"CONTRADICTIONS:\s*([^\n]+)", _eval_raw or "", flags=re.IGNORECASE,
    )
    _contra_lead_token_for_marker = ""
    if _m_contra_for_marker:
        _contra_text = _m_contra_for_marker.group(1).strip()
        if _contra_text:
            _contra_lead_token_for_marker = (
                _contra_text.split()[0].rstrip(".:,;").lower()
            )
    _contradictions_present_for_marker = _contra_lead_token_for_marker not in {
        "", "nein", "no", "keine", "none", "k.a.", "n/a",
    }
    _confidence_unjustified_drop = bool(
        not _evaluate_fallback
        and int(_prev_conf or 0) > 0
        and int(conf) < int(_prev_conf)
        and _competing_unchanged_for_marker
        and not _contradictions_present_for_marker
    )
    if _confidence_unjustified_drop:
        log.warning(
            "TRACE evaluate: confidence drop without new contradictions/"
            "competing-events (prev=%d, curr=%d, round=%d)",
            int(_prev_conf), int(conf), s["round"],
        )

    _stop_cascade: dict[str, Any] = {
        "confidence": conf,
        "confidence_stop_target": settings.confidence_stop,
        "round": s["round"],
        "max_rounds": settings.max_rounds,
        "min_rounds": max(1, int(getattr(settings, "min_rounds", 1) or 1)),
        "utility_score": _utility,
        "utility_stop": _utility_stop,
        "plateau_stop": _plateau_stop,
        "stagnation_detected": _stagnation_detected,
        "falsification_triggered": bool(s.get("falsification_triggered", False)),
        "done_after_utility": _done_after_utility,
        "done_after_plateau": _done_after_plateau,
        "confidence_stop": conf >= settings.confidence_stop,
        "round_limit": s["round"] >= settings.max_rounds,
        "evidence_depth_gap_active": bool(evidence_depth_gap.get("active")),
        "evidence_depth_gap": evidence_depth_gap,
        "report_eligible_evidence_count": report_eligible_evidence_count,
        "min_report_eligible_evidence": tuning.min_report_eligible_evidence,
    }

    def _resolve_stop_reason() -> str:
        if _utility_stop:
            return "utility_stop"
        if _plateau_stop:
            return "plateau_stop"
        if conf >= settings.confidence_stop:
            return "confidence_stop"
        if s["round"] >= settings.max_rounds:
            return "round_limit"
        if s.get("done"):
            return "strategy_done"
        return ""

    # --- Final stop logic ---
    log.info(
        "TRACE evaluate: round=%d confidence=%d/%d gap_chars=%d evidence_records=%d "
        "quality=%.2f claim_quality=%.2f claims(v/a/c/u)=%d/%d/%d/%d model=%s done=%s",
        s["round"], conf, settings.confidence_stop,
        len(str(s.get("gaps", "") or "")),
        len(s.get("evidence_ledger", []) or []),
        quality_score, claim_quality,
        verified_claims, attributed_claims, contested_claims, unverified_claims,
        evaluate_model,
        conf >= settings.confidence_stop or s["round"] >= settings.max_rounds or s["done"],
    )
    emit_progress(
        s,
        t(
            s,
            "evaluate_quality_summary",
            source_quality=f"{quality_score:.2f}",
            claim_quality=f"{claim_quality:.2f}",
            evidence_records=len(s.get("evidence_ledger", []) or []),
            open_aspects=len(s.get("uncovered_aspects", []) or []),
        ),
    )

    _resolved_stop_reason = _resolve_stop_reason()
    if s["done"] or conf >= settings.confidence_stop or s["round"] >= settings.max_rounds:
        s["done"] = True
        s["_stop_reason"] = _resolved_stop_reason or "done"
        emit_progress(
            s,
            t(s, "research_finished", conf=conf, round=s["round"]),
        )
    else:
        s["_stop_reason"] = ""
        emit_progress(s, t(s, "confidence_continue", conf=conf))

    # min_rounds enforcement: if any earlier stop heuristic flipped done=True
    # but the configured min_rounds floor is not reached yet AND we are still
    # below max_rounds, suppress the stop. ``max_rounds`` always wins so a
    # mis-configured ``min_rounds > max_rounds`` cannot extend the loop
    # beyond the user-specified hard cap. Greift einheitlich für alle
    # Stop-Heuristiken (confidence, plateau, utility, stagnation,
    # falsification) — kein per-Heuristik-Sondercode noetig.
    _min_rounds = max(1, int(getattr(settings, "min_rounds", 1) or 1))
    if (
        s["done"]
        and s["round"] < _min_rounds
        and s["round"] < settings.max_rounds
    ):
        log.info(
            "TRACE evaluate: stop suppressed by min_rounds (round=%d < %d, max=%d)",
            s["round"], _min_rounds, settings.max_rounds,
        )
        emit_progress(
            s,
            t(
                s,
                "min_rounds_continue",
                min_rounds=_min_rounds,
                round=s["round"],
            ),
        )
        s["done"] = False
        _stop_cascade["suppressed_by_min_rounds"] = True
        _stop_cascade["suppressed_stop_reason"] = s.get("_stop_reason", "")
        s["_stop_reason"] = ""
    else:
        _stop_cascade["suppressed_by_min_rounds"] = False
    # Do not stop early while the EvidenceLedger still holds too few
    # report-eligible records to render a substantial report. This is a
    # general minimum-evidence guardrail, not a damping layer over a
    # derived list -- it checks the primary truth directly.
    report_evidence_too_thin = (
        bool(s.get("evidence_ledger"))
        and report_eligible_evidence_count < tuning.min_report_eligible_evidence
    )
    if (
        s["done"]
        and report_evidence_too_thin
        and s["round"] < settings.max_rounds
    ):
        log.info(
            "TRACE evaluate: stop suppressed by report-eligible evidence "
            "(records=%d/%d round=%d/%d)",
            report_eligible_evidence_count,
            tuning.min_report_eligible_evidence,
            s["round"],
            settings.max_rounds,
        )
        emit_progress(
            s,
            (
                "Evidenzlage noch zu duenn "
                f"({report_eligible_evidence_count}/{tuning.min_report_eligible_evidence} "
                "report-faehige Records); weitere Recherche."
            ),
        )
        _stop_cascade["suppressed_by_report_evidence"] = True
        _stop_cascade["suppressed_stop_reason"] = s.get("_stop_reason", "")
        s["done"] = False
        s["_stop_reason"] = ""
    else:
        _stop_cascade["suppressed_by_report_evidence"] = False
    _stop_cascade["final_stop_reason"] = s.get("_stop_reason", "")
    _stop_cascade["final_done"] = bool(s.get("done"))

    _append_forensic_event(
        s,
        settings,
        event="stop_cascade",
        node="evaluate",
        payload=_stop_cascade,
    )
    _evaluate_score_snapshot = append_score_snapshot(
        s,
        phase="evaluate",
        extra={
            "llm_confidence": conf,
            "final_confidence": s.get("final_confidence", conf),
            "utility_score": _utility,
            "stop_reason": s.get("_stop_reason", ""),
            "evidence_depth_gap_active": bool(evidence_depth_gap.get("active")),
            "report_eligible_evidence_count": report_eligible_evidence_count,
            "confidence_unjustified_drop": _confidence_unjustified_drop,
        },
    )
    _append_forensic_event(
        s,
        settings,
        event="score_snapshot",
        node="evaluate",
        payload=_evaluate_score_snapshot,
    )

    _eval_log_entry: dict[str, Any] = {
        "node": "evaluate",
        "timestamp": time.time(),
        "duration_s": round(time.monotonic() - _t0, 3),
        "round": s["round"],
        "confidence": conf,
        "final_confidence": conf,
        "prev_conf": _prev_conf,
        "confidence_parsed": _confidence_parsed,
        "_confidence_parsed": _confidence_parsed,
        "confidence_clamped": _confidence_clamped,
        "confidence_unjustified_drop": _confidence_unjustified_drop,
        "confidence_stop_target": settings.confidence_stop,
        "gaps": s.get("gaps", ""),
        "competing_events": s.get("competing_events", ""),
        "stagnation_detected": _stagnation_detected,
        "falsification_triggered": s.get("falsification_triggered", False),
        "evidence_consistency": s.get("evidence_consistency", 0),
        "evidence_sufficiency": s.get("evidence_sufficiency", 0),
        "evidence_consistency_parsed": s.get("_evidence_consistency_parsed", True),
        "evidence_sufficiency_parsed": s.get("_evidence_sufficiency_parsed", True),
        "_evidence_consistency_parsed": s.get("_evidence_consistency_parsed", True),
        "_evidence_sufficiency_parsed": s.get("_evidence_sufficiency_parsed", True),
        "verified_claims": verified_claims,
        "attributed_claims": attributed_claims,
        "contested_claims": contested_claims,
        "unverified_claims": unverified_claims,
        "source_tier_counts": s.get("source_tier_counts", {}),
        "source_quality_score": s.get("source_quality_score", 0.0),
        "claim_status_counts": s.get("claim_status_counts", {}),
        "claim_quality_score": s.get("claim_quality_score", 0.0),
        "consolidated_claims_count": len(claim_assessments(s)),
        "claim_needs_primary_total": s.get("claim_needs_primary_total", 0),
        "claim_needs_primary_supported": s.get("claim_needs_primary_supported", 0),
        "evidence_depth_gap": evidence_depth_gap,
        "evidence_depth_gap_active": bool(evidence_depth_gap.get("active")),
        "report_eligible_evidence_count": report_eligible_evidence_count,
        "aspect_coverage": s.get("aspect_coverage", 0.0),
        "uncovered_aspects": s.get("uncovered_aspects", []),
        "evaluate_status": s.get("evaluate_status", ""),
        "coverage_source": s.get("coverage_source", ""),
        "event_mode": bool(s.get("event_mode", False)),
        "model": evaluate_model,
        "utility_score": _utility,
        "utility_stop": _utility_stop,
        "plateau_stop": _plateau_stop,
        "evidence_record_count": len(s.get("evidence_ledger", []) or []),
        "stop_cascade": _stop_cascade,
        "_stop_reason": s.get("_stop_reason", ""),
        "stop_by_confidence": conf >= settings.confidence_stop,
        "stop_by_round_limit": s["round"] >= settings.max_rounds,
        "stop_by_existing_done": _done_after_utility or _done_after_plateau,
        "done": s["done"],
        "guardrail_reasons": list(_guardrail_result.reasons or []),
        "_evaluate_fallback": bool(_evaluate_fallback),
        **_evaluate_fallback,
    }
    if settings.testing_mode and _eval_raw:
        _eval_log_entry["reasoning"] = _eval_raw
    append_iteration_log(s, _eval_log_entry, testing_mode=settings.testing_mode)
    return s


# ======================================================================= #
# 5. answer
# ======================================================================= #


def _build_algorithm_failure_report(
    s: dict[str, Any],
    *,
    failures: list[dict[str, Any]],
    evidence_record_count: int,
    citation_count: int,
) -> str:
    """Render a short diagnostic instead of a normal report after core failure."""
    lines = [
        t(s, "research_algofail_heading"),
        "",
        t(s, "research_algofail_intro"),
        "",
        t(s, "research_algofail_diagnosis"),
        "",
        t(s, "research_algofail_run_id", value=s.get("_run_id", "")),
        t(s, "research_algofail_rounds", value=s.get("round", 0)),
        t(s, "research_algofail_references", value=citation_count),
        t(s, "research_algofail_evidence_records", value=evidence_record_count),
        t(
            s,
            "research_algofail_claim_calls",
            value=s.get("_claim_extraction_attempts_total", 0),
        ),
        t(
            s,
            "research_algofail_claim_failures",
            value=s.get("_claim_extraction_failures_total", 0),
        ),
        "",
        t(s, "research_algofail_blocking"),
        "",
    ]
    for failure in failures:
        phase = str(failure.get("phase", "unknown"))
        reason = str(failure.get("reason", "unknown"))
        round_index = failure.get("round", "")
        message = str(failure.get("message", "") or "")
        lines.append(
            t(
                s,
                "research_algofail_failure_line",
                phase=phase,
                round=round_index,
                reason=reason,
            )
        )
        if message:
            lines.append(f"  {message}")
    lines.extend(
        [
            "",
            t(s, "research_algofail_next_step"),
            "",
            t(s, "research_algofail_next_step_text"),
        ]
    )
    return "\n".join(lines)


def _answer_language_code(s: dict) -> str | None:
    """The run's answer language, or ``None`` when it stayed unresolved."""
    if s.get("language_source") == "unresolved":
        return None
    return str(s.get("language") or "") or None


def answer(
    s: dict,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict:
    """Formulate the final user-facing answer from the collected evidence.

    Args:
        s: Mutable AgentState-compatible dict. Reads context, claim
            metrics, language, citations, and deadline; writes the final
            answer text and answer-node runtime metadata.
        providers: Active LLM and search providers.
        strategies: Runtime strategies for answer citation selection and
            claim formatting.
        settings: Agent behavior settings controlling citation limits and
            fallback behavior.

    Returns:
        The mutated state dict with the final answer text populated.

    Raises:
        AgentRateLimited: Propagated when final answer generation hits a
            fatal upstream rate limit.

    Example:
        >>> answer(state, providers=providers, strategies=strategies, settings=settings)
        {'answer': 'Aktueller Stand ...', ...}
    """
    check_cancel_event(s)
    n_rounds = s.get("round", 0)
    round_label = t(
        s,
        "answer_round_singular" if n_rounds == 1 else "answer_round_plural",
    )
    emit_progress(
        s,
        t(s, "answer_start", n=n_rounds, round_label=round_label),
    )
    _t0 = time.monotonic()
    tuning = settings.report_tuning

    # The answer language as a code; ``None`` (unresolved) yields the
    # language-neutral directive. Headings follow the run's language.
    answer_lang = _answer_language_code(s)
    heading_language = s.get("language")

    # Single canonical evidence view: the answer prompt consumes exactly one
    # record-driven Markdown overview rendered straight from the EvidenceLedger.
    # There are no parallel report-evidence-bundle / prompt-evidence-unit /
    # rendered-context channels and no separate answer-citation lists -- the
    # citation allowlist is the union of visible source-block URLs.
    consolidated_claims = claim_assessments(s)
    evidence_ledger = s.get("evidence_ledger", []) or []
    evidence_overview = render_evidence_ledger_overview(
        evidence_ledger,
        max_total_chars=tuning.prompt_evidence_total_char_budget,
        max_record_chars=tuning.prompt_evidence_record_char_limit,
        query_synthesis=s.get("query_synthesis", {}),
    )
    evidence_overview_markdown = evidence_overview.markdown
    allowed_citations = list(evidence_overview.allowed_urls)
    _trace_evidence_overview(
        evidence_overview,
        budget_chars=tuning.prompt_evidence_total_char_budget,
        view="answer",
    )
    has_ledger_context = bool(evidence_ledger)
    strict_algorithm_mode = _strict_algorithm_failure_mode(settings)
    if strict_algorithm_mode:
        claim_attempts_total = int(s.get("_claim_extraction_attempts_total", 0) or 0)
        claim_failures_total = int(s.get("_claim_extraction_failures_total", 0) or 0)
        if claim_attempts_total and claim_failures_total >= claim_attempts_total:
            _record_algorithm_failure(
                s,
                settings,
                node="answer",
                phase="claim_extraction",
                reason="run_all_sources_failed",
                message=(
                    "All claim-extraction calls across the run failed; "
                    "normal report synthesis would rely on unverified source context only."
                ),
                blocking=True,
                details={
                    "attempts": claim_attempts_total,
                    "failed": claim_failures_total,
                    "total": claim_attempts_total,
                },
            )
    if has_ledger_context and evidence_overview.rendered_record_count == 0:
        _record_algorithm_failure(
            s,
            settings,
            node="answer",
            phase="evidence_overview",
            reason="no_rendered_evidence_records",
            message=(
                "The EvidenceLedger held records but the evidence overview "
                "rendered no source records for the final composer."
            ),
            blocking=strict_algorithm_mode,
            details={
                "evidence_record_count": len(evidence_ledger),
                "omitted_record_count": evidence_overview.omitted_record_count,
            },
        )
    blocking_algorithm_failures = _blocking_algorithm_failures(s)
    algorithm_report_blocked = bool(blocking_algorithm_failures)

    # Assemble shared prompt state for the section-wise composer.
    state_data: dict[str, Any] = {
        "today_str": today(),
        "answer_lang": answer_lang,
        "evidence_overview": evidence_overview_markdown,
        "evidence_label_urls": dict(evidence_overview.label_urls),
        "evidence_label_by_id": dict(evidence_overview.label_by_evidence_id),
        "visible_evidence_labels": sorted(evidence_overview.label_urls),
        "visible_evidence_label_count": len(evidence_overview.label_urls),
        "rendered_evidence_ids": list(evidence_overview.rendered_evidence_ids),
        "allowed_citations": allowed_citations,
        "rendered_evidence_record_count": evidence_overview.rendered_record_count,
        "omitted_evidence_record_count": evidence_overview.omitted_record_count,
        # The evidence-depth gap is the only signal that knows how many
        # verified claims rest on a single source. It is read by the
        # prompt builder to trigger the EVIDENCE DEPTH block + the existing
        # TRANSPARENCY OBLIGATION block when cross-check coverage is thin.
        "evidence_depth_gap": dict(s.get("evidence_depth_gap", {}) or {}),
        "source_tier_counts": s.get("source_tier_counts", {}),
        "source_quality_score": s.get("source_quality_score", 0.0),
        "claim_status_counts": s.get("claim_status_counts", {}),
        "claim_quality_score": s.get("claim_quality_score", 0.0),
        "claim_needs_primary_total": s.get("claim_needs_primary_total", 0),
        "claim_needs_primary_supported": s.get("claim_needs_primary_supported", 0),
        "required_aspects": s.get("required_aspects"),
        "uncovered_aspects": s.get("uncovered_aspects", []),
        "coverage_source": s.get("coverage_source", ""),
        "evaluate_status": s.get("evaluate_status", ""),
        "gaps": s.get("gaps", ""),
        "answer_contract": s.get("answer_contract", "general"),
        "competing_events": s.get("competing_events", ""),
        "history": s.get("history", ""),
        "report_profile": str(settings.report_profile),
    }
    s.update(
        {
            "evidence_label_urls": dict(evidence_overview.label_urls),
            "evidence_label_by_id": dict(evidence_overview.label_by_evidence_id),
            "visible_evidence_labels": sorted(evidence_overview.label_urls),
            "visible_evidence_label_count": len(evidence_overview.label_urls),
            "rendered_evidence_ids": list(evidence_overview.rendered_evidence_ids),
            "allowed_citations": allowed_citations,
            "rendered_evidence_record_count": evidence_overview.rendered_record_count,
            "omitted_evidence_record_count": evidence_overview.omitted_record_count,
            # Per search, so ResearchMetrics and the parent run can name
            # which searches this report read only partially.
            "omitted_evidence_by_query": [
                dict(entry) for entry in evidence_overview.omitted_by_query
            ],
        }
    )
    if settings.testing_mode or forensic_enabled(settings):
        diagnostics = _build_answer_prompt_diagnostics(state_data, s)
        append_iteration_log(
            s,
            {
                "event": "answer_prompt_diagnostics",
                "node": "answer",
                "timestamp": time.time(),
                **diagnostics,
            },
            testing_mode=settings.testing_mode,
        )
    # Resolve the answer node once (emits the single model_resolution event) and
    # reuse the result for the diagnostics and both composition calls, so every
    # answer-node diagnostic names the model/effort actually routed -- not the
    # provider's reasoning_model default.
    answer_model, answer_effort = _resolve_node_llm(s, settings, providers, "answer")
    _append_forensic_event(
        s,
        settings,
        event="answer_prompt_inputs",
        node="answer",
        payload={
            "report_profile": str(settings.report_profile),
            "model": answer_model,
            "evidence_record_count": len(evidence_ledger),
            "rendered_evidence_record_count": evidence_overview.rendered_record_count,
            "omitted_evidence_record_count": evidence_overview.omitted_record_count,
            "visible_evidence_label_count": len(evidence_overview.label_urls),
            "evidence_overview_chars": len(evidence_overview_markdown),
            "allowed_citation_count": len(allowed_citations),
            "consolidated_claim_count": len(consolidated_claims),
            "algorithm_failure_count": len(s.get("algorithm_failures", []) or []),
            "blocking_algorithm_failure_count": len(blocking_algorithm_failures),
            "evidence_overview": evidence_overview_markdown,
            "section_plan": [
                {
                    "key": section.key,
                    "heading": section.heading(heading_language),
                    "required": section.required,
                }
                for section in settings.report_tuning.answer_sections
            ],
        },
    )
    fallback_model = _resolve_answer_fallback_model(settings, providers, answer_model)
    fallback_attempted = False
    fallback_succeeded = False
    answer_fallback_kind = ""
    answer_fallback_reason = ""
    composition_result = _AnswerCompositionResult(answer="", finish_reason="", section_logs=[])

    def _answer_warning_header(reason: str) -> str:
        """Render a visible warning header for fallback answers.

        The header is the first thing the operator and any downstream
        consumer (UI, parity tooling, exported markdown) sees, so a
        silent degradation cannot pass unnoticed even when the iteration
        log is not consulted.
        """
        return t(s, "research_answer_fallback_warning", reason=reason)

    def _build_fallback_answer(reason: str, *, no_context_message: str) -> str:
        if evidence_overview_markdown:
            return f"{_answer_warning_header(reason)}\n\n{evidence_overview_markdown}"
        return f"{_answer_warning_header(reason)}\n\n{no_context_message}"

    def _answer_failure_phase(exc: BaseException) -> str:
        """Classify provider failures for fail-closed answer diagnostics."""
        if is_model_capacity_error(exc):
            return "model_capacity"
        error_text = str(exc).lower()
        if any(
            marker in error_text
            for marker in (
                "usage limit",
                "usage limits",
                "rate limit",
                "quota",
                "context limit",
                "token limit",
                "max token",
                "maximum token",
            )
        ):
            return "provider_limit"
        return "answer_synthesis"

    def _build_answer_algorithm_failure(
        *,
        exc: BaseException,
        phase: str,
        reason: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> _AnswerCompositionResult:
        _record_algorithm_failure(
            s,
            settings,
            node="answer",
            phase=phase,
            reason=reason or type(exc).__name__,
            message=message or str(exc),
            blocking=True,
            details=details,
        )
        failures = _blocking_algorithm_failures(s)
        emit_progress(
            s,
            t(s, "algorithm_failure_report_blocked", count=len(failures)),
        )
        s["final_confidence"] = min(int(s.get("final_confidence", 0) or 0) or 3, 3)
        s["answer"] = _build_algorithm_failure_report(
            s,
            failures=failures,
            evidence_record_count=len(evidence_ledger),
            citation_count=len(s.get("source_records", {}) or {}),
        )
        return _AnswerCompositionResult(
            answer=s["answer"],
            finish_reason=phase,
            section_logs=[],
            composition_aborted=True,
            sections_planned=len(settings.report_tuning.answer_sections),
            sections_attempted=0,
        )

    if algorithm_report_blocked:
        emit_progress(
            s,
            t(s, "algorithm_failure_report_blocked", count=len(blocking_algorithm_failures)),
        )
        s["final_confidence"] = min(int(s.get("final_confidence", 0) or 0) or 3, 3)
        s["answer"] = _build_algorithm_failure_report(
            s,
            failures=blocking_algorithm_failures,
            evidence_record_count=len(evidence_ledger),
            citation_count=len(s.get("source_records", {}) or {}),
        )
        composition_result = _AnswerCompositionResult(
            answer=s["answer"],
            finish_reason="algorithm_failure",
            section_logs=[],
            composition_aborted=True,
            sections_planned=len(settings.report_tuning.answer_sections),
            sections_attempted=0,
        )
    else:
        try:
            composition_result = _compose_answer_sections(
                s,
                providers=providers,
                settings=settings,
                state_data=state_data,
                model=answer_model,
                reasoning_effort=answer_effort,
            )
            s["answer"] = composition_result.answer
        except AgentModelCapacityError as exc_capacity:
            _record_algorithm_failure(
                s,
                settings,
                node="answer",
                phase="model_capacity",
                reason=type(exc_capacity).__name__,
                message=str(exc_capacity),
                blocking=True,
                details={
                    "model": getattr(exc_capacity, "model", ""),
                    "capacity_phase": getattr(exc_capacity, "phase", ""),
                },
            )
            blocking_algorithm_failures = _blocking_algorithm_failures(s)
            emit_progress(
                s,
                t(s, "algorithm_failure_report_blocked", count=len(blocking_algorithm_failures)),
            )
            s["final_confidence"] = min(int(s.get("final_confidence", 0) or 0) or 3, 3)
            s["answer"] = _build_algorithm_failure_report(
                s,
                failures=blocking_algorithm_failures,
                evidence_record_count=len(evidence_ledger),
                citation_count=len(s.get("source_records", {}) or {}),
            )
            composition_result = _AnswerCompositionResult(
                answer=s["answer"],
                finish_reason="model_capacity",
                section_logs=[],
                composition_aborted=True,
                sections_planned=len(settings.report_tuning.answer_sections),
                sections_attempted=0,
            )
        except AgentTimeout as exc_timeout:
            if strict_algorithm_mode:
                composition_result = _build_answer_algorithm_failure(
                    exc=exc_timeout,
                    phase="answer_synthesis_timeout",
                    message=(
                        "Answer synthesis exceeded the configured timeout; "
                        "strict mode will not emit a raw-context fallback report."
                    ),
                    details={"error": str(exc_timeout)[:300]},
                )
                s["answer"] = composition_result.answer
                answer_fallback_kind = ""
                answer_fallback_reason = ""
                finish_reason = str(composition_result.finish_reason or "")
                section_logs = list(composition_result.section_logs)
                # Continue with common answer bookkeeping below.
            else:
                answer_fallback_kind = "timeout"
                answer_fallback_reason = t(
                    s, "research_answer_timeout_reason"
                )
                log.warning(
                    "TRACE answer fallback (error_code=%s)",
                    type(exc_timeout).__name__,
                )
                emit_progress(s, t(s, "answer_timeout_fallback"), severity="warning")
                s["answer"] = _build_fallback_answer(
                    answer_fallback_reason,
                    no_context_message=t(s, "research_no_context_timeout"),
                )
        except (
            OpenAIError,
            AgentRateLimited,
            AnthropicAPIError,
            AzureOpenAIAPIError,
            BedrockAPIError,
        ) as e:
            provider_error_label = f"{type(e).__name__}: {e}"[:240]
            log.error(
                "Final answer failed (error_code=%s)",
                type(e).__name__,
            )
            if strict_algorithm_mode:
                phase = _answer_failure_phase(e)
                composition_result = _build_answer_algorithm_failure(
                    exc=e,
                    phase=phase,
                    reason=type(e).__name__,
                    message=(
                        "Answer synthesis provider call failed; strict mode will "
                        "not emit a raw-context fallback report."
                    ),
                    details={
                        "error": str(e)[:300],
                        "error_type": type(e).__name__,
                    },
                )
                s["answer"] = composition_result.answer
            elif fallback_model:
                try:
                    fallback_attempted = True
                    emit_progress(
                        s,
                        t(s, "answer_fallback_model", model=fallback_model),
                        severity="warning",
                    )
                    composition_result = _compose_answer_sections(
                        s,
                        providers=providers,
                        settings=settings,
                        state_data=state_data,
                        model=fallback_model,
                        reasoning_effort="",
                    )
                    s["answer"] = composition_result.answer
                    fallback_succeeded = True
                except (
                    OpenAIError,
                    AgentTimeout,
                    AgentRateLimited,
                    AnthropicAPIError,
                    AzureOpenAIAPIError,
                    BedrockAPIError,
                ) as e2:
                    fallback_error_label = f"{type(e2).__name__}: {e2}"[:240]
                    answer_fallback_kind = "fallback_model_failed"
                    answer_fallback_reason = (
                        f"Primaeres Antwort-Modell fehlgeschlagen ({provider_error_label}); "
                        f"Fallback-Modell `{fallback_model}` ebenfalls fehlgeschlagen "
                        f"({fallback_error_label})."
                    )
                    log.warning(
                        "TRACE answer fallback (fallback_model_failed model=%s error_code=%s)",
                        fallback_model,
                        type(e2).__name__,
                    )
                    emit_progress(
                        s,
                        t(s, "answer_fallback_model_failed", error=fallback_error_label),
                        severity="warning",
                    )
                    s["answer"] = _build_fallback_answer(
                        answer_fallback_reason,
                        no_context_message=t(
                            s, "research_no_context_provider"
                        ),
                    )
            else:
                answer_fallback_kind = "no_fallback_model"
                answer_fallback_reason = t(
                    s,
                    "research_answer_failed_no_fallback_body",
                    error=provider_error_label,
                )
                log.warning(
                    "TRACE answer fallback (no_fallback_model error_code=%s)",
                    type(e).__name__,
                )
                emit_progress(
                    s,
                    t(
                        s,
                        "answer_provider_failed_no_fallback",
                        error=provider_error_label,
                    ),
                    severity="warning",
                )
                s["answer"] = _build_fallback_answer(
                    answer_fallback_reason,
                    no_context_message=t(s, "research_no_context_provider"),
                )

    algorithm_report_blocked = bool(_blocking_algorithm_failures(s))
    finish_reason = str(composition_result.finish_reason or "")
    section_logs = list(composition_result.section_logs)
    s["answer_finish_reason"] = finish_reason
    incomplete_reasons = _detect_incomplete_answer(
        s.get("answer", ""),
        finish_reason=finish_reason,
        report_profile=settings.report_profile,
    )
    s["answer_incomplete"] = bool(incomplete_reasons)
    s["answer_incomplete_reasons"] = list(incomplete_reasons)
    if incomplete_reasons:
        emit_progress(s, t(s, "answer_incomplete_detected"), severity="warning")
        log.warning(
            "TRACE answer: incomplete answer detected (finish_reason=%s, reasons=%s)",
            finish_reason or "unknown",
            incomplete_reasons,
        )

    s["answer"], expanded_evidence_label_links = _expand_bare_evidence_label_links(
        s.get("answer", ""),
        state_data.get("evidence_label_urls", {}) or {},
    )

    # Quick citation guardrail: remove links that are not in the
    # EvidenceOverview-derived allowlist (the union of visible source-block URLs).
    removed_link_count = 0
    allowed_citation_urls = {
        normalize_url(url)
        for url in allowed_citations
        if normalize_url(url)
    }
    appended_sources_footer = False
    allowed_link_count = 0
    if allowed_citation_urls and s.get("answer"):
        s["answer"], removed_link_count = sanitize_answer_links(
            s["answer"], allowed_citation_urls)
        if removed_link_count:
            log.info("TRACE answer: removed %d non-allowed links", removed_link_count)
            emit_progress(s, t(s, "answer_links_removed", n=removed_link_count))
        allowed_link_count = count_allowed_links(s["answer"], allowed_citation_urls)

    if incomplete_reasons:
        s["answer"] = _repair_answer_markdown_tail(s.get("answer", ""))
    if algorithm_report_blocked:
        appendix_sections = []
        reference_link_count = 0
        additional_link_count = 0
        s["report_references"] = []
    else:
        ledger_facts = _reference_facts_from_evidence_ledger(evidence_ledger)
        appendix = _build_answer_appendix_sections(
            s.get("answer", ""),
            allowed_citations=allowed_citations,
            label_urls=state_data.get("evidence_label_urls", {}) or {},
            strategies=strategies,
            incomplete_reasons=incomplete_reasons,
            finish_reason=finish_reason,
            answer_contract=s.get("answer_contract", "general"),
            tier_by_url=ledger_facts.tier_by_url,
            title_by_url=ledger_facts.title_by_url,
            omitted_by_query=evidence_overview.omitted_by_query,
            placement_by_url=ledger_facts.placement_by_url,
            statements_by_url=ledger_facts.statements_by_url,
            language=s.get("language"),
        )
        appendix_sections = appendix.sections
        reference_link_count = len(appendix.references)
        additional_link_count = len(appendix.additional_links)
        s["report_references"] = appendix.references
    answer_body = s.get("answer", "")
    if appendix_sections:
        # Appendix sections carry their own ``##`` headings; no rule between them.
        s["answer"] = answer_body.rstrip() + "\n\n" + "\n\n".join(appendix_sections)
        appended_sources_footer = True

    answer_claim_bindings = _build_answer_claim_bindings(
        answer_body,
        consolidated_claims=consolidated_claims,
        allowed_citations=allowed_citations,
        provider_citation_records=s.get("provider_citation_records", []) or [],
    )
    matched_claim_ids = {
        str(binding.get("claim_id", ""))
        for binding in answer_claim_bindings
        if binding.get("claim_id") and binding.get("binding_status") == "matched"
    }
    # A custom materializer may return copies. Project usage by ID onto both
    # the complete assessment and the bounded display list.
    for pool in (consolidated_claims, s.get("consolidated_claims", []) or []):
        for claim in pool:
            claim["used_in_answer"] = str(claim.get("claim_id", "")) in matched_claim_ids
    s["answer_claim_bindings"] = answer_claim_bindings
    for binding in answer_claim_bindings:
        _append_forensic_event(
            s,
            settings,
            event="answer_claim_binding",
            node="answer",
            payload=binding,
        )
    answer_evidence_bindings = audit_answer_evidence_bindings(
        answer_body,
        evidence_ledger,
    )
    s["answer_evidence_bindings"] = answer_evidence_bindings
    _unknown_citation_count = sum(
        1
        for binding in answer_evidence_bindings
        if binding.get("binding_status") == "unknown_citation"
    )
    _matched_evidence_count = sum(
        1
        for binding in answer_evidence_bindings
        if binding.get("binding_status") == "matched"
    )
    # The contract is decided by the claim-level binding (does a cited answer
    # sentence plausibly carry a verified/contested consolidated claim?), not by
    # the coarser URL-level evidence audit (does the URL resolve to a record
    # with any verified claim?).
    _matched_claim_count = sum(
        1
        for binding in answer_claim_bindings
        if binding.get("binding_status") == "matched"
    )
    # A cited body sentence whose URL carries NO consolidated claim at all -- the
    # claim-level twin of `unknown_citation` (a cited link with no backing
    # record). Both mean the answer points at something the evidence base does
    # not substantiate, so mixed with a genuine match the run is not clean.
    _citation_without_claim_count = sum(
        1
        for binding in answer_claim_bindings
        if binding.get("binding_status") == "citation_without_claim"
    )
    # Tiers ordered strictly by severity, monotonic to _CONTRACT_CONFIDENCE_CAP
    # (algorithm_failed 3 < source_context_only 4 < needs_review 6 < clean):
    #   no citable evidence base at all              -> unknown (audit N/A, uncapped)
    #   no claim-bound sentence at all               -> source_context_only
    #   bound, but a cited target is unsubstantiated -> needs_review
    #     (unknown_citation: no record; citation_without_claim: record w/o claim)
    #   bound and nothing unsubstantiated            -> clean
    # `unknown` keys on whether the body HAD citable evidence to ground in
    # (allowed_citations), not on whether it happened to cite any. Keying it on
    # empty bindings let an answer that ignores real gathered evidence and cites
    # nothing escape to uncapped `unknown` -- while the appendix still appended
    # references, making the report look sourced. Such an ungrounded body now
    # falls to source_context_only (cap 4) like any other claimless answer; only
    # a run with no citable evidence at all (direct-LLM / empty search) is
    # `unknown`. A `source_only_binding` (background citation whose claim sits in
    # the ledger) is deliberately tolerated and never downgrades the tier.
    _evidence_contract_status = (
        "algorithm_failed"
        if algorithm_report_blocked
        else "unknown"
        if not allowed_citations
        else "source_context_only"
        if not _matched_claim_count
        else "needs_review"
        if (_unknown_citation_count or _citation_without_claim_count)
        else "clean"
    )
    if s.get("algorithm_failures") and _evidence_contract_status == "clean":
        _evidence_contract_status = "needs_review"
    # A run where almost every "verified" claim rests on a single quality
    # source is not a clean hard-fact report -- downgrade so the
    # Evidenzhinweis appendix + confidence cap fire even when the URL audit
    # itself looks fine.
    if (
        _evidence_contract_status == "clean"
        and (s.get("evidence_depth_gap") or {}).get("active")
    ):
        _evidence_contract_status = "needs_review"

    # Single source of truth: persist the canonical contract so result.py and
    # scoring.py read it instead of each recomputing a divergent copy.
    s["evidence_contract_status"] = _evidence_contract_status

    # One confidence ceiling, keyed on the contract (see _CONTRACT_CONFIDENCE_CAP).
    _confidence_cap = _CONTRACT_CONFIDENCE_CAP.get(_evidence_contract_status)
    if _confidence_cap is not None:
        s["final_confidence"] = min(
            int(s.get("final_confidence", 0) or 0), _confidence_cap
        )

    if _evidence_contract_status in {"needs_review", "source_context_only"}:
        if _evidence_contract_status == "needs_review":
            emit_progress(s, t(s, "evidence_contract_needs_review"))
        else:
            emit_progress(s, t(s, "evidence_contract_source_context_only"))
        if s.get("answer"):
            depth_gap_active_now = bool(
                (s.get("evidence_depth_gap") or {}).get("active")
            )
            if _evidence_contract_status == "source_context_only":
                evidence_notice = t(s, "answer_evidence_notice_source_context")
            elif depth_gap_active_now:
                # Recomputed from the claims, never read from a stored dict
                # whose keys an older state may lack.
                gap = _evidence_depth_gap(s)
                evidence_notice = t(
                    s,
                    "answer_evidence_notice_single_source",
                    single=gap["single_source_count"],
                    supported=gap["supported_count"],
                    ratio=int(gap["single_source_ratio"] * 100),
                    cross=gap["cross_checked_count"],
                )
            else:
                evidence_notice = t(s, "answer_evidence_notice_unbound")
            s["answer"] = (
                s["answer"].rstrip()
                + "\n\n"
                + t(s, "answer_evidence_notice_heading")
                + "\n\n"
                + evidence_notice
            )

    for binding in answer_evidence_bindings:
        _append_forensic_event(
            s,
            settings,
            event="answer_sentence_audit",
            node="answer",
            payload=binding,
        )
    _answer_score_snapshot = append_score_snapshot(
        s,
        phase="answer",
        extra={
            # Same claim-grounded count the contract uses; the URL-level audit
            # feeds the separate matched_evidence_binding_count diagnostic.
            "answer_bound_claims_count": _matched_claim_count,
            "unbound_answer_citations_count": _unknown_citation_count,
            "evidence_contract_status": _evidence_contract_status,
            "final_confidence": s.get("final_confidence", 0),
        },
    )
    _append_forensic_event(
        s,
        settings,
        event="score_snapshot",
        node="answer",
        payload=_answer_score_snapshot,
    )
    _append_forensic_event(
        s,
        settings,
        event="citation_selection",
        node="answer",
        payload={
            "allowed_citations": allowed_citations[:60],
            "allowed_citation_count": len(allowed_citations),
            "allowed_link_count": allowed_link_count,
            "removed_non_allowed_links": removed_link_count,
            "answer_claim_binding_count": len(answer_claim_bindings),
            "answer_evidence_binding_count": len(answer_evidence_bindings),
            "matched_evidence_binding_count": _matched_evidence_count,
            "unknown_citation_count": _unknown_citation_count,
            "expanded_evidence_label_links": expanded_evidence_label_links,
        },
    )

    # Append stats footer
    elapsed = time.monotonic() - s.get("start_time", time.monotonic())
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    time_str = f"{minutes}:{seconds:02d} min" if minutes > 0 else f"{seconds}s"
    conf = s.get("final_confidence", 0)
    n_sources = len(s.get("source_records", {}) or {})
    n_rounds = s.get("round", 0)
    n_queries = len(s.get("queries", []))
    rendered_record_count = evidence_overview.rendered_record_count
    omitted_record_count = evidence_overview.omitted_record_count
    verified_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "verified"
    )
    attributed_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "attributed"
    )
    # "Cross-checked" in the footer means checked: two independent source
    # texts. Corroborated attribution is part of the attributed count.
    cross_checked_claim_count = sum(
        1 for claim in consolidated_claims
        if claim.get("status") == "verified"
        and claim.get("verification_basis") == "cross_checked"
    )

    stats_parts = []
    if n_sources:
        stats_parts.append(t(s, "answer_stats_sources", n=n_sources))
    if rendered_record_count:
        stats_parts.append(t_count(s, "answer_stats_evidence_prompt", rendered_record_count))
    if allowed_link_count:
        stats_parts.append(t(s, "answer_stats_linked", n=allowed_link_count))
    if verified_claim_count:
        stats_parts.append(t(s, "answer_stats_verified", n=verified_claim_count))
    if attributed_claim_count:
        stats_parts.append(t_count(s, "answer_stats_attributed", attributed_claim_count))
    if cross_checked_claim_count:
        stats_parts.append(t(s, "answer_stats_cross_checked", n=cross_checked_claim_count))
    # When the evidence-depth gap is active, surface the single-source share
    # of the supported claims so the footer makes the imbalance visible at a
    # glance instead of hiding it behind a generic count.
    _depth_gap_footer = _evidence_depth_gap(s)
    if _depth_gap_footer["active"] and _depth_gap_footer["supported_count"]:
        ratio_pct = int(_depth_gap_footer["single_source_ratio"] * 100)
        stats_parts.append(
            f"{_depth_gap_footer['single_source_count']}/"
            f"{_depth_gap_footer['supported_count']} single-source ({ratio_pct}%)"
        )
    if omitted_record_count:
        stats_parts.append(t_count(s, "answer_stats_omitted", omitted_record_count))
    if _evidence_contract_status != "unknown":
        stats_parts.append(f"Evidence-Contract: {_evidence_contract_status}")
    if s.get("algorithm_failures"):
        stats_parts.append(
            f"ALGO-FAIL: {len(s.get('algorithm_failures', []) or [])}"
        )
    if n_queries:
        stats_parts.append(t(s, "answer_stats_searches", n=n_queries))
    if n_rounds:
        stats_parts.append(t_count(s, "answer_stats_round", n_rounds))
    stats_parts.append(time_str)
    if conf:
        stats_parts.append(f"Confidence {conf}/10")

    stats_line = " · ".join(stats_parts)
    s["answer"] += f"\n\n---\n*{stats_line}*"

    log.info(
        "TRACE answer: length=%d sources=%d rendered_records=%d omitted_records=%d "
        "linked=%d refs=%d extra_links=%d sections=%d rounds=%d elapsed=%.1fs "
        "confidence=%d finish_reason=%s incomplete=%s",
        len(s["answer"]), n_sources, rendered_record_count, omitted_record_count,
        allowed_link_count, reference_link_count, additional_link_count, len(section_logs),
        n_rounds, elapsed, conf, finish_reason or "", bool(incomplete_reasons),
    )

    append_iteration_log(s, {
        "node": "answer",
        "timestamp": time.time(),
        "duration_s": round(time.monotonic() - _t0, 3),
        "answer_length": len(s["answer"]),
        "evidence_record_count": len(evidence_ledger),
        "rendered_evidence_record_count": rendered_record_count,
        "omitted_evidence_record_count": omitted_record_count,
        "evidence_overview_chars": len(evidence_overview_markdown),
        "visible_evidence_label_count": len(evidence_overview.label_urls),
        "allowed_citation_count": len(allowed_citations),
        "allowed_citations": allowed_citations[:10],
        "_answer_fallback": bool(answer_fallback_kind) or (fallback_attempted and not fallback_succeeded),
        "_answer_fallback_kind": answer_fallback_kind,
        "_answer_fallback_reason": answer_fallback_reason,
        "_answer_links_sanitized": removed_link_count,
        "removed_non_allowed_links": removed_link_count,
        "expanded_evidence_label_links": expanded_evidence_label_links,
        "allowed_link_count": allowed_link_count,
        "reference_link_count": reference_link_count,
        "additional_link_count": additional_link_count,
        "answer_claim_binding_count": len(answer_claim_bindings),
        "answer_claim_bindings": answer_claim_bindings[:20],
        "answer_evidence_binding_count": len(answer_evidence_bindings),
        "answer_evidence_bindings": answer_evidence_bindings[:20],
        "evidence_contract_status": _evidence_contract_status,
        "algorithm_report_blocked": algorithm_report_blocked,
        "algorithm_failure_count": len(s.get("algorithm_failures", []) or []),
        "blocking_algorithm_failure_count": len(blocking_algorithm_failures),
        "algorithm_failures": (s.get("algorithm_failures", []) or [])[-10:],
        "section_logs": section_logs,
        "composition_aborted": composition_result.composition_aborted,
        "consecutive_empty_at_break": composition_result.consecutive_empty_at_break,
        "sections_planned": composition_result.sections_planned,
        "sections_attempted": composition_result.sections_attempted,
        "sections_rendered": len(section_logs),
        "answer_finish_reason": finish_reason,
        "answer_incomplete": bool(incomplete_reasons),
        "answer_incomplete_reasons": incomplete_reasons,
        "appended_sources_footer": appended_sources_footer,
        "fallback_model": fallback_model or "",
        "fallback_attempted": fallback_attempted,
        "fallback_succeeded": fallback_succeeded,
        "stats_line": stats_line,
        "rounds": n_rounds,
        "elapsed_total_s": round(elapsed, 1),
        "confidence": conf,
    }, testing_mode=settings.testing_mode)

    emit_progress(s, "done")
    return s
